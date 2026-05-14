"""
Composed-stack integration runner.

Brings up the OSS base compose layered with the customer overlay, waits
for ready, runs three test phases against the same live stack, then
tears down.

Phase A — OSS Hero Scenario v3 (16 tests)
   Run the existing OSS-only runner. These must still pass against the
   composed deployment; if they don't, the overlay broke OSS behavior.

Phase B — Customer overlay tests (11 tests)
   Run the existing customer-only runner. These must pass because OSS
   is healthy and the overlay is layered on top.

Phase C — Integration-only tests (4 tests)
   New tests that ONLY make sense against the composed stack — both
   feeds simultaneously, both services emitting together, schema-drift
   trip-wires.

Exit code:
   0 — every test PASS or SKIP
   1 — any FAIL, OR bring-up failed, OR ready-wait timed out

Usage:
   py -3 openddil-customer-bundle-example/tests/integration/run_all.py
   py -3 ... run_all.py --no-bring-up        # stack already running
   py -3 ... run_all.py --keep-up            # leave stack up for debug
   py -3 ... run_all.py --skip-oss           # skip Phase A
   py -3 ... run_all.py --skip-overlay       # skip Phase B
   py -3 ... run_all.py --only-integration   # skip A + B, just C
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _compose_helpers import (  # noqa: E402
    BUNDLE_ROOT,
    OPENDDIL_ROOT,
    bring_up,
    tear_down,
    wait_for_ready,
)

PY = sys.executable

OSS_RUNNER     = OPENDDIL_ROOT / "openddil-demo" / "tests" / "hero_scenario_v3" / "run_all.py"
OVERLAY_RUNNER = BUNDLE_ROOT / "tests" / "run_all.py"

INTEGRATION_TESTS = [
    "test_integration_dual_feed_same_asset.py",
    "test_integration_cm_and_logistics_emit_together.py",
    "test_integration_oss_silver_schema_unchanged.py",
    "test_integration_overlay_egress_intact.py",
]


def _run_subprocess(label: str, cmd: list[str]) -> tuple[str, int, str]:
    print(f"\n========== {label} ==========", flush=True)
    print(f"  cmd: {' '.join(cmd[:3])}...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # Echo the tail of stdout for visibility.
    out = (proc.stdout or "").strip()
    last_lines = "\n".join(out.splitlines()[-50:])
    if last_lines:
        print(last_lines, flush=True)
    return label, proc.returncode, out


def _run_integration_test(script: str) -> tuple[str, str, str]:
    path = HERE / script
    proc = subprocess.run([PY, str(path)], capture_output=True, text=True)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    last = out.splitlines()[-1] if out else ""
    if last.startswith("PASS:"):
        verdict = "PASS"
    elif last.startswith("SKIP:"):
        verdict = "SKIP"
    else:
        verdict = "FAIL"
    return script, verdict, last or "<no output>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-bring-up", action="store_true",
                    help="skip docker compose up (stack must already be ready)")
    ap.add_argument("--keep-up", action="store_true",
                    help="don't tear down at the end (for debug)")
    ap.add_argument("--skip-oss", action="store_true",
                    help="skip Phase A (OSS Hero Scenario v3)")
    ap.add_argument("--skip-overlay", action="store_true",
                    help="skip Phase B (customer overlay tests)")
    ap.add_argument("--only-integration", action="store_true",
                    help="skip Phase A and Phase B; run only the 4 integration tests")
    ap.add_argument("--bring-up-timeout", type=int, default=360,
                    help="seconds to wait for the stack to be ready (default 360)")
    args = ap.parse_args()

    if args.only_integration:
        args.skip_oss = True
        args.skip_overlay = True

    started_stack = False
    bring_up_t0 = time.monotonic()

    if not args.no_bring_up:
        print("==========  composed-stack bring-up  ==========", flush=True)
        rc = bring_up()
        if rc != 0:
            print(f"FATAL: docker compose up returned {rc}", flush=True)
            return 1
        started_stack = True

    ok, msg = wait_for_ready(timeout_s=args.bring_up_timeout)
    bring_up_dur = time.monotonic() - bring_up_t0
    if not ok:
        print(f"FATAL: stack not ready ({msg}) after {bring_up_dur:.0f}s",
              flush=True)
        if not args.keep_up and started_stack:
            tear_down()
        return 1
    print(f"[integration] stack ready in {bring_up_dur:.0f}s — {msg}",
          flush=True)

    # ----------------------------------------------------------------------
    # Phase A — OSS Hero Scenario v3
    # ----------------------------------------------------------------------
    phase_a_rc = 0
    phase_a_summary = "(skipped)"
    if not args.skip_oss:
        if not OSS_RUNNER.is_file():
            print(f"FATAL: OSS runner missing at {OSS_RUNNER}", flush=True)
            phase_a_rc = 1
        else:
            label, rc, out = _run_subprocess(
                "Phase A — OSS Hero Scenario v3 (composed stack)",
                [PY, str(OSS_RUNNER)],
            )
            phase_a_rc = rc
            # Pull the summary line from the OSS runner output
            for line in out.splitlines()[::-1]:
                if line.startswith("PASS:") and "FAIL:" in line:
                    phase_a_summary = line
                    break
            else:
                phase_a_summary = (
                    out.splitlines()[-1] if out else "<no output>"
                )

    # ----------------------------------------------------------------------
    # Phase B — Customer overlay tests
    # ----------------------------------------------------------------------
    phase_b_rc = 0
    phase_b_summary = "(skipped)"
    if not args.skip_overlay:
        if not OVERLAY_RUNNER.is_file():
            print(f"FATAL: overlay runner missing at {OVERLAY_RUNNER}",
                  flush=True)
            phase_b_rc = 1
        else:
            label, rc, out = _run_subprocess(
                "Phase B — Customer overlay (composed stack)",
                [PY, str(OVERLAY_RUNNER)],
            )
            phase_b_rc = rc
            for line in out.splitlines()[::-1]:
                if line.startswith("PASS:") and "FAIL:" in line:
                    phase_b_summary = line
                    break
            else:
                phase_b_summary = (
                    out.splitlines()[-1] if out else "<no output>"
                )

    # ----------------------------------------------------------------------
    # Phase C — Integration-only tests
    # ----------------------------------------------------------------------
    print("\n========== Phase C — Integration-only tests ==========",
          flush=True)
    phase_c_results = []
    for t in INTEGRATION_TESTS:
        print(f"... running {t}", flush=True)
        phase_c_results.append(_run_integration_test(t))
    n_pass = sum(1 for _, v, _ in phase_c_results if v == "PASS")
    n_skip = sum(1 for _, v, _ in phase_c_results if v == "SKIP")
    n_fail = sum(1 for _, v, _ in phase_c_results if v == "FAIL")
    phase_c_rc = 1 if n_fail else 0

    # ----------------------------------------------------------------------
    # Tear-down
    # ----------------------------------------------------------------------
    if started_stack and not args.keep_up:
        print("\n==========  composed-stack tear-down  ==========",
              flush=True)
        td_rc = tear_down()
        if td_rc != 0:
            print(f"[integration] WARNING: tear-down returned {td_rc}",
                  flush=True)

    # ----------------------------------------------------------------------
    # Report
    # ----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("INTEGRATION SUMMARY")
    print("=" * 70)
    print(f"Phase A (OSS Hero Scenario v3, composed stack):")
    print(f"  {phase_a_summary}")
    print(f"  rc={phase_a_rc}")
    print()
    print(f"Phase B (Customer overlay, composed stack):")
    print(f"  {phase_b_summary}")
    print(f"  rc={phase_b_rc}")
    print()
    print(f"Phase C (Integration-only):")
    for name, verdict, line in phase_c_results:
        marker = {"PASS": "[OK]", "SKIP": "[~~]", "FAIL": "[XX]"}[verdict]
        print(f"  {marker} {name:55s} {verdict:5s}  {line}")
    print(f"  PASS: {n_pass}   SKIP: {n_skip}   FAIL: {n_fail}")
    print()
    overall_rc = phase_a_rc or phase_b_rc or phase_c_rc
    print(f"OVERALL: {'PASS' if overall_rc == 0 else 'FAIL'} "
          f"(rc={overall_rc})")
    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())
