#include "pm/v7_user_ws.hpp"

#include <boost/json.hpp>
#include <boost/json/static_resource.hpp>

#include <algorithm>
#include <array>
#include <stdexcept>

namespace pm::v7::user_ws {
namespace {
namespace json = boost::json;
constexpr std::size_t kArenaBytes = 256 * 1024;

[[nodiscard]] const json::value* field(
    const json::object& object,
    std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] std::string_view text(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& string = value->as_string();
    return {string.data(), string.size()};
}

template <std::size_t N>
[[nodiscard]] bool copy(FixedText<N>& output, std::string_view value) noexcept {
    if (value.size() > N || value.size() > 0xffffU) return false;
    std::copy(value.begin(), value.end(), output.data.begin());
    output.size = static_cast<std::uint16_t>(value.size());
    return true;
}

[[nodiscard]] OrderEventType order_type(std::string_view value) noexcept {
    if (value == "PLACEMENT") return OrderEventType::Placement;
    if (value == "UPDATE") return OrderEventType::Update;
    if (value == "CANCELLATION") return OrderEventType::Cancellation;
    return OrderEventType::Unknown;
}

[[nodiscard]] TradeStatus trade_status(std::string_view value) noexcept {
    if (value == "MATCHED") return TradeStatus::Matched;
    if (value == "MINED") return TradeStatus::Mined;
    if (value == "CONFIRMED") return TradeStatus::Confirmed;
    if (value == "RETRYING") return TradeStatus::Retrying;
    if (value == "FAILED") return TradeStatus::Failed;
    return TradeStatus::Unknown;
}
} // namespace

DecodeResult decode(
    std::string_view payload,
    std::int64_t receive_monotonic_ns) noexcept {
    DecodeResult output;
    if (receive_monotonic_ns <= 0 || payload.empty()) {
        output.invalid = 1;
        return output;
    }
    if (payload == kHeartbeatResponse) {
        output.event.kind = EventKind::Pong;
        output.event.receive_monotonic_ns = receive_monotonic_ns;
        output.recognized = 1;
        return output;
    }

    try {
        thread_local std::array<unsigned char, kArenaBytes> arena{};
        json::static_resource resource(arena.data(), arena.size());
        boost::system::error_code error;
        const json::value root = json::parse(payload, error, &resource);
        if (error || !root.is_object()) {
            output.invalid = 1;
            return output;
        }
        const auto& object = root.as_object();
        const auto event_type = text(field(object, "event_type"));
        if (event_type != "order" && event_type != "trade") return output;

        output.event.receive_monotonic_ns = receive_monotonic_ns;
        if (!copy(output.event.id, text(field(object, "id")))
            || !copy(output.event.market, text(field(object, "market")))
            || !copy(output.event.asset_id, text(field(object, "asset_id")))
            || output.event.id.size == 0) {
            output.invalid = 1;
            return output;
        }
        const auto side = text(field(object, "side"));
        if (side == "BUY") output.event.buy_side = 1;
        else if (side != "SELL") {
            output.invalid = 1;
            return output;
        }

        if (event_type == "order") {
            output.event.kind = EventKind::Order;
            if (!copy(output.event.price, text(field(object, "price")))
                || !copy(output.event.original_size, text(field(object, "original_size")))
                || !copy(output.event.size_matched, text(field(object, "size_matched")))) {
                output.invalid = 1;
                return output;
            }
            output.event.order_type = order_type(text(field(object, "type")));
            if (output.event.order_type == OrderEventType::Unknown) {
                output.invalid = 1;
                return output;
            }
        } else {
            output.event.kind = EventKind::Trade;
            if (!copy(output.event.price, text(field(object, "price")))
                || !copy(output.event.trade_size, text(field(object, "size")))) {
                output.invalid = 1;
                return output;
            }
            output.event.trade_status = trade_status(text(field(object, "status")));
            if (output.event.trade_status == TradeStatus::Unknown) {
                output.invalid = 1;
                return output;
            }
        }
        output.recognized = 1;
        return output;
    } catch (...) {
        output.invalid = 1;
        return output;
    }
}

std::string subscription_json(
    CredentialsView credentials,
    std::span<const std::string_view> condition_ids) {
    if (credentials.api_key.empty()
        || credentials.secret.empty()
        || credentials.passphrase.empty()) {
        throw std::invalid_argument(
            "Polymarket user websocket credentials must be non-empty");
    }

    json::object auth;
    auth.emplace("apiKey", credentials.api_key);
    auth.emplace("secret", credentials.secret);
    auth.emplace("passphrase", credentials.passphrase);

    json::object root;
    root.emplace("auth", std::move(auth));
    if (!condition_ids.empty()) {
        json::array markets;
        markets.reserve(condition_ids.size());
        for (const auto condition_id : condition_ids) {
            if (condition_id.empty()) throw std::invalid_argument("empty condition id");
            markets.emplace_back(condition_id);
        }
        root.emplace("markets", std::move(markets));
    }
    root.emplace("type", "user");
    return json::serialize(root);
}

} // namespace pm::v7::user_ws
