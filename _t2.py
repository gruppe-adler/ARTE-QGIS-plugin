import numpy as np, erosion
rng = np.random.default_rng(0)
y, x = np.mgrid[0:256, 0:256] / 256.0
t = ((1 - abs(x-0.5)*2)*0.5 + rng.normal(0,0.005,(256,256))).astype(np.float32)
t = (t - t.min())/(t.max()-t.min())

# instrument a single wave: how far do particles actually travel?
p = erosion.resolve_params('moderate', t.shape)
p['n_particles'] = 20000
h = t.astype(np.float32).copy()
w_ = h.shape[1]
rng2 = np.random.default_rng(1)
X = rng2.uniform(0, w_-1, 20000).astype(np.float32)
Y = rng2.uniform(0, 255, 20000).astype(np.float32)
sx, sy = X.copy(), Y.copy()
dx = np.zeros(20000, np.float32); dy = np.zeros(20000, np.float32)
vel = np.full(20000, 0.9, np.float32); water = np.full(20000,1.0,np.float32)
alive = np.ones(20000, bool)
steps_alive = np.zeros(20000, int)
for s in range(32):
    idx = np.nonzero(alive)[0]
    if len(idx)==0: break
    cx,cy = X[idx],Y[idx]
    ht,gx,gy = erosion._bilinear(h,cx,cy)
    ndx = dx[idx]*0.3 - gx*0.7; ndy = dy[idx]*0.3 - gy*0.7
    nrm = np.hypot(ndx,ndy); nz = nrm>1e-8
    ndx=np.where(nz,ndx/np.maximum(nrm,1e-8),0); ndy=np.where(nz,ndy/np.maximum(nrm,1e-8),0)
    nx=cx+ndx; ny=cy+ndy
    oob=(nx<0)|(nx>=w_-1)|(ny<0)|(ny>=255)|~nz
    X[idx],Y[idx]=nx,ny; dx[idx],dy[idx]=ndx,ndy
    water[idx]*=0.98
    steps_alive[idx]+=1
    alive[idx[oob|(water[idx]<0.01)]]=False
dist = np.hypot(X-sx, Y-sy)
print('particles still alive at end: %d / 20000' % alive.sum())
print('steps survived: mean %.1f  median %.0f  (ttl=32)' % (steps_alive.mean(), np.median(steps_alive)))
print('displacement:   mean %.2f px  max %.1f px' % (dist.mean(), dist.max()))
print('gradient magnitude on this ramp: %.6f' % np.abs(np.gradient(t)[1]).mean())
