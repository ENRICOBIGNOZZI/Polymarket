#include "pm/v7_external_execution.hpp"

#include <algorithm>
#include <cmath>

namespace pm::v7::external_fair {
namespace {

constexpr double kEps = 1e-12;

[[nodiscard]] bool finite(double value) noexcept { return std::isfinite(value); }

[[nodiscard]] double oriented_lower_bound(const FairValueSnapshot& fair,
                                          bool instrument_is_yes) noexcept {
    return instrument_is_yes ? fair.fair_yes_lower : 1.0 - fair.fair_yes_upper;
}

[[nodiscard]] double oriented_upper_bound(const FairValueSnapshot& fair,
                                          bool instrument_is_yes) noexcept {
    return instrument_is_yes ? fair.fair_yes_upper : 1.0 - fair.fair_yes_lower;
}

} // namespace

IntentPurpose to_intent_purpose(Purpose purpose) noexcept {
    switch (purpose) {
        case Purpose::InventoryReduction: return IntentPurpose::InventoryReduction;
        case Purpose::Risk: return IntentPurpose::Risk;
        case Purpose::Liquidation: return IntentPurpose::Liquidation;
        case Purpose::Alpha:
        default: return IntentPurpose::Alpha;
    }
}

ExecutionPlan build_execution_plan(
    const CandidateAction& action,
    std::uint64_t intent_id,
    std::uint64_t event_handle,
    std::uint64_t state_version,
    std::uint64_t model_version,
    std::uint64_t policy_version,
    std::int64_t decision_monotonic_ns,
    std::int32_t tick_size_e4) noexcept {

    ExecutionPlan plan;
    plan.intent = to_strategy_intent(action, intent_id, event_handle, state_version,
                                     model_version, policy_version,
                                     decision_monotonic_ns);
    plan.intent.purpose = to_intent_purpose(action.purpose);
    plan.tick_size_e4 = tick_size_e4;
    plan.market_state_version = state_version;
    plan.public_queue_observable = action.action == Action::Make ? 1 : 0;

    if (action.purpose == Purpose::Liquidation) {
        plan.policy = ExecutionPolicyId::Liquidation;
    } else if (action.critical != 0 || action.action == Action::Cancel
               || action.action == Action::Withdraw) {
        plan.policy = ExecutionPolicyId::Emergency;
    } else if (action.action == Action::Take) {
        plan.policy = ExecutionPolicyId::AggressiveTaker;
    } else {
        plan.policy = ExecutionPolicyId::PassiveMaker;
    }
    return plan;
}

SelfCrossGuardResult guard_self_cross(
    const CandidateAction& take,
    const OwnOrderView& own,
    std::uint64_t causal_cut_id) noexcept {

    SelfCrossGuardResult out;
    out.deferred_take = take;
    out.required_private_state_version = own.private_state_version;
    if (take.admissible == 0 || take.action != Action::Take
        || take.instrument_handle == 0 || take.instrument_handle != own.instrument_handle
        || take.price_tick <= 0 || (take.side != Side::Buy && take.side != Side::Sell)) {
        return out;
    }

    bool conflict = false;
    bool cancel_pending = false;
    Side cancel_side = Side::None;
    std::int64_t cancel_tick = 0;
    if (take.side == Side::Buy) {
        conflict = own.ask_active != 0 && own.ask_tick > 0 && own.ask_tick <= take.price_tick;
        cancel_pending = own.ask_cancel_pending != 0;
        cancel_side = Side::Sell;
        cancel_tick = own.ask_tick;
    } else {
        conflict = own.bid_active != 0 && own.bid_tick > 0 && own.bid_tick >= take.price_tick;
        cancel_pending = own.bid_cancel_pending != 0;
        cancel_side = Side::Buy;
        cancel_tick = own.bid_tick;
    }

    if (!conflict) {
        out.immediate = take;
        out.take_allowed = 1;
        return out;
    }

    out.requires_cancel_confirmation = 1;
    out.required_private_state_version = own.private_state_version + 1;
    if (cancel_pending) {
        return out;
    }

    CandidateAction cancel;
    cancel.action = Action::Cancel;
    cancel.purpose = Purpose::Risk;
    cancel.side = cancel_side;
    cancel.cancel_reason = CancelReason::Risk;
    cancel.market_handle = take.market_handle;
    cancel.instrument_handle = take.instrument_handle;
    cancel.causal_cut_id = causal_cut_id;
    cancel.price_tick = cancel_tick;
    cancel.admissible = 1;
    cancel.critical = 1;
    out.immediate = cancel;
    return out;
}

TakerPaperFill simulate_taker_paper(
    const ExecutionPlan& plan,
    const AggressiveBook& arrival_book,
    const FeeScheduleSnapshot& fee,
    const FairValueSnapshot& fair,
    bool instrument_is_yes,
    AggressiveTimeInForce tif,
    std::int64_t arrival_monotonic_ns) noexcept {

    TakerPaperFill out;
    out.requested_microunits = plan.intent.quantity_microunits;
    out.arrival_monotonic_ns = arrival_monotonic_ns;
    out.fee_authoritative = fee.valid != 0 && fee.authoritative != 0 ? 1 : 0;

    if (plan.policy != ExecutionPolicyId::AggressiveTaker
        || plan.intent.type != IntentType::TargetPosition
        || plan.intent.side != Side::Buy
        || plan.intent.passive != 0 || plan.intent.post_only != 0
        || plan.intent.quantity_microunits <= 0 || plan.intent.price_tick <= 0
        || plan.tick_size_e4 <= 0 || arrival_book.valid == 0
        || arrival_book.ask_count == 0 || arrival_book.ask_count > arrival_book.asks.size()
        || arrival_book.receive_monotonic_ns <= 0
        || arrival_book.receive_monotonic_ns > arrival_monotonic_ns
        || fee.valid == 0 || fee.authoritative == 0
        || fee.market_handle != plan.intent.market_handle
        || !fair_snapshot_valid(fair, arrival_monotonic_ns)) {
        out.rejected = 1;
        return out;
    }

    const double tick_size = static_cast<double>(plan.tick_size_e4) / 10'000.0;
    const double limit_price = static_cast<double>(plan.intent.price_tick) * tick_size;
    if (!finite(limit_price) || !(limit_price > 0.0 && limit_price < 1.0)) {
        out.rejected = 1;
        return out;
    }

    const double requested = static_cast<double>(plan.intent.quantity_microunits) / 1'000'000.0;
    double remaining = requested;
    double filled = 0.0;
    double book_cost = 0.0;
    double total_fee = 0.0;
    const std::size_t count = std::min<std::size_t>(arrival_book.ask_count,
                                                    arrival_book.asks.size());
    for (std::size_t i = 0; i < count && remaining > kEps; ++i) {
        const auto& level = arrival_book.asks[i];
        if (!finite(level.price) || !finite(level.quantity) || level.quantity <= 0.0
            || level.price <= 0.0 || level.price >= 1.0) {
            break;
        }
        if (level.price > limit_price + 1e-12) break;
        const double quantity = std::min(remaining, level.quantity);
        filled += quantity;
        book_cost += quantity * level.price;
        total_fee += quantity * fee_per_share(level.price, fee, true);
        remaining -= quantity;
    }

    if (tif == AggressiveTimeInForce::Fok && remaining > 1e-9) {
        return out;
    }
    if (filled <= 0.0) return out;

    out.filled_microunits = static_cast<std::int64_t>(std::llround(filled * 1'000'000.0));
    out.average_fill_price = book_cost / filled;
    out.gross_book_cost = book_cost;
    out.authoritative_fee = total_fee;
    const double robust_probability = oriented_lower_bound(fair, instrument_is_yes);
    out.robust_terminal_value = filled * robust_probability;
    out.robust_net_ev = out.robust_terminal_value - book_cost - total_fee;
    // Limit-relative execution slippage is diagnostic only. For a BUY a
    // negative value is price improvement; it is not double-counted in EV.
    out.slippage_vs_plan = filled * (out.average_fill_price - limit_price);
    out.complete = remaining <= 1e-9 ? 1 : 0;
    return out;
}

CandidateAction build_external_make(
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
    std::int64_t now_ns) noexcept {

    CandidateAction out;
    out.action = Action::Make;
    out.purpose = Purpose::Alpha;
    out.side = side;
    out.market_handle = contract.market_handle;
    out.instrument_handle = instrument_handle;
    out.causal_cut_id = causal_cut_id;

    if (maker_policy.enabled == 0 || !contract_authorized(contract)
        || !fair_snapshot_valid(fair, now_ns)
        || (side != Side::Buy && side != Side::Sell)
        || !finite(tick_size) || tick_size <= 0.0 || tick_size >= 1.0
        || best_bid_tick <= 0 || best_ask_tick <= best_bid_tick
        || !finite(signed_inventory_fraction)
        || !finite(maker_policy.inventory_skew_ticks)
        || !finite(maker_policy.quote_quantity) || maker_policy.quote_quantity <= 0.0) {
        out.admissible = 0;
        return out;
    }

    // Quote from the robust interval boundary, not the point fair. Quoting at
    // the point estimate and then requiring positive lower-bound capture makes
    // every non-degenerate interval inadmissible unless the touch happens to
    // cap the price. The uncertainty-aware anchor guarantees that admissible
    // maker quotes have the required conservative capture by construction.
    const double robust_anchor = side == Side::Buy
        ? oriented_lower_bound(fair, instrument_is_yes)
        : oriented_upper_bound(fair, instrument_is_yes);
    const double capture_buffer = side == Side::Buy
        ? -std::max(0.0, maker_policy.minimum_robust_capture_per_share)
        :  std::max(0.0, maker_policy.minimum_robust_capture_per_share);
    const double reservation = robust_anchor + capture_buffer
        - maker_policy.inventory_skew_ticks * tick_size * signed_inventory_fraction;
    std::int64_t quote_tick = 0;
    if (side == Side::Buy) {
        quote_tick = static_cast<std::int64_t>(std::floor(reservation / tick_size + 1e-12));
        quote_tick = std::min(quote_tick, best_ask_tick - 1);
    } else {
        quote_tick = static_cast<std::int64_t>(std::ceil(reservation / tick_size - 1e-12));
        quote_tick = std::max(quote_tick, best_bid_tick + 1);
    }
    if (quote_tick <= 0) return out;
    const double price = static_cast<double>(quote_tick) * tick_size;
    if (!(price > 0.0 && price < 1.0)) return out;

    const double robust_capture = side == Side::Buy
        ? oriented_lower_bound(fair, instrument_is_yes) - price
        : price - oriented_upper_bound(fair, instrument_is_yes);
    if (!finite(robust_capture)
        || robust_capture <= maker_policy.minimum_robust_capture_per_share) {
        return out;
    }

    out.price_tick = quote_tick;
    out.quantity_microunits = static_cast<std::int64_t>(
        std::llround(maker_policy.quote_quantity * 1'000'000.0));
    out.average_price = price;
    out.robust_ev_per_share = robust_capture;
    out.expected_change_in_wealth = maker_policy.quote_quantity * robust_capture;
    out.admissible = out.quantity_microunits > 0 ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
