#pragma once

#include "poly/types.hpp"
#include <armadillo>
#include <deque>
#include <string>
#include <unordered_map>
#include <vector>

namespace poly {

struct ModelConfig {
    std::size_t lookback{120};
    std::size_t min_history{40};
    std::size_t max_factor_universe{600};
    std::size_t global_factors{8};
    std::size_t local_factors{3};
    double ew_half_life{40.0};
    double robust_threshold{1.75};
    double assumed_reversion_half_life_min{90.0};
    double horizon_min{30.0};
    double min_liquidity{50.0};
    double uncertainty_penalty{1.5};
    std::size_t graph_neighbors{8};
    double graph_min_similarity{0.28};
};

class StatisticalModel {
public:
    explicit StatisticalModel(ModelConfig cfg = {});

    void observe(const std::unordered_map<std::string, TokenMeta>& meta,
                 const std::unordered_map<std::string, BookSnapshot>& books);

    void seed_prices(const std::unordered_map<std::string, std::vector<double>>& prices);

    std::vector<Signal> generate(const std::unordered_map<std::string, TokenMeta>& meta,
                                 const std::unordered_map<std::string, BookSnapshot>& books);

    [[nodiscard]] std::size_t history_points(const std::string& token_id) const;

private:
    struct Series {
        std::deque<double> logits;
        std::deque<double> mids;
    };

    struct FitResult {
        std::vector<std::string> tokens;
        arma::mat standardized_returns;
        arma::rowvec means;
        arma::rowvec scales;
        arma::mat loadings;
        arma::rowvec latest_residual;
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
