import tradedesk_marketdata.sources.dukascopy as dk


def test_probe_price_format_raises_on_too_short_payload() -> None:
    try:
        dk._probe_price_format(b"")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "not enough decompressed bytes" in str(e).lower()
