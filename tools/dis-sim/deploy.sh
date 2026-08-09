#!/usr/bin/env bash
# Deploy dis-sim into a namespace. Idempotent.
#
#   ./deploy.sh [namespace]        default: openddil
#
# Creates the ConfigMap from the SINGLE source file, then applies the
# Deployments. Recreating the ConfigMap on every run is what keeps the
# running generator and dis_sim.py from drifting.
set -euo pipefail
NS="${1:-openddil}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "namespace: $NS"
kubectl -n "$NS" create configmap dis-sim-src \
  --from-file=dis_sim.py="$HERE/dis_sim.py" \
  --dry-run=client -o yaml | kubectl -n "$NS" apply -f -

kubectl -n "$NS" apply -f "$HERE/k8s/dis-sim.yaml"

# Force a restart so a changed script is actually picked up — a ConfigMap
# update alone does not restart pods, and the script is read once at start.
kubectl -n "$NS" rollout restart deploy/dis-sim-edge-northpoint deploy/dis-sim-edge-capeverdant

echo
echo "watch it start:  kubectl -n $NS logs -l app.kubernetes.io/name=dis-sim --tail=20 -f"
