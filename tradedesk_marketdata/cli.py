import argparse
import logging
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from rich.logging import RichHandler

from . import sources
from .metadata import ExportMetadata, now_iso_utc, write_sidecar
from .source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    ActionRequest,
    Source,
    SourceError,
)

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


class _ExportParser(argparse.ArgumentParser):
    """An argument parser whose missing-``--source`` error names the sources."""

    def error(self, message: str) -> NoReturn:
        if message.startswith("the following arguments are required") and "--source" in message:
            message += f" (available sources: {', '.join(sources.names())})"
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    p = _ExportParser(prog="tradedesk-md-export")
    registered = [sources.get(name) for name in sources.names()]
    p.add_argument(
        "--source",
        choices=sources.names(),
        required=True,
        help="where to fetch ticks from (required). "
        + "; ".join(f"'{cls.name}': {cls.summary}" for cls in registered)
        + ". Every source writes the same cache files; a day already in the cache is never "
        "rewritten, whichever source wrote it. A source's own options (below) are refused "
        "with any other source.",
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
        help="Age (in UTC days) past which a source treats a gap as permanent and commits "
        "what it has; a partial day is recorded in the per-symbol _partial_days.jsonl manifest. "
        + "; ".join(f"{cls.name}: {cls.partial_commit_rule}" for cls in registered)
        + ". Default: 7",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help="Seconds to wait for a connection to the source on each request "
        f"(default: {DEFAULT_CONNECT_TIMEOUT:g})",
    )
    p.add_argument(
        "--read-timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT,
        help="Seconds to wait for the source to answer on each request; a slow "
        f"source needs this raised, not --retries (default: {DEFAULT_READ_TIMEOUT:g})",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Attempts per fetch unit ("
        + ", ".join(f"{cls.name}: one {cls.unit}" for cls in registered)
        + f") before it is skipped for this run (default: {DEFAULT_RETRIES})",
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
    for cls in registered:
        group = p.add_argument_group(f"{cls.name} options (--source {cls.name} only)")
        for opt in cls.options:
            if opt.flag_only:
                group.add_argument(opt.flag, dest=opt.dest, action="store_true", help=opt.help)
            else:
                kwargs: dict[str, object] = {"default": None, "metavar": opt.metavar}
                if opt.type is not None:
                    kwargs["type"] = opt.type
                group.add_argument(opt.flag, dest=opt.dest, help=opt.help, **kwargs)  # type: ignore[arg-type]
    return p


def _configure_source(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Source:
    """The chosen source with its options; another source's option is a usage error."""
    given = [
        (opt.flag, other)
        for other in sources.names()
        if other != args.source
        for opt in sources.get(other).options
        if opt.given(getattr(args, opt.dest))
    ]
    if given:
        parser.error(
            "; ".join(
                f"{flag} is a --source {other} option, not valid with --source {args.source}"
                for flag, other in given
            )
        )
    cls = sources.get(args.source)
    return cls({opt.dest: getattr(args, opt.dest) for opt in cls.options})


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

    source = _configure_source(parser, args)
    try:
        for symbol in args.symbols:
            source.check_symbol(symbol)
    except SourceError as e:
        parser.error(str(e))

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
    cache_dir = None if args.no_cache else args.cache_dir

    # A source command that replaces the export (e.g. a probe).
    try:
        rc = source.action(
            ActionRequest(
                symbols=list(args.symbols),
                start_utc=start_utc,
                end_utc_inclusive=end_utc,
                cache_dir=cache_dir,
                timeout=timeout,
                retries=args.retries,
            )
        )
    except SourceError as e:
        parser.error(str(e))
    except KeyboardInterrupt:
        return 130
    if rc is not None:
        return rc

    log.info(f"Processing {len(args.symbols)} symbols with up to {workers} workers")

    # Build export tasks
    from tradedesk_marketdata.parallel import ExportTask, run_parallel_exports

    out = Path(args.out) if args.out is not None else Path(tempfile.gettempdir())

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
