#include "pm/v7_coinbase_l2_observer.hpp"
#include <array>
#include <cassert>
#include <sstream>
#include <iostream>
#include <iomanip>
using namespace pm::v7::external_fair;
int main() {
    ExternalVenueIngress ingress(VenueId::CoinbaseSpot, 1);
    CoinbaseL2FrameObserver observer(ingress, 1);
    observer.on_connection_epoch(1);
    std::ostringstream payload;
    payload << std::fixed << std::setprecision(2);
    payload << "{\"type\":\"snapshot\",\"product_id\":\"BTC-USD\",\"bids\":[";
    for (int i=0; i<25000; ++i) {
        if(i) payload << ',';
        payload << "[\"" << 50000.0-i*.01 << "\",\"0.1\"]";
    }
    payload << "],\"asks\":[";
    for (int i=0; i<25000; ++i) {
        if(i) payload << ',';
        payload << "[\"" << 60000.0+i*.01 << "\",\"0.2\"]";
    }
    payload << "]}";
    const auto frame = payload.str();
    assert(frame.size() > 500000);
    observer.on_frame(1,1000000000LL,1700000000000000000LL,frame);
    const auto state = observer.metrics();
    std::cerr << observer.diagnostic() << " failures=" << state.parse_failures << '\n';
    assert(state.valid && state.parse_failures==0);
    assert(state.bid_levels==25000 && state.ask_levels==25000);
    assert(state.best_bid==50000.0 && state.best_ask==60000.0);
    std::array<ExternalVenueEvent,4> events{};
    const auto count=ingress.drain_events(events,4);
    assert(count==2); // initial recovery Health plus recovered BookTop.
    assert(events[1].event_type==ExternalEventType::BookTop && events[1].healthy);
}
