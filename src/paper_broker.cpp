#include "poly/paper_broker.hpp"
#include "poly/math.hpp"

#include <algorithm>
#include <cmath>

namespace poly {

PaperBroker::PaperBroker(double starting_cash) {
    state_.starting_cash = starting_cash;
    state_.cash = starting_cash;
    state_.equity = starting_cash;
    state_.peak_equity = starting_cash;
}

std::optional<Fill> PaperBroker::execute(const OrderIntent& order,
                                         const TokenMeta& meta,
                                         const BookSnapshot& book) {
    if (order.side != Side::Buy || order.shares <= 0.0 || book.asks.empty()) return std::nullopt;

    auto levels = book.asks;
    std::sort(levels.begin(), levels.end(), [](const BookLevel& a, const BookLevel& b){ return a.price < b.price; });
    const double top = levels.front().price;
    double remaining = order.shares;
    double shares = 0.0;
    double notional = 0.0;

    for (const auto& l : levels) {
        if (l.price > top + order.max_slippage) break;
        const double take = std::min(remaining, l.size);
        if (take <= 0.0) continue;
        shares += take;
        notional += take * l.price;
        remaining -= take;
        if (remaining <= 1e-9) break;
    }
    if (shares < book.min_order_size || shares <= 0.0) return std::nullopt;

    const double avg = notional / shares;
    const double fee = taker_fee_usdc(shares, avg, meta.category);
    const double cash_needed = notional + fee;
    if (cash_needed > state_.cash) {
        const double scale = std::max(0.0, state_.cash / std::max(1e-9, cash_needed));
        shares *= scale;
        notional *= scale;
        if (shares < book.min_order_size) return std::nullopt;
    }

    const double final_fee = taker_fee_usdc(shares, avg, meta.category);
    const double final_notional = shares * avg;
    state_.cash -= final_notional + final_fee;
    state_.total_fees += final_fee;

    auto& p = state_.positions[order.token_id];
    p.token_id = order.token_id;
    const double old_cost = p.avg_cost * p.shares;
    p.shares += shares;
    p.avg_cost = p.shares > 0.0 ? (old_cost + final_notional + final_fee) / p.shares : 0.0;
    p.mark = book.mid();

    Fill f;
    f.token_id = order.token_id;
    f.side = Side::Buy;
    f.shares = shares;
    f.avg_price = avg;
    f.fee = final_fee;
    f.slippage = avg - top;
    f.source = order.source;
    fills_.push_back(f);
    return f;
}

std::vector<Fill> PaperBroker::execute_basket(
        const std::vector<OrderIntent>& orders,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books) {
    struct Quote { Fill fill; double notional{0.0}; };
    std::vector<Quote> quotes;
    quotes.reserve(orders.size());
    double total_cash = 0.0;

    for (const auto& order : orders) {
        if (order.side != Side::Buy || order.shares <= 0.0) return {};
        const auto mit = meta.find(order.token_id);
        const auto bit = books.find(order.token_id);
        if (mit == meta.end() || bit == books.end() || bit->second.asks.empty()) return {};
        auto levels = bit->second.asks;
        std::sort(levels.begin(), levels.end(), [](const BookLevel& a, const BookLevel& b){ return a.price < b.price; });
        const double top = levels.front().price;
        double remaining = order.shares, shares = 0.0, notional = 0.0;
        for (const auto& l : levels) {
            if (l.price > top + order.max_slippage) break;
            const double take = std::min(remaining, l.size);
            if (take <= 0.0) continue;
            shares += take;
            notional += take * l.price;
            remaining -= take;
            if (remaining <= 1e-9) break;
        }
        // Exact baskets require full requested size on every leg; partial baskets are rejected.
        if (shares + 1e-8 < order.shares || shares < bit->second.min_order_size) return {};
        const double avg = notional / shares;
        const double fee = taker_fee_usdc(shares, avg, mit->second.category);
        Fill f;
        f.token_id = order.token_id;
        f.side = Side::Buy;
        f.shares = shares;
        f.avg_price = avg;
        f.fee = fee;
        f.slippage = avg - top;
        f.source = order.source;
        quotes.push_back({f, notional});
        total_cash += notional + fee;
    }
    if (quotes.empty() || total_cash > state_.cash) return {};

    std::vector<Fill> committed;
    committed.reserve(quotes.size());
    for (const auto& q : quotes) {
        const auto& f = q.fill;
        state_.cash -= q.notional + f.fee;
        state_.total_fees += f.fee;
        auto& p = state_.positions[f.token_id];
        p.token_id = f.token_id;
        const double old_cost = p.avg_cost * p.shares;
        p.shares += f.shares;
        p.avg_cost = p.shares > 0.0 ? (old_cost + q.notional + f.fee) / p.shares : 0.0;
        const auto bit = books.find(f.token_id);
        if (bit != books.end()) p.mark = bit->second.mid();
        fills_.push_back(f);
        committed.push_back(f);
    }
    return committed;
}

void PaperBroker::mark(const std::unordered_map<std::string, BookSnapshot>& books) {
    double market_value = 0.0;
    double unrealized = 0.0;
    double gross = 0.0;
    double net = 0.0;
    for (auto& [token, p] : state_.positions) {
        const auto bit = books.find(token);
        if (bit != books.end()) {
            const double m = bit->second.mid();
            if (std::isfinite(m)) p.mark = m;
        }
        const double mv = p.shares * p.mark;
        p.unrealized_pnl = p.shares * (p.mark - p.avg_cost);
        market_value += mv;
        unrealized += p.unrealized_pnl;
        gross += std::abs(mv);
        net += mv;
    }
    state_.unrealized_pnl = unrealized;
    state_.gross_exposure = gross;
    state_.net_exposure = net;
    state_.equity = state_.cash + market_value;
    state_.peak_equity = std::max(state_.peak_equity, state_.equity);
    state_.drawdown = state_.peak_equity > 0.0 ? 1.0 - state_.equity / state_.peak_equity : 0.0;
}

} // namespace poly
