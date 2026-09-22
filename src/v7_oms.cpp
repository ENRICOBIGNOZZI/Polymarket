#include "pm/v7_oms.hpp"

#include <algorithm>

namespace pm::v7 {
namespace {

[[nodiscard]] bool terminal(OrderState state) noexcept {
    return state == OrderState::Filled || state == OrderState::Cancelled
        || state == OrderState::Rejected || state == OrderState::Expired
        || state == OrderState::Lost;
}

} // namespace

const char* to_string(OrderState state) noexcept {
    switch (state) {
        case OrderState::Intent: return "INTENT";
        case OrderState::SendPending: return "SEND_PENDING";
        case OrderState::AckPending: return "ACK_PENDING";
        case OrderState::Live: return "LIVE";
        case OrderState::Partial: return "PARTIAL";
        case OrderState::Filled: return "FILLED";
        case OrderState::CancelRequested: return "CANCEL_REQUESTED";
        case OrderState::CancelPending: return "CANCEL_PENDING";
        case OrderState::Cancelled: return "CANCELLED";
        case OrderState::Rejected: return "REJECTED";
        case OrderState::Expired: return "EXPIRED";
        case OrderState::Unknown: return "UNKNOWN";
        case OrderState::Reconciling: return "RECONCILING";
        case OrderState::Lost: return "LOST";
        case OrderState::PendingDelay: return "PENDING_DELAY";
    }
    return "UNKNOWN";
}

OmsOrder::OmsOrder(const StrategyIntent& intent, std::uint64_t client_order_id) noexcept {
    record_.intent_id = intent.intent_id;
    record_.client_order_id = client_order_id;
    record_.market_handle = intent.market_handle;
    record_.event_handle = intent.event_handle;
    record_.instrument_handle = intent.instrument_handle;
    record_.strategy_id = intent.strategy_id;
    record_.side = intent.side;
    record_.price_tick = intent.price_tick;
    record_.causal_trigger_receive_monotonic_ns = intent.causal_trigger_receive_monotonic_ns;
    record_.signal_ready_monotonic_ns = intent.signal_ready_monotonic_ns;
    record_.decision_monotonic_ns = intent.decision_monotonic_ns;
    record_.original_microunits = std::max<std::int64_t>(0, intent.quantity_microunits);
    record_.remaining_microunits = record_.original_microunits;
    record_.state = OrderState::Intent;
}

OmsLatencySnapshot oms_latency_snapshot(const OmsOrderRecord& record) noexcept {
    OmsLatencySnapshot out;
    const auto leg = [&](std::int64_t start, std::int64_t end,
                         OmsLatencyLeg flag, std::int64_t& destination) noexcept {
        if (start > 0 && end >= start) {
            destination = end - start;
            out.valid_mask |= static_cast<std::uint32_t>(flag);
        }
    };
    leg(record.causal_trigger_receive_monotonic_ns, record.signal_ready_monotonic_ns,
        OmsLatencyLeg::TriggerToSignal, out.trigger_to_signal_ns);
    leg(record.signal_ready_monotonic_ns, record.decision_monotonic_ns,
        OmsLatencyLeg::SignalToDecision, out.signal_to_decision_ns);
    leg(record.causal_trigger_receive_monotonic_ns, record.decision_monotonic_ns,
        OmsLatencyLeg::TriggerToDecision, out.trigger_to_decision_ns);
    leg(record.decision_monotonic_ns, record.submission_ns,
        OmsLatencyLeg::DecisionToQueue, out.decision_to_queue_ns);
    leg(record.submission_ns, record.wire_ns,
        OmsLatencyLeg::QueueToWire, out.queue_to_wire_ns);
    leg(record.wire_ns, record.ack_ns,
        OmsLatencyLeg::WireToAck, out.wire_to_ack_ns);

    leg(record.submission_ns, record.delay_start_ns,
        OmsLatencyLeg::QueueToDelay, out.queue_to_delay_ns);
    leg(record.delay_start_ns, record.delay_release_ns,
        OmsLatencyLeg::DelayDuration, out.delay_duration_ns);
    leg(record.delay_release_ns, record.wire_ns,
        OmsLatencyLeg::DelayToWire, out.delay_to_wire_ns);

    // End-to-end causal metrics are stricter than pairwise legs. Do not report
    // trigger->wire/ACK if an intermediate timestamp is missing or reversed;
    // that would make a malformed trace look fast. signal_ready is optional,
    // but when present it must lie between trigger receive and decision.
    const bool signal_chain_valid = record.signal_ready_monotonic_ns == 0
        || (record.signal_ready_monotonic_ns >= record.causal_trigger_receive_monotonic_ns
            && record.signal_ready_monotonic_ns <= record.decision_monotonic_ns);
    const bool causal_to_wire_valid = record.causal_trigger_receive_monotonic_ns > 0
        && record.decision_monotonic_ns >= record.causal_trigger_receive_monotonic_ns
        && signal_chain_valid
        && record.submission_ns >= record.decision_monotonic_ns
        && record.wire_ns >= record.submission_ns;
    if (causal_to_wire_valid) {
        out.trigger_to_wire_ns = record.wire_ns - record.causal_trigger_receive_monotonic_ns;
        out.valid_mask |= static_cast<std::uint32_t>(OmsLatencyLeg::TriggerToWire);
        if (record.ack_ns >= record.wire_ns) {
            out.trigger_to_ack_ns = record.ack_ns - record.causal_trigger_receive_monotonic_ns;
            out.valid_mask |= static_cast<std::uint32_t>(OmsLatencyLeg::TriggerToAck);
        }
    }
    return out;
}

OmsTransitionResult OmsOrder::result(bool applied, bool duplicate,
                                     bool reconcile, bool violation) const noexcept {
    return OmsTransitionResult{
        record_.state,
        static_cast<std::uint8_t>(applied),
        static_cast<std::uint8_t>(duplicate),
        static_cast<std::uint8_t>(reconcile),
        static_cast<std::uint8_t>(violation),
    };
}

void OmsOrder::mark_event(const OmsEvent& event) noexcept {
    if (event.event_id != 0) record_.last_event_id = event.event_id;
    if (event.source_version != 0) record_.last_source_version = event.source_version;
    ++record_.state_version;
}

void OmsOrder::force_unknown(const OmsEvent& event) noexcept {
    record_.state = OrderState::Unknown;
    mark_event(event);
}

bool OmsOrder::authoritative_sizes_valid(const OmsEvent& event) const noexcept {
    if (event.authoritative_filled_microunits < 0 || event.authoritative_remaining_microunits < 0) {
        return false;
    }
    return event.authoritative_filled_microunits + event.authoritative_remaining_microunits
        == record_.original_microunits;
}

OmsTransitionResult OmsOrder::apply(const OmsEvent& event) noexcept {
    if (event.event_id != 0 && event.event_id == record_.last_event_id) {
        return result(false, true, record_.state == OrderState::Unknown || record_.state == OrderState::Reconciling, false);
    }
    if (event.source_version != 0 && record_.last_source_version != 0
        && event.source_version <= record_.last_source_version) {
        return result(false, true, record_.state == OrderState::Unknown || record_.state == OrderState::Reconciling, false);
    }
    if (event.timestamp_ns <= 0) {
        force_unknown(event);
        return result(true, false, true, true);
    }

    switch (event.type) {
        case OmsEventType::QueueSend:
            if (record_.state == OrderState::Intent && record_.original_microunits > 0
                && record_.client_order_id != 0) {
                record_.state = OrderState::SendPending;
                record_.submission_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::SendPending || record_.state == OrderState::AckPending
                || record_.state == OrderState::Live || record_.state == OrderState::Partial) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::BeginDelay:
            if (record_.state == OrderState::SendPending && event.timestamp_ns > 0) {
                record_.state = OrderState::PendingDelay;
                record_.delay_start_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::PendingDelay) {
                return result(false, true, false, false);
            }
            return result(false, false, false, true);

        case OmsEventType::DelayElapsed:
            if (record_.state == OrderState::PendingDelay
                && event.timestamp_ns >= record_.delay_start_ns && event.timestamp_ns > 0) {
                record_.state = OrderState::SendPending;
                record_.delay_release_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::SendPending
                && record_.delay_release_ns > 0) {
                return result(false, true, false, false);
            }
            return result(false, false, false, true);

        case OmsEventType::WireSend:
            if (record_.state == OrderState::SendPending) {
                record_.state = OrderState::AckPending;
                record_.wire_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::AckPending || record_.state == OrderState::Live
                || record_.state == OrderState::Partial) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::AckLive:
            if (record_.state == OrderState::AckPending || record_.state == OrderState::SendPending) {
                if (event.exchange_order_handle > 0) {
                    record_.exchange_order_handle = static_cast<std::uint64_t>(event.exchange_order_handle);
                }
                record_.ack_ns = event.timestamp_ns;
                record_.live_ns = event.timestamp_ns;
                record_.state = record_.filled_microunits > 0 ? OrderState::Partial : OrderState::Live;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::Live || record_.state == OrderState::Partial) {
                if (event.exchange_order_handle > 0 && record_.exchange_order_handle == 0) {
                    record_.exchange_order_handle = static_cast<std::uint64_t>(event.exchange_order_handle);
                }
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::FillDelta:
            if (record_.state == OrderState::AckPending || record_.state == OrderState::Live
                || record_.state == OrderState::Partial || record_.state == OrderState::CancelRequested
                || record_.state == OrderState::CancelPending) {
                if (event.fill_delta_microunits <= 0
                    || event.fill_delta_microunits > record_.remaining_microunits) {
                    force_unknown(event);
                    return result(true, false, true, true);
                }
                const bool cancel_in_flight = record_.state == OrderState::CancelRequested
                                           || record_.state == OrderState::CancelPending;
                record_.filled_microunits += event.fill_delta_microunits;
                record_.remaining_microunits -= event.fill_delta_microunits;
                if (record_.remaining_microunits == 0) {
                    record_.state = OrderState::Filled;
                } else if (cancel_in_flight) {
                    record_.state = record_.state == OrderState::CancelPending
                        ? OrderState::CancelPending : OrderState::CancelRequested;
                } else {
                    record_.state = OrderState::Partial;
                }
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::Filled) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::RequestCancel:
            if (record_.state == OrderState::AckPending || record_.state == OrderState::Live
                || record_.state == OrderState::Partial) {
                record_.state = OrderState::CancelRequested;
                record_.cancel_request_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::CancelRequested || record_.state == OrderState::CancelPending
                || record_.state == OrderState::Cancelled || record_.state == OrderState::Filled) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::WireCancel:
            if (record_.state == OrderState::CancelRequested) {
                record_.state = OrderState::CancelPending;
                record_.cancel_wire_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::CancelPending || record_.state == OrderState::Cancelled
                || record_.state == OrderState::Filled) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::AckCancel:
            if (record_.state == OrderState::CancelRequested || record_.state == OrderState::CancelPending) {
                record_.cancel_ack_ns = event.timestamp_ns;
                record_.cancel_effective_ns = event.timestamp_ns;
                record_.state = record_.remaining_microunits == 0 ? OrderState::Filled : OrderState::Cancelled;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::Cancelled || record_.state == OrderState::Filled) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::Reject:
            if (record_.state == OrderState::SendPending || record_.state == OrderState::AckPending) {
                record_.state = OrderState::Rejected;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::Rejected) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::Expire:
            if (record_.state == OrderState::Live || record_.state == OrderState::Partial
                || record_.state == OrderState::CancelRequested || record_.state == OrderState::CancelPending) {
                record_.state = record_.remaining_microunits == 0 ? OrderState::Filled : OrderState::Expired;
                mark_event(event);
                return result(true, false, false, false);
            }
            if (record_.state == OrderState::Expired || record_.state == OrderState::Filled) {
                mark_event(event);
                return result(false, true, false, false);
            }
            break;

        case OmsEventType::TransportUnknown:
            if (!terminal(record_.state)) {
                record_.state = OrderState::Unknown;
                mark_event(event);
                return result(true, false, true, false);
            }
            mark_event(event);
            return result(false, true, false, false);

        case OmsEventType::BeginReconcile:
            if (record_.state == OrderState::Unknown) {
                record_.state = OrderState::Reconciling;
                mark_event(event);
                return result(true, false, true, false);
            }
            if (record_.state == OrderState::Reconciling) {
                mark_event(event);
                return result(false, true, true, false);
            }
            break;

        case OmsEventType::ReconcileLive:
            if (record_.state == OrderState::Unknown || record_.state == OrderState::Reconciling) {
                if (!authoritative_sizes_valid(event) || event.authoritative_remaining_microunits <= 0) {
                    force_unknown(event);
                    return result(true, false, true, true);
                }
                record_.filled_microunits = event.authoritative_filled_microunits;
                record_.remaining_microunits = event.authoritative_remaining_microunits;
                if (event.exchange_order_handle > 0) {
                    record_.exchange_order_handle = static_cast<std::uint64_t>(event.exchange_order_handle);
                }
                record_.state = record_.filled_microunits > 0 ? OrderState::Partial : OrderState::Live;
                mark_event(event);
                return result(true, false, false, false);
            }
            break;

        case OmsEventType::ReconcileCancelled:
            if (record_.state == OrderState::Unknown || record_.state == OrderState::Reconciling) {
                if (!authoritative_sizes_valid(event)) {
                    force_unknown(event);
                    return result(true, false, true, true);
                }
                record_.filled_microunits = event.authoritative_filled_microunits;
                record_.remaining_microunits = event.authoritative_remaining_microunits;
                record_.state = record_.remaining_microunits == 0 ? OrderState::Filled : OrderState::Cancelled;
                record_.cancel_effective_ns = event.timestamp_ns;
                mark_event(event);
                return result(true, false, false, false);
            }
            break;

        case OmsEventType::ReconcileFilled:
            if (record_.state == OrderState::Unknown || record_.state == OrderState::Reconciling) {
                if (!authoritative_sizes_valid(event)
                    || event.authoritative_remaining_microunits != 0
                    || event.authoritative_filled_microunits != record_.original_microunits) {
                    force_unknown(event);
                    return result(true, false, true, true);
                }
                record_.filled_microunits = record_.original_microunits;
                record_.remaining_microunits = 0;
                record_.state = OrderState::Filled;
                mark_event(event);
                return result(true, false, false, false);
            }
            break;

        case OmsEventType::ReconcileLost:
            if (record_.state == OrderState::Unknown || record_.state == OrderState::Reconciling) {
                record_.state = OrderState::Lost;
                mark_event(event);
                return result(true, false, false, false);
            }
            break;
    }

    force_unknown(event);
    return result(true, false, true, true);
}

} // namespace pm::v7
