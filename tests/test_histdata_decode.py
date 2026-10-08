"""
HistData decoding, symbol table, month arithmetic and the download handshake.
No test here touches the network.
"""

import threading
import time
import zipfile
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from histdata_fixtures import month_zip

from tradedesk_marketdata import histdata as hd
from tradedesk_marketdata.cancel import cancellation


@pytest.fixture(autouse=True)
def _clear_cancellation():
    cancellation.clear()
    yield
    cancellation.clear()


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


def test_decode_converts_fixed_est_to_utc_all_year_round():
    # EST without daylight saving is UTC-5 in winter *and* in summer.
    csv = b"20150101 180000497,2055.000000,2055.250000,0\n20150701 120000000,2.0,2.5,0\n"
    ticks = hd.decode_ticks(csv, scale=1.0)
    assert list(ticks.index) == [
        pd.Timestamp("2015-01-01 23:00:00.497", tz="UTC"),
        pd.Timestamp("2015-07-01 17:00:00", tz="UTC"),
    ]


def test_decode_reads_bid_then_ask_and_zero_volume():
    ticks = hd.decode_ticks(b"20150105 100000000,1.5,1.6,7\n", scale=1.0)
    assert ticks["bid"].iloc[0] == 1.5
    assert ticks["ask"].iloc[0] == 1.6
    assert ticks["bid_vol"].iloc[0] == 0.0 and ticks["ask_vol"].iloc[0] == 0.0


@pytest.mark.parametrize(
    "symbol, quote, cache_value",
    [
        ("EURUSD", "1.103660", 11036.6),
        ("GBPJPY", "179.601000", 17960.1),
        ("XAUUSD", "2063.625000", 206362.5),
        ("USA500IDXUSD", "4774.361000", 4774.361),
        ("DEUIDXEUR", "13174.675000", 13174.675),
    ],
)
def test_decode_scales_into_the_cache_raw_units(symbol, quote, cache_value):
    scale = hd.lookup(symbol).scale
    ticks = hd.decode_ticks(f"20200102 030000000,{quote},{quote},0\n".encode(), scale=scale)
    # Exact equality: the scaled value is the same float the cache holds, with
    # no binary residue (1.10366 * 1e4 alone would be 11036.599999999999).
    assert ticks["bid"].iloc[0] == cache_value
    assert repr(float(ticks["bid"].iloc[0])) == repr(cache_value)


def test_decode_skips_malformed_lines_and_keeps_the_rest(caplog):
    csv = b"\n".join(
        [
            b"DateTime,Bid,Ask,Volume",  # header
            b"20150105 100000000,1.5,1.6,0",  # good
            b"20150105 1000000,1.5,1.6,0",  # truncated time field
            b"20150105 100001000,abc,1.6,0",  # non-numeric bid
            b"20150105 100002000,1.5,,0",  # empty ask
            b"20150105 100003000,-1.5,1.6,0",  # non-positive price
            b"20150105 100004000,1.5,1.6,0,9",  # too many fields
            b"20150105",  # too few fields
            b"",  # blank
            b"20150105 100005000,1.7,1.8,0",  # good
        ]
    )
    with caplog.at_level("WARNING"):
        ticks = hd.decode_ticks(csv, scale=1.0)
    assert list(ticks["bid"]) == [1.5, 1.7]
    assert "skipped 7 malformed tick line(s) of 9" in caplog.text


def test_decode_refuses_a_file_without_one_good_line():
    with pytest.raises(ValueError):
        hd.decode_ticks(b"garbage\nmore garbage\n", scale=1.0)


def test_decode_sorts_by_time_and_keeps_order_within_a_timestamp():
    csv = b"20150105 100001000,3,3,0\n20150105 100000000,1,1,0\n20150105 100000000,2,2,0\n"
    ticks = hd.decode_ticks(csv, scale=1.0)
    assert list(ticks["bid"]) == [1.0, 2.0, 3.0]


