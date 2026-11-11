"""Shared by the overlay checkers. Stdlib plus pyyaml.

Exit codes: 0 ok, 1 refused, 2 bad input or a missing prerequisite (named on stderr).
Output is counts and ids only, never a value from the files checked.
"""

from __future__ import annotations

import pathlib
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("missing prerequisite: the pyyaml package", file=sys.stderr)
    raise SystemExit(2)

OK, REFUSED, BAD_INPUT = 0, 1, 2


class BadInput(Exception):
    """A prerequisite is missing or an argument is unusable. Exit 2."""


class Tally:
    """Counts checks and failures, and prints one FAIL line per failure."""

    def __init__(self, name: str):
        self.name, self.checked, self.fail = name, 0, 0

    def check(self, ok: bool, subject: str, rule: str) -> bool:
        self.checked += 1
        if not ok:
            self.fail += 1
            print(f"FAIL {subject}: {rule}")
        return ok

    def summary(self, stream=None) -> int:
        print(f"{self.name}: checked={self.checked} fail={self.fail}", file=stream or sys.stdout)
        return REFUSED if self.fail else OK


def root_dir(value: str) -> pathlib.Path:
    root = pathlib.Path(value)
    if not root.is_dir():
        raise BadInput(f"--root is not a directory: {value}")
    return root


def load_yaml(path: pathlib.Path, what: str):
    if not path.is_file():
        raise BadInput(f"missing prerequisite: {what} ({path.name})")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise BadInput(f"{what} ({path.name}) is not readable YAML: {type(exc).__name__}") from exc


def deep_merge(base, over):
    """Maps merge deep, everything else (lists included) is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        merged = dict(base)
        for key, value in over.items():
            merged[key] = deep_merge(base[key], value) if key in base else value
        return merged
    return over


def merged_values(files: list[str]) -> dict:
    merged: dict = {}
    for name in files:
        doc = load_yaml(pathlib.Path(name), "values file")
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            raise BadInput(f"values file ({pathlib.Path(name).name}) is not a mapping")
        merged = deep_merge(merged, doc)
    return merged


def run_main(fn, argv=None) -> int:
    try:
        return fn(argv)
    except BadInput as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        return BAD_INPUT
