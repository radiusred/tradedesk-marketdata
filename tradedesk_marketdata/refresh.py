"""Re-check a window of days in an existing store and top it up.

:func:`refresh` is an orchestrator over the export framework
(:func:`tradedesk_marketdata.export.export_range`); it writes no day file or
manifest line itself. Per symbol:

1. **Scan** the window ``[since, until]``: the days with no day files
   (*missing*), and the weekdays whose day files are *empty* (no candle on
   either side) or *partial* (the day's latest ``_partial_days.jsonl`` record
   is not older than its latest commit record in ``_sources.jsonl``). An empty
   or partial weekday on a closure day (``CLOSURES``, plus the caller's) is
   skipped and reported.
2. **Primary step**: one export of the window from the primary source with the
   empty and partial weekdays as its ``recheck`` set. The missing days are
   added by the framework's first-committed-wins path, so one month file
   serves every missing and rechecked day in it and a complete day is never
   planned. A rechecked day is replaced only when its fresh data has ticks and
   differs from the stored files. The newest day with candles in the window
   after this step is the *newest day*.
3. **Fallback step** (only with a fallback source): each weekday up to the
   newest day, not a closure, that is still missing or empty (and, with
   ``fallback_only``, named there) is exported from the fallback source, one
   day per run. The scale sentry refuses a day at the wrong scale.

Before the first change to a symbol, its ``_sources.jsonl`` and
``_partial_days.jsonl`` are copied to ``{backup_dir}/{run stamp}/{SYMBOL}/``,
and before each day is written its current day files are copied there, in the
store's layout (:class:`Backup`). A dry run makes no request and writes
nothing; its report lists the days the run would add, recheck and send to the
fallback.

The report (:meth:`RefreshReport.as_json`) is documented in the README.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .bad_ticks import BadTickRule
from .export import (
    _daily_candle_path,
    _day_is_committed,
    _partial_day_manifest_path,
    _read_day_csv_bytes,
    _source_manifest_path,
    _symbol_normalise,
    export_range,
    read_floor,
)
from .floor import _copy_verified
from .source import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, DEFAULT_RETRIES, Source

log = logging.getLogger(__name__)

#: Market-closure days (month, day): an empty weekday on one is expected, not a gap.
CLOSURES: tuple[tuple[int, int], ...] = ((12, 25), (1, 1))

REPORT_SCHEMA_VERSION = 1


class RefreshError(ValueError):
    """A refresh refused before any change: a bad argument or backup directory."""


# ---------------------------------------------------------------------------
# The store, read only
# ---------------------------------------------------------------------------


def store_symbols(cache_dir: Path) -> list[str]:
    """The symbols present in a store: directories named as cache symbols that hold day files
    or a ``_sources.jsonl``."""
    found = []
    for d in sorted(cache_dir.iterdir()) if cache_dir.is_dir() else []:
        if not d.is_dir() or d.name.startswith("_") or _symbol_normalise(d.name) != d.name:
            continue
        has_year = any(c.is_dir() and c.name.isdigit() and len(c.name) == 4 for c in d.iterdir())
        if has_year or _source_manifest_path(cache_dir, d.name).exists():
            found.append(d.name)
    return found


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _latest_by_day(records: list[dict[str, Any]], stamp: str) -> dict[date, datetime]:
    latest: dict[date, datetime] = {}
    for rec in records:
        try:
            day = date.fromisoformat(rec["day"])
            at = datetime.fromisoformat(rec[stamp])
        except (KeyError, TypeError, ValueError):
            continue
        if day not in latest or at > latest[day]:
            latest[day] = at
    return latest


def partial_days(cache_dir: Path, symbol: str) -> set[date]:
    """The days whose latest ``_partial_days.jsonl`` record is not older than their latest
    commit record in ``_sources.jsonl`` (a later full commit ends a day's partial state)."""
    partial = _latest_by_day(
        _records(_partial_day_manifest_path(cache_dir, symbol)), "committed_at"
    )
    commits = _latest_by_day(
        [r for r in _records(_source_manifest_path(cache_dir, symbol)) if "status" not in r],
        "committed_at",
    )
    return {d for d, at in partial.items() if d not in commits or at >= commits[d]}


def day_state(cache_dir: Path, symbol: str, day: date, partial: Collection[date] = ()) -> str:
    """``missing``, ``empty``, ``partial``, ``complete`` or ``unreadable``."""
    if not _day_is_committed(cache_dir, symbol, day):
        return "missing"
    sides = [
        _read_day_csv_bytes(_daily_candle_path(cache_dir, symbol, day, side))
        for side in ("bid", "ask")
    ]
    if any(b is None for b in sides):
        return "unreadable"
    if all(len(b.strip().splitlines()) <= 1 for b in sides if b is not None):
        return "empty"
    return "partial" if day in partial else "complete"


def sha256_file(path: Path) -> str | None:
    """A file's sha256 in hex, or None when it does not exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


class Backup:
    """Copies what a run is about to change into ``{backup_dir}/{stamp}/{SYMBOL}/``."""

    def __init__(self, cache_dir: Path, backup_dir: Path, stamp: str) -> None:
        cache_root, backup_root = cache_dir.resolve(), backup_dir.resolve()
        if backup_root == cache_root or cache_root in backup_root.parents:
            raise RefreshError(f"--backup-dir {backup_dir} is inside the store {cache_dir}")
        self.cache_dir = cache_dir
        self.run_dir = backup_dir / stamp
        self.symbols: set[str] = set()
        self.files = 0

    def _copy(self, symbol: str, src: Path) -> None:
        if src.exists():
            _copy_verified(src, self.run_dir / symbol / src.relative_to(self.cache_dir / symbol))
            self.files += 1

    def before(self, symbol: str, day: date) -> None:
        """Back up the symbol's manifests (first call only) and the day's current files."""
        if symbol not in self.symbols:
            self._copy(symbol, _source_manifest_path(self.cache_dir, symbol))
            self._copy(symbol, _partial_day_manifest_path(self.cache_dir, symbol))
            self.symbols.add(symbol)
        for side in ("bid", "ask"):
            self._copy(symbol, _daily_candle_path(self.cache_dir, symbol, day, side))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _iso(days: Collection[date]) -> list[str]:
    return [d.isoformat() for d in sorted(days)]


@dataclass
class SymbolRefresh:
    """One symbol's part of the report."""

    symbol: str
    added: list[dict[str, str]] = field(default_factory=list)
    reexported: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[date] = field(default_factory=list)
    still_empty: list[date] = field(default_factory=list)
    skipped_closures: list[date] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    newest_day: date | None = None
    sources_sha256_before: str | None = None
    sources_sha256_after: str | None = None
    would_add: list[date] = field(default_factory=list)
    would_recheck: list[date] = field(default_factory=list)
    would_fallback: list[date] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.sources_sha256_before != self.sources_sha256_after

    def as_json(self, dry_run: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "changed": self.changed,
            "added": self.added,
            "reexported": self.reexported,
            "unchanged": _iso(self.unchanged),
            "still_empty": _iso(self.still_empty),
            "skipped_closures": _iso(self.skipped_closures),
            "refused": self.refused,
            "excluded": self.excluded,
            "newest_day": self.newest_day.isoformat() if self.newest_day else None,
            "sources_sha256_before": self.sources_sha256_before,
            "sources_sha256_after": self.sources_sha256_after,
            "error": self.error,
        }
        if dry_run:
            out["would_add"] = _iso(self.would_add)
            out["would_recheck"] = _iso(self.would_recheck)
            out["would_fallback"] = _iso(self.would_fallback)
        return out


@dataclass
class RefreshReport:
    since: date
    until: date
    dry_run: bool
    primary_source: str
    fallback_source: str | None
    backup_dir: Path | None
    symbols: list[SymbolRefresh] = field(default_factory=list)
    skipped_symbols: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return any(s.changed for s in self.symbols)

    @property
    def failed(self) -> bool:
        return any(s.refused or s.error for s in self.symbols)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "tool_version": __version__,
            "generated_at": datetime.now(UTC).isoformat(),
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "dry_run": self.dry_run,
            "primary_source": self.primary_source,
            "fallback_source": self.fallback_source,
            "backup_dir": str(self.backup_dir.absolute()) if self.backup_dir else None,
            "changed": self.changed,
            "symbols": [s.as_json(self.dry_run) for s in self.symbols],
            "skipped_symbols": self.skipped_symbols,
        }

    def summary(self) -> str:
        """The run's one-line exit summary."""
        if self.dry_run:
            return (
                f"dry run, nothing written: would add {sum(len(s.would_add) for s in self.symbols)}"
                f", recheck {sum(len(s.would_recheck) for s in self.symbols)} and send "
                f"{sum(len(s.would_fallback) for s in self.symbols)} day(s) to the fallback"
            )
        parts = [
            f"{sum(len(s.added) for s in self.symbols)} day(s) added",
            f"{sum(len(s.reexported) for s in self.symbols)} re-exported",
            f"{sum(len(s.unchanged) for s in self.symbols)} unchanged",
            f"{sum(len(s.still_empty) for s in self.symbols)} still empty",
            f"{sum(len(s.refused) for s in self.symbols)} refused",
        ]
        errors = sum(1 for s in self.symbols if s.error)
        if errors:
            parts.append(f"{errors} symbol(s) failed")
        head = "changed" if self.changed else "no change"
        return f"{head}: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshOptions:
    """What every export of a refresh shares."""

    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
    retries: int = DEFAULT_RETRIES
    commit_partial_after_days: int = 7
    bad_tick_rule: BadTickRule | None = BadTickRule()


