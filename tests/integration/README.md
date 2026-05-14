# Integration Test Runner — Composed OSS + Customer Overlay

This runner exercises the **composed** OpenDDIL deployment: the OSS base
compose layered with the customer overlay. It exists because individual
runners can't catch a whole class of bug.

## The gap this closes

Today there are two test runners:

- `openddil/openddil-demo/tests/hero_scenario_v3/run_all.py` — 16 DIS-driven
  tests against the OSS stack only.
- `openddil-customer-bundle-example/tests/run_all.py` — 11 customer-feed tests
  against the customer overlay tests in isolation.

Neither runs against the composed deployment. So this can happen:

1. Someone adds a new required field to `EntityTelemetryEvent` in OSS.
2. OSS Hero Scenario v3 passes — the new field isn't referenced anywhere
   OSS-side yet.
3. Customer overlay tests pass against the **previous** OSS version still
   on disk.
4. The new OSS version ships. The overlay layers on top. The overlay
   Bloblang doesn't fill the new field. Customer deployments break.

The integration runner catches that class of bug by composing both stacks
and running everything against the merged deployment.

## What the runner does

1. **Brings up** the composed stack:
   ```
   docker compose \
     -f <openddil>/openddil-demo/docker-compose.yml \
     -f <openddil-customer-bundle>/docker-compose.customer.yml \
     up -d
   ```
   (No `-p`; the runner takes over the default `openddil-demo` project name
   and tears down at the end.)

2. **Waits for ready** — layered probes:
   - Every expected container is `running` (or `exited 0` for one-shots).
   - Every required Kafka topic exists on the broker.
   - cm-service, logistics-fusion-service, proprietary-ingest HTTP
     endpoints reachable; RabbitMQ AMQP port reachable.

3. **Phase A — OSS Hero Scenario v3 (16 tests)** — subprocesses out to
   the existing OSS runner. These must still pass; if not, the overlay
   broke OSS behavior.

4. **Phase B — Customer overlay (11 tests)** — subprocesses out to the
   existing customer runner. These must pass because OSS is healthy.

5. **Phase C — Integration-only tests (4 tests)** — only meaningful
   against the composed stack:

   - **`test_integration_dual_feed_same_asset`** — DIS PDU and sim-a AMQP
     for the same physical hull (entity `1:1:4773` ↔ vehicle `SIM-A-001`
     ↔ canonical `USA-ARMY-1HBCT-M1A2-4773`). Both Silver events present.
     A future strict assertion (commented) tightens to "same canonical
     asset_id" once ADR-0015 (identity-resolver service) lands.

   - **`test_integration_cm_and_logistics_emit_together`** — same asset
     gets both a CM-derived CRITICAL condition AND a fuel-derived CRITICAL.
     Verifies cm-service emits CloudEvent on `tactical-events`,
     fusion-service emits AssetLogisticsStatusUpdate on
     `asset-logistics-status`, and the egress AMQP message lands on
     `battle-mgmt.asset-status`. Proves the two state machines + the egress
     pipeline run independently and reach their downstream consumers.

   - **`test_integration_oss_silver_schema_unchanged`** — captures a Silver
     event from `raw-sensor-stream` and verifies every OSS-defined field
     path resolves. Trip-wire for OSS schema drift. Also asserts
     Quantity's wire-name is `openddil.common.v1.Quantity` (catches a
     regression on the Phase 3.5 refactor).

   - **`test_integration_overlay_egress_intact`** — drives a CRITICAL
     transition, consumes the resulting AMQP message from
     `battle-mgmt.asset-status`, parses JSON, verifies every customer-
     promised field is present (schema_version, message_type, asset_id,
     platform, status, is_transition, is_initial, revision, computed_at,
     factors) plus the routing-key contract. Trip-wire for overlay
     Bloblang drift.

6. **Tears down** (`docker compose down -v --remove-orphans`).

## Usage

```bash
# Standard run — bring up, test, tear down
py -3 openddil-customer-bundle-example/tests/integration/run_all.py

# Skip bring-up (stack already running)
py -3 ... run_all.py --no-bring-up

# Keep stack up for post-run debug
py -3 ... run_all.py --keep-up

# Just Phase C (the new integration-only tests)
py -3 ... run_all.py --only-integration
```

## Maintenance contract

Run this BEFORE:

- Cutting a release of either the OSS core or the customer overlay.
- Pushing a Silver schema change in `openddil-contracts/proto/openddil/telemetry/v1/telemetry.proto`.
- Pushing an overlay Bloblang change in
  `openddil-customer-bundle-example/dynamic-mappings/`.
- Merging a PR that touches `openddil-base-connect.yaml` or any
  Connect-pipeline file.

When the OSS Silver schema changes, the integration runner is the gate
that proves the overlay still composes against the new schema. When
customer ICDs land and overlay YAMLs update, the integration runner
proves the new overlay still works against the current OSS proto.

## Not in scope (yet)

- **CI integration.** This is a manual gate before release for now.
  Wiring it into a GitHub Actions cron is a separate cleanup item.
- **Performance / load testing.** The runner asserts correctness, not
  throughput.
- **Network-failure scenarios.** Toxiproxy chaos is exercised by the
  existing OSS test_07; not duplicated here.

## Known soft-failures (deferred per ADR)

- `test_integration_dual_feed_same_asset`'s strict cross-feed asset_id
  reconciliation assertion is commented out pending ADR-0015 (identity-
  resolver service). The test still passes today by verifying both feeds
  produce Silver events for the same physical hull; the canonical-
  reconciliation tightening will go live when the resolver service does.
