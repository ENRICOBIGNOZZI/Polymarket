#include "poly/engine.hpp"
#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    using namespace poly;
    auto a=hash_text_embedding("Will Bitcoin exceed 150000 before December?",32);
    auto b=hash_text_embedding("Will Bitcoin exceed 200000 before December?",32);
    auto c=hash_text_embedding("Will France win the World Cup?",32);
    assert(a.size()==32 && b.size()==32);
    assert(cosine_similarity(a,b) > cosine_similarity(a,c));
    assert(std::abs(clamp_probability(-2.0)-0.001)<1e-12);

    const std::string gamma = R"JSON([{"id":"42","question":"Test?","slug":"test","conditionId":"0xabc","active":true,"closed":false,"acceptingOrders":true,"negRisk":false,"liquidity":"1234.5","clobTokenIds":"[\"111\", \"222\"]","outcomePrices":"[\"0.61\", \"0.39\"]"}])JSON";
    auto parsed=parse_gamma_markets_json(gamma);
    assert(parsed.size()==1);
    assert(parsed[0].yes_token=="111" && parsed[0].no_token=="222");
    assert(std::abs(parsed[0].gamma_yes_price-0.61)<1e-9);

    EngineConfig cfg;
    RiskManager r(cfg);
    r.update_equity(9000.0);
    assert(!r.killed());
    r.update_equity(8499.0);
    assert(r.killed());

    PaperBroker broker(1000.0);
    TradeIdea idea; idea.market_id="m"; idea.token_id="t"; idea.outcome="YES"; idea.entry_price=0.5; idea.net_edge=0.05; idea.fair_probability=0.6;
    assert(broker.buy(idea,100.0));
    assert(std::abs(broker.cash()-900.0)<1e-9);
    std::unordered_map<std::string,double> mids{{"t",0.6}};
    assert(std::abs(broker.marked_equity(mids)-1020.0)<1e-9);

    std::cout << "all tests passed\n";
    return 0;
}
