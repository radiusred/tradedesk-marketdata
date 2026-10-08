"""
Export Dukascopy historical data by downloading .bi5 *tick* files and resampling to candles.

- Downloads hourly tick files:
    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
  where MM is zero-based (Jan=00..Dec=11).

- Decompresses LZMA .bi5
- Decodes ticks (bid/ask + volumes)
- Resamples ticks into bid-side and ask-side OHLCV candles
- Writes one CSV per price side for the requested date range when
  ``resample_rule`` is provided

Output format:
timestamp,open,high,low,close,volume
(UTC, rendered as ``YYYY-MM-DD HH:MM:SS+00:00`` in the exported CSV)

- Prices are floats, volumes are floats.
- Month in URL is zero-based. See Dukascopy datafeed conventions.

Examples:
  tradedesk-md-export --symbols EURUSD \
    --from 2025-08-01 --to 2025-12-31 \
    --resample 5min \
    --out out

  tradedesk-md-export --symbols USA500IDXUSD \
    --from 2025-11-01 --to 2025-12-31 \
    --resample 5min \
    --out out
"""

import io
import json
import logging
import lzma
import math
import shutil
import struct
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests  # type: ignore[import-untyped]
import zstandard as zstd
from rich.progress import Progress, TaskID

from .cancel import cancellation, check_cancelled
from .scale_sentry import check_scale_consistency

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
UA = "tradedesk/1.0 bi5-export (https://github.com/radiusred/tradedesk-marketdata)"
# Retry configuration
RETRY_BASE_DELAY = 0.8  # seconds
RETRY_MAX_DELAY = 6.0  # seconds
RETRY_BACKOFF_FACTOR = 2.5
# Per-request HTTP timeouts (connect, read) in seconds and attempts per hour.
# Dukascopy's datafeed can take 20 s+ to answer a single hour under load, so
# these are overridable from the CLI (--connect-timeout, --read-timeout, --retries).
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_RETRIES = 3
# Download parallelisation
DOWNLOAD_THREADS_PER_INSTRUMENT = 2

# Dukascopy .bi5 tick record layout: 20 bytes per tick (>i f f f f or >i i i f f).
_TICK_RECORD_SIZE = 20
# Minimum decompressed-source bi5 payload to consider non-junk; tiny non-zero
# payloads are treated as "no data" for the hour.
_MIN_PAYLOAD_BYTES = 64

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA})

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tick:
    ts: datetime
    bid: float
    ask: float
    bid_vol: float
    ask_vol: float


def _symbol_normalise(s: str) -> str:
    """
    Accept inputs like:
      - EURUSD
      - USDJPY
      - USA500.IDX/USD
      - GBR.IDX/GBP
      - usa500idxusd
    Convert to Dukascopy datafeed folder naming, typically uppercase alnum only.
    """
    raw = s.strip()
    if not raw:
        raise ValueError("Empty symbol")

    # Remove separators
    cleaned = "".join(ch for ch in raw if ch.isalnum())
    # Datafeed folders are typically uppercase
    return cleaned.upper()


def _iter_hours(start: datetime, end_exclusive: datetime) -> Iterable[datetime]:
    """
    Yield hour starts [start, end_exclusive) at hourly granularity, UTC.
    """
    cur = start.replace(minute=0, second=0, microsecond=0)
    if cur < start:
        cur += timedelta(hours=1)
    while cur < end_exclusive:
        yield cur
        cur += timedelta(hours=1)


def _dukascopy_tick_url(symbol: str, hour_start: datetime) -> str:
    """
    Dukascopy uses zero-based months in the URL: Jan=00 ... Dec=11
    """
    y = hour_start.year
    m0 = hour_start.month - 1
    d = hour_start.day
    h = hour_start.hour
    return f"{BASE_URL}/{symbol}/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"


