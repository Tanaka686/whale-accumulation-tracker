"""Scan tab: pick a chain + source, get today's hot tokens, then find the wallets trading them."""
import asyncio

from core.client import CoinGeckoClient
from core.wallets import likely_bot_row

from . import config


def _short(addr: str | None) -> str:
    """A wallet address, shortened for display (0x1234...abcd)."""
    if not addr or len(addr) < 10:
        return addr or ""
    return f"{addr[:6]}...{addr[-4:]}"


async def _tokens_from_pools(client: CoinGeckoClient, chain: str, source: str, n_tokens: int) -> list[dict]:
    """The top N distinct base tokens for a source, as {address, symbol, pool}."""
    if source == "new_pools":
        pools = await client.new_pools(chain, n=40)
    elif source == "safe_movers":
        pools = await client.megafilter(networks=chain, **config.SAFE_MOVERS_FILTERS)
    else:
        duration = "24h" if source == "trending_24h" else "1h"
        pools = await client.trending_pools(chain, duration, n=40)

    tokens: list[dict] = []
    seen: set[str] = set()
    for p in pools:
        attrs = p.get("attributes", {})
        rel = (p.get("relationships") or {}).get("base_token", {}).get("data", {}) or {}
        token_id = rel.get("id", "")
        address = token_id.split("_", 1)[1] if "_" in token_id else attrs.get("address", "")
        if not address or address.lower() in seen:
            continue
        seen.add(address.lower())
        name = attrs.get("name", "") or ""
        symbol = name.split(" / ")[0] if " / " in name else name
        tokens.append({"address": address, "symbol": symbol or _short(address), "pool": attrs.get("address")})
        if len(tokens) >= n_tokens:
            break
    return tokens


async def scan(client: CoinGeckoClient, chain: str, source: str, n_tokens: int = config.DEFAULT_TOP_N_TOKENS) -> dict:
    """Top N tokens for chain+source, then every wallet in their top_traders, deduped and counted.

    A wallet that shows up trading several of today's hot tokens is more interesting than one
    that only shows up once. Likely bots (huge trade counts on a single token) are flagged, not
    dropped, so the Wallets tab can filter them out without losing the count.
    """
    tokens = await _tokens_from_pools(client, chain, source, n_tokens)

    async def top_traders_for(token: dict):
        try:
            traders = await client.top_traders(chain, token["address"], n=25)
        except Exception:
            traders = []
        return token, traders

    results = await asyncio.gather(*(top_traders_for(t) for t in tokens))

    by_wallet: dict[str, dict] = {}
    for token, traders in results:
        for t in traders:
            address = (t.get("address") or "").strip()
            if not address:
                continue
            key = address.lower()
            c = by_wallet.setdefault(
                key,
                {
                    "address": address,
                    "short": _short(address),
                    "label_hint": t.get("label") or t.get("name"),
                    "seen_in": [],
                    "realized_seen_usd": 0.0,
                    "trades_seen": 0,
                    "likely_bot": False,
                },
            )
            c["seen_in"].append({"symbol": token["symbol"], "address": token["address"], "realized_usd": round(float(t.get("realized_pnl_usd") or 0))})
            c["realized_seen_usd"] += float(t.get("realized_pnl_usd") or 0)
            c["trades_seen"] += (t.get("total_buy_count") or 0) + (t.get("total_sell_count") or 0)
            c["likely_bot"] = c["likely_bot"] or likely_bot_row(t)

    candidates = sorted(
        by_wallet.values(),
        key=lambda c: (c["likely_bot"], -len(c["seen_in"]), -c["realized_seen_usd"]),
    )
    for c in candidates:
        c["realized_seen_usd"] = round(c["realized_seen_usd"])
        c["tokens_seen_in"] = len(c["seen_in"])

    return {"chain": chain, "source": source, "tokens": tokens, "candidates": candidates}
