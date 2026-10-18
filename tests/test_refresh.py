"""tradedesk-md-refresh (M19): window re-check, HistData top-up, Dukascopy fallback, backups,
dry run and report.

Every fixture is labelled synthetic data: the symbol ``TESTSYM`` with a test-only HistData
symbol map, deterministic EURUSD-like ticks from ``source_feeds.synthetic_ticks``, and the
providers' network calls stubbed. No test makes a network request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from histdata_fixtures import FakeFetcher, est_line, zips_by_month
from source_feeds import TickRow, dukascopy_feed, synthetic_ticks

from tradedesk_marketdata import refresh as rf
from tradedesk_marketdata import refresh_cli
from tradedesk_marketdata.cancel import cancellation
from tradedesk_marketdata.export import (
    _daily_candle_path,
    _load_daily_candles,
    _partial_day_manifest_path,
    _source_manifest_path,
    export_range,
)
from tradedesk_marketdata.sources import dukascopy as dk
from tradedesk_marketdata.sources import histdata as hd

SYMBOL = "TESTSYM"  # synthetic: no real instrument
SYMBOL_MAP = """# Test-only HistData symbol map for the synthetic TESTSYM (decimal quotes x 1e4).
[symbols.TESTSYM]
name = "TESTSYM"
scale = 10000
first_month = "2014-01"
"""

JAN5, JAN9, JAN14, JAN15, JAN16, JAN23 = (
    date(2015, 1, 5),
    date(2015, 1, 9),
    date(2015, 1, 14),
    date(2015, 1, 15),
    date(2015, 1, 16),
    date(2015, 1, 23),
)


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(hd, "REQUEST_INTERVAL", 0.0)
    cancellation.clear()


@pytest.fixture
def symbol_map(tmp_path) -> Path:
    path = tmp_path / "symbols.toml"
    path.write_text(SYMBOL_MAP, encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path) -> Path:
    path = tmp_path / "store"
    path.mkdir()
    return path


def ticks(first: date, last: date, *, without: tuple[date, ...] = ()) -> list[TickRow]:
    return [t for t in synthetic_ticks(first, last) if t[0].date() not in without]


def install_histdata(monkeypatch, rows: list[TickRow]) -> FakeFetcher:
    lines = [est_line(ts, f"{b / 1e5:.6f}", f"{a / 1e5:.6f}") for ts, b, a in rows]
    fetcher = FakeFetcher(zips_by_month(lines))
    monkeypatch.setattr(hd, "_fetch_month_zip", fetcher)
    return fetcher


def seed(store: Path, symbol_map: Path, first: date, last: date) -> None:
    """Fill the store from the installed HistData stub, as tradedesk-md-export would."""
    export_range(
        source=hd.HistDataSource({"symbol_map": symbol_map}),
        symbol=SYMBOL,
        start_utc=datetime(first.year, first.month, first.day, tzinfo=UTC),
        end_utc_inclusive=datetime(last.year, last.month, last.day, tzinfo=UTC),
        out=store / "_out",
        resample_rule=None,
        cache_dir=store,
    )


def rows(store: Path, day: date) -> int:
    df = _load_daily_candles(_daily_candle_path(store, SYMBOL, day, "bid"))
    assert df is not None
    return len(df)


def manifest(store: Path) -> list[dict]:
    path = _source_manifest_path(store, SYMBOL)
    return [json.loads(line) for line in path.read_text().splitlines()]


def tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run(store: Path, symbol_map: Path, tmp_path: Path, *args: str) -> tuple[int, dict]:
    report = tmp_path / "report.json"
    argv = [
        "--cache-dir",
        str(store),
        "--symbol-map",
        str(symbol_map),
        "--report",
        str(report),
        *args,
    ]
    if "--dry-run" not in args and "--backup-dir" not in args:
        argv += ["--backup-dir", str(tmp_path / "backups")]
    rc = refresh_cli.main(argv)
    return rc, json.loads(report.read_text()) if report.exists() else {}


def sym(report: dict) -> dict:
    (entry,) = report["symbols"]
    assert entry["symbol"] == SYMBOL
    return entry


# ---------------------------------------------------------------------------
# M19-R1: add missing days; never rewrite a complete day
# ---------------------------------------------------------------------------


def test_missing_days_are_added_and_complete_days_are_not_rewritten(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN23))
    seed(store, symbol_map, JAN5, JAN16)
    before = {
        d: _daily_candle_path(store, SYMBOL, d, "bid").read_bytes() for d in (JAN5, JAN9, JAN16)
    }
    lines_before = len(manifest(store))

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-23")

    assert rc == 0
    s = sym(report)
    added = [a["day"] for a in s["added"]]
    assert added == [f"2015-01-{d}" for d in range(17, 24)]
    assert {a["source"] for a in s["added"]} == {"histdata"}
    assert s["reexported"] == [] and s["unchanged"] == []
    assert s["newest_day"] == "2015-01-23"
    for d, data in before.items():
        assert _daily_candle_path(store, SYMBOL, d, "bid").read_bytes() == data
    new = manifest(store)[lines_before:]
    assert [r["day"] for r in new] == added  # one line per added day, none for complete days
    assert report["changed"] is True and s["changed"] is True


def test_symbols_default_to_every_mapped_symbol_in_the_store(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN16))
    seed(store, symbol_map, JAN5, JAN16)
    (store / "OTHERSYM" / "2015").mkdir(parents=True)  # in the store, not in the map

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-16")

    assert rc == 0
    assert [s["symbol"] for s in report["symbols"]] == [SYMBOL]
    assert report["skipped_symbols"] == {"OTHERSYM": "no histdata mapping"}


# ---------------------------------------------------------------------------
# M19-R2: empty and partial weekdays, one month fetch, identical days, closures
# ---------------------------------------------------------------------------


def test_corrected_empty_days_are_reexported_from_one_month_fetch(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14, JAN15)))
    seed(store, symbol_map, JAN5, JAN23)
    assert rows(store, JAN14) == 0 and rows(store, JAN15) == 0
    lines_before = len(manifest(store))
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN23))  # HistData corrected them

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-23")

    assert rc == 0
    assert fetcher.calls == [(2015, 1)]  # one month file for both days
    s = sym(report)
    assert s["reexported"] == [
        {"day": "2015-01-14", "source": "histdata"},
        {"day": "2015-01-15", "source": "histdata"},
    ]
    assert s["added"] == [] and s["still_empty"] == []
    assert rows(store, JAN14) > 0 and rows(store, JAN15) > 0
    new = manifest(store)[lines_before:]
    assert [(r["day"], r["source"], r.get("reexport")) for r in new] == [
        ("2015-01-14", "histdata", True),
        ("2015-01-15", "histdata", True),
    ]


def test_an_empty_day_the_source_still_lacks_is_left_and_reported_still_empty(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    sha = hashlib.sha256(_source_manifest_path(store, SYMBOL).read_bytes()).hexdigest()

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-23")

    assert rc == 0
    s = sym(report)
    assert s["still_empty"] == ["2015-01-14"]
    assert s["reexported"] == [] and s["changed"] is False
    assert s["sources_sha256_before"] == s["sources_sha256_after"] == sha


def test_a_partial_day_with_identical_fresh_data_is_left_alone(
    monkeypatch, store, symbol_map, tmp_path, capsys
):
    install_histdata(monkeypatch, ticks(JAN5, JAN16))
    seed(store, symbol_map, JAN5, JAN16)
    # The store records JAN14 as partial (as a commit with a missing month would).
    with open(_partial_day_manifest_path(store, SYMBOL), "a") as f:
        rec = {
            "day": JAN14.isoformat(),
            "missing_hours": [0],
            "gap_reason": "test",
            "committed_at": datetime.now(UTC).isoformat(),
        }
        f.write(json.dumps(rec) + "\n")
    bid = _daily_candle_path(store, SYMBOL, JAN14, "bid").read_bytes()
    sources = _source_manifest_path(store, SYMBOL).read_bytes()
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN16))

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-16")

    assert rc == 0
    assert fetcher.calls == [(2015, 1)]  # the partial day was rechecked
    s = sym(report)
    assert s["unchanged"] == ["2015-01-14"]
    assert s["reexported"] == [] and s["added"] == []
    assert _daily_candle_path(store, SYMBOL, JAN14, "bid").read_bytes() == bid
    assert _source_manifest_path(store, SYMBOL).read_bytes() == sources  # no new line
    assert report["changed"] is False
    assert "no change" in capsys.readouterr().out
    assert not (tmp_path / "backups").exists() or not any((tmp_path / "backups").rglob("*.zst"))


def test_partial_state_ends_with_a_later_full_commit(store):
    sym_dir = store / SYMBOL
    sym_dir.mkdir()
    (sym_dir / "_partial_days.jsonl").write_text(
        json.dumps({"day": "2015-01-14", "committed_at": "2015-02-01T00:00:01+00:00"}) + "\n"
    )
    (sym_dir / "_sources.jsonl").write_text(
        json.dumps({"day": "2015-01-14", "committed_at": "2015-02-01T00:00:00+00:00"})
        + "\n"
        + json.dumps({"day": "2015-01-15", "committed_at": "2015-02-01T00:00:00+00:00"})
        + "\n"
    )
    assert rf.partial_days(store, SYMBOL) == {JAN14}
    with open(sym_dir / "_sources.jsonl", "a") as f:
        rec = {"day": "2015-01-14", "committed_at": "2015-03-01T00:00:00+00:00", "reexport": True}
        f.write(json.dumps(rec) + "\n")
    assert rf.partial_days(store, SYMBOL) == set()


def test_closure_days_are_skipped_not_rechecked(monkeypatch, store, symbol_map, tmp_path):
    first, last = date(2014, 12, 22), date(2015, 1, 9)
    closed = (date(2014, 12, 25), date(2015, 1, 1))
    install_histdata(monkeypatch, ticks(first, last, without=closed))
    seed(store, symbol_map, first, last)
    assert all(rows(store, d) == 0 for d in closed)
    fetcher = install_histdata(monkeypatch, ticks(first, last))  # a source that now has them

    rc, report = run(store, symbol_map, tmp_path, "--since", "2014-12-22", "--until", "2015-01-09")

    assert rc == 0
    assert fetcher.calls == []  # nothing to add or recheck: no request
    s = sym(report)
    assert s["skipped_closures"] == ["2014-12-25", "2015-01-01"]
    assert s["still_empty"] == [] and s["reexported"] == []


def test_a_further_closure_day_is_taken_from_the_command_line(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN16, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN16)
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN16))

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-16",
        "--closure",
        "01-14",
    )

    assert rc == 0 and fetcher.calls == []
    assert sym(report)["skipped_closures"] == ["2015-01-14"]


# ---------------------------------------------------------------------------
# M19-R3: the Dukascopy fallback
# ---------------------------------------------------------------------------


def test_fallback_fills_a_still_empty_day_with_the_operators_divisor(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    feed = dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))
    timeouts = []
    stub = dk._SESSION.get

    def get(url, *, timeout):
        timeouts.append(timeout)
        return stub(url, timeout=timeout)

    monkeypatch.setattr(dk._SESSION, "get", get)

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-23",
        "--fallback-source",
        "dukascopy",
        "--price-divisor",
        "TESTSYM=10",
        "--connect-timeout",
        "3.5",
        "--read-timeout",
        "21",
    )

    assert rc == 0
    s = sym(report)
    assert s["reexported"] == [{"day": "2015-01-14", "source": "dukascopy"}]
    assert s["still_empty"] == []
    assert len(feed.requests) == 24  # the one day's hours, nothing else
    assert set(timeouts) == {(3.5, 21.0)}
    last = manifest(store)[-1]
    assert (last["day"], last["source"], last["reexport"]) == ("2015-01-14", "dukascopy", True)
    # The fallback day sits at the store's scale: same level as its HistData neighbours.
    mid = _load_daily_candles(_daily_candle_path(store, SYMBOL, JAN14, "bid"))
    nxt = _load_daily_candles(_daily_candle_path(store, SYMBOL, JAN15, "bid"))
    assert mid is not None and nxt is not None
    assert 0.9 < mid["close"].median() / nxt["close"].median() < 1.1


def test_fallback_divisors_come_from_a_toml_file(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))
    divisors = tmp_path / "divisors.toml"
    divisors.write_text("# synthetic\nTESTSYM = 10\n")

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-23",
        "--fallback-source",
        "dukascopy",
        "--price-divisors",
        str(divisors),
    )

    assert rc == 0
    assert sym(report)["reexported"] == [{"day": "2015-01-14", "source": "dukascopy"}]


def test_scale_sentry_refuses_a_fallback_day_at_the_wrong_scale(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))
    bid = _daily_candle_path(store, SYMBOL, JAN14, "bid").read_bytes()

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-23",
        "--fallback-source",
        "dukascopy",
        "--price-divisor",
        "TESTSYM=1",  # ten times the store's units
    )

    assert rc == 1
    s = sym(report)
    assert s["refused"] == [{"day": "2015-01-14", "source": "dukascopy", "reason": "scale_sentry"}]
    assert s["reexported"] == [] and s["still_empty"] == ["2015-01-14"]
    assert _daily_candle_path(store, SYMBOL, JAN14, "bid").read_bytes() == bid


def test_no_dukascopy_request_without_the_fallback_flag(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    feed = dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-23")

    assert rc == 0 and feed.requests == []
    assert sym(report)["still_empty"] == ["2015-01-14"]


def test_fallback_only_limits_the_fallback_to_named_days(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14, JAN15)))
    seed(store, symbol_map, JAN5, JAN23)
    feed = dukascopy_feed(monkeypatch, ticks(JAN14, JAN15))
    days = tmp_path / "days.txt"
    days.write_text("# synthetic day list\n2015-01-15\n")

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-23",
        "--fallback-source",
        "dukascopy",
        "--price-divisor",
        "TESTSYM=10",
        "--fallback-only",
        str(days),
    )

    assert rc == 0
    s = sym(report)
    assert s["reexported"] == [{"day": "2015-01-15", "source": "dukascopy"}]
    assert s["still_empty"] == ["2015-01-14"]
    assert all("/2015/00/15/" in url for url in feed.requests)


def test_fallback_without_a_divisor_is_refused_before_any_request(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN16))
    seed(store, symbol_map, JAN5, JAN16)
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN16))
    with pytest.raises(SystemExit) as e:
        run(
            store,
            symbol_map,
            tmp_path,
            "--since",
            "2015-01-05",
            "--fallback-source",
            "dukascopy",
        )
    assert e.value.code == 2 and fetcher.calls == []


def test_fallback_options_need_the_fallback_flag(store, symbol_map, tmp_path):
    with pytest.raises(SystemExit) as e:
        run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--price-divisor", "TESTSYM=10")
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# M19-R4: backups, the no-backup refusal and the dry run
# ---------------------------------------------------------------------------


def test_backup_holds_the_manifests_and_every_changed_day_file(
    monkeypatch, store, symbol_map, tmp_path
):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN16)
    old = {
        "_sources.jsonl": _source_manifest_path(store, SYMBOL).read_bytes(),
        "2015/00/14_bid.csv.zst": _daily_candle_path(store, SYMBOL, JAN14, "bid").read_bytes(),
        "2015/00/14_ask.csv.zst": _daily_candle_path(store, SYMBOL, JAN14, "ask").read_bytes(),
    }
    install_histdata(monkeypatch, ticks(JAN5, JAN23))
    backups = tmp_path / "backups"

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-23")

    assert rc == 0
    s = sym(report)
    assert [r["day"] for r in s["reexported"]] == ["2015-01-14"]
    assert len(s["added"]) == 7
    (run_dir,) = backups.iterdir()
    assert report["backup_dir"] == str(run_dir.absolute())
    copied = {
        str(p.relative_to(run_dir / SYMBOL)): p.read_bytes()
        for p in (run_dir / SYMBOL).rglob("*")
        if p.is_file()
    }
    assert copied == old  # an added day had no files to copy


def test_a_run_without_a_backup_dir_is_refused(monkeypatch, store, symbol_map, tmp_path):
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN16))
    with pytest.raises(SystemExit) as e:
        refresh_cli.main(
            ["--cache-dir", str(store), "--symbol-map", str(symbol_map), "--since", "2015-01-05"]
        )
    assert e.value.code == 2 and fetcher.calls == []
    with pytest.raises(rf.RefreshError):
        rf.refresh(
            cache_dir=store,
            symbols=[SYMBOL],
            since=JAN5,
            until=JAN16,
            primary=hd.HistDataSource({"symbol_map": symbol_map}),
        )
    assert fetcher.calls == []


def test_a_backup_dir_inside_the_store_is_refused(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN5, JAN16, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN16)
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN16))
    digest = tree_digest(store)

    rc, _ = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-16",
        "--backup-dir",
        str(store / "_backup"),
    )

    assert rc == 1 and fetcher.calls == []
    assert tree_digest(store) == digest


def test_dry_run_reports_the_plan_and_writes_nothing(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN16)
    fetcher = install_histdata(monkeypatch, ticks(JAN5, JAN23))
    feed = dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))
    digest = tree_digest(store)

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-23",
        "--dry-run",
        "--fallback-source",
        "dukascopy",
        "--price-divisor",
        "TESTSYM=10",
    )

    assert rc == 0
    assert fetcher.calls == [] and feed.requests == []
    assert tree_digest(store) == digest
    assert not (tmp_path / "backups").exists()
    s = sym(report)
    assert report["dry_run"] is True and report["changed"] is False
    assert s["would_add"] == [f"2015-01-{d}" for d in range(17, 24)]
    assert s["would_recheck"] == ["2015-01-14"]
    assert s["would_fallback"] == ["2015-01-14", *[f"2015-01-{d}" for d in range(19, 24)]]


# ---------------------------------------------------------------------------
# M19-R5: the report
# ---------------------------------------------------------------------------


def test_report_schema_and_sources_digests(monkeypatch, store, symbol_map, tmp_path, capsys):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    before = hashlib.sha256(_source_manifest_path(store, SYMBOL).read_bytes()).hexdigest()
    install_histdata(monkeypatch, ticks(JAN5, JAN23))

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-23")

    assert rc == 0
    after = hashlib.sha256(_source_manifest_path(store, SYMBOL).read_bytes()).hexdigest()
    assert set(report) == {
        "schema_version",
        "tool_version",
        "generated_at",
        "since",
        "until",
        "dry_run",
        "primary_source",
        "fallback_source",
        "keep_raw",
        "backup_dir",
        "changed",
        "symbols",
        "skipped_symbols",
    }
    assert report["schema_version"] == 1
    assert (report["since"], report["until"]) == ("2015-01-05", "2015-01-23")
    assert (report["primary_source"], report["fallback_source"]) == ("histdata", None)
    s = sym(report)
    assert set(s) == {
        "symbol",
        "changed",
        "added",
        "reexported",
        "unchanged",
        "still_empty",
        "skipped_closures",
        "refused",
        "excluded",
        "newest_day",
        "sources_sha256_before",
        "sources_sha256_after",
        "error",
    }
    assert (s["sources_sha256_before"], s["sources_sha256_after"]) == (before, after)
    assert before != after and s["changed"] is True
    out = capsys.readouterr().out
    assert "tradedesk-md-refresh: changed: 0 day(s) added, 1 re-exported" in out


def test_a_run_with_no_change_says_so(monkeypatch, store, symbol_map, tmp_path, capsys):
    install_histdata(monkeypatch, ticks(JAN5, JAN16))
    seed(store, symbol_map, JAN5, JAN16)

    rc, report = run(store, symbol_map, tmp_path, "--since", "2015-01-05", "--until", "2015-01-16")

    assert rc == 0
    assert report["changed"] is False and sym(report)["changed"] is False
    assert "tradedesk-md-refresh: no change:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# M25-R1..R3: --keep-raw
# ---------------------------------------------------------------------------


# A month file is retired once every day it feeds is committed: January's feeds 2015-01-01 to
# 2015-02-01, so the window runs to 02-01 and the ticks run past it.
JAN1, FEB6 = date(2015, 1, 1), date(2015, 2, 6)
MONTH = ("--since", "2015-01-01", "--until", "2015-02-01")


def staged_jan(store: Path) -> bool:
    return hd._zip_path(store, SYMBOL, (2015, 1)).exists()


def raw_files(store: Path, source: str) -> dict[str, str]:
    root = store / SYMBOL / "_raw" / source
    return tree_digest(root) if root.exists() else {}


def test_keep_raw_keeps_the_primary_month_file(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN1, FEB6))
    seed(store, symbol_map, JAN1, JAN9)
    assert not (store / SYMBOL / "_raw").exists()

    rc, report = run(store, symbol_map, tmp_path, *MONTH, "--keep-raw")

    assert rc == 0 and sym(report)["added"]
    assert report["keep_raw"] is True
    assert list(raw_files(store, "histdata")) == ["201501.zip"]
    assert not staged_jan(store)  # moved, not copied


def test_keep_raw_keeps_the_fallback_units(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN5, JAN23, without=(JAN14,)))
    seed(store, symbol_map, JAN5, JAN23)
    dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        "--since",
        "2015-01-05",
        "--until",
        "2015-01-23",
        "--fallback-source",
        "dukascopy",
        "--price-divisor",
        "TESTSYM=10",
        "--keep-raw",
    )

    assert rc == 0
    assert sym(report)["reexported"] == [{"day": "2015-01-14", "source": "dukascopy"}]
    kept = raw_files(store, "dukascopy")
    # Dukascopy's months are zero-based: January is 00.
    assert sorted(kept) == [f"2015/00/14/{h:02d}h_ticks.bi5" for h in range(24)]


def test_without_keep_raw_no_raw_unit_is_kept(monkeypatch, store, symbol_map, tmp_path):
    install_histdata(monkeypatch, ticks(JAN1, FEB6, without=(JAN14,)))
    seed(store, symbol_map, JAN1, JAN16)
    dukascopy_feed(monkeypatch, ticks(JAN14, JAN14))

    rc, report = run(
        store,
        symbol_map,
        tmp_path,
        *MONTH,
        "--fallback-source",
        "dukascopy",
        "--price-divisor",
        "TESTSYM=10",
    )

    assert rc == 0 and report["keep_raw"] is False
    s = sym(report)
    assert s["added"] and s["reexported"] == [{"day": "2015-01-14", "source": "dukascopy"}]
    assert not (store / SYMBOL / "_raw").exists()
    assert not staged_jan(store)  # deleted, as before


@pytest.mark.parametrize("first_run", ["refresh", "export"])
def test_a_kept_unit_survives_a_later_refresh_without_keep_raw(
    monkeypatch, store, symbol_map, tmp_path, first_run
):
    install_histdata(monkeypatch, ticks(JAN1, FEB6, without=(JAN14,)))
    if first_run == "refresh":
        seed(store, symbol_map, JAN1, JAN9)
        rc, _ = run(store, symbol_map, tmp_path, *MONTH, "--keep-raw")
        assert rc == 0
    else:  # tradedesk-md-export --keep-raw
        export_range(
            source=hd.HistDataSource({"symbol_map": symbol_map}),
            symbol=SYMBOL,
            start_utc=datetime(2015, 1, 1, tzinfo=UTC),
            end_utc_inclusive=datetime(2015, 2, 1, tzinfo=UTC),
            out=store / "_out",
            resample_rule=None,
            cache_dir=store,
            keep_raw=True,
        )
    kept = raw_files(store, "histdata")
    assert list(kept) == ["201501.zip"]
    assert rows(store, JAN14) == 0
    fetcher = install_histdata(monkeypatch, ticks(JAN1, FEB6))  # HistData corrected JAN14

    rc, report = run(store, symbol_map, tmp_path, *MONTH)

    assert rc == 0 and report["keep_raw"] is False
    assert fetcher.calls == [(2015, 1)]  # the month was fetched again and its days committed
    assert sym(report)["reexported"] == [{"day": "2015-01-14", "source": "histdata"}]
    assert raw_files(store, "histdata") == kept  # not deleted, not moved, not replaced
    assert not staged_jan(store)  # the fresh copy was deleted, as a run without the option does


def test_keep_raw_is_in_the_help_text(capsys):
    with pytest.raises(SystemExit):
        refresh_cli.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--keep-raw" in out
    assert "{cache}/{SYMBOL}/_raw/{source}/" in out
