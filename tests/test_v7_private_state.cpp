#include "pm/v7_private_state.hpp"

#include <cassert>

namespace {

pm::v7::StrategyIntent intent(std::uint64_t intent_id) {
    pm::v7::StrategyIntent out;
    out.intent_id = intent_id;
    out.market_handle = 11;
    out.event_handle = 22;
    out.instrument_handle = 33;
    out.strategy_id = pm::v7::StrategyId::ProfessionalMaker;
    out.type = pm::v7::IntentType::Quote;
    out.side = pm::v7::Side::Buy;
    out.price_tick = 52;
    out.quantity_microunits = 1'000'000;
    return out;
}

pm::v7::OmsEvent event(pm::v7::OmsEventType type,
                       std::uint64_t event_id,
                       std::uint64_t source_version,
                       std::int64_t timestamp_ns) {
    pm::v7::OmsEvent out;
    out.type = type;
    out.event_id = event_id;
    out.source_version = source_version;
    out.timestamp_ns = timestamp_ns;
    return out;
}

void make_live(pm::v7::PrivateOrderState& state,
               std::uint64_t client_order_id,
               std::uint64_t intent_id,
               std::uint64_t source_base = 1) {
    assert(state.register_order(intent(intent_id), client_order_id));
    auto queued = event(pm::v7::OmsEventType::QueueSend, 100 + intent_id, source_base, 100);
    assert(state.apply_order_event(client_order_id, queued).applied);
    auto wired = event(pm::v7::OmsEventType::WireSend, 200 + intent_id, source_base + 1, 200);
    assert(state.apply_order_event(client_order_id, wired).applied);
    auto ack = event(pm::v7::OmsEventType::AckLive, 300 + intent_id, source_base + 2, 300);
    ack.exchange_order_handle = static_cast<std::int64_t>(5000 + client_order_id);
    assert(state.apply_order_event(client_order_id, ack).applied);
    assert(state.order(client_order_id)->state == pm::v7::OrderState::Live);
}

void test_disconnect_freezes_submission_until_authoritative_reconcile() {
    pm::v7::PrivateOrderState state;
    assert(!state.snapshot().submissions_safe);
    assert(state.on_stream_connected(10));
    assert(state.snapshot().submissions_safe);
    make_live(state, 1001, 1);

    assert(state.on_stream_disconnect(400));
    auto snap = state.snapshot();
    assert(snap.stream_state == pm::v7::PrivateStreamState::ReconcileRequired);
    assert(!snap.submissions_safe);
    assert(snap.unresolved_orders == 1);
    assert(state.order(1001)->state == pm::v7::OrderState::Unknown);

    // Reconnecting transport is not enough: authoritative reconciliation is
    // still mandatory before new submissions become safe.
    assert(state.on_stream_connected(450));
    assert(state.snapshot().stream_state == pm::v7::PrivateStreamState::ReconcileRequired);
    assert(!state.register_order(intent(2), 1002));

    assert(state.begin_reconciliation(500));
    assert(state.order(1001)->state == pm::v7::OrderState::Reconciling);
    assert(!state.complete_reconciliation());

    pm::v7::AuthoritativeOrderSnapshot authoritative;
    authoritative.client_order_id = 1001;
    authoritative.exchange_order_handle = 6001;
    authoritative.filled_microunits = 0;
    authoritative.remaining_microunits = 1'000'000;
    authoritative.timestamp_ns = 600;
    authoritative.source_version = 4;
    authoritative.state = pm::v7::AuthoritativeOrderState::Live;
    assert(state.reconcile(authoritative));
    assert(state.order(1001)->state == pm::v7::OrderState::Live);
    assert(state.complete_reconciliation());
    assert(state.snapshot().submissions_safe);
}

void test_partial_fill_is_reconciled_to_authoritative_cancelled_sizes() {
    pm::v7::PrivateOrderState state;
    assert(state.on_stream_connected(10));
    make_live(state, 2001, 11, 10);

    auto fill = event(pm::v7::OmsEventType::FillDelta, 900, 13, 350);
    fill.fill_delta_microunits = 300'000;
    assert(state.apply_order_event(2001, fill).applied);
    assert(state.order(2001)->state == pm::v7::OrderState::Partial);

    assert(state.on_stream_disconnect(400));
    assert(state.begin_reconciliation(500));
    pm::v7::AuthoritativeOrderSnapshot authoritative;
    authoritative.client_order_id = 2001;
    authoritative.exchange_order_handle = 7001;
    authoritative.filled_microunits = 400'000;
    authoritative.remaining_microunits = 600'000;
    authoritative.timestamp_ns = 600;
    authoritative.source_version = 14;
    authoritative.state = pm::v7::AuthoritativeOrderState::Cancelled;
    assert(state.reconcile(authoritative));
    const auto* order = state.order(2001);
    assert(order->state == pm::v7::OrderState::Cancelled);
    assert(order->filled_microunits == 400'000);
    assert(order->remaining_microunits == 600'000);
    assert(state.complete_reconciliation());
}

void test_all_unresolved_orders_must_reconcile_before_resume() {
    pm::v7::PrivateOrderState state;
    assert(state.on_stream_connected(10));
    make_live(state, 3001, 21, 20);
    make_live(state, 3002, 22, 30);
    assert(state.on_stream_disconnect(400));
    assert(state.begin_reconciliation(500));

    pm::v7::AuthoritativeOrderSnapshot first;
    first.client_order_id = 3001;
    first.filled_microunits = 1'000'000;
    first.remaining_microunits = 0;
    first.timestamp_ns = 600;
    first.source_version = 40;
    first.state = pm::v7::AuthoritativeOrderState::Filled;
    assert(state.reconcile(first));
    assert(!state.complete_reconciliation());
    assert(!state.snapshot().submissions_safe);

    pm::v7::AuthoritativeOrderSnapshot second;
    second.client_order_id = 3002;
    second.timestamp_ns = 610;
    second.source_version = 41;
    second.state = pm::v7::AuthoritativeOrderState::Lost;
    assert(state.reconcile(second));
    assert(state.complete_reconciliation());
    assert(state.snapshot().submissions_safe);
}

void test_terminal_retirement_only() {
    pm::v7::PrivateOrderState state;
    assert(state.on_stream_connected(10));
    make_live(state, 4001, 31, 50);
    assert(!state.retire_terminal_order(4001));

    auto cancel_request = event(pm::v7::OmsEventType::RequestCancel, 1000, 53, 400);
    assert(state.apply_order_event(4001, cancel_request).applied);
    auto cancel_wire = event(pm::v7::OmsEventType::WireCancel, 1001, 54, 450);
    assert(state.apply_order_event(4001, cancel_wire).applied);
    auto cancel_ack = event(pm::v7::OmsEventType::AckCancel, 1002, 55, 500);
    assert(state.apply_order_event(4001, cancel_ack).applied);
    assert(state.order(4001)->state == pm::v7::OrderState::Cancelled);
    assert(state.retire_terminal_order(4001));
    assert(state.order(4001) == nullptr);
    assert(state.snapshot().tracked_orders == 0);
}

void test_local_disconnect_event_does_not_collide_with_last_venue_event_id() {
    pm::v7::PrivateOrderState state;
    assert(state.on_stream_connected(10));
    assert(state.register_order(intent(41), 5001));
    // Deliberately use event_id=1; local disconnect transitions use id=0 and
    // therefore cannot be suppressed as a duplicate.
    auto queued = event(pm::v7::OmsEventType::QueueSend, 1, 1, 100);
    assert(state.apply_order_event(5001, queued).applied);
    assert(state.on_stream_disconnect(200));
    assert(state.order(5001)->state == pm::v7::OrderState::Unknown);
}

} // namespace

int main() {
    test_disconnect_freezes_submission_until_authoritative_reconcile();
    test_partial_fill_is_reconciled_to_authoritative_cancelled_sizes();
    test_all_unresolved_orders_must_reconcile_before_resume();
    test_terminal_retirement_only();
    test_local_disconnect_event_does_not_collide_with_last_venue_event_id();
    return 0;
}
