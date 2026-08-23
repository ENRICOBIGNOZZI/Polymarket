#include "pm/engine.hpp"
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
    // More bid than ask depth should push microprice above the midpoint.
    assert(micro>mid);

    pm::FeeDetails fd{0.04,1.0,true};
    double fee=pm::Engine::protocol_fee(100,0.5,fd);
    assert(std::abs(fee-1.0)<1e-12);
    assert(std::abs(pm::Engine::full_kelly(0.60,0.50)-0.20)<1e-12);
    assert(pm::Engine::full_kelly(0.40,0.50)==0.0);

    // Non-sports remain eligible. Sports fail closed if the game clock is missing,
    // and are excluded before a safety buffer reaches the scheduled start.
    pm::Market nonsport;
    assert(pm::eligible_before_game_start(nonsport,1000,60));
    pm::Market sport;
    sport.sports_market=true;
    assert(!pm::eligible_before_game_start(sport,1000,60));
    sport.game_start_ts=1200;
    assert(pm::eligible_before_game_start(sport,1000,60));
    assert(!pm::eligible_before_game_start(sport,1140,60));
    assert(!pm::eligible_before_game_start(sport,1200,0));

    // A resting bid is depleted only by a later public taker SELL at or below our price.
    pm::PublicTrade trade;
    trade.asset_id="token";
    trade.side="SELL";
    trade.price=0.39;
    trade.size=60.0;
    trade.ts=101;
    assert(pm::is_aggressive_sell_for_bid(trade,"token",0.39,0.01,100));
    trade.side="BUY";
    assert(!pm::is_aggressive_sell_for_bid(trade,"token",0.39,0.01,100));
    trade.side="SELL";
    trade.price=0.41;
    assert(!pm::is_aggressive_sell_for_bid(trade,"token",0.39,0.01,100));
    trade.price=0.39;
    trade.ts=100;
    assert(!pm::is_aggressive_sell_for_bid(trade,"token",0.39,0.01,100));

    // Queue-ahead must be exhausted before fills begin, and fills can be partial.
    auto q1=pm::consume_bid_queue(100.0,50.0,60.0);
    assert(std::abs(q1.queue_ahead-40.0)<1e-12);
    assert(std::abs(q1.remaining_shares-50.0)<1e-12);
    assert(q1.filled_shares==0.0);
    auto q2=pm::consume_bid_queue(q1.queue_ahead,q1.remaining_shares,70.0);
    assert(q2.queue_ahead==0.0);
    assert(std::abs(q2.filled_shares-30.0)<1e-12);
    assert(std::abs(q2.remaining_shares-20.0)<1e-12);
    auto q3=pm::consume_bid_queue(q2.queue_ahead,q2.remaining_shares,100.0);
    assert(std::abs(q3.filled_shares-20.0)<1e-12);
    assert(q3.remaining_shares==0.0);

    std::cout<<"core tests passed\n";
}
