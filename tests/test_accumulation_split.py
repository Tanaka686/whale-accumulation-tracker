"""Whale Accumulation Tracker: the 3-part net flow (DEX / exchange / other transfers) and the "Incomplete data" rule."""
import json

import pytest

from app import accumulation as acc
from tests.test_accumulation import NOW, POOL, TOKEN, FakeWhaleClient, addr, buy, holder, transfer

EXCH = "0x" + "ba" * 20  # an exchange wallet we excluded, used as a counterparty (not the same as TOKEN)


def bd(dex=0.0, exchange=0.0, other=0.0):
    return {"dex": {"tokens": dex, "usd": dex}, "exchange": {"tokens": exchange, "usd": exchange}, "other_transfers": {"tokens": other, "usd": other}}


# ---------- B: net flow split into DEX / exchange / other transfers ----------


def test_flow_is_split_into_dex_exchange_and_other_transfers():
    trades = acc.trade_events([buy(100, 200, "0x1")], TOKEN)
    transfers = acc.transfer_events(
        [
            transfer("in", 50, "0x2", from_address=EXCH.upper().replace("0X", "0x")),  # withdrawal from the exchange, +50
            transfer("out", 10, "0x5", to_address=EXCH),  # deposit to the exchange, -10
            transfer("out", 30, "0x3", to_address=addr(7)),  # other, -30
            transfer("in", 20, "0x4", from_address=addr(8)),  # other, +20
        ],
        TOKEN,
    )
    events, _ = acc.combine_events(trades, transfers)
    flow = acc.compute_flow(events, price_usd=2.0, exchange_addresses={EXCH})
    b = flow["breakdown"]
    assert b["dex"] == {"tokens": 100, "usd": 200}
    assert b["exchange"] == {"tokens": 40, "usd": 80}
    assert b["other_transfers"] == {"tokens": -10, "usd": -20}
    assert flow["net_tokens"] == 130 and flow["net_usd"] == 260
    assert sum(b[k]["tokens"] for k in b) == flow["net_tokens"]  # the three parts add up to the total


def test_without_exchange_addresses_every_transfer_is_other():
    events = acc.transfer_events([transfer("in", 50, "0x2", from_address=EXCH)], TOKEN)
    flow = acc.compute_flow(events, price_usd=1.0)
    assert flow["breakdown"]["exchange"] == {"tokens": 0, "usd": 0}
    assert flow["breakdown"]["other_transfers"]["tokens"] == 50


def test_only_exchange_wallets_count_as_exchange_not_pools_or_contracts():
    events = acc.transfer_events([transfer("in", 50, "0x2", from_address=POOL)], TOKEN)
    flow = acc.compute_flow(events, price_usd=1.0, exchange_addresses={EXCH})
    assert flow["breakdown"]["other_transfers"]["tokens"] == 50


def test_trades_are_always_dex_even_if_the_router_is_an_exchange_address():
    events = acc.trade_events([buy(10, 20, "0x1")], TOKEN)
    flow = acc.compute_flow(events, price_usd=1.0, exchange_addresses={POOL, EXCH})
    assert flow["breakdown"]["dex"]["tokens"] == 10 and flow["breakdown"]["exchange"]["tokens"] == 0


@pytest.mark.parametrize(
    "breakdown, text, dominant",
    [
        (bd(dex=100), "mostly DEX buying", "dex"),
        (bd(dex=-100), "mostly DEX selling", "dex"),
        (bd(exchange=100), "mostly exchange withdrawal", "exchange"),
        (bd(exchange=-100), "mostly exchange deposit", "exchange"),
        (bd(other=100), "mostly other transfers in", "other_transfers"),
        (bd(other=-100), "mostly other transfers out", "other_transfers"),
        (bd(dex=100, exchange=90, other=90), "mixed: no single source dominates", None),
        (bd(), "no net movement in the window", None),
    ],
)
def test_flow_reason_names_the_biggest_part(breakdown, text, dominant):
    assert acc.flow_reason(breakdown) == (text, dominant)


def test_flow_reason_falls_back_to_tokens_when_there_is_no_usd():
    b = {"dex": {"tokens": 0.0, "usd": 0.0}, "exchange": {"tokens": 500.0, "usd": 0.0}, "other_transfers": {"tokens": 0.0, "usd": 0.0}}
    assert acc.flow_reason(b)[0] == "mostly exchange withdrawal"


