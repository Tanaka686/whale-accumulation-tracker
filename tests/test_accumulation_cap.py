"""The safety cap on credits for a whole whale scan: past it, no more wallets are started."""
import json

from app import accumulation as acc
from tests.test_accumulation import NOW, TOKEN, FakeWhaleClient, addr, buy, holder


def five_whales():
    holders = [holder(r, addr(r), None, amount="1000") for r in range(1, 6)]
    return FakeWhaleClient(holders, trades={addr(r): [buy(10, 20, f"0xd{r}")] for r in range(1, 6)})


def test_the_default_cap_is_1500_credits():
    assert acc.config.WHALE_MAX_CREDITS_PER_SCAN == 1500
    assert acc.config.WHALE_CONCURRENCY >= 1


async def test_a_scan_under_the_cap_scans_every_wallet(tmp_path):
    client = five_whales()
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path)
    assert result["summary"]["not_scanned"] == 0 and result["summary"]["whales"] == 5
    assert not [w for w in result["warnings"] if w.startswith("Credit cap")]
    assert all(w["stance"] != "Not scanned" for w in result["whales"])


async def test_a_low_cap_stops_starting_wallets_and_says_so(tmp_path):
    client = five_whales()
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path, credit_cap=25)
    by_rank = {w["rank"]: w for w in result["whales"]}
    scanned = [r for r, w in by_rank.items() if w["stance"] != "Not scanned"]
    assert scanned == [1]  # wallets start in rank order, so the biggest holder gets the credits
    for r in (2, 3, 4, 5):
        w = by_rank[r]
        assert w["stance"] == "Not scanned" and "credit cap" in w["stance_reason"] and w["breakdown"] is None
    assert result["credits"] <= 25  # the cap is a real limit, not a hint
    assert result["summary"]["not_scanned"] == 4 and result["summary"]["errors"] == 0
    assert [w for w in result["warnings"] if w.startswith("Credit cap of 25 reached: 4 smaller wallets (#2 onward)")]


async def test_wallets_not_scanned_stay_out_of_the_stance_counts_and_totals(tmp_path):
    client = five_whales()
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path, credit_cap=25)
    s = result["summary"]
    assert s["new_position"] + s["accumulating"] + s["distributing"] + s["holding"] == 1
    only = next(w for w in result["whales"] if w["rank"] == 1)
    assert s["total_net_flow_tokens"] == only["net_flow_tokens"] and s["total_net_flow_usd"] == only["net_flow_usd"]


async def test_a_cap_in_between_scans_some_wallets_and_never_goes_over(tmp_path):
    client = five_whales()
    result = await acc.scan(client, "base", TOKEN, now=NOW, save_dir=tmp_path, credit_cap=70)
    n = result["summary"]["not_scanned"]
    assert 0 < n < 5
    assert result["credits"] <= 70


async def test_the_saved_json_lists_the_wallets_not_scanned(tmp_path):
    await acc.scan(five_whales(), "base", TOKEN, now=NOW, save_dir=tmp_path, credit_cap=25)
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert saved["summary"]["not_scanned"] == 4
    assert [w["stance"] for w in saved["whales"]].count("Not scanned") == 4
