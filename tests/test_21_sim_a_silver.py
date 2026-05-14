"""
Test 21 — System A Silver translation.

After test 20's passthrough lands on ingress-sim-a-raw, verify the
sim-a-mapping.yaml Bloblang produces a Silver event on raw-sensor-stream
with:
  - canonical asset_id resolved via aliases (SIM-A-001 → M1A2-4773)
  - kinematics as Quantity-shaped messages
  - sustainment fields (fuel, ammo) populated
  - platform_variant set (via variant alias lookup, not direct passthrough)
  - source_protocol == "sim-a-v1"
"""
from __future__ import annotations

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

NAME = "test_21_sim_a_silver"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable")
    try:
        # Unique callsign discriminator so we don't pick up stale records
        # from earlier tests when iterating the topic.
        msg = sample_sim_a_message(
            vehicle_id="SIM-A-001",
            platform_type="M1A2-SEPv3",
            callsign="IRON-LEAD-T21",
            sequence=21,
            timestamp="2026-05-13T12:21:00Z",
            fuel_gal=200.0,
        )
        publish_sim_a_amqp(msg)
    except ImportError as exc:
        skip_(NAME, str(exc))

    time.sleep(5)

    try:
        from openddil.telemetry.v1 import telemetry_pb2  # noqa: F401
    except ImportError as exc:
        skip_(NAME, f"protobuf bindings unavailable: {exc}")
    from _protobuf import decode_entity_event  # noqa: E402

    # DIS produces continuously, so the topic tail is mostly DIS; cast a
    # wide net so we don't miss the sim-a record under the noise.
    raws = consume_topic_recent("raw-sensor-stream",
                                  max_records=2000, timeout_s=12.0,
                                  per_partition_tail=400)
    if not raws:
        fail_(NAME, "no records on raw-sensor-stream")

    found = None
    for key, value in raws:
        try:
            evt = decode_entity_event(value)
        except Exception:
            continue
        if evt.provenance.source_protocol == "sim-a-v1" \
                and evt.asset.callsign == "IRON-LEAD-T21":
            found = evt
            break
    if found is None:
        fail_(NAME, "no sim-a-v1 Silver event with callsign=IRON-LEAD-T21; "
                    "verify sim-a-mapping.yaml is loaded by redpanda-connect")

    if found.asset.asset_id != "USA-ARMY-1HBCT-M1A2-4773":
        fail_(NAME, f"asset_id not reconciled via aliases: "
                    f"got {found.asset.asset_id!r} expected USA-ARMY-1HBCT-M1A2-4773")
    if found.asset.platform_variant != "M1A2-SEPv3":
        fail_(NAME, f"platform_variant not reconciled via platform_variant_aliases: "
                    f"got {found.asset.platform_variant!r} expected M1A2-SEPv3")

    if not found.kinematics.position.HasField("ecef"):
        fail_(NAME, "kinematics.position.ecef not set; expected ECEF Quantity")
    if found.kinematics.position.ecef.x.unit != "m":
        fail_(NAME, f"ECEF x unit not 'm': {found.kinematics.position.ecef.x.unit!r}")

    if found.sustainment.fluids.fuel_remaining.unit != "gal_us":
        fail_(NAME, f"fuel unit not 'gal_us': "
                    f"{found.sustainment.fluids.fuel_remaining.unit!r}")
    if abs(found.sustainment.fluids.fuel_remaining.value - 200.0) > 0.01:
        fail_(NAME, f"fuel value mismatch: "
                    f"{found.sustainment.fluids.fuel_remaining.value} vs 200.0")

    items = dict(found.sustainment.consumables.items)
    if "main_gun" not in items:
        fail_(NAME, f"main_gun slot missing from consumables: keys={list(items)}")

    pass_(NAME, f"Silver translation OK; asset_id={found.asset.asset_id} "
                f"variant={found.asset.platform_variant} "
                f"fuel={found.sustainment.fluids.fuel_remaining.value}"
                f"{found.sustainment.fluids.fuel_remaining.unit}")


if __name__ == "__main__":
    main()
