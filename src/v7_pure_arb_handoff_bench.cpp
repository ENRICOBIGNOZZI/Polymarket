#include "pm/v7_pure_arb_lane.hpp"
#include "pm/v7_spsc.hpp"
#include <boost/json.hpp>

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <memory>
#include <string_view>
#include <thread>
#include <vector>

using namespace pm::v7;
namespace json = boost::json;

namespace {
std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

struct Event {
    BookHotSnapshot book{};
    std::uint64_t instrument = 0; // 1=yes, 2=no
    std::uint64_t sequence = 0;
    std::int64_t receive_ns = 0;
};
static_assert(std::is_trivially_copyable_v<Event>);

struct Digest {
    std::uint64_t evaluations = 0;
    std::uint64_t positive = 0;
    std::int64_t shares_microunits = 0;
    double pnl = 0.0;
};

BookHotSnapshot base_book(bool yes) {
    BookHotSnapshot b{};
    b.valid = 1;
    b.lineage_continuous = 1;
    b.tick_size_e4 = 100;
    b.best_bid_e4 = yes ? 3900 : 4900;
    b.best_ask_e4 = yes ? 4000 : 5000;
    b.best_bid_microunits = 10'000'000;
    b.best_ask_microunits = 10'000'000;
    b.bid_levels[0] = {b.best_bid_e4, 10'000'000};
    b.ask_levels[0] = {b.best_ask_e4, 10'000'000};
    b.bid_level_count = 1;
    b.ask_level_count = 1;
    return b;
}

void mutate(Event& event, std::size_t i) {
    event.sequence = i + 1;
    event.instrument = (i & 1U) ? 2 : 1;
    event.book = base_book(event.instrument == 1);
    const auto wave = static_cast<std::int64_t>(i % 5);
    event.book.best_ask_microunits = (6 + wave) * 1'000'000;
    event.book.ask_levels[0].quantity_microunits = event.book.best_ask_microunits;
    event.book.best_bid_microunits = (7 + wave) * 1'000'000;
    event.book.bid_levels[0].quantity_microunits = event.book.best_bid_microunits;
    event.book.state_version = i + 1;
}

void evaluate(
    BookHotSnapshot& yes,
    BookHotSnapshot& no,
    const Event& event,
    Digest& digest) noexcept {
    if (event.instrument == 1) yes = event.book;
    else no = event.book;
    if (yes.valid == 0 || no.valid == 0
        || yes.lineage_continuous == 0 || no.lineage_continuous == 0) return;
    const auto result = pure_arb::sweep(
        yes, no, 0.0, 1.0, 0.0005, true);
    ++digest.evaluations;
    if (result.shares_microunits > 0) {
        ++digest.positive;
        digest.shares_microunits += result.shares_microunits;
        digest.pnl += result.gross_locked_pnl;
    }
}

std::int64_t q(std::vector<std::int64_t> values, double p) {
    if (values.empty()) return 0;
    std::sort(values.begin(), values.end());
    const auto idx = static_cast<std::size_t>(
        std::llround(std::clamp(p,0.0,1.0) * static_cast<double>(values.size()-1)));
    return values[idx];
}

json::object dist(const std::vector<std::int64_t>& v) {
    return {
        {"p50", q(v,.50)},
        {"p95", q(v,.95)},
        {"p99", q(v,.99)},
        {"p999", q(v,.999)},
        {"max", q(v,1.0)},
    };
}

std::size_t parse_samples(int argc, char** argv) {
    std::size_t n=100'000;
    for(int i=1;i<argc;++i) {
        if(std::string_view(argv[i])!="--samples" || i+1>=argc) return 0;
        const auto text=std::string_view(argv[++i]);
        const auto r=std::from_chars(text.data(),text.data()+text.size(),n);
        if(r.ec!=std::errc{} || r.ptr!=text.data()+text.size()
            || n<1'000 || n>2'000'000) return 0;
    }
    return n;
}

struct Result {
    Digest digest{};
    std::vector<std::int64_t> latency;
};

Result direct(std::size_t n) {
    Result out; out.latency.reserve(n);
    BookHotSnapshot yes{},no{};
    for(std::size_t i=0;i<n;++i) {
        Event e{}; mutate(e,i); e.receive_ns=now_ns();
        evaluate(yes,no,e,out.digest);
        out.latency.push_back(now_ns()-e.receive_ns);
    }
    return out;
}

Result spsc(std::size_t n) {
    Result out; out.latency.reserve(n);
    auto queue = std::make_unique<SpscRing<Event, 65536>>();
    std::atomic<bool> producer_done{false};
    std::thread consumer([&] {
        BookHotSnapshot yes{},no{};
        Event e{};
        std::size_t consumed=0;
        while(consumed<n) {
            if(!queue->try_pop(e)) {
                if(producer_done.load(std::memory_order_acquire)
                    && queue->approximate_size()==0) break;
                std::this_thread::yield();
                continue;
            }
            evaluate(yes,no,e,out.digest);
            out.latency.push_back(now_ns()-e.receive_ns);
            ++consumed;
        }
    });
    for(std::size_t i=0;i<n;++i) {
        Event e{}; mutate(e,i); e.receive_ns=now_ns();
        while(!queue->try_push(e)) std::this_thread::yield();
    }
    producer_done.store(true,std::memory_order_release);
    consumer.join();
    return out;
}

bool parity(const Digest& a,const Digest& b) {
    return a.evaluations==b.evaluations
        && a.positive==b.positive
        && a.shares_microunits==b.shares_microunits
        && std::abs(a.pnl-b.pnl)<1e-9;
}
} // namespace

int main(int argc,char**argv) {
    const auto n=parse_samples(argc,argv);
    if(n==0) return 64;
    (void)direct(5'000);
    (void)spsc(5'000);
    auto a=direct(n);
    auto b=spsc(n);
    const bool same=parity(a.digest,b.digest)
        && a.latency.size()==n && b.latency.size()==n;
    json::object out{
        {"schema","polymarket_v7_pure_arb_handoff_bench_v1"},
        {"paper_only",true},
        {"authenticated_execution",false},
        {"real_order_submission",false},
        {"scope","SYNTHETIC_SAME_KERNEL_HANDOFF_ONLY_NOT_NETWORK"},
        {"samples",n},
        {"economic_parity",same},
        {"latency_ns",json::object{
            {"direct",dist(a.latency)},
            {"spsc_decision_core",dist(b.latency)},
        }},
        {"digest",json::object{
            {"evaluations",a.digest.evaluations},
            {"positive",a.digest.positive},
            {"shares_microunits",a.digest.shares_microunits},
            {"gross_pnl",a.digest.pnl},
        }},
    };
    std::cout << json::serialize(out) << '\n';
    return same?0:2;
}
