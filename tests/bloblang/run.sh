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

BLESS=0
[ "${1:-}" = "--bless" ] && BLESS=1

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
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
