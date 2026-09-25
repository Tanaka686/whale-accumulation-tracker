"""Wallets tab: profile candidate wallets with wallet_pnl + wallet_trades (+ wallet_balances on EVM)."""
import asyncio

from core import config as core_config
from core.client import CoinGeckoClient

from . import scoring


async def profile_one(client: CoinGeckoClient, chain: str, address: str, budget_usd: float) -> dict:
    """One wallet: pnl + up to 3 pages of trades (+ balances on EVM chains), turned into a scored profile."""
    caps = core_config.wallet_caps(chain)
    pnl_task = client.wallet_pnl(address, [chain]) if caps.get("pnl") else _noop({})
    trades_task = client.wallet_trades(chain, address, max_pages=3) if caps.get("trades") else _noop([])
    balances_task = client.wallet_balances(address, [chain]) if caps.get("balances") else _noop({})
    pnl_attrs, trades_rows, balances = await asyncio.gather(pnl_task, trades_task, balances_task)
    scored = scoring.profile_wallet(pnl_attrs, trades_rows, budget_usd)
    return {
        "address": address,
        "chain": chain,
        "holdings_count": len(balances.get("balances", [])) if isinstance(balances, dict) else 0,
        **scored,
    }


async def _noop(default):
    return default


async def profile_candidates(client: CoinGeckoClient, chain: str, addresses: list[str], budget_usd: float) -> list[dict]:
    """Profiles every candidate address (default 30), one core.wallets call chain per wallet."""
    results = await asyncio.gather(*(profile_one(client, chain, a, budget_usd) for a in addresses), return_exceptions=True)
    out = []
    for addr, r in zip(addresses, results):
        if isinstance(r, Exception):
            out.append({"address": addr, "chain": chain, "error": str(r)[:200]})
        else:
            out.append(r)
    return out


async def wallet_drawer(client: CoinGeckoClient, chain: str, address: str) -> dict:
    """Everything the wallet drawer shows: holdings, per-token performance, and recent trades."""
    caps = core_config.wallet_caps(chain)
    pnl_attrs = await client.wallet_pnl(address, [chain]) if caps.get("pnl") else {}
    trades_rows = await client.wallet_trades(chain, address, max_pages=3) if caps.get("trades") else []
    balances = await client.wallet_balances(address, [chain]) if caps.get("balances") else {}

    stats = pnl_attrs.get("token_stats") or []
    performance = sorted(
        [
            {
                "symbol": s.get("symbol"),
                "network": s.get("network"),
                "realized_pnl_usd": s.get("realized_pnl_usd"),
                "unrealized_pnl_usd": s.get("unrealized_pnl_usd"),
                "total_buy_count": s.get("total_buy_count"),
                "total_sell_count": s.get("total_sell_count"),
            }
            for s in stats
        ],
        key=lambda x: -abs((x["realized_pnl_usd"] or 0) + (x["unrealized_pnl_usd"] or 0)),
    )[:20]
    holdings = [
        {
            "symbol": b.get("symbol"),
            "network": b.get("network"),
            "balance": b.get("balance"),
            "value_usd": b.get("value_usd"),
            "change_24h": b.get("h24_price_change_percentage"),
        }
        for b in (balances.get("balances") if isinstance(balances, dict) else []) or []
    ][:20]
    recent_trades = [
        {
            "ts": t.get("block_timestamp"),
            "kind": t.get("kind"),
            "usd": t.get("volume_in_usd"),
            "pool_dex": t.get("pool_dex"),
            "tx": t.get("tx_hash"),
        }
        for t in trades_rows[:30]
    ]
    return {
        "address": address,
        "chain": chain,
        "chain_label": core_config.CHAINS.get(chain, chain),
        "capabilities": caps,
        "holdings": holdings,
        "performance": performance,
        "recent_trades": recent_trades,
        "lifetime_realized_pnl_usd": pnl_attrs.get("total_realized_pnl_usd"),
        "lifetime_unrealized_pnl_usd": pnl_attrs.get("total_unrealized_pnl_usd"),
        "tokens_traded": pnl_attrs.get("total_tokens"),
    }
