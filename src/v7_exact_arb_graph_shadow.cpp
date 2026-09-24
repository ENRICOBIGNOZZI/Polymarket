#include "pm/v7_exact_arb_graph_shadow.hpp"
#include "pm/v7_exact_arb_evidence_json.hpp"
#include <boost/json.hpp>

#include <chrono>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <cstring>

namespace pm::v7::exact_arb_graph {
namespace {
namespace json = boost::json;
namespace fs = std::filesystem;
constexpr std::size_t kObservationQueue = 4096, kFullQueue = 8;
constexpr std::size_t kFrameQueue = 4096, kFrameByteCapacity = 16*1024*1024, kMaximumFrameBytes = 1024*1024;
constexpr std::int64_t kMaxAgeNs = 1000000000, kMaxSkewNs = 50000000;
std::int64_t steady_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::string hex(const std::array<std::uint8_t, 32>& value) {
    std::string out; out.reserve(64);
    for (auto b : value) { out += "0123456789abcdef"[b >> 4]; out += "0123456789abcdef"[b & 15]; }
    return out;
}
void atomic_file(const fs::path& path, std::string_view contents) {
    const auto temp = path.string()+".tmp";
    {
        std::ofstream stream(temp, std::ios::binary | std::ios::trunc);
        stream.write(contents.data(), contents.size()); stream.flush();
        if (!stream) throw std::runtime_error("native_graph_atomic_write");
    }
    fs::rename(temp, path);
}
struct QueuedObservation {
    NativeObservation observation{};
    NativeFrameClock clock{};
    std::int64_t decision_end_ns = 0;
    std::int64_t paper_capital_microunits = 0;
    std::uint64_t observation_sequence = 0;
    std::uint64_t feed_frame_sequence = 0;
};
struct FrameEvidence {
    NativeFrameClock clock{};
    std::uint64_t sequence = 0, epoch = 0, byte_start = 0;
    std::uint32_t byte_count = 0, decoded_events = 0;
    bool valid = false;
};
struct FullEvidence {
    QueuedObservation row{};
    std::uint8_t leg_count = 0;
    std::array<std::uint32_t, kMaxLegs> handles{};
    std::array<BookDeepSnapshot, kMaxLegs> books{};
};
} // namespace

struct NativeGraphShadow::Impl {
    Impl(fs::path path, std::string sha, std::string id, std::vector<NativeTokenBinding> tokens)
        : output(std::move(path)), model(std::move(sha)), session(std::move(id)), bindings(std::move(tokens)) {
        fs::create_directories(output/"native_generations");
        fs::create_directories(output/"native_sessions");
        auto manifest = evidence_safety();
        manifest["schema"] = "polymarket_v7_native_exact_arb_ws_session_v1";
        manifest["model_sha"] = model; manifest["observer_session_id"] = session;
        manifest["decoder_output_capacity"] = 512;
        manifest["maximum_frame_bytes"] = kMaximumFrameBytes;
        manifest["capture_scope"] = "PUBLIC_WS_FRAMES_NOT_REST";
        json::array subscribed;
        for (const auto& binding : bindings)
            subscribed.push_back(json::object{{"token_id", binding.token_id}, {"book_handle", binding.book_handle},
                                             {"tick_size_e4", binding.tick_size_e4}});
        manifest["bindings"] = std::move(subscribed);
        const auto serialized = json::serialize(manifest);
        session_digest = evidence_sha256(serialized);
        atomic_file(output/"native_sessions"/(session_digest+".json"), serialized);
        observations.open(output/"native_exact_arb_observations.jsonl", std::ios::app);
        evidence.open(output/"native_exact_arb_full_evidence.jsonl", std::ios::app);
        ws_frames.open(output/"native_exact_arb_ws_frames.jsonl", std::ios::app);
        control.open(output/"native_exact_arb_control.jsonl", std::ios::app);
        if (!observations || !evidence || !ws_frames || !control) throw std::runtime_error("native_graph_output_open");
        control_record("INVALIDATE", steady_ns());
    }
    // Single CONTROL writer. Persist before publishing a generation; the feed
    // only copies its compact sequence handle. A failed journal is sticky until
    // process restart: a later heartbeat cannot certify a missing transition.
    void control_record(const char* kind, std::int64_t now, std::string_view bundle = {}, std::int64_t until = 0,
                        std::string_view selection = {}) {
        if (control_failed || now < control_last_ns || now <= 0 || control_sequence == UINT64_MAX)
            throw std::runtime_error("native_control_unavailable");
        auto row = evidence_safety();
        row["schema"] = "polymarket_v7_native_exact_arb_control_v1";
        row["model_sha"] = model; row["observer_session_id"] = session;
        row["session_manifest_sha256"] = session_digest;
        row["sequence"] = control_sequence+1; row["timestamp_monotonic_ns"] = now;
        row["kind"] = kind; row["previous_record_sha256"] = control_hash;
        row["native_bundle_sha256"] = std::string(bundle); row["valid_until_monotonic_ns"] = until;
        row["selection_receipt_sha256"] = std::string(selection);
        const auto wire = json::serialize(row);
        control << wire << '\n'; control.flush();
        if (!control) { control_failed = true; throw std::runtime_error("native_control_write"); }
        ++control_sequence;
        control_hash = evidence_sha256(wire); control_last_ns = now;
        rotate(control, "native_exact_arb_control.jsonl", control_rotation);
    }
    json::object row_json(const QueuedObservation& row) const {
        const auto& o = row.observation;
        const auto& d = o.decision;
        auto root = evidence_safety();
        root["schema"] = "polymarket_v7_native_exact_arb_observation_v2";
        root["evidence_scope"] = "CAUSAL_FRAME_END_PRE_ALLOCATION_EVALUATION";
        root["actionable"] = false;
        root["economic_execution_verified"] = false;
        root["model_sha"] = model;
        root["observer_session_id"] = session;
        root["session_manifest_sha256"] = session_digest;
        root["feed_frame_sequence"] = row.feed_frame_sequence;
        root["graph_generation"] = hex(o.graph_generation);
        root["native_bundle_sha256"] = hex(o.bundle_digest);
        root["selection_receipt_sha256"] = hex(o.selection_digest);
        root["source_valid_until_wall_ms"] = o.source_valid_until_wall_ms;
        root["source_valid_until_monotonic_ns"] = o.source_valid_until_monotonic_ns;
        root["connection_epoch"] = o.connection_epoch;
        root["frame_sequence"] = o.frame_sequence;
        root["observation_sequence"] = row.observation_sequence;
        root["continuity_serial"] = o.continuity_serial;
        root["control_admission_sequence"] = o.control_admission_sequence;
        root["evaluation_valid_until_monotonic_ns"] = o.evaluation_valid_until_monotonic_ns;
        root["relation_valid_until_monotonic_ns"] = o.relation_valid_until_monotonic_ns;
        root["relation_handle"] = d.relation_handle;
        root["proof_handle"] = o.proof_handle;
        root["receive_wall_ms"] = row.clock.receive_wall_ms;
        root["receive_monotonic_ns"] = row.clock.receive_monotonic_ns;
        root["decision_start_ns"] = o.decision_monotonic_ns;
        root["decision_end_ns"] = row.decision_end_ns;
        root["reject_code"] = static_cast<unsigned>(d.reject);
        constexpr std::array<const char*, 17> rejection_names{
            "ACCEPTED_PRE_ALLOCATION", "INVALID_RELATION", "MISSING_BOOK", "INCOMPLETE_DEPTH",
            "STALE_BOOK", "LEG_SKEW", "INSUFFICIENT_DEPTH", "MINIMUM_ORDER", "NO_POSITIVE_EDGE",
            "INVENTORY_UNAVAILABLE", "CAPITAL_LIMIT", "UNKNOWN_FEE", "NUMERIC_OVERFLOW",
            "TRANSFORMATION_UNAVAILABLE", "VENUE_PRECISION", "SIZING_INCOMPLETE", "UNKNOWN"};
        const auto rejection = static_cast<std::size_t>(d.reject);
        root["rejection_reason"] = rejection_names[std::min(rejection, rejection_names.size()-1)];
        root["evaluation_accepted"] = d.reject == HotReject::Accepted;
        root["quantity_microunits"] = d.quantity_microunits;
        root["unconstrained_quantity_microunits"] = o.unconstrained_quantity_microunits;
        root["relation_quantum_microunits"] = o.relation_quantum_microunits;
        root["order_quantity_precision_ready"] = o.quantity_precision_ready != 0;
        root["global_size_optimum_proven"] = o.global_size_optimum_proven != 0;
        root["sizing_model"] = "PER_L2_LEVEL_5DP_EXACT_ORDER_LATTICE_V1";
        root["sizing_proof_scope"] = "RECORDED_MODEL_ONLY_NOT_VERIFIED_VENUE_EXECUTION";
        root["legacy_sweep_quantity_microunits"] = o.unconstrained_quantity_microunits;
        root["sizing_search_exhausted"] = o.sizing_search_exhausted != 0;
        root["sizing_quantities_evaluated"] = o.sizing_quantities_evaluated;
        root["sizing_net_upper_bound_microunits"] = o.sizing_net_upper_bound_valid
            ? json::value(o.sizing_net_upper_bound_microunits) : json::value(nullptr);
        const auto& near = o.near_arb;
        json::object diagnostics{
            {"scope", "TOP_OF_BOOK_PER_RELATION_UNIT_NOT_EXECUTABLE_CAPACITY"},
            {"valid", near.valid != 0}, {"books_ready", near.books_ready != 0},
            {"lineage_ready", near.lineage_ready != 0}, {"fees_ready", near.fees_ready != 0},
            {"freshness_ready", near.freshness_ready != 0}, {"leg_skew_ready", near.skew_ready != 0},
            {"depth_complete", near.depth_complete != 0}};
        diagnostics["readiness_reason"] = near.valid ? "READY" : !near.books_ready ? "BOOK_OR_RELATION_UNAVAILABLE"
            : !near.lineage_ready ? "LINEAGE_UNAVAILABLE" : !near.fees_ready ? "FEE_UNAVAILABLE"
            : !near.freshness_ready ? "STALE_OR_FUTURE_BOOK" : !near.skew_ready ? "LEG_SKEW"
            : "NUMERIC_OR_QUANTUM_LIMIT";
        if (near.valid) {
            diagnostics["raw_distance_nano"] = near.raw_distance_nano;
            diagnostics["after_fee_distance_nano"] = near.after_fee_distance_nano;
            diagnostics["after_reserve_distance_nano"] = near.after_reserve_distance_nano;
            diagnostics["actionable_tick_nano"] = near.actionable_tick_nano;
            diagnostics["guarantee_nano"] = near.guarantee_nano;
            diagnostics["fee_probe_quantity_microunits"] = near.fee_probe_quantity_microunits;
        } else {
            for (const auto* key : {"raw_distance_nano", "after_fee_distance_nano", "after_reserve_distance_nano",
                                   "actionable_tick_nano", "guarantee_nano", "fee_probe_quantity_microunits"}) diagnostics[key] = nullptr;
        }
        if (near.freshness_ready) {
            diagnostics["maximum_leg_age_ns"] = near.maximum_leg_age_ns;
            diagnostics["minimum_leg_age_ns"] = near.minimum_leg_age_ns;
            diagnostics["leg_skew_ns"] = near.leg_skew_ns;
        } else {
            for (const auto* key : {"maximum_leg_age_ns", "minimum_leg_age_ns", "leg_skew_ns"}) diagnostics[key] = nullptr;
        }
        root["near_arbitrage"] = std::move(diagnostics);
        root["raw_pnl_microunits"] = d.raw_pnl_microunits;
        root["fees_microunits"] = d.fees_microunits;
        root["after_fee_pnl_microunits"] = d.gross_pnl_microunits;
        root["after_reserve_pnl_microunits"] = d.net_pnl_microunits;
        root["capital_required_microunits"] = d.capital_required_microunits;
        root["paper_budget_before_resource_allocation_microunits"] = row.paper_capital_microunits;
        json::array versions, used;
        for (auto v : o.leg_versions) versions.push_back(v);
        for (auto n : d.levels_consumed) used.push_back(n);
        root["leg_versions"] = std::move(versions);
        root["levels_consumed"] = std::move(used);
        return root;
    }
    void rotate(std::ofstream& stream, const char* name, std::uint64_t& sequence) {
        if (stream.tellp() < 64*1024*1024) return;
        stream.flush();
        if (!stream) throw std::runtime_error("native_graph_flush");
        stream.close();
        fs::rename(output/name, output/(std::string(name)+"."+session+"."+std::to_string(sequence++)));
        stream.open(output/name, std::ios::app);
        if (!stream) throw std::runtime_error("native_graph_rotate");
    }
    fs::path output;
    std::string model, session, session_digest;
    std::vector<NativeTokenBinding> bindings;
    NativeGraphRuntime runtime;
    std::unique_ptr<SpscRing<QueuedObservation, kObservationQueue>> queue =
        std::make_unique<SpscRing<QueuedObservation, kObservationQueue>>();
    std::unique_ptr<SpscRing<FullEvidence, kFullQueue>> full_queue =
        std::make_unique<SpscRing<FullEvidence, kFullQueue>>();
    std::unique_ptr<FullEvidence> scratch = std::make_unique<FullEvidence>(); // feed only
    std::unique_ptr<SpscRing<FrameEvidence, kFrameQueue>> frame_queue = std::make_unique<SpscRing<FrameEvidence, kFrameQueue>>();
    std::unique_ptr<std::array<char, kFrameByteCapacity>> frame_bytes = std::make_unique<std::array<char, kFrameByteCapacity>>();
    std::uint64_t byte_head = 0; // feed only; published by descriptor queue release
    std::atomic<std::uint64_t> byte_tail{0}; // control -> feed buffer ownership
    std::atomic<std::uint64_t> ws_dropped{0}, ws_oversized{0};
    std::uint64_t ws_written = 0, ws_disk_suppressed = 0, ws_rotation = 0;
    std::ofstream observations, evidence, ws_frames, control;
    std::uint64_t control_sequence = 0, control_rotation = 0;
    std::int64_t control_last_ns = 0;
    std::string control_hash;
    bool control_failed = false;
    std::atomic<std::int64_t> budget{0};
    std::atomic<std::uint64_t> frames{0}, evaluations{0}, accepted{0}, dropped{0}, full_dropped{0}, invalid_frames{0};
    std::string selection_bytes, policy_bytes, target_bundle, observed_bundle, error;
    std::string target_graph;
    std::size_t relation_directions = 0;
    std::int64_t expiration_ms = 0;
    std::uint64_t written = 0, full_written = 0, disk_suppressed = 0, full_disk_suppressed = 0;
    std::uint64_t observation_rotation = 0, evidence_rotation = 0;
    bool source_valid = false;
};

NativeGraphShadow::NativeGraphShadow(fs::path path, std::string model, std::string session,
                                   std::vector<NativeTokenBinding> bindings)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(model), std::move(session), std::move(bindings))) {}
