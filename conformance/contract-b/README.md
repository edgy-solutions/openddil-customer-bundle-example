# Contract B conformance kit

Contract B is the read side from a system of record (equipment readiness and spares) into OpenDDIL.
An adapter is integrated when it passes this kit **against its own output**. Compiling, a demo
running, or a document saying so does not count. The contract is defined in
`openddil-contracts/decisions/DESIGN-2026-09-06-interface-contracts.md`, §2–§4 and the 2026-10-03
amendment.

This kit contains no knowledge of any particular source system. Tables, endpoints and code maps
live in the integrator's private overlay.

| File | What it is |
|---|---|
| `validate.py` | The validator. Stdlib Python ≥ 3.10, no installs. Reads JSONL or `rpk topic consume` output. |
| `selftest.sh` | Proves the validator can fail: every rule has a red case, and an empty input exits 2. |
| `fixtures/replay/` | Synthetic Contract B records: 6 assets, both planes, one replay. |
| `fixtures/refused/` | One file per rule, each violating exactly that rule. |
| `fixtures/nations.txt` | The fixture's nation set, for `--nations`. |

## The record

Records go on the element planes:
- `asset-element-telemetry`, one record per asset per observation, key `asset_id`;
- `asset-element-inventory`, one per asset per layer, key `asset_id:layer_name`.

Each record is the existing element-plane envelope plus:

| Block | Rule it answers to |
|---|---|
| `sustainment_id` beside `asset_id` | Both identifiers carried, neither derived (`sustainment_id_absent`, `identifier_collapsed`). |
| `site` | The asset's site as a declared field, which is the key for labels. `asset_id` is opaque: nothing, this validator included, parses it (`site_absent`). |
| `provenance.originator_nation`, `provenance.releasable_to` | Labels under `provenance`, never at top level (`label_absent`, `label_misplaced`). |
| `extraction.cursor`, `extraction.extracted_at_ns` | Extraction lag is data (`extraction_absent`, `extraction_before_observation`). |
| `operational.readiness` `{status: FMC\|PMC\|NMC, factors: [...]}` | Status equals the worst factor (`readiness_factor_mismatch`), and agrees with `health_state` (`readiness_health_conflict`). |
| `extras` `{declared: "organic", fields: {...}}` | Anything with no contract home. Nothing else may appear at top level (`unknown_field`). |

Run `python3 validate.py --list-rules` for the full rule set.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | At least one record, none refused. |
| 1 | At least one record refused; the counts by rule are printed. |
| 2 | **No records: nothing was validated.** An empty run is not a pass. |
| 3 | Usage or IO error. |
| 4 | `--check-enums`: the embedded vocabularies differ from the proto. |

## Runbook

### Stage 0: the validator can fail, on this machine

```sh
bash selftest.sh          # Windows Git Bash: PYTHON="py -3" bash selftest.sh
```
- It ends with
  `contract-b selftest: good=25 accepted, red cases=<n>/<n> matched, rules uncovered=0, enums=<…>`.
- Anything else means stop: the validator itself is not trustworthy here.

### Stage 1: the adapter's real output

Consume what the adapter actually wrote, from the start of both planes. `<N>` must be at least the
number of records written:
```sh
rpk topic consume asset-element-telemetry asset-element-inventory -o start -n <N> \
  | python3 validate.py --nations <your-nations.txt>
```
- `--readiness required` is the default.
- The output is counts only.
- `--show-refused` adds record keys to find a defect locally. It warns that its output must not
  leave the side that produced it, and that warning applies.

### Stage 2: what the adapter did not emit

The adapter sends every record it does not emit to `ingress-dlq` as
`{"dlq_reason": "<id>", "dlq_stage": "sor_adapter"}`. Count by reason:
```sh
rpk topic consume ingress-dlq -o start -n <N> -f '%v\n' | python3 -c '
import collections, json, sys
c = collections.Counter()
for line in sys.stdin:
    try: v = json.loads(line)
    except ValueError: continue
    if isinstance(v, dict) and v.get("dlq_stage") == "sor_adapter": c[v.get("dlq_reason")] += 1
print(" ".join(f"{k}={n}" for k, n in sorted(c.items())) or "none")'
```

### Stage 3: landing

On the hub postgres, records from a system-of-record adapter are the ones carrying
`operational.readiness`:
```sql
SELECT count(*) AS rows, count(originator_nation) AS labelled
  FROM asset_element_telemetry WHERE operational ? 'readiness';
SELECT count(*) FROM inventory_items i
 WHERE EXISTS (SELECT 1 FROM asset_element_telemetry t
                WHERE t.asset_id = i.asset_id AND t.operational ? 'readiness');
```
Expect telemetry rows = distinct telemetry assets accepted in stage 1, with labelled = rows.

**Known gaps on the OpenDDIL side**, so these counts are not misread:
- `sustainment_id` is not stored at landing;
- `inventory_items` labels are not filled.

Both are listed in the design amendment.

### Optional: prove the landing path with the fixture, before an adapter exists

Do this on a development stack only, never a shared or production one. It writes 6 synthetic assets
(`dis:1:9:4001`–`4006`):
```sh
for t in asset-element-telemetry asset-element-inventory; do
  python3 -c 'import json,sys
for l in open(sys.argv[1]):
    e = json.loads(l); print(e["key"] + "\t" + json.dumps(e["value"], separators=(",", ":")))' \
    fixtures/replay/$t.jsonl | rpk topic produce "$t" -f '%k\t%v\n'
done
```
- Then stage 3 should read 6 telemetry rows, 6 labelled, and 12 inventory rows.
- Remove them afterwards on both tables with
  `DELETE … WHERE asset_id IN ('dis:1:9:4001', …, 'dis:1:9:4006')`, listing the six ids exactly. Do
  not pattern-match ids: `asset_id` is opaque (ADR-0047).

## The report

Report back **only** this block. Leave out records, keys and error text:

```
contract-b report  (adapter rev <sha>, source form <api|db-poll|db-cdc>)
  selftest:      <final line of selftest.sh>
  source:        rows read=<n> over polls=<n>
  validator:     <first line of validate.py output>
  refused:       <none | rule=count ...>
  dlq:           <stage 2 output>
  conservation:  rows read = emitted + dlq + not-advanced  →  <n> = <n> + <n> + <n>  (<holds|DOES NOT HOLD>)
  readiness:     <readiness line>
  labels:        <labels line>
  identifiers:   <identifiers line>
  lag:           <extraction lag line>
  landing:       asset_element_telemetry rows=<n> labelled=<n>; inventory_items rows=<n>
```

A stage that did not run is reported as did not run, never as zero.
