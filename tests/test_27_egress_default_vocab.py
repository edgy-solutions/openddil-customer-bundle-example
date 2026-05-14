"""
Test 27 — RabbitMQ egress with the default OK/DEGRADED/CRITICAL vocabulary.

Subscribe to battle-mgmt.asset-status, publish a sim-a message that drops
fuel below CRITICAL threshold, verify the emitted AMQP message body
contains `"status": "CRITICAL"` and the expected envelope structure
(schema_version, asset_id, factors[], etc.).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _example_helpers import (  # noqa: E402
    consume_battle_mgmt_egress,
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_27_egress_default_vocab"
ASSET = "USA-ARMY-1HBCT-M1A2-4773"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable")
    try:
        # Drive the fusion engine into CRITICAL by dropping fuel ~10%.
        def driver():
            time.sleep(3)
            try:
                publish_sim_a_amqp(sample_sim_a_message(
                    vehicle_id="SIM-A-001", fuel_gal=45.0, sequence=2700,
                    timestamp="2026-05-13T12:27:00Z",
                ))
            except Exception:
                pass
        threading.Thread(target=driver, daemon=True).start()

        # The fusion engine emits cadenced updates for ALL active assets every
        # ~30s. We want any message for OUR asset; ask for more than one so
        # we don't miss the 4773-keyed message behind unrelated cadenced traffic.
        received = consume_battle_mgmt_egress(timeout_s=120, expected=10)
    except ImportError as exc:
        skip_(NAME, str(exc))

    if not received:
        fail_(NAME, "no messages on battle-mgmt.asset-status; verify "
                    "redpanda-connect-egress is running and bound")

    # Find at least one message for ASSET with the default vocabulary applied
    candidates = [r for r in received
                   if isinstance(r["body"], dict)
                   and r["body"].get("asset_id") == ASSET]
    if not candidates:
        seen_ids = sorted({r["body"].get("asset_id") for r in received
                            if isinstance(r["body"], dict)})
        fail_(NAME, f"no egress message for asset {ASSET} among "
                    f"{len(received)} received; saw asset_ids={seen_ids}")

    body = candidates[-1]["body"]
    for required in ("schema_version", "message_type", "asset_id",
                      "status", "is_transition", "is_initial", "factors"):
        if required not in body:
            fail_(NAME, f"envelope missing required field {required!r}: "
                        f"keys={sorted(body)}")

    if body["status"] not in ("CRITICAL", "DEGRADED"):
        fail_(NAME, f"unexpected status {body['status']!r}; expected "
                    f"CRITICAL or DEGRADED (default vocabulary)")

    rk = candidates[-1]["routing_key"]
    if not rk.endswith(".status"):
        fail_(NAME, f"routing_key {rk!r} should end with .status")

    pass_(NAME, f"egress envelope OK; status={body['status']} "
                f"routing_key={rk} factors={len(body['factors'])}")


if __name__ == "__main__":
    main()
