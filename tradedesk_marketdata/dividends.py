"""Public index-dividend series in the store's reference-data area.

A series is named by the caller's configuration (``[[series]]`` tables in a
TOML file, see ``dividends.example.toml``); this module ships no series list.
A series has one of three kinds:

- ``points``: a published daily dividend-points index, one input.
- ``tr_pr``: the daily dividend return derived from two inputs, a
  total-return (``tr``) and a price-return (``pr``) index level:
  ``(TR_t / TR_p) / (PR_t / PR_p) - 1``, where ``p`` is the previous date on
  which both levels are published.
- ``monthly``: a public monthly dividend series, one input. It is a coarse
  fallback: the manifest and every row say so.

Each series lives in ``{store}/_reference/dividends/{id}/``, beside the rate
series of :mod:`tradedesk_marketdata.rates`:

- ``_raw/{sha256}.{ext}``: every downloaded body, content-addressed. Never
  changed or deleted.
- ``_fetches.jsonl``: one line per kept download: ``fetched_at`` (UTC),
  ``url``, ``http_status``, ``sha256``, ``bytes``, ``raw`` (the path under the
  series directory) and ``input`` (``points``, ``monthly``, ``tr`` or ``pr``).
- ``observations.csv``: one row per date with a value. ``value`` is the
  source's decimal text for ``points`` and ``monthly`` and the derived return
  for ``tr_pr``. ``published`` is the publication time as the source states
  it, empty when it states none. ``revised`` is ``true`` when an earlier fetch
  held another value for an input of the row.
- ``gaps.csv``: ``date,reason``. The expected days (or months) without a
  value. A gap is never filled.
- ``manifest.json``: the series configuration, the parsers, the derivation,
  every fetch and the sha256 of the two parsed files. It holds no write time
  and no tool version, so its sha256 pins the series.

:func:`build` makes the three files from the raw files alone, so
:func:`verify_series` can reproduce them byte for byte. A download that does
not parse is refused before anything is written.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import statistics
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from . import rates as _rates
from .rates import (
    DAILY_MAX_MEDIAN_STEP,
    Response,
    _csv_bytes,
    _sha,
    _write_atomic,
    manifest_bytes,
)
from .source import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, DEFAULT_RETRIES

log = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1

#: The reference-data area of a store, relative to the store root.
AREA = Path("_reference") / "dividends"

OBSERVATIONS = "observations.csv"
GAPS = "gaps.csv"
MANIFEST = "manifest.json"
FETCHES = "_fetches.jsonl"
RAW = "_raw"

KINDS = ("points", "tr_pr", "monthly")
CALENDARS = ("weekdays", "all")
GAP_REASONS = ("missing_in_source", "no_row", "withdrawn", "missing_tr", "missing_pr")

#: The derived return's decimal places, and the working precision of its arithmetic.
TR_PR_PLACES = 12
TR_PR_PRECISION = 34
TR_PR_FORMULA = "(TR_t / TR_p) / (PR_t / PR_p) - 1"

#: The unit of a ``tr_pr`` series when the configuration states none.
TR_PR_DEFAULT_UNIT = "fraction"

#: The median step, in days, between the dated rows of a monthly download.
MONTHLY_MEDIAN_STEP = (28, 31)

_ID = _rates._ID
_DECIMAL = _rates._DECIMAL
_MISSING_TOKENS = _rates._MISSING_TOKENS


class DividendsError(ValueError):
    """A configuration, a download or a store that cannot be used as it is."""


# ---------------------------------------------------------------------------
# Configuration


#: The options of the generic ``csv`` format, with their defaults (None: required or unset).
CSV_OPTIONS: dict[str, Any] = {
    "date_column": None,
    "value_column": None,
    "date_format": "%Y-%m-%d",
    "published_column": None,
    "period_column": None,
    "encoding": "utf-8-sig",
    "delimiter": ",",
    "skip_lines": 0,
    "footer_prefix": None,
}
_CSV_REQUIRED = ("date_column", "value_column")


@dataclass(frozen=True)
class InputConfig:
    """One input of a series: where to download it and how to read it."""

    source: str
    key: str | None = None
    url: str | None = None
    #: The ``csv`` format's options, as sorted (name, value) pairs; empty for a preset.
    options: tuple[tuple[str, Any], ...] = ()

    def option(self, name: str) -> Any:
        return dict(self.options).get(name, CSV_OPTIONS[name])

    def as_json(self) -> dict[str, Any]:
        return {"source": self.source, "key": self.key, "url": self.url, **dict(self.options)}

    def identity(self) -> tuple[Any, ...]:
        """What makes two inputs the same series: everything but the URL."""
        return (self.source, self.key, self.options)

    def resolved_url(self) -> str:
        url = self.url or FORMATS[self.source].url(self)
        if url is None:
            raise DividendsError(f"{self.source}: the format has no default URL; set 'url'")
        return url


@dataclass(frozen=True)
class SeriesConfig:
    """One ``[[series]]`` table of the caller's configuration."""

    id: str
    kind: str
    inputs: tuple[tuple[str, InputConfig], ...]
    unit: str | None = None
    calendar: str = "weekdays"

    @property
    def coarse(self) -> bool:
        return self.kind == "monthly"

    @property
    def frequency(self) -> str:
        return "monthly" if self.kind == "monthly" else "daily"

    def input(self, role: str) -> InputConfig:
        return dict(self.inputs)[role]

    def resolved_unit(self) -> str:
        if self.unit:
            return self.unit
        if self.kind == "tr_pr":
            return TR_PR_DEFAULT_UNIT
        (_, inp) = self.inputs[0]
        unit = FORMATS[inp.source].unit(inp)
        if unit is None:
            raise DividendsError(f"{self.id}: the source states no unit; set 'unit' for the series")
        return unit

    def identity(self) -> tuple[Any, ...]:
        return (self.kind, tuple((role, inp.identity()) for role, inp in self.inputs))

    def as_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind, "unit": self.unit}
        if self.kind != "monthly":
            d["calendar"] = self.calendar
        for role, inp in self.inputs:
            d["input" if role in ("points", "monthly") else role] = inp.as_json()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SeriesConfig:
        return _series_config(d, "manifest")


