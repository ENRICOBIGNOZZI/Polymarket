#pragma once
#include "pm/http.hpp"
#include "pm/types.hpp"
#include <unordered_map>

namespace pm {
class PolymarketApi {
public:
    explicit PolymarketApi(Config cfg);
    std::vector<Market> discover_markets(std::size_t limit, double min_liquidity) const;
    std::unordered_map<std::string,Book> fetch_books(const std::vector<std::string>& token_ids) const;
    std::optional<Market> fetch_market_by_id(const std::string& id) const;
    FeeDetails fetch_fee_details(const Market& market) const;
private:
    Config cfg_;
    HttpClient http_;
};
}
