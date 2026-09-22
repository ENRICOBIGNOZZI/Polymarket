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
    std::int32_t yes_limit_price_e4 = 0;
    std::int32_t no_limit_price_e4 = 0;

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
        result.yes_limit_price_e4 = yes_levels[yi].price_e4;
        result.no_limit_price_e4 = no_levels[ni].price_e4;
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

enum class Direction : std::uint8_t {
    None = 0,
    BuyCompleteSet = 1,
    SellCompleteSet = 2,
};

enum class RejectReason : std::uint8_t {
    Accepted = 0,
    InvalidContext = 1,
    EpochMismatch = 2,
    LegSkewExceeded = 3,
    LineageInvalid = 4,
    BookInvalid = 5,
    FeeInvalid = 6,
    NoPositiveEdge = 7,
    BelowVenueMinimum = 8,
    AmbiguousDirection = 9,
};

struct Context {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    double reserve_per_share = 0.0005;
    std::int64_t minimum_order_microunits = 0;
    std::int64_t maximum_leg_skew_ns = 100'000'000LL;
    std::int64_t sell_capacity_microunits =
        std::numeric_limits<std::int64_t>::max();
    std::uint8_t fee_verified = 0;
    std::array<std::uint8_t, 7> reserved{};
};

struct PairInput {
    BookHotSnapshot yes{};
    BookHotSnapshot no{};
    std::uint64_t yes_connection_epoch = 0;
    std::uint64_t no_connection_epoch = 0;
    std::int64_t decision_monotonic_ns = 0;
};

struct LegPlan {
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::int64_t quantity_microunits = 0;
    std::int32_t limit_price_e4 = 0;
    Side side = Side::None;
    std::array<std::uint8_t, 3> reserved{};
};

struct PureArbExecutionPlan {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::int64_t trigger_receive_monotonic_ns = 0;
    std::int64_t decision_monotonic_ns = 0;
    LegPlan yes{};
    LegPlan no{};
    SweepResult economics{};
    Direction direction = Direction::None;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 6> reserved{};
};

struct Evaluation {
    PureArbExecutionPlan plan{};
    SweepResult buy{};
    SweepResult sell{};
    double buy_raw_edge_per_share = 0.0;
    double sell_raw_edge_per_share = 0.0;
    double buy_edge_per_share = 0.0;
    double sell_edge_per_share = 0.0;
    double buy_fee_per_share = 0.0;
    double sell_fee_per_share = 0.0;
    RejectReason reason = RejectReason::InvalidContext;
    std::uint8_t epoch_synced = 0;
    std::uint8_t leg_skew_ready = 0;
    std::uint8_t lineage_ready = 0;
    std::uint8_t book_valid = 0;
    std::uint8_t fee_finite = 0;
    std::uint8_t buy_minimum_met = 0;
    std::uint8_t sell_minimum_met = 0;
};

class PureArbLane final {
public:
    explicit PureArbLane(Context context) noexcept : context_(context) {}

    [[nodiscard]] bool valid() const noexcept;
    [[nodiscard]] const Context& context() const noexcept { return context_; }
    [[nodiscard]] Evaluation evaluate(const PairInput& input) const noexcept;

private:
    Context context_{};
};

static_assert(std::is_trivially_copyable_v<SweepResult>);
static_assert(std::is_trivially_copyable_v<Context>);
static_assert(std::is_trivially_copyable_v<PairInput>);
static_assert(std::is_trivially_copyable_v<LegPlan>);
static_assert(std::is_trivially_copyable_v<PureArbExecutionPlan>);
static_assert(std::is_trivially_copyable_v<Evaluation>);

} // namespace pm::v7::pure_arb
