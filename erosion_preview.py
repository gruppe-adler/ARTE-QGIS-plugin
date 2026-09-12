# -*- coding: utf-8 -*-
"""
Erosion preview dialog.

Shows a hillshade of the heightmap with a draggable wipe between the original
and the eroded version, plus the parameter controls. The preview runs on a
downsampled copy so a change is visible in a second or two rather than the
minutes a full-resolution run takes.

Preview fidelity note: erosion is resolution dependent. A droplet crossing a
512 px preview covers eight times the ground a droplet crosses on a 4096 px
export, so the preview shows the *character* of a setting, not its exact
result. Particle counts are scaled per megapixel to keep the two comparable.
"""

import numpy as np

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QImage, QPixmap, QPainter, QPen, QColor
from qgis.PyQt.QtWidgets import (
    QDialog, QLabel, QSlider, QComboBox, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QWidget, QSizePolicy, QCheckBox, QProgressBar
)

try:
    from . import erosion          # loaded as part of the plugin package
except ImportError:  # pragma: no cover - console / test use
    import erosion


PREVIEW_MAX = 512


def hillshade(z, azimuth=315.0, altitude=45.0, vert_exag=3.0, ref=None):
    """Standard hillshade, in elevation units per pixel.

    `ref` fixes the contrast stretch to another array's statistics. Both sides
    of the wipe must be stretched identically or the eroded half simply looks
    brighter and the comparison is meaningless.
    """
    z = np.nan_to_num(z.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    src = z if ref is None else np.nan_to_num(ref.astype(np.float32))

    # Derive the contrast stretch from the interior only.
    #
    # An unfilled NoData border sits ~2465 m below the terrain, which is a
    # cliff at the frame edge. Measured on a real export it pushed the p99
    # slope from 5.4 to 248: the stretch is then set by the border rather than
    # the landscape, and identical terrain rendered at a hillshade contrast of
    # 13.5 instead of 44.8 -- a flat grey sheet with the relief still in the
    # data. Trimming a margin before measuring keeps the picture honest even
    # when the export has a void edge.
    margin = max(2, int(min(src.shape) * 0.03))
    inner = src[margin:-margin, margin:-margin] if min(src.shape) > 4 * margin else src
    spread = float(np.percentile(np.abs(np.gradient(inner)[0]), 99))
    if not np.isfinite(spread) or spread <= 0:
        spread = 1.0

    gy, gx = np.gradient(z / spread * vert_exag)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    shaded = (np.sin(alt) * np.cos(slope) +
              np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    shaded = np.nan_to_num(shaded, nan=0.0)
    return np.clip((shaded * 0.5 + 0.5) * 255.0, 0, 255).astype(np.uint8)


# QGIS 3 ships Qt5, QGIS 4 ships Qt6. Qt6 moved enum members out of the class
# namespace into scoped enums (QSizePolicy.Expanding -> QSizePolicy.Policy.
# Expanding, and so on). Resolve each one by walking the scopes rather than
# hardcoding a Qt version, so the plugin loads under both.
def _enum(owner, name, *scopes):
    val = getattr(owner, name, None)
    if val is not None:
        return val
    for scope in scopes:
        holder = getattr(owner, scope, None)
        if holder is not None:
            val = getattr(holder, name, None)
            if val is not None:
                return val
    raise AttributeError("cannot resolve %s.%s" % (owner.__name__, name))


_FMT_GRAY8 = _enum(QImage, 'Format_Grayscale8', 'Format')
SP_EXPANDING = _enum(QSizePolicy, 'Expanding', 'Policy')
CUR_SIZEHOR = _enum(Qt, 'SizeHorCursor', 'CursorShape')
ALIGN_CENTER = _enum(Qt, 'AlignCenter', 'AlignmentFlag')
KEEP_ASPECT = _enum(Qt, 'KeepAspectRatio', 'AspectRatioMode')
SMOOTH_XFORM = _enum(Qt, 'SmoothTransformation', 'TransformationMode')
LEFT_BUTTON = _enum(Qt, 'LeftButton', 'MouseButton')
HORIZONTAL = _enum(Qt, 'Horizontal', 'Orientation')


def to_pixmap(gray):
    """uint8 2-D array -> QPixmap, with the row stride Qt expects."""
    gray = np.ascontiguousarray(gray)
    h, w = gray.shape
    img = QImage(gray.data, w, h, w, _FMT_GRAY8)
    return QPixmap.fromImage(img.copy())


def _downsample(z, target):
    """Area-average `z` down to roughly `target` pixels on its long side."""
    z = z.astype(np.float32)
    step = max(1, int(max(z.shape) / float(target)))
    if step == 1:
        return z.copy()
    h = (z.shape[0] // step) * step
    w = (z.shape[1] // step) * step
    return z[:h, :w].reshape(h // step, step, w // step, step).mean(axis=(1, 3))


class WipeView(QWidget):
    """Before/after image with a draggable vertical divider."""

    def __init__(self, parent=None, labels=("original", "modified")):
        super().__init__(parent)
        self.before = None
        self.after = None
        self.labels = labels
        self.split = 0.5
        self.setMinimumSize(PREVIEW_MAX, PREVIEW_MAX)
        self.setSizePolicy(SP_EXPANDING, SP_EXPANDING)
        self.setCursor(CUR_SIZEHOR)

    def set_images(self, before, after):
        self.before = before
        self.after = after
        self.update()

    def _rect(self):
        if self.before is None:
            return None
        side = min(self.width(), self.height())
        x = (self.width() - side) // 2
        y = (self.height() - side) // 2
        return x, y, side

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        r = self._rect()
        if r is None:
            p.setPen(QColor(180, 180, 180))
            p.drawText(self.rect(), ALIGN_CENTER, "No preview yet")
            return
        x, y, side = r
        cut = int(side * self.split)

        if self.before is not None:
            p.drawPixmap(x, y, self.before.scaled(side, side, KEEP_ASPECT,
                                                  SMOOTH_XFORM))
        if self.after is not None and cut < side:
            scaled = self.after.scaled(side, side, KEEP_ASPECT,
                                       SMOOTH_XFORM)
            p.drawPixmap(x + cut, y, scaled, cut, 0, side - cut, side)

        p.setPen(QPen(QColor(255, 200, 0), 2))
        p.drawLine(x + cut, y, x + cut, y + side)
        p.setPen(QColor(255, 255, 255))
        left, right = self.labels
        p.drawText(x + 6, y + 18, left)
        p.drawText(x + side - 6 - p.fontMetrics().horizontalAdvance(right),
                   y + 18, right)

    def mousePressEvent(self, e):
        self._drag(e)

    def mouseMoveEvent(self, e):
        if e.buttons() & LEFT_BUTTON:
            self._drag(e)

    def _drag(self, e):
        r = self._rect()
        if r is None:
            return
        x, _, side = r
        self.split = min(1.0, max(0.0, (e.pos().x() - x) / float(side)))
        self.update()


class _Worker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, hmap, params, pixel_size=None, protect=None):
        super().__init__()
        self.hmap = hmap
        self.params = params
        self.pixel_size = pixel_size
        self.protect = protect

    def run(self):
        try:
            self.done.emit(erosion.simulate(self.hmap, self.params, seed=1,
                                            pixel_size=self.pixel_size,
                                            protect_mask=self.protect))
        except Exception as exc:
            self.failed.emit(str(exc))


class ErosionPreviewDialog(QDialog):
    """Tune erosion against a live preview; returns the chosen settings."""

    def __init__(self, heightmap, pixel_size=None, parent=None,
                 protect_lines=None, protect_width_m=12.0):
        super().__init__(parent)
        self.setWindowTitle("ARTE - Hydraulic Erosion")
        self.resize(900, 640)

        self.full = heightmap
        self.pixel_size = pixel_size
        self.worker = None
        self._pending = False
        self._protect_lines = protect_lines or []
        self._protect_width_m = protect_width_m
        self._protect = None

        # Downsample once; every preview run starts from this. Block-average
        # rather than stride: heightmap[::32, ::32] keeps 1 pixel in 1024 and
        # can miss whole valleys, which makes the preview unrepresentative.
        self.small = _downsample(heightmap, PREVIEW_MAX)
        self.base_shade = to_pixmap(hillshade(self.small))
        self._build_protect_mask()

        self.view = WipeView(labels=("original", "eroded"))
        self.view.set_images(self.base_shade, None)

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItems(["subtle", "moderate", "strong", "custom"])
        self.cmb_preset.setCurrentText("moderate")

        self.sl_strength = self._slider(0, 200, 100)
        self.sl_detail = self._slider(16, 256, 128)
        self.sl_radius = self._slider(0, 6, 2)
        self.sl_deposit = self._slider(0, 100, 30)

        self.lbl_strength = QLabel("1.00")
        self.lbl_detail = QLabel("128")
        self.lbl_radius = QLabel("2")
        self.lbl_deposit = QLabel("0.30")

        self.chk_protect = QCheckBox("Preserve roads / rivers shaped by OSM")
        self.chk_protect.setChecked(True)
        self.chk_protect.setToolTip(
            "Erosion is masked out where the terrain engineer already flattened "
            "roads or carved riverbeds, so it cannot undo that work.")

        self.chk_parallel = QCheckBox("Use all CPU cores for the final export")
        self.chk_parallel.setChecked(True)
        self.chk_parallel.setToolTip(
            "Splits the heightmap into tiles across processes. Roughly 5x faster "
            "on this machine. The preview always runs single-threaded.")

        grid = QGridLayout()
        rows = [
            ("Preset", self.cmb_preset, None),
            ("Strength", self.sl_strength, self.lbl_strength),
            ("Detail (droplet life)", self.sl_detail, self.lbl_detail),
            ("Channel width", self.sl_radius, self.lbl_radius),
            ("Deposition", self.sl_deposit, self.lbl_deposit),
        ]
        for i, (name, widget, lab) in enumerate(rows):
            grid.addWidget(QLabel(name), i, 0)
            grid.addWidget(widget, i, 1)
            if lab:
                lab.setMinimumWidth(40)
                grid.addWidget(lab, i, 2)

        box = QGroupBox("Settings")
        bl = QVBoxLayout(box)
        bl.addLayout(grid)
        bl.addWidget(self.chk_protect)
        bl.addWidget(self.chk_parallel)

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet("color:#888;")
        bl.addWidget(self.lbl_info)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()

        self.btn_apply = QPushButton("Apply to Export")
        self.btn_skip = QPushButton("Export Without Erosion")
        self.btn_apply.setDefault(True)
        self.btn_apply.clicked.connect(self.accept)
        self.btn_skip.clicked.connect(self.reject)

        btns = QHBoxLayout()
        btns.addWidget(self.bar, 1)
        btns.addStretch()
        btns.addWidget(self.btn_skip)
        btns.addWidget(self.btn_apply)

        side = QVBoxLayout()
        side.addWidget(box)
        side.addStretch()

        top = QHBoxLayout()
        top.addWidget(self.view, 3)
        top.addLayout(side, 2)

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(btns)

        for s in (self.sl_strength, self.sl_detail, self.sl_radius, self.sl_deposit):
            s.valueChanged.connect(self._on_change)
        self.cmb_preset.currentTextChanged.connect(self._on_preset)
        # Without this the checkbox only affected the export, never the preview.
        self.chk_protect.toggled.connect(lambda _=None: self._run_preview())

        self._sync_labels()
        self._sync_protect_state()
        self._refresh_info()
        self._run_preview()

    def _sync_protect_state(self):
        """Disable the protect toggle when there is no geometry to protect.

        Leaving it enabled and inert is worse than disabling it: it looks like
        a setting that does nothing.
        """
        have = self._protect is not None and bool(self._protect.any())
        self.chk_protect.setEnabled(have)
        if have:
            n = int(self._protect.sum())
            self.chk_protect.setText(
                "Preserve roads / rivers (%s px protected)" % "{:,}".format(n))
        else:
            self.chk_protect.setText(
                "Preserve roads / rivers (no OSM geometry loaded)")
            self.chk_protect.setToolTip(
                "No road or river centrelines were passed to this preview, so "
                "there is nothing to protect. The export still protects "
                "whatever the terrain engineer shaped.")

    def _slider(self, lo, hi, val):
        s = QSlider(HORIZONTAL)
        s.setRange(lo, hi)
        s.setValue(val)
        return s

    def _sync_labels(self):
        self.lbl_strength.setText("%.2f" % (self.sl_strength.value() / 100.0))
        self.lbl_detail.setText(str(self.sl_detail.value()))
        self.lbl_radius.setText(str(self.sl_radius.value()))
        self.lbl_deposit.setText("%.2f" % (self.sl_deposit.value() / 100.0))

    def _on_preset(self, name):
        if name == "custom":
            return
        cfg = erosion.PRESETS.get(name)
        if not cfg:
            return
        for s in (self.sl_strength, self.sl_detail):
            s.blockSignals(True)
        self.sl_strength.setValue(int(cfg['erosion_coeff'] * 100))
        self.sl_detail.setValue(int(cfg['ttl']))
        for s in (self.sl_strength, self.sl_detail):
            s.blockSignals(False)
        self._sync_labels()
        self._refresh_info()
        self._run_preview()

    def _on_change(self):
        self._sync_labels()
        if self.cmb_preset.currentText() != "custom":
            self.cmb_preset.blockSignals(True)
            self.cmb_preset.setCurrentText("custom")
            self.cmb_preset.blockSignals(False)
        self._refresh_info()
        self._run_preview()

    def _refresh_info(self):
        mp = (self.full.shape[0] * self.full.shape[1]) / 1e6
        n = int(self._per_mp() * mp)
        msg = "Full export: %d x %d (%.1f MP), about %s particles." % (
            self.full.shape[1], self.full.shape[0], mp, "{:,}".format(n))
        if self.pixel_size:
            msg += " Ground scale %.2f m/px." % self.pixel_size
        msg += ("\nPreview is %d px and single-threaded, so it shows the "
                "character of these settings rather than the exact result."
                % max(self.small.shape))
        self.lbl_info.setText(msg)

    def _per_mp(self):
        name = self.cmb_preset.currentText()
        if name in erosion.PRESETS:
            return erosion.PRESETS[name]['particles_per_mp']
        base = erosion.PRESETS['moderate']['particles_per_mp']
        return base * (self.sl_strength.value() / 100.0)

    def params(self):
        cfg = dict(erosion.DEFAULTS)
        cfg['erosion_coeff'] = self.sl_strength.value() / 100.0
        cfg['ttl'] = self.sl_detail.value()
        cfg['radius'] = self.sl_radius.value()
        cfg['deposition_coeff'] = self.sl_deposit.value() / 100.0
        return cfg

    def result_settings(self):
        """What the caller needs to run the real thing."""
        return {
            'params': self.params(),
            'per_mp': self._per_mp(),
            'protect': self.chk_protect.isChecked(),
            'parallel': self.chk_parallel.isChecked(),
        }

    def _build_protect_mask(self):
        """Corridor mask at preview scale from road/river centrelines."""
        self._protect = None
        if not self._protect_lines:
            return
        try:
            from scipy.ndimage import distance_transform_edt
        except ImportError:
            return
        scale = float(self.small.shape[0]) / max(1, self.full.shape[0])
        seeds = np.zeros(self.small.shape, bool)
        h, w = self.small.shape
        for line in self._protect_lines:
            pts = np.asarray(line, np.float64) * scale
            rr = np.clip(pts[:, 0].astype(int), 0, h - 1)
            cc = np.clip(pts[:, 1].astype(int), 0, w - 1)
            seeds[rr, cc] = True
        if not seeds.any():
            return
        px = (self.pixel_size or 1.0) / max(scale, 1e-9)
        radius_px = max(1.0, (self._protect_width_m * 0.5) / max(px, 1e-6))
        self._protect = distance_transform_edt(~seeds) <= radius_px

    def _run_preview(self):
        if self.worker and self.worker.isRunning():
            self._pending = True
            return
        cfg = self.params()
        mp = (self.small.shape[0] * self.small.shape[1]) / 1e6
        cfg['n_particles'] = max(2000, int(self._per_mp() * mp))
        self.bar.show()
        protect = self._protect if self.chk_protect.isChecked() else None
        self.worker = _Worker(self.small, cfg, self.pixel_size, protect)
        self.worker.done.connect(self._preview_ready)
        self.worker.failed.connect(self._preview_failed)
        self.worker.start()

    def _preview_ready(self, eroded):
        self.bar.hide()
        self.view.set_images(self.base_shade,
                             to_pixmap(hillshade(eroded, ref=self.small)))
        if self._pending:
            self._pending = False
            self._run_preview()

    def _preview_failed(self, msg):
        self.bar.hide()
        self.lbl_info.setText("Preview failed: %s" % msg)
