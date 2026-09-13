# ARTE+ - Arma Reforger Terrain Exporter (QGIS Plugin)

[![Version](https://img.shields.io/badge/version-1.1.0-blue.svg)](https://github.com/Rendszerguru/ARTE-QGIS-plugin/releases/latest)
[![QGIS](https://img.shields.io/badge/QGIS-3.x-green.svg)](https://qgis.org/)
[![Engine](https://img.shields.io/badge/Engine-Enfusion%20(Arma%20Reforger)-orange.svg)](https://reforger.armaplatform.com/)
[![License](https://img.shields.io/badge/license-Free-lightgrey.svg)](#-license)

> **Fork of [Rendszerguru/ARTE-QGIS-plugin](https://github.com/Rendszerguru/ARTE-QGIS-plugin)** by Icebird (MIT).
> Adds hydraulic erosion, per-feature road and river shaping, live previews, and a NoData fix - see [**What this fork adds**](#-what-this-fork-adds).
> Everything below is upstream's and still applies; each new pass falls back to the original code if it fails.

**ARTE** is an advanced QGIS plugin designed for Arma Reforger terrain creators. It provides a streamlined workflow to extract high-resolution satellite imagery, process digital elevation models (DEM), apply OSM-based **Terrain Engineering**, and **automatically generate Enfusion-ready import parameters**.

<img width="751" height="768" alt="arte" src="https://github.com/user-attachments/assets/d270fdb4-6b5d-4630-a473-c4f5f08c0f96" />

## ✨ Key Features
* **Interactive Map Selection:** Visually draw, move, and resize your terrain boundaries directly on the QGIS map canvas with aspect-ratio locking.
* **Flexible & Expandable Elevation Sources:**
    * **Custom Source Manager (New ✨):** Add, edit, and remove your own terrain data sources and custom download links directly through the plugin interface.
    * **AWS Terrarium:** 30m global resolution database, **no API key required**.
    * **Mapbox Terrain-RGB:** Ideal for high-fidelity global elevation data, **requires Mapbox API key**.
    * **OpenTopography Datasets:** Gives access to premium LiDAR, COP30, AW3D30, and EU_DTM data, **requires OpenTopography API key**. *OpenTopography COP30 is highly recommended as the most optimal option.*
    * *Note: Both Mapbox and OpenTopography require a free API key to access their servers. You can easily generate your personal tokens by creating a free account on their official websites and pasting them directly into the plugin interface.*
* **OSM Terrain Engineering:** Automatically flattens heightmaps under roads/railways and smooths riverbeds using real-time OpenStreetMap data.
* **Enfusion-Ready Export:** Automatically calculates the exact `Grid cell size` and `Height scale` parameters required for the Arma Reforger Workbench.
* **Flexible Formats:** Export heightmaps as 16-bit PNG, Esri ASCII Grid (.asc), or raw Float32 GeoTIFF.

## 🛠️ OSM Terrain Engineering
When enabled, the plugin uses OpenStreetMap vector data to guide terrain modification of raw DEM data for Enfusion workflow preparation:

* **Smart OSM Filtering:** Fetches data from Overpass API (with fallback endpoints) and filters roads, rails, and water features, excluding bridges and tunnels.
* **Dynamic Road & Lane Widths:** Parses OSM tags (where available) such as width and lanes to estimate corridor widths for buffering and rasterization.
* **Pixel-Aware Embankments:** Uses buffer scaling and distance-based falloff that adapts to raster resolution (pixel size) to avoid overly sharp or blocky transitions.
* **Dual-Mask Water Processing:** Combines water line and polygon data into raster masks and applies smoothing and morphological cleanup to improve coastline continuity and reduce artifacts.
* **Riverbed Shaping:** Modifies terrain under water features using DEM-derived slope information combined with smoothing and distance falloff, applying a simplified depth offset for game-ready river profiles rather than true hydrological modeling.
* **Built-in Flood Protection:** Ensures roads and railways are not lowered below nearby water levels by applying elevation constraints during terrain modification.
* **Visual Audit Logs:** Outputs a real-time `engineer_debug_[timestamp].txt` log and a `heightmap_diff_[timestamp].tif` file showing terrain modifications for debugging and QA.

### 📊 Terrain Engineering Comparison
![Terrain Engineering Comparison](https://github.com/user-attachments/assets/93152eba-9bb0-40a6-87fb-57f6740777dd)

## 🚀 Installation
1. Download `ARTE-QGIS-plugin.zip` from [Releases](https://github.com/Rendszerguru/ARTE-QGIS-plugin/releases/latest).
2. Open QGIS and navigate to **Plugins** -> **Manage and Install Plugins...**
3. Select the **Install from ZIP** tab, browse for the `.zip` file and click **Install Plugin**.
4. *Note: ARTE automatically registers its own update repository for seamless future updates.*

## 🛠️ Quick Usage Guide
1. Click the **ARTE icon** in the toolbar or find it under the `Arma Tools (ARTE)` menu.
2. Click **1. Load Satellite Preview** to center the map.
3. Click **2. Select Extent on Map**, drag the red bounding box, and press `ENTER` or `Right-Click` when finished.
4. *(Optional)* Use the **Source Manager** to set up custom terrain data providers.
5. Set resolutions, select your Elevation Data Source, and click **OK**.
6. Open `enfusion_import.txt` and copy the calculated scale values directly into your Arma Reforger World Editor!

## ➕ What this fork adds

A 2 km export at 8192 px is 0.26 m/px, but AW3D30 is 30 m/px — about 119 of every 120 pixels are interpolation, so terrain arrives smooth and featureless. These additions put plausible detail back and fix defects in the existing shaping. All figures measured on a real 8192² export.

### 🌊 Hydraulic Erosion
* **Droplet simulation** (numpy port of [erodr](https://github.com/henrikglass/erodr), MIT) carving drainage channels and gullies the source DEM cannot resolve.
* **Presets** subtle / moderate / strong, scaled per megapixel so a preset means the same at any resolution.
* **Parallel:** tiled across CPU cores, **5.1x on 11 workers**. A full 8192² pass is ~4 minutes.
* **Protects** roads and riverbeds already shaped by Terrain Engineering.

### 🛰️ Satellite Micro-Relief *(optional, off by default)*
The satmap has already been downloaded, and on bare terrain most of its fine brightness is not colour — it is sun shading, which is a direct function of slope. This pass reads that shading back out and turns it into terrain, recovering relief that is genuinely on the ground but far below what a 30 m DEM can record.

This is **additional to erosion, not a replacement.** Erosion invents plausible drainage where there is no evidence; this measures where a gully actually is. On a real export the two outputs correlate at **0.017** — near zero — so they are not two guesses at the same thing and do not double-count.

It only runs when the imagery supports it. Before doing anything, the plugin correlates the DEM's own hillshade against the satmap and sweeps for the sun angle, which gives a **fit score**:

| Imagery | fit | result |
|---|---|---|
| Real terrain | **0.288** | runs |
| Smooth albedo blobs | 0.036 | refuses |
| Uniform snow, random noise | 0.003 | refuses |

Below 0.15 the heightmap is returned untouched. The preview shows you the score and its verdict, so you can tell whether your satmap is one this helps. The effect is confined to detail finer than the source DEM's cell size — it cannot disturb real landform — and is clamped to ±3 m by default. Roads and riverbeds are protected.

*Caveats:* buildings and trees read as terrain, clouds become faint hills, and a mosaic seam can put the sun in the wrong place for part of the map. All are bounded by the clamp rather than solved — this is relief, not ground truth.

### 🛣️ Better Road & River Shaping
Upstream flattens roads with a 2-D gaussian over the raster, which does not know where the road goes and mixes in terrain from both sides. This fork shapes each feature along its own length — sample a profile down the polyline, smooth and grade-cap it, then write it back across the corridor.

| | upstream | this fork |
|---|---|---|
| Cant across carriageway | 2.345 m | **0.024 m** |
| River steps running against flow | 422 | **112** |

Rivers get a monotonically descending bed, bounded so it cannot trench through high ground.

### 🔍 Live Previews
Three **Preview / Tune** dialogs show what a setting does before committing to a multi-minute run — one for erosion, one for shaping, one for satellite micro-relief. The shaping preview plots a road cross-section and river long-profile, because a 7 m road is two pixels wide on a 2 km map and a canted carriageway or pooling riverbed is invisible from above.

### ⚡ Faster Exports
Terrain Engineering went from **291 s to 64 s** (rivers 108 s → 7 s, heavy roads 46 s → 6 s). Shaping also used to **crash QGIS** at full resolution — it compared every pixel in a road's bounding box against every point along it, a 209 GiB allocation for a road spanning the map. A distance transform does the same job in one bounded pass.

### 🧹 NoData Border Fix
Where the DEM source does not quite cover the requested area, GDAL fills the shortfall with 0 and nothing records that those pixels were never real. A "sea" of 0 m terrain then sat against ground at ~2465 m, so the 16-bit heightmap had to span 2921 m instead of the real 478 m of relief — squeezing every actual feature into a sixth of the available precision, and writing a wrong height scale into the import file. Uncovered pixels are now marked and filled by continuing the nearest real terrain outward, before road and river shaping runs.

### 🎛️ Clearer Export Flow
* Two buttons — **Export (no erosion)** and **Export with erosion** — so it is never ambiguous which you are getting. The erosion button stays disabled until a heightmap exists to erode.
* Long stages report progress and stay cancellable, instead of the window freezing for tens of seconds.
* *Let one export finish before starting another — two erosion passes will fight over your CPU cores.*

---

### 📝 **Changelog**
See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

### 📄 **License**
This project is licensed under the MIT License - free to use, modify, and distribute.

### Author 🧑‍💻
Created by **Icebird** - Copyright (c) 2026.
Fork additions by [gruppe-adler](https://github.com/gruppe-adler).
