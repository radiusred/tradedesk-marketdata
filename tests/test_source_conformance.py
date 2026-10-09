"""
Provider conformance: every registered source, run through the framework on the
same synthetic ticks, writes the same cache.

Each source is fed the ticks in its own wire format by tests/source_feeds.py
(real bi5 hours through the real Dukascopy decoder; real month zips through the
real HistData decoder) and must produce:

- day files byte-identical to every other source's, and to the day files the
  code wrote before the source contract existed (the golden digests below);
- one provenance record per committed day, of the same shape, naming the source;
- the same range CSVs, and sidecars of the same shape;
- with ``--keep-raw``, retained raw units that a re-run decodes without a request.

Anything a provider does differently (its timestamps, units, gaps and defects)
is tested in that provider's own test modules, not here.
"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import zstandard as zstd
from source_feeds import FEEDS, SYMBOL, synthetic_ticks

import tradedesk_marketdata.cli as cli
import tradedesk_marketdata.export as ex
from tradedesk_marketdata import sources
from tradedesk_marketdata.cancel import cancellation

# A winter month (no DST question), with feed ticks either side of it so the
# 1st and the last day have their neighbouring month files.
FIRST, LAST = date(2016, 1, 1), date(2016, 1, 31)
FEED_FIRST, FEED_LAST = date(2015, 12, 28), date(2016, 2, 5)

# SHA-256 over the decompressed bid then ask day file of every day FIRST..LAST,
# and of the 15-minute bid range CSV, as written for these ticks by the code
# before the source contract (main at 608df5b, v1.0.0 + HistData) from both of
# its export paths. A change here is a change to the cache format.
GOLDEN_DAY_FILES = "6338b629cfaf539436aefd5f735f4cfbdddc8c7f21a3b8b3a79d04cdcd145147"
GOLDEN_RANGE_BID_15MIN = "0c0d944fb8f526be806f30b25a7dd1dfe7dd1adb238b8cf6d11067a830274e24"

# The sidecars that code wrote for --from 2016-01-04 --to 2016-01-08 --resample 1h
# (generated_at aside).
GOLDEN_SIDECAR = {
    "dukascopy": {"price_divisor": 10.0, "params": {}},
    "histdata": {"price_divisor": 0.0001, "params": {"scale_factor": 10000.0}},
}


@pytest.fixture(autouse=True)
def _clear_cancellation():
    cancellation.clear()
    yield
    cancellation.clear()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _run(name, feed, cache: Path, out: Path, first=FIRST, last=LAST, **kwargs):
    return ex.export_range(
        source=sources.create(name, feed.options),
        symbol=SYMBOL,
        start_utc=_utc(first),
        end_utc_inclusive=_utc(last),
        out=out,
        resample_rule=kwargs.pop("resample_rule", "15min"),
        cache_dir=cache,
        **kwargs,
    )


def _day_files(cache: Path, first=FIRST, last=LAST) -> list[Path]:
    return [
        ex._daily_candle_path(cache, SYMBOL, d, side)
        for d in _days(first, last)
        for side in ("bid", "ask")
    ]


def _digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(zstd.ZstdDecompressor().decompress(p.read_bytes(), max_output_size=1 << 26))
    return h.hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_every_registered_source_has_a_feed() -> None:
    assert set(FEEDS) == set(sources.names())


@pytest.fixture(params=sources.names())
def name(request) -> str:
    return request.param


def test_day_files_and_range_csv_match_the_pre_contract_output(name, monkeypatch, tmp_path):
    feed = FEEDS[name](monkeypatch, synthetic_ticks(FEED_FIRST, FEED_LAST))
    cache, out = tmp_path / "cache", tmp_path / "out"

    bid_csv, ask_csv = _run(name, feed, cache, out)

    files = _day_files(cache)
    assert all(p.exists() for p in files)
    assert _digest(files) == GOLDEN_DAY_FILES
    assert bid_csv is not None and ask_csv is not None
    assert hashlib.sha256(bid_csv.read_bytes()).hexdigest() == GOLDEN_RANGE_BID_15MIN


def test_every_source_writes_byte_identical_day_files_and_range_csvs(monkeypatch, tmp_path):
    ticks = synthetic_ticks(FEED_FIRST, FEED_LAST)
    written: dict[str, dict[str, bytes]] = {}
    for name in sources.names():
        feed = FEEDS[name](monkeypatch, ticks)
        cache, out = tmp_path / name / "cache", tmp_path / name / "out"
        bid_csv, ask_csv = _run(name, feed, cache, out)
        assert bid_csv is not None and ask_csv is not None
        files = {str(p.relative_to(cache)): p.read_bytes() for p in _day_files(cache)}
        files["range_bid"], files["range_ask"] = bid_csv.read_bytes(), ask_csv.read_bytes()
        written[name] = files

    first, *others = written.values()
    for other in others:
        assert other.keys() == first.keys()
        for key in first:
            assert other[key] == first[key], key  # byte-identical zstd day files and CSVs


def test_provenance_records_have_one_shape_and_name_their_source(name, monkeypatch, tmp_path):
    feed = FEEDS[name](monkeypatch, synthetic_ticks(FEED_FIRST, FEED_LAST))
    cache = tmp_path / "cache"

    _run(name, feed, cache, tmp_path / "out", resample_rule=None)

    records = _jsonl(ex._source_manifest_path(cache, SYMBOL))
    assert [r["day"] for r in records] == [d.isoformat() for d in _days(FIRST, LAST)]
    for r in records:
        assert set(r) == {"day", "source", "scale_factor", "committed_at", "source_unit"}
        assert r["source"] == name
        assert r["scale_factor"] == feed.scale_factor
        assert isinstance(r["source_unit"], str) and r["source_unit"]
        assert datetime.fromisoformat(r["committed_at"]).tzinfo is not None
    # A complete feed leaves no partial days.
    assert _jsonl(ex._partial_day_manifest_path(cache, SYMBOL)) == []


def test_sidecars_match_the_pre_contract_output(name, monkeypatch, tmp_path):
    feed = FEEDS[name](monkeypatch, synthetic_ticks(FEED_FIRST, FEED_LAST))
    out = tmp_path / "out"

    rc = cli.main(
        ["--source", name, "--symbols", SYMBOL, "--from", "2016-01-04", "--to", "2016-01-08"]
        + ["--resample", "1h", "--out", str(out), "--cache-dir", str(tmp_path / "cache")]
        + feed.cli_args
    )

    assert rc == 0
    for side in ("bid", "ask"):
        meta = json.loads((out / f"{SYMBOL}_1H_{side}.csv.meta.json").read_text())
        assert meta.pop("generated_at")
        assert meta == {
            "data_type": "candles",
            "params": {
                "date_from": "2016-01-04",
                "date_to": "2016-01-08",
                "price_side": side,
                "resample": "1h",
                **GOLDEN_SIDECAR[name]["params"],
            },
            "price_divisor": GOLDEN_SIDECAR[name]["price_divisor"],
            "schema_version": "1",
            "source": name,
            "symbol": SYMBOL,
            "timestamp_format": "iso8601_utc",
        }


# ---------------------------------------------------------------------------
# --keep-raw
# ---------------------------------------------------------------------------

# Through February 1st, so at least one month file has every day it covers committed.
KEEP_LAST = date(2016, 2, 1)


def test_raw_units_are_deleted_after_commit_by_default(name, monkeypatch, tmp_path):
    feed = FEEDS[name](monkeypatch, synthetic_ticks(FEED_FIRST, FEED_LAST))
    cache = tmp_path / "cache"

    _run(name, feed, cache, tmp_path / "out", last=KEEP_LAST, resample_rule=None)

    assert all(p.exists() for p in _day_files(cache, last=KEEP_LAST))
    assert not (cache / SYMBOL / "_raw").exists()


def test_keep_raw_retains_units_that_a_rerun_decodes_without_a_request(name, monkeypatch, tmp_path):
    feed = FEEDS[name](monkeypatch, synthetic_ticks(FEED_FIRST, FEED_LAST))
    cache = tmp_path / "cache"

    _run(name, feed, cache, tmp_path / "out", last=KEEP_LAST, resample_rule=None, keep_raw=True)

    kept = cache / SYMBOL / "_raw" / name
    assert kept.is_dir() and any(p.is_file() for p in kept.rglob("*"))
    files = _day_files(cache, last=KEEP_LAST)
    before = {p: p.read_bytes() for p in files}

    # A decode fix lands: drop the committed days and re-run, offline.
    for p in files:
        p.unlink()
    ex._source_manifest_path(cache, SYMBOL).unlink()
    feed.requests.clear()

    _run(name, feed, cache, tmp_path / "out", last=KEEP_LAST, resample_rule=None, keep_raw=True)

    assert feed.requests == []
    assert {p: p.read_bytes() for p in files} == before
    assert len(_jsonl(ex._source_manifest_path(cache, SYMBOL))) == len(files) // 2
