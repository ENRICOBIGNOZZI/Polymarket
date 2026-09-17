#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <span>
#include <type_traits>

namespace pm::v7::prepared_order {

inline constexpr std::size_t kMaxFrameBytes = 4096;

struct Key {
    std::uint64_t instrument_handle = 0;
    std::int32_t price_e4 = 0;
    std::int32_t tick_size_e4 = 0;
    std::int64_t quantity_microunits = 0;
    std::uint8_t side = 0;
    std::uint8_t time_in_force = 0;
    std::array<std::uint8_t, 6> reserved{};
};

struct Publication {
    Key key{};
    std::int64_t source_exchange_event_ns = 0;
    std::int64_t prepared_monotonic_ns = 0;
    std::uint64_t order_timestamp_ms = 0;
    std::uint64_t salt = 0;
};

struct Requirement {
    Key key{};
    std::int64_t source_exchange_event_ns = 0;
    std::int64_t now_monotonic_ns = 0;
    std::int64_t maximum_age_ns = 0;
    std::uint8_t require_exact_source_event = 1;
    std::array<std::uint8_t, 7> reserved{};
};

enum class AcquireReason : std::uint8_t {
    Hit = 1,
    Empty = 2,
    Mismatch = 3,
    Stale = 4,
    AlreadyConsumed = 5,
    ChangedDuringAcquire = 6,
};

struct Lease {
    const char* data = nullptr;
    std::size_t size = 0;
    Publication publication{};
    std::uint64_t generation = 0;
    std::uint8_t slot = 0;
    std::uint8_t valid = 0;
    std::array<std::uint8_t, 6> reserved{};
};

struct AcquireResult {
    Lease lease{};
    AcquireReason reason = AcquireReason::Empty;
};

struct Snapshot {
    std::uint64_t published_generation = 0;
    std::uint64_t publishes = 0;
    std::uint64_t publish_blocked = 0;
    std::uint64_t hits = 0;
    std::uint64_t empty = 0;
    std::uint64_t mismatch = 0;
    std::uint64_t stale = 0;
    std::uint64_t consumed = 0;
    std::uint64_t changed_during_acquire = 0;
};

// One producer prepares complete signed HTTP frames. One execution consumer
// acquires a matching frame and owns the returned slot until release().
// Frames are one-shot: an acquired frame is never returned again, even when
// network submission becomes ambiguous after SSL_write starts.
class Cache final {
public:
    Cache() noexcept = default;
    [[nodiscard]] bool publish(const Publication& publication,
                               std::span<const char> frame) noexcept;
    [[nodiscard]] AcquireResult try_acquire(const Requirement& requirement) noexcept;
    void release(Lease& lease) noexcept;
    void clear() noexcept;
    [[nodiscard]] Snapshot snapshot() const noexcept;

private:
    struct alignas(64) Slot {
        std::array<char, kMaxFrameBytes> frame{};
        Publication publication{};
        std::size_t size = 0;
        std::uint64_t generation = 0;
        std::atomic<std::uint32_t> readers{0};
        std::atomic<std::uint8_t> consumed{0};
    };

    [[nodiscard]] static bool key_equal(const Key& a, const Key& b) noexcept;
    [[nodiscard]] static std::uint64_t pack(std::uint64_t generation,
                                            std::uint8_t slot) noexcept;
    [[nodiscard]] static std::uint8_t unpack_slot(std::uint64_t published) noexcept;
    [[nodiscard]] static std::uint64_t unpack_generation(std::uint64_t published) noexcept;

    std::array<Slot, 2> slots_{};
    std::atomic<std::uint64_t> published_{0};
    std::uint64_t producer_generation_ = 0;
    std::atomic<std::uint64_t> publishes_{0};
    std::atomic<std::uint64_t> publish_blocked_{0};
    std::atomic<std::uint64_t> hits_{0};
    std::atomic<std::uint64_t> empty_{0};
    std::atomic<std::uint64_t> mismatch_{0};
    std::atomic<std::uint64_t> stale_{0};
    std::atomic<std::uint64_t> consumed_{0};
    std::atomic<std::uint64_t> changed_{0};
};

static_assert(std::is_trivially_copyable_v<Key>);
static_assert(std::is_trivially_copyable_v<Publication>);
static_assert(std::is_trivially_copyable_v<Requirement>);
static_assert(std::is_trivially_copyable_v<Lease>);

} // namespace pm::v7::prepared_order
