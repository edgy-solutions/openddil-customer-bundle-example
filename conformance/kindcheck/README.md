# kindcheck

Before a deploy, prove that each kind an overlay assembles actually assembles, and that
the record it produces validates against the kind's schema the way the egress gate would
check it. It runs the egress service's own assembler code, offline, on files. Nothing is
sent and nothing is read from a cluster.

For each route in the assembler config, and each open episode in the cm-state, it builds
the record with the assembler's `build_picture` and `assemble`, then validates it against
the kind's JSON Schema (Draft 2020-12). A kind with no route, or with a route but no
episode to assemble, is reported `UNCHECKED` and is not a pass.

Output is ids and key names only, never a record body, and ends with one line:

    kindcheck: checked=N pass=N fail=N unchecked=N

## Run it

    python conformance/kindcheck/kindcheck.py \
      --assembler-config conformance/kindcheck/fixtures/assembler-config.json \
      --schemas          conformance/kindcheck/fixtures/kind-schemas \
      --cm-state         conformance/kindcheck/fixtures/cm-state.json \
      --asset            conformance/kindcheck/fixtures/asset.json \
      --now 2026-11-07T12:00:00Z

For your own overlay, give the same four inputs from your overlay: the assembler config
(`egress.assembler.config` in your values), the kind schemas (your `kinds/` folder, only
the kinds that are assembled), a cm-state message with one open episode per routed kind,
and the asset row the assembler would read.

| flag | meaning |
|---|---|
| `--assembler-config FILE` | the assembler's route list, JSON |
| `--schemas DIR` | `<Kind>.schema.json` files |
| `--cm-state FILE` | one `asset-cm-state` message, JSON, carrying the episodes |
| `--asset FILE` | the readiness and rollup rows for the asset, JSON object or `null` |
| `--parts FILE` | parts records, one JSON object per line (repeatable) |
| `--parts-sim-config FILE` | build the parts records with the logistics sim's own publisher |
| `--now RFC3339` | the assembler's clock; set it so a run is repeatable |
| `--cold` | drive the real handler with an empty, unprimed parts book, as a restart would |
| `--show-picture KEY` | also print this picture section (repeatable) |
| `--print-asset-sql` | print the SQL the assembler reads the asset with, and exit |
| `--egress-src`, `--contracts-src`, `--sim-src` | override a source checkout |

## Exit codes

| code | meaning |
|---|---|
| 0 | every record validated and no kind is unchecked |
| 1 | at least one record was refused by its kind |
| 3 | nothing failed, but a kind was not checked (no route, or no episode) |
| 2 | bad input, or a prerequisite is missing; the message names it |

## What it needs

- Python 3.10 or later, with `jsonschema`, `pyyaml`, `protobuf`, `asyncpg` and
  `confluent-kafka` (the assembler imports the last two when it loads).
- Sibling checkouts, found by the kit's one rule (`conformance/siblings.py`): the directory
  named by `OPENDDIL_REPOS`, then an `openddil/` folder next to this repository, then the
  folder holding it.
  - `openddil-demo`, for `egress/`.
  - `openddil-contracts`, for `gen/python`. The generated python is not committed there; run
    `make python` in that checkout first.
  - `openddil-logistics-sim`, for `src/`, only with `--parts-sim-config`.

## The fixtures

`fixtures/` is built from this repository's public egress twin, `k8s/egress-values.yaml`, by
`make_fixtures.py`: the assembler config and the assembled kind schemas are read from the
values, and the cm-state and asset are synthetic and fictional. `test_kindcheck.py` asserts
the committed files equal the script's output, so they cannot drift. Regenerate with
`python conformance/kindcheck/make_fixtures.py`.

A kind the intake takes in, such as `RepairOrder`, declares no episode, so it is not
assembled and is not in `fixtures/kind-schemas`. Check those with `conformance/overlay/check_kinds.py`.

Run the tests with `python -m pytest conformance/kindcheck -q`.
