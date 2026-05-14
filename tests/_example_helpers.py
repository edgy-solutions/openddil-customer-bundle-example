"""
Example-bundle test helpers (sim-a AMQP + battle-mgmt egress).

Lives in the EXAMPLE customer overlay. Contains only placeholder-shape
helpers that are safe to publish — no real customer ICD is encoded here.
Imports the OSS `_cm_helpers` module for generic infrastructure (Kafka
consume, docker compose exec, Restate state clear, etc.).

Real-customer test helpers live in the corresponding PRIVATE bundle
(e.g., openddil-customer-bundle-customer-overlay/tests/_proprietary_helpers.py)
and are NEVER published.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

# Add the OSS hero_scenario_v3 dir to sys.path so we can reuse the generic
# helpers (consume_topic_recent, docker_compose, etc.).
#
# This file lives at:
#   openddil/openddil-customer-bundle-example/tests/_example_helpers.py
# parents[2] = the openddil/ OSS workspace (the example bundle is checked
# in alongside the other OSS repos as a sibling of openddil-demo).
_OSS_HELPERS_DIR = (
    Path(__file__).resolve().parents[2]
    / "openddil-demo" / "tests" / "hero_scenario_v3"
)
if _OSS_HELPERS_DIR.is_dir() and str(_OSS_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_OSS_HELPERS_DIR))

# Re-export the OSS generic helpers so example tests have one import root.
from _cm_helpers import (  # noqa: E402, F401
    COMPOSE_DIR,
    KAFKA_BOOTSTRAP,
    REPO_ROOT,
    clear_asset_cm_state,
    clear_asset_logistics_state,
    cm_service_alive,
    consume_asset_cm_state_for,
    consume_asset_logistics_updates,
    consume_tactical_events,
    consume_topic_recent,
    count_tactical_events,
    docker_compose,
    submit_cm_event_via_cli,
)


# ---------------------------------------------------------------------------
# Sim-A AMQP feed + battle-mgmt egress (PLACEHOLDER shape)
# ---------------------------------------------------------------------------
RABBIT_URL = "amqp://openddil:openddil@localhost:5672/"
SIM_A_EXCHANGE = "sim-a.entity-state"
SIM_A_ROUTING_KEY_TPL = "entity.{vehicle_id}"
EGRESS_EXCHANGE = "battle-mgmt.asset-status"


def rabbitmq_alive() -> bool:
    """True if RabbitMQ AMQP listener is reachable on localhost:5672."""
    try:
        import socket
        with socket.create_connection(("localhost", 5672), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _pika_or_skip():
    """Return pika or raise ImportError; tests catch and SKIP gracefully."""
    try:
        import pika  # type: ignore
        return pika
    except ImportError as exc:
        raise ImportError(
            "pika not installed on host; install via `uv pip install --system "
            "pika` or run tests in a uv venv with pika"
        ) from exc


def publish_sim_a_amqp(msg: dict, *, vehicle_id: str | None = None) -> None:
    """Publish one System A message to the sim-a.entity-state exchange."""
    pika = _pika_or_skip()
    vid = vehicle_id or msg.get("vehicle_id") or "SIM-A-UNKNOWN"
    params = pika.URLParameters(RABBIT_URL)
    conn = pika.BlockingConnection(params)
    try:
        ch = conn.channel()
        ch.basic_publish(
            exchange=SIM_A_EXCHANGE,
            routing_key=SIM_A_ROUTING_KEY_TPL.format(vehicle_id=vid),
            body=json.dumps(msg).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2,
            ),
        )
    finally:
        conn.close()


def sample_sim_a_message(
    vehicle_id: str = "SIM-A-001",
    platform_type: str = "M1A2-SEPv3",
    callsign: str = "IRON-LEAD",
    fuel_gal: float = 200.0,
    main_gun_remaining: int = 32,
    main_gun_capacity: int = 40,
    extra_subsystems: list[dict] | None = None,
    sequence: int = 1,
    timestamp: str = "2026-05-13T12:00:00Z",
) -> dict:
    """Build a PLACEHOLDER System A message for tests.

    Real System A ICD will replace this shape entirely. Every field name
    here is invented; do not treat as customer-promised contract.
    """
    subsystems = [{"subsystem_id": "POWERPLANT", "health": "OPERATIONAL"}]
    if extra_subsystems:
        subsystems.extend(extra_subsystems)
    return {
        "vehicle_id":    vehicle_id,
        "callsign":      callsign,
        "platform_type": platform_type,
        "timestamp":     timestamp,
        "sequence":      sequence,
        "position":      {"x": -796104.6, "y": -5417889.3, "z": 3280898.7},
        "velocity":      {"x": 0.0, "y": 0.0, "z": 0.0},
        "fuel":          {"main_tank": fuel_gal},
        "weapons": [
            {"station_id": "main_gun", "rounds_remaining": main_gun_remaining,
             "rounds_capacity": main_gun_capacity, "nsn": "1315-01-204-3041"},
        ],
        "subsystems": subsystems,
    }


def consume_battle_mgmt_egress(
    *, timeout_s: float = 60.0,
    expected: int = 1,
) -> list[dict]:
    """Bind a temp queue to battle-mgmt.asset-status and collect up to
    `expected` messages or until timeout."""
    pika = _pika_or_skip()
    params = pika.URLParameters(RABBIT_URL)
    conn = pika.BlockingConnection(params)
    received: list[dict] = []
    try:
        ch = conn.channel()
        result = ch.queue_declare(queue="", exclusive=True)
        qname = result.method.queue
        ch.queue_bind(exchange=EGRESS_EXCHANGE, queue=qname, routing_key="asset.#")

        def cb(channel, method, props, body):
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = body.decode("utf-8", errors="replace")
            received.append({"routing_key": method.routing_key, "body": parsed})
            if len(received) >= expected:
                channel.stop_consuming()

        ch.basic_consume(queue=qname, on_message_callback=cb, auto_ack=True)

        deadline = time.monotonic() + timeout_s

        def stop_later():
            while time.monotonic() < deadline and len(received) < expected:
                time.sleep(0.5)
            try:
                ch.stop_consuming()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=stop_later, daemon=True).start()
        ch.start_consuming()
    finally:
        conn.close()
    return received
