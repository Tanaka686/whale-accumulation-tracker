"""Whale Accumulation Tracker: exclusion rules, tx-hash de-duplication, net flow, stances. No network."""
import json
from datetime import datetime

import pytest

from app import accumulation as acc
from core.client import CoinGeckoError

TOKEN = "0x" + "ab" * 20
POOL = "0x" + "cc" * 20
WETH = "0x" + "ee" * 20
NOW = 1_790_000_000  # a fixed clock so the window is predictable


def addr(n: int) -> str:
    return "0x" + f"{n:02x}" * 20


def holder(rank, address, label=None, amount="1000", pct="1.5", value="2000"):
    return {"rank": rank, "address": address, "label": label, "amount": amount, "percentage": pct, "value": value}


def buy(amount, usd, tx, token_on="to"):
    if token_on == "to":
        return {"kind": "buy", "from_token_address": WETH, "to_token_address": TOKEN, "from_token_amount": "0.01", "to_token_amount": str(amount), "volume_in_usd": str(usd), "tx_hash": tx, "pool_address": POOL, "block_timestamp": "2026-09-29T10:00:00Z"}
    return {"kind": "sell", "from_token_address": TOKEN, "to_token_address": WETH, "from_token_amount": str(amount), "to_token_amount": "0.01", "volume_in_usd": str(usd), "tx_hash": tx, "pool_address": POOL, "block_timestamp": "2026-09-29T10:00:00Z"}


def transfer(direction, amount, tx, **extra):
    row = {"tx_hash": tx, "direction": direction, "token_address": TOKEN, "amount": str(amount), "amount_raw": str(int(amount * 10**18)), "decimals": 18, "from_address": POOL, "to_address": addr(9), "block_timestamp": "2026-09-29T10:00:00Z"}
    row.update(extra)
    return row


class FakeWhaleClient:
    """Canned top_holders / token / wallet_trades / wallet_transfers answers, recording every call."""

    def __init__(self, holders, pools=(), price="2", trades=None, transfers=None, fail=()):
        self._holders = holders
        self._pools = list(pools)
        self._price = price
        self._trades = {k.lower(): v for k, v in (trades or {}).items()}
        self._transfers = {k.lower(): v for k, v in (transfers or {}).items()}
        self._fail = {a.lower() for a in fail}
        self.credits_used = 0
        self.holder_calls: list[dict] = []
        self.wallet_calls: list[dict] = []

    async def top_holders(self, network, token, n=20, include_pnl_details=False):
        self.credits_used += 1
        self.holder_calls.append({"network": network, "token": token, "n": n})
        return self._holders

    async def token(self, network, address):
        self.credits_used += 1
        return {"attributes": {"symbol": "TST", "name": "Test Token", "price_usd": self._price}, "pools": [{"attributes": {"address": p}} for p in self._pools]}

    async def _wallet(self, kind, network, address, max_pages, per_page, token, from_ts, to_ts):
        self.credits_used += 1
        self.wallet_calls.append({"kind": kind, "address": address, "token": token, "from": from_ts, "to": to_ts, "per_page": per_page})
        if address.lower() in self._fail:
            raise CoinGeckoError(500, "boom")
        source = self._trades if kind == "trades" else self._transfers
        return source.get(address.lower(), [])

    async def wallet_trades(self, network, address, max_pages=10, per_page=None, token=None, from_ts=None, to_ts=None):
        return await self._wallet("trades", network, address, max_pages, per_page, token, from_ts, to_ts)

    async def wallet_transfers(self, network, address, max_pages=10, per_page=None, token=None, from_ts=None, to_ts=None, direction=None):
        return await self._wallet("transfers", network, address, max_pages, per_page, token, from_ts, to_ts)


# ---------- exclusion rules ----------


def verdict(label=None, address=None, pools=()):
    return acc.classify_holder(holder(1, address or addr(1), label), TOKEN, set(pools))


@pytest.mark.parametrize(
    "label, code",
    [
        ("Voting Escrow", "contract_label"),
        ("Volatile AMM - USDC/AERO (VAMM-USDC/AERO)", "contract_label"),
        ("LP", "contract_label"),
        ("Uniswap Router", "contract_label"),
        ("Yield Vault", "contract_label"),
        ("Base Bridge", "contract_label"),
        ("Aerodrome(ExtraFi Interest Bearing Token) (EAERO)", "contract_label"),
        ("Cold Wallet", "exchange_label"),
        ("Hot Wallet", "exchange_label"),
        ("Binance 14", "exchange_label"),
        ("okx deposit", "exchange_label"),
        ("Some Exchange", "exchange_label"),
    ],
)
def test_contract_and_exchange_labels_are_excluded_with_a_reason(label, code):
    v = verdict(label)
    assert v["kind"] == "excluded"
    assert v["reason_code"] == code
    assert label in v["reason"]


