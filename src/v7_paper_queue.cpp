#include "pm/v7_paper_queue.hpp"

#include <algorithm>
#include <cmath>

namespace pm::v7 {
namespace {

[[nodiscard]] bool compatible(const PaperRestingOrder& order,
                              const PublicTradePrint& trade) noexcept {
    if (order.side == Side::Buy) {
        return trade.aggressor_side == Side::Sell && trade.price_tick <= order.price_tick;
    }
    if (order.side == Side::Sell) {
        return trade.aggressor_side == Side::Buy && trade.price_tick >= order.price_tick;
    }
    return false;
}

[[nodiscard]] bool causally_after_arrival(const PaperRestingOrder& order,
                                          const PublicTradePrint& trade) noexcept {
    return trade.exchange_event_ns > order.arrival_exchange_event_ns
        && trade.receive_monotonic_ns > order.arrival_receive_monotonic_ns;
}

void consume_path(std::int64_t& available,
                  std::int64_t& queue_ahead,
                  std::int64_t& remaining,
                  std::int64_t& fill) noexcept {
    if (available <= 0 || remaining <= 0) return;
    const std::int64_t queue_used = std::min(std::max<std::int64_t>(0, queue_ahead), available);
    queue_ahead -= queue_used;
    available -= queue_used;
    if (available <= 0) return;
    fill = std::min(remaining, available);
    remaining -= fill;
    available -= fill;
}

} // namespace

bool QueueEnvelope::valid() const noexcept {
    return ahead_lower_microunits >= 0
        && ahead_expected_microunits >= ahead_lower_microunits
        && ahead_upper_microunits >= ahead_expected_microunits
        && std::isfinite(confidence) && confidence >= 0.0 && confidence <= 1.0;
}

bool PaperRestingOrder::valid() const noexcept {
    if (order_id == 0 || instrument_handle == 0 || side == Side::None || price_tick <= 0
        || original_microunits <= 0 || arrival_exchange_event_ns <= 0
        || arrival_receive_monotonic_ns <= 0 || !queue.valid()) {
        return false;
    }
    if (remaining_pessimistic_microunits < 0 || remaining_expected_microunits < 0
        || remaining_optimistic_microunits < 0) {
        return false;
    }
    if (remaining_pessimistic_microunits > original_microunits
        || remaining_expected_microunits > original_microunits
        || remaining_optimistic_microunits > original_microunits) {
        return false;
    }
    return cancel_effective_monotonic_ns == 0
        || cancel_effective_monotonic_ns >= arrival_receive_monotonic_ns;
}

bool PaperRestingOrder::cancel_effective_before(std::int64_t receive_ns) const noexcept {
    return cancel_effective_monotonic_ns > 0 && receive_ns >= cancel_effective_monotonic_ns;
}

bool PublicTradePrint::valid() const noexcept {
    return trade_id != 0 && instrument_handle != 0 && aggressor_side != Side::None
        && price_tick > 0 && quantity_microunits > 0
        && exchange_event_ns > 0 && receive_monotonic_ns > 0;
}

PaperPrintAllocation allocate_public_print(
    const PublicTradePrint& trade,
    std::span<PaperRestingOrder> fifo_orders,
    std::span<PaperFillEnvelope> output) noexcept {

    PaperPrintAllocation result;
    result.print_microunits = std::max<std::int64_t>(0, trade.quantity_microunits);
    if (!trade.valid() || output.size() < fifo_orders.size()) {
        result.invalid_input = 1;
        return result;
    }

    std::int64_t previous_arrival_receive = 0;
    std::int64_t previous_arrival_exchange = 0;
    for (const auto& order : fifo_orders) {
        if (!order.valid()) {
            result.invalid_input = 1;
            return result;
        }
        if (order.instrument_handle != trade.instrument_handle) continue;
        if (previous_arrival_receive > 0
            && (order.arrival_receive_monotonic_ns < previous_arrival_receive
                || (order.arrival_receive_monotonic_ns == previous_arrival_receive
                    && order.arrival_exchange_event_ns < previous_arrival_exchange))) {
            result.invalid_input = 1;
            return result;
        }
        previous_arrival_receive = order.arrival_receive_monotonic_ns;
        previous_arrival_exchange = order.arrival_exchange_event_ns;
    }

    std::int64_t pessimistic_available = trade.quantity_microunits;
    std::int64_t expected_available = trade.quantity_microunits;
    std::int64_t optimistic_available = trade.quantity_microunits;

    for (auto& order : fifo_orders) {
        if (order.instrument_handle != trade.instrument_handle || !compatible(order, trade)) continue;
        if (!causally_after_arrival(order, trade)) {
            result.causal_rejection = 1;
            continue;
        }
        if (order.cancel_effective_before(trade.receive_monotonic_ns)) continue;

        PaperFillEnvelope fill;
        fill.order_id = order.order_id;
        fill.trade_id = trade.trade_id;

        // Pessimistic PAPER path assumes the upper queue-ahead estimate,
        // expected path the central estimate, optimistic path the lower bound.
        consume_path(pessimistic_available, order.queue.ahead_upper_microunits,
                     order.remaining_pessimistic_microunits,
                     fill.pessimistic_fill_microunits);
        consume_path(expected_available, order.queue.ahead_expected_microunits,
                     order.remaining_expected_microunits,
                     fill.expected_fill_microunits);
        consume_path(optimistic_available, order.queue.ahead_lower_microunits,
                     order.remaining_optimistic_microunits,
                     fill.optimistic_fill_microunits);

        if (fill.pessimistic_fill_microunits > 0 || fill.expected_fill_microunits > 0
            || fill.optimistic_fill_microunits > 0) {
            output[result.output_count++] = fill;
        }
    }

    result.pessimistic_filled_microunits = trade.quantity_microunits - pessimistic_available;
    result.expected_filled_microunits = trade.quantity_microunits - expected_available;
    result.optimistic_filled_microunits = trade.quantity_microunits - optimistic_available;

    // These totals include public volume consumed by queue-ahead as well as own
    // fills. They can never exceed the single public print in any scenario.
    if (result.pessimistic_filled_microunits < 0
        || result.expected_filled_microunits < 0
        || result.optimistic_filled_microunits < 0
        || result.pessimistic_filled_microunits > trade.quantity_microunits
        || result.expected_filled_microunits > trade.quantity_microunits
        || result.optimistic_filled_microunits > trade.quantity_microunits) {
        result.invalid_input = 1;
    }
    return result;
}

} // namespace pm::v7
