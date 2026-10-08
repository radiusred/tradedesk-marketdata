"""Tests for daily 1-min candle CSV caching helpers."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

import tradedesk_marketdata.export as ex


def _sample_candles(day: date, count: int = 5) -> pd.DataFrame:
    """Build sample 1-min OHLCV candles starting at midnight of `day`."""
    base = pd.Timestamp(datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC))
    idx = pd.date_range(base, periods=count, freq="1min")
    return pd.DataFrame(
        {
            "open": [1.10 + i * 0.001 for i in range(count)],
            "high": [1.11 + i * 0.001 for i in range(count)],
            "low": [1.09 + i * 0.001 for i in range(count)],
            "close": [1.105 + i * 0.001 for i in range(count)],
            "volume": [100.0 + i for i in range(count)],
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# _daily_candle_path
# ---------------------------------------------------------------------------


def test_daily_candle_path_structure_bid(tmp_path: Path) -> None:
    day = date(2025, 3, 15)
    p = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")
    # Month is zero-based: March (3) → "02"
    assert p == tmp_path / "EURUSD" / "2025" / "02" / "15_bid.csv.zst"


def test_daily_candle_path_structure_ask(tmp_path: Path) -> None:
    day = date(2025, 3, 15)
    p = ex._daily_candle_path(tmp_path, "EURUSD", day, "ask")
    assert p == tmp_path / "EURUSD" / "2025" / "02" / "15_ask.csv.zst"


def test_daily_candle_path_first_day_of_year(tmp_path: Path) -> None:
    day = date(2025, 1, 1)
    p = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")
    assert p.name == "01_bid.csv.zst"


# ---------------------------------------------------------------------------
# _write_daily_candles / _load_daily_candles round-trip
# ---------------------------------------------------------------------------


def test_write_and_load_round_trip(tmp_path: Path) -> None:
    day = date(2025, 6, 1)
    candles = _sample_candles(day, count=5)
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")

    ex._write_daily_candles(candles, path)
    assert path.exists()

    loaded = ex._load_daily_candles(path)
    assert loaded is not None
    assert len(loaded) == len(candles)
    for col in ("open", "high", "low", "close", "volume"):
        for orig, got in zip(candles[col], loaded[col], strict=True):
            assert orig == pytest.approx(got)


def test_write_and_load_preserves_utc_index(tmp_path: Path) -> None:
    day = date(2025, 6, 1)
    candles = _sample_candles(day, count=3)
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")

    ex._write_daily_candles(candles, path)
    loaded = ex._load_daily_candles(path)
    assert loaded is not None
    assert loaded.index.tz is not None  # must have timezone
    assert list(loaded.index) == list(candles.index)


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    day = date(2025, 6, 1)
    nested = tmp_path / "deep" / "nested"
    path = ex._daily_candle_path(nested, "GBPUSD", day, "ask")
    candles = _sample_candles(day, count=2)

    ex._write_daily_candles(candles, path)
    assert path.exists()


def test_write_is_atomic(tmp_path: Path) -> None:
    """No .tmp file should remain after a successful write."""
    day = date(2025, 6, 1)
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")
    ex._write_daily_candles(_sample_candles(day), path)

    tmp_files = list(tmp_path.rglob("*.tmp"))
    assert tmp_files == []


def test_write_empty_dataframe_is_loadable(tmp_path: Path) -> None:
    """Empty candle files (market-closed days) must survive a round-trip."""
    day = date(2025, 6, 7)
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")

    ex._write_daily_candles(empty, path)
    loaded = ex._load_daily_candles(path)
    assert loaded is not None
    assert loaded.empty


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent.csv.zst"
    assert ex._load_daily_candles(p) is None


def test_load_returns_none_for_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv.zst"
    p.write_bytes(b"this is not valid zstd data")
    assert ex._load_daily_candles(p) is None


# ---------------------------------------------------------------------------
# candle CSV round-trip through aggregation
# ---------------------------------------------------------------------------


def test_candles_roundtrip_through_csv_and_aggregate(tmp_path: Path) -> None:
    """Write 1-min candles, reload, aggregate to 5-min — values must be stable."""
    day = date(2025, 1, 5)
    candles = _sample_candles(day, count=10)  # 10 one-minute candles

    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")
    ex._write_daily_candles(candles, path)
    loaded = ex._load_daily_candles(path)

    assert loaded is not None
    aggregated = ex._candles_to_candles(loaded, "5min")
    assert len(aggregated) == 2  # 10 min → 2 five-minute bars
    assert aggregated.iloc[0]["open"] == pytest.approx(candles.iloc[0]["open"])
    assert aggregated.iloc[0]["volume"] == pytest.approx(sum(candles["volume"][:5]))
