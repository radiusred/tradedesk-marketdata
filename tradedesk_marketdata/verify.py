"""
Compare a cache with a reference cache of the same symbols, day by day.

Built from one source and checked against another, a cache shows its source's
defects as differences from the reference:

- a timestamp convention that is off by an hour shows as days that match the
  reference only *shifted* by a whole number of minutes;
- another instrument's data under a symbol shows as a *close-ratio regime*: a
  run of days whose reference/cache close ratio sits away from 1.0;
- a wrong price scale shows as a regime at a power of ten.

This module reads both caches with the framework's own day-file reader and
never writes to either. It names no source: the two caches may come from any.

Per day (``compare_day``): which sides have data, the row counts, the best
whole-minute alignment among ``SHIFTS`` with the fraction of minutes whose
OHLC is identical there, and the relative difference of the daily bars. A
day's status is one of ``same``, ``shifted``, ``different``, ``only-left``
(reference only), ``only-right`` (cache only) or ``empty``.

Per symbol (``summarise``): counts by status, worst and median daily
difference per year, the close-ratio regimes as runs, and the findings: one
actionable line per run of shifted days, per run of different days and per
regime other than 1.0.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .cancel import cancellation, check_cancelled
from .export import _daily_candle_path, _load_daily_candles

log = logging.getLogger(__name__)

#: Whole-minute shifts tried when aligning a cache day with the reference day.
SHIFTS: tuple[int, ...] = tuple(range(-180, 181, 30))
#: Fraction of minutes that must be identical for a day to count as aligned.
ALIGNED = 0.9
#: A day joins a close-ratio run when its ratio is within this relative distance
#: of the run's first ratio; a run is a regime "other than 1.0" beyond it.
REGIME_TOLERANCE = 0.05
#: Default ``--tolerance``: the largest relative daily OHLC difference that
#: still counts as the same day.
DEFAULT_TOLERANCE = 0.002

PRICE_COLUMNS = ("open", "high", "low", "close")
STATUSES = ("same", "shifted", "different", "only-left", "only-right", "empty")


@dataclass
class DayResult:
    """One symbol-day compared. ``left`` is the reference, ``right`` the cache."""

    symbol: str
    day: date
    status: str
    left_rows: int
    right_rows: int
    shift_minutes: int | None = None  # best alignment, cache minus reference
    identical: float | None = None  # fraction of minutes identical at that shift
    identical_at_zero: float | None = None
    daily_diff: float | None = None  # largest relative daily OHLC difference
    left_close: float | None = None
    right_close: float | None = None

    def as_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["day"] = self.day.isoformat()
        return out


@dataclass
class Regime:
    """A run of consecutive compared days with one reference/cache close ratio."""

    first: date
    last: date
    ratio: float
    days: int

    @property
    def is_unity(self) -> bool:
        return abs(self.ratio - 1.0) <= REGIME_TOLERANCE

    def as_json(self) -> dict[str, Any]:
        return {
            "first": self.first.isoformat(),
            "last": self.last.isoformat(),
            "ratio": self.ratio,
            "days": self.days,
        }


@dataclass
class SymbolReport:
    symbol: str
    days: list[DayResult]
    years: dict[int, dict[str, Any]] = field(default_factory=dict)
    regimes: list[Regime] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.findings)

    def as_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "failed": self.failed,
            "findings": self.findings,
            "years": {str(y): v for y, v in self.years.items()},
            "regimes": [r.as_json() for r in self.regimes],
            "days": [d.as_json() for d in self.days],
        }


# ---------------------------------------------------------------------------
# One day
# ---------------------------------------------------------------------------


def _minute_ns(df: pd.DataFrame) -> np.ndarray:
    idx = pd.DatetimeIndex(df.index).tz_convert("UTC").tz_localize(None).as_unit("ns")
    return idx.to_numpy().astype("int64")


@dataclass(frozen=True)
class _Side:
    """A day file as arrays: minute times (ns) and the compared columns."""

    t: np.ndarray
    values: np.ndarray  # rows x columns

    @classmethod
    def of(cls, df: pd.DataFrame, columns: Sequence[str]) -> _Side:
        return cls(_minute_ns(df), df[list(columns)].to_numpy(dtype=float))


def _identical_fraction(
    left: pd.DataFrame | _Side,
    right: pd.DataFrame | _Side,
    columns: Sequence[str],
    shift: int,
    day: date,
) -> float | None:
    """Fraction of the reference's minutes identical in the cache shifted by ``shift`` minutes.

    Only reference minutes whose shifted time falls inside the cache's UTC day
    are candidates; ``None`` when there are none.
    """
    lside = left if isinstance(left, _Side) else _Side.of(left, columns)
    rside = right if isinstance(right, _Side) else _Side.of(right, columns)
    start = pd.Timestamp(day, tz="UTC").value
    end = start + 86_400 * 10**9
    target = lside.t + shift * 60 * 10**9
    candidates = (target >= start) & (target < end)
    n = int(candidates.sum())
    if n == 0:
        return None
    if len(rside.t) == 0:
        return 0.0
    pos = np.clip(np.searchsorted(rside.t, target), 0, len(rside.t) - 1)
    found = candidates & (rside.t[pos] == target)
    same = found & np.isclose(lside.values, rside.values[pos], rtol=1e-9, atol=0.0).all(axis=1)
    return float(same.sum()) / n


def _daily_bar(df: pd.DataFrame) -> tuple[float, float, float, float]:
    return (
        float(df["open"].iloc[0]),
        float(df["high"].max()),
        float(df["low"].min()),
        float(df["close"].iloc[-1]),
    )


def _rel(a: float, b: float) -> float:
    if a == 0.0:
        return 0.0 if b == 0.0 else math.inf
    return abs(b - a) / abs(a)


def _load(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = _load_daily_candles(path)
    if df is None or df.empty:
        return None
    return df.sort_index()


def compare_day(
    reference: Path,
    cache: Path,
    symbol: str,
    day: date,
    *,
    side: str = "bid",
    tolerance: float = DEFAULT_TOLERANCE,
    include_volume: bool = False,
) -> DayResult | None:
    """Compare one symbol-day; ``None`` when neither cache has a file for it."""
    check_cancelled()
    lpath = _daily_candle_path(reference, symbol, day, side)
    rpath = _daily_candle_path(cache, symbol, day, side)
    if not lpath.exists() and not rpath.exists():
        return None
    left, right = _load(lpath), _load(rpath)
    lrows = 0 if left is None else len(left)
    rrows = 0 if right is None else len(right)
    if left is None and right is None:
        return DayResult(symbol, day, "empty", 0, 0)
    if right is None:
        return DayResult(symbol, day, "only-left", lrows, 0)
    if left is None:
        return DayResult(symbol, day, "only-right", 0, rrows)

    columns = [*PRICE_COLUMNS, "volume"] if include_volume else list(PRICE_COLUMNS)
    lside, rside = _Side.of(left, columns), _Side.of(right, columns)
    fractions = {s: _identical_fraction(lside, rside, columns, s, day) for s in SHIFTS}
    at_zero = fractions.get(0) or 0.0
    best_shift, best = 0, at_zero
    for s in sorted(SHIFTS, key=lambda s: (abs(s), s)):
        f = fractions[s]
        if f is not None and f > best:
            best_shift, best = s, f
    lbar, rbar = _daily_bar(left), _daily_bar(right)
    daily_diff = max(_rel(a, b) for a, b in zip(lbar, rbar, strict=True))

    if best_shift != 0 and best >= ALIGNED and at_zero < ALIGNED:
        status = "shifted"
    elif daily_diff > tolerance:
        status = "different"
    else:
        status = "same"
    return DayResult(
        symbol,
        day,
        status,
        lrows,
        rrows,
        shift_minutes=best_shift if best > 0 else None,
        identical=best,
        identical_at_zero=at_zero,
        daily_diff=daily_diff,
        left_close=lbar[3],
        right_close=rbar[3],
    )


# ---------------------------------------------------------------------------
# One symbol
# ---------------------------------------------------------------------------


def _regimes(days: Sequence[DayResult]) -> list[Regime]:
    runs: list[list[tuple[date, float]]] = []
    for d in days:
        if not d.left_close or not d.right_close:
            continue
        ratio = d.left_close / d.right_close
        if runs and abs(ratio / runs[-1][0][1] - 1.0) <= REGIME_TOLERANCE:
            runs[-1].append((d.day, ratio))
        else:
            runs.append([(d.day, ratio)])
    return [
        Regime(run[0][0], run[-1][0], statistics.median(r for _, r in run), len(run))
        for run in runs
    ]


def _status_runs(days: Sequence[DayResult], status: str) -> list[list[DayResult]]:
    """Runs of days with ``status`` (and, for ``shifted``, the same shift), in order.

    A run continues across days that have no data on either side (weekends).
    """
    runs: list[list[DayResult]] = []
    current: list[DayResult] = []
    for d in days:
        if d.status in ("empty",):
            continue
        same_run = (
            d.status == status
            and current
            and (status != "shifted" or d.shift_minutes == current[-1].shift_minutes)
        )
        if same_run:
            current.append(d)
        else:
            if current:
                runs.append(current)
            current = [d] if d.status == status else []
    if current:
        runs.append(current)
    return runs


def _span(run: Sequence[DayResult]) -> str:
    a, b = run[0].day, run[-1].day
    return a.isoformat() if a == b else f"{a.isoformat()}..{b.isoformat()}"


def summarise(symbol: str, days: Sequence[DayResult], *, tolerance: float) -> SymbolReport:
    """Per-year statistics, ratio regimes and findings for one symbol's compared days."""
    report = SymbolReport(symbol, sorted(days, key=lambda d: d.day))
    for d in report.days:
        y = report.years.setdefault(
            d.day.year, {"counts": dict.fromkeys(STATUSES, 0), "_diffs": []}
        )
        y["counts"][d.status] += 1
        if d.daily_diff is not None:
            y["_diffs"].append((d.daily_diff, d.day))
    for y in report.years.values():
        diffs = y.pop("_diffs")
        if diffs:
            worst, worst_day = max(diffs)
            y["worst_daily_diff"] = worst
            y["worst_day"] = worst_day.isoformat()
            y["median_daily_diff"] = statistics.median(v for v, _ in diffs)
        else:
            y["worst_daily_diff"] = y["worst_day"] = y["median_daily_diff"] = None

    report.regimes = _regimes(report.days)
    off = [r for r in report.regimes if not r.is_unity]

    def explained(d: DayResult) -> bool:
        """A different day inside a level regime is reported once, as the regime."""
        return any(r.first <= d.day <= r.last for r in off)

    for run in _status_runs(report.days, "shifted"):
        shift = run[0].shift_minutes or 0
        report.findings.append(
            f"{symbol} {_span(run)}: {len(run)} day(s) match the reference only shifted by "
            f"{shift:+d} min (the cache's timestamps are {abs(shift)} min "
            f"{'late' if shift > 0 else 'early'})"
        )
    unexplained = [d for d in report.days if not (d.status == "different" and explained(d))]
    for run in _status_runs(unexplained, "different"):
        worst = max(d.daily_diff or 0.0 for d in run)
        report.findings.append(
            f"{symbol} {_span(run)}: {len(run)} day(s) differ from the reference by more "
            f"than {tolerance:g} at the daily level (worst {worst:.4g})"
        )
    for r in off:
        span = r.first.isoformat() if r.first == r.last else f"{r.first}..{r.last}"
        report.findings.append(
            f"{symbol} {span}: reference/cache close ratio {r.ratio:.4g} over {r.days} "
            "day(s): another level or scale than the reference"
        )
    return report


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def verify(
    reference: Path,
    cache: Path,
    symbols: Sequence[str],
    first: date,
    last: date,
    *,
    side: str = "bid",
    tolerance: float = DEFAULT_TOLERANCE,
    include_volume: bool = False,
    workers: int = 4,
    progress: Callable[[], None] | None = None,
) -> list[SymbolReport]:
    """Compare every symbol-day in [first, last]; reads only, in parallel.

    A pending cancel (``cancel.cancellation``) stops the run: queued days are
    dropped and ``KeyboardInterrupt`` is raised.
    """
    jobs = [(s, d) for s in symbols for d in _days(first, last)]
    results: dict[str, list[DayResult]] = {s: [] for s in symbols}
    executor = ThreadPoolExecutor(max_workers=max(1, workers))
    interrupted = False
    try:
        futures = {
            executor.submit(
                compare_day,
                reference,
                cache,
                s,
                d,
                side=side,
                tolerance=tolerance,
                include_volume=include_volume,
            ): s
            for s, d in jobs
        }
        for fut in as_completed(futures):
            if cancellation.is_set():
                raise KeyboardInterrupt()
            res = fut.result()
            if res is not None:
                results[futures[fut]].append(res)
            if progress is not None:
                progress()
    except KeyboardInterrupt:
        interrupted = True
        cancellation.set()
        raise
    finally:
        executor.shutdown(wait=not interrupted, cancel_futures=interrupted)
    return [summarise(s, results[s], tolerance=tolerance) for s in symbols]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.3f}%"


