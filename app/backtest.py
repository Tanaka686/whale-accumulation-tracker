"""Backtest: walk-forward wallet selection + two paper-copy scenarios (blind full-size vs size-matched).

No lookahead: a wallet is scored using only the first 60% of its own covered trade history (the
"train" slice). Only wallets that look proven in that slice get replayed against the paper engine
over the remaining 40% (the "test" slice) -- the same trades, never touched during selection.
"""
from dataclasses import dataclass

from core import wallets as w
from core.backtest import Event, walk_forward_split
from core.paper import Portfolio

TRAIN_FRAC = 0.6
SELECT_MIN_TRADES = 2
SELECT_MIN_WIN_RATE = 0.5


@dataclass
class WalletWindow:
    address: str
    events: list[Event]
    train: list[Event]
    test: list[Event]
    train_metrics: dict
    selected: bool


def trades_to_events(address: str, trades_rows: list[dict]) -> list[Event]:
    """Raw wallet_trades rows -> chronologically-ready Events, oldest first."""
    normalized = w.normalize_trades(trades_rows)
    return [Event(ts=t["ts"], kind="wallet_trade", data={**t, "wallet": address}) for t in normalized]


def covered_window(events: list[Event]) -> dict:
    if not events:
        return {"start_ts": None, "end_ts": None, "trades": 0}
    stamps = [e.ts for e in events]
    return {"start_ts": min(stamps), "end_ts": max(stamps), "trades": len(events)}


def select_wallet(address: str, events: list[Event], train_frac: float = TRAIN_FRAC) -> WalletWindow:
    """Splits one wallet's events 60/40 and scores selection using the train slice only."""
    train, test = walk_forward_split(events, train_frac)
    closed = w.fifo_matches([e.data for e in train])
    metrics = w.match_metrics(closed)
    selected = metrics["trades"] >= SELECT_MIN_TRADES and (metrics["win_rate"] or 0) >= SELECT_MIN_WIN_RATE and metrics["realized_pnl_usd"] > 0
    return WalletWindow(address=address, events=events, train=train, test=test, train_metrics=metrics, selected=selected)


def _price(trade: dict) -> float:
    return trade["usd"] / trade["qty"] if trade.get("qty") else 0.0


def replay_scenario(test_events: list[Event], scenario: str, budget_usd: float, assumptions: dict) -> Portfolio:
    """Replays merged, chronologically-sorted test-slice trades through a fresh paper Portfolio.

    scenario "blind": mirrors the wallet's own USD trade size (capped by our cash, like copying at
    full size would really behave). scenario "size_matched": every buy is sized to a fixed slice of
    *your* budget instead, so one wallet's $50k trade doesn't blow through a $100 account.
    """
    portfolio = Portfolio(
        cash=budget_usd,
        slippage_bps=assumptions.get("slippage_bps", 30),
        fee_bps=assumptions.get("fee_bps", 25),
        max_position_pct=assumptions.get("max_position_pct", 0.2),
        cooldown_s=assumptions.get("cooldown_s", 30),
    )
    last_price: dict[str, float] = {}
    for ev in sorted(test_events, key=lambda e: e.ts):
        trade = ev.data
        price = _price(trade)
        if price <= 0:
            continue
        symbol = trade["token"]
        if trade["kind"] == "buy":
            usd = trade["usd"] if scenario == "blind" else budget_usd * assumptions.get("max_position_pct", 0.2)
            portfolio.buy(symbol, trade["ts"], price, usd=usd)
        else:
            portfolio.sell(symbol, trade["ts"], price, fraction=1.0)
        last_price[symbol] = price
        portfolio.snapshot(trade["ts"], last_price)
    return portfolio


def run(wallet_trade_rows: dict[str, list[dict]], budget_usd: float, assumptions: dict, train_frac: float = TRAIN_FRAC) -> dict:
    """wallet_trade_rows: {address: raw wallet_trades rows}. Returns selection + both scenarios' metrics."""
    windows = []
    for address, rows in wallet_trade_rows.items():
        events = trades_to_events(address, rows)
        if not events:
            continue
        windows.append(select_wallet(address, events, train_frac))

    selected = [win for win in windows if win.selected]
    test_events = [e for win in selected for e in win.test]

    scenarios = {}
    for scenario in ("blind", "size_matched"):
        portfolio = replay_scenario(test_events, scenario, budget_usd, assumptions)
        scenarios[scenario] = {"metrics": portfolio.metrics(), "equity_curve": portfolio.equity_curve, "closed_trades": portfolio.closed}

    windows_all = covered_window([e for win in windows for e in win.events])
    return {
        "candidate_wallets": len(windows),
        "selected_wallets": [win.address for win in selected],
        "selection": {win.address: {"train_metrics": win.train_metrics, "selected": win.selected} for win in windows},
        "covered_window": windows_all,
        "scenarios": scenarios,
    }
