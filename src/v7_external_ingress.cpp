#include "pm/v7_external_ingress.hpp"
#include "pm/v7_external_tape.hpp"
#include "pm/v7_ingress_wakeup.hpp"

#include <algorithm>
#include <array>

namespace pm::v7::external_fair {

bool causal_event_precedes(const ExternalVenueEvent& left,
                           const ExternalVenueEvent& right) noexcept {
    if (left.local_receive_monotonic_ns != right.local_receive_monotonic_ns) {
        return left.local_receive_monotonic_ns < right.local_receive_monotonic_ns;
    }
    if (left.local_receive_wall_ns != right.local_receive_wall_ns) {
        return left.local_receive_wall_ns < right.local_receive_wall_ns;
    }
    if (left.venue != right.venue) {
        return static_cast<std::uint8_t>(left.venue)
            < static_cast<std::uint8_t>(right.venue);
    }
    return left.source_sequence < right.source_sequence;
}

CausalEventMergeResult merge_causal_events(
    std::span<const ExternalVenueEvent> first,
    std::span<const ExternalVenueEvent> second,
    std::span<ExternalVenueEvent> output) noexcept {
    CausalEventMergeResult result;
    const std::size_t count = first.size() + second.size();
    if (count > output.size()) {
        result.output_overflow = 1;
        return result;
    }
    const bool sorted = std::is_sorted(first.begin(), first.end(), causal_event_precedes)
        && std::is_sorted(second.begin(), second.end(), causal_event_precedes);
    if (sorted) {
        std::merge(first.begin(), first.end(), second.begin(), second.end(),
                   output.begin(), causal_event_precedes);
    } else {
        result.sort_fallback = 1;
        std::copy(first.begin(), first.end(), output.begin());
        std::copy(second.begin(), second.end(),
                  output.begin() + static_cast<std::ptrdiff_t>(first.size()));
        std::sort(output.begin(),
                  output.begin() + static_cast<std::ptrdiff_t>(count),
                  causal_event_precedes);
    }
    result.output_count = count;
    return result;
}

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

    for (std::size_t i = 0; i < result.output_count; ++i) (void)enqueue_event(decoded[i]);
    return result;
}

void ExternalVenueIngress::on_observer_frame(
    std::uint64_t connection_epoch, bool invalid_frame) noexcept {
    frames_.fetch_add(1, std::memory_order_relaxed);
    if (invalid_frame) invalid_frames_.fetch_add(1, std::memory_order_relaxed);
    const auto previous_epoch = connection_epoch_.exchange(
        connection_epoch, std::memory_order_acq_rel);
    if (previous_epoch != 0 && previous_epoch != connection_epoch) {
        reconnects_.fetch_add(1, std::memory_order_relaxed);
        gap_pending_.store(true, std::memory_order_release);
    }
}

bool ExternalVenueIngress::on_event(ExternalVenueEvent event) noexcept {
    if (event.venue != venue_ || event.asset_handle != asset_handle_
        || event.connection_epoch == 0 || event.local_receive_monotonic_ns <= 0
        || event.local_receive_wall_ns <= 0) {
        invalid_frames_.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    decoded_events_.fetch_add(1, std::memory_order_relaxed);
    const auto previous_epoch = connection_epoch_.exchange(
        event.connection_epoch, std::memory_order_acq_rel);
    if (previous_epoch != 0 && previous_epoch != event.connection_epoch) {
        reconnects_.fetch_add(1, std::memory_order_relaxed);
        gap_pending_.store(true, std::memory_order_release);
    }
    return enqueue_event(event);
}

bool ExternalVenueIngress::enqueue_event(ExternalVenueEvent event) noexcept {
    if (gap_pending_.exchange(false, std::memory_order_acq_rel)) {
        event.gap = 1;
        propagated_gaps_.fetch_add(1, std::memory_order_relaxed);
    }
    if (!queue_.try_push(event)) {
        dropped_events_.fetch_add(1, std::memory_order_relaxed);
        gap_pending_.store(true, std::memory_order_release);
        healthy_.store(false, std::memory_order_release);
        return false;
    }
    enqueued_events_.fetch_add(1, std::memory_order_relaxed);
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
    while (count < max_events) {
        const auto* event = queue_.try_peek();
        if (event == nullptr) break;
        (void)state.on_venue_event(*event, policy);
        if (!queue_.pop_commit()) break;
        ++count;
    }
    drained_events_.fetch_add(count, std::memory_order_relaxed);
    return count;
}

std::size_t ExternalVenueIngress::drain_events(
    std::span<ExternalVenueEvent> output,
    std::size_t max_events) noexcept {
    const std::size_t limit = std::min(output.size(), max_events);
    std::size_t count = 0;
    while (count < limit && queue_.try_pop(output[count])) ++count;
    drained_events_.fetch_add(count, std::memory_order_relaxed);
    return count;
}

const ExternalVenueEvent* ExternalVenueIngress::peek_event() const noexcept {
    return queue_.try_peek();
}

bool ExternalVenueIngress::commit_event() noexcept {
    if (!queue_.pop_commit()) return false;
    drained_events_.fetch_add(1, std::memory_order_relaxed);
    return true;
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
