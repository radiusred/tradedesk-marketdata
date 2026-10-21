"""Public central-bank daily rate series in the store's reference-data area.

A series is named by the caller's configuration (``[[series]]`` tables in a
TOML file, see ``rates.example.toml``); this module ships no series list. Each
series lives in ``{store}/_reference/rates/{id}/``:

- ``_raw/{sha256}.{ext}``: every downloaded body, content-addressed, with the
  format's extension (``csv``, ``xls`` or ``xlsx``). Never changed or deleted.
- ``_fetches.jsonl``: one line per kept download: ``fetched_at`` (UTC),
  ``url``, ``sha256``, ``bytes``, ``raw`` (the path under the series
  directory).
- ``observations.csv``: ``date,value,unit,published,revised``. ``value`` is
  the source's decimal text, unchanged. ``published`` is the publication date
  as the source states it, as ``YYYY-MM-DD``, empty when the source states
  none. ``revised`` is ``true`` when an earlier fetch held another value for
  the date.
- ``gaps.csv``: ``date,reason``. The days without a value: every expected day
  (``calendar``) between the first and last observation, and every row the
  source lists without a value. A gap is never filled.
- ``manifest.json``: the series configuration, the parser, every fetch and
  the sha256 of the two parsed files. It holds no write time and no tool
  version, so it is a pure function of the fetches: its sha256 pins the
  series.

:func:`build` makes the three files from the raw files alone, so
:func:`verify_series` can reproduce them byte for byte. A download that does
not parse is refused before anything is written. A download whose bytes equal
the latest fetch's adds no fetch line, so a re-check that finds nothing new
leaves the manifest, and its sha256, unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import statistics
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import url2pathname
from xml.etree import ElementTree as ET

import requests

from . import __version__
from .cancel import cancellation, check_cancelled
from .source import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_BACKOFF_FACTOR,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
)

log = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1

#: The reference-data area of a store, relative to the store root.
AREA = Path("_reference") / "rates"

OBSERVATIONS = "observations.csv"
GAPS = "gaps.csv"
MANIFEST = "manifest.json"
FETCHES = "_fetches.jsonl"
RAW = "_raw"

CALENDARS = ("weekdays", "all")
GAP_REASONS = ("missing_in_source", "no_row", "withdrawn")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DECIMAL = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")
_MISSING_TOKENS = frozenset({"", ".", "NA", "N/A", "NaN", "nan", "ND", "null"})

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": f"tradedesk-marketdata/{__version__}"})


class RatesError(ValueError):
    """A configuration, a download or a store that cannot be used as it is."""


# ---------------------------------------------------------------------------
# Configuration


@dataclass(frozen=True)
class SeriesConfig:
    """One ``[[series]]`` table of the caller's configuration."""

    id: str
    source: str
    key: str
    url: str | None = None
    unit: str | None = None
    calendar: str = "weekdays"
    start: date | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "key": self.key,
            "url": self.url,
            "unit": self.unit,
            "calendar": self.calendar,
            "start": self.start.isoformat() if self.start else None,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SeriesConfig:
        return _series_config(d, "manifest")

    def resolved_url(self) -> str:
        return self.url or FORMATS[self.source].url(self)


_CONFIG_KEYS = frozenset({"id", "source", "key", "url", "unit", "calendar", "start"})


