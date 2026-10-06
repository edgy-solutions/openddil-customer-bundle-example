"""
dis-sim's `--fire-schedule` / `--detonate-schedule` (effector events):
two timed one-shot schedules modelled on test_30_dis_destroy_schedule.py's
--destroy-schedule, with one deliberate difference pinned below -- a
malformed entry here is FATAL (SystemExit), not logged-and-skipped, because
this is a test fixture read once rather than a live operator control.

PURE PARSE/RESOLVE TESTS, NO SOCKET: these call dis_sim's parse_* functions
directly and check the entries that come back (or the SystemExit that
doesn't).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    parse_detonate_schedule,
    parse_fire_schedule,
    parse_munition_type,
)


# ---------------------------------------------------------------------------
# parse_fire_schedule
# ---------------------------------------------------------------------------

def test_fire_schedule_parses_canonical_and_bare_launcher_and_target():
    out = parse_fire_schedule("dis:1:1:57001@10:1:1:dis:1:1:57010,57002@20:2:3",
                              site_id=1, app_id=1)
    assert out == [
        {"launcher": (1, 1, 57001), "t": 10.0, "event": 1, "quantity": 1,
         "target": (1, 1, 57010)},
        {"launcher": (1, 1, 57002), "t": 20.0, "event": 2, "quantity": 3,
         "target": (0, 0, 0)},
    ]


def test_fire_schedule_no_target_defaults_to_zero():
    out = parse_fire_schedule("57001@1:1:1", site_id=1, app_id=1)
    assert out[0]["target"] == (0, 0, 0)


def test_fire_schedule_empty_spec_yields_empty_list():
    assert parse_fire_schedule("", site_id=1, app_id=1) == []
    assert parse_fire_schedule(None, site_id=1, app_id=1) == []  # DIS_FIRE_SCHEDULE default


@pytest.mark.parametrize("bad", [
    "57001",             # missing @T:E:Q
    "57001@10:1",         # missing quantity
    "57001@abc:1:1",      # non-numeric T
    "@10:1:1",             # empty launcher
])
def test_fire_schedule_bad_entry_exits_non_zero_naming_it(bad):
    with pytest.raises(SystemExit) as exc_info:
        parse_fire_schedule(bad, site_id=1, app_id=1)
    assert repr(bad) in str(exc_info.value)


# ---------------------------------------------------------------------------
# parse_detonate_schedule
# ---------------------------------------------------------------------------

def test_detonate_schedule_resolves_launcher_from_fire_schedule():
    fires = parse_fire_schedule("57001@1:1:1", site_id=1, app_id=1)
    fire_by_event = {e["event"]: e for e in fires}
    out = parse_detonate_schedule("1@5:3", site_id=1, app_id=1, fire_by_event=fire_by_event)
    assert out == [{"event": 1, "t": 5.0, "result": 3, "launcher": (1, 1, 57001)}]


def test_detonate_schedule_explicit_launcher_overrides_fire_schedule():
    fires = parse_fire_schedule("57001@1:1:1", site_id=1, app_id=1)
    fire_by_event = {e["event"]: e for e in fires}
    out = parse_detonate_schedule("1@5:3:dis:1:1:99999", site_id=1, app_id=1,
                                  fire_by_event=fire_by_event)
    assert out[0]["launcher"] == (1, 1, 99999)


def test_orphan_detonation_without_launcher_override_is_refused_at_parse():
    with pytest.raises(SystemExit) as exc_info:
        parse_detonate_schedule("9@5:3", site_id=1, app_id=1, fire_by_event={})
    assert "9" in str(exc_info.value)


def test_orphan_detonation_with_launcher_override_is_accepted():
    out = parse_detonate_schedule("9@5:3:57099", site_id=1, app_id=1, fire_by_event={})
    assert out == [{"event": 9, "t": 5.0, "result": 3, "launcher": (1, 1, 57099)}]


@pytest.mark.parametrize("bad", [
    "1",            # missing @T:R
    "1@abc:3",       # non-numeric T
])
def test_detonate_schedule_bad_entry_exits_non_zero_naming_it(bad):
    with pytest.raises(SystemExit) as exc_info:
        parse_detonate_schedule(bad, site_id=1, app_id=1, fire_by_event={})
    assert repr(bad) in str(exc_info.value)


# ---------------------------------------------------------------------------
# parse_munition_type
# ---------------------------------------------------------------------------

def test_munition_type_parses_default_tuple():
    assert parse_munition_type("2.9.225.2.1.1.0") == (2, 9, 225, 2, 1, 1, 0)


def test_munition_type_wrong_field_count_exits_non_zero():
    with pytest.raises(SystemExit):
        parse_munition_type("2.9.225")