def _is_closure(day: date, closures: Collection[tuple[int, int]]) -> bool:
    return (day.month, day.day) in closures


def refresh(
    *,
    cache_dir: Path,
    symbols: Sequence[str],
    since: date,
    until: date,
    primary: Source,
    fallback: Callable[[str], Source] | None = None,
    fallback_name: str | None = None,
    backup_dir: Path | None = None,
    dry_run: bool = False,
    closures: Collection[tuple[int, int]] = CLOSURES,
    fallback_only: Collection[date] | None = None,
    options: RefreshOptions = RefreshOptions(),  # noqa: B008 (frozen)
    stamp: str | None = None,
) -> RefreshReport:
    """Re-check ``[since, until]`` for each symbol (see the module docstring).

    ``fallback`` builds the fallback source for a symbol (its price divisor is
    the caller's); without it no fallback request is made. ``backup_dir`` is
    required unless ``dry_run``: :class:`RefreshError` is raised before
    anything is read from a source. ``stamp`` names the run's backup
    subdirectory (default: the UTC time, ``YYYYMMDDTHHMMSSZ``).
    """
    if until < since:
        raise RefreshError(f"--since {since.isoformat()} is after {until.isoformat()}")
    backup: Backup | None = None
    if not dry_run:
        if backup_dir is None:
            raise RefreshError("a refresh that may change the store needs --backup-dir")
        stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = Backup(cache_dir, backup_dir, stamp)
    report = RefreshReport(
        since=since,
        until=until,
        dry_run=dry_run,
        primary_source=primary.name,
        fallback_source=fallback_name if fallback is not None else None,
        backup_dir=backup.run_dir if backup is not None else None,
    )
    for symbol in symbols:
        sr = SymbolRefresh(symbol=symbol)
        report.symbols.append(sr)
        sr.sources_sha256_before = sha256_file(_source_manifest_path(cache_dir, symbol))
        try:
            _refresh_symbol(
                sr,
                cache_dir=cache_dir,
                since=since,
                until=until,
                primary=primary,
                fallback=fallback,
                backup=backup,
                closures=closures,
                fallback_only=fallback_only,
                options=options,
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            sr.error = f"{type(e).__name__}: {e}"
            log.error(f"{symbol}: refresh failed: {sr.error}")
        sr.sources_sha256_after = sha256_file(_source_manifest_path(cache_dir, symbol))
    return report


def _refresh_symbol(
    sr: SymbolRefresh,
    *,
    cache_dir: Path,
    since: date,
    until: date,
    primary: Source,
    fallback: Callable[[str], Source] | None,
    backup: Backup | None,
    closures: Collection[tuple[int, int]],
    fallback_only: Collection[date] | None,
    options: RefreshOptions,
) -> None:
    symbol = sr.symbol
    floor = read_floor(cache_dir, symbol)
    window = [d for d in _days(since, until) if floor is None or d >= floor]
    if not window:
        log.info(f"{symbol}: the window lies before the floor; nothing to do")
        return

    def scan() -> dict[date, str]:
        partial = partial_days(cache_dir, symbol)
        return {d: day_state(cache_dir, symbol, d, partial) for d in window}

    def fallback_candidate(d: date, state: str) -> bool:
        return (
            state in ("missing", "empty")
            and d.weekday() < 5
            and not _is_closure(d, closures)
            and (fallback_only is None or d in fallback_only)
        )

    states = scan()
    missing = [d for d in window if states[d] == "missing"]
    recheck: list[date] = []
    for d in window:
        if states[d] in ("empty", "partial") and d.weekday() < 5:
            (sr.skipped_closures if _is_closure(d, closures) else recheck).append(d)

    if backup is None:  # a dry run: report the plan, request nothing
        sr.would_add, sr.would_recheck = missing, recheck
        if fallback is not None:
            sr.would_fallback = [d for d in window if fallback_candidate(d, states[d])]
        return

    def before_change(day: date) -> None:
        backup.before(symbol, day)

    outcomes: dict[date, str] = {}
    if missing or recheck:
        _export(
            primary,
            symbol,
            since,
            until,
            cache_dir,
            recheck,
            outcomes,
            before_change,
            options,
            refetch=bool(recheck),  # a staged month file would hide the source's correction
        )
    _collect(sr, primary.name, outcomes, missing)

    states = scan()
    with_data = [d for d in window if states[d] in ("complete", "partial")]
    sr.newest_day = max(with_data) if with_data else None

    if fallback is not None:
        if sr.newest_day is None:
            log.warning(f"{symbol}: no day with candles in the window; no fallback")
        else:
            todo = [d for d in window if d <= sr.newest_day and fallback_candidate(d, states[d])]
            if todo:
                source = fallback(symbol)
                outcomes = {}
                for d in todo:
                    recheck_d = [d] if states[d] == "empty" else []
                    _export(
                        source,
                        symbol,
                        d,
                        d,
                        cache_dir,
                        recheck_d,
                        outcomes,
                        before_change,
                        options,
                    )
                _collect(sr, source.name, outcomes, [d for d in todo if states[d] == "missing"])
            states = scan()

    newest = sr.newest_day
    sr.still_empty = [
        d
        for d in window
        if newest is not None
        and d <= newest
        and states[d] in ("missing", "empty")
        and d.weekday() < 5
        and not _is_closure(d, closures)
    ]


def _export(
    source: Source,
    symbol: str,
    first: date,
    last: date,
    cache_dir: Path,
    recheck: Collection[date],
    outcomes: dict[date, str],
    before_change: Callable[[date], None],
    options: RefreshOptions,
    *,
    refetch: bool = False,
) -> None:
    with tempfile.TemporaryDirectory(prefix="tradedesk-md-refresh-") as out:
        export_range(
            source=source,
            symbol=symbol,
            start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
            end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
            out=Path(out),
            resample_rule=None,
            cache_dir=cache_dir,
            commit_partial_after_days=options.commit_partial_after_days,
            timeout=options.timeout,
            retries=options.retries,
            bad_tick_rule=options.bad_tick_rule,
            recheck=recheck,
            outcomes=outcomes,
            before_change=before_change,
            refetch=refetch,
        )


def _collect(
    sr: SymbolRefresh, source: str, outcomes: dict[date, str], missing: Collection[date]
) -> None:
    """File one export's day outcomes into the symbol's report."""
    for day, what in sorted(outcomes.items()):
        if what in ("committed", "committed_empty") and day in missing:
            sr.added.append({"day": day.isoformat(), "source": source})
        elif what == "reexported":
            sr.reexported.append({"day": day.isoformat(), "source": source})
        elif what == "unchanged" and day not in sr.unchanged:
            sr.unchanged.append(day)
        elif what == "excluded":
            sr.excluded.append({"day": day.isoformat(), "source": source})
        elif what == "rejected_scale_sentry":
            sr.refused.append({"day": day.isoformat(), "source": source, "reason": "scale_sentry"})
        elif what == "write_failed":
            sr.refused.append({"day": day.isoformat(), "source": source, "reason": "write_failed"})
