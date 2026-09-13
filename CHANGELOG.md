# Changelog

All notable changes to this fork. Versions follow the upstream plugin's
numbering; 1.1.0 is the last upstream release this fork was taken from.

## [1.2.2] — 2026-09-13

### Fixed

- **Roads were flattened about four times too wide.** `BUFF_*` are upstream's
  buffer distances — full corridor widths — but they were passed straight in as
  `half_width_m`, so a Heavy road came out 27.6 m across (39.6 m with the blend)
  where a real primary road is about 7 m. Measured on a test slope: modified
  band 14 px → 6 px.
- **Per-road widths from OSM were discarded.** ARTE already derives a real width
  per way from the `width` and `lanes` tags and stores it as `arte_dyn_width`
  — 7.0 m for the Bamyan primaries, 4.0 m for the tertiary. Upstream's raster
  path honours it; the per-feature shaping never read it and used one constant
  per class. Each road is now shaped at its own width: 7 m → 6 px, 4 m → 4 px,
  12 m → 12 px.
- **Two dead-straight segments meeting at an elbow in the preview.** Preview-only
  — the export converts geometry through the raster's own GeoTransform and was
  never affected. Overpass returns whole ways whenever any part intersects the
  query box, so 65% of vertices lie outside a Bamiyan export. Those were clamped
  to the raster border rather than clipped, pinning long runs of points onto the
  edge and stamping straight ribbons along it; a way leaving one edge and
  re-entering another drew a false chord across the map. Polylines are now split
  at the boundary instead: 7 edge-ribbon lines → 0, with all 35 source ways
  preserved. The preview also now prefers the heightmap's own georeferencing for
  its query box where the file carries any, and ignores non-way elements.

## [1.2.1] — 2026-09-13

### Fixed

- **The plugin now loads.** 1.2.0 shipped without `__init__.py`, the module QGIS
  calls `classFactory()` on to start a plugin. It would install and then simply
  not appear in the plugin list. The file had been removed from the repo by an
  over-broad cleanup pattern; the pattern now explicitly excludes it.
- **Erosion tuning no longer compounds on itself.** Both preview dialogs pick the
  newest heightmap in the output directory, and nothing on disk distinguished an
  eroded export from a raw one — so a second tuning round loaded the previous
  *eroded* result and eroded it again. Measured on a real pair of exports, the
  second file was 50% rougher (mean |dz| 64.0 against 42.7) and 2.6× larger.
  Eroded exports are now written as `heightmap_<timestamp>_eroded.png` and the
  previews skip them, so erosion is always tuned against the raw DEM.

### Changed

- `metadata.txt` now points at this fork for homepage, repository and tracker
  rather than upstream, and drops the `-fork` version suffix that QGIS's version
  comparison does not parse reliably.
- Added `plugins.xml`, the self-update feed the plugin registers with QGIS.
  Upstream's points at upstream's releases, so the fork needs its own.

## [1.2.0] — 2026-09-12

First release of the fork. Everything below is new relative to upstream 1.1.0.
Figures are measured on a real 8192×8192 export.

### Added

- **Hydraulic erosion** (`erosion.py`) — droplet simulation, a vectorised numpy
  port of the method used by [erodr](https://github.com/henrikglass/erodr) (MIT).
  Ported rather than shelled out to because QGIS ships no C toolchain. Presets
  scale particle count per megapixel so a preset means the same at any export
  resolution. Tiled across processes: **5.1× on 11 workers**, roughly 4 minutes
  for a full 8192² pass. Roads and riverbeds already shaped by Terrain
  Engineering are protected from it.
- **Per-feature road and river shaping** (`roadwater.py`) — each feature is
  shaped along its own length: sample a profile down the polyline, smooth and
  grade-cap it, then write it back across the corridor at each pixel's
  perpendicular foot.
- **Two preview dialogs** — one for erosion, one for shaping, each with a
  draggable before/after wipe over a hillshade. The shaping preview also plots a
  road cross-section and a river long-profile, because a 7 m road is two pixels
  wide on a 2 km map and the defects that matter are invisible from above.
- **Two explicit export buttons** — *Export (no erosion)* and *Export with
  erosion* — so it is never ambiguous which you are getting. The erosion button
  stays disabled until a heightmap exists to erode.
- **Progress reporting** on the long stages, which stay cancellable. Previously
  the window stopped responding for tens of seconds at a time, which is
  indistinguishable from a crash.
- `test_installed.py` — 22 checks against the installed copy, so it catches a
  stale or partial install as well as regressions.

### Fixed

- **Roads came out canted.** Upstream flattens them with a 2-D gaussian over the
  raster, which does not know where the road goes and mixes in terrain from both
  sides. Cant across the carriageway: **2.345 m → 0.024 m**.
- **Carved rivers pooled.** They now get a monotonically descending bed, bounded
  so it cannot trench through high ground — an unbounded running minimum
  flattened 68% of a test profile and cut 160 m deep, producing a canal. Steps
  running against flow: **422 → 112**.
- **NoData border.** Where the DEM source did not quite cover the requested area,
  GDAL filled the shortfall with 0 and nothing recorded that those pixels were
  never real. A sea of 0 m terrain then sat against ground at ~2465 m, so the
  16-bit heightmap spanned 2921 m instead of the real 478 m of relief —
  squeezing every actual feature into a sixth of the available precision, and
  writing a wrong height scale into the Enfusion import file. Uncovered pixels
  are now marked at the warp (`dstNodata`) and filled by continuing the nearest
  real terrain outward, before shaping runs.
- **Shaping crashed QGIS at full resolution.** It compared every pixel in a
  road's bounding box against every point along the road — a 209 GiB allocation
  for a road spanning an 8192 px export. A distance transform does the same work
  in one bounded pass.
- **Erosion ran away on large maps.** Thousands of droplets revisit the same
  pixel and their edits compound; relief grew from 489 m to 3950 m at 100k
  particles. Now bounded to a band around the input heightmap.

### Performance

Terrain Engineering: **291 s → 64 s**.

| Stage | Before | After |
|---|---|---|
| Rivers | 108 s | 7 s |
| Heavy roads | 46 s | 6 s |
| Light / medium roads | 46 / 45 s | 10 / 10 s |

Also removed a sleep-poll after each of the twelve OSM mask rasterisations that
waited on a file `processing.run` had already written (up to 6 s each, GUI
frozen), and morphology that ran on empty water masks (10.6 s on an all-zero
array).

---

[1.2.2]: https://github.com/gruppe-adler/ARTE-QGIS-plugin/releases/tag/1.2.2
[1.2.1]: https://github.com/gruppe-adler/ARTE-QGIS-plugin/releases/tag/1.2.1
[1.2.0]: https://github.com/gruppe-adler/ARTE-QGIS-plugin/releases/tag/1.2.0
