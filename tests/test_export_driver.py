"""
The framework's side of the source contract, driven by a toy in-test source:
unit ordering, per-day decisions (commit, partial, leave, wait), what the
framework records, and that a refused symbol stops before any fetch.
"""

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

import tradedesk_marketdata.export as ex
from tradedesk_marketdata.source import (
    Commit,
    Exclude,
    Leave,
    Partial,
    Provenance,
    Source,
    SourceDescription,
    SourceError,
    SourceRun,
    Wait,
)

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


@dataclass(frozen=True)
class ToyUnit:
    key: int
    days: tuple[date, ...]


def _ticks(day: date, hour: int, price: float) -> pd.DataFrame:
    ts = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    return pd.DataFrame(
        {"bid": [price], "ask": [price + 1], "bid_vol": [0.0], "ask_vol": [0.0]},
        index=pd.DatetimeIndex([ts]),
    )


class ToyRun(SourceRun):
    def __init__(self, source: "ToySource") -> None:
        self.source = source

    def plan(self, pending):
        self.source.calls.append(("plan", tuple(pending)))
        return [u for u in self.source.units if any(d in pending for d in u.days)]

    def fetch(self, unit):
        delay = self.source.fetch_delays.get(unit.key, 0.0)
        time.sleep(delay)
        with self.source.lock:
            self.source.fetched.append(unit.key)
        return unit.key

    def decode(self, unit, raw):
        self.source.calls.append(("decode", raw))
        return self.source.ticks.get(unit.key)

    def decide(self, day, *, has_data):
        self.source.calls.append(("decide", day, has_data))
        answers = self.source.decisions.get(day, [Commit()])
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def excludes(self, day):
        self.source.calls.append(("excludes", day))
        return self.source.still_excluded.get(day)

    def committed(self, day, *, empty):
        self.source.calls.append(("committed", day, empty))
        return Provenance(scale_factor=2.0, source_unit=f"toy-{day.isoformat()}")

    def finish(self):
        self.source.calls.append(("finish",))


class ToySource(Source):
    name = "toy"
    summary = "a source that exists only in this test"
    unit = "toy unit"
    partial_commit_rule = "whatever the test says"

    def __init__(self, units, ticks, decisions=None, workers=1, fetch_delays=None, refuse=None):
        super().__init__()
        self.units = units
        self.ticks = ticks
        self.decisions = decisions or {}
        self.workers = workers
        self.fetch_delays = fetch_delays or {}
        self.refuse = refuse
        self.still_excluded: dict[date, str] = {}
        self.calls: list[tuple] = []
        self.fetched: list[int] = []
        self.lock = threading.Lock()

    def check_symbol(self, symbol):
        if symbol == self.refuse:
            raise SourceError(f"{symbol} is not served")

    def describe(self, symbol):
        return SourceDescription(price_divisor=1.0)

    def open(self, ctx):
        self.calls.append(("open", ctx.symbol))
        run = ToyRun(self)
        run.fetch_workers = self.workers
        return run


def _export(source, tmp_path, first=D1, last=D3, **kwargs):
    return ex.export_range(
        source=source,
        symbol="toy/usd",
        start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
        end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
        out=tmp_path / "out",
        resample_rule=kwargs.pop("resample_rule", None),
        cache_dir=tmp_path / "cache",
        **kwargs,
    )


def _committed(tmp_path, day) -> bool:
    return ex._day_is_committed(tmp_path / "cache", "TOYUSD", day)


def _jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_a_day_is_decided_once_every_unit_covering_it_is_decoded(tmp_path):
    units = [ToyUnit(1, (D1, D2)), ToyUnit(2, (D2,)), ToyUnit(3, (D3,))]
    ticks = {1: pd.concat([_ticks(D1, 10, 5.0), _ticks(D2, 1, 6.0)]), 2: _ticks(D2, 12, 7.0)}
    source = ToySource(units, ticks)

    _export(source, tmp_path)

    order = [c for c in source.calls if c[0] in ("decode", "decide", "finish")]
    assert order == [
        ("decode", 1),
        ("decide", D1, True),
        ("decode", 2),
        ("decide", D2, True),
        ("decode", 3),
        ("decide", D3, False),
        ("finish",),
    ]
    # A unit spanning two days fed both; D2 holds the candles of both units.
    d2 = ex._load_daily_candles(ex._daily_candle_path(tmp_path / "cache", "TOYUSD", D2, "bid"))
    assert d2 is not None and list(d2["close"]) == [6.0, 7.0]
    # D3 had no ticks: committed as an empty day, and the source was told so.
    assert ("committed", D3, True) in source.calls


def test_units_are_processed_in_plan_order_whatever_order_they_arrive_in(tmp_path):
    units = [ToyUnit(k, (D1,)) for k in range(1, 6)]
    # The first unit is the slowest to fetch, so the pool finishes it last.
    source = ToySource(units, {}, workers=3, fetch_delays={1: 0.2, 2: 0.1})

    _export(source, tmp_path, last=D1)

    assert source.fetched.index(1) > 0  # it did arrive out of order
    assert [c[1] for c in source.calls if c[0] == "decode"] == [1, 2, 3, 4, 5]


