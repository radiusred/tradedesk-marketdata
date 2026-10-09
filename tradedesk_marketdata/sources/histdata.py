"""
HistData.com's monthly tick files as a source.

Everything specific to HistData lives here; the framework
(:mod:`tradedesk_marketdata.export`) does the rest.

HistData serves one "Generic ASCII" tick zip per instrument-month, with bid and
ask on every tick, so an instrument's whole history is a few hundred requests
rather than one request per hour.

- **Unit:** one month file. Its month is an EST month, so it covers UTC
  [YYYY-MM-01 05:00, next month 01 05:00); a UTC day needs one file, except the
  1st of a month, whose first five hours are in the previous month's file
  (``_months_for_day``). Months whose days are all committed are not planned.
- **Fetch:** scrape the month page for its one-off download token, then POST
  the download form (``_fetch_month_zip``), strictly one month at a time with a
  pause between requests (``REQUEST_INTERVAL``): HistData is a free service.
  Settled months are staged under ``{cache}/{SYMBOL}/_histdata/{YYYY}{MM}.zip``
  until every day they cover is committed.
- **Decode:** ``decode_ticks`` turns the zip's CSV into ticks in UTC, scaled
  into the cache's raw units by the symbol map (``--symbol-map``, TOML; the
  package ships ``histdata.example.toml``).
- **Days:** a day is committed once the data provably covers it (later ticks
  exist, or every month file it needs has settled), as an empty day only when
  there are ticks before it; a day that needs a month HistData has no file for
  is committed from the ticks it has once that month has settled, as a partial
  day.

Tick file format (``DAT_ASCII_{PAIR}_T_{YYYYMM}.csv``), one tick per line::

    20150101 180000497,2055.000000,2055.250000,0

``YYYYMMDD HHMMSSNNN`` in EST with no daylight-saving adjustment (a fixed
UTC-5), bid, ask, and a volume that is always 0.
"""

from __future__ import annotations

import io
import logging
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests  # type: ignore[import-untyped]
from rich.progress import Progress

from .. import __version__
from .. import export as _export
from ..cancel import cancellation, check_cancelled
from ..export import _day_is_committed, _symbol_normalise
from ..source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_BACKOFF_FACTOR,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    Commit,
    Decision,
    Exclude,
    Leave,
    Partial,
    Provenance,
    RunContext,
    Source,
    SourceDescription,
    SourceError,
    SourceOption,
    SourceRun,
    Wait,
)

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
# TODO(#82): HistData's "EST" is not a fixed UTC-5. The per-era rule replaces
# this constant inside _utc_to_est, _est_to_utc and _est_series_to_utc, its
# only readers; every timestamp conversion in this module goes through them.
EST_OFFSET = timedelta(hours=5)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA})

log = logging.getLogger(__name__)

YearMonth = tuple[int, int]


# ---------------------------------------------------------------------------
# Symbol map
# ---------------------------------------------------------------------------
#
# Which cache symbols HistData serves, under which code, at which scale and
# from which month is configuration: a TOML symbol map. The package ships
# ``histdata.example.toml`` (documented there), used when no --symbol-map is
# given.

SYMBOL_MAP_EXAMPLE = "histdata.example.toml"


@dataclass(frozen=True)
class ExcludedSpan:
    """Days [first, last] whose HistData data must not be committed, and why."""

    first: date
    last: date
    reason: str


@dataclass(frozen=True)
class HistDataInstrument:
    """How one cache symbol maps onto HistData."""

    name: str  # HistData's instrument code, e.g. "SPXUSD"
    scale: float  # cache raw price = HistData decimal price x scale
    first_month: YearMonth  # earliest (year, month) HistData serves ticks for
    scale_verified: bool = False  # the scale was confirmed against another source
    scale_evidence: str = ""
    exclude: tuple[ExcludedSpan, ...] = ()

    def excluded(self, day: date) -> ExcludedSpan | None:
        """The exclusion span ``day`` falls in, if any."""
        return next((s for s in self.exclude if s.first <= day <= s.last), None)


class SymbolMapError(SourceError):
    """A symbol map that cannot be used: unreadable, not TOML, or not the schema."""


