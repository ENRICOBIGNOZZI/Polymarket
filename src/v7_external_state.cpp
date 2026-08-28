#include "pm/v7_external_state.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace pm::v7::external_fair {
namespace {

constexpr double kEps = 1e-12;

[[nodiscard]] bool finite(double value) noexcept { return std::isfinite(value); }

[[nodiscard]] double clamp(double value, double lo, double hi) noexcept {
    return std::max(lo, std::min(hi, value));
}

[[nodiscard]] double ew_update(double previous, double observation,
                               double alpha) noexcept {
    const double a = clamp(alpha, 0.0, 1.0);
    return (1.0 - a) * previous + a * observation;
}

} // namespace

bool OracleState::on_event(const OracleEvent& event) noexcept {
    if (event.feed_handle == 0 || event.connection_epoch == 0
        || event.local_receive_monotonic_ns <= 0
        || !finite(event.value_numeric) || event.value_numeric <= 0.0
        || event.window_seconds == 0) {
        return false;
    }
    if (previous_epoch_ == event.connection_epoch && event.source_sequence > 0
        && previous_sequence_ > 0 && event.source_sequence <= previous_sequence_) {
        return false;
    }

    const bool epoch_changed = previous_epoch_ != 0 && previous_epoch_ != event.connection_epoch;
    const bool continuity_break = epoch_changed || event.reconnect != 0 || event.gap != 0;
    if (continuity_break) {
        continuity_ = OracleContinuity::ContinuityUnknown;
        consecutive_clean_ = 0;
    }

    if (event.same_oracle_recovery != 0 && event.healthy != 0 && event.gap == 0) {
        continuity_ = OracleContinuity::RecoveredSameOracleSnapshot;
        consecutive_clean_ = 1;
    } else if (event.healthy != 0 && event.gap == 0) {
        if (previous_epoch_ == event.connection_epoch && !continuity_break) {
            consecutive_clean_ = static_cast<std::uint8_t>(
                std::min<int>(255, static_cast<int>(consecutive_clean_) + 1));
        } else if (previous_epoch_ == 0) {
            consecutive_clean_ = 1;
        }
        if (consecutive_clean_ >= 2) continuity_ = OracleContinuity::LiveContinuous;
    } else {
        continuity_ = OracleContinuity::ContinuityUnknown;
        consecutive_clean_ = 0;
    }

    last_ = event;
    previous_epoch_ = event.connection_epoch;
    previous_sequence_ = event.source_sequence;
    ++state_version_;
    return true;
}

OracleSnapshot OracleState::snapshot(std::int64_t now_ns,
                                     std::int64_t max_age_ns) const noexcept {
    OracleSnapshot out;
    out.state_version = state_version_;
    out.feed_handle = last_.feed_handle;
    out.connection_epoch = last_.connection_epoch;
    out.exact_decimal_handle = last_.exact_decimal_handle;
    out.value_numeric = last_.value_numeric;
    out.oracle_observation_ns = last_.oracle_observation_ns;
    out.receive_monotonic_ns = last_.local_receive_monotonic_ns;
    out.window_seconds = last_.window_seconds;
    out.continuity = continuity_;
    out.healthy = last_.healthy;
    out.gap = last_.gap;
    if (now_ns <= 0 || last_.local_receive_monotonic_ns <= 0
        || last_.local_receive_monotonic_ns > now_ns) {
        return out;
    }
    out.age_ns = now_ns - last_.local_receive_monotonic_ns;
    out.valid = state_version_ > 0
        && last_.healthy != 0
        && last_.gap == 0
        && continuity_ != OracleContinuity::ContinuityUnknown
        && out.age_ns >= 0
        && max_age_ns > 0
        && out.age_ns <= max_age_ns ? 1 : 0;
    return out;
}

ExternalAssetState::ExternalAssetState(std::uint64_t asset_handle) noexcept
    : asset_handle_(asset_handle) {}

std::size_t ExternalAssetState::venue_index(VenueId venue) noexcept {
    switch (venue) {
        case VenueId::BinanceSpot: return 0;
        case VenueId::CoinbaseSpot: return 1;
        case VenueId::BybitSpot: return 2;
        case VenueId::Unknown:
        default: return kVenueCount;
    }
}

