import numpy as np
rng=np.random.default_rng(0)
print("Case: coastal map where the pad value (0 m) is ALSO real sea level")
terr=rng.normal(60,25,(1000,1000)).astype(np.float32)
terr=np.clip(terr,0,None)                 # real sea level exists at 0
terr[-20:,:]=0.0                          # pad, indistinguishable
ref=terr[terr>-10000.0]; p01,p50=np.percentile(ref,[1,50]); drop=p50-p01
print("  drop %.1f -> fires? %s   (pad and real sea level are the same value)"%(drop,drop>200.0))
print()
print("Case: small void (<1%) on a high-relief map")
t2=rng.normal(2465,80,(1000,1000)).astype(np.float32)
t2[-5:,:]=0.0                             # 0.5% border
ref2=t2[t2>-10000.0]; a,b=np.percentile(ref2,[1,50])
print("  p01 %.1f (inside real terrain, void is under 1%%)  drop %.1f -> fires? %s"%(a,b-a,(b-a)>200.0))
sus=t2<(a+(b-a)*0.25)
print("  suspect threshold %.1f catches %d of the %d void px"%(a+(b-a)*0.25,int((sus&(t2==0)).sum()),int((t2==0).sum())))
