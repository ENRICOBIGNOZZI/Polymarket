#pragma once

#include "pm/v7_private_state.hpp"

#include <cstdint>

namespace pm::v7 {

enum class LivenessAction : std::uint8_t {
    AllowQuotes = 1,
    NoNewQuotes = 2,
    Reconcile = 3,
    CancelAll = 4,
};

enum LivenessReason : std::uint32_t {
    LivenessNone = 0,
    PublicFeedStale = 1U << 0U,
    PrivateFeedStale = 1U << 1U,
    HeartbeatStale = 1U << 2U,
    ClockUnhealthy = 1U << 3U,
    OmsInconsistent = 1U << 4U,
    TransportUnhealthy = 1U << 5U,
    PrivateReconciliationRequired = 1U << 6U,
};

struct LivenessPolicy {
    std::int64_t max_public_feed_age_ns = 2'000'000'000LL;
    std::int64_t max_private_feed_age_ns = 2'000'000'000LL;
    std::int64_t max_heartbeat_age_ns = 5'000'000'000LL;
    std::uint8_t private_stream_required = 0; // PAPER default: disabled.
    std::uint8_t heartbeat_required = 0;      // PAPER default: disabled.

    [[nodiscard]] bool valid() const noexcept;
};

struct LivenessInput {
    std::int64_t now_monotonic_ns = 0;
    std::int64_t last_public_receive_ns = 0;
    std::int64_t last_private_receive_ns = 0;
    std::int64_t last_heartbeat_ack_ns = 0;
    PrivateStreamState private_stream_state = PrivateStreamState::Disconnected;
    std::uint8_t clock_healthy = 1;
    std::uint8_t oms_consistent = 1;
    std::uint8_t transport_healthy = 1;
};

struct LivenessDecision {
    LivenessAction action = LivenessAction::CancelAll;
    std::uint32_t reason_mask = LivenessNone;
    std::uint8_t maker_admission = 0;
};

// Pure common risk-plane function: no I/O, allocation or wall-clock reads. The
// caller supplies monotonic timestamps so replay and live behavior are identical.
// Risk-off precedence is conservative: stale public market state, heartbeat loss
// or clock failure request CANCEL_ALL; uncertain private/OMS state requests a
// reconciliation freeze; transport failure forbids new quotes.
[[nodiscard]] LivenessDecision evaluate_liveness(
    const LivenessInput& input,
    const LivenessPolicy& policy) noexcept;

} // namespace pm::v7
