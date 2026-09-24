#include "pm/v7_exact_arb_diagnostics.hpp"
#include <cassert>

using namespace pm::v7;
using namespace pm::v7::exact_arb_graph;

int main() {
    CompiledRelation r;
    r.enabled = 1; r.leg_count = 2; r.guaranteed_payout_microunits = 1000000;
    r.reserve_per_unit_microunits = 500;
    std::array<BookDeepSnapshot, 2> books{};
    for (unsigned i = 0; i < 2; ++i) {
        r.legs[i].book_handle = i; r.legs[i].coefficient = {1, 1};
        r.legs[i].fee_verified = 1; r.legs[i].minimum_order_microunits = 5000000;
        auto& b = books[i];
        b.valid = b.lineage_continuous = 1;
        b.receive_monotonic_ns = 1000;
        b.ask_level_count = b.bid_level_count = 1;
        b.ask_levels[0] = {4000, 5019000};
        b.bid_levels[0] = {6000, 5019000};
    }
    HotTimingContext t{1010, 100, 50};
    HotResources resources;
    resources.capital_microunits = 1000000000;
    auto sized = evaluate_order_constrained_basket(r, books, t, resources, 10000);
    assert(sized.decision.reject == HotReject::Accepted);
    assert(sized.unconstrained_quantity_microunits == 5019000);
    assert(sized.decision.quantity_microunits == 5010000);
    assert(sized.quantity_precision_ready && sized.global_optimum_proven && !sized.search_exhausted);
    assert(sized.net_upper_bound_valid && sized.net_upper_bound_microunits == 999495);
    assert(sized.decision.net_pnl_microunits == 999495);
    assert(sized.decision.capital_required_microunits <= resources.capital_microunits);

    // Two sub-cent-share fragments form a perfectly valid total order. Applying
    // the venue size quantum separately to levels would erase useful liquidity.
    for (auto& b : books) {
        b.ask_level_count = 2;
        b.ask_levels[0] = {4000, 5006000};
        b.ask_levels[1] = {4100, 4000};
    }
    sized = evaluate_order_constrained_basket(r, books, t, resources, 10000);
    assert(sized.decision.reject == HotReject::Accepted);
    assert(sized.decision.quantity_microunits == 5010000);
    assert(sized.decision.levels_consumed[0] == 2 && sized.decision.levels_consumed[1] == 2);
    assert(sized.decision.net_pnl_microunits == 999415);
    r.legs[0].minimum_order_microunits = 5015000;
    assert(evaluate_order_constrained_basket(r, books, t, resources, 10000).decision.reject != HotReject::Accepted);
    r.legs[0].minimum_order_microunits = 5000000;

    // Every leg, not merely the relation quantity, must satisfy the order grid.
    auto fractional = r;
    fractional.legs[0].coefficient = {1, 3};
    fractional.legs[1].coefficient = {2, 7};
    assert(order_quantity_quantum(fractional, 10000) == 210000);
    for (int a = 1; a <= 20; ++a) for (int b = 1; b <= 20; ++b) {
        fractional.legs[0].coefficient = {a, b};
        const auto quantum = order_quantity_quantum(fractional, 10000);
        assert(quantum > 0);
        for (unsigned leg = 0; leg < 2; ++leg) {
            const auto c = fractional.legs[leg].coefficient;
            assert((static_cast<__int128>(quantum)*c.numerator)%(static_cast<__int128>(c.denominator)*10000) == 0);
        }
    }
    fractional.legs[0].coefficient = {1, INT64_MAX};
    assert(order_quantity_quantum(fractional, 10000) == 0);
    assert(evaluate_order_constrained_basket(fractional, books, t, resources, 10000).decision.reject == HotReject::VenuePrecision);

    // Small budgets are re-evaluated on the order grid, never rounded upward.
    resources.capital_microunits = 4003500;
    sized = evaluate_order_constrained_basket(r, books, t, resources, 10000);
    assert(sized.decision.reject == HotReject::Accepted && sized.decision.quantity_microunits == 5000000);
    assert(sized.decision.capital_required_microunits <= resources.capital_microunits);
    resources.capital_microunits = 1000000000;

    auto near = near_arbitrage(r, books, t, 10000);
    assert(near.valid && near.books_ready && near.lineage_ready && near.fees_ready && near.freshness_ready);
    assert(near.raw_distance_nano == -200000000);
    assert(near.after_fee_distance_nano == -200000000);
    assert(near.after_reserve_distance_nano == -199500000);
    assert(near.actionable_tick_nano == 10000000);
    assert(near.maximum_leg_age_ns == 10 && near.minimum_leg_age_ns == 10);
    for (auto& l : r.legs) l.fee_rate = .07;
    near = near_arbitrage(r, books, t, 10000);
    assert(near.valid && near.fee_probe_quantity_microunits == 5000000);
    assert(near.after_fee_distance_nano == -166400000);
    assert(near.after_reserve_distance_nano == -165900000);
    r.sell_inventory = 1;
    assert(near_arbitrage(r, books, t, 10000).raw_distance_nano == -200000000);
    assert(evaluate_order_constrained_basket(r, books, t, resources, 10000).decision.reject == HotReject::InventoryUnavailable);
    r.sell_inventory = 0;
    books[0].ask_truncated = 1;
    assert(near_arbitrage(r, books, t, 10000).valid);
    assert(!near_arbitrage(r, books, t, 10000).depth_complete);
    assert(evaluate_order_constrained_basket(r, books, t, resources, 10000).decision.reject == HotReject::IncompleteDepth);
    books[0].ask_truncated = 0;
    books[0].receive_monotonic_ns = 1011;
    near = near_arbitrage(r, books, t, 10000);
    assert(!near.valid && !near.freshness_ready);
    books[0].receive_monotonic_ns = 910;
    near = near_arbitrage(r, books, t, 10000);
    assert(!near.valid && near.freshness_ready && !near.skew_ready);
    books[0].receive_monotonic_ns = 1000;
    books[0].lineage_continuous = 0;
    assert(!near_arbitrage(r, books, t, 10000).valid);
    books[0].lineage_continuous = 1;
    r.legs[0].fee_verified = 0;
    near = near_arbitrage(r, books, t, 10000);
    assert(!near.valid && !near.fees_ready);
    assert(!rounded_fee_units(1, 5000, r.legs[0]).valid);
    r.legs[0].fee_verified = 1;
    assert(!rounded_fee_units(-1, 5000, r.legs[0]).valid);
    assert(!rounded_fee_units(1, 0, r.legs[0]).valid);
    r.legs[0].coefficient = {INT64_MAX, 1};
    assert(!near_arbitrage(r, books, t, 10000).valid);
}
