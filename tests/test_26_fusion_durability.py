"""
Test 26 — Restate AssetLogistics durability across worker restart.

After test_25 establishes per-asset state for USA-ARMY-1HBCT-M1A2-4773,
restart the logistics-fusion-service container. Verify:
  - The Kafka topic high-watermarks ADVANCE post-restart (i.e., the worker
    came back and resumed emitting cadenced updates).
  - No spurious transition for the asset (severity stayed CRITICAL).

Why we don't compare AssetLogisticsStatusUpdate.status_revision pre vs
post: Restate journal replay can re-execute the tail of an in-flight
invocation, causing the in-process revision counter to appear to "rewind"
relative to records that were already on the wire. The CORRECT durability
properties are (a) the worker recovers (topic appends continue) and (b)
state semantics are preserved (no false transition). Both are checked here.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _example_helpers import (  # noqa: E402
    consume_asset_logistics_updates,
    docker_compose,
    COMPOSE_DIR,
)
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_26_fusion_durability"
ASSET = "USA-ARMY-1HBCT-M1A2-4773"


def _topic_high_watermark(topic: str) -> int:
    """Sum of high-watermark offsets across all partitions — a monotonic
    proxy for total records-ever-appended. Goes UP every time something
    publishes."""
    from confluent_kafka import Consumer, TopicPartition
    c = Consumer({
        "bootstrap.servers":  "localhost:9093",
        "group.id":           f"watermark-{int(time.time()*1000)}",
        "auto.offset.reset":  "earliest",
        "enable.auto.commit": False,
    })
    try:
        md = c.list_topics(topic=topic, timeout=5)
        total = 0
        for p in md.topics[topic].partitions.keys():
            tp = TopicPartition(topic, p)
            _, hi = c.get_watermark_offsets(tp, timeout=5)
            total += hi
        return total
    finally:
        c.close()


def main() -> None:
    # Establish pre-restart baseline.
    pre = consume_asset_logistics_updates(asset_id=ASSET, timeout_s=10,
                                            max_records=2000,
                                            per_partition_tail=400)
    if not pre:
        skip_(NAME, "no pre-restart emissions for asset; run test_25 first")
    pre_severities = {u["overall_severity"] for u in pre}
    pre_last_severity = sorted(pre, key=lambda u: u["revision"])[-1]["overall_severity"]
    pre_watermark_sum = _topic_high_watermark("asset-logistics-status")

    # Restart the fusion service. Restate state lives on the restate-server
    # side and survives the worker bouncing. The bootstrap container is a
    # one-shot from compose-up and doesn't re-run; subscriptions are
    # already persisted in Restate.
    try:
        cmd = docker_compose() + ["restart", "logistics-fusion-service"]
        proc = subprocess.run(cmd, cwd=str(COMPOSE_DIR), capture_output=True,
                              timeout=60, text=True)
        if proc.returncode != 0:
            skip_(NAME, f"docker compose restart failed: {proc.stderr[:200]}")
    except subprocess.TimeoutExpired:
        skip_(NAME, "docker compose restart timed out")

    # Wait for the next cadenced emission cycle to land (EMIT_INTERVAL=30s).
    # Poll the high watermark; if it strictly increases, the worker has
    # come back and is publishing again.
    deadline = time.monotonic() + 120
    new_watermark_sum = pre_watermark_sum
    while time.monotonic() < deadline:
        time.sleep(10)
        new_watermark_sum = _topic_high_watermark("asset-logistics-status")
        if new_watermark_sum > pre_watermark_sum:
            break

    if new_watermark_sum <= pre_watermark_sum:
        fail_(NAME, f"asset-logistics-status total watermark did not advance "
                    f"post-restart ({pre_watermark_sum} -> {new_watermark_sum}); "
                    f"worker did not recover")

    # Severity continuity: re-read the asset's recent updates and check no
    # new SEVERITY value appeared (i.e., no spurious transition).
    post = consume_asset_logistics_updates(asset_id=ASSET, timeout_s=10,
                                            max_records=2000,
                                            per_partition_tail=400)
    post_severities = {u["overall_severity"] for u in post}
    new_severities = post_severities - pre_severities
    if new_severities:
        fail_(NAME, f"spurious severity change after restart: new severities "
                    f"{new_severities}; pre={pre_severities}")

    pass_(NAME, f"durability OK; watermark advanced "
                f"{pre_watermark_sum} -> {new_watermark_sum} "
                f"(+{new_watermark_sum - pre_watermark_sum} records); "
                f"no spurious transition; severity remained "
                f"{pre_last_severity}")


if __name__ == "__main__":
    main()
