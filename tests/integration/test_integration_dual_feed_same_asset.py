"""
Integration test — dual feed, same physical asset.

Drives BOTH the DIS UDP feed and the sim-a AMQP feed for the same hull
(DIS entity 1:1:4773 ↔ sim-a vehicle SIM-A-001 ↔ canonical hull
USA-ARMY-1HBCT-M1A2-4773). Both Silver events should arrive on
raw-sensor-stream within seconds of each other; downstream consumers
(cm-service, fusion-service) should see both inputs.

Today's behaviour (per ADR-0015):
  - DIS Silver event has asset.asset_id = "dis:1:1:4773"  (URN form)
  - sim-a Silver event has asset.asset_id = "USA-ARMY-1HBCT-M1A2-4773"
  - cm-service and fusion-service treat these as TWO separate asset
    instances.

Future behaviour (when the identity-resolver service lands, ADR-0015):
  - DIS feed will also produce canonical "USA-ARMY-1HBCT-M1A2-4773"
  - The strict-assertion block at the end of this test will then pass
    instead of being skipped.

This test PASSES today (both feeds produce Silver for the same physical
hull) AND documents the deferred reconciliation as a strict assertion
to enable later — uncomment that block when ADR-0015 lands.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Inject parent tests/ dir for _customer_helpers
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _example_helpers import (  # noqa: E402
    consume_topic_recent,
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)

# Inject OSS hero_scenario_v3 dir for _helpers (DIS UDP sending).
# parents[3] = the openddil/ OSS workspace (the example bundle lives
# alongside openddil-demo there).
_OSS_HELPERS = (Path(__file__).resolve().parents[3]
                / "openddil-demo" / "tests" / "hero_scenario_v3")
sys.path.insert(0, str(_OSS_HELPERS))
from _helpers import send_fixture, fail_, pass_, skip_  # noqa: E402

NAME = "test_integration_dual_feed_same_asset"
DIS_URN = "dis:1:1:4773"
CANONICAL = "USA-ARMY-1HBCT-M1A2-4773"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable — composed stack not up?")

    # 1. Fire the two feeds within ~1 s of each other
    try:
        send_fixture("sample_entity_state.bin")
    except FileNotFoundError as exc:
        fail_(NAME, f"DIS fixture missing: {exc}")

    try:
        publish_sim_a_amqp(sample_sim_a_message(
            vehicle_id="SIM-A-001",
            callsign="INTEGRATION-T-DUAL",
            sequence=2901,
            timestamp="2026-05-13T22:00:00Z",
        ))
    except ImportError as exc:
        skip_(NAME, str(exc))

    time.sleep(6)  # let Bloblang translate both Bronze entries

    # 2. Decode Silver records; look for both feeds touching the same hull
    try:
        from openddil.telemetry.v1 import telemetry_pb2  # noqa: F401
    except ImportError as exc:
        skip_(NAME, f"protobuf bindings unavailable: {exc}")
    from _protobuf import decode_entity_event  # noqa: E402

    records = consume_topic_recent("raw-sensor-stream",
                                    max_records=2000, timeout_s=12.0,
                                    per_partition_tail=400)
    if not records:
        fail_(NAME, "no records on raw-sensor-stream")

    found_dis = None
    found_sim_a = None
    for _key, value in records:
        try:
            evt = decode_entity_event(value)
        except Exception:
            continue
        sp = evt.provenance.source_protocol
        if "DIS" in sp.upper() and evt.asset.asset_id == DIS_URN:
            found_dis = evt
        elif sp == "sim-a-v1" and evt.asset.callsign == "INTEGRATION-T-DUAL":
            found_sim_a = evt

    if found_dis is None:
        fail_(NAME, f"no DIS Silver event for {DIS_URN} — DIS path broken?")
    if found_sim_a is None:
        fail_(NAME, "no sim-a Silver event with our INTEGRATION-T-DUAL "
                    "callsign — sim-a path broken?")

    # The two events MUST refer to the same physical hull (DIS URN
    # 1:1:4773 = sim-a SIM-A-001 = canonical M1A2-4773), even if their
    # asset_id strings differ today (ADR-0015 asymmetry).
    if found_sim_a.asset.asset_id != CANONICAL:
        fail_(NAME, f"sim-a Silver event missing canonical asset_id; "
                    f"got {found_sim_a.asset.asset_id!r}")
    if found_dis.asset.asset_id != DIS_URN:
        fail_(NAME, f"DIS Silver event lost URN form; "
                    f"got {found_dis.asset.asset_id!r}")

    # FUTURE strict assertion (uncomment when ADR-0015 resolver service lands
    # and the DIS feed performs alias rewrite at Silver):
    #
    # if found_dis.asset.asset_id != CANONICAL:
    #     fail_(NAME, f"ADR-0015 reconciliation: DIS event still URN-form "
    #                 f"({found_dis.asset.asset_id!r}); expected canonical "
    #                 f"{CANONICAL!r}")

    pass_(NAME, f"both feeds produced Silver for the same hull "
                f"(DIS: {DIS_URN}, sim-a: {CANONICAL}); cross-feed "
                f"reconciliation strict-assertion currently deferred per "
                f"ADR-0015")


if __name__ == "__main__":
    main()
