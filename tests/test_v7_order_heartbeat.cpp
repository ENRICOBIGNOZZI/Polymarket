#include "pm/v7_order_heartbeat.hpp"

#include <cassert>

namespace {

void test_heartbeat_requires_real_send_and_ack_before_admission() {
    pm::v7::OrderHeartbeatGate gate;
    assert(gate.healthy(1)); // Inactive mode has no live heartbeat obligation.
    assert(gate.activate(100));
    assert(!gate.healthy(101));
    assert(gate.send_due(100));
    assert(gate.record_send(100));
    assert(!gate.record_send(101));
    assert(gate.record_ack(101));
    assert(gate.healthy(5'000'000'100LL));
    assert(gate.send_due(5'000'000'100LL));
}

void test_missing_ack_expires_at_the_strict_ten_second_deadline() {
    pm::v7::OrderHeartbeatGate gate;
    assert(gate.activate(100));
    assert(gate.record_send(100));
    assert(!gate.healthy(10'000'000'100LL));
    assert(gate.snapshot().state == pm::v7::OrderHeartbeatState::Expired);
    assert(!gate.record_ack(10'000'000'101LL));
}

void test_pre_send_ack_and_clock_regression_fail_closed() {
    pm::v7::OrderHeartbeatGate first;
    assert(first.activate(100));
    assert(!first.record_ack(101));
    assert(first.snapshot().state == pm::v7::OrderHeartbeatState::Expired);

    pm::v7::OrderHeartbeatGate second;
    assert(second.activate(100));
    assert(second.record_send(100));
    assert(!second.record_ack(99));
    assert(second.snapshot().state == pm::v7::OrderHeartbeatState::Expired);
}

void test_deactivation_requires_a_new_active_epoch() {
    pm::v7::OrderHeartbeatGate gate;
    assert(gate.activate(100));
    assert(gate.record_send(100));
    assert(gate.record_ack(101));
    assert(gate.deactivate());
    assert(gate.snapshot().state == pm::v7::OrderHeartbeatState::Inactive);
    assert(gate.activate(200));
    assert(!gate.healthy(201));
}

} // namespace

int main() {
    test_heartbeat_requires_real_send_and_ack_before_admission();
    test_missing_ack_expires_at_the_strict_ten_second_deadline();
    test_pre_send_ack_and_clock_regression_fail_closed();
    test_deactivation_requires_a_new_active_epoch();
    return 0;
}
