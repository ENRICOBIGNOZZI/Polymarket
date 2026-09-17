#include "pm/v7_redundant_bbo.hpp"
#include <cassert>
using namespace pm::v7;
using namespace pm::v7::redundant_bbo;
Envelope e(unsigned lane,long long ts,int bid,int ask,long long rx=1000){
 Envelope x; x.lane=(unsigned char)lane; x.update.instrument_handle=17;
 x.update.market_handle=7; x.update.event_handle=8; x.update.exchange_event_ns=ts;
 x.update.receive_monotonic_ns=rx; x.update.best_bid_e4=bid; x.update.best_ask_e4=ask;
 x.update.valid=1; return x;
}
int main(){
 Gate q(Mode::Quorum2Of3);
 assert(q.observe(e(0,100,4900,5100,1000)).outcome==Outcome::None);
 auto b=q.observe(e(1,100,4900,5100,1010)); assert(b.outcome==Outcome::Actionable);
 assert(b.agreeing_lanes_mask==3 && b.first_receive_monotonic_ns==1000 && b.ready_monotonic_ns==1010);
 auto third=q.observe(e(2,100,4900,5100,1020)); assert(third.outcome!=Outcome::Actionable);
 assert(q.observe(e(0,99,4800,5200,1030)).outcome==Outcome::Stale);
 assert(q.observe(e(0,101,4800,5200,1040)).outcome==Outcome::None);
 assert(q.observe(e(1,101,4700,5300,1050)).outcome==Outcome::None);
 auto quorum=q.observe(e(2,101,4700,5300,1060)); assert(quorum.outcome==Outcome::Actionable);
 assert(quorum.agreeing_lanes_mask==(2|4) && quorum.update.best_bid_e4==4700 && quorum.update.best_ask_e4==5300);
 q.reset_instrument(17);
 assert(q.observe(e(0,200,4900,5100,2000)).outcome==Outcome::None);
 assert(q.observe(e(0,200,4800,5200,2010)).outcome==Outcome::Conflict);
 assert(q.observe(e(1,200,4700,5300,2020)).outcome==Outcome::None);
 assert(q.observe(e(2,200,4700,5300,2030)).outcome==Outcome::Actionable);
 Gate fast(Mode::FirstOf3Shadow);
 auto f=fast.observe(e(2,300,4900,5100,3000)); assert(f.outcome==Outcome::Actionable && f.agreeing_lanes_mask==4);
 assert(fast.observe(e(0,300,4900,5100,3010)).outcome==Outcome::Duplicate);
 assert(fast.observe(e(1,300,4800,5200,3020)).outcome==Outcome::Conflict);
 assert(fast.metrics().post_emit_conflicts==1);
 auto bad=e(0,400,4900,5100,4000); bad.update.instrument_handle=kInstrumentCapacity;
 assert(q.observe(bad).outcome==Outcome::OutOfRange);
 bad=e(3,400,4900,5100,4000); assert(q.observe(bad).outcome==Outcome::Invalid);
 return 0;
}
