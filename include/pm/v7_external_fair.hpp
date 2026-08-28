#pragma once

#include "pm/v7_intent.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::size_t kVenueCount = 3;
inline constexpr std::size_t kMaxAggressiveLevels = 16;
inline constexpr std::size_t kMaxUnifiedCandidates = 8;

enum class VenueId : std::uint8_t {
    Unknown = 0,
    BinanceSpot = 1,
    CoinbaseSpot = 2,
    BybitSpot = 3,
};

enum class ExternalEventType : std::uint8_t {
    BookTop = 1,
    Trade = 2,
    Health = 3,
};

enum class OracleContinuity : std::uint8_t {
    ContinuityUnknown = 0,
    LiveContinuous = 1,
    RecoveredSameOracleSnapshot = 2,
};

enum class Comparator : std::uint8_t {
    GreaterEqual = 1,
    Greater = 2,
    LessEqual = 3,
    Less = 4,
};

enum class TriggerSource : std::uint8_t {
    PmL2Update = 1,
    PmTrade = 2,
    ExternalPriceUpdate = 3,
    OracleUpdate = 4,
    SettlementReferenceUpdate = 5,
    PrivateOrderUpdate = 6,
    InventoryUpdate = 7,
    RiskUpdate = 8,
    HealthUpdate = 9,
};

enum class Action : std::uint8_t {
    Nothing = 0,
    Make = 1,
    Take = 2,
    Cancel = 3,
    Withdraw = 4,
};

enum class Purpose : std::uint8_t {
    Alpha = 1,
    InventoryReduction = 2,
    Risk = 3,
    Liquidation = 4,
};

enum class CancelReason : std::uint8_t {
    None = 0,
    FairShock = 1,
    OracleInvalid = 2,
    ExternalStateInvalid = 3,
    UncertaintySpike = 4,
    VenueDisagreementLimit = 5,
    RulesHashMismatch = 6,
    FeeChange = 7,
    ModelInvalid = 8,
    Risk = 9,
    Kill = 10,
    FairExpired = 11,
};

// Slow-plane ContractSpec owns human-readable identifiers and full hashes.
// This compact immutable projection is the only contract representation needed
// by the hot decision path.
struct ContractHotSpec {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    std::uint64_t condition_handle = 0;
    std::uint64_t rules_hash_handle = 0;
    std::uint64_t oracle_feed_handle = 0;
    std::uint64_t contract_version = 0;
    std::int64_t start_monotonic_ns = 0;
    std::int64_t end_monotonic_ns = 0;
    std::uint32_t oracle_window_seconds = 0;
    Comparator comparator = Comparator::GreaterEqual;
    std::uint8_t verified_template = 0;
    std::uint8_t rules_hash_recognized = 0;
    std::uint8_t reference_semantics_verified = 0;
    std::uint8_t settlement_source_verified = 0;
};

struct SettlementReferenceSnapshot {
    std::uint64_t market_handle = 0;
    std::uint64_t contract_version = 0;
    std::uint64_t reference_version = 0;
    std::uint64_t exact_decimal_handle = 0;
    std::uint64_t oracle_feed_handle = 0;
    std::uint64_t provenance_hash_handle = 0;
    std::uint64_t source_state_version = 0;
    double reference_value_numeric = 0.0;
    std::int64_t reference_observation_ns = 0;
    std::int64_t reference_receive_monotonic_ns = 0;
    std::uint32_t oracle_window_seconds = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 3> reserved{};
};

struct OracleEvent {
    std::uint64_t feed_handle = 0;
    std::uint64_t source_sequence = 0;
    std::uint64_t connection_epoch = 0;
    std::uint64_t exact_decimal_handle = 0;
    double value_numeric = 0.0;
    std::int64_t oracle_observation_ns = 0;
    std::int64_t publisher_timestamp_ns = 0;
    std::int64_t local_receive_monotonic_ns = 0;
    std::int64_t local_receive_wall_ns = 0;
    std::uint32_t window_seconds = 0;
    std::uint8_t healthy = 0;
    std::uint8_t gap = 0;
    std::uint8_t reconnect = 0;
    std::uint8_t same_oracle_recovery = 0;
};

