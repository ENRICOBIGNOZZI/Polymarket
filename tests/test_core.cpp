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
    std::cout<<"core tests passed\n";
}
