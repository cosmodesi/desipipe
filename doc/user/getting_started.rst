.. _user-getting-started:


Getting started
===============

In this page we will describe **desipipe**'s basics' with a practical example,
but further examples can be found in the provided `notebooks <https://github.com/cosmodesi/desipipe/blob/main/nb>`_.

**desipipe** provides a framework to organize the processing of your analysis.

Write tasks
-----------
The point of **desipipe** is to write all your tasks and resource requests within one Python script.
Let's consider a simple example: the Monte-Carlo estimation of :math:`\pi`.

.. code-block:: python

  def draw_random_numbers(size):
      import numpy as np
      return np.random.uniform(-1, 1, size)

  def fraction(seed=42, size=10000, draw_random_numbers=draw_random_numbers):
      # All definitions, except input parameters, must be in the function itself, or in its arguments
      # and this, recursively:
      # draw_random_numbers is defined above and all definitions, except input parameters, are in the function itself
      # This is required for the tasks to be pickelable (~ can be converted to bytes)
      import time
      import numpy as np
      time.sleep(5)  # wait 5 seconds, just to show jobs are indeed run in parallel
      x, y = draw_random_numbers(size), draw_random_numbers(size)
      return np.sum((x**2 + y**2) < 1.) * 1. / size  # fraction of points in the inner circle of radius 1

  def average(fractions):
      import numpy as np
      return np.average(fractions) * 4.


This is pretty close to what one would have written in Python, except we must take care of including all definitions in the functions to be run.
Typically, modules should be imported in the function, and if there is dependence in other functions, they should be included in the signature (input arguments),
this is the case of ``draw_random_numbers`` here.

Now, to get a reliable estimate of :math:`\pi`, we want to repeate the simulation ``fraction`` many times, and if possible, in parallel.
Let's specify this! We create a queue, that will contain all our tasks (here one task = one ``fraction`` call).

.. code-block:: python

  from desipipe import Queue, Environment, TaskManager, FileManager

  # Let's instantiate a Queue, which records all tasks to be performed
  queue = Queue('test', base_dir='_tests')
  # Pool of 4 workers
  # Any environment variable can be passed to Environment: it will be set when running the tasks below
  tm = TaskManager(queue, environ=Environment(), scheduler=dict(max_workers=4))

  # Left untouched, this is a helper function, not a standalone task
  def draw_random_numbers(size):
      import numpy as np
      return np.random.uniform(-1, 1, size)

  # We decorate the function (task) with tm.python_app
  @tm.python_app
  def fraction(seed=42, size=10000, draw_random_numbers=draw_random_numbers):
      # All definitions, except input parameters, must be in the function itself, or in its arguments
      # and this, recursively:
      # draw_random_numbers is defined above and all definitions, except input parameters, are in the function itself
      # This is required for the tasks to be pickelable (~ can be converted to bytes)
      import time
      time.sleep(5)  # wait 5 seconds, just to show jobs are indeed run in parallel
      x, y = draw_random_numbers(size), draw_random_numbers(size)
      return np.sum((x**2 + y**2) < 1.) * 1. / size  # fraction of points in the inner circle of radius 1

  # Here we use another task manager, with only 1 worker
  tm2 = tm.clone(scheduler=dict(max_workers=1))
  @tm2.python_app
  def average(fractions):
      import numpy as np
      return np.average(fractions) * 4.

  # Let's add another task, to be run with bash
  @tm2.bash_app
  def echo(avg):
      return ['echo', '-n', 'bash app says pi is ~ {:.4f}'.format(avg)]

  # The following line stacks all the tasks in the queue
  fractions = [fraction(seed=i) for i in range(20)]
  # fractions is a list of Future instances
  # We can pass them to other tasks, which creates a dependency graph
  avg = average(fractions)
  ech = echo(avg)

Running the script above stacks all tasks in the queue. ``fraction`` tasks will be 'PENDING' (waiting to be run),
while ``average`` tasks will be 'WAITING' for the former to complete. ``echo`` also depends on ``average``.
Running the script above will write a :class:`desipipe.Queue` on disk, with name 'test', in the directory ``_tests``
(by default, it is ``${HOME}/.desipipe/queues/${USERNAME}/``).

