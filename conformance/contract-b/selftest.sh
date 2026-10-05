#!/usr/bin/env bash
# Contract B conformance selftest.
#
# This proves two things about validate.py, not one:
#   1. it ACCEPTS the well-formed replay fixtures (fixtures/replay/*.jsonl);
#   2. it REFUSES, for every rule the validator knows about, a minimal
#      input built to fail that rule and no other (fixtures/refused/*.jsonl).
#
# The second half is the point. A validator that always says "ok" passes
# the first check trivially; the red cases are what prove each rule can
# actually fire. The coverage step below fails loudly if a rule and its
# case ever drift apart — in either direction — so "the suite would have
# passed vacuously" cannot happen quietly.
#
# An empty run is exit 2, never a silent pass — see case 4. "Nothing was
# validated" and "everything validated" are different outcomes and this
# kit keeps them distinguishable.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
VALIDATE="validate.py"

overall_fail=0

# Runs "$@", capturing stdout into $OUT and the exit code into $RC without
# tripping `set -e` — most of the calls below are EXPECTED to fail.
#
# `py -3` (Windows) writes CRLF line endings even on a pipe; stripped here
# so downstream string/JSON comparisons don't silently fail on a stray \r.
run() {
  set +e
  OUT="$("$@")"
  RC=$?
  set -e
  OUT="${OUT//$'\r'/}"
}

jpy() {
  # $1 = python expression using `d` (the parsed JSON); $2 = the JSON text.
  $PYTHON -c "
import json, sys
d = json.loads(sys.argv[1])
print($1)
" "$2"
}

# ---------------------------------------------------------------------------
# 1. Good run
# ---------------------------------------------------------------------------
echo "== good run =="
run $PYTHON "$VALIDATE" --now-ns 1800000000000000000 --json fixtures/replay/*.jsonl
good_rc=$RC
good_json="$OUT"

if [ "$good_rc" -ne 0 ]; then
  echo "FAIL good run: expected exit 0, got $good_rc" >&2
  echo "  $good_json" >&2
  overall_fail=1
  good_accepted=0
else
  good_summary="$(jpy "f\"{d['records']},{d['accepted']},{d['refused']},{d['replayed']},{d['assets']}\"" "$good_json")"
  if [ "$good_summary" = "34,34,0,2,6" ]; then
    echo "  ok   records=34 accepted=34 refused=0 replayed=2 assets=6"
    good_accepted="$(jpy "d['accepted']" "$good_json")"
  else
    echo "FAIL good run: expected records=34,accepted=34,refused=0,replayed=2,assets=6; got $good_summary" >&2
    overall_fail=1
    good_accepted=0
  fi
fi

# ---------------------------------------------------------------------------
# 2 & 3. Red cases + coverage
# ---------------------------------------------------------------------------
# Two rules need a non-default flag to isolate cleanly from another rule
# that the spec's own cross-checks would otherwise also trip:
#   - label_unknown_nation needs --nations (it IS the --nations feature).
#   - vocab_health_state needs --readiness optional: an out-of-vocabulary
#     health_state also fails readiness_health_conflict's exact-match
#     comparison whenever a readiness block is present, so isolating the
#     vocab rule alone means the record carries no readiness block at all.
# Every other case runs with no extra flags.
echo
echo "== red cases =="

mapfile -t rule_ids < <($PYTHON "$VALIDATE" --list-rules | tr -d '\r')
total_rules=${#rule_ids[@]}
matched=0
uncovered=0

for rule in "${rule_ids[@]}"; do
  case_file="fixtures/refused/${rule}.jsonl"
  if [ ! -f "$case_file" ]; then
    echo "FAIL rule ${rule} has no red case" >&2
    overall_fail=1
    uncovered=$((uncovered + 1))
    continue
  fi
  case "$rule" in
    label_unknown_nation) run $PYTHON "$VALIDATE" --json --nations fixtures/nations.txt "$case_file" ;;
    vocab_health_state)   run $PYTHON "$VALIDATE" --json --readiness optional "$case_file" ;;
    *)                     run $PYTHON "$VALIDATE" --json "$case_file" ;;
  esac
  if [ "$RC" -ne 1 ]; then
    echo "FAIL ${rule}: expected exit 1, got $RC" >&2
    overall_fail=1
    continue
  fi
  nonzero="$(jpy "','.join(k for k, v in d['refused_by_rule'].items() if v)" "$OUT")"
  if [ "$nonzero" = "$rule" ]; then
    echo "  ok   $rule"
    matched=$((matched + 1))
  else
    echo "FAIL ${rule}: expected exactly rule '$rule' to fire, got [$nonzero]" >&2
    overall_fail=1
  fi
done

# The other direction: a case file naming a rule the validator doesn't know.
for case_file in fixtures/refused/*.jsonl; do
  name="$(basename "$case_file" .jsonl)"
  known=0
  for rule in "${rule_ids[@]}"; do
    [ "$rule" = "$name" ] && known=1 && break
  done
  if [ "$known" -eq 0 ]; then
    echo "FAIL case file ${name} does not name a real rule" >&2
    overall_fail=1
  fi
done

# ---------------------------------------------------------------------------
# 4. Empty input
# ---------------------------------------------------------------------------
echo
echo "== empty input =="
run $PYTHON "$VALIDATE" < /dev/null
if [ "$RC" -eq 2 ]; then
  echo "  ok   empty input exits 2"
else
  echo "FAIL empty input: expected exit 2, got $RC" >&2
  echo "  $OUT" >&2
  overall_fail=1
fi

# ---------------------------------------------------------------------------
# 5. Enum sync (opt-in: only when a proto tree is pointed at)
# ---------------------------------------------------------------------------
echo
echo "== enum sync =="
enums_status="skipped"
if [ -n "${OPENDDIL_PROTO_DIR:-}" ]; then
  run $PYTHON "$VALIDATE" --check-enums "$OPENDDIL_PROTO_DIR"
  if [ "$RC" -eq 0 ]; then
    echo "  ok   $OUT"
    enums_status="in sync"
  else
    echo "FAIL enum check: expected exit 0, got $RC" >&2
    echo "  $OUT" >&2
    overall_fail=1
    enums_status="drift"
  fi
else
  echo "  SKIPPED (OPENDDIL_PROTO_DIR not set)"
fi

# ---------------------------------------------------------------------------
# 6. Final summary line — always printed.
# ---------------------------------------------------------------------------
echo
echo "contract-b selftest: good=${good_accepted} accepted, red cases=${matched}/${total_rules} matched, rules uncovered=${uncovered}, enums=${enums_status}"

exit $overall_fail
