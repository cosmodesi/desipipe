import os
import re
import io
import sys
import contextlib
import time
import random
import uuid
import signal
import pickle
import subprocess
import traceback
import logging
import argparse
import hashlib
import inspect
import glob
import textwrap
import copyreg
import shutil

import sqlite3

from . import utils
from .utils import BaseClass
from .config import Config
from .environment import get_environ, change_environ
from .scheduler import get_scheduler
from .provider import get_provider


task_states = ["WAITING",       # Waiting for requirements (other tasks) to finish
               "PENDING",       # Eligible to be selected and run
               "RUNNING",       # Running right now
               "SUCCEEDED",     # Finished with errno = 0
               "FAILED",        # Finished with errno != 0
               "KILLED",        # Finished with SIGTERM (eg Slurm job timeout)
               "UNKNOWN"]       # Something went wrong and we lost track


TaskState = type("TaskState", (), {**dict(zip(task_states, task_states)), "ALL": task_states})


var_types = ["POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY", "VAR_POSITIONAL", "VAR_KEYWORD"]


VarType = type("VarType", (), {**dict(zip(var_types, var_types)), "ALL": var_types})


class SerializationError(Exception): pass


class DeserializationError(Exception): pass


def unique_id(uid):
    """Return ~ unique ID from input object."""
    if not isinstance(uid, bytes):
        uid = pickle.dumps(uid)
    hex = hashlib.md5(uid).hexdigest()
    return str(uuid.UUID(hex=hex))


def reduce_app(self):
    """Special reduce method for :class:`BaseApp`, dropping :attr:`task_manager`."""
    state = self.__getstate__()
    state["task_manager"] = None
    return (self.__class__.__new__, (self.__class__,), state)


def serialize_function(func, remove_decorator=False):
    if not inspect.isfunction(func):
        raise SerializationError("input object is not a function")
    name = func.__name__
    if name.startswith("<") and name.endswith(">"):
        raise SerializationError("input object has no valuable name, e.g. may be a lambda expression?")
    code = getattr(func, "__desipipecode__", None)
    if code is None:
        try:
            code = inspect.getsource(func)
        except Exception as exc:
            raise SerializationError("cannot find source code for input object") from exc
    code = textwrap.dedent(code).split("\n")
    if remove_decorator:
        if code[0].startswith("@"):
            code = code[1:]
    code = "\n".join(code)
    if not code.startswith("def "):
        raise SerializationError("input object code does not start with def: {}".format(code))
    _, code = code.split(":", maxsplit=1)
    sig = inspect.signature(func)
    parameters, vartypes, dlocals = [], {}, {}
    for param in sig.parameters.values():
        vartypes[param.name] = {param.POSITIONAL_OR_KEYWORD: VarType.POSITIONAL_OR_KEYWORD,
                                param.KEYWORD_ONLY: VarType.KEYWORD_ONLY,
                                param.VAR_POSITIONAL: VarType.VAR_POSITIONAL,
                                param.VAR_KEYWORD: VarType.VAR_KEYWORD}[param.kind]
        default = param.default
        if default is not inspect._empty:
            try:
                param = param.replace(default="#{}#".format(param.name))
                dlocals[param.name] = default
            except ValueError:
                pass
        parameters.append(param)
    sig = sig.replace(parameters=parameters)
    sig = str(sig)
    for param in dlocals:
        sig = sig.replace('"#{}#"'.format(param), param)
    code = "def {}{}:{}".format(name, sig, code)
    return name, code, vartypes, dlocals


def deserialize_function(name, code, dlocals):
    scope = {}
    exec(code, dict(dlocals), scope)  # exec fills dlocals, so let"s make a copy
    scope[name].__desipipecode__ = code
    return scope[name]


class TaskPickler(pickle.Pickler):

    """Special pickler for tasks, handling :class:`BaseApp` and :class:`Future` instances."""

    def __init__(self, *args, reduce_app=reduce_app, **kwargs):
        """Initialize pickler and add the special reduce method for :class:`BaseApp` to the :attr:`dispatch_table`."""
        super(TaskPickler, self).__init__(*args, **kwargs)
        self.dispatch_table = copyreg.dispatch_table.copy()
        if reduce_app:
            for cls in BaseApp.__subclasses__():
                self.dispatch_table[cls] = reduce_app

    def persistent_id(self, obj):
        # Instead of pickling obj as a regular class instance, we emit a persistent ID.
        if isinstance(obj, Future):
            # Here, our persistent ID is simply a tuple, containing a tag and a
            # key, which refers to a specific record in the database.
            ids = getattr(self, "future_ids", [])
            ids.append(obj.id)
            setattr(self, "future_ids", ids)
            return ("Future", obj.id)
        try:
            name, code, vartypes, dlocals = serialize_function(obj)
        except SerializationError:
            pass
        else:
            return ("Function", (name, code, dlocals))
        # If obj does not have a persistent ID, return None. This means obj
        # needs to be pickled as usual.
        return None

    @classmethod
    def dumps(cls, obj, *args, **kwargs):
        """
        Dump input ``obj`` to string.
        args and kwargs are passed to :meth:`__init__`.
        """
        f = io.BytesIO()
        cls(f, *args, **kwargs).dump(obj)
        return f.getvalue()


class TaskUnpickler(pickle.Unpickler):
    """
    Unpickler that corresponds to :class:`TaskPickler`,
    replacing :class:`Future` instances by the actual result of the computation.
    """
    def __init__(self, file, queue=None):
        super().__init__(file)
        self.queue = queue

    def persistent_load(self, pid):
        # This method is invoked whenever a persistent ID is encountered.
        # Here, pid is the tuple returned by TaskUnpickler.
        tag, tid = pid
        if tag == "Future":
            # Fetch the referenced record from the database and return it.
            return self.queue[tid].result
        elif tag == "Function":
            return deserialize_function(*tid)
        else:
            # Always raises an error if you cannot return the correct object.
            # Otherwise, the unpickler will think None is the object referenced
            # by the persistent ID.
            raise pickle.UnpicklingError("unsupported persistent object")

    @classmethod
    def loads(cls, s, *args, **kwargs):
        """
        Load input string ``s``.
        args and kwargs are passed to :meth:`__init__`.
        """
        f = io.BytesIO(s)
        return cls(f, *args, **kwargs).load()


class TaskManagerPickler(pickle.Pickler):

    """Special pickler for tasks, handling :class:`BaseApp` and :class:`Future` instances."""

    def persistent_id(self, obj):
        # Instead of pickling obj as a regular class instance, we emit a persistent ID.
        if isinstance(obj, TaskManager):
            # Here, our persistent ID is simply a tuple, containing a tag and a
            # key, which refers to a specific record in the database.
            state = obj.__getstate__()
            state["queue"] = None
            return ("TaskManager", state)

        # If obj does not have a persistent ID, return None. This means obj
        # needs to be pickled as usual.
        return None

    @classmethod
    def dumps(cls, obj, *args, **kwargs):
        """
        Dump input ``obj`` to string.
        args and kwargs are passed to :meth:`__init__`.
        """
        f = io.BytesIO()
        cls(f, *args, **kwargs).dump(obj)
        return f.getvalue()


class TaskManagerUnpickler(pickle.Unpickler):
    """
    Unpickler that corresponds to :class:`TaskPickler`,
    replacing :class:`Future` instances by the actual result of the computation.
    """
    def __init__(self, file, queue=None):
        super().__init__(file)
        self.queue = queue

    def persistent_load(self, pid):
        # This method is invoked whenever a persistent ID is encountered.
        # Here, pid is the tuple returned by TaskUnpickler.
        tag, state = pid
        if tag == "TaskManager":
            # Fetch the referenced record from the database and return it.
            state["queue"] = self.queue
            toret = TaskManager.__new__(TaskManager)
            toret.__dict__ = state
            return toret
        else:
            # Always raises an error if you cannot return the correct object.
            # Otherwise, the unpickler will think None is the object referenced
            # by the persistent ID.
            raise pickle.UnpicklingError("unsupported persistent object")

    @classmethod
    def loads(cls, s, *args, **kwargs):
        """
        Load input string ``s``.
        args and kwargs are passed to :meth:`__init__`.
        """
        f = io.BytesIO(s)
        return cls(f, *args, **kwargs).load()


