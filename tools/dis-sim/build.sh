#!/usr/bin/env bash
# Build the dis-sim image. Idempotent.
#
#   ./build.sh [--load]
#
# The image carries ONE thing the base python image does not: opendis, pinned
# to the version openddil-sensor-ingest decodes with. dis_sim.py itself is
# NOT in it -- deploy.sh mounts that from a ConfigMap so there is exactly one
# copy of the generator. See Dockerfile for why the split falls there.
#
# WHY YOU HAVE TO DO THIS AT ALL
# ------------------------------
# There is no registry behind `openddil/dis-sim`. Nothing pushes it, so the
# kubelet cannot pull it, so it has to already be on the node. That is the
# price of the image not depending on PyPI at start, and it is the right way
# round: a build you run once beats a network call every container start.
#
# --load copies it into a local cluster's node. Without a local cluster
# runtime it is a no-op with a warning rather than an error, because on a
# real cluster the image gets there some other way.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${DIS_SIM_IMAGE:-openddil/dis-sim:1.0}"

echo "building $IMAGE"
docker build -t "$IMAGE" "$HERE"

# Prove the dependency is actually IN there rather than trusting the build
# log. A base image that already had opendis and a RUN line that silently
# no-opped look identical from the outside.
echo -n "  opendis import inside the image: "
docker run --rm "$IMAGE" python -c \
  "import opendis,sys;from opendis.dis7 import EntityStatePdu;print('ok')"

# And prove it needs no network, which is the entire point. --network none
# makes a container that would reach PyPI fail here instead of in a lab.
echo -n "  same import with no network at all: "
docker run --rm --network none "$IMAGE" python -c \
  "from opendis.dis7 import EntityStatePdu;print('ok')"

if [[ "${1:-}" == "--load" ]]; then
  if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -q .; then
    for c in $(kind get clusters); do
      echo "  loading into kind cluster: $c"
      kind load docker-image "$IMAGE" --name "$c"
    done
  elif command -v minikube >/dev/null 2>&1 && minikube status >/dev/null 2>&1; then
    echo "  loading into minikube"
    minikube image load "$IMAGE"
  elif command -v k3d >/dev/null 2>&1 && k3d cluster list 2>/dev/null | grep -q .; then
    for c in $(k3d cluster list --no-headers | awk '{print $1}'); do
      echo "  loading into k3d cluster: $c"
      k3d image import "$IMAGE" -c "$c"
    done
  else
    echo "  WARNING: no local kind/minikube/k3d cluster found; nothing loaded."
    echo "  On a real cluster, get $IMAGE onto the nodes by whatever means"
    echo "  that cluster uses, or the pods will ImagePullBackOff."
  fi
fi

echo
echo "built: $IMAGE"
echo "next:  ./deploy.sh [namespace]"
