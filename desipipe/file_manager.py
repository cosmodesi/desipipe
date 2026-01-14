import os
import glob
import re
import copy
import shutil
import itertools
import tempfile

import yaml

from . import utils
from .utils import BaseClass
from .environment import get_environ
from .io import get_filetype


class YamlLoader(yaml.SafeLoader):
    """
    *yaml* loader that correctly parses numbers.
    Taken from https://stackoverflow.com/questions/30458977/yaml-loads-5e-6-as-string-and-not-a-number.
    """


# https://stackoverflow.com/questions/30458977/yaml-loads-5e-6-as-string-and-not-a-number
YamlLoader.add_implicit_resolver(u'tag:yaml.org,2002:float',
                                 re.compile(u'''^(?:
                                 [-+]?(?:[0-9][0-9_]*)\\.[0-9_]*(?:[eE][-+]?[0-9]+)?
                                 |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
                                 |\\.[0-9_]+(?:[eE][-+][0-9]+)?
                                 |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\\.[0-9_]*
                                 |[-+]?\\.(?:inf|Inf|INF)
                                 |\\.(?:nan|NaN|NAN))$''', re.X),
                                 list(u'-+0123456789.'))

YamlLoader.add_implicit_resolver('!none', re.compile('None$'), first='None')


def none_constructor(loader, node):
    return None


YamlLoader.add_constructor('!none', none_constructor)


def yaml_parser(string):
    """Parse string in *yaml* format."""
    return list(yaml.load_all(string, Loader=YamlLoader))


class BaseMutableClass(BaseClass):
    """Base mutable class, merely used to factorize code of :class:`BaseFileEntry` and :class:`BaseFile`."""
    _defaults = dict()

    def __init__(self, *args, **kwargs):
        """
        Initialize :class:`BaseMutableClass`.

        Parameters
        ----------
        *args : dict, :class:`BaseMutableClass`
            Dictionary of (some) attributes (see above) or :class:`BaseMutableClass` instance.

        **kwargs : dict
            Optionally, more attributes.
        """
        if len(args):
            if isinstance(args[0], self.__class__):
                self.__dict__.update(args[0].__dict__)
                return
            try:
                kwargs = {**args[0], **kwargs}
            except TypeError:
                args = dict(zip(self._defaults, args))
                kwargs = {**args, **kwargs}
        for name, value in self._defaults.items():
            setattr(self, name, copy.copy(value))
        self.update(**kwargs)

    def clone(self, **kwargs):
        """Return an updated copy."""
        new = self.copy()
        new.update(**kwargs)
        return new

    def update(self, **kwargs):
        """Update input attributes."""
        for name, value in kwargs.items():
            if name in self._defaults:
                default = self._defaults[name]
                if default is not None:
                    value = type(default)(value)
                setattr(self, name, value)
            else:
                raise ValueError('unknown argument {}; supports {}'.format(name, list(self._defaults)))

    def __setstate__(self, state):
        self.__dict__.update(self._defaults | state)

    def deepcopy(self):
        """Return a deep copy."""
        new = self.copy()
        for name, value in self._defaults.items():
            setattr(new, name, copy.copy(getattr(self, name, value)))
        return new

    def to_dict(self):
        """View as a dictionary (of attributes)."""
        return {name: getattr(self, name) for name in self._defaults}

    def __eq__(self, other):
        return type(other) == type(self) and all(_deep_eq(getattr(other, name), getattr(self, name)) for name in self._defaults)

    def __getstate__(self):
        return dict(self.__dict__)

    def __setstate__(self, state):
        self.__dict__.update(self._defaults | state)


def _deep_eq(obj1, obj2):
    """(Recursively) test equality between ``obj1`` and ``obj2``."""
    if type(obj2) == type(obj1):
        if isinstance(obj1, dict):
            if obj2.keys() == obj1.keys():
                return all(_deep_eq(obj1[name], obj2[name]) for name in obj1)
        elif hasattr(obj1, '__iter__') and not isinstance(obj1, str):
            if len(obj2) == len(obj1):
                return all(_deep_eq(o1, o2) for o1, o2 in zip(obj1, obj2))
        else:
            return obj2 == obj1
    return False


def _make_list(values):
    """Turn input ``values`` (single value, list, iterator, etc.) into a list."""
    if isinstance(values, range):
        return values
    if not hasattr(values, '__iter__') or isinstance(values, str):
        values = [values]
    return list(values)


def _make_list_options(values):
    if values is Ellipsis:
        return values
    if not isinstance(values, (list, range)):
        return [values]
    return values


def in_options(values, options, return_index=False):
    """Return input values that are in options."""
    if not options:
        return []

    def get_ndim(opt):
        if hasattr(opt, '__iter__') and not isinstance(opt, str):
            return 1 + max([0] + [get_ndim(opt) for opt in opt])
        return 0

    if values is Ellipsis:
        values = options
    if options is Ellipsis:
        toret = values
        if toret is not Ellipsis and get_ndim(toret) == 0:
            toret = [toret]
        if return_index:
            return toret, Ellipsis
        return toret

    options = list(options)
    if values in options or get_ndim(values) == 0:
        values = [values]

    toret, index = [], []
    for value in values:
        try:
            tval, topt = type(value), type(options[0])
            if tval is not topt and all(topt is type(opt) for opt in options):
                value = topt(value)
        except Exception as exc:
            pass
            #raise ValueError('issue with converting {} of type {} to type {} to match options {}'.format(value, tval, topt, options)) from exc
        if value in options:
            ii = options.index(value)
            toret.append(options[ii])
            index.append(ii)
    if return_index:
        return toret, index
    return toret


