from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import tradedesk_marketdata.export as ex


def test_symbol_normalise_empty_raises() -> None:
    with pytest.raises(ValueError, match="Empty symbol"):
        ex._symbol_normalise("   ")


def test_symbol_normalise_removes_separators_and_uppercases() -> None:
    assert ex._symbol_normalise("usa500.idx/usd") == "USA500IDXUSD"
    assert ex._symbol_normalise(" EURUSD ") == "EURUSD"


def test_ticks_to_candles_empty_returns_empty_frame_with_columns() -> None:
    df = ex._ticks_to_candles([], resample_rule="1min", price_side="bid")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.empty


def test_ticks_to_candles_bid_ohlcv_resample() -> None:
    base = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    ticks = [
        ex.Tick(
            ts=base + timedelta(seconds=10),
            bid=100.0,
            ask=101.0,
            bid_vol=1.0,
            ask_vol=2.0,
        ),
        ex.Tick(
            ts=base + timedelta(seconds=50),
            bid=102.0,
            ask=103.0,
            bid_vol=3.0,
            ask_vol=4.0,
        ),
        ex.Tick(
            ts=base + timedelta(minutes=1, seconds=5),
            bid=101.0,
            ask=102.0,
            bid_vol=5.0,
            ask_vol=6.0,
        ),
    ]

    df = ex._ticks_to_candles(ticks, resample_rule="1min", price_side="bid")

    # Minute 00:00
    first = df.loc[pd.Timestamp("2025-01-01T00:00:00Z")]
    assert first["open"] == 100.0
    assert first["high"] == 102.0
    assert first["low"] == 100.0
    assert first["close"] == 102.0
    assert first["volume"] == 1.0 + 3.0

    # Minute 00:01
    second = df.loc[pd.Timestamp("2025-01-01T00:01:00Z")]
    assert second["open"] == 101.0
    assert second["high"] == 101.0
    assert second["low"] == 101.0
    assert second["close"] == 101.0
    assert second["volume"] == 5.0


def test_ticks_to_candles_mid_price_and_mid_volume() -> None:
    base = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    ticks = [
        ex.Tick(ts=base + timedelta(seconds=1), bid=100.0, ask=102.0, bid_vol=2.0, ask_vol=4.0),
        ex.Tick(ts=base + timedelta(seconds=2), bid=101.0, ask=103.0, bid_vol=6.0, ask_vol=8.0),
    ]

    df = ex._ticks_to_candles(ticks, resample_rule="1min", price_side="mid")
    row = df.iloc[0]

    # mid prices are (bid+ask)/2: 101.0 then 102.0
    assert row["open"] == 101.0
    assert row["high"] == 102.0
    assert row["low"] == 101.0
    assert row["close"] == 102.0

    # mid volume is (bid_vol+ask_vol)/2: 3.0 then 7.0 => 10.0
    assert row["volume"] == (2.0 + 4.0) / 2.0 + (6.0 + 8.0) / 2.0


def test_ticks_to_candles_invalid_price_side_raises() -> None:
    base = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    ticks = [ex.Tick(ts=base, bid=1.0, ask=2.0, bid_vol=1.0, ask_vol=1.0)]

    with pytest.raises(ValueError, match="price_side must be one of"):
        ex._ticks_to_candles(ticks, resample_rule="1min", price_side="nope")


# ---------------------------------------------------------------------------
# _candles_to_candles
# ---------------------------------------------------------------------------


def test_candles_to_candles_aggregates_ohlcv_correctly() -> None:
    # Five consecutive 1-min candles aggregated to a single 5-min bar.
    base = pd.Timestamp("2025-01-01T00:00:00", tz="UTC")
    idx = pd.date_range(base, periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0, 4.0, 5.0],
            "high": [1.9, 2.9, 3.9, 4.9, 5.9],
            "low": [0.1, 1.1, 2.1, 3.1, 4.1],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5],
            "volume": [10.0, 20.0, 30.0, 40.0, 50.0],
        },
        index=idx,
    )

    out = ex._candles_to_candles(df, "5min")

    assert len(out) == 1
    row = out.iloc[0]
    assert row["open"] == 1.0  # first open
    assert row["high"] == 5.9  # max high
    assert row["low"] == 0.1  # min low
    assert row["close"] == 5.5  # last close
    assert row["volume"] == 150.0  # sum


def test_candles_to_candles_empty_returns_empty() -> None:
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = ex._candles_to_candles(df, "5min")
    assert out.empty


# ---------------------------------------------------------------------------
# _write_daily_candles / _load_daily_candles round-trip
# ---------------------------------------------------------------------------


def test_write_and_load_daily_candles_round_trip(tmp_path: Path) -> None:
    import pandas as pd

    base = pd.Timestamp(datetime(2025, 6, 15, 12, 0, tzinfo=UTC))
    idx = pd.date_range(base, periods=5, freq="1min")
    original = pd.DataFrame(
        {
            "open": [1.1 + i * 0.001 for i in range(5)],
            "high": [1.11 + i * 0.001 for i in range(5)],
            "low": [1.09 + i * 0.001 for i in range(5)],
            "close": [1.105 + i * 0.001 for i in range(5)],
            "volume": [float(i + 1) for i in range(5)],
        },
        index=idx,
    )
    path = ex._daily_candle_path(tmp_path, "EURUSD", datetime(2025, 6, 15).date(), "bid")

    ex._write_daily_candles(original, path)
    assert path.exists()

    loaded = ex._load_daily_candles(path)
    assert loaded is not None
    assert len(loaded) == len(original)

    for col in ("open", "high", "low", "close", "volume"):
        for orig, got in zip(original[col], loaded[col], strict=True):
            assert abs(orig - got) < 1e-6


def test_load_daily_candles_returns_none_for_missing_file(tmp_path: Path) -> None:
    result = ex._load_daily_candles(tmp_path / "nonexistent.csv.zst")
    assert result is None


def test_write_daily_candles_is_atomic(tmp_path: Path) -> None:
    import pandas as pd

    base = pd.Timestamp(datetime(2025, 1, 1, tzinfo=UTC))
    candles = pd.DataFrame(
        {"open": [1.0], "high": [1.01], "low": [0.99], "close": [1.005], "volume": [1.0]},
        index=pd.DatetimeIndex([base], tz="UTC"),
    )
    path = ex._daily_candle_path(tmp_path, "EURUSD", datetime(2025, 1, 1).date(), "bid")

    ex._write_daily_candles(candles, path)

    tmp = path.with_suffix(path.suffix + ".tmp")
    assert path.exists()
    assert not tmp.exists()
