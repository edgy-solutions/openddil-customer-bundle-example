#!/usr/bin/env python3
"""
dis-sim — a minimal DIS Entity State PDU generator for pipeline validation.

WHY THIS EXISTS
---------------
The OpenDDIL pipeline's front door is IEEE 1278.1 DIS over UDP. Validating
the pipeline end-to-end therefore needs DIS traffic — and a lab or a pilot
site that has no upstream simulator has no data at all, which from inside the
cluster is indistinguishable from a broken deployment.

This is NOT a simulation. It has no physics, no behaviours, no scenario
model. It emits well-formed EntityState PDUs for N entities on a heartbeat,
which is precisely — and only — what is needed to prove that the chain
    UDP -> sensor-ingest -> decode -> Bloblang -> Silver -> projector
         -> tier store -> fusion -> bridge -> buffer -> sever -> drain
carries real traffic. For anything requiring behaviour, use a real CGF
(VR-Forces, mixr).

WHY THE FRONT DOOR RATHER THAN INJECTING MID-PIPELINE
-----------------------------------------------------
Producing synthetic records straight onto `raw-sensor-stream` would skip the
decode and mapping stages, and would require reproducing an INTERNAL wire
shape by inspection — a provenance question with no published answer. Entering
at the DIS socket means the wire format is an OPEN PUBLISHED STANDARD that the
pipeline already decodes, so there is nothing to reverse-engineer and no
asterisk on the result.

Mid-pipeline `rpk produce` remains useful for debugging a single stage. It is
not a proof path.

SERIALIZATION
-------------
Uses `opendis` — THE SAME LIBRARY `dis_ingestor.py` DECODES WITH. Wire
compatibility is therefore true by construction rather than by agreement
between two hand-written implementations. This mirrors
openddil-sensor-ingest/fixtures/generate_fixtures.py, which established the
pattern after an earlier hand-rolled hex blob turned out to be unparseable.

ENTITY TYPES ARE READ, NOT INVENTED
-----------------------------------
Every entity type below appears in openddil-contracts/ontology/
dis_entity_types.yaml. An unrecognised 7-tuple does not error — it falls to
the `_default` entry and becomes UNKNOWN, which then propagates as an asset
with no platform metadata and effectively disappears from meaningful display.
That failure is silent, so the enumerations here were taken from that file
rather than constructed from the SISO-REF-010 conventions by hand.

Verify coverage before pointing a real CGF at this pipeline:
    python dis_sim.py --list-types

COORDINATES ARE SYNTHETIC
-------------------------
Positions are generated around a fictional training area and carry no
relationship to any real installation, unit, or operation. Callsigns are
likewise invented and follow the sample overlay's fictional naming.

MULTI-TARGET FAN-OUT (--targets / DIS_TARGETS)
-----------------------------------------------
A real DIS deployment often puts every receiver on one shared multicast
group, so every entity reaches every listener at once. This tool talks
unicast UDP instead, so --targets is the unicast stand-in: a comma-
separated host:port list that every PDU goes to, as if they were all on
that shared segment. Leave it unset and --host/--port behave exactly as
before (one destination, unchanged). --targets and a non-default --host
cannot both be set -- there would be no single answer for which one the
operator meant.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import signal
import socket
import sys
import time
from io import BytesIO

try:
    from opendis.dis7 import (
        EntityStatePdu,
        RemoveEntityPdu,
        EventReportPdu,
        DataPdu,
        ElectromagneticEmissionsPdu,
        EmissionSystemRecord,
        EmissionSystemBeamRecord,
        EmitterSystem,
        EEFundamentalParameterData,
        FixedDatum,
        VariableDatum,
        FirePdu,
        DetonationPdu,
        ResupplyReceivedPdu,
        SupplyQuantity,
        EntityID,
        EventIdentifier,
        SimulationAddress,
        MunitionDescriptor,
        EntityType,
        Vector3Double,
    )
    from opendis.DataOutputStream import DataOutputStream
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "opendis is required: pip install opendis==1.0\n"
        "(the same version openddil-sensor-ingest decodes with)\n"
    )
    raise

LOG = logging.getLogger("dis-sim")

# ---------------------------------------------------------------------------
# The built-in type list: one tuple per platform, each of them a key in
# openddil-contracts/ontology/dis_entity_types.yaml. The ontology can hold
# more than one key for a platform (the MQ-9A as both Reaper and Predator B);
# this list emits one.
#
# Key order is the DIS 7-tuple:
#   (kind, domain, country, category, subcategory, specific, extra)
#
# A tuple here that is not in the ontology is not an error here, it is an
# invisible asset there. Position in this list means nothing: ENTITY_PLATFORMS
# below decides which entity id is which platform.
# ---------------------------------------------------------------------------
RECOGNISED_TYPES: list[tuple[tuple[int, int, int, int, int, int, int], str]] = [
    ((1, 1, 225, 1, 1, 2, 0), "M1A1"),
    ((1, 1, 225, 1, 1, 18, 0), "M1A2-SEPv3"),
    ((1, 1, 225, 2, 1, 9, 0), "M2A3-Bradley"),
    ((1, 1, 225, 6, 1, 32, 1), "HMMWV-M1151A1"),
    ((1, 2, 225, 20, 1, 7, 0), "AH-64E-V6"),
    ((1, 2, 225, 21, 2, 26, 0), "UH-60M"),
    ((1, 2, 225, 23, 1, 9, 0), "CH-47F-BlockII"),
    ((1, 2, 225, 1, 12, 1, 0), "F-35A-Block4"),
    ((1, 2, 225, 1, 3, 3, 4), "F-16C-Block50"),
    ((1, 2, 225, 50, 34, 1, 0), "MQ-9A-Block5"),
]

# ---------------------------------------------------------------------------
# THE ENUMERATION LIST IS A DATA DROP (VR-Forces readiness)
# ---------------------------------------------------------------------------
# The list above is a DEFAULT, not the contract. A scenario names the entity
# types it will actually emit, and that list arrives as data — from whoever
# owns the scenario — rather than being transcribed into this file by
# somebody reading a document.
#
# Set DIS_ENTITY_TYPES_PATH to a JSON file shaped as:
#
#     [
#       {"type": [1, 1, 225, 1, 1, 18, 0], "variant": "M1A2-SEPv3"},
#       {"type": [2, 2, 225,  1, 8,  0, 0], "variant": "Javelin"}
#     ]
#
# `kind` is the first element. **kind=2 is MUNITION**, and the ontology
# currently recognises zero of them (GD-11) — which is exactly why the list
# must be able to carry them before anyone can measure the gap. This loader
# takes whatever it is given; it does not filter by kind, and it does not
# know which kinds are "supposed" to appear.
#
# ⚠ NOTHING IS INVENTED HERE. If the file is absent the default list above
# is used unchanged, so this is inert until a real scenario list exists. A
# placeholder munition entry would be worse than none: it would make the
# coverage query report progress that no scenario had asked for.
def load_entity_types(
    path: str | None = None,
) -> list[tuple[tuple[int, int, int, int, int, int, int], str]]:
    """Scenario enumeration list, or the built-in default when absent.

    Fails LOUDLY on a malformed file rather than silently falling back: a
    scenario list that was supplied and then ignored is the worst outcome,
    because the run looks like it honoured it.
    """
    path = path or os.getenv("DIS_ENTITY_TYPES_PATH", "")
    if not path:
        return RECOGNISED_TYPES
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path}: expected a non-empty JSON list of entity types")
    out: list[tuple[tuple[int, int, int, int, int, int, int], str]] = []
    for i, item in enumerate(raw):
        try:
            tup = tuple(int(v) for v in item["type"])
            variant = str(item["variant"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"{path}[{i}]: {exc!r} — need {{'type': [7 ints], 'variant': str}}")
        if len(tup) != 7:
            raise SystemExit(f"{path}[{i}]: DIS entity type is a 7-tuple, got {len(tup)}")
        if not variant:
            raise SystemExit(f"{path}[{i}]: variant must be non-empty")
        out.append((tup, variant))  # type: ignore[arg-type]
    kinds = sorted({t[0] for t, _ in out})
    print(f"dis-sim: loaded {len(out)} entity type(s) from {path}; kinds present: {kinds}",
          flush=True)
    return out


# Resolved ONCE at import: a malformed scenario list must stop the sim at
# start-up, not on the tick that first needs an entity.
ENTITY_TYPES = load_entity_types()

# ---------------------------------------------------------------------------
# EACH ENTITY ID IS PINNED TO ONE PLATFORM
# ---------------------------------------------------------------------------
# The asset id is dis:{site}:{app}:{entity}, and every store downstream keys
# on it: the upsert tables, wear, and the CM baseline that a
# `baseline_assigned` event attached. So an id keeps its platform across
# releases. The map states that platform per id; editing the type list above
# relabels nothing.
#
# Values are variants, resolved against the loaded type list. An id with no
# entry, or a variant the list does not carry exactly once, stops the sim at
# start-up.
#
# Set DIS_ENTITY_PLATFORMS_PATH to replace the map with a JSON object:
#
#     {"dis:1:1:1000": "M1A1", "dis:1:1:1001": "M1A2-SEPv3"}
#
# The built-in map covers the two lab edges in k8s/dis-sim.yaml (site 1 with
# 8 entities, site 2 with 6). 1004 at both sites is the AH-64E-V6; see
# openddil-contracts decisions/FOLLOW-UPS.md "RCV-M stays unmapped".
_LAB_EDGE = [
    "M1A1", "M1A2-SEPv3", "M2A3-Bradley", "HMMWV-M1151A1", "AH-64E-V6",
    "AH-64E-V6", "UH-60M", "CH-47F-BlockII",
]
DEFAULT_ENTITY_PLATFORMS: dict[str, str] = {
    **{f"dis:1:1:{1000 + i}": v for i, v in enumerate(_LAB_EDGE)},
    **{f"dis:2:1:{1000 + i}": v for i, v in enumerate(_LAB_EDGE[:6])},
    # THE UNDECLARED ID. Pinned here and deliberately ABSENT from
    # ontology/releasability.yaml, which is the whole of its job: it is a
    # well-formed asset of a known platform that no declaration covers, so the
    # egress gate must refuse it `unlabelled` rather than admit it or refuse it
    # for a nation reason.
    #
    # Site 1 ON PURPOSE. Site 1 is the ATL fleet's site, so this id looks like
    # an asset that belongs and was simply never declared -- which is the case
    # that actually occurs. A deliberately foreign site would test a different
    # and easier thing, because a reader would expect it to be refused.
    #
    # Entity 1099, not 1008: a contiguous id would be indistinguishable from
    # the emitted fleet growing by one, and the gap is what makes it legible in
    # a log as a thing that was added on purpose.
    #
    # Pinning it is NOT declaring it. The platform map says what an id IS; the
    # releasability declaration says who may SEE it. Keeping the two separate is
    # the point -- an asset can be perfectly well identified and still carry no
    # releasability label, and that is exactly the record this fixture makes.
    "dis:1:1:1099": "HMMWV-M1151A1",
}


def load_entity_platforms(path: str | None = None) -> dict[str, str]:
    """Pinned platform per asset id, or the built-in map when absent."""
    path = path or os.getenv("DIS_ENTITY_PLATFORMS_PATH", "")
    if not path:
        return DEFAULT_ENTITY_PLATFORMS
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"{path}: expected a non-empty JSON object of asset id -> variant")
    for key, variant in raw.items():
        if not isinstance(variant, str) or not variant:
            raise SystemExit(f"{path}: {key!r} needs a non-empty variant string")
    print(f"dis-sim: loaded {len(raw)} pinned platform(s) from {path}", flush=True)
    return dict(raw)


ENTITY_PLATFORMS = load_entity_platforms()


def platform_for(
    site_id: int,
    app_id: int,
    entity_id: int,
    pins: dict[str, str] | None = None,
    types: list[tuple[tuple[int, int, int, int, int, int, int], str]] | None = None,
) -> tuple[tuple[int, int, int, int, int, int, int], str]:
    """The pinned (tuple, variant) for one asset id. Refuses, never guesses."""
    pins = ENTITY_PLATFORMS if pins is None else pins
    types = (ENTITY_TYPES or RECOGNISED_TYPES) if types is None else types
    key = f"dis:{site_id}:{app_id}:{entity_id}"
    variant = pins.get(key)
    if variant is None:
        raise SystemExit(f"{key}: no platform pinned; add it to the platform map "
                         "(DIS_ENTITY_PLATFORMS_PATH or DEFAULT_ENTITY_PLATFORMS)")
    tuples = [t for t, v in types if v == variant]
    if len(tuples) != 1:
        raise SystemExit(f"{key}: pinned variant {variant!r} appears {len(tuples)} "
                         "time(s) in the entity type list; it must appear once")
    return tuples[0], variant

# Fictional callsign stems, consistent with the sample overlay's invented
# naming. Deliberately not drawn from any real unit designation.
CALLSIGN_STEMS = ["NORTHPOINT", "CAPEVERD", "ATLAS", "BEDROCK", "SYLVAN"]

# Synthetic training area. Not a real installation.
BASE_LAT_DEG = 39.0
BASE_LON_DEG = -105.0
SPREAD_DEG = 0.25

# WGS84
_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2 - _WGS84_F)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    """WGS84 geodetic -> ECEF metres, which is what an EntityState PDU carries."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _WGS84_E2) + alt_m) * sin_lat
    return x, y, z



