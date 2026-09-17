#include "pm/v7_binance_sbe.hpp"

#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <type_traits>

using namespace pm::v7::external_fair;

namespace {
template <class T>
void append_le(std::string& out, T value) {
    static_assert(std::is_integral_v<T>);
    using U = std::make_unsigned_t<T>;
    U bits = 0;
    std::memcpy(&bits, &value, sizeof(T));
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        out.push_back(static_cast<char>((bits >> (8U * i)) & 0xffU));
    }
}
void append_symbol(std::string& out, const std::string& symbol) {
    assert(symbol.size() < 256);
    out.push_back(static_cast<char>(symbol.size()));
    out += symbol;
}
std::string best_frame() {
    std::string out;
    append_le<std::uint16_t>(out, 50); append_le<std::uint16_t>(out, 10001);
    append_le<std::uint16_t>(out, 1); append_le<std::uint16_t>(out, 0);
    append_le<std::int64_t>(out, 1'789'650'000'123'456LL);
    append_le<std::int64_t>(out, 44'001);
    append_le<std::int8_t>(out, -2); append_le<std::int8_t>(out, -3);
    append_le<std::int64_t>(out, 6'500'123); append_le<std::int64_t>(out, 1'250);
    append_le<std::int64_t>(out, 6'500'456); append_le<std::int64_t>(out, 2'500);
    append_symbol(out, "BTCUSDT");
    return out;
}
std::string trades_frame() {
    std::string out;
    append_le<std::uint16_t>(out, 18); append_le<std::uint16_t>(out, 10000);
    append_le<std::uint16_t>(out, 1); append_le<std::uint16_t>(out, 0);
    append_le<std::int64_t>(out, 1'789'650'000'123'456LL);
    append_le<std::int64_t>(out, 1'789'650'000'122'999LL);
    append_le<std::int8_t>(out, -2); append_le<std::int8_t>(out, -3);
    append_le<std::uint16_t>(out, 25); append_le<std::uint32_t>(out, 2);
    append_le<std::int64_t>(out, 9001); append_le<std::int64_t>(out, 6'500'100);
    append_le<std::int64_t>(out, 1'500); append_le<std::uint8_t>(out, 1);
    append_le<std::int64_t>(out, 9002); append_le<std::int64_t>(out, 6'500'200);
    append_le<std::int64_t>(out, 2'000); append_le<std::uint8_t>(out, 0);
    append_symbol(out, "BTCUSDT");
    return out;
}
}

int main() {
    {
        BinanceSbeBestBidAsk value;
        auto payload = best_frame();
        assert(parse_binance_sbe_best_bid_ask(payload, value) == BinanceSbeParseState::Parsed);
        assert(value.event_time_us == 1'789'650'000'123'456LL);
        assert(value.book_update_id == 44'001);
        assert(value.symbol == "BTCUSDT");
        assert(std::abs(value.bid - 65001.23) < 1e-9);
        assert(std::abs(value.ask - 65004.56) < 1e-9);
        assert(std::abs(value.bid_qty - 1.25) < 1e-12);
        assert(std::abs(value.ask_qty - 2.5) < 1e-12);
        payload.pop_back();
        assert(parse_binance_sbe_best_bid_ask(payload, value) == BinanceSbeParseState::Invalid);
    }
    {
        BinanceSbeTradeSummary value;
        auto payload = trades_frame();
        assert(parse_binance_sbe_trades(payload, value) == BinanceSbeParseState::Parsed);
        assert(value.trade_count == 2);
        assert(value.first_trade_id == 9001 && value.last_trade_id == 9002);
        assert(value.symbol == "BTCUSDT");
        assert(std::abs(value.last_price - 65002.00) < 1e-9);
        assert(std::abs(value.total_qty - 3.5) < 1e-12);
        assert(std::abs(value.buyer_maker_qty - 1.5) < 1e-12);
        payload[2] = static_cast<char>(0xff); payload[3] = static_cast<char>(0x7f);
        assert(parse_binance_sbe_trades(payload, value) == BinanceSbeParseState::Ignored);
    }
    {
        auto payload = best_frame();
        payload[4] = 2; // wrong schema
        BinanceSbeBestBidAsk value;
        assert(parse_binance_sbe_best_bid_ask(payload, value) == BinanceSbeParseState::Invalid);
        payload = best_frame();
        payload[6] = 1; // unsupported stream schema version
        assert(parse_binance_sbe_best_bid_ask(payload, value) == BinanceSbeParseState::Invalid);
    }
    std::cout << "Binance SBE bounded decoder PASS\n";
}
