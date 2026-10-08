"""Write-time scale-discontinuity sentry for the Dukascopy daily candle cache.

A Dukascopy `tradedesk-md-export` run applies a single ``--price-divisor`` to
every tick it decodes.  If the operator re-runs the exporter for a later date
range with a *different* divisor, the resulting daily CSVs in
``cache_dir / SYMBOL / YYYY / MM / DD_{bid,ask}.csv.zst`` end up at a different
scale to the bulk of the cache.  Downstream backtests that assume one scale
silently produce wrong PnL across the boundary.

The sentry below compares the median close of a freshly-resampled day to the
medians of the closest existing canonical days in the cache (up to
``history_days`` days on each side, ``min_history`` minimum) and flags a write
when the ratio exceeds ``max_ratio``.

The check is intentionally history-anchored rather than absolute-band-anchored:
- it cannot become stale as a symbol's natural price drifts,
- it works regardless of the divisor convention chosen for the cache,
- it catches inconsistency without needing to know the canonical convention.

The first ever export for a symbol has no history and is therefore allowed
through; the *second* export run that drifts will be caught.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import zstandard as zstd

log = logging.getLogger(__name__)


class ScaleDiscontinuityError(RuntimeError):
    """Raised when a daily candle frame's scale diverges from the cache history."""


def _daily_bid_path(cache_dir: Path, symbol: str, day: date) -> Path:
    return (
        cache_dir
        / symbol
        / f"{day.year}"
        / f"{day.month - 1:02d}"
        / f"{day.day:02d}_bid.csv.zst"
    )


def _read_median_close(path: Path) -> float | None:
    """Read a daily candle CSV.zst and return the median close, or None on any error."""
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f_in, dctx.stream_reader(f_in) as reader:
            df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, zstd.ZstdError, pd.errors.ParserError):
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    m = float(df["close"].median())
    return None if math.isnan(m) else m


def _iter_days_around(
    day: date,
    *,
    history_days: int,
) -> Iterable[date]:
    """Yield dates within ±history_days of *day*, closest first, excluding *day*.

    Order: day-1, day+1, day-2, day+2, ... so we prefer the nearest neighbours
    when sampling for a baseline median.
    """
    for delta in range(1, history_days + 1):
        yield day - timedelta(days=delta)
        yield day + timedelta(days=delta)


def collect_history_medians(
    cache_dir: Path,
    symbol: str,
    day: date,
    *,
    history_days: int = 7,
    max_samples: int = 5,
) -> list[float]:
    """Return up to *max_samples* recent canonical-cache medians around *day*.

    Reads existing daily bid files within ±history_days of *day*.  The current
    day itself is excluded.  Days with unreadable / empty files are skipped.
    """
    medians: list[float] = []
    for probe in _iter_days_around(day, history_days=history_days):
        path = _daily_bid_path(cache_dir, symbol, probe)
        if not path.exists():
            continue
        m = _read_median_close(path)
        if m is not None and m > 0.0:
            medians.append(m)
            if len(medians) >= max_samples:
                break
    return medians


def check_scale_consistency(
    cache_dir: Path,
    symbol: str,
    day: date,
    new_median: float,
    *,
    max_ratio: float = 3.0,
    history_days: int = 7,
    max_samples: int = 5,
    min_history: int = 2,
) -> tuple[bool, str | None]:
    """Decide whether *new_median* is consistent with recent cache history.

    Returns ``(ok, reason)``.  ``ok=True`` when:
      - the new median is non-finite (handled elsewhere), OR
      - fewer than ``min_history`` neighbour files exist (cold start), OR
      - new / baseline ratio (and its inverse) are both below ``max_ratio``.

    The baseline is the median of the recent neighbour medians, which is
    robust to a single anomalous neighbour.
    """
    if not math.isfinite(new_median) or new_median <= 0.0:
        return True, None
    history = collect_history_medians(
        cache_dir,
        symbol,
        day,
        history_days=history_days,
        max_samples=max_samples,
    )
    if len(history) < min_history:
        return True, None
    history_sorted = sorted(history)
    baseline = history_sorted[len(history_sorted) // 2]
    if baseline <= 0.0:
        return True, None
    ratio = max(new_median / baseline, baseline / new_median)
    if ratio > max_ratio:
        reason = (
            f"{symbol} {day.isoformat()}: median close {new_median:.4f} "
            f"diverges {ratio:.1f}× from neighbour baseline {baseline:.4f} "
            f"(samples={history_sorted}). Likely a --price-divisor "
            f"mismatch vs the existing cache; daily CSV not written. "
            f"Investigate and re-run with the matching divisor."
        )
        return False, reason
    return True, None
