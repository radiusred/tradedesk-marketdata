"""The registry of market-data sources, by name.

Each source is a module of this package holding everything specific to one
provider, behind the :class:`tradedesk_marketdata.source.Source` contract.
The framework never imports a source: the CLI looks one up here by its
``--source`` name and hands the configured object to the framework.

To add a source, implement the contract in a new module and register its
class below (or call :func:`register` from your own code).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..source import Source, SourceError
from .dukascopy import DukascopySource
from .histdata import HistDataSource

_REGISTRY: dict[str, type[Source]] = {}


class UnknownSourceError(SourceError):
    """No source is registered under the name."""


def register(cls: type[Source]) -> type[Source]:
    """Register a source class under its ``name``; its option flags must be its own."""
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"a source named {cls.name!r} is already registered")
    taken = {opt.flag: other.name for other in _REGISTRY.values() for opt in other.options}
    for opt in cls.options:
        if opt.flag in taken and taken[opt.flag] != cls.name:
            raise ValueError(f"{opt.flag} is already an option of source {taken[opt.flag]!r}")
    _REGISTRY[cls.name] = cls
    return cls


def names() -> list[str]:
    """The registered source names, sorted."""
    return sorted(_REGISTRY)


def get(name: str) -> type[Source]:
    """The source class registered under ``name``."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownSourceError(
            f"no source named {name!r}; available sources: {', '.join(names())}"
        ) from None


def create(name: str, options: Mapping[str, Any] | None = None) -> Source:
    """A source configured with its option values (``{dest: value}``)."""
    return get(name)(options)


register(DukascopySource)
register(HistDataSource)

__all__ = ["UnknownSourceError", "create", "get", "names", "register"]
