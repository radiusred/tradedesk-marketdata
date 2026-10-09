"""CLI entry-point for cache price-scale normalisation.

Usage::

    tradedesk-md-normalize --cache-dir ./cache [--symbols EURUSD AUDNZD] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .normalize import normalize_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tradedesk-md-normalize",
        description=(
            "Detect and correct price-scale errors in a candle cache.\n\n"
            "Use --dry-run first to preview what would be changed."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Root cache directory (e.g. ./cache)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        metavar="SYMBOL",
        help="Symbols to normalize (default: all subdirectories)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without modifying any files",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    log = logging.getLogger(__name__)

    if not args.cache_dir.is_dir():
        log.error("Cache directory not found: %s", args.cache_dir)
        return 1

    if args.dry_run:
        log.info("DRY-RUN mode — no files will be modified")

    results = normalize_cache(args.cache_dir, args.symbols, dry_run=args.dry_run)

    total_fixed = sum(r["fixed"] for r in results.values())
    total_errors = sum(r["errors"] for r in results.values())

    for sym, r in sorted(results.items()):
        if r["fixed"] > 0 or r["errors"] > 0:
            action = "would fix" if args.dry_run else "fixed"
            log.info(
                "%s: %s %d day(s), %d error(s)",
                sym,
                action,
                r["fixed"],
                r["errors"],
            )

    log.info(
        "Done — %d day(s) %s, %d error(s)",
        total_fixed,
        "would be normalized" if args.dry_run else "normalized",
        total_errors,
    )
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
