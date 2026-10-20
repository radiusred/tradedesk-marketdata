"""CLI entry point for central-bank daily rate series in a store's reference-data area.

Usage::

    tradedesk-md-rates fetch --store ./cache --config my-series.toml
    tradedesk-md-rates verify --store ./cache

Exit status: see ``EXIT_STATUS``.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from pathlib import Path

from . import rates as r
from .cancel import cancellation
from .source import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, DEFAULT_RETRIES

EXIT_STATUS = """exit status:
  0    every series fetched (or verified) without a failure
  1    a series failed: a download that did not arrive or did not parse, a raw file
       that is missing or does not match its sha256, or a parsed file or manifest
       that the raw files do not reproduce
  2    a usage error (including a configuration file that is not valid)
  130  interrupted (Ctrl-C)"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradedesk-md-rates",
        epilog=EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Fetch public central-bank daily rate series into the store's reference-data "
            f"area ({r.AREA}/<id>/), and verify that the raw files reproduce the parsed files."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser(
        "fetch",
        help="download the configured series and rebuild their files",
        epilog=EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    f.add_argument("--store", type=Path, required=True, help="the store root (the cache dir)")
    f.add_argument("--config", type=Path, required=True, help="TOML file of [[series]] tables")
    f.add_argument("--series", nargs="+", metavar="ID", help="only these ids from --config")
    f.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    f.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    f.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    f.add_argument("--format", choices=("text", "json"), default="text")

    v = sub.add_parser(
        "verify",
        help="reproduce each series' files from its raw files; writes nothing",
        epilog=EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    v.add_argument("--store", type=Path, required=True, help="the store root (the cache dir)")
    v.add_argument("--series", nargs="+", metavar="ID", help="default: every series in the store")
    v.add_argument("--format", choices=("text", "json"), default="text")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.store.is_dir():
        parser.error(f"--store {args.store}: not a directory")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cancellation.clear()
    try:
        if args.command == "fetch":
            return _fetch(parser, args)
        return _verify(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _fetch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.retries < 1:
        parser.error("--retries must be >= 1")
    try:
        configs = r.load_config(args.config)
    except r.RatesError as e:
        parser.error(str(e))
    if args.series:
        known = {c.id for c in configs}
        unknown = [s for s in args.series if s not in known]
        if unknown:
            parser.error(f"--series: not in {args.config}: {', '.join(unknown)}")
        configs = [c for c in configs if c.id in args.series]
    get = functools.partial(
        r.download, timeout=(args.connect_timeout, args.read_timeout), retries=args.retries
    )
    results = r.fetch(args.store, configs, get=get)
    if args.format == "json":
        json.dump({"series": [x.as_json() for x in results]}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(r.render_fetch_text(results))
    return 1 if any(x.status == "failed" for x in results) else 0


def _verify(args: argparse.Namespace) -> int:
    reports = r.verify(args.store, args.series)
    if args.format == "json":
        json.dump({"series": [x.as_json() for x in reports]}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(r.render_verify_text(reports))
    return 1 if any(x.failed for x in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