def _series_config(d: dict[str, Any], where: str) -> SeriesConfig:
    unknown = sorted(set(d) - _CONFIG_KEYS)
    if unknown:
        raise RatesError(f"{where}: unknown key(s) {', '.join(unknown)}")
    for k in ("id", "source", "key"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            raise RatesError(f"{where}: {k!r} is required and must be a non-empty string")
    sid = d["id"].strip()
    if not _ID.match(sid):
        raise RatesError(f"{where}: id {sid!r} must match {_ID.pattern}")
    source = d["source"].strip()
    if source not in FORMATS:
        raise RatesError(
            f"{where} ({sid}): source {source!r} is not one of {', '.join(sorted(FORMATS))}"
        )
    for k in ("url", "unit"):
        if d.get(k) is not None and (not isinstance(d[k], str) or not d[k].strip()):
            raise RatesError(f"{where} ({sid}): {k!r} must be a non-empty string")
    calendar = d.get("calendar") or "weekdays"
    if calendar not in CALENDARS:
        raise RatesError(f"{where} ({sid}): calendar must be one of {', '.join(CALENDARS)}")
    start = d.get("start")
    if isinstance(start, str):
        try:
            start = date.fromisoformat(start)
        except ValueError:
            raise RatesError(f"{where} ({sid}): start {start!r} is not YYYY-MM-DD") from None
    if start is not None and (not isinstance(start, date) or isinstance(start, datetime)):
        raise RatesError(f"{where} ({sid}): start must be a date (YYYY-MM-DD)")
    cfg = SeriesConfig(
        id=sid,
        source=source,
        key=d["key"].strip(),
        url=d["url"].strip() if d.get("url") else None,
        unit=d["unit"].strip() if d.get("unit") else None,
        calendar=calendar,
        start=start,
    )
    FORMATS[source].check_key(cfg)
    return cfg


def load_config(path: Path) -> list[SeriesConfig]:
    """The ``[[series]]`` tables of a TOML configuration file, checked."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise RatesError(f"{path}: {e}") from None
    unknown = sorted(set(data) - {"series"})
    if unknown:
        raise RatesError(f"{path}: unknown top-level key(s) {', '.join(unknown)}")
    tables = data.get("series")
    if not isinstance(tables, list) or not tables:
        raise RatesError(f"{path}: no [[series]] table")
    out: list[SeriesConfig] = []
    seen: set[str] = set()
    for i, t in enumerate(tables, 1):
        cfg = _series_config(t, f"{path} [[series]] #{i}")
        if cfg.id in seen:
            raise RatesError(f"{path}: series id {cfg.id!r} appears more than once")
        seen.add(cfg.id)
        out.append(cfg)
    return out


# ---------------------------------------------------------------------------
# Parsers: one per published format


@dataclass(frozen=True)
class Row:
    """One dated row of a download: ``value`` is None when the source gives none."""

    value: str | None
    unit: str | None
    published: str | None


@dataclass(frozen=True)
class Format:
    """A source's published format: how to address a series and how to read a download."""

    name: str
    version: int
    url: Callable[[SeriesConfig], str]
    #: The dated rows, and True when the download states a daily frequency for them.
    parse: Callable[[bytes, SeriesConfig], tuple[dict[date, Row], bool]]
    check_key: Callable[[SeriesConfig], None]
    #: The extension of the kept raw file: ``_raw/{sha256}.{ext}``.
    ext: str = "csv"


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise RatesError(f"the download is not UTF-8 text ({e})") from None


def _value(text: str, where: str) -> str | None:
    v = text.strip()
    if v in _MISSING_TOKENS:
        return None
    if not _DECIMAL.match(v):
        raise RatesError(f"{where}: value {v!r} is not a decimal number")
    return v


def _put(rows: dict[date, Row], d: date, row: Row, where: str) -> None:
    if d in rows:
        raise RatesError(f"{where}: date {d.isoformat()} appears more than once")
    rows[d] = row


def _date(text: str, fmt: str, where: str) -> date:
    try:
        return datetime.strptime(text.strip(), fmt).date()
    except ValueError:
        raise RatesError(f"{where}: {text.strip()!r} is not a daily date ({fmt})") from None


def _no_key_check(cfg: SeriesConfig) -> None:
    return None


def _split_key(cfg: SeriesConfig, left: str, right: str) -> tuple[str, str]:
    a, sep, b = cfg.key.partition("/")
    if not sep or not a or not b or "/" in b:
        raise RatesError(f"{cfg.id}: a {cfg.source} key is '{left}/{right}', not {cfg.key!r}")
    return a, b


# FRED (Federal Reserve Bank of St. Louis): fredgraph.csv, header
# "observation_date,<SERIES>" (older files: "DATE,<SERIES>"), "." or "" for no value.


def _fred_url(cfg: SeriesConfig) -> str:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(cfg.key)}"
    if cfg.start:
        url += f"&cosd={cfg.start.isoformat()}"
    return url


def _fred_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    lines = list(csv.reader(io.StringIO(_text(body))))
    if not lines or len(lines[0]) != 2 or lines[0][0].strip() not in ("observation_date", "DATE"):
        raise RatesError("fred: the header is not 'observation_date,<series>'")
    if lines[0][1].strip() != cfg.key:
        raise RatesError(f"fred: the column is {lines[0][1].strip()!r}, not {cfg.key!r}")
    rows: dict[date, Row] = {}
    for n, line in enumerate(lines[1:], 2):
        if not line:
            continue
        where = f"fred line {n}"
        if len(line) != 2:
            raise RatesError(f"{where}: {len(line)} fields, not 2")
        d = _date(line[0], "%Y-%m-%d", where)
        _put(rows, d, Row(_value(line[1], where), None, None), where)
    return rows, False


# ECB Data Portal: service/data/{flow}/{series key}?format=csvdata, one row per
# observation with TIME_PERIOD, OBS_VALUE, OBS_STATUS ("M": missing) and UNIT_MEASURE.


def _ecb_url(cfg: SeriesConfig) -> str:
    flow, series = _split_key(cfg, "flow", "series key")
    url = f"https://data-api.ecb.europa.eu/service/data/{quote(flow)}/{quote(series)}"
    url += "?format=csvdata"
    if cfg.start:
        url += f"&startPeriod={cfg.start.isoformat()}"
    return url


def _ecb_check(cfg: SeriesConfig) -> None:
    _split_key(cfg, "flow", "series key")


def _ecb_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    flow, series = _split_key(cfg, "flow", "series key")
    reader = csv.DictReader(io.StringIO(_text(body)))
    need = {"KEY", "FREQ", "TIME_PERIOD", "OBS_VALUE"}
    if reader.fieldnames is None or not need <= set(reader.fieldnames):
        raise RatesError(f"ecb: the header lacks {', '.join(sorted(need))}")
    rows: dict[date, Row] = {}
    for n, rec in enumerate(reader, 2):
        where = f"ecb line {n}"
        if rec["KEY"] != f"{flow}.{series}":
            raise RatesError(f"{where}: series {rec['KEY']!r}, not {flow}.{series!r}")
        if rec["FREQ"] not in ("D", "B"):
            raise RatesError(f"{where}: frequency {rec['FREQ']!r} is not daily (D or B)")
        d = _date(rec["TIME_PERIOD"], "%Y-%m-%d", where)
        value = None if rec.get("OBS_STATUS") == "M" else _value(rec["OBS_VALUE"], where)
        unit = (rec.get("UNIT_MEASURE") or "").strip() or None
        _put(rows, d, Row(value, unit, None), where)
    return rows, True


