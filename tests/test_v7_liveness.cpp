#include "pm/v7_liveness.hpp"

#include <cassert>

namespace {

pm::v7::LivenessInput healthy_input() {
    pm::v7::LivenessInput input;
    input.now_monotonic_ns = 10'000'000'000LL;
    input.last_public_receive_ns = 9'900'000'000LL;
    input.last_private_receive_ns = 9'900'000'000LL;
    input.last_heartbeat_ack_ns = 9'900'000'000LL;
    input.private_stream_state = pm::v7::PrivateStreamState::Healthy;
    input.clock_healthy = 1;
    input.oms_consistent = 1;
    input.transport_healthy = 1;
    return input;
}

void test_paper_default_does_not_require_private_or_heartbeat() {
    pm::v7::LivenessPolicy policy;
    auto input = healthy_input();
    input.last_private_receive_ns = 0;
    input.last_heartbeat_ack_ns = 0;
    input.private_stream_state = pm::v7::PrivateStreamState::Disconnected;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::AllowQuotes);
    assert(decision.maker_admission);
    assert(decision.reason_mask == pm::v7::LivenessNone);
}

void test_stale_public_feed_requests_cancel_all() {
    pm::v7::LivenessPolicy policy;
    auto input = healthy_input();
    input.last_public_receive_ns = 1;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::CancelAll);
    assert(!decision.maker_admission);
    assert((decision.reason_mask & pm::v7::PublicFeedStale) != 0U);
}

void test_private_uncertainty_requires_reconcile_when_enabled() {
    pm::v7::LivenessPolicy policy;
    policy.private_stream_required = 1;
    auto input = healthy_input();
    input.private_stream_state = pm::v7::PrivateStreamState::ReconcileRequired;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::Reconcile);
    assert(!decision.maker_admission);
    assert((decision.reason_mask & pm::v7::PrivateReconciliationRequired) != 0U);
}

void test_stale_private_stream_requires_reconcile_not_fake_cancel_ack() {
    pm::v7::LivenessPolicy policy;
    policy.private_stream_required = 1;
    auto input = healthy_input();
    input.last_private_receive_ns = 1;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::Reconcile);
    assert((decision.reason_mask & pm::v7::PrivateFeedStale) != 0U);
}

void test_heartbeat_deadman_has_cancel_all_precedence() {
    pm::v7::LivenessPolicy policy;
    policy.heartbeat_required = 1;
    auto input = healthy_input();
    input.last_heartbeat_ack_ns = 1;
    input.oms_consistent = 0;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::CancelAll);
    assert((decision.reason_mask & pm::v7::HeartbeatStale) != 0U);
    assert((decision.reason_mask & pm::v7::OmsInconsistent) != 0U);
}

void test_clock_failure_requests_cancel_all() {
    pm::v7::LivenessPolicy policy;
    auto input = healthy_input();
    input.clock_healthy = 0;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::CancelAll);
    assert((decision.reason_mask & pm::v7::ClockUnhealthy) != 0U);
}

void test_transport_failure_blocks_new_quotes_without_hiding_other_health() {
    pm::v7::LivenessPolicy policy;
    auto input = healthy_input();
    input.transport_healthy = 0;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::NoNewQuotes);
    assert(!decision.maker_admission);
    assert((decision.reason_mask & pm::v7::TransportUnhealthy) != 0U);
}

void test_oms_inconsistency_requires_reconciliation() {
    pm::v7::LivenessPolicy policy;
    auto input = healthy_input();
    input.oms_consistent = 0;
    const auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::Reconcile);
    assert((decision.reason_mask & pm::v7::OmsInconsistent) != 0U);
}

} // namespace

int main() {
    test_paper_default_does_not_require_private_or_heartbeat();
    test_stale_public_feed_requests_cancel_all();
    test_private_uncertainty_requires_reconcile_when_enabled();
    test_stale_private_stream_requires_reconcile_not_fake_cancel_ack();
    test_heartbeat_deadman_has_cancel_all_precedence();
    test_clock_failure_requests_cancel_all();
    test_transport_failure_blocks_new_quotes_without_hiding_other_health();
    test_oms_inconsistency_requires_reconciliation();
    return 0;
}
