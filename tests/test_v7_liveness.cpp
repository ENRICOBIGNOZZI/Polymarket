#include "pm/v7_kill_switch.hpp"
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

void test_clob_v2_restrictions_preserve_cancel_capacity_and_reconciliation() {
    pm::v7::LivenessPolicy policy;
    policy.clob_v2_recovery_required = 1;
    auto input = healthy_input();

    input.clob_v2_venue_mode = pm::v7::ClobV2VenueMode::RestartBackoff;
    auto decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::NoNewQuotes);
    assert((decision.reason_mask & pm::v7::VenueRestartBackoff) != 0U);

    input.clob_v2_venue_mode = pm::v7::ClobV2VenueMode::PostOnlyRecovery;
    decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::AllowQuotes);
    assert(decision.maker_admission == 1);
    assert(decision.maker_post_only_required == 1);

    input.clob_v2_venue_mode = pm::v7::ClobV2VenueMode::CancelOnly;
    decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::NoNewQuotes);

    input.clob_v2_venue_mode = pm::v7::ClobV2VenueMode::ReconciliationRequired;
    decision = pm::v7::evaluate_liveness(input, policy);
    assert(decision.action == pm::v7::LivenessAction::Reconcile);
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

void test_scoped_kill_switch_is_idempotent_and_precise() {
    pm::v7::KillSwitchPlane kills;
    const auto initial_version = kills.state_version();
    auto decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 17, 29, 41);
    assert(!decision.killed);
    assert(decision.reason_mask == pm::v7::KillNone);

    const auto first = kills.set_market(17, true);
    assert(first.accepted);
    assert(first.changed);
    assert(first.state_version > initial_version);

    const auto repeated = kills.set_market(17, true);
    assert(repeated.accepted);
    assert(!repeated.changed);
    assert(repeated.state_version == first.state_version);

    decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 17, 29, 41);
    assert(decision.killed);
    assert((decision.reason_mask & pm::v7::MarketKilled) != 0U);
    assert((decision.reason_mask & pm::v7::InstrumentKilled) == 0U);

    const auto cleared = kills.set_market(17, false);
    assert(cleared.accepted);
    assert(cleared.changed);
    decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 17, 29, 41);
    assert(!decision.killed);
}

void test_scoped_kill_switch_composes_all_levels_and_global_dominates() {
    pm::v7::KillSwitchPlane kills;
    assert(kills.set_instrument(41, true).changed);
    assert(kills.set_event(29, true).changed);
    assert(kills.set_strategy(pm::v7::StrategyId::ProfessionalMaker, true).changed);

    auto decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 17, 29, 41);
    assert(decision.killed);
    assert((decision.reason_mask & pm::v7::InstrumentKilled) != 0U);
    assert((decision.reason_mask & pm::v7::EventKilled) != 0U);
    assert((decision.reason_mask & pm::v7::StrategyKilled) != 0U);
    assert((decision.reason_mask & pm::v7::MarketKilled) == 0U);
    assert((decision.reason_mask & pm::v7::GlobalKilled) == 0U);

    assert(kills.set_global(true).changed);
    decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 17, 29, 41);
    assert(decision.killed);
    assert((decision.reason_mask & pm::v7::GlobalKilled) != 0U);

    assert(kills.set_instrument(41, false).changed);
    assert(kills.set_event(29, false).changed);
    assert(kills.set_strategy(pm::v7::StrategyId::ProfessionalMaker, false).changed);
    decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 17, 29, 41);
    assert(decision.killed);
    assert(decision.reason_mask == pm::v7::GlobalKilled);
}

void test_scoped_kill_switch_fails_closed_on_invalid_handles() {
    pm::v7::KillSwitchPlane kills;
    const auto invalid_mutation = kills.set_instrument(0, true);
    assert(!invalid_mutation.accepted);
    assert(!invalid_mutation.changed);

    const auto decision = kills.evaluate(pm::v7::StrategyId::ProfessionalMaker, 0, 29, 41);
    assert(decision.killed);
    assert((decision.reason_mask & pm::v7::KillInvalidHandle) != 0U);
}

} // namespace

int main() {
    test_paper_default_does_not_require_private_or_heartbeat();
    test_stale_public_feed_requests_cancel_all();
    test_private_uncertainty_requires_reconcile_when_enabled();
    test_stale_private_stream_requires_reconcile_not_fake_cancel_ack();
    test_clob_v2_restrictions_preserve_cancel_capacity_and_reconciliation();
    test_heartbeat_deadman_has_cancel_all_precedence();
    test_clock_failure_requests_cancel_all();
    test_transport_failure_blocks_new_quotes_without_hiding_other_health();
    test_oms_inconsistency_requires_reconciliation();
    test_scoped_kill_switch_is_idempotent_and_precise();
    test_scoped_kill_switch_composes_all_levels_and_global_dominates();
    test_scoped_kill_switch_fails_closed_on_invalid_handles();
    return 0;
}
