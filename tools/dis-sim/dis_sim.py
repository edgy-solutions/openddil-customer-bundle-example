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
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import socket
import sys
import time
from io import BytesIO

try:
    from opendis.dis7 import EntityStatePdu
    from opendis.DataOutputStream import DataOutputStream
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "opendis is required: pip install opendis==1.0\n"
        "(the same version openddil-sensor-ingest decodes with)\n"
    )
    raise

LOG = logging.getLogger("dis-sim")

# ---------------------------------------------------------------------------
# The recognised set, transcribed from
# openddil-contracts/ontology/dis_entity_types.yaml.
#
# Key order is the DIS 7-tuple:
#   (kind, domain, country, category, subcategory, specific, extra)
#
# If that ontology file gains or loses entries, update this list — a type that
# is not in the ontology is not an error here, it is an invisible asset there.
# ---------------------------------------------------------------------------
RECOGNISED_TYPES: list[tuple[tuple[int, int, int, int, int, int, int], str]] = [
    ((1, 1, 225, 1, 1, 1, 0), "M1A1"),
    ((1, 1, 225, 1, 3, 1, 0), "M1A2-SEPv3"),
    ((1, 1, 225, 2, 1, 1, 0), "M2A3-Bradley"),
    ((1, 1, 225, 3, 1, 1, 0), "HMMWV-M1151A1"),
    ((1, 1, 225, 80, 1, 1, 0), "RCV-M"),
    ((1, 2, 225, 20, 1, 3, 0), "AH-64E-V6"),
    ((1, 2, 225, 21, 1, 2, 0), "UH-60M"),
    ((1, 2, 225, 22, 1, 1, 0), "CH-47F-BlockII"),
    ((1, 2, 225, 40, 1, 5, 0), "F-35A-Block4"),
    ((1, 2, 225, 41, 1, 1, 0), "F-16C-Block50"),
    ((1, 2, 225, 50, 1, 1, 0), "MQ-9A-Block5"),
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
#       {"type": [1, 1, 225, 1, 3, 1, 0], "variant": "M1A2-SEPv3"},
#       {"type": [2, 1, 225,  2, 1, 1, 0], "variant": "120mm-HEAT"}
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
_DAMAGE_MAP: dict[str, str] = {}
_DAMAGE_MAP_MTIME: float | None = None


def _damage_map_key(site: int, app: int, entity: int) -> tuple[str, str]:
    return (f"dis:{site}:{app}:{entity}", str(entity))


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
            if level not in DAMAGE_LEVELS and level != "unspecified":
                LOG.warning("damage map: %r has unknown level %r; skipping "
                            "(valid: %s, unspecified)", k, v,
                            "|".join(DAMAGE_LEVELS))
                continue
            out[str(k).strip()] = level
        _DAMAGE_MAP, _DAMAGE_MAP_MTIME = out, mtime
        LOG.info("damage map reloaded from %s: %d override(s) %s",
                 path, len(out), out)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("damage map %s unreadable (%s); keeping previous %d "
                    "override(s)", path, exc, len(_DAMAGE_MAP))
    return _DAMAGE_MAP


def appearance_bits(domain: int,
                    damage: str = "none",
                    mobility_kill: bool = False,
                    firepower_kill: bool = False,
                    powerplant_on: bool = True,
                    deactivated: bool = False) -> int:
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
    return bits


class Entity:
    """One emitting entity. Drifts slowly so positions are not static."""

    def __init__(self, index: int, site_id: int, app_id: int, rng: random.Random):
        types = ENTITY_TYPES or RECOGNISED_TYPES
        etype, variant = types[index % len(types)]
        self.entity_type = etype
        self.variant = variant
        self.site_id = site_id
        self.app_id = app_id
        self.entity_id = 1000 + index

        stem = CALLSIGN_STEMS[index % len(CALLSIGN_STEMS)]
        # DIS marking is 11 bytes + a charset byte; keep it short and ASCII.
        self.marking = f"{stem[:7]}-{index % 100:02d}"[:11]

        self.lat = BASE_LAT_DEG + rng.uniform(-SPREAD_DEG, SPREAD_DEG)
        self.lon = BASE_LON_DEG + rng.uniform(-SPREAD_DEG, SPREAD_DEG)
        self.alt = 1600.0 + rng.uniform(0, 200)
        self.heading = rng.uniform(0, 2 * math.pi)
        # Air domain (2) moves faster than land (1).
        self.speed_mps = rng.uniform(40, 120) if etype[1] == 2 else rng.uniform(2, 12)
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

    def apply_damage_override(self, damage_map: dict[str, str]) -> bool:
        """Apply a per-asset override from the declarative map, if present.

        Returns True when this entity is under an override, so the caller can
        report which assets are being driven rather than leaving it implicit.

        Precedence is deliberate: a per-asset entry WINS over the fleet-wide
        --damage, because the point of the lever is to make ONE asset differ
        from its fleet. `unspecified` turns emission off for that asset — a
        deliberate silence, and the reason the map has a value that is not a
        damage level at all.
        """
        for key in _damage_map_key(self.site_id, self.app_id, self.entity_id):
            level = (damage_map or {}).get(key)
            if level is None:
                continue
            if level == "unspecified":
                # Say nothing about this asset. NOT the same as "none":
                # none asserts undamaged, this asserts nothing at all.
                self.emit_appearance = False
            else:
                self.damage = level
                self.emit_appearance = True
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
        return False

    def step(self, dt_s: float) -> None:
        # Crude flat-earth step. Adequate: nothing downstream does geodesy on
        # these, and dead-reckoning is not being exercised.
        dm = self.speed_mps * dt_s
        self.lat += (dm * math.cos(self.heading)) / 111_320.0
        self.lon += (dm * math.sin(self.heading)) / (111_320.0 * math.cos(math.radians(self.lat)))
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


def serialize(pdu: EntityStatePdu) -> bytes:
    bio = BytesIO()
    pdu.serialize(DataOutputStream(bio))
    return bio.getvalue()


def main() -> int:
    p = argparse.ArgumentParser(description="DIS EntityState PDU generator")
    p.add_argument("--host", default=os.getenv("DIS_TARGET_HOST", "127.0.0.1"),
                   help="destination host (sensor-ingest)")
    p.add_argument("--port", type=int, default=int(os.getenv("DIS_TARGET_PORT", "62040")))
    p.add_argument("--entities", type=int, default=int(os.getenv("DIS_ENTITIES", "8")))
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
                        "bare entity number; values are a damage level or "
                        "\"unspecified\" to emit NO claim for that asset. A "
                        "per-asset entry overrides --damage. Absent file "
                        "changes nothing.")
    p.add_argument("--damage-fraction", type=float,
                   default=float(os.getenv("DIS_DAMAGE_FRACTION", "1.0")),
                   help="Fraction of entities that carry the --damage level; "
                        "the rest emit appearance with damage none. Only "
                        "meaningful with --damage.")
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

    if args.list_types:
        print("Recognised DIS entity types (from ontology/dis_entity_types.yaml):")
        for t, variant in RECOGNISED_TYPES:
            print("  %-22s %s" % ("_".join(str(v) for v in t), variant))
        print("\nAnything not listed resolves to _default -> UNKNOWN.")
        return 0

    rng = random.Random(args.seed)
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

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

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
    try:
        while True:
            # Re-read the per-asset control once per sweep, not per entity:
            # one stat() per tick, and every entity in a sweep sees the same
            # desired state rather than a file that changed mid-loop.
            dmap = reload_damage_map(args.damage_map)
            now_overridden = set()
            for e in entities:
                if e.apply_damage_override(dmap):
                    now_overridden.add(e.entity_id)
                e.step(per_entity_gap)
                try:
                    sock.sendto(serialize(e.to_pdu(args.exercise_id, args.protocol_version)),
                                (args.host, args.port))
                    sent += 1
                except OSError as exc:
                    errors += 1
                    LOG.warning("send failed: %s", exc)
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
