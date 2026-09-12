import numpy as np, os, tempfile
from osgeo import gdal
gdal.UseExceptions()
tmp=tempfile.mkdtemp()
src=os.path.join(tmp,'s.tif')
drv=gdal.GetDriverByName('GTiff')
ds=drv.Create(src,100,100,1,gdal.GDT_Float32)
ds.SetGeoTransform((1000.0,10.0,0,2000.0,0,-10.0))
a=np.full((100,100),2500.0,np.float32)
a[:10,:]=-32768.0                      # a real source sentinel void
b=ds.GetRasterBand(1); b.WriteArray(a); b.SetNoDataValue(-32768.0); ds.FlushCache(); ds=None
bounds=(1000.0,1000.0,2000.0,2000.0)
for label,kw in [("Lanczos, no srcNodata", dict(resampleAlg=gdal.GRA_Lanczos)),
                 ("Lanczos + srcNodata",   dict(resampleAlg=gdal.GRA_Lanczos, srcNodata=-32768, dstNodata=-32768)),
                 ("Cubic  + srcNodata",    dict(resampleAlg=gdal.GRA_Cubic,   srcNodata=-32768, dstNodata=-32768))]:
    out=os.path.join(tmp,'o%d.tif'%(abs(hash(label))%9999))
    gdal.Warp(out,src,width=200,height=200,outputBounds=bounds,outputType=gdal.GDT_Float32,format='GTiff',**kw)
    d=gdal.Open(out); arr=d.GetRasterBand(1).ReadAsArray(); d=None
    smear=((arr>-10000)&(arr<2400)).sum()   # intermediate garbage between void and terrain
    print("%-24s min %10.1f  smeared px (between void and 2500): %d"%(label,arr.min(),smear))
