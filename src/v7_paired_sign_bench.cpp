#include "pm/v7_clob_eip712.hpp"
#include "pm/v7_poly1271.hpp"
#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <thread>
#include <vector>

using namespace pm::v7;
namespace json = boost::json;

namespace {
using Clock=std::chrono::steady_clock;
std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        Clock::now().time_since_epoch()).count();
}

std::int64_t q(std::vector<std::int64_t> values,double p) {
    std::sort(values.begin(),values.end());
    const auto i=static_cast<std::size_t>(
        std::llround(std::clamp(p,0.0,1.0)*static_cast<double>(values.size()-1)));
    return values[i];
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
std::size_t samples(int argc,char**argv) {
    std::size_t n=100'000;
    for(int i=1;i<argc;++i) {
        if(std::string_view(argv[i])!="--samples" || i+1>=argc) return 0;
        const auto t=std::string_view(argv[++i]);
        const auto r=std::from_chars(t.data(),t.data()+t.size(),n);
        if(r.ec!=std::errc{} || r.ptr!=t.data()+t.size()
           || n<1'000 || n>1'000'000) return 0;
    }
    return n;
}

struct Lane {
    clob_eip712::ExchangeV2PreparedOrderHasher order;
    poly1271::PreparedHasher poly;
    explicit Lane(std::string_view token,std::uint8_t side)
        : order({137,"0xE111180000d2663C0091e4f400237545B87B996B"},
                {"0x1111111111111111111111111111111111111111",
                 "0x1111111111111111111111111111111111111111",
                 token,side,3,
                 "0x0000000000000000000000000000000000000000000000000000000000000000",
                 "0x0000000000000000000000000000000000000000000000000000000000000000"}),
          poly(137,"0x1111111111111111111111111111111111111111",
               order.domain_separator()) {}
    bool sign(const poly1271::Secp256k1Signer& signer,
              std::uint64_t salt,std::uint64_t ts,
              std::span<char> out) noexcept {
        return poly1271::sign_prepared_poly1271_hex(
            order,poly,signer,salt,2'500'000,5'000'000,ts,out);
    }
};

struct Job {
    std::uint64_t salt=0;
    std::uint64_t ts=0;
    std::atomic<std::uint64_t> epoch{0};
    std::atomic<std::uint64_t> done_epoch{0};
    std::atomic<std::int64_t> completed_ns{0};
    std::array<char,poly1271::kWrappedSignatureHexChars> output{};
    std::atomic<bool> ok{true};
};

} // namespace

int main(int argc,char**argv) {
    const auto n=samples(argc,argv);
    if(!n) return 64;
    std::array<std::uint8_t,32> key{}; key.back()=1;
    poly1271::Secp256k1Signer signer(key);
    Lane yes_serial("1234",0),no_serial("5678",0);
    Lane yes_parallel("1234",0),no_parallel("5678",0);
    if(!signer.valid()||!yes_serial.order.valid()||!no_serial.order.valid()
       ||!yes_parallel.order.valid()||!no_parallel.order.valid()) return 65;

    std::vector<std::int64_t> serial_pair,serial_skew,parallel_pair,parallel_skew;
    for(auto* v:{&serial_pair,&serial_skew,&parallel_pair,&parallel_skew}) v->reserve(n);

    std::array<char,poly1271::kWrappedSignatureHexChars> y{},z{};
    for(std::size_t i=0;i<4'000;++i) {
        if(!yes_serial.sign(signer,1000+i,1'720'000'000'000ULL+i,y)
           ||!no_serial.sign(signer,2000+i,1'720'000'000'000ULL+i,z)) return 66;
    }

    for(std::size_t i=0;i<n;++i) {
        const auto t0=now_ns();
        if(!yes_serial.sign(signer,10'000+i,1'730'000'000'000ULL+i,y)) return 67;
        const auto ydone=now_ns();
        if(!no_serial.sign(signer,20'000+i,1'730'000'000'000ULL+i,z)) return 68;
        const auto ndone=now_ns();
        serial_pair.push_back(ndone-t0);
        serial_skew.push_back(std::llabs(ndone-ydone));
    }

    Job job{};
    std::atomic<bool> stop{false};
    std::thread worker([&] {
        std::uint64_t seen=0;
        while(!stop.load(std::memory_order_acquire)) {
            const auto e=job.epoch.load(std::memory_order_acquire);
            if(e==0 || e==seen) {
                std::this_thread::yield();
                continue;
            }
            seen=e;
            const bool ok=no_parallel.sign(
                signer,job.salt,job.ts,job.output);
            job.ok.store(ok,std::memory_order_relaxed);
            job.completed_ns.store(now_ns(),std::memory_order_relaxed);
            job.done_epoch.store(e,std::memory_order_release);
        }
    });

    std::array<char,poly1271::kWrappedSignatureHexChars> py{};
    for(std::size_t i=0;i<n;++i) {
        const std::uint64_t epoch=i+1;
        job.salt=40'000+i;
        job.ts=1'740'000'000'000ULL+i;
        job.completed_ns.store(0,std::memory_order_relaxed);
        const auto t0=now_ns();
        job.epoch.store(epoch,std::memory_order_release);
        if(!yes_parallel.sign(signer,30'000+i,job.ts,py)) {
            stop.store(true,std::memory_order_release); worker.join(); return 69;
        }
        const auto ydone=now_ns();
        while(job.done_epoch.load(std::memory_order_acquire)!=epoch) {
            std::this_thread::yield();
        }
        if(!job.ok.load(std::memory_order_relaxed)) {
            stop.store(true,std::memory_order_release); worker.join(); return 70;
        }
        const auto ndone=job.completed_ns.load(std::memory_order_relaxed);
        const auto pair_done=std::max(ydone,ndone);
        parallel_pair.push_back(pair_done-t0);
        parallel_skew.push_back(std::llabs(ndone-ydone));
    }
    stop.store(true,std::memory_order_release);
    worker.join();

    json::object out{
        {"schema","polymarket_v7_paired_sign_bench_v1"},
        {"paper_only",true},
        {"authenticated_execution",false},
        {"real_order_submission",false},
        {"samples",n},
        {"latency_ns",json::object{
            {"serial_pair_completion",dist(serial_pair)},
            {"serial_leg_completion_skew",dist(serial_skew)},
            {"parallel_pair_completion",dist(parallel_pair)},
            {"parallel_leg_completion_skew",dist(parallel_skew)},
        }},
    };
    std::cout << json::serialize(out) << '\n';
    return 0;
}
