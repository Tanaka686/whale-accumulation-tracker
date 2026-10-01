"""Bring-your-own-key: the visitor's key stays in memory, is validated once, and never comes back. No network."""
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import server
from app.session_keys import KeyVault
from core import config as core_config
from core.client import CoinGeckoClient
from tests.test_accumulation import TOKEN

KEY = "CG-visitor-secret-key-123"
GOOD = {"network": "base", "token": TOKEN, "days": 7, "holders": 50}


@pytest.fixture
def byok(monkeypatch):
    """No .env key; CoinGeckoClient is rebuilt on a mock transport so POST /api/key works offline."""
    seen: list[httpx.Request] = []
    plan = {"value": "analyst", "status": 200}

    def handler(request):
        seen.append(request)
        if plan["status"] != 200:
            return httpx.Response(plan["status"], json={"status": {"error_code": 10010 if plan["status"] == 401 else 0}})
        return httpx.Response(200, json={"plan": plan["value"]})

    real = CoinGeckoClient.__init__

    def init(self, api_key=None, base_url=None, transport=None, environment=None):
        real(self, api_key=api_key, base_url=base_url, transport=httpx.MockTransport(handler), environment=environment)

    monkeypatch.setattr(CoinGeckoClient, "__init__", init)
    monkeypatch.setattr(core_config, "API_KEY", "")
    monkeypatch.setattr(server, "vault", KeyVault())
    monkeypatch.setattr(server.app.state, "client", type("Dummy", (), {"credits_used": 0})(), raising=False)
    return TestClient(server.app), seen, plan


def test_no_key_means_scan_needs_a_key(byok):
    client, _, _ = byok
    assert client.get("/api/key/status").json()["has_key"] is False
    assert client.post("/api/whales/scan", json=GOOD).status_code == 401


def test_a_pasted_key_is_used_on_the_right_host_and_never_returned(byok, caplog):
    client, seen, _ = byok
    caplog.set_level(logging.DEBUG)
    r = client.post("/api/key", json={"key": KEY, "environment": "demo"})
    assert r.status_code == 200 and r.json()["source"] == "session" and r.json()["analyst"] is True
    assert seen[0].headers["x-cg-demo-api-key"] == KEY and seen[0].url.host == "api.coingecko.com"
    assert KEY not in r.text and KEY not in client.get("/api/key/status").text and KEY not in caplog.text
    assert "httponly" in r.headers["set-cookie"].lower() and KEY not in r.headers["set-cookie"]
    assert client.get("/api/key/status").json()["has_key"] is True


def test_pro_uses_the_pro_host_and_header(byok):
    client, seen, _ = byok
    client.post("/api/key", json={"key": KEY, "environment": "pro"})
    assert seen[0].headers["x-cg-pro-api-key"] == KEY and seen[0].url.host == "pro-api.coingecko.com"


def test_the_key_belongs_to_one_browser_session(byok):
    client, _, _ = byok
    client.post("/api/key", json={"key": KEY})
    other = TestClient(server.app)
    assert other.get("/api/key/status").json()["has_key"] is False


def test_a_demo_key_is_told_it_needs_analyst(byok):
    client, _, plan = byok
    plan["value"] = "demo"
    r = client.post("/api/key", json={"key": KEY, "environment": "demo"})
    assert r.json()["analyst"] is False
    d = client.post("/api/whales/scan", json=GOOD).json()
    assert d["locked"] is True


def test_a_rejected_key_is_not_stored_or_echoed(byok):
    client, _, plan = byok
    plan["status"] = 401
    r = client.post("/api/key", json={"key": KEY})
    assert r.status_code == 400 and KEY not in r.text
    assert client.get("/api/key/status").json()["has_key"] is False


@pytest.mark.parametrize("bad", ["", "short", "has space in it here", "x" * 300, "bad<script>key"])
def test_malformed_keys_are_rejected_without_a_call(byok, bad):
    client, seen, _ = byok
    r = client.post("/api/key", json={"key": bad})
    assert r.status_code == 400 and (not bad or bad not in r.text)
    assert seen == []


def test_bad_environment_is_rejected(byok):
    client, seen, _ = byok
    assert client.post("/api/key", json={"key": KEY, "environment": "evil"}).status_code == 400 and seen == []


def test_remove_key_forgets_it(byok):
    client, _, _ = byok
    client.post("/api/key", json={"key": KEY})
    client.delete("/api/key")
    assert client.get("/api/key/status").json()["has_key"] is False


def test_an_env_key_wins_and_pasting_is_refused(byok, monkeypatch):
    client, _, _ = byok
    monkeypatch.setattr(core_config, "API_KEY", "from-env-key-123")
    assert client.get("/api/key/status").json()["source"] == "env"
    assert client.post("/api/key", json={"key": KEY}).status_code == 400


async def test_idle_sessions_expire_and_the_number_is_capped():
    vault = KeyVault(ttl_s=0.01, max_sessions=2)
    c = lambda: CoinGeckoClient(api_key="k" * 10, base_url="https://example.test")
    sid = await vault.put(None, c(), {})
    import asyncio
    await asyncio.sleep(0.05)
    assert await vault.get(sid) is None
    vault = KeyVault(max_sessions=2)
    sids = [await vault.put(None, c(), {}) for _ in range(3)]
    assert len(vault._s) == 2 and sids[0] not in vault._s
    await vault.close_all()


def test_the_key_is_never_written_to_disk(byok, tmp_path, monkeypatch):
    client, _, _ = byok
    monkeypatch.chdir(tmp_path)
    client.post("/api/key", json={"key": KEY})
    assert not any(KEY in p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file())
