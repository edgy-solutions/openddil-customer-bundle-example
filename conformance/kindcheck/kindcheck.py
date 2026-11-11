#!/usr/bin/env python
"""kindcheck: assemble one record per (route, episode) with the real
assembler code and validate it against the kind schema the way the gate does.
Offline: files only. Output is ids and keys only, never record bodies.

Exit: 0 all pass and nothing unchecked; 1 any fail; 3 no fail but unchecked
kinds; 2 bad input or a missing prerequisite (a sibling checkout or a package).
The sibling checkouts are found by the kit's one rule (conformance/siblings.py:
OPENDDIL_REPOS, then siblings of this repository); --*-src overrides each.
--cold: instead of build_picture/assemble, drive handle_cm_state_message with an
unprimed, empty parts book per route (a restart re-emit); incompatible with --parts*.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import inspect
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from siblings import sibling  # noqa: E402

# Each source is a sibling checkout: (the repository's name, its path under a sibling root).
# The image copies the contracts' gen/python to /proto (egress/Dockerfile). The hub wire carries
# lifecycle as a proto3 int, and _lifecycle_name imports as_maintained_pb2 to name it.
SOURCES = {
    "egress": ("openddil-demo", ("openddil-demo", "egress")),
    "contracts": ("openddil-contracts", ("openddil-contracts", "gen", "python")),
    "sim": ("openddil-logistics-sim", ("openddil-logistics-sim", "src")),
}


class BadInput(Exception):
    pass


def resolve_source(which: str, given: str | None) -> str:
    """The --*-src value, or the sibling checkout's path. A missing directory is BadInput naming the sibling."""
    name, parts = SOURCES[which]
    path = Path(given) if given else sibling(*parts)
    if not path.is_dir():
        raise BadInput(f"missing prerequisite: the {name} checkout ({'/'.join(parts[1:])}); "
                       f"set OPENDDIL_REPOS or pass --{which}-src")
    return str(path)


def _load_json(path: str, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BadInput(f"{what} {path}: {exc}") from exc


def asset_sql(assembler) -> list[str]:
    """The string literals read_asset passes to conn.fetchrow, read off the
    function's own source, so the SQL is never re-typed here."""
    tree = ast.parse(inspect.getsource(assembler.read_asset).lstrip())
    out: list[str] = []
    for node in sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)),
                       key=lambda n: (n.lineno, n.col_offset)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("fetchrow", "fetch", "fetchval")
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.append(node.args[0].value)
    return out


async def sim_records(sim_src: str, cfg_path: str):
    """Records the sim's own publisher would send, captured by a fake
    producer. Returns (topic, [(topic, key, record)])."""
    import yaml  # noqa: PLC0415

    if sim_src not in sys.path:
        sys.path.insert(0, sim_src)
    # The sim's parser lets these env vars override the file; an offline
    # check reads the file only.
    for var in ("LOGISTICS_SIM_PARTS_AVAILABILITY_TOPIC",
                "LOGISTICS_SIM_PARTS_AVAILABILITY_INTERVAL_S"):
        os.environ.pop(var, None)
    from logistics_sim.config import _parse_parts_availability  # noqa: PLC0415
    from logistics_sim.publisher import HqProducer  # noqa: PLC0415

    raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BadInput(f"--parts-sim-config {cfg_path}: must be a YAML mapping")
    if "parts_availability" not in raw:
        raw = {"parts_availability": raw}
    cfg = _parse_parts_availability(raw, cfg_path)

    captured: list[tuple[str, str, dict]] = []

    class FakeProducer:
        async def send_and_wait(self, topic, value=None, key=None):
            captured.append((topic, key.decode("utf-8"), json.loads(value.decode("utf-8"))))

    hq = HqProducer("unused:0", "unused")
    hq._producer = FakeProducer()  # the seam: the one underlying producer
    await hq.publish_parts_availability(cfg)
    return cfg.topic, captured


def _errors(validator, record) -> list[tuple[str, str]]:
    out = []
    for err in sorted(validator.iter_errors(record), key=lambda e: (list(map(str, e.absolute_path)), e.message)):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        out.append((path, err.message))
    return out


