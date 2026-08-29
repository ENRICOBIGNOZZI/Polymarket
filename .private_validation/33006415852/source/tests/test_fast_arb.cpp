#include "pm/fast_arb.hpp"
#include <cassert>
#include <cmath>
#include <iostream>
#include <unordered_map>
using namespace pm;
using namespace pm::fast;

Book book(std::string token, double bid, double ask, double depth=1000.0) {
    Book b; b.token_id=std::move(token); b.bids={{bid,depth}}; b.asks={{ask,depth}}; b.tick_size=.01; b.min_order_size=1.0; return b;
}
int main(){
    Policy p; p.min_net_edge=0.0001; p.slippage_bps=0; p.latency_penalty_bps=0; p.max_notional_usd=100; p.min_executable_shares=1;
    Market m; m.id="m"; m.event_id="e"; m.condition_id="c"; m.yes_token="y"; m.no_token="n";
    auto y=book("y",.47,.48); auto n=book("n",.49,.50);
    auto b=evaluate_binary(m,y,n,FeeDetails{},p);
    assert(b.executable); assert(std::abs(b.net_edge_per_share-.02)<1e-9); assert(b.hard_arbitrage);

    apply_level(y,false,.48,0); assert(std::abs(y.best_ask()-.48)>1e-6 || !std::isfinite(y.best_ask()));
    apply_level(y,false,.49,25); assert(std::abs(y.best_ask()-.49)<1e-9);

    Market a=m; a.id="a"; a.condition_id="ca"; a.no_token="an"; a.yes_token="ay";
    Market c=m; c.id="b"; c.condition_id="cb"; c.no_token="bn"; c.yes_token="by";
    std::unordered_map<std::string,Book> books{{"an",book("an",.3,.31)},{"by",book("by",.65,.66)}};
    std::unordered_map<std::string,FeeDetails> fees;
    auto imp=evaluate_implication({"r","a","b"},a,c,books,fees,p);
    assert(imp.executable); assert(std::abs(imp.net_edge_per_share-.03)<1e-9);

    books={{"an",book("an",.3,.31)},{"bn",book("bn",.3,.31)}};
    auto mex=evaluate_mutual_exclusion({"mx","a","b"},a,c,books,fees,p);
    assert(mex.executable); assert(std::abs(mex.net_edge_per_share-.38)<1e-9);
    books={{"ay",book("ay",.3,.31)},{"by",book("by",.3,.31)}};
    auto exh=evaluate_exhaustive_pair({"ex","a","b"},a,c,books,fees,p);
    assert(exh.executable); assert(std::abs(exh.net_edge_per_share-.38)<1e-9);

    p.conversion_fixed_cost_usd=0.0;
    a.neg_risk=true; c.neg_risk=true; a.event_id="ne"; c.event_id="ne";
    books={{"an",book("an",.2,.21)},{"by",book("by",.7,.71)}};
    std::vector<const Market*> ng{&a,&c};
    auto conv=evaluate_negrisk_conversion("ne",a,ng,books,fees,p);
    assert(conv.executable); assert(conv.net_edge_per_share>.48);

    ExternalSignal s{.8,1.0,"test",1000};
    auto ext=evaluate_external_latency(m,book("y",.6,.61),book("n",.38,.39),FeeDetails{},s,1000*1000+1000,p);
    assert(ext.executable); assert(ext.net_edge_per_share>.18);

    auto maker=evaluate_maker_complete_set(m,book("y",.45,.50),book("n",.45,.50),p);
    assert(maker.executable); assert(maker.net_edge_per_share>0);

    std::cout << "fast arb tests passed\n";
}
