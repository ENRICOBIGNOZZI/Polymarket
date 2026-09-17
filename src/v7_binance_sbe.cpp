#include "pm/v7_binance_sbe.hpp"

#include <cmath>
#include <cstring>
#include <limits>
#include <utility>
#include <type_traits>

namespace pm::v7::external_fair {
namespace {
constexpr std::uint16_t kSchemaId = 1;
constexpr std::uint16_t kBestBidAskTemplate = 10001;
constexpr std::uint16_t kTradesTemplate = 10000;
constexpr std::size_t kHeaderBytes = 8;
constexpr std::size_t kBestBidAskRootBytes = 50;
constexpr std::size_t kTradesRootBytes = 18;
constexpr std::size_t kGroupHeaderBytes = 6;
constexpr std::size_t kTradeBlockBytes = 25;
constexpr std::uint32_t kMaxTradesPerFrame = 100000;

template <class T>
bool read_le(std::string_view payload, std::size_t offset, T& output) noexcept {
    if (offset > payload.size() || payload.size() - offset < sizeof(T)) return false;
    static_assert(std::is_integral_v<T>);
    using U = std::make_unsigned_t<T>;
    U value = 0;
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        value |= static_cast<U>(static_cast<unsigned char>(payload[offset + i])) << (8U * i);
    }
    std::memcpy(&output, &value, sizeof(T));
    return true;
}

bool header(std::string_view payload, std::uint16_t& block_length,
            std::uint16_t& template_id, std::uint16_t& version) noexcept {
    std::uint16_t schema = 0;
    return read_le(payload, 0, block_length) && read_le(payload, 2, template_id)
        && read_le(payload, 4, schema) && read_le(payload, 6, version)
        && schema == kSchemaId;
}

double decimal(std::int64_t mantissa, std::int8_t exponent) noexcept {
    const double value = static_cast<double>(mantissa) * std::pow(10.0, static_cast<int>(exponent));
    return std::isfinite(value) ? value : std::numeric_limits<double>::quiet_NaN();
}

bool symbol_at(std::string_view payload, std::size_t offset, std::string& symbol) {
    if (offset >= payload.size()) return false;
    const auto length = static_cast<std::size_t>(static_cast<unsigned char>(payload[offset]));
    ++offset;
    if (length == 0 || length > 32 || offset > payload.size() || payload.size() - offset != length) return false;
    symbol.assign(payload.data() + offset, length);
    return true;
}
} // namespace

BinanceSbeParseState parse_binance_sbe_best_bid_ask(
    std::string_view payload, BinanceSbeBestBidAsk& output) noexcept {
    try {
        std::uint16_t block = 0, id = 0, version = 0;
        if (!header(payload, block, id, version)) return BinanceSbeParseState::Invalid;
        if (id != kBestBidAskTemplate) return BinanceSbeParseState::Ignored;
        if (version != 0 || block < kBestBidAskRootBytes || payload.size() < kHeaderBytes + block + 1)
            return BinanceSbeParseState::Invalid;
        std::int64_t event = 0, update = 0, bid = 0, bid_qty = 0, ask = 0, ask_qty = 0;
        std::int8_t px_exp = 0, qty_exp = 0;
        const std::size_t root = kHeaderBytes;
        if (!read_le(payload, root + 0, event) || !read_le(payload, root + 8, update)
            || !read_le(payload, root + 16, px_exp) || !read_le(payload, root + 17, qty_exp)
            || !read_le(payload, root + 18, bid) || !read_le(payload, root + 26, bid_qty)
            || !read_le(payload, root + 34, ask) || !read_le(payload, root + 42, ask_qty))
            return BinanceSbeParseState::Invalid;
        BinanceSbeBestBidAsk parsed;
        parsed.event_time_us = event; parsed.book_update_id = update;
        parsed.bid = decimal(bid, px_exp); parsed.bid_qty = decimal(bid_qty, qty_exp);
        parsed.ask = decimal(ask, px_exp); parsed.ask_qty = decimal(ask_qty, qty_exp);
        if (!symbol_at(payload, kHeaderBytes + block, parsed.symbol)
            || parsed.event_time_us <= 0 || parsed.book_update_id <= 0
            || !std::isfinite(parsed.bid) || !std::isfinite(parsed.ask)
            || !std::isfinite(parsed.bid_qty) || !std::isfinite(parsed.ask_qty)
            || parsed.bid <= 0.0 || parsed.ask <= 0.0 || parsed.ask < parsed.bid
            || parsed.bid_qty < 0.0 || parsed.ask_qty < 0.0)
            return BinanceSbeParseState::Invalid;
        output = std::move(parsed);
        return BinanceSbeParseState::Parsed;
    } catch (...) { return BinanceSbeParseState::Invalid; }
}

