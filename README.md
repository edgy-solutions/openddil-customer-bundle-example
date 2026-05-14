# OpenDDIL Customer Overlay — Worked Example

This is a **publishable** worked example of the OpenDDIL customer
overlay pattern. Everything in this directory uses **placeholder shapes**
— invented JSON field names, invented hull identifiers, invented vendor
names. No real customer ICD is encoded here.

The OSS OpenDDIL demo (in [`../openddil/openddil-demo/`](../openddil/openddil-demo/))
runs DIS-only out of the box. This overlay layers two additional feeds
on top — a sim-a AMQP-delivered placeholder vehicle simulator and a
System B AMQP egress for battle-management consumers — to demonstrate
the full customer-overlay pattern end-to-end.

This repo lives at `edgy-solutions/openddil-customer-bundle-example` on
GitHub and is checked out under `openddil/openddil-customer-bundle-example/`
alongside the other OSS OpenDDIL repos.

**Forking for a real customer**: copy this whole directory to a sibling
OUTSIDE the `openddil/` workspace folder (e.g.,
`openddil-customer-bundle-<your-customer>/`) and treat THAT copy as
private — never inside `openddil/`, since everything under `openddil/` is
OSS. Replace the placeholders with the real customer's ICD shapes,
ontology entries, and feed-specific Bloblang. See
[`../../openddil-customer-bundle-customer-overlay/`](../../openddil-customer-bundle-customer-overlay/)
for a real example of a private companion bundle (proprietary HTTP feed
with the customer's `SensorMessage` schema baked in).

## Layout

```
openddil-customer-bundle-example/
├── README.md (this file)
├── LICENSE                                  ← TBD, match OSS repos
├── docker-compose.customer.yml              ← rabbitmq + sim-a + egress only
├── connect/
│   ├── openddil-customer-connect.yaml       ← sim-a Bronze → Silver
│   ├── connect-sim-a-amqp.yaml              ← AMQP → Kafka transport (sim-a)
│   └── connect-egress.yaml                  ← Kafka → AMQP egress (System B)
├── dynamic-mappings/
│   ├── sim-a-mapping.yaml                   ← placeholder sim-a shape → Silver
│   └── egress/
│       ├── system-b-egress.yaml             ← default OK/DEGRADED/CRITICAL vocabulary
│       └── system-b-egress-fmc.yaml         ← alt FMC/PMC/NMC vocabulary swap
├── ontology/
│   ├── asset_identity_aliases.yaml          ← dis: + sim_a: placeholder aliases
│   └── platform_variant_aliases.yaml        ← sim_a: placeholder variants
├── schemas/
│   └── (empty — real customer schemas live in private bundles)
└── tests/
    ├── _example_helpers.py                  ← sim-a + egress + generic-infra helpers
    ├── run_all.py                           ← runs the 8 sim-a/fusion/egress tests
    ├── test_19_silver_feed_agnostic.py      ← DIS + sim-a multi-protocol Silver
    ├── test_20-28_*.py                      ← sim-a tests
    └── integration/                         ← composed-stack test runner
        ├── README.md
        ├── _compose_helpers.py
        ├── run_all.py                       ← OSS Hero v3 + overlay + integration tests
        └── test_integration_*.py            ← 4 cross-boundary regression checks
```

## Running

**OSS demo alone (no overlay):**

```bash
docker compose -f openddil/openddil-demo/docker-compose.yml up
```

This is the publishable, DIS-only mode. Customers fork the demo and add
their own overlays on top.

**OSS + example overlay** (this bundle's sim-a + System B paths):

```bash
docker compose \
  -f openddil/openddil-demo/docker-compose.yml \
  -f openddil/openddil-customer-bundle-example/docker-compose.customer.yml \
  up
```

Run the example tests:

```bash
py -3 openddil/openddil-customer-bundle-example/tests/run_all.py
```

Or the full composed-stack integration runner (recommended pre-release
gate — exercises OSS Hero v3 + example overlay + 4 integration tests
against the composed deployment):

```bash
py -3 openddil/openddil-customer-bundle-example/tests/integration/run_all.py
```

**OSS + example + private overlay**: see the private bundle's README at
`openddil-customer-bundle-<customer>/README.md`. Multiple overlays
layer via additional `-f` flags on the same `docker compose` command.

## Lifecycle — forking for a real customer

1. Copy this whole directory to `openddil-customer-bundle-<customer>/`.
2. Add a real LICENSE / "DO NOT PUBLISH" notice to the new bundle's
   README. (The whole copied directory should be treated as customer-
   encumbered.)
3. Replace the placeholder Bloblang in `dynamic-mappings/sim-a-mapping.yaml`
   with the real customer's feed shape (or rename and create new mapping
   files for non-AMQP feeds).
4. Replace the placeholder ontology entries in `ontology/*.yaml` with
   real customer alias data.
5. Replace the placeholder egress envelope in
   `dynamic-mappings/egress/system-b-egress.yaml` with the real customer's
   ICD.
6. If the real customer has a non-AMQP feed (HTTP, custom protocol):
   - Add a `sensor-ingest/<feed>_ingestor.py` Python sidecar.
   - Add a Connect Bronze→Silver config in `connect/`.
   - Add the corresponding service block to `docker-compose.customer.yml`.
7. Optionally split into a private bundle (proprietary content) +
   public-template-stripped-clean bundle (just the structure, no
   customer-shape content) — see the customer-overlay + example split for the
   pattern.
8. Set up CI to run the bundle's integration runner as a release gate.

## Why this pattern

Customer ICDs and reference rosters are encumbered (NDA, contract). The
OSS demo and architecture must remain freely shareable. The overlay
pattern lets the OSS code show the architecture (Connect at protocol
boundary, Bloblang at shape boundary, generic Silver everywhere)
without shipping anything customer-controlled.

This **example bundle** is shareable — it demonstrates the overlay
pattern with invented placeholder content. A real customer bundle keeps
the same structure but encodes the real ICD; that bundle is private and
never published.

## Cross-references

- [`openddil/`](../openddil/) — the OSS repos (contracts, demo, services).
- [`openddil-customer-bundle-customer-overlay/`](../openddil-customer-bundle-customer-overlay/) —
  a real private bundle showing the proprietary HTTP-feed pattern.
  **DO NOT PUBLISH** that one; it has the real customer SensorMessage schema.
