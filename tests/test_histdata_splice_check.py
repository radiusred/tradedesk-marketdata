"""Tests for scripts/histdata_splice_check.py (loaded by path, like the other script tests)."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import tradedesk_marketdata.export as ex

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "histdata_splice_check.py"


@pytest.fixture(scope="module")
def splice():
    spec = importlib.util.spec_from_file_location("histdata_splice_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_day(cache: Path, symbol: str, day: date, mid: float, source: str | None) -> None:
    idx = pd.date_range(
        pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=20), periods=90, freq="1min"
    )
    for side, px in (("bid", mid - 0.5), ("ask", mid + 0.5)):
        df = pd.DataFrame(
            {"open": px, "high": px, "low": px, "close": px, "volume": 0.0}, index=idx
        )
        ex._write_daily_candles(df, ex._daily_candle_path(cache, symbol, day, side))
    if source is not None:
        ex._append_source_manifest(
            cache, symbol, day, source=source, scale_factor=1.0, source_unit="test"
        )


def test_reports_bias_mean_abs_and_largest_divergence(splice, tmp_path):
    main, other = tmp_path / "main", tmp_path / "hd"
    days = [date(2020, 1, d) for d in (6, 7, 8, 9)]
    for i, d in enumerate(days):
        _write_day(main, "USA500IDXUSD", d, 3200.0 + i, source=None)  # legacy Dukascopy day
        _write_day(other, "USA500IDXUSD", d, 3200.0 + i + (2.0 if i == 2 else 0.5), "histdata")

    res = splice.check(main, other, "USA500IDXUSD", "2020-01-01", "2020-01-31")

    assert res["status"] == "OK"
    assert res["n_aligned_days"] == 4
    assert res["bias_units"] == pytest.approx((0.5 + 0.5 + 2.0 + 0.5) / 4)
    assert res["mean_abs_diff_units"] == pytest.approx(0.875)
    assert res["max_divergence"]["day"] == "2020-01-08"
    assert res["max_divergence"]["diff_units"] == pytest.approx(2.0)


def test_only_compares_days_recorded_as_the_right_source(splice, tmp_path):
    main, other = tmp_path / "main", tmp_path / "hd"
    d = date(2020, 1, 6)
    _write_day(main, "EURUSD", d, 11000.0, source="histdata")  # not a Dukascopy day
    _write_day(other, "EURUSD", d, 11000.0, source="histdata")

    res = splice.check(main, other, "EURUSD", "2020-01-01", "2020-01-31")

    assert res["status"] == "NO_DUKASCOPY_DATA"


def test_an_excluded_record_is_not_a_source_for_the_day(splice, tmp_path):
    cache = tmp_path / "main"
    d = date(2020, 1, 6)
    _write_day(cache, "EURUSD", d, 11000.0, source=None)
    manifest = ex._source_manifest_path(cache, "EURUSD")
    manifest.write_text(
        json.dumps({"day": d.isoformat(), "source": "histdata", "status": "excluded"}) + "\n"
    )
    ex._append_source_manifest(
        cache, "EURUSD", d, source="dukascopy", scale_factor=1.0, source_unit="test"
    )

    assert splice.load_sources(cache, "EURUSD") == {d.isoformat(): "dukascopy"}


def test_lists_the_joins_between_sources_in_the_main_cache(splice, tmp_path):
    main = tmp_path / "main"
    _write_day(main, "EURUSD", date(2019, 12, 30), 11200.0, source="histdata")
    _write_day(main, "EURUSD", date(2019, 12, 31), 11210.0, source="histdata")
    _write_day(main, "EURUSD", date(2020, 1, 2), 11220.0, source="dukascopy")

    assert splice.find_joins(main, "EURUSD") == [
        {"last_day": "2019-12-31", "from": "histdata", "first_day": "2020-01-02", "to": "dukascopy"}
    ]


def test_cli_writes_a_json_report(splice, tmp_path, monkeypatch):
    main, other = tmp_path / "main", tmp_path / "hd"
    for i, d in enumerate([date(2020, 1, 6), date(2020, 1, 7)]):
        _write_day(main, "EURUSD", d, 11000.0 + i, source="dukascopy")
        _write_day(other, "EURUSD", d, 11001.0 + i, source="histdata")
    out = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        ["histdata_splice_check.py", "--cache", str(main), "--histdata-cache", str(other)]
        + ["--instruments", "EURUSD", "--start", "2020-01-01", "--end", "2020-01-31"]
        + ["--out", str(out)],
    )

    splice.main()

    [res] = json.loads(out.read_text())["instruments"]
    assert res["status"] == "OK" and res["bias_units"] == pytest.approx(1.0)
