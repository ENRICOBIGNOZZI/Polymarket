#include "pm/v7_pure_arb_lane.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>

namespace pm::v7::pure_arb {
namespace {

[[nodiscard]] double e4_price(std::int32_t value) noexcept {
    return static_cast<double>(value) / 10'000.0;
}

[[nodiscard]] double micro_shares(std::int64_t value) noexcept {
    return static_cast<double>(value)
        / static_cast<double>(kMicrounitsPerShare);
}

template <std::size_t N>
[[nodiscard]] SweepResult sweep_levels(
    const std::array<PriceLevelE4, N>& yes_levels,
    std::size_t yes_count,
    const std::array<PriceLevelE4, N>& no_levels,
    std::size_t no_count,
    const Terms& terms,
    bool buy,
    std::int64_t maximum_shares_microunits) noexcept {
    SweepResult result{};
    std::size_t yi = 0, ni = 0;
    std::int64_t yes_remaining = 0, no_remaining = 0;
    std::int64_t capacity_remaining =
        std::max<std::int64_t>(0, maximum_shares_microunits);

    while (yi < yes_count && ni < no_count && capacity_remaining > 0) {
        if (yes_remaining <= 0) yes_remaining = yes_levels[yi].quantity_microunits;
        if (no_remaining <= 0) no_remaining = no_levels[ni].quantity_microunits;
        if (yes_remaining <= 0) { ++yi; continue; }
        if (no_remaining <= 0) { ++ni; continue; }

        const double yes_price = e4_price(yes_levels[yi].price_e4);
        const double no_price = e4_price(no_levels[ni].price_e4);
        const auto quantity =
            std::min({yes_remaining, no_remaining, capacity_remaining});
        if (quantity <= 0) break;

        const double shares = micro_shares(quantity);
        const double fee_total =
            fee_usdc(shares, yes_price, terms.fee_rate, terms.fee_exponent)
            + fee_usdc(shares, no_price, terms.fee_rate, terms.fee_exponent);
        if (!std::isfinite(fee_total)) break;

        const double fee = fee_total / shares;
        const double gross_edge = buy
            ? 1.0 - yes_price - no_price - fee
            : yes_price + no_price - 1.0 - fee;
        if (!(gross_edge > terms.reserve_per_share + 1e-12)) break;

        result.shares_microunits += quantity;
        result.gross_locked_pnl += shares * gross_edge;
        result.conservative_locked_pnl +=
            shares * (gross_edge - terms.reserve_per_share);
        result.yes_notional += shares * yes_price;
        result.no_notional += shares * no_price;
        result.marginal_edge_per_share = gross_edge;
        result.yes_levels_used = static_cast<std::uint16_t>(
            std::min<std::size_t>(
                std::numeric_limits<std::uint16_t>::max(),
                std::max<std::size_t>(result.yes_levels_used, yi + 1)));
        result.no_levels_used = static_cast<std::uint16_t>(
            std::min<std::size_t>(
                std::numeric_limits<std::uint16_t>::max(),
                std::max<std::size_t>(result.no_levels_used, ni + 1)));

        yes_remaining -= quantity;
        no_remaining -= quantity;
        capacity_remaining -= quantity;
        if (yes_remaining <= 0) ++yi;
        if (no_remaining <= 0) ++ni;
    }
    return result;
}

} // namespace

double fee_per_share(double price, double rate, double exponent) noexcept {
    if (!std::isfinite(price) || price <= 0.0 || price >= 1.0
        || !std::isfinite(rate) || rate < 0.0 || rate > 1.0
        || !std::isfinite(exponent) || exponent < 0.0) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    return rate == 0.0
        ? 0.0
        : rate * std::pow(price * (1.0 - price), exponent);
}

double fee_usdc(
    double shares, double price, double rate, double exponent) noexcept {
    const double per_share = fee_per_share(price, rate, exponent);
    if (!std::isfinite(shares) || shares <= 0.0 || !std::isfinite(per_share)) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    const double raw = shares * per_share;
    if (raw < 0.00001 - 1e-15) return 0.0;
    return std::round(raw * 100000.0) / 100000.0;
}

L1Evaluation evaluate_l1(
    const BookHotSnapshot& yes,
    const BookHotSnapshot& no,
    const Terms& terms) noexcept {
    L1Evaluation out{};
    const bool valid =
        yes.valid != 0 && no.valid != 0
        && yes.lineage_continuous != 0 && no.lineage_continuous != 0
        && yes.best_bid_e4 > 0 && yes.best_ask_e4 > yes.best_bid_e4
        && yes.best_ask_e4 < 10'000
        && no.best_bid_e4 > 0 && no.best_ask_e4 > no.best_bid_e4
        && no.best_ask_e4 < 10'000
        && yes.best_bid_microunits > 0 && yes.best_ask_microunits > 0
        && no.best_bid_microunits > 0 && no.best_ask_microunits > 0;
    if (!valid) return out;

    const double yes_ask = e4_price(yes.best_ask_e4);
    const double no_ask = e4_price(no.best_ask_e4);
    const double yes_bid = e4_price(yes.best_bid_e4);
    const double no_bid = e4_price(no.best_bid_e4);
    const auto buy_qty =
        std::min(yes.best_ask_microunits, no.best_ask_microunits);
    const auto sell_qty =
        std::min(yes.best_bid_microunits, no.best_bid_microunits);
    out.buy_executable_shares = micro_shares(buy_qty);
    out.sell_executable_shares = micro_shares(sell_qty);
    if (!(out.buy_executable_shares > 0.0)
        || !(out.sell_executable_shares > 0.0)) {
        return out;
    }

    out.buy_fee_per_share = (
        fee_usdc(out.buy_executable_shares, yes_ask,
                 terms.fee_rate, terms.fee_exponent)
        + fee_usdc(out.buy_executable_shares, no_ask,
                   terms.fee_rate, terms.fee_exponent))
        / out.buy_executable_shares;
    out.sell_fee_per_share = (
        fee_usdc(out.sell_executable_shares, yes_bid,
                 terms.fee_rate, terms.fee_exponent)
        + fee_usdc(out.sell_executable_shares, no_bid,
                   terms.fee_rate, terms.fee_exponent))
        / out.sell_executable_shares;
    if (!std::isfinite(out.buy_fee_per_share)
        || !std::isfinite(out.sell_fee_per_share)) {
        return L1Evaluation{};
    }

    out.buy_raw_edge_per_share = 1.0 - yes_ask - no_ask;
    out.sell_raw_edge_per_share = yes_bid + no_bid - 1.0;
    out.buy_edge_per_share =
        out.buy_raw_edge_per_share - out.buy_fee_per_share;
    out.sell_edge_per_share =
        out.sell_raw_edge_per_share - out.sell_fee_per_share;
    out.valid = 1;
    return out;
}

SweepResult sweep(
    const BookHotSnapshot& yes,
    const BookHotSnapshot& no,
    const Terms& terms,
    bool buy,
    std::int64_t maximum_shares_microunits) noexcept {
    return buy
        ? sweep_levels(
            yes.ask_levels, yes.ask_level_count,
            no.ask_levels, no.ask_level_count,
            terms, true, maximum_shares_microunits)
        : sweep_levels(
            yes.bid_levels, yes.bid_level_count,
            no.bid_levels, no.bid_level_count,
            terms, false, maximum_shares_microunits);
}

SweepResult sweep(
    const BookDeepSnapshot& yes,
    const BookDeepSnapshot& no,
    const Terms& terms,
    bool buy,
    std::int64_t maximum_shares_microunits) noexcept {
    return buy
        ? sweep_levels(
            yes.ask_levels, yes.ask_level_count,
            no.ask_levels, no.ask_level_count,
            terms, true, maximum_shares_microunits)
        : sweep_levels(
            yes.bid_levels, yes.bid_level_count,
            no.bid_levels, no.bid_level_count,
            terms, false, maximum_shares_microunits);
}

} // namespace pm::v7::pure_arb
