#pragma once

#include "pm/v7_external_state.hpp"
#include "pm/v7_spsc.hpp"

#include <boost/json.hpp>

#include <array>
#include <atomic>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::size_t kExternalCancelPublishCapacity = 256;

struct ExternalCancelPublishRecord {
    ExternalCancelSignalSnapshot signal{};
    std::int64_t publish_monotonic_ns = 0;
    std::int64_t publish_wall_ns = 0;
};

struct ExternalCancelPublisherSnapshot {
    std::uint64_t submitted = 0;
    std::uint64_t written = 0;
    std::uint64_t dropped = 0;
    std::uint64_t failures = 0;
    std::size_t queued = 0;
    std::uint8_t healthy = 0;
    std::uint8_t in_flight = 0;
    std::array<std::uint8_t, 6> reserved{};
};

// Lossless ordered latest-file publication. The single hot producer only
// enqueues a fixed POD record; JSON construction and filesystem I/O belong to
// one cold writer thread. Queue saturation is an explicit failure, never a
// coalescing/drop policy.
class ExternalCancelSignalPublisher final {
public:
    using JsonBuilder = std::function<boost::json::object(const ExternalCancelPublishRecord&)>;

    ExternalCancelSignalPublisher(std::filesystem::path path, JsonBuilder builder);
    ~ExternalCancelSignalPublisher();

    ExternalCancelSignalPublisher(const ExternalCancelSignalPublisher&) = delete;
    ExternalCancelSignalPublisher& operator=(const ExternalCancelSignalPublisher&) = delete;

    [[nodiscard]] bool publish(const ExternalCancelPublishRecord& record) noexcept;
    [[nodiscard]] bool healthy() const noexcept;
    [[nodiscard]] ExternalCancelPublisherSnapshot snapshot() const noexcept;
    void close() noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::is_trivially_copyable_v<ExternalCancelPublishRecord>);
static_assert(std::is_trivially_copyable_v<ExternalCancelPublisherSnapshot>);

} // namespace pm::v7::external_fair
