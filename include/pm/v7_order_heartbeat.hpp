#pragma once

#include <cstdint>

namespace pm::v7 {

// The CLOB order-heartbeat control plane is deliberately independent from the
// transport adapter.  The adapter sends only when send_due() permits it and
// reports the corresponding successful acknowledgement here.  A missing or
// malformed acknowledgement transitions irreversibly to Expired, so callers
// must cancel/reconcile before any future admission.
enum class OrderHeartbeatState : std::uint8_t {
    Inactive = 1,
    AwaitingAck = 2,
    Healthy = 3,
    Expired = 4,
};

struct OrderHeartbeatPolicy {
    std::int64_t interval_ns = 5'000'000'000LL;
    std::int64_t max_ack_age_ns = 10'000'000'000LL;

    [[nodiscard]] bool valid() const noexcept {
        return interval_ns > 0 && max_ack_age_ns >= interval_ns;
    }
};

struct OrderHeartbeatSnapshot {
    OrderHeartbeatState state = OrderHeartbeatState::Inactive;
    std::int64_t activated_ns = 0;
    std::int64_t last_send_ns = 0;
    std::int64_t last_ack_ns = 0;
    std::int64_t next_send_ns = 0;
    std::uint64_t state_version = 0;
};

class OrderHeartbeatGate final {
public:
    explicit OrderHeartbeatGate(OrderHeartbeatPolicy policy = {}) noexcept
        : policy_(policy) {}

    [[nodiscard]] const OrderHeartbeatSnapshot& snapshot() const noexcept {
        return snapshot_;
    }

    // Call exactly when the venue heartbeat requirement becomes active for an
    // account. The first heartbeat is immediately due, but loss of a valid ack
    // is terminal after max_ack_age_ns.
    [[nodiscard]] bool activate(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns) || snapshot_.state != OrderHeartbeatState::Inactive) {
            return fail_closed();
        }
        snapshot_.state = OrderHeartbeatState::AwaitingAck;
        snapshot_.activated_ns = now_ns;
        snapshot_.next_send_ns = now_ns;
        bump_version();
        return true;
    }

    [[nodiscard]] bool deactivate() noexcept {
        snapshot_ = {};
        bump_version();
        return true;
    }

    // Evaluating at a non-monotonic timestamp or after the deadline fails
    // closed. It is safe to call before every new order and scheduler tick.
    [[nodiscard]] bool send_due(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns)) return fail_closed();
        if (snapshot_.state == OrderHeartbeatState::Inactive) return false;
        if (snapshot_.state == OrderHeartbeatState::Expired) return false;
        if (deadline_elapsed(now_ns)) return fail_closed();
        return now_ns >= snapshot_.next_send_ns;
    }

    // The adapter must invoke this only after it has written one heartbeat to
    // the wire. Duplicate sends before next_send_ns are rejected, not silently
    // accepted, so an adapter cannot manufacture liveness locally.
    [[nodiscard]] bool record_send(std::int64_t now_ns) noexcept {
        if (!send_due(now_ns)) return false;
        snapshot_.last_send_ns = now_ns;
        snapshot_.next_send_ns = now_ns + policy_.interval_ns;
        bump_version();
        return true;
    }

    // Acknowledge only a heartbeat that was actually sent during this active
    // epoch. Timestamp regressions, acks before a send, and late acks expire
    // the gate rather than restoring admission.
    [[nodiscard]] bool record_ack(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns) || snapshot_.state == OrderHeartbeatState::Inactive
            || snapshot_.state == OrderHeartbeatState::Expired
            || snapshot_.last_send_ns <= 0 || now_ns < snapshot_.last_send_ns
            || now_ns < snapshot_.last_ack_ns || deadline_elapsed(now_ns)) {
            return fail_closed();
        }
        snapshot_.state = OrderHeartbeatState::Healthy;
        snapshot_.last_ack_ns = now_ns;
        bump_version();
        return true;
    }

    [[nodiscard]] bool healthy(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns)) return fail_closed();
        if (snapshot_.state == OrderHeartbeatState::Inactive) return true;
        if (snapshot_.state == OrderHeartbeatState::Expired || deadline_elapsed(now_ns)) {
            return fail_closed();
        }
        return snapshot_.state == OrderHeartbeatState::Healthy;
    }

private:
    [[nodiscard]] bool valid_now(std::int64_t now_ns) const noexcept {
        return policy_.valid() && now_ns > 0;
    }

    [[nodiscard]] bool deadline_elapsed(std::int64_t now_ns) const noexcept {
        const auto reference_ns = snapshot_.last_ack_ns > 0
            ? snapshot_.last_ack_ns : snapshot_.activated_ns;
        return reference_ns <= 0 || now_ns - reference_ns >= policy_.max_ack_age_ns;
    }

    [[nodiscard]] bool fail_closed() noexcept {
        if (snapshot_.state != OrderHeartbeatState::Expired) {
            snapshot_.state = OrderHeartbeatState::Expired;
            bump_version();
        }
        return false;
    }

    void bump_version() noexcept {
        ++snapshot_.state_version;
        if (snapshot_.state_version == 0) snapshot_.state_version = 1;
    }

    OrderHeartbeatPolicy policy_{};
    OrderHeartbeatSnapshot snapshot_{};
};

} // namespace pm::v7
