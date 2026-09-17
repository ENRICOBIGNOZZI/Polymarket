#pragma once

#include "pm/v7_polymarket_bbo.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace pm::v7::redundant_bbo {

inline constexpr std::size_t kLaneCount = 3;
inline constexpr std::size_t kInstrumentCapacity = 2048;

enum class Mode : std::uint8_t {
    Quorum2Of3 = 1,
    FirstOf3Shadow = 2,
};

enum class Outcome : std::uint8_t {
    None = 0,
    Actionable = 1,
    Duplicate = 2,
    Stale = 3,
    Conflict = 4,
    Invalid = 5,
    OutOfRange = 6,
};

struct Envelope {
    polymarket_bbo::Update update{};
    std::uint8_t lane = 0;
    std::array<std::uint8_t, 7> reserved{};
};

struct Decision {
    polymarket_bbo::Update update{};
    Outcome outcome = Outcome::None;
    std::uint8_t agreeing_lanes_mask = 0;
    std::uint8_t observed_lanes_mask = 0;
    std::uint8_t conflicting_lanes_mask = 0;
    std::int64_t first_receive_monotonic_ns = 0;
    std::int64_t ready_monotonic_ns = 0;
};

struct Metrics {
    std::uint64_t observations = 0;
    std::uint64_t actionable = 0;
    std::uint64_t duplicates = 0;
    std::uint64_t stale = 0;
    std::uint64_t conflicts = 0;
    std::uint64_t invalid = 0;
    std::uint64_t out_of_range = 0;
    std::uint64_t superseded_without_quorum = 0;
    std::uint64_t post_emit_conflicts = 0;
};

class Gate final {
public:
    explicit Gate(Mode mode = Mode::Quorum2Of3) noexcept : mode_(mode) {}
    [[nodiscard]] Decision observe(const Envelope& envelope) noexcept;
    void reset_instrument(std::uint64_t instrument_handle) noexcept;
    void reset_all() noexcept;
    [[nodiscard]] const Metrics& metrics() const noexcept { return metrics_; }
    [[nodiscard]] Mode mode() const noexcept { return mode_; }

private:
    struct Slot {
        std::int64_t exchange_event_ns = 0;
        std::int64_t first_receive_ns = 0;
        std::array<std::int32_t, kLaneCount> bid_e4{};
        std::array<std::int32_t, kLaneCount> ask_e4{};
        std::array<std::int64_t, kLaneCount> receive_ns{};
        std::uint8_t seen_mask = 0;
        std::uint8_t conflict_mask = 0;
        std::uint8_t emitted = 0;
        std::uint8_t reserved = 0;
    };

    [[nodiscard]] Decision start_epoch(Slot& slot, const Envelope& envelope) noexcept;
    [[nodiscard]] Decision decide(Slot& slot, const Envelope& envelope) noexcept;
    [[nodiscard]] static std::uint8_t matching_mask(const Slot& slot) noexcept;

    Mode mode_ = Mode::Quorum2Of3;
    std::array<Slot, kInstrumentCapacity> slots_{};
    Metrics metrics_{};
};

static_assert(std::is_trivially_copyable_v<Envelope>);
static_assert(std::is_trivially_copyable_v<Decision>);
static_assert(std::is_trivially_copyable_v<Metrics>);

} // namespace pm::v7::redundant_bbo
