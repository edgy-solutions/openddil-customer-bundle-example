"""
Test 23 — Faust rolling windows producing real slopes.

Publish 8+ sim-a messages with monotonically declining fuel. Verify a
WindowedTelemetry record lands on asset-telemetry-windows with the
fluid_trends[fuel_remaining] slope correctly signed (negative) and
unit-tagged via Quantity (gal/h or gallon/h).
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

NAME = "test_23_faust_windows"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable")
    # Use a unique vehicle_id so the Faust windowing buffer is dedicated to
    # this test (the buffer is keyed by canonical asset_id; SIM-A-WINDOW-T23
    # has no alias entry, so its canonical id is the URN sim_a:SIM-A-WINDOW-T23
    # — distinct from any other test's asset).
    test_vehicle = "SIM-A-WINDOW-T23"
    test_asset = f"sim_a:{test_vehicle}"
    try:
        for i in range(8):
            fuel = 200.0 - (i * 5.0)  # 200 -> 165 gal_us
            publish_sim_a_amqp(sample_sim_a_message(
                vehicle_id=test_vehicle,
                fuel_gal=fuel,
                sequence=300 + i,
                timestamp=f"2026-05-13T12:23:{i:02d}Z",
            ))
            time.sleep(0.3)
    except ImportError as exc:
        skip_(NAME, str(exc))

    time.sleep(6)  # Faust agent emits every N samples

    try:
        from openddil.logistics.v1 import windowed_telemetry_pb2 as winpb
    except ImportError as exc:
        skip_(NAME, f"protobuf bindings unavailable: {exc}")

    records = consume_topic_recent("asset-telemetry-windows",
                                    max_records=50, timeout_s=8.0,
                                    per_partition_tail=30)
    if not records:
        fail_(NAME, "no records on asset-telemetry-windows — verify faust-edge "
                    "is running and the windowing agent emits")

    found = None
    for _key, value in records:
        try:
            w = winpb.WindowedTelemetry()
            w.ParseFromString(value)
        except Exception:
            continue
        if w.asset_id == test_asset and "fuel_remaining" in w.fluid_trends:
            found = w
            break

    if found is None:
        fail_(NAME, f"no WindowedTelemetry for {test_asset} with "
                    f"fluid_trends.fuel_remaining; verify Faust windowing agent "
                    f"is buffering and emitting")

    trend = found.fluid_trends["fuel_remaining"]
    if not trend.slope.unit:
        fail_(NAME, f"slope unit empty; expected per-hour rate")
    if trend.slope.value >= 0:
        fail_(NAME, f"slope should be negative (fuel declining): "
                    f"got {trend.slope.value} {trend.slope.unit}")
    if trend.latest.unit != "gallon":
        # Pint emits "gallon" for UCUM "gal_us"; that's correct in the round-trip.
        fail_(NAME, f"latest unit not 'gallon': {trend.latest.unit!r}")

    pass_(NAME, f"window slope OK; asset={found.asset_id} "
                f"latest={trend.latest.value:.1f}{trend.latest.unit} "
                f"slope={trend.slope.value:.2f}{trend.slope.unit} "
                f"samples={found.window.sample_count}")


if __name__ == "__main__":
    main()
