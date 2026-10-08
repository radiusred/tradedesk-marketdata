from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import tradedesk_marketdata.export as ex


class DummyResponse:
    def __init__(self, *, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


def test_404_returns_none_and_does_not_cache(cache_dir: Path, monkeypatch):
    def fake_get(*_, **__):
        return DummyResponse(status_code=404, content=b"")

    monkeypatch.setattr(ex._SESSION, "get", fake_get)

    result = ex._download_bi5(
        url="http://example.com/data.bi5",
        cache_path=cache_dir / "file.bi5",
        retries=1,
    )

    assert result is None
    assert not cache_dir.exists()


def test_200_zero_length_body_is_cached(cache_dir: Path, monkeypatch):
    def fake_get(*_, **__):
        return DummyResponse(status_code=200, content=b"")

    monkeypatch.setattr(ex._SESSION, "get", fake_get)

    cache_path = cache_dir / "file.bi5"

    result = ex._download_bi5(
        url="http://example.com/data.bi5",
        cache_path=cache_path,
        retries=1,
    )

    assert result == b""
    assert cache_path.exists()
    assert cache_path.read_bytes() == b""


def test_cached_empty_file_short_circuits_download(cache_dir: Path, monkeypatch):
    cache_path = cache_dir / "file.bi5"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"")

    get_mock = Mock()
    monkeypatch.setattr(ex._SESSION, "get", get_mock)

    result = ex._download_bi5(
        url="http://example.com/data.bi5",
        cache_path=cache_path,
        retries=1,
    )

    assert result == b""
    get_mock.assert_not_called()


def test_tiny_payload_is_treated_as_no_data_and_cached(cache_dir: Path, monkeypatch):
    def fake_get(*_, **__):
        return DummyResponse(status_code=200, content=b"123")

    monkeypatch.setattr(ex._SESSION, "get", fake_get)

    cache_path = cache_dir / "file.bi5"

    result = ex._download_bi5(
        url="http://example.com/data.bi5",
        cache_path=cache_path,
        retries=1,
    )

    assert result == b""
    assert cache_path.exists()
    assert cache_path.read_bytes() == b""


def test_retry_then_success_caches_payload(cache_dir: Path, monkeypatch):
    calls = {"count": 0}
    payload = b"x" * 80  # must be >= 64 bytes or it is treated as "tiny" and coerced to b""

    def fake_get(*_, **__):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.ConnectionError("temporary failure")
        return DummyResponse(status_code=200, content=payload)

    monkeypatch.setattr(ex._SESSION, "get", fake_get)

    cache_path = cache_dir / "file.bi5"

    result = ex._download_bi5(
        url="http://example.com/data.bi5",
        cache_path=cache_path,
        retries=2,
    )

    assert result == payload
    assert cache_path.exists()
    assert cache_path.read_bytes() == payload
