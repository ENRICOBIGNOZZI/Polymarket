#include "pm/v7_clob_pair_transport.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string_view>
#include <vector>

namespace json = boost::json;
using namespace pm::v7::clob;

namespace {

std::size_t parse_samples(int argc,char**argv) {
    std::size_t n=2'000;
    for(int i=3;i<argc;++i) {
        if(std::string_view(argv[i])!="--samples" || i+1>=argc) return 0;
        const auto t=std::string_view(argv[++i]);
        const auto r=std::from_chars(t.data(),t.data()+t.size(),n);
        if(r.ec!=std::errc{} || r.ptr!=t.data()+t.size()
           || n<100 || n>100'000) return 0;
    }
    return n;
}

std::int64_t q(std::vector<std::int64_t> values,double p) {
    if(values.empty()) return 0;
    std::sort(values.begin(),values.end());
    const auto idx=static_cast<std::size_t>(
        std::llround(std::clamp(p,0.0,1.0)*static_cast<double>(values.size()-1)));
    return values[idx];
}

json::object dist(const std::vector<std::int64_t>& v) {
    return {
        {"p50",q(v,.50)},
        {"p95",q(v,.95)},
        {"p99",q(v,.99)},
        {"p999",q(v,.999)},
        {"max",q(v,1.0)},
    };
}

} // namespace

int main(int argc,char**argv) {
    if(argc<3) return 64;
    const auto samples=parse_samples(argc,argv);
    if(!samples) return 64;
    const auto port=static_cast<std::uint16_t>(
        std::strtoul(argv[1],nullptr,10));
    PairPersistentTlsTransport transport("localhost",port,2'000);
    const auto connected=transport.connect(argv[2]);
    if(!connected.ready || !transport.ready()) return 65;

    constexpr std::string_view yes=
        "POST /order HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: 3\r\nConnection: keep-alive\r\n\r\nyes";
    constexpr std::string_view no=
        "POST /order HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: 2\r\nConnection: keep-alive\r\n\r\nno";
    constexpr std::string_view batch=
        "POST /orders HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: 8\r\nConnection: keep-alive\r\n\r\n[yes,no]";

    std::vector<std::int64_t> pair_completion,wire_skew,ack_skew,batch_completion;
    pair_completion.reserve(samples);
    wire_skew.reserve(samples);
    ack_skew.reserve(samples);
    batch_completion.reserve(samples);

    // Warm persistent TLS sessions and parser paths.
    for(std::size_t i=0;i<32;++i) {
        const auto p=transport.submit_parallel(
            {yes.data(),yes.size()},{no.data(),no.size()});
        const auto b=transport.submit_batch({batch.data(),batch.size()});
        if(!p.both_response_ok || !b.response_ok) return 66;
    }

    for(std::size_t i=0;i<samples;++i) {
        const auto p=transport.submit_parallel(
            {yes.data(),yes.size()},{no.data(),no.size()});
        if(!p.both_response_ok) return 67;
        const auto first_start=std::min(
            p.yes.write_start_monotonic_ns,p.no.write_start_monotonic_ns);
        const auto pair_done=std::max(
            p.yes.ack_complete_monotonic_ns,p.no.ack_complete_monotonic_ns);
        if(first_start<=0 || pair_done<first_start) return 68;
        pair_completion.push_back(pair_done-first_start);
        wire_skew.push_back(p.wire_skew_ns);
        ack_skew.push_back(p.ack_skew_ns);

        const auto b=transport.submit_batch({batch.data(),batch.size()});
        if(!b.response_ok || b.write_start_monotonic_ns<=0
           || b.ack_complete_monotonic_ns<b.write_start_monotonic_ns) return 69;
        batch_completion.push_back(
            b.ack_complete_monotonic_ns-b.write_start_monotonic_ns);
    }
    transport.close();

    std::cout<<json::serialize(json::object{
        {"schema","polymarket_v7_pair_transport_bench_v1"},
        {"paper_only",true},
        {"authenticated_execution",false},
        {"real_order_submission",false},
        {"scope","LOCAL_TLS_TRANSPORT_OVERHEAD_NOT_VENUE_EXECUTION"},
        {"samples",samples},
        {"latency_ns",json::object{
            {"parallel_pair_write_to_both_ack",dist(pair_completion)},
            {"parallel_wire_skew",dist(wire_skew)},
            {"parallel_ack_skew",dist(ack_skew)},
            {"batch_write_to_ack",dist(batch_completion)},
        }},
    })<<'\n';
    return 0;
}