async def run_cold(args, assembler, jsonschema, routes, declarations_map, schemas,
                   cm_state, read_asset, now, out) -> int:
    checked = passed = failed = 0
    covered: set[str] = set()
    not_primed = getattr(assembler, "PartsBookNotPrimed", None)
    for route in routes:
        if not route.parts_topic:
            print(f"COLD route={route.name} kind={route.kind} skipped=no-parts-topic", file=out)
            continue
        decl = declarations_map[route.kind]
        schema = schemas.get(route.kind, {})
        state = assembler._RouteState(route=route, declarations=decl, schema=schema)
        produced: list = []

        def produce(topic, value, key, _p=produced):
            _p.append((topic, value, key))

        covered.add(route.kind)
        try:
            await assembler.handle_cm_state_message(
                state, cm_state, read_asset=read_asset, now=now, produce=produce)
        except Exception as exc:  # noqa: BLE001
            if not_primed is not None and isinstance(exc, not_primed) and not produced:
                print(f"COLD route={route.name} kind={route.kind} held=parts-book-not-primed", file=out)
                print("  PASS", file=out)
                checked += 1
                passed += 1
                continue
            raise
        if not produced:
            print(f"COLD route={route.name} kind={route.kind} produced=0", file=out)
            print("  PASS", file=out)
            checked += 1
            passed += 1
            continue
        validator = jsonschema.Draft202012Validator(schema)
        for _topic, value, _key in produced:
            record = json.loads(value)
            errs = _errors(validator, record)
            checked += 1
            pic_keys = sorted(
                assembler.pointer.get(record, decl.picture, {}) or {}) if decl.picture else []
            event_id = assembler.pointer.get(record, decl.key, "")
            episode_id = (assembler.pointer.get(record, decl.episode_id, "")
                          if decl.episode_id else "")
            print(f"COLD route={route.name} kind={route.kind} "
                  f"event_id={event_id} episode_id={episode_id}", file=out)
            print(f"COLD   picture={','.join(pic_keys)}", file=out)
            if errs:
                failed += 1
                print("  FAIL", file=out)
                for path, msg in errs:
                    print(f"  error: {path} {msg}", file=out)
            else:
                passed += 1
                print("  PASS", file=out)

    route_kinds = {r.kind for r in routes}
    unchecked = 0
    for kind in sorted(schemas):
        if kind in covered:
            continue
        reason = "no route" if kind not in route_kinds else "no episode in input"
        print(f"UNCHECKED kind={kind} reason={reason}", file=out)
        unchecked += 1
    print(f"kindcheck: checked={checked} pass={passed} fail={failed} unchecked={unchecked}", file=out)
    if failed:
        return 1
    return 3 if unchecked else 0