# ---------------------------------------------------------------------------
# Entity appearance — the damage-emission control
# ---------------------------------------------------------------------------
# WHY THIS EXISTS. Until 2026-08-19 this generator hardcoded
# `entityAppearance = 0`, and a measurement of 3000 records across 8 entities
# on the lab found the field zero in every one. That mattered because a
# consumer decoding bits 3-4 of zero reads damage NONE, which maps to
# HEALTH_STATE_NOMINAL -- a POSITIVE assertion of health. A mapping built
# against this generator would have declared every asset healthy on the
# strength of a field nobody set.
#
# So the generator has to be able to SAY something on this axis, deliberately,
# before anything can be built to read it. Sibling of
# logistics_sim.AssetState.as_unclaimed(): both exist so the honest-absence
# path and the positive-claim path are each reachable on purpose.
#
# THE BIT LAYOUT IS DOMAIN-SPECIFIC AND THAT IS THE WHOLE TRAP. Bit 2 is
# firepower-kill for LAND platforms and is reused for an unrelated meaning in
# other domains, so a generator (or decoder) that ignores domain will assert
# firepower kills on aircraft. Encoded per domain here rather than as one
# layout, for the same reason the mapping design puts interpretation in the
# ontology.
#
# Layout is standard-derived and MUST be verified against the current
# SISO-REF-010 / IEEE 1278.1 publication before it is relied on for anything
# beyond exercising our own pipeline.
DAMAGE_LEVELS = {"none": 0, "slight": 1, "moderate": 2, "destroyed": 3}

# ADR-0044 slice A: three per-asset damage-map values that are NOT damage
# levels at all -- they are lifecycle states, not appearance claims about a
# still-present entity. Kept as a separate tuple rather than folded into
# DAMAGE_LEVELS (which also validates --damage, a fleet-wide DAMAGE profile
# that has no business offering "removed" as a level every entity could
# share) because "removed" in particular has no bit-field encoding at all --
# it is a second PDU type, not a value of entityAppearance.
#   "deactivated" -- sets the appearance deactivated bit (ontology's
#       dis_appearance.yaml:66 land / :82 air -- `deactivated: { bit: 22 }`)
#       IN ADDITION to whatever damage level already applies. Reversible:
#       clearing the map entry restores the baseline, same contract as a
#       damage level.
#   "silent" -- stops ESPDU transmission for that entity outright; no
#       appearance claim is made or changed. Reversible, same as above.
#   "removed" -- sends exactly one Remove Entity PDU for that entity, then
#       behaves like "silent" forever after. NOT reversible: a Remove Entity
#       PDU is DIS's own terminal signal for "this entity is gone", and
#       undoing it for real would need a Create Entity PDU, which this
#       generator -- "no physics, no behaviours, no scenario model" -- does
#       not emit. Clearing the map entry does not bring the entity back.
LIFECYCLE_OVERRIDES = ("unspecified", "deactivated", "silent", "removed")


# ---------------------------------------------------------------------------
# PER-ASSET DAMAGE CONTROL — the injection lever, on the wire
# ---------------------------------------------------------------------------
# WHY IT LIVES HERE AND NOT IN THE EVALUATOR. A failure lever inside
# prognostics or fusion would have OpenDDIL's own derivation manufacturing a
# sustainment fact: a degradation with no source, no wire message and no
# synthetic stamp — the product proving itself against a fact it invented.
# The sim is the synthetic SOURCE; it publishes onto the same wire the
# product consumes, so the path from message to screen is the real one.
#
# The tactical route is also the more interesting one: damage -> appearance
# bits -> health axis -> operational-state evaluation -> severity transition
# is ADR-0039's thesis (tactical damage crossing into the sustainment plane)
# demonstrated rather than described.
#
# SHAPE: a declarative desired-state file, re-read each tick.
#   {"dis:1:1:1005": "moderate", "1007": "destroyed"}
#
# Keys accept either the canonical `dis:site:app:entity` an operator reads off
# the screen, or a bare entity number. Values are DAMAGE_LEVELS keys, plus
# "unspecified" which means EMIT NO CLAIM for that asset — the deliberate
# silence, distinct from "none" which asserts undamaged.
#
# WHY DECLARATIVE AND RE-READ RATHER THAN AN API:
#   * idempotent by construction — the file IS the desired state, so applying
#     it twice is applying it once, and there is no command to replay;
#   * default-off — absent file changes nothing;
#   * an operator can edit it mid-demo and the next tick carries the change,
#     which is what "drive one asset degraded during the sever" needs.
#
# Malformed entries are logged and SKIPPED rather than fatal: this is a live
# control surface during a demonstration, and a typo must not take the
# generator down mid-run. That is the opposite of the enumeration list's
# fail-loudly rule, and deliberately so — a scenario list is read once at
# start-up where stopping is cheap, this is read every tick where it is not.
#
# --destroy-schedule IS A TIMED DEFAULT, NOT A SECOND LIVE CONTROL. It is a
# CLI value (or DIS_DESTROY_SCHEDULE), parsed ONCE at start-up like the
# --damage profile, not re-read from disk like --damage-map. Entries are
# `ASSET@SECONDS` (seconds since process start; float ok), comma-separated,
# ASSET in the same key forms --damage-map accepts (see _damage_map_key).
# Once an entry's time has elapsed, that asset's damage is "destroyed" and
# it KEEPS transmitting ESPDUs — ADR-0044: destroyed + reporting is a real
# state, same thesis as LIFECYCLE_OVERRIDES above, just reached on a timer
# instead of an operator edit. One-shot: because elapsed time only grows for
# the life of one process, recomputing "has it fired" every tick from
# elapsed-vs-target IS the one-shot behaviour, with no separate state to
# drift — a restart zeroes elapsed time and replays the whole schedule.
# PRECEDENCE: an explicit --damage-map entry for the same asset WINS over the
# schedule every tick, same as --damage-map wins over nothing special here —
# the schedule is only consulted when apply_damage_override() reports no
# override is active this tick. Malformed entries are logged and skipped,
# same contract as the damage map, since this is also an operator-facing
# value that must not take a demo down over a typo.
_DAMAGE_MAP: dict[str, str] = {}
_DAMAGE_MAP_MTIME: float | None = None


def _damage_map_key(site: int, app: int, entity: int) -> tuple[str, str]:
    return (f"dis:{site}:{app}:{entity}", str(entity))


def parse_destroy_schedule(spec: str) -> dict[str, float]:
    """Parse `--destroy-schedule` / `DIS_DESTROY_SCHEDULE` into asset -> seconds.

    Format: comma-separated `ASSET@SECONDS`, ASSET in either key form
    `_damage_map_key` produces (canonical `dis:site:app:entity` or a bare
    entity number), SECONDS a float count of seconds since process start.

    Malformed entries are logged and SKIPPED, not fatal — same contract as
    `reload_damage_map`: an operator-facing value, and a typo must not take
    the generator down.
    """
    schedule: dict[str, float] = {}
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok:
            continue
        if "@" not in tok:
            LOG.warning("destroy schedule: %r missing '@ASSET@SECONDS'; skipping", tok)
            continue
        asset, _, secs = tok.partition("@")
        asset = asset.strip()
        secs = secs.strip()
        if not asset:
            LOG.warning("destroy schedule: %r has an empty asset; skipping", tok)
            continue
        try:
            seconds = float(secs)
        except ValueError:
            LOG.warning("destroy schedule: %r has a non-numeric time %r; skipping", tok, secs)
            continue
        if seconds < 0:
            LOG.warning("destroy schedule: %r has a negative time; skipping", tok)
            continue
        schedule[asset] = seconds
    return schedule


def resolve_destroy_schedule(entities: list[Entity], schedule: dict[str, float]) -> None:
    """Match each entity against a parsed --destroy-schedule, if any.

    Logged once per matched entity here, at start-up, so an operator sees
    the whole schedule before anything fires rather than discovering it
    piecemeal as entries fire. An entity with no matching key is untouched
    (its destroy_at_s stays None and the schedule never applies to it).
    """
    for e in entities:
        for key in _damage_map_key(e.site_id, e.app_id, e.entity_id):
            if key in schedule:
                e.destroy_at_s = schedule[key]
                LOG.info("destroy schedule: dis:%d:%d:%d at t+%gs",
                         e.site_id, e.app_id, e.entity_id, e.destroy_at_s)
                break


# ---------------------------------------------------------------------------
# --fire-schedule / --detonate-schedule (effector events, Fire/Detonation
# PDUs). Modelled on --destroy-schedule's shape -- a CLI value (or env var),
# parsed ONCE at start-up, applied per tick off the same injected elapsed_s
# clock -- with one deliberate difference: a malformed entry here is FATAL
# (SystemExit), not logged-and-skipped. --destroy-schedule tolerates a typo
# because it is a live operator control during a demonstration; an effector
# schedule is a test fixture, read once, where a silently-dropped entry
# would make a predicted count wrong without any visible sign why.
#
# L (the launcher) and TGT (the target) accept the same two forms
# --destroy-schedule's ASSET does: the canonical `dis:site:app:entity`, or a
# bare entity number resolved against THIS sim's own --site-id/--app-id.
# Unlike --destroy-schedule's ASSET, L does not have to match one of this
# sim's own Entity objects -- a Fire/Detonation PDU's firingEntityID is
# just a field, not a claim that the sender also emits that entity's
# ESPDUs, and a fixture that fires from an id the fleet does not have is a
# deliberate case: it exercises the downstream unknown-launcher refusal.
def _parse_effector_entity_key(tok: str, site_id: int, app_id: int) -> tuple[int, int, int]:
    """`dis:site:app:entity` or a bare entity number -> (site, app, entity).

    Raises ValueError (not SystemExit) so both schedule parsers can catch it
    and name the FULL bad entry, not just this sub-field.
    """
    tok = tok.strip()
    if tok.startswith("dis:"):
        parts = tok.split(":")
        if len(parts) != 4:
            raise ValueError(f"{tok!r} is not dis:site:app:entity")
        try:
            return int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            raise ValueError(f"{tok!r} has a non-integer dis:site:app:entity field")
    if not tok.lstrip("-").isdigit():
        raise ValueError(f"{tok!r} is not an entity number or dis:site:app:entity")
    return site_id, app_id, int(tok)


def parse_fire_schedule(spec: str, site_id: int, app_id: int) -> list[dict]:
    """Parse `--fire-schedule` / `DIS_FIRE_SCHEDULE`.

    Format: comma-separated `L@T:E:Q[:TGT]` -- launcher L, elapsed seconds
    T, eventNumber E, quantity Q, optional target TGT (default 0:0:0, DIS's
    "no target" convention -- see _entity_urn_or_none's sensor-ingest
    counterpart).

    A bad entry exits non-zero NAMING that entry, unlike --destroy-
    schedule's skip-and-warn (see the module comment above this function).
    """
    entries: list[dict] = []
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok:
            continue
        try:
            launcher_s, sep, rest = tok.partition("@")
            if not sep:
                raise ValueError("missing '@T:E:Q[:TGT]'")
            # maxsplit=3: T, E and Q never contain ':', but TGT can (the
            # canonical dis:site:app:entity form) -- splitting on every ':'
            # would shred it. The same reasoning applies to L below.
            fields = rest.split(":", 3)
            if len(fields) not in (3, 4):
                raise ValueError("expected T:E:Q or T:E:Q:TGT")
            t_s, event_s, qty_s = fields[0], fields[1], fields[2]
            tgt_s = fields[3] if len(fields) == 4 else None
            launcher = _parse_effector_entity_key(launcher_s, site_id, app_id)
            t = float(t_s)
            if t < 0:
                raise ValueError("T must be >= 0")
            event = int(event_s)
            qty = int(qty_s)
            target = (_parse_effector_entity_key(tgt_s, site_id, app_id)
                      if tgt_s else (0, 0, 0))
        except ValueError as exc:
            raise SystemExit(f"--fire-schedule: bad entry {tok!r}: {exc}")
        entries.append({
            "launcher": launcher, "t": t, "event": event,
            "quantity": qty, "target": target,
        })
    return entries


def parse_detonate_schedule(spec: str, site_id: int, app_id: int,
                            fire_by_event: dict[int, dict]) -> list[dict]:
    """Parse `--detonate-schedule` / `DIS_DETONATE_SCHEDULE`.

    Format: comma-separated `E@T:R[:L]` -- fire event E, elapsed seconds T,
    detonationResult R, optional launcher override L.

    Without L, E must be a launcher this call already knows about (an entry
    in `fire_by_event`, built from the already-parsed --fire-schedule) --
    the firing entity for a Detonation is the launcher of its Fire. An
    orphan detonation (E not in --fire-schedule) with no L is refused HERE,
    at parse time, not left to fail later: that is what makes "an orphan
    without L is refused at parse" a parser-level guarantee rather than a
    runtime one.
    """
    entries: list[dict] = []
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok:
            continue
        try:
            event_s, sep, rest = tok.partition("@")
            if not sep:
                raise ValueError("missing '@T:R[:L]'")
            fields = rest.split(":", 2)  # L can contain ':' (dis:s:a:e) -- see fire-schedule's comment
            if len(fields) not in (2, 3):
                raise ValueError("expected T:R or T:R:L")
            t_s, result_s = fields[0], fields[1]
            launcher_s = fields[2] if len(fields) == 3 else None
            event = int(event_s)
            t = float(t_s)
            if t < 0:
                raise ValueError("T must be >= 0")
            result = int(result_s)
            if launcher_s:
                launcher = _parse_effector_entity_key(launcher_s, site_id, app_id)
            elif event in fire_by_event:
                launcher = fire_by_event[event]["launcher"]
            else:
                raise ValueError(
                    f"event {event} is not in --fire-schedule and no "
                    "launcher override (third field, L) was given -- an "
                    "orphan detonation needs an explicit launcher"
                )
        except ValueError as exc:
            raise SystemExit(f"--detonate-schedule: bad entry {tok!r}: {exc}")
        entries.append({
            "event": event, "t": t, "result": result, "launcher": launcher,
        })
    return entries


