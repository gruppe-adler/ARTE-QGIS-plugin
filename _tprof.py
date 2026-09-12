import numpy as np, erosion, cProfile, pstats, io
rng=np.random.default_rng(0)
N=512
y,x=np.mgrid[0:N,0:N]/float(N)
t=(np.sin(x*6)*np.cos(y*5)*0.3+(1-abs(x-0.5)*2)*0.5+rng.normal(0,0.01,(N,N))).astype(np.float32)
t=(t-t.min())/(t.max()-t.min())
p=erosion.resolve_params('moderate',t.shape); p['ttl']=128
pr=cProfile.Profile(); pr.enable()
erosion.simulate(t,p,seed=1)
pr.disable()
s=io.StringIO(); ps=pstats.Stats(pr,stream=s).sort_stats('cumulative'); ps.print_stats(12)
print(s.getvalue()[:2200])
