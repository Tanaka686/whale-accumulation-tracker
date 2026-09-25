"""Deterministic, explainable wallet scoring shared by the live Wallets tab and the backtest.

Every number here comes from core.wallets (FIFO-matched trades + the wallet's own lifetime PnL).
No black box: a skill score is just win rate, lifetime realized PnL, and sample size, weighted and
capped so one huge trade can't dominate.
"""
from core import wallets as w


def skill_score(match_metrics: dict, pnl_features: dict) -> int:
    """0-100: win rate (50%), lifetime realized PnL capped at $50k (30%), sample size capped at 20 trades (20%)."""
    win_rate = match_metrics.get("win_rate") or 0
    lifetime_pnl = max(pnl_features.get("lifetime_realized_pnl_usd") or 0, 0)
    trades = match_metrics.get("trades") or 0
    pnl_component = min(lifetime_pnl, 50_000) / 50_000
    sample_component = min(trades, 20) / 20
    return round(min(100, max(0, win_rate * 50 + pnl_component * 30 + sample_component * 20)))


def profile_wallet(pnl_attrs: dict, trades_rows: list[dict], budget_usd: float, now: float | None = None) -> dict:
    """Turns raw wallet_pnl + wallet_trades rows into the fields the Wallets tab and the backtest both need."""
    normalized = w.normalize_trades(trades_rows)
    closed = w.fifo_matches(normalized)
    match_metrics = w.match_metrics(closed)
    pnl_feat = w.pnl_features(pnl_attrs)
    activity = w.activity_features(trades_rows, now=now)
    label = w.label_wallet(match_metrics, pnl_feat, activity)
    copy_score = w.copyability(match_metrics.get("median_trade_usd"), activity.get("recent_trades_per_day"), budget_usd)
    return {
        "label": label,
        "skill_score": skill_score(match_metrics, pnl_feat),
        "copyability": copy_score,
        "days_since_last_trade": activity.get("days_since_last_trade"),
        "recent_trades_per_day": activity.get("recent_trades_per_day"),
        "match_metrics": match_metrics,
        "pnl_features": pnl_feat,
        "closed_trades": closed,
    }


LABEL_DESCRIPTIONS = {
    "proven_trader": "Wins more than it loses, with a real sample of trades behind it.",
    "one_hit": "Only one closed trade so far, not enough to judge yet.",
    "whale": "Large lifetime PnL, but its trade sizes may be too big to copy at a small budget.",
    "bot_like": "Trades far more often than a human would; likely automated.",
    "flipper": "Trades often, in and out fast.",
    "dormant": "Hasn't traded in over two weeks.",
}
