"""The contract between the export framework and a market-data source.

The framework (:mod:`tradedesk_marketdata.export`) owns everything every
source shares: the day-file cache and its layout, tick to 1-minute candles,
day assembly, the scale sentry, the atomic commit, the ``_sources.jsonl`` and
``_partial_days.jsonl`` manifests, the range CSVs, progress, cancellation and
the fetch pool. A *source* owns everything that differs between providers:
what one fetch unit is, how it is fetched (URLs, tokens, pauses, rate limits),
how it is decoded (file format, timestamp convention, price units), and when a
day it fed may be committed (its gap and known-defect rules).

A source is a :class:`Source` subclass registered by name in
:mod:`tradedesk_marketdata.sources`. For one symbol the framework opens a
:class:`SourceRun` and drives it:

1. ``plan(pending_days)``: the units to fetch, in order; each names the UTC
   days it holds ticks for.
2. ``fetch(unit)``: the raw unit, on a worker thread when ``fetch_workers``
   is above one. Whatever it returns is handed to ``decode`` unchanged.
3. ``decode(unit, raw)``: in plan order, the unit's ticks as a frame indexed
   by UTC time in time order, with columns ``bid``, ``ask``, ``bid_vol`` and
   ``ask_vol`` in the cache's raw price units; ``None`` when the unit has none.
4. ``decide(day, has_data)``: once every unit of a day has been decoded,
   whether the day may be committed now (:class:`Commit`, possibly as a
   partial day), must be left for a later run (:class:`Leave`), or should be
   asked again after the next unit (:class:`Wait`).
5. ``committed(day, empty)``: after the framework wrote a day, its provenance
   for ``_sources.jsonl``; the source removes or retains (``--keep-raw``) the
   raw units it no longer needs.
6. ``finish()``: once every unit is processed, the source's own summary and
   cleanup.

This module imports no source; sources import it and the framework.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

import pandas as pd

log = logging.getLogger(__name__)

# Per-request HTTP timeouts (connect, read) in seconds and attempts per fetch
# unit, overridable from the CLI (--connect-timeout, --read-timeout, --retries).
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_RETRIES = 3
# Backoff between attempts at one fetch unit, in seconds.
RETRY_BASE_DELAY = 0.8
RETRY_MAX_DELAY = 6.0
RETRY_BACKOFF_FACTOR = 2.5

TICK_COLUMNS = ("bid", "ask", "bid_vol", "ask_vol")


class SourceError(ValueError):
    """A request a source refuses before making any network request.

    For example a symbol the source has no mapping for, or a provider option
    that does not apply. The CLI reports it as a usage error.
    """


# ---------------------------------------------------------------------------
# Ticks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tick:
    """One quote: a UTC time, bid and ask in cache raw units, and their volumes."""

    ts: datetime
    bid: float
    ask: float
    bid_vol: float
    ask_vol: float


def ticks_frame(ticks: Sequence[Tick]) -> pd.DataFrame:
    """The tick frame ``decode`` returns, built from a sequence of :class:`Tick` (order kept)."""
    index = pd.DatetimeIndex([t.ts for t in ticks], tz="UTC")
    return pd.DataFrame(
        {
            "bid": [t.bid for t in ticks],
            "ask": [t.ask for t in ticks],
            "bid_vol": [t.bid_vol for t in ticks],
            "ask_vol": [t.ask_vol for t in ticks],
        },
        index=index,
        dtype=float,
    )


# ---------------------------------------------------------------------------
# Day decisions and provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Partial:
    """A day committed with known-permanent gaps, recorded in ``_partial_days.jsonl``."""

    missing_hours: list[int]
    gap_reason: str
    note: str  # the parenthetical of the "partial-committed" log line


@dataclass(frozen=True)
class Commit:
    """Commit the day now (``partial`` set when it has known-permanent gaps)."""

    partial: Partial | None = None


@dataclass(frozen=True)
class Leave:
    """Do not commit the day in this run; a later run will try again."""

    reason: str


@dataclass(frozen=True)
class Wait:
    """Ask again after the next unit; ``reason`` is used if the run ends first."""

    reason: str


Decision = Commit | Leave | Wait


@dataclass(frozen=True)
class Provenance:
    """A committed day's ``_sources.jsonl`` fields that only the source knows.

    ``scale_factor`` is the multiplier from the source's native price to the
    cache's raw units (cache price = native price x scale_factor);
    ``source_unit`` names the raw unit(s) the day was built from.
    """

    scale_factor: float
    source_unit: str


@dataclass(frozen=True)
class SourceDescription:
    """What an export's ``.meta.json`` sidecar says about the source.

    ``price_divisor`` keeps the sidecar's convention, cache price = source
    price / price_divisor; ``params`` are merged into the sidecar's params.
    """

    price_divisor: float
    params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Options and run context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceOption:
    """A command-line flag that only one source understands.

    The CLI adds it to the parser with a default of ``None`` (``False`` for a
    ``flag_only`` switch), passes its value to that source, and refuses it
    with any other source. The source applies its own default for ``None``.
    """

    flag: str
    help: str
    type: Callable[[str], Any] | None = None
    flag_only: bool = False
    metavar: str | None = None

    @property
    def dest(self) -> str:
        return self.flag.lstrip("-").replace("-", "_")

    def given(self, value: Any) -> bool:
        return value not in (None, False)


@dataclass(frozen=True)
class RunContext:
    """One symbol's export, as the framework hands it to ``Source.open``."""

    source_name: str
    symbol: str  # the cache symbol, already normalised
    start_utc: datetime
    end_utc_inclusive: datetime
    cache_dir: Path | None
    commit_partial_after_days: int = 7
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
    retries: int = DEFAULT_RETRIES

    def retire_raw(self, path: Path, relpath: str) -> None:
        """Dispose of a staged raw unit whose days are committed: it is deleted.

        ``relpath`` names the unit below the symbol directory. A unit that is
        not there is ignored.
        """
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning(f"{self.symbol}: could not delete {path}")


