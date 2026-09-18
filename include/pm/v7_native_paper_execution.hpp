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
};

struct NativePaperSubmitResult {
    NativePaperReason reason = NativePaperReason::InvalidCommand;
    OrderState final_state = OrderState::Unknown;
    std::uint64_t client_order_id = 0;
    std::int64_t filled_microunits = 0;
    std::uint8_t accepted = 0;
    std::uint8_t resting = 0;
};

struct NativePaperTradeResult {
    std::size_t fills = 0;
    std::int64_t filled_microunits = 0;
    std::uint8_t invalid = 0;
};

class NativePaperExecutionAdapter final {
public:
    explicit NativePaperExecutionAdapter(
        NativeSettlementOmsEndpoint& endpoint,
        std::int64_t cancel_latency_ns = 100'000'000LL) noexcept;

    [[nodiscard]] NativePaperSubmitResult submit(
        const NativeOrderCommand& command,
        const BookHotSnapshot& book,
        std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] bool request_cancel(
        const NativeCancelCommand& command,
        std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] NativePaperTradeResult on_public_trade(
        const PublicTradePrint& trade) noexcept;

    [[nodiscard]] bool advance_time(std::int64_t now_monotonic_ns) noexcept;

    [[nodiscard]] std::size_t resting_orders() const noexcept;
    [[nodiscard]] std::uint64_t synthetic_acks() const noexcept { return synthetic_acks_; }
    [[nodiscard]] std::uint64_t paper_fills() const noexcept { return paper_fills_; }
    [[nodiscard]] std::uint64_t paper_cancels() const noexcept { return paper_cancels_; }

private:
    struct Slot {
        PaperRestingOrder paper{};
        std::uint64_t client_order_id = 0;
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
    std::int64_t cancel_latency_ns_ = 100'000'000LL;
    std::array<Slot, kNativePaperOrderCapacity> slots_{};
    std::array<PaperFillEnvelope, kNativePaperOrderCapacity> fill_scratch_{};
    std::uint64_t synthetic_exchange_sequence_ = 0;
    std::uint64_t synthetic_acks_ = 0;
    std::uint64_t paper_fills_ = 0;
    std::uint64_t paper_cancels_ = 0;
};

} // namespace pm::v7