def test_read_month_ticks_takes_the_csv_out_of_the_zip():
    ticks = hd._read_month_ticks(month_zip(["20150105 100000000,1.5,1.6,0"]), scale=1e4)
    assert ticks["bid"].iloc[0] == 15000.0


def test_read_month_ticks_rejects_a_corrupt_zip():
    with pytest.raises(zipfile.BadZipFile):
        hd._read_month_ticks(b"PK\x03\x04 not really a zip", scale=1.0)


# ---------------------------------------------------------------------------
# Symbol table
# ---------------------------------------------------------------------------


def test_symbol_table_maps_cache_symbols_to_histdata_names():
    assert hd.lookup("USA500IDXUSD").name == "SPXUSD"
    assert hd.lookup("DEUIDXEUR").name == "GRXEUR"
    assert hd.lookup("GBRIDXGBP").name == "UKXGBP"
    assert hd.lookup("JPNIDXJPY").name == "JPXJPY"
    assert hd.lookup("AUSIDXAUD").name == "AUXAUD"
    assert hd.lookup("XAUUSD").name == "XAUUSD"
    assert hd.lookup("eur/usd").name == "EURUSD"  # same normalisation as Dukascopy symbols


def test_unmapped_symbol_is_refused():
    with pytest.raises(hd.UnmappedSymbolError, match="BTCUSD has no HistData mapping"):
        hd.lookup("BTCUSD")


def test_every_mapping_has_a_power_of_ten_scale_and_a_sane_first_month():
    for symbol, inst in hd.HISTDATA_SYMBOLS.items():
        assert inst.scale in (1.0, 1e2, 1e4), symbol
        assert (2000, 1) <= inst.first_month <= (2010, 11), symbol


# ---------------------------------------------------------------------------
# Months and days
# ---------------------------------------------------------------------------


def test_first_of_month_needs_two_month_files():
    # 00:00-05:00 UTC on the 1st is 19:00-24:00 EST on the last day of the previous month.
    assert hd._months_for_day(date(2015, 2, 1)) == [(2015, 1), (2015, 2)]
    assert hd._months_for_day(date(2015, 2, 2)) == [(2015, 2)]
    assert hd._months_for_day(date(2015, 1, 1)) == [(2014, 12), (2015, 1)]