def _nonempty(d: dict[str, Any], k: str, where: str) -> str | None:
    v = d.get(k)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise DividendsError(f"{where}: {k!r} must be a non-empty string")
    return v.strip()


def _input_config(d: Any, role: str, where: str) -> InputConfig:
    if not isinstance(d, dict):
        raise DividendsError(f"{where}: the {role!r} input must be a table")
    where = f"{where} {role}"
    source = _nonempty(d, "source", where)
    if source is None:
        raise DividendsError(f"{where}: 'source' is required")
    if source not in FORMATS:
        raise DividendsError(f"{where}: source {source!r} is not one of {', '.join(FORMATS)}")
    fmt = FORMATS[source]
    allowed = {"source", "key", "url"} | (set(CSV_OPTIONS) if source == "csv" else set())
    unknown = sorted(k for k in d if k not in allowed and d[k] is not None)
    if unknown:
        raise DividendsError(f"{where}: unknown key(s) {', '.join(unknown)}")
    options: dict[str, Any] = {}
    if source == "csv":
        for k, default in CSV_OPTIONS.items():
            v = d.get(k)
            if v is None:
                if k in _CSV_REQUIRED:
                    raise DividendsError(f"{where}: csv needs {k!r}")
                continue
            if k == "skip_lines":
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise DividendsError(f"{where}: 'skip_lines' must be an integer >= 0")
            elif not isinstance(v, str) or not v:
                raise DividendsError(f"{where}: {k!r} must be a non-empty string")
            elif k == "delimiter" and len(v) != 1:
                raise DividendsError(f"{where}: 'delimiter' must be one character")
            elif k == "encoding":
                try:
                    "".encode(v)
                except LookupError:
                    raise DividendsError(f"{where}: unknown encoding {v!r}") from None
            if v != default:
                options[k] = v
    inp = InputConfig(
        source=source,
        key=_nonempty(d, "key", where),
        url=_nonempty(d, "url", where),
        options=tuple(sorted(options.items())),
    )
    fmt.check(inp, role, where)
    return inp


_SERIES_KEYS = frozenset({"id", "kind", "unit", "calendar", "input", "tr", "pr"})


def _series_config(d: dict[str, Any], where: str) -> SeriesConfig:
    unknown = sorted(k for k in set(d) - _SERIES_KEYS if d[k] is not None)
    if unknown:
        raise DividendsError(f"{where}: unknown key(s) {', '.join(unknown)}")
    sid = _nonempty(d, "id", where)
    if sid is None:
        raise DividendsError(f"{where}: 'id' is required and must be a non-empty string")
    if not _ID.match(sid):
        raise DividendsError(f"{where}: id {sid!r} must match {_ID.pattern}")
    where = f"{where} ({sid})"
    kind = _nonempty(d, "kind", where)
    if kind not in KINDS:
        raise DividendsError(f"{where}: kind must be one of {', '.join(KINDS)}")
    roles = ("tr", "pr") if kind == "tr_pr" else ("input",)
    other = ("input",) if kind == "tr_pr" else ("tr", "pr")
    for k in other:
        if d.get(k) is not None:
            raise DividendsError(f"{where}: a {kind} series takes no {k!r} table")
    for k in roles:
        if d.get(k) is None:
            raise DividendsError(f"{where}: a {kind} series needs an [series.{k}] table")
    inputs = tuple(
        (kind if k == "input" else k, _input_config(d[k], kind if k == "input" else k, where))
        for k in roles
    )
    calendar = d.get("calendar")
    if kind == "monthly":
        if calendar is not None:
            raise DividendsError(f"{where}: a monthly series takes no calendar (every month)")
        calendar = "weekdays"
    elif calendar is None:
        calendar = "weekdays"
    elif calendar not in CALENDARS:
        raise DividendsError(f"{where}: calendar must be one of {', '.join(CALENDARS)}")
    cfg = SeriesConfig(sid, kind, inputs, unit=_nonempty(d, "unit", where), calendar=calendar)
    cfg.resolved_unit()
    return cfg