class Task(BaseClass):
    """
    Class representing a task, i.e. a application (:attr:`app`) and the arguments to call it with (:attr:`args` and :attr:`kwargs`).

    Attributes
    ----------
    id : str
        Task unique identifier.
        It is built from the pickle representation of (:attr:`app`, :attr:`args`, :attr:`kwargs`),
        such that different tasks have different identifiers (with extremely high probability).

    app : BaseApp
        Application.

    kwargs : dict
        Dictionary of arguments to be passed to :meth:`BaseApp.run`.

    require_ids : list
        List of task IDs that are required for this task to be run,
        infered from :class:`Future` instances passed to :attr:`args` and :attr:`kwargs`.

    state : str
        Task state, one of :attr:`TaskState.ALL`, i.e. ("WAITING", "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "KILLED", "UNKNOWN").

    jobid : str
        Job identifier (not used for now).

    errno : int
        0 if no error, else error number (defaults to 42).

    err : str
        Error message; empty if no error.

    out : str
        Standard output message.

    versions : dict
        Module versions.

    result : object
        Value returned by :class:`BaseApp.func`.

    t0 : float
        Start time of :class:`BaseApp.run`.

    dtime : float
        Running time of :class:`BaseApp.run`.
    """
    _attrs = ["id", "app", "index", "kwargs", "require_ids", "state", "jobid", "errno", "err", "out", "versions", "result", "t0", "dtime"]

    def __init__(self, app, kwargs=None, state=None):
        """
        Initialize :class:`Task`.

        Parameters
        ----------
        app : BaseApp
            Application.

        kwargs : dict, default=None
            Dictionary of arguments to be passed to :meth:`BaseApp.run`.

        state : str, default=None
            Task state. Defaults to "WAITING" if this task requires others to be run,
            else to "PENDING".
        """
        self.result = self.dtime = None
        self.t0 = -1.
        self.update(app=app, kwargs=kwargs, state=state, jobid="", errno=None, err="", out="")

    def update(self, **kwargs):
        """Update task with input attributes."""
        require_id = False
        if "app" in kwargs:
            self.app = kwargs.pop("app")
            self.index = self.app.index
            require_id = True
        if "kwargs" in kwargs:
            self.kwargs = dict(kwargs.pop("kwargs") or {})
            require_id = True

        if not hasattr(self, "require_ids") or require_id:
            try:
                f = io.BytesIO()
                pickler = TaskPickler(f)
                pickler.dump((self.app.name, self.app.code, self.kwargs))
                uid = f.getvalue()
                self.require_ids = list(getattr(pickler, "future_ids", []))
            except (AttributeError, pickle.PicklingError) as exc:
                raise SerializationError("Make sure the task function, args and kwargs are picklable") from exc

        if "state" in kwargs:
            self.state = kwargs.pop("state")
            if self.state is None:
                if self.require_ids:
                    self.state = TaskState.WAITING
                else:
                    self.state = TaskState.PENDING
        if require_id:
            self.id = unique_id(uid)  # unique ID, tied to the given app, args and kwargs
        for name in ["jobid", "errno", "err", "out", "result", "t0", "dtime"]:
            if name in kwargs:
                setattr(self, name, kwargs.pop(name))
        if kwargs:
            raise ValueError("Unrecognized arguments {}".format(kwargs))

    def clone(self, *args, **kwargs):
        """Return an updated copy."""
        new = self.copy()
        new.update(*args, **kwargs)
        return new

    def run(self, **kwargs):
        """
        Run task:

        - call :class:`BaseApp.run`, saving main script where the task is defined and package versions in a folder ".desipipe" located in the directory where files are saved (if any).
        - set :attr:`errno`, :attr:`result`, :attr:`err`, :attr:`out`, :attr:`versions` and :attr:`dtime`.
        - set :attr:`state`: "KILLED" if termination signal, "FAILED" if :class:`BaseApp.run` raised an exception, else "SUCCEEDED".

        """
        from .file_manager import BaseFile
        t0 = time.time()

        def save_attrs(file, base_dir):
            dirname = os.path.join(base_dir, ".desipipe")
            utils.mkdir(dirname)
            script_fn = os.path.join(dirname, "{}.py".format(self.app.name))
            if "code" in self.app.save_attrs:
                input_fn = getattr(self.app, "filename", None)
                if input_fn is not None and os.path.isfile(input_fn):
                    shutil.copyfile(input_fn, script_fn)
                else:
                    with open(script_fn, "w") as file:
                        file.write(self.app.code)
            if "versions" in self.app.save_attrs:
                versions_fn = os.path.join(dirname, "{}.versions".format(self.app.name))
                with open(versions_fn, "w") as file:
                    for name, version in self.app.versions().items():
                        file.write("{}={}\n".format(name, version))
            if "cwd" in self.app.save_attrs:
                shutil.copytree(self.app.dirname, dirname, dirs_exist_ok=True)
            return self.app.save_dir  # destination

        self.errno = 42
        try:
            # make copy of kwargs to avoid in-place modification and potientially pickling error
            self_kwargs = TaskUnpickler.loads(TaskPickler.dumps(self.kwargs))
        except Exception as exc:
            self.errno = getattr(exc, "errno", None) or self.errno
            self.err = traceback.format_exc()
            return
        BaseFile.save_attrs = save_attrs  # save main script and versions whenever a file is written to disk
        if hasattr(self, "callback"):
            self.app.callback = self.callback
        self.errno, self.result, self.err, self.out, self.versions = self.app.run(**{**self_kwargs, **kwargs})
        try:
            del self.app.callback
        except AttributeError:  # may have been deleted by callback in work()
            pass
        try:
            TaskPickler.dumps(self.result)  # to test pickling; the rest should be safe (producted by desipipe)
        except Exception as exc:
            self.errno = getattr(exc, "errno", None) or self.errno
            self.err = traceback.format_exc()
            return
        BaseFile.save_attrs = None
        if self.errno:
            if self.errno == signal.SIGTERM:
                self.state = TaskState.KILLED
            else:
                self.state = TaskState.FAILED
        else:
            self.state = TaskState.SUCCEEDED
        self.dtime = time.time() - t0

    def __getstate__(self):
        """Return the task state (called by pickler)."""
        return {name: getattr(self, name) for name in self._attrs if hasattr(self, name)}


class Future(BaseClass):

    """Structure that keeps track of :class:`Task` result (searching in the :attr:`queue`, with given :attr:`tid`)."""

    def __init__(self, queue, tid):
        """
        Initialize :class:`Future`.

        Parameters
        ----------
        queue : Queue
            Queue where the associated :class:`Task` is saved.

        tid : str
            :attr:`Task.id`.
        """
        self.queue = queue
        self.id = str(tid)


def _make_getter(name):

    def getter(self, timeout=1e4, timestep=1.):
        """
        Return task {0}.

        Parameters
        ----------
        timeout : float, default=1e4
            Return ``None`` after this delay (in seconds) if no success.

        timestep : float, default=1.
            Period (in seconds) at which the queue is queried.

        Returns
        -------
        {0} : object
            Value returned by :class:`BaseApp.func`.
        """.format(name)
        t0 = time.time()
        try:
            return getattr(self, "_" + name)
        except AttributeError:
            while True:
                if (time.time() - t0) < timeout:
                    if self.queue.tasks(tid=self.id, property="state") not in (TaskState.WAITING, TaskState.PENDING, TaskState.RUNNING, TaskState.UNKNOWN):
                        # print(self.queue.tasks(self.id)[0].err)
                        tmp = getattr(self.queue.tasks(tid=self.id), name)
                        setattr(self, "_" + name, tmp)
                        return tmp
                    time.sleep(timestep * random.uniform(0.8, 1.2))
                else:
                    self.log_error("time out while getting {}".format(name))
                    return None

    return getter


for name in ["result", "err", "out"]:
    setattr(Future, name, _make_getter(name))


queue_states = ["ACTIVE", "PAUSED"]


QueueState = type("QueueState", (), {**dict(zip(queue_states, queue_states)), "ALL": queue_states})


def _make_list(obj, tp=str):
    if obj is None:
        return [], False
    one = False
    if not isinstance(obj, (tuple, list)):
        one = True
        obj = [obj]
    return tuple(tp(oo) for oo in obj), one


def _to_str(li):
    return "({})".format(", ".join(["'{}'".format(obj) for obj in li]))


def get_mpicomm():
    mpicomm = None
    try:
        from mpi4py import MPI
    except ImportError:
        pass
    else:
        mpicomm = MPI.COMM_WORLD
    return mpicomm