def test_a_month_file_covers_the_1st_to_the_next_1st():
    days = hd._days_for_month((2015, 1))
    assert days[0] == date(2015, 1, 1) and days[-1] == date(2015, 2, 1)
    assert len(days) == 32
    start, end = hd._month_span((2015, 12))
    assert start == datetime(2015, 12, 1, 5, tzinfo=UTC)
    assert end == datetime(2016, 1, 1, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Download handshake
# ---------------------------------------------------------------------------

_PAGE = """<html><body>
<form id="other" action="/x"><input type="hidden" name="cmd" value="nope" /></form>
<form id="file_down" name="file_down" method="POST" action="/get.php">
  <input type="hidden" name="tk" id="tk" value="{tk}" />
  <input type="hidden" name="date" id="date" value="2015" />
  <input type="hidden" name="datemonth" id="datemonth" value="201501" />
  <input type="hidden" name="platform" id="platform" value="ASCII" />
  <input type="hidden" name="timeframe" id="timeframe" value="T" />
  <input type="hidden" name="fxpair" id="fxpair" value="SPXUSD" />
</form></body></html>"""


class _Resp:
    def __init__(self, *, text: str = "", content: bytes = b"", status: int = 200):
        self.text = text
        self.content = content
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


def test_scrape_download_form_reads_only_the_download_form():
    fields = hd._scrape_download_form(_PAGE.format(tk="abc123"))
    assert fields == {
        "tk": "abc123",
        "date": "2015",
        "datemonth": "201501",
        "platform": "ASCII",
        "timeframe": "T",
        "fxpair": "SPXUSD",
    }
    assert hd._scrape_download_form("<html>no form</html>") is None


def test_fetch_scrapes_a_fresh_token_and_posts_the_form_with_a_referer(monkeypatch):
    payload = month_zip(["20150105 100000000,1.5,1.6,0"])
    seen: dict = {}

    def fake_get(url, timeout):
        seen["page"] = url
        seen["timeout"] = timeout
        return _Resp(text=_PAGE.format(tk="tok-1"))

    def fake_post(url, data, headers, timeout, stream):
        seen.update(post_url=url, data=data, headers=headers)
        return _Resp(content=payload)

    monkeypatch.setattr(hd._SESSION, "get", fake_get)
    monkeypatch.setattr(hd._SESSION, "post", fake_post)

    got = hd._fetch_month_zip(hd.lookup("USA500IDXUSD"), (2015, 1), timeout=(3.0, 30.0))

    assert got == payload
    assert seen["page"].endswith("/tick-data-quotes/spxusd/2015/1")
    assert seen["timeout"] == (3.0, 30.0)
    assert seen["post_url"] == "https://www.histdata.com/get.php"
    assert seen["data"]["tk"] == "tok-1" and seen["data"]["fxpair"] == "SPXUSD"
    assert seen["headers"]["Referer"] == seen["page"]
    assert "tradedesk-marketdata" in hd._SESSION.headers["User-Agent"]


def test_fetch_returns_none_without_posting_when_histdata_has_no_file(monkeypatch):
    monkeypatch.setattr(hd._SESSION, "get", lambda url, timeout: _Resp(text=_PAGE.format(tk="")))

    def no_post(*_, **__):
        raise AssertionError("no download should be attempted")

    monkeypatch.setattr(hd._SESSION, "post", no_post)
    assert hd._fetch_month_zip(hd.lookup("USA500IDXUSD"), (2009, 1)) is None


def test_fetch_retries_then_raises_when_the_download_is_not_a_zip(monkeypatch):
    posts = {"n": 0}

    def fake_post(*_, **__):
        posts["n"] += 1
        return _Resp(content=b"<html>rate limited</html>")

    monkeypatch.setattr(hd._SESSION, "get", lambda url, timeout: _Resp(text=_PAGE.format(tk="t")))
    monkeypatch.setattr(hd._SESSION, "post", fake_post)
    monkeypatch.setattr(hd._ex, "RETRY_BASE_DELAY", 0.0)

    with pytest.raises(hd.MonthFetchError, match="not a zip"):
        hd._fetch_month_zip(hd.lookup("USA500IDXUSD"), (2015, 1), retries=3)
    assert posts["n"] == 3


def test_fetch_ends_backoff_early_when_cancelled(monkeypatch):
    gets = {"n": 0}

    def failing_get(url, timeout):
        gets["n"] += 1
        return _Resp(status=503)

    monkeypatch.setattr(hd._SESSION, "get", failing_get)
    monkeypatch.setattr(hd._ex, "RETRY_BASE_DELAY", 5.0)  # a sleep the test must not sit out

    threading.Timer(0.1, cancellation.set).start()
    start = time.time()
    with pytest.raises(KeyboardInterrupt):
        hd._fetch_month_zip(hd.lookup("USA500IDXUSD"), (2015, 1), retries=3)
    assert gets["n"] == 1
    assert time.time() - start < 1.0


def test_fetch_stops_mid_download_when_cancelled(monkeypatch):
    class SlowZip(_Resp):
        def iter_content(self, chunk_size):
            yield b"PK"
            cancellation.set()
            yield b"more"
            raise AssertionError("download continued after cancel")

    monkeypatch.setattr(hd._SESSION, "get", lambda url, timeout: _Resp(text=_PAGE.format(tk="t")))
    monkeypatch.setattr(hd._SESSION, "post", lambda *_, **__: SlowZip())

    with pytest.raises(KeyboardInterrupt):
        hd._fetch_month_zip(hd.lookup("USA500IDXUSD"), (2015, 1))
