#include "pm/v7_external_kernel.hpp"

#include <cassert>
#include <iostream>

using namespace pm::v7;
using namespace pm::v7::external_fair;

namespace {

ExternalKernelInput input() {
    ExternalKernelInput in;
    in.contract.market_handle = 1;
    in.contract.event_handle = 2;
    in.contract.yes_instrument_handle = 3;
    in.contract.no_instrument_handle = 4;
    in.contract.condition_handle = 5;
    in.contract.rules_hash_handle = 6;
    in.contract.oracle_feed_handle = 7;
    in.contract.contract_version = 8;
    in.contract.oracle_window_seconds = 60;
    in.contract.comparator = Comparator::GreaterEqual;
    in.contract.verified_template = 1;
    in.contract.rules_hash_recognized = 1;
    in.contract.reference_semantics_verified = 1;
    in.contract.settlement_source_verified = 1;

    in.reference.market_handle = 1;
    in.reference.contract_version = 8;
    in.reference.reference_version = 9;
    in.reference.reference_value_numeric = 100.0;
    in.reference.reference_observation_ns = 80;
    in.reference.reference_receive_monotonic_ns = 90;
    in.reference.oracle_feed_handle = 7;
    in.reference.oracle_window_seconds = 60;
    in.reference.source_state_version = 1;
    in.reference.valid = 1;

    in.oracle.state_version = 2;
    in.oracle.feed_handle = 7;
    in.oracle.connection_epoch = 1;
    in.oracle.value_numeric = 100.05;
    in.oracle.oracle_observation_ns = 100;
    in.oracle.receive_monotonic_ns = 110;
    in.oracle.age_ns = 5;
    in.oracle.window_seconds = 60;
    in.oracle.continuity = OracleContinuity::LiveContinuous;
    in.oracle.healthy = 1;
    in.oracle.valid = 1;

    in.external.asset_handle = 10;
    in.external.state_version = 3;
    in.external.venue_composite_price = 100.25;
    in.external.venue_composite_log_price = 4.607667;
    in.external.venue_composite_microprice = 100.26;
    in.external.venue_count_fresh = 3;
    in.external.venue_health_mask = 7;
    in.external.venue_dispersion_bps = 2.0;
    in.external.aggregate_ofi = 0.4;
    in.external.aggregate_trade_imbalance = 0.2;
    in.external.realized_vol_fast = 0.001;
    in.external.realized_vol_medium = 0.001;
    in.external.realized_vol_slow = 0.001;
    in.external.fast_slow_vol_ratio = 1.0;
    in.external.latest_input_receive_monotonic_ns = 120;
    in.external.valid = 1;

    in.model.model_version = 11;
    in.model.model_hash_handle = 12;
    in.model.policy_version = 13;
    in.model.feature_schema_version = 14;
    in.model.bridge_gap_coefficient = 0.8;
    in.model.sigma_per_sqrt_second_fraction = 0.0002;
    in.model.bridge_residual_sigma_fraction = 0.0001;
    in.model.sigma_floor_fraction = 0.00005;
    in.model.calibration_slope = 1.0;
    in.model.max_microstructure_logit_adjustment = 0.25;
    in.model.base_logit_uncertainty = 0.05;
    in.model.disagreement_logit_per_10bps = 0.01;
    in.model.jump_logit_penalty = 0.01;
    in.model.drift_logit_penalty = 0.01;
    in.model.uncertainty_z = 1.64;
    in.model.max_external_disagreement_bps = 50.0;
    in.model.max_model_drift_score = 5.0;
    in.model.max_oracle_age_ns = 1'000'000'000LL;
    in.model.max_external_age_ns = 1'000'000'000LL;
    in.model.min_healthy_venues = 2;
    in.model.mature = 1;

    in.policy.cancel_overlay_enabled = 1;
    in.policy.external_fair_required = 1;
    in.policy.taker_enabled = 1;
    in.policy.maker_cancel_robust_capture_floor = 0.0;
    in.policy.taker_minimum_robust_ev_per_share = 0.0001;
    in.policy.max_depth_fraction = 0.5;
    in.policy.depth_survival_fraction = 1.0;
    in.policy.fractional_kelly = 0.1;
    in.policy.max_market_capital_fraction = 0.1;
    in.policy.max_quantity = 10.0;
    in.policy.max_book_age_ns = 1'000'000'000LL;

    in.fee.market_handle = 1;
    in.fee.fee_version = 1;
    in.fee.rate = 0.001;
    in.fee.exponent = 2.0;
    in.fee.authoritative = 1;
    in.fee.valid = 1;
    in.fee.taker_only = 1;

    in.yes_book.pm_state_version = 100;
    in.yes_book.receive_monotonic_ns = 119;
    in.yes_book.ask_count = 2;
    in.yes_book.asks[0] = BookLevel{0.50, 2.0};
    in.yes_book.asks[1] = BookLevel{0.55, 2.0};
    in.yes_book.valid = 1;
    in.no_book = in.yes_book;
    in.no_book.asks[0] = BookLevel{0.45, 2.0};
    in.no_book.asks[1] = BookLevel{0.50, 2.0};

    in.resting_quotes[0].order_handle = 1000;
    in.resting_quotes[0].instrument_handle = 3;
    in.resting_quotes[0].price_tick = 50;
    in.resting_quotes[0].price = 0.50;
    in.resting_quotes[0].side = Side::Sell;
    in.resting_quotes[0].instrument_is_yes = 1;
    in.resting_quotes[0].active = 1;

    in.causal_inputs.pm_state_version = 100;
    in.causal_inputs.oracle_state_version = 2;
    in.causal_inputs.external_state_version = 3;
    in.causal_inputs.settlement_reference_version = 9;
    in.causal_inputs.inventory_state_version = 4;
    in.causal_inputs.private_state_version = 5;
    in.causal_inputs.risk_state_version = 6;
    in.causal_inputs.pm_receive_ns = 115;
    in.causal_inputs.oracle_receive_ns = 110;
    in.causal_inputs.external_receive_ns = 120;
    in.causal_inputs.settlement_reference_receive_ns = 90;
    in.causal_inputs.inventory_receive_ns = 100;
    in.causal_inputs.private_receive_ns = 116;
    in.causal_inputs.risk_receive_ns = 117;

    in.causal_cut_id = 50;
    in.trigger_source_version = 3;
    in.decision_monotonic_ns = 125;
    in.time_to_expiry_ns = 30'000'000'000LL;
    in.available_cash = 1'000.0;
    in.current_market_exposure = 0.0;
    in.expected_taker_latency_seconds = 0.01;
    in.tick_size = 0.01;
    in.trigger_source = TriggerSource::ExternalPriceUpdate;
    in.cancel_path_healthy = 1;
    return in;
}

} // namespace

