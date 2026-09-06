#pragma once

#include "pm/v7_market_state.hpp"

#include <array>
#include <cstdint>
#include <type_traits>

namespace pm::v7::research {

inline constexpr std::uint32_t kCryptoBookTapeSchemaVersion = 2;

enum class CryptoBookOutcome : std::uint8_t {
    Yes = 1,
    No = 2,
};

// Payload stored inside TapeRecordKind::PmState for the dedicated BTC M5
// research tape. Token strings and handle mappings live in the immutable
// per-contract session manifest beside the binary segments.
struct CryptoBookTapePayload {
    std::uint64_t connection_epoch = 0;
    std::int64_t receive_wall_ms = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint32_t schema_version = kCryptoBookTapeSchemaVersion;
    std::uint8_t event_kind = 0;
    CryptoBookOutcome outcome = CryptoBookOutcome::Yes;
    std::array<std::uint8_t, 2> reserved{};
    std::int32_t trade_price_e4 = 0;
    std::int64_t trade_quantity_microunits = 0;
    std::int8_t trade_side = 0; // +1 buyer initiated, -1 seller initiated.
    std::array<std::uint8_t, 7> trade_reserved{};
    BookHotSnapshot book{};
};

static_assert(std::is_trivially_copyable_v<CryptoBookTapePayload>);

} // namespace pm::v7::research
