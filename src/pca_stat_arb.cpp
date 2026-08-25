#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/market_relation.hpp"
#include "pm/types.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

struct TimedValue {
    std::int64_t ts = 0;
    double value = 0.0;
};

struct ResidualFit {
    double phi = 1.0;
    double t_reversion = 0.0;
    double mean = 0.0;
    double sd = 0.0;
    std::size_t transitions = 0;
    bool ok = false;
};

struct Leg {
    const pm::Market* market = nullptr;
    const pm::Book* book = nullptr;
    pm::FeeDetails fee;
    std::string side;
    double shares = 0.0;
    bool target = false;
};

struct PortfolioEconomics {
    double raw_edge = -1.0;
    double net_edge = -1.0;
    double capital = 0.0;
    double executable_notional = 0.0;
    bool ok = false;
};

struct Opportunity {
    const pm::Market* market = nullptr;
    std::string side;
    std::size_t observations = 0;
    std::size_t hedge_legs = 0;
    double explained = 0.0;
    double residual_z = 0.0;
    double phi = 1.0;
    double half_life_hours = 0.0;
    double t_reversion = 0.0;
    double stability = 0.0;
    double factor_hedge_error = 1.0;
    double expected_mark_move = 0.0;
    double raw_expected_edge = 0.0;
    double taker_net_edge = 0.0;
    double maker_entry_net_edge = 0.0;
    double executable_notional = 0.0;
    std::string legs;
    double score = -1e9;
};

double mean(const std::vector<double>& x) {
    return x.empty() ? 0.0 : std::accumulate(x.begin(), x.end(), 0.0) / static_cast<double>(x.size());
}

double sd(const std::vector<double>& x, double m) {
    if (x.size() < 2) return 0.0;
    double s = 0.0;
    for (double v : x) {
        const double d = v - m;
        s += d * d;
    }
    return std::sqrt(s / static_cast<double>(x.size() - 1));
}

double dot(const std::vector<double>& a, const std::vector<double>& b) {
    return std::inner_product(a.begin(), a.end(), b.begin(), 0.0);
}

double norm2(const std::vector<double>& x) {
    return std::sqrt(std::max(0.0, dot(x, x)));
}

std::map<std::int64_t,double> bucket_levels(const std::vector<pm::PricePoint>& v, std::int64_t bucket_s) {
    std::map<std::int64_t,double> out;
    for (const auto& p : v) {
        if (p.ts <= 0 || p.price <= 0.0 || p.price >= 1.0) continue;
        out[(p.ts / bucket_s) * bucket_s] = pm::logit(p.price);
    }
    return out;
}

std::optional<double> asof_level(const std::map<std::int64_t,double>& values,
                                 std::int64_t ts, std::int64_t max_age) {
    auto it = values.upper_bound(ts);
    if (it == values.begin()) return std::nullopt;
    --it;
    if (ts - it->first > max_age) return std::nullopt;
    return it->second;
}