bool ExternalAssetState::on_venue_event(const ExternalVenueEvent& event,
                                        const ExternalStatePolicy& policy) noexcept {
    const std::size_t index = venue_index(event.venue);
    if (index >= venues_.size() || event.asset_handle == 0
        || (asset_handle_ != 0 && event.asset_handle != asset_handle_)
        || event.connection_epoch == 0 || event.local_receive_monotonic_ns <= 0) {
        return false;
    }
    if (asset_handle_ == 0) asset_handle_ = event.asset_handle;
    auto& venue = venues_[index];
    if (venue.connection_epoch == event.connection_epoch && event.source_sequence > 0
        && venue.source_sequence > 0 && event.source_sequence <= venue.source_sequence) {
        return false;
    }

    const bool epoch_changed = venue.connection_epoch != 0
        && venue.connection_epoch != event.connection_epoch;
    if (epoch_changed || event.gap != 0) venue.valid = 0;
    venue.connection_epoch = event.connection_epoch;
    venue.source_sequence = event.source_sequence;
    venue.last_receive_ns = event.local_receive_monotonic_ns;
    venue.healthy = event.healthy;
    venue.gap = event.gap;

    if (event.event_type == ExternalEventType::BookTop) {
        if (!finite(event.bid) || !finite(event.ask) || !finite(event.bid_size)
            || !finite(event.ask_size) || event.bid <= 0.0 || event.ask <= event.bid
            || event.bid_size < 0.0 || event.ask_size < 0.0) {
            venue.valid = 0;
            ++state_version_;
            return false;
        }
        const double previous_bid_size = venue.bid_size;
        const double previous_ask_size = venue.ask_size;
        venue.previous_bid_size = previous_bid_size;
        venue.previous_ask_size = previous_ask_size;
        venue.bid = event.bid;
        venue.ask = event.ask;
        venue.bid_size = event.bid_size;
        venue.ask_size = event.ask_size;
        venue.mid = 0.5 * (event.bid + event.ask);
        const double total_depth = event.bid_size + event.ask_size;
        venue.microprice = total_depth > kEps
            ? (event.ask * event.bid_size + event.bid * event.ask_size) / total_depth
            : venue.mid;
        const double raw_ofi = (event.bid_size - previous_bid_size)
            - (event.ask_size - previous_ask_size);
        venue.ofi = ew_update(venue.ofi, raw_ofi, policy.flow_alpha);
        venue.valid = event.healthy != 0 && event.gap == 0 ? 1 : 0;
    } else if (event.event_type == ExternalEventType::Trade) {
        if (finite(event.trade_size) && event.trade_size >= 0.0
            && (event.trade_side == 1 || event.trade_side == -1)) {
            const double signed_flow = static_cast<double>(event.trade_side) * event.trade_size;
            venue.signed_trade_flow = ew_update(
                venue.signed_trade_flow, signed_flow, policy.flow_alpha);
        }
    } else if (event.event_type == ExternalEventType::Health) {
        if (event.healthy == 0 || event.gap != 0) venue.valid = 0;
    }

    latest_receive_ns_ = std::max(latest_receive_ns_, event.local_receive_monotonic_ns);
    ++state_version_;

    double micro = 0.0;
    double dispersion = 0.0;
    std::uint32_t health_mask = 0;
    std::uint32_t fresh_count = 0;
    const double composite = compute_composite(event.local_receive_monotonic_ns, policy,
                                               &micro, &dispersion,
                                               &health_mask, &fresh_count);
    if (composite > 0.0 && fresh_count >= policy.min_healthy_venues) {
        if (last_composite_ > 0.0) {
            const double r = std::log(composite / last_composite_);
            const double r2 = r * r;
            ew_var_fast_ = ew_update(ew_var_fast_, r2, policy.vol_fast_alpha);
            ew_var_medium_ = ew_update(ew_var_medium_, r2, policy.vol_medium_alpha);
            ew_var_slow_ = ew_update(ew_var_slow_, r2, policy.vol_slow_alpha);
        }
        last_composite_ = composite;
        record_price_sample(event.local_receive_monotonic_ns, composite);
    }

    double ofi_sum = 0.0;
    double trade_sum = 0.0;
    double weight_sum = 0.0;
    for (std::size_t i = 0; i < venues_.size(); ++i) {
        const auto& row = venues_[i];
        if (row.valid == 0) continue;
        const double weight = finite(policy.venue_weights[i])
            ? std::max(0.0, policy.venue_weights[i]) : 0.0;
        ofi_sum += weight * row.ofi;
        trade_sum += weight * row.signed_trade_flow;
        weight_sum += weight;
    }
    if (weight_sum > kEps) {
        aggregate_ofi_ = ofi_sum / weight_sum;
        aggregate_trade_imbalance_ = trade_sum / weight_sum;
    }
    return true;
}

