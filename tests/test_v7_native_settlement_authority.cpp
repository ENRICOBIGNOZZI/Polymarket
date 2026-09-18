#include "pm/v7_native_settlement_authority.hpp"

#include <cassert>

using namespace pm::v7;

namespace {

ExecutionPlan plan(std::uint64_t id, StrategyId strategy, IntentType type,
                   Side side, ExecutionPolicyId policy,
                   std::int64_t quantity = 5'000'000) {
    ExecutionPlan out;
    out.intent.intent_id = id;
    out.intent.market_handle = 7;
    out.intent.event_handle = 8;
    out.intent.instrument_handle = side == Side::Buy ? 11 : 12;
    out.intent.state_version = 9;
    out.intent.decision_monotonic_ns = 1'000;
    out.intent.exchange_event_ns = 900;
    out.intent.price_tick = 40;
    out.intent.quantity_microunits = quantity;
    out.intent.strategy_id = strategy;
    out.intent.type = type;
    out.intent.side = side;
    out.intent.urgency = policy == ExecutionPolicyId::AggressiveTaker
        ? Urgency::Aggressive : Urgency::Passive;
    out.intent.purpose = IntentPurpose::Alpha;
    out.intent.passive = policy == ExecutionPolicyId::PassiveMaker;
    out.intent.post_only = out.intent.passive;
    out.tick_size_e4 = 100;
    out.market_state_version = 9;
    out.policy = policy;
    return out;
}

CapitalLimits limits() {
    CapitalLimits out;
    out.sleeve_budget_microdollars = 20'000'000;
    out.max_total_exposure_microdollars = 20'000'000;
    out.max_market_exposure_microdollars = 20'000'000;
    out.max_single_order_microdollars = 10'000'000;
    return out;
}

void test_one_authority_accepts_maker_and_taker_buy_paths() {
    NativeSettlementAuthority authority(limits());
    const auto taker = authority.submit(
        plan(1, StrategyId::CryptoInformedTaker, IntentType::TargetPosition,
             Side::Buy, ExecutionPolicyId::AggressiveTaker),
        5'000'000, 2'000);
    assert(taker.accepted == 1);
    assert(taker.tx.oms.state == OrderState::SendPending);

    const auto maker = authority.submit(
        plan(2, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker),
        5'000'000, 2'100);
    assert(maker.accepted == 1);
    assert(maker.tx.oms.state == OrderState::SendPending);
    assert(authority.active_orders() == 2);
}

void test_fail_closed_inventory_and_venue_minimum() {
    NativeSettlementAuthority authority(limits());
    const auto sell = authority.submit(
        plan(3, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Sell, ExecutionPolicyId::PassiveMaker),
        5'000'000, 2'000);
    assert(sell.accepted == 0);
    assert(sell.reason == NativeSettlementAuthorityReason::InventoryUnavailable);

    const auto small = authority.submit(
        plan(4, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 1'000'000),
        5'000'000, 2'000);
    assert(small.accepted == 0);
    assert(small.reason == NativeSettlementAuthorityReason::BelowVenueMinimum);
    assert(authority.active_orders() == 0);
}

} // namespace

int main() {
    test_one_authority_accepts_maker_and_taker_buy_paths();
    test_fail_closed_inventory_and_venue_minimum();
    return 0;
}
