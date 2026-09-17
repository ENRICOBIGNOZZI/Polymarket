#pragma once

#include "pm/fast_ws.hpp"
#include "pm/v7_intent.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

namespace pm::v7 {

enum class UserWsEventKind : std::uint8_t { Order = 1, Trade = 2 };
enum class UserOrderAction : std::uint8_t {
    Unknown = 0, Placement = 1, Update = 2, Cancellation = 3,
};
enum class UserOrderStatus : std::uint8_t {
    Unknown = 0, Live = 1, Matched = 2, Delayed = 3,
    Unmatched = 4, Canceled = 5,
};
enum class UserTradeStatus : std::uint8_t {
    Unknown = 0, Matched = 1, MatchedNotBroadcasted = 2,
    Mined = 3, Confirmed = 4, Retrying = 5, Failed = 6,
};

template <std::size_t Capacity>
struct FixedUserText {
    std::array<char, Capacity> bytes{};
    std::uint16_t size = 0;
    [[nodiscard]] std::string_view view() const noexcept {
        return {bytes.data(), size};
    }
};

struct UserWsEvent {
    UserWsEventKind kind = UserWsEventKind::Order;
    UserOrderAction order_action = UserOrderAction::Unknown;
    UserOrderStatus order_status = UserOrderStatus::Unknown;
    UserTradeStatus trade_status = UserTradeStatus::Unknown;
    Side side = Side::None;
    FixedUserText<96> order_id{};
    FixedUserText<96> trade_id{};
    FixedUserText<96> taker_order_id{};
    FixedUserText<96> market{};
    FixedUserText<128> asset_id{};
    std::int64_t original_size_microunits = 0;
    std::int64_t size_matched_microunits = 0;
    std::int64_t trade_size_microunits = 0;
    std::int32_t price_e4 = 0;
    std::int64_t exchange_timestamp_ms = 0;
    std::int64_t receive_monotonic_ns = 0;
};

struct UserWsParseResult {
    std::size_t output_count = 0;
    std::uint8_t invalid_frame = 0;
    std::uint8_t output_overflow = 0;
};

class UserWsParser final {
public:
    UserWsParser();
    ~UserWsParser();
    UserWsParser(const UserWsParser&) = delete;
    UserWsParser& operator=(const UserWsParser&) = delete;

    [[nodiscard]] UserWsParseResult parse(
        std::string_view payload,
        const pm::fast::FeedReceiveStamp& receive,
        std::span<UserWsEvent> output) noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

struct UserWsCredentials {
    std::string api_key;
    std::string secret;
    std::string passphrase;
};

struct UserWsSnapshot {
    std::uint8_t connected = 0;
    std::uint64_t messages = 0;
    std::uint64_t reconnects = 0;
    std::uint64_t errors = 0;
};

class UserWebSocketFeed final {
public:
    using MessageHandler = std::function<void(
        std::string_view, const pm::fast::FeedReceiveStamp&)>;
    using ErrorHandler = std::function<void(std::string_view)>;

    UserWebSocketFeed(UserWsCredentials credentials,
                      std::vector<std::string> markets,
                      MessageHandler on_message,
                      ErrorHandler on_error = {});
    ~UserWebSocketFeed();

    UserWebSocketFeed(const UserWebSocketFeed&) = delete;
    UserWebSocketFeed& operator=(const UserWebSocketFeed&) = delete;

    void start();
    void stop();
    [[nodiscard]] UserWsSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::is_trivially_copyable_v<UserWsEvent>);
static_assert(std::is_standard_layout_v<UserWsEvent>);

} // namespace pm::v7
