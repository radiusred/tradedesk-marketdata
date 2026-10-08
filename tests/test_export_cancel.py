"""
Cancellation: a pending cancel stops downloads at the next check instead of
draining the queue, and ends a backoff sleep early.
"""

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

import tradedesk_marketdata.export as ex
from tradedesk_marketdata.cancel import cancellation


@pytest.fixture(autouse=True)
def _clear_cancellation():
    cancellation.clear()
    yield
    cancellation.clear()


def test_download_bi5_ends_backoff_early_when_cancelled(monkeypatch, tmp_path: Path):
    attempts = {"n": 0}

    class Always503:
        def __enter__(self):
            attempts["n"] += 1
            return self

        def __exit__(self, *_):
            return False

        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("503")

    monkeypatch.setattr(ex._SESSION, "get", lambda *_, **__: Always503())
    monkeypatch.setattr(ex, "RETRY_BASE_DELAY", 5.0)  # a sleep the test must not sit out

    threading.Timer(0.1, cancellation.set).start()
    start = time.time()
    with pytest.raises(KeyboardInterrupt):
        ex._download_bi5("http://example.com/1.bi5", tmp_path / "1.bi5", retries=3)

    assert attempts["n"] == 1  # cancelled inside the first backoff
    assert time.time() - start < 1.0


def test_download_bi5_refuses_to_start_when_cancelled(monkeypatch, tmp_path: Path):
    calls = {"n": 0}

    def fake_get(*_, **__):
        calls["n"] += 1
        raise AssertionError("no request should be made")

    monkeypatch.setattr(ex._SESSION, "get", fake_get)
    cancellation.set()

    with pytest.raises(KeyboardInterrupt):
        ex._download_bi5("http://example.com/1.bi5", tmp_path / "1.bi5", retries=3)
    assert calls["n"] == 0


def test_export_range_stops_downloading_once_cancelled(monkeypatch, tmp_path: Path):
    """Cancelling after the first hour must not let the download pool work
    through the remaining queued hours."""
    calls = {"n": 0}

    def fake_download(url, cache_path, timeout, retries):
        calls["n"] += 1
        cancellation.set()
        return None

    monkeypatch.setattr(ex, "_download_bi5", fake_download)

    with pytest.raises(KeyboardInterrupt):
        ex.export_range(
            symbol="EURUSD",
            start_utc=datetime(2025, 1, 6, tzinfo=UTC),
            end_utc_inclusive=datetime(2025, 1, 12, tzinfo=UTC),  # 168 hours queued
            out=tmp_path,
            resample_rule=None,
            cache_dir=None,
        )

    # Only the hours already in flight when the cancel landed were fetched.
    assert calls["n"] <= ex.DOWNLOAD_THREADS_PER_INSTRUMENT
