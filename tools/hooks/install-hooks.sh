#!/bin/sh
# Install the openddil git hooks into a repository.
# Usage: install-hooks.sh --mode public|private [--force] REPO
set -eu

usage() {
  echo "usage: install-hooks.sh --mode public|private [--force] REPO" >&2
  exit 2
}

mode=
force=0
repo=
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) [ $# -ge 2 ] || usage; mode=$2; shift 2 ;;
    --force) force=1; shift ;;
    -h|--help) usage ;;
    -*) usage ;;
    *) [ -z "$repo" ] || usage; repo=$1; shift ;;
  esac
done
case "$mode" in public|private) ;; *) usage ;; esac
[ -n "$repo" ] || usage

src=$(cd "$(dirname "$0")" && pwd)

git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || { echo "install-hooks: $repo is not a git repository." >&2; exit 1; }

# Resolve a git-path (hooks, info/<file>) to a usable path, whether git prints it relative or absolute.
gpath() {
  p=$(git -C "$repo" rev-parse --git-path "$1")
  case "$p" in
    /*|[A-Za-z]:*) printf '%s\n' "$p" ;;
    *) printf '%s\n' "$repo/$p" ;;
  esac
}

if git -C "$repo" config --get core.hooksPath >/dev/null 2>&1; then
  echo "install-hooks: refused: core.hooksPath is set for this repository, so hooks in .git/hooks would be ignored." >&2
  echo "Unset core.hooksPath (or install the hooks into the directory it names by hand), then run this again." >&2
  exit 1
fi

hooks=$(gpath hooks)
info=$(gpath info)
mkdir -p "$hooks" "$info"

if [ "$force" -ne 1 ]; then
  clash=
  for h in pre-commit commit-msg pre-push; do
    d=$hooks/$h
    case "$d" in *.sample) continue ;; esac
    if [ -e "$d" ] && ! cmp -s "$src/$h" "$d"; then
      clash="$clash $h"
    fi
  done
  if [ -n "$clash" ]; then
    echo "install-hooks: refused: these hooks already exist and differ from ours:$clash" >&2
    echo "Look at them, then re-run with --force to replace them." >&2
    exit 1
  fi
fi

for h in pre-commit commit-msg pre-push lib.sh; do
  cp "$src/$h" "$hooks/$h"
  chmod +x "$hooks/$h"
done

printf '%s\n' "$mode" > "$info/openddil-hooks-mode"

if [ "$mode" = public ]; then
  if [ ! -f "$info/private-terms" ]; then
    {
      echo "# One private term per line: your program's names, data names and model names."
      echo "# Matching is case-insensitive and fixed-string. Blank lines and # lines are ignored."
      echo "# This file lives inside .git and is never committed. Keep it current."
    } > "$info/private-terms"
  fi
else
  if [ ! -f "$info/allowed-remotes" ]; then
    {
      echo "# One remote URL per line: the private remote(s) this repository may be pushed to."
      echo "# Blank lines and # lines are ignored. An empty list means no push."
      echo "# This file lives inside .git and is never committed."
    } > "$info/allowed-remotes"
  fi
fi

echo "Installed pre-commit, commit-msg, pre-push and lib.sh into $hooks"
echo "Mode: $mode (recorded in $info/openddil-hooks-mode)"
if [ "$mode" = public ]; then
  echo "Next: fill $info/private-terms with your private terms, one per line."
else
  echo "Next: list the allowed remote URL(s) in $info/allowed-remotes."
fi
