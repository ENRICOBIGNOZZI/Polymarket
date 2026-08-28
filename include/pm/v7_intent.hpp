#pragma once

#include <cstdint>
#include <type_traits>

namespace pm::v7 {

// Hot-path strategy intents use numeric handles assigned by the canonical V7
// state registry. Human-readable market/token identifiers are resolved only at
// cold-path boundaries; they are deliberately absent from this POD contract.
enum class StrategyId : std::uint16_t {
    ProfessionalMaker = 1,
    FastStructural = 2,
    GraphRelativeValue = 3,
    CrossSectionalRanking = 4,
    PcaStatArb = 5,
    LocalFactor = 6,
    MicroTaker = 7,
    ExternalInformation = 8,
};

enum class IntentType : std::uint8_t {
    Quote = 1,
    CancelQuote = 2,
    Withdraw = 3,
    Kill = 4,
    TargetPosition = 5,
    MultiLegTarget = 6,
    ExecuteBasket = 7,
};

enum class Side : std::uint8_t {
    None = 0,
    Buy = 1,
    Sell = 2,
};

enum class Urgency : std::uint8_t {
    Passive = 0,
    Normal = 1,
    Aggressive = 2,
    Critical = 3,
};

struct StrategyIntent {
    std::uint64_t intent_id = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::uint64_t model_version = 0;
    std::uint64_t policy_version = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t price_tick = 0;
    std::int64_t quantity_microunits = 0;
    std::uint32_t horizon_ms = 0;
    StrategyId strategy_id = StrategyId::ProfessionalMaker;
    IntentType type = IntentType::Quote;
    Side side = Side::None;
    Urgency urgency = Urgency::Normal;
    std::uint8_t passive = 1;
    std::uint8_t post_only = 1;
    std::uint8_t reserved0 = 0;
    std::uint8_t reserved1 = 0;
    double expected_edge = 0.0;
    double expected_cost = 0.0;
    double expected_risk = 0.0;
    double expected_ev = 0.0;
    double ev_uncertainty = 0.0;
};

[[nodiscard]] constexpr bool is_critical_intent(IntentType type) noexcept {
    return type == IntentType::CancelQuote || type == IntentType::Withdraw || type == IntentType::Kill;
}

static_assert(std::is_trivially_copyable_v<StrategyIntent>);
static_assert(std::is_standard_layout_v<StrategyIntent>);

} // namespace pm::v7
