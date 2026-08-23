#include "poly/math.hpp"
#include "poly/model.hpp"
#include "poly/paper_broker.hpp"
#include "poly/risk.hpp"

#include <chrono>
#include <cmath>
#include <iostream>
#include <string>
#include <unordered_map>

#define REQUIRE(cond) do { if (!(cond)) { std::cerr << "REQUIRE failed: " #cond " at " << __FILE__ << ':' << __LINE__ << '\n'; return 1; } } while (0)

int main() {
    using namespace poly;
    REQUIRE(std::abs(logistic(logit(0.37)) - 0.37) < 1e-10);
    REQUIRE(std::abs(taker_fee_usdc(100.0, 0.5, "finance") - 1.0) < 1e-10);
    REQUIRE(std::abs(taker_fee_usdc(100.0, 0.5, "geopolitics")) < 1e-12);
    REQUIRE(std::abs(taker_fee_usdc(100.0, 0.5, "politics") - 1.0) < 1e-10);

    BookSnapshot b; b.token_id="t"; b.asks={{0.41,50.0},{0.42,50.0}}; b.bids={{0.39,100.0}}; b.min_order_size=1.0;
    TokenMeta m; m.token_id="t"; m.category="finance";
    PaperBroker broker(1000.0);
    OrderIntent o{.token_id="t",.side=Side::Buy,.shares=60.0,.max_slippage=0.02,.source=SignalKind::Pca};
    auto f=broker.execute(o,m,b); REQUIRE(f.has_value()); REQUIRE(f->shares==60.0); REQUIRE(f->avg_price>0.41&&f->avg_price<0.42);
    broker.mark({{"t",b}}); REQUIRE(broker.state().equity<1000.0);
    BookSnapshot sell_book=b; sell_book.bids={{0.43,100.0}}; const double cash_before_sell=broker.state().cash;
    OrderIntent sell{.token_id="t",.side=Side::Sell,.shares=20.0,.max_slippage=0.01,.source=SignalKind::Pca};
    auto sf=broker.execute(sell,m,sell_book); REQUIRE(sf.has_value()&&sf->side==Side::Sell&&sf->shares==20.0);
    REQUIRE(broker.state().cash>cash_before_sell); REQUIRE(std::abs(broker.state().positions.at("t").shares-40.0)<1e-10);

    PaperBroker basket_broker(1000.0);
    TokenMeta my; my.token_id="y"; my.category="geopolitics"; TokenMeta mn; mn.token_id="n"; mn.category="geopolitics";
    BookSnapshot by; by.token_id="y"; by.asks={{0.48,100.0}}; by.bids={{0.47,100.0}};
    BookSnapshot bn; bn.token_id="n"; bn.asks={{0.49,100.0}}; bn.bids={{0.48,100.0}};
    std::vector<OrderIntent> basket_orders{{.token_id="y",.side=Side::Buy,.shares=10.0,.max_slippage=0.0,.source=SignalKind::ExactBasketArb},{.token_id="n",.side=Side::Buy,.shares=10.0,.max_slippage=0.0,.source=SignalKind::ExactBasketArb}};
    auto basket_fills=basket_broker.execute_basket(basket_orders,{{"y",my},{"n",mn}},{{"y",by},{"n",bn}}); REQUIRE(basket_fills.size()==2);
    const double cash_after_good=basket_broker.state().cash; bn.asks={{0.49,5.0}};
    auto rejected=basket_broker.execute_basket(basket_orders,{{"y",my},{"n",mn}},{{"y",by},{"n",bn}}); REQUIRE(rejected.empty()); REQUIRE(std::abs(basket_broker.state().cash-cash_after_good)<1e-10);

    RiskEngine risk; PortfolioState p; p.drawdown=0.0; REQUIRE(std::abs(risk.risk_multiplier(p)-1.0)<1e-12); p.drawdown=0.15; REQUIRE(risk.risk_multiplier(p)==0.0);
    PortfolioState ep; ep.equity=1000.0; ep.peak_equity=1000.0; Position stat_pos; stat_pos.token_id="t"; stat_pos.shares=10.0; stat_pos.mark=0.42; stat_pos.source=SignalKind::Pca; stat_pos.opened_at=Clock::now()-std::chrono::seconds(120); ep.positions["t"]=stat_pos;
    auto exits=risk.exits({},{{"t",m}},{{"t",sell_book}},ep); REQUIRE(exits.size()==1&&exits[0].side==Side::Sell);
    ep.positions["t"].source=SignalKind::ExactBasketArb; REQUIRE(risk.exits({},{{"t",m}},{{"t",sell_book}},ep).empty()); ep.drawdown=0.14;
    REQUIRE(!risk.exits({},{{"t",m}},{{"t",sell_book}},ep).empty()); REQUIRE(risk.risk_multiplier(ep)==0.0);

    LogicalArbModel arb;
    TokenMeta ymeta; ymeta.token_id="by"; ymeta.market_id="bm"; ymeta.event_id="be"; ymeta.outcome="Yes"; ymeta.category="geopolitics";
    TokenMeta nmeta=ymeta; nmeta.token_id="bn"; nmeta.outcome="No";
    BookSnapshot by2; by2.token_id="by"; by2.asks={{0.47,100.0}}; by2.bids={{0.46,100.0}}; BookSnapshot bn2; bn2.token_id="bn"; bn2.asks={{0.48,100.0}}; bn2.bids={{0.47,100.0}};
    auto binary_signals=arb.generate({{"by",ymeta},{"bn",nmeta}},{{"by",by2},{"bn",bn2}}); REQUIRE(binary_signals.size()==2); for(const auto&sig:binary_signals) REQUIRE(sig.kind==SignalKind::ExactBasketArb);
    std::unordered_map<std::string,TokenMeta> neg_meta; std::unordered_map<std::string,BookSnapshot> neg_books;
    for(int i=0;i<3;++i){const std::string token="ny"+std::to_string(i);TokenMeta nm;nm.token_id=token;nm.market_id="nm"+std::to_string(i);nm.event_id="ne";nm.outcome="Yes";nm.neg_risk=true;nm.category="politics";nm.event_title="candidate event";neg_meta[token]=nm;BookSnapshot nb;nb.token_id=token;nb.asks={{0.20,100.0}};nb.bids={{0.19,100.0}};neg_books[token]=nb;}
    auto neg_signals=arb.generate(neg_meta,neg_books); REQUIRE(neg_signals.size()==3); for(const auto&sig:neg_signals) REQUIRE(sig.kind==SignalKind::LogicalArb);

    ModelConfig bars; bars.lookback=5; bars.min_history=2; bars.bar_seconds=60; StatisticalModel bar_model(bars);
    bar_model.seed_prices({{"x",{{60,0.40},{89,0.41},{120,0.42},{180,0.43}}}}); REQUIRE(bar_model.history_points("x")==3);

    ModelConfig mc; mc.lookback=120; mc.min_history=60; mc.global_factors=1; mc.local_factors=1; mc.min_liquidity=1.0; mc.min_state_z=0.20; mc.max_ou_unit_root_t=-0.50; mc.min_ou_rho=0.0001; mc.max_ou_rho=0.99999; mc.max_ou_half_life_min=10000.0; mc.uncertainty_penalty=0.0; mc.horizon_min=120.0;
    StatisticalModel stat(mc); std::unordered_map<std::string,std::vector<HistoricalPoint>> sh; std::unordered_map<std::string,TokenMeta> sm; std::unordered_map<std::string,BookSnapshot> sb;
    constexpr int T=121,N=8; constexpr std::int64_t base=1700000000; double state[N]{};
    for(int k=0;k<T;++k){const double common=.25*std::sin(k*.08)+.10*std::sin(k*.021);for(int j=0;j<N;++j){if(k>0){const double eps=.015*std::sin(.37*k+1.1*j)+.008*std::cos(.19*k+.3*j);state[j]=.82*state[j]+eps;}const double load=.7+.6*j/(N-1.0);const double px=logistic(load*common+state[j]);sh["s"+std::to_string(j)].push_back({base+60LL*k,px});}}
    stat.seed_prices(sh);
    for(int j=0;j<N;++j){const std::string token="s"+std::to_string(j);const double px=sh[token].back().price;TokenMeta tm;tm.token_id=token;tm.market_id="sm"+std::to_string(j);tm.event_id="se"+std::to_string(j);tm.category="geopolitics";tm.outcome="Yes";tm.question="Synthetic "+token;tm.liquidity=10000;tm.volume=10000;sm[token]=tm;BookSnapshot bb;bb.token_id=token;bb.bids={{std::max(.001,px-.00001),10000}};bb.asks={{px,10000}};sb[token]=bb;}
    const auto ss=stat.generate(sm,sb); bool saw_pca=false; for(const auto&sig:ss)if(sig.kind==SignalKind::Pca&&sig.net_edge>0.0)saw_pca=true; REQUIRE(saw_pca);
    std::cout<<"core tests passed\n"; return 0;
}
