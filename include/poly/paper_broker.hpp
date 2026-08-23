#pragma once

#include "poly/types.hpp"
#include <unordered_map>
#include <vector>

namespace poly {

class PaperBroker {
public:
    explicit PaperBroker(double starting_cash = 10000.0);

    std::optional<Fill> execute(const OrderIntent& order,
                                const TokenMeta& meta,
                                const BookSnapshot& book);

    std::vector<Fill> execute_basket(const std::vector<OrderIntent>& orders,
                                     const std::unordered_map<std::string, TokenMeta>& meta,
                                     const std::unordered_map<std::string, BookSnapshot>& books);

    void mark(const std::unordered_map<std::string, BookSnapshot>& books);

    [[nodiscard]] const PortfolioState& state() const { return state_; }
    [[nodiscard]] const std::vector<Fill>& fills() const { return fills_; }

private:
    PortfolioState state_;
    std::vector<Fill> fills_;
};

} // namespace poly
