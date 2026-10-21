# Synthetic rate-series fixtures

Every file here is **synthetic test data**. Each one has the shape of a
public source's published format (FRED, ECB Data Portal, Bank of England
database, Bank of Japan time-series API, RBA statistical table as CSV and as an
`.xls` workbook, RBNZ statistical table as an `.xlsx` workbook, SNB data
portal cube CSV, Bank of Canada Valet JSON, Riksbank SWEA and SWESTR JSON,
Norges Bank SDMX-CSV), but the series and cube codes (`SYNTH...`, `synthcube`,
`SYN`) and every value are made up. They are not real
publications and they name no real series.

Each covers 2024-01-01 (Mon) to 2024-01-05 (Fri) and has, by design:

- 2024-01-01: no value in the source (missing marker, or an absent row),
- 2024-01-03: no row at all (except where noted),
- values on 2024-01-02, 2024-01-04 and 2024-01-05.

The two workbooks are written by `tests/rates_workbooks.py`
(`uv run --with xlwt python tests/rates_workbooks.py`). `synthetic_rba.xls` has
a third column, `SYNTHIDC`, with a `Monthly` frequency, to test the refusal of
a column that is not daily.