# Bank of England database (IADB): _iadb-fromshowcolumns.asp CSV, header "DATE,<CODE>",
# dates "DD Mon YYYY".


def _boe_url(cfg: SeriesConfig) -> str:
    start = (cfg.start or date(1950, 1, 1)).strftime("%d/%b/%Y")
    return (
        "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
        f"?csv.x=yes&Datefrom={start}&Dateto=now&SeriesCodes={quote(cfg.key)}"
        "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
    )


def _boe_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    lines = list(csv.reader(io.StringIO(_text(body))))
    if not lines or len(lines[0]) != 2 or lines[0][0].strip() != "DATE":
        raise RatesError("boe: the header is not 'DATE,<series code>'")
    if lines[0][1].strip() != cfg.key:
        raise RatesError(f"boe: the column is {lines[0][1].strip()!r}, not {cfg.key!r}")
    rows: dict[date, Row] = {}
    for n, line in enumerate(lines[1:], 2):
        if not line:
            continue
        where = f"boe line {n}"
        if len(line) != 2:
            raise RatesError(f"{where}: {len(line)} fields, not 2")
        d = _date(line[0], "%d %b %Y", where)
        _put(rows, d, Row(_value(line[1], where), None, None), where)
    return rows, False


# Bank of Japan time-series data search API: getDataCode?format=csv&lang=en, a
# preamble (STATUS, NEXTPOSITION, ...) then SERIES_CODE, ..., UNIT, FREQUENCY, ...,
# LAST_UPDATE (YYYYMMDD), SURVEY_DATES (YYYYMMDD for daily), VALUES.


def _boj_url(cfg: SeriesConfig) -> str:
    db, code = _split_key(cfg, "database", "series code")
    url = (
        "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
        f"?format=csv&lang=en&db={quote(db)}&code={quote(code)}"
    )
    if cfg.start:
        url += f"&startDate={cfg.start.strftime('%Y%m')}"
    return url


def _boj_check(cfg: SeriesConfig) -> None:
    _split_key(cfg, "database", "series code")


def _boj_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    _, code = _split_key(cfg, "database", "series code")
    lines = list(csv.reader(io.StringIO(_text(body))))
    header_at = next((i for i, ln in enumerate(lines) if ln and ln[0] == "SERIES_CODE"), None)
    if header_at is None:
        raise RatesError("boj: no SERIES_CODE header")
    pre = {ln[0]: ln[1:] for ln in lines[:header_at] if ln}
    if pre.get("STATUS", [""])[0].strip() != "200":
        raise RatesError(f"boj: the response status is {pre.get('STATUS')!r}, not 200")
    if any(v.strip() for v in pre.get("NEXTPOSITION", [])):
        raise RatesError("boj: the response is paged (NEXTPOSITION is set); set a later start")
    header = lines[header_at]
    need = ("SERIES_CODE", "UNIT", "FREQUENCY", "LAST_UPDATE", "SURVEY_DATES", "VALUES")
    missing = [c for c in need if c not in header]
    if missing:
        raise RatesError(f"boj: the header lacks {', '.join(missing)}")
    col = {c: header.index(c) for c in need}
    rows: dict[date, Row] = {}
    for n, line in enumerate(lines[header_at + 1 :], header_at + 2):
        if not line:
            continue
        where = f"boj line {n}"
        if len(line) != len(header):
            raise RatesError(f"{where}: {len(line)} fields, not {len(header)}")
        if line[col["SERIES_CODE"]] != code:
            raise RatesError(f"{where}: series {line[col['SERIES_CODE']]!r}, not {code!r}")
        if line[col["FREQUENCY"]].strip().upper() != "DAILY":
            raise RatesError(f"{where}: frequency {line[col['FREQUENCY']]!r} is not DAILY")
        d = _date(line[col["SURVEY_DATES"]], "%Y%m%d", where)
        published = _date(line[col["LAST_UPDATE"]], "%Y%m%d", where).isoformat()
        unit = line[col["UNIT"]].strip() or None
        _put(rows, d, Row(_value(line[col["VALUES"]], where), unit, published), where)
    return rows, True


# Spreadsheet workbooks (.xls, .xlsx): each sheet becomes a grid of cells. A cell is
# None when it is empty.


@dataclass(frozen=True)
class Cell:
    """One workbook cell (or one CSV field, as ``s``).

    ``kind`` is ``s`` (text), ``n`` (a number, as decimal text), ``d`` (a date, as ISO
    text), ``b`` (a boolean) or ``e`` (an error, such as ``#N/A``).
    """

    kind: str
    text: str


Grid = list[list[Cell | None]]


@dataclass(frozen=True)
class Workbook:
    """The sheets of a workbook, by name, in workbook order.

    ``epoch`` is the date of serial day 0 when date cells are plain numbers (``.xlsx``),
    None when the reader gives date cells as ``d`` (``.xls``).
    """

    sheets: dict[str, Grid]
    epoch: date | None


