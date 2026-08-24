#pragma once
#include "pm/http.hpp"
#include "pm/market_data.hpp"
#include "pm/types.hpp"
#include <unordered_map>

namespace pm {

std::string history_interval_for_range(std::int64_t start_ts, std::int64_t end_ts);

class PolymarketApi {
public:
    explicit PolymarketApi(Config cfg);
    std::vector<Market> discover_markets(std::size_t limit, double min_liquidity) const;
    std::vector<Market> fetch_event_markets(const std::string& event_id) const;
    std::unordered_map<std::string,Book> fetch_books(const std::vector<std::string>& token_ids) const;
    std::unordered_map<std::string,std::vector<PricePoint>> fetch_price_history(
        const std::vector<std::string>& token_ids,
        std::int64_t start_ts,
        std::int64_t end_ts,
        std::size_t fidelity_minutes) const;
    std::optional<Market> fetch_market_by_id(const std::string& id) const;
    std::optional<MarketTiming> fetch_market_timing(const std::string& id) const;
    std::vector<RecentTrade> fetch_recent_trades(
        const std::vector<std::string>& condition_ids,
        std::int64_t start_ts,
        std::int64_t end_ts,
        std::size_t limit_per_market = 1000) const;
    FeeDetails fetch_fee_details(const Market& market) const;
private:
    Config cfg_;
    HttpClient http_;
};
}
