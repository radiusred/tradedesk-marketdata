"""
Per-day provenance: every day a Dukascopy export commits is recorded in the
symbol's append-only ``_sources.jsonl``, and a day that is already committed
(by any source) is never rewritten or re-recorded.
"""

import json
from datetime import UTC, datetime, timedelta

import tradedesk_marketdata.export as ex
import tradedesk_marketdata.sources.dukascopy as dk


def _patch_fake_feed(monkeypatch, *, price_format: str = "int"):
    def fake_download(url, *, cache_path, **kwargs):
        return b"fake"

    def fake_decode(hour_start, _comp, *, price_format, price_divisor):
        return [ex.Tick(ts=hour_start, bid=1.1, ask=1.2, bid_vol=1.0, ask_vol=1.0)]

    monkeypatch.setattr(dk, "_download_bi5", fake_download)
    monkeypatch.setattr(dk, "_probe_price_format", lambda *_: price_format)
    monkeypatch.setattr(dk, "_decode_ticks", fake_decode)
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)


def _records(cache_dir, symbol):
    path = ex._source_manifest_path(cache_dir, symbol)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_dukascopy_commit_records_source_scale_and_hour_set(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    start = datetime(2025, 3, 3, 0, 0, tzinfo=UTC)
    hours = [start + timedelta(hours=i) for i in range(24)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter(hours))
    _patch_fake_feed(monkeypatch)

    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start,
        out=tmp_path / "out",
        price_divisor=100.0,
        resample_rule=None,
        cache_dir=cache_dir,
    )

    [rec] = _records(cache_dir, "EURUSD")
    assert rec["day"] == "2025-03-03"
    assert rec["source"] == "dukascopy"
    assert rec["scale_factor"] == 0.01  # int32 ticks divided by --price-divisor 100
    assert rec["source_unit"] == "EURUSD/2025/02/03/00h-23h_ticks.bi5"
    assert datetime.fromisoformat(rec["committed_at"]).tzinfo is not None


def test_float_ticks_record_unit_scale(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    start = datetime(2025, 3, 3, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter([start]))
    _patch_fake_feed(monkeypatch, price_format="float")

    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start,
        out=tmp_path / "out",
        price_divisor=100.0,
        resample_rule=None,
        cache_dir=cache_dir,
    )

    [rec] = _records(cache_dir, "EURUSD")
    assert rec["scale_factor"] == 1.0  # the divisor is not applied to float ticks


def test_already_committed_day_is_not_rewritten_or_rerecorded(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    start = datetime(2025, 3, 3, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(dk, "_iter_hours", lambda *_: iter([start]))
    _patch_fake_feed(monkeypatch)

    # Another source committed the day first.
    bid = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "bid")
    ask = ex._daily_candle_path(cache_dir, "EURUSD", start.date(), "ask")
    bid.parent.mkdir(parents=True)
    bid.write_bytes(b"first")
    ask.write_bytes(b"first")

    dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start,
        out=tmp_path / "out",
        resample_rule=None,
        cache_dir=cache_dir,
    )

    assert bid.read_bytes() == b"first" and ask.read_bytes() == b"first"
    assert _records(cache_dir, "EURUSD") == []
