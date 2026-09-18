#include "pm/v7_native_settlement_oms_endpoint.hpp"

// Kept as an explicit translation unit in the immutable London runtime build.

namespace pm::v7 {

const OmsOrderRecord* NativeSettlementOmsEndpoint::find(
    std::uint64_t client_order_id) const noexcept {
    if (const auto* current = authority_.find_order(client_order_id)) return current;
    return client_order_id != 0 && terminal_record_.client_order_id == client_order_id
        ? &terminal_record_ : nullptr;
}

OmsTransitionResult NativeSettlementOmsEndpoint::apply_owned(
    std::uint64_t client_order_id, OmsEvent event) noexcept {
    if (!healthy_) {
        OmsTransitionResult failed;
        failed.state = OrderState::Unknown;
        failed.invariant_violation = 1;
        failed.reconciliation_required = 1;
        return failed;
    }
    const auto result = authority_.apply_order_event(client_order_id, event);
    auto transition = result.transition;
    if (result.reason != NativeSettlementAuthorityReason::Accepted
        || transition.invariant_violation || transition.reconciliation_required) {
        healthy_ = false;
        transition.invariant_violation = 1;
        transition.reconciliation_required = 1;
        return transition;
    }
    if (result.applied) ++lifecycle_events_;
    if (result.terminal_retired) terminal_record_ = result.record;
    return transition;
}

bool NativeSettlementOmsEndpoint::observe_unsent(
    const NativeOrderCommand& command, std::int64_t now_monotonic_ns) noexcept {
    const auto* record = authority_.find_order(command.client_order_id);
    if (!matches_pending_command(command) || record == nullptr || record->state != OrderState::SendPending
        || command.command_id == 0 || command.client_order_id == 0
        || command.intent_id != record->intent_id
        || command.market_handle != record->market_handle
        || command.event_handle != record->event_handle
        || command.instrument_handle != record->instrument_handle
        || command.price_tick != record->price_tick
        || command.quantity_microunits != record->original_microunits
        || command.side != record->side || command.tick_size_e4 <= 0
        || command.decision_monotonic_ns != record->decision_monotonic_ns
        || command.queue_monotonic_ns != record->submission_ns
        || now_monotonic_ns < command.queue_monotonic_ns) {
        ++invalid_commands_;
        // Preserve the existing reservation for explicit reconciliation.
        healthy_ = false;
        return false;
    }
    OmsEvent rejected;
    rejected.type = OmsEventType::Reject;
    rejected.timestamp_ns = now_monotonic_ns;
    const auto result = apply_owned(command.client_order_id, rejected);
    if (!result.applied || result.state != OrderState::Rejected
        || result.invariant_violation || result.reconciliation_required) return false;
    ++observed_unsent_;
    return true;
}

} // namespace pm::v7