def test_framework_records_the_source_provenance_and_partial_days(tmp_path):
    units = [ToyUnit(1, (D1,)), ToyUnit(2, (D2,))]
    ticks = {1: _ticks(D1, 10, 5.0), 2: _ticks(D2, 10, 5.0)}
    partial = Partial(missing_hours=[3, 4], gap_reason="toy_gap", note="two toy hours missing")
    source = ToySource(units, ticks, decisions={D2: [Commit(partial)]})

    _export(source, tmp_path, last=D2)

    records = _jsonl(ex._source_manifest_path(tmp_path / "cache", "TOYUSD"))
    assert [(r["day"], r["source"], r["scale_factor"], r["source_unit"]) for r in records] == [
        ("2024-01-02", "toy", 2.0, "toy-2024-01-02"),
        ("2024-01-03", "toy", 2.0, "toy-2024-01-03"),
    ]
    [rec] = _jsonl(ex._partial_day_manifest_path(tmp_path / "cache", "TOYUSD"))
    assert (rec["day"], rec["missing_hours"], rec["gap_reason"]) == (
        "2024-01-03",
        [3, 4],
        "toy_gap",
    )


def test_a_left_day_is_not_written_or_recorded(tmp_path):
    units = [ToyUnit(1, (D1,))]
    source = ToySource(units, {1: _ticks(D1, 10, 5.0)}, decisions={D1: [Leave("not yet")]})

    _export(source, tmp_path, last=D1)

    assert not _committed(tmp_path, D1)
    assert ("committed", D1, False) not in source.calls
    assert _jsonl(ex._source_manifest_path(tmp_path / "cache", "TOYUSD")) == []


def test_a_waiting_day_is_asked_again_after_the_next_unit(tmp_path):
    units = [ToyUnit(1, (D1,)), ToyUnit(2, (D2,))]
    ticks = {1: _ticks(D1, 10, 5.0), 2: _ticks(D2, 10, 5.0)}
    source = ToySource(units, ticks, decisions={D1: [Wait("maybe more"), Commit()]})

    _export(source, tmp_path, last=D2)

    decides = [c[1] for c in source.calls if c[0] == "decide"]
    assert decides == [D1, D1, D2]
    assert _committed(tmp_path, D1) and _committed(tmp_path, D2)


def test_a_day_still_waiting_when_the_units_run_out_is_left(tmp_path, caplog):
    units = [ToyUnit(1, (D1,))]
    source = ToySource(units, {1: _ticks(D1, 10, 5.0)}, decisions={D1: [Wait("data may run on")]})

    with caplog.at_level("DEBUG"):
        _export(source, tmp_path, last=D1)

    assert not _committed(tmp_path, D1)
    assert "2024-01-02 left uncommitted (data may run on)" in caplog.text
    assert "left_uncommitted=1" in caplog.text


def test_a_pending_day_no_unit_covers_is_left_alone(tmp_path):
    source = ToySource([ToyUnit(1, (D1,))], {1: _ticks(D1, 10, 5.0)})

    _export(source, tmp_path, last=D2)

    assert _committed(tmp_path, D1)
    assert not _committed(tmp_path, D2)
    assert all(c[1] != D2 for c in source.calls if c[0] == "decide")


def test_committed_days_are_neither_planned_nor_rewritten(tmp_path):
    for side in ("bid", "ask"):
        p = ex._daily_candle_path(tmp_path / "cache", "TOYUSD", D1, side)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"first")
    source = ToySource([ToyUnit(1, (D1,)), ToyUnit(2, (D2,))], {2: _ticks(D2, 10, 5.0)})

    _export(source, tmp_path, last=D2)

    assert ("plan", (D2,)) in source.calls
    assert source.fetched == [2]
    assert ex._daily_candle_path(tmp_path / "cache", "TOYUSD", D1, "bid").read_bytes() == b"first"


def test_a_refused_symbol_stops_before_the_source_is_opened(tmp_path):
    source = ToySource([ToyUnit(1, (D1,))], {}, refuse="TOYUSD")

    with pytest.raises(SourceError, match="TOYUSD is not served"):
        _export(source, tmp_path, last=D1)

    assert source.calls == []


def test_range_csvs_include_days_already_in_the_cache(tmp_path):
    first = _export(ToySource([ToyUnit(1, (D1,))], {1: _ticks(D1, 10, 5.0)}), tmp_path, last=D1)
    assert first == (None, None)
    source = ToySource([ToyUnit(2, (D2,))], {2: _ticks(D2, 10, 6.0)})

    bid_csv, _ = _export(source, tmp_path, last=D2, resample_rule="1h")

    assert bid_csv is not None
    closes = list(pd.read_csv(bid_csv)["close"])
    assert closes == [5.0, 6.0]
    assert source.fetched == [2]


