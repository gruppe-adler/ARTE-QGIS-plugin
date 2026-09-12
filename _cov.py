import math
# The 2200 m export: band widths measured earlier at 8192 px, 0.2686 m/px
px=2200.0/8192
for name,band in [("top",36),("bottom",109),("left",0),("right",46)]:
    print("  %-7s %3d px = %6.1f m"%(name,band,band*px))
print()
# OpenTopography AW3D30 is 1 arc-second ~= 30 m. A 2200 m window is ~73 cells.
print("AW3D30 cell ~30 m; 2200 m window spans ~%.1f source cells"%(2200/30.0))
print("Requested bounds are in metres (projected); the API takes lat/lon.")
print("A half-cell rounding at the source = %.1f m, which is the order of the bands."%(30/2))
