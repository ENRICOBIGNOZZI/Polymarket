#pragma once

#include "pm/v7_external_ingress.hpp"

#include <atomic>
#include <cstdint>
#include <string>
#include <string_view>
#include <type_traits>
#include <version>

#if !defined(__APPLE__) && defined(__cpp_lib_jthread) && __cpp_lib_jthread >= 201911L
#include <stop_token>
#define PM_V7_EXTERNAL_USE_STD_STOP_TOKEN 1
#else
#define PM_V7_EXTERNAL_USE_STD_STOP_TOKEN 0
#endif

namespace pm::v7::external_fair {

#if PM_V7_EXTERNAL_USE_STD_STOP_TOKEN
using ExternalStopToken = std::stop_token;
#else
// Apple's deployed libc++ does not expose std::stop_token even in C++20 mode.
// Keep cancellation explicit and allocation-free instead of depending on a
// transitive or unavailable standard-library implementation.
class ExternalStopToken final {
public:
    explicit ExternalStopToken(const std::atomic<bool>& requested) noexcept
        : requested_(&requested) {}

    [[nodiscard]] bool stop_requested() const noexcept {
        return requested_->load(std::memory_order_relaxed);
    }

private:
    const std::atomic<bool>* requested_;
};
#endif

struct ExternalVenueConnectionSpec {
    VenueId venue = VenueId::Unknown;
    std::string host;
    std::string port = "443";
    std::string target;
    std::string subscription_json;
    std::string symbol;
    std::uint64_t asset_handle = 0;
    std::size_t max_message_bytes = 1U << 20;
};

struct ExternalWsSnapshot {
    std::uint64_t connection_epoch = 0;
    std::uint64_t connection_attempts = 0;
    std::uint64_t successful_connections = 0;
    std::uint64_t frames_received = 0;
    std::uint64_t transport_failures = 0;
    std::uint64_t decode_failures = 0;
    std::int64_t last_receive_monotonic_ns = 0;
    std::int64_t last_receive_wall_ns = 0;
    std::uint8_t connected = 0;
    std::uint8_t healthy = 0;
    std::array<std::uint8_t, 6> reserved{};
};

[[nodiscard]] ExternalVenueConnectionSpec btc_spot_connection_spec(
    VenueId venue,
    std::uint64_t asset_handle);

// IO-plane only. Persistent TLS WebSocket session with bounded message memory.
// It owns no strategy, OMS, inventory, capital or execution authority. Received
// frames are normalized immediately into ExternalVenueIngress and then passed
// through the existing single-writer ExternalAssetState handoff.
class ExternalVenueWsClient final {
public:
    ExternalVenueWsClient(ExternalVenueConnectionSpec spec,
                          ExternalVenueIngress& ingress);

    ExternalVenueWsClient(const ExternalVenueWsClient&) = delete;
    ExternalVenueWsClient& operator=(const ExternalVenueWsClient&) = delete;

    // Blocking IO-loop intended for one dedicated jthread per venue. Returns
    // when stop is requested. Transport failures reconnect with bounded backoff
    // and increment connection_epoch so continuity loss is observable.
    void run(ExternalStopToken stop) noexcept;

    [[nodiscard]] ExternalWsSnapshot snapshot() const noexcept;

private:
    ExternalVenueConnectionSpec spec_;
    ExternalVenueIngress& ingress_;
    std::atomic<std::uint64_t> connection_epoch_{0};
    std::atomic<std::uint64_t> connection_attempts_{0};
    std::atomic<std::uint64_t> successful_connections_{0};
    std::atomic<std::uint64_t> frames_received_{0};
    std::atomic<std::uint64_t> transport_failures_{0};
    std::atomic<std::uint64_t> decode_failures_{0};
    std::atomic<std::int64_t> last_receive_monotonic_ns_{0};
    std::atomic<std::int64_t> last_receive_wall_ns_{0};
    std::atomic<bool> connected_{false};
    std::atomic<bool> healthy_{false};
};

static_assert(std::is_trivially_copyable_v<ExternalWsSnapshot>);

} // namespace pm::v7::external_fair

#undef PM_V7_EXTERNAL_USE_STD_STOP_TOKEN
