#include "pm/v7_clob_order_lane_selector.hpp"

#include <limits>

namespace pm::v7::clob {

OrderLaneSelector::OrderLaneSelector(
    std::size_t lane_count, std::int64_t max_idle_ns) noexcept
    : lane_count_(lane_count), max_idle_ns_(max_idle_ns) {
    valid_ = lane_count_ > 0 && lane_count_ <= lanes_.size() && max_idle_ns_ > 0;
    if (!valid_) lane_count_ = 0;
}

bool OrderLaneSelector::on_connected(
    std::size_t lane, std::uint64_t connection_epoch,
    std::int64_t connected_ns) noexcept {
    if (!valid_ || lane >= lane_count_ || connection_epoch == 0 || connected_ns <= 0) {
        return false;
    }
    auto& state = lanes_[lane];
    if (state.connected || state.phase != OrderLanePhase::Idle
        || state.active_order_id != 0) return false;
    state.connected = true;
    state.connection_epoch = connection_epoch;
    state.connected_ns = connected_ns;
    state.last_roundtrip_ns = 0;
    state.last_rtt_ns = 0;
    return true;
}
bool OrderLaneSelector::record_roundtrip(
    std::size_t lane, std::uint64_t connection_epoch,
    std::int64_t completed_ns, std::int64_t rtt_ns) noexcept {
    if (!valid_ || lane >= lane_count_ || completed_ns <= 0 || rtt_ns <= 0) return false;
    auto& state = lanes_[lane];
    if (!state.connected || state.connection_epoch != connection_epoch
        || state.phase != OrderLanePhase::Idle
        || completed_ns < state.connected_ns
        || (state.last_roundtrip_ns != 0 && completed_ns < state.last_roundtrip_ns)) {
        return false;
    }
    state.last_roundtrip_ns = completed_ns;
    state.last_rtt_ns = rtt_ns;
    return true;
}

bool OrderLaneSelector::on_disconnected(
    std::size_t lane, std::uint64_t connection_epoch) noexcept {
    if (!valid_ || lane >= lane_count_) return false;
    auto& state = lanes_[lane];
    if (!state.connected || state.connection_epoch != connection_epoch) return false;
    state.connected = false;
    state.last_roundtrip_ns = 0;
    state.last_rtt_ns = 0;
    if (state.phase == OrderLanePhase::Reserved) {
        state.phase = OrderLanePhase::Idle;
        state.active_order_id = 0;
    }
    return true;
}
OrderLaneLease OrderLaneSelector::acquire(
    std::uint64_t order_id, std::int64_t now_ns) noexcept {
    OrderLaneLease out{};
    if (!valid_ || order_id == 0 || now_ns <= 0) return out;
    for (std::size_t i = 0; i < lane_count_; ++i) {
        if (lanes_[i].active_order_id == order_id) return out;
    }

    std::size_t best = lane_count_;
    std::int64_t best_rtt = std::numeric_limits<std::int64_t>::max();
    std::int64_t best_roundtrip = 0;
    for (std::size_t i = 0; i < lane_count_; ++i) {
        const auto& state = lanes_[i];
        if (!state.connected || state.phase != OrderLanePhase::Idle
            || state.active_order_id != 0 || state.last_roundtrip_ns == 0
            || state.last_rtt_ns <= 0 || now_ns < state.last_roundtrip_ns
            || now_ns - state.last_roundtrip_ns > max_idle_ns_) {
            continue;
        }
        if (state.last_rtt_ns < best_rtt
            || (state.last_rtt_ns == best_rtt && state.last_roundtrip_ns > best_roundtrip)) {
            best = i;
            best_rtt = state.last_rtt_ns;
            best_roundtrip = state.last_roundtrip_ns;
        }
    }
    if (best == lane_count_) return out;
    auto& state = lanes_[best];
    std::uint64_t generation = next_lease_generation_++;
    if (generation == 0) generation = next_lease_generation_++;
    if (next_lease_generation_ == 0) next_lease_generation_ = 1;
    state.active_order_id = order_id;
    state.lease_generation = generation;
    state.phase = OrderLanePhase::Reserved;

    out.order_id = order_id;
    out.connection_epoch = state.connection_epoch;
    out.lease_generation = generation;
    out.lane = static_cast<std::uint8_t>(best);
    out.valid = 1;
    return out;
}

bool OrderLaneSelector::lease_matches(const OrderLaneLease& lease) const noexcept {
    if (!lease.valid || lease.lane >= lane_count_ || lease.order_id == 0
        || lease.connection_epoch == 0 || lease.lease_generation == 0) return false;
    const auto& state = lanes_[lease.lane];
    return state.active_order_id == lease.order_id
        && state.connection_epoch == lease.connection_epoch
        && state.lease_generation == lease.lease_generation;
}

bool OrderLaneSelector::cancel_unsent(const OrderLaneLease& lease) noexcept {
    if (!lease_matches(lease)) return false;
    auto& state = lanes_[lease.lane];
    if (state.phase != OrderLanePhase::Reserved) return false;
    state.active_order_id = 0;
    state.phase = OrderLanePhase::Idle;
    return true;
}

bool OrderLaneSelector::mark_send_started(const OrderLaneLease& lease) noexcept {
    if (!lease_matches(lease)) return false;
    auto& state = lanes_[lease.lane];
    if (!state.connected || state.phase != OrderLanePhase::Reserved) return false;
    state.phase = OrderLanePhase::SentAmbiguous;
    return true;
}

bool OrderLaneSelector::complete_response(
    const OrderLaneLease& lease,
    std::int64_t completed_ns, std::int64_t rtt_ns) noexcept {
    if (!lease_matches(lease) || completed_ns <= 0 || rtt_ns <= 0) return false;
    auto& state = lanes_[lease.lane];
    if (!state.connected || state.phase != OrderLanePhase::SentAmbiguous
        || completed_ns < state.connected_ns
        || (state.last_roundtrip_ns != 0 && completed_ns < state.last_roundtrip_ns)) {
        return false;
    }
    state.last_roundtrip_ns = completed_ns;
    state.last_rtt_ns = rtt_ns;
    state.active_order_id = 0;
    state.phase = OrderLanePhase::Idle;
    return true;
}
bool OrderLaneSelector::transport_failure(const OrderLaneLease& lease) noexcept {
    if (!lease_matches(lease)) return false;
    auto& state = lanes_[lease.lane];
    state.connected = false;
    state.last_roundtrip_ns = 0;
    state.last_rtt_ns = 0;
    if (state.phase == OrderLanePhase::Reserved) {
        state.active_order_id = 0;
        state.phase = OrderLanePhase::Idle;
        return true;
    }
    return state.phase == OrderLanePhase::SentAmbiguous;
}

bool OrderLaneSelector::resolve_ambiguous(const OrderLaneLease& lease) noexcept {
    if (!lease_matches(lease)) return false;
    auto& state = lanes_[lease.lane];
    if (state.phase != OrderLanePhase::SentAmbiguous) return false;
    state.active_order_id = 0;
    state.phase = OrderLanePhase::Idle;
    return true;
}

OrderLaneSnapshot OrderLaneSelector::snapshot(std::size_t lane) const noexcept {
    OrderLaneSnapshot out{};
    if (!valid_ || lane >= lane_count_) return out;
    const auto& state = lanes_[lane];
    out.last_roundtrip_ns = state.last_roundtrip_ns;
    out.last_rtt_ns = state.last_rtt_ns;
    out.active_order_id = state.active_order_id;
    out.connection_epoch = state.connection_epoch;
    out.lease_generation = state.lease_generation;
    out.phase = state.phase;
    out.connected = state.connected ? 1U : 0U;
    return out;
}

} // namespace pm::v7::clob
