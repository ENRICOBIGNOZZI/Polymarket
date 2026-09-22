#pragma once

#include "pm/v7_market_state.hpp"

#include <cstdint>
#include <limits>

namespace pm::v7::pure_arb {

inline constexpr std::int64_t kMicrounitsPerShare = 1'000'000;

struct Terms {
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    double reserve_per_share = 0.0;
};

struct SweepResult {
    std::int64_t shares_microunits = 0;
    double gross_locked_pnl = 0.0;
    double conservative_locked_pnl = 0.0;
    double yes_notional = 0.0;
    double no_notional = 0.0;
    double marginal_edge_per_share = 0.0;
    std::uint16_t yes_levels_used = 0;
    std::uint16_t no_levels_used = 0;

    [[nodiscard]] double shares() const noexcept {
        return static_cast<double>(shares_microunits)
            / static_cast<double>(kMicrounitsPerShare);
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

struct L1Evaluation {
    double buy_fee_per_share = 0.0;
    double sell_fee_per_share = 0.0;
    double buy_raw_edge_per_share = 0.0;
    double sell_raw_edge_per_share = 0.0;
    double buy_edge_per_share = 0.0;
    double sell_edge_per_share = 0.0;
    double buy_executable_shares = 0.0;
    double sell_executable_shares = 0.0;
    std::uint8_t valid = 0;
};

[[nodiscard]] double fee_per_share(
    double price, double rate, double exponent) noexcept;

[[nodiscard]] double fee_usdc(
    double shares, double price, double rate, double exponent) noexcept;

[[nodiscard]] L1Evaluation evaluate_l1(
    const BookHotSnapshot& yes,
    const BookHotSnapshot& no,
    const Terms& terms) noexcept;

[[nodiscard]] SweepResult sweep(
    const BookHotSnapshot& yes,
    const BookHotSnapshot& no,
    const Terms& terms,
    bool buy,
    std::int64_t maximum_shares_microunits =
        std::numeric_limits<std::int64_t>::max()) noexcept;

[[nodiscard]] SweepResult sweep(
    const BookDeepSnapshot& yes,
    const BookDeepSnapshot& no,
    const Terms& terms,
    bool buy,
    std::int64_t maximum_shares_microunits =
        std::numeric_limits<std::int64_t>::max()) noexcept;

} // namespace pm::v7::pure_arb