class Queue(BaseClass):

    """Queue keeping track of all tasks that have been run and to be run, with sqlite backend."""

    def __init__(self, name, base_dir=None, create=None):
        """
        Initialize queue.

        Parameters
        ----------
        name : str
            Name of queue; can contain alphanumeric characters, underscores and hyphens.
            "this/queue" saves the queue as ``base_dir/this/queue.sqlite``.
            "/this/queue" saves the queue as "/this/queue.sqlite" (starting from root).

        base_dir : str, default=None
            Base directory where to save queue.
            If ``None``, defaults to :attr:`Config.queue_dir`.

        create : bool, default=None
            If ``True``, create a new queue; a :class:`ValueError` is raised if the queue already exists.
            If ``False``, do not create the queue; a class:`ValueError` is raised if no queue exists.
            If ``None`` (default), create the queue if does not exist.
        """
        if isinstance(name, self.__class__):
            self.__dict__.update(name.__dict__)
            return

        if base_dir is None:
            base_dir = Config().queue_dir

        if re.match("^[a-zA-Z0-9_/.-]+$", name) is None:
            raise ValueError("Input queue name {} must be alphanumeric plus underscores and hyphens".format(name))

        if not name.endswith(".sqlite"):
            name += ".sqlite"
        self.filename = os.path.abspath(os.path.join(base_dir, name))
        self.dirname = os.path.dirname(self.filename)

        mpicomm = get_mpicomm()
        if mpicomm is None or mpicomm.rank == 0:
            # Check if it already exists and/or if we are supposed to create it
            exists = os.path.exists(self.filename)

            if create is None:
                create = not exists
            elif create and exists:
                raise ValueError("Queue {} already exists".format(name))
            elif (not create) and (not exists):
                raise ValueError("Queue {} does not exist".format(name))

            # Create directory with rwx for user but no one else
            if create:
                self.log_info("Creating queue {}".format(self.filename))
                utils.mkdir(self.dirname, mode=0o700)
                self.db = sqlite3.Connection(self.filename)

                # Give rw access to user but no one else
                os.chmod(self.filename, 0o600)

                # Create tables
                script = """
                CREATE TABLE tasks (
                    tid      TEXT PRIMARY KEY,
                    task     TEXT,
                    state    TEXT,
                    mid      TEXT,  -- task manager id
                    jobid    TEXT,
                    t0       REAL
                );
                -- Dependencies table.  Multiple entries for multiple deps.
                CREATE TABLE requires (
                    tid     TEXT,      -- task.id foreign key
                    require TEXT,      -- task.id that it depends upon
                -- Add foreign key constraints
                    FOREIGN KEY(tid) REFERENCES tasks(tid),
                    FOREIGN KEY(require) REFERENCES tasks(tid)
                );
                -- Task manager table
                CREATE TABLE managers (
                    mid     TEXT PRIMARY KEY, -- task manager id foreign key
                    manager TEXT,             -- task manager
                -- Add foreign key constraints
                    FOREIGN KEY(mid) REFERENCES tasks(mid)
                );
                -- Process table
                CREATE TABLE processes (
                    pid      TEXT,             -- process id
                    provider TEXT,             -- provider
                    PRIMARY KEY (pid, provider)
                );
                -- Metadata about this queue, e.g. active/paused
                CREATE TABLE metadata (
                    key   TEXT,
                    value TEXT
                );
                """
                self.db.executescript(script)
                # Initial queue state is active
                self.db.execute("INSERT INTO metadata VALUES (?, ?)", ("state", QueueState.ACTIVE))
                self.db.commit()
                self.db.close()
        if mpicomm is not None:
            mpicomm.barrier()

        self.log_debug("Connection to queue {}".format(self.filename))
        self.timeout_query = 120.
        self.timestep_query = 2.
        self.timeout_lock = 120.
        self.timestep_lock = 4.
        self.db = sqlite3.Connection(self.filename, timeout=self.timeout_query)

    def _query(self, query, timeout=None, timestep=None, many=False):
        """
        Perform a database query, retrying if needed.

        Parameters
        ----------
        query : str, list
            Query as a string, or a tuple or list (query, arguments),
            where arguments is a tuple, or list of tuples if ``many`` is ``True``.

        timeout : float, default=120
            After this delay (in seconds) without success, raise sqlite3.OperationalError.

        timestep : float, default=2
            Period (in seconds) at which the queue is queried.

        many : bool, default=False
            If ``True``, call the same query for the list of arguments (tuple).
            (in practice, call ``db.executemany(query, args)`` instead of ``db.execute(query)``).

        Returns
        -------
        result : str
            Result of query.
        """
        if timeout is None: timeout = self.timeout_query
        if timestep is None: timestep = self.timestep_query
        t0, ntries = time.time(), 1
        if isinstance(query, str):
            query = (query,)
        while True:
            try:
                if ntries > 1:
                    self.log_debug("Retrying: '{}'".format(" ".join(query)))

                if many:
                    result = self.db.executemany(*query)
                else:
                    result = self.db.execute(*query)

                if ntries > 1:
                    self.log_debug("Succeeded after {} tries".format(ntries))

                return result

            except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                # A few known errors that can occur when multiple clients
                # are hammering on the database. For these cases, wait
                # and try again a few times before giving up.
                known_excs = ["database is locked", "database disk image is malformed"]  # on NFS
                if getattr(exc, "message", "").lower() in known_excs:
                    if (time.time() - t0) < timeout:
                        self.db.close()
                        time.sleep(timestep * random.uniform(0.8, 1.2))
                        self.db = sqlite3.Connection(self.filename)
                        ntries += 1
                    else:
                        self.log_error("tried {} times and still getting errors".format(ntries))
                        raise exc
                else:
                    raise exc

    def _add_requires(self, tid, requires):
        """
        Add requirements for a task.

        Parameters
        ----------
        tid : str
            Task ID.

        requires : list
            List of IDs upon which this task depends.
        """
        query = "INSERT OR REPLACE INTO requires (tid, require) VALUES (?, ?)"
        if isinstance(requires, str):
            self._query([query, (tid, requires)])
        else:
            args = [(tid, x) for x in requires]
            self._query([query, args], many=True)
        self.db.commit()

    def _add_manager(self, manager):
        # """Add input :class:`TaskManager` to the data base (or replace if already there, as specified by :attr:`TaskManager.id`)."""
        query = "INSERT OR REPLACE INTO managers (mid, manager) VALUES (?, ?)"
        self._query([query, (manager.id, TaskManagerPickler.dumps(manager))])
        self.db.commit()

    def add_process(self, pid, provider):
        # """Add input process id to the data base (or replace if already there)."""
        query = "INSERT OR REPLACE INTO processes (pid, provider) VALUES (?, ?)"
        pid = str(pid)
        provider = str(getattr(provider, "name", provider))
        self._get_lock()
        self._query([query, (pid, provider)])
        self.db.commit()
        self._release_lock()

    def add(self, tasks, replace=False, lock=True):
        """
        Add input task(s) to the queue.

        Parameters
        ----------
        tasks : Task, list
            Task or list of tasks.

        replace : bool, default=False
            If ``True``, replace task(s) (as identified by their IDs) with the input ones.
            If ``False``, an error is raised if input tasks (as identified by their IDs) are already in the queue.
            If ``None``, do not add task if already in queue, but update task manager if task state is "PENDING" or "WAITING".

        lock : bool, default=True
            Ask for lock.

        Returns
        -------
        futures : list
            List of :class:`Future` corresponding to input tasks.
        """
        isscalar = isinstance(tasks, Task)
        if isscalar:
            tasks = [tasks]
        ids, requires, managers, states, tasks_serialized, jobids, t0s, futures = [], [], [], [], [], [], [], []
        if lock: self._get_lock()
        for task in tasks:
            futures.append(Future(queue=self, tid=task.id))
            manager = task.app.task_manager
            if replace is None:
                row = self._query(["SELECT state, mid FROM tasks WHERE tid=?", (task.id,)]).fetchone()
                if row:
                    state, mid = row
                    if state in (TaskState.PENDING, TaskState.WAITING) and mid != manager.id:
                        self._query(["REPLACE INTO tasks (tid, mid) VALUES (?, ?)", (task.id, mid)])
                        managers.append(manager)
                    continue
            ids.append(task.id)
            requires.append(task.require_ids)
            managers.append(manager)
            states.append(task.state)
            tasks_serialized.append(TaskPickler.dumps(task))
            jobids.append(task.jobid)
            t0s.append(task.t0)
        query = "INSERT"
        if replace: query = "REPLACE"
        query += " INTO tasks (tid, task, state, mid, jobid, t0) VALUES (?,?,?,?,?,?)"
        self._query([query, zip(ids, tasks_serialized, states, [tm.id for tm in managers], jobids, t0s)], many=True)
        for tid, requires in zip(ids, requires):
            self._add_requires(tid, requires)
        for manager in managers:
            self._add_manager(manager)
        self.db.commit()
        for tid, state in zip(ids, states):
            if state == TaskState.WAITING:
                self._update_waiting_task_state(tid=tid)
            elif state in (TaskState.SUCCEEDED, TaskState.FAILED):
                self._update_waiting_tasks(tid)
        if isscalar:
            return futures[0]
        return futures

    def _get_lock(self, timeout=None, timestep=None):
        if timeout is None: timeout = self.timeout_lock
        if timestep is None: timestep = self.timestep_lock
        # """Get lock on the data base."""
        t0 = time.time()
        while True:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                return True
            except sqlite3.OperationalError as exc:
                if (time.time() - t0) > timeout:
                    self.log_error("unable to get database lock")
                    return False
                time.sleep(timestep * random.uniform(0.8, 1.2))

    def _release_lock(self):
        # """Release lock on the data base."""
        self.db.commit()

    @property
    def state(self):
        """Get queue state ("ACTIVE" or "PAUSED")."""
        return self._query("SELECT value FROM metadata WHERE key='state'").fetchone()[0]

    @state.setter
    def state(self, state):
        """Set queue state ("ACTIVE" or "PAUSED")."""
        if state not in (QueueState.ACTIVE, QueueState.PAUSED):
            raise ValueError("Invalid queue state {}; should be {} or {}".format(state, QueueState.ACTIVE, QueueState.PAUSED))
        self._get_lock()
        self._query(["UPDATE metadata SET value=? where key='state'", (state,)])
        self.db.commit()
        self._release_lock()

    def pause(self):
        """Pause queue, i.e. set state to "PAUSED"."""
        self.state = QueueState.PAUSED

    @property
    def paused(self):
        """Is queue paused?"""
        return self.state == QueueState.PAUSED

    def resume(self):
        """Resume queue, i.e. set state to "ACTIVE"."""
        self.state = QueueState.ACTIVE

    def set_task_state(self, tid, state, jobid=None, t0=None):
        """Set the state of task with input ID ``tid`` to ``state``."""
        try:
            self._get_lock()
            query = "UPDATE tasks SET state=? WHERE tid=?"
            self._query([query, (state, tid)])
            if jobid is not None:
                query = "UPDATE tasks SET jobid=? WHERE tid=?"
                self._query([query, (jobid, tid)])
            if t0 is not None:
                query = "UPDATE tasks SET t0=? WHERE tid=?"
                self._query([query, (float(t0), tid)])
            self.db.commit()

            if state in (TaskState.SUCCEEDED, TaskState.FAILED):
                self._update_waiting_tasks(tid)

        finally:
            self._release_lock()

    def _update_waiting_task_state(self, tid, force=False):
        """
        Check if all requirements of task of ID ``tid`` have finished running.
        If so, set it into the task to "PENDING" state.

        If ``force``, do the check no matter what. Otherwise, only proceed
        with check if the task is still in the "WAITING" state.
        """
        if not self._get_lock():
            self.log_error("unable to get db lock; not updating waiting task state")
            return

        # Ensure that it is still waiting
        # (another process could have moved it into pending)
        if not force:
            q = "SELECT state FROM tasks WHERE tasks.tid=?"
            row = self.db.execute(q, (tid,)).fetchone()
            if row is None:
                self._release_lock()
                raise ValueError("Task ID {} not found".format(tid))
            if row[0] != TaskState.WAITING:
                self._release_lock()
                return

        query = "SELECT COUNT(d.require) FROM requires d JOIN tasks t ON d.require = t.tid WHERE d.tid=? AND t.state IN (?, ?, ?, ?, ?, ?)"
        row = self._query([query, (tid, TaskState.WAITING, TaskState.PENDING, TaskState.RUNNING, TaskState.FAILED, TaskState.KILLED, TaskState.UNKNOWN)]).fetchone()
        self._release_lock()
        if row is None:
            return
        if row[0] == 0:
            self.set_task_state(tid, TaskState.PENDING)
        elif force:
            self.set_task_state(tid, TaskState.WAITING)

    def _update_waiting_tasks(self, tid):
        """Identify tasks that are waiting for task of ID ``tid``, and call :meth:`_update_waiting_task_state` on them."""
        query = "SELECT t.tid FROM tasks t JOIN requires d ON d.tid=t.tid WHERE d.require=? AND t.state=?"
        waiting_tasks = self._query([query, (tid, TaskState.WAITING)]).fetchall()
        for tid in waiting_tasks:
            self._update_waiting_task_state(tid[0])

    def delete(self, kill=True):
        """Delete data base :attr:`db` from both this instance and the disk (and delete associated jobs)."""
        if kill:
            globals()["kill"](queue=self)
        if hasattr(self, "db"):
            self.db.close()
            del self.db
        mpicomm = get_mpicomm()
        if mpicomm is None or mpicomm.rank == 0:
            try:
                os.remove(self.filename)
            except OSError:
                pass
        if mpicomm is not None:
            mpicomm.barrier()

    def clear(self, kill=True):
        """Clear queue: delete and recreate."""
        self.delete(kill=kill)
        self.__init__(self.filename)

    def tasks(self, tid=None, state=None, mid=None, jobid=None, name=None, index=None, one=None, property=None):
        """
        List tasks in queue.

        Parameters
        ----------
        tid : str, list, default=None
            If not ``None``, select task with given ID.

        state : str, list, default=None
            If not ``None``, select tasks with given state.

        mid : str, list, default=None
            If not ``None``, select tasks with given task manager ID.

        jobid : str, list, default=None
            If not ``None``, select tasks with given job ID.

        name : str, list, default=None
            If not ``None``, select tasks with input application name.

        index : int, list, default=None
            If not ``None``, select tasks with input index.

        one : bool, default=None
            If ``True``, return only one task.
            If ``False``, return list.
            If ``None``, and ``tid`` is not ``None``, return a single task; else a list of tasks.

        property : str, list, default=None
            If not ``None``, instead of returning task(s), return this property
            (one of "tid", "state", "mid", "jobid", "t0", "task_manager").

        Returns
        -------
        tasks : Task, list
            Task or property or property or list of such objects.
        """
        tid = _make_list(tid, tp=str)[0]
        state = _make_list(state, tp=str)[0]
        mid = _make_list(mid, tp=str)[0]
        jobid = _make_list(jobid, tp=str)[0]
        property, one_property = _make_list(property, tp=str)

        select = []
        if tid:
            select.append("tid IN {}".format(_to_str(tid)))
            if one is None: one = True
        if state:
            select.append("state IN {}".format(_to_str(state)))
        if mid:
            select.append("mid IN {}".format(_to_str(mid)))
        if jobid:
            select.append("jobid IN {}".format(_to_str(jobid)))
        query = "SELECT task, tid, state, mid, jobid, t0 FROM tasks"
        if select: query += " WHERE {}".format(" AND ".join(select))
        tasks = self._query(query)
        if one:
            tasks = tasks.fetchone()
            if tasks is None: return None
            tasks = [tasks]
        else:
            tasks = tasks.fetchall()
            if tasks is None: return []
        name, one_name = _make_list(name, tp=str)
        index, one_index = _make_list(index, tp=int)
        if one is None and (one_name and one_index):
            one = True
        toret = []
        for task in tasks:
            (ptask, tid, state, mid, jobid, t0), task = task, None
            if name or index or not property or any(prop in ["index", "name"] for prop in property):
                task = TaskUnpickler.loads(ptask, queue=self)
                if task.id != tid:
                    raise RuntimeError("something bad happened, unpickled task id is not the same as the one it was registered with")
                if name and task.app.name not in name:
                    continue
                if index and task.index not in index:
                    continue
            if not property or "task_manager" in property:
                task_manager = self.managers(mid=mid)
            props = []
            for prop in property:
                if prop == "tid":
                    props.append(tid)
                    continue
                if prop == "state":
                    props.append(state)
                    continue
                if prop == "mid":
                    props.append(mid)
                    continue
                if prop == "jobid":
                    props.append(jobid)
                    continue
                if prop == "t0":
                    props.append(float(t0))
                    continue
                if prop == "index":
                    props.append(task.index)
                    continue
                if prop == "name":
                    props.append(task.app.name)
                    continue
                if prop == "task_manager":
                    props.append(task_manager)
                    continue
                if prop is not None:
                    raise ValueError("unkown property {}".format(prop))
            if property:
                toret.append(props[0] if one_property else tuple(props))
                continue
            task.update(state=state, jobid=jobid, t0=t0)
            task.app.task_manager = task_manager
            toret.append(task)
        if one:
            if len(toret):
                return toret[0]
            return None
        return toret

    def pop(self, state=TaskState.PENDING, **kwargs):
        """
        Retrieve a task to be run (i.e. in "PENDING" state).

        Parameters
        ----------
        state : str, list, default="PENDING"
            Select a task with this state.

        **kwargs : dict
            Optional arguments to select task: tid, mid, name, etc.

        Returns
        -------
        task : Task
        """
        # First make sure we are not paused
        if self.state == QueueState.PAUSED:
            return None

        if self._get_lock():
            task = self.tasks(state=state, one=True, **kwargs)
        else:
            self.log_warning('There may be tasks left in queue but I couldn"t get lock to see')
            return None
        self._release_lock()
        if task is None:
            return None
        jobid = task.app.task_manager.provider.jobid()
        task.update(state=TaskState.RUNNING, jobid=jobid, t0=time.time())
        return task

    def managers(self, mid=None, property=None):
        """
        List managers.

        Parameters
        ----------
        mid : str, default=None
            If not ``None``, return the task manager with given ID.

        property : str, default=None
            If not ``None``, instead of returning task manager(s), return this property
            (one of "mid").

        Returns
        -------
        tm : TaskManager, list
           Task manager or property or list of such objects.
        """
        query = "SELECT mid, manager FROM managers"
        one = mid is not None
        if one:
            query += " WHERE mid='{}'".format(mid)
        managers = self._query(query).fetchall()
        toret = []
        for manager in managers:
            mid, manager = manager
            if property in ("mid", "tid"):
                toret.append(mid)
                continue
            manager = TaskManagerUnpickler.loads(manager, queue=self)
            if manager.id != mid:
                raise RuntimeError("something bad happened, unpickled manager id is not the same as the one it was registered with")
            toret.append(manager)
        if one:
            return toret[0]
        return toret

    def processes(self):
        """List processes that have been launched."""
        query = "SELECT pid, provider FROM processes"
        return self._query(query).fetchall()

    def counts(self, state=None, mid=None):
        """
        Count the number of tasks.

        Parameters
        ----------
        state : str, list, default=None
            If not ``None``, select tasks with given state.

        mid : str, list, default=None
            If not ``None``, select tasks with given task manager ID.

        Returns
        -------
        counts : int
        """
        state = _make_list(state, tp=str)[0]
        mid = _make_list(mid, tp=str)[0]
        select = []
        if state:
            select.append("state IN {}".format(_to_str(state)))
        if mid:
            select.append("mid IN {}".format(_to_str(mid)))
        query = "SELECT count(state) FROM tasks"
        if select: query += " WHERE {}".format(" AND ".join(select))
        return self._query(query).fetchone()[0]

    def summary(self, mid=None, return_type="dict"):
        """
        Return summary description of queue, i.e. number of tasks in all states :attr:`TaskState.ALL`.

        Parameters
        ----------
        mid : str, list, default=None
            If not ``None``, select tasks with given task manager ID.

        return_type : str, default="dict"
            If "dict", return a dictionary mapping task state to the task counts.
            If "str", return a string.

        Returns
        -------
        summary : dict, str
        """
        counts = {state: self.counts(mid=mid, state=state) for state in TaskState.ALL}
        if return_type == "dict":
            return counts
        if return_type == "str":
            toret = ["{:10s}: {}".format(state, count) for state, count in counts.items()]
            return "\n".join(toret)
        raise ValueError("Unknown return_type {}".format(return_type))

    def __repr__(self):
        """String representation of queue: size, state and file name."""
        return "{}(size={}, state={}, filename={})".format(self.__class__.__name__, self.counts(), self.state, self.filename)

    def __str__(self):
        """String representation of queue: size, state and file name, and summary."""
        return self.__repr__() + "\n" + self.summary(return_type="str")

    def __getitem__(self, tid):
        """Return task with input ID ``tid``."""
        toret = self.tasks(tid=tid)
        if toret is None:
            raise KeyError("task {} not found".format(tid))
        return toret

    def __delitem__(self, tid):
        """Delete task with input ID ``tid``."""
        if not self._get_lock():
            self.log_error("unable to get db lock; not deleting task")
            return
        query = "DELETE FROM tasks WHERE tid=?"
        self._query([query, (tid,)])
        self._release_lock()

    def __getstate__(self):
        """Return queue state: just its file name."""
        return {"filename": self.filename}

    def __setstate__(self, state):
        """Set queue state, from the file name."""
        self.__init__(state["filename"], base_dir="", create=False)


