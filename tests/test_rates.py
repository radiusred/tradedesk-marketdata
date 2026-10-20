"""tradedesk-md-rates (M27-R1): central-bank daily rate series in the reference-data area.

Every fixture is labelled synthetic data (``tests/fixtures/rates/README.md``): the shapes
of the public formats with made-up series codes and values. Downloads are stubbed; no test
makes a network request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from tradedesk_marketdata import rates as r
from tradedesk_marketdata import rates_cli
from tradedesk_marketdata.refresh import store_symbols

FIXTURES = Path(__file__).parent / "fixtures" / "rates"

# Synthetic configurations, one per format: no real series.
CONFIGS = {
    "fred": r.SeriesConfig("SYN-FRED", "fred", "SYNTHRATE", unit="percent"),
    "ecb": r.SeriesConfig("SYN-ECB", "ecb", "SYN/B.SYNTH.RATE"),
    "boe": r.SeriesConfig("SYN-BOE", "boe", "XSYNTH1", unit="percent"),
    "boj": r.SeriesConfig("SYN-BOJ", "boj", "SYN01/SYNTHCODE"),
    "rba": r.SeriesConfig("SYN-RBA", "rba", "s1/SYNTHIDA"),
}

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in range(1, 6))


def fixture(fmt: str) -> bytes:
    return (FIXTURES / f"synthetic_{fmt}.csv").read_bytes()


def clock():
    stamps = iter(f"2024-01-{d:02d}T12:00:00+00:00" for d in range(6, 32))
    return lambda: next(stamps)


def fetch(store: Path, cfg: r.SeriesConfig, body: bytes, now=None) -> r.FetchResult:
    return r.fetch_series(store, cfg, get=lambda url: r.Response(200, body), now=now or clock())


def csv_rows(path: Path) -> list[list[str]]:
    return [line.split(",") for line in path.read_text().splitlines()[1:]]


# ---------------------------------------------------------------------------
# Parsers


@pytest.mark.parametrize(
    "fmt, unit, published",
    [
        ("fred", None, None),
        ("ecb", "PC", None),
        ("boe", None, None),
        ("boj", "percent per annum", "2024-01-06"),
        ("rba", "Per cent", "2024-01-06"),
    ],
)
def test_each_format_parses_its_synthetic_fixture(fmt, unit, published):
    rows = r.parse(fixture(fmt), CONFIGS[fmt])
    values = {d: row.value for d, row in rows.items()}
    assert D3 not in rows
    assert values[D2] is not None and values[D4] is not None and values[D5] is not None
    if fmt != "boe":
        assert values[D1] is None  # listed by the source without a value
    assert {row.unit for row in rows.values()} == {unit}
    assert {row.published for row in rows.values()} == {published}


def test_values_are_kept_as_the_source_text():
    rows = r.parse(fixture("boe"), CONFIGS["boe"])
    assert rows[D4].value == "3.5125"
    rows = r.parse(fixture("fred"), CONFIGS["fred"])
    assert rows[D4].value == "1.30"  # not 1.3


def test_rba_picks_the_column_by_series_id_and_a_short_row_has_no_value():
    cfg = r.SeriesConfig("SYN-RBA-B", "rba", "s1/SYNTHIDB")
    rows = r.parse(fixture("rba"), cfg)
    assert rows[D1].value == "9.9"
    assert rows[D5].value is None


@pytest.mark.parametrize(
    "fmt, body, message",
    [
        ("fred", b"observation_date,OTHER\n2024-01-02,1\n", "not 'SYNTHRATE'"),
        ("fred", b"observation_date,SYNTHRATE\n2024-01,1\n", "not a daily date"),
        ("fred", b"observation_date,SYNTHRATE\n2024-01-02,abc\n", "not a decimal"),
        ("fred", b"observation_date,SYNTHRATE\n2024-01-02,1\n2024-01-02,2\n", "more than once"),
        ("fred", b"observation_date,SYNTHRATE\n2024-01-02,\n", "holds no value"),
        ("fred", b"<html>error</html>\n", "header"),
        ("boe", b"DATE,XSYNTH1\n2024-01-02,1\n", "not a daily date"),
        ("ecb", fixture("ecb").replace(b",B,SYNTH,", b",M,SYNTH,"), "not daily"),
        ("boj", fixture("boj").replace(b"STATUS,200", b"STATUS,400"), "status"),
        ("boj", fixture("boj").replace(b"NEXTPOSITION,", b"NEXTPOSITION,5"), "paged"),
        ("boj", fixture("boj").replace(b",DAILY,", b",MONTHLY,"), "not DAILY"),
        ("rba", fixture("rba").replace(b"SYNTHIDA", b"OTHERID"), "not in the table"),
        ("rba", fixture("rba").replace(b"Frequency,Daily", b"Frequency,Monthly"), "Monthly"),
        ("rba", b"\xff\xfe\x00", "not UTF-8"),
    ],
)
def test_a_download_that_does_not_parse_is_refused(fmt, body, message):
    with pytest.raises(r.RatesError, match=message):
        r.parse(body, CONFIGS[fmt])


@pytest.mark.parametrize(
    "cfg, url",
    [
        (CONFIGS["fred"], "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SYNTHRATE"),
        (
            CONFIGS["ecb"],
            "https://data-api.ecb.europa.eu/service/data/SYN/B.SYNTH.RATE?format=csvdata",
        ),
        (
            CONFIGS["boj"],
            "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
            "?format=csv&lang=en&db=SYN01&code=SYNTHCODE",
        ),
        (CONFIGS["rba"], "https://www.rba.gov.au/statistics/tables/csv/s1-data.csv"),
        (
            r.SeriesConfig("X", "fred", "K", url="https://example.test/x.csv"),
            "https://example.test/x.csv",
        ),
    ],
)
def test_each_format_addresses_its_series(cfg, url):
    assert cfg.resolved_url() == url


def test_boe_url_asks_from_the_configured_start():
    cfg = r.SeriesConfig("SYN-BOE", "boe", "XSYNTH1", unit="percent", start=date(2024, 1, 1))
    assert "Datefrom=01/Jan/2024" in cfg.resolved_url()
    assert "SeriesCodes=XSYNTH1" in cfg.resolved_url()


# ---------------------------------------------------------------------------
# The parsed files


def test_fetch_writes_the_series_files(tmp_path):
    res = fetch(tmp_path, CONFIGS["rba"], fixture("rba"))
    sdir = tmp_path / "_reference" / "rates" / "SYN-RBA"
    assert res.status == "updated"
    assert csv_rows(sdir / "observations.csv") == [
        ["2024-01-02", "4.35", "Per cent", "2024-01-06", "false"],
        ["2024-01-04", "4.35", "Per cent", "2024-01-06", "false"],
        ["2024-01-05", "4.40", "Per cent", "2024-01-06", "false"],
    ]
    assert (sdir / "observations.csv").read_text().splitlines()[0] == (
        "date,value,unit,published,revised"
    )
    # Never filled: 2024-01-01 is listed without a value, 2024-01-03 has no row.
    assert csv_rows(sdir / "gaps.csv") == [
        ["2024-01-01", "missing_in_source"],
        ["2024-01-03", "no_row"],
    ]
    body = fixture("rba")
    sha = hashlib.sha256(body).hexdigest()
    assert (sdir / "_raw" / f"{sha}.csv").read_bytes() == body
    (rec,) = [json.loads(x) for x in (sdir / "_fetches.jsonl").read_text().splitlines()]
    assert rec == {
        "fetched_at": "2024-01-06T12:00:00+00:00",
        "url": "https://www.rba.gov.au/statistics/tables/csv/s1-data.csv",
        "http_status": 200,
        "sha256": sha,
        "bytes": len(body),
        "raw": f"_raw/{sha}.csv",
    }
    manifest = json.loads((sdir / "manifest.json").read_text())
    assert manifest["fetches"] == [rec]
    assert manifest["series"]["key"] == "s1/SYNTHIDA"
    assert manifest["parser"] == {"format": "rba", "version": 1}
    for name in ("observations", "gaps"):
        data = (sdir / manifest[name]["path"]).read_bytes()
        assert manifest[name]["sha256"] == hashlib.sha256(data).hexdigest()
    assert res.manifest_sha256 == hashlib.sha256((sdir / "manifest.json").read_bytes()).hexdigest()


def test_weekend_days_are_not_gaps_with_the_weekday_calendar(tmp_path):
    body = b"observation_date,SYNTHRATE\n2024-01-05,1\n2024-01-08,1\n"
    fetch(tmp_path, CONFIGS["fred"], body)
    gaps = tmp_path / "_reference/rates/SYN-FRED/gaps.csv"
    assert csv_rows(gaps) == []
    cfg = r.SeriesConfig("SYN-FRED-ALL", "fred", "SYNTHRATE", unit="percent", calendar="all")
    fetch(tmp_path, cfg, body)
    gaps = tmp_path / "_reference/rates/SYN-FRED-ALL/gaps.csv"
    assert csv_rows(gaps) == [["2024-01-06", "no_row"], ["2024-01-07", "no_row"]]


def test_unit_comes_from_the_source_or_the_configuration(tmp_path):
    with pytest.raises(r.RatesError, match="states no unit"):
        fetch(tmp_path, r.SeriesConfig("SYN-FRED", "fred", "SYNTHRATE"), fixture("fred"))
    cfg = r.SeriesConfig("SYN-ECB", "ecb", "SYN/B.SYNTH.RATE", unit="percent")
    with pytest.raises(r.RatesError, match="states unit 'PC'"):
        fetch(tmp_path, cfg, fixture("ecb"))
    assert not (tmp_path / "_reference").exists()  # refused before anything was written


def test_a_second_fetch_flags_revisions_and_withdrawals(tmp_path):
    cfg = CONFIGS["fred"]
    now = clock()
    fetch(tmp_path, cfg, fixture("fred"), now)
    later = b"observation_date,SYNTHRATE\n2024-01-01,\n2024-01-02,1.20\n2024-01-05,1.30\n"
    res = fetch(tmp_path, cfg, later, now)
    sdir = tmp_path / "_reference/rates/SYN-FRED"
    assert res.status == "updated" and res.revised == 1
    assert csv_rows(sdir / "observations.csv") == [
        ["2024-01-02", "1.20", "percent", "", "true"],
        ["2024-01-05", "1.30", "percent", "", "false"],
    ]
    assert csv_rows(sdir / "gaps.csv") == [
        ["2024-01-01", "missing_in_source"],
        ["2024-01-03", "no_row"],
        ["2024-01-04", "withdrawn"],
    ]
    assert len((sdir / "_fetches.jsonl").read_text().splitlines()) == 2
    assert len(list((sdir / "_raw").iterdir())) == 2


def test_a_later_fetch_with_a_shorter_span_keeps_the_earlier_rows(tmp_path):
    cfg = CONFIGS["fred"]
    now = clock()
    fetch(tmp_path, cfg, fixture("fred"), now)
    fetch(tmp_path, cfg, b"observation_date,SYNTHRATE\n2024-01-05,1.35\n2024-01-08,1.40\n", now)
    rows = csv_rows(tmp_path / "_reference/rates/SYN-FRED/observations.csv")
    assert [(d, v, rev) for d, v, _, _, rev in rows] == [
        ("2024-01-02", "1.25", "false"),
        ("2024-01-04", "1.30", "false"),
        ("2024-01-05", "1.35", "true"),
        ("2024-01-08", "1.40", "false"),
    ]


def test_the_same_download_again_changes_nothing(tmp_path):
    cfg = CONFIGS["boj"]
    now = clock()
    first = fetch(tmp_path, cfg, fixture("boj"), now)
    sdir = tmp_path / "_reference/rates/SYN-BOJ"
    before = {p.name: p.read_bytes() for p in sdir.iterdir() if p.is_file()}
    again = fetch(tmp_path, cfg, fixture("boj"), now)
    assert again.status == "unchanged"
    assert again.manifest_sha256 == first.manifest_sha256
    assert {p.name: p.read_bytes() for p in sdir.iterdir() if p.is_file()} == before


def test_the_manifest_is_a_function_of_the_fetches(tmp_path):
    a = fetch(tmp_path / "a", CONFIGS["ecb"], fixture("ecb"))
    b = fetch(tmp_path / "b", CONFIGS["ecb"], fixture("ecb"))
    assert a.manifest_sha256 == b.manifest_sha256
    text = (tmp_path / "a/_reference/rates/SYN-ECB/manifest.json").read_text()
    assert json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n" == text


def test_a_download_that_does_not_parse_writes_nothing(tmp_path):
    fetch(tmp_path, CONFIGS["fred"], fixture("fred"))
    sdir = tmp_path / "_reference/rates/SYN-FRED"
    before = {p: p.read_bytes() for p in sdir.rglob("*") if p.is_file()}
    with pytest.raises(r.RatesError):
        fetch(tmp_path, CONFIGS["fred"], b"<html>maintenance</html>")
    assert {p: p.read_bytes() for p in sdir.rglob("*") if p.is_file()} == before


# Synthetic monthly and weekly downloads in the two formats that state no frequency.
MONTHLY_FRED = b"observation_date,SYNTHRATE\n" + b"".join(
    f"2024-{m:02d}-01,{m}.5\n".encode() for m in range(1, 13)
)
MONTHLY_BOE = b"DATE,XSYNTH1\n" + b"".join(
    date(2024, m, 1).strftime("%d %b %Y,3.5\n").encode() for m in range(1, 13)
)
MONTHLY_RBA_NO_FREQUENCY = (
    fixture("rba")
    .replace(b"Frequency,Daily,Daily\n", b"")
    .replace(b"02-Jan-2024", b"01-Feb-2024")
    .replace(b"04-Jan-2024", b"01-Mar-2024")
    .replace(b"05-Jan-2024", b"01-Apr-2024")
)
WEEKLY_FRED = b"observation_date,SYNTHRATE\n" + b"".join(
    f"2024-01-{d:02d},1.25\n".encode() for d in (5, 12, 19, 26)
)


@pytest.mark.parametrize(
    "fmt, body, message",
    [
        ("fred", MONTHLY_FRED, r"median 3\d days apart"),
        ("boe", MONTHLY_BOE, r"median 3\d days apart"),
        ("fred", WEEKLY_FRED, "median 7 days apart"),
        ("fred", b"observation_date,SYNTHRATE\n2024-01-02,1.25\n", "one dated row"),
        ("rba", MONTHLY_RBA_NO_FREQUENCY, r"median 3\d days apart"),
    ],
)
def test_a_series_whose_dates_are_not_daily_is_refused(fmt, body, message):
    with pytest.raises(r.RatesError, match=message):
        r.parse(body, CONFIGS[fmt])


def test_an_rba_column_without_a_frequency_row_is_judged_by_its_dates():
    body = fixture("rba").replace(b"Frequency,Daily,Daily\n", b"")
    assert r.parse(body, CONFIGS["rba"]) == r.parse(fixture("rba"), CONFIGS["rba"])


def test_a_daily_series_with_a_long_weekend_is_accepted():
    # Thu 2024-03-28, then Tue 2024-04-02 (Good Friday and Easter Monday closed): steps 5 and 1.
    body = b"observation_date,SYNTHRATE\n2024-03-28,1\n2024-04-02,1\n2024-04-03,1\n"
    assert len(r.parse(body, CONFIGS["fred"])) == 3


@pytest.mark.parametrize("fmt, body", [("fred", MONTHLY_FRED), ("boe", MONTHLY_BOE)])
def test_a_monthly_download_writes_nothing(tmp_path, fmt, body):
    res = r.fetch(tmp_path, [CONFIGS[fmt]], get=lambda url: r.Response(200, body), now=clock())
    assert res[0].status == "failed" and "not daily" in res[0].error
    assert not (tmp_path / "_reference").exists()
    fetch(tmp_path, CONFIGS[fmt], fixture(fmt))
    sdir = tmp_path / "_reference/rates" / CONFIGS[fmt].id
    before = {p: p.read_bytes() for p in sdir.rglob("*") if p.is_file()}
    with pytest.raises(r.RatesError, match="not daily"):
        fetch(tmp_path, CONFIGS[fmt], body)
    assert {p: p.read_bytes() for p in sdir.rglob("*") if p.is_file()} == before


def test_another_series_cannot_be_written_under_the_same_id(tmp_path):
    fetch(tmp_path, CONFIGS["fred"], fixture("fred"))
    other = r.SeriesConfig("SYN-FRED", "fred", "OTHERRATE", unit="percent")
    with pytest.raises(r.RatesError, match="holds fred 'SYNTHRATE'"):
        fetch(tmp_path, other, b"observation_date,OTHERRATE\n2024-01-02,1\n")


def test_fetch_reports_a_failed_series_and_goes_on(tmp_path):
    def get(url: str) -> r.Response:
        if "SYNTHRATE" in url:
            raise r.RatesError("synthetic outage")
        return r.Response(200, fixture("rba"))

    res = r.fetch(tmp_path, [CONFIGS["fred"], CONFIGS["rba"]], get=get, now=clock())
    assert [x.status for x in res] == ["failed", "updated"]
    assert res[0].error == "synthetic outage"


def test_the_candle_tools_do_not_see_the_reference_area(tmp_path):
    fetch(tmp_path, CONFIGS["fred"], fixture("fred"))
    assert store_symbols(tmp_path) == []


# ---------------------------------------------------------------------------
# Verify


@pytest.fixture
def store(tmp_path):
    now = clock()
    for fmt, cfg in CONFIGS.items():
        fetch(tmp_path, cfg, fixture(fmt), now)
    later = fixture("fred").replace(b"1.25", b"1.20")
    fetch(tmp_path, CONFIGS["fred"], later, now)
    return tmp_path


def test_verify_reproduces_every_series(store):
    reports = r.verify(store)
    assert [x.id for x in reports] == sorted(c.id for c in CONFIGS.values())
    assert all(not x.failed and not x.warnings for x in reports)
    assert [x.fetches for x in reports if x.id == "SYN-FRED"] == [2]


def _sdir(store: Path, sid: str = "SYN-FRED") -> Path:
    return store / "_reference" / "rates" / sid


def test_verify_finds_a_changed_raw_file(store):
    raw = sorted((_sdir(store) / "_raw").iterdir())[0]
    raw.write_bytes(raw.read_bytes().replace(b"1.30", b"1.31"))
    (rep,) = r.verify(store, ["SYN-FRED"])
    assert rep.failed and "does not match the record" in rep.findings[0]


def test_verify_finds_a_missing_raw_file(store):
    sorted((_sdir(store) / "_raw").iterdir())[0].unlink()
    (rep,) = r.verify(store, ["SYN-FRED"])
    assert rep.failed and "raw file is missing" in rep.findings[0]


@pytest.mark.parametrize("name", ["observations.csv", "gaps.csv", "manifest.json"])
def test_verify_finds_a_changed_parsed_file(store, name):
    path = _sdir(store) / name
    path.write_bytes(path.read_bytes() + b"\n")
    (rep,) = r.verify(store, ["SYN-FRED"])
    assert rep.findings == [f"{name} differs from the file rebuilt from the raw files"]


def test_verify_finds_a_filled_gap(store):
    path = _sdir(store) / "observations.csv"
    lines = path.read_text().splitlines()
    lines.insert(2, "2024-01-03,1.20,percent,,false")
    path.write_text("\n".join(lines) + "\n")
    (rep,) = r.verify(store, ["SYN-FRED"])
    assert rep.findings == ["observations.csv differs from the file rebuilt from the raw files"]


def test_verify_warns_on_a_raw_file_with_no_fetch_record(store):
    (_sdir(store) / "_raw" / "stray.csv").write_bytes(b"x")
    (rep,) = r.verify(store, ["SYN-FRED"])
    assert not rep.failed and rep.warnings == ["_raw/stray.csv: a raw file with no fetch record"]


def test_verify_names_a_series_with_no_manifest(store):
    (rep,) = r.verify(store, ["SYN-NONE"])
    assert rep.findings == ["manifest.json is missing"]


# ---------------------------------------------------------------------------
# Configuration


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "series.toml"
    p.write_text(text)
    return p


def test_load_config_reads_the_series_tables(tmp_path):
    cfgs = r.load_config(
        write(
            tmp_path,
            '[[series]]\nid = "SYN-A"\nsource = "boe"\nkey = "XSYNTH1"\nunit = "percent"\n'
            "start = 2024-01-01\n"
            '[[series]]\nid = "SYN-B"\nsource = "rba"\nkey = "s1/SYNTHIDA"\ncalendar = "all"\n',
        )
    )
    assert cfgs == [
        r.SeriesConfig("SYN-A", "boe", "XSYNTH1", unit="percent", start=date(2024, 1, 1)),
        r.SeriesConfig("SYN-B", "rba", "s1/SYNTHIDA", calendar="all"),
    ]


def test_the_shipped_example_configuration_loads():
    path = Path(r.__file__).parent / "rates.example.toml"
    assert [c.id for c in r.load_config(path)] == ["EXAMPLE-FRED", "EXAMPLE-ECB"]


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "no \\[\\[series\\]\\]"),
        ('other = 1\n[[series]]\nid="A"\nsource="fred"\nkey="K"\n', "unknown top-level"),
        ('[[series]]\nid="A"\nsource="fred"\n', "'key' is required"),
        ('[[series]]\nid="A/B"\nsource="fred"\nkey="K"\n', "must match"),
        ('[[series]]\nid="A"\nsource="nope"\nkey="K"\n', "not one of"),
        ('[[series]]\nid="A"\nsource="ecb"\nkey="NOSLASH"\n', "flow/series key"),
        ('[[series]]\nid="A"\nsource="fred"\nkey="K"\ncalendar="x"\n', "calendar"),
        ('[[series]]\nid="A"\nsource="fred"\nkey="K"\nstart="2024-13-01"\n', "start"),
        ('[[series]]\nid="A"\nsource="fred"\nkey="K"\nextra=1\n', "unknown key"),
        (
            '[[series]]\nid="A"\nsource="fred"\nkey="K"\n[[series]]\nid="A"\nsource="fred"\nkey="K"\n',
            "more than once",
        ),
        ("[[series]\n", "series.toml"),
    ],
)
def test_load_config_refuses_a_bad_configuration(tmp_path, text, message):
    with pytest.raises(r.RatesError, match=message):
        r.load_config(write(tmp_path, text))


# ---------------------------------------------------------------------------
# CLI


def test_cli_fetch_and_verify(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, '[[series]]\nid = "SYN-BOJ"\nsource = "boj"\nkey = "SYN01/SYNTHCODE"\n')
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(r, "download", lambda url, **kw: r.Response(200, fixture("boj")))
    assert rates_cli.main(["fetch", "--store", str(store), "--config", str(cfg)]) == 0
    assert "SYN-BOJ: updated: 3 observation(s), 2 gap(s), 0 revised" in capsys.readouterr().out
    assert rates_cli.main(["verify", "--store", str(store), "--format", "json"]) == 0
    (rep,) = json.loads(capsys.readouterr().out)["series"]
    assert rep["id"] == "SYN-BOJ" and rep["ok"] is True
    (_sdir(store, "SYN-BOJ") / "gaps.csv").write_text("date,reason\n")
    assert rates_cli.main(["verify", "--store", str(store)]) == 1
    assert "SYN-BOJ: FAIL" in capsys.readouterr().out


def test_cli_fetch_failure_exits_1(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, '[[series]]\nid = "SYN-BOJ"\nsource = "boj"\nkey = "SYN01/SYNTHCODE"\n')
    monkeypatch.setattr(r, "download", lambda url, **kw: r.Response(200, b"not a boj body"))
    assert (
        rates_cli.main(
            ["fetch", "--store", str(tmp_path), "--config", str(cfg), "--format", "json"]
        )
        == 1
    )
    (res,) = json.loads(capsys.readouterr().out)["series"]
    assert res["status"] == "failed" and "SERIES_CODE" in res["error"]


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch", "--store", "{missing}", "--config", "{cfg}"],
        ["fetch", "--store", "{tmp}", "--config", "{bad}"],
        ["fetch", "--store", "{tmp}", "--config", "{cfg}", "--series", "SYN-OTHER"],
        ["fetch", "--store", "{tmp}", "--config", "{cfg}", "--retries", "0"],
    ],
)
def test_cli_usage_errors_exit_2(tmp_path, argv):
    cfg = write(tmp_path, '[[series]]\nid = "SYN-BOJ"\nsource = "boj"\nkey = "SYN01/SYNTHCODE"\n')
    bad = tmp_path / "bad.toml"
    bad.write_text("[[series]]\n")
    subst = {
        "{missing}": str(tmp_path / "nope"),
        "{cfg}": str(cfg),
        "{bad}": str(bad),
        "{tmp}": str(tmp_path),
    }
    with pytest.raises(SystemExit) as e:
        rates_cli.main([subst.get(a, a) for a in argv])
    assert e.value.code == 2


def test_download_retries_then_refuses(monkeypatch):
    calls = []

    class Boom:
        def get(self, url, timeout):
            calls.append(url)
            raise OSError("synthetic network failure")

    monkeypatch.setattr(r, "_SESSION", Boom())
    monkeypatch.setattr(r.cancellation, "wait", lambda delay: False)
    with pytest.raises(r.RatesError, match="2 failed attempt"):
        r.download("https://example.test/x", retries=2)
    assert len(calls) == 2


def test_download_returns_the_http_status_with_the_body(monkeypatch):
    class Ok:
        status_code = 203
        content = b"synthetic body"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

    class Session:
        def get(self, url, timeout):
            return Ok()

    monkeypatch.setattr(r, "_SESSION", Session())
    assert r.download("https://example.test/x") == r.Response(203, b"synthetic body")


def test_the_fetch_record_keeps_the_http_status(tmp_path):
    cfg = CONFIGS["rba"]
    r.fetch_series(tmp_path, cfg, get=lambda url: r.Response(203, fixture("rba")), now=clock())
    sdir = _sdir(tmp_path, cfg.id)
    (rec,) = [json.loads(x) for x in (sdir / "_fetches.jsonl").read_text().splitlines()]
    assert rec["http_status"] == 203
    assert json.loads((sdir / "manifest.json").read_text())["fetches"][0]["http_status"] == 203
    assert r.verify_series(tmp_path, cfg.id).findings == []


def test_a_fetch_record_without_http_status_is_refused(tmp_path):
    cfg = CONFIGS["rba"]
    fetch(tmp_path, cfg, fixture("rba"))
    path = _sdir(tmp_path, cfg.id) / "_fetches.jsonl"
    rec = json.loads(path.read_text())
    del rec["http_status"]
    path.write_text(json.dumps(rec) + "\n")
    (finding,) = r.verify_series(tmp_path, cfg.id).findings
    assert "not a fetch record" in finding
