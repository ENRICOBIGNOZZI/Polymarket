#include "pm/v7_exact_arb_ws_replay.hpp"
#include "pm/v7_exact_arb_evidence_json.hpp"
#include "pm/v7_market_ws.hpp"
#include <set>
#include <stdexcept>

namespace pm::v7::exact_arb_graph {
namespace {
namespace json = boost::json;
void require(bool condition, const char* reason) { if (!condition) throw std::runtime_error(reason); }
std::int64_t number(const json::object& value, const char* key, std::int64_t minimum = 0) {
    const auto& n = value.at(key);
    require(n.is_int64() || n.is_uint64(), "ws_replay_integer");
    const auto result = json::value_to<std::int64_t>(n);
    require(result >= minimum, "ws_replay_integer_bounds"); return result;
}
std::string string(const json::object& value, const char* key) {
    return std::string(value.at(key).as_string());
}
void safety(const json::object& root) {
    for (const auto& entry : evidence_safety())
        require(root.at(entry.key()) == entry.value(), "ws_replay_paper_boundary");
}
} // namespace

struct NativeWsReplay::Impl {
    std::string model, session, manifest_hash, output_hash;
    std::vector<TokenBinding> bindings;
    std::unique_ptr<MarketWsShard> decoder;
    std::unique_ptr<std::array<MarketWsEvent,512>> events = std::make_unique<std::array<MarketWsEvent,512>>();
    std::uint64_t sequence = 0, epoch = 0, continuity = 0, frames = 0, missing = 0, invalid = 0;
    std::int64_t receive = 0, available = 0;
    bool failed = false;
    bool source_versions_comparable = true;
};

NativeWsReplay::NativeWsReplay(std::string_view manifest, std::string_view expected_model)
    : impl_(std::make_unique<Impl>()) {
    require(manifest.size() <= 1024*1024, "ws_replay_manifest_size");
    const auto document = json::parse(manifest);
    const auto& root = document.as_object(); safety(root);
    auto& p = *impl_;
    p.model = string(root,"model_sha"); p.session = string(root,"observer_session_id");
    require(p.model == expected_model && p.model.size() == 40 && !p.session.empty(), "ws_replay_identity");
    require(string(root,"schema") == "polymarket_v7_native_exact_arb_ws_session_v1"
        && string(root,"capture_scope") == "PUBLIC_WS_FRAMES_NOT_REST", "ws_replay_manifest_schema");
    require(number(root,"decoder_output_capacity") == 512
        && number(root,"maximum_frame_bytes") == 1024*1024, "ws_replay_decoder_contract");
    const auto& tokens = root.at("bindings").as_array();
    require(!tokens.empty() && tokens.size() <= 128, "ws_replay_binding_capacity");
    std::set<std::string> ids; std::set<std::int64_t> handles;
    for (const auto& value : tokens) {
        const auto& token = value.as_object();
        const auto id = string(token,"token_id");
        const auto h = number(token,"book_handle",1), tick = number(token,"tick_size_e4",1);
        require(!id.empty() && id.size() <= 256 && h <= 128 && tick < 10000
            && ids.insert(id).second && handles.insert(h).second, "ws_replay_binding_identity");
        // Market/event handles do not participate in CanonicalL2Book state;
        // only token -> book handles/ticks are replayed, not order ownership.
        p.bindings.push_back({id,1,1,static_cast<std::uint64_t>(h),static_cast<std::int32_t>(tick)});
    }
    p.manifest_hash = evidence_sha256(manifest);
    p.decoder = std::make_unique<MarketWsShard>(p.bindings);
}
NativeWsReplay::~NativeWsReplay() = default;

std::string NativeWsReplay::ingest(std::string_view record) {
    auto& p = *impl_;
    require(!p.failed, "ws_replay_failed_session");
    try {
        require(record.size() <= 8*1024*1024, "ws_replay_record_size");
        const auto document = json::parse(record);
        const auto& root = document.as_object(); safety(root);
        require(string(root,"schema") == "polymarket_v7_native_exact_arb_ws_frame_v1"
            && string(root,"model_sha") == p.model && string(root,"observer_session_id") == p.session
            && string(root,"session_manifest_sha256") == p.manifest_hash, "ws_replay_frame_identity");
        const auto payload = string(root,"payload");
        require(payload.size() <= 1024*1024 && evidence_sha256(payload) == string(root,"payload_sha256"), "ws_replay_payload_digest");
        const auto sequence = static_cast<std::uint64_t>(number(root,"feed_frame_sequence",1));
        const auto epoch = static_cast<std::uint64_t>(number(root,"connection_epoch",1));
        require(sequence > p.sequence, "ws_replay_sequence_reversal_or_duplicate");
        require(epoch >= (p.epoch ? p.epoch : 1), "ws_replay_epoch_reversal");
        const auto receive = number(root,"receive_monotonic_ns",1);
        const auto available = number(root,"decode_complete_monotonic_ns",1);
        const auto decision = number(root,"graph_decision_start_ns",1);
        require(receive >= p.receive && available >= p.available && available >= receive && decision >= available,
                "ws_replay_clock_inversion");
        const auto expected_events = number(root,"decoded_events");
        require(expected_events <= 512, "ws_replay_output_count");
        const bool valid = root.at("source_frame_valid").as_bool();
        std::string reset;
        const auto skipped = sequence-p.sequence-1;
        if (!p.sequence) reset = "INITIAL_SESSION";
        if (skipped) { p.missing += skipped; reset = "FEED_SEQUENCE_GAP"; }
        if (p.epoch && epoch != p.epoch) reset = "CONNECTION_EPOCH_CHANGED";
        if (!valid) reset = "SOURCE_FRAME_INVALID";
        if (!reset.empty()) ++p.continuity;
        const auto reconnects = epoch-(p.epoch ? p.epoch : 1);
        require(reconnects <= 4096, "ws_replay_epoch_jump");
        for (std::uint64_t i = 0; i < reconnects; ++i) p.decoder->invalidate_all_lineage();
        if (skipped) {
            p.decoder->invalidate_all_lineage();
            // Missing frames mean source state-version increments are unknown.
            // Fresh snapshots may restore book contents, never invent counters.
            p.source_versions_comparable = false;
        }
        auto out = evidence_safety();
        out["schema"] = "polymarket_v7_native_exact_arb_replayed_frame_v1";
        out["model_sha"] = p.model; out["observer_session_id"] = p.session;
        out["session_manifest_sha256"] = p.manifest_hash;
        out["feed_frame_sequence"] = sequence; out["connection_epoch"] = epoch;
        out["receive_monotonic_ns"] = receive; out["availability_monotonic_ns"] = available;
        out["graph_decision_start_ns"] = decision;
        out["source_payload_sha256"] = string(root,"payload_sha256");
        out["reset_reason"] = reset; out["replay_continuity_serial"] = p.continuity;
        out["missing_frames_before"] = skipped; out["source_frame_valid"] = valid;
        out["source_state_versions_comparable"] = p.source_versions_comparable;
        json::array books;
        pm::fast::FeedReceiveStamp stamp;
        stamp.wall_ms = number(root,"receive_wall_ms",1); stamp.monotonic_ns = receive;
        // Preserve the recorded host wall clock for offline hourly attribution.
        // Matching and book availability continue to use monotonic time only.
        out["receive_wall_ms"] = stamp.wall_ms;
        const auto decoded = p.decoder->process_frame(payload, stamp, *p.events);
        if (valid) {
            require(!decoded.invalid_frame && !decoded.output_overflow && !decoded.arena_exhausted
                && decoded.output_count == static_cast<std::size_t>(expected_events), "ws_replay_decoder_divergence");
            std::array<bool,129> affected{};
            for (std::size_t i = 0; i < decoded.output_count; ++i) {
                const auto h = (*p.events)[i].instrument_handle;
                require(h > 0 && h < affected.size(), "ws_replay_event_binding");
                affected[h] = true;
            }
            // Final per-token book AFTER the whole frame, never a partial JSON
            // array or one of several intermediate same-frame depth updates.
            for (const auto& binding : p.bindings) if (affected[binding.instrument_handle]) {
                auto book = book_evidence_json(p.decoder->deep_snapshot(binding.instrument_handle));
                book["token_id"] = binding.asset_id; book["book_handle"] = binding.instrument_handle;
                books.push_back(std::move(book));
            }
        } else {
            ++p.invalid;
            if (decoded.output_count != static_cast<std::size_t>(expected_events)) p.source_versions_comparable = false;
        }
        out["source_state_versions_comparable"] = p.source_versions_comparable;
        out["books"] = std::move(books);
        p.sequence = sequence; p.epoch = epoch; p.receive = receive; p.available = available; ++p.frames;
        const auto serialized = json::serialize(out);
        p.output_hash = evidence_sha256(p.output_hash+serialized);
        return serialized;
    } catch (...) { p.failed = true; throw; }
}

std::string NativeWsReplay::receipt() const {
    const auto& p = *impl_;
    auto root = evidence_safety();
    root["schema"] = "polymarket_v7_native_exact_arb_ws_replay_receipt_v1";
    root["model_sha"] = p.model; root["observer_session_id"] = p.session;
    root["session_manifest_sha256"] = p.manifest_hash;
    root["state"] = p.failed ? "FAILED" : "INPUT_REPLAYED";
    root["frames_replayed"] = p.frames; root["missing_frames"] = p.missing;
    root["invalid_frames"] = p.invalid; root["last_feed_frame_sequence"] = p.sequence;
    root["availability_monotonic_ns"] = p.available; root["output_chain_sha256"] = p.output_hash;
    root["producer_tail_completeness_verified"] = false;
    root["venue_execution_verified"] = false;
    return json::serialize(root);
}
} // namespace pm::v7::exact_arb_graph
