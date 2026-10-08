"""
HistData.com as a second data source: monthly tick zips into the shared candle cache.

HistData serves one "Generic ASCII" tick zip per instrument-month, with bid and
ask on every tick, so an instrument's whole history is a few hundred requests
rather than Dukascopy's one request per hour. This module is the HistData front
end of the export pipeline:

- unit listing: the EST months that cover the requested UTC days
  (``_months_for_day``); months whose days are all committed are skipped
- fetch: scrape the month page for its one-off download token, then POST the
  download form (``_fetch_month_zip``); settled months are kept under
  ``{cache}/{SYMBOL}/_histdata/{YYYY}{MM}.zip`` until every day they cover is
  committed
- decode: ``decode_ticks`` turns the zip's CSV into ticks in UTC, scaled into
  the cache's raw units

From there the day assembly, the scale sentry, the atomic day-file writes and
the range CSVs are the ones the Dukascopy path uses (``export._commit_day`` and
friends), so a HistData day file has exactly the form of a Dukascopy one.

Tick file format (``DAT_ASCII_{PAIR}_T_{YYYYMM}.csv``), one tick per line::

    20150101 180000497,2055.000000,2055.250000,0

``YYYYMMDD HHMMSSNNN`` in EST with no daylight-saving adjustment (a fixed
UTC-5), bid, ask, and a volume that is always 0.

HistData is a free service: requests are sequential per instrument, one month
zip per request, with a pause between months and a descriptive User-Agent.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd
import requests  # type: ignore[import-untyped]
from rich.progress import Progress, TaskID

from . import __version__
from . import export as _ex
from .cancel import cancellation, check_cancelled

SOURCE = "histdata"

BASE_URL = "https://www.histdata.com"
# The month page carries the download form and its one-off ``tk`` token.
PAGE_URL = BASE_URL + "/download-free-forex-historical-data/?/ascii/tick-data-quotes/{pair}/{y}/{m}"
DOWNLOAD_URL = BASE_URL + "/get.php"
UA = (
    f"tradedesk-marketdata/{__version__} histdata-export "
    "(+https://github.com/radiusred/tradedesk-marketdata)"
)
# Seconds to wait between two month downloads for one instrument.
REQUEST_INTERVAL = 2.0

# HistData timestamps are EST without daylight saving: always UTC-5.
EST_OFFSET = timedelta(hours=5)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA})

log = logging.getLogger(__name__)

YearMonth = tuple[int, int]


# ---------------------------------------------------------------------------
# Symbol table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistDataInstrument:
    """How one cache symbol maps onto HistData."""

    name: str  # HistData's instrument code, e.g. "SPXUSD"
    scale: float  # cache raw price = HistData decimal price x scale
    first_month: YearMonth  # earliest (year, month) HistData serves ticks for


# The cache stores Dukascopy's raw units, and downstream ``raw_scale`` settings
# depend on them, so HistData's decimal prices are scaled on decode:
_PIP4 = 1e4  # FX quoted to a 0.0001 pip: 1.10366 -> 11036.6
_PIP2 = 1e2  # JPY-quoted FX and gold: 179.601 -> 17960.1, $2063.625 -> 206362.5
_POINTS = 1.0  # indices (points) and Brent (dollars) as quoted: 4774.361 -> 4774.361

# Cache symbol -> HistData instrument. Names and first months checked against
# histdata.com's tick-data instrument list on 2026-10-08; scales checked against
# the median close of cached Dukascopy days (EURUSD ~11217, USDJPY ~12029,
# XAUUSD ~151948, USA500IDXUSD ~4782 and so on). EURSEK's scale follows the
# 4-decimal FX convention; no cached day was available to confirm it.
# Not on HistData, so not mapped: EURSGD, GBPEUR, BTCUSD.
HISTDATA_SYMBOLS: dict[str, HistDataInstrument] = {
    # Indices and commodities: HistData uses its own codes.
    "USA500IDXUSD": HistDataInstrument("SPXUSD", _POINTS, (2010, 11)),
    "DEUIDXEUR": HistDataInstrument("GRXEUR", _POINTS, (2010, 11)),
    "GBRIDXGBP": HistDataInstrument("UKXGBP", _POINTS, (2010, 11)),
    "JPNIDXJPY": HistDataInstrument("JPXJPY", _POINTS, (2010, 11)),
    "AUSIDXAUD": HistDataInstrument("AUXAUD", _POINTS, (2010, 11)),
    "BRENTCMDUSD": HistDataInstrument("BCOUSD", _POINTS, (2010, 11)),
    "XAUUSD": HistDataInstrument("XAUUSD", _PIP2, (2009, 3)),
    # FX: same names on both sides.
    "AUDCAD": HistDataInstrument("AUDCAD", _PIP4, (2007, 7)),
    "AUDJPY": HistDataInstrument("AUDJPY", _PIP2, (2002, 8)),
    "AUDNZD": HistDataInstrument("AUDNZD", _PIP4, (2007, 9)),
    "AUDUSD": HistDataInstrument("AUDUSD", _PIP4, (2000, 6)),
    "CHFJPY": HistDataInstrument("CHFJPY", _PIP2, (2002, 8)),
    "EURCAD": HistDataInstrument("EURCAD", _PIP4, (2007, 3)),
    "EURCHF": HistDataInstrument("EURCHF", _PIP4, (2002, 3)),
    "EURGBP": HistDataInstrument("EURGBP", _PIP4, (2002, 3)),
    "EURSEK": HistDataInstrument("EURSEK", _PIP4, (2008, 8)),
    "EURUSD": HistDataInstrument("EURUSD", _PIP4, (2000, 5)),
    "GBPAUD": HistDataInstrument("GBPAUD", _PIP4, (2007, 9)),
    "GBPCHF": HistDataInstrument("GBPCHF", _PIP4, (2002, 8)),
    "GBPJPY": HistDataInstrument("GBPJPY", _PIP2, (2002, 5)),
    "GBPUSD": HistDataInstrument("GBPUSD", _PIP4, (2000, 5)),
    "NZDCAD": HistDataInstrument("NZDCAD", _PIP4, (2008, 3)),
    "USDCAD": HistDataInstrument("USDCAD", _PIP4, (2000, 6)),
    "USDCHF": HistDataInstrument("USDCHF", _PIP4, (2000, 5)),
    "USDJPY": HistDataInstrument("USDJPY", _PIP2, (2000, 5)),
}


class UnmappedSymbolError(ValueError):
    """The symbol has no entry in the HistData symbol table."""


def lookup(symbol: str) -> HistDataInstrument:
    """Return the HistData mapping for a cache symbol, or raise ``UnmappedSymbolError``."""
    key = _ex._symbol_normalise(symbol)
    try:
        return HISTDATA_SYMBOLS[key]
    except KeyError:
        raise UnmappedSymbolError(
            f"{key} has no HistData mapping; mapped symbols: {', '.join(sorted(HISTDATA_SYMBOLS))}"
        ) from None


# ---------------------------------------------------------------------------
# Months and days
# ---------------------------------------------------------------------------
#
# A HistData month file covers one EST month, i.e. UTC [YYYY-MM-01 05:00,
# next month 01 05:00). A UTC day therefore needs one file, except the 1st of a
# month, whose first five hours are in the previous month's file.


def _next_month(ym: YearMonth) -> YearMonth:
    y, m = ym
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _month_of(ts_utc: datetime) -> YearMonth:
    est = ts_utc - EST_OFFSET
    return (est.year, est.month)


def _month_span(ym: YearMonth) -> tuple[datetime, datetime]:
    """The UTC interval [start, end) a month file covers."""
    y, m = ym
    ny, nm = _next_month(ym)
    return (
        datetime(y, m, 1, tzinfo=UTC) + EST_OFFSET,
        datetime(ny, nm, 1, tzinfo=UTC) + EST_OFFSET,
    )


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _months_for_day(day: date) -> list[YearMonth]:
    start, end = _day_bounds(day)
    first, last = _month_of(start), _month_of(end - timedelta(microseconds=1))
    return [first] if first == last else [first, last]


def _days_for_month(ym: YearMonth) -> list[date]:
    start, end = _month_span(ym)
    d, last = start.date(), (end - timedelta(microseconds=1)).date()
    days = []
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    return days


def _zip_path(cache_dir: Path, symbol: str, ym: YearMonth) -> Path:
    return cache_dir / symbol / "_histdata" / f"{ym[0]}{ym[1]:02d}.zip"


def _zip_name(inst: HistDataInstrument, ym: YearMonth) -> str:
    """The file name HistData serves a month's tick zip under."""
    return f"HISTDATA_COM_ASCII_{inst.name}_T{ym[0]}{ym[1]:02d}.zip"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class MonthFetchError(RuntimeError):
    """A month could not be downloaded within the configured retries."""


