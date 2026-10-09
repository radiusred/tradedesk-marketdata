"""
Dukascopy's hourly tick datafeed as a source.

Everything specific to Dukascopy lives here; the framework
(:mod:`tradedesk_marketdata.export`) does the rest.

- **Unit:** one UTC hour of one instrument, an LZMA-compressed ``.bi5`` file::

      https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5

  where ``MM0`` is the zero-based month (Jan=00 .. Dec=11). Hours are fetched
  two at a time per symbol (``DOWNLOAD_THREADS_PER_INSTRUMENT``) and staged as
  ``{cache}/{SYMBOL}/{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5`` until their day is
  committed.
- **Answers:** a 404 is an hour with no data (a permanent gap once old enough);
  a 200 with an empty (or tiny) body is a market-closed hour; a timeout, 429 or
  5xx after every retry is *unavailable*: the hour may exist, so its day is
  never committed in this run.
- **Decode:** 20-byte big-endian records ``ms_since_hour, ask, bid, ask_volume,
  bid_volume``. Prices are float32 for some instruments and int32 points for
  others; the format is detected from the first hour with data, and int32
  prices are divided by ``--price-divisor``. Timestamps are UTC.
- **Gaps:** a day with 404 or undecodable hours is committed from its other
  hours once it is ``--commit-partial-after-days`` old (Dukascopy's history is
  immutable and published with less than a day's lag) and recorded in
  ``_partial_days.jsonl``; a younger one is left for a retry.

Examples::

  tradedesk-md-export --source dukascopy --symbols EURUSD \\
    --from 2025-08-01 --to 2025-12-31 --resample 5min --out out
"""

from __future__ import annotations

import io
import logging
import lzma
import math
import shutil
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests  # type: ignore[import-untyped]
from rich.progress import Progress

from .. import export as _export
from ..cancel import cancellation, check_cancelled
from ..export import _parse_cache_day, _symbol_normalise
from ..source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_BACKOFF_FACTOR,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    ActionRequest,
    Commit,
    Decision,
    Leave,
    Partial,
    Provenance,
    RunContext,
    Source,
    SourceDescription,
    SourceError,
    SourceOption,
    SourceRun,
    Tick,
    ticks_frame,
)

SOURCE = "dukascopy"

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
UA = "tradedesk/1.0 bi5-export (https://github.com/radiusred/tradedesk-marketdata)"
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

__all__ = [
    "DOWNLOAD_THREADS_PER_INSTRUMENT",
    "DukascopySource",
    "SOURCE",
    "Tick",
    "Unavailable",
    "export_range",
]


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


def _bi5_relpath(hour: datetime) -> str:
    """An hour's file below the symbol directory: ``{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5``."""
    return f"{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"


def _bi5_path(cache_dir: Path, symbol: str, hour: datetime) -> Path:
    """Where an hour's ``.bi5`` is staged until its day is committed."""
    return cache_dir / symbol / _bi5_relpath(hour)


@dataclass(frozen=True)
class Unavailable:
    """An hour that could not be fetched in this run.

    Returned by ``_download_bi5`` when every attempt ended in a timeout, a
    connection error or a non-404 HTTP status (429, 5xx). It is not a 404: the
    datafeed may well hold the hour, so its day is left uncommitted and the
    next run retries it. Conflating the two was how a rate-limited run came to
    partial-commit days with 21 of 24 hours recorded as permanent gaps.
    """

    error: str


