#include "pm/v7_private_state.hpp"

#include <limits>

namespace pm::v7 {

std::size_t PrivateOrderState::hash(std::uint64_t client_order_id) const noexcept {
    static_assert((kMaxPrivateOrders & (kMaxPrivateOrders - 1U)) == 0,
                  "private order capacity must be a power of two");
    std::uint64_t x = client_order_id + 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27U)) * 0x94d049bb133111ebULL;
    x ^= x >> 31U;
    return static_cast<std::size_t>(x) & (kMaxPrivateOrders - 1U);
}

PrivateOrderState::Slot* PrivateOrderState::find(std::uint64_t client_order_id) noexcept {
    if (client_order_id == 0) return nullptr;
    const std::size_t start = hash(client_order_id);
    for (std::size_t step = 0; step < kMaxPrivateOrders; ++step) {
        auto& slot = slots_[(start + step) & (kMaxPrivateOrders - 1U)];
        if (slot.state == SlotState::Empty) return nullptr;
        if (slot.state == SlotState::Active && slot.client_order_id == client_order_id) return &slot;
    }
    return nullptr;
}

const PrivateOrderState::Slot* PrivateOrderState::find(
    std::uint64_t client_order_id) const noexcept {
    if (client_order_id == 0) return nullptr;
    const std::size_t start = hash(client_order_id);
    for (std::size_t step = 0; step < kMaxPrivateOrders; ++step) {
        const auto& slot = slots_[(start + step) & (kMaxPrivateOrders - 1U)];
        if (slot.state == SlotState::Empty) return nullptr;
        if (slot.state == SlotState::Active && slot.client_order_id == client_order_id) return &slot;
    }
    return nullptr;
}

PrivateOrderState::Slot* PrivateOrderState::insert_slot(
    std::uint64_t client_order_id) noexcept {
    if (client_order_id == 0) return nullptr;
    const std::size_t start = hash(client_order_id);
    Slot* first_tombstone = nullptr;
    for (std::size_t step = 0; step < kMaxPrivateOrders; ++step) {
        auto& slot = slots_[(start + step) & (kMaxPrivateOrders - 1U)];
        if (slot.state == SlotState::Active && slot.client_order_id == client_order_id) return &slot;
        if (slot.state == SlotState::Tombstone && first_tombstone == nullptr) first_tombstone = &slot;
        if (slot.state == SlotState::Empty) return first_tombstone != nullptr ? first_tombstone : &slot;
    }
    return first_tombstone;
}

bool PrivateOrderState::terminal(OrderState state) noexcept {
    return state == OrderState::Filled || state == OrderState::Cancelled
        || state == OrderState::Rejected || state == OrderState::Expired
        || state == OrderState::Lost;
}

OmsEvent PrivateOrderState::control_event(OmsEventType type,
                                          std::int64_t timestamp_ns) noexcept {
    OmsEvent event;
    if (event_sequence_ == std::numeric_limits<std::uint64_t>::max()) event_sequence_ = 1;
    else ++event_sequence_;
    event.event_id = event_sequence_;
    event.type = type;
    event.timestamp_ns = timestamp_ns;
    // Local control transitions do not claim an authoritative venue source
    // version; that lineage remains reserved for actual private snapshots.
    event.source_version = 0;
    return event;
}

void PrivateOrderState::erase(Slot& slot) noexcept {
    slot.order = OmsOrder{};
    slot.client_order_id = 0;
    slot.state = SlotState::Tombstone;
}

void PrivateOrderState::bump_version() noexcept {
    if (state_version_ == std::numeric_limits<std::uint64_t>::max()) state_version_ = 1;
    else ++state_version_;
}

bool PrivateOrderState::register_order(const StrategyIntent& intent,
                                       std::uint64_t client_order_id) noexcept {
    if (stream_state_ != PrivateStreamState::Healthy || client_order_id == 0
        || intent.intent_id == 0 || intent.quantity_microunits <= 0) {
        return false;
    }
    if (const auto* existing = find(client_order_id); existing != nullptr) {
        const auto& record = existing->order.record();
        return record.intent_id == intent.intent_id
            && record.market_handle == intent.market_handle
            && record.instrument_handle == intent.instrument_handle
            && record.side == intent.side
            && record.price_tick == intent.price_tick
            && record.original_microunits == intent.quantity_microunits;
    }

    auto* slot = insert_slot(client_order_id);
    if (slot == nullptr || slot->state == SlotState::Active) return false;
    slot->order = OmsOrder(intent, client_order_id);
    slot->client_order_id = client_order_id;
    slot->state = SlotState::Active;
    ++tracked_orders_;
    bump_version();
    return true;
}

OmsTransitionResult PrivateOrderState::apply_order_event(
    std::uint64_t client_order_id,
    const OmsEvent& event) noexcept {
    auto* slot = find(client_order_id);
    if (slot == nullptr) {
        return OmsTransitionResult{OrderState::Unknown, 0, 0, 1, 1};
    }
    if (stream_state_ != PrivateStreamState::Healthy) {
        return OmsTransitionResult{slot->order.record().state, 0, 0, 1, 0};
    }
    const auto result = slot->order.apply(event);
    if (result.applied) bump_version();
    if (result.reconciliation_required) stream_state_ = PrivateStreamState::ReconcileRequired;
    return result;
}

