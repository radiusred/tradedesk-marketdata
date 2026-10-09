"""
HistData exports through the framework: month skipping and idempotency, empty
and partial days, provenance, first-committed-wins, the scale sentry and
cancellation. A stub stands in for the month download throughout. That a
HistData day file is byte-identical to any other source's for the same ticks
is the conformance suite's job (test_source_conformance.py).
"""

import json
import os
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from histdata_fixtures import FakeFetcher, est_line, month_zip, weekday_lines, zips_by_month

import tradedesk_marketdata.export as ex
from tradedesk_marketdata.cancel import cancellation
from tradedesk_marketdata.sources import histdata as hd

SYMBOL = "USA500IDXUSD"


@pytest.fixture(autouse=True)
def _no_pause_and_clear_cancellation(monkeypatch):
    monkeypatch.setattr(hd, "REQUEST_INTERVAL", 0.0)
    cancellation.clear()
    yield
    cancellation.clear()


def _run(cache_dir, first: date, last: date, **kwargs):
    return hd.export_range_histdata(
        symbol=kwargs.pop("symbol", SYMBOL),
        start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
        end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
        out=kwargs.pop("out", cache_dir.parent / "out"),
        resample_rule=kwargs.pop("resample_rule", None),
        cache_dir=cache_dir,
        **kwargs,
    )


def _committed(cache_dir, day: date, symbol: str = SYMBOL) -> bool:
    return ex._day_is_committed(cache_dir, symbol, day)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def jan_2015(monkeypatch):
    """SPXUSD-like ticks on weekdays from 2014-12-01 to 2015-02-27, as month zips."""
    fetch = FakeFetcher(zips_by_month(weekday_lines(date(2014, 12, 1), date(2015, 2, 27))))
    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    return fetch


# ---------------------------------------------------------------------------
# Months: listing, skipping, idempotency
# ---------------------------------------------------------------------------


