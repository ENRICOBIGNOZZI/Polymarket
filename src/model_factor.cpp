#include "poly/model.hpp"
#include "poly/math.hpp"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <map>
#include <numeric>
#include <unordered_set>

namespace poly {

StatisticalModel::StatisticalModel(ModelConfig cfg) : cfg_(std::move(cfg)) {}

void StatisticalModel::seed_prices(const std::unordered_map<std::string, std::vector<double>>& prices) {
    const std::size_t cap = std::max<std::size_t>(cfg_.lookback + 4, cfg_.min_history + 4);
    for (const auto& [token, px] : prices) {
        auto& s = history_[token];
        s.logits.clear();
        s.mids.clear();
        const std::size_t start = px.size() > cap ? px.size() - cap : 0;
        for (std::size_t i = start; i < px.size(); ++i) {
            const double p = px[i];
            if (!std::isfinite(p) || p <= 0.0 || p >= 1.0) continue;
            s.mids.push_back(p);
            s.logits.push_back(logit(p));
        }
    }
}

void StatisticalModel::observe(const std::unordered_map<std::string, TokenMeta>& meta,
                               const std::unordered_map<std::string, BookSnapshot>& books) {
    for (const auto& [token, book] : books) {
        const auto mit = meta.find(token);
        if (mit == meta.end()) continue;
        const double p = book.mid();
        if (!std::isfinite(p) || p <= 0.0 || p >= 1.0) continue;
        auto& s = history_[token];
        s.mids.push_back(p);
        s.logits.push_back(logit(p));
        const std::size_t cap = std::max<std::size_t>(cfg_.lookback + 4, cfg_.min_history + 4);
        while (s.mids.size() > cap) s.mids.pop_front();
        while (s.logits.size() > cap) s.logits.pop_front();
    }
}

std::size_t StatisticalModel::history_points(const std::string& token_id) const {
    const auto it = history_.find(token_id);
    return it == history_.end() ? 0 : it->second.logits.size();
}

std::vector<std::string> StatisticalModel::liquid_universe(const std::unordered_map<std::string, TokenMeta>& meta) const {
    std::vector<std::pair<double, std::string>> ranked;
    ranked.reserve(meta.size());
    for (const auto& [token, m] : meta) {
        const auto hit = history_.find(token);
        if (hit == history_.end() || hit->second.logits.size() < cfg_.min_history + 1) continue;
        if (!m.active || m.closed || !m.enable_order_book || m.liquidity < cfg_.min_liquidity) continue;
        ranked.emplace_back(m.liquidity + 0.01 * m.volume, token);
    }
    std::sort(ranked.begin(), ranked.end(), [](const auto& a, const auto& b){ return a.first > b.first; });
    if (ranked.size() > cfg_.max_factor_universe) ranked.resize(cfg_.max_factor_universe);
    std::vector<std::string> out; out.reserve(ranked.size());
    for (auto& x : ranked) out.push_back(std::move(x.second));
    return out;
}

StatisticalModel::FitResult StatisticalModel::fit_pca(const std::vector<std::string>& tokens,
                                                       bool ew,
                                                       bool robust,
                                                       std::size_t factors) const {
    FitResult fit;
    if (tokens.size() < 4) return fit;

    std::size_t points = cfg_.lookback + 1;
    for (const auto& token : tokens) {
        const auto it = history_.find(token);
        if (it == history_.end()) return fit;
        points = std::min(points, it->second.logits.size());
    }
    if (points < cfg_.min_history + 1) return fit;

    const std::size_t rows = points - 1;
    const std::size_t cols = tokens.size();
    arma::mat r(rows, cols, arma::fill::zeros);

    for (std::size_t j = 0; j < cols; ++j) {
        const auto& q = history_.at(tokens[j]).logits;
        const std::size_t start = q.size() - points;
        for (std::size_t t = 0; t < rows; ++t) {
            r(t, j) = q[start + t + 1] - q[start + t];
        }
    }

    arma::vec w(rows, arma::fill::ones);
    if (ew) {
        const double lambda = std::exp(-std::log(2.0) / std::max(1.0, cfg_.ew_half_life));
        double x = 1.0;
        for (std::size_t rr = rows; rr-- > 0;) {
            w(rr) = x;
            x *= lambda;
        }
    }
    w /= arma::accu(w);

    fit.means = w.t() * r;
    fit.scales.set_size(cols);
    arma::mat z(rows, cols, arma::fill::zeros);
    for (std::size_t j = 0; j < cols; ++j) {
        arma::vec d = r.col(j) - fit.means(j);
        double s = std::sqrt(std::max(1e-10, arma::dot(w, arma::square(d))));
        fit.scales(j) = s;
        z.col(j) = d / s;
    }

    const std::size_t requested = factors == 0 ? cfg_.global_factors : factors;
    const std::size_t k = std::min<std::size_t>(requested, std::min(rows, cols) - 1);
    if (k == 0) return fit;

    arma::mat sparse(rows, cols, arma::fill::zeros);
    arma::mat residuals;
    arma::mat V;
    for (int iter = 0; iter < (robust ? 5 : 1); ++iter) {
        arma::mat clean = z - sparse;
        arma::mat weighted = clean;
        for (std::size_t i = 0; i < rows; ++i) weighted.row(i) *= std::sqrt(w(i) * static_cast<double>(rows));
        arma::mat U;
        arma::vec sv;
        if (!arma::svd_econ(U, sv, V, weighted, "both", "std")) return fit;
        fit.loadings = V.cols(0, k - 1);
        arma::mat low_rank = clean * fit.loadings * fit.loadings.t();
        residuals = z - low_rank;
        if (robust) {
            sparse = residuals;
            const double tau = cfg_.robust_threshold;
            sparse.transform([&](double v) {
                const double a = std::abs(v) - tau;
                return a > 0.0 ? std::copysign(a, v) : 0.0;
            });
        }
    }

    const arma::rowvec latest = z.row(rows - 1);
    if (robust) {
        fit.latest_residual = sparse.row(rows - 1);
    } else {
        fit.latest_residual = latest - (latest * fit.loadings) * fit.loadings.t();
    }

    fit.residual_scale = std::sqrt(std::max(1e-8, arma::accu(arma::square(residuals)) /
                                                    static_cast<double>(residuals.n_elem)));
    fit.standardized_returns = std::move(z);
    fit.tokens = tokens;
    fit.ok = true;
    return fit;
}

std::vector<Signal> StatisticalModel::signals_from_fit(
        const FitResult& fit,
        SignalKind kind,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books,
        double blend_weight) const {
    std::vector<Signal> out;
    if (!fit.ok) return out;

    const double reversion = 1.0 - std::exp(-std::log(2.0) * cfg_.horizon_min /
                                             std::max(1.0, cfg_.assumed_reversion_half_life_min));
    for (std::size_t j = 0; j < fit.tokens.size(); ++j) {
        const auto& token = fit.tokens[j];
        const auto mit = meta.find(token);
        const auto bit = books.find(token);
        const auto hit = history_.find(token);
        if (mit == meta.end() || bit == books.end() || hit == history_.end() || hit->second.logits.empty()) continue;

        const double ask = bit->second.best_ask();
        const double bid = bit->second.best_bid();
        if (!std::isfinite(ask) || !std::isfinite(bid) || ask <= 0.0 || ask >= 1.0) continue;

        const double residual_logit = fit.latest_residual(j) * fit.scales(j) * blend_weight;
        const double current_logit = hit->second.logits.back();
        const double fair = logistic(current_logit - reversion * residual_logit);
        const double gross = fair - ask;
        if (gross <= 0.0) continue;

        const double fee_per_share = taker_fee_usdc(1.0, ask, mit->second.category);
        const double micro_cost = 0.25 * bit->second.spread();
        const double uncertainty = std::max(1e-5,
            fit.residual_scale * fit.scales(j) * ask * (1.0 - ask) /
            std::sqrt(static_cast<double>(fit.standardized_returns.n_rows)));
        const double cost = fee_per_share + micro_cost;
        const double net = gross - cost - cfg_.uncertainty_penalty * uncertainty;
        if (net <= 0.0) continue;

        Signal s;
        s.kind = kind;
        s.token_id = token;
        s.market_id = mit->second.market_id;
        s.event_id = mit->second.event_id;
        s.question = mit->second.question + " [" + mit->second.outcome + "]";
        s.side = Side::Buy;
        s.observed_price = ask;
        s.fair_price = fair;
        s.gross_edge = gross;
        s.est_cost = cost;
        s.uncertainty = uncertainty;
        s.net_edge = net;
        s.score = net / std::max(1e-4, uncertainty + bit->second.spread());
        s.expected_half_life_min = cfg_.assumed_reversion_half_life_min;
        s.confidence = std::clamp(1.0 - std::exp(-std::max(0.0, s.score)), 0.0, 0.999);
        s.rationale = std::string(to_string(kind)) + " factor residual mean reversion";
        out.push_back(std::move(s));
    }
    return out;
}

std::vector<Signal> StatisticalModel::hierarchical_signals(
        const std::vector<std::string>& universe,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books) const {
    std::vector<Signal> out;
    const auto global = fit_pca(universe, true, false);
    if (!global.ok) return out;

    std::unordered_map<std::string, double> global_residual;
    for (std::size_t j = 0; j < global.tokens.size(); ++j) {
        global_residual[global.tokens[j]] = global.latest_residual(j) * global.scales(j);
    }

    std::map<std::string, std::vector<std::string>> groups;
    for (const auto& token : universe) {
        const auto it = meta.find(token);
        if (it != meta.end()) groups[it->second.category].push_back(token);
    }

    for (auto& [category, tokens] : groups) {
        if (tokens.size() < std::max<std::size_t>(6, cfg_.local_factors + 2)) continue;
        auto local = fit_pca(tokens, true, true, cfg_.local_factors);
        if (!local.ok) continue;
        for (std::size_t j = 0; j < local.tokens.size(); ++j) {
            const auto git = global_residual.find(local.tokens[j]);
            if (git == global_residual.end()) continue;
            const double local_rl = local.latest_residual(j) * local.scales(j);
            const double blended = 0.5 * git->second + 0.5 * local_rl;
            local.latest_residual(j) = blended / std::max(1e-9, local.scales(j));
        }
        auto sig = signals_from_fit(local, SignalKind::HierarchicalPca, meta, books, 1.0);
        out.insert(out.end(), sig.begin(), sig.end());
    }
    return out;
}

} // namespace poly
