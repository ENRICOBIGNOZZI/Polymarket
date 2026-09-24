// Synthetic compute/queue pressure, not semantic or economic admission.
// Uses the real native runtime and frozen champion sweep; no execution APIs.
#include "pm/v7_exact_arb_graph_runtime.hpp"
#include <charconv>
#include <chrono>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string_view>
#include <thread>

using namespace pm::v7;
using namespace pm::v7::exact_arb_graph;
using Clock=std::chrono::steady_clock;
thread_local volatile std::int64_t sink=0;

struct Samples {
    std::vector<std::int64_t> values;
    explicit Samples(std::size_t limit) { values.reserve(limit); }
    void add(Clock::time_point start,Clock::time_point end) {
        values.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end-start).count());
    }
    void print() {
        std::sort(values.begin(),values.end());
        auto at=[&](std::size_t numerator,std::size_t denominator) {
            return values.empty() ? 0 : values[(values.size()-1)*numerator/denominator];
        };
        std::cout<<"{\"samples\":"<<values.size()<<",\"p50\":"<<at(1,2)<<",\"p90\":"<<at(9,10)
            <<",\"p99\":"<<at(99,100)<<",\"p99_9\":"<<at(999,1000)<<",\"max\":"<<at(1,1)
            <<",\"tail_sample_ge_10000\":"<<(values.size()>=10000?"true":"false")<<'}';
    }
};

static OwnedGeneration generation(unsigned legs) {
    OwnedGeneration g;
    g.digest.fill(1);g.bundle_digest.fill(2);g.valid_until_monotonic_ns=INT64_MAX/2;
    g.order_share_quantum_microunits=10000;
    for (unsigned token=1;token<kRuntimeBooks;++token) {
        CompiledNode node;node.book_handle=token;node.identity_hash.fill(token);
        g.nodes.push_back(node);g.expected_ticks_e4[token]=1;
    }
    // Four distinct cyclic portfolios per rotation. Every token belongs to
    // exactly 4*legs relations, at most the actual 64-dependency native limit.
    for (unsigned rotation=0;rotation<128;++rotation) for (unsigned variant=0;variant<4;++variant) {
        CompiledRelation r;r.enabled=1;r.proof_handle=1;r.relation_handle=g.relations.size();
        r.leg_count=legs;r.guaranteed_payout_microunits=1000000;r.reserve_per_unit_microunits=500;
        for (unsigned leg=0;leg<legs;++leg) {
            r.legs[leg].book_handle=(rotation+leg+(leg==legs-1?variant:0))%128+1;
            r.legs[leg].coefficient={1,1};r.legs[leg].fee_verified=1;
            r.legs[leg].fee_rate=.02;r.legs[leg].fee_exponent=1;
            r.legs[leg].minimum_order_microunits=1000000;
        }
        g.relations.push_back(r);g.relation_deadlines_ns.push_back(INT64_MAX/2);
    }
    for (unsigned token=1;token<kRuntimeBooks;++token) {
        const auto start=g.relation_handles.size();
        for (const auto& r:g.relations) for (unsigned leg=0;leg<legs;++leg)
            if (r.legs[leg].book_handle==token) g.relation_handles.push_back(r.relation_handle);
        g.dependencies.push_back({token,static_cast<std::uint32_t>(start),static_cast<std::uint16_t>(g.relation_handles.size()-start)});
    }
    if (!g.structurally_valid()) throw std::runtime_error("invalid_pressure_generation");
    return g;
}

