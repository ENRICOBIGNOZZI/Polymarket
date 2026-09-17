#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>

namespace pm::v7::user_ws {

inline constexpr std::string_view kEndpoint =
    "wss://ws-subscriptions-clob.polymarket.com/ws/user";
inline constexpr std::string_view kHeartbeatRequest = "PING";
inline constexpr std::string_view kHeartbeatResponse = "PONG";
inline constexpr std::int64_t kHeartbeatIntervalMs = 10'000;

template <std::size_t N>
struct FixedText {
    std::array<char, N> data{};
    std::uint16_t size = 0;
    [[nodiscard]] std::string_view view() const noexcept {
        return {data.data(), size};
    }
};

enum class EventKind : std::uint8_t {
    Unknown = 0,
    Order = 1,
    Trade = 2,
    Pong = 3,
};

enum class OrderEventType : std::uint8_t {
    Unknown = 0,
    Placement = 1,
    Update = 2,
    Cancellation = 3,
};

enum class TradeStatus : std::uint8_t {
    Unknown = 0,
    Matched = 1,
    Mined = 2,
    Confirmed = 3,
    Retrying = 4,
    Failed = 5,
};

struct Event {
    EventKind kind = EventKind::Unknown;
    OrderEventType order_type = OrderEventType::Unknown;
    TradeStatus trade_status = TradeStatus::Unknown;
    std::uint8_t buy_side = 0;
    std::int64_t receive_monotonic_ns = 0;
    FixedText<96> id{};
    FixedText<96> market{};
    FixedText<96> asset_id{};
    FixedText<40> price{};
    FixedText<40> original_size{};
    FixedText<40> size_matched{};
    FixedText<40> trade_size{};
};

struct DecodeResult {
    Event event{};
    std::uint8_t recognized = 0;
    std::uint8_t invalid = 0;
};

struct CredentialsView {
    std::string_view api_key;
    std::string_view secret;
    std::string_view passphrase;
};

[[nodiscard]] DecodeResult decode(
    std::string_view payload,
    std::int64_t receive_monotonic_ns) noexcept;

// Cold-path helper. Credentials are supplied by the caller at runtime and are
// never embedded in the repository. The returned JSON must be treated as secret.
[[nodiscard]] std::string subscription_json(
    CredentialsView credentials,
    std::span<const std::string_view> condition_ids = {});

static_assert(std::is_trivially_copyable_v<Event>);
static_assert(std::is_standard_layout_v<Event>);

} // namespace pm::v7::user_ws
