"""Whale Accumulation Tracker fixes: page limits and the per-wallet credit cap, the volume/liquidity warning,
and "New position" vs tokens that simply came back from a pool. (Retries on 408/5xx are in test_client_retry.py.)"""
import pytest

from app import accumulation as acc
from tests.test_accumulation import NOW, POOL, TOKEN, FakeWhaleClient, addr, buy, holder, transfer

VE = addr(0x51)


# ---------- 1. page limits ----------


def test_the_default_limits_are_ten_pages_of_300_rows_and_a_20_credit_cap():
    assert acc.config.WHALE_MAX_PAGES == 10
    assert acc.PER_PAGE == 300
    assert acc.config.WHALE_MAX_CREDITS_PER_WALLET == 20
    assert acc.pages_per_call() == 10  # 10 pages x 2 calls = 20 credits at most per wallet


def test_the_credit_cap_lowers_the_pages_per_call(monkeypatch):
    monkeypatch.setattr(acc.config, "WHALE_MAX_CREDITS_PER_WALLET", 8)
    assert acc.pages_per_call() == 4
    monkeypatch.setattr(acc.config, "WHALE_MAX_CREDITS_PER_WALLET", 1)
    assert acc.pages_per_call() == 1  # never zero
    monkeypatch.setattr(acc.config, "WHALE_MAX_CREDITS_PER_WALLET", 500)
    assert acc.pages_per_call() == 10  # the page limit still applies


async def test_wallet_calls_use_the_page_limit_and_the_credit_cap(tmp_path, monkeypatch):
    client = FakeWhaleClient([holder(3, addr(3), None)])
    await acc.scan(client, "base", TOKEN, now=NOW, save=False)
    assert {c["max_pages"] for c in client.wallet_calls} == {10} and {c["per_page"] for c in client.wallet_calls} == {300}

    monkeypatch.setattr(acc.config, "WHALE_MAX_CREDITS_PER_WALLET", 8)
    client = FakeWhaleClient([holder(3, addr(3), None)])
    await acc.scan(client, "base", TOKEN, now=NOW, save=False)
    assert {c["max_pages"] for c in client.wallet_calls} == {4}


def rows(n):
    return [transfer("in", 0.001, f"0x{i:05x}") for i in range(n)]


async def test_a_wallet_that_fills_all_ten_pages_is_incomplete_with_the_bot_reason(tmp_path):
    client = FakeWhaleClient([holder(3, addr(3), None, amount="1000")], transfers={addr(3): rows(3000)})
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    w = result["whales"][0]
    assert w["stance"] == "Incomplete data" and w["truncated"] is True
    assert w["stance_reason"] == "very active wallet (possible bot or market maker)"
    assert any("very active wallet (possible bot or market maker)" in x and x.startswith("Incomplete data") for x in result["warnings"])
    assert result["summary"]["incomplete"] == 1 and result["summary"]["total_net_flow_tokens"] == 0


async def test_a_busy_wallet_just_under_the_limit_is_complete(tmp_path):
    client = FakeWhaleClient([holder(3, addr(3), None, amount="1000")], transfers={addr(3): rows(2999)})
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    w = result["whales"][0]
    assert w["truncated"] is False and w["stance"] == "Holding"  # +2.999 tokens on ~997 is +0.3%
    assert result["summary"]["incomplete"] == 0


# ---------- 3. volume vs liquidity ----------

WARNING = "Volume looks unusually high compared to liquidity; possible bot or wash trading."


def market(volume, liquidity):
    return {"volume_usd": {"h24": str(volume)}, "total_reserve_in_usd": str(liquidity)}


async def test_volume_over_50x_liquidity_adds_the_warning_first():
    client = FakeWhaleClient([holder(3, addr(3), None)], token_attrs=market(6000, 100))
    result = await acc.scan(client, "base", TOKEN, now=NOW, save=False)
    assert result["warnings"][0] == WARNING
    assert result["market"] == {"volume_24h_usd": 6000, "liquidity_usd": 100, "volume_to_liquidity": 60}


