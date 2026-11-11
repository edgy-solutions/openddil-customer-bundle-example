"""Tests for the template unit. Keep the rule tests when you copy this:
they are the four rules' checks, and they run with no broker and no network."""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

from algorithm_runtime import declaration, guard

UNIT_DIR = Path(__file__).resolve().parents[1] / "speed_watch"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", refuse)


def make_unit(overrides=None):
    decl = declaration.load(UNIT_DIR / "unit.yaml")
    guard.check_unit_dir(UNIT_DIR)
    name = "kit_template.speed_watch"
    spec = importlib.util.spec_from_file_location(name, UNIT_DIR / "speed_watch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.SpeedWatch(declaration.resolve_params(decl, overrides)), decl


def sample(speed, t=0.0):
    return {"speed_mps": speed, "sample_time_s": t}


def run(unit, speeds):
    state = {"over_limit_count": 0}
    return [unit.step(sample(s, float(i)), state) for i, s in enumerate(speeds)]


def test_fires_once_when_the_count_is_reached():
    unit, _ = make_unit({"min_samples": 2})
    out = run(unit, [25.0, 25.0, 25.0])
    assert out[0] is None and out[2] is None
    assert out[1]["anomaly"]["evidence"] == {"speed_mps": 25.0, "at_s": 1.0}


def test_a_dip_restarts_the_count():
    unit, _ = make_unit({"min_samples": 2})
    assert [x is not None for x in run(unit, [25.0, 5.0, 25.0, 25.0])] == [False, False, False, True]


def test_a_sample_with_no_speed_changes_nothing():
    unit, _ = make_unit()
    state = {"over_limit_count": 2}
    assert unit.step({"sample_time_s": 0.0}, state) is None
    assert state == {"over_limit_count": 2}


def test_params_come_from_the_yaml():
    unit, _ = make_unit({"speed_limit_mps": 50.0, "min_samples": 1})
    assert run(unit, [25.0]) == [None]


def test_rule_1_declares_its_inputs_by_role():
    _, decl = make_unit()
    assert {r.role for r in decl.roles} == {"sample_time_s", "speed_mps"}


def test_rule_3_passes_the_composability_gate():
    # declaration.load raises if a terminal unit emits upward.
    declaration.load(UNIT_DIR / "unit.yaml")


def test_rule_4_passes_the_import_guard_with_no_dependencies():
    # declaration.load refuses a non-empty dependencies list.
    guard.check_unit_dir(UNIT_DIR)
    declaration.load(UNIT_DIR / "unit.yaml")


def fast_event(asset="asset-1", t=0, nation=""):
    from openddil.telemetry.v1 import telemetry_pb2 as pb
    evt = pb.EntityTelemetryEvent()
    evt.asset.asset_id = asset
    evt.kinematics.velocity.ecef.x = 50.0
    evt.kinematics.velocity.ecef.unit = "m/s"
    evt.provenance.sample_time.seconds = t + 1
    if nation:
        evt.provenance.originator_nation = nation
        evt.provenance.releasable_to.append(nation)
    return evt.SerializeToString()


def run_host(monkeypatch, nation):
    from algorithm_runtime import core
    monkeypatch.setenv("OPENDDIL_EDGE_ID", "edge-test")
    monkeypatch.setenv("OPENDDIL_REGION_ID", "region-test")
    unit, decl = make_unit()
    state, sends = None, []
    for t in range(3):
        state, out, _ = core.process_one(unit, decl, fast_event(t=t, nation=nation), state)
        sends.extend(out)
    assert len(sends) == 1
    return sends[0]


def test_rule_2_the_host_labels_the_output_from_its_source(monkeypatch):
    send = run_host(monkeypatch, "ATL")
    assert "ATL" in json.dumps(send[2])


def test_rule_2_an_unlabelled_source_stays_unlabelled(monkeypatch):
    send = run_host(monkeypatch, "")
    assert "originator_nation" not in json.dumps(send[2])


def test_rule_2_a_payload_cannot_state_severity():
    from algorithm_runtime import outputs
    with pytest.raises(outputs.OutputError):
        outputs.build_payload("anomaly", {"summary": "x", "evidence": {}, "severity": "critical"})
