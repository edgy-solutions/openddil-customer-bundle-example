#!/usr/bin/env bash
# Golden-file runner for the overlay's ingress mappings.
#
#   ./run.sh            # verify every case against its golden
#   ./run.sh --bless    # regenerate goldens (review the diff before committing)
#
# Each case is a directory under cases/ holding:
#   input.json      one source message, as the producer would emit it
#   expected.json   the Silver output the mapping must produce
#   README          why this case exists — what it pins down
#
# The binary is fetched once into .bin/ and cached. Nothing else is needed:
# no broker, no daemon, no Docker.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OVERLAY="$(cd "$HERE/../.." && pwd)"
MAPPING="$OVERLAY/dynamic-mappings/sample-sensor-mapping.yaml"
BIN_DIR="$HERE/.bin"
CONNECT="$BIN_DIR/redpanda-connect"
VERSION="${CONNECT_VERSION:-4.91.0}"   # pinned; matches the chart's connect image

CONFORMANCE="$HERE/conformance.yaml"

BLESS=0
ALLOW_MISSING_PROTO=0
for arg in "$@"; do
  case "$arg" in
    --bless)                BLESS=1 ;;
    --allow-missing-proto)  ALLOW_MISSING_PROTO=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
# Where the proto tree comes from
# ---------------------------------------------------------------------------
# The conformance stage below encodes each case's output against the
# canonical messages, which means it needs the .proto tree that
# openddil-contracts owns. This overlay does not vendor a copy, deliberately:
# a vendored tree is a snapshot that drifts, and a conformance check run
# against a stale contract is worse than no check because it reports green
# for a shape the real ingress would reject.
#
# Resolution order, most explicit first. Whichever wins is PRINTED, because
# "which contract did this pass against?" is the first question a failure
# raises and the answer must not have to be reconstructed.
resolve_proto_dir() {
  local marker="openddil/telemetry/v1/telemetry.proto" c
  if [ -n "${OPENDDIL_PROTO_DIR:-}" ]; then
    if [ -f "$OPENDDIL_PROTO_DIR/$marker" ]; then
      PROTO_DIR="$OPENDDIL_PROTO_DIR"; PROTO_SRC="OPENDDIL_PROTO_DIR"; return 0
    fi
    # Set but wrong is a different failure from unset, and saying so saves
    # the next person from re-deriving it. Do not fall through silently.
    PROTO_WHY="OPENDDIL_PROTO_DIR=$OPENDDIL_PROTO_DIR is set but has no $marker"
    return 1
  fi
  for c in "$OVERLAY/../openddil-contracts/proto" "$OVERLAY/../../openddil-contracts/proto"; do
    if [ -f "$c/$marker" ]; then
      PROTO_DIR="$(cd "$c" && pwd)"; PROTO_SRC="sibling checkout"; return 0
    fi
  done
  PROTO_WHY="no \$OPENDDIL_PROTO_DIR, and no sibling openddil-contracts/proto beside this overlay"
  return 1
}


fetch_binary() {
  [ -x "$CONNECT" ] && return 0
  mkdir -p "$BIN_DIR"
  local os arch url
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "unsupported arch: $(uname -m)" >&2; return 1 ;;
  esac
  url="https://github.com/redpanda-data/connect/releases/download/v${VERSION}/redpanda-connect_${VERSION}_${os}_${arch}.tar.gz"
  echo "fetching redpanda-connect ${VERSION} (${os}/${arch})"
  curl -fsSL "$url" | tar -xz -C "$BIN_DIR" redpanda-connect || {
    echo "fetch failed: $url" >&2; return 1; }
  chmod +x "$CONNECT"
}

fetch_binary || exit 1

# `connect run` is chatty on stderr and emits lifecycle lines; only stdout
# carries the mapped document.
run_case() {
  "$CONNECT" run -r "$MAPPING" "$HERE/harness.yaml" \
    < "$1" 2>/dev/null | head -1
}

