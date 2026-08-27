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
};

struct OmsEvent {
    std::uint64_t event_id = 0;
    std::uint64_t source_version = 0;
    OmsEventType type = OmsEventType::QueueSend;
    std::int64_t timestamp_ns = 0;
    std::int64_t exchange_order_handle = 0;
    std::int64_t fill_delta_microunits = 0;
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
    std::int64_t submission_ns = 0;
    std::int64_t wire_ns = 0;
    std::int64_t ack_ns = 0;
    std::int64_t live_ns = 0;
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

} // namespace pm::v7
