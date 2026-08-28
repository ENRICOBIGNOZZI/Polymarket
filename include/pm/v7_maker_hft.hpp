#pragma once

#include "pm/v7_intent.hpp"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>

namespace pm::v7::maker {

inline constexpr std::size_t kFeatureCount = 10;
inline constexpr std::size_t kExecutionActionCount = 4;
inline constexpr std::size_t kExecutionOutcomeCount = 2;
inline constexpr std::size_t kExecutionSideCount = 2;
inline constexpr std::size_t kExecutionCellCount =
    kExecutionActionCount * kExecutionOutcomeCount * kExecutionSideCount;

enum class Action : std::uint8_t {
    Join = 1,
    Improve1 = 2,
    Fade1 = 3,
    Fade2 = 4,
    OneSided = 5,
    Withdraw = 6,
};

enum class DecisionReason : std::uint8_t {
    Quote = 1,
    NoEconomicQuote = 2,
    Toxic = 3,
    InventoryLimit = 4,
    GlobalKill = 5,
    StrategyKill = 6,
    MarketKill = 7,
    FeedUnhealthy = 8,
    StaleState = 9,
    InvalidBook = 10,
    InvalidModel = 11,
    QuoteLifetimeHold = 12,
    NoChange = 13,
};

// One MakerHotPath instance is owned by exactly one market/instrument on one
// shard. The canonical feed/book owner supplies this compact mutation view;
// Maker does not own a second order book.
struct MarketUpdate {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::int64_t best_bid_tick = 0;
    std::int64_t best_ask_tick = 0;
    double bid_depth_l1 = 0.0;
    double ask_depth_l1 = 0.0;
    double bid_depth_l5 = 0.0;
    double ask_depth_l5 = 0.0;
    double bid_depth_l10 = 0.0;
    double ask_depth_l10 = 0.0;
    double trade_buy_qty_delta = 0.0;
    double trade_sell_qty_delta = 0.0;
    double cancel_bid_qty_delta = 0.0;
    double cancel_ask_qty_delta = 0.0;
    double conservative_rebate_ev_per_share = 0.0;
    double conservative_reward_ev_per_share = 0.0;
    double related_fair_value = 0.0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t socket_receive_monotonic_ns = 0;
    std::int64_t related_snapshot_age_ns = 0;
    // +1 for the YES/outcome-1 token, -1 for its complementary NO/outcome-0
    // token. Inventory is measured globally as YES-NO, but the hot path must
    // view it in the orientation of the instrument it is quoting.
    std::int8_t instrument_inventory_sign = 1;
    std::uint8_t feed_healthy = 1;
    std::uint8_t related_state_valid = 0;
    std::uint8_t reserved0 = 0;
};

struct InventorySnapshot {
    double yes_shares = 0.0;
    double no_shares = 0.0;
    double reserved_buy_shares = 0.0;
    double reserved_sell_shares = 0.0;
    double pending_split_shares = 0.0;
    double pending_merge_shares = 0.0;

    [[nodiscard]] double complete_sets() const noexcept;
    [[nodiscard]] double residual_shares() const noexcept;
};

struct QuoteSnapshot {
    std::int64_t bid_tick = 0;
    std::int64_t ask_tick = 0;
    std::int64_t last_quote_monotonic_ns = 0;
    std::uint8_t bid_active = 0;
    std::uint8_t ask_active = 0;
    std::uint8_t cancel_pending = 0;
    std::uint8_t reserved = 0;
};

struct RiskSnapshot {
    double max_quote_shares = 1.0;
    double max_abs_residual_shares = 1.0;
    std::int64_t max_local_state_age_ns = 5'000'000'000LL;
    std::uint8_t global_kill = 0;
    std::uint8_t strategy_kill = 0;
    std::uint8_t market_kill = 0;
    std::uint8_t reserved = 0;
};

// A cell is pre-shrunk on the slow path toward the GLOBAL execution model.
// The hot path never estimates or parses anything; it only selects one of the
// fixed 4 actions x 2 outcomes x 2 execution sides.
struct ExecutionCellBaseline {
    double fill_probability = 0.0;
    double adverse_markout_per_share = 0.0;
    double fill_weight = 0.0;
    double markout_weight = 0.0;
    std::uint32_t orders = 0;
    std::uint32_t filled_orders = 0;
    std::uint32_t adverse_markouts = 0;
    std::uint32_t event_clusters = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 7> reserved{};
};

[[nodiscard]] std::size_t execution_cell_index(
    Action action, std::int8_t instrument_inventory_sign, Side side) noexcept;

// Slow-path training may be arbitrarily rich; only this compact immutable
// snapshot is published to the C++ decision loop.
struct MakerModelSnapshot {
    // The constructor may enrich the snapshot from the exact-SHA slow-plane
    // execution-model file before publication. It never throws; unavailable or
    // stale evidence leaves all cells invalid and the kernel uses GLOBAL.
    MakerModelSnapshot() noexcept;

