"""Release calendars (dated events) in the store's reference-data area.

A calendar is named by the caller's configuration (``[[calendar]]`` tables in a
TOML file, see ``calendars.example.toml``); this module ships no list of
publishers. A calendar is one publisher's list of dated events (a policy
decision, a statistics release), read from one of two formats:

- ``ics``: an iCalendar (RFC 5545) file that the publisher offers.
- ``csv``: a curated file: a person writes the events from the publisher's
  pages into a delimited file with a header row. The calendar can name those
  ``pages``; each is downloaded and kept raw beside the file.

Each calendar lives in ``{store}/_reference/calendars/{id}/``, beside the rate
and dividend series:

- ``_raw/{sha256}.{ext}``: every downloaded body, content-addressed. Never
  changed or deleted.
- ``_fetches.jsonl``: one line per kept download: ``fetched_at`` (UTC),
  ``url``, ``http_status``, ``sha256``, ``bytes``, ``raw`` (the path under the
  calendar directory) and ``input`` (``events`` for the parsed file, ``page``
  for a kept source page).
- ``events.csv``: one row per event: ``date``, ``time`` (``HH:MM``, empty
  when the source states no time of day), ``timezone`` (the zone of the date
  and time), ``institution``, ``type``, ``title``, ``source_url`` and
  ``raw_sha256`` (the download the event was last read from) and ``revised``.
- ``withdrawn.csv``: the events that an earlier download listed and a later
  one, over the same dates, does not.
- ``manifest.json``: the configuration, the parser, every fetch and the sha256
  of the two parsed files. It holds no write time and no tool version, so its
  sha256 pins the calendar.

:func:`build` makes the three files from the raw files alone, so
:func:`verify_calendar` can reproduce them byte for byte. A download that does
not parse is refused before anything is written.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import rates as _rates
from .rates import Response, _csv_bytes, _sha, _write_atomic, manifest_bytes
from .source import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, DEFAULT_RETRIES

log = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1

#: The reference-data area of a store, relative to the store root.
AREA = Path("_reference") / "calendars"

EVENTS = "events.csv"
WITHDRAWN = "withdrawn.csv"
MANIFEST = "manifest.json"
FETCHES = "_fetches.jsonl"
RAW = "_raw"

#: The ``input`` of a fetch record: the parsed file, or a kept source page.
EVENTS_INPUT = "events"
PAGE_INPUT = "page"

EVENTS_HEADER = (
    "date",
    "time",
    "timezone",
    "institution",
    "type",
    "title",
    "source_url",
    "raw_sha256",
    "revised",
)
WITHDRAWN_HEADER = ("date", "time", "timezone", "type", "title", "listed_sha256", "dropped_sha256")

_ID = _rates._ID


class CalendarError(ValueError):
    """A configuration, a download or a store that cannot be used as it is."""


# ---------------------------------------------------------------------------
# Configuration


#: The options of the ``csv`` format, with their defaults (None: required or unset).
CSV_OPTIONS: dict[str, Any] = {
    "date_column": None,
    "title_column": None,
    "time_column": None,
    "timezone_column": None,
    "type_column": None,
    "date_format": "%Y-%m-%d",
    "time_format": "%H:%M",
    "encoding": "utf-8-sig",
    "delimiter": ",",
    "skip_lines": 0,
}
_CSV_REQUIRED = ("date_column", "title_column")


@dataclass(frozen=True)
class TypeRule:
    """An event whose title matches ``match`` (a regular expression) has type ``type``."""

    type: str
    match: str

    def as_json(self) -> dict[str, str]:
        return {"type": self.type, "match": self.match}


@dataclass(frozen=True)
class CalendarConfig:
    """One ``[[calendar]]`` table of the caller's configuration."""

    id: str
    institution: str
    timezone: str
    source: str
    url: str
    #: The ``csv`` format's options, as sorted (name, value) pairs.
    options: tuple[tuple[str, Any], ...] = ()
    #: The one type of every event, or None.
    type: str | None = None
    #: The type rules, in order; the first that matches an event's title wins.
    types: tuple[TypeRule, ...] = ()
    #: The publisher pages to keep raw beside the events file.
    pages: tuple[str, ...] = ()
    #: An iCalendar file's own TZID names mapped to IANA names, as sorted pairs.
    tzids: tuple[tuple[str, str], ...] = ()

    def option(self, name: str) -> Any:
        return dict(self.options).get(name, CSV_OPTIONS[name])

    def identity(self) -> tuple[Any, ...]:
        """What makes two configurations the same calendar: everything but the URLs."""
        return (
            self.institution,
            self.timezone,
            self.source,
            self.options,
            self.type,
            self.types,
            self.tzids,
        )

    def as_json(self) -> dict[str, Any]:
        """The configuration as its TOML table holds it: unset keys are left out."""
        d: dict[str, Any] = {
            "id": self.id,
            "institution": self.institution,
            "timezone": self.timezone,
            "source": self.source,
            "url": self.url,
            **dict(self.options),
        }
        if self.type is not None:
            d["type"] = self.type
        if self.types:
            d["types"] = [r.as_json() for r in self.types]
        if self.pages:
            d["pages"] = list(self.pages)
        if self.tzids:
            d["tzids"] = dict(self.tzids)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CalendarConfig:
        return _calendar_config(d, "manifest")


