#include "pm/v7_pure_arb_lane.hpp"

#include <algorithm>
#include <bit>
#include <cassert>
#include <cmath>
#include <cstdint>

using namespace pm::v7;
using namespace pm::v7::pure_arb;

namespace {
struct LegacyDecision {
    Direction direction = Direction::None;
    SweepResult economics{};
    double buy_raw_edge = 0.0, sell_raw_edge = 0.0;
    double buy_fee = 0.0, sell_fee = 0.0;
    double buy_edge = 0.0, sell_edge = 0.0;
    double buy_l1_shares = 0.0, sell_l1_shares = 0.0;
    bool accepted = false;
};

bool legacy_book_valid(const BookHotSnapshot& b) {
    return b.valid != 0 && b.lineage_continuous != 0
        && b.tick_size_e4 > 0 && b.tick_size_e4 < 10'000
        && b.best_bid_e4 > 0 && b.best_ask_e4 > b.best_bid_e4
        && b.best_ask_e4 < 10'000
        && b.best_bid_microunits > 0 && b.best_ask_microunits > 0
        && b.bid_level_count > 0 && b.ask_level_count > 0;
}

LegacyDecision legacy_reference(const PairInput& in) {
    LegacyDecision out{};
    if (in.market_handle == 0 || in.yes_instrument_handle == 0
        || in.no_instrument_handle == 0 || in.minimum_order_microunits <= 0
        || in.maximum_leg_skew_ns <= 0 || in.trigger_receive_monotonic_ns <= 0
        || !(in.market_start_wall_ms <= in.now_wall_ms
             && in.now_wall_ms < in.market_end_wall_ms)
        || in.fee_verified == 0 || in.yes_epoch == 0
        || in.yes_epoch != in.no_epoch
        || in.yes.receive_monotonic_ns <= 0 || in.no.receive_monotonic_ns <= 0
        || !std::isfinite(in.reserve_per_share) || in.reserve_per_share < 0.0) {
        return out;
    }
    const auto skew = in.yes.receive_monotonic_ns >= in.no.receive_monotonic_ns
        ? in.yes.receive_monotonic_ns - in.no.receive_monotonic_ns
        : in.no.receive_monotonic_ns - in.yes.receive_monotonic_ns;
    if (skew > in.maximum_leg_skew_ns
        || !legacy_book_valid(in.yes) || !legacy_book_valid(in.no)) return out;

    const double yes_ask = price(in.yes.best_ask_e4);
    const double no_ask = price(in.no.best_ask_e4);
    const double yes_bid = price(in.yes.best_bid_e4);
    const double no_bid = price(in.no.best_bid_e4);
    out.buy_l1_shares = static_cast<double>(std::min(
        in.yes.best_ask_microunits, in.no.best_ask_microunits))
        / kMicrounitsPerShare;
    out.sell_l1_shares = static_cast<double>(std::min(
        in.yes.best_bid_microunits, in.no.best_bid_microunits))
        / kMicrounitsPerShare;
    out.buy_fee =
        (fee_usdc(out.buy_l1_shares, yes_ask, in.fee_rate, in.fee_exponent)
         + fee_usdc(out.buy_l1_shares, no_ask, in.fee_rate, in.fee_exponent))
        / out.buy_l1_shares;
    out.sell_fee =
        (fee_usdc(out.sell_l1_shares, yes_bid, in.fee_rate, in.fee_exponent)
         + fee_usdc(out.sell_l1_shares, no_bid, in.fee_rate, in.fee_exponent))
        / out.sell_l1_shares;
    if (!std::isfinite(out.buy_fee) || !std::isfinite(out.sell_fee)) return out;

    out.buy_raw_edge = 1.0 - yes_ask - no_ask;
    out.sell_raw_edge = yes_bid + no_bid - 1.0;
    out.buy_edge = out.buy_raw_edge - out.buy_fee;
    out.sell_edge = out.sell_raw_edge - out.sell_fee;
    const auto buy = sweep(in.yes, in.no, in.fee_rate, in.fee_exponent,
                           in.reserve_per_share, true);
    const auto sell = sweep(in.yes, in.no, in.fee_rate, in.fee_exponent,
                            in.reserve_per_share, false,
                            std::max<std::int64_t>(0, in.sell_available_microunits));
    const SweepResult* selected = nullptr;
    if (buy.shares_microunits > 0) {
        selected = &buy; out.direction = Direction::BuyCompleteSet;
    } else if (sell.shares_microunits > 0) {
        selected = &sell; out.direction = Direction::SellCompleteSet;
    } else return out;
    if (selected->shares_microunits < in.minimum_order_microunits) return out;
    if (out.direction == Direction::SellCompleteSet
        && in.sell_available_microunits < selected->shares_microunits) return out;
    out.economics = *selected;
    out.accepted = true;
    return out;
}

void same_double(double a, double b) {
    assert(std::bit_cast<std::uint64_t>(a) == std::bit_cast<std::uint64_t>(b));
}
void same_sweep(const SweepResult& a, const SweepResult& b) {
    assert(a.shares_microunits == b.shares_microunits);
    same_double(a.gross_locked_pnl, b.gross_locked_pnl);
    same_double(a.conservative_locked_pnl, b.conservative_locked_pnl);
    same_double(a.yes_notional, b.yes_notional);
    same_double(a.no_notional, b.no_notional);
    same_double(a.marginal_edge_per_share, b.marginal_edge_per_share);
    assert(a.yes_levels_used == b.yes_levels_used);
    assert(a.no_levels_used == b.no_levels_used);
    assert(a.yes_limit_e4 == b.yes_limit_e4);
    assert(a.no_limit_e4 == b.no_limit_e4);
}

BookHotSnapshot make_book(std::int32_t bid, std::int32_t ask,
                          std::int64_t bid_q, std::int64_t ask_q,
                          std::int64_t receive, std::uint64_t version) {
    BookHotSnapshot b{};
    b.valid = 1; b.lineage_continuous = 1; b.tick_size_e4 = 100;
    b.state_version = version; b.receive_monotonic_ns = receive;
    b.exchange_event_ns = receive - 100;
    b.best_bid_e4 = bid; b.best_ask_e4 = ask;
    b.best_bid_microunits = bid_q; b.best_ask_microunits = ask_q;
    b.bid_levels[0] = {bid, bid_q}; b.ask_levels[0] = {ask, ask_q};
    b.bid_level_count = 1; b.ask_level_count = 1;
    return b;
}

PairInput base() {
    PairInput in{};
    in.market_handle = 7; in.event_handle = 8;
    in.yes_instrument_handle = 11; in.no_instrument_handle = 12;
    in.yes_epoch = in.no_epoch = 3;
    in.market_start_wall_ms = 1'000; in.market_end_wall_ms = 2'000;
    in.now_wall_ms = 1'500;
    in.trigger_receive_monotonic_ns = 10'000;
    in.decode_complete_monotonic_ns = 10'100;
    in.decision_monotonic_ns = 10'200;
    in.maximum_leg_skew_ns = 1'000;
    in.minimum_order_microunits = 5'000'000;
    in.sell_available_microunits = 20'000'000;
    in.fee_rate = 0.02; in.fee_exponent = 1.0;
    in.reserve_per_share = 0.0005; in.fee_verified = 1;
    in.yes = make_book(3900, 4000, 10'000'000, 6'000'000, 9'900, 21);
    in.no = make_book(4900, 5000, 10'000'000, 10'000'000, 10'000, 22);
    in.yes.ask_levels[1] = {4100, 4'000'000};
    in.yes.ask_level_count = 2;
    return in;
}

void compare(const PairInput& in) {
    const auto legacy = legacy_reference(in);
    const auto modern = evaluate_pair(in);
    assert((modern.accepted != 0) == legacy.accepted);
    if (!legacy.accepted) return;
    assert(modern.direction == legacy.direction);
    same_double(modern.buy_raw_edge_per_share, legacy.buy_raw_edge);
    same_double(modern.sell_raw_edge_per_share, legacy.sell_raw_edge);
    same_double(modern.buy_fee_per_share, legacy.buy_fee);
    same_double(modern.sell_fee_per_share, legacy.sell_fee);
    same_double(modern.buy_edge_per_share, legacy.buy_edge);
    same_double(modern.sell_edge_per_share, legacy.sell_edge);
    same_double(modern.buy_l1_shares, legacy.buy_l1_shares);
    same_double(modern.sell_l1_shares, legacy.sell_l1_shares);
    same_sweep(modern.economics, legacy.economics);
}
}

int main() {
    auto buy = base(); compare(buy);

    auto sell = base();
    sell.yes = make_book(6000, 6100, 8'000'000, 8'000'000, 9'900, 31);
    sell.no = make_book(4500, 4600, 8'000'000, 8'000'000, 10'000, 32);
    compare(sell);

    auto outside = buy; outside.now_wall_ms = outside.market_end_wall_ms; compare(outside);
    auto fee = buy; fee.fee_verified = 0; compare(fee);
    auto epoch = buy; epoch.no_epoch = 4; compare(epoch);
    auto skew = buy; skew.yes.receive_monotonic_ns = 20'000; compare(skew);
    auto lineage = buy; lineage.no.lineage_continuous = 0; compare(lineage);
    auto crossed = buy; crossed.yes.best_bid_e4 = crossed.yes.best_ask_e4; compare(crossed);
    auto minimum = buy; minimum.minimum_order_microunits = 20'000'000; compare(minimum);
    auto no_inventory = sell; no_inventory.sell_available_microunits = 0; compare(no_inventory);
    return 0;
}