def render_text(reports: Sequence[SymbolReport]) -> str:
    lines: list[str] = []
    header = f"{'symbol':<14}{'year':>6}" + "".join(f"{s:>11}" for s in STATUSES)
    header += f"{'worst':>11}{'median':>11}  worst day"
    lines.append(header)
    for rep in reports:
        for year, y in sorted(rep.years.items()):
            row = f"{rep.symbol:<14}{year:>6}" + "".join(f"{y['counts'][s]:>11}" for s in STATUSES)
            row += f"{_pct(y['worst_daily_diff']):>11}{_pct(y['median_daily_diff']):>11}"
            row += f"  {y['worst_day'] or '-'}"
            lines.append(row)
    lines.append("")
    lines.append("Close-ratio regimes (reference/cache):")
    for rep in reports:
        for r in rep.regimes:
            span = r.first.isoformat() if r.first == r.last else f"{r.first}..{r.last}"
            mark = "" if r.is_unity else "  <-"
            lines.append(f"  {rep.symbol} {span}: {r.ratio:.6g} ({r.days} day(s)){mark}")
    findings = [f for rep in reports for f in rep.findings]
    lines.append("")
    if findings:
        lines.append(f"Findings ({len(findings)}):")
        lines.extend(f"  {f}" for f in findings)
    else:
        lines.append("No findings: the cache matches the reference.")
    return "\n".join(lines) + "\n"


def render_json(reports: Sequence[SymbolReport]) -> dict[str, Any]:
    return {
        "failed": any(r.failed for r in reports),
        "findings": [f for r in reports for f in r.findings],
        "symbols": [r.as_json() for r in reports],
    }