def load_config(path: Path) -> list[SeriesConfig]:
    """The ``[[series]]`` tables of a TOML configuration file, checked."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise DividendsError(f"{path}: {e}") from None
    unknown = sorted(set(data) - {"series"})
    if unknown:
        raise DividendsError(f"{path}: unknown top-level key(s) {', '.join(unknown)}")
    tables = data.get("series")
    if not isinstance(tables, list) or not tables:
        raise DividendsError(f"{path}: no [[series]] table")
    out: list[SeriesConfig] = []
    seen: set[str] = set()
    for i, t in enumerate(tables, 1):
        cfg = _series_config(t, f"{path} [[series]] #{i}")
        if cfg.id in seen:
            raise DividendsError(f"{path}: series id {cfg.id!r} appears more than once")
        seen.add(cfg.id)
        out.append(cfg)
    return out


# ---------------------------------------------------------------------------
# Parsers: one per format


@dataclass(frozen=True)
class Row:
    """One dated row of a download: ``value`` is None when the source gives none."""

    value: str | None
    published: str | None = None
    period: str | None = None


@dataclass(frozen=True)
class Format:
    """A published format: how to address an input and how to read a download."""

    name: str
    version: int
    #: The raw file's extension.
    ext: str
    url: Callable[[InputConfig], str | None]
    parse: Callable[[bytes, InputConfig], dict[date, Row]]
    #: Checks an input for its role (``points``, ``monthly``, ``tr`` or ``pr``).
    check: Callable[[InputConfig, str, str], None]
    #: The unit the format implies for an input, or None.
    unit: Callable[[InputConfig], str | None]


def _value(text: str, where: str) -> str | None:
    v = text.strip()
    if v in _MISSING_TOKENS:
        return None
    if not _DECIMAL.match(v):
        raise DividendsError(f"{where}: value {v!r} is not a decimal number")
    return v


def _put(rows: dict[date, Row], d: date, row: Row, where: str) -> None:
    if d in rows:
        raise DividendsError(f"{where}: date {d.isoformat()} appears more than once")
    rows[d] = row


def _date(text: str, fmt: str, where: str) -> date:
    try:
        return datetime.strptime(text.strip(), fmt).date()
    except ValueError:
        raise DividendsError(f"{where}: {text.strip()!r} is not a date ({fmt})") from None


def _no_url(inp: InputConfig) -> str | None:
    return None


def _no_unit(inp: InputConfig) -> str | None:
    return None


def _csv_rows(text: str, inp: InputConfig, name: str) -> dict[date, Row]:
    """The dated rows of a delimited text with a header row, read by the input's options."""
    lines = list(csv.reader(io.StringIO(text), delimiter=inp.option("delimiter")))
    lines = lines[inp.option("skip_lines") :]
    while lines and not any(c.strip() for c in lines[0]):
        lines = lines[1:]
    if not lines:
        raise DividendsError(f"{name}: no header row")
    header = [c.strip() for c in lines[0]]
    cols: dict[str, int] = {}
    for opt in ("date_column", "value_column", "published_column", "period_column"):
        want = inp.option(opt)
        if want is None:
            continue
        if want not in header:
            raise DividendsError(f"{name}: the header has no column {want!r}")
        cols[opt] = header.index(want)
    footer = inp.option("footer_prefix")
    date_format = inp.option("date_format")
    rows: dict[date, Row] = {}
    for n, line in enumerate(lines[1:], inp.option("skip_lines") + 2):
        if not any(c.strip() for c in line):
            continue
        if footer is not None and line[0].strip().startswith(footer):
            continue
        where = f"{name} line {n}"
        if len(line) != len(header):
            raise DividendsError(f"{where}: {len(line)} fields, not {len(header)}")

        def cell(opt: str, line: list[str] = line) -> str | None:
            return line[cols[opt]].strip() or None if opt in cols else None

        d = _date(line[cols["date_column"]], date_format, where)
        row = Row(
            _value(line[cols["value_column"]], where),
            cell("published_column"),
            cell("period_column"),
        )
        _put(rows, d, row, where)
    return rows


# Generic CSV: a header row, then one dated row per line; the columns and the date
# format are the input's options.


def _csv_parse(body: bytes, inp: InputConfig) -> dict[date, Row]:
    try:
        text = body.decode(inp.option("encoding"))
    except UnicodeDecodeError as e:
        raise DividendsError(
            f"csv: the download is not {inp.option('encoding')} text ({e})"
        ) from None
    return _csv_rows(text, inp, "csv")


