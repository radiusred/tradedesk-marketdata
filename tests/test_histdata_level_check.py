"""
HistData's month-join level check: the provider's own guard against a series
that switches to another instrument at a month join. A synthetic ×0.26 step is
refused and named, its days stay uncommitted, and the threshold is configuration.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from histdata_fixtures import FakeFetcher, weekday_lines, zips_by_month

import tradedesk_marketdata.export as ex
from tradedesk_marketdata.cancel import cancellation
from tradedesk_marketdata.sources import histdata as hd

SYMBOL = "USA500IDXUSD"
ENTRY = '[symbols.USA500IDXUSD]\nname = "SPXUSD"\nscale = 1\nfirst_month = "2010-11"\n'


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(hd, "REQUEST_INTERVAL", 0.0)
    cancellation.clear()
    yield
    cancellation.clear()


def _feed(monkeypatch, segments: list[tuple[date, date, float]]) -> FakeFetcher:
    lines = [line for first, last, px in segments for line in weekday_lines(first, last, price=px)]
    fetch = FakeFetcher(zips_by_month(lines))
    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    return fetch


def _export(cache: Path, first: date, last: date, symbol_map: Path | None = None):
    return hd.export_range_histdata(
        symbol=SYMBOL,
        start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
        end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
        out=cache.parent / "out",
        resample_rule=None,
        cache_dir=cache,
        symbol_map=symbol_map,
    )


def _committed(cache: Path, first: date, last: date) -> list[date]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if ex._day_is_committed(cache, SYMBOL, d)]


def _weekdays(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if d.weekday() < 5]


def test_a_step_down_at_a_month_join_refuses_the_month(monkeypatch, tmp_path, caplog):
    # January at DAX-like levels, February at 0.26x: another instrument.
    _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 10000.0),
            (date(2015, 2, 2), date(2015, 2, 27), 2600.0),
        ],
    )
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _export(cache, date(2015, 1, 5), date(2015, 2, 27))

    days = _committed(cache, date(2015, 1, 5), date(2015, 2, 28))
    assert days and max(days) < date(2015, 2, 1)  # nothing fed by February's file
    assert "refusing HistData month 2015-02" in caplog.text
    assert "3.85x the last accepted close 10000" in caplog.text
    assert "refused=1" in caplog.text
    assert "1 HistData month(s) refused by the month-join level check: 2015-02" in caplog.text


def test_a_step_up_is_refused_too_and_a_refused_month_is_no_reference(monkeypatch, tmp_path):
    # Two substituted months, then the level returns.
    _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 2600.0),
            (date(2015, 2, 2), date(2015, 3, 31), 10000.0),
            (date(2015, 4, 1), date(2015, 4, 30), 2600.0),
        ],
    )
    cache = tmp_path / "cache"

    _export(cache, date(2015, 1, 5), date(2015, 4, 29))

    days = _committed(cache, date(2015, 1, 5), date(2015, 4, 30))
    # February and March are refused (March is compared with January, not with
    # the refused February); April joins January's level again.
    assert not [d for d in days if date(2015, 2, 1) < d < date(2015, 4, 1)]
    assert set(_weekdays(date(2015, 4, 2), date(2015, 4, 29))) <= set(days)


def test_a_normal_join_passes(monkeypatch, tmp_path):
    _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 10000.0),
            (date(2015, 2, 2), date(2015, 2, 27), 10500.0),
        ],
    )
    cache = tmp_path / "cache"

    _export(cache, date(2015, 1, 5), date(2015, 2, 26))

    days = _committed(cache, date(2015, 1, 5), date(2015, 2, 26))
    assert set(_weekdays(date(2015, 1, 5), date(2015, 2, 26))) <= set(days)


def test_a_join_after_a_missing_month_is_not_checked(monkeypatch, tmp_path, caplog):
    # January ~100, no February file at all, March ~180: weeks apart, a real
    # market can move that far; March must not be refused against January.
    _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 100.0),
            (date(2015, 3, 2), date(2015, 3, 31), 180.0),
        ],
    )
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _export(cache, date(2015, 1, 5), date(2015, 3, 30))

    assert "refusing HistData month" not in caplog.text and "refused=0" in caplog.text
    days = _committed(cache, date(2015, 3, 2), date(2015, 3, 30))
    assert set(_weekdays(date(2015, 3, 2), date(2015, 3, 30))) <= set(days)


def test_a_join_after_a_year_end_holiday_run_is_not_checked(monkeypatch, tmp_path, caplog):
    # December ticks stop on the 18th, January's start on the 5th: an 18-day gap.
    _feed(
        monkeypatch,
        [
            (date(2014, 12, 1), date(2014, 12, 18), 100.0),
            (date(2015, 1, 5), date(2015, 1, 30), 180.0),
        ],
    )
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _export(cache, date(2014, 12, 1), date(2015, 1, 29))

    assert "refusing HistData month" not in caplog.text and "refused=0" in caplog.text
    days = _committed(cache, date(2015, 1, 5), date(2015, 1, 29))
    assert set(_weekdays(date(2015, 1, 5), date(2015, 1, 29))) <= set(days)


def test_a_join_within_seven_days_across_a_year_end_is_checked(monkeypatch, tmp_path, caplog):
    # Ticks to Wednesday 2014-12-31, then 0.26x from Friday 2015-01-02: checked.
    _feed(
        monkeypatch,
        [
            (date(2014, 12, 1), date(2014, 12, 31), 10000.0),
            (date(2015, 1, 2), date(2015, 1, 30), 2600.0),
        ],
    )
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _export(cache, date(2014, 12, 1), date(2015, 1, 29))

    assert "refusing HistData month 2015-01" in caplog.text


def test_the_cache_is_the_reference_when_the_previous_month_is_not_fetched(monkeypatch, tmp_path):
    fetch = _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 10000.0),
            (date(2015, 2, 2), date(2015, 2, 27), 2600.0),
        ],
    )
    cache = tmp_path / "cache"
    _export(cache, date(2015, 1, 5), date(2015, 1, 31))  # January in the cache
    fetch.calls.clear()

    _export(cache, date(2015, 2, 2), date(2015, 2, 27))  # February alone, against the cache

    assert fetch.calls == [(2015, 2)]
    assert _committed(cache, date(2015, 2, 2), date(2015, 2, 27)) == []


def test_excluded_days_do_not_trip_the_check(monkeypatch, tmp_path):
    # The substituted span is configured: the step at the join is inside it.
    _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 10000.0),
            (date(2015, 2, 2), date(2015, 2, 13), 2600.0),
            (date(2015, 2, 16), date(2015, 2, 27), 10100.0),
        ],
    )
    span = 'exclude = [{ from = 2015-01-31, to = 2015-02-14, reason = "other index" }]\n'
    path = tmp_path / "map.toml"
    path.write_text(ENTRY + span)
    cache = tmp_path / "cache"

    _export(cache, date(2015, 1, 5), date(2015, 2, 26), symbol_map=path)

    days = _committed(cache, date(2015, 1, 5), date(2015, 2, 26))
    assert set(_weekdays(date(2015, 2, 16), date(2015, 2, 26))) <= set(days)
    assert not [d for d in days if date(2015, 1, 31) <= d <= date(2015, 2, 14)]


def test_raising_the_threshold_lets_a_real_move_through(monkeypatch, tmp_path, caplog):
    _feed(
        monkeypatch,
        [
            (date(2015, 1, 5), date(2015, 1, 30), 10000.0),
            (date(2015, 2, 2), date(2015, 2, 27), 2600.0),
        ],
    )
    path = tmp_path / "map.toml"
    path.write_text("[histdata]\nmax_month_join_step = 5\n" + ENTRY)
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _export(cache, date(2015, 1, 5), date(2015, 2, 26), symbol_map=path)

    # The level check lets February through...
    assert "refusing HistData month" not in caplog.text and "refused=0" in caplog.text
    # ...and the framework's scale sentry, a different guard, still refuses the
    # first days against January's neighbours until February's own days take over.
    days = _committed(cache, date(2015, 2, 2), date(2015, 2, 26))
    assert date(2015, 2, 2) not in days
    assert set(_weekdays(date(2015, 2, 9), date(2015, 2, 26))) <= set(days)


def test_the_threshold_defaults_to_0_7_and_is_validated(tmp_path):
    assert hd.load_map()[1] == hd.HistDataSettings(max_month_join_step=0.7)
    for body, message in (
        ("[histdata]\nmax_month_join_step = 0\n", "above zero"),
        ("[histdata]\nstep = 1\n", "histdata: unknown key(s) step"),
    ):
        path = tmp_path / "bad.toml"
        path.write_text(body + ENTRY)
        with pytest.raises(
            hd.SymbolMapError, match=message.replace("(", r"\(").replace(")", r"\)")
        ):
            hd.load_map(path)
