"""CLI entry point for re-checking a window of days in a store and topping it up.

Usage::

    tradedesk-md-refresh --cache-dir ./cache --since YYYY-MM-DD --backup-dir ./backups \\
        [--symbols SYMBOL ...] [--report report.json] [--dry-run] \\
        [--fallback-source NAME --price-divisor SYMBOL=N ... [--fallback-only days.txt]]

The primary source and the fallback sources come from the source registry
(``sources.REFRESH_PRIMARY``, ``sources.REFRESH_FALLBACKS``); like the export
CLI, this module names no source.

Exit status: see ``EXIT_STATUS``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

from . import sources
from .bad_ticks import DEFAULT_MAX_JUMP, DEFAULT_WINDOW, BadTickRule
from .cli import configure_logging
from .export import FloorError, _symbol_normalise
from .refresh import CLOSURES, RefreshError, RefreshOptions, refresh, store_symbols
from .source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    Source,
    SourceError,
)

PRIMARY = sources.get(sources.REFRESH_PRIMARY)

EXIT_STATUS = """exit status:
  0    the refresh ran (days still empty are reported, not an error)
  1    a day was refused (the scale sentry, a failed write) or a symbol's refresh failed,
       or the run was refused before any change (e.g. a backup directory inside the store)
  2    a usage error"""


def _day(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def _month_day(text: str) -> tuple[int, int]:
    try:
        d = datetime.strptime("2000-" + text.strip(), "%Y-%m-%d")  # a leap year: 02-29 is valid
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an MM-DD day: {text!r}") from None
    return (d.month, d.day)


def _divisor_flag(text: str) -> tuple[str, float]:
    symbol, sep, value = text.partition("=")
    try:
        if not sep or not symbol.strip():
            raise ValueError
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not SYMBOL=N: {text!r}") from None
    if not n > 0:
        raise argparse.ArgumentTypeError(f"{text!r}: the divisor must be above zero")
    return (_symbol_normalise(symbol), n)


def load_divisors(path: Path) -> dict[str, float]:
    """A TOML price-divisor file: one ``SYMBOL = N`` per line, N above zero."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{path}: cannot read the price-divisor file ({e})") from None
    out = {}
    for symbol, n in data.items():
        if isinstance(n, bool) or not isinstance(n, int | float) or not n > 0:
            raise ValueError(f"{path}: {symbol} must be a number above zero")
        key = _symbol_normalise(symbol)
        if key != symbol:
            raise ValueError(f"{path}: {symbol}: write the cache symbol as {key}")
        out[key] = float(n)
    return out