def _csv_check(inp: InputConfig, role: str, where: str) -> None:
    if inp.key is not None:
        raise DividendsError(f"{where}: csv takes no 'key'; name the columns")
    if inp.url is None:
        raise DividendsError(f"{where}: csv has no default URL; set 'url'")


# Nikkei Inc. index CSVs (indexes.nikkei.co.jp/nkave/historical/): header "Date of
# Data,Close,...", quoted "YYYY/MM/DD" dates, and one footer line of copyright text in
# Shift JIS. The dividend-points file adds the "Year" its points accumulate for.

_NIKKEI_BASE = "https://indexes.nikkei.co.jp/nkave/historical/"
#: key -> (file, value column index, roles it may fill, unit)
_NIKKEI: dict[str, tuple[str, int, tuple[str, ...], str]] = {
    "dividend_points": ("nikkei_stock_average_dp_daily_en.csv", 1, ("points",), "index points"),
    "total_return": ("nikkei_225_total_return_index_daily_en.csv", 1, ("tr",), "index level"),
    "net_total_return": ("nikkei_225_total_return_index_daily_en.csv", 2, ("tr",), "index level"),
    "price": ("nikkei_stock_average_daily_en.csv", 1, ("pr",), "index level"),
}
_NIKKEI_FOOTER = "This material is a copyrightable work of Nikkei"


def _nikkei_check(inp: InputConfig, role: str, where: str) -> None:
    if inp.key not in _NIKKEI:
        raise DividendsError(f"{where}: a nikkei key is one of {', '.join(_NIKKEI)}")
    roles = _NIKKEI[inp.key][2]
    if role not in roles:
        raise DividendsError(f"{where}: nikkei {inp.key!r} is a {'/'.join(roles)} input")


def _nikkei_url(inp: InputConfig) -> str:
    return _NIKKEI_BASE + _NIKKEI[inp.key or ""][0]


def _nikkei_unit(inp: InputConfig) -> str:
    return _NIKKEI[inp.key or ""][3]


def _nikkei_parse(body: bytes, inp: InputConfig) -> dict[date, Row]:
    try:
        text = body.decode("cp932")
    except UnicodeDecodeError as e:
        raise DividendsError(f"nikkei: the download is not Shift JIS text ({e})") from None
    header = next(csv.reader(io.StringIO(text)), [])
    _, col, _, _ = _NIKKEI[inp.key or ""]
    if len(header) <= col or header[0].strip() != "Date of Data":
        raise DividendsError("nikkei: the header is not 'Date of Data,Close,...'")
    if inp.key == "net_total_return" and "Net Total Return" not in header[col]:
        raise DividendsError(f"nikkei: column {col + 1} is {header[col]!r}, not a net total return")
    if inp.key == "dividend_points" and header[2:3] != ["Year"]:
        raise DividendsError("nikkei: the dividend-points header has no 'Year' column")
    preset = InputConfig(
        "csv",
        options=(
            ("date_column", "Date of Data"),
            ("date_format", "%Y/%m/%d"),
            ("footer_prefix", _NIKKEI_FOOTER),
            ("period_column", "Year" if inp.key == "dividend_points" else None),
            ("value_column", header[col].strip()),
        ),
    )
    return _csv_rows(text, preset, "nikkei")


# Robert J. Shiller's monthly stock-market data (ie_data.xls, shillerdata.com): sheet
# "Data", a header row whose first cell is "Date", then one row per month dated
# YYYY.MM as a number (October is YYYY.1), and a note row after the last month.


def _shiller_check(inp: InputConfig, role: str, where: str) -> None:
    if role != "monthly":
        raise DividendsError(f"{where}: shiller is a monthly input only")
    if inp.url is None:
        raise DividendsError(f"{where}: shiller has no stable URL; set 'url'")


def _shiller_month(x: Any, where: str) -> date:
    year = int(x)
    month = round((float(x) - year) * 100)
    if not 1 <= month <= 12:
        raise DividendsError(f"{where}: {x!r} is not a YYYY.MM month")
    return date(year, month, 1)


