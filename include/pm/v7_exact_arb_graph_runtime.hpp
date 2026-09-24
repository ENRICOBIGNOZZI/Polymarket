#pragma once

// Zero-authority native research kernel. Control-plane compilation/validation
// and storage reclamation never run on the feed thread. The caller must join
// BOTH threads before destroying this object. No order/execution API exists.
#include "pm/v7_exact_arb_graph_hotpath.hpp"
#include "pm/v7_exact_arb_order_sizing.hpp"
#include "pm/v7_exact_arb_diagnostics.hpp"
#include "pm/v7_spsc.hpp"

#include <atomic>
#include <memory>
#include <utility>
#include <vector>

namespace pm::v7::exact_arb_graph {

inline constexpr std::size_t kRuntimeBooks = 129; // decoder handles 1..128
inline constexpr std::size_t kRuntimeRelations = 512;

struct OwnedGeneration {
    std::array<std::uint8_t, 32> digest{};
    // The same full graph can have different hotsets/handle assignments. Both
    // identities are necessary to reconstruct a local relation handle.
    std::array<std::uint8_t, 32> bundle_digest{};
    std::array<std::uint8_t, 32> selection_digest{};
    std::int64_t source_valid_until_wall_ms = 0;
    std::int64_t order_share_quantum_microunits = 0;
    // Converted from a verified bounded wall-clock lease OFF PATH. A repeated
    // read of the same file must not extend the original source expiration.
    std::int64_t valid_until_monotonic_ns = 0;
    std::vector<CompiledNode> nodes;
    std::vector<CompiledRelation> relations;
    std::vector<TokenDependency> dependencies;
    std::vector<std::uint32_t> relation_handles;
    // Per-direction venue/settlement deadline; zero is not an unlimited lease.
    std::vector<std::int64_t> relation_deadlines_ns;
    std::array<std::int32_t, kRuntimeBooks> expected_ticks_e4{};
    std::uint64_t admission_serial = 0; // overwritten by publisher, not input authority
    std::uint64_t control_admission_sequence = 0; // producer control journal, not semantic authority
    std::int64_t control_admitted_monotonic_ns = 0;