@pytest.mark.parametrize("attrs", [market(5000, 100), market(1000, 100), market(6000, 0), {}, {"volume_usd": {"h24": "6000"}}])
async def test_no_warning_at_50x_or_below_or_without_data(attrs):
    client = FakeWhaleClient([holder(3, addr(3), None)], token_attrs=attrs)
    result = await acc.scan(client, "base", TOKEN, now=NOW, save=False)
    assert WARNING not in result["warnings"]


async def test_the_volume_check_costs_no_extra_credits():
    plain = FakeWhaleClient([holder(3, addr(3), None)])
    flagged = FakeWhaleClient([holder(3, addr(3), None)], token_attrs=market(6000, 100))
    a = await acc.scan(plain, "base", TOKEN, now=NOW, save=False)
    b = await acc.scan(flagged, "base", TOKEN, now=NOW, save=False)
    assert a["credits"] == b["credits"] == 1 + 1 + 2  # holders + token + trades + transfers for one whale


# ---------- 4. "New position" that is really tokens coming back from a pool ----------


def lp_holders(whale_amount="1000"):
    return [holder(1, POOL, None, amount="9000"), holder(2, VE, "Voting Escrow", amount="9000"), holder(3, addr(3), None, amount=whale_amount)]


async def scan_whale(tmp_path, trades=None, transfers=None, amount="1000"):
    client = FakeWhaleClient(lp_holders(amount), pools=[POOL], price="2", trades={addr(3): trades} if trades else None, transfers={addr(3): transfers} if transfers else None)
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    return next(w for w in result["whales"] if w["rank"] == 3)


async def test_tokens_removed_from_a_pool_are_holding_not_a_new_position(tmp_path):
    w = await scan_whale(tmp_path, transfers=[transfer("in", 1000, "0x1", from_address=POOL)])
    assert w["start_balance"] == 0
    assert w["stance"] == "Holding"
    assert w["stance_reason"] == "mostly removed from a liquidity pool"
    assert w["dominant_flow"] == "locked_lp"
    assert w["net_flow_tokens"] == 0  # Locked/LP is still not part of the net flow


async def test_tokens_unlocked_from_voting_escrow_are_holding_too(tmp_path):
    w = await scan_whale(tmp_path, transfers=[transfer("in", 1000, "0x1", from_address=VE)])
    assert w["stance"] == "Holding" and w["stance_reason"] == "mostly unlocked from Voting Escrow"


async def test_a_real_dex_buyer_from_zero_is_still_a_new_position(tmp_path):
    w = await scan_whale(tmp_path, trades=[buy(1000, 2000, "0xd1")])
    assert w["stance"] == "New position" and w["start_balance"] == 0
    assert w["stance_reason"] == "mostly DEX buying"


async def test_mostly_means_at_least_half_of_the_inflow(tmp_path):
    # 400 back from the pool (40%) + 600 bought on a DEX: mostly a buyer
    w = await scan_whale(tmp_path, trades=[buy(600, 1200, "0xd1")], transfers=[transfer("in", 400, "0x1", from_address=POOL)])
    assert w["stance"] == "New position"
    # 600 back from the pool (60%) + 400 bought: mostly the pool
    w = await scan_whale(tmp_path, trades=[buy(400, 800, "0xd1")], transfers=[transfer("in", 600, "0x1", from_address=POOL)])
    assert w["stance"] == "Holding" and w["stance_reason"] == "mostly removed from a liquidity pool"


async def test_a_wallet_with_a_real_starting_balance_is_unaffected(tmp_path):
    w = await scan_whale(tmp_path, transfers=[transfer("in", 100, "0x1", from_address=addr(8))], amount="1100")  # start 1000, +10%
    assert w["stance"] == "Accumulating"


def test_mostly_locked_inflow_uses_gross_inflow_not_the_net():
    inflow = {b: {"tokens": v, "usd": v} for b, v in {"dex": 0, "exchange": 0, "other_transfers": 0, "locked_lp": 100}.items()}
    assert acc.mostly_locked_inflow({"inflow": inflow}) is True
    inflow["dex"] = {"tokens": 101, "usd": 101}
    assert acc.mostly_locked_inflow({"inflow": inflow}) is False
    zero = {b: {"tokens": 0, "usd": 0} for b in acc.BUCKETS}
    assert acc.mostly_locked_inflow({"inflow": zero}) is False
