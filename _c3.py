import numpy as np
from osgeo import gdal
gdal.UseExceptions()
ds=gdal.Open(r"C:/QGIS/ArmaTerrainExport/bamiyanhighway/heightmap_20260906_122103.png")
a=ds.GetRasterBand(1).ReadAsArray(); ds=None
print("raw uint16. unique values in bottom 120 rows:", np.unique(a[-120:]).size)
print("  bottom row      :", np.unique(a[-1])[:6])
print("  row -60         :", np.unique(a[-60])[:6])
print("  row -110        :", np.unique(a[-110])[:6])
print("  row -130 (inside):", np.unique(a[-130])[:4], "... count", np.unique(a[-130]).size)
print()
# Is the void a single constant? that is the signature of a GDAL init/pad fill
band=a[-109:]
print("bottom 109 rows: unique values =", np.unique(band).size, "->", np.unique(band)[:5])
right=a[:,-46:]
print("right 46 cols  : unique values =", np.unique(right).size, "->", np.unique(right)[:5])
