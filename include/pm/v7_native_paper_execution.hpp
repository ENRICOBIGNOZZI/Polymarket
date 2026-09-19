#pragma once

#include "pm/v7_native_order_tx.hpp"
#include "pm/v7_native_settlement_oms_endpoint.hpp"
#include "pm/v7_paper_queue.hpp"
#include "pm/v7_market_state.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

namespace pm::v7 {

inline constexpr std::size_t kNativePaperOrderCapacity = 64;

enum class NativePaperReason : std::uint8_t {
    Accepted = 1,
    InvalidCommand = 2,
    BookUnavailable = 3,
    NotMarketable = 4,
    InsufficientDepth = 5,
    CapacityFull = 6,
    LifecycleFailure = 7,
    UnknownOrder = 8,
    PendingArrival = 9,
    ArrivalCensored = 10,
    VenueTermsUnknown = 11,
    PriceImprovementUnmodelled = 12,
    PartialFillUnmodelled = 13,
    DepthAccountingUnavailable = 14,
    PartialFillModelled = 15,
};

struct NativePaperFillRecord {
    NativeOrderCommand command{};
    StrategyId strategy_id = StrategyId::CryptoSettlementEngine;
    std::uint64_t client_order_id = 0;
    std::uint64_t command_id = 0;
    std::uint64_t instrument_handle = 0;
    Side side = Side::None;
    std::int64_t price_tick = 0;
    std::int32_t tick_size_e4 = 0;
    std::int64_t fill_microunits = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    OrderState order_state = OrderState::Unknown;
    std::uint8_t taker = 0;
    std::int64_t arrival_book_receive_ns = 0;
    std::uint64_t arrival_book_version = 0;
    std::uint8_t causal_arrival_modelled = 0;
};

struct NativePaperCancelRecord {
    NativeOrderCommand command{};
    StrategyId strategy_id = StrategyId::CryptoSettlementEngine;
    std::int64_t cancel_effective_monotonic_ns = 0;
};

struct NativePaperAdvanceResult {
    std::array<NativePaperCancelRecord, kNativePaperOrderCapacity> cancellations{};
    std::size_t cancellation_count = 0;
    std::uint8_t invalid = 0;
};

struct NativePaperSubmitResult {
    NativePaperReason reason = NativePaperReason::InvalidCommand;
    OrderState final_state = OrderState::Unknown;
    std::uint64_t client_order_id = 0;
    std::int64_t filled_microunits = 0;
    NativePaperFillRecord fill{};
    std::uint8_t accepted = 0;
    std::uint8_t resting = 0;
    std::uint8_t pending_arrival = 0;
    std::uint8_t censored = 0;
};

struct NativePaperTradeResult {
    std::array<NativePaperFillRecord, kNativePaperOrderCapacity> records{};
    std::size_t fills = 0;
    std::int64_t filled_microunits = 0;
    std::uint8_t invalid = 0;
};

struct NativePaperArrivalRecord {
    NativeOrderCommand command{};
    NativePaperSubmitResult result{};
};
struct NativePaperArrivalBatch {
    std::array<NativePaperArrivalRecord, kNativePaperOrderCapacity> records{};
    std::size_t count = 0;
    std::uint8_t invalid = 0;
};

class NativePaperExecutionAdapter final {
public:
    explicit NativePaperExecutionAdapter(
        NativeSettlementOmsEndpoint& endpoint,
        std::int64_t cancel_latency_ns = 100'000'000LL,
        std::int64_t taker_delay_ns = 0,
        std::int64_t maximum_arrival_book_age_ns = 100'000'000LL) noexcept;

    // A strict receive watermark must pass the scheduled arrival before the
    // previous consumed book can be used. A later book is never substituted.
    [[nodiscard]] NativePaperArrivalBatch advance_arrivals(
        std::uint64_t instrument, const BookHotSnapshot& previous_book,
        std::int64_t receive_watermark_ns) noexcept;
    void invalidate_arrivals() noexcept;
    [[nodiscard]] std::size_t pending_arrivals() const noexcept;

    [[nodiscard]] NativePaperSubmitResult submit(
        const NativeOrderCommand& command,
        const BookHotSnapshot& book,
        std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] bool request_cancel(
        const NativeCancelCommand& command,
        std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] NativePaperTradeResult on_public_trade(
        const PublicTradePrint& trade) noexcept;

    [[nodiscard]] NativePaperAdvanceResult advance_time(
        std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] std::size_t resting_orders() const noexcept;
    [[nodiscard]] std::uint64_t synthetic_acks() const noexcept { return synthetic_acks_; }
    [[nodiscard]] std::uint64_t paper_fills() const noexcept { return paper_fills_; }
    [[nodiscard]] std::uint64_t paper_cancels() const noexcept { return paper_cancels_; }

private:
    struct PendingArrival {
        NativeOrderCommand command{};
        std::int64_t deadline_ns = 0;
        std::uint8_t invalidated = 0;
    };
    struct ConsumedTop {
        std::uint64_t instrument = 0;
        Side side = Side::None;
        std::int32_t price_e4 = 0;
        std::int64_t visible = 0, remaining = 0;
    };
    [[nodiscard]] NativePaperSubmitResult match_now(
        const NativeOrderCommand& command, const BookHotSnapshot& book,
        std::int64_t now_monotonic_ns) noexcept;
    [[nodiscard]] ConsumedTop* available_top(
        const NativeOrderCommand& command, const BookHotSnapshot& book) noexcept;
    struct Slot {
        PaperRestingOrder paper{};
        NativeOrderCommand command{};
        StrategyId strategy_id = StrategyId::CryptoSettlementEngine;
        std::uint64_t client_order_id = 0;
        std::uint64_t command_id = 0;
        std::int32_t tick_size_e4 = 0;
        std::int64_t cancel_deadline_ns = 0;
        std::uint8_t occupied = 0;
    };

    [[nodiscard]] Slot* free_slot() noexcept;
    [[nodiscard]] Slot* find(std::uint64_t client_order_id) noexcept;
    [[nodiscard]] std::int64_t visible_queue(
        const NativeOrderCommand& command,
        const BookHotSnapshot& book) const noexcept;
    [[nodiscard]] bool live_locally(
        const NativeOrderCommand& command,
        std::int64_t now_monotonic_ns) noexcept;
    void clear(Slot& slot) noexcept;

    NativeSettlementOmsEndpoint& endpoint_;
    std::int64_t taker_delay_ns_ = 0;
    std::int64_t maximum_arrival_book_age_ns_ = 100'000'000LL;
    std::array<PendingArrival, kNativePaperOrderCapacity> pending_{};
    std::array<ConsumedTop, 256> consumed_tops_{};
    std::int64_t cancel_latency_ns_ = 100'000'000LL;
    std::array<Slot, kNativePaperOrderCapacity> slots_{};
    std::array<PaperFillEnvelope, kNativePaperOrderCapacity> fill_scratch_{};
    std::uint64_t synthetic_exchange_sequence_ = 0;
    std::uint64_t synthetic_acks_ = 0;
    std::uint64_t paper_fills_ = 0;
    std::uint64_t paper_cancels_ = 0;
};

} // namespace pm::v7
