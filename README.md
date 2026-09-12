# ARTE+ — erosion fork

Fork of [Rendszerguru/ARTE-QGIS-plugin](https://github.com/Rendszerguru/ARTE-QGIS-plugin)
(ARTE 1.1.0 by Icebird, MIT) adding hydraulic erosion, per-feature road and river
shaping, and previews for both. Upstream behaviour is preserved: every new pass
falls back to the original code if anything fails.

## Why

A 2 km export at 8192 px is 0.26 m/px, but AW3D30 is 30 m/px. About 119 of every
120 pixels are interpolation, so the terrain arrives smooth and featureless. These
additions put plausible detail back and fix defects in the existing shaping.

## Using it

The dialog has two export buttons, so it is always obvious which you are getting:

- **Export (no erosion)** — heightmap, satmap and OSM road/river shaping. This is
  the file you tune erosion against.
- **Export with erosion** — the same, then the erosion pass. Disabled until a
  heightmap exists in the output directory, because erosion runs *on* an exported
  heightmap and has nothing to work from before that. Its tooltip tells you which
  state it is in.

Two **Preview / Tune** buttons let you see what a setting does before committing
to a multi-minute run: one for erosion, one for road and river shaping. The
shaping preview also plots a road cross-section and a river long-profile, because
a 7 m road is two pixels wide on a 2 km map and the defects that matter — a
canted carriageway, a pooling riverbed — are invisible from above.

Long stages report progress ("Shaping Heavy Roads (3/5)…", "Carving rivers
(4/9)…", "Eroding: 6 of 9 tiles done"), and Cancel works throughout. Previously
the window simply stopped responding for tens of seconds, which is
indistinguishable from a crash.

**Let one export finish before starting another.** There is no guard against
concurrent runs, and two erosion passes at once will fight over your CPU cores.

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

### Faster exports

The terrain-engineer stage went from **291 s to 64 s** on a real 8192² export:

| | before | after |
|---|---|---|
| Rivers | 108 s | **7 s** |
| Heavy roads | 46 s | **6 s** |
| Light / medium roads | 46 / 45 s | **10 / 10 s** |

Rivers collapsed because the per-feature path replaces a 13-gaussian raster pass
entirely. Also removed: a sleep-poll after each of the twelve OSM mask
rasterisations that waited on a file already written (up to 6 s each with the GUI
frozen), and morphology that ran on empty water masks (10.6 s of work on an
all-zero array).

Shaping also used to **crash QGIS** at full resolution. It compared every pixel
in a road's bounding box against every point along it, which for a road spanning
an 8192 px export is a 209 GiB allocation. A distance transform does the same job
in one bounded pass.

### NoData border fix

Where the DEM source does not quite cover the area you asked for, GDAL fills the
shortfall with 0. Nothing recorded that those pixels were never real, so a sea
of "0 m terrain" sat against ground at ~2465 m. The 16-bit heightmap then had to
span 2921 m instead of the real 478 m of relief, squeezing every actual feature
into about a sixth of the available precision — and writing a wrong height scale
into the Enfusion import file.

OpenTopography serves 30 m cells and snaps to them, so a request can come back a
fraction of a cell short. Measured bands on a 2200 m export: 9.7 m, 29.3 m and
12.4 m on three sides, all under one source cell.

The export now tells GDAL to mark uncovered pixels instead of silently zeroing
them, and fills them by continuing the nearest real terrain outward — so your
heightmap keeps exactly the extent you asked for. Filling happens before road and
river shaping, so that shaping can no longer blur void pixels into real ground.

Earlier attempts to spot the border by statistics are gone. They cannot work: on
a coastal map the fill value and real sea level are the same number, and one such
attempt flagged 1.67 million pixels of genuine low ground as void.

## Install

Copy this directory to your QGIS plugins folder:

    %APPDATA%/QGIS/QGIS3/profiles/default/python/plugins/ARTE     (QGIS 3)
    %APPDATA%/QGIS/QGIS4/profiles/default/python/plugins/ARTE     (QGIS 4)

Requires numpy and scipy, both bundled with QGIS. Loads under Qt5 and Qt6.
Restart QGIS after copying — it loads plugin code once at startup.

## Notes

- Previews run downsampled and single-threaded, so they show the character of a
  setting rather than its exact result. A 7 m road is sub-pixel at preview scale,
  so it will always look like a thin thread there however wide you set it.
- Erosion is bounded to a band around the input heightmap. Without that it runs
  away: measured relief growing 489 m → 3950 m at 100k particles, because
  thousands of droplets revisit the same pixel and their edits compound.
- Verify any heightmap before importing it. Two defects have cost real time on
  this project: a truncated Gaea write (89% of the file missing, no IEND chunk)
  and the NoData border above.
- `test_installed.py` runs 22 checks against the *installed* copy, so it catches a
  stale or partial install as well as regressions. Run it with QGIS's bundled
  Python, not the system one — only QGIS ships scipy.

## Licence

MIT, as upstream.
