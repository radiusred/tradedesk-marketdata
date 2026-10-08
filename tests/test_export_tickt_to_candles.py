from datetime import UTC, datetime

import pandas as pd
import pytest

import tradedesk_marketdata.export as ex


def _make_1min_df(rows: list[tuple]) -> pd.DataFrame:
    """Helper: build a 1-min candle DataFrame from (timestamp, o, h, l, c, vol) tuples."""
    data = [
        {"open": o, "high": h, "low": low, "close": c, "volume": v} for _, o, h, low, c, v in rows
    ]
    idx = pd.DatetimeIndex([r[0] for r in rows], tz="UTC")
    return pd.DataFrame(data, index=idx)


def test_ticks_to_candles_basic_ohlc_and_volume() -> None:
    # Two ticks in the same 5-min bucket, one in the next
    ticks = [
        ex.Tick(
            ts=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
            bid=1.0,
            ask=1.2,
            bid_vol=2.0,
            ask_vol=3.0,
        ),
        ex.Tick(
            ts=datetime(2025, 1, 1, 0, 4, 59, tzinfo=UTC),
            bid=1.1,
            ask=1.3,
            bid_vol=5.0,
            ask_vol=7.0,
        ),
        ex.Tick(
            ts=datetime(2025, 1, 1, 0, 5, 0, tzinfo=UTC),
            bid=0.9,
            ask=1.0,
            bid_vol=11.0,
            ask_vol=13.0,
        ),
    ]

    df = ex._ticks_to_candles(ticks, resample_rule="5min", price_side="bid")

    # Two buckets: 00:00 and 00:05
    assert len(df) == 2

    first = df.iloc[0]
    assert first["open"] == 1.0
    assert first["high"] == 1.1
    assert first["low"] == 1.0
    assert first["close"] == 1.1
    assert first["volume"] == 2.0 + 5.0  # bid_vol

    second = df.iloc[1]
    assert second["open"] == 0.9
    assert second["high"] == 0.9
    assert second["low"] == 0.9
    assert second["close"] == 0.9
    assert second["volume"] == 11.0


def test_candles_to_candles_aggregates_ohlcv() -> None:
    # Three 1-min candles that span a single 5-min bucket (00:00)
    rows = [
        (datetime(2025, 1, 1, 0, 0, tzinfo=UTC), 1.0, 1.5, 0.9, 1.2, 100.0),
        (datetime(2025, 1, 1, 0, 1, tzinfo=UTC), 1.2, 1.8, 1.1, 1.7, 200.0),
        (datetime(2025, 1, 1, 0, 2, tzinfo=UTC), 1.7, 2.0, 1.4, 1.9, 150.0),
    ]
    df = _make_1min_df(rows)
    out = ex._candles_to_candles(df, "5min")

    assert len(out) == 1
    row = out.iloc[0]
    assert row["open"] == pytest.approx(1.0)  # first open
    assert row["high"] == pytest.approx(2.0)  # max high
    assert row["low"] == pytest.approx(0.9)  # min low
    assert row["close"] == pytest.approx(1.9)  # last close
    assert row["volume"] == pytest.approx(450.0)  # sum


def test_candles_to_candles_two_buckets() -> None:
    rows = [
        (datetime(2025, 1, 1, 0, 0, tzinfo=UTC), 1.0, 1.1, 0.9, 1.05, 10.0),
        (datetime(2025, 1, 1, 0, 5, tzinfo=UTC), 2.0, 2.2, 1.8, 2.1, 20.0),
    ]
    df = _make_1min_df(rows)
    out = ex._candles_to_candles(df, "5min")

    assert len(out) == 2
    assert out.iloc[0]["open"] == pytest.approx(1.0)
    assert out.iloc[1]["open"] == pytest.approx(2.0)


def test_candles_to_candles_empty_passthrough() -> None:
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = ex._candles_to_candles(df, "5min")
    assert out.empty


def test_candles_to_candles_1min_is_passthrough() -> None:
    # Aggregating 1-min → 1-min should be a no-op
    rows = [
        (datetime(2025, 1, 1, 0, 0, tzinfo=UTC), 1.0, 1.1, 0.9, 1.05, 10.0),
        (datetime(2025, 1, 1, 0, 1, tzinfo=UTC), 1.05, 1.2, 1.0, 1.15, 15.0),
    ]
    df = _make_1min_df(rows)
    out = ex._candles_to_candles(df, "1min")

    assert len(out) == 2
    assert out.iloc[0]["open"] == pytest.approx(1.0)
    assert out.iloc[1]["open"] == pytest.approx(1.05)
    assert out.iloc[0]["volume"] == pytest.approx(10.0)
    assert out.iloc[1]["volume"] == pytest.approx(15.0)


def test_ticks_to_candles_mid_price_and_mid_volume() -> None:
    ticks = [
        ex.Tick(
            ts=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            bid=1.0,
            ask=1.2,
            bid_vol=2.0,
            ask_vol=6.0,
        ),
        ex.Tick(
            ts=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
            bid=1.1,
            ask=1.3,
            bid_vol=4.0,
            ask_vol=10.0,
        ),
    ]

    df = ex._ticks_to_candles(ticks, resample_rule="5min", price_side="mid")
    row = df.iloc[0]

    # mid prices: (bid+ask)/2 => 1.1 then 1.2
    assert row["open"] == pytest.approx(1.1)
    assert row["close"] == pytest.approx(1.2)
    assert row["high"] == pytest.approx(1.2)
    assert row["low"] == pytest.approx(1.1)

    assert row["volume"] == pytest.approx(((2.0 + 6.0) / 2) + ((4.0 + 10.0) / 2))
