"""Shared Zstandard-compressed daily-candle CSV I/O helpers.

Both :mod:`tradedesk_marketdata.normalize` and :mod:`tradedesk_marketdata.rescale`
read and rewrite the same ``{DD}_{bid,ask}.csv.zst`` daily candle files, so the
read/write round-trip lives here to keep a single implementation.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import zstandard as zstd


def read_zst(path: Path) -> pd.DataFrame | None:
    """Read a Zstandard-compressed candle CSV into a DataFrame.

    Returns ``None`` if the file is missing or cannot be decoded/parsed.
    """
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f_in, dctx.stream_reader(f_in) as reader:
            df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")
    except (OSError, UnicodeDecodeError, ValueError, zstd.ZstdError, pd.errors.ParserError):
        return None


def write_zst(df: pd.DataFrame, path: Path) -> None:
    """Atomically write *df* as a Zstandard-compressed candle CSV to *path*."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    out = df.copy()
    out.index.name = "timestamp"
    cctx = zstd.ZstdCompressor(level=3)
    compressed = cctx.compress(out.reset_index().to_csv(index=False).encode("utf-8"))
    tmp.write_bytes(compressed)
    tmp.replace(path)
