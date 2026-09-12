import numpy as np
# Current detector gate: _drop = p50 - p01 > 200.0
# Simulate a LOW-RELIEF map (e.g. plains, 120 m of relief) with a 2% zero border
rng=np.random.default_rng(0)
terr=rng.normal(300,20,(1000,1000)).astype(np.float32)   # ~120 m span
terr[-20:,:]=0.0; terr[:,-20:]=0.0                        # 2% border at 0 m
valid=terr>-10000.0
ref=terr[valid]
p01,p50=np.percentile(ref,[1,50]); drop=p50-p01
print("LOW-RELIEF map with 0 m border:")
print("  p01 %.1f  p50 %.1f  drop %.1f  -> detector fires? %s"%(p01,p50,drop,drop>200.0))
print("  => border pixels: %d, and they would be MISSED"%int((terr==0).sum()))
print()
# And the user's high-relief case, for contrast
terr2=rng.normal(2465,80,(1000,1000)).astype(np.float32)
terr2[-20:,:]=0.0
ref2=terr2[terr2>-10000.0]; p01b,p50b=np.percentile(ref2,[1,50])
print("HIGH-RELIEF (Bamiyan-like): drop %.1f -> fires? %s"%(p50b-p01b,(p50b-p01b)>200.0))