class BaseApp(BaseClass):
    """
    Base application class.

    Attributes
    ----------
    func : callable
        Function doing the job.

    name : str
        :attr:`func` name.jobid

    code : str
        :attr:`func` code.

    filename : str
        File path where :attr:`func` is defined (may be ``None``, e.g. in case of notebooks).

    dirname : str
        Current working directory.

    task_manager : TaskManager
        Task manager to which the task has been added.
    """
    def __init__(self, func, task_manager=None, skip=False, name=None, state=None, save_attrs=("code", "versions"), save_dir=None):
        """
        Initialize application, called by :class:`TaskManager` decorators :meth:`TaskManager.bash_app` and :meth:`TaskManager.python_app`.

        Parameters
        ----------
        func : callable
            Function doing the job.

        task_manager : TaskManager, default=None
            Task manager to which the task has been added.
        """
        if isinstance(func, self.__class__):
            self.__dict__.update(func.__dict__)
            return
        self.add = {"skip": False, "name": None, "state": tuple()}
        self.update(func=func, task_manager=task_manager, skip=skip, name=name, state=state, save_attrs=save_attrs, save_dir=save_dir)

    def update(self, **kwargs):
        """Update app with input attributes."""
        if "func" in kwargs:
            self.func = kwargs.pop("func")
            self.name, self.code, self.vartypes, dlocals = serialize_function(self.func, remove_decorator=True)
            self.dlocals = dict.fromkeys(list(dlocals.keys()))
            self.filename = None
            try:
                self.filename = inspect.getfile(self.func)
                if not os.path.isfile(self.filename): self.filename = None
            except:
                pass
            self.dirname = os.getcwd()
            #self.imports = {}
            #for m in re.findall("[\n;\s]*from\s+([^\s.]*)\s+import", self.code) + re.findall("[\n;\s]*import\s+([^\s.]*)\s*", self.code):
            #    self.imports[m.__name__] = getattr(m, "__version__")
            if "task_manager" in kwargs:
                self.task_manager = kwargs.pop("task_manager")
            self.index = -1
        if "skip" in kwargs:
            self.add["skip"] = bool(kwargs.pop("skip"))
        if "name" in kwargs:
            name = kwargs.pop("name")
            if name:
                if not isinstance(name, str):
                    name = self.name
                self.add["name"] = name
                self.add["state"] = tuple(TaskState.ALL)
        if "state" in kwargs:
            state = kwargs.pop("state", None)
            if state is None: state = TaskState.ALL
            self.add["state"] = tuple(_make_list(state)[0])
            if self.add["state"]: self.add.setdefault("name", self.name)
        if "save_attrs" in kwargs:
            self.save_attrs = kwargs.pop("save_attrs")
            if isinstance(self.save_attrs, str):
                self.save_attrs = (self.save_attrs,)
            self.save_attrs = tuple(self.save_attrs)
        if "save_dir" in kwargs:
            self.save_dir = kwargs.pop("save_dir")
            if self.save_dir:
                self.save_dir = str(self.save_dir)
            else:
                self.save_dir = None
        if kwargs:
            raise ValueError("Unrecognized arguments {}".format(kwargs))

    def clone(self, **kwargs):
        """Return an updated copy."""
        new = self.copy()
        new.update(**kwargs)
        return new

    def __call__(self, *args, **kwargs):
        """Call the decorator, i.e. add task to the queue."""
        self.index += 1
        if self.add["skip"]:
            return None
        queue = self.task_manager.queue
        if self.add["name"]:
            tid, state = queue.tasks(name=self.add["name"], index=self.index, property=("tid", "state"))
            if state in self.add["state"]:
                return Future(queue=queue, tid=tid)
        kwargs = inspect.getcallargs(self.func, *args, **kwargs)
        task = Task(self, kwargs)
        return queue.add(task, replace=None)

    def __getstate__(self):
        """Return app state."""
        state = {name: getattr(self, name) for name in ["name", "code", "vartypes", "dlocals", "filename", "dirname", "save_attrs", "save_dir", "task_manager"]}
        return state

    def __setstate__(self, state):
        """Set app state."""
        self.add = {"skip": False, "name": None, "state": tuple()}
        self.__dict__.update(state)
        self.func = deserialize_function(self.name, self.code, self.dlocals)

    def run(self, **kwargs):
        """Run app with input ``kwargs``."""
        raise NotImplementedError

    def _run(self, **kwargs):
        args, kw = [], {}
        for var, vtype in self.vartypes.items():
            if var not in kwargs: continue
            value = kwargs[var]
            if vtype == VarType.POSITIONAL_OR_KEYWORD:
                args.append(value)
            elif vtype == VarType.KEYWORD_ONLY:
                kw[var] = value
            elif vtype == VarType.VAR_POSITIONAL:
                args += list(value)
            elif vtype == VarType.VAR_KEYWORD:
                kw = {**value, **kw}
        return self.func(*args, **kw)

    def versions(self):
        """Return module versions (unknown by default)."""
        return {}


