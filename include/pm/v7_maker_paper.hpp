#pragma once

#include "pm/v7_maker_hft.hpp"
#include "pm/v7_oms.hpp"
#include "pm/v7_paper_queue.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace pm::v7::maker {

inline constexpr std::size_t kMaxPaperOrdersPerMarket = 16;
inline constexpr std::size_t kRecentPublicTradeIds = 4096;

enum class PaperMakerEventKind : std::uint8_t {
    OrderLive = 1,
    CancelRequested = 2,
    Cancelled = 3,
    Fill = 4,
    FinalMerge = 5,
    Rejected = 6,
};

struct PaperMakerPolicy {
    std::int64_t assumed_submission_latency_ns = 1'000'000LL;
    std::int64_t cancel_latency_ns = 100'000'000LL;
    double queue_expected_multiplier = 1.25;
    double queue_upper_multiplier = 1.50;
    double queue_confidence = 0.50;
    std::uint8_t operational_fill_is_pessimistic = 1;

    [[nodiscard]] bool valid() const noexcept;
};

struct PaperMakerEvent {
    PaperMakerEventKind kind = PaperMakerEventKind::Rejected;
    std::uint64_t order_id = 0;
    std::uint64_t intent_id = 0;
    std::uint64_t trade_id = 0;
    std::uint64_t instrument_handle = 0;
    Side side = Side::None;
    OrderState order_state = OrderState::Intent;
    std::int64_t timestamp_ns = 0;
    std::int64_t price_tick = 0;
    std::int32_t tick_size_e4 = 0;
    std::int64_t original_microunits = 0;
    std::int64_t operational_fill_microunits = 0;
    std::int64_t pessimistic_fill_microunits = 0;
    std::int64_t expected_fill_microunits = 0;
    std::int64_t optimistic_fill_microunits = 0;
    QueueEnvelope queue{};
    double realized_pnl = 0.0;
};

struct PaperMakerResult {
    std::array<PaperMakerEvent, kMaxPaperOrdersPerMarket + 2> events{};
    std::size_t event_count = 0;
    std::uint8_t applied = 0;
    std::uint8_t duplicate_trade = 0;
    std::uint8_t rejected = 0;
    std::uint8_t invariant_violation = 0;
};

struct PaperMakerInventory {
    std::int64_t yes_microunits = 0;
    std::int64_t no_microunits = 0;
    double yes_cost = 0.0;
    double no_cost = 0.0;
    double realized_trading_pnl = 0.0;
};

// PAPER-only market-local execution state. One instance owns exactly one binary
// market's YES/NO hypothetical orders. It never talks to an authenticated
// endpoint and it never writes the canonical ledger; slow telemetry serializes
// its events through the existing V7 spool/single-writer path.
class MakerPaperMarketEngine final {
public:
    MakerPaperMarketEngine(std::uint64_t market_handle,
                           std::uint64_t yes_instrument_handle,
                           std::uint64_t no_instrument_handle,
                           PaperMakerPolicy policy = {}) noexcept;

    [[nodiscard]] PaperMakerResult apply_intent(
        const StrategyIntent& intent,
        std::int64_t visible_queue_ahead_microunits,
        std::int32_t tick_size_e4) noexcept;

    [[nodiscard]] PaperMakerResult on_public_trade(
        const PublicTradePrint& trade) noexcept;

    [[nodiscard]] PaperMakerResult advance_time(
        std::int64_t monotonic_ns) noexcept;

    [[nodiscard]] QuoteSnapshot quote_snapshot(
        std::uint64_t instrument_handle) const noexcept;

    [[nodiscard]] InventorySnapshot inventory_snapshot() const noexcept;
    [[nodiscard]] const PaperMakerInventory& inventory() const noexcept { return inventory_; }
    [[nodiscard]] std::size_t active_order_count() const noexcept;

private:
    struct Slot {
        OmsOrder oms{};
        PaperRestingOrder paper{};
        std::uint64_t intent_id = 0;
        std::int32_t tick_size_e4 = 0;
        std::uint8_t occupied = 0;
    };

    [[nodiscard]] bool instrument_known(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] bool is_yes(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] bool trade_seen(std::uint64_t trade_id) const noexcept;
    void remember_trade(std::uint64_t trade_id) noexcept;
    [[nodiscard]] Slot* free_slot() noexcept;
    [[nodiscard]] Slot* active_slot(std::uint64_t instrument_handle, Side side) noexcept;
    [[nodiscard]] const Slot* active_slot(std::uint64_t instrument_handle, Side side) const noexcept;
    [[nodiscard]] std::int64_t available_to_sell(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] QueueEnvelope make_queue(std::int64_t visible) const noexcept;
    void emit(PaperMakerResult& result, PaperMakerEvent event) const noexcept;
    void request_cancel(Slot& slot, std::int64_t now, PaperMakerResult& result) noexcept;
    void apply_operational_fill(Slot& slot, std::int64_t fill_microunits,
                                std::int64_t timestamp_ns, PaperMakerEvent& event) noexcept;
    void maybe_merge(std::int64_t timestamp_ns, PaperMakerResult& result) noexcept;
    [[nodiscard]] OmsEvent oms_event(OmsEventType type, std::int64_t timestamp_ns) noexcept;

    std::uint64_t market_handle_ = 0;
    std::uint64_t yes_instrument_handle_ = 0;
    std::uint64_t no_instrument_handle_ = 0;
    PaperMakerPolicy policy_{};
    PaperMakerInventory inventory_{};
    std::array<Slot, kMaxPaperOrdersPerMarket> slots_{};
    std::array<std::uint64_t, kRecentPublicTradeIds> recent_trade_ids_{};
    std::size_t recent_trade_cursor_ = 0;
    std::uint64_t order_sequence_ = 0;
    std::uint64_t oms_event_sequence_ = 0;
    std::uint64_t source_version_ = 0;
};

} // namespace pm::v7::maker