def test_month_run_commits_every_day_including_weekends(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    _run(cache, date(2015, 1, 1), date(2015, 1, 31))

    # Jan 1 starts at 19:00 EST on Dec 31, so December's file is needed too.
    assert jan_2015.calls == [(2014, 12), (2015, 1)]
    assert all(_committed(cache, d) for d in _days(date(2015, 1, 1), date(2015, 1, 31)))
    # Saturday 2015-01-31 is an empty day: header only, like a Dukascopy weekend.
    sat = ex._load_daily_candles(ex._daily_candle_path(cache, SYMBOL, date(2015, 1, 31), "bid"))
    assert sat is not None and sat.empty
    # Under the cache symbol, never HistData's name.
    assert not (cache / "SPXUSD").exists()


def test_rerun_of_a_committed_month_makes_no_request(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    _run(cache, date(2015, 1, 1), date(2015, 1, 31))
    jan_2015.calls.clear()

    _run(cache, date(2015, 1, 1), date(2015, 1, 31))

    assert jan_2015.calls == []


def test_settled_month_zip_is_reused_then_dropped_once_its_days_are_committed(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    _run(cache, date(2015, 1, 1), date(2015, 1, 31))
    jan_zip = hd._zip_path(cache, SYMBOL, (2015, 1))
    assert jan_zip.exists()  # 2015-02-01 (in January's file) is not committed yet
    jan_2015.calls.clear()

    _run(cache, date(2015, 2, 1), date(2015, 2, 28))

    assert jan_2015.calls == [(2015, 2)]  # January came from the cached zip
    assert not jan_zip.exists()  # every day January's file covers is now committed


def test_corrupt_cached_zip_is_fetched_again(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    zpath = hd._zip_path(cache, SYMBOL, (2015, 1))
    zpath.parent.mkdir(parents=True)
    zpath.write_bytes(b"PK\x03\x04 truncated")

    _run(cache, date(2015, 1, 5), date(2015, 1, 9))

    assert jan_2015.calls == [(2015, 1)]
    assert all(_committed(cache, d) for d in _days(date(2015, 1, 5), date(2015, 1, 9)))


def test_zip_cached_before_its_month_settled_is_fetched_again(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    zpath = hd._zip_path(cache, SYMBOL, (2015, 1))
    zpath.parent.mkdir(parents=True)
    zpath.write_bytes(month_zip(["20150105 100000000,1.0,1.0,0"]))  # a partial early copy
    early = datetime(2015, 1, 20, tzinfo=UTC).timestamp()
    os.utime(zpath, (early, early))

    _run(cache, date(2015, 1, 5), date(2015, 1, 9))

    assert jan_2015.calls == [(2015, 1)]
    bid = ex._load_daily_candles(ex._daily_candle_path(cache, SYMBOL, date(2015, 1, 9), "bid"))
    assert bid is not None and len(bid) == 44  # the full file, not the early copy


def test_months_before_the_first_histdata_month_are_skipped_without_a_request(
    monkeypatch, tmp_path, caplog
):
    fetch = FakeFetcher(zips_by_month(weekday_lines(date(2010, 11, 1), date(2010, 11, 30))))
    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _run(cache, date(2010, 9, 1), date(2010, 11, 30))

    assert fetch.calls == [(2010, 11)]
    assert "2010-08 precedes HistData's first SPXUSD month (2010-11); skipping" in caplog.text
    assert not any(_committed(cache, d) for d in _days(date(2010, 9, 1), date(2010, 10, 31)))
    assert _committed(cache, date(2010, 11, 1)) and _committed(cache, date(2010, 11, 30))


def test_unmapped_symbol_is_refused_before_any_request(monkeypatch, tmp_path):
    fetch = FakeFetcher({})
    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    with pytest.raises(hd.UnmappedSymbolError):
        _run(tmp_path / "cache", date(2015, 1, 1), date(2015, 1, 2), symbol="BTCUSD")
    assert fetch.calls == []


# ---------------------------------------------------------------------------
# Empty, partial and uncommitted days
# ---------------------------------------------------------------------------


def test_running_month_commits_only_days_the_data_runs_past(monkeypatch, tmp_path):
    # A month that has not settled yet: HistData's file may stop mid-month.
    lines = weekday_lines(date(2015, 1, 5), date(2015, 1, 14))  # data ends Wed 14th
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips_by_month(lines)))
    cache = tmp_path / "cache"

    _run(cache, date(2015, 1, 5), date(2015, 1, 20), commit_partial_after_days=100_000)

    assert all(_committed(cache, d) for d in _days(date(2015, 1, 5), date(2015, 1, 13)))
    # The last day with ticks and the days after it may still fill in: left for retry.
    assert not any(_committed(cache, d) for d in _days(date(2015, 1, 14), date(2015, 1, 20)))
    # ...and a running month's zip is not kept.
    assert not hd._zip_path(cache, SYMBOL, (2015, 1)).exists()


def test_holiday_inside_a_running_month_is_committed_as_empty(monkeypatch, tmp_path):
    lines = [
        line
        for line in weekday_lines(date(2015, 1, 5), date(2015, 1, 14))
        if not line.startswith(("20150106 19", "20150106 2", "20150107"))  # no ticks on Jan 7
    ]
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips_by_month(lines)))
    cache = tmp_path / "cache"

    _run(cache, date(2015, 1, 5), date(2015, 1, 9), commit_partial_after_days=100_000)

    hol = ex._load_daily_candles(ex._daily_candle_path(cache, SYMBOL, date(2015, 1, 7), "bid"))
    assert hol is not None and hol.empty


def test_days_before_the_first_tick_are_not_committed(monkeypatch, tmp_path):
    lines = weekday_lines(date(2015, 1, 14), date(2015, 1, 30))  # history starts mid-month
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips_by_month(lines)))
    cache = tmp_path / "cache"

    _run(cache, date(2015, 1, 2), date(2015, 1, 31))

    assert not any(_committed(cache, d) for d in _days(date(2015, 1, 2), date(2015, 1, 13)))
    assert all(_committed(cache, d) for d in _days(date(2015, 1, 14), date(2015, 1, 31)))


