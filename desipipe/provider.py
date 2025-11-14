import os
import time
import copy
import random
import subprocess
import functools
from shlex import split

from .utils import BaseClass
from . import utils


class RegisteredProvider(type(BaseClass)):

    """Metaclass registering :class:`BaseProvider`-derived classes."""

    _registry = {}

    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)
        meta._registry[cls.name] = cls
        return cls


def time_lru_cache(func):
    """
    Wrapper that caches function result for a ``dt`` second time period maximum.
    Idea taken from https://stackoverflow.com/questions/31771286/python-in-memory-cache-with-time-to-live
    """
    def get_ttl_hash(timestep=1):
        """Return the same value withing ``dt`` second time period."""
        return round(time.time() / timestep)

    @functools.lru_cache()
    def my_func(*args, ttl_hash=None, **kwargs):
        return func(*args, **kwargs)

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        return my_func(self, *args, ttl_hash=get_ttl_hash(timestep=self.timestep), **kwargs)

    return wrapper


class BaseProvider(BaseClass, metaclass=RegisteredProvider):

    """Base computing resource provider class, that runs commands on the specific computer / cluster."""

    name = 'base'
    _defaults = {'stop_after': None, 'timestep': 1.0}

    def __init__(self, environ=None, **kwargs):
        """
        Initialize provider.

        Parameters
        ----------
        environ : BaseEnvironment, str, dict, default=None
            Environment, see :func:`get_environ`.

        **kwargs : dict
            Other attributes, to replace values in :attr:`_defaults`.
        """
        for name, value in self._defaults.items():
            setattr(self, name, copy.copy(value))

        self.update(**{'environ': environ, **kwargs})
        self.processes = []

    @classmethod
    def jobid(cls):
        """This worker's job ID."""
        return 0

    @classmethod
    def kill(cls, *jobids):
        """Kill input job IDs."""

    def update(self, **kwargs):
        """Update provider with input attributes."""
        for name, value in kwargs.items():
            if name == 'environ':
                from .environment import get_environ
                self.environ = get_environ(value)
            elif name in self._defaults:
                vt = type(self._defaults[name])
                try: value = vt(value)
                except TypeError: pass
                setattr(self, name, value)
            else:
                raise ValueError('Unknown argument {}; supports {}'.format(name, list(self._defaults)))

    def __setstate__(self, state):
        self.__dict__.update(self._defaults | state)

    def clear(self):
        """Clear, i.e. delete information (typically job IDs) from current run."""
        pass

    @time_lru_cache
    def jobids(self, state=('PENDING', 'RUNNING'), return_nworkers=False):
        """List of workers, from oldest to newest."""
        return []

    def nworkers(self, of='workers', state=('PENDING', 'RUNNING')):
        """Number of running workers."""
        return len(self.jobids(state=state))

    def cost(self, workers=1):
        """
        Compute cost associated to the input number of workers (in addition to running ones).
        Only cost variations matter (not the absolute cost):
        constant cost triggers more workers, increasing cost penalizes more workers.
        """
        return 0

    def __call__(self, cmd, workers=1):
        """Submit input command ``cmd`` on ``workers`` workers."""
        raise NotImplementedError

    @property
    def timeout(self):
        """Job times out after this number of seconds."""
        return 1e20


def get_provider(provider=None, **kwargs):
    """
    Convenient function that returns the provider.

    Parameters
    ----------
    provider : BaseProvider, str, dict, default=None
        A :class:`BaseProvider` instance, which is then returned directly,
        a string specifying the name of the provider (e.g. 'local')
        or a dictionary of provider attributes.
        If not specified, the default provider in desipipe's configuration
        (see :class:`Config`) is used if provided, else 'local'.

    **kwargs : dict
        Optionally, additional provider attributes.

    Returns
    -------
    provider : BaseProvider
    """
    if isinstance(provider, BaseProvider):
        return provider
    if isinstance(provider, dict):
        provider, kwargs = provider.pop('provider', None), {**provider, **kwargs}
    if provider is None:
        from .config import Config
        provider = Config().get('provider', 'local')
    return BaseProvider._registry[provider](**kwargs)


Provider = get_provider


