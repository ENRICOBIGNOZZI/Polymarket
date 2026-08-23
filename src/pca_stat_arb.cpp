#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/types.hpp"

#include <algorithm>
#include <chrono>
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

struct ResidualFit {
    double phi = 1.0;
    double t_reversion = 0.0;
    double mean = 0.0;
    double sd = 0.0;
    bool ok = false;
};

struct Opportunity {
    const pm::Market* market = nullptr;
    std::string side;
    std::size_t observations = 0;
    double explained = 0.0;
    double residual_z = 0.0;
    double phi = 1.0;
    double half_life_hours = 0.0;
    double t_reversion = 0.0;
    double stability = 0.0;
    double expected_mark_move = 0.0;
    double taker_net_edge = 0.0;
    double maker_entry_net_edge = 0.0;
    double executable_notional = 0.0;
    double maker_limit = 0.0;
    double score = -1e9;
};

double mean(const std::vector<double>& x) {
    return x.empty() ? 0.0 : std::accumulate(x.begin(), x.end(), 0.0) / static_cast<double>(x.size());
}

double sd(const std::vector<double>& x, double m) {
    if (x.size() < 2) return 0.0;
    double s = 0.0;
    for (double v : x) { const double d = v - m; s += d * d; }
    return std::sqrt(s / static_cast<double>(x.size() - 1));
}

double dot(const std::vector<double>& a, const std::vector<double>& b) {
    return std::inner_product(a.begin(), a.end(), b.begin(), 0.0);
}

std::map<std::int64_t,double> bucket_levels(const std::vector<pm::PricePoint>& v, std::int64_t bucket_s) {
    std::map<std::int64_t,double> out;
    for (const auto& p : v) {
        if (p.ts <= 0 || p.price <= 0.0 || p.price >= 1.0) continue;
        out[(p.ts / bucket_s) * bucket_s] = pm::logit(p.price);
    }
    return out;
}

ResidualFit fit_residual(const std::vector<double>& r) {
    ResidualFit f;
    if (r.size() < 24) return f;
    f.mean = mean(r); f.sd = sd(r, f.mean);
    if (f.sd < 1e-5) return f;
    std::vector<double> lag, dr;
    lag.reserve(r.size() - 1); dr.reserve(r.size() - 1);
    for (std::size_t t = 1; t < r.size(); ++t) {
        lag.push_back(r[t - 1]);
        dr.push_back(r[t] - r[t - 1]);
    }
    const double ml = mean(lag), md = mean(dr);
    double sxx = 0.0, sxy = 0.0;
    for (std::size_t i = 0; i < lag.size(); ++i) {
        const double x = lag[i] - ml, y = dr[i] - md;
        sxx += x * x; sxy += x * y;
    }
    if (sxx < 1e-10) return f;
    const double gamma = sxy / sxx;
    const double c = md - gamma * ml;
    double rss = 0.0;
    for (std::size_t i = 0; i < lag.size(); ++i) {
        const double e = dr[i] - (c + gamma * lag[i]);
        rss += e * e;
    }
    const double sigma2 = rss / static_cast<double>(std::max<std::size_t>(1, lag.size() - 2));
    const double se = std::sqrt(std::max(0.0, sigma2) / sxx);
    f.t_reversion = se > 1e-12 ? gamma / se : 0.0;
    f.phi = 1.0 + gamma;
    f.ok = gamma < 0.0 && f.phi > 0.02 && f.phi < 0.999 && std::isfinite(f.t_reversion);
    return f;
}

std::vector<std::vector<double>> top_eigenvectors(std::vector<std::vector<double>> C, std::size_t k,
                                                  std::vector<double>& eigenvalues) {
    const std::size_t n = C.size();
    std::vector<std::vector<double>> vecs;
    if (!n) return vecs;
    k = std::min(k, n);
    for (std::size_t comp = 0; comp < k; ++comp) {
        std::vector<double> v(n, 0.0);
        for (std::size_t i = 0; i < n; ++i) v[i] = 1.0 + static_cast<double>((i + 3 * comp) % 7) / 10.0;
        double norm = std::sqrt(dot(v, v));
        for (double& x : v) x /= std::max(1e-12, norm);
        for (int it = 0; it < 100; ++it) {
            std::vector<double> w(n, 0.0);
            for (std::size_t i = 0; i < n; ++i) for (std::size_t j = 0; j < n; ++j) w[i] += C[i][j] * v[j];
            for (const auto& u : vecs) {
                const double p = dot(w, u);
                for (std::size_t i = 0; i < n; ++i) w[i] -= p * u[i];
            }
            norm = std::sqrt(dot(w, w));
            if (norm < 1e-10) break;
            for (std::size_t i = 0; i < n; ++i) v[i] = w[i] / norm;
        }
        std::vector<double> Cv(n, 0.0);
        for (std::size_t i = 0; i < n; ++i) for (std::size_t j = 0; j < n; ++j) Cv[i] += C[i][j] * v[j];
        const double lambda = dot(v, Cv);
        if (!std::isfinite(lambda) || lambda <= 1e-6) break;
        vecs.push_back(v); eigenvalues.push_back(lambda);
    }
    return vecs;
}

