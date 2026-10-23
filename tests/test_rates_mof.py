"""tradedesk-md-rates (M34-R2): the MOF JGB interest-rate CSV format, two files per fetch.

The fixtures are labelled synthetic data (``tests/fixtures/rates/README.md``): the shape of
the publisher's all-dates and current-month files with made-up values. Downloads are
stubbed; no test makes a network request.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tradedesk_marketdata import rates as r

FIXTURES = Path(__file__).parent / "fixtures" / "rates"
ALL = (FIXTURES / "synthetic_mof_all.csv").read_bytes()
CUR = (FIXTURES / "synthetic_mof_cur.csv").read_bytes()
ALL_URL, CUR_URL = r.FORMATS["mof"].url(r.SeriesConfig("x", "mof", "7Y"))

CFG = r.SeriesConfig("SYN-MOF", "mof", "7Y")


def mof_file(title: str, rows: list[str], header: str = "Date,3Y,7Y,40Y") -> bytes:
    """A synthetic MOF file: the title line, the header and the rows, CRLF, cp932."""
    lines = [f"{title},,,(Unit : %)", header, *rows]
    return ("\r\n".join(lines) + "\r\n").encode("cp932")


def clock():
    stamps = iter(f"2024-02-{d:02d}T12:00:00+00:00" for d in range(1, 29))
    return lambda: next(stamps)


def fetch(store: Path, all_body: bytes, cur_body: bytes, cfg: r.SeriesConfig = CFG, now=None):
    bodies = {ALL_URL: all_body, CUR_URL: cur_body}
    asked: list[str] = []

    def get(url: str) -> r.Response:
        asked.append(url)
        return r.Response(200, bodies[url])

    result = r.fetch_series(store, cfg, get=get, now=now or clock())
    return result, asked


def sdir(store: Path) -> Path:
    return r.series_dir(store, CFG.id)


def csv_rows(path: Path) -> list[list[str]]:
    return [line.split(",") for line in path.read_text().splitlines()[1:]]


# ---------------------------------------------------------------------------
# Parse


def test_the_all_dates_file_parses_the_tenor_column():
    rows = r.parse(ALL, CFG)
    assert {d: row.value for d, row in rows.items()} == {
        date(2023, 12, 25): "0.501",
        date(2023, 12, 26): "0.502",
        date(2023, 12, 27): None,  # "-": listed without a value
        date(2023, 12, 29): "0.505",
    }
    assert {row.unit for row in rows.values()} == {"%"}
    assert {row.published for row in rows.values()} == {None}


def test_the_current_month_file_parses_and_its_trailer_is_not_read():
    rows = r.parse(CUR, CFG)
    assert {d: row.value for d, row in rows.items()} == {
        date(2024, 1, 1): None,
        date(2024, 1, 2): "0.512",
        date(2024, 1, 4): "0.514",
        date(2024, 1, 5): "0.515",
    }


def test_the_tenor_is_part_of_the_series_key():
    three = r.parse(ALL, r.SeriesConfig("SYN-MOF-3", "mof", "3Y"))
    assert three[date(2023, 12, 27)].value == "0.103"


def test_rows_before_a_tenor_s_first_value_are_not_read_in_the_all_dates_file():
    rows = r.parse(ALL, r.SeriesConfig("SYN-MOF-40", "mof", "40Y"))
    assert min(rows) == date(2023, 12, 27)
    # In the current-month file a "-" stays a day without a value.
    rows = r.parse(CUR, r.SeriesConfig("SYN-MOF-40", "mof", "40Y"))
    assert rows[date(2024, 1, 1)].value is None


def test_start_drops_the_rows_dated_before_it():
    rows = r.parse(ALL, r.SeriesConfig("SYN-MOF", "mof", "7Y", start=date(2023, 12, 26)))
    assert min(rows) == date(2023, 12, 26)


def test_without_a_unit_in_the_title_the_configured_unit_is_used(tmp_path):
    no_unit = ALL.replace(b"(Unit : %)", b"")
    assert {row.unit for row in r.parse(no_unit, CFG).values()} == {None}
    cfg = r.SeriesConfig("SYN-MOF", "mof", "7Y", unit="percent")
    fetch(tmp_path, no_unit, CUR.replace(b"(Unit : %)", b""), cfg)
    assert {row[2] for row in csv_rows(sdir(tmp_path) / r.OBSERVATIONS)} == {"percent"}


@pytest.mark.parametrize(
    "body, message",
    [
        (b"Rates,,,\r\nDate,3Y,7Y,40Y\r\n2023/12/25,1,1,1\r\n", "title line"),
        (mof_file("Interest Rate (Janvier 2024)", ["2024/1/2,1,1,1"]), "not a month"),
        (mof_file("Interest Rate", ["2023/12/25,1,1,1"], "Day,3Y,7Y,40Y"), "header"),
        (mof_file("Interest Rate", ["2023/12/25,1,1,1"], "Date,3Y,5Y,40Y"), "'7Y' is not one"),
        (mof_file("Interest Rate", ["2023/12/25,1,1,1"], "Date,7Y,7Y,40Y"), "'7Y' is not one"),
        (mof_file("Interest Rate", ["2023/12/25,1,1"]), "3 fields, not 4"),
        (mof_file("Interest Rate", ["2023-12-25,1,1,1"]), "not a daily date"),
        (mof_file("Interest Rate", ["2023/12/25,1,n/a,1"]), "not a decimal number"),
        (mof_file("Interest Rate", ["2023/12/25,1,1,1", "2023/12/25,1,2,1"]), "more than once"),
        (mof_file("Interest Rate (January 2024)", ["2024/2/1,1,1,1"]), "not in January 2024"),
        (mof_file("Interest Rate", ["2023/12/25,1,1,1", ",,,", "2023/12/26,,,"]), "after the end"),
        (mof_file("Interest Rate", ["2023/12/25,1,1,1", ",,,", "x,1,1,1"]), "after the end"),
        (mof_file("Interest Rate", ["2023/12/25,1,-,1"]), "holds no value"),
        (b"Interest Rate\r\n\x85\x40", "not cp932"),
    ],
)
def test_a_file_that_does_not_parse_is_refused(body, message):
    with pytest.raises(r.RatesError, match=message):
        r.parse(body, CFG)


# ---------------------------------------------------------------------------
# Fetch: two files, joined without overlap or gap


def test_one_fetch_reads_both_files_and_joins_them(tmp_path):
    result, asked = fetch(tmp_path, ALL, CUR)
    assert asked == [ALL_URL, CUR_URL]
    assert result.status == "updated"
    assert result.sha256 == f"{r._sha(ALL)} {r._sha(CUR)}"
    d = sdir(tmp_path)
    recs = r.read_fetches(d)
    assert [(f.url, f.sha256, f.raw) for f in recs] == [
        (ALL_URL, r._sha(ALL), f"_raw/{r._sha(ALL)}.csv"),
        (CUR_URL, r._sha(CUR), f"_raw/{r._sha(CUR)}.csv"),
    ]
    assert (d / recs[0].raw).read_bytes() == ALL and (d / recs[1].raw).read_bytes() == CUR
    assert csv_rows(d / r.OBSERVATIONS) == [
        ["2023-12-25", "0.501", "%", "", "false"],
        ["2023-12-26", "0.502", "%", "", "false"],
        ["2023-12-29", "0.505", "%", "", "false"],
        ["2024-01-02", "0.512", "%", "", "false"],
        ["2024-01-04", "0.514", "%", "", "false"],
        ["2024-01-05", "0.515", "%", "", "false"],
    ]
    assert csv_rows(d / r.GAPS) == [
        ["2023-12-27", "missing_in_source"],
        ["2023-12-28", "no_row"],
        ["2024-01-01", "missing_in_source"],
        ["2024-01-03", "no_row"],
    ]
    manifest = json.loads((d / r.MANIFEST).read_text())
    assert manifest["parser"] == {"format": "mof", "version": 1}
    assert [f["url"] for f in manifest["fetches"]] == [ALL_URL, CUR_URL]
    assert not r.verify_series(tmp_path, CFG.id).failed


def test_the_same_files_again_change_nothing(tmp_path):
    fetch(tmp_path, ALL, CUR)
    before = {p.name: p.read_bytes() for p in sdir(tmp_path).iterdir() if p.is_file()}
    result, _ = fetch(tmp_path, ALL, CUR)
    assert result.status == "unchanged"
    after = {p.name: p.read_bytes() for p in sdir(tmp_path).iterdir() if p.is_file()}
    assert after == before


def test_a_new_current_month_file_adds_one_fetch(tmp_path):
    fetch(tmp_path, ALL, CUR)
    cur2 = CUR.replace(b"2024/1/5,0.115,0.515,1.915\r\n", b"2024/1/5,0.115,0.515,1.915\r\n"
                       b"2024/1/8,0.118,0.518,1.918\r\n")  # fmt: skip
    result, _ = fetch(tmp_path, ALL, cur2)
    assert result.status == "updated"
    assert [f.url for f in r.read_fetches(sdir(tmp_path))] == [ALL_URL, CUR_URL, CUR_URL]
    assert csv_rows(sdir(tmp_path) / r.OBSERVATIONS)[-1] == [
        "2024-01-08",
        "0.518",
        "%",
        "",
        "false",
    ]
    assert not r.verify_series(tmp_path, CFG.id).failed


def test_a_month_roll_lets_the_new_all_dates_file_supersede_the_old_month(tmp_path):
    fetch(tmp_path, ALL, CUR)
    all2 = ALL + (
        b"2024/1/1,-,-,-\r\n2024/1/2,0.112,0.512,1.912\r\n"
        b"2024/1/4,0.114,0.524,1.914\r\n2024/1/5,0.115,0.515,1.915\r\n"
    )
    cur2 = mof_file("Interest Rate (February 2024)", ["2024/2/1,0.121,0.521,1.921"])
    fetch(tmp_path, all2, cur2)
    rows = {row[0]: row for row in csv_rows(sdir(tmp_path) / r.OBSERVATIONS)}
    assert rows["2024-01-04"] == ["2024-01-04", "0.524", "%", "", "true"]  # revised
    assert rows["2024-02-01"][1] == "0.521"
    assert not r.verify_series(tmp_path, CFG.id).failed


@pytest.mark.parametrize(
    "all_body, cur_body, message",
    [
        # The all-dates file already holds a day of the current month.
        (
            ALL + b"2024/1/2,0.112,0.512,1.912\r\n",
            CUR,
            "overlap: the all-dates file ends 2024-01-02, the current-month file starts 2024-01-01",
        ),
        # The current-month file is still last month's.
        (
            ALL,
            mof_file("Interest Rate (December 2023)", ["2023/12/29,0.105,0.505,1.905"]),
            "overlap",
        ),
        # A month is missing between the two files.
        (
            ALL,
            mof_file("Interest Rate (February 2024)", ["2024/2/1,0.121,0.521,1.921"]),
            "gap: the all-dates file ends 2023-12-29, the current-month file is for 2024-02",
        ),
        (CUR, CUR, "first file is a current-month file"),
        (ALL, ALL, "second file is an all-dates file"),
    ],
)
def test_files_that_do_not_join_are_refused_and_nothing_is_written(
    tmp_path, all_body, cur_body, message
):
    with pytest.raises(r.RatesError, match=message):
        fetch(tmp_path, all_body, cur_body)
    assert not (tmp_path / r.AREA).exists()


def test_a_refused_join_on_a_later_fetch_leaves_the_series_as_it_was(tmp_path):
    fetch(tmp_path, ALL, CUR)
    before = {p.name: p.read_bytes() for p in sdir(tmp_path).iterdir() if p.is_file()}
    cur_feb = mof_file("Interest Rate (February 2024)", ["2024/2/1,0.121,0.521,1.921"])
    with pytest.raises(r.RatesError, match="gap"):
        fetch(tmp_path, ALL, cur_feb)
    after = {p.name: p.read_bytes() for p in sdir(tmp_path).iterdir() if p.is_file()}
    assert after == before


def test_the_joined_rows_must_be_daily(tmp_path):
    weekly = mof_file("Interest Rate", ["2023/12/1,1,1,1", "2023/12/8,1,1,1", "2023/12/15,1,1,1"])
    cur = mof_file("Interest Rate (January 2024)", ["2024/1/5,1,1,1"])
    with pytest.raises(r.RatesError, match="not daily"):
        fetch(tmp_path, weekly, cur)


# ---------------------------------------------------------------------------
# Configuration


def test_the_default_urls_are_the_publisher_s_english_files():
    assert CFG.resolved_urls() == (
        "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv",
        "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv",
    )
    with pytest.raises(r.RatesError, match="reads 2 files"):
        CFG.resolved_url()


def test_a_url_list_names_the_two_files_and_round_trips_through_the_manifest(tmp_path):
    (tmp_path / "all.csv").write_bytes(ALL)
    (tmp_path / "cur.csv").write_bytes(CUR)
    urls = [(tmp_path / "all.csv").as_uri(), (tmp_path / "cur.csv").as_uri()]
    conf = tmp_path / "series.toml"
    conf.write_text(
        f'[[series]]\nid = "SYN-MOF"\nsource = "mof"\nkey = "7Y"\nurl = {json.dumps(urls)}\n'
    )
    (cfg,) = r.load_config(conf)
    assert cfg.resolved_urls() == tuple(urls)
    store = tmp_path / "store"
    store.mkdir()
    result = r.fetch_series(store, cfg)
    assert result.status == "updated"
    assert [f.http_status for f in r.read_fetches(sdir(store))] == [None, None]
    assert r.stored_config(sdir(store)) == cfg
    assert json.loads((sdir(store) / r.MANIFEST).read_text())["series"]["url"] == urls
    assert not r.verify_series(store, cfg.id).failed


@pytest.mark.parametrize(
    "url, message",
    [
        ('"file:///one.csv"', "list of 2 URLs"),
        ('["file:///one.csv"]', "list of 2 URLs"),
        ('["file:///a.csv", ""]', "list of 2 URLs"),
        ('["file:///a.csv", "file:///b.csv", "file:///c.csv"]', "list of 2 URLs"),
    ],
)
def test_a_mof_url_must_be_a_list_of_two(tmp_path, url, message):
    conf = tmp_path / "series.toml"
    conf.write_text(f'[[series]]\nid = "SYN-MOF"\nsource = "mof"\nkey = "7Y"\nurl = {url}\n')
    with pytest.raises(r.RatesError, match=message):
        r.load_config(conf)


def test_a_single_file_format_refuses_a_url_list(tmp_path):
    conf = tmp_path / "series.toml"
    conf.write_text('[[series]]\nid = "SYN"\nsource = "fred"\nkey = "SYN"\nurl = ["a", "b"]\n')
    with pytest.raises(r.RatesError, match="'url' must be a non-empty string"):
        r.load_config(conf)


def test_a_single_url_manifest_is_unchanged():
    cfg = r.SeriesConfig("SYN-FRED", "fred", "SYN", url="https://example.invalid/x.csv")
    assert cfg.as_json()["url"] == "https://example.invalid/x.csv"
