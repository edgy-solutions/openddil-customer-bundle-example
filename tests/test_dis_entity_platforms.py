"""dis-sim pins each entity id to one platform.

Asset ids key every downstream store, so an id's platform must not depend on
where a type sits in the type list. These tests hold the lab map to the
platforms the two lab edges already report, and check that the list's order
has no effect.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    DEFAULT_ENTITY_PLATFORMS,
    RECOGNISED_TYPES,
    platform_for,
)

# What the lab edges report today, per asset id (revision 50).
DEPLOYED = {
    "dis:1:1:1000": "M1A1",
    "dis:1:1:1001": "M1A2-SEPv3",
    "dis:1:1:1002": "M2A3-Bradley",
    "dis:1:1:1003": "HMMWV-M1151A1",
    "dis:1:1:1004": "RCV-M",
    "dis:1:1:1005": "AH-64E-V6",
    "dis:1:1:1006": "UH-60M",
    "dis:1:1:1007": "CH-47F-BlockII",
    "dis:2:1:1000": "M1A1",
    "dis:2:1:1001": "M1A2-SEPv3",
    "dis:2:1:1002": "M2A3-Bradley",
    "dis:2:1:1003": "HMMWV-M1151A1",
    "dis:2:1:1004": "RCV-M",
    "dis:2:1:1005": "AH-64E-V6",
}


def test_map_covers_exactly_the_lab_edges():
    assert set(DEFAULT_ENTITY_PLATFORMS) == set(DEPLOYED)


def test_only_the_rcv_m_ids_change_platform():
    changed = {k: (DEPLOYED[k], v) for k, v in DEFAULT_ENTITY_PLATFORMS.items()
               if DEPLOYED[k] != v}
    assert changed == {
        "dis:1:1:1004": ("RCV-M", "AH-64E-V6"),
        "dis:2:1:1004": ("RCV-M", "AH-64E-V6"),
    }


def test_every_pinned_variant_resolves():
    for key in DEFAULT_ENTITY_PLATFORMS:
        _, site, app, entity = key.split(":")
        tup, variant = platform_for(int(site), int(app), int(entity),
                                    types=RECOGNISED_TYPES)
        assert variant == DEFAULT_ENTITY_PLATFORMS[key]
        assert (tup, variant) in RECOGNISED_TYPES


def test_list_order_does_not_relabel():
    reordered = list(reversed(RECOGNISED_TYPES))
    for key in DEFAULT_ENTITY_PLATFORMS:
        _, site, app, entity = key.split(":")
        args = (int(site), int(app), int(entity))
        assert platform_for(*args, types=reordered) == platform_for(*args, types=RECOGNISED_TYPES)


def test_unpinned_id_is_refused():
    with pytest.raises(SystemExit, match="no platform pinned"):
        platform_for(1, 1, 1008, types=RECOGNISED_TYPES)


def test_variant_missing_from_list_is_refused():
    with pytest.raises(SystemExit, match="appears 0 time"):
        platform_for(1, 1, 1000, pins={"dis:1:1:1000": "RCV-M"}, types=RECOGNISED_TYPES)
