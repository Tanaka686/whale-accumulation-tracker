"""Whale Accumulation Tracker: the Locked/LP part of the flow. Locking into Voting Escrow or adding to an LP
is not distributing, and unlocking is not accumulating."""
import pytest

from app import accumulation as acc
from tests.test_accumulation import NOW, POOL, TOKEN, FakeWhaleClient, addr, buy, holder, transfer

VE = addr(0x51)  # a "Voting Escrow" contract wallet
VAULT = addr(0x52)
EXCH = "0x" + "ba" * 20


def bd(dex=0.0, exchange=0.0, other=0.0, locked=0.0):
    return {b: {"tokens": v, "usd": v} for b, v in {"dex": dex, "exchange": exchange, "other_transfers": other, "locked_lp": locked}.items()}


# ---------- the numbers ----------


def test_transfers_to_and_from_excluded_contracts_are_locked_lp_not_other():
    events = acc.transfer_events(
        [
            transfer("out", 300, "0x1", to_address=VE),  # locked
            transfer("in", 100, "0x2", from_address=POOL.upper().replace("0X", "0x")),  # removed from the pool
            transfer("out", 20, "0x3", to_address=addr(7)),  # a normal transfer
            transfer("in", 50, "0x4", from_address=EXCH),  # exchange withdrawal
        ],
        TOKEN,
    )
    flow = acc.compute_flow(events, price_usd=2.0, exchange_addresses={EXCH}, locked_addresses={VE, POOL})
    b = flow["breakdown"]
    assert b["locked_lp"] == {"tokens": -200, "usd": -400}
    assert b["other_transfers"] == {"tokens": -20, "usd": -40}
    assert b["exchange"] == {"tokens": 50, "usd": 100}
    assert b["dex"] == {"tokens": 0, "usd": 0}


def test_locked_lp_is_not_part_of_the_net_flow_but_is_part_of_all_movement():
    events = acc.transfer_events([transfer("out", 300, "0x1", to_address=VE), transfer("in", 40, "0x2", from_address=addr(8))], TOKEN)
    flow = acc.compute_flow(events, price_usd=1.0, locked_addresses={VE})
    assert flow["net_tokens"] == 40 and flow["net_usd"] == 40  # the lock is not in it
    assert flow["all_tokens"] == 40 - 300 and flow["all_usd"] == 40 - 300


def test_locked_targets_are_listed_biggest_first():
    events = acc.transfer_events([transfer("out", 30, "0x1", to_address=VAULT), transfer("out", 300, "0x2", to_address=VE), transfer("out", 100, "0x3", to_address=VE)], TOKEN)
    flow = acc.compute_flow(events, price_usd=1.0, locked_addresses={VE, VAULT})
    assert [(t["address"], t["tokens"]) for t in flow["locked_targets"]] == [(VE, -400), (VAULT, -30)]


def test_a_trade_is_always_dex_even_when_the_pool_is_a_locked_address():
    events = acc.trade_events([buy(10, 20, "0x1")], TOKEN)
    flow = acc.compute_flow(events, price_usd=1.0, locked_addresses={POOL})
    assert flow["breakdown"]["dex"]["tokens"] == 10 and flow["breakdown"]["locked_lp"]["tokens"] == 0


def test_the_gauge_keyword_excludes_a_holder_as_a_contract():
    v = acc.classify_holder(holder(1, VE, "Aerodrome Gauge"), TOKEN, set())
    assert v["kind"] == "excluded" and v["reason_code"] == "contract_label"


# ---------- the stance ignores locked/LP ----------


def test_locking_tokens_is_not_distributing():
    # started with 1000, locked 300 in Voting Escrow: 700 left in the wallet
    s = acc.stance_for(current_balance=700, net_tokens=0, locked_tokens=-300)
    assert s["stance"] == "Holding"
    assert s["start_balance"] == 1000
    assert s["net_pct_of_start"] == 0


def test_the_old_way_would_have_called_a_lock_distributing():
    # the same wallet if the lock were counted as a sell: -30% of the start
    assert acc.stance_for(current_balance=700, net_tokens=-300)["stance"] == "Distributing"


def test_unlocking_tokens_is_not_accumulating():
    s = acc.stance_for(current_balance=1300, net_tokens=0, locked_tokens=300)
    assert s["stance"] == "Holding" and s["start_balance"] == 1000


def test_a_real_sale_next_to_a_lock_is_still_distributing():
    # started with 1000, sold 50 on a DEX, locked 300: 650 left
    s = acc.stance_for(current_balance=650, net_tokens=-50, locked_tokens=-300)
    assert s["stance"] == "Distributing"
    assert s["start_balance"] == 1000
    assert s["net_pct_of_start"] == pytest.approx(-5.0)


def test_a_real_buy_next_to_a_lock_is_still_accumulating():
    s = acc.stance_for(current_balance=820, net_tokens=120, locked_tokens=-300)  # start 1000, +12%
    assert s["stance"] == "Accumulating" and s["net_pct_of_start"] == pytest.approx(12.0)


def test_stance_for_without_locked_tokens_is_unchanged():
    assert acc.stance_for(1021, 21)["stance"] == "Accumulating"
    assert acc.stance_for(1000, 1000)["stance"] == "New position"


# ---------- the reasons ----------


