"""
Integration test — overlay-defined egress envelope shape unchanged.

Drives a CRITICAL transition for a known asset via sim-a, captures the
RabbitMQ message that lands on `battle-mgmt.asset-status`, parses it as
JSON, and verifies every customer-overlay-promised field is present.

What this catches:
  - Someone changes `system-b-egress.yaml` and drops a required field
  - Someone renames the vocabulary label without updating the match table
  - The egress Connect pipeline regresses (e.g., protobuf-to-json
    processor stops emitting structured output)
  - The egress AMQP routing key contract breaks
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _example_helpers import (  # noqa: E402
    consume_battle_mgmt_egress,
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)

# parents[3] = the openddil/ OSS workspace (sibling of openddil-demo).
_OSS_HELPERS = (Path(__file__).resolve().parents[3]
                / "openddil-demo" / "tests" / "hero_scenario_v3")
sys.path.insert(0, str(_OSS_HELPERS))
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_integration_overlay_egress_intact"
ASSET = "USA-ARMY-1HBCT-M1A2-4773"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable — composed stack not up?")

    # ----------------------------------------------------------------------
    # 1. Drive a CRITICAL transition + collect egress on the main thread.
    #    Main thread blocks on consume_battle_mgmt_egress (which sets up
    #    the bind BEFORE we start collecting). The PUBLISH runs in a
    #    worker so it fires after the bind is established.
    # ----------------------------------------------------------------------
    def driver():
        time.sleep(3)  # let the subscriber bind first
        try:
            publish_sim_a_amqp(sample_sim_a_message(
                vehicle_id="SIM-A-001",
                callsign="INTEGRATION-T-EGRESS",
                fuel_gal=40.0,  # ~8% of 504 gal_us → CRITICAL band
                sequence=2960,
                timestamp="2026-05-13T22:10:00Z",
            ))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=driver, daemon=True).start()

    try:
        received = consume_battle_mgmt_egress(timeout_s=120, expected=20)
    except ImportError as exc:
        skip_(NAME, str(exc))

    # ----------------------------------------------------------------------
    # 2. Find a message for ASSET; validate envelope
    # ----------------------------------------------------------------------
    for_asset = [m for m in received
                  if isinstance(m["body"], dict)
                  and m["body"].get("asset_id") == ASSET]
    if not for_asset:
        seen_ids = sorted({m["body"].get("asset_id") for m in received
                            if isinstance(m["body"], dict)})
        fail_(NAME, f"no battle-mgmt egress message for {ASSET}; "
                    f"saw {len(received)} total, asset_ids={seen_ids}")

    # Use the latest (highest revision) — older messages may predate the
    # transition we just triggered.
    msg = max(for_asset, key=lambda m: int(m["body"].get("revision", 0) or 0))
    body = msg["body"]

    # Required envelope fields per system-b-egress.yaml (default vocabulary).
    REQUIRED_FIELDS = [
        "schema_version",
        "message_type",
        "asset_id",
        "platform",
        "status",
        "is_transition",
        "is_initial",
        "revision",
        "computed_at",
        "factors",
    ]
    missing = [k for k in REQUIRED_FIELDS if k not in body]
    if missing:
        fail_(NAME, f"egress envelope missing fields: {missing}; "
                    f"keys present: {sorted(body)}")

    # Required value-level invariants.
    if body["message_type"] != "asset_subsystem_status":
        fail_(NAME, f"unexpected message_type: {body['message_type']!r}")

    if body["status"] not in {"OK", "DEGRADED", "CRITICAL", "NON_OPERATIONAL",
                                "UNKNOWN"}:
        fail_(NAME, f"unexpected status vocabulary token: {body['status']!r}; "
                    f"the default mapping should emit OK / DEGRADED / "
                    f"CRITICAL / NON_OPERATIONAL")

    factors = body.get("factors") or []
    if not isinstance(factors, list):
        fail_(NAME, f"factors must be a list, got {type(factors).__name__}")
    if factors:
        # Each factor's shape should also be stable.
        REQUIRED_FACTOR_FIELDS = ["factor", "severity", "description",
                                    "current_value", "threshold"]
        fmissing = [k for k in REQUIRED_FACTOR_FIELDS if k not in factors[0]]
        if fmissing:
            fail_(NAME, f"factor[0] missing fields: {fmissing}; "
                        f"keys present: {sorted(factors[0])}")

    # Routing key contract: asset.<asset_id>.status
    rk = msg["routing_key"]
    expected_rk_prefix = f"asset.{ASSET}"
    if not rk.startswith(expected_rk_prefix):
        fail_(NAME, f"routing_key {rk!r} doesn't start with "
                    f"{expected_rk_prefix!r}")
    if not rk.endswith(".status"):
        fail_(NAME, f"routing_key {rk!r} doesn't end with '.status'")

    pass_(NAME, f"egress envelope intact for {ASSET}: status={body['status']}, "
                f"revision={body['revision']}, factors={len(factors)}, "
                f"routing_key={rk}")


if __name__ == "__main__":
    main()
