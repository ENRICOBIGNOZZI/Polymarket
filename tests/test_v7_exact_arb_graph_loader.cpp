#include "pm/v7_exact_arb_graph_loader.hpp"
#include "pm/v7_exact_arb_graph_shadow.hpp"
#include "pm/v7_exact_arb_evidence_json.hpp"

#include <boost/json.hpp>
#include <openssl/sha.h>
#include <cassert>
#include <fstream>
#include <iostream>
#include <sstream>
#include <cstdlib>
#include <chrono>

using namespace pm::v7::exact_arb_graph;
namespace json = boost::json;

json::array pair(const char* n, const char* d = "1") { return {n, d}; }

json::object body() {
    json::array legs;
    for (unsigned i = 0; i < 2; ++i) {
        legs.emplace_back(json::object{
            {"node_handle", i}, {"coefficient", pair("1")},
            {"minimum_order_microunits", 5000000}, {"fee_rate_nanos", 0}, {"fee_exponent", 1},
            {"tick_size_e4", 100},
            {"payout_vector", json::array{pair(i ? "0" : "1"), pair(i ? "1" : "0")}}});
    }
    return {{"schema", "polymarket_v7_exact_arb_native_bundle_v1"},
        {"order_share_quantum_microunits", 10000},
        {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
        {"real_capital_at_risk", false}, {"automatic_promotion", false}, {"execution_authority", false},
        {"model_sha", std::string(40, 'a')}, {"graph_generation", std::string(64, 'b')},
        {"nodes", json::array{
            json::object{{"node_handle", 0}, {"node_id", std::string(64, '1')}, {"token_id", "1001"}},
            json::object{{"node_handle", 1}, {"node_id", std::string(64, '2')}, {"token_id", "2001"}}}},
        {"relations", json::array{json::object{
            {"relation_handle", 0}, {"relation_id", "binary:1"}, {"economic_identity", std::string(64, 'c')},
            {"relation_family", "SAME_MARKET_BINARY_COMPLETE_SET"}, {"proof_hash", std::string(64, 'd')},
            {"states", json::array{"YES", "NO"}}, {"guaranteed_payout_microunits", 1000000},
            {"reserve_per_unit_microunits", 500}, {"sell_inventory", false},
            {"settlement_close_ms", 100000}, {"legs", std::move(legs)}}}},
        {"dependencies", json::array{json::array{0}, json::array{0}}}, {"excluded", json::array{}}};
}

json::object envelope(const json::object& payload) {
    const auto bytes = json::serialize(payload);
    std::array<unsigned char, 32> hash{};
    SHA256(reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size(), hash.data());
    std::string hex;
    for (auto byte : hash) { hex += "0123456789abcdef"[byte >> 4]; hex += "0123456789abcdef"[byte & 15]; }
    return {{"schema", "polymarket_v7_exact_arb_hotset_selection_v1"},
        {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
        {"real_capital_at_risk", false}, {"automatic_promotion", false}, {"execution_authority", false},
        {"selection_only", true}, {"source_valid", true}, {"source_actionable", false},
        {"selection_purpose", "CAUSAL_HOT_OBSERVATION_PRIORITY_ONLY"},
        {"source_evidence_quality", "NONATOMIC_PUBLIC_REST_SCREEN_ONLY"},
        {"model_sha", std::string(40, 'a')}, {"graph_generation", std::string(64, 'b')},
        {"timestamp_ms", 1000}, {"valid_until_ms", 1100},
        {"selected_relation_ids", json::array{"binary:1"}},
        {"native_runtime_bundle", json::object{{"payload", bytes}, {"sha256", hex}}}};
}

std::array<NativeTokenBinding, 2> bindings() {
    return {{{"1001", 11, 100, 1, 100000, 5000000, 0, 1},
             {"2001", 7, 100, 1, 100000, 5000000, 0, 1}}};
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--stdin") {
        // Cross-language fixture runner used by CTest. No network or orders.
        try {
            std::ostringstream bytes; bytes << std::cin.rdbuf();
            const auto selection = json::parse(bytes.str()).as_object();
            std::vector<NativeTokenBinding> subscribed;
            std::uint32_t handle = 1;
            for (const auto& value : selection.at("markets").as_array()) {
                const auto& m = value.as_object();
                for (const auto* side : {"yes_token", "no_token"}) {
                    subscribed.push_back({std::string(m.at(side).as_string()), handle++, 100,
                        json::value_to<std::int64_t>(m.at("start_timestamp_ms")),
                        json::value_to<std::int64_t>(m.at("end_timestamp_ms")), 5000000, 0, 1});
                }
            }
            auto loaded = load_native_generation(bytes.str(), std::string(selection.at("model_sha").as_string()),
                subscribed, json::value_to<std::int64_t>(selection.at("timestamp_ms")), 1000000000);
            auto engine = std::make_unique<NativeGraphRuntime>();
            assert(engine->publish(std::move(loaded.generation)));
            assert(engine->begin_frame(1000000000, 1));
            pm::v7::BookDeepSnapshot book;
            book.valid = book.lineage_continuous = 1; book.state_version = 1;
            book.receive_monotonic_ns = 1000000000;
            book.ask_level_count = book.bid_level_count = 1;
            book.ask_levels[0] = {4000, 5000000}; book.bid_levels[0] = {3900, 5000000};
            for (const auto& binding : subscribed) assert(engine->update_book(binding.book_handle, book));
            HotResources resources; resources.capital_microunits = 1000000000;
            json::array decisions;
            engine->end_frame({1000000000, 1000000000, 50000000}, resources,
                [&](const auto& o, const auto&, auto) noexcept {
                    decisions.push_back(json::object{{"handle", o.decision.relation_handle},
                        {"reject", static_cast<unsigned>(o.decision.reject)},
                        {"quantity", o.decision.quantity_microunits}, {"net", o.decision.net_pnl_microunits}});
                });
            std::cout << json::serialize(decisions) << '\n';
            return 0;
        } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 2; }
    }
    const auto token_bindings = bindings();
    auto load = [&](const json::object& s) {
        return load_native_generation(json::serialize(s), std::string(40, 'a'), token_bindings, 1000, 1000000000);
    };
    auto invalid = [&](const json::object& s) {
        bool rejected = false;
        try { (void)load(s); } catch (const std::exception&) { rejected = true; }
        assert(rejected);
    };
    const auto original = envelope(body());
    auto loaded = load(original);
    assert(loaded.generation.structurally_valid());
    assert(loaded.generation.valid_until_monotonic_ns == 1100000000);
    assert(loaded.generation.relations[0].legs[0].book_handle == 11);
    assert(loaded.generation.dependencies[0].token_handle == 7);
    auto runtime = std::make_unique<NativeGraphRuntime>();
    assert(runtime->publish(std::move(loaded.generation)));
    assert(runtime->begin_frame(1000000000, 1));
    pm::v7::BookDeepSnapshot b;
    b.valid = b.lineage_continuous = 1;
    b.state_version = 1;
    b.receive_monotonic_ns = 1000000000;
    b.ask_level_count = 1;
    b.ask_levels[0] = {4000, 5000000};
    assert(runtime->update_book(11, b));
    assert(runtime->update_book(7, b));
    HotResources resources;
    resources.capital_microunits = 1000000000;
    unsigned emitted = 0;
    runtime->end_frame({1000000000, 1000000000, 50000000}, resources,
        [&](const auto& o, const auto&, auto) noexcept {
            ++emitted;
            assert(o.decision.reject == HotReject::Accepted);
            assert(o.decision.net_pnl_microunits == 997500);
        });
    assert(emitted == 1);

    auto s = original;
    s["paper_only"] = false; invalid(s);
    s = original; s["source_valid"] = false; invalid(s);
    s = original; s["real_capital_at_risk"] = true; invalid(s);
    s = original; s["model_sha"] = std::string(40, 'f'); invalid(s);
    s = original; s["valid_until_ms"] = 1000; invalid(s);
    s = original; s["valid_until_ms"] = 200000; invalid(s);
    s = original; s["timestamp_ms"] = 1001; invalid(s);
    s = original; s["native_runtime_bundle"].as_object()["sha256"] = std::string(64, '0'); invalid(s);

    // These all have a newly calculated VALID digest. A hash cannot grant
    // semantic authority or make a false finite-state equality true.
    auto p = body();
    p["relations"].as_array()[0].as_object()["guaranteed_payout_microunits"] = 900000;
    invalid(envelope(p));
    p = body(); p["dependencies"].as_array()[0] = json::array{}; invalid(envelope(p));
    p = body(); p["relations"].as_array()[0].as_object()["reserve_per_unit_microunits"] = 0; invalid(envelope(p));
    p = body(); p["relations"].as_array()[0].as_object()["settlement_close_ms"] = 999; invalid(envelope(p));
    p = body(); p["relations"].as_array()[0].as_object()["states"] = json::array{"YES", "YES"}; invalid(envelope(p));
    for (const auto* key : {"fee_rate_nanos", "fee_exponent", "tick_size_e4", "minimum_order_microunits"}) {
        p = body();
        p["relations"].as_array()[0].as_object()["legs"].as_array()[0].as_object()[key] = 999;
        invalid(envelope(p));
    }
    p = body();
    p["relations"].as_array()[0].as_object()["legs"].as_array()[0].as_object()["payout_vector"]
        = json::array{pair("1", "0"), pair("0")};
    invalid(envelope(p));
    p = body();
    p["relations"].as_array()[0].as_object()["legs"].as_array()[1].as_object()["node_handle"] = 0;
    invalid(envelope(p));
    p = body(); p["nodes"].as_array()[1].as_object()["token_id"] = "1001"; invalid(envelope(p));
    p = body(); p["nodes"].as_array()[1].as_object()["node_id"] = std::string(64, '1'); invalid(envelope(p));
    p = body(); p["authenticated_execution"] = true; invalid(envelope(p));
    p = body(); p["nodes"].as_array()[0].as_object()["token_id"] = "unsubscribed"; invalid(envelope(p));

    // Exercise the real WS decoder -> native kernel -> bounded writer pipeline.
    // The sink deliberately reports evaluations, not allocated opportunities.
    std::string temporary = (std::filesystem::temp_directory_path()/"pm-native-graph-test-XXXXXX").string();
    assert(mkdtemp(temporary.data()) != nullptr);
    const std::filesystem::path directory(temporary);
    {
        NativeGraphShadow shadow(directory, std::string(40, 'a'), "test-session",
                                 std::vector<NativeTokenBinding>(token_bindings.begin(), token_bindings.end()));
        const json::object policy{{"schema", "polymarket_v7_pure_arb_capital_policy_v1"},
            {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
            {"automatic_promotion", false}, {"paper_budget_pusd", 10000}};
        auto clock_ns=[] { return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count(); };
        const auto base_ns=clock_ns();
        auto shadow_selection=original; shadow_selection["valid_until_ms"]=61000;
        shadow.refresh(json::serialize(shadow_selection), json::serialize(policy), 1000, base_ns);
        pm::v7::MarketWsShard decoder({{"1001", 1, 1, 11, 100}, {"2001", 1, 1, 7, 100}});
        std::array<pm::v7::MarketWsEvent, 8> events;
        const std::string frame = R"([
          {"event_type":"book","asset_id":"1001","timestamp":1000,
           "bids":[{"price":"0.39","size":"5"}],"asks":[{"price":"0.40","size":"5"}]},
          {"event_type":"book","asset_id":"2001","timestamp":1000,
           "bids":[{"price":"0.39","size":"5"}],"asks":[{"price":"0.40","size":"5"}]}
        ])";
        auto feed = [&](std::int64_t mono) {
            pm::fast::FeedReceiveStamp stamp;
            stamp.wall_ms = 1000; stamp.monotonic_ns = mono;
            const auto result = decoder.process_frame(frame, stamp, events);
            assert(!result.invalid_frame && result.output_count == 2);
            shadow.on_frame(decoder, {events.data(), result.output_count}, {1000, mono, mono+1, mono}, 1, true, frame);
        };
        const auto first_ns=clock_ns();
        feed(first_ns);
        shadow.drain(false);
        shadow.write_status(1000);
        auto read_status = [&]() {
            std::ifstream input(directory/"native_exact_arb_status.json");
            std::ostringstream bytes; bytes << input.rdbuf();
            return json::parse(bytes.str()).as_object();
        };
        auto status = read_status();
        assert(status.at("state").as_string() == "RUNNING");
        assert(json::value_to<unsigned>(status.at("evaluations")) == 1);
        assert(json::value_to<unsigned>(status.at("full_evidence_written")) == 1);
        assert(status.at("execution_pnl").is_null());
        {
            std::ifstream evidence(directory/"native_exact_arb_full_evidence.jsonl");
            std::string line; std::getline(evidence, line);
            auto row = json::parse(line).as_object();
            assert(row.at("leg_books").as_array().size() == 2);
            assert(row.at("leg_books").as_array()[0].as_object().at("token_id").as_string() == "1001");
            assert(row.at("leg_books").as_array()[1].as_object().at("token_id").as_string() == "2001");
            assert(json::value_to<std::int64_t>(row.at("relation_valid_until_monotonic_ns")) == base_ns+60000000000);
            assert(json::value_to<unsigned>(row.at("control_admission_sequence")) == 3);
            assert(!row.at("actionable").as_bool());
            assert(!row.at("economic_execution_verified").as_bool());
            assert(row.at("order_quantity_precision_ready").as_bool());
            assert(row.at("sizing_model").as_string() == "PER_L2_LEVEL_5DP_EXACT_ORDER_LATTICE_V1");
            assert(row.at("sizing_proof_scope").as_string() == "RECORDED_MODEL_ONLY_NOT_VERIFIED_VENUE_EXECUTION");
            assert(row.at("global_size_optimum_proven").as_bool());
            assert(!row.at("sizing_search_exhausted").as_bool());
            assert(!row.at("economic_execution_verified").as_bool());
            assert(json::value_to<unsigned>(row.at("observation_sequence")) == 1);
            assert(json::value_to<unsigned>(row.at("continuity_serial")) > 0);
            assert(json::value_to<std::int64_t>(row.at("evaluation_valid_until_monotonic_ns")) == first_ns+1000000000);
            const auto& near = row.at("near_arbitrage").as_object();
            assert(near.at("valid").as_bool());
            assert(json::value_to<std::int64_t>(near.at("raw_distance_nano")) == -200000000);
            assert(row.at("rejection_reason").as_string() == "ACCEPTED_PRE_ALLOCATION");
            assert(row.at("native_bundle_sha256") == original.at("native_runtime_bundle").as_object().at("sha256"));
        }
        // Producer overload drops telemetry explicitly; it cannot wait on I/O.
        for (unsigned i = 1; i <= 5000; ++i) feed(clock_ns());
        shadow.drain(true); shadow.write_status(1000);
        status = read_status();
        assert(json::value_to<unsigned>(status.at("evaluations")) == 5001);
        assert(json::value_to<unsigned>(status.at("observations_dropped")) > 0);
        assert(json::value_to<unsigned>(status.at("full_evidence_dropped")) > 0);
        assert(json::value_to<unsigned>(status.at("disk_suppressed")) == 4096);
        assert(json::value_to<unsigned>(status.at("full_disk_suppressed")) == 8);
        feed(clock_ns()); shadow.drain(false);
        {
            std::ifstream observations(directory/"native_exact_arb_observations.jsonl");
            std::string line; std::getline(observations, line); std::getline(observations, line);
            assert(json::value_to<unsigned>(json::parse(line).as_object().at("observation_sequence")) == 5002);
        }
        shadow.refresh("{}", json::serialize(policy), 1000, clock_ns());
        feed(clock_ns()); shadow.drain(false); shadow.write_status(1000);
        status = read_status();
        assert(status.at("state").as_string() == "BLOCKED");
        assert(json::value_to<unsigned>(status.at("evaluations")) == 5002);
        // An unchanged success may recover only through fresh validation, and
        // a read at expiration must not manufacture a renewed source lease.
        shadow.refresh(json::serialize(original), json::serialize(policy), 1100, clock_ns());
        shadow.write_status(1100);
        assert(read_status().at("state").as_string() == "BLOCKED");
        // The control plane preserves every admission/invalidation independently
        // of market updates and observation-queue overload.
        std::ifstream journal(directory/"native_exact_arb_control.jsonl");
        std::string previous; std::uint64_t sequence=0; std::int64_t last_stamp=0;
        unsigned admissions=0, invalidations=0, checkpoints=0;
        for (std::string line; std::getline(journal,line);) {
            const auto event=json::parse(line).as_object();
            assert(json::value_to<std::uint64_t>(event.at("sequence"))==++sequence);
            assert(event.at("previous_record_sha256").as_string()==previous);
            const auto when=json::value_to<std::int64_t>(event.at("timestamp_monotonic_ns"));
            assert(when>=last_stamp);last_stamp=when;
            previous=evidence_sha256(line);
            if (event.at("kind").as_string()=="ADMIT") {
                ++admissions; assert(sequence==3);
                assert(event.at("native_bundle_sha256")==original.at("native_runtime_bundle").as_object().at("sha256"));
                assert(json::value_to<std::int64_t>(event.at("valid_until_monotonic_ns"))==base_ns+60000000000);
            } else if (event.at("kind").as_string()=="INVALIDATE") ++invalidations;
            else { assert(event.at("kind").as_string()=="CHECKPOINT"); ++checkpoints; }
        }
        assert(admissions==1 && invalidations>=4 && checkpoints>=3);
        status=read_status();
        assert(!status.at("control_journal_failed").as_bool());
        assert(status.at("control_record_sha256").as_string()==previous);
        assert(json::value_to<std::uint64_t>(status.at("control_records_written"))==sequence);
    }
    if (argc == 2 && std::string_view(argv[1]) == "--emit-evidence") {
        json::object fixture;
        json::array rows;
        std::ifstream input(directory/"native_exact_arb_observations.jsonl");
        for (std::string line; std::getline(input, line);) rows.push_back(json::parse(line));
        fixture["rows"] = std::move(rows);
        fixture["native_runtime_bundle"] = original.at("native_runtime_bundle");
        json::array control;
        std::ifstream journal(directory/"native_exact_arb_control.jsonl");
        // Preserve exact producer serialization: the chain hashes bytes, not a
        // Python reserialization of a parsed JSON object.
        for (std::string line; std::getline(journal,line);) control.emplace_back(line);
        fixture["control_wires"] = std::move(control);
        std::cout << json::serialize(fixture) << '\n';
    }
    std::filesystem::remove_all(directory);
}
