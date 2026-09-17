#pragma once

#include "pm/v7_user_ws.hpp"

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace pm::v7::user_ws {

struct FeedSnapshot {
    std::uint8_t connected = 0;
    std::uint64_t frames = 0;
    std::uint64_t decoded_events = 0;
    std::uint64_t invalid_frames = 0;
    std::uint64_t reconnects = 0;
    std::uint64_t transport_errors = 0;
};

class AuthenticatedFeed final {
public:
    using EventHandler = std::function<void(const Event&)>;
    using ErrorHandler = std::function<void(std::string_view)>;
    using ReconnectHandler = std::function<void()>;

    AuthenticatedFeed(CredentialsView credentials,
                      std::vector<std::string> condition_ids,
                      EventHandler on_event,
                      ErrorHandler on_error = {},
                      ReconnectHandler on_reconnect = {});
    ~AuthenticatedFeed();

    AuthenticatedFeed(const AuthenticatedFeed&) = delete;
    AuthenticatedFeed& operator=(const AuthenticatedFeed&) = delete;

    void start();
    void stop();
    [[nodiscard]] FeedSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace pm::v7::user_ws
