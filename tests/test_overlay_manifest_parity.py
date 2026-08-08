"""Parity check — the sample overlay must contain what the manifest declares.

    sample overlay  ⊇  overlay-manifest.yaml

This enforces the PRINCIPLES rule "Public twin, declared by manifest". It is
deliberately expressible WITHOUT reference to any private material, so it runs
anywhere — including CI, where private overlays do not exist and must not be
named.

Why enforce at all: an unenforced manifest asserts the overlay's contents
without checking them, which is the same claims-vs-sources failure the
manifest exists to prevent, one layer up. Declared-and-unchecked drifts.

What this does NOT check: content. This bundle's content is invented by
design, so content parity with anything is neither possible nor desired. The
check is structural only.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = BUNDLE_ROOT / "overlay-manifest.yaml"

VALID_STATUSES = {"present", "MISSING", "PROSPECTIVE"}


def _manifest() -> dict:
    with MANIFEST.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _classes() -> list[dict]:
    return _manifest()["classes"]


def test_manifest_exists_and_parses():
    assert MANIFEST.is_file(), f"missing manifest: {MANIFEST}"
    doc = _manifest()
    assert doc.get("version") == 1
    assert doc.get("classes"), "manifest declares no classes"


@pytest.mark.parametrize("cls", _classes(), ids=lambda c: c["id"])
def test_class_is_well_formed(cls):
    """Every declared class carries the fields the check depends on."""
    for field in ("id", "purpose", "path", "required", "status"):
        assert field in cls, f"class {cls.get('id')!r} missing field {field!r}"
    assert cls["status"] in VALID_STATUSES, (
        f"class {cls['id']!r} has status {cls['status']!r}; "
        f"expected one of {sorted(VALID_STATUSES)}"
    )


@pytest.mark.parametrize(
    "cls", [c for c in _classes() if c["status"] == "present"], ids=lambda c: c["id"]
)
def test_present_classes_actually_exist(cls):
    """THE PARITY CHECK.

    A class marked `present` must exist on disk. This is the assertion that
    keeps the manifest honest: claiming a class is present without shipping it
    is exactly the drift the manifest was written to prevent.
    """
    target = BUNDLE_ROOT / cls["path"]
    assert target.exists(), (
        f"class {cls['id']!r} is declared status=present at {cls['path']!r}, "
        f"but nothing exists there. Either ship the artifact or change the "
        f"status to MISSING — do not leave the manifest asserting something "
        f"the bundle does not contain."
    )
    if cls["path"].endswith("/"):
        assert target.is_dir(), f"{cls['path']!r} should be a directory"
        assert any(target.iterdir()), (
            f"class {cls['id']!r} is declared present but {cls['path']!r} is "
            f"an empty directory — an empty twin is not a twin."
        )


@pytest.mark.parametrize(
    "cls", [c for c in _classes() if c["status"] == "MISSING"], ids=lambda c: c["id"]
)
def test_missing_classes_are_genuinely_absent(cls):
    """The inverse, and it matters as much.

    A class marked MISSING that has quietly been populated means the manifest
    is understating the bundle — the status should be promoted to `present`
    so the parity check starts guarding it. Silent over-delivery still leaves
    an artifact unguarded.
    """
    target = BUNDLE_ROOT / cls["path"]
    populated = target.is_dir() and any(target.iterdir()) if target.is_dir() else target.exists()
    assert not populated, (
        f"class {cls['id']!r} is declared status=MISSING but {cls['path']!r} "
        f"now has content. Promote it to status=present so the parity check "
        f"guards it."
    )


@pytest.mark.parametrize(
    "cls",
    [c for c in _classes() if c["status"] == "MISSING"],
    ids=lambda c: c["id"],
)
def test_missing_classes_declare_a_travel_group(cls):
    """Every gap must say when it closes.

    A MISSING class with no `travels_with` is a gap nobody has scheduled,
    which is how backlog items become permanent.
    """
    assert cls.get("travels_with"), (
        f"class {cls['id']!r} is MISSING but declares no travels_with — "
        f"name the work it rides with, or it will not close."
    )
