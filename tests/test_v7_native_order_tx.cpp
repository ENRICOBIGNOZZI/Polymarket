#include "pm/v7_native_order_tx.hpp"

#include <atomic>
#include <cassert>
#include <cstdlib>
#include <cstdint>
#include <new>
#include <vector>

namespace { std::atomic<std::uint64_t> allocations{0}; }
void* operator new(std::size_t size) {
    allocations.fetch_add(1, std::memory_order_relaxed);
    if (void* value=std::malloc(size)) return value;
    throw std::bad_alloc();
}
void operator delete(void* value) noexcept { std::free(value); }
void operator delete(void* value, std::size_t) noexcept { std::free(value); }

using namespace pm::v7;

ExecutionPlan plan(std::uint64_t intent_id = 1) {
    ExecutionPlan value;
    value.intent.intent_id = intent_id;
    value.intent.market_handle = 7;
    value.intent.event_handle = 8;
    value.intent.instrument_handle = 11;
    value.intent.state_version = 9;
    value.intent.decision_monotonic_ns = 1'000;
    value.intent.price_tick = 40;
    value.intent.quantity_microunits = 5'000'000;
    value.intent.strategy_id = StrategyId::CryptoInformedTaker;
    value.intent.type = IntentType::TargetPosition;
    value.intent.side = Side::Buy;
    value.intent.urgency = Urgency::Aggressive;
    value.intent.purpose = IntentPurpose::Alpha;
    value.intent.passive = 0;
    value.intent.post_only = 0;
    value.tick_size_e4 = 100;
    value.market_state_version = 9;
    value.policy = ExecutionPolicyId::AggressiveTaker;
    return value;
}

OmsEvent event(std::uint64_t id, OmsEventType type, std::int64_t ns) {
    OmsEvent value;
    value.event_id = id;
    value.type = type;
    value.timestamp_ns = ns;
    return value;
}

void test_prepare_wire_reject_retire() {
    NativeOrderTxOwner owner;
    const auto prepared = owner.prepare_submit(plan(), 1'100);
    assert(prepared.accepted);
    assert(prepared.reason == NativeOrderTxReason::Accepted);
    assert(prepared.oms.state == OrderState::SendPending);
    assert(prepared.command.time_in_force == AdapterTimeInForce::Fak);
    assert(prepared.command.instrument_handle == 11);
    assert(prepared.command.client_order_id != 0);
    assert(owner.active_orders() == 1);

    const auto wire = owner.apply(prepared.command.client_order_id,
                                  event(100, OmsEventType::WireSend, 1'200));
    assert(wire.applied && wire.state == OrderState::AckPending);
    const auto rejected = owner.apply(prepared.command.client_order_id,
                                      event(101, OmsEventType::Reject, 1'300));
    assert(rejected.applied && rejected.state == OrderState::Rejected);
    assert(owner.retire_terminal(prepared.command.client_order_id));
    assert(owner.active_orders() == 0);
    assert(owner.find(prepared.command.client_order_id) == nullptr);
}

void test_capacity_is_bounded_and_reusable() {
    NativeOrderTxOwner owner;
    std::vector<std::uint64_t> ids;
    ids.reserve(kNativeOrderTxCapacity);
    for (std::size_t i = 0; i < kNativeOrderTxCapacity; ++i) {
        auto p = plan(static_cast<std::uint64_t>(i + 1));
        const auto result = owner.prepare_submit(p, 1'100 + static_cast<std::int64_t>(i));
        assert(result.accepted);
        ids.push_back(result.command.client_order_id);
    }
    assert(owner.active_orders() == kNativeOrderTxCapacity);
    assert(owner.prepare_submit(plan(kNativeOrderTxCapacity + 1), 3'000).reason
           == NativeOrderTxReason::CapacityFull);

    const auto first = ids.front();
    assert(owner.apply(first, event(5000, OmsEventType::Reject, 4'000)).state
           == OrderState::Rejected);
    assert(owner.retire_terminal(first));
    const auto replacement = owner.prepare_submit(plan(kNativeOrderTxCapacity + 2), 4'100);
    assert(replacement.accepted);
    assert(replacement.command.client_order_id != first);
}

void test_prepare_submit_is_allocation_free() {
    NativeOrderTxOwner owner;
    const auto before=allocations.load(std::memory_order_relaxed);
    const auto result=owner.prepare_submit(plan(),1'100);
    const auto after=allocations.load(std::memory_order_relaxed);
    assert(result.accepted);
    assert(after==before);
}

void test_invalid_and_unknown_fail_closed() {
    NativeOrderTxOwner owner;
    auto bad = plan();
    bad.intent.price_tick = 0;
    assert(owner.prepare_submit(bad, 1'100).reason == NativeOrderTxReason::InvalidPlan);
    const auto missing = owner.apply(999999, event(1, OmsEventType::WireSend, 2'000));
    assert(missing.invariant_violation);
    assert(missing.reconciliation_required);
    assert(missing.state == OrderState::Unknown);
    assert(!owner.retire_terminal(999999));
}

int main() {
    test_prepare_wire_reject_retire();
    test_capacity_is_bounded_and_reusable();
    test_prepare_submit_is_allocation_free();
    test_invalid_and_unknown_fail_closed();
    return 0;
}