class LocalProvider(BaseProvider):
    """
    Local provider: input commands are executed as subprocesses.

    Parameters
    ----------
    mpiprocs_per_worker : int, default=1
        Number of MPI processes per worker.

    mpiexec : str, default='mpiexec -np {mpiprocs:d}'
        Template to run a command with MPI.
    """
    name = 'local'
    _defaults = {**BaseProvider._defaults, 'mpiprocs_per_worker': 1, 'mpiexec': 'mpiexec -np {mpiprocs:d} {cmd}'}

    @classmethod
    def jobid(cls):
        """This worker's job ID."""
        return os.getpid()

    @classmethod
    def kill(cls, *jobids):
        """Kill input job IDs."""
        import signal
        for jobid in jobids:
            try:
                os.kill(int(jobid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def __call__(self, cmd, workers=1):
        """Submit input command ``cmd`` on ``workers`` workers."""
        environ = {**os.environ, **self.environ.to_dict(all=True)}
        for worker in range(workers):
            tmp = cmd
            if self.mpiprocs_per_worker > 1:
                tmp = self.mpiexec.format(mpiprocs=self.mpiprocs_per_worker, cmd=tmp)
            self.processes.append(subprocess.Popen(split(tmp), start_new_session=True, env=environ))
            #time.sleep(random.uniform(0.8, 1.2))

    def clear(self):
        """Clear, i.e. delete information (processes) from current run."""
        self.processes = []

    @time_lru_cache
    def jobids(self, state=('PENDING', 'RUNNING'), return_nworkers=False):
        """List of workers, from oldest to newest."""

        allowed_state = ['PENDING', 'RUNNING']
        if utils.is_sequence(state): states = [s.upper() for s in state]
        else: states = [state.upper()]
        for state in states:
            if state == 'ALL':
                if return_nworkers:
                    return [(None, 1)] * len(self.processes)
                return [None] * len(self.processes)
            if state not in allowed_state:
                raise ValueError('state must be one of {}, found {}'.format(allowed_state, state))

        if 'RUNNING' in states:
            nrunning = sum(process.poll() is None for process in self.processes)
            if return_nworkers:
                return [(None, 1)] * nrunning
            return [None] * nrunning
        return []

    def nworkers(self, of='workers', state=('PENDING', 'RUNNING')):
        """Number of running workers."""
        return len(self.jobids(state=state))

    def cost(self, workers=1):
        """
        Compute cost associated to the input number of workers.
        Cost is constant, then increases steeply when
        the total number of processes (workers times MPI) reaches the number of CPU counts.
        """
        nprocs = workers * self.mpiprocs_per_worker
        ncpus = os.cpu_count()
        if nprocs < ncpus:
            return 0.
        if nprocs == ncpus:
            return 5.
        return 10.


def decode_slurm_time(s):
    """Decode Slurm time."""
    allowed = '"minutes", "minutes:seconds", "hours:minutes:seconds", "days-hours", "days-hours:minutes" and "days-hours:minutes:seconds"'
    days_hours_minutes_seconds = [0, 0, 0, 0]
    ss = s.split(':')
    if len(ss) > 3:
        raise ValueError('unknown time format {}, can only decode {}'.format(s, allowed))
    days_hours = ss[0].split('-')
    if len(days_hours) > 2:
        raise ValueError('unknown time format {}, can only decode {}'.format(s, allowed))
    if len(days_hours) == 2:  # minutes
        days_hours_minutes_seconds[:2] = days_hours
        ss = ss[1:]  # days-hours
    if len(ss) <= 2:  # minutes, minutes:seconds
        days_hours_minutes_seconds[2:len(ss) + 2] = ss
    else:  # hours:minutes:seconds
        days_hours_minutes_seconds[1:len(ss) + 1] = ss
    try:
        days_hours_minutes_seconds = [int(s) for s in days_hours_minutes_seconds]
    except ValueError as exc:
        raise ValueError('unknown time format {}, can only decode {}'.format(s, allowed)) from exc
    return tuple(days_hours_minutes_seconds)


class SlurmProvider(BaseProvider):
    """
    Slurm provider: input commands are submitted as Slurm jobs.

    Parameters
    ----------
    account : str, default='desi'
        Account.

    constraint : str, default='cpu'
        Run on CPU ('cpu') or GPU ('gpu') nodes.

    queue : str, default='regular'
        Name of job queue.

    time : str, default='01:00:00'
        Time to be allocated for a particuler worker.
        Best is to take the estimated time for the task (with some margin),
        or an integer times this time (in case of short tasks).

    nodes_per_workers : float, default=1.
        Number of nodes to reserve for each worker; must be > 0.
        If a float, workers are piled up as MPI processes on the number of nodes :meth:`nodes`.

    mpiprocs_per_worker : int, default=1
        Number of MPI processes per worker.

    mpiexec : str, default='srun -N {nodes:d} -n {mpiprocs:d} --cpu-bind=cores {cmd}'
        Template to run a command with MPI.
    """
    name = 'slurm'
    _defaults = {**BaseProvider._defaults, 'sqs': None, 'qos': 'regular', 'time': '01:00:00', 'nodes_per_worker': 1., 'mpiprocs_per_worker': 1, 'output': '/dev/null', 'error': '/dev/null', 'mpiexec': 'srun --unbuffered -N {nodes:d} -n {mpiprocs:d} {cmd}', 'signal': 'SIGTERM@30', 'killed_at_timeout': None, 'kwargs': {}}

    def update(self, **kwargs):
        """Update provider with input attributes."""
        super(SlurmProvider, self).update(**kwargs)
        self.time = '{:d}-{:d}:{:d}:{:d}'.format(*decode_slurm_time(self.time))  # days-hours:minutes:seconds
        self.get_sqs()

    def get_sqs(self):
        # Backward-compatibility
        self.sqs = getattr(self, 'sqs', None)
        if self.sqs is None:
            user = self.environ.get('USER', '')
            if not user:
                import getpass
                user = getpass.getuser()
            self.sqs = ['sacct', '-u', user, '-X', '-b']
        else:
            if isinstance(self.sqs, str):
                self.sqs = split(self.sqs)
            self.sqs = list(self.sqs)
        return self.sqs

    @classmethod
    def jobid(cls):
        """This worker's job ID."""
        return os.environ.get('SLURM_JOB_ID', '')

    @classmethod
    def kill(cls, *jobids):
        """Kill input job IDs."""
        if jobids:
            subprocess.run(['scancel'] + [str(jobid) for jobid in jobids])

    def __call__(self, cmd, workers=1):
        """Submit input command ``cmd`` on ``workers`` workers."""
        if self.nodes_per_worker <= 0.:
            raise ValueError('Cannot set nodes_per_worker <= 0.')
        nodes = self.nodes(workers=workers)
        if int(self.nodes_per_worker) != self.nodes_per_worker:  # stack jobs
            cmd = self.mpiexec.format(nodes=nodes, mpiprocs=self.mpiprocs_per_worker * workers, cmd=cmd) + ' --mpisplits {:d}'.format(workers)
        else:
            cmd = [self.mpiexec.format(nodes=int(self.nodes_per_worker), mpiprocs=self.mpiprocs_per_worker, cmd=cmd)] * workers
            cmd = ' & '.join(cmd)
            if workers: cmd += ' & wait'
        cmd = self.environ.to_script(sep=' ; ') + ' ; ' + cmd
        kwargs = []
        for name, value in self.kwargs.items(): kwargs += ['--{}'.format(name), str(value)]
        # -- parsable to get jobid (optionally, cluster name)
        # -- wrap to pass the job
        cmd = ['sbatch', '--output', str(self.output), '--error', str(self.error), '--account', str(self.account), '--constraint', str(self.constraint), '--qos', str(self.qos), '--time', str(self.time), '--nodes', str(nodes), '--signal', str(self.signal), '--parsable'] + kwargs + ['--wrap', cmd]
        proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.processes.append((proc.stdout.split(',')[0].strip(), nodes, workers))  # jobid, workers

    def clear(self):
        """Clear, i.e. delete information (processes) from current run."""
        self.processes = []

    @time_lru_cache
    def jobids(self, state=('PENDING', 'RUNNING'), return_nworkers=False):
        """List of workers, from oldest to newest."""
        allowed_state = ['PENDING', 'RUNNING']
        if utils.is_sequence(state): states = [s.upper() for s in state]
        else: states = [state.upper()]

        for state in states:
            if state == 'ALL':
                if return_nworkers:
                    return [(jobid, workers) for jobid, nodes, workers in self.processes]
                return [jobid for jobid, nodes, workers in self.processes]
            if state not in allowed_state:
                raise ValueError('state must be one of {}, found {}'.format(allowed_state, state))
        try:
            sqs = subprocess.run(self.get_sqs(), check=True, stdout=subprocess.PIPE, text=True).stdout.split('\n')
        except subprocess.CalledProcessError:
            jobids = getattr(self, '_jobids', [])
        else:
            jobids = []
            for line in sqs[2:]:
                if line:
                    state = line.split()[1].strip()
                    jobid = line.split()[0].strip()
                    if 'RUNNING' in states and state == 'RUNNING':
                        jobids.append(jobid)
                    elif 'PENDING' in states and state in ('PENDING', 'REQUEUED', 'RESIZING', 'REVOKED', 'SUSPENDED', 'PENDING+', 'REQUEUED+', 'RESIZING+', 'REVOKED+', 'SUSPENDED+'): # https://slurm.schedmd.com/sacct.html#SECTION_JOB-STATE-CODES
                        jobids.append(jobid)
            self._jobids = jobids = [jobid[0] for jobid in self.processes if jobid[0] in jobids]
        if return_nworkers:
            return [(jobid, workers) for jobid, nodes, workers in self.processes if jobid in jobids]
        return jobids

    def nworkers(self, of='workers', state=('PENDING', 'RUNNING')):
        """Number of (pending or running) workers."""
        jobids = self.jobids(state=state)
        if of == 'workers':
            return sum(workers * (jobid in jobids) for jobid, nodes, workers in self.processes)
        return sum(nodes * (jobid in jobids) for jobid, nodes, workers in self.processes)

    def nodes(self, workers=1):
        """
        Number of nodes required for input number of workers,
        computed as the nearest greater integer of :attr:`nodes_per_worker` and ``workers``.
        """
        import math
        return math.ceil(self.nodes_per_worker * workers)

    def cost(self, workers=1):
        """Cost required for input number of workers."""
        return self.nodes(workers=workers)

    @property
    def timeout(self):
        """Job times out after this number of seconds."""
        days_hours_minutes_seconds = decode_slurm_time(self.time)
        factors = [24. * 3600., 3600., 60., 1.]
        return sum(tmp * factor for tmp, factor in zip(days_hours_minutes_seconds, factors)) + 30.  # 30 seconds of margin


class NERSCProvider(SlurmProvider):
    """
    Slurm provider on NERSC: same as :class:`SlurmProvider`,
    but taking into account NERSC specificities:

    - maximum number of MPI processes on a node of 128 (which sets a minimum value on :attr:`nodes_per_worker` given :attr:`mpiprocs_per_worker`)
    - cost increases linearly after :attr:`threshold_nodes` are allocated.

    Parameters
    ----------
    threshold_nodes : int, default=10
        Below this number of allocated nodes, cost is constant.
        Beyond this number of allocated nodes, cost increases linearly.
    """
    name = 'nersc'
    _defaults = {**SlurmProvider._defaults, 'account': 'desi', 'constraint': 'cpu', 'threshold_nodes': 1}  # threshold_nodes = 1: favors 1-node jobs

    def __init__(self, *args, **kwargs):
        super(NERSCProvider, self).__init__(*args, **kwargs)
        self.max_mpiprocs_per_node = 128
        self.nodes_per_worker = max(self.nodes_per_worker, self.mpiprocs_per_worker * 1. / self.max_mpiprocs_per_node)
        if self.constraint == 'gpu':
            if not self.account.endswith('_g'): self.account += '_g'
            if 'gpus-per-node' not in self.kwargs: self.kwargs['gpus-per-node'] = 4

    def cost(self, workers=1):
        """Cost required for input number of workers."""
        nodes = self.nodes(workers=workers)
        if nodes < self.threshold_nodes:
            return 0
        return nodes - self.threshold_nodes

    def __call__(self, cmd, workers=1):
        """
        Submit input command ``cmd`` on ``workers`` workers.
        In case we stack multiple parallel single-process jobs, we use GNU parallel.
        Indeed, in case of bash_app, this avoids spawning a subprocess from an MPI application, which is undefined behavior.
        """
        if self.nodes_per_worker <= 0.:
            raise ValueError('Cannot set nodes_per_worker <= 0.')
        nodes = self.nodes(workers=workers)
        if int(self.nodes_per_worker) != self.nodes_per_worker:  # stack jobs
            if self.mpiprocs_per_worker == 1:
                cmd = ' ; '.join(['module load parallel', 'seq {:d} | parallel -n0 {}'.format(workers, cmd)])
            else:
                cmd = self.mpiexec.format(nodes=nodes, mpiprocs=self.mpiprocs_per_worker * workers, cmd=cmd) + ' --mpisplits {:d}'.format(workers)
        else:
            cmd = [self.mpiexec.format(nodes=int(self.nodes_per_worker), mpiprocs=self.mpiprocs_per_worker, cmd=cmd)] * workers
            cmd = ' & '.join(cmd)
            if workers: cmd += ' & wait'
        cmd = self.environ.to_script(sep=' ; ') + ' ; ' + cmd
        kwargs = []
        for name, value in self.kwargs.items(): kwargs += ['--{}'.format(name), str(value)]
        # -- parsable to get jobid (optionally, cluster name)
        # -- wrap to pass the job
        cmd = ['sbatch', '--output', str(self.output), '--error', str(self.error), '--account', str(self.account), '--constraint', str(self.constraint), '--qos', str(self.qos), '--time', str(self.time), '--nodes', str(nodes), '--signal', str(self.signal), '--parsable'] + kwargs + ['--wrap', cmd]
        proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.processes.append((proc.stdout.split(',')[0].strip(), nodes, workers))  # jobid, workers