def parse_munition_type(spec: str) -> tuple[int, int, int, int, int, int, int]:
    """`--munition-type "k.d.c.cat.sub.spec.extra"` -> the DIS 7-tuple.

    Default 2.9.225.2.1.1.0 is a PLACEHOLDER TUPLE for these tests' own
    fixture use, not a claim about any real munition -- same discipline as
    RECOGNISED_TYPES' header comment for platform entity types.
    """
    parts = spec.split(".")
    if len(parts) != 7:
        raise SystemExit(
            f"--munition-type: {spec!r} must be 7 dot-separated integers "
            "(kind.domain.country.category.subcategory.specific.extra)"
        )
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        raise SystemExit(f"--munition-type: {spec!r} has a non-integer field")


def fire_pdu(entry: dict, munition_type: tuple[int, int, int, int, int, int, int],
            sim_site_id: int, sim_app_id: int, exercise_id: int,
            protocol_version: int) -> FirePdu:
    """One Fire PDU (type 2, Warfare family) for one fired --fire-schedule
    entry. eventID's simulationAddress is THIS SIM's own site/app (not the
    launcher's) -- the event belongs to the exercise this sim is running,
    same convention event_report_pdu() uses for originatingEntityID's
    site/app. PURE TRANSPORT: munitionType/warhead/fuse are opaque DIS
    codes, same discipline as the rest of this module's schedule PDUs."""
    pdu = FirePdu(
        munitionExpendableID=EntityID(0, 0, 0),
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=sim_site_id, application=sim_app_id),
            eventNumber=entry["event"],
        ),
        location=Vector3Double(x=0.0, y=0.0, z=0.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(
                entityKind=munition_type[0], domain=munition_type[1],
                country=munition_type[2], category=munition_type[3],
                subcategory=munition_type[4], specific=munition_type[5],
                extra=munition_type[6],
            ),
            warhead=0, fuse=0, quantity=entry["quantity"],
        ),
        range_=0.0,
    )
    pdu.firingEntityID.siteID, pdu.firingEntityID.applicationID, pdu.firingEntityID.entityID = entry["launcher"]
    pdu.targetEntityID.siteID, pdu.targetEntityID.applicationID, pdu.targetEntityID.entityID = entry["target"]
    pdu.protocolVersion = protocol_version
    pdu.exerciseID = exercise_id
    pdu.pduType = 2        # Fire
    pdu.protocolFamily = 2  # Warfare
    pdu.pduStatus = 0
    return pdu


def detonation_pdu(entry: dict, munition_type: tuple[int, int, int, int, int, int, int],
                   sim_site_id: int, sim_app_id: int, exercise_id: int,
                   protocol_version: int) -> DetonationPdu:
    """One Detonation PDU (type 3, Warfare family) for one fired
    --detonate-schedule entry. eventID carries the SAME eventNumber as its
    Fire (that is how event_urn correlates the two downstream) -- entry["event"]
    is this call's only link back to the Fire; entry["launcher"] was
    already resolved (override or inherited) by parse_detonate_schedule."""
    pdu = DetonationPdu(
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=sim_site_id, application=sim_app_id),
            eventNumber=entry["event"],
        ),
        location=Vector3Double(x=0.0, y=0.0, z=0.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(
                entityKind=munition_type[0], domain=munition_type[1],
                country=munition_type[2], category=munition_type[3],
                subcategory=munition_type[4], specific=munition_type[5],
                extra=munition_type[6],
            ),
            warhead=0, fuse=0, quantity=0,
        ),
        detonationResult=entry["result"],
    )
    pdu.firingEntityID.siteID, pdu.firingEntityID.applicationID, pdu.firingEntityID.entityID = entry["launcher"]
    pdu.protocolVersion = protocol_version
    pdu.exerciseID = exercise_id
    pdu.pduType = 3        # Detonation
    pdu.protocolFamily = 2  # Warfare
    pdu.pduStatus = 0
    return pdu


# --resupply-schedule (Resupply Received, PDU type 7, Logistics family 3):
# `L@T[/R]:Q[:S]`, same fatal-on-bad-entry discipline as --fire-schedule.
# The receiver de-duplicates resupply events on receiver + header timestamp,
# so unlike every other PDU this module builds, a resupply MUST carry a real
# DIS timestamp -- dis_timestamp() below. Entries are one-shot unless they
# carry /R, which becomes a "repeat_s" key for due_schedule_entries().
def dis_timestamp(now: float | None = None) -> int:
    """DIS absolute timestamp (bit 0 set) for wall-clock `now` (default
    time.time()): the fraction of the current hour in the upper 31 bits."""
    if now is None:
        now = time.time()
    return (((int((now % 3600) / 3600 * 2**31)) << 1) | 1) & 0xFFFFFFFF


def parse_resupply_schedule(spec: str, site_id: int, app_id: int) -> list[dict]:
    """Parse `--resupply-schedule` / `DIS_RESUPPLY_SCHEDULE`.

    Format: comma-separated `L@T:Q[:S]` -- receiving launcher L, elapsed
    seconds T, quantity Q (> 0), optional supplier S (default 0:0:0).
    `L@T/R:Q[:S]` re-sends every R seconds (R > 0) after T. L/S accept
    dis:site:app:entity or a bare entity number.

    A bad entry exits non-zero NAMING that entry (see parse_fire_schedule).
    """
    entries: list[dict] = []
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok:
            continue
        try:
            launcher_s, sep, rest = tok.partition("@")
            if not sep:
                raise ValueError("missing '@T:Q[:S]'")
            # maxsplit=2: S can contain ':' (dis:site:app:entity).
            fields = rest.split(":", 2)
            if len(fields) not in (2, 3):
                raise ValueError("expected T:Q or T:Q:S")
            t_s, has_repeat, r_s = fields[0].partition("/")
            sup_s = fields[2] if len(fields) == 3 else None
            launcher = _parse_effector_entity_key(launcher_s, site_id, app_id)
            t = float(t_s)
            if t < 0:
                raise ValueError("T must be >= 0")
            repeat_s = None
            if has_repeat:
                repeat_s = float(r_s)
                if not repeat_s > 0:
                    raise ValueError("R must be > 0")
            qty = float(fields[1])
            if not (qty > 0 and math.isfinite(qty)):
                raise ValueError("Q must be > 0")
            supplier = (_parse_effector_entity_key(sup_s, site_id, app_id)
                        if sup_s else (0, 0, 0))
        except ValueError as exc:
            raise SystemExit(f"--resupply-schedule: bad entry {tok!r}: {exc}")
        entry = {"launcher": launcher, "t": t, "quantity": qty, "supplier": supplier}
        if repeat_s is not None:
            entry["repeat_s"] = repeat_s
        entries.append(entry)
    return entries


def resupply_pdu(entry: dict, munition_type: tuple[int, int, int, int, int, int, int],
                 exercise_id: int, protocol_version: int,
                 now: float | None = None) -> ResupplyReceivedPdu:
    """One Resupply Received PDU (type 7, Logistics family) for one due
    --resupply-schedule entry: one supply, the --munition-type tuple the
    Fire/Detonation PDUs use, at entry["quantity"]. Header timestamp is a
    real DIS timestamp (see dis_timestamp) so repeats stay distinct events."""
    pdu = ResupplyReceivedPdu(
        supplies=[SupplyQuantity(
            supplyType=EntityType(
                entityKind=munition_type[0], domain=munition_type[1],
                country=munition_type[2], category=munition_type[3],
                subcategory=munition_type[4], specific=munition_type[5],
                extra=munition_type[6],
            ),
            quantity=entry["quantity"],
        )],
    )
    pdu.receivingEntityID.siteID, pdu.receivingEntityID.applicationID, pdu.receivingEntityID.entityID = entry["launcher"]
    pdu.supplyingEntityID.siteID, pdu.supplyingEntityID.applicationID, pdu.supplyingEntityID.entityID = entry["supplier"]
    pdu.protocolVersion = protocol_version
    pdu.exerciseID = exercise_id
    pdu.pduType = 7         # Resupply Received
    pdu.protocolFamily = 3  # Logistics
    pdu.pduStatus = 0
    pdu.timestamp = dis_timestamp(now)
    return _with_length(pdu)


def reload_damage_map(path: str) -> dict[str, str]:
    """Re-read the per-asset damage file when it changes. Returns the map."""
    global _DAMAGE_MAP, _DAMAGE_MAP_MTIME
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _DAMAGE_MAP:
            LOG.info("damage map %s disappeared; reverting to no overrides", path)
        _DAMAGE_MAP, _DAMAGE_MAP_MTIME = {}, None
        return {}
    if mtime == _DAMAGE_MAP_MTIME:
        return _DAMAGE_MAP
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("expected a JSON object of asset -> damage level")
        out: dict[str, str] = {}
        for k, v in raw.items():
            level = str(v).lower()
            if level not in DAMAGE_LEVELS and level not in LIFECYCLE_OVERRIDES:
                LOG.warning("damage map: %r has unknown level %r; skipping "
                            "(valid: %s, %s)", k, v,
                            "|".join(DAMAGE_LEVELS), "|".join(LIFECYCLE_OVERRIDES))
                continue
            out[str(k).strip()] = level
        _DAMAGE_MAP, _DAMAGE_MAP_MTIME = out, mtime
        LOG.info("damage map reloaded from %s: %d override(s) %s",
                 path, len(out), out)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("damage map %s unreadable (%s); keeping previous %d "
                    "override(s)", path, exc, len(_DAMAGE_MAP))
    return _DAMAGE_MAP


# ---------------------------------------------------------------------------
# --posture-schedule (launcher posture: raise/stow the launcher-raised
# appearance bit, move/stop a scheduled entity's own motion). Same shape as
# --fire-schedule/--detonate-schedule above -- a test fixture read once at
# start-up, so a malformed entry is FATAL (SystemExit), not logged-and-
# skipped like --destroy-schedule's live operator control.
_POSTURE_ACTIONS = ("raise", "stow", "move", "stop")
# Constant speed a "move" action gives a scheduled entity ("5 m/s along x" --
# see Entity.step()'s posture-schedule branch, which holds heading at 0.0 so
# this is the entity's entire velocity).
_POSTURE_MOVE_SPEED_MPS = 5.0


def parse_posture_schedule(spec: str, site_id: int, app_id: int) -> list[dict]:
    """Parse `--posture-schedule` / `DIS_POSTURE_SCHEDULE`.

    Format: comma-separated `ENTITY:T:ACTION` -- ENTITY accepts the same two
    forms as --fire-schedule's L (dis:site:app:entity or a bare entity
    number resolved against this sim's own --site-id/--app-id), T is
    elapsed seconds since process start, ACTION is one of raise|stow|move|
    stop. Entries are returned sorted by T so resolve_posture_schedule can
    hand each entity its own entries in firing order.

    A bad entry exits non-zero naming it, same discipline as --fire-
    schedule/--detonate-schedule (this is a fixture read once, not an
    operator lever where a typo must not take the generator down).
    """
    entries: list[dict] = []
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok:
            continue
        try:
            fields = tok.split(":")
            # ENTITY can itself be dis:site:app:entity (4 colon-separated
            # fields) plus :T:ACTION, or a bare number plus :T:ACTION.
            if len(fields) == 6 and fields[0] == "dis":
                entity_s = ":".join(fields[:4])
                t_s, action = fields[4], fields[5]
            elif len(fields) == 3:
                entity_s, t_s, action = fields
            else:
                raise ValueError("expected ENTITY:T:ACTION")
            entity = _parse_effector_entity_key(entity_s, site_id, app_id)
            t = float(t_s)
            if t < 0:
                raise ValueError("T must be >= 0")
            action = action.strip()
            if action not in _POSTURE_ACTIONS:
                raise ValueError(f"action must be one of {_POSTURE_ACTIONS}")
        except ValueError as exc:
            raise SystemExit(f"--posture-schedule: bad entry {tok!r}: {exc}")
        entries.append({"entity": entity, "t": t, "action": action})
    entries.sort(key=lambda e: e["t"])
    return entries


