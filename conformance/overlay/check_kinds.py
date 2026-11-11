#!/usr/bin/env python3
"""Check DIR/kinds/, the egress kind schemas an overlay carries.

    check_kinds.py --root DIR --values FILE [FILE ...]

 - DIR/kinds/*.schema.json load with openddil-demo's egress/kinds.py `load_kinds`; any
   load error is a refusal naming the file. The openddil-demo checkout is found by the
   shared sibling rule (OPENDDIL_REPOS, then siblings of this repository), and the
   jsonschema package is needed.
 - Every DIR/kinds/fixtures/<Kind>.*.json validates against its kind; a fixture whose
   kind is not declared is a refusal.
 - The merged `egress.kinds` of the --values files (maps merge deep, lists replace, in
   the order given) must equal the kinds/ files exactly: the same names, JSON-equal.
 - At least one kind, and at least one fixture per kind.

Prints file names, kind names and schema paths, never a value from a fixture.
Exit 0 ok, 1 refused, 2 bad input or a missing prerequisite.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from common import BadInput, Tally, merged_values, root_dir, run_main  # noqa: E402
from siblings import sibling  # noqa: E402

SUFFIX = ".schema.json"


def _import_kinds():
    src = sibling("openddil-demo", "egress")
    if not (src / "kinds.py").is_file():
        raise BadInput("missing prerequisite: the openddil-demo checkout (egress/kinds.py); set OPENDDIL_REPOS")
    sys.path.insert(0, str(src))
    try:
        import kinds  # noqa: PLC0415
        import jsonschema  # noqa: PLC0415
    except ImportError as exc:
        raise BadInput(f"missing prerequisite: a python package ({exc.name})") from exc
    finally:
        sys.path.remove(str(src))
    return kinds, jsonschema


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(HERE.parents[1]))
    ap.add_argument("--values", nargs="+", required=True)
    args = ap.parse_args(argv)
    root = root_dir(args.root)
    folder = root / "kinds"
    if not folder.is_dir():
        raise BadInput("missing prerequisite: the kinds folder")
    kinds_mod, jsonschema = _import_kinds()
    values = merged_values(args.values)

    tally = Tally("check_kinds")
    files = sorted(folder.glob(f"*{SUFFIX}"))
    if not files:
        raise BadInput("missing prerequisite: the kinds folder holds no schema")

    schemas: dict[str, object] = {}
    for path in files:
        kind = path.name[: -len(SUFFIX)]
        try:
            schemas[kind] = json.loads(path.read_text(encoding="utf-8"))
            _load_one(kinds_mod, path)
        except ValueError as exc:
            tally.check(False, path.name, f"does not load: {type(exc).__name__}")
            schemas.pop(kind, None)
            continue
        tally.check(True, path.name, "loads")

    declared = (values.get("egress") or {}).get("kinds")
    if tally.check(isinstance(declared, dict), "values", "no egress.kinds in the merged values"):
        same_names = set(declared) == {p.name[: -len(SUFFIX)] for p in files}
        tally.check(same_names, "values", f"egress.kinds and kinds/ name different kinds ({len(declared)} vs {len(files)})")
        for kind, schema in sorted(declared.items()):
            if kind in schemas:
                tally.check(json.loads(json.dumps(schema)) == schemas[kind], f"{kind}{SUFFIX}",
                            "differs from egress.kinds in the values")

    fixtures = sorted((folder / "fixtures").glob("*.json")) if (folder / "fixtures").is_dir() else []
    per_kind = {k: 0 for k in schemas}
    for path in fixtures:
        kind = path.name.split(".", 1)[0]
        if not tally.check(kind in schemas or any(p.name == kind + SUFFIX for p in files), path.name,
                           "fixture names a kind that is not declared"):
            continue
        if kind not in schemas:
            continue  # the schema itself did not load; already refused
        per_kind[kind] += 1
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            tally.check(False, path.name, "is not valid JSON")
            continue
        errors = sorted(jsonschema.Draft202012Validator(schemas[kind]).iter_errors(record),
                        key=lambda e: (list(map(str, e.absolute_path)), e.validator))
        for err in errors:
            where = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
            tally.check(False, path.name, f"fails {err.validator} at {where}")
        if not errors:
            tally.check(True, path.name, "validates")
    for kind, count in sorted(per_kind.items()):
        tally.check(count >= 1, kind, "no fixture")
    return tally.summary()


def _load_one(kinds_mod, path: pathlib.Path) -> None:
    """load_kinds reads a directory, so give it a scratch directory holding this one file."""
    with tempfile.TemporaryDirectory(prefix="check-kinds-") as scratch:
        (pathlib.Path(scratch) / path.name).write_bytes(path.read_bytes())
        kinds_mod.load_kinds(scratch)


if __name__ == "__main__":
    raise SystemExit(run_main(main))
