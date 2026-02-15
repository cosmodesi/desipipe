"""To implement a new file format, just subclass :class:`BaseFile`."""

import os

from . import utils
from .utils import BaseClass


class RegisteredFile(type(BaseClass)):

    """Metaclass registering :class:`BaseFile`-derived classes."""

    _registry = {}

    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)
        meta._registry[cls.name] = cls
        return cls


class BaseFile(BaseClass, metaclass=RegisteredFile):

    """Base class to handle a file, with path saved as :attr:`path`, and :meth:`load` and :meth:`save` methods."""

    name = 'base'

    def __init__(self, path):
        self.path = path

    def load(self):
        raise NotImplementedError('Implement load method in {}'.format(self.__class__.__name__))

    def save(self):
        raise NotImplementedError('Implement save method in {}'.format(self.__class__.__name__))


def get_filetype(filetype, path, *args, **kwargs):
    """
    Convenient function that returns a :class:`BaseFile` instance.

    Parameters
    ----------
    filetype : str, :class:`BaseFile`
        Name of :class:`BaseFile`, or :class:`BaseFile` instance.

    path : str
        Path to file.

    *args : tuple
        Other arguments for :class:`BaseFile`.

    **kwargs : dict
        Other optional arguments for :class:`BaseFile`.

    Returns
    -------
    file : BaseFile
    """
    if isinstance(filetype, BaseFile):
        return filetype
    return BaseFile._registry[filetype](path, *args, **kwargs)


class TextFile(BaseFile):

    """Text file."""
    name = 'text'

    def load(self):
        """Load file."""
        with open(self.path, 'r') as file:
            return file.read()

    def save(self, txt):
        """Save file."""
        utils.mkdir(os.path.dirname(self.path))
        with open(self.path, 'w') as file:
            file.write(txt)


class GenericFile(BaseFile):

    """Generic file."""
    name = 'generic'

    def load(self, load, **kwargs):
        """Load file."""
        for name in ['load', 'read']:
            func = getattr(load, name, None)
            if callable(func):
                load = func
                break
        return load(self.path, **kwargs)

    def save(self, save, **kwargs):
        """Save file."""
        for name in ['save', 'write']:
            func = getattr(save, name, None)
            if callable(func):
                save = func
                break
        save(self.path, **kwargs)