def _label(cell: Cell | None) -> str:
    return cell.text.strip() if cell is not None and cell.kind == "s" else ""


def _float_text(x: float, where: str) -> str:
    """A stored double as text: the shortest decimal that reads back as ``x``, positional."""
    d = Decimal(repr(x))
    if not d.is_finite():
        raise RatesError(f"{where}: the number {x!r} is not finite")
    text = format(d, "f")
    return text[:-2] if text.endswith(".0") else text


def _number_text(text: str, where: str) -> str:
    """An ``.xlsx`` number's stored text, kept; an exponent form is written out positionally."""
    t = text.strip()
    if _DECIMAL.match(t):
        return t
    try:
        d = Decimal(t)
    except InvalidOperation:
        raise RatesError(f"{where}: the number {t!r} is not a decimal number") from None
    if not d.is_finite():
        raise RatesError(f"{where}: the number {t!r} is not finite")
    return format(d, "f")


def _cell_value(cell: Cell | None, where: str) -> str | None:
    """A cell's value text, or None when it has none (empty, a missing marker, an error)."""
    if cell is None or cell.kind == "e":
        return None
    if cell.kind == "n":
        return cell.text
    if cell.kind == "s":
        return _value(cell.text, where)
    raise RatesError(f"{where}: a {'date' if cell.kind == 'd' else 'boolean'} cell is not a value")


def _cell_date(cell: Cell, where: str, fmt: str | None, epoch: date | None) -> date:
    """A cell's date: a date cell, a serial day number after ``epoch``, or text in ``fmt``."""
    if cell.kind == "d":
        try:
            dt = datetime.fromisoformat(cell.text)
        except ValueError:
            raise RatesError(f"{where}: {cell.text!r} is not a date") from None
        if dt.time() != time():
            raise RatesError(f"{where}: {cell.text!r} is not a daily date (it has a time)")
        return dt.date()
    if cell.kind == "n" and epoch is not None:
        serial = Decimal(cell.text)
        if serial != serial.to_integral_value():
            raise RatesError(f"{where}: date serial {cell.text} is not a daily date")
        if epoch.year == 1899 and serial < 61:
            raise RatesError(f"{where}: date serial {cell.text} is before 1900-03-01")
        return epoch + timedelta(days=int(serial))
    if cell.kind == "s" and fmt is not None:
        return _date(cell.text, fmt, where)
    raise RatesError(f"{where}: {cell.text!r} is not a date")


def _sheet(book: Workbook, name: str, source: str) -> Grid:
    """The sheet called ``name``; a workbook without one gives its first sheet."""
    if name in book.sheets:
        return book.sheets[name]
    if not book.sheets:
        raise RatesError(f"{source}: the workbook has no sheet")
    return next(iter(book.sheets.values()))


def _read_xls(body: bytes) -> Workbook:
    """An ``.xls`` (BIFF) workbook. A number has no stored text: see :func:`_float_text`."""
    try:
        import xlrd
    except ImportError:
        raise RatesError(
            "reading .xls needs xlrd: pip install 'tradedesk-marketdata[xls]'"
        ) from None
    try:
        book = xlrd.open_workbook(file_contents=body, logfile=io.StringIO())
    except Exception as e:  # untrusted bytes: any reader failure means it does not parse
        raise RatesError(f"the download is not an .xls workbook ({e})") from None
    sheets: dict[str, Grid] = {}
    for sh in book.sheets():
        grid: Grid = []
        for i in range(sh.nrows):
            line: list[Cell | None] = []
            for j, c in enumerate(sh.row(i)):
                where = f"sheet {sh.name!r} row {i + 1} column {j + 1}"
                if c.ctype == xlrd.XL_CELL_TEXT:
                    line.append(Cell("s", c.value))
                elif c.ctype == xlrd.XL_CELL_NUMBER:
                    line.append(Cell("n", _float_text(c.value, where)))
                elif c.ctype == xlrd.XL_CELL_DATE:
                    try:
                        dt = xlrd.xldate.xldate_as_datetime(c.value, book.datemode)
                    except xlrd.xldate.XLDateError as e:
                        raise RatesError(f"{where}: not a date ({e!r})") from None
                    line.append(Cell("d", dt.isoformat()))
                elif c.ctype == xlrd.XL_CELL_BOOLEAN:
                    line.append(Cell("b", str(bool(c.value)).lower()))
                elif c.ctype == xlrd.XL_CELL_ERROR:
                    line.append(Cell("e", xlrd.error_text_from_code.get(c.value, "#ERR")))
                else:
                    line.append(None)
            grid.append(line)
        sheets[sh.name] = grid
    return Workbook(sheets, None)


_XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XL_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _xlsx_text(el: ET.Element) -> str:
    """The text of a shared or inline string: its ``t``, or its rich-text runs' ``t``."""
    t = el.find(f"{_XL}t")
    if t is not None:
        return t.text or ""
    return "".join(rt.text or "" for r in el.findall(f"{_XL}r") for rt in r.findall(f"{_XL}t"))


