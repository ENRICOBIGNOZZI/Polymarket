#include "pm/v7_latency_arb.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>

namespace pm::v7 {
namespace {

constexpr double kPriceScale = 10'000.0;

[[nodiscard]] std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] bool valid_book(const BookHotSnapshot& book,
                              std::int64_t now,
                              std::int64_t maximum_age_ns) noexcept {
    return book.valid != 0 && book.lineage_continuous != 0
        && book.state_version > 0 && book.tick_size_e4 > 0
        && book.best_bid_e4 > 0 && book.best_ask_e4 > book.best_bid_e4
        && book.best_ask_e4 < 10'000
        && book.best_bid_microunits > 0 && book.best_ask_microunits > 0
        && book.receive_monotonic_ns > 0 && book.receive_monotonic_ns <= now
        && now - book.receive_monotonic_ns <= maximum_age_ns;
}

[[nodiscard]] double fee_per_share(double price,
                                   double rate,
                                   double exponent) noexcept {
    if (!std::isfinite(price) || price <= 0.0 || price >= 1.0
        || !std::isfinite(rate) || rate < 0.0 || rate > 1.0
        || !std::isfinite(exponent) || exponent < 0.0) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    return rate == 0.0 ? 0.0
        : rate * std::pow(price * (1.0 - price), exponent);
}

} // namespace

LatencyArbLane::LatencyArbLane(LatencyArbPolicy policy) noexcept
    : policy_(policy) {}

