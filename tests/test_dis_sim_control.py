"""dis-sim's optional posture control listener (--control-port).

Covers the pure validator (parse_posture_arm), the thread-safe arm/drain
hand-off into an entity's posture schedule, the unchanged firing path
(due_schedule_entries), and a real-socket HTTP round trip on an ephemeral
port. No simulator process is spawned.
"""
from __future__ import annotations

import http.client
import json
import random
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    _POSTURE_MOVE_SPEED_MPS,
    Entity,
    PostureArmError,
    PostureControl,
    due_schedule_entries,
    make_control_server,
    parse_posture_arm,
)

SITE, APP = 1, 1
KNOWN = {(1, 1, 1001), (1, 1, 1002)}


def _arm(entries, elapsed=10.0, known=KNOWN):
    return parse_posture_arm({"entries": entries}, SITE, APP, known, elapsed)


def _entity(eid: int) -> Entity:
    return Entity(0, site_id=SITE, app_id=APP, rng=random.Random(1), entity_id=eid)


def _control(*ids, start=None):
    known = {(SITE, APP, i) for i in ids}
    return PostureControl(SITE, APP, known, time.monotonic() if start is None else start)


# ---- validator ------------------------------------------------------------

def test_accepts_both_entity_forms_and_sorts():
    out = _arm([
        {"entity": "dis:1:1:1002", "after_s": 30, "action": "raise"},
        {"entity": 1001, "after_s": 5, "action": "stow"},
    ])
    assert [(e["entity"], e["t"], e["action"], e["source"]) for e in out] == [
        ((1, 1, 1001), 15.0, "stow", "control"),
        ((1, 1, 1002), 40.0, "raise", "control"),
    ]


def test_bare_entity_as_string():
    out = _arm([{"entity": "1001", "after_s": 0, "action": "raise"}])
    assert out[0]["entity"] == (1, 1, 1001)


def test_move_with_distance_adds_auto_stop():
    out = _arm([{"entity": 1001, "after_s": 2, "action": "move", "distance_m": 100}])
    assert len(out) == 2
    assert out[0]["action"] == "move" and out[0]["t"] == 12.0
    assert out[1]["action"] == "stop" and out[1]["source"] == "control-auto-stop"
    assert out[1]["t"] == pytest.approx(12.0 + 100 / _POSTURE_MOVE_SPEED_MPS)
    assert out[1]["t"] == pytest.approx(32.0)


def test_move_without_distance_has_no_stop():
    assert len(_arm([{"entity": 1001, "after_s": 2, "action": "move"}])) == 1


_OK = {"entity": 1001, "after_s": 0, "action": "raise"}


@pytest.mark.parametrize("body,needle", [
    ([], "object"),
    ({}, "entries"),
    ({"entries": "x"}, "entries"),
    ({"entries": []}, "1..32"),
    ({"entries": [_OK] * 33}, "1..32"),
    ({"entries": ["x"]}, "entry 0"),
    ({"entries": [{"after_s": 0, "action": "raise"}]}, "entry 0"),
    ({"entries": [{**_OK, "entity": "nope"}]}, "entry 0"),
    ({"entries": [{**_OK, "entity": True}]}, "entry 0"),
    ({"entries": [{"entity": 1001, "action": "raise"}]}, "after_s"),
    ({"entries": [{**_OK, "after_s": "1"}]}, "after_s"),
    ({"entries": [{**_OK, "after_s": True}]}, "after_s"),
    ({"entries": [{**_OK, "after_s": -1}]}, "after_s"),
    ({"entries": [{**_OK, "after_s": 3601}]}, "after_s"),
    ({"entries": [{**_OK, "after_s": float("nan")}]}, "after_s"),
    ({"entries": [{**_OK, "action": "dance"}]}, "action"),
    ({"entries": [{"entity": 1001, "after_s": 0}]}, "action"),
    ({"entries": [{**_OK, "distance_m": 5}]}, "distance_m"),
    ({"entries": [{**_OK, "action": "move", "distance_m": 0}]}, "distance_m"),
    ({"entries": [{**_OK, "action": "move", "distance_m": 50001}]}, "distance_m"),
    ({"entries": [{**_OK, "action": "move", "distance_m": "9"}]}, "distance_m"),
])
def test_rejects_malformed_400(body, needle):
    with pytest.raises(PostureArmError) as ei:
        parse_posture_arm(body, SITE, APP, KNOWN, 0.0)
    assert ei.value.status == 400
    assert needle in ei.value.message


def test_bad_entry_names_its_index():
    with pytest.raises(PostureArmError) as ei:
        _arm([_OK, {**_OK, "action": "bogus"}])
    assert ei.value.status == 400 and "entry 1" in ei.value.message


def test_unknown_entity_404_names_it():
    with pytest.raises(PostureArmError) as ei:
        _arm([_OK, {**_OK, "entity": "dis:1:1:9999"}])
    assert ei.value.status == 404 and "9999" in ei.value.message


def test_all_or_nothing_through_control():
    ctl = _control(1001)
    with pytest.raises(PostureArmError):
        ctl.arm({"entries": [_OK, {**_OK, "entity": 7777}]}, "test")
    assert ctl.drain([_entity(1001)], 0.0) == 0


# ---- drain / firing --------------------------------------------------------

