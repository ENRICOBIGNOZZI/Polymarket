#include "pm/engine.hpp"
#include "pm/market_data.hpp"
#include <cassert>
#include <cmath>
#include <iostream>

int main(){
    pm::Book b;
    b.tick_size=0.01;
    b.asks={{0.40,100},{0.41,100}};
    b.bids={{0.39,300},{0.38,100}};

    auto buy=pm::Engine::walk_book(b,true,60.0);
    assert(buy);
    assert(std::abs(buy->first-(100.0+20.0/0.41))<1e-8);
    assert(buy->second>0.40&&buy->second<0.41);

    auto sell=pm::Engine::walk_book(b,false,50.0);
    assert(sell&&sell->second<0.391&&sell->second>0.379);

    const double mid=b.midpoint();
    const double micro=b.microprice();
    assert(std::isfinite(micro));
    assert(micro>=b.best_bid()&&micro<=b.best_ask());
    assert(micro>mid);

    pm::Book y_pressure;
    y_pressure.tick_size=0.01;
    y_pressure.bids={{0.49,1000}};
    y_pressure.asks={{0.51,50}};
    pm::Book n_pressure;
    n_pressure.tick_size=0.01;
    n_pressure.bids={{0.49,50}};
    n_pressure.asks={{0.51,1000}};
    const auto pressure=pm::micro_forecast(y_pressure,n_pressure,2.5,0.75);
    assert(pressure.q_yes>y_pressure.midpoint());
    assert(pressure.confidence>0.0);
    n_pressure.asks={{0.80,1000}};
    assert(pm::model_market_eligible(y_pressure,n_pressure,0.01,0.99,0.35));

    pm::FeeDetails fd{0.04,1.0,true};
    double fee=pm::Engine::protocol_fee(100,0.5,fd);
    assert(std::abs(fee-1.0)<1e-12);
    assert(std::abs(pm::Engine::full_kelly(0.60,0.50)-0.20)<1e-12);
    assert(pm::Engine::full_kelly(0.40,0.50)==0.0);

    const std::int64_t start=10'000;
    pm::MarketTiming sports{true,start,3};
    assert(pm::pregame_market_eligible(sports,start-pm::kSportsPregameBufferSeconds-1));
    assert(!pm::pregame_market_eligible(sports,start-pm::kSportsPregameBufferSeconds));
    assert(!pm::pregame_market_eligible(sports,start+1));
    assert(pm::timed_sports_started(sports,start));
    assert(!pm::pregame_market_eligible(pm::MarketTiming{true,0,0},0));
    assert(pm::pregame_market_eligible(pm::MarketTiming{},start));

    pm::RecentTrade sell_trade;
    sell_trade.token_id="token";
    sell_trade.side="SELL";
    sell_trade.price=0.39;
    sell_trade.size=70.0;
    sell_trade.ts=101;
    sell_trade.id="trade-1";
    auto q=pm::consume_passive_bid_trade(50.0,40.0,0.39,0.01,100,sell_trade);
    assert(q.eligible_trade);
    assert(std::abs(q.queue_ahead)<1e-12);
    assert(std::abs(q.filled_shares-20.0)<1e-12);
    assert(std::abs(q.remaining_shares-20.0)<1e-12);

    auto buy_trade=sell_trade;
    buy_trade.side="BUY";
    auto no_buy_fill=pm::consume_passive_bid_trade(50.0,40.0,0.39,0.01,100,buy_trade);
    assert(!no_buy_fill.eligible_trade);
    assert(no_buy_fill.filled_shares==0.0);

    auto high_sell=sell_trade;
    high_sell.price=0.41;
    auto no_high_fill=pm::consume_passive_bid_trade(50.0,40.0,0.39,0.01,100,high_sell);
    assert(!no_high_fill.eligible_trade);
    assert(no_high_fill.filled_shares==0.0);

    auto stale_sell=sell_trade;
    stale_sell.ts=100;
    auto no_stale_fill=pm::consume_passive_bid_trade(50.0,40.0,0.39,0.01,100,stale_sell);
    assert(!no_stale_fill.eligible_trade);

    std::cout<<"core tests passed\n";
}
