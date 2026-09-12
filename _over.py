import sys, numpy as np, os, tempfile
sys.path.insert(0, r"C:/Users/nomisum/AppData/Roaming/QGIS/QGIS4/profiles/default/python/plugins")
from qgis.core import QgsApplication
app=QgsApplication([],False); app.initQgis()
from osgeo import gdal
gdal.UseExceptions()
from ARTE.arte import VOID_SENTINEL, _probe_nodata, _fill_voids_in_place
tmp=tempfile.mkdtemp(); drv=gdal.GetDriverByName('GTiff')
src=os.path.join(tmp,'s.tif')
ds=drv.Create(src,100,100,1,gdal.GDT_Float32)
ds.SetGeoTransform((1000.0,10.0,0,2000.0,0,-10.0))
a=(2400+np.random.default_rng(0).random((100,100))*400).astype(np.float32)
ds.GetRasterBand(1).WriteArray(a); ds.FlushCache(); ds=None
for alg,nm in [(gdal.GRA_Lanczos,'Lanczos'),(gdal.GRA_Cubic,'Cubic'),(gdal.GRA_Bilinear,'Bilinear')]:
    out=os.path.join(tmp,nm+'.tif')
    gdal.Warp(out,src,width=240,height=240,outputBounds=(1000.0,800.0,2200.0,2000.0),
              resampleAlg=alg,outputType=gdal.GDT_Float32,
              srcNodata=_probe_nodata(src),dstNodata=VOID_SENTINEL,format='GTiff')
    d=gdal.Open(out); pre=d.GetRasterBand(1).ReadAsArray(); d=None
    valid=pre!=VOID_SENTINEL
    _fill_voids_in_place(out)
    d=gdal.Open(out); post=d.GetRasterBand(1).ReadAsArray(); d=None
    print("%-9s valid-region range %7.1f..%7.1f (src %.1f..%.1f) | after fill %7.1f..%7.1f"%(
        nm,pre[valid].min(),pre[valid].max(),a.min(),a.max(),post.min(),post.max()))
