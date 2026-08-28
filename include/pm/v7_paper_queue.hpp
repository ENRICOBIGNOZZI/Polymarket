#pragma once

#include "pm/v7_intent.hpp"

#include <cstddef>
#include <cstdint>
#include <span>
#include <type_traits>

namespace pm::v7 {

struct QueueEnvelope {
    std::int64_t ahead_lower_microunits = 0;
    std::int64_t ahead_expected_microunits = 0;
    std::int64_t ahead_upper_microunits = 0;
    double confidence = 0.0;

    [[nodiscard]] bool valid() const noexcept;
};

struct PaperRestingOrder {
    std::uint64_t order_id = 0;
    std::uint64_t instrument_handle = 0;
    Side side = Side::None;
    std::int64_t price_tick = 0;
    std::int64_t original_microunits = 0;
    std::int64_t arrival_exchange_event_ns = 0;
    std::int64_t arrival_receive_monotonic_ns = 0;
    std::int64_t cancel_effective_monotonic_ns = 0;
    QueueEnvelope queue{};
    std::int64_t remaining_pessimistic_microunits = 0;
    std::int64_t remaining_expected_microunits = 0;
    std::int64_t remaining_optimistic_microunits = 0;

    [[nodiscard]] bool valid() const noexcept;
    [[nodiscard]] bool cancel_effective_before(std::int64_t receive_ns) const noexcept;
};

struct PublicTradePrint {
    std::uint64_t trade_id = 0;
    std::uint64_t instrument_handle = 0;
    Side aggressor_side = Side::None;
    std::int64_t price_tick = 0;
    std::int64_t quantity_microunits = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_monotonic_ns = 0;

    [[nodiscard]] bool valid() const noexcept;
};

struct PaperFillEnvelope {
    std::uint64_t order_id = 0;
    std::uint64_t trade_id = 0;
    std::int64_t pessimistic_fill_microunits = 0;
    std::int64_t expected_fill_microunits = 0;
    std::int64_t optimistic_fill_microunits = 0;
};

struct PaperPrintAllocation {
    std::int64_t print_microunits = 0;
    std::int64_t pessimistic_consumed_microunits = 0;
    std::int64_t expected_consumed_microunits = 0;
    std::int64_t optimistic_consumed_microunits = 0;
    std::int64_t pessimistic_own_fill_microunits = 0;
    std::int64_t expected_own_fill_microunits = 0;
    std::int64_t optimistic_own_fill_microunits = 0;
    std::size_t output_count = 0;
    std::uint8_t causal_rejection = 0;
    std::uint8_t invalid_input = 0;
};

// Applies one public print once per queue scenario. The order span must be in
// deterministic FIFO arrival order for the same instrument. No output vector is
// allocated: the caller supplies a fixed/preallocated result span. Queue-ahead
// depletion and own fills share the same public volume within each scenario.
[[nodiscard]] PaperPrintAllocation allocate_public_print(
    const PublicTradePrint& trade,
    std::span<PaperRestingOrder> fifo_orders,
    std::span<PaperFillEnvelope> output) noexcept;

static_assert(std::is_trivially_copyable_v<QueueEnvelope>);
static_assert(std::is_trivially_copyable_v<PaperRestingOrder>);
static_assert(std::is_trivially_copyable_v<PublicTradePrint>);
static_assert(std::is_trivially_copyable_v<PaperFillEnvelope>);

} // namespace pm::v7
