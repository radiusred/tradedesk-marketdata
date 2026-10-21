"""tradedesk-md-rates (M29-R2): publisher spreadsheet workbooks as rate sources.

``rba-xls`` reads an RBA statistical-table workbook (``.xls``), ``rbnz`` an RBNZ
statistical-table workbook (``.xlsx``). Every workbook here is labelled synthetic data
(``tests/fixtures/rates/README.md``, ``tests/rates_workbooks.py``): the publishers' layouts
with made-up series codes and values. No test makes a network request.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from rates_workbooks import FIXTURES, rbnz_sheets, rbnz_workbook, xlsx

from tradedesk_marketdata import rates as r
from tradedesk_marketdata import rates_cli

RBA_XLS = r.SeriesConfig("SYN-RBA-XLS", "rba-xls", "s1hist/SYNTHIDA")
RBNZ = r.SeriesConfig("SYN-RBNZ", "rbnz", "hs1-daily/SYNTH.A")

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in range(1, 6))


def rba_xls() -> bytes:
    return (FIXTURES / "synthetic_rba.xls").read_bytes()


def rbnz() -> bytes:
    return (FIXTURES / "synthetic_rbnz.xlsx").read_bytes()


def clock():
    stamps = iter(f"2024-01-{d:02d}T12:00:00+00:00" for d in range(6, 32))
    return lambda: next(stamps)


def fetch(store: Path, cfg: r.SeriesConfig, body: bytes) -> r.FetchResult:
    return r.fetch_series(store, cfg, get=lambda url: r.Response(200, body), now=clock())


def csv_rows(path: Path) -> list[list[str]]:
    return [line.split(",") for line in path.read_text().splitlines()[1:]]


def rbnz_with(data: list[list[object]], **kw) -> bytes:
    """The synthetic RBNZ workbook with other data rows under the same header rows."""
    sheets = rbnz_sheets()
    sheets["Data"] = sheets["Data"][:5] + data
    return xlsx(sheets, **kw)


# ---------------------------------------------------------------------------
# The fixtures


def test_the_committed_rbnz_fixture_is_the_generator_output():
    assert rbnz() == rbnz_workbook()


# ---------------------------------------------------------------------------
# rba-xls


def test_rba_xls_parses_the_same_table_as_the_rba_csv():
    rows = r.parse(rba_xls(), RBA_XLS)
    csv_rows_ = r.parse(
        (FIXTURES / "synthetic_rba.csv").read_bytes(),
        r.SeriesConfig("SYN-RBA", "rba", "s1/SYNTHIDA"),
    )
    assert sorted(rows) == sorted(csv_rows_) == [D1, D2, D4, D5]
    assert rows[D1].value is None  # an empty cell: listed without a value
    assert {(x.unit, x.published) for x in rows.values()} == {("Per cent", "2024-01-06")}
    # A .xls number has no stored text: the shortest decimal that reads back the same.
    assert [rows[d].value for d in (D2, D4, D5)] == ["4.35", "4.35", "4.4"]


def test_rba_xls_picks_the_column_by_series_id_and_a_short_row_has_no_value():
    rows = r.parse(rba_xls(), r.SeriesConfig("X", "rba-xls", "s1hist/SYNTHIDB"))
    assert rows[D1].value == "9.9"
    assert rows[D5].value is None


@pytest.mark.parametrize(
    "key, body, message",
    [
        ("s1hist/SYNTHIDC", None, "series SYNTHIDC has frequency 'Monthly', not Daily"),
        ("s1hist/OTHERID", None, "'OTHERID' is not in the table"),
        ("s1hist/SYNTHIDA", b"<html>maintenance</html>", "not an .xls workbook"),
        ("s1hist/SYNTHIDA", rbnz_workbook(), "not an .xls workbook"),
    ],
)
def test_rba_xls_refuses(key, body, message):
    with pytest.raises(r.RatesError, match=message):
        r.parse(rba_xls() if body is None else body, r.SeriesConfig("X", "rba-xls", key))


@pytest.mark.parametrize(
    "x, text",
    [
        (4.35, "4.35"),
        (3.0, "3"),
        (0.1 + 0.2, "0.30000000000000004"),
        (1e-05, "0.00001"),
        (1e16, "10000000000000000"),
        (-0.25, "-0.25"),
    ],
)
def test_a_stored_double_is_written_as_its_shortest_decimal(x, text):
    assert r._float_text(x, "test") == text


def test_a_stored_double_that_is_not_finite_is_refused():
    with pytest.raises(r.RatesError, match="not finite"):
        r._float_text(float("inf"), "test")


# ---------------------------------------------------------------------------
# rbnz


def test_rbnz_parses_its_synthetic_workbook():
    rows = r.parse(rbnz(), RBNZ)
    assert sorted(rows) == [D1, D2, D4, D5]
    assert rows[D1].value is None
    assert [rows[d].value for d in (D2, D4, D5)] == ["5.5", "5.5", "5.25"]
    assert {(x.unit, x.published) for x in rows.values()} == {("%pa", "2024-01-06")}
    other = r.parse(rbnz(), r.SeriesConfig("X", "rbnz", "hs1-daily/SYNTH.B"))
    assert other[D5].value == "3"  # an integral number, as stored


def test_rbnz_keeps_the_stored_number_text():
    body = rbnz_with(
        [
            [D2, ("n", "5.50")],
            [D4, ("n", "5.5000000000000001E-2")],
            [D5, ("n", "  5.25 ")],
        ]
    )
    rows = r.parse(body, RBNZ)
    assert [rows[d].value for d in (D2, D4, D5)] == ["5.50", "0.055000000000000001", "5.25"]


def test_rbnz_reads_inline_strings_error_cells_and_iso_date_cells():
    sheets = rbnz_sheets()
    sheets["Data"][4] = [("inline", "Series Id"), ("inline", "SYNTH.A")]
    sheets["Data"] = sheets["Data"][:5] + [
        [("d", "2024-01-02T00:00:00"), ("e", "#N/A")],
        [D4, ("str", "5.5")],
        [D5, 5.25],
    ]
    rows = r.parse(xlsx(sheets), RBNZ)
    assert rows[D2].value is None and rows[D4].value == "5.5"


def test_rbnz_reads_the_1904_date_system():
    rows = r.parse(rbnz_with([[D2, 5.5], [D4, 5.5], [D5, 5.25]], date1904=True), RBNZ)
    assert sorted(rows) == [D2, D4, D5]


@pytest.mark.parametrize(
    "body, message",
    [
        (b"<html>access restricted</html>", "not an .xlsx workbook"),
        (rba_xls(), "not an .xlsx workbook"),
        (rbnz_with([[D2, 5.5], [("n", "45295.5"), 5.5]]), "not a daily date"),
        (rbnz_with([[D2, 5.5], [D4, "five"]]), "not a decimal number"),
        (rbnz_with([[D2, 5.5], [D4, ("n", "NaN")]]), "not finite"),
        (rbnz_with([[D2, 5.5], [D4, ("b", "1")]]), "boolean cell is not a value"),
        (rbnz_with([[D2, 5.5], ["2024-01-04", 5.5]]), "is not a date"),
        (rbnz_with([[D2, 5.5], [D2, 5.5]]), "more than once"),
        (rbnz_with([[date(2024, m, 1), 5.5] for m in (1, 2, 3, 4)]), r"median 3\d days apart"),
        (rbnz_with([[D2, 5.5]]), "one dated row"),
        (xlsx({"Data": [["Unit", "%pa"], ["Series", "SYNTH.A"]]}), "no 'Series Id' row"),
    ],
)
def test_rbnz_refuses(body, message):
    with pytest.raises(r.RatesError, match=message):
        r.parse(body, RBNZ)


def test_rbnz_refuses_a_series_not_in_the_table():
    with pytest.raises(r.RatesError, match="'SYNTH.Z' is not in the table"):
        r.parse(rbnz(), r.SeriesConfig("X", "rbnz", "hs1-daily/SYNTH.Z"))


def test_rbnz_without_a_table_description_has_no_published_date():
    sheets = rbnz_sheets()
    del sheets["Table Description"]
    assert {x.published for x in r.parse(xlsx(sheets), RBNZ).values()} == {None}


# ---------------------------------------------------------------------------
# Addressing and configuration


@pytest.mark.parametrize(
    "cfg, url",
    [
        (RBA_XLS, "https://www.rba.gov.au/statistics/tables/xls-hist/s1hist.xls"),
        (
            RBNZ,
            "https://www.rbnz.govt.nz/-/media/project/sites/rbnz/files/statistics/series/"
            "s/s1/hs1-daily.xlsx",
        ),
        (
            r.SeriesConfig("X", "rbnz", "synthetic/SYNTH.A", url="file:///tmp/x.xlsx"),
            "file:///tmp/x.xlsx",
        ),
    ],
)
def test_each_workbook_format_addresses_its_series(cfg, url):
    assert cfg.resolved_url() == url


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "series.toml"
    p.write_text(text)
    return p


@pytest.mark.parametrize(
    "text, message",
    [
        ('[[series]]\nid="A"\nsource="rba-xls"\nkey="NOSLASH"\n', "file/series ID"),
        ('[[series]]\nid="A"\nsource="rbnz"\nkey="NOSLASH"\n', "file/Series Id"),
        ('[[series]]\nid="A"\nsource="rbnz"\nkey="synthetic/SYNTH.A"\n', "set 'url'"),
    ],
)
def test_load_config_refuses_a_bad_workbook_key(tmp_path, text, message):
    with pytest.raises(r.RatesError, match=message):
        r.load_config(write(tmp_path, text))


def test_an_rbnz_file_without_a_default_url_loads_with_a_url(tmp_path):
    text = '[[series]]\nid="A"\nsource="rbnz"\nkey="synthetic/SYNTH.A"\nurl="file:///x.xlsx"\n'
    assert r.load_config(write(tmp_path, text))[0].url == "file:///x.xlsx"


# ---------------------------------------------------------------------------
# The store: the workbook is the raw file, verify reproduces the parsed files


@pytest.mark.parametrize("cfg, body, ext", [(RBA_XLS, rba_xls(), "xls"), (RBNZ, rbnz(), "xlsx")])
def test_fetch_keeps_the_workbook_and_verify_reproduces_the_files(tmp_path, cfg, body, ext):
    res = fetch(tmp_path, cfg, body)
    sdir = tmp_path / "_reference" / "rates" / cfg.id
    sha = hashlib.sha256(body).hexdigest()
    assert res.status == "updated" and res.rows == 3
    assert (sdir / "_raw" / f"{sha}.{ext}").read_bytes() == body
    (rec,) = [json.loads(x) for x in (sdir / "_fetches.jsonl").read_text().splitlines()]
    assert rec["raw"] == f"_raw/{sha}.{ext}" and rec["sha256"] == sha
    # Never filled: 2024-01-01 is listed without a value, 2024-01-03 has no row.
    assert csv_rows(sdir / "gaps.csv") == [
        ["2024-01-01", "missing_in_source"],
        ["2024-01-03", "no_row"],
    ]
    assert json.loads((sdir / "manifest.json").read_text())["parser"] == {
        "format": cfg.source,
        "version": 1,
    }
    rep = r.verify_series(tmp_path, cfg.id)
    assert rep.findings == [] and rep.warnings == []
    assert fetch(tmp_path, cfg, body).status == "unchanged"


def test_verify_finds_a_changed_workbook(tmp_path):
    fetch(tmp_path, RBNZ, rbnz())
    (raw,) = (tmp_path / "_reference/rates/SYN-RBNZ/_raw").iterdir()
    raw.write_bytes(raw.read_bytes() + b"\0")
    (finding,) = r.verify_series(tmp_path, RBNZ.id).findings
    assert "does not match the record" in finding


def test_a_download_that_is_not_a_workbook_writes_nothing(tmp_path):
    fetch(tmp_path, RBNZ, rbnz())
    sdir = tmp_path / "_reference/rates/SYN-RBNZ"
    before = {p: p.read_bytes() for p in sdir.rglob("*") if p.is_file()}
    with pytest.raises(r.RatesError, match="not an .xlsx workbook"):
        fetch(tmp_path, RBNZ, b"<html>access restricted</html>")
    assert {p: p.read_bytes() for p in sdir.rglob("*") if p.is_file()} == before


# ---------------------------------------------------------------------------
# A local file: a workbook that a person downloaded


def test_download_reads_a_file_url(tmp_path):
    path = tmp_path / "synthetic.xlsx"
    path.write_bytes(rbnz())
    assert r.download(path.as_uri()) == r.Response(None, rbnz())
    with pytest.raises(r.RatesError, match="missing.xlsx"):
        r.download((tmp_path / "missing.xlsx").as_uri())


def test_cli_fetches_a_file_url_and_records_no_http_status(tmp_path, capsys):
    path = tmp_path / "synthetic workbook.xlsx"
    path.write_bytes(rbnz())
    cfg = write(
        tmp_path,
        f'[[series]]\nid = "SYN-RBNZ"\nsource = "rbnz"\nkey = "hs1-daily/SYNTH.A"\n'
        f'url = "{path.as_uri()}"\n',
    )
    store = tmp_path / "store"
    store.mkdir()
    assert rates_cli.main(["fetch", "--store", str(store), "--config", str(cfg)]) == 0
    assert "SYN-RBNZ: updated: 3 observation(s), 2 gap(s)" in capsys.readouterr().out
    sdir = store / "_reference/rates/SYN-RBNZ"
    (rec,) = [json.loads(x) for x in (sdir / "_fetches.jsonl").read_text().splitlines()]
    assert rec["url"] == path.as_uri() and rec["http_status"] is None
    assert json.loads((sdir / "manifest.json").read_text())["fetches"] == [rec]
    assert rates_cli.main(["verify", "--store", str(store)]) == 0
    assert "SYN-RBNZ: ok (1 fetch(es))" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# start: a workbook is one whole file, so rows dated before start are not read


def test_a_text_cell_that_is_not_a_decimal_refuses_the_series_with_its_row():
    body = rbnz_with([[D1, "5.25 to 5.50"], [D2, 5.5], [D4, 5.5], [D5, 5.25]])
    with pytest.raises(r.RatesError, match=r"rbnz row 6: value '5.25 to 5.50' is not a decimal"):
        r.parse(body, RBNZ)


def test_start_drops_the_rows_before_it_in_a_workbook(tmp_path):
    body = rbnz_with([[D1, "5.25 to 5.50"], [D2, 5.5], [D4, 5.5], [D5, 5.25]])
    cfg = r.SeriesConfig("SYN-RBNZ", "rbnz", RBNZ.key, start=D2)
    assert sorted(r.parse(body, cfg)) == [D2, D4, D5]
    xls = r.SeriesConfig("SYN-RBA-XLS", "rba-xls", RBA_XLS.key, start=D2)
    assert sorted(r.parse(rba_xls(), xls)) == [D2, D4, D5]
    fetch(tmp_path, xls, rba_xls())
    sdir = tmp_path / "_reference/rates/SYN-RBA-XLS"
    assert csv_rows(sdir / "gaps.csv") == [["2024-01-03", "no_row"]]  # D1 is not a gap
    assert r.verify_series(tmp_path, xls.id).findings == []


def test_a_start_after_every_row_leaves_no_value():
    cfg = r.SeriesConfig("SYN-RBNZ", "rbnz", RBNZ.key, start=date(2024, 2, 1))
    with pytest.raises(r.RatesError, match="holds no value"):
        r.parse(rbnz(), cfg)


def test_the_rba_csv_still_reads_every_row_with_a_start():
    cfg = r.SeriesConfig("SYN-RBA", "rba", "s1/SYNTHIDA", start=D2)
    assert D1 in r.parse((FIXTURES / "synthetic_rba.csv").read_bytes(), cfg)


def test_rba_xls_without_xlrd_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "xlrd", None)  # import xlrd raises ImportError
    with pytest.raises(r.RatesError, match=r"tradedesk-marketdata\[xls\]"):
        r.parse(rba_xls(), RBA_XLS)
