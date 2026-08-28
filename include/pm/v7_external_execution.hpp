#pragma once

#include "pm/v7_execution_plan.hpp"
#include "pm/v7_external_fair.hpp"

#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

enum class AggressiveTimeInForce : std::uint8_t {
    Fak = 1,
    Fok = 2,
};

struct OwnOrderView {
    std::uint64_t instrument_handle = 0;
    std::uint64_t private_state_version = 0;
    std::int64_t bid_tick = 0;
    std::int64_t ask_tick = 0;
    std::uint8_t bid_active = 0;
    std::uint8_t ask_active = 0;
    std::uint8_t bid_cancel_pending = 0;
    std::uint8_t ask_cancel_pending = 0;
};

struct SelfCrossGuardResult {
    CandidateAction immediate{};
    CandidateAction deferred_take{};
    std::uint64_t required_private_state_version = 0;
    std::uint8_t take_allowed = 0;
    std::uint8_t requires_cancel_confirmation = 0;
    std::array<std::uint8_t, 6> reserved{};
};

struct TakerPaperFill {
    std::int64_t requested_microunits = 0;
    std::int64_t filled_microunits = 0;
    std::int64_t arrival_monotonic_ns = 0;
    double average_fill_price = 0.0;
    double gross_book_cost = 0.0;
    double authoritative_fee = 0.0;
    double robust_terminal_value = 0.0;
    double robust_net_ev = 0.0;
    double slippage_vs_plan = 0.0;
    std::uint8_t complete = 0;
    std::uint8_t rejected = 0;
    std::uint8_t fee_authoritative = 0;
    std::uint8_t reserved = 0;
};

struct ExternalMakerPolicy {
    double inventory_skew_ticks = 1.0;
    double minimum_robust_capture_per_share = 0.0;
    double quote_quantity = 1.0;
    std::uint8_t enabled = 0;
    std::array<std::uint8_t, 7> reserved{};
};

[[nodiscard]] IntentPurpose to_intent_purpose(Purpose purpose) noexcept;

[[nodiscard]] ExecutionPlan build_execution_plan(
    const CandidateAction& action,
    std::uint64_t intent_id,
    std::uint64_t event_handle,
    std::uint64_t state_version,
    std::uint64_t model_version,
    std::uint64_t policy_version,
    std::int64_t decision_monotonic_ns,
    std::int32_t tick_size_e4) noexcept;

// Aggressive orders never race a conflicting own resting order. A conflict
// emits CANCEL first and keeps the TAKE deferred until a newer canonical
// private-state version confirms the conflicting order is no longer live.
[[nodiscard]] SelfCrossGuardResult guard_self_cross(
    const CandidateAction& take,
    const OwnOrderView& own,
    std::uint64_t causal_cut_id) noexcept;

// PAPER aggressive execution is evaluated against the canonical L2 snapshot at
// simulated arrival time. FAK may partially fill; FOK fills only if the entire
// requested size is executable at or better than the plan's limit.
[[nodiscard]] TakerPaperFill simulate_taker_paper(
    const ExecutionPlan& plan,
    const AggressiveBook& arrival_book,
    const FeeScheduleSnapshot& fee,
    const FairValueSnapshot& fair,
    bool instrument_is_yes,
    AggressiveTimeInForce tif,
    std::int64_t arrival_monotonic_ns) noexcept;

// Shadow/PAPER external-fair maker challenger. PM book state is used only for
// post-only placement. Directional fair comes directly from FairValueSnapshot.
[[nodiscard]] CandidateAction build_external_make(
    const ContractHotSpec& contract,
    const FairValueSnapshot& fair,
    const ExternalMakerPolicy& maker_policy,
    Side side,
    bool instrument_is_yes,
    std::uint64_t instrument_handle,
    std::uint64_t causal_cut_id,
    std::int64_t best_bid_tick,
    std::int64_t best_ask_tick,
    double tick_size,
    double signed_inventory_fraction,
    std::int64_t now_ns) noexcept;

static_assert(std::is_trivially_copyable_v<OwnOrderView>);
static_assert(std::is_trivially_copyable_v<SelfCrossGuardResult>);
static_assert(std::is_trivially_copyable_v<TakerPaperFill>);
static_assert(std::is_trivially_copyable_v<ExternalMakerPolicy>);

} // namespace pm::v7::external_fair
