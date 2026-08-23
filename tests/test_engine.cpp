#include "poly/engine.hpp"
#include <cassert>
#include <cmath>
#include <iostream>

int main(){using namespace poly;
auto a=hash_text_embedding("Will Bitcoin exceed 150000 before December?",32),b=hash_text_embedding("Will Bitcoin exceed 200000 before December?",32),c=hash_text_embedding("Will France win the World Cup?",32);assert(cosine_similarity(a,b)>cosine_similarity(a,c));
const std::string gamma=R"JSON([{"id":"42","question":"Test?","conditionId":"0xabc","active":true,"closed":false,"acceptingOrders":true,"liquidity":"1234.5","clobTokenIds":"[\"111\",\"222\"]","outcomePrices":"[\"0.61\",\"0.39\"]"}])JSON";auto p=parse_gamma_markets_json(gamma);assert(p.size()==1&&p[0].yes_token=="111"&&p[0].no_token=="222");assert(std::abs(p[0].gamma_yes_price-.61)<1e-9);
assert(std::abs(platform_fee_per_share(.5,.05,1)-.0125)<1e-12);assert(std::abs(platform_fee_per_share(.5,.25,2)-.015625)<1e-12);
EngineConfig cfg;RiskManager r(cfg);r.update_equity(9000);assert(!r.killed());r.update_equity(8499);assert(r.killed());
PaperBroker br(1000);TradeIdea i;i.market_id="m";i.token_id="t";i.outcome="YES";i.entry_price=.5;i.fair_probability=.6;i.net_edge=.05;auto fill=br.buy(i,100,0,1,0);assert(fill&&std::abs(br.cash()-900)<1e-9);std::unordered_map<std::string,double>mid{{"t",.6}};assert(std::abs(br.marked_equity(mid)-1020)<1e-9);auto close=br.close_token("m","t","YES",.6,0,1,0);assert(close&&std::abs(br.cash()-1020)<1e-9&&br.positions().empty());
RiskManager rr(cfg);TradeIdea edge=i;double allowed=rr.allowed_notional(edge,10000,0,0,0,1490);assert(allowed<=10.000001);std::cout<<"all tests passed\n";}
