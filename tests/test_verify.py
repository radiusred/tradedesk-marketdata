"""
tradedesk-md-verify on synthetic cache pairs written with the framework's own
day-file writer: an identical pair, a one-hour summer shift, a x0.26 span and a
x100 scale, each reported in one actionable line; plus one-sided and empty days,
JSON output, the exit status, read-only access and cancellation. No network.
"""

import json
import threading
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import tradedesk_marketdata.export as ex
from tradedesk_marketdata import verify as v
from tradedesk_marketdata import verify_cli
from tradedesk_marketdata.cancel import cancellation

SYMBOL = "EURUSD"


@pytest.fixture(autouse=True)
def _clear_cancellation():
    cancellation.clear()
    yield
    cancellation.clear()


def _minutes(first: date, last: date) -> pd.DataFrame:
    """A continuous EURUSD-like minute series: weekdays 00:00-21:59 UTC, in cache units."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    index = pd.DatetimeIndex(
        np.concatenate(
            [
                pd.date_range(pd.Timestamp(d), periods=22 * 60, freq="1min").to_numpy()
                for d in days
                if d.weekday() < 5
            ]
        )
    ).tz_localize("UTC")
    n = np.arange(len(index))
    close = 10950.0 + (n * 7919 % 400) / 10.0
    return pd.DataFrame(
        {
            "open": close - 0.3,
            "high": close + 0.5,
            "low": close - 0.6,
            "close": close,
            "volume": 1.0 + n % 5,
        },
        index=index,
    )


def _write(cache: Path, minutes: pd.DataFrame, first: date, last: date) -> None:
    """Write one bid day file per day of [first, last], empty ones included."""
    by_day = {ts.date(): part for ts, part in minutes.groupby(minutes.index.normalize())}
    empty = minutes.iloc[0:0]
    d = first
    while d <= last:
        part = by_day.get(d, empty)
        ex._write_daily_candles(part, ex._daily_candle_path(cache, SYMBOL, d, "bid"))
        d += timedelta(days=1)


def _run(tmp_path: Path, *extra: str, first="2024-01-01", last="2024-07-31", capsys=None):
    rc = verify_cli.main(
        ["--reference", str(tmp_path / "ref"), "--cache-dir", str(tmp_path / "cache")]
        + ["--symbols", SYMBOL, "--from", first, "--to", last, *extra]
    )
    out = capsys.readouterr().out if capsys is not None else ""
    return rc, out


FIRST, LAST = date(2024, 1, 1), date(2024, 7, 31)


@pytest.fixture
def reference(tmp_path) -> pd.DataFrame:
    minutes = _minutes(FIRST, LAST)
    _write(tmp_path / "ref", minutes, FIRST, LAST)
    return minutes


def _findings(out: str) -> list[str]:
    tail = out.split("Findings", 1)
    return (
        [line.strip() for line in tail[1].splitlines()[1:] if line.strip()] if len(tail) > 1 else []
    )


def test_an_identical_pair_has_no_findings(tmp_path, reference, capsys):
    _write(tmp_path / "cache", reference, FIRST, LAST)

    rc, out = _run(tmp_path, capsys=capsys)

    assert rc == 0
    assert "No findings: the cache matches the reference." in out
    [report] = v.verify(tmp_path / "ref", tmp_path / "cache", [SYMBOL], FIRST, LAST)
    statuses = {d.status for d in report.days}
    assert statuses == {"same", "empty"}
    assert all(d.identical_at_zero == 1.0 for d in report.days if d.status == "same")
    assert [(r.first, r.last, r.ratio) for r in report.regimes] == [
        (date(2024, 1, 1), date(2024, 7, 31), 1.0)
    ]


def test_a_one_hour_summer_shift_is_one_line(tmp_path, reference, capsys):
    # The cache's summer days (June) are stamped an hour late; winter days match.
    summer = (reference.index >= pd.Timestamp("2024-06-01", tz="UTC")) & (
        reference.index < pd.Timestamp("2024-07-01", tz="UTC")
    )
    shifted = reference.copy()
    shifted.index = reference.index + pd.to_timedelta(np.where(summer, 60, 0), unit="min")
    _write(tmp_path / "cache", shifted, FIRST, LAST)

    rc, out = _run(tmp_path, capsys=capsys)

    assert rc == 1
    assert _findings(out) == [
        "EURUSD 2024-06-03..2024-06-28: 20 day(s) match the reference only shifted by +60 min "
        "(the cache's timestamps are 60 min late)"
    ]
    [report] = v.verify(tmp_path / "ref", tmp_path / "cache", [SYMBOL], FIRST, LAST)
    june = [d for d in report.days if d.status == "shifted"]
    assert {d.shift_minutes for d in june} == {60}
    assert all(d.identical == 1.0 for d in june)


def test_a_x0_26_span_is_one_regime_line(tmp_path, reference, capsys):
    # Another instrument at 0.26x the reference's level from 2024-03-11 to 2024-05-17.
    span = (reference.index >= pd.Timestamp("2024-03-11", tz="UTC")) & (
        reference.index < pd.Timestamp("2024-05-18", tz="UTC")
    )
    substituted = reference.copy()
    for col in ("open", "high", "low", "close"):
        substituted.loc[span, col] = (substituted.loc[span, col] * 0.26).round(1)
    _write(tmp_path / "cache", substituted, FIRST, LAST)

    rc, out = _run(tmp_path, capsys=capsys)

    assert rc == 1
    [line] = _findings(out)
    assert line.startswith("EURUSD 2024-03-11..2024-05-17: reference/cache close ratio 3.84")
    assert line.endswith("over 50 day(s): another level or scale than the reference")
    [report] = v.verify(tmp_path / "ref", tmp_path / "cache", [SYMBOL], FIRST, LAST)
    assert [round(r.ratio, 2) for r in report.regimes] == [1.0, 3.85, 1.0]


def test_a_x100_scale_is_one_regime_line(tmp_path, reference, capsys):
    scaled = reference.copy()
    for col in ("open", "high", "low", "close"):
        scaled[col] = scaled[col] * 100
    _write(tmp_path / "cache", scaled, FIRST, LAST)

    rc, out = _run(tmp_path, capsys=capsys)

    assert rc == 1
    assert _findings(out) == [
        "EURUSD 2024-01-01..2024-07-31: reference/cache close ratio 0.01 over 153 day(s): "
        "another level or scale than the reference"
    ]


def test_one_sided_days_are_counted_but_do_not_fail(tmp_path, reference, capsys):
    _write(tmp_path / "cache", reference, date(2024, 1, 8), LAST)  # the cache starts later
    extra = _minutes(date(2024, 8, 1), date(2024, 8, 2))
    _write(tmp_path / "cache", extra, date(2024, 8, 1), date(2024, 8, 2))  # and ends later

    rc, out = _run(tmp_path, last="2024-08-02", capsys=capsys)

    assert rc == 0
    [report] = v.verify(tmp_path / "ref", tmp_path / "cache", [SYMBOL], FIRST, date(2024, 8, 2))
    counts = report.years[2024]["counts"]
    assert counts["only-left"] == 5  # 2024-01-01..05 (the weekend is empty on the left too)
    assert counts["only-right"] == 2
    assert counts["empty"] > 0


def test_a_daily_difference_beyond_the_tolerance_fails(tmp_path, reference, capsys):
    nudged = reference.copy()
    day = (nudged.index >= pd.Timestamp("2024-02-14", tz="UTC")) & (
        nudged.index < pd.Timestamp("2024-02-15", tz="UTC")
    )
    nudged.loc[day, "high"] = nudged.loc[day, "high"] * 1.01  # one bad day, 1% high
    _write(tmp_path / "cache", nudged, FIRST, LAST)

    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1
    assert _findings(out) == [
        "EURUSD 2024-02-14: 1 day(s) differ from the reference by more than 0.002 at the daily "
        "level (worst 0.01)"
    ]
    rc, _ = _run(tmp_path, "--tolerance", "0.02", capsys=capsys)
    assert rc == 0


def test_json_output_carries_every_day(tmp_path, reference, capsys):
    _write(tmp_path / "cache", reference, FIRST, LAST)

    rc, out = _run(tmp_path, "--format", "json", capsys=capsys)

    doc = json.loads(out)
    assert rc == 0 and doc["failed"] is False and doc["findings"] == []
    [sym] = doc["symbols"]
    assert sym["symbol"] == SYMBOL and len(sym["days"]) == (LAST - FIRST).days + 1
    assert sym["years"]["2024"]["counts"]["same"] == 153
    assert sym["regimes"] == [
        {"first": "2024-01-01", "last": "2024-07-31", "ratio": 1.0, "days": 153}
    ]


def test_verify_writes_nothing(tmp_path, reference, capsys):
    _write(tmp_path / "cache", reference, FIRST, LAST)

    def snapshot():
        return {
            p: (p.stat().st_mtime_ns, p.read_bytes())
            for root in (tmp_path / "ref", tmp_path / "cache")
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    _run(tmp_path, "--format", "json", capsys=capsys)
    assert snapshot() == before


def test_a_cancel_stops_the_run(tmp_path, reference, monkeypatch):
    _write(tmp_path / "cache", reference, FIRST, LAST)
    calls = {"n": 0}
    real = v.compare_day
    lock = threading.Lock()

    def cancelling(*args, **kwargs):
        with lock:
            calls["n"] += 1
        cancellation.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(v, "compare_day", cancelling)

    with pytest.raises(KeyboardInterrupt):
        v.verify(tmp_path / "ref", tmp_path / "cache", [SYMBOL], FIRST, LAST, workers=2)
    assert calls["n"] < (LAST - FIRST).days + 1


@pytest.mark.parametrize(
    "argv",
    [
        ["--from", "2024-02-01", "--to", "2024-01-01"],
        ["--from", "2024-01-01", "--to", "2024-01-31", "--tolerance", "-1"],
        ["--from", "2024-13-01", "--to", "2024-01-31"],
    ],
)
def test_usage_errors_exit_2(tmp_path, reference, argv):
    _write(tmp_path / "cache", reference, FIRST, FIRST)
    with pytest.raises(SystemExit) as exc:
        verify_cli.main(
            ["--reference", str(tmp_path / "ref"), "--cache-dir", str(tmp_path / "cache")]
            + ["--symbols", SYMBOL, *argv]
        )
    assert exc.value.code == 2


def test_a_missing_cache_directory_is_a_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        verify_cli.main(
            ["--reference", str(tmp_path), "--cache-dir", str(tmp_path / "nope")]
            + ["--symbols", SYMBOL, "--from", "2024-01-01", "--to", "2024-01-02"]
        )
    assert exc.value.code == 2
    assert "--cache-dir" in capsys.readouterr().err


def test_the_shift_detector_reports_the_best_whole_minute_alignment():
    day = date(2024, 6, 12)
    minutes = _minutes(day, day)
    late = minutes.copy()
    late.index = minutes.index + pd.Timedelta(minutes=60)
    assert v._identical_fraction(minutes, late, v.PRICE_COLUMNS, 60, day) == 1.0
    assert v._identical_fraction(minutes, late, v.PRICE_COLUMNS, 0, day) < 0.1


def test_the_command_is_installed() -> None:
    import tomllib

    scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"][
        "scripts"
    ]
    assert scripts["tradedesk-md-verify"] == "tradedesk_marketdata.verify_cli:main"