def resolve_posture_schedule(entities: list[Entity], schedule: list[dict]) -> None:
    """Match each --posture-schedule entry to its entity, same lookup as
    resolve_destroy_schedule. Logged once per matched entry at start-up, so
    an operator sees the whole schedule before anything fires.

    An entity with no matching entries is left completely untouched
    (has_posture_schedule stays False) -- Entity.step() and to_pdu() then
    take their ordinary, unscheduled path, unchanged from today. A matched
    entity's speed/heading are zeroed here (not left at the random startup
    values) so its very first PDU, before any action has fired, already
    shows a stationary launcher rather than one still drifting from
    Entity.__init__'s random walk.
    """
    by_key: dict[str, Entity] = {}
    for e in entities:
        for key in _damage_map_key(e.site_id, e.app_id, e.entity_id):
            by_key[key] = e
    for entry in schedule:
        site, app, entity_id = entry["entity"]
        match: Entity | None = None
        for key in _damage_map_key(site, app, entity_id):
            if key in by_key:
                match = by_key[key]
                break
        if match is None:
            LOG.warning("posture schedule: entity %d:%d:%d matches nothing in "
                        "this sim; entry at t+%gs ignored",
                        site, app, entity_id, entry["t"])
            continue
        match.posture_schedule.append(entry)
    for e in entities:
        if not e.posture_schedule:
            continue
        e.posture_schedule.sort(key=lambda x: x["t"])
        e._posture_next_due = [entry["t"] for entry in e.posture_schedule]
        e.has_posture_schedule = True
        e.speed_mps = 0.0
        e.heading = 0.0
        for entry in e.posture_schedule:
            LOG.info("posture schedule: entity %d:%d:%d action=%s at t+%gs",
                     e.site_id, e.app_id, e.entity_id, entry["action"], entry["t"])


# Bit 15, LAND platforms only -- "launcher raised" (see
# openddil-contracts/ontology/dis_appearance.yaml, row "1_1" -> launcher).
# This decoder's mapping, not verified against the published standard (same
# caveat the ontology row itself carries); checking it is owed.
_LAUNCHER_RAISED_BIT = 15


def appearance_bits(domain: int,
                    damage: str = "none",
                    mobility_kill: bool = False,
                    firepower_kill: bool = False,
                    powerplant_on: bool = True,
                    deactivated: bool = False,
                    launcher_raised: bool = False) -> int:
    """Compose a 32-bit DIS entity-appearance value for a PLATFORM (kind 1)."""
    bits = 0
    bits |= (DAMAGE_LEVELS[damage] & 0x3) << 3       # bits 3-4, all platform domains
    if mobility_kill:
        bits |= 1 << 1                                # mobility (land) / propulsion (air)
    if firepower_kill:
        if domain != 1:
            raise ValueError(
                "firepower_kill is a LAND-domain bit; setting it for domain "
                f"{domain} would encode an unrelated meaning"
            )
        bits |= 1 << 2
    if powerplant_on:
        bits |= 1 << 21
    if deactivated:
        bits |= 1 << 22
    if launcher_raised:
        if domain != 1:
            raise ValueError(
                "launcher_raised is a LAND-domain bit; setting it for domain "
                f"{domain} would encode an unrelated meaning"
            )
        bits |= 1 << _LAUNCHER_RAISED_BIT
    return bits


class Entity:
    """One emitting entity. Drifts slowly so positions are not static."""

    def __init__(self, index: int, site_id: int, app_id: int, rng: random.Random,
                 entity_id: int | None = None):
        self.site_id = site_id
        self.app_id = app_id
        # `1000 + index` is the default and stays the default: the contiguous
        # fleet is what almost every run wants. An explicit id is for the runs
        # that need a SPECIFIC asset id -- a fixture that has to be recognisable
        # by id downstream, where "the ninth entity" would not identify it.
        self.entity_id = (1000 + index) if entity_id is None else entity_id
        self.entity_type, self.variant = platform_for(site_id, app_id, self.entity_id)

        # The marking follows the ENTITY ID, not the position in the list. For
        # the contiguous fleet the two are the same number -- entity_id is
        # 1000 + index there -- so this changes no existing run. It matters only
        # for an explicit id, where deriving from the position would hand the
        # first explicit entity the same marking as the first entity of every
        # other sim, and a fixture that shares a callsign with a fleet asset is
        # not recognisable on an overlay, which is the whole reason it was given
        # a specific id.
        slot = self.entity_id - 1000
        stem = CALLSIGN_STEMS[slot % len(CALLSIGN_STEMS)]
        # DIS marking is 11 bytes + a charset byte; keep it short and ASCII.
        self.marking = f"{stem[:7]}-{slot % 100:02d}"[:11]

        self.lat = BASE_LAT_DEG + rng.uniform(-SPREAD_DEG, SPREAD_DEG)
        self.lon = BASE_LON_DEG + rng.uniform(-SPREAD_DEG, SPREAD_DEG)
        self.alt = 1600.0 + rng.uniform(0, 200)
        self.heading = rng.uniform(0, 2 * math.pi)
        # Air domain (2) moves faster than land (1).
        self.speed_mps = rng.uniform(40, 120) if self.entity_type[1] == 2 else rng.uniform(2, 12)
        self.force_id = 1  # friendly

        # Appearance is UNSET by default -- the historical behaviour, and the
        # correct one: a generator that always claims "undamaged" is making an
        # assertion it has no basis for, which is the defect this control was
        # added to make visible rather than to hide.
        self.damage = "none"
        self.mobility_kill = False
        self.firepower_kill = False
        self.emit_appearance = False
        # Baseline defaults; overwritten after the --damage profile runs.
        self._baseline_damage = "none"
        self._baseline_emit = False

        # ADR-0044 slice A lifecycle overrides (see LIFECYCLE_OVERRIDES).
        # None of these are touched by the --damage profile or its baseline
        # freeze below -- they are driven ONLY by --damage-map, and default
        # off, same as emit_appearance.
        self.deactivated = False  # appearance bit only; reversible
        self.silent = False       # stop sending ESPDUs; reversible
        self.removed = False      # stop sending ESPDUs; NOT reversible
        self._removed_pdu_sent = False  # edge-detect: send Remove Entity once

        # --destroy-schedule (see resolve_destroy_schedule / apply_destroy_schedule):
        # the elapsed-seconds-since-start at which this entity becomes a
        # one-shot "destroyed" claim, or None when nothing in the schedule
        # matches this entity's id.
        self.destroy_at_s: float | None = None
        self._destroy_logged = False  # edge-detect: log the fire once, not every tick

        # --posture-schedule (see resolve_posture_schedule / apply_posture_
        # schedule): this entity's own entries (sorted by t), matched
        # next_due list for due_schedule_entries, and the launcher-raised
        # claim raise/stow actions set. Empty/False until
        # resolve_posture_schedule matches at least one entry to this
        # entity -- an unmatched entity's motion and appearance are
        # untouched, same contract as destroy_at_s is None.
        self.posture_schedule: list[dict] = []
        self._posture_next_due: list[float | None] = []
        self.has_posture_schedule = False
        self.launcher_raised = False

        # Power plant lever. True is the historical encoding (appearance_bits'
        # own default), so every existing run is byte-identical.
        #
        # With emit_appearance on, power plant off, undamaged and not
        # deactivated encodes to an ALL-ZERO field. The receiver reads an
        # all-zero field as "power plant off" only because the ontology opts
        # this source into zero_after_claim AND this generator has already
        # sent a non-zero field for the entity (every claim it makes sets the
        # power-plant bit). So a condition schedule must open with a
        # powered-on step before any power-off step; load_condition_schedule
        # refuses one that does not.
        self.powerplant_on = True

        # --condition-schedule (see load_condition_schedule / apply_condition_
        # schedule): this entity's own steps (sorted by at_s), the cycle
        # length, the "nominal" emission profile, and the step currently in
        # effect. Empty / None until the loader gives this entity steps -- an
        # entity without steps makes no emission or Data claim and its
        # appearance is untouched, same contract as destroy_at_s is None.
        self.condition_steps: list[dict] = []
        self.condition_cycle_s: float | None = None
        self.condition_profile: dict | None = None
        self.condition: dict | None = None
        self._condition_logged_idx: int | None = None  # edge-detect: log a step change once

    def apply_condition_schedule(self, elapsed_s: float, appearance_free: bool = True) -> dict | None:
        """Resolve this entity's --condition-schedule step for `elapsed_s`.

        Returns the step in effect (also kept on self.condition, which main()
        reads for the EE and Data PDUs), or None before the first step or when
        the entity has no steps -- in which case NOTHING is touched, so the
        entity keeps today's behaviour (no claims, no EE, no Data).

        `appearance_free` is False when a live --damage-map override or a
        fired --destroy-schedule owns the appearance fields this tick: those
        win, so the step then drives only the EE and Data PDUs. Precedence for
        the appearance fields is damage-map, then destroy-schedule, then this.
        launcher_raised and motion are never touched here; the posture
        schedule keeps owning them.
        """
        if not self.condition_steps:
            self.condition = None
            return None
        step = resolve_condition_step(self.condition_steps, self.condition_cycle_s, elapsed_s)
        self.condition = step
        if step is None:
            return None
        if appearance_free:
            self.emit_appearance = True
            self.damage = step["damage"]
            self.powerplant_on = step["power_plant"] == "on"
            self.deactivated = step["deactivated"]
        else:
            # Another source owns the appearance this tick; do not leave a
            # power-off from an earlier step encoded into its claim.
            self.powerplant_on = True
        if step["idx"] != self._condition_logged_idx:
            LOG.info("condition step: entity %d at_s=%g set=%s",
                     self.entity_id, step["at_s"], json.dumps(step["set"], sort_keys=True))
            self._condition_logged_idx = step["idx"]
        return step

    def apply_damage_override(self, damage_map: dict[str, str]) -> bool:
        """Apply a per-asset override from the declarative map, if present.

        Returns True when this entity is under an override, so the caller can
        report which assets are being driven rather than leaving it implicit.

        Precedence is deliberate: a per-asset entry WINS over the fleet-wide
        --damage, because the point of the lever is to make ONE asset differ
        from its fleet. `unspecified` turns emission off for that asset — a
        deliberate silence, and the reason the map has a value that is not a
        damage level at all.

        ADR-0044 slice A adds three more non-damage-level values (see
        LIFECYCLE_OVERRIDES): "deactivated" (appearance bit, reversible),
        "silent" (stop transmitting, reversible), and "removed" (stop
        transmitting, NOT reversible — see LIFECYCLE_OVERRIDES for why). This
        method only sets state; main()'s loop is what actually sends or
        withholds a PDU based on self.silent / self.removed, and sends the
        one-shot Remove Entity PDU on the False -> True edge of self.removed.
        """
        for key in _damage_map_key(self.site_id, self.app_id, self.entity_id):
            level = (damage_map or {}).get(key)
            if level is None:
                continue
            if level == "unspecified":
                # Say nothing about this asset. NOT the same as "none":
                # none asserts undamaged, this asserts nothing at all.
                self.emit_appearance = False
                self.deactivated = False
                self.silent = False
            elif level == "deactivated":
                # An ADDITIONAL claim layered on whatever damage already
                # applies -- not a replacement for it.
                self.deactivated = True
                self.emit_appearance = True
                self.silent = False
            elif level == "silent":
                # Not an appearance claim at all. Appearance state is left
                # exactly as it is so it resumes unchanged when "silent" is
                # cleared.
                self.silent = True
            elif level == "removed":
                # See LIFECYCLE_OVERRIDES: terminal, no baseline restore.
                self.removed = True
            else:
                self.damage = level
                self.emit_appearance = True
                self.deactivated = False
                self.silent = False
            return True

        # NO ENTRY FOR THIS ENTITY -> RESTORE THE BASELINE.
        #
        # This was missing, and it made the control only half declarative:
        # emptying the file left every previously-injected entity damaged
        # forever, because nothing reset what an earlier tick had set. Found
        # by running the downward leg — the injection cleared in the file and
        # the asset stayed CRITICAL.
        #
        # A desired-state file has to be able to say "no override" by
        # OMISSION, or it is an append-only command log wearing a state
        # file's clothes. The baseline is whatever --damage established at
        # start-up, so clearing returns to the fleet-wide profile rather than
        # to an invented "undamaged".
        self.damage = self._baseline_damage
        self.emit_appearance = self._baseline_emit
        # "deactivated" and "silent" restore to baseline-off on omission too,
        # the same declarative contract as damage. "removed" is deliberately
        # NOT reset here -- see LIFECYCLE_OVERRIDES.
        self.deactivated = False
        self.silent = False
        return False

    def apply_destroy_schedule(self, elapsed_s: float) -> bool:
        """One-shot scheduled destroy claim (see --destroy-schedule).

        Call this ONLY when `apply_damage_override` reported no live
        override for this tick — an explicit --damage-map entry for this
        asset always wins over the schedule (the operator lever is live
        control; the schedule is a timed default).

        Encodes the claim exactly the way a literal "destroyed" --damage-map
        entry does (see the `else` branch of apply_damage_override): damage
        level + emit_appearance, which is also what sets the power-plant bit
        at PDU-encode time — no new encoding is invented here.

        No persisted "fired" flag drives the claim itself: elapsed_s only
        grows for the life of one process, so recomputing "elapsed_s >=
        destroy_at_s" every tick already IS one-shot-and-never-reverts, and a
        process restart (elapsed_s back to 0) is the only way to replay it —
        exactly the contract the spec calls for. Returns True while the
        schedule is in effect, so the caller can log the transition once.
        """
        if self.destroy_at_s is None or elapsed_s < self.destroy_at_s:
            return False
        self.damage = "destroyed"
        self.emit_appearance = True
        self.deactivated = False
        self.silent = False
        return True

    def apply_posture_schedule(self, elapsed_s: float) -> bool:
        """Advance this entity's --posture-schedule, if it has one.

        Call this the same way apply_destroy_schedule is called -- only
        when apply_damage_override (and, for a shared entity, apply_
        destroy_schedule) reported no live override for this tick. Returns
        True on every tick once this entity has a schedule, whether or not
        an entry fires THIS tick (same "stays in effect" contract as
        apply_destroy_schedule), which is what lets main()'s loop skip the
        ordinary Entity.step() random motion for a scheduled entity: "A
        scheduled entity's motion comes ONLY from its schedule."

        Re-asserts emit_appearance every tick for the same reason apply_
        destroy_schedule does: apply_damage_override's "no entry" branch
        resets it to baseline (False) first, on every tick, and this is
        what overrides that back to True -- a scheduled launcher always
        makes a claim, even before its first raise/stow action has fired
        (see resolve_posture_schedule's zeroed starting speed/heading).
        """
        if not self.has_posture_schedule:
            return False
        self.emit_appearance = True
        self.deactivated = False
        self.silent = False
        for idx in due_schedule_entries(self.posture_schedule, self._posture_next_due, elapsed_s):
            entry = self.posture_schedule[idx]
            action = entry["action"]
            if action == "raise":
                self.launcher_raised = True
            elif action == "stow":
                self.launcher_raised = False
            elif action == "move":
                self.speed_mps = _POSTURE_MOVE_SPEED_MPS
            elif action == "stop":
                self.speed_mps = 0.0
            LOG.info("posture schedule fired: entity %d:%d:%d action=%s at t+%gs",
                     self.site_id, self.app_id, self.entity_id, action, elapsed_s)
        return True

    def step(self, dt_s: float) -> None:
        # Crude flat-earth step. Adequate: nothing downstream does geodesy on
        # these, and dead-reckoning is not being exercised.
        dm = self.speed_mps * dt_s
        self.lat += (dm * math.cos(self.heading)) / 111_320.0
        self.lon += (dm * math.sin(self.heading)) / (111_320.0 * math.cos(math.radians(self.lat)))
        # A posture-scheduled entity's heading is fixed by resolve_posture_
        # schedule (0.0) and never jittered -- "A scheduled entity's motion
        # comes ONLY from its schedule" (--posture-schedule's help).
        if not self.has_posture_schedule:
            self.heading += random.uniform(-0.05, 0.05)

    def to_pdu(self, exercise_id: int, protocol_version: int) -> EntityStatePdu:
        pdu = EntityStatePdu()
        pdu.protocolVersion = protocol_version
        pdu.exerciseID = exercise_id
        pdu.pduType = 1        # Entity State
        pdu.protocolFamily = 1  # Entity Information / Interaction
        pdu.pduStatus = 0
        # Zero unless the operator asked for a claim. Zero is NOT "undamaged"
        # here -- it is "this generator said nothing", and the two are
        # indistinguishable in the bits, which is exactly why a consumer must
        # not read NONE out of an unpopulated field.
        pdu.entityAppearance = (
            appearance_bits(
                domain=self.entity_type[1],
                damage=self.damage,
                mobility_kill=self.mobility_kill,
                firepower_kill=self.firepower_kill,
                powerplant_on=self.powerplant_on,
                deactivated=self.deactivated,
                launcher_raised=self.launcher_raised,
            )
            if self.emit_appearance else 0
        )
        pdu.capabilities = 0

        pdu.entityID.siteID = self.site_id
        pdu.entityID.applicationID = self.app_id
        pdu.entityID.entityID = self.entity_id

        (k, d, c, cat, sub, spec, extra) = self.entity_type
        pdu.entityType.entityKind = k
        pdu.entityType.domain = d
        pdu.entityType.country = c
        pdu.entityType.category = cat
        pdu.entityType.subcategory = sub
        pdu.entityType.specific = spec
        pdu.entityType.extra = extra

        pdu.forceId = self.force_id
        pdu.marking.characters = list(self.marking.encode("ascii").ljust(11, b"\x00"))

        x, y, z = geodetic_to_ecef(self.lat, self.lon, self.alt)
        pdu.entityLocation.x = x
        pdu.entityLocation.y = y
        pdu.entityLocation.z = z

        pdu.entityLinearVelocity.x = self.speed_mps * math.cos(self.heading)
        pdu.entityLinearVelocity.y = self.speed_mps * math.sin(self.heading)
        pdu.entityLinearVelocity.z = 0.0

        pdu.entityOrientation.psi = self.heading
        pdu.entityOrientation.theta = 0.0
        pdu.entityOrientation.phi = 0.0
        return pdu

    def to_remove_entity_pdu(self, exercise_id: int, protocol_version: int) -> RemoveEntityPdu:
        """The one Remove Entity PDU sent when this entity's damage-map entry
        becomes "removed" (ADR-0044 slice A, LIFECYCLE_OVERRIDES).

        originatingEntityID is this sim's own site/application with entity 0
        -- the SIM is the originator of the removal, not the entity being
        removed, and entity 0 is DIS's convention for "no specific entity" on
        that side. receivingEntityID is the entity being removed.

        Layout/semantics taken from Open-DIS's RemoveEntityPdu, same caveat as
        dis_ingestor.py's _extract_remove_entity() and appearance_bits()
        above: not independently verified against the published IEEE 1278.1
        text.
        """
        pdu = RemoveEntityPdu()
        pdu.protocolVersion = protocol_version
        pdu.exerciseID = exercise_id
        pdu.pduType = 12        # Remove Entity
        pdu.protocolFamily = 5  # Simulation Management
        pdu.pduStatus = 0
        pdu.originatingEntityID.siteID = self.site_id
        pdu.originatingEntityID.applicationID = self.app_id
        pdu.originatingEntityID.entityID = 0
        pdu.receivingEntityID.siteID = self.site_id
        pdu.receivingEntityID.applicationID = self.app_id
        pdu.receivingEntityID.entityID = self.entity_id
        pdu.requestID = 0
        return pdu


