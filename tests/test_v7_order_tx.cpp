#include "pm/v7_order_tx.hpp"

#include <cassert>

namespace {

pm::v7::StrategyIntent intent(std::uint64_t id,
                              pm::v7::IntentType type = pm::v7::IntentType::Quote) {
    pm::v7::StrategyIntent out;
    out.intent_id = id;
    out.type = type;
    out.urgency = pm::v7::is_critical_intent(type)
        ? pm::v7::Urgency::Critical : pm::v7::Urgency::Passive;
    return out;
}

pm::v7::ExecutionPlan plan(std::uint64_t id,
                           pm::v7::IntentType type = pm::v7::IntentType::Quote) {
    pm::v7::ExecutionPlan out;
    out.intent = intent(id, type);
    out.tick_size_e4 = 100;
    out.market_state_version = id;
    out.public_queue_observable = 1;
    out.policy = pm::v7::is_critical_intent(type)
        ? pm::v7::ExecutionPolicyId::Emergency
        : pm::v7::ExecutionPolicyId::PassiveMaker;
    return out;
}

void test_critical_from_later_shard_preempts_earlier_normal_quote() {
    pm::v7::OrderTxArbiter<4, 4, 4> arbiter(3);
    assert(arbiter.try_push(0, plan(10)));
    assert(arbiter.try_push(2, plan(20, pm::v7::IntentType::CancelQuote)));

    pm::v7::ExecutionPlan out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard));
    assert(out.intent.intent_id == 20);
    assert(shard == 2);
    assert(arbiter.try_pop(out, shard));
    assert(out.intent.intent_id == 10);
    assert(shard == 0);
}

void test_critical_round_robin_avoids_one_shard_starvation() {
    pm::v7::OrderTxArbiter<4, 8, 8> arbiter(2);
    assert(arbiter.try_push(0, plan(1, pm::v7::IntentType::CancelQuote)));
    assert(arbiter.try_push(0, plan(2, pm::v7::IntentType::CancelQuote)));
    assert(arbiter.try_push(1, plan(3, pm::v7::IntentType::Kill)));

    pm::v7::ExecutionPlan out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent.intent_id == 1);
    assert(arbiter.try_pop(out, shard) && shard == 1 && out.intent.intent_id == 3);
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent.intent_id == 2);
}

void test_normal_round_robin_is_fair() {
    pm::v7::OrderTxArbiter<4, 8, 8> arbiter(2);
    assert(arbiter.try_push(0, plan(11)));
    assert(arbiter.try_push(0, plan(12)));
    assert(arbiter.try_push(1, plan(21)));

    pm::v7::ExecutionPlan out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent.intent_id == 11);
    assert(arbiter.try_pop(out, shard) && shard == 1 && out.intent.intent_id == 21);
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent.intent_id == 12);
}

void test_queue_is_bounded_and_reports_backlog() {
    pm::v7::OrderTxArbiter<2, 2, 2> arbiter(1);
    assert(arbiter.try_push(0, plan(1)));
    assert(arbiter.try_push(0, plan(2)));
    assert(!arbiter.try_push(0, plan(3)));
    assert(arbiter.try_push(0, plan(4, pm::v7::IntentType::Kill)));
    const auto backlog = arbiter.backlog();
    assert(backlog.normal == 2);
    assert(backlog.critical == 1);
}

void test_invalid_shard_fails_closed() {
    pm::v7::OrderTxArbiter<4, 4, 4> arbiter(2);
    assert(!arbiter.try_push(2, plan(1)));
    assert(!arbiter.try_push(3, plan(2)));
}

void test_raw_normal_intent_has_no_fabricated_queue_context() {
    pm::v7::OrderTxArbiter<2, 4, 4> arbiter(1);
    assert(arbiter.try_push(0, intent(100)));
    pm::v7::ExecutionPlan out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard));
    assert(out.intent.intent_id == 100);
    assert(out.policy == pm::v7::ExecutionPolicyId::PassiveMaker);
    assert(out.public_queue_observable == 0);
}

} // namespace

int main() {
    test_critical_from_later_shard_preempts_earlier_normal_quote();
    test_critical_round_robin_avoids_one_shard_starvation();
    test_normal_round_robin_is_fair();
    test_queue_is_bounded_and_reports_backlog();
    test_invalid_shard_fails_closed();
    test_raw_normal_intent_has_no_fabricated_queue_context();
    return 0;
}
