#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/types.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

struct Fit {
    double alpha = 0.0;
    double beta = 0.0;
    double resid_mean = 0.0;
    double resid_sd = 0.0;
    double phi = 1.0;
    double t_reversion = 0.0;
    double r2 = 0.0;
    bool ok = false;
};

struct Opportunity {
    const pm::Market* y = nullptr;
    const pm::Market* x = nullptr;
    std::size_t window = 0;
    std::size_t aligned_points = 0;
    std::string relation;
    double semantic_similarity = 0.0;
    double return_corr = 0.0;
    double beta = 0.0;
    double phi = 1.0;
    double half_life_hours = 0.0;
    double t_reversion = 0.0;
    double stability = 0.0;
    double z = 0.0;
    double raw_expected_edge = 0.0;
    double taker_net_edge = 0.0;
    double maker_entry_net_edge = 0.0;
    double executable_notional = 0.0;
    std::string y_side;
    std::string x_side;
    double y_limit = 0.0;
    double x_limit = 0.0;
    double y_weight = 1.0;
    double x_weight = 1.0;
    double score = -1e9;
};

std::vector<std::string> words(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (unsigned char c : s) {
        if (std::isalnum(c)) cur.push_back(static_cast<char>(std::tolower(c)));
        else {
            if (cur.size() >= 3) out.push_back(cur);
            cur.clear();
        }
    }
    if (cur.size() >= 3) out.push_back(cur);
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}

double jaccard(const std::string& a, const std::string& b) {
    auto x = words(a), y = words(b);
    std::size_t i = 0, j = 0, inter = 0, uni = 0;
    while (i < x.size() || j < y.size()) {
        if (i == x.size()) { ++uni; ++j; }
        else if (j == y.size()) { ++uni; ++i; }
        else if (x[i] == y[j]) { ++inter; ++uni; ++i; ++j; }
        else if (x[i] < y[j]) { ++uni; ++i; }
        else { ++uni; ++j; }
    }
    return uni ? static_cast<double>(inter) / static_cast<double>(uni) : 0.0;
}

double mean(const std::vector<double>& x) {
    return x.empty() ? 0.0 : std::accumulate(x.begin(), x.end(), 0.0) / static_cast<double>(x.size());
}

double sd(const std::vector<double>& x, double m) {
    if (x.size() < 2) return 0.0;
    double s = 0.0;
    for (double v : x) { const double d = v - m; s += d * d; }
    return std::sqrt(s / static_cast<double>(x.size() - 1));
}

double corr(const std::vector<double>& a, const std::vector<double>& b) {
    if (a.size() != b.size() || a.size() < 3) return 0.0;
    const double ma = mean(a), mb = mean(b);
    double sxx = 0.0, syy = 0.0, sxy = 0.0;
    for (std::size_t i = 0; i < a.size(); ++i) {
        const double x = a[i] - ma, y = b[i] - mb;
        sxx += x * x; syy += y * y; sxy += x * y;
    }
    return sxx > 1e-12 && syy > 1e-12 ? sxy / std::sqrt(sxx * syy) : 0.0;
}