def iter_options(options, include=None, exclude=None, yield_index=False):
    if include is None:
        include = list(options.keys())
    if exclude is None:
        exclude = []
    include, exclude = _make_list(include), _make_list(exclude)
    include = [name for name in options if name in include and name not in exclude]

    for ivalues in itertools.product(*([Ellipsis] if options[name] is Ellipsis else range(len(options[name])) for name in include)):
        opt = {}
        for iname, name in enumerate(include):
            opt[name] = Ellipsis if options[name] is Ellipsis else options[name][ivalues[iname]]
        if yield_index:
            yield opt, ivalues
        else:
            yield opt


def path_replace_environ(path, environ=None):
    environ = environ or {}
    placeholders = re.finditer(r'\$\{.*?\}', path)
    for placeholder in placeholders:
        placeholder = placeholder.group()
        placeholder_nobrackets = placeholder[2:-1]
        if placeholder_nobrackets in environ:
            path = path.replace(placeholder, environ[placeholder_nobrackets])
    return path


class JointMetaClass(type(os.PathLike), type(BaseClass)): pass



def _find_basedir(path):
    index = re.match(r"\{[^{}]*\}", path)
    if index is not None: index = index.start(0)
    else: index = None
    olddir = path[:index]
    if not olddir.endswith(os.sep):
        olddir = os.path.dirname(olddir)
    return olddir


