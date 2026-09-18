#include "pm/v7_native_settlement_oms_endpoint.hpp"
#include <cassert>
#include <iostream>
using namespace pm::v7;

CapitalLimits limits() {
    return {20'000'000, 20'000'000, 20'000'000, 10'000'000};
}
ExecutionPlan plan(std::uint64_t id, bool maker = false) {
    ExecutionPlan p;
    p.intent.intent_id = id;
    p.intent.market_handle = 7;
    p.intent.event_handle = 8;
    p.intent.instrument_handle = 11;
    p.intent.state_version = 9;
    p.intent.decision_monotonic_ns = 1'000;
    p.intent.price_tick = 40;
    p.intent.quantity_microunits = 5'000'000;
    p.intent.strategy_id = maker ? StrategyId::ProfessionalMaker : StrategyId::CryptoInformedTaker;
    p.intent.type = maker ? IntentType::Quote : IntentType::TargetPosition;
    p.intent.side = Side::Buy;
    p.intent.urgency = maker ? Urgency::Passive : Urgency::Aggressive;
    p.intent.purpose = IntentPurpose::Alpha;
    p.intent.passive = maker;
    p.intent.post_only = maker;
    p.tick_size_e4 = 100;
    p.market_state_version = 9;
    p.policy = maker ? ExecutionPolicyId::PassiveMaker : ExecutionPolicyId::AggressiveTaker;
    return p;
}
OmsEvent event(OmsEventType type, std::int64_t now, std::int64_t fill = 0) {
    OmsEvent e; e.type = type; e.timestamp_ns = now;
    e.fill_delta_microunits = fill;
    if (type == OmsEventType::AckLive) e.exchange_order_handle = 9001;
    return e;
}

void unsent_churn_is_terminal_and_does_not_trade() {
    NativeSettlementAuthority owner(limits());
    NativeSettlementOmsEndpoint endpoint(owner);
    for (std::uint64_t i = 1; i <= 8192; ++i) {
        const auto submitted = owner.submit(plan(i, i % 2 == 0), 1'000'000, 2'000 + i);
        assert(submitted.accepted);
        const auto id = submitted.tx.command.client_order_id;
        assert(endpoint.observe_unsent(submitted.tx.command, 3'000 + i));
        assert(owner.active_orders() == 0);
        assert(owner.find_order(id) == nullptr);
        const auto* terminal = endpoint.find(id);
        assert(terminal && terminal->state == OrderState::Rejected);
        assert(terminal->wire_ns == 0 && terminal->ack_ns == 0 && terminal->filled_microunits == 0);
        assert(owner.capital_snapshot().total_exposure_microdollars == 0);
        assert(owner.inventory_snapshot(11).total_microunits == 0);
    }
    assert(endpoint.observed_unsent() == 8192 && endpoint.healthy());
}

void actual_adapter_events_reach_common_inventory_and_capital() {
    NativeSettlementAuthority owner(limits());
    NativeSettlementOmsEndpoint endpoint(owner);
    const auto submitted = owner.submit(plan(1), 1'000'000, 2'000);
    assert(submitted.accepted);
    const auto id = submitted.tx.command.client_order_id;
    assert(endpoint.apply_owned(id,event(OmsEventType::WireSend,2100)).state == OrderState::AckPending);
    assert(endpoint.apply_owned(id,event(OmsEventType::AckLive,2200)).state == OrderState::Live);
    assert(endpoint.apply_owned(id,event(OmsEventType::FillDelta,2300,2'000'000)).state == OrderState::Partial);
    assert(owner.inventory_snapshot(11).total_microunits == 2'000'000);
    assert(owner.capital_snapshot().inventory_committed_microdollars == 800'000);
    assert(owner.capital_snapshot().order_reserved_microdollars == 1'200'000);
    assert(endpoint.apply_owned(id,event(OmsEventType::FillDelta,2400,3'000'000)).state == OrderState::Filled);
    assert(owner.active_orders() == 0 && owner.find_order(id) == nullptr);
    const auto* terminal = endpoint.find(id);
    assert(terminal && terminal->state == OrderState::Filled);
    assert(terminal->filled_microunits == 5'000'000 && terminal->remaining_microunits == 0);
    assert(owner.capital_snapshot().order_reserved_microdollars == 0);
    assert(owner.capital_snapshot().inventory_committed_microdollars == 2'000'000);
    assert(endpoint.observed_unsent() == 0 && endpoint.healthy());
}

void malformed_handoff_is_quarantined_without_mutation() {
    NativeSettlementAuthority owner(limits());
    NativeSettlementOmsEndpoint endpoint(owner);
    const auto submitted = owner.submit(plan(1), 1'000'000, 2'000);
    auto wrong = submitted.tx.command;
    ++wrong.quantity_microunits;
    assert(!endpoint.observe_unsent(wrong,3'000));
    assert(!endpoint.healthy() && endpoint.invalid_commands() == 1);
    assert(owner.active_orders() == 1);
    assert(owner.find_order(wrong.client_order_id)->state == OrderState::SendPending);
    assert(owner.capital_snapshot().order_reserved_microdollars == 2'000'000);
    assert(!endpoint.observe_unsent(submitted.tx.command,3'100));
}

void every_admitted_command_field_is_bound_before_wire() {
    NativeSettlementAuthority owner(limits());
    NativeSettlementOmsEndpoint endpoint(owner);
    const auto submitted = owner.submit(plan(1), 1'000'000, 2'000);
    const auto original = submitted.tx.command;
    assert(endpoint.matches_pending_command(original));
    auto changed = original; ++changed.tick_size_e4;
    assert(!endpoint.matches_pending_command(changed));
    changed = original; ++changed.quantity_microunits;
    assert(!endpoint.matches_pending_command(changed));
    changed = original; ++changed.price_tick;
    assert(!endpoint.matches_pending_command(changed));
    changed = original; changed.time_in_force = AdapterTimeInForce::Gtc;
    assert(!endpoint.matches_pending_command(changed));
    changed = original; changed.post_only = 1;
    assert(!endpoint.matches_pending_command(changed));
    changed = original; ++changed.market_state_version;
    assert(!endpoint.matches_pending_command(changed));
    assert(owner.active_orders() == 1);
    assert(endpoint.observe_unsent(original,3000));
    assert(!endpoint.matches_pending_command(original));
}

void foreign_or_inconsistent_events_fail_closed() {
    NativeSettlementAuthority owner(limits());
    NativeSettlementOmsEndpoint endpoint(owner);
    const auto result = endpoint.apply_owned(999,event(OmsEventType::AckLive,2'000));
    assert(result.invariant_violation && result.reconciliation_required && !endpoint.healthy());
    assert(owner.active_orders() == 0);
}

int main() {
    unsent_churn_is_terminal_and_does_not_trade();
    actual_adapter_events_reach_common_inventory_and_capital();
    malformed_handoff_is_quarantined_without_mutation();
    every_admitted_command_field_is_bound_before_wire();
    foreign_or_inconsistent_events_fail_closed();
    std::cout << "native settlement OMS endpoint PASS\n";
}
