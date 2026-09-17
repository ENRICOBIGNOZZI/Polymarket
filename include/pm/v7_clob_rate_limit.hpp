#pragma once
#include <algorithm>
#include <cstdint>

namespace pm::v7::clob {
struct RateWindowConfig {
    std::uint32_t capacity = 0;
    std::int64_t window_ns = 0;
};
struct LaneRateConfig {
    RateWindowConfig burst{};
    RateWindowConfig sustained{};
};
enum class RateLane : std::uint8_t { Order = 0, Cancel = 1 };

class FixedTokenBucket final {
public:
    FixedTokenBucket() noexcept = default;
    explicit FixedTokenBucket(RateWindowConfig config) noexcept { reset(config, 0); }
    void reset(RateWindowConfig config, std::int64_t now_ns) noexcept {
        config_ = config;
        tokens_q32_ = static_cast<std::uint64_t>(config.capacity) << 32U;
        last_ns_ = now_ns;
    }
    [[nodiscard]] bool try_acquire(std::int64_t now_ns, std::uint32_t units = 1) noexcept {
        if (!valid() || units == 0 || units > config_.capacity || now_ns < last_ns_) return false;
        refill(now_ns);
        const std::uint64_t cost = static_cast<std::uint64_t>(units) << 32U;
        if (tokens_q32_ < cost) return false;
        tokens_q32_ -= cost;
        return true;
    }
    [[nodiscard]] bool valid() const noexcept {
        return config_.capacity > 0 && config_.window_ns > 0;
    }
private:
    void refill(std::int64_t now_ns) noexcept {
        if (now_ns <= last_ns_) return;
        const auto elapsed = static_cast<std::uint64_t>(now_ns - last_ns_);
        const auto cap_q32 = static_cast<std::uint64_t>(config_.capacity) << 32U;
#if defined(__SIZEOF_INT128__)
        const unsigned __int128 gained = static_cast<unsigned __int128>(elapsed)
            * static_cast<unsigned __int128>(cap_q32)
            / static_cast<unsigned __int128>(config_.window_ns);
        tokens_q32_ = static_cast<std::uint64_t>(std::min<unsigned __int128>(
            static_cast<unsigned __int128>(cap_q32),
            static_cast<unsigned __int128>(tokens_q32_) + gained));
#else
        const long double gained = static_cast<long double>(elapsed)
            * static_cast<long double>(cap_q32) / static_cast<long double>(config_.window_ns);
        tokens_q32_ = std::min(cap_q32, tokens_q32_ + static_cast<std::uint64_t>(gained));
#endif
        last_ns_ = now_ns;
    }
    RateWindowConfig config_{};
    std::uint64_t tokens_q32_ = 0;
    std::int64_t last_ns_ = 0;
};

class LaneLimiter final {
public:
    LaneLimiter() noexcept = default;
    explicit LaneLimiter(LaneRateConfig config) noexcept
        : burst_(config.burst), sustained_(config.sustained) {}
    void reset(LaneRateConfig config, std::int64_t now_ns) noexcept {
        burst_.reset(config.burst, now_ns);
        sustained_.reset(config.sustained, now_ns);
    }
    [[nodiscard]] bool try_acquire(std::int64_t now_ns, std::uint32_t units = 1) noexcept {
        auto burst = burst_;
        auto sustained = sustained_;
        if (!burst.try_acquire(now_ns, units) || !sustained.try_acquire(now_ns, units)) return false;
        burst_ = burst;
        sustained_ = sustained;
        return true;
    }
private:
    FixedTokenBucket burst_{};
    FixedTokenBucket sustained_{};
};

// Orders and cancels never consume the same bucket. Exhausting order capacity
// therefore cannot delay a risk-reducing cancellation locally.
class ClobRateLimiter final {
public:
    ClobRateLimiter(LaneRateConfig order, LaneRateConfig cancel) noexcept
        : order_(order), cancel_(cancel) {}
    void reset(LaneRateConfig order, LaneRateConfig cancel, std::int64_t now_ns) noexcept {
        order_.reset(order, now_ns);
        cancel_.reset(cancel, now_ns);
    }
    [[nodiscard]] bool try_acquire(RateLane lane, std::int64_t now_ns,
                                   std::uint32_t units = 1) noexcept {
        return lane == RateLane::Cancel
            ? cancel_.try_acquire(now_ns, units)
            : order_.try_acquire(now_ns, units);
    }
private:
    LaneLimiter order_{};
    LaneLimiter cancel_{};
};
} // namespace pm::v7::clob
