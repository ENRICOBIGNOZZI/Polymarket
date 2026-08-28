#include "pm/v7_external_fair.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::external_fair {
namespace {

constexpr double kEps = 1e-12;
constexpr double kSqrt2 = 1.4142135623730950488;

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

[[nodiscard]] double logit(double probability) noexcept {
    const double p = clamp(probability, 1e-9, 1.0 - 1e-9);
    return std::log(p / (1.0 - p));
}

[[nodiscard]] double normal_cdf(double value) noexcept {
    return 0.5 * std::erfc(-value / kSqrt2);
}

[[nodiscard]] double oriented_lower(const FairValueSnapshot& fair,
                                    bool instrument_is_yes) noexcept {
    return instrument_is_yes ? fair.fair_yes_lower : 1.0 - fair.fair_yes_upper;
}

[[nodiscard]] double oriented_upper(const FairValueSnapshot& fair,
                                    bool instrument_is_yes) noexcept {
    return instrument_is_yes ? fair.fair_yes_upper : 1.0 - fair.fair_yes_lower;
}

[[nodiscard]] bool model_valid(const FairValueModelSnapshot& model) noexcept {
    return model.model_version > 0
        && model.model_hash_handle > 0
        && model.feature_schema_version > 0
        && finite(model.bridge_gap_coefficient)
        && finite(model.external_return_250ms_coefficient)
        && finite(model.external_return_1s_coefficient)
        && finite(model.sigma_per_sqrt_second_fraction)
        && model.sigma_per_sqrt_second_fraction >= 0.0
        && finite(model.bridge_residual_sigma_fraction)
        && model.bridge_residual_sigma_fraction >= 0.0
        && finite(model.sigma_floor_fraction)
        && model.sigma_floor_fraction > 0.0
        && finite(model.calibration_intercept)
        && finite(model.calibration_slope)
        && model.calibration_slope > 0.0
        && finite(model.max_microstructure_logit_adjustment)
        && model.max_microstructure_logit_adjustment >= 0.0
        && finite(model.base_logit_uncertainty)
        && model.base_logit_uncertainty >= 0.0
        && finite(model.uncertainty_z)
        && model.uncertainty_z >= 0.0
        && finite(model.max_external_disagreement_bps)
        && model.max_external_disagreement_bps >= 0.0
        && model.max_oracle_age_ns > 0
        && model.max_external_age_ns > 0
        && model.min_healthy_venues >= 1;
}

[[nodiscard]] int action_priority(const CandidateAction& candidate) noexcept {
    if (candidate.action == Action::Cancel ||
        (candidate.action == Action::Withdraw && candidate.purpose == Purpose::Risk)) {
        return 1;
    }
    if (candidate.purpose == Purpose::Liquidation) return 2;
    if (candidate.purpose == Purpose::InventoryReduction) return 3;
    if (candidate.action == Action::Take) return 4;
    if (candidate.action == Action::Make) return 5;
    return 6;
}

} // namespace

bool contract_authorized(const ContractHotSpec& contract) noexcept {
    return contract.market_handle > 0
        && contract.event_handle > 0
        && contract.yes_instrument_handle > 0
        && contract.no_instrument_handle > 0
        && contract.rules_hash_handle > 0
        && contract.oracle_feed_handle > 0
        && contract.contract_version > 0
        && contract.oracle_window_seconds > 0
        && contract.verified_template != 0
        && contract.rules_hash_recognized != 0
        && contract.reference_semantics_verified != 0
        && contract.settlement_source_verified != 0;
}

bool causal_cut_valid(const CausalCut& cut) noexcept {
    return cut.valid != 0
        && cut.causal_cut_id > 0
        && cut.decision_monotonic_ns > 0
        && cut.max_input_receive_monotonic_ns > 0
        && cut.max_input_receive_monotonic_ns <= cut.decision_monotonic_ns
        && cut.pm_state_version > 0
        && cut.oracle_state_version > 0
        && cut.external_state_version > 0
        && cut.settlement_reference_version > 0
        && cut.inventory_state_version > 0
        && cut.private_state_version > 0
        && cut.risk_state_version > 0
        && cut.trigger_source_version > 0;
}

