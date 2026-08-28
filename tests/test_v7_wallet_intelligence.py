import json
import math
import sys
import unittest
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_function_test_support import approximately, function_test_loader, raises

import v7_wallet_dataset as wd
import v7_wallet_intelligence as w


def mapping(token="yes", market="m1", event="e1", category="crypto", side=1,
            verified_at=900):
    return w.MarketMapping(
        token, market, event, category, side, f"condition-{market}",
        f"question-{market}", "official-oracle", f"rules-{market}", 800,
        verified_at, "registry", "CONDITION_ID_AND_RULES_HASH", True,
    )


def fill(fill_id="f1", wallet="alice", token="yes", price=.4, size=10,
         action="BUY", block_ts=1000, observed_ts=1010, fund=""):
    return w.RawWalletFill(
        fill_id, wallet, f"tx-{fill_id}", 0, 137, token, price, size,
        action, block_ts, observed_ts, "polygon-indexer", f"cursor-{fill_id}",
        True, fund,
    )


def outcome(market="m1", event="e1", value=1, resolved=2000, observed=2010):
    return w.ResolvedOutcome(
        market, event, value, "official-oracle", f"rules-{market}", resolved,
        observed, f"proof-{market}-{value}", True,
    )


def trade(wallet, market, event, edge_outcome, price, trade_ts, outcome_ts,
          *, side=1, category="crypto", fund=""):
    return w.WalletTrade(
        wallet, category, market, event, side, price, edge_outcome, 10, trade_ts,
        fund, trade_ts + 1, outcome_ts, f"fill-{wallet}-{market}",
        f"map-{market}", f"proof-{market}",
    )


def test_reconstruction_is_point_in_time_price_aware_and_deduplicated():
    rows = w.reconstruct_trades(
        [fill(), fill()], [mapping()], [outcome()], as_of_ms=3000,
        require_resolved=True,
    )
    assert len(rows) == 1
    assert rows[0].entry_probability == .4
    assert math.isclose(rows[0].price_aware_edge, .6)
    assert rows[0].mapping_hash and rows[0].outcome_provenance_hash == "proof-m1-1"

    no_rows = w.reconstruct_trades(
        [fill(token="no", price=.3)], [mapping(token="no", side=-1)],
        [outcome()], as_of_ms=3000,
    )
    assert no_rows[0].side == -1
    assert math.isclose(no_rows[0].entry_probability, .7)
    assert math.isclose(no_rows[0].price_aware_edge, -.3)


def test_mapping_must_be_authoritatively_verified_before_fill_observation():
    with raises(w.WalletError, "mapping_not_known_at_decision"):
        w.reconstruct_trades([fill()], [mapping(verified_at=1020)], as_of_ms=3000)
    with raises(w.WalletError, "not_authoritative"):
        bad = replace(mapping(), verification_method="LLM")
        w.reconstruct_trades([fill()], [bad], as_of_ms=3000)
    with raises(w.WalletError, "outcome_mapping_lineage_mismatch"):
        w.reconstruct_trades([fill()], [mapping()],
                             [replace(outcome(), rules_hash="other")], as_of_ms=3000)


def test_causal_tape_is_deterministic_hash_chained_and_fail_closed():
    tape = w.build_causal_tape([fill()], [mapping()], [outcome()], as_of_ms=3000)
    assert [row.sequence for row in tape] == [1, 2, 3, 4]
    assert [row.record_type for row in tape] == [
        "MARKET_MAPPING", "ONCHAIN_FILL", "MAPPED_TRADE", "RESOLVED_OUTCOME",
    ]
    assert tape[0].previous_hash == "0" * 64
    assert all(tape[index].previous_hash == tape[index - 1].record_hash
               for index in range(1, len(tape)))
    assert tape == w.build_causal_tape([fill()], [mapping()], [outcome()], as_of_ms=3000)
    with raises(w.WalletError, "future_outcome_used"):
        w.build_causal_tape([fill()], [mapping()], [outcome(observed=4000)], as_of_ms=3000)


def test_historical_dataset_has_reproducible_provenance_manifest():
    rows, manifest = w.historical_dataset(
        [fill()], [mapping()], [outcome()], as_of_ms=3000, created_at_ms=3100,
    )
    rows2, manifest2 = w.historical_dataset(
        [fill()], [mapping()], [outcome()], as_of_ms=3000, created_at_ms=3100,
    )
    assert rows == rows2 and manifest == manifest2
    assert manifest.row_count == 1 and len(manifest.dataset_hash) == 64
    assert manifest.mapping_hashes == (mapping().mapping_hash,)
    assert manifest.feature_only and not manifest.execution_authority


