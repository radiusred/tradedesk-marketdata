"""
The export's bad-tick rule (M12-R1 to M12-R4), on labelled synthetic ticks.

Every tick here is SYNTHETIC: a made-up quote stream on made-up days for the
toy symbol TOYUSD, built by ``_quotes``. Nothing is market data. A "4-decimal
quote" stream sits near 1.1 and an "index-like quote" stream near 6800; the
bad prices are the defects M12 names: one side with one extra digit (about ten
times too large) and one side with one digit missing (about ten times too small).
"""

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from test_export_driver import ToySource, ToyUnit

import tradedesk_marketdata.cli as cli
import tradedesk_marketdata.export as ex
import tradedesk_marketdata.verify_cli as verify_cli
from tradedesk_marketdata import sources
from tradedesk_marketdata.bad_ticks import (
    DEFAULT_MAX_JUMP,
    DEFAULT_WINDOW,
    RULE_ONE_SIDE_OUTLIER,
    BadTickRule,
)
from tradedesk_marketdata.source import Leave, SourceDescription

DAY = date(2024, 1, 2)  # a synthetic day
SYMBOL = "TOYUSD"


def _quotes(day: date, *, level: float, n: int = 120, spread: float = 0.0002) -> pd.DataFrame:
    """SYNTHETIC: ``n`` quotes, one every 30 s from 10:00 UTC, a slow saw-tooth around ``level``."""
    start = datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
    index = pd.DatetimeIndex([start + timedelta(seconds=30 * i) for i in range(n)])
    bid = level * (1 + 0.0001 * (np.arange(n) % 7))
    return pd.DataFrame(
        {
            "bid": bid,
            "ask": bid * (1 + spread),
            "bid_vol": np.ones(n),
            "ask_vol": np.ones(n),
        },
        index=index,
    )


def _px(ticks: pd.DataFrame, i: int, column: str) -> float:
    return float(ticks[column].to_numpy(dtype=float)[i])


def _set(ticks: pd.DataFrame, i: int, side: str, price: float) -> pd.DataFrame:
    values = ticks[side].to_numpy(dtype=float, copy=True)
    values[i] = price
    return ticks.assign(**{side: values})


# ---------------------------------------------------------------------------
# The rule on its own (M12-R1, M12-R3)
# ---------------------------------------------------------------------------


def test_an_ask_with_one_extra_digit_is_dropped_and_its_bid_kept() -> None:
    clean = _quotes(DAY, level=1.1)
    bad_price = _px(clean, 40, "ask") * 10  # SYNTHETIC defect: one extra digit
    ticks = _set(clean, 40, "ask", bad_price)

    out, dropped = BadTickRule().apply(ticks)

    [d] = dropped
    assert (d.ts, d.side, d.price, d.rule) == (
        ticks.index[40],
        "ask",
        bad_price,
        RULE_ONE_SIDE_OUTLIER,
    )
    assert np.isnan(_px(out, 40, "ask")) and _px(out, 40, "ask_vol") == 0.0
    assert _px(out, 40, "bid") == _px(clean, 40, "bid")  # the correct side stays
    assert out.drop(index=ticks.index[40]).equals(clean.drop(index=ticks.index[40]))


def test_a_bid_with_one_digit_missing_is_dropped() -> None:
    clean = _quotes(DAY, level=6800.0)
    bad_price = 680.0  # SYNTHETIC defect: an index-like bid with one digit missing
    ticks = _set(clean, 70, "bid", bad_price)

    out, dropped = BadTickRule().apply(ticks)

    assert [(d.ts, d.side, d.price) for d in dropped] == [(ticks.index[70], "bid", bad_price)]
    assert np.isnan(_px(out, 70, "bid"))
    assert _px(out, 70, "ask") == _px(clean, 70, "ask")


def test_a_gap_that_both_sides_follow_is_kept() -> None:
    ticks = _quotes(DAY, level=1.1)
    ticks.iloc[60:, :2] *= 1.3  # SYNTHETIC: both sides gap up 30% and stay there

    out, dropped = BadTickRule().apply(ticks)

    assert dropped == []
    assert out is ticks


