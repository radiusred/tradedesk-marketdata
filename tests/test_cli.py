import logging
from datetime import UTC
from pathlib import Path

import tradedesk_marketdata.cli as cli
import tradedesk_marketdata.parallel as par
from tradedesk_marketdata.parallel import ExportResult


def test_configure_logging_maps_fatal_to_critical() -> None:
    # logging.setLevel("FATAL") happens to work on CPython but the CLI choice
    # is intentionally translated to the canonical stdlib name CRITICAL.
    cli.configure_logging(level="fatal")
    assert logging.getLogger().level == logging.CRITICAL


def test_configure_logging_maps_trace_to_debug() -> None:
    # "TRACE" is not a stdlib logging level; setLevel("TRACE") raises ValueError.
    # The CLI maps trace -> DEBUG so the choice is usable.
    cli.configure_logging(level="trace")
    assert logging.getLogger().level == logging.DEBUG


def test_parse_ymd_sets_utc_timezone() -> None:
    dt = cli._parse_ymd("2025-07-01")
    assert dt.tzinfo == UTC
    assert dt.year == 2025 and dt.month == 7 and dt.day == 1


def test_main_writes_sidecar_for_both_bid_and_ask(monkeypatch, tmp_path: Path) -> None:
    bid_csv = tmp_path / "EURUSD_5MIN_bid.csv"
    ask_csv = tmp_path / "EURUSD_5MIN_ask.csv"
    bid_csv.touch()
    ask_csv.touch()
    sidecars_written: list[Path] = []

    def fake_run_parallel_exports(tasks, max_workers):
        return [ExportResult(symbol="EURUSD", output_csvs=[bid_csv, ask_csv], success=True)]

    def fake_write_sidecar(_meta, output_csv):
        sidecars_written.append(output_csv)
        return output_csv.with_suffix(output_csv.suffix + ".meta.json")

    monkeypatch.setattr(par, "run_parallel_exports", fake_run_parallel_exports)
    monkeypatch.setattr(cli, "write_sidecar", fake_write_sidecar)

    rc = cli.main(
        [
            "--symbols",
            "EURUSD",
            "--from",
            "2025-07-01",
            "--to",
            "2025-07-01",
            "--resample",
            "5min",
            "--out",
            str(tmp_path),
            "--log-level",
            "info",
        ]
    )

    assert rc == 0
    assert len(sidecars_written) == 2
    assert bid_csv in sidecars_written
    assert ask_csv in sidecars_written


def test_main_does_not_write_sidecar_when_no_output_csvs(monkeypatch, tmp_path: Path) -> None:
    sidecars_written: list[Path] = []

    def fake_run_parallel_exports(tasks, max_workers):
        return [ExportResult(symbol="EURUSD", output_csvs=[], success=True)]

    def fake_write_sidecar(_meta, output_csv):
        sidecars_written.append(output_csv)
        return output_csv.with_suffix(output_csv.suffix + ".meta.json")

    monkeypatch.setattr(par, "run_parallel_exports", fake_run_parallel_exports)
    monkeypatch.setattr(cli, "write_sidecar", fake_write_sidecar)

    rc = cli.main(  # no --resample or --out
        [
            "--symbols",
            "EURUSD",
            "--from",
            "2025-07-01",
            "--to",
            "2025-07-01",
            "--log-level",
            "info",
        ]
    )

    assert rc == 0
    assert sidecars_written == []


def test_parser_network_defaults_match_export_module() -> None:
    import tradedesk_marketdata.export as ex

    args = cli.build_parser().parse_args(
        ["--symbols", "EURUSD", "--from", "2025-07-01", "--to", "2025-07-01"]
    )
    assert args.connect_timeout == ex.DEFAULT_CONNECT_TIMEOUT
    assert args.read_timeout == ex.DEFAULT_READ_TIMEOUT
    assert args.retries == ex.DEFAULT_RETRIES


