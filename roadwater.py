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
        # Two passes: forward then backward, so the cap applies in both
        # directions and the result does not drift toward one end.
        for _ in range(2):
            for i in range(1, len(out)):
                d = out[i] - out[i - 1]
                if abs(d) > limit:
                    out[i] = out[i - 1] + np.sign(d) * limit
            out = out[::-1]
    return out


def monotonic_downhill(prof, pixel_size, min_drop=0.0005):
    """Force a profile to never run uphill.

    A river that climbs is the single most visible artefact in a carved
    waterway: water pools in the dips and the channel reads as a chain of
    ponds. OSM waterway direction is unreliable, so the descent direction is
    taken from whichever end sits higher.
    """
    if len(prof) < 2:
        return prof
    out = prof.astype(np.float64).copy()
    if out[-1] > out[0]:
        out = out[::-1]
        flipped = True
    else:
        flipped = False

    step = max(pixel_size, 1e-6) * min_drop
    for i in range(1, len(out)):
        if out[i] >= out[i - 1] - step:
            out[i] = out[i - 1] - step
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
    rr, cc = np.mgrid[r_lo:r_hi, c_lo:c_hi]
    flat_r = rr.reshape(-1, 1)
    flat_c = cc.reshape(-1, 1)

    # Chunk the stations so a long line does not allocate an enormous matrix.
    best_d = np.full(flat_r.shape[0], np.inf)
    best_i = np.zeros(flat_r.shape[0], np.int64)
    CH = 512
    for s in range(0, len(pts), CH):
        seg = pts[s:s + CH]
        d = ((flat_r - seg[:, 0]) ** 2 + (flat_c - seg[:, 1]) ** 2)
        loc = d.argmin(axis=1)
        dmin = d[np.arange(d.shape[0]), loc]
        upd = dmin < best_d
        best_d[upd] = dmin[upd]
        best_i[upd] = loc[upd] + s

    dist = np.sqrt(best_d)
    inside = dist <= half_width_px + feather_px
    if not inside.any():
        return out, weight

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
                smooth_m=60.0, max_grade=0.08, log=None):
    """Flatten each road/rail corridor along its own smoothed profile."""
    out = z.astype(np.float32).copy()
    weight = np.zeros(z.shape, np.float32)
    half_px = max(0.5, half_width_m / max(pixel_size, 1e-6))
    feather_px = max(0.5, feather_m / max(pixel_size, 1e-6))

    done = 0
    for pts in features:
        pts = np.asarray(pts, np.float64)
        if len(pts) < 2:
            continue
        pts = densify(pts, step=1.0)
        prof = sample_profile(z, pts)
        prof = smooth_profile(prof, pixel_size, smooth_m, max_grade)
        out, weight = stamp_profile(z, pts, prof, half_px, feather_px,
                                    out=out, weight=weight)
        done += 1

    if log:
        log("Roads shaped: %d features, %d px modified"
            % (done, int((out != z).sum())))
    return out


def apply_rivers(z, features, pixel_size, half_width_m, feather_m,
                 depth_m=1.5, bank_m=None, log=None):
    """Carve waterways along a monotonically descending profile."""
    out = z.astype(np.float32).copy()
    weight = np.zeros(z.shape, np.float32)
    half_px = max(0.5, half_width_m / max(pixel_size, 1e-6))
    feather_px = max(0.5, (bank_m if bank_m else feather_m) /
                     max(pixel_size, 1e-6))

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
        prof = monotonic_downhill(prof, pixel_size)
        out, weight = stamp_profile(z, pts, prof, half_px, feather_px,
                                    out=out, weight=weight, depth=depth_m)
        done += 1

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


def extract_lines(layer, transform_fn):
    """QGIS vector layer -> list of (row, col) pixel polylines."""
    feats = []
    if layer is None:
        return feats
    for f in layer.getFeatures():
        geom = f.geometry()
        if geom is None or geom.isEmpty():
            continue
        if geom.isMultipart():
            parts = geom.asMultiPolyline()
        else:
            parts = [geom.asPolyline()]
        for part in parts:
            if len(part) < 2:
                continue
            pts = np.array([transform_fn(p.x(), p.y()) for p in part],
                           dtype=np.float64)
            feats.append(pts)
    return feats