def test_a_one_tick_spike_on_both_sides_is_kept() -> None:
    ticks = _quotes(DAY, level=1.1)
    ticks.iloc[50, :2] *= 1.5  # SYNTHETIC: both sides spike together for one tick

    out, dropped = BadTickRule().apply(ticks)

    assert dropped == [] and out is ticks


def test_a_one_side_step_the_next_ticks_follow_is_kept() -> None:
    ticks = _quotes(DAY, level=1.1)
    ticks.iloc[60:, 1] *= 1.3  # SYNTHETIC: the ask steps away from the bid and stays

    _, dropped = BadTickRule().apply(ticks)

    assert dropped == []  # not isolated: the ticks after it agree with it


def test_the_thresholds_are_set_by_the_caller() -> None:
    assert (BadTickRule().max_jump, BadTickRule().window) == (DEFAULT_MAX_JUMP, DEFAULT_WINDOW)
    clean = _quotes(DAY, level=1.1)
    ticks = _set(clean, 40, "ask", _px(clean, 40, "ask") * 10)

    out, dropped = BadTickRule(max_jump=10.0).apply(ticks)

    assert dropped == [] and out is ticks
    with pytest.raises(ValueError):
        BadTickRule(max_jump=0.0)
    with pytest.raises(ValueError):
        BadTickRule(window=0)


def test_the_rule_is_deterministic_and_positional_on_shared_timestamps() -> None:
    clean = _quotes(DAY, level=1.1)
    ticks = _set(clean, 41, "ask", _px(clean, 41, "ask") * 10)
    # SYNTHETIC: tick 41 shares tick 40's timestamp; only tick 41 is bad.
    ticks.index = ticks.index.where(np.arange(len(ticks)) != 41, ticks.index[40])

    first = BadTickRule().apply(ticks)
    second = BadTickRule().apply(ticks)

    assert first[1] == second[1] and first[0].equals(second[0])
    assert [(d.ts, d.side) for d in first[1]] == [(ticks.index[40], "ask")]
    assert _px(first[0], 40, "ask") == _px(clean, 40, "ask")  # its twin is untouched
    assert np.isnan(_px(first[0], 41, "ask"))


# ---------------------------------------------------------------------------
# In the export: the candles and the audit record (M12-R2, M12-R3)
# ---------------------------------------------------------------------------


def _export(cache, ticks, **kwargs):
    source = kwargs.pop("source", None) or ToySource([ToyUnit(1, (DAY,))], {1: ticks})
    start = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC)
    ex.export_range(
        source=source,
        symbol=SYMBOL,
        start_utc=start,
        end_utc_inclusive=start,
        out=cache.parent / "out",
        resample_rule=None,
        cache_dir=cache,
        **kwargs,
    )
    return source


def _day_bytes(cache) -> dict[str, bytes]:
    return {
        side: ex._daily_candle_path(cache, SYMBOL, DAY, side).read_bytes()
        for side in ("bid", "ask")
    }


def _records(cache) -> list[dict]:
    path = ex._source_manifest_path(cache, SYMBOL)
    return [json.loads(line) for line in path.read_text().splitlines()]


def _without_time(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "committed_at"}


def test_an_unchanged_day_is_byte_identical_to_the_export_without_the_rule(tmp_path) -> None:
    ticks = _quotes(DAY, level=1.1)

    _export(tmp_path / "plain", ticks, bad_tick_rule=None)
    _export(tmp_path / "ruled", ticks)

    assert _day_bytes(tmp_path / "ruled") == _day_bytes(tmp_path / "plain")
    [plain] = _records(tmp_path / "plain")
    [ruled] = _records(tmp_path / "ruled")
    assert _without_time(ruled) == _without_time(plain)
    assert "dropped_ticks" not in ruled and "reexport" not in ruled


