"""
dis-sim's `--munition-track`: a fired munition that is a tracked entity in
flight (id 5000 + event) on the Fire, the kind-2 Entity State samples, and
the Detonation. Pure-function tests, NO SOCKET; PDUs are serialized and
decoded back with opendis.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from opendis.PduFactory import createPdu  # noqa: E402

from dis_sim import (  # noqa: E402
    detonation_pdu,
    fire_pdu,
    munition_track_pdu,
    munition_track_sample_times,
    parse_detonate_schedule,
    parse_fire_schedule,
    plan_munition_tracks,
    serialize,
)

MUNITION = (2, 9, 225, 2, 1, 1, 0)
SITE, APP = 1, 1


def _plan(fire, det="", rate=2.0, flight=20.0, configured=()):
    fs = parse_fire_schedule(fire, SITE, APP)
    ds = parse_detonate_schedule(det, SITE, APP, {e["event"]: e for e in fs})
    return fs, ds, plan_munition_tracks(fs, ds, SITE, APP, rate, flight, set(configured))


def test_fire_carries_munition_id_with_flag_and_zero_without():
    fs, _, tracks = _plan("dis:1:1:1001@5:7:1")
    on = createPdu(serialize(fire_pdu(fs[0], MUNITION, SITE, APP, 1, 7,
                                      munition_id=tracks[7]["id"])))
    off = createPdu(serialize(fire_pdu(fs[0], MUNITION, SITE, APP, 1, 7)))
    assert (on.munitionExpendableID.siteID, on.munitionExpendableID.applicationID,
            on.munitionExpendableID.entityID) == (1, 1, 5007)
    assert (off.munitionExpendableID.siteID, off.munitionExpendableID.applicationID,
            off.munitionExpendableID.entityID) == (0, 0, 0)


def test_flag_off_fire_bytes_equal_explicit_none():
    fs, _, _ = _plan("dis:1:1:1001@5:7:1")
    assert serialize(fire_pdu(fs[0], MUNITION, SITE, APP, 1, 7)) == \
        serialize(fire_pdu(fs[0], MUNITION, SITE, APP, 1, 7, munition_id=None))


def test_detonation_carries_exploding_entity_id():
    fs, ds, tracks = _plan("dis:1:1:1001@5:7:1", det="7@15:1")
    d = createPdu(serialize(detonation_pdu(ds[0], MUNITION, SITE, APP, 1, 7,
                                           munition_id=tracks[7]["id"])))
    assert (d.explodingEntityID.siteID, d.explodingEntityID.applicationID,
            d.explodingEntityID.entityID) == (1, 1, 5007)
    plain = createPdu(serialize(detonation_pdu(ds[0], MUNITION, SITE, APP, 1, 7)))
    assert plain.explodingEntityID.entityID == 0


def test_flight_without_detonation_samples_count_and_stop():
    _, _, tracks = _plan("dis:1:1:1001@5:7:1", rate=2.0, flight=10.0)
    times = munition_track_sample_times(tracks[7])
    assert abs(len(times) - 20) <= 1
    assert times[0] == 5.0 and times[-1] < 15.0
    assert tracks[7]["reason"] == "flight time"


def test_samples_stop_at_detonation_time():
    _, _, tracks = _plan("dis:1:1:1001@5:7:1", det="7@12:1", flight=100.0)
    times = munition_track_sample_times(tracks[7])
    assert times[0] == 5.0 and max(times) < 12.0 and len(times) == 14
    assert tracks[7]["reason"] == "detonation"


def test_sample_pdu_fields_and_consistent_velocity():
    _, _, tracks = _plan("dis:1:1:1001@5:7:1")
    origin = (39.0, -105.0, 1600.0)
    p0 = createPdu(serialize(munition_track_pdu(tracks[7], origin, 0.0, MUNITION, 1, 1, 7)))
    p1 = createPdu(serialize(munition_track_pdu(tracks[7], origin, 2.0, MUNITION, 1, 1, 7)))
    assert p0.pduType == 1
    assert (p0.entityID.siteID, p0.entityID.applicationID, p0.entityID.entityID) == (1, 1, 5007)
    assert (p0.entityType.entityKind, p0.entityType.domain, p0.entityType.country,
            p0.entityType.category) == (2, 9, 225, 2)
    assert p0.forceId == 1
    assert bytes(p0.marking.characters).rstrip(b"\x00") == b"MSL-7"
    disp = [getattr(p1.entityLocation, a) - getattr(p0.entityLocation, a) for a in "xyz"]
    vel = [getattr(p0.entityLinearVelocity, a) * 2.0 for a in "xyz"]
    # velocity is a 0.5 s finite difference of a ~linear path: agrees to a few metres over 2 s
    assert all(abs(d - v) < 5.0 for d, v in zip(disp, vel))
    assert sum(d * d for d in disp) ** 0.5 > 400.0


def test_id_collision_with_configured_entity_is_fatal_and_named():
    with pytest.raises(SystemExit) as e:
        _plan("dis:1:1:1001@5:7:1", configured=[(1, 1, 5007)])
    assert "dis:1:1:5007" in str(e.value) and "collides" in str(e.value)


@pytest.mark.parametrize("det", ["7@5:1", "7@3:1"])
def test_detonation_not_after_fire_is_fatal(det):
    with pytest.raises(SystemExit) as e:
        _plan("dis:1:1:1001@5:7:1", det=det)
    assert "event 7" in str(e.value)
