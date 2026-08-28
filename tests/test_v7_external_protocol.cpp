#include "pm/v7_external_protocol.hpp"

#include <array>
#include <cassert>
#include <cmath>
#include <iostream>

using namespace pm::v7::external_fair;

int main() {
    std::array<ExternalVenueEvent, 8> output{};

    const auto binance_book = decode_external_venue_frame(
        VenueId::BinanceSpot, 1, 10, 1'000, 2'000,
        R"({"u":400900217,"s":"BTCUSDT","b":"65000.10","B":"1.20","a":"65000.20","A":"2.30"})",
        output);
    assert(binance_book.invalid_frame == 0);
    assert(binance_book.output_count == 1);
    assert(output[0].event_type == ExternalEventType::BookTop);
    assert(output[0].source_sequence == 400900217);
    assert(output[0].exchange_event_ns == 0); // Binance bookTicker does not provide one.
    assert(std::abs(output[0].bid - 65000.10) < 1e-9);
    assert(std::abs(output[0].ask_size - 2.30) < 1e-9);

    const auto binance_trade = decode_external_venue_frame(
        VenueId::BinanceSpot, 1, 10, 1'010, 2'010,
        R"({"e":"aggTrade","E":1672515782136,"s":"BTCUSDT","a":12345,"p":"65000.50","q":"0.25","T":1672515782136,"m":false})",
        output);
    assert(binance_trade.output_count == 1);
    assert(output[0].event_type == ExternalEventType::Trade);
    assert(output[0].trade_side == 1);
    assert(output[0].exchange_event_ns == 1672515782136000000LL);

    const auto coinbase_ticker = decode_external_venue_frame(
        VenueId::CoinbaseSpot, 1, 20, 1'020, 2'020,
        R"({"channel":"ticker","timestamp":"2026-08-28T16:00:00.123456789Z","sequence_num":42,"events":[{"type":"update","tickers":[{"type":"ticker","product_id":"BTC-USD","price":"65001.0","best_bid":"65000.9","best_bid_quantity":"1.5","best_ask":"65001.1","best_ask_quantity":"1.7"}]}]})",
        output);
    assert(coinbase_ticker.output_count == 1);
    assert(output[0].event_type == ExternalEventType::BookTop);
    assert(output[0].source_sequence == 42);
    assert(output[0].exchange_event_ns > 0);
    assert(std::abs(output[0].bid_size - 1.5) < 1e-9);

    const auto coinbase_trade = decode_external_venue_frame(
        VenueId::CoinbaseSpot, 1, 20, 1'030, 2'030,
        R"({"channel":"market_trades","timestamp":"2026-08-28T16:00:00.250Z","sequence_num":43,"events":[{"type":"update","trades":[{"trade_id":"999","product_id":"BTC-USD","price":"65001.2","size":"0.1","side":"BUY","time":"2026-08-28T16:00:00.200Z"}]}]})",
        output);
    assert(coinbase_trade.output_count == 1);
    assert(output[0].trade_side == -1); // Coinbase side is maker side.
    assert(output[0].trade_price > 65001.1);

    const auto bybit_book = decode_external_venue_frame(
        VenueId::BybitSpot, 1, 30, 1'040, 2'040,
        R"({"topic":"orderbook.1.BTCUSDT","type":"snapshot","ts":1770000000123,"data":{"s":"BTCUSDT","b":[["65002.0","2.0"]],"a":[["65002.1","2.5"]],"u":100,"seq":200}})",
        output);
    assert(bybit_book.output_count == 1);
    assert(output[0].event_type == ExternalEventType::BookTop);
    assert(output[0].source_sequence == 100);
    assert(output[0].exchange_event_ns == 1770000000123000000LL);

    const auto bybit_trade = decode_external_venue_frame(
        VenueId::BybitSpot, 1, 30, 1'050, 2'050,
        R"({"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":1770000000223,"data":[{"T":1770000000200,"s":"BTCUSDT","S":"Sell","v":"0.4","p":"65002.0"}]})",
        output);
    assert(bybit_trade.output_count == 1);
    assert(output[0].event_type == ExternalEventType::Trade);
    assert(output[0].trade_side == -1);

    std::array<ExternalVenueEvent, 0> none{};
    const auto overflow = decode_external_venue_frame(
        VenueId::BinanceSpot, 1, 10, 1'060, 2'060,
        R"({"u":400900218,"s":"BTCUSDT","b":"65000.10","B":"1.20","a":"65000.20","A":"2.30"})",
        none);
    assert(overflow.output_count == 0);
    assert(overflow.output_overflow == 1);

    const auto invalid = decode_external_venue_frame(
        VenueId::BinanceSpot, 1, 10, 1'070, 2'070, "not-json", output);
    assert(invalid.invalid_frame == 1);

    std::cout << "v7 external protocol tests passed\n";
    return 0;
}
