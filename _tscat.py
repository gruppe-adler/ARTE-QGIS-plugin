import numpy as np, time
for N in [512, 2048, 8192]:
    size=N*N
    flat=np.zeros(size,np.float32)
    for npart in [50000, 200000]:
        idx=np.random.randint(0,size,npart).astype(np.int64)
        vals=np.random.random(npart).astype(np.float32)
        t0=time.perf_counter()
        for _ in range(5): np.add.at(flat,idx,vals)
        t_at=(time.perf_counter()-t0)/5
        t0=time.perf_counter()
        for _ in range(5): flat+=np.bincount(idx,weights=vals,minlength=size).astype(np.float32)
        t_bc=(time.perf_counter()-t0)/5
        print('%5d^2 npart=%7d  add.at %7.4fs  bincount %7.4fs  -> %s' % (
            N,npart,t_at,t_bc,'bincount' if t_bc<t_at else 'add.at'))
