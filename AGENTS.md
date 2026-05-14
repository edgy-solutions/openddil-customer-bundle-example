# AGENTS.md — OpenDDIL Customer Overlay (Worked Example)

Guidelines and safety constraints for AI agents working in this repository.

## Repository Scope

This repo (`openddil-customer-bundle-example`) is a **publishable worked
example** of the OpenDDIL customer-overlay pattern. It uses entirely
**placeholder shapes** — invented JSON field names (`sim_a`), invented hull
identifiers, invented vendor names. No real customer ICD lives here.

The OSS OpenDDIL demo runs DIS-only out of the box. This bundle layers two
additional feeds on top — a sim-a AMQP-delivered placeholder vehicle
simulator and a System B AMQP egress for battle-management consumers — to
demonstrate the full customer-overlay pattern end-to-end.

Real customer bundles (with real ICD shapes) live OUTSIDE the `openddil/`
workspace as private repos (e.g., `openddil-customer-bundle-<customer>/`).
This example is the template they fork from.

## What You CAN Do

- **Extend the placeholder feeds** — add new sim-a fields, new ontology
  alias entries, new egress vocabularies. As long as the content stays
  invented, it remains publishable.
- **Add new placeholder feeds** — new AMQP exchanges, new HTTP sidecars,
  new mappings. Keep the field names obviously invented.
- **Improve the test harness** — `tests/_example_helpers.py` re-exports
  OSS generic helpers; private bundles cross-import from here, so any
  new public helpers benefit them too.
- **Improve the integration runner** — `tests/integration/` orchestrates
  OSS base + this overlay end-to-end. Adding new cross-boundary
  regression checks is welcome.
- **Update the README / lifecycle doc** when the fork-for-real-customer
  steps change.

## What You MUST NOT Do

- ❌ **Never encode a real customer ICD here.** Field names like
  `parentUnitId`, `azimuthFOV`, real radar serial numbers, or any
  identifier that came from a real customer's documentation belong in a
  PRIVATE bundle, not this one. If unsure, ask.
- ❌ **Never reference a private bundle by name in code or compose
  files.** Private bundle names (customer codenames) MUST NOT appear in
  this repo's history. Cross-references in the README point at the
  *example* of a private bundle structure, not at any specific real one.
- ❌ **Never add a `build:` directive** to `docker-compose.customer.yml`.
  This overlay only references registry images (`ghcr.io/...`) and
  bind-mounts source files for Connect configs. The OSS base+override
  pattern stays unchanged.
- ❌ **Never break the cross-import contract.** The private bundles
  cross-import `_example_helpers.py` for OSS generic plumbing and
  sim-a publishers. If you rename or remove a public helper here, you
  break every private bundle in the wild — coordinate first.
- ❌ **Never commit secrets, real hull rosters, real frequency tables,
  or any customer-encumbered artifact.** This repo is public.

## Layout

```
openddil-customer-bundle-example/
├── README.md
├── LICENSE                                  ← MIT
├── docker-compose.customer.yml              ← rabbitmq + sim-a + egress
├── connect/                                 ← Redpanda Connect configs
├── dynamic-mappings/                        ← Bloblang Bronze→Silver + egress
├── ontology/                                ← placeholder asset+variant aliases
├── schemas/                                 ← empty; private bundles ship their own
└── tests/
    ├── _example_helpers.py                  ← sim-a + egress + OSS re-exports
    ├── run_all.py                           ← unit-style overlay tests
    └── integration/                         ← composed-stack runner + 4 tests
```

## Docker Compose Conventions (cross-repo rule)

When this overlay is layered onto `openddil-demo/docker-compose.yml`:

- Docker Compose resolves relative paths in ALL `-f` files relative to the
  **first** `-f` file's directory. The first file is always
  `openddil-demo/docker-compose.yml`. Paths in `docker-compose.customer.yml`
  therefore use `../openddil-customer-bundle-example/...`.
- The overlay references registry images only
  (`ghcr.io/edgy-solutions/openddil/...`, `docker.redpanda.com/...`,
  `rabbitmq:3.13-management`). No `build:` directives.
- The integration runner brings the stack up under the default
  `openddil-demo` compose project name. The runner tears down at the
  end with `-v --remove-orphans`.

## Tests

`tests/run_all.py` runs the 8 overlay-only unit-ish tests (sim-a
mapping, multi-protocol Silver, egress vocabulary swap, etc.) against a
running stack.

`tests/integration/run_all.py` is the composed-stack regression gate:

1. Brings up OSS base + this overlay via `docker compose`.
2. Waits for containers, Kafka topics, and HTTP/AMQP endpoints.
3. Runs OSS Hero v3 tests + overlay tests + 4 cross-boundary
   integration checks.
4. Tears down (`down -v --remove-orphans`).

This is the recommended pre-release gate. CI runs `docker compose
config --quiet` against the four canonical layering modes as a
syntactic smoke check (see `.github/workflows/compose-validate.yml`).

## Documentation Maintenance

After ANY structural change, update:
1. `README.md` — overlay layout, fork-for-real-customer lifecycle.
2. `llms.txt` — high-level summary for downstream LLM context.
3. `.cursorrules` — only if new conventions are introduced.
4. This file — only if new safety constraints apply.
