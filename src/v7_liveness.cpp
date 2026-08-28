#include "pm/v7_liveness.hpp"

namespace pm::v7 {
namespace {

[[nodiscard]] bool stale(std::int64_t now,
                         std::int64_t last,
                         std::int64_t max_age) noexcept {
    if (now <= 0 || last <= 0 || max_age <= 0 || last > now) return true;
    return now - last > max_age;
}

} // namespace

bool LivenessPolicy::valid() const noexcept {
    return max_public_feed_age_ns > 0
        && max_private_feed_age_ns > 0
        && max_heartbeat_age_ns > 0;
}

LivenessDecision evaluate_liveness(
    const LivenessInput& input,
    const LivenessPolicy& policy) noexcept {

    LivenessDecision out;
    if (!policy.valid() || input.now_monotonic_ns <= 0) {
        out.action = LivenessAction::CancelAll;
        out.reason_mask = PublicFeedStale | ClockUnhealthy;
        return out;
    }

    if (stale(input.now_monotonic_ns,
              input.last_public_receive_ns,
              policy.max_public_feed_age_ns)) {
        out.reason_mask |= PublicFeedStale;
    }
    if (policy.private_stream_required
        && stale(input.now_monotonic_ns,
                 input.last_private_receive_ns,
                 policy.max_private_feed_age_ns)) {
        out.reason_mask |= PrivateFeedStale;
    }
    if (policy.heartbeat_required
        && stale(input.now_monotonic_ns,
                 input.last_heartbeat_ack_ns,
                 policy.max_heartbeat_age_ns)) {
        out.reason_mask |= HeartbeatStale;
    }
    if (!input.clock_healthy) out.reason_mask |= ClockUnhealthy;
    if (!input.oms_consistent) out.reason_mask |= OmsInconsistent;
    if (!input.transport_healthy) out.reason_mask |= TransportUnhealthy;
    if (policy.private_stream_required
        && (input.private_stream_state == PrivateStreamState::ReconcileRequired
            || input.private_stream_state == PrivateStreamState::Reconciling
            || input.private_stream_state == PrivateStreamState::Disconnected)) {
        out.reason_mask |= PrivateReconciliationRequired;
    }

    const std::uint32_t cancel_all_mask = PublicFeedStale | HeartbeatStale | ClockUnhealthy;
    if ((out.reason_mask & cancel_all_mask) != 0U) {
        out.action = LivenessAction::CancelAll;
        return out;
    }

    const std::uint32_t reconcile_mask = PrivateFeedStale
                                       | OmsInconsistent
                                       | PrivateReconciliationRequired;
    if ((out.reason_mask & reconcile_mask) != 0U) {
        out.action = LivenessAction::Reconcile;
        return out;
    }

    if ((out.reason_mask & TransportUnhealthy) != 0U) {
        out.action = LivenessAction::NoNewQuotes;
        return out;
    }

    out.action = LivenessAction::AllowQuotes;
    out.maker_admission = 1;
    return out;
}

} // namespace pm::v7