def select_modules(modules):
    """Select "standard" top-level modules: those which do not start with an underscore."""
    return set(name.split(".", maxsplit=1)[0] for name in modules if not name.startswith("_"))


_modules = select_modules(sys.modules)


class MyStream(object):

    def __init__(self, stream, callback=None):
        self._stream = stream
        self._log = io.StringIO()
        self.callback = callback

    def flush(self):
        self._stream.flush()

    def write(self, message, **kwargs):
        if _stream_out_err:
            self._stream.write(message, **kwargs)
        self._log.write(message)
        if self.callback is not None:
            self.callback()

    def result(self, clear=False):
        toret = self._log.getvalue()
        if clear: self.clear()
        return toret

    def clear(self):
        self._log.seek(0)
        self._log.truncate(0)

    def isatty(self):
        return False

    def __del__(self):
        pass
        #self._log.close()

_stream_out_err = True
_sout, _serr = MyStream(sys.stdout), MyStream(sys.stderr)


class PythonApp(BaseApp):
    """
    Python application, e.g.:

    .. code-block:: python

        @tm.python_app
        def test(n):
            print("hello" * n)

    """
    def run(self, **kwargs):
        """Run app with input ``args`` and ``kwargs``."""
        errno, result, err, out, versions = 0, None, "", "", {}
        if self.dirname not in sys.path:
            sys.path.insert(0, self.dirname)

        def callback():
            callback = getattr(self, "callback", None)
            if callback is not None:
                callback(_sout.result(), _serr.result())

        _sout.clear(), _serr.clear()
        _sout.callback = callback
        # We shall use previous streams, as they may have been imported already by e.g. the logger
        with contextlib.redirect_stdout(_sout), contextlib.redirect_stderr(_serr):
            try:
                result = self._run(**kwargs)
            except Exception as exc:
                errno = getattr(exc, "errno", None) or 42
                traceback.print_exc(file=_serr)
                # raise exc
            versions = self.versions()
        err, out = _serr.result(clear=True), _sout.result(clear=True)
        return errno, result, err, out, versions

    def versions(self):
        """Return module versions."""
        versions = {}
        for name in select_modules(sys.modules) - _modules:
            try:
                versions[name] = sys.modules[name].__version__
            except (KeyError, AttributeError):
                pass
        return versions


