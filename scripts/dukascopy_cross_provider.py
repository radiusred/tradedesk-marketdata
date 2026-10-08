#!/usr/bin/env python3
"""Cross-provider correlation between Dukascopy and reference feeds.

References:
- Frankfurter (ECB official daily reference rates, https://api.frankfurter.app) for FX.
- Yahoo Finance v8 chart API for indices, metals, commodities, and FX fallback.

Methodology:
1. For each instrument, fetch daily reference close prices over [start, end].
2. Load the local Dukascopy 1-min mid-price series (normalised), resample to daily close
   sampled at 21:00 UTC (London close) — the closest universal anchor.
3. Compute Pearson correlation, mean abs error in pips (FX) or %, RMSE, max divergence,
   and bias.
4. Output a JSON report per instrument.

Usage:
    python scripts/dukascopy_cross_provider.py --cache ./cache \\
        --instruments EURUSD GBPUSD USDJPY \\
        --start 2024-01-01 --end 2025-12-31 \\
        --out /tmp/cross_provider.json
"""

from __future__ import annotations

import argparse
import io
import json
import sys as _sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import zstandard as zstd

_REPO_ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_REPO_ROOT))
from tradedesk_marketdata.normalize import _expected_price_range, infer_price_divisor  # noqa: E402

# Map our instrument symbols to Yahoo Finance tickers (used for indices/metals/commodities)
YAHOO_TICKER: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "EURGBP": "EURGBP=X",
    "AUDJPY": "AUDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "XAUUSD": "GC=F",  # gold futures continuous (good proxy for spot)
    "XAGUSD": "SI=F",  # silver futures continuous
    "USA500IDXUSD": "^GSPC",  # S&P 500 cash index
    "USATECHIDXUSD": "^NDX",  # Nasdaq 100 cash index
    "DEUIDXEUR": "^GDAXI",  # DAX 40
    "GBRIDXGBP": "^FTSE",  # FTSE 100
    "JPNIDXJPY": "^N225",  # Nikkei 225
    "BRENTCMDUSD": "BZ=F",  # Brent crude futures
    "LIGHTCMDUSD": "CL=F",  # WTI crude futures
}


# ---------- reference feed loaders ----------


def fetch_frankfurter(base: str, quote: str, start: str, end: str) -> pd.Series | None:
    """Fetch ECB reference rates (base→quote) as a daily-indexed Series."""
    url = f"https://api.frankfurter.app/{start}..{end}?from={base}&to={quote}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rad-audit/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rates = data.get("rates") or {}
        if not rates:
            return None
        s = pd.Series(
            {pd.Timestamp(d, tz="UTC"): float(r[quote]) for d, r in rates.items() if quote in r}
        )
        s.index.name = "date"
        return s.sort_index()
    except Exception as e:
        print(f"  frankfurter error for {base}/{quote}: {e}")
        return None


def fetch_yahoo(ticker: str, start: str, end: str, retries: int = 3) -> pd.Series | None:
    """Yahoo v8 chart API. Returns daily close indexed by UTC date."""
    p1 = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    p2 = int(datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        f"?period1={p1}&period2={p2}&interval=1d"
    )
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = (data.get("chart") or {}).get("result")
            if not result:
                return None
            r = result[0]
            ts = r.get("timestamp") or []
            indicators = (r.get("indicators") or {}).get("quote", [{}])[0]
            close = indicators.get("close") or []
            if not ts or not close:
                return None
            idx = [pd.Timestamp(t, unit="s", tz="UTC").normalize() for t in ts]
            s = pd.Series(close, index=idx, dtype="float64").dropna()
            s.index.name = "date"
            return s.sort_index()
        except Exception as e:
            print(f"  yahoo error attempt {attempt + 1} for {ticker}: {e}")
            time.sleep(1 + attempt)
    return None


def fetch_reference(symbol: str, start: str, end: str) -> tuple[pd.Series | None, str]:
    """Return (series, source_tag). Prefer Frankfurter for FX (official ECB)."""
    s = symbol.upper()
    fx_majors = {"EURUSD", "GBPUSD", "USDJPY", "EURGBP", "AUDUSD", "USDCHF", "USDCAD"}
    if s in fx_majors:
        # Frankfurter is base/quote; ECB publishes EUR-based, so do the right transform.
        base, quote = s[:3], s[3:]
        ser = fetch_frankfurter(base, quote, start, end)
        if ser is not None:
            return ser, "frankfurter/ecb"
        # fall back to Yahoo
    # For AUDJPY, JPY pairs without EUR base: cross via Frankfurter EUR triangulation
    if s == "AUDJPY":
        eur_aud = fetch_frankfurter("EUR", "AUD", start, end)
        eur_jpy = fetch_frankfurter("EUR", "JPY", start, end)
        if eur_aud is not None and eur_jpy is not None:
            df = pd.concat([eur_aud, eur_jpy], axis=1, keys=["EURAUD", "EURJPY"]).dropna()
            audjpy = df["EURJPY"] / df["EURAUD"]
            return audjpy, "frankfurter/ecb-cross"
    # Yahoo for indices/commodities/metals
    if s in YAHOO_TICKER:
        ser = fetch_yahoo(YAHOO_TICKER[s], start, end)
        if ser is not None:
            return ser, f"yahoo:{YAHOO_TICKER[s]}"
    return None, "none"