def test_failed_month_leaves_its_days_uncommitted_and_is_reported(monkeypatch, tmp_path, caplog):
    zips = zips_by_month(weekday_lines(date(2014, 12, 1), date(2015, 2, 27)))
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips, fail={(2015, 1)}))
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _run(cache, date(2014, 12, 29), date(2015, 2, 3))

    assert _committed(cache, date(2014, 12, 30))
    assert not any(_committed(cache, d) for d in _days(date(2015, 1, 1), date(2015, 2, 1)))
    assert _committed(cache, date(2015, 2, 2))
    assert "1 HistData month(s) failed" in caplog.text and "2015-01; re-run" in caplog.text
    assert "failed=1" in caplog.text


def test_month_histdata_lacks_is_retried_until_settled_then_partial_committed(
    monkeypatch, tmp_path
):
    # Mon 2015-06-01 00:00-05:00 UTC is in May's file, which HistData lacks here;
    # the rest of the day is in June's.
    zips = {
        (2015, 6): month_zip(
            [est_line(datetime(2015, 6, 1, 10, tzinfo=UTC), "2050.000000", "2050.500000")]
        )
    }
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips))
    cache = tmp_path / "cache"

    _run(cache, date(2015, 6, 1), date(2015, 6, 1), commit_partial_after_days=100_000)
    assert not _committed(cache, date(2015, 6, 1))  # May may still appear

    _run(cache, date(2015, 6, 1), date(2015, 6, 1))
    assert _committed(cache, date(2015, 6, 1))
    [partial] = _jsonl(ex._partial_day_manifest_path(cache, SYMBOL))
    assert partial["day"] == "2015-06-01"
    assert partial["missing_hours"] == [0, 1, 2, 3, 4]
    assert partial["gap_reason"] == "histdata_month_unavailable"


# ---------------------------------------------------------------------------
# Provenance, precedence, scale sentry
# ---------------------------------------------------------------------------


def test_each_committed_day_records_histdata_provenance(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    _run(cache, date(2015, 1, 1), date(2015, 1, 2))

    recs = {r["day"]: r for r in _jsonl(ex._source_manifest_path(cache, SYMBOL))}
    assert set(recs) == {"2015-01-01", "2015-01-02"}
    assert recs["2015-01-02"]["source"] == "histdata"
    assert recs["2015-01-02"]["scale_factor"] == 1.0
    assert recs["2015-01-02"]["source_unit"] == "HISTDATA_COM_ASCII_SPXUSD_T201501.zip"
    assert recs["2015-01-01"]["source_unit"] == (
        "HISTDATA_COM_ASCII_SPXUSD_T201412.zip+HISTDATA_COM_ASCII_SPXUSD_T201501.zip"
    )
    assert datetime.fromisoformat(recs["2015-01-01"]["committed_at"]).tzinfo is not None


def test_fx_provenance_records_the_symbol_scale(monkeypatch, tmp_path):
    lines = [
        est_line(datetime(2015, 1, 6, 10, tzinfo=UTC), "1.103660", "1.103700"),
        est_line(datetime(2015, 1, 8, 10, tzinfo=UTC), "1.103660", "1.103700"),
    ]
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips_by_month(lines)))
    cache = tmp_path / "cache"

    _run(cache, date(2015, 1, 6), date(2015, 1, 6), symbol="EURUSD")

    [rec] = _jsonl(ex._source_manifest_path(cache, "EURUSD"))
    assert rec["scale_factor"] == 1e4
    bid = ex._load_daily_candles(ex._daily_candle_path(cache, "EURUSD", date(2015, 1, 6), "bid"))
    assert bid is not None and bid["close"].iloc[0] == 11036.6


def test_day_committed_by_another_source_is_never_overwritten(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    day = date(2015, 1, 6)
    for side in ("bid", "ask"):
        p = ex._daily_candle_path(cache, SYMBOL, day, side)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"dukascopy was here first")

    _run(cache, date(2015, 1, 5), date(2015, 1, 7))

    for side in ("bid", "ask"):
        assert ex._daily_candle_path(cache, SYMBOL, day, side).read_bytes() == (
            b"dukascopy was here first"
        )
    days = [r["day"] for r in _jsonl(ex._source_manifest_path(cache, SYMBOL))]
    assert days == ["2015-01-05", "2015-01-07"]


