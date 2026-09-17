#pragma once

#include "pm/v7_redundant_bbo.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace pm::v7::redundant_bbo {

inline constexpr std::size_t kQueueCapacity = 4096;

struct LaneSnapshot {
    std::uint64_t generation = 0;
    std::uint64_t decoded_updates = 0;
    std::uint64_t queue_drops = 0;
    std::uint64_t decode_failures = 0;
    std::size_t backlog = 0;
    std::uint8_t disabled = 0;
};

struct FeedSnapshot {
    std::array<LaneSnapshot, kLaneCount> lanes{};
    Metrics gate{};
    std::uint8_t started = 0;
    std::uint8_t disabled_mask = 0;
};

// Three independent public market-WebSocket connections feeding three SPSC
// queues. Exactly one decision owner drains all lanes and owns Gate.
class Feed final {
public:
    Feed(std::string url, std::vector<polymarket_bbo::Binding> bindings,
         Mode mode = Mode::Quorum2Of3);
    ~Feed();
    Feed(const Feed&) = delete;
    Feed& operator=(const Feed&) = delete;

    void start();
    void stop() noexcept;
    [[nodiscard]] bool try_next_actionable(Decision& decision) noexcept;
    [[nodiscard]] std::array<std::uint64_t, kLaneCount> generations() const noexcept;
    [[nodiscard]] FeedSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7::redundant_bbo
