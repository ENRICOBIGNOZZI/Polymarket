#include "pm/v7_pure_arb_lane.hpp"

#include <cassert>
#include <cmath>

using namespace pm::v7;
using namespace pm::v7::pure_arb;

int main() {
    {
        BookHotSnapshot yes{}, no{};
        yes.ask_levels[0] = {4000, 2'000'000};
        yes.ask_levels[1] = {4100, 3'000'000};
        yes.ask_level_count = 2;
        no.ask_levels[0] = {5000, 5'000'000};
        no.ask_level_count = 1;

        const auto result = sweep(
            yes, no, 0.0, 1.0, 0.0005, true, 10'000'000);
        assert(result.shares_microunits == 5'000'000);
        assert(result.yes_levels_used == 2);
        assert(result.no_levels_used == 1);
        assert(std::abs(result.yes_vwap() - 0.406) < 1e-12);
        assert(std::abs(result.no_vwap() - 0.5) < 1e-12);
        assert(std::abs(result.gross_locked_pnl - 0.47) < 1e-12);
        assert(std::abs(result.conservative_locked_pnl - 0.4675) < 1e-12);
        assert(std::abs(result.marginal_edge_per_share - 0.09) < 1e-12);
    }
    {
        BookHotSnapshot yes{}, no{};
        yes.bid_levels[0] = {6000, 10'000'000};
        yes.bid_level_count = 1;
        no.bid_levels[0] = {4500, 10'000'000};
        no.bid_level_count = 1;

        const auto result = sweep(
            yes, no, 0.0, 1.0, 0.001, false, 4'000'000);
        assert(result.shares_microunits == 4'000'000);
        assert(std::abs(result.gross_edge_per_share() - 0.05) < 1e-12);
        assert(std::abs(result.conservative_edge_per_share() - 0.049) < 1e-12);
    }
    {
        assert(fee_usdc(0.001, 0.5, 0.02, 1.0) == 0.0);
        assert(std::abs(fee_usdc(10.0, 0.5, 0.02, 1.0) - 0.05) < 1e-12);
        assert(std::isnan(fee_usdc(1.0, 0.0, 0.02, 1.0)));
    }
    {
        BookDeepSnapshot yes{}, no{};
        yes.ask_levels[0] = {3000, 1'000'000};
        yes.ask_level_count = 1;
        no.ask_levels[0] = {6500, 1'000'000};
        no.ask_level_count = 1;
        const auto result = sweep(
            yes, no, 0.0, 1.0, 0.0005, true, 1'000'000);
        assert(result.shares_microunits == 1'000'000);
        assert(std::abs(result.gross_edge_per_share() - 0.05) < 1e-12);
    }
    return 0;
}
