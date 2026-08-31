#include "pm/v7_clob_v2_recovery.hpp"

#include <cassert>

namespace {

void test_425_backoff_then_two_minute_post_only_recovery() {
    pm::v7::ClobV2RecoveryGate gate;
    assert(gate.complete_reconciliation(1));
    assert(gate.admit(pm::v7::ClobV2Action::NewOrder, false, 2));
    assert(gate.observe_http_425(1'000));
    assert(gate.snapshot().mode == pm::v7::ClobV2VenueMode::RestartBackoff);
    assert(!gate.admit(pm::v7::ClobV2Action::NewOrder, true, 1'001));
    assert(gate.admit(pm::v7::ClobV2Action::Cancel, false, 1'001));
    assert(gate.observe_engine_recovered(2'000));
    assert(gate.snapshot().mode == pm::v7::ClobV2VenueMode::PostOnlyRecovery);
    assert(!gate.admit(pm::v7::ClobV2Action::NewOrder, false, 2'001));
    assert(gate.admit(pm::v7::ClobV2Action::NewOrder, true, 2'001));
    assert(gate.admit(pm::v7::ClobV2Action::NewOrder, false, 120'000'002'000LL));
}

void test_cancel_only_and_reconciliation_are_fail_closed_for_new_orders() {
    pm::v7::ClobV2RecoveryGate gate;
    assert(gate.complete_reconciliation(1));
    assert(gate.observe_cancel_only(10));
    assert(!gate.admit(pm::v7::ClobV2Action::NewOrder, true, 11));
    assert(gate.admit(pm::v7::ClobV2Action::Cancel, false, 11));
    assert(gate.require_reconciliation());
    assert(!gate.admit(pm::v7::ClobV2Action::NewOrder, true, 12));
    assert(gate.admit(pm::v7::ClobV2Action::Cancel, false, 12));
    assert(gate.complete_reconciliation(13));
    assert(gate.admit(pm::v7::ClobV2Action::NewOrder, false, 14));
}

void test_repeated_425s_backoff_and_invalid_clock_fail_closed() {
    pm::v7::ClobV2RecoveryGate gate;
    assert(gate.complete_reconciliation(1));
    assert(gate.observe_http_425(100));
    const auto first = gate.snapshot().retry_after_ns;
    assert(gate.observe_http_425(200));
    assert(gate.snapshot().retry_after_ns - 200 >= first - 100);
    assert(!gate.observe_http_425(0));
    assert(gate.snapshot().mode == pm::v7::ClobV2VenueMode::ReconciliationRequired);
}

} // namespace

int main() {
    test_425_backoff_then_two_minute_post_only_recovery();
    test_cancel_only_and_reconciliation_are_fail_closed_for_new_orders();
    test_repeated_425s_backoff_and_invalid_clock_fail_closed();
    return 0;
}
