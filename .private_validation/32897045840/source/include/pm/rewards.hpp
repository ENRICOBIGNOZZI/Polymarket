#pragma once

#include <algorithm>
#include <cmath>

namespace pm {

// Polymarket exposes rewards_max_spread in cents. Convert it to the same
// probability-price scale used by the CLOB books (e.g. 3 cents -> 0.03).
inline double reward_spread_price_from_cents(double spread_cents) {
    if (!std::isfinite(spread_cents) || spread_cents <= 0.0) return 0.0;
    return spread_cents / 100.0;
}

// Official quadratic order-position score S(v,s)=((v-s)/v)^2 * size * b.
// Orders outside the configured maximum spread earn no score.
inline double reward_order_score(double max_spread_price,
                                 double distance_from_adjusted_mid,
                                 double shares,
                                 double in_game_multiplier = 1.0) {
    if (!std::isfinite(max_spread_price) || max_spread_price <= 0.0 ||
        !std::isfinite(distance_from_adjusted_mid) || distance_from_adjusted_mid < 0.0 ||
        !std::isfinite(shares) || shares <= 0.0 ||
        !std::isfinite(in_game_multiplier) || in_game_multiplier <= 0.0 ||
        distance_from_adjusted_mid >= max_spread_price) {
        return 0.0;
    }
    const double relative = (max_spread_price - distance_from_adjusted_mid) / max_spread_price;
    return relative * relative * shares * in_game_multiplier;
}

// Official Q_min construction. Between 10c and 90c, one-sided liquidity can
// score at a reduced rate; outside that interval two-sided liquidity is needed.
inline double reward_minimum_score(double q_one,
                                   double q_two,
                                   double adjusted_midpoint,
                                   double one_sided_divisor = 3.0) {
    q_one = std::max(0.0, q_one);
    q_two = std::max(0.0, q_two);
    const double balanced = std::min(q_one, q_two);
    if (adjusted_midpoint >= 0.10 && adjusted_midpoint <= 0.90 &&
        std::isfinite(one_sided_divisor) && one_sided_divisor > 1.0) {
        return std::max(balanced, std::max(q_one, q_two) / one_sided_divisor);
    }
    return balanced;
}

// Public books do not reveal maker identity, hidden refresh or future quoting.
// Inflate observed competition and then apply a separate empirical haircut to
// the resulting share before treating it as an economic input.
inline double estimated_reward_share(double our_qmin,
                                     double observed_book_qmin,
                                     double competition_multiplier = 2.0) {
    our_qmin = std::max(0.0, our_qmin);
    observed_book_qmin = std::max(0.0, observed_book_qmin);
    competition_multiplier = std::max(1.0, competition_multiplier);
    const double denominator = our_qmin + competition_multiplier * observed_book_qmin;
    return denominator > 1e-12 ? std::clamp(our_qmin / denominator, 0.0, 1.0) : 0.0;
}

} // namespace pm
