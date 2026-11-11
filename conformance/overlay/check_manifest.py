#!/usr/bin/env python3
"""Check an overlay's manifest against what the overlay contains.

    check_manifest.py --root DIR [--min-present N]

The rules are those of tests/test_overlay_manifest_parity.py, taken to a root of your
choosing: version 1; every class has its required fields and a valid status; a
`present` path exists and a directory is not empty; a `MISSING` path is empty and the
class says what it travels with. Fewer than N `present` classes is a refusal (default
1), so a manifest that declares nothing cannot pass.

Prints class ids and rule names only. Exit 0 ok, 1 refused, 2 bad input.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from common import Tally, load_yaml, root_dir, run_main  # noqa: E402

VALID_STATUSES = {"present", "MISSING", "PROSPECTIVE"}
REQUIRED = ("id", "purpose", "path", "required", "status")


def _populated(target: pathlib.Path) -> bool:
    return any(target.iterdir()) if target.is_dir() else target.exists()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parents[2]))
    ap.add_argument("--min-present", type=int, default=1)
    args = ap.parse_args(argv)
    root = root_dir(args.root)
    doc = load_yaml(root / "overlay-manifest.yaml", "the overlay manifest")

    tally = Tally("check_manifest")
    if not tally.check(isinstance(doc, dict) and doc.get("version") == 1, "manifest", "version is not 1"):
        return tally.summary()
    classes = doc.get("classes")
    if not tally.check(isinstance(classes, list) and bool(classes), "manifest", "declares no classes"):
        return tally.summary()

    present = 0
    for index, cls in enumerate(classes):
        if not tally.check(isinstance(cls, dict), f"class[{index}]", "is not a mapping"):
            continue
        cid = str(cls.get("id", f"class[{index}]"))
        absent = [f for f in REQUIRED if f not in cls]
        if not tally.check(not absent, cid, f"missing fields: {', '.join(absent)}"):
            continue
        status, target = cls["status"], root / str(cls["path"])
        if not tally.check(status in VALID_STATUSES, cid, "status is not one of present, MISSING, PROSPECTIVE"):
            continue
        if status == "present":
            present += 1
            if tally.check(target.exists(), cid, "present, but nothing exists at its path") and target.is_dir():
                tally.check(any(target.iterdir()), cid, "present, but its directory is empty")
        elif status == "MISSING":
            tally.check(not _populated(target), cid, "MISSING, but its path has content: promote it to present")
            tally.check(bool(cls.get("travels_with")), cid, "MISSING, but it declares no travels_with")

    tally.check(present >= args.min_present, "manifest",
                f"{present} present classes, fewer than --min-present {args.min_present}")
    return tally.summary()


if __name__ == "__main__":
    raise SystemExit(run_main(main))
