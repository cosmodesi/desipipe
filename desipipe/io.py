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

    """Base class to handle a file, with path saved as :attr:`path`, and :meth:`read` and :meth:`write` methods."""

    name = 'base'

    def __init__(self, path):
        self.path = path

    def read(self):
        raise NotImplementedError('Implement read method in {}'.format(self.__class__.__name__))

    def write(self):
        raise NotImplementedError('Implement write method in {}'.format(self.__class__.__name__))


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


class CatalogFile(BaseFile):

    """Catalog file."""
    name = 'catalog'

    def read(self, *args, **kwargs):
        """Read catalog."""
        from mpytools import Catalog
        return Catalog.read(self.path, *args, **kwargs)

    def write(self, catalog, *args, **kwargs):
        """Write catalog."""
        return catalog.write(self.path, *args, **kwargs)


class PowerSpectrumFile(BaseFile):

    """Power spectrum file."""
    name = 'power'

    def read(self):
        """Read power spectrum."""
        from pypower import MeshFFTPower, PowerSpectrumMultipoles
        with utils.LoggingContext(level='warning'):
            toret = MeshFFTPower.load(self.path)
            if hasattr(toret, 'poles'):
                toret = toret.poles
            else:
                toret = PowerSpectrumMultipoles.load(self.path)
        return toret

    def write(self, power):
        """Write power spectrum."""
        return power.save(self.path)