int main() {
    auto in = input();
    const auto out = run_external_fair_kernel(in);
    assert(out.causal_valid == 1);
    assert(out.fair_valid == 1);
    assert(out.external_only_revaluation == 1);
    assert(out.causal_cut.pm_state_version == 100);
    assert(out.causal_cut.external_state_version == 3);
    assert(out.candidate_count > 0);
    // Upward external information makes a low resting YES ask toxic. Critical
    // cancellation outranks any simultaneously positive informed TAKE.
    assert(out.chosen.action == Action::Cancel);
    assert(out.chosen.critical == 1);
    assert(out.chosen.cancel_reason == CancelReason::FairShock);

    // A second external receive can trigger another decision with identical PM
    // state/version. This is the mandatory PM-unchanged external shock case.
    auto second = in;
    second.causal_cut_id = 51;
    second.external.state_version = 4;
    second.external.venue_composite_price = 100.40;
    second.external.latest_input_receive_monotonic_ns = 130;
    second.causal_inputs.external_state_version = 4;
    second.causal_inputs.external_receive_ns = 130;
    second.trigger_source_version = 4;
    second.decision_monotonic_ns = 135;
    second.yes_book.receive_monotonic_ns = 119; // PM unchanged
    const auto out2 = run_external_fair_kernel(second);
    assert(out2.causal_valid == 1);
    assert(out2.causal_cut.pm_state_version == out.causal_cut.pm_state_version);
    assert(out2.causal_cut.external_state_version > out.causal_cut.external_state_version);
    assert(out2.external_only_revaluation == 1);
    assert(out2.chosen.action == Action::Cancel);

    // Future receive information invalidates the cut and fails closed for a
    // required market, instead of silently using the newer state.
    auto future = in;
    future.causal_cut_id = 52;
    future.causal_inputs.external_receive_ns = 130;
    future.decision_monotonic_ns = 125;
    const auto bad_cut = run_external_fair_kernel(future);
    assert(bad_cut.causal_valid == 0);
    assert(bad_cut.fail_closed == 1);
    assert(bad_cut.chosen.action == Action::Withdraw);
    assert(bad_cut.chosen.purpose == Purpose::Risk);

    // Invalid oracle is not substituted by external spot. Required market goes
    // risk-off even though Binance/Coinbase/Bybit state remains healthy.
    auto bad_oracle = in;
    bad_oracle.causal_cut_id = 53;
    bad_oracle.oracle.valid = 0;
    bad_oracle.oracle.continuity = OracleContinuity::ContinuityUnknown;
    const auto invalid = run_external_fair_kernel(bad_oracle);
    assert(invalid.fair_valid == 0);
    assert(invalid.fail_closed == 1);
    assert(invalid.chosen.action == Action::Withdraw);

    // Kill dominates the complete action set.
    auto killed = in;
    killed.causal_cut_id = 54;
    killed.global_kill = 1;
    const auto kill = run_external_fair_kernel(killed);
    assert(kill.chosen.action == Action::Withdraw);
    assert(kill.chosen.purpose == Purpose::Risk);
    assert(kill.chosen.critical == 1);

    std::cout << "v7 external kernel tests passed\n";
    return 0;
}
