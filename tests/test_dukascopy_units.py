"""Dukascopy units: hour iteration, URLs, the bi5 price-format probe and --probe."""

import lzma
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tradedesk_marketdata.sources.dukascopy as dk


def test_iter_hours_rounds_up_to_next_hour() -> None:
    start = datetime(2025, 1, 1, 0, 30, tzinfo=UTC)
    end_excl = datetime(2025, 1, 1, 3, 0, tzinfo=UTC)

    hours = list(dk._iter_hours(start, end_excl))
    assert hours == [
        datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 2, 0, tzinfo=UTC),
    ]


def test_export_range_probe_exits_after_first_successful_hour(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """
    Probe should stop after the first hour where comp bytes are present,
    without attempting subsequent hours.
    """

    start = datetime(2025, 7, 1, 0, 0, tzinfo=UTC)
    end_incl = datetime(2025, 7, 1, 0, 0, tzinfo=UTC)

    hours = [
        start,
        start + timedelta(hours=1),
    ]

    calls = {"download": 0}

    def fake_iter_hours(_start, _end_excl):
        yield from hours

    def fake_download(_url, *, cache_path, timeout, retries):
        calls["download"] += 1
        # first hour has data; should cause probe return
        return b"fake-comp"

    def fake_probe_price_format(_comp):
        return "float"

    # One tick record of 20 bytes: >i f f f f
    raw20 = struct.pack(">i f f f f", 0, 1.234, 1.233, 0.1, 0.2)

    def fake_read_n_tick_records(_comp, _n):
        return raw20

    monkeypatch.setattr(dk, "_iter_hours", fake_iter_hours)
    monkeypatch.setattr(dk, "_download_bi5", fake_download)
    monkeypatch.setattr(dk, "_probe_price_format", fake_probe_price_format)
    monkeypatch.setattr(dk, "_read_n_tick_records", fake_read_n_tick_records)

    dk.export_range(
        symbol="GBPSEK",
        start_utc=start,
        end_utc_inclusive=end_incl,
        out=tmp_path,
        price_divisor=1000.0,
        resample_rule="5min",
        cache_dir=None,
        probe=True,
        probe_ticks=1,
    )

    # Probe should stop after first successful hour.
    assert calls["download"] == 1

    out = capsys.readouterr().out
    assert "GBPSEK" in out
    assert "first 1 ticks" in out


def _compress_records(raw: bytes) -> bytes:
    return lzma.compress(raw)


def test_iter_hours_includes_hour_when_start_on_boundary() -> None:
    start = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    end_excl = datetime(2025, 1, 1, 3, 0, tzinfo=UTC)
    got = list(dk._iter_hours(start, end_excl))
    assert got == [
        datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 2, 0, tzinfo=UTC),
    ]


def test_iter_hours_rounds_up_to_next_hour_when_start_not_on_boundary() -> None:
    start = datetime(2025, 1, 1, 0, 30, tzinfo=UTC)
    end_excl = datetime(2025, 1, 1, 2, 0, tzinfo=UTC)
    got = list(dk._iter_hours(start, end_excl))
    assert got == [
        datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
    ]


def test_dukascopy_tick_url_month_is_zero_based() -> None:
    # June is month 6 => zero-based "05"
    t = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    url = dk._dukascopy_tick_url("EURUSD", t)
    assert url.startswith(dk.BASE_URL)
    assert "/EURUSD/2025/05/01/00h_ticks.bi5" in url


def test_probe_price_format_returns_float_for_plausible_float_prices() -> None:
    # float layout: >i f f f f  (ms, ask, bid, ask_vol, bid_vol)
    raw = struct.pack(">i f f f f", 0, 1.2345, 1.2340, 10.0, 12.0)
    comp = _compress_records(raw)
    assert dk._probe_price_format(comp) == "float"


def test_probe_price_format_returns_int_when_float_decode_is_tiny() -> None:
    # int layout (what we actually want to detect): >i i i f f
    # When these int32 bytes are interpreted as float32, they commonly become tiny values.
    raw = struct.pack(">i i i f f", 0, 100000, 99999, 10.0, 12.0)
    comp = _compress_records(raw)
    assert dk._probe_price_format(comp) == "int"


def test_read_n_tick_records_reads_exact_number_of_records() -> None:
    rec1 = struct.pack(">i f f f f", 0, 1.0, 2.0, 3.0, 4.0)
    rec2 = struct.pack(">i f f f f", 1, 1.1, 2.1, 3.1, 4.1)
    rec3 = struct.pack(">i f f f f", 2, 1.2, 2.2, 3.2, 4.2)
    raw = rec1 + rec2 + rec3
    comp = _compress_records(raw)

    out = dk._read_n_tick_records(comp, 2)
    assert out == raw[: 20 * 2]
