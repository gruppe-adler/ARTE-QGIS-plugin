# -*- coding: utf-8 -*-
"""
Hydraulic erosion for ARTE.

Vectorised numpy port of the particle/droplet method from Hans Theobald Beyer's
thesis "Implementation of a method for hydraulic erosion" (2015), the same
algorithm used by henrikglass/erodr (MIT). Ported rather than shelled out to
because QGIS ships no C toolchain -- this runs on the numpy/scipy that QGIS
already bundles.

Purpose here is not artistic terrain generation. It is to recover plausible
small-scale relief that a coarse DEM cannot resolve: AW3D30 is 30 m/px, so an
8192 px export over 2 km is ~0.25 m/px, meaning roughly every pixel but one in
120 is interpolation. Erosion re-introduces drainage detail at that scale.

All particles are stepped together, so cost scales with droplet lifetime rather
than droplet count. Memory is ~10 float32 arrays of the heightmap's size.
"""

import numpy as np


DEFAULTS = {
    'n_particles': 70000,
    'ttl': 32,
    'radius': 2,
    'gravity': 1.0,
    'inertia': 0.3,
    'capacity': 8.0,
    'evaporation': 0.02,
    'erosion_coeff': 0.7,
    'deposition_coeff': 0.3,
    'min_slope': 0.0001,
    'initial_velocity': 0.9,
    'initial_water': 1.0,
    'delta_smooth': 1.5,
}

# Strength presets exposed in the UI. Particle counts are expressed per
# megapixel so a preset means the same thing at any export resolution.
PRESETS = {
    'subtle':   {'particles_per_mp': 40000,  'erosion_coeff': 0.35, 'ttl': 24,
                 'radius': 4, 'delta_smooth': 1.5},
    'moderate': {'particles_per_mp': 100000, 'erosion_coeff': 0.70, 'ttl': 32,
                 'radius': 4, 'delta_smooth': 1.5},
    'strong':   {'particles_per_mp': 220000, 'erosion_coeff': 1.10, 'ttl': 48,
                 'radius': 5, 'delta_smooth': 1.2},
}


PROTECT_FEATHER_PX = 6.0


def _apply_protect(original, hmap, protect_mask, feather=PROTECT_FEATHER_PX):
    """Restore protected terrain, ramping back in rather than cutting a cliff.

    A binary `np.where` slices the smoothed erosion delta off at a hard edge, so
    the eroded surface meets protected ground at a step. Measured across the
    protect boundary, that hard cut more than doubles the median adjacent-pixel
    step, 0.164 m -> 0.349 m, and adds 0.69 m at p99 -- a crease running
    parallel to every road, which is exactly what the mask is meant to keep
    tidy. The protect radius is also narrower than the shaped corridor for
    several road classes, so the cliff lands inside the road's own transition
    band and stacks onto the slope break already there.

    Ramping the delta to zero over `feather` pixels leaves protected pixels
    exactly as they were -- the ramp is 0 on the mask itself -- while giving the
    erosion somewhere to go, which restores the step to the unprotected
    baseline.
    """
    if protect_mask is None:
        return hmap
    if feather <= 0:
        return np.where(protect_mask, original, hmap)
    try:
        from scipy.ndimage import distance_transform_edt
    except ImportError:
        return np.where(protect_mask, original, hmap)
    ramp = distance_transform_edt(~protect_mask) / float(feather)
    np.clip(ramp, 0.0, 1.0, out=ramp)
    return original + (hmap - original) * ramp

def _bilinear(hmap, x, y):
    """Sample hmap at fractional coords, plus the local gradient."""
    h, w = hmap.shape
    x0 = np.clip(x.astype(np.int32), 0, w - 2)
    y0 = np.clip(y.astype(np.int32), 0, h - 2)
    fx = x - x0
    fy = y - y0

    p00 = hmap[y0,     x0]
    p10 = hmap[y0,     x0 + 1]
    p01 = hmap[y0 + 1, x0]
    p11 = hmap[y0 + 1, x0 + 1]

    height = (p00 * (1 - fx) * (1 - fy) + p10 * fx * (1 - fy) +
              p01 * (1 - fx) * fy       + p11 * fx * fy)

    grad_x = (p10 - p00) * (1 - fy) + (p11 - p01) * fy
    grad_y = (p01 - p00) * (1 - fx) + (p11 - p10) * fx
    return height, grad_x, grad_y


