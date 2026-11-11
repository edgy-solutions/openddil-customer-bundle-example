"""The sample egress values must render and must load in the service that reads them.

The chart passes egress config through as opaque JSON, so a clean render says
nothing about whether the service would start. These tests take the config out
of the rendered ConfigMaps and hand it to the egress service's own loaders.

A checkout-free run still gets the shape tests. The render and loader tests skip
when `helm` or the sibling checkouts are unavailable. Set OPENDDIL_REPOS to the
directory holding them when this bundle is checked out somewhere else.

The test reads the overlay it checks from two environment variables:

  OVERLAY_ROOT    the overlay's root directory. Default: this repository.
  OVERLAY_VALUES  a colon-separated values chain, relative to the root, in helm's -f order.
                  Default: k8s/tier-node-values.yaml:k8s/egress-values.yaml

The kinds, routes, assembler, intake and forwarder names it expects come from the merged
chain, and the nations from OVERLAY_ROOT/dimensions.yaml when that file exists. The
workload list is the chart's.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

import pytest
import yaml

CONFORMANCE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE))
sys.path.insert(0, str(CONFORMANCE / "overlay"))
from common import deep_merge  # noqa: E402
from siblings import BUNDLE_ROOT, sibling  # noqa: E402

OVERLAY_ROOT = pathlib.Path(os.environ.get("OVERLAY_ROOT") or BUNDLE_ROOT)
OVERLAY_VALUES = (os.environ.get("OVERLAY_VALUES") or "k8s/tier-node-values.yaml:k8s/egress-values.yaml").split(":")
VALUES_CHAIN = [OVERLAY_ROOT / rel for rel in OVERLAY_VALUES]
DEFAULT_NATIONS = {"ATL", "BDR", "SYL"}
EGRESS_WORKLOADS = ["egress-gate-c2", "egress-assembler", "egress-intake", "egress-forwarder", "egress-stub-sink"]

CHART = sibling("openddil-helm", "openddil-demo")
EGRESS_SRC = sibling("openddil-demo", "egress")

needs_render = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.is_dir(),
    reason="helm and a sibling chart checkout are required",
)
needs_service = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.is_dir() or not EGRESS_SRC.is_dir(),
    reason="helm and sibling chart and openddil-demo checkouts are required",
)


def _nations() -> set[str]:
    path = OVERLAY_ROOT / "dimensions.yaml"
    if not path.is_file():
        return DEFAULT_NATIONS
    for dim in yaml.safe_load(path.read_text(encoding="utf-8"))["dimensions"]:
        if dim["name"] == "nation":
            return set(dim["vocabulary"])
    raise AssertionError("dimensions.yaml declares no nation dimension")


@pytest.fixture(scope="module")
def values():
    merged: dict = {}
    for path in VALUES_CHAIN:
        merged = deep_merge(merged, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return merged


@pytest.fixture(scope="module")
def rendered():
    proc = subprocess.run(
        ["helm", "template", "openddil", str(CHART), "-n", "openddil",
         *[arg for path in VALUES_CHAIN for arg in ("-f", str(path))]],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr[:2000]}"
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


@pytest.fixture(scope="module")
def egress_modules():
    sys.path.insert(0, str(EGRESS_SRC))
    try:
        import assembler, forwarder, intake, kinds, routes  # noqa: E401
    except ImportError as exc:
        pytest.skip(f"the egress service's dependencies are not installed: {exc}")
    finally:
        sys.path.remove(str(EGRESS_SRC))
    return {"assembler": assembler, "forwarder": forwarder, "intake": intake, "kinds": kinds, "routes": routes}


def _config_map(docs: list[dict], suffix: str) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == f"openddil-{suffix}":
            return doc["data"]
    raise AssertionError(f"no ConfigMap openddil-{suffix} in the render")


def _urls(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("url", "token_url") and isinstance(value, str):
                yield value
            else:
                yield from _urls(value)
    elif isinstance(node, list):
        for value in node:
            yield from _urls(value)


def test_nations_are_the_declared_nations(values):
    nations = _nations()
    for name, entry in values["releasability"]["destinations"]["entries"].items():
        assert set(entry["nations"]) <= nations, name


def test_every_host_is_reserved_for_examples(values):
    urls = list(_urls(values["egress"]))
    assert urls, "expected the forwarder and intake to name URLs"
    for url in urls:
        assert urlsplit(url).hostname.endswith(".invalid"), url


def test_every_route_goes_to_a_destination_that_accepts_its_kind(values):
    entries = values["releasability"]["destinations"]["entries"]
    for route in values["egress"]["routes"]:
        assert route["destination"] in entries, route["name"]
        assert route["kind"] in entries[route["destination"]]["accepts"], route["name"]


def test_every_accepted_kind_is_declared(values):
    declared = set(values["egress"]["kinds"])
    for name, entry in values["releasability"]["destinations"]["entries"].items():
        assert set(entry["accepts"]) <= declared, name


def test_every_egress_topic_is_created_on_the_hub(values):
    egress = values["egress"]
    created = {spec.split("|", 1)[0] for spec in egress["hubTopics"]}
    used = {t for r in egress["routes"] for t in (r["source_topic"], r["sink_topic"])}
    used |= {e["output_topic"] for e in egress["assembler"]["config"]}
    used |= {e["onward_topic"] for e in egress["intake"]["config"]}
    assert used <= created, sorted(used - created)


@needs_render
def test_every_egress_workload_renders(rendered):
    names = {d["metadata"]["name"] for d in rendered if d.get("kind") == "Deployment"}
    missing = [w for w in EGRESS_WORKLOADS if f"openddil-{w}" not in names]
    assert not missing, f"not rendered: {missing}"


@needs_service
def test_the_rendered_config_loads_in_the_service(rendered, values, egress_modules, tmp_path):
    kinds_dir = tmp_path / "kinds"
    kinds_dir.mkdir()
    for key, body in _config_map(rendered, "egress-kinds").items():
        (kinds_dir / key).write_text(body, encoding="utf-8")
    declarations = egress_modules["kinds"].load_declarations(kinds_dir)
    egress = values["egress"]
    assert set(declarations) == set(egress["kinds"])

    def written(name: str, suffix: str, key: str = "config.json") -> pathlib.Path:
        path = tmp_path / name
        path.write_text(_config_map(rendered, suffix)[key], encoding="utf-8")
        return path

    routes = egress_modules["routes"].load_routes(written("routes.json", "egress-routes", "routes.json"), declarations)
    assert {r["name"] for r in egress["routes"]} <= {r.name for r in routes}

    assemblable = {k for k, d in declarations.items() if d.owning_tier and d.episode}
    assembled = egress_modules["assembler"].load_assembler_config(
        written("assembler.json", "egress-assembler-config"), assemblable)
    assert [e.name for e in assembled] == [e["name"] for e in egress["assembler"]["config"]]

    taken_in = egress_modules["intake"].load_intake_config(written("intake.json", "egress-intake-config"), declarations)
    assert [e.name for e in taken_in] == [e["name"] for e in egress["intake"]["config"]]

    forwarded = egress_modules["forwarder"].load_forward_config(written("forwarder.json", "egress-forwarder-config"))
    assert {e.name for e in forwarded} == {e["name"] for e in egress["forwarder"]["config"]}


@needs_service
def test_the_stand_in_artifact_is_a_valid_intake_kind(rendered, values, egress_modules, tmp_path):
    for key, body in _config_map(rendered, "egress-kinds").items():
        (tmp_path / key).write_text(body, encoding="utf-8")
    validators = egress_modules["kinds"].load_kinds(tmp_path)
    intake_kinds = {e["kind"] for e in values["egress"]["intake"]["config"]}
    assert intake_kinds, "expected the intake to name a kind"
    artifacts = json.loads(_config_map(rendered, "egress-stub-artifacts")["artifacts.json"])
    assert artifacts, "expected the stand-in to serve an artifact"
    for artifact in artifacts:
        assert any(validators[kind](artifact) is None for kind in intake_kinds), artifact
