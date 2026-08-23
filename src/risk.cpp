#include "poly/risk.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <unordered_map>

namespace poly {

RiskEngine::RiskEngine(RiskConfig cfg) : cfg_(std::move(cfg)) {}

double RiskEngine::risk_multiplier(const PortfolioState& p) const {
    const double d = std::max(0.0, p.drawdown);
    if (d >= cfg_.emergency_flatten_drawdown) return 0.0;
    if (d <= cfg_.soft_drawdown) return 1.0;
    if (d >= cfg_.hard_deleverage_drawdown) {
        const double width = std::max(1e-6, cfg_.max_drawdown - cfg_.hard_deleverage_drawdown);
        return std::clamp(0.25 * (cfg_.max_drawdown - d) / width, 0.0, 0.25);
    }
    const double x = (d - cfg_.soft_drawdown) /
                     std::max(1e-6, cfg_.hard_deleverage_drawdown - cfg_.soft_drawdown);
    return std::clamp(1.0 - 0.75 * x, 0.25, 1.0);
}

std::vector<OrderIntent> RiskEngine::allocate(
        const std::vector<Signal>& signals,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books,
        const PortfolioState& portfolio) const {
    std::vector<OrderIntent> orders;
    const double rm = risk_multiplier(portfolio);
    if (rm <= 0.0 || portfolio.equity <= 0.0) return orders;

    const double gross_cap = cfg_.max_gross_fraction * portfolio.equity * rm;
    double remaining_gross = std::max(0.0, gross_cap - portfolio.gross_exposure);
    if (remaining_gross <= 0.0) return orders;

    std::unordered_map<std::string, double> event_alloc;
    std::unordered_map<std::string, double> token_alloc;
    for (const auto& [token, pos] : portfolio.positions) {
        const double existing = std::abs(pos.shares * pos.mark);
        token_alloc[token] += existing;
        const auto mit = meta.find(token);
        if (mit != meta.end()) event_alloc[mit->second.event_id] += existing;
    }

    // The same token may be signalled by several models. Keep the strongest one to avoid
    // accidental double-counting of highly correlated alpha estimates.
    std::unordered_map<std::string, const Signal*> best;
    for (const auto& s : signals) {
        if (s.side != Side::Buy || s.net_edge < cfg_.min_net_edge || s.score < cfg_.min_score) continue;
        auto it = best.find(s.token_id);
        if (it == best.end() || s.score > it->second->score) best[s.token_id] = &s;
    }

    std::vector<const Signal*> ranked;
    ranked.reserve(best.size());
    for (auto& [_, ptr] : best) ranked.push_back(ptr);
    std::sort(ranked.begin(), ranked.end(), [](const Signal* a, const Signal* b){
        return a->score > b->score;
    });

    for (const Signal* sp : ranked) {
        if (remaining_gross <= 0.0) break;
        const auto mit = meta.find(sp->token_id);
        const auto bit = books.find(sp->token_id);
        if (mit == meta.end() || bit == books.end()) continue;
        const double ask = bit->second.best_ask();
        if (!std::isfinite(ask) || ask <= 0.0 || ask >= 1.0) continue;

        const double token_cap = cfg_.max_token_fraction * portfolio.equity * rm;
        const double event_cap = cfg_.max_event_fraction * portfolio.equity * rm;
        const double trade_cap = cfg_.max_trade_fraction * portfolio.equity * rm;
        const double event_used = event_alloc[mit->second.event_id];
        const double token_used = token_alloc[sp->token_id];

        double visible_notional = 0.0;
        for (const auto& l : bit->second.asks) visible_notional += l.price * l.size;
        const double depth_cap = cfg_.depth_fraction * visible_notional;

        const double payoff_var = std::max(1e-4, ask * (1.0 - ask));
        const double model_var = sp->uncertainty * sp->uncertainty;
        const double kelly_fraction = std::clamp(0.25 * sp->net_edge / (payoff_var + model_var),
                                                 0.0, cfg_.max_token_fraction);
        const double desired = kelly_fraction * portfolio.equity * rm;
        double notional = std::min({desired,
                                    trade_cap,
                                    std::max(0.0, token_cap - token_used),
                                    std::max(0.0, event_cap - event_used),
                                    remaining_gross,
                                    depth_cap});
        if (notional <= ask * bit->second.min_order_size) continue;
        const double shares = notional / ask;
        if (shares < bit->second.min_order_size) continue;

        orders.push_back(OrderIntent{
            .token_id = sp->token_id,
            .side = Side::Buy,
            .shares = shares,
            .max_slippage = std::min(0.05, std::max(0.005, 2.0 * bit->second.spread())),
            .source = sp->kind
        });
        token_alloc[sp->token_id] += notional;
        event_alloc[mit->second.event_id] += notional;
        remaining_gross -= notional;
    }
    return orders;
}

std::vector<OrderIntent> RiskEngine::exits(
        const std::vector<Signal>& signals,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books,
        const PortfolioState& portfolio) const {
    std::vector<OrderIntent> orders;
    if (portfolio.positions.empty()) return orders;

    std::unordered_map<std::string, double> best_live_edge;
    for (const auto& s : signals) {
        if (s.side != Side::Buy || s.kind == SignalKind::ExactBasketArb) continue;
        auto& x = best_live_edge[s.token_id];
        x = std::max(x, s.net_edge);
    }

    const bool emergency = portfolio.drawdown >= cfg_.emergency_flatten_drawdown;
    const auto now = Clock::now();
    for (const auto& [token, pos] : portfolio.positions) {
        if (pos.shares <= 0.0) continue;
        const auto mit = meta.find(token);
        const auto bit = books.find(token);
        if (mit == meta.end() || bit == books.end()) continue;
        const double bid = bit->second.best_bid();
        if (!std::isfinite(bid) || bid <= 0.0) continue;

        // Locked arbitrage baskets are held unless the portfolio-level emergency
        // drawdown controller requires a flatten. Statistical positions use hysteresis:
        // they can only exit after a minimum hold and once net alpha has decayed.
        if (!emergency && pos.source == SignalKind::ExactBasketArb) continue;
        const auto age = std::chrono::duration_cast<std::chrono::seconds>(now - pos.opened_at).count();
        const double live_edge = best_live_edge.contains(token) ? best_live_edge.at(token) : 0.0;
        if (!emergency && (age < cfg_.min_hold_seconds || live_edge > cfg_.exit_net_edge)) continue;

        orders.push_back(OrderIntent{
            .token_id = token,
            .side = Side::Sell,
            .shares = pos.shares,
            .max_slippage = emergency ? 0.08 : std::min(0.05, std::max(0.005, 2.0 * bit->second.spread())),
            .source = pos.source
        });
    }
    return orders;
}

} // namespace poly