def exchange_client():
    holders = [
        holder(2, EXCH, "Cold Wallet", amount="9000"),
        holder(3, addr(3), None, amount="1000"),
        holder(4, addr(4), None, amount="1000"),
    ]
    return FakeWhaleClient(
        holders,
        price="2",
        trades={addr(4): [buy(100, 200, "0xd1")]},
        transfers={
            addr(3): [transfer("in", 500, "0xe1", from_address=EXCH)],
            addr(4): [transfer("out", 40, "0xe2", to_address=EXCH)],
        },
    )


async def test_scan_shows_the_three_parts_and_keeps_the_stance_on_the_total(tmp_path):
    result = await acc.scan(exchange_client(), "base", TOKEN, now=NOW, save_dir=tmp_path)
    by_rank = {w["rank"]: w for w in result["whales"]}
    assert [e["reason_code"] for e in result["excluded"]] == ["exchange_label"]

    w3 = by_rank[3]  # 500 tokens withdrawn from the excluded exchange wallet
    assert w3["breakdown"]["exchange"] == {"tokens": 500, "usd": 1000}
    assert w3["breakdown"]["dex"] == {"tokens": 0, "usd": 0}
    assert w3["stance"] == "Accumulating"  # the stance still follows the total (+500 on a start of 500)
    assert w3["stance_reason"] == "mostly exchange withdrawal"
    assert w3["dominant_flow"] == "exchange"

    w4 = by_rank[4]  # bought 100 on a DEX, sent 40 to the exchange
    assert w4["breakdown"]["dex"] == {"tokens": 100, "usd": 200}
    assert w4["breakdown"]["exchange"] == {"tokens": -40, "usd": -80}
    assert w4["net_flow_tokens"] == 60 and w4["stance"] == "Accumulating"
    assert w4["stance_reason"] == "mostly DEX buying"

    totals = result["summary"]["totals"]
    assert totals["dex"]["usd"] == 200
    assert totals["exchange"]["usd"] == 1000 - 80
    assert totals["other_transfers"]["usd"] == 0
    assert result["summary"]["total_net_flow_usd"] == 200 + 920


async def test_the_split_is_saved_in_the_json_file(tmp_path):
    result = await acc.scan(exchange_client(), "base", TOKEN, now=NOW, save_dir=tmp_path)
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert saved["whales"][0]["breakdown"] == result["whales"][0]["breakdown"]
    assert saved["summary"]["totals"] == result["summary"]["totals"]


# ---------- C: a wallet that hit the page limit ----------


async def test_a_truncated_wallet_is_incomplete_and_kept_out_of_the_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(acc.config, "WHALE_MAX_PAGES", 1)  # cap = 300 rows per call
    holders = [holder(3, addr(3), None, amount="1000"), holder(4, addr(4), None, amount="1000")]
    client = FakeWhaleClient(
        holders,
        price="2",
        transfers={addr(3): [transfer("in", 1, f"0x{i:04x}") for i in range(300)]},  # a full page: more may exist
        trades={addr(4): [buy(100, 200, "0xd1")]},
    )
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    by_rank = {w["rank"]: w for w in result["whales"]}

    bad = by_rank[3]
    assert bad["stance"] == "Incomplete data"
    assert bad["truncated"] is True and bad["start_balance"] is None and bad["net_flow_pct_of_start"] is None
    assert bad["stance_reason"] == "very active wallet (possible bot or market maker)" and bad["warnings"]

    s = result["summary"]
    assert s["incomplete"] == 1 and s["errors"] == 0
    assert (s["new_position"], s["accumulating"], s["distributing"], s["holding"]) == (0, 1, 0, 0)  # only rank 4 is counted
    assert s["total_net_flow_tokens"] == 100 and s["total_net_flow_usd"] == 200  # rank 3's 300 tokens are not in it
    assert s["totals"]["other_transfers"]["usd"] == 0

    listed = [w for w in result["warnings"] if w.startswith("Incomplete data")]
    assert len(listed) == 1 and "#3" in listed[0] and bad["short"] in listed[0]


async def test_a_full_page_of_trades_also_makes_a_wallet_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(acc.config, "WHALE_MAX_PAGES", 1)
    client = FakeWhaleClient([holder(3, addr(3), None)], trades={addr(3): [buy(1, 2, f"0x{i:04x}") for i in range(300)]})
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    assert result["whales"][0]["stance"] == "Incomplete data"


async def test_one_row_under_the_page_limit_is_still_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(acc.config, "WHALE_MAX_PAGES", 1)
    client = FakeWhaleClient([holder(3, addr(3), None, amount="1000")], transfers={addr(3): [transfer("in", 1, f"0x{i:04x}") for i in range(299)]})
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    w = result["whales"][0]
    assert w["truncated"] is False and w["stance"] == "Accumulating"  # 299 on a start of 701 is +42.6%
    assert result["summary"]["incomplete"] == 0
