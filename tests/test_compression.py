"""Tests for Zstandard compression of daily candle cache files."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
import zstandard as zstd

import tradedesk_marketdata.export as ex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candles(n: int = 5) -> pd.DataFrame:
    base = pd.Timestamp(datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC))
    idx = pd.date_range(base, periods=n, freq="1min")
    return pd.DataFrame(
        {
            "open": [1.1000 + i * 0.0001 for i in range(n)],
            "high": [1.1010 + i * 0.0001 for i in range(n)],
            "low": [1.0990 + i * 0.0001 for i in range(n)],
            "close": [1.1005 + i * 0.0001 for i in range(n)],
            "volume": [float(i + 1) for i in range(n)],
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# _daily_candle_path
# ---------------------------------------------------------------------------


def test_daily_candle_path_uses_zst_extension_bid(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")
    assert path.suffix == ".zst"
    assert path.stem.endswith(".csv")
    assert path.name == "15_bid.csv.zst"


def test_daily_candle_path_uses_zst_extension_ask(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "ask")
    assert path.name == "15_ask.csv.zst"


# ---------------------------------------------------------------------------
# _write_daily_candles / _load_daily_candles round-trip
# ---------------------------------------------------------------------------


def test_write_and_load_round_trip(tmp_path: Path) -> None:
    candles = _make_candles(5)
    day = date(2025, 1, 15)
    path = ex._daily_candle_path(tmp_path, "EURUSD", day, "bid")

    ex._write_daily_candles(candles, path)

    assert path.exists()
    assert not path.with_suffix("").exists(), "uncompressed .csv must not exist"

    # File must be valid zstd
    dctx = zstd.ZstdDecompressor()
    raw = dctx.decompress(path.read_bytes())
    assert b"open" in raw  # CSV header present

    loaded = ex._load_daily_candles(path)
    assert loaded is not None
    assert len(loaded) == len(candles)

    for col in ("open", "high", "low", "close", "volume"):
        for orig, got in zip(candles[col], loaded[col], strict=True):
            assert orig == pytest.approx(got)


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "EURUSD" / "2025" / "00" / "15_bid.csv.zst"
    assert ex._load_daily_candles(path) is None


def test_load_returns_none_for_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv.zst"
    path.write_bytes(b"this is not zstd data")
    assert ex._load_daily_candles(path) is None
