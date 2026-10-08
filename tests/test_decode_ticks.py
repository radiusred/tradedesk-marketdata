import lzma
import struct
from datetime import UTC, datetime

import pytest

import tradedesk_marketdata.export as ex


def _bi5_float_records(*rows: tuple[int, float, float, float, float]) -> bytes:
    """
    Encode rows to bi5 raw bytes in the 'float' layout:
      > i f f f f
    """
    pack = struct.Struct(">i f f f f").pack
    raw = b"".join(pack(*r) for r in rows)
    return lzma.compress(raw)


def _bi5_int_records(*rows: tuple[int, int, int, float, float]) -> bytes:
    """
    Encode rows to bi5 raw bytes in the 'int' layout:
      > i i i f f
    """
    pack = struct.Struct(">i i i f f").pack
    raw = b"".join(pack(*r) for r in rows)
    return lzma.compress(raw)


def test_decode_ticks_float_decodes_values_and_timestamps() -> None:
    hour_start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    comp = _bi5_float_records(
        (0, 1.2345, 1.2340, 10.0, 12.0),
        (500, 1.2350, 1.2346, 11.0, 13.0),
    )

    ticks = ex._decode_ticks(
        hour_start,
        comp,
        price_format="float",
        price_divisor=1.0,
    )

    assert len(ticks) == 2

    t0 = ticks[0]
    assert t0.ts == hour_start
    assert t0.ask == pytest.approx(1.2345)
    assert t0.bid == pytest.approx(1.2340)
    assert t0.ask_vol == pytest.approx(10.0)
    assert t0.bid_vol == pytest.approx(12.0)

    t1 = ticks[1]
    assert t1.ts == hour_start.replace(microsecond=500_000)
    assert (t1.ts - hour_start).total_seconds() == pytest.approx(0.5)
    assert t1.ask == pytest.approx(1.2350)
    assert t1.bid == pytest.approx(1.2346)
    assert t1.ask_vol == pytest.approx(11.0)
    assert t1.bid_vol == pytest.approx(13.0)


def test_decode_ticks_int_applies_divisor_scaling() -> None:
    hour_start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    # bid/ask are integers; divisor converts them to floats
    comp = _bi5_int_records(
        (0, 123450, 123400, 10.0, 12.0),
        (250, 123500, 123460, 11.0, 13.0),
    )

    ticks = ex._decode_ticks(
        hour_start,
        comp,
        price_format="int",
        price_divisor=100000.0,
    )

    assert len(ticks) == 2

    t0 = ticks[0]
    assert t0.ts == hour_start
    assert t0.ask == pytest.approx(1.23450)
    assert t0.bid == pytest.approx(1.23400)
    assert t0.ask_vol == pytest.approx(10.0)
    assert t0.bid_vol == pytest.approx(12.0)

    t1 = ticks[1]
    assert (t1.ts - hour_start).total_seconds() == pytest.approx(0.25)
    assert t1.ask == pytest.approx(1.23500)
    assert t1.bid == pytest.approx(1.23460)
    assert t1.ask_vol == pytest.approx(11.0)
    assert t1.bid_vol == pytest.approx(13.0)


def test_decode_ticks_raises_on_non_multiple_of_20_payload() -> None:
    hour_start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    # Decompresses to 21 bytes => invalid (not multiple of 20)
    comp = lzma.compress(b"x" * 21)

    with pytest.raises(ValueError, match="not multiple of 20"):
        ex._decode_ticks(
            hour_start,
            comp,
            price_format="float",
            price_divisor=1.0,
        )


def test_decode_ticks_invalid_price_format_raises() -> None:
    hour_start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    comp = _bi5_float_records((0, 1.0, 1.0, 1.0, 1.0))

    with pytest.raises(ValueError, match="price_format must be"):
        ex._decode_ticks(
            hour_start,
            comp,
            price_format="nope",
            price_divisor=1.0,
        )