NativeGraphShadow::~NativeGraphShadow() = default;

void NativeGraphShadow::invalidate(std::string reason) noexcept {
    auto& p = *impl_;
    const auto when = steady_ns();
    p.runtime.invalidate(); p.source_valid = false; p.budget.store(0, std::memory_order_release);
    p.error = std::move(reason); p.selection_bytes.clear(); p.policy_bytes.clear();
    try { p.control_record("INVALIDATE", when); }
    catch (const std::exception&) { p.control_failed = true; }
}

void NativeGraphShadow::refresh(std::string_view selection, std::string_view policy,
                               std::int64_t wall, std::int64_t mono) noexcept {
    auto& p = *impl_;
    try {
        p.runtime.collect_retired();
        if (p.control_failed) throw std::runtime_error("native_control_unavailable");
        if (p.source_valid && wall < p.expiration_ms && selection == p.selection_bytes && policy == p.policy_bytes) return;
        // New source bytes are untrusted until validation finishes. Neither a
        // formerly successful lease nor queued publication may bridge this gap.
        invalidate("SOURCE_REVALIDATION");
        if (p.control_failed) throw std::runtime_error("native_control_unavailable");
        if (policy.size() > 65536) throw std::runtime_error("native_capital_policy_bounds");
        const auto document = json::parse(policy);
        const auto& capital = document.as_object();
        if (capital.at("schema").as_string() != "polymarket_v7_pure_arb_capital_policy_v1"
            || !capital.at("paper_only").as_bool() || capital.at("authenticated_execution").as_bool()
            || capital.at("real_order_submission").as_bool() || capital.at("automatic_promotion").as_bool())
            throw std::runtime_error("native_capital_policy_boundary");
        const auto amount = json::value_to<double>(capital.at("paper_budget_pusd"));
        if (!std::isfinite(amount) || amount <= 0 || amount > 1000000000)
            throw std::runtime_error("native_capital_policy_budget");
        auto loaded = load_native_generation(selection, p.model, p.bindings, wall, mono);
        const auto archive = p.output/"native_generations"/(loaded.bundle_sha256+".json");
        if (fs::exists(archive)) {
            if (fs::file_size(archive) != loaded.canonical_payload.size()) throw std::runtime_error("native_archive_conflict");
            std::ifstream input(archive);
            std::ostringstream existing; existing << input.rdbuf();
            if (!input || existing.str() != loaded.canonical_payload) throw std::runtime_error("native_archive_conflict");
        } else atomic_file(archive, loaded.canonical_payload);
        const auto receipt = p.output/"native_generations"/(hex(loaded.generation.selection_digest)+".selection.json");
        atomic_file(receipt, selection);
        // Archive precedes publication, so any emitted handle is reconstructible.
        p.budget.store(static_cast<std::int64_t>(std::floor(amount*1000000)), std::memory_order_release);
        p.target_graph = hex(loaded.generation.digest);
        p.relation_directions = loaded.generation.relations.size();
        const auto admitted_at = steady_ns();
        if (admitted_at >= loaded.generation.valid_until_monotonic_ns)
            throw std::runtime_error("native_control_source_expired_during_validation");
        loaded.generation.control_admission_sequence = p.control_sequence+1;
        loaded.generation.control_admitted_monotonic_ns = admitted_at;
        p.control_record("ADMIT", admitted_at, loaded.bundle_sha256, loaded.generation.valid_until_monotonic_ns,
                         hex(loaded.generation.selection_digest));
        if (!p.runtime.publish(std::move(loaded.generation))) throw std::runtime_error("native_generation_publish");
        p.target_bundle = loaded.bundle_sha256;
        p.expiration_ms = loaded.valid_until_wall_ms;
        p.selection_bytes = selection; p.policy_bytes = policy;
        p.source_valid = true; p.error.clear();
    } catch (const std::exception& e) { invalidate(e.what()); }
}

