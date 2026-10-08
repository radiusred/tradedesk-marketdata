"""
Tests for parallel.py — _export_worker and run_parallel_exports.
"""

from datetime import UTC, datetime
from pathlib import Path

import tradedesk_dukascopy.export as ex
import tradedesk_dukascopy.parallel as par
from tradedesk_dukascopy.parallel import ExportResult, ExportTask, _export_worker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(symbol: str = "EURUSD", tmp_path: Path | None = None) -> ExportTask:
    return ExportTask(
        symbol=symbol,
        start_utc=datetime(2025, 1, 1, tzinfo=UTC),
        end_utc_inclusive=datetime(2025, 1, 1, tzinfo=UTC),
        resample_rule="1min",
        price_divisor=1.0,
        cache_dir=None,
        out=tmp_path,
    )


# ---------------------------------------------------------------------------
# _export_worker
# ---------------------------------------------------------------------------


def test_export_worker_success_collects_both_output_csvs(monkeypatch, tmp_path):
    bid = tmp_path / "EURUSD_1MIN_bid.csv"
    ask = tmp_path / "EURUSD_1MIN_ask.csv"

    monkeypatch.setattr(ex, "export_range", lambda **_: (bid, ask))

    result = _export_worker(_task(tmp_path=tmp_path))

    assert result.success is True
    assert result.error is None
    assert result.output_csvs == [bid, ask]


def test_export_worker_filters_none_from_output_csvs(monkeypatch, tmp_path):
    # One side produces no data (e.g. all-empty frames).
    bid = tmp_path / "EURUSD_1MIN_bid.csv"

    monkeypatch.setattr(ex, "export_range", lambda **_: (bid, None))

    result = _export_worker(_task(tmp_path=tmp_path))

    assert result.success is True
    assert result.output_csvs == [bid]


def test_export_worker_returns_failure_on_exception(monkeypatch, tmp_path):
    def bad_export(**_):
        raise RuntimeError("network error")

    monkeypatch.setattr(ex, "export_range", bad_export)

    result = _export_worker(_task(tmp_path=tmp_path))

    assert result.success is False
    assert result.output_csvs == []
    assert "network error" in result.error


# ---------------------------------------------------------------------------
# run_parallel_exports
# ---------------------------------------------------------------------------


def test_run_parallel_exports_collects_results_from_all_tasks(monkeypatch, tmp_path):
    symbols = ["EURUSD", "GBPUSD", "USDJPY"]

    def fake_worker(task, progress=None):
        return ExportResult(symbol=task.symbol, output_csvs=[], success=True)

    monkeypatch.setattr(par, "_export_worker", fake_worker)

    tasks = [_task(symbol=s, tmp_path=tmp_path) for s in symbols]
    results = par.run_parallel_exports(tasks, max_workers=2)

    assert len(results) == len(symbols)
    assert {r.symbol for r in results} == set(symbols)
    assert all(r.success for r in results)


def test_run_parallel_exports_reports_failed_symbol(monkeypatch, tmp_path):
    def fake_worker(task, progress=None):
        if task.symbol == "BADUSD":
            return ExportResult(symbol=task.symbol, output_csvs=[], success=False, error="boom")
        return ExportResult(symbol=task.symbol, output_csvs=[], success=True)

    monkeypatch.setattr(par, "_export_worker", fake_worker)

    tasks = [_task(symbol=s, tmp_path=tmp_path) for s in ["EURUSD", "BADUSD"]]
    results = par.run_parallel_exports(tasks, max_workers=2)

    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["EURUSD"].success is True
    assert by_symbol["BADUSD"].success is False
    assert by_symbol["BADUSD"].error == "boom"


def test_run_parallel_exports_empty_task_list(monkeypatch):
    results = par.run_parallel_exports([], max_workers=2)
    assert results == []


def test_export_worker_forwards_timeouts_and_retries(monkeypatch, tmp_path):
    """A task carrying network settings passes them to export_range; one without
    leaves export_range's own defaults in force."""
    from datetime import UTC, datetime

    import tradedesk_dukascopy.export as ex
    import tradedesk_dukascopy.parallel as par

    seen: list[dict] = []

    def fake_export_range(**kwargs):
        seen.append(kwargs)
        return (None, None)

    monkeypatch.setattr(ex, "export_range", fake_export_range)

    common = dict(
        symbol="EURUSD",
        start_utc=datetime(2025, 7, 1, tzinfo=UTC),
        end_utc_inclusive=datetime(2025, 7, 1, tzinfo=UTC),
        resample_rule=None,
        price_divisor=1.0,
        cache_dir=tmp_path,
        out=tmp_path,
    )
    par._export_worker(par.ExportTask(**common, timeout=(15.0, 90.0), retries=5))
    par._export_worker(par.ExportTask(**common))

    assert seen[0]["timeout"] == (15.0, 90.0) and seen[0]["retries"] == 5
    assert "timeout" not in seen[1] and "retries" not in seen[1]