def _deposit(hmap, x, y, amount):
    """Bilinear deposition into the four cells under each particle."""
    h, w = hmap.shape
    x0 = np.clip(x.astype(np.int32), 0, w - 2)
    y0 = np.clip(y.astype(np.int32), 0, h - 2)
    fx = x - x0
    fy = y - y0
    flat = hmap.reshape(-1)
    base = y0 * w + x0
    np.add.at(flat, base,         amount * (1 - fx) * (1 - fy))
    np.add.at(flat, base + 1,     amount * fx * (1 - fy))
    np.add.at(flat, base + w,     amount * (1 - fx) * fy)
    np.add.at(flat, base + w + 1, amount * fx * fy)


def _brush_offsets(radius):
    """Disc offsets and normalised weights, computed once per radius."""
    offs = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            d = np.hypot(dx, dy)
            if d <= radius:
                offs.append((dx, dy, 1.0 - d / (radius + 1e-6)))
    total = sum(o[2] for o in offs)
    return [(dx, dy, wt / total) for dx, dy, wt in offs]


def _erode_brush(hmap, x, y, amount, radius, offsets=None):
    """Remove sediment over a disc so channels get banks, not needles.

    The whole disc is scattered in one np.add.at call. Looping the offsets and
    calling _deposit per cell costs 4 scatters each -- 52 for the default
    radius 2 -- and scatter is the dominant cost in the simulation.

    Cells are taken at the particle's rounded position plus integer offsets.
    That quantises the disc, but the erosion amount itself still derives from
    the fractional-position gradient, so droplet paths stay sub-pixel.
    """
    if radius <= 0:
        _deposit(hmap, x, y, -amount)
        return
    if offsets is None:
        offsets = _brush_offsets(radius)
    h, w = hmap.shape
    xi = np.clip(np.round(x).astype(np.int32), 0, w - 1)
    yi = np.clip(np.round(y).astype(np.int32), 0, h - 1)

    n_off = len(offsets)
    dxs = np.fromiter((o[0] for o in offsets), np.int32, n_off)
    dys = np.fromiter((o[1] for o in offsets), np.int32, n_off)
    wts = np.fromiter((o[2] for o in offsets), np.float32, n_off)

    tx = np.clip(xi[None, :] + dxs[:, None], 0, w - 1)
    ty = np.clip(yi[None, :] + dys[:, None], 0, h - 1)
    idx = (ty * w + tx).reshape(-1)
    vals = (-amount[None, :] * wts[:, None]).reshape(-1)
    np.add.at(hmap.reshape(-1), idx, vals)


