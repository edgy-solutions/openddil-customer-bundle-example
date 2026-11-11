#!/usr/bin/env python3
"""Write kindcheck's fixtures from this repository's public egress twin.

    make_fixtures.py [--values FILE] [--out DIR]

 - assembler-config.json   `egress.assembler.config` from k8s/egress-values.yaml, as is.
 - kind-schemas/<Kind>.schema.json   the kinds that can be assembled: those that declare
   `x-openddil.episode` and `owning_tier`. A kind the intake takes in (it has no episode) is
   not assembled here, so kindcheck would report it unchecked.
 - cm-state.json   one open discrepancy episode, on a fictional asset at a fictional edge.
 - asset.json      the readiness and rollup rows the assembler would read for that asset.

Everything but the first two is synthetic and fictional. tests/test_kindcheck.py asserts the
committed fixtures equal this script's output, so they cannot drift from the values.
Stdlib plus pyyaml.
"""

from __future__ import annotations

import argparse
import json
import pathlib

import yaml

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_VALUES = HERE.parents[1] / "k8s" / "egress-values.yaml"

CM_STATE = {
    "asset_id": "ATL_NORTH_SENTRY1_R",
    "edge_id": "edge-north",
    "originator_nation": "ATL",
    "releasable_to": ["ATL"],
    "lifecycle": 2,
    "as_of_ns": 1793332400000000000,
    "installed": [{"slot_id": "cooling_fan", "ci_id": "", "installed_at_ns": 1700000000000000000}],
    "manual_discrepancies": [{
        "component": "cooling_fan",
        "fault_code": "SAMPLE-FAN-001",
        "detected_at_ns": 1793332388000000000,
        "sources": [{
            "source": "manual",
            "reported_by": "sample-operator",
            "event_id": "sample-report-1",
            "reported_at_ns": 1793332388000000000,
        }],
    }],
}

ASSET = {
    "readiness": {"operational_status": "DEGRADED", "reporting_status": "REPORTING"},
    "readiness_observed_at": "2026-11-07T11:59:00Z",
    "rollup": {"overall_severity": "WARNING"},
    "rollup_observed_at": "2026-11-07T11:58:00Z",
    "factors": [],
}


def build(values_path: pathlib.Path) -> dict[str, object]:
    egress = yaml.safe_load(values_path.read_text(encoding="utf-8"))["egress"]
    files: dict[str, object] = {"assembler-config.json": egress["assembler"]["config"]}
    for kind, schema in egress["kinds"].items():
        block = schema.get("x-openddil", {})
        if block.get("episode") and block.get("owning_tier"):
            files[f"kind-schemas/{kind}.schema.json"] = schema
    files["cm-state.json"] = CM_STATE
    files["asset.json"] = ASSET
    return files


def render(doc: object) -> str:
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--values", default=str(DEFAULT_VALUES))
    ap.add_argument("--out", default=str(HERE / "fixtures"))
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)
    for name, doc in build(pathlib.Path(args.values)).items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(doc), encoding="utf-8", newline="\n")
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
