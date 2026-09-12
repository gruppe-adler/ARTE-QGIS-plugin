import sys, numpy as np, os, tempfile
sys.path.insert(0, r"C:/Users/nomisum/AppData/Roaming/QGIS/QGIS4/profiles/default/python/plugins")
from qgis.core import QgsApplication
app=QgsApplication([],False); app.initQgis()
from osgeo import gdal
gdal.UseExceptions()
from ARTE.arte import VOID_SENTINEL, _probe_nodata, _fill_voids_in_place

tmp=tempfile.mkdtemp()
src=os.path.join(tmp,'src.tif')
drv=gdal.GetDriverByName('GTiff')
ds=drv.Create(src,100,100,1,gdal.GDT_Float32)
ds.SetGeoTransform((1000.0,10.0,0,2000.0,0,-10.0))   # covers 1000..2000
a=(2400+np.random.default_rng(0).random((100,100))*400).astype(np.float32)
ds.GetRasterBand(1).WriteArray(a); ds.FlushCache(); ds=None

out=os.path.join(tmp,'out.tif')
# request a LARGER window than the source covers
gdal.Warp(out, src, width=240, height=240, outputBounds=(1000.0,800.0,2200.0,2000.0),
          resampleAlg=gdal.GRA_Lanczos, outputType=gdal.GDT_Float32,
          srcNodata=_probe_nodata(src), dstNodata=VOID_SENTINEL, format='GTiff')
d=gdal.Open(out); arr=d.GetRasterBand(1).ReadAsArray(); nd=d.GetRasterBand(1).GetNoDataValue(); d=None
print("after warp : min %9.1f  declared nodata %s  void px %d"%(arr.min(),nd,int((arr==VOID_SENTINEL).sum())))
n=_fill_voids_in_place(out)
d=gdal.Open(out); arr2=d.GetRasterBand(1).ReadAsArray(); nd2=d.GetRasterBand(1).GetNoDataValue(); d=None
print("after fill : min %9.1f  declared nodata %s  filled %d"%(arr2.min(),nd2,n))
print("relief     : %.1f  (interior real range %.1f)"%(np.ptp(arr2),np.ptp(a)))
print("PASS" if arr2.min()>2000 and (arr2==VOID_SENTINEL).sum()==0 else "FAIL")
