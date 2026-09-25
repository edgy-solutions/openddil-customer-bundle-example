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
IMAGE="${DIS_SIM_IMAGE:-openddil/dis-sim:1.0}"

# The image carries opendis; nothing pulls it from a registry because there
# is none. Say so HERE, where the fix is one command away, rather than
# leaving it to be read off an ImagePullBackOff later.
if command -v docker >/dev/null 2>&1 && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "WARNING: $IMAGE is not in the local docker daemon."
  echo "         Run ./build.sh --load first, or make sure the cluster's"
  echo "         nodes already have it. Continuing anyway -- a remote"
  echo "         cluster may well have it without this machine knowing."
  echo
fi

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
