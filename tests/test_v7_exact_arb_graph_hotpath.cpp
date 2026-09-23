#include "pm/v7_exact_arb_graph_hotpath.hpp"
#include "pm/v7_pure_arb_lane.hpp"

#include <array>
#include <cassert>

int main() {
    using namespace pm::v7;
    using namespace pm::v7::exact_arb_graph;
    std::array<BookDeepSnapshot, 2> books{};
    for (auto& book : books) {
        book.valid = 1; book.lineage_continuous = 1; book.ask_truncated = 0;
        book.ask_level_count = 2;
        book.ask_levels[0] = PriceLevelE4{4000, 2 * kShareMicrounits};
        book.ask_levels[1] = PriceLevelE4{4900, 3 * kShareMicrounits};
    }
    CompiledRelation relation{};
    relation.relation_handle = 7; relation.proof_handle = 9; relation.enabled = 1;
    relation.leg_count = 2; relation.guaranteed_payout_microunits = kShareMicrounits;
    relation.legs[0].book_handle = 0; relation.legs[0].coefficient = {1, 1};
    relation.legs[1].book_handle = 1; relation.legs[1].coefficient = {1, 1};
    relation.legs[0].fee_verified = relation.legs[1].fee_verified = 1;
    const auto direct = evaluate_buy(relation, books);
    assert(direct.reject == HotReject::Accepted && direct.quantity_microunits == 5 * kShareMicrounits);
    const auto champion = pm::v7::pure_arb::sweep(books[0], books[1], 0.0, 1.0, 0.0, true);
    assert(champion.shares_microunits == direct.quantity_microunits);
    assert(static_cast<std::int64_t>(std::llround(champion.gross_locked_pnl * kShareMicrounits))
           == direct.gross_pnl_microunits);

    auto below_minimum = relation;
    below_minimum.legs[0].minimum_order_microunits = 6 * kShareMicrounits;
    assert(evaluate_buy(below_minimum, books).reject == HotReject::MinimumOrder);
    auto truncated = books;
    truncated[1].ask_truncated = 1;
    assert(evaluate_buy(relation, truncated).reject == HotReject::IncompleteDepth);
    auto no_edge = books;
    no_edge[0].ask_levels[0].price_e4 = 6000;
    no_edge[1].ask_levels[0].price_e4 = 6000;
    no_edge[0].ask_levels[1].price_e4 = no_edge[1].ask_levels[1].price_e4 = 6001;
    assert(evaluate_buy(relation, no_edge).reject == HotReject::NoPositiveEdge);
    auto timed = books;
    timed[0].receive_monotonic_ns = 100;
    timed[1].receive_monotonic_ns = 100;
    assert(evaluate_buy(relation, timed, {200, 50, 0}).reject == HotReject::StaleBook);
    timed[0].receive_monotonic_ns = 190;
    timed[1].receive_monotonic_ns = 150;
    assert(evaluate_buy(relation, timed, {200, 100, 20}).reject == HotReject::LegSkew);
    std::array<CompiledRelation, 1> relations{relation};
    std::array<TokenDependency, 1> dependencies{TokenDependency{42, 0, 1}};
    std::array<std::uint32_t, 1> handles{0}; int callbacks = 0;
    evaluate_token_update(42, dependencies, handles, relations, books, {}, [&](const HotDecision& result) {
        ++callbacks; assert(result.relation_handle == 7 && result.reject == HotReject::Accepted);
    });
    evaluate_token_update(43, dependencies, handles, relations, books, {}, [&](const HotDecision&) { ++callbacks; });
    assert(callbacks == 1);

    // Relation units need not equal leg shares: the hot path preserves the
    // denominator quantum rather than rounding a half-share leg upward.
    relation.legs[0].coefficient = {1, 2};
    relation.legs[1].coefficient = {1, 2};
    const auto fractional = evaluate_buy(relation, books);
    assert(fractional.reject == HotReject::Accepted
           && fractional.quantity_microunits == 10 * kShareMicrounits);

    // Deterministic buy/sell matrix against the frozen rounded-fee sweep.
    // Champion 'gross' is after fees but before reserve; raw PnL is separate.
    for (bool buy : {true, false}) for (double rate : {0.0, 0.02, 0.07})
    for (std::int64_t reserve : {0, 500, 20000}) for (int depth : {1, 2}) {
        auto r = relation;
        r.reserve_per_unit_microunits = reserve;
        for (int i = 0; i < 2; ++i) {
            r.legs[i].coefficient = {1, 1}; r.legs[i].fee_rate = rate;
            books[i].ask_level_count = books[i].bid_level_count = depth;
            books[i].bid_levels[0] = {6000, 2*kShareMicrounits};
            books[i].bid_levels[1] = {5100, 3*kShareMicrounits};
        }
        std::array<std::int64_t, 2> inventory{5*kShareMicrounits, 5*kShareMicrounits};
        HotResources resources{}; resources.inventory = inventory;
        const auto g = evaluate_basket(r, books, buy, {}, resources);
        const auto c = pure_arb::sweep(books[0], books[1], rate, 1.0, reserve/1e6, buy);
        assert(g.quantity_microunits == c.shares_microunits);
        assert(g.gross_pnl_microunits == std::llround(c.gross_locked_pnl*1e6));
        assert(g.net_pnl_microunits == std::llround(c.conservative_locked_pnl*1e6));
        assert(g.levels_consumed[0] == c.yes_levels_used);
        assert(g.levels_consumed[1] == c.no_levels_used);
    }
    relation.legs[0].coefficient = relation.legs[1].coefficient = {1, 1};
    assert(evaluate_sell_inventory_basket(relation, books).reject == HotReject::InventoryUnavailable);
    HotResources budget{}; budget.capital_microunits = 800000;
    const auto limited = evaluate_buy_basket(relation, books, {}, budget);
    assert(limited.reject == HotReject::Accepted && limited.quantity_microunits == kShareMicrounits);
    auto unknown_fee = relation; unknown_fee.legs[0].fee_verified = 0;
    assert(evaluate_buy(unknown_fee, books).reject == HotReject::UnknownFee);
    auto rounded_fee = relation.legs[0]; rounded_fee.fee_rate=.06;
    assert(std::llround(exact_fee(2000000,1750,rounded_fee)*1e6)==17330);

    std::uint64_t seed = 910237;
    auto random = [&]() { seed ^= seed << 13; seed ^= seed >> 7; seed ^= seed << 17; return seed; };
    for (int trial = 0; trial < 1000; ++trial) {
        std::array<BookDeepSnapshot,kMaxLegs> many{};
        CompiledRelation r{}; r.enabled=1; r.guaranteed_payout_microunits=1000000;
        r.leg_count=static_cast<std::uint8_t>(2+random()%15);
        r.reserve_per_unit_microunits=500;
        for (std::uint32_t i=0;i<r.leg_count;++i) {
            r.legs[i].book_handle=i; r.legs[i].coefficient={1+static_cast<std::int64_t>(random()%8),1};
            r.legs[i].fee_verified=1;
            many[i].valid=many[i].lineage_continuous=1; many[i].ask_level_count=1;
            many[i].ask_levels[0]={10,static_cast<std::int64_t>(1+random()%100)*1000000};
        }
        const auto d=evaluate_buy(r,many);
        if (d.reject==HotReject::Accepted) {
            assert(d.net_pnl_microunits>0);
            for (std::uint32_t i=0;i<r.leg_count;++i)
                assert(static_cast<__int128>(d.quantity_microunits)*r.legs[i].coefficient.numerator
                    <=many[i].ask_levels[0].quantity_microunits);
        }
    }
    auto overflow=relation;
    overflow.legs[0].coefficient={1,std::numeric_limits<std::int64_t>::max()};
    overflow.legs[0].minimum_order_microunits=2;
    assert(evaluate_buy(overflow,books).reject==HotReject::NumericOverflow);
}