def _nonempty(d: dict[str, Any], k: str, where: str) -> str | None:
    v = d.get(k)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise CalendarError(f"{where}: {k!r} must be a non-empty string")
    return v.strip()


def _zone(name: str, where: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CalendarError(f"{where}: {name!r} is not an IANA time zone") from None
    return name


def _type_name(v: Any, where: str) -> str:
    if not isinstance(v, str) or not _ID.match(v):
        raise CalendarError(f"{where}: an event type must match {_ID.pattern}")
    return v


def _type_rules(v: Any, where: str) -> tuple[TypeRule, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or not v:
        raise CalendarError(f"{where}: 'types' must be a non-empty list of tables")
    out = []
    for i, t in enumerate(v, 1):
        at = f"{where} types #{i}"
        if not isinstance(t, dict):
            raise CalendarError(f"{at}: a type rule is a table with 'type' and 'match'")
        unknown = sorted(set(t) - {"type", "match"})
        if unknown:
            raise CalendarError(f"{at}: unknown key(s) {', '.join(unknown)}")
        match = t.get("match")
        if not isinstance(match, str) or not match:
            raise CalendarError(f"{at}: 'match' must be a non-empty regular expression")
        try:
            re.compile(match)
        except re.error as e:
            raise CalendarError(f"{at}: 'match' is not a regular expression ({e})") from None
        out.append(TypeRule(_type_name(t.get("type"), at), match))
    return tuple(out)


def _pages(v: Any, where: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(u, str) and u.strip() for u in v):
        raise CalendarError(f"{where}: 'pages' must be a list of URLs")
    pages = tuple(u.strip() for u in v)
    if len(set(pages)) != len(pages):
        raise CalendarError(f"{where}: 'pages' names a URL more than once")
    return pages


def _tzids(v: Any, source: str, where: str) -> tuple[tuple[str, str], ...]:
    if v is None:
        return ()
    if source != "ics":
        raise CalendarError(f"{where}: 'tzids' is for the ics format only")
    if not isinstance(v, dict) or not v:
        raise CalendarError(f"{where}: 'tzids' must be a table of TZID = IANA name")
    out = []
    for k, name in v.items():
        if not isinstance(name, str):
            raise CalendarError(f"{where}: tzids {k!r} must name an IANA time zone")
        out.append((k, _zone(name, f"{where} tzids {k!r}")))
    return tuple(sorted(out))


def _csv_options(d: dict[str, Any], where: str) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for k, default in CSV_OPTIONS.items():
        v = d.get(k)
        if v is None:
            if k in _CSV_REQUIRED:
                raise CalendarError(f"{where}: csv needs {k!r}")
            continue
        if k == "skip_lines":
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise CalendarError(f"{where}: 'skip_lines' must be an integer >= 0")
        elif not isinstance(v, str) or not v:
            raise CalendarError(f"{where}: {k!r} must be a non-empty string")
        elif k == "delimiter" and len(v) != 1:
            raise CalendarError(f"{where}: 'delimiter' must be one character")
        elif k == "encoding":
            try:
                "".encode(v)
            except LookupError:
                raise CalendarError(f"{where}: unknown encoding {v!r}") from None
        if v != default:
            options[k] = v
    return options


_CALENDAR_KEYS = frozenset(
    {"id", "institution", "timezone", "source", "url", "type", "types", "pages", "tzids"}
)


def _calendar_config(d: dict[str, Any], where: str) -> CalendarConfig:
    cid = _nonempty(d, "id", where)
    if cid is None:
        raise CalendarError(f"{where}: 'id' is required and must be a non-empty string")
    if not _ID.match(cid):
        raise CalendarError(f"{where}: id {cid!r} must match {_ID.pattern}")
    where = f"{where} ({cid})"
    source = _nonempty(d, "source", where)
    if source not in FORMATS:
        raise CalendarError(f"{where}: source must be one of {', '.join(FORMATS)}")
    allowed = _CALENDAR_KEYS | (set(CSV_OPTIONS) if source == "csv" else set())
    unknown = sorted(k for k in d if k not in allowed and d[k] is not None)
    if unknown:
        raise CalendarError(f"{where}: unknown key(s) {', '.join(unknown)}")
    institution = _nonempty(d, "institution", where)
    if institution is None:
        raise CalendarError(f"{where}: 'institution' is required")
    tz = _nonempty(d, "timezone", where)
    if tz is None:
        raise CalendarError(f"{where}: 'timezone' is required (an IANA name)")
    url = _nonempty(d, "url", where)
    if url is None:
        raise CalendarError(f"{where}: 'url' is required")
    options = _csv_options(d, where) if source == "csv" else {}
    one_type = None if d.get("type") is None else _type_name(d["type"], where)
    rules = _type_rules(d.get("types"), where)
    typed_by_column = "type_column" in options
    if sum((one_type is not None, bool(rules), typed_by_column)) != 1:
        raise CalendarError(
            f"{where}: give exactly one of 'type', 'types'"
            + (" or 'type_column'" if source == "csv" else "")
        )
    return CalendarConfig(
        id=cid,
        institution=institution,
        timezone=_zone(tz, where),
        source=source,
        url=url,
        options=tuple(sorted(options.items())),
        type=one_type,
        types=rules,
        pages=_pages(d.get("pages"), where),
        tzids=_tzids(d.get("tzids"), source, where),
    )


def load_config(path: Path) -> list[CalendarConfig]:
    """The ``[[calendar]]`` tables of a TOML configuration file, checked."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CalendarError(f"{path}: {e}") from None
    unknown = sorted(set(data) - {"calendar"})
    if unknown:
        raise CalendarError(f"{path}: unknown top-level key(s) {', '.join(unknown)}")
    tables = data.get("calendar")
    if not isinstance(tables, list) or not tables:
        raise CalendarError(f"{path}: no [[calendar]] table")
    out: list[CalendarConfig] = []
    seen: set[str] = set()
    for i, t in enumerate(tables, 1):
        cfg = _calendar_config(t, f"{path} [[calendar]] #{i}")
        if cfg.id in seen:
            raise CalendarError(f"{path}: calendar id {cfg.id!r} appears more than once")
        seen.add(cfg.id)
        out.append(cfg)
    return out


# ---------------------------------------------------------------------------
# Parsers: one per format


#: An event's identity in a calendar: its date, type and title.
EventKey = tuple[date, str, str]


@dataclass(frozen=True)
class Event:
    """One event of a download. ``time`` is ``HH:MM`` (``HH:MM:SS`` when the
    seconds are not zero), or empty when the source states no time of day.
    ``type`` is set by a csv type column, else by the configuration."""

    date: date
    time: str
    timezone: str
    title: str
    type: str | None = None

    @property
    def key(self) -> EventKey:
        """An event's identity in a calendar: a changed time is a revision, not a new event."""
        return (self.date, self.type or "", self.title)


@dataclass(frozen=True)
class Format:
    """A published format: how to read a download into events."""

    name: str
    version: int
    #: The raw file's extension.
    ext: str
    parse: Callable[[bytes, CalendarConfig], list[Event]]


def _time_text(t: datetime) -> str:
    return t.strftime("%H:%M:%S" if t.second else "%H:%M")


# iCalendar (RFC 5545): VEVENT components with DTSTART and SUMMARY. Long lines are
# folded (a line break and one space or tab); text values escape "\", ";", "," and new
# lines. VALARM components inside an event and VTIMEZONE components are skipped.

_ICS_FOLD = re.compile(rb"\r?\n[ \t]")
_ICS_DATE = re.compile(r"^\d{8}$")
_ICS_TIME = re.compile(r"^(\d{8}T\d{6})(Z?)$")
_ICS_UNESCAPE = re.compile(r"\\([\\;,nN])")


def _ics_property(line: str, where: str) -> tuple[str, dict[str, str], str]:
    """A content line's name, parameters and value. A quoted parameter value may hold ':'."""
    i, n = 0, len(line)
    while i < n and line[i] not in ";:":
        i += 1
    name = line[:i].upper()
    params: dict[str, str] = {}
    while i < n and line[i] == ";":
        j = line.find("=", i)
        if j < 0:
            raise CalendarError(f"{where}: a parameter without '='")
        pname = line[i + 1 : j].upper()
        i = j + 1
        if i < n and line[i] == '"':
            k = line.find('"', i + 1)
            if k < 0:
                raise CalendarError(f"{where}: an unterminated quoted parameter")
            params[pname] = line[i + 1 : k]
            i = k + 1
        else:
            k = i
            while k < n and line[k] not in ";:":
                k += 1
            params[pname] = line[i:k]
            i = k
    if i >= n or line[i] != ":":
        raise CalendarError(f"{where}: a content line without ':'")
    return name, params, line[i + 1 :]


def _ics_text(value: str) -> str:
    text = _ICS_UNESCAPE.sub(lambda m: " " if m.group(1) in "nN" else m.group(1), value)
    return " ".join(text.split())


def _ics_event(
    props: dict[str, tuple[dict[str, str], str]], cfg: CalendarConfig, where: str
) -> Event:
    for name in ("RRULE", "RDATE"):
        if name in props:
            raise CalendarError(f"{where}: a recurring event ({name}) is not read; list each date")
    if "DTSTART" not in props:
        raise CalendarError(f"{where}: an event without DTSTART")
    title = _ics_text(props.get("SUMMARY", ({}, ""))[1])
    if not title:
        raise CalendarError(f"{where}: an event without SUMMARY")
    params, value = props["DTSTART"]
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or _ICS_DATE.match(value):
        try:
            return Event(datetime.strptime(value, "%Y%m%d").date(), "", cfg.timezone, title)
        except ValueError:
            raise CalendarError(f"{where}: DTSTART {value!r} is not a date") from None
    m = _ICS_TIME.match(value)
    if m is None:
        raise CalendarError(f"{where}: DTSTART {value!r} is not a date or a date-time")
    try:
        t = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        raise CalendarError(f"{where}: DTSTART {value!r} is not a date-time") from None
    if m.group(2):
        tz = "UTC"
    else:
        tzid = params.get("TZID", "").strip()
        tz = dict(cfg.tzids).get(tzid, tzid) or cfg.timezone
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise CalendarError(
                f"{where}: TZID {tzid!r} is not an IANA time zone; map it in 'tzids'"
            ) from None
    return Event(t.date(), _time_text(t), tz, title)


def _ics_parse(body: bytes, cfg: CalendarConfig) -> list[Event]:
    # RFC 5545 §3.1: unfold the octets before decoding, as a fold can split a UTF-8 character.
    try:
        text = _ICS_FOLD.sub(b"", body).decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CalendarError(f"ics: the download is not UTF-8 text ({e})") from None
    lines = text.splitlines()
    if not lines or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise CalendarError("ics: the download does not start with BEGIN:VCALENDAR")
    events: list[Event] = []
    stack: list[str] = []
    props: dict[str, tuple[dict[str, str], str]] = {}
    start = 0
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        where = f"ics line {n}"
        name, params, value = _ics_property(line, where)
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                props, start = {}, n
            continue
        if name == "END":
            if not stack or stack[-1] != value.strip().upper():
                raise CalendarError(f"{where}: END:{value} does not close the open component")
            ended = stack.pop()
            if ended == "VEVENT":
                status = props.get("STATUS", ({}, ""))[1].strip().upper()
                if status != "CANCELLED":
                    events.append(_ics_event(props, cfg, f"ics event at line {start}"))
            continue
        if stack and stack[-1] == "VEVENT":
            if name in props and name in ("DTSTART", "SUMMARY"):
                raise CalendarError(f"{where}: {name} appears twice in one event")
            props[name] = (params, value)
    if stack:
        raise CalendarError(f"ics: the file ends inside {stack[-1]}")
    return events


# A curated file: a header row, then one event per line; the columns and the date and
# time formats are the calendar's options.


def _csv_parse(body: bytes, cfg: CalendarConfig) -> list[Event]:
    try:
        text = body.decode(cfg.option("encoding"))
    except UnicodeDecodeError as e:
        raise CalendarError(
            f"csv: the download is not {cfg.option('encoding')} text ({e})"
        ) from None
    lines = list(csv.reader(io.StringIO(text), delimiter=cfg.option("delimiter")))
    lines = lines[cfg.option("skip_lines") :]
    while lines and not any(c.strip() for c in lines[0]):
        lines = lines[1:]
    if not lines:
        raise CalendarError("csv: no header row")
    header = [c.strip() for c in lines[0]]
    cols: dict[str, int] = {}
    for opt in ("date_column", "title_column", "time_column", "timezone_column", "type_column"):
        want = cfg.option(opt)
        if want is None:
            continue
        if want not in header:
            raise CalendarError(f"csv: the header has no column {want!r}")
        cols[opt] = header.index(want)
    events: list[Event] = []
    for n, line in enumerate(lines[1:], cfg.option("skip_lines") + 2):
        if not any(c.strip() for c in line):
            continue
        where = f"csv line {n}"
        if len(line) != len(header):
            raise CalendarError(f"{where}: {len(line)} fields, not {len(header)}")

        def cell(opt: str, line: list[str] = line) -> str:
            return line[cols[opt]].strip() if opt in cols else ""

        try:
            d = datetime.strptime(cell("date_column"), cfg.option("date_format")).date()
        except ValueError:
            raise CalendarError(
                f"{where}: {cell('date_column')!r} is not a date ({cfg.option('date_format')})"
            ) from None
        time = ""
        if cell("time_column"):
            try:
                time = _time_text(datetime.strptime(cell("time_column"), cfg.option("time_format")))
            except ValueError:
                raise CalendarError(
                    f"{where}: {cell('time_column')!r} is not a time ({cfg.option('time_format')})"
                ) from None
        tz = cell("timezone_column")
        tz = _zone(tz, where) if tz else cfg.timezone
        title = " ".join(cell("title_column").split())
        if not title:
            raise CalendarError(f"{where}: the title is empty")
        kind = None
        if "type_column" in cols:
            kind = _type_name(cell("type_column"), where)
        events.append(Event(d, time, tz, title, kind))
    return events


FORMATS: dict[str, Format] = {
    "ics": Format("ics", 1, "ics", _ics_parse),
    "csv": Format("csv", 1, "csv", _csv_parse),
}


@dataclass(frozen=True)
class Parsed:
    """The events of one download: typed and keyed, and the span of dates it covers."""

    events: dict[EventKey, Event]
    first: date
    last: date
    #: The events whose title no type rule matches; they are not kept.
    unmatched: frozenset[tuple[date, str]]


def _typed(e: Event, cfg: CalendarConfig) -> Event | None:
    if e.type is not None:
        return e
    if cfg.type is not None:
        return replace(e, type=cfg.type)
    for rule in cfg.types:
        if re.search(rule.match, e.title):
            return replace(e, type=rule.type)
    return None


def parse(body: bytes, cfg: CalendarConfig) -> Parsed:
    """The typed events of one download. Raises :class:`CalendarError`.

    An event listed twice with the same date, type and title is one event when both
    give the same time and zone, and refused when they do not.
    """
    raw = FORMATS[cfg.source].parse(body, cfg)
    if not raw:
        raise CalendarError(f"{cfg.source}: the download holds no event")
    events: dict[EventKey, Event] = {}
    unmatched: set[tuple[date, str]] = set()
    for e in raw:
        t = _typed(e, cfg)
        if t is None:
            unmatched.add((e.date, e.title))
            continue
        other = events.get(t.key)
        if other is not None and other != t:
            raise CalendarError(
                f"{cfg.source}: {t.date.isoformat()} {t.type} {t.title!r} is listed twice "
                f"with another time or zone"
            )
        events[t.key] = t
    days = [e.date for e in raw]
    return Parsed(events, min(days), max(days), frozenset(unmatched))


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
    input: str

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Built:
    """The three files a calendar's fetches make, as bytes."""

    events: bytes
    withdrawn: bytes
    manifest: bytes
    rows: int
    withdrawn_rows: int
    revised: int


def _sort_key(e: Event) -> tuple[str, str, str, str]:
    return (e.date.isoformat(), e.time, e.type or "", e.title)


def build(cfg: CalendarConfig, fetches: Sequence[tuple[Fetch, bytes]]) -> Built:
    """Make ``events.csv``, ``withdrawn.csv`` and ``manifest.json`` from the fetches, in order.

    The events files merge in order: a later file wins for the dates of its span (its
    first to its last listed date). An event an earlier file listed, inside that span
    but not in the later file, moves to ``withdrawn.csv``. Events outside the later
    file's span keep their earlier rows. Kept pages are recorded, not parsed.
    """
    for f, _ in fetches:
        if f.input not in (EVENTS_INPUT, PAGE_INPUT):
            raise CalendarError(f"{f.raw}: input {f.input!r} is not events or page")
    current: dict[EventKey, tuple[Event, Fetch]] = {}
    seen: dict[EventKey, set[tuple[str, str]]] = {}
    withdrawn: dict[EventKey, tuple[Event, str, str]] = {}
    unmatched: set[tuple[date, str]] = set()
    for f, body in fetches:
        if f.input != EVENTS_INPUT:
            continue
        p = parse(body, cfg)
        unmatched |= p.unmatched
        for k in [k for k in current if p.first <= k[0] <= p.last and k not in p.events]:
            e, listed = current.pop(k)
            withdrawn[k] = (e, listed.sha256, f.sha256)
        for k, e in p.events.items():
            current[k] = (e, f)
            seen.setdefault(k, set()).add((e.time, e.timezone))
            withdrawn.pop(k, None)
    if not any(f.input == EVENTS_INPUT for f, _ in fetches):
        raise CalendarError(f"{cfg.id}: no events fetch to build from")
    if not current:
        raise CalendarError(f"{cfg.id}: the fetches hold no event")

    rows = [
        [
            e.date.isoformat(),
            e.time,
            e.timezone,
            cfg.institution,
            e.type or "",
            e.title,
            f.url,
            f.sha256,
            "true" if len(seen[k]) > 1 else "false",
        ]
        for k, (e, f) in sorted(current.items(), key=lambda kv: _sort_key(kv[1][0]))
    ]
    gone = [
        [e.date.isoformat(), e.time, e.timezone, e.type or "", e.title, listed, dropped]
        for e, listed, dropped in sorted(withdrawn.values(), key=lambda v: _sort_key(v[0]))
    ]
    events_bytes = _csv_bytes(EVENTS_HEADER, rows)
    withdrawn_bytes = _csv_bytes(WITHDRAWN_HEADER, gone)
    revised = sum(1 for r in rows if r[-1] == "true")
    fmt = FORMATS[cfg.source]
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "calendar": cfg.as_json(),
        "parser": {"format": fmt.name, "version": fmt.version},
        "fetches": [f.as_json() for f, _ in fetches],
        "events": {
            "path": EVENTS,
            "sha256": _sha(events_bytes),
            "rows": len(rows),
            "first": rows[0][0],
            "last": rows[-1][0],
            "revised": revised,
            "unmatched": len(unmatched),
        },
        "withdrawn": {"path": WITHDRAWN, "sha256": _sha(withdrawn_bytes), "rows": len(gone)},
    }
    return Built(
        events_bytes, withdrawn_bytes, manifest_bytes(manifest), len(rows), len(gone), revised
    )


# ---------------------------------------------------------------------------
# The store


def calendar_dir(store: Path, calendar_id: str) -> Path:
    if not _ID.match(calendar_id):
        raise CalendarError(f"calendar id {calendar_id!r} must match {_ID.pattern}")
    return store / AREA / calendar_id


def store_calendars(store: Path) -> list[str]:
    """The calendars present in a store's calendar area (directories with a manifest)."""
    area = store / AREA
    if not area.is_dir():
        return []
    return sorted(d.name for d in area.iterdir() if d.is_dir() and (d / MANIFEST).is_file())


def read_fetches(cdir: Path) -> list[Fetch]:
    path = cdir / FETCHES
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
                    d["input"],
                )
            )
        except (ValueError, KeyError, TypeError) as e:
            raise CalendarError(f"{path} line {n}: not a fetch record ({e})") from None
    return out


