#pragma once

#include <boost/json.hpp>

#include <array>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <type_traits>

namespace pm::v7::external_fair {

struct LatestJsonPublisherSnapshot {
    std::uint64_t submitted = 0;
    std::uint64_t written = 0;
    std::uint64_t coalesced = 0;
    std::uint64_t failures = 0;
    std::uint8_t healthy = 0;
    std::uint8_t pending = 0;
    std::uint8_t in_flight = 0;
    std::array<std::uint8_t, 5> reserved{};
};

// Latest-state JSON publication for status surfaces. The producer never waits
// for serialization or filesystem I/O. If the writer is slower than the
// producer, only superseded status snapshots are coalesced; event/ledger data
// must never use this class.
class LatestJsonPublisher final {
public:
    explicit LatestJsonPublisher(std::filesystem::path path);
    ~LatestJsonPublisher();

    LatestJsonPublisher(const LatestJsonPublisher&) = delete;
    LatestJsonPublisher& operator=(const LatestJsonPublisher&) = delete;

    [[nodiscard]] bool publish(boost::json::object value) noexcept;
    [[nodiscard]] LatestJsonPublisherSnapshot snapshot() const noexcept;
    void close() noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::is_trivially_copyable_v<LatestJsonPublisherSnapshot>);

} // namespace pm::v7::external_fair
