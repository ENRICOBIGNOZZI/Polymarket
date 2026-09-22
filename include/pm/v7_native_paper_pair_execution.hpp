#pragma once

#include "pm/v7_market_state.hpp"
#include "pm/v7_native_order_tx.hpp"
#include "pm/v7_native_settlement_oms_endpoint.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7 {

inline constexpr std::size_t kNativePaperPairCapacity = 32;

enum class NativePaperPairReason : std::uint8_t {
    Accepted = 1,
    PendingArrival = 2,
    InvalidPair = 3,
    VenueTermsUnknown = 4,
    ArrivalCensored = 5,
    BookUnavailable = 6,
    NotMarketable = 7,
    InsufficientDepth = 8,
    LifecycleFailure = 9,
    CapacityFull = 10,
};

struct NativePaperPairLegFill {
    NativeOrderCommand command{};
    std::int64_t fill_microunits = 0;
    std::int32_t conservative_fill_price_e4 = 0;
    double vwap_e4 = 0.0;
    std::uint16_t levels_used = 0;
    OrderState final_state = OrderState::Unknown;
};

struct NativePaperPairResult {
    NativePaperPairReason reason = NativePaperPairReason::InvalidPair;
    NativePaperPairLegFill yes{};
    NativePaperPairLegFill no{};
    std::int64_t scheduled_arrival_ns = 0;
    std::int64_t evaluated_arrival_ns = 0;
    std::uint8_t accepted = 0;
    std::uint8_t pending_arrival = 0;
    std::uint8_t censored = 0;
};

struct NativePaperPairArrivalBatch {
    std::array<NativePaperPairResult, kNativePaperPairCapacity> records{};
    std::size_t count = 0;
    std::uint8_t invalid = 0;
};

class NativePaperPairExecutionAdapter final {
public:
    explicit NativePaperPairExecutionAdapter(
        NativeSettlementOmsEndpoint& endpoint,
        std::int64_t taker_delay_ns,
        std::int64_t maximum_arrival_book_age_ns = 100'000'000LL) noexcept;

    [[nodiscard]] NativePaperPairResult submit(
        const NativeOrderCommand& yes,
        const NativeOrderCommand& no,
        const BookHotSnapshot& yes_book,
        const BookHotSnapshot& no_book,
        std::int64_t now_monotonic_ns) noexcept;

    // Call before consuming the first PM event whose receive watermark is
    // strictly after the scheduled pair arrival. yes_book/no_book must be the
    // last causal books consumed before that watermark.
    [[nodiscard]] NativePaperPairArrivalBatch advance_arrivals(
        const BookHotSnapshot& yes_book,
        const BookHotSnapshot& no_book,
        std::int64_t receive_watermark_ns) noexcept;

    void invalidate_arrivals() noexcept;
    [[nodiscard]] std::size_t pending_arrivals() const noexcept;
    [[nodiscard]] std::uint64_t complete_pairs() const noexcept {
        return complete_pairs_;
    }
    [[nodiscard]] std::uint64_t rejected_pairs() const noexcept {
        return rejected_pairs_;
    }

private:
    struct PendingPair {
        NativeOrderCommand yes{};
        NativeOrderCommand no{};
        std::int64_t deadline_ns = 0;
        std::uint8_t invalidated = 0;
    };
    struct Preflight {
        std::int64_t quantity_microunits = 0;
        std::int32_t conservative_fill_price_e4 = 0;
        double vwap_e4 = 0.0;
        std::uint16_t levels_used = 0;
        NativePaperPairReason reason = NativePaperPairReason::InvalidPair;
        std::uint8_t fillable = 0;
    };

    [[nodiscard]] Preflight preflight(
        const NativeOrderCommand& command,
        const BookHotSnapshot& book) const noexcept;
    [[nodiscard]] NativePaperPairResult execute_now(
        const NativeOrderCommand& yes,
        const NativeOrderCommand& no,
        const BookHotSnapshot& yes_book,
        const BookHotSnapshot& no_book,
        std::int64_t now_monotonic_ns) noexcept;
    [[nodiscard]] bool reject_pair(
        const NativeOrderCommand& yes,
        const NativeOrderCommand& no,
        std::int64_t timestamp_ns) noexcept;
    [[nodiscard]] bool release_delay(
        const NativeOrderCommand& command,
        std::int64_t timestamp_ns) noexcept;
    [[nodiscard]] bool fill_leg(
        const NativeOrderCommand& command,
        const Preflight& preflight,
        std::int64_t now_ns,
        NativePaperPairLegFill& out) noexcept;

    NativeSettlementOmsEndpoint& endpoint_;
    std::int64_t taker_delay_ns_ = -1;
    std::int64_t maximum_arrival_book_age_ns_ = 100'000'000LL;
    std::array<PendingPair, kNativePaperPairCapacity> pending_{};
    std::uint64_t exchange_sequence_ = 0;
    std::uint64_t complete_pairs_ = 0;
    std::uint64_t rejected_pairs_ = 0;
};

} // namespace pm::v7
