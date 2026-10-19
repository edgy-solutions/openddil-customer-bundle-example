// Synthetic "fictional twin" terrain for the example bundle: Terrarium elevation
// tiles plus a water.json, over the example sim's own synthetic training area.
// usage: node make-twin.mjs <out-src-dir> [--check]
//   writes <out>/dem/{z}/{x}/{y}.png and <out>/water.json; pack with openddil-demo
//   frontend/scripts/pack-terrain.mjs. Deterministic: same bytes on every run.
// Node >= 20, no dependencies. The terrain is invented; it is not a real place.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

// Centre of the sim's training area: BASE_LAT_DEG / BASE_LON_DEG in tools/dis-sim/dis_sim.py.
const CLAT = 39.0, CLON = -105.0;
const CORE = { w: -105.75, s: 38.4, e: -104.25, n: 39.6 };
const SHRUNK = { w: -105.5, s: 38.6, e: -104.5, n: 39.4 }; // z12 fallback if over budget
const LAKE_C = { lat: 38.85, lon: -104.85 };
const LAKE_SURFACE = 1700;
const SEED = 20260101;
const SIZE = 256;
const BUDGET = 12 * 1024 * 1024;

// ---- elevation ------------------------------------------------------------
function hash2(ix, iy, seed) {
  let h = Math.imul(ix, 0x27d4eb2d) ^ Math.imul(iy, 0x165667b1) ^ Math.imul(seed, 0x9e3779b1);
  h = Math.imul(h ^ (h >>> 15), 0x85ebca6b);
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}
const fade = (t) => t * t * (3 - 2 * t);
function vnoise(x, y, seed) {
  const x0 = Math.floor(x), y0 = Math.floor(y), fx = fade(x - x0), fy = fade(y - y0);
  const a = hash2(x0, y0, seed), b = hash2(x0 + 1, y0, seed), c = hash2(x0, y0 + 1, seed), d = hash2(x0 + 1, y0 + 1, seed);
  return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy;
}
function fbm(lat, lon) {
  let amp = 1, freq = 1 / 0.6, sum = 0, norm = 0;
  for (let o = 0; o < 6; o++) {
    sum += amp * vnoise(lon * freq + 31.7 * o, lat * freq - 17.3 * o, SEED + o);
    norm += amp; amp *= 0.5; freq *= 2;
  }
  return sum / norm; // 0..1, centred near 0.5
}
const gauss = (d2, s2) => Math.exp(-d2 / (2 * s2));

/** Lake outline: noisy ellipse (~8 km x 5 km), 64 vertices, closed [lon, lat] ring. */
function lakeRing() {
  const n = 64, ring = [];
  const kmLat = 110.57, kmLon = 111.32 * Math.cos((LAKE_C.lat * Math.PI) / 180);
  for (let i = 0; i < n; i++) {
    const t = (i / n) * 2 * Math.PI;
    const r = 1 + 0.18 * (vnoise(Math.cos(t) * 2 + 5, Math.sin(t) * 2 + 5, SEED + 99) - 0.5) * 2 + 0.07 * Math.sin(3 * t + 1);
    const lon = LAKE_C.lon + (4.0 * r * Math.cos(t)) / kmLon, lat = LAKE_C.lat + (2.5 * r * Math.sin(t)) / kmLat;
    ring.push([Math.round(lon * 1e5) / 1e5, Math.round(lat * 1e5) / 1e5]);
  }
  ring.push(ring[0].slice());
  return ring;
}
const LAKE = lakeRing();
function inRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Elevation in metres at (lat, lon): the same value at every zoom. */
export function elevation(lat, lon) {
  let e = 1600 + 1800 * Math.min(1, Math.max(0, (fbm(lat, lon) - 0.25) / 0.5));
  // Broad ridge running NW-SE through the centre, and a basin holding the lake.
  const d = (lat - CLAT) * 0.6 + (lon - CLON) * 0.8; // distance across the ridge line, degrees
  e += 350 * gauss(d * d, 0.35 * 0.35);
  const bl = lat - LAKE_C.lat, bo = lon - LAKE_C.lon, b2 = bl * bl + bo * bo;
  e -= 700 * gauss(b2, 0.45 * 0.45);
  if (inRing(lon, lat, LAKE)) return LAKE_SURFACE;
  // Shore sits a little above the lake surface; the basin floor never dips below it.
  return Math.max(e, 1712 + 40 * (1 - gauss(b2, 0.2 * 0.2)));
}