class _DownloadFormParser(HTMLParser):
    """Collect the hidden inputs of the page's ``file_down`` form."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] | None = None
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._in_form = a.get("id") == "file_down"
            if self._in_form:
                self.fields = {}
        elif tag == "input" and self._in_form and self.fields is not None:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def _scrape_download_form(html: str) -> dict[str, str] | None:
    """Return the download form's fields (``tk``, ``date``, ``datemonth``, ...), or None."""
    parser = _DownloadFormParser()
    parser.feed(html)
    return parser.fields


def _page_url(inst: HistDataInstrument, ym: YearMonth) -> str:
    return PAGE_URL.format(pair=inst.name.lower(), y=ym[0], m=ym[1])


def _fetch_month_zip(
    inst: HistDataInstrument,
    ym: YearMonth,
    *,
    timeout: tuple[float, float] = (_ex.DEFAULT_CONNECT_TIMEOUT, _ex.DEFAULT_READ_TIMEOUT),
    retries: int = _ex.DEFAULT_RETRIES,
) -> bytes | None:
    """Download one month's tick zip.

    Each attempt loads the month page for a fresh download token, then posts
    the page's download form with the page as Referer. Returns the zip bytes,
    or None when HistData has no file for the month (the page's token is
    empty). Raises ``MonthFetchError`` once ``retries`` attempts have failed,
    with the same backoff as the Dukascopy downloader, and ``KeyboardInterrupt``
    as soon as a cancel is pending: before each request, between download
    chunks and from inside a backoff sleep.
    """
    page = _page_url(inst, ym)
    last_exc: Exception | None = None
    delay = _ex.RETRY_BASE_DELAY

    for attempt in range(1, retries + 1):
        check_cancelled()
        try:
            with _SESSION.get(page, timeout=timeout) as r:
                r.raise_for_status()
                form = _scrape_download_form(r.text)
            if form is None:
                raise ValueError("no download form on the month page")
            if not form.get("tk"):
                return None  # HistData has no file for this month

            check_cancelled()
            buf = io.BytesIO()
            with _SESSION.post(
                DOWNLOAD_URL, data=form, headers={"Referer": page}, timeout=timeout, stream=True
            ) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1 << 16):
                    check_cancelled()
                    buf.write(chunk)
            data = buf.getvalue()
            if not zipfile.is_zipfile(io.BytesIO(data)):
                raise ValueError(f"download is not a zip ({len(data)} bytes)")
            return data

        except Exception as e:
            last_exc = e
            log.debug("histdata attempt %d/%d failed for %s: %s", attempt, retries, page, e)
            # Backoff before retry (but not after final attempt); a cancel ends the sleep early
            if attempt < retries:
                if cancellation.wait(delay):
                    raise KeyboardInterrupt() from None
                delay = min(delay * _ex.RETRY_BACKOFF_FACTOR, _ex.RETRY_MAX_DELAY)

    raise MonthFetchError(f"{page}: {retries} failed attempt(s) ({last_exc})")


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

