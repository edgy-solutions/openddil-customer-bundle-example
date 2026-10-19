# edge-assignment.yaml: the site table

One file says where every site is. The same file is read by:
- **asset-registry and the projectors**, which take the assignment keys (`strategy`, `chain`, `fallback`) and decide an asset's edge;
- **every map** (the hub frontend and every tier frontend), which takes the site rows (FOBs and tier sites) and draws them.

No map takes a position from anywhere else.

The chart mounts the file from a ConfigMap named by `edgeAssignment.configMap`. Browsers fetch it at `/edge-assignment/edge-assignment.yaml`.

`edge-assignment.yaml` here is the example deployment's file: fictional sites in the synthetic twin area.

## Site rows

### `fobs`: one row per edge

| Field | Type | Rule |
|---|---|---|
| `edge_id` | string | Non-empty and unique. The edge tier's id (`edge-01`). |
| `region_id` | string | Non-empty. The region tier the edge rolls up into. |
| `lat` | number | Within [-90, 90]. |
| `lon` | number | Within [-180, 180]. |
| `label` | string | Optional. The marker's text. |

FOB rows live in one place: either top-level `fobs:`, or the `fobs:` of the file's single `nearest_fob` step (top-level or inside a `chain`).
- **Top level:** the rows are declared for the maps and assign nothing.
- **In the step:** the same rows also assign assets to the nearest FOB.

Both places at once, or two `nearest_fob` steps, is an error.

### `tier_sites`: where a region or HQ sits (optional)

| Field | Type | Rule |
|---|---|---|
| `tier_id` | string | Non-empty and unique. A region tier's id, or `hq`. Never a FOB's `edge_id`, because an edge sits at its FOB. |
| `lat` | number | Within [-90, 90]. |
| `lon` | number | Within [-180, 180]. |
| `label` | string | Optional. |

Without its own row:
- a region is drawn at the centre of its FOBs;
- HQ is drawn where each map put it before (the HQ map: the centre of the FOBs).

## What each map draws

| Map | FOBs | Highlighted |
|---|---|---|
| HQ | every FOB | none |
| Region | FOBs whose `region_id` is the region's id | none |
| Edge | every FOB in the edge's region | its own FOB |

## Validation

Every map validates the file the same way, with the rules above. One bad row invalidates the whole table: the map draws no sites and reports the row and field in the console. A partly drawn theatre would look complete when it isn't.

A frontend with no file mounted draws no sites. This is the open-core default.

The assignment keys are validated by asset-registry and the projectors at startup. `strategy` is required; use `chain` with `chain: []` to assign nothing by position.
