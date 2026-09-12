import numpy as np, erosion, time
from scipy.ndimage import gaussian_filter
rng = np.random.default_rng(0)
N=512
y,x = np.mgrid[0:N,0:N]/float(N)
t = (np.sin(x*6)*np.cos(y*5)*0.3 + (1-abs(x-0.5)*2)*0.5 + rng.normal(0,0.01,(N,N))).astype(np.float32)
t = (t-t.min())/(t.max()-t.min())
base_rnd = None
print('%-6s %-8s %-9s %-9s %-8s' % ('ttl','clust','random','ratio','secs'))
for ttl in [32, 64, 128, 256]:
    p = erosion.resolve_params('moderate', t.shape)
    p['ttl'] = ttl
    t0=time.time()
    out = erosion.simulate(t, p, seed=1)
    el=time.time()-t0
    d = out-t
    e = (d<0).astype(np.float32)
    c = gaussian_filter(e,3).std()
    rnd = (rng.random((N,N))<e.mean()).astype(np.float32)
    r = gaussian_filter(rnd,3).std()
    print('%-6d %-8.4f %-9.4f %-9.2f %-8.1f' % (ttl, c, r, c/r, el))
