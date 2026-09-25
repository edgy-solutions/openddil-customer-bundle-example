# =============================================================================
# dis-sim — DIS entity generator, dependencies baked in
# =============================================================================
# WHY THIS EXISTS
# ---------------
# The Deployments used to run `pip install opendis==1.0` in their args, on
# every container start. That bought not maintaining an image for a test
# fixture, and it cost a reachable PyPI at start -- on every restart, of
# every replica, forever. In an air-gapped or merely flaky environment the
# generator does not start, and the failure arrives as a CrashLoop in a
# simulator rather than as a network error, which is a long way from the
# cause.
#
# One dependency, pinned to the version openddil-sensor-ingest DECODES with.
# If these two ever disagree the sim emits PDUs the ingestor cannot read, so
# the pin is the contract between them, not a lockfile habit.
#
# WHAT IS DELIBERATELY *NOT* IN HERE
# ----------------------------------
# dis_sim.py itself. It stays in the ConfigMap that deploy.sh recreates from
# the single source file on every run, because that is what keeps the running
# generator from drifting away from the file in this directory. Baking the
# script would make this Dockerfile a SECOND place it lives -- the exact
# shape of the 2026-08-19 sensor-ingest failure, where an image shipped
# without a module the source tree had, and the pod CrashLooped on a line
# that worked locally.
#
# So: dependencies change rarely and are baked; the script changes constantly
# and is mounted. Each lives in exactly one place.
# =============================================================================
FROM python:3.11-slim

# Pinned to match openddil-sensor-ingest's decoder. See above: this is a
# wire contract between the generator and the ingestor.
RUN pip install --no-cache-dir opendis==1.0

# Fail at BUILD time if the pin ever stops providing what dis_sim.py imports,
# rather than at 03:00 in a lab with no PyPI to fall back on.
RUN python -c "from opendis.dis7 import EntityStatePdu; from opendis.DataOutputStream import DataOutputStream"

ENV PYTHONUNBUFFERED=1
WORKDIR /src

# No CMD. The Deployment supplies it, because the script path is a mount
# point this image knows nothing about.
