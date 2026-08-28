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

void test_critical_from_later_shard_preempts_earlier_normal_quote() {
    pm::v7::OrderTxArbiter<4, 4, 4> arbiter(3);
    assert(arbiter.try_push(0, intent(10)));
    assert(arbiter.try_push(2, intent(20, pm::v7::IntentType::CancelQuote)));

    pm::v7::StrategyIntent out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard));
    assert(out.intent_id == 20);
    assert(shard == 2);
    assert(arbiter.try_pop(out, shard));
    assert(out.intent_id == 10);
    assert(shard == 0);
}

void test_critical_round_robin_avoids_one_shard_starvation() {
    pm::v7::OrderTxArbiter<4, 8, 8> arbiter(2);
    assert(arbiter.try_push(0, intent(1, pm::v7::IntentType::CancelQuote)));
    assert(arbiter.try_push(0, intent(2, pm::v7::IntentType::CancelQuote)));
    assert(arbiter.try_push(1, intent(3, pm::v7::IntentType::Kill)));

    pm::v7::StrategyIntent out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent_id == 1);
    assert(arbiter.try_pop(out, shard) && shard == 1 && out.intent_id == 3);
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent_id == 2);
}

void test_normal_round_robin_is_fair() {
    pm::v7::OrderTxArbiter<4, 8, 8> arbiter(2);
    assert(arbiter.try_push(0, intent(11)));
    assert(arbiter.try_push(0, intent(12)));
    assert(arbiter.try_push(1, intent(21)));

    pm::v7::StrategyIntent out;
    std::size_t shard = 99;
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent_id == 11);
    assert(arbiter.try_pop(out, shard) && shard == 1 && out.intent_id == 21);
    assert(arbiter.try_pop(out, shard) && shard == 0 && out.intent_id == 12);
}

void test_queue_is_bounded_and_reports_backlog() {
    pm::v7::OrderTxArbiter<2, 2, 2> arbiter(1);
    assert(arbiter.try_push(0, intent(1)));
    assert(arbiter.try_push(0, intent(2)));
    assert(!arbiter.try_push(0, intent(3)));
    assert(arbiter.try_push(0, intent(4, pm::v7::IntentType::Kill)));
    const auto backlog = arbiter.backlog();
    assert(backlog.normal == 2);
    assert(backlog.critical == 1);
}

void test_invalid_shard_fails_closed() {
    pm::v7::OrderTxArbiter<4, 4, 4> arbiter(2);
    assert(!arbiter.try_push(2, intent(1)));
    assert(!arbiter.try_push(3, intent(2)));
}

} // namespace

int main() {
    test_critical_from_later_shard_preempts_earlier_normal_quote();
    test_critical_round_robin_avoids_one_shard_starvation();
    test_normal_round_robin_is_fair();
    test_queue_is_bounded_and_reports_backlog();
    test_invalid_shard_fails_closed();
    return 0;
}
