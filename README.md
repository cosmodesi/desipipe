# desipipe

**desipipe** is an attempt to provide a common framework for running DESI clustering analyses,
handling files, submitting jobs, etc. within Python alone.

Example notebooks presenting most use cases are provided in directory nb/.

## Documentation

Documentation in construction on Read the Docs, [desipipe docs](https://desipipe.readthedocs.io/).
See in particular [getting started](https://desipipe.readthedocs.io/en/latest/user/getting_started.html).

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
