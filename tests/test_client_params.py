"""CoinGeckoClient query parameters for top_holders and the wallet trades/transfers filters. No network."""
import httpx
import pytest

from core.client import CoinGeckoClient


def make_client(seen: list[httpx.Request], body: dict | None = None) -> CoinGeckoClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body if body is not None else {"data": []})

    return CoinGeckoClient(api_key="test-key", base_url="https://example.test/api/v3", transport=httpx.MockTransport(handler))


async def test_top_holders_default_params_unchanged():
    seen: list[httpx.Request] = []
    holders = [{"address": "0xabc", "label": None}]
    client = make_client(seen, {"data": {"attributes": {"holders": holders}}})
    assert await client.top_holders("base", "0xtoken", n=20) == holders
    assert dict(seen[0].url.params) == {"holders": "20"}


async def test_top_holders_include_pnl_details():
    seen: list[httpx.Request] = []
    client = make_client(seen, {"data": {"attributes": {"holders": []}}})
    await client.top_holders("base", "0xtoken", n=20, include_pnl_details=True)
    assert dict(seen[0].url.params) == {"holders": "20", "include_pnl_details": "true"}


async def test_wallet_trades_default_params_unchanged():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    await client.wallet_trades("base", "0xwallet", max_pages=1, per_page=200)
    assert dict(seen[0].url.params) == {"per_page": "200"}
    assert seen[0].url.path.endswith("/onchain/networks/base/wallets/0xwallet/trades")


async def test_wallet_trades_token_and_window():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    await client.wallet_trades("base", "0xwallet", max_pages=1, per_page=300, token="0xtoken", from_ts="2026-09-23", to_ts="2026-09-30")
    assert dict(seen[0].url.params) == {"per_page": "300", "token": "0xtoken", "from": "2026-09-23", "to": "2026-09-30"}


async def test_wallet_transfers_default_params_unchanged():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    await client.wallet_transfers("eth", "0xwallet", max_pages=1)
    assert dict(seen[0].url.params) == {}
    assert seen[0].url.path.endswith("/onchain/networks/eth/wallets/0xwallet/transfers")


async def test_wallet_transfers_token_window_and_direction():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    await client.wallet_transfers("eth", "0xwallet", max_pages=1, per_page=300, token="0xtoken", from_ts=1_700_000_000, to_ts=1_700_600_000, direction="in")
    assert dict(seen[0].url.params) == {"per_page": "300", "token": "0xtoken", "from": "1700000000", "to": "1700600000", "direction": "in"}


async def test_wallet_calls_reject_half_a_window_before_any_request():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    with pytest.raises(ValueError):
        await client.wallet_trades("base", "0xwallet", from_ts="2026-09-23")
    with pytest.raises(ValueError):
        await client.wallet_transfers("base", "0xwallet", to_ts="2026-09-30")
    assert seen == []


async def test_wallet_transfers_rejects_bad_direction():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    with pytest.raises(ValueError):
        await client.wallet_transfers("base", "0xwallet", direction="sideways")
    assert seen == []


async def test_wallet_calls_follow_cursor_with_filters_kept():
    seen: list[httpx.Request] = []
    pages = [
        {"data": [{"attributes": {"tx_hash": "0x1"}}], "meta": {"next_cursor": "c2"}},
        {"data": [{"attributes": {"tx_hash": "0x2"}}], "meta": {"next_cursor": None}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    client = CoinGeckoClient(api_key="test-key", base_url="https://example.test/api/v3", transport=httpx.MockTransport(handler))
    rows = await client.wallet_transfers("eth", "0xwallet", max_pages=5, token="0xtoken")
    assert [r["tx_hash"] for r in rows] == ["0x1", "0x2"]
    assert dict(seen[1].url.params) == {"token": "0xtoken", "cursor": "c2"}