def _xlsx_cell(c: ET.Element, shared: list[str], where: str) -> Cell | None:
    kind = c.get("t", "n")
    v = c.find(f"{_XL}v")
    if kind == "inlineStr":
        is_ = c.find(f"{_XL}is")
        return Cell("s", _xlsx_text(is_)) if is_ is not None else None
    if v is None or v.text is None:
        return None
    if kind == "s":
        try:
            return Cell("s", shared[int(v.text)])
        except (ValueError, IndexError):
            raise RatesError(f"{where}: shared string {v.text!r} does not exist") from None
    if kind == "str":
        return Cell("s", v.text)
    if kind == "n":
        return Cell("n", _number_text(v.text, where))
    if kind in ("d", "b", "e"):
        return Cell(kind, v.text)
    raise RatesError(f"{where}: cell type {kind!r} is not known")


def _column(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - ord("A") + 1
    return n - 1


def _xlsx_grid(root: ET.Element, shared: list[str], name: str) -> Grid:
    grid: Grid = []
    data = root.find(f"{_XL}sheetData")
    for row in data if data is not None else []:
        if row.tag != f"{_XL}row":
            continue
        i = int(row.get("r", len(grid) + 1)) - 1
        while len(grid) <= i:
            grid.append([])
        line = grid[i]
        for c in row.findall(f"{_XL}c"):
            ref = _CELL_REF.match(c.get("r", ""))
            j = _column(ref[1]) if ref else len(line)
            while len(line) <= j:
                line.append(None)
            line[j] = _xlsx_cell(c, shared, f"sheet {name!r} row {i + 1} column {j + 1}")
    return grid


def _read_xlsx(body: bytes) -> Workbook:
    """An ``.xlsx`` (Office Open XML) workbook, read with the standard library.

    A number keeps its stored ``<v>`` text, so the value is the publisher's decimal text.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_xlsx_text(si) for si in sst.findall(f"{_XL}si")]
        targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(_PKG_REL)}
        pr = wb.find(f"{_XL}workbookPr")
        date1904 = pr is not None and pr.get("date1904", "false").lower() in ("1", "true")
        sheets: dict[str, Grid] = {}
        sheets_el = wb.find(f"{_XL}sheets")
        for sh in sheets_el if sheets_el is not None else []:
            name = sh.get("name", "")
            target = targets.get(sh.get(_XL_REL), "")
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            sheets[name] = _xlsx_grid(ET.fromstring(z.read(path)), shared, name)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as e:
        if isinstance(e, RatesError):
            raise
        raise RatesError(f"the download is not an .xlsx workbook ({e})") from None
    return Workbook(sheets, date(1904, 1, 1) if date1904 else date(1899, 12, 30))


# Reserve Bank of Australia statistical tables: a title line, metadata rows (Title,
# Description, Frequency, Type, Units, Source, Publication date, Series ID) and one
# row per date. A short row has no value for the columns it does not reach. The same
# layout comes as CSV (tables/csv/{table}-data.csv, dates "DD-Mon-YYYY") and as an
# .xls workbook with date cells (the historical files under tables/xls-hist/).
# A workbook is one whole file, so for rba-xls (and rbnz) the configured start drops
# the rows dated before it; the rba CSV reads every row, as it always has.


def _rba_table(
    grid: Grid, series: str, source: str, fmt: str | None, start: date | None = None
) -> tuple[dict[date, Row], bool]:
    """The rows of one RBA table column. A row dated before ``start`` is not read."""
    meta: dict[str, list[Cell | None]] = {}
    data_at = None
    for i, ln in enumerate(grid):
        name = _label(ln[0]) if ln else ""
        if name == "Series ID":
            meta[name] = ln
            data_at = i + 1
            break
        if name:
            meta.setdefault(name, ln)
    if data_at is None:
        raise RatesError(f"{source}: no 'Series ID' row")
    ids = [_label(c) for c in meta["Series ID"]]
    if series not in ids[1:]:
        raise RatesError(f"{source}: series ID {series!r} is not in the table")
    c = ids.index(series)

    def cell(row_name: str) -> Cell | None:
        r = meta.get(row_name)
        x = r[c] if r is not None and c < len(r) else None
        return x if x is not None and x.text.strip() else None

    freq = cell("Frequency")
    frequency = freq.text.strip() if freq is not None else None
    if frequency is not None and frequency.lower() != "daily":
        raise RatesError(f"{source}: series {series} has frequency {frequency!r}, not Daily")
    units = cell("Units")
    unit = units.text.strip() if units is not None else None
    pub = cell("Publication date")
    published = (
        _cell_date(pub, f"{source} Publication date", fmt, None).isoformat() if pub else None
    )
    rows: dict[date, Row] = {}
    for n, line in enumerate(grid[data_at:], data_at + 1):
        if not line or line[0] is None or not line[0].text.strip():
            continue
        where = f"{source} line {n}"
        d = _cell_date(line[0], where, fmt, None)
        if start is not None and d < start:
            continue
        value = _cell_value(line[c], where) if c < len(line) else None
        _put(rows, d, Row(value, unit, published), where)
    return rows, frequency is not None


def _rba_url(cfg: SeriesConfig) -> str:
    table, _ = _split_key(cfg, "table", "series ID")
    return f"https://www.rba.gov.au/statistics/tables/csv/{quote(table.lower())}-data.csv"


def _rba_check(cfg: SeriesConfig) -> None:
    _split_key(cfg, "table", "series ID")


def _rba_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    _, series = _split_key(cfg, "table", "series ID")
    grid: Grid = [[Cell("s", x) for x in ln] for ln in csv.reader(io.StringIO(_text(body)))]
    return _rba_table(grid, series, "rba", "%d-%b-%Y")


def _rba_xls_url(cfg: SeriesConfig) -> str:
    file, _ = _split_key(cfg, "file", "series ID")
    return f"https://www.rba.gov.au/statistics/tables/xls-hist/{quote(file.lower())}.xls"


def _rba_xls_check(cfg: SeriesConfig) -> None:
    _split_key(cfg, "file", "series ID")


def _rba_xls_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    _, series = _split_key(cfg, "file", "series ID")
    grid = _sheet(_read_xls(body), "Data", "rba-xls")
    return _rba_table(grid, series, "rba-xls", "%d-%b-%Y", cfg.start)


# Reserve Bank of New Zealand statistical tables: an .xlsx workbook. The "Data" sheet has
# header rows (group, series, "Notes", "Unit", "Series Id"; column A names each row),
# then one row per date with a date cell in column A. The "Table Description" sheet
# states the "Published Date". No frequency is stated, so the dates must be daily.

#: The file names whose default URL is known: "h{letter}{number}..." as in "hb2-daily".
_RBNZ_FILE = re.compile(r"^h([a-z])(\d+)[a-z0-9-]*$")


def _rbnz_url(cfg: SeriesConfig) -> str:
    file, _ = _split_key(cfg, "file", "Series Id")
    m = _RBNZ_FILE.match(file.lower())
    if m is None:
        raise RatesError(f"{cfg.id}: no default URL for rbnz file {file!r}; set 'url'")
    return (
        "https://www.rbnz.govt.nz/-/media/project/sites/rbnz/files/statistics/series/"
        f"{m[1]}/{m[1]}{m[2]}/{quote(file.lower())}.xlsx"
    )


def _rbnz_check(cfg: SeriesConfig) -> None:
    _split_key(cfg, "file", "Series Id")
    if cfg.url is None:
        _rbnz_url(cfg)


def _rbnz_parse(body: bytes, cfg: SeriesConfig) -> tuple[dict[date, Row], bool]:
    _, series = _split_key(cfg, "file", "Series Id")
    book = _read_xlsx(body)
    grid = _sheet(book, "Data", "rbnz")
    head = None
    unit_row: list[Cell | None] = []
    for i, ln in enumerate(grid):
        name = _label(ln[0]).lower() if ln else ""
        if name == "unit":
            unit_row = ln
        if name == "series id":
            head = i
            break
    if head is None:
        raise RatesError("rbnz: no 'Series Id' row")
    ids = [_label(c) for c in grid[head]]
    if series not in ids[1:]:
        raise RatesError(f"rbnz: series Id {series!r} is not in the table")
    c = ids.index(series)
    unit = (_label(unit_row[c]) or None) if c < len(unit_row) else None
    published = None
    for ln in book.sheets.get("Table Description", []):
        if ln and _label(ln[0]).lower() == "published date" and len(ln) > 1 and ln[1]:
            published = _cell_date(ln[1], "rbnz Published Date", None, book.epoch).isoformat()
            break
    rows: dict[date, Row] = {}
    for n, line in enumerate(grid[head + 1 :], head + 2):
        if not line or line[0] is None or not line[0].text.strip():
            continue
        where = f"rbnz row {n}"
        d = _cell_date(line[0], where, None, book.epoch)
        if cfg.start is not None and d < cfg.start:
            continue
        value = _cell_value(line[c], where) if c < len(line) else None
        _put(rows, d, Row(value, unit, published), where)
    return rows, False


FORMATS: dict[str, Format] = {
    "fred": Format("fred", 1, _fred_url, _fred_parse, _no_key_check),
    "ecb": Format("ecb", 1, _ecb_url, _ecb_parse, _ecb_check),
    "boe": Format("boe", 1, _boe_url, _boe_parse, _no_key_check),
    "boj": Format("boj", 1, _boj_url, _boj_parse, _boj_check),
    "rba": Format("rba", 1, _rba_url, _rba_parse, _rba_check),
    "rba-xls": Format("rba-xls", 1, _rba_xls_url, _rba_xls_parse, _rba_xls_check, "xls"),
    "rbnz": Format("rbnz", 1, _rbnz_url, _rbnz_parse, _rbnz_check, "xlsx"),
}


#: The largest median step, in days, between the dated rows of a daily download: a
#: weekend plus a one-day holiday. A weekly series steps 7 days, a monthly one 28 to 31.
DAILY_MAX_MEDIAN_STEP = 4


def _check_daily(rows: dict[date, Row], source: str) -> None:
    """Refuse rows whose dates are not a daily series, for a download that states no frequency."""
    days = sorted(rows)
    if len(days) < 2:
        raise RatesError(
            f"{source}: one dated row does not show a daily series; set an earlier start"
        )
    median = statistics.median((b - a).days for a, b in zip(days, days[1:], strict=False))
    if median > DAILY_MAX_MEDIAN_STEP:
        raise RatesError(
            f"{source}: the dates are a median {median:g} days apart, so the series is not daily"
        )


def parse(body: bytes, cfg: SeriesConfig) -> dict[date, Row]:
    """The dated rows of one download. Raises :class:`RatesError` when it does not parse.

    A download that states no daily frequency must be daily by its dates
    (:func:`_check_daily`), so it cannot let a weekly or monthly series in. One that
    states a daily frequency is daily, even with one dated row.
    """
    rows, daily_stated = FORMATS[cfg.source].parse(body, cfg)
    if not any(r.value is not None for r in rows.values()):
        raise RatesError(f"{cfg.source}: the download holds no value")
    if not daily_stated:
        _check_daily(rows, cfg.source)
    return rows


# ---------------------------------------------------------------------------
# Building the parsed files from the fetches


@dataclass(frozen=True)
class Fetch:
    """One line of ``_fetches.jsonl``. ``http_status`` is None for a ``file:`` URL."""

    fetched_at: str
    url: str
    http_status: int | None
    sha256: str
    bytes: int
    raw: str

    def as_json(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "url": self.url,
            "http_status": self.http_status,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class Built:
    """The three files a series' fetches make, as bytes."""

    observations: bytes
    gaps: bytes
    manifest: bytes
    rows: int
    gap_rows: int
    revised: int


def _expected(d: date, calendar: str) -> bool:
    return calendar == "all" or d.weekday() < 5


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """The manifest's canonical form: sorted keys, two-space indent, one final newline."""
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def build(cfg: SeriesConfig, fetches: Sequence[tuple[Fetch, bytes]]) -> Built:
    """Make ``observations.csv``, ``gaps.csv`` and ``manifest.json`` from the fetches, in order.

    A later fetch wins for each date it lists. A date an earlier fetch had a value for,
    inside the later fetch's span but without a value there, is removed and flagged
    ``withdrawn``. Dates before or after a later fetch's span keep their earlier rows.
    """
    if not fetches:
        raise RatesError(f"{cfg.id}: no fetch to build from")
    merged: dict[date, Row] = {}
    seen: dict[date, set[str]] = {}
    marked_missing: set[date] = set()
    for _, body in fetches:
        rows = parse(body, cfg)
        lo, hi = min(rows), max(rows)
        for d in [d for d in merged if lo <= d <= hi and d not in rows]:
            del merged[d]
        marked_missing = {d for d in marked_missing if not lo <= d <= hi}
        for d, row in rows.items():
            if row.value is None:
                merged.pop(d, None)
                marked_missing.add(d)
                continue
            unit = row.unit or cfg.unit
            if unit is None:
                raise RatesError(f"{cfg.id}: the source states no unit; set 'unit' for the series")
            if row.unit and cfg.unit and row.unit != cfg.unit:
                raise RatesError(
                    f"{cfg.id}: the source states unit {row.unit!r}, the configuration {cfg.unit!r}"
                )
            merged[d] = Row(row.value, unit, row.published)
            seen.setdefault(d, set()).add(row.value)

    if not merged:
        raise RatesError(f"{cfg.id}: the fetches hold no value")
    obs = sorted(merged.items())
    first, last = obs[0][0], obs[-1][0]
    revised = sum(1 for d, _ in obs if len(seen[d]) > 1)
    obs_bytes = _csv_bytes(
        ("date", "value", "unit", "published", "revised"),
        [
            (
                d.isoformat(),
                r.value or "",
                r.unit or "",
                r.published or "",
                "true" if len(seen[d]) > 1 else "false",
            )
            for d, r in obs
        ],
    )

    absent: dict[date, str] = {}
    d = first
    while d <= last:
        if d not in merged and _expected(d, cfg.calendar):
            absent[d] = "no_row"
        d += timedelta(days=1)
    for d in marked_missing:
        if d not in merged:
            absent[d] = "missing_in_source"
    for d in seen:
        if d not in merged:
            absent[d] = "withdrawn"
    gaps_bytes = _csv_bytes(
        ("date", "reason"), [(d.isoformat(), absent[d]) for d in sorted(absent)]
    )

    fmt = FORMATS[cfg.source]
    manifest = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "series": cfg.as_json(),
        "parser": {"format": fmt.name, "version": fmt.version},
        "fetches": [f.as_json() for f, _ in fetches],
        "observations": {
            "path": OBSERVATIONS,
            "sha256": _sha(obs_bytes),
            "rows": len(obs),
            "first": first.isoformat(),
            "last": last.isoformat(),
            "revised": revised,
        },
        "gaps": {"path": GAPS, "sha256": _sha(gaps_bytes), "rows": len(absent)},
    }
    return Built(obs_bytes, gaps_bytes, manifest_bytes(manifest), len(obs), len(absent), revised)


