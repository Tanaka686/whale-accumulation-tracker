"""Bring-your-own-key: a visitor's CoinGecko key lives only in this process's memory, tied to a random
session cookie. It is never written to disk, never logged and never sent back in any response.
Idle sessions expire; the number of sessions is capped."""
import re
import secrets
import time

from core.client import CoinGeckoClient

COOKIE = "wat_sid"
TTL_S = 2 * 3600
MAX_SESSIONS = 50
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,200}$")


class KeyVault:
    def __init__(self, ttl_s: float = TTL_S, max_sessions: int = MAX_SESSIONS):
        self.ttl_s, self.max_sessions = ttl_s, max_sessions
        self._s: dict[str, dict] = {}

    async def _purge(self):
        now = time.monotonic()
        for sid in [k for k, v in self._s.items() if now - v["seen"] > self.ttl_s]:
            await self.drop(sid)

    async def get(self, sid: str | None) -> dict | None:
        await self._purge()
        entry = self._s.get(sid or "")
        if entry:
            entry["seen"] = time.monotonic()
        return entry

    async def put(self, sid: str | None, client: CoinGeckoClient, caps: dict) -> str:
        await self._purge()
        if sid in self._s:
            await self.drop(sid)
        while len(self._s) >= self.max_sessions:  # evict the least recently used
            await self.drop(min(self._s, key=lambda k: self._s[k]["seen"]))
        sid = secrets.token_urlsafe(24)
        self._s[sid] = {"client": client, "caps": caps, "seen": time.monotonic()}
        return sid

    async def drop(self, sid: str | None):
        entry = self._s.pop(sid or "", None)
        if entry:
            await entry["client"].close()

    async def close_all(self):
        for sid in list(self._s):
            await self.drop(sid)