@pytest.mark.parametrize("label", ["Gnosis Safe Proxy", "gnosis safe", "Safe Proxy", "Team Multisig"])
def test_multisigs_are_kept_and_tagged(label):
    v = verdict(label)
    assert v["kind"] == "whale"
    assert v["tags"] == ["Multisig"]


@pytest.mark.parametrize("label", ["Foundation Treasury", "Vitalik", "Help Desk"])
def test_other_labels_are_kept_without_a_tag(label):
    v = verdict(label)
    assert v["kind"] == "whale"
    assert v["tags"] == []
    assert label in v["reason"]  # the label is part of what is shown


def test_short_keywords_need_a_whole_word():
    assert verdict("Help Desk")["kind"] == "whale"  # "LP" is inside "Help" but is not a word
    assert verdict("Sammy's Wallet")["kind"] == "whale"  # "AMM" is inside "Sammy" but is not a word
    assert verdict("LP")["kind"] == "excluded"


def test_unlabelled_holder_is_a_whale():
    v = verdict(None)
    assert v["kind"] == "whale"
    assert v["reason_code"] == "no_label"


def test_burn_token_and_pool_addresses_are_excluded_even_without_a_label():
    assert verdict(address="0x" + "0" * 40)["reason_code"] == "burn_address"
    assert verdict(address="0x000000000000000000000000000000000000dEaD")["reason_code"] == "burn_address"
    assert verdict(address=TOKEN.upper().replace("0X", "0x"))["reason_code"] == "token_contract"
    assert verdict(address=POOL, pools={POOL})["reason_code"] == "liquidity_pool"


def test_a_contract_keyword_beats_the_multisig_keyword():
    assert verdict("Safe Vault")["kind"] == "excluded"


# ---------- de-duplication and net flow ----------


def test_a_swap_reported_as_trade_and_transfer_is_counted_once():
    trades = acc.trade_events([buy(100, 200, "0xAA")], TOKEN)
    transfers = acc.transfer_events([transfer("in", 100, "0xaa")], TOKEN)  # same tx, different case
    events, dropped = acc.combine_events(trades, transfers)
    assert dropped == 1
    flow = acc.compute_flow(events, price_usd=2.0)
    assert flow["net_tokens"] == 100
    assert flow["net_usd"] == 200
    assert flow["bought"]["tokens"] == 100 and flow["transferred_in"]["tokens"] == 0


def test_transfers_in_other_transactions_or_directions_are_kept():
    trades = acc.trade_events([buy(100, 200, "0xaa1")], TOKEN)
    transfers = acc.transfer_events(
        [transfer("in", 100, "0xaa1"), transfer("in", 50, "0xbb2"), transfer("out", 30, "0xaa1")], TOKEN
    )
    events, dropped = acc.combine_events(trades, transfers)
    assert dropped == 1  # only the "in" of the swap's own tx
    flow = acc.compute_flow(events, price_usd=2.0)
    assert flow["transferred_in"]["tokens"] == 50
    assert flow["transferred_out"]["tokens"] == 30
    assert flow["net_tokens"] == 100 + 50 - 30
    assert flow["net_usd"] == 200 + 100 - 60  # transfers are priced at 2.0


def test_net_flow_is_bought_plus_in_minus_sold_plus_out():
    trades = acc.trade_events([buy(100, 300, "0x1"), buy(40, 100, "0x2", token_on="from")], TOKEN)
    transfers = acc.transfer_events([transfer("in", 10, "0x3"), transfer("out", 5, "0x4")], TOKEN)
    events, _ = acc.combine_events(trades, transfers)
    flow = acc.compute_flow(events, price_usd=1.0)
    assert flow["bought"]["tokens"] == 100 and flow["sold"]["tokens"] == 40
    assert flow["net_tokens"] == (100 + 10) - (40 + 5)
    assert flow["net_usd"] == (300 + 10) - (100 + 5)


def test_trade_direction_follows_the_token_side_not_the_kind_field():
    rows = [buy(10, 20, "0x1", token_on="from")]
    rows[0]["kind"] = "buy"  # deliberately misleading
    assert acc.trade_events(rows, TOKEN)[0]["direction"] == "out"


