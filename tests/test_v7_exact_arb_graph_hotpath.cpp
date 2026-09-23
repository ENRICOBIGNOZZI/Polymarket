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
    assert(evaluate_buy(relation, no_edge).reject == HotReject::NoPositiveEdge);
    std::array<CompiledRelation, 1> relations{relation};
    std::array<TokenDependency, 1> dependencies{TokenDependency{42, 0, 1}};
    std::array<std::uint32_t, 1> handles{0}; int callbacks = 0;
    evaluate_token_update(42, dependencies, handles, relations, books, [&](const HotDecision& result) {
        ++callbacks; assert(result.relation_handle == 7 && result.reject == HotReject::Accepted);
    });
    evaluate_token_update(43, dependencies, handles, relations, books, [&](const HotDecision&) { ++callbacks; });
    assert(callbacks == 1);

    // Relation units need not equal leg shares: the hot path preserves the
    // denominator quantum rather than rounding a half-share leg upward.
    relation.legs[0].coefficient = {1, 2};
    relation.legs[1].coefficient = {1, 2};
    const auto fractional = evaluate_buy(relation, books);
    assert(fractional.reject == HotReject::Accepted
           && fractional.quantity_microunits == 10 * kShareMicrounits);
}
