#include "pm/v7_maker_hft.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace pm::v7::maker {
namespace {

using SteadyClock = std::chrono::steady_clock;
constexpr double kEps = 1e-12;
// ShardRuntime's canonical normal maker risk cap is one percent of the sleeve
// before price conversion. The policy exploration quote fraction is expressed
// against the same sleeve, so this ratio turns the already capital-bounded
// max_quote_shares into a strictly smaller exploration share cap.
constexpr double kNormalQuoteCapitalFraction = 0.01;

[[nodiscard]] std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               SteadyClock::now().time_since_epoch())
        .count();
}

[[nodiscard]] double clamp(double value, double lo, double hi) noexcept {
    return std::max(lo, std::min(hi, value));
}

[[nodiscard]] bool finite(double value) noexcept {
    return std::isfinite(value);
}

[[nodiscard]] std::uint64_t mix64(std::uint64_t x) noexcept {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27U)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31U);
}

[[nodiscard]] bool exploration_sample(const MarketUpdate& update, double epsilon) noexcept {
    if (epsilon >= 1.0) return true;
    if (!(epsilon > 0.0)) return false;
    constexpr std::uint64_t kBuckets = 1'000'000ULL;
    const auto threshold = static_cast<std::uint64_t>(
        std::floor(clamp(epsilon, 0.0, 1.0) * static_cast<double>(kBuckets)));
    const std::uint64_t seed = update.state_version
        ^ (update.instrument_handle * 0x9e3779b97f4a7c15ULL)
        ^ (update.market_handle * 0xbf58476d1ce4e5b9ULL);
    return mix64(seed) % kBuckets < threshold;
}

[[nodiscard]] bool persistent_lifetime_sample(
    const MarketUpdate& update, std::uint64_t quote_sequence, double fraction) noexcept {
    if (fraction >= 1.0) return true;
    if (!(fraction > 0.0)) return false;
    constexpr std::uint64_t kBuckets = 1'000'000ULL;
    const auto threshold = static_cast<std::uint64_t>(
        std::floor(clamp(fraction, 0.0, 1.0) * static_cast<double>(kBuckets)));
    const std::uint64_t seed = update.state_version
        ^ (update.instrument_handle * 0x94d049bb133111ebULL)
        ^ (update.market_handle * 0xbf58476d1ce4e5b9ULL)
        ^ (quote_sequence * 0x9e3779b97f4a7c15ULL);
    return mix64(seed) % kBuckets < threshold;
}

[[nodiscard]] double logistic(double value) noexcept {
    if (value >= 0.0) {
        const double e = std::exp(-value);
        return 1.0 / (1.0 + e);
    }
    const double e = std::exp(value);
    return e / (1.0 + e);
}

[[nodiscard]] double safe_logit(double probability) noexcept {
    const double p = clamp(probability, 1e-6, 1.0 - 1e-6);
    return std::log(p / (1.0 - p));
}

[[nodiscard]] double dot(const std::array<double, kFeatureCount>& a,
                         const std::array<double, kFeatureCount>& b) noexcept {
    double out = 0.0;
    for (std::size_t i = 0; i < kFeatureCount; ++i) out += a[i] * b[i];
    return out;
}

struct SideEconomics {
    bool admissible = false;
    Side side = Side::None;
    std::int64_t price_tick = 0;
    double quote_shares = 0.0;
    double fill_probability = 0.0;
    double flow_reach_probability = 0.0;
    double queue_depletion_probability = 0.0;
    double opposite_flow_shares_per_second = 0.0;
    double opposite_flow_prints_per_second = 0.0;
    double gross_capture = 0.0;
    double expected_cost = 0.0;
    double expected_risk = 0.0;
    double uncertainty = 0.0;
    double point_ev = -std::numeric_limits<double>::infinity();
    double robust_ev = -std::numeric_limits<double>::infinity();
};

[[nodiscard]] double exploration_adjusted_ev(
    const SideEconomics& economics,
    const MakerModelSnapshot& model) noexcept {
    return economics.point_ev
        - std::max(0.0, model.exploration_confidence_z) * economics.uncertainty;
}

struct Candidate {
    Action action = Action::Withdraw;
    SideEconomics bid{};
    SideEconomics ask{};
    double score = -std::numeric_limits<double>::infinity();
};

struct ExplorationChoice {
    const SideEconomics* economics = nullptr;
    Action action = Action::Join;
    double adjusted_ev = -std::numeric_limits<double>::infinity();
};

[[nodiscard]] ExplorationChoice choose_exploration(
    const Candidate* candidates,
    std::size_t candidate_count,
    const MakerModelSnapshot& model,
    Side preferred_side) noexcept {
    ExplorationChoice best;
    double best_fill_probability = -1.0;
    // Give the deterministically sampled side the first opportunity to collect
    // side-specific evidence. Fall back to the other side only when the
    // preferred side has no positive adjusted-EV placement. This prevents a
    // symmetric cold start from becoming permanently BUY-only.
    const std::array<Side, 2> side_order = preferred_side == Side::Sell
        ? std::array<Side, 2>{Side::Sell, Side::Buy}
        : std::array<Side, 2>{Side::Buy, Side::Sell};
    for (const auto wanted_side : side_order) {
        best = {};
        best_fill_probability = -1.0;
        for (std::size_t i = 0; i < candidate_count; ++i) {
            const auto& candidate = candidates[i];
            // JOIN and IMPROVE1 increase information rate without crossing.
            if (candidate.action != Action::Join && candidate.action != Action::Improve1) continue;
            const auto& economics = wanted_side == Side::Sell ? candidate.ask : candidate.bid;
            const double adjusted_ev = exploration_adjusted_ev(economics, model);
            // Exploration has its own tiny PAPER capital envelope and exists
            // to learn below the exploit confidence floor.  Requiring the
            // exploit floor here recreates the cold-start deadlock: a strictly
            // positive point-EV cell can never rest long enough to label its
            // reach, queue depletion and markout.  Zero remains fail-closed.
            if (!finite(adjusted_ev) || adjusted_ev <= 0.0
                || economics.price_tick <= 0 || economics.side != wanted_side) continue;
            // Exploration still spends real PAPER risk. Rank by conservative
            // expected dollars first; use fill probability only as a tie-break
            // for equally economic placements. Maximizing fill probability
            // selected IMPROVE1 precisely where informed flow was most toxic.
            if (adjusted_ev > best.adjusted_ev + kEps
                || (std::abs(adjusted_ev - best.adjusted_ev) <= kEps
                    && economics.fill_probability > best_fill_probability)) {
                best.economics = &economics;
                best.action = candidate.action;
                best.adjusted_ev = adjusted_ev;
                best_fill_probability = economics.fill_probability;
            }
        }
        if (best.economics != nullptr) return best;
    }
    return best;
}

