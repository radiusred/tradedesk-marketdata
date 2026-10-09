"""
Integration tests for the daily candle CSV caching layer in export_range.

These tests exercise:
  - Daily candle CSVs written + .bi5 files deleted after a gapless day
  - Daily candle CSVs NOT written when a day has 404 hours (gap prevention)
  - Early exit when all days are cached and output CSVs already exist
  - Early exit (None, None) when all days are cached and no resample is requested
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

import tradedesk_marketdata.export as ex
import tradedesk_marketdata.sources.dukascopy as dk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick(ts: datetime) -> ex.Tick:
    return ex.Tick(ts=ts, bid=1.1000, ask=1.1005, bid_vol=1.0, ask_vol=1.0)


def _make_candles(ts: datetime) -> pd.DataFrame:
    idx = pd.DatetimeIndex([ts], tz="UTC")
    return pd.DataFrame(
        {"open": [1.1000], "high": [1.1005], "low": [1.0995], "close": [1.1002], "volume": [1.0]},
        index=idx,
    )


def _patch_download_and_decode(monkeypatch, *, return_none_for_hour: datetime | None = None):
    """
    Patch _download_bi5, _probe_price_format, and _decode_ticks so that
    export_range can run without real network or LZMA data.

    If return_none_for_hour is set, that hour's download returns None (404).
    The fake downloader also writes a sentinel .bi5 file to cache so that
    _flush_day has something to delete.
    """

    def fake_download(url, *, cache_path, **kwargs):
        hour_str = url.split("/")[-1]  # e.g. "00h_ticks.bi5"
        if return_none_for_hour is not None and f"{return_none_for_hour.hour:02d}h" in hour_str:
            return None
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"fake")
        return b"fake"

    monkeypatch.setattr(dk, "_download_bi5", fake_download)
    monkeypatch.setattr(dk, "_probe_price_format", lambda *_: "float")

    def fake_decode(hour_start, _comp, *, price_format, price_divisor):
        return [_make_tick(hour_start)]

    monkeypatch.setattr(dk, "_decode_ticks", fake_decode)


# ---------------------------------------------------------------------------
# Cache lifecycle: daily candle CSVs written, .bi5 deleted
# ---------------------------------------------------------------------------


def test_daily_candle_csvs_written_and_bi5_deleted_after_complete_day(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)
    _patch_download_and_decode(monkeypatch)

    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "bid")
    ask_csv = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "ask")
    assert bid_csv.exists(), "daily bid candle CSV must be written after a complete day"
    assert ask_csv.exists(), "daily ask candle CSV must be written after a complete day"

    # The fake downloader wrote .bi5 files; _flush_day should have deleted them.
    for h in hours:
        bi5 = (
            cache_dir
            / "EURUSD"
            / str(h.year)
            / f"{h.month - 1:02d}"
            / f"{h.day:02d}"
            / f"{h.hour:02d}h_ticks.bi5"
        )
        assert not bi5.exists(), f".bi5 file should be deleted after flush: {bi5}"


def test_daily_candle_csvs_contain_data_from_all_hours(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start + timedelta(hours=i) for i in range(3)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)
    _patch_download_and_decode(monkeypatch)

    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=2),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "bid")
    candles = ex._load_daily_candles(bid_csv)
    assert candles is not None
    # One fake tick per hour → one 1-min candle per hour.
    assert len(candles) == len(hours)


# ---------------------------------------------------------------------------
# Gap prevention: a 404 hour leaves a *below-threshold* day uncommitted
# (the gap may still be transient, so the day is retried on the next run).
# Aged-gap partial-commit behaviour is covered in test_export_partial_commit.py.
# ---------------------------------------------------------------------------


def test_daily_candle_csvs_not_written_when_recent_day_has_404_hour(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)
    # Hour 00 returns 404 (None).
    _patch_download_and_decode(monkeypatch, return_none_for_hour=start)

    # A very large age threshold makes the day "too young" to partial-commit,
    # so a 404 hour leaves the day uncommitted (the original retry behaviour).
    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
        commit_partial_after_days=100_000,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "bid")
    ask_csv = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "ask")
    assert not bid_csv.exists(), "bid candle CSV must NOT be written for a below-threshold 404 day"
    assert not ask_csv.exists(), "ask candle CSV must NOT be written for a below-threshold 404 day"


# ---------------------------------------------------------------------------
# Early exit: all days cached + output CSVs exist
# ---------------------------------------------------------------------------


def test_early_exit_returns_existing_csvs_when_all_cached(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]

    # Pre-create the daily candle CSVs so the day appears fully cached.
    _make_and_write_candle_cache(cache_dir, "EURUSD", start, start.date())

    # Pre-create output CSVs.
    out_dir.mkdir(parents=True, exist_ok=True)
    bid_csv = out_dir / "EURUSD_1MIN_bid.csv"
    ask_csv = out_dir / "EURUSD_1MIN_ask.csv"
    bid_csv.write_text("existing")
    ask_csv.write_text("existing")

    download_calls = {"n": 0}

    def fake_download(*_, **__):
        download_calls["n"] += 1
        return b"should_not_be_called"

    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "_download_bi5", fake_download)

    result = dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
    )

    assert result == (bid_csv, ask_csv)
    assert download_calls["n"] == 0, "early exit must skip all downloads"


def test_early_exit_still_processes_when_only_one_output_csv_missing(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]

    _make_and_write_candle_cache(cache_dir, "EURUSD", start, start.date())

    # Only bid CSV exists; ask is missing → must not early-exit.
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "EURUSD_1MIN_bid.csv").write_text("existing")

    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)

    bid_csv, ask_csv = dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
    )

    assert ask_csv is not None and ask_csv.exists()


# ---------------------------------------------------------------------------
# Early exit: no resample requested + all days cached
# ---------------------------------------------------------------------------


def test_early_exit_returns_none_tuple_when_no_resample_and_all_cached(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]

    _make_and_write_candle_cache(cache_dir, "EURUSD", start, start.date())

    download_calls = {"n": 0}

    def fake_download(*_, **__):
        download_calls["n"] += 1
        return b"should_not_be_called"

    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "_download_bi5", fake_download)

    result = dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=tmp_path / "out",
        price_divisor=1.0,
        resample_rule=None,
        cache_dir=cache_dir,
        probe=False,
    )

    assert result == (None, None)
    assert download_calls["n"] == 0


# ---------------------------------------------------------------------------
# Self-heal: re-export removes a leftover .bi5 dir for a fully-cached day
# ---------------------------------------------------------------------------


def test_reexport_self_heals_leftover_bi5_dir_for_cached_day(monkeypatch, tmp_path):
    """A fully-cached day whose .bi5 dir was left behind (export interrupted
    before bi5 deletion) is self-healed on re-export — the bi5 dir is removed
    and the daily-candle CSVs are left untouched. Before this fix the day was
    skipped as fully-cached and the leftover .bi5 dir was permanent, tripping
    the consumer's _check_old_format guard."""
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    day = start.date()
    hours = [start, start + timedelta(hours=1)]

    # Day is fully cached: both daily-candle CSVs already exist.
    _make_and_write_candle_cache(cache_dir, "EURUSD", start, day)
    bid_csv = ex._daily_candle_path(cache_dir, "EURUSD", day, "bid")
    ask_csv = ex._daily_candle_path(cache_dir, "EURUSD", day, "ask")
    bid_bytes_before = bid_csv.read_bytes()
    ask_bytes_before = ask_csv.read_bytes()

    # Leftover .bi5 day-dir from a run interrupted before deleting its bi5 files.
    day_dir = cache_dir / "EURUSD" / f"{start.year}" / f"{start.month - 1:02d}" / f"{start.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "00h_ticks.bi5").write_bytes(b"leftover")
    (day_dir / "01h_ticks.bi5").write_bytes(b"leftover")

    # A re-export of a fully-cached day must not download anything.
    def fake_download(*_, **__):
        raise AssertionError("re-export of a fully-cached day must not download")

    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "_download_bi5", fake_download)

    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
    )

    # The redundant bi5 day-dir is gone...
    assert not day_dir.exists(), "leftover bi5 day-dir must be removed on re-export"
    # ...and the candle CSVs are byte-for-byte untouched.
    assert bid_csv.read_bytes() == bid_bytes_before
    assert ask_csv.read_bytes() == ask_bytes_before