# ---------------------------------------------------------------------------
# The store


def series_dir(store: Path, series_id: str) -> Path:
    if not _ID.match(series_id):
        raise RatesError(f"series id {series_id!r} must match {_ID.pattern}")
    return store / AREA / series_id


def store_series(store: Path) -> list[str]:
    """The series present in a store's reference-data area (directories with a manifest)."""
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
                    None if d["http_status"] is None else int(d["http_status"]),
                    d["sha256"],
                    int(d["bytes"]),
                    d["raw"],
                )
            )
        except (ValueError, KeyError, TypeError) as e:
            raise RatesError(f"{path} line {n}: not a fetch record ({e})") from None
    return out


def _read_raw(sdir: Path, f: Fetch) -> bytes:
    path = sdir / f.raw
    if not path.is_file():
        raise RatesError(f"{f.raw}: the raw file is missing")
    body = path.read_bytes()
    if _sha(body) != f.sha256:
        raise RatesError(f"{f.raw}: sha256 {_sha(body)} does not match the record {f.sha256}")
    return body


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stored_config(sdir: Path) -> SeriesConfig | None:
    path = sdir / MANIFEST
    if not path.exists():
        return None
    try:
        return SeriesConfig.from_json(json.loads(path.read_text(encoding="utf-8"))["series"])
    except (ValueError, KeyError, TypeError) as e:
        raise RatesError(f"{path}: not a series manifest ({e})") from None


