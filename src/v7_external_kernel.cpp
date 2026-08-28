#include "pm/v7_external_kernel.hpp"

#include <algorithm>
#include <cmath>

namespace pm::v7::external_fair {
namespace {

void push_candidate(ExternalKernelOutput& output, const CandidateAction& candidate) noexcept {
    if (candidate.action == Action::Nothing || candidate.admissible == 0
        || output.candidate_count >= output.candidates.size()) {
        return;
    }
    output.candidates[output.candidate_count++] = candidate;
}

[[nodiscard]] CandidateAction fail_closed_withdraw(
    const ExternalKernelInput& input,
    std::uint64_t causal_cut_id) noexcept {
    CandidateAction action;
    action.action = Action::Withdraw;
    action.purpose = Purpose::Risk;
    action.market_handle = input.contract.market_handle;
    action.causal_cut_id = causal_cut_id;
    action.cancel_reason = CancelReason::Risk;
    action.admissible = 1;
    action.critical = 1;
    return action;
}

[[nodiscard]] bool touch_valid(const MakerTouchSnapshot& touch) noexcept {
    return touch.valid != 0 && touch.best_bid_tick > 0
        && touch.best_ask_tick > touch.best_bid_tick;
}

void append_maker_pair(
    ExternalKernelOutput& output,
    const ExternalKernelInput& input,
    bool instrument_is_yes,
    std::uint64_t instrument_handle,
    const MakerTouchSnapshot& touch,
    double inventory_fraction) noexcept {
    if (input.allow_shadow_maker == 0 || input.maker_policy.enabled == 0
        || !touch_valid(touch)) return;
    push_candidate(output, build_external_make(
        input.contract, output.fair, input.maker_policy, Side::Buy,
        instrument_is_yes, instrument_handle, output.causal_cut.causal_cut_id,
        touch.best_bid_tick, touch.best_ask_tick, input.tick_size,
        inventory_fraction, input.decision_monotonic_ns));
    push_candidate(output, build_external_make(
        input.contract, output.fair, input.maker_policy, Side::Sell,
        instrument_is_yes, instrument_handle, output.causal_cut.causal_cut_id,
        touch.best_bid_tick, touch.best_ask_tick, input.tick_size,
        inventory_fraction, input.decision_monotonic_ns));
}

} // namespace

ExternalKernelOutput run_external_fair_kernel(
    const ExternalKernelInput& input) noexcept {
    ExternalKernelOutput output;

    output.causal_cut = make_causal_cut(
        input.causal_cut_id,
        input.decision_monotonic_ns,
        input.trigger_source,
        input.trigger_source_version,
        input.causal_inputs);
    output.causal_valid = output.causal_cut.valid;
    output.external_only_revaluation = (
        input.trigger_source == TriggerSource::ExternalPriceUpdate
        || input.trigger_source == TriggerSource::OracleUpdate
        || input.trigger_source == TriggerSource::SettlementReferenceUpdate) ? 1 : 0;

    const bool killed = input.global_kill != 0 || input.strategy_kill != 0
        || input.market_kill != 0;
    if (killed || input.cancel_path_healthy == 0) {
        output.fail_closed = 1;
        push_candidate(output, fail_closed_withdraw(input, output.causal_cut.causal_cut_id));
        output.chosen = choose_unified_action(
            output.candidates, output.candidate_count, killed);
        return output;
    }

    if (output.causal_cut.valid == 0) {
        if (input.policy.external_fair_required != 0) {
            output.fail_closed = 1;
            push_candidate(output, fail_closed_withdraw(input, input.causal_cut_id));
            output.chosen = choose_unified_action(
                output.candidates, output.candidate_count, false);
        }
        return output;
    }

    output.fair = compute_fair_value(
        input.contract,
        input.reference,
        input.oracle,
        input.external,
        input.model,
        input.decision_monotonic_ns,
        input.time_to_expiry_ns,
        input.model_drift_score);
    output.fair_valid = output.fair.valid;

    // Risk-priority cancel/withdraw candidates are evaluated before any maker
    // or taker alpha. No quote-lifetime/cooldown enters this critical path.
    for (const auto& quote : input.resting_quotes) {
        if (quote.active == 0 || quote.cancel_pending != 0
            || quote.instrument_handle == 0 || quote.side == Side::None
            || !std::isfinite(quote.price) || quote.price <= 0.0 || quote.price >= 1.0) {
            continue;
        }
        push_candidate(output, evaluate_cancel_overlay(
            input.contract,
            output.fair,
            input.policy,
            quote.side,
            quote.instrument_is_yes != 0,
            quote.instrument_handle,
            quote.price,
            quote.price_tick,
            output.causal_cut.causal_cut_id,
            input.decision_monotonic_ns,
            false));
    }

    if (output.fair.valid == 0) {
        if (input.policy.external_fair_required != 0) {
            output.fail_closed = 1;
            push_candidate(output, fail_closed_withdraw(
                input, output.causal_cut.causal_cut_id));
        }
        output.chosen = choose_unified_action(
            output.candidates, output.candidate_count, false);
        return output;
    }

    if (input.allow_shadow_taker != 0 && input.policy.taker_enabled != 0) {
        push_candidate(output, build_informed_take(
            input.contract,
            output.fair,
            input.fee,
            input.policy,
            input.yes_book,
            true,
            input.contract.yes_instrument_handle,
            output.causal_cut.causal_cut_id,
            input.decision_monotonic_ns,
            input.available_cash,
            input.current_market_exposure,
            input.expected_taker_latency_seconds));
        push_candidate(output, build_informed_take(
            input.contract,
            output.fair,
            input.fee,
            input.policy,
            input.no_book,
            false,
            input.contract.no_instrument_handle,
            output.causal_cut.causal_cut_id,
            input.decision_monotonic_ns,
            input.available_cash,
            input.current_market_exposure,
            input.expected_taker_latency_seconds));
    }

    append_maker_pair(output, input, true, input.contract.yes_instrument_handle,
                      input.yes_touch, input.signed_yes_inventory_fraction);
    append_maker_pair(output, input, false, input.contract.no_instrument_handle,
                      input.no_touch, input.signed_no_inventory_fraction);

    output.chosen = choose_unified_action(
        output.candidates, output.candidate_count, false);
    return output;
}

} // namespace pm::v7::external_fair
