#include "poly/engine.hpp"

#include <cassert>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unordered_map>

namespace {
double sig(double z) { return 1.0 / (1.0 + std::exp(-z)); }
poly::BookSnapshot book(const std::string& id, double mid) {
    poly::BookSnapshot b;
    b.token_id = id;
    b.best_bid = mid - 0.01;
    b.best_ask = mid + 0.01;
    b.bids = {{b.best_bid, 1000.0}};
    b.asks = {{b.best_ask, 1000.0}};
    b.bid_depth = b.ask_depth = 1000.0;
    return b;
}
poly::LiveMarket live(const std::string& id, double mid) {
    poly::Market m;
    m.id = id;
    m.question = "Related market " + id;
    m.yes_token = id + "Y";
    m.no_token = id + "N";
    m.active = true;
    m.accepting_orders = true;
    m.liquidity = 10000.0;
    return {m, book(m.yes_token, mid), book(m.no_token, 1.0-mid), poly::hash_text_embedding(m.question)};
}
}

int main() {
    using namespace poly;

    auto a = hash_text_embedding("Will Bitcoin exceed 150000 before December?", 32);
    auto b = hash_text_embedding("Will Bitcoin exceed 200000 before December?", 32);
    auto c = hash_text_embedding("Will France win the World Cup?", 32);
    assert(cosine_similarity(a,b) > cosine_similarity(a,c));

    const std::string gamma = R"JSON([{"id":"42","question":"Test?","conditionId":"0xabc","active":true,"closed":false,"acceptingOrders":true,"liquidity":"1234.5","clobTokenIds":"[\"111\",\"222\"]","outcomePrices":"[\"0.61\",\"0.39\"]","umaResolutionStatus":"proposed","feesEnabled":true,"feeSchedule":{"rate":0.04,"exponent":2,"takerOnly":true}}])JSON";
    auto p = parse_gamma_markets_json(gamma);
    assert(p.size() == 1 && p[0].yes_token == "111" && p[0].no_token == "222");
    assert(std::abs(p[0].gamma_yes_price - 0.61) < 1e-9);
    assert(p[0].fees.live && std::abs(p[0].fees.rate - 0.04) < 1e-12);
    assert(p[0].resolution_status == "proposed");

    const std::string keyset = R"JSON({"markets":[{"id":"7","question":"K?","active":true,"closed":false,"clobTokenIds":"[\"1\",\"2\"]","outcomePrices":"[\"0.4\",\"0.6\"]"}],"next_cursor":"abc"})JSON";
    auto kp = parse_gamma_markets_json(keyset);
    assert(kp.size() == 1 && kp[0].id == "7");

    assert(std::abs(platform_fee_per_share(0.5, 0.05, 1.0) - 0.0125) < 1e-12);
    assert(std::abs(platform_fee_per_share(0.5, 0.05, 2.0) - 0.0125) < 1e-12);

    EngineConfig cfg;
    RiskManager r(cfg);
    r.update_equity(9000.0);
    assert(!r.killed());
    r.update_equity(8499.0);
    assert(r.killed());

    PaperBroker br(1000.0);
    TradeIdea i;
    i.market_id = "m";
    i.token_id = "t";
    i.outcome = "YES";
    i.entry_price = 0.5;
    i.fair_probability = 0.6;
    i.net_edge = 0.05;
    auto fill = br.buy(i, 100.0, 0.0, 1.0, 0.0);
    assert(fill && std::abs(br.cash() - 900.0) < 1e-9);
    std::unordered_map<std::string,double> mid{{"t",0.6}};
    assert(std::abs(br.marked_equity(mid) - 1020.0) < 1e-9);
    auto close = br.close_token("m", "t", "YES", 0.6, 0.0, 1.0, 0.0);
    assert(close && std::abs(br.cash() - 1020.0) < 1e-9 && br.positions().empty());

    PaperBroker void_br(1000.0);
    TradeIdea void_i = i;
    void_i.token_id = "voidY";
    auto void_buy = void_br.buy(void_i, 100.0, 0.0, 1.0, 0.0);
    assert(void_buy);
    auto void_fills = void_br.settle_market("m", 0.5);
    assert(void_fills.size() == 1);
    assert(std::abs(void_fills[0].price - 0.5) < 1e-12);
    assert(std::abs(void_br.cash() - 1000.0) < 1e-9);

    RiskManager rr(cfg);
    TradeIdea edge = i;
    edge.estimated_cost = 0.0;
    const double allowed = rr.allowed_notional(edge, 10000.0, 0.0, 0.0, 0.0, 1490.0);
    assert(allowed <= 10.000001);

    const auto tmp = std::filesystem::temp_directory_path() / "poly_engine_test";
    std::filesystem::remove_all(tmp);
    std::filesystem::create_directories(tmp);
    const auto broker_state = (tmp / "broker.csv").string();
    br.save_state(broker_state);
    PaperBroker br2(1.0);
    br2.load_state(broker_state);
    assert(std::abs(br2.cash() - br.cash()) < 1e-9);

    const auto hist = (tmp / "history.csv").string();
    {
        std::ofstream out(hist);
        for (int t = 0; t < 30; ++t) {
            const double f = 0.12 * std::sin(t / 3.0);
            out << (1000+t) << ",a," << sig(-0.3 + 1.0*f) << '\n';
            out << (1000+t) << ",b," << sig( 0.1 + 0.8*f) << '\n';
            out << (1000+t) << ",c," << sig( 0.5 + 1.2*f) << '\n';
        }
    }
    cfg.min_factor_history = 20;
    cfg.factor_history = 30;
    UniversalModel model(cfg);
    model.load_history(hist);
    std::vector<LiveMarket> universe{live("a", sig(-0.3+0.15)), live("b", sig(0.1+0.10)), live("c", sig(0.5+0.14))};
    model.prepare_cycle(universe);
    ExternalSignalStore ext;
    auto fv = model.predict(universe[0], universe, ext);
    bool pca_active = false;
    for (const auto& ep : fv.components) if (ep.expert == "pca_factor") pca_active = ep.active;
    assert(pca_active);
    assert(!model.pending_market_ids().empty());
    model.forget_prediction("a");
    for (const auto& id : model.pending_market_ids()) assert(id != "a");

    std::filesystem::remove_all(tmp);
    std::cout << "all tests passed\n";
}
