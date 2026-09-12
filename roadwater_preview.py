# -*- coding: utf-8 -*-
"""
Road and river shaping preview.

Same wipe-comparison idea as the erosion preview, but the useful view here is
not the hillshade -- a 7 m road on a 2 km map is a couple of pixels wide and
invisible at map scale. So this shows two things side by side:

  * a hillshade wipe for where the shaping lands, and
  * a cross-section and long-profile plot for what it actually did,

because the defects that matter are cant across the carriageway and pooling
along a riverbed, and neither is visible from above.
"""

import numpy as np

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QPolygonF
from qgis.PyQt.QtCore import QPointF
from qgis.PyQt.QtWidgets import (
    QDialog, QLabel, QSlider, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QWidget, QSizePolicy, QCheckBox, QProgressBar,
    QTabWidget, QDoubleSpinBox
)

import roadwater
from erosion_preview import (
    hillshade, to_pixmap, WipeView, _downsample, SP_EXPANDING, HORIZONTAL
)


class ProfilePlot(QWidget):
    """Minimal line plot: original vs shaped, with a zero-cant reference."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.series = []
        self.title = ""
        self.ylabel = ""
        self.setMinimumHeight(150)
        self.setSizePolicy(SP_EXPANDING, SP_EXPANDING)

    def set_series(self, series, title="", ylabel=""):
        """series: list of (label, colour, 1-D array)."""
        self.series = series
        self.title = title
        self.ylabel = ylabel
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing
                        if hasattr(QPainter, 'RenderHint')
                        else QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(25, 25, 28))
        if not self.series:
            return

        pad_l, pad_r, pad_t, pad_b = 52, 10, 22, 20
        w = self.width() - pad_l - pad_r
        h = self.height() - pad_t - pad_b
        if w <= 10 or h <= 10:
            return

        allv = np.concatenate([s[2] for s in self.series if len(s[2])])
        if not len(allv):
            return
        lo, hi = float(allv.min()), float(allv.max())
        if hi - lo < 1e-6:
            lo, hi = lo - 0.5, hi + 0.5
        span = hi - lo

        p.setPen(QColor(120, 120, 130))
        p.drawText(6, 14, self.title)
        p.setPen(QColor(70, 70, 80))
        for f in (0.0, 0.5, 1.0):
            y = pad_t + h * f
            p.drawLine(pad_l, int(y), pad_l + w, int(y))
            p.setPen(QColor(140, 140, 150))
            p.drawText(4, int(y) + 4, "%.1f" % (hi - span * f))
            p.setPen(QColor(70, 70, 80))

        for label, colour, data in self.series:
            if len(data) < 2:
                continue
            poly = QPolygonF()
            for i, v in enumerate(data):
                x = pad_l + w * i / float(len(data) - 1)
                y = pad_t + h * (1.0 - (v - lo) / span)
                poly.append(QPointF(x, y))
            p.setPen(QPen(QColor(colour), 2))
            p.drawPolyline(poly)

        x = pad_l + 4
        for label, colour, _d in self.series:
            p.setPen(QColor(colour))
            p.drawText(x, pad_t + 12, label)
            x += p.fontMetrics().horizontalAdvance(label) + 14


class _Worker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, z, roads, rivers, pixel_size, cfg):
        super().__init__()
        self.z = z
        self.roads = roads
        self.rivers = rivers
        self.pixel_size = pixel_size
        self.cfg = cfg

    def run(self):
        try:
            out = self.z
            if self.roads:
                out = roadwater.apply_roads(
                    out, self.roads, self.pixel_size,
                    half_width_m=self.cfg['road_width'] / 2.0,
                    feather_m=self.cfg['road_feather'],
                    smooth_m=self.cfg['road_smooth'],
                    max_grade=self.cfg['max_grade'])
            if self.rivers:
                out = roadwater.apply_rivers(
                    out, self.rivers, self.pixel_size,
                    half_width_m=self.cfg['river_width'] / 2.0,
                    feather_m=self.cfg['river_bank'],
                    depth_m=self.cfg['river_depth'])
            self.done.emit(out)
        except Exception as exc:
            self.failed.emit(str(exc))


class RoadWaterPreviewDialog(QDialog):
    """Tune road flattening and river carving against a live preview."""

    def __init__(self, heightmap, roads=None, rivers=None, pixel_size=1.0,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("ARTE - Road & River Shaping")
        self.resize(1000, 700)

        self.pixel_full = pixel_size or 1.0
        self.worker = None
        self._pending = False

        # Work at preview scale, scaling the geometry to match.
        target = 768
        step = max(1, int(max(heightmap.shape) / float(target)))
        self.small = _downsample(heightmap, target)
        self.scale = 1.0 / step
        self.pixel_size = self.pixel_full * step

        self.roads = [np.asarray(r, np.float64) * self.scale for r in (roads or [])]
        self.rivers = [np.asarray(r, np.float64) * self.scale for r in (rivers or [])]

        self.base_shade = to_pixmap(hillshade(self.small))
        self.view = WipeView()
        self.view.set_images(self.base_shade, None)

        self.plot_cross = ProfilePlot()
        self.plot_long = ProfilePlot()

        self.sp_road_w = self._spin(1.0, 40.0, 7.0, " m")
        self.sp_road_feather = self._spin(0.0, 40.0, 6.0, " m")
        self.sp_road_smooth = self._spin(0.0, 400.0, 90.0, " m")
        self.sp_grade = self._spin(0.0, 0.30, 0.07, "", decimals=3, step=0.005)
        self.sp_river_w = self._spin(1.0, 60.0, 8.0, " m")
        self.sp_river_depth = self._spin(0.0, 15.0, 1.5, " m")
        self.sp_river_bank = self._spin(0.0, 60.0, 10.0, " m")

        grid = QGridLayout()
        rows = [
            ("Road width", self.sp_road_w),
            ("Road shoulder blend", self.sp_road_feather),
            ("Road profile smoothing", self.sp_road_smooth),
            ("Max gradient (rise/run)", self.sp_grade),
            ("River width", self.sp_river_w),
            ("River depth", self.sp_river_depth),
            ("River bank blend", self.sp_river_bank),
        ]
        for i, (name, widget) in enumerate(rows):
            grid.addWidget(QLabel(name), i, 0)
            grid.addWidget(widget, i, 1)

        self.lbl_stats = QLabel()
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet("color:#9aa;")

        box = QGroupBox("Shaping")
        bl = QVBoxLayout(box)
        bl.addLayout(grid)
        bl.addWidget(self.lbl_stats)

        tabs = QTabWidget()
        tabs.addTab(self.view, "Map")
        plots = QWidget()
        pl = QVBoxLayout(plots)
        pl.addWidget(self.plot_cross)
        pl.addWidget(self.plot_long)
        tabs.addTab(plots, "Profiles")

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()

        btn_apply = QPushButton("Apply")
        btn_skip = QPushButton("Cancel")
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self.accept)
        btn_skip.clicked.connect(self.reject)

        btns = QHBoxLayout()
        btns.addWidget(self.bar, 1)
        btns.addStretch()
        btns.addWidget(btn_skip)
        btns.addWidget(btn_apply)

        side = QVBoxLayout()
        side.addWidget(box)
        side.addStretch()

        top = QHBoxLayout()
        top.addWidget(tabs, 3)
        top.addLayout(side, 2)

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(btns)

        for sp in (self.sp_road_w, self.sp_road_feather, self.sp_road_smooth,
                   self.sp_grade, self.sp_river_w, self.sp_river_depth,
                   self.sp_river_bank):
            sp.valueChanged.connect(self._run)

        self._run()

    def _spin(self, lo, hi, val, suffix, decimals=1, step=0.5):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(decimals)
        sp.setSingleStep(step)
        sp.setValue(val)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    def settings(self):
        return {
            'road_width': self.sp_road_w.value(),
            'road_feather': self.sp_road_feather.value(),
            'road_smooth': self.sp_road_smooth.value(),
            'max_grade': self.sp_grade.value() or None,
            'river_width': self.sp_river_w.value(),
            'river_depth': self.sp_river_depth.value(),
            'river_bank': self.sp_river_bank.value(),
        }

    def _run(self):
        if self.worker and self.worker.isRunning():
            self._pending = True
            return
        self.bar.show()
        self.worker = _Worker(self.small, self.roads, self.rivers,
                              self.pixel_size, self.settings())
        self.worker.done.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _ready(self, out):
        self.bar.hide()
        self.view.set_images(self.base_shade,
                             to_pixmap(hillshade(out, ref=self.small)))
        self._update_plots(out)
        if self._pending:
            self._pending = False
            self._run()

    def _failed(self, msg):
        self.bar.hide()
        self.lbl_stats.setText("Shaping failed: %s" % msg)

    def _update_plots(self, out):
        h, w = self.small.shape
        notes = []

        if self.roads:
            pts = roadwater.densify(self.roads[0], 1.0)
            mid = len(pts) // 2
            # Cross-section perpendicular to the road at its midpoint.
            tan = pts[min(mid + 1, len(pts) - 1)] - pts[max(mid - 1, 0)]
            n = np.hypot(*tan) or 1.0
            perp = np.array([-tan[1] / n, tan[0] / n])
            span = np.arange(-12, 13)
            coords = pts[mid][None, :] + perp[None, :] * span[:, None]
            before = roadwater.sample_profile(self.small, coords)
            after = roadwater.sample_profile(out, coords)
            self.plot_cross.set_series(
                [("original", "#e08a5a", before), ("shaped", "#5ac8e0", after)],
                "Road cross-section (perpendicular, metres across)")
            core = span[np.abs(span) <= max(1, int(
                self.sp_road_w.value() / 2.0 / self.pixel_size))]
            if len(core) >= 2:
                idx = np.isin(span, core)
                notes.append("Road cant across carriageway: %.3f m before, "
                             "%.3f m after." % (np.ptp(before[idx]),
                                                np.ptp(after[idx])))

        if self.rivers:
            pts = roadwater.densify(self.rivers[0], 1.0)
            before = roadwater.sample_profile(self.small, pts)
            after = roadwater.sample_profile(out, pts)
            self.plot_long.set_series(
                [("original", "#e08a5a", before), ("carved", "#7ad17a", after)],
                "River long profile (downstream)")

            def against_flow(b):
                d = np.diff(b)
                return int(((-d if b[0] < b[-1] else d) > 0).sum())

            notes.append("River steps running against flow: %d before, "
                         "%d after (of %d)."
                         % (against_flow(before), against_flow(after),
                            max(1, len(before) - 1)))

        notes.append("Preview at %.2f m/px; the export runs at %.2f m/px."
                     % (self.pixel_size, self.pixel_full))
        self.lbl_stats.setText("\n".join(notes))