def test_the_tick_split_follows_utc_days_for_unsorted_ticks() -> None:
    late = _ticks(D2, 23, 2.0)
    early = _ticks(D2, 1, 1.0)
    other = _ticks(D3, 0, 3.0)
    frame = pd.concat([late, other, early])  # not in time order

    parts = list(ex._split_days(frame))

    assert [d for d, _ in parts] == [D2, D3]
    assert list(parts[0][1]["bid"]) == [2.0, 1.0]  # original order kept within a day
    assert list(parts[1][1]["bid"]) == [3.0]


def test_the_tick_split_of_a_sorted_frame_skips_empty_days() -> None:
    frame = pd.concat([_ticks(D1, 23, 1.0), _ticks(D3, 0, 3.0)])

    parts = list(ex._split_days(frame))

    assert [d for d, _ in parts] == [D1, D3]
    assert parts[0][1].index[0] < pd.Timestamp(D1 + timedelta(days=1), tz="UTC")


# ---------------------------------------------------------------------------
# Exclude: a day the source refuses to commit, on the record
# ---------------------------------------------------------------------------


def _excluding_source() -> ToySource:
    units = [ToyUnit(1, (D1,)), ToyUnit(2, (D2,))]
    ticks = {1: _ticks(D1, 10, 5.0), 2: _ticks(D2, 10, 5.0)}
    reason = "toy feed carries another instrument here"
    source = ToySource(units, ticks, decisions={D2: [Exclude(reason, source_unit="toy-unit-2")]})
    source.still_excluded = {D2: reason}
    return source


def test_an_excluded_day_is_recorded_but_never_written(tmp_path, caplog):
    source = _excluding_source()

    with caplog.at_level("INFO"):
        _export(source, tmp_path, last=D2)

    assert _committed(tmp_path, D1)
    for side in ("bid", "ask"):
        assert not ex._daily_candle_path(tmp_path / "cache", "TOYUSD", D2, side).exists()
    assert ("committed", D2, False) not in source.calls
    records = _jsonl(ex._source_manifest_path(tmp_path / "cache", "TOYUSD"))
    [excluded] = [r for r in records if r.get("status") == "excluded"]
    assert set(excluded) == {"day", "source", "status", "reason", "decided_at", "source_unit"}
    assert excluded["day"] == "2024-01-03" and excluded["source"] == "toy"
    assert excluded["reason"] == "toy feed carries another instrument here"
    assert excluded["source_unit"] == "toy-unit-2"
    assert datetime.fromisoformat(excluded["decided_at"]).tzinfo is not None
    # The commit record keeps its shape: no status means committed.
    [committed] = [r for r in records if "status" not in r]
    assert committed["day"] == "2024-01-02"
    assert "excluded=1, already_excluded=0" in caplog.text
    assert [c[1] for c in source.calls if c[0] == "decide"] == [D1, D2]  # asked once


def test_an_excluded_day_is_not_fetched_again_while_the_source_still_excludes_it(tmp_path, caplog):
    _export(_excluding_source(), tmp_path, last=D2)
    rerun = _excluding_source()

    with caplog.at_level("INFO"):
        _export(rerun, tmp_path, last=D2)

    # D1 committed and D2 recorded as excluded: nothing left to plan or fetch.
    assert not any(c[0] == "plan" for c in rerun.calls)
    assert rerun.fetched == []
    assert ("excludes", D2) in rerun.calls
    records = _jsonl(ex._source_manifest_path(tmp_path / "cache", "TOYUSD"))
    assert len([r for r in records if r.get("status") == "excluded"]) == 1  # not re-recorded
    assert "1 day(s) recorded as excluded by toy; not fetched again" in caplog.text
    assert "all 2 days cached or recorded as excluded (1)" in caplog.text


def test_a_recorded_exclusion_is_counted_in_a_run_that_still_has_work(tmp_path, caplog):
    _export(_excluding_source(), tmp_path, last=D2)
    rerun = _excluding_source()
    rerun.units.append(ToyUnit(3, (D3,)))

    with caplog.at_level("INFO"):
        _export(rerun, tmp_path, last=D3)

    assert ("plan", (D3,)) in rerun.calls
    assert rerun.fetched == [3]
    assert "excluded=0, already_excluded=1" in caplog.text


def test_lifting_an_exclusion_makes_the_day_pending_again(tmp_path):
    _export(_excluding_source(), tmp_path, last=D2)
    lifted = ToySource([ToyUnit(2, (D2,))], {2: _ticks(D2, 10, 5.0)})  # no exclusion now

    _export(lifted, tmp_path, last=D2)

    assert lifted.fetched == [2]
    assert _committed(tmp_path, D2)


def test_another_source_may_fill_a_day_one_source_excluded(tmp_path):
    _export(_excluding_source(), tmp_path, last=D2)
    other = ToySource([ToyUnit(2, (D2,))], {2: _ticks(D2, 10, 5.0)})
    other.name = "other"  # an instance attribute: a different source's records

    _export(other, tmp_path, last=D2)

    assert _committed(tmp_path, D2)
    assert ("excludes", D2) not in other.calls  # toy's exclusion is not other's
