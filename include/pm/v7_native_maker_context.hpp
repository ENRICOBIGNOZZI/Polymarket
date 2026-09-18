#pragma once
#include "pm/v7_maker_lane.hpp"
#include "pm/v7_native_settlement_authority.hpp"
#include <algorithm>
#include <cstdint>

namespace pm::v7 {
inline maker::MakerLaneContext native_maker_context(
    const NativeSettlementAuthority& owner, std::uint64_t yes, std::uint64_t no,
    std::uint64_t instrument, maker::RiskSnapshot risk) noexcept {
    maker::MakerLaneContext out{};
    out.risk = risk;
    constexpr double scale = 1'000'000.0;
    out.inventory.yes_shares = owner.inventory_snapshot(yes).total_microunits / scale;
    out.inventory.no_shares = owner.inventory_snapshot(no).total_microunits / scale;
    for (const auto token : {yes, no}) {
        for (const auto side : {Side::Buy, Side::Sell}) {
            const auto* order = owner.maker_order(token, side);
            if (order == nullptr) continue;
            const double quantity = std::max<std::int64_t>(0, order->remaining_microunits) / scale;
            if ((token == yes) == (side == Side::Buy)) out.inventory.reserved_buy_shares += quantity;
            else out.inventory.reserved_sell_shares += quantity;
            if (token != instrument) continue;
            if (side == Side::Buy) { out.quotes.bid_active = 1; out.quotes.bid_tick = order->price_tick; }
            else { out.quotes.ask_active = 1; out.quotes.ask_tick = order->price_tick; }
            out.quotes.last_quote_monotonic_ns = std::max(out.quotes.last_quote_monotonic_ns,
                order->live_ns > 0 ? order->live_ns : order->submission_ns);
            if (order->state == OrderState::CancelRequested || order->state == OrderState::CancelPending)
                out.quotes.cancel_pending = 1;
        }
    }
    return out;
}

// Returns zero when the venue minimum and approved caps have no intersection.
// The caller supplies already-approved share and monetary caps; this never
// increases them and never grants execution authority.
inline std::int64_t native_maker_admissible_quantity(
    std::int64_t desired, std::int64_t venue_minimum, std::int64_t share_cap,
    std::int64_t price_e4, std::int64_t notional_cap_microdollars,
    std::int64_t available_microdollars, std::int64_t visible_depth_microunits) noexcept {
    if (desired <= 0 || venue_minimum <= 0 || share_cap < venue_minimum || price_e4 <= 0
        || price_e4 >= 10'000 || notional_cap_microdollars <= 0 || available_microdollars <= 0
        || visible_depth_microunits < venue_minimum) return 0;
    const auto budget = std::min(notional_cap_microdollars, available_microdollars);
    const auto by_budget = static_cast<std::int64_t>(
        std::min<long double>(share_cap, static_cast<long double>(budget) * 10'000 / price_e4));
    const auto quantity = std::min({std::max(desired, venue_minimum), share_cap, by_budget, visible_depth_microunits});
    return quantity >= venue_minimum ? quantity : 0;
}
} // namespace pm::v7