# ---------------------------------------------------------------------------
# Scheduled Event Report PDUs — DIS_EVENT_SCHEDULE_PATH
# ---------------------------------------------------------------------------
# PURE TRANSPORT. This sim sends what its schedule says; it does not know,
# and this file does not encode, what any event_type or datum id MEANS.
# That interpretation is configuration elsewhere, in a later build — the
# same separation ENTITY_TYPES/ENTITY_PLATFORMS above draw between "what
# platform is this" and "who may see it".
#
# Unset DIS_EVENT_SCHEDULE_PATH -> load_event_schedule returns [] and
# nothing about an existing run changes: no new PDUs, and no new startup
# log line (print only happens when a path was actually given, same
# contract as load_entity_types/load_entity_platforms above).
#
# FAILS LOUDLY, UNLIKE THE DAMAGE MAP. reload_damage_map above skips a bad
# entry because it is re-read live, mid-demo, where stopping the process
# over a typo would be worse than ignoring one line. This file is read
# ONCE at start-up, same as DIS_ENTITY_TYPES_PATH/DIS_ENTITY_PLATFORMS_PATH
# — so a bad entry stops the whole sim rather than silently never firing.
_UINT32_MAX = 0xFFFFFFFF


def _schedule_uint32_key(key: object, path: str, index: int, field: str) -> int:
    try:
        value = int(str(key))
    except (TypeError, ValueError):
        raise SystemExit(f"{path}[{index}]: {field} key {key!r} is not an integer")
    if not (0 <= value <= _UINT32_MAX):
        raise SystemExit(f"{path}[{index}]: {field} key {key!r} is not a uint32")
    return value


def load_event_schedule(path: str | None, valid_entity_ids: set[int]) -> list[dict]:
    """Validate and return the DIS_EVENT_SCHEDULE_PATH schedule.

    `valid_entity_ids` is the set of entity ids THIS RUN will actually
    emit (the contiguous fleet or an explicit --entity-ids list) -- not the
    platform map or any ontology -- because a schedule entry for an id this
    process never emits would silently never fire.

    Any violation exits non-zero, naming the entry's index, and does NOT
    skip just that entry: a schedule that was supplied and then partially
    ignored is the worst outcome, same reasoning as load_entity_types'
    docstring.
    """
    path = path or os.getenv("DIS_EVENT_SCHEDULE_PATH", "").strip()
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise SystemExit(f"{path}: expected a JSON list of event schedule entries")

    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SystemExit(f"{path}[{i}]: expected an object")

        entity = item.get("entity")
        if not isinstance(entity, int) or isinstance(entity, bool):
            raise SystemExit(f"{path}[{i}]: 'entity' must be an int")
        if entity not in valid_entity_ids:
            raise SystemExit(
                f"{path}[{i}]: entity {entity} is not one of the ids this sim "
                f"emits ({sorted(valid_entity_ids)})"
            )

        at_s = item.get("at_s")
        if isinstance(at_s, bool) or not isinstance(at_s, (int, float)) or at_s < 0:
            raise SystemExit(f"{path}[{i}]: 'at_s' must be a number >= 0")

        event_type = item.get("event_type")
        if (isinstance(event_type, bool) or not isinstance(event_type, int)
                or not (0 <= event_type <= _UINT32_MAX)):
            raise SystemExit(f"{path}[{i}]: 'event_type' must be a uint32")

        variable_datums_raw = item.get("variable_datums")
        if not isinstance(variable_datums_raw, dict):
            raise SystemExit(f"{path}[{i}]: 'variable_datums' must be an object")
        variable_datums: dict[int, str] = {}
        for k, v in variable_datums_raw.items():
            did = _schedule_uint32_key(k, path, i, "variable_datums")
            if not isinstance(v, str):
                raise SystemExit(f"{path}[{i}]: variable_datums[{k!r}] must be a string")
            if len(v.encode("utf-8")) > 255:
                raise SystemExit(f"{path}[{i}]: variable_datums[{k!r}] exceeds 255 bytes")
            variable_datums[did] = v

        fixed_datums_raw = item.get("fixed_datums") or {}
        if not isinstance(fixed_datums_raw, dict):
            raise SystemExit(f"{path}[{i}]: 'fixed_datums' must be an object")
        fixed_datums: dict[int, int] = {}
        for k, v in fixed_datums_raw.items():
            did = _schedule_uint32_key(k, path, i, "fixed_datums")
            if (isinstance(v, bool) or not isinstance(v, int)
                    or not (0 <= v <= _UINT32_MAX)):
                raise SystemExit(f"{path}[{i}]: fixed_datums[{k!r}] must be a uint32")
            fixed_datums[did] = v

        repeat_s = item.get("repeat_s")
        if repeat_s is not None:
            if (isinstance(repeat_s, bool) or not isinstance(repeat_s, (int, float))
                    or repeat_s <= 0):
                raise SystemExit(f"{path}[{i}]: 'repeat_s' must be a number > 0")

        out.append({
            "entity": entity,
            "at_s": float(at_s),
            "event_type": event_type,
            "fixed_datums": fixed_datums,
            "variable_datums": variable_datums,
            "repeat_s": float(repeat_s) if repeat_s is not None else None,
        })

    print(f"dis-sim: loaded {len(out)} event schedule entr"
          f"{'y' if len(out) == 1 else 'ies'} from {path}", flush=True)
    return out


def due_schedule_entries(schedule: list[dict], next_due: list[float | None],
                         elapsed_s: float) -> list[int]:
    """Indices of `schedule` entries due to fire at `elapsed_s`.

    `next_due` is a same-length, caller-owned list of next-fire times
    (None once a one-shot entry has fired and will not fire again); this
    function advances it in place. Injecting `elapsed_s` as a plain float
    argument -- rather than reading a clock itself -- is what lets a test
    pass synthetic values directly, same contract as
    Entity.apply_destroy_schedule(elapsed_s).
    """
    fired: list[int] = []
    for i, entry in enumerate(schedule):
        due = next_due[i]
        if due is None or elapsed_s < due:
            continue
        fired.append(i)
        repeat_s = entry.get("repeat_s")
        next_due[i] = due + repeat_s if repeat_s else None
    return fired


def event_report_pdu(entity: Entity, entry: dict, exercise_id: int,
                     protocol_version: int) -> EventReportPdu:
    """One Event Report PDU (type 21) for one fired schedule entry.

    originatingEntityID is the entity the schedule entry names, site/app as
    this sim's own (same convention as to_remove_entity_pdu). receivingEntityID
    is left at Open-DIS's own all-zero EntityID(0, 0, 0) default: DIS has no
    separate standard encoding for "no specific receiver", and all-zero is
    this codebase's existing convention for an unaddressed party (see
    to_remove_entity_pdu's originating entity 0 for "no specific entity").

    PURE TRANSPORT: sends exactly what the schedule entry says. eventType
    and the datum ids are opaque integers here, same as everywhere else in
    this module's event-schedule code.
    """
    fixed = [FixedDatum(did, val) for did, val in entry["fixed_datums"].items()]
    variable = []
    for did, text in entry["variable_datums"].items():
        data = list(text.encode("utf-8"))
        variable.append(VariableDatum(did, len(data) * 8, data))

    pdu = EventReportPdu(eventType=entry["event_type"], fixedDatumRecords=fixed,
                         variableDatumRecords=variable)
    pdu.protocolVersion = protocol_version
    pdu.exerciseID = exercise_id
    pdu.pduType = 21        # Event Report
    pdu.protocolFamily = 5  # Simulation Management
    pdu.pduStatus = 0
    pdu.originatingEntityID.siteID = entity.site_id
    pdu.originatingEntityID.applicationID = entity.app_id
    pdu.originatingEntityID.entityID = entity.entity_id
    return pdu