_TIMESTAMP_PATTERN = r"\d{8} \d{9}"


def decode_ticks(csv_bytes: bytes, *, scale: float) -> pd.DataFrame:
    """Decode a HistData Generic ASCII tick CSV.

    Returns ticks indexed by UTC time (sorted, original order kept within a
    timestamp) with columns ``bid``, ``ask``, ``bid_vol`` and ``ask_vol``.
    Prices are scaled into the cache's raw units (``x scale``) and rounded to
    6 decimals so the binary float error of the multiplication does not reach
    the cache files (1.10366 x 1e4 is 11036.6, not 11036.599999999999). HistData
    volumes are always 0 and stay 0.

    Lines that do not parse (a header, a short or long line, a timestamp not in
    ``YYYYMMDD HHMMSSNNN`` form, a non-numeric or non-positive price) are
    skipped and counted in a warning. Raises ``ValueError`` when no line parses.
    """
    raw = pd.read_csv(
        io.BytesIO(csv_bytes),
        header=None,
        names=["ts", "bid", "ask", "volume"],
        dtype=str,
        on_bad_lines="skip",
        skip_blank_lines=True,
    )
    ts_text = raw["ts"].str.strip()
    ts_ok = ts_text.str.fullmatch(_TIMESTAMP_PATTERN).fillna(False).astype(bool)
    est = pd.to_datetime(ts_text.where(ts_ok), format="%Y%m%d %H%M%S%f", errors="coerce")
    bid = pd.to_numeric(raw["bid"], errors="coerce")
    ask = pd.to_numeric(raw["ask"], errors="coerce")
    ok = est.notna() & np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > 0)

    if not ok.any():
        raise ValueError("no parseable tick lines")
    # read_csv drops lines with too many fields itself, so count against the input.
    n_lines = sum(1 for line in csv_bytes.splitlines() if line.strip())
    skipped = n_lines - int(ok.sum())
    if skipped:
        log.warning("histdata: skipped %d malformed tick line(s) of %d", skipped, n_lines)

    index = pd.DatetimeIndex((est[ok] + EST_OFFSET).dt.tz_localize("UTC"))
    index.name = None
    ticks = pd.DataFrame(
        {
            "bid": np.round(bid[ok].to_numpy(dtype=float) * scale, 6),
            "ask": np.round(ask[ok].to_numpy(dtype=float) * scale, 6),
            "bid_vol": 0.0,
            "ask_vol": 0.0,
        },
        index=index,
    )
    return ticks.sort_index(kind="stable")


