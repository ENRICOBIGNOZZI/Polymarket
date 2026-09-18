#include "pm/v7_native_paper_execution.hpp"

#include <cassert>
#include <iostream>

using namespace pm::v7;

namespace {
ExecutionPlan plan(std::uint64_t id, StrategyId strategy, IntentType type,
                   Side side, ExecutionPolicyId policy, std::int64_t px,
                   std::int64_t qty = 2'000'000) {
    ExecutionPlan out{};
    out.intent.intent_id = id;
    out.intent.market_handle = 7;
    out.intent.event_handle = 8;
    out.intent.instrument_handle = 11;
    out.intent.state_version = 9;
    out.intent.decision_monotonic_ns = 1'000;
    out.intent.exchange_event_ns = 900;
    out.intent.price_tick = px;
    out.intent.quantity_microunits = qty;
    out.intent.strategy_id = strategy;
    out.intent.type = type;
    out.intent.side = side;
    out.intent.urgency = policy == ExecutionPolicyId::AggressiveTaker ? Urgency::Aggressive : Urgency::Passive;
    out.intent.purpose = IntentPurpose::Alpha;
    out.intent.passive = policy == ExecutionPolicyId::PassiveMaker;
    out.intent.post_only = out.intent.passive;
    out.tick_size_e4 = 100;
    out.market_state_version = 9;
    out.policy = policy;
    return out;
}

CapitalLimits limits() {
    CapitalLimits out{};
    out.sleeve_budget_microdollars = 20'000'000;
    out.max_total_exposure_microdollars = 20'000'000;
    out.max_market_exposure_microdollars = 20'000'000;
    out.max_single_order_microdollars = 10'000'000;
    return out;
}

BookHotSnapshot book() {
    BookHotSnapshot out{};
    out.valid = 1;
    out.lineage_continuous = 1;
    out.tick_size_e4 = 100;
    out.best_bid_e4 = 4000;
    out.best_ask_e4 = 4100;
    out.best_bid_microunits = 5'000'000;
    out.best_ask_microunits = 5'000'000;
    out.bid_levels[0] = {4000, 5'000'000};
    out.ask_levels[0] = {4100, 5'000'000};
    out.bid_level_count = 1;
    out.ask_level_count = 1;
    out.exchange_event_ns = 900;
    out.receive_monotonic_ns = 1'000;
    return out;
}

void test_taker_fills_common_authority() {
    NativeSettlementAuthority authority(limits());
    assert(authority.sync_inventory(7, 11, 0, 0, 1));
    NativeSettlementOmsEndpoint endpoint(authority);
    NativePaperExecutionAdapter paper(endpoint);
    const auto admitted = authority.submit(
        plan(1, StrategyId::CryptoInformedTaker, IntentType::TargetPosition,
             Side::Buy, ExecutionPolicyId::AggressiveTaker, 41),
        1'000'000, 2'000);
    assert(admitted.accepted);
    const auto result = paper.submit(admitted.tx.command, book(), 2'100);
    assert(result.accepted && result.filled_microunits == 2'000'000);
    assert(result.fill.command == admitted.tx.command);
    assert(result.fill.order_state == OrderState::Filled);
    assert(authority.active_orders() == 0);
    const auto inv = authority.inventory_snapshot(11);
    assert(inv.total_microunits == 2'000'000);
    assert(inv.collateral_basis_microdollars == 820'000);
    assert(authority.capital_snapshot().inventory_committed_microdollars == 820'000);
}

void test_maker_queue_and_cancel_latency() {
    NativeSettlementAuthority authority(limits());
    assert(authority.sync_inventory(7, 11, 0, 0, 1));
    NativeSettlementOmsEndpoint endpoint(authority);
    NativePaperExecutionAdapter paper(endpoint, 100);
    const auto admitted = authority.submit(
        plan(2, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 40),
        1'000'000, 2'000);
    assert(admitted.accepted);
    const auto submitted = paper.submit(admitted.tx.command, book(), 2'100);
    assert(submitted.accepted && submitted.resting && paper.resting_orders() == 1);

    PublicTradePrint first{1, 11, Side::Sell, 40, 5'000'000, 901, 2'200};
    assert(paper.on_public_trade(first).fills == 0);

    const auto cancel = authority.cancel_maker_quote(11, Side::Buy, 2'300);
    assert(cancel.accepted);
    assert(paper.request_cancel(cancel.command, 2'300));
    const auto before_cancel = paper.advance_time(2'399);
    assert(!before_cancel.invalid && before_cancel.cancellation_count == 0);
    assert(authority.active_orders() == 1);
    const auto cancelled = paper.advance_time(2'400);
    assert(!cancelled.invalid && cancelled.cancellation_count == 1);
    assert(cancelled.cancellations[0].command.client_order_id
           == admitted.tx.command.client_order_id);
    assert(authority.active_orders() == 0);
    assert(paper.resting_orders() == 0);
}

void test_maker_fill_after_queue_depletion() {
    NativeSettlementAuthority authority(limits());
    assert(authority.sync_inventory(7, 11, 0, 0, 1));
    NativeSettlementOmsEndpoint endpoint(authority);
    NativePaperExecutionAdapter paper(endpoint);
    const auto admitted = authority.submit(
        plan(3, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 40),
        1'000'000, 2'000);
    assert(admitted.accepted);
    assert(paper.submit(admitted.tx.command, book(), 2'100).accepted);

    PublicTradePrint first{1, 11, Side::Sell, 40, 5'000'000, 901, 2'200};
    assert(paper.on_public_trade(first).fills == 0);
    PublicTradePrint second{2, 11, Side::Sell, 40, 2'000'000, 902, 2'300};
    const auto filled = paper.on_public_trade(second);
    assert(filled.fills == 1 && filled.filled_microunits == 2'000'000);
    assert(filled.records[0].command == admitted.tx.command);
    assert(filled.records[0].order_state == OrderState::Filled);
    assert(authority.active_orders() == 0);
    assert(authority.inventory_snapshot(11).total_microunits == 2'000'000);
}

void test_mutated_command_cannot_paper_fill() {
    NativeSettlementAuthority authority(limits());
    assert(authority.sync_inventory(7, 11, 0, 0, 1));
    NativeSettlementOmsEndpoint endpoint(authority);
    NativePaperExecutionAdapter paper(endpoint);
    const auto admitted = authority.submit(
        plan(4, StrategyId::CryptoInformedTaker, IntentType::TargetPosition,
             Side::Buy, ExecutionPolicyId::AggressiveTaker, 41),
        1'000'000, 2'000);
    assert(admitted.accepted);
    auto tampered = admitted.tx.command;
    ++tampered.quantity_microunits;
    const auto result = paper.submit(tampered, book(), 2'100);
    assert(!result.accepted && result.reason == NativePaperReason::InvalidCommand);
    assert(authority.active_orders() == 1);
    assert(endpoint.observe_unsent(admitted.tx.command, 2'200));
    assert(authority.active_orders() == 0);
}
}

int main() {
    test_taker_fills_common_authority();
    test_maker_queue_and_cancel_latency();
    test_maker_fill_after_queue_depletion();
    test_mutated_command_cannot_paper_fill();
    std::cout << "native paper execution PASS\n";
    return 0;
}
