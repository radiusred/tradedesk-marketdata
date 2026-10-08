"""CLI entry-point for cache scale rescaling.

Usage::

    tradedesk-md-rescale --cache-dir ./cache --symbols USDJPY [--dry-run]

Brings every per-day median onto the symbol's dominant scale by a power-of-ten
factor.  Days that cannot be reconciled to a power of ten are logged as
``unfixable`` and must be deleted + re-exported with the matching
``--price-divisor``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .rescale import rescale_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tradedesk-md-rescale",
        description=(
            "Rescale daily candle cache files onto the symbol's dominant scale. "
            "Use --dry-run to preview."
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
        help="Symbols to rescale (default: all subdirectories)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without modifying any files",
    )
    parser.add_argument(
        "--tolerance-log10",
        type=float,
        default=0.18,
        help=(
            "Match tolerance in log10 units (default 0.18 ≈ ±50%%). "
            "A day matches the dominant scale when "
            "abs(log10(day_median * factor / dominant)) < tolerance."
        ),
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

    results = rescale_cache(
        args.cache_dir,
        args.symbols,
        dry_run=args.dry_run,
        tolerance_log10=args.tolerance_log10,
    )

    total_fixed = 0
    total_unfixable = 0
    total_errors = 0
    for sym, r in sorted(results.items()):
        f = len(r["fixed"])
        u = len(r["unfixable"])
        e = len(r["errors"])
        c = len(r["unchanged"])
        total_fixed += f
        total_unfixable += u
        total_errors += e
        if f or u or e:
            action = "would fix" if args.dry_run else "fixed"
            log.info(
                "%s: %s %d days, unfixable=%d errors=%d (unchanged=%d)",
                sym,
                action,
                f,
                u,
                e,
                c,
            )

    log.info(
        "Done — %d day(s) %s, %d unfixable, %d errors",
        total_fixed,
        "would be rescaled" if args.dry_run else "rescaled",
        total_unfixable,
        total_errors,
    )

    if total_unfixable > 0:
        log.warning(
            "Unfixable days must be deleted and re-exported with the correct "
            "--price-divisor matching the dominant cache scale."
        )

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