Now, we can spawn a manager process that will run the above tasks (in PENDING state), following the specifications of the task managers.


.. code-block:: python

  spawn(queue)


One can interact with ``queue`` from python directly, e.g.: :meth:`Queue.tasks` to list tasks,
:meth:`Queue.pause` to pause the queue, :meth:`Queue.resume` to resume the queue, etc.
Usually though, one will use the command line: see the cheat list below.

.. note::

  To play with the above example, and in particular discover tips in case you want to rerun selected tasks only,
  see this `notebook <https://github.com/cosmodesi/desipipe/blob/main/nb/task_manager_examples.ipynb>`_.


NERSC
-----
There is already a provider and environment implemented for NERSC. See the example below.

.. code-block:: python

  from desipipe import Queue, Environment, TaskManager, FileManager

  # Let's instantiate a Queue, which records all tasks to be performed
  queue = Queue('my_queue')
  environ = Environment('nersc-cosmodesi')  # nersc-cosmodesi environment, set up for DESI
  tm = TaskManager(queue=queue, environ=environ)
  # Pool of 30 workers (max_workers=30), each running on 1 CPU node (nodes_per_worker=1), each with 64 MPI processes (mpiprocs_per_worker=64) for 30 minutes (time='00:30:00')
  # Slurm output / error files written in _sbatch directory
  tm_power = tm.clone(scheduler=dict(max_workers=30), provider=dict(provider='nersc', time='00:30:00', mpiprocs_per_worker=64, nodes_per_worker=1, output='_sbatch/slurm-%j.out', error='_sbatch/slurm-%j.err'))
  # Pool of 4 workers (max_workers=4), each running on 1 GPU node (nodes_per_worker=1), each with 1 MPI process (mpiprocs_per_worker=1) for 10 minutes (time='00:10:00')
  tm_corr = tm.clone(scheduler=dict(max_workers=4), provider=dict(provider='nersc', time='00:10:00', mpiprocs_per_worker=1, output='_sbatch/slurm-%j.out', error='_sbatch/slurm-%j.err', constraint='gpu'))
  # Pool of 10 workers (max_workers=10), each running on 1 / 5 of a CPU node (nodes_per_worker=0.2), each with 8 MPI processes (mpiprocs_per_worker=8) for one hour (time='01:00:00')
  tm_fit = tm.clone(scheduler=dict(max_workers=10), provider=dict(provider='nersc', time='01:00:00', mpiprocs_per_worker=8, nodes_per_worker=0.2, output='_sbatch/slurm-%j.out', error='_sbatch/slurm-%j.err'))

  @tm_power.python_app
  def compute_power_spectrum(...):
    ...

  @tm_fit.python_app
  def profile(...):
    ...


Run this script, and then:

.. code-block:: bash

  desipipe spawn -q my_queue --spawn


Warning
-------

To avoid too frequent requests to 'sacct' (showing the list of submitted jobs), specify ``--timestep 60``;
this will call 'sacct' every 60 seconds.


Cheat list
----------

To know more about the options for the commands below, use ``--help``!, e.g.:

.. code-block:: bash

  desipipe tasks --help


Print queues
~~~~~~~~~~~~

.. code-block:: bash

  desipipe queues -q '*/*'

Print the list of all your queues.

Spawn a manager process
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

  desipipe spawn -q my_queue --spawn

is the equivalent of the Python code:

.. code-block:: python

  spawn(queue, spawn=True)

This command is the one to "get the work job done".
Specifically, it spawns a manager process that distributes the tasks, in PENDING state, among workers.

Pause a queue
~~~~~~~~~~~~~

.. code-block:: bash

  desipipe pause -q my_queue

When pausing a queue, all processes running tasks from this queue will stop (after they finish their current task).

Resume a queue
~~~~~~~~~~~~~~

.. code-block:: bash

  desipipe resume -q my_queue   # pass --spawn to spawn a manager process that will distribute the tasks among workers

This is the opposite of ``pause``. When resuming a queue, PENDING tasks can get processed again (if a manager process is running).

