#!/usr/bin/env python3
"""Audit a Dukascopy FX cache for scale-corrupt day files (e.g. raw int32
stored without dividing by 100 000).

For each ``DD_{bid,ask}.csv.zst`` under ``<cache_dir>/<SYMBOL>``, the script
checks whether the median close is inside the expected FX rate envelope.
A file is flagged ``hi`` when the median close is above ``--max``, ``lo``
when below ``--min``. Empty / unreadable files are reported as ``err``.

Usage::

    python scripts/audit_fx_scale.py NZDUSD --cache-dir ./cache --min 0.30 --max 2.00
    python scripts/audit_fx_scale.py NZDUSD --cache-dir ./cache --print-dates
"""

from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import zstandard as zstd


def _read_zst_close(path: Path) -> float | None:
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f_in, dctx.stream_reader(f_in) as reader:
            df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
    except (OSError, ValueError, zstd.ZstdError, pd.errors.ParserError, UnicodeDecodeError):
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    med = float(df["close"].median())
    if pd.isna(med):
        return None
    return med


def _iter_day_files(sym_dir: Path, side: str) -> list[Path]:
    files: list[Path] = []
    for year_dir in sorted(sym_dir.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            files.extend(sorted(month_dir.glob(f"*_{side}.csv.zst")))
    return files


def _parse_day(path: Path) -> tuple[int, int, int]:
    """Return (year, month_1based, day) from a cache path."""
    year = int(path.parts[-3])
    month0 = int(path.parts[-2])
    day = int(path.name.split("_")[0])
    return (year, month0 + 1, day)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="Symbol directory under --cache-dir (e.g. NZDUSD).")
    ap.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Cache root containing the symbol directory.",
    )
    ap.add_argument("--min", type=float, default=0.30, help="Lower envelope (default: 0.30).")
    ap.add_argument("--max", type=float, default=2.00, help="Upper envelope (default: 2.00).")
    ap.add_argument(
        "--print-dates",
        action="store_true",
        help="Print one ISO date per line for downstream re-fetch automation.",
    )
    args = ap.parse_args()

    sym_dir: Path = args.cache_dir / args.symbol
    if not sym_dir.is_dir():
        print(f"audit: missing symbol dir: {sym_dir}", file=sys.stderr)
        return 2

    flagged_dates: set[tuple[int, int, int]] = set()
    flags_bid: Counter[str] = Counter()
    flags_ask: Counter[str] = Counter()
    per_year: dict[int, set[tuple[int, int, int]]] = {}
    per_dow: Counter[int] = Counter()

    for side, counter in (("bid", flags_bid), ("ask", flags_ask)):
        for path in _iter_day_files(sym_dir, side):
            med = _read_zst_close(path)
            day = _parse_day(path)
            if med is None:
                counter["err"] += 1
                continue
            if med < args.min:
                counter["lo"] += 1
                flagged_dates.add(day)
                per_year.setdefault(day[0], set()).add(day)
            elif med > args.max:
                counter["hi"] += 1
                flagged_dates.add(day)
                per_year.setdefault(day[0], set()).add(day)
            else:
                counter["ok"] += 1

    for d in flagged_dates:
        dt = pd.Timestamp(*d)
        per_dow[dt.dayofweek] += 1

    if args.print_dates:
        for d in sorted(flagged_dates):
            print(f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}")
        return 0 if not flagged_dates else 1

    def _fmt(counter: Counter[str]) -> str:
        total = sum(counter.values())
        parts = [f"{k}={counter[k]}" for k in ("ok", "hi", "lo", "err")]
        return f"total={total} " + " ".join(parts)

    print(f"audit: {args.symbol} envelope=[{args.min}, {args.max}]")
    print(f"audit: bid {_fmt(flags_bid)}")
    print(f"audit: ask {_fmt(flags_ask)}")
    print(f"audit: unique flagged calendar dates: {len(flagged_dates)}")

    if not flagged_dates:
        return 0

    print("audit: per-year flagged-date counts")
    for yr in sorted(per_year):
        print(f"  {yr}: {len(per_year[yr])}")
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(
        "audit: day-of-week histogram " + " ".join(f"{dow_names[k]}:{per_dow[k]}" for k in range(7))
    )
    print("audit: flagged dates (sorted):")
    for d in sorted(flagged_dates):
        print(f"  {d[0]:04d}-{d[1]:02d}-{d[2]:02d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
