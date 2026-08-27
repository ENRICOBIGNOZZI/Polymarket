#pragma once

#include "pm/v7_intent.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <type_traits>

namespace pm::v7 {

// Polymarket prices live on [0,1]. A 1e-4 canonical quantum covers the venue
// tick regimes used by V7 while keeping level mutation direct-indexed.
inline constexpr std::int32_t kCanonicalPriceScale = 10'000;
inline constexpr std::size_t kCanonicalPriceSlots = 10'001;
inline constexpr std::size_t kOccupancyWords = (kCanonicalPriceSlots + 63) / 64;

struct PriceLevelE4 {
    std::int32_t price_e4 = 0;
    std::int64_t quantity_microunits = 0;
};

struct DepthSummary {
    std::int64_t l1_microunits = 0;
    std::int64_t l5_microunits = 0;
    std::int64_t l10_microunits = 0;
};

struct BookHotSnapshot {
    std::uint64_t state_version = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int32_t tick_size_e4 = 100;
    std::int32_t best_bid_e4 = 0;
    std::int32_t best_ask_e4 = 0;
    std::int64_t best_bid_microunits = 0;
    std::int64_t best_ask_microunits = 0;
    DepthSummary bid_depth{};
    DepthSummary ask_depth{};
    std::uint8_t valid = 0;
};

// Single-writer fixed-domain L2 state. This is V7-common market state, not a
// Maker-private book. It is constructed once and mutated by the owning feed
// shard; Maker consumes BookHotSnapshot/reference-derived state only.
class CanonicalL2Book final {
public:
    explicit CanonicalL2Book(std::int32_t tick_size_e4 = 100) noexcept;

    [[nodiscard]] bool set_tick_size(std::int32_t tick_size_e4) noexcept;
    [[nodiscard]] bool change_tick_size(std::int32_t tick_size_e4,
                                        std::int64_t exchange_event_ns,
                                        std::int64_t receive_monotonic_ns) noexcept;
    [[nodiscard]] bool replace_snapshot(std::span<const PriceLevelE4> bids,
                                        std::span<const PriceLevelE4> asks,
                                        std::int64_t exchange_event_ns,
                                        std::int64_t receive_monotonic_ns) noexcept;
    [[nodiscard]] bool mutate_level(Side side, std::int32_t price_e4,
                                    std::int64_t quantity_microunits,
                                    std::int64_t exchange_event_ns,
                                    std::int64_t receive_monotonic_ns) noexcept;

    [[nodiscard]] BookHotSnapshot hot_snapshot() const noexcept;
    [[nodiscard]] std::int64_t quantity_at(Side side, std::int32_t price_e4) const noexcept;
    [[nodiscard]] std::int32_t venue_tick_index(std::int32_t price_e4) const noexcept;
    [[nodiscard]] std::int32_t tick_size_e4() const noexcept { return tick_size_e4_; }
    [[nodiscard]] std::uint64_t state_version() const noexcept { return state_version_; }

private:
    [[nodiscard]] bool valid_level(std::int32_t price_e4,
                                   std::int64_t quantity_microunits) const noexcept;
    void clear() noexcept;
    void set_raw(Side side, std::int32_t price_e4,
                 std::int64_t quantity_microunits) noexcept;
    [[nodiscard]] std::int32_t best_bid() const noexcept;
    [[nodiscard]] std::int32_t best_ask() const noexcept;
    [[nodiscard]] std::int32_t next_bid(std::int32_t from_exclusive) const noexcept;
    [[nodiscard]] std::int32_t next_ask(std::int32_t from_exclusive) const noexcept;
    [[nodiscard]] DepthSummary depth_summary(Side side) const noexcept;

    std::array<std::int64_t, kCanonicalPriceSlots> bid_qty_{};
    std::array<std::int64_t, kCanonicalPriceSlots> ask_qty_{};
    std::array<std::uint64_t, kOccupancyWords> bid_occupied_{};
    std::array<std::uint64_t, kOccupancyWords> ask_occupied_{};
    std::uint64_t state_version_ = 0;
    std::int64_t exchange_event_ns_ = 0;
    std::int64_t receive_monotonic_ns_ = 0;
    std::int32_t tick_size_e4_ = 100;
    std::int32_t best_bid_e4_ = 0;
    std::int32_t best_ask_e4_ = 0;
};

static_assert(std::is_trivially_copyable_v<PriceLevelE4>);
static_assert(std::is_trivially_copyable_v<BookHotSnapshot>);

} // namespace pm::v7
