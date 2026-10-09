#!/usr/bin/env python3
"""Cross-source splice check: HistData vs Dukascopy daily closes over an overlap.

HistData and Dukascopy are different liquidity providers, so a cache whose
history is HistData up to some day and Dukascopy after it carries the basis
between the two at the join. This script measures that basis where both
sources have data, so you can see it before trusting a spliced series.

A cache holds one source per day (first committed wins), so the overlap comes
from two caches: the main cache (Dukascopy days) and a second cache that a
``tradedesk-md-export --source histdata`` run filled over the same dates
(HistData covers 2020 onwards too). Each cache's ``_sources.jsonl`` decides
which source a day is from; a day without a record predates provenance and is
taken to be Dukascopy's.

Methodology:
1. For each instrument, load both sources' 1-min bid/ask candles over
   [start, end] and form the mid close.
2. Per UTC day, take the last minute at or before 21:00 UTC that both sources
   have a candle for (the anchor ``dukascopy_cross_provider.py`` uses, but on a
   minute common to both so the comparison is like-for-like).
3. Report, in the cache's raw units (pips for FX, cents for gold, points for
   indices) and in percent: bias (mean HistData - Dukascopy), mean and median
   absolute difference, the largest divergence and its day, and Pearson r.
4. List the joins in the main cache: days where the recorded source changes.

Usage:
    python scripts/histdata_splice_check.py --cache ./cache \\
        --histdata-cache ./cache-histdata \\
        --instruments EURUSD USA500IDXUSD \\
        --start 2020-01-01 --end 2020-12-31 \\
        --out /tmp/histdata_splice.json
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import zstandard as zstd

ANCHOR = "21:00"


# ---------- cache loaders ----------


def load_day(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f:
            with dctx.stream_reader(f) as reader:
                df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")
    except Exception:
        return pd.DataFrame()


def load_sources(cache: Path, symbol: str) -> dict[str, str]:
    """Day (ISO) -> recorded source, from the symbol's ``_sources.jsonl``.

    Only commit records count: an ``"status": "excluded"`` record says the
    source wrote nothing for the day.
    """
    path = cache / symbol / "_sources.jsonl"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status", "committed") != "committed":
            continue
        out.setdefault(rec["day"], rec["source"])  # first committed wins
    return out


def load_mid(cache: Path, symbol: str, start: str, end: str, source: str) -> pd.Series:
    """1-min mid close for the days in [start, end] whose source is ``source``."""
    sources = load_sources(cache, symbol)
    frames = []
    for day in pd.date_range(start, end, freq="D"):
        recorded = sources.get(day.date().isoformat(), "dukascopy")
        if recorded != source:
            continue
        month_dir = cache / symbol / f"{day.year}" / f"{day.month - 1:02d}"
        bid = load_day(month_dir / f"{day.day:02d}_bid.csv.zst")
        ask = load_day(month_dir / f"{day.day:02d}_ask.csv.zst")
        if bid.empty or ask.empty:
            continue
        mid = ((bid["close"] + ask["close"]) / 2.0).dropna()
        frames.append(mid)
    if not frames:
        return pd.Series(dtype=float)
    s = pd.concat(frames).sort_index()
    return s[~s.index.duplicated(keep="last")]


def daily_closes(hd_mid: pd.Series, dc_mid: pd.Series) -> pd.DataFrame:
    """Per-day closes of both sources at the last common minute at or before the anchor."""
    both = pd.concat([hd_mid, dc_mid], axis=1, keys=["histdata", "dukascopy"]).dropna()
    if both.empty:
        return both
    both = both[both.index.strftime("%H:%M") <= ANCHOR]
    daily = both.groupby(both.index.normalize()).last()
    daily.index.name = "date"
    return daily


def find_joins(cache: Path, symbol: str) -> list[dict]:
    """Days where the recorded source changes from the previous committed day."""
    sources = load_sources(cache, symbol)
    joins = []
    prev_day, prev_src = None, None
    for day in sorted(sources):
        src = sources[day]
        if prev_src is not None and src != prev_src:
            joins.append({"last_day": prev_day, "from": prev_src, "first_day": day, "to": src})
        prev_day, prev_src = day, src
    return joins


def compare(daily: pd.DataFrame) -> dict:
    if len(daily) < 2:
        return {"status": "INSUFFICIENT_OVERLAP", "n_aligned_days": int(len(daily))}
    diff = daily["histdata"] - daily["dukascopy"]
    rel_pct = diff / daily["dukascopy"] * 100.0
    worst = diff.abs().idxmax()
    return {
        "status": "OK",
        "n_aligned_days": int(len(daily)),
        "first_day": daily.index.min().strftime("%Y-%m-%d"),
        "last_day": daily.index.max().strftime("%Y-%m-%d"),
        "bias_units": float(diff.mean()),
        "bias_pct": float(rel_pct.mean()),
        "mean_abs_diff_units": float(diff.abs().mean()),
        "mean_abs_diff_pct": float(rel_pct.abs().mean()),
        "median_abs_diff_units": float(diff.abs().median()),
        "p95_abs_diff_units": float(np.percentile(diff.abs(), 95)),
        "max_divergence": {
            "day": worst.strftime("%Y-%m-%d"),
            "histdata": float(daily.loc[worst, "histdata"]),
            "dukascopy": float(daily.loc[worst, "dukascopy"]),
            "diff_units": float(diff.loc[worst]),
            "diff_pct": float(rel_pct.loc[worst]),
        },
        "pearson_r": float(daily["histdata"].corr(daily["dukascopy"])),
    }


def check(cache: Path, histdata_cache: Path, symbol: str, start: str, end: str) -> dict:
    dc = load_mid(cache, symbol, start, end, "dukascopy")
    hd = load_mid(histdata_cache, symbol, start, end, "histdata")
    if dc.empty or hd.empty:
        result: dict = {"status": "NO_DUKASCOPY_DATA" if dc.empty else "NO_HISTDATA_DATA"}
    else:
        result = compare(daily_closes(hd, dc))
    result["symbol"] = symbol
    result["joins_in_cache"] = find_joins(cache, symbol)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", required=True, help="main cache (Dukascopy days)")
    ap.add_argument(
        "--histdata-cache",
        help="cache filled by --source histdata over the overlap (default: --cache)",
    )
    ap.add_argument("--instruments", nargs="+", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cache = Path(args.cache)
    hd_cache = Path(args.histdata_cache) if args.histdata_cache else cache
    report: dict = {
        "cache": str(cache),
        "histdata_cache": str(hd_cache),
        "start": args.start,
        "end": args.end,
        "anchor_utc": ANCHOR,
        "instruments": [],
    }
    for sym in args.instruments:
        print(f"\n=== {sym} ===")
        res = check(cache, hd_cache, sym, args.start, args.end)
        report["instruments"].append(res)
        if res["status"] != "OK":
            print(f"  {res['status']} ({res.get('n_aligned_days', 0)} aligned days)")
        else:
            worst = res["max_divergence"]
            print(
                f"  days={res['n_aligned_days']} [{res['first_day']}..{res['last_day']}] "
                f"bias={res['bias_units']:+.4f} ({res['bias_pct']:+.4f}%) "
                f"mean_abs={res['mean_abs_diff_units']:.4f} ({res['mean_abs_diff_pct']:.4f}%) "
                f"r={res['pearson_r']:.6f}"
            )
            print(
                f"  largest divergence {worst['day']}: histdata={worst['histdata']} "
                f"dukascopy={worst['dukascopy']} ({worst['diff_units']:+.4f}, "
                f"{worst['diff_pct']:+.4f}%)"
            )
        for j in res["joins_in_cache"]:
            print(f"  join: {j['last_day']} ({j['from']}) -> {j['first_day']} ({j['to']})")

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {args.out}")


if __name__ == "__main__":
    main()