# ---------------------------------------------------------------------------
# Fetch


@dataclass(frozen=True)
class Response:
    """One successful download: the HTTP status (None for a ``file:`` URL) and the body."""

    status: int | None
    body: bytes


def download(
    url: str,
    *,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
) -> Response:
    """GET one URL with the package's backoff. Raises :class:`RatesError` after ``retries``.

    A ``file:`` URL reads a local file instead: a publisher's file that a person
    downloaded, for a site that refuses scripted requests. It has no HTTP status.
    """
    if urlparse(url).scheme == "file":
        path = Path(url2pathname(urlparse(url).path))
        try:
            return Response(None, path.read_bytes())
        except OSError as e:
            raise RatesError(f"{url}: {e}") from None
    last_exc: Exception | None = None
    delay = RETRY_BASE_DELAY
    for attempt in range(1, retries + 1):
        check_cancelled()
        try:
            with _SESSION.get(url, timeout=timeout) as r:
                r.raise_for_status()
                return Response(r.status_code, r.content)
        except Exception as e:
            last_exc = e
            log.debug("rates attempt %d/%d failed for %s: %s", attempt, retries, url, e)
            if attempt < retries:
                if cancellation.wait(delay):
                    raise KeyboardInterrupt() from None
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)
    raise RatesError(f"{url}: {retries} failed attempt(s) ({last_exc})")


