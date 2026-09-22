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
#include <cstdlib>
#include <iostream>
#include <string_view>
#include <thread>
#include <vector>

namespace json = boost::json;
using namespace pm::v7;

namespace {
std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::int64_t q(std::vector<std::int64_t> v,double p){
    std::sort(v.begin(),v.end());
    const auto i=static_cast<std::size_t>(
        std::llround(std::clamp(p,0.0,1.0)*static_cast<double>(v.size()-1)));
    return v[i];
}
json::object dist(const std::vector<std::int64_t>& v){
    return {{"p50",q(v,.50)},{"p95",q(v,.95)},{"p99",q(v,.99)},
            {"p999",q(v,.999)},{"max",q(v,1.0)}};
}
std::size_t samples(int argc,char**argv){
    std::size_t n=50'000;
    if(argc==1) return n;
    if(argc!=3 || std::string_view(argv[1])!="--samples") return 0;
    const auto t=std::string_view(argv[2]);
    const auto r=std::from_chars(t.data(),t.data()+t.size(),n);
    if(r.ec!=std::errc{} || r.ptr!=t.data()+t.size()
       || n<1'000 || n>500'000) return 0;
    return n;
}

struct Job {
    std::uint64_t salt=0;
    std::uint64_t maker=0;
    std::uint64_t taker=0;
    std::uint64_t timestamp=0;
};

class PairWorkers final {
public:
    PairWorkers(clob_eip712::ExchangeV2PreparedOrderHasher& yes_order,
                poly1271::PreparedHasher& yes_poly,
                const poly1271::Secp256k1Signer& yes_signer,
                clob_eip712::ExchangeV2PreparedOrderHasher& no_order,
                poly1271::PreparedHasher& no_poly,
                const poly1271::Secp256k1Signer& no_signer)
        : order_{&yes_order,&no_order}, poly_{&yes_poly,&no_poly},
          signer_{&yes_signer,&no_signer} {
        for(std::size_t i=0;i<2;++i) {
            workers_[i]=std::thread([this,i]{ run(i); });
        }
    }
    ~PairWorkers(){
        stop_.store(true,std::memory_order_release);
        generation_.fetch_add(1,std::memory_order_acq_rel);
        for(auto& t:workers_) if(t.joinable()) t.join();
    }
    bool sign_pair(const Job& yes,const Job& no,
                   std::array<char,poly1271::kWrappedSignatureHexChars>& yes_out,
                   std::array<char,poly1271::kWrappedSignatureHexChars>& no_out,
                   std::int64_t& total_ns,std::int64_t& skew_ns) noexcept {
        jobs_[0]=yes; jobs_[1]=no;
        const auto start=now_ns();
        const auto gen=generation_.fetch_add(1,std::memory_order_release)+1;
        while(done_[0].load(std::memory_order_acquire)!=gen
              || done_[1].load(std::memory_order_acquire)!=gen) {
            std::this_thread::yield();
        }
        if(!ok_[0].load(std::memory_order_relaxed)
           || !ok_[1].load(std::memory_order_relaxed)) return false;
        yes_out=output_[0]; no_out=output_[1];
        const auto d0=finished_[0].load(std::memory_order_relaxed);
        const auto d1=finished_[1].load(std::memory_order_relaxed);
        total_ns=std::max(d0,d1)-start;
        skew_ns=d0>=d1?d0-d1:d1-d0;
        return total_ns>=0 && skew_ns>=0;
    }
private:
    void run(std::size_t i) noexcept {
        std::uint64_t seen=0;
        while(true) {
            const auto gen=generation_.load(std::memory_order_acquire);
            if(gen==seen) {
                if(stop_.load(std::memory_order_acquire)) return;
                std::this_thread::yield();
                continue;
            }
            if(stop_.load(std::memory_order_acquire)) return;
            const auto job=jobs_[i];
            const bool ok=poly1271::sign_prepared_poly1271_hex(
                *order_[i],*poly_[i],*signer_[i],
                job.salt,job.maker,job.taker,job.timestamp,output_[i]);
            finished_[i].store(now_ns(),std::memory_order_relaxed);
            ok_[i].store(ok,std::memory_order_relaxed);
            done_[i].store(gen,std::memory_order_release);
            seen=gen;
        }
    }
    std::array<clob_eip712::ExchangeV2PreparedOrderHasher*,2> order_{};
    std::array<poly1271::PreparedHasher*,2> poly_{};
    std::array<const poly1271::Secp256k1Signer*,2> signer_{};
    std::array<Job,2> jobs_{};
    std::array<std::array<char,poly1271::kWrappedSignatureHexChars>,2> output_{};
    std::array<std::atomic<std::uint64_t>,2> done_{};
    std::array<std::atomic<std::int64_t>,2> finished_{};
    std::array<std::atomic<bool>,2> ok_{};
    std::atomic<std::uint64_t> generation_{0};
    std::atomic<bool> stop_{false};
    std::array<std::thread,2> workers_{};
};
}

