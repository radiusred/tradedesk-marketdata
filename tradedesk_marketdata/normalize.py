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
range: with a gold band of ``(500, 50_000)``, a gold price above $5 000 stored
at ÷1 000 instead of ÷100 still lands inside it.  That is why bands are
configuration (``price_bands.example.toml``, ``--bands``) and should hold an
instrument's whole history in the cache.

Both classes of error reduce to the same shape: every OHLC value on the
affected day is too large or too small by an integer power of ten.  This
module detects that case and applies the inverse factor in-place without
fetching the data again.  Days where the price already falls
inside the expected range are left untouched.
"""

from __future__ import annotations

import logging
import math
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import pandas as pd

# Re-exported under the historical private names for backward compatibility.
from ._zstio import read_zst as _read_zst
from ._zstio import write_zst as _write_zst

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Expected price bands: configuration
# ---------------------------------------------------------------------------
#
# Which band a symbol's natural price should sit in is configuration, read from
# a TOML file; the package ships ``price_bands.example.toml`` (documented there)
# and uses it when no --bands is given.

PRICE_BANDS_EXAMPLE = "price_bands.example.toml"

Band = tuple[float, float]


class PriceBandsError(ValueError):
    """A price-bands file that cannot be used: unreadable, not TOML, or not the schema."""


@dataclass(frozen=True)
class PriceBands:
    """Expected natural-unit price bands: exact symbols, then substring rules, then a default."""

    default: Band
    contains: tuple[tuple[str, Band], ...] = ()
    symbols: dict[str, Band] = field(default_factory=dict)

    def band(self, symbol: str) -> Band:
        upper = symbol.upper()
        if upper in self.symbols:
            return self.symbols[upper]
        for pattern, band in self.contains:
            if pattern in upper:
                return band
        return self.default


def _band(where: str, what: str, value: object) -> Band:
    ok = (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
    )
    if not ok:
        raise PriceBandsError(f"{where}: {what} must be [low, high]")
    assert isinstance(value, list)
    low, high = float(value[0]), float(value[1])
    if not 0 < low < high:
        raise PriceBandsError(f"{where}: {what} must have 0 < low < high")
    return (low, high)


def load_price_bands(path: Path | None = None) -> PriceBands:
    """Read a TOML price-bands file (the shipped example when ``path`` is None)."""
    if path is None:
        where = PRICE_BANDS_EXAMPLE
        raw = resources.files("tradedesk_marketdata").joinpath(PRICE_BANDS_EXAMPLE).read_bytes()
    else:
        where = str(path)
        try:
            raw = Path(path).read_bytes()
        except OSError as e:
            raise PriceBandsError(f"{where}: cannot read the price bands ({e})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PriceBandsError(f"{where}: not a TOML file ({e})") from None
    unknown = set(data) - {"default", "contains", "symbols"}
    if unknown:
        raise PriceBandsError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    if "default" not in data:
        raise PriceBandsError(f"{where}: no default band")
    default = _band(where, "default", data["default"])
    contains = []
    for i, rule in enumerate(data.get("contains", [])):
        if not isinstance(rule, dict) or set(rule) != {"pattern", "band"}:
            raise PriceBandsError(f"{where}: contains[{i}] needs exactly pattern and band")
        if not isinstance(rule["pattern"], str) or not rule["pattern"]:
            raise PriceBandsError(f"{where}: contains[{i}].pattern must be text")
        contains.append(
            (rule["pattern"].upper(), _band(where, f"contains[{i}].band", rule["band"]))
        )
    table = data.get("symbols", {})
    if not isinstance(table, dict):
        raise PriceBandsError(f"{where}: symbols must be a table of SYMBOL = [low, high]")
    symbols = {k.upper(): _band(where, f"symbols.{k}", v) for k, v in table.items()}
    return PriceBands(default=default, contains=tuple(contains), symbols=symbols)


_SHIPPED_BANDS: PriceBands | None = None


def _expected_price_range(symbol: str, bands: PriceBands | None = None) -> Band:
    """Return the (min, max) plausible mid-price range for *symbol*.

    From ``bands``, or the shipped ``price_bands.example.toml`` when None. The
    ranges are used to detect and correct price scale errors in cached daily
    candle files, and are intentionally wide to avoid false positives.
    """
    global _SHIPPED_BANDS
    if bands is None:
        if _SHIPPED_BANDS is None:
            _SHIPPED_BANDS = load_price_bands()
        bands = _SHIPPED_BANDS
    return bands.band(symbol)


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
    bands: PriceBands | None = None,
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
        bands: Expected price bands; the shipped example when ``None``.

    Returns:
        Dict with keys ``"fixed"``, ``"skipped"``, ``"errors"``.
    """
    price_min, price_max = _expected_price_range(symbol, bands)
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
    bands: PriceBands | None = None,
) -> dict[str, dict[str, int]]:
    """Normalize all symbols (or a specified subset) in *cache_dir*.

    Args:
        cache_dir: Root of the cache directory.
        symbols: Symbols to process; defaults to every subdirectory.
        dry_run: If ``True``, no files are modified.
        bands: Expected price bands; the shipped example when ``None``.

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
        results[symbol] = normalize_symbol(sym_dir, symbol, dry_run=dry_run, bands=bands)

    return results