[[nodiscard]] const SideEconomics* exploration_side(
    const Candidate* candidates,
    std::size_t candidate_count,
    Action action,
    Side side) noexcept {
    for (std::size_t i = 0; i < candidate_count; ++i) {
        if (candidates[i].action != action) continue;
        return side == Side::Sell ? &candidates[i].ask : &candidates[i].bid;
    }
    return nullptr;
}

[[nodiscard]] const ExecutionCellBaseline* execution_cell(
    const MakerModelSnapshot& model,
    Action action,
    const MarketUpdate& update,
    Side side) noexcept {
    const std::size_t index = execution_cell_index(action, update.instrument_inventory_sign, side);
    if (index >= model.execution_cells.size()) return nullptr;
    const auto& cell = model.execution_cells[index];
    return cell.valid != 0 ? &cell : nullptr;
}

[[nodiscard]] std::array<double, kFeatureCount> model_features(
    const Features& features, Side side, double queue_ahead,
    double quote_shares, double distance_ticks) noexcept {
    const double direction = side == Side::Buy ? 1.0 : -1.0;
    const double opposite_print_rate = side == Side::Buy
        ? features.aggressive_sell_prints_per_second
        : features.aggressive_buy_prints_per_second;
    return {
        1.0,
        features.spread_ticks,
        direction * features.imbalance,
        direction * features.ofi,
        features.ew_vol_ticks,
        features.trade_intensity,
        features.cancel_intensity,
        direction * features.short_return_ticks,
        direction * features.inventory_fraction,
        features.local_latency_ms,
        std::max(0.0, opposite_print_rate),
        std::log1p(std::max(0.0, queue_ahead) / std::max(kEps, quote_shares)),
        distance_ticks,
    };
}

[[nodiscard]] SideEconomics evaluate_side(
    Action action,
    Side side,
    std::int64_t quote_tick,
    const MarketUpdate& update,
    const InventorySnapshot& inventory,
    const RiskSnapshot& risk,
    const MakerModelSnapshot& model,
    const Features& features,
    double fair_value,
    double quote_shares) noexcept {

    SideEconomics out;
    out.side = side;
    out.price_tick = quote_tick;
    out.quote_shares = quote_shares;

    if (quote_shares <= 0.0 || quote_tick <= 0) return out;
    if (side == Side::Buy && quote_tick >= update.best_ask_tick) return out;
    if (side == Side::Sell && quote_tick <= update.best_bid_tick) return out;

    const double price = static_cast<double>(quote_tick) * model.tick_size;
    if (!(price > 0.0 && price < 1.0)) return out;

    const double touch_depth = side == Side::Buy ? update.bid_depth_l1 : update.ask_depth_l1;
    const double distance_ticks = side == Side::Buy
        ? static_cast<double>(update.best_bid_tick - quote_tick)
        : static_cast<double>(quote_tick - update.best_ask_tick);
    double queue_ahead = 0.0;
    if (distance_ticks >= -kEps && distance_ticks <= kEps) {
        queue_ahead = std::max(0.0, touch_depth);
    } else if (distance_ticks > 0.0) {
        queue_ahead = side == Side::Buy
            ? std::max(0.0, update.bid_depth_l10)
            : std::max(0.0, update.ask_depth_l10);
    }
    const auto x = model_features(
        features, side, queue_ahead, quote_shares, distance_ticks);
    const auto* cell = execution_cell(model, action, update, side);

    const double global_fill_logit = model.fill_coefficients[0];
    const double baseline_fill_logit = cell != nullptr
        ? safe_logit(cell->fill_probability) : global_fill_logit;
    const double feature_fill_logit = dot(model.fill_coefficients, x) - global_fill_logit;
    const double queue_penalty = 0.10 * std::log1p(std::max(0.0, touch_depth) /
                                                   std::max(kEps, quote_shares));
    const double fill_logit = baseline_fill_logit + feature_fill_logit
                            - 0.55 * distance_ticks - queue_penalty;
    const double statistical_fill_probability = clamp(logistic(fill_logit), 0.0, 1.0);

    // A passive order cannot fill unless opposite aggressive flow reaches its
    // price and then consumes the queue ahead.  Keep these as distinct causal
    // stages so thousands of no-fills remain informative before markout data
    // is mature.  IMPROVE1 has no visible queue ahead; JOIN uses L1; faded
    // placements conservatively use the visible L10 side as a lower bound.
    const double opposite_flow_rate = side == Side::Buy
        ? features.aggressive_sell_shares_per_second
        : features.aggressive_buy_shares_per_second;
    const double opposite_print_rate = side == Side::Buy
        ? features.aggressive_sell_prints_per_second
        : features.aggressive_buy_prints_per_second;
    const double horizon_seconds = std::max(0.001, model.fill_prediction_horizon_seconds);
    const double expected_opposite_shares = std::max(0.0, opposite_flow_rate) * horizon_seconds;
    const double expected_opposite_prints =
        std::max(0.0, opposite_print_rate) * horizon_seconds;
    // Arrival and size are separate causal stages. A single historical whale
    // print may imply large conditional depletion if another print arrives,
    // but it must not imply that a second print is almost certain.
    const double modeled_flow_reach_probability = clamp(
        1.0 - std::exp(-expected_opposite_prints), 0.0, 1.0);
    const double conditional_opposite_shares = modeled_flow_reach_probability > kEps
        ? expected_opposite_shares / modeled_flow_reach_probability : 0.0;
    const double modeled_queue_depletion_probability = queue_ahead <= kEps
        ? 1.0
        : clamp(conditional_opposite_shares /
                std::max(kEps, queue_ahead + quote_shares), 0.0, 1.0);
    const double flow_reach_probability = features.flow_evidence_valid != 0
        ? modeled_flow_reach_probability : 1.0;
    const double queue_depletion_probability = features.flow_evidence_valid != 0
        ? modeled_queue_depletion_probability : 1.0;
    const double executable_fill_ceiling = flow_reach_probability
        * queue_depletion_probability;
    const double fill_probability = clamp(
        std::min(statistical_fill_probability, executable_fill_ceiling), 0.0, 1.0);

    const double global_markout = model.markout_coefficients[0];
    const double baseline_markout = cell != nullptr
        ? cell->adverse_markout_per_share : global_markout;
    double adverse_markout = std::max(
        0.0, baseline_markout + dot(model.markout_coefficients, x) - global_markout);
    if (distance_ticks < 0.0) adverse_markout += (-distance_ticks) * 0.0001;

    const double capture = side == Side::Buy ? fair_value - price : price - fair_value;
    const double residual = inventory.residual_shares();
    const double signed_delta = side == Side::Buy ? quote_shares : -quote_shares;
    const double next_inventory_fraction = clamp(
        (residual + signed_delta) / std::max(kEps, risk.max_abs_residual_shares), -2.0, 2.0);
    const double inventory_cost = model.inventory_cost_per_share * std::abs(next_inventory_fraction);
    const double latency_cost = model.latency_cost_per_second * std::max(0.0, features.local_latency_ms) / 1000.0;
    const double expected_cost = adverse_markout + inventory_cost + latency_cost;

    const double rebate_ev = fill_probability * std::max(0.0, update.conservative_rebate_ev_per_share);
    const double reward_ev = std::max(0.0, update.conservative_reward_ev_per_share);
    const double expected_ev = fill_probability * (capture - expected_cost) + rebate_ev + reward_ev;
    const double evidence_confidence = cell != nullptr
        ? clamp(std::min(cell->fill_weight, cell->markout_weight), 0.0, 1.0) : 1.0;
    const double evidence_uncertainty_multiplier = cell != nullptr
        ? 1.0 + 0.50 * (1.0 - evidence_confidence) : 1.0;
    const double uncertainty = std::max(0.0, model.base_ev_se_per_share) *
        (1.0 + std::abs(features.ofi) + 0.25 * features.ew_vol_ticks
             + 0.25 * std::abs(features.inventory_fraction))
        * evidence_uncertainty_multiplier;
    const double robust_ev = expected_ev - std::max(0.0, model.robust_ev_z) * uncertainty;

    out.admissible = finite(robust_ev) && robust_ev > model.min_robust_ev_per_share;
    out.fill_probability = fill_probability;
    out.flow_reach_probability = flow_reach_probability;
    out.queue_depletion_probability = queue_depletion_probability;
    out.opposite_flow_shares_per_second = std::max(0.0, opposite_flow_rate);
    out.opposite_flow_prints_per_second = std::max(0.0, opposite_print_rate);
    out.gross_capture = capture;
    out.expected_cost = expected_cost;
    out.expected_risk = adverse_markout + inventory_cost;
    out.uncertainty = uncertainty;
    out.point_ev = expected_ev;
    out.robust_ev = robust_ev;
    return out;
}