bool PrivateOrderState::on_stream_disconnect(std::int64_t timestamp_ns) noexcept {
    if (timestamp_ns <= 0) return false;
    bool ok = true;
    std::size_t unresolved = 0;
    for (auto& slot : slots_) {
        if (slot.state != SlotState::Active) continue;
        const auto state = slot.order.record().state;
        if (terminal(state)) continue;
        const auto result = slot.order.apply(control_event(OmsEventType::TransportUnknown, timestamp_ns));
        if (result.invariant_violation) ok = false;
        if (!terminal(slot.order.record().state)) ++unresolved;
    }
    stream_state_ = unresolved > 0
        ? PrivateStreamState::ReconcileRequired : PrivateStreamState::Disconnected;
    bump_version();
    return ok;
}

bool PrivateOrderState::on_stream_connected(std::int64_t timestamp_ns) noexcept {
    if (timestamp_ns <= 0) return false;
    std::size_t unresolved = 0;
    for (const auto& slot : slots_) {
        if (slot.state != SlotState::Active) continue;
        const auto state = slot.order.record().state;
        if (state == OrderState::Unknown || state == OrderState::Reconciling) ++unresolved;
    }
    stream_state_ = unresolved == 0
        ? PrivateStreamState::Healthy : PrivateStreamState::ReconcileRequired;
    bump_version();
    return true;
}

bool PrivateOrderState::begin_reconciliation(std::int64_t timestamp_ns) noexcept {
    if (timestamp_ns <= 0 || stream_state_ != PrivateStreamState::ReconcileRequired) return false;
    bool ok = true;
    std::size_t unresolved = 0;
    for (auto& slot : slots_) {
        if (slot.state != SlotState::Active) continue;
        if (slot.order.record().state == OrderState::Unknown) {
            const auto result = slot.order.apply(control_event(OmsEventType::BeginReconcile, timestamp_ns));
            if (result.invariant_violation || slot.order.record().state != OrderState::Reconciling) ok = false;
        }
        if (slot.order.record().state == OrderState::Unknown
            || slot.order.record().state == OrderState::Reconciling) {
            ++unresolved;
        }
    }
    stream_state_ = unresolved > 0 ? PrivateStreamState::Reconciling : PrivateStreamState::Healthy;
    bump_version();
    return ok;
}

bool PrivateOrderState::reconcile(const AuthoritativeOrderSnapshot& snapshot) noexcept {
    if (stream_state_ != PrivateStreamState::Reconciling
        || snapshot.client_order_id == 0 || snapshot.timestamp_ns <= 0
        || snapshot.source_version == 0) {
        return false;
    }
    auto* slot = find(snapshot.client_order_id);
    if (slot == nullptr) return false;
    const auto current = slot->order.record().state;
    if (current != OrderState::Unknown && current != OrderState::Reconciling) return false;

    OmsEvent event;
    if (event_sequence_ == std::numeric_limits<std::uint64_t>::max()) event_sequence_ = 1;
    else ++event_sequence_;
    event.event_id = event_sequence_;
    event.source_version = snapshot.source_version;
    event.timestamp_ns = snapshot.timestamp_ns;
    event.exchange_order_handle = snapshot.exchange_order_handle;
    event.authoritative_filled_microunits = snapshot.filled_microunits;
    event.authoritative_remaining_microunits = snapshot.remaining_microunits;
    switch (snapshot.state) {
        case AuthoritativeOrderState::Live:
            event.type = OmsEventType::ReconcileLive;
            break;
        case AuthoritativeOrderState::Cancelled:
            event.type = OmsEventType::ReconcileCancelled;
            break;
        case AuthoritativeOrderState::Filled:
            event.type = OmsEventType::ReconcileFilled;
            break;
        case AuthoritativeOrderState::Lost:
            event.type = OmsEventType::ReconcileLost;
            break;
    }

    const auto result = slot->order.apply(event);
    if (result.applied) bump_version();
    return result.applied && !result.invariant_violation && !result.reconciliation_required;
}

bool PrivateOrderState::complete_reconciliation() noexcept {
    if (stream_state_ != PrivateStreamState::Reconciling) return false;
    for (const auto& slot : slots_) {
        if (slot.state != SlotState::Active) continue;
        const auto state = slot.order.record().state;
        if (state == OrderState::Unknown || state == OrderState::Reconciling) return false;
    }
    stream_state_ = PrivateStreamState::Healthy;
    bump_version();
    return true;
}

bool PrivateOrderState::retire_terminal_order(std::uint64_t client_order_id) noexcept {
    auto* slot = find(client_order_id);
    if (slot == nullptr || !terminal(slot->order.record().state)) return false;
    erase(*slot);
    if (tracked_orders_ > 0) --tracked_orders_;
    bump_version();
    return true;
}

const OmsOrderRecord* PrivateOrderState::order(std::uint64_t client_order_id) const noexcept {
    const auto* slot = find(client_order_id);
    return slot == nullptr ? nullptr : &slot->order.record();
}

PrivateStateSnapshot PrivateOrderState::snapshot() const noexcept {
    PrivateStateSnapshot out;
    out.stream_state = stream_state_;
    out.tracked_orders = tracked_orders_;
    for (const auto& slot : slots_) {
        if (slot.state != SlotState::Active) continue;
        const auto state = slot.order.record().state;
        if (state == OrderState::Unknown || state == OrderState::Reconciling) ++out.unresolved_orders;
    }
    out.submissions_safe = static_cast<std::uint8_t>(stream_state_ == PrivateStreamState::Healthy);
    out.state_version = state_version_;
    return out;
}

} // namespace pm::v7
