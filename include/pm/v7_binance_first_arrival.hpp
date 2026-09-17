#pragma once

#include "pm/v7_external_fair.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::size_t kBinanceFirstArrivalSlots = 4096;
static_assert((kBinanceFirstArrivalSlots & (kBinanceFirstArrivalSlots - 1)) == 0);

enum class BinanceFirstArrivalDisposition : std::uint8_t {
    Invalid = 0,
    First = 1,
    IndependentConfirm = 2,
    SameLaneDuplicate = 3,
    Conflict = 4,
    StaleSequence = 5,
};

struct BinanceFirstArrivalResult {
    ExternalVenueEvent event{};
    BinanceFirstArrivalDisposition disposition = BinanceFirstArrivalDisposition::Invalid;
    std::uint64_t source_sequence = 0;
    std::int64_t first_receive_monotonic_ns = 0;
    std::int64_t confirmation_receive_monotonic_ns = 0;
    std::uint8_t lane_mask = 0;
    std::uint8_t emit_first = 0;
    std::uint8_t independent_confirmations = 0;
    std::uint8_t conflict = 0;
    std::array<std::uint8_t, 4> reserved{};
};

class BinanceAggTradeFirstArrivalGate final {
public:
    [[nodiscard]] BinanceFirstArrivalResult observe(
        std::uint8_t lane,
        const ExternalVenueEvent& event) noexcept;

    [[nodiscard]] std::uint64_t first_arrivals() const noexcept { return first_arrivals_; }
    [[nodiscard]] std::uint64_t independent_confirms() const noexcept { return confirms_; }
    [[nodiscard]] std::uint64_t same_lane_duplicates() const noexcept { return duplicates_; }
    [[nodiscard]] std::uint64_t conflicts() const noexcept { return conflicts_; }
    [[nodiscard]] std::uint64_t stale_sequences() const noexcept { return stale_sequences_; }

private:
    struct Slot {
        std::uint64_t asset_handle = 0;
        std::uint64_t source_sequence = 0;
        std::int64_t exchange_event_ns = 0;
        std::int64_t first_receive_monotonic_ns = 0;
        double trade_price = 0.0;
        double trade_size = 0.0;
        std::int8_t trade_side = 0;
        std::uint8_t lane_mask = 0;
        std::uint8_t independent_confirmations = 0;
        std::uint8_t conflict = 0;
        std::uint8_t occupied = 0;
        std::array<std::uint8_t, 3> reserved{};
    };
    [[nodiscard]] static bool valid_event(const ExternalVenueEvent& event) noexcept;
    [[nodiscard]] static bool same_payload(
        const Slot& slot, const ExternalVenueEvent& event) noexcept;

    std::array<Slot, kBinanceFirstArrivalSlots> slots_{};
    std::uint64_t first_arrivals_ = 0;
    std::uint64_t confirms_ = 0;
    std::uint64_t duplicates_ = 0;
    std::uint64_t conflicts_ = 0;
    std::uint64_t stale_sequences_ = 0;
    std::uint64_t high_watermark_sequence_ = 0;
};

static_assert(std::is_trivially_copyable_v<BinanceFirstArrivalResult>);
static_assert(std::is_standard_layout_v<BinanceFirstArrivalResult>);

} // namespace pm::v7::external_fair
