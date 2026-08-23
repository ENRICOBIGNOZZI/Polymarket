#include "poly/engine.hpp"
#include "poly/math.hpp"

#include <json-c/json.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <limits>
#include <sstream>

namespace poly {
namespace {

long long epoch_ms(TimePoint tp) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(tp.time_since_epoch()).count();
}

json_object* signal_to_json(const Signal& s) {
    json_object* x = json_object_new_object();
    json_object_object_add(x, "engine", json_object_new_string(to_string(s.kind)));
    json_object_object_add(x, "token_id", json_object_new_string(s.token_id.c_str()));
    json_object_object_add(x, "market_id", json_object_new_string(s.market_id.c_str()));
    json_object_object_add(x, "event_id", json_object_new_string(s.event_id.c_str()));
    json_object_object_add(x, "question", json_object_new_string(s.question.c_str()));
    json_object_object_add(x, "side", json_object_new_string(s.side == Side::Buy ? "BUY" : "SELL"));
    json_object_object_add(x, "price", json_object_new_double(s.observed_price));
    json_object_object_add(x, "fair", json_object_new_double(s.fair_price));
    json_object_object_add(x, "gross_edge", json_object_new_double(s.gross_edge));
    json_object_object_add(x, "cost", json_object_new_double(s.est_cost));
    json_object_object_add(x, "uncertainty", json_object_new_double(s.uncertainty));
    json_object_object_add(x, "net_edge", json_object_new_double(s.net_edge));
    json_object_object_add(x, "score", json_object_new_double(s.score));
    json_object_object_add(x, "confidence", json_object_new_double(s.confidence));
    json_object_object_add(x, "rationale", json_object_new_string(s.rationale.c_str()));
    json_object_object_add(x, "ts", json_object_new_int64(epoch_ms(s.ts)));
    return x;
}

std::string json_string(json_object* obj) {
    const std::string s = json_object_to_json_string_ext(obj, JSON_C_TO_STRING_PLAIN);
    json_object_put(obj);
    return s;
}

} // namespace

QuantEngine::QuantEngine(EngineConfig engine_cfg,
                         ClientConfig client_cfg,
                         ModelConfig model_cfg,
                         RiskConfig risk_cfg)
    : engine_cfg_(std::move(engine_cfg)),
      client_(std::move(client_cfg)),
      stat_model_(std::move(model_cfg)),
      risk_(std::move(risk_cfg)),
      broker_(engine_cfg_.starting_cash) {}

QuantEngine::~QuantEngine() { stop(); }

void QuantEngine::start() {
    if (running_.exchange(true)) return;
    worker_ = std::thread([this] {
        while (running_.load()) {
            const auto begin = std::chrono::steady_clock::now();
            try {
                run_cycle();
            } catch (const std::exception& e) {
                std::lock_guard lock(mu_);
                metrics_.status = std::string("error: ") + e.what();
                std::cerr << metrics_.status << '\n';
            }
            const auto elapsed = std::chrono::steady_clock::now() - begin;
            const auto target = std::chrono::seconds(std::max(1, engine_cfg_.cycle_seconds));
            if (elapsed < target) std::this_thread::sleep_for(target - elapsed);
        }
    });
}

void QuantEngine::stop() {
    running_.store(false);
    if (worker_.joinable()) worker_.join();
}

