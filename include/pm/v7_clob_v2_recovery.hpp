#pragma once

#include <algorithm>
#include <cstdint>

namespace pm::v7 {

// Pure V2 matching-engine restriction state.  It deliberately has no HTTP,
// signer or order-submission code: adapters report observed responses and the
// common risk plane asks whether a proposed action is admissible.
enum class ClobV2VenueMode : std::uint8_t {
    Healthy = 1,
    RestartBackoff = 2,
    PostOnlyRecovery = 3,
    CancelOnly = 4,
    ReconciliationRequired = 5,
};

enum class ClobV2Action : std::uint8_t {
    NewOrder = 1,
    Cancel = 2,
};

struct ClobV2RecoveryPolicy {
    std::int64_t initial_backoff_ns = 1'000'000'000LL;
    std::int64_t maximum_backoff_ns = 30'000'000'000LL;
    std::int64_t post_restart_post_only_ns = 120'000'000'000LL;

    [[nodiscard]] bool valid() const noexcept {
        return initial_backoff_ns > 0 && maximum_backoff_ns >= initial_backoff_ns
            && post_restart_post_only_ns > 0;
    }
};

struct ClobV2RecoverySnapshot {
    ClobV2VenueMode mode = ClobV2VenueMode::ReconciliationRequired;
    std::int64_t retry_after_ns = 0;
    std::int64_t post_only_until_ns = 0;
    std::uint32_t restart_attempts = 0;
    std::uint64_t state_version = 0;
};

class ClobV2RecoveryGate final {
public:
    explicit ClobV2RecoveryGate(ClobV2RecoveryPolicy policy = {}) noexcept
        : policy_(policy), snapshot_{} {
        if (!policy_.valid()) snapshot_.mode = ClobV2VenueMode::ReconciliationRequired;
    }

    [[nodiscard]] const ClobV2RecoverySnapshot& snapshot() const noexcept { return snapshot_; }

    // HTTP 425 is a temporary restart condition, never a terminal rejection.
    // Cancels remain admissible, while retry timing uses bounded exponential
    // backoff until the first non-425 health observation arrives.
    [[nodiscard]] bool observe_http_425(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns)) return fail_closed();
        snapshot_.mode = ClobV2VenueMode::RestartBackoff;
        const std::uint32_t exponent = std::min<std::uint32_t>(snapshot_.restart_attempts, 30U);
        std::int64_t delay = policy_.initial_backoff_ns;
        for (std::uint32_t i = 0; i < exponent && delay < policy_.maximum_backoff_ns; ++i) {
            delay = std::min(policy_.maximum_backoff_ns, delay * 2);
        }
        snapshot_.retry_after_ns = now_ns + delay;
        ++snapshot_.restart_attempts;
        ++snapshot_.state_version;
        return true;
    }

    // A successful post-restart health observation starts the documented
    // two-minute post-only period. Reconciliation is still required separately
    // if private order state was interrupted.
    [[nodiscard]] bool observe_engine_recovered(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns) || snapshot_.mode != ClobV2VenueMode::RestartBackoff) return false;
        snapshot_.mode = ClobV2VenueMode::PostOnlyRecovery;
        snapshot_.retry_after_ns = 0;
        snapshot_.post_only_until_ns = now_ns + policy_.post_restart_post_only_ns;
        snapshot_.restart_attempts = 0;
        ++snapshot_.state_version;
        return true;
    }

    // A 503 cancel-only response forbids all new orders but preserves emergency
    // cancel capacity. A finite Retry-After is accepted when supplied.
    [[nodiscard]] bool observe_cancel_only(std::int64_t now_ns,
                                           std::int64_t retry_after_ns = 0) noexcept {
        if (!valid_now(now_ns)) return fail_closed();
        snapshot_.mode = ClobV2VenueMode::CancelOnly;
        snapshot_.retry_after_ns = retry_after_ns > now_ns ? retry_after_ns : 0;
        ++snapshot_.state_version;
        return true;
    }

    [[nodiscard]] bool require_reconciliation() noexcept {
        snapshot_.mode = ClobV2VenueMode::ReconciliationRequired;
        ++snapshot_.state_version;
        return true;
    }

    [[nodiscard]] bool complete_reconciliation(std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns) || snapshot_.mode != ClobV2VenueMode::ReconciliationRequired) return false;
        snapshot_.mode = ClobV2VenueMode::Healthy;
        snapshot_.retry_after_ns = 0;
        snapshot_.post_only_until_ns = 0;
        ++snapshot_.state_version;
        return true;
    }

    [[nodiscard]] bool admit(ClobV2Action action, bool post_only,
                             std::int64_t now_ns) noexcept {
        if (!valid_now(now_ns)) return false;
        if (action == ClobV2Action::Cancel) return true;
        switch (snapshot_.mode) {
            case ClobV2VenueMode::Healthy:
                return true;
            case ClobV2VenueMode::PostOnlyRecovery:
                if (now_ns >= snapshot_.post_only_until_ns) {
                    snapshot_.mode = ClobV2VenueMode::Healthy;
                    snapshot_.post_only_until_ns = 0;
                    ++snapshot_.state_version;
                    return true;
                }
                return post_only;
            case ClobV2VenueMode::RestartBackoff:
            case ClobV2VenueMode::CancelOnly:
            case ClobV2VenueMode::ReconciliationRequired:
                return false;
        }
        return false;
    }

private:
    [[nodiscard]] bool valid_now(std::int64_t now_ns) const noexcept {
        return policy_.valid() && now_ns > 0;
    }
    [[nodiscard]] bool fail_closed() noexcept {
        snapshot_.mode = ClobV2VenueMode::ReconciliationRequired;
        ++snapshot_.state_version;
        return false;
    }

    ClobV2RecoveryPolicy policy_{};
    ClobV2RecoverySnapshot snapshot_{};
};

} // namespace pm::v7
