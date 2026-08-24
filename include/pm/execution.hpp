#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string_view>
#include <vector>

namespace pm {

struct QueueFillResult {
    double queue_ahead = 0.0;
    double fill_shares = 0.0;
};

struct FilledLegExposure {
    double filled_shares = 0.0;
    double target_shares = 0.0;
    double average_entry_price = 0.0;
};

// A bundle still has execution/market risk while it is resting, holding a
// completed position, or aborting. ABORTING must remain live until every
// outstanding order is cancelled and every filled leg is unwound.
inline bool bundle_has_live_risk(std::string_view status) {
    return status == "RESTING" || status == "COMPLETE" || status == "ABORTING";
}

// Unequal fills can continue to arrive during cancel latency after the bundle
// first crosses its completion threshold. Keep the naked-leg risk gate active
// for both RESTING and COMPLETE states; ABORTING is already committed to unwind.
inline bool bundle_requires_unmatched_risk_check(std::string_view status) {
    return status == "RESTING" || status == "COMPLETE";
}

// A passive order remains fillable during cancel latency. A trade at or after
// the effective cancellation time must not fill it. The strict arrival check
// preserves the existing conservative queue model for same-timestamp prints.
inline bool passive_buy_active_for_trade(std::string_view order_state,
                                         std::int64_t trade_ms,
                                         std::int64_t arrival_ms,
                                         std::int64_t cancel_effective_ms) {
    if (order_state != "RESTING" && order_state != "CANCEL_PENDING") return false;
    if (trade_ms <= arrival_ms) return false;
    if (order_state == "CANCEL_PENDING" && cancel_effective_ms > 0 &&
        trade_ms >= cancel_effective_ms) {
        return false;
    }
    return true;
}

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

// Worst-case capital at risk solely because legs filled at different rates.
// The common completed fraction is treated as the hedged bundle; every share
// above that fraction is naked and can lose its full entry cost. This is the
// quantity a max-leg-risk gate must control, not the current liquidation loss.
inline double unmatched_entry_risk(const std::vector<FilledLegExposure>& legs) {
    if (legs.empty()) return 0.0;
    std::vector<std::pair<double,double>> completion_inputs;
    completion_inputs.reserve(legs.size());
    for (const auto& leg : legs) {
        completion_inputs.push_back({
            std::max(0.0, leg.filled_shares),
            std::max(0.0, leg.target_shares)
        });
    }
    const double common = minimum_completion(completion_inputs);
    double risk = 0.0;
    for (const auto& leg : legs) {
        const double filled = std::max(0.0, leg.filled_shares);
        const double target = std::max(0.0, leg.target_shares);
        const double naked = std::max(0.0, filled - common * target);
        risk += naked * std::max(0.0, leg.average_entry_price);
    }
    return risk;
}

} // namespace pm
