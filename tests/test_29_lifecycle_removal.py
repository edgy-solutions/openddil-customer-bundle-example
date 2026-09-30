"""
dis-sim's ADR-0044 slice A lifecycle overrides (2026-09-29).

test_24_dis_appearance.py pins the appearance-bit control (damage,
mobility/firepower kill, the "silence stays silent" default). This file
pins the three values ADR-0044 slice A adds to the SAME --damage-map file:
"deactivated" (an additional appearance claim), "silent" (stop
transmitting, reversible), and "removed" (stop transmitting via one Remove
Entity PDU, NOT reversible -- see dis_sim.LIFECYCLE_OVERRIDES for why).

The last test in this file is the cross-repo self-check the task asked
for: encode a Remove Entity PDU with dis-sim's new to_remove_entity_pdu(),
then decode it with dis_ingestor.py's new decoder. It is the only test
here that reaches into the sibling openddil-sensor-ingest repo, and it
skips (rather than fails) if that repo is not checked out alongside this
one, since the two are published and can be cloned independently.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    DAMAGE_LEVELS,
    LIFECYCLE_OVERRIDES,
    Entity,
    appearance_bits,
    reload_damage_map,
    serialize,
)

_DEACTIVATED_BIT = 1 << 22


def _entity() -> Entity:
    return Entity(0, site_id=1, app_id=1, rng=random.Random(1))


# ---------------------------------------------------------------------------
# "deactivated" -- additional appearance claim, reversible
# ---------------------------------------------------------------------------

def test_deactivated_sets_the_appearance_bit_and_forces_emission():
    e = _entity()
    assert e.apply_damage_override({"dis:1:1:1000": "deactivated"}) is True
    assert e.deactivated is True
    assert e.emit_appearance is True  # otherwise the bit would never be sent
    pdu = e.to_pdu(exercise_id=1, protocol_version=7)
    assert pdu.entityAppearance & _DEACTIVATED_BIT


def test_deactivated_layers_on_top_of_damage_not_instead_of_it():
    """"deactivated" is an ADDITIONAL claim -- it must not erase a damage
    level a fleet-wide --damage profile (or the baseline) already set."""
    e = _entity()
    e._baseline_damage = "destroyed"
    e._baseline_emit = True
    e.damage, e.emit_appearance = "destroyed", True
    e.apply_damage_override({"dis:1:1:1000": "deactivated"})
    assert e.damage == "destroyed"
    bits = e.to_pdu(exercise_id=1, protocol_version=7).entityAppearance
    assert bits & _DEACTIVATED_BIT
    assert (bits >> 3) & 0x3 == DAMAGE_LEVELS["destroyed"]


def test_deactivated_restores_to_baseline_when_entry_is_cleared():
    e = _entity()
    e.apply_damage_override({"dis:1:1:1000": "deactivated"})
    assert e.deactivated is True
    e.apply_damage_override({})  # entry removed
    assert e.deactivated is False


# ---------------------------------------------------------------------------
# "silent" -- stop transmitting, no appearance claim, reversible
# ---------------------------------------------------------------------------

def test_silent_sets_the_flag_without_touching_appearance():
    e = _entity()
    e.damage, e.emit_appearance = "moderate", True
    e.apply_damage_override({"dis:1:1:1000": "silent"})
    assert e.silent is True
    # Not an appearance claim: damage/emit_appearance are untouched.
    assert e.damage == "moderate"
    assert e.emit_appearance is True


def test_silent_resumes_when_entry_is_cleared():
    e = _entity()
    e.apply_damage_override({"dis:1:1:1000": "silent"})
    assert e.silent is True
    e.apply_damage_override({})
    assert e.silent is False


# ---------------------------------------------------------------------------
# "removed" -- terminal: one PDU, then withheld forever, no baseline restore
# ---------------------------------------------------------------------------

def test_removed_sets_the_flag_and_survives_the_entry_being_cleared():
    e = _entity()
    e.apply_damage_override({"dis:1:1:1000": "removed"})
    assert e.removed is True
    # Unlike deactivated/silent, clearing the map entry does NOT bring the
    # entity back -- a Remove Entity PDU has no real-DIS undo here.
    e.apply_damage_override({})
    assert e.removed is True


def test_removed_pdu_sent_flag_is_the_edge_detector_main_uses():
    """main()'s loop sends the one-shot Remove Entity PDU on the
    False -> True edge of e.removed, gated by e._removed_pdu_sent. This
    pins that the flag starts False and that apply_damage_override never
    sets it -- only main() (via to_remove_entity_pdu/sendto) is allowed
    to flip it, so a re-applied "removed" entry cannot cause a second
    send."""
    e = _entity()
    assert e._removed_pdu_sent is False
    e.apply_damage_override({"dis:1:1:1000": "removed"})
    assert e._removed_pdu_sent is False
    e._removed_pdu_sent = True  # what main() does after the one send
    e.apply_damage_override({"dis:1:1:1000": "removed"})  # re-applied next tick
    assert e._removed_pdu_sent is True  # apply_damage_override did not reset it


def test_removed_pdu_fields():
    e = _entity()
    pdu = e.to_remove_entity_pdu(exercise_id=1, protocol_version=7)
    assert pdu.pduType == 12
    assert pdu.protocolFamily == 5
    assert pdu.originatingEntityID.siteID == e.site_id
    assert pdu.originatingEntityID.applicationID == e.app_id
    assert pdu.originatingEntityID.entityID == 0
    assert pdu.receivingEntityID.siteID == e.site_id
    assert pdu.receivingEntityID.applicationID == e.app_id
    assert pdu.receivingEntityID.entityID == e.entity_id


# ---------------------------------------------------------------------------
# reload_damage_map() accepts the three new values rather than rejecting
# them as unknown damage levels
# ---------------------------------------------------------------------------

def test_reload_damage_map_accepts_lifecycle_overrides(tmp_path):
    p = tmp_path / "damage.json"
    p.write_text(
        '{"dis:1:1:1000": "deactivated", "dis:1:1:1001": "silent", '
        '"dis:1:1:1002": "removed"}',
        encoding="utf-8",
    )
    out = reload_damage_map(str(p))
    assert out["dis:1:1:1000"] == "deactivated"
    assert out["dis:1:1:1001"] == "silent"
    assert out["dis:1:1:1002"] == "removed"
    for v in LIFECYCLE_OVERRIDES:
        assert v in DAMAGE_LEVELS or v in LIFECYCLE_OVERRIDES  # sanity


def test_default_behaviour_is_unchanged_by_the_new_values():
    """The byte-for-byte-unchanged requirement: an entity with no
    --damage-map entry at all is unaffected by any of this."""
    e = _entity()
    assert e.deactivated is False
    assert e.silent is False
    assert e.removed is False
    assert e.to_pdu(exercise_id=1, protocol_version=7).entityAppearance == 0


# ---------------------------------------------------------------------------
# Cross-repo self-check: dis-sim encodes, dis_ingestor.py decodes
# ---------------------------------------------------------------------------

_SENSOR_INGEST_ROOT = Path(__file__).resolve().parents[2] / "openddil-sensor-ingest"
_SENSOR_INGEST_AVAILABLE = (_SENSOR_INGEST_ROOT / "dis_ingestor.py").is_file()


@pytest.mark.skipif(
    not _SENSOR_INGEST_AVAILABLE,
    reason=(
        f"openddil-sensor-ingest not found at {_SENSOR_INGEST_ROOT}; this repo "
        "is published separately and can be exercised standalone, so this one "
        "cross-repo self-check is opt-in rather than a hard dependency for the "
        "rest of this file"
    ),
)
def test_dis_sim_remove_entity_pdu_round_trips_through_the_ingestor_decoder():
    sys.path.insert(0, str(_SENSOR_INGEST_ROOT))
    from opendis.PduFactory import createPdu
    from opendis.dis7 import RemoveEntityPdu
    import dis_ingestor

    e = Entity(0, site_id=1, app_id=1, rng=random.Random(1), entity_id=1005)
    e.apply_damage_override({"dis:1:1:1005": "removed"})
    assert e.removed is True

    wire = serialize(e.to_remove_entity_pdu(exercise_id=1, protocol_version=7))
    decoded = createPdu(wire)

    assert isinstance(decoded, RemoveEntityPdu)
    assert dis_ingestor._is_single_entity_removal(decoded.receivingEntityID) is True

    record = dis_ingestor._extract_remove_entity(decoded)
    assert record["pdu_type"] == "remove_entity"
    assert record["entity_id_urn"] == "dis:1:1:1005"
    assert record["dis_entity_id"] == {"site": 1, "application": 1, "entity": 1005}
    assert record["originating_entity_id"] == {"site": 1, "application": 1, "entity": 0}
