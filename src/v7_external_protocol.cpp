#include "pm/v7_external_protocol.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>

namespace pm::v7::external_fair {
namespace {
namespace json = boost::json;
constexpr std::size_t kJsonArenaBytes = 512 * 1024;

[[nodiscard]] const json::value* find_value(const json::object& object,
                                            std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] std::string_view string_view_of(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& str = value->as_string();
    return std::string_view(str.data(), str.size());
}

[[nodiscard]] bool ascii_ieq(std::string_view lhs, std::string_view rhs) noexcept {
    if (lhs.size() != rhs.size()) return false;
    for (std::size_t i = 0; i < lhs.size(); ++i) {
        unsigned char a = static_cast<unsigned char>(lhs[i]);
        unsigned char b = static_cast<unsigned char>(rhs[i]);
        if (a >= 'A' && a <= 'Z') a = static_cast<unsigned char>(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = static_cast<unsigned char>(b - 'A' + 'a');
        if (a != b) return false;
    }
    return true;
}

[[nodiscard]] bool parse_double(const json::value* value, double& out) noexcept {
    if (value == nullptr) return false;
    if (value->is_double()) out = value->as_double();
    else if (value->is_int64()) out = static_cast<double>(value->as_int64());
    else if (value->is_uint64()) out = static_cast<double>(value->as_uint64());
    else if (value->is_string()) {
        const auto text = string_view_of(value);
        if (text.empty() || text.size() >= 96) return false;
        std::array<char, 96> buffer{};
        std::memcpy(buffer.data(), text.data(), text.size());
        char* end = nullptr;
        out = std::strtod(buffer.data(), &end);
        if (end != buffer.data() + static_cast<std::ptrdiff_t>(text.size())) return false;
    } else return false;
    return std::isfinite(out);
}

[[nodiscard]] bool parse_u64(const json::value* value, std::uint64_t& out) noexcept {
    if (value == nullptr) return false;
    if (value->is_uint64()) {
        out = value->as_uint64();
        return true;
    }
    if (value->is_int64()) {
        const auto raw = value->as_int64();
        if (raw < 0) return false;
        out = static_cast<std::uint64_t>(raw);
        return true;
    }
    if (!value->is_string()) return false;
    const auto text = string_view_of(value);
    if (text.empty()) return false;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), out);
    return result.ec == std::errc{} && result.ptr == text.data() + text.size();
}

[[nodiscard]] bool parse_i64(const json::value* value, std::int64_t& out) noexcept {
    if (value == nullptr) return false;
    if (value->is_int64()) {
        out = value->as_int64();
        return true;
    }
    if (value->is_uint64()) {
        const auto raw = value->as_uint64();
        if (raw > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return false;
        out = static_cast<std::int64_t>(raw);
        return true;
    }
    if (!value->is_string()) return false;
    const auto text = string_view_of(value);
    if (text.empty()) return false;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), out);
    return result.ec == std::errc{} && result.ptr == text.data() + text.size();
}

[[nodiscard]] bool parse_bool(const json::value* value, bool& out) noexcept {
    if (value == nullptr || !value->is_bool()) return false;
    out = value->as_bool();
    return true;
}

[[nodiscard]] std::int64_t milliseconds_to_ns(std::int64_t value) noexcept {
    if (value <= 0 || value > std::numeric_limits<std::int64_t>::max() / 1'000'000LL) return 0;
    return value * 1'000'000LL;
}

// Howard Hinnant civil-date transform, valid for Gregorian dates. Returns days
// relative to 1970-01-01 without libc timezone state.
[[nodiscard]] std::int64_t days_from_civil(int year, unsigned month, unsigned day) noexcept {
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(year - era * 400);
    const unsigned doy = (153U * (month + (month > 2 ? static_cast<unsigned>(-3) : 9U)) + 2U) / 5U
        + day - 1U;
    const unsigned doe = yoe * 365U + yoe / 4U - yoe / 100U + doy;
    return static_cast<std::int64_t>(era) * 146097LL + static_cast<std::int64_t>(doe) - 719468LL;
}

