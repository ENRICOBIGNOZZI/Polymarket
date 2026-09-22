#include "pm/v7_native_settlement_authority.hpp"
#include "pm/v7_native_maker_context.hpp"

#include <cassert>

using namespace pm::v7;

namespace {

ExecutionPlan plan(std::uint64_t id, StrategyId strategy, IntentType type,
                   Side side, ExecutionPolicyId policy,
                   std::int64_t quantity = 5'000'000,
                   std::int64_t price_tick = 40,
                   std::uint64_t instrument = 11) {
    ExecutionPlan out;
    out.intent.intent_id = id;
    out.intent.market_handle = 7;
    out.intent.event_handle = 8;
    out.intent.instrument_handle = instrument;
    out.intent.state_version = 9;
    out.intent.decision_monotonic_ns = 1'000;
    out.intent.exchange_event_ns = 900;
    out.intent.price_tick = price_tick;
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

OmsEvent event(OmsEventType type, std::int64_t ns,
               std::int64_t exchange_order_handle = 0,
               std::int64_t fill_delta = 0) {
    OmsEvent out;
    out.type = type;
    out.timestamp_ns = ns;
    out.exchange_order_handle = exchange_order_handle;
    out.fill_delta_microunits = fill_delta;
    return out;
}

void make_live(NativeSettlementAuthority& authority,
               std::uint64_t client_order_id,
               std::int64_t exchange_order_handle,
               std::int64_t start_ns) {
    const auto wire = authority.apply_order_event(
        client_order_id, event(OmsEventType::WireSend, start_ns));
    assert(wire.reason == NativeSettlementAuthorityReason::Accepted);
    assert(wire.transition.state == OrderState::AckPending);
    const auto ack = authority.apply_order_event(
        client_order_id, event(OmsEventType::AckLive, start_ns + 100, exchange_order_handle));
    assert(ack.reason == NativeSettlementAuthorityReason::Accepted);
    assert(ack.transition.state == OrderState::Live);
}

void test_one_authority_accepts_maker_and_taker_buy_paths() {
    NativeSettlementAuthority authority(limits());
    const auto taker = authority.submit(
        plan(1, StrategyId::CryptoInformedTaker, IntentType::TargetPosition,
             Side::Buy, ExecutionPolicyId::AggressiveTaker),
        5'000'000, 2'000);
    assert(taker.accepted == 1);
    assert(taker.risk_admitted_monotonic_ns >= taker.tx.command.decision_monotonic_ns);
    assert(taker.tx.command.risk_admitted_monotonic_ns == taker.risk_admitted_monotonic_ns);
    assert(taker.tx.command.queue_monotonic_ns >= taker.risk_admitted_monotonic_ns);
    assert(taker.tx.oms.state == OrderState::SendPending);

    const auto maker = authority.submit(
        plan(2, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker),
        5'000'000, 2'100);
    assert(maker.accepted == 1);
    assert(maker.tx.oms.state == OrderState::SendPending);
    assert(authority.active_orders() == 2);
}

void test_fail_closed_without_inventory_and_venue_minimum() {
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

void test_inventory_sync_buy_fill_and_sell_fill_share_one_capital_owner() {
    NativeSettlementAuthority authority(limits());
    assert(authority.sync_inventory(7, 11, 0, 0, 1));

    const auto buy = authority.submit(
        plan(10, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker),
        1'000'000, 2'000);
    assert(buy.accepted);
    make_live(authority, buy.tx.command.client_order_id, 9001, 2'100);

    const auto fill = authority.apply_order_event(
        buy.tx.command.client_order_id,
        event(OmsEventType::FillDelta, 2'300, 0, 2'000'000));
    assert(fill.reason == NativeSettlementAuthorityReason::Accepted);
    assert(fill.transition.state == OrderState::Partial);
    auto context = native_maker_context(authority, 11, 12, 11, {});
    assert(context.inventory.yes_shares == 2.0);
    assert(context.inventory.reserved_buy_shares == 3.0);
    assert(context.quotes.bid_active && context.quotes.bid_tick == 40);
    auto inv = authority.inventory_snapshot(11);
    assert(inv.total_microunits == 2'000'000);
    assert(inv.collateral_basis_microdollars == 800'000); // 2 shares at 0.40.
    auto capital = authority.capital_snapshot();
    assert(capital.inventory_committed_microdollars == 800'000);
    assert(capital.order_reserved_microdollars == 1'200'000);

    const auto cancel = authority.cancel_maker_quote(11, Side::Buy, 2'400);
    assert(cancel.accepted);
    assert(cancel.command.exchange_order_handle == 9001);
    assert(authority.apply_order_event(
        buy.tx.command.client_order_id,
        event(OmsEventType::WireCancel, 2'500)).transition.state == OrderState::CancelPending);
    const auto cancelled = authority.apply_order_event(
        buy.tx.command.client_order_id,
        event(OmsEventType::AckCancel, 2'600));
    assert(cancelled.terminal_retired);
    context = native_maker_context(authority, 11, 12, 11, {});
    assert(!context.quotes.bid_active && context.inventory.reserved_buy_shares == 0.0);
    assert(context.inventory.yes_shares == 2.0);
    capital = authority.capital_snapshot();
    assert(capital.order_reserved_microdollars == 0);
    assert(capital.inventory_committed_microdollars == 800'000);

    const auto sell = authority.submit(
        plan(11, StrategyId::CryptoInformedTaker, IntentType::TargetPosition,
             Side::Sell, ExecutionPolicyId::AggressiveTaker, 1'000'000),
        1'000'000, 2'700);
    assert(sell.accepted);
    inv = authority.inventory_snapshot(11);
    assert(inv.reserved_sell_microunits == 1'000'000);
    assert(inv.available_microunits == 1'000'000);
    make_live(authority, sell.tx.command.client_order_id, 9002, 2'800);
    const auto sold = authority.apply_order_event(
        sell.tx.command.client_order_id,
        event(OmsEventType::FillDelta, 3'000, 0, 1'000'000));
    assert(sold.reason == NativeSettlementAuthorityReason::Accepted);
    inv = authority.inventory_snapshot(11);
    assert(inv.total_microunits == 1'000'000);
    assert(inv.reserved_sell_microunits == 0);
    assert(inv.collateral_basis_microdollars == 400'000);
    capital = authority.capital_snapshot();
    assert(capital.inventory_committed_microdollars == 400'000);
}

void test_maker_replace_is_cancel_first_and_never_parallel() {
    NativeSettlementAuthority authority(limits());
    const auto first = authority.submit(
        plan(20, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 2'000'000, 40),
        1'000'000, 2'000);
    assert(first.accepted);
    const auto duplicate = authority.submit(
        plan(21, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 2'000'000, 40),
        1'000'000, 2'050);
    assert(!duplicate.accepted);
    assert(duplicate.reason == NativeSettlementAuthorityReason::DuplicateMakerQuote);
    assert(authority.active_orders() == 1);

    // Before exchange acknowledgement, replacement is fail-closed and cannot
    // synthesize a cancel identity.
    const auto early_replace = authority.submit(
        plan(22, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 2'000'000, 41),
        1'000'000, 2'100);
    assert(!early_replace.accepted);
    assert(early_replace.reason == NativeSettlementAuthorityReason::MakerReplacePending);
    assert(early_replace.cancel.reason == NativeCancelTxReason::NotCancelable);
    assert(authority.active_orders() == 1);

    make_live(authority, first.tx.command.client_order_id, 9100, 2'200);
    const auto replace = authority.submit(
        plan(23, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 2'000'000, 41),
        1'000'000, 2'400);
    assert(!replace.accepted);
    assert(replace.reason == NativeSettlementAuthorityReason::MakerReplacePending);
    assert(replace.cancel.accepted);
    assert(authority.active_orders() == 1);

    assert(authority.apply_order_event(
        first.tx.command.client_order_id,
        event(OmsEventType::WireCancel, 2'500)).transition.state == OrderState::CancelPending);
    const auto terminal_cancel = authority.apply_order_event(
        first.tx.command.client_order_id,
        event(OmsEventType::AckCancel, 2'600));
    assert(terminal_cancel.terminal_retired);
    assert(authority.active_orders() == 0);

    const auto fresh = authority.submit(
        plan(24, StrategyId::ProfessionalMaker, IntentType::Quote,
             Side::Buy, ExecutionPolicyId::PassiveMaker, 2'000'000, 41),
        1'000'000, 2'700);
    assert(fresh.accepted);
    assert(authority.active_orders() == 1);
}

void test_inventory_sync_rejects_active_order_overwrite() {
    NativeSettlementAuthority authority(limits());
    assert(authority.sync_inventory(7, 11, 3'000'000, 1'200'000, 1));
    const auto sell = authority.submit(
        plan(30, StrategyId::CryptoInformedTaker, IntentType::TargetPosition,
             Side::Sell, ExecutionPolicyId::AggressiveTaker, 1'000'000),
        1'000'000, 2'000);
    assert(sell.accepted);
    assert(!authority.sync_inventory(7, 11, 4'000'000, 1'600'000, 2));
}

} // namespace

int main() {
    assert(native_maker_admissible_quantity(1'000'000, 5'000'000, 1'000'000, 4000, 10'000'000, 10'000'000, 10'000'000) == 0);
    assert(native_maker_admissible_quantity(1'000'000, 5'000'000, 5'000'000, 4000, 10'000'000, 10'000'000, 10'000'000) == 5'000'000);
    assert(native_maker_admissible_quantity(1'000'000, 5'000'000, 5'000'000, 4000, 1'000'000, 10'000'000, 10'000'000) == 0);
    assert(native_maker_admissible_quantity(1'000'000, 5'000'000, 5'000'000, 4000, 10'000'000, 10'000'000, 4'000'000) == 0);
    test_one_authority_accepts_maker_and_taker_buy_paths();
    test_fail_closed_without_inventory_and_venue_minimum();
    test_inventory_sync_buy_fill_and_sell_fill_share_one_capital_owner();
    test_maker_replace_is_cancel_first_and_never_parallel();
    test_inventory_sync_rejects_active_order_overwrite();
    return 0;
}
