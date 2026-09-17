#include "pm/v7_user_ws.hpp"

#include <boost/json.hpp>
#include <boost/json/static_resource.hpp>
#include <boost/system/error_code.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <memory>
#include <vector>

namespace pm::v7 {
namespace {
namespace json = boost::json;
constexpr std::size_t kArenaBytes = 2U * 1024U * 1024U;

[[nodiscard]] const json::value* field(
    const json::object& object, std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] const json::value* first_field(
    const json::object& object,
    std::initializer_list<std::string_view> keys) noexcept {
    for (const auto key : keys) {
        if (const auto* value = field(object, key); value != nullptr) return value;
    }
    return nullptr;
}

[[nodiscard]] std::string_view text(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& string = value->as_string();
    return {string.data(), string.size()};
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

template <std::size_t Capacity>
[[nodiscard]] bool copy_text(const json::value* value,
                             FixedUserText<Capacity>& output) noexcept {
    const auto source = text(value);
    if (source.empty() || source.size() >= Capacity
        || source.size() > std::numeric_limits<std::uint16_t>::max()) {
        return false;
    }
    std::copy(source.begin(), source.end(), output.bytes.begin());
    output.size = static_cast<std::uint16_t>(source.size());
    return true;
}

[[nodiscard]] bool parse_decimal_scaled(std::string_view source,
                                        std::int64_t scale,
                                        std::int64_t& output) noexcept {
    if (source.empty() || scale <= 0) return false;
    std::size_t position = 0;
    bool negative = false;
    if (source[position] == '+' || source[position] == '-') {
        negative = source[position] == '-';
        if (++position == source.size()) return false;
    }
    std::int64_t whole = 0;
    bool any = false;
    while (position < source.size() && source[position] >= '0'
           && source[position] <= '9') {
        any = true;
        const int digit = source[position++] - '0';
        if (whole > (std::numeric_limits<std::int64_t>::max() - digit) / 10)
            return false;
        whole = whole * 10 + digit;
    }
    if (!any) return false;

    int scale_digits = 0;
    for (auto value = scale; value > 1; value /= 10) {
        if (value % 10 != 0) return false;
        ++scale_digits;
    }
    std::int64_t fraction = 0;
    int fraction_digits = 0;
    if (position < source.size() && source[position] == '.') {
        ++position;
        while (position < source.size() && source[position] >= '0'
               && source[position] <= '9') {
            const int digit = source[position++] - '0';
            if (fraction_digits < scale_digits) {
                fraction = fraction * 10 + digit;
                ++fraction_digits;
            }
        }
    }
    if (position != source.size()) return false;
    while (fraction_digits < scale_digits) {
        fraction *= 10;
        ++fraction_digits;
    }
    if (whole > (std::numeric_limits<std::int64_t>::max() - fraction) / scale)
        return false;
    const std::int64_t scaled = whole * scale + fraction;
    output = negative ? -scaled : scaled;
    return true;
}

[[nodiscard]] bool parse_scaled(const json::value* value,
                                std::int64_t scale,
                                std::int64_t& output) noexcept {
    if (value == nullptr) return false;
    if (value->is_string()) return parse_decimal_scaled(text(value), scale, output);
    long double raw = 0.0L;
    if (value->is_double()) raw = value->as_double();
    else if (value->is_int64()) raw = value->as_int64();
    else if (value->is_uint64()) raw = value->as_uint64();
    else return false;
    if (!std::isfinite(static_cast<double>(raw))) return false;
    const long double scaled = raw * static_cast<long double>(scale);
    if (scaled < static_cast<long double>(std::numeric_limits<std::int64_t>::min())
        || scaled > static_cast<long double>(std::numeric_limits<std::int64_t>::max()))
        return false;
    output = static_cast<std::int64_t>(std::llround(scaled));
    return true;
}

[[nodiscard]] bool parse_integer(const json::value* value,
                                 std::int64_t& output) noexcept {
    return parse_scaled(value, 1, output);
}

[[nodiscard]] Side parse_side(const json::value* value) noexcept {
    const auto source = text(value);
    if (ieq(source, "BUY")) return Side::Buy;
    if (ieq(source, "SELL")) return Side::Sell;
    return Side::None;
}

[[nodiscard]] UserOrderAction parse_action(std::string_view source) noexcept {
    if (ieq(source, "PLACEMENT")) return UserOrderAction::Placement;
    if (ieq(source, "UPDATE")) return UserOrderAction::Update;
    if (ieq(source, "CANCELLATION")) return UserOrderAction::Cancellation;
    return UserOrderAction::Unknown;
}

[[nodiscard]] UserOrderStatus parse_order_status(std::string_view source) noexcept {
    if (ieq(source, "LIVE")) return UserOrderStatus::Live;
    if (ieq(source, "MATCHED")) return UserOrderStatus::Matched;
    if (ieq(source, "DELAYED")) return UserOrderStatus::Delayed;
    if (ieq(source, "UNMATCHED")) return UserOrderStatus::Unmatched;
    if (ieq(source, "CANCELED") || ieq(source, "CANCELLED"))
        return UserOrderStatus::Canceled;
    return UserOrderStatus::Unknown;
}

[[nodiscard]] UserTradeStatus parse_trade_status(std::string_view source) noexcept {
    if (ieq(source, "MATCHED") || ieq(source, "TRADE_STATUS_MATCHED"))
        return UserTradeStatus::Matched;
    if (ieq(source, "MATCHED_NOT_BROADCASTED")
        || ieq(source, "TRADE_STATUS_MATCHED_NOT_BROADCASTED"))
        return UserTradeStatus::MatchedNotBroadcasted;
    if (ieq(source, "MINED") || ieq(source, "TRADE_STATUS_MINED"))
        return UserTradeStatus::Mined;
    if (ieq(source, "CONFIRMED") || ieq(source, "TRADE_STATUS_CONFIRMED"))
        return UserTradeStatus::Confirmed;
    if (ieq(source, "RETRYING") || ieq(source, "TRADE_STATUS_RETRYING"))
        return UserTradeStatus::Retrying;
    if (ieq(source, "FAILED") || ieq(source, "TRADE_STATUS_FAILED"))
        return UserTradeStatus::Failed;
    return UserTradeStatus::Unknown;
}

void emit(UserWsParseResult& result, std::span<UserWsEvent> output,
          const UserWsEvent& event) noexcept {
    if (result.output_count >= output.size()) {
        result.output_overflow = 1;
        return;
    }
    output[result.output_count++] = event;
}

} // namespace

struct UserWsParser::Impl {
    std::vector<unsigned char> arena{kArenaBytes};
    std::unique_ptr<json::static_resource> resource =
        std::make_unique<json::static_resource>(arena.data(), arena.size());
};

UserWsParser::UserWsParser() : impl_(std::make_unique<Impl>()) {}
UserWsParser::~UserWsParser() = default;

UserWsParseResult UserWsParser::parse(
    std::string_view payload,
    const pm::fast::FeedReceiveStamp& receive,
    std::span<UserWsEvent> output) noexcept {

    UserWsParseResult result;
    if (payload.empty() || receive.monotonic_ns <= 0 || output.empty()) {
        result.invalid_frame = 1;
        return result;
    }
    try {
        impl_->resource->release();
        boost::system::error_code error;
        const auto root_value = json::parse(payload, error, impl_->resource.get());
        if (error || !root_value.is_object()) {
            result.invalid_frame = 1;
            return result;
        }
        const auto& root = root_value.as_object();
        const json::object* body = &root;
        if (const auto* wrapped = field(root, "payload");
            wrapped != nullptr && wrapped->is_object()) {
            body = &wrapped->as_object();
        }
        auto event_type = text(field(*body, "event_type"));
        if (event_type.empty()) event_type = text(field(root, "event_type"));
        if (event_type.empty() && body != &root) event_type = text(field(root, "type"));

        std::int64_t timestamp_ms = 0;
        (void)parse_integer(first_field(*body, {"timestamp", "matchtime", "match_time"}),
                            timestamp_ms);

        if (ieq(event_type, "order")) {
            UserWsEvent event;
            event.kind = UserWsEventKind::Order;
            event.receive_monotonic_ns = receive.monotonic_ns;
            event.exchange_timestamp_ms = timestamp_ms;
            event.order_action = parse_action(text(field(*body, "type")));
            event.order_status = parse_order_status(text(field(*body, "status")));
            event.side = parse_side(field(*body, "side"));
            std::int64_t price = 0;
            if (!copy_text(first_field(*body, {"id", "order_id"}), event.order_id)
                || !copy_text(field(*body, "market"), event.market)
                || !copy_text(first_field(*body, {"asset_id", "assetId"}), event.asset_id)
                || event.side == Side::None
                || !parse_scaled(field(*body, "price"), 10'000, price)
                || price <= 0 || price >= 10'000) {
                result.invalid_frame = 1;
                return result;
            }
            event.price_e4 = static_cast<std::int32_t>(price);
            (void)parse_scaled(first_field(*body, {"original_size", "originalSize"}),
                               1'000'000, event.original_size_microunits);
            (void)parse_scaled(first_field(*body, {"size_matched", "sizeMatched"}),
                               1'000'000, event.size_matched_microunits);
            emit(result, output, event);
            return result;
        }

        if (ieq(event_type, "trade")) {
            UserWsEvent base;
            base.kind = UserWsEventKind::Trade;
            base.receive_monotonic_ns = receive.monotonic_ns;
            base.exchange_timestamp_ms = timestamp_ms;
            base.trade_status = parse_trade_status(text(field(*body, "status")));
            base.side = parse_side(field(*body, "side"));
            std::int64_t price = 0;
            if (!copy_text(first_field(*body, {"id", "trade_id"}), base.trade_id)
                || !copy_text(field(*body, "market"), base.market)
                || !copy_text(first_field(*body, {"asset_id", "assetId"}), base.asset_id)
                || base.side == Side::None
                || !parse_scaled(field(*body, "price"), 10'000, price)
                || !parse_scaled(field(*body, "size"), 1'000'000,
                                 base.trade_size_microunits)
                || price <= 0 || price >= 10'000 || base.trade_size_microunits <= 0) {
                result.invalid_frame = 1;
                return result;
            }
            base.price_e4 = static_cast<std::int32_t>(price);

            // Emit the taker correlation when present. The downstream venue map
            // ignores foreign order IDs, so this is safe for maker-side events.
            if (const auto* taker = first_field(*body, {"taker_order_id", "takerOrderId"});
                taker != nullptr && !text(taker).empty()) {
                UserWsEvent event = base;
                if (!copy_text(taker, event.order_id)
                    || !copy_text(taker, event.taker_order_id)) {
                    result.invalid_frame = 1;
                    return result;
                }
                emit(result, output, event);
            }

            // A user may be maker. Emit one correlation record per maker order;
            // the exact trade/order pair is later deduplicated before OMS apply.
            if (const auto* makers = first_field(*body, {"maker_orders", "makerOrders"});
                makers != nullptr && makers->is_array()) {
                for (const auto& raw : makers->as_array()) {
                    if (!raw.is_object()) continue;
                    const auto& maker = raw.as_object();
                    UserWsEvent event = base;
                    if (!copy_text(first_field(maker, {"order_id", "orderId"}),
                                   event.order_id)) continue;
                    std::int64_t matched = 0;
                    if (parse_scaled(first_field(maker, {"matched_amount", "matchedAmount"}),
                                     1'000'000, matched) && matched > 0) {
                        event.trade_size_microunits = matched;
                    }
                    emit(result, output, event);
                }
            }
            return result;
        }
        return result;
    } catch (...) {
        result.invalid_frame = 1;
        return result;
    }
}

} // namespace pm::v7
