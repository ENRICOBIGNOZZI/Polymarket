#pragma once

#include "pm/v7_execution_admission.hpp"
#include "pm/v7_execution_plan.hpp"
#include "pm/v7_maker_paper.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

namespace pm::v7::maker {

inline constexpr std::size_t kMaxMakerExecutionMarkets = kMaxCapitalMarkets;
inline constexpr std::size_t kMaxMakerBuyReservations = kMaxOpenCapitalReservations;

enum class MakerExecutionReason : std::uint8_t {
    Applied = 1,
    DuplicateNoop = 2,
    UnknownMarket = 3,
    UnsupportedPolicy = 4,
    QueueUnobservable = 5,
    AdmissionDenied = 6,
    PaperRejected = 7,
    CapitalInvariant = 8,
};

struct MakerExecutionResult {
    PaperMakerResult paper{};
    MakerExecutionReason reason = MakerExecutionReason::PaperRejected;
    std::uint8_t accepted = 0;
    std::uint8_t capital_invariant_violation = 0;
};

// PassiveMaker execution policy owned by the single V7 order-TX/execution core.
// It is deliberately not a strategy: it consumes a common ExecutionPlan and a
// common SleeveCapitalAccount. Market engines are mutable only here; hot shards
// must consume returned immutable snapshots/events instead of mutating PAPER
// order state themselves.
class MakerPaperExecutionPolicy final {
public:
    [[nodiscard]] bool register_market(std::uint64_t market_handle,
                                       std::uint64_t yes_instrument_handle,
                                       std::uint64_t no_instrument_handle,
                                       std::int32_t tick_size_e4,
                                       PaperMakerPolicy policy = {}) noexcept;

    [[nodiscard]] MakerExecutionResult process(
        const ExecutionPlan& plan,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] MakerExecutionResult on_public_trade(
        std::uint64_t market_handle,
        const PublicTradePrint& trade,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] MakerExecutionResult advance_time(
        std::uint64_t market_handle,
        std::int64_t monotonic_ns,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] MakerExecutionResult cancel_all(
        std::uint64_t market_handle,
        std::int64_t monotonic_ns,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] MakerExecutionResult liquidate_directional_inventory(
        std::uint64_t market_handle,
        std::int64_t yes_best_bid_tick,
        std::int64_t yes_bid_depth_microunits,
        std::int64_t no_best_bid_tick,
        std::int64_t no_bid_depth_microunits,
        std::int64_t timestamp_ns,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] MakerExecutionResult split_complete_sets(
        std::uint64_t market_handle,
        std::int64_t microunits,
        std::int64_t timestamp_ns,
        double yes_reference_price,
        SleeveCapitalAccount& capital) noexcept;

    [[nodiscard]] bool set_complete_set_reserve_floor(
        std::uint64_t market_handle,
        std::int64_t microunits) noexcept;

    [[nodiscard]] QuoteSnapshot quote_snapshot(std::uint64_t market_handle,
                                               std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] InventorySnapshot inventory_snapshot(
        std::uint64_t market_handle) const noexcept;
    [[nodiscard]] const PaperMakerInventory* inventory(
        std::uint64_t market_handle) const noexcept;

private:
    struct MarketSlot {
        std::optional<MakerPaperMarketEngine> engine{};
        std::uint64_t yes_instrument_handle = 0;
        std::uint64_t no_instrument_handle = 0;
        std::int32_t tick_size_e4 = 0;
    };

    enum class TrackState : std::uint8_t { Empty = 0, Active = 1, Tombstone = 2 };
    struct BuyReservationTrack {
        std::uint64_t intent_id = 0;
        std::uint64_t market_handle = 0;
        std::int64_t price_e4 = 0;
        std::int64_t original_microunits = 0;
        std::int64_t cumulative_fill_microunits = 0;
        std::int64_t settled_microdollars = 0;
        TrackState state = TrackState::Empty;
    };

    [[nodiscard]] MarketSlot* market(std::uint64_t market_handle) noexcept;
    [[nodiscard]] const MarketSlot* market(std::uint64_t market_handle) const noexcept;
    [[nodiscard]] std::size_t hash(std::uint64_t intent_id) const noexcept;
    [[nodiscard]] BuyReservationTrack* track(std::uint64_t intent_id) noexcept;
    [[nodiscard]] const BuyReservationTrack* track(std::uint64_t intent_id) const noexcept;
    [[nodiscard]] BuyReservationTrack* insert_track(std::uint64_t intent_id) noexcept;
    void erase_track(BuyReservationTrack& track) noexcept;

    [[nodiscard]] static bool price_e4(const StrategyIntent& intent,
                                       std::int32_t tick_size_e4,
                                       std::int64_t& out) noexcept;
    [[nodiscard]] static bool cumulative_notional_microdollars(
        std::int64_t cumulative_fill_microunits,
        std::int64_t price_e4,
        std::int64_t& out) noexcept;
    [[nodiscard]] bool process_paper_events(std::uint64_t market_handle,
                                            const PaperMakerResult& paper,
                                            SleeveCapitalAccount& capital) noexcept;
    [[nodiscard]] bool sync_inventory_capital(std::uint64_t market_handle,
                                              SleeveCapitalAccount& capital) noexcept;

    std::array<MarketSlot, kMaxMakerExecutionMarkets> markets_{};
    std::array<BuyReservationTrack, kMaxMakerBuyReservations> buy_tracks_{};
};

} // namespace pm::v7::maker