async def run(args, assembler, kinds_mod, jsonschema, out) -> int:
    if args.cold and (args.parts or args.parts_sim_config):
        raise BadInput("--cold cannot be combined with --parts or --parts-sim-config")
    now = datetime.now(timezone.utc)
    if args.now:
        try:
            now = assembler._parse_rfc3339(args.now)
        except ValueError as exc:
            raise BadInput(f"--now: {exc}") from exc

    schemas_dir = Path(args.schemas)
    if not schemas_dir.is_dir():
        raise BadInput(f"--schemas {args.schemas}: not a directory")
    try:
        known = kinds_mod.load_kinds(schemas_dir)
        declarations_map = kinds_mod.load_declarations(schemas_dir)
    except ValueError as exc:
        raise BadInput(str(exc)) from exc
    assemblable = [k for k, d in declarations_map.items()
                   if k in known and d.owning_tier and d.episode]
    try:
        routes = assembler.load_assembler_config(args.assembler_config, assemblable)
    except (assembler.AssemblerConfigError, OSError, ValueError) as exc:
        raise BadInput(f"--assembler-config: {exc}") from exc
    schemas = assembler._load_raw_schemas(schemas_dir)

    try:
        cm_state = assembler._decode(Path(args.cm_state).read_bytes())
    except (OSError, ValueError) as exc:
        raise BadInput(f"--cm-state {args.cm_state}: {exc}") from exc
    if not isinstance(cm_state, dict):
        raise BadInput("--cm-state: must be a JSON object")
    asset_value = _load_json(args.asset, "--asset")
    if asset_value is not None and not isinstance(asset_value, dict):
        raise BadInput("--asset: must be a JSON object or null")

    async def read_asset(_asset_id):
        return asset_value

    books = {r.name: assembler.PartsBook() for r in routes}

    # --parts files carry no topic: feed every route that has a parts_topic.
    for path in args.parts or []:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            recs = [json.loads(ln) for ln in lines if ln.strip()]
        except (OSError, ValueError) as exc:
            raise BadInput(f"--parts {path}: {exc}") from exc
        for r in routes:
            if r.parts_topic:
                for rec in recs:
                    books[r.name].ingest(rec)

    if args.parts_sim_config:
        try:
            _topic, captured = await sim_records(args.sim_src, args.parts_sim_config)
        except BadInput:
            raise
        except Exception as exc:  # noqa: BLE001 — sim parser refusals etc.
            raise BadInput(f"--parts-sim-config {args.parts_sim_config}: {exc}") from exc
        # Same rule as _main_async: a record reaches a route's PartsBook only
        # when it arrives on that route's parts_topic. The sim publishes every
        # record through its one HQ producer to cfg.topic.
        for topic, _key, rec in captured:
            for r in routes:
                if r.parts_topic and r.parts_topic == topic:
                    books[r.name].ingest(assembler._decode(json.dumps(rec).encode("utf-8")))

    if args.cold:
        return await run_cold(args, assembler, jsonschema, routes, declarations_map,
                              schemas, cm_state, read_asset, now, out)

    episodes = assembler.episodes(cm_state)
    owning_tier = assembler.owning_tier_of(cm_state)
    checked = passed = failed = 0
    covered: set[str] = set()

    for route in routes:
        decl = declarations_map[route.kind]
        schema = schemas.get(route.kind, {})
        validator = jsonschema.Draft202012Validator(schema)  # kinds.validator_for's class, no format checker
        for episode in episodes:
            picture = await assembler.build_picture(
                cm_state, episode, owning_tier,
                read_asset=read_asset, parts=books[route.name],
                part_refs=dict(route.part_refs),
                designations=dict(route.battle_condition))
            record = assembler.assemble(
                route.kind, decl, schema, cm_state, episode, picture, now)
            covered.add(route.kind)
            errs = _errors(validator, record)
            checked += 1
            pic_keys = sorted(
                assembler.pointer.get(record, decl.picture, {}) or {}) if decl.picture else []
            event_id = assembler.pointer.get(record, decl.key, "")
            episode_id = (assembler.pointer.get(record, decl.episode_id, "")
                          if decl.episode_id else "")
            print(f"route={route.name} kind={route.kind} asset={episode.asset} "
                  f"component={episode.component} fault_code={episode.fault_code} "
                  f"event_id={event_id} episode_id={episode_id}", file=out)
            print(f"  picture={','.join(pic_keys)}", file=out)
            for key in getattr(args, "show_picture", None) or []:
                print(f"  picture.{key}={json.dumps((record.get('picture') or {}).get(key), sort_keys=True)}", file=out)
            if errs:
                failed += 1
                print("  FAIL", file=out)
                for path, msg in errs:
                    print(f"  error: {path} {msg}", file=out)
            else:
                passed += 1
                print("  PASS", file=out)

    route_kinds = {r.kind for r in routes}
    unchecked = 0
    for kind in sorted(schemas):
        if kind in covered:
            continue
        reason = "no route" if kind not in route_kinds else "no episode in input"
        print(f"UNCHECKED kind={kind} reason={reason}", file=out)
        unchecked += 1

    print(f"kindcheck: checked={checked} pass={passed} fail={failed} unchecked={unchecked}", file=out)
    if failed:
        return 1
    return 3 if unchecked else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--egress-src", help="openddil-demo egress/ (default: the sibling checkout)")
    ap.add_argument("--contracts-src",
                    help="openddil-contracts gen/python, the image's /proto (default: the sibling checkout)")
    ap.add_argument("--sim-src",
                    help="logistics-sim src dir, holds the logistics_sim package (default: the sibling checkout; "
                         "needed only with --parts-sim-config)")
    ap.add_argument("--assembler-config")
    ap.add_argument("--schemas")
    ap.add_argument("--cm-state")
    ap.add_argument("--asset")
    ap.add_argument("--print-asset-sql", action="store_true")
    ap.add_argument("--parts", action="append")
    ap.add_argument("--parts-sim-config")
    ap.add_argument("--now")
    ap.add_argument("--cold", action="store_true",
                    help="drive the real handler with an empty, unprimed parts book")
    ap.add_argument("--show-picture", action="append", help="print this picture section (repeatable)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    try:
        args.egress_src = resolve_source("egress", args.egress_src)
        args.contracts_src = resolve_source("contracts", args.contracts_src)
        if args.parts_sim_config:
            args.sim_src = resolve_source("sim", args.sim_src)
    except BadInput as exc:
        print(f"kindcheck: {exc}", file=sys.stderr)
        return 2

    # The live wire's lifecycle is an int, so _lifecycle_name needs the contracts stubs, as the image has them.
    sys.path.insert(0, args.contracts_src)
    sys.path.insert(0, args.egress_src)
    try:
        import assembler  # noqa: PLC0415
        import jsonschema  # noqa: PLC0415
        import kinds as kinds_mod  # noqa: PLC0415
    except ImportError as exc:
        print(f"kindcheck: cannot import egress code from {args.egress_src}: {exc}", file=sys.stderr)
        return 2

    if args.print_asset_sql:
        for sql in asset_sql(assembler):
            print(sql)
        return 0

    missing = [f for f in ("assembler_config", "schemas", "cm_state", "asset")
               if getattr(args, f) is None]
    if missing:
        print("kindcheck: missing required: " + ", ".join("--" + m.replace("_", "-") for m in missing),
              file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args, assembler, kinds_mod, jsonschema, sys.stdout))
    except BadInput as exc:
        print(f"kindcheck: bad input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
