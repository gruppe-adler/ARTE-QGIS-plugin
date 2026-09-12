import numpy as np
from osgeo import gdal
gdal.UseExceptions()
d=r"C:/QGIS/ArmaTerrainExport/bamiyanhighway"
for name,f,lo,hi in [
    ("124435 (clean, Min 2346)", d+"/heightmap_20260906_124435.png", 2346.58, 2824.34),
    ("122103 (bordered)",        d+"/heightmap_20260906_122103.png", -37.33, 2825.53)]:
    try:
        ds=gdal.Open(f); a=ds.GetRasterBand(1).ReadAsArray().astype(np.float32); ds=None
        e=lo+(a/65535.0)*(hi-lo)
        low=e<(lo+(hi-lo)*0.25)
        print("%-26s relief %7.1f m | low-band px %8d (%.3f%%)"%(name,np.ptp(e),low.sum(),100*low.mean()))
        print("      edges: top %.2f bot %.2f left %.2f right %.2f"%(
            low[0].mean(),low[-1].mean(),low[:,0].mean(),low[:,-1].mean()))
    except Exception as ex:
        print(name,"ERR",ex)
