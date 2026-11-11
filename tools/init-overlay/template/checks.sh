#!/usr/bin/env bash
# Runs the ten overlay checks from this overlay's root.
#
# The checkers live in the public openddil-customer-bundle-example and run here against
# this overlay's files. Each check writes out/checks/<name>.log and prints one line,
# `overlay-checks: <name> PASS|FAIL`. A missing tool or sibling checkout is a FAIL that
# names it, never a skip. The last line is `overlay-checks: ran=N pass=P fail=F`, and the
# script exits 0 only when every check passed.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 2
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PY="${PYTHON:-python}"
export PYTHON="$PY"
LOGS="$ROOT/out/checks"
mkdir -p "$LOGS"

# Sibling lookup, in the order the example's conformance/siblings.py uses: OPENDDIL_REPOS,
# then an openddil/ folder beside this repo, then the folder holding it.
sib() {
  local r
  for r in "${OPENDDIL_REPOS:-}" "$ROOT/../openddil" "$ROOT/.."; do
    if [ -n "$r" ] && [ -e "$r/$1" ]; then (cd "$r/$1" && pwd); return 0; fi
  done
  return 1
}

EXAMPLE="${OPENDDIL_BUNDLE_EXAMPLE:-}"
[ -n "$EXAMPLE" ] || EXAMPLE="$(sib openddil-customer-bundle-example || true)"
HELM_CHART="$(sib openddil-helm/openddil-demo || true)"
PROTO_PY="${OPENDDIL_PROTO_PY:-}"
[ -n "$PROTO_PY" ] || PROTO_PY="$(sib openddil-contracts/gen/python || true)"
export OPENDDIL_PROTO_PY="$PROTO_PY"
if [ -z "${ALGORITHM_RUNTIME:-}" ]; then
  AR="$(sib openddil-tactical-agents/edge || true)"
  [ -n "$AR" ] && export ALGORITHM_RUNTIME="$AR"
fi

# The values chain: one path per line in values/chain.txt, blanks and # lines ignored.
CHAIN=()
if [ -f values/chain.txt ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) ;; *) CHAIN+=("$line") ;; esac
  done < values/chain.txt
fi

PASS=0; FAIL=0

# Each check function runs with its stdout and stderr in the log and returns 0 on a pass.
run_check() {
  local name="$1"; shift
  if "$@" >"$LOGS/$name.log" 2>&1; then
    echo "overlay-checks: $name PASS"; PASS=$((PASS+1))
  else
    echo "overlay-checks: $name FAIL (see out/checks/$name.log)"; FAIL=$((FAIL+1))
  fi
}

need_example() {
  [ -n "$EXAMPLE" ] && [ -d "$EXAMPLE" ] && return 0
  echo "missing prerequisite: the openddil-customer-bundle-example checkout (set OPENDDIL_BUNDLE_EXAMPLE or OPENDDIL_REPOS)" >&2
  return 1
}
need_chain() {
  [ "${#CHAIN[@]}" -gt 0 ] && return 0
  echo "values/chain.txt lists no files" >&2
  return 1
}

c_manifest()     { need_example && "$PY" "$EXAMPLE/conformance/overlay/check_manifest.py" --root . --min-present 8; }
c_dimensions()   { need_example && "$PY" "$EXAMPLE/conformance/overlay/check_dimensions.py" --root .; }
c_declarations() { need_example && need_chain && "$PY" "$EXAMPLE/conformance/overlay/check_declarations.py" --root . --values "${CHAIN[@]}"; }
c_enumeration()  { need_example && "$PY" "$EXAMPLE/tools/dis-sim/enumeration/validate_overlay.py" enumeration/overlay.json; }
c_kinds()        { need_example && need_chain && "$PY" "$EXAMPLE/conformance/overlay/check_kinds.py" --root . --values "${CHAIN[@]}"; }

c_values_chain() {
  need_chain || return 1
  if [ -z "$HELM_CHART" ]; then echo "missing prerequisite: the openddil-helm checkout (openddil-helm/openddil-demo)" >&2; return 1; fi
  command -v helm >/dev/null 2>&1 || { echo "missing prerequisite: helm on PATH" >&2; return 1; }
  local args=() f bytes
  for f in "${CHAIN[@]}"; do args+=(-f "$f"); done
  # The render can hold Secrets, so it is counted in the pipe and never written to disk.
  set -o pipefail
  bytes="$(helm template openddil "$HELM_CHART" -n openddil "${args[@]}" | wc -c)" || { echo "helm template failed" >&2; return 1; }
  bytes="${bytes//[[:space:]]/}"
  echo "render bytes: $bytes"
  [ "$bytes" -gt 0 ]
}

c_contract_b() {
  need_example || return 1
  mkdir -p out
  PYTHON="$PY" bash "$EXAMPLE/conformance/contract-b/selftest.sh" || return 1
  "$PY" "$EXAMPLE/conformance/overlay/check_dimensions.py" --root . --print-nations > out/nations.txt || return 1
  "$PY" "$EXAMPLE/conformance/contract-b/validate.py" --nations out/nations.txt contract-b/replay.jsonl
}

