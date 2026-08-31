#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

inline constexpr std::size_t kMaxSignerRateLimiters = 64;
inline constexpr std::size_t kMaxRequestsPerSignerWindow = 64;

enum class SignerRequestClass : std::uint8_t {
    NewOrder = 1,
    CancelOrHeartbeat = 2,
};

struct SignerRateLimitPolicy {
    std::int64_t rolling_window_ns = 1'000'000'000LL;
    std::uint8_t maximum_regular_requests = 10;
    std::uint8_t maximum_emergency_requests = 10;
    std::uint8_t maximum_total_requests = 20;

    [[nodiscard]] bool valid() const noexcept {
        return rolling_window_ns > 0 && maximum_regular_requests > 0
            && maximum_emergency_requests > 0
            && maximum_total_requests > 0
            && maximum_regular_requests <= kMaxRequestsPerSignerWindow
            && maximum_emergency_requests <= kMaxRequestsPerSignerWindow
            && maximum_total_requests <= kMaxRequestsPerSignerWindow
            && static_cast<unsigned>(maximum_regular_requests)
                   + static_cast<unsigned>(maximum_emergency_requests)
                   <= maximum_total_requests;
    }
};

struct SignerRateLimitSnapshot {
    std::uint64_t signer_id = 0;
    std::uint8_t registered = 0;
    std::uint8_t regular_in_window = 0;
    std::uint8_t emergency_in_window = 0;
    std::uint8_t total_in_window = 0;
    std::uint8_t fail_closed = 0;
    std::uint64_t state_version = 0;
};

// Fixed-capacity, rolling-window limiter. Each signer has an independent
// history and no allocation occurs on the hot path. New orders cannot consume
// the emergency quota, which preserves cancellation and heartbeat traffic
// during a submission burst. Any clock regression quarantines that signer.
class SignerRateLimiter final {
public:
    explicit SignerRateLimiter(SignerRateLimitPolicy policy = {}) noexcept
        : policy_(policy) {}

    [[nodiscard]] bool register_signer(std::uint64_t signer_id) noexcept {
        if (!policy_.valid() || signer_id == 0) return false;
        if (auto* existing = find(signer_id); existing != nullptr) return !existing->fail_closed;
        for (auto& entry : entries_) {
            if (!entry.registered) {
                entry = {};
                entry.signer_id = signer_id;
                entry.registered = 1;
                bump(entry);
                return true;
            }
        }
        return false;
    }

    [[nodiscard]] bool admit(std::uint64_t signer_id, SignerRequestClass request_class,
                             std::int64_t now_ns) noexcept {
        auto* entry = find(signer_id);
        if (!policy_.valid() || entry == nullptr || !entry->registered
            || entry->fail_closed || now_ns <= 0
            || (entry->last_seen_ns > 0 && now_ns < entry->last_seen_ns)) {
            if (entry != nullptr) quarantine(*entry);
            return false;
        }
        entry->last_seen_ns = now_ns;
        const Counts counts = count_active(*entry, now_ns);
        const auto total = static_cast<unsigned>(counts.regular) + counts.emergency;
        if (total >= policy_.maximum_total_requests) return false;
        if (request_class == SignerRequestClass::NewOrder) {
            if (counts.regular >= policy_.maximum_regular_requests) return false;
            append(entry->regular_ns, now_ns);
        } else {
            if (counts.emergency >= policy_.maximum_emergency_requests) return false;
            append(entry->emergency_ns, now_ns);
        }
        bump(*entry);
        return true;
    }

    [[nodiscard]] SignerRateLimitSnapshot snapshot(std::uint64_t signer_id,
                                                   std::int64_t now_ns) noexcept {
        SignerRateLimitSnapshot out;
        const auto* const_entry = find(signer_id);
        if (const_entry == nullptr || now_ns <= 0 || !policy_.valid()) return out;
        auto* entry = find(signer_id);
        if (entry->last_seen_ns > 0 && now_ns < entry->last_seen_ns) {
            quarantine(*entry);
        }
        const Counts counts = count_active(*entry, now_ns);
        out.signer_id = entry->signer_id;
        out.registered = entry->registered;
        out.regular_in_window = counts.regular;
        out.emergency_in_window = counts.emergency;
        out.total_in_window = static_cast<std::uint8_t>(counts.regular + counts.emergency);
        out.fail_closed = entry->fail_closed;
        out.state_version = entry->state_version;
        return out;
    }

private:
    struct Entry {
        std::uint64_t signer_id = 0;
        std::int64_t last_seen_ns = 0;
        std::array<std::int64_t, kMaxRequestsPerSignerWindow> regular_ns{};
        std::array<std::int64_t, kMaxRequestsPerSignerWindow> emergency_ns{};
        std::uint8_t registered = 0;
        std::uint8_t fail_closed = 0;
        std::uint8_t regular_next = 0;
        std::uint8_t emergency_next = 0;
        std::uint64_t state_version = 0;
    };
    struct Counts { std::uint8_t regular = 0; std::uint8_t emergency = 0; };

    [[nodiscard]] Entry* find(std::uint64_t signer_id) noexcept {
        for (auto& entry : entries_) if (entry.registered && entry.signer_id == signer_id) return &entry;
        return nullptr;
    }
    [[nodiscard]] const Entry* find(std::uint64_t signer_id) const noexcept {
        for (const auto& entry : entries_) if (entry.registered && entry.signer_id == signer_id) return &entry;
        return nullptr;
    }
    [[nodiscard]] std::uint8_t active(const std::array<std::int64_t, kMaxRequestsPerSignerWindow>& samples,
                                      std::int64_t now_ns) const noexcept {
        std::uint8_t count = 0;
        for (const auto sample : samples) {
            if (sample > 0 && now_ns >= sample && now_ns - sample < policy_.rolling_window_ns) ++count;
        }
        return count;
    }
    [[nodiscard]] Counts count_active(const Entry& entry, std::int64_t now_ns) const noexcept {
        return Counts{active(entry.regular_ns, now_ns), active(entry.emergency_ns, now_ns)};
    }
    static void append(std::array<std::int64_t, kMaxRequestsPerSignerWindow>& samples,
                       std::int64_t now_ns) noexcept {
        std::size_t oldest = 0;
        for (std::size_t i = 0; i < samples.size(); ++i) {
            if (samples[i] == 0) { samples[i] = now_ns; return; }
            if (samples[i] < samples[oldest]) oldest = i;
        }
        samples[oldest] = now_ns;
    }
    static void bump(Entry& entry) noexcept {
        ++entry.state_version;
        if (entry.state_version == 0) entry.state_version = 1;
    }
    static void quarantine(Entry& entry) noexcept {
        entry.fail_closed = 1;
        bump(entry);
    }

    SignerRateLimitPolicy policy_{};
    std::array<Entry, kMaxSignerRateLimiters> entries_{};
};

} // namespace pm::v7
