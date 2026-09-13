# -*- coding: utf-8 -*-
"""
Preview for satmap-derived micro-relief.

The important thing on screen is not the sliders, it is the **fit score**. Shape
from shading only works where the imagery is dominated by sun shading, and this
dialog measures whether that holds for the satmap in hand before anything is
committed to the export. A scene of snow, cloud, dense canopy or flat farmland
scores near zero and the pass declines to run; the number and its verdict are
shown plainly rather than hidden behind a checkbox.

Preview fidelity note, and it runs opposite to the erosion preview: this effect
lives *below* the source DEM's cell size. At preview scale a 30 m cutoff is a
fraction of a pixel, so the geometry has to be kept in metres and both rasters
downsampled by the same factor, or the preview would show nothing while the
export looked fine.
"""

import numpy as np
from scipy.ndimage import gaussian_filter

from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QProgressBar, QDoubleSpinBox, QComboBox
)

try:
    from . import satrelief
    from .erosion_preview import (
        hillshade, to_pixmap, WipeView, _downsample, PREVIEW_MAX
    )
except ImportError:  # pragma: no cover - console / test use
    import satrelief
    from erosion_preview import (
        hillshade, to_pixmap, WipeView, _downsample, PREVIEW_MAX
    )


def relief_only(delta, scale=None):
    """Render a height field stretched about zero, optionally on a fixed scale.

    A side-by-side hillshade cannot show this effect and it is worth being
    explicit about why. `hillshade` sets its contrast stretch from the p99 of
    the terrain's own gradient, which on real terrain is metres per pixel; the
    micro-relief contributes centimetres per pixel. The change is then about 1%
    of the contrast range -- genuinely present in the data, and invisible on
    screen. Stretching the delta is the only view in which a sub-metre effect
    over hundreds of metres of landform can actually be seen.

    `scale` must be shared by both panes of the wipe. Stretching each to its own
    range independently is actively misleading: the added relief measures ~0.1x
    the amplitude of the DEM's existing detail, but rendered separately the two
    look equally strong, so the pass reads as if it were replacing the terrain
    rather than adding a tenth on top of it.

    Stretched symmetrically about zero so that flat reads as mid-grey and the
    sign of the change is legible.
    """
    d = np.nan_to_num(np.asarray(delta, np.float32))
    if scale is None:
        scale = float(np.percentile(np.abs(d), 99))
    if not np.isfinite(scale) or scale < 1e-9:
        return np.full(d.shape, 128, np.uint8)
    return np.clip((d / scale) * 127.0 + 128.0, 0, 255).astype(np.uint8)


class _Worker(QThread):
    done = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, z, rgb, pixel_size, params, sun=None):
        super().__init__()
        self.z = z
        self.rgb = rgb
        self.pixel_size = pixel_size
        self.params = params
        self.sun = sun

    def run(self):
        try:
            out, info = satrelief.apply(self.z, self.rgb, self.pixel_size,
                                        self.params, sun=self.sun)
            self.done.emit(out, info)
        except Exception as exc:
            self.failed.emit(str(exc))


