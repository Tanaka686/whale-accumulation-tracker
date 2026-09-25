"""Follow tab / forward test: poll followed wallets' trades, mirror them into the paper engine.

Copies are detected by polling, not streaming (there's no wallet-level WebSocket channel), so this
is honest about latency: a followed wallet's trade shows up here up to `poll_s` seconds late.
"""
import time

from core import wallets as w
from core.client import CoinGeckoClient
from core.paper import Portfolio


class FollowEngine:
    """Mirrors buys/sells from a list of followed wallets into one paper Portfolio, scaled to budget."""

    def __init__(self, client: CoinGeckoClient, chain: str, addresses: list[str], budget_usd: float, assumptions: dict, poll_s: float = 30):
        self.client = client
        self.chain = chain
        self.addresses = addresses
        self.poll_s = poll_s
        self.assumptions = assumptions
        self.portfolio = Portfolio(
            cash=budget_usd,
            slippage_bps=assumptions.get("slippage_bps", 30),
            fee_bps=assumptions.get("fee_bps", 25),
            max_position_pct=assumptions.get("max_position_pct", 0.2),
            cooldown_s=assumptions.get("cooldown_s", 30),
        )
        self.last_ts: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.decisions: list[dict] = []
        self.polls = 0

    async def poll_once(self) -> list[dict]:
        """One polling round across every followed wallet. Returns the new decisions it made, if any."""
        new_decisions = []
        for address in self.addresses:
            try:
                rows = await self.client.wallet_trades(self.chain, address, max_pages=1)
            except Exception as e:
                new_decisions.append({"ts": time.time(), "wallet": address, "action": "error", "reason": str(e)[:200]})
                continue
            normalized = w.normalize_trades(rows)
            since = self.last_ts.get(address, time.time() - 3600)
            fresh = sorted((t for t in normalized if t["ts"] > since), key=lambda t: t["ts"])
            for t in fresh:
                price = t["usd"] / t["qty"] if t["qty"] else 0.0
                if price <= 0:
                    continue
                symbol = t["token"]
                if t["kind"] == "buy":
                    usd = self.portfolio.cash * self.assumptions.get("max_position_pct", 0.2)
                    filled = self.portfolio.buy(symbol, t["ts"], price, usd=usd)
                    action = "buy" if filled else "skip_buy"
                else:
                    filled = self.portfolio.sell(symbol, t["ts"], price, fraction=1.0)
                    action = "sell" if filled else "skip_sell"
                self.last_price[symbol] = price
                decision = {
                    "ts": t["ts"],
                    "wallet": address,
                    "token": symbol,
                    "action": action,
                    "wallet_usd": round(t["usd"], 2),
                    "reason": f"mirrored {address[:8]}...'s {t['kind']}",
                }
                self.decisions.append(decision)
                new_decisions.append(decision)
            if fresh:
                self.last_ts[address] = fresh[-1]["ts"]
        self.portfolio.snapshot(time.time(), self.last_price)
        self.polls += 1
        return new_decisions

    def status(self) -> dict:
        return {
            "polls": self.polls,
            "addresses": self.addresses,
            "metrics": self.portfolio.metrics(self.last_price),
            "positions": {sym: {"qty": p.qty, "cost_usd": p.cost_usd} for sym, p in self.portfolio.positions.items()},
            "recent_decisions": self.decisions[-20:],
            "latency_note": self.assumptions.get("latency_note", ""),
            "poll_s": self.poll_s,
        }