def _download_bi5(
    url: str,
    cache_path: Path | None,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> bytes | None:
    """
    Returns compressed bytes.

    - None means "no file" (HTTP 404) or unrecoverable failure.
    - b"" means "valid but empty" (HTTP 200 with zero-length body): no tick data for that hour.

    We cache empty payloads as empty files so repeated exports do not re-download them.

    Uses exponential backoff on retries: 0.5s, 1.0s, 2.0s, 4.0s (capped).

    Raises KeyboardInterrupt as soon as a cancel is pending: before each attempt
    and from inside a backoff sleep, so a cancelled export never starts another
    request or sits out a backoff.
    """
    # If cached, return it even if it's 0 bytes (0 bytes means "no ticks for this hour")
    if cache_path is not None and cache_path.exists():
        return cache_path.read_bytes()

    last_exc: Exception | None = None
    delay = RETRY_BASE_DELAY

    for attempt in range(1, retries + 1):
        check_cancelled()
        try:
            with _SESSION.get(url, timeout=timeout) as r:
                if r.status_code == 404:
                    log.info("no tick data found (HTTP 404): %s", url)
                    return None
                r.raise_for_status()
                data: bytes = bytes(r.content)

            # HTTP 200 with empty body is valid: "no ticks this hour"
            if len(data) == 0:
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.touch(exist_ok=True)  # cache the "empty hour"
                return b""

            # Tiny non-zero payloads are usually junk/edge; keep existing behavior.
            if len(data) < _MIN_PAYLOAD_BYTES:
                log.debug("tiny bi5 payload (%d bytes) for %s; treating as no data", len(data), url)
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.touch(exist_ok=True)
                return b""

            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(cache_path)

            return data

        except Exception as e:
            last_exc = e
            log.debug("download attempt %d/%d failed for %s: %s", attempt, retries, url, e)

            # Backoff before retry (but not after final attempt); a cancel ends the sleep early
            if attempt < retries:
                if cancellation.wait(delay):
                    raise KeyboardInterrupt() from None
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

    log.warning("skipping %s after %d failed attempts (%s)", url, retries, last_exc)
    return None


def _probe_price_format(compressed: bytes) -> str:
    """
    Read only the first 20-byte tick record via streaming LZMA and decide whether
    bid/ask are float32 or int32.

    Heuristic:
      - interpret ask/bid as float32: if non-finite OR absurdly small (subnormal/near-zero)
        then treat as int32.
    """
    try:
        with lzma.open(io.BytesIO(compressed), "rb") as f:
            first = f.read(_TICK_RECORD_SIZE)

        if len(first) < _TICK_RECORD_SIZE:
            raise ValueError("bi5 too short to probe")

    except EOFError as e:
        raise ValueError("Not enough decompressed bytes to probe tick format") from e

    # float layout: >i f f f f
    ms, ask_f, bid_f, ask_v, bid_v = struct.unpack(">i f f f f", first)

    if (not math.isfinite(ask_f)) or (not math.isfinite(bid_f)):
        return "int"

    # Float mis-decode often yields tiny denormals ~1e-38 for indices.
    if abs(ask_f) < 1e-6 and abs(bid_f) < 1e-6:
        return "int"

    return "float"


def _read_n_tick_records(compressed: bytes, n: int) -> bytes:
    # Stream-decompress just enough to read n tick records.
    need = _TICK_RECORD_SIZE * n
    with lzma.open(io.BytesIO(compressed), "rb") as f:
        return f.read(need)


def _decode_ticks(
    hour_start: datetime, compressed: bytes, *, price_format: str, price_divisor: float
) -> list[Tick]:
    """
    Decode a .bi5 tick file.

    Layout per tick row (20 bytes):
      int32  ms_since_hour_start
      float32 ask
      float32 bid
      float32 ask_volume
      float32 bid_volume

    Endianness: big-endian is commonly used in bi5 decoders.
    """
    raw = lzma.decompress(compressed)
    if len(raw) % _TICK_RECORD_SIZE != 0:
        raise ValueError(
            f"Unexpected bi5 payload length: {len(raw)} (not multiple of {_TICK_RECORD_SIZE})"
        )

    ticks: list[Tick] = []

    if price_format == "float":
        unpack = struct.Struct(">i f f f f").unpack_from
        for i in range(0, len(raw), _TICK_RECORD_SIZE):
            ms, ask, bid, ask_vol, bid_vol = unpack(raw, i)
            ts = hour_start + timedelta(milliseconds=int(ms))
            ticks.append(
                Tick(
                    ts=ts,
                    bid=float(bid),
                    ask=float(ask),
                    bid_vol=float(bid_vol),
                    ask_vol=float(ask_vol),
                )
            )
        return ticks

    if price_format == "int":
        div = float(price_divisor or 1.0)
        unpack = struct.Struct(">i i i f f").unpack_from  # ask,bid as int32
        for i in range(0, len(raw), _TICK_RECORD_SIZE):
            ms, ask_i, bid_i, ask_vol, bid_vol = unpack(raw, i)
            ts = hour_start + timedelta(milliseconds=int(ms))
            ticks.append(
                Tick(
                    ts=ts,
                    bid=float(bid_i) / div,
                    ask=float(ask_i) / div,
                    bid_vol=float(bid_vol),
                    ask_vol=float(ask_vol),
                )
            )
        return ticks

    raise ValueError("price_format must be 'float' or 'int'")


def _ticks_to_candles(
    ticks: list[Tick],
    *,
    resample_rule: str,
    price_side: str = "bid",
) -> pd.DataFrame:
    """
    Resample ticks to OHLCV using a pandas resample rule (e.g. '1min', '5min', '15min', '1H').
    Volume uses bid_vol (for bid) or ask_vol (for ask); if mid, uses (bid_vol+ask_vol)/2.
    """
    if not ticks:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    idx = pd.DatetimeIndex([t.ts for t in ticks], tz="UTC")
    resample_rule = resample_rule.strip().lower()

    if price_side == "bid":
        px = pd.Series([t.bid for t in ticks], index=idx)
        vol = pd.Series([t.bid_vol for t in ticks], index=idx)
    elif price_side == "ask":
        px = pd.Series([t.ask for t in ticks], index=idx)
        vol = pd.Series([t.ask_vol for t in ticks], index=idx)
    elif price_side == "mid":
        px = pd.Series([(t.bid + t.ask) / 2.0 for t in ticks], index=idx)
        vol = pd.Series([(t.bid_vol + t.ask_vol) / 2.0 for t in ticks], index=idx)

    else:
        raise ValueError("price_side must be one of: bid, ask, mid")

    ohlc = px.resample(resample_rule).ohlc()
    v = vol.resample(resample_rule).sum().rename("volume")

    out = pd.concat([ohlc, v], axis=1)
    out = out.dropna(subset=["open", "high", "low", "close"])

    return out


def _probe(
    symbol: str,
    hours: list[datetime],
    cache_dir: Path | None,
    probe_ticks: int,
    price_divisor: float | None,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> None:
    for hour in hours:
        url = _dukascopy_tick_url(symbol, hour)
        cache_path = None
        if cache_dir is not None:
            cache_path = (
                cache_dir
                / symbol
                / f"{hour.year}"
                / f"{hour.month - 1:02d}"
                / f"{hour.day:02d}"
                / f"{hour.hour:02d}h_ticks.bi5"
            )

        comp = _download_bi5(url, cache_path=cache_path, timeout=timeout, retries=retries)

        if comp is None or len(comp) == 0:
            print(f"{symbol}: no data for probe hour {hour.isoformat()}")
            continue

        detected_format = _probe_price_format(comp)
        print(f"{symbol}: detected tick price format = {detected_format}")
        raw20 = _read_n_tick_records(comp, max(1, probe_ticks))

        if len(raw20) < _TICK_RECORD_SIZE:
            print(f"{symbol}: probe failed (not enough decompressed bytes)")
            continue

        if detected_format == "float":
            unpack = struct.Struct(">i f f f f").unpack_from
            print(f"{symbol} @ {hour.isoformat()} (float): first {probe_ticks} ticks")
            for i in range(0, min(len(raw20), _TICK_RECORD_SIZE * probe_ticks), _TICK_RECORD_SIZE):
                ms, ask, bid, ask_vol, bid_vol = unpack(raw20, i)
                ts = hour + timedelta(milliseconds=int(ms))
                print(ts.isoformat(), "bid", bid, "ask", ask, "bid_vol", bid_vol)
        else:
            unpack = struct.Struct(">i i i f f").unpack_from
            print(f"{symbol} @ {hour.isoformat()} (int): first {probe_ticks} ticks")
            divisors = [1, 10, 100, 1000, 10000, 100000]
            rows = []
            for i in range(0, min(len(raw20), _TICK_RECORD_SIZE * probe_ticks), _TICK_RECORD_SIZE):
                ms, ask_i, bid_i, ask_vol, bid_vol = unpack(raw20, i)
                ts = hour + timedelta(milliseconds=int(ms))
                rows.append((ts, bid_i, ask_i, bid_vol))
            ts0, bid0, ask0, vol0 = rows[0]
            print("first tick raw:", ts0.isoformat(), "bid_i", bid0, "ask_i", ask0, "vol", vol0)
            for divisor in divisors:
                print(f"  divisor {divisor:>6}: bid {bid0 / divisor:.6f} ask {ask0 / divisor:.6f}")

            price_div: float = price_divisor or 1.0
            print(f"using --price-divisor {price_div}:")
            for ts, bid_i, ask_i, bid_vol in rows:
                print(
                    ts.isoformat(),
                    "bid",
                    bid_i / price_div,
                    "ask",
                    ask_i / price_div,
                    "bid_vol",
                    bid_vol,
                )
        return None


def _daily_candle_path(cache_dir: Path, symbol: str, day: date, side: str) -> Path:
    """Return path for a daily 1-min candle cache file (Zstandard compressed)."""
    return (
        cache_dir
        / symbol
        / f"{day.year}"
        / f"{day.month - 1:02d}"
        / f"{day.day:02d}_{side}.csv.zst"
    )


def _parse_cache_day(year_name: str, month0_name: str, day_name: str) -> date | None:
    """Parse a cache ``YYYY/MM/DD`` dir triple (MM zero-based) into a ``date``.

    Returns ``None`` if any component is non-numeric or out of range (e.g. a
    stray non-cache directory), so callers can skip it without raising.
    """
    try:
        return date(int(year_name), int(month0_name) + 1, int(day_name))
    except ValueError:
        return None


def _cleanup_stale_day_dirs(
    cache_dir: Path,
    symbol: str,
    *,
    today: date | None = None,
    commit_partial_after_days: int = 7,
) -> None:
    """Remove leftover ``.bi5`` day directories that are redundant or empty.

    A day directory (``{symbol}/YYYY/MM/DD/``) holds the raw hourly ``.bi5``
    tick files for one day. Those files are redundant once that day's two
    daily-candle CSVs (``DD_bid.csv.zst`` and ``DD_ask.csv.zst``, written as
    siblings in the month directory) exist. We remove a day directory when:

      - it is **empty** (the normal post-flush state where every ``.bi5`` was
        already deleted but the ``rmdir`` never ran),
      - it is **non-empty but both candle CSVs for that day already exist**, or
      - **every staged ``.bi5`` is 0 bytes** (a market-closed / no-tick day) and
        the day is older than ``commit_partial_after_days``.

    The second case self-heals a run that wrote the candle CSVs but was
    interrupted before deleting (all of) its ``.bi5``. Without this, the day is
    permanently stuck: ``export_range`` marks it fully-cached and skips
    download/decode, so the leftover ``.bi5`` are never cleaned, and the
    consumer's ``_check_old_format`` guard hard-fails any backtest touching the
    day. Re-running the export now repairs it (matching the documented
    "re-run tradedesk-md-export" remediation). The raw ``.bi5`` are losslessly
    reproducible, so removing them once candles exist is safe.

    The third case covers weekend / market-holiday days where every
    fetched hour returned no ticks: each ``.bi5`` is written as a 0-byte file, so
    the day decodes to nothing and **no** candle CSV is ever produced — yet the
    staging dir lingers and trips ``_check_old_format`` on every backtest
    touching the day. The 0-byte ``.bi5`` carry no recoverable data and are
    losslessly reproducible, and leaving the dir makes ``export_range`` treat the
    day as cached and skip the re-download that would refill it, so removal is
    strictly safe. It is age-gated like a partial commit so a same-day in-flight
    export (early empty hours staged before ticks arrive) is left alone.

    Day directories with leftover **non-empty** ``.bi5`` but no complete candle
    pair are left untouched so a subsequent run can still finish committing the
    day.
    """
    if today is None:
        today = datetime.now(UTC).date()
    sym_dir = cache_dir / symbol
    if not sym_dir.is_dir():
        return
    for year_dir in sym_dir.iterdir():
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                day_files = list(day_dir.iterdir())
                if not day_files:
                    try:
                        day_dir.rmdir()
                    except OSError:
                        pass
                    continue
                # Non-empty: remove if both daily-candle CSVs exist, in which
                # case the leftover .bi5 are redundant and removable.
                bid_csv = month_dir / f"{day_dir.name}_bid.csv.zst"
                ask_csv = month_dir / f"{day_dir.name}_ask.csv.zst"
                if bid_csv.exists() and ask_csv.exists():
                    try:
                        shutil.rmtree(day_dir)
                    except OSError:
                        log.warning(
                            "%s: could not remove redundant bi5 day-dir %s", symbol, day_dir
                        )
                    continue
                # All-empty .bi5 staging: no decodable ticks, so the
                # day will never produce a candle. Remove once aged past the
                # partial-commit window so a same-day export is not disturbed.
                bi5_files = [f for f in day_files if f.suffix == ".bi5"]
                if (
                    bi5_files
                    and len(bi5_files) == len(day_files)
                    and all(f.stat().st_size == 0 for f in bi5_files)
                ):
                    day = _parse_cache_day(year_dir.name, month_dir.name, day_dir.name)
                    if day is None or (today - day).days >= commit_partial_after_days:
                        try:
                            shutil.rmtree(day_dir)
                        except OSError:
                            log.warning(
                                "%s: could not remove empty-bi5 day-dir %s", symbol, day_dir
                            )


# Backwards-compatible alias: this function historically only pruned empty
# directories; it now also self-heals redundant non-empty ones.
_cleanup_empty_day_dirs = _cleanup_stale_day_dirs


def _candles_to_candles(df: pd.DataFrame, resample_rule: str) -> pd.DataFrame:
    """
    Aggregate OHLCV candle DataFrame to a larger timeframe.

    Uses first/max/min/last/sum aggregation — matching the CandleAggregator
    pattern in the tradedesk project.
    """
    if df.empty:
        return df
    rule = resample_rule.strip().lower()
    out = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["open"])


def _write_daily_candles(df: pd.DataFrame, path: Path) -> None:
    """Atomically write a 1-min candle DataFrame as a Zstandard-compressed CSV (level 3)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    out = df.copy()
    out.index.name = "timestamp"
    cctx = zstd.ZstdCompressor(level=3)
    compressed = cctx.compress(out.reset_index().to_csv(index=False).encode("utf-8"))
    tmp.write_bytes(compressed)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Day assembly and commit, shared by every source
# ---------------------------------------------------------------------------
#
# A source turns its raw units (Dukascopy hours, HistData months) into 1-minute
# bid/ask candle frames per UTC day; from there on every source goes through the
# same steps: assemble the day, run the scale sentry and write both day files
# atomically.


def _day_is_committed(cache_dir: Path, symbol: str, day: date) -> bool:
    """True when both of a day's candle files exist.

    A committed day is never rewritten by any source: cache precedence is
    "first committed wins".
    """
    return (
        _daily_candle_path(cache_dir, symbol, day, "bid").exists()
        and _daily_candle_path(cache_dir, symbol, day, "ask").exists()
    )


def _assemble_day(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate a day's 1-min candle frames in time order (an empty day has no rows)."""
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return pd.concat(frames).sort_index()


def _commit_day(
    cache_dir: Path, symbol: str, day: date, bid_df: pd.DataFrame, ask_df: pd.DataFrame
) -> str:
    """Scale-check a day and write its bid and ask candle files.

    Returns ``"committed"``, ``"scale_rejected"`` (the scale sentry refused the
    day; nothing written) or ``"write_failed"``.

    Scale-discontinuity sentry: refuse to write a daily candle CSV whose median
    close diverges from the existing neighbour cache. That class of mismatch is
    silent in backtests and produces order-of-magnitude wrong PnL across the
    boundary.
    """
    if not bid_df.empty:
        new_median = float(bid_df["close"].median())
        ok, reason = check_scale_consistency(cache_dir, symbol, day, new_median)
        if not ok:
            log.error(reason)
            return "scale_rejected"

    for side, df in (("bid", bid_df), ("ask", ask_df)):
        try:
            _write_daily_candles(df, _daily_candle_path(cache_dir, symbol, day, side))
        except Exception as e:
            log.warning(f"{symbol}: failed to write daily {side} candle CSV for {day}: {e}")
            return "write_failed"
    return "committed"


def _write_range_outputs(
    symbol: str,
    bid_frames: list[pd.DataFrame],
    ask_frames: list[pd.DataFrame],
    *,
    out: Path,
    resample_rule: str,
    start_utc: datetime,
    end_utc_inclusive: datetime,
    progress: "Progress | None" = None,
    write_task_id: TaskID | None = None,
) -> tuple[Path | None, Path | None]:
    """Aggregate the run's 1-min candles to ``resample_rule`` and write the range CSVs."""
    if not bid_frames and not ask_frames:
        raise RuntimeError(
            f"No data produced for symbol={symbol} in range {start_utc}..{end_utc_inclusive}"
        )

    out.mkdir(parents=True, exist_ok=True)
    rule_label = resample_rule.replace(" ", "").upper()
    start_ts = pd.Timestamp(start_utc)
    end_ts = pd.Timestamp(end_utc_inclusive + timedelta(days=1) - timedelta(microseconds=1))

    out_csv_bid: Path | None = None
    out_csv_ask: Path | None = None

    for side, frames_list in (
        ("bid", bid_frames),
        ("ask", ask_frames),
    ):
        if not frames_list:
            continue
        all_1min = pd.concat(frames_list).sort_index()
        all_1min = all_1min.loc[start_ts:end_ts]
        # Aggregate 1-min candles to target resample rule (single pass over full range
        # avoids boundary artefacts that occur when aggregating hour-by-hour)
        frames = _candles_to_candles(all_1min, resample_rule)
        # Deduplication is a safety net; should be a no-op after single-pass aggregation
        frames = frames.loc[~frames.index.duplicated(keep="last")]
        if frames.empty:
            continue
        out_csv = out / f"{symbol}_{rule_label}_{side}.csv"
        out_reset = frames.reset_index().rename(columns={"index": "timestamp"})
        out_reset["timestamp"] = out_reset["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
        out_reset.to_csv(out_csv, index=False)
        log.info(f"Wrote: {out_csv} ({len(frames)} candles)")
        if side == "bid":
            out_csv_bid = out_csv
        else:
            out_csv_ask = out_csv
        if progress is not None and write_task_id is not None:
            progress.update(write_task_id, advance=1)

    return (out_csv_bid, out_csv_ask)


def _partial_day_manifest_path(cache_dir: Path, symbol: str) -> Path:
    """Path to a symbol's append-only partial-day manifest."""
    return cache_dir / symbol / "_partial_days.jsonl"


def _append_partial_day_manifest(
    cache_dir: Path,
    symbol: str,
    day: date,
    missing_hours: list[int],
    gap_reason: str,
) -> None:
    """Record a partial-day commit in the per-symbol manifest.

    A *partial day* is one that was committed to daily candle CSVs despite
    holding one or more permanently-absent hours (404 / decode-failure that
    never resolved). The manifest makes "known-permanent gap, not a bug"
    machine-readable for downstream data-quality checks without changing the
    candle-CSV schema. One JSON object per line, append-only; safe because a
    given symbol is exported by a single worker thread.
    """
    manifest = _partial_day_manifest_path(cache_dir, symbol)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "day": day.isoformat(),
        "missing_hours": missing_hours,
        "gap_reason": gap_reason,
        "committed_at": datetime.now(UTC).isoformat(),
    }
    with open(manifest, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _load_daily_candles(path: Path) -> pd.DataFrame | None:
    """Read a Zstandard-compressed 1-min candle CSV. Returns None on missing file or parse error."""
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f_in:
            with dctx.stream_reader(f_in) as reader:
                df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")
    except Exception:
        return None


def export_range(
    *,
    symbol: str,
    start_utc: datetime,
    end_utc_inclusive: datetime,
    out: Path,
    price_divisor: float = 1.0,
    resample_rule: str | None,
    cache_dir: Path | None,
    probe: bool = False,
    probe_ticks: int = 10,
    commit_partial_after_days: int = 7,
    progress: "Progress | None" = None,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> tuple[Path | None, Path | None]:
    """
    Export [start_utc, end_utc_inclusive] into two CSVs: one for bid prices, one for ask.
    Returns (bid_csv, ask_csv); either may be None if no data or resample_rule is None.

    If progress is provided, we create three tasks per symbol:
      - dl: download attempts
      - rs: processing/resampling progress (advances once per hour processed)
      - write: advances once per output file written (max 2)

    Caching strategy (when cache_dir is set):
      - .bi5 tick files are downloaded and cached as before.
      - After all hours of a day decode successfully, two daily 1-min candle
        CSV files are written ({day}_bid.csv.zst, {day}_ask.csv.zst) and the
        .bi5 files for that day are deleted.
      - On subsequent runs, days with both candle CSVs present skip .bi5
        download/decode entirely and load candles directly from the CSVs.
      - A day is only committed to candle CSVs when every hour has a
        definitive result (successfully decoded or legitimate empty-200). Hours
        with 404 or decode failures leave the day uncommitted so the next run
        can retry — UNLESS the day is older than ``commit_partial_after_days``,
        in which case the gap is treated as permanent (Dukascopy historical
        ticks are immutable and published with <1-day lag) and the day is
        *partial-committed*: candle CSVs are written from the hours that did
        decode, the bi5 are deleted, and the day is recorded in the per-symbol
        ``_partial_days.jsonl`` manifest. Days rejected by the scale-sentry are
        never partial-committed (a wrong-scale day must be re-run with the
        correct ``--price-divisor``, not committed).

    commit_partial_after_days:
        Age threshold (in days, UTC) past which a permanent-gap day (404 /
        decode-failure hours) is committed from its available hours instead of
        being left for retry. Default 7. ``0`` commits any permanent-gap day
        immediately (used by the orphan-cache backfill sweep).

    timeout, retries:
        The per-request ``(connect, read)`` timeout in seconds and the number of
        attempts per hour, applied to every datafeed request this export makes
        (probe, download, and the re-download after a decode failure).
    """

    # counters
    hours_total = 0
    hours_missing_404 = 0
    hours_empty_200 = 0
    hours_downloaded = 0
    hours_decode_failed = 0
    hours_resampled_nonempty = 0
    hours_loaded_from_cache = 0
    days_rejected_scale_sentry = 0
    days_committed_partial = 0

    today_utc = datetime.now(UTC).date()

    detected_format: str | None = None
    symbol = _symbol_normalise(symbol)

    # End-exclusive boundary for hour iteration
    end_exclusive = (end_utc_inclusive + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Accumulates 1-minute candle frames (bid and ask) across all hours.
    # At the end these are each aggregated once to the target resample_rule.
    all_1min_bid_frames: list[pd.DataFrame] = []
    all_1min_ask_frames: list[pd.DataFrame] = []

    # Per-day 1-min candle frame lists — used for writing daily candle cache files.
    day_bid_frames: dict[date, list[pd.DataFrame]] = {}
    day_ask_frames: dict[date, list[pd.DataFrame]] = {}

    # Days with >=1 permanent-gap hour (404 / decode failure). These are not
    # written to daily CSVs until they age past commit_partial_after_days, at
    # which point the gap is treated as permanent and the day is partial-committed.
    day_perm_gap: set[date] = set()
    # Days rejected by the scale-sentry (price-scale divergence). These must
    # NEVER be partial-committed — they need a re-run with the correct
    # --price-divisor, not commitment. Scale-rejection dominates a gap.
    day_scale_rejected: set[date] = set()
    # Permanent-gap hours per day (for the partial-day manifest).
    day_missing_hours: dict[date, set[int]] = {}
    # Gap reason(s) per day: "missing_404" and/or "decode_failed".
    day_gap_reasons: dict[date, set[str]] = {}

    # Collect all hours to download
    hours_to_fetch = list(_iter_hours(start_utc, end_exclusive))
    hours_total = len(hours_to_fetch)

    # Probe mode: check only first 24 hours, probe the first that works, and exit immediately
    if probe:
        log.info(f"Running probe for {symbol} starting at {start_utc.isoformat()}")
        _probe(
            symbol,
            hours_to_fetch[0:24],
            cache_dir,
            probe_ticks,
            price_divisor,
            timeout=timeout,
            retries=retries,
        )
        return (None, None)

    # Pre-check: identify days where daily 1-min candle CSVs already exist.
    # Those days skip .bi5 download and decode entirely.
    # Prune leftover bi5 day-dirs (empty, or redundant where candle CSVs exist)
    # so a re-export self-heals dirs interrupted mid-deletion.
    days_fully_cached: set[date] = set()
    unique_days: set[date] = set()
    if cache_dir is not None:
        unique_days = {h.date() for h in hours_to_fetch}
        for day in unique_days:
            bid_path = _daily_candle_path(cache_dir, symbol, day, "bid")
            ask_path = _daily_candle_path(cache_dir, symbol, day, "ask")
            if bid_path.exists() and ask_path.exists():
                days_fully_cached.add(day)
        _cleanup_stale_day_dirs(
            cache_dir,
            symbol,
            today=today_utc,
            commit_partial_after_days=commit_partial_after_days,
        )

    # Early exit: if every day is cached there may be nothing to (re)generate.
    if cache_dir is not None and unique_days and days_fully_cached == unique_days:
        if resample_rule is None:
            # No output is ever written without a resample rule; cache is complete.
            log.info(
                f"{symbol}: all {len(unique_days)} days "
                "cached and no resample requested; nothing to do"
            )
            return (None, None)
        rule_label = resample_rule.replace(" ", "").upper()
        bid_csv = out / f"{symbol}_{rule_label}_bid.csv"
        ask_csv = out / f"{symbol}_{rule_label}_ask.csv"
        if bid_csv.exists() and ask_csv.exists():
            log.info(
                f"{symbol}: all {len(unique_days)} days "
                "cached and output CSVs exist; skipping export"
            )
            return (bid_csv, ask_csv)

    # Create progress tasks if Progress object provided.
    dl_task_id = None
    rs_task_id = None
    write_task_id = None
    cache_task_id = None
    if progress is not None:
        dl_task_id = progress.add_task(
            f"[cyan]{symbol}[/] dl",
            total=hours_total,
            symbol=symbol,
            phase="dl",
        )
        if resample_rule is not None:
            rs_task_id = progress.add_task(
                f"[cyan]{symbol}[/] rs",
                total=hours_total,
                symbol=symbol,
                phase="rs",
            )
            write_task_id = progress.add_task(
                f"[cyan]{symbol}[/] write",
                total=2,
                symbol=symbol,
                phase="write",
            )
        n_days_to_write = len(unique_days - days_fully_cached)
        if cache_dir is not None and n_days_to_write > 0:
            cache_task_id = progress.add_task(
                f"[cyan]{symbol}[/] cache",
                total=n_days_to_write,
                symbol=symbol,
                phase="cache",
            )

    hours_to_download = [h for h in hours_to_fetch if h.date() not in days_fully_cached]

    # last_hour_of_day[d] is the latest hour in hours_to_fetch for date d.
    # Used to detect when a day's processing is complete.
    last_hour_of_day: dict[date, datetime] = {h.date(): h for h in hours_to_fetch}

    # Sentinel value stored in hour_data for hours belonging to fully-cached days.
    _DAY_CACHED = object()

    # Normal mode: parallel download
    log.info(f"Exporting {symbol} from {start_utc.isoformat()} to {end_utc_inclusive.isoformat()}")
    log.info(
        f"{symbol}: fetching {len(hours_to_download)} hours "
        f"({len(days_fully_cached)} days loaded from cache) "
        f"with {DOWNLOAD_THREADS_PER_INSTRUMENT} threads"
    )

    # Pre-populate hour_data for cached days and advance dl progress immediately.
    hour_data: dict[datetime, object] = {}
    for h in hours_to_fetch:
        if h.date() in days_fully_cached:
            hour_data[h] = _DAY_CACHED
    if days_fully_cached and progress is not None and dl_task_id is not None:
        n_cached_hours = sum(1 for h in hours_to_fetch if h.date() in days_fully_cached)
        progress.update(dl_task_id, advance=n_cached_hours)

    # Download hours in parallel
    def download_hour(hour_start: datetime) -> tuple[datetime, bytes | None]:
        """Download a single hour's tick data."""
        try:
            check_cancelled()
            url = _dukascopy_tick_url(symbol, hour_start)
            cache_path = None
            if cache_dir is not None:
                cache_path = (
                    cache_dir
                    / symbol
                    / f"{hour_start.year}"
                    / f"{hour_start.month - 1:02d}"
                    / f"{hour_start.day:02d}"
                    / f"{hour_start.hour:02d}h_ticks.bi5"
                )

            comp = _download_bi5(url, cache_path=cache_path, timeout=timeout, retries=retries)

            if progress is not None and dl_task_id is not None:
                progress.update(dl_task_id, advance=1)

            return (hour_start, comp)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.debug(f"Download failed for {hour_start}: {e}")
            return (hour_start, None)

    next_to_process = 0  # Index in hours_to_fetch

    def _advance_resample_progress() -> None:
        if progress is not None and rs_task_id is not None and resample_rule is not None:
            progress.update(rs_task_id, advance=1)

    def _mark_perm_gap(day: date, hour: int, reason: str) -> None:
        """Record a permanent-gap hour (404 / decode-failure) for a day."""
        day_perm_gap.add(day)
        day_missing_hours.setdefault(day, set()).add(hour)
        day_gap_reasons.setdefault(day, set()).add(reason)

    def _flush_day(day: date) -> None:
        """
        Write daily 1-min candle CSVs (bid + ask, compressed) and delete .bi5 files + day
        directory.

        A clean day (no 404s or decode failures) is always committed. A day with
        a permanent-gap hour is committed from its available hours only once it
        is older than ``commit_partial_after_days`` — a *partial commit* recorded
        in the per-symbol manifest. Younger gap days are left for retry, and
        scale-sentry-rejected days are never committed. Always advances the
        cache progress task.
        """
        nonlocal days_rejected_scale_sentry, days_committed_partial
        bid_frames = day_bid_frames.pop(day, [])
        ask_frames = day_ask_frames.pop(day, [])

        def _advance_cache_progress() -> None:
            if progress is not None and cache_task_id is not None:
                progress.update(cache_task_id, advance=1)

        if cache_dir is None:
            _advance_cache_progress()
            return

        # A permanent-gap day (404 / decode-failure hours) is only committed
        # once it is old enough that the gap is provably permanent. Younger gap
        # days keep the original behaviour: leave the bi5 in place so the next
        # run can retry, and write nothing.
        is_partial = day in day_perm_gap
        if is_partial and (today_utc - day).days < commit_partial_after_days:
            _advance_cache_progress()
            return

        bid_df = _assemble_day(bid_frames)
        ask_df = _assemble_day(ask_frames)

        # A permanent-gap day with no decoded hours at all has nothing to
        # commit; leave it untouched (404 hours wrote no bi5 anyway).
        if is_partial and bid_df.empty and ask_df.empty:
            _advance_cache_progress()
            return

        # A day the scale sentry refuses keeps its bi5 files so a subsequent
        # retry with the correct --price-divisor can write the day cleanly.
        # Scale-rejection dominates a permanent gap: such a day is recorded in
        # day_scale_rejected and never partial-committed.
        outcome = _commit_day(cache_dir, symbol, day, bid_df, ask_df)
        if outcome == "scale_rejected":
            days_rejected_scale_sentry += 1
            day_scale_rejected.add(day)
        if outcome != "committed":
            _advance_cache_progress()
            return

        # Delete .bi5 files for this day and remove the now-empty day directory
        day_dir: Path | None = None
        for h in hours_to_fetch:
            if h.date() != day:
                continue
            bi5_path = (
                cache_dir
                / symbol
                / f"{h.year}"
                / f"{h.month - 1:02d}"
                / f"{h.day:02d}"
                / f"{h.hour:02d}h_ticks.bi5"
            )
            if day_dir is None:
                day_dir = bi5_path.parent
            if bi5_path.exists():
                try:
                    bi5_path.unlink()
                except OSError:
                    log.warning(f"{symbol}: could not delete bi5 cache: {bi5_path}")

        if day_dir is not None:
            try:
                day_dir.rmdir()
            except OSError:
                pass  # Not empty or already gone; that's fine

        # Record the partial commit so downstream data-quality checks can tell a
        # known-permanent gap from a complete day.
        if is_partial:
            days_committed_partial += 1
            missing = sorted(day_missing_hours.get(day, set()))
            reasons = day_gap_reasons.get(day, set())
            gap_reason = "+".join(sorted(reasons)) if reasons else "unknown"
            _append_partial_day_manifest(cache_dir, symbol, day, missing, gap_reason)
            log.info(
                f"{symbol}: partial-committed {day.isoformat()} "
                f"({len(missing)} permanent-gap hour(s): {missing}, reason={gap_reason})"
            )

        _advance_cache_progress()

    def _process_ready_hours() -> None:
        nonlocal next_to_process, hours_missing_404, hours_empty_200, hours_downloaded
        nonlocal hours_decode_failed, hours_resampled_nonempty, detected_format
        nonlocal hours_loaded_from_cache

        while next_to_process < len(hours_to_fetch):
            current_hour = hours_to_fetch[next_to_process]

            if current_hour not in hour_data:
                break  # Wait for this hour to download

            comp = hour_data.pop(current_hour)
            next_to_process += 1

            cache_path = None
            if cache_dir is not None:
                cache_path = (
                    cache_dir
                    / symbol
                    / f"{current_hour.year}"
                    / f"{current_hour.month - 1:02d}"
                    / f"{current_hour.day:02d}"
                    / f"{current_hour.hour:02d}h_ticks.bi5"
                )

            current_day = current_hour.date()
            is_last_hour_of_day = current_hour == last_hour_of_day[current_day]

            # --- Cached day: load daily 1-min candle CSVs on the last hour of the day ---
            if comp is _DAY_CACHED:
                hours_loaded_from_cache += 1
                if is_last_hour_of_day and resample_rule is not None:
                    bid_candles = _load_daily_candles(
                        _daily_candle_path(cache_dir, symbol, current_day, "bid")  # type: ignore[arg-type]
                    )
                    ask_candles = _load_daily_candles(
                        _daily_candle_path(cache_dir, symbol, current_day, "ask")  # type: ignore[arg-type]
                    )
                    if bid_candles is not None and not bid_candles.empty:
                        all_1min_bid_frames.append(bid_candles)
                    if ask_candles is not None and not ask_candles.empty:
                        all_1min_ask_frames.append(ask_candles)
                _advance_resample_progress()
                continue

            # --- 404: no data for this hour ---
            if comp is None:
                hours_missing_404 += 1
                _mark_perm_gap(current_day, current_hour.hour, "missing_404")
                _advance_resample_progress()
                if is_last_hour_of_day:
                    _flush_day(current_day)
                continue

            # --- Empty 200: legitimate market-closed hour ---
            if len(comp) == 0:  # type: ignore[arg-type]
                hours_empty_200 += 1
                _advance_resample_progress()
                if is_last_hour_of_day:
                    _flush_day(current_day)
                continue

            hours_downloaded += 1

            if detected_format is None:
                detected_format = _probe_price_format(comp)  # type: ignore[arg-type]
                log.info(f"{symbol}: detected tick price format = {detected_format}")
                if detected_format == "int" and price_divisor == 1.0:
                    # Warn when int32 format is used with the default divisor.
                    # Dukascopy encodes FX tick prices as integers scaled by a
                    # point-factor (e.g. 10 000 for 4-decimal pairs).  If you
                    # pass --price-divisor 1.0 (the default) the cached candles
                    # will store raw integer values instead of real prices.
                    # Use tradedesk-md-normalize to fix an affected cache.
                    log.warning(
                        "%s: int32 tick format detected with --price-divisor 1.0 (default). "
                        "Decoded prices will be raw integer values, not actual market prices. "
                        "Pass the correct --price-divisor for this instrument "
                        "(e.g. 10000 for 4-decimal FX, 100 for JPY crosses) "
                        "or run 'tradedesk-md-normalize' on an existing cache.",
                        symbol,
                    )

            # --- Decode ticks ---
            try:
                assert detected_format is not None
                ticks = _decode_ticks(
                    current_hour,
                    comp,  # type: ignore[arg-type]
                    price_format=detected_format,
                    price_divisor=price_divisor,
                )
            except lzma.LZMAError:
                if cache_path is not None and cache_path.exists():
                    try:
                        log.warning(f"{symbol}: deleting suspect cache file: {cache_path}")
                        cache_path.unlink()
                    except OSError:
                        log.error(f"{symbol}: rm failed: suspect cache file: {cache_path}")

                comp2 = _download_bi5(
                    _dukascopy_tick_url(symbol, current_hour),
                    cache_path=cache_path,
                    timeout=timeout,
                    retries=retries,
                )
                if comp2 is None:
                    _mark_perm_gap(current_day, current_hour.hour, "decode_failed")
                    _advance_resample_progress()
                    if is_last_hour_of_day:
                        _flush_day(current_day)
                    continue

                try:
                    ticks = _decode_ticks(
                        current_hour,
                        comp2,
                        price_format=detected_format,
                        price_divisor=price_divisor,
                    )
                except Exception as e:
                    log.warning(f"corrupt hour {_dukascopy_tick_url(symbol, current_hour)}: {e}")
                    hours_decode_failed += 1
                    _mark_perm_gap(current_day, current_hour.hour, "decode_failed")
                    _advance_resample_progress()
                    if is_last_hour_of_day:
                        _flush_day(current_day)
                    continue
            except Exception as e:
                log.warning(f"skipping hour {_dukascopy_tick_url(symbol, current_hour)}: {e}")
                hours_decode_failed += 1
                _mark_perm_gap(current_day, current_hour.hour, "decode_failed")
                _advance_resample_progress()
                if is_last_hour_of_day:
                    _flush_day(current_day)
                continue

            # --- Generate 1-minute candles for bid and ask ---
            if resample_rule is not None or cache_dir is not None:
                one_min_bid = _ticks_to_candles(ticks, resample_rule="1min", price_side="bid")
                one_min_ask = _ticks_to_candles(ticks, resample_rule="1min", price_side="ask")
                if resample_rule is not None:
                    if not one_min_bid.empty or not one_min_ask.empty:
                        hours_resampled_nonempty += 1
                    if not one_min_bid.empty:
                        all_1min_bid_frames.append(one_min_bid)
                    if not one_min_ask.empty:
                        all_1min_ask_frames.append(one_min_ask)
                if cache_dir is not None:
                    if not one_min_bid.empty:
                        day_bid_frames.setdefault(current_day, []).append(one_min_bid)
                    if not one_min_ask.empty:
                        day_ask_frames.setdefault(current_day, []).append(one_min_ask)

            _advance_resample_progress()

            if current_hour.hour == 0 and progress is None:
                log.info(f"{symbol}: processed up to {current_hour.isoformat()}")

            if is_last_hour_of_day:
                _flush_day(current_day)

    # Not a `with` block: its exit would call shutdown(wait=True) and drain every
    # queued hour before returning, which is what kept a cancelled export running.
    executor = ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS_PER_INSTRUMENT)
    interrupted = False
    try:
        futures = {executor.submit(download_hour, h): h for h in hours_to_download}

        for future in as_completed(futures):
            check_cancelled()

            hour_start, comp = future.result()
            hour_data[hour_start] = comp

            _process_ready_hours()

    except KeyboardInterrupt:
        interrupted = True
        log.warning(f"{symbol}: download interrupted")
        raise
    finally:
        # On a cancel, drop the queued hours and return at once; the in-flight
        # requests end at their next timeout or cancel check.
        executor.shutdown(wait=not interrupted, cancel_futures=interrupted)

    # Flush any remaining hours (e.g. all days were cached, no futures ran)
    _process_ready_hours()

    log.info(
        f"{symbol}: hours total={hours_total}, missing_404={hours_missing_404}, "
        f"missing_200={hours_empty_200}, downloaded={hours_downloaded}, "
        f"decode_failed={hours_decode_failed}, "
        f"resampled_nonempty={hours_resampled_nonempty}, "
        f"loaded_from_cache={hours_loaded_from_cache}, "
        f"days_rejected_scale_sentry={days_rejected_scale_sentry}, "
        f"days_committed_partial={days_committed_partial}"
    )

    if resample_rule is None:
        return (None, None)

    return _write_range_outputs(
        symbol,
        all_1min_bid_frames,
        all_1min_ask_frames,
        out=out,
        resample_rule=resample_rule,
        start_utc=start_utc,
        end_utc_inclusive=end_utc_inclusive,
        progress=progress,
        write_task_id=write_task_id,
    )
