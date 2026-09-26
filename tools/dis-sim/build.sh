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
# WHAT THIS IS FOR, NOW THAT THE IMAGE IS PUBLISHED
# -------------------------------------------------
# openddil-helm's build-bundle workflow builds this same Dockerfile and
# pushes it to ghcr.io/edgy-solutions/openddil/dis-sim, so a cluster can pull
# it and an air-gapped site gets it from the mirror inventory with the chart's
# other images. Running this script is no longer a prerequisite for deploying.
#
# It is how you iterate on the Dockerfile without waiting for CI. It builds
# the SAME ref CI publishes, on purpose: with imagePullPolicy IfNotPresent, a
# local build shadows the registry copy on this machine and nothing else has
# to change. The cost of that is the other direction -- a stale local build
# also shadows a newer published one, so `docker rmi` it when you are done
# testing a change you did not push.
#
# --load copies it into a local cluster's node. Without a local cluster
# runtime it is a no-op with a warning rather than an error: on a real cluster
# the kubelet now pulls it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${DIS_SIM_IMAGE:-ghcr.io/edgy-solutions/openddil/dis-sim:1.0}"

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
    echo "  Not necessarily a problem: a real cluster pulls $IMAGE from"
    echo "  GHCR (or from its mirror). It only matters if you were testing a"
    echo "  local change, which is now unpublished and nowhere the cluster"
    echo "  can see."
  fi
fi

echo
echo "built: $IMAGE"
echo "next:  ./deploy.sh [namespace]"
