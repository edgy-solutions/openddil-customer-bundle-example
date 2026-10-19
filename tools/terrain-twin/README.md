# terrain-twin — synthetic terrain for the example bundle

The public twin of the overlay `terrain` class (see
[`../../overlay-manifest.yaml`](../../overlay-manifest.yaml)). It packages a
**fictional** area: synthetic elevation and one synthetic lake over the
example sim's own training area (centred on `BASE_LAT_DEG` / `BASE_LON_DEG`
in [`../dis-sim/dis_sim.py`](../dis-sim/dis_sim.py), 39.0 N, 105.0 W). The
terrain is invented, not sampled from a real place.

Why: a real overlay's terrain is location, and location stays private. This
twin lets the overlay mechanism, the packer and the frontend's basemap relief
be exercised end to end without any real geography.

## Regenerate

From the repo root (Node >= 20, no dependencies; needs `../openddil-demo`
checked out alongside):

```
node tools/terrain-twin/make-twin.mjs <tmp-src> [--check]
node ../openddil-demo/frontend/scripts/pack-terrain.mjs <tmp-src> deployment-overlay
```

`<tmp-src>` is a scratch directory outside the repo. The generator is fully
deterministic (fixed seed), so the same run always yields the same content
hash. `--check` decodes a few pixels back from the written PNGs and compares
them with the elevation function (within 1 m).
After packing, `deployment-overlay/deployment.json` carries the `terrain`
URLs plus the `credits` lines for elevation and water.

The tiles are not committed: `dem/` is build output and git-ignored. Only the
generator, `deployment.json` and `water.json` live in the repo. Run the two
commands above to build the tiles. The pack must report hash
`103ba1d3b58f44ea`, the one `deployment.json` names; any other hash means
the generator changed, and the new `deployment.json` is committed with it.
Without the tiles the maps fall back to flat ground and the credit reads
"Terrain data unavailable".

## What it contains

- `dem/{z}/{x}/{y}.png`: Terrarium elevation, 256x256, integer metres
  (`r*256 + g + b/256 - 32768`).
  - z8 to z12 over the core box, lat 38.4 to 39.6, lon -105.75 to -104.25.
  - z7 over a wider surround, 8 degrees of latitude either side of the centre.
- `water.json`: one invented lake (about 8 km x 5 km, near 38.85 N, 104.85 W),
  in the same GeoJSON shape the water-clip tool writes. Inside it the DEM is
  flat at the lake surface.

Elevation is a function of (lat, lon) only, so tiles agree across zooms:
seeded fractal noise (roughly 1600 to 3400 m), one broad ridge, and a basin
holding the lake.

## How the frontend uses it

The frontend serves the packaged directory at
`/deployment-overlay/terrain/<hash>/`, with the hashed URLs read from
`deployment.json`. The basemap shows relief from it by default; the water
layer feeds the coastline and lake mask.

## Licence

The generated data is synthetic and released under CC0-1.0.