def _read_raw(cdir: Path, f: Fetch) -> bytes:
    path = cdir / f.raw
    if not path.is_file():
        raise CalendarError(f"{f.raw}: the raw file is missing")
    body = path.read_bytes()
    if _sha(body) != f.sha256:
        raise CalendarError(f"{f.raw}: sha256 {_sha(body)} does not match the record {f.sha256}")
    return body


def stored_config(cdir: Path) -> CalendarConfig | None:
    path = cdir / MANIFEST
    if not path.exists():
        return None
    try:
        return CalendarConfig.from_json(json.loads(path.read_text(encoding="utf-8"))["calendar"])
    except (ValueError, KeyError, TypeError) as e:
        raise CalendarError(f"{path}: not a calendar manifest ({e})") from None


_PAGE_EXTS = frozenset({"html", "htm", "pdf", "txt", "xml", "json", "csv", "ics"})


def page_ext(url: str, body: bytes) -> str:
    """The kept page's extension: the URL's own when it is a known one, else from the body."""
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in _PAGE_EXTS:
        return suffix
    head = body[:1024].lstrip().lower()
    if head.startswith(b"%pdf"):
        return "pdf"
    if head.startswith(b"<!doctype html") or b"<html" in head:
        return "html"
    return "bin"


# ---------------------------------------------------------------------------
# Fetch


