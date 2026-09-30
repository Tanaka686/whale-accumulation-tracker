"""The Whales API and page: /api/whales/config, /api/whales/scan, and the HTML that serves the tab. No network."""
import pytest
from fastapi.testclient import TestClient

from app import accumulation, server
from core import config as core_config
from core.client import CoinGeckoError, PlanRestrictedError
from tests.test_accumulation import POOL, TOKEN, FakeWhaleClient, addr, buy, holder

FOOTNOTE = "Holders, trades, transfers and prices come from CoinGecko API. Stance labels, reasons and the flow breakdown are computed by this tool, not CoinGecko fields, and are not financial advice."
SECRET = "SECRET-TEST-KEY-123"
GOOD = {"network": "base", "token": TOKEN, "days": 7, "holders": 50}


def fake_client():
    holders = [holder(1, POOL, None, amount="9000"), holder(2, addr(2), "Cold Wallet", amount="7000"), holder(3, addr(3), None, amount="1000")]
    return FakeWhaleClient(holders, pools=[POOL], price="2", trades={addr(3): [buy(30, 60, "0xd1")]})


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = fake_client()
    monkeypatch.setattr(server.app.state, "client", fake, raising=False)
    monkeypatch.setitem(server.state, "capabilities", {"analyst": True, "paid": True, "upgrade_url": "https://example.test/pricing"})
    monkeypatch.setitem(server.state, "whale_scan_running", False)
    monkeypatch.setattr(accumulation.config, "WHALE_SCANS_DIR", str(tmp_path))
    monkeypatch.setattr(core_config, "API_KEY", SECRET)  # the page and API must never echo it
    return TestClient(server.app), fake


# ---------- config ----------


def test_config_lists_the_networks_windows_and_the_credit_cap(api):
    client, _ = api
    d = client.get("/api/whales/config").json()
    assert d["networks"] == [{"id": "eth", "label": "Ethereum"}, {"id": "base", "label": "Base"}, {"id": "bsc", "label": "BNB Chain"}]
    assert d["days"] == [7, 30] and d["holders"] == [20, 50] and d["default_holders"] == 50
    assert d["credit_cap"] == 1500 and d["credits_per_wallet"] == 20
    assert d["thresholds"] == {"new_position_max_start_pct": 1.0, "stance_pct": 2.0}
    assert set(d["explorers"]) == {"eth", "base", "bsc"}


# ---------- scan ----------


def test_scan_returns_the_result_and_saves_a_json_file(api, tmp_path):
    client, fake = api
    r = client.post("/api/whales/scan", json=GOOD)
    assert r.status_code == 200
    d = r.json()
    assert d["network"] == "base" and d["token"] == TOKEN and d["symbol"] == "TST" and d["days"] == 7
    assert d["summary"]["whales"] == 1 and d["summary"]["excluded"] == 2
    assert [e["reason_code"] for e in d["excluded"]] == ["liquidity_pool", "exchange_label"]
    assert d["whales"][0]["breakdown"]["dex"] == {"tokens": 30, "usd": 60}
    assert d["credits"] == fake.credits_used > 0 and d["elapsed_ms"] >= 0
    assert d["saved_to"] and len(list(tmp_path.glob("*.json"))) == 1


def test_scan_defaults_to_50_holders_and_7_days(api):
    client, fake = api
    d = client.post("/api/whales/scan", json={"network": "base", "token": TOKEN}).json()
    assert fake.holder_calls[0]["n"] == 50 and d["days"] == 7


def test_scan_honours_20_holders_and_30_days(api):
    client, fake = api
    d = client.post("/api/whales/scan", json={**GOOD, "days": 30, "holders": 20}).json()
    assert fake.holder_calls[0]["n"] == 20 and d["days"] == 30 and d["holders_requested"] == 20


@pytest.mark.parametrize(
    "patch, word",
    [
        ({"network": "solana"}, "network"),
        ({"network": ""}, "network"),
        ({"token": "not-an-address"}, "token"),
        ({"token": ""}, "token"),
        ({"days": 14}, "days"),
        ({"days": "abc"}, "numbers"),
        ({"holders": "many"}, "numbers"),
    ],
)
def test_bad_input_is_a_400_with_a_plain_message_and_costs_nothing(api, patch, word):
    client, fake = api
    r = client.post("/api/whales/scan", json={**GOOD, **patch})
    assert r.status_code == 400 and word in r.json()["detail"]
    assert fake.credits_used == 0
    assert server.state["whale_scan_running"] is False


