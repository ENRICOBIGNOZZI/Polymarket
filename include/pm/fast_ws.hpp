#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace pm::fast {

struct FeedReceiveStamp {
    // Wall time is used only for exchange-vs-local lineage. Local latency must
    // use the monotonic clock carried alongside it.
    std::int64_t wall_ms = 0;
    std::int64_t monotonic_ns = 0;
};

struct FeedSnapshot {
    std::size_t workers = 0;
    std::size_t connected_workers = 0;
    std::uint64_t messages = 0;
    std::uint64_t reconnects = 0;
    std::uint64_t errors = 0;
};

class MarketWebSocketFeed {
public:
    using MessageHandler = std::function<void(std::string_view, const FeedReceiveStamp&, std::size_t)>;
    using ErrorHandler = std::function<void(std::size_t, std::string_view)>;

    MarketWebSocketFeed(std::string url,
                        std::vector<std::string> asset_ids,
                        std::size_t shard_size,
                        MessageHandler on_message,
                        ErrorHandler on_error = {});
    ~MarketWebSocketFeed();

    MarketWebSocketFeed(const MarketWebSocketFeed&) = delete;
    MarketWebSocketFeed& operator=(const MarketWebSocketFeed&) = delete;

    void start();
    void stop();
    [[nodiscard]] FeedSnapshot snapshot() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::fast
