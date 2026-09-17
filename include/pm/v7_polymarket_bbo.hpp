#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

namespace pm::v7::polymarket_bbo {

enum class SourceKind : std::uint8_t {
    PriceChange = 1,
    BestBidAsk = 2,
};

struct Binding {
    std::string asset_id;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
};

// Price-only top-of-book event for the reaction path. Queue/depth state remains
// owned by the canonical full L2 path; this object intentionally does not copy
// BookHotSnapshot or reconstruct depth.
struct Update {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int32_t best_bid_e4 = 0;
    std::int32_t best_ask_e4 = 0;
    SourceKind source = SourceKind::PriceChange;
    std::uint8_t valid = 0;
};

struct FrameResult {
    std::size_t output_count = 0;
    std::size_t recognized_updates = 0;
    std::size_t unknown_assets = 0;
    std::size_t incomplete_bbo = 0;
    std::uint8_t invalid_frame = 0;
    std::uint8_t output_overflow = 0;
};

class Decoder final {
public:
    explicit Decoder(std::vector<Binding> bindings);

    // Decodes only the venue-provided BBO surfaces used by the decision path:
    // `price_change[].best_bid/best_ask` and `best_bid_ask`. Full book snapshots,
    // trades, tick changes and recovery remain with MarketWsShard.
    [[nodiscard]] FrameResult decode(
        std::string_view payload,
        std::int64_t receive_monotonic_ns,
        std::span<Update> output) const noexcept;

private:
    [[nodiscard]] const Binding* binding(std::string_view asset_id) const noexcept;
    std::vector<Binding> bindings_;
};

static_assert(std::is_trivially_copyable_v<Update>);
static_assert(std::is_standard_layout_v<Update>);
static_assert(sizeof(Update) <= 64);

} // namespace pm::v7::polymarket_bbo