    // Structural admission only. This does NOT establish settlement semantics:
    // a proof-verifying compiler/loader must supply this immutable generation.
    [[nodiscard]] bool structurally_valid() const noexcept {
        if (valid_until_monotonic_ns <= 0 || order_share_quantum_microunits <= 0 || nodes.size() >= kRuntimeBooks
            || relations.size() > kRuntimeRelations
            || dependencies.size() != nodes.size()
            || relation_deadlines_ns.size() != relations.size()
            || relation_handles.size() > kRuntimeBooks * kMaxDependenciesPerToken
            || std::all_of(digest.begin(), digest.end(), [](auto x) { return x == 0; })
            || std::all_of(bundle_digest.begin(), bundle_digest.end(), [](auto x) { return x == 0; })) return false;
        std::array<bool, kRuntimeBooks> registered{};
        for (std::size_t i = 0; i < nodes.size(); ++i) {
            const auto& n = nodes[i];
            if (n.book_handle == 0 || n.book_handle >= kRuntimeBooks || registered[n.book_handle]
                || std::all_of(n.identity_hash.begin(), n.identity_hash.end(), [](auto x) { return x == 0; })) return false;
            if (expected_ticks_e4[n.book_handle] <= 0 || expected_ticks_e4[n.book_handle] >= 10000) return false;
            for (std::size_t j = 0; j < i; ++j) if (nodes[j].identity_hash == n.identity_hash) return false;
            registered[n.book_handle] = true;
        }
        std::array<std::uint16_t, kRuntimeRelations> occurrences{};
        std::size_t offset = 0;
        for (std::size_t i = 0; i < dependencies.size(); ++i) {
            const auto& d = dependencies[i];
            if (d.token_handle >= kRuntimeBooks || !registered[d.token_handle]
                || (i && d.token_handle <= dependencies[i-1].token_handle)
                || d.first_relation != offset || d.relation_count > kMaxDependenciesPerToken
                || offset + d.relation_count > relation_handles.size()) return false;
            std::array<bool, kRuntimeRelations> seen{};
            for (std::size_t j = 0; j < d.relation_count; ++j) {
                const auto h = relation_handles[offset + j];
                if (h >= relations.size() || seen[h]) return false;
                seen[h] = true;
                const auto& r = relations[h];
                if (r.leg_count > kMaxLegs) return false;
                bool member = false;
                for (std::size_t leg = 0; leg < r.leg_count; ++leg)
                    member |= r.legs[leg].book_handle == d.token_handle;
                if (!member) return false;
                ++occurrences[h];
            }
            offset += d.relation_count;
        }
        if (offset != relation_handles.size()) return false;
        for (std::size_t h = 0; h < relations.size(); ++h) {
            const auto& r = relations[h];
            if (r.enabled != 1 || r.sell_inventory > 1 || r.relation_handle != h
                || r.leg_count == 0 || r.leg_count > kMaxLegs
                || occurrences[h] != r.leg_count || r.guaranteed_payout_microunits <= 0
                || r.reserve_per_unit_microunits < 0 || relation_deadlines_ns[h] <= 0) return false;
            std::array<bool, kRuntimeBooks> seen{};
            for (std::size_t i = 0; i < r.leg_count; ++i) {
                const auto& l = r.legs[i];
                if (l.book_handle >= kRuntimeBooks || !registered[l.book_handle] || seen[l.book_handle]
                    || !l.coefficient.valid() || l.minimum_order_microunits < 0 || l.fee_verified != 1
                    || !std::isfinite(l.fee_rate) || l.fee_rate < 0 || l.fee_rate > 1
                    || !std::isfinite(l.fee_exponent) || l.fee_exponent < 0 || l.fee_exponent > 2
                    || std::floor(l.fee_exponent) != l.fee_exponent) return false;
                seen[l.book_handle] = true;
            }
        }
        return true;
    }
};

struct NativeObservation {
    std::array<std::uint8_t, 32> graph_generation{};
    std::array<std::uint8_t, 32> bundle_digest{};
    std::array<std::uint8_t, 32> selection_digest{};
    std::int64_t source_valid_until_wall_ms = 0;
    std::int64_t source_valid_until_monotonic_ns = 0;
    std::uint64_t connection_epoch = 0;
    std::uint64_t frame_sequence = 0;
    std::uint64_t continuity_serial = 0;
    std::uint64_t control_admission_sequence = 0;
    std::int64_t evaluation_valid_until_monotonic_ns = 0;
    std::int64_t relation_valid_until_monotonic_ns = 0;
    std::int64_t decision_monotonic_ns = 0;
    std::uint64_t proof_handle = 0;
    HotDecision decision{};
    std::int64_t unconstrained_quantity_microunits = 0;
    std::int64_t relation_quantum_microunits = 0;
    std::uint8_t quantity_precision_ready = 0;
    std::uint8_t global_size_optimum_proven = 0;
    std::uint8_t sizing_search_exhausted = 0;
    std::uint32_t sizing_quantities_evaluated = 0;
    std::int64_t sizing_net_upper_bound_microunits = 0;
    std::uint8_t sizing_net_upper_bound_valid = 0;
    NearArbDiagnostic near_arb{};
    std::array<std::uint64_t, kMaxLegs> leg_versions{};
};

// Three exclusive owners: control, pending mailbox, feed. Only control frees
// backing vectors. Atomic exchange transfers ownership, never a borrowed view.
// At most three slots can be retired, so the four-entry return queue cannot
// fill. Superseded pending generations are reclaimed by control, not feed.
class NativeGraphRuntime final {
    static_assert(std::atomic<OwnedGeneration*>::is_always_lock_free);
    static_assert(std::atomic<std::uint64_t>::is_always_lock_free);
public:
    NativeGraphRuntime() : books_(std::make_unique<std::array<BookDeepSnapshot, kRuntimeBooks>>()),
        sizing_workspace_(std::make_unique<order_sizing_detail::DepthWorkspace>()) {}
    NativeGraphRuntime(const NativeGraphRuntime&) = delete;
    NativeGraphRuntime& operator=(const NativeGraphRuntime&) = delete;

    // CONTROL THREAD ONLY. false means invalid input or transient slot pressure;
    // caller must invalidate explicitly after a source failure, not keep retrying
    // an old success. Validation/allocations/destruction are confined here.
    [[nodiscard]] bool publish(OwnedGeneration generation) {
        if (!generation.structurally_valid()) return false;
        generation.admission_serial = invalidation_.load(std::memory_order_acquire);
        collect_retired();
        for (std::size_t i = 0; i < slots_.size(); ++i) {
            if (occupied_[i]) continue;
            slots_[i] = std::move(generation);
            occupied_[i] = true;
            auto* superseded = pending_.exchange(&slots_[i], std::memory_order_acq_rel);
            if (superseded) release(superseded);
            return true;
        }
        return false;
    }

    // Invalidating must also cancel a queued success. The atomic serial prevents
    // a feed/control race from reviving that success at the next frame boundary.
    void invalidate() noexcept {
        invalidation_.fetch_add(1, std::memory_order_release);
        auto* superseded = pending_.exchange(nullptr, std::memory_order_acq_rel);
        if (superseded) release(superseded);
    }

    void collect_retired() noexcept {
        OwnedGeneration* old = nullptr;
        while (retired_.try_pop(old)) release(old);
    }