ResidualFit fit_residual(const std::vector<TimedValue>& r, std::int64_t bucket_s) {
    ResidualFit f;
    if (r.size() < 16) return f;
    std::vector<double> values;
    values.reserve(r.size());
    for (const auto& x : r) values.push_back(x.value);
    f.mean = mean(values);
    f.sd = sd(values, f.mean);
    if (f.sd < 1e-5) return f;

    std::vector<double> lag, dr;
    for (std::size_t t = 1; t < r.size(); ++t) {
        if (r[t].ts - r[t - 1].ts != bucket_s) continue;
        lag.push_back(r[t - 1].value);
        dr.push_back(r[t].value - r[t - 1].value);
    }
    f.transitions = lag.size();
    if (lag.size() < 12) return f;
    const double ml = mean(lag), md = mean(dr);
    double sxx = 0.0, sxy = 0.0;
    for (std::size_t i = 0; i < lag.size(); ++i) {
        const double x = lag[i] - ml;
        const double y = dr[i] - md;
        sxx += x * x;
        sxy += x * y;
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
    f.ok = gamma < 0.0 && f.phi > 0.05 && f.phi < 0.999 && std::isfinite(f.t_reversion);
    return f;
}

std::vector<std::vector<double>> top_eigenvectors(
    const std::vector<std::vector<double>>& C,
    std::size_t k,
    std::vector<double>& eigenvalues) {
    const std::size_t n = C.size();
    std::vector<std::vector<double>> vecs;
    if (!n) return vecs;
    k = std::min(k, n);
    for (std::size_t comp = 0; comp < k; ++comp) {
        std::vector<double> v(n, 0.0);
        for (std::size_t i = 0; i < n; ++i) v[i] = 1.0 + static_cast<double>((i + 3 * comp) % 7) / 10.0;
        double nv = norm2(v);
        for (double& x : v) x /= std::max(1e-12, nv);
        for (int it = 0; it < 100; ++it) {
            std::vector<double> w(n, 0.0);
            for (std::size_t i = 0; i < n; ++i)
                for (std::size_t j = 0; j < n; ++j)
                    w[i] += C[i][j] * v[j];
            for (const auto& u : vecs) {
                const double projection = dot(w, u);
                for (std::size_t i = 0; i < n; ++i) w[i] -= projection * u[i];
            }
            nv = norm2(w);
            if (nv < 1e-10) break;
            for (std::size_t i = 0; i < n; ++i) v[i] = w[i] / nv;
        }
        std::vector<double> Cv(n, 0.0);
        for (std::size_t i = 0; i < n; ++i)
            for (std::size_t j = 0; j < n; ++j)
                Cv[i] += C[i][j] * v[j];
        const double lambda = dot(v, Cv);
        if (!std::isfinite(lambda) || lambda <= 1e-6) break;
        vecs.push_back(v);
        eigenvalues.push_back(lambda);
    }
    return vecs;
}

bool solve_linear(std::vector<std::vector<double>> A, std::vector<double> b, std::vector<double>& x) {
    const std::size_t n = b.size();
    if (A.size() != n) return false;
    for (std::size_t i = 0; i < n; ++i) {
        std::size_t pivot = i;
        for (std::size_t r = i + 1; r < n; ++r)
            if (std::abs(A[r][i]) > std::abs(A[pivot][i])) pivot = r;
        if (std::abs(A[pivot][i]) < 1e-10) return false;
        std::swap(A[i], A[pivot]);
        std::swap(b[i], b[pivot]);
        const double d = A[i][i];
        for (std::size_t c = i; c < n; ++c) A[i][c] /= d;
        b[i] /= d;
        for (std::size_t r = 0; r < n; ++r) {
            if (r == i) continue;
            const double q = A[r][i];
            if (std::abs(q) < 1e-14) continue;
            for (std::size_t c = i; c < n; ++c) A[r][c] -= q * A[i][c];
            b[r] -= q * b[i];
        }
    }
    x = std::move(b);
    return true;
}

double touch_size(const pm::Book& b, bool ask_side) {
    const double px = ask_side ? b.best_ask() : b.best_bid();
    if (!std::isfinite(px)) return 0.0;
    double q = 0.0;
    const auto& levels = ask_side ? b.asks : b.bids;
    for (const auto& l : levels)
        if (std::abs(l.price - px) <= 1e-9) q += l.size;
    return q;
}

PortfolioEconomics evaluate_portfolio(
    const std::vector<Leg>& legs,
    double target_expected_side_move,
    double slippage_bps,
    bool maker_entry) {
    PortfolioEconomics out;
    if (legs.empty()) return out;
    const double slip = slippage_bps / 10000.0;
    double capital = 0.0, pnl = 0.0, raw_pnl = 0.0;
    double max_units = std::numeric_limits<double>::infinity();

    for (const auto& l : legs) {
        if (!l.book || l.shares <= 0.0) return out;
        const double mid = l.book->midpoint();
        const double spread = l.book->spread();
        const double bid = l.book->best_bid();
        const double ask = l.book->best_ask();
        if (!std::isfinite(mid) || !std::isfinite(spread) || !std::isfinite(bid) || !std::isfinite(ask)) return out;

        double entry = 0.0, fee_in = 0.0;
        if (maker_entry) {
            entry = bid;
        } else {
            entry = ask * (1.0 + slip);
            fee_in = pm::Engine::protocol_fee(l.shares, entry, l.fee);
        }
        const double expected_side_move = l.target ? target_expected_side_move : 0.0;
        const double future_mid = std::clamp(mid + expected_side_move, 0.001, 0.999);
        const double exit = std::clamp(future_mid - 0.5 * spread, 0.001, 0.999) * (1.0 - slip);
        const double fee_out = pm::Engine::protocol_fee(l.shares, exit, l.fee);

        capital += l.shares * entry + fee_in;
        pnl += l.shares * (exit - entry) - fee_in - fee_out;
        if (l.target) raw_pnl += l.shares * expected_side_move;

        const double touch = touch_size(*l.book, !maker_entry);
        if (touch <= 0.0) return out;
        max_units = std::min(max_units, touch / l.shares);
    }
    if (capital <= 1e-10 || !std::isfinite(max_units) || max_units <= 0.0) return out;
    out.raw_edge = raw_pnl / capital;
    out.net_edge = pnl / capital;
    out.capital = capital;
    out.executable_notional = max_units * capital;
    out.ok = true;
    return out;
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config = "config/paper_v3.json", csv_path;
        std::size_t market_limit = 600, universe_limit = 100, lookback_hours = 336;
        std::size_t fidelity_minutes = 30, factors = 3, max_hedges = 8, top = 40;
        double min_liquidity = 25.0, min_z = 0.90, min_t = 0.75, max_half_life_hours = 240.0;
        double max_factor_hedge_error = 0.65;

        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a);
                return std::string(argv[++i]);
            };
            if (a == "--config") config = next();
            else if (a == "--markets") market_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--universe") universe_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--lookback-hours") lookback_hours = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--fidelity-minutes") fidelity_minutes = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--factors") factors = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--max-hedges") max_hedges = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--min-liquidity") min_liquidity = std::stod(next());
            else if (a == "--min-z") min_z = std::stod(next());
            else if (a == "--min-t-reversion") min_t = std::stod(next());
            else if (a == "--max-half-life-hours") max_half_life_hours = std::stod(next());
            else if (a == "--max-factor-hedge-error") max_factor_hedge_error = std::stod(next());
            else if (a == "--top") top = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--csv") csv_path = next();
            else if (a == "--help" || a == "-h") {
                std::cout << "polymarket_pca_stat_arb [--config FILE] [--markets N] [--universe N] "
                             "[--lookback-hours H] [--fidelity-minutes M] [--factors K] [--max-hedges N] "
                             "[--min-z Z] [--min-t-reversion T] [--max-half-life-hours H] "
                             "[--max-factor-hedge-error X] [--top N] [--csv FILE]\n";
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + a);
            }
        }

        auto cfg = pm::Engine::load_config(config);
        pm::PolymarketApi api(cfg);
        auto markets = api.discover_markets(market_limit, min_liquidity);

        std::vector<std::string> all_tokens;
        all_tokens.reserve(markets.size() * 2);
        for (const auto& m : markets) {
            all_tokens.push_back(m.yes_token);
            all_tokens.push_back(m.no_token);
        }
        auto books = api.fetch_books(all_tokens);
        const auto now = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();

        std::vector<const pm::Market*> universe;
        for (const auto& m : markets) {
            if (!pm::pregame_market_eligible(
                    pm::MarketTiming{m.timed_sports, m.game_start_ts, m.seconds_delay}, now)) continue;
            auto y = books.find(m.yes_token), n = books.find(m.no_token);
            if (y == books.end() || n == books.end()) continue;
            if (!pm::model_market_eligible(y->second, n->second, cfg.min_mid, cfg.max_mid, cfg.max_spread)) continue;
            universe.push_back(&m);
        }
        std::sort(universe.begin(), universe.end(), [](auto* a, auto* b) {
            return a->liquidity + 0.20 * a->volume24h > b->liquidity + 0.20 * b->volume24h;
        });
        if (universe.size() > universe_limit) universe.resize(universe_limit);

        std::vector<std::string> yes_tokens;
        for (auto* m : universe) yes_tokens.push_back(m->yes_token);
        const auto start = now - static_cast<std::int64_t>(lookback_hours) * 3600;
        auto hist = api.fetch_price_history(yes_tokens, start, now, fidelity_minutes);
        const std::int64_t bucket_s = static_cast<std::int64_t>(std::max<std::size_t>(1, fidelity_minutes)) * 60;

        struct Series {
            const pm::Market* market = nullptr;
            std::map<std::int64_t,double> level;
            double mu = 0.0;
            double sigma = 1.0;
        };
        std::vector<Series> s;
        for (auto* m : universe) {
            auto it = hist.find(m->yes_token);
            if (it == hist.end()) continue;
            auto lv = bucket_levels(it->second, bucket_s);
            const double current = books.at(m->yes_token).midpoint();
            if (std::isfinite(current)) lv[(now / bucket_s) * bucket_s] = pm::logit(current);
            if (lv.size() < 24) continue;
            std::vector<double> values;
            values.reserve(lv.size());
            for (const auto& kv : lv) values.push_back(kv.second);
            const double mu = mean(values), sigma = sd(values, mu);
            if (sigma < 1e-4) continue;
            s.push_back({m, std::move(lv), mu, sigma});
        }
        const std::size_t N = s.size();
        if (N < std::max<std::size_t>(4, factors + 1)) {
            std::cout << "pca_stat_arb insufficient_series=" << N << '\n';
            return 0;
        }

        // Pairwise correlation on a regular as-of panel. Prediction-market
        // histories update asynchronously; exact timestamp intersections were
        // discarding most of the usable covariance information.
        const std::int64_t panel_start = (start / bucket_s) * bucket_s;
        const std::int64_t panel_end = (now / bucket_s) * bucket_s;
        const std::int64_t max_age = 3 * bucket_s;
        std::vector<std::vector<double>> C(N, std::vector<double>(N, 0.0));
        for (std::size_t i = 0; i < N; ++i) C[i][i] = 1.0;
        for (std::size_t i = 0; i < N; ++i) {
            for (std::size_t j = i + 1; j < N; ++j) {
                double sum = 0.0;
                std::size_t n = 0;
                for (std::int64_t ts = panel_start; ts <= panel_end; ts += bucket_s) {
                    auto av = asof_level(s[i].level, ts, max_age);
                    auto bv = asof_level(s[j].level, ts, max_age);
                    if (!av || !bv) continue;
                    sum += ((*av - s[i].mu) / s[i].sigma) * ((*bv - s[j].mu) / s[j].sigma);
                    ++n;
                }
                if (n >= 16) C[i][j] = C[j][i] = std::clamp(sum / static_cast<double>(n), -1.0, 1.0);
            }
        }

        std::vector<double> eigenvalues;
        auto vecs = top_eigenvectors(C, factors, eigenvalues);
        if (vecs.empty()) throw std::runtime_error("PCA factor extraction failed");
        const double explained = std::clamp(
            std::accumulate(eigenvalues.begin(), eigenvalues.end(), 0.0) / static_cast<double>(N), 0.0, 1.0);

        // Regular as-of residual panel with bounded carry-forward staleness.
        std::vector<std::vector<TimedValue>> residuals(N);
        for (std::int64_t ts = panel_start; ts <= panel_end; ts += bucket_s) {
            std::vector<double> y(N, 0.0);
            std::vector<bool> observed(N, false);
            std::size_t count = 0;
            for (std::size_t j = 0; j < N; ++j) {
                auto value = asof_level(s[j].level, ts, max_age);
                if (!value) continue;
                y[j] = (*value - s[j].mu) / s[j].sigma;
                observed[j] = true;
                ++count;
            }
            if (count < std::max<std::size_t>(4, N / 5)) continue;
            std::vector<double> factor(vecs.size(), 0.0);
            for (std::size_t k = 0; k < vecs.size(); ++k) {
                double num = 0.0, den = 0.0;
                for (std::size_t j = 0; j < N; ++j) if (observed[j]) {
                    num += vecs[k][j] * y[j];
                    den += vecs[k][j] * vecs[k][j];
                }
                if (den > 1e-10) factor[k] = num / den;
            }
            for (std::size_t j = 0; j < N; ++j) if (observed[j]) {
                double fitted = 0.0;
                for (std::size_t k = 0; k < vecs.size(); ++k) fitted += vecs[k][j] * factor[k];
                residuals[j].push_back({ts, y[j] - fitted});
            }
        }

        std::vector<double> current_y(N, 0.0);
        std::vector<bool> current_observed(N, false);
        for (std::size_t j = 0; j < N; ++j) {
            const double p = books.at(s[j].market->yes_token).midpoint();
            if (!std::isfinite(p)) continue;
            current_y[j] = (pm::logit(p) - s[j].mu) / s[j].sigma;
            current_observed[j] = true;
        }
        std::vector<double> current_factor(vecs.size(), 0.0);
        for (std::size_t k = 0; k < vecs.size(); ++k) {
            double num = 0.0, den = 0.0;
            for (std::size_t j = 0; j < N; ++j) if (current_observed[j]) {
                num += vecs[k][j] * current_y[j];
                den += vecs[k][j] * vecs[k][j];
            }
            if (den > 1e-10) current_factor[k] = num / den;
        }

        std::vector<Opportunity> opportunities;
        std::size_t mr_pass = 0, relation_pass = 0, hedge_candidates = 0, hedge_pass = 0;
        for (std::size_t j = 0; j < N; ++j) {
            auto fit = fit_residual(residuals[j], bucket_s);
            if (!fit.ok || fit.t_reversion > -min_t) continue;
            const std::size_t cut = residuals[j].size() / 2;
            if (cut < 12 || residuals[j].size() - cut < 12) continue;
            std::vector<TimedValue> r1(residuals[j].begin(), residuals[j].begin() + static_cast<std::ptrdiff_t>(cut));
            std::vector<TimedValue> r2(residuals[j].begin() + static_cast<std::ptrdiff_t>(cut), residuals[j].end());
            auto f1 = fit_residual(r1, bucket_s), f2 = fit_residual(r2, bucket_s);
            if (!f1.ok || !f2.ok) continue;
            const double stability = std::exp(-4.0 * std::abs(f1.phi - f2.phi)) *
                                     ((f1.t_reversion < 0.0 && f2.t_reversion < 0.0) ? 1.0 : 0.0);
            if (stability < 0.25) continue;
            const double bucket_hours = static_cast<double>(fidelity_minutes) / 60.0;
            const double half_life = -std::log(2.0) / std::log(fit.phi) * bucket_hours;
            if (!std::isfinite(half_life) || half_life <= 0.0 || half_life > max_half_life_hours) continue;
            ++mr_pass;

            double fitted_now = 0.0;
            for (std::size_t k = 0; k < vecs.size(); ++k) fitted_now += vecs[k][j] * current_factor[k];
            const double residual_now = current_y[j] - fitted_now;
            const double z = (residual_now - fit.mean) / fit.sd;
            if (std::abs(z) < min_z) continue;

            // PCA already supplies a statistical hedge relation. Explicit
            // event/semantic links receive a bonus, but are no longer a hard gate.
            // Aggregate factor error and stability remain the execution controls.
            struct HC { std::size_t index; double score; bool explicit_relation; };
            std::vector<HC> candidates;
            std::vector<double> target_loading(vecs.size(), 0.0);
            for (std::size_t k = 0; k < vecs.size(); ++k) target_loading[k] = vecs[k][j];
            const double target_norm = norm2(target_loading);
            if (target_norm < 1e-6) continue;
            for (std::size_t i = 0; i < N; ++i) {
                if (i == j || !current_observed[i]) continue;
                std::vector<double> loading(vecs.size(), 0.0);
                for (std::size_t k = 0; k < vecs.size(); ++k) loading[k] = vecs[k][i];
                const double loading_norm = norm2(loading);
                if (loading_norm < 1e-4) continue;
                const double cosine = std::abs(dot(target_loading, loading)) /
                                      std::max(1e-8, target_norm * loading_norm);
                const bool explicit_relation =
                    pm::market_relation(*s[j].market, *s[i].market) != pm::MarketRelation::none;
                if (!explicit_relation && cosine < 0.10) continue;
                if (explicit_relation) ++relation_pass;
                ++hedge_candidates;
                const double relation_bonus = explicit_relation ? 1.50 : 1.0;
                const double score = (0.25 + cosine) * relation_bonus *
                                     std::log1p(std::max(0.0, s[i].market->liquidity));
                candidates.push_back({i, score, explicit_relation});
            }
            std::sort(candidates.begin(), candidates.end(), [](const auto& a, const auto& b) { return a.score > b.score; });
            if (candidates.size() > max_hedges) candidates.resize(max_hedges);
            if (candidates.empty()) continue;

            const std::size_t K = vecs.size();
            std::vector<std::vector<double>> A(K, std::vector<double>(K, 0.0));
            std::vector<double> b(K, 0.0);
            for (std::size_t k = 0; k < K; ++k) {
                A[k][k] = 1e-3;
                b[k] = -vecs[k][j];
            }
            for (const auto& c : candidates) {
                for (std::size_t k = 0; k < K; ++k)
                    for (std::size_t l = 0; l < K; ++l)
                        A[k][l] += vecs[k][c.index] * vecs[l][c.index];
            }
            std::vector<double> coeff;
            if (!solve_linear(A, b, coeff)) continue;

            std::vector<std::pair<std::size_t,double>> hedges;
            for (const auto& c : candidates) {
                double h = 0.0;
                for (std::size_t k = 0; k < K; ++k) h += vecs[k][c.index] * coeff[k];
                h = std::clamp(h, -4.0, 4.0);
                if (std::abs(h) >= 0.01) hedges.push_back({c.index, h});
            }
            if (hedges.empty()) continue;
            std::vector<double> remaining = target_loading;
            for (const auto& [i,h] : hedges)
                for (std::size_t k = 0; k < K; ++k) remaining[k] += h * vecs[k][i];
            const double hedge_error = norm2(remaining) / std::max(1e-8, target_norm);
            if (!std::isfinite(hedge_error) || hedge_error > max_factor_hedge_error) continue;
            ++hedge_pass;

            const double delta_standardized = -(residual_now - fit.mean) * 0.5; // one estimated half-life
            const int direction = delta_standardized >= 0.0 ? 1 : -1;
            const double p_target = books.at(s[j].market->yes_token).midpoint();
            const double delta_logit = std::clamp(delta_standardized * s[j].sigma, -0.75, 0.75);
            const double p_new = pm::logistic(pm::logit(p_target) + delta_logit);
            const double target_side_move = std::abs(p_new - p_target);
            if (target_side_move < 1e-6) continue;

            const double target_sensitivity = std::max(1e-4, s[j].sigma * p_target * (1.0 - p_target));
            std::vector<Leg> legs;
            auto add_leg = [&](std::size_t i, double standardized_weight, bool target) -> bool {
                const double p = books.at(s[i].market->yes_token).midpoint();
                if (!std::isfinite(p)) return false;
                const double sensitivity = std::max(1e-4, s[i].sigma * p * (1.0 - p));
                const double shares = std::abs(standardized_weight) * target_sensitivity / sensitivity;
                if (shares < 0.01 || shares > 10.0) return false;
                const std::string side = standardized_weight >= 0.0 ? "YES" : "NO";
                const auto& book = side == "YES" ? books.at(s[i].market->yes_token) : books.at(s[i].market->no_token);
                legs.push_back({s[i].market, &book, api.fetch_fee_details(*s[i].market), side, shares, target});
                return true;
            };

            if (!add_leg(j, static_cast<double>(direction), true)) continue;
            bool leg_ok = true;
            for (const auto& [i,h] : hedges) {
                const double w = static_cast<double>(direction) * h;
                if (std::abs(w) < 0.02) continue;
                if (!add_leg(i, w, false)) { leg_ok = false; break; }
            }
            if (!leg_ok || legs.size() < 2) continue;

            auto taker = evaluate_portfolio(legs, target_side_move, cfg.slippage_bps, false);
            auto maker = evaluate_portfolio(legs, target_side_move, cfg.slippage_bps, true);
            if (!taker.ok || !maker.ok) continue;

            std::ostringstream leg_text;
            for (std::size_t q = 0; q < legs.size(); ++q) {
                if (q) leg_text << '|';
                leg_text << legs[q].market->id << ':' << legs[q].side << ':' << legs[q].shares;
            }
            const double score = maker.net_edge * std::abs(z) * stability * (1.0 - hedge_error) /
                                 std::sqrt(std::max(1.0, half_life));
            opportunities.push_back({
                s[j].market,
                legs.front().side,
                residuals[j].size(),
                legs.size() - 1,
                explained,
                z,
                fit.phi,
                half_life,
                fit.t_reversion,
                stability,
                hedge_error,
                target_side_move,
                taker.raw_edge,
                taker.net_edge,
                maker.net_edge,
                std::min(taker.executable_notional, maker.executable_notional),
                leg_text.str(),
                score
            });
        }

        std::sort(opportunities.begin(), opportunities.end(), [](const auto& a, const auto& b) { return a.score > b.score; });
        std::size_t raw_positive = 0, taker_positive = 0, maker_positive = 0;
        for (const auto& o : opportunities) {
            if (o.raw_expected_edge > 0.0) ++raw_positive;
            if (o.taker_net_edge > 0.0) ++taker_positive;
            if (o.maker_entry_net_edge > 0.0) ++maker_positive;
        }

        std::cout << "pca_stat_arb discovered=" << markets.size()
                  << " panel_series=" << N
                  << " factors=" << vecs.size()
                  << " explained=" << explained
                  << " mr_pass=" << mr_pass
                  << " relation_pass=" << relation_pass
                  << " hedge_candidates=" << hedge_candidates
                  << " hedge_pass=" << hedge_pass
                  << " opportunities=" << opportunities.size()
                  << " raw_positive=" << raw_positive
                  << " taker_positive=" << taker_positive
                  << " maker_entry_positive=" << maker_positive << '\n';
        std::cout << "market,side,obs,hedges,residual_z,phi,half_life_h,t_reversion,stability,hedge_error,"
                     "expected_mark_move,raw_expected_edge,taker_net_edge,maker_entry_net_edge,executable_notional,legs\n";
        for (std::size_t i = 0; i < std::min(top, opportunities.size()); ++i) {
            const auto& o = opportunities[i];
            std::cout << o.market->id << ',' << o.side << ',' << o.observations << ',' << o.hedge_legs << ','
                      << o.residual_z << ',' << o.phi << ',' << o.half_life_hours << ',' << o.t_reversion << ','
                      << o.stability << ',' << o.factor_hedge_error << ',' << o.expected_mark_move << ','
                      << o.raw_expected_edge << ',' << o.taker_net_edge << ',' << o.maker_entry_net_edge << ','
                      << o.executable_notional << ',' << o.legs << '\n';
        }

        if (!csv_path.empty()) {
            std::ofstream out(csv_path);
            if (!out) throw std::runtime_error("Cannot open output CSV: " + csv_path);
            out << "market,slug,side,obs,hedges,explained,residual_z,phi,half_life_h,t_reversion,stability,hedge_error,"
                   "expected_mark_move,raw_expected_edge,taker_net_edge,maker_entry_net_edge,executable_notional,legs\n";
            for (const auto& o : opportunities) {
                out << o.market->id << ',' << o.market->slug << ',' << o.side << ',' << o.observations << ','
                    << o.hedge_legs << ',' << o.explained << ',' << o.residual_z << ',' << o.phi << ','
                    << o.half_life_hours << ',' << o.t_reversion << ',' << o.stability << ',' << o.factor_hedge_error << ','
                    << o.expected_mark_move << ',' << o.raw_expected_edge << ',' << o.taker_net_edge << ','
                    << o.maker_entry_net_edge << ',' << o.executable_notional << ',' << o.legs << '\n';
            }
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
