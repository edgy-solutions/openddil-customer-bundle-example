"""Where the sibling openddil checkouts are. One rule, shared by every check in this kit.

Search order: the directory named by OPENDDIL_REPOS, then an `openddil/` folder next
to this repository, then the folder holding this repository. The first root that
holds the path wins. When none does, the first root is returned, so a caller can name
it in an error.
"""

from __future__ import annotations

import os
import pathlib

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[1]


def roots() -> list[pathlib.Path]:
    found = [BUNDLE_ROOT.parent / "openddil", BUNDLE_ROOT.parent]
    if os.environ.get("OPENDDIL_REPOS"):
        found.insert(0, pathlib.Path(os.environ["OPENDDIL_REPOS"]))
    return found


def sibling(*parts: str) -> pathlib.Path:
    candidates = roots()
    for root in candidates:
        if root.joinpath(*parts).exists():
            return root.joinpath(*parts)
    return candidates[0].joinpath(*parts)