// ---- web mercator -----------------------------------------------------------
const lonToTileX = (lon, z) => ((lon + 180) / 360) * 2 ** z;
const latToTileY = (lat, z) => {
  const r = (lat * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z;
};
const tileXToLon = (x, z) => (x / 2 ** z) * 360 - 180;
const tileYToLat = (y, z) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / 2 ** z))) * 180) / Math.PI;

// ---- PNG ----------------------------------------------------------------------
const CRC = new Uint32Array(256);
for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; CRC[n] = c >>> 0; }
function crc32(buf) { let c = 0xffffffff; for (const b of buf) c = CRC[(c ^ b) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
function chunk(type, data) {
  const out = Buffer.alloc(12 + data.length);
  out.writeUInt32BE(data.length, 0); out.write(type, 4, 'ascii'); data.copy(out, 8);
  out.writeUInt32BE(crc32(out.subarray(4, 8 + data.length)), 8 + data.length);
  return out;
}
function encodePng(rgb) { // rgb: SIZE*SIZE*3 bytes; filter Up on every row
  const stride = SIZE * 3, raw = Buffer.alloc((stride + 1) * SIZE);
  for (let y = 0; y < SIZE; y++) {
    raw[y * (stride + 1)] = 2;
    for (let i = 0; i < stride; i++) raw[y * (stride + 1) + 1 + i] = (rgb[y * stride + i] - (y ? rgb[(y - 1) * stride + i] : 0)) & 255;
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(SIZE, 0); ihdr.writeUInt32BE(SIZE, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw, { level: 9 })), chunk('IEND', Buffer.alloc(0))]);
}
function decodePng(buf) { // RGB8, all five filters, CRC verified
  let off = 8; const idat = [];
  while (off < buf.length) {
    const len = buf.readUInt32BE(off), type = buf.toString('ascii', off + 4, off + 8), data = buf.subarray(off + 8, off + 8 + len);
    if (crc32(buf.subarray(off + 4, off + 8 + len)) !== buf.readUInt32BE(off + 8 + len)) throw new Error(`bad CRC in ${type}`);
    if (type === 'IDAT') idat.push(data);
    off += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat)), stride = SIZE * 3, px = Buffer.alloc(stride * SIZE);
  for (let y = 0; y < SIZE; y++) {
    const f = raw[y * (stride + 1)];
    for (let i = 0; i < stride; i++) {
      const v = raw[y * (stride + 1) + 1 + i], a = i >= 3 ? px[y * stride + i - 3] : 0, b = y ? px[(y - 1) * stride + i] : 0, c = i >= 3 && y ? px[(y - 1) * stride + i - 3] : 0;
      let p = 0;
      if (f === 1) p = a; else if (f === 2) p = b; else if (f === 3) p = (a + b) >> 1;
      else if (f === 4) { const q = a + b - c, pa = Math.abs(q - a), pb = Math.abs(q - b), pc = Math.abs(q - c); p = pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
      px[y * stride + i] = (v + p) & 255;
    }
  }
  return px;
}

