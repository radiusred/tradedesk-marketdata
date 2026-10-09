#!/usr/bin/env python3
"""Dukascopy data audit — gap, DST, spread, stale-price checks.

Reads the locally cached 1-min bid/ask candles produced by the existing pipeline
(`<cache>/<SYMBOL>/<YYYY>/<MM_zerobased>/<DD>_{bid,ask}.csv.zst`) and emits a
JSON report summarising data-quality findings per instrument.

Usage:
    python scripts/dukascopy_audit.py --cache ./cache \\
        --instruments EURUSD GBPUSD USDJPY \\
        --year-start 2024 --year-end 2025 \\
        --out /tmp/audit_report.json
"""

from __future__ import annotations

import argparse
import io
import json
import sys as _sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import zstandard as zstd

_REPO_ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_REPO_ROOT))
from tradedesk_marketdata.normalize import _expected_price_range, infer_price_divisor  # noqa: E402


def load_day(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f:
            with dctx.stream_reader(f) as reader:
                df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        return df
    except Exception:
        return pd.DataFrame()


def load_instrument(cache: Path, symbol: str, year_start: int, year_end: int) -> pd.DataFrame:
    """Load merged 1-min mid-price OHLC from bid/ask cache for date window."""
    rows: list[pd.DataFrame] = []
    sym_dir = cache / symbol
    if not sym_dir.is_dir():
        return pd.DataFrame()

    for year in range(year_start, year_end + 1):
        y_dir = sym_dir / str(year)
        if not y_dir.is_dir():
            continue
        for m_zb in range(12):
            m_dir = y_dir / f"{m_zb:02d}"
            if not m_dir.is_dir():
                continue
            for bid_path in sorted(m_dir.glob("*_bid.csv.zst")):
                day_str = bid_path.name.split("_")[0]
                ask_path = m_dir / f"{day_str}_ask.csv.zst"
                bid = load_day(bid_path)
                ask = load_day(ask_path)
                if bid.empty:
                    continue
                # Align on timestamp; rename for join
                bid = bid.rename(columns={c: f"bid_{c}" for c in bid.columns})
                if not ask.empty:
                    ask = ask.rename(columns={c: f"ask_{c}" for c in ask.columns})
                    df = bid.join(ask, how="outer")
                else:
                    df = bid
                rows.append(df)

    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


# ---------- audit checks ----------


@dataclass
class GapResult:
    total_minutes_in_window: int
    weekend_minutes: int
    expected_session_minutes: int
    observed_bars: int
    missing_session_bars: int
    missing_pct_of_session: float
    longest_intraday_gap_minutes: int
    longest_intraday_gap_start: str | None
    gaps_over_15min_count: int


def fx_session_mask(idx: pd.DatetimeIndex) -> pd.Series:
    """FX session: open Sun 22:00 UTC, close Fri 22:00 UTC. Returns True for session minutes."""
    dow = idx.dayofweek  # Mon=0 ... Sun=6
    hour = idx.hour
    # Closed: Sat (all), Sun 00:00–21:59 UTC, Fri 22:00 onwards.
    is_sat = dow == 5
    is_sun_closed = (dow == 6) & (hour < 22)
    is_fri_closed = (dow == 4) & (hour >= 22)
    return ~(is_sat | is_sun_closed | is_fri_closed)


def index_session_mask(idx: pd.DatetimeIndex) -> pd.Series:
    """CFD index session — rough approximation: weekday 00:00–22:00 UTC, off weekends."""
    dow = idx.dayofweek
    is_weekend = dow >= 5
    return ~is_weekend


def gap_audit(df: pd.DataFrame, is_index: bool = False) -> GapResult:
    if df.empty:
        return GapResult(0, 0, 0, 0, 0, 0.0, 0, None, 0)
    start = df.index.min().floor("1min")
    end = df.index.max().ceil("1min")
    full = pd.date_range(start, end, freq="1min", tz="UTC")
    if is_index:
        session = index_session_mask(full)
    else:
        session = fx_session_mask(full)
    expected = pd.Series(session, index=full)
    expected_session = int(expected.sum())
    total_minutes = len(full)
    weekend_minutes = total_minutes - expected_session

    observed_idx = df.index.intersection(full)
    observed = pd.Series(True, index=observed_idx).reindex(full, fill_value=False)
    missing_session = expected & (~observed)
    missing_count = int(missing_session.sum())

    # Longest intraday gap
    diffs = pd.Series(observed_idx).diff().dt.total_seconds() / 60.0
    intraday = diffs[(diffs > 1) & (diffs < 60 * 12)]  # exclude weekend gaps
    longest = int(intraday.max()) if not intraday.empty else 0
    longest_idx = observed_idx[int(intraday.idxmax())].isoformat() if not intraday.empty else None
    over_15 = int((intraday > 15).sum())

    return GapResult(
        total_minutes_in_window=total_minutes,
        weekend_minutes=weekend_minutes,
        expected_session_minutes=expected_session,
        observed_bars=int(observed.sum()),
        missing_session_bars=missing_count,
        missing_pct_of_session=round(100.0 * missing_count / max(expected_session, 1), 4),
        longest_intraday_gap_minutes=longest,
        longest_intraday_gap_start=longest_idx,
        gaps_over_15min_count=over_15,
    )


@dataclass
class DstResult:
    transitions_checked: int
    transitions_with_anomaly: int
    anomalies: list[dict] = field(default_factory=list)


def dst_audit(df: pd.DataFrame) -> DstResult:
    """For each London DST boundary in window, check minute-bar count per day.

    A clean UTC series should have exactly 1440 minute bars on a full session day,
    regardless of DST. Anomaly: bar count materially off from expected for adjacent
    full-session days.
    """
    if df.empty:
        return DstResult(0, 0, [])
    # London DST: last Sunday of March (spring forward), last Sunday of October (fall back).
    yrs = sorted({df.index.min().year, df.index.max().year, df.index.max().year + 1})

    boundaries: list[pd.Timestamp] = []
    for y in yrs:
        for m in (3, 10):
            for d in range(31, 24, -1):
                try:
                    ts = pd.Timestamp(year=y, month=m, day=d, tz="UTC")
                except ValueError:
                    continue
                if ts.dayofweek == 6:
                    boundaries.append(ts)
                    break

    bar_counts = df.resample("1D").size()

    anomalies: list[dict] = []
    checked = 0
    for b in boundaries:
        if b < df.index.min() or b > df.index.max():
            continue
        checked += 1
        # Compare boundary day's bar count to mean of surrounding weekdays
        window_start = b - pd.Timedelta(days=10)
        window_end = b + pd.Timedelta(days=10)
        nearby = bar_counts.loc[window_start:window_end]
        # exclude weekends
        weekdays = nearby[nearby.index.dayofweek < 5]
        if len(weekdays) < 4:
            continue
        median = float(weekdays.median())
        boundary_count = int(bar_counts.get(b.normalize(), 0))
        # Also check day-after (where DST drift typically manifests)
        day_after = b + pd.Timedelta(days=1)
        day_after_count = int(bar_counts.get(day_after.normalize(), 0))
        for tag, val in [("boundary_sun", boundary_count), ("day_after_mon", day_after_count)]:
            if abs(val - median) > 60:  # >1 hour off median
                anomalies.append(
                    {
                        "date": (b if tag == "boundary_sun" else day_after).strftime("%Y-%m-%d"),
                        "tag": tag,
                        "bar_count": val,
                        "weekday_median": median,
                        "delta_minutes": val - median,
                    }
                )

    return DstResult(
        transitions_checked=checked,
        transitions_with_anomaly=len(anomalies),
        anomalies=anomalies,
    )


@dataclass
class SpreadResult:
    n_bars_with_spread: int
    median_spread: float
    p05_spread: float
    p95_spread: float
    p99_spread: float
    impossible_narrow_bars: int  # spread <= 0
    extreme_wide_bars: int  # spread > 10x p95
    by_hour_utc: dict[str, dict] = field(default_factory=dict)


def spread_audit(df: pd.DataFrame, pip_factor: float = 1e-4) -> SpreadResult:
    """Use bid_close / ask_close to estimate per-bar spread."""
    if df.empty or "bid_close" not in df.columns or "ask_close" not in df.columns:
        return SpreadResult(0, 0, 0, 0, 0, 0, 0)
    bid = df["bid_close"].astype(float)
    ask = df["ask_close"].astype(float)
    sp = (ask - bid) / pip_factor  # in pips (FX) or just price units
    sp = sp.replace([np.inf, -np.inf], np.nan).dropna()
    if sp.empty:
        return SpreadResult(0, 0, 0, 0, 0, 0, 0)
    p05, p50, p95, p99 = np.percentile(sp, [5, 50, 95, 99])
    extreme_thr = 10 * p95 if p95 > 0 else float("inf")
    impossible = int((sp <= 0).sum())
    extreme = int((sp > extreme_thr).sum())

    # by-hour
    by_hour: dict[str, dict] = {}
    sp_with_hour = pd.DataFrame({"sp": sp, "hour": sp.index.hour})
    for h, grp in sp_with_hour.groupby("hour"):
        vals = grp["sp"].values
        if len(vals) < 100:
            continue
        ph = np.percentile(vals, [50, 95])
        by_hour[str(int(h))] = {
            "n": int(len(vals)),
            "median": float(ph[0]),
            "p95": float(ph[1]),
        }

    return SpreadResult(
        n_bars_with_spread=int(len(sp)),
        median_spread=float(p50),
        p05_spread=float(p05),
        p95_spread=float(p95),
        p99_spread=float(p99),
        impossible_narrow_bars=impossible,
        extreme_wide_bars=extreme,
        by_hour_utc=by_hour,
    )


@dataclass
class StaleResult:
    n_bars: int
    n_stale_bars: int  # OHLC all equal AND equal to previous
    longest_run: int
    longest_run_start: str | None
    runs_gt_5_count: int


def stale_audit(df: pd.DataFrame) -> StaleResult:
    if df.empty or "bid_close" not in df.columns:
        return StaleResult(0, 0, 0, None, 0)
    needed = ["bid_open", "bid_high", "bid_low", "bid_close"]
    if not all(c in df.columns for c in needed):
        return StaleResult(0, 0, 0, None, 0)
    rows = df[needed].astype(float)
    flat_within = (
        (rows["bid_open"] == rows["bid_high"])
        & (rows["bid_high"] == rows["bid_low"])
        & (rows["bid_low"] == rows["bid_close"])
    )
    same_as_prev = rows["bid_close"] == rows["bid_close"].shift(1)
    stale_mask = flat_within & same_as_prev

    # find runs
    longest = 0
    longest_start = None
    runs_gt_5 = 0
    cur = 0
    cur_start = None
    for ts, val in stale_mask.items():
        if val:
            if cur == 0:
                cur_start = ts
            cur += 1
        else:
            if cur > 5:
                runs_gt_5 += 1
            if cur > longest:
                longest = cur
                longest_start = cur_start
            cur = 0
            cur_start = None
    if cur > 5:
        runs_gt_5 += 1
    if cur > longest:
        longest = cur
        longest_start = cur_start

    return StaleResult(
        n_bars=int(len(rows)),
        n_stale_bars=int(stale_mask.sum()),
        longest_run=int(longest),
        longest_run_start=longest_start.isoformat() if longest_start is not None else None,
        runs_gt_5_count=int(runs_gt_5),
    )


def pip_for(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s and s.endswith("JPY"):
        return 0.01
    if "IDX" in s or "CMD" in s:  # indices and commodities use raw price units
        return 1.0
    if s in ("XAUUSD", "XAGUSD"):
        return 0.01
    return 1e-4


def normalize_prices(df: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, float]:
    """Apply per-day price divisor inferred from median bid_close."""
    if df.empty or "bid_close" not in df.columns:
        return df, 1.0
    pmin, pmax = _expected_price_range(symbol)
    # Use full-window median to infer one divisor — close enough for audit purposes;
    # if some days were normalized and others weren't we'll catch that in spread anomaly.
    med = float(df["bid_close"].median())
    div = infer_price_divisor(med, pmin, pmax)
    if div != 1.0:
        df = df.copy()
        for c in df.columns:
            if (c.startswith("bid_") and c != "bid_volume") or (
                c.startswith("ask_") and c != "ask_volume"
            ):
                df[c] = df[c] / div
    return df, div


def audit_instrument(cache: Path, symbol: str, year_start: int, year_end: int) -> dict:
    df = load_instrument(cache, symbol, year_start, year_end)
    if df.empty:
        return {"symbol": symbol, "status": "NO_DATA"}
    df, divisor = normalize_prices(df, symbol)
    is_index = "IDX" in symbol.upper()
    pip = pip_for(symbol)
    gap = gap_audit(df, is_index=is_index)
    dst = dst_audit(df)
    spr = spread_audit(df, pip_factor=pip)
    stale = stale_audit(df)
    return {
        "symbol": symbol,
        "status": "OK",
        "first_bar": df.index.min().isoformat(),
        "last_bar": df.index.max().isoformat(),
        "total_bars": int(len(df)),
        "pip_factor": pip,
        "inferred_divisor": divisor,
        "gap": asdict(gap),
        "dst": asdict(dst),
        "spread_pips": asdict(spr) if pip != 1.0 else None,
        "spread_raw": asdict(spr) if pip == 1.0 else None,
        "stale": asdict(stale),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--instruments", nargs="+", required=True)
    ap.add_argument("--year-start", type=int, required=True)
    ap.add_argument("--year-end", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cache = Path(args.cache)
    report = {
        "cache": str(cache),
        "year_start": args.year_start,
        "year_end": args.year_end,
        "instruments": [],
    }
    for sym in args.instruments:
        print(f"Auditing {sym} ...", flush=True)
        r = audit_instrument(cache, sym, args.year_start, args.year_end)
        report["instruments"].append(r)
        print(f"  -> {r.get('status')} bars={r.get('total_bars')}", flush=True)

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {args.out}")


if __name__ == "__main__":
    main()