pass=0; fail=0; blessed=0
for dir in "$HERE"/cases/*/; do
  [ -d "$dir" ] || continue
  name="$(basename "$dir")"
  input="$dir/input.json"
  golden="$dir/expected.json"
  [ -f "$input" ] || { echo "  SKIP $name (no input.json)"; continue; }

  actual="$(run_case "$input")"
  # Kept for the conformance stage: that stage must encode exactly what the
  # mapping emitted, NOT the key-sorted normalisation used for diffing.
  # Normalisation is a diffing convenience; the encoder sees the raw line.
  printf '%s
' "$actual" > "$WORK/$name.json"
  if [ -z "$actual" ]; then
    echo "  FAIL $name — mapping produced no output"
    fail=$((fail + 1)); continue
  fi

  # Normalise key order so a diff reports semantic change, not serialisation
  # order. Without this the suite fails on a Connect upgrade that reorders
  # keys, which is noise dressed as a regression.
  norm_actual="$(printf '%s' "$actual" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True, indent=2))')"

  if [ "$BLESS" -eq 1 ]; then
    printf '%s\n' "$norm_actual" > "$golden"
    echo "  BLESS $name"; blessed=$((blessed + 1)); continue
  fi

  if [ ! -f "$golden" ]; then
    echo "  FAIL $name — no expected.json (run --bless, then REVIEW it)"
    fail=$((fail + 1)); continue
  fi

  norm_golden="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True, indent=2))' "$golden")"

  if [ "$norm_actual" = "$norm_golden" ]; then
    echo "  ok   $name"; pass=$((pass + 1))
  else
    echo "  FAIL $name"
    diff <(printf '%s\n' "$norm_golden") <(printf '%s\n' "$norm_actual") \
      | sed 's/^/        /' | head -20
    fail=$((fail + 1))
  fi
done

echo
if [ "$BLESS" -eq 1 ]; then
  echo "blessed $blessed case(s) — REVIEW the diff before committing."
  echo "A golden accepted without reading it records current behaviour as if"
  echo "it were intent, which is the opposite of what these tests are for."
  exit 0
fi
# ---------------------------------------------------------------------------
# Proto-conformance stage — ADR-0029 Phase 0's acceptance test
# ---------------------------------------------------------------------------
# Encodes each case's mapping output through the same `protobuf from_json`
# step the real ingress uses. A golden file says what the mapping PRODUCES;
# this says whether the consumer can ACCEPT it. Those failed apart once
# already — see the note in conformance.yaml.
echo
cpass=0; cfail=0
if ! resolve_proto_dir; then
  if [ "$ALLOW_MISSING_PROTO" -eq 1 ]; then
    # An honest skip names its reason and is visible in the summary line.
    # It is opt-in precisely so that it cannot become the quiet default:
    # a conformance stage that skips by itself is indistinguishable from
    # one that passes, which is the defect this whole stage exists to stop.
    echo "conformance SKIPPED — $PROTO_WHY"
    echo "  (--allow-missing-proto was passed; the encode seam is UNCHECKED"
    echo "   in this run. Goldens above prove mapping output, nothing more.)"
    echo
    echo "$pass passed, $fail failed, conformance skipped"
    [ "$fail" -eq 0 ]; exit $?
  fi
  echo "conformance FAILED to run — $PROTO_WHY" >&2
  echo "  Point OPENDDIL_PROTO_DIR at openddil-contracts/proto, or pass" >&2
  echo "  --allow-missing-proto to run the goldens alone and SAY SO." >&2
  exit 1
fi

# NOT prefixed "conformance:" — the summary line at the end owns that prefix
# and CI parses it. Two lines sharing a prefix made the first assertion
# written against this output read the wrong one.
echo "conformance proto tree: $PROTO_DIR ($PROTO_SRC)"
export CONFORMANCE_PROTO_DIR="$PROTO_DIR"
for dir in "$HERE"/cases/*/; do
  [ -d "$dir" ] || continue
  name="$(basename "$dir")"
  [ -s "$WORK/$name.json" ] || continue          # no output: already FAILed above

  # Per-case message override, for cases whose mapping targets something
  # other than the telemetry event. Declared in the case, not inferred.
  if [ -f "$dir/message" ]; then
    CONFORMANCE_MESSAGE="$(tr -d ' 	
' < "$dir/message")"
  else
    CONFORMANCE_MESSAGE="openddil.telemetry.v1.EntityTelemetryEvent"
  fi
  export CONFORMANCE_MESSAGE

  verdict="$("$CONNECT" run "$CONFORMANCE" < "$WORK/$name.json" 2>/dev/null | head -1)"
  case "$verdict" in
    CONFORMANCE_OK)
      echo "  ok   $name → $CONFORMANCE_MESSAGE"; cpass=$((cpass + 1)) ;;
    CONFORMANCE_FAIL*)
      echo "  FAIL $name → $CONFORMANCE_MESSAGE"
      echo "        ${verdict#CONFORMANCE_FAIL }"
      cfail=$((cfail + 1)) ;;
    *)
      # Neither token means the harness itself did not run — a missing
      # binary, a config error, a broken proto tree. That is NOT a passing
      # case and must not be counted as one.
      echo "  FAIL $name — conformance harness produced no verdict"
      echo "        got: ${verdict:-<nothing>}"
      cfail=$((cfail + 1)) ;;
  esac
done

echo
echo "goldens:    $pass passed, $fail failed"
echo "conformance: $cpass passed, $cfail failed"
[ "$fail" -eq 0 ] && [ "$cfail" -eq 0 ]