[[nodiscard]] bool parse_two(std::string_view text, std::size_t offset, int& out) noexcept {
    if (offset + 2 > text.size()) return false;
    if (text[offset] < '0' || text[offset] > '9'
        || text[offset + 1] < '0' || text[offset + 1] > '9') return false;
    out = (text[offset] - '0') * 10 + (text[offset + 1] - '0');
    return true;
}

[[nodiscard]] bool parse_four(std::string_view text, std::size_t offset, int& out) noexcept {
    if (offset + 4 > text.size()) return false;
    out = 0;
    for (std::size_t i = 0; i < 4; ++i) {
        const char ch = text[offset + i];
        if (ch < '0' || ch > '9') return false;
        out = out * 10 + (ch - '0');
    }
    return true;
}

[[nodiscard]] std::int64_t parse_iso8601_ns(std::string_view text) noexcept {
    // Required shape: YYYY-MM-DDTHH:MM:SS[.fffffffff]Z
    if (text.size() < 20 || text[4] != '-' || text[7] != '-'
        || (text[10] != 'T' && text[10] != 't')
        || text[13] != ':' || text[16] != ':'
        || (text.back() != 'Z' && text.back() != 'z')) return 0;
    int year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;
    if (!parse_four(text, 0, year) || !parse_two(text, 5, month)
        || !parse_two(text, 8, day) || !parse_two(text, 11, hour)
        || !parse_two(text, 14, minute) || !parse_two(text, 17, second)) return 0;
    if (month < 1 || month > 12 || day < 1 || day > 31
        || hour < 0 || hour > 23 || minute < 0 || minute > 59
        || second < 0 || second > 60) return 0;
    std::int64_t fraction_ns = 0;
    std::size_t pos = 19;
    if (pos < text.size() - 1) {
        if (text[pos] != '.') return 0;
        ++pos;
        int digits = 0;
        while (pos < text.size() - 1 && digits < 9) {
            const char ch = text[pos++];
            if (ch < '0' || ch > '9') return 0;
            fraction_ns = fraction_ns * 10 + (ch - '0');
            ++digits;
        }
        while (digits < 9) {
            fraction_ns *= 10;
            ++digits;
        }
        if (pos != text.size() - 1) return 0;
    }
    const std::int64_t days = days_from_civil(year, static_cast<unsigned>(month),
                                               static_cast<unsigned>(day));
    const std::int64_t seconds = days * 86400LL + hour * 3600LL + minute * 60LL + second;
    if (seconds <= 0 || seconds > std::numeric_limits<std::int64_t>::max() / 1'000'000'000LL) return 0;
    return seconds * 1'000'000'000LL + fraction_ns;
}

void emit(ExternalDecodeResult& result,
          std::span<ExternalVenueEvent> output,
          const ExternalVenueEvent& event) noexcept {
    if (result.output_count >= output.size()) {
        result.output_overflow = 1;
        return;
    }
    output[result.output_count++] = event;
}

[[nodiscard]] bool parse_level1(const json::value* raw,
                                double& price, double& quantity) noexcept {
    if (raw == nullptr || !raw->is_array()) return false;
    const auto& level = raw->as_array();
    if (level.size() < 2) return false;
    return parse_double(&level[0], price) && parse_double(&level[1], quantity)
        && price > 0.0 && quantity >= 0.0;
}

void decode_binance(const json::object& root,
                    std::uint64_t asset_handle,
                    std::uint64_t epoch,
                    std::int64_t receive_ns,
                    std::int64_t wall_ns,
                    ExternalDecodeResult& result,
                    std::span<ExternalVenueEvent> output) noexcept {
    const json::object* object = &root;
    const auto* wrapped = find_value(root, "data");
    if (wrapped != nullptr && wrapped->is_object()) object = &wrapped->as_object();

    const auto event_type = string_view_of(find_value(*object, "e"));
    if (ascii_ieq(event_type, "aggTrade")) {
        ++result.recognized_events;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BinanceSpot;
        event.event_type = ExternalEventType::Trade;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_timestamp = wall_ns;
        std::uint64_t trade_id = 0;
        (void)parse_u64(find_value(*object, "a"), trade_id);
        event.source_sequence = trade_id;
        std::int64_t exchange_ms = 0;
        if (!parse_i64(find_value(*object, "T"), exchange_ms))
            (void)parse_i64(find_value(*object, "E"), exchange_ms);
        event.exchange_event_timestamp = milliseconds_to_ns(exchange_ms);
        bool buyer_is_maker = false;
        if (!parse_double(find_value(*object, "p"), event.trade_price)
            || !parse_double(find_value(*object, "q"), event.trade_size)
            || !parse_bool(find_value(*object, "m"), buyer_is_maker)
            || event.trade_price <= 0.0 || event.trade_size <= 0.0) {
            result.invalid_frame = 1;
            return;
        }
        event.trade_side = buyer_is_maker ? -1 : 1;
        event.healthy = 1;
        emit(result, output, event);
        return;
    }

    // Binance bookTicker has no source event timestamp in the documented
    // payload; preserve that fact as 0 rather than fabricating event time.
    if (find_value(*object, "u") != nullptr && find_value(*object, "b") != nullptr
        && find_value(*object, "a") != nullptr) {
        ++result.recognized_events;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BinanceSpot;
        event.event_type = ExternalEventType::BookTop;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_timestamp = wall_ns;
        (void)parse_u64(find_value(*object, "u"), event.source_sequence);
        if (!parse_double(find_value(*object, "b"), event.bid)
            || !parse_double(find_value(*object, "a"), event.ask)
            || !parse_double(find_value(*object, "B"), event.bid_size)
            || !parse_double(find_value(*object, "A"), event.ask_size)
            || event.bid <= 0.0 || event.ask <= event.bid
            || event.bid_size < 0.0 || event.ask_size < 0.0) {
            result.invalid_frame = 1;
            return;
        }
        event.healthy = 1;
        emit(result, output, event);
        return;
    }
    ++result.ignored_events;
}

void decode_coinbase(const json::object& root,
                     std::uint64_t asset_handle,
                     std::uint64_t epoch,
                     std::int64_t receive_ns,
                     std::int64_t wall_ns,
                     ExternalDecodeResult& result,
                     std::span<ExternalVenueEvent> output) noexcept {
    const auto channel = string_view_of(find_value(root, "channel"));
    std::uint64_t sequence = 0;
    (void)parse_u64(find_value(root, "sequence_num"), sequence);
    const std::int64_t root_ts = parse_iso8601_ns(string_view_of(find_value(root, "timestamp")));
    const auto* events_value = find_value(root, "events");
    if (events_value == nullptr || !events_value->is_array()) {
        ++result.ignored_events;
        return;
    }

    if (ascii_ieq(channel, "ticker")) {
        ++result.recognized_events;
        for (const auto& raw_event : events_value->as_array()) {
            if (!raw_event.is_object()) continue;
            const auto* tickers_value = find_value(raw_event.as_object(), "tickers");
            if (tickers_value == nullptr || !tickers_value->is_array()) continue;
            for (const auto& raw_ticker : tickers_value->as_array()) {
                if (!raw_ticker.is_object()) continue;
                const auto& ticker = raw_ticker.as_object();
                ExternalVenueEvent event;
                event.asset_handle = asset_handle;
                event.source_sequence = sequence;
                event.connection_epoch = epoch;
                event.venue = VenueId::CoinbaseSpot;
                event.event_type = ExternalEventType::BookTop;
                event.exchange_event_timestamp = root_ts;
                event.local_receive_monotonic_ns = receive_ns;
                event.local_receive_wall_timestamp = wall_ns;
                if (!parse_double(find_value(ticker, "best_bid"), event.bid)
                    || !parse_double(find_value(ticker, "best_ask"), event.ask)
                    || !parse_double(find_value(ticker, "best_bid_quantity"), event.bid_size)
                    || !parse_double(find_value(ticker, "best_ask_quantity"), event.ask_size)
                    || event.bid <= 0.0 || event.ask <= event.bid
                    || event.bid_size < 0.0 || event.ask_size < 0.0) {
                    result.invalid_frame = 1;
                    continue;
                }
                event.healthy = 1;
                emit(result, output, event);
            }
        }
        return;
    }

    if (ascii_ieq(channel, "market_trades")) {
        ++result.recognized_events;
        for (const auto& raw_event : events_value->as_array()) {
            if (!raw_event.is_object()) continue;
            const auto* trades_value = find_value(raw_event.as_object(), "trades");
            if (trades_value == nullptr || !trades_value->is_array()) continue;
            for (const auto& raw_trade : trades_value->as_array()) {
                if (!raw_trade.is_object()) continue;
                const auto& trade = raw_trade.as_object();
                ExternalVenueEvent event;
                event.asset_handle = asset_handle;
                event.connection_epoch = epoch;
                event.venue = VenueId::CoinbaseSpot;
                event.event_type = ExternalEventType::Trade;
                event.exchange_event_timestamp = parse_iso8601_ns(
                    string_view_of(find_value(trade, "time")));
                if (event.exchange_event_timestamp == 0) event.exchange_event_timestamp = root_ts;
                event.local_receive_monotonic_ns = receive_ns;
                event.local_receive_wall_timestamp = wall_ns;
                if (!parse_double(find_value(trade, "price"), event.trade_price)
                    || !parse_double(find_value(trade, "size"), event.trade_size)
                    || event.trade_price <= 0.0 || event.trade_size <= 0.0) {
                    result.invalid_frame = 1;
                    continue;
                }
                // Coinbase documents side as the MAKER side, so the taker sign
                // used by aggregate trade imbalance is the opposite side.
                const auto maker_side = string_view_of(find_value(trade, "side"));
                if (ascii_ieq(maker_side, "BUY")) event.trade_side = -1;
                else if (ascii_ieq(maker_side, "SELL")) event.trade_side = 1;
                else {
                    result.invalid_frame = 1;
                    continue;
                }
                event.healthy = 1;
                emit(result, output, event);
            }
        }
        return;
    }
    ++result.ignored_events;
}

void decode_bybit(const json::object& root,
                  std::uint64_t asset_handle,
                  std::uint64_t epoch,
                  std::int64_t receive_ns,
                  std::int64_t wall_ns,
                  ExternalDecodeResult& result,
                  std::span<ExternalVenueEvent> output) noexcept {
    const auto topic = string_view_of(find_value(root, "topic"));
    std::int64_t root_ms = 0;
    (void)parse_i64(find_value(root, "ts"), root_ms);
    const std::int64_t root_ts = milliseconds_to_ns(root_ms);
    const auto* data_value = find_value(root, "data");

    if (topic.starts_with("orderbook.1.") && data_value != nullptr && data_value->is_object()) {
        ++result.recognized_events;
        const auto& data = data_value->as_object();
        const auto* bids = find_value(data, "b");
        const auto* asks = find_value(data, "a");
        if (bids == nullptr || asks == nullptr || !bids->is_array() || !asks->is_array()
            || bids->as_array().empty() || asks->as_array().empty()) {
            result.invalid_frame = 1;
            return;
        }
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BybitSpot;
        event.event_type = ExternalEventType::BookTop;
        event.exchange_event_timestamp = root_ts;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_timestamp = wall_ns;
        (void)parse_u64(find_value(data, "u"), event.source_sequence);
        if (!parse_level1(&bids->as_array()[0], event.bid, event.bid_size)
            || !parse_level1(&asks->as_array()[0], event.ask, event.ask_size)
            || event.ask <= event.bid) {
            result.invalid_frame = 1;
            return;
        }
        event.healthy = 1;
        emit(result, output, event);
        return;
    }

    if (topic.starts_with("publicTrade.") && data_value != nullptr && data_value->is_array()) {
        ++result.recognized_events;
        for (const auto& raw_trade : data_value->as_array()) {
            if (!raw_trade.is_object()) continue;
            const auto& trade = raw_trade.as_object();
            ExternalVenueEvent event;
            event.asset_handle = asset_handle;
            event.connection_epoch = epoch;
            event.venue = VenueId::BybitSpot;
            event.event_type = ExternalEventType::Trade;
            event.local_receive_monotonic_ns = receive_ns;
            event.local_receive_wall_timestamp = wall_ns;
            std::int64_t trade_ms = 0;
            (void)parse_i64(find_value(trade, "T"), trade_ms);
            event.exchange_event_timestamp = trade_ms > 0 ? milliseconds_to_ns(trade_ms) : root_ts;
            if (!parse_double(find_value(trade, "p"), event.trade_price)
                || !parse_double(find_value(trade, "v"), event.trade_size)
                || event.trade_price <= 0.0 || event.trade_size <= 0.0) {
                result.invalid_frame = 1;
                continue;
            }
            const auto taker_side = string_view_of(find_value(trade, "S"));
            if (ascii_ieq(taker_side, "Buy")) event.trade_side = 1;
            else if (ascii_ieq(taker_side, "Sell")) event.trade_side = -1;
            else {
                result.invalid_frame = 1;
                continue;
            }
            event.healthy = 1;
            emit(result, output, event);
        }
        return;
    }
    ++result.ignored_events;
}

} // namespace

