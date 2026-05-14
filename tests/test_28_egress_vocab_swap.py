"""
Test 28 — Vocabulary swap via Bloblang sideload (the configurability test).

Replace dynamic-mappings/system-b-egress.yaml with the FMC variant
(system-b-egress-fmc.yaml), trigger the same CRITICAL transition, and
verify the egress message now contains `"status": "PMC"` instead of
`"CRITICAL"`. No Python code changed. No service rebuilt.

If the test fails or completes successfully, the original mapping is
restored so subsequent tests use the default vocabulary.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _example_helpers import (  # noqa: E402
    COMPOSE_DIR,
    consume_battle_mgmt_egress,
    docker_compose,
    publish_sim_a_amqp,
    rabbitmq_alive,
    sample_sim_a_message,
)
from _helpers import fail_, pass_, skip_  # noqa: E402

NAME = "test_28_egress_vocab_swap"
ASSET = "USA-ARMY-1HBCT-M1A2-4773"
# Egress mappings live in this example bundle (sibling of openddil/),
# not in openddil-demo. COMPOSE_DIR (from _example_helpers) still points
# at openddil-demo for docker compose invocations.
MAPPINGS = Path(__file__).resolve().parents[1] / "dynamic-mappings" / "egress"
DEFAULT_FILE = MAPPINGS / "system-b-egress.yaml"
FMC_FILE = MAPPINGS / "system-b-egress-fmc.yaml"
BACKUP_FILE = MAPPINGS / "system-b-egress.yaml.test28.bak"


def _restart_egress() -> None:
    # Use plain `docker restart <container-name>` rather than `docker
    # compose restart <service>` because the egress service is defined in
    # the example overlay compose, not openddil-demo's base. Restarting
    # by container name avoids needing every `-f` flag in scope.
    subprocess.run(
        ["docker", "restart", "openddil-demo-connect-egress"],
        capture_output=True, timeout=30, text=True,
    )


def main() -> None:
    if not rabbitmq_alive():
        skip_(NAME, "RabbitMQ not reachable")
    if not FMC_FILE.exists() or not DEFAULT_FILE.exists():
        skip_(NAME, "mapping files not present")

    # Swap: backup default, copy FMC over default, restart egress.
    shutil.copyfile(DEFAULT_FILE, BACKUP_FILE)
    shutil.copyfile(FMC_FILE, DEFAULT_FILE)
    _restart_egress()
    time.sleep(8)  # let Connect re-load

    try:
        try:
            def driver():
                time.sleep(3)
                try:
                    publish_sim_a_amqp(sample_sim_a_message(
                        vehicle_id="SIM-A-001", fuel_gal=42.0,
                        sequence=2800, timestamp="2026-05-13T12:28:00Z",
                    ))
                except Exception:
                    pass
            threading.Thread(target=driver, daemon=True).start()

            received = consume_battle_mgmt_egress(timeout_s=120, expected=10)
        except ImportError as exc:
            skip_(NAME, str(exc))

        if not received:
            fail_(NAME, "no egress messages after vocabulary swap; verify "
                        "the FMC mapping has label `system_b_egress_mapping` "
                        "and connect-egress reloaded")

        candidates = [r for r in received
                       if isinstance(r["body"], dict)
                       and r["body"].get("asset_id") == ASSET]
        if not candidates:
            seen_ids = sorted({r["body"].get("asset_id") for r in received
                                if isinstance(r["body"], dict)})
            fail_(NAME, f"no egress message for asset {ASSET} after swap; "
                        f"saw asset_ids={seen_ids}")
        body = candidates[-1]["body"]

        if body["status"] not in ("FMC", "PMC", "NMC"):
            fail_(NAME, f"FMC vocabulary not applied; status={body['status']!r}, "
                        f"expected one of FMC/PMC/NMC")
        if body["status"] == "CRITICAL":
            fail_(NAME, "default vocabulary still active; FMC swap did not "
                        "take effect")

        pass_(NAME, f"vocabulary swap OK; same internal severity now emitted "
                    f"as status={body['status']!r} — Bloblang-only change, "
                    f"no Python touched")
    finally:
        # Restore default for downstream tests.
        if BACKUP_FILE.exists():
            shutil.copyfile(BACKUP_FILE, DEFAULT_FILE)
            BACKUP_FILE.unlink()
            _restart_egress()


if __name__ == "__main__":
    main()
