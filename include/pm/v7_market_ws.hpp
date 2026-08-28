#pragma once

#include "pm/fast_ws.hpp"
#include "pm/v7_market_state.hpp"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace pm::v7 {

struct TokenBinding {
    std::string asset_id;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::int32_t tick_size_e4 = 100;
};

enum class MarketWsEventKind : std::uint8_t {
    BookChanged = 1,
    Trade = 2,
    TickSizeChanged = 3,
    LineageInvalidated = 4,
};

struct MarketWsEvent {
    MarketWsEventKind kind = MarketWsEventKind::BookChanged;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    Side side = Side::None;
    std::int32_t price_e4 = 0;
    std::int64_t quantity_microunits = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    BookHotSnapshot book{};
};

struct MarketWsFrameResult {
    std::size_t output_count = 0;
    std::size_t recognized_events = 0;
    std::size_t applied_state_events = 0;
    std::size_t trade_events = 0;
    std::size_t ignored_unknown_assets = 0;
    std::uint8_t invalid_frame = 0;
    std::uint8_t arena_exhausted = 0;
    std::uint8_t output_overflow = 0;
    std::uint8_t lineage_invalidated = 0;
};

// One instance is owned by one public-feed shard. All mappings and JSON memory
// are allocated at cold start. process_frame itself uses a fixed JSON arena and
// caller-provided output storage; it never falls back to heap allocation.
class MarketWsShard final {
public:
    explicit MarketWsShard(std::vector<TokenBinding> bindings);
    ~MarketWsShard();

    MarketWsShard(const MarketWsShard&) = delete;
    MarketWsShard& operator=(const MarketWsShard&) = delete;

    [[nodiscard]] MarketWsFrameResult process_frame(
        std::string_view payload,
        const pm::fast::FeedReceiveStamp& receive,
        std::span<MarketWsEvent> output) noexcept;

    // Call on transport error/reconnect before consuming any new delta. A fresh
    // full book snapshot is required to restore continuity per instrument.
    void invalidate_all_lineage() noexcept;

    [[nodiscard]] BookHotSnapshot snapshot(std::uint64_t instrument_handle) const noexcept;
    [[nodiscard]] std::size_t instrument_count() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7
