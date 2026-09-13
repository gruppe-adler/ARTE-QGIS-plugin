import os
import time
import gc
import re
import json
import numpy as np
import math
import configparser
import urllib.request
from PIL import Image
from osgeo import gdal, osr
from qgis.core import *
from qgis.utils import iface
from qgis.gui import QgsMapToolExtent, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QSize, pyqtSignal, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QIcon, QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QFormLayout, QDoubleSpinBox,
								 QSpinBox, QDialogButtonBox, QVBoxLayout, QMessageBox, QAction,
								 QLineEdit, QHBoxLayout, QPushButton, QFileDialog, QProgressDialog, QMenu,
								 QComboBox, QToolTip, QCheckBox, QLabel, QRadioButton, QButtonGroup, QTextEdit, QListWidget, QListWidgetItem, QInputDialog)

_plugin_dir = os.path.dirname(__file__)
_metadata_path = os.path.join(_plugin_dir, 'metadata.txt')
_sources_json_path = os.path.join(_plugin_dir, 'sources.json')

try:
	_config = configparser.ConfigParser()
	with open(_metadata_path, 'r', encoding='utf-8') as _f:
		_config.read_file(_f)
	VERSION = _config.get('general', 'version', fallback="1.0.0")
except Exception:
	VERSION = "1.0.0"

# --- Version & GitHub Info ---
GITHUB_USER = "Rendszerguru"
GITHUB_REPO = "ARTE-QGIS-plugin"

if hasattr(QMessageBox, 'StandardButton'):
	QM_Yes = QMessageBox.StandardButton.Yes
	QM_No = QMessageBox.StandardButton.No
	QM_Ok = QMessageBox.StandardButton.Ok
else:
	QM_Yes = QMessageBox.Yes
	QM_No = QMessageBox.No
	QM_Ok = QMessageBox.Ok

if hasattr(Qt, 'MouseButton'):
	Qt_LeftButton = Qt.MouseButton.LeftButton
	Qt_RightButton = Qt.MouseButton.RightButton
else:
	Qt_LeftButton = Qt.LeftButton
	Qt_RightButton = Qt.RightButton

if hasattr(Qt, 'CursorShape'):
	Cur_Cross = Qt.CursorShape.CrossCursor
	Cur_SizeAll = Qt.CursorShape.SizeAllCursor
	Cur_SizeHor = Qt.CursorShape.SizeHorCursor
	Cur_SizeVer = Qt.CursorShape.SizeVerCursor
	Cur_SizeFDiag = Qt.CursorShape.SizeFDiagCursor
	Cur_SizeBDiag = Qt.CursorShape.SizeBDiagCursor
	Cur_Arrow = Qt.CursorShape.ArrowCursor
else:
	Cur_Cross = Qt.CrossCursor
	Cur_SizeAll = Qt.SizeAllCursor
	Cur_SizeHor = Qt.SizeHorCursor
	Cur_SizeVer = Qt.SizeVerCursor
	Cur_SizeFDiag = Qt.SizeFDiagCursor
	Cur_SizeBDiag = Qt.SizeBDiagCursor
	Cur_Arrow = Qt.ArrowCursor

if hasattr(Qt, 'Key'):
	Key_Enter = Qt.Key.Key_Enter
	Key_Return = Qt.Key.Key_Return
else:
	Key_Enter = Qt.Key_Enter
	Key_Return = Qt.Key_Return

if hasattr(Qt, 'AlignmentFlag'):
	Qt_AlignCenter = Qt.AlignmentFlag.AlignCenter
else:
	Qt_AlignCenter = Qt.AlignCenter

# --- Default Sources ---
DEFAULT_SOURCES = [
	{
		"name": "AWS Terrarium (Free, Global 30m, Enhanced Smooth)",
		"url": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
		"type": "xyz",
		"format": "PNG",
		"decoder": "terrarium",
		"requires_api": False,
		"datasets": []
	},
	{
		"name": "Mapbox Terrain-RGB (API Key Required, Higher Detail)",
		"url": "https://api.mapbox.com/v4/mapbox.terrain-rgb/{z}/{x}/{y}.png?access_token={api_key}",
		"type": "xyz",
		"format": "PNG",
		"decoder": "mapbox",
		"requires_api": True,
		"datasets": []
	},
	{
		"name": "LiDAR (BEV / OpenTopography) - API Key Required",
		"url": "https://portal.opentopography.org/API/globaldem?demtype={ot_dem_type}&south={south}&north={north}&west={west}&east={east}&outputFormat=GTiff&API_Key={api_key}",
		"type": "bbox",
		"format": "GTiff",
		"decoder": "none",
		"requires_api": True,
		"datasets": [
			{"name": "COP30 (Best Global - Clean & Modern)", "value": "COP30"},
			{"name": "AW3D30 (Good Global - Detailed)", "value": "AW3D30"},
			{"name": "EU_DTM (Best for Europe - Bare Earth)", "value": "EU_DTM"},
			{"name": "NASADEM (Good Global Alternative)", "value": "NASADEM"},
			{"name": "SRTMGL1 (Classic NASA DEM)", "value": "SRTMGL1"}
		]
	}
]

# --- Map Tool classes ---
# Sentinel written into pixels the warp could not cover.
#
# gdal.Warp initialises its destination buffer before warping source pixels in,
# and with no dstNodata that init value is 0. Where the fetched DEM falls short
# of the requested outputBounds -- OpenTopography snaps to whole ~30 m source
# cells, so a request can come back a fraction of a cell small -- the margin is
# left at 0 and nothing records that the warp never touched it. Real terrain at
# 0 m is then indistinguishable from a void, which is why every downstream
# heuristic to spot it has failed: on a coastal map the two are genuinely the
# same number.
#
# -32768 is exactly representable in float32 (so equality is exact), far below
# any real elevation, and is already the value the existing `> -10000` tests
# were written to catch.
VOID_SENTINEL = -32768.0


def _raster_extent_4326(path):
	"""(west, south, east, north) in degrees for a raster on disk, or None.

	Read from the file's own GeoTransform and projection, which is what the
	export uses, so the preview cannot drift from it.
	"""
	try:
		from osgeo import gdal as _gdal, osr as _osr
		ds = _gdal.Open(path)
		if ds is None:
			return None
		gt = ds.GetGeoTransform()
		nx, ny = ds.RasterXSize, ds.RasterYSize
		wkt = ds.GetProjection()
		ds = None
		if not gt or not wkt:
			return None

		corners = [(gt[0] + gt[1] * x + gt[2] * y,
					gt[3] + gt[4] * x + gt[5] * y)
				   for x, y in ((0, 0), (nx, 0), (0, ny), (nx, ny))]

		src = _osr.SpatialReference()
		src.ImportFromWkt(wkt)
		dst = _osr.SpatialReference()
		dst.ImportFromEPSG(4326)
		try:
			# GDAL 3 honours the authority's axis order (lat, lon) unless told
			# otherwise; force lon/lat so the numbers mean what they read.
			src.SetAxisMappingStrategy(_osr.OAMS_TRADITIONAL_GIS_ORDER)
			dst.SetAxisMappingStrategy(_osr.OAMS_TRADITIONAL_GIS_ORDER)
		except AttributeError:
			pass
		tr = _osr.CoordinateTransformation(src, dst)

		lons, lats = [], []
		for mx, my in corners:
			lon, lat = tr.TransformPoint(mx, my)[:2]
			lons.append(lon)
			lats.append(lat)
		return (min(lons), min(lats), max(lons), max(lats))
	except Exception:
		return None


def _clip_polyline(pts, h, w, margin=2.0):
	"""Split a pixel polyline into the runs that lie inside the raster.

	Returns a list of arrays, each with at least two points. A road that leaves
	the map and comes back yields two separate lines rather than one with a
	straight chord joining the exit and entry points.

	`margin` keeps a little geometry just outside the frame so a corridor at the
	edge still feathers correctly instead of ending abruptly at the border.
	"""
	import numpy as _np
	if pts is None or len(pts) < 2:
		return []
	inside = ((pts[:, 0] >= -margin) & (pts[:, 0] <= h - 1 + margin) &
			  (pts[:, 1] >= -margin) & (pts[:, 1] <= w - 1 + margin))
	if inside.all():
		return [pts]
	out = []
	run = []
	for pt, ok in zip(pts, inside):
		if ok:
			run.append(pt)
		else:
			if len(run) >= 2:
				out.append(_np.array(run, dtype=float))
			run = []
	if len(run) >= 2:
		out.append(_np.array(run, dtype=float))
	return out


def _probe_nodata(path):
    """The NoData value a source raster declares, or None.

    Passed as srcNodata so the warper excludes flagged voids from the
    resampling kernel -- otherwise a wide Lanczos window straddling an SRTM
    hole smears the sentinel into its valid neighbours. GDAL accepts None and
    simply omits the option, so no branching is needed at the call site.
    """
    try:
        _ds = gdal.Open(path, gdal.GA_ReadOnly)
        if _ds is None:
            return None
        _nd = _ds.GetRasterBand(1).GetNoDataValue()
        _ds = None
        return _nd
    except Exception:
        return None


def _fill_voids_in_place(path, step_callback=None):
    """Replace void pixels in `path` with the nearest valid elevation.

    Run directly after the warp, before anything reads the raster. Voids are
    identified by the NoData value the warp recorded, so there is no threshold
    to guess. Nearest-valid replication continues the surrounding terrain
    outward rather than cutting a hole, which keeps the exported extent exactly
    what the user asked for.

    Returns the number of pixels filled, or 0 if there was nothing to do.
    """
    try:
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is None:
            return 0
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        arr = band.ReadAsArray()

        valid = arr > -10000.0
        if nd is not None and nd <= -10000.0:
            valid &= (arr != nd)

        n_void = int((~valid).sum())
        if n_void == 0 or not valid.any():
            ds = None
            return 0

        if step_callback:
            step_callback(84, "Filling %s void pixels..." % "{:,}".format(n_void))

        from scipy.ndimage import distance_transform_edt
        _, (iy, ix) = distance_transform_edt(~valid, return_indices=True)
        arr = np.where(valid, arr, arr[iy, ix])

        band.WriteArray(arr)
        # The raster no longer contains voids, so it must not advertise one --
        # a declared NoData on fully valid data invites downstream tools to
        # mask real terrain that happens to equal the sentinel.
        try:
            band.DeleteNoDataValue()
        except Exception:
            pass
        ds.FlushCache()
        ds = None

        QgsMessageLog.logMessage(
            "Filled %d void pixels by nearest-valid replication" % n_void,
            "ArmaTerrainExport", Qgis.Info)
        return n_void
    except Exception as exc:
        QgsMessageLog.logMessage(
            "Void fill skipped: %s" % exc, "ArmaTerrainExport", Qgis.Warning)
        return 0


def _find_heightmap(directory):
	"""Newest raw (un-eroded) heightmap export in `directory`, or None.

	"Raw" is the point: erosion and shaping are always tuned against the
	unmodified DEM export, never against a previous eroded result.

	ARTE also writes ``heightmap_diff_*.tif``, a debug raster holding only what
	the terrain engineer changed. It sorts after ``heightmap_<stamp>.png`` in a
	reverse listing, so a naive "first match" picks the diff and the preview
	shows roads floating on a blank field instead of terrain.

	The ``.tif`` is an *intermediate*: NoData is not filled until step 85, which
	runs after the terrain engineer. An export that is cancelled or killed in
	between leaves a .tif whose voids are still ~0 m against terrain at ~2465 m.
	That 2465 m cliff around all four edges dominates the hillshade contrast
	stretch and the preview renders as a flat grey sheet. So prefer the finished
	.png, and reject any candidate that still carries a NoData border.
	"""
	import os
	if not directory or not os.path.isdir(directory):
		return None
	cands = []
	for name in os.listdir(directory):
		low = name.lower()
		if not low.startswith("heightmap"):
			continue
		if not low.endswith((".png", ".tif", ".tiff")):
			continue
		if "_diff" in low or "debug" in low or "preview" in low:
			continue
		# Never tune against an already-eroded export. Both previews pick the
		# newest heightmap here, so without this a second tuning round would
		# erode terrain that has already been eroded, compounding each time
		# instead of always starting from the raw DEM.
		if "_eroded" in low:
			continue
		path = os.path.join(directory, name)
		try:
			# A finished .png outranks any .tif of the same vintage.
			cands.append((0 if low.endswith(".png") else 1,
						  -os.path.getmtime(path), path))
		except OSError:
			continue
	if not cands:
		return None
	cands.sort()

	for _kind, _age, path in cands:
		if not _has_nodata_border(path):
			return path
	# Everything looks unfinished; hand back the best-ranked one anyway so the
	# caller can still show something rather than failing outright.
	return cands[0][2]