void QuantEngine::run_cycle() {
    const auto t0 = std::chrono::steady_clock::now();
    std::unordered_map<std::string, TokenMeta> meta_copy;
    {
        std::lock_guard lock(mu_);
        if (meta_.empty() || metrics_.cycle % std::max(1, engine_cfg_.rediscovery_cycles) == 0) {
            metrics_.status = "discovering";
        } else {
            meta_copy = meta_;
        }
    }

    if (meta_copy.empty()) {
        meta_copy = client_.discover_tokens();
        std::lock_guard lock(mu_);
        meta_ = meta_copy;
        metrics_.markets_discovered = 0;
        std::unordered_map<std::string, bool> unique_markets;
        for (const auto& [_, m] : meta_) unique_markets[m.market_id] = true;
        metrics_.markets_discovered = unique_markets.size();
        metrics_.tokens_tracked = meta_.size();
    }

    if (!warm_started_) {
        { std::lock_guard lock(mu_); metrics_.status = "warm_start"; }
        const auto history = client_.fetch_warm_history(meta_copy);
        stat_model_.seed_prices(history);
        warm_started_ = true;
    }

    auto books = client_.fetch_books(meta_copy);
    stat_model_.observe(meta_copy, books);
    PortfolioState portfolio_before;
    {
        std::lock_guard lock(mu_);
        broker_.mark(books);
        portfolio_before = broker_.state();
    }

    auto stat_signals = stat_model_.generate(meta_copy, books);
    auto arb_signals = arb_model_.generate(meta_copy, books);
    std::vector<Signal> combined;
    combined.reserve(stat_signals.size() + arb_signals.size());
    combined.insert(combined.end(), stat_signals.begin(), stat_signals.end());
    combined.insert(combined.end(), arb_signals.begin(), arb_signals.end());
    std::sort(combined.begin(), combined.end(), [](const Signal& a, const Signal& b){ return a.score > b.score; });
    if (combined.size() > 500) combined.resize(500);

    // Exit logic runs before new allocation. Statistical positions are closed once
    // their live net alpha has decayed below the exit threshold after a short hysteresis.
    // At the emergency drawdown threshold every liquid position, including arb baskets,
    // is flattened before any new risk can be considered.
    const auto exit_orders = risk_.exits(stat_signals, meta_copy, books, portfolio_before);
    PortfolioState portfolio_after_exits = portfolio_before;
    {
        std::lock_guard lock(mu_);
        if (engine_cfg_.paper_trading) {
            for (const auto& o : exit_orders) {
                const auto mit = meta_copy.find(o.token_id);
                const auto bit = books.find(o.token_id);
                if (mit != meta_copy.end() && bit != books.end()) broker_.execute(o, mit->second, bit->second);
            }
        }
        broker_.mark(books);
        portfolio_after_exits = broker_.state();
    }

    // Only statistical alpha is allowed into the ordinary allocator. Logical consistency
    // candidates stay visible but do not create P&L. Exact binary baskets are handled
    // separately as all-or-nothing paper transactions.
    std::vector<Signal> allocatable;
    for (const auto& s : combined) {
        if (s.kind != SignalKind::ExactBasketArb && s.kind != SignalKind::LogicalArb) {
            allocatable.push_back(s);
        }
    }
    const auto orders = risk_.allocate(allocatable, meta_copy, books, portfolio_after_exits);

    struct BasketCandidate {
        double edge{0.0};
        std::string event_id;
        double approx_notional{0.0};
        std::vector<OrderIntent> orders;
    };
    std::vector<BasketCandidate> baskets;
    if (engine_cfg_.paper_trading && risk_.risk_multiplier(portfolio_after_exits) > 0.0) {
        std::unordered_map<std::string, double> existing_event_exposure;
        for (const auto& [token, pos] : portfolio_after_exits.positions) {
            const auto mit = meta_copy.find(token);
            if (mit == meta_copy.end()) continue;
            existing_event_exposure[mit->second.event_id] += std::abs(pos.shares * pos.mark);
        }

        std::unordered_map<std::string, std::vector<const Signal*>> groups;
        for (const auto& s : arb_signals) {
            if (s.kind != SignalKind::ExactBasketArb) continue;
            // In v0.1 ExactBasketArb is restricted to a fully known binary YES/NO market.
            groups["m:" + s.market_id].push_back(&s);
        }
        for (const auto& [_, legs] : groups) {
            if (legs.size() < 2) continue;
            double cost_per_share = 0.0;
            const double payout = 1.0;
            double max_equal_shares = std::numeric_limits<double>::infinity();
            bool ok = true;
            for (const auto* sp : legs) {
                const auto mit = meta_copy.find(sp->token_id);
                const auto bit = books.find(sp->token_id);
                if (mit == meta_copy.end() || bit == books.end()) { ok = false; break; }
                const double ask = bit->second.best_ask();
                if (!std::isfinite(ask)) { ok = false; break; }
                cost_per_share += ask + taker_fee_usdc(1.0, ask, mit->second.category);
                double ask_depth = 0.0;
                for (const auto& l : bit->second.asks) ask_depth += l.size;
                max_equal_shares = std::min(max_equal_shares, 0.05 * ask_depth);
            }
            const double edge = payout - cost_per_share;
            if (!ok || edge <= 0.001 || !(max_equal_shares > 0.0)) continue;
            const std::string event_id = legs.front()->event_id;
            const double event_cap = 0.10 * portfolio_after_exits.equity;
            const double event_room = std::max(0.0, event_cap - existing_event_exposure[event_id]);
            const double basket_budget = std::min(
                0.01 * portfolio_after_exits.equity * risk_.risk_multiplier(portfolio_after_exits),
                event_room);
            const double shares = std::min(max_equal_shares, basket_budget / std::max(1e-6, cost_per_share));
            if (shares <= 0.0) continue;
            BasketCandidate bc;
            bc.edge = edge;
            bc.event_id = event_id;
            for (const auto* sp : legs) {
                const auto& b = books.at(sp->token_id);
                if (shares < b.min_order_size) { bc.orders.clear(); break; }
                bc.orders.push_back(OrderIntent{
                    .token_id = sp->token_id,
                    .side = Side::Buy,
                    .shares = shares,
                    .max_slippage = std::min(0.02, std::max(0.002, b.spread())),
                    .source = SignalKind::ExactBasketArb
                });
            }
            if (bc.orders.size() == legs.size()) {
                bc.approx_notional = shares * cost_per_share;
                existing_event_exposure[event_id] += bc.approx_notional;
                baskets.push_back(std::move(bc));
            }
        }
        std::sort(baskets.begin(), baskets.end(), [](const auto& a, const auto& b){ return a.edge > b.edge; });
    }

    {
        std::lock_guard lock(mu_);
        if (engine_cfg_.paper_trading) {
            for (const auto& o : orders) {
                const auto mit = meta_copy.find(o.token_id);
                const auto bit = books.find(o.token_id);
                if (mit != meta_copy.end() && bit != books.end()) broker_.execute(o, mit->second, bit->second);
            }
            double basket_spent_budget = 0.0;
            const double basket_cycle_cap = 0.10 * portfolio_after_exits.equity * risk_.risk_multiplier(portfolio_after_exits);
            for (const auto& basket : baskets) {
                const double approx = basket.approx_notional;
                if (basket_spent_budget + approx > basket_cycle_cap) continue;
                const auto fills = broker_.execute_basket(basket.orders, meta_copy, books);
                if (fills.size() == basket.orders.size()) basket_spent_budget += approx;
            }
        }
        broker_.mark(books);
    }

    const auto dt = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    {
        std::lock_guard lock(mu_);
        books_ = std::move(books);
        signals_ = std::move(combined);
        metrics_.books_live = books_.size();
        metrics_.signals_live = signals_.size();
        metrics_.fills = broker_.fills().size();
        metrics_.last_cycle_ms = dt;
        ++metrics_.cycle;
        equity_curve_.emplace_back(epoch_ms(Clock::now()), broker_.state().equity);
        while (equity_curve_.size() > 10000) equity_curve_.pop_front();
        metrics_.status = risk_.risk_multiplier(broker_.state()) <= 0.0 ? "risk_halt" : "paper_live";
    }
}