def test_a_dropped_tick_is_kept_out_of_the_candles_and_recorded(tmp_path) -> None:
    clean = _quotes(DAY, level=1.1)
    bad_price = _px(clean, 40, "ask") * 10  # SYNTHETIC defect: one extra digit
    cache = tmp_path / "cache"

    _export(cache, _set(clean, 40, "ask", bad_price))

    ask = ex._load_daily_candles(ex._daily_candle_path(cache, SYMBOL, DAY, "ask"))
    assert ask is not None and ask["high"].max() < 2.0
    [rec] = _records(cache)
    assert rec["dropped_ticks"] == [
        {
            "ts": clean.index[40].isoformat(),
            "side": "ask",
            "price": bad_price,
            "rule": RULE_ONE_SIDE_OUTLIER,
        }
    ]


# ---------------------------------------------------------------------------
# Re-exporting one committed day (M12-R4)
# ---------------------------------------------------------------------------


class _CliToySource(ToySource):
    """A toy source the CLI can build: its units and ticks are set by the test."""

    name = "toy"
    feed: dict = {}

    def __init__(self, options=None):
        super().__init__([ToyUnit(1, (DAY,))], {1: self.feed["ticks"]})

    def describe(self, symbol):
        return SourceDescription(price_divisor=1.0)


def test_the_cli_re_exports_a_committed_day_and_verify_then_reports_it(
    tmp_path, monkeypatch
) -> None:
    clean = _quotes(DAY, level=1.1)
    bad = _set(clean, 40, "ask", _px(clean, 40, "ask") * 10)  # SYNTHETIC defect
    cache, reference = tmp_path / "cache", tmp_path / "reference"
    _export(reference, clean)
    _export(cache, bad, bad_tick_rule=None)  # a day committed before the rule existed
    monkeypatch.setitem(sources._REGISTRY, "toy", _CliToySource)
    monkeypatch.setitem(_CliToySource.feed, "ticks", bad)

    def verify() -> int:
        return verify_cli.main(
            ["--reference", str(reference), "--cache-dir", str(cache), "--symbols", SYMBOL]
            + ["--from", DAY.isoformat(), "--to", DAY.isoformat(), "--side", "ask"]
        )

    assert verify() == 1  # the bad ask makes the day differ from the reference

    rc = cli.main(
        ["--source", "toy", "--symbols", SYMBOL, "--from", DAY.isoformat()]
        + ["--to", DAY.isoformat(), "--cache-dir", str(cache), "--reexport"]
    )

    assert rc == 0
    assert _day_bytes(cache)["bid"] == _day_bytes(reference)["bid"]
    ask = ex._load_daily_candles(ex._daily_candle_path(cache, SYMBOL, DAY, "ask"))
    assert ask is not None and ask["high"].max() < 2.0
    first, second = _records(cache)
    assert "reexport" not in first and "dropped_ticks" not in first
    assert second["reexport"] is True
    assert [d["side"] for d in second["dropped_ticks"]] == ["ask"]
    assert verify() == 0


def test_without_reexport_a_committed_day_is_not_rewritten(tmp_path) -> None:
    clean = _quotes(DAY, level=1.1)
    cache = tmp_path / "cache"
    _export(cache, _set(clean, 40, "ask", 11.0), bad_tick_rule=None)
    before = _day_bytes(cache)

    source = _export(cache, clean)

    assert _day_bytes(cache) == before
    assert not any(c[0] == "decode" for c in source.calls)


def test_a_re_export_the_source_does_not_commit_leaves_the_day_as_it_was(tmp_path) -> None:
    clean = _quotes(DAY, level=1.1)
    cache = tmp_path / "cache"
    _export(cache, clean)
    before, records = _day_bytes(cache), _records(cache)
    source = ToySource([ToyUnit(1, (DAY,))], {1: clean}, decisions={DAY: [Leave("not yet")]})

    with pytest.raises(ex.ReexportError, match="not re-exported .*not yet"):
        _export(cache, clean, source=source, reexport=True)

    assert _day_bytes(cache) == before and _records(cache) == records