struct Workload {
    NativeGraphRuntime runtime;
    std::unique_ptr<std::array<BookDeepSnapshot,kRuntimeBooks>> books=
        std::make_unique<std::array<BookDeepSnapshot,kRuntimeBooks>>();
    // Deliberately stalled diagnostic sink: drop explicitly, never serialize
    // or wait in the measured callback. Not the complete production adapter.
    SpscRing<NativeObservation,64> queue;
    std::uint64_t frames=0,evaluations=0,accepted=0,exhausted=0,points=0,dropped=0;
    unsigned legs,updated;
    std::int64_t now=100;
    Workload(unsigned n,unsigned depth,unsigned changed,bool positive):legs(n),updated(changed) {
        if (!runtime.publish(generation(n)) || !runtime.begin_frame(now,1)) throw std::runtime_error("publish_failed");
        for (unsigned token=1;token<kRuntimeBooks;++token) {
            auto& b=(*books)[token];b.tick_size_e4=1;b.valid=b.lineage_continuous=1;
            b.state_version=1;b.receive_monotonic_ns=now;b.ask_level_count=depth;
            for (unsigned level=0;level<depth;++level)
                b.ask_levels[level]={static_cast<int>((positive?9000:11000)/n+level),1000000};
            if (!runtime.update_book(token,b)) throw std::runtime_error("seed_failed");
        }
        // Seed all cached books but do not hide an expensive evaluation in setup.
        runtime.end_frame({}, {}, [](const auto&,const auto&,auto) noexcept {});
    }
    void frame(Samples* update=nullptr,Samples* evaluate=nullptr,Samples* total=nullptr,
               std::atomic<std::uint64_t>* progress=nullptr) {
        const auto start=Clock::now();
        ++now;
        if (!runtime.begin_frame(now,1)) std::terminate();
        for (unsigned token=1;token<=updated;++token) {
            auto& b=(*books)[token];++b.state_version;b.receive_monotonic_ns=now;
            if (!runtime.update_book(token,b)) std::terminate();
        }
        const auto updated_at=Clock::now();
        std::uint32_t emitted=0,last=0;
        runtime.end_frame({now,INT64_MAX/4,INT64_MAX/4},{},
            [&](const NativeObservation& o,const auto&,auto) noexcept {
                if ((emitted && o.decision.relation_handle<=last) || o.sizing_quantities_evaluated>512) std::terminate();
                last=o.decision.relation_handle;++emitted;++evaluations;
                accepted+=o.decision.reject==HotReject::Accepted;
                exhausted+=o.sizing_search_exhausted;points+=o.sizing_quantities_evaluated;
                if (!queue.try_push(o)) ++dropped;
                if (progress) progress->fetch_add(1,std::memory_order_release);
            });
        const auto done=Clock::now();
        const auto expected=updated==1?4*legs:512;
        if (emitted!=expected) std::terminate();
        ++frames;
        if (update) update->add(start,updated_at);
        if (evaluate) evaluate->add(updated_at,done);
        if (total) total->add(start,done);
    }
    void counts() const {
        std::cout<<"{\"frames\":"<<frames<<",\"relation_evaluations\":"<<evaluations
            <<",\"accepted_recorded_model\":"<<accepted<<",\"search_exhausted\":"<<exhausted
            <<",\"quantities_evaluated\":"<<points<<",\"telemetry_enqueued\":"<<queue.approximate_size()
            <<",\"telemetry_dropped\":"<<dropped<<'}';
    }
};

static void profile(unsigned legs,unsigned depth,unsigned updated,bool positive,unsigned milliseconds,unsigned limit) {
    Workload w(legs,depth,updated,positive);
    Samples update(limit),evaluate(limit),total(limit);
    const auto begin=Clock::now();
    const auto deadline=begin+std::chrono::milliseconds(milliseconds);
    do { w.frame(&update,&evaluate,&total); } while (total.values.size()<limit && Clock::now()<deadline);
    const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-begin).count();
    std::cout<<"{\"legs\":"<<legs<<",\"depth\":"<<depth<<",\"tokens_updated\":"<<updated
        <<",\"relations\":512,\"dependencies_per_token\":"<<4*legs
        <<",\"raw_positive_fixture\":"<<(positive?"true":"false")
        <<",\"wall_elapsed_ns\":"<<elapsed<<",\"update_and_dependency_ns\":";
    update.print();std::cout<<",\"evaluation_and_bounded_sink_ns\":";evaluate.print();
    std::cout<<",\"total_frame_ns\":";total.print();std::cout<<",\"counters\":";w.counts();std::cout<<'}';
}

struct Progress { std::atomic<std::uint64_t> frames{0},evaluations{0}; };

static Samples champion(unsigned milliseconds,unsigned limit,
    const Progress* progress=nullptr,std::uint64_t* overlapping_frames=nullptr,std::uint64_t* overlapping_evaluations=nullptr) {
    // Actual frozen champion sweep, with the same four-level books in all arms.
    auto yes=std::make_unique<BookDeepSnapshot>();auto no=std::make_unique<BookDeepSnapshot>();
    yes->valid=no->valid=yes->lineage_continuous=no->lineage_continuous=1;
    yes->ask_level_count=no->ask_level_count=4;
    for (int j=0;j<4;++j) yes->ask_levels[j]=no->ask_levels[j]={4500+j*10,1000000*(j+1)};
    Samples result(limit);
    const auto initial_frames=progress?progress->frames.load(std::memory_order_acquire):0;
    const auto initial_evaluations=progress?progress->evaluations.load(std::memory_order_acquire):0;
    const auto deadline=Clock::now()+std::chrono::milliseconds(milliseconds);
    std::size_t i=0;
    do {
        yes->ask_levels[0].price_e4=4500+static_cast<int>(i++%2);
        const auto start=Clock::now();
        const auto value=pure_arb::sweep(*yes,*no,.02,1.0,.0005,true);
        sink=value.shares_microunits+std::llround(value.conservative_locked_pnl*1e6);
        result.add(start,Clock::now());
    } while (result.values.size()<limit && Clock::now()<deadline);
    if (progress) {
        *overlapping_frames=progress->frames.load(std::memory_order_acquire)-initial_frames;
        *overlapping_evaluations=progress->evaluations.load(std::memory_order_acquire)-initial_evaluations;
    }
    return result;
}

static unsigned number(const char* raw,unsigned low,unsigned high) {
    unsigned value=0;const std::string_view text(raw);
    const auto parsed=std::from_chars(text.data(),text.data()+text.size(),value);
    if (parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size() || value<low || value>high)
        throw std::runtime_error("invalid_benchmark_bound");
    return value;
}

