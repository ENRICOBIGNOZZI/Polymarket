#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <string>

namespace pm {

inline constexpr std::int64_t kSportsPregameBufferSeconds = 15 * 60;

struct MarketTiming {
    bool timed_sports = false;
    std::int64_t game_start_ts = 0;
    int seconds_delay = 0;
};

inline bool pregame_market_eligible(
    const MarketTiming& timing,
    std::int64_t now,
    std::int64_t buffer_seconds = kSportsPregameBufferSeconds) {
    if (!timing.timed_sports) return true;
    if (timing.game_start_ts <= 0) return false;
    return now < timing.game_start_ts - std::max<std::int64_t>(0, buffer_seconds);
}

inline bool timed_sports_started(const MarketTiming& timing, std::int64_t now) {
    return timing.timed_sports && timing.game_start_ts > 0 && now >= timing.game_start_ts;
}

struct RecentTrade {
    std::string condition_id;
    std::string token_id;
    std::string side;
    std::string transaction_hash;
    std::string id;
    double price = 0.0;
    double size = 0.0;
    std::int64_t ts = 0;
};

struct QueueConsumption {
    double queue_ahead = 0.0;
    double remaining_shares = 0.0;
    double filled_shares = 0.0;
    bool eligible_trade = false;
};

inline std::string uppercase_ascii(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::toupper(c));
    });
    return s;
}

inline QueueConsumption consume_passive_bid_trade(
    double queue_ahead,
    double remaining_shares,
    double limit_price,
    double tick_size,
    std::int64_t order_created_ts,
    const RecentTrade& trade) {
    QueueConsumption out{
        std::max(0.0, queue_ahead),
        std::max(0.0, remaining_shares),
        0.0,
        false
    };

    if (trade.ts <= order_created_ts || trade.size <= 0.0 || trade.price <= 0.0) return out;
    if (uppercase_ascii(trade.side) != "SELL") return out;

    const double tolerance = 0.25 * std::max(1e-8, tick_size);
    if (trade.price > limit_price + tolerance) return out;
    out.eligible_trade = true;

    double aggressive_size = trade.size;
    const double queue_consumed = std::min(out.queue_ahead, aggressive_size);
    out.queue_ahead -= queue_consumed;
    aggressive_size -= queue_consumed;

    out.filled_shares = std::min(out.remaining_shares, std::max(0.0, aggressive_size));
    out.remaining_shares -= out.filled_shares;
    if (out.queue_ahead < 1e-12) out.queue_ahead = 0.0;
    if (out.remaining_shares < 1e-12) out.remaining_shares = 0.0;
    return out;
}

} // namespace pm