bool fair_snapshot_valid(const FairValueSnapshot& fair,
                         std::int64_t now_ns) noexcept {
    return fair.valid != 0
        && fair.contract_verified != 0
        && fair.reference_verified != 0
        && fair.oracle_healthy != 0
        && fair.external_healthy != 0
        && finite(fair.fair_yes)
        && finite(fair.fair_yes_lower)
        && finite(fair.fair_yes_upper)
        && 0.0 <= fair.fair_yes_lower
        && fair.fair_yes_lower <= fair.fair_yes
        && fair.fair_yes <= fair.fair_yes_upper
        && fair.fair_yes_upper <= 1.0
        && fair.calculated_monotonic_ns > 0
        && fair.valid_until_monotonic_ns >= fair.calculated_monotonic_ns
        && now_ns >= fair.calculated_monotonic_ns
        && now_ns <= fair.valid_until_monotonic_ns;
}

double fee_per_share(double price,
                     const FeeScheduleSnapshot& fee,
                     bool taker) noexcept {
    if (!finite(price) || !(price > 0.0 && price < 1.0)) return 0.0;
    if (fee.valid == 0 || fee.authoritative == 0 || !finite(fee.rate)
        || !finite(fee.exponent) || fee.rate <= 0.0) {
        return 0.0;
    }
    if (fee.taker_only != 0 && !taker) return std::max(0.0, fee.maker_fee_per_share);
    const double base = std::max(0.0, price * (1.0 - price));
    return std::max(0.0, fee.rate) * std::pow(base, std::max(0.0, fee.exponent));
}

