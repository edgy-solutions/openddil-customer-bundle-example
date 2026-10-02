"""
dis-sim's `DIS_EVENT_SCHEDULE_PATH`: a scheduled DIS Event Report PDU
(type 21), decoded on the sensor-ingest side as pure transport.

Neither side interprets the event -- dis-sim sends what its schedule says,
sensor-ingest (openddil-sensor-ingest/tests/event_report/) reports what
arrived. This file pins dis-sim's half only; the two repos are separate and
this file does not import sensor-ingest.

The clock and the sender are both injected, same contract as test_30_
dis_destroy_schedule.py's apply_destroy_schedule(elapsed_s): elapsed_s is a
plain float argument and the UDP send is a plain callable, so these tests
pass synthetic values directly rather than sleeping or opening a socket.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    Entity,
    due_schedule_entries,
    event_report_pdu,
    load_event_schedule,
    serialize,
)

from opendis.PduFactory import createPdu  # noqa: E402
from opendis.dis7 import EventReportPdu  # noqa: E402


def _entity(entity_id: int = 1000, site_id: int = 1, app_id: int = 1) -> Entity:
    return Entity(0, site_id=site_id, app_id=app_id, rng=random.Random(1), entity_id=entity_id)


# ---------------------------------------------------------------------------
# load_event_schedule -- validation at load
# ---------------------------------------------------------------------------

def test_unset_path_yields_no_entries():
    assert load_event_schedule("", {1000}) == []
    assert load_event_schedule(None, {1000}) == []


def test_valid_schedule_loads(tmp_path):
    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps([
        {"entity": 1000, "at_s": 5, "event_type": 42,
         "variable_datums": {"11": "hello"}, "fixed_datums": {"7": 3}},
        {"entity": 1001, "at_s": 10, "event_type": 1, "repeat_s": 30,
         "variable_datums": {}},
    ]))
    out = load_event_schedule(str(schedule_file), {1000, 1001})
    assert len(out) == 2
    assert out[0]["entity"] == 1000
    assert out[0]["at_s"] == 5.0
    assert out[0]["event_type"] == 42
    assert out[0]["variable_datums"] == {11: "hello"}
    assert out[0]["fixed_datums"] == {7: 3}
    assert out[0]["repeat_s"] is None
    assert out[1]["repeat_s"] == 30.0
    assert out[1]["fixed_datums"] == {}


def test_unknown_entity_fails_startup_naming_index(tmp_path):
    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps([
        {"entity": 9999, "at_s": 1, "event_type": 1, "variable_datums": {}},
    ]))
    with pytest.raises(SystemExit) as exc_info:
        load_event_schedule(str(schedule_file), {1000})
    assert "[0]" in str(exc_info.value)


def test_bad_entry_does_not_skip_the_rest(tmp_path):
    """A violation stops startup entirely -- it does not skip just the bad
    entry and load the rest, which would silently drop a schedule entry
    nobody asked to drop."""
    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps([
        {"entity": 1000, "at_s": 1, "event_type": 1, "variable_datums": {}},
        {"entity": 1000, "at_s": -1, "event_type": 1, "variable_datums": {}},
    ]))
    with pytest.raises(SystemExit) as exc_info:
        load_event_schedule(str(schedule_file), {1000})
    assert "[1]" in str(exc_info.value)


@pytest.mark.parametrize("field,value", [
    ("entity", "not-an-int"),
    ("at_s", -1),
    ("event_type", -1),
    ("event_type", 2 ** 32),
    ("repeat_s", 0),
    ("repeat_s", -5),
])
def test_type_and_range_violations_fail_startup(tmp_path, field, value):
    entry = {"entity": 1000, "at_s": 1, "event_type": 1, "variable_datums": {}}
    entry[field] = value
    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps([entry]))
    with pytest.raises(SystemExit) as exc_info:
        load_event_schedule(str(schedule_file), {1000})
    assert "[0]" in str(exc_info.value)


def test_oversized_variable_datum_string_fails_startup(tmp_path):
    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps([
        {"entity": 1000, "at_s": 1, "event_type": 1,
         "variable_datums": {"1": "x" * 256}},
    ]))
    with pytest.raises(SystemExit) as exc_info:
        load_event_schedule(str(schedule_file), {1000})
    assert "[0]" in str(exc_info.value)


def test_non_uint32_datum_id_key_fails_startup(tmp_path):
    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps([
        {"entity": 1000, "at_s": 1, "event_type": 1,
         "variable_datums": {"not-a-number": "x"}},
    ]))
    with pytest.raises(SystemExit) as exc_info:
        load_event_schedule(str(schedule_file), {1000})
    assert "[0]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# due_schedule_entries -- the injectable scheduler
# ---------------------------------------------------------------------------

def test_entry_fires_exactly_once_by_at_s():
    schedule = [{"entity": 1000, "at_s": 5.0, "event_type": 1,
                 "fixed_datums": {}, "variable_datums": {}, "repeat_s": None}]
    next_due = [5.0]

    assert due_schedule_entries(schedule, next_due, 4.999) == []
    assert due_schedule_entries(schedule, next_due, 5.0) == [0]
    # One-shot: does not fire again on a later tick.
    assert due_schedule_entries(schedule, next_due, 5.0) == []
    assert due_schedule_entries(schedule, next_due, 999.0) == []


def test_repeat_s_fires_again_after_the_interval():
    schedule = [{"entity": 1000, "at_s": 5.0, "event_type": 1,
                 "fixed_datums": {}, "variable_datums": {}, "repeat_s": 10.0}]
    next_due = [5.0]

    assert due_schedule_entries(schedule, next_due, 5.0) == [0]
    assert due_schedule_entries(schedule, next_due, 10.0) == []
    assert due_schedule_entries(schedule, next_due, 15.0) == [0]
    assert due_schedule_entries(schedule, next_due, 15.0) == []


def test_multiple_entries_fire_independently():
    schedule = [
        {"entity": 1000, "at_s": 5.0, "event_type": 1,
         "fixed_datums": {}, "variable_datums": {}, "repeat_s": None},
        {"entity": 1001, "at_s": 8.0, "event_type": 2,
         "fixed_datums": {}, "variable_datums": {}, "repeat_s": None},
    ]
    next_due = [5.0, 8.0]
    assert due_schedule_entries(schedule, next_due, 5.0) == [0]
    assert due_schedule_entries(schedule, next_due, 8.0) == [1]


def test_scheduled_entry_sends_exactly_one_pdu_by_at_s():
    """The end-to-end shape of what main()'s loop does, with an injected
    clock (a loop over explicit elapsed_s values -- no sleeping) and an
    injected sender (a plain list, not a socket)."""
    entity = _entity(1000)
    entry = {"entity": 1000, "at_s": 5.0, "event_type": 3,
              "fixed_datums": {}, "variable_datums": {1: "x"}, "repeat_s": None}
    schedule = [entry]
    next_due = [entry["at_s"]]
    sent: list[bytes] = []

    for elapsed_s in (0.0, 4.999, 5.0, 6.0, 100.0):
        for idx in due_schedule_entries(schedule, next_due, elapsed_s):
            pdu = event_report_pdu(entity, schedule[idx], exercise_id=1, protocol_version=7)
            sent.append(serialize(pdu))

    assert len(sent) == 1
    decoded = createPdu(sent[0])
    assert int(decoded.eventType) == 3


# ---------------------------------------------------------------------------
# event_report_pdu -- the wire round trip, exact
# ---------------------------------------------------------------------------

def test_round_trip_exact_event_type_and_datums():
    entity = _entity(1005, site_id=2, app_id=1)  # pinned: dis:2:1:1005
    entry = {
        "entity": 1005,
        "at_s": 1.0,
        "event_type": 777,
        "fixed_datums": {9: 123456},
        # "abc" is 3 bytes -- not a multiple of 8 -- which pins the
        # variableDatumLength/padding round trip opendis 1.0 does.
        # "12345678" is exactly 8 bytes, the boundary case.
        "variable_datums": {11: "abc", 22: "12345678"},
        "repeat_s": None,
    }
    pdu = event_report_pdu(entity, entry, exercise_id=1, protocol_version=7)
    data = serialize(pdu)

    decoded = createPdu(data)
    assert isinstance(decoded, EventReportPdu)
    assert int(decoded.pduType) == 21
    assert int(decoded.eventType) == 777

    assert decoded.originatingEntityID.siteID == 2
    assert decoded.originatingEntityID.applicationID == 1
    assert decoded.originatingEntityID.entityID == 1005
    # All-zero: Open-DIS's own default EntityID, used here for "no specific
    # receiver" (see event_report_pdu's docstring).
    assert decoded.receivingEntityID.siteID == 0
    assert decoded.receivingEntityID.applicationID == 0
    assert decoded.receivingEntityID.entityID == 0

    fixed = {d.fixedDatumID: d.fixedDatumValue for d in decoded._datums.fixedDatumRecords}
    assert fixed == {9: 123456}

    variable = {
        d.variableDatumID: bytes(d.variableData).decode("utf-8")
        for d in decoded._datums.variableDatumRecords
    }
    assert variable == {11: "abc", 22: "12345678"}
