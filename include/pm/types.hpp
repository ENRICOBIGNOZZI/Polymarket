#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <map>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace pm {

struct Level {
    double price = 0.0;
    double size = 0.0;
};

struct Book {
    std::string token_id;
    std::vector<Level> bids;
    std::vector<Level> asks;
    double tick_size = 0.01;
    double min_order_size = 1.0;
    bool neg_risk = false;
    double last_trade = 0.0;

    [[nodiscard]] double best_bid() const {
        if (bids.empty()) return std::numeric_limits<double>::quiet_NaN();
        return std::max_element(bids.begin(), bids.end(), [](auto const& a, auto const& b){ return a.price < b.price; })->price;
    }
    [[nodiscard]] double best_ask() const {
        if (asks.empty()) return std::numeric_limits<double>::quiet_NaN();
        return std::min_element(asks.begin(), asks.end(), [](auto const& a, auto const& b){ return a.price < b.price; })->price;
    }
    [[nodiscard]] double midpoint() const {
        const auto b = best_bid(), a = best_ask();
        if (std::isfinite(b) && std::isfinite(a)) return 0.5 * (a + b);
        if (std::isfinite(last_trade) && last_trade > 0.0) return last_trade;
        return std::numeric_limits<double>::quiet_NaN();
    }
    [[nodiscard]] double spread() const {
        const auto b = best_bid(), a = best_ask();
        return (std::isfinite(b) && std::isfinite(a)) ? std::max(0.0, a-b) : 1.0;
    }
    [[nodiscard]] double top_depth(bool bid_side, std::size_t n = 3) const {
        auto v = bid_side ? bids : asks;
        if (bid_side) std::sort(v.begin(), v.end(), [](auto const& x, auto const& y){ return x.price > y.price; });
        else std::sort(v.begin(), v.end(), [](auto const& x, auto const& y){ return x.price < y.price; });
        double s = 0.0;
        for (std::size_t i=0; i<std::min(n, v.size()); ++i) s += std::max(0.0, v[i].size);
        return s;
    }
};

struct Market {
    std::string id;
    std::string condition_id;
    std::string slug;
    std::string question;
    std::string event_id;
    std::string yes_token;
    std::string no_token;
    double liquidity = 0.0;
    bool neg_risk = false;
    bool active = true;
    bool closed = false;
    std::optional<int> resolved_yes;
    double fee_rate = -1.0;
    double fee_exponent = 1.0;
    bool fee_taker_only = true;
};

struct FeeDetails {
    double rate = 0.0;
    double exponent = 1.0;
    bool taker_only = true;
};

struct ExpertPrediction {
    std::string name;
    double q_yes = 0.5;
    double confidence = 0.0;
};

struct Signal {
    std::string market_id;
    std::string slug;
    std::string side;
    double market_mid = 0.5;
    double executable_price = 0.5;
    double fair_side = 0.5;
    double fair_yes = 0.5;
    double uncertainty = 1.0;
    double fee_per_share = 0.0;
    double slippage_per_share = 0.0;
    double net_edge = -1.0;
    double score = -1.0;
    double desired_notional = 0.0;
    std::vector<ExpertPrediction> experts;
};

struct ExternalSignal {
    double q_yes = 0.5;
    double confidence = 0.0;
    std::string source;
    std::int64_t timestamp = 0;
};

struct Position {
    std::string market_id;
    std::string event_id;
    std::string slug;
    std::string side;
    std::string token_id;
    double shares = 0.0;
    double avg_price = 0.0;
    double cost_basis = 0.0;
    double fees_paid = 0.0;
};

struct Fill {
    std::int64_t ts = 0;
    std::string market_id;
    std::string slug;
    std::string action;
    std::string side;
    double shares = 0.0;
    double price = 0.0;
    double notional = 0.0;
    double fee = 0.0;
};

struct Config {
    std::string gamma_url = "https://gamma-api.polymarket.com";
    std::string clob_url = "https://clob.polymarket.com";
    std::string run_dir = "runs/paper";
    std::string external_signals_file = "data/external_signals.csv";
    std::size_t market_limit = 150;
    std::size_t books_batch_size = 80;
    int interval_seconds = 10;
    double starting_capital = 10000.0;
    double min_liquidity = 500.0;
    double min_net_edge = 0.01;
    double uncertainty_penalty = 0.25;
    double slippage_bps = 5.0;
    double fractional_kelly = 0.10;
    double max_trade_usd = 250.0;
    double max_market_fraction = 0.03;
    double max_event_fraction = 0.08;
    double max_gross_fraction = 0.25;
    double max_drawdown = 0.15;
    std::size_t pca_window = 40;
    std::size_t pca_min_history = 12;
    std::size_t pca_universe = 80;
    bool scan_only = false;
    std::map<std::string,double> expert_weights{{"micro",0.25},{"pca",0.35},{"graph",0.55},{"semantic",0.15},{"external",1.0}};
};

inline double clamp_prob(double p) { return std::clamp(p, 1e-6, 1.0-1e-6); }
inline double logit(double p) { p = clamp_prob(p); return std::log(p/(1.0-p)); }
inline double logistic(double z) { if (z>=0) { double e=std::exp(-z); return 1.0/(1.0+e); } double e=std::exp(z); return e/(1.0+e); }

} // namespace pm
