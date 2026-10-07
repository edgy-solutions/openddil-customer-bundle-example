"""
dis-sim's `--posture-schedule` (launcher posture: raise/stow the
launcher-raised appearance bit, move/stop a scheduled entity's own motion).

Modelled on test_30_dis_destroy_schedule.py's --destroy-schedule shape, with
one deliberate difference pinned below -- a malformed entry here is FATAL
(SystemExit), same discipline as test_32_dis_effector_schedule.py's
--fire-schedule/--detonate-schedule, because this is a test fixture read
once rather than a live operator control.

The clock is injected throughout: `apply_posture_schedule(elapsed_s)` takes
elapsed seconds as a plain float argument, same contract as
`Entity.apply_destroy_schedule(elapsed_s)`.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    Entity,
    appearance_bits,
    parse_posture_schedule,
    resolve_posture_schedule,
)

_LAUNCHER_RAISED = 1 << 15
_POWERPLANT_ON = 1 << 21


def _entity(entity_id: int = 1000) -> Entity:
    return Entity(0, site_id=1, app_id=1, rng=random.Random(1), entity_id=entity_id)


# ---------------------------------------------------------------------------
# parse_posture_schedule
# ---------------------------------------------------------------------------

def test_parse_accepts_canonical_and_bare_keys_multiple_actions():
    out = parse_posture_schedule(
        "dis:1:1:1005:30:raise,1005:60:stow,1007:0:move", site_id=1, app_id=1,
    )
    assert out == [
        {"entity": (1, 1, 1007), "t": 0.0, "action": "move"},
        {"entity": (1, 1, 1005), "t": 30.0, "action": "raise"},
        {"entity": (1, 1, 1005), "t": 60.0, "action": "stow"},
    ]


def test_parse_empty_spec_yields_empty_schedule():
    assert parse_posture_schedule("", site_id=1, app_id=1) == []
    assert parse_posture_schedule(None, site_id=1, app_id=1) == []  # DIS_POSTURE_SCHEDULE default


@pytest.mark.parametrize("bad", [
    "1005",                 # missing T:ACTION
    "1005:30",               # missing ACTION
    "1005:abc:raise",        # non-numeric T
    "1005:-1:raise",         # negative T
    "1005:30:detonate",      # not a recognised action
    ":30:raise",              # empty entity
])
def test_bad_entry_exits_non_zero_naming_it(bad):
    with pytest.raises(SystemExit) as exc_info:
        parse_posture_schedule(bad, site_id=1, app_id=1)
    assert repr(bad) in str(exc_info.value)


# ---------------------------------------------------------------------------
# resolve_posture_schedule — matching entities to schedule entries
# ---------------------------------------------------------------------------

def test_resolve_matches_canonical_and_bare_forms_and_zeroes_motion():
    e_canonical = _entity(1005)
    e_bare = _entity(1007)
    e_unrelated = _entity(1001)  # not in the schedule
    schedule = parse_posture_schedule(
        "dis:1:1:1005:0:raise,1007:0:stow", site_id=1, app_id=1,
    )
    resolve_posture_schedule([e_canonical, e_bare, e_unrelated], schedule)
    assert e_canonical.has_posture_schedule is True
    assert e_bare.has_posture_schedule is True
    assert e_unrelated.has_posture_schedule is False
    # Zeroed so the very first PDU, before any action fires, already shows
    # a stationary launcher rather than Entity.__init__'s random drift.
    assert e_canonical.speed_mps == 0.0
    assert e_canonical.heading == 0.0


def test_unmatched_entity_is_left_completely_untouched():
    e = _entity(1001)
    original_speed = e.speed_mps
    schedule = parse_posture_schedule("1005:0:raise", site_id=1, app_id=1)
    resolve_posture_schedule([e], schedule)
    assert e.has_posture_schedule is False
    assert e.posture_schedule == []
    assert e.speed_mps == original_speed


# ---------------------------------------------------------------------------
# apply_posture_schedule — one tick at a time, off an injected elapsed_s
# ---------------------------------------------------------------------------

def test_unscheduled_entity_never_fires():
    e = _entity()
    assert e.apply_posture_schedule(10_000.0) is False
    assert e.launcher_raised is False
    assert e.emit_appearance is False


def test_raise_then_stow_toggle_launcher_raised():
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule(
        "1000:0:raise,1000:30:stow", site_id=1, app_id=1))

    assert e.apply_posture_schedule(0.0) is True
    assert e.launcher_raised is True
    assert e.emit_appearance is True

    # Holds between actions -- "stays in effect" every tick, same contract
    # as apply_destroy_schedule.
    assert e.apply_posture_schedule(15.0) is True
    assert e.launcher_raised is True

    assert e.apply_posture_schedule(30.0) is True
    assert e.launcher_raised is False


def test_move_then_stop_set_constant_speed():
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule(
        "1000:0:move,1000:60:stop", site_id=1, app_id=1))

    e.apply_posture_schedule(0.0)
    assert e.speed_mps > 0.0
    moving_speed = e.speed_mps

    e.apply_posture_schedule(30.0)
    assert e.speed_mps == moving_speed  # holds, nothing due yet

    e.apply_posture_schedule(60.0)
    assert e.speed_mps == 0.0


def test_appearance_carries_the_launcher_raised_bit_and_powerplant_stays_on():
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule("1000:0:raise", site_id=1, app_id=1))
    e.apply_posture_schedule(0.0)
    pdu = e.to_pdu(exercise_id=1, protocol_version=7)
    assert pdu.entityAppearance & _LAUNCHER_RAISED
    assert pdu.entityAppearance & _POWERPLANT_ON


def test_stow_is_an_explicit_claim_not_silence():
    """Red-check 1's shape: an entity with only a '0 stow' action still
    MAKES a claim (appearance != 0, power plant on) -- it asserts
    launcher_raised=False rather than saying nothing, which is what lets
    sensor-ingest decode a real (false) value instead of no key at all."""
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule("1000:0:stow", site_id=1, app_id=1))
    e.apply_posture_schedule(0.0)
    pdu = e.to_pdu(exercise_id=1, protocol_version=7)
    assert pdu.entityAppearance != 0
    assert not (pdu.entityAppearance & _LAUNCHER_RAISED)
    assert pdu.entityAppearance & _POWERPLANT_ON


# ---------------------------------------------------------------------------
# Precedence: an explicit --damage-map entry for the same asset wins (main()
# only calls apply_posture_schedule when apply_damage_override and apply_
# destroy_schedule both report no override this tick).
# ---------------------------------------------------------------------------

def test_damage_map_entry_wins_over_posture_schedule():
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule("1000:0:raise", site_id=1, app_id=1))
    under_override = e.apply_damage_override({"dis:1:1:1000": "moderate"})
    assert under_override is True
    # main() would NOT call apply_posture_schedule in this branch.
    assert e.damage == "moderate"


# ---------------------------------------------------------------------------
# step() — a scheduled entity's motion comes ONLY from its schedule
# ---------------------------------------------------------------------------

def test_scheduled_entity_does_not_jitter_heading():
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule("1000:0:move", site_id=1, app_id=1))
    e.apply_posture_schedule(0.0)
    assert e.heading == 0.0
    for _ in range(20):
        e.step(1.0)
    assert e.heading == 0.0  # never jittered, unlike an unscheduled entity


def test_scheduled_entity_moves_only_while_moving():
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule(
        "1000:0:move,1000:10:stop", site_id=1, app_id=1))
    e.apply_posture_schedule(0.0)
    lat_before, lon_before = e.lat, e.lon
    e.step(1.0)
    assert (e.lat, e.lon) != (lat_before, lon_before)  # moving

    e.apply_posture_schedule(10.0)
    lat_before, lon_before = e.lat, e.lon
    e.step(1.0)
    assert (e.lat, e.lon) == (lat_before, lon_before)  # stopped


def test_unscheduled_entity_still_jitters_heading_unchanged():
    """Guards that the posture branch in step() does not change existing
    (unscheduled) behaviour -- flip this guard by asserting equality instead
    of inequality to see it fail (heading WILL have moved)."""
    e = _entity()
    heading_before = e.heading
    e.step(1.0)
    assert e.heading != heading_before


def test_appearance_bits_refuses_launcher_raised_off_land():
    with pytest.raises(ValueError):
        appearance_bits(domain=2, launcher_raised=True)
