#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::leadlag {

inline constexpr std::size_t kEventLeadLagHistoryCapacity = 8192;

struct EventLeadLagPolicy {
    std::int64_t shock_window_ns = 100'000'000LL;
    std::int64_t cooldown_ns = 250'000'000LL;
    std::int64_t warmup_ns = 300'000'000LL;
    std::int64_t signal_ttl_ns = 100'000'000LL;
    double minimum_absolute_binance_return_bp = 0.30;
};

struct EventLeadLagSignal {
    std::uint64_t signal_version = 0;
    std::int64_t causal_trigger_receive_monotonic_ns = 0;
    std::int64_t evaluated_receive_monotonic_ns = 0;
    std::int64_t valid_until_monotonic_ns = 0;
    std::int64_t binance_prior_receive_monotonic_ns = 0;
    std::int64_t coinbase_current_receive_monotonic_ns = 0;
    std::int64_t coinbase_prior_receive_monotonic_ns = 0;
    double binance_return_100ms_bp = 0.0;
    double coinbase_return_100ms_bp = 0.0;
    std::int8_t direction = 0;
    std::uint8_t confirmed_non_opposing = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 5> reserved{};
};

struct EventLeadLagMetrics {
    std::uint64_t binance_events = 0;
    std::uint64_t coinbase_events = 0;
    std::uint64_t evaluations = 0;
    std::uint64_t triggers = 0;
    std::uint64_t invalid_events = 0;
    std::uint64_t out_of_order_events = 0;
    std::uint64_t history_overflows = 0;
    std::uint64_t warmup_or_history_rejects = 0;
    std::uint64_t weak_shock_rejects = 0;
    std::uint64_t opposing_confirmation_rejects = 0;
    std::uint64_t cooldown_rejects = 0;
};

// Zero-authority, event-driven research/shadow trigger. One owner must feed
// Binance trades and Coinbase mids in global local-receive-time order.
// Unlike the frozen V1 25ms grid, evaluation occurs on every Binance trade.
class EventLeadLagEngine final {
public:
    explicit EventLeadLagEngine(EventLeadLagPolicy policy = {}) noexcept;

    [[nodiscard]] bool on_coinbase_mid(std::int64_t receive_monotonic_ns,
                                       double mid) noexcept;
    [[nodiscard]] EventLeadLagSignal on_binance_trade(
        std::int64_t receive_monotonic_ns, double trade_price) noexcept;
    [[nodiscard]] EventLeadLagSignal snapshot(std::int64_t now_monotonic_ns) const noexcept;
    [[nodiscard]] const EventLeadLagMetrics& metrics() const noexcept { return metrics_; }
    void reset() noexcept;

private:
    struct PriceSample {
        std::int64_t receive_ns = 0;
        double price = 0.0;
    };
    struct PriceRing {
        std::array<PriceSample, kEventLeadLagHistoryCapacity> samples{};
        std::size_t head = 0;
        std::size_t count = 0;

        void clear() noexcept { head = 0; count = 0; }
        [[nodiscard]] bool push(PriceSample sample) noexcept;
        void advance_prior(std::int64_t target_ns) noexcept;
        [[nodiscard]] const PriceSample* prior_at_or_before(std::int64_t target_ns) noexcept;
        [[nodiscard]] const PriceSample* latest() const noexcept;
    };

    [[nodiscard]] bool policy_valid() const noexcept;
    [[nodiscard]] bool accept_global_time(std::int64_t receive_ns) noexcept;
    void fail_closed_recovery(std::int64_t receive_ns) noexcept;

    EventLeadLagPolicy policy_{};
    PriceRing binance_{};
    PriceRing coinbase_{};
    EventLeadLagSignal signal_{};
    EventLeadLagMetrics metrics_{};
    std::int64_t first_binance_ns_ = 0;
    std::int64_t first_coinbase_ns_ = 0;
    std::int64_t last_global_receive_ns_ = 0;
    std::int64_t last_trigger_ns_ = 0;
    std::uint64_t signal_version_counter_ = 0;
};

static_assert(std::is_trivially_copyable_v<EventLeadLagPolicy>);
static_assert(std::is_trivially_copyable_v<EventLeadLagSignal>);
static_assert(std::is_trivially_copyable_v<EventLeadLagMetrics>);

} // namespace pm::v7::leadlag