def _download_bi5(
    url: str,
    cache_path: Path | None,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> bytes | None | Unavailable:
    """
    Returns compressed bytes.

    - None means "no file" (HTTP 404).
    - Unavailable means every attempt failed for another reason; retry next run.
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

    log.warning("skipping %s this run after %d failed attempts (%s)", url, retries, last_exc)
    return Unavailable(str(last_exc))


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
            cache_path = _bi5_path(cache_dir, symbol, hour)

        comp = _download_bi5(url, cache_path=cache_path, timeout=timeout, retries=retries)

        if isinstance(comp, Unavailable):
            print(f"{symbol}: could not fetch probe hour {hour.isoformat()}: {comp.error}")
            continue
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


def _cleanup_stale_day_dirs(
    cache_dir: Path,
    symbol: str,
    *,
    today: date | None = None,
    commit_partial_after_days: int = 7,
    ctx: RunContext | None = None,
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
    reproducible, so removing them once candles exist is safe. With
    ``--keep-raw`` (``ctx.keep_raw``) they are retained under ``_raw/``
    instead, as a commit would have done.

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
                    if ctx is not None and ctx.keep_raw:
                        for f in day_files:
                            rel = f"{year_dir.name}/{month_dir.name}/{day_dir.name}/{f.name}"
                            ctx.retire_raw(f, rel)
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


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Hour:
    """One fetch unit: an hour of ticks."""

    start: datetime

    @property
    def days(self) -> tuple[date, ...]:
        return (self.start.date(),)


class _DukascopyRun(SourceRun):
    """One symbol's hours: fetched two at a time, decoded in order, committed per day."""

    def __init__(self, source: DukascopySource, ctx: RunContext) -> None:
        self.ctx = ctx
        self.symbol = ctx.symbol
        self.price_divisor = source.price_divisor
        self.fetch_workers = DOWNLOAD_THREADS_PER_INSTRUMENT
        self.today = datetime.now(UTC).date()
        self.detected_format: str | None = None
        # Planned hours per day: the provenance hour set and the bi5 to retire.
        self.day_hours: dict[date, list[int]] = {}
        # Days with an hour this run could not fetch: never committed, retried next run.
        self.day_unavailable: set[date] = set()
        # Permanent-gap hours (404 / decode failure) and their reasons, per day.
        self.day_missing_hours: dict[date, set[int]] = {}
        self.day_gap_reasons: dict[date, set[str]] = {}
        self.counts = dict.fromkeys(
            ("total", "missing_404", "empty_200", "downloaded", "unavailable", "decode_failed"),
            0,
        )
        # Prune leftover bi5 day-dirs (empty, or redundant where candle CSVs exist)
        # so a re-export self-heals dirs interrupted mid-deletion.
        if ctx.cache_dir is not None:
            _cleanup_stale_day_dirs(
                ctx.cache_dir,
                self.symbol,
                today=self.today,
                commit_partial_after_days=ctx.commit_partial_after_days,
                ctx=ctx,
            )

    def _staged(self, hour: datetime) -> Path | None:
        cache_dir = self.ctx.cache_dir
        return None if cache_dir is None else _bi5_path(cache_dir, self.symbol, hour)

    def plan(self, pending: Sequence[date]) -> list[_Hour]:
        end_exclusive = (self.ctx.end_utc_inclusive + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wanted = set(pending)
        hours = [h for h in _iter_hours(self.ctx.start_utc, end_exclusive) if h.date() in wanted]
        for h in hours:
            self.day_hours.setdefault(h.date(), []).append(h.hour)
        self.counts["total"] = len(hours)
        log.info(
            f"Exporting {self.symbol} from {self.ctx.start_utc.isoformat()} "
            f"to {self.ctx.end_utc_inclusive.isoformat()}"
        )
        log.info(f"{self.symbol}: fetching {len(hours)} hours with {self.fetch_workers} threads")
        return [_Hour(h) for h in hours]

    def fetch(self, unit: _Hour) -> bytes | None | Unavailable:
        hour = unit.start
        try:
            cache_path = self._staged(hour)
            if cache_path is not None and not cache_path.exists():
                kept = self.ctx.kept_raw(_bi5_relpath(hour))
                if kept is not None:
                    return kept.read_bytes()
            return _download_bi5(
                _dukascopy_tick_url(self.symbol, hour),
                cache_path=cache_path,
                timeout=self.ctx.timeout,
                retries=self.ctx.retries,
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.debug(f"Download failed for {hour}: {e}")
            return Unavailable(str(e))

    def _mark_perm_gap(self, day: date, hour: int, reason: str) -> None:
        """Record a permanent-gap hour (404 / decode-failure) for a day."""
        self.day_missing_hours.setdefault(day, set()).add(hour)
        self.day_gap_reasons.setdefault(day, set()).add(reason)

    def decode(self, unit: _Hour, raw: bytes | None | Unavailable) -> pd.DataFrame | None:
        hour = unit.start
        day = hour.date()

        # --- Could not fetch this run (timeouts, 429, 5xx): not a gap ---
        if isinstance(raw, Unavailable):
            self.counts["unavailable"] += 1
            self.day_unavailable.add(day)
            return None

        # --- 404: no data for this hour ---
        if raw is None:
            self.counts["missing_404"] += 1
            self._mark_perm_gap(day, hour.hour, "missing_404")
            return None

        # --- Empty 200: legitimate market-closed hour ---
        if len(raw) == 0:
            self.counts["empty_200"] += 1
            return None

        self.counts["downloaded"] += 1

        if self.detected_format is None:
            self.detected_format = _probe_price_format(raw)
            log.info(f"{self.symbol}: detected tick price format = {self.detected_format}")
            if self.detected_format == "int" and self.price_divisor == 1.0:
                # Warn when int32 format is used with the default divisor.
                # Dukascopy encodes FX tick prices as integers scaled by a
                # point-factor (e.g. 10 000 for 4-decimal pairs).  If you
                # pass --price-divisor 1.0 (the default) the cached candles
                # will store raw integer values instead of real prices.
                log.warning(
                    "%s: int32 tick format detected with --price-divisor 1.0 (default). "
                    "Decoded prices will be raw integer values, not actual market prices. "
                    "Pass the correct --price-divisor for this instrument "
                    "(e.g. 10000 for 4-decimal FX, 100 for JPY crosses) "
                    "or run 'tradedesk-md-normalize' on an existing cache.",
                    self.symbol,
                )
        fmt = self.detected_format

        url = _dukascopy_tick_url(self.symbol, hour)
        try:
            ticks = _decode_ticks(hour, raw, price_format=fmt, price_divisor=self.price_divisor)
        except lzma.LZMAError:
            # A corrupt file (truncated download, bad staging write): fetch it once more.
            for suspect in (self._staged(hour), self.ctx.kept_raw(_bi5_relpath(hour))):
                if suspect is not None and suspect.exists():
                    try:
                        log.warning(f"{self.symbol}: deleting suspect cache file: {suspect}")
                        suspect.unlink()
                    except OSError:
                        log.error(f"{self.symbol}: rm failed: suspect cache file: {suspect}")

            raw2 = _download_bi5(
                url,
                cache_path=self._staged(hour),
                timeout=self.ctx.timeout,
                retries=self.ctx.retries,
            )
            if isinstance(raw2, Unavailable):
                self.counts["unavailable"] += 1
                self.day_unavailable.add(day)
                return None
            if raw2 is None:
                self._mark_perm_gap(day, hour.hour, "decode_failed")
                return None
            try:
                ticks = _decode_ticks(
                    hour, raw2, price_format=fmt, price_divisor=self.price_divisor
                )
            except Exception as e:
                log.warning(f"corrupt hour {url}: {e}")
                self.counts["decode_failed"] += 1
                self._mark_perm_gap(day, hour.hour, "decode_failed")
                return None
        except Exception as e:
            log.warning(f"skipping hour {url}: {e}")
            self.counts["decode_failed"] += 1
            self._mark_perm_gap(day, hour.hour, "decode_failed")
            return None

        if hour.hour == 0:
            log.debug(f"{self.symbol}: processed up to {hour.isoformat()}")
        return ticks_frame(ticks) if ticks else None

    def decide(self, day: date, *, has_data: bool) -> Decision:
        # A day with an hour this run could not fetch is never committed, whole
        # or partial, whatever its age: the hour may exist. Its downloaded bi5
        # stay in place for the next run.
        if day in self.day_unavailable:
            return Leave("an hour could not be fetched this run")
        # A permanent-gap day (404 / decode-failure hours) is only committed
        # once it is old enough that the gap is provably permanent. Younger gap
        # days are left with their bi5 in place so the next run can retry.
        if day in self.day_missing_hours:
            if (self.today - day).days < self.ctx.commit_partial_after_days:
                return Leave("permanent-gap hour(s), too recent to be final")
            # Nothing decoded at all: nothing to commit (404 hours wrote no bi5 anyway).
            if not has_data:
                return Leave("no hour decoded")
            missing = sorted(self.day_missing_hours[day])
            reasons = self.day_gap_reasons.get(day, set())
            gap_reason = "+".join(sorted(reasons)) if reasons else "unknown"
            return Commit(
                Partial(
                    missing_hours=missing,
                    gap_reason=gap_reason,
                    note=f"{len(missing)} permanent-gap hour(s): {missing}, reason={gap_reason}",
                )
            )
        return Commit()

    def committed(self, day: date, *, empty: bool) -> Provenance:
        assert self.ctx.cache_dir is not None
        hours = self.day_hours.get(day, [])
        # Retire the day's bi5 files and remove the now-empty day directory.
        day_dir: Path | None = None
        for hh in hours:
            hour = datetime(day.year, day.month, day.day, hh, tzinfo=UTC)
            bi5 = _bi5_path(self.ctx.cache_dir, self.symbol, hour)
            day_dir = bi5.parent
            self.ctx.retire_raw(bi5, _bi5_relpath(hour))
        if day_dir is not None:
            try:
                day_dir.rmdir()
            except OSError:
                pass  # Not empty or already gone; that's fine

        # Provenance: the day came from this run's hours of the datafeed. The
        # divisor only applies to int32-encoded ticks; float ticks are stored as-is.
        hours_label = f"{min(hours):02d}h-{max(hours):02d}h" if hours else "no hours"
        return Provenance(
            scale_factor=1.0 / (self.price_divisor or 1.0)
            if self.detected_format == "int"
            else 1.0,
            source_unit=(
                f"{self.symbol}/{day.year}/{day.month - 1:02d}/{day.day:02d}/"
                f"{hours_label}_ticks.bi5"
            ),
        )

    def finish(self) -> None:
        c = self.counts
        log.info(
            f"{self.symbol}: hours total={c['total']}, missing_404={c['missing_404']}, "
            f"missing_200={c['empty_200']}, downloaded={c['downloaded']}, "
            f"unavailable={c['unavailable']}, decode_failed={c['decode_failed']}, "
            f"days_left_for_retry={len(self.day_unavailable)}"
        )
        if self.day_unavailable:
            log.warning(
                f"{self.symbol}: {c['unavailable']} hour(s) could not be fetched this run; "
                f"{len(self.day_unavailable)} day(s) left uncommitted. Re-run to retry them."
            )


class DukascopySource(Source):
    """Dukascopy's hourly ``.bi5`` tick datafeed."""

    name = SOURCE
    summary = (
        "Dukascopy's hourly tick datafeed (one request per hour; int32 prices are "
        "divided by --price-divisor)"
    )
    unit = "hour"
    partial_commit_rule = (
        "a day with hours the datafeed answers 404 for, or that do not decode, is committed "
        "from its other hours once it is this many days old (0: at once) and recorded in "
        "_partial_days.jsonl; a younger one is left for a retry"
    )
    options = (
        SourceOption(
            "--price-divisor",
            type=float,
            metavar="N",
            help="divisor applied to int32-encoded tick prices on decode and recorded in the "
            "metadata (default: 1); float32-encoded prices are stored as they are",
        ),
        SourceOption(
            "--probe",
            flag_only=True,
            help="fetch the first hour of the range that has data and print its decoded "
            "ticks under each candidate divisor; no files written (one symbol only)",
        ),
        SourceOption(
            "--probe-ticks",
            type=int,
            metavar="N",
            help="number of ticks to print when probing (default: 10)",
        ),
    )

    @property
    def price_divisor(self) -> float:
        return float(self.option("price_divisor", 1.0))

    def describe(self, symbol: str) -> SourceDescription:
        return SourceDescription(price_divisor=self.price_divisor)

    def open(self, ctx: RunContext) -> _DukascopyRun:
        return _DukascopyRun(self, ctx)

    def action(self, request: ActionRequest) -> int | None:
        """``--probe``: print the first decodable hour's ticks instead of exporting."""
        if not self.option("probe", False):
            return None
        if len(request.symbols) != 1:
            raise SourceError("--probe mode only supports a single symbol")
        symbol = _symbol_normalise(request.symbols[0])
        end_exclusive = (request.end_utc_inclusive + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        hours = list(_iter_hours(request.start_utc, end_exclusive))
        log.info(f"Running probe for {symbol} starting at {request.start_utc.isoformat()}")
        _probe(
            symbol,
            hours[0:24],
            request.cache_dir,
            int(self.option("probe_ticks", 10)),
            self.price_divisor,
            timeout=request.timeout,
            retries=request.retries,
        )
        return 0


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
    progress: Progress | None = None,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
    keep_raw: bool = False,
) -> tuple[Path | None, Path | None]:
    """Export one symbol from Dukascopy: :func:`tradedesk_marketdata.export.export_range`
    with a :class:`DukascopySource`, or the probe when ``probe`` is set.
    """
    source = DukascopySource({"price_divisor": price_divisor, "probe_ticks": probe_ticks})
    if probe:
        source.option_values["probe"] = True
        source.action(
            ActionRequest(
                symbols=[symbol],
                start_utc=start_utc,
                end_utc_inclusive=end_utc_inclusive,
                cache_dir=cache_dir,
                timeout=timeout,
                retries=retries,
            )
        )
        return (None, None)
    return _export.export_range(
        source=source,
        symbol=symbol,
        start_utc=start_utc,
        end_utc_inclusive=end_utc_inclusive,
        out=out,
        resample_rule=resample_rule,
        cache_dir=cache_dir,
        commit_partial_after_days=commit_partial_after_days,
        progress=progress,
        timeout=timeout,
        retries=retries,
        keep_raw=keep_raw,
    )
