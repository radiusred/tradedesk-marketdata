"""CLI entry point for a symbol's earliest-date floor.

Usage::

    tradedesk-md-floor --cache-dir ./cache --symbol SYMBOL --floor YYYY-MM-DD \\
        --backup-dir ./retired [--lower] [--dry-run]
    tradedesk-md-floor --cache-dir ./cache --symbol SYMBOL --remove [--dry-run]

Exit status: see ``EXIT_STATUS``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .export import FloorError, _symbol_normalise
from .floor import remove_floor, set_floor


def _day(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


EXIT_STATUS = """exit status:
  0    the floor was set, lowered or removed, or it was already so (nothing written)
  1    refused: no such symbol, a floor below the current one without --lower,
       a backup directory inside the cache, a backup file with other bytes,
       or a floor file that does not parse; nothing was changed
  2    a usage error"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradedesk-md-floor",
        epilog=EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Set a symbol's earliest-available day (its floor). Every day before it moves, "
            "with its manifest lines, to --backup-dir; nothing is deleted. The export does "
            "not write a day before the floor and tradedesk-md-verify does not check one."
        ),
    )
    p.add_argument("--cache-dir", type=Path, required=True, help="root cache directory")
    p.add_argument("--symbol", required=True)
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--floor", type=_day, help="the first day to keep, UTC YYYY-MM-DD")
    what.add_argument(
        "--remove",
        action="store_true",
        help="remove the floor (the moved days stay in their backup)",
    )
    p.add_argument(
        "--backup-dir",
        type=Path,
        help="where the days before the floor go (required with --floor; not inside the cache)",
    )
    p.add_argument("--lower", action="store_true", help="allow a floor below the current one")
    p.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.floor is not None and args.backup_dir is None:
        parser.error("--floor needs --backup-dir")
    if args.remove and (args.backup_dir is not None or args.lower):
        parser.error("--remove takes neither --backup-dir nor --lower")
    if not args.cache_dir.is_dir():
        parser.error(f"--cache-dir {args.cache_dir}: not a directory")
    try:
        symbol = _symbol_normalise(args.symbol)
    except ValueError as e:
        parser.error(str(e))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    try:
        if args.remove:
            result = remove_floor(args.cache_dir, symbol, dry_run=args.dry_run)
        else:
            result = set_floor(
                args.cache_dir,
                symbol,
                args.floor,
                args.backup_dir,
                lower=args.lower,
                dry_run=args.dry_run,
            )
    except FloorError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1

    prefix = "dry run: would " if args.dry_run else ""
    if not result.changed:
        what = f"floor {result.floor.isoformat()}" if result.floor else "no floor"
        print(f"{symbol}: {what} already; nothing to do")
    elif result.action == "remove":
        assert result.previous is not None
        print(f"{symbol}: {prefix}remove the floor {result.previous.isoformat()}")
    else:
        assert result.floor is not None
        print(
            f"{symbol}: {prefix}{result.action} the floor {result.floor.isoformat()}; "
            f"{len(result.days_moved)} day(s) and {result.manifest_lines_moved} manifest "
            f"line(s) {'to move' if args.dry_run else 'moved'} to {args.backup_dir / symbol}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
