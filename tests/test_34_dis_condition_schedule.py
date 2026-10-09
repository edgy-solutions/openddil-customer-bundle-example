"""
dis-sim's `--condition-schedule`: a timed list of steps that flip an entity's
appearance bits, its Electromagnetic Emission PDU and a Data PDU health datum.

Modelled on test_33_dis_posture_schedule.py. The clock is injected: every
resolution call takes elapsed seconds as a plain float.

The round-trip tests decode serialized bytes with opendis' PduFactory and read
them through the same attribute paths the receiving sidecar uses
(openddil-sensor-ingest dis_ingestor.py `_extract_emission_systems` and
`_handle_data`), so a PDU that this generator builds but the receiver could
not read fails here.
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
    appearance_bits,
    emission_pdu,
    health_data_pdu,
    load_condition_schedule,
    resolve_condition_schedule,
    serialize,
)

_POWERPLANT_ON = 1 << 21
_DEACTIVATED = 1 << 22
_EXAMPLE = (Path(__file__).resolve().parents[1]
            / "tools" / "dis-sim" / "examples" / "condition-schedule.example.json")
_PROFILE = {"emitter_name": 1001, "beams": 4, "erp_dbm": 70.0, "reduced_power_db": 15}


def _entity(entity_id: int = 1000) -> Entity:
    return Entity(0, site_id=1, app_id=1, rng=random.Random(1), entity_id=entity_id)


def _doc(**over) -> dict:
    doc = {
        "cycle_s": 960,
        "emission_profiles": {"1000": dict(_PROFILE)},
        "steps": [
            {"entity": 1000, "at_s": 0, "set": {}},
            {"entity": 1000, "at_s": 90, "set": {"damage": "slight"}},
            {"entity": 1000, "at_s": 180, "set": {"emission": "reduced_beams"}},
        ],
    }
    doc.update(over)
    return doc


def _load(tmp_path, doc, ids=(1000,)):
    f = tmp_path / "cond.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return load_condition_schedule(str(f), set(ids))


def _armed(tmp_path, doc=None) -> Entity:
    e = _entity()
    resolve_condition_schedule([e], _load(tmp_path, doc or _doc()))
    return e


# ---------------------------------------------------------------------------
# Parse / validate
# ---------------------------------------------------------------------------

def test_example_file_loads():
    sched = load_condition_schedule(str(_EXAMPLE), {1000})
    assert sched["cycle_s"] == 630.0
    assert sched["datum_id"] == 61000
    assert [s["at_s"] for s in sched["steps"][1000]] == [0, 90, 180, 270, 360, 450, 540]


def test_unset_path_is_none(monkeypatch):
    monkeypatch.delenv("DIS_CONDITION_SCHEDULE_PATH", raising=False)
    assert load_condition_schedule(None, {1000}) is None


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["steps"].append({"entity": 4242, "at_s": 5, "set": {}}), "steps[3]"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": -1, "set": {}}), "steps[3]"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": "5", "set": {}}), "steps[3]"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 960, "set": {}}), "cycle_s"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"damage": "bent"}}), "damage"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"power_plant": "idle"}}), "power_plant"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"deactivated": "yes"}}), "deactivated"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"emission": "loud"}}), "emission"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"datum_health": 101}}), "datum_health"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"datum_health": -1}}), "datum_health"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"datum_health": 40.5}}), "datum_health"),
    (lambda d: d["steps"].append({"entity": 1000, "at_s": 5, "set": {"colour": "red"}}), "unknown key"),
], ids=["bad-entity", "negative-at", "string-at", "at-beyond-cycle", "bad-damage",
        "bad-power-plant", "bad-deactivated", "bad-emission", "health-high",
        "health-low", "health-float", "unknown-set-key"])
def test_refusals_name_the_entry(tmp_path, mutate, needle):
    doc = _doc()
    mutate(doc)
    with pytest.raises(SystemExit) as exc:
        _load(tmp_path, doc)
    assert needle in str(exc.value)


def test_emission_without_profile_is_refused(tmp_path):
    doc = _doc(emission_profiles={})
    with pytest.raises(SystemExit) as exc:
        _load(tmp_path, doc)
    assert "emission_profiles" in str(exc.value) and "steps[0]" in str(exc.value)


def test_silent_only_entity_needs_no_profile(tmp_path):
    doc = _doc(emission_profiles={}, steps=[
        {"entity": 1000, "at_s": 0, "set": {"emission": "silent"}}])
    assert _load(tmp_path, doc)["steps"][1000][0]["emission"] == "silent"


def test_datum_health_null_is_accepted(tmp_path):
    doc = _doc()
    doc["steps"].append({"entity": 1000, "at_s": 5, "set": {"datum_health": None}})
    assert _load(tmp_path, doc)["steps"][1000][1]["datum_health"] is None


def test_first_step_power_off_undamaged_is_refused(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {"power_plant": "off"}},
                      {"entity": 1000, "at_s": 30, "set": {}}])
    with pytest.raises(SystemExit) as exc:
        _load(tmp_path, doc)
    assert "steps[0]" in str(exc.value) and "powered-on" in str(exc.value)


def test_first_step_is_by_at_s_not_file_order(tmp_path):
    # Listed first in the file but second by time: the powered-on step opens.
    doc = _doc(steps=[{"entity": 1000, "at_s": 60, "set": {"power_plant": "off"}},
                      {"entity": 1000, "at_s": 0, "set": {}}])
    assert len(_load(tmp_path, doc)["steps"][1000]) == 2


# ---------------------------------------------------------------------------
# Step resolution
# ---------------------------------------------------------------------------

def test_before_first_step_makes_no_claim(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 30, "set": {}}])
    e = _armed(tmp_path, doc)
    assert e.apply_condition_schedule(10.0) is None
    assert e.emit_appearance is False
    assert e.condition is None  # main() sends no EE and no Data for this


def test_entity_without_steps_is_untouched(tmp_path):
    e = _entity(1001)
    resolve_condition_schedule([e], _load(tmp_path, _doc(), ids=(1000, 1001)))
    assert e.apply_condition_schedule(500.0) is None
    assert e.emit_appearance is False and e.condition is None


def test_defaults_are_nominal(tmp_path):
    e = _armed(tmp_path)
    step = e.apply_condition_schedule(1.0)
    assert (step["damage"], step["power_plant"], step["deactivated"]) == ("none", "on", False)
    assert step["emission"] == "nominal" and step["datum_health"] == 92
    assert e.emit_appearance is True


@pytest.mark.parametrize("set_", [
    {"power_plant": "off", "damage": "slight"},
    {"deactivated": True},
    {"damage": "destroyed"},
], ids=["power-off", "deactivated", "destroyed"])
def test_off_entity_does_not_radiate(tmp_path, set_):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {}},
                      {"entity": 1000, "at_s": 10, "set": set_}])
    step = _armed(tmp_path, doc).apply_condition_schedule(20.0)
    assert step["emission"] == "silent" and step["datum_health"] is None


def test_explicit_keys_beat_derived_defaults(tmp_path):
    doc = _doc(steps=[
        {"entity": 1000, "at_s": 0, "set": {"emission": "silent"}},   # powered on, silent
        {"entity": 1000, "at_s": 10, "set": {"deactivated": True, "emission": "nominal",
                                              "datum_health": 7}},
    ])
    e = _armed(tmp_path, doc)
    s0 = e.apply_condition_schedule(1.0)
    assert s0["emission"] == "silent" and s0["power_plant"] == "on" and s0["datum_health"] == 92
    s1 = e.apply_condition_schedule(11.0)
    assert s1["emission"] == "nominal" and s1["datum_health"] == 7


def test_cycle_wraps_to_the_90s_step(tmp_path):
    e = _armed(tmp_path)
    step = e.apply_condition_schedule(960 + 95)
    assert step["at_s"] == 90 and step["damage"] == "slight"


def test_step_change_is_logged_once(tmp_path, caplog):
    e = _armed(tmp_path)
    with caplog.at_level("INFO", logger="dis-sim"):
        for t in (1.0, 2.0, 3.0, 95.0, 96.0):
            e.apply_condition_schedule(t)
    assert len([r for r in caplog.records if "condition step" in r.getMessage()]) == 2


# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------

def _es_bits(e: Entity) -> int:
    return e.to_pdu(1, 7).entityAppearance


def test_slight_damage_sets_damage_bits_and_power_plant(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {"damage": "slight"}}])
    e = _armed(tmp_path, doc)
    e.apply_condition_schedule(1.0)
    bits = _es_bits(e)
    assert (bits >> 3) & 0x3 == 1 and bits & _POWERPLANT_ON


def test_power_off_undamaged_is_all_zero_with_emit_appearance(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {}},
                      {"entity": 1000, "at_s": 10, "set": {"power_plant": "off"}}])
    e = _armed(tmp_path, doc)
    e.apply_condition_schedule(1.0)
    assert _es_bits(e) != 0
    e.apply_condition_schedule(11.0)
    assert e.emit_appearance is True
    assert _es_bits(e) == 0


def test_deactivated_sets_bit_22(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {}},
                      {"entity": 1000, "at_s": 10, "set": {"deactivated": True}}])
    e = _armed(tmp_path, doc)
    e.apply_condition_schedule(11.0)
    assert _es_bits(e) & _DEACTIVATED


def test_first_step_deactivated_undamaged_is_refused(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {"deactivated": True}}])
    with pytest.raises(SystemExit) as exc:
        _load(tmp_path, doc)
    assert "steps[0]" in str(exc.value)


# ---------------------------------------------------------------------------
# Round trips: what the receiver reads
# ---------------------------------------------------------------------------

def _decode(pdu_bytes: bytes):
    return createPdu(pdu_bytes)


def _extract_emission_systems(pdu) -> list[dict]:
    """Copied attribute paths from openddil-sensor-ingest dis_ingestor.py
    `_extract_emission_systems` -- keep in step with it."""
    systems = []
    for s in pdu.systems:
        systems.append({
            "emitter_name": int(s.emitterSystem.emitterName),
            "beams": [{"erp_dbm": float(b.fundamentalParameterData.effectiveRadiatedPower)}
                      for b in s.beamRecords],
        })
    return systems


@pytest.mark.parametrize("mode,n_beams,erp", [
    ("nominal", 4, 70.0),
    ("reduced_beams", 2, 70.0),
    ("reduced_power", 4, 55.0),
], ids=["nominal", "reduced_beams", "reduced_power"])
def test_emission_round_trip(mode, n_beams, erp):
    raw = serialize(emission_pdu(_entity(), _PROFILE, mode, 1, 7))
    pdu = _decode(raw)
    assert int(pdu.pduType) == 23 and int(pdu.protocolFamily) == 6
    assert int(pdu.length) == len(raw)
    assert int(pdu.emittingEntityID.entityID) == 1000
    systems = _extract_emission_systems(pdu)
    assert len(systems) == 1 and systems[0]["emitter_name"] == 1001
    assert len(systems[0]["beams"]) == n_beams
    assert all(b["erp_dbm"] == erp for b in systems[0]["beams"])


def test_emission_zero_has_no_systems():
    raw = serialize(emission_pdu(_entity(), _PROFILE, "zero", 1, 7))
    pdu = _decode(raw)
    assert int(pdu.length) == len(raw)
    assert _extract_emission_systems(pdu) == []


def test_emission_odd_beam_count_halves_down_but_not_below_one():
    one = {**_PROFILE, "beams": 1}
    three = {**_PROFILE, "beams": 3}
    assert len(_extract_emission_systems(_decode(serialize(
        emission_pdu(_entity(), one, "reduced_beams", 1, 7))))[0]["beams"]) == 1
    assert len(_extract_emission_systems(_decode(serialize(
        emission_pdu(_entity(), three, "reduced_beams", 1, 7))))[0]["beams"]) == 1


def test_emission_lengths_are_consistent():
    """systemDataLength / beamDataLength are the caller's in opendis 1.0."""
    raw = serialize(emission_pdu(_entity(), _PROFILE, "nominal", 1, 7))
    pdu = _decode(raw)
    system = pdu.systems[0]
    assert int(system.systemDataLength) * 4 == 20 + 52 * 4
    assert all(int(b.beamDataLength) * 4 == 52 for b in system.beamRecords)


