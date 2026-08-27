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

[[nodiscard]] double logistic(double value) noexcept {
    if (value >= 0.0) {
        const double e = std::exp(-value);
        return 1.0 / (1.0 + e);
    }
    const double e = std::exp(value);
    return e / (1.0 + e);
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
    double fill_probability = 0.0;
    double gross_capture = 0.0;
    double expected_cost = 0.0;
    double expected_risk = 0.0;
    double uncertainty = 0.0;
    double robust_ev = -std::numeric_limits<double>::infinity();
};

struct Candidate {
    Action action = Action::Withdraw;
    SideEconomics bid{};
    SideEconomics ask{};
    double score = -std::numeric_limits<double>::infinity();
};

[[nodiscard]] std::array<double, kFeatureCount> model_features(
    const Features& features, Side side) noexcept {
    const double direction = side == Side::Buy ? 1.0 : -1.0;
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
    };
}

[[nodiscard]] SideEconomics evaluate_side(
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

    if (quote_shares <= 0.0 || quote_tick <= 0) return out;
    if (side == Side::Buy && quote_tick >= update.best_ask_tick) return out;
    if (side == Side::Sell && quote_tick <= update.best_bid_tick) return out;

    const double price = static_cast<double>(quote_tick) * model.tick_size;
    if (!(price > 0.0 && price < 1.0)) return out;

    const auto x = model_features(features, side);
    const double touch_depth = side == Side::Buy ? update.bid_depth_l1 : update.ask_depth_l1;
    const double distance_ticks = side == Side::Buy
        ? static_cast<double>(update.best_bid_tick - quote_tick)
        : static_cast<double>(quote_tick - update.best_ask_tick);

    // The first deployed model is deliberately simple and interpretable. Queue
    // depth and distance-to-touch enter causally; slow-path challengers can
    // replace these coefficients via a future snapshot schema extension.
    const double queue_penalty = 0.10 * std::log1p(std::max(0.0, touch_depth) /
                                                   std::max(kEps, quote_shares));
    const double fill_logit = dot(model.fill_coefficients, x)
                            - 0.55 * distance_ticks - queue_penalty;
    const double fill_probability = clamp(logistic(fill_logit), 0.0, 1.0);

    double adverse_markout = std::max(0.0, dot(model.markout_coefficients, x));
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
    const double uncertainty = std::max(0.0, model.base_ev_se_per_share) *
        (1.0 + std::abs(features.ofi) + 0.25 * features.ew_vol_ticks
             + 0.25 * std::abs(features.inventory_fraction));
    const double robust_ev = expected_ev - std::max(0.0, model.robust_ev_z) * uncertainty;

    out.admissible = finite(robust_ev) && robust_ev > model.min_robust_ev_per_share;
    out.fill_probability = fill_probability;
    out.gross_capture = capture;
    out.expected_cost = expected_cost;
    out.expected_risk = adverse_markout + inventory_cost;
    out.uncertainty = uncertainty;
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
    double fair_value,
    double quote_shares) noexcept {

    Candidate candidate;
    candidate.action = action;
    candidate.bid = evaluate_side(Side::Buy, bid_tick, update, inventory, risk, model,
                                  features, fair_value, quote_shares);
    candidate.ask = evaluate_side(Side::Sell, ask_tick, update, inventory, risk, model,
                                  features, fair_value, quote_shares);
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
    if (!finite(toxicity_withdraw_threshold) || toxicity_withdraw_threshold < 0.0 || toxicity_withdraw_threshold > 1.0) return false;
    if (!finite(one_sided_inventory_fraction) || one_sided_inventory_fraction < 0.0) return false;
    if (min_quote_lifetime_ns < 0 || max_related_snapshot_age_ns < 0) return false;
    for (double value : fill_coefficients) if (!finite(value)) return false;
    for (double value : markout_coefficients) if (!finite(value)) return false;
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

    const double decay = model.valid() ? model.feature_decay : 0.90;
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
    ew_var_ticks2_ = decay * ew_var_ticks2_ + (1.0 - decay) * short_return_ticks * short_return_ticks;
    const double visible_depth = std::max(kEps, l5_total);
    const double trade_event_intensity = (std::max(0.0, update.trade_buy_qty_delta)
                                         + std::max(0.0, update.trade_sell_qty_delta)) / visible_depth;
    const double cancel_event_intensity = (std::max(0.0, update.cancel_bid_qty_delta)
                                          + std::max(0.0, update.cancel_ask_qty_delta)) / visible_depth;
    ew_trade_intensity_ = decay * ew_trade_intensity_ + (1.0 - decay) * trade_event_intensity;
    ew_cancel_intensity_ = decay * ew_cancel_intensity_ + (1.0 - decay) * cancel_event_intensity;

    previous_mid_ = mid;
    previous_bid_depth_ = update.bid_depth_l5;
    previous_ask_depth_ = update.ask_depth_l5;
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
    decision.features.cancel_intensity = std::max(0.0, ew_cancel_intensity_);
    decision.features.inventory_fraction = clamp(
        inventory.residual_shares() / std::max(kEps, risk.max_abs_residual_shares), -2.0, 2.0);
    decision.features.local_latency_ms = static_cast<double>(local_age_ns) / 1'000'000.0;
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
    decision.fair_value = fair_value;
    decision.reservation_value = reservation_value;

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
        decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
        return control_exit(DecisionReason::Toxic, IntentType::Withdraw);
    }

    const double quote_shares = std::min(std::max(0.0, model.base_quote_shares),
                                         std::max(0.0, risk.max_quote_shares));
    if (quote_shares <= 0.0 || risk.max_abs_residual_shares <= 0.0) {
        decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
        return control_exit(DecisionReason::InventoryLimit, IntentType::Withdraw);
    }

    std::array<Candidate, 4> candidates{};
    std::size_t candidate_count = 0;
    candidates[candidate_count++] = evaluate_candidate(
        Action::Join, update.best_bid_tick, update.best_ask_tick,
        update, inventory, risk, model, decision.features, reservation_value, quote_shares);
    if (update.best_ask_tick - update.best_bid_tick >= 3) {
        candidates[candidate_count++] = evaluate_candidate(
            Action::Improve1, update.best_bid_tick + 1, update.best_ask_tick - 1,
            update, inventory, risk, model, decision.features, reservation_value, quote_shares);
    }
    candidates[candidate_count++] = evaluate_candidate(
        Action::Fade1, update.best_bid_tick - 1, update.best_ask_tick + 1,
        update, inventory, risk, model, decision.features, reservation_value, quote_shares);
    candidates[candidate_count++] = evaluate_candidate(
        Action::Fade2, update.best_bid_tick - 2, update.best_ask_tick + 2,
        update, inventory, risk, model, decision.features, reservation_value, quote_shares);

    Candidate* best = nullptr;
    const bool inventory_one_sided = std::abs(decision.features.inventory_fraction)
                                  >= model.one_sided_inventory_fraction;
    for (std::size_t i = 0; i < candidate_count; ++i) {
        auto& candidate = candidates[i];
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

    if (best == nullptr || !finite(best->score) || best->score <= model.min_robust_ev_per_share) {
        decision.latency.decision_ns = monotonic_ns() - decision_start_ns;
        return control_exit(DecisionReason::NoEconomicQuote, IntentType::Withdraw);
    }

    const bool want_bid = best->bid.admissible;
    const bool want_ask = best->ask.admissible;
    decision.action = inventory_one_sided || (want_bid != want_ask) ? Action::OneSided : best->action;
    decision.reason = DecisionReason::Quote;
    decision.robust_ev = (want_bid ? best->bid.robust_ev : 0.0) + (want_ask ? best->ask.robust_ev : 0.0);
    decision.ev_uncertainty = (want_bid ? best->bid.uncertainty : 0.0)
                            + (want_ask ? best->ask.uncertainty : 0.0);

    const std::int64_t now = monotonic_ns();
    const bool lifetime_hold = quotes.last_quote_monotonic_ns > 0
        && now - quotes.last_quote_monotonic_ns < model.min_quote_lifetime_ns;

    auto reconcile_side = [&](Side side, bool active, std::int64_t active_tick,
                              bool wanted, const SideEconomics& economics) noexcept {
        const bool same = active && wanted && active_tick == economics.price_tick;
        if (same) return;
        if (active) {
            if (lifetime_hold) return;
            append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                IntentType::CancelQuote, side, active_tick,
                                                0.0, nullptr, now));
            // Do not replace on the same cycle. The common OMS/private state must
            // observe the cancel transition before a fresh quote is admitted.
            return;
        }
        if (wanted && !quotes.cancel_pending) {
            append_intent(decision, make_intent(++intent_sequence_, update, model,
                                                IntentType::Quote, side, economics.price_tick,
                                                quote_shares, &economics, now));
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