# ---------------------------------------------------------------------------
# Condition schedule -- --condition-schedule / DIS_CONDITION_SCHEDULE_PATH
# ---------------------------------------------------------------------------
# Flips three independent claims about one entity on a timetable, so a
# receiver can be shown each source alone and then two at once: the Entity
# State appearance bits (damage, power plant, deactivated), an Electromagnetic
# Emission PDU (nominal / fewer beams / lower power / explicit nothing / no
# PDU at all) and a Data PDU carrying one health datum. Like the event
# schedule this is PURE TRANSPORT of what the file says; what each claim
# MEANS is the receiving side's ontology (dis_condition.yaml), not this file.
#
# Unset -> load_condition_schedule returns None and nothing about an existing
# run changes: no new PDUs, no new startup log line.
#
# FAILS LOUDLY for the same reason load_event_schedule does: read once at
# start-up, so a bad entry stops the sim rather than silently never firing.
_CONDITION_EMISSION_MODES = ("nominal", "reduced_beams", "reduced_power", "zero", "silent")
_CONDITION_SET_KEYS = ("damage", "power_plant", "deactivated", "emission", "datum_health")
_CONDITION_DEFAULT_DATUM_ID = 61000   # locally assigned, as in the receiving ontology
_CONDITION_NOMINAL_HEALTH = 92
_CONDITION_DEFAULT_REDUCED_DB = 15.0


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _condition_step_state(raw_set: dict) -> dict:
    """The WHOLE condition state for one step: given keys, else nominal.

    An entity that is off does not radiate, so emission defaults to silent and
    there is no health datum when the power plant is off, the entity is
    deactivated, or damage is destroyed. An explicit key always wins over that
    derived default (a powered-on entity made silent is the sensor-failed case).
    """
    damage = raw_set.get("damage", "none")
    power_plant = raw_set.get("power_plant", "on")
    deactivated = raw_set.get("deactivated", False)
    off = power_plant == "off" or deactivated is True or damage == "destroyed"
    emission = raw_set.get("emission", "silent" if off else "nominal")
    datum_health = raw_set["datum_health"] if "datum_health" in raw_set else (
        None if off else _CONDITION_NOMINAL_HEALTH)
    return {"damage": damage, "power_plant": power_plant, "deactivated": deactivated,
            "emission": emission, "datum_health": datum_health}


def load_condition_schedule(path: str | None, valid_entity_ids: set[int]) -> dict | None:
    """Validate and return the --condition-schedule file, or None when unset.

    Returns {"cycle_s", "datum_id", "profiles": {entity: profile},
    "steps": {entity: [step, ...]}} with each entity's steps sorted by at_s
    and carrying their resolved whole-state fields. Any violation exits
    non-zero naming the entry; nothing is skipped (see load_event_schedule).
    """
    path = path or os.getenv("DIS_CONDITION_SCHEDULE_PATH", "").strip()
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a JSON object")

    cycle_s = raw.get("cycle_s")
    if cycle_s is not None and (not _is_number(cycle_s) or cycle_s <= 0):
        raise SystemExit(f"{path}: 'cycle_s' must be a number > 0 or absent")

    datum_id = raw.get("datum_id", _CONDITION_DEFAULT_DATUM_ID)
    if isinstance(datum_id, bool) or not isinstance(datum_id, int) or not (0 <= datum_id <= _UINT32_MAX):
        raise SystemExit(f"{path}: 'datum_id' must be a uint32")

    profiles_raw = raw.get("emission_profiles", {})
    if not isinstance(profiles_raw, dict):
        raise SystemExit(f"{path}: 'emission_profiles' must be an object")
    profiles: dict[int, dict] = {}
    for key, prof in profiles_raw.items():
        where = f"{path}: emission_profiles[{key!r}]"
        eid = _schedule_uint32_key(key, path, 0, "emission_profiles")
        if eid not in valid_entity_ids:
            raise SystemExit(f"{where}: entity {eid} is not one of the ids this sim "
                             f"emits ({sorted(valid_entity_ids)})")
        if not isinstance(prof, dict):
            raise SystemExit(f"{where}: expected an object")
        name = prof.get("emitter_name")
        if isinstance(name, bool) or not isinstance(name, int) or not (0 <= name <= 0xFFFF):
            raise SystemExit(f"{where}: 'emitter_name' must be a uint16")
        beams = prof.get("beams")
        if isinstance(beams, bool) or not isinstance(beams, int) or not (1 <= beams <= 255):
            raise SystemExit(f"{where}: 'beams' must be an int 1..255")
        erp = prof.get("erp_dbm")
        if not _is_number(erp):
            raise SystemExit(f"{where}: 'erp_dbm' must be a number")
        red = prof.get("reduced_power_db", _CONDITION_DEFAULT_REDUCED_DB)
        if not _is_number(red) or red < 0:
            raise SystemExit(f"{where}: 'reduced_power_db' must be a number >= 0")
        profiles[eid] = {"emitter_name": name, "beams": beams, "erp_dbm": float(erp),
                         "reduced_power_db": float(red)}

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        raise SystemExit(f"{path}: 'steps' must be a list")
    steps: dict[int, list[dict]] = {}
    for i, item in enumerate(steps_raw):
        where = f"{path}.steps[{i}]"
        if not isinstance(item, dict):
            raise SystemExit(f"{where}: expected an object")
        entity = item.get("entity")
        if not isinstance(entity, int) or isinstance(entity, bool):
            raise SystemExit(f"{where}: 'entity' must be an int")
        if entity not in valid_entity_ids:
            raise SystemExit(f"{where}: entity {entity} is not one of the ids this sim "
                             f"emits ({sorted(valid_entity_ids)})")
        at_s = item.get("at_s")
        if not _is_number(at_s) or at_s < 0:
            raise SystemExit(f"{where}: 'at_s' must be a number >= 0")
        if cycle_s is not None and at_s >= cycle_s:
            raise SystemExit(f"{where}: 'at_s' {at_s} must be < cycle_s {cycle_s}")
        s = item.get("set")
        if not isinstance(s, dict):
            raise SystemExit(f"{where}: 'set' must be an object")
        unknown = sorted(set(s) - set(_CONDITION_SET_KEYS))
        if unknown:
            raise SystemExit(f"{where}: unknown key(s) in 'set': {unknown} "
                             f"(allowed: {list(_CONDITION_SET_KEYS)})")
        if "damage" in s and s["damage"] not in DAMAGE_LEVELS:
            raise SystemExit(f"{where}: 'damage' must be one of {sorted(DAMAGE_LEVELS)}")
        if "power_plant" in s and s["power_plant"] not in ("on", "off"):
            raise SystemExit(f"{where}: 'power_plant' must be \"on\" or \"off\"")
        if "deactivated" in s and not isinstance(s["deactivated"], bool):
            raise SystemExit(f"{where}: 'deactivated' must be a bool")
        if "emission" in s and s["emission"] not in _CONDITION_EMISSION_MODES:
            raise SystemExit(f"{where}: 'emission' must be one of {list(_CONDITION_EMISSION_MODES)}")
        if "datum_health" in s and s["datum_health"] is not None:
            h = s["datum_health"]
            if isinstance(h, bool) or not isinstance(h, int) or not (0 <= h <= 100):
                raise SystemExit(f"{where}: 'datum_health' must be null or an int 0..100")
        state = _condition_step_state(s)
        if state["emission"] != "silent" and entity not in profiles:
            raise SystemExit(f"{where}: emission {state['emission']!r} needs an "
                             f"emission_profiles entry for entity {entity}")
        steps.setdefault(entity, []).append(
            {"at_s": float(at_s), "set": dict(s), **state, "_src": i})

    for entity, lst in steps.items():
        lst.sort(key=lambda st: st["at_s"])
        first = lst[0]
        # The all-zero reason (see Entity.powerplant_on): the first claim must
        # be a powered-on one, or the receiver has no prior non-zero field and
        # reads the zero as silence.
        if (first["power_plant"] == "off" or first["deactivated"]) and first["damage"] == "none":
            raise SystemExit(f"{path}.steps[{first['_src']}]: the first step of entity {entity} "
                             f"is a power-off/deactivated step with damage none; open with a "
                             f"powered-on step so the receiver has a non-zero appearance to "
                             f"compare against")
        for idx, st in enumerate(lst):
            st["idx"] = idx
            del st["_src"]

    print(f"dis-sim: loaded condition schedule from {path}: "
          f"{sum(len(v) for v in steps.values())} step(s) for entities {sorted(steps)}"
          f"{'' if cycle_s is None else f', cycle {cycle_s:g}s'}", flush=True)
    return {"cycle_s": float(cycle_s) if cycle_s is not None else None,
            "datum_id": datum_id, "profiles": profiles, "steps": steps}


def resolve_condition_step(steps: list[dict], cycle_s: float | None, elapsed_s: float) -> dict | None:
    """The last step with at_s <= (elapsed mod cycle_s, or elapsed), else None."""
    t = elapsed_s % cycle_s if cycle_s else elapsed_s
    current = None
    for st in steps:
        if st["at_s"] <= t:
            current = st
    return current


def resolve_condition_schedule(entities: list[Entity], schedule: dict | None) -> None:
    """Hand each entity its own steps, profile and cycle."""
    if not schedule:
        return
    for e in entities:
        e.condition_steps = schedule["steps"].get(e.entity_id, [])
        e.condition_cycle_s = schedule["cycle_s"]
        e.condition_profile = schedule["profiles"].get(e.entity_id)


def _with_length(pdu):
    """Set pdu.length to the serialized size. opendis 1.0 leaves the header
    length field at 0 for the caller to set; a receiver that trusts it (or
    this module's own round-trip test) needs the real value."""
    pdu.length = len(serialize(pdu))
    return pdu