    // FEED ONLY: a corrupt frame invalidates cached lineage even without a
    // transport reconnect. The control-plane generation remains separately pinned.
    void reset_lineage() noexcept { clear_books(); ready_ = false; }

    // FEED THREAD ONLY. Decisions are made at frame END, after every changed
    // token has received the final decoded frame state. A two-leg frame cannot
    // emit an intermediate opportunity using one old leg and one new leg.
    [[nodiscard]] bool begin_frame(std::int64_t now_ns, std::uint64_t epoch) noexcept {
        for (std::size_t i = 0; i < affected_count_; ++i) touched_[affected_[i]] = false;
        affected_count_ = 0;
        const auto serial = invalidation_.load(std::memory_order_acquire);
        if (serial != observed_invalidation_) {
            ++continuity_serial_;
            retire_active();
            observed_invalidation_ = serial;
        }
        if (auto* next = pending_.exchange(nullptr, std::memory_order_acq_rel)) {
            ++continuity_serial_;
            // Retain a book only if the canonical claim at that handle is
            // unchanged. This scan happens only at generation activation.
            std::array<std::array<std::uint8_t, 32>, kRuntimeBooks> identities{};
            for (const auto& node : next->nodes) identities[node.book_handle] = node.identity_hash;
            for (std::size_t h = 0; h < kRuntimeBooks; ++h) {
                if (identities[h] != identities_[h]
                    || (active_ && next->expected_ticks_e4[h] != active_->expected_ticks_e4[h])) (*books_)[h] = {};
            }
            identities_ = identities;
            retire_active();
            active_ = next;
        }
        if (serial != invalidation_.load(std::memory_order_acquire)
            || (active_ && active_->admission_serial != serial)) retire_active();
        if (epoch == 0 || epoch != epoch_ || now_ns < now_ns_) clear_books();
        const bool clock_ok = now_ns > 0 && now_ns >= now_ns_;
        now_ns_ = std::max(now_ns_, now_ns); // a bad frame cannot move the causal watermark back
        epoch_ = epoch;
        ++frame_sequence_;
        ready_ = clock_ok && epoch != 0 && active_ && now_ns < active_->valid_until_monotonic_ns
            && now_ns >= active_->control_admitted_monotonic_ns;
        return ready_;
    }

    // Snapshot must be the final state of this frame, never a REST screen. The
    // owning decoder enforces snapshot/delta reconciliation and venue lineage.
    [[nodiscard]] bool update_book(std::uint32_t h, const BookDeepSnapshot& book) noexcept {
        if (!ready_ || h >= kRuntimeBooks
            || std::all_of(identities_[h].begin(), identities_[h].end(), [](auto x) { return x == 0; })) return false;
        auto& previous = (*books_)[h];
        if (book.tick_size_e4 != active_->expected_ticks_e4[h]
            || book.state_version == 0 || book.receive_monotonic_ns <= 0 || book.receive_monotonic_ns > now_ns_
            || (previous.state_version && (book.state_version < previous.state_version
                || book.receive_monotonic_ns < previous.receive_monotonic_ns))) {
            // Keep version/time watermarks: another old frame must not heal a
            // rejected inversion. Only a newer decoder state/epoch can recover.
            previous.valid = previous.lineage_continuous = 0;
        } else if (previous.state_version && book.state_version == previous.state_version
                   && book.valid && book.lineage_continuous) {
            // Same decoder version cannot introduce a different state. Avoid
            // duplicate evaluation without trusting replacement book contents.
            return previous.valid != 0;
        } else {
            previous = book;
        }
        const auto deps = std::span<const TokenDependency>(active_->dependencies);
        const auto it = std::lower_bound(deps.begin(), deps.end(), h,
            [](const auto& d, auto token) { return d.token_handle < token; });
        if (it != deps.end() && it->token_handle == h) {
            for (std::size_t j = 0; j < it->relation_count; ++j) {
                const auto r = active_->relation_handles[it->first_relation + j];
                if (!touched_[r]) { touched_[r] = true; affected_[affected_count_++] = r; }
            }
        }
        return previous.valid != 0;
    }