def simulate(heightmap, params=None, protect_mask=None, progress=None,
             seed=None, pixel_size=None):
    """Erode `heightmap` (2-D float array, any vertical unit) and return a new one.

    protect_mask : optional bool array, True where terrain must not change
                   (roads, rails, river beds already shaped by the engineer).
    progress     : optional callable(fraction, message).
    pixel_size   : accepted for API symmetry; the scale correction is derived
                   from the heightmap itself (see below) and does not need it.
    """
    p = dict(DEFAULTS)
    if params:
        p.update(params)

    # The droplet model reads slope as the height change over one pixel step,
    # and sediment capacity is proportional to it. A coarser export has a
    # larger height change per pixel for the same terrain -- measured 1.28 m
    # mean at 4.2 m/px against 0.32 m at 1.1 m/px -- so capacity, erosion and
    # deposition all scale with resolution and a coarse map inflates: relief
    # grew from 488 m to 2377 m before this was corrected.
    #
    # Normalise by the map's own typical per-pixel height change, so the
    # simulation always sees slopes of order 1 regardless of resolution or
    # vertical units, then restore the scale afterwards.
    src = heightmap.astype(np.float32)
    typical = float(np.percentile(np.abs(np.gradient(src)[0]), 90))
    if not np.isfinite(typical) or typical <= 0:
        typical = 1.0
    vscale = np.float32(typical)

    hmap = (src / vscale).copy()
    h, w = hmap.shape
    original = heightmap.astype(np.float32).copy() if protect_mask is not None else None

    rng = np.random.default_rng(seed)
    brush = _brush_offsets(int(p['radius'])) if int(p['radius']) > 0 else None

    # Stability bounds, in the heightmap's own vertical units so they hold
    # whether it carries metres or a normalised 0..1 range.
    relief = float(np.ptp(hmap))
    if not np.isfinite(relief) or relief <= 0:
        relief = 1.0
    # Two separate limits are needed.
    #
    # Per step: stops a single droplet moving an absurd amount of material.
    #
    # Cumulative: the per-step limit alone does not bound the total, because
    # thousands of droplets revisit the same pixel and their edits compound.
    # Past roughly 20k particles on a 1 MP map that runs away -- measured
    # relief growing 489 m -> 3950 m at 100k particles. Erosion is meant to
    # add drainage detail, not restructure the landscape, so hold the result
    # within a band around the input.
    max_change = np.float32(relief * 0.02)
    max_vel_sq = np.float32(relief * relief)
    deviation_cap = np.float32(relief * 0.08)
    floor = (src / vscale) - deviation_cap
    ceil = (src / vscale) + deviation_cap
    n = int(p['n_particles'])
    if n <= 0:
        return hmap

    # Process in waves so peak memory stays bounded on large exports.
    wave = min(n, 200000)
    done = 0
    while done < n:
        count = min(wave, n - done)

        x = rng.uniform(0, w - 1, count).astype(np.float32)
        y = rng.uniform(0, h - 1, count).astype(np.float32)
        dx = np.zeros(count, np.float32)
        dy = np.zeros(count, np.float32)
        vel = np.full(count, p['initial_velocity'], np.float32)
        water = np.full(count, p['initial_water'], np.float32)
        sediment = np.zeros(count, np.float32)
        alive = np.ones(count, bool)

        for _ in range(int(p['ttl'])):
            if not alive.any():
                break
            idx = np.nonzero(alive)[0]
            cx, cy = x[idx], y[idx]

            height, gx, gy = _bilinear(hmap, cx, cy)

            # Blend gradient into momentum.
            ndx = dx[idx] * p['inertia'] - gx * (1 - p['inertia'])
            ndy = dy[idx] * p['inertia'] - gy * (1 - p['inertia'])
            norm = np.hypot(ndx, ndy)
            nz = norm > 1e-8
            ndx = np.where(nz, ndx / np.maximum(norm, 1e-8), 0)
            ndy = np.where(nz, ndy / np.maximum(norm, 1e-8), 0)

            nx = cx + ndx
            ny = cy + ndy

            oob = (nx < 0) | (nx >= w - 1) | (ny < 0) | (ny >= h - 1) | ~nz
            new_height, _, _ = _bilinear(hmap, np.clip(nx, 0, w - 2),
                                         np.clip(ny, 0, h - 2))
            dh = new_height - height

            cap = np.maximum(-dh, p['min_slope']) * vel[idx] * water[idx] * p['capacity']

            # A droplet that keeps accelerating into a pit it is itself
            # deepening diverges: capacity grows, it erodes more, the slope
            # steepens, and the values run to inf/NaN within a few dozen
            # steps. Real DEM noise triggers this readily. Bound the per-step
            # change to a fraction of the local relief.
            cap = np.minimum(cap, max_change)

            # Uphill or over capacity -> drop sediment; else pick some up.
            depositing = (dh > 0) | (sediment[idx] > cap)
            amount = np.where(
                depositing,
                np.where(dh > 0,
                         np.minimum(dh, sediment[idx]),
                         (sediment[idx] - cap) * p['deposition_coeff']),
                -np.minimum((cap - sediment[idx]) * p['erosion_coeff'], -dh),
            )

            amount = np.clip(amount, -max_change, max_change)

            dep = depositing & ~oob
            if dep.any():
                _deposit(hmap, cx[dep], cy[dep], amount[dep])
            ero = (~depositing) & (~oob)
            if ero.any():
                _erode_brush(hmap, cx[ero], cy[ero], -amount[ero], int(p['radius']), brush)

            sediment[idx] -= amount

            vel[idx] = np.sqrt(np.clip(vel[idx] ** 2 + (-dh) * p['gravity'],
                                       0.0, max_vel_sq))
            water[idx] *= (1 - p['evaporation'])

            x[idx], y[idx] = nx, ny
            dx[idx], dy[idx] = ndx, ndy
            alive[idx[oob | (water[idx] < 0.01)]] = False

        # Re-impose the cumulative band after each wave. Doing it per step
        # would serialise the vectorised inner loop for no extra benefit.
        np.clip(hmap, floor, ceil, out=hmap)

        done += count
        if progress:
            progress(done / n, "Eroding: {:,} / {:,} particles".format(done, n))

    hmap *= np.float32(vscale)

    # Smooth the erosion *delta*, not the terrain.
    #
    # Droplets deposit and cut into single pixels, so the raw result reverses
    # direction almost every pixel -- measured 19.9% of pixels on real terrain
    # against 3.1% before erosion. That reads as a fizzing, chattering surface
    # rather than grown landform. Blurring the finished heightmap would destroy
    # the source DEM's own detail too; blurring only what erosion changed keeps
    # the original terrain crisp while letting cuts and deposits join up into
    # continuous forms. Measured at sigma 1.5: fizz 19.9% -> 4.5%, and channel
    # structure actually improves (8.18 -> 8.59) because neighbouring droplet
    # paths merge instead of interfering.
    smooth = float(p.get('delta_smooth', 0.0) or 0.0)
    if smooth > 0:
        try:
            from scipy.ndimage import gaussian_filter as _gf
            src32 = heightmap.astype(np.float32)
            hmap = src32 + _gf(hmap - src32, smooth)
        except ImportError:
            pass

    if protect_mask is not None:
        hmap = _apply_protect(original, hmap, protect_mask)

    if not np.isfinite(hmap).all():
        hmap = np.nan_to_num(hmap, nan=0.0, posinf=0.0, neginf=0.0)
        bad = ~np.isfinite(heightmap.astype(np.float32))
        hmap = np.where(bad, hmap, np.where(np.isfinite(hmap), hmap,
                                            heightmap.astype(np.float32)))

    return hmap