class SatReliefPreviewDialog(QDialog):
    """Tune satmap micro-relief, and see whether the imagery supports it."""

    def __init__(self, heightmap, satmap, pixel_size=1.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ARTE - Satellite Micro-Relief")
        self.resize(950, 680)

        self.pixel_full = pixel_size or 1.0
        self.worker = None
        self._pending = False
        self._last = None
        self._last_info = None

        # Downsample heightmap and satmap by the SAME factor, and carry the
        # cutoff in metres, so the preview reproduces the export's geometry
        # rather than a differently-scaled version of it.
        step = max(1, int(max(heightmap.shape) / float(PREVIEW_MAX)))
        self.small = _downsample(heightmap, PREVIEW_MAX)
        self.pixel_size = self.pixel_full * step
        h, w = self.small.shape
        sat = np.asarray(satmap, np.float32)[:, :, :3]
        self.rgb = satrelief._match_grid(sat, (h, w))

        # Keep the full-resolution inputs. Zooming has to RECOMPUTE the visible
        # region at native scale, not magnify downsampled pixels: measured on
        # the same ground, the downsampled pass resolves 4.3x fewer features
        # along a scanline than the export does. Amplitude survives downsampling
        # (0.52 m against 0.55 m) so the fit and ratio are trustworthy either
        # way, but the SHAPE is what this view is for, and magnifying blocks
        # would show a shape the export never produces.
        self._full_z = np.asarray(heightmap, np.float32)
        self._full_sat = sat
        # Cache of the whole-map result, so returning from an inspected patch
        # is instant rather than a second full recompute.
        self._overview = None
        self._overview_info = None
        self.patch_m = 100.0
        self.inspecting = False

        self.base_shade = to_pixmap(hillshade(self.small))
        self.view = WipeView(labels=("original", "sat-relief"))
        self.view.set_images(self.base_shade, None)
        # A fixed 100 m patch: bounded work whatever the export's size, and at
        # 0.27 m/px it is ~370 px, which computes in well under a second.
        span_m = self.pixel_full * max(self._full_z.shape)
        self.view.patch_frac = min(1.0, self.patch_m / max(span_m, 1e-6))
        self.view.patch_requested.connect(self._inspect)

        self.btn_back = QPushButton("← Back to whole map")
        self.btn_back.clicked.connect(self._show_overview)
        self.btn_back.hide()

        self.cmb_view = QComboBox()
        self.cmb_view.addItem("Added relief only (recommended)", "delta")
        self.cmb_view.addItem("Hillshade before / after", "shade")
        self.cmb_view.currentIndexChanged.connect(self._redraw)

        self.sp_gain = self._spin(0.0, 2.0, 0.6, "", 2, 0.1)
        self.sp_cutoff = self._spin(5.0, 200.0, 30.0, " m", 0, 5.0)
        self.sp_clamp = self._spin(0.5, 20.0, 3.0, " m", 1, 0.5)
        self.sp_smooth = self._spin(0.0, 4.0, 1.0, "", 1, 0.2)

        grid = QGridLayout()
        for i, (name, widget) in enumerate([
                ("Strength", self.sp_gain),
                ("Detail finer than", self.sp_cutoff),
                ("Maximum change", self.sp_clamp),
                ("Smoothing", self.sp_smooth)]):
            grid.addWidget(QLabel(name), i, 0)
            grid.addWidget(widget, i, 1)

        # The honest confidence signal. Given its own box, because it decides
        # whether this feature does anything at all.
        self.lbl_fit = QLabel("Measuring...")
        self.lbl_fit.setWordWrap(True)
        self.lbl_fit.setStyleSheet("font-weight: bold;")
        fit_box = QGroupBox("Does this imagery support it?")
        fl = QVBoxLayout(fit_box)
        fl.addWidget(self.lbl_fit)

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet("color:#888;")

        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View"))
        view_row.addWidget(self.cmb_view, 1)

        box = QGroupBox("Settings")
        bl = QVBoxLayout(box)
        bl.addLayout(view_row)
        bl.addLayout(grid)
        bl.addWidget(self.lbl_info)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()

        self.btn_apply = QPushButton("Apply to Export")
        btn_skip = QPushButton("Cancel")
        self.btn_apply.setDefault(True)
        self.btn_apply.clicked.connect(self.accept)
        btn_skip.clicked.connect(self.reject)

        btns = QHBoxLayout()
        btns.addWidget(self.btn_back)
        btns.addWidget(self.bar, 1)
        btns.addStretch()
        btns.addWidget(btn_skip)
        btns.addWidget(self.btn_apply)

        side = QVBoxLayout()
        side.addWidget(fit_box)
        side.addWidget(box)
        side.addStretch()

        top = QHBoxLayout()
        top.addWidget(self.view, 3)
        top.addLayout(side, 2)

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(btns)

        for sp in (self.sp_gain, self.sp_cutoff, self.sp_clamp, self.sp_smooth):
            sp.valueChanged.connect(self._run)

        self._refresh_info()
        self._run()

    def _spin(self, lo, hi, val, suffix, decimals, step):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(decimals)
        sp.setSingleStep(step)
        sp.setValue(val)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    def params(self):
        return {
            'gain': self.sp_gain.value(),
            'cutoff_m': self.sp_cutoff.value(),
            'clamp_m': self.sp_clamp.value(),
            'delta_smooth': self.sp_smooth.value(),
        }

    def result_settings(self):
        return {'params': self.params()}

    def _refresh_info(self):
        if self.inspecting:
            self.lbl_info.setText(
                "Inspecting a %.0f m patch at %.2f m/px — the export's own "
                "resolution, so this is exactly what it will produce here.\n\n"
                "Detail below the cutoff is the only band touched, so the "
                "source DEM's own landform is left alone."
                % (self.patch_m, self.pixel_size))
        else:
            self.lbl_info.setText(
                "Overview at %.2f m/px; the export runs at %.2f m/px, so fine "
                "detail is under-resolved here — click anywhere to recompute a "
                "%.0f m patch at full resolution.\n\nThis effect is sub-metre "
                "against hundreds of metres of landform, so it is close to "
                "invisible in a before/after hillshade. Use 'Added relief "
                "only' to see what it does."
                % (self.pixel_size, self.pixel_full, self.patch_m))

    def _run(self):
        if self.worker and self.worker.isRunning():
            self._pending = True
            return
        self.bar.show()
        # A 100 m patch is too small a sample to re-estimate the sun from, and
        # the whole map already answered that question -- so carry the
        # overview's estimate into the patch. It is also the slowest step.
        sun = None
        if self.inspecting and self._overview_info:
            sun = (self._overview_info.get('azimuth'),
                   self._overview_info.get('altitude'))
            if sun[0] is None or sun[1] is None:
                sun = None
        self.worker = _Worker(self.small, self.rgb, self.pixel_size,
                              self.params(), sun=sun)
        self.worker.done.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _ready(self, out, info):
        self.bar.hide()
        self._last = out
        self._last_info = info
        fit = info.get('fit', 0.0)
        if not info.get('applied'):
            self.lbl_fit.setText(
                "Fit %.3f — declining.\n%s\n\nThe heightmap would be left "
                "unchanged." % (fit, info.get('reason', '')))
            self.lbl_fit.setStyleSheet("font-weight:bold; color:#c0392b;")
            self.view.set_images(self.base_shade, None)
            self.btn_apply.setEnabled(False)
        else:
            quality = ("strong" if fit >= satrelief.GOOD_FIT else
                       "weak, running at reduced strength")
            # State the amplitude relative to the detail the DEM already has.
            # The wipe shows shape; this is the honest answer to "how much is
            # this actually changing my terrain?".
            delta = out - self.small
            base_band = self.small - gaussian_filter(
                self.small, max(1.0, self.sp_cutoff.value() / self.pixel_size))
            ratio = delta.std() / max(float(base_band.std()), 1e-9)
            # %.2f printed a real 0.05 ratio as "0.00x". Use a percentage,
            # which stays readable across the range these ratios actually take.
            self.lbl_fit.setText(
                "Fit %.3f — %s.\nSun estimated at %.0f° azimuth, "
                "%.0f° altitude.\nRelief amplitude %.2f m — %.0f%% of the "
                "detail the DEM already has at this scale.\n\nEach pane is "
                "stretched to its own range, so the two look equally strong "
                "on screen; the percentage above is the real amplitude."
                % (fit, quality, info.get('azimuth', 0),
                   info.get('altitude', 0), info.get('amplitude', 0.0),
                   100.0 * ratio))
            self.lbl_fit.setStyleSheet("font-weight:bold; color:#27ae60;")
            self._redraw()
            self.btn_apply.setEnabled(True)
        if self._pending:
            self._pending = False
            self._run()

    def _inspect(self, cx, cy):
        """Recompute a 100 m patch at native resolution, centred on the click.

        The overview is computed downsampled, where the 30 m detail band is only
        a few pixels wide; measured on the same ground it resolves 4.3x fewer
        features along a scanline than the export does. Magnifying that would
        show a shape the export never produces, so the patch is recomputed from
        the full-resolution inputs instead.
        """
        if self._overview is None:
            self._overview = self._last
            self._overview_info = self._last_info
        fh, fw = self._full_z.shape
        half = max(16, int(self.patch_m / max(self.pixel_full, 1e-6) / 2))
        px = int(min(max(cx * fw, half), fw - half))
        py = int(min(max(cy * fh, half), fh - half))
        ys, xs = slice(py - half, py + half), slice(px - half, px + half)

        self.small = self._full_z[ys, xs]
        sat_patch = satrelief._match_grid(self._full_sat, self._full_z.shape)
        self.rgb = sat_patch[ys, xs]
        self.pixel_size = self.pixel_full
        self.base_shade = to_pixmap(hillshade(self.small))
        self.inspecting = True
        self.btn_back.show()
        self.view.patch_frac = 0.0
        self._refresh_info()
        self._run()

    def _show_overview(self):
        """Return to the whole map, from cache rather than recomputing."""
        step = max(1, int(max(self._full_z.shape) / float(PREVIEW_MAX)))
        self.small = _downsample(self._full_z, PREVIEW_MAX)
        self.pixel_size = self.pixel_full * step
        self.rgb = satrelief._match_grid(self._full_sat, self.small.shape)
        self.base_shade = to_pixmap(hillshade(self.small))
        self.inspecting = False
        self.btn_back.hide()
        span_m = self.pixel_full * max(self._full_z.shape)
        self.view.patch_frac = min(1.0, self.patch_m / max(span_m, 1e-6))
        self._refresh_info()
        if self._overview is not None:
            self._last = self._overview
            self._ready(self._overview, self._overview_info)
        else:
            self._run()

    def _redraw(self):
        """Draw the current result in whichever view is selected."""
        if self._last is None:
            return
        if self.cmb_view.currentData() == "delta":
            # Left: the terrain's own detail in the same band, so the two sides
            # are directly comparable -- what the DEM already has against what
            # the imagery adds. Both stretched independently, since the point is
            # the shape of the detail, not its amplitude.
            delta = self._last - self.small
            base_band = self.small - gaussian_filter(
                self.small, max(1.0, self.sp_cutoff.value() / self.pixel_size))
            # Each pane on its own scale, so the SHAPE of the added relief is
            # legible -- at 0.05x a shared scale renders it as flat grey, which
            # says nothing except "small", and the ratio is already stated as a
            # number above. Amplitude is the label's job; this view is for
            # judging whether the recovered detail looks like terrain.
            self.view.labels = ("DEM detail", "added relief")
            self.view.set_images(to_pixmap(relief_only(base_band)),
                                 to_pixmap(relief_only(delta)))
        else:
            self.view.labels = ("original", "sat-relief")
            self.view.set_images(self.base_shade,
                                 to_pixmap(hillshade(self._last,
                                                     ref=self.small)))

    def _failed(self, msg):
        self.bar.hide()
        self.lbl_fit.setText("Preview failed: %s" % msg)
        self.lbl_fit.setStyleSheet("font-weight:bold; color:#c0392b;")
