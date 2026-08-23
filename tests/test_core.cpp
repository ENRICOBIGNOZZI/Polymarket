#include "poly/math.hpp"
#include "poly/model.hpp"
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

    BookSnapshot sell_book = b;
    sell_book.bids = {{0.43, 100.0}};
    const double cash_before_sell = broker.state().cash;
    OrderIntent sell{.token_id="t", .side=Side::Sell, .shares=20.0, .max_slippage=0.01, .source=SignalKind::Pca};
    auto sf = broker.execute(sell, m, sell_book);
    assert(sf.has_value() && sf->side == Side::Sell && sf->shares == 20.0);
    assert(broker.state().cash > cash_before_sell);
    assert(std::abs(broker.state().positions.at("t").shares - 40.0) < 1e-10);

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

    PortfolioState ep; ep.equity=1000.0; ep.peak_equity=1000.0;
    Position stat_pos; stat_pos.token_id="t"; stat_pos.shares=10.0; stat_pos.mark=0.42; stat_pos.source=SignalKind::Pca;
    stat_pos.opened_at = Clock::now() - std::chrono::seconds(120);
    ep.positions["t"] = stat_pos;
    auto exits = risk.exits({}, {{"t",m}}, {{"t",sell_book}}, ep);
    assert(exits.size()==1 && exits[0].side==Side::Sell);
    ep.positions["t"].source=SignalKind::ExactBasketArb;
    assert(risk.exits({}, {{"t",m}}, {{"t",sell_book}}, ep).empty());
    ep.drawdown=0.14;
    assert(!risk.exits({}, {{"t",m}}, {{"t",sell_book}}, ep).empty());
    assert(risk.risk_multiplier(ep)==0.0);

    LogicalArbModel arb;
    TokenMeta ymeta; ymeta.token_id="by"; ymeta.market_id="bm"; ymeta.event_id="be"; ymeta.outcome="Yes"; ymeta.category="geopolitics";
    TokenMeta nmeta=ymeta; nmeta.token_id="bn"; nmeta.outcome="No";
    BookSnapshot by2; by2.token_id="by"; by2.asks={{0.47,100.0}}; by2.bids={{0.46,100.0}};
    BookSnapshot bn2; bn2.token_id="bn"; bn2.asks={{0.48,100.0}}; bn2.bids={{0.47,100.0}};
    auto binary_signals = arb.generate({{"by",ymeta},{"bn",nmeta}}, {{"by",by2},{"bn",bn2}});
    assert(binary_signals.size()==2);
    for (const auto& sig : binary_signals) assert(sig.kind==SignalKind::ExactBasketArb);

    std::unordered_map<std::string,TokenMeta> neg_meta;
    std::unordered_map<std::string,BookSnapshot> neg_books;
    for (int i=0;i<3;++i) {
        const std::string token="ny"+std::to_string(i);
        TokenMeta nm; nm.token_id=token; nm.market_id="nm"+std::to_string(i); nm.event_id="ne";
        nm.outcome="Yes"; nm.neg_risk=true; nm.category="politics"; nm.event_title="candidate event";
        neg_meta[token]=nm;
        BookSnapshot nb; nb.token_id=token; nb.asks={{0.20,100.0}}; nb.bids={{0.19,100.0}};
        neg_books[token]=nb;
    }
    auto neg_signals=arb.generate(neg_meta,neg_books);
    assert(neg_signals.size()==3);
    for (const auto& sig : neg_signals) assert(sig.kind==SignalKind::LogicalArb);

    std::cout << "core tests passed\n";
}