_ENTRY_KEYS = {"name", "scale", "first_month", "scale_verified", "scale_evidence", "exclude"}
_SPAN_KEYS = {"from", "to", "reason"}


def _parse_entry(where: str, symbol: str, entry: object) -> HistDataInstrument:
    def bad(msg: str) -> SymbolMapError:
        return SymbolMapError(f"{where}: symbols.{symbol}: {msg}")

    if not isinstance(entry, dict):
        raise bad("must be a table")
    unknown = set(entry) - _ENTRY_KEYS
    if unknown:
        raise bad(f"unknown key(s) {', '.join(sorted(unknown))}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise bad("name must be HistData's instrument code")
    scale = entry.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, int | float) or not scale > 0:
        raise bad("scale must be a number above zero")
    month = entry.get("first_month")
    try:
        assert isinstance(month, str) and len(month) == 7 and month[4] == "-"
        first_month = (int(month[:4]), int(month[5:]))
        assert 1 <= first_month[1] <= 12
    except (AssertionError, ValueError):
        raise bad('first_month must be "YYYY-MM"') from None
    verified = entry.get("scale_verified", False)
    if not isinstance(verified, bool):
        raise bad("scale_verified must be true or false")
    evidence = entry.get("scale_evidence", "")
    if not isinstance(evidence, str):
        raise bad("scale_evidence must be text")
    spans = entry.get("exclude", [])
    if not isinstance(spans, list):
        raise bad("exclude must be a list of {from, to, reason} tables")
    exclude = []
    for span in spans:
        if not isinstance(span, dict) or set(span) != _SPAN_KEYS:
            raise bad("each exclude entry needs exactly from, to and reason")
        first, last, reason = span["from"], span["to"], span["reason"]
        if not isinstance(first, date) or isinstance(first, datetime):
            raise bad("exclude from must be a date (YYYY-MM-DD)")
        if not isinstance(last, date) or isinstance(last, datetime):
            raise bad("exclude to must be a date (YYYY-MM-DD)")
        if last < first:
            raise bad(f"exclude span {first}..{last} ends before it starts")
        if not isinstance(reason, str) or not reason:
            raise bad("exclude reason must be text")
        exclude.append(ExcludedSpan(first, last, reason))
    return HistDataInstrument(
        name=name,
        scale=float(scale),
        first_month=first_month,
        scale_verified=verified,
        scale_evidence=evidence,
        exclude=tuple(exclude),
    )


