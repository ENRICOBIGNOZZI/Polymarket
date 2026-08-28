#pragma once

#include "pm/v7_external_protocol.hpp"
#include "pm/v7_external_state.hpp"
#include "pm/v7_spsc.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string_view>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::size_t kExternalIngressQueueCapacity = 4096;
inline constexpr std::size_t kExternalIngressDecodeBatch = 32;

struct ExternalIngressSnapshot {
    std::uint64_t frames = 0;
    std::uint64_t decoded_events = 0;
    std::uint64_t enqueued_events = 0;
    std::uint64_t drained_events = 0;
    std::uint64_t dropped_events = 0;
    std::uint64_t invalid_frames = 0;
    std::uint64_t reconnects = 0;
    std::uint64_t propagated_gaps = 0;
    std::uint64_t connection_epoch = 0;
    std::size_t queued = 0;
    VenueId venue = VenueId::Unknown;
    std::uint8_t healthy = 0;
    std::uint8_t gap_pending = 0;
    std::array<std::uint8_t, 5> reserved{};
};

// One instance per persistent venue WebSocket connection. on_frame() belongs to
// that venue's IO thread; drain_into() belongs to the single ExternalAssetState
// writer. No locks, allocation or persistence are introduced after parsing.
class ExternalVenueIngress final {
public:
    ExternalVenueIngress(VenueId venue, std::uint64_t asset_handle) noexcept;

    [[nodiscard]] ExternalDecodeResult on_frame(
        std::uint64_t connection_epoch,
        std::int64_t local_receive_monotonic_ns,
        std::int64_t local_receive_wall_ns,
        std::string_view payload) noexcept;

    [[nodiscard]] std::size_t drain_into(
        ExternalAssetState& state,
        const ExternalStatePolicy& policy,
        std::size_t max_events = kExternalIngressQueueCapacity) noexcept;

    void mark_disconnected(std::uint64_t connection_epoch) noexcept;

    [[nodiscard]] ExternalIngressSnapshot snapshot() const noexcept;

private:
    VenueId venue_ = VenueId::Unknown;
    std::uint64_t asset_handle_ = 0;
    SpscRing<ExternalVenueEvent, kExternalIngressQueueCapacity> queue_{};
    std::atomic<std::uint64_t> frames_{0};
    std::atomic<std::uint64_t> decoded_events_{0};
    std::atomic<std::uint64_t> enqueued_events_{0};
    std::atomic<std::uint64_t> drained_events_{0};
    std::atomic<std::uint64_t> dropped_events_{0};
    std::atomic<std::uint64_t> invalid_frames_{0};
    std::atomic<std::uint64_t> reconnects_{0};
    std::atomic<std::uint64_t> propagated_gaps_{0};
    std::atomic<std::uint64_t> connection_epoch_{0};
    std::atomic<bool> healthy_{false};
    std::atomic<bool> gap_pending_{false};
};

static_assert(std::is_trivially_copyable_v<ExternalIngressSnapshot>);

} // namespace pm::v7::external_fair
