"""CLI entry point for comparing a cache with a reference cache.

Usage::

    tradedesk-md-verify --reference ./trusted-cache --cache-dir ./new-cache \\
        --symbols EURUSD USA500IDXUSD --from 2020-01-01 --to 2020-12-31

Exit status: see ``EXIT_STATUS``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from . import verify as v
from .cancel import cancellation
from .export import _symbol_normalise


def _day(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


EXIT_STATUS = """exit status:
  0    the cache matches the reference over the days compared
  1    a finding: a day shifted by whole minutes, a day differing beyond --tolerance,
       a close-ratio regime other than 1.0, an unreadable day file, or a symbol with
       no day that has data on both sides (nothing was compared)
  2    a usage error
  130  interrupted (Ctrl-C)"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradedesk-md-verify",
        epilog=EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Compare the day files of a cache with a reference cache of the same symbols, "
            "day by day: whole-minute timestamp shifts, daily-bar differences and "
            "close-ratio regimes. Reads both caches; writes nothing."
        ),
    )
    p.add_argument("--reference", type=Path, required=True, help="the cache you trust")
    p.add_argument("--cache-dir", type=Path, required=True, help="the cache to check")
    p.add_argument("--symbols", nargs="+", required=True, metavar="SYMBOL")
    p.add_argument("--from", dest="date_from", type=_day, required=True, help="UTC YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", type=_day, required=True, help="UTC YYYY-MM-DD")
    p.add_argument(
        "--side", choices=("bid", "ask"), default="bid", help="price side to compare (default: bid)"
    )
    p.add_argument(
        "--tolerance",
        type=float,
        default=v.DEFAULT_TOLERANCE,
        help="largest relative difference of a day's open/high/low/close that still counts "
        f"as the same day (default: {v.DEFAULT_TOLERANCE:g})",
    )
    p.add_argument(
        "--include-volume",
        action="store_true",
        help="compare volume as well in the minute-level identity (sources differ on it, "
        "so it is excluded by default)",
    )
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--workers", type=int, default=4, help="parallel day comparisons (default: 4)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.date_to < args.date_from:
        parser.error("--to must be on or after --from")
    if args.tolerance < 0:
        parser.error("--tolerance must be >= 0")
    for flag, path in (("--reference", args.reference), ("--cache-dir", args.cache_dir)):
        if not path.is_dir():
            parser.error(f"{flag} {path}: not a directory")
    try:
        symbols = [_symbol_normalise(s) for s in args.symbols]
    except ValueError as e:
        parser.error(str(e))

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cancellation.clear()
    try:
        reports = v.verify(
            args.reference,
            args.cache_dir,
            symbols,
            args.date_from,
            args.date_to,
            side=args.side,
            tolerance=args.tolerance,
            include_volume=args.include_volume,
            workers=args.workers,
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.format == "json":
        json.dump(v.render_json(reports), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(v.render_text(reports))
    return 1 if any(r.failed for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
