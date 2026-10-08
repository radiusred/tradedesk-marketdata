"""
Tests for age-gated partial-commit of permanent-gap days.

A day with a permanent-gap hour (404 / decode-failure) is committed from the
hours that DID decode once it is older than ``commit_partial_after_days``;
younger such days are left for retry. Scale-sentry-rejected days are never
partial-committed. Each partial commit is recorded in a per-symbol
``_partial_days.jsonl`` manifest without touching the candle-CSV schema.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import tradedesk_marketdata.export as ex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick(ts: datetime) -> ex.Tick:
    return ex.Tick(ts=ts, bid=1.1000, ask=1.1005, bid_vol=1.0, ask_vol=1.0)


def _aged_start(days_ago: int) -> datetime:
    """Midnight UTC of the date `days_ago` days before today (UTC)."""
    d = datetime.now(UTC).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=UTC)


def _bi5_path(cache_dir: Path, symbol: str, hour: datetime) -> Path:
    return (
        cache_dir
        / symbol
        / f"{hour.year}"
        / f"{hour.month - 1:02d}"
        / f"{hour.day:02d}"
        / f"{hour.hour:02d}h_ticks.bi5"
    )


def _patch(monkeypatch, *, gap_hours=(), decode_fail_hours=(), empty_hours=()):
    """Patch download/probe/decode so export_range runs offline.

    - gap_hours: hours whose download returns None (404).
    - empty_hours: hours whose download returns b"" (legitimate empty-200).
    - decode_fail_hours: hours that download OK but raise on decode.
    A sentinel .bi5 is written for every downloaded (non-404) hour so _flush_day
    has something to delete.
    """
    gap = {h.hour for h in gap_hours}
    empty = {h.hour for h in empty_hours}
    dfail = {h.hour for h in decode_fail_hours}

    def fake_download(url, *, cache_path, **kwargs):
        hh = int(url.split("/")[-1][:2])  # "00h_ticks.bi5" -> 0
        if hh in gap:
            return None
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"fake")
        if hh in empty:
            return b""
        return b"fake"

    monkeypatch.setattr(ex, "_download_bi5", fake_download)
    monkeypatch.setattr(ex, "_probe_price_format", lambda *_: "float")

    def fake_decode(hour_start, _comp, *, price_format, price_divisor):
        if hour_start.hour in dfail:
            raise ValueError("synthetic decode failure")
        return [_make_tick(hour_start)]

    monkeypatch.setattr(ex, "_decode_ticks", fake_decode)


def _run(monkeypatch, *, symbol, start, hours, **kwargs):
    monkeypatch.setattr(ex, "_iter_hours", lambda *_: iter(hours))
    monkeypatch.setattr(ex, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)
    return ex.export_range(
        symbol=symbol,
        start_utc=start,
        end_utc_inclusive=hours[-1],
        out=kwargs.pop("out"),
        price_divisor=1.0,
        resample_rule="1min",
        probe=False,
        **kwargs,
    )


def _read_manifest(cache_dir: Path, symbol: str) -> list[dict]:
    path = ex._partial_day_manifest_path(cache_dir, symbol)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Aged permanent-gap day → partial commit
# ---------------------------------------------------------------------------


def test_aged_gap_day_partial_commits_deletes_bi5_and_writes_manifest(monkeypatch, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(30)
    hours = [start, start + timedelta(hours=1), start + timedelta(hours=2)]
    # Hour 00 is a permanent 404; hours 01 and 02 decode fine.
    _patch(monkeypatch, gap_hours=[hours[0]])

    _run(
        monkeypatch,
        symbol="LIGHTCMDUSD",
        start=start,
        hours=hours,
        cache_dir=cache_dir,
        out=out_dir,
        commit_partial_after_days=7,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "LIGHTCMDUSD", start.date(), "bid")
    ask_csv = ex._daily_candle_path(cache_dir, "LIGHTCMDUSD", start.date(), "ask")
    assert bid_csv.exists() and ask_csv.exists(), "aged gap day must partial-commit"

    # Committed frame loads as a normal (shorter) candle frame: 2 good hours.
    candles = ex._load_daily_candles(bid_csv)
    assert candles is not None and len(candles) == 2

    # All bi5 for the day are deleted and the day-dir removed.
    assert not _bi5_path(cache_dir, "LIGHTCMDUSD", hours[1]).exists()
    assert not _bi5_path(cache_dir, "LIGHTCMDUSD", hours[2]).exists()
    assert not _bi5_path(cache_dir, "LIGHTCMDUSD", hours[0]).parent.exists()

    # Manifest records the permanent-gap hour and reason.
    manifest = _read_manifest(cache_dir, "LIGHTCMDUSD")
    assert len(manifest) == 1
    rec = manifest[0]
    assert rec["day"] == start.date().isoformat()
    assert rec["missing_hours"] == [0]
    assert rec["gap_reason"] == "missing_404"
    assert "committed_at" in rec


def test_young_gap_day_is_not_committed(monkeypatch, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(2)
    hours = [start, start + timedelta(hours=1)]
    _patch(monkeypatch, gap_hours=[hours[0]])

    _run(
        monkeypatch,
        symbol="LIGHTCMDUSD",
        start=start,
        hours=hours,
        cache_dir=cache_dir,
        out=out_dir,
        commit_partial_after_days=7,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "LIGHTCMDUSD", start.date(), "bid")
    assert not bid_csv.exists(), "below-threshold gap day must not commit"
    # The good hour's bi5 is retained for the next retry.
    assert _bi5_path(cache_dir, "LIGHTCMDUSD", hours[1]).exists()
    assert _read_manifest(cache_dir, "LIGHTCMDUSD") == []


def test_age_gate_boundary(monkeypatch, tmp_path):
    # Exactly at the threshold (>= commits); just under (does not).
    for days_ago, should_commit in ((7, True), (6, False)):
        cache_dir = tmp_path / f"cache_{days_ago}"
        out_dir = tmp_path / f"out_{days_ago}"
        start = _aged_start(days_ago)
        hours = [start, start + timedelta(hours=1)]
        _patch(monkeypatch, gap_hours=[hours[0]])
        _run(
            monkeypatch,
            symbol="XPDCMDUSD",
            start=start,
            hours=hours,
            cache_dir=cache_dir,
            out=out_dir,
            commit_partial_after_days=7,
        )
        bid_csv = ex._daily_candle_path(cache_dir, "XPDCMDUSD", start.date(), "bid")
        assert bid_csv.exists() is should_commit, (
            f"days_ago={days_ago} expected commit={should_commit}"
        )


# ---------------------------------------------------------------------------
# Scale-sentry rejection dominates: never partial-commit
# ---------------------------------------------------------------------------


def test_scale_rejected_gap_day_is_never_partial_committed(monkeypatch, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(365)  # very old: would otherwise commit at any threshold
    hours = [start, start + timedelta(hours=1)]
    _patch(monkeypatch, gap_hours=[hours[0]])
    # Force the scale-sentry to reject this day.
    monkeypatch.setattr(
        ex, "check_scale_consistency", lambda *a, **k: (False, "synthetic scale reject")
    )

    _run(
        monkeypatch,
        symbol="LIGHTCMDUSD",
        start=start,
        hours=hours,
        cache_dir=cache_dir,
        out=out_dir,
        commit_partial_after_days=0,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "LIGHTCMDUSD", start.date(), "bid")
    assert not bid_csv.exists(), "scale-rejected day must never commit"
    # bi5 retained for a corrected --price-divisor re-run.
    assert _bi5_path(cache_dir, "LIGHTCMDUSD", hours[1]).exists()
    assert _read_manifest(cache_dir, "LIGHTCMDUSD") == []


# ---------------------------------------------------------------------------
# Empty-200 (market closed) is unchanged: still a clean commit, no manifest
# ---------------------------------------------------------------------------


def test_empty_200_hour_commits_normally_without_manifest(monkeypatch, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(30)
    hours = [start, start + timedelta(hours=1)]
    # Hour 00 is a legitimate empty-200 (market closed), hour 01 decodes.
    _patch(monkeypatch, empty_hours=[hours[0]])

    _run(
        monkeypatch,
        symbol="EURUSD",
        start=start,
        hours=hours,
        cache_dir=cache_dir,
        out=out_dir,
        commit_partial_after_days=7,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "bid")
    assert bid_csv.exists(), "empty-200 day must still commit (unchanged behaviour)"
    # Not a partial commit → no manifest entry.
    assert _read_manifest(cache_dir, "EURUSD") == []


# ---------------------------------------------------------------------------
# Decode-failure gap reason is recorded
# ---------------------------------------------------------------------------


def test_decode_failure_records_decode_failed_reason(monkeypatch, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(30)
    hours = [start, start + timedelta(hours=1)]
    # Hour 00 downloads but fails to decode; hour 01 decodes fine.
    _patch(monkeypatch, decode_fail_hours=[hours[0]])

    _run(
        monkeypatch,
        symbol="COPPERCMDUSD",
        start=start,
        hours=hours,
        cache_dir=cache_dir,
        out=out_dir,
        commit_partial_after_days=7,
    )

    bid_csv = ex._daily_candle_path(cache_dir, "COPPERCMDUSD", start.date(), "bid")
    assert bid_csv.exists()
    manifest = _read_manifest(cache_dir, "COPPERCMDUSD")
    assert len(manifest) == 1
    assert manifest[0]["missing_hours"] == [0]
    assert manifest[0]["gap_reason"] == "decode_failed"


def test_fully_gapped_day_commits_nothing(monkeypatch, tmp_path):
    # A day where every fetched hour 404s has no decoded data: it must not
    # write empty candle CSVs and must not record a manifest entry. (The export
    # then raises the pre-existing "no data produced" error, as nothing decoded.)
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(30)
    hours = [start, start + timedelta(hours=1)]
    _patch(monkeypatch, gap_hours=hours)

    with pytest.raises(RuntimeError, match="No data produced"):
        _run(
            monkeypatch,
            symbol="LIGHTCMDUSD",
            start=start,
            hours=hours,
            cache_dir=cache_dir,
            out=out_dir,
            commit_partial_after_days=0,
        )

    bid_csv = ex._daily_candle_path(cache_dir, "LIGHTCMDUSD", start.date(), "bid")
    assert not bid_csv.exists(), "a fully-gapped day must not commit empty CSVs"
    assert _read_manifest(cache_dir, "LIGHTCMDUSD") == []


# ---------------------------------------------------------------------------
# Idempotent re-export: a committed partial day stays committed, no re-strand
# ---------------------------------------------------------------------------


def test_reexport_is_idempotent(monkeypatch, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    start = _aged_start(30)
    hours = [start, start + timedelta(hours=1)]
    _patch(monkeypatch, gap_hours=[hours[0]])

    for _ in range(2):
        _run(
            monkeypatch,
            symbol="XPTCMDUSD",
            start=start,
            hours=hours,
            cache_dir=cache_dir,
            out=out_dir,
            commit_partial_after_days=7,
        )

    bid_csv = ex._daily_candle_path(cache_dir, "XPTCMDUSD", start.date(), "bid")
    assert bid_csv.exists()
    # Day-dir stays gone; no bi5 re-stranded.
    assert not _bi5_path(cache_dir, "XPTCMDUSD", hours[1]).parent.exists()
    # Manifest is not duplicated on the second (fully-cached) run.
    assert len(_read_manifest(cache_dir, "XPTCMDUSD")) == 1


# ---------------------------------------------------------------------------
# Manifest helper: append-only, one JSON object per line
# ---------------------------------------------------------------------------


def test_manifest_helper_appends_one_line_per_record(tmp_path):
    cache_dir = tmp_path / "cache"
    d1 = datetime(2025, 1, 2, tzinfo=UTC).date()
    d2 = datetime(2025, 1, 3, tzinfo=UTC).date()
    ex._append_partial_day_manifest(cache_dir, "LIGHTCMDUSD", d1, [2, 5, 6], "missing_404")
    ex._append_partial_day_manifest(cache_dir, "LIGHTCMDUSD", d2, [1], "decode_failed+missing_404")

    records = _read_manifest(cache_dir, "LIGHTCMDUSD")
    assert [r["day"] for r in records] == [d1.isoformat(), d2.isoformat()]
    assert records[0]["missing_hours"] == [2, 5, 6]
    assert records[1]["gap_reason"] == "decode_failed+missing_404"
