#pragma once

#include "pm/v7_crypto_decision_lane.hpp"
#include "pm/v7_event_leadlag.hpp"

#include <cstdint>
#include <type_traits>

namespace pm::v7 {

enum class LatencyArbReason : std::uint8_t {
    None = 0,
    Enter = 1,
    ExitConverged = 2,
    ExitReversal = 3,
    ExitTimeout = 4,
    PendingOrder = 5,
    InvalidSignal = 6,
    ExpiredSignal = 7,
    WeakSignal = 8,
    MarketUnavailable = 9,
    InvalidBook = 10,
    BookAlreadyRepriced = 11,
    SpreadTooWide = 12,
    InsufficientDepth = 13,
    EntryPriceTooHigh = 14,
    DuplicateSignal = 15,
    ExitNotReady = 16,
    InvalidTick = 17,
    InvalidFee = 18,
};

enum class LatencyArbAction : std::uint8_t {
    None = 0,
    Enter = 1,
    Exit = 2,
};

struct LatencyArbPolicy {
    std::int64_t minimum_tte_ns = 5'000'000'000LL;
    std::int64_t maximum_tte_ns = 120'000'000'000LL;
    std::int64_t maximum_signal_age_ns = 100'000'000LL;
    std::int64_t maximum_book_age_ns = 100'000'000LL;
    std::int64_t hard_timeout_ns = 500'000'000LL;
    std::int64_t target_quantity_microunits = 5'000'000LL;
    std::int32_t maximum_entry_price_e4 = 9'500;
    std::int32_t maximum_spread_ticks = 2;
    std::int32_t minimum_profit_ticks = 1;
    double minimum_absolute_binance_return_bp = 0.30;
    std::uint8_t require_full_visible_depth = 1;
};

struct LatencyArbInput {
    leadlag::EventLeadLagSignal signal{};
    NativeCryptoMarketContext market{};
    BookHotSnapshot yes_book{};
    BookHotSnapshot no_book{};
    std::int64_t now_monotonic_ns = 0;
    double taker_fee_rate = 0.0;
    double taker_fee_exponent = 1.0;
};

struct LatencyArbDecision {
    StrategyIntent intent{};
    LatencyArbReason reason = LatencyArbReason::None;
    LatencyArbAction action = LatencyArbAction::None;
    std::uint64_t signal_version = 0;
    std::uint64_t selected_instrument_handle = 0;
    std::int64_t decision_compute_ns = 0;
    std::int32_t entry_price_e4 = 0;
    std::int32_t target_exit_e4 = 0;
    std::int8_t direction = 0;
    std::uint8_t accepted = 0;
};

class LatencyArbLane final {
public:
    explicit LatencyArbLane(LatencyArbPolicy policy = {}) noexcept;

    [[nodiscard]] LatencyArbDecision construct_candidate(
        const LatencyArbInput& input) noexcept;

    void on_submitted(std::uint64_t client_order_id,
                      const LatencyArbDecision& decision) noexcept;
    void on_fill(std::uint64_t client_order_id,
                 std::uint64_t instrument_handle,
                 Side side,
                 std::int64_t fill_microunits,
                 std::int32_t fill_price_e4,
                 std::int64_t fill_monotonic_ns) noexcept;
    void on_terminal(std::uint64_t client_order_id) noexcept;

    [[nodiscard]] bool owns_order(std::uint64_t client_order_id) const noexcept {
        return client_order_id != 0 && client_order_id == pending_client_order_id_;
    }
    [[nodiscard]] bool position_open() const noexcept { return position_microunits_ > 0; }
    [[nodiscard]] std::int64_t position_microunits() const noexcept { return position_microunits_; }
    [[nodiscard]] std::uint64_t position_instrument() const noexcept { return position_instrument_; }

private:
    [[nodiscard]] std::int32_t target_exit_price(
        std::int32_t entry_e4, std::int32_t tick_e4,
        double fee_rate, double fee_exponent) const noexcept;

    LatencyArbPolicy policy_{};
    std::uint64_t next_intent_id_ = 1;
    std::uint64_t last_entry_signal_version_ = 0;

    std::uint64_t position_instrument_ = 0;
    std::int64_t position_microunits_ = 0;
    std::int32_t entry_price_e4_ = 0;
    std::int32_t target_exit_e4_ = 0;
    std::int32_t position_tick_e4_ = 0;
    std::int64_t entry_monotonic_ns_ = 0;
    std::int64_t entry_trigger_ns_ = 0;
    std::uint64_t entry_signal_version_ = 0;
    std::int8_t entry_direction_ = 0;

    std::uint64_t pending_client_order_id_ = 0;
    LatencyArbAction pending_action_ = LatencyArbAction::None;
    std::uint64_t pending_instrument_ = 0;
    std::int32_t pending_entry_price_e4_ = 0;
    std::int32_t pending_target_exit_e4_ = 0;
    std::int32_t pending_tick_e4_ = 0;
    std::int64_t pending_trigger_ns_ = 0;
    std::uint64_t pending_signal_version_ = 0;
    std::int8_t pending_direction_ = 0;
};

static_assert(std::is_trivially_copyable_v<LatencyArbPolicy>);
static_assert(std::is_trivially_copyable_v<LatencyArbInput>);
static_assert(std::is_trivially_copyable_v<LatencyArbDecision>);

} // namespace pm::v7
