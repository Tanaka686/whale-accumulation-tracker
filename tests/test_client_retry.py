"""CoinGeckoClient retries: 429, 408 and 5xx are tried again after a short wait; other errors are not. No network."""
import httpx
import pytest

from core import config
from core.client import CoinGeckoClient, CoinGeckoError


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(config, "BACKOFF_BASE_S", 0)


def make(statuses: list[int]):
    """A client whose Nth request answers statuses[N] (the last status repeats), plus the list of requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(request)
        return httpx.Response(status, json={"data": {"ok": True}} if status == 200 else {"status": {"error_code": status, "error_message": "x"}})

    return CoinGeckoClient(api_key="k", base_url="https://example.test/api/v3", transport=httpx.MockTransport(handler)), seen


@pytest.mark.parametrize("bad", [408, 429, 500, 502, 503, 504])
async def test_a_transient_error_is_retried_then_succeeds(bad):
    client, seen = make([bad, bad, 200])
    assert await client.get("/x") == {"data": {"ok": True}}
    assert len(seen) == 3
    assert client.credits_used == 1  # only the successful request is counted


async def test_it_gives_up_after_the_retry_limit_and_reports_the_status():
    client, seen = make([503])
    with pytest.raises(CoinGeckoError) as e:
        await client.get("/x")
    assert e.value.status == 503
    assert len(seen) == config.MAX_RETRIES == 4  # the first try plus 3 retries
    assert client.credits_used == 0


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_other_errors_are_not_retried(status):
    client, seen = make([status, 200])
    with pytest.raises(CoinGeckoError):
        await client.get("/x")
    assert len(seen) == 1


async def test_a_retried_request_still_uses_the_cache_afterwards():
    client, seen = make([408, 200])
    await client.get("/x", ttl=60)
    await client.get("/x", ttl=60)
    assert len(seen) == 2  # 408 + 200, then the second call is served from the cache