def test_scale_sentry_refuses_a_day_off_its_neighbours_scale(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    # Neighbours at 1000x the points convention (a mis-scaled earlier export).
    idx = pd.DatetimeIndex([datetime(2015, 1, 1, 12, tzinfo=UTC)])
    for d in (date(2015, 1, 5), date(2015, 1, 7)):
        df = pd.DataFrame(
            {"open": 2055250.0, "high": 2055250.0, "low": 2055250.0, "close": 2055250.0},
            index=idx,
        ).assign(volume=0.0)
        for side in ("bid", "ask"):
            ex._write_daily_candles(df, ex._daily_candle_path(cache, SYMBOL, d, side))

    _run(cache, date(2015, 1, 6), date(2015, 1, 6))

    assert not _committed(cache, date(2015, 1, 6))
    assert _jsonl(ex._source_manifest_path(cache, SYMBOL)) == []


def test_resample_writes_range_csvs_from_histdata_days(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    out = tmp_path / "out"
    bid_csv, ask_csv = _run(cache, date(2015, 1, 5), date(2015, 1, 6), resample_rule="1h", out=out)

    assert bid_csv == out / f"{SYMBOL}_1H_bid.csv" and ask_csv is not None
    bid = pd.read_csv(bid_csv)
    assert list(bid.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert bid["timestamp"].iloc[0] == "2015-01-05 00:00:00+00:00"
    assert len(bid) == 44  # 22 trading hours x 2 days


def test_keep_raw_retains_a_fully_committed_months_zip(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    _run(cache, date(2015, 1, 1), date(2015, 2, 1), keep_raw=True)

    # January's file covers 2015-01-01..02-01, all committed: retained, not deleted.
    assert not hd._zip_path(cache, SYMBOL, (2015, 1)).exists()
    kept = cache / SYMBOL / "_raw" / "histdata" / "201501.zip"
    assert kept.exists()
    # December's covers days not committed yet: it stays staged.
    assert hd._zip_path(cache, SYMBOL, (2014, 12)).exists()


def test_a_kept_month_zip_is_read_instead_of_fetched(jan_2015, tmp_path):
    cache = tmp_path / "cache"
    _run(cache, date(2015, 1, 1), date(2015, 2, 1), keep_raw=True)
    for d in _days(date(2015, 1, 5), date(2015, 1, 9)):
        for side in ("bid", "ask"):
            ex._daily_candle_path(cache, SYMBOL, d, side).unlink()
    jan_2015.calls.clear()

    _run(cache, date(2015, 1, 5), date(2015, 1, 9))

    assert jan_2015.calls == []
    assert all(_committed(cache, d) for d in _days(date(2015, 1, 5), date(2015, 1, 9)))
    assert (cache / SYMBOL / "_raw" / "histdata" / "201501.zip").exists()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_stops_before_the_next_month(monkeypatch, tmp_path):
    inner = FakeFetcher(zips_by_month(weekday_lines(date(2014, 12, 1), date(2015, 2, 27))))

    def cancelling_fetch(inst, ym, **kw):
        cancellation.set()
        return inner(inst, ym, **kw)

    monkeypatch.setattr(hd, "_fetch_month_zip", cancelling_fetch)

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path / "cache", date(2015, 1, 1), date(2015, 2, 28))
    assert inner.calls == [(2014, 12)]


def test_tick_outside_its_month_file_is_ignored(monkeypatch, tmp_path, caplog):
    # January's file runs to 2015-02-01 05:00 UTC; a tick after that is February's business.
    stray = est_line(datetime(2015, 2, 1, 10, tzinfo=UTC), "9999.000000", "9999.500000")
    zips = {
        (2015, 1): month_zip(weekday_lines(date(2015, 1, 26), date(2015, 1, 30)) + [stray]),
        (2015, 2): month_zip(weekday_lines(date(2015, 2, 2), date(2015, 2, 6))),
    }
    monkeypatch.setattr(hd, "_fetch_month_zip", FakeFetcher(zips))
    cache = tmp_path / "cache"

    with caplog.at_level("WARNING"):
        _run(cache, date(2015, 2, 1), date(2015, 2, 2))

    sunday = ex._load_daily_candles(ex._daily_candle_path(cache, SYMBOL, date(2015, 2, 1), "bid"))
    assert sunday is not None and sunday.empty
    assert "ignoring 1 tick(s) outside 2015-01" in caplog.text
