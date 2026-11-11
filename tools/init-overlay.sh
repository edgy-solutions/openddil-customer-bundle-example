#!/usr/bin/env bash
# Start a private overlay from the public example's skeleton.
#
#   tools/init-overlay.sh [--policy-id ID] [--name NAME] TARGET
#
# Copies tools/init-overlay/template/ into TARGET (which must not exist or must be empty),
# fills the @NAME@ placeholders, runs git init, installs the hooks in private mode, and
# makes one initial commit. The overlay's CI pins this repo's commit: HEAD in a clone, or the SHA in
# tools/init-overlay/bundle.ref when tools/ was copied out of one (the starter kit does that).
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
EXAMPLE="$(cd "$HERE/.." && pwd)"
TEMPLATE="$HERE/init-overlay/template"
policy_id=example-policy
name=
target=

usage() { echo "usage: init-overlay.sh [--policy-id ID] [--name NAME] TARGET" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --policy-id) [ $# -ge 2 ] || usage; policy_id=$2; shift 2 ;;
    --name) [ $# -ge 2 ] || usage; name=$2; shift 2 ;;
    -h|--help) usage ;;
    -*) usage ;;
    *) [ -z "$target" ] || usage; target=$1; shift ;;
  esac
done
[ -n "$target" ] || usage
[ -n "$name" ] || name="$(basename "$target")"

if [ -e "$target" ] && { [ ! -d "$target" ] || [ -n "$(ls -A "$target")" ]; }; then
  echo "init-overlay: refused: $target exists and is not an empty directory." >&2
  exit 1
fi
[ -d "$TEMPLATE" ] || { echo "init-overlay: the template is missing: $TEMPLATE" >&2; exit 2; }
[ -f "$HERE/init-overlay/pins.env" ] || { echo "init-overlay: pins.env is missing" >&2; exit 2; }

if git -C "$EXAMPLE" rev-parse HEAD >/dev/null 2>&1; then
  bundle_sha="$(git -C "$EXAMPLE" rev-parse HEAD)"
  if [ -z "$(git -C "$EXAMPLE" branch -r --contains "$bundle_sha" 2>/dev/null)" ]; then
    echo "init-overlay: warning: the example's HEAD is on no remote branch; the CI pin will not resolve until it is pushed." >&2
  fi
  if [ -n "$(git -C "$EXAMPLE" status --porcelain 2>/dev/null)" ]; then
    echo "init-overlay: warning: the example's working tree is dirty; the pin names HEAD, not your changes." >&2
  fi
elif [ -f "$HERE/init-overlay/bundle.ref" ]; then
  # A copy outside a clone (a kit) names the commit it was copied from.
  bundle_sha="$(tr -d ' \r\n' < "$HERE/init-overlay/bundle.ref")"
  if ! printf '%s' "$bundle_sha" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "init-overlay: refused: init-overlay/bundle.ref must hold one full commit SHA." >&2
    exit 2
  fi
else
  echo "init-overlay: the example at $EXAMPLE is not a git clone and has no init-overlay/bundle.ref, so there is no SHA to pin." >&2
  exit 2
fi

mkdir -p "$target"
# tar keeps the dotfiles and the modes.
(cd "$TEMPLATE" && tar -cf - --exclude='__pycache__' .) | (cd "$target" && tar -xf -)

# pins.env is KEY=VALUE, comments and blanks ignored.
subst_args=()
add_subst() { subst_args+=(-e "s|@$1@|$(printf '%s' "$2" | sed 's/[|&\\]/\\&/g')|g"); }
add_subst POLICY_ID "$policy_id"
add_subst NAME "$name"
add_subst BUNDLE_SHA "$bundle_sha"
while IFS='=' read -r key value; do
  case "$key" in ''|'#'*) continue ;; esac
  value="${value%$'\r'}"
  add_subst "$key" "$value"
done < "$HERE/init-overlay/pins.env"

(cd "$target" && find . -type f -print0 | xargs -0 sed -i "${subst_args[@]}")

left="$(cd "$target" && grep -rEl '@[A-Z_]+@' . || true)"
if [ -n "$left" ]; then
  echo "init-overlay: refused: an @NAME@ placeholder remains in: $(echo "$left" | tr '\n' ' ')" >&2
  exit 1
fi

git -C "$target" init -q -b main
sh "$EXAMPLE/tools/hooks/install-hooks.sh" --mode private "$target"

files=()
while IFS= read -r -d '' f; do files+=("${f#./}"); done < <(cd "$TEMPLATE" && find . -type f -not -path '*/__pycache__/*' -print0 | sort -z)
(cd "$target" && git add -- "${files[@]}")
short="$(printf '%s' "$bundle_sha" | cut -c1-7)"
git -C "$target" commit -q -m "Initialise the overlay skeleton from openddil-customer-bundle-example $short."

echo
echo "Overlay created at $target (one commit, hooks in private mode)."
echo "Next:"
echo "  1. list your private remote in $target/.git/info/allowed-remotes"
echo "  2. git -C $target remote add origin <your private remote>"
echo "  3. cd $target, copy .env.example to .env and fill it, then run: bash checks.sh"
