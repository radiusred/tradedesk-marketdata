import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tradedesk_marketdata.export as ex


def test_symbol_normalise_strips_separators_and_uppercases() -> None:
    assert ex._symbol_normalise("usa500.idx/usd") == "USA500IDXUSD"
    assert ex._symbol_normalise(" EURUSD ") == "EURUSD"


def test_iter_hours_rounds_up_to_next_hour() -> None:
    start = datetime(2025, 1, 1, 0, 30, tzinfo=UTC)
    end_excl = datetime(2025, 1, 1, 3, 0, tzinfo=UTC)

    hours = list(ex._iter_hours(start, end_excl))
    assert hours == [
        datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 2, 0, tzinfo=UTC),
    ]


def test_dukascopy_tick_url_month_is_zero_based() -> None:
    # June is month 6 => zero-based "05"
    t = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    url = ex._dukascopy_tick_url("EURUSD", t)
    assert "/2025/05/01/00h_ticks.bi5" in url


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

    monkeypatch.setattr(ex, "_iter_hours", fake_iter_hours)
    monkeypatch.setattr(ex, "_download_bi5", fake_download)
    monkeypatch.setattr(ex, "_probe_price_format", fake_probe_price_format)
    monkeypatch.setattr(ex, "_read_n_tick_records", fake_read_n_tick_records)

    ex.export_range(
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
