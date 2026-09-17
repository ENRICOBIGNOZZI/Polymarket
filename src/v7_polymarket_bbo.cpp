#include "pm/v7_polymarket_bbo.hpp"

#include <boost/json.hpp>
#include <boost/json/null_resource.hpp>
#include <boost/json/parser.hpp>
#include <boost/json/static_resource.hpp>

#include <algorithm>
#include <array>
#include <charconv>
#include <limits>
#include <stdexcept>

namespace pm::v7::polymarket_bbo {
namespace {
namespace json = boost::json;

constexpr std::size_t kValueArenaBytes = 128 * 1024;
constexpr std::size_t kParserScratchBytes = 16 * 1024;

struct Scratch {
    std::array<unsigned char, kValueArenaBytes> value_arena{};
    json::static_resource value_resource{value_arena.data(), value_arena.size()};
    std::array<unsigned char, kParserScratchBytes> parser_scratch{};
    json::parser parser{
        json::storage_ptr(json::get_null_resource()),
        json::parse_options{},
        parser_scratch.data(), parser_scratch.size()};
};

Scratch& scratch() noexcept {
    thread_local Scratch value{};
    return value;
}

[[nodiscard]] const json::value* field(
    const json::object& object,
    std::string_view name) noexcept {
    const auto it = object.find(name);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] std::string_view text(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& string = value->as_string();
    return {string.data(), string.size()};
}

[[nodiscard]] bool decimal_e4(std::string_view value, std::int32_t& output) noexcept {
    if (value.empty()) return false;
    std::size_t cursor = 0;
    std::int64_t integer = 0;
    bool integer_digit = false;
    while (cursor < value.size() && value[cursor] >= '0' && value[cursor] <= '9') {
        integer_digit = true;
        integer = integer * 10 + static_cast<std::int64_t>(value[cursor] - '0');
        if (integer > 1) return false;
        ++cursor;
    }
    if (!integer_digit && (cursor >= value.size() || value[cursor] != '.')) return false;
    std::int64_t fraction = 0;
    int digits = 0;
    if (cursor < value.size() && value[cursor] == '.') {
        ++cursor;
        while (cursor < value.size()) {
            const char c = value[cursor++];
            if (c < '0' || c > '9') return false;
            if (digits < 4) {
                fraction = fraction * 10 + static_cast<std::int64_t>(c - '0');
                ++digits;
            } else if (c != '0') {
                return false;
            }
        }
    }
    while (digits < 4) {
        fraction *= 10;
        ++digits;
    }
    const auto scaled = integer * 10'000 + fraction;
    if (scaled < 0 || scaled > 10'000) return false;
    output = static_cast<std::int32_t>(scaled);
    return true;
}

[[nodiscard]] bool timestamp_ns(const json::value* value, std::int64_t& output) noexcept {
    if (value == nullptr) { output = 0; return true; }
    std::int64_t milliseconds = 0;
    if (value->is_string()) {
        const auto raw = text(value);
        if (raw.empty()) return false;
        const auto [end, ec] = std::from_chars(raw.data(), raw.data() + raw.size(), milliseconds);
        if (ec != std::errc{} || end != raw.data() + raw.size()) return false;
    } else if (value->is_int64()) {
        milliseconds = value->as_int64();
    } else if (value->is_uint64()) {
        const auto raw = value->as_uint64();
        if (raw > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return false;
        milliseconds = static_cast<std::int64_t>(raw);
    } else {
        return false;
    }
    if (milliseconds < 0 || milliseconds > std::numeric_limits<std::int64_t>::max() / 1'000'000)
        return false;
    output = milliseconds * 1'000'000;
    return true;
}

[[nodiscard]] bool valid_bbo(std::int32_t bid, std::int32_t ask) noexcept {
    return bid > 0 && ask > bid && ask <= 10'000;
}

[[nodiscard]] std::uint64_t mix64(std::uint64_t value) noexcept {
    value ^= value >> 30U;
    value *= 0xbf58476d1ce4e5b9ULL;
    value ^= value >> 27U;
    value *= 0x94d049bb133111ebULL;
    value ^= value >> 31U;
    return value;
}

[[nodiscard]] std::uint64_t update_identity(
    std::uint64_t instrument_handle, std::int64_t exchange_ns,
    std::int32_t bid, std::int32_t ask, SourceKind source) noexcept {
    if (instrument_handle == 0 || exchange_ns <= 0 || !valid_bbo(bid, ask)) return 0;
    std::uint64_t value = mix64(instrument_handle + 0x9e3779b97f4a7c15ULL);
    value ^= mix64(static_cast<std::uint64_t>(exchange_ns));
    value ^= mix64((static_cast<std::uint64_t>(static_cast<std::uint32_t>(bid)) << 32U)
                   | static_cast<std::uint32_t>(ask));
    value ^= mix64(static_cast<std::uint8_t>(source) + 0xd6e8feb86659fd93ULL);
    value = mix64(value);
    return value == 0 ? 1 : value;
}
} // namespace

FirstArrivalGate::FirstArrivalGate(std::int64_t duplicate_window_ns) noexcept
    : duplicate_window_ns_(duplicate_window_ns > 0 ? duplicate_window_ns : 500'000'000LL) {}

void FirstArrivalGate::reset() noexcept {
    entries_ = {};
}

FirstArrivalResult FirstArrivalGate::observe(
    const Update& update, std::uint8_t connection_slot) noexcept {
    FirstArrivalResult result;
    if (update.valid == 0 || update.receive_monotonic_ns <= 0 || connection_slot >= 8) {
        result.decision = FirstArrivalDecision::Invalid;
        return result;
    }
    const std::uint8_t source_bit = static_cast<std::uint8_t>(1U << connection_slot);
    if (update.event_identity == 0) {
        result.decision = FirstArrivalDecision::Accept;
        result.first_receive_monotonic_ns = update.receive_monotonic_ns;
        result.connection_mask = source_bit;
        return result;
    }

    // Direct-mapped, deliberately fail-open. A hash-bucket collision can only
    // reduce dedupe efficiency: a distinct identity overwrites the slot and is
    // accepted. It can never cause a distinct market update to be dropped.
    auto& entry = entries_[static_cast<std::size_t>(mix64(update.event_identity)) & kMask];
    const bool same_identity = entry.identity == update.event_identity;
    const bool expired = same_identity
        && update.receive_monotonic_ns > entry.last_receive_monotonic_ns
        && update.receive_monotonic_ns - entry.last_receive_monotonic_ns > duplicate_window_ns_;
    if (same_identity && !expired) {
        entry.last_receive_monotonic_ns = std::max(
            entry.last_receive_monotonic_ns, update.receive_monotonic_ns);
        entry.connection_mask = static_cast<std::uint8_t>(entry.connection_mask | source_bit);
        result.decision = FirstArrivalDecision::Duplicate;
        result.first_receive_monotonic_ns = entry.first_receive_monotonic_ns;
        result.duplicate_delay_ns = std::max<std::int64_t>(
            0, update.receive_monotonic_ns - entry.first_receive_monotonic_ns);
        result.connection_mask = entry.connection_mask;
        return result;
    }

    const bool collision = entry.identity != 0 && !same_identity;
    entry.identity = update.event_identity;
    entry.first_receive_monotonic_ns = update.receive_monotonic_ns;
    entry.last_receive_monotonic_ns = update.receive_monotonic_ns;
    entry.connection_mask = source_bit;
    result.decision = collision ? FirstArrivalDecision::CollisionAccept
                                : FirstArrivalDecision::Accept;
    result.first_receive_monotonic_ns = update.receive_monotonic_ns;
    result.connection_mask = source_bit;
    return result;
}

Decoder::Decoder(std::vector<Binding> bindings) : bindings_(std::move(bindings)) {
    if (bindings_.empty()) throw std::invalid_argument("BBO decoder requires bindings");
    for (const auto& binding : bindings_) {
        if (binding.asset_id.empty() || binding.instrument_handle == 0)
            throw std::invalid_argument("invalid BBO binding");
    }
    std::sort(bindings_.begin(), bindings_.end(), [](const Binding& left, const Binding& right) {
        return left.asset_id < right.asset_id;
    });
    for (std::size_t i = 1; i < bindings_.size(); ++i) {
        if (bindings_[i - 1].asset_id == bindings_[i].asset_id)
            throw std::invalid_argument("duplicate BBO asset binding");
    }
}

const Binding* Decoder::binding(std::string_view asset_id) const noexcept {
    const auto it = std::lower_bound(
        bindings_.begin(), bindings_.end(), asset_id,
        [](const Binding& left, std::string_view right) { return left.asset_id < right; });
    return it != bindings_.end() && it->asset_id == asset_id ? &*it : nullptr;
}

FrameResult Decoder::decode(
    std::string_view payload,
    std::int64_t receive_monotonic_ns,
    std::span<Update> output) const noexcept {
    FrameResult result;
    if (payload.empty() || receive_monotonic_ns <= 0) {
        result.invalid_frame = 1;
        return result;
    }
    if (payload == "PONG") return result;

    try {
        auto& state = scratch();
        state.value_resource.release();
        state.parser.reset(json::storage_ptr(&state.value_resource));
        boost::system::error_code error;
        const auto consumed = state.parser.write(payload.data(), payload.size(), error);
        if (error || consumed != payload.size()) {
            result.invalid_frame = 1;
            return result;
        }
        const auto root = state.parser.release();
        if (!root.is_object()) {
            result.invalid_frame = 1;
            return result;
        }
        const auto& object = root.as_object();
        const auto event_type = text(field(object, "event_type"));
        std::int64_t exchange_ns = 0;
        if (!timestamp_ns(field(object, "timestamp"), exchange_ns)) {
            result.invalid_frame = 1;
            return result;
        }

        const auto emit = [&](std::string_view asset_id, std::string_view bid_text,
                              std::string_view ask_text, SourceKind source) noexcept -> bool {
            const auto* mapped = binding(asset_id);
            if (mapped == nullptr) {
                ++result.unknown_assets;
                return true;
            }
            std::int32_t bid = 0;
            std::int32_t ask = 0;
            if (!decimal_e4(bid_text, bid) || !decimal_e4(ask_text, ask) || !valid_bbo(bid, ask)) {
                ++result.incomplete_bbo;
                return true;
            }
            ++result.recognized_updates;
            if (result.output_count >= output.size()) {
                result.output_overflow = 1;
                return false;
            }
            auto& update = output[result.output_count++];
            update = {};
            update.market_handle = mapped->market_handle;
            update.event_handle = mapped->event_handle;
            update.instrument_handle = mapped->instrument_handle;
            update.exchange_event_ns = exchange_ns;
            update.receive_monotonic_ns = receive_monotonic_ns;
            update.event_identity = update_identity(
                mapped->instrument_handle, exchange_ns, bid, ask, source);
            update.best_bid_e4 = bid;
            update.best_ask_e4 = ask;
            update.source = source;
            update.valid = 1;
            return true;
        };

        if (event_type == "best_bid_ask") {
            const auto asset_id = text(field(object, "asset_id"));
            const auto bid = text(field(object, "best_bid"));
            const auto ask = text(field(object, "best_ask"));
            if (asset_id.empty() || bid.empty() || ask.empty()) {
                result.invalid_frame = 1;
                return result;
            }
            (void)emit(asset_id, bid, ask, SourceKind::BestBidAsk);
            return result;
        }

        if (event_type == "price_change") {
            const auto* changes = field(object, "price_changes");
            if (changes == nullptr || !changes->is_array()) {
                result.invalid_frame = 1;
                return result;
            }
            for (const auto& value : changes->as_array()) {
                if (!value.is_object()) {
                    result.invalid_frame = 1;
                    return result;
                }
                const auto& change = value.as_object();
                const auto asset_id = text(field(change, "asset_id"));
                const auto bid = text(field(change, "best_bid"));
                const auto ask = text(field(change, "best_ask"));
                if (asset_id.empty()) {
                    result.invalid_frame = 1;
                    return result;
                }
                if (bid.empty() || ask.empty()) {
                    ++result.incomplete_bbo;
                    continue;
                }
                if (!emit(asset_id, bid, ask, SourceKind::PriceChange)) break;
            }
            return result;
        }

        return result;
    } catch (...) {
        result.invalid_frame = 1;
        return result;
    }
}

} // namespace pm::v7::polymarket_bbo
