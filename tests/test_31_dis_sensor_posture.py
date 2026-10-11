"""
dis-sim `--posture-schedule`: raise/stow mean deploy/undeploy. For a sensor
(an entity with an emission profile in the condition schedule) stow sends an
explicit zero-beam EE every tick until raise, and the launcher bit is left
alone; for a launcher they toggle the launcher-raised bit as before.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from opendis.PduFactory import createPdu  # noqa: E402

from dis_sim import (  # noqa: E402
    Entity,
    effective_emission_mode,
    emission_pdu,
    load_condition_schedule,
    parse_posture_schedule,
    resolve_condition_schedule,
    resolve_posture_schedule,
    serialize,
)

_PROFILE = {"emitter_name": 1001, "beams": 4, "erp_dbm": 70.0, "reduced_power_db": 15}


def _entity(entity_id: int = 1000) -> Entity:
    return Entity(0, site_id=1, app_id=1, rng=random.Random(1), entity_id=entity_id)


def _sensor(tmp_path, spec: str) -> Entity:
    e = _entity()
    doc = {"cycle_s": 960, "emission_profiles": {"1000": dict(_PROFILE)},
           "steps": [{"entity": 1000, "at_s": 0, "set": {}}]}
    f = tmp_path / "cond.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    # Same order as main(): posture resolved first, condition second.
    resolve_posture_schedule([e], parse_posture_schedule(spec, 1, 1))
    resolve_condition_schedule([e], load_condition_schedule(str(f), {1000}))
    return e


def _launcher(spec: str) -> Entity:
    e = _entity()
    resolve_posture_schedule([e], parse_posture_schedule(spec, 1, 1))
    return e


def _beams(pdu) -> int:
    return sum(len(s.beamRecords) for s in createPdu(serialize(pdu)).systems)


@pytest.mark.parametrize("step", ["nominal", "reduced_beams", "reduced_power", "zero"])
def test_effective_mode_stowed_zeroes_every_emitting_mode(step):
    assert effective_emission_mode(step, True) == "zero"
    assert effective_emission_mode(step, False) == step


@pytest.mark.parametrize("stowed", [False, True])
def test_effective_mode_silent_stays_silent(stowed):
    assert effective_emission_mode("silent", stowed) == "silent"


def test_sensor_stow_at_zero_applies_from_first_tick(tmp_path):
    e = _sensor(tmp_path, "1000:0:stow")
    assert e.apply_posture_schedule(0.0) is True
    assert e.emission_stowed is True
    assert e.launcher_raised is False


def test_sensor_raise_clears_stow_and_never_touches_launcher_bit(tmp_path):
    e = _sensor(tmp_path, "1000:0:stow,1000:10:raise,1000:20:stow")
    e.apply_posture_schedule(0.0)
    assert e.emission_stowed is True and e.launcher_raised is False
    e.apply_posture_schedule(10.0)
    assert e.emission_stowed is False and e.launcher_raised is False
    e.apply_posture_schedule(20.0)
    assert e.emission_stowed is True and e.launcher_raised is False


def test_launcher_without_profile_toggles_launcher_bit(tmp_path):
    e = _launcher("1000:0:raise,1000:10:stow")
    e.apply_posture_schedule(0.0)
    assert e.launcher_raised is True and e.emission_stowed is False
    e.apply_posture_schedule(10.0)
    assert e.launcher_raised is False and e.emission_stowed is False


def test_stowed_sensor_ee_has_zero_beams_unstowed_has_profile_beams(tmp_path):
    e = _sensor(tmp_path, "1000:0:stow,1000:10:raise")
    e.apply_posture_schedule(0.0)
    stowed = emission_pdu(e, e.condition_profile,
                          effective_emission_mode("nominal", e.emission_stowed), 1, 7)
    assert _beams(stowed) == 0
    e.apply_posture_schedule(10.0)
    up = emission_pdu(e, e.condition_profile,
                      effective_emission_mode("nominal", e.emission_stowed), 1, 7)
    assert _beams(up) == _PROFILE["beams"]
