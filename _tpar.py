import numpy as np, erosion, time
if __name__ == '__main__':
    rng=np.random.default_rng(0)
    N=2048
    y,x=np.mgrid[0:N,0:N]/float(N)
    t=(np.sin(x*6)*np.cos(y*5)*0.3+(1-abs(x-0.5)*2)*0.5+rng.normal(0,0.01,(N,N))).astype(np.float32)
    t=(t-t.min())/(t.max()-t.min())
    p=erosion.resolve_params('moderate',t.shape); p['ttl']=128
    print('map %dx%d  particles=%d ttl=%d' % (N,N,p['n_particles'],p['ttl']))
    t0=time.time(); a=erosion.simulate(t,p,seed=1); ts=time.time()-t0
    print('serial   %.1fs' % ts)
    t0=time.time(); b=erosion.simulate_parallel(t,p,seed=1,workers=11); tp=time.time()-t0
    print('parallel %.1fs  speedup %.2fx' % (tp, ts/tp))
    print('result diff mean|d| %.6f' % np.abs(a-b).mean())
    from scipy.ndimage import gaussian_filter
    print('clustering serial %.4f parallel %.4f' % (
        gaussian_filter((a-t<0).astype(np.float32),3).std(),
        gaussian_filter((b-t<0).astype(np.float32),3).std()))