def test_main_passes_timeouts_and_retries_to_export_tasks(monkeypatch, tmp_path: Path) -> None:
    captured: list[par.ExportTask] = []

    def fake_run_parallel_exports(tasks, max_workers):
        captured.extend(tasks)
        return [ExportResult(symbol=t.symbol, output_csvs=[], success=True) for t in tasks]

    monkeypatch.setattr(par, "run_parallel_exports", fake_run_parallel_exports)

    rc = cli.main(
        [
            "--symbols",
            "EURUSD",
            "GBPUSD",
            "--from",
            "2025-07-01",
            "--to",
            "2025-07-01",
            "--cache-dir",
            str(tmp_path),
            "--connect-timeout",
            "15",
            "--read-timeout",
            "90",
            "--retries",
            "5",
        ]
    )

    assert rc == 0
    assert [t.symbol for t in captured] == ["EURUSD", "GBPUSD"]
    assert all(t.timeout == (15.0, 90.0) and t.retries == 5 for t in captured)


def test_main_passes_timeouts_and_retries_to_probe(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_export_range(**kwargs):
        seen.update(kwargs)
        return (None, None)

    monkeypatch.setattr(cli, "export_range", fake_export_range)

    rc = cli.main(
        [
            "--symbols",
            "EURUSD",
            "--from",
            "2025-07-01",
            "--to",
            "2025-07-01",
            "--probe",
            "--no-cache",
            "--connect-timeout",
            "15",
            "--read-timeout",
            "90",
            "--retries",
            "5",
        ]
    )

    assert rc == 0
    assert seen["probe"] is True
    assert seen["timeout"] == (15.0, 90.0)
    assert seen["retries"] == 5


def test_main_rejects_non_positive_timeouts_and_zero_retries() -> None:
    import pytest

    base = ["--symbols", "EURUSD", "--from", "2025-07-01", "--to", "2025-07-01"]
    with pytest.raises(SystemExit):
        cli.main(base + ["--read-timeout", "0"])
    with pytest.raises(SystemExit):
        cli.main(base + ["--connect-timeout", "-1"])
    with pytest.raises(SystemExit):
        cli.main(base + ["--retries", "0"])


def test_main_waits_for_export_threads_after_interrupt(monkeypatch) -> None:
    waited: list[bool] = []

    def interrupted_run(tasks, max_workers):
        raise KeyboardInterrupt()

    monkeypatch.setattr(par, "run_parallel_exports", interrupted_run)
    monkeypatch.setattr(cli, "_wait_for_export_threads", lambda _log: waited.append(True))

    rc = cli.main(
        ["--symbols", "EURUSD", "--from", "2025-07-01", "--to", "2025-07-01", "--no-cache"]
    )

    assert rc == 130
    assert waited == [True]


def test_wait_for_export_threads_second_interrupt_exits_130(monkeypatch) -> None:
    import os
    import threading

    class HungThread:
        daemon = False

        def join(self):
            raise KeyboardInterrupt()

    exit_codes: list[int] = []

    def fake_exit(code):
        exit_codes.append(code)
        raise SystemExit(code)  # os._exit never returns; stop the test here too

    monkeypatch.setattr(threading, "enumerate", lambda: [threading.current_thread(), HungThread()])
    monkeypatch.setattr(os, "_exit", fake_exit)

    import pytest

    with pytest.raises(SystemExit):
        cli._wait_for_export_threads(logging.getLogger("test"))
    assert exit_codes == [130]


def test_probe_returns_130_on_interrupt(monkeypatch) -> None:
    def interrupted_export_range(**_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "export_range", interrupted_export_range)

    rc = cli.main(
        [
            "--symbols",
            "EURUSD",
            "--from",
            "2025-07-01",
            "--to",
            "2025-07-01",
            "--probe",
            "--no-cache",
        ]
    )
    assert rc == 130


# ---------------------------------------------------------------------------
# --source
# ---------------------------------------------------------------------------


def _capture_tasks(monkeypatch, *, csvs=()):
    captured: list[par.ExportTask] = []

    def fake_run_parallel_exports(tasks, max_workers):
        captured.extend(tasks)
        return [ExportResult(symbol=t.symbol, output_csvs=list(csvs), success=True) for t in tasks]

    monkeypatch.setattr(par, "run_parallel_exports", fake_run_parallel_exports)
    return captured


_RANGE = ["--from", "2015-01-01", "--to", "2015-01-31"]


def test_source_defaults_to_dukascopy(monkeypatch) -> None:
    captured = _capture_tasks(monkeypatch)
    assert cli.main(["--symbols", "EURUSD", *_RANGE, "--no-cache"]) == 0
    assert [t.source for t in captured] == ["dukascopy"]
    assert captured[0].price_divisor == 1.0


def test_source_histdata_reaches_the_export_tasks(monkeypatch) -> None:
    captured = _capture_tasks(monkeypatch)
    rc = cli.main(
        ["--source", "histdata", "--symbols", "USA500IDXUSD", "EURUSD", *_RANGE, "--no-cache"]
    )
    assert rc == 0
    assert [t.source for t in captured] == ["histdata", "histdata"]


def test_histdata_refuses_dukascopy_only_flags(monkeypatch, capsys) -> None:
    import pytest

    captured = _capture_tasks(monkeypatch)
    base = ["--source", "histdata", "--symbols", "EURUSD", *_RANGE]
    for extra in (["--price-divisor", "1"], ["--probe"], ["--probe-ticks", "5"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(base + extra)
        assert exc.value.code == 2
        assert f"{extra[0]}: Dukascopy-only" in capsys.readouterr().err
    assert captured == []


def test_histdata_refuses_an_unmapped_symbol_before_any_export(monkeypatch, capsys) -> None:
    import pytest

    captured = _capture_tasks(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["--source", "histdata", "--symbols", "EURUSD", "BTCUSD", *_RANGE])
    assert "BTCUSD has no HistData mapping" in capsys.readouterr().err
    assert captured == []


def test_histdata_sidecar_reports_source_and_scale(monkeypatch, tmp_path: Path) -> None:
    import json

    bid_csv = tmp_path / "EURUSD_1H_bid.csv"
    bid_csv.touch()
    _capture_tasks(monkeypatch, csvs=[bid_csv])

    rc = cli.main(
        ["--source", "histdata", "--symbols", "EURUSD", *_RANGE]
        + ["--resample", "1h", "--out", str(tmp_path), "--no-cache"]
    )

    assert rc == 0
    meta = json.loads((tmp_path / "EURUSD_1H_bid.csv.meta.json").read_text())
    assert meta["source"] == "histdata"
    assert meta["price_divisor"] == 1e-4  # cache price = HistData price / 1e-4
    assert meta["params"]["scale_factor"] == 1e4


def test_dukascopy_sidecar_is_unchanged(monkeypatch, tmp_path: Path) -> None:
    import json

    bid_csv = tmp_path / "EURUSD_1H_bid.csv"
    bid_csv.touch()
    _capture_tasks(monkeypatch, csvs=[bid_csv])

    rc = cli.main(
        ["--symbols", "EURUSD", *_RANGE, "--price-divisor", "10"]
        + ["--resample", "1h", "--out", str(tmp_path), "--no-cache"]
    )

    assert rc == 0
    meta = json.loads((tmp_path / "EURUSD_1H_bid.csv.meta.json").read_text())
    assert meta["source"] == "dukascopy"
    assert meta["price_divisor"] == 10.0
    assert set(meta["params"]) == {"date_from", "date_to", "resample", "price_side"}


def test_help_documents_source(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "--source {dukascopy,histdata}" in out
    assert "USA500IDXUSD" in out
