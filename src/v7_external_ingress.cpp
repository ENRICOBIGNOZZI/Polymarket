#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_tape.hpp"
#include "pm/v7_ingress_wakeup.hpp"

#include <algorithm>
#include <array>

namespace pm::v7::external_fair {
namespace {

template <class T>
inline void single_writer_add(std::atomic<T>& counter, T delta = T{1}) noexcept {
    counter.store(counter.load(std::memory_order_relaxed) + delta,
                  std::memory_order_relaxed);
}

} // namespace

ExternalVenueIngress::ExternalVenueIngress(VenueId venue,
                                           std::uint64_t asset_handle,
                                           ExternalTapeRecorder* normalized_tape,
                                           IngressWakeup* wakeup) noexcept
    : venue_(venue), asset_handle_(asset_handle), normalized_tape_(normalized_tape),
      wakeup_(wakeup) {}

ExternalDecodeResult ExternalVenueIngress::on_frame(
    std::uint64_t connection_epoch,
    std::int64_t local_receive_monotonic_ns,
    std::int64_t local_receive_wall_ns,
    std::string_view payload) noexcept {

    single_writer_add(frames_);
    std::array<ExternalVenueEvent, kExternalIngressDecodeBatch> decoded{};
    auto result = decode_external_venue_frame(
        venue_, asset_handle_, connection_epoch,
        local_receive_monotonic_ns, local_receive_wall_ns,
        payload, decoded);
    if (result.invalid_frame != 0) {
        single_writer_add(invalid_frames_);
    }
    single_writer_add(decoded_events_, static_cast<std::uint64_t>(result.output_count));

    const auto previous_epoch = writer_connection_epoch_;
    if (previous_epoch != connection_epoch) {
        writer_connection_epoch_ = connection_epoch;
        connection_epoch_.store(connection_epoch, std::memory_order_release);
    }
    const bool reconnect = previous_epoch != 0 && previous_epoch != connection_epoch;
    if (reconnect) {
        single_writer_add(reconnects_);
        set_gap_pending();
    }
    if (result.output_overflow != 0 || result.arena_exhausted != 0) {
        set_gap_pending();
    }

    for (std::size_t i = 0; i < result.output_count; ++i) (void)enqueue_event(decoded[i]);
    return result;
}

bool ExternalVenueIngress::on_event(ExternalVenueEvent event) noexcept {
    if (event.venue != venue_ || event.asset_handle != asset_handle_
        || event.connection_epoch == 0 || event.local_receive_monotonic_ns <= 0
        || event.local_receive_wall_ns <= 0) {
        single_writer_add(invalid_frames_);
        return false;
    }
    single_writer_add(decoded_events_);
    const auto previous_epoch = writer_connection_epoch_;
    if (previous_epoch != event.connection_epoch) {
        writer_connection_epoch_ = event.connection_epoch;
        connection_epoch_.store(event.connection_epoch, std::memory_order_release);
    }
    if (previous_epoch != 0 && previous_epoch != event.connection_epoch) {
        single_writer_add(reconnects_);
        set_gap_pending();
    }
    return enqueue_event(event);
}

bool ExternalVenueIngress::enqueue_event(ExternalVenueEvent event) noexcept {
    if (writer_gap_pending_) {
        writer_gap_pending_ = false;
        gap_pending_.store(false, std::memory_order_release);
        event.gap = 1;
        single_writer_add(propagated_gaps_);
    }
    if (!queue_.try_push(event)) {
        single_writer_add(dropped_events_);
        set_gap_pending();
        healthy_.store(false, std::memory_order_release);
        return false;
    }
    single_writer_add(enqueued_events_);
    healthy_.store(event.healthy != 0 && event.stale == 0,
                   std::memory_order_release);
    if (normalized_tape_ != nullptr) (void)normalized_tape_->try_record_external_venue_event(event);
    if (wakeup_ != nullptr) wakeup_->notify();
    return true;
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
    single_writer_add(drained_events_, static_cast<std::uint64_t>(count));
    return count;
}

std::size_t ExternalVenueIngress::drain_events(
    std::span<ExternalVenueEvent> output,
    std::size_t max_events) noexcept {
    const std::size_t limit = std::min(output.size(), max_events);
    std::size_t count = 0;
    while (count < limit && queue_.try_pop(output[count])) ++count;
    single_writer_add(drained_events_, static_cast<std::uint64_t>(count));
    return count;
}

void ExternalVenueIngress::set_gap_pending() noexcept {
    if (writer_gap_pending_) return;
    writer_gap_pending_ = true;
    gap_pending_.store(true, std::memory_order_release);
}

void ExternalVenueIngress::mark_disconnected(
    std::uint64_t connection_epoch) noexcept {
    const auto previous = writer_connection_epoch_;
    if (previous != connection_epoch) {
        writer_connection_epoch_ = connection_epoch;
        connection_epoch_.store(connection_epoch, std::memory_order_release);
    }
    if (previous != 0 && previous != connection_epoch) {
        single_writer_add(reconnects_);
    }
    set_gap_pending();
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
