import os
import sys
import copy
import contextlib

from .utils import BaseDict


def _insert_first(li, el):
    # Remove element el from list li if exists,
    # then add it at the start of li
    while True:
        try:
            li.remove(el)
        except ValueError:
            break
    li.insert(0, el)
    return li


def bash_env(cmd):
    """Run input command in a subprocess and collect environment variables."""
    import subprocess
    environ = {}
    if isinstance(cmd, (tuple, list)): cmd = ' && '.join(cmd)
    result = subprocess.run(['bash', '-c', '{} && env'.format(cmd)], capture_output=True, text=True)
    for line in result.stdout.split('\n'):
        try:
            key, value = line.split('=')
            environ[key] = value
        except ValueError:
            pass
    return environ


def _make_list(obj, tp=str):
    if obj is None:
        return tuple()
    if not isinstance(obj, (tuple, list)):
        obj = [obj]
    return tuple(tp(oo) for oo in obj)


class RegisteredEnvironment(type(BaseDict)):

    """Metaclass registering :class:`BaseEnvironment`-derived classes."""

    _registry = {}

    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)
        meta._registry[cls.name] = cls
        return cls


class BaseEnvironment(BaseDict, metaclass=RegisteredEnvironment):
    """
    Base environment dictionary-like class.
    Environment is stored as key, value pairs and an optional extra bash command.
    """
    name = 'base'
    _defaults = dict()

    def __init__(self, data=None, command=None):
        """
        Initialize environment.

        Parameters
        ----------
        data : dict, default=None
            Dictionary of environment variables.

        command : str, default=None
            Optional bash command that sources the environment.
        """
        self.data = {}
        for name, value in self._defaults.items():
            self.setdefault(name, copy.copy(value))
        self.update((data or {}), command=command)
    
    def update(self, *args, **kwargs):
        """Update."""
        if 'command' in kwargs:
            command = kwargs.pop('command')
            self.command = _make_list(getattr(self, '_command', []), tp=str) + _make_list(command, tp=str)
        super().update(*args, **kwargs)

    def to_dict(self, all=False):  # including command
        """
        Return environment as a dictionary.
        If ``all``, also include environment variables defined by :attr:`command`.
        """
        new = self.copy()
        if all:
            new.update(bash_env(self.command))
        return dict(new)
    
    def to_script(self, sep='\n', all=True):
        """
        Export environment as a bash script, including both the command :attr:`command`
        and variables defined in this instance.
        """
        tojoin = []
        if all and self.command:
            tojoin += [*self.command]
        tojoin += ['export {}={}'.format(name, value) for name, value in self.items()]
        out = sep.join(tojoin)
        return out


def get_environ(environ=None, data=None, **kwargs):
    """
    Convenient function that returns an environment.

    Parameters
    ----------
    environ : BaseEnvironment, str, dict, default=None
        A :class:`BaseEnvironment` instance, which is then returned directly,
        a string specifying the name of the environment (e.g. 'base', 'nersc-cosmodesi')
        or a dictionary of environment variables.
        If not specified, the default environment in desipipe's configuration
        (see :class:`Config`) if provided, else 'base'.

    data : dict, default=None
        Optionally, additional environment variables.

    **kwargs : dict
        Optionally, bash command for the environment, see :class:`BaseEnvironment`.

    Returns
    -------
    environ : BaseEnvironment
    """
    if isinstance(environ, BaseEnvironment):
        return environ
    if isinstance(environ, dict):
        environ, data = environ.pop('environ', None), {**environ, **(data or {})}
    if environ is None:
        from .config import Config
        config = Config().get('environ', {})
        if isinstance(config, str):
            environ = config
            config = {}
        else:
            environ = config.get('name', 'base')
        data = {**config.get('data', {}), **(data or {})}
        kwargs.setdefault('command', config.get('command', None))
    return BaseEnvironment._registry[environ](data=data, **kwargs)


Environment = get_environ


@contextlib.contextmanager
def change_environ(environ):
    """
    Temporarily set the process environment variables.

    >>> with change_environ(PLUGINS_DIR='test/plugins'):
    ...   "PLUGINS_DIR" in os.environ
    True

    >>> "PLUGINS_DIR" in os.environ
    False

    :type environ: dict[str, unicode]
    :param environ: Environment variables to set
    """
    if environ is None:
        yield
    environ_bak = os.environ.copy()
    syspath_bak = sys.path.copy()
    os.environ.clear()
    os.environ.update(environ)
    for key, value in environ.items():
        if key == 'PYTHONPATH':
            for path in value.split(':')[::-1]: _insert_first(sys.path, path)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(environ_bak)
        sys.path.clear()
        sys.path += syspath_bak


class BaseNERSCEnvironment(BaseEnvironment):

    """Base NERSC environment."""

    name = 'nersc-base'
    _defaults = dict(DESICFS='/global/cfs/cdirs/desi')


class CosmodesiNERSCEnvironment(BaseNERSCEnvironment):

    """cosmodesi environment at NERSC."""

    name = 'nersc-cosmodesi'
    _command = 'source /global/common/software/desi/users/adematti/cosmodesi_environment.sh main'