void NativeGraphShadow::on_frame(const MarketWsShard& decoder, std::span<const MarketWsEvent> events,
                               const NativeFrameClock& clock, std::uint64_t epoch, bool valid,
                               std::string_view raw_frame) noexcept {
    auto& p = *impl_;
    const auto frame_sequence = p.frames.fetch_add(1, std::memory_order_relaxed)+1;
    // Capture every received frame, even with no positive relation or valid graph.
    // Byte storage is bounded independently of descriptor count. No allocation,
    // hashing, serialization or I/O on this thread; copy only actual wire bytes.
    const auto size = raw_frame.size();
    if (size > kMaximumFrameBytes) { ++p.ws_dropped; ++p.ws_oversized; }
    else if (p.frame_queue->approximate_size() >= kFrameQueue
             || p.byte_head-p.byte_tail.load(std::memory_order_acquire) > kFrameByteCapacity-size
             || p.byte_head > UINT64_MAX-size) ++p.ws_dropped;
    else {
        const auto offset = p.byte_head%kFrameByteCapacity;
        const auto first = std::min<std::size_t>(size, kFrameByteCapacity-offset);
        if (first) std::memcpy(p.frame_bytes->data()+offset, raw_frame.data(), first);
        if (size>first) std::memcpy(p.frame_bytes->data(), raw_frame.data()+first, size-first);
        const FrameEvidence frame{clock, frame_sequence, epoch, p.byte_head,
            static_cast<std::uint32_t>(size), static_cast<std::uint32_t>(events.size()), valid};
        if (p.frame_queue->try_push(frame)) p.byte_head += size;
        else ++p.ws_dropped;
    }
    if (!valid) { ++p.invalid_frames; p.runtime.reset_lineage(); return; }
    if (!p.runtime.begin_frame(clock.decision_monotonic_ns, epoch)) return;
    std::array<bool, kRuntimeBooks> supplied{};
    for (const auto& event : events) {
        const auto h = event.instrument_handle;
        if (h == 0 || h >= supplied.size() || supplied[h]) continue;
        supplied[h] = true;
        (void)p.runtime.update_book(h, decoder.deep_snapshot(h));
    }
    HotResources resources;
    resources.capital_microunits = p.budget.load(std::memory_order_acquire);
    // No prefunded or synthetic short inventory. Resource allocation and venue
    // order-precision admission remain separate downstream research gates.
    p.runtime.end_frame({clock.decision_monotonic_ns, kMaxAgeNs, kMaxSkewNs}, resources,
        [&](const NativeObservation& observation, const CompiledRelation& relation,
            std::span<const BookDeepSnapshot> books) noexcept {
            const auto sequence = p.evaluations.fetch_add(1, std::memory_order_relaxed)+1;
            QueuedObservation row{observation, clock, steady_ns(), resources.capital_microunits, sequence, frame_sequence};
            if (!p.queue->try_push(row)) ++p.dropped;
            if (observation.decision.reject != HotReject::Accepted) return;
            ++p.accepted;
            if (p.full_queue->approximate_size() >= p.full_queue->capacity()) { ++p.full_dropped; return; }
            auto& full = *p.scratch;
            full.row = row; full.leg_count = relation.leg_count;
            for (std::size_t i = 0; i < relation.leg_count; ++i) {
                full.handles[i] = relation.legs[i].book_handle;
                full.books[i] = books[full.handles[i]];
            }
            if (!p.full_queue->try_push(full)) ++p.full_dropped;
        });
}