    template<class Callback>
    void end_frame(const HotTimingContext& timing, const HotResources& resources, Callback&& emit) noexcept {
        if (!ready_ || !active_ || observed_invalidation_ != invalidation_.load(std::memory_order_acquire)
            || timing.now_receive_monotonic_ns != now_ns_ || timing.maximum_book_age_ns <= 0
            || timing.maximum_leg_skew_ns <= 0 || resources.capital_microunits < 0) {
            ready_ = false;
            return;
        }
        // Deterministic relation order independent of token ordering in a WS
        // frame. Bounded by affected relations, not total graph cardinality.
        std::sort(affected_.begin(), affected_.begin() + affected_count_);
        for (std::size_t i = 0; i < affected_count_; ++i) {
            if (observed_invalidation_ != invalidation_.load(std::memory_order_acquire)) break;
            const auto h = affected_[i];
            if (now_ns_ >= active_->relation_deadlines_ns[h]) continue;
            const auto& r = active_->relations[h];
            NativeObservation observation{};
            observation.graph_generation = active_->digest;
            observation.bundle_digest = active_->bundle_digest;
            observation.selection_digest = active_->selection_digest;
            observation.source_valid_until_wall_ms = active_->source_valid_until_wall_ms;
            observation.source_valid_until_monotonic_ns = active_->valid_until_monotonic_ns;
            observation.connection_epoch = epoch_;
            observation.frame_sequence = frame_sequence_;
            observation.continuity_serial = continuity_serial_;
            observation.control_admission_sequence = active_->control_admission_sequence;
            observation.relation_valid_until_monotonic_ns = active_->relation_deadlines_ns[h];
            observation.decision_monotonic_ns = now_ns_;
            observation.proof_handle = r.proof_handle;
            const auto sized = evaluate_order_constrained_basket(r, *books_, timing, resources,
                                                                 active_->order_share_quantum_microunits,512,sizing_workspace_.get());
            observation.decision = sized.decision;
            observation.unconstrained_quantity_microunits = sized.unconstrained_quantity_microunits;
            observation.relation_quantum_microunits = sized.relation_quantum_microunits;
            observation.quantity_precision_ready = sized.quantity_precision_ready;
            observation.global_size_optimum_proven = sized.global_optimum_proven;
            observation.sizing_search_exhausted = sized.search_exhausted;
            observation.sizing_quantities_evaluated = sized.quantities_evaluated;
            observation.sizing_net_upper_bound_microunits = sized.net_upper_bound_microunits;
            observation.sizing_net_upper_bound_valid = sized.net_upper_bound_valid;
            observation.near_arb = near_arbitrage(r, *books_, timing, active_->order_share_quantum_microunits);
            if (observation.near_arb.valid) {
                const auto remaining = timing.maximum_book_age_ns-observation.near_arb.maximum_leg_age_ns;
                const auto freshness_deadline = now_ns_ > INT64_MAX-remaining ? INT64_MAX : now_ns_+remaining;
                observation.evaluation_valid_until_monotonic_ns = std::min({freshness_deadline,
                    active_->valid_until_monotonic_ns, active_->relation_deadlines_ns[h]});
            }
            for (std::size_t leg = 0; leg < r.leg_count; ++leg)
                observation.leg_versions[leg] = (*books_)[r.legs[leg].book_handle].state_version;
            // Callback may copy evidence into a bounded SPSC queue. It must NOT
            // perform I/O, allocate, block, or retain references after return.
            if (observed_invalidation_ != invalidation_.load(std::memory_order_acquire)) break;
            emit(observation, r, std::span<const BookDeepSnapshot>(*books_));
        }
        ready_ = false; // prohibit duplicate end_frame emissions
    }

private:
    void clear_books() noexcept { ++continuity_serial_; for (auto& book : *books_) book = {}; }
    void release(OwnedGeneration* p) noexcept {
        for (std::size_t i = 0; i < slots_.size(); ++i) if (p == &slots_[i]) occupied_[i] = false;
    }
    void retire_active() noexcept {
        if (!active_) return;
        // Capacity proof above: failure would mean violating single producer /
        // single consumer ownership. Keep storage pinned even in that case.
        if (retired_.try_push(active_)) active_ = nullptr;
        ready_ = false;
    }

    std::array<OwnedGeneration, 3> slots_{};
    std::array<bool, 3> occupied_{}; // control thread only
    std::atomic<OwnedGeneration*> pending_{nullptr};
    SpscRing<OwnedGeneration*, 4> retired_;
    std::atomic<std::uint64_t> invalidation_{0};
    OwnedGeneration* active_ = nullptr; // feed thread only
    std::uint64_t observed_invalidation_ = 0;
    std::unique_ptr<std::array<BookDeepSnapshot, kRuntimeBooks>> books_;
    std::unique_ptr<order_sizing_detail::DepthWorkspace> sizing_workspace_;
    std::array<std::array<std::uint8_t, 32>, kRuntimeBooks> identities_{};
    std::array<bool, kRuntimeRelations> touched_{};
    std::array<std::uint32_t, kRuntimeRelations> affected_{};
    std::size_t affected_count_ = 0;
    std::int64_t now_ns_ = 0;
    std::uint64_t epoch_ = 0;
    std::uint64_t frame_sequence_ = 0;
    std::uint64_t continuity_serial_ = 0;
    bool ready_ = false;
};

} // namespace pm::v7::exact_arb_graph
