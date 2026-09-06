#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
source_path = ROOT / "src/v7_maker_hft.cpp"
test_path = ROOT / "tests/test_v7_maker_hft.cpp"
source = source_path.read_text(encoding="utf-8")
tests = test_path.read_text(encoding="utf-8")


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


source = once(
    source,
    """    Candidate* best = nullptr;\n    double best_observed_robust_ev = -std::numeric_limits<double>::infinity();\n    double best_observed_uncertainty = 0.0;\n""",
    """    Candidate* best = nullptr;\n    double best_observed_robust_ev = -std::numeric_limits<double>::infinity();\n    double best_observed_uncertainty = 0.0;\n    const SideEconomics* best_observed_side = nullptr;\n    Action best_observed_action = Action::Withdraw;\n""",
    "best-observed declaration",
)
source = once(
    source,
    """                best_observed_robust_ev = side->robust_ev;\n                best_observed_uncertainty = side->uncertainty;\n""",
    """                best_observed_robust_ev = side->robust_ev;\n                best_observed_uncertainty = side->uncertainty;\n                best_observed_side = side;\n                best_observed_action = candidate.action;\n""",
    "best-observed capture",
)
marker = """    const bool economic_quote = best != nullptr && finite(best->score)\n                             && best->score > model.min_robust_ev_per_share;\n"""
publish = """    const auto publish_best_rejected = [&]() noexcept {\n        if (best_observed_side == nullptr) return;\n        decision.placement_action = best_observed_action;\n        const auto& side = *best_observed_side;\n        if (side.side == Side::Buy) {\n            decision.bid_fill_probability = side.fill_probability;\n            decision.bid_statistical_fill_probability = side.statistical_fill_probability;\n            decision.bid_flow_reach_probability = side.flow_reach_probability;\n            decision.bid_queue_depletion_probability = side.queue_depletion_probability;\n            decision.bid_opposite_flow_shares_per_second = side.opposite_flow_shares_per_second;\n            decision.bid_opposite_flow_prints_per_second = side.opposite_flow_prints_per_second;\n            decision.bid_causal_funnel_identified = static_cast<std::uint8_t>(side.causal_funnel_identified);\n            decision.bid_exact_cell_baseline = static_cast<std::uint8_t>(side.exact_cell_baseline);\n        } else if (side.side == Side::Sell) {\n            decision.ask_fill_probability = side.fill_probability;\n            decision.ask_statistical_fill_probability = side.statistical_fill_probability;\n            decision.ask_flow_reach_probability = side.flow_reach_probability;\n            decision.ask_queue_depletion_probability = side.queue_depletion_probability;\n            decision.ask_opposite_flow_shares_per_second = side.opposite_flow_shares_per_second;\n            decision.ask_opposite_flow_prints_per_second = side.opposite_flow_prints_per_second;\n            decision.ask_causal_funnel_identified = static_cast<std::uint8_t>(side.causal_funnel_identified);\n            decision.ask_exact_cell_baseline = static_cast<std::uint8_t>(side.exact_cell_baseline);\n        }\n    };\n\n"""
source = once(source, marker, publish + marker, "best-rejected publisher")
source = once(
    source,
    """    const std::int64_t now = monotonic_ns();\n    const bool lifetime_hold = quotes.last_quote_monotonic_ns > 0\n        && now - quotes.last_quote_monotonic_ns < model.min_quote_lifetime_ns;\n    const std::int64_t exploration_elapsed = last_exploration_quote_ns_ > 0\n        ? std::max<std::int64_t>(0, now - last_exploration_quote_ns_)\n""",
    """    const std::int64_t compute_now_ns = monotonic_ns();\n    const std::int64_t policy_now_ns = update.socket_receive_monotonic_ns > 0\n        ? update.socket_receive_monotonic_ns : compute_now_ns;\n    const bool lifetime_hold = quotes.last_quote_monotonic_ns > 0\n        && policy_now_ns - quotes.last_quote_monotonic_ns < model.min_quote_lifetime_ns;\n    const std::int64_t exploration_elapsed = last_exploration_quote_ns_ > 0\n        ? std::max<std::int64_t>(0, policy_now_ns - last_exploration_quote_ns_)\n""",
    "policy clock",
)
source = once(
    source,
    """        if (finite(best_observed_robust_ev)) {\n            decision.robust_ev = best_observed_robust_ev;\n            decision.ev_uncertainty = best_observed_uncertainty;\n        }\n        // A transient feature update must not turn a freshly accepted quote into\n""",
    """        if (finite(best_observed_robust_ev)) {\n            decision.robust_ev = best_observed_robust_ev;\n            decision.ev_uncertainty = best_observed_uncertainty;\n            publish_best_rejected();\n        }\n        // A transient feature update must not turn a freshly accepted quote into\n""",
    "publish on no-economic quote",
)
start = source.index("    const bool economic_quote = best != nullptr")
end = source.index("\n    const std::int64_t decision_end_ns = monotonic_ns();", start)
section = source[start:end]
section = section.replace(", now));", ", policy_now_ns));")
section = section.replace(", now);", ", policy_now_ns);")
section = section.replace("last_exploration_quote_ns_ = now;", "last_exploration_quote_ns_ = policy_now_ns;")
if re.search(r"\bnow\b", section):
    raise SystemExit("standalone 'now' remains in policy section")
