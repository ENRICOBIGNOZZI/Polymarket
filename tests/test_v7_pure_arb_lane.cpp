#include "pm/v7_pure_arb_lane.hpp"

#include <bit>
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
        assert(result.yes_limit_price_e4 == 4100);
        assert(result.no_limit_price_e4 == 5000);
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
        Context context{};
        context.market_handle = 7;
        context.event_handle = 8;
        context.yes_instrument_handle = 11;
        context.no_instrument_handle = 12;
        context.fee_rate = 0.0;
        context.fee_exponent = 1.0;
        context.reserve_per_share = 0.0005;
        context.minimum_order_microunits = 1'000'000;
        context.maximum_leg_skew_ns = 100'000'000;
        context.sell_capacity_microunits = 20'000'000;
        context.fee_verified = 1;
        PureArbLane lane(context);
        assert(lane.valid());

        PairInput input{};
        input.yes.valid = input.no.valid = 1;
        input.yes.lineage_continuous = input.no.lineage_continuous = 1;
        input.yes.state_version = 101;
        input.no.state_version = 202;
        input.yes.receive_monotonic_ns = 1'000'000;
        input.no.receive_monotonic_ns = 1'000'100;
        input.yes_connection_epoch = input.no_connection_epoch = 3;
        input.decode_complete_monotonic_ns = 1'000'150;
        input.decision_monotonic_ns = 1'000'200;

        input.yes.best_bid_e4 = 3900;
        input.yes.best_ask_e4 = 4000;
        input.yes.best_bid_microunits = 10'000'000;
        input.yes.best_ask_microunits = 5'000'000;
        input.yes.bid_levels[0] = {3900, 10'000'000};
        input.yes.bid_level_count = 1;
        input.yes.ask_levels[0] = {4000, 2'000'000};
        input.yes.ask_levels[1] = {4100, 3'000'000};
        input.yes.ask_level_count = 2;

        input.no.best_bid_e4 = 4900;
        input.no.best_ask_e4 = 5000;
        input.no.best_bid_microunits = 10'000'000;
        input.no.best_ask_microunits = 5'000'000;
        input.no.bid_levels[0] = {4900, 10'000'000};
        input.no.bid_level_count = 1;
        input.no.ask_levels[0] = {5000, 5'000'000};
        input.no.ask_level_count = 1;

        // The legacy detector economics are exactly the shared sweep kernel.
        const auto legacy = sweep(
            input.yes, input.no,
            context.fee_rate, context.fee_exponent,
            context.reserve_per_share, true);
        const auto result = lane.evaluate(input);
        assert(result.reason == RejectReason::Accepted);
        assert(result.plan.valid == 1);
        assert(result.plan.direction == Direction::BuyCompleteSet);
        assert(result.plan.trigger_receive_monotonic_ns == input.no.receive_monotonic_ns);
        assert(result.plan.yes.instrument_handle == 11);
        assert(result.plan.no.instrument_handle == 12);
        assert(result.plan.yes.side == Side::Buy);
        assert(result.plan.no.side == Side::Buy);
        assert(result.plan.yes.quantity_microunits == legacy.shares_microunits);
        assert(result.plan.no.quantity_microunits == legacy.shares_microunits);
        assert(result.plan.yes.limit_price_e4 == legacy.yes_limit_price_e4);
        assert(result.plan.no.limit_price_e4 == legacy.no_limit_price_e4);
        assert(std::bit_cast<std::uint64_t>(result.plan.economics.gross_locked_pnl)
            == std::bit_cast<std::uint64_t>(legacy.gross_locked_pnl));
        assert(std::bit_cast<std::uint64_t>(result.plan.economics.conservative_locked_pnl)
            == std::bit_cast<std::uint64_t>(legacy.conservative_locked_pnl));
        assert(std::bit_cast<std::uint64_t>(result.plan.economics.yes_notional)
            == std::bit_cast<std::uint64_t>(legacy.yes_notional));
        assert(std::bit_cast<std::uint64_t>(result.plan.economics.no_notional)
            == std::bit_cast<std::uint64_t>(legacy.no_notional));
        assert(result.plan.decode_complete_monotonic_ns == input.decode_complete_monotonic_ns);
        ExecutionPlan yes_plan{}, no_plan{};
        assert(make_execution_plan(result.plan, true, 101, yes_plan));
        assert(make_execution_plan(result.plan, false, 102, no_plan));
        assert(yes_plan.policy == ExecutionPolicyId::PureArbFok);
        assert(no_plan.policy == ExecutionPolicyId::PureArbFok);
        assert(yes_plan.intent.strategy_id == StrategyId::HardArbitrage);
        assert(no_plan.intent.strategy_id == StrategyId::HardArbitrage);
        assert(yes_plan.intent.quantity_microunits == no_plan.intent.quantity_microunits);
        assert(yes_plan.intent.decode_complete_monotonic_ns == input.decode_complete_monotonic_ns);
        assert(yes_plan.intent.price_tick == result.plan.yes.limit_price_e4 / input.yes.tick_size_e4);
        assert(no_plan.intent.price_tick == result.plan.no.limit_price_e4 / input.no.tick_size_e4);

        auto bad_epoch = input;
        bad_epoch.no_connection_epoch = 4;
        assert(lane.evaluate(bad_epoch).reason == RejectReason::EpochMismatch);

        auto stale = input;
        stale.no.receive_monotonic_ns =
            input.yes.receive_monotonic_ns + context.maximum_leg_skew_ns + 1;
        assert(lane.evaluate(stale).reason == RejectReason::LegSkewExceeded);

        auto broken = input;
        broken.no.lineage_continuous = 0;
        assert(lane.evaluate(broken).reason == RejectReason::LineageInvalid);

        auto too_small_context = context;
        too_small_context.minimum_order_microunits = 6'000'000;
        PureArbLane too_small(too_small_context);
        assert(too_small.evaluate(input).reason == RejectReason::BelowVenueMinimum);
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
