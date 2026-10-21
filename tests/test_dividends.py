"""tradedesk-md-dividends (M28-R1): index-dividend series in the reference-data area.

Every fixture is labelled synthetic data (``tests/fixtures/dividends/README.md``): the
shapes of the formats with made-up values. Downloads are stubbed; no test makes a network
request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from tradedesk_marketdata import dividends as dv
from tradedesk_marketdata import dividends_cli
from tradedesk_marketdata.refresh import store_symbols

FIXTURES = Path(__file__).parent / "fixtures" / "dividends"
SHILLER_URL = "https://example.test/synthetic_shiller.xls"
CSV_URL = "https://example.test/synthetic_generic.csv"
NK = dv._NIKKEI_BASE

D1, D2, D3, D4, D5, D6, D7, D8 = (date(2024, 1, d) for d in range(1, 9))


def nikkei(key: str) -> dv.InputConfig:
    return dv.InputConfig("nikkei", key)


GENERIC = dv.InputConfig(
    "csv",
    url=CSV_URL,
    options=tuple(
        sorted(
            {
                "date_column": "day",
                "value_column": "points",
                "published_column": "released",
                "delimiter": ";",
                "skip_lines": 1,
            }.items()
        )
    ),
)

# Synthetic configurations: no real series.
CONFIGS = {
    "points": dv.SeriesConfig("SYN-DP", "points", (("points", nikkei("dividend_points")),)),
    "tr_pr": dv.SeriesConfig(
        "SYN-TRPR", "tr_pr", (("tr", nikkei("total_return")), ("pr", nikkei("price")))
    ),
    "monthly": dv.SeriesConfig(
        "SYN-M",
        "monthly",
        (("monthly", dv.InputConfig("shiller", "D", SHILLER_URL)),),
        unit="synthetic units",
    ),
    "csv": dv.SeriesConfig("SYN-CSV", "points", (("points", GENERIC),), unit="index points"),
}

BODIES = {
    NK + "nikkei_stock_average_dp_daily_en.csv": "synthetic_nikkei_dp.csv",
    NK + "nikkei_225_total_return_index_daily_en.csv": "synthetic_nikkei_tr.csv",
    NK + "nikkei_stock_average_daily_en.csv": "synthetic_nikkei_pr.csv",
    SHILLER_URL: "synthetic_shiller.xls",
    CSV_URL: "synthetic_generic.csv",
}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def served(overrides: dict[str, bytes] | None = None):
    def get(url: str) -> dv.Response:
        if overrides and url in overrides:
            return dv.Response(200, overrides[url])
        return dv.Response(200, fixture(BODIES[url]))

    return get


def clock():
    stamps = iter(f"2024-02-{d:02d}T12:00:00+00:00" for d in range(1, 29))
    return lambda: next(stamps)


def fetch(store: Path, cfg: dv.SeriesConfig, overrides=None, now=None) -> dv.FetchResult:
    return dv.fetch_series(store, cfg, get=served(overrides), now=now or clock())


def sdir(store: Path, sid: str) -> Path:
    return store / "_reference" / "dividends" / sid


def csv_lines(path: Path) -> list[list[str]]:
    return [line.split(",") for line in path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# Parsers


def test_nikkei_dividend_points_keeps_the_text_and_the_year():
    rows = dv.parse(fixture("synthetic_nikkei_dp.csv"), CONFIGS["points"], "points")
    assert rows == {
        D1: dv.Row(None, None, "2023"),
        D2: dv.Row("1.25", None, "2024"),
        D4: dv.Row("1.30", None, "2024"),
        D5: dv.Row("1.30", None, "2024"),
    }


def test_nikkei_level_files_pick_their_column():
    body = fixture("synthetic_nikkei_tr.csv")
    cfg = dv.SeriesConfig(
        "SYN-NTR", "tr_pr", (("tr", nikkei("net_total_return")), ("pr", nikkei("price")))
    )
    assert dv.parse(body, CONFIGS["tr_pr"], "tr")[D4].value == "101"
    assert dv.parse(body, cfg, "tr")[D4].value == "90.9"
    assert dv.parse(fixture("synthetic_nikkei_pr.csv"), cfg, "pr")[D3].value == "50.2"


def test_shiller_reads_months_and_the_october_date():
    rows = dv.parse(fixture("synthetic_shiller.xls"), CONFIGS["monthly"], "monthly")
    assert rows[date(2022, 10, 1)].value == "1.25"
    assert rows[date(2023, 1, 1)].value == "1.75"
    assert rows[date(2023, 3, 1)].value is None
    assert date(2023, 2, 1) not in rows
    assert max(rows) == date(2023, 4, 1)


def test_shiller_reads_another_column_by_its_header():
    cfg = dv.SeriesConfig(
        "SYN-P", "monthly", (("monthly", dv.InputConfig("shiller", "P", SHILLER_URL)),), unit="u"
    )
    assert dv.parse(fixture("synthetic_shiller.xls"), cfg, "monthly")[date(2023, 4, 1)].value == (
        "12.5"
    )


def test_generic_csv_reads_its_options():
    rows = dv.parse(fixture("synthetic_generic.csv"), CONFIGS["csv"], "points")
    assert rows[D1] == dv.Row(None, None, None)
    assert rows[D4] == dv.Row("3.75", "2024-01-04T18:00:00Z", None)
    assert D3 not in rows


@pytest.mark.parametrize(
    "cfg, body, message",
    [
        ("points", b"Date,Close,Year\n", "header is not 'Date of Data"),
        ("points", b"Date of Data\n", "header is not"),
        ("points", b'Date of Data,Close,Open\n"2024/01/02","1","1"\n', "no 'Year' column"),
        ("points", b'Date of Data,Close,Year\n"2024-01-02","1","2024"\n', "is not a date"),
        ("points", b'Date of Data,Close,Year\n"2024/01/02","x","2024"\n', "not a decimal"),
        ("points", b'Date of Data,Close,Year\n"2024/01/02","","2024"\n', "holds no value"),
        ("points", b'Date of Data,Close,Year\n"2024/01/02","1"\n', "2 fields, not 3"),
        (
            "points",
            b'Date of Data,Close,Year\n"2024/01/02","1","2024"\n"2024/01/02","1","2024"\n',
            "appears more than once",
        ),
        ("points", b'Date of Data,Close,Year\n"2024/01/02","1","2024"\n', "one dated row"),
        ("csv", b"title\nday;other\n", "no column 'points'"),
        ("csv", b"\xff\xfe", "not utf-8-sig text"),
        ("monthly", b"not a workbook", "not an .xls workbook"),
    ],
)
def test_a_download_that_does_not_parse_is_refused(cfg, body, message):
    role = CONFIGS[cfg].inputs[0][0]
    with pytest.raises(dv.DividendsError, match=message):
        dv.parse(body, CONFIGS[cfg], role)


def test_a_net_total_return_column_must_say_so():
    cfg = dv.SeriesConfig(
        "SYN-NTR", "tr_pr", (("tr", nikkei("net_total_return")), ("pr", nikkei("price")))
    )
    body = fixture("synthetic_nikkei_pr.csv")
    with pytest.raises(dv.DividendsError, match="not a net total return"):
        dv.parse(body, cfg, "tr")


def test_a_monthly_download_is_refused_as_a_daily_input():
    body = b'Date of Data,Close,Year\n"2024/01/01","1","2024"\n"2024/02/01","1","2024"\n'
    with pytest.raises(dv.DividendsError, match="median 31 days apart, so the series is not daily"):
        dv.parse(body, CONFIGS["points"], "points")


def test_a_daily_download_is_refused_as_a_monthly_input():
    cfg = dv.SeriesConfig(
        "SYN-MC",
        "monthly",
        (("monthly", dv.InputConfig("csv", url=CSV_URL, options=GENERIC.options)),),
        unit="u",
    )
    with pytest.raises(dv.DividendsError, match="two rows fall in 2024-01, so the series is not"):
        dv.parse(fixture("synthetic_generic.csv"), cfg, "monthly")
    weekly = b"t\nday;points;released\n2024-01-01;1;\n2024-01-08;1;\n2024-01-15;1;\n"
    body = b"t\nday;points;released\n2024-01-01;1;\n2024-03-01;1;\n2024-05-01;1;\n"
    with pytest.raises(dv.DividendsError, match="so the series is not monthly"):
        dv.parse(weekly, cfg, "monthly")
    with pytest.raises(dv.DividendsError, match="so the series is not monthly"):
        dv.parse(body, cfg, "monthly")


def test_a_dated_row_after_the_shiller_note_is_refused():
    body = fixture("synthetic_shiller_row_after_note.xls")
    with pytest.raises(dv.DividendsError, match="after the end of the data"):
        dv.parse(body, CONFIGS["monthly"], "monthly")


def test_each_preset_addresses_its_file():
    assert CONFIGS["points"].input("points").resolved_url() == (
        NK + "nikkei_stock_average_dp_daily_en.csv"
    )
    assert CONFIGS["tr_pr"].input("pr").resolved_url() == NK + "nikkei_stock_average_daily_en.csv"
    assert CONFIGS["monthly"].input("monthly").resolved_url() == SHILLER_URL


# ---------------------------------------------------------------------------
# The parsed files


def test_a_points_series_writes_its_files(tmp_path):
    res = fetch(tmp_path, CONFIGS["points"])
    d = sdir(tmp_path, "SYN-DP")
    assert res.status == "updated"
    assert csv_lines(d / "observations.csv") == [
        ["date", "value", "unit", "period", "published", "revised"],
        ["2024-01-02", "1.25", "index points", "2024", "", "false"],
        ["2024-01-04", "1.30", "index points", "2024", "", "false"],
        ["2024-01-05", "1.30", "index points", "2024", "", "false"],
    ]
    # Never filled: 2024-01-01 is listed without a value, 2024-01-03 has no row.
    assert csv_lines(d / "gaps.csv") == [
        ["date", "reason"],
        ["2024-01-01", "missing_in_source"],
        ["2024-01-03", "no_row"],
    ]
    body = fixture("synthetic_nikkei_dp.csv")
    sha = hashlib.sha256(body).hexdigest()
    assert (d / "_raw" / f"{sha}.csv").read_bytes() == body
    (rec,) = [json.loads(x) for x in (d / "_fetches.jsonl").read_text().splitlines()]
    assert rec == {
        "fetched_at": "2024-02-01T12:00:00+00:00",
        "url": NK + "nikkei_stock_average_dp_daily_en.csv",
        "http_status": 200,
        "sha256": sha,
        "bytes": len(body),
        "raw": f"_raw/{sha}.csv",
        "input": "points",
    }
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["fetches"] == [rec]
    assert manifest["frequency"] == "daily" and manifest["coarse"] is False
    assert manifest["parsers"] == {"points": {"format": "nikkei", "version": 1}}
    assert "derivation" not in manifest
    for name in ("observations", "gaps"):
        data = (d / manifest[name]["path"]).read_bytes()
        assert manifest[name]["sha256"] == hashlib.sha256(data).hexdigest()
    assert res.manifest_sha256 == hashlib.sha256((d / "manifest.json").read_bytes()).hexdigest()


def test_a_generic_csv_series_keeps_the_publication_time(tmp_path):
    fetch(tmp_path, CONFIGS["csv"])
    assert csv_lines(sdir(tmp_path, "SYN-CSV") / "observations.csv")[2] == [
        "2024-01-04",
        "3.75",
        "index points",
        "",
        "2024-01-04T18:00:00Z",
        "false",
    ]


def test_a_tr_pr_series_derives_the_dividend_return(tmp_path):
    res = fetch(tmp_path, CONFIGS["tr_pr"])
    d = sdir(tmp_path, "SYN-TRPR")
    assert res.status == "updated"
    # Both levels exist on 01-02, 01-05 and 01-08. 01-05: (102/100)/(50.5/50) - 1;
    # 01-08: (102.51/102)/(50.5/50.5) - 1. 12 places, half-even.
    assert csv_lines(d / "observations.csv") == [
        ["date", "value", "unit", "span_from", "published", "revised"],
        ["2024-01-05", "0.009900990099", "fraction", "2024-01-02", "", "false"],
        ["2024-01-08", "0.005000000000", "fraction", "2024-01-05", "", "false"],
    ]
    # 01-03 has a price but no total return; 01-04 the reverse. The weekend is not expected.
    assert csv_lines(d / "gaps.csv") == [
        ["date", "reason"],
        ["2024-01-03", "missing_tr"],
        ["2024-01-04", "missing_pr"],
    ]
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["derivation"] == {
        "formula": "(TR_t / TR_p) / (PR_t / PR_p) - 1",
        "places": 12,
        "precision": 34,
        "rounding": "ROUND_HALF_EVEN",
    }
    assert [f["input"] for f in manifest["fetches"]] == ["tr", "pr"]
    assert manifest["parsers"] == {
        "tr": {"format": "nikkei", "version": 1},
        "pr": {"format": "nikkei", "version": 1},
    }


def test_a_tr_pr_series_with_the_all_calendar_lists_the_weekend(tmp_path):
    cfg = dv.SeriesConfig("SYN-TRPR-ALL", "tr_pr", CONFIGS["tr_pr"].inputs, calendar="all")
    fetch(tmp_path, cfg)
    assert csv_lines(sdir(tmp_path, "SYN-TRPR-ALL") / "gaps.csv")[1:] == [
        ["2024-01-03", "missing_tr"],
        ["2024-01-04", "missing_pr"],
        ["2024-01-06", "no_row"],
        ["2024-01-07", "no_row"],
    ]


def test_a_tr_pr_series_needs_two_common_dates(tmp_path):
    pr = b'Date of Data,Close,Open,High,Low\n"2024/01/03","1","1","1","1"\n'
    pr += b'"2024/01/04","1","1","1","1"\n'
    with pytest.raises(dv.DividendsError, match="fewer than two dates"):
        fetch(tmp_path, CONFIGS["tr_pr"], {NK + "nikkei_stock_average_daily_en.csv": pr})
    assert not sdir(tmp_path, "SYN-TRPR").exists()


def test_a_zero_level_is_refused(tmp_path):
    tr = fixture("synthetic_nikkei_tr.csv").replace(b'"100"', b'"0"')
    with pytest.raises(dv.DividendsError, match="level of zero"):
        fetch(tmp_path, CONFIGS["tr_pr"], {NK + "nikkei_225_total_return_index_daily_en.csv": tr})


def test_a_monthly_series_is_labelled_coarse(tmp_path):
    fetch(tmp_path, CONFIGS["monthly"])
    d = sdir(tmp_path, "SYN-M")
    lines = csv_lines(d / "observations.csv")
    assert lines[0] == ["date", "value", "unit", "period", "published", "revised", "coarse"]
    assert lines[1] == ["2022-10-01", "1.25", "synthetic units", "", "", "false", "true"]
    assert {row[6] for row in lines[1:]} == {"true"}
    assert csv_lines(d / "gaps.csv")[1:] == [
        ["2023-02-01", "no_row"],
        ["2023-03-01", "missing_in_source"],
    ]
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["frequency"] == "monthly" and manifest["coarse"] is True
    assert manifest["fetches"][0]["raw"].endswith(".xls")


def test_a_second_fetch_flags_revisions_and_withdrawals(tmp_path):
    now = clock()
    fetch(tmp_path, CONFIGS["points"], now=now)
    url = NK + "nikkei_stock_average_dp_daily_en.csv"
    later = fixture("synthetic_nikkei_dp.csv").replace(b'"1.25"', b'"1.20"')
    later = later.replace(b'"2024/01/04","1.30"', b'"2024/01/04",""')
    res = fetch(tmp_path, CONFIGS["points"], {url: later}, now)
    d = sdir(tmp_path, "SYN-DP")
    assert res.revised == 1
    assert csv_lines(d / "observations.csv")[1:] == [
        ["2024-01-02", "1.20", "index points", "2024", "", "true"],
        ["2024-01-05", "1.30", "index points", "2024", "", "false"],
    ]
    assert csv_lines(d / "gaps.csv")[1:] == [
        ["2024-01-01", "missing_in_source"],
        ["2024-01-03", "no_row"],
        ["2024-01-04", "withdrawn"],
    ]


def test_a_revised_level_flags_both_returns_it_enters(tmp_path):
    now = clock()
    fetch(tmp_path, CONFIGS["tr_pr"], now=now)
    url = NK + "nikkei_225_total_return_index_daily_en.csv"
    later = fixture("synthetic_nikkei_tr.csv").replace(b'"102"', b'"102.1"')
    fetch(tmp_path, CONFIGS["tr_pr"], {url: later}, now)
    d = sdir(tmp_path, "SYN-TRPR")
    assert [row[5] for row in csv_lines(d / "observations.csv")[1:]] == ["true", "true"]
    fetches = [json.loads(x)["input"] for x in (d / "_fetches.jsonl").read_text().splitlines()]
    assert fetches == ["tr", "pr", "tr"]  # the unchanged price adds no line


def test_the_same_downloads_again_change_nothing(tmp_path):
    for cfg in CONFIGS.values():
        first = fetch(tmp_path, cfg)
        d = sdir(tmp_path, cfg.id)
        before = {p: p.read_bytes() for p in d.rglob("*") if p.is_file()}
        again = fetch(tmp_path, cfg)
        assert again.status == "unchanged"
        assert again.manifest_sha256 == first.manifest_sha256
        assert {p: p.read_bytes() for p in d.rglob("*") if p.is_file()} == before


def test_the_manifest_is_a_function_of_the_fetches(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for store in (a, b):
        fetch(store, CONFIGS["tr_pr"])
    assert (sdir(a, "SYN-TRPR") / "manifest.json").read_bytes() == (
        sdir(b, "SYN-TRPR") / "manifest.json"
    ).read_bytes()


def test_a_download_that_does_not_parse_writes_nothing(tmp_path):
    url = NK + "nikkei_stock_average_daily_en.csv"
    with pytest.raises(dv.DividendsError):
        fetch(tmp_path, CONFIGS["tr_pr"], {url: b"not a nikkei body"})
    assert not (tmp_path / "_reference").exists()


def test_another_series_cannot_be_written_under_the_same_id(tmp_path):
    fetch(tmp_path, CONFIGS["tr_pr"])
    other = dv.SeriesConfig(
        "SYN-TRPR", "tr_pr", (("tr", nikkei("net_total_return")), ("pr", nikkei("price")))
    )
    with pytest.raises(dv.DividendsError, match="another series under this id"):
        fetch(tmp_path, other)


def test_fetch_reports_a_failed_series_and_goes_on(tmp_path):
    def get(url):
        if "dp_daily" in url:
            raise dv.DividendsError("synthetic outage")
        return served()(url)

    results = dv.fetch(tmp_path, [CONFIGS["points"], CONFIGS["csv"]], get=get, now=clock())
    assert [(x.id, x.status) for x in results] == [("SYN-DP", "failed"), ("SYN-CSV", "updated")]
    assert results[0].error == "synthetic outage"


def test_the_candle_tools_do_not_see_the_reference_area(tmp_path):
    fetch(tmp_path, CONFIGS["points"])
    assert store_symbols(tmp_path) == []


# ---------------------------------------------------------------------------
# Verify


@pytest.fixture
def store(tmp_path):
    now = clock()
    for cfg in CONFIGS.values():
        fetch(tmp_path, cfg, now=now)
    return tmp_path


def test_verify_reproduces_every_series(store):
    reports = dv.verify(store)
    assert [x.id for x in reports] == sorted(c.id for c in CONFIGS.values())
    assert all(not x.failed and not x.warnings for x in reports)
    assert [x.fetches for x in reports if x.id == "SYN-TRPR"] == [2]


def test_verify_finds_a_changed_raw_file(store):
    raw = sorted((sdir(store, "SYN-DP") / "_raw").iterdir())[0]
    raw.write_bytes(raw.read_bytes().replace(b"1.30", b"1.31"))
    (rep,) = dv.verify(store, ["SYN-DP"])
    assert rep.failed and "does not match the record" in rep.findings[0]


def test_verify_finds_a_missing_raw_file(store):
    sorted((sdir(store, "SYN-TRPR") / "_raw").iterdir())[0].unlink()
    (rep,) = dv.verify(store, ["SYN-TRPR"])
    assert rep.failed and "raw file is missing" in rep.findings[0]


@pytest.mark.parametrize("name", ["observations.csv", "gaps.csv", "manifest.json"])
@pytest.mark.parametrize("sid", ["SYN-DP", "SYN-TRPR", "SYN-M"])
def test_verify_finds_a_changed_parsed_file(store, sid, name):
    path = sdir(store, sid) / name
    path.write_bytes(path.read_bytes() + b"\n")
    (rep,) = dv.verify(store, [sid])
    assert rep.findings == [f"{name} differs from the file rebuilt from the raw files"]


def test_verify_finds_a_filled_gap(store):
    path = sdir(store, "SYN-DP") / "observations.csv"
    lines = path.read_text().splitlines()
    lines.insert(2, "2024-01-03,1.25,index points,2024,,false")
    path.write_text("\n".join(lines) + "\n")
    (rep,) = dv.verify(store, ["SYN-DP"])
    assert rep.findings == ["observations.csv differs from the file rebuilt from the raw files"]


def test_verify_warns_on_a_raw_file_with_no_fetch_record(store):
    (sdir(store, "SYN-DP") / "_raw" / "stray.csv").write_bytes(b"x")
    (rep,) = dv.verify(store, ["SYN-DP"])
    assert not rep.failed and rep.warnings == ["_raw/stray.csv: a raw file with no fetch record"]


def test_verify_names_a_series_with_no_manifest(store):
    (rep,) = dv.verify(store, ["SYN-NONE"])
    assert rep.findings == ["manifest.json is missing"]


def test_a_fetch_record_without_its_input_is_refused(store):
    path = sdir(store, "SYN-DP") / "_fetches.jsonl"
    rec = json.loads(path.read_text())
    del rec["input"]
    path.write_text(json.dumps(rec) + "\n")
    (rep,) = dv.verify(store, ["SYN-DP"])
    assert rep.failed and "not a fetch record" in rep.findings[0]


# ---------------------------------------------------------------------------
# Configuration


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "series.toml"
    p.write_text(text)
    return p


def test_load_config_reads_the_series_tables(tmp_path):
    cfgs = dv.load_config(
        write(
            tmp_path,
            '[[series]]\nid = "SYN-A"\nkind = "tr_pr"\ncalendar = "all"\n'
            '[series.tr]\nsource = "nikkei"\nkey = "total_return"\n'
            '[series.pr]\nsource = "nikkei"\nkey = "price"\n'
            '[[series]]\nid = "SYN-B"\nkind = "points"\nunit = "index points"\n'
            f'[series.input]\nsource = "csv"\nurl = "{CSV_URL}"\ndate_column = "day"\n'
            'value_column = "points"\npublished_column = "released"\ndelimiter = ";"\n'
            "skip_lines = 1\n"
            '[[series]]\nid = "SYN-C"\nkind = "monthly"\nunit = "synthetic units"\n'
            f'[series.input]\nsource = "shiller"\nkey = "D"\nurl = "{SHILLER_URL}"\n',
        )
    )
    assert cfgs == [
        dv.SeriesConfig("SYN-A", "tr_pr", CONFIGS["tr_pr"].inputs, calendar="all"),
        dv.SeriesConfig("SYN-B", "points", CONFIGS["csv"].inputs, unit="index points"),
        dv.SeriesConfig("SYN-C", "monthly", CONFIGS["monthly"].inputs, unit="synthetic units"),
    ]


def test_the_manifest_configuration_round_trips(store):
    for cfg in CONFIGS.values():
        assert dv.stored_config(sdir(store, cfg.id)) == cfg


def test_the_shipped_example_configuration_loads():
    example = Path(dv.__file__).with_name("dividends.example.toml")
    cfgs = dv.load_config(example)
    assert {c.kind for c in cfgs} == set(dv.KINDS)
    assert all(c.id.startswith("EXAMPLE-") for c in cfgs)


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "no \\[\\[series\\]\\] table"),
        ("x = 1\n", "unknown top-level key"),
        ('[[series]]\nkind = "points"\n', "'id' is required"),
        ('[[series]]\nid = "a/b"\nkind = "points"\n', "must match"),
        ('[[series]]\nid = "S"\nkind = "weekly"\n', "kind must be one of"),
        ('[[series]]\nid = "S"\nkind = "points"\n', "needs an \\[series.input\\] table"),
        (
            '[[series]]\nid = "S"\nkind = "points"\nother = 1\n[series.input]\nsource = "csv"\n',
            "unknown key\\(s\\) other",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\n[series.input]\nsource = "nope"\n',
            "source 'nope' is not one of",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\n[series.input]\nsource = "nikkei"\n'
            'key = "price"\n',
            "nikkei 'price' is a pr input",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\n[series.input]\nsource = "nikkei"\n'
            'key = "other"\n',
            "a nikkei key is one of",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\nunit = "u"\n[series.input]\n'
            'source = "shiller"\nurl = "https://example.test/x.xls"\n',
            "shiller is a monthly input only",
        ),
        (
            '[[series]]\nid = "S"\nkind = "monthly"\nunit = "u"\n[series.input]\n'
            'source = "shiller"\n',
            "shiller has no stable URL",
        ),
        (
            '[[series]]\nid = "S"\nkind = "monthly"\n[series.input]\nsource = "shiller"\n'
            'url = "https://example.test/x.xls"\n',
            "states no unit",
        ),
        (
            '[[series]]\nid = "S"\nkind = "monthly"\nunit = "u"\ncalendar = "all"\n'
            '[series.input]\nsource = "shiller"\nurl = "https://example.test/x.xls"\n',
            "takes no calendar",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\nunit = "u"\n[series.input]\nsource = "csv"\n'
            'url = "https://example.test/x.csv"\nvalue_column = "v"\n',
            "csv needs 'date_column'",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\nunit = "u"\n[series.input]\nsource = "csv"\n'
            'url = "https://example.test/x.csv"\ndate_column = "d"\nvalue_column = "v"\n'
            'encoding = "nope"\n',
            "unknown encoding",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\nunit = "u"\n[series.input]\nsource = "csv"\n'
            'date_column = "d"\nvalue_column = "v"\n',
            "csv has no default URL",
        ),
        (
            '[[series]]\nid = "S"\nkind = "tr_pr"\n[series.input]\nsource = "nikkei"\n'
            'key = "dividend_points"\n',
            "takes no 'input' table",
        ),
        (
            '[[series]]\nid = "S"\nkind = "points"\n[series.input]\nsource = "nikkei"\n'
            'key = "dividend_points"\n[[series]]\nid = "S"\nkind = "points"\n'
            '[series.input]\nsource = "nikkei"\nkey = "dividend_points"\n',
            "appears more than once",
        ),
    ],
)
def test_load_config_refuses_a_bad_configuration(tmp_path, text, message):
    with pytest.raises(dv.DividendsError, match=message):
        dv.load_config(write(tmp_path, text))


# ---------------------------------------------------------------------------
# CLI

POINTS_TOML = (
    '[[series]]\nid = "SYN-DP"\nkind = "points"\n'
    '[series.input]\nsource = "nikkei"\nkey = "dividend_points"\n'
)


def test_cli_fetch_and_verify(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, POINTS_TOML)
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(dv, "download", lambda url, **kw: served()(url))
    assert dividends_cli.main(["fetch", "--store", str(store), "--config", str(cfg)]) == 0
    assert "SYN-DP: updated: 3 observation(s), 2 gap(s), 0 revised" in capsys.readouterr().out
    assert dividends_cli.main(["verify", "--store", str(store), "--format", "json"]) == 0
    (rep,) = json.loads(capsys.readouterr().out)["series"]
    assert rep["id"] == "SYN-DP" and rep["ok"] is True
    (sdir(store, "SYN-DP") / "gaps.csv").write_text("date,reason\n")
    assert dividends_cli.main(["verify", "--store", str(store)]) == 1
    assert "SYN-DP: FAIL" in capsys.readouterr().out


def test_cli_fetch_failure_exits_1(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, POINTS_TOML)
    monkeypatch.setattr(dv, "download", lambda url, **kw: dv.Response(200, b"not a body"))
    argv = ["fetch", "--store", str(tmp_path), "--config", str(cfg), "--format", "json"]
    assert dividends_cli.main(argv) == 1
    (res,) = json.loads(capsys.readouterr().out)["series"]
    assert res["status"] == "failed" and "Date of Data" in res["error"]


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
    cfg = write(tmp_path, POINTS_TOML)
    bad = tmp_path / "bad.toml"
    bad.write_text("[[series]]\n")
    subst = {
        "{missing}": str(tmp_path / "nope"),
        "{cfg}": str(cfg),
        "{bad}": str(bad),
        "{tmp}": str(tmp_path),
    }
    with pytest.raises(SystemExit) as e:
        dividends_cli.main([subst.get(a, a) for a in argv])
    assert e.value.code == 2


def test_download_refuses_with_a_dividends_error(monkeypatch):
    from tradedesk_marketdata import rates

    def fail(url, **kw):
        raise rates.RatesError("synthetic: 2 failed attempt(s)")

    monkeypatch.setattr(rates, "download", fail)
    with pytest.raises(dv.DividendsError, match="2 failed attempt"):
        dv.download("https://example.test/x", retries=2)


def test_a_file_url_input_is_kept_with_no_http_status(tmp_path):
    # rates.download reads a file: URL with no HTTP status (M29-R2); the fetch line keeps null.
    path = tmp_path / "synthetic_generic.csv"
    path.write_bytes(fixture("synthetic_generic.csv"))
    assert dv.download(path.as_uri()).status is None
    inp = dv.InputConfig("csv", url=path.as_uri(), options=GENERIC.options)
    cfg = dv.SeriesConfig("SYN-CSV-FILE", "points", (("points", inp),), unit="index points")
    store = tmp_path / "store"
    assert dv.fetch_series(store, cfg, now=clock()).status == "updated"
    (rec,) = [
        json.loads(x) for x in (sdir(store, cfg.id) / "_fetches.jsonl").read_text().splitlines()
    ]
    assert rec["url"] == path.as_uri() and rec["http_status"] is None
    assert dv.verify_series(store, cfg.id).findings == []
