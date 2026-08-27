#include "pm/v7_paper_queue.hpp"

#include <array>
#include <cassert>

namespace {

pm::v7::PaperRestingOrder order(std::uint64_t id, std::int64_t arrival_exchange,
                                std::int64_t arrival_receive) {
    pm::v7::PaperRestingOrder out;
    out.order_id = id;
    out.instrument_handle = 77;
    out.side = pm::v7::Side::Buy;
    out.price_tick = 48;
    out.original_microunits = 10'000'000;
    out.arrival_exchange_event_ns = arrival_exchange;
    out.arrival_receive_monotonic_ns = arrival_receive;
    out.queue = pm::v7::QueueEnvelope{5'000'000, 10'000'000, 15'000'000, 0.60};
    out.remaining_pessimistic_microunits = out.original_microunits;
    out.remaining_expected_microunits = out.original_microunits;
    out.remaining_optimistic_microunits = out.original_microunits;
    return out;
}

pm::v7::PublicTradePrint sell_print(std::uint64_t id, std::int64_t qty,
                                    std::int64_t exchange, std::int64_t receive) {
    pm::v7::PublicTradePrint trade;
    trade.trade_id = id;
    trade.instrument_handle = 77;
    trade.aggressor_side = pm::v7::Side::Sell;
    trade.price_tick = 48;
    trade.quantity_microunits = qty;
    trade.exchange_event_ns = exchange;
    trade.receive_monotonic_ns = receive;
    return trade;
}

void test_queue_envelope_produces_ordered_fill_scenarios() {
    std::array<pm::v7::PaperRestingOrder, 1> orders{order(1, 100, 1000)};
    std::array<pm::v7::PaperFillEnvelope, 1> fills{};
    const auto result = pm::v7::allocate_public_print(
        sell_print(9, 12'000'000, 101, 1001), orders, fills);
    assert(!result.invalid_input);
    assert(result.output_count == 1);
    assert(fills[0].pessimistic_fill_microunits == 0);
    assert(fills[0].expected_fill_microunits == 2'000'000);
    assert(fills[0].optimistic_fill_microunits == 7'000'000);
    assert(result.pessimistic_consumed_microunits == 12'000'000);
    assert(result.expected_consumed_microunits == 12'000'000);
    assert(result.optimistic_consumed_microunits == 12'000'000);
    assert(result.pessimistic_own_fill_microunits <= result.print_microunits);
    assert(result.expected_own_fill_microunits <= result.print_microunits);
    assert(result.optimistic_own_fill_microunits <= result.print_microunits);
}

void test_prearrival_print_cannot_deplete_queue_or_fill() {
    std::array<pm::v7::PaperRestingOrder, 1> orders{order(1, 100, 1000)};
    const auto before = orders[0];
    std::array<pm::v7::PaperFillEnvelope, 1> fills{};
    const auto result = pm::v7::allocate_public_print(
        sell_print(10, 50'000'000, 100, 1001), orders, fills);
    assert(!result.invalid_input);
    assert(result.causal_rejection);
    assert(result.output_count == 0);
    assert(orders[0].queue.ahead_lower_microunits == before.queue.ahead_lower_microunits);
    assert(orders[0].queue.ahead_expected_microunits == before.queue.ahead_expected_microunits);
    assert(orders[0].queue.ahead_upper_microunits == before.queue.ahead_upper_microunits);
}

void test_cancel_pending_remains_fillable_until_effective_time() {
    std::array<pm::v7::PaperRestingOrder, 1> before_cancel{order(1, 100, 1000)};
    before_cancel[0].cancel_effective_monotonic_ns = 2000;
    before_cancel[0].queue = pm::v7::QueueEnvelope{0, 0, 0, 0.5};
    std::array<pm::v7::PaperFillEnvelope, 1> fills{};
    auto result = pm::v7::allocate_public_print(
        sell_print(11, 2'000'000, 101, 1999), before_cancel, fills);
    assert(!result.invalid_input);
    assert(result.expected_own_fill_microunits == 2'000'000);

    std::array<pm::v7::PaperRestingOrder, 1> after_cancel{order(2, 100, 1000)};
    after_cancel[0].cancel_effective_monotonic_ns = 2000;
    after_cancel[0].queue = pm::v7::QueueEnvelope{0, 0, 0, 0.5};
    result = pm::v7::allocate_public_print(
        sell_print(12, 2'000'000, 102, 2000), after_cancel, fills);
    assert(!result.invalid_input);
    assert(result.output_count == 0);
    assert(result.expected_own_fill_microunits == 0);
}

void test_one_print_is_conserved_across_multiple_orders() {
    std::array<pm::v7::PaperRestingOrder, 2> orders{
        order(1, 100, 1000), order(2, 101, 1100)};
    for (auto& row : orders) row.queue = pm::v7::QueueEnvelope{0, 0, 0, 0.5};
    std::array<pm::v7::PaperFillEnvelope, 2> fills{};
    const auto result = pm::v7::allocate_public_print(
        sell_print(13, 15'000'000, 102, 1200), orders, fills);
    assert(!result.invalid_input);
    assert(result.output_count == 2);
    assert(result.expected_own_fill_microunits == 15'000'000);
    assert(fills[0].expected_fill_microunits == 10'000'000);
    assert(fills[1].expected_fill_microunits == 5'000'000);
    assert(result.expected_own_fill_microunits <= result.print_microunits);
}

void test_non_fifo_input_fails_closed_without_mutation() {
    std::array<pm::v7::PaperRestingOrder, 2> orders{
        order(1, 101, 1100), order(2, 100, 1000)};
    const auto first_before = orders[0].queue.ahead_expected_microunits;
    std::array<pm::v7::PaperFillEnvelope, 2> fills{};
    const auto result = pm::v7::allocate_public_print(
        sell_print(14, 20'000'000, 102, 1200), orders, fills);
    assert(result.invalid_input);
    assert(result.output_count == 0);
    assert(orders[0].queue.ahead_expected_microunits == first_before);
}

} // namespace

int main() {
    test_queue_envelope_produces_ordered_fill_scenarios();
    test_prearrival_print_cannot_deplete_queue_or_fill();
    test_cancel_pending_remains_fillable_until_effective_time();
    test_one_print_is_conserved_across_multiple_orders();
    test_non_fifo_input_fails_closed_without_mutation();
    return 0;
}
