"""
Example bundle test runner.

Runs the 8 sim-a / egress / fusion-lifecycle Hero Scenario v3 tests.
Requires the example overlay to be running (docker-compose.customer.yml
on top of the OSS compose). See ../README.md for usage.

The proprietary-feed tests (17, 18, 22) live in the private bundle
(openddil-customer-bundle-customer-overlay/tests/) and are NOT runnable from
this overlay alone — they need the private overlay to be layered.

Exit code:
  0 = all PASS or SKIP
  1 = any FAIL
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
PY = sys.executable

TESTS = [
    "test_19_silver_feed_agnostic.py",
    "test_20_sim_a_passthrough.py",
    "test_21_sim_a_silver.py",
    "test_23_faust_windows.py",
    "test_25_fusion_lifecycle.py",
    "test_26_fusion_durability.py",
    "test_27_egress_default_vocab.py",
    "test_28_egress_vocab_swap.py",
]


def run_one(script: str) -> tuple[str, str, str]:
    path = HERE / script
    proc = subprocess.run([PY, str(path)], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    last = out.splitlines()[-1] if out else ""
    if last.startswith("PASS:"):
        verdict = "PASS"
    elif last.startswith("SKIP:"):
        verdict = "SKIP"
    else:
        verdict = "FAIL"
    return script, verdict, last or "<no output>"


def main() -> int:
    print("Hero Scenario v3 (Example Overlay) — Sim-A + Fusion + Egress")
    print("=" * 70)
    results = []
    for t in TESTS:
        print(f"... running {t}")
        results.append(run_one(t))

    print()
    print("Summary")
    print("-" * 70)
    n_pass = sum(1 for _, v, _ in results if v == "PASS")
    n_skip = sum(1 for _, v, _ in results if v == "SKIP")
    n_fail = sum(1 for _, v, _ in results if v == "FAIL")
    for name, verdict, line in results:
        marker = {"PASS": "[OK]", "SKIP": "[~~]", "FAIL": "[XX]"}[verdict]
        print(f"  {marker} {name:42s} {verdict:5s}  {line}")
    print()
    print(f"PASS: {n_pass}   SKIP: {n_skip}   FAIL: {n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
