#include "pm/v7_external_fair.hpp"
#include "pm/v7_external_state.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

using namespace pm::v7;
using namespace pm::v7::external_fair;

namespace {

ContractHotSpec contract() {
    ContractHotSpec value;
    value.market_handle = 1;
    value.event_handle = 2;
    value.yes_instrument_handle = 3;
    value.no_instrument_handle = 4;
    value.condition_handle = 5;
    value.rules_hash_handle = 6;
    value.oracle_feed_handle = 7;
    value.contract_version = 8;
    value.oracle_window_seconds = 60;
    value.comparator = Comparator::GreaterEqual;
    value.verified_template = 1;
    value.rules_hash_recognized = 1;
    value.reference_semantics_verified = 1;
    value.settlement_source_verified = 1;
    return value;
}

SettlementReferenceSnapshot reference(std::int64_t receive_ns = 100) {
    SettlementReferenceSnapshot value;
    value.market_handle = 1;
    value.contract_version = 8;
    value.reference_version = 9;
    value.exact_decimal_handle = 10;
    value.oracle_feed_handle = 7;
    value.provenance_hash_handle = 11;
    value.source_state_version = 12;
    value.reference_value_numeric = 100.0;
    value.reference_observation_ns = 90;
    value.reference_receive_monotonic_ns = receive_ns;
    value.oracle_window_seconds = 60;
    value.valid = 1;
    return value;
}

OracleSnapshot oracle(std::int64_t receive_ns = 200, std::int64_t age_ns = 10) {
    OracleSnapshot value;
    value.state_version = 13;
    value.feed_handle = 7;
    value.connection_epoch = 1;
    value.exact_decimal_handle = 14;
    value.value_numeric = 100.05;
    value.oracle_observation_ns = 190;
    value.receive_monotonic_ns = receive_ns;
    value.age_ns = age_ns;
    value.window_seconds = 60;
    value.continuity = OracleContinuity::LiveContinuous;
    value.healthy = 1;
    value.valid = 1;
    return value;
}

ExternalAssetSnapshot external(std::int64_t receive_ns = 210) {
    ExternalAssetSnapshot value;
    value.asset_handle = 15;
    value.state_version = 16;
    value.venue_composite_price = 100.20;
    value.venue_composite_log_price = std::log(value.venue_composite_price);
    value.venue_composite_microprice = 100.21;
    value.venue_composite_return_250ms = 0.0005;
    value.venue_composite_return_1s = 0.001;
    value.venue_dispersion_bps = 2.0;
    value.venue_health_mask = 0x7;
    value.venue_count_fresh = 3;
    value.aggregate_ofi = 0.5;
    value.aggregate_trade_imbalance = 0.25;
    value.realized_vol_fast = 0.001;
    value.realized_vol_medium = 0.001;
    value.realized_vol_slow = 0.001;
    value.fast_slow_vol_ratio = 1.0;
    value.jump_score = 0.5;
    value.latest_input_receive_monotonic_ns = receive_ns;
    value.valid = 1;
    return value;
}

FairValueModelSnapshot model(bool mature = true) {
    FairValueModelSnapshot value;
    value.model_version = 17;
    value.model_hash_handle = 18;
    value.policy_version = 19;
    value.feature_schema_version = 20;
    value.bridge_gap_coefficient = 0.8;
    value.sigma_per_sqrt_second_fraction = 0.0002;
    value.bridge_residual_sigma_fraction = 0.0001;
    value.sigma_floor_fraction = 0.00005;
    value.calibration_intercept = 0.0;
    value.calibration_slope = 1.0;
    value.micro_ofi_logit_coefficient = 0.05;
    value.micro_trade_imbalance_logit_coefficient = 0.05;
    value.max_microstructure_logit_adjustment = 0.25;
    value.base_logit_uncertainty = 0.05;
    value.disagreement_logit_per_10bps = 0.01;
    value.jump_logit_penalty = 0.01;
    value.drift_logit_penalty = 0.01;
    value.uncertainty_z = 1.64;
    value.max_external_disagreement_bps = 50.0;
    value.max_model_drift_score = 5.0;
    value.max_oracle_age_ns = 1'000'000'000LL;
    value.max_external_age_ns = 1'000'000'000LL;
    value.min_healthy_venues = 2;
    value.mature = mature ? 1 : 0;
    return value;
}

} // namespace

