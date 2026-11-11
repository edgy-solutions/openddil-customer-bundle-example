# Shared functions for the openddil git hooks. Sourced by the hooks; not run directly.
# POSIX sh. Holds no vocabulary: every private term comes from the user's own file inside .git.

# Awk fragment that loads the terms file named in $HK_TF (may be unset or empty).
# term[i] is lower-cased; num[i] is the term's line number in the file.
HK_AWK_TERMS='
BEGIN {
  n = 0; tf = ENVIRON["HK_TF"]; label = ENVIRON["HK_LABEL"]; pfx = ENVIRON["HK_PFX"]
  if (tf != "") {
    k = 0
    while ((getline l < tf) > 0) {
      k++; sub(/\r$/, "", l); t = l
      sub(/^[ \t]+/, "", t); sub(/[ \t]+$/, "", t)
      if (t == "" || substr(t, 1, 1) == "#") continue
      n++; term[n] = tolower(t); num[n] = k
    }
    close(tf)
  }
}
'

# Awk fragment: scan the added lines of a unified diff (-U0) on stdin.
HK_AWK_DIFF='
function showpath(p,   i, lp) {
  lp = tolower(p)
  for (i = 1; i <= n; i++) if (index(lp, term[i])) return "(path holding a term)"
  return p
}
function hit(chk, extra) {
  print label ": " chk ": " pfx showpath(path) ":" ln extra
}
function chk(line,   i, ll) {
  if (line ~ secre) hit("secret-manifest", "")
  if (line ~ pkre) hit("private-key", "")
  if (line ~ keyre) hit("kubeconfig-key", "")
  if (n > 0) {
    ll = tolower(line)
    for (i = 1; i <= n; i++) if (index(ll, term[i])) hit("private-term", ": term #" num[i])
  }
}
BEGIN {
  q = "\047"
  secre = "^[ \t]*kind:[ \t]*[\"" q "]?Secret[\"" q "]?[ \t\r]*$"
  pkre = "-----BEGIN [A-Z ]*PRIVATE" " KEY-----"
  keyre = "^[ \t]*(client-key-data|client-certificate-data):[ \t]*[^ \t\r]"
  remaining = 0; path = ""; ln = 0
}
remaining > 0 && substr($0, 1, 1) == "+" { chk(substr($0, 2)); ln++; remaining--; next }
/^@@ / {
  s = $0; sub(/^@@ [^+]*\+/, "", s)
  ln = s + 0
  if (s ~ /^[0-9]+,/) { t = s; sub(/^[0-9]+,/, "", t); remaining = t + 0 } else remaining = 1
  next
}
/^\+\+\+ / {
  path = substr($0, 7); sub(/\t$/, "", path)
  if ($0 == "+++ /dev/null") path = ""
  next
}
'

# Awk fragment: scan plain lines on stdin (paths or a commit message) for terms.
# HK_KIND is "path" or "msg".
HK_AWK_TEXT='
BEGIN { kind = ENVIRON["HK_KIND"] }
{
  if (n == 0) next
  ll = tolower($0)
  for (i = 1; i <= n; i++) if (index(ll, term[i])) {
    if (kind == "path") print label ": private-term: " pfx "changed path entry " NR ": term #" num[i]
    else print label ": private-term: " pfx "message line " NR ": term #" num[i]
  }
}
'

hk_err() { printf '%s\n' "$*" >&2; }

# hk_init HOOKNAME: reads the mode, sets HK_NAME, HK_MODE, HK_TF (terms file, public only).
hk_init() {
  HK_NAME=$1
  hk_modefile=$(git rev-parse --git-path info/openddil-hooks-mode) || exit 1
  HK_MODE=
  if [ -f "$hk_modefile" ]; then
    HK_MODE=$(head -n 1 "$hk_modefile" | tr -d '\r \t')
  fi
  case "$HK_MODE" in
    public|private) ;;
    *)
      hk_err "$HK_NAME: refused: the hooks mode is missing or unknown."
      hk_err "Run tools/hooks/install-hooks.sh --mode public|private REPO to set it."
      exit 1
      ;;
  esac
  HK_TF=
  if [ "$HK_MODE" = public ]; then
    HK_TF=$(git rev-parse --git-path info/private-terms) || exit 1
    if [ ! -f "$HK_TF" ]; then
      hk_err "$HK_NAME: refused: public mode needs the file $HK_TF and it does not exist."
      hk_err "Create it with one private term per line (see tools/hooks/README.md), or re-run install-hooks.sh."
      exit 1
    fi
  fi
}

# hk_scan_diff PREFIX: added lines of a diff on stdin; prints one line per finding.
hk_scan_diff() {
  HK_TF=$HK_TF HK_LABEL=$HK_NAME HK_PFX=$1 awk "$HK_AWK_TERMS$HK_AWK_DIFF"
}

# hk_scan_text KIND PREFIX: plain lines on stdin (public mode only).
hk_scan_text() {
  [ "$HK_MODE" = public ] || return 0
  HK_TF=$HK_TF HK_LABEL=$HK_NAME HK_KIND=$1 HK_PFX=$2 awk "$HK_AWK_TERMS$HK_AWK_TEXT"
}

# hk_gitdiff: git diff with the options the scanner expects.
hk_gitdiff() {
  git -c core.quotepath=off diff --no-color --no-ext-diff --src-prefix=a/ --dst-prefix=b/ -U0 "$@"
}

# hk_finish OUTPUT FIXLINE: print findings and exit 1, or return when there are none.
hk_finish() {
  if [ -n "$1" ]; then
    printf '%s\n' "$1" >&2
    hk_err "$2"
    exit 1
  fi
}
