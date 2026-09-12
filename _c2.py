import numpy as np
from osgeo import gdal
gdal.UseExceptions()
d=r"C:/QGIS/ArmaTerrainExport/bamiyanhighway"
for name,f,lo,hi in [
    ("124435 clean", d+"/heightmap_20260906_124435.png", 2346.58, 2824.34),
    ("122103 border", d+"/heightmap_20260906_122103.png", -37.33, 2825.53)]:
    ds=gdal.Open(f); a=ds.GetRasterBand(1).ReadAsArray().astype(np.float32); ds=None
    e=lo+(a/65535.0)*(hi-lo)
    print("%-14s bottom-row elev: min %.1f max %.1f mean %.1f"%(name,e[-1].min(),e[-1].max(),e[-1].mean()))
    print("               right-col elev: min %.1f max %.1f mean %.1f"%(e[:,-1].min(),e[:,-1].max(),e[:,-1].mean()))
    print("               interior[200:-200] min %.1f"%e[200:-200,200:-200].min())
    print("               raw uint16 bottom row: min %d max %d"%(a[-1].min(),a[-1].max()))
