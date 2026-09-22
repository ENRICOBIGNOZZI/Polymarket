#include "pm/v7_pure_arb_lane.hpp"

#include <cassert>
#include <cmath>

using namespace pm::v7;
using namespace pm::v7::pure_arb;

namespace {
bool close(double a, double b, double eps = 1e-12) {
    return std::abs(a - b) <= eps;
}

BookHotSnapshot make_book() {
    BookHotSnapshot b{};
    b.valid = 1;
    b.lineage_continuous = 1;
    b.tick_size_e4 = 100;
    return b;
}
} // namespace

int main() {
    assert(fee_usdc(0.001, 0.5, 0.02, 1.0) == 0.0);
    assert(close(fee_usdc(10.0, 0.5, 0.02, 1.0), 0.05));

    auto yes = make_book();
    auto no = make_book();
    yes.best_bid_e4 = 3900;
    yes.best_ask_e4 = 4000;
    yes.best_bid_microunits = 5'000'000;
    yes.best_ask_microunits = 5'000'000;
    no.best_bid_e4 = 4900;
    no.best_ask_e4 = 5000;
    no.best_bid_microunits = 5'000'000;
    no.best_ask_microunits = 5'000'000;

    // L10 asks: 2 @ .40, then 3 @ .41 versus 4 @ .50, then 2 @ .51.
    yes.ask_levels[0] = {4000, 2'000'000};
    yes.ask_levels[1] = {4100, 3'000'000};
    yes.ask_level_count = 2;
    no.ask_levels[0] = {5000, 4'000'000};
    no.ask_levels[1] = {5100, 2'000'000};
    no.ask_level_count = 2;

    const Terms zero_fee{0.0, 1.0, 0.001};
    const auto l1 = evaluate_l1(yes, no, zero_fee);
    assert(l1.valid == 1);
    assert(close(l1.buy_raw_edge_per_share, 0.10));
    assert(close(l1.sell_raw_edge_per_share, -0.12));
    assert(close(l1.buy_edge_per_share, 0.10));
    assert(close(l1.sell_edge_per_share, -0.12));
    assert(close(l1.buy_executable_shares, 5.0));
    assert(close(l1.sell_executable_shares, 5.0));

    const auto buy = sweep(yes, no, zero_fee, true);
    assert(buy.shares_microunits == 5'000'000);
    assert(buy.yes_levels_used == 2);
    assert(buy.no_levels_used == 2);
    assert(close(buy.gross_locked_pnl, 0.46));
    assert(close(buy.conservative_locked_pnl, 0.455));
    assert(close(buy.yes_vwap(), 0.406));
    assert(close(buy.no_vwap(), 0.502));
    assert(close(buy.marginal_edge_per_share, 0.08));

    const auto capped = sweep(yes, no, zero_fee, true, 3'000'000);
    assert(capped.shares_microunits == 3'000'000);
    assert(close(capped.gross_locked_pnl, 0.29));
    assert(close(capped.conservative_locked_pnl, 0.287));

    // SELL needs a different, non-crossed book. A single valid book cannot
    // simultaneously have the large BUY and SELL complete-set edges above.
    auto sell_yes = make_book();
    auto sell_no = make_book();
    sell_yes.best_bid_e4 = 6000;
    sell_yes.best_ask_e4 = 6100;
    sell_yes.best_bid_microunits = 6'000'000;
    sell_yes.best_ask_microunits = 5'000'000;
    sell_no.best_bid_e4 = 4500;
    sell_no.best_ask_e4 = 4600;
    sell_no.best_bid_microunits = 6'000'000;
    sell_no.best_ask_microunits = 5'000'000;
    sell_yes.bid_levels[0] = {6000, 3'000'000};
    sell_yes.bid_levels[1] = {5900, 3'000'000};
    sell_yes.bid_level_count = 2;
    sell_no.bid_levels[0] = {4500, 4'000'000};
    sell_no.bid_levels[1] = {4400, 2'000'000};
    sell_no.bid_level_count = 2;
    const auto sell_l1 = evaluate_l1(sell_yes, sell_no, zero_fee);
    assert(sell_l1.valid == 1);
    assert(close(sell_l1.sell_raw_edge_per_share, 0.05));
    assert(close(sell_l1.sell_edge_per_share, 0.05));
    const auto sell = sweep(sell_yes, sell_no, zero_fee, false);
    assert(sell.shares_microunits == 6'000'000);
    assert(close(sell.gross_locked_pnl, 0.25));
    assert(close(sell.conservative_locked_pnl, 0.244));

    // Hot/deep use one identical economic kernel.
    BookDeepSnapshot yes_deep{}, no_deep{};
    yes_deep.valid = no_deep.valid = 1;
    yes_deep.lineage_continuous = no_deep.lineage_continuous = 1;
    yes_deep.ask_levels[0] = yes.ask_levels[0];
    yes_deep.ask_levels[1] = yes.ask_levels[1];
    yes_deep.ask_level_count = yes.ask_level_count;
    no_deep.ask_levels[0] = no.ask_levels[0];
    no_deep.ask_levels[1] = no.ask_levels[1];
    no_deep.ask_level_count = no.ask_level_count;
    const auto deep_buy = sweep(yes_deep, no_deep, zero_fee, true);
    assert(deep_buy.shares_microunits == buy.shares_microunits);
    assert(close(deep_buy.gross_locked_pnl, buy.gross_locked_pnl));
    assert(close(deep_buy.yes_vwap(), buy.yes_vwap()));
    assert(close(deep_buy.no_vwap(), buy.no_vwap()));

    // Invalid causal state never produces a valid L1 evaluation.
    yes.lineage_continuous = 0;
    assert(evaluate_l1(yes, no, zero_fee).valid == 0);

    return 0;
}
