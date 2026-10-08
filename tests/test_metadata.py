import json
from pathlib import Path

from tradedesk_marketdata.metadata import ExportMetadata, now_iso_utc, write_sidecar


def test_now_iso_utc_ends_with_z_and_is_isoish() -> None:
    s = now_iso_utc()
    assert s.endswith("Z")
    # simple sanity: date separator and time separator
    assert "T" in s and ":" in s


def test_write_sidecar_writes_expected_json(tmp_path: Path) -> None:
    out_csv = tmp_path / "EURUSD_5MIN.csv"
    out_csv.write_text("timestamp,open,high,low,close,volume\n")  # placeholder

    meta = ExportMetadata(
        schema_version="1",
        source="dukascopy",
        symbol="EURUSD",
        data_type="candles",
        timestamp_format="iso8601_utc",
        price_divisor=100000.0,
        generated_at="2026-01-03T00:00:00Z",
        params={
            "resample": "5min",
            "price_side": "bid",
            "date_from": "2025-01-01",
            "date_to": "2025-01-02",
        },
    )

    sidecar = write_sidecar(meta, out_csv)
    assert sidecar.exists()

    # expected naming contract: <csv>.meta.json
    assert sidecar.name == "EURUSD_5MIN.csv.meta.json"

    payload = json.loads(sidecar.read_text())
    assert payload["schema_version"] == "1"
    assert payload["symbol"] == "EURUSD"
    assert payload["price_divisor"] == 100000.0
    assert payload["params"]["resample"] == "5min"
