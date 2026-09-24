#include "pm/v7_exact_arb_graph_runtime.hpp"

#include <cassert>
#include <cstdlib>
#include <new>
#include <thread>

// Count this thread's allocations, including over-aligned queues. The control
// thread remains free to allocate while the feed's counter must stay unchanged.
thread_local std::size_t test_allocations = 0;
thread_local std::size_t test_deallocations = 0;
void* operator new(std::size_t size) {
    ++test_allocations;
    if (auto* p = std::malloc(size ? size : 1)) return p;
    throw std::bad_alloc();
}
void* operator new[](std::size_t size) { return ::operator new(size); }
void operator delete(void* p) noexcept { ++test_deallocations; std::free(p); }
void operator delete[](void* p) noexcept { ::operator delete(p); }
void operator delete(void* p, std::size_t) noexcept { ::operator delete(p); }
void operator delete[](void* p, std::size_t) noexcept { ::operator delete(p); }
void* operator new(std::size_t size, std::align_val_t alignment) {
    ++test_allocations;
    void* p = nullptr;
    if (posix_memalign(&p, static_cast<std::size_t>(alignment), size ? size : 1) == 0) return p;
    throw std::bad_alloc();
}
void* operator new[](std::size_t size, std::align_val_t alignment) { return ::operator new(size, alignment); }
void operator delete(void* p, std::align_val_t) noexcept { ::operator delete(p); }
void operator delete[](void* p, std::align_val_t) noexcept { ::operator delete(p); }
void operator delete(void* p, std::size_t, std::align_val_t) noexcept { ::operator delete(p); }
void operator delete[](void* p, std::size_t, std::align_val_t) noexcept { ::operator delete(p); }

using namespace pm::v7;
using namespace pm::v7::exact_arb_graph;

OwnedGeneration generation(std::uint8_t tag = 1) {
    OwnedGeneration g;
    g.digest.fill(tag);
    g.bundle_digest.fill(tag);
    g.valid_until_monotonic_ns = 1000000;
    g.order_share_quantum_microunits = 10000;
    CompiledRelation r;
    r.enabled = 1;
    r.proof_handle = 99;
    r.leg_count = 2;
    r.guaranteed_payout_microunits = 1000000;
    r.reserve_per_unit_microunits = 500;
    for (unsigned i = 0; i < 2; ++i) {
        CompiledNode n;
        n.book_handle = i + 1;
        n.identity_hash.fill(i + 1);
        g.nodes.push_back(n);
        g.expected_ticks_e4[i + 1] = 100;
        r.legs[i].book_handle = i + 1;
        r.legs[i].coefficient = {1, 1};
        r.legs[i].fee_verified = 1;
        r.legs[i].minimum_order_microunits = 1000000;
        g.dependencies.push_back({i+1, i, 1});
        g.relation_handles.push_back(0);
    }
    g.relations.push_back(r);
    g.relation_deadlines_ns.push_back(1000000);
    return g;
}

BookDeepSnapshot book(std::int64_t now = 100, std::uint64_t version = 1, int price = 4000) {
    BookDeepSnapshot b;
    b.valid = b.lineage_continuous = 1;
    b.state_version = version;
    b.receive_monotonic_ns = now;
    b.ask_level_count = b.bid_level_count = 1;
    b.ask_levels[0] = {price, 5000000};
    b.bid_levels[0] = {price-100, 5000000};
    return b;
}

