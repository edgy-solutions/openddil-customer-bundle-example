"""Acceptance: the tier-node template renders against THIS overlay.

The three overlay classes added for Arc 1 Phase 3 (`cm-baselines`,
`deployment-manifests`, `chart-overlay`) are **test fixtures for the tier
presentation node**, not parallel documentation. Their acceptance is
behavioural — "the chart renders against them" — which keeps the fictional
content honest by construction: a fixture that does not work is a fixture
that fails here.

This also makes the parity check and the template validation the same motion:
the manifest says the class exists, and this says the class functions.

Skips cleanly when `helm` is unavailable or the chart is not checked out
beside this bundle, so the suite stays runnable in isolation.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest
import yaml

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[1]
VALUES = BUNDLE_ROOT / "k8s" / "tier-node-values.yaml"
CHART = BUNDLE_ROOT.parent / "openddil" / "openddil-helm" / "openddil-demo"
if not CHART.is_dir():  # sibling-checkout layout
    CHART = BUNDLE_ROOT.parent / "openddil-helm" / "openddil-demo"

# The template's own header states these components. Auditing rendered object
# names against this list is the enforcement for PRINCIPLES §"A deliverable's
# self-description is a claim" — the rule earned when a chart header listed
# fusion + cm-service and the render silently omitted both.
EXPECTED_COMPONENTS = [
    "tier-pg",
    "tier-schema-init",
    "tier-projector-config",
    "tier-projector",
    "tier-restate-config",
    "tier-restate",
    "tier-restate-bootstrap",
    "tier-fusion",
    "tier-cm",
    "tier-electric",
    "tier-frontend",
    "tier-topaz",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.is_dir(),
    reason="helm and a sibling chart checkout are required",
)


def _render() -> str:
    proc = subprocess.run(
        ["helm", "template", str(CHART), "-f", str(VALUES)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr[:2000]}"
    return proc.stdout


@pytest.fixture(scope="module")
def rendered() -> str:
    return _render()


def test_values_fixture_exists_and_parses():
    assert VALUES.is_file(), f"missing deployment-manifests fixture: {VALUES}"
    doc = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    assert doc["tierNode"]["enabled"] is True, (
        "the fixture must ENABLE the tier node — a fixture that leaves the "
        "unit under test switched off validates nothing"
    )


def test_baseline_fixture_matches_the_mapping_variant():
    """The fixture chain must actually close.

    schema -> mapping -> platform_variant -> baseline. If the baseline's
    platform_variant does not match what the sample mapping aliases to, a
    tier's cm-service will observe assets it has no baseline for and produce
    the empty gesture this fixture exists to prevent.
    """
    baselines = list((BUNDLE_ROOT / "baselines").glob("*.yaml"))
    assert baselines, "cm-baselines class is empty"

    variants = {
        yaml.safe_load(b.read_text(encoding="utf-8"))["platform_variant"]
        for b in baselines
    }
    mapping = (BUNDLE_ROOT / "dynamic-mappings" / "sample-sensor-mapping.yaml").read_text(
        encoding="utf-8"
    )
    assert any(v in mapping for v in variants), (
        f"no baseline platform_variant {sorted(variants)} appears in the "
        f"sample mapping's alias table — the fixture chain is broken and a "
        f"tier's cm-service would have nothing to initialise against"
    )


def test_tier_node_renders_against_this_overlay(rendered):
    assert "tier-pg-edge-northpoint" in rendered, (
        "the chart did not render tier-node objects for the fixture's tiers"
    )


@pytest.mark.parametrize("component", EXPECTED_COMPONENTS)
def test_every_declared_component_actually_renders(rendered, component):
    """Rendered-object audit — the enforcement, not a formality.

    `helm template` exiting zero proves the render RAN. It does not prove the
    render produced what the template's header claims. This asserts each
    named component appears in the output by name.
    """
    assert f"{component}-edge-northpoint" in rendered, (
        f"component {component!r} is named in the tier-node template's header "
        f"but does not appear in the rendered output. Exit-zero proved the "
        f"render ran, not that it rendered what the header promises."
    )


def test_tier_scoped_projector_config_omits_root_only_topics(rendered):
    """The third parameterization axis, asserted rather than assumed.

    A tier subscribes to topics its own broker carries. Root-only rollups
    must NOT appear in a tier's projector config — their presence is what
    produced the UNKNOWN_TOPIC_OR_PART noise the gate observed.

    Asserted against the SUBSCRIBED TOPIC LIST, not against raw text. The
    first version of this test substring-matched the rendered window and
    failed on the ConfigMap's own comment explaining that those topics are
    deliberately absent — a test that fails for the wrong reason would
    equally have passed for the wrong reason.
    """
    start = rendered.index("tier-projector-config-edge-northpoint")
    window = rendered[start:start + 6000]
    subscribed = {
        line.split("- topic:", 1)[1].strip()
        for line in window.splitlines()
        if line.strip().startswith("- topic:")
    }
    assert subscribed, "tier projector config declares no topics at all"

    for rollup in ("region-fleet-summary", "region-top-factors", "region-wear-trends"):
        assert rollup not in subscribed, (
            f"root-only rollup topic {rollup!r} is SUBSCRIBED in a tier "
            f"projector config; a tier's broker does not carry it"
        )

    # And the tier does subscribe to what it genuinely carries.
    assert "telemetry-latest-state" in subscribed
    assert "asset-logistics-status" in subscribed, (
        "a tier must project its own severity output — that is the whole "
        "point of running fusion at the tier"
    )


def test_restate_partitions_pinned_at_the_measured_tier_profile(rendered):
    """Provision-time-only value — pinned, not inherited.

    Partition count cannot be changed after a node first boots, so the
    fixture must set it explicitly. 6 is the measured tier profile
    (~167 MiB RSS idle vs ~389 MiB at the product default of 24).
    """
    assert "--default-num-partitions" in rendered
    assert '"6"' in rendered or "'6'" in rendered, (
        "tier Restate partition count is not pinned to the measured profile"
    )


# ---------------------------------------------------------------------------
# Structural audit — PARSED, not substring-matched.
#
# `test_every_declared_component_actually_renders` above asserts each component
# NAME appears in the rendered text. That is necessary and insufficient, and
# the gap is not theoretical: a missing `---` at the tier loop boundary
# concatenated tier N's last object with tier N+1's first into a single YAML
# document. Duplicate top-level keys resolve last-wins, so the first object was
# silently discarded — while its text remained fully present in the render.
# Every substring assertion still passed. Observed as 3 topaz Services against
# 1 topaz Deployment; the two tiers that lost it got a Service with no
# endpoints, i.e. no local authorizer.
#
# The lesson generalises past this one bug: assertions about what a cluster
# will RECEIVE must be made against parsed objects, because the unit the
# cluster consumes is the document, not the byte range.
# ---------------------------------------------------------------------------

# Each component and the kind it must produce, once per tier. Exact names are
# built rather than substring-matched because `tier-restate` is a prefix of
# both `tier-restate-config` and `tier-restate-bootstrap`.
POD_BEARING = {
    "tier-pg": "StatefulSet",
    "tier-restate": "StatefulSet",
    "tier-projector": "Deployment",
    "tier-fusion": "Deployment",
    "tier-cm": "Deployment",
    "tier-electric": "Deployment",
    "tier-frontend": "Deployment",
    "tier-topaz": "Deployment",
    "tier-schema-init": "Job",
    "tier-restate-bootstrap": "Job",
}

# `helm template CHART` with no explicit release name uses this default.
RELEASE = "release-name"


def _tier_ids() -> list[str]:
    """Tiers the fixture should produce a node for.

    Mirrors the template's own gate: range over `edges`, filtered by
    `tierNode.tiers` when that list is non-empty.
    """
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8")) or {}
    edges = [e["id"] for e in (values.get("edges") or []) if e.get("id")]
    selected = (values.get("tierNode") or {}).get("tiers") or []
    return [e for e in edges if e in selected] if selected else edges


@pytest.fixture(scope="module")
def parsed(rendered) -> list[dict]:
    docs = [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]
    assert docs, "render parsed to zero documents"
    return docs


@pytest.mark.parametrize("component,kind", sorted(POD_BEARING.items()))
def test_every_tier_gets_its_own_object(parsed, component, kind):
    """One object of the right kind per tier — counted, not grepped."""
    tiers = _tier_ids()
    assert tiers, "fixture defines no tiers; the rest of this test is vacuous"

    by_name = {
        d.get("metadata", {}).get("name"): d.get("kind")
        for d in parsed
        if d.get("metadata", {}).get("name")
    }
    missing = [
        f"{RELEASE}-{component}-{t}"
        for t in tiers
        if by_name.get(f"{RELEASE}-{component}-{t}") != kind
    ]
    assert not missing, (
        f"{component}: expected a {kind} for each of {len(tiers)} tiers, but "
        f"these are absent or of the wrong kind after parsing: {missing}. "
        f"If the name appears in `helm template` output but fails here, the "
        f"object was merged into a neighbouring YAML document and discarded — "
        f"check for a missing `---` separator."
    )


def test_no_two_objects_share_a_yaml_document(rendered):
    """Each object gets its own document.

    Direct guard on the separator bug, independent of any component list, so a
    component added later is covered without touching this file.
    """
    offenders: list[str] = []
    seen_in_doc = None
    for lineno, line in enumerate(rendered.splitlines(), start=1):
        if line.strip() == "---":
            seen_in_doc = None
        elif line.startswith("apiVersion:"):
            if seen_in_doc is not None:
                offenders.append(f"line {lineno} (previous object at line {seen_in_doc})")
            seen_in_doc = lineno

    assert not offenders, (
        "two objects share one YAML document, so the first is silently "
        "discarded by last-wins key resolution: " + "; ".join(offenders)
    )
