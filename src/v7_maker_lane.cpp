#include "pm/v7_maker_lane.hpp"

#include <algorithm>

namespace pm::v7::maker {
namespace {

constexpr double kMicrounitsPerShare = 1'000'000.0;
constexpr double kPriceScale = 10'000.0;

[[nodiscard]] double shares(std::int64_t microunits) noexcept {
    return static_cast<double>(std::max<std::int64_t>(0, microunits)) / kMicrounitsPerShare;
}

} // namespace

MakerInstrumentLane::MakerInstrumentLane(std::int8_t inventory_sign) noexcept
    : inventory_sign_(inventory_sign) {}

InventorySnapshot MakerInstrumentLane::oriented_inventory(
    const InventorySnapshot& inventory) const noexcept {
    if (inventory_sign_ >= 0) return inventory;
    InventorySnapshot out = inventory;
    std::swap(out.yes_shares, out.no_shares);
    // The common market inventory is expressed in YES-NO orientation. Swapping
    // pending BUY/SELL buckets negates their net reservation too, so the NO lane
    // sees positive exposure as overweight NO rather than overweight YES.
    std::swap(out.reserved_buy_shares, out.reserved_sell_shares);
    return out;
}

MakerDecision MakerInstrumentLane::on_market_event(
    const MarketWsEvent& event,
    const MakerLaneContext& context,
    const MakerModelSnapshot& model) noexcept {

    MarketUpdate update;
    update.market_handle = event.market_handle;
    update.event_handle = event.event_handle;
    update.instrument_handle = event.instrument_handle;
    update.state_version = event.state_version;
    update.exchange_event_ns = event.exchange_event_ns > 0
        ? event.exchange_event_ns : event.book.exchange_event_ns;
    update.socket_receive_monotonic_ns = event.receive_monotonic_ns > 0
        ? event.receive_monotonic_ns : event.book.receive_monotonic_ns;
    update.instrument_inventory_sign = inventory_sign_;

    const bool sign_valid = inventory_sign_ == 1 || inventory_sign_ == -1;
    const bool book_valid = event.book.valid != 0 && event.book.lineage_continuous != 0
        && event.book.tick_size_e4 > 0;
    update.feed_healthy = static_cast<std::uint8_t>(
        sign_valid && book_valid && event.kind != MarketWsEventKind::LineageInvalidated);

    if (event.book.tick_size_e4 > 0) {
        update.best_bid_tick = event.book.best_bid_e4 / event.book.tick_size_e4;
        update.best_ask_tick = event.book.best_ask_e4 / event.book.tick_size_e4;
    }
    update.bid_depth_l1 = shares(event.book.bid_depth.l1_microunits);
    update.ask_depth_l1 = shares(event.book.ask_depth.l1_microunits);
    update.bid_depth_l5 = shares(event.book.bid_depth.l5_microunits);
    update.ask_depth_l5 = shares(event.book.ask_depth.l5_microunits);
    update.bid_depth_l10 = shares(event.book.bid_depth.l10_microunits);
    update.ask_depth_l10 = shares(event.book.ask_depth.l10_microunits);

    if (event.kind == MarketWsEventKind::Trade) {
        const double quantity = shares(event.quantity_microunits);
        if (event.side == pm::v7::Side::Buy) update.trade_buy_qty_delta = quantity;
        else if (event.side == pm::v7::Side::Sell) update.trade_sell_qty_delta = quantity;
    }

    update.conservative_rebate_ev_per_share =
        std::max(0.0, context.conservative_rebate_ev_per_share);
    update.conservative_reward_ev_per_share =
        std::max(0.0, context.conservative_reward_ev_per_share);
    update.related_fair_value = context.related_fair_value;
    update.related_snapshot_age_ns = context.related_snapshot_age_ns;
    update.related_state_valid = context.related_state_valid;

    MakerModelSnapshot local_model = model;
    if (event.book.tick_size_e4 > 0) {
        local_model.tick_size = static_cast<double>(event.book.tick_size_e4) / kPriceScale;
    }

    return hot_.on_market_update(
        update,
        oriented_inventory(context.inventory),
        context.quotes,
        context.risk,
        local_model);
}

} // namespace pm::v7::maker