Print tasks in queue
~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

  desipipe tasks -q my_queue

Task state can be:

  - 'WAITING': Waiting for requirements (other tasks) to finish.
  - 'PENDING': Eligible to be selected and run.
  - 'RUNNING': Running right now (out and err are updated live).
  - 'SUCCEEDED': Finished with errno = 0. All good!
  - 'FAILED': Finished with errno != 0. This means the code raised an exception.
  - 'KILLED': Killed. Typically when the task has not had time to finish, because the requested amount of time (if any) was not sufficient. May be raised by out-of-memory as well.
  - 'UNKNOWN': The task has been in 'RUNNING' state longer than the requested amount of time (if any) in the provider. This means that **desipipe** could not properly update the task state before the job was killed, typically because the job ran out-of-time. If you scheduled the requested time to be able to fit in multiple tasks, you may just want to retry running these tasks (see below).


Retry tasks
~~~~~~~~~~~

.. code-block:: bash

  desipipe retry -q my_queue --state KILLED

Tasks for which state is 'KILLED', and only those tasks, are changed to 'PENDING', i.e. they will be processed again.

Kill tasks
~~~~~~~~~~

.. code-block:: bash

  desipipe kill -q my_queue

Kills tasks of the queue (including manager processes).

Delete queue(s)
~~~~~~~~~~~~~~~

.. code-block:: bash

  desipipe delete -q 'my_*'  # pass --force to actually delete the queue


Example & troubleshooting
-------------------------

Let's consider this queue.

.. code-block:: bash

  $ desipipe queues -q my_queue
  [000000.02]  11-28 21:14  desipipe                  INFO     Matching queues:
  [000000.07]  11-28 21:14  desipipe                  INFO     Queue(size=116, state=ACTIVE, filename=.../my_queue.sqlite)
  WAITING   : 0
  PENDING   : 1
  RUNNING   : 0
  SUCCEEDED : 107
  FAILED    : 0
  KILLED    : 7
  UNKNOWN   : 1

Out of the 116 tasks, 107 have been processed and 'SUCCEEDED' already, good!

7 tasks are 'KILLED'. It is probably because they have not finished in the required time.
You can check their output (if any) and errors with:

.. code-block:: bash

  $ desipipe tasks -q my_queue --state KILLED

If that is not enough to understand why they have been killed and you need to review e.g. the input parameters or the code processed by these tasks, go for Python:

.. code-block:: python

  queue = Queue('my_queue')
  for task in queue.tasks(state='KILLED'):
      print(task.kwargs)
      print(task.app.code)  # code that is run
      task.run()  # run the task; you can pass any argument to override those in kwargs

You might also want to do that if you had 'FAILED' tasks.

There is 1 'UNKNOWN' task. This means that **desipipe** could not properly update the task state before the job was killed, typically because the job ran out-of-time.
Note that, in this very case, the issue encountered by the task with 'UNKNOWN' state may be the same as for the 7 'KILLED' tasks.
Anyway, you might just want to retry running this task:

.. code-block:: bash

  desipipe retry -q my_queue --state UNKNOWN

In practice, the command switches the task(s) with state 'UNKNOWN' (and only those tasks) to 'PENDING', so they can be processed again.
However, if the manager process is not running anymore, you may need to restart it --- read just below.

There is 1 'PENDING' task, i.e. to be processed. If it never goes to running, it may mean that for some reason the manager process has been killed.
On Unix systems, you can typically check that with:

.. code-block:: bash

  top -p $(pgrep -d',' -f "desipipe")

If nothing is returned, that means the manager process does not exist anymore. You can just spawn a new one:

.. code-block:: bash

  desipipe spawn -q my_queue --spawn

In case of **emergency** (nothing working anymore), you can manually delete the queue sqlite file ``.../my_queue.sqlite``, that you can get with:

.. code-block:: bash

  $ desipipe queues -q my_queue
  [000000.02]  11-28 21:14  desipipe                  INFO     Matching queues:
  [000000.07]  11-28 21:14  desipipe                  INFO     Queue(size=116, state=ACTIVE, filename=.../my_queue.sqlite)

That will eventually kill all jobs associated with this queue.