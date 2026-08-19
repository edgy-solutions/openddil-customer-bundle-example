"""
dis-sim's damage-emission control (Item 1, 2026-08-19).

The generator hardcoded `entityAppearance = 0` until today, and a live
measurement found the field zero in 3000 records across 8 entities. That is
why these tests exist: a consumer decoding bits 3-4 of zero reads damage
NONE, which maps to HEALTH_STATE_NOMINAL -- a POSITIVE claim of health
manufactured out of a field nobody set.

The control makes the claim deliberate. These tests pin the two properties
that make it honest: the layout is domain-aware, and silence stays silent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import DAMAGE_LEVELS, Entity, appearance_bits  # noqa: E402

_POWERPLANT_ON = 1 << 21


def test_damage_occupies_bits_3_and_4():
    for name, level in DAMAGE_LEVELS.items():
        bits = appearance_bits(domain=1, damage=name)
        assert (bits >> 3) & 0x3 == level, name


def test_firepower_kill_is_land_only():
    """Bit 2 is firepower-kill for LAND and is reused elsewhere.

    A generator that ignores domain would assert firepower kills on
    aircraft -- a defect that renders plausibly and is found late. Refusing
    is better than encoding: it fails at the author rather than at a reader
    who has no way to know the bit meant something else.
    """
    assert appearance_bits(domain=1, damage="none", firepower_kill=True) & (1 << 2)
    with pytest.raises(ValueError, match="LAND-domain"):
        appearance_bits(domain=2, damage="none", firepower_kill=True)


def test_mobility_kill_is_shared_across_domains():
    """Bit 1 is mobility-kill (land) / propulsion-kill (air) -- same bit,
    domain-specific name, so both are legal."""
    for domain in (1, 2):
        assert appearance_bits(domain=domain, damage="none", mobility_kill=True) & (1 << 1)


def test_a_populated_field_is_never_zero():
    """The sentinel that lets a consumer tell 'undamaged' from 'unsaid'.

    An entity making a claim sets the power-plant bit, so even the most
    benign claim -- powered on, no damage -- is non-zero. An all-zero field
    therefore means the producer said NOTHING, which is what the live
    measurement found.

    The edge case is named rather than hidden: a powered-OFF, undamaged
    entity also encodes to zero, so zero is ambiguous in exactly one
    combination. A consumer must not treat zero as 'no damage' on the
    strength of this sentinel alone.
    """
    assert appearance_bits(domain=1, damage="none") == _POWERPLANT_ON
    assert appearance_bits(domain=1, damage="none") != 0
    assert appearance_bits(domain=1, damage="none", powerplant_on=False) == 0


def test_entities_make_no_claim_by_default():
    """Default behaviour is unchanged, and that is the point.

    A generator that always emits 'undamaged' asserts something it has no
    basis for. Absence stays the default; the operator opts in.
    """
    import random
    e = Entity(0, site_id=1, app_id=1, rng=random.Random(1))
    assert e.emit_appearance is False
    assert e.damage == "none"