def _shiller_parse(body: bytes, inp: InputConfig) -> dict[date, Row]:
    try:
        import xlrd
    except ImportError:
        raise DividendsError(
            "shiller: reading .xls needs xlrd: pip install 'tradedesk-marketdata[xls]'"
        ) from None
    try:
        book = xlrd.open_workbook(file_contents=body)
    except Exception as e:
        raise DividendsError(f"shiller: the download is not an .xls workbook ({e})") from None
    if "Data" not in book.sheet_names():
        raise DividendsError("shiller: the workbook has no 'Data' sheet")
    sh = book.sheet_by_name("Data")
    header_at = next(
        (r for r in range(sh.nrows) if str(sh.cell_value(r, 0)).strip() == "Date"), None
    )
    if header_at is None:
        raise DividendsError("shiller: no header row starting 'Date'")
    header = [str(v).strip() for v in sh.row_values(header_at)]
    want = inp.key or "D"
    if want not in header:
        raise DividendsError(f"shiller: the header has no column {want!r}")
    c = header.index(want)
    rows: dict[date, Row] = {}
    r = header_at + 1
    while r < sh.nrows and sh.cell_type(r, 0) == xlrd.XL_CELL_NUMBER:
        where = f"shiller row {r + 1}"
        d = _shiller_month(sh.cell_value(r, 0), where)
        kind = sh.cell_type(r, c) if c < sh.ncols else xlrd.XL_CELL_EMPTY
        if kind == xlrd.XL_CELL_NUMBER:
            # The shortest text that reads back as the cell's number, never in exponent form.
            value: str | None = format(Decimal(repr(float(sh.cell_value(r, c)))), "f")
        elif kind == xlrd.XL_CELL_TEXT:
            value = _value(sh.cell_value(r, c), where)
        elif kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
            value = None
        else:
            raise DividendsError(f"{where}: the {want!r} cell is not a number")
        _put(rows, d, Row(value), where)
        r += 1
    for rest in range(r, sh.nrows):
        if sh.cell_type(rest, 0) == xlrd.XL_CELL_NUMBER:
            raise DividendsError(f"shiller row {rest + 1}: a dated row after the end of the data")
    return rows


FORMATS: dict[str, Format] = {
    "csv": Format("csv", 1, "csv", _no_url, _csv_parse, _csv_check, _no_unit),
    "nikkei": Format("nikkei", 1, "csv", _nikkei_url, _nikkei_parse, _nikkei_check, _nikkei_unit),
    "shiller": Format("shiller", 1, "xls", _no_url, _shiller_parse, _shiller_check, _no_unit),
}


def _check_daily(rows: dict[date, Row], source: str) -> None:
    days = sorted(rows)
    if len(days) < 2:
        raise DividendsError(f"{source}: one dated row does not show a daily series")
    median = statistics.median((b - a).days for a, b in zip(days, days[1:], strict=False))
    if median > DAILY_MAX_MEDIAN_STEP:
        raise DividendsError(
            f"{source}: the dates are a median {median:g} days apart, so the series is not daily"
        )


def _to_months(rows: dict[date, Row], source: str) -> dict[date, Row]:
    out: dict[date, Row] = {}
    for d, row in rows.items():
        month = d.replace(day=1)
        if month in out:
            raise DividendsError(
                f"{source}: two rows fall in {month:%Y-%m}, so the series is not monthly"
            )
        out[month] = row
    days = sorted(out)
    if len(days) < 2:
        raise DividendsError(f"{source}: one dated row does not show a monthly series")
    median = statistics.median((b - a).days for a, b in zip(days, days[1:], strict=False))
    lo, hi = MONTHLY_MEDIAN_STEP
    if not lo <= median <= hi:
        raise DividendsError(
            f"{source}: the dates are a median {median:g} days apart, so the series is not monthly"
        )
    return out


def parse(body: bytes, cfg: SeriesConfig, role: str) -> dict[date, Row]:
    """The dated rows of one download for one input. Raises :class:`DividendsError`.

    A ``monthly`` input's dates are taken as their month (dated the first) and must
    step by a month; any other input's dates must step by a day.
    """
    inp = cfg.input(role)
    rows = FORMATS[inp.source].parse(body, inp)
    if not any(r.value is not None for r in rows.values()):
        raise DividendsError(f"{inp.source}: the download holds no value")
    if role == "monthly":
        return _to_months(rows, inp.source)
    _check_daily(rows, inp.source)
    return rows


# ---------------------------------------------------------------------------
# Building the parsed files from the fetches


@dataclass(frozen=True)
class Fetch:
    """One line of ``_fetches.jsonl``."""

    fetched_at: str
    url: str
    http_status: int
    sha256: str
    bytes: int
    raw: str
    input: str

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Built:
    """The three files a series' fetches make, as bytes."""

    observations: bytes
    gaps: bytes
    manifest: bytes
    rows: int
    gap_rows: int
    revised: int


@dataclass
class _Merged:
    """One input's rows merged over its fetches, with what each date was."""

    values: dict[date, Row] = field(default_factory=dict)
    seen: dict[date, set[str]] = field(default_factory=dict)
    missing: set[date] = field(default_factory=set)

    def revised(self, d: date) -> bool:
        return len(self.seen.get(d, ())) > 1

    def flags(self) -> dict[date, str]:
        """The dates the source listed without a value, or no longer has a value for."""
        out = {d: "missing_in_source" for d in self.missing if d not in self.values}
        out.update({d: "withdrawn" for d in self.seen if d not in self.values})
        return out


def _merge(parsed: Sequence[dict[date, Row]]) -> _Merged:
    """Merge one input's fetches in order: a later fetch wins for each date in its span."""
    m = _Merged()
    for rows in parsed:
        lo, hi = min(rows), max(rows)
        for d in [d for d in m.values if lo <= d <= hi and d not in rows]:
            del m.values[d]
        m.missing = {d for d in m.missing if not lo <= d <= hi}
        for d, row in rows.items():
            if row.value is None:
                m.values.pop(d, None)
                m.missing.add(d)
                continue
            m.values[d] = row
            m.seen.setdefault(d, set()).add(row.value)
    return m