class BaseFile(BaseMutableClass, os.PathLike, metaclass=JointMetaClass):
    """
    Class describing a single file (single :attr:`options` values).

    Attributes
    ----------
    path : str, default=''
        Path to file(s). May contain placeholders, e.g. 'data_{tracer}_{region}.fits',
        with the list of values that ``tracer``, ``region`` may take specified in ``options`` (see below).

    filetype : str, default=''
        BaseFile type, e.g. 'catalog', 'power', etc.

    id : str, default=''
        BaseFile (unique) identifier.

    author : str, default=''
        Who produced this file.

    options : dict, default=dict()
        Dictionary matching placeholders in ``path`` to the list of values they can take, e.g. {'region': ['NGC', 'SGC']}

    description : str, default=''
        Plain text describing the file(s).

    link : str, default=''
        Symlink.
    """
    _defaults = dict(path='', ro=None, filetype='generic', id='', author='', options=dict(), foptions=dict(), description='', link='')

    def update(self, **kwargs):
        """Update input attributes."""
        super(BaseFile, self).update(**kwargs)
        if 'options' in kwargs:
            self.options, self.foptions = dict(kwargs['options']), dict(kwargs['options'])
        if 'foptions' in kwargs:
            self.foptions = dict(kwargs['foptions'])
        for name, values in self.options.items():
            self.foptions.setdefault(name, values)

    def _resolve_path(self, path):
        """Real path i.e. replacing placeholders in :attr:`path` by their value."""
        path = path_replace_environ(path, environ=getattr(self, 'environ', {}))

        # return path.format(**self.foptions)
        def fstr(template, kwargs):
            try:
                return eval(f"f'{template}'", dict(), kwargs)
            except NameError as exc:
                raise ValueError('make sure that all options used in the template path {} are defined in :attr:`options`: {}'.format(self.path, self.options)) from exc

        path = fstr(path, self.foptions)
        if '*' in path:
            exist = glob.glob(path)
            if len(exist) == 1:
                return exist[0]
            if len(exist) > 1:
                raise ValueError('found multiple paths for {}: {}'.format(path, exist))
        return path

    def __fspath__(self):
        """Real path i.e. replacing placeholders in :attr:`path` by their value."""
        return self._resolve_path(self.path)

    def load(self, *args, **kwargs):
        """Load file from disk."""
        filepath = self.filepathro
        self.log_info('Loading {}'.format(filepath))
        return get_filetype(filetype=self.filetype, path=filepath).load(*args, **kwargs)

    def save(self, *args, **kwargs):
        """
        Save file to disk. First written in a temporary directory, then moved to its final destination.
        To save additional files, a method :attr:`save_attrs`, that should take the path to the directory as input,
        can be added to the current instance.
        """
        save_attrs = getattr(self, 'save_attrs', None)
        filepath = self.__fspath__()
        dirname = os.path.dirname(filepath)
        utils.mkdir(dirname)
        if save_attrs is not None:
            with tempfile.TemporaryDirectory(dir=dirname) as tmp_dir:
                shutil.copystat(dirname, tmp_dir)  # set same permissions as dirname, which are then copied by copytree below
                new_dir = save_attrs(tmp_dir) or dirname
                utils.copytree(tmp_dir, new_dir, dirs_exist_ok=True, copy_function=shutil.copyfile, dirs_copystat=False) # copy directory and file contents without trying to change permissions via shutil.copystat
                try: utils.copytree(tmp_dir, new_dir, dirs_exist_ok=True, copy_function=shutil.copystat, dirs_copystat=True) # attempt to copy directory and file metadata with shutil.copystat; this can fail if the owner of dirname or some files in it is different from the current user
                except Exception as e: self.log_warning('Failed to copy metadata recursively from {0} to {1}: {2}'.format(tmp_dir, new_dir, e)) # this failure is not critical, report as warning and move on
        with tempfile.TemporaryDirectory(dir=dirname) as tmp_dir:
            shutil.copystat(dirname, tmp_dir)  # set same permissions as dirname, which are then copied by copytree below
            path = os.path.join(tmp_dir, os.path.basename(filepath))
            toret = get_filetype(filetype=self.filetype, path=path).save(*args, **kwargs)
            self.log_info('Moving output to {}'.format(filepath))
            utils.copytree(tmp_dir, dirname, dirs_exist_ok=True, copy_function=shutil.copyfile, dirs_copystat=False) # copy directory and file contents without trying to change permissions via shutil.copystat
            try: utils.copytree(tmp_dir, dirname, dirs_exist_ok=True, copy_function=shutil.copystat, dirs_copystat=True) # attempt to copy directory and file metadata with shutil.copystat; this can fail if the owner of dirname or some files in it is different from the current user
            except Exception as e: self.log_warning('Failed to copy metadata recursively from {0} to {1}: {2}'.format(tmp_dir, dirname, e)) # this failure is not critical, report as warning and move on
            return toret

    def exists(self):
        """``True`` if file exists, else ``False``."""
        return os.path.isfile(self.__fspath__())

    def symlink(self, raise_error=True):
        """Create symlink."""
        filepath = self.filepath
        try:
            if not os.path.exists(filepath):
                raise FileNotFoundError('{} does not exist'.format(filepath))
            sympath = self.sympath
            utils.mkdir(os.path.dirname(sympath))
            if os.path.islink(sympath): os.remove(sympath)
            return os.symlink(filepath, sympath)
        except Exception as exc:
            if raise_error: raise exc

    @property
    def filepath(self):
        return self.__fspath__()

    @property
    def filepathro(self):
        filepath = self.__fspath__()
        if self.ro is not None:
            filepath = filepath.replace(*map(self._resolve_path, self.ro))
        return filepath

    @property
    def sympath(self):
        if not self.link:
            raise ValueError('no link given for {}, try update(link=...)'.format(self))
        return self._resolve_path(self.link)

    def __str__(self):
        return self.__fspath__()

    def __hash__(self):
        return hash(self.__fspath__())

    def __repr__(self):
        """String representation: class name and attributes."""
        di = self.to_dict()
        di.pop('path'); di.pop('link')
        di['filepath'] = self.__fspath__()
        if self.link: di['symlink'] = self._resolve_path(self.link)
        di.pop('foptions')
        if self.ro is None: di.pop('ro')
        #return '{}({})'.format(self.__class__.__name__, ', '.join(['{}={}'.format(name, value) for name, value in di.items()]))
        return '{}(\n{}\n)'.format(self.__class__.__name__, ',\n'.join(['{}: {}'.format(name, value) for name, value in di.items()]))

    def __truediv__(self, other):
        """Return ``self/other``, including a separator."""
        new = self.deepcopy()
        if isinstance(other, BaseFile):
            new.path = os.path.join(self.path, other.path)
        else:
            new.path = os.path.join(self.path, other)
        return new

    def __itruediv__(self, other):
        """Return other/self, including a separator."""
        new = self.deepcopy()
        new.path = os.path.join(other, self.path)
        return new

    def dirname(self):
        """Return ``os.path.dirname(self)``."""
        new = self.deepcopy()
        new.path = os.path.dirname(self.path)
        return new

    def basename(self):
        """Return ``os.path.basename(self)``."""
        new = self.deepcopy()
        new.path = os.path.basename(self.path)
        return new

    def stemname(self):
        """Return ``os.path.splitext(self)[0]``."""
        new = self.deepcopy()
        new.path = os.path.splitext(self.path)[0]
        return new

    def extname(self):
        """Return ``os.path.splitext(self)[1]``."""
        return os.path.splitext(self.__fspath__())[1]

    @property
    def parent(self):
        """Equivalent of :meth:`dirname`, following https://docs.python.org/fr/3/library/pathlib.html."""
        return self.dirname()

    @property
    def name(self):
        """Equivalent of :meth:`basename`, following https://docs.python.org/fr/3/library/pathlib.html."""
        return self.basename()

    @property
    def stem(self):
        """Equivalent of :meth:`stemname`, following https://docs.python.org/fr/3/library/pathlib.html."""
        return self.stemname()

    @property
    def suffix(self):
        """Equivalent of :meth:`extname`, following https://docs.python.org/fr/3/library/pathlib.html."""
        return self.extname()

    def chgdir(self, newdir, olddir=None):
        """
        Change directory for new one in :attr:`path`.
        As a default, ``olddir`` (directory to be replaced) is taken to be
        the top directory which does not contain replacement options.
        """
        path = self.path
        if olddir is None:
            olddir = _find_basedir(path)
        path = os.path.join(newdir, os.path.relpath(path, olddir))
        self.update(path=path)


class RegisteredFileEntry(type(BaseMutableClass)):

    """Metaclass registering :class:`BaseFileEntry`-derived classes."""

    _registry = {}

    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)
        meta._registry[cls.name] = cls
        return cls