void NativeGraphShadow::drain(bool disk_pressure) {
    auto& p = *impl_;
    QueuedObservation row;
    // Bound each drain pass so a busy producer cannot starve lease refresh.
    for (std::size_t i = 0; i < kObservationQueue && p.queue->try_pop(row); ++i) {
        p.observed_bundle = hex(row.observation.bundle_digest);
        if (disk_pressure) { ++p.disk_suppressed; continue; }
        p.observations << json::serialize(p.row_json(row)) << '\n'; ++p.written;
        p.rotate(p.observations, "native_exact_arb_observations.jsonl", p.observation_rotation);
    }
    for (std::size_t i = 0; i < kFullQueue; ++i) {
        const auto* full = p.full_queue->try_peek();
        if (!full) break;
        if (disk_pressure) { ++p.full_disk_suppressed; (void)p.full_queue->pop_commit(); continue; }
        auto root = p.row_json(full->row);
        root["schema"] = "polymarket_v7_native_exact_arb_full_evidence_v2";
        json::array books;
        for (std::size_t j = 0; j < full->leg_count; ++j) {
            auto b = book_evidence_json(full->books[j]); b["book_handle"] = full->handles[j];
            const auto binding = std::find_if(p.bindings.begin(), p.bindings.end(),
                [&](const auto& token) { return token.book_handle == full->handles[j]; });
            if (binding == p.bindings.end()) throw std::runtime_error("native_evidence_token_binding");
            b["token_id"] = binding->token_id;
            books.push_back(std::move(b));
        }
        root["leg_books"] = std::move(books);
        p.evidence << json::serialize(root) << '\n'; ++p.full_written;
        (void)p.full_queue->pop_commit();
        p.rotate(p.evidence, "native_exact_arb_full_evidence.jsonl", p.evidence_rotation);
    }
    FrameEvidence frame;
    std::size_t drained_bytes = 0;
    for (std::size_t i = 0; i < kFrameQueue && drained_bytes < 8*1024*1024 && p.frame_queue->try_pop(frame); ++i) {
        drained_bytes += frame.byte_count;
        if (disk_pressure) ++p.ws_disk_suppressed;
        else {
            const auto offset = frame.byte_start%kFrameByteCapacity;
            const auto first = std::min<std::size_t>(frame.byte_count, kFrameByteCapacity-offset);
            std::string payload(p.frame_bytes->data()+offset, first);
            payload.append(p.frame_bytes->data(), frame.byte_count-first);
            auto root = evidence_safety();
            root["schema"] = "polymarket_v7_native_exact_arb_ws_frame_v1";
            root["model_sha"] = p.model; root["observer_session_id"] = p.session;
            root["session_manifest_sha256"] = p.session_digest;
            root["feed_frame_sequence"] = frame.sequence; root["connection_epoch"] = frame.epoch;
            root["source_frame_valid"] = frame.valid; root["decoded_events"] = frame.decoded_events;
            root["receive_wall_ms"] = frame.clock.receive_wall_ms;
            root["receive_monotonic_ns"] = frame.clock.receive_monotonic_ns;
            root["decode_complete_monotonic_ns"] = frame.clock.decode_complete_monotonic_ns;
            root["graph_decision_start_ns"] = frame.clock.decision_monotonic_ns;
            root["payload_sha256"] = evidence_sha256(payload);
            root["payload"] = std::move(payload);
            p.ws_frames << json::serialize(root) << '\n'; ++p.ws_written;
            p.rotate(p.ws_frames, "native_exact_arb_ws_frames.jsonl", p.ws_rotation);
        }
        // The consumer has finished reading these bytes before releasing them.
        p.byte_tail.store(frame.byte_start+frame.byte_count, std::memory_order_release);
    }
    p.observations.flush(); p.evidence.flush(); p.ws_frames.flush();
    if (!p.observations || !p.evidence || !p.ws_frames) throw std::runtime_error("native_graph_evidence_flush");
}

