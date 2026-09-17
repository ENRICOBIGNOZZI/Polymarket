#pragma once

#include <cstdint>
#include <string>
#include <string_view>

namespace pm::v7::external_fair {

enum class BinanceSbeParseState : std::uint8_t { Parsed = 0, Ignored = 1, Invalid = 2 };

struct BinanceSbeBestBidAsk {
    std::int64_t event_time_us = 0;
    std::int64_t book_update_id = 0;
    double bid = 0.0;
    double bid_qty = 0.0;
    double ask = 0.0;
    double ask_qty = 0.0;
    std::string symbol;
};

struct BinanceSbeTradeSummary {
    std::int64_t event_time_us = 0;
    std::int64_t transact_time_us = 0;
    std::uint32_t trade_count = 0;
    std::int64_t first_trade_id = 0;
    std::int64_t last_trade_id = 0;
    double last_price = 0.0;
    double total_qty = 0.0;
    double buyer_maker_qty = 0.0;
    std::string symbol;
};

[[nodiscard]] BinanceSbeParseState parse_binance_sbe_best_bid_ask(
    std::string_view payload, BinanceSbeBestBidAsk& output) noexcept;
[[nodiscard]] BinanceSbeParseState parse_binance_sbe_trades(
    std::string_view payload, BinanceSbeTradeSummary& output) noexcept;

} // namespace pm::v7::external_fair
