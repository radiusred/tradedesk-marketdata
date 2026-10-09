"""Tests for tradedesk_marketdata.normalize."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from tradedesk_marketdata.normalize import (
    _expected_price_range,
    _read_zst,
    _write_zst,
    infer_correction_factor,
    infer_price_divisor,
    normalize_cache,
    normalize_symbol,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daily_candle_df(close_price: float, n: int = 5) -> pd.DataFrame:
    """Create a minimal 1-min candle DataFrame at a fixed price."""
    base = pd.Timestamp("2026-03-10T00:00:00", tz="UTC")
    idx = pd.date_range(base, periods=n, freq="1min")
    return pd.DataFrame(
        {
            "open": [close_price] * n,
            "high": [close_price + 0.1] * n,
            "low": [close_price - 0.1] * n,
            "close": [close_price] * n,
            "volume": [100.0] * n,
        },
        index=idx,
    )


def _write_candle_zst(path: Path, close_price: float, n: int = 5) -> None:
    """Write a minimal candle CSV.zst at *path*."""
    df = _make_daily_candle_df(close_price, n)
    _write_zst(df, path)


def _read_median_close(path: Path) -> float:
    df = _read_zst(path)
    assert df is not None
    return float(df["close"].median())


def _make_cache_day(
    tmp_path: Path,
    symbol: str,
    year: int,
    month_0: int,
    day: int,
    bid_price: float,
    ask_price: float,
) -> tuple[Path, Path]:
    """Write bid+ask CSV.zst files for one day in the standard cache layout."""
    day_dir = tmp_path / symbol / str(year) / f"{month_0:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    bid_path = day_dir / f"{day:02d}_bid.csv.zst"
    ask_path = day_dir / f"{day:02d}_ask.csv.zst"
    _write_candle_zst(bid_path, bid_price)
    _write_candle_zst(ask_path, ask_price)
    return bid_path, ask_path


# ---------------------------------------------------------------------------
# _expected_price_range
# ---------------------------------------------------------------------------


def test_expected_price_range_jpy_cross() -> None:
    lo, hi = _expected_price_range("USDJPY")
    assert lo == 50.0
    assert hi == 500.0


def test_expected_price_range_jpy_cross_lowercase() -> None:
    lo, hi = _expected_price_range("audjpy")
    assert lo == 50.0
    assert hi == 500.0


def test_expected_price_range_gold() -> None:
    lo, hi = _expected_price_range("XAUUSD")
    assert lo > 0 and hi > lo


def test_expected_price_range_index() -> None:
    # USA500 is now a known index with a tight band (1_000, 10_000) so that
    # infer_price_divisor unambiguously picks ÷1000 for raw ~3M values.
    lo, hi = _expected_price_range("USA500IDXUSD")
    assert lo == 1_000.0
    assert hi == 10_000.0


def test_expected_price_range_unknown_index_fallback() -> None:
    # Unknown indices fall through to the wide generic IDX band.
    lo, hi = _expected_price_range("UNKNOWNIDXUSD")
    assert lo == 100.0
    assert hi == 500_000.0


def test_expected_price_range_known_indices() -> None:
    # Sanity-check each known index's range covers its post-2000 trading band.
    for symbol, expected_mid in {
        "DEUIDXEUR": 18_000.0,  # DAX 2025
        "GBRIDXGBP": 8_000.0,  # FTSE 2025
        "JPNIDXJPY": 63_000.0,  # Nikkei 2026-04 (broke 60k)
        "AUSIDXAUD": 8_000.0,  # ASX 2025
    }.items():
        lo, hi = _expected_price_range(symbol)
        assert lo <= expected_mid <= hi, f"{symbol} band {lo}-{hi} excludes {expected_mid}"


def test_infer_divisor_usa500_picks_1000_not_10000() -> None:
    # USA500 raw close ~3 000 000 must pick ÷1000 (→ 3000, real S&P level),
    # not ÷10000 (→ 300, well below S&P).
    lo, hi = _expected_price_range("USA500IDXUSD")
    assert infer_price_divisor(3_000_000.0, lo, hi) == 1_000.0


def test_infer_divisor_nikkei_picks_1000_not_10000() -> None:
    # With Nikkei trading >60k in Apr-2026, raw 60M cache values must pick
    # ÷1000 (→ 60 000, real Nikkei) not ÷10000 (→ 6 000, well below Nikkei).
    # A narrower band (5_000, 60_000) would allow both divisors to land
    # inside it and `infer_price_divisor` would pick the larger one; the
    # widened band must exclude the ÷10000 result.
    lo, hi = _expected_price_range("JPNIDXJPY")
    assert infer_price_divisor(60_000_000.0, lo, hi) == 1_000.0
    # The early-cycle corruption (~22M, Nikkei 2018) must also pick ÷1000.
    assert infer_price_divisor(22_000_000.0, lo, hi) == 1_000.0


def test_infer_divisor_nikkei_above_60k_left_unchanged() -> None:
    # 2026-04-13 cache day printed median 63 129 (real Nikkei). The pre-2122
    # band capped at 60 000 so `infer_price_divisor` would falsely flag this
    # as ÷10 too large and corrupt the file. The widened band must leave it.
    lo, hi = _expected_price_range("JPNIDXJPY")
    assert infer_price_divisor(63_129.0, lo, hi) == 1.0


def test_expected_price_range_fx4() -> None:
    lo, hi = _expected_price_range("EURUSD")
    # Standard FX pair: price well under 15
    assert hi < 20


def test_expected_price_range_fx4_upper_excludes_10x_drift() -> None:
    # Regression: NZDUSD stored at 5.87 (10× too high) must
    # fall OUTSIDE the standard FX band so normalize flags it.  An upper
    # bound of 15.0 was too lax — 5.87 was admitted and the file was left
    # alone.  A tighter upper of 5.0 still admits every major non-JPY/
    # non-high-rate FX cross while catching a 10× shift.
    lo, hi = _expected_price_range("NZDUSD")
    assert hi <= 5.0
    assert not (lo <= 5.87 <= hi)


def test_infer_factor_nzdusd_10x_too_large_divides_by_10() -> None:
    # NZDUSD median 5.87 must collapse to 0.587 (factor 0.1).
    lo, hi = _expected_price_range("NZDUSD")
    assert infer_correction_factor(5.87, lo, hi) == pytest.approx(0.1)


def test_infer_factor_nzdusd_already_correct_left_unchanged() -> None:
    # Sanity: a correct NZDUSD value (~0.587) must not be touched.
    lo, hi = _expected_price_range("NZDUSD")
    assert infer_correction_factor(0.587, lo, hi) == 1.0


def test_infer_factor_fx_majors_in_their_natural_range_left_unchanged() -> None:
    # Every non-JPY, non-high-rate FX cross in our universe must sit inside
    # the tightened standard band — protects against false positives.
    for sym, natural in {
        "AUDCAD": 0.90,
        "AUDNZD": 1.09,
        "AUDUSD": 0.66,
        "EURCAD": 1.49,
        "EURCHF": 0.94,
        "EURGBP": 0.84,
        "EURSGD": 1.46,
        "EURUSD": 1.10,
        "GBPAUD": 1.95,
        "GBPCHF": 1.13,
        "GBPUSD": 1.27,
        "NZDCAD": 0.83,
        "NZDUSD": 0.59,
        "USDCAD": 1.36,
        "USDCHF": 0.85,
    }.items():
        lo, hi = _expected_price_range(sym)
        assert infer_correction_factor(natural, lo, hi) == 1.0, (
            f"{sym} natural {natural} unexpectedly outside band {lo}-{hi}"
        )


def test_expected_price_range_crude_oil() -> None:
    lo, hi = _expected_price_range("BRENTCMDUSD")
    assert lo <= 20.0
    assert hi >= 150.0


def test_expected_price_range_copper_covers_2026_spike() -> None:
    # COMEX copper futures USD/lb hit $6.40 in 2026-04. The standard FX
    # default (0.3, 5.0) would corrupt these legit days — copper needs its
    # own wider band.
    lo, hi = _expected_price_range("COPPERCMDUSD")
    assert lo <= 0.5
    assert hi >= 6.4


def test_infer_factor_copper_at_real_price_left_unchanged() -> None:
    lo, hi = _expected_price_range("COPPERCMDUSD")
    # 2026-04-22: real copper 6.36 USD/lb — must not be flagged.
    assert infer_correction_factor(6.36, lo, hi) == 1.0
    # 1999 low: real copper 0.65 USD/lb.
    assert infer_correction_factor(0.65, lo, hi) == 1.0


def test_expected_price_range_natgas() -> None:
    # Henry-Hub natural gas USD/MMBtu; envelope covers $1.50 (2020) – $13 (2008/2022).
    lo, hi = _expected_price_range("GASCMDUSD")
    assert lo <= 1.5
    assert hi >= 13.0


def test_infer_factor_natgas_real_values_left_unchanged() -> None:
    lo, hi = _expected_price_range("GASCMDUSD")
    # 2022 spike to ~$9 MMBtu — real, not a 10× drift.
    assert infer_correction_factor(9.0, lo, hi) == 1.0
    # 2020 trough ~$1.70 — real.
    assert infer_correction_factor(1.70, lo, hi) == 1.0


def test_expected_price_range_high_rate_fx() -> None:
    lo, hi = _expected_price_range("EURSEK")
    # EURSEK trades around 11 — must be above the standard FX upper of 15
    assert lo >= 5.0
    assert hi >= 15.0


def test_infer_divisor_crude_oil_already_correct() -> None:
    lo, hi = _expected_price_range("BRENTCMDUSD")
    # Brent at ~$76 should be left unchanged
    assert infer_price_divisor(76.0, lo, hi) == 1.0


def test_infer_divisor_crude_oil_100x_too_large() -> None:
    lo, hi = _expected_price_range("BRENTCMDUSD")
    # Raw Dukascopy int ~7600 (= $76 × 100) → should divide by 100
    assert infer_price_divisor(7_600.0, lo, hi) == 100.0


def test_infer_divisor_high_rate_fx_already_correct() -> None:
    lo, hi = _expected_price_range("EURSEK")
    # EURSEK at 11.03 should be left unchanged
    assert infer_price_divisor(11.03, lo, hi) == 1.0


def test_infer_divisor_high_rate_fx_10000x_too_large() -> None:
    lo, hi = _expected_price_range("EURSEK")
    # Raw Dukascopy int 110314 (= 11.03 × 10000) → should divide by 10000
    assert infer_price_divisor(110_314.0, lo, hi) == 10_000.0


# ---------------------------------------------------------------------------
# infer_price_divisor
# ---------------------------------------------------------------------------


def test_infer_divisor_already_in_range_returns_1() -> None:
    lo, hi = _expected_price_range("EURUSD")
    assert infer_price_divisor(1.10, lo, hi) == 1.0


def test_infer_divisor_100x_too_large_returns_100() -> None:
    lo, hi = _expected_price_range("EURUSD")
    # EURUSD at 1.10 → 100× → 110
    assert infer_price_divisor(110.0, lo, hi) == 100.0


def test_infer_divisor_10000x_too_large_returns_10000() -> None:
    lo, hi = _expected_price_range("EURUSD")
    # EURUSD at 1.10 → 10 000× → 11000
    assert infer_price_divisor(11_000.0, lo, hi) == 10_000.0


def test_infer_divisor_jpy_100x_too_large() -> None:
    lo, hi = _expected_price_range("USDJPY")
    # USDJPY at 155 → 100× → 15 500
    assert infer_price_divisor(15_500.0, lo, hi) == 100.0


def test_infer_divisor_jpy_already_correct() -> None:
    lo, hi = _expected_price_range("USDJPY")
    assert infer_price_divisor(155.0, lo, hi) == 1.0


def test_infer_divisor_gold_100x_too_large() -> None:
    lo, hi = _expected_price_range("XAUUSD")
    # Gold at ~$5 363 → stored as 536 300 (×100); ÷1000 must NOT be chosen
    # (536.3 is outside the gold range) — ÷100 must win.
    assert infer_price_divisor(536_300.0, lo, hi) == 100.0


def test_infer_divisor_gold_already_correct() -> None:
    lo, hi = _expected_price_range("XAUUSD")
    assert infer_price_divisor(2_641.0, lo, hi) == 1.0


def test_infer_divisor_returns_1_when_no_candidate_works() -> None:
    # Price 0.00001 — nothing plausible
    lo, hi = _expected_price_range("EURUSD")
    assert infer_price_divisor(0.00001, lo, hi) == 1.0


def test_infer_divisor_returns_1_when_value_too_small_in_jpy_band() -> None:
    # infer_price_divisor only reports "divide" corrections; a value that is
    # too small (multiply needed) must yield 1.0 from this wrapper even
    # though the underlying factor-finder can fix it.
    lo, hi = _expected_price_range("USDJPY")
    assert infer_price_divisor(1.59, lo, hi) == 1.0


# ---------------------------------------------------------------------------
# infer_correction_factor (bidirectional)
# ---------------------------------------------------------------------------


def test_infer_factor_already_in_range_returns_1() -> None:
    lo, hi = _expected_price_range("USDJPY")
    assert infer_correction_factor(155.0, lo, hi) == 1.0


def test_infer_factor_jpy_100x_too_small_multiplies_by_100() -> None:
    # USDJPY stored at 1.59 (×0.01 of real ~159) — must multiply by 100.
    lo, hi = _expected_price_range("USDJPY")
    assert infer_correction_factor(1.59, lo, hi) == 100.0


def test_infer_factor_jpy_10x_too_small_multiplies_by_10() -> None:
    # USDJPY stored at 15.5 — needs ×10 to reach 155.
    lo, hi = _expected_price_range("USDJPY")
    assert infer_correction_factor(15.5, lo, hi) == 10.0


def test_infer_factor_jpy_100x_too_large_divides_by_100() -> None:
    # USDJPY stored at 15500 — needs ÷100. infer_correction_factor returns
    # the multiplicative factor 0.01.
    lo, hi = _expected_price_range("USDJPY")
    assert infer_correction_factor(15_500.0, lo, hi) == pytest.approx(0.01)


def test_infer_factor_fx_10000x_too_large_divides_by_10000() -> None:
    lo, hi = _expected_price_range("EURUSD")
    # EURUSD raw 11000 → 1.10 (factor = 1e-4).
    assert infer_correction_factor(11_000.0, lo, hi) == pytest.approx(1e-4)


def test_infer_factor_returns_1_when_value_off_by_non_power_of_ten() -> None:
    # 2.5 cannot reach the USDJPY band [50, 500] via any power of ten:
    # ×10 = 25 (still below), ×100 = 250 (in band!).
    # Confirm the in-band candidate wins.
    lo, hi = _expected_price_range("USDJPY")
    assert infer_correction_factor(2.5, lo, hi) == 100.0
    # A value too small even for ×1e5 returns 1.0 (unfixable).
    assert infer_correction_factor(1e-10, lo, hi) == 1.0


def test_infer_factor_picks_factor_closest_to_log_midpoint() -> None:
    # XAUUSD band [1000, 50000], log-midpoint ≈ 7071.  A raw value of 80
    # has two viable multiplications: ×100 → 8000 (close to mid), ×1000 →
    # 80 000 (out).  Only ×100 lands inside, so the choice is unambiguous.
    lo, hi = _expected_price_range("XAUUSD")
    assert infer_correction_factor(80.0, lo, hi) == 100.0


def test_infer_factor_handles_zero_and_negative() -> None:
    lo, hi = _expected_price_range("EURUSD")
    assert infer_correction_factor(0.0, lo, hi) == 1.0
    assert infer_correction_factor(-1.0, lo, hi) == 1.0


# ---------------------------------------------------------------------------
# New per-symbol bands
# ---------------------------------------------------------------------------


def test_expected_price_range_light_crude() -> None:
    # LIGHTCMDUSD is WTI light-sweet crude; same envelope as Brent.
    lo, hi = _expected_price_range("LIGHTCMDUSD")
    assert lo == 10.0
    assert hi == 250.0


def test_expected_price_range_palladium() -> None:
    lo, hi = _expected_price_range("XPDCMDUSD")
    # Covers both the 2003 low (~$200) and the 2022 high (~$3 400).
    assert lo <= 200.0
    assert hi >= 3_400.0


def test_expected_price_range_platinum() -> None:
    lo, hi = _expected_price_range("XPTCMDUSD")
    # Covers both the 2002 low (~$400) and the 2008 high (~$2 250).
    assert lo <= 400.0
    assert hi >= 2_250.0


def test_expected_price_range_bund_future() -> None:
    lo, hi = _expected_price_range("BUNDTREUR")
    # Euro Bund Future trades 100–180 as a price index.
    assert lo <= 100.0
    assert hi >= 180.0


def test_expected_price_range_nasdaq() -> None:
    lo, hi = _expected_price_range("USATECHIDXUSD")
    # Nasdaq Composite has ranged ~3500 (2018) to ~22000 (2025).
    assert lo <= 3_500.0
    assert hi >= 20_000.0


def test_infer_factor_nasdaq_correct_left_unchanged() -> None:
    lo, hi = _expected_price_range("USATECHIDXUSD")
    assert infer_correction_factor(15_000.0, lo, hi) == 1.0


def test_infer_factor_palladium_1000x_too_large_divides_by_1000() -> None:
    lo, hi = _expected_price_range("XPDCMDUSD")
    # Stored raw ~1.5e6 — divide by 1000 → 1500 (palladium 2024 ≈ $1 000).
    assert infer_correction_factor(1_500_000.0, lo, hi) == pytest.approx(1e-3)


# ---------------------------------------------------------------------------
# _read_zst / _write_zst round-trip
# ---------------------------------------------------------------------------


def test_read_write_zst_round_trip(tmp_path: Path) -> None:
    df = _make_daily_candle_df(1.2345)
    path = tmp_path / "test.csv.zst"
    _write_zst(df, path)
    loaded = _read_zst(path)
    assert loaded is not None
    assert len(loaded) == len(df)
    assert abs(loaded["close"].iloc[0] - 1.2345) < 1e-6


def test_read_zst_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _read_zst(tmp_path / "nonexistent.csv.zst") is None


# ---------------------------------------------------------------------------
# normalize_symbol
# ---------------------------------------------------------------------------


def test_normalize_symbol_corrects_100x_error(tmp_path: Path) -> None:
    """4-decimal FX daily CSV at 100× real price should be divided by 100."""
    symbol = "EURUSD"
    # EURUSD real price ≈ 1.10 → stored as 110.0 (100×)
    bid_path, ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 10, 110.0, 110.5)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 1
    assert result["errors"] == 0
    assert abs(_read_median_close(bid_path) - 110.0 / 100) < 0.01
    assert abs(_read_median_close(ask_path) - 110.5 / 100) < 0.01


def test_normalize_symbol_corrects_10000x_error(tmp_path: Path) -> None:
    """4-decimal FX daily CSV at 10 000× real price should be divided by 10 000."""
    symbol = "AUDNZD"
    # AUDNZD real price ≈ 1.20 → stored as 12 000
    bid_path, ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 5, 12_000.0, 12_010.0)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 1
    assert abs(_read_median_close(bid_path) - 12_000 / 10_000) < 0.01


def test_normalize_symbol_leaves_correct_price_unchanged(tmp_path: Path) -> None:
    """A correctly priced daily CSV must not be modified."""
    symbol = "GBPUSD"
    # GBPUSD real price ≈ 1.27 — already correct
    bid_path, ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 10, 1.27, 1.271)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 0
    assert abs(_read_median_close(bid_path) - 1.27) < 1e-4


def test_normalize_symbol_jpy_corrects_100x(tmp_path: Path) -> None:
    """JPY cross at 100× real price should be divided by 100."""
    symbol = "USDJPY"
    # USDJPY real price ≈ 155 → stored as 15 500
    bid_path, _ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 5, 15_500.0, 15_520.0)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 1
    assert abs(_read_median_close(bid_path) - 155.0) < 0.5


def test_normalize_symbol_jpy_corrects_100x_too_small(tmp_path: Path) -> None:
    """JPY cross stored 100× too small (exported with too-large divisor) must
    be multiplied back into the natural-units band — the symmetric inverse
    of the over-scaled case."""
    symbol = "USDJPY"
    # USDJPY real price ≈ 159 → stored as 1.59 (off by a factor of 100).
    bid_path, ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 6, 1.59, 1.595)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 1
    assert result["errors"] == 0
    assert abs(_read_median_close(bid_path) - 159.0) < 0.5
    assert abs(_read_median_close(ask_path) - 159.5) < 0.5


def test_normalize_symbol_idempotent_on_correctly_priced_data(tmp_path: Path) -> None:
    """Re-running on already-correct data is a no-op (acceptance criterion:
    a second --dry-run reports zero days needing rescale)."""
    symbol = "USDJPY"
    bid_path, _ = _make_cache_day(tmp_path, symbol, 2026, 2, 7, 158.9, 159.0)
    # First pass: nothing to fix.
    first = normalize_symbol(tmp_path / symbol, symbol)
    assert first["fixed"] == 0
    # Second pass: still nothing to fix; file untouched.
    second = normalize_symbol(tmp_path / symbol, symbol)
    assert second["fixed"] == 0
    assert abs(_read_median_close(bid_path) - 158.9) < 0.01


def test_normalize_symbol_round_trip_multiply_then_dry_run_finds_nothing(
    tmp_path: Path,
) -> None:
    """Apply a multiply correction; a follow-up dry-run must report zero days
    needing further rescale — the post-condition of the whole-cache fix."""
    symbol = "USDJPY"
    _make_cache_day(tmp_path, symbol, 2026, 2, 8, 1.59, 1.595)
    fixed = normalize_symbol(tmp_path / symbol, symbol)
    assert fixed["fixed"] == 1
    audit = normalize_symbol(tmp_path / symbol, symbol, dry_run=True)
    assert audit["fixed"] == 0


def test_normalize_symbol_jpy_leaves_correct_unchanged(tmp_path: Path) -> None:
    symbol = "AUDJPY"
    bid_path, _ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 12, 113.5, 113.6)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 0
    assert abs(_read_median_close(bid_path) - 113.5) < 0.01


def test_normalize_symbol_dry_run_does_not_modify_files(tmp_path: Path) -> None:
    """Dry-run must not write any files."""
    symbol = "EURUSD"
    bid_path, _ask_path = _make_cache_day(tmp_path, symbol, 2026, 2, 10, 110.0, 110.5)

    result = normalize_symbol(tmp_path / symbol, symbol, dry_run=True)

    assert result["fixed"] == 1
    # File must still contain the original wrong price
    assert abs(_read_median_close(bid_path) - 110.0) < 0.01


def test_normalize_symbol_handles_empty_month_dir(tmp_path: Path) -> None:
    symbol = "EURUSD"
    sym_dir = tmp_path / symbol / "2026" / "02"
    sym_dir.mkdir(parents=True)
    # No .csv.zst files

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 0
    assert result["errors"] == 0


def test_normalize_symbol_skips_corrupted_file(tmp_path: Path) -> None:
    symbol = "EURUSD"
    day_dir = tmp_path / symbol / "2026" / "02"
    day_dir.mkdir(parents=True)
    bad_path = day_dir / "10_bid.csv.zst"
    bad_path.write_bytes(b"not a valid zstd file")

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["skipped"] == 1
    assert result["errors"] == 0


def test_normalize_symbol_multiple_days(tmp_path: Path) -> None:
    """Multiple days in the same month — all wrong ones get corrected."""
    symbol = "USDCAD"
    # Day 5 wrong: 13600 (10000×) → should become 1.36
    # Day 6 wrong: 13610 (10000×) → should become 1.361
    # Day 12 correct: 1.36 → should stay
    _make_cache_day(tmp_path, symbol, 2026, 2, 5, 13_600.0, 13_620.0)
    _make_cache_day(tmp_path, symbol, 2026, 2, 6, 13_610.0, 13_630.0)
    bid_ok, _ = _make_cache_day(tmp_path, symbol, 2026, 2, 12, 1.36, 1.362)

    result = normalize_symbol(tmp_path / symbol, symbol)

    assert result["fixed"] == 2
    assert abs(_read_median_close(bid_ok) - 1.36) < 1e-4


# ---------------------------------------------------------------------------
# normalize_cache
# ---------------------------------------------------------------------------


def test_normalize_cache_processes_multiple_symbols(tmp_path: Path) -> None:
    _make_cache_day(tmp_path, "EURUSD", 2026, 2, 10, 110.0, 110.5)
    _make_cache_day(tmp_path, "GBPUSD", 2026, 2, 10, 135.0, 135.5)

    results = normalize_cache(tmp_path)

    assert results["EURUSD"]["fixed"] == 1
    assert results["GBPUSD"]["fixed"] == 1


def test_normalize_cache_symbol_filter(tmp_path: Path) -> None:
    _make_cache_day(tmp_path, "EURUSD", 2026, 2, 10, 110.0, 110.5)
    _make_cache_day(tmp_path, "GBPUSD", 2026, 2, 10, 135.0, 135.5)

    results = normalize_cache(tmp_path, ["EURUSD"])

    assert "EURUSD" in results
    assert "GBPUSD" not in results


def test_normalize_cache_warns_missing_symbol(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        results = normalize_cache(tmp_path, ["DOESNOTEXIST"])

    assert "DOESNOTEXIST" not in results
    assert any("not found" in r.message.lower() for r in caplog.records)


def test_normalize_cache_dry_run(tmp_path: Path) -> None:
    bid_path, _ = _make_cache_day(tmp_path, "EURUSD", 2026, 2, 10, 110.0, 110.5)

    results = normalize_cache(tmp_path, dry_run=True)

    assert results["EURUSD"]["fixed"] == 1
    # Original wrong price must be untouched
    assert abs(_read_median_close(bid_path) - 110.0) < 0.01


# ---------------------------------------------------------------------------
# Bands are configuration (price_bands.example.toml, --bands)
# ---------------------------------------------------------------------------


def test_the_shipped_bands_ship_in_the_package() -> None:
    from tradedesk_marketdata import normalize as nz

    path = Path(nz.__file__).with_name(nz.PRICE_BANDS_EXAMPLE)
    assert path.is_file()
    shipped = nz.load_price_bands(path)
    assert shipped == nz.load_price_bands()
    assert shipped.default == (0.3, 5.0)
    assert shipped.contains == (("IDX", (100.0, 500_000.0)),)
    assert shipped.band("usa500idxusd") == (1_000.0, 10_000.0)  # exact symbol before IDX rule


def test_custom_bands_change_what_normalize_corrects(tmp_path: Path) -> None:
    from tradedesk_marketdata import normalize as nz

    # 1.10 is a correct EURUSD price under the shipped bands, and left alone...
    bid, _ = _make_cache_day(tmp_path, "EURUSD", 2026, 2, 10, 1.10, 1.11)
    assert normalize_symbol(tmp_path / "EURUSD", "EURUSD")["fixed"] == 0
    # ...but a file that says EURUSD lives in pips makes it 10^4 too small.
    bands_file = tmp_path / "bands.toml"
    bands_file.write_text("default = [0.3, 5.0]\n[symbols]\nEURUSD = [5000.0, 30000.0]\n")
    bands = nz.load_price_bands(bands_file)

    result = normalize_symbol(tmp_path / "EURUSD", "EURUSD", bands=bands)

    assert result["fixed"] == 1
    assert _read_median_close(bid) == pytest.approx(11000.0)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[symbols]\nEURUSD = [1.0, 2.0]\n", "no default band"),
        ("default = [5.0, 1.0]\n", "0 < low < high"),
        ("default = [1.0]\n", "must be [low, high]"),
        ('default = [0.3, 5.0]\n[[contains]]\npattern = "IDX"\n', "exactly pattern and band"),
        ("default = [0.3, 5.0]\nbands = 1\n", "unknown key(s) bands"),
        ("default = [\n", "not a TOML file"),
    ],
)
def test_a_bands_file_that_breaks_the_schema_is_refused(tmp_path, body, message) -> None:
    import re

    from tradedesk_marketdata import normalize as nz

    path = tmp_path / "bands.toml"
    path.write_text(body)
    with pytest.raises(nz.PriceBandsError, match=re.escape(message)):
        nz.load_price_bands(path)


def test_normalize_cli_takes_a_bands_file(tmp_path: Path, capsys) -> None:
    from tradedesk_marketdata import normalize_cli

    bid, _ = _make_cache_day(tmp_path, "EURUSD", 2026, 2, 10, 1.10, 1.11)
    bands_file = tmp_path / "bands.toml"
    bands_file.write_text("default = [0.3, 5.0]\n[symbols]\nEURUSD = [5000.0, 30000.0]\n")

    rc = normalize_cli.main(["--cache-dir", str(tmp_path), "--bands", str(bands_file)])

    assert rc == 0
    assert _read_median_close(bid) == pytest.approx(11000.0)

    bad = tmp_path / "bad.toml"
    bad.write_text("default = [5.0, 1.0]\n")
    with pytest.raises(SystemExit) as exc:
        normalize_cli.main(["--cache-dir", str(tmp_path), "--bands", str(bad)])
    assert exc.value.code == 2
    assert "0 < low < high" in capsys.readouterr().err