void NativeGraphShadow::write_status(std::int64_t wall, bool stopped) {
    auto& p = *impl_;
    if (stopped) invalidate("OBSERVER_STOPPED");
    if (!p.control_failed) {
        try { p.control_record("CHECKPOINT", steady_ns()); }
        catch (const std::exception&) { p.control_failed = true; invalidate("CONTROL_JOURNAL_FAILED"); }
    }
    auto root = evidence_safety();
    root["schema"] = "polymarket_v7_native_exact_arb_status_v1";
    root["model_sha"] = p.model; root["observer_session_id"] = p.session;
    root["timestamp_ms"] = wall;
    root["state"] = stopped ? "STOPPED" : !p.source_valid ? "BLOCKED" : wall >= p.expiration_ms ? "EXPIRED"
        : p.target_bundle == p.observed_bundle ? "RUNNING" : "WAITING_FOR_FRAMES";
    root["valid_until_ms"] = p.expiration_ms;
    root["target_bundle_sha256"] = p.target_bundle; root["observed_bundle_sha256"] = p.observed_bundle;
    root["graph_generation"] = p.target_graph;
    root["relation_directions"] = p.relation_directions;
    root["frames_processed"] = p.frames.load();
    root["error"] = p.error;
    root["evaluations"] = p.evaluations.load(); root["accepted_evaluations"] = p.accepted.load();
    root["observations_written"] = p.written; root["observations_dropped"] = p.dropped.load();
    root["full_evidence_written"] = p.full_written; root["full_evidence_dropped"] = p.full_dropped.load();
    root["queue_depth"] = p.queue->approximate_size(); root["full_queue_depth"] = p.full_queue->approximate_size();
    root["disk_suppressed"] = p.disk_suppressed; root["full_disk_suppressed"] = p.full_disk_suppressed;
    root["invalid_frames"] = p.invalid_frames.load();
    root["ws_frames_written"] = p.ws_written; root["ws_frames_dropped"] = p.ws_dropped.load();
    root["ws_frames_oversized"] = p.ws_oversized.load(); root["ws_frames_disk_suppressed"] = p.ws_disk_suppressed;
    root["ws_frame_queue_depth"] = p.frame_queue->approximate_size();
    root["session_manifest_sha256"] = p.session_digest;
    root["control_journal_failed"] = p.control_failed;
    root["control_records_written"] = p.control_sequence;
    root["control_record_sha256"] = p.control_hash;
    root["control_watermark_monotonic_ns"] = p.control_last_ns;
    root["opportunity_count"] = nullptr; root["execution_pnl"] = nullptr;
    root["evidence_scope"] = "CAUSAL_FRAME_END_PRE_ALLOCATION_EVALUATION";
    atomic_file(p.output/"native_exact_arb_status.json", json::serialize(root)+"\n");
}
} // namespace pm::v7::exact_arb_graph