struct OracleSnapshot {
    std::uint64_t state_version = 0;
    std::uint64_t feed_handle = 0;
    std::uint64_t connection_epoch = 0;
    std::uint64_t exact_decimal_handle = 0;
    double value_numeric = 0.0;
    std::int64_t oracle_observation_ns = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int64_t age_ns = 0;
    std::uint32_t window_seconds = 0;
    OracleContinuity continuity = OracleContinuity::ContinuityUnknown;
    std::uint8_t healthy = 0;
    std::uint8_t valid = 0;
    std::uint8_t gap = 0;
};

struct ExternalVenueEvent {
    std::uint64_t asset_handle = 0;
    std::uint64_t source_sequence = 0;
    std::uint64_t connection_epoch = 0;
    VenueId venue = VenueId::Unknown;
    ExternalEventType event_type = ExternalEventType::BookTop;
    std::uint16_t reserved0 = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t local_receive_monotonic_ns = 0;
    std::int64_t local_receive_wall_ns = 0;
    double bid = 0.0;
    double ask = 0.0;
    double bid_size = 0.0;
    double ask_size = 0.0;
    double trade_price = 0.0;
    double trade_size = 0.0;
    std::int8_t trade_side = 0; // +1 buyer initiated, -1 seller initiated.
    std::uint8_t gap = 0;
    std::uint8_t stale = 0;
    std::uint8_t healthy = 0;
};

struct ExternalAssetSnapshot {
    std::uint64_t asset_handle = 0;
    std::uint64_t state_version = 0;
    std::uint64_t chainlink_feed_handle = 0;
    std::uint64_t chainlink_connection_epoch = 0;
    double chainlink_value_numeric = 0.0;
    std::int64_t chainlink_observed_ns = 0;
    std::int64_t chainlink_receive_ns = 0;
    std::int64_t chainlink_age_ns = 0;
    std::uint32_t chainlink_window_seconds = 0;
    OracleContinuity chainlink_continuity = OracleContinuity::ContinuityUnknown;
    std::uint8_t chainlink_healthy = 0;
    std::uint8_t chainlink_valid = 0;
    std::uint16_t reserved0 = 0;
    double venue_composite_log_price = 0.0;
    double venue_composite_price = 0.0;
    double venue_composite_microprice = 0.0;
    double venue_composite_return_50ms = 0.0;
    double venue_composite_return_100ms = 0.0;
    double venue_composite_return_250ms = 0.0;
    double venue_composite_return_1s = 0.0;
    double venue_composite_return_5s = 0.0;
    double venue_dispersion_bps = 0.0;
    std::uint32_t venue_health_mask = 0;
    std::uint32_t venue_count_fresh = 0;
    double aggregate_ofi = 0.0;
    double aggregate_trade_imbalance = 0.0;
    double realized_vol_fast = 0.0;
    double realized_vol_medium = 0.0;
    double realized_vol_slow = 0.0;
    double fast_slow_vol_ratio = 0.0;
    double jump_score = 0.0;
    std::int64_t latest_input_receive_monotonic_ns = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 7> reserved1{};
};

struct CausalCut {
    std::uint64_t causal_cut_id = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::uint64_t pm_state_version = 0;
    std::uint64_t oracle_state_version = 0;
    std::uint64_t external_state_version = 0;
    std::uint64_t settlement_reference_version = 0;
    std::uint64_t inventory_state_version = 0;
    std::uint64_t private_state_version = 0;
    std::uint64_t risk_state_version = 0;
    std::int64_t max_input_receive_monotonic_ns = 0;
    TriggerSource trigger_source = TriggerSource::PmL2Update;
    std::uint64_t trigger_source_version = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 7> reserved{};
};