def load_symbol_map(path: Path | None = None) -> dict[str, HistDataInstrument]:
    """Read a TOML symbol map (the shipped example when ``path`` is None).

    Raises ``SymbolMapError`` for a file that cannot be read or does not follow
    the schema documented in the example.
    """
    if path is None:
        where = SYMBOL_MAP_EXAMPLE
        raw = (
            resources.files("tradedesk_marketdata.sources")
            .joinpath(SYMBOL_MAP_EXAMPLE)
            .read_bytes()
        )
    else:
        where = str(path)
        try:
            raw = Path(path).read_bytes()
        except OSError as e:
            raise SymbolMapError(f"{where}: cannot read the symbol map ({e})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SymbolMapError(f"{where}: not a TOML file ({e})") from None
    if set(data) - {"symbols"}:
        raise SymbolMapError(
            f"{where}: unknown top-level key(s) {', '.join(sorted(set(data) - {'symbols'}))}"
        )
    table = data.get("symbols")
    if not isinstance(table, dict) or not table:
        raise SymbolMapError(f"{where}: no [symbols.<SYMBOL>] entries")
    out: dict[str, HistDataInstrument] = {}
    for symbol, entry in table.items():
        key = _symbol_normalise(symbol)
        if key != symbol:
            raise SymbolMapError(f"{where}: symbols.{symbol}: write the cache symbol as {key}")
        out[key] = _parse_entry(where, symbol, entry)
    return out


# The shipped symbol map, as loaded.
HISTDATA_SYMBOLS: dict[str, HistDataInstrument] = load_symbol_map()


class UnmappedSymbolError(SourceError):
    """The symbol has no entry in the HistData symbol map."""


def lookup(symbol: str, symbols: dict[str, HistDataInstrument] | None = None) -> HistDataInstrument:
    """Return a cache symbol's HistData mapping (from ``symbols``, default the shipped map).

    Raises ``UnmappedSymbolError`` for a symbol the map does not hold.
    """
    table = HISTDATA_SYMBOLS if symbols is None else symbols
    key = _symbol_normalise(symbol)
    try:
        return table[key]
    except KeyError:
        raise UnmappedSymbolError(
            f"{key} has no HistData mapping; mapped symbols: {', '.join(sorted(table))}"
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


def _utc_to_est(ts_utc: datetime) -> datetime:
    """A UTC time in HistData's timestamp convention."""
    return ts_utc - EST_OFFSET


def _est_to_utc(est: datetime) -> datetime:
    """A HistData timestamp (naive, its own convention) as a UTC time."""
    return est.replace(tzinfo=UTC) + EST_OFFSET


def _est_series_to_utc(est: pd.Series) -> pd.DatetimeIndex:
    """HistData timestamps (naive) as a UTC index: the vectorised ``_est_to_utc``."""
    return pd.DatetimeIndex((est + EST_OFFSET).dt.tz_localize("UTC"))


def _month_of(ts_utc: datetime) -> YearMonth:
    est = _utc_to_est(ts_utc)
    return (est.year, est.month)


def _month_span(ym: YearMonth) -> tuple[datetime, datetime]:
    """The UTC interval [start, end) a month file covers."""
    y, m = ym
    ny, nm = _next_month(ym)
    return (_est_to_utc(datetime(y, m, 1)), _est_to_utc(datetime(ny, nm, 1)))


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


def _zip_relpath(ym: YearMonth) -> str:
    """A month zip's file name in the staging and ``--keep-raw`` directories."""
    return f"{ym[0]}{ym[1]:02d}.zip"


def _zip_path(cache_dir: Path, symbol: str, ym: YearMonth) -> Path:
    """Where a settled month's zip is staged until every day it covers is committed."""
    return cache_dir / symbol / "_histdata" / _zip_relpath(ym)


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
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> bytes | None:
    """Download one month's tick zip.

    Each attempt loads the month page for a fresh download token, then posts
    the page's download form with the page as Referer. Returns the zip bytes,
    or None when HistData has no file for the month (the page's token is
    empty). Raises ``MonthFetchError`` once ``retries`` attempts have failed,
    with the framework's backoff between attempts, and ``KeyboardInterrupt``
    as soon as a cancel is pending: before each request, between download
    chunks and from inside a backoff sleep.
    """
    page = _page_url(inst, ym)
    last_exc: Exception | None = None
    delay = RETRY_BASE_DELAY

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
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

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

    index = _est_series_to_utc(est[ok])
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


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Month:
    """One fetch unit: a month file, and the pending days it holds ticks for."""

    ym: YearMonth
    days: tuple[date, ...]


@dataclass(frozen=True)
class _Zip:
    """A month zip's bytes, and the file they were read from when cached."""

    data: bytes
    cached: Path | None = None


@dataclass(frozen=True)
class _Failed:
    """A month that could not be downloaded within the retries."""

    error: str


_BEFORE_FIRST = "before_first"  # a month before HistData's first one for the instrument
_UNAVAILABLE = "unavailable"  # HistData has no file for the month


class _HistDataRun(SourceRun):
    """One symbol's month files: fetched one at a time, in order."""

    fetch_workers = 1

    def __init__(self, source: HistDataSource, ctx: RunContext) -> None:
        self.ctx = ctx
        self.symbol = ctx.symbol
        self.inst = source.instrument(ctx.symbol)
        if not self.inst.scale_verified:
            log.warning(
                f"{self.symbol}: the symbol map's scale {self.inst.scale:g} for HistData "
                f"{self.inst.name} is not verified (scale_verified = false); check a few "
                "committed days against another source before trusting them"
            )
        self.now = datetime.now(UTC)
        self.settle_after = timedelta(days=ctx.commit_partial_after_days)
        self.status: dict[YearMonth, str] = {}  # ok | before_first | unavailable | failed
        self.first_tick: datetime | None = None
        self.last_tick: datetime | None = None
        self.requested = False
        self.months: list[YearMonth] = []
        self.failed_months: list[YearMonth] = []
        self.counts = dict.fromkeys(
            ("from_cache", "downloaded", "unavailable", "before_first", "failed"), 0
        )

    # --- month arithmetic for this run ---

    def settled_at(self, ym: YearMonth) -> datetime:
        return _month_span(ym)[1] + self.settle_after

    def is_settled(self, ym: YearMonth) -> bool:
        """A month whose file is final: past its end by the settle window, or before the first."""
        return ym < self.inst.first_month or self.now >= self.settled_at(ym)

    def _staged(self, ym: YearMonth) -> Path | None:
        cache_dir = self.ctx.cache_dir
        return None if cache_dir is None else _zip_path(cache_dir, self.symbol, ym)

    # --- the contract ---

    def plan(self, pending: Sequence[date]) -> list[_Month]:
        by_month: dict[YearMonth, list[date]] = {}
        for d in pending:
            for m in _months_for_day(d):
                by_month.setdefault(m, []).append(d)
        self.months = sorted(by_month)
        n_days = (self.ctx.end_utc_inclusive.date() - self.ctx.start_utc.date()).days + 1
        log.info(
            f"Exporting {self.symbol} from HistData {self.inst.name}: {len(pending)} of "
            f"{n_days} days to build from {len(self.months)} month file(s)"
        )
        return [_Month(m, tuple(by_month[m])) for m in self.months]

    def _download(self, ym: YearMonth) -> _Zip | _Failed | str:
        """One month from HistData, after the pause that keeps requests polite."""
        if self.requested and cancellation.wait(REQUEST_INTERVAL):
            raise KeyboardInterrupt()
        self.requested = True
        try:
            data = _fetch_month_zip(
                self.inst, ym, timeout=self.ctx.timeout, retries=self.ctx.retries
            )
        except MonthFetchError as e:
            return _Failed(str(e))
        return _UNAVAILABLE if data is None else _Zip(data)

    def fetch(self, unit: _Month) -> _Zip | _Failed | str:
        """A month's zip from the staged or kept copy when it is final, else from HistData."""
        ym = unit.ym
        if ym < self.inst.first_month:
            return _BEFORE_FIRST
        staged = self._staged(ym)
        if staged is not None:
            cached = staged if staged.exists() else self.ctx.kept_raw(_zip_relpath(ym))
            if cached is not None:
                mtime = datetime.fromtimestamp(cached.stat().st_mtime, UTC)
                if self.is_settled(ym) and mtime >= self.settled_at(ym):
                    return _Zip(cached.read_bytes(), cached=cached)
                log.info(
                    f"{self.symbol}: cached {cached} predates its month settling; fetching it again"
                )
                if cached == staged:  # a kept unit stays where --keep-raw put it
                    cached.unlink(missing_ok=True)
        return self._download(ym)

    def _fail(self, ym: YearMonth) -> None:
        self.status[ym] = "failed"
        self.counts["failed"] += 1
        self.failed_months.append(ym)

    def decode(self, unit: _Month, raw: _Zip | _Failed | str) -> pd.DataFrame | None:
        ym = unit.ym
        if raw == _BEFORE_FIRST:
            first = self.inst.first_month
            log.info(
                f"{self.symbol}: {ym[0]}-{ym[1]:02d} precedes HistData's first {self.inst.name} "
                f"month ({first[0]}-{first[1]:02d}); skipping"
            )
            self.status[ym] = "before_first"
            self.counts["before_first"] += 1
            return None
        if isinstance(raw, _Zip) and raw.cached is not None:
            try:
                ticks = _read_month_ticks(raw.data, scale=self.inst.scale)
                self.counts["from_cache"] += 1
                return self._ingest(ym, ticks)
            except Exception as e:
                log.warning(
                    f"{self.symbol}: cached {raw.cached} is corrupt ({e}); fetching it again"
                )
                raw.cached.unlink(missing_ok=True)
                raw = self._download(ym)
        if isinstance(raw, _Failed):
            log.warning(f"{self.symbol}: skipping HistData month {ym[0]}-{ym[1]:02d}: {raw.error}")
            self._fail(ym)
            return None
        if not isinstance(raw, _Zip):
            log.info(
                f"{self.symbol}: HistData has no {self.inst.name} file for {ym[0]}-{ym[1]:02d}"
            )
            self.status[ym] = "unavailable"
            self.counts["unavailable"] += 1
            return None
        try:
            ticks = _read_month_ticks(raw.data, scale=self.inst.scale)
        except Exception as e:
            log.warning(f"{self.symbol}: HistData month {ym[0]}-{ym[1]:02d} does not decode: {e}")
            self._fail(ym)
            return None
        self.counts["downloaded"] += 1
        # Only a settled month's file is final; a running month's is re-fetched next run.
        staged = self._staged(ym)
        if staged is not None and self.is_settled(ym):
            staged.parent.mkdir(parents=True, exist_ok=True)
            tmp = staged.with_suffix(staged.suffix + ".tmp")
            tmp.write_bytes(raw.data)
            tmp.replace(staged)
        return self._ingest(ym, ticks)

    def _ingest(self, ym: YearMonth, ticks: pd.DataFrame) -> pd.DataFrame | None:
        """Keep the ticks inside the file's own month and track the data's extent."""
        self.status[ym] = "ok"
        # Each file owns exactly its EST month, so a stray tick outside it can
        # never double up with the neighbouring file's ticks for the same day.
        span_start, span_end = _month_span(ym)
        lo, hi = ticks.index.searchsorted([span_start, span_end])
        if hi - lo < len(ticks):
            log.warning(
                f"{self.symbol}: ignoring {len(ticks) - (hi - lo)} tick(s) outside "
                f"{ym[0]}-{ym[1]:02d} in HistData's file for that month"
            )
            ticks = ticks.iloc[lo:hi]
        if ticks.empty:
            return None
        t0, t1 = ticks.index[0].to_pydatetime(), ticks.index[-1].to_pydatetime()
        self.first_tick = t0 if self.first_tick is None else min(self.first_tick, t0)
        self.last_tick = t1 if self.last_tick is None else max(self.last_tick, t1)
        return ticks

    def excludes(self, day: date) -> str | None:
        span = self.inst.excluded(day)
        return None if span is None else span.reason

    def _units(self, day: date) -> str:
        units = [_zip_name(self.inst, m) for m in _months_for_day(day) if self.status[m] == "ok"]
        return "+".join(units) if units else "none"

    def decide(self, day: date, *, has_data: bool) -> Decision:
        # A day inside one of the symbol map's exclusion spans is never committed
        # from HistData, whatever its data; the framework records why.
        # TODO(#83): a month-join level check, the provider's own guard against a
        # substituted instrument, belongs here too.
        span = self.inst.excluded(day)
        if span is not None:
            return Exclude(span.reason, source_unit=self._units(day))
        needed = _months_for_day(day)
        states = [self.status[m] for m in needed]
        if "failed" in states:
            return Leave("a month it needs failed to download")
        missing = [m for m in needed if self.status[m] == "unavailable"]
        if missing:
            if all(self.is_settled(m) for m in missing) and has_data:
                day_start, _ = _day_bounds(day)
                missing_hours = [
                    h
                    for h in range(24)
                    for m in missing
                    if _month_span(m)[0] <= day_start + timedelta(hours=h) < _month_span(m)[1]
                ]
                months = ", ".join(f"{y}-{mm:02d}" for y, mm in missing)
                return Commit(
                    Partial(
                        missing_hours=missing_hours,
                        gap_reason="histdata_month_unavailable",
                        note=f"HistData has no file for {months}",
                    )
                )
            return Leave("HistData has no file yet for a month it needs")
        day_start, day_end = _day_bounds(day)
        covered = (self.last_tick is not None and self.last_tick >= day_end) or all(
            self.is_settled(m) for m in needed
        )
        if not covered:
            # A later month may show the data runs past this day.
            return Wait("the HistData data does not reach past this day yet")
        if not has_data and (self.first_tick is None or self.first_tick >= day_start):
            return Leave("no HistData ticks before or on this day")
        return Commit()

    def committed(self, day: date, *, empty: bool) -> Provenance:
        return Provenance(scale_factor=self.inst.scale, source_unit=self._units(day))

    def finish(self) -> None:
        # Retire month zips whose days are all committed; keep the rest for the next run.
        cache_dir = self.ctx.cache_dir
        if cache_dir is not None:
            for ym, st in self.status.items():
                zpath = _zip_path(cache_dir, self.symbol, ym)
                if st != "ok" or not zpath.exists():
                    continue
                if all(_day_is_committed(cache_dir, self.symbol, d) for d in _days_for_month(ym)):
                    self.ctx.retire_raw(zpath, _zip_relpath(ym))
            try:
                (cache_dir / self.symbol / "_histdata").rmdir()
            except OSError:
                pass  # not empty or not there

        c = self.counts
        log.info(
            f"{self.symbol}: histdata months total={len(self.months)}, "
            f"from_cache={c['from_cache']}, downloaded={c['downloaded']}, "
            f"unavailable={c['unavailable']}, before_first={c['before_first']}, "
            f"failed={c['failed']}"
        )
        if self.failed_months:
            log.warning(
                f"{self.symbol}: {len(self.failed_months)} HistData month(s) failed after "
                f"{self.ctx.retries} attempt(s) and were skipped this run: "
                f"{', '.join(f'{y}-{m:02d}' for y, m in self.failed_months)}; re-run to retry"
            )


class HistDataSource(Source):
    """HistData.com's free monthly tick files."""

    name = SOURCE
    summary = (
        "HistData.com's monthly tick files, which reach back to 2000 for the FX majors and "
        "to 2010-11 for the indices, for the symbols in its symbol map (--symbol-map; the "
        f"shipped one maps {', '.join(sorted(HISTDATA_SYMBOLS))}); prices are scaled into "
        "the cache's units per symbol"
    )
    unit = "month file"
    partial_commit_rule = (
        "the age past the end of a month after which HistData's file for it is treated as final"
    )

    options = (
        SourceOption(
            "--symbol-map",
            type=Path,
            metavar="FILE",
            help="TOML symbol map: each cache symbol's HistData code, scale into the cache's "
            "units, first month, scale verification and exclusion spans (default: the shipped "
            f"{SYMBOL_MAP_EXAMPLE}; copy it to change it)",
        ),
    )

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        self.symbol_map: Path | None = self.option("symbol_map")
        self.symbols = load_symbol_map(self.symbol_map)  # SymbolMapError on a bad file

    def instrument(self, symbol: str) -> HistDataInstrument:
        """The symbol's HistData mapping (raises ``UnmappedSymbolError``)."""
        return lookup(symbol, self.symbols)

    def check_symbol(self, symbol: str) -> None:
        self.instrument(symbol)

    def describe(self, symbol: str) -> SourceDescription:
        # HistData prices are multiplied into the cache's units; the
        # divisor-equivalent keeps "cache price = source price / divisor".
        scale = self.instrument(symbol).scale
        return SourceDescription(price_divisor=1.0 / scale, params={"scale_factor": scale})

    def open(self, ctx: RunContext) -> _HistDataRun:
        return _HistDataRun(self, ctx)


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
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
    keep_raw: bool = False,
    symbol_map: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Export the UTC days of [start_utc, end_utc_inclusive] from HistData.

    :func:`tradedesk_marketdata.export.export_range` with a
    :class:`HistDataSource`: the same cache files, the same range CSVs and the
    same return value as any source. Days are written under the cache's symbol
    (``USA500IDXUSD``, not ``SPXUSD``).

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
      in ``_partial_days.jsonl`` (gap_reason ``histdata_month_unavailable``).
      A day that needs a month that failed to download is never committed in
      this run; the month is reported in the end-of-run summary.
    - Every day committed here is recorded in ``_sources.jsonl`` with source
      ``histdata``, the symbol's scale factor and the month zip(s) it came from.
    """
    return _export.export_range(
        source=HistDataSource({"symbol_map": symbol_map}),
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


__all__ = [
    "HISTDATA_SYMBOLS",
    "SYMBOL_MAP_EXAMPLE",
    "ExcludedSpan",
    "HistDataInstrument",
    "HistDataSource",
    "MonthFetchError",
    "SymbolMapError",
    "SOURCE",
    "UnmappedSymbolError",
    "decode_ticks",
    "export_range_histdata",
    "load_symbol_map",
    "lookup",
]
