#pragma once
#include "pm/v7_exact_arb_order_sizing.hpp"

namespace pm::v7::exact_arb_graph {

// Per-relation-unit top-of-book distances, not fills or full-depth capacity.
// Fees are normalized from a stated venue-sized probe quantity. Invalid inputs
// remain unknown, not a zero distance. Integer nanocurrency avoids sign drift.
struct NearArbDiagnostic {
    std::int64_t raw_distance_nano = 0;
    std::int64_t after_fee_distance_nano = 0;
    std::int64_t after_reserve_distance_nano = 0;
    std::int64_t actionable_tick_nano = 0;
    std::int64_t guarantee_nano = 0;
    std::int64_t fee_probe_quantity_microunits = 0;
    std::int64_t maximum_leg_age_ns = 0;
    std::int64_t minimum_leg_age_ns = 0;
    std::int64_t leg_skew_ns = 0;
    std::uint8_t books_ready = 0, lineage_ready = 0, fees_ready = 0;
    std::uint8_t freshness_ready = 0, skew_ready = 0, depth_complete = 0;
    std::uint8_t valid = 0;
};

[[nodiscard]] inline NearArbDiagnostic near_arbitrage(
    const CompiledRelation& r, std::span<const BookDeepSnapshot> books,
    const HotTimingContext& t, std::int64_t share_quantum) noexcept {
    NearArbDiagnostic out;
    if (!r.enabled || !r.leg_count || r.leg_count > kMaxLegs || r.guaranteed_payout_microunits <= 0
        || r.reserve_per_unit_microunits < 0 || t.now_receive_monotonic_ns <= 0
        || t.maximum_book_age_ns <= 0 || t.maximum_leg_skew_ns <= 0) return out;
    const auto quantum = order_quantity_quantum(r, share_quantum);
    if (!quantum) return out;
    out.books_ready = out.lineage_ready = out.fees_ready = out.freshness_ready = out.depth_complete = 1;
    std::int64_t earliest = INT64_MAX, latest = 0;
    __int128 probe = 1000000;
    const bool buy = !r.sell_inventory;
    for (std::size_t i = 0; i < r.leg_count; ++i) {
        const auto& l = r.legs[i];
        if (!l.coefficient.valid()) return {};
        out.fees_ready &= valid_fee_terms(l);
        if (l.book_handle >= books.size()) {
            out.books_ready = out.lineage_ready = out.freshness_ready = out.depth_complete = 0;
            continue;
        }
        const auto& b = books[l.book_handle];
        const auto count = buy ? b.ask_level_count : b.bid_level_count;
        const auto& depth = buy ? b.ask_levels : b.bid_levels;
        out.books_ready &= b.valid && count && count <= depth.size() && depth[0].quantity_microunits > 0
            && depth[0].price_e4 > 0 && depth[0].price_e4 < 10000 && b.tick_size_e4 > 0 && b.tick_size_e4 < 10000;
        out.lineage_ready &= b.lineage_continuous != 0;
        out.depth_complete &= !(buy ? b.ask_truncated : b.bid_truncated);
        out.freshness_ready &= b.receive_monotonic_ns > 0 && b.receive_monotonic_ns <= t.now_receive_monotonic_ns
            && t.now_receive_monotonic_ns-b.receive_monotonic_ns <= t.maximum_book_age_ns;
        earliest = std::min(earliest, b.receive_monotonic_ns);
        latest = std::max(latest, b.receive_monotonic_ns);
        if (l.minimum_order_microunits < 0) return {};
        const auto required = (static_cast<__int128>(l.minimum_order_microunits)*l.coefficient.denominator
                              + l.coefficient.numerator-1)/l.coefficient.numerator;
        probe = std::max(probe, required);
    }
    if (out.freshness_ready) {
        out.maximum_leg_age_ns = t.now_receive_monotonic_ns-earliest;
        out.minimum_leg_age_ns = t.now_receive_monotonic_ns-latest;
        out.leg_skew_ns = latest-earliest;
        out.skew_ready = out.leg_skew_ns <= t.maximum_leg_skew_ns;
    }
    if (!out.books_ready || !out.lineage_ready || !out.fees_ready || !out.freshness_ready || !out.skew_ready) return out;
    probe = ((probe+quantum-1)/quantum)*quantum;
    if (probe > INT64_MAX) return out;
    out.fee_probe_quantity_microunits = static_cast<std::int64_t>(probe);
    __int128 cash = 0, fees = 0;
    std::int64_t tick = INT64_MAX;
    for (std::size_t i = 0; i < r.leg_count; ++i) {
        const auto& l = r.legs[i]; const auto& b = books[l.book_handle];
        const auto price = (buy ? b.ask_levels : b.bid_levels)[0].price_e4;
        const auto n = l.coefficient.numerator, d = l.coefficient.denominator;
        const __int128 raw = static_cast<__int128>(price)*100000*n;
        // Buy cash cost rounds upward; sell proceeds round downward. Distances
        // never become more positive-PnL through nanocurrency representation.
        cash += raw/d + (buy && raw%d != 0);
        const __int128 leg_tick = static_cast<__int128>(b.tick_size_e4)*100000*n/d;
        if (leg_tick <= 0 || leg_tick > INT64_MAX) return out;
        tick = std::min(tick, static_cast<std::int64_t>(leg_tick));
        const __int128 shares = probe*n/d;
        if (shares > INT64_MAX) return out;
        const auto rounded = rounded_fee_units(static_cast<std::int64_t>(shares), price, l);
        if (!rounded.valid || rounded.units > static_cast<unsigned __int128>(INT64_MAX)) return out;
        const __int128 fee = static_cast<__int128>(rounded.units)*10000000000LL;
        fees += fee/probe + (fee%probe != 0);
    }
    const __int128 guarantee = static_cast<__int128>(r.guaranteed_payout_microunits)*1000;
    const __int128 raw_distance = buy ? cash-guarantee : guarantee-cash;
    const __int128 after_fee = raw_distance+fees;
    const __int128 after_reserve = after_fee+static_cast<__int128>(r.reserve_per_unit_microunits)*1000;
    for (const auto value : {guarantee, raw_distance, after_fee, after_reserve})
        if (value < INT64_MIN || value > INT64_MAX) return out;
    out.raw_distance_nano = static_cast<std::int64_t>(raw_distance);
    out.after_fee_distance_nano = static_cast<std::int64_t>(after_fee);
    out.after_reserve_distance_nano = static_cast<std::int64_t>(after_reserve);
    out.guarantee_nano = static_cast<std::int64_t>(guarantee);
    out.actionable_tick_nano = tick;
    out.valid = 1;
    return out;
}
} // namespace pm::v7::exact_arb_graph
