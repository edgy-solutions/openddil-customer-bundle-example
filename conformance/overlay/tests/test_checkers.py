"""The overlay checkers: this repository passes, a planted defect per rule is refused (exit 1),
and a missing prerequisite is exit 2. A refusal must name its rule and never echo a value.

The openddil-demo checkout (for check_kinds) is found as the kit finds every sibling:
OPENDDIL_REPOS, then siblings of this repository. Without it check_kinds exits 2 and the
check_kinds tests fail: they do not skip.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve().parent
OVERLAY = HERE.parent
REPO = OVERLAY.parents[1]
GOOD = HERE / "fixtures" / "good"
VALUES = [str(REPO / "k8s" / "tier-node-values.yaml"), str(REPO / "k8s" / "egress-values.yaml")]

MANIFEST = {
    "version": 1,
    "classes": [
        {"id": "kinds", "purpose": "p", "path": "kinds/", "required": True, "status": "present"},
        {"id": "gap", "purpose": "p", "path": "gap/", "required": False, "status": "MISSING", "travels_with": "later"},
        {"id": "idea", "purpose": "p", "path": "idea/", "required": False, "status": "PROSPECTIVE"},
    ],
}


def run(script: str, *args: str):
    proc = subprocess.run([sys.executable, str(OVERLAY / script), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def summary(out: str, name: str) -> str:
    line = out.strip().splitlines()[-1]
    assert line.startswith(f"{name}: checked="), out
    return line


@pytest.fixture()
def overlay(tmp_path):
    """A mutable copy of the good overlay, with a manifest that fits it."""
    root = tmp_path / "overlay"
    shutil.copytree(GOOD, root)
    (root / "overlay-manifest.yaml").write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    return root


def edit_yaml(path: pathlib.Path, mutate) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


# ---- check_manifest -------------------------------------------------------------------------


def test_manifest_this_repo_passes():
    rc, out, err = run("check_manifest.py", "--root", str(REPO))
    assert rc == 0, out + err
    assert summary(out, "check_manifest").endswith("fail=0")


def test_manifest_good_overlay_passes(overlay):
    rc, out, err = run("check_manifest.py", "--root", str(overlay))
    assert rc == 0, out + err


def _version(doc): doc["version"] = 2
def _no_classes(doc): doc["classes"] = []
def _drop_field(doc): del doc["classes"][0]["purpose"]
def _bad_status(doc): doc["classes"][0]["status"] = "done"
def _present_absent(doc): doc["classes"][0]["path"] = "nowhere/"
def _missing_no_travel(doc): del doc["classes"][1]["travels_with"]


@pytest.mark.parametrize("mutate,rule", [
    (_version, "version is not 1"),
    (_no_classes, "declares no classes"),
    (_drop_field, "missing fields"),
    (_bad_status, "status is not one of"),
    (_present_absent, "nothing exists"),
    (_missing_no_travel, "no travels_with"),
])
def test_manifest_defects_are_refused(overlay, mutate, rule):
    edit_yaml(overlay / "overlay-manifest.yaml", mutate)
    rc, out, err = run("check_manifest.py", "--root", str(overlay))
    assert rc == 1, out + err
    assert rule in out


def test_manifest_empty_present_directory_is_refused(overlay):
    shutil.rmtree(overlay / "kinds")
    (overlay / "kinds").mkdir()
    rc, out, _ = run("check_manifest.py", "--root", str(overlay))
    assert rc == 1 and "directory is empty" in out


def test_manifest_missing_class_with_content_is_refused(overlay):
    (overlay / "gap").mkdir()
    (overlay / "gap" / "x").write_text("x")
    rc, out, _ = run("check_manifest.py", "--root", str(overlay))
    assert rc == 1 and "promote it to present" in out


def test_manifest_min_present(overlay):
    rc, out, _ = run("check_manifest.py", "--root", str(overlay), "--min-present", "2")
    assert rc == 1 and "fewer than --min-present 2" in out


def test_manifest_none_present_is_refused_by_default(overlay):
    edit_yaml(overlay / "overlay-manifest.yaml", lambda d: d["classes"][0].update(status="PROSPECTIVE"))
    rc, out, _ = run("check_manifest.py", "--root", str(overlay))
    assert rc == 1 and "0 present classes" in out


def test_manifest_missing_file_is_exit_2(tmp_path):
    rc, out, err = run("check_manifest.py", "--root", str(tmp_path))
    assert rc == 2 and "overlay manifest" in err and out == ""


def test_manifest_missing_root_is_exit_2(tmp_path):
    rc, _, err = run("check_manifest.py", "--root", str(tmp_path / "nope"))
    assert rc == 2 and "--root" in err


# ---- check_dimensions -----------------------------------------------------------------------


def test_dimensions_good_passes(overlay):
    rc, out, err = run("check_dimensions.py", "--root", str(overlay))
    assert rc == 0, out + err
    assert summary(out, "check_dimensions").endswith("fail=0")


def _nation(doc): return doc["dimensions"][0]


def _bad_policy(doc): doc["policy_id"] = "Bad Policy"
def _unknown_top(doc): doc["extra"] = 1
def _no_dims(doc): doc["dimensions"] = []
def _bad_name(doc): _nation(doc)["name"] = "Nation"
def _dup_name(doc): doc["dimensions"].append(dict(_nation(doc)))
def _bad_type(doc): _nation(doc)["type"] = "set"
def _empty_vocab(doc): _nation(doc)["vocabulary"] = []
def _dup_vocab(doc): _nation(doc)["vocabulary"] = ["ATL", "ATL"]
def _no_owner(doc): _nation(doc)["owner"] = " "
def _unknown_key(doc): _nation(doc)["colour"] = "red"
def _no_nation(doc): _nation(doc)["name"] = "region"
def _nation_restrictive(doc): _nation(doc)["type"] = "restrictive_set"
def _lowercase_code(doc): _nation(doc)["vocabulary"] = ["ATL", "bdr"]
def _long_code(doc): _nation(doc)["vocabulary"] = ["ATLA"]


@pytest.mark.parametrize("mutate,rule", [
    (_bad_policy, "policy_id"),
    (_unknown_top, "unknown top-level keys"),
    (_no_dims, "missing or empty"),
    (_bad_name, "name missing or not matching"),
    (_dup_name, "declared twice"),
    (_bad_type, "type is not"),
    (_empty_vocab, "non-empty list of unique strings"),
    (_dup_vocab, "non-empty list of unique strings"),
    (_no_owner, "owner missing or empty"),
    (_unknown_key, "unknown keys"),
    (_no_nation, "no dimension named nation"),
    (_nation_restrictive, "not a permissive_set"),
    (_lowercase_code, "do not match"),
    (_long_code, "do not match"),
])
def test_dimensions_defects_are_refused(overlay, mutate, rule):
    edit_yaml(overlay / "dimensions.yaml", mutate)
    rc, out, err = run("check_dimensions.py", "--root", str(overlay))
    assert rc == 1, out + err
    assert rule in out
    assert "bdr" not in out and "ATLA" not in out  # values are not echoed


def test_dimensions_nations_file(overlay, tmp_path):
    nations = tmp_path / "nations.txt"
    nations.write_text("# the set\nATL\nBDR\nSYL\n", encoding="utf-8")
    rc, out, err = run("check_dimensions.py", "--root", str(overlay), "--nations", str(nations))
    assert rc == 0, out + err
    nations.write_text("ATL\nBDR\n", encoding="utf-8")
    rc, out, _ = run("check_dimensions.py", "--root", str(overlay), "--nations", str(nations))
    assert rc == 1 and "1 extra, 0 missing" in out
    nations.write_text("ATL\nBDR\nSYL\nZZZ\n", encoding="utf-8")
    rc, out, _ = run("check_dimensions.py", "--root", str(overlay), "--nations", str(nations))
    assert rc == 1 and "0 extra, 1 missing" in out


def test_dimensions_print_nations(overlay):
    rc, out, err = run("check_dimensions.py", "--root", str(overlay), "--print-nations")
    assert rc == 0, err
    assert out.split() == ["ATL", "BDR", "SYL"]
    assert "check_dimensions: checked=" in err


def test_dimensions_print_nations_prints_nothing_when_refused(overlay):
    edit_yaml(overlay / "dimensions.yaml", _no_owner)
    rc, out, _ = run("check_dimensions.py", "--root", str(overlay), "--print-nations")
    assert rc == 1 and "ATL" not in out


def test_dimensions_missing_prerequisites_are_exit_2(overlay, tmp_path):
    (overlay / "dimensions.yaml").unlink()
    rc, _, err = run("check_dimensions.py", "--root", str(overlay))
    assert rc == 2 and "dimensions file" in err
    rc, _, err = run("check_dimensions.py", "--root", str(GOOD), "--nations", str(tmp_path / "none.txt"))
    assert rc == 2 and "nations file" in err


# ---- check_declarations ---------------------------------------------------------------------

DECL = "declarations/site-edge-north.yaml"


def decl_args(root):
    return ["--root", str(root), "--values", *VALUES]


def test_declarations_good_passes(overlay):
    rc, out, err = run("check_declarations.py", *decl_args(overlay))
    assert rc == 0, out + err
    assert summary(out, "check_declarations").endswith("fail=0")


def test_declarations_a_rule_and_an_exemption_pass(overlay):
    (overlay / "declarations" / "rule.yaml").write_text(yaml.safe_dump({
        "kind": "rule", "scope": {"kind": "edge", "id": "edge-south", "records": ["entity"]},
        "match": {"country": 99}, "labels": {"originator_nation": "BDR", "releasable_to": ["ATL"]},
        "reason": "sample"}), encoding="utf-8")
    (overlay / "declarations" / "exempt.json").write_text(json.dumps(
        {"kind": "exemption", "asset_id": "sample-asset-9", "reason": "sample"}), encoding="utf-8")
    rc, out, err = run("check_declarations.py", *decl_args(overlay))
    assert rc == 0, out + err


def _set(path, value):
    def mutate(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return mutate


def _drop(*path):
    def mutate(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return mutate


@pytest.mark.parametrize("mutate,rule", [
    (_set(["colour"], "x"), "unknown keys"),
    (_set(["declared_by"], "someone"), "server-set owner or date keys"),
    (_set(["declared_at"], "2026-01-01T00:00:00Z"), "server-set owner or date keys"),
    (_set(["kind"], "default"), "kind is not site, rule or exemption"),
    (_drop("reason"), "reason missing"),
    (_set(["reason"], "x" * 513), "reason missing"),
    (_set(["asset_id"], "sample-asset-1"), "must not carry asset_id"),
    (_set(["match"], {"country": 1}), "only a rule carries a match"),
    (_drop("scope"), "scope missing"),
    (_set(["scope", "kind"], "region"), "scope.kind is not"),
    (_set(["scope", "records"], ["entity", "everything"]), "scope.records must be"),
    (_set(["scope", "records"], []), "scope.records must be"),
    (_set(["scope", "extra"], 1), "scope keys must be exactly"),
    (_set(["scope", "id"], "edge-unknown"), "not among the 2 edges"),
    (_drop("labels"), "labels missing or empty"),
    (_drop("labels", "releasable_to"), "must state releasable_to"),
    (_drop("labels", "originator_nation"), "nation author value"),
    (_set(["labels", "originator_nation"], "ZZZ"), "outside the dimension's vocabulary"),
    (_set(["labels", "releasable_to"], ["ZZZ"]), "outside the dimension's vocabulary"),
    (_set(["labels", "releasable_to"], "ATL"), "releasable_to must be a list"),
    (_set(["labels", "colour"], "red"), "is not a declared dimension"),
    (_set(["supersedes"], ""), "supersedes is empty"),
    (_set(["reason"], "ok"), None),  # control: a valid reason still passes
])
def test_declarations_defects_are_refused(overlay, mutate, rule):
    edit_yaml(overlay / DECL, mutate)
    rc, out, err = run("check_declarations.py", *decl_args(overlay))
    if rule is None:
        assert rc == 0, out + err
        return
    assert rc == 1, out + err
    assert rule in out
    assert "ZZZ" not in out and "someone" not in out  # values are not echoed


def test_declarations_rule_defects_are_refused(overlay):
    base = {"kind": "rule", "scope": {"kind": "edge", "id": "edge-south", "records": ["entity"]},
            "match": {"country": 99}, "labels": {"originator_nation": "BDR", "releasable_to": []}, "reason": "r"}
    cases = {
        "no_match": (lambda d: d.pop("match"), "a rule needs a match"),
        "two_matches": (lambda d: d["match"].update(marking="x"), "exactly one of"),
        "bad_match": (lambda d: d.update(match={"colour": "x"}), "exactly one of"),
        "records": (lambda d: d["scope"].update(records=["entity", "logistics"]), "exactly [entity]"),
    }
    for name, (mutate, rule) in cases.items():
        body = json.loads(json.dumps(base))
        mutate(body)
        (overlay / DECL).write_text(yaml.safe_dump(body), encoding="utf-8")
        rc, out, err = run("check_declarations.py", *decl_args(overlay))
        assert rc == 1 and rule in out, (name, out, err)


def test_declarations_exemption_defects_are_refused(overlay):
    for body, rule in [
        ({"kind": "exemption", "reason": "r"}, "needs an asset_id"),
        ({"kind": "exemption", "asset_id": "a", "reason": "r", "labels": {"releasable_to": []}}, "must not carry labels"),
        ({"kind": "exemption", "asset_id": "a", "reason": "r", "scope": {}}, "must not carry scope"),
    ]:
        (overlay / DECL).write_text(yaml.safe_dump(body), encoding="utf-8")
        rc, out, _ = run("check_declarations.py", *decl_args(overlay))
        assert rc == 1 and rule in out, out


def test_declarations_oversize_body_is_refused(overlay):
    edit_yaml(overlay / DECL, _set(["labels", "originator_nation"], "ATL"))
    doc = yaml.safe_load((overlay / DECL).read_text(encoding="utf-8"))
    doc["supersedes"] = "x" * 9000
    (overlay / DECL).write_text(yaml.safe_dump(doc), encoding="utf-8")
    rc, out, _ = run("check_declarations.py", *decl_args(overlay))
    assert rc == 1 and "over 8 KiB" in out


def test_declarations_unreadable_file_is_refused(overlay):
    (overlay / DECL).write_text("kind: [unclosed", encoding="utf-8")
    rc, out, _ = run("check_declarations.py", *decl_args(overlay))
    assert rc == 1 and "not readable" in out


def test_declarations_edges_come_from_the_merged_values(overlay, tmp_path):
    later = tmp_path / "later.yaml"
    later.write_text("edges:\n  - id: edge-west\n", encoding="utf-8")  # a list replaces
    rc, out, _ = run("check_declarations.py", "--root", str(overlay), "--values", *VALUES, str(later))
    assert rc == 1 and "not among the 1 edges" in out
    edit_yaml(overlay / DECL, _set(["scope", "id"], "edge-west"))
    rc, out, err = run("check_declarations.py", "--root", str(overlay), "--values", *VALUES, str(later))
    assert rc == 0, out + err


def test_declarations_missing_prerequisites_are_exit_2(overlay, tmp_path):
    rc, _, err = run("check_declarations.py", "--root", str(overlay), "--values", str(tmp_path / "none.yaml"))
    assert rc == 2 and "values file" in err
    (overlay / "dimensions.yaml").unlink()
    rc, _, err = run("check_declarations.py", *decl_args(overlay))
    assert rc == 2 and "dimensions file" in err
    shutil.rmtree(overlay / "declarations")
    rc, _, err = run("check_declarations.py", *decl_args(overlay))
    assert rc == 2 and "declarations folder" in err


def test_declarations_empty_folder_is_exit_2(overlay):
    (overlay / DECL).unlink()
    rc, _, err = run("check_declarations.py", *decl_args(overlay))
    assert rc == 2 and "no declaration" in err


def test_declarations_refused_dimensions_is_exit_2(overlay):
    edit_yaml(overlay / "dimensions.yaml", _no_owner)
    rc, _, err = run("check_declarations.py", *decl_args(overlay))
    assert rc == 2 and "check_dimensions" in err


def test_declarations_values_are_required(overlay):
    rc, _, _ = run("check_declarations.py", "--root", str(overlay))
    assert rc == 2


# ---- check_kinds ----------------------------------------------------------------------------


def kind_args(root):
    return ["--root", str(root), "--values", *VALUES]


def test_kinds_good_passes(overlay):
    rc, out, err = run("check_kinds.py", *kind_args(overlay))
    assert rc == 0, out + err
    assert summary(out, "check_kinds").endswith("fail=0")


def test_kinds_a_schema_that_does_not_load_is_refused(overlay):
    (overlay / "kinds" / "FaultEvent.schema.json").write_text('{"type": 12}', encoding="utf-8")
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "FaultEvent.schema.json: does not load" in out


def test_kinds_a_schema_that_is_not_json_is_refused(overlay):
    (overlay / "kinds" / "FaultEvent.schema.json").write_text("{not json", encoding="utf-8")
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "FaultEvent.schema.json: does not load" in out


def test_kinds_a_fixture_that_fails_its_kind_is_refused_without_echoing_it(overlay):
    path = overlay / "kinds" / "fixtures" / "RepairOrder.sample.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["order_id"] = 12345678
    path.write_text(json.dumps(doc), encoding="utf-8")
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "RepairOrder.sample.json: fails type at $.order_id" in out
    assert "12345678" not in out


def test_kinds_a_fixture_for_an_undeclared_kind_is_refused(overlay):
    (overlay / "kinds" / "fixtures" / "Mystery.one.json").write_text("{}", encoding="utf-8")
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "Mystery.one.json: fixture names a kind that is not declared" in out


def test_kinds_a_kind_without_a_fixture_is_refused(overlay):
    (overlay / "kinds" / "fixtures" / "RepairOrder.sample.json").unlink()
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "RepairOrder: no fixture" in out


def test_kinds_files_must_equal_the_values(overlay):
    path = overlay / "kinds" / "RepairOrder.schema.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["title"] = "Other"
    path.write_text(json.dumps(doc), encoding="utf-8")
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "RepairOrder.schema.json: differs from egress.kinds" in out


def test_kinds_names_must_match_the_values(overlay):
    (overlay / "kinds" / "RepairOrder.schema.json").rename(overlay / "kinds" / "Renamed.schema.json")
    rc, out, _ = run("check_kinds.py", *kind_args(overlay))
    assert rc == 1 and "name different kinds" in out


def test_kinds_values_without_egress_kinds_are_refused(overlay, tmp_path):
    empty = tmp_path / "v.yaml"
    empty.write_text("edges: []\n", encoding="utf-8")
    rc, out, _ = run("check_kinds.py", "--root", str(overlay), "--values", str(empty))
    assert rc == 1 and "no egress.kinds" in out


def test_kinds_missing_prerequisites_are_exit_2(overlay, tmp_path, monkeypatch):
    shutil.rmtree(overlay / "kinds" / "fixtures")
    for f in (overlay / "kinds").glob("*.schema.json"):
        f.unlink()
    rc, _, err = run("check_kinds.py", *kind_args(overlay))
    assert rc == 2 and "holds no schema" in err
    shutil.rmtree(overlay / "kinds")
    rc, _, err = run("check_kinds.py", *kind_args(overlay))
    assert rc == 2 and "kinds folder" in err


def test_kinds_without_the_demo_checkout_is_exit_2(overlay, tmp_path):
    import os
    env = {**os.environ, "OPENDDIL_REPOS": str(tmp_path / "empty")}
    # The sibling rule falls back to siblings of this repository; hide them by copying the kit alone.
    kit = tmp_path / "kit" / "bundle"
    shutil.copytree(REPO / "conformance", kit / "conformance", ignore=shutil.ignore_patterns("__pycache__", "fixtures"))
    proc = subprocess.run([sys.executable, str(kit / "conformance" / "overlay" / "check_kinds.py"), *kind_args(overlay)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2 and "openddil-demo checkout" in proc.stderr