# ---------- Dukascopy loader (reuse audit-style) ----------


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


def load_dc_daily_close(cache: Path, symbol: str, start: str, end: str) -> pd.Series:
    """Load Dukascopy mid-price close at 21:00 UTC each business day."""
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    sym_dir = cache / symbol
    rows: list[pd.DataFrame] = []
    if not sym_dir.is_dir():
        return pd.Series(dtype=float)
    for year_dir in sorted(sym_dir.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        if not (start_ts.year <= int(year_dir.name) <= end_ts.year):
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for bid_path in sorted(month_dir.glob("*_bid.csv.zst")):
                ask_path = month_dir / bid_path.name.replace("_bid", "_ask")
                bid = load_day(bid_path)
                ask = load_day(ask_path)
                if bid.empty:
                    continue
                bid = bid.rename(columns={c: f"bid_{c}" for c in bid.columns})
                if not ask.empty:
                    ask = ask.rename(columns={c: f"ask_{c}" for c in ask.columns})
                    df = bid.join(ask, how="outer")
                else:
                    df = bid
                rows.append(df)
    if not rows:
        return pd.Series(dtype=float)
    full = pd.concat(rows).sort_index()
    full = full[~full.index.duplicated(keep="last")]

    # Normalise
    pmin, pmax = _expected_price_range(symbol)
    med = float(full["bid_close"].median())
    div = infer_price_divisor(med, pmin, pmax)
    if div != 1.0:
        for c in full.columns:
            if c.endswith(("_open", "_high", "_low", "_close")):
                full[c] = full[c] / div

    # Mid close
    if "ask_close" in full.columns:
        mid = (full["bid_close"].astype(float) + full["ask_close"].astype(float)) / 2.0
    else:
        mid = full["bid_close"].astype(float)

    # Sample at 21:00 UTC each day (just before FX session close)
    mid = mid[(mid.index >= start_ts) & (mid.index <= end_ts)]
    daily = mid.between_time("20:55", "21:05").resample("1D").last().dropna()
    daily.index = daily.index.tz_convert("UTC").normalize()
    return daily


def pip_for(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s and s.endswith("JPY"):
        return 0.01
    if "IDX" in s or "CMD" in s:
        return 1.0
    if s in ("XAUUSD", "XAGUSD"):
        return 0.01
    return 1e-4


def compare(dc: pd.Series, ref: pd.Series, pip: float) -> dict:
    df = pd.concat([dc, ref], axis=1, keys=["dc", "ref"]).dropna()
    if len(df) < 10:
        return {"n_aligned_days": int(len(df)), "status": "INSUFFICIENT_OVERLAP"}
    diff_price = df["dc"] - df["ref"]
    diff_pips = diff_price / pip
    rel = (df["dc"] - df["ref"]) / df["ref"]
    pearson = float(df["dc"].corr(df["ref"]))
    return {
        "status": "OK",
        "n_aligned_days": int(len(df)),
        "dc_first": df.index.min().strftime("%Y-%m-%d"),
        "dc_last": df.index.max().strftime("%Y-%m-%d"),
        "pearson_r": pearson,
        "mean_bias_pips": float(diff_pips.mean()) if pip != 1.0 else None,
        "median_abs_diff_pips": float(diff_pips.abs().median()) if pip != 1.0 else None,
        "p95_abs_diff_pips": float(np.percentile(diff_pips.abs(), 95)) if pip != 1.0 else None,
        "max_abs_diff_pips": float(diff_pips.abs().max()) if pip != 1.0 else None,
        "mean_bias_pct": float(rel.mean() * 100),
        "median_abs_diff_pct": float(rel.abs().median() * 100),
        "p95_abs_diff_pct": float(np.percentile(rel.abs(), 95) * 100),
        "max_abs_diff_pct": float(rel.abs().max() * 100),
        "rmse_pct": float(np.sqrt((rel**2).mean()) * 100),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--instruments", nargs="+", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cache = Path(args.cache)
    report: dict = {
        "cache": str(cache),
        "start": args.start,
        "end": args.end,
        "instruments": [],
    }

    for sym in args.instruments:
        print(f"\n=== {sym} ===")
        print("  loading Dukascopy daily close ...", flush=True)
        dc = load_dc_daily_close(cache, sym, args.start, args.end)
        if dc.empty:
            print("  NO_DC_DATA")
            report["instruments"].append({"symbol": sym, "status": "NO_DC_DATA"})
            continue
        print(f"  dc days: {len(dc)} [{dc.index.min().date()}..{dc.index.max().date()}]")
        print(f"  fetching reference for {sym} ...", flush=True)
        ref, src = fetch_reference(sym, args.start, args.end)
        if ref is None:
            print("  NO_REFERENCE")
            report["instruments"].append({"symbol": sym, "status": "NO_REFERENCE"})
            continue
        print(f"  ref source: {src} ({len(ref)} days)")
        cmp_ = compare(dc, ref, pip_for(sym))
        cmp_["symbol"] = sym
        cmp_["reference_source"] = src
        report["instruments"].append(cmp_)
        print(
            f"  -> pearson_r={cmp_.get('pearson_r')} "
            f"max_abs_diff_pct={cmp_.get('max_abs_diff_pct')}"
        )
        time.sleep(0.5)  # be polite

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {args.out}")


if __name__ == "__main__":
    main()