[[nodiscard]] Candidate evaluate_candidate(
    Action action,
    std::int64_t bid_tick,
    std::int64_t ask_tick,
    const MarketUpdate& update,
    const InventorySnapshot& inventory,
    const RiskSnapshot& risk,
    const MakerModelSnapshot& model,
    const Features& features,
    double bid_fair_value,
    double ask_fair_value,
    double bid_quote_shares,
    double ask_quote_shares) noexcept {

    Candidate candidate;
    candidate.action = action;
    candidate.bid = evaluate_side(action, Side::Buy, bid_tick, update, inventory, risk, model,
                                  features, bid_fair_value, bid_quote_shares);
    candidate.ask = evaluate_side(action, Side::Sell, ask_tick, update, inventory, risk, model,
                                  features, ask_fair_value, ask_quote_shares);
    candidate.score = 0.0;
    if (candidate.bid.admissible) candidate.score += candidate.bid.robust_ev;
    if (candidate.ask.admissible) candidate.score += candidate.ask.robust_ev;
    if (!candidate.bid.admissible && !candidate.ask.admissible) {
        candidate.score = -std::numeric_limits<double>::infinity();
    }
    return candidate;
}

[[nodiscard]] StrategyIntent make_intent(
    std::uint64_t sequence,
    const MarketUpdate& update,
    const MakerModelSnapshot& model,
    IntentType type,
    Side side,
    std::int64_t price_tick,
    double quote_shares,
    const SideEconomics* economics,
    std::int64_t decision_ns) noexcept {

    StrategyIntent intent;
    intent.intent_id = update.instrument_handle * 0x9e3779b97f4a7c15ULL ^ sequence;
    intent.market_handle = update.market_handle;
    intent.event_handle = update.event_handle;
    intent.instrument_handle = update.instrument_handle;
    intent.state_version = update.state_version;
    intent.model_version = model.model_version;
    intent.policy_version = model.policy_version;
    intent.decision_monotonic_ns = decision_ns;
    intent.exchange_event_ns = update.exchange_event_ns;
    intent.price_tick = price_tick;
    intent.quantity_microunits = type == IntentType::Quote
        ? static_cast<std::int64_t>(std::llround(std::max(0.0, quote_shares) * 1'000'000.0))
        : 0;
    intent.horizon_ms = 0;
    intent.strategy_id = StrategyId::ProfessionalMaker;
    intent.type = type;
    intent.side = side;
    intent.urgency = is_critical_intent(type) ? Urgency::Critical : Urgency::Passive;
    intent.passive = 1;
    intent.post_only = 1;
    if (economics != nullptr) {
        intent.expected_edge = economics->gross_capture;
        intent.expected_cost = economics->expected_cost;
        intent.expected_risk = economics->expected_risk;
        intent.expected_ev = economics->robust_ev;
        intent.ev_uncertainty = economics->uncertainty;
    }
    return intent;
}

void append_intent(MakerDecision& decision, const StrategyIntent& intent) noexcept {
    if (decision.intent_count >= decision.intents.size()) return;
    decision.intents[decision.intent_count++] = intent;
}

} // namespace

double InventorySnapshot::complete_sets() const noexcept {
    return std::max(0.0, std::min(yes_shares, no_shares));
}

double InventorySnapshot::residual_shares() const noexcept {
    return (yes_shares - no_shares) + reserved_buy_shares - reserved_sell_shares;
}

bool MakerModelSnapshot::valid() const noexcept {
    if (!finite(tick_size) || tick_size <= 0.0 || tick_size >= 1.0) return false;
    if (!finite(mid_weight) || !finite(microprice_weight) || !finite(flow_weight) || !finite(related_weight)) return false;
    if (mid_weight < 0.0 || microprice_weight < 0.0 || flow_weight < 0.0 || related_weight < 0.0) return false;
    if (mid_weight + microprice_weight + flow_weight + related_weight <= kEps) return false;
    if (!finite(feature_decay) || feature_decay < 0.0 || feature_decay >= 1.0) return false;
    if (!finite(base_quote_shares) || base_quote_shares <= 0.0) return false;
    if (!finite(fill_prediction_horizon_seconds)
        || fill_prediction_horizon_seconds <= 0.0
        || fill_prediction_horizon_seconds > 300.0) return false;
    if (!finite(toxicity_withdraw_threshold) || toxicity_withdraw_threshold < 0.0 || toxicity_withdraw_threshold > 1.0) return false;
    if (!finite(one_sided_inventory_fraction) || one_sided_inventory_fraction < 0.0) return false;
    if (!finite(exploration_epsilon) || exploration_epsilon < 0.0 || exploration_epsilon > 1.0) return false;
    if (!finite(exploration_confidence_z) || exploration_confidence_z < 0.0
        || exploration_confidence_z > std::max(0.0, robust_ev_z)) return false;
    if (!finite(exploration_quote_notional_fraction) || exploration_quote_notional_fraction < 0.0
        || exploration_quote_notional_fraction > kNormalQuoteCapitalFraction) return false;
    if (!finite(exploration_persistent_fraction) || exploration_persistent_fraction < 0.0
        || exploration_persistent_fraction > 1.0) return false;
    if (exploration_enabled != 0
        && (exploration_epsilon <= 0.0 || exploration_quote_notional_fraction <= 0.0
            || exploration_max_active_markets == 0
            || exploration_concurrent_market_cap == 0
            || exploration_concurrent_market_cap > exploration_max_active_markets)) return false;
    if (exploration_min_rest_ns < 0 || exploration_max_rest_ns < exploration_min_rest_ns) return false;
    if (exploration_persistent_max_rest_ns < exploration_max_rest_ns) return false;
    if (min_quote_lifetime_ns < 0 || max_related_snapshot_age_ns < 0) return false;
    for (double value : fill_coefficients) if (!finite(value)) return false;
    for (double value : markout_coefficients) if (!finite(value)) return false;
    for (const auto& cell : execution_cells) {
        if (cell.valid == 0) continue;
        if (!finite(cell.fill_probability) || cell.fill_probability <= 0.0
            || cell.fill_probability >= 1.0) return false;
        if (!finite(cell.adverse_markout_per_share) || cell.adverse_markout_per_share < 0.0) return false;
        if (!finite(cell.fill_weight) || cell.fill_weight < 0.0 || cell.fill_weight > 1.0) return false;
        if (!finite(cell.markout_weight) || cell.markout_weight < 0.0 || cell.markout_weight > 1.0) return false;
        if (cell.filled_orders > cell.orders) return false;
    }
    return true;
}