struct FairValueModelSnapshot {
    std::uint64_t model_version = 0;
    std::uint64_t model_hash_handle = 0;
    std::uint64_t policy_version = 0;
    std::uint64_t feature_schema_version = 0;
    double bridge_gap_coefficient = 0.70;
    double external_return_250ms_coefficient = 0.0;
    double external_return_1s_coefficient = 0.0;
    double sigma_per_sqrt_second_fraction = 0.00020;
    double bridge_residual_sigma_fraction = 0.00010;
    double sigma_floor_fraction = 0.00005;
    double calibration_intercept = 0.0;
    double calibration_slope = 1.0;
    double micro_ofi_logit_coefficient = 0.0;
    double micro_trade_imbalance_logit_coefficient = 0.0;
    double max_microstructure_logit_adjustment = 0.25;
    double base_logit_uncertainty = 0.15;
    double disagreement_logit_per_10bps = 0.05;
    double jump_logit_penalty = 0.05;
    double drift_logit_penalty = 0.10;
    double uncertainty_z = 1.64;
    double max_external_disagreement_bps = 50.0;
    double max_model_drift_score = 5.0;
    std::int64_t max_oracle_age_ns = 2'000'000'000LL;
    std::int64_t max_external_age_ns = 1'000'000'000LL;
    std::uint32_t min_healthy_venues = 2;
    std::uint8_t mature = 0;
    std::array<std::uint8_t, 3> reserved{};
};

struct FairValueSnapshot {
    std::uint64_t market_handle = 0;
    std::uint64_t contract_version = 0;
    std::uint64_t reference_version = 0;
    std::uint64_t model_version = 0;
    std::uint64_t model_hash_handle = 0;
    std::uint64_t policy_version = 0;
    std::uint64_t feature_schema_version = 0;
    double fair_yes = 0.5;
    double fair_yes_lower = 0.0;
    double fair_yes_upper = 1.0;
    double structural_probability = 0.5;
    double calibrated_probability = 0.5;
    double microstructure_logit_adjustment = 0.0;
    double expected_settlement_margin = 0.0;
    double settlement_margin_sigma = 0.0;
    double efficient_price = 0.0;
    double oracle_price = 0.0;
    double opening_reference_price = 0.0;
    double external_disagreement_bps = 0.0;
    std::int64_t oracle_age_ns = 0;
    std::int64_t external_age_ns = 0;
    double model_drift_score = 0.0;
    std::int64_t calculated_monotonic_ns = 0;
    std::int64_t valid_until_monotonic_ns = 0;
    std::uint8_t contract_verified = 0;
    std::uint8_t reference_verified = 0;
    std::uint8_t model_mature = 0;
    std::uint8_t oracle_healthy = 0;
    std::uint8_t external_healthy = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 2> reserved{};
};

struct FeeScheduleSnapshot {
    std::uint64_t market_handle = 0;
    std::uint64_t fee_version = 0;
    double rate = 0.0;
    double exponent = 1.0;
    double maker_fee_per_share = 0.0;
    std::int64_t retrieved_monotonic_ns = 0;
    std::uint8_t taker_only = 1;
    std::uint8_t authoritative = 0;
    std::uint8_t valid = 0;
    std::uint8_t reserved = 0;
};

struct BookLevel {
    double price = 0.0;
    double quantity = 0.0;
};

struct AggressiveBook {
    std::array<BookLevel, kMaxAggressiveLevels> asks{};
    std::uint8_t ask_count = 0;
    std::array<std::uint8_t, 7> reserved{};
    std::uint64_t pm_state_version = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::uint8_t valid = 0;
};

struct ExternalFairPolicy {
    double maker_cancel_robust_capture_floor = 0.0;
    double taker_minimum_robust_ev_per_share = 0.001;
    double max_uncertainty_width = 0.25;
    double max_depth_fraction = 0.50;
    double depth_survival_fraction = 0.75;
    double base_taker_execution_risk_per_share = 0.0005;
    double latency_risk_per_second = 0.001;
    double fractional_kelly = 0.10;
    double max_market_capital_fraction = 0.02;
    double max_quantity = 100.0;
    std::int64_t max_fair_age_ns = 1'000'000'000LL;
    std::int64_t max_book_age_ns = 1'000'000'000LL;
    std::uint8_t external_fair_required = 1;
    std::uint8_t cancel_overlay_enabled = 1;
    std::uint8_t maker_repricing_enabled = 0;
    std::uint8_t taker_enabled = 0;
};

