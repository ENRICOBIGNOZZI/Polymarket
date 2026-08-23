#include "pm/engine.hpp"
#include "pm/maker.hpp"
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

    // One-tick spread: improving would cross, so join the displayed bid.
    assert(std::abs(pm::MakerPaperController::quote_price(b,1)-0.39)<1e-12);
    assert(std::abs(pm::MakerPaperController::queue_ahead(b,0.39)-300.0)<1e-12);

    pm::Book wide;
    wide.tick_size=0.01;
    wide.bids={{0.30,50}};
    wide.asks={{0.35,60}};
    assert(std::abs(pm::MakerPaperController::quote_price(wide,1)-0.31)<1e-12);
    assert(pm::MakerPaperController::queue_ahead(wide,0.31)==0.0);

    pm::FeeDetails fd{0.04,1.0,true};
    double fee=pm::Engine::protocol_fee(100,0.5,fd);
    assert(std::abs(fee-1.0)<1e-12);
    assert(std::abs(pm::Engine::full_kelly(0.60,0.50)-0.20)<1e-12);
    assert(pm::Engine::full_kelly(0.40,0.50)==0.0);
    std::cout<<"core tests passed\n";
}
