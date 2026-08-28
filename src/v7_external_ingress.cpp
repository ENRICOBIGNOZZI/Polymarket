#include "pm/v7_external_ingress.hpp"

#include <algorithm>
#include <array>

namespace pm::v7::external_fair {

ExternalVenueIngress::ExternalVenueIngress(VenueId venue,
                                           std::uint64_t asset_handle) noexcept
    : venue_(venue), asset_handle_(asset_handle) {}

ExternalDecodeResult ExternalVenueIngress::on_frame(
    std::uint64_t connection_epoch,
    std::int64_t local_receive_monotonic_ns,
    std::int64_t local_receive_wall_ns,
    std::string_view payload) noexcept {

    frames_.fetch_add(1, std::memory_order_relaxed);
    std::array<ExternalVenueEvent, kExternalIngressDecodeBatch> decoded{};
    auto result = decode_external_venue_frame(
        venue_, asset_handle_, connection_epoch,
        local_receive_monotonic_ns, local_receive_wall_ns,
        payload, decoded);
    if (result.invalid_frame != 0) {
        invalid_frames_.fetch_add(1, std::memory_order_relaxed);
    }
    decoded_events_.fetch_add(result.output_count, std::memory_order_relaxed);

    const auto previous_epoch = connection_epoch_.exchange(
        connection_epoch, std::memory_order_acq_rel);
    const bool reconnect = previous_epoch != 0 && previous_epoch != connection_epoch;
    if (reconnect) {
        reconnects_.fetch_add(1, std::memory_order_relaxed);
        gap_pending_.store(true, std::memory_order_release);
    }
    if (result.output_overflow != 0 || result.arena_exhausted != 0) {
        gap_pending_.store(true, std::memory_order_release);
    }

    for (std::size_t i = 0; i < result.output_count; ++i) {
        auto event = decoded[i];
        if (reconnect) event.reconnect = 1;
        if (gap_pending_.exchange(false, std::memory_order_acq_rel)) {
            event.gap = 1;
            propagated_gaps_.fetch_add(1, std::memory_order_relaxed);
        }
        if (!queue_.try_push(event)) {
            dropped_events_.fetch_add(1, std::memory_order_relaxed);
            gap_pending_.store(true, std::memory_order_release);
            healthy_.store(false, std::memory_order_release);
            continue;
        }
        enqueued_events_.fetch_add(1, std::memory_order_relaxed);
        healthy_.store(event.healthy != 0 && event.stale == 0,
                       std::memory_order_release);
    }
    return result;
}

std::size_t ExternalVenueIngress::drain_into(
    ExternalAssetState& state,
    const ExternalStatePolicy& policy,
    std::size_t max_events) noexcept {
    std::size_t count = 0;
    ExternalVenueEvent event;
    while (count < max_events && queue_.try_pop(event)) {
        (void)state.on_venue_event(event, policy);
        ++count;
    }
    drained_events_.fetch_add(count, std::memory_order_relaxed);
    return count;
}

void ExternalVenueIngress::mark_disconnected(
    std::uint64_t connection_epoch) noexcept {
    const auto previous = connection_epoch_.exchange(
        connection_epoch, std::memory_order_acq_rel);
    if (previous != 0 && previous != connection_epoch) {
        reconnects_.fetch_add(1, std::memory_order_relaxed);
    }
    gap_pending_.store(true, std::memory_order_release);
    healthy_.store(false, std::memory_order_release);
}

ExternalIngressSnapshot ExternalVenueIngress::snapshot() const noexcept {
    ExternalIngressSnapshot out;
    out.frames = frames_.load(std::memory_order_acquire);
    out.decoded_events = decoded_events_.load(std::memory_order_acquire);
    out.enqueued_events = enqueued_events_.load(std::memory_order_acquire);
    out.drained_events = drained_events_.load(std::memory_order_acquire);
    out.dropped_events = dropped_events_.load(std::memory_order_acquire);
    out.invalid_frames = invalid_frames_.load(std::memory_order_acquire);
    out.reconnects = reconnects_.load(std::memory_order_acquire);
    out.propagated_gaps = propagated_gaps_.load(std::memory_order_acquire);
    out.connection_epoch = connection_epoch_.load(std::memory_order_acquire);
    out.queued = queue_.approximate_size();
    out.venue = venue_;
    out.healthy = healthy_.load(std::memory_order_acquire) ? 1 : 0;
    out.gap_pending = gap_pending_.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