def test_exact_duplicate_rows_from_paging_are_dropped():
    rows = [buy(10, 20, "0x1"), buy(10, 20, "0x1")]
    assert len(acc.trade_events(rows, TOKEN)) == 1
    assert len(acc.transfer_events([transfer("in", 5, "0x9"), transfer("in", 5, "0x9")], TOKEN)) == 1


def test_two_different_swaps_in_one_tx_are_both_counted():
    rows = [buy(10, 20, "0x1"), {**buy(15, 30, "0x1"), "pool_address": "0x" + "dd" * 20}]
    assert len(acc.trade_events(rows, TOKEN)) == 2


def test_transfer_amount_falls_back_to_amount_raw_over_decimals():
    row = {**transfer("in", 1, "0x5"), "amount": None, "amount_raw": "2500000", "decimals": 6}
    assert acc.transfer_events([row], TOKEN)[0]["amount"] == 2.5


def test_no_price_leaves_transfer_usd_out_and_says_so():
    events, _ = acc.combine_events([], acc.transfer_events([transfer("in", 5, "0x9")], TOKEN))
    flow = acc.compute_flow(events, price_usd=None)
    assert flow["net_tokens"] == 5 and flow["net_usd"] == 0 and flow["usd_incomplete"] is True


# ---------- stances ----------


def test_new_position_when_the_start_balance_is_zero():
    s = acc.stance_for(current_balance=1000, net_tokens=1000)
    assert s["stance"] == "New position"
    assert s["start_balance"] == 0
    assert s["net_pct_of_start"] is None  # no percentage of nothing
    assert s["warning"] is None


def test_new_position_when_the_start_is_under_one_percent_of_current():
    assert acc.stance_for(1000, 995)["stance"] == "New position"  # start 5 = 0.5%
    assert acc.stance_for(1000, 990)["stance"] == "Accumulating"  # start 10 = exactly 1%, not under it


def test_accumulating_above_plus_two_percent():
    s = acc.stance_for(current_balance=1021, net_tokens=21)  # start 1000, +2.1%
    assert s["stance"] == "Accumulating"
    assert s["net_pct_of_start"] == pytest.approx(2.1)


def test_distributing_below_minus_two_percent():
    s = acc.stance_for(current_balance=979, net_tokens=-21)  # start 1000, -2.1%
    assert s["stance"] == "Distributing"
    assert s["net_pct_of_start"] == pytest.approx(-2.1)


def test_holding_inside_the_two_percent_band_including_the_edges():
    assert acc.stance_for(1000, 0)["stance"] == "Holding"
    assert acc.stance_for(1020, 20)["stance"] == "Holding"  # exactly +2.0%
    assert acc.stance_for(980, -20)["stance"] == "Holding"  # exactly -2.0%


def test_negative_start_is_flagged_and_treated_as_zero():
    s = acc.stance_for(current_balance=100, net_tokens=150)
    assert s["stance"] == "New position"
    assert s["start_balance"] == 0
    assert "negative" in s["warning"]


def test_zero_current_balance_has_no_stance():
    assert acc.stance_for(0, 0)["stance"] is None


# ---------- the whole scan ----------


def sample_client():
    holders = [
        holder(1, POOL, None, amount="5000"),
        holder(2, addr(2), "Cold Wallet", amount="4000"),
        holder(3, addr(3), "Gnosis Safe Proxy", amount="1000"),
        holder(4, addr(4), None, amount="1000"),
        holder(5, addr(5), None, amount="500"),
        holder(6, addr(6), "Foundation Treasury", amount="800"),
    ]
    swap = buy(21, 42, "0xAA1")
    return FakeWhaleClient(
        holders,
        pools=[POOL],
        price="2",
        trades={addr(3): [swap], addr(4): [buy(30, 60, "0xbb1", token_on="from")]},
        transfers={addr(3): [transfer("in", 21, "0xaa1")], addr(6): [transfer("in", 800, "0xcc1")]},
    )


