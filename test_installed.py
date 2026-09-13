import sys, os, numpy as np
sys.path.insert(0, r"C:/Users/nomisum/AppData/Roaming/QGIS/QGIS4/profiles/default/python/plugins/ARTE")
from qgis.core import QgsApplication
app=QgsApplication([],False); app.initQgis()
ok=True
def chk(name, cond, detail=""):
    global ok
    print("  %-38s %s %s" % (name, "PASS" if cond else "FAIL", detail))
    if not cond: ok=False

import arte, erosion, erosion_preview, roadwater, roadwater_preview
chk("modules import", True)

from qgis.PyQt.QtWidgets import QApplication, QWidget
qa=QApplication.instance() or QApplication(sys.argv)
dlg=arte.CombinedArmaInputDialog(parent=QWidget())
for n in ['cb_erosion','cmb_erosion','btn_erosion_preview','btn_roadwater_preview']:
    chk("dialog has %s"%n, hasattr(dlg,n))

# nodata fix present
import inspect
src=inspect.getsource(arte.ArmaExportPlugin.execute_export)
chk("nodata uses nearest-valid", "distance_transform_edt" in src and "nearest-valid" in src)
chk("erosion hook present", "_erosion_settings" in src)

chk("warps declare dstNodata", src.count("dstNodata") >= 2)
chk("warp probes source nodata", "_probe_nodata" in src)
chk("mask reads declared nodata", "GetNoDataValue" in src and "valid_mask" in src)
chk("failed heuristic removed", "_p01" not in src and "_border_void" not in src)
chk("voids filled before engineer", "_fill_voids_in_place" in src)

# The mechanism itself, not just the presence of a string: warp a source that
# under-covers the requested bounds and confirm the margin comes back marked.
import tempfile, os
from osgeo import gdal
gdal.UseExceptions()
_tmp = tempfile.mkdtemp()
_src = os.path.join(_tmp, "s.tif")
_d = gdal.GetDriverByName("GTiff").Create(_src, 100, 100, 1, gdal.GDT_Float32)
_d.SetGeoTransform((1000.0, 10.0, 0, 2000.0, 0, -10.0))
_d.GetRasterBand(1).WriteArray(np.full((100, 100), 2500.0, np.float32))
_d.FlushCache(); _d = None
_out = os.path.join(_tmp, "o.tif")
gdal.Warp(_out, _src, width=240, height=240,
          outputBounds=(1000.0, 800.0, 2200.0, 2000.0),
          resampleAlg=gdal.GRA_Lanczos, outputType=gdal.GDT_Float32,
          srcNodata=arte._probe_nodata(_src), dstNodata=arte.VOID_SENTINEL,
          format="GTiff")
_ds = gdal.Open(_out); _a = _ds.GetRasterBand(1).ReadAsArray(); _ds = None
chk("uncovered margin is marked", int((_a == arte.VOID_SENTINEL).sum()) > 0,
    "%d px" % int((_a == arte.VOID_SENTINEL).sum()))
_n = arte._fill_voids_in_place(_out)
_ds = gdal.Open(_out); _b = _ds.GetRasterBand(1).ReadAsArray(); _ds = None
chk("fill removes every void", int((_b == arte.VOID_SENTINEL).sum()) == 0,
    "filled %d" % _n)
chk("fill restores real elevation", _b.min() > 2000.0, "min %.1f" % _b.min())

# --- satellite micro-relief ---
_srm = arte._arte_import('satrelief')
from scipy.ndimage import gaussian_filter as _gf
_rng = np.random.default_rng(3)
_N = 384
_yy, _xx = np.mgrid[0:_N, 0:_N].astype(np.float32)
_terr = _gf((2400 + 90*np.sin(_xx/22.)*np.cos(_yy/26.)).astype(np.float32), 2)
# imagery that genuinely shades the terrain: hillshade it and add mild albedo
_gy, _gx = np.gradient(_gf(_terr, 3))
_sl = np.arctan(np.hypot(_gx, _gy)); _asp = np.arctan2(-_gx, _gy)
_hs = (np.sin(np.radians(30))*np.cos(_sl) +
       np.cos(np.radians(30))*np.sin(_sl)*np.cos(np.radians(360-315+90)-_asp))
