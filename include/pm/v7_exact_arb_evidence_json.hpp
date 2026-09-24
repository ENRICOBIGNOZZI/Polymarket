#pragma once
// CONTROL/OFFLINE ONLY. Never include JSON serialization in the native kernel.
#include "pm/v7_market_state.hpp"
#include <boost/json.hpp>
#include <openssl/sha.h>
#include <string>
#include <string_view>

namespace pm::v7::exact_arb_graph {
inline std::string evidence_sha256(std::string_view bytes) {
    std::array<unsigned char, 32> hash{};
    SHA256(reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size(), hash.data());
    std::string result; result.reserve(64);
    for (auto byte : hash) { result += "0123456789abcdef"[byte >> 4]; result += "0123456789abcdef"[byte & 15]; }
    return result;
}
inline boost::json::object evidence_safety() {
    return {{"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
            {"real_capital_at_risk", false}, {"automatic_promotion", false}, {"execution_authority", false}};
}
inline boost::json::object book_evidence_json(const BookDeepSnapshot& book) {
    namespace json = boost::json;
    json::array bids, asks;
    for (std::size_t i = 0; i < book.bid_level_count && i < book.bid_levels.size(); ++i)
        bids.push_back(json::array{book.bid_levels[i].price_e4, book.bid_levels[i].quantity_microunits});
    for (std::size_t i = 0; i < book.ask_level_count && i < book.ask_levels.size(); ++i)
        asks.push_back(json::array{book.ask_levels[i].price_e4, book.ask_levels[i].quantity_microunits});
    return {{"state_version", book.state_version}, {"exchange_event_ns", book.exchange_event_ns},
            {"receive_monotonic_ns", book.receive_monotonic_ns}, {"tick_size_e4", book.tick_size_e4},
            {"lineage_continuous", book.lineage_continuous != 0}, {"valid", book.valid != 0},
            {"bid_truncated", book.bid_truncated != 0}, {"ask_truncated", book.ask_truncated != 0},
            {"bids_e4_microshares", std::move(bids)}, {"asks_e4_microshares", std::move(asks)}};
}
} // namespace pm::v7::exact_arb_graph