# ---------------------------------------------------------------------------
# Output CSV filenames include _bid / _ask
# ---------------------------------------------------------------------------


def test_output_csv_filenames_include_bid_and_ask_suffixes(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)
    _patch_download_and_decode(monkeypatch)

    bid_csv, ask_csv = dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule="1min",
        cache_dir=cache_dir,
        probe=False,
    )

    assert bid_csv is not None and "_bid" in bid_csv.name
    assert ask_csv is not None and "_ask" in ask_csv.name
    assert bid_csv.exists()
    assert ask_csv.exists()


# ---------------------------------------------------------------------------
# No resample: returns (None, None), no output files written
# ---------------------------------------------------------------------------


def test_no_resample_returns_none_tuple_and_writes_no_csv(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    hours = [start, start + timedelta(hours=1)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)
    _patch_download_and_decode(monkeypatch)

    result = dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start + timedelta(hours=1),
        out=out_dir,
        price_divisor=1.0,
        resample_rule=None,
        cache_dir=cache_dir,
        probe=False,
    )

    assert result == (None, None)
    assert not out_dir.exists() or not any(out_dir.iterdir())


# ---------------------------------------------------------------------------
# Helpers for test setup
# ---------------------------------------------------------------------------


def _make_and_write_candle_cache(cache_dir: Path, symbol: str, ts: datetime, day: date) -> None:
    """Write minimal bid+ask candle cache files for a day."""

    candles = _make_candles(ts)
    bid_path = ex._daily_candle_path(cache_dir, symbol, day, "bid")
    ask_path = ex._daily_candle_path(cache_dir, symbol, day, "ask")
    ex._write_daily_candles(candles, bid_path)
    ex._write_daily_candles(candles, ask_path)
