#pragma once

#include "poly/types.hpp"
#include <armadillo>
#include <cstdint>
#include <deque>
#include <string>
#include <unordered_map>
#include <vector>

namespace poly {

struct ModelConfig {
    std::size_t lookback{120};
    std::size_t min_history{60};
    std::size_t max_factor_universe{600};
    std::size_t global_factors{8};
    std::size_t local_factors{3};
    double ew_half_life{40.0};
    double robust_threshold{1.75};
    int bar_seconds{60};
    int max_stale_bars{2};
    double horizon_min{15.0};
    double min_ou_rho{0.05};
    double max_ou_rho{0.995};
    double max_ou_half_life_min{240.0};
    double max_ou_unit_root_t{-1.25};
    double min_state_z{1.0};
    double min_liquidity{50.0};
    double uncertainty_penalty{1.5};
    std::size_t graph_neighbors{8};
    double graph_min_similarity{0.28};
    double graph_reversion_half_life_min{30.0};
};

class StatisticalModel {
public:
    explicit StatisticalModel(ModelConfig cfg = {});
    void observe(const std::unordered_map<std::string, TokenMeta>& meta,
                 const std::unordered_map<std::string, BookSnapshot>& books);
    void seed_prices(const std::unordered_map<std::string, std::vector<HistoricalPoint>>& prices);
    std::vector<Signal> generate(const std::unordered_map<std::string, TokenMeta>& meta,
                                 const std::unordered_map<std::string, BookSnapshot>& books);
    [[nodiscard]] std::size_t history_points(const std::string& token_id) const;

private:
    struct Observation { std::int64_t ts{0}; double logit{0.0}; double mid{0.0}; };
    struct Series { std::deque<Observation> obs; };
    struct FitResult {
        std::vector<std::string> tokens;
        arma::mat standardized_returns;
        arma::rowvec means;
        arma::rowvec scales;
        arma::mat loadings;
        arma::rowvec forecast_delta_logit;
        arma::rowvec forecast_sigma_logit;
        arma::rowvec state_z;
        arma::rowvec rho;
        arma::rowvec half_life_min;
        arma::rowvec unit_root_t;
        arma::urowvec valid_ou;
        double residual_scale{1.0};
        bool ok{false};
    };

    ModelConfig cfg_;
    std::unordered_map<std::string, Series> history_;
    std::vector<std::string> liquid_universe(const std::unordered_map<std::string, TokenMeta>& meta) const;
    FitResult fit_pca(const std::vector<std::string>& tokens, bool ew, bool robust, std::size_t factors = 0) const;
    std::vector<Signal> signals_from_fit(const FitResult& fit,
                                         SignalKind kind,
                                         const std::unordered_map<std::string, TokenMeta>& meta,
                                         const std::unordered_map<std::string, BookSnapshot>& books,
                                         double blend_weight = 1.0) const;
    std::vector<Signal> hierarchical_signals(const std::vector<std::string>& universe,
                                             const std::unordered_map<std::string, TokenMeta>& meta,
                                             const std::unordered_map<std::string, BookSnapshot>& books) const;
    std::vector<Signal> graph_signals(const std::vector<std::string>& universe,
                                      const std::unordered_map<std::string, TokenMeta>& meta,
                                      const std::unordered_map<std::string, BookSnapshot>& books) const;
};

class LogicalArbModel {
public:
    std::vector<Signal> generate(const std::unordered_map<std::string, TokenMeta>& meta,
                                 const std::unordered_map<std::string, BookSnapshot>& books) const;
};

} // namespace poly
