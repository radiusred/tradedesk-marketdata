"""
Normalize cache daily candle files with incorrect price scaling.

Detects days where cached prices are off by a power of ten compared to the
expected real-price range for the instrument, and corrects them in-place by
multiplying every OHLC value by the inverse power of ten.

Background
----------
When ``tradedesk-md-export`` is run with the wrong ``--price-divisor`` for a
symbol the daily candle CSVs store prices that are off by a power of ten.
The correct divisor varies by instrument type:

* 4-decimal FX (EURUSD, AUDNZD, GBPUSD …): ÷100 000
* 2-decimal FX / JPY crosses (USDJPY, AUDJPY …): ÷1 000
* 2-decimal commodities (XAUUSD, XAGUSD …): ÷100

A second class of miscalibration arises when the inferred divisor was the
wrong one because the instrument's price had moved outside the expected
range.  A known instance: XAUUSD files downloaded between 2026-01-25 and
2026-03-10 were stored at ÷1 000 instead of ÷100 because gold broke $5 000
for the first time and the plausible-range guard was set too low
(``(500, 50_000)``).  The guard was corrected to ``(1_000, 50_000)`` in the
same release that introduced this module.

Both classes of error reduce to the same shape: every OHLC value on the
affected day is too large or too small by an integer power of ten.  This
module detects that case and applies the inverse factor in-place without
fetching the data again.  Days where the price already falls
inside the expected range are left untouched.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pandas as pd

# Re-exported under the historical private names for backward compatibility.
from ._zstio import read_zst as _read_zst
from ._zstio import write_zst as _write_zst

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instrument classification
# ---------------------------------------------------------------------------

_JPY_CROSSES = frozenset(
    {
        "AUDJPY",
        "CADJPY",
        "CHFJPY",
        "EURJPY",
        "GBPJPY",
        "NZDJPY",
        "SGDJPY",
        "USDJPY",
    }
)
# Gold and silver need separate ranges: gold has never been below $1 000 in the
# modern era, so a lower bound of 1 000 prevents ÷1000 from being chosen when
# the correct divisor is ÷100 (which would happen if gold > $5 000 and the
# stored raw value is in the 500 000–999 999 range).
_GOLD = frozenset({"XAUUSD"})
_SILVER = frozenset({"XAGUSD"})
_IDX_SUBSTRINGS = ("IDX",)
# Per-index ranges. The default IDX fallback (100, 500_000) is wide enough to
# admit two divisors for the same raw value — e.g. USA500 raw ~3 000 000 fits
# both ÷1000 (3 000) and ÷10000 (300), and infer_price_divisor would pick the
# larger one (÷10000) since it tries divisors largest-first. Tight per-index
# bands eliminate that ambiguity.
_INDEX_RANGES: dict[str, tuple[float, float]] = {
    "USA500IDXUSD": (1_000.0, 10_000.0),  # S&P 500: ~2400-7000 in our era
    "USATECHIDXUSD": (2_000.0, 30_000.0),  # Nasdaq Composite: ~3500-22000
    "DEUIDXEUR": (3_000.0, 30_000.0),  # DAX: ~5000-25000
    "GBRIDXGBP": (2_000.0, 12_000.0),  # FTSE 100: ~3000-9000
    # Nikkei 225: broke 60k in Apr-2026; band wide enough to avoid the
    # XAUUSD-style band-too-low miscalibration.
    "JPNIDXJPY": (10_000.0, 100_000.0),
    "AUSIDXAUD": (3_000.0, 12_000.0),  # ASX 200: ~4000-9000
}
# Crude oil and energy commodities quoted in USD per barrel (~20–200 range).
# LIGHTCMDUSD is the WTI light-sweet crude symbol; treat identically.
_CRUDE_OIL = frozenset({"BRENTCMDUSD", "WTIOILUSD", "USOILUSD", "LIGHTCMDUSD"})
# COMEX copper futures quoted in USD/lb. Historical envelope $0.50 (1999) to
# $6.40 (2026-04 spike). Without an explicit band, the standard FX default
# (0.3, 5.0) would treat any post-2024 day above $5 as 10× too large and
# corrupt the file.
_COPPER = frozenset({"COPPERCMDUSD"})
# Henry-Hub natural gas in USD/MMBtu. Has spiked to ~$13 (2008, 2022).
_NATGAS = frozenset({"GASCMDUSD", "NATGASCMDUSD"})
# Platinum-group metals quoted in USD/oz at higher levels than silver.
_PALLADIUM = frozenset({"XPDCMDUSD"})
_PLATINUM = frozenset({"XPTCMDUSD"})
# European bond futures quoted as a price index in the 100–200 range.
_BOND_FUTURE = frozenset({"BUNDTREUR"})
# Pairs quoted above 5.0 in their natural rate (e.g. EURSEK ~11, EURNOK ~12).
# Without this, infer_price_divisor selects ÷100000 instead of ÷10000 because
# both results fall in the default FX range (0.3, 15.0).
_HIGH_RATE_FX = frozenset({"EURSEK", "EURNOK", "USDNOK", "GBPSEK", "GBPNOK"})


def _expected_price_range(symbol: str) -> tuple[float, float]:
    """Return the (min, max) plausible mid-price range for *symbol*.

    These ranges are used to detect and correct price scale errors in cached
    daily candle files.  They are intentionally wide to avoid false positives.
    """
    upper = symbol.upper()
    if upper in _JPY_CROSSES:
        return (50.0, 500.0)
    if upper in _GOLD:
        return (1_000.0, 50_000.0)
    if upper in _SILVER:
        return (10.0, 500.0)
    if upper in _PALLADIUM:
        # Palladium has traded $200 (2003) to $3 400 (2022); band kept wide.
        return (100.0, 5_000.0)
    if upper in _PLATINUM:
        # Platinum has traded $400 (2002) to $2 250 (2008); band kept wide.
        return (200.0, 3_000.0)
    if upper in _CRUDE_OIL:
        # Crude oil quoted in USD per barrel; range covers post-2000 extremes.
        return (10.0, 250.0)
    if upper in _COPPER:
        # COMEX copper futures USD/lb. Wide enough to cover the 2026-04 spike
        # to $6.40 yet still flag a 10× drift (would land >50).
        return (0.3, 15.0)
    if upper in _NATGAS:
        # Henry-Hub natural gas USD/MMBtu. Wide enough to cover the 2008/2022
        # ~$13 spikes yet still flag a 10× drift.
        return (0.5, 25.0)
    if upper in _BOND_FUTURE:
        # Euro Bund Future trades roughly 110–180 as a price index.
        return (80.0, 200.0)
    if upper in _HIGH_RATE_FX:
        # Pairs with a natural rate above 5 — prevent over-division by 100000.
        return (5.0, 20.0)
    if upper in _INDEX_RANGES:
        return _INDEX_RANGES[upper]
    if any(s in upper for s in _IDX_SUBSTRINGS):
        # Generic fallback for unknown indices; deliberately wide.
        return (100.0, 500_000.0)
    # Standard 4-decimal FX pairs. Upper bound is 5.0 (not 15.0) so that a
    # value stuck 10× too high — e.g. NZDUSD at 5.87 instead of 0.587 — is
    # detected and corrected. Pairs whose natural rate exceeds 5 (SEK/NOK
    # crosses) live in _HIGH_RATE_FX with their own (5.0, 20.0) band.
    return (0.3, 5.0)


# Candidate multiplicative corrections, ordered from strongest divide (1e-5)
# through unity to strongest multiply (1e5).  Symmetric around 1.0 so the
# same routine fixes days that were exported with a divisor that was too
# small OR too large for the symbol.
_CORRECTION_FACTORS: tuple[float, ...] = (
    1e-5,
    1e-4,
    1e-3,
    1e-2,
    1e-1,
    1.0,
    1e1,
    1e2,
    1e3,
    1e4,
    1e5,
)


def infer_correction_factor(
    median_close: float,
    price_min: float,
    price_max: float,
) -> float:
    """Return the power-of-ten factor *f* such that ``median_close * f`` lies
    inside ``[price_min, price_max]``.

    When several candidate factors all land inside the band — possible on
    wide bands — the factor whose result sits closest to the band's geometric
    midpoint (i.e. the centre in log10 space) is preferred.  This avoids
    snapping a barely-out value past the midpoint and out the other side.

    Returns ``1.0`` when the price is already inside the band or when no
    candidate succeeds (e.g. a corrupt or extreme value that cannot be
    reconciled by a power-of-ten correction).
    """
    if median_close <= 0.0 or price_min <= 0.0 or price_max <= 0.0:
        return 1.0
    if price_min <= median_close <= price_max:
        return 1.0
    mid_log = (math.log10(price_min) + math.log10(price_max)) / 2.0
    best: tuple[float, float] | None = None  # (distance_to_mid, factor)
    for factor in _CORRECTION_FACTORS:
        scaled = median_close * factor
        if price_min <= scaled <= price_max:
            distance = abs(math.log10(scaled) - mid_log)
            if best is None or distance < best[0]:
                best = (distance, factor)
    return 1.0 if best is None else best[1]


def infer_price_divisor(
    median_close: float,
    price_min: float,
    price_max: float,
) -> float:
    """Return the divisor that brings an over-scaled *median_close* into range.

    Thin wrapper over :func:`infer_correction_factor` that preserves the
    historical "divisor" semantics: returns a value ``>= 1.0`` representing
    the integer power of ten by which the stored value is too large.  When
    the value is already correct, or is too small (multiply needed), or no
    correction can be inferred, returns ``1.0``.
    """
    factor = infer_correction_factor(median_close, price_min, price_max)
    if factor >= 1.0:
        return 1.0
    return 1.0 / factor


# ---------------------------------------------------------------------------
# Core normalization logic
# ---------------------------------------------------------------------------


def normalize_symbol(
    sym_dir: Path,
    symbol: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Normalize all daily candle files for *symbol* in *sym_dir*.

    Scans every ``*_bid.csv.zst`` file under *sym_dir*, checks whether the
    median close price is within the expected range, infers the required
    divisor if not, and rewrites both the bid and the corresponding ask file
    in-place.

    Args:
        sym_dir: Directory for this symbol (e.g. ``cache_dir / "AUDNZD"``).
        symbol: Instrument symbol string used for range lookup.
        dry_run: If ``True``, report what would change without writing files.

    Returns:
        Dict with keys ``"fixed"``, ``"skipped"``, ``"errors"``.
    """
    price_min, price_max = _expected_price_range(symbol)
    result: dict[str, int] = {"fixed": 0, "skipped": 0, "errors": 0}

    for year_dir in sorted(sym_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for bid_path in sorted(month_dir.glob("*_bid.csv.zst")):
                ask_path = bid_path.parent / bid_path.name.replace("_bid.csv.zst", "_ask.csv.zst")

                df_bid = _read_zst(bid_path)
                if df_bid is None or df_bid.empty:
                    result["skipped"] += 1
                    continue

                median = float(df_bid["close"].median())
                if pd.isna(median):
                    result["skipped"] += 1
                    continue

                factor = infer_correction_factor(median, price_min, price_max)
                if factor == 1.0:
                    continue  # already correct

                day_label = f"{year_dir.name}/{month_dir.name}/{bid_path.name[:2]}"
                if factor < 1.0:
                    action = "would divide" if dry_run else "dividing"
                    log.info(
                        "%s %s: median close %.4f is %.0f× too large — %s by %.0f",
                        symbol,
                        day_label,
                        median,
                        1.0 / factor,
                        action,
                        1.0 / factor,
                    )
                else:
                    action = "would multiply" if dry_run else "multiplying"
                    log.info(
                        "%s %s: median close %.4f is %.0f× too small — %s by %.0f",
                        symbol,
                        day_label,
                        median,
                        factor,
                        action,
                        factor,
                    )

                result["fixed"] += 1
                if dry_run:
                    continue

                # Apply factor to bid OHLC (not volume)
                price_cols = ["open", "high", "low", "close"]
                for col in price_cols:
                    df_bid[col] = df_bid[col] * factor
                try:
                    _write_zst(df_bid, bid_path)
                except Exception as exc:
                    log.error("Failed to write %s: %s", bid_path, exc)
                    result["errors"] += 1
                    continue

                # Apply same factor to ask
                df_ask = _read_zst(ask_path)
                if df_ask is not None and not df_ask.empty:
                    for col in price_cols:
                        df_ask[col] = df_ask[col] * factor
                    try:
                        _write_zst(df_ask, ask_path)
                    except Exception as exc:
                        log.error("Failed to write %s: %s", ask_path, exc)
                        result["errors"] += 1

    return result


def normalize_cache(
    cache_dir: Path,
    symbols: list[str] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, dict[str, int]]:
    """Normalize all symbols (or a specified subset) in *cache_dir*.

    Args:
        cache_dir: Root of the cache directory.
        symbols: Symbols to process; defaults to every subdirectory.
        dry_run: If ``True``, no files are modified.

    Returns:
        Dict mapping symbol name to per-symbol result dicts.
    """
    if symbols is None:
        symbols = sorted(d.name for d in cache_dir.iterdir() if d.is_dir())

    results: dict[str, dict[str, int]] = {}
    for symbol in sorted(symbols):
        sym_dir = cache_dir / symbol
        if not sym_dir.is_dir():
            log.warning("Symbol directory not found: %s", sym_dir)
            continue
        results[symbol] = normalize_symbol(sym_dir, symbol, dry_run=dry_run)

    return results