def load_day_list(path: Path) -> set[date]:
    """A day-list file: one ``YYYY-MM-DD`` per line; blank lines and ``#`` comments ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{path}: cannot read the day list ({e})") from None
    days = set()
    for n, line in enumerate(lines, 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        try:
            days.add(date.fromisoformat(text))
        except ValueError:
            raise ValueError(f"{path}:{n}: not a YYYY-MM-DD day: {text!r}") from None
    return days


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradedesk-md-refresh",
        epilog=EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            f"Re-check a window of days in an existing store and top it up from the primary "
            f"source ({PRIMARY.name}): add the missing days up to the newest day it has "
            "published, and re-export the empty or partial weekdays whose fresh data now has "
            "ticks and differs. A complete day is never rewritten. Every change is backed up "
            "first and reported."
        ),
    )
    p.add_argument("--cache-dir", type=Path, required=True, help="the store to refresh")
    p.add_argument(
        "--since", type=_day, required=True, help="first day of the window, UTC YYYY-MM-DD"
    )
    p.add_argument(
        "--until",
        type=_day,
        default=None,
        help="last day of the window, UTC YYYY-MM-DD (default: today, UTC)",
    )
    p.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYMBOL",
        help=f"symbols to refresh (default: every symbol in the store that {PRIMARY.name} serves)",
    )
    p.add_argument(
        "--backup-dir",
        type=Path,
        help="where each run copies what it changes, under a run-stamp subdirectory "
        "(required unless --dry-run; not inside the store)",
    )
    p.add_argument("--report", type=Path, metavar="PATH", help="write the JSON report here")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report the days the run would add, recheck and send to the fallback; "
        "make no request and write nothing",
    )
    p.add_argument(
        "--closure",
        type=_month_day,
        action="append",
        default=[],
        metavar="MM-DD",
        help="a further market-closure day, every year (repeatable; always: "
        + ", ".join(f"{m:02d}-{d:02d}" for m, d in CLOSURES)
        + ")",
    )
    fb = p.add_argument_group("fallback (only with --fallback-source)")
    fb.add_argument(
        "--fallback-source",
        choices=sources.REFRESH_FALLBACKS,
        help=f"fill the weekdays still empty after {PRIMARY.name} from this source",
    )
    fb.add_argument(
        "--price-divisors",
        type=Path,
        metavar="FILE",
        help="TOML file of SYMBOL = N: the fallback's price divisor per symbol, so that it "
        "writes the store's units",
    )
    fb.add_argument(
        "--price-divisor",
        type=_divisor_flag,
        action="append",
        default=[],
        metavar="SYMBOL=N",
        help="one symbol's fallback price divisor (repeatable; wins over --price-divisors)",
    )
    fb.add_argument(
        "--fallback-only",
        type=Path,
        metavar="FILE",
        help="limit the fallback to the days in this file (one YYYY-MM-DD per line)",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"seconds per connection, every source (default: {DEFAULT_CONNECT_TIMEOUT:g})",
    )
    p.add_argument(
        "--read-timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT,
        help=f"seconds per answer, every source (default: {DEFAULT_READ_TIMEOUT:g})",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"attempts per fetch unit, every source (default: {DEFAULT_RETRIES})",
    )
    p.add_argument(
        "--commit-partial-after-days",
        type=int,
        default=7,
        help="as for tradedesk-md-export (default: 7)",
    )
    p.add_argument("--bad-tick-max-jump", type=float, default=DEFAULT_MAX_JUMP, metavar="FRACTION")
    p.add_argument("--bad-tick-window", type=int, default=DEFAULT_WINDOW, metavar="TICKS")
    p.add_argument(
        "--log-level",
        choices=["fatal", "error", "warn", "info", "debug", "trace"],
        default="info",
    )
    group = p.add_argument_group(f"{PRIMARY.name} options (the primary source)")
    for opt in PRIMARY.options:
        if opt.flag_only:
            group.add_argument(opt.flag, dest=opt.dest, action="store_true", help=opt.help)
        else:
            kwargs: dict[str, object] = {"default": None, "metavar": opt.metavar}
            if opt.type is not None:
                kwargs["type"] = opt.type
            group.add_argument(opt.flag, dest=opt.dest, help=opt.help, **kwargs)  # type: ignore[arg-type]
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cache_dir.is_dir():
        parser.error(f"--cache-dir {args.cache_dir}: not a directory")
    if not args.dry_run and args.backup_dir is None:
        parser.error("a refresh that may change the store needs --backup-dir (or --dry-run)")
    fallback_opts = [
        flag
        for flag, given in (
            ("--price-divisors", args.price_divisors is not None),
            ("--price-divisor", bool(args.price_divisor)),
            ("--fallback-only", args.fallback_only is not None),
        )
        if given
    ]
    if fallback_opts and args.fallback_source is None:
        parser.error(f"{', '.join(fallback_opts)} need(s) --fallback-source")
    if args.connect_timeout <= 0 or args.read_timeout <= 0:
        parser.error("--connect-timeout and --read-timeout must be > 0")
    if args.retries < 1:
        parser.error("--retries must be >= 1")
    if args.commit_partial_after_days < 0:
        parser.error("--commit-partial-after-days must be >= 0")
    try:
        bad_tick_rule = BadTickRule(max_jump=args.bad_tick_max_jump, window=args.bad_tick_window)
    except ValueError as e:
        parser.error(f"bad-tick rule: {e}")
    until = args.until or datetime.now(UTC).date()

    try:
        primary = PRIMARY({opt.dest: getattr(args, opt.dest) for opt in PRIMARY.options})
    except SourceError as e:
        parser.error(str(e))
    skipped: dict[str, str] = {}
    if args.symbols:
        try:
            symbols = [_symbol_normalise(s) for s in args.symbols]
            for s in symbols:
                primary.check_symbol(s)
        except (ValueError, SourceError) as e:
            parser.error(str(e))
    else:
        symbols = []
        for s in store_symbols(args.cache_dir):
            try:
                primary.check_symbol(s)
                symbols.append(s)
            except SourceError:
                skipped[s] = f"no {primary.name} mapping"

    divisors: dict[str, float] = {}
    fallback_only = None
    if args.fallback_source is not None:
        try:
            if args.price_divisors is not None:
                divisors = load_divisors(args.price_divisors)
            if args.fallback_only is not None:
                fallback_only = load_day_list(args.fallback_only)
        except ValueError as e:
            parser.error(str(e))
        divisors.update(dict(args.price_divisor))
        without = [s for s in symbols if s not in divisors]
        if without:
            parser.error(
                f"--fallback-source {args.fallback_source} needs a price divisor for "
                f"{', '.join(without)} (--price-divisor SYMBOL=N or --price-divisors FILE)"
            )

    def fallback(symbol: str) -> Source:
        return sources.create(args.fallback_source, {"price_divisor": divisors[symbol]})

    configure_logging(level=args.log_level)
    log = logging.getLogger(__name__)
    for s, why in skipped.items():
        log.warning(f"{s}: not refreshed ({why})")
    try:
        report = refresh(
            cache_dir=args.cache_dir,
            symbols=symbols,
            since=args.since,
            until=until,
            primary=primary,
            fallback=fallback if args.fallback_source is not None else None,
            fallback_name=args.fallback_source,
            backup_dir=args.backup_dir,
            dry_run=args.dry_run,
            closures=(*CLOSURES, *args.closure),
            fallback_only=fallback_only,
            options=RefreshOptions(
                timeout=(args.connect_timeout, args.read_timeout),
                retries=args.retries,
                commit_partial_after_days=args.commit_partial_after_days,
                bad_tick_rule=bad_tick_rule,
            ),
        )
    except (RefreshError, FloorError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    report.skipped_symbols = skipped

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.report.with_suffix(args.report.suffix + ".tmp")
        tmp.write_text(json.dumps(report.as_json(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(args.report)
        log.info(f"Wrote report: {args.report}")
    print(f"tradedesk-md-refresh: {report.summary()}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
