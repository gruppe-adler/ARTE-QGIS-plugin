import sys, numpy as np
sys.path.insert(0,'.')
from scipy.ndimage import distance_transform_edt, label
import roadwater
# Reproduce the exact failing case: 100 m OSM nodes at 8192, scaled to 512
full=8192; small=512; scale=small/float(full)
m_per_fullpx=1.95
step_px=100.0/m_per_fullpx            # ~51 full px between nodes
pts=np.array([[400.0+i*step_px*0.8, 500.0+i*step_px*0.6] for i in range(200)])
for label_,dens in [("OLD (no densify)",False),("NEW (densify 0.5)",True)]:
    p=pts*scale
    if dens and len(p)>=2: p=roadwater.densify(p,step=0.5)
    seeds=np.zeros((small,small),bool)
    rr=np.clip(p[:,0].astype(int),0,small-1); cc=np.clip(p[:,1].astype(int),0,small-1)
    seeds[rr,cc]=True
    _,nseed=label(seeds,structure=np.ones((3,3)))
    mask=distance_transform_edt(~seeds)<=1.0
    _,nblob=label(mask,structure=np.ones((3,3)))
    print("%-20s seeds %5d | seed components %4d | blobs after dilate %4d"%(
        label_,int(seeds.sum()),nseed,nblob))