int main() {
    auto runtime = std::make_unique<NativeGraphRuntime>();
    assert(!runtime->publish({}));
    auto g = generation();
    assert(g.structurally_valid());
    auto malformed = g;
    malformed.relation_handles[1] = 1;
    assert(!runtime->publish(malformed));
    malformed = g;
    malformed.dependencies[1].token_handle = 1;
    assert(!runtime->publish(malformed));
    malformed = g;
    malformed.relations[0].legs[1].book_handle = 1;
    assert(!runtime->publish(malformed));
    malformed = g;
    malformed.relation_handles.push_back(0);
    assert(!runtime->publish(malformed));
    malformed = g;
    malformed.relations[0].legs[0].fee_rate = std::numeric_limits<double>::quiet_NaN();
    assert(!runtime->publish(malformed));
    assert(runtime->publish(g));
    HotResources resources;
    resources.capital_microunits = 1000000000;
    std::vector<NativeObservation> observations;
    auto emit = [&](const auto& o, const auto&, auto) noexcept { observations.push_back(o); };
    // Tests may allocate in the callback; a production sink must be bounded.
    observations.reserve(100);
    const auto allocated = test_allocations;
    assert(runtime->begin_frame(100, 1));
    assert(runtime->update_book(1, book()));
    assert(runtime->update_book(2, book()));
    runtime->end_frame({100, 1000, 50}, resources, emit);
    assert(observations.size() == 1);
    assert(observations.back().decision.reject == HotReject::Accepted);
    assert(observations.back().leg_versions[0] == 1);
    assert(observations.back().evaluation_valid_until_monotonic_ns == 1100);
    const auto initial_continuity = observations.back().continuity_serial;
    assert(initial_continuity > 0);
    assert(test_allocations == allocated);
    runtime->end_frame({100, 1000, 50}, resources, emit);
    assert(observations.size() == 1); // frame is single-use

    assert(runtime->begin_frame(110, 1));
    assert(runtime->update_book(1, book()));
    assert(runtime->update_book(2, book()));
    runtime->end_frame({110, 1000, 50}, resources, emit);
    assert(observations.size() == 1); // repeated versions do not invent events

    // Evaluate only AFTER both legs of a frame. The transient 0.2+0.4 basket
    // must not appear: final prices are 0.2+0.9, i.e. no opportunity.
    assert(runtime->begin_frame(120, 1));
    assert(runtime->update_book(1, book(120, 2, 2000)));
    assert(runtime->update_book(2, book(120, 2, 9000)));
    runtime->end_frame({120, 1000, 50}, resources, emit);
    assert(observations.size() == 2);
    assert(observations.back().decision.reject == HotReject::NoPositiveEdge);
    runtime->reset_lineage();
    assert(runtime->begin_frame(125, 1));
    assert(runtime->update_book(1, book(125, 3, 4000)));
    assert(runtime->update_book(2, book(125, 3, 4000)));
    runtime->end_frame({125, 1000, 50}, resources, emit);
    assert(observations.back().continuity_serial > initial_continuity);

    // A new generation with the same claims can retain causal books. Remapping
    // a handle to a different claim MUST clear it, even if version numbers match.
    assert(runtime->publish(generation(2)));
    assert(runtime->begin_frame(130, 1));
    assert(runtime->update_book(2, book(130, 4, 4000)));
    runtime->end_frame({130, 1000, 50}, resources, emit);
    assert(observations.back().graph_generation[0] == 2);
    assert(observations.back().continuity_serial > initial_continuity);
    assert(observations.back().decision.reject == HotReject::Accepted);
    auto changed = generation(3);
    changed.nodes[0].identity_hash.fill(99);
    assert(runtime->publish(changed));
    assert(runtime->begin_frame(140, 1));
    assert(runtime->update_book(2, book(140, 5)));
    runtime->end_frame({140, 1000, 50}, resources, emit);
    assert(observations.back().decision.reject != HotReject::Accepted);

    // Queued old success is cancelled by a failure. A later validated success
    // may recover, but never the old cached generation.
    assert(runtime->publish(generation(4)));
    runtime->invalidate();
    assert(!runtime->begin_frame(150, 1));
    assert(runtime->publish(generation(5)));
    assert(runtime->begin_frame(160, 2));
    assert(runtime->update_book(1, book(160, 1)));
    runtime->end_frame({160, 1000, 50}, resources, emit);
    assert(observations.back().decision.reject != HotReject::Accepted); // new epoch lacks other leg
    assert(runtime->begin_frame(170, 2));
    assert(!runtime->update_book(2, book(180, 1))); // future snapshot
    runtime->end_frame({170, 1000, 50}, resources, emit);
    assert(observations.back().decision.reject != HotReject::Accepted);
    const auto before = observations.size();
    assert(!runtime->begin_frame(1000000, 2)); // exact lease boundary
    runtime->end_frame({1000000, 1000, 50}, resources, emit);
    assert(observations.size() == before);

    // A clock inversion remains blocked until the prior receive watermark is
    // reached, and must not make a recently expired generation fresh again.
    assert(!runtime->begin_frame(500, 2));
    assert(!runtime->begin_frame(600, 2));

    // Independent relations are not scanned/evaluated on unrelated token input.
    runtime = std::make_unique<NativeGraphRuntime>();
    auto disconnected = generation();
    disconnected.control_admission_sequence = 7;
    auto second = disconnected.relations[0];
    second.relation_handle = 1;
    for (unsigned i = 0; i < 2; ++i) {
        auto n = disconnected.nodes[i];
        n.book_handle += 2;
        n.identity_hash.fill(i + 3);
        disconnected.nodes.push_back(n);
        disconnected.expected_ticks_e4[n.book_handle] = 100;
        second.legs[i].book_handle += 2;
        disconnected.dependencies.push_back({i + 3, i + 2, 1});
        disconnected.relation_handles.push_back(1);
    }
    disconnected.relations.push_back(second);
    disconnected.relation_deadlines_ns.push_back(1000000);
    assert(runtime->publish(disconnected));
    assert(runtime->begin_frame(100, 1));
    assert(runtime->update_book(1, book()));
    assert(runtime->update_book(2, book()));
    unsigned evaluated = 0;
    runtime->end_frame({100, 1000, 50}, resources,
        [&](const auto& o, const auto&, auto) noexcept {
            ++evaluated;
            assert(o.decision.relation_handle == 0);
        });
    assert(evaluated == 1);

    // Invalidation during a multi-relation frame must suppress subsequent
    // emissions, not wait until the next market frame to notice the revocation.
    assert(runtime->begin_frame(110, 1));
    for (unsigned h=1;h<=4;++h) assert(runtime->update_book(h,book(110,2)));
    evaluated=0;
    runtime->end_frame({110,1000,50},resources,[&](const auto& o,const auto&,auto) noexcept {
        ++evaluated;
        assert(o.control_admission_sequence==7);
        runtime->invalidate();
    });
    assert(evaluated==1);
    assert(!runtime->begin_frame(120,1));

    auto admission_later=generation();
    admission_later.control_admitted_monotonic_ns=150;
    assert(runtime->publish(admission_later));
    assert(!runtime->begin_frame(149,1));
    assert(runtime->begin_frame(150,1));

    // Control/feed ownership stress: generation data must never mix or become
    // dangling while publications supersede pending work and slots are reused.
    runtime = std::make_unique<NativeGraphRuntime>();
    std::atomic<bool> finished{false};
    std::atomic<unsigned> accepted{0};
    std::thread feed([&] {
        std::uint64_t frame = 1;
        while (!finished.load(std::memory_order_acquire)) {
            const auto allocations_before = test_allocations;
            const auto deallocations_before = test_deallocations;
            const bool ready = runtime->begin_frame(100, 1);
            assert(test_allocations == allocations_before);
            assert(test_deallocations == deallocations_before);
            if (!ready) continue;
            assert(runtime->update_book(1, book(100, frame)));
            assert(runtime->update_book(2, book(100, frame)));
            runtime->end_frame({100, 1000, 50}, resources,
                [&](const auto& o, const auto& r, auto) noexcept {
                    const auto tag = o.graph_generation[0];
                    for (auto byte : o.graph_generation) assert(byte == tag);
                    for (auto byte : o.bundle_digest) assert(byte == tag);
                    assert(r.proof_handle == tag);
                    assert(o.proof_handle == tag);
                    assert(o.control_admission_sequence == tag);
                    assert(o.leg_versions[0] == o.leg_versions[1]);
                    assert(o.decision.reject == HotReject::Accepted);
                    accepted.fetch_add(1, std::memory_order_relaxed);
                });
            assert(test_allocations == allocations_before);
            assert(test_deallocations == deallocations_before);
            ++frame;
        }
    });
    for (unsigned i = 1; i < 20000; ++i) {
        auto next = generation(static_cast<std::uint8_t>(i % 255 + 1));
        next.relations[0].proof_handle = next.digest[0];
        next.control_admission_sequence = next.digest[0];
        while (!runtime->publish(next)) std::this_thread::yield();
        if (i % 7 == 0) runtime->invalidate();
    }
    // Ensure the stress actually exercised the concurrent feed, not merely the
    // publisher on an unlucky scheduling run.
    auto last = generation(42);
    last.relations[0].proof_handle = 42;
    last.control_admission_sequence = 42;
    while (!runtime->publish(last)) std::this_thread::yield();
    while (!accepted.load(std::memory_order_relaxed)) std::this_thread::yield();
    finished.store(true, std::memory_order_release);
    feed.join();
    runtime->collect_retired();
}
