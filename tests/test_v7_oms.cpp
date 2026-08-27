#include "pm/v7_oms.hpp"

#include <cassert>
#include <cstdint>

namespace {

pm::v7::StrategyIntent quote_intent() {
    pm::v7::StrategyIntent intent;
    intent.intent_id = 101;
    intent.market_handle = 11;
    intent.event_handle = 22;
    intent.instrument_handle = 33;
    intent.strategy_id = pm::v7::StrategyId::ProfessionalMaker;
    intent.type = pm::v7::IntentType::Quote;
    intent.side = pm::v7::Side::Buy;
    intent.price_tick = 48;
    intent.quantity_microunits = 10'000'000;
    return intent;
}

pm::v7::OmsEvent event(std::uint64_t id, pm::v7::OmsEventType type,
                       std::int64_t ts, std::uint64_t source_version = 0) {
    pm::v7::OmsEvent out;
    out.event_id = id;
    out.source_version = source_version;
    out.type = type;
    out.timestamp_ns = ts;
    return out;
}

void test_submit_partial_cancel_and_fill_during_cancel_pending() {
    pm::v7::OmsOrder order(quote_intent(), 9001);

    auto e = event(1, pm::v7::OmsEventType::QueueSend, 100, 1);
    assert(order.apply(e).state == pm::v7::OrderState::SendPending);
    e = event(2, pm::v7::OmsEventType::WireSend, 200, 2);
    assert(order.apply(e).state == pm::v7::OrderState::AckPending);
    e = event(3, pm::v7::OmsEventType::AckLive, 300, 3);
    e.exchange_order_handle = 7001;
    assert(order.apply(e).state == pm::v7::OrderState::Live);

    e = event(4, pm::v7::OmsEventType::FillDelta, 400, 4);
    e.fill_delta_microunits = 3'000'000;
    auto result = order.apply(e);
    assert(result.state == pm::v7::OrderState::Partial);
    assert(order.record().filled_microunits == 3'000'000);
    assert(order.record().remaining_microunits == 7'000'000);

    e = event(5, pm::v7::OmsEventType::RequestCancel, 500, 5);
    assert(order.apply(e).state == pm::v7::OrderState::CancelRequested);
    e = event(6, pm::v7::OmsEventType::WireCancel, 600, 6);
    assert(order.apply(e).state == pm::v7::OrderState::CancelPending);

    e = event(7, pm::v7::OmsEventType::FillDelta, 650, 7);
    e.fill_delta_microunits = 2'000'000;
    result = order.apply(e);
    assert(result.state == pm::v7::OrderState::CancelPending);
    assert(order.record().filled_microunits == 5'000'000);
    assert(order.record().remaining_microunits == 5'000'000);

    e = event(8, pm::v7::OmsEventType::AckCancel, 700, 8);
    result = order.apply(e);
    assert(result.state == pm::v7::OrderState::Cancelled);
    assert(order.record().cancel_effective_ns == 700);
}

void test_duplicate_fill_is_idempotent() {
    pm::v7::OmsOrder order(quote_intent(), 9002);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 200, 2)).applied);
    assert(order.apply(event(3, pm::v7::OmsEventType::AckLive, 300, 3)).applied);

    auto fill = event(4, pm::v7::OmsEventType::FillDelta, 400, 4);
    fill.fill_delta_microunits = 2'000'000;
    assert(order.apply(fill).applied);
    const auto after_first = order.record().filled_microunits;
    const auto duplicate = order.apply(fill);
    assert(!duplicate.applied);
    assert(duplicate.duplicate_or_stale);
    assert(order.record().filled_microunits == after_first);
}

void test_unknown_requires_reconciliation_and_can_restore_partial_live() {
    pm::v7::OmsOrder order(quote_intent(), 9003);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 200, 2)).applied);

    auto unknown = event(3, pm::v7::OmsEventType::TransportUnknown, 300, 3);
    auto result = order.apply(unknown);
    assert(result.state == pm::v7::OrderState::Unknown);
    assert(result.reconciliation_required);
    assert(order.record().state != pm::v7::OrderState::Cancelled);

    result = order.apply(event(4, pm::v7::OmsEventType::BeginReconcile, 400, 4));
    assert(result.state == pm::v7::OrderState::Reconciling);

    auto live = event(5, pm::v7::OmsEventType::ReconcileLive, 500, 5);
    live.exchange_order_handle = 7003;
    live.authoritative_filled_microunits = 4'000'000;
    live.authoritative_remaining_microunits = 6'000'000;
    result = order.apply(live);
    assert(result.state == pm::v7::OrderState::Partial);
    assert(!result.reconciliation_required);
    assert(order.record().filled_microunits == 4'000'000);
    assert(order.record().remaining_microunits == 6'000'000);
}

void test_overfill_fails_closed_to_unknown() {
    pm::v7::OmsOrder order(quote_intent(), 9004);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 200, 2)).applied);
    assert(order.apply(event(3, pm::v7::OmsEventType::AckLive, 300, 3)).applied);

    auto overfill = event(4, pm::v7::OmsEventType::FillDelta, 400, 4);
    overfill.fill_delta_microunits = 11'000'000;
    const auto result = order.apply(overfill);
    assert(result.state == pm::v7::OrderState::Unknown);
    assert(result.reconciliation_required);
    assert(result.invariant_violation);
}

void test_reconcile_filled_requires_exact_authoritative_sizes() {
    pm::v7::OmsOrder order(quote_intent(), 9005);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 200, 2)).applied);
    assert(order.apply(event(3, pm::v7::OmsEventType::TransportUnknown, 300, 3)).applied);
    assert(order.apply(event(4, pm::v7::OmsEventType::BeginReconcile, 400, 4)).applied);

    auto filled = event(5, pm::v7::OmsEventType::ReconcileFilled, 500, 5);
    filled.authoritative_filled_microunits = 10'000'000;
    filled.authoritative_remaining_microunits = 0;
    const auto result = order.apply(filled);
    assert(result.state == pm::v7::OrderState::Filled);
    assert(order.record().filled_microunits == 10'000'000);
    assert(order.record().remaining_microunits == 0);
}

} // namespace

int main() {
    test_submit_partial_cancel_and_fill_during_cancel_pending();
    test_duplicate_fill_is_idempotent();
    test_unknown_requires_reconciliation_and_can_restore_partial_live();
    test_overfill_fails_closed_to_unknown();
    test_reconcile_filled_requires_exact_authoritative_sizes();
    return 0;
}
