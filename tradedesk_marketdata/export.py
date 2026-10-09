"""
The export framework: from a source's ticks to the shared day-file cache and range CSVs.

Everything here is common to every source (see :mod:`tradedesk_marketdata.source`
for the contract and :mod:`tradedesk_marketdata.sources` for the providers):

- the cache layout, one 1-minute candle file per UTC day and price side::

    {cache}/{SYMBOL}/{YYYY}/{MM0}/{DD}_{bid,ask}.csv.zst   (MM0: Jan=00 .. Dec=11)

- tick to 1-minute candles, split per UTC day, and the day assembly
- the write-time scale sentry and the atomic commit of a day's two files
  ("first committed wins": a committed day is never rewritten by any source)
- the per-symbol ``_sources.jsonl`` (provenance) and ``_partial_days.jsonl``
  (known-permanent gaps) manifests
- the fetch pool, unit ordering, progress and cancellation
- the range CSVs, one per price side, resampled once over the whole range

Output format of the range CSVs::

    timestamp,open,high,low,close,volume

with UTC timestamps rendered as ``YYYY-MM-DD HH:MM:SS+00:00``.
"""

import io
import json
import logging
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import zstandard as zstd
from rich.progress import Progress, TaskID

from .cancel import check_cancelled
from .scale_sentry import check_scale_consistency
from .source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_BACKOFF_FACTOR,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    Commit,
    Decision,
    Leave,
    RunContext,
    Source,
    Tick,
    Wait,
    ticks_frame,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_READ_TIMEOUT",
    "DEFAULT_RETRIES",
    "RETRY_BACKOFF_FACTOR",
    "RETRY_BASE_DELAY",
    "RETRY_MAX_DELAY",
    "Tick",
    "export_range",
]

log = logging.getLogger(__name__)


def _symbol_normalise(s: str) -> str:
    """
    Accept inputs like:
      - EURUSD
      - USDJPY
      - USA500.IDX/USD
      - GBR.IDX/GBP
      - usa500idxusd
    Convert to the cache's symbol naming: uppercase, alphanumerics only.
    """
    raw = s.strip()
    if not raw:
        raise ValueError("Empty symbol")

    # Remove separators
    cleaned = "".join(ch for ch in raw if ch.isalnum())
    return cleaned.upper()


def _ticks_to_candles(
    ticks: pd.DataFrame | Sequence[Tick],
    *,
    resample_rule: str,
    price_side: str = "bid",
) -> pd.DataFrame:
    """
    Resample ticks to OHLCV using a pandas resample rule (e.g. '1min', '5min', '15min', '1H').

    ``ticks`` is a tick frame (see :mod:`tradedesk_marketdata.source`) or a
    sequence of :class:`Tick`. Volume uses bid_vol (for bid) or ask_vol (for
    ask); if mid, uses (bid_vol+ask_vol)/2.
    """
    if len(ticks) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = ticks if isinstance(ticks, pd.DataFrame) else ticks_frame(ticks)
    resample_rule = resample_rule.strip().lower()

    if price_side == "bid":
        px = frame["bid"]
        vol = frame["bid_vol"]
    elif price_side == "ask":
        px = frame["ask"]
        vol = frame["ask_vol"]
    elif price_side == "mid":
        px = (frame["bid"] + frame["ask"]) / 2.0
        vol = (frame["bid_vol"] + frame["ask_vol"]) / 2.0
    else:
        raise ValueError("price_side must be one of: bid, ask, mid")

    ohlc = px.resample(resample_rule).ohlc()
    v = vol.resample(resample_rule).sum().rename("volume")

    out = pd.concat([ohlc, v], axis=1)
    out = out.dropna(subset=["open", "high", "low", "close"])

    return out