int main(int argc,char** argv) {
    try {
        if (argc==2 && std::string_view(argv[1])=="--self-test") {
            for (auto n:{2u,3u,4u,8u,16u}) {
                const auto g=generation(n);std::set<std::vector<unsigned>> portfolios;
                for (const auto& r:g.relations) {
                    std::vector<unsigned> handles;
                    for (unsigned i=0;i<n;++i) handles.push_back(r.legs[i].book_handle);
                    std::sort(handles.begin(),handles.end());
                    if (!portfolios.insert(handles).second) throw std::runtime_error("duplicate_pressure_portfolio");
                }
                for (const auto& d:g.dependencies) if (d.relation_count!=4*n) throw std::runtime_error("wrong_pressure_fanout");
            }
            Workload one(16,4,1,true);one.frame();one.frame();
            if (one.evaluations!=128 || one.accepted!=128 || one.dropped!=64) throw std::runtime_error("single_token_pressure");
            Workload all(16,1024,128,false);all.frame();all.frame();
            if (all.evaluations!=1024 || all.accepted || all.points!=1024 || all.dropped!=960)
                throw std::runtime_error("full_depth_nonpositive_pressure");
            return 0;
        }
        if (argc!=1 && argc!=5) throw std::runtime_error("usage: bench [case_ms max_samples pressure_ms pressure_depth]");
        const auto ms=argc==5?number(argv[1],1,30000):100;
        const auto limit=argc==5?number(argv[2],1,1000000):1000000;
        const auto pressure_ms=argc==5?number(argv[3],1,30000):1000;
        const auto pressure_depth=argc==5?number(argv[4],1,1024):64;
        std::cout<<"{\"schema\":\"polymarket_v7_native_exact_arb_pressure_benchmark_v1\",\"paper_only\":true,"
            "\"authenticated_execution\":false,\"real_order_submission\":false,\"real_capital_at_risk\":false,"
            "\"automatic_promotion\":false,\"execution_authority\":false,\"units\":\"nanoseconds\","
            "\"fixture_semantics_verified\":false,\"cpu_affinity_verified\":false,\"london_non_regression_verified\":false,"
            "\"quantile_definition\":\"sorted[floor(p*(n-1))]; no statistical tail guarantee\","
            "\"timed_io\":false,\"scope\":\"NATIVE_KERNEL_AND_STALLED_DIAGNOSTIC_SINK_NOT_DECODE_FULL_EVIDENCE_WRITER_OR_NETWORK\","
            "\"case_ms\":"<<ms<<",\"max_samples\":"<<limit<<",\"profiles\":[";
        bool first=true;
        for (auto legs:{2u,3u,4u,8u,16u}) for (auto depth:{4u,64u,1024u}) for (auto updated:{1u,128u}) {
            if (!first) std::cout<<',';first=false;
            profile(legs,depth,updated,true,ms,limit);
        }
        std::cout<<',';profile(16,1024,128,false,ms,limit);
        auto before=champion(pressure_ms,limit);
        auto graph=std::make_unique<Workload>(16,pressure_depth,128,true);
        std::atomic<bool> ready{false},start{false},stop{false};
        Progress progress;
        std::thread worker([&] {
            ready.store(true,std::memory_order_release);
            while (!start.load(std::memory_order_acquire)) std::this_thread::yield();
            while (!stop.load(std::memory_order_acquire)) {
                graph->frame(nullptr,nullptr,nullptr,&progress.evaluations);
                progress.frames.fetch_add(1,std::memory_order_release);
            }
        });
        while (!ready.load(std::memory_order_acquire)) std::this_thread::yield();
        start.store(true,std::memory_order_release);
        Samples during(0);std::uint64_t during_frames=0,during_evaluations=0;
        try { during=champion(pressure_ms,limit,&progress,&during_frames,&during_evaluations); }
        catch (...) { stop.store(true,std::memory_order_release);worker.join();throw; }
        stop.store(true,std::memory_order_release);worker.join();
        auto after=champion(pressure_ms,limit);
        std::cout<<"],\"champion_same_host_pressure\":{\"champion_depth\":4,\"graph_depth\":"<<pressure_depth
            <<",\"duration_budget_ms_per_arm\":"<<pressure_ms<<",\"graph_frames_completed_during_sampling\":"<<during_frames
            <<",\"graph_relation_evaluations_completed_during_sampling\":"<<during_evaluations
            <<",\"before_ns\":";before.print();std::cout<<",\"during_ns\":";during.print();
        std::cout<<",\"after_ns\":";after.print();std::cout<<",\"graph_counters_after_join\":";graph->counts();
        std::cout<<"},\"budget_scope\":\"SOFT_DURATION_PER_CASE_CHECKED_AFTER_EACH_BOUNDED_FRAME; NO_ABSOLUTE_WORST_CASE_PROOF\"}\n";
        return 0;
    } catch (const std::exception& e) { std::cerr<<e.what()<<'\n';return 2; }
}
