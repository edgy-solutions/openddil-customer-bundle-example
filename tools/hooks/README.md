# openddil git hooks

A small hook set that keeps secrets and private vocabulary out of commits and pushes. It holds no
vocabulary of its own: every term comes from a file you keep inside `.git`, which can never be committed.

## Two modes

- **public**: your clone of a public repo such as openddil-tactical-agents. List your program's names,
  data names and model names in `.git/info/private-terms`, one per line (blank lines and `#` comments are
  ignored; matching is case-insensitive and fixed-string). That file never leaves your machine. The file must
  exist, or the hooks refuse.
- **private**: a private overlay. List the private remote it may be pushed to in
  `.git/info/allowed-remotes`, one URL per line. A missing or empty file refuses every push. The terms check is
  not applied here, because private terms belong in this repo.

## What is checked

Both modes refuse, on added lines only: a YAML `kind: Secret` line, a private-key PEM header, and a kubeconfig
`client-key-data:` or `client-certificate-data:` line with a value.

Public mode also refuses a private term in an added line, in a staged path, and in a commit message line.
pre-commit and commit-msg check what you are about to commit. pre-push re-checks every outgoing commit (added
lines, paths and message), so a commit made while the hooks were absent is still caught.

Removed lines are never refused, so a clean-up commit can go through.

## Install

    sh tools/hooks/install-hooks.sh --mode public|private [--force] REPO

It copies `pre-commit`, `commit-msg`, `pre-push` and `lib.sh` into the repo's hooks directory, writes the mode
file, and creates the terms (or allowed-remotes) file with a comment header if it is absent. It refuses to replace
an existing hook that differs from ours unless you pass `--force`, and it refuses when `core.hooksPath` is set,
because the hooks would be ignored. Linked worktrees share the main checkout's hooks and info files.

## Refusal format

Each finding is one line: the hook, the check, the location, and for a term its number in your terms file:

    pre-commit: private-term: docs/notes.md:12: term #4
    pre-push: private-term: commit 1a2b3c4 message line 1: term #4

The output never prints the matched line, the term, or the remote URL. It ends with one line on how to fix the
commit (unstage, edit, amend). A path that holds a term is shown as `(path holding a term)`.

## What this is not

The hooks are a floor, not a review. They find the values you listed and a few well-known secret shapes. They
do not read the diff for you. The list file is yours: keep it current as your program's names change.

## Test

    bash tools/hooks/test-hooks.sh

It plants each value in throwaway repos and prints `test-hooks: N/N passed`.
