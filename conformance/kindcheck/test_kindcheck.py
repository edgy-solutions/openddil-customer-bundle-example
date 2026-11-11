"""kindcheck tests, on fixtures built from this repository's public egress twin.

Run: python -m pytest conformance/kindcheck -q

The egress source and the contracts' generated python come from the sibling checkouts
(OPENDDIL_REPOS, then siblings of this repository). Without them kindcheck exits 2 and
these tests fail: they do not skip.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import make_fixtures

HERE = Path(__file__).resolve().parent
F = HERE / "fixtures"
TOOL = HERE / "kindcheck.py"

# uuid5 over the fixture's kind, tier, asset, component, fault code and detected time.
EVENT_ID = "165c2b28-e7a1-5259-8214-685cccc3a7a9"


def run(*extra, schemas=None, with_inputs=True):
    cmd = [sys.executable, str(TOOL)]
    if with_inputs:
        cmd += ["--assembler-config", str(F / "assembler-config.json"),
                "--schemas", str(schemas or F / "kind-schemas"),
                "--cm-state", str(F / "cm-state.json"),
                "--asset", str(F / "asset.json"),
                "--now", "2026-11-07T12:00:00Z"]
    cmd += list(extra)
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def last_line(out):
    return out.strip().splitlines()[-1]


def test_the_fixtures_pass():
    rc, out, err = run()
    assert rc == 0, out + err
    assert "  PASS" in out
    assert last_line(out) == "kindcheck: checked=1 pass=1 fail=0 unchecked=0"


def test_the_committed_fixtures_equal_what_make_fixtures_writes(tmp_path):
    built = make_fixtures.build(make_fixtures.DEFAULT_VALUES)
    assert sorted(built) == sorted(
        str(p.relative_to(F)).replace("\\", "/") for p in F.rglob("*.json")), "fixture files differ from the script's"
    for name, doc in built.items():
        assert (F / name).read_text(encoding="utf-8") == make_fixtures.render(doc), name


def test_known_ids():
    rc, out, err = run()
    assert f"event_id={EVENT_ID}" in out, out + err


def test_a_record_the_kind_refuses_fails(tmp_path):
    schemas = tmp_path / "schemas"
    shutil.copytree(F / "kind-schemas", schemas)
    path = schemas / "FaultEvent.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["picture"]["required"] = ["a_field_the_assembler_never_writes"]
    path.write_text(json.dumps(schema), encoding="utf-8")
    rc, out, err = run(schemas=schemas)
    assert rc == 1, out + err
    assert "FAIL" in out
    assert "'a_field_the_assembler_never_writes' is a required property" in out
    assert last_line(out) == "kindcheck: checked=1 pass=0 fail=1 unchecked=0"


def test_print_asset_sql():
    rc, out, err = run("--print-asset-sql", with_inputs=False)
    assert rc == 0, err
    assert "$1" in out and "SELECT" in out


def test_unchecked_kind_without_route(tmp_path):
    schemas = tmp_path / "schemas"
    shutil.copytree(F / "kind-schemas", schemas)
    (schemas / "SecondKind.schema.json").write_text(json.dumps({"type": "object"}))
    rc, out, err = run(schemas=schemas)
    assert rc == 3, out + err
    assert "UNCHECKED kind=SecondKind reason=no route" in out
    assert last_line(out) == "kindcheck: checked=1 pass=1 fail=0 unchecked=1"


def test_cold_skips_a_route_with_no_parts_topic():
    rc, out, err = run("--cold")
    assert rc == 3, out + err
    assert "skipped=no-parts-topic" in out
    assert "UNCHECKED kind=FaultEvent reason=no episode in input" in out
    assert last_line(out) == "kindcheck: checked=0 pass=0 fail=0 unchecked=1"


def test_cold_refuses_with_parts():
    rc, out, err = run("--cold", "--parts", str(F / "cm-state.json"))
    assert rc == 2, out + err


def test_a_missing_input_is_exit_2():
    rc, out, err = run("--asset", str(F / "no-such-file.json"))
    assert rc == 2
    assert "bad input" in err


def test_missing_required_arguments_are_exit_2():
    rc, out, err = run("--schemas", str(F / "kind-schemas"), with_inputs=False)
    assert rc == 2
    assert "missing required" in err


def test_a_missing_sibling_source_is_exit_2_naming_the_sibling(tmp_path):
    rc, out, err = run("--egress-src", str(tmp_path / "none"))
    assert rc == 2 and "openddil-demo" in err and out == ""
    rc, out, err = run("--contracts-src", str(tmp_path / "none"))
    assert rc == 2 and "openddil-contracts" in err and out == ""
    rc, out, err = run("--parts-sim-config", str(F / "cm-state.json"), "--sim-src", str(tmp_path / "none"))
    assert rc == 2 and "openddil-logistics-sim" in err and out == ""
