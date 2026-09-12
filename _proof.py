import numpy as np, os, tempfile
from osgeo import gdal
gdal.UseExceptions()
tmp=tempfile.mkdtemp()
# Build a small "source DEM" that does NOT cover the full requested window,
# mimicking OpenTopography snapping to 30 m cells.
src=os.path.join(tmp,'src.tif')
drv=gdal.GetDriverByName('GTiff')
ds=drv.Create(src,100,100,1,gdal.GDT_Float32)
ds.SetGeoTransform((1000.0, 10.0, 0, 2000.0, 0, -10.0))   # covers x 1000..2000, y 1000..2000
a=(2400+np.random.default_rng(0).random((100,100))*400).astype(np.float32)
ds.GetRasterBand(1).WriteArray(a); ds.FlushCache(); ds=None

# Request a LARGER window than the source covers (pad on right and bottom)
bounds=(1000.0, 800.0, 2200.0, 2000.0)
for label,kw in [("NO dstNodata (current)", {}),
                 ("dstNodata=-32768 (fix)", {"srcNodata":None,"dstNodata":-32768})]:
    out=os.path.join(tmp,'out_%d.tif'%len(label))
    gdal.Warp(out, src, width=240, height=240, outputBounds=bounds,
              resampleAlg=gdal.GRA_Bilinear, outputType=gdal.GDT_Float32,
              format='GTiff', **{k:v for k,v in kw.items() if v is not None})
    d=gdal.Open(out); b=d.GetRasterBand(1); arr=b.ReadAsArray(); nd=b.GetNoDataValue(); d=None
    pad_detect_old = int((arr <= -10000).sum())
    print("%-26s min %9.2f  declared nodata %-10s  pixels caught by '> -10000': %d"%(
        label, arr.min(), nd, pad_detect_old))