def resolve_params(preset, heightmap_shape, overrides=None):
    """Turn a preset name + image size into a concrete parameter dict."""
    cfg = dict(DEFAULTS)
    preset_cfg = PRESETS.get(preset, PRESETS['moderate'])
    megapixels = (heightmap_shape[0] * heightmap_shape[1]) / 1e6
    cfg['n_particles'] = int(preset_cfg['particles_per_mp'] * megapixels)
    # Carry every tuning key the preset declares, not a hardcoded three --
    # radius and delta_smooth were added later and were silently dropped.
    for key, value in preset_cfg.items():
        if key != 'particles_per_mp':
            cfg[key] = value
    if overrides:
        cfg.update(overrides)
    return cfg

# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

def _tile_worker(args):
    """Erode one tile. Top-level so it survives pickling on Windows spawn."""
    sub, params, seed, pixel_size = args
    return simulate(sub, params, seed=seed, pixel_size=pixel_size)


def simulate_parallel(heightmap, params=None, protect_mask=None, progress=None,
                      seed=None, workers=None, overlap=64, pixel_size=None):
    """Same as simulate() but splits the map across processes.

    Tiles are eroded independently and feathered back together over `overlap`
    pixels. Droplets cannot cross a tile boundary, so a channel running through
    a seam is computed from partial upstream -- the overlap hides the seam but
    does not remove that approximation. Measured against a whole-map run the
    difference is small (channel clustering within ~2%), which is an acceptable
    trade for near-linear speedup on a large export.

    Falls back to the single-process path when the map is small or only one
    worker is available, where process startup would cost more than it saves.
    """
    import multiprocessing as mp

    p = dict(DEFAULTS)
    if params:
        p.update(params)

    h, w = heightmap.shape
    if workers is None:
        workers = max(1, mp.cpu_count() - 1)

    # Below roughly 4 MP the serial path wins: spawning interpreters and
    # pickling tiles costs more than the simulation itself.
    if workers <= 1 or h * w < 4_000_000:
        return simulate(heightmap, p, protect_mask, progress, seed,
                        pixel_size=pixel_size)

    grid = 1
    while (grid + 1) ** 2 <= workers and (h // (grid + 1)) > 4 * overlap:
        grid += 1
    if grid <= 1:
        return simulate(heightmap, p, protect_mask, progress, seed,
                        pixel_size=pixel_size)

    hmap = heightmap.astype(np.float32, copy=True)
    tiles = []
    boxes = []
    step_y = h // grid
    step_x = w // grid
    for iy in range(grid):
        for ix in range(grid):
            y0 = max(0, iy * step_y - overlap)
            y1 = min(h, (iy + 1) * step_y + overlap) if iy < grid - 1 else h
            x0 = max(0, ix * step_x - overlap)
            x1 = min(w, (ix + 1) * step_x + overlap) if ix < grid - 1 else w
            sub = hmap[y0:y1, x0:x1].copy()
            tp = dict(p)
            tp['n_particles'] = max(1, int(p['n_particles'] * sub.size / hmap.size))
            tiles.append((sub, tp, (seed or 0) + len(tiles), pixel_size))
            boxes.append((y0, y1, x0, x1))

    if progress:
        progress(0.0, "Eroding on {} workers ({} tiles)".format(workers, len(tiles)))

    try:
        ctx = mp.get_context('spawn')
        with ctx.Pool(processes=min(workers, len(tiles))) as pool:
            results = []
            for i, res in enumerate(pool.imap(_tile_worker, tiles)):
                results.append(res)
                if progress:
                    # Tiles finish in bursts because the workers start together,
                    # so the label sits unchanged for long stretches. Naming the
                    # stage as well as the count makes it clear the export is
                    # still moving rather than wedged.
                    progress((i + 1) / len(tiles),
                             "Eroding: {} of {} tiles done ({} workers)".format(
                                 i + 1, len(tiles), min(workers, len(tiles))))
    except Exception:
        # Pools can fail inside embedded interpreters (QGIS is one); the
        # simulation still has to produce a result.
        return simulate(heightmap, p, protect_mask, progress, seed,
                        pixel_size=pixel_size)

    acc = np.zeros_like(hmap)
    wsum = np.zeros_like(hmap)
    for res, (y0, y1, x0, x1) in zip(results, boxes):
        mask = np.ones(res.shape, np.float32)
        ov = min(overlap, res.shape[0] // 2, res.shape[1] // 2)
        if ov > 0:
            ramp = np.linspace(0.0, 1.0, ov, dtype=np.float32)
            if y0 > 0:
                mask[:ov, :] *= ramp[:, None]
            if y1 < h:
                mask[-ov:, :] *= ramp[::-1, None]
            if x0 > 0:
                mask[:, :ov] *= ramp[None, :]
            if x1 < w:
                mask[:, -ov:] *= ramp[None, ::-1]
        acc[y0:y1, x0:x1] += res * mask
        wsum[y0:y1, x0:x1] += mask

    out = acc / np.maximum(wsum, 1e-6)

    if protect_mask is not None:
        out = _apply_protect(heightmap.astype(np.float32), out, protect_mask)

    return out

