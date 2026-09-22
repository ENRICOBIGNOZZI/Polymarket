#pragma once

#include "pm/v7_execution_plan.hpp"
#include "pm/v7_oms.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7 {

inline constexpr std::size_t kNativeOrderTxCapacity = 1024;
static_assert((kNativeOrderTxCapacity & (kNativeOrderTxCapacity - 1)) == 0);

enum class AdapterTimeInForce : std::uint8_t {
    Gtc = 1,
    Fak = 2,
    Fok = 3,
};

enum class NativeOrderTxReason : std::uint8_t {
    Accepted = 1,
    InvalidPlan = 2,
    CapacityFull = 3,
    OmsRejected = 4,
    UnknownClientOrder = 5,
    NonTerminalRetire = 6,
};

enum class NativeCancelTxReason : std::uint8_t {
    Accepted = 1,
    UnknownClientOrder = 2,
    NotCancelable = 3,
    DuplicateNoop = 4,
    OmsRejected = 5,
};

struct NativeOrderCommand {
    std::uint64_t command_id = 0;
    std::uint64_t client_order_id = 0;
    std::uint64_t intent_id = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t market_state_version = 0;
    std::int64_t price_tick = 0;
    std::int64_t quantity_microunits = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::int64_t risk_admitted_monotonic_ns = 0;
    std::int64_t queue_monotonic_ns = 0;
    std::int32_t tick_size_e4 = 0;
    Side side = Side::None;
    AdapterTimeInForce time_in_force = AdapterTimeInForce::Gtc;
    std::uint8_t post_only = 0;
    std::uint8_t passive = 0;
    std::array<std::uint8_t, 4> reserved{};
    friend bool operator==(const NativeOrderCommand&, const NativeOrderCommand&) = default;
};

struct NativeOrderTxResult {
    NativeOrderCommand command{};
    OmsOrderRecord oms{};
    NativeOrderTxReason reason = NativeOrderTxReason::InvalidPlan;
    std::uint8_t accepted = 0;
    std::array<std::uint8_t, 6> reserved{};
};

struct NativeCancelCommand {
    std::uint64_t command_id = 0;
    std::uint64_t client_order_id = 0;
    std::uint64_t intent_id = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t exchange_order_handle = 0;
    std::int64_t queue_monotonic_ns = 0;
};

struct NativeCancelTxResult {
    NativeCancelCommand command{};
    OmsOrderRecord oms{};
    NativeCancelTxReason reason = NativeCancelTxReason::UnknownClientOrder;
    std::uint8_t accepted = 0;
    std::array<std::uint8_t, 6> reserved{};
};

class NativeOrderTxOwner final {
public:
    NativeOrderTxOwner() noexcept;

    [[nodiscard]] NativeOrderTxResult prepare_submit(
        const ExecutionPlan& plan,
        std::int64_t now_monotonic_ns) noexcept;
    [[nodiscard]] NativeCancelTxResult prepare_cancel(
        std::uint64_t client_order_id,
        std::int64_t now_monotonic_ns) noexcept;
    [[nodiscard]] OmsTransitionResult apply(
        std::uint64_t client_order_id,
        const OmsEvent& event) noexcept;
    // Canonical local-event path: the OMS owner assigns the event id.
    [[nodiscard]] OmsTransitionResult apply_owned(
        std::uint64_t client_order_id,
        OmsEvent event) noexcept;
    [[nodiscard]] bool retire_terminal(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] const OmsOrderRecord* find(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] std::size_t active_orders() const noexcept { return active_orders_; }

private:
    struct Slot {
        OmsOrder order{};
        std::uint64_t client_order_id = 0;
        std::uint64_t generation = 0;
        std::uint8_t occupied = 0;
        std::array<std::uint8_t, 7> reserved{};
    };

    [[nodiscard]] Slot* find_slot(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] const Slot* find_slot(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] std::uint64_t next_nonzero(std::uint64_t& value) noexcept;
    void release_slot(std::size_t index) noexcept;

    std::array<Slot, kNativeOrderTxCapacity> slots_{};
    std::array<std::uint16_t, kNativeOrderTxCapacity> free_slots_{};
    std::size_t free_count_ = kNativeOrderTxCapacity;
    std::size_t active_orders_ = 0;
    std::uint64_t next_event_id_ = 0;
    std::uint64_t next_command_id_ = 0;
};

static_assert(std::is_trivially_copyable_v<NativeOrderCommand>);
static_assert(std::is_standard_layout_v<NativeOrderCommand>);
static_assert(std::is_trivially_copyable_v<NativeOrderTxResult>);
static_assert(std::is_trivially_copyable_v<NativeCancelCommand>);
static_assert(std::is_standard_layout_v<NativeCancelCommand>);
static_assert(std::is_trivially_copyable_v<NativeCancelTxResult>);

} // namespace pm::v7
