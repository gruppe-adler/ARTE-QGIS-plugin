import sys
print('python', sys.version.split()[0])
for m in ['numba','cupy','torch','pyopencl','taichi']:
    try:
        mod=__import__(m); print('%-10s %s' % (m, getattr(mod,'__version__','?')))
    except ImportError:
        print('%-10s MISSING' % m)
import os, multiprocessing as mp
print('cpu cores:', mp.cpu_count())
try:
    import numpy as np
    print('numpy threads config:'); np.show_config()
except Exception as e: print(e)
