#pragma once

#include "poly/types.hpp"
#include <unordered_map>
#include <vector>

namespace poly {

struct RiskConfig {
    double max_drawdown{0.15};
    double soft_drawdown{0.06};
    double hard_deleverage_drawdown{0.12};
    double max_gross_fraction{1.50};
    double max_token_fraction{0.04};
    double max_event_fraction{0.12};
    double max_trade_fraction{0.015};
    double min_net_edge{0.003};
    double min_score{0.25};
    double depth_fraction{0.10};
};

class RiskEngine {
public:
    explicit RiskEngine(RiskConfig cfg = {});

    [[nodiscard]] double risk_multiplier(const PortfolioState& p) const;

    std::vector<OrderIntent> allocate(const std::vector<Signal>& signals,
                                      const std::unordered_map<std::string, TokenMeta>& meta,
                                      const std::unordered_map<std::string, BookSnapshot>& books,
                                      const PortfolioState& portfolio) const;

private:
    RiskConfig cfg_;
};

} // namespace poly