@dataclass(frozen=True)
class ActionRequest:
    """The run a :meth:`Source.action` replaces."""

    symbols: list[str]
    start_utc: datetime
    end_utc_inclusive: datetime
    cache_dir: Path | None
    timeout: tuple[float, float]
    retries: int


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class Unit(Protocol):
    """One fetch unit (an hour file, a month file, ...)."""

    @property
    def days(self) -> tuple[date, ...]:
        """The pending UTC days this unit holds ticks for, or is needed by."""
        ...


class SourceRun(ABC):
    """One symbol's export from one source, driven by the framework."""

    #: Units fetched concurrently. One means strictly sequential, in plan order.
    fetch_workers: int = 1

    @abstractmethod
    def plan(self, pending: Sequence[date]) -> Sequence[Unit]:
        """The units to fetch for the pending (uncommitted) days, in processing order."""

    @abstractmethod
    def fetch(self, unit: Any) -> Any:
        """Fetch one unit. Must not raise for a unit that is merely unavailable."""

    @abstractmethod
    def decode(self, unit: Any, raw: Any) -> pd.DataFrame | None:
        """A fetched unit's ticks (see the module docstring), or ``None``."""

    @abstractmethod
    def decide(self, day: date, *, has_data: bool) -> Decision:
        """Whether a day whose units are all decoded may be committed now."""

    @abstractmethod
    def committed(self, day: date, *, empty: bool) -> Provenance:
        """A day was written: its provenance; dispose of raw units it no longer needs."""

    def finish(self) -> None:  # noqa: B027 - an optional hook
        """Every unit is processed: log the source's summary, clean up."""


class Source(ABC):
    """A market-data provider, configured from its own command-line options."""

    #: The ``--source`` value and the ``source`` of every provenance record.
    name: ClassVar[str]
    #: One line for ``--help``: what the source is and what it covers.
    summary: ClassVar[str]
    #: What one fetch unit is, for ``--retries`` help: "hour", "month file".
    unit: ClassVar[str]
    #: What ``--commit-partial-after-days`` means for this source.
    partial_commit_rule: ClassVar[str]
    #: The flags only this source understands.
    options: ClassVar[tuple[SourceOption, ...]] = ()

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.option_values: dict[str, Any] = dict(options or {})

    def option(self, dest: str, default: Any = None) -> Any:
        """The value given for one of this source's options, or ``default``."""
        value = self.option_values.get(dest)
        return default if value is None else value

    def check_symbol(self, symbol: str) -> None:  # noqa: B027 - accept by default
        """Raise :class:`SourceError` for a symbol this source cannot serve."""

    @abstractmethod
    def describe(self, symbol: str) -> SourceDescription:
        """The sidecar description of an export of ``symbol``."""

    @abstractmethod
    def open(self, ctx: RunContext) -> SourceRun:
        """Start one symbol's export. Called before the all-cached early exit."""

    def action(self, request: ActionRequest) -> int | None:
        """Run a source command that replaces the export (e.g. a probe).

        Return its exit status, or ``None`` when the options ask for a
        normal export.
        """
        return None
