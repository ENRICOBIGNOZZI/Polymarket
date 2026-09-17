#include "pm/v7_prepared_order_frame.hpp"
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <vector>
using namespace pm::v7::prepared_order;
static long long q(std::vector<long long> v,double p){std::sort(v.begin(),v.end());return v[(size_t)(p*(v.size()-1))];}
int main(){constexpr int N=500000; Cache c; Key k; k.instrument_handle=17;k.price_e4=5000;k.tick_size_e4=100;k.quantity_microunits=5000000;k.side=0;k.time_in_force=1; std::array<char,1150> frame{}; for(size_t i=0;i<frame.size();i++) frame[i]=(char)(i&127); std::vector<long long> v;v.reserve(N); for(int i=1;i<=N;i++){Publication p; p.key=k;p.source_exchange_event_ns=i;p.prepared_monotonic_ns=i*100;p.order_timestamp_ms=1710000000000ULL+i;p.salt=42+i; if(!c.publish(p,frame)) return 2; Requirement r; r.key=k;r.source_exchange_event_ns=i;r.now_monotonic_ns=i*100+1;r.maximum_age_ns=1000; auto t0=std::chrono::steady_clock::now(); auto a=c.try_acquire(r); if(a.reason!=AcquireReason::Hit) return 3; volatile char x=a.lease.data[0]; (void)x; c.release(a.lease); auto t1=std::chrono::steady_clock::now(); v.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count()); } std::printf("samples=%d p50=%lldns p95=%lldns p99=%lldns p999=%lldns max=%lldns\n",N,q(v,.5),q(v,.95),q(v,.99),q(v,.999),*std::max_element(v.begin(),v.end()));}
