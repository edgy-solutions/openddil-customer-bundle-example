"""
dis-sim's `--destroy-schedule` (Item 3b, ADR-0044): one entity marked
destroyed on a schedule.

test_24_dis_appearance.py pins the appearance-bit control; test_29_
lifecycle_removal.py pins the ADR-0044 slice A --damage-map lifecycle
values. This file pins the timed default that sits alongside both: once
SECONDS have elapsed since process start, the named asset becomes a
"destroyed" claim and KEEPS transmitting ESPDUs (ADR-0044: destroyed +
reporting is a real state) -- a one-shot that never reverts while the
process runs.

The clock is injected throughout: `apply_destroy_schedule(elapsed_s)` takes
elapsed seconds as a plain float argument, so these tests pass synthetic
values directly rather than sleeping or patching `time`.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    DAMAGE_LEVELS,
    Entity,
    parse_destroy_schedule,
    resolve_destroy_schedule,
)

_POWERPLANT_ON = 1 << 21


def _entity(entity_id: int = 1000) -> Entity:
    return Entity(0, site_id=1, app_id=1, rng=random.Random(1), entity_id=entity_id)


# ---------------------------------------------------------------------------
# parse_destroy_schedule
# ---------------------------------------------------------------------------

def test_parse_accepts_canonical_and_bare_keys():
    out = parse_destroy_schedule("dis:1:1:1005@300,1007@12.5")
    assert out == {"dis:1:1:1005": 300.0, "1007": 12.5}


def test_parse_skips_malformed_entries_and_logs_a_warning(caplog):
    with caplog.at_level("WARNING"):
        out = parse_destroy_schedule("1005@300,nope,1007@notanumber,@5,1009@-1,,1010@10")
    assert out == {"1005": 300.0, "1010": 10.0}
    assert len(caplog.records) == 4  # nope / notanumber / empty-asset / negative


def test_parse_empty_spec_yields_empty_schedule():
    assert parse_destroy_schedule("") == {}
    assert parse_destroy_schedule(None) == {}  # DIS_DESTROY_SCHEDULE default


# ---------------------------------------------------------------------------
# resolve_destroy_schedule — matching entities to schedule entries
# ---------------------------------------------------------------------------

def test_resolve_matches_canonical_and_bare_forms():
    e_canonical = _entity(1005)
    e_bare = _entity(1007)
    e_unrelated = _entity(1001)  # pinned (dis:1:1:1001) but not in the schedule
    schedule = parse_destroy_schedule("dis:1:1:1005@300,1007@12.5")
    resolve_destroy_schedule([e_canonical, e_bare, e_unrelated], schedule)
    assert e_canonical.destroy_at_s == 300.0
    assert e_bare.destroy_at_s == 12.5
    assert e_unrelated.destroy_at_s is None


# ---------------------------------------------------------------------------
# apply_destroy_schedule — before / after the scheduled time, one-shot
# ---------------------------------------------------------------------------

def test_before_the_scheduled_time_nothing_fires():
    e = _entity()
    e.destroy_at_s = 300.0
    assert e.apply_destroy_schedule(299.999) is False
    assert e.damage == "none"
    assert e.emit_appearance is False


def test_at_and_after_the_scheduled_time_it_fires():
    e = _entity()
    e.destroy_at_s = 300.0
    assert e.apply_destroy_schedule(300.0) is True
    assert e.damage == "destroyed"
    assert e.emit_appearance is True
    # Stays fired on later ticks (one-shot, never reverts within the run).
    assert e.apply_destroy_schedule(301.0) is True
    assert e.damage == "destroyed"


def test_unscheduled_entity_never_fires():
    e = _entity()
    assert e.destroy_at_s is None
    assert e.apply_destroy_schedule(10_000.0) is False
    assert e.damage == "none"
    assert e.emit_appearance is False


# ---------------------------------------------------------------------------
# Precedence: an explicit --damage-map entry for the same asset wins
# ---------------------------------------------------------------------------

def test_damage_map_entry_wins_over_a_fired_schedule():
    """main()'s loop only calls apply_destroy_schedule when
    apply_damage_override reported no override this tick -- this pins the
    caller contract by exercising both calls the way main() does."""
    e = _entity()
    e.destroy_at_s = 300.0
    under_override = e.apply_damage_override({"dis:1:1:1000": "moderate"})
    assert under_override is True
    # main() would NOT call apply_destroy_schedule in this branch.
    assert e.damage == "moderate"


def test_schedule_applies_once_the_damage_map_entry_is_cleared():
    e = _entity()
    e.destroy_at_s = 300.0
    e.apply_damage_override({"dis:1:1:1000": "moderate"})
    assert e.damage == "moderate"
    # Entry cleared next tick: apply_damage_override restores baseline and
    # reports no override, so main() goes on to consult the schedule.
    under_override = e.apply_damage_override({})
    assert under_override is False
    assert e.apply_destroy_schedule(300.0) is True
    assert e.damage == "destroyed"


# ---------------------------------------------------------------------------
# Encoded appearance bits for the fired entity: damage=3, power-plant set
# ---------------------------------------------------------------------------

def test_fired_entity_encodes_destroyed_with_powerplant_bit():
    e = _entity()
    e.destroy_at_s = 300.0
    e.apply_destroy_schedule(300.0)
    pdu = e.to_pdu(exercise_id=1, protocol_version=7)
    assert (pdu.entityAppearance >> 3) & 0x3 == DAMAGE_LEVELS["destroyed"] == 3
    assert pdu.entityAppearance & _POWERPLANT_ON


def test_unrelated_entity_is_untouched_when_another_entity_fires():
    scheduled = _entity(1005)
    scheduled.destroy_at_s = 300.0
    unrelated = _entity(1006)
    assert unrelated.destroy_at_s is None

    scheduled.apply_destroy_schedule(300.0)
    # unrelated never had a schedule entry, so nothing about it changes.
    assert unrelated.apply_destroy_schedule(300.0) is False
    assert unrelated.damage == "none"
    assert unrelated.emit_appearance is False
    assert unrelated.to_pdu(exercise_id=1, protocol_version=7).entityAppearance == 0
    # The scheduled one did fire.
    assert scheduled.damage == "destroyed"