class BashApp(BaseApp):
    """
    Bash application, e.g.:

    .. code-block:: python

        @tm.bash_app
        def test(n):
            return 'echo "{}"'.format("hello" * n)

    """
    def run(self, **kwargs):
        """Run app with input ``args`` and ``kwargs``."""
        errno, result, out, err = 0, None, "", ""
        cmd = self._run(**kwargs)
        cmd = list(map(str, cmd))
        os.environ["PYTHONUNBUFFERED"] = "1"

        callback = getattr(self, "callback", None)

        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, shell=False)
        out, err = "", ""
        # We could have created a ThreadPoolExecutor(2) to get out and err at the same time
        # but we wouldn"t have been able to write task.out and err in the queue live from a thread
        # Here out will be written first, then err; which is fine in most cases
        for line in proc.stdout:
            if _stream_out_err: print(line[:-1])
            out += line
            if callback is not None:
                callback(out, "")
        for line in proc.stderr:
            if _stream_out_err: print(line[:-1])
            err += line
        while True:
            errno = proc.poll()
            if errno is not None: break
            time.sleep(1.)
        return errno, result, err, out, {}


def decorator(func):
    """
    Decorator to deal with decorator arguments, e.g.:

    .. code-block:: python

        @tm.python_app
        def test(n):
            print("hello" * n)

    and

    .. code-block:: python

        @tm.python_app(skip=False)
        def test(n):
            print("hello" * n)

    are equivalent.
    """
    def wrapper(self, *args, **kwargs):
        if kwargs or not args:
            if args:
                raise ValueError("unexpected args: {}, {}".format(args, kwargs))

            def wrapper(app):
                return func(self, app, **kwargs)

            return wrapper

        if len(args) != 1:
            raise ValueError("unexpected args: {}".format(args))

        return func(self, args[0], **kwargs)

    return wrapper


class TaskManager(BaseClass):
    """
    Task manager, main class to be used in scripts, e.g.:

    .. code-block:: python

        queue = Queue("test", base_dir="_tests")
        tm = TaskManager(queue, environ=Environment(), scheduler=dict(max_workers=10))

        @tm.python_app
        def test(n):
            print("hello" * n)

    Attributes
    ----------
    queue : Queue
        Queue, see :class:`Queue`.

    tid : str
        Task manager unique identifier.
        It is built from the pickle representation of (:attr:`environ`, :attr:`scheduler`, :attr:`provider`),
        such that different task managers have different identifiers (with extremely high probability).

    environ : BaseEnvironment
        Tasks are run within this environment.

    scheduler : BaseScheduler
        Tasks are distributed to workers with this scheduler.

    provider : BaseProvider
        Tasks are executed on the machine with this provider.
    """
    def __init__(self, queue, environ=None, scheduler=None, provider=None):
        """
        Initialize :class:`TaskManager`.

        Parameters
        ----------
        queue : str, Queue
            Queue, see :func:`get_queue`.

        environ : BaseEnvironment, str, dict, default=None
            Tasks are run within this environment. See :func:`get_environ`.

        scheduler : BaseScheduler, str, dict, default=None
            Tasks are distributed to workers with this scheduler. See :func:`get_scheduler`.

        provider : BaseProvider, str, dict, default=None
            Tasks are executed on the machine with this provider. See :func:`get_provider`.
        """
        self.update(queue=queue, environ=environ, scheduler=scheduler, provider=provider)

    def update(self, **kwargs):
        """Update task manager attributes."""
        require_id = False
        for name, func in zip(["queue", "environ", "scheduler", "provider"],
                              [get_queue, get_environ, get_scheduler, get_provider]):
            if name in kwargs:
                setattr(self, name, func(kwargs.pop(name)))
                if name != "queue": require_id = True
        if require_id:
            self.provider.update(environ=self.environ)
            uid = pickle.dumps((self.environ, self.scheduler, self.provider))
            self.id = unique_id(uid)  # unique ID, tied to the given environ, scheduler, provider
        if kwargs:
            raise ValueError("Unrecognized arguments {}".format(kwargs))

    def clone(self, **kwargs):
        """Return an updated copy."""
        new = self.copy()
        new.update(**kwargs)
        return new

    def __getstate__(self):
        return {name: getattr(self, name) for name in ["queue", "environ", "scheduler", "provider", "id"]}

    @decorator
    def python_app(self, func, **kwargs):
        """Decorator for :class:`PythonApp`."""
        return PythonApp(func, task_manager=self, **kwargs)

    @decorator
    def bash_app(self, func, **kwargs):
        """Decorator for :class:`BashApp`."""
        return BashApp(func, task_manager=self, **kwargs)

    def spawn(self, *args, **kwargs):
        """Distribute tasks to workers."""
        self.scheduler.update(provider=self.provider)
        return self.scheduler(*args, **kwargs)


