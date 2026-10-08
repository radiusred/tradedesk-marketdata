"""Rescale daily candle cache files that drifted off a symbol's dominant scale.

Sibling to :mod:`tradedesk_marketdata.normalize`, but with a different target:
``normalize`` brings each day's prices into a hardcoded *natural-units* band
(``USDJPY`` → 50–500); ``rescale`` brings each day's prices into the symbol's
**existing dominant cache scale** by power-of-ten multiplication.  This is the
right tool when the bulk of the cache is at one scale (e.g. ``--price-divisor
10`` for FX/JPY pairs, which leaves USDJPY at ~15 700) and a small subset of
days was overwritten by a later run with a different ``--price-divisor`` value.

For each symbol the dominant scale is the median of all per-day median closes.
For each day the rescale factor is the unique power of ten ``f ∈ {1e-4 … 1e4}``
that brings ``day_median * f`` within ``±tolerance`` of the dominant median.
Days that cannot be rescaled by a power of ten are flagged ``unfixable`` and
left alone; the operator must delete and re-export those.
"""

from __future__ import annotations

import logging
import math
import statistics
from pathlib import Path

from ._zstio import read_zst as _read_zst
from ._zstio import write_zst as _write_zst

log = logging.getLogger(__name__)

_PRICE_COLS = ("open", "high", "low", "close")
_FACTOR_CANDIDATES = (1e-4, 1e-3, 1e-2, 1e-1, 1.0, 1e1, 1e2, 1e3, 1e4)


def _iter_bid_files(sym_dir: Path):
    for year_dir in sorted(sym_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            yield from sorted(month_dir.glob("*_bid.csv.zst"))


def _day_median_close(path: Path) -> float | None:
    df = _read_zst(path)
    if df is None or df.empty or "close" not in df.columns:
        return None
    m = float(df["close"].median())
    return None if math.isnan(m) else m


def _pick_factor(day_median: float, target: float, tolerance: float) -> float | None:
    """Return the power-of-ten factor that snaps day_median onto target.

    Returns 1.0 when already in tolerance, None when no candidate matches.
    """
    if day_median <= 0.0 or target <= 0.0:
        return None
    best: tuple[float, float] | None = None  # (relative_distance, factor)
    for factor in _FACTOR_CANDIDATES:
        scaled = day_median * factor
        rel = abs(math.log10(scaled / target))
        if rel < tolerance:
            if best is None or rel < best[0]:
                best = (rel, factor)
    if best is None:
        return None
    return best[1]


def rescale_symbol(
    sym_dir: Path,
    symbol: str,
    *,
    dry_run: bool = False,
    tolerance_log10: float = 0.18,  # ≈ ±50% in log10 space; smaller than one power of 10
) -> dict[str, list[str]]:
    """Bring every day's prices in *sym_dir* onto the symbol's dominant scale.

    Algorithm:
      1. Read all per-day median closes.
      2. Dominant scale = median of those medians.
      3. For each day, snap to a power-of-ten factor f such that
         abs(log10(day_med * f / dominant)) < tolerance_log10.
      4. If no f works the day is *unfixable* — left alone; caller must
         delete and re-export.

    Returns a dict with keys ``fixed``, ``unchanged``, ``unfixable``, ``errors``
    each mapping to a list of file paths (relative to ``sym_dir``).
    """
    result: dict[str, list[str]] = {
        "fixed": [],
        "unchanged": [],
        "unfixable": [],
        "errors": [],
    }

    bid_files = list(_iter_bid_files(sym_dir))
    if not bid_files:
        return result

    medians: dict[Path, float] = {}
    for bid in bid_files:
        m = _day_median_close(bid)
        if m is None:
            continue
        medians[bid] = m
    if not medians:
        return result

    dominant = statistics.median(medians.values())
    log.info(
        "%s: %d days surveyed, dominant median = %.4f",
        symbol,
        len(medians),
        dominant,
    )

    for bid_path, day_median in medians.items():
        rel_path = str(bid_path.relative_to(sym_dir))
        factor = _pick_factor(day_median, dominant, tolerance_log10)
        if factor is None:
            log.warning(
                "%s: cannot rescale %s (median=%.4f, dominant=%.4f) — delete and re-export",
                symbol,
                rel_path,
                day_median,
                dominant,
            )
            result["unfixable"].append(rel_path)
            continue
        if factor == 1.0:
            result["unchanged"].append(rel_path)
            continue

        log.info(
            "%s: %s median %.4f × %g → %.4f (dominant %.4f)",
            symbol,
            rel_path,
            day_median,
            factor,
            day_median * factor,
            dominant,
        )

        if dry_run:
            result["fixed"].append(rel_path)
            continue

        ask_path = bid_path.parent / bid_path.name.replace("_bid.csv.zst", "_ask.csv.zst")
        ok = True
        for side_path in (bid_path, ask_path):
            if not side_path.exists():
                continue
            df = _read_zst(side_path)
            if df is None or df.empty:
                continue
            try:
                for col in _PRICE_COLS:
                    if col in df.columns:
                        df[col] = df[col] * factor
                _write_zst(df, side_path)
            except (OSError, ValueError) as exc:
                log.error("%s: write failed for %s: %s", symbol, side_path, exc)
                result["errors"].append(str(side_path.relative_to(sym_dir)))
                ok = False
                break
        if ok:
            result["fixed"].append(rel_path)

    return result


def rescale_cache(
    cache_dir: Path,
    symbols: list[str] | None = None,
    *,
    dry_run: bool = False,
    tolerance_log10: float = 0.18,
) -> dict[str, dict[str, list[str]]]:
    """Rescale every symbol (or a specified subset) in *cache_dir*."""
    if symbols is None:
        symbols = sorted(d.name for d in cache_dir.iterdir() if d.is_dir())
    results: dict[str, dict[str, list[str]]] = {}
    for symbol in sorted(symbols):
        sym_dir = cache_dir / symbol
        if not sym_dir.is_dir():
            log.warning("Symbol directory not found: %s", sym_dir)
            continue
        results[symbol] = rescale_symbol(
            sym_dir, symbol, dry_run=dry_run, tolerance_log10=tolerance_log10
        )
    return results