class BaseFileEntry(BaseMutableClass, metaclass=RegisteredFileEntry):
    """
    Class describing a file entry.
    It can be subclassed to implement a special behaviour, e.g. a complex relation between options and the file path.
    For this, you may only need to update :meth:`_get_file`, which returns a :class:`BaseFile` instance given the input options.

    Attributes
    ----------
    path : str, default=''
        Path to file(s). May contain placeholders, e.g. 'data_{tracer}_{region}.fits',
        with the list of values that ``tracer``, ``region`` may take specified in ``options`` (see below).

    filetype : str, default=''
        BaseFile type, e.g. 'catalog', 'power', etc.

    id : str, default=''
        BaseFile (unique) identifier.

    author : str, default=''
        Who produced this file.

    options : dict, default=dict()
        Dictionary matching placeholders in ``path`` to the list of values they can take, e.g. {'region': ['NGC', 'SGC']}

    description : str, default=''
        Plain text describing the file(s).

    link : str, default=''
        Symlink.
    """
    name = 'base'
    _defaults = dict(path='', ro=None, filetype='generic', id='', author='', options=dict(), foptions=dict(), description='', link='')
    _file_cls = BaseFile

    def update(self, **kwargs):
        """Update input attributes (options values are turned into lists)."""
        super(BaseFileEntry, self).update(**kwargs)
        foptions = {}
        if 'options' in kwargs:
            options = {}
            for name, values in kwargs['options'].items():
                if values is Ellipsis:
                    values = Ellipsis
                elif isinstance(values, dict):
                    foptions[name] = list(values.values())
                    values = list(values.keys())
                elif isinstance(values, str) and re.match(r'range\((.*)\)$', values):
                    values = eval(values)
                options[name] = values = _make_list_options(values)
            self.options, self.foptions = options, foptions
        if 'foptions' in kwargs:
            self.foptions = {**foptions, **kwargs['foptions']}
        for name, values in self.options.items():
            self.foptions.setdefault(name, values)
        self.foptions = {name: _make_list_options(value) for name, value in self.foptions.items() if name in self.options}

    def select(self, ignore=False, check_exists=False, raise_error=True, **kwargs):
        """
        Restrict to input options, e.g.

        >>> entry.select(region=['NGC'])

        returns a new entry, with option 'region' taking values in ``['NGC']``.

        Parameters
        ----------
        ignore : bool, list, default=False
            If ``True``, ignore input options that are not in these entry's options.
            If list, do not apply a filter for these input options.

        check_exists : bool, default=False
            If ``True``, check whether all files exist; if not, raise a :class:`FileNotFoundError`.

        raise_error : bool, default=True
            If ``False`` and an error is raised, catch it and return ``None``.
        """
        options, foptions = self.options.copy(), self.foptions.copy()
        if not ignore in [False, True]: ignore = _make_list(ignore)
        for name, values in kwargs.items():
            if isinstance(ignore, list) and name in ignore: continue
            if name in options:
                options[name], indices = in_options(values, options[name], return_index=True)
                if indices is Ellipsis:  # Ellipsis
                    foptions[name] = options[name]
                else:
                    foptions[name] = [foptions[name][index] for index in indices]
            elif not ignore:
                if raise_error: raise ValueError('Unknown option {}, select from {}'.format(name, self.options))
                return None
        toret = self.clone(options=options, foptions=foptions)
        if check_exists:
            exists = toret.exists()
            if exists[False]:
                if raise_error: raise FileNotFoundError('file {} not found.'.format(exists[False]))
                return None
        return toret

    def get(self, *args, raise_error=True, **kwargs):
        """
        Return the :class:`BaseFile` instance that matches input arguments, see :meth:`select`.

        Parameters
        ----------
        *args, **kwargs : dict
            If :meth:`select` returns several file entries, a :class:`ValueError` is raised.

        check_exists : bool, default=False
            If ``True``, check whether file exists; if not, raise a :class:`FileNotFoundError`.

        raise_error : bool, default=True
            If ``False`` and an error is raised, catch it and return ``None``.

        Returns
        -------
        file : BaseFile
        """
        new = self.select(*args, raise_error=raise_error, **kwargs)
        if new is None:
            return None
        try:
            if len(new) == 1:
                for fi in new:
                    return fi  # BaseFile instance
            if len(new) == 0:
                raise ValueError('"get" is not applicable as there are no matching entries')
            raise ValueError('"get" is not applicable as there are multiple options:\n{}'.format(new))
        except ValueError:
            if raise_error: raise
            return None

    def __len__(self):
        """Length, i.e. number of individual files (looping over all options) described by this file entry."""
        size = 1
        for values in self.options.values():
            if values is Ellipsis: continue
            size *= len(values)
        return size

    def _get_file(self, options, foptions=None):
        """Return :class:`BaseFile` given input options."""
        if foptions is None:
            foptions = dict(options)
        fi = self._file_cls()
        fi.__dict__.update(self.__dict__)
        fi.options, fi.foptions = options, foptions
        return fi

    def iter_options(self, include=None, exclude=None, return_foptions=False):
        """
        Iterate over options.

        Parameters
        ----------
        include : str, list, default=None
            List of options to include in the iteration.
            ``None`` to include all options.

        exclude : str, list, default=None
            List of options to exclude in the iteration.
            ``None`` to not exclude any option.

        return_foptions : bool, default=False
            If ``True``, return list of ``(options, foptions)``,
            where ``foptions`` are the options to be passed to the formatted path.

        Returns
        -------
        options : list of the options.
        """
        toret = []
        for options, index in iter_options(self.options, include=include, exclude=exclude, yield_index=True):
            foptions = {}
            for iname, name in enumerate(options):
                if options[name] is Ellipsis:
                    foptions[name] = self.foptions[name]
                else:
                    foptions[name] = self.foptions[name][index[iname]]
            if return_foptions:
                toret.append((options, foptions))
            else:
                toret.append(options)

        return toret

    def iter(self, include=None, exclude=None):
        """
        Iterate over options and return the corresponding list of files.

        Parameters
        ----------
        include : str, list, default=None
            List of options to include in the iteration.
            ``None`` to include all options.

        exclude : str, list, default=None
            List of options to exclude in the iteration.
            ``None`` to not exclude any option.

        Returns
        -------
        files : list of files.
        """
        return [self._get_file(options, foptions=foptions) for options, foptions in self.iter_options(include=include, exclude=exclude, return_foptions=True)]

    def __iter__(self):
        """Iterate over all files (looping over all options) described by this file entry."""
        for file in self.iter(include=None, exclude=None):
            yield file

    def __repr__(self):
        """String representation: class name and attributes."""
        di = self.to_dict()
        di.pop('foptions', None)
        if self.ro is None: di.pop('ro')
        #return '{}({})'.format(self.__class__.__name__, ', '.join(['{}={}'.format(name, value) for name, value in di.items()]))
        return '{}(\n{}\n)'.format(self.__class__.__name__, ',\n'.join(['{}: {}'.format(name, value) for name, value in di.items()]))

    @property
    def filepaths(self):
        """All file paths in file entry."""
        return [ff.filepath for ff in self]

    def symlink(self, raise_error=True):
        """Create symlink for all file entries."""
        for ff in self: ff.symlink(raise_error=raise_error)

    def exists(self, return_type='dict'):
        """
        Return summary description of which files exist or not.

        Parameters
        ----------
        return_type : str, default='dict'
            If 'dict', return a dictionary mapping exists to the file paths.
            If 'str', return a string.

        Returns
        -------
        summary : dict, str
        """
        counts = {True: [], False: []}
        for ff in self:
            counts[ff.exists()].append(ff.filepath)
        if return_type == 'dict':
            return counts
        if return_type == 'str':
            toret = []
            for exists, filepaths in counts.items():
                toret.append('exists = {}'.format(exists))
                toret.append('=' * len(toret[-1]))
                toret += filepaths
            return '\n'.join(toret)
        raise ValueError('unknown return_type {}'.format(return_type))

    chgdir = BaseFile.chgdir


