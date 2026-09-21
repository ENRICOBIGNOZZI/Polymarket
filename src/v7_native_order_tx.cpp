#include "pm/v7_native_order_tx.hpp"

#include <algorithm>
#include <limits>

namespace pm::v7 {
namespace {

[[nodiscard]] bool terminal(OrderState state) noexcept {
    return state == OrderState::Filled || state == OrderState::Cancelled
        || state == OrderState::Rejected || state == OrderState::Expired
        || state == OrderState::Lost;
}

[[nodiscard]] AdapterTimeInForce tif_for(const ExecutionPlan& plan) noexcept {
    if (plan.intent.strategy_id == StrategyId::CryptoLatencyArb) return AdapterTimeInForce::Fok;
    if (plan.policy == ExecutionPolicyId::AggressiveTaker) return AdapterTimeInForce::Fak;
    return AdapterTimeInForce::Gtc;
}

} // namespace

NativeOrderTxOwner::NativeOrderTxOwner() noexcept {
    for (std::size_t i = 0; i < kNativeOrderTxCapacity; ++i) {
        free_slots_[i] = static_cast<std::uint16_t>(kNativeOrderTxCapacity - 1 - i);
    }
}

std::uint64_t NativeOrderTxOwner::next_nonzero(std::uint64_t& value) noexcept {
    ++value;
    if (value == 0) ++value;
    return value;
}

NativeOrderTxOwner::Slot* NativeOrderTxOwner::find_slot(
    std::uint64_t client_order_id) noexcept {
    if (client_order_id == 0) return nullptr;
    const auto index = static_cast<std::size_t>(client_order_id) & (kNativeOrderTxCapacity - 1);
    auto& slot = slots_[index];
    return slot.occupied != 0 && slot.client_order_id == client_order_id ? &slot : nullptr;
}

const NativeOrderTxOwner::Slot* NativeOrderTxOwner::find_slot(
    std::uint64_t client_order_id) const noexcept {
    if (client_order_id == 0) return nullptr;
    const auto index = static_cast<std::size_t>(client_order_id) & (kNativeOrderTxCapacity - 1);
    const auto& slot = slots_[index];
    return slot.occupied != 0 && slot.client_order_id == client_order_id ? &slot : nullptr;
}

NativeOrderTxResult NativeOrderTxOwner::prepare_submit(
    const ExecutionPlan& plan,
    std::int64_t now_monotonic_ns) noexcept {
    NativeOrderTxResult out;
    const auto& intent = plan.intent;
    if (intent.intent_id == 0 || intent.market_handle == 0 || intent.instrument_handle == 0
        || intent.quantity_microunits <= 0 || intent.price_tick <= 0
        || plan.tick_size_e4 <= 0 || now_monotonic_ns <= 0
        || intent.decision_monotonic_ns <= 0 || intent.decision_monotonic_ns > now_monotonic_ns
        || (intent.side != Side::Buy && intent.side != Side::Sell)
        || (intent.type != IntentType::Quote && intent.type != IntentType::TargetPosition)) {
        out.reason = NativeOrderTxReason::InvalidPlan;
        return out;
    }
    if (free_count_ == 0) {
        out.reason = NativeOrderTxReason::CapacityFull;
        return out;
    }

    const auto index = static_cast<std::size_t>(free_slots_[--free_count_]);
    auto& slot = slots_[index];
    auto generation = ++slot.generation;
    if (generation == 0) generation = ++slot.generation;
    const auto client_order_id = (generation << 10U) | static_cast<std::uint64_t>(index);
    slot.client_order_id = client_order_id;
    slot.occupied = 1;
    slot.order = OmsOrder(intent, client_order_id);
    ++active_orders_;

    OmsEvent queued;
    queued.event_id = next_nonzero(next_event_id_);
    queued.type = OmsEventType::QueueSend;
    queued.timestamp_ns = now_monotonic_ns;
    const auto transition = slot.order.apply(queued);
    if (transition.applied == 0 || transition.invariant_violation != 0
        || transition.state != OrderState::SendPending) {
        release_slot(index);
        out.reason = NativeOrderTxReason::OmsRejected;
        return out;
    }

    NativeOrderCommand command;
    command.command_id = next_nonzero(next_command_id_);
    command.client_order_id = client_order_id;
    command.intent_id = intent.intent_id;
    command.market_handle = intent.market_handle;
    command.event_handle = intent.event_handle;
    command.instrument_handle = intent.instrument_handle;
    command.market_state_version = plan.market_state_version;
    command.price_tick = intent.price_tick;
    command.quantity_microunits = intent.quantity_microunits;
    command.decision_monotonic_ns = intent.decision_monotonic_ns;
    command.queue_monotonic_ns = now_monotonic_ns;
    command.tick_size_e4 = plan.tick_size_e4;
    command.side = intent.side;
    command.time_in_force = tif_for(plan);
    command.post_only = intent.post_only;
    command.passive = intent.passive;

    out.command = command;
    out.oms = slot.order.record();
    out.reason = NativeOrderTxReason::Accepted;
    out.accepted = 1;
    return out;
}

NativeCancelTxResult NativeOrderTxOwner::prepare_cancel(
    std::uint64_t client_order_id,
    std::int64_t now_monotonic_ns) noexcept {
    NativeCancelTxResult out;
    auto* slot = find_slot(client_order_id);
    if (slot == nullptr || now_monotonic_ns <= 0) {
        out.reason = NativeCancelTxReason::UnknownClientOrder;
        return out;
    }
    const auto before = slot->order.record();
    if (before.state == OrderState::CancelRequested
        || before.state == OrderState::CancelPending
        || before.state == OrderState::Cancelled
        || before.state == OrderState::Filled) {
        out.oms = before;
        out.reason = NativeCancelTxReason::DuplicateNoop;
        return out;
    }
    // A real adapter needs an acknowledged exchange order identity. Never
    // manufacture a cancel for a merely queued/unacknowledged order.
    if ((before.state != OrderState::Live && before.state != OrderState::Partial)
        || before.exchange_order_handle == 0) {
        out.oms = before;
        out.reason = NativeCancelTxReason::NotCancelable;
        return out;
    }

    OmsEvent request;
    request.event_id = next_nonzero(next_event_id_);
    request.type = OmsEventType::RequestCancel;
    request.timestamp_ns = now_monotonic_ns;
    const auto transition = slot->order.apply(request);
    if (transition.applied == 0 || transition.invariant_violation != 0
        || transition.state != OrderState::CancelRequested) {
        out.oms = slot->order.record();
        out.reason = NativeCancelTxReason::OmsRejected;
        return out;
    }

    NativeCancelCommand command;
    command.command_id = next_nonzero(next_command_id_);
    command.client_order_id = client_order_id;
    command.intent_id = before.intent_id;
    command.market_handle = before.market_handle;
    command.instrument_handle = before.instrument_handle;
    command.exchange_order_handle = before.exchange_order_handle;
    command.queue_monotonic_ns = now_monotonic_ns;
    out.command = command;
    out.oms = slot->order.record();
    out.reason = NativeCancelTxReason::Accepted;
    out.accepted = 1;
    return out;
}

OmsTransitionResult NativeOrderTxOwner::apply(
    std::uint64_t client_order_id,
    const OmsEvent& event) noexcept {
    auto* slot = find_slot(client_order_id);
    if (slot == nullptr) {
        OmsTransitionResult out;
        out.invariant_violation = 1;
        out.reconciliation_required = 1;
        out.state = OrderState::Unknown;
        return out;
    }
    return slot->order.apply(event);
}

OmsTransitionResult NativeOrderTxOwner::apply_owned(
    std::uint64_t client_order_id, OmsEvent event) noexcept {
    event.event_id = next_nonzero(next_event_id_);
    return apply(client_order_id, event);
}

void NativeOrderTxOwner::release_slot(std::size_t index) noexcept {
    auto& slot = slots_[index];
    if (slot.occupied == 0) return;
    slot.order = OmsOrder{};
    slot.client_order_id = 0;
    slot.occupied = 0;
    free_slots_[free_count_++] = static_cast<std::uint16_t>(index);
    if (active_orders_ > 0) --active_orders_;
}

bool NativeOrderTxOwner::retire_terminal(std::uint64_t client_order_id) noexcept {
    auto* slot = find_slot(client_order_id);
    if (slot == nullptr || !terminal(slot->order.record().state)) return false;
    const auto index = static_cast<std::size_t>(slot - slots_.data());
    release_slot(index);
    return true;
}

const OmsOrderRecord* NativeOrderTxOwner::find(std::uint64_t client_order_id) const noexcept {
    const auto* slot = find_slot(client_order_id);
    return slot == nullptr ? nullptr : &slot->order.record();
}

} // namespace pm::v7
