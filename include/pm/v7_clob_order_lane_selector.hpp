#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7::clob {

constexpr std::size_t kMaxOrderTransportLanes = 4;

enum class OrderLanePhase : std::uint8_t {
    Idle = 0,
    Reserved = 1,
    SentAmbiguous = 2,
};

struct OrderLaneLease {
    std::uint64_t order_id = 0;
    std::uint64_t connection_epoch = 0;
    std::uint64_t lease_generation = 0;
    std::uint8_t lane = 0;
    std::uint8_t valid = 0;
};

struct OrderLaneSnapshot {
    std::int64_t last_roundtrip_ns = 0;
    std::int64_t last_rtt_ns = 0;
    std::uint64_t active_order_id = 0;
    std::uint64_t connection_epoch = 0;
    std::uint64_t lease_generation = 0;
    OrderLanePhase phase = OrderLanePhase::Idle;
    std::uint8_t connected = 0;
};

class OrderLaneSelector final {
public:
    OrderLaneSelector(std::size_t lane_count, std::int64_t max_idle_ns) noexcept;

    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] std::size_t lane_count() const noexcept { return lane_count_; }

    [[nodiscard]] bool on_connected(
        std::size_t lane, std::uint64_t connection_epoch,
        std::int64_t connected_ns) noexcept;
    [[nodiscard]] bool record_roundtrip(
        std::size_t lane, std::uint64_t connection_epoch,
        std::int64_t completed_ns, std::int64_t rtt_ns) noexcept;
    [[nodiscard]] bool on_disconnected(
        std::size_t lane, std::uint64_t connection_epoch) noexcept;

    [[nodiscard]] OrderLaneLease acquire(
        std::uint64_t order_id, std::int64_t now_ns) noexcept;
    [[nodiscard]] bool cancel_unsent(const OrderLaneLease& lease) noexcept;
    [[nodiscard]] bool mark_send_started(const OrderLaneLease& lease) noexcept;
    [[nodiscard]] bool complete_response(
        const OrderLaneLease& lease,
        std::int64_t completed_ns, std::int64_t rtt_ns) noexcept;
    [[nodiscard]] bool transport_failure(const OrderLaneLease& lease) noexcept;
    [[nodiscard]] bool resolve_ambiguous(const OrderLaneLease& lease) noexcept;

    [[nodiscard]] OrderLaneSnapshot snapshot(std::size_t lane) const noexcept;

private:
    struct LaneState {
        std::int64_t connected_ns = 0;
        std::int64_t last_roundtrip_ns = 0;
        std::int64_t last_rtt_ns = 0;
        std::uint64_t active_order_id = 0;
        std::uint64_t connection_epoch = 0;
        std::uint64_t lease_generation = 0;
        OrderLanePhase phase = OrderLanePhase::Idle;
        bool connected = false;
    };

    [[nodiscard]] bool lease_matches(const OrderLaneLease& lease) const noexcept;
    std::array<LaneState, kMaxOrderTransportLanes> lanes_{};
    std::size_t lane_count_ = 0;
    std::int64_t max_idle_ns_ = 0;
    std::uint64_t next_lease_generation_ = 1;
    bool valid_ = false;
};

} // namespace pm::v7::clob
