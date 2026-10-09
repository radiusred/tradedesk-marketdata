"""
The HistData symbol map is configuration: the shipped TOML example, the loader's
schema checks, --symbol-map, the verified-scale field and exclusion spans.
"""

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from histdata_fixtures import FakeFetcher, weekday_lines, zips_by_month

import tradedesk_marketdata.cli as cli
import tradedesk_marketdata.export as ex
import tradedesk_marketdata.parallel as par
from tradedesk_marketdata.cancel import cancellation
from tradedesk_marketdata.parallel import ExportResult
from tradedesk_marketdata.sources import histdata as hd

ENTRY = 'name = "SPXUSD"\nscale = 1\nfirst_month = "2010-11"\n'


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(hd, "REQUEST_INTERVAL", 0.0)
    cancellation.clear()
    yield
    cancellation.clear()


def _map(tmp_path: Path, body: str, name: str = "map.toml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def test_the_shipped_map_holds_the_25_instruments() -> None:
    shipped = hd.load_symbol_map()
    assert shipped == hd.HISTDATA_SYMBOLS
    assert len(shipped) == 25
    assert shipped["USA500IDXUSD"] == hd.HistDataInstrument(
        "SPXUSD", 1.0, (2010, 11), True, "Dukascopy median close ~4782"
    )
    assert (shipped["EURUSD"].scale, shipped["EURUSD"].first_month) == (1e4, (2000, 5))
    assert (shipped["USDJPY"].scale, shipped["XAUUSD"].scale) == (1e2, 1e2)
    # The one shipped exclusion span: GRXEUR's Euro Stoxx 50 years.
    assert [s for s, inst in shipped.items() if inst.exclude] == ["DEUIDXEUR"]
    assert shipped["DEUIDXEUR"].exclude == (
        hd.ExcludedSpan(
            date(2020, 6, 17),
            date(2023, 12, 5),
            "GRXEUR carries Euro Stoxx 50 levels, not the DAX",
        ),
    )


def test_every_shipped_entry_carries_its_figure_or_is_unverified() -> None:
    verified = {s: i.scale_evidence for s, i in hd.HISTDATA_SYMBOLS.items() if i.scale_verified}
    assert verified == {
        "USA500IDXUSD": "Dukascopy median close ~4782",
        "BRENTCMDUSD": "Dukascopy 2022-02-21: raw 9712.8 for 97.128",
        "XAUUSD": "Dukascopy median close ~151948",
        "EURUSD": "Dukascopy median close ~11217",
        "USDJPY": "Dukascopy median close ~12029",
    }
    assert all(not i.scale_evidence for i in hd.HISTDATA_SYMBOLS.values() if not i.scale_verified)


def test_brent_is_scaled_into_dukascopy_cents() -> None:
    brent = hd.HISTDATA_SYMBOLS["BRENTCMDUSD"]
    assert (brent.name, brent.scale, brent.scale_verified) == ("BCOUSD", 100.0, True)


def test_the_example_ships_in_the_package() -> None:
    path = Path(hd.__file__).with_name(hd.SYMBOL_MAP_EXAMPLE)
    assert path.is_file()
    assert hd.load_symbol_map(path) == hd.HISTDATA_SYMBOLS


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def test_a_custom_map_with_spans_loads(tmp_path) -> None:
    path = _map(
        tmp_path,
        "[symbols.USA500IDXUSD]\n" + ENTRY + "scale_verified = true\n"
        'scale_evidence = "checked"\n'
        'exclude = [{ from = 2020-06-17, to = 2020-06-19, reason = "wrong index" }]\n',
    )
    [(symbol, inst)] = hd.load_symbol_map(path).items()
    assert symbol == "USA500IDXUSD" and inst.scale_verified
    assert inst.exclude == (hd.ExcludedSpan(date(2020, 6, 17), date(2020, 6, 19), "wrong index"),)
    assert inst.excluded(date(2020, 6, 18)).reason == "wrong index"
    assert inst.excluded(date(2020, 6, 20)) is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[symbols.USA500IDXUSD]\n" + ENTRY + "colour = 1\n", "unknown key(s) colour"),
        (
            '[symbols.USA500IDXUSD]\nname = "SPXUSD"\nscale = 1\nfirst_month = "2010-13"\n',
            "YYYY-MM",
        ),
        (
            '[symbols.USA500IDXUSD]\nname = "SPXUSD"\nscale = 0\nfirst_month = "2010-11"\n',
            "above zero",
        ),
        ('[symbols.USA500IDXUSD]\nscale = 1\nfirst_month = "2010-11"\n', "instrument code"),
        ("[symbols.USA500IDXUSD]\n" + ENTRY + 'scale_verified = "yes"\n', "true or false"),
        (
            "[symbols.USA500IDXUSD]\n" + ENTRY + "scale_verified = true\n",
            "needs the scale_evidence",
        ),
        (
            "[symbols.USA500IDXUSD]\n" + ENTRY + "exclude = [{ from = 2020-06-19, "
            'to = 2020-06-17, reason = "x" }]\n',
            "ends before it starts",
        ),
        ("[symbols.USA500IDXUSD]\n" + ENTRY + "exclude = [{ from = 2020-06-19 }]\n", "exactly"),
        ("[symbols.usa500idxusd]\n" + ENTRY, "write the cache symbol as USA500IDXUSD"),
        ("[other]\nx = 1\n", "unknown top-level key(s) other"),
        ("[symbols]\n", "no [symbols.<SYMBOL>] entries"),
        ("not = [toml\n", "not a TOML file"),
    ],
)
def test_a_map_that_breaks_the_schema_is_refused(tmp_path, body, message) -> None:
    with pytest.raises(hd.SymbolMapError, match=re.escape(message)):
        hd.load_symbol_map(_map(tmp_path, body))