struct CandidateAction {
    Action action = Action::Nothing;
    Purpose purpose = Purpose::Alpha;
    Side side = Side::None;
    CancelReason cancel_reason = CancelReason::None;
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t causal_cut_id = 0;
    std::int64_t price_tick = 0;
    std::int64_t quantity_microunits = 0;
    double average_price = 0.0;
    double expected_change_in_wealth = 0.0;
    double robust_ev_per_share = 0.0;
    double risk_cost = 0.0;
    double capital_time_cost = 0.0;
    double execution_uncertainty = 0.0;
    std::uint8_t admissible = 0;
    std::uint8_t critical = 0;
    std::array<std::uint8_t, 6> reserved{};
};

[[nodiscard]] bool contract_authorized(const ContractHotSpec& contract) noexcept;
[[nodiscard]] bool causal_cut_valid(const CausalCut& cut) noexcept;
[[nodiscard]] bool fair_snapshot_valid(const FairValueSnapshot& fair,
                                       std::int64_t now_ns) noexcept;
[[nodiscard]] double fee_per_share(double price,
                                   const FeeScheduleSnapshot& fee,
                                   bool taker = true) noexcept;

[[nodiscard]] FairValueSnapshot compute_fair_value(
    const ContractHotSpec& contract,
    const SettlementReferenceSnapshot& reference,
    const OracleSnapshot& oracle,
    const ExternalAssetSnapshot& external,
    const FairValueModelSnapshot& model,
    std::int64_t decision_monotonic_ns,
    std::int64_t time_to_expiry_ns,
    double model_drift_score = 0.0) noexcept;

[[nodiscard]] CandidateAction evaluate_cancel_overlay(
    const ContractHotSpec& contract,
    const FairValueSnapshot& fair,
    const ExternalFairPolicy& policy,
    Side resting_side,
    bool instrument_is_yes,
    std::uint64_t instrument_handle,
    double resting_price,
    std::int64_t price_tick,
    std::uint64_t causal_cut_id,
    std::int64_t now_ns,
    bool global_or_market_kill = false) noexcept;

[[nodiscard]] CandidateAction build_informed_take(
    const ContractHotSpec& contract,
    const FairValueSnapshot& fair,
    const FeeScheduleSnapshot& fee,
    const ExternalFairPolicy& policy,
    const AggressiveBook& book,
    bool instrument_is_yes,
    std::uint64_t instrument_handle,
    std::uint64_t causal_cut_id,
    std::int64_t now_ns,
    double available_capital,
    double current_market_gross_cost,
    double tick_size) noexcept;

[[nodiscard]] CandidateAction choose_unified_action(
    const std::array<CandidateAction, kMaxUnifiedCandidates>& candidates,
    std::size_t candidate_count,
    bool global_or_strategy_or_market_kill) noexcept;

[[nodiscard]] StrategyIntent to_strategy_intent(
    const CandidateAction& action,
    std::uint64_t intent_id,
    std::uint64_t event_handle,
    std::uint64_t state_version,
    std::uint64_t model_version,
    std::uint64_t policy_version,
    std::int64_t decision_monotonic_ns) noexcept;

static_assert(std::is_trivially_copyable_v<SettlementReferenceSnapshot>);
static_assert(std::is_trivially_copyable_v<OracleEvent>);
static_assert(std::is_trivially_copyable_v<OracleSnapshot>);
static_assert(std::is_trivially_copyable_v<ExternalVenueEvent>);
static_assert(std::is_trivially_copyable_v<ExternalAssetSnapshot>);
static_assert(std::is_trivially_copyable_v<CausalCut>);
static_assert(std::is_trivially_copyable_v<FairValueSnapshot>);
static_assert(std::is_trivially_copyable_v<CandidateAction>);

} // namespace pm::v7::external_fair