def download(
    url: str,
    *,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    retries: int = DEFAULT_RETRIES,
    user_agent: str | None = None,
) -> Response:
    """GET one URL with the package's backoff. Raises :class:`CalendarError` after ``retries``.

    ``user_agent`` replaces the package's User-Agent header (see :func:`rates.download`).
    """
    try:
        return _rates.download(url, timeout=timeout, retries=retries, user_agent=user_agent)
    except _rates.RatesError as e:
        raise CalendarError(str(e)) from None


@dataclass
class FetchResult:
    """What one calendar's fetch did. ``status`` is ``updated``, ``unchanged`` or ``failed``."""

    id: str
    status: str
    events: int | None = None
    withdrawn: int | None = None
    revised: int | None = None
    manifest_sha256: str | None = None
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def fetch_calendar(
    store: Path,
    cfg: CalendarConfig,
    *,
    get: Callable[[str], Response] | None = None,
    now: Callable[[], str] = _rates._now,
) -> FetchResult:
    """Download a calendar's events file and its pages, keep the raw bodies and rebuild its files.

    Every earlier raw file is checked against its sha256, and the new events file is
    parsed, before anything is written. A body equal to the latest kept body from the
    same input (for a page: the same URL) adds no fetch line. The calendar's
    configuration (all but its URLs) must match the stored manifest's: another
    calendar cannot be written under the same id.
    """
    cdir = calendar_dir(store, cfg.id)
    prior_cfg = stored_config(cdir)
    if prior_cfg is not None and prior_cfg.identity() != cfg.identity():
        raise CalendarError(
            f"{cfg.id}: the store holds another calendar under this id; its configuration differs"
        )
    prior = [(f, _read_raw(cdir, f)) for f in read_fetches(cdir)]

    def latest(inp: str, url: str | None = None) -> str | None:
        shas = [f.sha256 for f, _ in prior if f.input == inp and url in (None, f.url)]
        return shas[-1] if shas else None

    new: list[tuple[Fetch, bytes]] = []
    for inp, url in [(EVENTS_INPUT, cfg.url), *((PAGE_INPUT, u) for u in cfg.pages)]:
        fetched_at = now()
        resp = (get or download)(url)
        if inp == EVENTS_INPUT:
            parse(resp.body, cfg)
            ext = FORMATS[cfg.source].ext
        else:
            ext = page_ext(url, resp.body)
        sha = _sha(resp.body)
        if latest(inp, None if inp == EVENTS_INPUT else url) == sha:
            continue
        f = Fetch(fetched_at, url, resp.status, sha, len(resp.body), f"{RAW}/{sha}.{ext}", inp)
        new.append((f, resp.body))

    built = build(cfg, [*prior, *new])
    files = ((EVENTS, built.events), (WITHDRAWN, built.withdrawn), (MANIFEST, built.manifest))
    result = FetchResult(
        cfg.id, "updated", built.rows, built.withdrawn_rows, built.revised, _sha(built.manifest)
    )
    if not new and all(
        (cdir / name).is_file() and (cdir / name).read_bytes() == data for name, data in files
    ):
        result.status = "unchanged"
        return result

    for f, body in new:
        raw_path = cdir / f.raw
        if not raw_path.exists():
            _write_atomic(raw_path, body)
        with (cdir / FETCHES).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(f.as_json(), sort_keys=True) + "\n")
    for name, data in files:
        _write_atomic(cdir / name, data)
    return result