@pytest.mark.parametrize(
    "breakdown, target, text",
    [
        (bd(locked=-300), {"label": "Voting Escrow", "reason_code": "contract_label"}, "mostly locked into Voting Escrow"),
        (bd(locked=300), {"label": "Voting Escrow", "reason_code": "contract_label"}, "mostly unlocked from Voting Escrow"),
        (bd(locked=-300), {"label": "Volatile AMM - USDC/AERO", "reason_code": "liquidity_pool"}, "mostly added to a liquidity pool"),
        (bd(locked=300), {"label": None, "reason_code": "liquidity_pool"}, "mostly removed from a liquidity pool"),
        (bd(locked=-300), {"label": "Yield Vault", "reason_code": "contract_label"}, 'mostly sent to "Yield Vault"'),
        (bd(locked=300), {"label": "Yield Vault", "reason_code": "contract_label"}, 'mostly received from "Yield Vault"'),
        (bd(locked=-300), None, "mostly sent to a contract"),
    ],
)
def test_locked_reasons_name_where_the_tokens_went(breakdown, target, text):
    assert acc.flow_reason(breakdown, target) == (text, "locked_lp")


def test_the_biggest_part_wins_across_all_four():
    assert acc.flow_reason(bd(dex=10, other=-20, locked=-300), {"label": "Voting Escrow"})[0] == "mostly locked into Voting Escrow"
    assert acc.flow_reason(bd(dex=300, locked=-20))[0] == "mostly DEX buying"
    assert acc.flow_reason(bd(dex=100, exchange=90, other=90, locked=90))[0] == "mixed: no single source dominates"
    assert acc.flow_reason(bd())[0] == "no net movement in the window"


# ---------- the whole scan ----------


def locked_client():
    holders = [
        holder(1, VE, "Voting Escrow", amount="9000"),
        holder(2, POOL, None, amount="8000"),
        holder(3, EXCH, "Cold Wallet", amount="7000"),
        holder(4, addr(4), None, amount="700"),  # locked 300 of its 1000 in Voting Escrow
        holder(5, addr(5), None, amount="650"),  # sold 50 on a DEX and added 200 to the pool -> start 900
        holder(6, addr(6), None, amount="1300"),  # unlocked 300 from Voting Escrow
    ]
    return FakeWhaleClient(
        holders,
        pools=[POOL],
        price="2",
        trades={addr(5): [buy(50, 100, "0xd1", token_on="from")]},
        transfers={
            addr(4): [transfer("out", 300, "0xa1", to_address=VE)],
            addr(5): [transfer("out", 200, "0xb1", to_address=POOL)],
            addr(6): [transfer("in", 300, "0xc1", from_address=VE)],
        },
    )


async def test_scan_keeps_locked_lp_out_of_the_stance_and_the_totals(tmp_path):
    result = await acc.scan(locked_client(), "base", TOKEN, now=NOW, save_dir=tmp_path)
    assert [(e["rank"], e["reason_code"]) for e in result["excluded"]] == [(1, "contract_label"), (2, "liquidity_pool"), (3, "exchange_label")]
    by_rank = {w["rank"]: w for w in result["whales"]}

    w4 = by_rank[4]  # locked in Voting Escrow
    assert w4["breakdown"]["locked_lp"] == {"tokens": -300, "usd": -600}
    assert w4["net_flow_tokens"] == 0 and w4["stance"] == "Holding" and w4["start_balance"] == 1000
    assert w4["stance_reason"] == "mostly locked into Voting Escrow"
    assert w4["locked_lp_targets"][0]["label"] == "Voting Escrow" and w4["all_movement_tokens"] == -300

    w5 = by_rank[5]  # a real sale plus an LP deposit
    assert w5["breakdown"]["dex"] == {"tokens": -50, "usd": -100}
    assert w5["breakdown"]["locked_lp"]["tokens"] == -200
    assert w5["stance"] == "Distributing" and w5["start_balance"] == 900
    assert w5["stance_reason"] == "mostly added to a liquidity pool"  # the biggest part of the movement, but the stance is the sale

    w6 = by_rank[6]  # unlocked
    assert w6["breakdown"]["locked_lp"] == {"tokens": 300, "usd": 600}
    assert w6["stance"] == "Holding" and w6["stance_reason"] == "mostly unlocked from Voting Escrow"

    s = result["summary"]
    assert (s["holding"], s["distributing"], s["accumulating"], s["new_position"]) == (2, 1, 0, 0)
    assert s["total_net_flow_tokens"] == -50 and s["total_net_flow_usd"] == -100  # locked/LP is not in the total
    assert s["totals"]["locked_lp"] == {"tokens": -300 - 200 + 300, "usd": (-300 - 200 + 300) * 2}
    assert s["totals"]["dex"]["usd"] == -100


async def test_the_locked_part_and_its_targets_are_saved_in_the_json(tmp_path):
    import json

    await acc.scan(locked_client(), "base", TOKEN, now=NOW, save_dir=tmp_path)
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())
    w4 = next(w for w in saved["whales"] if w["rank"] == 4)
    assert w4["breakdown"]["locked_lp"]["tokens"] == -300
    assert w4["locked_lp_targets"][0]["reason_code"] == "contract_label"
    assert saved["summary"]["totals"]["locked_lp"]["tokens"] == -200


async def test_an_exchange_wallet_is_not_treated_as_a_locked_contract(tmp_path):
    client = FakeWhaleClient(
        [holder(3, EXCH, "Cold Wallet", amount="7000"), holder(4, addr(4), None, amount="1000")],
        transfers={addr(4): [transfer("in", 500, "0xa1", from_address=EXCH)]},
    )
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    w = result["whales"][0]
    assert w["breakdown"]["exchange"]["tokens"] == 500 and w["breakdown"]["locked_lp"]["tokens"] == 0
    assert w["stance"] == "Accumulating"
