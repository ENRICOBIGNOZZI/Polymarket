#pragma once

#include "pm/v7_external_execution.hpp"
#include "pm/v7_external_state.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::size_t kMaxRestingQuotes = 4;
inline constexpr std::size_t kMaxKernelCandidates = 16;

struct RestingQuoteSnapshot {
    std::uint64_t order_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::int64_t price_tick = 0;
    double price = 0.0;
    Side side = Side::None;
    std::uint8_t instrument_is_yes = 0;
    std::uint8_t active = 0;
    std::uint8_t cancel_pending = 0;
    std::uint8_t reserved = 0;
};

struct MakerTouchSnapshot {
    std::int64_t best_bid_tick = 0;
    std::int64_t best_ask_tick = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 7> reserved{};
};

struct ExternalKernelInput {
    ContractHotSpec contract{};
    SettlementReferenceSnapshot reference{};
    OracleSnapshot oracle{};
    ExternalAssetSnapshot external{};
    FairValueModelSnapshot model{};
    FeeScheduleSnapshot fee{};
    ExternalFairPolicy policy{};
    ExternalMakerPolicy maker_policy{};
    AggressiveBook yes_book{};
    AggressiveBook no_book{};
    MakerTouchSnapshot yes_touch{};
    MakerTouchSnapshot no_touch{};
    CausalStateInputs causal_inputs{};
    std::array<RestingQuoteSnapshot, kMaxRestingQuotes> resting_quotes{};
    std::uint64_t causal_cut_id = 0;
    std::uint64_t trigger_source_version = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::int64_t time_to_expiry_ns = 0;
    double model_drift_score = 0.0;
    double available_cash = 0.0;
    double current_market_exposure = 0.0;
    double expected_taker_latency_seconds = 0.0;
    double signed_yes_inventory_fraction = 0.0;
    double signed_no_inventory_fraction = 0.0;
    double tick_size = 0.01;
    TriggerSource trigger_source = TriggerSource::ExternalPriceUpdate;
    std::uint8_t global_kill = 0;
    std::uint8_t strategy_kill = 0;
    std::uint8_t market_kill = 0;
    std::uint8_t cancel_path_healthy = 1;
    std::uint8_t allow_shadow_taker = 1;
    std::uint8_t allow_shadow_maker = 1;
    std::array<std::uint8_t, 2> reserved{};
};

struct ExternalKernelOutput {
    CausalCut causal_cut{};
    FairValueSnapshot fair{};
    std::array<CandidateAction, kMaxKernelCandidates> candidates{};
    std::size_t candidate_count = 0;
    CandidateAction chosen{};
    std::uint8_t causal_valid = 0;
    std::uint8_t fair_valid = 0;
    std::uint8_t external_only_revaluation = 0;
    std::uint8_t fail_closed = 0;
    std::array<std::uint8_t, 4> reserved{};
};

[[nodiscard]] ExternalKernelOutput run_external_fair_kernel(
    const ExternalKernelInput& input) noexcept;

static_assert(std::is_trivially_copyable_v<RestingQuoteSnapshot>);
static_assert(std::is_trivially_copyable_v<MakerTouchSnapshot>);
static_assert(std::is_trivially_copyable_v<ExternalKernelInput>);
static_assert(std::is_trivially_copyable_v<ExternalKernelOutput>);

} // namespace pm::v7::external_fair
