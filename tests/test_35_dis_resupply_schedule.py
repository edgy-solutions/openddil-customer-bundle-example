"""
dis-sim's `--resupply-schedule` (Resupply Received, PDU type 7, Logistics
family 3): a timed schedule modelled on test_32_dis_effector_schedule.py's
--fire-schedule, with an optional `/R` repeat, and a real DIS header
timestamp so repeated resupplies to one launcher stay distinct events
downstream.

NO SOCKET: parse functions, the PDU builder and the scheduler are called
directly. The wire test decodes the serialized bytes with a struct-based
copy of the receiver's parser logic written inline below.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    dis_timestamp,
    due_schedule_entries,
    parse_munition_type,
    parse_resupply_schedule,
    resupply_pdu,
    serialize,
)

MUNITION = parse_munition_type("2.9.225.2.1.1.0")


def _decode(data: bytes) -> dict:
    """Inline copy of the receiver's Resupply Received layout: 12-byte
    header, receiving id (6), supplying id (6), supply count (1), 3 bytes
    padding, then 12 bytes per supply (EntityType 8 + float32 quantity)."""
    ver, ex, ptype, family, ts, length, status, _pad = struct.unpack(">BBBBIHBB", data[:12])
    r_site, r_app, r_ent = struct.unpack(">HHH", data[12:18])
    s_site, s_app, s_ent = struct.unpack(">HHH", data[18:24])
    n = data[24]
    padding = data[25:28]
    supplies = []
    off = 28
    for _ in range(n):
        kind, dom, country, cat, sub, spec, extra = struct.unpack(">BBHBBBB", data[off:off + 8])
        (qty,) = struct.unpack(">f", data[off + 8:off + 12])
        supplies.append(((kind, dom, country, cat, sub, spec, extra), qty))
        off += 12
    return {
        "type": ptype, "family": family, "timestamp": ts, "length": length,
        "receiving": (r_site, r_app, r_ent), "supplying": (s_site, s_app, s_ent),
        "n": n, "padding": padding, "supplies": supplies, "size": len(data),
    }


# ---------------------------------------------------------------------------
# parse_resupply_schedule
# ---------------------------------------------------------------------------

def test_one_shot_entry_has_no_repeat_and_defaults_supplier():
    out = parse_resupply_schedule("1009@600:3", site_id=1, app_id=1)
    assert out == [{"launcher": (1, 1, 1009), "t": 600.0, "quantity": 3.0,
                    "supplier": (0, 0, 0)}]
    assert "repeat_s" not in out[0]


def test_repeat_entry_carries_repeat_s():
    out = parse_resupply_schedule("1009@600/600:3", site_id=1, app_id=1)
    assert out[0]["t"] == 600.0
    assert out[0]["repeat_s"] == 600.0
    assert out[0]["quantity"] == 3.0


def test_supplier_given_in_both_forms():
    out = parse_resupply_schedule("dis:1:2:57001@10:1.5:dis:3:4:5,57002@20:2:77",
                                  site_id=9, app_id=8)
    assert out[0]["launcher"] == (1, 2, 57001)
    assert out[0]["supplier"] == (3, 4, 5)
    assert out[0]["quantity"] == 1.5
    assert out[1]["launcher"] == (9, 8, 57002)
    assert out[1]["supplier"] == (9, 8, 77)


def test_empty_spec_yields_empty_list():
    assert parse_resupply_schedule("", site_id=1, app_id=1) == []
    assert parse_resupply_schedule(None, site_id=1, app_id=1) == []


@pytest.mark.parametrize("bad", [
    "1009",             # missing @
    "1009@10",          # missing quantity
    "1009@10:0",        # Q == 0
    "1009@10:-2",       # Q < 0
    "1009@10:abc",      # non-numeric Q
    "1009@10/0:3",      # R == 0
    "1009@10/-5:3",     # R < 0
    "abc@10:3",         # non-integer entity
    "1009@10:3:xyz",    # non-integer supplier entity
    "@10:3",            # empty launcher
])
def test_bad_entry_exits_non_zero_naming_it(bad):
    with pytest.raises(SystemExit) as exc_info:
        parse_resupply_schedule(bad, site_id=1, app_id=1)
    assert repr(bad) in str(exc_info.value)


# ---------------------------------------------------------------------------
# wire format
# ---------------------------------------------------------------------------

def test_resupply_pdu_wire_roundtrip():
    entry = parse_resupply_schedule("dis:1:1:1009@5:2.5:dis:2:3:4", 1, 1)[0]
    data = serialize(resupply_pdu(entry, MUNITION, exercise_id=7,
                                  protocol_version=7, now=1234.5))
    d = _decode(data)
    assert d["size"] == 40 == d["length"]
    assert d["type"] == 7
    assert d["family"] == 3
    assert d["n"] == 1
    assert d["padding"] == b"\x00\x00\x00"
    assert d["receiving"] == (1, 1, 1009)
    assert d["supplying"] == (2, 3, 4)
    assert d["supplies"] == [((2, 9, 225, 2, 1, 1, 0), 2.5)]


def test_resupply_pdu_default_supplier_is_zero():
    entry = parse_resupply_schedule("1009@5:1", 1, 1)[0]
    d = _decode(serialize(resupply_pdu(entry, MUNITION, 1, 7)))
    assert d["supplying"] == (0, 0, 0)


# ---------------------------------------------------------------------------
# timestamp
# ---------------------------------------------------------------------------

def test_dis_timestamp_is_odd_monotonic_and_moves_per_second():
    a, b, c = dis_timestamp(100.0), dis_timestamp(101.0), dis_timestamp(1800.0)
    assert a & 1 and b & 1 and c & 1
    assert a < b < c
    assert a != b
    assert 0 <= dis_timestamp() <= 0xFFFFFFFF


def test_resupply_pdus_one_second_apart_have_different_header_timestamps():
    entry = parse_resupply_schedule("1009@5:1", 1, 1)[0]
    d1 = _decode(serialize(resupply_pdu(entry, MUNITION, 1, 7, now=1000.0)))
    d2 = _decode(serialize(resupply_pdu(entry, MUNITION, 1, 7, now=1001.0)))
    assert d1["timestamp"] != d2["timestamp"]
    assert d1["timestamp"] != 0


# ---------------------------------------------------------------------------
# scheduler timing
# ---------------------------------------------------------------------------

def test_repeat_form_is_due_at_t_t_plus_r_t_plus_2r():
    sched = parse_resupply_schedule("1009@600/600:3", site_id=1, app_id=1)
    next_due = [e["t"] for e in sched]
    assert due_schedule_entries(sched, next_due, 599.9) == []
    assert due_schedule_entries(sched, next_due, 600.0) == [0]
    assert due_schedule_entries(sched, next_due, 1199.9) == []
    assert due_schedule_entries(sched, next_due, 1200.0) == [0]
    assert due_schedule_entries(sched, next_due, 1799.9) == []
    assert due_schedule_entries(sched, next_due, 1800.0) == [0]


def test_one_shot_form_fires_once():
    sched = parse_resupply_schedule("1009@10:3", site_id=1, app_id=1)
    next_due = [e["t"] for e in sched]
    assert due_schedule_entries(sched, next_due, 10.0) == [0]
    assert due_schedule_entries(sched, next_due, 9999.0) == []
