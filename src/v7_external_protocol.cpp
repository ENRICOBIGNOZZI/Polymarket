#include "pm/v7_external_protocol.hpp"

#include <boost/json.hpp>
#include <boost/json/parser.hpp>
#include <boost/json/static_resource.hpp>
#include <boost/json/null_resource.hpp>

#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>

namespace pm::v7::external_fair {
namespace {
namespace json = boost::json;
constexpr std::size_t kJsonArenaBytes = 512 * 1024;
constexpr std::size_t kParserScratchBytes = 16 * 1024;

struct DecoderScratch {
    std::array<unsigned char, kJsonArenaBytes> value_arena{};
    json::static_resource value_resource{value_arena.data(), value_arena.size()};
    std::array<unsigned char, kParserScratchBytes> parser_scratch{};
    json::parser parser{
        json::storage_ptr(json::get_null_resource()), json::parse_options{},
        parser_scratch.data(), parser_scratch.size()};
};

DecoderScratch& decoder_scratch() noexcept {
    thread_local DecoderScratch scratch{};
    return scratch;
}

[[nodiscard]] const json::value* find_value(const json::object& object,
                                            std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] std::string_view text_of(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& text = value->as_string();
    return {text.data(), text.size()};
}

[[nodiscard]] bool ieq(std::string_view lhs, std::string_view rhs) noexcept {
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
        const auto text = text_of(value);
        if (text.empty() || text.size() >= 96) return false;
        std::array<char, 96> buffer{};
        std::memcpy(buffer.data(), text.data(), text.size());
        char* end = nullptr;
        out = std::strtod(buffer.data(), &end);
        if (end != buffer.data() + static_cast<std::ptrdiff_t>(text.size())) return false;
    } else {
        return false;
    }
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
    const auto text = text_of(value);
    if (text.empty()) return false;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
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
    const auto text = text_of(value);
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
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

[[nodiscard]] std::int64_t days_from_civil(int year, unsigned month, unsigned day) noexcept {
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(year - era * 400);
    const int adjusted_month = static_cast<int>(month) + (month > 2 ? -3 : 9);
    const unsigned doy = static_cast<unsigned>((153 * adjusted_month + 2) / 5)
        + day - 1U;
    const unsigned doe = yoe * 365U + yoe / 4U - yoe / 100U + doy;
    return static_cast<std::int64_t>(era) * 146097LL
        + static_cast<std::int64_t>(doe) - 719468LL;
}

[[nodiscard]] bool parse_digits(std::string_view text, std::size_t offset,
                                std::size_t count, int& out) noexcept {
    if (offset + count > text.size()) return false;
    out = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const char ch = text[offset + i];
        if (ch < '0' || ch > '9') return false;
        out = out * 10 + (ch - '0');
    }
    return true;
}

[[nodiscard]] std::int64_t parse_iso8601_ns(std::string_view text) noexcept {
    if (text.size() < 20 || text[4] != '-' || text[7] != '-'
        || (text[10] != 'T' && text[10] != 't') || text[13] != ':'
        || text[16] != ':' || (text.back() != 'Z' && text.back() != 'z')) return 0;
    int year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;
    if (!parse_digits(text, 0, 4, year) || !parse_digits(text, 5, 2, month)
        || !parse_digits(text, 8, 2, day) || !parse_digits(text, 11, 2, hour)
        || !parse_digits(text, 14, 2, minute) || !parse_digits(text, 17, 2, second)) return 0;
    if (month < 1 || month > 12 || day < 1 || day > 31
        || hour > 23 || minute > 59 || second > 60) return 0;

    std::int64_t fraction_ns = 0;
    std::size_t pos = 19;
    if (pos < text.size() - 1) {
        if (text[pos++] != '.') return 0;
        int digits = 0;
        while (pos < text.size() - 1 && digits < 9) {
            const char ch = text[pos++];
            if (ch < '0' || ch > '9') return 0;
            fraction_ns = fraction_ns * 10 + (ch - '0');
            ++digits;
        }
        if (pos != text.size() - 1) return 0;
        while (digits++ < 9) fraction_ns *= 10;
    }

    const auto days = days_from_civil(year, static_cast<unsigned>(month),
                                      static_cast<unsigned>(day));
    const auto seconds = days * 86400LL + hour * 3600LL + minute * 60LL + second;
    if (seconds <= 0 || seconds > std::numeric_limits<std::int64_t>::max() / 1'000'000'000LL) return 0;
    return seconds * 1'000'000'000LL + fraction_ns;
}


void emit(ExternalDecodeResult& result, std::span<ExternalVenueEvent> output,
          const ExternalVenueEvent& event) noexcept;

struct BinanceScalarFields {
    std::string_view event_type{};
    std::string_view update_id{};
    std::string_view aggregate_trade_id{};
    std::string_view event_time{};
    std::string_view trade_time{};
    std::string_view bid{};
    std::string_view bid_size{};
    std::string_view ask{};
    std::string_view ask_size{};
    std::string_view trade_price{};
    std::string_view trade_size{};
    std::string_view buyer_maker{};
};

enum class FastBinanceState : std::uint8_t { NotApplicable=0, Handled=1, Invalid=2 };

[[nodiscard]] bool json_space(char ch) noexcept {
    return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
}

[[nodiscard]] bool skip_json_string(std::string_view text, std::size_t& pos) noexcept {
    if (pos >= text.size() || text[pos] != '"') return false;
    ++pos;
    while (pos < text.size()) {
        const char ch = text[pos++];
        if (ch == '"') return true;
        if (ch == '\\') {
            if (pos >= text.size()) return false;
            ++pos;
        }
    }
    return false;
}

[[nodiscard]] bool json_value_span(std::string_view text, std::size_t& pos,
                                   std::string_view& value) noexcept {
    while (pos < text.size() && json_space(text[pos])) ++pos;
    if (pos >= text.size()) return false;
    const std::size_t begin = pos;
    if (text[pos] == '"') {
        if (!skip_json_string(text, pos)) return false;
        value = text.substr(begin, pos - begin);
        return true;
    }
    if (text[pos] == '{' || text[pos] == '[') {
        const char open = text[pos], close = open == '{' ? '}' : ']';
        int depth = 0;
        bool in_string = false, escaped = false;
        for (; pos < text.size(); ++pos) {
            const char ch = text[pos];
            if (in_string) {
                if (escaped) escaped = false;
                else if (ch == '\\') escaped = true;
                else if (ch == '"') in_string = false;
                continue;
            }
            if (ch == '"') { in_string = true; continue; }
            if (ch == open) ++depth;
            else if (ch == close && --depth == 0) {
                ++pos;
                value = text.substr(begin, pos - begin);
                return true;
            }
        }
        return false;
    }
    while (pos < text.size() && text[pos] != ',' && text[pos] != '}'
           && text[pos] != ']' && !json_space(text[pos])) ++pos;
    if (pos == begin) return false;
    value = text.substr(begin, pos - begin);
    return true;
}

[[nodiscard]] std::string_view unquote(std::string_view raw) noexcept {
    return raw.size() >= 2 && raw.front() == '"' && raw.back() == '"'
        ? raw.substr(1, raw.size() - 2) : raw;
}

void capture_binance_scalar(BinanceScalarFields& fields, std::string_view key,
                            std::string_view raw) noexcept {
    const auto value = unquote(raw);
    if (key == "e") fields.event_type = value;
    else if (key == "u") fields.update_id = value;
    else if (key == "a") fields.aggregate_trade_id = value;
    else if (key == "E") fields.event_time = value;
    else if (key == "T") fields.trade_time = value;
    else if (key == "b") fields.bid = value;
    else if (key == "B") fields.bid_size = value;
    else if (key == "A") fields.ask_size = value;
    else if (key == "p") fields.trade_price = value;
    else if (key == "q") fields.trade_size = value;
    else if (key == "m") fields.buyer_maker = value;
    // `a` is overloaded: aggregate-trade id in aggTrade and ask price in
    // bookTicker. Preserve the raw scalar in both slots; event type decides.
    if (key == "a") fields.ask = value;
}

[[nodiscard]] bool scan_binance_object(std::string_view text,
                                       BinanceScalarFields& fields,
                                       unsigned recursion = 0) noexcept {
    if (recursion > 1) return false;
    std::size_t pos = 0;
    while (pos < text.size() && json_space(text[pos])) ++pos;
    if (pos >= text.size() || text[pos++] != '{') return false;
    while (true) {
        while (pos < text.size() && json_space(text[pos])) ++pos;
        if (pos >= text.size()) return false;
        if (text[pos] == '}') return true;
        if (text[pos] != '"') return false;
        const std::size_t key_begin = ++pos;
        bool escaped = false;
        while (pos < text.size()) {
            const char ch = text[pos];
            if (!escaped && ch == '"') break;
            if (!escaped && ch == '\\') escaped = true;
            else escaped = false;
            ++pos;
        }
        if (pos >= text.size() || escaped) return false;
        const auto key = text.substr(key_begin, pos - key_begin);
        ++pos;
        while (pos < text.size() && json_space(text[pos])) ++pos;
        if (pos >= text.size() || text[pos++] != ':') return false;
        std::string_view raw;
        if (!json_value_span(text, pos, raw)) return false;
        if (key == "data" && raw.size() >= 2 && raw.front() == '{') {
            if (!scan_binance_object(raw, fields, recursion + 1)) return false;
        } else if (key.size() == 1 || key == "E" || key == "T") {
            capture_binance_scalar(fields, key, raw);
        }
        while (pos < text.size() && json_space(text[pos])) ++pos;
        if (pos >= text.size()) return false;
        if (text[pos] == ',') { ++pos; continue; }
        if (text[pos] == '}') return true;
        return false;
    }
}

[[nodiscard]] bool parse_u64_text(std::string_view text, std::uint64_t& out) noexcept {
    if (text.empty()) return false;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}
[[nodiscard]] bool parse_i64_text(std::string_view text, std::int64_t& out) noexcept {
    if (text.empty()) return false;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}
[[nodiscard]] bool parse_double_text(std::string_view text, double& out) noexcept {
    if (text.empty()) return false;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size()
        && std::isfinite(out);
}

[[nodiscard]] FastBinanceState decode_binance_fast(
    std::uint64_t asset_handle, std::uint64_t epoch,
    std::int64_t receive_ns, std::int64_t wall_ns,
    std::string_view payload, ExternalDecodeResult& result,
    std::span<ExternalVenueEvent> output) noexcept {
    BinanceScalarFields fields;
    if (!scan_binance_object(payload, fields)) return FastBinanceState::NotApplicable;
    if (fields.event_type == "depthUpdate") {
        ++result.ignored_events;
        return FastBinanceState::Handled;
    }
    if (fields.event_type == "aggTrade") {
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BinanceSpot;
        event.event_type = ExternalEventType::Trade;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        std::int64_t exchange_ms = 0;
        if (!parse_u64_text(fields.aggregate_trade_id, event.source_sequence)
            || !(parse_i64_text(fields.trade_time, exchange_ms)
                 || parse_i64_text(fields.event_time, exchange_ms))
            || !parse_double_text(fields.trade_price, event.trade_price)
            || !parse_double_text(fields.trade_size, event.trade_size)
            || event.trade_price <= 0.0 || event.trade_size <= 0.0
            || (fields.buyer_maker != "true" && fields.buyer_maker != "false")) {
            result.invalid_frame = 1;
            return FastBinanceState::Invalid;
        }
        event.exchange_event_ns = milliseconds_to_ns(exchange_ms);
        event.trade_side = fields.buyer_maker == "true" ? -1 : 1;
        event.healthy = 1;
        ++result.recognized_events;
        emit(result, output, event);
        return FastBinanceState::Handled;
    }
    // bookTicker has no event type but does have scalar u/b/B/a/A.
    if (!fields.update_id.empty() && !fields.bid.empty() && !fields.bid_size.empty()
        && !fields.ask.empty() && !fields.ask_size.empty()) {
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BinanceSpot;
        event.event_type = ExternalEventType::BookTop;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        if (!parse_u64_text(fields.update_id, event.source_sequence)
            || !parse_double_text(fields.bid, event.bid)
            || !parse_double_text(fields.bid_size, event.bid_size)
            || !parse_double_text(fields.ask, event.ask)
            || !parse_double_text(fields.ask_size, event.ask_size)
            || event.bid <= 0.0 || event.ask <= event.bid
            || event.bid_size < 0.0 || event.ask_size < 0.0) {
            result.invalid_frame = 1;
            return FastBinanceState::Invalid;
        }
        event.healthy = 1;
        ++result.recognized_events;
        emit(result, output, event);
        return FastBinanceState::Handled;
    }
    return FastBinanceState::NotApplicable;
}

void emit(ExternalDecodeResult& result, std::span<ExternalVenueEvent> output,
          const ExternalVenueEvent& event) noexcept {
    if (result.output_count >= output.size()) {
        result.output_overflow = 1;
        return;
    }
    output[result.output_count++] = event;
}

[[nodiscard]] bool parse_level(const json::value& raw,
                               double& price, double& quantity) noexcept {
    if (!raw.is_array()) return false;
    const auto& row = raw.as_array();
    return row.size() >= 2 && parse_double(&row[0], price)
        && parse_double(&row[1], quantity) && price > 0.0 && quantity >= 0.0;
}

void decode_binance(const json::object& root, std::uint64_t asset_handle,
                    std::uint64_t epoch, std::int64_t receive_ns,
                    std::int64_t wall_ns, ExternalDecodeResult& result,
                    std::span<ExternalVenueEvent> output) noexcept {
    const json::object* object = &root;
    if (const auto* data = find_value(root, "data"); data != nullptr && data->is_object()) {
        object = &data->as_object();
    }

    if (ieq(text_of(find_value(*object, "e")), "aggTrade")) {
        ++result.recognized_events;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BinanceSpot;
        event.event_type = ExternalEventType::Trade;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        (void)parse_u64(find_value(*object, "a"), event.source_sequence);
        std::int64_t exchange_ms = 0;
        if (!parse_i64(find_value(*object, "T"), exchange_ms))
            (void)parse_i64(find_value(*object, "E"), exchange_ms);
        event.exchange_event_ns = milliseconds_to_ns(exchange_ms);
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

    // Diff-depth frames are reconstructed by the dedicated bounded Binance L2
    // observer. They are not BBO payloads and must not inflate generic decoder
    // failure counts merely because b/a are arrays instead of scalar fields.
    if (ieq(text_of(find_value(*object, "e")), "depthUpdate")) {
        ++result.ignored_events;
        return;
    }

    if (find_value(*object, "u") != nullptr && find_value(*object, "b") != nullptr
        && find_value(*object, "a") != nullptr) {
        ++result.recognized_events;
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::BinanceSpot;
        event.event_type = ExternalEventType::BookTop;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
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
        // The documented Binance bookTicker payload has no source event time.
        event.exchange_event_ns = 0;
        event.healthy = 1;
        emit(result, output, event);
        return;
    }
    ++result.ignored_events;
}

void decode_coinbase(const json::object& root, std::uint64_t asset_handle,
                     std::uint64_t epoch, std::int64_t receive_ns,
                     std::int64_t wall_ns, ExternalDecodeResult& result,
                     std::span<ExternalVenueEvent> output) noexcept {
    const auto channel = text_of(find_value(root, "channel"));
    std::uint64_t sequence = 0;
    (void)parse_u64(find_value(root, "sequence_num"), sequence);
    const auto root_ts = parse_iso8601_ns(text_of(find_value(root, "timestamp")));
    const auto* events = find_value(root, "events");
    if (events == nullptr || !events->is_array()) {
        ++result.ignored_events;
        return;
    }

    if (ieq(channel, "ticker")) {
        ++result.recognized_events;
        for (const auto& raw_event : events->as_array()) {
            if (!raw_event.is_object()) continue;
            const auto* tickers = find_value(raw_event.as_object(), "tickers");
            if (tickers == nullptr || !tickers->is_array()) continue;
            for (const auto& raw_ticker : tickers->as_array()) {
                if (!raw_ticker.is_object()) continue;
                const auto& ticker = raw_ticker.as_object();
                ExternalVenueEvent event;
                event.asset_handle = asset_handle;
                event.source_sequence = sequence;
                event.connection_epoch = epoch;
                event.venue = VenueId::CoinbaseSpot;
                event.event_type = ExternalEventType::BookTop;
                event.exchange_event_ns = root_ts;
                event.local_receive_monotonic_ns = receive_ns;
                event.local_receive_wall_ns = wall_ns;
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

    if (ieq(channel, "market_trades")) {
        ++result.recognized_events;
        for (const auto& raw_event : events->as_array()) {
            if (!raw_event.is_object()) continue;
            const auto* trades = find_value(raw_event.as_object(), "trades");
            if (trades == nullptr || !trades->is_array()) continue;
            for (const auto& raw_trade : trades->as_array()) {
                if (!raw_trade.is_object()) continue;
                const auto& trade = raw_trade.as_object();
                ExternalVenueEvent event;
                event.asset_handle = asset_handle;
                event.connection_epoch = epoch;
                event.venue = VenueId::CoinbaseSpot;
                event.event_type = ExternalEventType::Trade;
                event.local_receive_monotonic_ns = receive_ns;
                event.local_receive_wall_ns = wall_ns;
                event.exchange_event_ns = parse_iso8601_ns(text_of(find_value(trade, "time")));
                if (event.exchange_event_ns == 0) event.exchange_event_ns = root_ts;
                if (!parse_double(find_value(trade, "price"), event.trade_price)
                    || !parse_double(find_value(trade, "size"), event.trade_size)
                    || event.trade_price <= 0.0 || event.trade_size <= 0.0) {
                    result.invalid_frame = 1;
                    continue;
                }
                // Coinbase market_trades documents side as MAKER side.
                const auto maker_side = text_of(find_value(trade, "side"));
                if (ieq(maker_side, "BUY")) event.trade_side = -1;
                else if (ieq(maker_side, "SELL")) event.trade_side = 1;
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

void decode_bybit(const json::object& root, std::uint64_t asset_handle,
                  std::uint64_t epoch, std::int64_t receive_ns,
                  std::int64_t wall_ns, ExternalDecodeResult& result,
                  std::span<ExternalVenueEvent> output) noexcept {
    const auto topic = text_of(find_value(root, "topic"));
    std::int64_t root_ms = 0;
    (void)parse_i64(find_value(root, "ts"), root_ms);
    const auto root_ts = milliseconds_to_ns(root_ms);
    const auto* data = find_value(root, "data");

    if (topic.starts_with("orderbook.1.") && data != nullptr && data->is_object()) {
        ++result.recognized_events;
        const auto& object = data->as_object();
        const auto* bids = find_value(object, "b");
        const auto* asks = find_value(object, "a");
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
        event.exchange_event_ns = root_ts;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        (void)parse_u64(find_value(object, "u"), event.source_sequence);
        if (!parse_level(bids->as_array()[0], event.bid, event.bid_size)
            || !parse_level(asks->as_array()[0], event.ask, event.ask_size)
            || event.ask <= event.bid) {
            result.invalid_frame = 1;
            return;
        }
        event.healthy = 1;
        emit(result, output, event);
        return;
    }

    if (topic.starts_with("publicTrade.") && data != nullptr && data->is_array()) {
        ++result.recognized_events;
        for (const auto& raw_trade : data->as_array()) {
            if (!raw_trade.is_object()) continue;
            const auto& trade = raw_trade.as_object();
            ExternalVenueEvent event;
            event.asset_handle = asset_handle;
            event.connection_epoch = epoch;
            event.venue = VenueId::BybitSpot;
            event.event_type = ExternalEventType::Trade;
            event.local_receive_monotonic_ns = receive_ns;
            event.local_receive_wall_ns = wall_ns;
            std::int64_t trade_ms = 0;
            (void)parse_i64(find_value(trade, "T"), trade_ms);
            event.exchange_event_ns = trade_ms > 0 ? milliseconds_to_ns(trade_ms) : root_ts;
            if (!parse_double(find_value(trade, "p"), event.trade_price)
                || !parse_double(find_value(trade, "v"), event.trade_size)
                || event.trade_price <= 0.0 || event.trade_size <= 0.0) {
                result.invalid_frame = 1;
                continue;
            }
            const auto taker_side = text_of(find_value(trade, "S"));
            if (ieq(taker_side, "Buy")) event.trade_side = 1;
            else if (ieq(taker_side, "Sell")) event.trade_side = -1;
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

void decode_deribit(const json::object& root, std::uint64_t asset_handle,
                    std::uint64_t epoch, std::int64_t receive_ns,
                    std::int64_t wall_ns, ExternalDecodeResult& result,
                    std::span<ExternalVenueEvent> output) noexcept {
    const auto method = text_of(find_value(root, "method"));
    const auto* params = find_value(root, "params");
    if (!ieq(method, "subscription") || params == nullptr || !params->is_object()) {
        ++result.ignored_events;
        return;
    }
    const auto& parameter = params->as_object();
    const auto channel = text_of(find_value(parameter, "channel"));
    const auto* data = find_value(parameter, "data");
    if (channel.starts_with("ticker.BTC-PERPETUAL.") && data != nullptr && data->is_object()) {
        ++result.recognized_events;
        const auto& ticker = data->as_object();
        ExternalVenueEvent event;
        event.asset_handle = asset_handle;
        event.connection_epoch = epoch;
        event.venue = VenueId::Deribit;
        event.event_type = ExternalEventType::BookTop;
        event.local_receive_monotonic_ns = receive_ns;
        event.local_receive_wall_ns = wall_ns;
        std::int64_t timestamp_ms = 0;
        if (parse_i64(find_value(ticker, "timestamp"), timestamp_ms)) {
            event.exchange_event_ns = milliseconds_to_ns(timestamp_ms);
        }
        if (!parse_double(find_value(ticker, "best_bid_price"), event.bid)
            || !parse_double(find_value(ticker, "best_ask_price"), event.ask)
            || !parse_double(find_value(ticker, "best_bid_amount"), event.bid_size)
            || !parse_double(find_value(ticker, "best_ask_amount"), event.ask_size)
            || event.bid <= 0.0 || event.ask <= event.bid
            || event.bid_size < 0.0 || event.ask_size < 0.0) {
            result.invalid_frame = 1;
            return;
        }
        event.healthy = 1;
        emit(result, output, event);
        ExternalVenueEvent context = event;
        context.event_type = ExternalEventType::DerivativeContext;
        context.context_valid_mask = 0;
        if (parse_double(find_value(ticker, "mark_price"), context.mark_price)
            && context.mark_price > 0.0) {
            context.context_valid_mask |= DerivativeContextMarkPrice;
        }
        if (parse_double(find_value(ticker, "index_price"), context.index_price)
            && context.index_price > 0.0) {
            context.context_valid_mask |= DerivativeContextIndexPrice;
        }
        if (parse_double(find_value(ticker, "current_funding"), context.funding_rate)
            && std::isfinite(context.funding_rate)) {
            context.context_valid_mask |= DerivativeContextFundingRate;
        }
        if (parse_double(find_value(ticker, "open_interest"), context.open_interest)
            && context.open_interest >= 0.0) {
            context.context_valid_mask |= DerivativeContextOpenInterest;
        }
        if (context.context_valid_mask != 0) emit(result, output, context);
        return;
    }
    if (channel.starts_with("trades.BTC-PERPETUAL.") && data != nullptr && data->is_array()) {
        ++result.recognized_events;
        for (const auto& raw_trade : data->as_array()) {
            if (!raw_trade.is_object()) continue;
            const auto& trade = raw_trade.as_object();
            ExternalVenueEvent event;
            event.asset_handle = asset_handle;
            event.connection_epoch = epoch;
            event.venue = VenueId::Deribit;
            event.event_type = ExternalEventType::Trade;
            event.local_receive_monotonic_ns = receive_ns;
            event.local_receive_wall_ns = wall_ns;
            (void)parse_u64(find_value(trade, "trade_seq"), event.source_sequence);
            std::int64_t timestamp_ms = 0;
            if (parse_i64(find_value(trade, "timestamp"), timestamp_ms)) event.exchange_event_ns = milliseconds_to_ns(timestamp_ms);
            if (!parse_double(find_value(trade, "price"), event.trade_price)
                || !parse_double(find_value(trade, "amount"), event.trade_size)
                || event.trade_price <= 0.0 || event.trade_size <= 0.0) {
                result.invalid_frame = 1;
                continue;
            }
            const auto direction = text_of(find_value(trade, "direction"));
            if (ieq(direction, "buy")) event.trade_side = 1;
            else if (ieq(direction, "sell")) event.trade_side = -1;
            else { result.invalid_frame = 1; continue; }
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
    if (venue == VenueId::BinanceSpot) {
        const auto fast = decode_binance_fast(
            asset_handle, connection_epoch, local_receive_monotonic_ns,
            local_receive_wall_ns, payload, result, output);
        if (fast != FastBinanceState::NotApplicable) return result;
    }
    try {
        auto& scratch = decoder_scratch();
        scratch.value_resource.release();
        scratch.parser.reset(json::storage_ptr(&scratch.value_resource));
        boost::system::error_code error;
        const auto consumed = scratch.parser.write(payload.data(), payload.size(), error);
        if (error || consumed != payload.size()) {
            result.invalid_frame = 1;
            return result;
        }
        const json::value root = scratch.parser.release();
        if (!root.is_object()) {
            result.invalid_frame = 1;
            return result;
        }
        switch (venue) {
            case VenueId::BinanceSpot:
                decode_binance(root.as_object(), asset_handle, connection_epoch,
                               local_receive_monotonic_ns, local_receive_wall_ns,
                               result, output);
                break;
            case VenueId::CoinbaseSpot:
                decode_coinbase(root.as_object(), asset_handle, connection_epoch,
                                local_receive_monotonic_ns, local_receive_wall_ns,
                                result, output);
                break;
            case VenueId::BybitSpot:
                decode_bybit(root.as_object(), asset_handle, connection_epoch,
                             local_receive_monotonic_ns, local_receive_wall_ns,
                             result, output);
                break;
            case VenueId::Deribit:
                decode_deribit(root.as_object(), asset_handle, connection_epoch,
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
