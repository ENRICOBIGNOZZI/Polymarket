#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>

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
        refill_reciprocal_q64_ = 0;
        reciprocal_valid_ = false;
#if defined(__SIZEOF_INT128__)
        if (config.capacity > 0 && config.window_ns > 0
            && static_cast<std::uint64_t>(config.window_ns) >= config.capacity) {
            const unsigned __int128 numerator =
                static_cast<unsigned __int128>(config.capacity) << 64U;
            const unsigned __int128 reciprocal = numerator
                / static_cast<std::uint64_t>(config.window_ns);
            if (reciprocal <= std::numeric_limits<std::uint64_t>::max()) {
                refill_reciprocal_q64_ = static_cast<std::uint64_t>(reciprocal);
                reciprocal_valid_ = true;
            }
        }
#endif
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
        const auto window = static_cast<std::uint64_t>(config_.window_ns);
        if (elapsed >= window) {
            tokens_q32_ = cap_q32;
            last_ns_ = now_ns;
            return;
        }
#if defined(__SIZEOF_INT128__)
        std::uint64_t gained = 0;
        if (reciprocal_valid_ && (elapsed >> 32U) <= 64U) {
            // Exact invariant-divisor reduction. The floor reciprocal gives a
            // conservative quotient. At most ceil(elapsed/2^32) subtractive
            // corrections recover the exact Q32 quotient; for normal CLOB
            // windows this is only a handful of iterations and no hot divide.
            const unsigned __int128 product =
                static_cast<unsigned __int128>(elapsed) * refill_reciprocal_q64_;
            gained = static_cast<std::uint64_t>(product >> 32U);
            const unsigned __int128 numerator =
                (static_cast<unsigned __int128>(elapsed) * config_.capacity) << 32U;
            unsigned __int128 residual = numerator
                - static_cast<unsigned __int128>(gained) * window;
            while (residual >= window) {
                residual -= window;
                ++gained;
            }
        } else {
            const unsigned __int128 exact = static_cast<unsigned __int128>(elapsed)
                * static_cast<unsigned __int128>(cap_q32)
                / static_cast<unsigned __int128>(window);
            gained = static_cast<std::uint64_t>(exact);
        }
        tokens_q32_ = std::min(cap_q32, tokens_q32_ + gained);
#else
        const long double gained = static_cast<long double>(elapsed)
            * static_cast<long double>(cap_q32) / static_cast<long double>(window);
        tokens_q32_ = std::min(cap_q32, tokens_q32_ + static_cast<std::uint64_t>(gained));
#endif
        last_ns_ = now_ns;
    }
    RateWindowConfig config_{};
    std::uint64_t tokens_q32_ = 0;
    std::int64_t last_ns_ = 0;
    std::uint64_t refill_reciprocal_q64_ = 0;
    bool reciprocal_valid_ = false;
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
