"""
Compose-orchestration helpers for the composed-stack integration runner.

Brings up the OSS base compose layered with the customer overlay,
waits for the stack to be ready, and tears it down. Used only by the
integration runner — individual integration tests assume the stack is
already up.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# This helper file lives at:
#   openddil/openddil-customer-bundle-example/tests/integration/_compose_helpers.py
# Up two levels = the example bundle root.
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
# The example bundle now lives INSIDE the openddil/ OSS workspace
# (alongside openddil-demo, openddil-contracts, etc.), so the OSS root is
# BUNDLE_ROOT's parent.
OPENDDIL_ROOT = BUNDLE_ROOT.parent

OSS_COMPOSE = OPENDDIL_ROOT / "openddil-demo" / "docker-compose.yml"
# OSS dev override — required locally because several base-compose services
# (notably faust-edge) reference registry images that aren't self-sufficient:
# the image lays down a runtime but the application source is bind-mounted
# from sibling repos via this override. The override is what `docker compose
# up` from openddil-demo picks up automatically; the integration runner has
# to name it explicitly because passing `-f` suppresses auto-loading.
OSS_OVERRIDE = OPENDDIL_ROOT / "openddil-demo" / "docker-compose.override.yml"
CUSTOMER_COMPOSE = BUNDLE_ROOT / "docker-compose.customer.yml"
OSS_DEMO_DIR = OPENDDIL_ROOT / "openddil-demo"


def docker_compose_cmd() -> list[str]:
    """Build the base docker-compose invocation with all -f flags.

    We do NOT pass `-p` — the integration runner takes over the default
    `openddil-demo` project name and assumes the operator has nothing
    critical running under that name. The runner tears down at the end.
    """
    docker = shutil.which("docker") or "docker"
    return [
        docker, "compose",
        "-f", str(OSS_COMPOSE),
        "-f", str(OSS_OVERRIDE),
        "-f", str(CUSTOMER_COMPOSE),
    ]


# ---------------------------------------------------------------------------
# Bring-up / tear-down
# ---------------------------------------------------------------------------

def tear_down(*, remove_volumes: bool = True, timeout_s: int = 120) -> int:
    """Stop and remove the composed stack. Idempotent."""
    cmd = docker_compose_cmd() + ["down"]
    if remove_volumes:
        cmd.append("-v")
    cmd += ["--remove-orphans"]
    print(f"[integration] tear-down: {' '.join(cmd[-4:])}")
    proc = subprocess.run(cmd, cwd=str(OSS_DEMO_DIR),
                          capture_output=True, text=True, timeout=timeout_s)
    return proc.returncode


def bring_up(*, build: bool = False, timeout_s: int = 300) -> int:
    """Start the composed stack in detached mode. Returns docker compose's
    exit code; the caller must then wait_for_ready()."""
    cmd = docker_compose_cmd() + ["up", "-d"]
    if build:
        cmd.append("--build")
    print(f"[integration] bring-up: docker compose -f <oss> -f <overlay> up -d")
    proc = subprocess.run(cmd, cwd=str(OSS_DEMO_DIR),
                          capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        print("[integration] bring-up STDERR:")
        print(proc.stderr[-2000:])
    return proc.returncode


# ---------------------------------------------------------------------------
# Health waits — layered (cheap probes first, expensive ones last)
# ---------------------------------------------------------------------------

# Containers that MUST reach a stable "running" state for integration tests
# to succeed. Excluded:
#   - frontend, postgres-hq, electric-sync, redpanda-hq — UI / regional-hub
#     plumbing; not exercised by Phase C tests, and the frontend has been
#     intermittently flaky as a pre-existing issue.
#   - toxiproxy — only OSS test_07 uses it; not a Phase C dep.
EXPECTED_RUNNING = [
    "redpanda-edge",
    "openddil-sensor-ingest",
    "redpanda-connect",
    "faust-edge",
    "restate-server",
    "restate-hub",
    "cm-service",
    "logistics-fusion-service",
    # example overlay (this bundle) — sim-a + System B egress
    "rabbitmq",
    "redpanda-connect-sim-a-amqp",
    "redpanda-connect-customer",
    "redpanda-connect-egress",
]

# One-shot containers expected to exit 0.
EXPECTED_EXITED_OK = [
    "redpanda-init",
    "rabbitmq-init",
    "cm-service-bootstrap",
    "logistics-fusion-bootstrap",
]

# Kafka topics that must exist before integration tests can run.
REQUIRED_TOPICS = [
    "raw-sensor-stream",
    "ingress-dlq",
    "telemetry-latest-state",
    "tactical-events",
    "asset-cm-state",
    "cm-items",
    "cm-events",
    "ingress-dis-raw",
    "ingress-sim-a-raw",
    "asset-telemetry-windows",
    "asset-logistics-status",
]


def _ps_status() -> dict[str, dict]:
    """Return {service_name: {state, status, exit_code}} for every compose
    service in the project."""
    cmd = docker_compose_cmd() + ["ps", "--all", "--format", "json"]
    proc = subprocess.run(cmd, cwd=str(OSS_DEMO_DIR), capture_output=True,
                          text=True, timeout=30)
    out: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[entry.get("Service", "")] = {
            "state":     entry.get("State", ""),
            "status":    entry.get("Status", ""),
            "exit_code": entry.get("ExitCode", -1),
        }
    return out


def wait_for_containers(timeout_s: int = 240) -> tuple[bool, str]:
    """Wait until every EXPECTED_RUNNING container is `running` (and ideally
    `healthy` where the compose file declared a healthcheck) AND every
    EXPECTED_EXITED_OK one-shot exited 0."""
    deadline = time.monotonic() + timeout_s
    last_state = "<no probe yet>"
    while time.monotonic() < deadline:
        try:
            ps = _ps_status()
        except subprocess.TimeoutExpired:
            time.sleep(2)
            continue

        running_ok = True
        for svc in EXPECTED_RUNNING:
            entry = ps.get(svc)
            if entry is None or entry["state"] != "running":
                running_ok = False
                last_state = f"{svc}: " + (entry["status"] if entry else "<not present>")
                break

        if not running_ok:
            time.sleep(3)
            continue

        oneshot_ok = True
        for svc in EXPECTED_EXITED_OK:
            entry = ps.get(svc)
            if entry is None:
                oneshot_ok = False
                last_state = f"{svc}: <not present>"
                break
            if entry["state"] == "exited" and entry["exit_code"] == 0:
                continue
            oneshot_ok = False
            last_state = f"{svc}: state={entry['state']} exit={entry['exit_code']}"
            break

        if running_ok and oneshot_ok:
            return True, "all containers reached expected state"

        time.sleep(3)

    return False, f"timed out waiting for containers; last_state: {last_state}"


def wait_for_kafka_topics(timeout_s: int = 60) -> tuple[bool, str]:
    """Verify every REQUIRED_TOPICS entry exists on the broker."""
    from confluent_kafka import Consumer  # localized import; not a test dep
    cfg = {
        "bootstrap.servers":  "localhost:9093",
        "group.id":           f"integration-probe-{int(time.time()*1000)}",
        "auto.offset.reset":  "earliest",
        "enable.auto.commit": False,
    }
    deadline = time.monotonic() + timeout_s
    last_missing: set[str] = set()
    while time.monotonic() < deadline:
        try:
            c = Consumer(cfg)
            md = c.list_topics(timeout=5)
            existing = set(md.topics.keys())
            c.close()
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        missing = set(REQUIRED_TOPICS) - existing
        if not missing:
            return True, f"{len(REQUIRED_TOPICS)} required topics present"
        last_missing = missing
        time.sleep(2)
    return False, f"timed out waiting for topics; missing: {sorted(last_missing)}"


def _http_probe(url: str, timeout: int = 2,
                 expected: tuple[int, ...] = (200, 405, 415)) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status in expected
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False
    except urllib.error.HTTPError as e:
        return e.code in expected


def _amqp_probe(host: str = "localhost", port: int = 5672,
                  timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def wait_for_service_endpoints(timeout_s: int = 90) -> tuple[bool, str]:
    """Probe each service that exposes a host-mapped port. Frontend is NOT
    probed — see EXPECTED_RUNNING comment for the same reason."""
    probes: dict[str, callable] = {
        "cm-service /discover":          lambda: _http_probe("http://localhost:9080/discover"),
        "logistics-fusion /discover":    lambda: _http_probe("http://localhost:9081/discover"),
        "rabbitmq AMQP listener":        lambda: _amqp_probe(),
    }
    deadline = time.monotonic() + timeout_s
    not_ready = list(probes.keys())
    while time.monotonic() < deadline:
        not_ready = [name for name, fn in probes.items() if not fn()]
        if not not_ready:
            return True, f"{len(probes)} service endpoints reachable"
        time.sleep(3)
    return False, f"timed out waiting for endpoints; not ready: {not_ready}"


def wait_for_ready(timeout_s: int = 360) -> tuple[bool, str]:
    """Run the layered wait sequence. Returns (success, message)."""
    print("[integration] waiting for containers...", flush=True)
    ok, msg = wait_for_containers(timeout_s=min(timeout_s, 240))
    if not ok:
        return False, f"containers: {msg}"
    print(f"[integration] containers OK ({msg})", flush=True)

    print("[integration] waiting for Kafka topics...", flush=True)
    ok, msg = wait_for_kafka_topics(timeout_s=60)
    if not ok:
        return False, f"kafka: {msg}"
    print(f"[integration] topics OK ({msg})", flush=True)

    print("[integration] waiting for service endpoints...", flush=True)
    ok, msg = wait_for_service_endpoints(timeout_s=90)
    if not ok:
        return False, f"endpoints: {msg}"
    print(f"[integration] endpoints OK ({msg})", flush=True)

    return True, "stack ready"