def _fail_the_ask_write(monkeypatch) -> None:
    real_write = ex._write_daily_candles

    def write(df, path):
        if "ask" in path.name:
            raise OSError("SYNTHETIC: the disk refused the ask file")
        real_write(df, path)

    monkeypatch.setattr(ex, "_write_daily_candles", write)


def test_a_re_export_whose_second_write_fails_leaves_both_files_as_they_were(
    tmp_path, monkeypatch
) -> None:
    clean = _quotes(DAY, level=1.1)
    cache = tmp_path / "cache"
    _export(cache, _set(clean, 40, "ask", _px(clean, 40, "ask") * 10), bad_tick_rule=None)
    before, records = _day_bytes(cache), _records(cache)
    _fail_the_ask_write(monkeypatch)

    # The re-export would change the bid file too: its candles come from other ticks.
    with pytest.raises(ex.ReexportError, match="could not be written\\); .* are unchanged"):
        _export(cache, _quotes(DAY, level=1.2), reexport=True)

    assert _day_bytes(cache) == before and _records(cache) == records
    assert not list(cache.rglob("*.tmp"))


def test_a_new_day_whose_second_write_fails_is_not_left_half_committed(
    tmp_path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    _fail_the_ask_write(monkeypatch)

    _export(cache, _quotes(DAY, level=1.1))

    assert not ex._daily_candle_path(cache, SYMBOL, DAY, "bid").exists()
    assert not ex._daily_candle_path(cache, SYMBOL, DAY, "ask").exists()
    assert not ex._source_manifest_path(cache, SYMBOL).exists()


def test_a_re_export_needs_one_committed_day(tmp_path) -> None:
    ticks = _quotes(DAY, level=1.1)
    with pytest.raises(ex.ReexportError, match="is not committed"):
        _export(tmp_path / "cache", ticks, reexport=True)
    with pytest.raises(ex.ReexportError, match="exactly one day"):
        ex.export_range(
            source=ToySource([], {}),
            symbol=SYMBOL,
            start_utc=datetime(2024, 1, 2, tzinfo=UTC),
            end_utc_inclusive=datetime(2024, 1, 3, tzinfo=UTC),
            out=tmp_path / "out",
            resample_rule=None,
            cache_dir=tmp_path / "cache",
            reexport=True,
        )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--symbols", "EURUSD", "GBPUSD", "--reexport"], "exactly one symbol and one day"),
        (["--symbols", "EURUSD", "--to", "2015-01-03", "--reexport"], "one day"),
        (["--symbols", "EURUSD", "--reexport", "--no-cache"], "needs the cache"),
        (["--symbols", "EURUSD", "--bad-tick-window", "0"], "window must be >= 1"),
        (["--symbols", "EURUSD", "--bad-tick-max-jump", "0"], "max_jump must be > 0"),
    ],
)
def test_the_cli_refuses_a_bad_reexport_or_rule_setting(args, message, capsys) -> None:
    argv = ["--source", "histdata", "--from", "2015-01-02", "--to", "2015-01-02"]
    if "--to" in args:
        argv = argv[:-2]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + args)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_the_cli_passes_the_rule_settings_to_the_export_tasks(monkeypatch) -> None:
    import tradedesk_marketdata.parallel as par

    captured: list = []

    def fake_run_parallel_exports(tasks, max_workers):
        captured.extend(tasks)
        return []

    monkeypatch.setattr(par, "run_parallel_exports", fake_run_parallel_exports)
    rc = cli.main(
        ["--source", "histdata", "--symbols", "EURUSD", "--from", "2015-01-02"]
        + ["--to", "2015-01-02", "--bad-tick-max-jump", "0.5", "--bad-tick-window", "3"]
    )
    assert rc == 0
    [task] = captured
    assert task.bad_tick_rule == BadTickRule(max_jump=0.5, window=3)
    assert task.reexport is False