def test_dataset_cli_writes_dataset_and_manifest(tmp_path):
    inputs = {
        "fills": [asdict(fill())],
        "mappings": [asdict(mapping())],
        "outcomes": [asdict(outcome())],
    }
    paths = {}
    for name, values in inputs.items():
        paths[name] = tmp_path / f"{name}.jsonl"
        paths[name].write_text("\n".join(json.dumps(value) for value in values) + "\n")
    dataset, manifest = tmp_path / "dataset.jsonl", tmp_path / "manifest.json"
    wd.build_files(fills_path=paths["fills"], mappings_path=paths["mappings"],
                   outcomes_path=paths["outcomes"], dataset_path=dataset,
                   manifest_path=manifest, as_of_ms=3000, created_at_ms=3100)
    assert len(dataset.read_text().splitlines()) == 1
    assert json.loads(manifest.read_text())["row_count"] == 1


def test_skill_decay_prior_and_copy_independence():
    day = 86_400_000
    rows = [
        trade("a", "old", "old", 0.0, .8, 100, 200),
        trade("a", "recent", "recent", 1.0, .2, 10 * day, 10 * day + 100),
    ]
    decayed = w.estimate_skill(rows, as_of_ms=11 * day, skill_half_life_days=1,
                               category_prior_std=.5)
    assert decayed.raw_mean_edge > .7
    assert decayed.effective_observations < 1.6

    copied = [
        trade("a", "m1", "e1", 1.0, .2, 1000, 2000, fund="same"),
        trade("b", "m2", "e2", 1.0, .2, 1100, 2100, fund="same"),
        trade("c", "m3", "e3", 0.0, .8, 1200, 2200),
    ]
    prior = w.fit_category_priors(copied, as_of_ms=3000)["crypto"]
    assert prior.independent_clusters == 2


def test_copy_detection_requires_two_distinct_contracts_not_split_fills():
    split = [
        trade("a", "m1", "e1", 1.0, .2, 1000, 2000),
        trade("a", "m1", "e1", 1.0, .2, 1010, 2000),
        trade("b", "m1", "e1", 1.0, .2, 1001, 2000),
    ]
    assert w.copy_clusters(split) == (("a",), ("b",))
    repeated = split + [
        trade("a", "m2", "e2", 1.0, .2, 3000, 4000),
        trade("b", "m2", "e2", 1.0, .2, 3001, 4000),
    ]
    assert w.copy_clusters(repeated) == (("a", "b"),)


def test_incremental_oos_never_uses_future_resolutions():
    rows = [
        trade("a", "m1", "e1", 1.0, .3, 1000, 1500),
        trade("a", "m2", "e2", 1.0, .4, 2000, 2500),
        trade("a", "m3", "e3", 1.0, .5, 3000, 3500),
        trade("a", "m4", "e4", 0.0, .5, 4000, 4500),
    ]
    result = w.incremental_oos(rows, minimum_training_observations=2,
                               minimum_lower_skill=-1)
    assert [row.market_id for row in result] == ["m3", "m4"]
    assert all(row.trained_until_ms < row.decision_ts_ms for row in result)
    assert result[0].training_observations == 2
    assert all(row.feature_only and not row.execution_authority for row in result)


def test_forward_features_are_bounded_feature_only_and_one_per_copy_cluster():
    p = w.SkillPosterior("a", "crypto", 10, .2, .2, .01, .18)
    q = w.SkillPosterior("b", "crypto", 10, .2, .2, .01, .18)
    current = [
        w.WalletTrade("a", "crypto", "m", "e", 1, .5, None, 100, 1000, "fund"),
        w.WalletTrade("b", "crypto", "m", "e", 1, .5, None, 100, 1001, "fund"),
    ]
    features = w.forward_features(
        current, {("a", "crypto"): p, ("b", "crypto"): q}, now_ms=1100,
        max_abs_logit_shift=.1,
    )
    assert features.independent_clusters == 1
    assert features.qualified_wallets == 2
    assert features.fair_value_logit_shift == .1
    assert features.toxicity == .1
    assert approximately(features.market_selection_score, 1 - math.exp(-1))
    assert features.feature_only and not features.execution_authority
    assert not hasattr(features, "action")


load_tests = function_test_loader(globals())

if __name__ == "__main__":
    unittest.main()
