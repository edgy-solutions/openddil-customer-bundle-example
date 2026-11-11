#!/bin/bash
# Plants each refused value in throwaway repos and asserts each outcome.
# Secret-looking lines are built from pieces so this file passes the hooks it tests.
set -u

here=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

# Isolate from the user's git configuration.
: > "$T/gitconfig"
export GIT_CONFIG_GLOBAL="$T/gitconfig" GIT_CONFIG_NOSYSTEM=1

TERM1=zorblat-alpha
pass=0
fail=0
OUT=

ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $1"; }
is() { if [ "$2" = "$3" ]; then ok; else bad "$1 (wanted $2, got $3)"; fi; }
has() { case "$OUT" in *"$2"*) ok ;; *) bad "$1 (output lacks: $2)" ;; esac; }
lacks() { lo=$(printf '%s' "$OUT" | tr 'A-Z' 'a-z'); w=$(printf '%s' "$2" | tr 'A-Z' 'a-z'); case "$lo" in *"$w"*) bad "$1 (output holds a forbidden string)" ;; *) ok ;; esac; }

n=0
mkrepo() { # mkrepo MODE [noinstall] -> sets R
  n=$((n + 1)); R="$T/r$n"
  git init -q "$R"
  git -C "$R" config user.name tester
  git -C "$R" config user.email tester@example.invalid
  git -C "$R" config commit.gpgsign false
  if [ "${2:-}" != noinstall ]; then
    sh "$here/install-hooks.sh" --mode "$1" "$R" >/dev/null || echo "install failed in mkrepo"
  fi
}
infof() { p=$(git -C "$R" rev-parse --git-path "info/$1"); case "$p" in /*|[A-Za-z]:*) echo "$p" ;; *) echo "$R/$p" ;; esac; }
terms() { printf '# comment\n\n%s\n' "$TERM1" > "$(infof private-terms)"; }
put() { printf '%s\n' "$2" > "$R/$1"; git -C "$R" add -- "$1"; }
commit() { OUT=$(git -C "$R" commit -q -m "${1:-change}" 2>&1); RC=$?; }
bare() { git init -q --bare "$T/b$n.git"; git -C "$R" remote add origin "$T/b$n.git"; }
push() { OUT=$(git -C "$R" push -q origin HEAD:refs/heads/main 2>&1); RC=$?; }

secret_line() { printf '%s%s\n' 'kind:' ' Secret'; }
key_line() { printf '%s%s\n' '-----BEGIN RSA PRIV' 'ATE KEY-----'; }
ckd_line() { printf '%s%s\n' 'client-key-' 'data: QUJDRA=='; }

for m in public private; do
  # 1 clean commit passes
  mkrepo $m; [ $m = public ] && terms
  put a.txt "hello"; commit; is "1 clean commit passes ($m)" 0 $RC
  # 2 Secret
  put s.yaml "$(secret_line)"; commit; is "2 Secret refused ($m)" 1 $RC; has "2 names check ($m)" secret-manifest
  git -C "$R" rm -q --cached s.yaml; rm -f "$R/s.yaml"
  # 3 private key
  put k.txt "$(key_line)"; commit; is "3 private key refused ($m)" 1 $RC
  git -C "$R" rm -q --cached k.txt; rm -f "$R/k.txt"
  # 4 client-key-data
  put c.yaml "$(ckd_line)"; commit; is "4 client-key-data refused ($m)" 1 $RC
  git -C "$R" rm -q --cached c.yaml; rm -f "$R/c.yaml"
done

# 5 public: content, path, message
mkrepo public; terms
put a.txt "Intro text about Zorblat-Alpha systems"; commit; is "5 term in content refused" 1 $RC
has "5 output has term #" "term #"; has "5 output has the line number" "a.txt:1"
lacks "5 output hides the term" "$TERM1"
git -C "$R" rm -q --cached a.txt; rm -f "$R/a.txt"
put "zorblat-alpha-notes.txt" "plain"; commit; is "5 term in path refused" 1 $RC
has "5 path output has term #" "term #"; lacks "5 path output hides the term" "$TERM1"
git -C "$R" rm -q --cached "zorblat-alpha-notes.txt"; rm -f "$R/zorblat-alpha-notes.txt"
put a.txt "plain"; commit "mentions ZORBLAT-ALPHA here"; is "5 term in message refused" 1 $RC
has "5 message output has term #" "term #"; has "5 message line named" "message line 1"
lacks "5 message output hides the term" "$TERM1"

# 6 public: removing a line holding a term passes
mkrepo public
put a.txt "keep"; commit; is "6 setup commit" 0 $RC
printf '%s\n' "keep" "gone $TERM1 gone" > "$R/a.txt"
git -C "$R" add a.txt
# plant the line without the hooks (terms file not yet written), then add the terms
rm -f "$(infof private-terms)"; : > "$(infof private-terms)"
commit "add line"; is "6 setup commit with line" 0 $RC
terms
printf '%s\n' "keep" > "$R/a.txt"; git -C "$R" add a.txt
commit "remove line"; is "6 removed term line passes" 0 $RC

# 7 public: missing private-terms refuses
mkrepo public; rm -f "$(infof private-terms)"
put a.txt "x"; commit; is "7 missing terms file refuses" 1 $RC; has "7 names the file" private-terms

# 8 private: terms not applied
mkrepo private
put a.txt "text $TERM1 here"; commit; is "8 private: term in content passes" 0 $RC

# 9 private pre-push
mkrepo private; bare
put a.txt "x"; commit; push; is "9 push to unlisted remote refused" 1 $RC
has "9 names the check" remote-not-allowed
url=$(git -C "$R" remote get-url origin)
OUT=$(echo "$OUT" | grep -v "failed to push"); lacks "9 output hides the remote" "$url"
printf '# list\n\n%s\n' "$url" > "$(infof allowed-remotes)"
push; is "9 push to listed remote passes" 0 $RC
: > "$(infof allowed-remotes)"
put b.txt "y"; commit; push; is "9 empty list refuses" 1 $RC

# 10 public pre-push re-check of commits made while the hooks were absent
mkrepo public noinstall; bare
printf 'a message check\n' > "$R/a.txt"; git -C "$R" add a.txt
git -C "$R" commit -q -m "talks about $TERM1 here"
sh "$here/install-hooks.sh" --mode public "$R" >/dev/null; terms
push; is "10 term in message refused at push" 1 $RC; has "10 names the commit" "commit "; lacks "10 hides the term" "$TERM1"
mkrepo public noinstall; bare
printf 'body %s body\n' "$TERM1" > "$R/a.txt"; git -C "$R" add a.txt
git -C "$R" commit -q -m "clean message"
sh "$here/install-hooks.sh" --mode public "$R" >/dev/null; terms
push; is "10 term in content refused at push" 1 $RC; has "10 names the file" "a.txt:1"
mkrepo public noinstall; bare
printf 'fine\n' > "$R/a.txt"; git -C "$R" add a.txt; git -C "$R" commit -q -m "clean"
sh "$here/install-hooks.sh" --mode public "$R" >/dev/null; terms
push; is "10 clean history pushes" 0 $RC

# 11 install over a foreign hook
mkrepo public noinstall
hd=$(git -C "$R" rev-parse --git-path hooks); case "$hd" in /*|[A-Za-z]:*) ;; *) hd="$R/$hd" ;; esac
printf '#!/bin/sh\nexit 0\n' > "$hd/pre-commit"
OUT=$(sh "$here/install-hooks.sh" --mode public "$R" 2>&1); RC=$?
is "11 install refuses over a foreign hook" 1 $RC; has "11 names the file" pre-commit
OUT=$(sh "$here/install-hooks.sh" --mode public --force "$R" 2>&1); RC=$?
is "11 --force overrides" 0 $RC
cmp -s "$here/pre-commit" "$hd/pre-commit" && ok || bad "11 hook replaced"

# 12 core.hooksPath set
mkrepo public noinstall
git -C "$R" config core.hooksPath "$T/elsewhere"
OUT=$(sh "$here/install-hooks.sh" --mode public "$R" 2>&1); RC=$?
is "12 install refuses with core.hooksPath" 1 $RC; has "12 names the setting" core.hooksPath; lacks "12 hides its value" "elsewhere"

# 13 mode file missing
mkrepo public; terms; rm -f "$(infof openddil-hooks-mode)"
put a.txt "x"; commit; is "13 missing mode file refuses" 1 $RC; has "13 names install-hooks.sh" install-hooks.sh

echo "test-hooks: $pass/$((pass + fail)) passed"
[ "$fail" -eq 0 ]
