#pragma once
#include "pm/http.hpp"
#include "pm/market_data.hpp"
#include "pm/types.hpp"
#include <boost/json.hpp>
#include <cmath>
#include <stdexcept>
#include <unordered_map>

namespace pm {
class PolymarketApi {
public:
    explicit PolymarketApi(Config cfg);
    std::vector<Market> fetch_event_markets(const std::string& event_id) const;
    std::unordered_map<std::string,Book> fetch_books(const std::vector<std::string>& token_ids) const;
    double fetch_tick_size(const std::string& token_id) const {
        if (token_id.empty()) throw std::runtime_error("CLOB tick-size token id is empty");
        for (const unsigned char ch : token_id) {
            const bool safe = (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'z')
                || (ch >= 'A' && ch <= 'Z') || ch == '-' || ch == '_' || ch == '.' || ch == '~';
            if (!safe) throw std::runtime_error("CLOB tick-size token id is not URL-safe");
        }
        const auto response = http_.get(cfg_.clob_url + "/tick-size?token_id=" + token_id);
        if (response.status < 200 || response.status >= 300) {
            throw std::runtime_error(
                "CLOB tick-size HTTP " + std::to_string(response.status) + ": "
                + response.body.substr(0, 300));
        }
        const auto root = boost::json::parse(response.body);
        if (!root.is_object()) throw std::runtime_error("Unexpected CLOB tick-size response");
        const auto it = root.as_object().find("minimum_tick_size");
        if (it == root.as_object().end()) throw std::runtime_error("CLOB tick-size response missing minimum_tick_size");
        double tick = 0.0;
        const auto& value = it->value();
        if (value.is_double()) tick = value.as_double();
        else if (value.is_int64()) tick = static_cast<double>(value.as_int64());
        else if (value.is_uint64()) tick = static_cast<double>(value.as_uint64());
        else if (value.is_string()) tick = std::stod(std::string(value.as_string()));
        else throw std::runtime_error("CLOB tick-size response has non-numeric minimum_tick_size");
        if (!std::isfinite(tick) || tick <= 0.0 || tick > 1.0) {
            throw std::runtime_error("CLOB tick-size response has invalid minimum_tick_size");
        }
        return tick;
    }
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