async def test_scan_end_to_end(tmp_path):
    client = sample_client()
    result = await acc.scan(client, "base", TOKEN, days=7, now=NOW, save_dir=tmp_path)

    assert [(e["rank"], e["reason_code"]) for e in result["excluded"]] == [(1, "liquidity_pool"), (2, "exchange_label")]
    assert result["excluded"][1]["label"] == "Cold Wallet"
    assert "Cold Wallet" in result["excluded"][1]["reason"]

    by_rank = {w["rank"]: w for w in result["whales"]}
    assert set(by_rank) == {3, 4, 5, 6}
    assert by_rank[3]["tags"] == ["Multisig"] and by_rank[3]["label"] == "Gnosis Safe Proxy"
    assert by_rank[3]["stance"] == "Accumulating" and by_rank[3]["duplicate_transfers_dropped"] == 1
    assert by_rank[3]["net_flow_tokens"] == 21
    assert by_rank[4]["stance"] == "Distributing" and by_rank[4]["net_flow_tokens"] == -30
    assert by_rank[5]["stance"] == "Holding" and by_rank[5]["net_flow_tokens"] == 0
    assert by_rank[6]["stance"] == "New position" and by_rank[6]["label"] == "Foundation Treasury"

    s = result["summary"]
    assert (s["whales"], s["excluded"], s["new_position"], s["accumulating"], s["distributing"], s["holding"], s["errors"]) == (4, 2, 1, 1, 1, 1, 0)
    assert s["total_net_flow_tokens"] == 21 - 30 + 0 + 800
    assert s["total_net_flow_usd"] == 42 - 60 + 0 + 1600  # the 800-token transfer priced at 2.0
    assert result["symbol"] == "TST" and result["price_usd"] == 2.0


async def test_scan_asks_for_50_holders_by_default_and_uses_the_window_and_token_filter(tmp_path):
    client = sample_client()
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    assert client.holder_calls == [{"network": "base", "token": TOKEN, "n": 50}]
    assert result["days"] == 7 and result["holders_requested"] == 50
    frm, to = result["window"]["from"], result["window"]["to"]
    assert (datetime.fromisoformat(to) - datetime.fromisoformat(frm)).days == 7
    assert client.wallet_calls and all(c["token"] == TOKEN and c["from"] == frm and c["to"] == to and c["per_page"] == 300 for c in client.wallet_calls)
    assert {c["kind"] for c in client.wallet_calls} == {"trades", "transfers"}
    assert {c["address"] for c in client.wallet_calls} == {addr(3), addr(4), addr(5), addr(6)}  # excluded wallets cost nothing


async def test_scan_can_use_a_30_day_window(tmp_path):
    result = await acc.scan(sample_client(), "eth", TOKEN, days=30, now=NOW, save_dir=tmp_path)
    frm, to = result["window"]["from"], result["window"]["to"]
    assert (datetime.fromisoformat(to) - datetime.fromisoformat(frm)).days == 30


async def test_scan_counts_credits_and_saves_a_json_file(tmp_path):
    client = sample_client()
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    assert result["credits"] == 1 + 1 + 2 * 4  # holders + token + (trades + transfers) for 4 whales
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert result["saved_to"].endswith(files[0].name)
    saved = json.loads(files[0].read_text())
    assert saved["summary"] == result["summary"]
    assert saved["excluded"][0]["label"] is None and saved["excluded"][1]["label"] == "Cold Wallet"


async def test_scan_can_skip_saving(tmp_path):
    result = await acc.scan(sample_client(), "base", TOKEN, now=NOW, save=False, save_dir=tmp_path)
    assert result["saved_to"] is None
    assert list(tmp_path.glob("*.json")) == []


async def test_a_wallet_that_fails_is_reported_and_left_out_of_the_totals(tmp_path):
    client = sample_client()
    client._fail = {addr(4)}
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    bad = next(w for w in result["whales"] if w["rank"] == 4)
    assert bad["error"] and bad["stance"] is None
    assert result["summary"]["errors"] == 1
    assert result["summary"]["distributing"] == 0
    assert result["summary"]["total_net_flow_tokens"] == 21 + 0 + 800


async def test_a_wallet_with_a_zero_starting_balance_is_a_new_position(tmp_path):
    client = FakeWhaleClient([holder(1, addr(1), None, amount="1000")], trades={addr(1): [buy(1000, 2000, "0x1")]})
    result = await acc.scan(client, "bsc", TOKEN, now=NOW, save_dir=tmp_path)
    w = result["whales"][0]
    assert w["stance"] == "New position"
    assert w["start_balance"] == 0
    assert w["net_flow_pct_of_start"] is None
    assert w["warnings"] == []


@pytest.mark.parametrize(
    "kwargs",
    [{"network": "solana"}, {"token": "not-an-address"}, {"days": 14}],
)
async def test_bad_input_is_rejected_before_any_api_call(kwargs):
    client = sample_client()
    args = {"network": "base", "token": TOKEN, "days": 7, **kwargs}
    with pytest.raises(acc.WhaleScanInputError):
        await acc.scan(client, args["network"], args["token"], days=args["days"], save=False)
    assert client.credits_used == 0