int main() {
    const auto c = contract();
    assert(contract_authorized(c));

    const std::int64_t now = 220;
    auto fair = compute_fair_value(c, reference(), oracle(), external(), model(),
                                   now, 30'000'000'000LL, 0.0);
    assert(fair.valid == 1);
    assert(fair.fair_yes > 0.5);
    assert(fair.fair_yes_lower <= fair.fair_yes);
    assert(fair.fair_yes <= fair.fair_yes_upper);
    assert(std::abs((1.0 - fair.fair_yes) + fair.fair_yes - 1.0) < 1e-12);

    auto stale_oracle = oracle();
    stale_oracle.age_ns = 2'000'000'000LL;
    auto stale_fair = compute_fair_value(c, reference(), stale_oracle, external(), model(),
                                         now, 30'000'000'000LL, 0.0);
    assert(stale_fair.valid == 0);

    ExternalFairPolicy policy;
    policy.cancel_overlay_enabled = 1;
    policy.external_fair_required = 1;
    policy.maker_cancel_robust_capture_floor = 0.0;
    auto cancel = evaluate_cancel_overlay(c, fair, policy, Side::Sell, true,
                                          c.yes_instrument_handle, 0.50, 50, 99, now, false);
    assert(cancel.action == Action::Cancel);
    assert(cancel.cancel_reason == CancelReason::FairShock);
    assert(cancel.critical == 1);
    // No minimum quote lifetime is an input to the critical overlay, so a fair
    // shock cannot be held live by ordinary maker dwell logic.

    auto invalid_cancel = evaluate_cancel_overlay(c, stale_fair, policy, Side::Buy, true,
                                                  c.yes_instrument_handle, 0.45, 45, 100, now, false);
    assert(invalid_cancel.action == Action::Withdraw);
    assert(invalid_cancel.critical == 1);

    FeeScheduleSnapshot fee;
    fee.market_handle = c.market_handle;
    fee.fee_version = 1;
    fee.rate = 0.25;
    fee.exponent = 2.0;
    fee.taker_only = 1;
    fee.authoritative = 1;
    fee.valid = 1;
    const double expected_fee = 0.25 * std::pow(0.5 * 0.5, 2.0);
    assert(std::abs(fee_per_share(0.5, fee, true) - expected_fee) < 1e-12);

    // Use a very small fee for the executable taker test so the known fair edge
    // remains positive after authoritative fee and execution-risk terms.
    fee.rate = 0.001;
    policy.taker_enabled = 1;
    policy.taker_minimum_robust_ev_per_share = 0.0001;
    policy.max_market_capital_fraction = 0.50;
    policy.fractional_kelly = 0.50;
    policy.max_quantity = 10.0;
    policy.max_book_age_ns = 1'000'000'000LL;
    AggressiveBook book;
    book.valid = 1;
    book.pm_state_version = 50;
    book.receive_monotonic_ns = 215;
    book.ask_count = 3;
    book.asks[0] = BookLevel{0.50, 4.0};
    book.asks[1] = BookLevel{0.55, 4.0};
    book.asks[2] = BookLevel{0.95, 4.0};
    auto take = build_informed_take(c, fair, fee, policy, book, true,
                                    c.yes_instrument_handle, 101, now,
                                    1'000.0, 0.0, 0.01);
    assert(take.action == Action::Take);
    assert(take.admissible == 1);
    assert(take.quantity_microunits > 0);
    assert(take.expected_change_in_wealth > 0.0);
    assert(take.price_tick <= 55);

    auto unknown_fee = fee;
    unknown_fee.authoritative = 0;
    auto no_take = build_informed_take(c, fair, unknown_fee, policy, book, true,
                                       c.yes_instrument_handle, 102, now,
                                       1'000.0, 0.0, 0.01);
    assert(no_take.admissible == 0);

    std::array<CandidateAction, kMaxUnifiedCandidates> candidates{};
    candidates[0] = take;
    candidates[1] = cancel;
    auto chosen = choose_unified_action(candidates, 2, false);
    assert(chosen.action == Action::Cancel);

    auto killed = choose_unified_action(candidates, 2, true);
    assert(killed.action == Action::Withdraw);
    assert(killed.purpose == Purpose::Risk);
    assert(killed.critical == 1);

    CausalStateInputs inputs;
    inputs.pm_state_version = 1;
    inputs.oracle_state_version = 2;
    inputs.external_state_version = 3;
    inputs.settlement_reference_version = 4;
    inputs.inventory_state_version = 5;
    inputs.private_state_version = 6;
    inputs.risk_state_version = 7;
    inputs.pm_receive_ns = 100;
    inputs.oracle_receive_ns = 110;
    inputs.external_receive_ns = 150;
    inputs.settlement_reference_receive_ns = 90;
    inputs.inventory_receive_ns = 120;
    inputs.private_receive_ns = 125;
    inputs.risk_receive_ns = 130;
    const auto cut = make_causal_cut(1, 160, TriggerSource::ExternalPriceUpdate, 3, inputs);
    assert(cut.valid == 1);
    assert(cut.max_input_receive_monotonic_ns == 150);
    assert(causal_cut_valid(cut));

    // Mandatory external-shock scenario: PM state version is unchanged. The
    // external receive alone makes a new causal cut and can generate a critical
    // cancel of the resting YES ask.
    inputs.external_state_version = 4;
    inputs.external_receive_ns = 155;
    const auto external_cut = make_causal_cut(2, 160, TriggerSource::ExternalPriceUpdate, 4, inputs);
    assert(external_cut.valid == 1);
    assert(external_cut.pm_state_version == cut.pm_state_version);
    assert(external_cut.external_state_version > cut.external_state_version);
    auto shock_cancel = evaluate_cancel_overlay(c, fair, policy, Side::Sell, true,
                                                c.yes_instrument_handle, 0.50, 50,
                                                external_cut.causal_cut_id, 160, false);
    assert(shock_cancel.action == Action::Cancel);
    assert(shock_cancel.critical == 1);

    auto intent = to_strategy_intent(take, 500, c.event_handle, 123,
                                     fair.model_version, fair.policy_version, now);
    assert(intent.strategy_id == StrategyId::ExternalInformation);
    assert(intent.urgency == Urgency::Aggressive);
    assert(intent.passive == 0);
    assert(intent.post_only == 0);

    std::cout << "v7 external fair tests passed\n";
    return 0;
}
