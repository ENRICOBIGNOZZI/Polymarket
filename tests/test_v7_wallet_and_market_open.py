import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_market_open as m
import v7_wallet_intelligence as w


def test_wallet_skill_is_price_aware_shrunk_and_feature_only():
    rows = [w.WalletTrade("w", "crypto", f"m{i}", f"e{i}", 1, p, 1.0, 10, 1000+i)
            for i, p in enumerate((.3, .4, .6, .9))]
    posterior = w.estimate_skill(rows)
    assert math.isclose(posterior.raw_mean_edge, .45)
    assert 0 < posterior.posterior_mean_edge < posterior.raw_mean_edge
    signal = w.aggregate_flow([rows[-1]], {("w", "crypto"): posterior}, now_ms=2000,
                              minimum_lower_skill=-1)
    assert signal.feature_only and not signal.execution_authority


def test_wallet_copy_cluster_counts_coordinated_wallets_once():
    rows = [
        w.WalletTrade("a", "c", "m1", "e1", 1, .5, 1, 1, 1000, "fund"),
        w.WalletTrade("b", "c", "m1", "e1", 1, .5, 1, 1, 1001, "fund"),
    ]
    assert w.copy_clusters(rows) == (("a", "b"),)


def test_market_open_uses_verified_fair_hierarchy_and_small_size():
    contract = m.ColdStartContract("m", "e", "hash", "official", ">=", 5000, "UTC", True)
    estimates = [
        m.FairEstimate(m.FairSource.BASE_RATE, .55, .10, "base", 1000, True),
        m.FairEstimate(m.FairSource.DETERMINISTIC_RELATION, .80, .02, "rel", 1000, True),
    ]
    result = m.decide_open(contract, estimates, decision_ts_ms=2000, open_ts_ms=1900,
                           pm_bid=.45, pm_ask=.50, executable_cost=.01, minimum_edge=.01)
    assert result.fair == .80 and result.action == "TAKE" and 0 < result.size_multiplier < .25


def test_market_open_unknown_semantics_abstains():
    contract = m.ColdStartContract("m", "e", "", "", ">=", 0, "UTC", False)
    result = m.decide_open(contract, [], decision_ts_ms=2000, open_ts_ms=1900,
                           pm_bid=.45, pm_ask=.50, executable_cost=.01, minimum_edge=.01)
    assert result.action == "NOTHING" and result.size_multiplier == 0
