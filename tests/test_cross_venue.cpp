#include "pm/cross_venue.hpp"

#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

std::string header_value(const pm::cross::SignedHeaders& headers, const std::string& key) {
    for (const auto& [name, value] : headers.headers) {
        if (name == key) return value;
    }
    return {};
}

void test_limitless_hmac() {
    pm::cross::Credentials credentials;
    credentials.limitless_token_id = "token";
    credentials.limitless_token_secret = "dGVzdC1zZWNyZXQtMzItYnl0ZXMtYWFhYWFhYWFhYWE=";
    const auto signed_headers = pm::cross::sign_limitless_request(
        credentials,
        "GET",
        "/portfolio/positions",
        "",
        std::string("2026-08-24T18:00:00.000Z"));
    require(
        header_value(signed_headers, "lmts-signature") ==
            "bLDI/Snixfzkh96SL4gh/bTHAMmbZbH8Ug5G3o1PZ1c=",
        "Limitless HMAC signature mismatch");
}

void test_market_parsers() {
    const std::string limitless_market = R"({
      "id": 7,
      "slug": "btc-above-100k",
      "title": "Will Bitcoin be above $100,000 on August 31?",
      "status": "active",
      "tradeType": "clob",
      "conditionId": "0xabc",
      "expirationTimestamp": 1788134400000,
      "liquidity": "25000.5",
      "volume": "7500.0",
      "tokens": {"yes": "101", "no": "102"}
    })";
    const auto market = pm::cross::parse_limitless_market_payload(limitless_market);
    require(market.venue == pm::cross::Venue::Limitless, "wrong Limitless venue");
    require(market.market_id == "7" && market.slug == "btc-above-100k", "wrong Limitless identity");
    require(market.yes_token == "101" && market.no_token == "102", "wrong Limitless tokens");

    const std::string limitless_book = R"({
      "bids": [{"price": 0.47, "size": 10000000, "side": "BUY"}],
      "asks": [{"price": 0.49, "size": 12000000, "side": "SELL"}],
      "tokenId": "101",
      "adjustedMidpoint": 0.48,
      "maxSpread": "0.10",
      "minSize": "1",
      "lastTradePrice": 0.48
    })";
    const auto limitless_books =
        pm::cross::parse_limitless_orderbook_payload(limitless_book, "101", "102");
    require(std::abs(limitless_books.yes.asks.front().size - 12.0) < 1e-12,
            "Limitless fixed-point size not normalized");
    require(std::abs(limitless_books.no.bids.front().price - 0.51) < 1e-12,
            "Limitless NO complement book is wrong");

    const std::string kalshi_markets = R"({
      "markets": [{
        "ticker": "KXBTC-26AUG31-T100000",
        "event_ticker": "KXBTC-26AUG31",
        "market_type": "binary",
        "title": "Will Bitcoin be above $100,000 on August 31?",
        "status": "open",
        "close_time": "2026-08-31T23:59:00Z",
        "liquidity_dollars": "5000.00",
        "volume_24h_fp": "100.00",
        "rules_primary": "Settles yes if the reference price is above 100000."
      }],
      "cursor": "next"
    })";
    std::string cursor;
    const auto kalshi = pm::cross::parse_kalshi_markets_payload(kalshi_markets, &cursor);
    require(kalshi.size() == 1 && cursor == "next", "Kalshi market parser failed");

    const std::string kalshi_book = R"({
      "orderbook_fp": {
        "yes_dollars": [["0.4700", "10.00"]],
        "no_dollars": [["0.5100", "8.00"]]
      }
    })";
    const auto kalshi_books =
        pm::cross::parse_kalshi_orderbook_payload(kalshi_book, "KXBTC-26AUG31-T100000");
    require(std::abs(kalshi_books.yes.best_ask() - 0.49) < 1e-12,
            "Kalshi YES ask complement is wrong");
    require(std::abs(kalshi_books.no.best_ask() - 0.53) < 1e-12,
            "Kalshi NO ask complement is wrong");
}

