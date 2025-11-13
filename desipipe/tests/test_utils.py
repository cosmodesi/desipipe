import re
import sys
import uuid
import pickle
import hashlib
import inspect


def func(a, b, c=2):
    import numpy as np
    return a * b * c


def test_uid():
    uid = pickle.dumps((func, (1, 2), {'c': 3}))
    hex = hashlib.md5(uid).hexdigest()
    id = uuid.UUID(hex=hex)
    print(id)


def test_subprocess():
    import os
    import subprocess
    proc = subprocess.Popen('mpiexec -np 1 python test_mpi.py'.split(), env={**os.environ, 'JOBID': 'TEST'})
    out, err = proc.communicate()
    print(out)


def test_import_code():
    code = inspect.getsource(func)
    for m in re.findall('[\n;\s]*from\s+([^\s.]*)\s+import', code) + re.findall('[\n;\s]*import\s+([^\s.]*)\s*', code):
        print(m)


def test_import():

    def select_modules(modules):
        return set(name.split('.', maxsplit=1)[0] for name in modules if not name.startswith('_'))

    modules_bak = select_modules(sys.modules)
    func(1, 1)
    versions = []
    for name in select_modules(sys.modules) - modules_bak:
        try:
            versions.append((name, sys.modules[name].__version__))
        except (KeyError, AttributeError):
            pass
    print(versions)


if __name__ == '__main__':

    #test_uid()
    #test_subprocess()
    #test_import_code()
    test_import()