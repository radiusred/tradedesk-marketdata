"""Tests for tradedesk_marketdata.scale_sentry."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import zstandard as zstd

from tradedesk_marketdata.scale_sentry import (
    check_scale_consistency,
    collect_history_medians,
)


def _write_day(cache_dir: Path, symbol: str, day: date, median: float) -> None:
    """Write a minimal bid+ask daily CSV.zst at *day* with the given median close."""
    p = cache_dir / symbol / str(day.year) / f"{day.month - 1:02d}" / f"{day.day:02d}_bid.csv.zst"
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


def test_cold_start_passes(tmp_path: Path) -> None:
    """No history → allow the write through."""
    ok, reason = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 15_720.0)
    assert ok
    assert reason is None


def test_single_neighbour_below_min_history_passes(tmp_path: Path) -> None:
    """With only one neighbour, min_history=2 trips the cold-start path."""
    _write_day(tmp_path, "USDJPY", date(2026, 5, 4), 15_710.0)
    ok, _ = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 157_200.0)
    assert ok  # not enough history to flag


def test_consistent_scale_passes(tmp_path: Path) -> None:
    for d in [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 4)]:
        _write_day(tmp_path, "USDJPY", d, 15_700.0)
    ok, reason = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 15_720.0)
    assert ok, reason


def test_10x_divergence_rejected(tmp_path: Path) -> None:
    """New day at 10× the neighbours → reject."""
    for d in [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 4)]:
        _write_day(tmp_path, "USDJPY", d, 15_700.0)
    ok, reason = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 157_200.0)
    assert not ok
    assert reason is not None
    assert "USDJPY" in reason
    assert "diverges" in reason
    assert "10" in reason  # 10× ratio mentioned


def test_100x_divergence_rejected(tmp_path: Path) -> None:
    for d in [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 4)]:
        _write_day(tmp_path, "USDJPY", d, 15_700.0)
    ok, reason = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 157.0)
    assert not ok
    assert reason is not None


def test_inverse_divergence_rejected(tmp_path: Path) -> None:
    """A new day at 1/10× the neighbours is also a discontinuity."""
    for d in [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 4)]:
        _write_day(tmp_path, "USDJPY", d, 157_000.0)
    ok, _ = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 15_700.0)
    assert not ok


def test_baseline_robust_to_one_outlier_neighbour(tmp_path: Path) -> None:
    """One off-scale neighbour shouldn't flip the baseline."""
    _write_day(tmp_path, "USDJPY", date(2026, 5, 1), 15_700.0)
    _write_day(tmp_path, "USDJPY", date(2026, 5, 2), 15_710.0)
    _write_day(tmp_path, "USDJPY", date(2026, 5, 3), 157_000.0)  # outlier
    _write_day(tmp_path, "USDJPY", date(2026, 5, 4), 15_720.0)
    # New day matches the dominant scale, the median of neighbour medians is
    # still ~15 700 not 157 000.
    ok, reason = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 15_730.0)
    assert ok, reason


def test_history_window_excludes_distant_days(tmp_path: Path) -> None:
    """A neighbour > history_days away is not consulted."""
    _write_day(tmp_path, "USDJPY", date(2026, 4, 1), 157_000.0)
    ok, _ = check_scale_consistency(
        tmp_path,
        "USDJPY",
        date(2026, 5, 5),
        15_720.0,
        history_days=7,
    )
    assert ok


def test_collect_history_medians_orders_nearest_first(tmp_path: Path) -> None:
    _write_day(tmp_path, "USDJPY", date(2026, 5, 1), 100.0)  # 4 days before
    _write_day(tmp_path, "USDJPY", date(2026, 5, 4), 200.0)  # 1 day before
    _write_day(tmp_path, "USDJPY", date(2026, 5, 7), 300.0)  # 2 days after
    medians = collect_history_medians(
        tmp_path, "USDJPY", date(2026, 5, 5), history_days=7, max_samples=2
    )
    assert medians == [200.0, 300.0]  # nearest-first, capped at max_samples


def test_invalid_new_median_passes(tmp_path: Path) -> None:
    """A non-finite or non-positive new median shouldn't be flagged."""
    for d in [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 4)]:
        _write_day(tmp_path, "USDJPY", d, 15_700.0)
    ok, _ = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), float("nan"))
    assert ok
    ok, _ = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 0.0)
    assert ok


def test_corrupt_neighbour_skipped(tmp_path: Path) -> None:
    """Corrupt neighbour file is silently skipped, others are still used."""
    _write_day(tmp_path, "USDJPY", date(2026, 5, 2), 15_700.0)
    _write_day(tmp_path, "USDJPY", date(2026, 5, 4), 15_710.0)
    bad = tmp_path / "USDJPY" / "2026" / "04" / "03_bid.csv.zst"
    bad.write_bytes(b"not a valid zstd stream")
    ok, reason = check_scale_consistency(tmp_path, "USDJPY", date(2026, 5, 5), 157_200.0)
    assert not ok, reason