def emission_pdu(entity: Entity, profile: dict, mode: str, exercise_id: int,
                 protocol_version: int) -> ElectromagneticEmissionsPdu:
    """One Electromagnetic Emission PDU (type 23) for `mode`.

    pduType 23 and protocolFamily 6 (Distributed Emission Regeneration) are
    opendis 1.0's own class defaults for ElectromagneticEmissionsPdu /
    DistributedEmissionsFamilyPdu; they are set explicitly below to the same
    values. "silent" is no PDU at all, so it is refused here -- the caller
    skips the send.

    Counts and lengths opendis 1.0 leaves to the caller, set here:
    systemDataLength and beamDataLength (32-bit words) and the PDU header
    length. numberOfSystems and numberOfBeams it derives itself. The beam
    record is 52 bytes (13 words) with no track/jam records; the system
    record is 20 bytes of header + location plus its beams.
    jammingModeSequence is left as an object in opendis, which cannot
    serialize, so it is set to 0.
    """
    if mode == "silent":
        raise ValueError("silent is no EE PDU at all; do not build one")
    if mode not in _CONDITION_EMISSION_MODES:
        raise ValueError(f"unknown emission mode {mode!r}")
    systems = []
    if mode != "zero":
        n = profile["beams"]
        erp = profile["erp_dbm"]
        if mode == "reduced_beams":
            n = max(1, n // 2)
        elif mode == "reduced_power":
            erp = erp - profile.get("reduced_power_db", _CONDITION_DEFAULT_REDUCED_DB)
        beams = [
            EmissionSystemBeamRecord(
                beamDataLength=13, beamIDNumber=b + 1,
                fundamentalParameterData=EEFundamentalParameterData(effectiveRadiatedPower=erp),
            )
            for b in range(n)
        ]
        for b in beams:
            b.jammingModeSequence = 0
        systems.append(EmissionSystemRecord(
            systemDataLength=5 + 13 * n,
            emitterSystem=EmitterSystem(emitterName=profile["emitter_name"]),
            beamRecords=beams,
        ))
    pdu = ElectromagneticEmissionsPdu(systems=systems)
    pdu.protocolVersion = protocol_version
    pdu.exerciseID = exercise_id
    pdu.pduType = 23        # Electromagnetic Emission
    pdu.protocolFamily = 6  # Distributed Emission Regeneration
    pdu.pduStatus = 0
    pdu.emittingEntityID.siteID = entity.site_id
    pdu.emittingEntityID.applicationID = entity.app_id
    pdu.emittingEntityID.entityID = entity.entity_id
    return _with_length(pdu)


def health_data_pdu(entity: Entity, datum_id: int, value: int, exercise_id: int,
                    protocol_version: int) -> DataPdu:
    """One Data PDU (type 20) carrying a single fixed health datum.

    originatingEntityID is the entity, as event_report_pdu does; the receiver
    is left at the all-zero EntityID. The datum id is locally assigned for
    this exchange, and the value is a uint32 on the wire (the 0-100 scale is
    the receiver's ontology, not this function's). Header length is set by the
    caller in opendis 1.0, so it is set here.
    """
    pdu = DataPdu(fixedDatumRecords=[FixedDatum(datum_id, value)])
    pdu.protocolVersion = protocol_version
    pdu.exerciseID = exercise_id
    pdu.pduType = 20        # Data
    pdu.protocolFamily = 5  # Simulation Management
    pdu.pduStatus = 0
    pdu.originatingEntityID.siteID = entity.site_id
    pdu.originatingEntityID.applicationID = entity.app_id
    pdu.originatingEntityID.entityID = entity.entity_id
    return _with_length(pdu)


def serialize(pdu: EntityStatePdu | RemoveEntityPdu | EventReportPdu
              | ElectromagneticEmissionsPdu | DataPdu) -> bytes:
    bio = BytesIO()
    pdu.serialize(DataOutputStream(bio))
    return bio.getvalue()


# The literal fallback for --host when neither --host nor DIS_TARGET_HOST is
# set. Named so main() can tell "the operator never touched --host" apart
# from "the operator set --host (or DIS_TARGET_HOST) to something" -- that
# distinction is what makes --targets + a non-default --host a startup error
# (see parse_targets / MULTI-TARGET FAN-OUT above) rather than something
# silently resolved one way or the other.
_DEFAULT_HOST = "127.0.0.1"


def parse_targets(spec: str) -> list[tuple[str, int]]:
    """Parse DIS_TARGETS / --targets: comma-separated host:port entries.

    Empty/unset -> [] (no fan-out; callers fall back to (--host, --port)).
    A malformed entry exits non-zero naming it, same fail-closed convention
    as this file's other --*-schedule parsers.
    """
    targets: list[tuple[str, int]] = []
    for tok in (t.strip() for t in spec.split(",")):
        if not tok:
            continue
        if ":" not in tok:
            raise SystemExit(f"--targets: {tok!r} is not host:port")
        host, _, port_s = tok.rpartition(":")
        if not host or not port_s.isdigit():
            raise SystemExit(f"--targets: {tok!r} is not host:port")
        targets.append((host, int(port_s)))
    return targets


def send_pdu(sock: socket.socket, payload: bytes, host: str, port: int,
             targets: list[tuple[str, int]]) -> None:
    """The one sock.sendto chokepoint for every PDU this tool emits.

    `targets`, when non-empty, fans `payload` out to every entry instead of
    just (host, port) -- the unicast stand-in for a shared multicast segment
    described under MULTI-TARGET FAN-OUT above. Empty `targets` (the default,
    --targets/DIS_TARGETS unset) sends to (host, port) exactly as every call
    site did before --targets existed -- no behaviour change in that case.
    """
    if targets:
        # Every target gets its attempt even when an earlier one fails: one
        # unreachable listener must not silence the others. The first error
        # is re-raised afterwards so the caller's error count still sees it.
        first_error: OSError | None = None
        for dest in targets:
            try:
                sock.sendto(payload, dest)
            except OSError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
    else:
        sock.sendto(payload, (host, port))


def check_targets_host_conflict(host: str, targets: list[tuple[str, int]]) -> None:
    """Refuse --targets combined with a non-default --host: with both set
    there is no single answer for which destination the operator meant, so
    fail closed at startup rather than silently picking one (see
    MULTI-TARGET FAN-OUT above)."""
    if targets and host != _DEFAULT_HOST:
        raise SystemExit(
            "--targets and a non-default --host cannot both be set "
            f"(--host={host!r}); drop --host/DIS_TARGET_HOST or drop "
            "--targets/DIS_TARGETS"
        )


def _stop_on_sigterm(signum, frame) -> None:
    """SIGTERM ends the run the same way Ctrl-C does. In a container this
    process is PID 1, and PID 1 gets no default action for SIGTERM: without a
    handler it ignores the stop, keeps sending for the whole termination
    grace period, and overlaps the replacement pod -- two senders for the same
    entity ids, each resetting the other's posture clock."""
    raise KeyboardInterrupt


def main() -> int:
    p = argparse.ArgumentParser(description="DIS EntityState PDU generator")
    p.add_argument("--host", default=os.getenv("DIS_TARGET_HOST", _DEFAULT_HOST),
                   help="destination host (sensor-ingest)")
    p.add_argument("--port", type=int, default=int(os.getenv("DIS_TARGET_PORT", "62040")))
    p.add_argument("--targets", default=os.getenv("DIS_TARGETS", ""),
                   help="Comma-separated host:port list. When set, every PDU "
                        "goes to every target instead of --host/--port -- a "
                        "unicast stand-in for a shared multicast segment "
                        "where every listener hears every entity. Cannot be "
                        "combined with a non-default --host.")
    p.add_argument("--entities", type=int, default=int(os.getenv("DIS_ENTITIES", "8")))
    # Explicit entity ids, comma separated. When given, this REPLACES the
    # contiguous `1000..1000+N-1` fleet rather than adding to it, and
    # --entities is ignored -- an id list that silently also emitted eight
    # other assets would make a single-asset fixture impossible to state.
    #
    # Every id still has to be pinned in the platform map; platform_for()
    # refuses an unpinned id at start-up, and that refusal is load-bearing here
    # (see DEFAULT_ENTITY_PLATFORMS).
    p.add_argument("--entity-ids", default=os.getenv("DIS_ENTITY_IDS", ""),
                   help="Comma-separated entity ids to emit INSTEAD of the "
                        "contiguous default fleet, e.g. '1099'.")
    p.add_argument("--interval", type=float, default=float(os.getenv("DIS_INTERVAL_S", "5.0")),
                   help="heartbeat seconds per entity (VR-Forces default is ~5s)")
    p.add_argument("--exercise-id", type=int, default=int(os.getenv("DIS_EXERCISE_ID", "1")))
    p.add_argument("--protocol-version", type=int, default=int(os.getenv("DIS_PROTOCOL_VERSION", "7")))
    p.add_argument("--site-id", type=int, default=int(os.getenv("DIS_SITE_ID", "1")))
    p.add_argument("--app-id", type=int, default=int(os.getenv("DIS_APP_ID", "1")))
    p.add_argument("--seed", type=int, default=int(os.getenv("DIS_SEED", "1337")))
    p.add_argument("--damage", default=os.getenv("DIS_DAMAGE", ""),
                   help="Emit entity appearance with this damage level "
                        "(none|slight|moderate|destroyed). Omitted = the field "
                        "stays 0 and NO claim is made, which is the default and "
                        "is NOT the same as 'none'.")
    p.add_argument("--damage-map", default=os.getenv("DIS_DAMAGE_MAP_PATH", ""),
                   help="Path to a JSON file of per-asset damage overrides, "
                        "re-read every tick: {\"dis:1:1:1005\": \"moderate\"}. "
                        "Keys accept the canonical dis:site:app:entity or a "
                        "bare entity number; values are a damage level, "
                        "\"unspecified\" to emit NO claim for that asset, "
                        "\"deactivated\" to additionally set the appearance "
                        "deactivated bit, \"silent\" to stop transmitting that "
                        "entity's ESPDUs, or \"removed\" to send one Remove "
                        "Entity PDU and then stop transmitting for good "
                        "(NOT reversible by clearing the entry). A per-asset "
                        "entry overrides --damage. Absent file changes "
                        "nothing.")
    p.add_argument("--destroy-schedule", default=os.getenv("DIS_DESTROY_SCHEDULE", ""),
                   help="Comma-separated ASSET@SECONDS: once SECONDS have "
                        "elapsed since process start, mark ASSET destroyed "
                        "and keep transmitting its ESPDUs (ADR-0044: "
                        "destroyed + reporting is a real state). ASSET "
                        "accepts the same forms as --damage-map "
                        "(dis:site:app:entity or a bare entity number). "
                        "One-shot: never reverts while this process runs; "
                        "a restart replays the schedule from zero. An "
                        "explicit --damage-map entry for the same asset "
                        "wins over the schedule.")
    p.add_argument("--damage-fraction", type=float,
                   default=float(os.getenv("DIS_DAMAGE_FRACTION", "1.0")),
                   help="Fraction of entities that carry the --damage level; "
                        "the rest emit appearance with damage none. Only "
                        "meaningful with --damage.")
    p.add_argument("--fire-schedule", default=os.getenv("DIS_FIRE_SCHEDULE", ""),
                   help="Comma-separated L@T:E:Q[:TGT]: at elapsed T seconds, "
                        "send one Fire PDU from launcher L (firingEntityID) "
                        "with eventNumber E (site/app = this sim's own "
                        "--site-id/--app-id), quantity Q, and target TGT "
                        "(default 0:0:0). L/TGT accept dis:site:app:entity "
                        "or a bare entity number. A bad entry exits non-zero "
                        "naming it.")
    p.add_argument("--detonate-schedule", default=os.getenv("DIS_DETONATE_SCHEDULE", ""),
                   help="Comma-separated E@T:R[:L]: at elapsed T seconds, "
                        "send one Detonation PDU for fire event E with "
                        "detonationResult R. The firing entity is E's "
                        "--fire-schedule launcher unless L overrides it; L "
                        "is REQUIRED when E is not in --fire-schedule (an "
                        "orphan detonation). A bad entry exits non-zero "
                        "naming it.")
    p.add_argument("--resupply-schedule", default=os.getenv("DIS_RESUPPLY_SCHEDULE", ""),
                   help="Comma-separated L@T[/R]:Q[:S]: at elapsed T seconds, "
                        "send one Resupply Received PDU to launcher L "
                        "(receivingEntityID) from supplier S "
                        "(supplyingEntityID, default 0:0:0) carrying one "
                        "supply of --munition-type, quantity Q (> 0). With "
                        "/R, re-send every R seconds after T. L/S accept "
                        "dis:site:app:entity or a bare entity number. A bad "
                        "entry exits non-zero naming it.")
    p.add_argument("--munition-type", default=os.getenv("DIS_MUNITION_TYPE", "2.9.225.2.1.1.0"),
                   help="DIS 7-tuple 'kind.domain.country.category.subcategory."
                        "specific.extra' for descriptor.munitionType on every "
                        "scheduled Fire/Detonation. Default is a placeholder "
                        "tuple, not a claim about any real munition.")
    p.add_argument("--posture-schedule", default=os.getenv("DIS_POSTURE_SCHEDULE", ""),
                   help="Comma-separated ENTITY:T:ACTION: at elapsed T "
                        "seconds, apply ACTION (raise|stow|move|stop) to "
                        "ENTITY. raise/stow set/clear the launcher-raised "
                        "appearance bit; move holds a constant "
                        f"{_POSTURE_MOVE_SPEED_MPS:g} m/s, stop holds 0 -- "
                        "either way this OVERRIDES the entity's random "
                        "motion entirely, for as long as it has any "
                        "--posture-schedule entry. ENTITY accepts the same "
                        "forms as --damage-map (dis:site:app:entity or a "
                        "bare entity number). Power plant stays on. A bad "
                        "entry exits non-zero naming it.")
    p.add_argument("--condition-schedule", default=os.getenv("DIS_CONDITION_SCHEDULE_PATH", ""),
                   help="Path to a JSON file that flips an entity's appearance "
                        "(damage, power plant, deactivated), its Electromagnetic "
                        "Emission PDU (nominal | reduced_beams | reduced_power | "
                        "zero | silent) and a Data PDU health datum on a timed "
                        "list of steps, each step setting the WHOLE state (keys "
                        "left out are nominal), optionally repeating every "
                        "cycle_s. A live --damage-map entry, then a fired "
                        "--destroy-schedule, win over a step's appearance. A "
                        "power-off step needs a prior powered-on step: an "
                        "undamaged power-off encodes to an all-zero field that "
                        "the receiver reads as off only after a non-zero one. "
                        "A bad entry exits non-zero naming it.")
    p.add_argument("--mobility-kill", action="store_true",
                   help="Set the mobility/propulsion-kill bit on damaged "
                        "entities.")
    p.add_argument("--firepower-kill", action="store_true",
                   help="Set the firepower-kill bit on damaged LAND entities. "
                        "Refused for other domains, where the bit means "
                        "something else.")
    p.add_argument("--list-types", action="store_true",
                   help="print the recognised entity types and exit")
    args = p.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    targets = parse_targets(args.targets)
    check_targets_host_conflict(args.host, targets)

    if args.list_types:
        print("Built-in DIS entity types (each a key in "
              "openddil-contracts ontology/dis_entity_types.yaml):")
        for t, variant in RECOGNISED_TYPES:
            print("  %-22s %s" % ("_".join(str(v) for v in t), variant))
        print("\nPinned platforms:")
        for key, variant in ENTITY_PLATFORMS.items():
            print("  %-22s %s" % (key, variant))
        print("\nAnything not listed resolves to _default -> UNKNOWN.")
        return 0

    rng = random.Random(args.seed)
    explicit_ids: list[int] = []
    for tok in (t.strip() for t in args.entity_ids.split(",")):
        if not tok:
            continue
        if not tok.isdigit():
            raise SystemExit(f"--entity-ids: {tok!r} is not an entity number")
        explicit_ids.append(int(tok))
    if explicit_ids:
        print(f"dis-sim: emitting {len(explicit_ids)} explicit entity id(s): "
              f"{explicit_ids} (--entities ignored)", flush=True)
        entities = [Entity(i, args.site_id, args.app_id, rng, entity_id=eid)
                    for i, eid in enumerate(explicit_ids)]
    else:
        entities = [Entity(i, args.site_id, args.app_id, rng) for i in range(args.entities)]

    # Apply the damage profile, if the operator asked for one. Without
    # --damage nothing changes and appearance stays 0 -- silence, not health.
    if args.damage:
        if args.damage not in DAMAGE_LEVELS:
            print(f"--damage must be one of {sorted(DAMAGE_LEVELS)}", file=sys.stderr)
            return 2
        n_damaged = max(1, round(len(entities) * max(0.0, min(1.0, args.damage_fraction))))
        for i, ent in enumerate(entities):
            ent.emit_appearance = True          # every entity now MAKES a claim
            if i < n_damaged:
                ent.damage = args.damage
                ent.mobility_kill = args.mobility_kill
                # Land only. Refused elsewhere by appearance_bits(), so an
                # air entity in the damaged set simply does not carry it.
                ent.firepower_kill = args.firepower_kill and ent.entity_type[1] == 1
        print(f"appearance: emitting on all {len(entities)} entities; "
              f"{n_damaged} at damage={args.damage}", file=sys.stderr)

    # Freeze the post-profile state as each entity's BASELINE, so a cleared
    # override returns here rather than to a hardcoded default.
    for ent in entities:
        ent._baseline_damage = ent.damage
        ent._baseline_emit = ent.emit_appearance

    # --destroy-schedule: parsed and matched once at start-up (unlike
    # --damage-map, this is not re-read from disk every tick).
    destroy_schedule = parse_destroy_schedule(args.destroy_schedule)
    if destroy_schedule:
        resolve_destroy_schedule(entities, destroy_schedule)

    # --posture-schedule: parsed and matched once at start-up, same point as
    # --destroy-schedule above. Unlike that schedule, a bad entry here is
    # fatal (same discipline as --fire-schedule/--detonate-schedule below).
    posture_schedule = parse_posture_schedule(args.posture_schedule, args.site_id, args.app_id)
    if posture_schedule:
        resolve_posture_schedule(entities, posture_schedule)

    # --condition-schedule: validated and handed out once at start-up. Unset
    # -> None and no entity has steps, so the run is unchanged.
    condition_schedule = load_condition_schedule(
        args.condition_schedule, {e.entity_id for e in entities})
    resolve_condition_schedule(entities, condition_schedule)

    # --fire-schedule / --detonate-schedule: parsed and cross-linked once at
    # start-up, same point as --destroy-schedule above. Unlike that schedule,
    # a bad entry here is fatal (see parse_fire_schedule's module comment).
    munition_type = parse_munition_type(args.munition_type)
    fire_schedule = parse_fire_schedule(args.fire_schedule, args.site_id, args.app_id)
    fire_by_event = {entry["event"]: entry for entry in fire_schedule}
    detonate_schedule = parse_detonate_schedule(
        args.detonate_schedule, args.site_id, args.app_id, fire_by_event,
    )
    for entry in fire_schedule:
        LOG.info("fire schedule: launcher=dis:%d:%d:%d event=%d qty=%d at t+%gs%s",
                 *entry["launcher"], entry["event"], entry["quantity"], entry["t"],
                 "" if entry["target"] == (0, 0, 0)
                 else f" target=dis:{entry['target'][0]}:{entry['target'][1]}:{entry['target'][2]}")
    resupply_schedule = parse_resupply_schedule(
        args.resupply_schedule, args.site_id, args.app_id,
    )
    for entry in resupply_schedule:
        LOG.info("resupply schedule: launcher=dis:%d:%d:%d qty=%g at t+%gs%s%s",
                 *entry["launcher"], entry["quantity"], entry["t"],
                 f" every {entry['repeat_s']:g}s" if entry.get("repeat_s") else "",
                 "" if entry["supplier"] == (0, 0, 0)
                 else f" supplier=dis:{entry['supplier'][0]}:{entry['supplier'][1]}:{entry['supplier'][2]}")
    for entry in detonate_schedule:
        LOG.info("detonate schedule: event=%d launcher=dis:%d:%d:%d result=%d at t+%gs",
                 entry["event"], *entry["launcher"], entry["result"], entry["t"])

    # DIS_EVENT_SCHEDULE_PATH: validated once at start-up, same as the
    # entity type/platform maps above -- a bad entry exits non-zero here
    # rather than mid-run. Unset -> [] and nothing below ever fires, so an
    # existing run's behaviour (and its startup log) is unchanged.
    entities_by_id = {e.entity_id: e for e in entities}
    event_schedule = load_event_schedule(None, set(entities_by_id))
    event_schedule_next_due = [entry["at_s"] for entry in event_schedule]
    # Fire/Detonation schedules are one-shot, same contract as --destroy-
    # schedule: due_schedule_entries() already gives that for free when an
    # entry carries no "repeat_s" key (entry.get("repeat_s") is None ->
    # next_due[i] set to None after firing, never due again).
    fire_schedule_next_due = [entry["t"] for entry in fire_schedule]
    detonate_schedule_next_due = [entry["t"] for entry in detonate_schedule]
    # Resupply entries repeat only when they carry "repeat_s" (the /R form).
    resupply_schedule_next_due = [entry["t"] for entry in resupply_schedule]
    for entry in event_schedule:
        LOG.info(
            "event schedule: entity %d event_type=%d at t+%gs%s",
            entry["entity"], entry["event_type"], entry["at_s"],
            f" every {entry['repeat_s']}s" if entry["repeat_s"] else "",
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    if targets:
        LOG.info(
            "dis-sim -> fan-out to %d target(s): %s | %d entities | %.1fs "
            "heartbeat | DIS v%d exercise %d",
            len(targets), ["%s:%d" % t for t in targets], len(entities),
            args.interval, args.protocol_version, args.exercise_id,
        )
    else:
        LOG.info(
            "dis-sim -> %s:%d | %d entities | %.1fs heartbeat | DIS v%d exercise %d",
            args.host, args.port, len(entities), args.interval,
            args.protocol_version, args.exercise_id,
        )
    for e in entities:
        LOG.info("  entity %d  %-14s  %s", e.entity_id, e.variant, e.marking)

    sent = 0
    errors = 0
    last_report = time.time()
    # Spread emissions across the interval rather than bursting, which is both
    # closer to a real CGF and easier to watch a buffer fill against.
    per_entity_gap = args.interval / max(len(entities), 1)

    overridden: set[int] = set()
    start_s = time.monotonic()
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    try:
        while True:
            # Re-read the per-asset control once per sweep, not per entity:
            # one stat() per tick, and every entity in a sweep sees the same
            # desired state rather than a file that changed mid-loop.
            dmap = reload_damage_map(args.damage_map)
            elapsed_s = time.monotonic() - start_s

            # Scheduled Event Report PDUs (DIS_EVENT_SCHEDULE_PATH), once per
            # sweep -- not per entity, same cadence as the damage-map reload
            # above. Pure transport: eventType and the datum ids are opaque
            # integers here, never interpreted.
            for idx in due_schedule_entries(event_schedule, event_schedule_next_due, elapsed_s):
                entry = event_schedule[idx]
                entity = entities_by_id[entry["entity"]]
                pdu = event_report_pdu(entity, entry, args.exercise_id, args.protocol_version)
                try:
                    send_pdu(sock, serialize(pdu), args.host, args.port, targets)
                    sent += 1
                    LOG.info(
                        "event report: entity=%d event_type=%d fixed_datum_ids=%s "
                        "variable_datum_ids=%s", entity.entity_id, entry["event_type"],
                        sorted(entry["fixed_datums"]), sorted(entry["variable_datums"]),
                    )
                except OSError as exc:
                    errors += 1
                    LOG.warning("event report send failed for entity %d: %s",
                               entity.entity_id, exc)

            # Scheduled Fire PDUs (--fire-schedule), same once-per-sweep
            # cadence as the event schedule above.
            for idx in due_schedule_entries(fire_schedule, fire_schedule_next_due, elapsed_s):
                entry = fire_schedule[idx]
                pdu = fire_pdu(entry, munition_type, args.site_id, args.app_id,
                               args.exercise_id, args.protocol_version)
                try:
                    send_pdu(sock, serialize(pdu), args.host, args.port, targets)
                    sent += 1
                    LOG.info("fire sent event=%d launcher=dis:%d:%d:%d qty=%d",
                             entry["event"], *entry["launcher"], entry["quantity"])
                except OSError as exc:
                    errors += 1
                    LOG.warning("fire send failed for event %d: %s", entry["event"], exc)

            # Scheduled Detonation PDUs (--detonate-schedule), same cadence.
            for idx in due_schedule_entries(detonate_schedule, detonate_schedule_next_due, elapsed_s):
                entry = detonate_schedule[idx]
                pdu = detonation_pdu(entry, munition_type, args.site_id, args.app_id,
                                     args.exercise_id, args.protocol_version)
                try:
                    send_pdu(sock, serialize(pdu), args.host, args.port, targets)
                    sent += 1
                    LOG.info("detonation sent event=%d result=%d",
                             entry["event"], entry["result"])
                except OSError as exc:
                    errors += 1
                    LOG.warning("detonation send failed for event %d: %s",
                               entry["event"], exc)

            # Scheduled Resupply Received PDUs (--resupply-schedule), same
            # cadence; repeat entries come back round via repeat_s.
            for idx in due_schedule_entries(resupply_schedule, resupply_schedule_next_due, elapsed_s):
                entry = resupply_schedule[idx]
                pdu = resupply_pdu(entry, munition_type, args.exercise_id,
                                   args.protocol_version)
                try:
                    send_pdu(sock, serialize(pdu), args.host, args.port, targets)
                    sent += 1
                    LOG.info("resupply sent launcher=dis:%d:%d:%d qty=%g",
                             *entry["launcher"], entry["quantity"])
                except OSError as exc:
                    errors += 1
                    LOG.warning("resupply send failed for launcher dis:%d:%d:%d: %s",
                                *entry["launcher"], exc)

            now_overridden = set()
            for e in entities:
                if e.apply_damage_override(dmap):
                    now_overridden.add(e.entity_id)
                elif e.apply_destroy_schedule(elapsed_s) and not e._destroy_logged:
                    # Logged once on the fire, not every tick thereafter.
                    LOG.info("destroy schedule fired: dis:%d:%d:%d",
                             e.site_id, e.app_id, e.entity_id)
                    e._destroy_logged = True
                elif e.apply_posture_schedule(elapsed_s):
                    # apply_posture_schedule logs its own fires (several
                    # entries over the run, unlike destroy's single edge),
                    # and -- same reasoning as the destroy branch above --
                    # a live --damage-map/--destroy-schedule claim on this
                    # tick wins over posture, so this path is only reached
                    # when neither of those applied.
                    pass
                # A live damage-map override or a fired destroy-schedule owns
                # the appearance fields; the condition step then drives only
                # the EE and Data PDUs. Runs after posture so the step's
                # deactivated/damage apply, while launcher_raised and motion
                # stay posture's.
                appearance_free = (e.entity_id not in now_overridden
                                   and not (e.destroy_at_s is not None and elapsed_s >= e.destroy_at_s))
                e.apply_condition_schedule(elapsed_s, appearance_free)
                e.step(per_entity_gap)
                # ADR-0044 slice A: "removed" fires exactly once, on the
                # False -> True edge of e.removed, regardless of how many
                # ticks the map keeps saying "removed" afterwards.
                if e.removed and not e._removed_pdu_sent:
                    try:
                        send_pdu(
                            sock,
                            serialize(e.to_remove_entity_pdu(args.exercise_id, args.protocol_version)),
                            args.host, args.port, targets,
                        )
                        LOG.info("entity %d: sent Remove Entity PDU; ESPDUs stop", e.entity_id)
                    except OSError as exc:
                        errors += 1
                        LOG.warning("remove-entity send failed for entity %d: %s", e.entity_id, exc)
                    e._removed_pdu_sent = True
                # "silent" and "removed" both withhold the ESPDU outright --
                # this is the one line that changes shape for the default
                # (no --damage-map) run, and for that run e.silent and
                # e.removed are always False, so the condition is always
                # True and behaviour is unchanged.
                if not (e.silent or e.removed):
                    try:
                        send_pdu(sock, serialize(e.to_pdu(args.exercise_id, args.protocol_version)),
                                 args.host, args.port, targets)
                        sent += 1
                    except OSError as exc:
                        errors += 1
                        LOG.warning("send failed: %s", exc)
                    # Condition claims ride right after the entity's ES, and
                    # only while it is transmitting at all.
                    step = e.condition
                    if step is not None:
                        pdus = []
                        if step["emission"] != "silent":
                            pdus.append(("emission", emission_pdu(
                                e, e.condition_profile, step["emission"],
                                args.exercise_id, args.protocol_version)))
                        if step["datum_health"] is not None:
                            pdus.append(("health datum", health_data_pdu(
                                e, condition_schedule["datum_id"], step["datum_health"],
                                args.exercise_id, args.protocol_version)))
                        for what, pdu in pdus:
                            try:
                                send_pdu(sock, serialize(pdu), args.host, args.port, targets)
                                sent += 1
                            except OSError as exc:
                                errors += 1
                                LOG.warning("%s send failed for entity %d: %s",
                                            what, e.entity_id, exc)
                time.sleep(per_entity_gap)
            if now_overridden != overridden:
                # Log the CHANGE, not the state: a line every tick would bury
                # the one moment an operator cares about.
                added = sorted(now_overridden - overridden)
                removed = sorted(overridden - now_overridden)
                if added:
                    LOG.info("damage override ON for entity id(s): %s", added)
                if removed:
                    LOG.info("damage override CLEARED for entity id(s): %s", removed)
                overridden = now_overridden

            if time.time() - last_report >= 30:
                LOG.info("stats — sent=%d errors=%d", sent, errors)
                last_report = time.time()
    except KeyboardInterrupt:
        LOG.info("stopping — sent=%d errors=%d", sent, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