def get_file_entry(file_entry=None, file_entry_collection=None, **kwargs):
    """
    Convenient function that returns the file entry.

    Parameters
    ----------
    file_entry : BaseFileEntry, str, dict, default=None
        A :class:`BaseFileEntry` instance, which is then returned directly,
        a string specifying the name of the file entry (e.g. 'base')
        or a dictionary of file entry attributes.
        If not specified, the default file entry in desipipe's configuration
        (see :class:`Config`) is used if provided, else 'base'.

    **kwargs : dict
        Optionally, additional provider attributes.

    Returns
    -------
    provider : BaseProvider
    """
    if isinstance(file_entry, BaseFileEntry):
        return file_entry
    if isinstance(file_entry, dict):
        file_entry, kwargs = file_entry.pop('fileentry', None), {**file_entry, **kwargs}
    if file_entry is None:
        file_entry = 'base'
    if file_entry_collection is not None and 'import' in kwargs:
        ref = file_entry_collection.select(id=kwargs.pop('import')).data[0]
        options = kwargs.get('options', {}) or {}  # in case None
        options = {**ref.options, **options}
        kwargs = {**ref.to_dict(), **kwargs, **{'options': options}}
    return BaseFileEntry._registry[file_entry](**kwargs)


def prod(iterator):
    toret, count = 1, 0
    for item in iterator:
        toret *= item
        count += 1
    if count:
        return toret
    return 0