def _days(first: date, last: date, step: Callable[[date], date]) -> Iterator[date]:
    d = first
    while d <= last:
        yield d
        d = step(d)


def _next_day(d: date) -> date:
    return d + timedelta(days=1)


def _next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def _expected(cfg: SeriesConfig, first: date, last: date) -> Iterator[date]:
    if cfg.kind == "monthly":
        return _days(first, last, _next_month)
    return (d for d in _days(first, last, _next_day) if cfg.calendar == "all" or d.weekday() < 5)


def _single(cfg: SeriesConfig, m: _Merged, unit: str) -> tuple[list[list[str]], dict[date, str]]:
    if not m.values:
        raise DividendsError(f"{cfg.id}: the fetches hold no value")
    obs = sorted(m.values.items())
    first, last = obs[0][0], obs[-1][0]
    rows = [
        [
            d.isoformat(),
            r.value or "",
            unit,
            r.period or "",
            r.published or "",
            "true" if m.revised(d) else "false",
            *(["true"] if cfg.coarse else []),
        ]
        for d, r in obs
    ]
    absent = {d: "no_row" for d in _expected(cfg, first, last) if d not in m.values}
    absent.update(m.flags())
    return rows, absent


def _ratio(tr_t: str, tr_p: str, pr_t: str, pr_p: str, where: str) -> str:
    with localcontext() as ctx:
        ctx.prec = TR_PR_PRECISION
        try:
            v = (Decimal(tr_t) / Decimal(tr_p)) / (Decimal(pr_t) / Decimal(pr_p)) - 1
        except (InvalidOperation, ZeroDivisionError):
            raise DividendsError(f"{where}: a level of zero cannot make a return") from None
        q = v.quantize(Decimal(1).scaleb(-TR_PR_PLACES), rounding=ROUND_HALF_EVEN)
    return format(q, "f")


def _published(a: str | None, b: str | None) -> str:
    """The later of the two inputs' publication texts (ISO dates and times sort in time)."""
    return max(a or "", b or "")


def _tr_pr(
    cfg: SeriesConfig, tr: _Merged, pr: _Merged, unit: str
) -> tuple[list[list[str]], dict[date, str]]:
    both = sorted(set(tr.values) & set(pr.values))
    if len(both) < 2:
        raise DividendsError(f"{cfg.id}: fewer than two dates have both a tr and a pr level")
    base, last = both[0], both[-1]
    rows = []
    for p, t in zip(both, both[1:], strict=False):
        rows.append(
            [
                t.isoformat(),
                _ratio(
                    tr.values[t].value or "",
                    tr.values[p].value or "",
                    pr.values[t].value or "",
                    pr.values[p].value or "",
                    f"{cfg.id} {t.isoformat()}",
                ),
                unit,
                p.isoformat(),
                _published(tr.values[t].published, pr.values[t].published),
                "true" if any(x.revised(d) for x in (tr, pr) for d in (t, p)) else "false",
            ]
        )
    have = set(both)
    flags = {**pr.flags(), **tr.flags()}
    candidates = set(_expected(cfg, base, last)) | set(tr.values) | set(pr.values) | set(flags)
    absent: dict[date, str] = {}
    for d in candidates:
        if d in have or not base <= d <= last:
            continue
        if d in tr.values:
            absent[d] = "missing_pr"
        elif d in pr.values:
            absent[d] = "missing_tr"
        else:
            absent[d] = flags.get(d, "no_row")
    return rows, absent


