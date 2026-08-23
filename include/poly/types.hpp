#pragma once

#include <chrono>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace poly {

using Clock = std::chrono::system_clock;
using TimePoint = Clock::time_point;

struct BookLevel {
    double price{0.0};
    double size{0.0};
};

struct HistoricalPoint {
    std::int64_t ts{0}; // Unix seconds.
    double price{0.0};
};

struct TokenMeta {
    std::string token_id;
    std::string market_id;
    std::string condition_id;
    std::string event_id;
    std::string event_title;
    std::string question;
    std::string slug;
    std::string category{"other"};
    std::string outcome;
    std::string end_date;
    bool active{true};
    bool closed{false};
    bool neg_risk{false};
    bool enable_order_book{true};
    double liquidity{0.0};
    double volume{0.0};
    double reference_price{std::numeric_limits<double>::quiet_NaN()};
};

struct BookSnapshot {
    std::string token_id;
    std::string market_id;
    TimePoint ts{Clock::now()};
    std::vector<BookLevel> bids;
    std::vector<BookLevel> asks;
    double last_trade{std::numeric_limits<double>::quiet_NaN()};
    double tick_size{0.01};
    double min_order_size{1.0};
    bool neg_risk{false};

    [[nodiscard]] double best_bid() const {
        if (bids.empty()) return std::numeric_limits<double>::quiet_NaN();
        double x = -std::numeric_limits<double>::infinity();
        for (const auto& l : bids) x = std::max(x, l.price);
        return x;
    }
    [[nodiscard]] double best_ask() const {
        if (asks.empty()) return std::numeric_limits<double>::quiet_NaN();
        double x = std::numeric_limits<double>::infinity();
        for (const auto& l : asks) x = std::min(x, l.price);
        return x;
    }
    [[nodiscard]] double mid() const {
        const auto b = best_bid();
        const auto a = best_ask();
        if (std::isfinite(a) && std::isfinite(b)) return 0.5 * (a + b);
        if (std::isfinite(last_trade)) return last_trade;
        return std::numeric_limits<double>::quiet_NaN();
    }
    [[nodiscard]] double spread() const {
        const auto b = best_bid();
        const auto a = best_ask();
        if (!std::isfinite(a) || !std::isfinite(b)) return std::numeric_limits<double>::infinity();
        return std::max(0.0, a - b);
    }
    [[nodiscard]] double bid_depth() const { double s = 0.0; for (const auto& l : bids) s += l.size; return s; }
    [[nodiscard]] double ask_depth() const { double s = 0.0; for (const auto& l : asks) s += l.size; return s; }
};

enum class Side { Buy, Sell };

enum class SignalKind {
    Pca,
    EwPca,
    RobustPca,
    HierarchicalPca,
    GraphResidual,
    LogicalArb,
    ExactBasketArb
};

inline const char* to_string(SignalKind k) {
    switch (k) {
        case SignalKind::Pca: return "pca";
        case SignalKind::EwPca: return "ew_pca";
        case SignalKind::RobustPca: return "robust_pca";
        case SignalKind::HierarchicalPca: return "hierarchical_pca";
        case SignalKind::GraphResidual: return "graph_residual";
        case SignalKind::LogicalArb: return "logical_arb";
        case SignalKind::ExactBasketArb: return "exact_basket_arb";
    }
    return "unknown";
}

struct Signal {
    SignalKind kind{SignalKind::Pca};
    std::string token_id;
    std::string market_id;
    std::string event_id;
    std::string question;
    Side side{Side::Buy};
    double observed_price{0.0};
    double fair_price{0.0};
    double gross_edge{0.0};
    double est_cost{0.0};
    double uncertainty{0.0};
    double net_edge{0.0};
    double score{0.0};
    double expected_half_life_min{0.0};
    double confidence{0.0};
    std::string rationale;
    TimePoint ts{Clock::now()};
};

struct OrderIntent {
    std::string token_id;
    Side side{Side::Buy};
    double shares{0.0};
    double max_slippage{0.02};
    SignalKind source{SignalKind::Pca};
};

struct Fill {
    std::string token_id;
    Side side{Side::Buy};
    double shares{0.0};
    double avg_price{0.0};
    double fee{0.0};
    double slippage{0.0};
    SignalKind source{SignalKind::Pca};
    TimePoint ts{Clock::now()};
};

struct Position {
    std::string token_id;
    double shares{0.0};
    double avg_cost{0.0};
    double mark{0.0};
    double realized_pnl{0.0};
    double unrealized_pnl{0.0};
    SignalKind source{SignalKind::Pca};
    TimePoint opened_at{Clock::now()};
    TimePoint last_trade_at{Clock::now()};
};

struct PortfolioState {
    double starting_cash{10000.0};
    double cash{10000.0};
    double equity{10000.0};
    double peak_equity{10000.0};
    double drawdown{0.0};
    double gross_exposure{0.0};
    double net_exposure{0.0};
    double total_fees{0.0};
    double realized_pnl{0.0};
    double unrealized_pnl{0.0};
    std::unordered_map<std::string, Position> positions;
};

struct EngineMetrics {
    std::size_t markets_discovered{0};
    std::size_t tokens_tracked{0};
    std::size_t books_live{0};
    std::size_t signals_live{0};
    std::size_t fills{0};
    std::uint64_t cycle{0};
    double last_cycle_ms{0.0};
    std::string status{"booting"};
};

} // namespace poly
