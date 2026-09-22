#pragma once

#include "pm/v7_market_ws.hpp"
#include "pm/v7_native_latency_tape.hpp"
#include "pm/v7_native_settlement_authority.hpp"
#include "pm/v7_pure_arb_lane.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <type_traits>

namespace pm::v7::pure_arb {

inline constexpr std::size_t kPureArbMultiMarketCapacity = 64;
inline constexpr std::size_t kPureArbInstrumentMapCapacity = 256;

struct MultiMarketConfig {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    std::int64_t market_start_wall_ms = 0;
    std::int64_t market_end_wall_ms = 0;
    std::int64_t maximum_leg_skew_ns = 100'000'000LL;
    std::int64_t minimum_order_microunits = 0;
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    double reserve_per_share = 0.0005;
    std::uint8_t fee_verified = 0;
};

struct MultiMarketDecision {
    PureArbExecutionPlan plan{};
    NativeSettlementPairResult admission{};
    std::uint64_t context_index = 0;
    std::uint8_t evaluated = 0;
    std::uint8_t admitted = 0;
};

class MultiMarketEngine final {
public:
    MultiMarketEngine(
        std::span<const MultiMarketConfig> configs,
        NativeSettlementAuthority& authority,
        NativeLatencyTape* latency_tape = nullptr) noexcept;

    [[nodiscard]] bool valid() const noexcept { return valid_; }
    [[nodiscard]] std::size_t market_count() const noexcept { return count_; }

    // Single-owner direct callback path. Caller guarantees all invocations are
    // serialized on the one PM socket worker that owns this authority.
    [[nodiscard]] MultiMarketDecision on_market_event(
        const MarketWsEvent& event,
        std::uint64_t connection_epoch,
        std::int64_t now_wall_ms) noexcept;

    void invalidate_all() noexcept;

private:
    struct MarketState {
        MultiMarketConfig config{};
        BookHotSnapshot yes{};
        BookHotSnapshot no{};
        std::uint64_t yes_epoch = 0;
        std::uint64_t no_epoch = 0;
    };

    struct InstrumentMap {
        std::uint64_t instrument_handle = 0;
        std::uint16_t context_index = 0;
        std::uint8_t yes_leg = 0;
        std::uint8_t occupied = 0;
    };

    [[nodiscard]] InstrumentMap* lookup(std::uint64_t instrument) noexcept;
    [[nodiscard]] const InstrumentMap* lookup(std::uint64_t instrument) const noexcept;
    [[nodiscard]] bool install(
        std::uint64_t instrument, std::uint16_t context, bool yes) noexcept;
    [[nodiscard]] std::uint64_t next_intent_id() noexcept;
    void publish_prefix(
        std::uint64_t client_order_id,
        const PureArbExecutionPlan& plan,
        std::int64_t risk_admitted_ns) noexcept;

    NativeSettlementAuthority& authority_;
    NativeLatencyTape* latency_tape_ = nullptr;
    std::array<MarketState, kPureArbMultiMarketCapacity> markets_{};
    std::array<InstrumentMap, kPureArbInstrumentMapCapacity> instrument_map_{};
    std::size_t count_ = 0;
    std::uint64_t next_intent_id_ = 0;
    bool valid_ = false;
};

static_assert(std::is_trivially_copyable_v<MultiMarketConfig>);
static_assert(std::is_trivially_copyable_v<MultiMarketDecision>);

} // namespace pm::v7::pure_arb
