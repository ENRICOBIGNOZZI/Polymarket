#pragma once

#include <algorithm>
#include <cmath>
#include <vector>

namespace pm {

struct QueueFillResult {
    double queue_ahead = 0.0;
    double fill_shares = 0.0;
};

// Conservative FIFO approximation for a resting BUY. Only a taker SELL at or
// through our limit is allowed to consume displayed queue and then our order.
inline QueueFillResult consume_passive_buy(double queue_ahead,
                                           double remaining_shares,
                                           double limit_price,
                                           double trade_price,
                                           double trade_size,
                                           bool taker_sell) {
    QueueFillResult out{std::max(0.0, queue_ahead), 0.0};
    if (!taker_sell || remaining_shares <= 0.0 || trade_size <= 0.0 ||
        !std::isfinite(limit_price) || !std::isfinite(trade_price) ||
        trade_price > limit_price + 1e-9) {
        return out;
    }
    double flow = trade_size;
    const double before = std::min(out.queue_ahead, flow);
    out.queue_ahead -= before;
    flow -= before;
    out.fill_shares = std::min(std::max(0.0, remaining_shares), std::max(0.0, flow));
    return out;
}

inline double minimum_completion(const std::vector<std::pair<double,double>>& filled_target) {
    if (filled_target.empty()) return 0.0;
    double c = 1.0;
    for (const auto& [filled, target] : filled_target) {
        if (target <= 1e-12) return 0.0;
        c = std::min(c, std::clamp(filled / target, 0.0, 1.0));
    }
    return c;
}

} // namespace pm
