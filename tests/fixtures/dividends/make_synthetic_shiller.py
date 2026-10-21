"""Write the synthetic .xls fixtures: the shape of a Shiller ie_data.xls, made-up values.

Run with: uv run --with xlwt python tests/fixtures/dividends/make_synthetic_shiller.py
The values are synthetic test data, not a real publication.
"""

from pathlib import Path

import xlwt

book = xlwt.Workbook()
book.add_sheet("Disclaimer").write(0, 0, "Synthetic fixture: test data only.")
sh = book.add_sheet("Data")
sh.write(1, 0, "Synthetic monthly data in the shape of ie_data.xls")
for c, text in enumerate(("Date", "P", "D", "E")):
    sh.write(7, c, text)
# (YYYY.MM, P, D, E); 2023.02 has no row and 2023.03 has no D.
rows = [
    (2022.10, 10.0, 1.25, 2.0),
    (2022.11, 10.5, 1.5, 2.0),
    (2022.12, 11.0, 1.5, 2.1),
    (2023.01, 11.5, 1.75, 2.1),
    (2023.03, 12.0, None, None),
    (2023.04, 12.5, 2.0, None),
]
for r, (d, p, dv, e) in enumerate(rows, 8):
    sh.write(r, 0, d)
    sh.write(r, 1, p)
    if dv is not None:
        sh.write(r, 2, dv)
    if e is not None:
        sh.write(r, 3, e)
sh.write(8 + len(rows), 1, "synthetic note row")
book.save(str(Path(__file__).with_name("synthetic_shiller.xls")))

# A workbook with a dated row after the note row: the parser must refuse it.
bad = xlwt.Workbook()
sh = bad.add_sheet("Data")
for c, text in enumerate(("Date", "P", "D")):
    sh.write(0, c, text)
for r, d in enumerate((2023.01, 2023.02), 1):
    sh.write(r, 0, d)
    sh.write(r, 2, 1.0)
sh.write(3, 1, "synthetic note row")
sh.write(4, 0, 2023.03)
bad.save(str(Path(__file__).with_name("synthetic_shiller_row_after_note.xls")))