def build(cfg: SeriesConfig, fetches: Sequence[tuple[Fetch, bytes]]) -> Built:
    """Make ``observations.csv``, ``gaps.csv`` and ``manifest.json`` from the fetches, in order.

    Each input's fetches merge in order: a later fetch wins for each date it lists. A
    date an earlier fetch had a value for, inside the later fetch's span but without a
    value there, is removed and flagged ``withdrawn``.
    """
    roles = [role for role, _ in cfg.inputs]
    for f, _ in fetches:
        if f.input not in roles:
            raise DividendsError(f"{f.raw}: input {f.input!r} is not one of {', '.join(roles)}")
    merged = {
        role: _merge([parse(body, cfg, role) for f, body in fetches if f.input == role])
        for role in roles
    }
    for role in roles:
        if not any(f.input == role for f, _ in fetches):
            raise DividendsError(f"{cfg.id}: no {role} fetch to build from")
    unit = cfg.resolved_unit()
    if cfg.kind == "tr_pr":
        rows, absent = _tr_pr(cfg, merged["tr"], merged["pr"], unit)
        header = ["date", "value", "unit", "span_from", "published", "revised"]
    else:
        rows, absent = _single(cfg, merged[cfg.kind], unit)
        header = ["date", "value", "unit", "period", "published", "revised"]
        if cfg.coarse:
            header.append("coarse")
    obs_bytes = _csv_bytes(header, rows)
    gaps_bytes = _csv_bytes(
        ("date", "reason"), [(d.isoformat(), absent[d]) for d in sorted(absent)]
    )
    revised = sum(1 for r in rows if r[5] == "true")
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "series": cfg.as_json(),
        "frequency": cfg.frequency,
        "coarse": cfg.coarse,
        "unit": unit,
        "parsers": {
            role: {"format": FORMATS[inp.source].name, "version": FORMATS[inp.source].version}
            for role, inp in cfg.inputs
        },
        "fetches": [f.as_json() for f, _ in fetches],
        "observations": {
            "path": OBSERVATIONS,
            "sha256": _sha(obs_bytes),
            "rows": len(rows),
            "first": rows[0][0],
            "last": rows[-1][0],
            "revised": revised,
        },
        "gaps": {"path": GAPS, "sha256": _sha(gaps_bytes), "rows": len(absent)},
    }
    if cfg.kind == "tr_pr":
        manifest["derivation"] = {
            "formula": TR_PR_FORMULA,
            "places": TR_PR_PLACES,
            "precision": TR_PR_PRECISION,
            "rounding": "ROUND_HALF_EVEN",
        }
    return Built(obs_bytes, gaps_bytes, manifest_bytes(manifest), len(rows), len(absent), revised)


# ---------------------------------------------------------------------------
# The store


def series_dir(store: Path, series_id: str) -> Path:
    if not _ID.match(series_id):
        raise DividendsError(f"series id {series_id!r} must match {_ID.pattern}")
    return store / AREA / series_id


def store_series(store: Path) -> list[str]:
    """The series present in a store's dividend area (directories with a manifest)."""
    area = store / AREA
    if not area.is_dir():
        return []
    return sorted(d.name for d in area.iterdir() if d.is_dir() and (d / MANIFEST).is_file())


def read_fetches(sdir: Path) -> list[Fetch]:
    path = sdir / FETCHES
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(
                Fetch(
                    d["fetched_at"],
                    d["url"],
                    int(d["http_status"]),
                    d["sha256"],
                    int(d["bytes"]),
                    d["raw"],
                    d["input"],
                )
            )
        except (ValueError, KeyError, TypeError) as e:
            raise DividendsError(f"{path} line {n}: not a fetch record ({e})") from None
    return out


def _read_raw(sdir: Path, f: Fetch) -> bytes:
    path = sdir / f.raw
    if not path.is_file():
        raise DividendsError(f"{f.raw}: the raw file is missing")
    body = path.read_bytes()
    if _sha(body) != f.sha256:
        raise DividendsError(f"{f.raw}: sha256 {_sha(body)} does not match the record {f.sha256}")
    return body


def stored_config(sdir: Path) -> SeriesConfig | None:
    path = sdir / MANIFEST
    if not path.exists():
        return None
    try:
        return SeriesConfig.from_json(json.loads(path.read_text(encoding="utf-8"))["series"])
    except (ValueError, KeyError, TypeError) as e:
        raise DividendsError(f"{path}: not a series manifest ({e})") from None


# ---------------------------------------------------------------------------
# Fetch


def download(
    url: str,
    *,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> Response:
    """GET one URL with the package's backoff. Raises :class:`DividendsError` after ``retries``."""
    try:
        return _rates.download(url, timeout=timeout, retries=retries)
    except _rates.RatesError as e:
        raise DividendsError(str(e)) from None


@dataclass
class FetchResult:
    """What one series' fetch did. ``status`` is ``updated``, ``unchanged`` or ``failed``."""

    id: str
    status: str
    rows: int | None = None
    gaps: int | None = None
    revised: int | None = None
    manifest_sha256: str | None = None
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def fetch_series(
    store: Path,
    cfg: SeriesConfig,
    *,
    get: Callable[[str], Response] | None = None,
    now: Callable[[], str] = _rates._now,
) -> FetchResult:
    """Download each input of one series, keep the raw bodies and rebuild the series' files.

    Every earlier raw file is checked against its sha256 and every new body is parsed
    before anything is written. The series' kind and inputs (all but their URLs) must
    match the stored manifest's: another series cannot be written under the same id.
    """
    sdir = series_dir(store, cfg.id)
    prior_cfg = stored_config(sdir)
    if prior_cfg is not None and prior_cfg.identity() != cfg.identity():
        raise DividendsError(
            f"{cfg.id}: the store holds another series under this id; its kind or inputs differ"
        )
    prior = [(f, _read_raw(sdir, f)) for f in read_fetches(sdir)]
    new: list[tuple[Fetch, bytes]] = []
    for role, inp in cfg.inputs:
        url = inp.resolved_url()
        fetched_at = now()
        resp = (get or download)(url)
        parse(resp.body, cfg, role)
        sha = _sha(resp.body)
        latest = [f for f, _ in prior if f.input == role]
        if latest and latest[-1].sha256 == sha:
            continue
        ext = FORMATS[inp.source].ext
        f = Fetch(fetched_at, url, resp.status, sha, len(resp.body), f"{RAW}/{sha}.{ext}", role)
        new.append((f, resp.body))

    built = build(cfg, [*prior, *new])
    files = ((OBSERVATIONS, built.observations), (GAPS, built.gaps), (MANIFEST, built.manifest))
    result = FetchResult(
        cfg.id, "updated", built.rows, built.gap_rows, built.revised, _sha(built.manifest)
    )
    if not new and all(
        (sdir / name).is_file() and (sdir / name).read_bytes() == data for name, data in files
    ):
        result.status = "unchanged"
        return result

    for f, body in new:
        raw_path = sdir / f.raw
        if not raw_path.exists():
            _write_atomic(raw_path, body)
        with (sdir / FETCHES).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(f.as_json(), sort_keys=True) + "\n")
    for name, data in files:
        _write_atomic(sdir / name, data)
    return result