class FileEntryCollection(BaseClass):

    """A collection of file entries."""

    def __init__(self, data=None, string=None, parser=None, environ=None, **kwargs):
        """
        Initialize :class:`FileEntryCollection`.

        Parameters
        ----------
        data : dict, string, default=None
            Dictionary or path to a data base *yaml* file.

        string : str, default=None
            If not ``None``, *yaml* format string to decode.
            Added on top of ``data``.

        parser : callable, default=None
            Function that parses *yaml* string into a dictionary.
            Used when ``data`` is string, or ``string`` is not ``None``.

        **kwargs : dict
            Arguments for :func:`parser`.
        """
        if isinstance(data, self.__class__):
            self.__dict__.update(data.copy().__dict__)

        self.parser = parser
        if parser is None:
            self.parser = yaml_parser

        self.environ = dict(environ or {})
        self.data = []

        if utils.is_path(data):
            if string is None: string = ''
            with open(data, 'r') as file:
                string += file.read()
        elif data is not None:
            if isinstance(data, (FileEntryCollection, FileManager)):
                data = data.data
            for dd in data:
                self.append(dd)

        if string is not None:
            for dd in self.parser(string, **kwargs):
                self.append(dd)

    def index(self, id=None, filetype=None, keywords=None, return_entry=False, empty_error=False, ignore=False, **kwargs):
        """
        Return indices for input identifiers, keywords, or options, e.g.

        >>> db.index(keywords=['power cutsky', 'fiber'], options={'tracer': 'ELG'})

        selects the index of data base entries whose description contains 'power' and 'cutsky' or 'fiber',
        and option 'tracer' is 'ELG'.

        Parameters
        ----------
        id : list, str, default=None
            List of file entry identifiers.
            Defaults to all identifiers (no selection).

        filetype : list, str, default=None
            List of file types.
            Defaults to all file types (no selection).

        keywords : list, str, default=None
            List of keywords to search for in the file entry descriptions.
            If a string contains several words, all of them must be in the description
            for the corresponding file entry to be selected.
            If a list of strings is provided, any of the strings must be in the description
            for the corresponding file entry to be selected.
            e.g. ``['power cutsky', 'fiber']`` selects the data base entries whose description contains 'power' and 'cutsky' or 'fiber'.

        empty_error : bool, default=False
            If ``True``, return (hopefully meaningful) error if no match is found.

        **kwargs : dict
            Restrict to these options, see :meth:`BaseFileEntry.select`.

        Returns
        -------
        index : list
            List of indices.
        """
        if id is not None:
            id = _make_list_options(id)
            id = [iid.lower() for iid in id]
        if filetype is not None:
            filetype = _make_list_options(filetype)
        if keywords is not None:
            keywords = _make_list_options(keywords)
            keywords = [keyword.lower().split() for keyword in keywords]
        indices, entries = [], []
        not_id, not_filetype, not_keywords, sentries = True, True, True, []
        for ientry, entry in enumerate(self.data):
            if id is not None and entry.id.lower() not in id:
                continue
            not_id = False
            if filetype is not None and entry.filetype.lower() not in filetype:
                continue
            not_filetype = False
            if keywords is not None:
                description = entry.description.lower()
                if not any(all(kw in description for kw in keyword) for keyword in keywords):
                    continue
            not_keywords = False
            sentry = entry.select(ignore=ignore, check_exists=False, raise_error=False, **kwargs)
            if sentry is None or not sentry:
                sentries.append((entry, sentry))
                continue
            indices.append(ientry)
            entries.append(sentry)
        if not indices and empty_error:
            if not self.data:
                raise ValueError('file collection is empty')
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                try:
                    from fuzzywuzzy import process
                except ImportError:
                    pass
                else:
                    limit = 2
                    if id is not None and not_id:
                        raise ValueError('id {} not found, maybe you meant {}?'.format(id, [[tmp[0] for tmp in process.extract(_, [entry.id.lower() for entry in self.data], limit=limit)] for _ in id]))
                    if filetype is not None and not_filetype:
                        raise ValueError('filetype {} not found, maybe you meant {}?'.format(filetype, [[tmp[0] for tmp in process.extract(_, [entry.filetype.lower() for entry in self.data], limit=limit)] for _ in filetype]))
                    if keywords is not None and not_keywords:
                        raise ValueError('keywords {} not found, maybe you meant {}?'.format(keywords, [[tmp[0] for tmp in process.extract(_, [entry.description.lower() for entry in self.data], limit=limit)] for _ in keywords]))
            if not len(self):
                raise ValueError('{} is empty'.format(self.__class__.__name__))
            values_not_found = [(entry, sentry) for entry, sentry in sentries if sentry is not None]
            if not values_not_found:
                options_not_found = [(entry, set(kwargs) - set(entry.options)) for entry, sentry in sentries if sentry is None]
                options_not_found = sorted(options_not_found, key=lambda x: len(x[1]), reverse=False)
                options_never_found = set.union(*[opt for _, opt in options_not_found])
                best_entries = [entry for entry, _ in options_not_found[:limit]]
                raise ValueError('options {} not found in any entry, best matching entries are {}. Note that you can pass ignore=True to ignore input options that cannot be found in any file entry'.format(options_never_found, best_entries))
            values_not_found = sorted(values_not_found, key=lambda x: sum(bool(values) for values in x[1].options.values()), reverse=True)
            values_never_found = {name: value for name, value in kwargs.items() if not all(bool(sentry.options.get(name, [])) for _, sentry in values_not_found)}
            best_entries = [entry for entry, _ in values_not_found[:limit]]
            raise ValueError('option values {} not found, best matching entries are {}'.format(values_never_found, best_entries))

        if return_entry:
            return indices, entries
        return indices

    def select(self, id=None, filetype=None, keywords=None, empty_error=False, **kwargs):
        """
        Restrict to input identifiers, keywords, or options, e.g.

        >>> db.select(keywords=['power cutsky', 'fiber'], options={'tracer': 'ELG'})

        selects the data base entries whose description contains 'power' and 'cutsky' or 'fiber',
        and option 'tracer' is 'ELG'.

        Parameters
        ----------
        id : list, str, default=None
            List of file entry identifiers.
            Defaults to all identifiers (no selection).

        filetype : list, str, default=None
            List of file types.
            Defaults to all file types (no selection).

        keywords : list, str, default=None
            List of keywords to search for in the file entry descriptions.
            If a string contains several words, all of them must be in the description
            for the corresponding file entry to be selected.
            If a list of strings is provided, any of the strings must be in the description
            for the corresponding file entry to be selected.
            e.g. ``['power cutsky', 'fiber']`` selects the data base entries whose description contains 'power' and 'cutsky' or 'fiber'.

        empty_error : bool, default=False
            If ``True``, return (hopefully meaningful) error if no match is found.

        ignore : bool, list, default=False
            If ``True``, also keep file entries that do not take the input options.
            If list, do not cut file entries to these input options.

        **kwargs : dict
            Restrict to these options, see :meth:`BaseFileEntry.select`.

        Returns
        -------
        new : FileEntryCollection
            Selected data base.
        """
        data = self.index(id=id, filetype=filetype, keywords=keywords, return_entry=True, empty_error=empty_error, **kwargs)[1]
        return self.clone(data=data)

    def get(self, *args, check_exists=False, raise_error=True, **kwargs):
        """
        Return the :class:`BaseFile` instance that matches input arguments, see :meth:`select`.

        Parameters
        ----------
        *args, **kwargs : dict
            If :meth:`select` returns several file entries, and / or file entries with multiples files,
            a :class:`ValueError` is raised.

        check_exists : bool, default=False
            If ``True``, check whether file exists; if not, raise a :class:`FileNotFoundError`.

        raise_error : bool, default=True
            If ``False`` and an error is raised, catch it and return ``None``.

        Returns
        -------
        file : BaseFile
        """
        new = self.select(*args, empty_error=raise_error, **kwargs)
        if len(new.data) == 1:
            return new.data[0].get(check_exists=check_exists, raise_error=raise_error)
        try:
            if len(new.data) > 1:
                raise ValueError('"get" is not applicable as there are {} entries:\n{}'.format(len(new.data), '\n'.join([repr(entry) for entry in new.data])))
        except ValueError:
            if raise_error: raise
            return None

    def __getitem__(self, index):
        """Return file entry(ies) at the input index(ices) in the list."""
        if utils.is_sequence(index):
            data = [self.data.__getitem__(ii) for ii in index]
        else:
            data = self.data.__getitem__(index)
        if isinstance(data, list):
            return self.clone(data=data)
        return data

    def __delitem__(self, index):
        """Delete file entry(ies) at the input index(ices) in the list."""
        if not utils.is_sequence(index):
            index = [index]
        for ii in index:
            self.data.__delitem__(ii)

    def __iter__(self):
        """Iterate over file entries."""
        return iter(self.data)

    def __len__(self):
        """Length, i.e. number of file entries."""
        return len(self.data)

    def _get_file_entry(self, entry):
        """``entry`` may be e.g. a dictionary, or a :class:`BaseFileEntry` instance, in a :class:`BaseFileEntry` instance."""
        entry = get_file_entry(entry, file_entry_collection=self)
        entry.environ = self.environ
        return entry

    def append(self, entry):
        """Append an input file entry, which may be e.g. a dictionary, or a :class:`BaseFileEntry` instance."""
        entry = self._get_file_entry(entry)
        if entry not in self.data:
            self.data.append(entry)

    def save(self, fn, replace_environ=False):
        """
        Save data base to *yaml* file ``fn``.

        Parameters
        ----------
        fn : str, Path
            Where to save file data base.

        replace_environ : bool, default=False
            If ``True``, replace environment variables in entry's path by their values.
        """
        utils.mkdir(os.path.dirname(fn))

        with open(fn, 'w') as file:

            def list_rep(dumper, data):
                return dumper.represent_sequence(u'tag:yaml.org,2002:seq', data, flow_style=True)

            yaml.add_representer(list, list_rep)

            dis = []
            for entry in self.data:
                di = entry.to_dict()
                if entry.name != 'base':
                    di['fileentry'] = entry.name
                di['foptions'] = {name: values for name, values in di['foptions'].items() if not _deep_eq(values, di['options'][name])}
                if not di['foptions']:
                    di.pop('foptions')
                if replace_environ:
                    di['path'] = path_replace_environ(di['path'], getattr(self, 'environ', {}))
                dis.append(utils.dict_to_yaml(di))
            yaml.dump_all(dis, file, default_flow_style=False)

    def __copy__(self):
        """Return a shallow copy (data list is copied)."""
        new = super(FileEntryCollection, self).__copy__()
        new.data = self.data.copy()
        return new

    def update(self, **kwargs):
        """Update :attr:`data` (list of :class:`BaseFileEntry`) or :attr:`environ` (dict)."""
        if 'data' in kwargs:
            self.data = []
            for entry in kwargs.pop('data'): self.append(entry)
        if 'environ' in kwargs:
            environ = kwargs.pop('environ')
            for entry in self.data:
                entry.environ = environ
        if 'ro' in kwargs:
            ro = kwargs.pop('ro')
            for entry in self.data:
                entry.update(ro=ro)
        if kwargs:
            raise ValueError('unrecognized arguments {}'.format(kwargs))

    def clone(self, **kwargs):
        """Return an updated copy."""
        new = self.copy()
        new.update(**kwargs)
        return new

    def __add__(self, other):
        """Sum of `self`` + ``other``."""
        new = self.copy()
        for entry in other.data:
            new.append(entry)
        return new

    def __radd__(self, other):
        if other == 0: return self.copy()
        return self.__add__(other)

    @property
    def filepaths(self):
        """All file paths in file collection."""
        toret = []
        for entry in self.data:
            toret += entry.filepaths
        return toret

    def __repr__(self):
        return '{}(\n{}\n)'.format(self.__class__.__name__, ',\n'.join([repr(entry) for entry in self.data]))

    def symlink(self, raise_error=True):
        """Create symlink for all files in the collection."""
        for entry in self.data:
            entry.symlink(raise_error=raise_error)

    def exists(self, return_type='dict'):
        """
        Return summary description of which files exist or not.

        Parameters
        ----------
        return_type : str, default='dict'
            If 'dict', return a dictionary mapping exists to the file paths.
            If 'str', return a string.

        Returns
        -------
        summary : dict, str
        """
        counts = {True: [], False: []}
        for entry in self.data:
            tmp = entry.exists(return_type='dict')
            for exists in tmp:
                counts[exists] += tmp[exists]
        if return_type == 'dict':
            return counts
        if return_type == 'str':
            toret = []
            for exists, filepaths in counts.items():
                toret.append('exists = {}'.format(exists))
                toret.append('=' * len(toret[-1]))
                toret += filepaths
            return '\n'.join(toret)
        raise ValueError('unknown return_type {}'.format(return_type))

    def chgdir(self, newdir, olddir=None):
        """
        Change directory for new one in :attr:`path`.
        As a default, ``olddir`` (directory to be replaced) is taken to be
        the common directory to all entries that does not contain replacement options.
        """
        if olddir is None:
            path = os.path.commonpath([entry.path for entry in self.data])
            olddir = _find_basedir(path)
        for entry in self.data:
            entry.chgdir(newdir, olddir=olddir)


