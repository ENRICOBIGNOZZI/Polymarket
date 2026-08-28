#include "pm/v7_external_execution.hpp"

#include <cassert>
#include <iostream>

using namespace pm::v7;
using namespace pm::v7::external_fair;

namespace {

ContractHotSpec contract() {
    ContractHotSpec c;
    c.market_handle = 1;
    c.event_handle = 2;
    c.yes_instrument_handle = 3;
    c.no_instrument_handle = 4;
    c.condition_handle = 5;
    c.rules_hash_handle = 6;
    c.oracle_feed_handle = 7;
    c.contract_version = 8;
    c.oracle_window_seconds = 60;
    c.comparator = Comparator::GreaterEqual;
    c.verified_template = 1;
    c.rules_hash_recognized = 1;
    c.reference_semantics_verified = 1;
    c.settlement_source_verified = 1;
    return c;
}

FairValueSnapshot fair(std::int64_t now = 1'000) {
    FairValueSnapshot f;
    f.market_handle = 1;
    f.contract_version = 8;
    f.reference_version = 9;
    f.model_version = 10;
    f.model_hash_handle = 11;
    f.policy_version = 12;
    f.feature_schema_version = 13;
    f.fair_yes = 0.70;
    f.fair_yes_lower = 0.65;
    f.fair_yes_upper = 0.75;
    f.structural_probability = 0.69;
    f.calibrated_probability = 0.70;
    f.oracle_healthy = 1;
    f.external_healthy = 1;
    f.contract_verified = 1;
    f.reference_verified = 1;
    f.model_mature = 1;
    f.calculated_monotonic_ns = now;
    f.valid_until_monotonic_ns = now + 1'000;
    f.valid = 1;
    return f;
}

CandidateAction take() {
    CandidateAction a;
    a.action = Action::Take;
    a.purpose = Purpose::Alpha;
    a.side = Side::Buy;
    a.market_handle = 1;
    a.instrument_handle = 3;
    a.causal_cut_id = 100;
    a.price_tick = 55;
    a.quantity_microunits = 4'000'000;
    a.average_price = 0.525;
    a.expected_change_in_wealth = 0.40;
    a.robust_ev_per_share = 0.10;
    a.risk_cost = 0.01;
    a.execution_uncertainty = 0.02;
    a.admissible = 1;
    return a;
}

} // namespace

int main() {
    auto a = take();
    auto plan = build_execution_plan(a, 500, 2, 20, 10, 12, 1'000, 100);
    assert(plan.policy == ExecutionPolicyId::AggressiveTaker);
    assert(plan.intent.type == IntentType::TargetPosition);
    assert(plan.intent.purpose == IntentPurpose::Alpha);
    assert(plan.intent.passive == 0);
    assert(plan.intent.post_only == 0);

    CandidateAction risk_cancel;
    risk_cancel.action = Action::Cancel;
    risk_cancel.purpose = Purpose::Risk;
    risk_cancel.side = Side::Sell;
    risk_cancel.market_handle = 1;
    risk_cancel.instrument_handle = 3;
    risk_cancel.price_tick = 50;
    risk_cancel.admissible = 1;
    risk_cancel.critical = 1;
    auto cancel_plan = build_execution_plan(risk_cancel, 501, 2, 21, 10, 12, 1'001, 100);
    assert(cancel_plan.policy == ExecutionPolicyId::Emergency);
    assert(cancel_plan.intent.purpose == IntentPurpose::Risk);
    assert(cancel_plan.intent.urgency == Urgency::Critical);

    OwnOrderView own;
    own.instrument_handle = 3;
    own.private_state_version = 30;
    own.ask_active = 1;
    own.ask_tick = 54;
    auto guarded = guard_self_cross(a, own, 101);
    assert(guarded.take_allowed == 0);
    assert(guarded.requires_cancel_confirmation == 1);
    assert(guarded.required_private_state_version == 31);
    assert(guarded.immediate.action == Action::Cancel);
    assert(guarded.immediate.purpose == Purpose::Risk);
    assert(guarded.deferred_take.action == Action::Take);

    own.ask_cancel_pending = 1;
    auto pending = guard_self_cross(a, own, 102);
    assert(pending.take_allowed == 0);
    assert(pending.requires_cancel_confirmation == 1);
    assert(pending.immediate.action == Action::Nothing);

    own.ask_active = 0;
    own.ask_cancel_pending = 0;
    own.private_state_version = 31;
    auto safe = guard_self_cross(a, own, 103);
    assert(safe.take_allowed == 1);
    assert(safe.immediate.action == Action::Take);

    FeeScheduleSnapshot fee;
    fee.market_handle = 1;
    fee.fee_version = 1;
    fee.rate = 0.001;
    fee.exponent = 2.0;
    fee.taker_only = 1;
    fee.authoritative = 1;
    fee.valid = 1;

    AggressiveBook arrival;
    arrival.pm_state_version = 40;
    arrival.receive_monotonic_ns = 1'005;
    arrival.valid = 1;
    arrival.ask_count = 3;
    arrival.asks[0] = BookLevel{0.50, 1.0};
    arrival.asks[1] = BookLevel{0.52, 2.0};
    arrival.asks[2] = BookLevel{0.55, 3.0};

    auto fak = simulate_taker_paper(plan, arrival, fee, fair(1'000), true,
                                    AggressiveTimeInForce::Fak, 1'010);
    assert(fak.rejected == 0);
    assert(fak.filled_microunits == 4'000'000);
    assert(fak.complete == 1);
    assert(fak.authoritative_fee > 0.0);
    assert(fak.robust_net_ev > 0.0);

    auto large = a;
    large.quantity_microunits = 10'000'000;
    auto large_plan = build_execution_plan(large, 502, 2, 22, 10, 12, 1'000, 100);
    auto fok = simulate_taker_paper(large_plan, arrival, fee, fair(1'000), true,
                                    AggressiveTimeInForce::Fok, 1'010);
    assert(fok.filled_microunits == 0);
    assert(fok.complete == 0);

    auto partial = simulate_taker_paper(large_plan, arrival, fee, fair(1'000), true,
                                        AggressiveTimeInForce::Fak, 1'010);
    assert(partial.filled_microunits == 6'000'000);
    assert(partial.complete == 0);

    ExternalMakerPolicy maker;
    maker.enabled = 1;
    maker.inventory_skew_ticks = 1.0;
    maker.minimum_robust_capture_per_share = 0.001;
    maker.quote_quantity = 1.0;
    auto bid = build_external_make(contract(), fair(1'000), maker, Side::Buy, true,
                                   3, 200, 50, 80, 0.01, 0.0, 1'010);
    assert(bid.admissible == 1);
    assert(bid.action == Action::Make);
    assert(bid.price_tick < 80);
    assert(bid.robust_ev_per_share > maker.minimum_robust_capture_per_share);

    // External fair remains directional. Changing the PM mid while preserving
    // the opposite touch constraint cannot move reservation toward PM mid.
    auto bid_wider_book = build_external_make(contract(), fair(1'000), maker, Side::Buy, true,
                                              3, 201, 20, 95, 0.01, 0.0, 1'010);
    assert(bid_wider_book.admissible == 1);
    assert(bid_wider_book.price_tick == bid.price_tick);

    std::cout << "v7 external execution tests passed\n";
    return 0;
}
