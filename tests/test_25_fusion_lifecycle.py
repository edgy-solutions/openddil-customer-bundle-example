"""
Test 25 — Restate AssetLogistics lifecycle (initial -> transition -> cadenced).

Clear durable state for a test asset, then drive its lifecycle by
publishing sim-a messages:
  1. Healthy fuel    -> expect is_initial=true emission, severity=OK or
                       DEGRADED (depending on staleness at first sight)
  2. Drop fuel <15%  -> expect is_transition=true emission to CRITICAL
  3. Steady CRITICAL -> expect cadenced updates with is_transition=false

Test asset:  SIM-A-TEST25 -> canonical USA-ARMY-1HBCT-M1A2-TEST25
The alias file does NOT have this entry, so the asset_id stays as
sim_a:SIM-A-TEST25. The fusion service handles UNKNOWN variants by
skipping fuel %-evaluation; we still get a transition through staleness
or DEGRADED-from-no-input. To make the lifecycle observable, we use the
known SIM-A-001 alias and accept the existing state — clear it first.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _example_helpers import (  # noqa: E402
    clear_asset_logistics_state,
    consume_asset_logistics_updates,
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_25_fusion_lifecycle"
ASSET = "USA-ARMY-1HBCT-M1A2-4773"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable")
    if not clear_asset_logistics_state(ASSET):
        skip_(NAME, "could not clear AssetLogistics state; restate-server "
                    "unreachable from compose exec")

    try:
        # 1) Initial healthy snapshot
        publish_sim_a_amqp(sample_sim_a_message(
            vehicle_id="SIM-A-001", fuel_gal=400.0, sequence=2500,
            timestamp="2026-05-13T12:25:00Z",
        ))
        time.sleep(8)
        # 2) Low fuel — should trigger a transition into CRITICAL
        publish_sim_a_amqp(sample_sim_a_message(
            vehicle_id="SIM-A-001", fuel_gal=40.0, sequence=2501,
            timestamp="2026-05-13T12:25:30Z",
        ))
        time.sleep(45)  # let at least one cadenced emit follow the transition
    except ImportError as exc:
        skip_(NAME, str(exc))

    updates = consume_asset_logistics_updates(asset_id=ASSET, timeout_s=10,
                                                max_records=200)
    if not updates:
        fail_(NAME, f"no AssetLogisticsStatusUpdate for {ASSET}; verify "
                    f"logistics-fusion-service is running and subscribed")

    # Sort by revision
    updates_sorted = sorted(updates, key=lambda u: u["revision"])
    has_initial = any(u["is_initial"] for u in updates_sorted)
    has_critical = any(u["overall_severity"] == "LOGISTICS_SEVERITY_CRITICAL"
                        for u in updates_sorted)
    has_cadenced = any(
        (not u["is_initial"]) and (not u["is_transition"])
        for u in updates_sorted
    )

    if not has_initial:
        fail_(NAME, f"no is_initial=true emission seen; "
                    f"revisions seen: {[u['revision'] for u in updates_sorted]}")
    if not has_critical:
        fail_(NAME, f"no CRITICAL severity reached after fuel drop; "
                    f"severities seen: "
                    f"{[u['overall_severity'] for u in updates_sorted]}")
    if not has_cadenced:
        fail_(NAME, "no cadenced (is_transition=false, is_initial=false) "
                    "emission; verify on_timer is scheduling")

    pass_(NAME, f"lifecycle OK; {len(updates_sorted)} updates, initial=Y "
                f"reached_CRITICAL=Y cadenced=Y; "
                f"max_revision={updates_sorted[-1]['revision']}")


if __name__ == "__main__":
    main()