def fetch(
    store: Path,
    configs: Sequence[CalendarConfig],
    *,
    get: Callable[[str], Response] | None = None,
    now: Callable[[], str] = _rates._now,
) -> list[FetchResult]:
    """Fetch each calendar in turn; a failed calendar is reported and the next one goes on."""
    out = []
    for cfg in configs:
        try:
            out.append(fetch_calendar(store, cfg, get=get, now=now))
        except CalendarError as e:
            log.error("%s: %s", cfg.id, e)
            out.append(FetchResult(cfg.id, "failed", error=str(e)))
    return out


# ---------------------------------------------------------------------------
# Verify


@dataclass
class VerifyReport:
    """The findings for one calendar; it passes when ``findings`` is empty."""

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


def verify_calendar(store: Path, calendar_id: str) -> VerifyReport:
    """Re-read a calendar's raw files and reproduce its three files byte for byte.

    Writes nothing.
    """
    rep = VerifyReport(calendar_id)
    try:
        cdir = calendar_dir(store, calendar_id)
        cfg = stored_config(cdir)
        if cfg is None:
            rep.findings.append(f"{MANIFEST} is missing")
            return rep
        if cfg.id != calendar_id:
            rep.findings.append(f"{MANIFEST} names calendar {cfg.id!r}")
            return rep
        recs = read_fetches(cdir)
        rep.fetches = len(recs)
        bodies = []
        for f in recs:
            try:
                bodies.append((f, _read_raw(cdir, f)))
            except CalendarError as e:
                rep.findings.append(str(e))
        if rep.findings:
            return rep
        built = build(cfg, bodies)
    except CalendarError as e:
        rep.findings.append(str(e))
        return rep
    for name, want in (
        (EVENTS, built.events),
        (WITHDRAWN, built.withdrawn),
        (MANIFEST, built.manifest),
    ):
        path = cdir / name
        if not path.is_file():
            rep.findings.append(f"{name} is missing")
        elif path.read_bytes() != want:
            rep.findings.append(f"{name} differs from the file rebuilt from the raw files")
    rep.manifest_sha256 = _sha((cdir / MANIFEST).read_bytes())
    recorded = {f.raw for f in recs}
    raw_dir = cdir / RAW
    if raw_dir.is_dir():
        for p in sorted(raw_dir.iterdir()):
            rel = f"{RAW}/{p.name}"
            if rel not in recorded:
                rep.warnings.append(f"{rel}: a raw file with no fetch record")
    return rep


def verify(store: Path, calendar_ids: Sequence[str] | None = None) -> list[VerifyReport]:
    ids = list(calendar_ids) if calendar_ids else store_calendars(store)
    return [verify_calendar(store, c) for c in ids]


def render_fetch_text(results: Sequence[FetchResult]) -> str:
    lines = []
    for r in results:
        if r.status == "failed":
            lines.append(f"{r.id}: failed: {r.error}")
        else:
            lines.append(
                f"{r.id}: {r.status}: {r.events} event(s), {r.withdrawn} withdrawn, "
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
        lines.append("no calendar in the store's calendar area")
    return "\n".join(lines) + "\n"
