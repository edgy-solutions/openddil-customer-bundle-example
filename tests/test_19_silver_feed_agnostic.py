"""
Test 19 — Silver schema is feed-agnostic.

Consume a batch of recent Silver records that mix DIS and sim-a sources
(the example bundle's two feeds). Verify:
  - All parse cleanly under the same EntityTelemetryEvent schema.
  - No downstream code needs to know which feed produced which record
    (the proof: a single decoder + a single set of assertions handles
    both source_protocol values uniformly).

This was originally a 3-protocol test (DIS + proprietary + sim-a) in the
mixed customer bundle. After the bundle split (Phase 3.6 cleanup), the
proprietary feed lives in the PRIVATE bundle and isn't loaded here. The
multi-protocol assertion still holds with 2 feeds — DIS + sim-a both
producing the same Silver schema is the meaningful property.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _example_helpers import (  # noqa: E402
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)
from _helpers import (  # noqa: E402
    TOPIC_SILVER,
    consume_topic_binary,
    fail_,
    pass_,
    send_fixture,
    skip_,
)

NAME = "test_19_silver_feed_agnostic"


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable — example overlay not up?")

    try:
        from _protobuf import decode_entity_event
    except ImportError as exc:
        skip_(NAME, f"protobuf helper unavailable: {exc}")

    # Generate traffic on both example-bundle feeds so the recent window has
    # a mix.
    try:
        send_fixture("sample_entity_state.bin")
    except FileNotFoundError as exc:
        fail_(NAME, f"DIS fixture missing: {exc}")
    try:
        for vid in ("SIM-A-001", "SIM-A-002", "SIM-A-CAB-01"):
            publish_sim_a_amqp(sample_sim_a_message(
                vehicle_id=vid, sequence=190,
                timestamp="2026-05-13T19:19:00Z",
            ))
    except ImportError as exc:
        skip_(NAME, str(exc))
    time.sleep(5)

    raws = consume_topic_binary(TOPIC_SILVER, n=300, timeout_s=20, offset="-40")
    if not raws:
        fail_(NAME, "no Silver records to inspect")

    parsed: list[tuple[str, str]] = []   # (source_protocol, asset_id)
    decode_failures = 0
    for raw in raws:
        try:
            evt = decode_entity_event(raw)
            parsed.append((evt.provenance.source_protocol, evt.asset.asset_id))
        except Exception:
            decode_failures += 1

    if decode_failures:
        fail_(NAME, f"{decode_failures} Silver record(s) failed to parse under "
                    f"EntityTelemetryEvent schema — schema is NOT feed-agnostic")

    if not parsed:
        fail_(NAME, "every Silver record failed to decode")

    protocols = {p for p, _ in parsed}
    if "DIS/IEEE-1278.1-binary" not in protocols:
        fail_(NAME, f"no DIS-source records in sample; got {protocols}")
    if "sim-a-v1" not in protocols:
        fail_(NAME, f"no sim-a-source records in sample; got {protocols}")

    pass_(NAME,
          f"{len(parsed)} records, all parsed under single schema; "
          f"protocols seen: {sorted(protocols)}; "
          f"decode_failures=0")


if __name__ == "__main__":
    main()
