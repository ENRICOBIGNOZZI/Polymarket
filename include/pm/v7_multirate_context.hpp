#pragma once

#include "pm/v7_spsc.hpp"
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>
#include <thread>

namespace pm::v7 {
inline constexpr std::size_t kSlowContextFields = 13;
inline constexpr std::array<std::string_view, kSlowContextFields> kSlowContextNames{
    "spot_composite", "volatility_medium", "volatility_slow", "return_5s",
    "dispersion_bps", "binance_funding", "binance_open_interest",
    "bybit_funding", "bybit_open_interest", "deribit_funding",
    "deribit_open_interest", "oracle_value", "opening_reference"};
inline constexpr std::uint32_t kSlowContextMask = (1U << kSlowContextFields) - 1;

struct SlowContextField {
    double value = std::numeric_limits<double>::quiet_NaN();
    std::int64_t receive_ns = 0;
    std::int64_t expires_ns = 0;
    std::uint64_t source_version = 0;
};
struct SlowContextSnapshot {
    std::array<SlowContextField, kSlowContextFields> fields{};
    std::uint64_t version = 0;
    std::int64_t published_ns = 0;
    std::uint32_t valid_mask = 0;
    std::uint8_t envelope_valid = 0;
};
struct SlowContextCut {
    SlowContextSnapshot snapshot{};
    std::int64_t decision_ns = 0;
    std::int64_t max_input_receive_ns = 0;
    std::uint32_t fresh_mask = 0;

    [[nodiscard]] bool satisfies(std::uint32_t required_mask) const noexcept {
        return (required_mask & ~kSlowContextMask) == 0
            && (fresh_mask & required_mask) == required_mask;
    }
};

// One native decision owner; never waits for the publisher or another venue.
class SlowContextCache final {
public:
    [[nodiscard]] bool apply(const SlowContextSnapshot& next, std::int64_t now_ns) noexcept {
        if (now_ns <= 0 || next.version <= current_.version) return false;
        if (!next.envelope_valid || next.published_ns <= 0 || next.published_ns > now_ns
            || (next.valid_mask & ~kSlowContextMask) != 0) {
            invalidate();
            current_.version = next.version;
            return false;
        }
        current_ = next;
        for (std::size_t i = 0; i < kSlowContextFields; ++i) {
            const auto& f = current_.fields[i];
            if (!std::isfinite(f.value) || f.receive_ns <= 0
                || f.receive_ns > next.published_ns || f.expires_ns < f.receive_ns
                || f.source_version == 0) current_.valid_mask &= ~(1U << i);
        }
        return true;
    }
    void invalidate() noexcept { current_.envelope_valid = 0; current_.valid_mask = 0; }
    [[nodiscard]] SlowContextCut at(std::int64_t now_ns) const noexcept {
        SlowContextCut cut;
        cut.snapshot = current_;
        cut.decision_ns = now_ns;
        if (!current_.envelope_valid || now_ns < current_.published_ns) return cut;
        for (std::size_t i = 0; i < kSlowContextFields; ++i) {
            const auto& f = current_.fields[i];
            if ((current_.valid_mask & (1U << i)) != 0
                && f.receive_ns <= now_ns && now_ns <= f.expires_ns) {
                cut.fresh_mask |= 1U << i;
                if (f.receive_ns > cut.max_input_receive_ns) cut.max_input_receive_ns = f.receive_ns;
            }
        }
        return cut;
    }
private:
    SlowContextSnapshot current_{};
};

// Bounded SPSC ownership; overflow invalidates context rather than blocking a
// fast decision or silently presenting pre-overflow data as a complete stream.
class SlowContextMailbox final {
public:
    [[nodiscard]] bool publish(const SlowContextSnapshot& value) noexcept {
        if (queue_.try_push(value)) return true;
        faults_.fetch_add(1, std::memory_order_release);
        return false;
    }
    std::size_t consume(SlowContextCache& cache, std::int64_t now_ns) noexcept {
        SlowContextSnapshot value;
        std::size_t count = 0;
        while (count < 8 && queue_.try_pop(value)) {
            (void)cache.apply(value, now_ns);
            ++count;
        }
        const auto faults = faults_.load(std::memory_order_acquire);
        if (faults != observed_faults_) {
            cache.invalidate();
            observed_faults_ = faults;
        }
        return count;
    }
    [[nodiscard]] std::uint64_t faults() const noexcept {
        return faults_.load(std::memory_order_acquire);
    }
private:
    SpscRing<SlowContextSnapshot, 8> queue_;
    std::atomic<std::uint64_t> faults_{0};
    std::uint64_t observed_faults_ = 0; // consumer-owned
};

// Pure bounded risk-off predicates; no fair model or slow state is consulted.
[[nodiscard]] inline int fresh_shock_direction(int direction, std::int64_t trigger,
    std::int64_t expires, std::int64_t now) noexcept {
    return (direction == 1 || direction == -1) && trigger > 0
        && trigger <= now && now <= expires ? direction : 0;
}
[[nodiscard]] inline bool quote_is_adverse_to_shock(bool is_yes, Side side, int direction) noexcept {
    if (direction != 1 && direction != -1) return false;
    const bool outcome_rises = is_yes == (direction > 0);
    return outcome_rises ? side == Side::Sell : side == Side::Buy;
}

struct SlowContextIdentity {
    std::string code_sha, run_id, market_id, asset, horizon;
};
// JSON is accepted only on the slow reader thread, never in the native loop.
[[nodiscard]] SlowContextSnapshot decode_slow_context(
    std::string_view bytes, const SlowContextIdentity& identity, std::int64_t now_ns);

class SlowContextFeed final {
public:
    SlowContextFeed(std::string path, SlowContextIdentity identity);
    ~SlowContextFeed();
    SlowContextFeed(const SlowContextFeed&) = delete;
    SlowContextFeed& operator=(const SlowContextFeed&) = delete;
    void start();
    void stop() noexcept;
    std::size_t consume(SlowContextCache& cache, std::int64_t now_ns) noexcept {
        return mailbox_.consume(cache, now_ns);
    }
    [[nodiscard]] std::uint64_t failures() const noexcept { return failures_.load(); }
    [[nodiscard]] std::uint64_t overflows() const noexcept { return mailbox_.faults(); }
private:
    void run() noexcept;
    std::string path_;
    SlowContextIdentity identity_;
    SlowContextMailbox mailbox_;
    std::atomic<bool> stopping_{false};
    std::atomic<std::uint64_t> failures_{0};
    std::thread thread_;
};
static_assert(std::is_trivially_copyable_v<SlowContextSnapshot>);
static_assert(std::is_trivially_copyable_v<SlowContextCut>);
} // namespace pm::v7
