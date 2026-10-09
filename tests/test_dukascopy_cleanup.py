"""The Dukascopy source's bi5 staging-dir cleanup (empty, redundant and all-empty day dirs)."""

from datetime import date
from pathlib import Path

import tradedesk_marketdata.sources.dukascopy as dk

# ---------------------------------------------------------------------------
# _cleanup_empty_day_dirs
# ---------------------------------------------------------------------------


def test_cleanup_removes_empty_day_dirs(tmp_path: Path) -> None:
    symbol = "EURUSD"
    # Create: one empty day dir, one non-empty day dir, one day dir with a file
    empty_dir = tmp_path / symbol / "2025" / "00" / "14"
    empty_dir.mkdir(parents=True)

    non_empty_dir = tmp_path / symbol / "2025" / "00" / "15"
    non_empty_dir.mkdir(parents=True)
    (non_empty_dir / "00h_ticks.bi5").write_bytes(b"data")

    dk._cleanup_empty_day_dirs(tmp_path, symbol)

    assert not empty_dir.exists()
    assert non_empty_dir.exists()


def test_cleanup_removes_redundant_bi5_dir_when_candle_csvs_exist(tmp_path: Path) -> None:
    # A non-empty bi5 day-dir is removed when both daily-candle CSVs
    # for that day exist (the .bi5 are redundant once candles are written).
    symbol = "EURUSD"
    leftover_dir = tmp_path / symbol / "2025" / "00" / "15"
    leftover_dir.mkdir(parents=True)
    (leftover_dir / "00h_ticks.bi5").write_bytes(b"data")
    (leftover_dir / "01h_ticks.bi5").write_bytes(b"data")

    # Candle CSVs are siblings in the month dir: {DD}_bid.csv.zst / {DD}_ask.csv.zst.
    month_dir = tmp_path / symbol / "2025" / "00"
    (month_dir / "15_bid.csv.zst").write_bytes(b"candles")
    (month_dir / "15_ask.csv.zst").write_bytes(b"candles")

    dk._cleanup_empty_day_dirs(tmp_path, symbol)

    assert not leftover_dir.exists(), "redundant bi5 day-dir must be removed once candles exist"
    # The candle CSVs themselves must be left untouched.
    assert (month_dir / "15_bid.csv.zst").read_bytes() == b"candles"
    assert (month_dir / "15_ask.csv.zst").read_bytes() == b"candles"


def test_cleanup_preserves_bi5_dir_when_only_one_candle_csv_exists(tmp_path: Path) -> None:
    # A partial candle pair (only bid written) is NOT a complete day,
    # so the bi5 must be kept for a retry to finish committing it.
    symbol = "EURUSD"
    leftover_dir = tmp_path / symbol / "2025" / "00" / "15"
    leftover_dir.mkdir(parents=True)
    (leftover_dir / "00h_ticks.bi5").write_bytes(b"data")

    month_dir = tmp_path / symbol / "2025" / "00"
    (month_dir / "15_bid.csv.zst").write_bytes(b"candles")  # ask missing

    dk._cleanup_empty_day_dirs(tmp_path, symbol)

    assert leftover_dir.exists(), "bi5 day-dir must be kept when the candle pair is incomplete"


def test_cleanup_removes_all_empty_bi5_dir_when_aged(tmp_path: Path) -> None:
    # A market-closed / no-tick day stages only 0-byte .bi5 (every
    # hour returned no ticks), so no candle CSV is ever produced. Once aged past
    # the partial-commit window the staging dir is pure cruft and must be swept,
    # else it trips the consumer's _check_old_format guard forever.

    symbol = "EURUSD"
    leftover_dir = tmp_path / symbol / "2005" / "01" / "25"  # 2005-02-25 (MM zero-based)
    leftover_dir.mkdir(parents=True)
    (leftover_dir / "22h_ticks.bi5").write_bytes(b"")
    (leftover_dir / "23h_ticks.bi5").write_bytes(b"")
    # No candle CSVs exist for this day.

    dk._cleanup_stale_day_dirs(tmp_path, symbol, today=date(2026, 6, 6))

    assert not leftover_dir.exists(), "aged all-empty bi5 day-dir must be swept"


def test_cleanup_preserves_recent_all_empty_bi5_dir(tmp_path: Path) -> None:
    # An all-empty staging dir younger than the partial-commit window
    # may belong to a same-day in-flight export (early empty hours staged before
    # ticks arrive), so it is left alone.

    symbol = "EURUSD"
    recent_dir = tmp_path / symbol / "2026" / "05" / "05"  # 2026-06-05
    recent_dir.mkdir(parents=True)
    (recent_dir / "00h_ticks.bi5").write_bytes(b"")

    dk._cleanup_stale_day_dirs(
        tmp_path, symbol, today=date(2026, 6, 6), commit_partial_after_days=7
    )

    assert recent_dir.exists(), "recent all-empty bi5 day-dir must be kept for in-flight export"


def test_cleanup_preserves_dir_with_nonempty_bi5_and_no_candles(tmp_path: Path) -> None:
    # A dir that still holds *non-empty* .bi5 (real pending tick data)
    # with no candle pair is a genuine partial — never swept by the all-empty
    # rule, even when aged.

    symbol = "GBPCAD"
    pending_dir = tmp_path / symbol / "2026" / "03" / "07"  # 2026-04-07
    pending_dir.mkdir(parents=True)
    (pending_dir / "08h_ticks.bi5").write_bytes(b"")
    (pending_dir / "09h_ticks.bi5").write_bytes(b"real-ticks")  # one non-empty

    dk._cleanup_stale_day_dirs(tmp_path, symbol, today=date(2026, 6, 6))

    assert pending_dir.exists(), "dir with any non-empty bi5 must be preserved"


def test_cleanup_is_noop_for_missing_symbol_dir(tmp_path: Path) -> None:
    # Should not raise even if symbol dir doesn't exist
    dk._cleanup_empty_day_dirs(tmp_path, "GBPUSD")


def test_cleanup_handles_multiple_months(tmp_path: Path) -> None:
    symbol = "USDJPY"
    empty1 = tmp_path / symbol / "2024" / "11" / "31"
    empty2 = tmp_path / symbol / "2025" / "00" / "01"
    empty1.mkdir(parents=True)
    empty2.mkdir(parents=True)

    dk._cleanup_empty_day_dirs(tmp_path, symbol)

    assert not empty1.exists()
    assert not empty2.exists()
