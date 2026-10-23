"""tradedesk-md-calendars (M34-R3): release calendars in the reference-data area.

Every fixture is labelled synthetic data (``tests/fixtures/calendars/README.md``): the
shapes of the formats with made-up institutions, titles, dates and times. Downloads are
stubbed; no test makes a network request.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date
from pathlib import Path

import pytest

from tradedesk_marketdata import calendars as cal
from tradedesk_marketdata import calendars_cli
from tradedesk_marketdata.refresh import store_symbols

FIXTURES = Path(__file__).parent / "fixtures" / "calendars"
ICS_URL = "https://example.test/synthetic_calendar.ics"
CSV_URL = "https://example.test/synthetic_releases.csv"
PAGE_URL = "https://example.test/synthetic-schedule"

# Synthetic configurations: no real publisher.
ICS = cal.CalendarConfig(
    "SYN-ICS",
    "Synthetic Central Bank",
    "Europe/London",
    "ics",
    ICS_URL,
    types=(
        cal.TypeRule("policy_decision", "(?i)policy decision"),
        cal.TypeRule("minutes", "Minutes"),
    ),
)
CSV = cal.CalendarConfig(
    "SYN-CSV",
    "Synthetic Statistics Office",
    "America/New_York",
    "csv",
    CSV_URL,
    options=tuple(
        sorted(
            {
                "date_column": "Day",
                "time_column": "Time",
                "timezone_column": "Zone",
                "title_column": "Release",
                "type_column": "Kind",
                "date_format": "%d/%m/%Y",
                "delimiter": ";",
                "skip_lines": 1,
            }.items()
        )
    ),
    pages=(PAGE_URL,),
)

BODIES = {
    ICS_URL: "synthetic_calendar.ics",
    CSV_URL: "synthetic_releases.csv",
    PAGE_URL: "synthetic_page.html",
}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def served(overrides: dict[str, bytes] | None = None):
    def get(url: str) -> cal.Response:
        if overrides and url in overrides:
            return cal.Response(200, overrides[url])
        return cal.Response(200, fixture(BODIES[url]))

    return get


def clock():
    stamps = iter(f"2024-04-{d:02d}T12:00:00+00:00" for d in range(1, 29))
    return lambda: next(stamps)


def fetch(store: Path, cfg: cal.CalendarConfig, overrides=None, now=None) -> cal.FetchResult:
    return cal.fetch_calendar(store, cfg, get=served(overrides), now=now or clock())


def cdir(store: Path, cid: str) -> Path:
    return store / "_reference" / "calendars" / cid


def rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text())))


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def ics(*events: str) -> bytes:
    body = "\r\n".join(("BEGIN:VCALENDAR", *events, "END:VCALENDAR", ""))
    return body.encode()


def vevent(*lines: str) -> str:
    return "\r\n".join(("BEGIN:VEVENT", *lines, "END:VEVENT"))


# ---------------------------------------------------------------------------
# Parsers


def test_ics_reads_each_dtstart_form_and_skips_cancelled_events():
    p = cal.parse(fixture("synthetic_calendar.ics"), ICS)
    got = sorted(p.events.values(), key=lambda e: e.date)
    assert got == [
        cal.Event(
            date(2024, 1, 10),
            "12:00",
            "Europe/London",
            "Synthetic Policy Decision, with a long title that the publisher folds over two lines",
            "policy_decision",
        ),
        cal.Event(date(2024, 1, 24), "13:00", "UTC", "Synthetic Minutes", "minutes"),
        # All day: no time of day, the calendar's zone.
        cal.Event(
            date(2024, 2, 7), "", "Europe/London", "Synthetic Policy Decision", "policy_decision"
        ),
        # Floating: the calendar's zone.
        cal.Event(date(2024, 2, 21), "09:30", "Europe/London", "Synthetic Minutes", "minutes"),
    ]
    # The cancelled event is not listed; the unmatched one is counted, and still spans.
    assert p.unmatched == {(date(2024, 3, 1), "Synthetic Office Closed")}
    assert (p.first, p.last) == (date(2024, 1, 10), date(2024, 3, 1))


def test_ics_quoted_parameters_and_unescaping():
    body = ics(
        vevent(
            'DTSTART;X-NOTE="a:b;c";TZID=America/Chicago:20240105T083015',
            "SUMMARY:Synthetic\\; one\\nand two \\\\ three",
        )
    )
    cfg = cal.CalendarConfig("SYN", "Synthetic", "UTC", "ics", ICS_URL, type="release")
    (e,) = cal.parse(body, cfg).events.values()
    assert (e.time, e.timezone, e.title) == (
        "08:30:15",
        "America/Chicago",
        "Synthetic; one and two \\ three",
    )


@pytest.mark.parametrize(
    "body, match",
    [
        (b"not a calendar", "BEGIN:VCALENDAR"),
        (ics(vevent("DTSTART:20240105", "SUMMARY:x", "RRULE:FREQ=MONTHLY")), "recurring"),
        (ics(vevent("DTSTART:20240105", "SUMMARY:x", "RDATE:20240205")), "recurring"),
        (ics(vevent("SUMMARY:x")), "without DTSTART"),
        (ics(vevent("DTSTART:20240105")), "without SUMMARY"),
        (ics(vevent("DTSTART:2024-01-05", "SUMMARY:x")), "not a date"),
        (ics(vevent("DTSTART:20241305", "SUMMARY:x")), "not a date"),
        (ics(vevent("DTSTART:20240105", "DTSTART:20240106", "SUMMARY:x")), "twice"),
        (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n", "ends inside VEVENT"),
        (ics(vevent("DTSTART:20240105", "SUMMARY:x", "STATUS:CANCELLED")), "holds no event"),
        (ics("BEGIN:VEVENT", "END:VTODO"), "does not close"),
        (ics(vevent("DTSTART;TZID", "SUMMARY:x")), "without '='"),
        (b"\xff\xfe", "not UTF-8"),
    ],
)
def test_ics_refusals(body, match):
    cfg = cal.CalendarConfig("SYN", "Synthetic", "UTC", "ics", ICS_URL, type="release")
    with pytest.raises(cal.CalendarError, match=match):
        cal.parse(body, cfg)


def test_ics_maps_a_tzid_of_its_own_and_refuses_one_it_cannot_map():
    body = ics(vevent("DTSTART;TZID=Synthetic-Eastern:20240105T083000", "SUMMARY:Synthetic"))
    cfg = cal.CalendarConfig("SYN", "Synthetic", "UTC", "ics", ICS_URL, type="release")
    with pytest.raises(cal.CalendarError, match="'Synthetic-Eastern' is not an IANA time zone"):
        cal.parse(body, cfg)
    mapped = cal.CalendarConfig(
        **{**cfg.__dict__, "tzids": (("Synthetic-Eastern", "America/New_York"),)}
    )
    (e,) = cal.parse(body, mapped).events.values()
    assert (e.time, e.timezone) == ("08:30", "America/New_York")


def test_an_event_listed_twice_is_one_event_or_refused():
    cfg = cal.CalendarConfig("SYN", "Synthetic", "UTC", "ics", ICS_URL, type="release")
    same = vevent("DTSTART:20240105T100000Z", "SUMMARY:Synthetic")
    assert len(cal.parse(ics(same, same), cfg).events) == 1
    other = vevent("DTSTART:20240105T110000Z", "SUMMARY:Synthetic")
    with pytest.raises(cal.CalendarError, match="listed twice"):
        cal.parse(ics(same, other), cfg)


def test_curated_csv_reads_the_named_columns():
    p = cal.parse(fixture("synthetic_releases.csv"), CSV)
    assert sorted(p.events.values(), key=lambda e: e.date) == [
        cal.Event(date(2024, 1, 5), "08:30", "America/New_York", "Synthetic Jobs Report", "jobs"),
        cal.Event(date(2024, 1, 11), "10:00", "America/Chicago", "Synthetic Price Index", "prices"),
        cal.Event(date(2024, 1, 31), "", "America/New_York", "Synthetic Survey", "survey"),
    ]
    assert p.unmatched == frozenset()


@pytest.mark.parametrize(
    "line, match",
    [
        ("2024-01-05;08:30;;Synthetic;jobs", "is not a date"),
        ("05/01/2024;8.30;;Synthetic;jobs", "is not a time"),
        ("05/01/2024;08:30;Mars/Olympus;Synthetic;jobs", "IANA"),
        ("05/01/2024;08:30;;;jobs", "title is empty"),
        ("05/01/2024;08:30;;Synthetic;not a type", "event type"),
        ("05/01/2024;08:30;Synthetic;jobs", "4 fields, not 5"),
    ],
)
def test_curated_csv_refusals(line, match):
    body = f"title\nDay;Time;Zone;Release;Kind\n{line}\n".encode()
    with pytest.raises(cal.CalendarError, match=match):
        cal.parse(body, CSV)


def test_curated_csv_needs_its_columns():
    with pytest.raises(cal.CalendarError, match="no column 'Kind'"):
        cal.parse(b"title\nDay;Time;Zone;Release\n05/01/2024;;;x\n", CSV)


# ---------------------------------------------------------------------------
# Configuration


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "calendars.toml"
    p.write_text(text)
    return p


ICS_TOML = (
    '[[calendar]]\nid = "SYN-ICS"\ninstitution = "Synthetic Central Bank"\n'
    'timezone = "Europe/London"\nsource = "ics"\n'
    f'url = "{ICS_URL}"\n'
    '[[calendar.types]]\ntype = "policy_decision"\nmatch = "(?i)policy decision"\n'
    '[[calendar.types]]\ntype = "minutes"\nmatch = "Minutes"\n'
)


def test_load_config_reads_the_tables_and_the_example_parses(tmp_path):
    (cfg,) = cal.load_config(write(tmp_path, ICS_TOML))
    assert cfg == ICS
    example = Path(cal.__file__).parent / "calendars.example.toml"
    loaded = cal.load_config(example)
    assert [c.id for c in loaded] == ["EXAMPLE-ICS", "EXAMPLE-CSV"]
    for c in loaded:
        assert cal.CalendarConfig.from_json(c.as_json()) == c


@pytest.mark.parametrize(
    "edit, match",
    [
        (('id = "SYN-ICS"', 'id = "a b"'), "must match"),
        (('timezone = "Europe/London"\n', ""), "'timezone' is required"),
        (("Europe/London", "Europe/Nowhere"), "IANA"),
        (('institution = "Synthetic Central Bank"\n', ""), "'institution' is required"),
        (('source = "ics"', 'source = "pdf"'), "source must be one of"),
        ((f'url = "{ICS_URL}"\n', ""), "'url' is required"),
        (('source = "ics"', 'source = "ics"\ndate_column = "Day"'), "unknown key"),
        (('source = "ics"', 'source = "ics"\ntype = "release"'), "exactly one of"),
        (('match = "Minutes"', 'match = "("'), "not a regular expression"),
        (('type = "minutes"', 'type = "two words"'), "event type"),
        (('source = "ics"', 'source = "ics"\npages = ["x", "x"]'), "more than once"),
        (('source = "ics"', 'source = "ics"\ntzids = { A = "Mars/Base" }'), "IANA"),
        (('source = "ics"', 'source = "ics"\ntzids = { A = 1 }'), "must name an IANA"),
    ],
)
def test_load_config_refusals(tmp_path, edit, match):
    with pytest.raises(cal.CalendarError, match=match):
        cal.load_config(write(tmp_path, ICS_TOML.replace(*edit, 1)))


def test_load_config_refuses_a_duplicate_id_and_no_type(tmp_path):
    with pytest.raises(cal.CalendarError, match="appears more than once"):
        cal.load_config(write(tmp_path, ICS_TOML + ICS_TOML))
    no_type = ICS_TOML.split("[[calendar.types]]")[0]
    with pytest.raises(cal.CalendarError, match="exactly one of"):
        cal.load_config(write(tmp_path, no_type))
    csv_table = no_type.replace('"ics"', '"csv"') + 'date_column = "D"\ntitle_column = "T"\n'
    with pytest.raises(cal.CalendarError, match="'tzids' is for the ics format only"):
        cal.load_config(write(tmp_path, csv_table + 'type = "x"\ntzids = { A = "UTC" }\n'))
    with pytest.raises(cal.CalendarError, match="needs 'title_column'"):
        cal.load_config(
            write(tmp_path, no_type.replace('"ics"', '"csv"') + 'date_column = "Day"\ntype = "x"\n')
        )


# ---------------------------------------------------------------------------
# Fetch, store and merge


def test_fetch_writes_the_store_layout(tmp_path):
    res = fetch(tmp_path, ICS)
    assert (res.status, res.events, res.withdrawn, res.revised) == ("updated", 4, 0, 0)
    d = cdir(tmp_path, "SYN-ICS")
    body = fixture("synthetic_calendar.ics")
    assert (d / "_raw" / f"{sha(body)}.ics").read_bytes() == body
    (rec,) = [json.loads(x) for x in (d / "_fetches.jsonl").read_text().splitlines()]
    assert rec == {
        "fetched_at": "2024-04-01T12:00:00+00:00",
        "url": ICS_URL,
        "http_status": 200,
        "sha256": sha(body),
        "bytes": len(body),
        "raw": f"_raw/{sha(body)}.ics",
        "input": "events",
    }
    ev = rows(d / "events.csv")
    assert list(ev[0]) == list(cal.EVENTS_HEADER)
    assert [(r["date"], r["time"], r["timezone"], r["type"]) for r in ev] == [
        ("2024-01-10", "12:00", "Europe/London", "policy_decision"),
        ("2024-01-24", "13:00", "UTC", "minutes"),
        ("2024-02-07", "", "Europe/London", "policy_decision"),
        ("2024-02-21", "09:30", "Europe/London", "minutes"),
    ]
    assert {r["institution"] for r in ev} == {"Synthetic Central Bank"}
    assert {(r["source_url"], r["raw_sha256"], r["revised"]) for r in ev} == {
        (ICS_URL, sha(body), "false")
    }
    assert (d / "withdrawn.csv").read_text() == ",".join(cal.WITHDRAWN_HEADER) + "\n"
    m = json.loads((d / "manifest.json").read_text())
    assert m["calendar"] == ICS.as_json()
    assert m["parser"] == {"format": "ics", "version": 1}
    assert m["events"] == {
        "path": "events.csv",
        "sha256": sha((d / "events.csv").read_bytes()),
        "rows": 4,
        "first": "2024-01-10",
        "last": "2024-02-21",
        "revised": 0,
        "unmatched": 1,
    }
    assert res.manifest_sha256 == sha((d / "manifest.json").read_bytes())


def test_a_later_download_revises_withdraws_and_keeps_events_outside_its_span(tmp_path):
    fetch(tmp_path, ICS)
    later = fixture("synthetic_calendar_later.ics")
    res = fetch(tmp_path, ICS, {ICS_URL: later}, now=clock())
    assert (res.status, res.events, res.withdrawn, res.revised) == ("updated", 4, 1, 1)
    d = cdir(tmp_path, "SYN-ICS")
    first = sha(fixture("synthetic_calendar.ics"))
    ev = rows(d / "events.csv")
    assert [(r["date"], r["time"], r["raw_sha256"], r["revised"]) for r in ev] == [
        ("2024-01-10", "12:00", first, "false"),  # before the later file's span: kept
        ("2024-01-24", "14:00", sha(later), "true"),  # moved: the later time, revised
        ("2024-02-21", "09:30", sha(later), "false"),
        ("2024-03-20", "12:00", sha(later), "false"),
    ]
    assert rows(d / "withdrawn.csv") == [
        {
            "date": "2024-02-07",
            "time": "",
            "timezone": "Europe/London",
            "type": "policy_decision",
            "title": "Synthetic Policy Decision",
            "listed_sha256": first,
            "dropped_sha256": sha(later),
        }
    ]
    assert len((d / "_fetches.jsonl").read_text().splitlines()) == 2
    assert cal.verify_calendar(tmp_path, "SYN-ICS").findings == []


def test_an_event_listed_again_leaves_withdrawn(tmp_path):
    fetch(tmp_path, ICS)
    fetch(tmp_path, ICS, {ICS_URL: fixture("synthetic_calendar_later.ics")})
    res = fetch(tmp_path, ICS)  # the first body again
    assert (res.status, res.events, res.withdrawn, res.revised) == ("updated", 5, 0, 1)
    d = cdir(tmp_path, "SYN-ICS")
    assert rows(d / "withdrawn.csv") == []
    # 2024-02-07 is back; 2024-03-20 is after the first body's span, so it stays.
    assert [(r["date"], r["time"]) for r in rows(d / "events.csv")] == [
        ("2024-01-10", "12:00"),
        ("2024-01-24", "13:00"),
        ("2024-02-07", ""),
        ("2024-02-21", "09:30"),
        ("2024-03-20", "12:00"),
    ]


def test_an_unchanged_body_adds_no_fetch_and_rewrites_nothing(tmp_path):
    fetch(tmp_path, ICS)
    d = cdir(tmp_path, "SYN-ICS")
    before = {p.name: p.stat().st_mtime_ns for p in d.iterdir() if p.is_file()}
    res = fetch(tmp_path, ICS)
    assert res.status == "unchanged"
    assert {p.name: p.stat().st_mtime_ns for p in d.iterdir() if p.is_file()} == before


def test_a_download_that_does_not_parse_writes_nothing(tmp_path):
    with pytest.raises(cal.CalendarError, match="BEGIN:VCALENDAR"):
        fetch(tmp_path, ICS, {ICS_URL: b"<html>moved</html>"})
    assert not cdir(tmp_path, "SYN-ICS").exists()
    fetch(tmp_path, ICS)
    d = cdir(tmp_path, "SYN-ICS")
    before = {p: p.read_bytes() for p in d.rglob("*") if p.is_file()}
    with pytest.raises(cal.CalendarError):
        fetch(tmp_path, ICS, {ICS_URL: b"<html>moved</html>"})
    assert {p: p.read_bytes() for p in d.rglob("*") if p.is_file()} == before


def test_another_calendar_under_the_same_id_is_refused(tmp_path):
    fetch(tmp_path, ICS)
    other = cal.CalendarConfig(ICS.id, "Synthetic Other", ICS.timezone, "ics", ICS_URL, type="x")
    with pytest.raises(cal.CalendarError, match="another calendar under this id"):
        fetch(tmp_path, other)
    # A new URL alone is the same calendar.
    moved = cal.CalendarConfig(**{**ICS.__dict__, "url": "https://example.test/moved.ics"})
    assert fetch(tmp_path, moved, {moved.url: fixture("synthetic_calendar.ics")}).status == (
        "updated"
    )


def test_curated_csv_keeps_its_pages_raw(tmp_path):
    res = fetch(tmp_path, CSV)
    assert (res.status, res.events) == ("updated", 3)
    d = cdir(tmp_path, "SYN-CSV")
    page = fixture("synthetic_page.html")
    recs = [json.loads(x) for x in (d / "_fetches.jsonl").read_text().splitlines()]
    assert [(r["input"], r["url"], r["raw"]) for r in recs] == [
        ("events", CSV_URL, f"_raw/{sha(fixture('synthetic_releases.csv'))}.csv"),
        ("page", PAGE_URL, f"_raw/{sha(page)}.html"),
    ]
    assert (d / "_raw" / f"{sha(page)}.html").read_bytes() == page
    assert json.loads((d / "manifest.json").read_text())["calendar"]["pages"] == [PAGE_URL]
    # An unchanged page adds no line; a changed page adds one and leaves the events alone.
    assert fetch(tmp_path, CSV).status == "unchanged"
    newer = page.replace(b"made-up dates", b"other made-up dates")
    assert fetch(tmp_path, CSV, {PAGE_URL: newer}).status == "updated"
    recs = [json.loads(x) for x in (d / "_fetches.jsonl").read_text().splitlines()]
    assert [r["input"] for r in recs] == ["events", "page", "page"]
    assert cal.verify_calendar(tmp_path, "SYN-CSV").findings == []


@pytest.mark.parametrize(
    "url, body, ext",
    [
        ("https://x.test/a.PDF", b"", "pdf"),
        ("https://x.test/a", b"%PDF-1.7", "pdf"),
        ("https://x.test/a", b"  <!DOCTYPE html><p>", "html"),
        ("https://x.test/a.aspx", b"<html>", "html"),
        ("https://x.test/a", b"\x00\x01", "bin"),
    ],
)
def test_page_ext(url, body, ext):
    assert cal.page_ext(url, body) == ext


def test_the_candle_tools_skip_the_calendar_area(tmp_path):
    fetch(tmp_path, ICS)
    assert store_symbols(tmp_path) == []


# ---------------------------------------------------------------------------
# Verify


def test_verify_reproduces_the_files_and_reports_each_finding(tmp_path):
    fetch(tmp_path, ICS)
    fetch(tmp_path, ICS, {ICS_URL: fixture("synthetic_calendar_later.ics")})
    rep = cal.verify_calendar(tmp_path, "SYN-ICS")
    assert (rep.findings, rep.warnings, rep.fetches) == ([], [], 2)
    d = cdir(tmp_path, "SYN-ICS")

    (d / "_raw" / "stray.ics").write_bytes(b"x")
    assert cal.verify_calendar(tmp_path, "SYN-ICS").warnings == [
        "_raw/stray.ics: a raw file with no fetch record"
    ]

    good = (d / "events.csv").read_bytes()
    (d / "events.csv").write_bytes(good.replace(b"14:00", b"15:00"))
    assert cal.verify_calendar(tmp_path, "SYN-ICS").findings == [
        "events.csv differs from the file rebuilt from the raw files"
    ]
    (d / "events.csv").write_bytes(good)

    (d / "withdrawn.csv").unlink()
    assert cal.verify_calendar(tmp_path, "SYN-ICS").findings == ["withdrawn.csv is missing"]

    raw = d / "_raw" / f"{sha(fixture('synthetic_calendar.ics'))}.ics"
    raw.write_bytes(b"tampered")
    (finding,) = cal.verify_calendar(tmp_path, "SYN-ICS").findings
    assert "does not match the record" in finding
    raw.unlink()
    (finding,) = cal.verify_calendar(tmp_path, "SYN-ICS").findings
    assert "the raw file is missing" in finding


def test_verify_lists_the_store_and_reports_a_missing_manifest(tmp_path):
    fetch(tmp_path, ICS)
    fetch(tmp_path, CSV)
    assert [r.id for r in cal.verify(tmp_path)] == ["SYN-CSV", "SYN-ICS"]
    assert cal.verify_calendar(tmp_path, "SYN-NONE").findings == ["manifest.json is missing"]
    assert "no calendar" in cal.render_verify_text([])


# ---------------------------------------------------------------------------
# CLI


def test_cli_fetch_and_verify(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, ICS_TOML)
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(cal, "download", lambda url, **kw: served()(url))
    assert calendars_cli.main(["fetch", "--store", str(store), "--config", str(cfg)]) == 0
    assert "SYN-ICS: updated: 4 event(s), 0 withdrawn, 0 revised" in capsys.readouterr().out
    assert calendars_cli.main(["verify", "--store", str(store), "--format", "json"]) == 0
    (rep,) = json.loads(capsys.readouterr().out)["calendars"]
    assert rep["id"] == "SYN-ICS" and rep["ok"] is True
    (cdir(store, "SYN-ICS") / "withdrawn.csv").write_text("x\n")
    assert calendars_cli.main(["verify", "--store", str(store)]) == 1
    assert "SYN-ICS: FAIL" in capsys.readouterr().out


def test_cli_fetch_failure_exits_1(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, ICS_TOML)
    monkeypatch.setattr(cal, "download", lambda url, **kw: cal.Response(200, b"not a body"))
    argv = ["fetch", "--store", str(tmp_path), "--config", str(cfg), "--format", "json"]
    assert calendars_cli.main(argv) == 1
    (res,) = json.loads(capsys.readouterr().out)["calendars"]
    assert res["status"] == "failed" and "BEGIN:VCALENDAR" in res["error"]


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch", "--store", "{missing}", "--config", "{cfg}"],
        ["fetch", "--store", "{tmp}", "--config", "{bad}"],
        ["fetch", "--store", "{tmp}", "--config", "{cfg}", "--calendar", "SYN-OTHER"],
        ["fetch", "--store", "{tmp}", "--config", "{cfg}", "--retries", "0"],
    ],
)
def test_cli_usage_errors_exit_2(tmp_path, argv):
    cfg = write(tmp_path, ICS_TOML)
    bad = tmp_path / "bad.toml"
    bad.write_text("[[calendar]]\n")
    subst = {
        "{missing}": str(tmp_path / "nope"),
        "{cfg}": str(cfg),
        "{bad}": str(bad),
        "{tmp}": str(tmp_path),
    }
    with pytest.raises(SystemExit) as e:
        calendars_cli.main([subst.get(a, a) for a in argv])
    assert e.value.code == 2


def test_download_refuses_with_a_calendar_error(monkeypatch):
    from tradedesk_marketdata import rates

    def fail(url, **kw):
        raise rates.RatesError("synthetic: 2 failed attempt(s)")

    monkeypatch.setattr(rates, "download", fail)
    with pytest.raises(cal.CalendarError, match="2 failed attempt"):
        cal.download("https://example.test/x", retries=2)


def test_a_file_url_events_file_is_kept_with_no_http_status(tmp_path):
    path = tmp_path / "synthetic_releases.csv"
    path.write_bytes(fixture("synthetic_releases.csv"))
    cfg = cal.CalendarConfig(**{**CSV.__dict__, "url": path.as_uri(), "pages": ()})
    store = tmp_path / "store"
    assert cal.fetch_calendar(store, cfg, now=clock()).status == "updated"
    (rec,) = [
        json.loads(x) for x in (cdir(store, cfg.id) / "_fetches.jsonl").read_text().splitlines()
    ]
    assert rec["url"] == path.as_uri() and rec["http_status"] is None
    assert cal.verify_calendar(store, cfg.id).findings == []


def test_user_agent_replaces_the_header_for_one_request(monkeypatch):
    from tradedesk_marketdata import rates

    seen = []

    class Reply:
        status_code = 200
        content = b"synthetic"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

    def get(url, timeout, headers=None):
        seen.append(headers)
        return Reply()

    monkeypatch.setattr(rates._SESSION, "get", get)
    cal.download("https://example.test/a", user_agent="synthetic-bot/1 (ops@example.test)")
    cal.download("https://example.test/a")
    assert seen == [{"User-Agent": "synthetic-bot/1 (ops@example.test)"}, None]


def test_cli_sends_the_user_agent(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, ICS_TOML)
    agents = []

    def get(url, **kw):
        agents.append(kw["user_agent"])
        return served()(url)

    monkeypatch.setattr(cal, "download", get)
    argv = ["fetch", "--store", str(tmp_path), "--config", str(cfg)]
    assert calendars_cli.main([*argv, "--user-agent", "synthetic-bot/1"]) == 0
    assert calendars_cli.main(argv) == 0
    assert agents == ["synthetic-bot/1", None]
