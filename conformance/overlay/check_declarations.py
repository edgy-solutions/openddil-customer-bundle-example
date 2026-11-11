#!/usr/bin/env python3
"""Check DIR/declarations/*.yaml|*.json, the site declarations an overlay carries.

    check_declarations.py --root DIR --values FILE [FILE ...]

Each file is one PEP request body for POST /declarations/site, as set out in
openddil-contracts decisions/DESIGN-2026-11-06-site-declaration.md sections 1 to 4:

    kind        site | rule | exemption
    scope       {kind: edge|tier|logistics-site, id, records: [entity|logistics|spares]}
                site and rule only; a rule's records are exactly [entity]
    match       rule only: one of geofence, marking, country
    asset_id    exemption only
    labels      site and rule only: dimension values (see below)
    reason      required, at most 512 bytes
    supersedes  optional

Owner and date (declared_by, declared_at, declared_role) are set by the PEP and are a
refusal in a body. Any other key is unknown and refused. A body is at most 8 KiB.

Labels: a key is a declared dimension's name or its author_value, or releasable_to (the
audience of the nation dimension). Every value must be within that dimension's
vocabulary in DIR/dimensions.yaml. A site or rule states its release, so releasable_to is
required for it, and so is the nation author value when the nation dimension declares one.

A `site` declaration scoped to an edge must name an edge declared by the merged --values
files (the `edges` list; maps merge deep, lists replace, in the order given).

Prints file names, key names and counts, never a value. Exit 0 ok, 1 refused, 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from check_dimensions import load_dimensions, validate as validate_dimensions  # noqa: E402
from common import BadInput, Tally, merged_values, root_dir, run_main  # noqa: E402
import yaml  # noqa: E402

ALLOWED = {"kind", "scope", "match", "asset_id", "labels", "reason", "supersedes"}
SERVER_SET = {"declared_by", "declared_at", "declared_role"}
KINDS = {"site", "rule", "exemption"}
SCOPE_KINDS = {"edge", "tier", "logistics-site"}
RECORDS = {"entity", "logistics", "spares"}
MATCHES = {"geofence", "marking", "country"}
MAX_BODY = 8 * 1024
MAX_REASON = 512
AUDIENCE_KEY = "releasable_to"


def _declaration_files(root: pathlib.Path) -> list[pathlib.Path]:
    folder = root / "declarations"
    if not folder.is_dir():
        raise BadInput("missing prerequisite: the declarations folder")
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml", ".json"))
    if not files:
        raise BadInput("missing prerequisite: the declarations folder holds no declaration")
    return files


def _edge_ids(values: dict) -> set[str]:
    edges = values.get("edges")
    if not isinstance(edges, list):
        return set()
    return {e["id"] for e in edges if isinstance(e, dict) and isinstance(e.get("id"), str)}


def _dimension_for(key: str, dimensions: dict[str, dict]):
    if key in dimensions:
        return dimensions[key]
    if key == AUDIENCE_KEY:
        return dimensions.get("nation")
    for dim in dimensions.values():
        if dim.get("author_value") == key:
            return dim
    return None


def check_one(name: str, body, dimensions: dict[str, dict], edges: set[str], tally: Tally) -> None:
    if not tally.check(isinstance(body, dict), name, "body is not a mapping"):
        return
    tally.check(len(json.dumps(body, default=str).encode("utf-8")) <= MAX_BODY, name, "body is over 8 KiB")
    tally.check(not (set(body) & SERVER_SET), name,
                f"{len(set(body) & SERVER_SET)} server-set owner or date keys in the body")
    tally.check(set(body) <= ALLOWED | SERVER_SET, name, f"{len(set(body) - ALLOWED - SERVER_SET)} unknown keys")
    kind = body.get("kind")
    if not tally.check(kind in KINDS, name, "kind is not site, rule or exemption"):
        return
    reason = body.get("reason")
    tally.check(isinstance(reason, str) and bool(reason.strip()) and len(reason.encode("utf-8")) <= MAX_REASON,
                name, "reason missing, empty or over 512 bytes")
    if "supersedes" in body:
        tally.check(isinstance(body["supersedes"], str) and bool(body["supersedes"]), name, "supersedes is empty")

    if kind == "exemption":
        tally.check(isinstance(body.get("asset_id"), str) and bool(body["asset_id"]), name, "exemption needs an asset_id")
        for absent in ("scope", "labels", "match"):
            tally.check(absent not in body, name, f"exemption must not carry {absent}")
        return

    tally.check("asset_id" not in body, name, f"{kind} must not carry asset_id")
    tally.check(("match" in body) == (kind == "rule"), name,
                "a rule needs a match" if kind == "rule" else "only a rule carries a match")
    if kind == "rule" and isinstance(body.get("match"), dict):
        match = body["match"]
        tally.check(len(match) == 1 and set(match) <= MATCHES and bool(next(iter(match.values()), None)), name,
                    "match must be exactly one of geofence, marking, country, non-empty")
    elif kind == "rule" and "match" in body:
        tally.check(False, name, "match is not a mapping")

    scope = body.get("scope")
    if tally.check(isinstance(scope, dict), name, "scope missing or not a mapping"):
        tally.check(set(scope) == {"kind", "id", "records"}, name, "scope keys must be exactly kind, id, records")
        tally.check(scope.get("kind") in SCOPE_KINDS, name, "scope.kind is not edge, tier or logistics-site")
        tally.check(isinstance(scope.get("id"), str) and bool(scope["id"]), name, "scope.id missing or empty")
        records = scope.get("records")
        records_ok = (isinstance(records, list) and bool(records) and len(set(map(str, records))) == len(records)
                      and set(records) <= RECORDS)
        tally.check(records_ok, name, "scope.records must be a non-empty, unique subset of entity, logistics, spares")
        if kind == "rule" and records_ok:
            tally.check(records == ["entity"], name, "a rule's scope.records must be exactly [entity]")
        if kind == "site" and scope.get("kind") == "edge" and isinstance(scope.get("id"), str):
            tally.check(scope["id"] in edges, name,
                        f"scope.id is not among the {len(edges)} edges the values declare")

    labels = body.get("labels")
    if not tally.check(isinstance(labels, dict) and bool(labels), name, "labels missing or empty"):
        return
    tally.check(AUDIENCE_KEY in labels, name, "labels must state releasable_to, even when empty")
    nation = dimensions.get("nation")
    if nation and nation.get("author_value"):
        author = labels.get(nation["author_value"])
        tally.check(isinstance(author, str) and bool(author), name,
                    "labels must state the nation author value as one string")
    for key, value in labels.items():
        dim = _dimension_for(key, dimensions)
        if not tally.check(dim is not None, name, f"label key {key!r} is not a declared dimension"):
            continue
        values = value if isinstance(value, list) else [value]
        in_vocab = all(isinstance(v, str) and v in dim["vocabulary"] for v in values)
        tally.check(in_vocab, name, f"label {key!r} holds a value outside the dimension's vocabulary")
    if AUDIENCE_KEY in labels:
        tally.check(isinstance(labels[AUDIENCE_KEY], list), name, "releasable_to must be a list")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parents[2]))
    ap.add_argument("--values", nargs="+", required=True)
    args = ap.parse_args(argv)
    root = root_dir(args.root)
    files = _declaration_files(root)
    values = merged_values(args.values)

    tally = Tally("check_declarations")
    quiet = Tally("check_dimensions")
    dimensions = validate_dimensions(load_dimensions(root), quiet)
    if quiet.fail:
        raise BadInput("dimensions.yaml is refused by check_dimensions; fix it first")
    edges = _edge_ids(values)
    for path in files:
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            tally.check(False, path.name, "not readable as YAML or JSON")
            continue
        check_one(path.name, body, dimensions, edges, tally)
    return tally.summary()


if __name__ == "__main__":
    raise SystemExit(run_main(main))