MakerModelStore::MakerModelStore(std::shared_ptr<const MakerModelSnapshot> initial)
    : active_(std::move(initial)) {
    const auto current = active_.load(std::memory_order_relaxed);
    if (!current || !current->valid()) throw std::invalid_argument("invalid MakerModelSnapshot");
}

std::shared_ptr<const MakerModelSnapshot> MakerModelStore::snapshot() const noexcept {
    return active_.load(std::memory_order_acquire);
}

bool MakerModelStore::publish(std::shared_ptr<const MakerModelSnapshot> next) noexcept {
    if (!next || !next->valid()) return false;
    active_.store(std::move(next), std::memory_order_release);
    return true;
}

MakerDecision MakerHotPath::on_market_update(
    const MarketUpdate& update,
    const InventorySnapshot& inventory,
    const QuoteSnapshot& quotes,
    const RiskSnapshot& risk,
    const MakerModelSnapshot& model) noexcept {

    MakerDecision decision;
    const std::int64_t start_ns = monotonic_ns();

    const double decay_per_second = model.valid() ? model.feature_decay : 0.90;
    const double tick_size = model.valid() ? model.tick_size : 0.01;
    const double bid_tick = static_cast<double>(update.best_bid_tick);
    const double ask_tick = static_cast<double>(update.best_ask_tick);
    const double mid_tick = 0.5 * (bid_tick + ask_tick);
    const double mid = mid_tick * tick_size;
    const double l5_total = std::max(0.0, update.bid_depth_l5) + std::max(0.0, update.ask_depth_l5);
    const double imbalance = l5_total > kEps
        ? (update.bid_depth_l5 - update.ask_depth_l5) / l5_total : 0.0;
    const double micro_tick = l5_total > kEps
        ? (ask_tick * std::max(0.0, update.bid_depth_l5)
           + bid_tick * std::max(0.0, update.ask_depth_l5)) / l5_total
        : mid_tick;

    double ofi = 0.0;
    double short_return_ticks = 0.0;
    if (initialized_) {
        const double delta_bid = update.bid_depth_l5 - previous_bid_depth_;
        const double delta_ask = update.ask_depth_l5 - previous_ask_depth_;
        ofi = (delta_bid - delta_ask) / (std::abs(delta_bid) + std::abs(delta_ask) + kEps);
        short_return_ticks = (mid - previous_mid_) / std::max(kEps, tick_size);
    }
    const std::int64_t feature_clock_ns = update.socket_receive_monotonic_ns > 0
        ? update.socket_receive_monotonic_ns : start_ns;
    const double elapsed_seconds = previous_feature_monotonic_ns_ > 0
        ? clamp(static_cast<double>(std::max<std::int64_t>(
                    0, feature_clock_ns - previous_feature_monotonic_ns_)) / 1'000'000'000.0,
                0.0, 60.0)
        : 0.0;
    const double time_decay = std::pow(decay_per_second, elapsed_seconds);
    ew_var_ticks2_ = time_decay * ew_var_ticks2_
                   + (1.0 - time_decay) * short_return_ticks * short_return_ticks;
    const double visible_depth = std::max(kEps, l5_total);
    // Treat trades as impulses into a continuous-time leaky arrival-rate
    // estimator.  The old event-count EMA applied another 0.9 multiplier on
    // every unrelated book message, so a busy feed erased real flow in
    // milliseconds.  feature_decay now has stable per-second semantics.
    const double tau_seconds = -1.0 / std::log(std::max(1e-6, decay_per_second));
    if (!flow_evidence_valid_ && update.flow_prior_valid != 0) {
        ew_aggressive_buy_shares_per_second_ = std::max(
            0.0, update.prior_aggressive_buy_shares_per_second);
        ew_aggressive_sell_shares_per_second_ = std::max(
            0.0, update.prior_aggressive_sell_shares_per_second);
        ew_aggressive_buy_prints_per_second_ = std::max(
            0.0, update.prior_aggressive_buy_prints_per_second);
        ew_aggressive_sell_prints_per_second_ = std::max(
            0.0, update.prior_aggressive_sell_prints_per_second);
        flow_evidence_valid_ = 1;
    } else {
        ew_aggressive_buy_shares_per_second_ *= time_decay;
        ew_aggressive_sell_shares_per_second_ *= time_decay;
        ew_aggressive_buy_prints_per_second_ *= time_decay;
        ew_aggressive_sell_prints_per_second_ *= time_decay;
    }
    const double buy_trade_delta = std::max(0.0, update.trade_buy_qty_delta);
    const double sell_trade_delta = std::max(0.0, update.trade_sell_qty_delta);
    ew_aggressive_buy_shares_per_second_ += buy_trade_delta / tau_seconds;
    ew_aggressive_sell_shares_per_second_ += sell_trade_delta / tau_seconds;
    ew_aggressive_buy_prints_per_second_ +=
        buy_trade_delta > 0.0 ? 1.0 / tau_seconds : 0.0;
    ew_aggressive_sell_prints_per_second_ +=
        sell_trade_delta > 0.0 ? 1.0 / tau_seconds : 0.0;
    if (buy_trade_delta > 0.0 || sell_trade_delta > 0.0) flow_evidence_valid_ = 1;
    ew_trade_intensity_ = (ew_aggressive_buy_shares_per_second_
                         + ew_aggressive_sell_shares_per_second_) / visible_depth;

    const double cancel_delta = std::max(0.0, update.cancel_bid_qty_delta)
                              + std::max(0.0, update.cancel_ask_qty_delta);
    ew_cancel_intensity_ = time_decay * ew_cancel_intensity_
                         + cancel_delta / (tau_seconds * visible_depth);

    previous_mid_ = mid;
    previous_bid_depth_ = update.bid_depth_l5;
    previous_ask_depth_ = update.ask_depth_l5;
    previous_feature_monotonic_ns_ = feature_clock_ns;
    initialized_ = 1;

    const std::int64_t feature_end_ns = monotonic_ns();
    const std::int64_t local_age_ns = update.socket_receive_monotonic_ns > 0
        ? std::max<std::int64_t>(0, feature_end_ns - update.socket_receive_monotonic_ns) : 0;
    decision.features.mid = mid;
    decision.features.microprice = micro_tick * tick_size;
    decision.features.spread_ticks = std::max(0.0, ask_tick - bid_tick);
    decision.features.imbalance = clamp(imbalance, -1.0, 1.0);
    decision.features.ofi = clamp(ofi, -1.0, 1.0);
    decision.features.ew_vol_ticks = std::sqrt(std::max(0.0, ew_var_ticks2_));
    decision.features.short_return_ticks = short_return_ticks;
    decision.features.trade_intensity = std::max(0.0, ew_trade_intensity_);
    decision.features.aggressive_buy_shares_per_second =
        std::max(0.0, ew_aggressive_buy_shares_per_second_);
    decision.features.aggressive_sell_shares_per_second =
        std::max(0.0, ew_aggressive_sell_shares_per_second_);
    decision.features.aggressive_buy_prints_per_second =
        std::max(0.0, ew_aggressive_buy_prints_per_second_);
    decision.features.aggressive_sell_prints_per_second =
        std::max(0.0, ew_aggressive_sell_prints_per_second_);
    decision.features.cancel_intensity = std::max(0.0, ew_cancel_intensity_);
    decision.features.inventory_fraction = clamp(
        inventory.residual_shares() / std::max(kEps, risk.max_abs_residual_shares), -2.0, 2.0);
    decision.features.local_latency_ms = static_cast<double>(local_age_ns) / 1'000'000.0;
    decision.features.flow_evidence_valid = flow_evidence_valid_;
    decision.latency.feature_ns = feature_end_ns - start_ns;

    const std::int64_t risk_start_ns = monotonic_ns();
    auto control_exit = [&](DecisionReason reason, IntentType type) noexcept {
        decision.action = Action::Withdraw;
        decision.reason = reason;
        const std::int64_t ts = monotonic_ns();
        append_intent(decision, make_intent(++intent_sequence_, update, model, type, Side::None,
                                            0, 0.0, nullptr, ts));
        decision.latency.risk_ns += monotonic_ns() - risk_start_ns;
        const std::int64_t end_ns = monotonic_ns();
        decision.latency.receive_to_intent_ns = update.socket_receive_monotonic_ns > 0
            ? std::max<std::int64_t>(0, end_ns - update.socket_receive_monotonic_ns) : 0;
        return decision;
    };

    if (risk.global_kill) return control_exit(DecisionReason::GlobalKill, IntentType::Kill);
    if (risk.strategy_kill) return control_exit(DecisionReason::StrategyKill, IntentType::Kill);
    if (risk.market_kill) return control_exit(DecisionReason::MarketKill, IntentType::Kill);
    if (risk.new_risk_frozen) {
        return control_exit(DecisionReason::NewRiskFrozen, IntentType::Withdraw);
    }
    if (!update.feed_healthy) return control_exit(DecisionReason::FeedUnhealthy, IntentType::Withdraw);
    if (risk.max_local_state_age_ns > 0 && local_age_ns > risk.max_local_state_age_ns) {
        return control_exit(DecisionReason::StaleState, IntentType::Withdraw);
    }
    if (!model.valid()) return control_exit(DecisionReason::InvalidModel, IntentType::Withdraw);
    if (update.best_bid_tick <= 0 || update.best_ask_tick <= update.best_bid_tick ||
        static_cast<double>(update.best_ask_tick) * model.tick_size >= 1.0 + model.tick_size * 0.5) {
        return control_exit(DecisionReason::InvalidBook, IntentType::Withdraw);
    }
    decision.latency.risk_ns += monotonic_ns() - risk_start_ns;

    const std::int64_t decision_start_ns = monotonic_ns();
    const double flow_value = mid + decision.features.ofi * model.tick_size;
    const bool related_ok = update.related_state_valid != 0
        && finite(update.related_fair_value)
        && update.related_fair_value > 0.0 && update.related_fair_value < 1.0
        && finite(update.related_fair_lower) && finite(update.related_fair_upper)
        && update.related_fair_lower > 0.0
        && update.related_fair_lower <= update.related_fair_value
        && update.related_fair_value <= update.related_fair_upper
        && update.related_fair_upper < 1.0
        && update.related_snapshot_age_ns >= 0
        && update.related_snapshot_age_ns <= model.max_related_snapshot_age_ns;
    double total_weight = model.mid_weight + model.microprice_weight + model.flow_weight;
    double weighted_value = model.mid_weight * mid
                          + model.microprice_weight * decision.features.microprice
                          + model.flow_weight * flow_value;
    if (related_ok && model.related_weight > 0.0) {
        total_weight += model.related_weight;
        weighted_value += model.related_weight * update.related_fair_value;
    }
    const double fair_value = clamp(weighted_value / std::max(kEps, total_weight),
                                    model.tick_size, 1.0 - model.tick_size);
    const double reservation_value = clamp(
        fair_value - model.inventory_skew_ticks * model.tick_size * decision.features.inventory_fraction,
        model.tick_size, 1.0 - model.tick_size);
    const double inventory_skew =
        model.inventory_skew_ticks * model.tick_size * decision.features.inventory_fraction;
    const double robust_bid_fair = related_ok && model.related_weight > 0.0
        ? clamp((weighted_value
                 + model.related_weight * (update.related_fair_lower
                                            - update.related_fair_value))
                    / std::max(kEps, total_weight) - inventory_skew,
                model.tick_size, 1.0 - model.tick_size)
        : reservation_value;
    const double robust_ask_fair = related_ok && model.related_weight > 0.0
        ? clamp((weighted_value
                 + model.related_weight * (update.related_fair_upper
                                            - update.related_fair_value))
                    / std::max(kEps, total_weight) - inventory_skew,
                model.tick_size, 1.0 - model.tick_size)
        : reservation_value;
    decision.fair_value = fair_value;
    decision.reservation_value = reservation_value;
    decision.fair_lower = robust_bid_fair;
    decision.fair_upper = robust_ask_fair;
    decision.external_fair_authority = static_cast<std::uint8_t>(related_ok);

    const double ret_component = std::abs(decision.features.short_return_ticks) /
                                 (1.0 + std::abs(decision.features.short_return_ticks));
    const double vol_component = decision.features.ew_vol_ticks /
                                 (1.0 + decision.features.ew_vol_ticks);
    const double trade_component = decision.features.trade_intensity /
                                   (1.0 + decision.features.trade_intensity);
    const double cancel_component = decision.features.cancel_intensity /
                                    (1.0 + decision.features.cancel_intensity);
    decision.toxicity = clamp(0.35 * std::abs(decision.features.ofi)
                            + 0.20 * ret_component
                            + 0.20 * vol_component
                            + 0.15 * trade_component
                            + 0.10 * cancel_component, 0.0, 1.0);

    if (decision.toxicity >= model.toxicity_withdraw_threshold) {
        exploration_active_ = 0;
        decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
        return control_exit(DecisionReason::Toxic, IntentType::Withdraw);
    }

    const double quote_shares = std::min(std::max(0.0, model.base_quote_shares),
                                         std::max(0.0, risk.max_quote_shares));
    if (quote_shares <= 0.0 || risk.max_abs_residual_shares <= 0.0) {
        exploration_active_ = 0;
        decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
        return control_exit(DecisionReason::InventoryLimit, IntentType::Withdraw);
    }

    const double residual_shares = inventory.residual_shares();
    const bool inventory_one_sided = std::abs(decision.features.inventory_fraction)
                                  >= model.one_sided_inventory_fraction;
    // Once inventory is outside the soft band, never ask the OMS to reduce more
    // than the actual directional residual. This is especially important after
    // a partial fill: a reward-qualified 20-share quote may leave only 10 shares
    // to unwind, and an uncovered 20-share SELL would correctly be rejected.
    const double bid_quote_shares = inventory_one_sided && residual_shares < 0.0
        ? std::min(quote_shares, -residual_shares)
        : (inventory_one_sided ? 0.0 : quote_shares);
    // Inventory is already oriented by MakerInstrumentLane, so yes_shares is
    // the quantity of the token owned by this lane.  Never rely on the PAPER
    // OMS to reject an uncovered ask: cap both normal and exploratory SELL
    // intent at the actual token inventory before economics are evaluated.
    const double token_inventory_shares = std::max(0.0, inventory.yes_shares);
    const double ask_quote_shares = inventory_one_sided && residual_shares > 0.0
        ? std::min({quote_shares, residual_shares, token_inventory_shares})
        : (inventory_one_sided ? 0.0
                               : std::min(quote_shares, token_inventory_shares));

    std::array<Candidate, 4> candidates{};
    std::size_t candidate_count = 0;
    candidates[candidate_count++] = evaluate_candidate(
        Action::Join, update.best_bid_tick, update.best_ask_tick,
        update, inventory, risk, model, decision.features, robust_bid_fair, robust_ask_fair,
        bid_quote_shares, ask_quote_shares);
    if (update.best_ask_tick - update.best_bid_tick >= 3) {
        candidates[candidate_count++] = evaluate_candidate(
            Action::Improve1, update.best_bid_tick + 1, update.best_ask_tick - 1,
            update, inventory, risk, model, decision.features, robust_bid_fair, robust_ask_fair,
            bid_quote_shares, ask_quote_shares);
    }
    candidates[candidate_count++] = evaluate_candidate(
        Action::Fade1, update.best_bid_tick - 1, update.best_ask_tick + 1,
        update, inventory, risk, model, decision.features, robust_bid_fair, robust_ask_fair,
        bid_quote_shares, ask_quote_shares);
    candidates[candidate_count++] = evaluate_candidate(
        Action::Fade2, update.best_bid_tick - 2, update.best_ask_tick + 2,
        update, inventory, risk, model, decision.features, robust_bid_fair, robust_ask_fair,
        bid_quote_shares, ask_quote_shares);

    Candidate* best = nullptr;
    double best_observed_robust_ev = -std::numeric_limits<double>::infinity();
    double best_observed_uncertainty = 0.0;
    for (std::size_t i = 0; i < candidate_count; ++i) {
        auto& candidate = candidates[i];
        for (const auto* side : {&candidate.bid, &candidate.ask}) {
            if (finite(side->robust_ev) && side->robust_ev > best_observed_robust_ev) {
                best_observed_robust_ev = side->robust_ev;
                best_observed_uncertainty = side->uncertainty;
            }
        }
        if (inventory_one_sided) {
            if (decision.features.inventory_fraction > 0.0) candidate.bid.admissible = false;
            else if (decision.features.inventory_fraction < 0.0) candidate.ask.admissible = false;
            candidate.score = 0.0;
            if (candidate.bid.admissible) candidate.score += candidate.bid.robust_ev;
            if (candidate.ask.admissible) candidate.score += candidate.ask.robust_ev;
            if (!candidate.bid.admissible && !candidate.ask.admissible) {
                candidate.score = -std::numeric_limits<double>::infinity();
            }
        }
        if (best == nullptr || candidate.score > best->score) best = &candidate;
    }

    const bool economic_quote = best != nullptr && finite(best->score)
                             && best->score > model.min_robust_ev_per_share;
    const std::int64_t now = monotonic_ns();
    const bool lifetime_hold = quotes.last_quote_monotonic_ns > 0
        && now - quotes.last_quote_monotonic_ns < model.min_quote_lifetime_ns;
    const std::int64_t exploration_elapsed = last_exploration_quote_ns_ > 0
        ? std::max<std::int64_t>(0, now - last_exploration_quote_ns_)
        : std::numeric_limits<std::int64_t>::max();
    const bool exploration_quote_active = exploration_side_ == Side::Sell
        ? quotes.ask_active != 0 : quotes.bid_active != 0;
    const std::int64_t exploration_quote_tick = exploration_side_ == Side::Sell
        ? quotes.ask_tick : quotes.bid_tick;

    // The minimum exposure belongs to the accepted order, not to the decision
    // branch that happens to be selected on a later book update.  In
    // particular, a transient move above the normal robust-EV threshold must
    // not "promote" the order, clear its exploration state, and let the next
    // negative-EV tick cancel it under the shorter ordinary quote lifetime.
    // This also covers the asynchronous decision -> OMS LIVE acknowledgement
    // gap: an intervening market update sees no active quote yet, but must not
    // clear the order's lifetime ownership or enqueue a competing placement.
    // All safety exits are evaluated before candidate selection, so holding
    // here protects only non-safety economic/repricing changes.
    if (exploration_active_ && exploration_elapsed < model.exploration_min_rest_ns) {
        decision.action = exploration_action_;
        decision.reason = DecisionReason::ExplorationHold;
        decision.exploration_max_rest_ns = exploration_max_rest_ns_;
        decision.exploration_persistent = exploration_persistent_;
        if (economic_quote) {
            decision.robust_ev = best->score;
        } else if (finite(best_observed_robust_ev)) {
            decision.robust_ev = best_observed_robust_ev;
            decision.ev_uncertainty = best_observed_uncertainty;
        }
        const std::int64_t end_ns = monotonic_ns();
        decision.latency.decision_ns = end_ns - decision_start_ns;
        decision.latency.receive_to_intent_ns = update.socket_receive_monotonic_ns > 0
            ? std::max<std::int64_t>(0, end_ns - update.socket_receive_monotonic_ns) : 0;
        return decision;
    }

    if (exploration_active_ && !exploration_quote_active) {
        exploration_active_ = 0;
        exploration_side_ = Side::None;
    }

    if (!economic_quote) {
        // Preserve the best rejected economics for aggregate diagnostics. This
        // does not grant execution authority: quote intents below the robust-EV
        // threshold remain forbidden, but operators can distinguish an
        // economic rejection from a dead feed or invalid model.
        if (finite(best_observed_robust_ev)) {
            decision.robust_ev = best_observed_robust_ev;
            decision.ev_uncertainty = best_observed_uncertainty;
        }
        // A transient feature update must not turn a freshly accepted quote into
        // an immediate cancel. The old path bypassed min_quote_lifetime here,
        // producing LIVE -> CANCEL_PENDING in the same millisecond and making
        // almost every quote impossible to hit. Safety exits above (kill, stale
        // feed, toxicity and inventory controls) remain immediate.
        if ((quotes.bid_active != 0 || quotes.ask_active != 0) && lifetime_hold) {
            decision.action = Action::Withdraw;
            decision.reason = DecisionReason::QuoteLifetimeHold;
            const std::int64_t end_ns = monotonic_ns();
            decision.latency.decision_ns = end_ns - decision_start_ns;
            decision.latency.receive_to_intent_ns = update.socket_receive_monotonic_ns > 0
                ? std::max<std::int64_t>(0, end_ns - update.socket_receive_monotonic_ns) : 0;
            return decision;
        }
        // PAPER-only exploration collects side-specific BUY and
        // inventory-backed SELL evidence. It may use a lower explicit
        // confidence penalty than exploit, but adjusted EV must remain positive.
        const double exploration_scale = clamp(
            model.exploration_quote_notional_fraction / kNormalQuoteCapitalFraction,
            0.0, 1.0);
        double exploration_cap = std::max(0.0, risk.max_quote_shares) * exploration_scale;
        if (risk.exploration_max_quote_shares > 0.0) {
            exploration_cap = std::min(exploration_cap, risk.exploration_max_quote_shares);
        }
        const double exploration_shares = std::min(quote_shares, exploration_cap);
        std::array<Candidate, 2> exploration_candidates{};
        std::size_t exploration_candidate_count = 0;
        exploration_candidates[exploration_candidate_count++] = evaluate_candidate(
            Action::Join, update.best_bid_tick, update.best_ask_tick,
            update, inventory, risk, model, decision.features, robust_bid_fair, robust_ask_fair,
            exploration_shares, std::min(exploration_shares, token_inventory_shares));
        if (update.best_ask_tick - update.best_bid_tick >= 3) {
            exploration_candidates[exploration_candidate_count++] = evaluate_candidate(
                Action::Improve1, update.best_bid_tick + 1, update.best_ask_tick - 1,
                update, inventory, risk, model, decision.features, robust_bid_fair, robust_ask_fair,
                exploration_shares, std::min(exploration_shares, token_inventory_shares));
        }
        const bool exploration_configured = model.exploration_enabled != 0
            && model.exploration_epsilon > 0.0
            && model.exploration_quote_notional_fraction > 0.0
            && update.market_handle > 0
            && update.market_handle <= model.exploration_max_active_markets
            && exploration_shares > 0.0
            && !inventory_one_sided;
        if (exploration_active_ && exploration_quote_active) {
            decision.exploration_max_rest_ns = exploration_max_rest_ns_;
            decision.exploration_persistent = exploration_persistent_;
            if (exploration_max_rest_ns_ > 0
                && exploration_elapsed >= exploration_max_rest_ns_) {
                decision.action = Action::Withdraw;
                decision.reason = DecisionReason::ExplorationExpired;
                append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                    IntentType::CancelQuote, exploration_side_,
                                                    exploration_quote_tick, 0.0, nullptr, now));
                exploration_active_ = 0;
                exploration_side_ = Side::None;
            } else if (exploration_configured) {
                const auto* exploratory = exploration_side(
                    exploration_candidates.data(), exploration_candidate_count,
                    exploration_action_, exploration_side_);
                const double adjusted_ev = exploratory != nullptr
                    ? exploration_adjusted_ev(*exploratory, model)
                    : -std::numeric_limits<double>::infinity();
                const bool exploration_economic = finite(adjusted_ev)
                    && adjusted_ev > 0.0;
                if (!exploration_economic) {
                    decision.robust_ev = finite(adjusted_ev) ? adjusted_ev : 0.0;
                    decision.ev_uncertainty = exploratory != nullptr ? exploratory->uncertainty : 0.0;
                    // minimum_rest_ms is an execution contract, not merely a
                    // delay before repricing.  Without this guard a small
                    // feature oscillation could turn a freshly accepted
                    // exploratory quote into LIVE -> CANCEL_PENDING after the
                    // ordinary quote lifetime (1s in production), even though
                    // the configured exploration arm promised at least 3s of
                    // causal exposure.  Safety exits are evaluated above and
                    // remain immediate; only an economic re-evaluation is held.
                    if (exploration_elapsed < model.exploration_min_rest_ns) {
                        decision.action = exploration_action_;
                        decision.reason = DecisionReason::ExplorationHold;
                    } else {
                        decision.action = Action::Withdraw;
                        decision.reason = DecisionReason::NoEconomicQuote;
                        if (!quotes.cancel_pending) {
                            append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                                IntentType::CancelQuote,
                                                                exploration_side_,
                                                                exploration_quote_tick, 0.0,
                                                                nullptr, now));
                        }
                        exploration_active_ = 0;
                        exploration_side_ = Side::None;
                    }
                } else {
                    decision.action = exploration_action_;
                    decision.reason = DecisionReason::ExplorationHold;
                    decision.robust_ev = adjusted_ev;
                    decision.ev_uncertainty = exploratory->uncertainty;
                    const std::int64_t required_rest = std::max(
                        model.min_quote_lifetime_ns, model.exploration_min_rest_ns);
                    if (exploration_quote_tick != exploratory->price_tick
                        && exploration_elapsed >= required_rest
                        && !quotes.cancel_pending) {
                        append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                            IntentType::CancelQuote, exploration_side_,
                                                            exploration_quote_tick, 0.0, nullptr, now));
                    }
                }
            } else {
                exploration_active_ = 0;
                exploration_side_ = Side::None;
                decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
                return control_exit(DecisionReason::NoEconomicQuote, IntentType::Withdraw);
            }

            const std::int64_t end_ns = monotonic_ns();
            decision.latency.decision_ns = end_ns - decision_start_ns;
            decision.latency.receive_to_intent_ns = update.socket_receive_monotonic_ns > 0
                ? std::max<std::int64_t>(0, end_ns - update.socket_receive_monotonic_ns) : 0;
            return decision;
        }

        const bool sampled = last_exploration_quote_ns_ == 0
            || exploration_sample(update, model.exploration_epsilon);
        if (exploration_configured
            && quotes.bid_active == 0 && quotes.ask_active == 0
            && !quotes.cancel_pending && sampled) {
            const auto preferred_side = (mix64(update.state_version
                ^ (update.instrument_handle * 0x94d049bb133111ebULL)
                ^ (intent_sequence_ * 0xbf58476d1ce4e5b9ULL)) & 1ULL) != 0
                ? Side::Sell : Side::Buy;
            const auto choice = choose_exploration(
                exploration_candidates.data(), exploration_candidate_count, model,
                preferred_side);
            if (choice.economics != nullptr) {
                auto authorized_exploration = *choice.economics;
                authorized_exploration.robust_ev = choice.adjusted_ev;
                if (choice.economics->side == Side::Buy) {
                    decision.bid_fill_probability = choice.economics->fill_probability;
                    decision.bid_flow_reach_probability =
                        choice.economics->flow_reach_probability;
                    decision.bid_queue_depletion_probability =
                        choice.economics->queue_depletion_probability;
                    decision.bid_opposite_flow_shares_per_second =
                        choice.economics->opposite_flow_shares_per_second;
                    decision.bid_opposite_flow_prints_per_second =
                        choice.economics->opposite_flow_prints_per_second;
                } else {
                    decision.ask_fill_probability = choice.economics->fill_probability;
                    decision.ask_flow_reach_probability =
                        choice.economics->flow_reach_probability;
                    decision.ask_queue_depletion_probability =
                        choice.economics->queue_depletion_probability;
                    decision.ask_opposite_flow_shares_per_second =
                        choice.economics->opposite_flow_shares_per_second;
                    decision.ask_opposite_flow_prints_per_second =
                        choice.economics->opposite_flow_prints_per_second;
                }
                decision.action = choice.action;
                decision.reason = DecisionReason::ExplorationQuote;
                decision.robust_ev = choice.adjusted_ev;
                decision.ev_uncertainty = choice.economics->uncertainty;
                const bool persistent = persistent_lifetime_sample(
                    update, intent_sequence_ + 1, model.exploration_persistent_fraction);
                exploration_max_rest_ns_ = persistent
                    ? model.exploration_persistent_max_rest_ns
                    : model.exploration_max_rest_ns;
                exploration_persistent_ = static_cast<std::uint8_t>(persistent);
                decision.exploration_max_rest_ns = exploration_max_rest_ns_;
                decision.exploration_persistent = exploration_persistent_;
                append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                    IntentType::Quote, choice.economics->side,
                                                    choice.economics->price_tick,
                                                    choice.economics->quote_shares,
                                                    &authorized_exploration, now));
                exploration_active_ = 1;
                exploration_action_ = choice.action;
                exploration_side_ = choice.economics->side;
                last_exploration_quote_ns_ = now;
                const std::int64_t end_ns = monotonic_ns();
                decision.latency.decision_ns = end_ns - decision_start_ns;
                decision.latency.receive_to_intent_ns = update.socket_receive_monotonic_ns > 0
                    ? std::max<std::int64_t>(0, end_ns - update.socket_receive_monotonic_ns) : 0;
                return decision;
            }
        }

        decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
        return control_exit(DecisionReason::NoEconomicQuote, IntentType::Withdraw);
    }

    exploration_active_ = 0;
    exploration_side_ = Side::None;
    const bool want_bid = best->bid.admissible;
    const bool want_ask = best->ask.admissible;
    decision.action = inventory_one_sided || (want_bid != want_ask) ? Action::OneSided : best->action;
    decision.reason = DecisionReason::Quote;
    decision.bid_fill_probability = best->bid.fill_probability;
    decision.ask_fill_probability = best->ask.fill_probability;
    decision.bid_flow_reach_probability = best->bid.flow_reach_probability;
    decision.ask_flow_reach_probability = best->ask.flow_reach_probability;
    decision.bid_queue_depletion_probability = best->bid.queue_depletion_probability;
    decision.ask_queue_depletion_probability = best->ask.queue_depletion_probability;
    decision.bid_opposite_flow_shares_per_second =
        best->bid.opposite_flow_shares_per_second;
    decision.ask_opposite_flow_shares_per_second =
        best->ask.opposite_flow_shares_per_second;
    decision.bid_opposite_flow_prints_per_second =
        best->bid.opposite_flow_prints_per_second;
    decision.ask_opposite_flow_prints_per_second =
        best->ask.opposite_flow_prints_per_second;
    decision.robust_ev = (want_bid ? best->bid.robust_ev : 0.0) + (want_ask ? best->ask.robust_ev : 0.0);
    decision.ev_uncertainty = (want_bid ? best->bid.uncertainty : 0.0)
                            + (want_ask ? best->ask.uncertainty : 0.0);

    auto reconcile_side = [&](Side side, bool active, std::int64_t active_tick,
                              bool wanted, const SideEconomics& economics) noexcept {
        const bool same = active && wanted && active_tick == economics.price_tick;
        if (same) return;
        if (active) {
            if (lifetime_hold) return;
            append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                IntentType::CancelQuote, side, active_tick,
                                                0.0, nullptr, now));
            return;
        }
        if (wanted && !quotes.cancel_pending) {
            append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                IntentType::Quote, side, economics.price_tick,
                                                economics.quote_shares, &economics, now));
        }
    };

    reconcile_side(Side::Buy, quotes.bid_active != 0, quotes.bid_tick, want_bid, best->bid);
    reconcile_side(Side::Sell, quotes.ask_active != 0, quotes.ask_tick, want_ask, best->ask);

    if (decision.intent_count == 0) {
        if (lifetime_hold && ((quotes.bid_active && (!want_bid || quotes.bid_tick != best->bid.price_tick))
                           || (quotes.ask_active && (!want_ask || quotes.ask_tick != best->ask.price_tick)))) {
            decision.reason = DecisionReason::QuoteLifetimeHold;
        } else {
            decision.reason = DecisionReason::NoChange;
        }
    }

    const std::int64_t decision_end_ns = monotonic_ns();
    decision.latency.decision_ns = decision_end_ns - decision_start_ns;
    decision.latency.receive_to_intent_ns = update.socket_receive_monotonic_ns > 0
        ? std::max<std::int64_t>(0, decision_end_ns - update.socket_receive_monotonic_ns) : 0;
    return decision;
}

} // namespace pm::v7::maker
