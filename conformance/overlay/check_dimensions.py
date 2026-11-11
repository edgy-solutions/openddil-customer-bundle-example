#!/usr/bin/env python3
"""Check DIR/dimensions.yaml, the label dimensions an overlay declares.

    check_dimensions.py --root DIR [--nations FILE] [--print-nations]

Format (init-overlay format, pending an ADR-0055 file ruling; not a contract):

    policy_id: <^[a-z0-9][a-z0-9:._-]{0,62}$>
    dimensions:
      - name: nation              # ^[a-z][a-z0-9_]*$, unique
        type: permissive_set      # hierarchical | permissive_set | restrictive_set | exclusionary
        vocabulary: [ATL, BDR]    # non-empty, unique strings
        owner: <a role>           # non-empty
        author_value: originator_nation   # optional

`nation` must be declared, must be a permissive_set, and every value must match
^[A-Z]{3}$. With --nations FILE (one code per line, # comments) the vocabulary must equal
that set. --print-nations writes the vocabulary one per line to stdout (the summary
then goes to stderr), so a caller can hand it to Contract B.

Prints dimension names and counts, never a vocabulary value (except --print-nations).
Exit 0 ok, 1 refused, 2 bad input.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from common import BadInput, Tally, load_yaml, root_dir, run_main  # noqa: E402

POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,62}$")
NAME = re.compile(r"^[a-z][a-z0-9_]*$")
NATION_CODE = re.compile(r"^[A-Z]{3}$")
TYPES = {"hierarchical", "permissive_set", "restrictive_set", "exclusionary"}
TOP_KEYS = {"policy_id", "dimensions"}
DIM_KEYS = {"name", "type", "vocabulary", "owner", "author_value"}


def load_dimensions(root: pathlib.Path):
    return load_yaml(root / "dimensions.yaml", "the dimensions file")


def validate(doc, tally: Tally) -> dict[str, dict]:
    """Check the document and return the well-formed dimensions by name."""
    good: dict[str, dict] = {}
    if not tally.check(isinstance(doc, dict), "dimensions.yaml", "is not a mapping"):
        return good
    tally.check(set(doc) <= TOP_KEYS, "dimensions.yaml", f"{len(set(doc) - TOP_KEYS)} unknown top-level keys")
    tally.check(isinstance(doc.get("policy_id"), str) and bool(POLICY_ID.match(doc["policy_id"])),
                "policy_id", "missing or not matching ^[a-z0-9][a-z0-9:._-]{0,62}$")
    dims = doc.get("dimensions")
    if not tally.check(isinstance(dims, list) and bool(dims), "dimensions", "missing or empty"):
        return good
    seen: set[str] = set()
    for index, dim in enumerate(dims):
        label = f"dimension[{index}]"
        if not tally.check(isinstance(dim, dict), label, "is not a mapping"):
            continue
        name = dim.get("name")
        if tally.check(isinstance(name, str) and bool(NAME.match(name)), label,
                       "name missing or not matching ^[a-z][a-z0-9_]*$"):
            label = name
            tally.check(name not in seen, label, "name is declared twice")
            seen.add(name)
        tally.check(set(dim) <= DIM_KEYS, label, f"{len(set(dim) - DIM_KEYS)} unknown keys")
        tally.check(dim.get("type") in TYPES, label,
                    "type is not hierarchical, permissive_set, restrictive_set or exclusionary")
        vocab = dim.get("vocabulary")
        vocab_ok = (isinstance(vocab, list) and bool(vocab) and all(isinstance(v, str) and v for v in vocab)
                    and len(set(vocab)) == len(vocab))
        tally.check(vocab_ok, label, "vocabulary must be a non-empty list of unique strings")
        tally.check(isinstance(dim.get("owner"), str) and bool(dim["owner"].strip()), label,
                    "owner missing or empty")
        if "author_value" in dim:
            tally.check(isinstance(dim["author_value"], str) and bool(dim["author_value"]), label,
                        "author_value is empty")
        if isinstance(name, str) and vocab_ok:
            good[name] = dim
    nation = good.get("nation")
    if tally.check("nation" in seen, "nation", "no dimension named nation"):
        if nation is not None:
            tally.check(nation.get("type") == "permissive_set", "nation", "is not a permissive_set")
            bad = [v for v in nation["vocabulary"] if not NATION_CODE.match(v)]
            tally.check(not bad, "nation", f"{len(bad)} values do not match ^[A-Z]{{3}}$")
    return good


def read_nations(path: str) -> set[str]:
    target = pathlib.Path(path)
    if not target.is_file():
        raise BadInput(f"missing prerequisite: the nations file ({target.name})")
    out = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parents[2]))
    ap.add_argument("--nations")
    ap.add_argument("--print-nations", action="store_true")
    args = ap.parse_args(argv)
    root = root_dir(args.root)
    tally = Tally("check_dimensions")
    good = validate(load_dimensions(root), tally)
    if args.nations is not None and "nation" in good:
        wanted = read_nations(args.nations)
        have = set(good["nation"]["vocabulary"])
        tally.check(have == wanted, "nation",
                    f"vocabulary differs from the nations file: {len(have - wanted)} extra, {len(wanted - have)} missing")
    if args.print_nations and "nation" in good and not tally.fail:
        for code in good["nation"]["vocabulary"]:
            print(code)
        return tally.summary(sys.stderr)
    return tally.summary()


if __name__ == "__main__":
    raise SystemExit(run_main(main))