def _read_month_ticks(zip_bytes: bytes, *, scale: float) -> pd.DataFrame:
    """Decode the single tick CSV inside a month zip (raises on a corrupt zip)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in the zip, found {names}")
        return decode_ticks(zf.read(names[0]), scale=scale)


def _tick_candles(ticks: pd.DataFrame, side: str) -> pd.DataFrame:
    """1-minute OHLCV candles for one price side.

    The same resample as ``export._ticks_to_candles``, on a tick frame rather
    than a list of ``Tick`` (a month can hold millions of ticks).
    """
    px = ticks[side]
    vol = ticks[f"{side}_vol"]
    ohlc = px.resample("1min").ohlc()
    v = vol.resample("1min").sum().rename("volume")
    out = pd.concat([ohlc, v], axis=1)
    return out.dropna(subset=["open", "high", "low", "close"])


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_range_histdata(
    *,
    symbol: str,
    start_utc: datetime,
    end_utc_inclusive: datetime,
    out: Path,
    resample_rule: str | None,
    cache_dir: Path | None,
    commit_partial_after_days: int = 7,
    progress: Progress | None = None,
    timeout: tuple[float, float] = (_ex.DEFAULT_CONNECT_TIMEOUT, _ex.DEFAULT_READ_TIMEOUT),
    retries: int = _ex.DEFAULT_RETRIES,
) -> tuple[Path | None, Path | None]:
    """Export the UTC days of [start_utc, end_utc_inclusive] from HistData.

    The HistData counterpart of ``export.export_range``: the same cache files,
    the same range CSVs and the same return value. Days are written under the
    cache's symbol (``USA500IDXUSD``, not ``SPXUSD``).

    - Months are fetched in order, one request at a time. A month whose days
      are all committed (by any source) is skipped without a request; a month
      before the instrument's first HistData month is skipped with a log line.
    - A *settled* month (one that ended at least ``commit_partial_after_days``
      days ago) is kept as a zip under ``{cache}/{SYMBOL}/_histdata/`` until
      every day it covers is committed, so a re-run does not download it again.
      A cached zip that is corrupt, or that was downloaded before its month had
      settled, is fetched again.
    - A day is committed once the data provably covers it: later ticks exist,
      or every month file it needs has settled. A day with no ticks is
      committed as an empty day (a weekend or holiday) only when there are
      ticks before it too; otherwise it is left for a later run.
    - A day that needs a month HistData has no file for is left for retry until
      that month has settled, then committed from the ticks it has and recorded
      in ``_partial_days.jsonl`` (gap_reason ``histdata_month_unavailable``),
      like a Dukascopy partial day. A day that needs a month that failed to
      download is never committed in this run; the month is reported in the
      end-of-run summary.
    - A day that already has both candle files is never rewritten: first
      committed wins. Every day committed here is recorded in
      ``_sources.jsonl`` with source ``histdata``, the symbol's scale factor
      and the month zip(s) it came from.
    """
    symbol = _ex._symbol_normalise(symbol)
    inst = lookup(symbol)  # an unmapped symbol is refused before any request
    now = datetime.now(UTC)
    settle_after = timedelta(days=commit_partial_after_days)

    def settled_at(ym: YearMonth) -> datetime:
        return _month_span(ym)[1] + settle_after

    def is_settled(ym: YearMonth) -> bool:
        return ym < inst.first_month or now >= settled_at(ym)

    # --- unit listing ---
    days: list[date] = []
    d = start_utc.date()
    while d <= end_utc_inclusive.date():
        days.append(d)
        d += timedelta(days=1)

    already = {
        d for d in days if cache_dir is not None and _ex._day_is_committed(cache_dir, symbol, d)
    }
    pending = [d for d in days if d not in already]
    months = sorted({m for d in pending for m in _months_for_day(d)})

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

    log.info(
        f"Exporting {symbol} from HistData {inst.name}: {len(pending)} of {len(days)} days to "
        f"build from {len(months)} month file(s)"
    )

    dl_task: TaskID | None = None
    cache_task: TaskID | None = None
    write_task: TaskID | None = None
    if progress is not None:
        dl_task = progress.add_task(
            f"[cyan]{symbol}[/] dl", total=len(months), symbol=symbol, phase="dl"
        )
        if cache_dir is not None and pending:
            cache_task = progress.add_task(
                f"[cyan]{symbol}[/] cache", total=len(pending), symbol=symbol, phase="cache"
            )
        if resample_rule is not None:
            write_task = progress.add_task(
                f"[cyan]{symbol}[/] write", total=2, symbol=symbol, phase="write"
            )

    # --- per-run state ---
    status: dict[YearMonth, str] = {}  # ok | before_first | unavailable | failed
    pending_set = set(pending)
    day_bid: dict[date, list[pd.DataFrame]] = {}
    day_ask: dict[date, list[pd.DataFrame]] = {}
    days_with_ticks: set[date] = set()
    first_tick: datetime | None = None
    last_tick: datetime | None = None
    all_bid: list[pd.DataFrame] = []
    all_ask: list[pd.DataFrame] = []
    undecided = list(pending)

    counts = dict.fromkeys(
        (
            "from_cache",
            "downloaded",
            "unavailable",
            "before_first",
            "failed",
            "committed",
            "committed_empty",
            "committed_partial",
            "rejected_scale_sentry",
            "left_uncommitted",
        ),
        0,
    )
    failed_months: list[YearMonth] = []
    requested = False

    def advance(task: TaskID | None) -> None:
        if progress is not None and task is not None:
            progress.update(task, advance=1)

    def commit(day: date, *, missing_months: list[YearMonth]) -> None:
        """Commit one decided day through the shared commit path."""
        bid_df = _ex._assemble_day(day_bid.pop(day, []))
        ask_df = _ex._assemble_day(day_ask.pop(day, []))
        if cache_dir is None:
            return
        outcome = _ex._commit_day(cache_dir, symbol, day, bid_df, ask_df)
        advance(cache_task)
        if outcome == "scale_rejected":
            counts["rejected_scale_sentry"] += 1
            return
        if outcome != "committed":
            counts["left_uncommitted"] += 1
            return
        counts["committed"] += 1
        if bid_df.empty and ask_df.empty:
            counts["committed_empty"] += 1
        units = [_zip_name(inst, m) for m in _months_for_day(day) if status[m] == "ok"]
        _ex._append_source_manifest(
            cache_dir,
            symbol,
            day,
            source=SOURCE,
            scale_factor=inst.scale,
            source_unit="+".join(units) if units else "none",
        )
        if missing_months:
            counts["committed_partial"] += 1
            day_start, _ = _day_bounds(day)
            missing_hours = [
                h
                for h in range(24)
                for m in missing_months
                if _month_span(m)[0] <= day_start + timedelta(hours=h) < _month_span(m)[1]
            ]
            _ex._append_partial_day_manifest(
                cache_dir, symbol, day, missing_hours, "histdata_month_unavailable"
            )
            log.info(
                f"{symbol}: partial-committed {day.isoformat()} "
                f"(HistData has no file for {', '.join(f'{y}-{m:02d}' for y, m in missing_months)})"
            )

    def leave(day: date, why: str) -> None:
        day_bid.pop(day, None)
        day_ask.pop(day, None)
        counts["left_uncommitted"] += 1
        advance(cache_task)
        log.debug(f"{symbol}: {day.isoformat()} left uncommitted ({why})")

    def decide_days() -> None:
        """Commit or give up on every day whose month files have all been processed."""
        still: list[date] = []
        for day in undecided:
            needed = _months_for_day(day)
            if any(m not in status for m in needed):
                still.append(day)
                continue
            states = [status[m] for m in needed]
            if "failed" in states:
                leave(day, "a month it needs failed to download")
                continue
            missing = [m for m in needed if status[m] == "unavailable"]
            if missing:
                if all(is_settled(m) for m in missing) and day in days_with_ticks:
                    commit(day, missing_months=missing)
                else:
                    leave(day, "HistData has no file yet for a month it needs")
                continue
            day_start, day_end = _day_bounds(day)
            covered = (last_tick is not None and last_tick >= day_end) or all(
                is_settled(m) for m in needed
            )
            if not covered:
                still.append(day)  # a later month may show the data runs past this day
                continue
            if day not in days_with_ticks and (first_tick is None or first_tick >= day_start):
                leave(day, "no HistData ticks before or on this day")
                continue
            commit(day, missing_months=[])
        undecided[:] = still

    def load_month(ym: YearMonth) -> pd.DataFrame | None:
        """Return a month's ticks, from the cached zip or HistData; None when there are none."""
        nonlocal requested
        zpath = _zip_path(cache_dir, symbol, ym) if cache_dir is not None else None
        if zpath is not None and zpath.exists():
            mtime = datetime.fromtimestamp(zpath.stat().st_mtime, UTC)
            if is_settled(ym) and mtime >= settled_at(ym):
                try:
                    ticks = _read_month_ticks(zpath.read_bytes(), scale=inst.scale)
                    counts["from_cache"] += 1
                    return ticks
                except Exception as e:
                    log.warning(f"{symbol}: cached {zpath} is corrupt ({e}); fetching it again")
            else:
                log.info(f"{symbol}: cached {zpath} predates its month settling; fetching it again")
            zpath.unlink(missing_ok=True)

        if requested and cancellation.wait(REQUEST_INTERVAL):
            raise KeyboardInterrupt()
        requested = True
        try:
            data = _fetch_month_zip(inst, ym, timeout=timeout, retries=retries)
        except MonthFetchError as e:
            log.warning(f"{symbol}: skipping HistData month {ym[0]}-{ym[1]:02d}: {e}")
            status[ym] = "failed"
            return None
        if data is None:
            log.info(f"{symbol}: HistData has no {inst.name} file for {ym[0]}-{ym[1]:02d}")
            status[ym] = "unavailable"
            return None
        try:
            ticks = _read_month_ticks(data, scale=inst.scale)
        except Exception as e:
            log.warning(f"{symbol}: HistData month {ym[0]}-{ym[1]:02d} does not decode: {e}")
            status[ym] = "failed"
            return None
        counts["downloaded"] += 1
        # Only a settled month's file is final; a running month's is re-fetched next run.
        if zpath is not None and is_settled(ym):
            zpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = zpath.with_suffix(zpath.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(zpath)
        return ticks

    def ingest(ym: YearMonth, ticks: pd.DataFrame) -> None:
        """Turn a month's ticks into 1-minute candles for the pending days it covers."""
        nonlocal first_tick, last_tick
        # Each file owns exactly its EST month, so a stray tick outside it can
        # never double up with the neighbouring file's ticks for the same day.
        span_start, span_end = _month_span(ym)
        lo, hi = ticks.index.searchsorted([span_start, span_end])
        if hi - lo < len(ticks):
            log.warning(
                f"{symbol}: ignoring {len(ticks) - (hi - lo)} tick(s) outside "
                f"{ym[0]}-{ym[1]:02d} in HistData's file for that month"
            )
            ticks = ticks.iloc[lo:hi]
        if ticks.empty:
            return
        t0, t1 = ticks.index[0].to_pydatetime(), ticks.index[-1].to_pydatetime()
        first_tick = t0 if first_tick is None else min(first_tick, t0)
        last_tick = t1 if last_tick is None else max(last_tick, t1)
        for day in _days_for_month(ym):
            if day not in pending_set:
                continue
            day_start, day_end = _day_bounds(day)
            lo, hi = ticks.index.searchsorted([day_start, day_end])
            if hi <= lo:
                continue
            part = ticks.iloc[lo:hi]
            days_with_ticks.add(day)
            bid_c = _tick_candles(part, "bid")
            ask_c = _tick_candles(part, "ask")
            day_bid.setdefault(day, []).append(bid_c)
            day_ask.setdefault(day, []).append(ask_c)
            if resample_rule is not None:
                all_bid.append(bid_c)
                all_ask.append(ask_c)

    # --- months, strictly one after another ---
    for ym in months:
        check_cancelled()
        if ym < inst.first_month:
            log.info(
                f"{symbol}: {ym[0]}-{ym[1]:02d} precedes HistData's first {inst.name} month "
                f"({inst.first_month[0]}-{inst.first_month[1]:02d}); skipping"
            )
            status[ym] = "before_first"
            counts["before_first"] += 1
        else:
            ticks = load_month(ym)
            if ticks is not None:
                status[ym] = "ok"
                ingest(ym, ticks)
                del ticks
            elif status[ym] == "failed":
                counts["failed"] += 1
                failed_months.append(ym)
            else:
                counts["unavailable"] += 1
        advance(dl_task)
        decide_days()

    for day in undecided:
        leave(day, "the HistData data does not reach past this day yet")
    undecided.clear()

    # Drop month zips whose days are all committed; keep the rest for the next run.
    if cache_dir is not None:
        for ym, st in status.items():
            zpath = _zip_path(cache_dir, symbol, ym)
            if st == "ok" and zpath.exists():
                if all(_ex._day_is_committed(cache_dir, symbol, d) for d in _days_for_month(ym)):
                    zpath.unlink(missing_ok=True)
        try:
            (cache_dir / symbol / "_histdata").rmdir()
        except OSError:
            pass  # not empty or not there

    log.info(
        f"{symbol}: histdata months total={len(months)}, from_cache={counts['from_cache']}, "
        f"downloaded={counts['downloaded']}, unavailable={counts['unavailable']}, "
        f"before_first={counts['before_first']}, failed={counts['failed']}; "
        f"days total={len(days)}, already_cached={len(already)}, "
        f"committed={counts['committed']} (empty={counts['committed_empty']}, "
        f"partial={counts['committed_partial']}), "
        f"rejected_scale_sentry={counts['rejected_scale_sentry']}, "
        f"left_uncommitted={counts['left_uncommitted']}"
    )
    if failed_months:
        log.warning(
            f"{symbol}: {len(failed_months)} HistData month(s) failed after {retries} "
            f"attempt(s) and were skipped this run: "
            f"{', '.join(f'{y}-{m:02d}' for y, m in failed_months)}; re-run to retry"
        )

    if resample_rule is None:
        return (None, None)

    if cache_dir is not None:
        for day in sorted(already):
            for side, frames in (("bid", all_bid), ("ask", all_ask)):
                df = _ex._load_daily_candles(_ex._daily_candle_path(cache_dir, symbol, day, side))
                if df is not None and not df.empty:
                    frames.append(df)

    return _ex._write_range_outputs(
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


__all__ = [
    "HISTDATA_SYMBOLS",
    "HistDataInstrument",
    "MonthFetchError",
    "SOURCE",
    "UnmappedSymbolError",
    "decode_ticks",
    "export_range_histdata",
    "lookup",
]