Fit fit_pair(const std::vector<double>& x, const std::vector<double>& y) {
    Fit f;
    if (x.size() != y.size() || x.size() < 24) return f;
    const double mx = mean(x), my = mean(y);
    double sxx = 0.0, sxy = 0.0, syy = 0.0;
    for (std::size_t i = 0; i < x.size(); ++i) {
        const double dx = x[i] - mx, dy = y[i] - my;
        sxx += dx * dx; sxy += dx * dy; syy += dy * dy;
    }
    if (sxx < 1e-8 || syy < 1e-8) return f;
    f.beta = sxy / sxx;
    f.alpha = my - f.beta * mx;
    if (std::abs(f.beta) < 0.10 || std::abs(f.beta) > 8.0) return f;

    std::vector<double> r(x.size());
    double rss_level = 0.0;
    for (std::size_t i = 0; i < x.size(); ++i) {
        r[i] = y[i] - f.alpha - f.beta * x[i];
        rss_level += r[i] * r[i];
    }
    f.resid_mean = mean(r);
    f.resid_sd = sd(r, f.resid_mean);
    if (f.resid_sd < 1e-4) return f;
    f.r2 = std::clamp(1.0 - rss_level / syy, -1.0, 1.0);

    // ADF-style one-lag mean-reversion screen: Δr_t = c + gamma r_{t-1} + eps_t.
    std::vector<double> lag, dr;
    lag.reserve(r.size() - 1); dr.reserve(r.size() - 1);
    for (std::size_t t = 1; t < r.size(); ++t) {
        lag.push_back(r[t - 1]);
        dr.push_back(r[t] - r[t - 1]);
    }
    const double ml = mean(lag), md = mean(dr);
    double lxx = 0.0, lxy = 0.0;
    for (std::size_t i = 0; i < lag.size(); ++i) {
        const double a = lag[i] - ml, b = dr[i] - md;
        lxx += a * a; lxy += a * b;
    }
    if (lxx < 1e-10) return f;
    const double gamma = lxy / lxx;
    const double c = md - gamma * ml;
    double rss = 0.0;
    for (std::size_t i = 0; i < lag.size(); ++i) {
        const double e = dr[i] - (c + gamma * lag[i]);
        rss += e * e;
    }
    const double sigma2 = rss / static_cast<double>(std::max<std::size_t>(1, lag.size() - 2));
    const double se = std::sqrt(std::max(0.0, sigma2) / lxx);
    f.t_reversion = se > 1e-12 ? gamma / se : 0.0;
    f.phi = 1.0 + gamma;
    f.ok = gamma < 0.0 && f.phi > 0.02 && f.phi < 0.999 && std::isfinite(f.t_reversion);
    return f;
}

std::map<std::int64_t,double> bucketize(const std::vector<pm::PricePoint>& v, std::int64_t bucket_s) {
    std::map<std::int64_t,double> out;
    for (const auto& p : v) {
        if (p.ts <= 0 || p.price <= 0.0 || p.price >= 1.0) continue;
        const auto b = (p.ts / bucket_s) * bucket_s;
        out[b] = p.price;
    }
    return out;
}

std::pair<std::vector<double>,std::vector<double>> align_levels(
    const std::map<std::int64_t,double>& a,
    const std::map<std::int64_t,double>& b) {
    std::vector<double> x, y;
    auto ia = a.begin(), ib = b.begin();
    while (ia != a.end() && ib != b.end()) {
        if (ia->first == ib->first) {
            x.push_back(pm::logit(ia->second));
            y.push_back(pm::logit(ib->second));
            ++ia; ++ib;
        } else if (ia->first < ib->first) ++ia;
        else ++ib;
    }
    return {std::move(x), std::move(y)};
}

std::vector<double> changes(const std::vector<double>& x) {
    std::vector<double> out;
    if (x.size() < 2) return out;
    out.reserve(x.size() - 1);
    for (std::size_t i = 1; i < x.size(); ++i) out.push_back(std::clamp(x[i] - x[i - 1], -2.0, 2.0));
    return out;
}

double touch_size(const pm::Book& b, bool ask) {
    const double px = ask ? b.best_ask() : b.best_bid();
    if (!std::isfinite(px)) return 0.0;
    double q = 0.0;
    const auto& levels = ask ? b.asks : b.bids;
    for (const auto& l : levels) if (std::abs(l.price - px) <= 1e-9) q += l.size;
    return q;
}

struct LegEval {
    std::string side;
    const pm::Book* book = nullptr;
    double expected_move = 0.0;
    double weight = 1.0;
};

std::optional<std::pair<double,double>> evaluate_taker_leg(
    const LegEval& l, const pm::FeeDetails& fd, double slippage_bps) {
    if (!l.book) return std::nullopt;
    const double ask = l.book->best_ask();
    const double mid = l.book->midpoint();
    const double spr = l.book->spread();
    if (!std::isfinite(ask) || !std::isfinite(mid) || !std::isfinite(spr)) return std::nullopt;
    const double slip = slippage_bps / 10000.0;
    const double entry = ask * (1.0 + slip);
    const double future_mid = std::clamp(mid + l.expected_move, 0.001, 0.999);
    const double future_bid = std::clamp(future_mid - 0.5 * spr, 0.001, 0.999) * (1.0 - slip);
    const double fee_in = pm::Engine::protocol_fee(l.weight, entry, fd);
    const double fee_out = pm::Engine::protocol_fee(l.weight, future_bid, fd);
    const double pnl = l.weight * (future_bid - entry) - fee_in - fee_out;
    const double capital = l.weight * entry + fee_in;
    return std::pair{pnl, capital};
}

