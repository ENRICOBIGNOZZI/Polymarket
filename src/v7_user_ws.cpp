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

[[nodiscard]] bool ieq(std::string_view lhs, std::string_view rhs) noexcept {
    if (lhs.size() != rhs.size()) return false;
    for (std::size_t i = 0; i < lhs.size(); ++i) {
        char a = lhs[i];
        char b = rhs[i];
        if (a >= 'A' && a <= 'Z') a = static_cast<char>(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = static_cast<char>(b - 'A' + 'a');
        if (a != b) return false;
    }
    return true;
}

[[nodiscard]] bool parse_side(std::string_view side, std::uint8_t& buy) noexcept {
    if (ieq(side, "BUY")) {
        buy = 1;
        return true;
    }
    if (ieq(side, "SELL")) {
        buy = 0;
        return true;
    }
    return false;
}

[[nodiscard]] OrderEventType order_type(std::string_view value) noexcept {
    if (ieq(value, "PLACEMENT")) return OrderEventType::Placement;
    if (ieq(value, "UPDATE")) return OrderEventType::Update;
    if (ieq(value, "CANCELLATION")) return OrderEventType::Cancellation;
    return OrderEventType::Unknown;
}

[[nodiscard]] TradeStatus trade_status(std::string_view value) noexcept {
    if (ieq(value, "MATCHED") || ieq(value, "TRADE_STATUS_MATCHED"))
        return TradeStatus::Matched;
    if (ieq(value, "MINED") || ieq(value, "TRADE_STATUS_MINED"))
        return TradeStatus::Mined;
    if (ieq(value, "CONFIRMED") || ieq(value, "TRADE_STATUS_CONFIRMED"))
        return TradeStatus::Confirmed;
    if (ieq(value, "RETRYING") || ieq(value, "TRADE_STATUS_RETRYING"))
        return TradeStatus::Retrying;
    if (ieq(value, "FAILED") || ieq(value, "TRADE_STATUS_FAILED"))
        return TradeStatus::Failed;
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
        const auto& wire = root.as_object();
        const json::object* object = &wire;
        auto event_type = text(field(wire, "event_type"));
        if (event_type.empty()) {
            // Current SDKs also accept the normalized {topic,type,payload}
            // envelope. Keep wire and SDK replay semantics identical.
            if (const auto* nested = field(wire, "payload");
                nested != nullptr && nested->is_object()) {
                object = &nested->as_object();
                event_type = text(field(wire, "type"));
            }
        }
        if (!ieq(event_type, "order") && !ieq(event_type, "trade")) return output;

        output.event.receive_monotonic_ns = receive_monotonic_ns;
        if (!copy(output.event.id, text(field(*object, "id")))
            || !copy(output.event.market, text(field(*object, "market")))
            || !copy(output.event.asset_id, text(field(*object, "asset_id")))
            || output.event.id.size == 0
            || !parse_side(text(field(*object, "side")), output.event.buy_side)) {
            output.invalid = 1;
            return output;
        }

        if (ieq(event_type, "order")) {
            output.event.kind = EventKind::Order;
            if (!copy(output.event.price, text(field(*object, "price")))
                || !copy(output.event.original_size, text(field(*object, "original_size")))
                || !copy(output.event.size_matched, text(field(*object, "size_matched")))) {
                output.invalid = 1;
                return output;
            }
            output.event.order_type = order_type(text(field(*object, "type")));
            if (output.event.order_type == OrderEventType::Unknown) {
                output.invalid = 1;
                return output;
            }
        } else {
            output.event.kind = EventKind::Trade;
            if (!copy(output.event.price, text(field(*object, "price")))
                || !copy(output.event.trade_size, text(field(*object, "size")))
                || !copy(output.event.taker_order_id,
                         text(field(*object, "taker_order_id")))
                || output.event.taker_order_id.size == 0) {
                output.invalid = 1;
                return output;
            }
            output.event.trade_status = trade_status(text(field(*object, "status")));
            if (output.event.trade_status == TradeStatus::Unknown) {
                output.invalid = 1;
                return output;
            }
            const auto trader_side = text(field(*object, "trader_side"));
            output.event.trader_is_taker = ieq(trader_side, "TAKER") ? 1 : 0;
            output.event.trader_is_maker = ieq(trader_side, "MAKER") ? 1 : 0;

            if (const auto* makers = field(*object, "maker_orders");
                makers != nullptr) {
                if (!makers->is_array()) {
                    output.invalid = 1;
                    return output;
                }
                const auto& array = makers->as_array();
                if (array.size() > kMaxMakerOrdersPerTrade) {
                    output.invalid = 1;
                    output.maker_order_overflow = 1;
                    return output;
                }
                for (const auto& raw : array) {
                    if (!raw.is_object()) {
                        output.invalid = 1;
                        return output;
                    }
                    const auto& maker = raw.as_object();
                    auto& match = output.event.maker_orders[output.event.maker_order_count];
                    if (!copy(match.order_id, text(field(maker, "order_id")))
                        || match.order_id.size == 0
                        || !copy(match.matched_amount, text(field(maker, "matched_amount")))
                        || match.matched_amount.size == 0
                        || !copy(match.price, text(field(maker, "price")))
                        || !copy(match.asset_id, text(field(maker, "asset_id")))
                        || !parse_side(text(field(maker, "side")), match.buy_side)) {
                        output.invalid = 1;
                        return output;
                    }
                    ++output.event.maker_order_count;
                }
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
