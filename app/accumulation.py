"""Whale Accumulation Tracker: are a token's biggest holders buying or selling it over the last 7 or 30 days?

1. classify_holder(): the token's top holders, split into whales and excluded wallets (burn addresses,
   the token's own contract, its liquidity pools, contracts and exchanges by CoinGecko label). The keyword
   lists live in app/config.py. Excluded wallets are always returned with their label and the reason.
2. For each whale: its trades and transfers of this token inside the window (two calls, cursor-paginated).
3. combine_events(): a swap shows up as a trade AND as a token transfer with the same tx hash. The trade
   wins, so a swap is never counted twice.
4. compute_flow(): net flow = (bought + transferred in) - (sold + transferred out), in tokens and USD.
5. stance_for(): the balance at the start of the window is current balance - net flow (the API has no
   historical balances), then New position / Accumulating / Distributing / Holding.
6. scan(): runs all of it and saves the result as one JSON file per scan.

Known limits (also written into each result's `warnings`): only *current* top holders are listed, so a
whale that sold everything during the window is not in the list; and USD for plain transfers is priced at
today's token price, since the transfers endpoint has no USD field.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.client import CoinGeckoClient, CoinGeckoError, PlanRestrictedError
from core.wallets import _f, _ts

from . import config

PER_PAGE = 300  # the maximum the API allows
_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WhaleScanInputError(ValueError):
    """The network, token address or window asked for is not one this tracker supports."""


def _short(addr: str | None) -> str:
    if not addr or len(addr) < 10:
        return addr or ""
    return f"{addr[:6]}...{addr[-4:]}"


# ---- 1. who counts as a whale ----


def _match_keyword(label: str, keywords: list[str]) -> str | None:
    """The first keyword found in `label` (case-insensitive), or None. Keywords of <= 3 characters
    (AMM, LP, OKX) must match a whole word, so "LP" does not fire on "Help"."""
    text = label.lower()
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        if len(k) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text):
                return kw
        elif k in text:
            return kw
    return None


def classify_holder(holder: dict, token: str, pool_addresses: set[str]) -> dict:
    """{"kind": "whale" | "excluded", "reason_code", "reason", "tags"} for one top_holders row.

    Order: burn address, the token's own contract, one of its pools, then the label
    (contract keywords, exchange keywords, multisig keywords). An unknown label is kept as a whale."""
    address = (holder.get("address") or "").lower()
    label = (holder.get("label") or "").strip()

    def excluded(code: str, reason: str) -> dict:
        return {"kind": "excluded", "reason_code": code, "reason": reason, "tags": []}

    if address in {a.lower() for a in config.WHALE_BURN_ADDRESSES}:
        return excluded("burn_address", "burn / zero address")
    if address == token.lower():
        return excluded("token_contract", "the token's own contract")
    if address in pool_addresses:
        return excluded("liquidity_pool", "one of this token's liquidity pools")
    if label:
        hit = _match_keyword(label, config.WHALE_CONTRACT_KEYWORDS)
        if hit:
            return excluded("contract_label", f'label "{label}" matches contract keyword "{hit}"')
        hit = _match_keyword(label, config.WHALE_EXCHANGE_KEYWORDS)
        if hit:
            return excluded("exchange_label", f'label "{label}" matches exchange keyword "{hit}"')
        if _match_keyword(label, config.WHALE_MULTISIG_KEYWORDS):
            return {"kind": "whale", "reason_code": "multisig", "reason": f'multisig wallet ("{label}"), kept as a whale', "tags": ["Multisig"]}
        return {"kind": "whale", "reason_code": "other_label", "reason": f'has label "{label}" but it is not a contract or exchange, kept as a whale', "tags": []}
    return {"kind": "whale", "reason_code": "no_label", "reason": "no label", "tags": []}


# ---- 2/3. events from trades and transfers, de-duplicated by tx hash ----


def trade_events(rows: list[dict], token: str) -> list[dict]:
    """Wallet trade rows -> {source, direction, amount, usd, tx, ts} for `token` only.

    Direction comes from which side of the swap `token` is on (not from the buy/sell `kind`), so it stays
    correct whichever side of the pool the token is. Exact duplicate rows (e.g. from paging) are dropped."""
    token = token.lower()
    out, seen = [], set()
    for r in rows:
        to_t = (r.get("to_token_address") or "").lower()
        from_t = (r.get("from_token_address") or "").lower()
        if to_t == token and from_t != token:
            direction, amount = "in", _f(r.get("to_token_amount"))
        elif from_t == token and to_t != token:
            direction, amount = "out", _f(r.get("from_token_amount"))
        else:
            continue
        if not amount:
            continue
        tx = (r.get("tx_hash") or "").lower()
        key = (tx, r.get("pool_address"), from_t, to_t, r.get("from_token_amount"), r.get("to_token_amount"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": "trade", "direction": direction, "amount": amount, "usd": _f(r.get("volume_in_usd")), "tx": tx, "ts": _ts(r.get("block_timestamp"))})
    return out


def transfer_events(rows: list[dict], token: str) -> list[dict]:
    """Wallet transfer rows -> {source, direction, amount, usd (None), tx, ts} for `token` only."""
    token = token.lower()
    out, seen = [], set()
    for r in rows:
        if (r.get("token_address") or "").lower() != token or r.get("direction") not in ("in", "out"):
            continue
        amount = _f(r.get("amount"))
        if amount is None:  # `amount` is null when the token can't be resolved; amount_raw is always there
            raw, decimals = _f(r.get("amount_raw")), r.get("decimals")
            amount = raw / 10**decimals if raw is not None and isinstance(decimals, int) else None
        if not amount:
            continue
        tx = (r.get("tx_hash") or "").lower()
        key = (tx, r["direction"], (r.get("from_address") or "").lower(), (r.get("to_address") or "").lower(), r.get("amount_raw"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": "transfer", "direction": r["direction"], "amount": amount, "usd": None, "tx": tx, "ts": _ts(r.get("block_timestamp"))})
    return out


def combine_events(trades: list[dict], transfers: list[dict]) -> tuple[list[dict], int]:
    """Trades plus the transfers that are not just the other half of one of those trades.

    A swap that moves `token` into (or out of) the wallet is reported by the trades endpoint AND by the
    transfers endpoint with the same tx hash. When a tx has a trade in a direction, the transfers of that
    tx in the same direction are dropped. Returns (events, number of transfers dropped)."""
    covered = {(t["tx"], t["direction"]) for t in trades if t["tx"]}
    kept = [t for t in transfers if not (t["tx"] and (t["tx"], t["direction"]) in covered)]
    return trades + kept, len(transfers) - len(kept)


# ---- 4. net flow ----


def compute_flow(events: list[dict], price_usd: float | None) -> dict:
    """Net flow = (bought + transferred in) - (sold + transferred out), in tokens and in USD.

    Trades use their own USD volume; transfers are priced at `price_usd` (today's price). If there is
    no price, transfer USD is left out and `usd_incomplete` is set."""
    parts = {k: {"tokens": 0.0, "usd": 0.0} for k in ("bought", "sold", "transferred_in", "transferred_out")}
    usd_incomplete = False
    for e in events:
        name = {("trade", "in"): "bought", ("trade", "out"): "sold", ("transfer", "in"): "transferred_in", ("transfer", "out"): "transferred_out"}[(e["source"], e["direction"])]
        usd = e["usd"]
        if usd is None and price_usd:
            usd = e["amount"] * price_usd
        if usd is None:
            usd_incomplete = True
            usd = 0.0
        parts[name]["tokens"] += e["amount"]
        parts[name]["usd"] += usd
    net_tokens = parts["bought"]["tokens"] + parts["transferred_in"]["tokens"] - parts["sold"]["tokens"] - parts["transferred_out"]["tokens"]
    net_usd = parts["bought"]["usd"] + parts["transferred_in"]["usd"] - parts["sold"]["usd"] - parts["transferred_out"]["usd"]
    return {**parts, "net_tokens": net_tokens, "net_usd": net_usd, "usd_incomplete": usd_incomplete}


# ---- 5. stance ----


def stance_for(current_balance: float, net_tokens: float) -> dict:
    """The wallet's stance from its current balance and its net flow over the window.

    start = current - net flow (the API has no historical balances).
      start < 1% of current                -> "New position"
      net flow > +2% of start              -> "Accumulating"
      net flow < -2% of start              -> "Distributing"
      otherwise                            -> "Holding"
    A negative start means the flow data and the holder snapshot disagree (e.g. more pages than we
    fetched); it is treated as zero and flagged in `warning`."""
    if current_balance <= 0:
        return {"stance": None, "start_balance": None, "net_pct_of_start": None, "warning": "current balance is zero"}
    start = current_balance - net_tokens
    warning = None
    if start < 0:
        warning = "start balance came out negative: flow data and holder snapshot disagree; treated as zero"
        start = 0.0
    if start < current_balance * config.WHALE_NEW_POSITION_MAX_START_PCT / 100:
        return {"stance": "New position", "start_balance": start, "net_pct_of_start": None, "warning": warning}
    pct = net_tokens / start * 100
    if pct > config.WHALE_STANCE_THRESHOLD_PCT:
        name = "Accumulating"
    elif pct < -config.WHALE_STANCE_THRESHOLD_PCT:
        name = "Distributing"
    else:
        name = "Holding"
    return {"stance": name, "start_balance": start, "net_pct_of_start": pct, "warning": warning}


# ---- 6. the scan ----


def _window(now: float, days: int) -> tuple[str, str]:
    """(from, to) as UTC 'YYYY-MM-DDTHH:MM' strings, `to` = now rounded down to the minute."""
    end = datetime.fromtimestamp(now, timezone.utc).replace(second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M"
    return (end - timedelta(days=days)).strftime(fmt), end.strftime(fmt)


async def _analyze_whale(client, network: str, token: str, holder: dict, verdict: dict, price_usd: float | None, frm: str, to: str) -> dict:
    balance = _f(holder.get("amount"), 0.0) or 0.0
    row = {
        "rank": holder.get("rank"),
        "address": holder.get("address"),
        "short": _short(holder.get("address")),
        "label": holder.get("label") or None,
        "tags": verdict["tags"],
        "supply_pct": _f(holder.get("percentage")),
        "balance": balance,
        "balance_usd": _f(holder.get("value")),
        "error": None,
    }
    try:
        trades, transfers = await asyncio.gather(
            client.wallet_trades(network, holder["address"], max_pages=config.WHALE_MAX_PAGES, per_page=PER_PAGE, token=token, from_ts=frm, to_ts=to),
            client.wallet_transfers(network, holder["address"], max_pages=config.WHALE_MAX_PAGES, per_page=PER_PAGE, token=token, from_ts=frm, to_ts=to),
        )
    except PlanRestrictedError:
        raise
    except CoinGeckoError as e:
        return {**row, "stance": None, "error": f"{type(e).__name__}: {str(e)[:120]}", "warnings": []}
    t_events, x_events = trade_events(trades, token), transfer_events(transfers, token)
    events, dropped = combine_events(t_events, x_events)
    flow = compute_flow(events, price_usd)
    st = stance_for(balance, flow["net_tokens"])
    warnings = []
    if st["warning"]:
        warnings.append(st["warning"])
    truncated = len(trades) >= PER_PAGE * config.WHALE_MAX_PAGES or len(transfers) >= PER_PAGE * config.WHALE_MAX_PAGES
    if truncated:
        warnings.append("very active wallet: more rows than we fetched, so the flow may be incomplete")
    if flow["usd_incomplete"]:
        warnings.append("no token price: USD for transfers is missing")
    return {
        **row,
        "stance": st["stance"],
        "start_balance": st["start_balance"],
        "net_flow_tokens": flow["net_tokens"],
        "net_flow_usd": flow["net_usd"],
        "net_flow_pct_of_start": st["net_pct_of_start"],
        "flows": {k: flow[k] for k in ("bought", "sold", "transferred_in", "transferred_out")},
        "trade_count": len(t_events),
        "transfer_count": len(x_events) - dropped,
        "duplicate_transfers_dropped": dropped,
        "truncated": truncated,
        "warnings": warnings,
    }


def save_scan(result: dict, save_dir: str | Path, now: float) -> str:
    """Writes the scan as data/whales/<time>-<network>-<token>.json and returns the path."""
    folder = Path(save_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}-{result['network']}-{result['token'][:10]}.json"
    result["saved_to"] = path.as_posix()
    path.write_text(json.dumps(result, indent=2, default=str))
    return result["saved_to"]


async def scan(
    client: CoinGeckoClient,
    network: str,
    token: str,
    days: int = config.WHALE_WINDOW_DAYS[0],
    holders: int = config.WHALE_DEFAULT_HOLDERS,
    now: float | None = None,
    save: bool = True,
    save_dir: str | Path = config.WHALE_SCANS_DIR,
) -> dict:
    """Runs one whale accumulation scan for `token` on `network` over the last `days` (7 or 30)."""
    if network not in config.WHALE_NETWORKS:
        raise WhaleScanInputError(f"network must be one of {', '.join(config.WHALE_NETWORKS)}")
    token = (token or "").strip()
    if not _EVM_ADDR.match(token):
        raise WhaleScanInputError("token must be an EVM contract address: 0x followed by 40 hex characters")
    if days not in config.WHALE_WINDOW_DAYS:
        raise WhaleScanInputError(f"days must be one of {', '.join(map(str, config.WHALE_WINDOW_DAYS))}")
    holders = max(1, min(int(holders), 50))
    token = token.lower()
    now = now if now is not None else time.time()
    frm, to = _window(now, days)
    credits0 = getattr(client, "credits_used", 0)
    warnings = [
        "Only current top holders are listed: a whale that sold everything during the window is not shown.",
        "The starting balance is estimated as current balance minus net flow; USD for plain transfers uses today's price.",
    ]

    holder_rows = await client.top_holders(network, token, n=holders)
    try:
        info = await client.token(network, token)
    except PlanRestrictedError:
        raise
    except CoinGeckoError as e:
        info = {"attributes": {}, "pools": []}
        warnings.append(f"could not load the token's pools ({type(e).__name__}); pool wallets are only caught by label")
    attrs = info.get("attributes") or {}
    price_usd = _f(attrs.get("price_usd"))
    pool_addresses = {((p.get("attributes") or {}).get("address") or "").lower() for p in info.get("pools") or []} - {""}
    if len(holder_rows) < holders:
        warnings.append(f"the API returned {len(holder_rows)} holders (asked for {holders})")

    whales_in, excluded = [], []
    for h in holder_rows:
        if not h.get("address"):
            continue
        verdict = classify_holder(h, token, pool_addresses)
        if verdict["kind"] == "excluded":
            excluded.append(
                {
                    "rank": h.get("rank"),
                    "address": h["address"],
                    "short": _short(h["address"]),
                    "label": h.get("label") or None,
                    "reason_code": verdict["reason_code"],
                    "reason": verdict["reason"],
                    "supply_pct": _f(h.get("percentage")),
                    "balance_usd": _f(h.get("value")),
                }
            )
        else:
            whales_in.append((h, verdict))

    whales = await asyncio.gather(*(_analyze_whale(client, network, token, h, v, price_usd, frm, to) for h, v in whales_in))
    ok = [w for w in whales if not w["error"]]
    count = lambda name: sum(1 for w in ok if w["stance"] == name)  # noqa: E731
    summary = {
        "holders_fetched": len(holder_rows),
        "whales": len(whales),
        "excluded": len(excluded),
        "new_position": count("New position"),
        "accumulating": count("Accumulating"),
        "distributing": count("Distributing"),
        "holding": count("Holding"),
        "errors": len(whales) - len(ok),
        "total_net_flow_tokens": sum(w["net_flow_tokens"] for w in ok),
        "total_net_flow_usd": sum(w["net_flow_usd"] for w in ok),
    }
    result = {
        "network": network,
        "token": token,
        "symbol": attrs.get("symbol"),
        "name": attrs.get("name"),
        "price_usd": price_usd,
        "days": days,
        "window": {"from": frm, "to": to},
        "holders_requested": holders,
        "summary": summary,
        "whales": whales,
        "excluded": excluded,
        "warnings": warnings,
        "credits": getattr(client, "credits_used", 0) - credits0,
        "scanned_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "saved_to": None,
    }
    if save:
        save_scan(result, save_dir, now)
    return result
