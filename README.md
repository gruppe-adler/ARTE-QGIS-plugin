# ARTE+ — erosion fork

Fork of [Rendszerguru/ARTE-QGIS-plugin](https://github.com/Rendszerguru/ARTE-QGIS-plugin)
(ARTE 1.1.0 by Icebird, MIT) adding hydraulic erosion, per-feature road and river
shaping, and previews for both. Upstream behaviour is preserved: every new pass
falls back to the original code if anything fails.

## Why

A 2 km export at 8192 px is 0.26 m/px, but AW3D30 is 30 m/px. About 119 of every
120 pixels are interpolation, so the terrain arrives smooth and featureless. These
additions put plausible detail back and fix defects in the existing shaping.

## What is new

### Hydraulic erosion (`erosion.py`)

Vectorised numpy port of the droplet method from Beyer's thesis, the algorithm
used by [erodr](https://github.com/henrikglass/erodr) (MIT). Ported rather than
shelled out to because QGIS ships no C toolchain.

- **Presets** subtle / moderate / strong, scaled per megapixel so a preset means
  the same at any export resolution.
- **Preview** with a draggable before/after wipe over a hillshade.
- **Parallel**: tiles across processes, measured **5.1x on 11 workers**
  (2048², 75 s → 14.7 s). A full 8192² export is about 4 minutes.
- **Protects** roads and riverbeds already shaped by the terrain engineer.

### Road and river shaping (`roadwater.py`)

Upstream flattens roads by taking each pixel's nearest centreline elevation and
running a 2-D gaussian over the raster. A gaussian does not know where the road
goes, so it mixes in terrain from either side.

This shapes each feature along its own length: sample a profile down the polyline,
smooth and grade-cap it, then interpolate back across the corridor at each pixel's
perpendicular foot.

| | upstream | this fork |
|---|---|---|
| Cant across carriageway (Bamiyan) | 2.345 m | **0.024 m** |
| Cant across carriageway (synthetic 25% slope) | 0.774 m | **0.053 m** |
| River steps running against flow | 422 | **112** |

Rivers get a monotonically descending bed, bounded so it cannot trench through
high ground — an unbounded running minimum flattened 68% of a test profile and
cut 160 m deep, producing a canal rather than a river.

### NoData fix

Upstream fills voids with `min_val`, the lowest point on the whole map. Where the
DEM does not reach the export window — a tile boundary along one edge is the
common case — that is a cliff the full height of the terrain wrapping the border.
In the Enfusion editor it reads as a giant bowl and drags the interior out of
shape through LOD blending. Now fills by nearest-valid replication.

## Install

Copy this directory to your QGIS plugins folder:

    %APPDATA%/QGIS/QGIS3/profiles/default/python/plugins/ARTE     (QGIS 3)
    %APPDATA%/QGIS/QGIS4/profiles/default/python/plugins/ARTE     (QGIS 4)

Requires numpy and scipy, both bundled with QGIS. Loads under Qt5 and Qt6.

## Notes

- Previews run downsampled and single-threaded, so they show the character of a
  setting rather than its exact result.
- Erosion is bounded to a band around the input heightmap. Without that it runs
  away: measured relief growing 489 m → 3950 m at 100k particles, because
  thousands of droplets revisit the same pixel and their edits compound.

## Licence

MIT, as upstream.