def _split_days(ticks: pd.DataFrame) -> Iterator[tuple[date, pd.DataFrame]]:
    """A tick frame's ticks per UTC day, in day order, each day's ticks in their original order."""
    idx = pd.DatetimeIndex(ticks.index)
    first, last = idx.min(), idx.max()
    if first.normalize() == last.normalize():
        yield first.date(), ticks
        return
    if idx.is_monotonic_increasing:
        day = first.normalize()
        while day <= last:
            nxt = day + pd.Timedelta(days=1)
            lo, hi = idx.searchsorted([day, nxt])
            if hi > lo:
                yield day.date(), ticks.iloc[lo:hi]
            day = nxt
        return
    days = idx.normalize()
    for d in days.unique().sort_values():
        yield d.date(), ticks[days == d]


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
# A source turns its raw units into ticks; the framework turns those into
# 1-minute bid/ask candle frames per UTC day and, for each day the source lets
# it commit, assembles the day, runs the scale sentry, writes both day files
# atomically, and records which source produced the day.


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


def _source_manifest_path(cache_dir: Path, symbol: str) -> Path:
    """Path to a symbol's append-only per-day provenance manifest."""
    return cache_dir / symbol / "_sources.jsonl"


def _append_source_manifest(
    cache_dir: Path,
    symbol: str,
    day: date,
    *,
    source: str,
    scale_factor: float,
    source_unit: str,
) -> None:
    """Record which source produced a committed day.

    ``scale_factor`` is the multiplier from the source's native price to the
    cache's raw units (cache price = native price x scale_factor);
    ``source_unit`` names the raw unit(s) the day was built from. One JSON
    object per line, append-only, written once per commit.
    """
    manifest = _source_manifest_path(cache_dir, symbol)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "day": day.isoformat(),
        "source": source,
        "scale_factor": scale_factor,
        "committed_at": datetime.now(UTC).isoformat(),
        "source_unit": source_unit,
    }
    with open(manifest, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


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
    holding one or more permanently-absent hours (the source's gap rule says
    they will not appear). The manifest makes "known-permanent gap, not a bug"
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


# ---------------------------------------------------------------------------
# The export driver
# ---------------------------------------------------------------------------


def export_range(
    *,
    source: Source,
    symbol: str,
    start_utc: datetime,
    end_utc_inclusive: datetime,
    out: Path,
    resample_rule: str | None,
    cache_dir: Path | None,
    commit_partial_after_days: int = 7,
    progress: "Progress | None" = None,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> tuple[Path | None, Path | None]:
    """Export the UTC days of [start_utc, end_utc_inclusive] of one symbol from ``source``.

    Returns the (bid, ask) range CSVs; both are None when ``resample_rule`` is
    None, and either may be None when its side produced no data.

    With a ``cache_dir``:

    - a day that already has both candle files is never fetched or rewritten,
      whichever source wrote it (first committed wins); when every day is
      committed and the range CSVs exist, nothing is done at all
    - every other day is built from the source's units and committed once the
      source decides it may be (its gap rules); a day the scale sentry refuses
      is not written
    - every committed day is recorded in ``_sources.jsonl`` with the source's
      name, scale factor and unit(s), and a partial day in
      ``_partial_days.jsonl``

    With progress, four tasks per symbol: ``dl`` (units fetched), ``rs``
    (units processed) and ``write`` (range CSVs) when resampling, and
    ``cache`` (days decided) when caching.

    timeout, retries:
        The per-request ``(connect, read)`` timeout in seconds and the number of
        attempts per fetch unit, applied to every request the source makes.
    """
    symbol = _symbol_normalise(symbol)
    source.check_symbol(symbol)  # a symbol the source cannot serve is refused first
    ctx = RunContext(
        source_name=source.name,
        symbol=symbol,
        start_utc=start_utc,
        end_utc_inclusive=end_utc_inclusive,
        cache_dir=cache_dir,
        commit_partial_after_days=commit_partial_after_days,
        timeout=timeout,
        retries=retries,
    )

    days: list[date] = []
    d = start_utc.date()
    while d <= end_utc_inclusive.date():
        days.append(d)
        d += timedelta(days=1)
    already = {d for d in days if cache_dir is not None and _day_is_committed(cache_dir, symbol, d)}
    pending = [d for d in days if d not in already]

    run = source.open(ctx)

    # Early exit: every day is cached, and there is nothing (more) to write.
    if cache_dir is not None and days and not pending:
        if resample_rule is None:
            log.info(
                f"{symbol}: all {len(days)} days cached and no resample requested; nothing to do"
            )
            return (None, None)
        rule_label = resample_rule.replace(" ", "").upper()
        bid_csv = out / f"{symbol}_{rule_label}_bid.csv"
        ask_csv = out / f"{symbol}_{rule_label}_ask.csv"
        if bid_csv.exists() and ask_csv.exists():
            log.info(
                f"{symbol}: all {len(days)} days cached and output CSVs exist; skipping export"
            )
            return (bid_csv, ask_csv)

    units = list(run.plan(pending))

    dl_task: TaskID | None = None
    rs_task: TaskID | None = None
    write_task: TaskID | None = None
    cache_task: TaskID | None = None
    if progress is not None:
        dl_task = progress.add_task(
            f"[cyan]{symbol}[/] dl", total=len(units), symbol=symbol, phase="dl"
        )
        if resample_rule is not None:
            rs_task = progress.add_task(
                f"[cyan]{symbol}[/] rs", total=len(units), symbol=symbol, phase="rs"
            )
            write_task = progress.add_task(
                f"[cyan]{symbol}[/] write", total=2, symbol=symbol, phase="write"
            )
        if cache_dir is not None and pending:
            cache_task = progress.add_task(
                f"[cyan]{symbol}[/] cache", total=len(pending), symbol=symbol, phase="cache"
            )

    def advance(task: TaskID | None) -> None:
        if progress is not None and task is not None:
            progress.update(task, advance=1)

    # --- per-run state ---
    pending_set = set(pending)
    all_bid: list[pd.DataFrame] = []
    all_ask: list[pd.DataFrame] = []
    day_bid: dict[date, list[pd.DataFrame]] = {}
    day_ask: dict[date, list[pd.DataFrame]] = {}
    days_with_data: set[date] = set()
    # Units still to be processed per pending day; a day is decided at zero.
    remaining: dict[date, int] = dict.fromkeys(pending, 0)
    for unit in units:
        for ud in unit.days:
            if ud in remaining:
                remaining[ud] += 1
    waiting: dict[date, Wait] = {}
    counts = dict.fromkeys(
        (
            "committed",
            "committed_empty",
            "committed_partial",
            "rejected_scale_sentry",
            "left_uncommitted",
        ),
        0,
    )

    def settle(day: date, decision: Decision) -> None:
        """Act on a final decision for one day: leave it, or commit it and record it."""
        assert cache_dir is not None
        bid_frames = day_bid.pop(day, [])
        ask_frames = day_ask.pop(day, [])
        if isinstance(decision, Leave):
            counts["left_uncommitted"] += 1
            advance(cache_task)
            log.debug(f"{symbol}: {day.isoformat()} left uncommitted ({decision.reason})")
            return
        assert isinstance(decision, Commit)
        bid_df = _assemble_day(bid_frames)
        ask_df = _assemble_day(ask_frames)
        outcome = _commit_day(cache_dir, symbol, day, bid_df, ask_df)
        advance(cache_task)
        if outcome == "scale_rejected":
            counts["rejected_scale_sentry"] += 1
            return
        if outcome != "committed":
            counts["left_uncommitted"] += 1
            return
        empty = bid_df.empty and ask_df.empty
        counts["committed"] += 1
        if empty:
            counts["committed_empty"] += 1
        prov = run.committed(day, empty=empty)
        _append_source_manifest(
            cache_dir,
            symbol,
            day,
            source=source.name,
            scale_factor=prov.scale_factor,
            source_unit=prov.source_unit,
        )
        if decision.partial is not None:
            counts["committed_partial"] += 1
            _append_partial_day_manifest(
                cache_dir,
                symbol,
                day,
                decision.partial.missing_hours,
                decision.partial.gap_reason,
            )
            log.info(f"{symbol}: partial-committed {day.isoformat()} ({decision.partial.note})")

    def decide(ready: list[date]) -> None:
        """Ask the source about every ready day, oldest first, and settle the final answers."""
        if cache_dir is None:
            return
        for day in sorted(set(waiting) | set(ready)):
            waiting.pop(day, None)
            decision = run.decide(day, has_data=day in days_with_data)
            if isinstance(decision, Wait):
                waiting[day] = decision
            else:
                settle(day, decision)

    def process(unit: Any, raw: Any) -> None:
        """Decode one unit, file its candles under their UTC days, and decide ready days."""
        ticks = run.decode(unit, raw)
        wanted = resample_rule is not None or cache_dir is not None
        if ticks is not None and len(ticks) and wanted:
            for day, part in _split_days(ticks):
                if day not in pending_set:
                    continue
                bid_c = _ticks_to_candles(part, resample_rule="1min", price_side="bid")
                ask_c = _ticks_to_candles(part, resample_rule="1min", price_side="ask")
                if resample_rule is not None:
                    if not bid_c.empty:
                        all_bid.append(bid_c)
                    if not ask_c.empty:
                        all_ask.append(ask_c)
                if cache_dir is not None:
                    if not bid_c.empty:
                        day_bid.setdefault(day, []).append(bid_c)
                    if not ask_c.empty:
                        day_ask.setdefault(day, []).append(ask_c)
                    if not (bid_c.empty and ask_c.empty):
                        days_with_data.add(day)
        advance(rs_task)
        ready = []
        for ud in unit.days:
            if ud in remaining:
                remaining[ud] -= 1
                if remaining[ud] == 0:
                    ready.append(ud)
        decide(ready)

    def fetch(unit: Any) -> Any:
        check_cancelled()
        raw = run.fetch(unit)
        advance(dl_task)
        return raw

    # Days no unit covers have nothing to decide on.
    if cache_dir is not None:
        for day in pending:
            if remaining[day] == 0:
                settle(day, Leave("no unit covers it"))

    workers = max(1, int(run.fetch_workers))
    if workers == 1:
        for unit in units:
            process(unit, fetch(unit))
    else:
        # Not a `with` block: its exit would call shutdown(wait=True) and drain every
        # queued unit before returning, which is what kept a cancelled export running.
        executor = ThreadPoolExecutor(max_workers=workers)
        interrupted = False
        try:
            futures = {executor.submit(fetch, unit): i for i, unit in enumerate(units)}
            fetched: dict[int, Any] = {}
            next_to_process = 0
            for future in as_completed(futures):
                check_cancelled()
                fetched[futures[future]] = future.result()
                # Process in plan order: a unit waits for the ones before it.
                while next_to_process in fetched:
                    process(units[next_to_process], fetched.pop(next_to_process))
                    next_to_process += 1
        except KeyboardInterrupt:
            interrupted = True
            log.warning(f"{symbol}: download interrupted")
            raise
        finally:
            # On a cancel, drop the queued units and return at once; the in-flight
            # requests end at their next timeout or cancel check.
            executor.shutdown(wait=not interrupted, cancel_futures=interrupted)

    for day, wait in sorted(waiting.items()):
        settle(day, Leave(wait.reason))
    waiting.clear()

    run.finish()
    if cache_dir is not None:
        log.info(
            f"{symbol}: days total={len(days)}, already_cached={len(already)}, "
            f"committed={counts['committed']} (empty={counts['committed_empty']}, "
            f"partial={counts['committed_partial']}), "
            f"rejected_scale_sentry={counts['rejected_scale_sentry']}, "
            f"left_uncommitted={counts['left_uncommitted']}"
        )

    if resample_rule is None:
        return (None, None)

    if cache_dir is not None:
        for day in sorted(already):
            for side, frames in (("bid", all_bid), ("ask", all_ask)):
                df = _load_daily_candles(_daily_candle_path(cache_dir, symbol, day, side))
                if df is not None and not df.empty:
                    frames.append(df)

    return _write_range_outputs(
        symbol,
        all_bid,
        all_ask,
        out=out,
        resample_rule=resample_rule,
        start_utc=start_utc,
        end_utc_inclusive=end_utc_inclusive,
        progress=progress,
        write_task_id=write_task,
    )