def test_drain_schedules_an_unscheduled_entity_and_zeroes_speed():
    e = _entity(1001)
    e.speed_mps = 3.0
    e.heading = 1.0
    assert not e.has_posture_schedule
    ctl = _control(1001)
    ctl.arm({"entries": [{**_OK, "after_s": 5}]}, "t")
    assert ctl.drain([e], 0.0) == 1
    assert e.has_posture_schedule
    assert e.speed_mps == 0.0 and e.heading == 0.0
    assert len(e.posture_schedule) == len(e._posture_next_due) == 1
    assert ctl.drain([e], 0.1) == 0


def test_drain_does_not_rezero_an_already_scheduled_entity():
    e = _entity(1001)
    e.has_posture_schedule = True
    e.speed_mps = 5.0
    ctl = _control(1001)
    ctl.arm({"entries": [{**_OK, "after_s": 5, "action": "stow"}]}, "t")
    ctl.drain([e], 0.0)
    assert e.speed_mps == 5.0


def test_armed_entries_fire_at_the_right_elapsed_values():
    e = _entity(1001)
    start = time.monotonic() - 100.0
    ctl = _control(1001, start=start)
    ctl.arm({"entries": [{**_OK, "after_s": 10, "action": "move", "distance_m": 50}]}, "t")
    elapsed = time.monotonic() - start
    ctl.drain([e], elapsed)
    t_move = e.posture_schedule[0]["t"]
    assert t_move == pytest.approx(elapsed + 10, abs=1.0)
    assert due_schedule_entries(e.posture_schedule, e._posture_next_due, t_move - 0.5) == []
    assert due_schedule_entries(e.posture_schedule, e._posture_next_due, t_move) == [0]
    t_stop = t_move + 50 / _POSTURE_MOVE_SPEED_MPS
    assert due_schedule_entries(e.posture_schedule, e._posture_next_due, t_stop - 0.1) == []
    assert due_schedule_entries(e.posture_schedule, e._posture_next_due, t_stop) == [1]
    assert due_schedule_entries(e.posture_schedule, e._posture_next_due, t_stop + 99) == []


def test_apply_posture_schedule_runs_armed_move_then_stop():
    e = _entity(1001)
    ctl = _control(1001)
    ctl.arm({"entries": [{**_OK, "after_s": 0, "action": "move", "distance_m": 10}]}, "t")
    ctl.drain([e], 0.0)
    t0 = e.posture_schedule[0]["t"]
    e.apply_posture_schedule(t0)
    assert e.speed_mps == _POSTURE_MOVE_SPEED_MPS
    e.apply_posture_schedule(t0 + 2.0)
    assert e.speed_mps == 0.0


# ---- HTTP round trip -------------------------------------------------------

@pytest.fixture()
def server():
    ents = [_entity(1001), _entity(1002)]
    ctl = _control(1001, 1002)
    ctl.entities = ents
    srv = make_control_server(0, ctl, host="127.0.0.1")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[1], ctl, ents
    srv.shutdown()
    srv.server_close()


def _call(port, method, path, body=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    payload = r.read()
    c.close()
    return r.status, (json.loads(payload) if payload else None)


def test_http_healthz(server):
    port, _, _ = server
    assert _call(port, "GET", "/healthz") == (200, {"status": "ok"})


def test_http_arm_202_then_posture_snapshot(server):
    port, ctl, ents = server
    st, out = _call(port, "POST", "/posture/arm",
                    {"entries": [{**_OK, "after_s": 30, "action": "move", "distance_m": 25}]})
    assert st == 202
    assert [a["action"] for a in out["armed"]] == ["move", "stop"]
    assert out["armed"][0]["entity"] == "dis:1:1:1001"
    assert out["armed"][1]["source"] == "control-auto-stop"
    st, snap = _call(port, "GET", "/posture")
    assert st == 200
    row = [x for x in snap["entities"] if x["entity"] == "dis:1:1:1001"][0]
    assert [p["action"] for p in row["pending"]] == ["move", "stop"]  # not yet drained
    ctl.drain(ents, out["elapsed_s"])
    st, snap = _call(port, "GET", "/posture")
    row = [x for x in snap["entities"] if x["entity"] == "dis:1:1:1001"][0]
    assert [p["action"] for p in row["pending"]] == ["move", "stop"]  # drained, not fired
    assert {"speed_mps", "launcher_raised", "emission_stowed"} <= set(row)
    assert all(x["entity"] != "dis:1:1:1002" for x in snap["entities"])


def test_http_400_404_413_and_routing(server):
    port, ctl, _ = server
    assert _call(port, "POST", "/posture/arm", {"entries": []})[0] == 400
    st, out = _call(port, "POST", "/posture/arm", raw=b"not json")
    assert st == 400 and "error" in out
    st, out = _call(port, "POST", "/posture/arm", {"entries": [{**_OK, "entity": 4242}]})
    assert st == 404 and "4242" in out["error"]
    st, out = _call(port, "POST", "/posture/arm", raw=b" " * (16 * 1024 + 1))
    assert st == 413
    assert ctl.drain([_entity(1001)], 0.0) == 0
    assert _call(port, "GET", "/nope")[0] == 404
    assert _call(port, "GET", "/posture/arm")[0] == 405
    assert _call(port, "POST", "/healthz", {})[0] == 405
    assert _call(port, "POST", "/posture", {})[0] == 405
