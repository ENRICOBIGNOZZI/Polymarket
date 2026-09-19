// Synthetic local-compute comparison only. No feed, model calibration,
// order submission, network latency, execution realism or profit is measured.
// All allocations are outside the timed candidate/cache path.
#include "pm/v7_crypto_decision_lane.hpp"
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <vector>
using namespace pm::v7;
std::int64_t clock_ns() { return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count(); }
struct Result {std::int64_t p50,p99,max; std::uint64_t accepted;};
Result run(bool with_context) {
    constexpr std::int64_t now=1'000'000'000'000LL;
    constexpr std::size_t n=100'000;
    NativeCryptoDecisionPolicy policy;policy.required_slow_context_mask=with_context ? 1 : 0;
    NativeCryptoDecisionLane lane(policy);
    NativeCryptoDecisionInput in;in.now_monotonic_ns=now;
    in.signal.trigger_receive_monotonic_ns=now-10'000'000;
    in.signal.valid_until_monotonic_ns=now+90'000'000;
    in.signal.binance_return_100ms_bp=.5;in.signal.direction=1;
    in.signal.confirmed_non_opposing=1;in.signal.valid=1;
    in.market.market_handle=7;in.market.event_handle=8;
    in.market.close_monotonic_ns=now+110'000'000'000LL;
    in.market.yes={11,1'000'000,1,{}};in.market.no={12,1'000'000,0,{}};
    in.market.accepting_orders=1;in.market.contract_verified=1;in.market.settlement_reference_valid=1;
    in.yes_book.state_version=9;in.yes_book.receive_monotonic_ns=now-1'000'000;
    in.yes_book.tick_size_e4=100;in.yes_book.best_ask_e4=4000;
    in.yes_book.best_ask_microunits=10'000'000;in.yes_book.lineage_continuous=1;in.yes_book.valid=1;
    SlowContextCache cache;SlowContextMailbox box;SlowContextSnapshot snapshot;
    snapshot.version=1;snapshot.published_ns=now-1;snapshot.valid_mask=kSlowContextMask;snapshot.envelope_valid=1;
    for(auto& field:snapshot.fields)field={.5,now-1000,now+1'000'000'000,1};
    if(!cache.apply(snapshot,now))std::terminate();
    std::vector<std::int64_t> samples;samples.reserve(n);std::uint64_t accepted=0;
    for(std::size_t i=0;i<n+1000;++i){
        in.signal.signal_version=i+1;
        if(with_context && i%1024==0){++snapshot.version;if(!box.publish(snapshot))std::terminate();}
        const auto start=clock_ns();
        if(with_context){box.consume(cache,now);in.slow_context=cache.at(now);}
        const auto result=lane.construct_candidate(in);
        const auto elapsed=clock_ns()-start;
        if(i>=1000){samples.push_back(elapsed);accepted+=result.accepted;}
    }
    std::sort(samples.begin(),samples.end());
    return {samples[n/2],samples[n*99/100],samples.back(),accepted};
}
int main(){
    const auto baseline=run(false),context=run(true);
    std::cout<<"{\"schema\":\"polymarket_v7_multirate_compute_benchmark_v1\","
      "\"scope\":\"SYNTHETIC_LOCAL_COMPUTE_NOT_NETWORK_OR_PROFIT\",\"samples_per_path\":100000,"
      "\"baseline\":{\"p50_ns\":"<<baseline.p50<<",\"p99_ns\":"<<baseline.p99<<",\"max_ns\":"<<baseline.max<<",\"accepted\":"<<baseline.accepted<<"},"
      "\"with_context\":{\"p50_ns\":"<<context.p50<<",\"p99_ns\":"<<context.p99<<",\"max_ns\":"<<context.max<<",\"accepted\":"<<context.accepted<<"}}\n";
    return baseline.accepted==100000 && context.accepted==100000 ? 0 : 2;
}