def work(queue, mid=None, tid=None, name=None, provider=None, mode="", mpicomm=None, mpisplits=None, timestep=120.):
    """
    Do the actual work: pop tasks from the input queue, and run them.

    Parameters
    ----------
    queue : Queue
        Queue to pop tasks from.

    mid : str, list, default=None
        If not ``None``, take tasks with this task manager ID.

    tid : str, list, default=None
        If not ``None``, take a task with this task ID.

    name : str, list, default=None
        If not ``None``, take a task with this name.

    provider : str, default=None
        Name of provider, to get process ID. Defaults to the manager"s provider.

    mode : str, default=""
        Processing mode.
        "stop_at_error" to stop as soon as a task is failed.
        "retry_at_timeout" to retry when time out.
        "no_stream" to not stream stderr/stdout during the tasks (helps when many jobs in parallel).
        "no_out" to not stream stderr/stdout and not save stdout.

    timestep : float, default=120
        Time step for streaming output.

    mpicomm : MPI communicator, default=mpi.COMM_WORLD
        The MPI communicator.

    mpisplits : int, default=None
        Split MPI communicator in this number of subcommunicators to run ``mpisplits`` tasks in parallel.
    """
    killed_at_timeout = None

    queue = get_queue(queue, create=False, one=True)
    task = None
    itask = 0

    #mpicomm_all = mpicomm

    try:
        from mpi4py import MPI
    except ImportError:
        MPI = mpicomm = None
    else:
        if mpicomm is None:
            mpicomm = MPI.COMM_WORLD

    if mpisplits is not None:
        if mpicomm is None:
            raise ImportError("mpicomm not defined")
        for isplit in range(mpisplits):
            if (mpicomm.size * isplit // mpisplits) <= mpicomm.rank < (mpicomm.size * (isplit + 1) // mpisplits):
                color = isplit
        mpicomm = mpicomm.Split(color, 0)
        MPI.COMM_WORLD = mpicomm  # a bit hacky

    #ti = time.time()
    def exit_killed(signal_number, stack_frame):
        global killed
        killed = True
        if mpicomm is None or mpicomm.rank == 0:
            if task is not None:
                if signal_number in (signal.SIGINT,):
                    state = TaskState.KILLED
                elif killed_at_timeout is True:
                    state = TaskState.KILLED
                elif killed_at_timeout is False:
                    state = TaskState.PENDING
                elif itask < 1:
                    state = TaskState.KILLED
                else:
                    state = TaskState.PENDING  # automatically propose new task
                queue.set_task_state(task.id, state)
        if signal_number == signal.SIGINT:
            exit()

    #signal.SIGOOM = 253  # slurm out-of-memory
    signal.signal(signal.SIGINT, exit_killed)
    signal.signal(signal.SIGTERM, exit_killed)
    #signal.signal(signal.SIGOOM, exit_killed)

    global t0, killed
    killed = False
    no_out = "no_out" in mode
    no_stream = no_out or ("no_stream" in mode)

    def callback(out, err):
        global t0, killed
        if killed or no_stream: return
        if (mpicomm is None or mpicomm.rank == 0) and time.time() - t0 > timestep:
            if (out, err) != (task.out, task.err):
                task.out, task.err = out, err
                try: queue.add(task, replace=True, lock=False)
                except: pass
            t0 = time.time()

    jobid = None
    if provider is not None:
        provider = get_provider(provider)
        jobid = provider.jobid()
        queue.add_process(jobid, provider=provider)

    global _stream_out_err
    ntasks = {}

    while True:
        stask = task = None
        # print(queue.summary(), queue.counts(state="PENDING"))
        if mpicomm is None or mpicomm.rank == 0:
            task = queue.pop(mid=mid, tid=tid, name=name, jobid=jobid)
            # Not in pop, because we want the task to be set in running state only when exit_killed can access it
            if task is not None:  # can be None if no task to pop
                tm = task.app.task_manager
                _stream_out_err = tm.provider.name != "local"
                if "retry_at_timeout" in mode:
                    killed_at_timeout = False
                else:
                    killed_at_timeout = getattr(tm.provider, "killed_at_timeout", None)
                ntasks[tm.id] = ntasks.get(tm.id, 0) + 1
                stop_after = getattr(tm.provider, "stop_after", None)
                if stop_after is not None and ntasks[tm.id] > stop_after:
                    task = None
                else:
                    try: queue.set_task_state(task.id, TaskState.RUNNING, jobid=task.jobid, t0=task.t0)
                    except: pass
            stask = TaskPickler.dumps(task, reduce_app=None)
        if mpicomm is not None:
            killed_at_timeout, _stream_out_err = mpicomm.bcast((killed_at_timeout, _stream_out_err), root=0)
            stask = mpicomm.bcast(stask, root=0)
        #task_in = TaskUnpickler.loads(stask)
        task = TaskUnpickler.loads(stask)
        if task is None:
            break
        kwargs = {}
        if task.kwargs.get("mpicomm", False) is None:
            kwargs["mpicomm"] = mpicomm
        task.callback = callback
        t0 = time.time() - timestep  # for callback
        task.run(**kwargs)
        if mpicomm is not None: mpicomm.barrier()
        if mpicomm is None or mpicomm.rank == 0:
            delta = time.time() - t0
            if task.err and delta > 0:
                time.sleep(delta * random.uniform(0.1, 1.))  # if the task fails immediately, avoid overloading the queue
            #time.sleep(0.5 * random.uniform(0.8, 1.2))
            if no_out: task.out = ""
            try: queue.add(task, replace=True)
            except: pass
        itask += 1
        if "stop_at_error" in mode and task.state == TaskState.FAILED:
            break


def spawn(queue, timeout=3600 * 24, timestep=3., mode="", max_workers=None, spawn=False):
    """
    Distribute tasks to workers.
    If all queues are paused, the function terminates.

    Parameters
    ----------
    queue : Queue, list
        Queue or list of queues to process.

    timeout : float, default=3600 * 24
        Time out after this delay (in seconds).

    timestep : float, default=3.
        Period (in seconds) at which the queue is queried for new tasks.

    mode : str, default=""
        Processing mode.
        "stop_at_error" to stop as soon as a task is failed.
        "retry_at_timeout" to retry when time out.
        "no_stream" to not stream stderr/stdout during the tasks (helps when many jobs in parallel).
        "no_out" to not stream stderr/stdout and not save stdout.

    spawn : bool, default=False
        If ``True``, spawn a new manager process and exit this one.
    """
    queues = get_queue(queue, create=False, one=False)

    if spawn:
        subprocess.Popen(["desipipe", "spawn", "--queue", " ".join([queue.filename for queue in queues]), "--timeout", str(timeout), "--mode", str(mode)], start_new_session=True, env=os.environ)
        return

    t0 = time.time()
    qmanagers, qadded_processes = [{} for i in range(len(queues))], [set() for i in range(len(queues))]
    stop = False
    nsteps, stop_after_nsteps = 0, 10
    while True:
        if (time.time() - t0) > timeout:
            break
        if all(queue.paused for queue in queues):
            break
        if stop:
            if nsteps > stop_after_nsteps:  # after stop_after_nsteps with stop, stop
                break
        else:
            nsteps = 0
        nsteps += 1
        stop = True
        for (queue, managers, added_processes) in zip(queues, qmanagers, qadded_processes):
            pid = os.getpid()
            #if ("local", pid) not in added_processes:
            #    added_processes.add(("local", pid))
            queue.add_process(pid, provider="local")  # force checking .sqlite file exists
            if queue.paused:
                continue
            if "stop_at_error" in mode and queue.counts(state=TaskState.FAILED):
                continue
            if queue.counts(state=(TaskState.PENDING, TaskState.RUNNING)):
                stop = False
            for manager in queue.managers():
                if manager.id not in managers:
                    if max_workers is not None:
                        manager.scheduler.update(max_workers=max_workers)
                    managers[manager.id] = manager
                manager = managers[manager.id]  # such that manager.provider keeps track of current processes
                for tid, tt0 in queue.tasks(property=("tid", "t0"), mid=manager.id, state=TaskState.RUNNING):
                    if time.time() - tt0 > manager.provider.timeout:
                        queue.set_task_state(tid, TaskState.UNKNOWN)
                ntasks = queue.counts(mid=manager.id, state=TaskState.PENDING)
                # print(ntasks, queue.counts(mid=manager.id, state=TaskState.PENDING), queue.counts(mid=manager.id, state=TaskState.WAITING), stop, flush=True)
                if ntasks:
                    manager.spawn('desipipe work --queue {} --mid {} --mode {}'.format(queue.filename, manager.id, mode), ntasks=ntasks)
                    for jobid in manager.provider.jobids():
                        if jobid is not None and (manager.provider.name, jobid) not in added_processes:  # just to limit queries
                            added_processes.add((manager.provider.name, jobid))
                            queue.add_process(jobid, provider=manager.provider)
        time.sleep(timestep * random.uniform(0.8, 1.2))


def kill(queue=None, provider=None, jobid=None, state=None, **kwargs):
    """
    Kill launched jobs.

    Parameters
    ----------
    queue : Queue, list, default=None
        Queue or list of queues to process.
        If ``None``, ``jobid`` must be provided.

    provider : BaseProvider, str, dict, default=None
        To access :meth:`BaseProvider.kill` method.
        See :func:`get_provider`.

    jobid : str, list, default=None
        IDs of jobs to kill, defaults to all.

    state : str, default=None
        State of tasks to kill, defaults to all.

    **kwargs : dict
        Optional arguments to select tasks in ``queue`` to be killed: tid, mid, name.
        See :meth:`Queue.tasks`.
    """
    if provider is not None:
        provider = get_provider(provider)
    if jobid is not None:
        jobid = _make_list(jobid, tp=str)[0]
    elif state is None:
        state = TaskState.RUNNING
    if queue is None:
        if jobid is None:
            raise ValueError("Provide at least queue or jobid")
        if provider is None:
            raise ValueError("Provide provider")
        provider.kill(*jobid)
    else:
        queues = get_queue(queue, create=False, one=False)
        for queue in queues:
            jobids = {}
            for task in queue.tasks(jobid=jobid, one=False, state=state, **kwargs):
                if not bool(task.jobid): continue
                if provider is not None and provider.__class__ is not task.app.task_manager.provider.__class__: continue
                prov = task.app.task_manager.provider.name
                jobids.setdefault(prov, set())
                jobids[prov].add(task.jobid)
            for jid, prov in queue.processes():
                if provider is not None and provider.name != prov: continue
                jobids.setdefault(prov, set())
                jobids[prov].add(jid)
            for prov, jid in jobids.items():
                get_provider(prov).kill(*jid)


def retry(queue, state=TaskState.KILLED, **kwargs):
    """
    Move (by default killed) tasks into PENDING state, so they are rerun.

    Parameters
    ----------
    queue : Queue, list, default=None
        Queue or list of queues to process.
        If ``None``, ``jobid`` must be provided.

    state : str, default="KILLED"
        State of tasks to move to PENDING state.

    **kwargs : dict
        Optional arguments to select tasks in ``queue`` to be moved to PENDING state: tid, mid, name, etc.
    """
    queues = get_queue(queue, create=False, one=False)
    for queue in queues:
        for tid in queue.tasks(property="tid", one=False, state=state, **kwargs):
            queue.set_task_state(tid, state=TaskState.PENDING)


def get_queue(queue, create=None, one=True):
    """
    Return queue.

    Parameters
    ----------
    queue : str, Queue, list
        Queue names, :class:`Queue`, or list of such objects.

    create : bool, default=None
        If ``True``, create a new queue; a :class:`ValueError` is raised if the queue already exists.
        If ``False``, do not create the queue; a class:`ValueError` is raised if no queue exists.
        If ``None`` (default), create the queue if does not exist.

    one : bool, default=True
        If ``True``, return one queue. Raise a :class:`ValueError` if ``queue`` corresponds to several queues.
        If ``False``, ensure a list of queues is returned.

    Returns
    -------
    queue : Queue, list
        Queue or list of queues.
    """
    def format_output(queues):
        if one:
            if isinstance(queues, list):
                if len(queues) > 1:
                    raise ValueError("Provide single queue!")
                return queues[0]
        elif not isinstance(queues, list):
            return [queues]
        return queues

    if isinstance(queue, list):
        toret = []
        for queue in queue:
            toret += get_queue(queue, create=create, one=False)
        return format_output(toret)
    if isinstance(queue, Queue):
        return format_output(queue)
    if "/" in queue:
        if queue.startswith("."):
            queue = os.path.abspath(queue)
        else:
            queue = os.path.join(Config()["base_queue_dir"], queue)
    else:
        queue = os.path.join(Config().queue_dir, queue)
    if "*" in queue:
        toret = []
        for queue in glob.glob(queue):
            if queue.endswith(".sqlite"):
                toret += get_queue(queue, create=create, one=False)
        return format_output(toret)
    return format_output(Queue(queue, create=create))


def action_from_args(action="work", args=None):

    """Function called when using desipipe from the command line."""

    logger = logging.getLogger("desipipe")
    from .utils import setup_logging

    setup_logging()

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    if action == "queues":

        parser.add_argument("-q", "--queue", type=str, required=False, default="*/*", help="Name of queue; user/queue to select user != {} and e.g. */* to select all queues of all users)".format(Config.default_user))
        args = parser.parse_args(args=args)
        queues = get_queue(args.queue, create=False, one=False)
        if not queues:
            logger.info("No matching queue")
            return
        logger.info("Matching queues:")
        for queue in queues:
            logger.info(str(queue))
        return

    if action == "work":

        parser.add_argument("-q", "--queue", type=str, required=True, help="Name of queue; user/queue to select user != {}".format(Config.default_user))
        parser.add_argument("--mid", type=str, required=False, default=None, help="Task manager ID")
        parser.add_argument("--tid", type=str, required=False, default=None, help="Task ID")
        parser.add_argument("--name", type=str, required=False, default=None, help="Task name")
        parser.add_argument("--mode", nargs="*", type=str, required=False, default=tuple(), help="Processing mode; 'stop_at_error' to stop as soon as a task is failed")
        parser.add_argument("--mpisplits", type=int, required=False, default=None, help="Number of MPI splits")
        args = parser.parse_args(args=args)
        return work(args.queue, mid=args.mid, tid=args.tid, name=args.name, mode=" ".join(args.mode), mpisplits=args.mpisplits)

    if action == "tasks":

        parser.add_argument("-q", "--queue", type=str, required=True, help="Name of queue; user/queue to select user != {}".format(Config.default_user))
        parser.add_argument("--mid", type=str, required=False, default=None, help="Task manager ID")
        parser.add_argument("--tid", type=str, required=False, default=None, help="Task ID")
        parser.add_argument("--name", type=str, required=False, default=None, help="Task name")
        parser.add_argument("--jobid", type=str, required=False, default=None, help="Job ID")
        parser.add_argument("--state", nargs="*", type=str, required=False, default=TaskState.ALL, choices=TaskState.ALL, help="Task state")
        properties = ["jobid", "errno", "err", "out"]
        parser.add_argument("--property", nargs="*", type=str, required=False, default=properties, choices=properties, help="Task properties")
        args = parser.parse_args(args=args)
        queue = get_queue(args.queue, create=False, one=True)
        for state in args.state:
            tasks = queue.tasks(state=state, tid=args.tid, name=args.name, mid=args.mid, jobid=args.jobid, one=False)
            if tasks:
                logger.info("Tasks that are {}:".format(state))
                for task in tasks:
                    logger.info("app: {}".format(task.app.name))
                    for name in args.property:
                        value = getattr(task, name)
                        if value: logger.info("{}: {}".format(name, value))
                    logger.info("=" * 30)
        return

    if action == "time":
        parser.add_argument("-q", "--queue", type=str, required=True, help="Name of queue; user/queue to select user != {}".format(Config.default_user))
        parser.add_argument("--mid", type=str, required=False, default=None, help="Task manager ID")
        parser.add_argument("--tid", type=str, required=False, default=None, help="Task ID")
        parser.add_argument("--name", type=str, required=False, default=None, help="Task name")
        parser.add_argument("--jobid", type=str, required=False, default=None, help="Job ID")
        args = parser.parse_args(args=args)
        s, s2, counts = {}, {}, {}
        queue = get_queue(args.queue, create=False, one=True)
        tasks = queue.tasks(state=TaskState.SUCCEEDED, tid=args.tid, name=args.name, mid=args.mid, jobid=args.jobid, one=False)
        for task in tasks:
            name = task.app.name
            s[name] = s.get(name, 0) + task.dtime
            s2[name] = s2.get(name, 0) + task.dtime**2
            counts[name] = counts.get(name, 0) + 1
        logger.info("Time:")
        for name in counts:
            mean = s[name] / counts[name]
            cov = (s2[name] - mean * s[name]) / (counts[name] - 1) if counts[name] > 1 else 0.
            logger.info("{}: {:.1f} +/- {:.1f}".format(name, mean, cov**0.5))
        return

    if action == "kill":

        parser.add_argument("-q", "--queue", nargs="*", type=str, required=False, default=None, help="Name of queue; user/queue to select user != {} and e.g. */* to select all queues of all users)".format(Config.default_user))
        parser.add_argument("--mid", type=str, required=False, default=None, help="Task manager ID")
        parser.add_argument("--tid", type=str, nargs="*", required=False, default=None, help="Task ID")
        parser.add_argument("--name", type=str, required=False, default=None, help="Task name")
        parser.add_argument("--jobid", type=str, nargs="*", required=False, default=None, help="Job ID")
        parser.add_argument("--provider", type=str, required=False, default=None, help="Provider")
        parser.add_argument("--state", type=str, required=False, default=TaskState.RUNNING, choices=TaskState.ALL, help="Task state")
        args = parser.parse_args(args=args)
        return kill(queue=args.queue, provider=args.provider, jobid=args.jobid, state=args.state, tid=args.tid, mid=args.mid)

    parser.add_argument("-q", "--queue", nargs="*", type=str, required=True, help="Name of queue; user/queue to select user != {} and e.g. */* to select all queues of all users)".format(Config.default_user))

    if action == "delete":

        parser.add_argument("--force", action="store_true", help="Pass this flag to force delete")
        args = parser.parse_args(args=args)
        queues = get_queue(args.queue, create=False, one=False)
        if not queues:
            logger.info("No queue to delete.")
            return
        logger.info("I will delete these queues:")
        for queue in queues:
            logger.info(str(queue))
        if not args.force:
            logger.warning("--force is not set. To actually delete the queues, pass --force")
            return
        for queue in queues:
            queue.delete()
        return

    if action == "pause":

        args = parser.parse_args(args=args)
        queues = get_queue(args.queue, create=False, one=False)
        for queue in queues:
            logger.info("Pausing queue {}".format(repr(queue)))
            queue.pause()
        return

    if action == "resume":

        parser.add_argument("--spawn", action="store_true", help="Spawn a new manager process")
        args = parser.parse_args(args=args)
        queues = get_queue(args.queue, create=False, one=False)
        for queue in queues:
            logger.info("Resuming queue {}".format(repr(queue)))
            queue.resume()
        if args.spawn:
            spawn(args.queue, spawn=True)
        return

    if action == "spawn":

        parser.add_argument("--timeout", type=float, required=False, default=3600 * 24, help="Stop after this time")
        parser.add_argument("--timestep", type=float, required=False, default=3., help="Period (in seconds) at which the queue is queried for new tasks")
        parser.add_argument("--mode", type=str, required=False, default="", help="Processing mode; 'stop_at_error' to stop as soon as a task is failed; 'retry_at_timeout' to retry when time out; 'no_stream' to not stream stderr/stdout during the tasks (helps when many jobs in parallel. 'no_out' to not stream stderr/stdout and not save stdout.")
        parser.add_argument("--max-workers", type=int, required=False, default=None, help="Maximum number of workers, overrides scheduler max_workers")
        parser.add_argument("--spawn", action="store_true", help="Spawn a new manager process and exit this one")
        args = parser.parse_args(args=args)
        return spawn(args.queue, timeout=args.timeout, mode=args.mode, max_workers=args.max_workers, spawn=args.spawn)

    if action == "retry":

        parser.add_argument("--mid", type=str, required=False, default=None, help="Task manager ID")
        parser.add_argument("--tid", type=str, required=False, default=None, help="Task ID")
        parser.add_argument("--state", type=str, required=False, default=TaskState.KILLED, choices=TaskState.ALL, help="Task state")
        parser.add_argument("--name", type=str, required=False, default=None, help="Task name")
        parser.add_argument("--jobid", type=str, required=False, default=None, help="Job ID")
        parser.add_argument("--spawn", action="store_true", help="Spawn a new manager process")
        args = parser.parse_args(args=args)
        queues = get_queue(args.queue, create=False, one=False)
        retry(queues, mid=args.mid, tid=args.tid, name=args.name, state=args.state, jobid=args.jobid)
        if args.spawn:
            spawn(queues, spawn=True)
        return

    raise ValueError("unknown action {}; pick from {}".format(action, list(action_from_args.actions.keys())))


# Description for command line help
action_from_args.actions = {
    "spawn": "Spawn a manager process to distribute tasks among workers (if queue is not paused)",
    "pause": "Pause a queue: all workers and managers of provided queues (exclusively) stop after finishing their current task",
    "resume": "Restart a queue: running manager processes (if any running) distribute again work among workers",
    "delete": "Delete queue and data base",
    "queues": "List queues",
    "tasks": "List tasks of given queue",
    "time": "List mean execution time of SUCCEEDED tasks, sorted by name",
    "retry": "Move (by default killed) tasks into PENDING state, so they are rerun.",
    "kill": "Kill running tasks of given queue",
}