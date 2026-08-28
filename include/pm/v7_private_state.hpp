#pragma once

#include "pm/v7_oms.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

inline constexpr std::size_t kMaxPrivateOrders = 2048;

enum class PrivateStreamState : std::uint8_t {
    Disconnected = 1,
    Healthy = 2,
    ReconcileRequired = 3,
    Reconciling = 4,
};

enum class AuthoritativeOrderState : std::uint8_t {
    Live = 1,
    Cancelled = 2,
    Filled = 3,
    Lost = 4,
};

struct AuthoritativeOrderSnapshot {
    std::uint64_t client_order_id = 0;
    std::int64_t exchange_order_handle = 0;
    std::int64_t filled_microunits = -1;
    std::int64_t remaining_microunits = -1;
    std::int64_t timestamp_ns = 0;
    std::uint64_t source_version = 0;
    AuthoritativeOrderState state = AuthoritativeOrderState::Live;
};

struct PrivateStateSnapshot {
    PrivateStreamState stream_state = PrivateStreamState::Disconnected;
    std::size_t tracked_orders = 0;
    std::size_t unresolved_orders = 0;
    std::uint8_t submissions_safe = 0;
    std::uint64_t state_version = 0;
};

// Canonical V7 private order-state owner. This class contains no networking,
// credentials or authenticated execution path; it only defines deterministic
// state/reconciliation semantics that a future private-user WS adapter can feed.
// The current PAPER runtime may test this abstraction without enabling auth.
class PrivateOrderState final {
public:
    PrivateOrderState() noexcept = default;

    [[nodiscard]] bool register_order(const StrategyIntent& intent,
                                      std::uint64_t client_order_id) noexcept;
    [[nodiscard]] OmsTransitionResult apply_order_event(
        std::uint64_t client_order_id,
        const OmsEvent& event) noexcept;

    // Stream loss invalidates certainty for every non-terminal tracked order.
    // New submissions remain unsafe until begin_reconciliation + authoritative
    // resolution + complete_reconciliation succeed.
    [[nodiscard]] bool on_stream_disconnect(std::int64_t timestamp_ns) noexcept;
    [[nodiscard]] bool on_stream_connected(std::int64_t timestamp_ns) noexcept;
    [[nodiscard]] bool begin_reconciliation(std::int64_t timestamp_ns) noexcept;
    [[nodiscard]] bool reconcile(const AuthoritativeOrderSnapshot& snapshot) noexcept;
    [[nodiscard]] bool complete_reconciliation() noexcept;

    [[nodiscard]] const OmsOrderRecord* order(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] PrivateStateSnapshot snapshot() const noexcept;

private:
    enum class SlotState : std::uint8_t { Empty = 0, Active = 1, Tombstone = 2 };
    struct Slot {
        OmsOrder order{};
        std::uint64_t client_order_id = 0;
        SlotState state = SlotState::Empty;
    };

    [[nodiscard]] std::size_t hash(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] Slot* find(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] const Slot* find(std::uint64_t client_order_id) const noexcept;
    [[nodiscard]] Slot* insert_slot(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] static bool terminal(OrderState state) noexcept;
    [[nodiscard]] OmsEvent control_event(OmsEventType type,
                                         std::int64_t timestamp_ns) noexcept;
    void bump_version() noexcept;

    std::array<Slot, kMaxPrivateOrders> slots_{};
    PrivateStreamState stream_state_ = PrivateStreamState::Disconnected;
    std::size_t tracked_orders_ = 0;
    std::uint64_t event_sequence_ = 0;
    std::uint64_t source_version_ = 0;
    std::uint64_t state_version_ = 1;
};

} // namespace pm::v7
