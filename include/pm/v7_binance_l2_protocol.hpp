#pragma once

#include "pm/v7_binance_l2.hpp"

#include <cstddef>
#include <cstdint>
#include <string_view>

namespace pm::v7::external_fair {

enum class BinanceDepthParseState : std::uint8_t {
    Ignored = 0,
    Parsed = 1,
    Invalid = 2,
    TooLarge = 3,
};

// Both functions have a bounded level count. Callers must treat TooLarge as a
// gap and recover, never trim a message into a seemingly valid book.
[[nodiscard]] BinanceDepthParseState parse_binance_spot_depth_delta(
    std::string_view payload, std::int64_t local_receive_monotonic_ns,
    BinanceDepthDelta& output, std::size_t max_levels_per_side = 1000) noexcept;

[[nodiscard]] BinanceDepthParseState parse_binance_depth_snapshot(
    std::string_view payload, std::int64_t local_receive_monotonic_ns,
    BinanceDepthSnapshot& output, std::size_t max_levels_per_side = 1000) noexcept;

} // namespace pm::v7::external_fair
