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
