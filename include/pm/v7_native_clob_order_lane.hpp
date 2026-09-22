#pragma once

#include "pm/v7_native_order_tx.hpp"
#include "pm/v7_clob_rate_limit.hpp"
#include "pm/v7_user_oms_bridge.hpp"

#include <cstdint>
#include <memory>
#include <span>
#include <string_view>

namespace pm::v7 {

class NativeSettlementOmsEndpoint;

enum class NativeClobSubmitReason : std::uint8_t {
    Accepted = 1,
    InvalidConfiguration = 2,
    InvalidCommand = 3,
    PreWireFailure = 4,
    TransportFailure = 5,
    ResponseFailure = 6,
    AckFailure = 7,
    OmsFailure = 8,
    MatchingEngineRestart = 9,
    RestrictedTradingMode = 10,
    RateLimited = 11,
    RateLimitBudgetExhausted = 12,
};

struct NativeClobLaneConfig {
    std::uint64_t chain_id = 0;
    std::string_view exchange_contract;
    std::string_view deposit_wallet;
    std::string_view signer_eoa_address;
    std::string_view token_id_decimal;
    std::string_view metadata_hex;
    std::string_view builder_hex;
    std::string_view api_key;
    std::string_view passphrase;
    std::string_view l2_secret_base64;
    std::string_view host = "clob.polymarket.com";
    std::uint16_t port = 443;
    int timeout_ms = 2'000;
};

struct NativeClobLatencyTrace {
    std::int64_t submit_start_monotonic_ns = 0;
    std::int64_t rate_limit_complete_monotonic_ns = 0;
    std::int64_t sign_start_monotonic_ns = 0;
    std::int64_t sign_complete_monotonic_ns = 0;
    std::int64_t frame_complete_monotonic_ns = 0;
    std::int64_t wire_start_monotonic_ns = 0;
    std::int64_t wire_complete_monotonic_ns = 0;
    std::int64_t http_ack_monotonic_ns = 0;

    [[nodiscard]] std::int64_t prewire_ns() const noexcept {
        return frame_complete_monotonic_ns >= submit_start_monotonic_ns
            ? frame_complete_monotonic_ns - submit_start_monotonic_ns : 0;
    }
    [[nodiscard]] std::int64_t signing_ns() const noexcept {
        return sign_complete_monotonic_ns >= sign_start_monotonic_ns
            ? sign_complete_monotonic_ns - sign_start_monotonic_ns : 0;
    }
};

struct NativeClobSubmitResult {
    NativeClobSubmitReason reason = NativeClobSubmitReason::InvalidConfiguration;
    OrderState final_state = OrderState::Unknown;
    std::uint64_t client_order_id = 0;
    int http_status = 0;
    int retry_after_seconds = 0;
    clob::RateTier rate_limit_tier = clob::RateTier::Unknown;
    double rate_limit_remaining = 0.0;
    std::int64_t rate_limit_reset_unix_seconds = 0;
    std::uint8_t rate_limit_warning = 0;
    std::int64_t wire_monotonic_ns = 0;
    std::int64_t response_complete_monotonic_ns = 0;
    std::uint8_t accepted = 0;
    std::uint8_t identity_bound = 0;
    NativeClobLatencyTrace latency{};
};

// Single-owner native Exchange V2 order lane. Construction/connect are cold path.
// submit() contains no Python, filesystem, REST market-data or cross-process IPC.
class NativeClobOrderLane final {
public:
    NativeClobOrderLane(const NativeClobLaneConfig& config,
                        std::span<const std::uint8_t, 32> private_key) noexcept;
    ~NativeClobOrderLane();
    NativeClobOrderLane(const NativeClobOrderLane&) = delete;
    NativeClobOrderLane& operator=(const NativeClobOrderLane&) = delete;

    [[nodiscard]] bool valid() const noexcept;
    [[nodiscard]] bool connect(std::string_view ca_file = {}) noexcept;
    void close() noexcept;
    [[nodiscard]] bool connected() const noexcept;

    // Settlement-owned path: transport cannot update OMS without its common
    // capital/inventory authority. Network permission remains external.
    [[nodiscard]] NativeClobSubmitResult submit(
        NativeSettlementOmsEndpoint& oms_owner,
        UserOmsBridge& account_bridge,
        const NativeOrderCommand& command,
        std::uint64_t wall_timestamp_ms,
        std::span<RoutedOmsEvent> routed_scratch) noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7
