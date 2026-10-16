"""
dis-sim placement area and leash. Pure-function tests, NO SOCKET.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

import dis_sim  # noqa: E402
from dis_sim import BASE_LAT_DEG, BASE_LON_DEG, SPREAD_DEG, Entity  # noqa: E402

# Captured from HEAD (before the placement change), Random(1337), site/app 1/1.
BASELINE = [
    (39.058876428475735, -104.9833672131975, 1673.1696718498752, 3.680610495170361, 3.6568728368878083),
    (39.16218687345381, -105.0581475956929, 1757.9225649831374, 5.791378464218098, 5.076333879795024),
    (39.24612353629291, -105.14752699896121, 1731.2555266377778, 5.732183526103962, 3.092883976212714),
]
BASE = (10.0, 20.0)


def _vals(e):
    return (e.lat, e.lon, e.alt, e.heading, e.speed_mps)


def _walk(leash, speed, spread=0.05, steps=20_000, dt=5.0, jitter=True, posture=False):
    random.seed(7)
    e = Entity(0, 1, 1, random.Random(3), base_lat=BASE[0], base_lon=BASE[1],
               spread_deg=spread, leash_deg=leash)
    e.speed_mps = speed
    if posture:
        e.has_posture_schedule = True
        e.heading = 0.7
    if not jitter:
        e.heading = 0.7
    max_d = 0.0
    for _ in range(steps):
        e.step(dt)
        max_d = max(max_d, abs(e.lat - BASE[0]), abs(e.lon - BASE[1]))
    return max_d


def test_defaults_unchanged_literal_baseline():
    r = random.Random(1337)
    for i, want in enumerate(BASELINE):
        assert _vals(Entity(i, 1, 1, r)) == pytest.approx(want, rel=0, abs=0)


def test_explicit_constants_equal_defaults():
    a = Entity(0, 1, 1, random.Random(5))
    b = Entity(0, 1, 1, random.Random(5), base_lat=BASE_LAT_DEG, base_lon=BASE_LON_DEG,
               spread_deg=SPREAD_DEG, leash_deg=0.0)
    assert _vals(a) == _vals(b)


def test_placement_area():
    r = random.Random(11)
    for i in range(50):
        e = Entity(i % 8, 1, 1, r, base_lat=10.0, base_lon=20.0, spread_deg=0.1)
        assert abs(e.lat - 10.0) <= 0.1 and abs(e.lon - 20.0) <= 0.1


@pytest.mark.parametrize("speed", [12.0, 120.0])
def test_leash_holds(speed):
    leash = 0.1
    one_step_deg = speed * 5.0 / 111_320.0 * 1.1
    assert _walk(leash, speed) <= leash + one_step_deg


@pytest.mark.parametrize("speed", [12.0, 120.0])
def test_no_leash_is_unbounded(speed):
    # Fixed heading, leash off: must leave the 0.1 box (proves the leash test can fail).
    assert _walk(0.0, speed, jitter=False, steps=2000) > 0.1 + 0.01


def test_posture_scheduled_entity_not_leashed():
    assert _walk(0.1, 12.0, posture=True, steps=2000) > 0.1 + 0.01


@pytest.mark.parametrize("spread,leash", [(-0.1, 0.0), (0.1, -1.0), (0.2, 0.1)])
def test_bad_placement_refused(spread, leash, monkeypatch):
    assert dis_sim.placement_error(spread, leash)
    monkeypatch.setattr(sys, "argv", ["dis_sim.py", "--spread-deg", str(spread),
                                      "--leash-deg", str(leash)])
    with pytest.raises(SystemExit) as ei:
        dis_sim.main()
    assert ei.value.code == 2


def test_good_placement_accepted():
    assert dis_sim.placement_error(0.1, 0.0) is None
    assert dis_sim.placement_error(0.1, 0.1) is None
    assert dis_sim.placement_error(0.0, 0.0) is None
