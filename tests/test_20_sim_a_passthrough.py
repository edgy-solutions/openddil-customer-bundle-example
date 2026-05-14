"""
Test 20 — System A AMQP -> Kafka passthrough.

Publish a System A message to RabbitMQ exchange sim-a.entity-state. Verify
it appears unchanged on Kafka topic ingress-sim-a-raw (no translation,
just transport).

The transport is now handled by Redpanda Connect's amqp_0_9 input
(redpanda-connect-sim-a-amqp container), which replaced the Python sidecar
in Phase 3.5. The Bloblang translation downstream is verified in test_21.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _example_helpers import (  # noqa: E402
    consume_topic_recent,
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_20_sim_a_passthrough"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable on localhost:5672 — "
                    "Phase 3.5 stack not running")
    try:
        msg = sample_sim_a_message(
            vehicle_id="SIM-A-TEST20",
            sequence=20,
            timestamp="2026-05-13T12:20:00Z",
        )
        publish_sim_a_amqp(msg)
    except ImportError as exc:
        skip_(NAME, str(exc))

    time.sleep(4)

    records = consume_topic_recent("ingress-sim-a-raw",
                                    max_records=50, timeout_s=6.0,
                                    per_partition_tail=20)
    matching = []
    for key, value in records:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if payload.get("vehicle_id") == "SIM-A-TEST20":
            matching.append((key, payload))

    if not matching:
        fail_(NAME, "no SIM-A-TEST20 record on ingress-sim-a-raw — verify the "
                    "Connect amqp_0_9 input is consuming "
                    "openddil.sim-a.ingress")

    key, payload = matching[-1]
    if key.decode() != "SIM-A-TEST20":
        fail_(NAME, f"Kafka key {key!r} != vehicle_id (partition affinity broken)")

    # Verify the native fields are passed through unchanged (no translation)
    for required in ("vehicle_id", "platform_type", "fuel", "weapons",
                      "subsystems", "position", "callsign", "timestamp"):
        if required not in payload:
            fail_(NAME, f"native field {required!r} missing from passthrough; "
                        f"sidecar/connect should NOT translate")

    pass_(NAME, f"AMQP->Kafka passthrough OK; key={key.decode()} "
                f"fields={sorted(payload.keys())[:5]}...")


if __name__ == "__main__":
    main()
