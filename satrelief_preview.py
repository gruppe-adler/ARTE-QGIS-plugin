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

    def __init__(self, z, rgb, pixel_size, params):
        super().__init__()
        self.z = z
        self.rgb = rgb
        self.pixel_size = pixel_size
        self.params = params

    def run(self):
        try:
            out, info = satrelief.apply(self.z, self.rgb, self.pixel_size,
                                        self.params)
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

        # Downsample heightmap and satmap by the SAME factor, and carry the
        # cutoff in metres, so the preview reproduces the export's geometry
        # rather than a differently-scaled version of it.
        step = max(1, int(max(heightmap.shape) / float(PREVIEW_MAX)))
        self.small = _downsample(heightmap, PREVIEW_MAX)
        self.pixel_size = self.pixel_full * step
        h, w = self.small.shape
        sat = np.asarray(satmap, np.float32)[:, :, :3]
        self.rgb = satrelief._match_grid(sat, (h, w))

        self.base_shade = to_pixmap(hillshade(self.small))
        self.view = WipeView(labels=("original", "sat-relief"))
        self.view.set_images(self.base_shade, None)

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
        self.lbl_info.setText(
            "Preview at %.2f m/px; the export runs at %.2f m/px. Detail below "
            "the cutoff is the only band touched, so the source DEM's own "
            "landform is left alone.\n\nThis effect is sub-metre against "
            "hundreds of metres of landform, so it is close to invisible in a "
            "before/after hillshade. Use the 'Added relief only' view to see "
            "what it actually does." % (self.pixel_size, self.pixel_full))

    def _run(self):
        if self.worker and self.worker.isRunning():
            self._pending = True
            return
        self.bar.show()
        self.worker = _Worker(self.small, self.rgb, self.pixel_size,
                              self.params())
        self.worker.done.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _ready(self, out, info):
        self.bar.hide()
        self._last = out
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
            self.lbl_fit.setText(
                "Fit %.3f — %s.\nSun estimated at %.0f° azimuth, "
                "%.0f° altitude.\nRelief amplitude %.2f m — %.2f× the detail "
                "the DEM already has at this scale."
                % (fit, quality, info.get('azimuth', 0),
                   info.get('altitude', 0), info.get('amplitude', 0.0), ratio))
            self.lbl_fit.setStyleSheet("font-weight:bold; color:#27ae60;")
            self._redraw()
            self.btn_apply.setEnabled(True)
        if self._pending:
            self._pending = False
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
            # One scale for both panes, set by the DEM's own detail, so the
            # comparison shows the true relative amplitude.
            scale = float(np.percentile(np.abs(base_band), 99))
            self.view.labels = ("DEM detail", "added relief")
            self.view.set_images(to_pixmap(relief_only(base_band, scale)),
                                 to_pixmap(relief_only(delta, scale)))
        else:
            self.view.labels = ("original", "sat-relief")
            self.view.set_images(self.base_shade,
                                 to_pixmap(hillshade(self._last,
                                                     ref=self.small)))

    def _failed(self, msg):
        self.bar.hide()
        self.lbl_fit.setText("Preview failed: %s" % msg)
        self.lbl_fit.setStyleSheet("font-weight:bold; color:#c0392b;")