def _has_nodata_border(path, threshold=0.005):
	"""True if a raster still has unfilled voids along its edges.

	Cheap check: read a coarse overview rather than the full 8192x8192 grid.
	"""
	try:
		import numpy as _np
		from osgeo import gdal as _gdal
		ds = _gdal.Open(path)
		if ds is None:
			return False
		band = ds.GetRasterBand(1)
		step = max(1, min(band.XSize, band.YSize) // 512)
		a = band.ReadAsArray(
			buf_xsize=max(1, band.XSize // step),
			buf_ysize=max(1, band.YSize // step)).astype('float32')
		ds = None
		if a.size == 0:
			return False
		hi = float(_np.percentile(a, 75))
		if hi <= 0:
			return False
		# Voids sit far below the working elevation range.
		void = a < (hi * 0.5)
		edges = _np.concatenate([void[0], void[-1], void[:, 0], void[:, -1]])
		return bool(edges.mean() > threshold)
	except Exception:
		return False


def _arte_import(name):
	"""Import a sibling module of this plugin.

	QGIS loads the plugin as a package, so a bare ``import erosion`` does not
	resolve -- the plugin directory is not on sys.path. Try the package-relative
	import first, then fall back to a direct one so the modules stay usable from
	the QGIS Python console and from tests.
	"""
	import importlib
	if __package__:
		try:
			return importlib.import_module("." + name, __package__)
		except ImportError:
			pass
	try:
		return importlib.import_module(name)
	except ImportError:
		import os, sys
		here = os.path.dirname(os.path.abspath(__file__))
		if here not in sys.path:
			sys.path.insert(0, here)
		return importlib.import_module(name)


class ArmaAdvancedMapTool(QgsMapTool):
	extentSelected = pyqtSignal(object)

	def __init__(self, canvas, dialog):
		super().__init__(canvas)
		self.canvas = canvas
		self.dialog = dialog

		self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
		self.rubber_band.setColor(QColor(255, 0, 0, 60))
		self.rubber_band.setStrokeColor(QColor(255, 0, 0, 255))

		self.rect = None
		self.drag_mode = None
		self.drag_start_pos = None
		self.drag_start_rect = None

		self.init_initial_extent()

	def init_initial_extent(self):
		center_x = self.dialog.sb_x.value()
		center_y = self.dialog.sb_y.value()
		size_w = self.dialog.sb_size_w.value()
		size_h = self.dialog.sb_size_h.value()

		crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
		canvas_crs = self.canvas.mapSettings().destinationCrs()
		transform = QgsCoordinateTransform(crs_4326, canvas_crs, QgsProject.instance().transformContext())

		try:
			pt_canvas = transform.transform(QgsPointXY(center_x, center_y))
		except:
			return

		lat_rad = math.radians(center_y)
		scale_factor = 1.0 / math.cos(lat_rad)
		size_w_mu = size_w * scale_factor
		size_h_mu = size_h * scale_factor
		half_w = size_w_mu / 2.0
		half_h = size_h_mu / 2.0

		self.rect = QgsRectangle(pt_canvas.x() - half_w, pt_canvas.y() - half_h, pt_canvas.x() + half_w, pt_canvas.y() + half_h)
		self.update_rubberband()

	def update_rubberband(self):
		if self.rect:
			self.rubber_band.setToGeometry(QgsGeometry.fromRect(self.rect), None)

	def get_drag_mode(self, pos):
		if not self.rect: return 'new'

		pt = self.toMapCoordinates(pos)
		tol = self.canvas.mapUnitsPerPixel() * 15

		left = abs(pt.x() - self.rect.xMinimum()) < tol
		right = abs(pt.x() - self.rect.xMaximum()) < tol
		top = abs(pt.y() - self.rect.yMaximum()) < tol
		bottom = abs(pt.y() - self.rect.yMinimum()) < tol

		if left and top: return 'resize_tl'
		if right and top: return 'resize_tr'
		if left and bottom: return 'resize_bl'
		if right and bottom: return 'resize_br'
		if left: return 'resize_l'
		if right: return 'resize_r'
		if top: return 'resize_t'
		if bottom: return 'resize_b'

		if self.rect.contains(pt):
			return 'move'

		return 'new'

	def show_size_tooltip(self, pos):
		if self.rect:
			try:
				center_pt = self.rect.center()
				crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
				canvas_crs = self.canvas.mapSettings().destinationCrs()
				transform_to_4326 = QgsCoordinateTransform(canvas_crs, crs_4326, QgsProject.instance().transformContext())

				center_4326 = transform_to_4326.transform(center_pt)
				lat_rad = math.radians(center_4326.y())
				scale_factor = 1.0 / math.cos(lat_rad)

				real_w = round(self.rect.width() / scale_factor)
				real_h = round(self.rect.height() / scale_factor)

				global_pos = self.canvas.mapToGlobal(pos)
				QToolTip.showText(global_pos, f"Keret: {real_w}m x {real_h}m")
			except:
				pass

	def canvasPressEvent(self, e):
		if e.button() == Qt_LeftButton:
			self.drag_mode = self.get_drag_mode(e.pos())
			self.drag_start_pos = self.toMapCoordinates(e.pos())
			if self.rect:
				self.drag_start_rect = QgsRectangle(self.rect)
			else:
				self.drag_start_rect = None

	def canvasMoveEvent(self, e):
		if not (e.buttons() & Qt_LeftButton) or not self.drag_start_pos:
			mode = self.get_drag_mode(e.pos())
			if mode in ('resize_tl', 'resize_br'):
				self.canvas.setCursor(Cur_SizeFDiag)
			elif mode in ('resize_tr', 'resize_bl'):
				self.canvas.setCursor(Cur_SizeBDiag)
			elif mode in ('resize_l', 'resize_r'):
				self.canvas.setCursor(Cur_SizeHor)
			elif mode in ('resize_t', 'resize_b'):
				self.canvas.setCursor(Cur_SizeVer)
			elif mode == 'move':
				self.canvas.setCursor(Cur_SizeAll)
			else:
				self.canvas.setCursor(Cur_Cross)

			if mode != 'new':
				self.show_size_tooltip(e.pos())
			else:
				QToolTip.hideText()
			return

		current_pt = self.toMapCoordinates(e.pos())
		dx = current_pt.x() - self.drag_start_pos.x()
		dy = current_pt.y() - self.drag_start_pos.y()

		is_square_locked = self.dialog.cb_size_lock.isChecked()

		if self.drag_mode == 'new':
			if is_square_locked:
				side = max(abs(dx), abs(dy))
				sign_x = 1 if dx > 0 else -1
				sign_y = 1 if dy > 0 else -1
				sq_pt = QgsPointXY(self.drag_start_pos.x() + side * sign_x, self.drag_start_pos.y() + side * sign_y)
				self.rect = QgsRectangle(self.drag_start_pos, sq_pt)
			else:
				self.rect = QgsRectangle(self.drag_start_pos, current_pt)

		elif self.drag_mode == 'move' and self.drag_start_rect:
			try:
				crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
				canvas_crs = self.canvas.mapSettings().destinationCrs()
				transform_to_4326 = QgsCoordinateTransform(canvas_crs, crs_4326, QgsProject.instance().transformContext())

				start_center = self.drag_start_rect.center()
				start_center_4326 = transform_to_4326.transform(start_center)
				start_scale_factor = 1.0 / math.cos(math.radians(start_center_4326.y()))

				real_w = self.drag_start_rect.width() / start_scale_factor
				real_h = self.drag_start_rect.height() / start_scale_factor

				new_center_pt = QgsPointXY(start_center.x() + dx, start_center.y() + dy)
				new_center_4326 = transform_to_4326.transform(new_center_pt)

				new_scale_factor = 1.0 / math.cos(math.radians(new_center_4326.y()))

				half_w = (real_w * new_scale_factor) / 2.0
				half_h = (real_h * new_scale_factor) / 2.0

				self.rect = QgsRectangle(
					new_center_pt.x() - half_w,
					new_center_pt.y() - half_h,
					new_center_pt.x() + half_w,
					new_center_pt.y() + half_h
				)
			except:
				self.rect = QgsRectangle(
					self.drag_start_rect.xMinimum() + dx,
					self.drag_start_rect.yMinimum() + dy,
					self.drag_start_rect.xMaximum() + dx,
					self.drag_start_rect.yMaximum() + dy
				)

		elif self.drag_start_rect:
			xmin = self.drag_start_rect.xMinimum()
			xmax = self.drag_start_rect.xMaximum()
			ymin = self.drag_start_rect.yMinimum()
			ymax = self.drag_start_rect.yMaximum()

			if 'l' in self.drag_mode: xmin += dx
			if 'r' in self.drag_mode: xmax += dx
			if 'b' in self.drag_mode: ymin += dy
			if 't' in self.drag_mode: ymax += dy

			if is_square_locked:
				w = abs(xmax - xmin)
				h = abs(ymax - ymin)
				side = max(w, h)

				if 'l' in self.drag_mode: xmin = xmax - side
				elif 'r' in self.drag_mode: xmax = xmin + side
				else:
					cx = (xmin + xmax) / 2
					xmin, xmax = cx - side/2, cx + side/2

				if 'b' in self.drag_mode: ymin = ymax - side
				elif 't' in self.drag_mode: ymax = ymin + side
				else:
					cy = (ymin + ymax) / 2
					ymin, ymax = cy - side/2, cy + side/2

			self.rect = QgsRectangle(min(xmin, xmax), min(ymin, ymax), max(xmin, xmax), max(ymin, ymax))

		self.update_rubberband()
		self.show_size_tooltip(e.pos())

	def canvasReleaseEvent(self, e):
		if e.button() == Qt_LeftButton:
			self.drag_mode = None
			self.drag_start_pos = None
		elif e.button() == Qt_RightButton:
			if self.rect:
				self.extentSelected.emit(self.rect)

	def keyPressEvent(self, e):
		if e.key() in (Key_Enter, Key_Return):
			if self.rect:
				self.extentSelected.emit(self.rect)

	def deactivate(self):
		self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
		self.canvas.setCursor(Cur_Arrow)
		QToolTip.hideText()
		super().deactivate()

# --- Custom Source Wizard Dialog ---
class CustomSourceBuilderDialog(QDialog):
	def __init__(self, source_data=None, parent=None):
		super().__init__(parent)
		self.setWindowTitle("⚙️ Data Source Configurator")
		self.resize(600, 650)

		self.datasets = []

		layout = QVBoxLayout(self)
		layout.setSpacing(12)

		# 0. Source Name
		layout.addWidget(QLabel("<b>0. Source Name (Display Label):</b>"))
		self.le_name = QLineEdit()
		self.le_name.setPlaceholderText("e.g. My Custom Topo Server")
		layout.addWidget(self.le_name)

		# 1. Base URL
		self.le_base_url = QLineEdit()
		self.le_base_url.setPlaceholderText("https://api.example.com/dem")
		layout.addWidget(QLabel("<b>1. Base API URL:</b>"))
		layout.addWidget(self.le_base_url)

		# 2. Coordinate System / Mode
		layout.addWidget(QLabel("<b>2. Coordinate / Tile System:</b>"))
		self.bg_mode = QButtonGroup(self)

		self.rb_bbox = QRadioButton("Bounding Box (?south={south}&north={north}...)")
		self.rb_xyz = QRadioButton("XYZ Tiles (/{z}/{x}/{y}.png)")
		self.rb_local = QRadioButton("Local File / Static URL")

		self.rb_bbox.setChecked(True)
		self.bg_mode.addButton(self.rb_bbox)
		self.bg_mode.addButton(self.rb_xyz)
		self.bg_mode.addButton(self.rb_local)

		layout.addWidget(self.rb_bbox)
		layout.addWidget(self.rb_xyz)
		layout.addWidget(self.rb_local)

		# --- Datasets List ---
		layout.addWidget(QLabel("<b>Datasets (Optional, use {ot_dem_type} in URL):</b>"))
		dataset_layout = QHBoxLayout()
		self.list_datasets = QListWidget()

		btn_ds_layout = QVBoxLayout()
		self.btn_add_ds = QPushButton("➕ Add")
		self.btn_edit_ds = QPushButton("✏️ Edit")
		self.btn_del_ds = QPushButton("🗑️ Del")

		self.btn_add_ds.clicked.connect(self.add_dataset)
		self.btn_edit_ds.clicked.connect(self.edit_dataset)
		self.btn_del_ds.clicked.connect(self.del_dataset)

		btn_ds_layout.addWidget(self.btn_add_ds)
		btn_ds_layout.addWidget(self.btn_edit_ds)
		btn_ds_layout.addWidget(self.btn_del_ds)
		btn_ds_layout.addStretch()

		dataset_layout.addWidget(self.list_datasets)
		dataset_layout.addLayout(btn_ds_layout)
		layout.addLayout(dataset_layout)

		# 3. Output Format
		layout.addWidget(QLabel("<b>3. Request Format:</b>"))
		self.cb_format = QComboBox()
		self.cb_format.addItems(["GTiff", "PNG", "ASC"])
		layout.addWidget(self.cb_format)

		# 4. Authentication
		layout.addWidget(QLabel("<b>4. Authentication:</b>"))
		api_layout = QHBoxLayout()
		self.chk_api = QCheckBox("Requires API Key (Appended via ?API_Key= or {api_key})")
		api_layout.addWidget(self.chk_api)
		layout.addLayout(api_layout)

		# 5. RGB Decoder
		layout.addWidget(QLabel("<b>5. RGB to Elevation Decoder:</b>"))
		self.cb_decoder = QComboBox()
		self.cb_decoder.addItem("None (Raw Float/GeoTIFF)", "none")
		self.cb_decoder.addItem("Terrarium Formula", "terrarium")
		self.cb_decoder.addItem("Mapbox Terrain-RGB Formula", "mapbox")
		layout.addWidget(self.cb_decoder)

		# 6. Live Preview
		layout.addWidget(QLabel("<b>Generated URL Preview:</b>"))
		self.te_preview = QTextEdit()
		self.te_preview.setReadOnly(True)
		self.te_preview.setMaximumHeight(60)
		self.te_preview.setStyleSheet("background-color: rgba(0,0,0,0.1); color: palette(text);")
		layout.addWidget(self.te_preview)

		# Buttons
		if hasattr(QDialogButtonBox, 'StandardButton'):
			ok_btn = QDialogButtonBox.StandardButton.Ok
			cancel_btn = QDialogButtonBox.StandardButton.Cancel
		else:
			ok_btn = QDialogButtonBox.Ok
			cancel_btn = QDialogButtonBox.Cancel

		buttons = QDialogButtonBox(ok_btn | cancel_btn)
		buttons.accepted.connect(self.accept)
		buttons.rejected.connect(self.reject)
		layout.addWidget(buttons)

		# Connect updates
		self.le_base_url.textChanged.connect(self.update_preview)
		self.rb_bbox.toggled.connect(self.update_preview)
		self.rb_xyz.toggled.connect(self.update_preview)
		self.rb_local.toggled.connect(self.update_preview)
		self.cb_format.currentTextChanged.connect(self.update_preview)
		self.chk_api.stateChanged.connect(self.update_preview)

		# Load existing data if editing
		if source_data:
			self.load_data(source_data)
		self.update_preview()

	def add_dataset(self):
		name, ok = QInputDialog.getText(self, "New Dataset", "Display Name (e.g. COP30 Global):")
		if ok and name:
			value, ok = QInputDialog.getText(self, "New Dataset", f"API Parameter Value for '{name}' (e.g. COP30):")
			if ok and value:
				self.datasets.append({"name": name, "value": value})
				self.refresh_dataset_list()

	def edit_dataset(self):
		row = self.list_datasets.currentRow()
		if row < 0: return
		ds = self.datasets[row]

		echo_mode = QLineEdit.EchoMode.Normal if hasattr(QLineEdit, 'EchoMode') else QLineEdit.Normal

		name, ok = QInputDialog.getText(self, "Edit Dataset", "Display Name:", echo_mode, ds["name"])
		if ok and name:
			value, ok = QInputDialog.getText(self, "Edit Dataset", "API Parameter Value:", echo_mode, ds["value"])
			if ok and value:
				self.datasets[row] = {"name": name, "value": value}
				self.refresh_dataset_list()

	def del_dataset(self):
		row = self.list_datasets.currentRow()
		if row >= 0:
			del self.datasets[row]
			self.refresh_dataset_list()

	def refresh_dataset_list(self):
		self.list_datasets.clear()
		for ds in self.datasets:
			self.list_datasets.addItem(f"{ds['name']} -> ({ds['value']})")
		self.update_preview()

	def update_preview(self):
		base = self.le_base_url.text().strip()
		if not base:
			self.te_preview.setText("")
			return

		url = base
		params = []

		if self.rb_xyz.isChecked():
			if not url.endswith("/"): url += "/"
			url += "{z}/{x}/{y}." + self.cb_format.currentText().lower()
			if self.chk_api.isChecked():
				params.append("access_token={api_key}")
		elif self.rb_bbox.isChecked():
			# Auto-inject dataset parameter if missing
			if self.datasets and "{dataset}" not in url and "{ot_dem_type}" not in url:
				if "opentopography" in url.lower():
					params.append("demtype={ot_dem_type}")
				else:
					params.append("dataset={ot_dem_type}")

			params.append("south={south}&north={north}&west={west}&east={east}")
			params.append(f"outputFormat={self.cb_format.currentText()}")
			if self.chk_api.isChecked():
				params.append("API_Key={api_key}")

		if params:
			separator = "?" if "?" not in url else "&"
			url += separator + "&".join(params)

		self.te_preview.setText(url)

	def load_data(self, data):
		self.le_name.setText(data.get("name", ""))
		url = data.get("url", "")

		# Set format
		fmt = data.get("format", "GTiff")
		idx_fmt = self.cb_format.findText(fmt)
		if idx_fmt >= 0:
			self.cb_format.setCurrentIndex(idx_fmt)

		# Set type
		if data.get("type") == "xyz":
			self.rb_xyz.setChecked(True)
			self.le_base_url.setText(url.split("/{z}")[0])
		elif data.get("type") in ["bbox", "opentopo"]:
			self.rb_bbox.setChecked(True)
			# Extract the exact base URL dynamically, leaving custom manual params intact
			base_url = url.split("&south=")[0].split("?south=")[0]
			self.le_base_url.setText(base_url)
		else:
			self.rb_local.setChecked(True)
			self.le_base_url.setText(url)

		self.chk_api.setChecked(data.get("requires_api", False))

		# Set datasets
		self.datasets = data.get("datasets", []).copy()
		self.refresh_dataset_list()

		# Set decoder
		idx = self.cb_decoder.findData(data.get("decoder", "none"))
		if idx >= 0:
			self.cb_decoder.setCurrentIndex(idx)

	def get_source_dict(self):
		source_type = "xyz"
		if self.rb_bbox.isChecked(): source_type = "bbox"
		elif self.rb_local.isChecked(): source_type = "local"

		return {
			"name": self.le_name.text().strip() or "Unnamed Source",
			"url": self.te_preview.toPlainText(),
			"type": source_type,
			"format": self.cb_format.currentText(),
			"decoder": self.cb_decoder.currentData(),
			"requires_api": self.chk_api.isChecked(),
			"datasets": self.datasets
		}

# --- Input Dialog Window ---
class CombinedArmaInputDialog(QDialog):
	def __init__(self, parent=None):
		super().__init__(parent if parent else iface.mainWindow())
		self.setWindowTitle(f"ARTE - Arma Reforger Terrain Exporter (v{VERSION})")
		self.resize(540, 720)

		self._updating = False
		self.sources_list = []

		plugin_dir = os.path.dirname(__file__)
		icon_path = os.path.join(plugin_dir, 'icon.png')
		if os.path.exists(icon_path):
			self.setWindowIcon(QIcon(icon_path))

		main_layout = QVBoxLayout(self)
		main_layout.setSpacing(10)
		main_layout.setContentsMargins(15, 15, 15, 15)

		self.btn_load_map = QPushButton("1. Load Satellite Preview")
		self.btn_load_map.clicked.connect(self.load_preview_map)

		self.btn_select_canvas = QPushButton("2. Select Extent on Map")
		self.btn_select_canvas.setStyleSheet("font-weight: bold;")

		existing_layers = QgsProject.instance().mapLayers().values()
		has_satellite_preview = any("Google Satellite" in layer.name() or "Preview" in layer.name() for layer in existing_layers)

		if has_satellite_preview:
			self.btn_select_canvas.setEnabled(True)
			self.btn_select_canvas.setToolTip("")
		else:
			self.btn_select_canvas.setEnabled(False)
			self.btn_select_canvas.setToolTip("Load the satellite map using button 1 first.")

		self.btn_select_canvas.clicked.connect(self.start_canvas_selection)

		map_layout = QHBoxLayout()
		map_layout.addWidget(self.btn_load_map)
		map_layout.addWidget(self.btn_select_canvas)

		settings = QgsSettings()
		saved_x = float(settings.value("ArmaReforgerTools/center_x", 15.256576))
		saved_y = float(settings.value("ArmaReforgerTools/center_y", 46.041305))
		saved_size_w = int(settings.value("ArmaReforgerTools/size_w", 12100))
		saved_size_h = int(settings.value("ArmaReforgerTools/size_h", 12100))
		saved_size_lock = settings.value("ArmaReforgerTools/size_lock", True, type=bool)

		saved_res_w = int(settings.value("ArmaReforgerTools/res_w", 4096))
		saved_res_h = int(settings.value("ArmaReforgerTools/res_h", 4096))
		saved_res_lock = settings.value("ArmaReforgerTools/res_lock", True, type=bool)

		saved_sat_res_w = int(settings.value("ArmaReforgerTools/sat_res_w", 4096))
		saved_sat_res_h = int(settings.value("ArmaReforgerTools/sat_res_h", 4096))
		saved_sat_res_lock = settings.value("ArmaReforgerTools/sat_res_lock", True, type=bool)

		saved_source = int(settings.value("ArmaReforgerTools/source", 0))
		saved_dataset = settings.value("ArmaReforgerTools/source_dataset", "")
		saved_format = int(settings.value("ArmaReforgerTools/format", 0))
		saved_path = settings.value("ArmaReforgerTools/output_path", "C:/QGIS/ArmaTerrainExport")
		saved_burn = settings.value("ArmaReforgerTools/burn_terrain", True, type=bool)
		saved_multiplier = float(settings.value("ArmaReforgerTools/engineer_multiplier", 1.15))
		saved_erosion = settings.value("ArmaReforgerTools/erosion", False, type=bool)
		saved_erosion_preset = settings.value("ArmaReforgerTools/erosion_preset", "moderate")

		self.sb_x = QDoubleSpinBox()
		self.sb_x.setRange(-180.0, 180.0)
		self.sb_x.setDecimals(6)
		self.sb_x.setValue(saved_x)
		self.sb_x.setSingleStep(0.01)

		self.sb_y = QDoubleSpinBox()
		self.sb_y.setRange(-90.0, 90.0)
		self.sb_y.setDecimals(6)
		self.sb_y.setValue(saved_y)
		self.sb_y.setSingleStep(0.01)

		self.sb_size_w = QSpinBox()
		self.sb_size_w.setRange(1, 1000000)
		self.sb_size_w.setValue(saved_size_w)
		self.sb_size_w.setSingleStep(100)

		self.sb_size_h = QSpinBox()
		self.sb_size_h.setRange(1, 1000000)
		self.sb_size_h.setValue(saved_size_h)
		self.sb_size_h.setSingleStep(100)

		self.cb_size_lock = QCheckBox()
		self.cb_size_lock.setToolTip("Lock Aspect Ratio to 1:1 (Square Selection / Size)")
		self.cb_size_lock.setChecked(saved_size_lock)
		self.cb_size_lock.stateChanged.connect(self.on_size_lock_toggled)

		self.sb_size_w.valueChanged.connect(self.on_size_w_changed)
		self.sb_size_h.valueChanged.connect(self.on_size_h_changed)

		size_layout = QHBoxLayout()
		size_layout.addWidget(self.sb_size_w)
		size_layout.addWidget(self.cb_size_lock)
		size_layout.addWidget(self.sb_size_h)

		# --- Heightmap Resolution ---
		self.sb_res_w = QSpinBox()
		self.sb_res_w.setRange(128, 32768)
		self.sb_res_w.setValue(saved_res_w)
		self.sb_res_w.setSingleStep(128)

		self.sb_res_h = QSpinBox()
		self.sb_res_h.setRange(128, 32768)
		self.sb_res_h.setValue(saved_res_h)
		self.sb_res_h.setSingleStep(128)

		self.cb_square_lock = QCheckBox()
		self.cb_square_lock.setToolTip("Lock Aspect Ratio to Match Physical Size")
		self.cb_square_lock.setChecked(saved_res_lock)
		self.cb_square_lock.stateChanged.connect(self.on_lock_toggled)

		self.sb_res_w.valueChanged.connect(self.on_res_w_changed)
		self.sb_res_h.valueChanged.connect(self.on_res_h_changed)

		res_layout = QHBoxLayout()
		res_layout.addWidget(self.sb_res_w)
		res_layout.addWidget(self.cb_square_lock)
		res_layout.addWidget(self.sb_res_h)

		self.lbl_heightmap_info = QLabel("<i>(Enfusion requirement: Power-of-two, e.g. 4096, 8192)</i>")
		self.lbl_heightmap_info.setStyleSheet("color: #7f8c8d; font-size: 10px;")

		# --- Satellite Resolution ---
		self.sb_sat_res_w = QSpinBox()
		self.sb_sat_res_w.setRange(128, 65536)
		self.sb_sat_res_w.setValue(saved_sat_res_w)
		self.sb_sat_res_w.setSingleStep(128)

		self.sb_sat_res_h = QSpinBox()
		self.sb_sat_res_h.setRange(128, 65536)
		self.sb_sat_res_h.setValue(saved_sat_res_h)
		self.sb_sat_res_h.setSingleStep(128)

		self.cb_sat_square_lock = QCheckBox()
		self.cb_sat_square_lock.setToolTip("Lock Aspect Ratio to Match Physical Size")
		self.cb_sat_square_lock.setChecked(saved_sat_res_lock)
		self.cb_sat_square_lock.stateChanged.connect(self.on_sat_lock_toggled)

		self.sb_sat_res_w.valueChanged.connect(self.on_sat_res_w_changed)
		self.sb_sat_res_h.valueChanged.connect(self.on_sat_res_h_changed)

		sat_res_layout = QHBoxLayout()
		sat_res_layout.addWidget(self.sb_sat_res_w)
		sat_res_layout.addWidget(self.cb_sat_square_lock)
		sat_res_layout.addWidget(self.sb_sat_res_h)

		self.lbl_chunks = QLabel("Calculating...")
		self.lbl_warnings = QLabel("OK")

		self.saved_api_keys = json.loads(settings.value("ArmaReforgerTools/api_keys", "{}"))

		# --- Source Manager UI ---
		self.cb_source = QComboBox()

		self.btn_add_source = QPushButton("➕")
		self.btn_add_source.setToolTip("Add New Source")
		self.btn_add_source.setFixedWidth(30)
		self.btn_add_source.clicked.connect(self.add_new_source)

		self.btn_edit_source = QPushButton("✏️")
		self.btn_edit_source.setToolTip("Edit Selected Source")
		self.btn_edit_source.setFixedWidth(30)
		self.btn_edit_source.clicked.connect(self.edit_current_source)

		self.btn_delete_source = QPushButton("🗑️")
		self.btn_delete_source.setToolTip("Delete Selected Source")
		self.btn_delete_source.setFixedWidth(30)
		self.btn_delete_source.clicked.connect(self.delete_current_source)

		source_manager_layout = QHBoxLayout()
		source_manager_layout.addWidget(self.cb_source)
		source_manager_layout.addWidget(self.btn_add_source)
		source_manager_layout.addWidget(self.btn_edit_source)
		source_manager_layout.addWidget(self.btn_delete_source)

		# --- Dynamic Dataset Selector ---
		self.lbl_dataset = QLabel("Source Dataset:")
		self.cb_dataset = QComboBox()
		self.lbl_dataset.hide()
		self.cb_dataset.hide()
		self.saved_dataset = saved_dataset

		self.lbl_apikey = QLabel("API Key:")
		self.le_apikey = QLineEdit()
		if hasattr(QLineEdit, 'EchoMode'):
			self.le_apikey.setEchoMode(QLineEdit.EchoMode.Password)
		else:
			self.le_apikey.setEchoMode(QLineEdit.Password)
		self.le_apikey.textChanged.connect(self.on_api_key_edited)

		self.cb_show_key = QCheckBox("Show")
		self.cb_show_key.stateChanged.connect(self.toggle_password_visibility)

		api_layout = QHBoxLayout()
		api_layout.addWidget(self.le_apikey)
		api_layout.addWidget(self.cb_show_key)

		self.cb_format = QComboBox()
		self.cb_format.addItems([
			"16-bit PNG + TXT (Recommended for Enfusion)",
			"Esri ASCII Grid (.asc)",
			"GeoTIFF (.tif) - Raw Float32 Elevation"
		])
		self.cb_format.setCurrentIndex(saved_format)

		self.cb_burn_terrain = QCheckBox("Smooth Roads && Carve Riverbeds (OSM)")
		self.cb_burn_terrain.setChecked(saved_burn)

		self.sb_multiplier = QDoubleSpinBox()
		self.sb_multiplier.setRange(0.5, 1.5)
		self.sb_multiplier.setDecimals(2)
		self.sb_multiplier.setValue(saved_multiplier)
		self.sb_multiplier.setSingleStep(0.05)
		self.sb_multiplier.setSuffix("x")
		self.sb_multiplier.setToolTip("Compensate for Enfusion smooth tool by over-widening roads/rails.")

		self.le_path = QLineEdit(saved_path)
		browse_layout = QHBoxLayout()
		browse_layout.addWidget(self.le_path)
		btn_browse = QPushButton("Browse")
		btn_browse.clicked.connect(self.browse_path)
		browse_layout.addWidget(btn_browse)

		from qgis.PyQt.QtWidgets import QGroupBox, QFrame

		groupbox_style = "QGroupBox { font-weight: bold; border: 1px solid #bdc3c7; border-radius: 5px; margin-top: 1ex; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }"

		box_area = QGroupBox("1. Location && Physical Size (Meters)")
		box_area.setStyleSheet(groupbox_style)
		layout_area = QFormLayout(box_area)
		layout_area.addRow("Map Tools:", map_layout)
		layout_area.addRow("Center X (Longitude):", self.sb_x)
		layout_area.addRow("Center Y (Latitude):", self.sb_y)
		layout_area.addRow("Size Width / Height:", size_layout)
		main_layout.addWidget(box_area)

		box_res = QGroupBox("2. Target Export Resolution (Pixels)")
		box_res.setStyleSheet(groupbox_style)
		layout_res = QFormLayout(box_res)
		layout_res.addRow("Satellite Res (px):", sat_res_layout)
		layout_res.addRow("Heightmap Res (px):", res_layout)
		layout_res.addRow("", self.lbl_heightmap_info)
		main_layout.addWidget(box_res)

		box_engine = QGroupBox("3. Enfusion Engine Validation")
		box_engine.setStyleSheet(groupbox_style + "QGroupBox { background-color: rgba(0, 0, 0, 0.2); }")
		layout_engine = QFormLayout(box_engine)
		layout_engine.addRow("Grid Chunks:", self.lbl_chunks)
		layout_engine.addRow("Status:", self.lbl_warnings)
		main_layout.addWidget(box_engine)

		box_source = QGroupBox("4. Elevation Source && Engineering")
		box_source.setStyleSheet(groupbox_style)
		layout_source = QFormLayout(box_source)
		layout_source.addRow("Elevation Data Source:", source_manager_layout)
		layout_source.addRow(self.lbl_dataset, self.cb_dataset)
		layout_source.addRow(self.lbl_apikey, api_layout)

		line = QFrame()
		line.setMinimumHeight(1)
		line.setFixedHeight(1)
		line.setStyleSheet("background-color: #bdc3c7; margin: 5px 0px;")
		layout_source.addRow(line)

		lbl_engineering = QLabel("⚙️ Post-Process Terrain Shaping")
		lbl_engineering.setStyleSheet("font-weight: bold; color: #2980b9;")
		layout_source.addRow(lbl_engineering)
		layout_source.addRow("Terrain Engineering:", self.cb_burn_terrain)
		layout_source.addRow("Engineering Multiplier:", self.sb_multiplier)

		# Kept as the carrier of the erosion preset, but the export buttons decide
		# whether erosion runs -- a checkbox and a button that both claim to
		# control it is exactly the ambiguity this UI had.
		self.cb_erosion = QCheckBox("Use erosion when pressing 'Export with erosion'")
		self.cb_erosion.setChecked(saved_erosion)
		self.cb_erosion.setToolTip(
			"Simulates water erosion to add drainage channels and gully detail.\n"
			"Useful when the source DEM is much coarser than the export: AW3D30\n"
			"is 30 m/px, so an 8192 px export over 2 km is mostly interpolation.")
		self.cmb_erosion = QComboBox()
		self.cmb_erosion.addItems(["subtle", "moderate", "strong"])
		self.cmb_erosion.setCurrentText(saved_erosion_preset)
		self.btn_erosion_preview = QPushButton("Preview / Tune...")
		self.btn_erosion_preview.setToolTip(
			"Run erosion on a downsampled copy and compare before/after "
			"before committing to a full export.")
		self.btn_erosion_preview.clicked.connect(self.open_erosion_preview)

		ero_row = QHBoxLayout()
		ero_row.addWidget(self.cmb_erosion, 1)
		ero_row.addWidget(self.btn_erosion_preview)
		layout_source.addRow("Erosion:", self.cb_erosion)
		layout_source.addRow("Erosion Strength:", ero_row)

		self.btn_roadwater_preview = QPushButton("Preview Roads & Rivers...")
		self.btn_roadwater_preview.setToolTip(
			"Inspect road cross-sections and river long profiles, and tune\n"
			"width, blend, gradient and depth before exporting.")
		self.btn_roadwater_preview.clicked.connect(self.open_roadwater_preview)
		layout_source.addRow("Shaping Preview:", self.btn_roadwater_preview)
		self.roadwater_settings = None

		self.erosion_settings = None
		main_layout.addWidget(box_source)

		box_output = QGroupBox("5. Output Settings")
		box_output.setStyleSheet(groupbox_style)
		layout_output = QFormLayout(box_output)
		layout_output.addRow("Heightmap Format:", self.cb_format)
		layout_output.addRow("Output Directory:", browse_layout)
		main_layout.addWidget(box_output)

		btn_save_layout = QHBoxLayout()
		self.btn_save_only = QPushButton("Save Settings")
		self.btn_save_only.clicked.connect(self.save_only)

		self.btn_reset = QPushButton("Reset to Defaults")
		self.btn_reset.clicked.connect(self.reset_settings)

		btn_save_layout.addWidget(self.btn_save_only)
		btn_save_layout.addWidget(self.btn_reset)

		main_layout.addStretch()
		main_layout.addLayout(btn_save_layout)

		if hasattr(QDialogButtonBox, 'StandardButton'):
			ok_btn = QDialogButtonBox.StandardButton.Ok
			cancel_btn = QDialogButtonBox.StandardButton.Cancel
		else:
			ok_btn = QDialogButtonBox.Ok
			cancel_btn = QDialogButtonBox.Cancel

		buttons = QDialogButtonBox(ok_btn | cancel_btn)
		buttons.rejected.connect(self.save_and_reject)

		# Two explicit export buttons rather than one Export plus a checkbox.
		# Whether erosion was going to run was previously only readable from a
		# tickbox halfway up the dialog, and an export with erosion takes
		# several minutes longer -- worth making unmistakable at the moment of
		# committing to it.
		self.btn_export_plain = buttons.button(ok_btn)
		self.btn_export_plain.setText("Export (no erosion)")
		self.btn_export_plain.setToolTip(
			"Heightmap, satmap and OSM road/river shaping.\n"
			"This is the file you tune erosion against.")
		self.btn_export_plain.clicked.connect(self._export_without_erosion)

		self.btn_export_eroded = buttons.addButton(
			"Export with erosion", QDialogButtonBox.ButtonRole.AcceptRole
			if hasattr(QDialogButtonBox, 'ButtonRole')
			else QDialogButtonBox.AcceptRole)
		self.btn_export_eroded.clicked.connect(self._export_with_erosion)

		main_layout.addWidget(buttons)

		self.setLayout(main_layout)

		self.load_sources()
		if 0 <= saved_source < len(self.sources_list):
			self.cb_source.setCurrentIndex(saved_source)

		self.export_mode = 'plain'
		self.le_path.textChanged.connect(lambda _=None: self._sync_export_buttons())
		self.cb_erosion.toggled.connect(lambda _=None: self._sync_export_buttons())
		self.cmb_erosion.currentTextChanged.connect(lambda _=None: self._sync_export_buttons())
		self._sync_export_buttons()
		self.cb_source.currentIndexChanged.connect(self.on_source_changed)
		self.on_source_changed(self.cb_source.currentIndex())
		self._update_all_from_size()

	# =========================================================================
	# JSON SOURCE MANAGER LOGIC
	# =========================================================================

	def load_sources(self):
		if os.path.exists(_sources_json_path):
			try:
				with open(_sources_json_path, 'r', encoding='utf-8') as f:
					self.sources_list = json.load(f)
			except Exception as e:
				self.sources_list = DEFAULT_SOURCES.copy()
		else:
			self.sources_list = DEFAULT_SOURCES.copy()
			self.save_sources()

		self.populate_sources()

	def save_sources(self):
		try:
			with open(_sources_json_path, 'w', encoding='utf-8') as f:
				json.dump(self.sources_list, f, indent=4)
		except:
			pass

	def populate_sources(self):
		saved_idx = self.cb_source.currentIndex()
		self.cb_source.blockSignals(True)
		self.cb_source.clear()
		for src in self.sources_list:
			self.cb_source.addItem(src.get("name", "Unnamed Source"))
		self.cb_source.blockSignals(False)

		if 0 <= saved_idx < len(self.sources_list):
			self.cb_source.setCurrentIndex(saved_idx)
		else:
			self.cb_source.setCurrentIndex(0)

	def add_new_source(self):
		dialog = CustomSourceBuilderDialog(parent=self)
		result = dialog.exec_() if hasattr(dialog, 'exec_') else dialog.exec()
		if result == 1 or (hasattr(QDialog, 'DialogCode') and result == QDialog.DialogCode.Accepted) or (hasattr(QDialog, 'Accepted') and result == QDialog.Accepted):
			new_src = dialog.get_source_dict()
			self.sources_list.append(new_src)
			self.save_sources()
			self.populate_sources()
			self.cb_source.setCurrentIndex(len(self.sources_list)-1)

	def edit_current_source(self):
		idx = self.cb_source.currentIndex()
		if idx < 0: return
		dialog = CustomSourceBuilderDialog(source_data=self.sources_list[idx], parent=self)
		result = dialog.exec_() if hasattr(dialog, 'exec_') else dialog.exec()
		if result == 1 or (hasattr(QDialog, 'DialogCode') and result == QDialog.DialogCode.Accepted) or (hasattr(QDialog, 'Accepted') and result == QDialog.Accepted):
			self.sources_list[idx] = dialog.get_source_dict()
			self.save_sources()
			self.populate_sources()
			self.on_source_changed(self.cb_source.currentIndex())

	def delete_current_source(self):
		idx = self.cb_source.currentIndex()
		if idx < 0: return
		reply = QMessageBox.question(self, "Delete Source", "Are you sure you want to delete this source?", QM_Yes | QM_No)
		if reply == QM_Yes:
			del self.sources_list[idx]
			if len(self.sources_list) == 0:
				self.sources_list = DEFAULT_SOURCES.copy()
			self.save_sources()
			self.populate_sources()
			self.on_source_changed(self.cb_source.currentIndex())

	# =========================================================================
	# HYBRID, LOOP-SAFE LOGIC HELPERS
	# =========================================================================

	def set_validation(self, state, text):
		if state == "ok":
			color = "#2ECC71"
			icon_text = f"✅ {text}"
		elif state == "warn":
			color = "#F39C12"
			icon_text = f"⚠️ {text}"
		else:
			color = "#E74C3C"
			icon_text = f"❌ {text}"

		self.lbl_warnings.setText(icon_text)
		self.lbl_warnings.setStyleSheet(f"font-weight: bold; color: {color};")

	def _safe_set(self, widget, value):
		if widget.value() == value:
			return
		widget.blockSignals(True)
		widget.setValue(value)
		widget.blockSignals(False)

	def _get_ratio_hw(self):
		w = max(1, self.sb_size_w.value())
		h = max(1, self.sb_size_h.value())
		return h / w

	def _get_ratio_wh(self):
		w = max(1, self.sb_size_w.value())
		h = max(1, self.sb_size_h.value())
		return w / h

	def _get_tile_size(self):
		return 512

	def _is_valid_resolution(self, value):
		if value <= 0:
			return False
		return (value & (value - 1)) == 0

	def _update_export_info(self):
		res_w = self.sb_res_w.value()
		res_h = self.sb_res_h.value()
		size_w = self.sb_size_w.value()

		tile = self._get_tile_size()
		chunks_x = max(1, int(res_w / tile))
		chunks_y = max(1, int(res_h / tile))

		grid_size_m = "N/A"
		if res_w > 0:
			grid_size_m = round((size_w / res_w) * tile, 1)

		errors = []
		warnings = []

		if not self._is_valid_resolution(res_w) or not self._is_valid_resolution(res_h):
			errors.append("Heightmap not Power-of-Two (e.g. 4096)!")
			self.lbl_chunks.setText(f"❌ Invalid chunks (not divisible by {tile})")
			self.lbl_chunks.setStyleSheet("color: #E74C3C;")
		else:
			self.lbl_chunks.setText(f"{chunks_x} x {chunks_y}  ({grid_size_m}m grid)")
			self.lbl_chunks.setStyleSheet("color: palette(text);")

		if res_w != res_h:
			warnings.append("Non-square resolution!")

		if errors:
			self.set_validation("error", " | ".join(errors))
		elif warnings:
			self.set_validation("warn", " | ".join(warnings))
		else:
			self.set_validation("ok", "Export Ready")

	def can_export(self):
		res_w = self.sb_res_w.value()
		res_h = self.sb_res_h.value()
		if not self._is_valid_resolution(res_w) or not self._is_valid_resolution(res_h):
			return False
		return True

	# =========================================================================
	# CORE SYNC ENGINE & UI EVENTS
	# =========================================================================

	def _update_all_from_size(self):
		ratio_hw = self._get_ratio_hw()

		if self.cb_square_lock.isChecked():
			new_res_h = int(round(self.sb_res_w.value() * ratio_hw))
			self._safe_set(self.sb_res_h, new_res_h)

		if self.cb_sat_square_lock.isChecked():
			new_sat_h = int(round(self.sb_sat_res_w.value() * ratio_hw))
			self._safe_set(self.sb_sat_res_h, new_sat_h)

		self._update_export_info()

	def on_size_w_changed(self, val):
		if self._updating:
			return
		self._updating = True

		if self.cb_size_lock.isChecked():
			self._safe_set(self.sb_size_h, val)

		self._update_all_from_size()
		self._updating = False

	def on_size_h_changed(self, val):
		if self._updating:
			return
		self._updating = True

		if self.cb_size_lock.isChecked():
			self._safe_set(self.sb_size_w, val)

		self._update_all_from_size()
		self._updating = False

	def on_size_lock_toggled(self, state):
		if self._updating:
			return
		self._updating = True

		if self.cb_size_lock.isChecked():
			self._safe_set(self.sb_size_h, self.sb_size_w.value())
			self._update_all_from_size()

		self._updating = False

	def on_res_w_changed(self, val):
		if self._updating:
			return
		self._updating = True

		if self.cb_square_lock.isChecked():
			ratio_hw = self._get_ratio_hw()
			new_val = int(round(val * ratio_hw))
			self._safe_set(self.sb_res_h, new_val)

		self._update_export_info()
		self._updating = False

	def on_res_h_changed(self, val):
		if self._updating:
			return
		self._updating = True

		if self.cb_square_lock.isChecked():
			ratio_wh = self._get_ratio_wh()
			new_val = int(round(val * ratio_wh))
			self._safe_set(self.sb_res_w, new_val)

		self._update_export_info()
		self._updating = False

	def on_lock_toggled(self, state):
		if self._updating:
			return
		self._updating = True

		if self.cb_square_lock.isChecked():
			self._update_all_from_size()

		self._updating = False

	def on_sat_res_w_changed(self, val):
		if self._updating:
			return
		self._updating = True

		if self.cb_sat_square_lock.isChecked():
			ratio_hw = self._get_ratio_hw()
			new_val = int(round(val * ratio_hw))
			self._safe_set(self.sb_sat_res_h, new_val)

		self._update_export_info()
		self._updating = False

	def on_sat_res_h_changed(self, val):
		if self._updating:
			return
		self._updating = True

		if self.cb_sat_square_lock.isChecked():
			ratio_wh = self._get_ratio_wh()
			new_val = int(round(val * ratio_wh))
			self._safe_set(self.sb_sat_res_w, new_val)

		self._update_export_info()
		self._updating = False

	def on_sat_lock_toggled(self, state):
		if self._updating:
			return
		self._updating = True

		if self.cb_sat_square_lock.isChecked():
			self._update_all_from_size()

		self._updating = False

	def save_settings(self):
		settings = QgsSettings()
		settings.setValue("ArmaReforgerTools/center_x", self.sb_x.value())
		settings.setValue("ArmaReforgerTools/center_y", self.sb_y.value())
		settings.setValue("ArmaReforgerTools/size_w", self.sb_size_w.value())
		settings.setValue("ArmaReforgerTools/size_h", self.sb_size_h.value())
		settings.setValue("ArmaReforgerTools/size_lock", self.cb_size_lock.isChecked())

		settings.setValue("ArmaReforgerTools/res_w", self.sb_res_w.value())
		settings.setValue("ArmaReforgerTools/res_h", self.sb_res_h.value())
		settings.setValue("ArmaReforgerTools/res_lock", self.cb_square_lock.isChecked())

		settings.setValue("ArmaReforgerTools/sat_res_w", self.sb_sat_res_w.value())
		settings.setValue("ArmaReforgerTools/sat_res_h", self.sb_sat_res_h.value())
		settings.setValue("ArmaReforgerTools/sat_res_lock", self.cb_sat_square_lock.isChecked())

		settings.setValue("ArmaReforgerTools/source", self.cb_source.currentIndex())
		if self.cb_dataset.count() > 0:
			settings.setValue("ArmaReforgerTools/source_dataset", self.cb_dataset.currentData())

		settings.setValue("ArmaReforgerTools/format", self.cb_format.currentIndex())
		settings.setValue("ArmaReforgerTools/burn_terrain", self.cb_burn_terrain.isChecked())
		settings.setValue("ArmaReforgerTools/erosion", self.cb_erosion.isChecked())
		settings.setValue("ArmaReforgerTools/erosion_preset", self.cmb_erosion.currentText())
		settings.setValue("ArmaReforgerTools/engineer_multiplier", self.sb_multiplier.value())
		settings.setValue("ArmaReforgerTools/output_path", self.le_path.text())
		settings.setValue("ArmaReforgerTools/api_keys", json.dumps(self.saved_api_keys))

	def save_only(self):
		self.save_settings()
		iface.messageBar().pushMessage("Settings", "Settings saved successfully.", level=Qgis.Success, duration=3)

	def reset_settings(self):
		self.sb_x.setValue(15.256576)
		self.sb_y.setValue(46.041305)
		self.sb_size_w.setValue(12100)
		self.sb_size_h.setValue(12100)
		self.cb_size_lock.setChecked(True)

		self.sb_res_w.setValue(4096)
		self.sb_res_h.setValue(4096)
		self.cb_square_lock.setChecked(True)

		self.sb_sat_res_w.setValue(4096)
		self.sb_sat_res_h.setValue(4096)
		self.cb_sat_square_lock.setChecked(True)

		self.cb_source.setCurrentIndex(0)
		self.cb_format.setCurrentIndex(0)
		self.cb_burn_terrain.setChecked(True)
		self.cb_erosion.setChecked(False)
		self.cmb_erosion.setCurrentText("moderate")
		self.sb_multiplier.setValue(1.10)
		self.le_path.setText("C:/QGIS/ArmaTerrainExport")
		iface.messageBar().pushMessage("Settings", "Reset to default values.", level=Qgis.Info, duration=3)

	def save_and_accept(self):
		self.save_settings()
		self.accept()

	def save_and_reject(self):
		self.save_settings()
		self.reject()

	def on_api_key_edited(self, text):
		idx = self.cb_source.currentIndex()
		if idx < 0 or idx >= len(self.sources_list): return
		src = self.sources_list[idx]
		src_name = src.get("name", "Unnamed Source")
		self.saved_api_keys[src_name] = text

	def on_source_changed(self, index):
		if not hasattr(self, 'sources_list') or index < 0 or index >= len(self.sources_list):
			return

		src = self.sources_list[index]

		self.le_apikey.hide()
		self.cb_show_key.hide()
		self.lbl_apikey.hide()
		self.lbl_dataset.hide()
		self.cb_dataset.hide()

		if src.get("requires_api", False):
			self.lbl_apikey.setText("API Key:")
			self.le_apikey.show()
			self.cb_show_key.show()
			self.lbl_apikey.show()

			src_name = src.get("name", "Unnamed Source")
			saved_key = self.saved_api_keys.get(src_name, "")
			self.le_apikey.setText(saved_key)

		self.cb_dataset.blockSignals(True)
		self.cb_dataset.clear()
		datasets = src.get("datasets", [])
		if datasets:
			for ds in datasets:
				self.cb_dataset.addItem(ds.get("name"), ds.get("value"))

			idx_ds = self.cb_dataset.findData(self.saved_dataset)
			if idx_ds != -1:
				self.cb_dataset.setCurrentIndex(idx_ds)

			self.lbl_dataset.show()
			self.cb_dataset.show()
		self.cb_dataset.blockSignals(False)

	def toggle_password_visibility(self, state=None):
		use_normal = QLineEdit.EchoMode.Normal if hasattr(QLineEdit, 'EchoMode') else QLineEdit.Normal
		use_password = QLineEdit.EchoMode.Password if hasattr(QLineEdit, 'EchoMode') else QLineEdit.Password

		if self.cb_show_key.isChecked():
			self.le_apikey.setEchoMode(use_normal)
		else:
			self.le_apikey.setEchoMode(use_password)

	def open_erosion_preview(self):
		"""Fetch the DEM for the current extent and open the tuning dialog.

		The preview needs real elevation data, so this performs the same
		download the export would. It is deliberately a button rather than
		automatic: the fetch costs time and bandwidth.
		"""
		from qgis.PyQt.QtWidgets import QMessageBox, QApplication
		from qgis.PyQt.QtCore import Qt as _Qt

		try:
			import numpy as _np
			from osgeo import gdal as _gdal
		except Exception as exc:
			QMessageBox.warning(self, "Erosion", "Could not load GDAL/numpy: %s" % exc)
			return

		path = self.le_path.text().strip()
		src = _find_heightmap(path)

		if not src:
			QMessageBox.information(
				self, "Erosion Preview",
				"No heightmap found in the output directory yet.\n\n"
				"Run an export once, then use this button to tune erosion "
				"against that heightmap and re-export.")
			return
		try:
			QApplication.setOverrideCursor(_Qt.WaitCursor if hasattr(_Qt, 'WaitCursor')
										   else _Qt.CursorShape.WaitCursor)
			ds = _gdal.Open(src)
			arr = ds.GetRasterBand(1).ReadAsArray().astype('float32')
			ds = None
		finally:
			QApplication.restoreOverrideCursor()

		try:
			erosion_preview = _arte_import('erosion_preview')
			pixel = None
			try:
				pixel = float(self.sb_size_w.value()) / float(self.sb_res_w.value())
			except Exception:
				pass
			# Hand the preview the same OSM centrelines the export will shape,
			# so 'preserve roads' has something to act on.
			protect_lines = self._osm_preview_lines(
				arr.shape, extent=_raster_extent_4326(src))
			dlg = erosion_preview.ErosionPreviewDialog(
				arr, pixel_size=pixel, parent=self, protect_lines=protect_lines)
			accepted = dlg.exec_() if hasattr(dlg, 'exec_') else dlg.exec()
			if accepted:
				self.erosion_settings = dlg.result_settings()
				self.cb_erosion.setChecked(True)
				self._sync_export_buttons()
		except Exception as exc:
			QMessageBox.warning(self, "Erosion Preview", "Preview failed: %s" % exc)

	def _osm_preview_lines(self, shape, extent=None):
		"""Road and waterway centrelines for the current extent, in pixel coords.

		Queries Overpass directly rather than reusing TerrainEngineer, which only
		runs inside a full export. Returns an empty list on any failure -- the
		preview is still useful without it, so a network problem must not raise.

		`extent` is (west, south, east, north) in degrees. Prefer the heightmap's
		own georeferencing when it has any, so the preview cannot drift from the
		export; fall back to the size spinboxes, which are ground metres (the
		export computes grid_cell_size as size_w / resolution_w from the same
		values).
		"""
		try:
			import json, urllib.request
			import numpy as _np

			if extent is not None:
				west, south, east, north = extent
			else:
				cx = float(self.sb_x.value())
				cy = float(self.sb_y.value())
				size_w = float(self.sb_size_w.value())
				size_h = float(self.sb_size_h.value())
				if size_w <= 0 or size_h <= 0:
					return []
				# size_w/size_h are ground metres: the export derives its own
				# grid_cell_size as size_w / resolution_w, and that matches the
				# cell size written to enfusion_import.txt. Do not rescale them.
				m_lat = 111132.92 - 559.82 * math.cos(2 * math.radians(cy))
				m_lon = 111412.84 * math.cos(math.radians(cy))
				if m_lat <= 0 or m_lon <= 0:
					return []
				dlat = (size_h / 2.0) / m_lat
				dlon = (size_w / 2.0) / m_lon
				south, north = cy - dlat, cy + dlat
				west, east = cx - dlon, cx + dlon
			if not (east > west and north > south):
				return []

			query = ('[out:json][timeout:45];('
					 'way["highway"](%f,%f,%f,%f);'
					 'way["railway"](%f,%f,%f,%f);'
					 'way["waterway"](%f,%f,%f,%f);'
					 ');out geom;') % (south, west, north, east,
									   south, west, north, east,
									   south, west, north, east)

			data = None
			for url in ("https://overpass-api.de/api/interpreter",
						"https://overpass.kumi.systems/api/interpreter"):
				try:
					req = urllib.request.Request(
						url, data=query.encode("utf-8"),
						headers={"User-Agent": "ARTE-QGIS-plugin"})
					with urllib.request.urlopen(req, timeout=50) as resp:
						data = json.loads(resp.read().decode("utf-8"))
					break
				except Exception:
					continue
			if not data:
				return []

			h, w = shape
			lines = []
			for el in data.get("elements", []):
				if el.get("type") != "way":
					continue
				geom = el.get("geometry")
				if not geom or len(geom) < 2:
					continue
				pts = []
				for node in geom:
					col = (node["lon"] - west) / (east - west) * (w - 1)
					row = (north - node["lat"]) / (north - south) * (h - 1)
					pts.append((row, col))
				# Overpass returns whole ways whenever any part intersects the
				# box, so most geometry runs well outside the raster -- 65% of
				# vertices on a real Bamiyan export. Those used to be clamped to
				# the border further down the pipeline, which pinned long runs of
				# points onto the edge and stamped dead-straight road ribbons
				# along it; a way leaving one edge and re-entering another drew a
				# false chord across the map. Split instead, and keep the pieces
				# that are actually inside.
				lines.extend(_clip_polyline(_np.array(pts, dtype=float), h, w))
			return lines
		except Exception:
			return []

	def _export_without_erosion(self):
		"""Export the base terrain. Erosion is skipped regardless of the tickbox."""
		self.export_mode = 'plain'
		self.save_and_accept()

	def _export_with_erosion(self):
		"""Export and then erode, using the tuned or preset settings."""
		self.export_mode = 'eroded'
		self.save_and_accept()

	def _sync_export_buttons(self):
		"""Enable the erosion export only once there is something to erode.

		Erosion runs on the exported heightmap, so it needs one to exist. With
		no output yet the button is disabled and says why, rather than starting
		a long run that has nothing to work from.
		"""
		btn = getattr(self, 'btn_export_eroded', None)
		if btn is None:
			return
		src = _find_heightmap(self.le_path.text().strip())
		tuned = getattr(self, 'erosion_settings', None)
		if not src:
			btn.setEnabled(False)
			btn.setToolTip(
				"No heightmap in the output directory yet.\n"
				"Run 'Export (no erosion)' first, then tune erosion against it.")
			return
		btn.setEnabled(True)
		if tuned:
			btn.setToolTip("Re-export and apply the erosion settings you tuned "
						   "in the preview.")
		else:
			btn.setToolTip(
				"Re-export and apply the '%s' erosion preset.\n"
				"Use Preview / Tune first if you want to see it before committing."
				% self.cmb_erosion.currentText())

	def open_roadwater_preview(self):
		"""Preview road/river shaping against the last exported heightmap.

		Uses OSM geometry for the current extent so the preview shapes the
		same features the export will.
		"""
		from qgis.PyQt.QtWidgets import QMessageBox
		try:
			import numpy as _np
			from osgeo import gdal as _gdal
		except Exception as exc:
			QMessageBox.warning(self, "Shaping", "Could not load GDAL/numpy: %s" % exc)
			return

		path = self.le_path.text().strip()
		src = _find_heightmap(path)
		if not src:
			QMessageBox.information(
				self, "Shaping Preview",
				"No heightmap found in the output directory yet.\n\n"
				"Run an export once, then use this button to tune shaping.")
			return

		try:
			ds = _gdal.Open(src)
			arr = ds.GetRasterBand(1).ReadAsArray().astype('float32')
			ds = None
			pixel = float(self.sb_size_w.value()) / float(self.sb_res_w.value())
		except Exception as exc:
			QMessageBox.warning(self, "Shaping Preview", "Could not read heightmap: %s" % exc)
			return

		# Prefer the real OSM geometry for this extent; fall back to a straight
		# demo line so the controls are still explorable offline.
		h, w = arr.shape
		lines = self._osm_preview_lines(arr.shape, extent=_raster_extent_4326(src))
		if lines:
			roads = lines
			rivers = []
		else:
			roads = [_np.array([[h * 0.5, float(c)] for c in range(0, w, 8)])]
			rivers = [_np.array([[float(r), w * 0.5] for r in range(0, h, 8)])]

		try:
			roadwater_preview = _arte_import('roadwater_preview')
			dlg = roadwater_preview.RoadWaterPreviewDialog(
				arr, roads=roads, rivers=rivers, pixel_size=pixel, parent=self)
			accepted = dlg.exec_() if hasattr(dlg, 'exec_') else dlg.exec()
			if accepted:
				self.roadwater_settings = dlg.settings()
		except Exception as exc:
			QMessageBox.warning(self, "Shaping Preview", "Preview failed: %s" % exc)

	def browse_path(self):
		directory = QFileDialog.getExistingDirectory(self, "Select Output Directory")
		if directory:
			self.le_path.setText(directory)

	def load_preview_map(self):
		canvas = iface.mapCanvas()

		crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
		QgsProject.instance().setCrs(crs_3857)

		layer_name = "Google Satellite Preview"
		layers = QgsProject.instance().mapLayersByName(layer_name)
		if not layers:
			uri = "type=xyz&url=https://mt1.google.com/vt/lyrs%3Ds%26x%3D%7Bx%7D%26y%3D%7By%7D%26z%3D%7Bz%7D&zmax=20&zmin=0"
			rlayer = QgsRasterLayer(uri, layer_name, "wms")
			if rlayer.isValid():
				QgsProject.instance().addMapLayer(rlayer)

		x_center = self.sb_x.value()
		y_center = self.sb_y.value()
		size_meters = max(self.sb_size_w.value(), self.sb_size_h.value())

		source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
		transform = QgsCoordinateTransform(source_crs, crs_3857, QgsProject.instance().transformContext())

		try:
			pt = transform.transform(QgsPointXY(x_center, y_center))
			half = (size_meters * 1.5) / 2
			rect = QgsRectangle(pt.x() - half, pt.y() - half, pt.x() + half, pt.y() + half)
			canvas.setExtent(rect)
			canvas.refresh()

			self.btn_select_canvas.setEnabled(True)
			self.btn_select_canvas.setToolTip("")

			iface.messageBar().pushMessage("Terrain Export", "Satellite map loaded and zoomed to coordinates.", level=Qgis.Success, duration=3)
			self.btn_load_map.setText("✔️ Satellite Loaded")
			self.btn_load_map.setStyleSheet("color: #27ae60; font-weight: bold;")
		except Exception as e:
			iface.messageBar().pushMessage("Terrain Export", f"Could not zoom: {e}", level=Qgis.Warning, duration=3)

	def start_canvas_selection(self):
		crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
		if QgsProject.instance().crs() != crs_3857:
			QgsProject.instance().setCrs(crs_3857)

		self.canvas = iface.mapCanvas()

		x_center = self.sb_x.value()
		y_center = self.sb_y.value()
		size_meters = max(self.sb_size_w.value(), self.sb_size_h.value())

		source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
		transform = QgsCoordinateTransform(source_crs, crs_3857, QgsProject.instance().transformContext())

		try:
			pt = transform.transform(QgsPointXY(x_center, y_center))
			half = (size_meters * 1.5) / 2
			rect = QgsRectangle(pt.x() - half, pt.y() - half, pt.x() + half, pt.y() + half)
			self.canvas.setExtent(rect)
			self.canvas.refresh()
		except Exception as e:
			pass

		self.hide()

		self.tool = ArmaAdvancedMapTool(self.canvas, self)
		self.tool.extentSelected.connect(self.on_extent_drawn)
		self.canvas.setMapTool(self.tool)

		iface.messageBar().pushMessage("Terrain Export", "The red frame shows the current selection. You can move or resize it, then RIGHT-CLICK or press ENTER to finish!", level=Qgis.Info, duration=8)

	def on_extent_drawn(self, extent):
		self.canvas.unsetMapTool(self.tool)
		self.show()

		canvas_crs = self.canvas.mapSettings().destinationCrs()
		crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
		crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")

		transform_to_4326 = QgsCoordinateTransform(canvas_crs, crs_4326, QgsProject.instance().transformContext())
		transform_to_3857 = QgsCoordinateTransform(canvas_crs, crs_3857, QgsProject.instance().transformContext())

		geom = QgsGeometry.fromRect(extent)

		geom_4326 = QgsGeometry(geom)
		geom_4326.transform(transform_to_4326)
		center = geom_4326.boundingBox().center()

		self.sb_x.setValue(center.x())
		self.sb_y.setValue(center.y())

		geom_3857 = QgsGeometry(geom)
		geom_3857.transform(transform_to_3857)
		box_3857 = geom_3857.boundingBox()

		lat_rad = math.radians(center.y())
		scale_factor = 1.0 / math.cos(lat_rad)
		base_size_w = box_3857.width() / scale_factor
		base_size_h = box_3857.height() / scale_factor

		self.sb_size_w.setValue(int(base_size_w))
		self.sb_size_h.setValue(int(base_size_h))
		iface.messageBar().pushMessage("Terrain Export", "Coordinates and size updated from map selection.", level=Qgis.Success, duration=3)

		self.btn_select_canvas.setText("✔️ Extent Selected")
		self.btn_select_canvas.setStyleSheet("color: #27ae60; font-weight: bold;")

# =========================================================================
# TERRAIN ENGINEERING (WITH DEBUG LOGGING, SAFE DYN WIDTH & POLYGONS)
# =========================================================================
class TerrainEngineer:
	def __init__(self, iface):
		self.iface = iface
		self.protect_mask = None

	def run(self, output_tif, output_dir, timestamp, xmin, ymin, xmax, ymax,
			resolution_w, resolution_h, target_crs, source_crs, context, pixel_size, step_callback, engineer_multiplier=1.10,
			write_diff_map=False):

		created_temp_files = []
		log_path = os.path.join(output_dir, f"engineer_debug_{timestamp}.txt")

		def debug_log(message):
			from qgis.core import QgsMessageLog, Qgis
			timestamp_str = time.strftime('%H:%M:%S')
			full_msg = f"[{timestamp_str}] {message}"
			QgsMessageLog.logMessage(full_msg, "ArmaTerrainExport", Qgis.Info)
			with open(log_path, "a", encoding="utf-8") as f:
				f.write(full_msg + "\n")

		try:
			import processing
			from qgis.core import QgsVectorLayer, QgsCoordinateTransform, QgsPointXY, QgsRectangle, QgsProperty
			from scipy.ndimage import distance_transform_edt, gaussian_filter

			debug_log("--- TERRAIN ENGINEER STARTED ---")
			debug_log(f"Resolution: {resolution_w}x{resolution_h}, Pixel size: {pixel_size:.2f}m")
			debug_log(f"Width Multiplier: {engineer_multiplier}x")

			step_callback(81, "Downloading OSM Engineering Vectors...")

			transform_to_4326_burn = QgsCoordinateTransform(target_crs, source_crs, context)
			pt_min_burn = transform_to_4326_burn.transform(QgsPointXY(xmin, ymin))
			pt_max_burn = transform_to_4326_burn.transform(QgsPointXY(xmax, ymax))

			south_b = min(pt_min_burn.y(), pt_max_burn.y())
			north_b = max(pt_min_burn.y(), pt_max_burn.y())
			west_b = min(pt_min_burn.x(), pt_max_burn.x())
			east_b = max(pt_min_burn.x(), pt_max_burn.x())

			overpass_urls = [
				"https://overpass-api.de/api/interpreter",
				"https://overpass.kumi.systems/api/interpreter",
				"https://lz4.overpass-api.de/api/interpreter"
			]

			overpass_query = f"""
			[out:json][timeout:120];
			(
			 way["highway"]({south_b},{west_b},{north_b},{east_b});
			 way["railway"]({south_b},{west_b},{north_b},{east_b});
			 way["waterway"]({south_b},{west_b},{north_b},{east_b});
			 way["natural"="water"]({south_b},{west_b},{north_b},{east_b});
			 way["water"]({south_b},{west_b},{north_b},{east_b});
			);
			(._;>;);
			out geom;
			"""

			debug_log("Fetching OSM data from Overpass API (with fallback endpoints)...")

			temp_geojson_lines = os.path.join(output_dir, f"temp_osm_lines_{timestamp}.geojson")
			temp_geojson_polys = os.path.join(output_dir, f"temp_osm_polys_{timestamp}.geojson")
			created_temp_files.extend([temp_geojson_lines, temp_geojson_polys])

			osm_data = None
			last_error = None

			for url in overpass_urls:
				try:
					debug_log(f"Trying: {url}")

					req = urllib.request.Request(
						url,
						data=overpass_query.encode('utf-8'),
						headers={
							'User-Agent': 'QGIS-Arma-Plugin/1.0',
							'Content-Type': 'application/x-www-form-urlencoded'
						}
					)

					with urllib.request.urlopen(req, timeout=90) as response:
						osm_data = response.read()

					if osm_data and len(osm_data) > 200:
						debug_log(f"SUCCESS: Data received from {url}")
						break

				except Exception as e:
					last_error = e
					debug_log(f"FAILED: {url} -> {e}")

			if osm_data is None:
				debug_log(f"CRITICAL: All Overpass endpoints failed: {last_error}")
				return

			try:
				data = json.loads(osm_data)
				features_lines = []
				features_polys = []

				custom_widths = 0
				custom_lanes = 0

				for el in data.get("elements", []):
					if el.get("type") == "way" and "geometry" in el:
						coords = [(p["lon"], p["lat"]) for p in el["geometry"]]

						tags = el.get("tags", {})
						dyn_width = -1.0

						try:
							if "width" in tags:
								match = re.search(r"([0-9]*\.?[0-9]+)", str(tags["width"]))
								if match:
									dyn_width = float(match.group(1))
									custom_widths += 1
									road_name = tags.get("name", "Unnamed road")
									debug_log(f"  -> DYNAMIC WIDTH: {road_name} ({tags['highway']}) = {dyn_width}m")

							elif "lanes" in tags:
								match = re.search(r"([0-9]*\.?[0-9]+)", str(tags["lanes"]))
								if match:
									dyn_width = float(match.group(1)) * 3.5
									custom_lanes += 1
									road_name = tags.get("name", "Unnamed road")
									debug_log(f"  -> DYNAMIC LANE-BASED: {road_name} ({tags['highway']}) = {dyn_width}m (based on lanes)")
						except Exception:
							pass

						tags["arte_dyn_width"] = dyn_width

						for field in ["highway", "railway", "waterway", "natural", "water", "bridge", "tunnel"]:
							if field not in tags:
								tags[field] = None

						is_closed = len(coords) >= 3 and coords[0] == coords[-1]
						is_water_poly = is_closed and (tags.get("natural") == "water" or tags.get("water") is not None or tags.get("waterway") in ["riverbank", "dock"])

						if is_water_poly:
							features_polys.append({
								"type": "Feature",
								"geometry": {
									"type": "Polygon",
									"coordinates": [coords]
								},
								"properties": tags
							})
						else:
							features_lines.append({
								"type": "Feature",
								"geometry": {
									"type": "LineString",
									"coordinates": coords
								},
								"properties": tags
							})

				water_polygons = len(features_polys)

				with open(temp_geojson_lines, 'w', encoding='utf-8') as f:
					json.dump({"type": "FeatureCollection", "features": features_lines}, f)

				with open(temp_geojson_polys, 'w', encoding='utf-8') as f:
					json.dump({"type": "FeatureCollection", "features": features_polys}, f)

				debug_log(f"Water Polygons Detected -> {water_polygons} areas converted successfully.")
				debug_log(f"OSM file saved: {len(features_lines)} lines, {water_polygons} polys.")
				debug_log(f"Extracted custom widths: {custom_widths}, Lane-based widths: {custom_lanes}")

			except Exception as e:
				debug_log(f"CRITICAL: Failed to process OSM data: {str(e)}")
				return

			vlayer_lines = QgsVectorLayer(temp_geojson_lines, "OSM_Lines", "ogr")
			vlayer_polys = QgsVectorLayer(temp_geojson_polys, "OSM_Polys", "ogr")

			if not vlayer_lines.isValid():
				debug_log("ERROR: OSM Vector layer is invalid! Skipping terrain engineering.")
			else:
				poly_count = vlayer_polys.featureCount() if vlayer_polys.isValid() else 0
				debug_log(f"SUCCESS: OSM data loaded. Total features found: {vlayer_lines.featureCount()} lines, {poly_count} polys.")
				step_callback(82, "Rasterizing core centerlines and buffers...")

				vlayer_lines = processing.run("native:reprojectlayer", {
					'INPUT': vlayer_lines,
					'TARGET_CRS': target_crs,
					'OUTPUT': 'TEMPORARY_OUTPUT'
				})['OUTPUT']

				if vlayer_polys.isValid() and vlayer_polys.featureCount() > 0:
					vlayer_polys = processing.run("native:reprojectlayer", {
						'INPUT': vlayer_polys,
						'TARGET_CRS': target_crs,
						'OUTPUT': 'TEMPORARY_OUTPUT'
					})['OUTPUT']
				else:
					vlayer_polys = None

				def extract_layer(source_layer, expr, name):
					if not source_layer or source_layer.featureCount() == 0:
						return None
					layer = processing.run("native:extractbyexpression", {
						'INPUT': source_layer,
						'EXPRESSION': expr,
						'OUTPUT': 'TEMPORARY_OUTPUT'
					})['OUTPUT']
					count = layer.featureCount() if layer else 0
					debug_log(f"  -> Extracted '{name}': {count} features matched.")
					return layer

				def create_buffer(layer, default_dist):
					if not layer or layer.featureCount() == 0:
						return None

					expr = f'CASE WHEN "arte_dyn_width" > 0 THEN min("arte_dyn_width" * {engineer_multiplier}, {default_dist * 1.5}) ELSE {default_dist} END'

					buf = processing.run("native:buffer", {
						'INPUT': layer,
						'DISTANCE': QgsProperty.fromExpression(expr),
						'SEGMENTS': 8,
						'DISSOLVE': True,
						'OUTPUT': 'TEMPORARY_OUTPUT'
					})['OUTPUT']

					min_width = pixel_size * 1.5

					buf_fixed = processing.run("native:buffer", {
						'INPUT': buf,
						'DISTANCE': max(0.0, min_width - default_dist),
						'SEGMENTS': 4,
						'DISSOLVE': True,
						'OUTPUT': 'TEMPORARY_OUTPUT'
					})['OUTPUT']

					return buf_fixed

				def create_mask_from_layer(layer, file_prefix, burn_val=1):
					if not layer or layer.featureCount() == 0:
						debug_log(f"    -> Mask '{file_prefix}' is EMPTY (0 features).")
						return np.zeros((resolution_h, resolution_w), dtype=bool)

					path = os.path.join(output_dir, f"temp_{file_prefix}_{timestamp}.tif")
					created_temp_files.append(path)

					if os.path.exists(path):
						try:
							os.remove(path)
						except:
							pass

					projwin = f"{xmin},{xmax},{ymin},{ymax}"

					result = processing.run("gdal:rasterize", {
						"INPUT": layer,
						"BURN": burn_val,
						"UNITS": 0,
						"WIDTH": resolution_w,
						"HEIGHT": resolution_h,
						"EXTENT": projwin,
						"NODATA": 0,
						"DATA_TYPE": 0,
						"INIT": 0,
						"OPTIONS": "COMPRESS=LZW",
						"EXTRA": "-at",
						"OUTPUT": path
					})

					# There used to be a `for _ in range(120): sleep(0.05)` poll
					# here waiting for this file. processing.run above is
					# synchronous -- it has already finished writing -- so the
					# wait was redundant, and `getsize() > 0` is not a valid
					# completeness test anyway because GDAL writes the TIFF
					# header before the raster body. In the one case it would
					# have mattered it froze the GUI for 6 s per mask, twelve
					# times over, with no way to cancel.
					if not os.path.exists(path):
						debug_log(f"    -> ERROR: Raster file was NOT created: {path}")
						debug_log(f"    -> GDAL result: {result}")
						return np.zeros((resolution_h, resolution_w), dtype=bool)

					ds = gdal.Open(path, gdal.GA_ReadOnly)
					if ds is None:
						debug_log(f"    -> ERROR: Failed to open raster '{file_prefix}'")
						return np.zeros((resolution_h, resolution_w), dtype=bool)

					arr = ds.GetRasterBand(1).ReadAsArray()
					ds = None

					if arr.shape != (resolution_h, resolution_w):
						debug_log(f"    -> WARNING: Shape mismatch {arr.shape} -> FIX")
						fixed = np.zeros((resolution_h, resolution_w), dtype=arr.dtype)
						h = min(resolution_h, arr.shape[0])
						w = min(resolution_w, arr.shape[1])
						fixed[:h, :w] = arr[:h, :w]
						arr = fixed

					mask = arr == burn_val
					debug_log(f"    -> Mask '{file_prefix}' generated. Active pixels: {np.sum(mask)}")
					# Twelve masks take roughly a second each. Tick the dialog after
					# each so the window stays responsive and cancellable instead of
					# going grey for the whole batch.
					mask_progress['done'] += 1
					step_callback(82, "Rasterizing OSM masks (%d/%d)..."
								  % (mask_progress['done'], mask_progress['total']))
					return mask

				mask_progress = {'done': 0, 'total': 12}

				no_bridge_tunnel = " AND (\"bridge\" IS NULL OR \"bridge\" NOT IN ('yes','true','1')) AND (\"tunnel\" IS NULL OR \"tunnel\" NOT IN ('yes','true','1'))"

				layer_heavy = extract_layer(vlayer_lines, f"\"highway\" IN ('motorway','trunk','primary','motorway_link','trunk_link','primary_link'){no_bridge_tunnel}", "Heavy Roads")
				layer_medium = extract_layer(vlayer_lines, f"\"highway\" IN ('secondary','tertiary','secondary_link','tertiary_link','residential','unclassified'){no_bridge_tunnel}", "Medium Roads")
				layer_light = extract_layer(vlayer_lines, f"\"highway\" IN ('track','path','footway','bridleway','cycleway','living_street','service'){no_bridge_tunnel}", "Light Roads")
				layer_rails = extract_layer(vlayer_lines, f"\"railway\" IS NOT NULL{no_bridge_tunnel}", "Railways")

				layer_water_lines = extract_layer(vlayer_lines, f"(\"waterway\" IS NOT NULL OR \"natural\" = 'water' OR \"water\" IS NOT NULL){no_bridge_tunnel}", "Waterways Lines")
				layer_water_polys = extract_layer(vlayer_polys, f"(\"waterway\" IS NOT NULL OR \"natural\" = 'water' OR \"water\" IS NOT NULL){no_bridge_tunnel}", "Waterways Polygons")

				BUFF_HEAVY = 12.0 * engineer_multiplier
				BUFF_MEDIUM = 6.5 * engineer_multiplier
				BUFF_LIGHT = 3.0 * engineer_multiplier
				BUFF_RAIL = 8.0 * engineer_multiplier
				BUFF_WATER = 12.0 * engineer_multiplier

				debug_log("Generating raster masks...")
				mask_heavy_b = create_mask_from_layer(create_buffer(layer_heavy, BUFF_HEAVY), "mask_h_b")
				mask_heavy_c = create_mask_from_layer(layer_heavy, "mask_h_c")

				mask_medium_b = create_mask_from_layer(create_buffer(layer_medium, BUFF_MEDIUM), "mask_m_b")
				mask_medium_c = create_mask_from_layer(layer_medium, "mask_m_c")

				mask_light_b = create_mask_from_layer(create_buffer(layer_light, BUFF_LIGHT), "mask_l_b")
				mask_light_c = create_mask_from_layer(layer_light, "mask_l_c")

				mask_rails_b = create_mask_from_layer(create_buffer(layer_rails, BUFF_RAIL), "mask_r_b")
				mask_rails_c = create_mask_from_layer(layer_rails, "mask_r_c")

				mask_w_lines_b = create_mask_from_layer(create_buffer(layer_water_lines, BUFF_WATER), "mask_w_lines_b")
				mask_w_lines_c = create_mask_from_layer(layer_water_lines, "mask_w_lines_c")

				mask_w_polys_c = create_mask_from_layer(layer_water_polys, "mask_w_polys_c")

				adaptive_buffer = max(2.0, pixel_size * 0.6)
				poly_buffered_layer = create_buffer(layer_water_polys, adaptive_buffer)
				mask_w_polys_b = create_mask_from_layer(poly_buffered_layer, "mask_w_polys_b")

				# create_mask_from_layer returns an all-zero array rather than
				# None when a layer has no features, so without this guard the
				# four passes below run on an empty mask -- measured 10.6 s of
				# pure waste at 8192x8192 on any map with no water polygons.
				if mask_w_polys_b.any():
					mask_w_polys_b = gaussian_filter(mask_w_polys_b.astype(np.float32), sigma=1.2) > 0.5

					from scipy.ndimage import binary_fill_holes, binary_opening, binary_closing
					mask_w_polys_b = binary_fill_holes(mask_w_polys_b)

					mask_w_polys_b = binary_opening(mask_w_polys_b, structure=np.ones((3,3)))
					mask_w_polys_b = binary_closing(mask_w_polys_b, structure=np.ones((3,3)))
				else:
					debug_log("Skipping water-polygon morphology (empty mask).")

				mask_water_c = np.logical_or(mask_w_lines_c, mask_w_polys_c)
				mask_water_b = np.logical_or(mask_w_lines_b, mask_w_polys_b)


				step_callback(83, "Applying Spline-based Cross-flat Engineering...")
				ds_elev = gdal.Open(output_tif, gdal.GA_Update)
				band_elev = ds_elev.GetRasterBand(1)
				elev_array = band_elev.ReadAsArray().astype(np.float32)

				# --- DATA-DRIVEN NODATA PROTECTION (SRTM / NASADEM FIX) ---
				nodata_value = band_elev.GetNoDataValue()
				is_nodata_present = False
				nodata_mask = None

				if nodata_value is not None or np.min(elev_array) < -10000:
					nodata_mask = (elev_array == nodata_value) | (elev_array < -10000)
					if np.any(nodata_mask):
						is_nodata_present = True
						safe_mean = np.mean(elev_array[~nodata_mask]) if np.any(~nodata_mask) else 0.0
						elev_array[nodata_mask] = safe_mean
						debug_log(f"Protected {np.sum(nodata_mask)} NoData pixels from math distortion.")
				# --------------------------------------------------------

				original_elev_array = elev_array.copy()

				def apply_flat_ribbon(current_z, center_mask, buffer_mask, sigma, falloff_dist, name):
					if not np.any(buffer_mask) or not np.any(center_mask):
						debug_log(f"Skipping ribbon '{name}' (Empty mask)")
						return current_z

					dist, indices = distance_transform_edt(~center_mask, return_indices=True)
					ribbon_z = current_z[indices[0], indices[1]]

					if sigma > 0:
						ribbon_z = gaussian_filter(ribbon_z, sigma=sigma)

					dist_inwards = distance_transform_edt(buffer_mask) * pixel_size
					falloff = np.clip(dist_inwards / falloff_dist, 0.0, 1.0)

					new_z = np.where(buffer_mask, (ribbon_z * falloff) + (current_z * (1.0 - falloff)), current_z)

					debug_log(f"Applied ribbon '{name}'. Modified pixels: {np.sum(new_z != current_z)}")
					return new_z

				# Per-feature shaping walks each polyline and flattens along its own
				# profile, which leaves no cross-slope. Fall back to the raster ribbon
				# if anything about the vector path fails, so a shaping problem never
				# costs the export.
				used_profile_roads = False
				try:
					roadwater = _arte_import('roadwater')
					gt = ds_elev.GetGeoTransform()
					inv_x = 1.0 / gt[1] if gt[1] else 0.0
					inv_y = 1.0 / gt[5] if gt[5] else 0.0

					def _to_px(mx, my):
						return ((my - gt[3]) * inv_y, (mx - gt[0]) * inv_x)

					road_specs = [
						(layer_light, BUFF_LIGHT, 2.0, 40.0, 0.12, "Light Roads"),
						(layer_medium, BUFF_MEDIUM, 4.0, 60.0, 0.10, "Medium Roads"),
						(layer_heavy, BUFF_HEAVY, 6.0, 90.0, 0.07, "Heavy Roads"),
						(layer_rails, BUFF_RAIL, 5.0, 150.0, 0.025, "Railways"),
					]
					shaped_any = False
					# Spread the four road classes across the 83-84 progress band so
					# the dialog keeps moving and stays cancellable. Shaping takes
					# tens of seconds; a static bar reads as a hang.
					for _si, (lyr, full_w, feather, smooth_m, grade, nm) in enumerate(road_specs):
						# BUFF_* are upstream's buffer distances -- full corridor
						# widths. Passing one straight in as half_width_m made every
						# road twice as wide as intended: a Heavy road came out
						# flattened 27.6 m across (39.6 m with the blend) where a
						# real primary road is about 7 m.
						half_w = full_w * 0.5
						lines = roadwater.extract_lines(lyr, _to_px,
														width_field="arte_dyn_width")
						if not lines:
							continue

						def _road_tick(done, total, _n=nm, _i=_si):
							step_callback(
								83, "Shaping %s (%d/%d)..." % (_n, done, total))

						elev_array = roadwater.apply_roads(
							elev_array, lines, pixel_size,
							half_width_m=half_w, feather_m=feather,
							smooth_m=smooth_m, max_grade=grade,
							log=lambda m, _n=nm: debug_log("  [%s] %s" % (_n, m)),
							progress=_road_tick,
							width_multiplier=engineer_multiplier)
						shaped_any = True
					used_profile_roads = shaped_any
				except MemoryError as _rw_mem:
					# Distinct from a routine fallback: running out of memory means the
					# fast path could not even be attempted at this resolution. Logging
					# it as an ordinary failure is how a 209 GiB allocation hid behind a
					# quiet 'using raster ribbons' line for as long as it did.
					debug_log("OUT OF MEMORY in per-feature road shaping (%s). "
							  "Falling back to raster ribbons -- roads will be canted. "
							  "Please report this with your export size." % _rw_mem)
					used_profile_roads = False
				except Exception as _rw_exc:
					debug_log("Per-feature road shaping failed (%s); using raster ribbons." % _rw_exc)
					used_profile_roads = False

				if not used_profile_roads:
					elev_array = apply_flat_ribbon(elev_array, mask_light_c, mask_light_b, sigma=1.0, falloff_dist=2.0, name="Light Roads")
					elev_array = apply_flat_ribbon(elev_array, mask_medium_c, mask_medium_b, sigma=2.5, falloff_dist=4.0, name="Medium Roads")
					elev_array = apply_flat_ribbon(elev_array, mask_heavy_c, mask_heavy_b, sigma=4.0, falloff_dist=6.0, name="Heavy Roads")
					elev_array = apply_flat_ribbon(elev_array, mask_rails_c, mask_rails_b, sigma=3.5, falloff_dist=5.0, name="Railways")

				# =========================================================
				# FULL GEOMORPH RIVER SYSTEM
				# =========================================================

				step_callback(84, "Geomorph river processing...")

				# Profile-based carving gives the bed a monotonic descent, so water
				# runs instead of pooling in a chain of ponds. Same fallback rule as
				# the roads: any failure drops through to the geomorph pass below.
				used_profile_rivers = False
				try:
					_rw = _arte_import('roadwater')
					_gt = ds_elev.GetGeoTransform()
					_ix = 1.0 / _gt[1] if _gt[1] else 0.0
					_iy = 1.0 / _gt[5] if _gt[5] else 0.0

					def _wpx(mx, my):
						return ((my - _gt[3]) * _iy, (mx - _gt[0]) * _ix)

					_wlines = _rw.extract_lines(layer_water_lines, _wpx)
					if _wlines:
						def _river_tick(done, total):
							step_callback(84, "Carving rivers (%d/%d)..." % (done, total))

						elev_array = _rw.apply_rivers(
							elev_array, _wlines, pixel_size,
							half_width_m=max(2.0, BUFF_WATER * 0.5),
							feather_m=max(3.0, BUFF_WATER),
							depth_m=1.5,
							log=lambda m: debug_log("  [Rivers] %s" % m),
							progress=_river_tick)
						used_profile_rivers = True
				except MemoryError as _rv_mem:
					debug_log("OUT OF MEMORY in per-feature river carving (%s). "
							  "Falling back to the geomorph pass. "
							  "Please report this with your export size." % _rv_mem)
					used_profile_rivers = False
				except Exception as _rv_exc:
					debug_log("Per-feature river carving failed (%s); using geomorph pass." % _rv_exc)
					used_profile_rivers = False

				if used_profile_rivers:
					debug_log("Rivers carved from profiles; skipping geomorph pass.")
					if np.any(mask_water_b):
						_road_mask = mask_light_b | mask_medium_b | mask_heavy_b | mask_rails_b
						self.protect_mask = _road_mask | mask_water_b
				elif np.any(mask_water_b):

					if not np.any(mask_water_c):
						mask_water_c = mask_water_b

					stable_ground = gaussian_filter(original_elev_array, sigma=1.0)

					dist_c, indices = distance_transform_edt(~mask_water_c, return_indices=True)
					meder_z = elev_array[indices[0], indices[1]]

					meder_z = gaussian_filter(meder_z, sigma=2.0)

					local_ground = gaussian_filter(stable_ground, sigma=3.0)

					depth = local_ground - meder_z

					river_extent = depth < 1.5

					river_extent = gaussian_filter(river_extent.astype(np.float32), sigma=2.0)
					river_extent = river_extent > 0.55

					river_extent = gaussian_filter(river_extent.astype(np.float32), sigma=1.0) > 0.5

					dist_real = distance_transform_edt(~river_extent) * pixel_size

					width_norm = np.clip(dist_real / (BUFF_WATER * 0.5), 0.0, 1.0)

					width_norm = gaussian_filter(width_norm, sigma=1.0)

					water_surface = gaussian_filter(meder_z, sigma=4.0)

					water_target = water_surface - 0.7

					water_target = np.maximum(water_target, stable_ground - 0.7)

					water_target = np.minimum(
						water_target,
						local_ground + (1.0 - width_norm) * 0.35
					)

					edge_limit = local_ground + 0.15
					water_target = np.minimum(water_target, edge_limit)

					try:
						gy, gx = np.gradient(meder_z)
						flow_dir = gaussian_filter(gy, sigma=1.0)

						water_target -= flow_dir * 0.12
					except Exception:
						pass

					water_target = gaussian_filter(water_target, sigma=0.8)

					dist_inwards = distance_transform_edt(mask_water_b) * pixel_size

					falloff = np.clip(dist_inwards / (BUFF_WATER * 0.5), 0.0, 1.0)

					falloff = gaussian_filter(falloff, sigma=0.8)

					new_elev_array = np.where(
						mask_water_b,
						(water_target * falloff) + (elev_array * (1.0 - falloff)),
						elev_array
					)

					elev_array = new_elev_array

					debug_log(f"Geomorph river applied (ULTRA STABLE). Pixels: {np.sum(mask_water_b)}")

					# -----------------------------------------------------
					# ROAD PROTECTION
					# -----------------------------------------------------
					debug_log("Applying road-water safety (ultra stable)...")

					road_mask = mask_light_b | mask_medium_b | mask_heavy_b | mask_rails_b

					# Hand the shaped areas to the erosion pass so droplets
					# cannot chew through a flattened road or a carved riverbed.
					self.protect_mask = road_mask | mask_water_b

					before = elev_array.copy()

					safe_water = water_target + 0.05

					elev_array[road_mask] = np.maximum(
						elev_array[road_mask],
						safe_water[road_mask]
					)

					debug_log(f"Road protection modified: {np.sum(before != elev_array)}")

				else:
					debug_log("Skipping Geomorph Rivers (Empty mask)")

				debug_log("Saving modified terrain to TIF...")

				# --- RESTORE NODATA & UPDATE SCALING STATISTICS ---
				if is_nodata_present:
					elev_array[nodata_mask] = nodata_value if nodata_value is not None else -32768

				band_elev = ds_elev.GetRasterBand(1)
				band_elev.WriteArray(elev_array)

				if nodata_value is not None:
					band_elev.SetNoDataValue(nodata_value)

				band_elev.ComputeStatistics(False)
				# ---------------------------------------------------------------

				# The difference map is a debug artefact: nothing reads it back
				# and nothing deletes it. It also caused real trouble -- both
				# preview dialogs used to pick heightmap_diff_*.tif instead of
				# the actual heightmap, showing roads on a blank field. Write it
				# only when asked, and compress it when we do (it was 256 MB
				# uncompressed per export).
				if write_diff_map:
					debug_log("Generating difference map...")
					diff_array = elev_array - original_elev_array
					diff_tif_path = os.path.join(output_dir, f"heightmap_diff_{timestamp}.tif")

					driver = gdal.GetDriverByName("GTiff")
					ds_diff = driver.Create(diff_tif_path, resolution_w, resolution_h, 1,
											gdal.GDT_Float32, options=["COMPRESS=LZW"])
					ds_diff.SetGeoTransform(ds_elev.GetGeoTransform())
					ds_diff.SetProjection(ds_elev.GetProjection())
					ds_diff.GetRasterBand(1).WriteArray(diff_array)
					ds_diff.FlushCache()
					ds_diff = None
					debug_log(f"Difference map saved to: {diff_tif_path}")

				ds_elev.FlushCache()
				ds_elev = None

				debug_log("--- TERRAIN ENGINEER FINISHED ---")

		except Exception as e:
			from qgis.core import Qgis
			error_msg = f"Terrain smoothing failed with Exception: {e}"
			debug_log(f"CRITICAL ERROR: {error_msg}")
			self.iface.messageBar().pushMessage(
				"Engineering Warn",
				error_msg,
				level=Qgis.Warning,
				duration=5
			)

		finally:
			for temp_file in created_temp_files:
				try:
					if os.path.exists(temp_file):
						os.remove(temp_file)
				except:
					pass

# --- Export classes ---
class ArmaExportPlugin:
	def __init__(self, iface):
		self.iface = iface
		self.action = None
		self.custom_menu = None
		self.plugin_dir = os.path.dirname(__file__)

	def initGui(self):
		if globals().get('GITHUB_USER') and globals().get('GITHUB_REPO') and "YOU_REPOD" not in GITHUB_REPO:
			self.register_github_repository()

		icon_path = os.path.join(self.plugin_dir, 'icon.png')
		if os.path.exists(icon_path):
			self.action = QAction(QIcon(icon_path), "ARTE - Arma Reforger Terrain Exporter", self.iface.mainWindow())
		else:
			self.action = QAction("ARTE - Arma Reforger Terrain Exporter", self.iface.mainWindow())

		self.action.triggered.connect(self.run)
		self.iface.addRasterToolBarIcon(self.action)

		menu_bar = self.iface.mainWindow().menuBar()
		self.custom_menu = menu_bar.findChild(QMenu, "ArmaToolsMenu")
		if not self.custom_menu:
			self.custom_menu = QMenu("Arma Tools (ARTE)", menu_bar)
			self.custom_menu.setObjectName("ArmaToolsMenu")
			menu_bar.addMenu(self.custom_menu)

		self.custom_menu.addAction(self.action)

	def register_github_repository(self):
		try:
			from qgis.core import QgsSettings
			settings = QgsSettings()

			settings.beginGroup("app/plugin_repositories")
			repos = settings.childGroups()
			settings.endGroup()

			repo_name = "Arma Reforger ARTE Repository"
			repo_url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/plugins.xml?v={VERSION}"

			already_exists = False

			for r in repos:
				if r.startswith("arma_auto_repo"):
					url = settings.value(f"app/plugin_repositories/{r}/url")

					if url == repo_url and "YOU_REPOD" not in repo_url:
						already_exists = True

			if not already_exists and globals().get('GITHUB_USER') and globals().get('GITHUB_REPO') and "YOU_REPOD" not in GITHUB_REPO:
				internal_id = "arma_auto_repo"

				settings.setValue(f"app/plugin_repositories/{internal_id}/name", repo_name)
				settings.setValue(f"app/plugin_repositories/{internal_id}/url", repo_url)
				settings.setValue(f"app/plugin_repositories/{internal_id}/enabled", True)
				settings.setValue(f"app/plugin_repositories/{internal_id}/valid", True)

				self.iface.messageBar().pushMessage(
					"Repository Config",
					"Arma Official Update Stream has been automatically linked!",
					level=Qgis.Success,
					duration=4
				)
		except Exception:
			pass

	def unload(self):
		if self.action:
			self.iface.removeRasterToolBarIcon(self.action)
			if self.custom_menu:
				self.custom_menu.removeAction(self.action)
				if not self.custom_menu.actions():
					menu_bar = self.iface.mainWindow().menuBar()
					menu_bar.removeAction(self.custom_menu.menuAction())

	def check_for_updates(self):
		try:
			if not globals().get('GITHUB_USER') or not globals().get('GITHUB_REPO') or "YOU_REPOD" in GITHUB_REPO:
				return

			url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/latest"
			req = urllib.request.Request(url, headers={'User-Agent': 'QGIS-Arma-Plugin-Updater'})

			with urllib.request.urlopen(req, timeout=2.0) as response:
				data = json.loads(response.read().decode('utf-8'))
				latest_version = data['tag_name'].replace('v', '')
				release_url = data['html_url']

				current_v = tuple(map(int, VERSION.split('.')))
				latest_v = tuple(map(int, latest_version.split('.')))

				if latest_v > current_v:
					msg = QMessageBox()
					msg.setIcon(QMessageBox.Information if not hasattr(QMessageBox, 'StandardButton') else QMessageBox.Information)
					msg.setWindowTitle("ARTE - Update Available")
					msg.setText(f"A new version of ARTE (Arma Reforger Terrain Exporter) is available!\n\nCurrent: v{VERSION}\nNew: v{latest_version}")
					msg.setInformativeText("Would you like to open the GitHub download page?")
					msg.setStandardButtons(QM_Yes | QM_No)

					if msg.exec() == QM_Yes:
						QDesktopServices.openUrl(QUrl(release_url))
		except Exception:
			pass

	def run(self):
		self.check_for_updates()

		self.dialog = CombinedArmaInputDialog(self.iface.mainWindow())
		self.dialog.accepted.connect(lambda: self.execute_export(self.dialog))

		from qgis.core import QgsProject

		for layer in QgsProject.instance().mapLayers().values():
			if "Google_Sat" in layer.name() or "Preview" in layer.name():
				self.dialog.btn_load_map.setText("✔️ Satellite Loaded")
				self.dialog.btn_load_map.setStyleSheet("color: #27ae60; font-weight: bold;")
				break

		self.dialog.show()

	def execute_export(self, dialog):
		from qgis.core import QgsSettings, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsPointXY, QgsRectangle, QgsRasterLayer, QgsMapSettings, QgsMapRendererSequentialJob

		x_center = dialog.sb_x.value()
		y_center = dialog.sb_y.value()
		size_w = dialog.sb_size_w.value()
		size_h = dialog.sb_size_h.value()

		resolution_w = dialog.sb_res_w.value()
		resolution_h = dialog.sb_res_h.value()
		sat_res_w = dialog.sb_sat_res_w.value()
		sat_res_h = dialog.sb_sat_res_h.value()

		output_dir = dialog.le_path.text()
		format_index = dialog.cb_format.currentIndex()
		want_burn = dialog.cb_burn_terrain.isChecked() if hasattr(dialog, 'cb_burn_terrain') else False

		# Erosion settings: an explicit Preview/Tune session wins, otherwise
		# fall back to the preset chosen in the combo.
		# The button the user pressed is the authority: "Export (no erosion)"
		# never erodes, whatever the tickbox says.
		self._erosion_settings = None
		_mode = getattr(dialog, 'export_mode', 'plain')
		if _mode != 'eroded':
			QgsMessageLog.logMessage(
				"Export without erosion (base terrain + OSM shaping).",
				"ArmaTerrainExport", Qgis.Info)
		elif getattr(dialog, 'cb_erosion', None) is not None:
			tuned = getattr(dialog, 'erosion_settings', None)
			if tuned:
				self._erosion_settings = tuned
			else:
				try:
					_ero = _arte_import('erosion')
					name = dialog.cmb_erosion.currentText()
					pre = _ero.PRESETS.get(name, _ero.PRESETS['moderate'])
					params = dict(_ero.DEFAULTS)
					params['erosion_coeff'] = pre['erosion_coeff']
					params['ttl'] = pre['ttl']
					self._erosion_settings = {
						'params': params,
						'per_mp': pre['particles_per_mp'],
						'protect': True,
						'parallel': True,
					}
				except Exception as _ero_exc:
					QgsMessageLog.logMessage('Erosion unavailable: %s' % _ero_exc,
											 'ArmaTerrainExport', Qgis.Warning)

		api_key = dialog.le_apikey.text().strip()
		ot_dem_type = dialog.cb_dataset.currentData() if dialog.cb_dataset.count() > 0 else ""
		if not ot_dem_type:
			ot_dem_type = ""

		# --- SOURCE MANAGER VARIABLES ---
		if len(dialog.sources_list) == 0:
			QMessageBox.warning(self.iface.mainWindow(), "Warning", "No source selected!")
			return

		current_source = dialog.sources_list[dialog.cb_source.currentIndex()]
		source_type = current_source.get("type", "xyz")
		decoder_type = current_source.get("decoder", "none")
		template_url = current_source.get("url", "")
		requires_api = current_source.get("requires_api", False)

		settings = QgsSettings()

		# --- API KEY SAVE LOGIC ---
		src_name = current_source.get("name", "Unnamed Source")
		dialog.saved_api_keys[src_name] = api_key
		settings.setValue("ArmaReforgerTools/api_keys", json.dumps(dialog.saved_api_keys))

		if requires_api and not api_key:
			QMessageBox.warning(self.iface.mainWindow(), "Warning", "An API key is required to use the selected source!")
			return

		if not template_url:
			QMessageBox.warning(self.iface.mainWindow(), "Warning", "A valid URL is required for this Source!")
			return

		if source_type == "opentopo" or (source_type == "bbox" and "opentopography" in template_url.lower()):
			max_allowed_size = 25000
			if size_w > max_allowed_size or size_h > max_allowed_size:
				QMessageBox.warning(
					self.iface.mainWindow(),
					"Too Large Area",
					f"OpenTopography API limits the data download size.\n\n"
					f"Your current selection is {size_w}m x {size_h}m.\n"
					f"Please reduce the 'Size (meters)' below {max_allowed_size}m (25km),\n"
					f"otherwise the server will return an HTTP 400 Error."
				)
				return

		want_png = format_index == 0
		want_asc = format_index == 1
		want_tif = format_index == 2

		gdal_alg = gdal.GRA_Lanczos

		valasz = QMessageBox.question(
			self.iface.mainWindow(),
			"Confirmation",
			f"Start Enfusion export?\n\nSource: {current_source.get('name')}\nSize: {size_w}x{size_h}m\nSatellite: {sat_res_w}x{sat_res_h}px\nHeightmap: {resolution_w}x{resolution_h}px\nPath: {output_dir}",
			QM_Yes | QM_No
		)

		if valasz != QM_Yes:
			return

		try:
			if not os.path.exists(output_dir):
				os.makedirs(output_dir)
		except Exception as e:
			QMessageBox.critical(self.iface.mainWindow(), "Error", f"Could not create directory {output_dir}: {e}")
			return

		timestamp = time.strftime("%Y%m%d_%H%M%S")
		# Tag eroded output in the filename. Without it the previews, which
		# pick the newest heightmap in the directory, would load an already
		# eroded export and erode it again -- each round compounding on the
		# last instead of starting from the raw DEM every time.
		_suffix = "_eroded" if _mode == 'eroded' else ""
		output_sat_png = os.path.join(output_dir, f"satmap_{timestamp}.png")
		output_tif = os.path.join(output_dir, f"heightmap_{timestamp}{_suffix}.tif")
		output_asc = os.path.join(output_dir, f"heightmap_{timestamp}{_suffix}.asc")
		output_png = os.path.join(output_dir, f"heightmap_{timestamp}{_suffix}.png")
		output_txt = os.path.join(output_dir, f"enfusion_import_{timestamp}.txt")
		temp_rgb_tif = os.path.join(output_dir, f"temp_source_{timestamp}.tif")

		progress = QProgressDialog("Export running...", "Cancel", 0, 100, self.iface.mainWindow())
		progress.setWindowTitle("Enfusion Export Status")
		progress.show()

		def step(val, text):
			progress.setValue(val)
			progress.setLabelText(text)
			QApplication.processEvents()
			if progress.wasCanceled():
				raise Exception("Export cancelled by user.")

		google_layer = None

		try:
			step(5, "Transforming coordinates with correction...")
			lat_rad = math.radians(y_center)
			scale_factor = 1.0 / math.cos(lat_rad)
			corrected_size_w = size_w * scale_factor
			corrected_size_h = size_h * scale_factor

			source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
			target_crs = QgsCoordinateReferenceSystem("EPSG:3857")
			context = QgsProject.instance().transformContext()
			transform = QgsCoordinateTransform(source_crs, target_crs, context)
			pt = transform.transform(QgsPointXY(x_center, y_center))

			half_w = corrected_size_w / 2
			half_h = corrected_size_h / 2
			rect = QgsRectangle(pt.x() - half_w, pt.y() - half_h, pt.x() + half_w, pt.y() + half_h)

			for layer in list(QgsProject.instance().mapLayers().values()):
				if "Google_Sat_" in layer.name() or "DEM_Source_" in layer.name() or "AWS_Terrarium_" in layer.name():
					QgsProject.instance().removeMapLayer(layer.id())
			gc.collect()
			QApplication.processEvents()

			# 1. PHASE: Satellite Image
			step(15, "Downloading Google Satellite imagery...")
			uri = "type=xyz&url=https://mt1.google.com/vt/lyrs%3Ds%26x%3D%7Bx%7D%26y%3D%7By%7D%26z%3D%7Bz%7D&zmax=19&zmin=0"
			google_layer = QgsRasterLayer(uri, f"Google_Sat_{timestamp}", "wms")
			QgsProject.instance().addMapLayer(google_layer)
			QApplication.processEvents()

			step(30, "Rendering satellite image...")
			settings_sat = QgsMapSettings()
			settings_sat.setLayers([google_layer])
			settings_sat.setBackgroundColor(QColor(255, 255, 255))

			settings_sat.setOutputSize(QSize(sat_res_w, sat_res_h))
			settings_sat.setExtent(rect)

			job_sat = QgsMapRendererSequentialJob(settings_sat)
			job_sat.start()
			job_sat.waitForFinished()
			job_sat.renderedImage().save(output_sat_png)

			QgsProject.instance().removeMapLayer(google_layer.id())
			google_layer = None
			job_sat = None
			settings_sat = None
			gc.collect()

			# 2. PHASE: Heightmap Prep
			step(45, "Calculating raw heightmap coordinates...")
			xmin, ymin, xmax, ymax = rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()

			if source_type == "xyz":
				def epsg3857_to_tile(x, y, z):
					origin_shift = 20037508.342789244
					res = (origin_shift * 2.0) / (256.0 * (2.0**z))
					px = (x + origin_shift) / res
					py = (origin_shift - y) / res
					tx = int(px // 256)
					ty = int(py // 256)
					return tx, ty, px, py

				target_mpp = corrected_size_w / resolution_w
				earth_circ = 20037508.342789244 * 2.0
				z_float = math.log2(earth_circ / (256.0 * target_mpp))
				z = int(math.ceil(z_float))
				z = max(0, min(z, 15))

				txmin, tymin, pxmin, pymin = epsg3857_to_tile(xmin, ymax, z)
				txmax, tymax, pxmax, pymax = epsg3857_to_tile(xmax, ymin, z)

				tiles_wide = txmax - txmin + 1
				tiles_high = tymax - tymin + 1
				total_tiles = tiles_wide * tiles_high

				if total_tiles > 1024:
					z -= 1
					txmin, tymin, pxmin, pymin = epsg3857_to_tile(xmin, ymax, z)
					txmax, tymax, pxmax, pymax = epsg3857_to_tile(xmax, ymin, z)
					tiles_wide = txmax - txmin + 1
					tiles_high = tymax - tymin + 1
					total_tiles = tiles_wide * tiles_high

				full_image = np.zeros((tiles_high * 256, tiles_wide * 256, 3), dtype=np.uint8)

				tile_idx = 0
				for tx in range(txmin, txmax + 1):
					for ty in range(tymin, tymax + 1):
						if progress.wasCanceled():
							raise Exception("Export cancelled by user.")
						step(50 + int(20 * (tile_idx / total_tiles)), f"Downloading XYZ DEM tiles {tile_idx+1}/{total_tiles}...")

						url = template_url.replace("{z}", str(z)) \
										  .replace("{tx}", str(tx)) \
										  .replace("{ty}", str(ty)) \
										  .replace("{x}", str(tx)) \
										  .replace("{y}", str(ty)) \
										  .replace("{api_key}", api_key)

						try:
							req = urllib.request.Request(url, headers={'User-Agent': 'QGIS-Arma-Plugin/1.0'})
							with urllib.request.urlopen(req) as response:
								img = Image.open(response).convert('RGB')
								y_off = (ty - tymin) * 256
								x_off = (tx - txmin) * 256
								full_image[y_off:y_off+256, x_off:x_off+256, :] = np.array(img)
						except Exception as e:
							pass
						tile_idx += 1

				step(75, "Decoding pure RGB channels to continuous float elevation...")
				crop_x1 = int(max(0, round(pxmin - txmin * 256)))
				crop_y1 = int(max(0, round(pymin - tymin * 256)))
				crop_x2 = int(min(full_image.shape[1], round(pxmax - txmin * 256)))
				crop_y2 = int(min(full_image.shape[0], round(pymax - tymin * 256)))

				crop_array = full_image[crop_y1:crop_y2, crop_x1:crop_x2, :]

				r = crop_array[:, :, 0].astype(np.float32)
				g = crop_array[:, :, 1].astype(np.float32)
				b = crop_array[:, :, 2].astype(np.float32)

				if decoder_type == "terrarium":
					elevation = (r * 256.0 + g + (b / 256.0)) - 32768.0
				elif decoder_type == "mapbox":
					elevation = -10000.0 + (r * 65536.0 + g * 256.0 + b) * 0.1
				else:
					elevation = r

				step(80, "Smooth scaling to requested resolution...")
				driver_mem = gdal.GetDriverByName('MEM')
				ds_mem = driver_mem.Create('', elevation.shape[1], elevation.shape[0], 1, gdal.GDT_Float32)

				pixel_size_x = (xmax - xmin) / elevation.shape[1]
				pixel_size_y = (ymax - ymin) / elevation.shape[0]

				ds_mem.SetGeoTransform((xmin, pixel_size_x, 0, ymax, 0, -pixel_size_y))
				srs = osr.SpatialReference()
				srs.ImportFromEPSG(3857)
				ds_mem.SetProjection(srs.ExportToWkt())
				ds_mem.GetRasterBand(1).WriteArray(elevation)
				ds_mem.GetRasterBand(1).SetNoDataValue(VOID_SENTINEL)

				gdal.Warp(output_tif, ds_mem,
						  width=resolution_w, height=resolution_h,
						  outputBounds=(xmin, ymin, xmax, ymax),
						  resampleAlg=gdal_alg,
						  outputType=gdal.GDT_Float32,
						  dstNodata=VOID_SENTINEL,
						  format='GTiff')

				ds_mem = None

			elif source_type in ("bbox", "opentopo", "local"):
				step(50, "Downloading / Extracting terrain via GDAL...")
				try:
					context_cs = QgsProject.instance().transformContext()
					transform_to_4326_cs = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:3857"), QgsCoordinateReferenceSystem("EPSG:4326"), context_cs)
					pt_min_cs = transform_to_4326_cs.transform(QgsPointXY(xmin, ymin))
					pt_max_cs = transform_to_4326_cs.transform(QgsPointXY(xmax, ymax))

					south_cs = min(pt_min_cs.y(), pt_max_cs.y())
					north_cs = max(pt_min_cs.y(), pt_max_cs.y())
					west_cs = min(pt_min_cs.x(), pt_max_cs.x())
					east_cs = max(pt_min_cs.x(), pt_max_cs.x())

					url = template_url.replace("{ot_dem_type}", ot_dem_type)\
									  .replace("{dataset}", ot_dem_type)\
									  .replace("{south}", str(south_cs))\
									  .replace("{north}", str(north_cs))\
									  .replace("{west}", str(west_cs))\
									  .replace("{east}", str(east_cs))\
									  .replace("{api_key}", api_key)

					# OpenTopography error handling specific logic
					if "opentopography" in url.lower():
						req = urllib.request.Request(url, headers={'User-Agent': 'QGIS-Arma-Plugin/1.0'})
						try:
							with urllib.request.urlopen(req) as response:
								with open(temp_rgb_tif, 'wb') as f:
									f.write(response.read())
						except urllib.error.HTTPError as http_err:
							try:
								server_response = http_err.read().decode('utf-8')
								clean_error = server_response
								if "<error>" in server_response and "</error>" in server_response:
									clean_error = server_response.split("<error>")[1].split("</error>")[0]

								if "maximum rate limit" in clean_error.lower():
									error_details = (
										"You have hit the anonymous rate limit (50 requests / 24 hours).\n\n"
										"HOW TO FIX THIS:\n"
										"1. Sign up for a free account at portal.opentopography.org\n"
										"2. Generate your personal API Key (Go to: myOT > myOT API Key)\n"
										"3. Paste your key into the 'API Key' field in this plugin to continue!"
									)
								elif "invalid api key" in clean_error.lower():
									error_details = "The provided API Key is invalid. Please double-check your key for typos!"
								elif "bad request" in clean_error.lower():
									error_details = "Bad Request. Your selected coordinates or bounding box format might be wrong."
								else:
									error_details = f"Server Response: {clean_error}"
							except:
								error_details = f"HTTP Error Code: {http_err.code}"
							raise Exception(f"OpenTopography API rejected your request!\n\n{error_details}")
						except Exception as e:
							raise Exception(f"Connection error to OpenTopography: {e}")

						gdal.Warp(output_tif, temp_rgb_tif,
								  width=resolution_w, height=resolution_h,
								  outputBounds=(xmin, ymin, xmax, ymax),
								  dstSRS='EPSG:3857',
								  resampleAlg=gdal_alg,
								  outputType=gdal.GDT_Float32,
								  srcNodata=_probe_nodata(temp_rgb_tif),
								  dstNodata=VOID_SENTINEL,
								  format='GTiff')
					else:
						# GDAL standard direct fetch
						gdal.Warp(output_tif, url,
								  width=resolution_w, height=resolution_h,
								  outputBounds=(xmin, ymin, xmax, ymax),
								  dstSRS='EPSG:3857',
								  resampleAlg=gdal_alg,
								  outputType=gdal.GDT_Float32,
								  srcNodata=_probe_nodata(url),
								  dstNodata=VOID_SENTINEL,
								  format='GTiff')

				except Exception as e:
					raise Exception(f"Failed to fetch data from custom URL/Path: {e}")

			# =========================================================================
			# TERRAIN ENGINEERING
			# =========================================================================
			# Fill voids before the terrain engineer, not after. The engineer
			# flattens roads and carves rivers using gaussian blurs and distance
			# transforms; run over an unfilled border those pull void elevations
			# into real terrain along any corridor that crosses the edge, and
			# the damage is written back before the correction step below ever
			# sees it.
			_fill_voids_in_place(output_tif, step)

			if want_burn:
				engineer = TerrainEngineer(self.iface)
				self._engineer = engineer
				engineer.run(
					output_tif=output_tif,
					output_dir=output_dir,
					timestamp=timestamp,
					xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
					resolution_w=resolution_w,
					resolution_h=resolution_h,
					target_crs=target_crs,
					source_crs=source_crs,
					context=context,
					pixel_size=(xmax - xmin) / resolution_w,
					step_callback=step
				)
				self._engineer_protect_mask = engineer.protect_mask
			# =========================================================================

			step(85, "Applying Enfusion specific corrections...")
			ds_out = gdal.Open(output_tif, gdal.GA_Update)
			elevation_resampled = ds_out.GetRasterBand(1).ReadAsArray()

			# Voids are now marked at the warp (see VOID_SENTINEL), so the mask can
			# read the value GDAL actually recorded instead of guessing from the
			# terrain's own distribution. Every heuristic tried here failed: a
			# blanket low-elevation cut flagged 1.67 M pixels of genuine low ground,
			# and an edge-connected flood fill silently missed the border entirely on
			# a coastal map, where pad and real sea level are the same number.
			_nd = ds_out.GetRasterBand(1).GetNoDataValue()
			valid_mask = elevation_resampled > -10000.0
			if _nd is not None and _nd <= -10000.0:
				# Only trust an out-of-range sentinel. If a source declared an
				# in-range NoData such as 0 and GDAL propagated it, masking every
				# pixel at that elevation would delete real terrain.
				valid_mask &= (elevation_resampled != _nd)
			if np.any(valid_mask):
				min_val = float(np.min(elevation_resampled[valid_mask]))
				max_val = float(np.max(elevation_resampled[valid_mask]))

				# Filling NoData with min_val drops every void to the lowest
				# point on the map. Where the DEM does not reach the export
				# window -- a tile boundary along one edge is the common case --
				# that is a cliff the full height of the terrain wrapping the
				# border, which reads in the Enfusion editor as a giant bowl and
				# drags the interior out of shape through LOD blending.
				# Replicate the nearest valid elevation instead, so a void
				# continues the terrain around it rather than cutting a hole.
				if not np.all(valid_mask):
					n_void = int((~valid_mask).sum())
					try:
						from scipy.ndimage import distance_transform_edt
						_, (iy, ix) = distance_transform_edt(
							~valid_mask, return_indices=True)
						elevation_resampled = np.where(
							valid_mask, elevation_resampled,
							elevation_resampled[iy, ix])
						fill_note = "nearest-valid replication"
					except Exception:
						elevation_resampled = np.where(
							valid_mask, elevation_resampled, min_val)
						fill_note = "min_val (scipy unavailable)"
					QgsMessageLog.logMessage(
						"Filled %d NoData pixels by %s" % (n_void, fill_note),
						"ArmaTerrainExport", Qgis.Info)
			else:
				min_val = float(np.min(elevation_resampled))
				max_val = float(np.max(elevation_resampled))

			# --- HYDRAULIC EROSION -------------------------------------------
			erosion_settings = getattr(self, "_erosion_settings", None)
			if erosion_settings:
				try:
					step(86, "Running hydraulic erosion...")
					_erosion = _arte_import('erosion')

					elev_f = elevation_resampled.astype(np.float32)
					params = dict(erosion_settings.get('params', {}))
					mp = elev_f.size / 1e6
					params['n_particles'] = max(
						1000, int(erosion_settings.get('per_mp', 100000) * mp))

					protect = None
					if erosion_settings.get('protect'):
						protect = getattr(self, "_engineer_protect_mask", None)

					def _erosion_step(frac, msg):
						step(86 + int(frac * 3), msg)

					runner = (_erosion.simulate_parallel
							  if erosion_settings.get('parallel')
							  else _erosion.simulate)
					_px = None
					try:
						_px = float(size_w) / float(resolution_w)
					except Exception:
						_px = None
					eroded = runner(elev_f, params, protect_mask=protect,
									progress=_erosion_step, seed=1,
									pixel_size=_px)

					if np.isfinite(eroded).all():
						elevation_resampled = eroded.astype(
							elevation_resampled.dtype)
						min_val = float(np.min(elevation_resampled))
						max_val = float(np.max(elevation_resampled))
						QgsMessageLog.logMessage(
							"Erosion applied: %s particles" % (
								"{:,}".format(params['n_particles'])),
							"ArmaTerrainExport", Qgis.Info)
					else:
						QgsMessageLog.logMessage(
							"Erosion produced non-finite values; keeping "
							"un-eroded heightmap.", "ArmaTerrainExport",
							Qgis.Warning)
				except Exception as ero_exc:
					# Erosion is an enhancement; never lose the export over it.
					QgsMessageLog.logMessage(
						"Erosion skipped: %s" % ero_exc,
						"ArmaTerrainExport", Qgis.Warning)

			ds_out.GetRasterBand(1).WriteArray(elevation_resampled)
			ds_out.FlushCache()

			orig_min = min_val
			orig_max = max_val

			grid_cell_size = size_w / resolution_w
			height_scale = (max_val - min_val) / 65535.0 if max_val > min_val else 1.0

			chunk_pixel_size = 512
			grid_chunks = math.ceil(resolution_w / chunk_pixel_size)

			with open(output_txt, "w", encoding="utf-8") as txt_f:
				txt_f.write("=== Arma Reforger / Enfusion Import Values ===\n\n")
				txt_f.write(f"Grid cell size (meters): {grid_cell_size:.4f}\n")
				txt_f.write(f"GridChunks: {grid_chunks}\n")
				txt_f.write(f"Height scale (meters): {height_scale:.6f}\n")
				txt_f.write(f"Terrain grid size: {resolution_w}x{resolution_h}\n\n")
				txt_f.write(f"--- Debug Info ---\n")
				txt_f.write(f"Original Min Elev: {orig_min:.2f} m\n")
				txt_f.write(f"Original Max Elev: {orig_max:.2f} m\n")

			if want_asc:
				driver_asc = gdal.GetDriverByName('AAIGrid')
				ds_asc = driver_asc.CreateCopy(output_asc, ds_out)
				ds_asc = None

			if want_png:
				step(95, "Generating high-fidelity 16-bit PNG...")
				if max_val > min_val:
					elevation_16bit = (((elevation_resampled - min_val) / (max_val - min_val)) * 65535.0)
				else:
					elevation_16bit = np.zeros_like(elevation_resampled)

				elevation_16bit = np.clip(elevation_16bit, 0, 65535).astype(np.uint16)

				driver_mem_16 = gdal.GetDriverByName('MEM')
				ds_mem_16 = driver_mem_16.Create('', resolution_w, resolution_h, 1, gdal.GDT_UInt16)
				ds_mem_16.GetRasterBand(1).WriteArray(elevation_16bit)

				driver_png = gdal.GetDriverByName('PNG')
				driver_png.CreateCopy(output_png, ds_mem_16)
				ds_mem_16 = None

			ds_out = None

			if not want_tif:
				try: os.remove(output_tif)
				except: pass

			step(100, "Done!")
			progress.close()

			success_msg = f"All Enfusion files exported to:\n{output_dir}!\n\n"
			success_msg += f"1. Satmap: satmap_{timestamp}.png ({sat_res_w}x{sat_res_h})\n"
			success_msg += f"2. Enfusion Values: enfusion_import_{timestamp}.txt\n"
			if want_tif: success_msg += f"3. Heightmap TIF: heightmap_{timestamp}.tif ({resolution_w}x{resolution_h})\n"
			if want_asc: success_msg += f"3. Heightmap ASC: heightmap_{timestamp}.asc ({resolution_w}x{resolution_h})\n"
			if want_png: success_msg += f"3. Heightmap 16-bit PNG: heightmap_{timestamp}.png ({resolution_w}x{resolution_h})\n"
			success_msg += f"\nMin Elev: {min_val:.2f}m\nMax Elev: {max_val:.2f}m\n"

			QMessageBox.information(self.iface.mainWindow(), "Success", success_msg)

		except Exception as e:
			QMessageBox.critical(self.iface.mainWindow(), "Error", str(e))
		finally:
			progress.close()
			try:
				if os.path.exists(temp_rgb_tif):
					os.remove(temp_rgb_tif)
			except:
				pass
			gc.collect()