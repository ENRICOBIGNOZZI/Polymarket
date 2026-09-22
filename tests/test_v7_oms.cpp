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
    intent.causal_trigger_receive_monotonic_ns = 40;
    intent.signal_ready_monotonic_ns = 70;
    intent.decision_monotonic_ns = 90;
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

void test_causal_wire_latency_preserves_grid_delay_and_wire_ack() {
    pm::v7::OmsOrder order(quote_intent(), 9010);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 130, 2)).applied);
    auto ack = event(3, pm::v7::OmsEventType::AckLive, 210, 3);
    ack.exchange_order_handle = 7010;
    assert(order.apply(ack).applied);

    const auto latency = pm::v7::oms_latency_snapshot(order.record());
    const auto has = [&](pm::v7::OmsLatencyLeg leg) {
        return (latency.valid_mask & static_cast<std::uint32_t>(leg)) != 0;
    };
    assert(has(pm::v7::OmsLatencyLeg::TriggerToSignal));
    assert(has(pm::v7::OmsLatencyLeg::SignalToDecision));
    assert(has(pm::v7::OmsLatencyLeg::TriggerToDecision));
    assert(has(pm::v7::OmsLatencyLeg::DecisionToQueue));
    assert(has(pm::v7::OmsLatencyLeg::QueueToWire));
    assert(has(pm::v7::OmsLatencyLeg::WireToAck));
    assert(has(pm::v7::OmsLatencyLeg::TriggerToWire));
    assert(has(pm::v7::OmsLatencyLeg::TriggerToAck));
    assert(latency.trigger_to_signal_ns == 30);
    assert(latency.signal_to_decision_ns == 20);
    assert(latency.trigger_to_decision_ns == 50);
    assert(latency.decision_to_queue_ns == 10);
    assert(latency.queue_to_wire_ns == 30);
    assert(latency.wire_to_ack_ns == 80);
    assert(latency.trigger_to_wire_ns == 90);
    assert(latency.trigger_to_ack_ns == 170);
}

void test_causal_latency_never_fabricates_missing_or_reversed_stages() {
    auto intent = quote_intent();
    intent.signal_ready_monotonic_ns = 0;
    intent.decision_monotonic_ns = 30; // earlier than causal receive: invalid causal leg
    pm::v7::OmsOrder order(intent, 9011);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 120, 2)).applied);

    const auto latency = pm::v7::oms_latency_snapshot(order.record());
    const auto has = [&](pm::v7::OmsLatencyLeg leg) {
        return (latency.valid_mask & static_cast<std::uint32_t>(leg)) != 0;
    };
    assert(!has(pm::v7::OmsLatencyLeg::TriggerToSignal));
    assert(!has(pm::v7::OmsLatencyLeg::SignalToDecision));
    assert(!has(pm::v7::OmsLatencyLeg::TriggerToDecision));
    assert(has(pm::v7::OmsLatencyLeg::DecisionToQueue));
    assert(has(pm::v7::OmsLatencyLeg::QueueToWire));
    assert(!has(pm::v7::OmsLatencyLeg::TriggerToWire));
    assert(!has(pm::v7::OmsLatencyLeg::WireToAck));
    assert(!has(pm::v7::OmsLatencyLeg::TriggerToAck));
}

void test_pending_delay_is_explicit_and_non_cancelable() {
    pm::v7::OmsOrder order(quote_intent(), 9012);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).state
           == pm::v7::OrderState::SendPending);
    auto delayed = order.apply(event(2, pm::v7::OmsEventType::BeginDelay, 110, 2));
    assert(delayed.applied);
    assert(delayed.state == pm::v7::OrderState::PendingDelay);
    assert(order.record().delay_start_ns == 110);
    const auto cancel = order.apply(event(3, pm::v7::OmsEventType::RequestCancel, 120, 3));
    assert(!cancel.applied);
    assert(cancel.invariant_violation);
    assert(order.record().state == pm::v7::OrderState::PendingDelay);
    auto released = order.apply(event(4, pm::v7::OmsEventType::DelayElapsed, 360, 4));
    assert(released.applied);
    assert(released.state == pm::v7::OrderState::SendPending);
    assert(order.record().delay_release_ns == 360);
    assert(order.apply(event(5, pm::v7::OmsEventType::WireSend, 361, 5)).state
           == pm::v7::OrderState::AckPending);
    const auto latency = pm::v7::oms_latency_snapshot(order.record());
    const auto has = [&](pm::v7::OmsLatencyLeg leg) {
        return (latency.valid_mask & static_cast<std::uint32_t>(leg)) != 0;
    };
    assert(has(pm::v7::OmsLatencyLeg::QueueToDelay));
    assert(has(pm::v7::OmsLatencyLeg::DelayDuration));
    assert(has(pm::v7::OmsLatencyLeg::DelayToWire));
    assert(latency.queue_to_delay_ns == 10);
    assert(latency.delay_duration_ns == 250);
    assert(latency.delay_to_wire_ns == 1);
}

void test_user_ws_match_timestamp_is_preserved_separately_from_http_ack() {
    pm::v7::OmsOrder order(quote_intent(), 9013);
    assert(order.apply(event(1, pm::v7::OmsEventType::QueueSend, 100, 1)).applied);
    assert(order.apply(event(2, pm::v7::OmsEventType::WireSend, 130, 2)).applied);
    auto http_ack = event(3, pm::v7::OmsEventType::AckLive, 210, 3);
    assert(order.apply(http_ack).applied);
    assert(order.record().ack_ns == 210);
    assert(order.record().user_ws_match_ns == 0);

    auto user_fill = event(4, pm::v7::OmsEventType::FillDelta, 260, 4);
    user_fill.fill_delta_microunits = 1'000'000;
    user_fill.user_ws_match_monotonic_ns = 260;
    assert(order.apply(user_fill).applied);
    assert(order.record().ack_ns == 210);
    assert(order.record().user_ws_match_ns == 260);
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
    test_causal_wire_latency_preserves_grid_delay_and_wire_ack();
    test_causal_latency_never_fabricates_missing_or_reversed_stages();
    test_pending_delay_is_explicit_and_non_cancelable();
    test_user_ws_match_timestamp_is_preserved_separately_from_http_ack();
    test_reconcile_filled_requires_exact_authoritative_sizes();
    return 0;
}