std::optional<std::pair<double,double>> evaluate_maker_entry_leg(
    const LegEval& l, const pm::FeeDetails& fd, double slippage_bps) {
    if (!l.book) return std::nullopt;
    const double bid = l.book->best_bid();
    const double mid = l.book->midpoint();
    const double spr = l.book->spread();
    if (!std::isfinite(bid) || !std::isfinite(mid) || !std::isfinite(spr)) return std::nullopt;
    const double future_mid = std::clamp(mid + l.expected_move, 0.001, 0.999);
    const double future_bid = std::clamp(future_mid - 0.5 * spr, 0.001, 0.999) * (1.0 - slippage_bps / 10000.0);
    // Entry is passive and therefore assigned zero fee. Exit is conservatively taker.
    const double fee_out = pm::Engine::protocol_fee(l.weight, future_bid, fd);
    const double pnl = l.weight * (future_bid - bid) - fee_out;
    const double capital = l.weight * bid;
    return std::pair{pnl, capital};
}

std::string relation_name(const pm::Market& a, const pm::Market& b, double sim, double rc) {
    if (!a.event_id.empty() && a.event_id == b.event_id) return "same_event";
    if (sim >= 0.15) return "semantic";
    if (std::abs(rc) >= 0.75) return "latent_corr";
    return "weak";
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config = "config/paper.example.json";
        std::size_t market_limit = 600, history_universe = 160, top = 40;
        std::size_t lookback_hours = 336, fidelity_minutes = 30;
        double min_liquidity = 100.0, min_z = 1.50, max_half_life_hours = 168.0;
        double min_t_reversion = 1.75;
        std::string csv_path;

        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() { if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a); return std::string(argv[++i]); };
            if (a == "--config") config = next();
            else if (a == "--markets") market_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--history-universe") history_universe = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--lookback-hours") lookback_hours = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--fidelity-minutes") fidelity_minutes = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--min-liquidity") min_liquidity = std::stod(next());
            else if (a == "--min-z") min_z = std::stod(next());
            else if (a == "--max-half-life-hours") max_half_life_hours = std::stod(next());
            else if (a == "--min-t-reversion") min_t_reversion = std::stod(next());
            else if (a == "--top") top = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--csv") csv_path = next();
            else if (a == "--help" || a == "-h") {
                std::cout << "polymarket_stat_arb [--config FILE] [--markets N] [--history-universe N] "
                             "[--lookback-hours H] [--fidelity-minutes M] [--min-z Z] "
                             "[--max-half-life-hours H] [--min-t-reversion T] [--top N] [--csv FILE]\n";
                return 0;
            } else throw std::runtime_error("Unknown argument: " + a);
        }

        auto cfg = pm::Engine::load_config(config);
        cfg.market_limit = market_limit;
        cfg.min_liquidity = min_liquidity;
        pm::PolymarketApi api(cfg);
        auto markets = api.discover_markets(market_limit, min_liquidity);

        std::vector<std::string> tokens;
        tokens.reserve(markets.size() * 2);
        for (const auto& m : markets) { tokens.push_back(m.yes_token); tokens.push_back(m.no_token); }
        auto books = api.fetch_books(tokens);

        std::vector<const pm::Market*> universe;
        universe.reserve(markets.size());
        for (const auto& m : markets) {
            auto y = books.find(m.yes_token), n = books.find(m.no_token);
            if (y == books.end() || n == books.end()) continue;
            const double p = y->second.midpoint();
            if (!std::isfinite(p) || p <= cfg.min_mid || p >= cfg.max_mid) continue;
            if (y->second.spread() > cfg.max_spread || n->second.spread() > cfg.max_spread) continue;
            if (!std::isfinite(y->second.best_ask()) || !std::isfinite(n->second.best_ask())) continue;
            universe.push_back(&m);
        }
        std::sort(universe.begin(), universe.end(), [](auto* a, auto* b) { return a->liquidity > b->liquidity; });
        if (universe.size() > history_universe) universe.resize(history_universe);

        std::vector<std::string> yes_tokens;
        yes_tokens.reserve(universe.size());
        for (auto* m : universe) yes_tokens.push_back(m->yes_token);
        const std::int64_t now = static_cast<std::int64_t>(std::time(nullptr));
        const std::int64_t start = now - static_cast<std::int64_t>(lookback_hours) * 3600;
        auto hist = api.fetch_price_history(yes_tokens, start, now, fidelity_minutes);
        const std::int64_t bucket_s = static_cast<std::int64_t>(std::max<std::size_t>(1, fidelity_minutes)) * 60;

        std::unordered_map<std::string,std::map<std::int64_t,double>> series;
        for (auto* m : universe) {
            auto it = hist.find(m->yes_token);
            if (it == hist.end()) continue;
            auto b = bucketize(it->second, bucket_s);
            const double cur = books.at(m->yes_token).midpoint();
            if (std::isfinite(cur)) b[(now / bucket_s) * bucket_s] = cur;
            if (b.size() >= 25) series[m->id] = std::move(b);
        }

        const std::vector<std::size_t> windows{48, 96, 192, 336, 672};
        std::vector<Opportunity> opportunities;
        std::size_t pairs_tested = 0, model_fits = 0, mr_pass = 0;

        for (std::size_t i = 0; i < universe.size(); ++i) {
            auto si = series.find(universe[i]->id);
            if (si == series.end()) continue;
            for (std::size_t j = i + 1; j < universe.size(); ++j) {
                auto sj = series.find(universe[j]->id);
                if (sj == series.end()) continue;
                ++pairs_tested;

                auto [lx_full, ly_full] = align_levels(si->second, sj->second);
                if (lx_full.size() < 30) continue;
                auto rx_full = changes(lx_full), ry_full = changes(ly_full);
                const double rc_full = corr(rx_full, ry_full);
                const double sim = jaccard(universe[i]->question, universe[j]->question);
                const bool same_event = !universe[i]->event_id.empty() && universe[i]->event_id == universe[j]->event_id;
                const double corr_gate = same_event ? 0.20 : (sim >= 0.15 ? 0.35 : 0.75);
                if (std::abs(rc_full) < corr_gate) continue;

                Opportunity best;
                bool have = false;
                for (std::size_t w0 : windows) {
                    if (lx_full.size() < std::min<std::size_t>(w0, 36)) continue;
                    const std::size_t w = std::min(w0, lx_full.size());
                    if (w < 30) continue;
                    std::vector<double> x(lx_full.end() - static_cast<std::ptrdiff_t>(w), lx_full.end());
                    std::vector<double> y(ly_full.end() - static_cast<std::ptrdiff_t>(w), ly_full.end());
                    auto f = fit_pair(x, y);
                    if (!f.ok) continue;
                    ++model_fits;

                    const std::size_t cut = w / 2;
                    std::vector<double> x1(x.begin(), x.begin() + static_cast<std::ptrdiff_t>(cut));
                    std::vector<double> y1(y.begin(), y.begin() + static_cast<std::ptrdiff_t>(cut));
                    std::vector<double> x2(x.begin() + static_cast<std::ptrdiff_t>(cut), x.end());
                    std::vector<double> y2(y.begin() + static_cast<std::ptrdiff_t>(cut), y.end());
                    auto f1 = fit_pair(x1, y1), f2 = fit_pair(x2, y2);
                    if (!f1.ok || !f2.ok) continue;
                    const double beta_stability = std::exp(-std::abs(std::log(std::max(1e-6, std::abs(f1.beta)) /
                                                                             std::max(1e-6, std::abs(f2.beta)))));
                    const double phi_stability = std::exp(-4.0 * std::abs(f1.phi - f2.phi));
                    const double sign_stability = f1.beta * f2.beta > 0.0 ? 1.0 : 0.0;
                    const double stability = beta_stability * phi_stability * sign_stability;
                    if (stability < 0.45) continue;

                    const double t_gate = (same_event || sim >= 0.15) ? min_t_reversion : std::max(2.25, min_t_reversion);
                    if (f.t_reversion > -t_gate) continue;
                    const double bucket_hours = static_cast<double>(fidelity_minutes) / 60.0;
                    const double half_life = -std::log(2.0) / std::log(f.phi) * bucket_hours;
                    if (!std::isfinite(half_life) || half_life <= 0.0 || half_life > max_half_life_hours) continue;
                    ++mr_pass;

                    const double px = books.at(universe[i]->yes_token).midpoint();
                    const double py = books.at(universe[j]->yes_token).midpoint();
                    const double residual_now = pm::logit(py) - f.alpha - f.beta * pm::logit(px) - f.resid_mean;
                    const double z = residual_now / f.resid_sd;
                    if (std::abs(z) < min_z) continue;

                    const double horizon_steps = std::max(1.0, half_life / bucket_hours);
                    const double convergence = 1.0 - std::pow(f.phi, horizon_steps);
                    const double delta_r = -residual_now * convergence;
                    const double denom = 1.0 + f.beta * f.beta;
                    const double dy_logit = delta_r / denom;
                    const double dx_logit = -f.beta * delta_r / denom;
                    const double py_new = pm::logistic(pm::logit(py) + dy_logit);
                    const double px_new = pm::logistic(pm::logit(px) + dx_logit);
                    const double dpy = py_new - py, dpx = px_new - px;

                    const double sens_y = std::max(0.02, py * (1.0 - py));
                    const double sens_x = std::max(0.02, px * (1.0 - px));
                    const double qy = 1.0;
                    const double qx = std::clamp(std::abs(f.beta) * sens_y / sens_x, 0.25, 4.0);

                    const pm::Book& by = dpy >= 0.0 ? books.at(universe[j]->yes_token) : books.at(universe[j]->no_token);
                    const pm::Book& bx = dpx >= 0.0 ? books.at(universe[i]->yes_token) : books.at(universe[i]->no_token);
                    const std::string side_y = dpy >= 0.0 ? "YES" : "NO";
                    const std::string side_x = dpx >= 0.0 ? "YES" : "NO";
                    LegEval ly{side_y, &by, std::abs(dpy), qy};
                    LegEval lx{side_x, &bx, std::abs(dpx), qx};

                    const pm::FeeDetails fdy = api.fetch_fee_details(*universe[j]);
                    const pm::FeeDetails fdx = api.fetch_fee_details(*universe[i]);
                    auto ty = evaluate_taker_leg(ly, fdy, cfg.slippage_bps);
                    auto tx = evaluate_taker_leg(lx, fdx, cfg.slippage_bps);
                    auto my = evaluate_maker_entry_leg(ly, fdy, cfg.slippage_bps);
                    auto mx = evaluate_maker_entry_leg(lx, fdx, cfg.slippage_bps);
                    if (!ty || !tx || !my || !mx) continue;

                    const double raw_pnl = qy * std::abs(dpy) + qx * std::abs(dpx);
                    const double taker_pnl = ty->first + tx->first;
                    const double taker_cap = ty->second + tx->second;
                    const double maker_pnl = my->first + mx->first;
                    const double maker_cap = my->second + mx->second;
                    if (taker_cap <= 1e-8 || maker_cap <= 1e-8) continue;

                    const double cap_y = touch_size(by, true) / qy;
                    const double cap_x = touch_size(bx, true) / qx;
                    const double units = std::max(0.0, std::min(cap_y, cap_x));
                    const double executable = units * taker_cap;
                    const double raw_edge = raw_pnl / std::max(1e-8, taker_cap);
                    const double taker_edge = taker_pnl / taker_cap;
                    const double maker_edge = maker_pnl / maker_cap;
                    const double relation_penalty = same_event ? 1.0 : (sim >= 0.15 ? 0.90 : 0.70);
                    const double score = maker_edge * std::abs(z) * stability * relation_penalty /
                                         std::sqrt(std::max(1.0, half_life));

                    Opportunity o;
                    o.y = universe[j]; o.x = universe[i]; o.window = w; o.aligned_points = lx_full.size();
                    o.relation = relation_name(*universe[i], *universe[j], sim, rc_full);
                    o.semantic_similarity = sim; o.return_corr = rc_full; o.beta = f.beta; o.phi = f.phi;
                    o.half_life_hours = half_life; o.t_reversion = f.t_reversion; o.stability = stability; o.z = z;
                    o.raw_expected_edge = raw_edge; o.taker_net_edge = taker_edge; o.maker_entry_net_edge = maker_edge;
                    o.executable_notional = executable; o.y_side = side_y; o.x_side = side_x;
                    o.y_limit = by.best_bid(); o.x_limit = bx.best_bid(); o.y_weight = qy; o.x_weight = qx; o.score = score;
                    if (!have || o.score > best.score) { best = std::move(o); have = true; }
                }
                if (have) opportunities.push_back(std::move(best));
            }
        }

        std::sort(opportunities.begin(), opportunities.end(), [](const auto& a, const auto& b) { return a.score > b.score; });
        std::size_t raw_positive = 0, taker_positive = 0, maker_positive = 0;
        for (const auto& o : opportunities) {
            if (o.raw_expected_edge > 0.0) ++raw_positive;
            if (o.taker_net_edge > 0.0) ++taker_positive;
            if (o.maker_entry_net_edge > 0.0) ++maker_positive;
        }

        std::cout << "discovered=" << markets.size() << " history_universe=" << universe.size()
                  << " usable_series=" << series.size() << " pairs_tested=" << pairs_tested
                  << " model_fits=" << model_fits << " mr_pass=" << mr_pass
                  << " opportunities=" << opportunities.size() << " raw_positive=" << raw_positive
                  << " taker_positive=" << taker_positive << " maker_entry_positive=" << maker_positive << '\n';
        std::cout << "relation,y_market,x_market,window,aligned,ret_corr,beta,phi,half_life_h,t_reversion,stability,z,"
                     "raw_expected_edge,taker_net_edge,maker_entry_net_edge,executable_notional,y_side,x_side,y_limit,x_limit\n";
        const std::size_t show = std::min(top, opportunities.size());
        for (std::size_t k = 0; k < show; ++k) {
            const auto& o = opportunities[k];
            std::cout << o.relation << ',' << o.y->id << ',' << o.x->id << ',' << o.window << ',' << o.aligned_points << ','
                      << o.return_corr << ',' << o.beta << ',' << o.phi << ',' << o.half_life_hours << ',' << o.t_reversion << ','
                      << o.stability << ',' << o.z << ',' << o.raw_expected_edge << ',' << o.taker_net_edge << ','
                      << o.maker_entry_net_edge << ',' << o.executable_notional << ',' << o.y_side << ',' << o.x_side << ','
                      << o.y_limit << ',' << o.x_limit << '\n';
        }

        if (!csv_path.empty()) {
            std::ofstream out(csv_path);
            if (!out) throw std::runtime_error("Cannot open output CSV: " + csv_path);
            out << "relation,y_market,y_slug,x_market,x_slug,window,aligned,ret_corr,beta,phi,half_life_h,t_reversion,stability,z,"
                   "raw_expected_edge,taker_net_edge,maker_entry_net_edge,executable_notional,y_side,x_side,y_limit,x_limit,y_weight,x_weight\n";
            for (const auto& o : opportunities) {
                out << o.relation << ',' << o.y->id << ',' << o.y->slug << ',' << o.x->id << ',' << o.x->slug << ','
                    << o.window << ',' << o.aligned_points << ',' << o.return_corr << ',' << o.beta << ',' << o.phi << ','
                    << o.half_life_hours << ',' << o.t_reversion << ',' << o.stability << ',' << o.z << ','
                    << o.raw_expected_edge << ',' << o.taker_net_edge << ',' << o.maker_entry_net_edge << ','
                    << o.executable_notional << ',' << o.y_side << ',' << o.x_side << ',' << o.y_limit << ',' << o.x_limit << ','
                    << o.y_weight << ',' << o.x_weight << '\n';
            }
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