FairValueSnapshot compute_fair_value(
    const ContractHotSpec& contract,
    const SettlementReferenceSnapshot& reference,
    const OracleSnapshot& oracle,
    const ExternalAssetSnapshot& external,
    const FairValueModelSnapshot& model,
    std::int64_t decision_monotonic_ns,
    std::int64_t time_to_expiry_ns,
    double model_drift_score) noexcept {

    FairValueSnapshot out;
    out.market_handle = contract.market_handle;
    out.contract_version = contract.contract_version;
    out.reference_version = reference.reference_version;
    out.model_version = model.model_version;
    out.model_hash_handle = model.model_hash_handle;
    out.policy_version = model.policy_version;
    out.feature_schema_version = model.feature_schema_version;
    out.model_mature = model.mature;
    out.contract_verified = contract_authorized(contract) ? 1 : 0;
    out.reference_verified = reference.valid != 0 ? 1 : 0;
    out.oracle_healthy = oracle.healthy;
    out.external_healthy = external.valid != 0 ? 1 : 0;
    out.calculated_monotonic_ns = decision_monotonic_ns;
    out.model_drift_score = model_drift_score;

    if (!contract_authorized(contract) || !model_valid(model)) return out;
    if (decision_monotonic_ns <= 0 || time_to_expiry_ns <= 0) return out;
    if (reference.valid == 0 || reference.market_handle != contract.market_handle
        || reference.contract_version != contract.contract_version
        || reference.oracle_feed_handle != contract.oracle_feed_handle
        || reference.oracle_window_seconds != contract.oracle_window_seconds
        || !finite(reference.reference_value_numeric)
        || reference.reference_value_numeric <= 0.0
        || reference.reference_receive_monotonic_ns <= 0
        || reference.reference_receive_monotonic_ns > decision_monotonic_ns) {
        return out;
    }
    if (oracle.valid == 0 || oracle.healthy == 0
        || oracle.continuity == OracleContinuity::ContinuityUnknown
        || oracle.feed_handle != contract.oracle_feed_handle
        || oracle.window_seconds != contract.oracle_window_seconds
        || !finite(oracle.value_numeric) || oracle.value_numeric <= 0.0
        || oracle.receive_monotonic_ns <= 0
        || oracle.receive_monotonic_ns > decision_monotonic_ns
        || oracle.age_ns < 0 || oracle.age_ns > model.max_oracle_age_ns) {
        return out;
    }
    if (external.valid == 0 || external.venue_count_fresh < model.min_healthy_venues
        || !finite(external.venue_composite_price) || external.venue_composite_price <= 0.0
        || !finite(external.venue_dispersion_bps) || external.venue_dispersion_bps < 0.0
        || external.venue_dispersion_bps > model.max_external_disagreement_bps
        || external.latest_input_receive_monotonic_ns <= 0
        || external.latest_input_receive_monotonic_ns > decision_monotonic_ns) {
        return out;
    }
    const std::int64_t external_age_ns =
        decision_monotonic_ns - external.latest_input_receive_monotonic_ns;
    if (external_age_ns < 0 || external_age_ns > model.max_external_age_ns) return out;
    if (!finite(model_drift_score) || model_drift_score < 0.0
        || model_drift_score > model.max_model_drift_score) {
        return out;
    }

    const double efficient = external.venue_composite_price;
    const double oracle_price = oracle.value_numeric;
    const double reference_price = reference.reference_value_numeric;
    const double gap = efficient - oracle_price;
    const double momentum_fraction =
        model.external_return_250ms_coefficient * external.venue_composite_return_250ms
        + model.external_return_1s_coefficient * external.venue_composite_return_1s;
    const double expected_terminal = oracle_price
        + model.bridge_gap_coefficient * gap
        + efficient * momentum_fraction;

    double margin = expected_terminal - reference_price;
    if (contract.comparator == Comparator::LessEqual || contract.comparator == Comparator::Less) {
        margin = -margin;
    }

    const double tte_seconds = std::max(1e-6,
        static_cast<double>(time_to_expiry_ns) / 1'000'000'000.0);
    const double dispersion_fraction = external.venue_dispersion_bps / 10'000.0;
    const double sigma_fraction2 =
        model.sigma_per_sqrt_second_fraction * model.sigma_per_sqrt_second_fraction * tte_seconds
        + model.bridge_residual_sigma_fraction * model.bridge_residual_sigma_fraction
        + 0.0625 * dispersion_fraction * dispersion_fraction;
    const double sigma = reference_price * std::max(
        model.sigma_floor_fraction, std::sqrt(std::max(0.0, sigma_fraction2)));
    if (!finite(sigma) || sigma <= 0.0) return out;

    const double structural = clamp(normal_cdf(margin / sigma), 1e-9, 1.0 - 1e-9);
    const double calibrated_logit = model.calibration_intercept
        + model.calibration_slope * logit(structural);
    const double calibrated = clamp(logistic(calibrated_logit), 1e-9, 1.0 - 1e-9);
    const double raw_micro =
        model.micro_ofi_logit_coefficient * external.aggregate_ofi
        + model.micro_trade_imbalance_logit_coefficient * external.aggregate_trade_imbalance;
    const double micro = clamp(raw_micro,
        -std::max(0.0, model.max_microstructure_logit_adjustment),
         std::max(0.0, model.max_microstructure_logit_adjustment));
    const double fair_logit = logit(calibrated) + micro;
    const double fair = clamp(logistic(fair_logit), 1e-9, 1.0 - 1e-9);

    const double uncertainty = std::max(0.0, model.base_logit_uncertainty)
        + std::max(0.0, model.disagreement_logit_per_10bps)
            * (external.venue_dispersion_bps / 10.0)
        + std::max(0.0, model.jump_logit_penalty) * std::max(0.0, external.jump_score)
        + std::max(0.0, model.drift_logit_penalty) * model_drift_score;
    const double width = std::max(0.0, model.uncertainty_z) * uncertainty;
    const double lower = clamp(logistic(fair_logit - width), 0.0, fair);
    const double upper = clamp(logistic(fair_logit + width), fair, 1.0);

    const std::int64_t oracle_remaining = model.max_oracle_age_ns - oracle.age_ns;
    const std::int64_t external_remaining = model.max_external_age_ns - external_age_ns;
    const std::int64_t freshness_remaining = std::min(oracle_remaining, external_remaining);
    if (freshness_remaining <= 0) return out;

    out.fair_yes = fair;
    out.fair_yes_lower = lower;
    out.fair_yes_upper = upper;
    out.structural_probability = structural;
    out.calibrated_probability = calibrated;
    out.microstructure_logit_adjustment = micro;
    out.expected_settlement_margin = margin;
    out.settlement_margin_sigma = sigma;
    out.efficient_price = efficient;
    out.oracle_price = oracle_price;
    out.opening_reference_price = reference_price;
    out.external_disagreement_bps = external.venue_dispersion_bps;
    out.oracle_age_ns = oracle.age_ns;
    out.external_age_ns = external_age_ns;
    out.valid_until_monotonic_ns = decision_monotonic_ns + freshness_remaining;
    out.valid = 1;
    return out;
}

CandidateAction evaluate_cancel_overlay(
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
    bool global_or_market_kill) noexcept {

    CandidateAction out;
    out.market_handle = contract.market_handle;
    out.instrument_handle = instrument_handle;
    out.causal_cut_id = causal_cut_id;
    out.side = resting_side;
    out.price_tick = price_tick;
    out.purpose = Purpose::Risk;

    if (global_or_market_kill) {
        out.action = Action::Withdraw;
        out.cancel_reason = CancelReason::Kill;
        out.admissible = 1;
        out.critical = 1;
        return out;
    }
    if (policy.cancel_overlay_enabled == 0) return out;
    if (!contract_authorized(contract)) {
        out.action = Action::Withdraw;
        out.cancel_reason = CancelReason::RulesHashMismatch;
        out.admissible = 1;
        out.critical = 1;
        return out;
    }
    if (!fair_snapshot_valid(fair, now_ns)) {
        out.action = policy.external_fair_required != 0 ? Action::Withdraw : Action::Cancel;
        out.cancel_reason = fair.valid != 0 ? CancelReason::FairExpired : CancelReason::ModelInvalid;
        out.admissible = 1;
        out.critical = 1;
        return out;
    }
    const double uncertainty_width = fair.fair_yes_upper - fair.fair_yes_lower;
    if (!finite(uncertainty_width) || uncertainty_width > policy.max_uncertainty_width) {
        out.action = Action::Cancel;
        out.cancel_reason = CancelReason::UncertaintySpike;
        out.admissible = 1;
        out.critical = 1;
        return out;
    }
    if (!finite(resting_price) || !(resting_price > 0.0 && resting_price < 1.0)
        || (resting_side != Side::Buy && resting_side != Side::Sell)) {
        return out;
    }

    const double robust_capture = resting_side == Side::Buy
        ? oriented_lower(fair, instrument_is_yes) - resting_price
        : resting_price - oriented_upper(fair, instrument_is_yes);
    out.robust_ev_per_share = robust_capture;
    if (finite(robust_capture) && robust_capture < policy.maker_cancel_robust_capture_floor) {
        out.action = Action::Cancel;
        out.cancel_reason = CancelReason::FairShock;
        out.expected_change_in_wealth = 0.0;
        out.risk_cost = std::max(0.0, -robust_capture);
        out.admissible = 1;
        out.critical = 1;
    }
    return out;
}

CandidateAction build_informed_take(
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
    double tick_size) noexcept {

    CandidateAction out;
    out.action = Action::Take;
    out.purpose = Purpose::Alpha;
    out.side = Side::Buy;
    out.market_handle = contract.market_handle;
    out.instrument_handle = instrument_handle;
    out.causal_cut_id = causal_cut_id;

    if (policy.taker_enabled == 0 || !contract_authorized(contract)
        || !fair_snapshot_valid(fair, now_ns) || fair.model_mature == 0
        || fee.valid == 0 || fee.authoritative == 0 || fee.market_handle != contract.market_handle
        || book.valid == 0 || book.ask_count == 0 || book.ask_count > book.asks.size()
        || book.receive_monotonic_ns <= 0 || book.receive_monotonic_ns > now_ns
        || now_ns - book.receive_monotonic_ns > policy.max_book_age_ns
        || !finite(available_capital) || available_capital <= 0.0
        || !finite(current_market_gross_cost) || current_market_gross_cost < 0.0
        || !finite(tick_size) || tick_size <= 0.0) {
        out.admissible = 0;
        return out;
    }
    if (fair.fair_yes_upper - fair.fair_yes_lower > policy.max_uncertainty_width) return out;

    const double robust_probability = oriented_lower(fair, instrument_is_yes);
    const std::int64_t latency_ns = now_ns - book.receive_monotonic_ns;
    const double execution_risk_per_share = std::max(0.0, policy.base_taker_execution_risk_per_share)
        + std::max(0.0, policy.latency_risk_per_second)
            * static_cast<double>(latency_ns) / 1'000'000'000.0;

    const BookLevel& first = book.asks[0];
    if (!finite(first.price) || !finite(first.quantity) || first.price <= 0.0
        || first.price >= 1.0 || first.quantity <= 0.0) return out;
    const double first_fee = fee_per_share(first.price, fee, true);
    const double effective_cost = clamp(first.price + first_fee + execution_risk_per_share,
                                        1e-9, 1.0 - 1e-9);
    const double full_kelly = robust_probability > effective_cost
        ? (robust_probability - effective_cost) / std::max(kEps, 1.0 - effective_cost)
        : 0.0;
    const double kelly_fraction = clamp(policy.fractional_kelly, 0.0, 1.0)
        * clamp(full_kelly, 0.0, 1.0);
    const double market_budget = std::max(0.0,
        available_capital * clamp(policy.max_market_capital_fraction, 0.0, 1.0)
        - current_market_gross_cost);
    const double kelly_budget = available_capital * kelly_fraction;
    double remaining_budget = std::min(market_budget, kelly_budget);
    if (remaining_budget <= 0.0) return out;

    const double visible_fraction = clamp(policy.max_depth_fraction, 0.0, 1.0)
        * clamp(policy.depth_survival_fraction, 0.0, 1.0);
    double total_quantity = 0.0;
    double total_book_cost = 0.0;
    double total_fee = 0.0;
    double total_execution_risk = 0.0;
    double total_ev = 0.0;
    double last_price = 0.0;
    const std::size_t count = std::min<std::size_t>(book.ask_count, book.asks.size());

    for (std::size_t i = 0; i < count; ++i) {
        const auto& level = book.asks[i];
        if (!finite(level.price) || !finite(level.quantity) || level.price <= 0.0
            || level.price >= 1.0 || level.quantity <= 0.0) break;
        const double per_share_fee = fee_per_share(level.price, fee, true);
        const double mev = robust_probability - level.price - per_share_fee
            - execution_risk_per_share;
        if (!finite(mev) || mev <= policy.taker_minimum_robust_ev_per_share) break;
        const double executable_quantity = level.quantity * visible_fraction;
        if (executable_quantity <= 0.0) continue;
        const double unit_cash_cost = level.price + per_share_fee;
        const double budget_quantity = remaining_budget / std::max(kEps, unit_cash_cost);
        const double quantity = std::min({executable_quantity,
                                          std::max(0.0, policy.max_quantity - total_quantity),
                                          budget_quantity});
        if (quantity <= 0.0) break;
        total_quantity += quantity;
        total_book_cost += quantity * level.price;
        total_fee += quantity * per_share_fee;
        total_execution_risk += quantity * execution_risk_per_share;
        total_ev += quantity * mev;
        remaining_budget -= quantity * unit_cash_cost;
        last_price = level.price;
        if (remaining_budget <= kEps || total_quantity >= policy.max_quantity - kEps) break;
    }

    if (total_quantity <= 0.0 || total_ev <= 0.0 || last_price <= 0.0) return out;
    out.quantity_microunits = static_cast<std::int64_t>(
        std::llround(total_quantity * 1'000'000.0));
    out.average_price = total_book_cost / total_quantity;
    out.price_tick = static_cast<std::int64_t>(std::ceil(last_price / tick_size - 1e-12));
    out.expected_change_in_wealth = total_ev;
    out.robust_ev_per_share = total_ev / total_quantity;
    out.risk_cost = total_execution_risk;
    out.capital_time_cost = 0.0;
    out.execution_uncertainty = (fair.fair_yes_upper - fair.fair_yes_lower)
        + execution_risk_per_share;
    out.admissible = 1;
    return out;
}

CandidateAction choose_unified_action(
    const std::array<CandidateAction, kMaxUnifiedCandidates>& candidates,
    std::size_t candidate_count,
    bool global_or_strategy_or_market_kill) noexcept {

    if (global_or_strategy_or_market_kill) {
        CandidateAction out;
        out.action = Action::Withdraw;
        out.purpose = Purpose::Risk;
        out.cancel_reason = CancelReason::Kill;
        out.admissible = 1;
        out.critical = 1;
        return out;
    }

    CandidateAction best;
    int best_priority = std::numeric_limits<int>::max();
    double best_score = -std::numeric_limits<double>::infinity();
    const std::size_t count = std::min(candidate_count, candidates.size());
    for (std::size_t i = 0; i < count; ++i) {
        const auto& candidate = candidates[i];
        if (candidate.admissible == 0 || candidate.action == Action::Nothing) continue;
        const int priority = action_priority(candidate);
        const double score = finite(candidate.expected_change_in_wealth)
            ? candidate.expected_change_in_wealth : -std::numeric_limits<double>::infinity();
        if (priority < best_priority || (priority == best_priority && score > best_score)) {
            best = candidate;
            best_priority = priority;
            best_score = score;
        }
    }
    return best;
}

StrategyIntent to_strategy_intent(
    const CandidateAction& action,
    std::uint64_t intent_id,
    std::uint64_t event_handle,
    std::uint64_t state_version,
    std::uint64_t model_version,
    std::uint64_t policy_version,
    std::int64_t decision_monotonic_ns) noexcept {

    StrategyIntent intent;
    intent.intent_id = intent_id;
    intent.market_handle = action.market_handle;
    intent.event_handle = event_handle;
    intent.instrument_handle = action.instrument_handle;
    intent.state_version = state_version;
    intent.model_version = model_version;
    intent.policy_version = policy_version;
    intent.decision_monotonic_ns = decision_monotonic_ns;
    intent.price_tick = action.price_tick;
    intent.quantity_microunits = action.quantity_microunits;
    intent.strategy_id = StrategyId::ExternalInformation;
    intent.side = action.side;
    intent.expected_edge = action.robust_ev_per_share;
    intent.expected_cost = action.risk_cost + action.capital_time_cost;
    intent.expected_risk = action.risk_cost;
    intent.expected_ev = action.expected_change_in_wealth;
    intent.ev_uncertainty = action.execution_uncertainty;

    switch (action.action) {
        case Action::Make:
            intent.type = IntentType::Quote;
            intent.urgency = Urgency::Passive;
            intent.passive = 1;
            intent.post_only = 1;
            break;
        case Action::Take:
            intent.type = IntentType::TargetPosition;
            intent.urgency = Urgency::Aggressive;
            intent.passive = 0;
            intent.post_only = 0;
            break;
        case Action::Cancel:
            intent.type = IntentType::CancelQuote;
            intent.urgency = Urgency::Critical;
            intent.quantity_microunits = 0;
            intent.passive = 1;
            intent.post_only = 1;
            break;
        case Action::Withdraw:
            intent.type = IntentType::Withdraw;
            intent.urgency = Urgency::Critical;
            intent.quantity_microunits = 0;
            intent.passive = 1;
            intent.post_only = 1;
            break;
        case Action::Nothing:
        default:
            intent.type = IntentType::TargetPosition;
            intent.urgency = Urgency::Normal;
            intent.quantity_microunits = 0;
            intent.passive = 1;
            intent.post_only = 1;
            break;
    }
    return intent;
}

} // namespace pm::v7::external_fair
