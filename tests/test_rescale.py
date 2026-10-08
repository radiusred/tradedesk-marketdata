"""Tests for tradedesk_marketdata.rescale."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import zstandard as zstd

from tradedesk_marketdata.rescale import _pick_factor, rescale_cache, rescale_symbol


def _write_day(cache_dir: Path, symbol: str, day: date, median: float, side: str = "bid") -> Path:
    p = (
        cache_dir
        / symbol
        / str(day.year)
        / f"{day.month - 1:02d}"
        / f"{day.day:02d}_{side}.csv.zst"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range(
        pd.Timestamp(day.isoformat() + "T00:00:00", tz="UTC"), periods=5, freq="1min"
    )
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": [median] * 5,
            "high": [median + 0.1] * 5,
            "low": [median - 0.1] * 5,
            "close": [median] * 5,
            "volume": [100.0] * 5,
        }
    )
    cctx = zstd.ZstdCompressor(level=3)
    p.write_bytes(cctx.compress(df.to_csv(index=False).encode("utf-8")))
    return p


def _read_median(path: Path) -> float:
    dctx = zstd.ZstdDecompressor()
    df = pd.read_csv(__import__("io").BytesIO(dctx.decompress(path.read_bytes())))
    return float(df["close"].median())


# ---------------------------------------------------------------------------
# _pick_factor
# ---------------------------------------------------------------------------


def test_pick_factor_exact_match_returns_1() -> None:
    assert _pick_factor(15_720.0, 15_700.0, 0.18) == 1.0


def test_pick_factor_10x_too_large() -> None:
    # 157200 → ÷10 to land near 15700
    assert _pick_factor(157_200.0, 15_700.0, 0.18) == 0.1


def test_pick_factor_100x_too_small() -> None:
    # 157.0 → ×100 to land near 15700
    assert _pick_factor(157.0, 15_700.0, 0.18) == 100.0


def test_pick_factor_10000x_too_small() -> None:
    # 1.59 → ×10000 to land near 15900
    assert _pick_factor(1.59, 15_700.0, 0.18) == 10_000.0


def test_pick_factor_no_match_returns_none() -> None:
    # 27.3 — no power of 10 brings this within ±50% of 15700
    assert _pick_factor(27.3, 15_700.0, 0.18) is None


# ---------------------------------------------------------------------------
# rescale_symbol
# ---------------------------------------------------------------------------


def test_rescale_symbol_repairs_rad1920_fingerprint(tmp_path: Path) -> None:
    """Pre-flip days at canonical scale, post-flip block at 10×, one outlier at 1/100×."""
    sym = "USDJPY"
    for d, med in [
        (date(2026, 5, 1), 15_700.0),
        (date(2026, 5, 4), 15_710.0),
        (date(2026, 5, 5), 157_200.0),  # 10× too large
        (date(2026, 5, 6), 157_300.0),  # 10× too large
        (date(2026, 5, 13), 157.8),  # 100× too small
    ]:
        _write_day(tmp_path, sym, d, med)
        _write_day(tmp_path, sym, d, med + 0.5, side="ask")

    result = rescale_symbol(tmp_path / sym, sym)

    # Three days needed fixing; two unchanged.
    assert len(result["fixed"]) == 3
    assert len(result["unchanged"]) == 2
    assert result["unfixable"] == []
    assert result["errors"] == []

    bid_5 = tmp_path / sym / "2026" / "04" / "05_bid.csv.zst"
    bid_13 = tmp_path / sym / "2026" / "04" / "13_bid.csv.zst"
    bid_1 = tmp_path / sym / "2026" / "04" / "01_bid.csv.zst"
    assert abs(_read_median(bid_5) - 15_720.0) < 1.0
    assert abs(_read_median(bid_13) - 15_780.0) < 1.0
    assert abs(_read_median(bid_1) - 15_700.0) < 1.0  # untouched


def test_rescale_symbol_marks_unfixable(tmp_path: Path) -> None:
    """A day whose median doesn't map to a power of 10 from dominant is unfixable."""
    sym = "USDJPY"
    for d, med in [
        (date(2026, 5, 1), 15_700.0),
        (date(2026, 5, 4), 15_710.0),
        (date(2026, 5, 5), 27.3),  # no power-of-10 from ~15700
    ]:
        _write_day(tmp_path, sym, d, med)

    result = rescale_symbol(tmp_path / sym, sym)

    assert result["unfixable"] == ["2026/04/05_bid.csv.zst"]
    assert result["fixed"] == []
    # Unfixable file must be left untouched
    bid_5 = tmp_path / sym / "2026" / "04" / "05_bid.csv.zst"
    assert abs(_read_median(bid_5) - 27.3) < 0.01


def test_rescale_symbol_dry_run_does_not_modify(tmp_path: Path) -> None:
    sym = "USDJPY"
    _write_day(tmp_path, sym, date(2026, 5, 1), 15_700.0)
    _write_day(tmp_path, sym, date(2026, 5, 4), 15_710.0)
    bad_path = _write_day(tmp_path, sym, date(2026, 5, 5), 157_200.0)
    _write_day(tmp_path, sym, date(2026, 5, 5), 157_220.0, side="ask")

    result = rescale_symbol(tmp_path / sym, sym, dry_run=True)
    assert len(result["fixed"]) == 1
    assert abs(_read_median(bad_path) - 157_200.0) < 1.0  # untouched


def test_rescale_symbol_applies_to_ask_as_well(tmp_path: Path) -> None:
    sym = "USDJPY"
    _write_day(tmp_path, sym, date(2026, 5, 1), 15_700.0)
    _write_day(tmp_path, sym, date(2026, 5, 4), 15_710.0)
    bid_p = _write_day(tmp_path, sym, date(2026, 5, 5), 157_200.0)
    ask_p = _write_day(tmp_path, sym, date(2026, 5, 5), 157_220.0, side="ask")

    rescale_symbol(tmp_path / sym, sym)
    assert abs(_read_median(bid_p) - 15_720.0) < 1.0
    assert abs(_read_median(ask_p) - 15_722.0) < 1.0


def test_rescale_cache_processes_multiple_symbols(tmp_path: Path) -> None:
    _write_day(tmp_path, "USDJPY", date(2026, 5, 1), 15_700.0)
    _write_day(tmp_path, "USDJPY", date(2026, 5, 4), 15_710.0)
    _write_day(tmp_path, "USDJPY", date(2026, 5, 5), 157_200.0)
    _write_day(tmp_path, "EURUSD", date(2026, 5, 1), 11_000.0)
    _write_day(tmp_path, "EURUSD", date(2026, 5, 4), 11_010.0)
    _write_day(tmp_path, "EURUSD", date(2026, 5, 5), 110_200.0)

    results = rescale_cache(tmp_path)
    assert len(results["USDJPY"]["fixed"]) == 1
    assert len(results["EURUSD"]["fixed"]) == 1


def test_rescale_cache_empty_directory(tmp_path: Path) -> None:
    """Empty cache directory should not crash."""
    results = rescale_cache(tmp_path)
    assert results == {}