def test_an_unreadable_map_is_refused(tmp_path) -> None:
    with pytest.raises(hd.SymbolMapError, match="cannot read the symbol map"):
        hd.load_symbol_map(tmp_path / "missing.toml")


# ---------------------------------------------------------------------------
# --symbol-map
# ---------------------------------------------------------------------------


def _capture_tasks(monkeypatch):
    captured: list[par.ExportTask] = []

    def fake_run_parallel_exports(tasks, max_workers):
        captured.extend(tasks)
        return [ExportResult(symbol=t.symbol, output_csvs=[], success=True) for t in tasks]

    monkeypatch.setattr(par, "run_parallel_exports", fake_run_parallel_exports)
    return captured


_RANGE = ["--from", "2015-01-01", "--to", "2015-01-31", "--no-cache"]


def test_symbol_map_selects_the_mapping(monkeypatch, tmp_path) -> None:
    path = _map(
        tmp_path, '[symbols.MYINDEX]\nname = "SPXUSD"\nscale = 10\nfirst_month = "2012-01"\n'
    )
    captured = _capture_tasks(monkeypatch)

    rc = cli.main(
        ["--source", "histdata", "--symbol-map", str(path), "--symbols", "MYINDEX", *_RANGE]
    )

    assert rc == 0
    inst = captured[0].source.instrument("MYINDEX")
    assert (inst.name, inst.scale, inst.first_month) == ("SPXUSD", 10.0, (2012, 1))


def test_a_symbol_the_given_map_lacks_is_refused(monkeypatch, tmp_path, capsys) -> None:
    path = _map(tmp_path, "[symbols.USA500IDXUSD]\n" + ENTRY)
    captured = _capture_tasks(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["--source", "histdata", "--symbol-map", str(path), "--symbols", "EURUSD", *_RANGE]
        )

    assert exc.value.code == 2
    assert "EURUSD has no HistData mapping; mapped symbols: USA500IDXUSD" in capsys.readouterr().err
    assert captured == []


def test_a_bad_map_is_a_usage_error(monkeypatch, tmp_path, capsys) -> None:
    path = _map(tmp_path, "[symbols.USA500IDXUSD]\n" + ENTRY + "colour = 1\n")
    _capture_tasks(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "--source",
                "histdata",
                "--symbol-map",
                str(path),
                "--symbols",
                "USA500IDXUSD",
                *_RANGE,
            ]
        )

    assert exc.value.code == 2
    assert "unknown key(s) colour" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Exports: unverified scales and exclusion spans
# ---------------------------------------------------------------------------