def test_silent_builds_no_pdu():
    with pytest.raises(ValueError):
        emission_pdu(_entity(), _PROFILE, "silent", 1, 7)


def test_data_round_trip():
    raw = serialize(health_data_pdu(_entity(), 61000, 40, 1, 7))
    pdu = _decode(raw)
    assert int(pdu.pduType) == 20 and int(pdu.protocolFamily) == 5
    assert int(pdu.length) == len(raw)
    assert int(pdu.originatingEntityID.entityID) == 1000
    # The paths _handle_data reads.
    assert [(int(fd.fixedDatumID), int(fd.fixedDatumValue)) for fd in pdu.fixedDatumRecords] \
        == [(61000, 40)]


# ---------------------------------------------------------------------------
# Precedence and the default run
# ---------------------------------------------------------------------------

def test_live_damage_map_beats_the_step(tmp_path):
    doc = _doc(steps=[{"entity": 1000, "at_s": 0, "set": {"damage": "slight",
                                                           "power_plant": "off"}}])
    e = _armed(tmp_path, doc)
    assert e.apply_damage_override({"dis:1:1:1000": "moderate"}) is True
    step = e.apply_condition_schedule(1.0, appearance_free=False)
    assert e.damage == "moderate"
    assert e.powerplant_on is True          # the step's power-off does not leak in
    assert step["emission"] == "silent"     # emission still follows the step
    assert (_es_bits(e) >> 3) & 0x3 == 2


def test_postures_launcher_raised_survives_a_step(tmp_path):
    e = _armed(tmp_path, _doc(steps=[{"entity": 1000, "at_s": 0, "set": {"damage": "slight"}}]))
    e.launcher_raised = True
    e.speed_mps = 0.0
    e.apply_condition_schedule(1.0)
    assert e.launcher_raised is True and e.speed_mps == 0.0
    assert _es_bits(e) & (1 << 15)


def test_default_run_es_bytes_unchanged():
    e = _entity()
    assert e.powerplant_on is True and e.condition_steps == []
    e.emit_appearance = True
    e.damage = "slight"
    expected = appearance_bits(domain=e.entity_type[1], damage="slight", mobility_kill=False,
                               firepower_kill=False, deactivated=False, launcher_raised=False)
    assert e.to_pdu(1, 7).entityAppearance == expected
    e.emit_appearance = False
    assert e.to_pdu(1, 7).entityAppearance == 0
