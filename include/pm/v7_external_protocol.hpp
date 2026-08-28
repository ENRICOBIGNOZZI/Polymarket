#pragma once

#include "pm/v7_external_fair.hpp"

#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>
#include <type_traits>

namespace pm::v7::external_fair {

struct ExternalDecodeResult {
    std::size_t output_count = 0;
    std::size_t recognized_events = 0;
    std::size_t ignored_events = 0;
    std::uint8_t invalid_frame = 0;
    std::uint8_t output_overflow = 0;
    std::uint8_t arena_exhausted = 0;
    std::uint8_t reserved = 0;
};

// Decoder boundary for public spot venue IO threads. Parsing is bounded and
// produces only fixed-size POD events before the SPSC handoff to the single
// ExternalAssetState writer. No venue JSON enters the decision kernel.
[[nodiscard]] ExternalDecodeResult decode_external_venue_frame(
    VenueId venue,
    std::uint64_t asset_handle,
    std::uint64_t connection_epoch,
    std::int64_t local_receive_monotonic_ns,
    std::int64_t local_receive_wall_ns,
    std::string_view payload,
    std::span<ExternalVenueEvent> output) noexcept;

static_assert(std::is_trivially_copyable_v<ExternalDecodeResult>);

} // namespace pm::v7::external_fair
