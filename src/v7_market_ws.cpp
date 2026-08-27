#include "pm/v7_market_ws.hpp"

#include <boost/json.hpp>
#include <boost/json/static_resource.hpp>
#include <boost/system/error_code.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <new>
#include <utility>

namespace pm::v7 {
namespace {
namespace json = boost::json;

constexpr std::size_t kJsonArenaBytes = 1024 * 1024;
constexpr std::size_t kMaxSnapshotLevelsPerSide = 256;

[[nodiscard]] bool ascii_ieq(std::string_view lhs, std::string_view rhs) noexcept {
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

[[nodiscard]] std::string_view string_view_of(const json::value* value) noexcept {
    if (value == nullptr || !value->is_string()) return {};
    const auto& text = value->as_string();
    return {text.data(), text.size()};
}

[[nodiscard]] const json::value* find_value(const json::object& object, const char* key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] bool parse_scaled_decimal(std::string_view text, std::int64_t scale,
                                        std::int64_t& out) noexcept {
    if (text.empty() || scale <= 0) return false;
    bool negative = false;
    std::size_t pos = 0;
    if (text[pos] == '+' || text[pos] == '-') {
        negative = text[pos] == '-';
        if (++pos == text.size()) return false;
    }

    std::int64_t integer = 0;
    bool any_integer = false;
    while (pos < text.size() && text[pos] >= '0' && text[pos] <= '9') {
        any_integer = true;
        const int digit = text[pos++] - '0';
        if (integer > (std::numeric_limits<std::int64_t>::max() - digit) / 10) return false;
        integer = integer * 10 + digit;
    }
    if (!any_integer) return false;

    int scale_digits = 0;
    for (std::int64_t x = scale; x > 1; x /= 10) {
        if (x % 10 != 0) return false;
        ++scale_digits;
    }

    std::int64_t fraction = 0;
    int digits = 0;
    int first_discarded = -1;
    if (pos < text.size() && text[pos] == '.') {
        ++pos;
        while (pos < text.size() && text[pos] >= '0' && text[pos] <= '9') {
            const int digit = text[pos++] - '0';
            if (digits < scale_digits) {
                fraction = fraction * 10 + digit;
                ++digits;
            } else if (first_discarded < 0) {
                first_discarded = digit;
            }
        }
    }
    if (pos != text.size()) return false;
    while (digits < scale_digits) {
        fraction *= 10;
        ++digits;
    }
    if (integer > (std::numeric_limits<std::int64_t>::max() - fraction) / scale) return false;
    std::int64_t scaled = integer * scale + fraction;
    if (first_discarded >= 5) {
        if (scaled == std::numeric_limits<std::int64_t>::max()) return false;
        ++scaled;
    }
    out = negative ? -scaled : scaled;
    return true;
}

[[nodiscard]] bool parse_scaled(const json::value* value, std::int64_t scale,
                                std::int64_t& out) noexcept {
    if (value == nullptr) return false;
    if (value->is_string()) return parse_scaled_decimal(string_view_of(value), scale, out);

    long double raw = 0.0L;
    if (value->is_double()) raw = static_cast<long double>(value->as_double());
    else if (value->is_int64()) raw = static_cast<long double>(value->as_int64());
    else if (value->is_uint64()) raw = static_cast<long double>(value->as_uint64());
    else return false;
    if (!std::isfinite(static_cast<double>(raw))) return false;
    const long double scaled = raw * static_cast<long double>(scale);
    if (scaled < static_cast<long double>(std::numeric_limits<std::int64_t>::min())
        || scaled > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return false;
    }
    out = static_cast<std::int64_t>(std::llround(scaled));
    return true;
}

[[nodiscard]] bool parse_integer(const json::value* value, std::int64_t& out) noexcept {
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
    if (value->is_double()) {
        const double raw = value->as_double();
        if (!std::isfinite(raw)) return false;
        out = static_cast<std::int64_t>(raw);
        return true;
    }
    if (value->is_string()) return parse_scaled_decimal(string_view_of(value), 1, out);
    return false;
}

[[nodiscard]] std::int64_t normalize_exchange_ns(const json::object& object) noexcept {
    std::int64_t raw = 0;
    if (!parse_integer(find_value(object, "timestamp"), raw) || raw <= 0) return 0;
    if (raw < 100'000'000'000LL) return raw * 1'000'000'000LL;
    if (raw < 100'000'000'000'000LL) return raw * 1'000'000LL;
    if (raw < 100'000'000'000'000'000LL) return raw * 1'000LL;
    return raw;
}

[[nodiscard]] Side parse_side(const json::value* value) noexcept {
    const auto text = string_view_of(value);
    if (ascii_ieq(text, "BUY") || ascii_ieq(text, "BID")) return Side::Buy;
    if (ascii_ieq(text, "SELL") || ascii_ieq(text, "ASK")) return Side::Sell;
    return Side::None;
}

[[nodiscard]] bool parse_price_e4(const json::value* value, std::int32_t& out) noexcept {
    std::int64_t scaled = 0;
    if (!parse_scaled(value, kCanonicalPriceScale, scaled)
        || scaled <= 0 || scaled >= kCanonicalPriceScale) {
        return false;
    }
    out = static_cast<std::int32_t>(scaled);
    return true;
}

[[nodiscard]] bool parse_quantity(const json::value* value, std::int64_t& out) noexcept {
    if (!parse_scaled(value, 1'000'000, out)) return false;
    return out >= 0;
}

[[nodiscard]] bool parse_level(const json::value& value, PriceLevelE4& out) noexcept {
    if (value.is_object()) {
        const auto& object = value.as_object();
        return parse_price_e4(find_value(object, "price"), out.price_e4)
            && parse_quantity(find_value(object, "size"), out.quantity_microunits);
    }
    if (value.is_array()) {
        const auto& array = value.as_array();
        if (array.size() < 2) return false;
        return parse_price_e4(&array[0], out.price_e4)
            && parse_quantity(&array[1], out.quantity_microunits);
    }
    return false;
}

} // namespace

struct MarketWsShard::Impl {
    struct TokenState {
        TokenBinding binding;
        CanonicalL2Book book;

        explicit TokenState(TokenBinding value)
            : binding(std::move(value)), book(binding.tick_size_e4) {}
    };

    std::vector<TokenState> states;
    std::array<unsigned char, kJsonArenaBytes> json_arena{};
    json::static_resource json_resource;
    std::array<PriceLevelE4, kMaxSnapshotLevelsPerSide> bid_scratch{};
    std::array<PriceLevelE4, kMaxSnapshotLevelsPerSide> ask_scratch{};

    explicit Impl(std::vector<TokenBinding> bindings)
        : json_resource(json_arena.data(), json_arena.size()) {
        std::sort(bindings.begin(), bindings.end(), [](const TokenBinding& lhs, const TokenBinding& rhs) {
            return lhs.asset_id < rhs.asset_id;
        });
        bindings.erase(std::unique(bindings.begin(), bindings.end(), [](const TokenBinding& lhs, const TokenBinding& rhs) {
            return lhs.asset_id == rhs.asset_id;
        }), bindings.end());
        states.reserve(bindings.size());
        for (auto& binding : bindings) {
            if (binding.asset_id.empty() || binding.instrument_handle == 0) continue;
            states.emplace_back(std::move(binding));
        }
    }

    [[nodiscard]] TokenState* find_asset(std::string_view asset) noexcept {
        const auto it = std::lower_bound(states.begin(), states.end(), asset,
            [](const TokenState& state, std::string_view needle) {
                return state.binding.asset_id.compare(needle) < 0;
            });
        if (it == states.end() || it->binding.asset_id.size() != asset.size()
            || it->binding.asset_id.compare(asset) != 0) {
            return nullptr;
        }
        return &*it;
    }

    [[nodiscard]] const TokenState* find_instrument(std::uint64_t instrument_handle) const noexcept {
        for (const auto& state : states) {
            if (state.binding.instrument_handle == instrument_handle) return &state;
        }
        return nullptr;
    }

    void emit(MarketWsFrameResult& result, std::span<MarketWsEvent> output,
              MarketWsEvent event) noexcept {
        if (result.output_count >= output.size()) {
            result.output_overflow = 1;
            return;
        }
        output[result.output_count++] = event;
    }

    void invalidate(TokenState& state, MarketWsFrameResult& result,
                    std::span<MarketWsEvent> output) noexcept {
        state.book.invalidate_lineage();
        result.lineage_invalidated = 1;
        MarketWsEvent event;
        event.kind = MarketWsEventKind::LineageInvalidated;
        event.market_handle = state.binding.market_handle;
        event.event_handle = state.binding.event_handle;
        event.instrument_handle = state.binding.instrument_handle;
        event.state_version = state.book.state_version();
        emit(result, output, event);
    }

    void emit_book(TokenState& state, MarketWsEventKind kind,
                   std::int64_t exchange_ns, std::int64_t receive_ns,
                   MarketWsFrameResult& result, std::span<MarketWsEvent> output) noexcept {
        MarketWsEvent event;
        event.kind = kind;
        event.market_handle = state.binding.market_handle;
        event.event_handle = state.binding.event_handle;
        event.instrument_handle = state.binding.instrument_handle;
        event.state_version = state.book.state_version();
        event.exchange_event_ns = exchange_ns;
        event.receive_monotonic_ns = receive_ns;
        event.book = state.book.hot_snapshot();
        emit(result, output, event);
    }

    [[nodiscard]] bool parse_snapshot_levels(const json::object& event, const char* key,
                                             std::array<PriceLevelE4, kMaxSnapshotLevelsPerSide>& scratch,
                                             std::size_t& count) noexcept {
        count = 0;
        const auto* value = find_value(event, key);
        if (value == nullptr || !value->is_array()) return false;
        const auto& levels = value->as_array();
        if (levels.size() > scratch.size()) return false;
        for (const auto& raw : levels) {
            PriceLevelE4 level;
            if (!parse_level(raw, level)) return false;
            scratch[count++] = level;
        }
        return true;
    }

    void process_event(const json::object& event, const pm::fast::FeedReceiveStamp& receive,
                       MarketWsFrameResult& result, std::span<MarketWsEvent> output) noexcept {
        auto type = string_view_of(find_value(event, "event_type"));
        if (type.empty()) type = string_view_of(find_value(event, "type"));
        if (type.empty()) return;

        const std::int64_t exchange_ns = normalize_exchange_ns(event);
        if (ascii_ieq(type, "book")) {
            ++result.recognized_events;
            const auto asset = string_view_of(find_value(event, "asset_id"));
            TokenState* state = find_asset(asset);
            if (state == nullptr) {
                ++result.ignored_unknown_assets;
                return;
            }
            std::size_t bid_count = 0;
            std::size_t ask_count = 0;
            if (exchange_ns <= 0 || receive.monotonic_ns <= 0
                || !parse_snapshot_levels(event, "bids", bid_scratch, bid_count)
                || !parse_snapshot_levels(event, "asks", ask_scratch, ask_count)
                || !state->book.replace_snapshot(
                    std::span<const PriceLevelE4>(bid_scratch.data(), bid_count),
                    std::span<const PriceLevelE4>(ask_scratch.data(), ask_count),
                    exchange_ns, receive.monotonic_ns)) {
                invalidate(*state, result, output);
                return;
            }
            ++result.applied_state_events;
            emit_book(*state, MarketWsEventKind::BookChanged, exchange_ns, receive.monotonic_ns,
                      result, output);
            return;
        }

        if (ascii_ieq(type, "price_change")) {
            ++result.recognized_events;
            const auto* raw_changes = find_value(event, "price_changes");
            if (exchange_ns <= 0 || receive.monotonic_ns <= 0
                || raw_changes == nullptr || !raw_changes->is_array()) {
                result.invalid_frame = 1;
                return;
            }
            for (const auto& raw : raw_changes->as_array()) {
                if (!raw.is_object()) {
                    result.invalid_frame = 1;
                    continue;
                }
                const auto& change = raw.as_object();
                const auto asset = string_view_of(find_value(change, "asset_id"));
                TokenState* state = find_asset(asset);
                if (state == nullptr) {
                    ++result.ignored_unknown_assets;
                    continue;
                }
                const Side side = parse_side(find_value(change, "side"));
                std::int32_t price_e4 = 0;
                std::int64_t quantity = 0;
                if (side == Side::None || !parse_price_e4(find_value(change, "price"), price_e4)
                    || !parse_quantity(find_value(change, "size"), quantity)
                    || !state->book.mutate_level(side, price_e4, quantity,
                                                 exchange_ns, receive.monotonic_ns)) {
                    invalidate(*state, result, output);
                    continue;
                }
                ++result.applied_state_events;
                MarketWsEvent out;
                out.kind = MarketWsEventKind::BookChanged;
                out.market_handle = state->binding.market_handle;
                out.event_handle = state->binding.event_handle;
                out.instrument_handle = state->binding.instrument_handle;
                out.state_version = state->book.state_version();
                out.side = side;
                out.price_e4 = price_e4;
                out.quantity_microunits = quantity;
                out.exchange_event_ns = exchange_ns;
                out.receive_monotonic_ns = receive.monotonic_ns;
                out.book = state->book.hot_snapshot();
                emit(result, output, out);
            }
            return;
        }

        if (ascii_ieq(type, "tick_size_change")) {
            ++result.recognized_events;
            const auto asset = string_view_of(find_value(event, "asset_id"));
            TokenState* state = find_asset(asset);
            if (state == nullptr) {
                ++result.ignored_unknown_assets;
                return;
            }
            std::int64_t new_tick = 0;
            if (exchange_ns <= 0 || receive.monotonic_ns <= 0
                || !parse_scaled(find_value(event, "new_tick_size"), kCanonicalPriceScale, new_tick)
                || new_tick <= 0 || new_tick > kCanonicalPriceScale
                || !state->book.change_tick_size(static_cast<std::int32_t>(new_tick),
                                                 exchange_ns, receive.monotonic_ns)) {
                invalidate(*state, result, output);
                return;
            }
            ++result.applied_state_events;
            emit_book(*state, MarketWsEventKind::TickSizeChanged, exchange_ns,
                      receive.monotonic_ns, result, output);
            return;
        }

        if (ascii_ieq(type, "last_trade_price")) {
            ++result.recognized_events;
            const auto asset = string_view_of(find_value(event, "asset_id"));
            TokenState* state = find_asset(asset);
            if (state == nullptr) {
                ++result.ignored_unknown_assets;
                return;
            }
            std::int32_t price_e4 = 0;
            std::int64_t quantity = 0;
            const Side aggressor = parse_side(find_value(event, "side"));
            if (exchange_ns <= 0 || receive.monotonic_ns <= 0
                || aggressor == Side::None
                || !parse_price_e4(find_value(event, "price"), price_e4)
                || !parse_quantity(find_value(event, "size"), quantity)
                || quantity <= 0) {
                // Some venue messages report only last price. Those are not
                // execution prints and must not feed queue depletion.
                return;
            }
            MarketWsEvent out;
            out.kind = MarketWsEventKind::Trade;
            out.market_handle = state->binding.market_handle;
            out.event_handle = state->binding.event_handle;
            out.instrument_handle = state->binding.instrument_handle;
            out.state_version = state->book.state_version();
            out.side = aggressor;
            out.price_e4 = price_e4;
            out.quantity_microunits = quantity;
            out.exchange_event_ns = exchange_ns;
            out.receive_monotonic_ns = receive.monotonic_ns;
            out.book = state->book.hot_snapshot();
            ++result.trade_events;
            emit(result, output, out);
        }
    }

    void invalidate_all() noexcept {
        for (auto& state : states) state.book.invalidate_lineage();
    }
};

MarketWsShard::MarketWsShard(std::vector<TokenBinding> bindings)
    : impl_(std::make_unique<Impl>(std::move(bindings))) {}

MarketWsShard::~MarketWsShard() = default;

MarketWsFrameResult MarketWsShard::process_frame(
    std::string_view payload,
    const pm::fast::FeedReceiveStamp& receive,
    std::span<MarketWsEvent> output) noexcept {

    MarketWsFrameResult result;
    if (payload.empty() || receive.monotonic_ns <= 0) {
        result.invalid_frame = 1;
        impl_->invalidate_all();
        result.lineage_invalidated = 1;
        return result;
    }

    impl_->json_resource.release();
    try {
        boost::system::error_code error;
        const json::value root = json::parse(payload, error, &impl_->json_resource);
        if (error) {
            result.invalid_frame = 1;
            impl_->invalidate_all();
            result.lineage_invalidated = 1;
            return result;
        }
        if (root.is_array()) {
            for (const auto& raw : root.as_array()) {
                if (!raw.is_object()) {
                    result.invalid_frame = 1;
                    continue;
                }
                impl_->process_event(raw.as_object(), receive, result, output);
            }
        } else if (root.is_object()) {
            impl_->process_event(root.as_object(), receive, result, output);
        } else {
            result.invalid_frame = 1;
        }
    } catch (const std::bad_alloc&) {
        result.invalid_frame = 1;
        result.arena_exhausted = 1;
    } catch (...) {
        result.invalid_frame = 1;
    }

    if (result.invalid_frame || result.output_overflow) {
        // A malformed/truncated frame or an output-capacity miss means that the
        // consumer cannot prove it observed every mutation. Discard continuity.
        impl_->invalidate_all();
        result.lineage_invalidated = 1;
    }
    return result;
}

void MarketWsShard::invalidate_all_lineage() noexcept {
    impl_->invalidate_all();
}

BookHotSnapshot MarketWsShard::snapshot(std::uint64_t instrument_handle) const noexcept {
    const auto* state = impl_->find_instrument(instrument_handle);
    return state == nullptr ? BookHotSnapshot{} : state->book.hot_snapshot();
}

std::size_t MarketWsShard::instrument_count() const noexcept {
    return impl_->states.size();
}

} // namespace pm::v7