_good = np.dstack([np.clip(_hs*120+130 + _rng.normal(0,4,(_N,_N)), 0, 255)]*3).astype(np.float32)
_p = _srm.resolve_params('moderate')

_out, _info = _srm.apply(_terr, _good, 1.0, _p)
chk("satrelief runs on shaded imagery", _info['applied'], "fit %.3f" % _info['fit'])
chk("satrelief output is bounded",
    np.isfinite(_out).all() and np.abs(_out-_terr).max() <= _p['clamp_m']+1e-3,
    "max %.2f m" % np.abs(_out-_terr).max())

# THE check that answers "must never make output worse": degenerate imagery
_bad = {
    "snow":  np.dstack([np.full((_N,_N),240.0)+_rng.normal(0,2,(_N,_N))]*3),
    "noise": _rng.random((_N,_N,3))*255,
}
_refused = all(np.array_equal(_srm.apply(_terr, v.astype(np.float32), 1.0, _p)[0], _terr)
               for v in _bad.values())
chk("satrelief refuses degenerate imagery", _refused)

# A satmap arrives as 0-255 or as normalised 0-1 float depending on source and
# on _match_grid's resampling. A hardcoded log floor of 1.0 flattened the whole
# 0-1 case to log(1)=0, so the pass refused with "integrated to nothing" however
# good the imagery was -- and every existing test used 0-255, so none caught it.
_o01, _i01 = _srm.apply(_terr, (_good/255.0).astype(np.float32), 1.0, _p)
chk("satrelief handles 0-1 float satmap", _i01['applied'] and
    abs((_o01-_terr).std() - (_out-_terr).std()) < 0.05,
    "std %.4f vs %.4f m" % ((_o01-_terr).std(), (_out-_terr).std()))

# protect mask must be honoured
_pm = np.zeros((_N,_N), bool); _pm[100:140, :] = True
_o2, _i2 = _srm.apply(_terr, _good, 1.0, _p, protect_mask=_pm)
chk("satrelief honours protect mask",
    (not _i2['applied']) or np.array_equal(_o2[_pm], _terr[_pm]))

eng=inspect.getsource(arte.TerrainEngineer.run)
chk("per-feature roads wired", "used_profile_roads" in eng)
chk("per-feature rivers wired", "used_profile_rivers" in eng)
chk("ribbon fallback kept", "apply_flat_ribbon" in eng)

# numeric behaviour
rng=np.random.default_rng(0); N=256
y,x=np.mgrid[0:N,0:N]/float(N)
z=(2400+(1-abs(x-0.5)*2)*400+rng.normal(0,0.3,(N,N))).astype(np.float32)
p=erosion.resolve_params('moderate',z.shape); p['ttl']=48
out=erosion.simulate(z,p,seed=1,pixel_size=2.0)
chk("erosion finite", np.isfinite(out).all())
chk("relief bounded", abs(np.ptp(out)-np.ptp(z))/np.ptp(z) < 0.25,
    "in %.1f out %.1f"%(np.ptp(z),np.ptp(out)))

road=np.array([[float(t), t*0.6+40.0] for t in range(0,200)])
r1=roadwater.apply_roads(z,[road],2.0,half_width_m=7.0,feather_m=6.0)
t=np.array([1,0.6]); t/=np.linalg.norm(t); perp=np.array([-t[1],t[0]])
def cant(a):
    v=[]
    for r in range(40,160,10):
        c=r*0.6+40.0
        s=[a[int(round(r+perp[0]*d)),int(round(c+perp[1]*d))] for d in (-2,0,2)]
        v.append(max(s)-min(s))
    return np.mean(v)
chk("road cant reduced", cant(r1) < cant(z)*0.2, "%.3f -> %.3f m"%(cant(z),cant(r1)))

river=np.array([[float(t), 128.0] for t in range(20,230)])
r2=roadwater.apply_rivers(z,[river],2.0,half_width_m=6.0,feather_m=8.0,depth_m=1.5)
def against(a):
    b=np.array([a[int(r),int(c)] for r,c in river])
    d=np.diff(b); d=-d if b[0]<b[-1] else d
    return int((d>0).sum())
chk("river descends", against(r2) < against(z)*0.5, "%d -> %d steps"%(against(z),against(r2)))

print()
print("RESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