int main(int argc,char**argv){
    const auto n=samples(argc,argv);
    if(n==0) return 64;
    constexpr std::string_view deposit=
        "0x1111111111111111111111111111111111111111";
    constexpr std::string_view signer_address=
        "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf";
    constexpr std::string_view exchange=
        "0xE111180000d2663C0091e4f400237545B87B996B";
    constexpr std::string_view metadata=
        "0x0000000000000000000000000000000000000000000000000000000000000000";
    std::array<std::uint8_t,32> key{}; key.back()=1;

    clob_eip712::ExchangeV2PreparedOrderHasher serial_yes(
        {137,exchange},{deposit,deposit,"1234",0,3,metadata,metadata});
    clob_eip712::ExchangeV2PreparedOrderHasher serial_no(
        {137,exchange},{deposit,deposit,"5678",0,3,metadata,metadata});
    poly1271::PreparedHasher serial_yes_poly(
        137,deposit,serial_yes.domain_separator());
    poly1271::PreparedHasher serial_no_poly(
        137,deposit,serial_no.domain_separator());
    poly1271::Secp256k1Signer serial_yes_signer(key),serial_no_signer(key);

    clob_eip712::ExchangeV2PreparedOrderHasher parallel_yes(
        {137,exchange},{deposit,deposit,"1234",0,3,metadata,metadata});
    clob_eip712::ExchangeV2PreparedOrderHasher parallel_no(
        {137,exchange},{deposit,deposit,"5678",0,3,metadata,metadata});
    poly1271::PreparedHasher parallel_yes_poly(
        137,deposit,parallel_yes.domain_separator());
    poly1271::PreparedHasher parallel_no_poly(
        137,deposit,parallel_no.domain_separator());
    poly1271::Secp256k1Signer parallel_yes_signer(key),parallel_no_signer(key);

    if(!serial_yes.valid() || !serial_no.valid()
       || !serial_yes_poly.valid() || !serial_no_poly.valid()
       || !serial_yes_signer.valid() || !serial_no_signer.valid()
       || !parallel_yes.valid() || !parallel_no.valid()
       || !parallel_yes_poly.valid() || !parallel_no_poly.valid()
       || !parallel_yes_signer.valid() || !parallel_no_signer.valid()) return 65;

    PairWorkers workers(parallel_yes,parallel_yes_poly,parallel_yes_signer,
                        parallel_no,parallel_no_poly,parallel_no_signer);
    std::vector<std::int64_t> serial_total,serial_skew,parallel_total,parallel_skew;
    for(auto* v:{&serial_total,&serial_skew,&parallel_total,&parallel_skew}) v->reserve(n);

    for(std::size_t i=0;i<4'000;++i) {
        Job y{1'000+i,2'500'000,5'000'000,1'720'000'000'000ULL+i};
        Job z{10'000+i,2'500'000,5'000'000,1'720'000'000'000ULL+i};
        std::array<char,poly1271::kWrappedSignatureHexChars> a{},b{};
        std::int64_t total=0,skew=0;
        if(!workers.sign_pair(y,z,a,b,total,skew)) return 66;
    }

    for(std::size_t i=0;i<n;++i) {
        const Job yes{100'000+i,2'500'000,5'000'000,1'730'000'000'000ULL+i};
        const Job no{1'000'000+i,2'500'000,5'000'000,1'730'000'000'000ULL+i};
        std::array<char,poly1271::kWrappedSignatureHexChars> sy{},sn{},py{},pn{};

        const auto s0=now_ns();
        if(!poly1271::sign_prepared_poly1271_hex(
               serial_yes,serial_yes_poly,serial_yes_signer,
               yes.salt,yes.maker,yes.taker,yes.timestamp,sy)) return 67;
        const auto s1=now_ns();
        if(!poly1271::sign_prepared_poly1271_hex(
               serial_no,serial_no_poly,serial_no_signer,
               no.salt,no.maker,no.taker,no.timestamp,sn)) return 68;
        const auto s2=now_ns();
        serial_total.push_back(s2-s0);
        serial_skew.push_back(s2-s1);

        std::int64_t pt=0,ps=0;
        if(!workers.sign_pair(yes,no,py,pn,pt,ps)) return 69;
        if(sy!=py || sn!=pn) return 70;
        parallel_total.push_back(pt);
        parallel_skew.push_back(ps);
    }

    const auto serial_p99=q(serial_total,.99);
    const auto parallel_p99=q(parallel_total,.99);
    const auto serial_p999=q(serial_total,.999);
    const auto parallel_p999=q(parallel_total,.999);
    const double improvement=serial_p99>0
        ? 1.0-static_cast<double>(parallel_p99)/static_cast<double>(serial_p99)
        : 0.0;
    const bool candidate=improvement>=0.10 && parallel_p999<=serial_p999;

    json::object out{
        {"schema","polymarket_v7_pure_arb_pair_sign_bench_v1"},
        {"paper_only",true},{"authenticated_execution",false},
        {"real_order_submission",false},
        {"samples",n},{"persistent_workers",true},
        {"independent_secp_contexts",true},
        {"signature_parity",true},
        {"serial_pair_total_ns",dist(serial_total)},
        {"serial_completion_skew_ns",dist(serial_skew)},
        {"parallel_pair_total_ns",dist(parallel_total)},
        {"parallel_completion_skew_ns",dist(parallel_skew)},
        {"p99_improvement_fraction",improvement},
        {"promotion_gate_10pct_p99_no_p999_regression",candidate},
    };
    std::cout<<json::serialize(out)<<'\n';
    return 0;
}
