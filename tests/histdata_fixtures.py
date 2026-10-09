"""Builders for synthetic HistData month zips (no network)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta

from tradedesk_marketdata.sources import histdata as hd


def est_line(ts_utc: datetime, bid: str, ask: str) -> str:
    """One HistData tick line: EST (UTC-5, no DST) timestamp, decimal bid/ask, volume 0."""
    est = ts_utc.astimezone(UTC) - timedelta(hours=5)
    stamp = est.strftime("%Y%m%d %H%M%S") + f"{est.microsecond // 1000:03d}"
    return f"{stamp},{bid},{ask},0"


def month_zip(lines: list[str], name: str = "DAT_ASCII_TEST_T_000000") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}.csv", "\n".join(lines) + "\n")
        zf.writestr(f"{name}.txt", "HistData.com (c) status report\n")
    return buf.getvalue()


def weekday_lines(first: date, last: date, *, price: float = 2055.25) -> list[str]:
    """Two ticks an hour, 00:00-21:00 UTC on weekdays; nothing at weekends."""
    lines = []
    d = first
    while d <= last:
        if d.weekday() < 5:
            for h in range(22):
                t = datetime(d.year, d.month, d.day, h, 0, 1, tzinfo=UTC)
                lines.append(est_line(t, f"{price:.6f}", f"{price + 0.5:.6f}"))
                t = t + timedelta(minutes=30, milliseconds=250)
                lines.append(est_line(t, f"{price + 0.25:.6f}", f"{price + 0.75:.6f}"))
        d += timedelta(days=1)
    return lines


def zips_by_month(lines: list[str]) -> dict[tuple[int, int], bytes]:
    """Split HistData lines into per-EST-month zips, as HistData serves them."""
    groups: dict[tuple[int, int], list[str]] = {}
    for line in lines:
        ym = (int(line[0:4]), int(line[4:6]))
        groups.setdefault(ym, []).append(line)
    return {ym: month_zip(g) for ym, g in groups.items()}


class FakeFetcher:
    """Stands in for ``histdata._fetch_month_zip``; records the months requested."""

    def __init__(self, zips: dict[tuple[int, int], bytes], fail: set | None = None):
        self.zips = zips
        self.fail = fail or set()
        self.calls: list[tuple[int, int]] = []

    def __call__(self, inst, ym, *, timeout, retries):
        self.calls.append(ym)
        if ym in self.fail:
            raise hd.MonthFetchError(f"{ym}: simulated failure")
        return self.zips.get(ym)