std::int32_t LatencyArbLane::target_exit_price(
    std::int32_t entry_e4, std::int32_t tick_e4,
    double fee_rate, double fee_exponent) const noexcept {
    if (entry_e4 <= 0 || entry_e4 >= 10'000 || tick_e4 <= 0
        || entry_e4 % tick_e4 != 0 || policy_.minimum_profit_ticks < 0) return 0;
    const double entry_price = static_cast<double>(entry_e4) / kPriceScale;
    const double entry_fee = fee_per_share(entry_price, fee_rate, fee_exponent);
    if (!std::isfinite(entry_fee)) return 0;
    const double required_profit =
        static_cast<double>(policy_.minimum_profit_ticks * tick_e4) / kPriceScale;
    for (std::int32_t target = entry_e4 + tick_e4;
         target > 0 && target < 10'000;
         target += tick_e4) {
        const double exit_price = static_cast<double>(target) / kPriceScale;
        const double exit_fee = fee_per_share(exit_price, fee_rate, fee_exponent);
        if (!std::isfinite(exit_fee)) return 0;
        const double net = exit_price - exit_fee - entry_price - entry_fee;
        if (net + 1e-12 >= required_profit) return target;
        if (target > 10'000 - tick_e4) break;
    }
    return 0;
}

LatencyArbDecision LatencyArbLane::construct_candidate(
    const LatencyArbInput& input) noexcept {
    const auto started = now_ns();
    LatencyArbDecision out;
    const auto finish = [&](LatencyArbReason reason) noexcept {
        out.reason = reason;
        out.decision_compute_ns = std::max<std::int64_t>(0, now_ns() - started);
        return out;
    };

    if (pending_client_order_id_ != 0) return finish(LatencyArbReason::PendingOrder);
    const auto now = input.now_monotonic_ns;
    if (now <= 0) return finish(LatencyArbReason::InvalidSignal);

    const auto& market = input.market;
    if (market.market_handle == 0 || market.event_handle == 0
        || market.accepting_orders == 0 || market.closed != 0
        || market.contract_verified == 0 || market.settlement_reference_valid == 0
        || market.close_monotonic_ns <= now) {
        return finish(LatencyArbReason::MarketUnavailable);
    }
    const auto tte = market.close_monotonic_ns - now;
    if (tte < policy_.minimum_tte_ns || tte > policy_.maximum_tte_ns) {
        return finish(LatencyArbReason::MarketUnavailable);
    }

    if (position_microunits_ > 0) {
        const bool yes = position_instrument_ == market.yes.instrument_handle;
        if (!yes && position_instrument_ != market.no.instrument_handle) {
            return finish(LatencyArbReason::InvalidBook);
        }
        const auto& book = yes ? input.yes_book : input.no_book;
        if (!valid_book(book, now, policy_.maximum_book_age_ns)
            || book.tick_size_e4 != position_tick_e4_
            || book.best_bid_e4 % book.tick_size_e4 != 0) {
            return finish(LatencyArbReason::InvalidBook);
        }

        const bool converged = target_exit_e4_ > 0
            && book.best_bid_e4 >= target_exit_e4_;
        const bool reversal = input.signal.valid != 0
            && input.signal.signal_version > entry_signal_version_
            && input.signal.causal_trigger_receive_monotonic_ns > entry_trigger_ns_
            && input.signal.direction == -entry_direction_;
        const bool timed_out = entry_monotonic_ns_ > 0
            && now - entry_monotonic_ns_ >= policy_.hard_timeout_ns;
        if (!converged && !reversal && !timed_out) {
            return finish(LatencyArbReason::ExitNotReady);
        }

        const auto& instrument = yes ? market.yes : market.no;
        const auto quantity = std::min(position_microunits_, book.best_bid_microunits);
        if (quantity < instrument.min_order_microunits) {
            return finish(LatencyArbReason::InsufficientDepth);
        }
        auto id = next_intent_id_++;
        if (next_intent_id_ == 0) next_intent_id_ = 1;
        StrategyIntent intent;
        intent.intent_id = id;
        intent.market_handle = market.market_handle;
        intent.event_handle = market.event_handle;
        intent.instrument_handle = position_instrument_;
        intent.state_version = book.state_version;
        intent.model_version = 1;
        intent.policy_version = 1;
        intent.causal_trigger_receive_monotonic_ns =
            reversal ? input.signal.causal_trigger_receive_monotonic_ns
                     : book.receive_monotonic_ns;
        intent.signal_ready_monotonic_ns = now;
        intent.decision_monotonic_ns = now;
        intent.exchange_event_ns = book.exchange_event_ns;
        intent.price_tick = book.best_bid_e4 / book.tick_size_e4;
        intent.quantity_microunits = quantity;
        intent.horizon_ms = static_cast<std::uint32_t>(
            std::clamp<std::int64_t>(policy_.hard_timeout_ns / 1'000'000LL, 1, 60'000));
        intent.strategy_id = StrategyId::CryptoLatencyArb;
        intent.type = IntentType::TargetPosition;
        intent.side = Side::Sell;
        intent.urgency = (reversal || timed_out) ? Urgency::Critical : Urgency::Aggressive;
        intent.purpose = (reversal || timed_out)
            ? IntentPurpose::Risk : IntentPurpose::InventoryReduction;
        intent.passive = 0;
        intent.post_only = 0;
        const double entry = static_cast<double>(entry_price_e4_) / kPriceScale;
        const double exit = static_cast<double>(book.best_bid_e4) / kPriceScale;
        const double entry_fee = fee_per_share(entry, input.taker_fee_rate, input.taker_fee_exponent);
        const double exit_fee = fee_per_share(exit, input.taker_fee_rate, input.taker_fee_exponent);
        if (std::isfinite(entry_fee) && std::isfinite(exit_fee)) {
            intent.expected_edge = exit - exit_fee - entry - entry_fee;
            intent.expected_cost = entry + entry_fee;
            intent.expected_ev = intent.expected_edge;
        }
        out.intent = intent;
        out.action = LatencyArbAction::Exit;
        out.selected_instrument_handle = position_instrument_;
        out.entry_price_e4 = entry_price_e4_;
        out.target_exit_e4 = target_exit_e4_;
        out.direction = entry_direction_;
        out.accepted = 1;
        return finish(converged ? LatencyArbReason::ExitConverged
             : reversal ? LatencyArbReason::ExitReversal
                        : LatencyArbReason::ExitTimeout);
    }

    const auto& signal = input.signal;
    out.signal_version = signal.signal_version;
    if (signal.signal_version == 0
        || signal.causal_trigger_receive_monotonic_ns <= 0
        || signal.causal_trigger_receive_monotonic_ns > now
        || signal.confirmed_non_opposing == 0
        || signal.valid == 0
        || (signal.direction != 1 && signal.direction != -1)) {
        return finish(LatencyArbReason::InvalidSignal);
    }
    if (now - signal.causal_trigger_receive_monotonic_ns > policy_.maximum_signal_age_ns
        || (signal.valid_until_monotonic_ns > 0 && now > signal.valid_until_monotonic_ns)) {
        return finish(LatencyArbReason::ExpiredSignal);
    }
    if (!std::isfinite(signal.binance_return_100ms_bp)
        || std::abs(signal.binance_return_100ms_bp) + 1e-12
            < policy_.minimum_absolute_binance_return_bp) {
        return finish(LatencyArbReason::WeakSignal);
    }
    if (signal.signal_version <= last_entry_signal_version_) {
        return finish(LatencyArbReason::DuplicateSignal);
    }

    const bool yes = signal.direction > 0;
    const auto& instrument = yes ? market.yes : market.no;
    const auto& book = yes ? input.yes_book : input.no_book;
    if (instrument.instrument_handle == 0
        || instrument.instrument_is_yes != static_cast<std::uint8_t>(yes)
        || instrument.min_order_microunits <= 0
        || !valid_book(book, now, policy_.maximum_book_age_ns)) {
        return finish(LatencyArbReason::InvalidBook);
    }
    if (book.receive_monotonic_ns >= signal.causal_trigger_receive_monotonic_ns) {
        return finish(LatencyArbReason::BookAlreadyRepriced);
    }
    if (book.best_bid_e4 % book.tick_size_e4 != 0
        || book.best_ask_e4 % book.tick_size_e4 != 0) {
        return finish(LatencyArbReason::InvalidTick);
    }
    const auto spread_e4 = book.best_ask_e4 - book.best_bid_e4;
    if (spread_e4 <= 0
        || spread_e4 > static_cast<std::int64_t>(policy_.maximum_spread_ticks)
                         * book.tick_size_e4) {
        return finish(LatencyArbReason::SpreadTooWide);
    }
    if (book.best_ask_e4 > policy_.maximum_entry_price_e4) {
        return finish(LatencyArbReason::EntryPriceTooHigh);
    }
    if (policy_.target_quantity_microunits < instrument.min_order_microunits
        || (policy_.require_full_visible_depth != 0
            && book.best_ask_microunits < policy_.target_quantity_microunits)) {
        return finish(LatencyArbReason::InsufficientDepth);
    }
    const auto target = target_exit_price(
        book.best_ask_e4, book.tick_size_e4,
        input.taker_fee_rate, input.taker_fee_exponent);
    if (target <= 0) return finish(LatencyArbReason::InvalidFee);

    auto id = next_intent_id_++;
    if (next_intent_id_ == 0) next_intent_id_ = 1;
    StrategyIntent intent;
    intent.intent_id = id;
    intent.market_handle = market.market_handle;
    intent.event_handle = market.event_handle;
    intent.instrument_handle = instrument.instrument_handle;
    intent.state_version = book.state_version;
    intent.model_version = 1;
    intent.policy_version = 1;
    intent.causal_trigger_receive_monotonic_ns = signal.causal_trigger_receive_monotonic_ns;
    intent.signal_ready_monotonic_ns = signal.evaluated_receive_monotonic_ns;
    intent.decision_monotonic_ns = now;
    intent.exchange_event_ns = book.exchange_event_ns;
    intent.price_tick = book.best_ask_e4 / book.tick_size_e4;
    intent.quantity_microunits = policy_.target_quantity_microunits;
    intent.horizon_ms = static_cast<std::uint32_t>(
        std::clamp<std::int64_t>(policy_.hard_timeout_ns / 1'000'000LL, 1, 60'000));
    intent.strategy_id = StrategyId::CryptoLatencyArb;
    intent.type = IntentType::TargetPosition;
    intent.side = Side::Buy;
    intent.urgency = Urgency::Aggressive;
    intent.purpose = IntentPurpose::Alpha;
    intent.passive = 0;
    intent.post_only = 0;
    const double entry = static_cast<double>(book.best_ask_e4) / kPriceScale;
    const double target_price = static_cast<double>(target) / kPriceScale;
    const double entry_fee = fee_per_share(entry, input.taker_fee_rate, input.taker_fee_exponent);
    const double exit_fee = fee_per_share(target_price, input.taker_fee_rate, input.taker_fee_exponent);
    if (std::isfinite(entry_fee) && std::isfinite(exit_fee)) {
        intent.expected_edge = target_price - exit_fee - entry - entry_fee;
        intent.expected_cost = entry + entry_fee;
        intent.expected_ev = intent.expected_edge;
    }

    out.intent = intent;
    out.reason = LatencyArbReason::Enter;
    out.action = LatencyArbAction::Enter;
    out.signal_version = signal.signal_version;
    out.selected_instrument_handle = instrument.instrument_handle;
    out.entry_price_e4 = book.best_ask_e4;
    out.target_exit_e4 = target;
    out.direction = signal.direction;
    out.accepted = 1;
    return finish(LatencyArbReason::Enter);
}

void LatencyArbLane::on_submitted(
    std::uint64_t client_order_id,
    const LatencyArbDecision& decision) noexcept {
    if (client_order_id == 0 || decision.accepted == 0
        || decision.action == LatencyArbAction::None) return;
    pending_client_order_id_ = client_order_id;
    pending_action_ = decision.action;
    pending_instrument_ = decision.selected_instrument_handle;
    pending_entry_price_e4_ = decision.entry_price_e4;
    pending_target_exit_e4_ = decision.target_exit_e4;
    pending_tick_e4_ = decision.intent.price_tick > 0 && decision.entry_price_e4 > 0
        ? decision.entry_price_e4 / decision.intent.price_tick : 0;
    pending_trigger_ns_ = decision.intent.causal_trigger_receive_monotonic_ns;
    pending_signal_version_ = decision.signal_version;
    pending_direction_ = decision.direction;
    if (decision.action == LatencyArbAction::Enter) {
        last_entry_signal_version_ = std::max(last_entry_signal_version_, decision.signal_version);
    }
}

void LatencyArbLane::on_fill(
    std::uint64_t client_order_id,
    std::uint64_t instrument_handle,
    Side side,
    std::int64_t fill_microunits,
    std::int32_t fill_price_e4,
    std::int64_t fill_monotonic_ns) noexcept {
    if (!owns_order(client_order_id) || instrument_handle != pending_instrument_
        || fill_microunits <= 0 || fill_price_e4 <= 0 || fill_price_e4 >= 10'000) return;
    if (pending_action_ == LatencyArbAction::Enter && side == Side::Buy) {
        if (position_microunits_ == 0) {
            position_instrument_ = instrument_handle;
            position_microunits_ = fill_microunits;
            entry_price_e4_ = fill_price_e4;
            target_exit_e4_ = std::max(fill_price_e4, pending_target_exit_e4_);
            position_tick_e4_ = pending_tick_e4_;
            entry_monotonic_ns_ = fill_monotonic_ns;
            entry_trigger_ns_ = pending_trigger_ns_;
            entry_signal_version_ = pending_signal_version_;
            entry_direction_ = pending_direction_;
        }
    } else if (pending_action_ == LatencyArbAction::Exit && side == Side::Sell
               && position_instrument_ == instrument_handle) {
        position_microunits_ = std::max<std::int64_t>(0, position_microunits_ - fill_microunits);
        if (position_microunits_ == 0) {
            position_instrument_ = 0;
            entry_price_e4_ = 0;
            target_exit_e4_ = 0;
            position_tick_e4_ = 0;
            entry_monotonic_ns_ = 0;
            entry_trigger_ns_ = 0;
            entry_signal_version_ = 0;
            entry_direction_ = 0;
        }
    }
}

void LatencyArbLane::on_terminal(std::uint64_t client_order_id) noexcept {
    if (!owns_order(client_order_id)) return;
    pending_client_order_id_ = 0;
    pending_action_ = LatencyArbAction::None;
    pending_instrument_ = 0;
    pending_entry_price_e4_ = 0;
    pending_target_exit_e4_ = 0;
    pending_tick_e4_ = 0;
    pending_trigger_ns_ = 0;
    pending_signal_version_ = 0;
    pending_direction_ = 0;
}

} // namespace pm::v7