def _export(
    cache: Path, first: date, last: date, symbol_map: Path | None = None, symbol="USA500IDXUSD"
):
    return hd.export_range_histdata(
        symbol=symbol,
        start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
        end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
        out=cache.parent / "out",
        resample_rule=None,
        cache_dir=cache,
        symbol_map=symbol_map,
    )


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _records(cache: Path, symbol="USA500IDXUSD") -> list[dict]:
    path = ex._source_manifest_path(cache, symbol)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def jan_2015(monkeypatch):
    fetch = FakeFetcher(zips_by_month(weekday_lines(date(2014, 12, 1), date(2015, 2, 27))))
    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    return fetch


def test_an_unverified_scale_is_warned_about(jan_2015, tmp_path, caplog) -> None:
    with caplog.at_level("WARNING"):
        _export(tmp_path / "cache", date(2015, 1, 5), date(2015, 1, 5), symbol="EURSEK")
    assert "EURSEK: the symbol map's scale 10000 for HistData EURSEK is not verified" in caplog.text


def test_a_verified_scale_is_not_warned_about(jan_2015, tmp_path, caplog) -> None:
    with caplog.at_level("WARNING"):
        _export(tmp_path / "cache", date(2015, 1, 5), date(2015, 1, 5))
    assert "not verified" not in caplog.text


def test_days_in_an_exclusion_span_are_recorded_not_committed(jan_2015, tmp_path) -> None:
    span = 'exclude = [{ from = 2015-01-07, to = 2015-01-08, reason = "another index" }]\n'
    path = _map(tmp_path, "[symbols.USA500IDXUSD]\n" + ENTRY + span)
    cache = tmp_path / "cache"

    _export(cache, date(2015, 1, 5), date(2015, 1, 9), symbol_map=path)

    committed = [
        d
        for d in _days(date(2015, 1, 5), date(2015, 1, 9))
        if ex._day_is_committed(cache, "USA500IDXUSD", d)
    ]
    assert committed == [date(2015, 1, 5), date(2015, 1, 6), date(2015, 1, 9)]
    excluded = [r for r in _records(cache) if r.get("status") == "excluded"]
    assert [(r["day"], r["reason"], r["source"]) for r in excluded] == [
        ("2015-01-07", "another index", "histdata"),
        ("2015-01-08", "another index", "histdata"),
    ]
    assert excluded[0]["source_unit"] == "HISTDATA_COM_ASCII_SPXUSD_T201501.zip"

    # Re-run with the span still configured: nothing to fetch, nothing re-recorded.
    jan_2015.calls.clear()
    _export(cache, date(2015, 1, 5), date(2015, 1, 9), symbol_map=path)
    assert jan_2015.calls == []
    assert len([r for r in _records(cache) if r.get("status") == "excluded"]) == 2

    # The span removed from the map: the days are pending again and get committed
    # (from January's zip, still staged because February 1st is not committed).
    lifted = _map(tmp_path, "[symbols.USA500IDXUSD]\n" + ENTRY, "lifted.toml")
    _export(cache, date(2015, 1, 5), date(2015, 1, 9), symbol_map=lifted)
    assert ex._day_is_committed(cache, "USA500IDXUSD", date(2015, 1, 7))
    assert ex._day_is_committed(cache, "USA500IDXUSD", date(2015, 1, 8))


def test_the_shipped_deuidxeur_span_is_honoured(monkeypatch, tmp_path, caplog) -> None:
    # DAX-like ticks around the span's first day; the shipped map, no --symbol-map.
    lines = weekday_lines(date(2020, 6, 1), date(2020, 6, 30), price=12300.0)
    fetch = FakeFetcher(zips_by_month(lines))
    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    cache = tmp_path / "cache"

    with caplog.at_level("INFO"):
        _export(cache, date(2020, 6, 15), date(2020, 6, 18), symbol="DEUIDXEUR")

    committed = [
        d
        for d in _days(date(2020, 6, 15), date(2020, 6, 18))
        if ex._day_is_committed(cache, "DEUIDXEUR", d)
    ]
    assert committed == [date(2020, 6, 15), date(2020, 6, 16)]
    excluded = [r for r in _records(cache, "DEUIDXEUR") if r.get("status") == "excluded"]
    assert [r["day"] for r in excluded] == ["2020-06-17", "2020-06-18"]
    assert {r["reason"] for r in excluded} == {"GRXEUR carries Euro Stoxx 50 levels, not the DAX"}
    assert "excluded=2" in caplog.text
