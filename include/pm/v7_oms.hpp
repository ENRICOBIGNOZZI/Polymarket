#pragma once

#include "pm/v7_intent.hpp"

#include <cstdint>
#include <type_traits>

namespace pm::v7 {

enum class OrderState : std::uint8_t {
    Intent = 1,
    SendPending = 2,
    AckPending = 3,
    Live = 4,
    Partial = 5,
    Filled = 6,
    CancelRequested = 7,
    CancelPending = 8,
    Cancelled = 9,
    Rejected = 10,
    Expired = 11,
    Unknown = 12,
    Reconciling = 13,
    Lost = 14,
    PendingDelay = 15,
};

enum class OmsEventType : std::uint8_t {
    QueueSend = 1,
    WireSend = 2,
    AckLive = 3,
    FillDelta = 4,
    RequestCancel = 5,
    WireCancel = 6,
    AckCancel = 7,
    Reject = 8,
    Expire = 9,
    TransportUnknown = 10,
    BeginReconcile = 11,
    ReconcileLive = 12,
    ReconcileCancelled = 13,
    ReconcileFilled = 14,
    ReconcileLost = 15,
    BeginDelay = 16,
    DelayElapsed = 17,
};

struct OmsEvent {
    std::uint64_t event_id = 0;
    std::uint64_t source_version = 0;
    OmsEventType type = OmsEventType::QueueSend;
    std::int64_t timestamp_ns = 0;
    std::int64_t exchange_order_handle = 0;
    std::int64_t fill_delta_microunits = 0;
    std::int32_t fill_price_e4 = 0; // actual execution price; zero means legacy/unknown.
    std::int32_t fill_price_reserved = 0;
    std::int64_t user_ws_match_monotonic_ns = 0;
    std::int64_t authoritative_filled_microunits = -1;
    std::int64_t authoritative_remaining_microunits = -1;
};

struct OmsOrderRecord {
    std::uint64_t intent_id = 0;
    std::uint64_t client_order_id = 0;
    std::uint64_t exchange_order_handle = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::uint64_t last_event_id = 0;
    std::uint64_t last_source_version = 0;
    StrategyId strategy_id = StrategyId::ProfessionalMaker;
    Side side = Side::None;
    OrderState state = OrderState::Intent;
    std::int64_t price_tick = 0;
    std::int64_t original_microunits = 0;
    std::int64_t filled_microunits = 0;
    std::int64_t remaining_microunits = 0;
    std::int64_t causal_trigger_receive_monotonic_ns = 0;
    std::int64_t signal_ready_monotonic_ns = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::int64_t submission_ns = 0;
    std::int64_t wire_ns = 0;
    std::int64_t ack_ns = 0;
    std::int64_t live_ns = 0;
    std::int64_t user_ws_match_ns = 0;
    std::int64_t delay_start_ns = 0;
    std::int64_t delay_release_ns = 0;
    std::int64_t cancel_request_ns = 0;
    std::int64_t cancel_wire_ns = 0;
    std::int64_t cancel_ack_ns = 0;
    std::int64_t cancel_effective_ns = 0;
};

struct OmsTransitionResult {
    OrderState state = OrderState::Intent;
    std::uint8_t applied = 0;
    std::uint8_t duplicate_or_stale = 0;
    std::uint8_t reconciliation_required = 0;
    std::uint8_t invariant_violation = 0;
};

// Validity is per leg: partial instrumentation remains useful without inventing
// zero-latency stages. All durations use one local monotonic clock domain.
enum OmsLatencyLeg : std::uint32_t {
    TriggerToSignal = 1U << 0,
    SignalToDecision = 1U << 1,
    TriggerToDecision = 1U << 2,
    DecisionToQueue = 1U << 3,
    QueueToWire = 1U << 4,
    WireToAck = 1U << 5,
    TriggerToWire = 1U << 6,
    TriggerToAck = 1U << 7,
    QueueToDelay = 1U << 8,
    DelayDuration = 1U << 9,
    DelayToWire = 1U << 10,
};

struct OmsLatencySnapshot {
    std::uint32_t valid_mask = 0;
    std::int64_t trigger_to_signal_ns = 0;
    std::int64_t signal_to_decision_ns = 0;
    std::int64_t trigger_to_decision_ns = 0;
    std::int64_t decision_to_queue_ns = 0;
    std::int64_t queue_to_wire_ns = 0;
    std::int64_t wire_to_ack_ns = 0;
    std::int64_t queue_to_delay_ns = 0;
    std::int64_t delay_duration_ns = 0;
    std::int64_t delay_to_wire_ns = 0;
    std::int64_t trigger_to_wire_ns = 0;
    std::int64_t trigger_to_ack_ns = 0;
};

[[nodiscard]] OmsLatencySnapshot oms_latency_snapshot(
    const OmsOrderRecord& record) noexcept;

class OmsOrder final {
public:
    OmsOrder() = default;
    OmsOrder(const StrategyIntent& intent, std::uint64_t client_order_id) noexcept;

    [[nodiscard]] const OmsOrderRecord& record() const noexcept { return record_; }
    [[nodiscard]] OmsTransitionResult apply(const OmsEvent& event) noexcept;

private:
    [[nodiscard]] OmsTransitionResult result(bool applied, bool duplicate,
                                             bool reconcile, bool violation) const noexcept;
    void mark_event(const OmsEvent& event) noexcept;
    void force_unknown(const OmsEvent& event) noexcept;
    [[nodiscard]] bool authoritative_sizes_valid(const OmsEvent& event) const noexcept;

    OmsOrderRecord record_{};
};

[[nodiscard]] const char* to_string(OrderState state) noexcept;

static_assert(std::is_trivially_copyable_v<OmsEvent>);
static_assert(std::is_trivially_copyable_v<OmsOrderRecord>);
static_assert(std::is_standard_layout_v<OmsOrderRecord>);
static_assert(std::is_trivially_copyable_v<OmsLatencySnapshot>);
static_assert(std::is_standard_layout_v<OmsLatencySnapshot>);

} // namespace pm::v7
