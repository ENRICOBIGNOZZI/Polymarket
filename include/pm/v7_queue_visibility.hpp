#pragma once

#include "pm/v7_intent.hpp"
#include "pm/v7_market_state.hpp"

#include <algorithm>
#include <cstdint>

namespace pm::v7 {

struct QueueVisibility {
    std::int64_t visible_ahead_microunits = 0;
    std::uint8_t observable = 0;
};

// Return the public quantity already resting at the exact quote price, using the
// canonical executable L10 ladder. A zero can be distinguished from an unknown
// deeper-than-L10 queue. This avoids both the previous whole-L10 overestimate
// and the dangerous alternative of treating unobserved depth as zero.
[[nodiscard]] inline QueueVisibility visible_queue_at_quote(
    const StrategyIntent& intent,
    const BookHotSnapshot& book) noexcept {

    QueueVisibility out;
    if (intent.type != IntentType::Quote || intent.price_tick <= 0
        || book.valid == 0 || book.lineage_continuous == 0 || book.tick_size_e4 <= 0
        || (intent.side != Side::Buy && intent.side != Side::Sell)) {
        return out;
    }
    const std::int64_t tick = static_cast<std::int64_t>(book.tick_size_e4);
    if (intent.price_tick > static_cast<std::int64_t>(kCanonicalPriceScale) / tick) return out;
    const std::int64_t quote_e4 = intent.price_tick * tick;
    if (quote_e4 <= 0 || quote_e4 >= kCanonicalPriceScale) return out;

    const auto inspect = [&](const auto& levels,
                             std::uint8_t count,
                             bool buy) noexcept -> QueueVisibility {
        QueueVisibility result;
        const std::size_t n = std::min<std::size_t>(count, levels.size());
        if (n == 0) return result;
        for (std::size_t i = 0; i < n; ++i) {
            if (levels[i].price_e4 == quote_e4) {
                result.visible_ahead_microunits = std::max<std::int64_t>(
                    0, levels[i].quantity_microunits);
                result.observable = 1;
                return result;
            }
        }

        // If the price lies inside the represented top-N price range, its
        // absence from the occupied ladder is itself an observation of zero.
        const std::int64_t deepest_e4 = levels[n - 1].price_e4;
        const bool inside_represented_range = buy
            ? quote_e4 >= deepest_e4
            : quote_e4 <= deepest_e4;
        if (inside_represented_range || n < levels.size()) {
            result.observable = 1;
            return result;
        }
        // Full L10 and a quote deeper than the tenth occupied level: unknown.
        return result;
    };

    if (intent.side == Side::Buy) {
        if (quote_e4 > book.best_bid_e4) {
            out.observable = 1; // price improvement: nobody is ahead at price.
            return out;
        }
        return inspect(book.bid_levels, book.bid_level_count, true);
    }

    if (quote_e4 < book.best_ask_e4) {
        out.observable = 1;
        return out;
    }
    return inspect(book.ask_levels, book.ask_level_count, false);
}

} // namespace pm::v7
