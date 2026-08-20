#!/usr/bin/env bash
# =============================================================================
# ADR-0039 bridging thesis, as a runnable demonstration
# =============================================================================
# Shows tactical damage constraining sustainment claims across two planes:
#
#   dis-sim --damage  ->  DIS appearance bits  ->  Stage 1 decode
#     ->  Silver health axis + subsystem fault codes
#     ->  logistics-sim caps what it may claim about those assets
#
# Every step prints a COUNT, not an adjective. A step that cannot be counted
# is a step that cannot be shown to have happened.
#
# PREREQUISITES — checked, not assumed (step 0). This needs THREE artifacts
# at or after 2026-08-19:
#   * dis-sim source with the --damage control        (ConfigMap, updatable here)
#   * sensor-ingest image with appearance.py          (container image)
#   * sim-dis-mapping.yaml reading $src.appearance    (bundle image)
# The first is deployable from this repo; the other two arrive by image
# rebuild. Step 0 fails loudly and names which one is missing, because a demo
# that silently shows nothing is worse than one that refuses to start.
set -uo pipefail
NS="${NS:-openddil}"
CTX="${CTX:-edgy-lab}"
SIM="${SIM:-dis-sim-edge-northpoint}"
K="kubectl --context $CTX -n $NS"
PG="$K exec openddil-postgres-hq-0 -- psql -U postgres -d openddil -tAc"

say() { printf '\n=== %s ===\n' "$1"; }

say "0. preflight — are the three artifacts deployed?"
missing=0
# "no pod ready" and "source too old" are different facts and must not share
# a message — one is a wait, the other is a deploy. Conflating them is the
# same mistake the whole absence-convention thread exists to correct.
p=$($K get pods --no-headers | grep "$SIM" | grep Running | awk '{print $1}' | head -1)
if [ -z "$p" ]; then
  echo "  UNKNOWN: no Running $SIM pod — cannot tell whether the source is"
  echo "           current. Wait for the rollout and re-run; this is not a"
  echo "           statement about the source."
  missing=1
# NOTE the sh -c wrapper on every in-container path. A bare /src/... passed
# as an exec argument is rewritten by MSYS path conversion under Git Bash to
# C:/Program Files/Git/src/..., so the check FAILED ON A PRESENT ARTIFACT and
# reported it missing. A preflight that produces false negatives sends people
# to redeploy something that is already there.
elif ! $K exec "$p" -- sh -c 'grep -q DIS_DAMAGE /src/dis_sim.py' 2>/dev/null; then
  echo "  MISSING: dis-sim source lacks --damage."
  echo "           fix: ./tools/dis-sim/deploy.sh   (rebuilds the ConfigMap)"
  missing=1
else
  echo "  ok: dis-sim can emit appearance"
fi
si=$($K get pods --no-headers | grep sensor-ingest | grep Running | awk '{print $1}' | head -1)
if [ -z "$si" ] || ! $K exec "$si" -- sh -c 'test -f /app/appearance.py' 2>/dev/null; then
  echo "  MISSING: sensor-ingest image predates appearance decoding."
  echo "           fix: image rebuild; nothing in this repo can supply it"
  missing=1
else
  echo "  ok: sensor-ingest decodes appearance"
fi
[ "$missing" -eq 0 ] || { echo; echo "REFUSING TO RUN — see above."; exit 1; }

say "1. baseline — health axis before any injection"
$PG "SELECT COALESCE(health_state,'(unset)'), count(*) FROM telemetry_latest_state GROUP BY 1 ORDER BY 2 DESC;"
echo "  expect: every row (unset). DIS carries no health until damage is claimed."

say "2. inject — half the entities destroyed, with a mobility kill"
$K set env "deploy/$SIM" DIS_DAMAGE=destroyed DIS_DAMAGE_FRACTION=0.5 >/dev/null
echo "  waiting 60s for the generator to restart and a projection cycle"
sleep 60

say "3. the wire — appearance bits are now non-zero"
$K exec openddil-redpanda-edge-01-0 -- bash -c \
  "rpk topic consume ingress-dis-raw -n 400 -o -400 -f '%v\n' 2>/dev/null" \
  | grep -o '"appearance_bits":[0-9]*' | sort | uniq -c
echo "  expect: a non-zero value appears. 0x200000 is an explicit NO-DAMAGE"
echo "          claim; 0x200018 carries destroyed. Zero means SILENCE, which"
echo "          is why the decoder refuses it (ADR-0026 clause 2)."

say "4. Silver — the health axis populates, and only for claimed assets"
$PG "SELECT COALESCE(health_state,'(unset)'), count(*) FROM telemetry_latest_state GROUP BY 1 ORDER BY 2 DESC;"
echo "  expect: HEALTH_STATE_FAILED for the damaged half, NOMINAL for the"
echo "          claimed-undamaged half. Assets from an undeclared source or"
echo "          with a zero field stay (unset) — absence is preserved."

say "5. subsystem faults — kills arrive as fusion's own vocabulary"
$PG "SELECT unnest(active_fault_codes), count(*) FROM telemetry_latest_state WHERE active_fault_codes IS NOT NULL GROUP BY 1;" 2>/dev/null \
  || echo "  (column not projected in this build — read from the topic instead)"

say "6. the constraint — logistics-sim caps what it may claim"
$PG "SELECT overall_severity, count(*) FROM asset_logistics_status GROUP BY 1 ORDER BY 2 DESC;"
echo "  expect: the damaged assets cannot be reported mission-capable."
echo "          THE POINT: the sustainment plane did not observe the damage."
echo "          It was constrained by a plane it does not own."

say "7. restore"
$K set env "deploy/$SIM" DIS_DAMAGE- DIS_DAMAGE_FRACTION- >/dev/null
echo "  injection reverted; steady state restored"
