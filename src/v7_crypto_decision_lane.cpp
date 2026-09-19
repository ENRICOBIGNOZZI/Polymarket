#include "pm/v7_crypto_decision_lane.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>

namespace pm::v7 {
namespace {

[[nodiscard]] std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] bool valid_market_handle(std::uint64_t handle) noexcept {
    return handle > 0 && handle < kMaxCapitalMarkets;
}

[[nodiscard]] bool valid_book(const BookHotSnapshot& book,
                              std::int64_t now_ns,
                              std::int64_t maximum_age_ns) noexcept {
    return book.valid != 0 && book.lineage_continuous != 0
        && book.state_version > 0 && book.tick_size_e4 > 0
        && book.best_ask_e4 > 0 && book.best_ask_e4 < kCanonicalPriceScale
        && book.best_ask_microunits > 0
        && book.receive_monotonic_ns > 0 && book.receive_monotonic_ns <= now_ns
        && now_ns - book.receive_monotonic_ns <= maximum_age_ns;
}

} // namespace
NativeCryptoDecisionLane::NativeCryptoDecisionLane(
    NativeCryptoDecisionPolicy policy) noexcept
    : policy_(policy) {}

bool NativeCryptoDecisionLane::seen_signal(
    std::uint64_t market_handle,
    std::uint64_t signal_version) const noexcept {
    return valid_market_handle(market_handle) && signal_version > 0
        && last_signal_version_[market_handle] >= signal_version;
}

void NativeCryptoDecisionLane::remember_signal(
    std::uint64_t market_handle,
    std::uint64_t signal_version) noexcept {
    if (valid_market_handle(market_handle) && signal_version > 0) {
        last_signal_version_[market_handle] = signal_version;
    }
}

bool NativeCryptoDecisionLane::market_traded(std::uint64_t market_handle) const noexcept {
    return valid_market_handle(market_handle) && traded_market_[market_handle] != 0;
}

void NativeCryptoDecisionLane::mark_market_traded(std::uint64_t market_handle) noexcept {
    if (valid_market_handle(market_handle)) traded_market_[market_handle] = 1;
}

