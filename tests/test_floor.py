"""
tradedesk-md-floor on a labelled synthetic store: symbol TESTSYM, made-up prices
around 100.0, days in the first weeks of 2024. Covers the floor set (days before
it moved with their manifest lines, days on and after it byte-identical), the
floor record, a second run as a no-op, lowering and removing as explicit
recorded actions, the refusals, and the export and verify honouring the floor.
No network.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from test_export_driver import ToySource, ToyUnit, _ticks

import tradedesk_marketdata.export as ex
from tradedesk_marketdata import floor as fl
from tradedesk_marketdata import floor_cli, verify_cli
from tradedesk_marketdata.cancel import cancellation

SYMBOL = "TESTSYM"  # synthetic: no instrument has this name
FIRST, LAST = date(2024, 1, 1), date(2024, 1, 31)
FLOOR = date(2024, 1, 15)


@pytest.fixture(autouse=True)
def _clear_cancellation():
    cancellation.clear()
    yield
    cancellation.clear()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _minutes(day: date) -> pd.DataFrame:
    """Sixty synthetic one-minute candles around 100.0, distinct per day."""
    index = pd.date_range(pd.Timestamp(day, tz="UTC"), periods=60, freq="1min")
    close = [100.0 + day.day / 100 + i / 1000 for i in range(60)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 0.01 for c in close],
            "low": [c - 0.01 for c in close],
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def _store(cache: Path, *, sides=("bid", "ask")) -> None:
    """Every day of [FIRST, LAST] committed, with its _sources.jsonl and some partial-day lines."""
    for d in _days(FIRST, LAST):
        for side in sides:
            ex._write_daily_candles(_minutes(d), ex._daily_candle_path(cache, SYMBOL, d, side))
        ex._append_source_manifest(
            cache, SYMBOL, d, source="toy", scale_factor=1.0, source_unit=f"unit-{d}"
        )
    for d in (date(2024, 1, 3), FLOOR, date(2024, 1, 20)):
        ex._append_partial_day_manifest(cache, SYMBOL, d, [5], "synthetic_gap")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _floor(tmp_path: Path, *extra: str, floor: str = FLOOR.isoformat()) -> int:
    return floor_cli.main(
        ["--cache-dir", str(tmp_path / "cache"), "--symbol", SYMBOL, "--floor", floor]
        + ["--backup-dir", str(tmp_path / "backup"), *extra]
    )


def test_setting_a_floor_moves_the_days_before_it_and_keeps_the_rest_byte_identical(tmp_path):
    cache = tmp_path / "cache"
    _store(cache)
    before = _tree(cache / SYMBOL)

    assert _floor(tmp_path) == 0

    moved = [d for d in _days(FIRST, LAST) if d < FLOOR]
    kept = [d for d in _days(FIRST, LAST) if d >= FLOOR]
    after = _tree(cache / SYMBOL)
    backup = _tree(tmp_path / "backup" / SYMBOL)
    for d in moved:
        for side in ("bid", "ask"):
            rel = str(ex._daily_candle_path(Path(), "", d, side))
            assert rel not in after
            assert backup[rel] == before[rel]
    for d in kept:
        for side in ("bid", "ask"):
            rel = str(ex._daily_candle_path(Path(), "", d, side))
            assert after[rel] == before[rel]
    # No day file before the floor is left, and no empty month directory either.
    assert not [p for p in (cache / SYMBOL).rglob("*") if p.is_dir() and not any(p.iterdir())]


def test_the_manifest_lines_of_the_moved_days_move_with_them(tmp_path):
    cache = tmp_path / "cache"
    _store(cache)
    _floor(tmp_path)

    live = _jsonl(ex._source_manifest_path(cache, SYMBOL))
    days = [r["day"] for r in live if "status" not in r]
    assert days == [d.isoformat() for d in _days(FLOOR, LAST)]
    backed = _jsonl(tmp_path / "backup" / SYMBOL / "_sources.jsonl")
    assert [r["day"] for r in backed] == [d.isoformat() for d in _days(FIRST, LAST) if d < FLOOR]
    assert [r["day"] for r in _jsonl(ex._partial_day_manifest_path(cache, SYMBOL))] == [
        "2024-01-15",
        "2024-01-20",
    ]
    assert [r["day"] for r in _jsonl(tmp_path / "backup" / SYMBOL / "_partial_days.jsonl")] == [
        "2024-01-03"
    ]


def test_the_floor_is_written_in_the_store_and_recorded_in_the_manifest(tmp_path):
    cache = tmp_path / "cache"
    _store(cache)
    _floor(tmp_path)

    stored = json.loads((cache / SYMBOL / "_floor.json").read_text())
    assert stored["floor"] == "2024-01-15"
    assert stored["tool_version"] == fl.__version__
    assert ex.read_floor(cache, SYMBOL) == FLOOR
    record = _jsonl(ex._source_manifest_path(cache, SYMBOL))[-1]
    assert record["status"] == "floor"
    assert (record["action"], record["floor"], record["previous"]) == ("set", "2024-01-15", None)
    assert record["days_moved"] == 14
    assert record["backup_dir"] == str(tmp_path / "backup")
    assert record["tool_version"] == fl.__version__
    assert datetime.fromisoformat(record["decided_at"]).tzinfo is not None
    # The floor record is not a commit and not an exclusion.
    assert ex._recorded_exclusions(cache, SYMBOL, "toy") == set()


def test_a_second_run_of_the_same_floor_is_a_no_op(tmp_path, capsys):
    _store(tmp_path / "cache")
    _floor(tmp_path)
    store, backup = _tree(tmp_path / "cache"), _tree(tmp_path / "backup")
    capsys.readouterr()

    assert _floor(tmp_path) == 0

    assert "nothing to do" in capsys.readouterr().out
    assert _tree(tmp_path / "cache") == store
    assert _tree(tmp_path / "backup") == backup


def test_a_day_with_one_side_only_is_moved_too(tmp_path):
    cache = tmp_path / "cache"
    _store(cache, sides=("bid",))

    result = fl.set_floor(cache, SYMBOL, FLOOR, tmp_path / "backup")

    assert result.days_moved == [d for d in _days(FIRST, LAST) if d < FLOOR]
    assert not ex._daily_candle_path(cache, SYMBOL, FIRST, "bid").exists()


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    _store(tmp_path / "cache")
    store = _tree(tmp_path / "cache")

    assert _floor(tmp_path, "--dry-run") == 0

    assert "would set the floor 2024-01-15; 14 day(s)" in capsys.readouterr().out
    assert _tree(tmp_path / "cache") == store
    assert not (tmp_path / "backup").exists()


def test_a_run_stopped_before_the_live_files_were_removed_is_completed_by_a_rerun(
    tmp_path, monkeypatch
):
    cache = tmp_path / "cache"
    _store(cache)
    real_unlink = Path.unlink

    def stop(self, *args, **kwargs):
        if self.name.endswith(".csv.zst"):
            raise OSError("stopped")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stop)
    with pytest.raises(OSError, match="stopped"):
        fl.set_floor(cache, SYMBOL, FLOOR, tmp_path / "backup")
    monkeypatch.setattr(Path, "unlink", real_unlink)

    fl.set_floor(cache, SYMBOL, FLOOR, tmp_path / "backup")

    assert not ex._daily_candle_path(cache, SYMBOL, FIRST, "bid").exists()
    backed = _jsonl(tmp_path / "backup" / SYMBOL / "_sources.jsonl")
    assert len(backed) == 14  # appended once, not twice
    days = [r["day"] for r in _jsonl(ex._source_manifest_path(cache, SYMBOL)) if "day" in r]
    assert days == [d.isoformat() for d in _days(FLOOR, LAST)]


def test_lowering_a_floor_needs_lower_and_is_recorded(tmp_path, capsys):
    cache = tmp_path / "cache"
    _store(cache)
    _floor(tmp_path)
    store = _tree(cache)

    assert _floor(tmp_path, floor="2024-01-08") == 1
    assert "lowering a floor needs --lower" in capsys.readouterr().err
    assert _tree(cache) == store

    assert _floor(tmp_path, "--lower", floor="2024-01-08") == 0
    assert ex.read_floor(cache, SYMBOL) == date(2024, 1, 8)
    record = _jsonl(ex._source_manifest_path(cache, SYMBOL))[-1]
    assert (record["action"], record["floor"], record["previous"], record["days_moved"]) == (
        "lower",
        "2024-01-08",
        "2024-01-15",
        0,
    )


def test_raising_a_floor_moves_the_next_days(tmp_path):
    cache = tmp_path / "cache"
    _store(cache)
    _floor(tmp_path)

    assert _floor(tmp_path, floor="2024-01-20") == 0

    record = _jsonl(ex._source_manifest_path(cache, SYMBOL))[-1]
    assert (record["action"], record["previous"], record["days_moved"]) == ("set", "2024-01-15", 5)
    assert len(_jsonl(tmp_path / "backup" / SYMBOL / "_sources.jsonl")) == 19


def test_removing_a_floor_is_recorded_and_a_second_remove_is_a_no_op(tmp_path, capsys):
    cache = tmp_path / "cache"
    _store(cache)
    _floor(tmp_path)
    args = ["--cache-dir", str(cache), "--symbol", SYMBOL, "--remove"]

    assert floor_cli.main(args) == 0

    assert ex.read_floor(cache, SYMBOL) is None
    record = _jsonl(ex._source_manifest_path(cache, SYMBOL))[-1]
    assert (record["action"], record["floor"], record["previous"]) == ("remove", None, "2024-01-15")
    store = _tree(cache)
    capsys.readouterr()
    assert floor_cli.main(args) == 0
    assert "no floor already" in capsys.readouterr().out
    assert _tree(cache) == store


def test_a_backup_dir_inside_the_cache_is_refused(tmp_path, capsys):
    cache = tmp_path / "cache"
    _store(cache)
    store = _tree(cache)
    args = ["--cache-dir", str(cache), "--symbol", SYMBOL, "--floor", FLOOR.isoformat()]

    assert floor_cli.main([*args, "--backup-dir", str(cache / "retired")]) == 1

    assert "inside the cache" in capsys.readouterr().err
    assert _tree(cache) == store


def test_a_backup_file_with_other_bytes_is_refused_and_nothing_moves(tmp_path, capsys):
    cache = tmp_path / "cache"
    _store(cache)
    clash = ex._daily_candle_path(tmp_path / "backup", SYMBOL, date(2024, 1, 10), "ask")
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"another day")
    store = _tree(cache)

    assert _floor(tmp_path) == 1

    assert "exists with other bytes" in capsys.readouterr().err
    assert _tree(cache) == store
    assert _tree(tmp_path / "backup") == {
        str(clash.relative_to(tmp_path / "backup")): b"another day"
    }


def test_an_unknown_symbol_is_refused(tmp_path, capsys):
    (tmp_path / "cache").mkdir()

    assert _floor(tmp_path) == 1

    assert "no such symbol" in capsys.readouterr().err
    assert not (tmp_path / "cache" / SYMBOL).exists()


def test_floor_needs_a_backup_dir(tmp_path):
    (tmp_path / "cache").mkdir()
    with pytest.raises(SystemExit) as e:
        floor_cli.main(
            ["--cache-dir", str(tmp_path / "cache"), "--symbol", SYMBOL, "--floor", "2024-01-15"]
        )
    assert e.value.code == 2


def test_a_floor_file_that_does_not_parse_stops_the_export(tmp_path):
    sym = tmp_path / "cache" / "TOYUSD"
    sym.mkdir(parents=True)
    (sym / "_floor.json").write_text("{}")
    source = ToySource([ToyUnit(1, (FIRST,))], {})

    with pytest.raises(ex.FloorError, match="not a floor file"):
        _export(source, tmp_path, FIRST, FIRST)

    assert source.calls == []


# ---------------------------------------------------------------------------
# The export honours the floor
# ---------------------------------------------------------------------------

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


def _export(source, tmp_path, first, last, **kwargs):
    return ex.export_range(
        source=source,
        symbol="TOYUSD",
        start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
        end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
        out=tmp_path / "out",
        resample_rule=kwargs.pop("resample_rule", None),
        cache_dir=tmp_path / "cache",
        **kwargs,
    )


def _toy(days=(D1, D2, D3)):
    units = [ToyUnit(i, (d,)) for i, d in enumerate(days, 1)]
    return ToySource(units, {i: _ticks(d, 10, 5.0 + i) for i, d in enumerate(days, 1)})


def _toy_store_with_floor(tmp_path) -> Path:
    cache = tmp_path / "cache"
    _export(_toy(), tmp_path, D1, D3)
    fl.set_floor(cache, "TOYUSD", D2, tmp_path / "backup")
    return cache


def test_the_export_does_not_bring_back_a_day_before_the_floor(tmp_path):
    cache = _toy_store_with_floor(tmp_path)
    source = _toy()

    _export(source, tmp_path, D1, D3)

    assert not ex._day_is_committed(cache, "TOYUSD", D1)
    assert source.fetched == []  # D2 and D3 are committed; D1 is not wanted
    assert all(D1 not in c[1] for c in source.calls if c[0] == "plan")
    days = [r.get("day") for r in _jsonl(ex._source_manifest_path(cache, "TOYUSD"))]
    assert "2024-01-02" not in days


def test_the_export_plans_only_the_days_on_and_after_the_floor(tmp_path):
    cache = tmp_path / "cache"
    (cache / "TOYUSD").mkdir(parents=True)
    fl.set_floor(cache, "TOYUSD", D2, tmp_path / "backup")
    source = _toy()

    bid_csv, _ = _export(source, tmp_path, D1, D3, resample_rule="1h")

    assert ("plan", (D2, D3)) in source.calls
    assert not ex._day_is_committed(cache, "TOYUSD", D1)
    assert ex._day_is_committed(cache, "TOYUSD", D2)
    assert bid_csv is not None
    assert list(pd.read_csv(bid_csv)["close"]) == [7.0, 8.0]  # no candle of D1


def test_a_range_wholly_before_the_floor_does_nothing(tmp_path):
    _toy_store_with_floor(tmp_path)
    source = _toy()

    assert _export(source, tmp_path, D1, D1, resample_rule="1h") == (None, None)

    assert source.calls == []


def test_a_reexport_before_the_floor_is_refused(tmp_path):
    _toy_store_with_floor(tmp_path)
    source = _toy()

    with pytest.raises(ex.ReexportError, match="before the symbol's floor 2024-01-03"):
        _export(source, tmp_path, D1, D1, reexport=True)

    assert source.calls == []


# ---------------------------------------------------------------------------
# Verify honours the floor
# ---------------------------------------------------------------------------


def _verify(tmp_path, *extra):
    return verify_cli.main(
        ["--reference", str(tmp_path / "ref"), "--cache-dir", str(tmp_path / "cache")]
        + ["--symbols", SYMBOL, "--from", FIRST.isoformat(), "--to", LAST.isoformat(), *extra]
    )


def test_verify_does_not_report_the_days_before_the_floor(tmp_path, capsys):
    _store(tmp_path / "ref")
    _store(tmp_path / "cache")
    _floor(tmp_path)
    capsys.readouterr()

    assert _verify(tmp_path, "--format", "json") == 0

    [rep] = json.loads(capsys.readouterr().out)["symbols"]
    assert rep["floor"] == "2024-01-15"
    assert rep["findings"] == []
    assert rep["years"]["2024"]["counts"]["only-left"] == 0
    assert [d["day"] for d in rep["days"]] == [d.isoformat() for d in _days(FLOOR, LAST)]


def test_verify_of_a_range_wholly_before_the_floor_has_no_no_overlap_finding(tmp_path, capsys):
    _store(tmp_path / "ref")
    _store(tmp_path / "cache")
    _floor(tmp_path)
    capsys.readouterr()

    code = verify_cli.main(
        ["--reference", str(tmp_path / "ref"), "--cache-dir", str(tmp_path / "cache")]
        + ["--symbols", SYMBOL, "--from", FIRST.isoformat()]
        + ["--to", (FLOOR - timedelta(days=1)).isoformat(), "--format", "json"]
    )

    assert code == 0
    [rep] = json.loads(capsys.readouterr().out)["symbols"]
    assert rep["floor"] == "2024-01-15"
    assert rep["compared"] == 0
    assert rep["days"] == []
    assert rep["findings"] == []


def test_verify_names_the_floor_in_its_text_report(tmp_path, capsys):
    _store(tmp_path / "ref")
    _store(tmp_path / "cache")
    _floor(tmp_path)
    capsys.readouterr()

    assert _verify(tmp_path) == 0

    out = capsys.readouterr().out
    assert f"Floors (days before them not compared):\n  {SYMBOL} 2024-01-15" in out
    assert "Days compared (data on both sides): 17" in out


def test_without_a_floor_verify_reports_the_missing_days(tmp_path, capsys):
    """The control: the same store with the early days gone but no floor."""
    _store(tmp_path / "ref")
    _store(tmp_path / "cache")
    _floor(tmp_path)
    (tmp_path / "cache" / SYMBOL / "_floor.json").unlink()
    capsys.readouterr()

    _verify(tmp_path, "--format", "json")

    [rep] = json.loads(capsys.readouterr().out)["symbols"]
    assert rep["floor"] is None
    assert rep["years"]["2024"]["counts"]["only-left"] == 14