// ---- tiles ----------------------------------------------------------------------
function tileRange(box, z) {
  return { x0: Math.floor(lonToTileX(box.w, z)), x1: Math.floor(lonToTileX(box.e, z)), y0: Math.floor(latToTileY(box.n, z)), y1: Math.floor(latToTileY(box.s, z)) };
}
function renderTile(z, x, y) {
  const rgb = Buffer.alloc(SIZE * SIZE * 3);
  for (let py = 0; py < SIZE; py++) {
    const lat = tileYToLat(y + (py + 0.5) / SIZE, z);
    for (let px = 0; px < SIZE; px++) {
      const v = Math.round(elevation(lat, tileXToLon(x + (px + 0.5) / SIZE, z))) + 32768, o = (py * SIZE + px) * 3;
      rgb[o] = Math.floor(v / 256); rgb[o + 1] = v % 256; rgb[o + 2] = 0;
    }
  }
  return encodePng(rgb);
}
function plan(core12) {
  const k = 8 / Math.cos((39 * Math.PI) / 180);
  const list = [[7, { w: CLON - k, e: CLON + k, s: CLAT - 8, n: CLAT + 8 }]];
  for (let z = 8; z <= 12; z++) list.push([z, z === 12 ? core12 : CORE]);
  return list;
}
function writeAll(out, core12) {
  fs.rmSync(path.join(out, 'dem'), { recursive: true, force: true });
  const counts = {}; let bytes = 0;
  for (const [z, box] of plan(core12)) {
    const r = tileRange(box, z); counts[z] = 0;
    for (let x = r.x0; x <= r.x1; x++) for (let y = r.y0; y <= r.y1; y++) {
      const png = renderTile(z, x, y), p = path.join(out, 'dem', String(z), String(x), `${y}.png`);
      fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, png);
      counts[z]++; bytes += png.length;
    }
  }
  return { counts, bytes };
}

function main() {
  const args = process.argv.slice(2), check = args.includes('--check');
  const out = args.find((a) => !a.startsWith('--'));
  if (!out) { console.error('usage: node make-twin.mjs <out-src-dir> [--check]'); process.exit(2); }
  let core12 = CORE, res = writeAll(out, core12);
  if (res.bytes > BUDGET) { core12 = SHRUNK; res = writeAll(out, core12); console.log('over budget: z12 shrunk to lat 38.6..39.4, lon -105.5..-104.5'); }
  const water = {
    type: 'FeatureCollection', bbox: [CORE.w, CORE.s, CORE.e, CORE.n],
    license: 'CC0-1.0', attribution: 'synthetic (OpenDDIL example twin)', source: 'tools/terrain-twin/make-twin.mjs',
    features: [{ type: 'Feature', properties: { name: 'Example Lake' }, geometry: { type: 'Polygon', coordinates: [LAKE] } }],
  };
  fs.writeFileSync(path.join(out, 'water.json'), JSON.stringify(water));
  console.log(`tiles per zoom: ${JSON.stringify(res.counts)}; total ${Object.values(res.counts).reduce((a, b) => a + b)}; dem ${(res.bytes / 1048576).toFixed(2)} MB`);
  if (check) {
    const r7 = tileRange(plan(core12)[0][1], 7);
    const picks = [[7, r7.x0, r7.y0], [10, Math.floor(lonToTileX(LAKE_C.lon, 10)), Math.floor(latToTileY(LAKE_C.lat, 10))], [12, Math.floor(lonToTileX(CLON, 12)), Math.floor(latToTileY(CLAT, 12))]];
    let worst = 0, n = 0;
    for (const [z, x, y] of picks) {
      const px = decodePng(fs.readFileSync(path.join(out, 'dem', String(z), String(x), `${y}.png`)));
      for (const [i, j] of [[0, 0], [17, 200], [128, 128], [255, 255], [90, 33]]) {
        const o = (j * SIZE + i) * 3, got = px[o] * 256 + px[o + 1] + px[o + 2] / 256 - 32768;
        const want = elevation(tileYToLat(y + (j + 0.5) / SIZE, z), tileXToLon(x + (i + 0.5) / SIZE, z));
        worst = Math.max(worst, Math.abs(got - want)); n++;
      }
    }
    console.log(`check: ${n} pixels, worst error ${worst.toFixed(3)} m ${worst <= 1 ? 'OK' : 'FAIL'}`);
    if (worst > 1) process.exit(1);
  }
}
main();
