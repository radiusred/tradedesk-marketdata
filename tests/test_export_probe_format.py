import tradedesk_marketdata.export as ex


def test_probe_price_format_raises_on_too_short_payload() -> None:
    try:
        ex._probe_price_format(b"")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "not enough decompressed bytes" in str(e).lower()
