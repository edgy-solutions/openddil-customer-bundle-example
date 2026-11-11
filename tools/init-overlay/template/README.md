# @NAME@

A private overlay for one deployment, made by init-overlay from the public example at
openddil-customer-bundle-example `@BUNDLE_SHA@`. Policy id: `@POLICY_ID@`.

Everything in the skeleton is fictional: the example's invented nations (ATL, BDR, SYL), edges, kinds and
values. Replace it with your own, here, and keep it here.

## Private, stays here

- your program's kinds, routes, destinations and hosts
- sites and edges
- nation vocabularies
- enumeration tuples
- declarations that name real owners
- units built on program data, and model names

## Crosses to public, as shape only, never values

- A unit that runs on public or synthetic data goes to openddil-tactical-agents as a pull request from a
  branch, one unit a branch, with the public hooks installed (`tools/hooks/install-hooks.sh --mode public`)
  and your terms in `.git/info/private-terms`.
- A checker fix or a new shape goes to openddil-customer-bundle-example as a fictional twin.
- Nothing from a program's data, names or models goes to a public repo. If public work needs it, it stays here.

## Hooks

This repo is installed in private mode. After a fresh clone, re-run
`install-hooks.sh --mode private .` (from `tools/hooks/` in the example). Push only to the remote listed in
`.git/info/allowed-remotes`; a missing or empty file refuses every push.

## The values chain

`values/chain.txt` lists the helm `-f` files in order, one per line. The overlay's last file comes last. Lists
replace, maps deep-merge, and a later `null` does not delete: swap a stale file in place in the list instead.

## Running the checks

Copy `.env.example` to `.env` and fill it, or export the same names. Then:

    bash checks.sh

It writes `out/checks/<name>.log` for each check and ends with `overlay-checks: ran=10 pass=10 fail=0`. A
missing tool or sibling checkout is a failure that names it, never a skip. The ten checks:

1. manifest: `overlay-manifest.yaml` matches what the overlay holds, with at least 8 classes present.
2. dimensions: `dimensions.yaml` is well formed and its nation set is a set of three-letter codes.
3. declarations: each file in `declarations/` has the PEP body shape, known labels and an edge the values declare.
4. enumeration: `enumeration/overlay.json` passes the DIS simulator's validator.
5. kinds: `kinds/` and the fixtures load as the egress service loads them and equal `egress.kinds` in the values.
6. values-chain: `helm template` renders the chain with the openddil-demo chart and the output is not empty.
7. contract-b: the validator's self-test, then `contract-b/replay.jsonl` against the nations in `dimensions.yaml`.
8. egress-twin: the example's egress test, run against this overlay's values, with no skips.
9. kindcheck: each kind the assembler routes assembles from `kindcheck/` inputs and validates against its schema.
10. algorithms: the unit tests, and the harness's guard on every unit directory.

CI (`.github/workflows/conformance.yml`) checks out the example, helm, demo, contracts and tactical-agents at
pinned commits, generates the contracts' python, runs `bash checks.sh`, and asserts the summary line.

## dimensions.yaml status

`dimensions.yaml` is in the init-overlay format, pending an ADR-0055 file ruling. It is not a contract, and
the format may change when the ruling lands.
