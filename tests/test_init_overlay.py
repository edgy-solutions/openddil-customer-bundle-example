"""init-overlay makes a repo in a temp dir, and every check passes on it as it comes.

Needs bash, git, helm and the sibling checkouts (OPENDDIL_REPOS): the checks run for real,
with no skips. A second test proves the checks can fail, and a third that init will not
write into a directory that holds files.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
INIT = REPO / "tools" / "init-overlay.sh"
HOOKS = ("pre-commit", "commit-msg", "pre-push")
SUMMARY = "overlay-checks: ran=10 pass=10 fail=0"


def _bash() -> str:
    # On Windows, a bare "bash" can be the WSL launcher, which cannot read these paths:
    # take the bash that ships beside git.
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for rel in ("../bin/bash.exe", "../../bin/bash.exe", "../usr/bin/bash.exe"):
                cand = (pathlib.Path(git).parent / rel).resolve()
                if cand.is_file():
                    return str(cand)
    return shutil.which("bash") or "bash"


def _env() -> dict:
    if not os.environ.get("OPENDDIL_REPOS"):
        pytest.fail("OPENDDIL_REPOS is not set: it must name the folder holding the openddil-* checkouts")
    env = dict(os.environ)
    env.update(
        OPENDDIL_BUNDLE_EXAMPLE=str(REPO),
        PYTHON=sys.executable,
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="author@example.invalid",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="author@example.invalid",
    )
    return env


def _git(cwd: pathlib.Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout


def _init(target: pathlib.Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([_bash(), INIT.as_posix(), target.as_posix()], env=env, capture_output=True, text=True)


def _checks(root: pathlib.Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([_bash(), "checks.sh"], cwd=root, env=env, capture_output=True, text=True)


@pytest.fixture(scope="module")
def inited(tmp_path_factory):
    env = _env()
    target = tmp_path_factory.mktemp("init") / "overlay"
    result = _init(target, env)
    return target, env, result


def test_init_makes_a_clean_one_commit_repo_that_passes_every_check(inited):
    target, env, result = inited
    assert result.returncode == 0, result.stderr[-1500:]
    assert _git(target, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(target, "status", "--porcelain").strip() == ""
    assert (target / ".git" / "info" / "openddil-hooks-mode").read_text().strip() == "private"
    for hook in HOOKS:
        assert (target / ".git" / "hooks" / hook).read_bytes() == (REPO / "tools" / "hooks" / hook).read_bytes(), hook
    leftover = []
    for path in target.rglob("*"):
        if path.is_file() and ".git" not in path.relative_to(target).parts:
            if re.search(rb"@[A-Z_]+@", path.read_bytes()):
                leftover.append(str(path.relative_to(target)))
    assert not leftover, leftover

    checks = _checks(target, env)
    lines = checks.stdout.strip().splitlines()
    assert checks.returncode == 0, checks.stdout[-2000:]
    assert lines[-1] == SUMMARY, lines[-12:]


def test_the_checks_can_fail(inited, tmp_path):
    target, env, result = inited
    assert result.returncode == 0, result.stderr[-1500:]
    copy = tmp_path / "planted"
    shutil.copytree(target, copy, ignore=shutil.ignore_patterns("out"))
    dims = copy / "dimensions.yaml"
    text = dims.read_text()
    assert "vocabulary: [ATL, BDR, SYL]" in text
    dims.write_text(text.replace("vocabulary: [ATL, BDR, SYL]", "vocabulary: [atl]"))
    checks = _checks(copy, env)
    assert checks.returncode != 0
    assert "overlay-checks: dimensions FAIL" in checks.stdout


def _copy_tools(dest: pathlib.Path) -> pathlib.Path:
    """The kit's shape: tools/ copied out of the clone, so there is no git to read a SHA from."""
    for rel in ("init-overlay.sh", "init-overlay", "hooks"):
        src = REPO / "tools" / rel
        if src.is_dir():
            shutil.copytree(src, dest / "tools" / rel, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            (dest / "tools").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / "tools" / rel)
    return dest / "tools" / "init-overlay.sh"


def test_a_copy_outside_a_clone_pins_its_bundle_ref(tmp_path):
    env = _env()
    init = _copy_tools(tmp_path / "kit")
    sha = "0123456789abcdef0123456789abcdef01234567"
    (tmp_path / "kit" / "tools" / "init-overlay" / "bundle.ref").write_text(sha + "\n")
    target = tmp_path / "overlay"
    result = subprocess.run([_bash(), init.as_posix(), target.as_posix()], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    workflow = (target / ".github" / "workflows" / "conformance.yml").read_text()
    assert sha in workflow
    assert "0123456" in _git(target, "log", "-1", "--format=%s")


@pytest.mark.parametrize("ref", [None, "not-a-sha\n"])
def test_a_copy_outside_a_clone_refuses_without_a_valid_bundle_ref(tmp_path, ref):
    env = _env()
    init = _copy_tools(tmp_path / "kit")
    if ref is not None:
        (tmp_path / "kit" / "tools" / "init-overlay" / "bundle.ref").write_text(ref)
    target = tmp_path / "overlay"
    result = subprocess.run([_bash(), init.as_posix(), target.as_posix()], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert not target.exists()


def test_init_refuses_a_non_empty_target(tmp_path):
    env = _env()
    target = tmp_path / "taken"
    target.mkdir()
    (target / "keep.txt").write_text("mine\n")
    result = _init(target, env)
    assert result.returncode != 0
    assert "refused" in result.stderr
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
