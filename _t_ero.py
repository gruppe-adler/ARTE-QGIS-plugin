import numpy as np, erosion
from scipy.ndimage import gaussian_filter
rng = np.random.default_rng(0)
y, x = np.mgrid[0:512, 0:512] / 512.0
t = (np.sin(x*6)*np.cos(y*5)*0.3 + (1-abs(x-0.5)*2)*0.5 + rng.normal(0,0.01,(512,512))).astype(np.float32)
t = (t - t.min())/(t.max()-t.min())
p = erosion.resolve_params('moderate', t.shape)
out = erosion.simulate(t, p, seed=1)
d = out - t
print('delta range %.4f .. %.4f' % (d.min(), d.max()))
print('eroded %.1f%%  deposited %.1f%%' % (100*(d<0).mean(), 100*(d>0).mean()))
print('net volume change %.6f' % d.mean())
lo = t < np.percentile(t,33); hi = t > np.percentile(t,66)
print('mean delta LOW third  %.6f' % d[lo].mean())
print('mean delta HIGH third %.6f' % d[hi].mean())
e = (d<0).astype(np.float32)
print('erosion clustering %.4f' % gaussian_filter(e,3).std())
rnd = (rng.random((512,512)) < e.mean()).astype(np.float32)
print('random baseline    %.4f' % gaussian_filter(rnd,3).std())
