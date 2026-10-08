import time
from pathlib import Path

import requests

import tradedesk_marketdata.export as ex


class BackoffResponse:
    """Mock response that tracks attempt timing."""

    def __init__(self, attempts_before_success: int):
        self.attempts_before_success = attempts_before_success
        self.attempt = 0
        self.attempt_times = []

    def __enter__(self):
        self.attempt_times.append(time.time())
        self.attempt += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def status_code(self):
        if self.attempt < self.attempts_before_success:
            return 503
        return 200

    @property
    def content(self):
        return b"x" * 80

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_exponential_backoff_timing(monkeypatch, tmp_path: Path):
    """Verify exponential backoff delays between retries."""
    mock_resp = BackoffResponse(attempts_before_success=3)

    def fake_get(*_, **__):
        return mock_resp

    monkeypatch.setattr(ex._SESSION, "get", fake_get)

    result = ex._download_bi5(
        url="http://example.com/data.bi5",
        cache_path=tmp_path / "file.bi5",
        retries=3,
    )

    assert result == b"x" * 80
    assert len(mock_resp.attempt_times) == 3

    # Verify exponential backoff: delays should be ~0.5s, ~1s between attempts
    delays = [
        mock_resp.attempt_times[i + 1] - mock_resp.attempt_times[i]
        for i in range(len(mock_resp.attempt_times) - 1)
    ]

    # Allow some tolerance for execution time
    assert 0.4 < delays[0] < 0.9  # ~0.5s backoff
    assert 0.9 < delays[1] < 2.1  # ~1.0s backoff


def test_backoff_resets_on_success(monkeypatch, tmp_path: Path):
    """Verify backoff doesn't accumulate across successful calls."""
    call_count = {"n": 0}

    def fake_get(*_, **__):
        call_count["n"] += 1
        # First call: 503, second call: 200
        if call_count["n"] == 1:
            resp = BackoffResponse(attempts_before_success=2)
        else:
            resp = BackoffResponse(attempts_before_success=1)
        return resp

    monkeypatch.setattr(ex._SESSION, "get", fake_get)

    # First download: should retry once
    start1 = time.time()
    ex._download_bi5("http://example.com/1.bi5", tmp_path / "1.bi5", retries=2)
    elapsed1 = time.time() - start1

    # Second download: should succeed immediately (no accumulated backoff)
    start2 = time.time()
    ex._download_bi5("http://example.com/2.bi5", tmp_path / "2.bi5", retries=2)
    elapsed2 = time.time() - start2

    assert 0.4 < elapsed1 < 0.9  # One backoff
    assert elapsed2 < 0.2  # No backoff
