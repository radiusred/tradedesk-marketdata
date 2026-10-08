"""Tests for scripts/audit_fx_scale.py — FX scale-corruption envelope checker.

The audit script lives in ``scripts/`` (it's an operator tool, not a public
module) so we import it through a path hack — the same approach the script
itself uses to ship as a single-file tool.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

from tradedesk_marketdata.normalize import _write_zst

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_fx_scale.py"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_fx_scale", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _write_day(cache: Path, symbol: str, year: int, month: int, day: int, close: float) -> None:
    """Write a minimal candle file at ``<cache>/<symbol>/<year>/<MM-1>/<DD>_<side>.csv.zst``."""
    month0 = month - 1
    base = pd.Timestamp(year=year, month=month, day=day, hour=0, tz="UTC")
    idx = pd.date_range(base, periods=3, freq="1min")
    for side in ("bid", "ask"):
        df = pd.DataFrame(
            {
                "open": [close] * 3,
                "high": [close] * 3,
                "low": [close] * 3,
                "close": [close] * 3,
                "volume": [1.0] * 3,
            },
            index=idx,
        )
        path = cache / symbol / f"{year}" / f"{month0:02d}" / f"{day:02d}_{side}.csv.zst"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_zst(df, path)


def test_envelope_flags_high_and_low(audit_mod, tmp_path, capsys):
    cache = tmp_path / "cache"
    _write_day(cache, "NZDUSD", 2024, 1, 2, 0.65)        # ok
    _write_day(cache, "NZDUSD", 2024, 1, 3, 65000.0)     # hi (×100000 corruption)
    _write_day(cache, "NZDUSD", 2024, 1, 4, 0.001)       # lo (sub-envelope)

    sys.argv = ["audit_fx_scale.py", "NZDUSD", "--cache-dir", str(cache)]
    rc = audit_mod.main()
    assert rc == 1  # non-zero when flagged
    out = capsys.readouterr().out
    assert "unique flagged calendar dates: 2" in out
    assert "2024-01-03" in out
    assert "2024-01-04" in out


def test_envelope_clean_returns_zero(audit_mod, tmp_path, capsys):
    cache = tmp_path / "cache"
    _write_day(cache, "NZDUSD", 2024, 1, 2, 0.65)
    _write_day(cache, "NZDUSD", 2024, 1, 3, 0.66)

    sys.argv = ["audit_fx_scale.py", "NZDUSD", "--cache-dir", str(cache)]
    rc = audit_mod.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "unique flagged calendar dates: 0" in out


def test_print_dates_emits_iso_dates(audit_mod, tmp_path, capsys):
    cache = tmp_path / "cache"
    _write_day(cache, "NZDUSD", 2024, 1, 3, 65000.0)
    _write_day(cache, "NZDUSD", 2024, 5, 15, 70000.0)

    sys.argv = ["audit_fx_scale.py", "NZDUSD", "--cache-dir", str(cache), "--print-dates"]
    rc = audit_mod.main()
    assert rc == 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines == ["2024-01-03", "2024-05-15"]
