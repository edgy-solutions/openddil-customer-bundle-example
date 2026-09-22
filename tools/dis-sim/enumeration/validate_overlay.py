#!/usr/bin/env python3
"""
validate_overlay.py — check a scenario enumeration overlay before it is used.

WHAT THIS CHECKS
----------------
The file dis-sim reads through DIS_ENTITY_TYPES_PATH: a JSON list of
    {"type": [kind, domain, country, category, subcategory, specific, extra],
     "variant": "<label>"}

  shape       a non-empty list; each row an object with exactly those two keys;
              `type` is 7 integers in their DIS field ranges
  kind        1 (platform) or 2 (munition); any other kind is refused
  duplicates  the same 7-tuple twice is refused; the same label on two
              different tuples is a warning
  labels      non-empty printable ASCII, no surrounding whitespace, <= 64 chars
  non-SISO    with --siso, each tuple must exist in that SISO-REF-010 XML;
              a tuple that does not is a WARNING, because a scenario may
              legitimately carry a locally defined type. Without --siso the
              check is reported NOT RUN, never as zero.

WHAT IT PRINTS
--------------
Counts and row numbers only. It never prints a tuple or a label, so its whole
output can be copied off the work side as it stands.

Exit 0 when there are no errors (warnings allowed), 1 otherwise.

Usage:
    python validate_overlay.py OVERLAY.json [--siso SISO-REF-010.xml]
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET

# DIS EntityType field widths (IEEE 1278.1): country is 16 bits, the rest 8.
FIELD_MAX = (255, 255, 65535, 255, 255, 255, 255)
ALLOWED_KINDS = {1, 2}
MAX_LABEL = 64


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_siso_index(path: str) -> tuple[set[tuple[int, ...]], str]:
    """Every entity-type tuple the SISO-REF-010 XML defines, at every level.

    A category or subcategory row is itself a valid type (the remaining
    fields zero), so those levels are indexed too.

    Only the Entity Types table (enum uid 30) is read. The same XML carries
    an Aggregate Types table (uid 207) that reuses the <entity> element, and
    an aggregate tuple is not an entity type.
    """
    root = ET.parse(path).getroot()
    title = root.get("title") or "untitled"
    tables = [t for t in root if _local(t.tag) == "cet" and t.get("uid") == "30"]
    if len(tables) != 1:
        raise SystemExit("--siso: expected one Entity Types table (uid 30); is this a SISO-REF-010 XML?")
    idx: set[tuple[int, ...]] = set()
    for ent in tables[0]:
        if _local(ent.tag) != "entity":
            continue
        head = (int(ent.get("kind")), int(ent.get("domain")), int(ent.get("country")))
        for cat in ent:
            if _local(cat.tag) != "category":
                continue
            c = int(cat.get("value"))
            idx.add(head + (c, 0, 0, 0))
            for sub in cat:
                if _local(sub.tag) != "subcategory":
                    continue
                s = int(sub.get("value"))
                idx.add(head + (c, s, 0, 0))
                for spec in sub:
                    if _local(spec.tag) != "specific":
                        continue
                    p = int(spec.get("value"))
                    idx.add(head + (c, s, p, 0))
                    for ext in spec:
                        if _local(ext.tag) == "extra":
                            idx.add(head + (c, s, p, int(ext.get("value"))))
    if not idx:
        raise SystemExit(f"--siso: no entity types found; is this a SISO-REF-010 XML?")
    return idx, title


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("overlay")
    ap.add_argument("--siso", help="path to the SISO-REF-010 XML to check tuples against")
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []

    try:
        with open(args.overlay, encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        # Line and column only; the exception text would quote the content.
        print(f"ERROR: not valid JSON at line {exc.lineno} column {exc.colno}")
        return 1
    except OSError as exc:
        print(f"ERROR: cannot read the overlay ({exc.__class__.__name__})")
        return 1

    if not isinstance(raw, list):
        print("ERROR: the top level must be a JSON list")
        return 1
    if not raw:
        print("ERROR: the list is empty -- dis-sim refuses an empty overlay, so fill it first")
        return 1

    kinds = {1: 0, 2: 0}
    good: list[tuple[int, tuple[int, ...], str]] = []  # (row, tuple, label)

    for row, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            errors.append(f"row {row}: not an object")
            continue
        keys = set(item)
        if keys != {"type", "variant"}:
            missing = {"type", "variant"} - keys
            extra = len(keys - {"type", "variant"})
            if missing:
                errors.append(f"row {row}: missing key(s): {', '.join(sorted(missing))}")
            if extra:
                errors.append(f"row {row}: {extra} unexpected key(s)")
            if missing:
                continue

        t = item["type"]
        if not isinstance(t, list) or len(t) != 7:
            errors.append(f"row {row}: `type` must be a list of 7 integers")
            continue
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in t):
            errors.append(f"row {row}: `type` holds a non-integer")
            continue
        out_of_range = [i + 1 for i, v in enumerate(t) if not 0 <= v <= FIELD_MAX[i]]
        if out_of_range:
            errors.append(f"row {row}: `type` field(s) {out_of_range} out of range")
            continue
        if t[0] not in ALLOWED_KINDS:
            errors.append(f"row {row}: kind is not 1 (platform) or 2 (munition)")
            continue

        label = item["variant"]
        if not isinstance(label, str) or not label:
            errors.append(f"row {row}: `variant` must be a non-empty string")
            continue
        if label != label.strip():
            errors.append(f"row {row}: `variant` has leading or trailing whitespace")
            continue
        if not label.isascii() or not label.isprintable():
            errors.append(f"row {row}: `variant` must be printable ASCII")
            continue
        if len(label) > MAX_LABEL:
            errors.append(f"row {row}: `variant` longer than {MAX_LABEL} characters")
            continue

        kinds[t[0]] += 1
        good.append((row, tuple(t), label))

    first_row_of_tuple: dict[tuple[int, ...], int] = {}
    first_row_of_label: dict[str, tuple[int, tuple[int, ...]]] = {}
    for row, tup, label in good:
        if tup in first_row_of_tuple:
            errors.append(f"row {row}: duplicate type, first seen at row {first_row_of_tuple[tup]}")
        else:
            first_row_of_tuple[tup] = row
        seen = first_row_of_label.get(label)
        if seen and seen[1] != tup:
            warnings.append(f"row {row}: same label as row {seen[0]} on a different type")
        elif not seen:
            first_row_of_label[label] = (row, tup)

    not_in_siso: int | None = None
    siso_title = "NOT RUN"
    if args.siso:
        index, siso_title = load_siso_index(args.siso)
        not_in_siso = 0
        for row, tup, _ in good:
            if tup not in index:
                not_in_siso += 1
                warnings.append(f"row {row}: type not defined in {siso_title}")

    for line in errors:
        print("ERROR:", line)
    for line in warnings:
        print("WARN: ", line)

    print()
    print(f"rows {len(raw)}   errors {len(errors)}   warnings {len(warnings)}")
    print(f"checked against: {siso_title}")
    if errors:
        print("RESULT: REFUSED -- fix the errors above; nothing to report yet")
        return 1
    print("RESULT: VALID")
    print()
    print("REPORT BACK (four numbers):")
    print(f"  ROWS        {len(raw)}")
    print(f"  KIND_1      {kinds[1]}")
    print(f"  KIND_2      {kinds[2]}")
    print(f"  NOT_IN_SISO {not_in_siso if not_in_siso is not None else 'NOT RUN'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
