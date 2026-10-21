"""Synthetic spreadsheet workbooks for the rates tests (M29-R2). Test data only.

:func:`xlsx` writes a minimal Office Open XML workbook with the standard library, the
same bytes every time. ``synthetic_rbnz.xlsx`` is :func:`rbnz_workbook`'s output, and
``synthetic_rba.xls`` is written by :func:`write_rba_xls`, which needs ``xlwt`` (not a
dependency of the package or its tests). To write both fixtures again::

    uv run --with xlwt python tests/rates_workbooks.py
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

FIXTURES = Path(__file__).parent / "fixtures" / "rates"

#: A cell: a str is a shared string, a float or int a number, a date a date serial,
#: ``("inline", text)`` an inline string, ``(type, v)`` a raw ``t``/``<v>`` pair, None empty.
Value = object

_EPOCH_1900 = date(1899, 12, 30)
_EPOCH_1904 = date(1904, 1, 1)


def _col(j: int) -> str:
    s = ""
    j += 1
    while j:
        j, rem = divmod(j - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


def _sheet_xml(rows: list[list[Value]], shared: list[str], epoch: date) -> str:
    out = []
    for i, row in enumerate(rows, 1):
        cells = []
        for j, v in enumerate(row):
            ref = f"{_col(j)}{i}"
            if v is None:
                continue
            if isinstance(v, date):
                cells.append(f'<c r="{ref}" s="1"><v>{(v - epoch).days}</v></c>')
            elif isinstance(v, str):
                if v not in shared:
                    shared.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{shared.index(v)}</v></c>')
            elif isinstance(v, tuple) and v[0] == "inline":
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v[1])}</t></is></c>')
            elif isinstance(v, tuple):
                cells.append(f'<c r="{ref}" t="{v[0]}"><v>{escape(v[1])}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        out.append(f'<row r="{i}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(out)}</sheetData></worksheet>"
    )


def xlsx(sheets: dict[str, list[list[Value]]], *, date1904: bool = False) -> bytes:
    """A workbook with these sheets, in order. The same input gives the same bytes."""
    epoch = _EPOCH_1904 if date1904 else _EPOCH_1900
    shared: list[str] = []
    sheet_xml = [_sheet_xml(rows, shared, epoch) for rows in sheets.values()]
    ns = "http://schemas.openxmlformats.org"
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{ns}/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{k}.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for k in range(1, len(sheets) + 1)
            )
            + '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{ns}/spreadsheetml/2006/main" '
            f'xmlns:r="{ns}/officeDocument/2006/relationships">'
            + ('<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>")
            + "<sheets>"
            + "".join(
                f'<sheet name="{escape(name)}" sheetId="{k}" r:id="rId{k}"/>'
                for k, name in enumerate(sheets, 1)
            )
            + "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{k}" Type="{ns}/officeDocument/2006/relationships/'
                f'worksheet" Target="worksheets/sheet{k}.xml"/>'
                for k in range(1, len(sheets) + 1)
            )
            + f'<Relationship Id="rIdS" Type="{ns}/officeDocument/2006/relationships/'
            'sharedStrings" Target="sharedStrings.xml"/>'
            f'<Relationship Id="rIdT" Type="{ns}/officeDocument/2006/relationships/'
            'styles" Target="styles.xml"/></Relationships>'
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{ns}/spreadsheetml/2006/main">'
            '<fonts count="1"><font/></fonts><fills count="1"><fill/></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
            "</styleSheet>"
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{ns}/spreadsheetml/2006/main" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">'
            + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)
            + "</sst>"
        ),
    }
    for k, xml in enumerate(sheet_xml, 1):
        files[f"xl/worksheets/sheet{k}.xml"] = xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def _d(day: int) -> date:
    return date(2024, 1, day)


def rbnz_sheets() -> dict[str, list[list[Value]]]:
    """The RBNZ table layout with synthetic series (``SYNTH...``) and made-up values."""
    return {
        "Data": [
            [None, "Synthetic group", "Synthetic group"],
            [None, "Synthetic rate A; test only", "Synthetic rate B; test only"],
            ["Notes", None, None],
            ["Unit", "%pa", "%pa"],
            ["Series Id", "SYNTH.A", "SYNTH.B"],
            [_d(1), None, 9.9],
            [_d(2), 5.5, 9.9],
            [_d(4), 5.5, 9.9],
            [_d(5), 5.25, 3],
        ],
        "Table Description": [
            ["Published By", "Synthetic publisher; test only"],
            ["Table", "Synthetic test table"],
            ["Published Date", _d(6)],
        ],
        "Series Definitions": [
            ["Group", "Series", "Series Id", "Unit"],
            ["Synthetic group", "Synthetic rate A; test only", "SYNTH.A", "%pa"],
            ["Synthetic group", "Synthetic rate B; test only", "SYNTH.B", "%pa"],
        ],
    }


def rbnz_workbook() -> bytes:
    return xlsx(rbnz_sheets())


def write_rba_xls(path: Path) -> None:  # pragma: no cover - run by hand, needs xlwt
    """The RBA table layout as an .xls workbook with date cells. Synthetic values only."""
    import xlwt  # type: ignore[import-not-found]

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Data")
    dates = xlwt.easyxf(num_format_str="DD-MMM-YYYY")
    head = [
        ["S1 SYNTHETIC TEST TABLE"],
        ["Title", "Synthetic A", "Synthetic B", "Synthetic C"],
        [
            "Description",
            "Synthetic rate A; test only",
            "Synthetic rate B; test only",
            "Synthetic rate C; test only",
        ],
        ["Frequency", "Daily", "Daily", "Monthly"],
        ["Type", "Original", "Original", "Original"],
        ["Units", "Per cent", "Per cent", "Per cent"],
        [],
        [],
        ["Source", "SYN", "SYN", "SYN"],
    ]
    for i, row in enumerate(head):
        for j, v in enumerate(row):
            ws.write(i, j, v)
    ws.write(9, 0, "Publication date")
    for j in (1, 2, 3):
        ws.write(9, j, _d(6), dates)
    for j, v in enumerate(["Series ID", "SYNTHIDA", "SYNTHIDB", "SYNTHIDC"]):
        ws.write(10, j, v)
    data = [(_d(1), None, 9.9), (_d(2), 4.35, 9.9), (_d(4), 4.35, 9.9), (_d(5), 4.4, None)]
    for i, (d, a, b) in enumerate(data, 11):
        ws.write(i, 0, d, dates)
        if a is not None:
            ws.write(i, 1, a)
        if b is not None:
            ws.write(i, 2, b)
    notes = wb.add_sheet("Notes")
    notes.write(0, 0, "Synthetic test workbook; not a real publication.")
    wb.save(str(path))


if __name__ == "__main__":  # pragma: no cover
    (FIXTURES / "synthetic_rbnz.xlsx").write_bytes(rbnz_workbook())
    write_rba_xls(FIXTURES / "synthetic_rba.xls")
