#pragma once

#include "pm/v7_market_state.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <type_traits>

namespace pm::v7::pure_arb {

inline constexpr double kPriceScaleE4 = 10'000.0;
inline constexpr double kMicrounitsPerShare = 1'000'000.0;

struct SweepResult {
    std::int64_t shares_microunits = 0;
    double gross_locked_pnl = 0.0;
    double conservative_locked_pnl = 0.0;
    double yes_notional = 0.0;
    double no_notional = 0.0;
    double marginal_edge_per_share = 0.0;
    std::uint16_t yes_levels_used = 0;
    std::uint16_t no_levels_used = 0;
    std::int32_t yes_limit_e4 = 0;
    std::int32_t no_limit_e4 = 0;

    [[nodiscard]] double shares() const noexcept {
        return static_cast<double>(std::max<std::int64_t>(0, shares_microunits))
            / kMicrounitsPerShare;
    }
    [[nodiscard]] double gross_edge_per_share() const noexcept {
        const double q = shares();
        return q > 0.0 ? gross_locked_pnl / q : 0.0;
    }
    [[nodiscard]] double conservative_edge_per_share() const noexcept {
        const double q = shares();
        return q > 0.0 ? conservative_locked_pnl / q : 0.0;
    }
    [[nodiscard]] double yes_vwap() const noexcept {
        const double q = shares();
        return q > 0.0 ? yes_notional / q : 0.0;
    }
    [[nodiscard]] double no_vwap() const noexcept {
        const double q = shares();
        return q > 0.0 ? no_notional / q : 0.0;
    }
};

[[nodiscard]] inline double price(std::int32_t value_e4) noexcept {
    return static_cast<double>(value_e4) / kPriceScaleE4;
}

[[nodiscard]] inline double fee_per_share(
    double price_value, double rate, double exponent) noexcept {
    if (!std::isfinite(price_value) || price_value <= 0.0 || price_value >= 1.0
        || !std::isfinite(rate) || rate < 0.0 || rate > 1.0
        || !std::isfinite(exponent) || exponent < 0.0) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    return rate == 0.0
        ? 0.0
        : rate * std::pow(price_value * (1.0 - price_value), exponent);
}

[[nodiscard]] inline double fee_usdc(
    double shares, double price_value, double rate, double exponent) noexcept {
    const double per_share = fee_per_share(price_value, rate, exponent);
    if (!std::isfinite(shares) || shares <= 0.0 || !std::isfinite(per_share)) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    const double raw = shares * per_share;
    if (raw < 0.00001 - 1e-15) return 0.0;
    return std::round(raw * 100000.0) / 100000.0;
}

template <std::size_t N>
[[nodiscard]] inline SweepResult sweep_levels(
    const std::array<PriceLevelE4, N>& yes_levels,
    std::size_t yes_count,
    const std::array<PriceLevelE4, N>& no_levels,
    std::size_t no_count,
    double fee_rate,
    double fee_exponent,
    double reserve_per_share,
    bool buy,
    std::int64_t maximum_shares_microunits =
        std::numeric_limits<std::int64_t>::max()) noexcept {
    SweepResult result{};
    if (!std::isfinite(reserve_per_share) || reserve_per_share < 0.0
        || maximum_shares_microunits <= 0) {
        return result;
    }

    std::size_t yi = 0, ni = 0;
    std::int64_t yes_remaining = 0, no_remaining = 0;
    std::int64_t capacity_remaining =
        std::max<std::int64_t>(0, maximum_shares_microunits);

    while (yi < yes_count && ni < no_count && capacity_remaining > 0) {
        if (yes_remaining <= 0) yes_remaining = yes_levels[yi].quantity_microunits;
        if (no_remaining <= 0) no_remaining = no_levels[ni].quantity_microunits;
        if (yes_remaining <= 0) { ++yi; continue; }
        if (no_remaining <= 0) { ++ni; continue; }

        const double yes_price = price(yes_levels[yi].price_e4);
        const double no_price = price(no_levels[ni].price_e4);
        const auto quantity = std::min({yes_remaining, no_remaining, capacity_remaining});
        if (quantity <= 0) break;
        const double shares = static_cast<double>(quantity) / kMicrounitsPerShare;
        const double fee_total =
            fee_usdc(shares, yes_price, fee_rate, fee_exponent)
            + fee_usdc(shares, no_price, fee_rate, fee_exponent);
        if (!std::isfinite(fee_total)) break;
        const double fee = fee_total / shares;

        const double gross_edge = buy
            ? 1.0 - yes_price - no_price - fee
            : yes_price + no_price - 1.0 - fee;
        if (!(gross_edge > reserve_per_share + 1e-12)) break;

        result.shares_microunits += quantity;
        result.gross_locked_pnl += shares * gross_edge;
        result.conservative_locked_pnl += shares * (gross_edge - reserve_per_share);
        result.yes_notional += shares * yes_price;
        result.no_notional += shares * no_price;
        result.marginal_edge_per_share = gross_edge;
        result.yes_limit_e4 = yes_levels[yi].price_e4;
        result.no_limit_e4 = no_levels[ni].price_e4;
        result.yes_levels_used = static_cast<std::uint16_t>(
            std::min<std::size_t>(std::numeric_limits<std::uint16_t>::max(),
                                  std::max<std::size_t>(result.yes_levels_used, yi + 1)));
        result.no_levels_used = static_cast<std::uint16_t>(
            std::min<std::size_t>(std::numeric_limits<std::uint16_t>::max(),
                                  std::max<std::size_t>(result.no_levels_used, ni + 1)));

        yes_remaining -= quantity;
        no_remaining -= quantity;
        capacity_remaining -= quantity;
        if (yes_remaining <= 0) ++yi;
        if (no_remaining <= 0) ++ni;
    }
    return result;
}

[[nodiscard]] inline SweepResult sweep(
    const BookHotSnapshot& yes,
    const BookHotSnapshot& no,
    double fee_rate,
    double fee_exponent,
    double reserve_per_share,
    bool buy,
    std::int64_t maximum_shares_microunits =
        std::numeric_limits<std::int64_t>::max()) noexcept {
    return buy
        ? sweep_levels(
            yes.ask_levels, yes.ask_level_count,
            no.ask_levels, no.ask_level_count,
            fee_rate, fee_exponent, reserve_per_share, true,
            maximum_shares_microunits)
        : sweep_levels(
            yes.bid_levels, yes.bid_level_count,
            no.bid_levels, no.bid_level_count,
            fee_rate, fee_exponent, reserve_per_share, false,
            maximum_shares_microunits);
}

[[nodiscard]] inline SweepResult sweep(
    const BookDeepSnapshot& yes,
    const BookDeepSnapshot& no,
    double fee_rate,
    double fee_exponent,
    double reserve_per_share,
    bool buy,
    std::int64_t maximum_shares_microunits =
        std::numeric_limits<std::int64_t>::max()) noexcept {
    return buy
        ? sweep_levels(
            yes.ask_levels, yes.ask_level_count,
            no.ask_levels, no.ask_level_count,
            fee_rate, fee_exponent, reserve_per_share, true,
            maximum_shares_microunits)
        : sweep_levels(
            yes.bid_levels, yes.bid_level_count,
            no.bid_levels, no.bid_level_count,
            fee_rate, fee_exponent, reserve_per_share, false,
            maximum_shares_microunits);
}


enum class DecisionReason : std::uint8_t {
    Accepted = 1,
    InvalidInput = 2,
    OutsideMarketWindow = 3,
    FeeUnverified = 4,
    EpochMismatch = 5,
    LegSkewExceeded = 6,
    LineageInvalid = 7,
    BookInvalid = 8,
    NoPositiveEdge = 9,
    BelowVenueMinimum = 10,
    SellInventoryUnavailable = 11,
};

enum class Direction : std::uint8_t {
    None = 0,
    BuyCompleteSet = 1,
    SellCompleteSet = 2,
};

struct PairInput {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    std::uint64_t yes_epoch = 0;
    std::uint64_t no_epoch = 0;
    std::int64_t market_start_wall_ms = 0;
    std::int64_t market_end_wall_ms = 0;
    std::int64_t now_wall_ms = 0;
    std::int64_t trigger_receive_monotonic_ns = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::int64_t maximum_leg_skew_ns = 100'000'000LL;
    std::int64_t minimum_order_microunits = 0;
    std::int64_t sell_available_microunits =
        std::numeric_limits<std::int64_t>::max();
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    double reserve_per_share = 0.0;
    std::uint8_t fee_verified = 0;
    BookHotSnapshot yes{};
    BookHotSnapshot no{};
};

struct LegPlan {
    std::uint64_t instrument_handle = 0;
    std::uint64_t market_state_version = 0;
    std::int64_t quantity_microunits = 0;
    std::int32_t limit_price_e4 = 0;
    std::int32_t tick_size_e4 = 0;
    Side side = Side::None;
};

struct PureArbExecutionPlan {
    Direction direction = Direction::None;
    DecisionReason reason = DecisionReason::InvalidInput;
    LegPlan yes{};
    LegPlan no{};
    SweepResult economics{};
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::int64_t trigger_receive_monotonic_ns = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::uint8_t accepted = 0;
};

[[nodiscard]] inline bool executable_book(const BookHotSnapshot& book) noexcept {
    return book.valid != 0 && book.lineage_continuous != 0
        && book.tick_size_e4 > 0 && book.tick_size_e4 < 10'000
        && book.best_bid_e4 > 0 && book.best_ask_e4 > book.best_bid_e4
        && book.best_ask_e4 < 10'000
        && book.best_bid_microunits > 0 && book.best_ask_microunits > 0
        && book.bid_level_count > 0 && book.ask_level_count > 0;
}

[[nodiscard]] inline PureArbExecutionPlan evaluate_pair(
    const PairInput& input) noexcept {
    PureArbExecutionPlan out{};
    out.market_handle = input.market_handle;
    out.event_handle = input.event_handle;
    out.trigger_receive_monotonic_ns = input.trigger_receive_monotonic_ns;
    out.decision_monotonic_ns = input.decision_monotonic_ns;

    if (input.market_handle == 0 || input.yes_instrument_handle == 0
        || input.no_instrument_handle == 0
        || input.minimum_order_microunits <= 0
        || input.maximum_leg_skew_ns <= 0
        || input.trigger_receive_monotonic_ns <= 0
        || input.decision_monotonic_ns < input.trigger_receive_monotonic_ns
        || !std::isfinite(input.reserve_per_share)
        || input.reserve_per_share < 0.0) {
        out.reason = DecisionReason::InvalidInput;
        return out;
    }
    if (!(input.market_start_wall_ms <= input.now_wall_ms
          && input.now_wall_ms < input.market_end_wall_ms)) {
        out.reason = DecisionReason::OutsideMarketWindow;
        return out;
    }
    if (input.fee_verified == 0) {
        out.reason = DecisionReason::FeeUnverified;
        return out;
    }
    if (input.yes_epoch == 0 || input.yes_epoch != input.no_epoch) {
        out.reason = DecisionReason::EpochMismatch;
        return out;
    }
    if (input.yes.receive_monotonic_ns <= 0 || input.no.receive_monotonic_ns <= 0) {
        out.reason = DecisionReason::InvalidInput;
        return out;
    }
    const auto leg_skew = input.yes.receive_monotonic_ns >= input.no.receive_monotonic_ns
        ? input.yes.receive_monotonic_ns - input.no.receive_monotonic_ns
        : input.no.receive_monotonic_ns - input.yes.receive_monotonic_ns;
    if (leg_skew > input.maximum_leg_skew_ns) {
        out.reason = DecisionReason::LegSkewExceeded;
        return out;
    }
    if (input.yes.lineage_continuous == 0 || input.no.lineage_continuous == 0) {
        out.reason = DecisionReason::LineageInvalid;
        return out;
    }
    if (!executable_book(input.yes) || !executable_book(input.no)) {
        out.reason = DecisionReason::BookInvalid;
        return out;
    }

    const auto buy = sweep(
        input.yes, input.no, input.fee_rate, input.fee_exponent,
        input.reserve_per_share, true);
    const auto sell = sweep(
        input.yes, input.no, input.fee_rate, input.fee_exponent,
        input.reserve_per_share, false,
        std::max<std::int64_t>(0, input.sell_available_microunits));

    const SweepResult* selected = nullptr;
    Direction direction = Direction::None;
    Side side = Side::None;
    if (buy.shares_microunits > 0) {
        selected = &buy;
        direction = Direction::BuyCompleteSet;
        side = Side::Buy;
    } else if (sell.shares_microunits > 0) {
        selected = &sell;
        direction = Direction::SellCompleteSet;
        side = Side::Sell;
    } else {
        out.reason = input.sell_available_microunits < input.minimum_order_microunits
            ? DecisionReason::SellInventoryUnavailable
            : DecisionReason::NoPositiveEdge;
        return out;
    }
    if (selected->shares_microunits < input.minimum_order_microunits) {
        out.reason = DecisionReason::BelowVenueMinimum;
        return out;
    }
    if (direction == Direction::SellCompleteSet
        && input.sell_available_microunits < selected->shares_microunits) {
        out.reason = DecisionReason::SellInventoryUnavailable;
        return out;
    }

    out.direction = direction;
    out.reason = DecisionReason::Accepted;
    out.economics = *selected;
    out.yes = LegPlan{
        input.yes_instrument_handle,
        input.yes.state_version,
        selected->shares_microunits,
        selected->yes_limit_e4,
        input.yes.tick_size_e4,
        side};
    out.no = LegPlan{
        input.no_instrument_handle,
        input.no.state_version,
        selected->shares_microunits,
        selected->no_limit_e4,
        input.no.tick_size_e4,
        side};
    out.accepted = 1;
    return out;
}

static_assert(std::is_trivially_copyable_v<PairInput>);
static_assert(std::is_trivially_copyable_v<LegPlan>);
static_assert(std::is_trivially_copyable_v<PureArbExecutionPlan>);

static_assert(std::is_trivially_copyable_v<SweepResult>);

} // namespace pm::v7::pure_arb
