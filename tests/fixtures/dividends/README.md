# Synthetic index-dividend fixtures

Every file here is **synthetic test data**. Each one has the shape of a
format that `tradedesk-md-dividends` reads (the Nikkei Inc. index CSVs, a
Shiller `ie_data.xls` workbook, and a generic delimited file), but every
value, column title and note is made up. They are not real publications.

- `synthetic_nikkei_dp.csv`: a dividend-points file. 2024-01-01 (Mon) is
  listed without a value, 2024-01-03 has no row, 2024-01-02, -04 and -05
  have values. It ends with a footer line in Shift JIS, as the format does.
- `synthetic_nikkei_tr.csv` and `synthetic_nikkei_pr.csv`: a total-return and
  a price level file. The total return has no 2024-01-03, the price has no
  2024-01-04; both have 2024-01-02, -05 and -08 (2024-01-06 and -07 are a
  weekend).
- `synthetic_generic.csv`: a title line, then `;`-separated columns
  `day;points;released`, with the same days as the dividend-points file.
- `synthetic_shiller.xls`: a `Data` sheet with months 2022-10 to 2023-04.
  2023-02 has no row and 2023-03 has no `D` value. A note row follows the
  data. `make_synthetic_shiller.py` writes it (see its docstring).
- `synthetic_shiller_row_after_note.xls`: a `Data` sheet with a dated row
  after its note row, which the parser must refuse. The same script writes it.