@dataclass
class FetchResult:
    """What one series' fetch did. ``status`` is ``updated``, ``unchanged`` or ``failed``."""

    id: str
    status: str
    sha256: str | None = None
    rows: int | None = None
    gaps: int | None = None
    revised: int | None = None
    manifest_sha256: str | None = None
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def fetch_series(
    store: Path,
    cfg: SeriesConfig,
    *,
    get: Callable[[str], Response] | None = None,
    now: Callable[[], str] = _now,
) -> FetchResult:
    """Download one series, keep the raw body and rebuild its parsed files and manifest.

    Every earlier raw file is checked against its sha256 and the new body is parsed
    before anything is written. The series' source and key must match the stored
    manifest's: another series cannot be written under the same id.
    """
    sdir = series_dir(store, cfg.id)
    prior_cfg = stored_config(sdir)
    if prior_cfg is not None and (prior_cfg.source, prior_cfg.key) != (cfg.source, cfg.key):
        raise RatesError(
            f"{cfg.id}: the store holds {prior_cfg.source} {prior_cfg.key!r} under this id, "
            f"not {cfg.source} {cfg.key!r}"
        )
    prior = [(f, _read_raw(sdir, f)) for f in read_fetches(sdir)]
    url = cfg.resolved_url()
    fetched_at = now()
    resp = (get or download)(url)
    body = resp.body
    parse(body, cfg)
    sha = _sha(body)

    new = (
        None
        if prior and prior[-1][0].sha256 == sha
        else Fetch(
            fetched_at, url, resp.status, sha, len(body), f"{RAW}/{sha}.{FORMATS[cfg.source].ext}"
        )
    )
    fetches = prior if new is None else [*prior, (new, body)]
    built = build(cfg, fetches)
    files = ((OBSERVATIONS, built.observations), (GAPS, built.gaps), (MANIFEST, built.manifest))
    result = FetchResult(
        cfg.id, "updated", sha, built.rows, built.gap_rows, built.revised, _sha(built.manifest)
    )
    if new is None and all(
        (sdir / name).is_file() and (sdir / name).read_bytes() == data for name, data in files
    ):
        result.status = "unchanged"
        return result

    if new is not None:
        raw_path = sdir / new.raw
        if not raw_path.exists():
            _write_atomic(raw_path, body)
        with (sdir / FETCHES).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(new.as_json(), sort_keys=True) + "\n")
    for name, data in files:
        _write_atomic(sdir / name, data)
    return result


def fetch(
    store: Path,
    configs: Sequence[SeriesConfig],
    *,
    get: Callable[[str], Response] | None = None,
    now: Callable[[], str] = _now,
) -> list[FetchResult]:
    """Fetch each series in turn; a failed series is reported and the next one goes on."""
    out = []
    for cfg in configs:
        try:
            out.append(fetch_series(store, cfg, get=get, now=now))
        except RatesError as e:
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
            except RatesError as e:
                rep.findings.append(str(e))
        if rep.findings:
            return rep
        built = build(cfg, bodies)
    except RatesError as e:
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
        lines.append("no series in the store's reference-data area")
    return "\n".join(lines) + "\n"