source = source[:start] + section + source[end:]

needle = """    assert(held.reason == pm::v7::maker::DecisionReason::ExplorationHold);\n    assert(held.intent_count == 0);\n    assert(held.exploration_max_rest_ns == model.exploration_max_rest_ns);\n}\n\nvoid test_exploration_minimum_rest_survives_transient_exploit_promotion() {\n"""
replacement = """    assert(held.reason == pm::v7::maker::DecisionReason::ExplorationHold);\n    assert(held.intent_count == 0);\n    assert(held.exploration_max_rest_ns == model.exploration_max_rest_ns);\n\n    // Replay must age policy state on the recorded owner clock, not CPU time.\n    // Advancing receive-monotonic time alone beyond minimum_rest must release\n    // the exploration hold without sleeping the test process.\n    const auto held_receive_ns = update.socket_receive_monotonic_ns;\n    quotes.last_quote_monotonic_ns = held_receive_ns;\n    update.state_version += 1;\n    update.socket_receive_monotonic_ns = held_receive_ns + model.exploration_min_rest_ns + 1;\n    const auto replay_aged = hot.on_market_update(update, inventory, quotes, risk, negative);\n    assert(replay_aged.reason != pm::v7::maker::DecisionReason::ExplorationHold);\n    assert(replay_aged.intent_count == 1);\n    assert(replay_aged.intents[0].type == pm::v7::IntentType::CancelQuote);\n}\n\nvoid test_exploration_minimum_rest_survives_transient_exploit_promotion() {\n"""
tests = once(tests, needle, replacement, "replay policy clock regression")
needle2 = """    assert(decision.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);\n    assert(decision.intent_count == 1);\n    assert(decision.intents[0].type == pm::v7::IntentType::Withdraw);\n}\n\nvoid test_global_kill_preempts_quote() {\n"""
replacement2 = """    assert(decision.reason == pm::v7::maker::DecisionReason::NoEconomicQuote);\n    assert(decision.intent_count == 1);\n    assert(decision.intents[0].type == pm::v7::IntentType::Withdraw);\n    assert(decision.placement_action != pm::v7::maker::Action::Withdraw);\n    assert(decision.bid_statistical_fill_probability > 0.0\n           || decision.ask_statistical_fill_probability > 0.0);\n    assert(decision.bid_fill_probability > 0.0 || decision.ask_fill_probability > 0.0);\n}\n\nvoid test_global_kill_preempts_quote() {\n"""
tests = once(tests, needle2, replacement2, "rejected funnel regression")

source_path.write_text(source, encoding="utf-8")
test_path.write_text(tests, encoding="utf-8")
print("patched maker replay policy clock and rejected diagnostics")
