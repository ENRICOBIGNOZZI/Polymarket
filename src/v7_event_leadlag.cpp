#include "pm/v7_event_leadlag.hpp"

#include <algorithm>
#include <cmath>

namespace pm::v7::leadlag {
namespace {
constexpr double kBp = 10'000.0;
[[nodiscard]] bool finite_positive(double x) noexcept {
    return std::isfinite(x) && x > 0.0;
}
}

bool EventLeadLagEngine::PriceRing::push(PriceSample sample) noexcept {
    if (count >= samples.size()) return false;
    samples[(head + count) % samples.size()] = sample;
    ++count;
    return true;
}

void EventLeadLagEngine::PriceRing::advance_prior(std::int64_t target_ns) noexcept {
    while (count >= 2) {
        const auto next = (head + 1) % samples.size();
        if (samples[next].receive_ns > target_ns) break;
        head = next;
        --count;
    }
}

const EventLeadLagEngine::PriceSample*
EventLeadLagEngine::PriceRing::prior_at_or_before(std::int64_t target_ns) noexcept {
    advance_prior(target_ns);
    if (count == 0 || samples[head].receive_ns > target_ns) return nullptr;
    return &samples[head];
}

const EventLeadLagEngine::PriceSample* EventLeadLagEngine::PriceRing::latest() const noexcept {
    if (count == 0) return nullptr;
    return &samples[(head + count - 1) % samples.size()];
}

EventLeadLagEngine::EventLeadLagEngine(EventLeadLagPolicy policy) noexcept
    : policy_(policy) {}

bool EventLeadLagEngine::policy_valid() const noexcept {
    return policy_.shock_window_ns > 0
        && policy_.cooldown_ns >= 0
        && policy_.warmup_ns >= policy_.shock_window_ns
        && policy_.signal_ttl_ns > 0
        && std::isfinite(policy_.minimum_absolute_binance_return_bp)
        && policy_.minimum_absolute_binance_return_bp > 0.0;
}

bool EventLeadLagEngine::accept_global_time(std::int64_t receive_ns) noexcept {
    if (receive_ns <= 0) return false;
    if (last_global_receive_ns_ > receive_ns) {
        ++metrics_.out_of_order_events;
        const auto high_watermark_ns = last_global_receive_ns_;
        fail_closed_recovery(high_watermark_ns);
        return false;
    }
    last_global_receive_ns_ = receive_ns;
    return true;
}

void EventLeadLagEngine::fail_closed_recovery(std::int64_t receive_ns) noexcept {
    binance_.clear();
    coinbase_.clear();
    signal_ = {};
    first_binance_ns_ = 0;
    first_coinbase_ns_ = 0;
    last_trigger_ns_ = 0;
    last_global_receive_ns_ = std::max<std::int64_t>(0, receive_ns);
}

void EventLeadLagEngine::reset() noexcept {
    const auto metrics = metrics_;
    *this = EventLeadLagEngine(policy_);
    metrics_ = metrics;
}

bool EventLeadLagEngine::on_coinbase_mid(std::int64_t receive_monotonic_ns,
                                         double mid) noexcept {
    ++metrics_.coinbase_events;
    if (!policy_valid() || !finite_positive(mid) || !accept_global_time(receive_monotonic_ns)) {
        ++metrics_.invalid_events;
        return false;
    }
    coinbase_.advance_prior(receive_monotonic_ns - policy_.shock_window_ns);
    if (!coinbase_.push({receive_monotonic_ns, mid})) {
        ++metrics_.history_overflows;
        fail_closed_recovery(receive_monotonic_ns);
        return false;
    }
    if (first_coinbase_ns_ == 0) first_coinbase_ns_ = receive_monotonic_ns;
    return true;
}

EventLeadLagSignal EventLeadLagEngine::on_binance_trade(
    std::int64_t receive_monotonic_ns, double trade_price) noexcept {
    ++metrics_.binance_events;
    if (!policy_valid() || !finite_positive(trade_price)
        || !accept_global_time(receive_monotonic_ns)) {
        ++metrics_.invalid_events;
        return snapshot(receive_monotonic_ns);
    }

    const auto target_ns = receive_monotonic_ns - policy_.shock_window_ns;
    binance_.advance_prior(target_ns);
    if (!binance_.push({receive_monotonic_ns, trade_price})) {
        ++metrics_.history_overflows;
        fail_closed_recovery(receive_monotonic_ns);
        return {};
    }
    if (first_binance_ns_ == 0) first_binance_ns_ = receive_monotonic_ns;
    ++metrics_.evaluations;

    if (first_coinbase_ns_ == 0) {
        ++metrics_.warmup_or_history_rejects;
        return snapshot(receive_monotonic_ns);
    }
    const auto start_ns = std::max(first_binance_ns_, first_coinbase_ns_);
    if (receive_monotonic_ns < start_ns + policy_.warmup_ns) {
        ++metrics_.warmup_or_history_rejects;
        return snapshot(receive_monotonic_ns);
    }

    const auto* binance_prior = binance_.prior_at_or_before(target_ns);
    const auto* coinbase_prior = coinbase_.prior_at_or_before(target_ns);
    const auto* coinbase_current = coinbase_.latest();
    if (binance_prior == nullptr || coinbase_prior == nullptr || coinbase_current == nullptr
        || !finite_positive(binance_prior->price) || !finite_positive(coinbase_prior->price)
        || !finite_positive(coinbase_current->price)
        || coinbase_current->receive_ns > receive_monotonic_ns) {
        ++metrics_.warmup_or_history_rejects;
        return snapshot(receive_monotonic_ns);
    }

    const double binance_bp = kBp * std::log(trade_price / binance_prior->price);
    const double coinbase_bp = kBp * std::log(coinbase_current->price / coinbase_prior->price);
    if (!std::isfinite(binance_bp) || !std::isfinite(coinbase_bp)) {
        ++metrics_.invalid_events;
        return snapshot(receive_monotonic_ns);
    }
    if (std::abs(binance_bp) + 1e-12 < policy_.minimum_absolute_binance_return_bp) {
        ++metrics_.weak_shock_rejects;
        return snapshot(receive_monotonic_ns);
    }
    const bool non_opposing = coinbase_bp == 0.0 || binance_bp * coinbase_bp > 0.0;
    if (!non_opposing) {
        ++metrics_.opposing_confirmation_rejects;
        return snapshot(receive_monotonic_ns);
    }
    if (last_trigger_ns_ != 0
        && receive_monotonic_ns - last_trigger_ns_ < policy_.cooldown_ns) {
        ++metrics_.cooldown_rejects;
        return snapshot(receive_monotonic_ns);
    }

    last_trigger_ns_ = receive_monotonic_ns;
    ++signal_version_counter_;
    if (signal_version_counter_ == 0) ++signal_version_counter_;
    signal_.signal_version = signal_version_counter_;
    signal_.causal_trigger_receive_monotonic_ns = receive_monotonic_ns;
    signal_.evaluated_receive_monotonic_ns = receive_monotonic_ns;
    signal_.valid_until_monotonic_ns = receive_monotonic_ns + policy_.signal_ttl_ns;
    signal_.binance_prior_receive_monotonic_ns = binance_prior->receive_ns;
    signal_.coinbase_current_receive_monotonic_ns = coinbase_current->receive_ns;
    signal_.coinbase_prior_receive_monotonic_ns = coinbase_prior->receive_ns;
    signal_.binance_return_100ms_bp = binance_bp;
    signal_.coinbase_return_100ms_bp = coinbase_bp;
    signal_.direction = binance_bp > 0.0 ? 1 : -1;
    signal_.confirmed_non_opposing = 1;
    signal_.valid = 1;
    ++metrics_.triggers;
    return signal_;
}

EventLeadLagSignal EventLeadLagEngine::snapshot(std::int64_t now_monotonic_ns) const noexcept {
    auto out = signal_;
    out.valid = out.signal_version > 0
        && now_monotonic_ns >= out.causal_trigger_receive_monotonic_ns
        && now_monotonic_ns <= out.valid_until_monotonic_ns ? 1 : 0;
    return out;
}

} // namespace pm::v7::leadlag
