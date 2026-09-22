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

    {
        PairInput input{};
        input.market_handle = 7;
        input.event_handle = 8;
        input.yes_instrument_handle = 11;
        input.no_instrument_handle = 12;
        input.yes_epoch = input.no_epoch = 3;
        input.market_start_wall_ms = 1'000;
        input.market_end_wall_ms = 2'000;
        input.now_wall_ms = 1'500;
        input.trigger_receive_monotonic_ns = 10'000;
        input.decision_monotonic_ns = 12'000;
        input.maximum_leg_skew_ns = 100'000'000;
        input.minimum_order_microunits = 5'000'000;
        input.fee_verified = 1;
        input.fee_rate = 0.0;
        input.fee_exponent = 1.0;
        input.reserve_per_share = 0.0005;
        input.yes.valid = input.no.valid = 1;
        input.yes.lineage_continuous = input.no.lineage_continuous = 1;
        input.yes.receive_monotonic_ns = 9'500;
        input.no.receive_monotonic_ns = 9'000;
        input.yes.tick_size_e4 = input.no.tick_size_e4 = 100;
        input.yes.state_version = 21;
        input.no.state_version = 22;
        input.yes.best_bid_e4 = 3900;
        input.yes.best_ask_e4 = 4000;
        input.no.best_bid_e4 = 4900;
        input.no.best_ask_e4 = 5000;
        input.yes.best_bid_microunits = input.no.best_bid_microunits = 10'000'000;
        input.yes.best_ask_microunits = input.no.best_ask_microunits = 10'000'000;
        input.yes.bid_levels[0] = {3900,10'000'000};
        input.no.bid_levels[0] = {4900,10'000'000};
        input.yes.ask_levels[0] = {4000,6'000'000};
        input.yes.ask_levels[1] = {4100,4'000'000};
        input.no.ask_levels[0] = {5000,10'000'000};
        input.yes.bid_level_count = input.no.bid_level_count = 1;
        input.yes.ask_level_count = 2;
        input.no.ask_level_count = 1;
        const auto plan = evaluate_pair(input);
        assert(plan.accepted == 1);
        assert(plan.direction == Direction::BuyCompleteSet);
        assert(plan.reason == DecisionReason::Accepted);
        assert(plan.economics.shares_microunits == 10'000'000);
        assert(plan.yes.quantity_microunits == plan.no.quantity_microunits);
        assert(plan.yes.limit_price_e4 == 4100);
        assert(plan.no.limit_price_e4 == 5000);
        assert(plan.yes.market_state_version == 21);
        assert(plan.no.market_state_version == 22);
        assert(plan.yes.side == Side::Buy && plan.no.side == Side::Buy);
        ExecutionPlan yes_execution{}, no_execution{};
        assert(make_execution_plan(plan, true, 101, yes_execution));
        assert(make_execution_plan(plan, false, 102, no_execution));
        assert(yes_execution.policy == ExecutionPolicyId::PureArbFok);
        assert(no_execution.policy == ExecutionPolicyId::PureArbFok);
        assert(yes_execution.intent.strategy_id == StrategyId::HardArbitrage);
        assert(no_execution.intent.strategy_id == StrategyId::HardArbitrage);
        assert(yes_execution.intent.price_tick == 41);
        assert(no_execution.intent.price_tick == 50);
        assert(yes_execution.intent.quantity_microunits
               == no_execution.intent.quantity_microunits);

        auto stale = input;
        stale.no.receive_monotonic_ns =
            stale.yes.receive_monotonic_ns - stale.maximum_leg_skew_ns - 1;
        assert(evaluate_pair(stale).reason == DecisionReason::LegSkewExceeded);

        auto no_fee = input;
        no_fee.fee_verified = 0;
        assert(evaluate_pair(no_fee).reason == DecisionReason::FeeUnverified);

        auto too_small = input;
        too_small.minimum_order_microunits = 11'000'000;
        assert(evaluate_pair(too_small).reason == DecisionReason::BelowVenueMinimum);
    }
    return 0;
}