ExternalDecodeResult decode_external_venue_frame(
    VenueId venue,
    std::uint64_t asset_handle,
    std::uint64_t connection_epoch,
    std::int64_t local_receive_monotonic_ns,
    std::int64_t local_receive_wall_ns,
    std::string_view payload,
    std::span<ExternalVenueEvent> output) noexcept {

    ExternalDecodeResult result;
    if (venue == VenueId::Unknown || asset_handle == 0 || connection_epoch == 0
        || local_receive_monotonic_ns <= 0 || local_receive_wall_ns <= 0
        || payload.empty()) {
        result.invalid_frame = 1;
        return result;
    }

    try {
        thread_local std::array<unsigned char, kJsonArenaBytes> arena{};
        json::static_resource resource(arena.data(), arena.size());
        boost::system::error_code error;
        const json::value root = json::parse(payload, error, &resource);
        if (error || !root.is_object()) {
            result.invalid_frame = 1;
            return result;
        }
        const auto& object = root.as_object();
        switch (venue) {
            case VenueId::BinanceSpot:
                decode_binance(object, asset_handle, connection_epoch,
                               local_receive_monotonic_ns, local_receive_wall_ns,
                               result, output);
                break;
            case VenueId::CoinbaseSpot:
                decode_coinbase(object, asset_handle, connection_epoch,
                                local_receive_monotonic_ns, local_receive_wall_ns,
                                result, output);
                break;
            case VenueId::BybitSpot:
                decode_bybit(object, asset_handle, connection_epoch,
                             local_receive_monotonic_ns, local_receive_wall_ns,
                             result, output);
                break;
            case VenueId::Unknown:
            default:
                result.invalid_frame = 1;
                break;
        }
    } catch (const std::bad_alloc&) {
        result.invalid_frame = 1;
        result.arena_exhausted = 1;
    } catch (...) {
        result.invalid_frame = 1;
    }
    return result;
}

} // namespace pm::v7::external_fair