std::string QuantEngine::state_json() const {
    std::lock_guard lock(mu_);
    const auto& p = broker_.state();
    json_object* root = json_object_new_object();
    json_object_object_add(root, "status", json_object_new_string(metrics_.status.c_str()));
    json_object_object_add(root, "cycle", json_object_new_int64(static_cast<long long>(metrics_.cycle)));
    json_object_object_add(root, "markets", json_object_new_int64(metrics_.markets_discovered));
    json_object_object_add(root, "tokens", json_object_new_int64(metrics_.tokens_tracked));
    json_object_object_add(root, "books", json_object_new_int64(metrics_.books_live));
    json_object_object_add(root, "signals", json_object_new_int64(metrics_.signals_live));
    json_object_object_add(root, "fills", json_object_new_int64(metrics_.fills));
    json_object_object_add(root, "cycle_ms", json_object_new_double(metrics_.last_cycle_ms));
    json_object_object_add(root, "cash", json_object_new_double(p.cash));
    json_object_object_add(root, "equity", json_object_new_double(p.equity));
    json_object_object_add(root, "pnl", json_object_new_double(p.equity - p.starting_cash));
    json_object_object_add(root, "drawdown", json_object_new_double(p.drawdown));
    json_object_object_add(root, "gross_exposure", json_object_new_double(p.gross_exposure));
    json_object_object_add(root, "net_exposure", json_object_new_double(p.net_exposure));
    json_object_object_add(root, "fees", json_object_new_double(p.total_fees));
    json_object_object_add(root, "realized_pnl", json_object_new_double(p.realized_pnl));
    json_object_object_add(root, "unrealized_pnl", json_object_new_double(p.unrealized_pnl));
    json_object_object_add(root, "positions", json_object_new_int64(p.positions.size()));
    return json_string(root);
}

std::string QuantEngine::signals_json() const {
    std::lock_guard lock(mu_);
    json_object* arr = json_object_new_array();
    for (const auto& s : signals_) json_object_array_add(arr, signal_to_json(s));
    return json_string(arr);
}

std::string QuantEngine::positions_json() const {
    std::lock_guard lock(mu_);
    json_object* arr = json_object_new_array();
    for (const auto& [token, p] : broker_.state().positions) {
        json_object* x = json_object_new_object();
        json_object_object_add(x, "token_id", json_object_new_string(token.c_str()));
        auto mit = meta_.find(token);
        if (mit != meta_.end()) {
            json_object_object_add(x, "question", json_object_new_string(mit->second.question.c_str()));
            json_object_object_add(x, "outcome", json_object_new_string(mit->second.outcome.c_str()));
            json_object_object_add(x, "category", json_object_new_string(mit->second.category.c_str()));
        }
        json_object_object_add(x, "shares", json_object_new_double(p.shares));
        json_object_object_add(x, "avg_cost", json_object_new_double(p.avg_cost));
        json_object_object_add(x, "mark", json_object_new_double(p.mark));
        json_object_object_add(x, "unrealized_pnl", json_object_new_double(p.unrealized_pnl));
        json_object_array_add(arr, x);
    }
    return json_string(arr);
}

std::string QuantEngine::equity_json() const {
    std::lock_guard lock(mu_);
    json_object* arr = json_object_new_array();
    for (const auto& [ts, equity] : equity_curve_) {
        json_object* x = json_object_new_object();
        json_object_object_add(x, "t", json_object_new_int64(ts));
        json_object_object_add(x, "v", json_object_new_double(equity));
        json_object_array_add(arr, x);
    }
    return json_string(arr);
}

} // namespace poly
