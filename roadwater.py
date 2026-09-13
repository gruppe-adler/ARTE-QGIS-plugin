# -*- coding: utf-8 -*-
"""
Road and waterway shaping.

Replaces the ribbon/geomorph passes in TerrainEngineer with versions that work
along each feature's length rather than over the whole raster.

The problem with filtering the raster: the upstream pass takes each pixel's
nearest centreline elevation and then runs a 2-D gaussian over the result. A
gaussian does not know where the road goes, so it mixes in terrain from either
side of it. Measured on a 25% hillside with a diagonal road, that leaves up to
1.15 m of cross-slope across a 6 px carriageway -- the surface is canted, and a
vehicle drives along the camber instead of across a flat road.

Working per feature fixes it: walk each polyline, take a profile of the terrain
along it, smooth or monotonically constrain *that* 1-D profile, then assign the
result across the full width. Every pixel at a given station gets one elevation,
so cross-slope is zero by construction.
"""

import numpy as np

try:
    from scipy.ndimage import distance_transform_edt, gaussian_filter, gaussian_filter1d
except ImportError:  # pragma: no cover - scipy ships with QGIS
    distance_transform_edt = None


# ---------------------------------------------------------------------------
# Profiles along a polyline
# ---------------------------------------------------------------------------

def sample_profile(z, pts):
    """Bilinear elevation sample at each (row, col) float coordinate."""
    h, w = z.shape
    r = np.clip(pts[:, 0], 0, h - 1.001)
    c = np.clip(pts[:, 1], 0, w - 1.001)
    r0 = r.astype(np.int32)
    c0 = c.astype(np.int32)
    fr = r - r0
    fc = c - c0
    r1 = np.minimum(r0 + 1, h - 1)
    c1 = np.minimum(c0 + 1, w - 1)
    return (z[r0, c0] * (1 - fr) * (1 - fc) + z[r0, c1] * (1 - fr) * fc +
            z[r1, c0] * fr * (1 - fc) + z[r1, c1] * fr * fc)


