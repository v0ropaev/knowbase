"""knowbase — a versioned, provenance-grounded knowledge layer over a codebase."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("knowbase")
except PackageNotFoundError:  # uninstalled source tree
    __version__ = "0.0.0+unknown"
