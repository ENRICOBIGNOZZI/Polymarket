#pragma once

#include "pm/v7_external_fair.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

struct FirstArrivalResult {
    std::uint64_t trigger_sequence = 0;
    std::int64_t trigger_receive_monotonic_ns = 0;
    VenueId venue = VenueId::Unknown;
    std::uint8_t evaluate = 0;
    std::uint8_t duplicate_or_stale = 0;
    std::uint8_t temporal_regression = 0;
    std::uint8_t unhealthy_or_gap = 0;
};

// Single decision-owner gate. It deliberately has no cross-venue readiness
// condition: one valid event from any venue can become actionable immediately.
// Continuity is enforced independently per venue, so a silent Coinbase/Bybit
// lane never blocks a Binance trigger (and vice versa).
class FirstArrivalGate final {
public:
    [[nodiscard]] FirstArrivalResult on_event(
        const ExternalVenueEvent& event) noexcept {
        FirstArrivalResult out;
        out.venue = event.venue;
        out.trigger_receive_monotonic_ns = event.local_receive_monotonic_ns;
        const auto index = venue_index(event.venue);
        if (index >= lanes_.size() || event.asset_handle == 0
            || event.connection_epoch == 0 || event.source_sequence == 0
            || event.local_receive_monotonic_ns <= 0) {
            out.temporal_regression = 1;
            return out;
        }
        auto& lane = lanes_[index];
        if (event.gap != 0 || event.healthy == 0) {
            lane.continuous = 0;
            out.unhealthy_or_gap = 1;
            return out;
        }

        if (lane.connection_epoch != event.connection_epoch) {
            if (lane.connection_epoch != 0
                && event.connection_epoch < lane.connection_epoch) {
                out.duplicate_or_stale = 1;
                return out;
            }
            lane.connection_epoch = event.connection_epoch;
            lane.source_sequence = 0;
            lane.receive_monotonic_ns = 0;
            lane.continuous = 1;
        }
        if (lane.continuous == 0) {
            // A gap must be healed explicitly by a source-specific recovery
            // boundary before this lane can trigger again.
            out.unhealthy_or_gap = 1;
            return out;
        }
        if (event.source_sequence <= lane.source_sequence) {
            out.duplicate_or_stale = 1;
            return out;
        }
        if (event.local_receive_monotonic_ns < lane.receive_monotonic_ns) {
            lane.continuous = 0;
            out.temporal_regression = 1;
            return out;
        }

        lane.source_sequence = event.source_sequence;
        lane.receive_monotonic_ns = event.local_receive_monotonic_ns;
        out.trigger_sequence = ++trigger_sequence_;
        out.evaluate = 1;
        return out;
    }

    // Call only after the venue's source-specific reconnect/snapshot logic has
    // re-established continuity. No other venue participates in this reset.
    [[nodiscard]] bool mark_recovered(
        VenueId venue,
        std::uint64_t connection_epoch) noexcept {
        const auto index = venue_index(venue);
        if (index >= lanes_.size() || connection_epoch == 0) return false;
        auto& lane = lanes_[index];
        if (lane.connection_epoch != 0 && connection_epoch < lane.connection_epoch)
            return false;
        lane.connection_epoch = connection_epoch;
        lane.source_sequence = 0;
        lane.receive_monotonic_ns = 0;
        lane.continuous = 1;
        return true;
    }

    [[nodiscard]] std::uint64_t trigger_sequence() const noexcept {
        return trigger_sequence_;
    }

private:
    struct LaneState {
        std::uint64_t connection_epoch = 0;
        std::uint64_t source_sequence = 0;
        std::int64_t receive_monotonic_ns = 0;
        std::uint8_t continuous = 1;
    };

    [[nodiscard]] static constexpr std::size_t venue_index(VenueId venue) noexcept {
        return static_cast<std::size_t>(venue);
    }

    std::array<LaneState, kVenueCount + 1> lanes_{};
    std::uint64_t trigger_sequence_ = 0;
};

static_assert(std::is_trivially_copyable_v<FirstArrivalResult>);
static_assert(std::is_standard_layout_v<FirstArrivalResult>);

} // namespace pm::v7::external_fair
