#!/usr/bin/env python3
"""Contract B conformance validator.

Validates records against the element-plane Contract B shape: the existing
telemetry/inventory envelopes (see openddil-logistics-sim's publisher and
openddil-projector's handlers) plus four additive blocks — sustainment_id,
provenance (labels), extraction, extras — and operational.readiness. A third
plane, "parts" (per-site spare-part stock on `parts-availability`, keyed by
site + part rather than by asset), carries its own rules in place of the
asset-identifier ones.

This validates RECORDS, not a mock of them: every rule below has a minimal
fixture under fixtures/refused/ that fails only that rule (see selftest.sh),
and an empty run is a distinct exit code (2), never a silent pass.

Stdlib only. Python >= 3.10. Single file.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time

# ---------------------------------------------------------------------------
# Embedded vocabularies. --check-enums compares these against the canonical
# .proto tree so drift between this kit and the real contract is caught
# rather than silently tolerated.
# ---------------------------------------------------------------------------
POWER_STATES = {
    "POWER_STATE_UNSPECIFIED", "POWER_STATE_OFF", "POWER_STATE_ON",
    "POWER_STATE_STANDBY", "POWER_STATE_MAINTENANCE", "POWER_STATE_SHUTTING_DOWN",
}
HEALTH_STATES = {
    "HEALTH_STATE_UNSPECIFIED", "HEALTH_STATE_NOMINAL", "HEALTH_STATE_DEGRADED",
    "HEALTH_STATE_FAULT", "HEALTH_STATE_FAILED",
}
# The validator never allows LOGISTICS_SEVERITY_UNSPECIFIED — a factor with
# no determinable severity is not a factor worth reporting.
SEVERITIES = {
    "LOGISTICS_SEVERITY_OK", "LOGISTICS_SEVERITY_DEGRADED",
    "LOGISTICS_SEVERITY_CRITICAL", "LOGISTICS_SEVERITY_NON_OPERATIONAL",
}
SEVERITY_ORDER = {
    "LOGISTICS_SEVERITY_OK": 0,
    "LOGISTICS_SEVERITY_DEGRADED": 1,
    "LOGISTICS_SEVERITY_CRITICAL": 2,
    "LOGISTICS_SEVERITY_NON_OPERATIONAL": 3,
}
READINESS_STATUSES = {"FMC", "PMC", "NMC"}

PLANE_FIELDS = {
    "telemetry": {
        "asset_id", "sustainment_id", "site", "platform_variant", "profile_name",
        "observed_at_ns", "operational", "elements", "provenance",
        "extraction", "extras",
    },
    "inventory": {
        "asset_id", "sustainment_id", "site", "layer_name", "platform_variant",
        "available_count", "allocated_count", "total_count", "observed_at_ns",
        "provenance", "extraction", "extras",
    },
    "parts": {
        "site", "part_ref", "item", "on_hand", "lead_time_days", "source",
        "nearest_site_with_stock", "nearest_on_hand", "observed_at_ns",
        "provenance", "extraction", "extras",
    },
}

LOWER_BOUND_NS = 946684800_000000000  # 2000-01-01T00:00:00Z
FUTURE_SLACK_NS = 300_000000000       # 300 s

# ---------------------------------------------------------------------------
# Stable rule ids, in a fixed order. --list-rules prints exactly this list,
# and fixtures/refused/<rule>.jsonl must exist for every one of them
# (selftest enforces the coverage both ways: every rule has a case, and
# every case names a real rule).
# ---------------------------------------------------------------------------
GENERIC_RULES = [
    "unparseable", "plane_unknown", "unknown_field", "asset_id_invalid",
    "sustainment_id_absent", "site_absent", "identifier_collapsed", "key_absent",
    "key_mismatch", "label_misplaced", "label_absent", "label_invalid",
    "label_unknown_nation", "timestamp_invalid", "extraction_absent",
    "extraction_before_observation", "extras_invalid",
]
TELEMETRY_RULES = [
    "operational_invalid", "vocab_power_state", "vocab_health_state",
    "elements_invalid", "readiness_absent", "readiness_vocab",
    "readiness_factor_mismatch", "readiness_health_conflict",
]
INVENTORY_RULES = ["counts_invalid"]
PARTS_RULES = ["part_invalid", "stock_invalid", "source_absent", "nearest_invalid"]
STREAM_RULES = ["conflicting_duplicate", "order_regression"]

ALL_RULES = GENERIC_RULES + TELEMETRY_RULES + INVENTORY_RULES + PARTS_RULES + STREAM_RULES


# ---------------------------------------------------------------------------
# Small scalar helpers
# ---------------------------------------------------------------------------
def is_int_not_bool(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def is_nonempty_str(x) -> bool:
    return isinstance(x, str) and x != ""


def is_scalar(x) -> bool:
    return x is None or isinstance(x, (str, int, float, bool))


# asset_id is OPAQUE inside the system of record: nothing here parses its
# internal structure (no splitting, no field-by-field range check). It is
# validated only as "present and a non-empty string." Nation lookup for
# labels runs off the declared `site` field instead (see site_absent),
# not off any structure assumed inside asset_id.
def asset_id_ok(aid) -> bool:
    return is_nonempty_str(aid)


# ---------------------------------------------------------------------------
# Input parsing: a stream of concatenated JSON values. Handles JSONL and
# `rpk topic consume` output, pretty or compact, without needing to know
# which it is in advance.
# ---------------------------------------------------------------------------
def iter_json_values(text: str):
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return
        obj, end = dec.raw_decode(text, i)
        yield obj
        i = end


def read_source(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Record model: unwrap the envelope (or treat as bare), returning
#   ok, is_envelope, key, topic, record, keys_checked_increment
# `ok` False means unparseable; nothing else is populated meaningfully.
# ---------------------------------------------------------------------------
def unwrap(raw):
    if isinstance(raw, dict) and "value" in raw:
        key = raw.get("key")
        topic = raw.get("topic")
        value = raw["value"]
        if isinstance(value, str):
            try:
                record = json.loads(value)
            except (ValueError, TypeError):
                return False, True, key, topic, None
        else:
            record = value
        if not isinstance(record, dict):
            return False, True, key, topic, None
        return True, True, key, topic, record
    if isinstance(raw, dict):
        return True, False, None, None, raw
    return False, False, None, None, None


def resolve_plane(topic, record, plane_arg):
    if isinstance(topic, str):
        if "asset-element-telemetry" in topic:
            return "telemetry"
        if "asset-element-inventory" in topic:
            return "inventory"
        if "parts-availability" in topic:
            return "parts"
    if plane_arg:
        return plane_arg
    if isinstance(record, dict):
        if "elements" in record:
            return "telemetry"
        if "layer_name" in record:
            return "inventory"
        if "part_ref" in record:
            return "parts"
    return None


def expected_key(plane, record):
    aid = record.get("asset_id")
    if plane == "telemetry":
        if isinstance(aid, str) and aid != "":
            return aid
        return None
    if plane == "inventory":
        layer = record.get("layer_name")
        if isinstance(aid, str) and aid != "" and isinstance(layer, str) and layer != "":
            return f"{aid}:{layer}"
        return None
    if plane == "parts":
        site = record.get("site")
        part_ref = record.get("part_ref")
        if isinstance(site, str) and site != "" and isinstance(part_ref, str) and part_ref != "":
            return f"{site}:{part_ref}"
        return None
    return None


def canonical_value(record):
    # The extraction block records WHEN a record was pulled from the
    # source system, not WHAT was observed. A re-read of the same
    # observation after an adapter restart gets a new extracted_at_ns on
    # otherwise-identical content; that is a replay, not a conflict. So
    # the canonical form used for duplicate/replay comparison excludes it.
    stripped = {k: v for k, v in record.items() if k != "extraction"}
    return json.dumps(stripped, sort_keys=True)


def check_labels(record, nations, out):
    prov = record.get("provenance")
    prov_is_obj = isinstance(prov, dict)
    originator = prov.get("originator_nation") if prov_is_obj else None
    releasable = prov.get("releasable_to") if prov_is_obj else None

    label_absent = (
        not prov_is_obj
        or not is_nonempty_str(originator)
        or not isinstance(releasable, list)
    )
    if label_absent:
        out.add("label_absent")

    invalid = False
    if is_nonempty_str(originator) and not re.fullmatch(r"[A-Z]{3}", originator):
        invalid = True
    if isinstance(releasable, list):
        seen = set()
        for nation in releasable:
            if is_nonempty_str(nation):
                if not re.fullmatch(r"[A-Z]{3}", nation):
                    invalid = True
                if nation in seen:
                    invalid = True
                seen.add(nation)
            else:
                invalid = True
    if invalid:
        out.add("label_invalid")

    if nations is not None:
        unknown = False
        if is_nonempty_str(originator) and originator not in nations:
            unknown = True
        if isinstance(releasable, list):
            for nation in releasable:
                if is_nonempty_str(nation) and nation not in nations:
                    unknown = True
        if unknown:
            out.add("label_unknown_nation")

    return originator if is_nonempty_str(originator) else None


def check_extraction(record, now_ns, out):
    extraction = record.get("extraction")
    if not isinstance(extraction, dict):
        out.add("extraction_absent")
        return None
    cursor = extraction.get("cursor")
    eat = extraction.get("extracted_at_ns")
    if not is_nonempty_str(cursor) or not is_int_not_bool(eat):
        out.add("extraction_absent")
        eat = eat if is_int_not_bool(eat) else None

    obs = record.get("observed_at_ns")
    if is_int_not_bool(eat) and is_int_not_bool(obs) and eat < obs:
        out.add("extraction_before_observation")
    if is_int_not_bool(eat) and eat > now_ns + FUTURE_SLACK_NS:
        out.add("timestamp_invalid")
    return eat


def check_extras(record, plane, out):
    if "extras" not in record:
        return
    extras = record["extras"]
    bad = False
    if not isinstance(extras, dict) or set(extras.keys()) != {"declared", "fields"}:
        bad = True
    else:
        if extras.get("declared") != "organic":
            bad = True
        fields = extras.get("fields")
        if not isinstance(fields, dict):
            bad = True
        else:
            allowed = PLANE_FIELDS.get(plane, set())
            for k, v in fields.items():
                if not is_scalar(v):
                    bad = True
                if plane and k in allowed:
                    bad = True
    if bad:
        out.add("extras_invalid")


def check_readiness(operational, readiness_mode, out):
    readiness = operational.get("readiness") if isinstance(operational, dict) else None
    if readiness is None:
        if readiness_mode == "required":
            out.add("readiness_absent")
        return
    if not isinstance(readiness, dict):
        out.add("readiness_vocab")
        return

    status = readiness.get("status")
    factors = readiness.get("factors")
    vocab_fail = status not in READINESS_STATUSES
    if not isinstance(factors, list):
        vocab_fail = True
        factors = []
    worst = "LOGISTICS_SEVERITY_OK"
    for f in factors:
        if not isinstance(f, dict):
            vocab_fail = True
            continue
        fid = f.get("factor_id")
        desc = f.get("description")
        sev = f.get("severity")
        if not is_nonempty_str(fid) or not is_nonempty_str(desc):
            vocab_fail = True
        if sev not in SEVERITIES:
            vocab_fail = True
        elif SEVERITY_ORDER[sev] > SEVERITY_ORDER[worst]:
            worst = sev

    if vocab_fail:
        out.add("readiness_vocab")
        return

    if status == "FMC" and worst != "LOGISTICS_SEVERITY_OK":
        out.add("readiness_factor_mismatch")
    elif status == "PMC" and worst not in ("LOGISTICS_SEVERITY_DEGRADED", "LOGISTICS_SEVERITY_CRITICAL"):
        out.add("readiness_factor_mismatch")
    elif status == "NMC" and worst != "LOGISTICS_SEVERITY_NON_OPERATIONAL":
        out.add("readiness_factor_mismatch")

    health = operational.get("health_state")
    conflict = False
    if status == "FMC" and health != "HEALTH_STATE_NOMINAL":
        conflict = True
    elif status == "PMC" and health not in ("HEALTH_STATE_DEGRADED", "HEALTH_STATE_FAULT"):
        conflict = True
    elif status == "NMC" and health not in ("HEALTH_STATE_FAULT", "HEALTH_STATE_FAILED"):
        conflict = True
    degraded = operational.get("degraded")
    if isinstance(degraded, bool) and degraded != (status != "FMC"):
        conflict = True
    if conflict:
        out.add("readiness_health_conflict")


def check_elements(record, out):
    elements = record.get("elements")
    if not isinstance(elements, list):
        out.add("elements_invalid")
        return
    for e in elements:
        bad = not isinstance(e, dict)
        if not bad:
            eid = e.get("element_id")
            layer = e.get("layer_name")
            health = e.get("health")
            depth = e.get("layer_depth")
            if not isinstance(eid, (str, int)) or isinstance(eid, bool):
                bad = True
            if not is_nonempty_str(layer):
                bad = True
            if "health" in e and not (isinstance(health, (int, float)) and not isinstance(health, bool) and 0 <= health <= 1):
                bad = True
            if "layer_depth" in e and not (is_int_not_bool(depth) and depth >= 0):
                bad = True
            for flag in ("tx_active", "rx_active"):
                if flag in e and not isinstance(e[flag], bool):
                    bad = True
        if bad:
            out.add("elements_invalid")
            return


def check_operational(record, out):
    operational = record.get("operational")
    if not isinstance(operational, dict):
        out.add("operational_invalid")
        return operational
    for flag in ("degraded", "actively_transmitting", "actively_receiving"):
        if flag in operational and not isinstance(operational[flag], bool):
            out.add("operational_invalid")
            break
    if operational.get("power_state") not in POWER_STATES:
        out.add("vocab_power_state")
    if operational.get("health_state") not in HEALTH_STATES:
        out.add("vocab_health_state")
    return operational


def check_counts(record, out):
    layer = record.get("layer_name")
    if not is_nonempty_str(layer):
        out.add("counts_invalid")
        return
    avail = record.get("available_count")
    alloc = record.get("allocated_count")
    total = record.get("total_count")
    ok = True
    for v in (avail, alloc, total):
        if not is_int_not_bool(v) or v < 0:
            ok = False
    if ok and (avail + alloc > total):
        ok = False
    if not ok:
        out.add("counts_invalid")


def check_parts(record, out):
    # The parts plane is keyed by site + part, not by asset: there is no
    # asset_id/sustainment_id pair here, opaque or otherwise.
    part_ref = record.get("part_ref")
    bad_part = not is_nonempty_str(part_ref)
    if "item" in record and not is_nonempty_str(record.get("item")):
        bad_part = True
    if bad_part:
        out.add("part_invalid")

    on_hand = record.get("on_hand")
    bad_stock = not is_int_not_bool(on_hand) or on_hand < 0
    if "lead_time_days" in record:
        ltd = record.get("lead_time_days")
        # An unknown lead time is ABSENT, never null or 0 — a source that
        # cannot estimate lead time omits the key rather than guessing.
        if not is_int_not_bool(ltd) or ltd < 0:
            bad_stock = True
    if bad_stock:
        out.add("stock_invalid")

    if not is_nonempty_str(record.get("source")):
        out.add("source_absent")

    if "nearest_site_with_stock" not in record:
        out.add("nearest_invalid")
    else:
        nearest = record["nearest_site_with_stock"]
        if nearest is None:
            # No site in the source's own search order has stock: there is
            # no count to report, so nearest_on_hand must be absent, not 0.
            if "nearest_on_hand" in record:
                out.add("nearest_invalid")
        elif is_nonempty_str(nearest):
            noh = record.get("nearest_on_hand")
            if not is_int_not_bool(noh) or noh < 1:
                out.add("nearest_invalid")
        else:
            out.add("nearest_invalid")


# ---------------------------------------------------------------------------
# Per-record validation. Returns the set of failing rule ids plus data for
# summary tallies / stream tracking.
# ---------------------------------------------------------------------------
def validate_record(raw, plane_arg, readiness_mode, nations, now_ns):
    fails = set()
    ok, is_envelope, key, topic, record = unwrap(raw)
    info = {"plane": None, "originator": None, "both_ids": False,
            "lag_s": None, "key": key if is_envelope else None,
            "asset_id": None}
    if not ok:
        fails.add("unparseable")
        return fails, record, info

    plane = resolve_plane(topic, record, plane_arg)
    info["plane"] = plane
    if plane is None:
        fails.add("plane_unknown")

    # unknown_field / label_misplaced — label fields are never allowed at
    # top level in either plane, so this check does not need a plane.
    allowed = PLANE_FIELDS.get(plane) if plane else None
    for k in record.keys():
        if k in ("originator_nation", "releasable_to"):
            fails.add("label_misplaced")
        elif allowed is not None and k not in allowed:
            fails.add("unknown_field")

    aid = record.get("asset_id")
    info["asset_id"] = aid if isinstance(aid, str) else None
    # The parts plane is keyed by site + part; it carries no asset_id or
    # sustainment_id, opaque or otherwise, so none of the three
    # asset-identifier rules apply to it.
    if plane != "parts":
        if not asset_id_ok(aid):
            fails.add("asset_id_invalid")

        sid = record.get("sustainment_id")
        if not is_nonempty_str(sid):
            fails.add("sustainment_id_absent")
        if isinstance(sid, str) and sid == aid:
            fails.add("identifier_collapsed")
        info["both_ids"] = isinstance(aid, str) and aid != "" and is_nonempty_str(sid)

    site = record.get("site")
    if not is_nonempty_str(site):
        fails.add("site_absent")

    if is_envelope:
        if key is None:
            fails.add("key_absent")
        elif plane is not None:
            exp = expected_key(plane, record)
            if exp is not None and key != exp:
                fails.add("key_mismatch")

    info["originator"] = check_labels(record, nations, fails)

    obs = record.get("observed_at_ns")
    if not is_int_not_bool(obs) or obs < LOWER_BOUND_NS or obs > now_ns + FUTURE_SLACK_NS:
        fails.add("timestamp_invalid")

    eat = check_extraction(record, now_ns, fails)
    if is_int_not_bool(eat) and is_int_not_bool(obs):
        info["lag_s"] = (eat - obs) / 1e9

    check_extras(record, plane, fails)

    if plane == "telemetry":
        operational = check_operational(record, fails)
        check_elements(record, fails)
        if isinstance(operational, dict):
            check_readiness(operational, readiness_mode, fails)
        elif readiness_mode == "required":
            fails.add("readiness_absent")
        if isinstance(operational, dict):
            status = operational.get("readiness", {})
            status = status.get("status") if isinstance(status, dict) else None
            if status in READINESS_STATUSES:
                info["readiness_status"] = status
    elif plane == "inventory":
        check_counts(record, fails)
    elif plane == "parts":
        check_parts(record, fails)

    return fails, record, info


def main(argv):
    p = argparse.ArgumentParser(prog="validate.py", add_help=True)
    p.add_argument("files", nargs="*", default=["-"], metavar="FILE")
    p.add_argument("--plane", choices=["telemetry", "inventory", "parts"])
    p.add_argument("--readiness", choices=["required", "optional"], default="required")
    p.add_argument("--nations")
    p.add_argument("--now-ns", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--show-refused", action="store_true")
    p.add_argument("--list-rules", action="store_true")
    p.add_argument("--check-enums")

    def _error(message):
        p.print_usage(sys.stderr)
        print(f"{p.prog}: error: {message}", file=sys.stderr)
        sys.exit(3)

    p.error = _error  # type: ignore[assignment]

    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        # argparse itself may call sys.exit(2) on -h/--help; honour that,
        # everything else routes through _error above (exit 3).
        raise e

    if args.list_rules:
        for r in ALL_RULES:
            print(r)
        return 0

    if args.check_enums:
        return do_check_enums(args.check_enums)

    now_ns = args.now_ns if args.now_ns is not None else time.time_ns()

    nations = None
    if args.nations:
        try:
            with open(args.nations, "r", encoding="utf-8") as f:
                nations = {
                    line.split("#", 1)[0].strip()
                    for line in f
                    if line.split("#", 1)[0].strip()
                }
        except OSError as e:
            print(f"contract-b: cannot read --nations file: {e}", file=sys.stderr)
            return 3

    try:
        raws = []
        for path in args.files:
            raws.extend(iter_json_values(read_source(path)))
    except OSError as e:
        print(f"contract-b: IO error: {e}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as e:
        print(f"contract-b: malformed input stream: {e}", file=sys.stderr)
        return 3

    records = len(raws)
    if records == 0:
        print("contract-b: no records: nothing was validated")
        return 2

    rule_counts = {r: 0 for r in ALL_RULES}
    accepted = refused = replayed = 0
    plane_counts = {"telemetry": 0, "inventory": 0, "parts": 0}
    assets = set()
    originators = set()
    labelled = 0
    both_present = 0
    lags = []
    readiness_counts = {"FMC": 0, "PMC": 0, "NMC": 0}
    refused_examples = {r: [] for r in ALL_RULES}

    stream_state: dict[tuple[str, str], dict] = {}

    for idx, raw in enumerate(raws):
        fails, record, info = validate_record(raw, args.plane, args.readiness, nations, now_ns)

        if info["plane"] in plane_counts:
            plane_counts[info["plane"]] += 1
        if info["asset_id"]:
            assets.add(info["asset_id"])
        if info["originator"]:
            originators.add(info["originator"])
            labelled += 1
        if info["both_ids"]:
            both_present += 1
        if info["lag_s"] is not None:
            lags.append(info["lag_s"])
        status = info.get("readiness_status")
        if status in readiness_counts:
            readiness_counts[status] += 1

        is_replay = False
        if isinstance(record, dict) and info["plane"] in ("telemetry", "inventory", "parts"):
            exp = expected_key(info["plane"], record)
            obs = record.get("observed_at_ns")
            if exp is not None and is_int_not_bool(obs):
                state = stream_state.setdefault((info["plane"], exp), {"ns_to_canon": {}, "max_ns": None})
                canon = canonical_value(record)
                if obs in state["ns_to_canon"]:
                    if state["ns_to_canon"][obs] != canon:
                        fails.add("conflicting_duplicate")
                    elif obs == state["max_ns"]:
                        is_replay = True
                    else:
                        fails.add("order_regression")
                else:
                    if state["max_ns"] is not None and obs < state["max_ns"]:
                        fails.add("order_regression")
                    state["ns_to_canon"][obs] = canon
                    if state["max_ns"] is None or obs > state["max_ns"]:
                        state["max_ns"] = obs

        if fails:
            refused += 1
            for r in fails:
                rule_counts[r] += 1
                if len(refused_examples[r]) < 3:
                    refused_examples[r].append((idx, info["key"]))
        else:
            accepted += 1
            if is_replay:
                replayed += 1

    keys_checked = sum(1 for raw in raws if unwrap(raw)[0])

    result = {
        "records": records,
        "accepted": accepted,
        "refused": refused,
        "replayed": replayed,
        "assets": len(assets),
        "planes": plane_counts,
        "keys_checked": keys_checked,
        "refused_by_rule": rule_counts,
        "readiness": readiness_counts,
        "labels": {"originator_nations": len(originators), "labelled": labelled},
        "identifiers": {"both_present": both_present},
        "extraction_lag_seconds": {
            "p50": statistics.median(lags) if lags else None,
            "max": max(lags) if lags else None,
        },
    }

    emit(result, args.json)

    if args.show_refused and refused:
        print("WARNING: output below contains record keys; do not share it "
              "outside the side that produced it.", file=sys.stderr)
        for r in ALL_RULES:
            if rule_counts[r]:
                print(f"{r}:")
                for i, k in refused_examples[r]:
                    print(f"  record={i} key={k!r}")

    return 1 if refused else 0


def emit(result, as_json):
    if as_json:
        print(json.dumps(result))
        return
    planes = result["planes"]
    print(
        f"contract-b: records={result['records']} accepted={result['accepted']} "
        f"refused={result['refused']} replayed={result['replayed']} "
        f"assets={result['assets']} "
        f"planes=telemetry:{planes.get('telemetry', 0)},inventory:{planes.get('inventory', 0)},"
        f"parts:{planes.get('parts', 0)} "
        f"keys_checked={result['keys_checked']}"
    )
    print("refused by rule:")
    nonzero = [(r, c) for r, c in sorted(result["refused_by_rule"].items()) if c]
    if not nonzero:
        print("  none")
    else:
        for r, c in nonzero:
            print(f"  {r}={c}")
    ready = result["readiness"]
    print(f"readiness: FMC={ready['FMC']} PMC={ready['PMC']} NMC={ready['NMC']}")
    labels = result["labels"]
    print(f"labels: originator_nations={labels['originator_nations']} labelled={labels['labelled']}")
    print(f"identifiers: both_present={result['identifiers']['both_present']}")
    lag = result["extraction_lag_seconds"]
    if lag["p50"] is None:
        print("extraction lag seconds: n/a")
    else:
        print(f"extraction lag seconds: p50={lag['p50']:.1f} max={lag['max']:.1f}")


def parse_enum(text, enum_name):
    m = re.search(rf"enum\s+{enum_name}\s*\{{(.*?)\n\}}", text, re.S)
    if not m:
        return None
    return set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*-?\d+\s*;", m.group(1), re.M))


def do_check_enums(proto_dir):
    telemetry_path = f"{proto_dir}/openddil/telemetry/v1/telemetry.proto"
    logistics_path = f"{proto_dir}/openddil/logistics/v1/logistics_status.proto"
    try:
        with open(telemetry_path, "r", encoding="utf-8") as f:
            telemetry_src = f.read()
        with open(logistics_path, "r", encoding="utf-8") as f:
            logistics_src = f.read()
    except OSError as e:
        print(f"contract-b: cannot read proto tree: {e}", file=sys.stderr)
        return 3

    power = parse_enum(telemetry_src, "PowerState")
    health = parse_enum(telemetry_src, "HealthState")
    severity = parse_enum(logistics_src, "LogisticsSeverity")
    if severity is not None:
        severity = severity - {"LOGISTICS_SEVERITY_UNSPECIFIED"}

    diffs = []
    for label, parsed, embedded in (
        ("PowerState", power, POWER_STATES),
        ("HealthState", health, HEALTH_STATES),
        ("LogisticsSeverity", severity, SEVERITIES),
    ):
        if parsed is None:
            diffs.append(f"{label}: not found in proto tree")
            continue
        missing = embedded - parsed
        extra = parsed - embedded
        if missing or extra:
            diffs.append(f"{label}: missing={sorted(missing)} extra={sorted(extra)}")

    if diffs:
        print("enums: drift detected")
        for d in diffs:
            print(f"  {d}")
        return 4
    print("enums: in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