def common_options(list_options, intersection=True):
    """Return the common options."""
    coptions = dict(list_options[0])

    def _intersect(options1, options2):
        options = {}
        if intersection:
            for name, values1 in options1.items():
                if name in options2:
                    options[name] = in_options(values1, options2[name])
        else:
            options = dict(options1)
            for name, values2 in options2.items():
                if name in options1:
                    values1 = options1[name]
                    options[name] = values1 + [value for value in values2 if not in_options(value, values1)]
                else:
                    options[name] = values2
        return options

    for options2 in list_options:
        coptions = _intersect(coptions, options2)
    return coptions


class FileManager(FileEntryCollection):

    """BaseFile manager, main class to be used to get paths to / load / save files."""

    def __init__(self, database=(), environ=None):
        environ = get_environ(environ).to_dict(all=True)
        dbc = FileEntryCollection(environ=environ)
        for db in _make_list(database):
            dbc += FileEntryCollection(db)
        self.__dict__.update(dbc.__dict__)

    def update(self, **kwargs):
        """Update :attr:`data` (list of :class:`BaseFileEntry`) or :attr:`environ` (dict)."""
        if 'environ' in kwargs:
            kwargs['environ'] = get_environ(kwargs['environ']).to_dict(all=True)
        super(FileManager, self).update(**kwargs)

    @property
    def options(self):
        """Return intersection of all options, i.e. options that are common to all file entries."""
        if not self.data:
            return {}

        return common_options([entry.options for entry in self.data], intersection=True)

    def iter_options(self, include=None, exclude=None, intersection=False):
        """
        Iterate over options that are common to all file entries (:attr:`options`).

        Parameters
        ----------
        include : str, list, default=None
            List of options to include in the iteration.
            ``None`` to include all options.

        exclude : str, list, default=None
            List of options to exclude in the iteration.
            ``None`` to not exclude any option.

        intersection : bool, default=True
            If ``False``, iterate over all file entry options,
            i.e. not only those which are common to all file entries.

        Returns
        -------
        options : list of the options.
        """
        if not intersection:
            common = []
            for entry in self.data:
                for options2 in entry.iter_options(include=include, exclude=exclude):
                    added = False
                    for i1, options1 in enumerate(common):
                        if set(options1).issubset(set(options2)):  # options1 in options
                            if all(options1[name] == options2[name] for name in options1):
                                added = True
                                common[i1] = options2
                        elif set(options2).issubset(set(options1)):
                            if all(options2[name] == options1[name] for name in options2):
                                added = True
                    if not added:
                        common.append(options2)
            return common
        return list(iter_options(common_options([entry.options for entry in self.data], intersection=True), include=include, exclude=exclude))

    def iter(self, include=None, exclude=None, get=None, intersection=False):
        """
        Iterate over options that are common to all file entries (:attr:`options`),
        and return the list of the (selected) :class:`FileManager` instances.

        Parameters
        ----------
        include : str, list, default=None
            List of options to include in the iteration.
            ``None`` to include all options.

        exclude : str, list, default=None
            List of options to exclude in the iteration.
            ``None`` to not exclude any option.

        get : bool, default=None
            If ``False``, return a list of :class:`FileManager` instances.
            If ``True``, call :meth:`FileManager.get` to return a list of :class:`BaseFile` instances.
            If ``None``, exclude is ``None``, and there are single options in :class:`FileManager` instances,
            call :meth:`FileManager.get` to return a list of :class:`BaseFile` instances.

        intersection : bool, default=True
            If ``False``, iterate over all file entry options,
            i.e. not only those which are common to all file entries.

        Returns
        -------
        fms : list of the :class:`FileManager` instances.
        """
        if not self.data:
            return []
        fms = []
        if intersection:
            for options in self.iter_options(include=include, exclude=exclude, intersection=True):
                fm = self.clone(data=[])
                for entry in self.data:
                    entry = entry.select(ignore=False, **{**entry.options, **options})
                    if entry:
                        fm.append(entry)
                if fm:
                    fms.append(fm)
        else:
            fm = self.clone(data=[])
            for entry in self.data:
                for options in iter_options(entry.options, include=include, exclude=exclude):
                    sentry = entry.select(ignore=True, **options)
                    if sentry:
                        fm = self.clone(data=[sentry])
                        fms.append(fm)

        if get is False:
            return fms
        if get is None and exclude is None:
            for fm in fms:
                if len(fm.data) == 1:
                    options = fm.data[0].options
                    if Ellipsis in options.values() or any(len(values) != 1 for values in options.values()):
                        get = False
                else:
                    get = False
                if get is False: break
            if get is None: get = True
        if get:
            return [fm.get() for fm in fms]
        return fms

    def __getitem__(self, index):
        """Return files at the input index(ices) in the list."""
        li = self.iter()
        if utils.is_sequence(index):
            data = [li[ii] for ii in index]
        else:
            data = li[index]
        return data

    def __iter__(self):
        """
        Iterate over options that are common to all file entries (:attr:`options`),
        and yield the (selected) :class:`FileManager` instances.
        """
        for fm in self.iter(include=None, exclude=None, get=None, intersection=True):
            yield fm

    @classmethod
    def databases(cls, keywords=None):
        """List of paths to available data bases following desipipe's configuration (see :class:`Config`)."""
        if keywords is not None:
            keywords = _make_list(keywords)
            keywords = [keyword.lower().split() for keyword in keywords]
        from .config import Config
        file = Config().get('file', {})
        filenames = _make_list(file.get('filename', []))
        toret = []
        for filename in filenames:
            for fn in glob.glob(filename):
                if any(all(kw in filename for kw in keyword) for keyword in keywords):
                    toret.append(fn)
        return toret
