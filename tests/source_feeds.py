"""Synthetic feeds, one per registered source, for the conformance suite (no network).

Each feed serves the same synthetic ticks in its provider's own wire format and
stubs only the provider's network call, so a run goes through the provider's
real staging, decode and day rules and through the framework unchanged.

A new source registers a feed here; ``test_every_registered_source_has_a_feed``
fails until it does.
"""

from __future__ import annotations

import lzma
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from histdata_fixtures import est_line, zips_by_month

from tradedesk_marketdata.sources import dukascopy as dk
from tradedesk_marketdata.sources import histdata as hd

SYMBOL = "EURUSD"

# (utc time, bid, ask) with prices in 1e-5 points: 108_003 is 1.08003.
TickRow = tuple[datetime, int, int]


def synthetic_ticks(first: date, last: date) -> list[TickRow]:
    """Irregular EURUSD-like ticks over all 24 UTC hours of every weekday in [first, last].

    Deterministic: the golden digests in the conformance suite depend on it.
    """
    out = []
    d = first
    while d <= last:
        if d.weekday() < 5:
            base = datetime(d.year, d.month, d.day, tzinfo=UTC)
            level = 108_000 + (d.toordinal() * 137) % 900
            for i in range(24 * 7):
                ts = base + timedelta(seconds=i * 514 % 86_400, milliseconds=(i * 37) % 1000)
                bid = level + (i * 7919) % 400
                out.append((ts, bid, bid + 3 + i % 5))
        d += timedelta(days=1)
    return sorted(out)


@dataclass
class Feed:
    """A provider's view of the synthetic ticks, installed in place of its network call."""

    name: str
    #: Option values for ``sources.create`` and the equivalent CLI arguments.
    options: dict[str, Any]
    cli_args: list[str]
    #: The ``_sources.jsonl`` scale factor the provider records for these ticks.
    scale_factor: float
    #: One entry per network request the provider made.
    requests: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dukascopy: int32 bi5 hours, divided by --price-divisor 10 into the cache's pips
# ---------------------------------------------------------------------------


def _bi5_hours(ticks: list[TickRow]) -> dict[datetime, bytes]:
    rows: dict[datetime, list[bytes]] = {}
    for ts, bid, ask in ticks:
        hour = ts.replace(minute=0, second=0, microsecond=0)
        ms = int((ts - hour).total_seconds() * 1000)
        rows.setdefault(hour, []).append(struct.pack(">iiiff", ms, ask, bid, 0.0, 0.0))
    return {h: lzma.compress(b"".join(r), format=lzma.FORMAT_ALONE) for h, r in rows.items()}


class _Response:
    def __init__(self, content: bytes) -> None:
        self.status_code = 200
        self.content = content

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None


def dukascopy_feed(monkeypatch, ticks: list[TickRow]) -> Feed:
    """The datafeed's session, answering every hour: its bi5, or an empty 200."""
    feed = Feed("dukascopy", {"price_divisor": 10.0}, ["--price-divisor", "10"], 0.1)
    hours = _bi5_hours(ticks)

    def get(url: str, *, timeout: tuple[float, float]) -> _Response:
        feed.requests.append(url)
        y, m0, d, hh = url.split("/")[-4:]
        hour = datetime(int(y), int(m0) + 1, int(d), int(hh[:2]), tzinfo=UTC)
        return _Response(hours.get(hour, b""))

    monkeypatch.setattr(dk._SESSION, "get", get)
    return feed


# ---------------------------------------------------------------------------
# HistData: decimal quotes in EST month zips, scaled x1e4 by the symbol table
# ---------------------------------------------------------------------------


def histdata_feed(monkeypatch, ticks: list[TickRow]) -> Feed:
    """HistData's month download, serving each EST month's zip (None when it has none)."""
    feed = Feed("histdata", {}, [], 1e4)
    lines = [est_line(ts, f"{b / 1e5:.6f}", f"{a / 1e5:.6f}") for ts, b, a in ticks]
    zips = zips_by_month(lines)

    def fetch(inst, ym, *, timeout, retries):
        feed.requests.append(ym)
        return zips.get(ym)

    monkeypatch.setattr(hd, "_fetch_month_zip", fetch)
    monkeypatch.setattr(hd, "REQUEST_INTERVAL", 0.0)
    return feed


FEEDS: dict[str, Callable[[Any, list[TickRow]], Feed]] = {
    "dukascopy": dukascopy_feed,
    "histdata": histdata_feed,
}