double touch_size(const pm::Book& b, bool ask) {
    const double px = ask ? b.best_ask() : b.best_bid();
    if (!std::isfinite(px)) return 0.0;
    double q = 0.0;
    const auto& levels = ask ? b.asks : b.bids;
    for (const auto& l : levels) if (std::abs(l.price - px) <= 1e-9) q += l.size;
    return q;
}

std::pair<double,double> economics(const pm::Book& b, const pm::FeeDetails& fd, double expected_move,
                                   double slippage_bps, bool maker_entry) {
    const double mid = b.midpoint(), spread = b.spread();
    if (!std::isfinite(mid) || !std::isfinite(spread)) return {-1.0, 0.0};
    const double future_mid = std::clamp(mid + expected_move, 0.001, 0.999);
    const double slip = slippage_bps / 10000.0;
    double entry = 0.0, fee_in = 0.0;
    if (maker_entry) entry = b.best_bid();
    else {
        entry = b.best_ask() * (1.0 + slip);
        fee_in = pm::Engine::protocol_fee(1.0, entry, fd);
    }
    if (!std::isfinite(entry) || entry <= 0.0) return {-1.0, 0.0};
    const double exit = std::clamp(future_mid - 0.5 * spread, 0.001, 0.999) * (1.0 - slip);
    const double fee_out = pm::Engine::protocol_fee(1.0, exit, fd);
    const double pnl = exit - entry - fee_in - fee_out;
    const double capital = entry + fee_in;
    return {pnl / std::max(1e-8, capital), capital};
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config = "config/paper_v3.json", csv_path;
        std::size_t market_limit = 600, universe_limit = 100, lookback_hours = 336, fidelity_minutes = 30, factors = 3, top = 40;
        double min_liquidity = 100.0, min_z = 1.5, min_t = 1.75, max_half_life_hours = 168.0;
        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() { if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a); return std::string(argv[++i]); };
            if (a == "--config") config = next();
            else if (a == "--markets") market_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--universe") universe_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--lookback-hours") lookback_hours = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--fidelity-minutes") fidelity_minutes = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--factors") factors = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--min-liquidity") min_liquidity = std::stod(next());
            else if (a == "--min-z") min_z = std::stod(next());
            else if (a == "--min-t-reversion") min_t = std::stod(next());
            else if (a == "--max-half-life-hours") max_half_life_hours = std::stod(next());
            else if (a == "--top") top = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--csv") csv_path = next();
            else if (a == "--help" || a == "-h") {
                std::cout << "polymarket_pca_stat_arb [--config FILE] [--markets N] [--universe N] [--lookback-hours H] "
                             "[--fidelity-minutes M] [--factors K] [--min-z Z] [--min-t-reversion T] "
                             "[--max-half-life-hours H] [--top N] [--csv FILE]\n";
                return 0;
            } else throw std::runtime_error("Unknown argument: " + a);
        }

        auto cfg = pm::Engine::load_config(config);
        pm::PolymarketApi api(cfg);
        auto markets = api.discover_markets(market_limit, min_liquidity);
        std::vector<std::string> all_tokens;
        for (const auto& m : markets) { all_tokens.push_back(m.yes_token); all_tokens.push_back(m.no_token); }
        auto books = api.fetch_books(all_tokens);

        std::vector<const pm::Market*> universe;
        for (const auto& m : markets) {
            auto y = books.find(m.yes_token), n = books.find(m.no_token);
            if (y == books.end() || n == books.end()) continue;
            const double p = y->second.midpoint();
            if (!std::isfinite(p) || p <= cfg.min_mid || p >= cfg.max_mid) continue;
            if (y->second.spread() > cfg.max_spread || n->second.spread() > cfg.max_spread) continue;
            universe.push_back(&m);
        }
        std::sort(universe.begin(), universe.end(), [](auto* a, auto* b){ return a->liquidity > b->liquidity; });
        if (universe.size() > universe_limit) universe.resize(universe_limit);

        std::vector<std::string> yes_tokens;
        for (auto* m : universe) yes_tokens.push_back(m->yes_token);
        const auto now = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        const auto start = now - static_cast<std::int64_t>(lookback_hours) * 3600;
        auto hist = api.fetch_price_history(yes_tokens, start, now, fidelity_minutes);
        const std::int64_t bucket_s = static_cast<std::int64_t>(std::max<std::size_t>(1, fidelity_minutes)) * 60;

        struct Series { const pm::Market* m; std::map<std::int64_t,double> level; double mu; double sigma; };
        std::vector<Series> s;
        for (auto* m : universe) {
            auto it = hist.find(m->yes_token); if (it == hist.end()) continue;
            auto lv = bucket_levels(it->second, bucket_s);
            if (lv.size() < 36) continue;
            std::vector<double> vals; vals.reserve(lv.size()); for (const auto& kv : lv) vals.push_back(kv.second);
            const double mu = mean(vals), sigma = sd(vals, mu);
            if (sigma < 1e-4) continue;
            s.push_back({m, std::move(lv), mu, sigma});
        }
        const std::size_t N = s.size();
        if (N < 4) {
            std::cout << "pca_stat_arb insufficient_series=" << N << '\n';
            return 0;
        }

        std::vector<std::vector<double>> C(N, std::vector<double>(N, 0.0));
        for (std::size_t i = 0; i < N; ++i) C[i][i] = 1.0;
        for (std::size_t i = 0; i < N; ++i) {
            for (std::size_t j = i + 1; j < N; ++j) {
                auto a = s[i].level.begin(), b = s[j].level.begin();
                double sum = 0.0; std::size_t n = 0;
                while (a != s[i].level.end() && b != s[j].level.end()) {
                    if (a->first == b->first) {
                        sum += ((a->second - s[i].mu) / s[i].sigma) * ((b->second - s[j].mu) / s[j].sigma);
                        ++n; ++a; ++b;
                    } else if (a->first < b->first) ++a; else ++b;
                }
                if (n >= 24) C[i][j] = C[j][i] = std::clamp(sum / static_cast<double>(n), -1.0, 1.0);
            }
        }

        std::vector<double> eigenvalues;
        auto vecs = top_eigenvectors(C, factors, eigenvalues);
        if (vecs.empty()) throw std::runtime_error("PCA factor extraction failed");
        double trace = static_cast<double>(N), evsum = std::accumulate(eigenvalues.begin(), eigenvalues.end(), 0.0);
        const double explained = std::clamp(evsum / std::max(1e-8, trace), 0.0, 1.0);

        std::set<std::int64_t> timestamps;
        for (const auto& z : s) for (const auto& kv : z.level) timestamps.insert(kv.first);
        std::vector<std::vector<double>> residuals(N);
        for (auto ts : timestamps) {
            std::vector<double> y(N, 0.0); std::vector<bool> obs(N, false);
            std::size_t count = 0;
            for (std::size_t j = 0; j < N; ++j) {
                auto it = s[j].level.find(ts);
                if (it == s[j].level.end()) continue;
                y[j] = (it->second - s[j].mu) / s[j].sigma; obs[j] = true; ++count;
            }
            if (count < std::max<std::size_t>(4, N / 4)) continue;
            std::vector<double> factor(vecs.size(), 0.0);
            for (std::size_t k = 0; k < vecs.size(); ++k) {
                double num = 0.0, den = 0.0;
                for (std::size_t j = 0; j < N; ++j) if (obs[j]) { num += vecs[k][j] * y[j]; den += vecs[k][j] * vecs[k][j]; }
                if (den > 1e-10) factor[k] = num / den;
            }
            for (std::size_t j = 0; j < N; ++j) if (obs[j]) {
                double fitted = 0.0;
                for (std::size_t k = 0; k < vecs.size(); ++k) fitted += vecs[k][j] * factor[k];
                residuals[j].push_back(y[j] - fitted);
            }
        }

        std::vector<double> current_y(N, 0.0);
        std::vector<bool> current_obs(N, false);
        for (std::size_t j = 0; j < N; ++j) {
            const double p = books.at(s[j].m->yes_token).midpoint();
            if (!std::isfinite(p)) continue;
            current_y[j] = (pm::logit(p) - s[j].mu) / s[j].sigma;
            current_obs[j] = true;
        }
        std::vector<double> current_factor(vecs.size(), 0.0);
        for (std::size_t k = 0; k < vecs.size(); ++k) {
            double num = 0.0, den = 0.0;
            for (std::size_t j = 0; j < N; ++j) if (current_obs[j]) { num += vecs[k][j] * current_y[j]; den += vecs[k][j] * vecs[k][j]; }
            if (den > 1e-10) current_factor[k] = num / den;
        }

        std::vector<Opportunity> opps;
        for (std::size_t j = 0; j < N; ++j) {
            auto f = fit_residual(residuals[j]);
            if (!f.ok || f.t_reversion > -min_t) continue;
            const std::size_t cut = residuals[j].size() / 2;
            if (cut < 24 || residuals[j].size() - cut < 24) continue;
            std::vector<double> r1(residuals[j].begin(), residuals[j].begin() + static_cast<std::ptrdiff_t>(cut));
            std::vector<double> r2(residuals[j].begin() + static_cast<std::ptrdiff_t>(cut), residuals[j].end());
            auto f1 = fit_residual(r1), f2 = fit_residual(r2);
            if (!f1.ok || !f2.ok) continue;
            const double stability = std::exp(-4.0 * std::abs(f1.phi - f2.phi)) *
                                     ((f1.t_reversion < 0.0 && f2.t_reversion < 0.0) ? 1.0 : 0.0);
            if (stability < 0.45) continue;
            const double bucket_hours = static_cast<double>(fidelity_minutes) / 60.0;
            const double half_life = -std::log(2.0) / std::log(f.phi) * bucket_hours;
            if (!std::isfinite(half_life) || half_life <= 0.0 || half_life > max_half_life_hours) continue;

            double fitted_now = 0.0;
            for (std::size_t k = 0; k < vecs.size(); ++k) fitted_now += vecs[k][j] * current_factor[k];
            const double residual_now = current_y[j] - fitted_now;
            const double z = (residual_now - f.mean) / f.sd;
            if (std::abs(z) < min_z) continue;

            const double convergence = 0.5; // one estimated half-life
            const double delta_standardized = -(residual_now - f.mean) * convergence;
            const double delta_logit = std::clamp(delta_standardized * s[j].sigma, -0.75, 0.75);
            const double p = books.at(s[j].m->yes_token).midpoint();
            const double p_new = pm::logistic(pm::logit(p) + delta_logit);
            const double dp = p_new - p;
            const std::string side = dp >= 0.0 ? "YES" : "NO";
            const auto& b = side == "YES" ? books.at(s[j].m->yes_token) : books.at(s[j].m->no_token);
            const double expected_move = std::abs(dp);
            const auto fd = api.fetch_fee_details(*s[j].m);
            auto taker = economics(b, fd, expected_move, cfg.slippage_bps, false);
            auto maker = economics(b, fd, expected_move, cfg.slippage_bps, true);
            const double cap = touch_size(b, true) * std::max(0.0, b.best_ask());
            const double score = maker.first * std::abs(z) * stability / std::sqrt(std::max(1.0, half_life));
            opps.push_back({s[j].m,side,residuals[j].size(),explained,z,f.phi,half_life,f.t_reversion,stability,
                            expected_move,taker.first,maker.first,cap,b.best_bid(),score});
        }

        std::sort(opps.begin(), opps.end(), [](const auto& a, const auto& b){ return a.score > b.score; });
        std::size_t taker_pos = 0, maker_pos = 0;
        for (const auto& o : opps) { if (o.taker_net_edge > 0.0) ++taker_pos; if (o.maker_entry_net_edge > 0.0) ++maker_pos; }
        std::cout << "pca_stat_arb discovered=" << markets.size() << " panel_series=" << N << " factors=" << vecs.size()
                  << " explained=" << explained << " opportunities=" << opps.size() << " taker_positive=" << taker_pos
                  << " maker_entry_positive=" << maker_pos << '\n';
        std::cout << "market,side,obs,residual_z,phi,half_life_h,t_reversion,stability,expected_mark_move,taker_net_edge,"
                     "maker_entry_net_edge,executable_notional,maker_limit\n";
        for (std::size_t i = 0; i < std::min(top, opps.size()); ++i) {
            const auto& o = opps[i];
            std::cout << o.market->id << ',' << o.side << ',' << o.observations << ',' << o.residual_z << ',' << o.phi << ','
                      << o.half_life_hours << ',' << o.t_reversion << ',' << o.stability << ',' << o.expected_mark_move << ','
                      << o.taker_net_edge << ',' << o.maker_entry_net_edge << ',' << o.executable_notional << ',' << o.maker_limit << '\n';
        }
        if (!csv_path.empty()) {
            std::ofstream out(csv_path);
            out << "market,slug,side,obs,explained,residual_z,phi,half_life_h,t_reversion,stability,expected_mark_move,"
                   "taker_net_edge,maker_entry_net_edge,executable_notional,maker_limit\n";
            for (const auto& o : opps) {
                out << o.market->id << ',' << o.market->slug << ',' << o.side << ',' << o.observations << ',' << o.explained << ','
                    << o.residual_z << ',' << o.phi << ',' << o.half_life_hours << ',' << o.t_reversion << ',' << o.stability << ','
                    << o.expected_mark_move << ',' << o.taker_net_edge << ',' << o.maker_entry_net_edge << ','
                    << o.executable_notional << ',' << o.maker_limit << '\n';
            }
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