BinanceSbeParseState parse_binance_sbe_trades(
    std::string_view payload, BinanceSbeTradeSummary& output) noexcept {
    try {
        std::uint16_t block = 0, id = 0, version = 0;
        if (!header(payload, block, id, version)) return BinanceSbeParseState::Invalid;
        if (id != kTradesTemplate) return BinanceSbeParseState::Ignored;
        if (version != 0 || block < kTradesRootBytes || payload.size() < kHeaderBytes + block + kGroupHeaderBytes + 1)
            return BinanceSbeParseState::Invalid;
        const std::size_t root = kHeaderBytes;
        std::int64_t event = 0, transact = 0; std::int8_t px_exp = 0, qty_exp = 0;
        if (!read_le(payload, root, event) || !read_le(payload, root + 8, transact)
            || !read_le(payload, root + 16, px_exp) || !read_le(payload, root + 17, qty_exp))
            return BinanceSbeParseState::Invalid;
        std::size_t cursor = kHeaderBytes + block;
        std::uint16_t trade_block = 0; std::uint32_t count = 0;
        if (!read_le(payload, cursor, trade_block) || !read_le(payload, cursor + 2, count)
            || trade_block < kTradeBlockBytes || count > kMaxTradesPerFrame)
            return BinanceSbeParseState::Invalid;
        cursor += kGroupHeaderBytes;
        if (count > 0 && (cursor > payload.size() || static_cast<std::uint64_t>(count) * trade_block > payload.size() - cursor))
            return BinanceSbeParseState::Invalid;
        BinanceSbeTradeSummary parsed; parsed.event_time_us = event; parsed.transact_time_us = transact; parsed.trade_count = count;
        for (std::uint32_t i = 0; i < count; ++i) {
            std::int64_t trade_id = 0, price_m = 0, qty_m = 0; std::uint8_t buyer_maker = 0;
            const auto base = cursor + static_cast<std::size_t>(i) * trade_block;
            if (!read_le(payload, base, trade_id) || !read_le(payload, base + 8, price_m)
                || !read_le(payload, base + 16, qty_m) || !read_le(payload, base + 24, buyer_maker))
                return BinanceSbeParseState::Invalid;
            const double price = decimal(price_m, px_exp), qty = decimal(qty_m, qty_exp);
            if (trade_id <= 0 || !std::isfinite(price) || !std::isfinite(qty) || price <= 0 || qty <= 0 || buyer_maker > 1)
                return BinanceSbeParseState::Invalid;
            if (i == 0) parsed.first_trade_id = trade_id;
            parsed.last_trade_id = trade_id; parsed.last_price = price; parsed.total_qty += qty;
            if (buyer_maker != 0) parsed.buyer_maker_qty += qty;
        }
        cursor += static_cast<std::size_t>(count) * trade_block;
        if (!symbol_at(payload, cursor, parsed.symbol) || event <= 0 || transact <= 0)
            return BinanceSbeParseState::Invalid;
        output = std::move(parsed);
        return BinanceSbeParseState::Parsed;
    } catch (...) { return BinanceSbeParseState::Invalid; }
}
} // namespace pm::v7::external_fair
