#include "pm/v7_binance_l2_protocol.hpp"

#include <boost/json.hpp>

#include <array>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>

namespace pm::v7::external_fair {
namespace {
namespace json = boost::json;
constexpr std::size_t kJsonArenaBytes = 512 * 1024;

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

[[nodiscard]] bool parse_double(const json::value* value, double& output) noexcept {
    if (value == nullptr) return false;
    if (value->is_double()) output = value->as_double();
    else if (value->is_int64()) output = static_cast<double>(value->as_int64());
    else if (value->is_uint64()) output = static_cast<double>(value->as_uint64());
    else if (value->is_string()) {
        const auto text = text_of(value);
        if (text.empty() || text.size() >= 96) return false;
        std::array<char, 96> buffer{};
        std::memcpy(buffer.data(), text.data(), text.size());
        char* end = nullptr;
        output = std::strtod(buffer.data(), &end);
        if (end != buffer.data() + static_cast<std::ptrdiff_t>(text.size())) return false;
    } else return false;
    return std::isfinite(output);
}

[[nodiscard]] bool parse_u64(const json::value* value, std::uint64_t& output) noexcept {
    if (value == nullptr) return false;
    if (value->is_uint64()) { output = value->as_uint64(); return true; }
    if (value->is_int64()) {
        if (value->as_int64() < 0) return false;
        output = static_cast<std::uint64_t>(value->as_int64());
        return true;
    }
    if (!value->is_string()) return false;
    const auto text = text_of(value);
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), output);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}

[[nodiscard]] bool parse_i64(const json::value* value, std::int64_t& output) noexcept {
    if (value == nullptr) return false;
    if (value->is_int64()) { output = value->as_int64(); return true; }
    if (value->is_uint64()) {
        if (value->as_uint64() > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return false;
        output = static_cast<std::int64_t>(value->as_uint64());
        return true;
    }
    if (!value->is_string()) return false;
    const auto text = text_of(value);
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), output);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}

[[nodiscard]] std::int64_t milliseconds_to_ns(std::int64_t value) noexcept {
    if (value <= 0 || value > std::numeric_limits<std::int64_t>::max() / 1'000'000LL) return 0;
    return value * 1'000'000LL;
}

[[nodiscard]] bool parse_levels(const json::value* raw, std::vector<BinanceDepthLevel>& output,
                                std::size_t max_levels) noexcept {
    if (raw == nullptr || !raw->is_array() || raw->as_array().size() > max_levels) return false;
    output.clear();
    output.reserve(raw->as_array().size());
    for (const auto& row_value : raw->as_array()) {
        if (!row_value.is_array() || row_value.as_array().size() < 2) return false;
        const auto& row = row_value.as_array();
        BinanceDepthLevel level;
        if (!parse_double(&row[0], level.price) || !parse_double(&row[1], level.quantity)
            || level.price <= 0.0 || level.quantity < 0.0) return false;
        output.push_back(level);
    }
    return true;
}

[[nodiscard]] BinanceDepthParseState parse_object(std::string_view payload, json::object& output) noexcept {
    try {
        thread_local std::array<unsigned char, kJsonArenaBytes> arena{};
        json::static_resource resource(arena.data(), arena.size());
        boost::system::error_code error;
        const json::value value = json::parse(payload, error, &resource);
        if (error || !value.is_object()) return BinanceDepthParseState::Invalid;
        output = value.as_object();
        return BinanceDepthParseState::Parsed;
    } catch (const std::bad_alloc&) {
        return BinanceDepthParseState::TooLarge;
    } catch (...) {
        return BinanceDepthParseState::Invalid;
    }
}

} // namespace

BinanceDepthParseState parse_binance_spot_depth_delta(
    std::string_view payload, std::int64_t receive_ns, BinanceDepthDelta& output,
    std::size_t max_levels_per_side) noexcept {
    if (payload.empty() || receive_ns <= 0 || max_levels_per_side == 0) return BinanceDepthParseState::Invalid;
    json::object root;
    const auto parsed = parse_object(payload, root);
    if (parsed != BinanceDepthParseState::Parsed) return parsed;
    const json::object* object = &root;
    if (const auto* data = find_value(root, "data"); data != nullptr && data->is_object()) object = &data->as_object();
    if (text_of(find_value(*object, "e")) != "depthUpdate") return BinanceDepthParseState::Ignored;
    BinanceDepthDelta next;
    next.local_receive_monotonic_ns = receive_ns;
    std::int64_t event_ms = 0;
    (void)parse_i64(find_value(*object, "E"), event_ms);
    next.source_event_ns = milliseconds_to_ns(event_ms);
    if (!parse_u64(find_value(*object, "U"), next.first_update_id)
        || !parse_u64(find_value(*object, "u"), next.final_update_id)
        || next.first_update_id == 0 || next.final_update_id < next.first_update_id) return BinanceDepthParseState::Invalid;
    const auto* bids = find_value(*object, "b");
    const auto* asks = find_value(*object, "a");
    if ((bids != nullptr && bids->is_array() && bids->as_array().size() > max_levels_per_side)
        || (asks != nullptr && asks->is_array() && asks->as_array().size() > max_levels_per_side)) return BinanceDepthParseState::TooLarge;
    if (!parse_levels(bids, next.bids, max_levels_per_side) || !parse_levels(asks, next.asks, max_levels_per_side)) return BinanceDepthParseState::Invalid;
    output = std::move(next);
    return BinanceDepthParseState::Parsed;
}

BinanceDepthParseState parse_binance_depth_snapshot(
    std::string_view payload, std::int64_t receive_ns, BinanceDepthSnapshot& output,
    std::size_t max_levels_per_side) noexcept {
    if (payload.empty() || receive_ns <= 0 || max_levels_per_side == 0) return BinanceDepthParseState::Invalid;
    json::object root;
    const auto parsed = parse_object(payload, root);
    if (parsed != BinanceDepthParseState::Parsed) return parsed;
    BinanceDepthSnapshot next;
    next.local_receive_monotonic_ns = receive_ns;
    if (!parse_u64(find_value(root, "lastUpdateId"), next.last_update_id) || next.last_update_id == 0) return BinanceDepthParseState::Invalid;
    const auto* bids = find_value(root, "bids");
    const auto* asks = find_value(root, "asks");
    if ((bids != nullptr && bids->is_array() && bids->as_array().size() > max_levels_per_side)
        || (asks != nullptr && asks->is_array() && asks->as_array().size() > max_levels_per_side)) return BinanceDepthParseState::TooLarge;
    if (!parse_levels(bids, next.bids, max_levels_per_side) || !parse_levels(asks, next.asks, max_levels_per_side)) return BinanceDepthParseState::Invalid;
    output = std::move(next);
    return BinanceDepthParseState::Parsed;
}

} // namespace pm::v7::external_fair
