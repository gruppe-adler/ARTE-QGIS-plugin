import numpy as np, erosion, time
from scipy.ndimage import gaussian_filter
# Can we split the map into tiles and erode them independently?
# Test: does tiling with overlap produce comparable results to whole-map?
rng=np.random.default_rng(0)
N=512
y,x=np.mgrid[0:N,0:N]/float(N)
t=(np.sin(x*6)*np.cos(y*5)*0.3+(1-abs(x-0.5)*2)*0.5+rng.normal(0,0.01,(N,N))).astype(np.float32)
t=(t-t.min())/(t.max()-t.min())
p=erosion.resolve_params('moderate',t.shape); p['ttl']=128
t0=time.time(); whole=erosion.simulate(t,p,seed=1); tw=time.time()-t0
print('whole-map: %.2fs  std %.5f' % (tw, (whole-t).std()))
# tiled 2x2 with 64px overlap, blended
OV=64
def tile_erode(arr, p, seed):
    H,W=arr.shape; out=arr.copy(); half=H//2
    acc=np.zeros_like(arr); wsum=np.zeros_like(arr)
    for iy in range(2):
        for ix in range(2):
            y0=max(0,iy*half-OV); y1=min(H,(iy+1)*half+OV)
            x0=max(0,ix*half-OV); x1=min(W,(ix+1)*half+OV)
            sub=arr[y0:y1,x0:x1].copy()
            pp=dict(p); pp['n_particles']=int(p['n_particles']*sub.size/arr.size)
            er=erosion.simulate(sub,pp,seed=seed+iy*2+ix)
            wmask=np.ones_like(sub)
            # feather edges
            for k in range(OV):
                f=k/OV
                if y0>0: wmask[k,:]*=f
                if y1<H: wmask[-1-k,:]*=f
                if x0>0: wmask[:,k]*=f
                if x1<W: wmask[:,-1-k]*=f
            acc[y0:y1,x0:x1]+=er*wmask; wsum[y0:y1,x0:x1]+=wmask
    return acc/np.maximum(wsum,1e-6)
t0=time.time(); tiled=tile_erode(t,p,1); tt=time.time()-t0
print('tiled 2x2: %.2fs  std %.5f' % (tt,(tiled-t).std()))
print('difference between whole and tiled: mean|d| %.6f  max %.4f' % (np.abs(whole-tiled).mean(), np.abs(whole-tiled).max()))
e1=gaussian_filter((whole-t<0).astype(np.float32),3).std()
e2=gaussian_filter((tiled-t<0).astype(np.float32),3).std()
print('channel clustering whole %.4f  tiled %.4f' % (e1,e2))
