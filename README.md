# desipipe

**desipipe** is an attempt to provide a common framework for running DESI clustering analyses,
handling files, submitting jobs, etc. within Python alone.

Example notebooks presenting most use cases are provided in directory nb/.

## Documentation

Documentation in construction on Read the Docs, [desipipe docs](https://desipipe.readthedocs.io/).
See in particular [getting started](https://desipipe.readthedocs.io/en/latest/user/getting_started.html).


## 🧪 Quick Example: running a job at NERSC


You just need to define the following script.

```python

from pathlib import Path
from desipipe import Queue, Environment, TaskManager, spawn, setup_logging

setup_logging()

queue = Queue('my_queue')  # give a name to the queue
queue.clear(kill=False)  # remove previous queue

# To customize the environment, you can add data={name of environment variable: value of environment variable}, command=[list of bash commands, e.g. "module swap mymodule"]
environ = Environment('nersc-cosmodesi', data={'OMP_NUM_THREADS': 8})

file_dir = Path('_sbash_test')
tm = TaskManager(queue=queue, environ=environ)
# maximum of 50 workers (number of "computing units" which will process tasks in parallel)
# 0.5 node per worker (i.e. 2 workers per node), 12 MPI processes per worker
# killed_at_timeout = True: if task is running out of time, it is considered killed and not re-submitted
# killed_at_timeout = False: task is resubmitted automatically (e.g. useful for MCMC chains which write the results periodically to disk)
tm = tm.clone(scheduler=dict(max_workers=50), provider=dict(provider='nersc', time='00:01:00', mpiprocs_per_worker=12, nodes_per_worker=0.2, output=file_dir / 'slurm-%j.out', error=file_dir / 'slurm-%j.err', killed_at_timeout=True))

@tm.python_app
def test(i, do_some_computation=lambda x: x):
    # All definitions, including imports, except input parameters, must be in the function itself, or in its arguments
    # and this, recursively:
    # do_some_computation is defined above and all definitions, except input parameters, are in the function itself
    # This is required for the tasks to be pickelable (~ can be converted to bytes)
    # And generally good for reproducibility.
    import time
    time.sleep(10)
    print('SUCCEEDED')
    return do_some_calculation(1)


if __name__ == '__main__':
    for i in range(10):
        test(i)

```

Running the script above stacks all tasks in the queue. You can spawn workers (= submit jobs) with:

```bash

  desipipe spawn -q my_queue --spawn --timestep 20

```

Note: To avoid too frequent requests to 'sacct' (showing the list of submitted jobs), specify ``--timestep 60``; this will call 'sacct' every 60 seconds.


## Requirements

## Installation

### pip

Simply run:
```
python -m pip install git+https://github.com/cosmodesi/desipipe
```

### git

First:
```
git clone https://github.com/cosmodesi/desipipe.git
```
To install the code:
```
pip install --user .
```
Or in development mode (any change to Python code will take place immediately):
```
pip install --user --editable .
```

## License

**despipe** is free software distributed under a BSD3 license. For details see the [LICENSE](https://github.com/cosmodesi/desipipe/blob/main/LICENSE).


## Acknowledgments

- Inspiration from parsl: https://github.com/Parsl/parsl
- Inspiration from qdo: https://bitbucket.org/berkeleylab/qdo
- Stephen Bailey, Julien Guy, Pat McDonald, Martin White for discussions
