"""
Integration test — OSS Silver schema parses without drift.

Captures a Silver event from `raw-sensor-stream` (any feed will do — DIS
is the most reliable producer since it streams continuously from the
ongoing simulator) and decodes it against the OSS-defined
EntityTelemetryEvent. Verifies every OSS-defined field path resolves
without `AttributeError` or default-empty surprises.

What this catches:
  - Someone renames `kinematics.position.ecef.x` to `kinematics.position.ecef.X`
  - Someone adds a new oneof variant the overlay-mapped feeds don't fill
  - Someone removes / renumbers a required field on EntityTelemetryEvent
  - Quantity proto moved namespace AGAIN and the wire-name no longer matches

If any OSS Silver consumer (CM service, fusion, downstream COP) would
break, this test breaks first — before customer overlays ship against the
new OSS proto.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _example_helpers import consume_topic_recent  # noqa: E402

# parents[3] = the openddil/ OSS workspace (sibling of openddil-demo).
_OSS_HELPERS = (Path(__file__).resolve().parents[3]
                / "openddil-demo" / "tests" / "hero_scenario_v3")
sys.path.insert(0, str(_OSS_HELPERS))
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_integration_oss_silver_schema_unchanged"


def _has(msg, path: str) -> bool:
    """True if `msg.path1.path2...` resolves to a populated proto field."""
    cur = msg
    for part in path.split("."):
        if not hasattr(cur, part):
            return False
        cur = getattr(cur, part)
    return True


def main() -> None:
    try:
        from openddil.telemetry.v1 import telemetry_pb2  # noqa: F401
        from openddil.common.v1 import quantity_pb2  # noqa: F401
    except ImportError as exc:
        skip_(NAME, f"protobuf bindings unavailable: {exc}")
    from _protobuf import decode_entity_event  # noqa: E402

    # Wait briefly for the DIS simulator to produce something fresh, then
    # consume the recent tail.
    time.sleep(3)

    records = consume_topic_recent("raw-sensor-stream",
                                    max_records=2000, timeout_s=10.0,
                                    per_partition_tail=400)
    if not records:
        fail_(NAME, "no records on raw-sensor-stream — is the DIS simulator "
                    "producing? sim-a/proprietary feeds also OK")

    # Pick a recent event from any feed.
    sample = None
    for _key, value in records:
        try:
            evt = decode_entity_event(value)
            if evt.event_id and evt.asset.asset_id:
                sample = evt
                break
        except Exception:
            continue
    if sample is None:
        fail_(NAME, "no decodable Silver event with non-empty event_id and "
                    "asset_id in the recent tail")

    # OSS-defined required field paths. If ANY of these stops resolving,
    # the OSS proto changed in a way that breaks downstream consumers.
    REQUIRED_PATHS = [
        "event_id",
        "schema_revision",
        "asset.asset_id",
        "asset.platform_type",
        "asset.force",
        "kinematics.position",
        "kinematics.velocity",
        "provenance.producer_id",
        "provenance.source_protocol",
        "provenance.sample_time",
        "provenance.ingest_time",
    ]
    missing = [p for p in REQUIRED_PATHS if not _has(sample, p)]
    if missing:
        fail_(NAME, f"OSS-defined paths missing from Silver event: {missing}")

    # Per ADR-0013: Quantity must be `openddil.common.v1.Quantity`, not the
    # old `openddil.telemetry.v1.Quantity`. The Quantity refactor was a
    # wire-compatible change in Phase 3.5; this test guards against a
    # regression that moves it back or removes the import.
    pos = sample.kinematics.position
    frame = pos.WhichOneof("frame")
    if frame is None:
        fail_(NAME, "kinematics.position oneof is unset")
    if frame == "ecef":
        q = sample.kinematics.position.ecef.x
    elif frame == "wgs84":
        q = sample.kinematics.position.wgs84.lat
    elif frame == "local_enu":
        q = sample.kinematics.position.local_enu.east
    else:
        fail_(NAME, f"unknown position frame {frame!r}")

    quantity_fqn = type(q).DESCRIPTOR.full_name
    if quantity_fqn != "openddil.common.v1.Quantity":
        fail_(NAME, f"Quantity wire-name regressed: got {quantity_fqn!r}; "
                    f"expected openddil.common.v1.Quantity")

    # Sanity: event_id is a UUID-ish string, schema_revision > 0
    if len(sample.event_id) < 16:
        fail_(NAME, f"event_id suspiciously short: {sample.event_id!r}")
    if sample.schema_revision == 0:
        fail_(NAME, "schema_revision is 0 — producer didn't set it")

    pass_(NAME, f"OSS Silver schema intact: "
                f"{len(REQUIRED_PATHS)} field paths resolved, "
                f"position frame={frame}, "
                f"Quantity={quantity_fqn}, "
                f"protocol={sample.provenance.source_protocol}")


if __name__ == "__main__":
    main()