    std::uint64_t model_version = 1;
    std::uint64_t policy_version = 1;
    double tick_size = 0.01;
    double mid_weight = 0.40;
    double microprice_weight = 0.45;
    double flow_weight = 0.10;
    double related_weight = 0.05;
    double inventory_skew_ticks = 1.0;
    double inventory_cost_per_share = 0.001;
    double latency_cost_per_second = 0.0;
    double robust_ev_z = 1.64;
    double base_ev_se_per_share = 0.0005;
    double toxicity_withdraw_threshold = 0.75;
    double one_sided_inventory_fraction = 0.50;
    double min_robust_ev_per_share = 0.0;
    double base_quote_shares = 1.0;
    double feature_decay = 0.90;
    double min_requote_ev_delta = 0.00001;
    std::int64_t min_quote_lifetime_ns = 100'000'000LL;
    std::int64_t max_related_snapshot_age_ns = 1'500'000'000LL;
    std::array<double, kFeatureCount> fill_coefficients{
        -2.8, -0.10, 0.35, 0.40, -0.15, 0.08, -0.05, -0.05, -0.10, -0.05};
    std::array<double, kFeatureCount> markout_coefficients{
        0.0015, 0.0001, -0.0002, -0.00025, 0.0002, 0.00005, 0.00005, 0.00005, 0.0002, 0.00005};
    std::array<ExecutionCellBaseline, kExecutionCellCount> execution_cells{};

    [[nodiscard]] bool valid() const noexcept;
};

class MakerModelStore final {
public:
    explicit MakerModelStore(std::shared_ptr<const MakerModelSnapshot> initial);

    [[nodiscard]] std::shared_ptr<const MakerModelSnapshot> snapshot() const noexcept;
    [[nodiscard]] bool publish(std::shared_ptr<const MakerModelSnapshot> next) noexcept;

private:
    std::atomic<std::shared_ptr<const MakerModelSnapshot>> active_;
};

struct Features {
    double mid = 0.0;
    double microprice = 0.0;
    double spread_ticks = 0.0;
    double imbalance = 0.0;
    double ofi = 0.0;
    double ew_vol_ticks = 0.0;
    double short_return_ticks = 0.0;
    double trade_intensity = 0.0;
    double cancel_intensity = 0.0;
    double inventory_fraction = 0.0;
    double local_latency_ms = 0.0;
};

struct LatencyTrace {
    std::int64_t feature_ns = 0;
    std::int64_t decision_ns = 0;
    std::int64_t risk_ns = 0;
    std::int64_t receive_to_intent_ns = 0;
};

struct MakerDecision {
    Action action = Action::Withdraw;
    DecisionReason reason = DecisionReason::NoEconomicQuote;
    Features features{};
    LatencyTrace latency{};
    double fair_value = 0.0;
    double reservation_value = 0.0;
    double toxicity = 0.0;
    double robust_ev = 0.0;
    double ev_uncertainty = 0.0;
    std::array<StrategyIntent, 4> intents{};
    std::uint8_t intent_count = 0;
    std::uint8_t reserved0 = 0;
    std::uint16_t reserved1 = 0;
};

class MakerHotPath final {
public:
    MakerHotPath() = default;

    // No allocation, filesystem, database, network, logging or Python is
    // permitted in this method. One owner thread calls it for one instrument.
    [[nodiscard]] MakerDecision on_market_update(
        const MarketUpdate& update,
        const InventorySnapshot& inventory,
        const QuoteSnapshot& quotes,
        const RiskSnapshot& risk,
        const MakerModelSnapshot& model) noexcept;

private:
    double previous_mid_ = 0.0;
    double previous_bid_depth_ = 0.0;
    double previous_ask_depth_ = 0.0;
    double ew_var_ticks2_ = 0.0;
    double ew_trade_intensity_ = 0.0;
    double ew_cancel_intensity_ = 0.0;
    std::uint64_t intent_sequence_ = 0;
    std::uint8_t initialized_ = 0;
};

} // namespace pm::v7::maker
