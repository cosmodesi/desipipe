#!/usr/bin/env python
import sys
from mpi4py import MPI


mpicomm = MPI.COMM_SELF.Spawn(sys.executable, args=['test_mpi_script_spawn.py'], maxprocs=1)
mpicomm = MPI.COMM_SELF.Spawn(sys.executable, args=['test_mpi_script_spawn.py'], maxprocs=2)