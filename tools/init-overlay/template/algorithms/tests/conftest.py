"""Puts the algorithm harness and the contracts bindings on the path for the unit tests.

The harness is openddil-tactical-agents' `edge/` directory: set ALGORITHM_RUNTIME to it, or
keep that repository beside the others under OPENDDIL_REPOS (or as a sibling of this one).
The bindings are the contracts repository's generated python: set OPENDDIL_PROTO_PY, or have
it at <repos>/openddil-contracts/gen/python. A missing one stops the run with exit 2.
"""
import os
import sys
from pathlib import Path

import pytest


def _repo_roots():
    roots = []
    if os.environ.get("OPENDDIL_REPOS"):
        roots.append(Path(os.environ["OPENDDIL_REPOS"]))
    here = Path(__file__).resolve().parents[2]
    roots += [here.parent / "openddil", here.parent]
    return roots


def _find(env_name, *parts):
    if os.environ.get(env_name):
        found = Path(os.environ[env_name])
        if found.is_dir():
            return found
        pytest.exit(f"{env_name} is set but is not a directory: {found}", returncode=2)
    for root in _repo_roots():
        if root.joinpath(*parts).is_dir():
            return root.joinpath(*parts)
    pytest.exit(
        f"cannot find {'/'.join(parts)}: set {env_name}, or set OPENDDIL_REPOS to the folder holding it",
        returncode=2,
    )


sys.path.insert(0, str(_find("ALGORITHM_RUNTIME", "openddil-tactical-agents", "edge")))
sys.path.insert(0, str(_find("OPENDDIL_PROTO_PY", "openddil-contracts", "gen", "python")))
