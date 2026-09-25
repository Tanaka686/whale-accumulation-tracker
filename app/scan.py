"""Scan tab: pick a chain + source, get today's hot tokens, then find the wallets trading them."""
import asyncio

from core.client import CoinGeckoClient
from core.wallets import _f, known_infra, likely_bot_row

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
        tokens.append(token_from_pool(p, address))
        if len(tokens) >= n_tokens:
            break
    return tokens


def token_from_pool(p: dict, address: str) -> dict:
    """One scanned token for the strip: logo, symbol, price, 1h/24h change, liquidity, volume, pool, DEX."""
    attrs = p.get("attributes", {})
    base = p.get("_base_token") or {}
    name = attrs.get("name", "") or ""
    symbol = base.get("symbol") or (name.split(" / ")[0] if " / " in name else name)
    change = attrs.get("price_change_percentage") or {}
    image = base.get("image_url")
    return {
        "address": address,
        "symbol": symbol or _short(address),
        "name": base.get("name"),
        "image_url": image if image and "missing" not in image else None,
        "pool": attrs.get("address"),
        "pool_name": name,
        "dex": p.get("_dex"),
        "price_usd": _f(attrs.get("base_token_price_usd")),
        "change_1h": _f(change.get("h1")),
        "change_24h": _f(change.get("h24")),
        "liquidity_usd": _f(attrs.get("reserve_in_usd")),
        "volume_24h_usd": _f((attrs.get("volume_usd") or {}).get("h24")),
        "fdv_usd": _f(attrs.get("fdv_usd")),
        "pool_created_at": attrs.get("pool_created_at"),
    }


async def scan(client: CoinGeckoClient, chain: str, source: str, n_tokens: int = config.DEFAULT_TOP_N_TOKENS) -> dict:
    """Top N tokens for chain+source, then every wallet in their top_traders, deduped and counted.

    A wallet that shows up trading several of today's hot tokens is more interesting than one
    that only shows up once. Likely bots (huge trade counts on a single token) are flagged, not
    dropped, so the Wallets tab can filter them out without losing the count.
    """
    tokens = await _tokens_from_pools(client, chain, source, n_tokens)
    return await _wallets_for_tokens(client, chain, source, tokens)


async def scan_tokens(client: CoinGeckoClient, chain: str, tokens: list[dict]) -> dict:
    """Same wallet-recurrence scan as `scan()`, but for a hand-picked list of tokens (from search)
    instead of a trending source. `tokens` items need at least {address, symbol, image_url}."""
    return await _wallets_for_tokens(client, chain, "handpicked", tokens[:20])


async def _wallets_for_tokens(client: CoinGeckoClient, chain: str, source: str, tokens: list[dict]) -> dict:
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
                    "bought_seen_usd": 0.0,
                    "trades_seen": 0,
                    "likely_bot": False,
                    "known_infra": known_infra(address),
                },
            )
            c["seen_in"].append(
                {"symbol": token["symbol"], "address": token["address"], "image_url": token.get("image_url"), "image": token.get("image_url"), "realized_usd": round(float(t.get("realized_pnl_usd") or 0))}
            )
            c["bought_seen_usd"] += float(t.get("total_buy_usd") or 0)
            c["realized_seen_usd"] += float(t.get("realized_pnl_usd") or 0)
            c["trades_seen"] += (t.get("total_buy_count") or 0) + (t.get("total_sell_count") or 0)
            c["likely_bot"] = c["likely_bot"] or likely_bot_row(t)

    candidates = sorted(
        by_wallet.values(),
        key=lambda c: (bool(c["known_infra"]), c["likely_bot"], -len(c["seen_in"]), -c["realized_seen_usd"]),
    )
    for c in candidates:
        c["realized_seen_usd"] = round(c["realized_seen_usd"])
        c["bought_seen_usd"] = round(c["bought_seen_usd"])
        c["tokens_seen_in"] = len(c["seen_in"])

    return {"chain": chain, "source": source, "tokens": tokens, "candidates": candidates}