def fetch(
    store: Path,
    configs: Sequence[SeriesConfig],
    *,
    get: Callable[[str], Response] | None = None,
    now: Callable[[], str] = _rates._now,
) -> list[FetchResult]:
    """Fetch each series in turn; a failed series is reported and the next one goes on."""
    out = []
    for cfg in configs:
        try:
            out.append(fetch_series(store, cfg, get=get, now=now))
        except DividendsError as e:
            log.error("%s: %s", cfg.id, e)
            out.append(FetchResult(cfg.id, "failed", error=str(e)))
    return out


# ---------------------------------------------------------------------------
# Verify


@dataclass
class VerifyReport:
    """The findings for one series; it passes when ``findings`` is empty."""

    id: str
    findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fetches: int = 0
    manifest_sha256: str | None = None

    @property
    def failed(self) -> bool:
        return bool(self.findings)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ok": not self.failed,
            "findings": self.findings,
            "warnings": self.warnings,
            "fetches": self.fetches,
            "manifest_sha256": self.manifest_sha256,
        }


def verify_series(store: Path, series_id: str) -> VerifyReport:
    """Re-read a series' raw files and reproduce its three files byte for byte. Writes nothing."""
    rep = VerifyReport(series_id)
    try:
        sdir = series_dir(store, series_id)
        cfg = stored_config(sdir)
        if cfg is None:
            rep.findings.append(f"{MANIFEST} is missing")
            return rep
        if cfg.id != series_id:
            rep.findings.append(f"{MANIFEST} names series {cfg.id!r}")
            return rep
        recs = read_fetches(sdir)
        rep.fetches = len(recs)
        bodies = []
        for f in recs:
            try:
                bodies.append((f, _read_raw(sdir, f)))
            except DividendsError as e:
                rep.findings.append(str(e))
        if rep.findings:
            return rep
        built = build(cfg, bodies)
    except DividendsError as e:
        rep.findings.append(str(e))
        return rep
    for name, want in (
        (OBSERVATIONS, built.observations),
        (GAPS, built.gaps),
        (MANIFEST, built.manifest),
    ):
        path = sdir / name
        if not path.is_file():
            rep.findings.append(f"{name} is missing")
        elif path.read_bytes() != want:
            rep.findings.append(f"{name} differs from the file rebuilt from the raw files")
    rep.manifest_sha256 = _sha((sdir / MANIFEST).read_bytes())
    recorded = {f.raw for f in recs}
    raw_dir = sdir / RAW
    if raw_dir.is_dir():
        for p in sorted(raw_dir.iterdir()):
            rel = f"{RAW}/{p.name}"
            if rel not in recorded:
                rep.warnings.append(f"{rel}: a raw file with no fetch record")
    return rep


def verify(store: Path, series_ids: Sequence[str] | None = None) -> list[VerifyReport]:
    ids = list(series_ids) if series_ids else store_series(store)
    return [verify_series(store, s) for s in ids]


def render_fetch_text(results: Sequence[FetchResult]) -> str:
    lines = []
    for r in results:
        if r.status == "failed":
            lines.append(f"{r.id}: failed: {r.error}")
        else:
            lines.append(
                f"{r.id}: {r.status}: {r.rows} observation(s), {r.gaps} gap(s), "
                f"{r.revised} revised; manifest sha256 {r.manifest_sha256}"
            )
    return "\n".join(lines) + ("\n" if lines else "")


def render_verify_text(reports: Sequence[VerifyReport]) -> str:
    lines = []
    for r in reports:
        if r.failed:
            lines.append(f"{r.id}: FAIL")
            lines.extend(f"  - {f}" for f in r.findings)
        else:
            lines.append(f"{r.id}: ok ({r.fetches} fetch(es)); manifest sha256 {r.manifest_sha256}")
        lines.extend(f"  ! {w}" for w in r.warnings)
    if not reports:
        lines.append("no series in the store's dividend area")
    return "\n".join(lines) + "\n"
