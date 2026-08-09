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


class Entity:
    """One emitting entity. Drifts slowly so positions are not static."""

    def __init__(self, index: int, site_id: int, app_id: int, rng: random.Random):
        etype, variant = RECOGNISED_TYPES[index % len(RECOGNISED_TYPES)]
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
        pdu.entityAppearance = 0
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

    try:
        while True:
            for e in entities:
                e.step(per_entity_gap)
                try:
                    sock.sendto(serialize(e.to_pdu(args.exercise_id, args.protocol_version)),
                                (args.host, args.port))
                    sent += 1
                except OSError as exc:
                    errors += 1
                    LOG.warning("send failed: %s", exc)
                time.sleep(per_entity_gap)

            if time.time() - last_report >= 30:
                LOG.info("stats — sent=%d errors=%d", sent, errors)
                last_report = time.time()
    except KeyboardInterrupt:
        LOG.info("stopping — sent=%d errors=%d", sent, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
