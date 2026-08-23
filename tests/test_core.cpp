#include "poly/math.hpp"
#include "poly/paper_broker.hpp"
#include "poly/risk.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    using namespace poly;
    assert(std::abs(logistic(logit(0.37)) - 0.37) < 1e-10);
    assert(std::abs(taker_fee_usdc(100.0, 0.5, "finance") - 1.0) < 1e-10);
    assert(std::abs(taker_fee_usdc(100.0, 0.5, "geopolitics")) < 1e-12);

    BookSnapshot b;
    b.token_id = "t";
    b.asks = {{0.41, 50.0}, {0.42, 50.0}};
    b.bids = {{0.39, 100.0}};
    b.min_order_size = 1.0;
    TokenMeta m; m.token_id = "t"; m.category = "finance";
    PaperBroker broker(1000.0);
    OrderIntent o{.token_id="t", .side=Side::Buy, .shares=60.0, .max_slippage=0.02, .source=SignalKind::Pca};
    auto f = broker.execute(o, m, b);
    assert(f.has_value());
    assert(f->shares == 60.0);
    assert(f->avg_price > 0.41 && f->avg_price < 0.42);
    broker.mark({{"t", b}});
    assert(broker.state().equity < 1000.0);

    PaperBroker basket_broker(1000.0);
    TokenMeta my; my.token_id="y"; my.category="geopolitics";
    TokenMeta mn; mn.token_id="n"; mn.category="geopolitics";
    BookSnapshot by; by.token_id="y"; by.asks={{0.48,100.0}}; by.bids={{0.47,100.0}};
    BookSnapshot bn; bn.token_id="n"; bn.asks={{0.49,100.0}}; bn.bids={{0.48,100.0}};
    std::vector<OrderIntent> basket_orders{
        {.token_id="y",.side=Side::Buy,.shares=10.0,.max_slippage=0.0,.source=SignalKind::ExactBasketArb},
        {.token_id="n",.side=Side::Buy,.shares=10.0,.max_slippage=0.0,.source=SignalKind::ExactBasketArb}};
    auto basket_fills = basket_broker.execute_basket(basket_orders, {{"y",my},{"n",mn}}, {{"y",by},{"n",bn}});
    assert(basket_fills.size()==2);
    const double cash_after_good = basket_broker.state().cash;
    bn.asks={{0.49,5.0}};
    auto rejected = basket_broker.execute_basket(basket_orders, {{"y",my},{"n",mn}}, {{"y",by},{"n",bn}});
    assert(rejected.empty());
    assert(std::abs(basket_broker.state().cash-cash_after_good)<1e-10);

    RiskEngine risk;
    PortfolioState p; p.drawdown = 0.0;
    assert(std::abs(risk.risk_multiplier(p) - 1.0) < 1e-12);
    p.drawdown = 0.15;
    assert(risk.risk_multiplier(p) == 0.0);

    std::cout << "core tests passed\n";
}
