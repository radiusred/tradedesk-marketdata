import argparse
import logging
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from rich.logging import RichHandler

from . import sources
from .metadata import ExportMetadata, now_iso_utc, write_sidecar
from .source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    SourceError,
)
from .sources.dukascopy import export_range
from .sources.histdata import HISTDATA_SYMBOLS

SOURCES = tuple(sources.names())
# Flags that only mean something for the Dukascopy datafeed.
_DUKASCOPY_ONLY = ("--price-divisor", "--probe", "--probe-ticks")

# Map CLI --log-level choices that are not valid stdlib `logging` level names
# onto their stdlib equivalents.
_LOG_LEVEL_ALIASES = {"FATAL": "CRITICAL", "TRACE": "DEBUG"}


def _resolve_log_level(level: str) -> str:
    """Resolve a CLI --log-level value to a stdlib `logging` level name."""
    upper = level.upper()
    return _LOG_LEVEL_ALIASES.get(upper, upper)


def configure_logging(level: str = "INFO") -> None:
    """
    Configure root logger with console output.
    Uses RichHandler if TTY, plain StreamHandler otherwise.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(_resolve_log_level(level))
    root_logger.handlers.clear()
    handler: logging.Handler

    if sys.stdout.isatty():
        # Rich handler for TTY - integrates with progress displays
        handler = RichHandler(
            show_time=True,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
        )
        formatter = logging.Formatter("%(message)s")
    else:
        # Plain handler for non-TTY
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradedesk-md-export")
    p.add_argument(
        "--source",
        choices=SOURCES,
        default="dukascopy",
        help="where to fetch ticks from (default: dukascopy). 'dukascopy' is Dukascopy's "
        "hourly datafeed; 'histdata' is HistData.com's monthly tick files, which reach back "
        "to 2000 for the FX majors and to 2010-11 for the indices, for the symbols in "
        f"its symbol table ({', '.join(sorted(HISTDATA_SYMBOLS))}). Both write the same "
        "cache files; a day already in the cache is never rewritten, whichever source "
        "wrote it. --price-divisor, --probe and --probe-ticks are Dukascopy-only.",
    )
    p.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        metavar="SYMBOL",
        help="One or more symbols to export (e.g., EURUSD GBPUSD)",
    )
    p.add_argument(
        "--from",
        dest="date_from",
        required=True,
        help="inclusive start date in UTC YYYY-MM-DD",
    )
    p.add_argument(
        "--to",
        dest="date_to",
        required=True,
        help="inclusive end date in UTC YYYY-MM-DD",
    )
    p.add_argument(
        "--resample",
        default=None,
        help="resample rule (candles only) - the sizing of the output candles, e.g. 5min, 1H, 1D",
    )
    p.add_argument(
        "--price-divisor",
        type=float,
        default=None,
        help="only used if Dukascopy tick prices are encoded as int32; "
        "divisor applied during decode and recorded in metadata (default: 1). "
        "Dukascopy only: HistData prices are scaled per symbol",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache/marketdata"),
        help="Cache directory for the raw downloads and the daily candle files "
        "(use --no-cache to disable)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable caching raw downloads and daily candles and always re-download",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Max parallel instrument workers (default: 4)",
    )
    p.add_argument(
        "--commit-partial-after-days",
        type=int,
        default=7,
        help="Age (in UTC days) past which a day with permanent-gap hours "
        "(404 / decode-failure) is committed from its available hours instead "
        "of being left for retry; the day is recorded in the per-symbol "
        "_partial_days.jsonl manifest. Use 0 to commit any permanent-gap day "
        "immediately (orphan-cache backfill sweep). With --source histdata, the "
        "age past the end of a month after which HistData's file for it is "
        "treated as final. Default: 7",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help="Seconds to wait for a connection to the datafeed on each request "
        f"(default: {DEFAULT_CONNECT_TIMEOUT:g})",
    )
    p.add_argument(
        "--read-timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT,
        help="Seconds to wait for the datafeed to answer on each request; a slow "
        f"datafeed needs this raised, not --retries (default: {DEFAULT_READ_TIMEOUT:g})",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Attempts per hour (Dukascopy) or per month (HistData) before it is skipped "
        f"for this run (default: {DEFAULT_RETRIES})",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="Probe one hour and print decoded ticks; no files written.",
    )
    p.add_argument(
        "--probe-ticks",
        type=int,
        default=None,
        help="Number of ticks to print when probing (default: 10)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output directory for exported CSV and metadata files",
    )
    p.add_argument(
        "--log-level",
        choices=["fatal", "error", "warn", "info", "debug", "trace"],
        default="info",
        help="Logging level (default: info)",
    )
    return p


def _wait_for_export_threads(log: logging.Logger) -> None:
    """After a cancel, wait for the export threads to finish their in-flight work.

    Without this the interpreter joins them at exit, where a second Ctrl-C
    surfaces as a traceback and exit code 1. Here a second Ctrl-C exits at once
    with 130. Every cache write is atomic (temp file + rename), so nothing is
    left half-written.
    """
    try:
        for t in threading.enumerate():
            if t is not threading.current_thread() and not t.daemon:
                t.join()
    except KeyboardInterrupt:
        log.warning("Interrupted again - exiting without waiting for in-flight downloads")
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)


def _parse_ymd(s: str) -> datetime:
    # Accept YYYY-MM-DD
    dt = datetime.strptime(s.strip(), "%Y-%m-%d")
    return dt.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validation checks
    if args.resample is not None and args.out is None:
        parser.error("--out is required when using --resample")

    if args.source == "histdata":
        given = [
            flag
            for flag, value in zip(
                _DUKASCOPY_ONLY, (args.price_divisor, args.probe, args.probe_ticks), strict=True
            )
            if value not in (None, False)
        ]
        if given:
            parser.error(
                f"{', '.join(given)}: Dukascopy-only, not valid with --source histdata "
                "(HistData prices are scaled into the cache's units by the symbol table)"
            )
    options = (
        {"price_divisor": args.price_divisor, "probe": args.probe, "probe_ticks": args.probe_ticks}
        if args.source == "dukascopy"
        else {}
    )
    source = sources.create(args.source, options)
    try:
        for symbol in args.symbols:
            source.check_symbol(symbol)
    except SourceError as e:
        parser.error(str(e))
    price_divisor = 1.0 if args.price_divisor is None else args.price_divisor
    probe_ticks = 10 if args.probe_ticks is None else args.probe_ticks

    start_utc = _parse_ymd(args.date_from)
    end_utc = _parse_ymd(args.date_to)
    if end_utc < start_utc:
        raise SystemExit("--to must be >= --from")
    if args.commit_partial_after_days < 0:
        raise SystemExit("--commit-partial-after-days must be >= 0")
    if args.connect_timeout <= 0 or args.read_timeout <= 0:
        raise SystemExit("--connect-timeout and --read-timeout must be > 0")
    if args.retries < 1:
        raise SystemExit("--retries must be >= 1")
    timeout = (args.connect_timeout, args.read_timeout)

    configure_logging(level=args.log_level)

    # Determine worker count
    workers = max(1, args.workers)

    log = logging.getLogger(__name__)
    log.info(f"Processing {len(args.symbols)} symbols with up to {workers} workers")

    # Handle probe mode (single symbol, single-threaded)
    if args.probe:
        if len(args.symbols) > 1:
            raise SystemExit("--probe mode only supports a single symbol")

        symbol = args.symbols[0]

        try:
            export_range(
                symbol=symbol,
                start_utc=start_utc,
                end_utc_inclusive=end_utc,
                resample_rule=args.resample,
                price_divisor=price_divisor,
                cache_dir=None if args.no_cache else args.cache_dir,
                probe=True,
                probe_ticks=probe_ticks,
                out=Path(tempfile.gettempdir()),
                timeout=timeout,
                retries=args.retries,
            )
        except KeyboardInterrupt:
            return 130

        return 0

    # Build export tasks
    from tradedesk_marketdata.parallel import ExportTask, run_parallel_exports

    out = Path(args.out) if args.out is not None else Path(tempfile.gettempdir())
    cache_dir = None if args.no_cache else args.cache_dir

    tasks = [
        ExportTask(
            symbol=symbol,
            start_utc=start_utc,
            end_utc_inclusive=end_utc,
            resample_rule=args.resample,
            cache_dir=cache_dir,
            out=out,
            source=source,
            commit_partial_after_days=args.commit_partial_after_days,
            timeout=timeout,
            retries=args.retries,
        )
        for symbol in args.symbols
    ]

    try:
        results = run_parallel_exports(tasks, max_workers=workers)

        # Write metadata for successful exports
        for result in results:
            if result.success:
                described = source.describe(result.symbol)
                for output_csv in result.output_csvs:
                    price_side = "bid" if output_csv.stem.endswith("_bid") else "ask"
                    params: dict[str, object] = {
                        "date_from": args.date_from,
                        "date_to": args.date_to,
                        "resample": args.resample,
                        "price_side": price_side,
                        **described.params,
                    }
                    meta = ExportMetadata(
                        schema_version="1",
                        source=source.name,
                        symbol=result.symbol,
                        data_type="candles",
                        timestamp_format="iso8601_utc",
                        price_divisor=float(described.price_divisor),
                        generated_at=now_iso_utc(),
                        params=params,
                    )
                    sidecar = write_sidecar(meta, output_csv)
                    # Only log in non-TTY mode
                    if not sys.stdout.isatty():
                        log.info(f"Wrote metadata sidecar: {sidecar}")

        # Summary
        succeeded = sum(1 for r in results if r.success)
        failed = len(results) - succeeded

        if failed > 0:
            log.warning(f"Completed: {succeeded} succeeded, {failed} failed")
            failed_symbols = [r.symbol for r in results if not r.success]
            log.warning(f"Failed symbols: {', '.join(failed_symbols)}")
            return 1
        else:
            # Only log success summary in non-TTY mode
            if not sys.stdout.isatty():
                log.info(f"All {succeeded} symbols exported successfully")
            return 0

    except KeyboardInterrupt:
        _wait_for_export_threads(log)
        return 130