void NativeCryptoDecisionLane::reset_market(std::uint64_t market_handle) noexcept {
    if (!valid_market_handle(market_handle)) return;
    last_signal_version_[market_handle] = 0;
    traded_market_[market_handle] = 0;
}
NativeCryptoDecisionResult NativeCryptoDecisionLane::construct_candidate(
    const NativeCryptoDecisionInput& input) noexcept {
    const auto started_ns = monotonic_now_ns();
    NativeCryptoDecisionResult out;
    out.signal_version = input.signal.signal_version;
    const auto finish = [&](NativeCryptoDecisionReason reason) noexcept {
        out.reason = reason;
        out.decision_compute_ns = std::max<std::int64_t>(0, monotonic_now_ns() - started_ns);
        return out;
    };

    const auto now_ns = input.now_monotonic_ns;
    if (now_ns <= 0 || input.signal.signal_version == 0
        || input.signal.trigger_receive_monotonic_ns <= 0
        || input.signal.trigger_receive_monotonic_ns > now_ns
        || input.signal.confirmed_non_opposing == 0
        || (input.signal.direction != 1 && input.signal.direction != -1)
        || (policy_.require_signal_valid != 0 && input.signal.valid == 0)) {
        return finish(NativeCryptoDecisionReason::InvalidSignal);
    }
    out.signal_age_ns = now_ns - input.signal.trigger_receive_monotonic_ns;
    if (out.signal_age_ns > policy_.maximum_signal_age_ns
        || (policy_.require_signal_valid != 0
            && input.signal.valid_until_monotonic_ns > 0
            && now_ns > input.signal.valid_until_monotonic_ns)) {
        return finish(NativeCryptoDecisionReason::ExpiredSignal);
    }
    if (!std::isfinite(input.signal.binance_return_100ms_bp)
        || std::abs(input.signal.binance_return_100ms_bp) + 1e-12
            < policy_.minimum_absolute_binance_return_bp) {
        return finish(NativeCryptoDecisionReason::WeakSignal);
    }
    const auto& market = input.market;
    if (!valid_market_handle(market.market_handle) || market.event_handle == 0
        || market.accepting_orders == 0 || market.closed != 0
        || market.contract_verified == 0 || market.settlement_reference_valid == 0
        || market.close_monotonic_ns <= now_ns) {
        return finish(NativeCryptoDecisionReason::MarketUnavailable);
    }
    out.tte_ns = market.close_monotonic_ns - now_ns;
    if (out.tte_ns < policy_.minimum_tte_ns || out.tte_ns > policy_.maximum_tte_ns) {
        return finish(NativeCryptoDecisionReason::TteOutsideWindow);
    }
    if (policy_.one_entry_per_market != 0 && market_traded(market.market_handle)) {
        return finish(NativeCryptoDecisionReason::MarketAlreadyTraded);
    }
    if (seen_signal(market.market_handle, input.signal.signal_version)) {
        return finish(NativeCryptoDecisionReason::DuplicateSignal);
    }
    bool choose_yes = input.signal.direction > 0;
    std::int64_t selected_quantity = policy_.target_quantity_microunits;
    if (policy_.probability_ev_enabled != 0) {
        const auto price_quote = [&](bool up) noexcept {
            const auto& b = up ? input.yes_book : input.no_book;
            const auto& instrument = up ? market.yes : market.no;
            ProbabilityEvQuote q;
            q.is_up = up ? 1 : 0;
            q.ask_e4 = b.best_ask_e4;
            q.visible_quantity_microunits = b.best_ask_microunits;
            q.minimum_quantity_microunits = instrument.min_order_microunits;
            q.fee_rate = input.taker_fee_rate;
            q.fee_exponent = input.taker_fee_exponent;
            q.execution_reserve_per_share = input.execution_reserve_per_share;
            if (!valid_book(b, now_ns, policy_.maximum_book_age_ns)
                || b.tick_size_e4 <= 0 || b.best_ask_e4 % b.tick_size_e4 != 0
                || b.best_ask_e4 > policy_.maximum_entry_price_e4
                || instrument.instrument_handle == 0
                || instrument.instrument_is_yes != static_cast<std::uint8_t>(up)) q.ask_e4 = 0;
            return q;
        };
        const auto yes = evaluate_probability_ev(input.probability, price_quote(true), input.risk_sizing, now_ns);
        const auto no = evaluate_probability_ev(input.probability, price_quote(false), input.risk_sizing, now_ns);
        choose_yes = yes.accepted && (!no.accepted || yes.conservative_net_edge >= no.conservative_net_edge);
        out.economics = choose_yes ? yes : no;
        if (!yes.accepted && !no.accepted) {
            // Preserve the most informative quote diagnosis; never fall back to direction-only.
            out.economics = std::isfinite(yes.conservative_net_edge)
                && (!std::isfinite(no.conservative_net_edge) || yes.conservative_net_edge >= no.conservative_net_edge)
                ? yes : no;
            if (out.economics.reason == ProbabilityEvReason::NonPositiveNetEdge)
                return finish(NativeCryptoDecisionReason::NetEdgeNonPositive);
            if (out.economics.reason == ProbabilityEvReason::BelowVenueMinimum)
                return finish(NativeCryptoDecisionReason::RiskSizeBelowMinimum);
            return finish(NativeCryptoDecisionReason::ProbabilityUnavailable);
        }
        selected_quantity = out.economics.quantity_microunits;
    }
    const auto& instrument = choose_yes ? market.yes : market.no;
    const auto& book = choose_yes ? input.yes_book : input.no_book;
    out.selected_yes = choose_yes ? 1 : 0;
    out.selected_instrument_handle = instrument.instrument_handle;
    if (instrument.instrument_handle == 0
        || instrument.instrument_is_yes != static_cast<std::uint8_t>(choose_yes)
        || instrument.min_order_microunits <= 0
        || !valid_book(book, now_ns, policy_.maximum_book_age_ns)) {
        return finish(NativeCryptoDecisionReason::InvalidBook);
    }
    if (policy_.maximum_entry_price_e4 <= 0
        || policy_.maximum_entry_price_e4 > kCanonicalPriceScale
        || book.best_ask_e4 > policy_.maximum_entry_price_e4) {
        return finish(NativeCryptoDecisionReason::EntryPriceTooHigh);
    }
    if (selected_quantity <= 0
        || instrument.min_order_microunits > selected_quantity
        || (policy_.require_full_visible_depth != 0
            && book.best_ask_microunits < selected_quantity)) {
        return finish(NativeCryptoDecisionReason::InsufficientDepth);
    }
    if (book.best_ask_e4 % book.tick_size_e4 != 0) {
        return finish(NativeCryptoDecisionReason::InvalidTick);
    }
    const auto price_tick = book.best_ask_e4 / book.tick_size_e4;
    if (price_tick <= 0) return finish(NativeCryptoDecisionReason::InvalidTick);

    remember_signal(market.market_handle, input.signal.signal_version);
    auto intent_id = next_intent_id_++;
    if (next_intent_id_ == 0) next_intent_id_ = 1;
    StrategyIntent intent;
    intent.intent_id = intent_id;
    intent.market_handle = market.market_handle;
    intent.event_handle = market.event_handle;
    intent.instrument_handle = instrument.instrument_handle;
    intent.state_version = book.state_version;
    intent.model_version = input.model_version;
    intent.policy_version = input.policy_version;
    intent.causal_trigger_receive_monotonic_ns = input.signal.trigger_receive_monotonic_ns;
    intent.signal_ready_monotonic_ns = now_ns;
    intent.decision_monotonic_ns = now_ns;
    intent.exchange_event_ns = book.exchange_event_ns;
    intent.price_tick = price_tick;
    intent.quantity_microunits = selected_quantity;
    intent.horizon_ms = 300'000;
    intent.strategy_id = StrategyId::CryptoInformedTaker;
    intent.type = IntentType::TargetPosition;
    intent.side = Side::Buy;
    intent.urgency = Urgency::Aggressive;
    intent.purpose = IntentPurpose::Alpha;
    intent.passive = 0;
    intent.post_only = 0;
    out.intent = intent;
    out.accepted = 1;
    return finish(NativeCryptoDecisionReason::Accepted);
}

NativeCryptoDecisionResult NativeCryptoDecisionLane::evaluate(
    const NativeCryptoDecisionInput& input,
    SleeveCapitalAccount& capital) noexcept {
    auto out = construct_candidate(input);
    if (out.accepted == 0) return out;
    const auto& book = out.selected_yes != 0 ? input.yes_book : input.no_book;
    out.admission = ExecutionAdmission::admit(out.intent, book.tick_size_e4, capital);
    if (out.admission.accepted == 0) {
        out.accepted = 0;
        out.reason = out.admission.reason == ExecutionAdmissionReason::CapitalDenied
            ? NativeCryptoDecisionReason::CapitalDenied
            : NativeCryptoDecisionReason::InvalidTick;
    }
    return out;
}

} // namespace pm::v7