def densify(pts, step=1.0):
    """Resample a polyline to roughly `step` pixels between vertices."""
    if len(pts) < 2:
        return pts
    seg = np.hypot(*(np.diff(pts, axis=0).T))
    total = float(seg.sum())
    if total <= 0:
        return pts
    n = max(2, int(total / max(step, 1e-6)) + 1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    target = np.linspace(0.0, total, n)
    out = np.empty((n, 2), np.float64)
    out[:, 0] = np.interp(target, cum, pts[:, 0])
    out[:, 1] = np.interp(target, cum, pts[:, 1])
    return out


def smooth_profile(prof, pixel_size, smooth_m=60.0, max_grade=None):
    """Smooth a 1-D elevation profile, optionally capping its gradient.

    `max_grade` is a rise/run limit (0.08 = 8%). Roads are built to a grade;
    draping one over raw DEM noise gives a surface that climbs and drops every
    few metres. Capping produces something a vehicle can actually follow.
    """
    if len(prof) < 3:
        return prof
    sigma = max(1.0, smooth_m / max(pixel_size, 1e-6) / 3.0)
    out = gaussian_filter1d(prof.astype(np.float64), sigma=sigma, mode='nearest')

    if max_grade:
        step = max(pixel_size, 1e-6)
        limit = max_grade * step

        # The cap has to be checked against what the terrain actually does.
        # Chaining it station by station makes the profile an integrator: on a
        # slope steeper than the limit it cannot descend fast enough, so it
        # floats higher and higher above the ground -- measured 37 m above
        # terrain partway along a road dropping 132 m, which then stamps a
        # ridge instead of a road. Only apply the cap where the road can
        # actually follow it, and re-anchor to the ground otherwise.
        ground = out.copy()
        max_float = max(2.0, limit * 8.0)
        for _ in range(2):
            for i in range(1, len(out)):
                d = out[i] - out[i - 1]
                if abs(d) > limit:
                    out[i] = out[i - 1] + np.sign(d) * limit
                if abs(out[i] - ground[i]) > max_float:
                    out[i] = ground[i]
            out = out[::-1]
            ground = ground[::-1]
    return out


def monotonic_downhill(prof, pixel_size, min_drop=0.0005, max_cut=None):
    """Force a profile to never run uphill, without bulldozing the terrain.

    A river that climbs is the most visible artefact in a carved waterway:
    water pools in the dips and the channel reads as a chain of ponds. The
    naive running-minimum fix is worse, though. Where a polyline genuinely
    rises -- an OSM way crossing a ridge, or a tributary digitised toward its
    source -- it drags the whole downstream reach to the lowest elevation
    seen so far. Measured on a line crossing Bamiyan's ridges that flattened
    68% of the profile, one run 1156 m long, cutting up to 160 m deep.

    `max_cut` bounds how far below the original ground the bed may be pushed.
    Where holding the descent would need a deeper cut than that, the profile
    is allowed to rise again and a new reach begins -- which is what a real
    drainage network does at a watershed divide.
    """
    if len(prof) < 2:
        return prof
    out = prof.astype(np.float64).copy()
    flipped = out[-1] > out[0]
    if flipped:
        out = out[::-1]
    ground = out.copy()

    step = max(pixel_size, 1e-6) * min_drop
    limit = None if max_cut is None else float(max_cut)

    for i in range(1, len(out)):
        target = out[i - 1] - step
        if out[i] >= target:
            if limit is not None and (ground[i] - target) > limit:
                # Too deep a cut: start a fresh reach at ground level rather
                # than trenching through the high ground.
                out[i] = ground[i]
            else:
                out[i] = target
    return out[::-1] if flipped else out


# ---------------------------------------------------------------------------
# Rasterising a profile back across a feature's width
# ---------------------------------------------------------------------------

def stamp_profile(z, pts, prof, half_width_px, feather_px, out=None,
                  weight=None, depth=0.0):
    """Write `prof` across a corridor around the polyline.

    Each raster pixel takes the elevation of its nearest station on the line,
    so there is no cross-slope. `feather_px` blends back to the surrounding
    terrain; `depth` sinks the surface (used for riverbeds).
    """
    h, w = z.shape
    if out is None:
        out = z.astype(np.float32).copy()
    if weight is None:
        weight = np.zeros(z.shape, np.float32)

    reach = int(np.ceil(half_width_px + feather_px)) + 1

    r_lo = max(0, int(np.floor(pts[:, 0].min())) - reach)
    r_hi = min(h, int(np.ceil(pts[:, 0].max())) + reach + 1)
    c_lo = max(0, int(np.floor(pts[:, 1].min())) - reach)
    c_hi = min(w, int(np.ceil(pts[:, 1].max())) + reach + 1)
    if r_hi <= r_lo or c_hi <= c_lo:
        return out, weight

    # Nearest-station lookup over the feature's bounding box only.
    #
    # This used to compare every pixel in the bounding box against every
    # station in chunks of 512, which allocates a (bbox_pixels x 512) float64
    # matrix. For a road spanning an 8192 px export that is 281 GB: it either
    # raised MemoryError or thrashed until the host gave up, and because the
    # caller wrapped it in a bare `except Exception` the export silently fell
    # back to the slow raster path -- or took QGIS down with it.
    #
    # A distance transform does the same job in one pass. Seed a small raster
    # with the station indices, and scipy hands back both the distance to the
    # nearest seed and which seed it was, in O(bbox) time and memory.
    sub_shape = (r_hi - r_lo, c_hi - c_lo)
    seed_idx = np.full(sub_shape, -1, np.int32)
    sr = np.clip(np.round(pts[:, 0]).astype(np.int64) - r_lo, 0, sub_shape[0] - 1)
    sc = np.clip(np.round(pts[:, 1]).astype(np.int64) - c_lo, 0, sub_shape[1] - 1)
    seed_idx[sr, sc] = np.arange(len(pts), dtype=np.int32)
    seeds = seed_idx >= 0
    if not seeds.any():
        return out, weight

    dist2d, (iy, ix) = distance_transform_edt(~seeds, return_indices=True)
    dist = dist2d.reshape(-1)
    best_i = seed_idx[iy, ix].reshape(-1).astype(np.int64)

    inside = dist <= half_width_px + feather_px
    if not inside.any():
        return out, weight

    rr, cc = np.mgrid[r_lo:r_hi, c_lo:c_hi]
    flat_r = rr.reshape(-1, 1)
    flat_c = cc.reshape(-1, 1)

    # Snapping to the nearest station still leaves a cant: stations sit about
    # a pixel apart, so pixels across the width land on different ones, and on
    # a steep road consecutive stations differ measurably. Take the pixel's
    # perpendicular foot on the line -- a fractional station -- and interpolate
    # the profile there, so every pixel in one cross-section reads the same
    # elevation regardless of which station happened to be nearest.
    tangent = np.gradient(pts, axis=0)
    tlen = np.hypot(tangent[:, 0], tangent[:, 1])
    tlen[tlen < 1e-9] = 1.0
    tangent = tangent / tlen[:, None]

    off_r = flat_r.reshape(-1) - pts[best_i, 0]
    off_c = flat_c.reshape(-1) - pts[best_i, 1]
    along = off_r * tangent[best_i, 0] + off_c * tangent[best_i, 1]
    station = np.clip(best_i + along, 0.0, len(prof) - 1.0)

    target = np.interp(station, np.arange(len(prof), dtype=np.float64),
                       prof) - depth
    alpha = np.ones_like(dist)
    if feather_px > 0:
        alpha = np.clip((half_width_px + feather_px - dist) / feather_px, 0.0, 1.0)
    alpha[~inside] = 0.0

    sub = (slice(r_lo, r_hi), slice(c_lo, c_hi))
    a = alpha.reshape(rr.shape).astype(np.float32)
    t = target.reshape(rr.shape).astype(np.float32)

    # Where corridors overlap (a junction), the strongest claim wins rather
    # than the last one drawn.
    prev = weight[sub]
    take = a > prev
    blended = out[sub] * (1.0 - a) + t * a
    out[sub] = np.where(take, blended, out[sub])
    weight[sub] = np.maximum(prev, a)
    return out, weight


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def apply_roads(z, features, pixel_size, half_width_m, feather_m,
                smooth_m=60.0, max_grade=0.08, log=None, progress=None,
                width_multiplier=1.0):
    """Flatten each road/rail corridor along its own smoothed profile.

    `progress` is called as progress(done, total) after each feature. Shaping a
    full export takes tens of seconds; without a per-feature tick the caller has
    nothing to report and the UI simply stops responding, which is
    indistinguishable from a hang.

    `width_multiplier` over-widens the corridor to compensate for Enfusion's
    smoothing narrowing it on import. It applies to a feature's own OSM width
    as well as to the class default -- upstream's buffer expression multiplies
    both, and leaving OSM-tagged roads unmultiplied made the setting silently
    do nothing for every road whose width OSM actually knows. The result is
    capped at 1.5x the class default, as upstream does, so one mis-tagged
    `width` cannot flatten a runway.
    """
    out = z.astype(np.float32).copy()
    weight = np.zeros(z.shape, np.float32)
    mult = max(width_multiplier, 1e-6)
    default_half_px = max(0.5, half_width_m / max(pixel_size, 1e-6))
    max_half_px = default_half_px * 1.5
    feather_px = max(0.5, feather_m / max(pixel_size, 1e-6))

    total = len(features)
    done = 0
    for item in features:
        # A feature may carry its own width from OSM; fall back to the class
        # default where the tags did not say.
        if isinstance(item, tuple):
            pts, width_m = item
            if width_m:
                half_px = min(
                    max(0.5, (width_m * mult * 0.5) / max(pixel_size, 1e-6)),
                    max_half_px)
            else:
                half_px = default_half_px
        else:
            pts, half_px = item, default_half_px

        pts = np.asarray(pts, np.float64)
        if len(pts) < 2:
            continue
        pts = densify(pts, step=1.0)
        prof = sample_profile(z, pts)
        prof = smooth_profile(prof, pixel_size, smooth_m, max_grade)
        out, weight = stamp_profile(z, pts, prof, half_px, feather_px,
                                    out=out, weight=weight)
        done += 1
        if progress:
            progress(done, total)

    if log:
        log("Roads shaped: %d features, %d px modified"
            % (done, int((out != z).sum())))
    return out


def apply_rivers(z, features, pixel_size, half_width_m, feather_m,
                 depth_m=1.5, bank_m=None, log=None, progress=None):
    """Carve waterways along a monotonically descending profile."""
    out = z.astype(np.float32).copy()
    weight = np.zeros(z.shape, np.float32)
    half_px = max(0.5, half_width_m / max(pixel_size, 1e-6))
    feather_px = max(0.5, (bank_m if bank_m else feather_m) /
                     max(pixel_size, 1e-6))

    total = len(features)
    done = 0
    for pts in features:
        pts = np.asarray(pts, np.float64)
        if len(pts) < 2:
            continue
        pts = densify(pts, step=1.0)
        prof = sample_profile(z, pts)
        prof = gaussian_filter1d(prof.astype(np.float64),
                                 sigma=max(1.0, 20.0 / max(pixel_size, 1e-6) / 3.0),
                                 mode='nearest')
        # Allow a few channel depths of cut, no more; beyond that the line is
        # crossing high ground rather than following a valley.
        prof = monotonic_downhill(prof, pixel_size,
                                  max_cut=max(3.0, depth_m * 4.0))
        out, weight = stamp_profile(z, pts, prof, half_px, feather_px,
                                    out=out, weight=weight, depth=depth_m)
        done += 1
        if progress:
            progress(done, total)

    # Never raise terrain when carving: a channel should cut in, not build up.
    # Applying that as a blanket minimum would undo the monotonic bed, because
    # wherever the descending profile sits above the noisy original the clamp
    # snaps it back down to the noise -- reintroducing every pool the profile
    # existed to remove. Restrict the clamp to pixels outside the channel core,
    # where it only stops the feathered banks from bulging upward.
    core = weight >= 0.999
    out = np.where(core, out, np.minimum(out, z.astype(np.float32)))

    if log:
        log("Rivers carved: %d features, %d px modified"
            % (done, int((out != z).sum())))
    return out


def extract_lines(layer, transform_fn, width_field=None):
    """QGIS vector layer -> list of (row, col) pixel polylines.

    With `width_field`, returns (pts, width_m) pairs instead of bare arrays.
    ARTE derives a real per-road width from the OSM `width` and `lanes` tags and
    stores it on each feature (`arte_dyn_width`), using -1 to mean "not
    specified". Passing that through lets each road be shaped at its own width
    rather than one constant per class -- the difference between a 7 m highway
    and a 4 m tertiary road actually reaching the terrain.
    """
    feats = []
    if layer is None:
        return feats
    for f in layer.getFeatures():
        geom = f.geometry()
        if geom is None or geom.isEmpty():
            continue
        width = None
        if width_field:
            try:
                raw = f[width_field]
                if raw is not None:
                    raw = float(raw)
                    if raw > 0:
                        width = raw
            except Exception:
                width = None
        if geom.isMultipart():
            parts = geom.asMultiPolyline()
        else:
            parts = [geom.asPolyline()]
        for part in parts:
            if len(part) < 2:
                continue
            pts = np.array([transform_fn(p.x(), p.y()) for p in part],
                           dtype=np.float64)
            feats.append((pts, width) if width_field else pts)
    return feats