def test_a_plan_without_analyst_gets_the_locked_payload_and_nothing_is_fetched(api, monkeypatch):
    client, fake = api
    monkeypatch.setitem(server.state, "capabilities", {"analyst": False, "upgrade_url": "https://example.test/pricing"})
    d = client.post("/api/whales/scan", json=GOOD).json()
    assert d["locked"] is True and d["upgrade_url"] == "https://example.test/pricing" and "Analyst" in d["feature"]
    assert fake.credits_used == 0


def test_a_plan_error_from_coingecko_becomes_the_locked_payload(api):
    client, fake = api

    async def boom(*a, **k):
        raise PlanRestrictedError(403, "plan")

    fake.top_holders = boom
    d = client.post("/api/whales/scan", json=GOOD).json()
    assert d["locked"] is True
    assert server.state["whale_scan_running"] is False


def test_a_missing_token_is_a_404_with_advice(api):
    client, fake = api

    async def boom(*a, **k):
        raise CoinGeckoError(404, "not found")

    fake.top_holders = boom
    r = client.post("/api/whales/scan", json=GOOD)
    assert r.status_code == 404 and "no holder data" in r.json()["error"] and r.json()["status"] == 404
    assert server.state["whale_scan_running"] is False


def test_other_coingecko_errors_are_a_502(api):
    client, fake = api

    async def boom(*a, **k):
        raise CoinGeckoError(500, "boom")

    fake.top_holders = boom
    r = client.post("/api/whales/scan", json=GOOD)
    assert r.status_code == 502 and "HTTP 500" in r.json()["error"]


def test_only_one_scan_runs_at_a_time(api, monkeypatch):
    client, fake = api
    monkeypatch.setitem(server.state, "whale_scan_running", True)
    r = client.post("/api/whales/scan", json=GOOD)
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    assert fake.credits_used == 0
    assert server.state["whale_scan_running"] is True  # the running scan's flag is left alone


def test_the_running_flag_is_cleared_after_a_good_scan(api):
    client, _ = api
    client.post("/api/whales/scan", json=GOOD)
    assert server.state["whale_scan_running"] is False


def test_the_api_key_never_appears_in_any_response(api):
    client, fake = api
    texts = [client.get("/api/whales/config").text, client.post("/api/whales/scan", json=GOOD).text, client.post("/api/whales/scan", json={**GOOD, "days": 3}).text, client.get("/").text]
    assert all(SECRET not in t for t in texts)


# ---------- the page ----------


def test_the_page_opens_on_the_whales_tab_with_the_badge_and_footnote(api):
    client, _ = api
    html = client.get("/").text
    tabs = html[html.index('<nav class="tabs">'): html.index("</nav>")]
    assert tabs.index('data-tab="whales"') < tabs.index('data-tab="scan"')  # Whales is the first tab...
    assert '<button class="active" data-tab="whales">' in tabs  # ...and the active one
    assert '<section id="panel-whales" class="panel active">' in html
    assert '<section id="panel-scan" class="panel">' in html  # Radar is still there, just not the default
    for tab in ("scan", "wallets", "follow", "runs"):
        assert f'data-tab="{tab}"' in tabs
    assert 'id="cg-badge"' in html  # the official CoinGecko API badge in the header
    assert FOOTNOTE in html
    for field in ("w-network", "w-token", "w-days", "w-holders", "w-run"):
        assert f'id="{field}"' in html


def test_the_whales_assets_are_served_and_the_old_ones_still_are(api):
    client, _ = api
    for path in ("/static/whales.js", "/static/whales.css", "/static/app.js", "/static/styles.css", "/whales"):
        assert client.get(path).status_code == 200, path
    assert "Scan whales" in client.get("/").text


def test_favicon_is_an_empty_204_instead_of_a_404(api):
    client, _ = api
    r = client.get("/favicon.ico")
    assert r.status_code == 204 and r.content == b""


def test_the_script_never_mentions_the_api_key():
    js = open(server.WEB / "whales.js", encoding="utf-8").read()
    assert "x-cg-pro-api-key" not in js.lower() and "api_key" not in js.lower() and "apikey" not in js.lower()