c_egress_twin() {
  need_example && need_chain || return 1
  local joined out="$LOGS/egress-twin.pytest" summary
  joined="$(IFS=:; echo "${CHAIN[*]}")"
  OVERLAY_ROOT="$PWD" OVERLAY_VALUES="$joined" "$PY" -m pytest "$EXAMPLE/conformance/egress-twin" -rs -q -p no:cacheprovider >"$out" 2>&1
  local rc=$?
  cat "$out"
  [ "$rc" -eq 0 ] || return 1
  summary="$(tail -n 1 "$out")"
  if echo "$summary" | grep -Eq '[0-9]+ (skipped|failed|error)'; then echo "the twin skipped, failed or errored" >&2; return 1; fi
  echo "$summary" | grep -Eq '^[0-9]+ passed' || { echo "the twin passed no tests" >&2; return 1; }
}

c_kindcheck() {
  need_example && need_chain || return 1
  mkdir -p out/kindcheck/kinds
  rm -f out/kindcheck/kinds/*.json
  # kindcheck takes only the kinds the assembler config routes. That list is derived here
  # from the merged values (maps merge deep, lists replace). A kind no assembler routes,
  # such as the intake-side RepairOrder, is covered by the kinds check instead. An empty
  # list is a FAIL.
  EXAMPLE="$EXAMPLE" "$PY" - "${CHAIN[@]}" <<'PYEOF' || return 1
import json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(os.environ["EXAMPLE"]) / "conformance" / "overlay"))
from common import merged_values
v = merged_values(sys.argv[1:])
config = (((v.get("egress") or {}).get("assembler") or {}).get("config")) or []
routed = sorted({c["kind"] for c in config if isinstance(c, dict) and c.get("kind")})
if not routed:
    sys.exit("the merged values route no assembler kind, so kindcheck has nothing to check")
out = pathlib.Path("out/kindcheck")
(out / "assembler-config.json").write_text(json.dumps(config, indent=2))
for kind in routed:
    src = pathlib.Path("kinds") / f"{kind}.schema.json"
    if not src.is_file():
        sys.exit(f"a routed kind has no kinds/ schema: {kind}")
    (out / "kinds" / src.name).write_text(src.read_text())
print("routed kinds:", len(routed))
PYEOF
  local extra=()
  [ -n "$PROTO_PY" ] && extra+=(--contracts-src "$PROTO_PY")
  "$PY" "$EXAMPLE/conformance/kindcheck/kindcheck.py" \
    --assembler-config out/kindcheck/assembler-config.json --schemas out/kindcheck/kinds \
    --cm-state kindcheck/cm-state.json --asset kindcheck/asset.json \
    --now 2026-11-07T12:00:00Z "${extra[@]}"
}

c_algorithms() {
  need_example || return 1
  local out="$LOGS/algorithms.pytest" summary rc d n=0
  "$PY" -m pytest algorithms/tests -rs -q -p no:cacheprovider >"$out" 2>&1
  rc=$?
  cat "$out"
  [ "$rc" -eq 0 ] || return 1
  summary="$(tail -n 1 "$out")"
  if echo "$summary" | grep -Eq '[0-9]+ (skipped|failed|error)'; then echo "the unit tests skipped, failed or errored" >&2; return 1; fi
  echo "$summary" | grep -Eq '^[0-9]+ passed' || { echo "no unit test passed" >&2; return 1; }
  for d in algorithms/*/; do
    [ -f "${d}unit.yaml" ] || continue
    UNIT_DIR="$d" "$PY" - <<'PYEOF' || return 1
import os, sys
rt = os.environ.get("ALGORITHM_RUNTIME")
if not rt:
    sys.exit("missing prerequisite: ALGORITHM_RUNTIME (openddil-tactical-agents edge/)")
sys.path.insert(0, rt)
if os.environ.get("OPENDDIL_PROTO_PY"):
    sys.path.insert(0, os.environ["OPENDDIL_PROTO_PY"])
from algorithm_runtime import guard
guard.check_unit_dir(os.environ["UNIT_DIR"])
print("guard ok:", os.path.basename(os.environ["UNIT_DIR"].rstrip("/")))
PYEOF
    n=$((n+1))
  done
  [ "$n" -gt 0 ] || { echo "no algorithms/*/unit.yaml found" >&2; return 1; }
}

run_check manifest     c_manifest
run_check dimensions   c_dimensions
run_check declarations c_declarations
run_check enumeration  c_enumeration
run_check kinds        c_kinds
run_check values-chain c_values_chain
run_check contract-b   c_contract_b
run_check egress-twin  c_egress_twin
run_check kindcheck    c_kindcheck
run_check algorithms   c_algorithms

echo "overlay-checks: ran=10 pass=$PASS fail=$FAIL"
[ "$PASS" -eq 10 ]
