#include "pm/v7_prepared_order_frame.hpp"
#include <array>
#include <cassert>
#include <cstring>
#include <string_view>
#include <atomic>
#include <thread>
using namespace pm::v7::prepared_order;
static Key key(int price=5000){ Key k; k.instrument_handle=17; k.price_e4=price; k.tick_size_e4=100; k.quantity_microunits=5'000'000; k.side=0; k.time_in_force=1; return k; }
static Publication pub(long long mono,int price=5000,long long ex=100){ Publication p; p.key=key(price); p.source_exchange_event_ns=ex; p.prepared_monotonic_ns=mono; p.order_timestamp_ms=1710000000000ULL+(unsigned long long)mono; p.salt=42+(unsigned long long)mono; return p; }
static Requirement req(long long now,int price=5000,long long ex=100,long long age=1'000){ Requirement r; r.key=key(price); r.source_exchange_event_ns=ex; r.now_monotonic_ns=now; r.maximum_age_ns=age; return r; }
int main(){
 Cache c; std::array<char,4> a{'a','b','c','d'}, b{'w','x','y','z'};
 auto empty=c.try_acquire(req(100)); assert(empty.reason==AcquireReason::Empty);
 assert(c.publish(pub(100),a));
 auto mismatch=c.try_acquire(req(110,5100)); assert(mismatch.reason==AcquireReason::Mismatch);
 auto hit=c.try_acquire(req(110)); assert(hit.reason==AcquireReason::Hit && hit.lease.valid && hit.lease.size==4);
 assert(std::string_view(hit.lease.data,4)=="abcd");
 // One-shot even before release.
 auto reused=c.try_acquire(req(111)); assert(reused.reason==AcquireReason::AlreadyConsumed);
 // Publish into the other slot while the first frame is still leased.
 assert(c.publish(pub(120,5000,101),b));
 auto hit2=c.try_acquire(req(125,5000,101)); assert(hit2.reason==AcquireReason::Hit);
 // Both slots are leased: producer must not overwrite either.
 assert(!c.publish(pub(130,5000,102),a));
 c.release(hit.lease); assert(!hit.lease.valid);
 // After releasing old slot, it can become the next publication target.
 assert(c.publish(pub(140,5000,102),a));
 c.release(hit2.lease);
 auto stale=c.try_acquire(req(2000,5000,102,100)); assert(stale.reason==AcquireReason::Stale);
 auto exact_miss=c.try_acquire(req(145,5000,999,100)); assert(exact_miss.reason==AcquireReason::Mismatch);
 Requirement relaxed=req(145,5000,999,100); relaxed.require_exact_source_event=0;
 auto relaxed_hit=c.try_acquire(relaxed); assert(relaxed_hit.reason==AcquireReason::Hit); c.release(relaxed_hit.lease);
 std::array<char,kMaxFrameBytes+1> huge{}; assert(!c.publish(pub(150),huge));
 auto s=c.snapshot(); assert(s.publishes==3 && s.hits==3 && s.publish_blocked>=2 && s.mismatch>=2 && s.stale==1 && s.consumed==1);

 // Real SPSC stress: producer is allowed to skip/retry only while the inactive
 // buffer is leased. A lease must never observe a torn frame.
 Cache concurrent; std::atomic<bool> done{false}; std::atomic<unsigned long long> observed{0};
 std::thread producer([&]{
   for(unsigned long long i=1;i<=100000;i++){
     std::array<char,64> frame{}; frame[0]=(char)(i&0xff); frame[1]=(char)((i>>8)&0xff);
     auto publication=pub((long long)i,5000,(long long)i); publication.salt=i;
     while(!concurrent.publish(publication,frame)) std::this_thread::yield();
   }
   done.store(true,std::memory_order_release);
 });
 std::thread consumer([&]{
   Requirement r=req(200000,5000,0,200000); r.require_exact_source_event=0;
   while(!done.load(std::memory_order_acquire) || concurrent.snapshot().published_generation<100000){
     auto x=concurrent.try_acquire(r);
     if(x.reason==AcquireReason::Hit){
       const auto salt=x.lease.publication.salt;
       assert((unsigned char)x.lease.data[0]==(salt&0xff));
       assert((unsigned char)x.lease.data[1]==((salt>>8)&0xff));
       observed.fetch_add(1,std::memory_order_relaxed);
       concurrent.release(x.lease);
     }
   }
 });
 producer.join(); consumer.join(); assert(observed.load()>0);
 return 0;
}