void test_matching_and_arbitrage() {
    require(pm::cross::numeric_tokens_equal(
                "Will BTC exceed 100000 on August 31 2026?",
                "Bitcoin above $100,000 by August 31, 2026"),
            "numeric token equality failed");
    require(pm::cross::title_similarity(
                "Will Bitcoin be above 100000 on August 31?",
                "Bitcoin above 100000 on August 31") > 0.80,
            "title similarity too low");

    pm::cross::VenueMarket a;
    a.venue = pm::cross::Venue::Polymarket;
    a.market_id = "poly";
    pm::cross::VenueMarket b;
    b.venue = pm::cross::Venue::Limitless;
    b.market_id = "limitless";
    pm::cross::OutcomeBooks books_a;
    books_a.yes.asks = {{0.45, 100.0}};
    books_a.no.asks = {{0.55, 100.0}};
    pm::cross::OutcomeBooks books_b;
    books_b.yes.asks = {{0.50, 100.0}};
    books_b.no.asks = {{0.50, 100.0}};
    pm::cross::VerifiedPair pair;
    pair.pair_id = "pair";
    pair.venue_a = a.venue;
    pair.market_a = a.market_id;
    pair.venue_b = b.venue;
    pair.market_b = b.market_id;
    pair.relation = pm::cross::Relation::SameEvent;
    pair.settlement_verified = true;
    pm::cross::Policy policy;
    policy.max_bundle_usd = 50.0;
    policy.min_net_edge_per_share = 0.001;
    policy.slippage_bps = 0.0;
    policy.latency_reserve_bps = 0.0;
    policy.fee_reserve_bps[pm::cross::Venue::Polymarket] = 0.0;
    policy.fee_reserve_bps[pm::cross::Venue::Limitless] = 0.0;
    const auto opportunities = pm::cross::evaluate_pair(pair, a, books_a, b, books_b, policy, 50.0);
    require(opportunities.size() == 2, "expected both hard-arbitrage directions");
    require(opportunities.front().executable, "profitable hard arbitrage was rejected");
    require(std::abs(opportunities.front().net_edge_per_share - 0.05) < 1e-9,
            "hard-arbitrage edge is wrong");
}

void test_pair_manifest_and_gate() {
    const auto root = std::filesystem::temp_directory_path() / "pm-cross-venue-test";
    std::filesystem::create_directories(root);
    const auto manifest = root / "pairs.csv";
    {
        std::ofstream output(manifest);
        output << "pair_id,venue_a,market_a,venue_b,market_b,relation,settlement_verified,max_close_diff_seconds,notes\n";
        output << "x,polymarket,p,kalshi,k,same_event,true,60,reviewed\n";
    }
    const auto pairs = pm::cross::load_pair_manifest(manifest);
    require(pairs.size() == 1 && pairs.front().settlement_verified, "pair manifest parser failed");

    const auto gate_path = root / "limits.json";
    {
        std::ofstream output(gate_path);
        const auto now = std::time(nullptr);
        output << "{\"schema_version\":1,\"timestamp\":" << now
               << ",\"global_kill\":false,\"engines\":{\"cross_venue\":{"
                  "\"capital_limit_usd\":2500,\"max_bundle_usd\":25,"
                  "\"new_exposure_allowed\":true,\"reason\":\"ok\"}}}";
    }
    const auto gate = pm::cross::load_supervisor_gate(gate_path);
    require(gate.available && gate.new_exposure_allowed && gate.capital_limit_usd == 2500.0,
            "supervisor gate parser failed");
    std::filesystem::remove_all(root);
}

} // namespace

int main() {
    try {
        test_limitless_hmac();
        test_market_parsers();
        test_matching_and_arbitrage();
        test_pair_manifest_and_gate();
        std::cout << "cross-venue tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "cross-venue test failure: " << error.what() << '\n';
        return 1;
    }
}