void ExternalAssetState::on_oracle_snapshot(const OracleSnapshot& oracle) noexcept {
    oracle_ = oracle;
    ++state_version_;
}

double ExternalAssetState::compute_composite(
    std::int64_t now_ns,
    const ExternalStatePolicy& policy,
    double* microprice,
    double* dispersion_bps,
    std::uint32_t* health_mask,
    std::uint32_t* fresh_count) const noexcept {

    double log_sum = 0.0;
    double micro_sum = 0.0;
    double weight_sum = 0.0;
    std::uint32_t mask = 0;
    std::uint32_t count = 0;
    for (std::size_t i = 0; i < venues_.size(); ++i) {
        const auto& venue = venues_[i];
        const bool fresh = venue.valid != 0 && venue.healthy != 0 && venue.gap == 0
            && venue.last_receive_ns > 0 && venue.last_receive_ns <= now_ns
            && now_ns - venue.last_receive_ns <= policy.max_venue_age_ns
            && finite(venue.mid) && venue.mid > 0.0;
        if (!fresh) continue;
        const double weight = finite(policy.venue_weights[i])
            ? std::max(0.0, policy.venue_weights[i]) : 0.0;
        if (weight <= 0.0) continue;
        log_sum += weight * std::log(venue.mid);
        micro_sum += weight * (venue.microprice > 0.0 ? venue.microprice : venue.mid);
        weight_sum += weight;
        mask |= (1U << static_cast<unsigned>(i));
        ++count;
    }
    const double composite = weight_sum > kEps ? std::exp(log_sum / weight_sum) : 0.0;
    if (microprice != nullptr) *microprice = weight_sum > kEps ? micro_sum / weight_sum : 0.0;
    if (health_mask != nullptr) *health_mask = mask;
    if (fresh_count != nullptr) *fresh_count = count;

    double max_dispersion = 0.0;
    if (composite > 0.0) {
        for (std::size_t i = 0; i < venues_.size(); ++i) {
            if ((mask & (1U << static_cast<unsigned>(i))) == 0U) continue;
            max_dispersion = std::max(max_dispersion,
                std::abs(std::log(venues_[i].mid / composite)) * 10'000.0);
        }
    }
    if (dispersion_bps != nullptr) *dispersion_bps = max_dispersion;
    return composite;
}

void ExternalAssetState::record_price_sample(std::int64_t receive_ns, double price) noexcept {
    if (receive_ns <= 0 || !finite(price) || price <= 0.0) return;
    history_[history_head_] = PriceSample{receive_ns, price};
    history_head_ = (history_head_ + 1) % history_.size();
    history_count_ = std::min(history_.size(), history_count_ + 1);
}

double ExternalAssetState::lagged_return(std::int64_t now_ns,
                                         std::int64_t horizon_ns,
                                         double current_price) const noexcept {
    if (history_count_ == 0 || current_price <= 0.0 || horizon_ns <= 0) return 0.0;
    const std::int64_t target = now_ns - horizon_ns;
    std::int64_t best_time = std::numeric_limits<std::int64_t>::min();
    double best_price = 0.0;
    for (std::size_t i = 0; i < history_count_; ++i) {
        const auto& sample = history_[i];
        if (sample.receive_ns > 0 && sample.receive_ns <= target
            && sample.receive_ns > best_time && sample.price > 0.0) {
            best_time = sample.receive_ns;
            best_price = sample.price;
        }
    }
    return best_price > 0.0 ? std::log(current_price / best_price) : 0.0;
}

ExternalAssetSnapshot ExternalAssetState::snapshot(
    std::int64_t now_ns,
    const ExternalStatePolicy& policy) const noexcept {

    ExternalAssetSnapshot out;
    out.asset_handle = asset_handle_;
    out.state_version = state_version_;
    out.chainlink_feed_handle = oracle_.feed_handle;
    out.chainlink_connection_epoch = oracle_.connection_epoch;
    out.chainlink_value_numeric = oracle_.value_numeric;
    out.chainlink_observed_ns = oracle_.oracle_observation_ns;
    out.chainlink_receive_ns = oracle_.receive_monotonic_ns;
    out.chainlink_age_ns = oracle_.age_ns;
    out.chainlink_window_seconds = oracle_.window_seconds;
    out.chainlink_continuity = oracle_.continuity;
    out.chainlink_healthy = oracle_.healthy;
    out.chainlink_valid = oracle_.valid;
    out.latest_input_receive_monotonic_ns = latest_receive_ns_;

    double micro = 0.0;
    double dispersion = 0.0;
    std::uint32_t mask = 0;
    std::uint32_t count = 0;
    const double composite = compute_composite(now_ns, policy, &micro, &dispersion, &mask, &count);
    out.venue_composite_price = composite;
    out.venue_composite_log_price = composite > 0.0 ? std::log(composite) : 0.0;
    out.venue_composite_microprice = micro;
    out.venue_dispersion_bps = dispersion;
    out.venue_health_mask = mask;
    out.venue_count_fresh = count;
    out.aggregate_ofi = aggregate_ofi_;
    out.aggregate_trade_imbalance = aggregate_trade_imbalance_;
    out.realized_vol_fast = std::sqrt(std::max(0.0, ew_var_fast_));
    out.realized_vol_medium = std::sqrt(std::max(0.0, ew_var_medium_));
    out.realized_vol_slow = std::sqrt(std::max(0.0, ew_var_slow_));
    out.fast_slow_vol_ratio = out.realized_vol_slow > kEps
        ? out.realized_vol_fast / out.realized_vol_slow : 0.0;
    out.jump_score = out.realized_vol_medium > kEps
        ? std::abs(lagged_return(now_ns, 250'000'000LL, composite))
            / out.realized_vol_medium : 0.0;
    out.venue_composite_return_50ms = lagged_return(now_ns, 50'000'000LL, composite);
    out.venue_composite_return_100ms = lagged_return(now_ns, 100'000'000LL, composite);
    out.venue_composite_return_250ms = lagged_return(now_ns, 250'000'000LL, composite);
    out.venue_composite_return_1s = lagged_return(now_ns, 1'000'000'000LL, composite);
    out.venue_composite_return_5s = lagged_return(now_ns, 5'000'000'000LL, composite);
    out.valid = asset_handle_ > 0 && state_version_ > 0 && composite > 0.0
        && count >= policy.min_healthy_venues && latest_receive_ns_ > 0
        && latest_receive_ns_ <= now_ns ? 1 : 0;
    return out;
}

CausalCut make_causal_cut(
    std::uint64_t causal_cut_id,
    std::int64_t decision_monotonic_ns,
    TriggerSource trigger_source,
    std::uint64_t trigger_source_version,
    const CausalStateInputs& inputs) noexcept {

    CausalCut out;
    out.causal_cut_id = causal_cut_id;
    out.decision_monotonic_ns = decision_monotonic_ns;
    out.pm_state_version = inputs.pm_state_version;
    out.oracle_state_version = inputs.oracle_state_version;
    out.external_state_version = inputs.external_state_version;
    out.settlement_reference_version = inputs.settlement_reference_version;
    out.inventory_state_version = inputs.inventory_state_version;
    out.private_state_version = inputs.private_state_version;
    out.risk_state_version = inputs.risk_state_version;
    out.trigger_source = trigger_source;
    out.trigger_source_version = trigger_source_version;

    const std::array<std::int64_t, 7> receive_times{
        inputs.pm_receive_ns,
        inputs.oracle_receive_ns,
        inputs.external_receive_ns,
        inputs.settlement_reference_receive_ns,
        inputs.inventory_receive_ns,
        inputs.private_receive_ns,
        inputs.risk_receive_ns,
    };
    std::int64_t maximum = 0;
    bool all_positive = true;
    for (const auto value : receive_times) {
        if (value <= 0) all_positive = false;
        maximum = std::max(maximum, value);
    }
    out.max_input_receive_monotonic_ns = maximum;
    out.valid = causal_cut_id > 0
        && decision_monotonic_ns > 0
        && trigger_source_version > 0
        && all_positive
        && maximum <= decision_monotonic_ns
        && inputs.pm_state_version > 0
        && inputs.oracle_state_version > 0
        && inputs.external_state_version > 0
        && inputs.settlement_reference_version > 0
        && inputs.inventory_state_version > 0
        && inputs.private_state_version > 0
        && inputs.risk_state_version > 0 ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
