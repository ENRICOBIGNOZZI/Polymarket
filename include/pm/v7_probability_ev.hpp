#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <type_traits>

namespace pm::v7 {
// Settlement probability and its model uncertainty are independent of order
// submission. Missing forecasts/costs are never a zero-cost permission to trade.
struct SettlementProbabilityForecast {
    double up = std::numeric_limits<double>::quiet_NaN();
    double lower = std::numeric_limits<double>::quiet_NaN();
    double upper = std::numeric_limits<double>::quiet_NaN();
    std::int64_t asof_ns = 0, max_input_receive_ns = 0, valid_until_ns = 0;
    std::uint64_t version = 0;
    std::uint8_t valid = 0, forward_calibrated = 0;
};
struct ProbabilityEvPolicy {
    double minimum_net_edge = 0.0;
    double fractional_kelly = 0.25;
    std::int32_t maximum_chase_ticks = 0;
    // These are per-order cost/risk ceilings, not forecasts of capacity.
    std::int64_t max_order_cost_microdollars = 3'750'000;
    std::int64_t allocated_wealth_microdollars = 0;
    std::int64_t available_microdollars = 0;
    std::int64_t maximum_quantity_microunits = 20'000'000;
};
struct ProbabilityEvQuote {
    std::int32_t ask_e4 = 0;
    std::int32_t tick_size_e4 = 0;
    std::int32_t maximum_limit_e4 = 9'999;
    std::int64_t visible_quantity_microunits = 0;
    std::int64_t minimum_quantity_microunits = 0;
    double fee_rate = std::numeric_limits<double>::quiet_NaN();
    double fee_exponent = std::numeric_limits<double>::quiet_NaN();
    double execution_reserve_per_share = std::numeric_limits<double>::quiet_NaN();
    std::uint8_t is_up = 0;
};
enum class ProbabilityEvReason : std::uint8_t {
    Accepted = 1, InvalidForecast, StaleForecast, InvalidPolicy,
    UnknownCost, InvalidQuote, NonPositiveNetEdge, BelowVenueMinimum
};
struct ProbabilityEvDecision {
    ProbabilityEvReason reason = ProbabilityEvReason::InvalidForecast;
    double probability = std::numeric_limits<double>::quiet_NaN();
    double probability_lower = std::numeric_limits<double>::quiet_NaN();
    double fee_per_share = std::numeric_limits<double>::quiet_NaN();
    double cost_per_share = std::numeric_limits<double>::quiet_NaN();
    double expected_net_edge = std::numeric_limits<double>::quiet_NaN();
    double conservative_net_edge = std::numeric_limits<double>::quiet_NaN();
    double worst_case_cost_per_share = std::numeric_limits<double>::quiet_NaN();
    double worst_case_conservative_net_edge = std::numeric_limits<double>::quiet_NaN();
    std::int32_t observed_ask_e4 = 0;
    std::int32_t maximum_executable_price_e4 = 0;
    std::int64_t quantity_microunits = 0, cost_ceiling_microdollars = 0;
    std::uint8_t accepted = 0;
};
[[nodiscard]] inline ProbabilityEvDecision evaluate_probability_ev(
    const SettlementProbabilityForecast& f, const ProbabilityEvQuote& q,
    const ProbabilityEvPolicy& p, std::int64_t now_ns) noexcept {
    ProbabilityEvDecision d;
    if (!f.valid || !f.version || !std::isfinite(f.up) || !std::isfinite(f.lower)
        || !std::isfinite(f.upper) || f.lower < 0 || f.upper > 1
        || f.lower > f.up || f.up > f.upper || f.asof_ns <= 0
        || f.max_input_receive_ns <= 0 || f.max_input_receive_ns > f.asof_ns
        || f.asof_ns > now_ns || now_ns <= 0) return d;
    if (f.valid_until_ns < now_ns || f.valid_until_ns < f.asof_ns) {
        d.reason=ProbabilityEvReason::StaleForecast;return d;
    }
    if (!std::isfinite(p.minimum_net_edge) || p.minimum_net_edge < 0
        || !std::isfinite(p.fractional_kelly) || p.fractional_kelly <= 0
        || p.fractional_kelly > 1 || p.maximum_chase_ticks < 0
        || p.maximum_chase_ticks > 20 || p.max_order_cost_microdollars <= 10
        || p.allocated_wealth_microdollars <= 0 || p.available_microdollars <= 10
        || p.maximum_quantity_microunits <= 0) {
        d.reason=ProbabilityEvReason::InvalidPolicy;return d;
    }
    if (!std::isfinite(q.fee_rate) || q.fee_rate < 0 || q.fee_rate > 1
        || !std::isfinite(q.fee_exponent) || q.fee_exponent < 1.0
        || !std::isfinite(q.execution_reserve_per_share)
        || q.execution_reserve_per_share < 0) {
        d.reason=ProbabilityEvReason::UnknownCost;return d;
    }
    if (q.is_up > 1 || q.ask_e4 <= 0 || q.ask_e4 >= 10'000
        || q.tick_size_e4 <= 0 || q.ask_e4 % q.tick_size_e4 != 0
        || q.maximum_limit_e4 < q.ask_e4 || q.maximum_limit_e4 >= 10'000
        || q.visible_quantity_microunits <= 0 || q.minimum_quantity_microunits <= 0) {
        d.reason=ProbabilityEvReason::InvalidQuote;return d;
    }
    const auto all_in_cost = [&](std::int32_t e4) noexcept {
        const double price=static_cast<double>(e4)/10'000.0;
        const double fee=q.fee_rate*std::pow(price*(1.0-price),q.fee_exponent);
        return price+fee+q.execution_reserve_per_share;
    };
    const double price=static_cast<double>(q.ask_e4)/10'000.0;
    d.observed_ask_e4=q.ask_e4;
    d.probability=q.is_up ? f.up : 1.0-f.up;
    // DOWN's conservative probability is 1-UPPER(UP), not 1-LOWER(UP).
    d.probability_lower=q.is_up ? f.lower : 1.0-f.upper;
    d.fee_per_share=q.fee_rate*std::pow(price*(1.0-price),q.fee_exponent);
    d.cost_per_share=all_in_cost(q.ask_e4);
    d.expected_net_edge=d.probability-d.cost_per_share;
    d.conservative_net_edge=d.probability_lower-d.cost_per_share;
    if (!std::isfinite(d.cost_per_share) || d.cost_per_share >= 1.0
        || !(d.conservative_net_edge > p.minimum_net_edge)) {
        d.reason=ProbabilityEvReason::NonPositiveNetEdge;return d;
    }

    // A taker may chase only a small, predeclared number of ticks, and only
    // while the conservative settlement edge still clears all-in cost.
    d.maximum_executable_price_e4=q.ask_e4;
    d.worst_case_cost_per_share=d.cost_per_share;
    d.worst_case_conservative_net_edge=d.conservative_net_edge;
    for (std::int32_t step=1; step<=p.maximum_chase_ticks; ++step) {
        const auto candidate64=static_cast<std::int64_t>(q.ask_e4)
            + static_cast<std::int64_t>(step)*q.tick_size_e4;
        if (candidate64>q.maximum_limit_e4 || candidate64>=10'000) break;
        const auto candidate=static_cast<std::int32_t>(candidate64);
        const double cost=all_in_cost(candidate);
        const double edge=d.probability_lower-cost;
        if (!std::isfinite(cost) || cost>=1.0 || !(edge>p.minimum_net_edge)) break;
        d.maximum_executable_price_e4=candidate;
        d.worst_case_cost_per_share=cost;
        d.worst_case_conservative_net_edge=edge;
    }
    const long double fraction=static_cast<long double>(p.fractional_kelly)
        *d.worst_case_conservative_net_edge/(1.0-d.worst_case_cost_per_share);
    const long double budget=std::min({fraction*p.allocated_wealth_microdollars,
        static_cast<long double>(p.max_order_cost_microdollars-10),
        static_cast<long double>(p.available_microdollars-10)});
    const long double raw=std::min({budget/d.worst_case_cost_per_share,
        static_cast<long double>(q.visible_quantity_microunits),
        static_cast<long double>(p.maximum_quantity_microunits)});
    if (!std::isfinite(raw) || raw < q.minimum_quantity_microunits
        || raw >= static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        d.reason=ProbabilityEvReason::BelowVenueMinimum;return d;
    }
    d.quantity_microunits=static_cast<std::int64_t>(std::floor(raw));
    // Reserve ten microdollars for fee rounding. Never round size UP to a venue minimum.
    d.cost_ceiling_microdollars=static_cast<std::int64_t>(std::ceil(
        static_cast<long double>(d.quantity_microunits)*d.worst_case_cost_per_share))+10;
    if (d.cost_ceiling_microdollars>p.max_order_cost_microdollars
        || d.cost_ceiling_microdollars>p.available_microdollars) {
        d.quantity_microunits=0;d.reason=ProbabilityEvReason::InvalidPolicy;return d;
    }
    d.accepted=1;d.reason=ProbabilityEvReason::Accepted;return d;
}
static_assert(std::is_trivially_copyable_v<SettlementProbabilityForecast>);
static_assert(std::is_trivially_copyable_v<ProbabilityEvDecision>);
} // namespace pm::v7
