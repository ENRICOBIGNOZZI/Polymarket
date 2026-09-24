#include "pm/v7_exact_arb_graph_loader.hpp"

#include <boost/json.hpp>
#include <boost/multiprecision/cpp_int.hpp>
#include <boost/rational.hpp>
#include <openssl/sha.h>

#include <map>
#include <set>
#include <stdexcept>

namespace pm::v7::exact_arb_graph {
namespace {
namespace json = boost::json;
using Big = boost::multiprecision::cpp_int;
using Rational = boost::rational<Big>;

[[noreturn]] void fail(const char* reason) { throw std::runtime_error(reason); }
std::string str(const json::value& v) {
    if (!v.is_string() || v.as_string().size() > 1024) fail("native_string");
    return std::string(v.as_string());
}
std::int64_t integer(const json::value& v) {
    if (v.is_int64()) return v.as_int64();
    if (v.is_uint64() && v.as_uint64() <= std::uint64_t(INT64_MAX)) return static_cast<std::int64_t>(v.as_uint64());
    fail("native_integer");
}
std::int64_t bounded(const json::value& v, std::int64_t low, std::int64_t high) {
    const auto n = integer(v);
    if (n < low || n > high) fail("native_integer_bounds");
    return n;
}
void flag(const json::object& o, const char* key, bool expected) {
    const auto* v = o.if_contains(key);
    if (!v || !v->is_bool() || v->as_bool() != expected) fail("native_paper_boundary");
}
void safety(const json::object& o) {
    flag(o, "paper_only", true);
    for (const auto* k : {"authenticated_execution", "real_order_submission", "real_capital_at_risk",
                           "automatic_promotion", "execution_authority"}) flag(o, k, false);
}
std::array<std::uint8_t, 32> digest(std::string_view s) {
    if (s.size() != 64) fail("native_digest_shape");
    std::array<std::uint8_t, 32> result{};
    auto nibble = [](char c) -> unsigned {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        fail("native_digest_shape");
    };
    for (std::size_t i = 0; i < result.size(); ++i) result[i] = nibble(s[2*i])*16 + nibble(s[2*i+1]);
    return result;
}
Big decimal(const json::value& v) {
    const auto s = str(v);
    if (s.empty() || s.size() > 64 || (s.size() > 1 && s[0] == '0')) fail("native_rational_encoding");
    Big n = 0;
    for (const auto c : s) {
        if (c < '0' || c > '9') fail("native_rational_encoding");
        n *= 10; n += c - '0';
    }
    return n;
}
Rational rational(const json::value& v) {
    const auto& pair = v.as_array();
    if (pair.size() != 2) fail("native_rational_shape");
    auto n = decimal(pair[0]), d = decimal(pair[1]);
    if (d == 0) fail("native_rational_denominator");
    return Rational(n, d);
}
std::int64_t i64(const Big& n) {
    if (n < 0 || n > INT64_MAX) fail("native_rational_overflow");
    return n.convert_to<std::int64_t>();
}
std::int64_t deadline(std::int64_t end, std::int64_t wall, std::int64_t mono) {
    if (end <= wall || end-wall > 120000 || mono <= 0 || mono > INT64_MAX-(end-wall)*1000000)
        fail("native_lease");
    return mono+(end-wall)*1000000;
}
} // namespace

LoadedNativeGeneration load_native_generation(
    std::string_view selection_json, std::string_view model_sha,
    std::span<const NativeTokenBinding> bindings, std::int64_t wall, std::int64_t mono) {
    if (selection_json.size() > 8*1024*1024 || wall <= 0 || mono <= 0
        || model_sha.size() != 40 || model_sha.find_first_not_of("0123456789abcdef") != std::string_view::npos
        || bindings.empty() || bindings.size() >= kRuntimeBooks) fail("native_input_bounds");
    const auto document = json::parse(selection_json);
    const auto& selection = document.as_object();
    safety(selection);
    flag(selection, "selection_only", true);
    flag(selection, "source_valid", true);
    flag(selection, "source_actionable", false);
    if (str(selection.at("schema")) != "polymarket_v7_exact_arb_hotset_selection_v1"
        || str(selection.at("model_sha")) != model_sha
        || str(selection.at("selection_purpose")) != "CAUSAL_HOT_OBSERVATION_PRIORITY_ONLY"
        || str(selection.at("source_evidence_quality")) != "NONATOMIC_PUBLIC_REST_SCREEN_ONLY") fail("native_selection_identity");
    const auto timestamp = bounded(selection.at("timestamp_ms"), 1, wall);
    const auto expiration = bounded(selection.at("valid_until_ms"), timestamp, INT64_MAX);
    if (expiration-timestamp > 120000) fail("native_lease_extension");
    LoadedNativeGeneration out;
    out.valid_until_wall_ms = expiration;
    auto& g = out.generation;
    g.source_valid_until_wall_ms = expiration;
    SHA256(reinterpret_cast<const unsigned char*>(selection_json.data()), selection_json.size(), g.selection_digest.data());
    g.valid_until_monotonic_ns = deadline(expiration, wall, mono);
    g.digest = digest(str(selection.at("graph_generation")));

    const auto& wire = selection.at("native_runtime_bundle").as_object();
    const auto& raw_payload = wire.at("payload").as_string();
    if (raw_payload.size() > 4*1024*1024) fail("native_payload_bounds");
    out.canonical_payload = std::string(raw_payload);
    out.bundle_sha256 = str(wire.at("sha256"));
    g.bundle_digest = digest(out.bundle_sha256);
    std::array<unsigned char, SHA256_DIGEST_LENGTH> computed{};
    SHA256(reinterpret_cast<const unsigned char*>(raw_payload.data()), raw_payload.size(), computed.data());
    if (computed != g.bundle_digest) fail("native_payload_digest");
    const auto parsed = json::parse(raw_payload);
    const auto& body = parsed.as_object();
    g.order_share_quantum_microunits = bounded(body.at("order_share_quantum_microunits"), 10000, 10000);
    safety(body);
    if (str(body.at("schema")) != "polymarket_v7_exact_arb_native_bundle_v1"
        || str(body.at("model_sha")) != model_sha
        || digest(str(body.at("graph_generation"))) != g.digest) fail("native_bundle_identity");
    std::map<std::string, const NativeTokenBinding*> tokens;
    std::set<std::uint32_t> book_handles;
    for (const auto& b : bindings) {
        if (b.token_id.empty() || b.book_handle == 0 || b.book_handle >= kRuntimeBooks
            || b.tick_size_e4 <= 0 || b.tick_size_e4 >= 10000
            || b.start_wall_ms <= 0 || b.end_wall_ms <= b.start_wall_ms
            || b.minimum_order_microunits <= 0 || b.fee_rate_nanos < 0 || b.fee_rate_nanos > 1000000000
            || b.fee_exponent < 0 || b.fee_exponent > 2
            || !tokens.emplace(b.token_id, &b).second || !book_handles.insert(b.book_handle).second)
            fail("native_binding");
    }
    std::set<std::string> selected_ids;
    for (const auto& id : selection.at("selected_relation_ids").as_array())
        if (!selected_ids.insert(str(id)).second) fail("native_selected_relation_duplicate");
    const auto& nodes = body.at("nodes").as_array();
    const auto& relations = body.at("relations").as_array();
    const auto& dependencies = body.at("dependencies").as_array();
    if (nodes.size() > bindings.size() || relations.size() > kRuntimeRelations
        || dependencies.size() != nodes.size()) fail("native_generation_bounds");
    std::vector<const NativeTokenBinding*> mapped;
    std::set<std::string> node_tokens;
    for (std::size_t i = 0; i < nodes.size(); ++i) {
        const auto& n = nodes[i].as_object();
        const auto token = str(n.at("token_id"));
        const auto found = tokens.find(token);
        if (integer(n.at("node_handle")) != static_cast<std::int64_t>(i) || found == tokens.end()
            || !node_tokens.insert(token).second) fail("native_node_binding");
        mapped.push_back(found->second);
        g.nodes.push_back({found->second->book_handle, digest(str(n.at("node_id")))});
        g.expected_ticks_e4[found->second->book_handle] = found->second->tick_size_e4;
    }
    std::vector<std::vector<std::uint32_t>> expected_dependencies(nodes.size());
    std::set<std::pair<std::string, bool>> portfolios;
    for (std::size_t h = 0; h < relations.size(); ++h) {
        const auto& row = relations[h].as_object();
        CompiledRelation r;
        r.enabled = 1;
        r.relation_handle = bounded(row.at("relation_handle"), h, h);
        const auto id = str(row.at("relation_id"));
        const auto economic_identity = str(row.at("economic_identity"));
        (void)digest(economic_identity);
        if (!selected_ids.count(id) || str(row.at("relation_family")).empty()) fail("native_relation_identity");
        r.sell_inventory = row.at("sell_inventory").as_bool();
        if (!portfolios.emplace(economic_identity, r.sell_inventory != 0).second) fail("native_duplicate_portfolio");
        const auto proof = digest(str(row.at("proof_hash")));
        for (unsigned i = 0; i < 8; ++i) r.proof_handle = (r.proof_handle << 8) | proof[i];
        r.guaranteed_payout_microunits = bounded(row.at("guaranteed_payout_microunits"), 1, INT64_MAX);
        r.reserve_per_unit_microunits = bounded(row.at("reserve_per_unit_microunits"), 0, INT64_MAX);
        // Preserve the frozen 5-bps guarantee-scaled baseline. Sensitivity arms
        // belong in separate diagnostics, never in native candidate admission.
        const auto reserve_floor = r.guaranteed_payout_microunits/2000
            + (r.guaranteed_payout_microunits%2000 != 0);
        if (r.reserve_per_unit_microunits < reserve_floor) fail("native_reserve_below_baseline");
        const auto& states = row.at("states").as_array();
        const auto& legs = row.at("legs").as_array();
        if (states.empty() || states.size() > 64 || legs.size() < 2 || legs.size() > kMaxLegs) fail("native_proof_shape");
        std::set<std::string> unique_states;
        for (const auto& s : states) if (!unique_states.insert(str(s)).second) fail("native_proof_states");
        std::vector<Rational> totals(states.size());
        auto relation_end = std::min(expiration, bounded(row.at("settlement_close_ms"), 1, INT64_MAX));
        std::int64_t relation_start = 0;
        std::array<bool, kRuntimeBooks> used{};
        r.leg_count = legs.size();
        for (std::size_t j = 0; j < legs.size(); ++j) {
            const auto& leg = legs[j].as_object();
            if (nodes.empty()) fail("native_missing_nodes");
            const auto n = bounded(leg.at("node_handle"), 0, nodes.size()-1);
            const auto& b = *mapped[n];
            if (used[b.book_handle]) fail("native_duplicate_leg");
            used[b.book_handle] = true;
            const auto coefficient = rational(leg.at("coefficient"));
            auto& l = r.legs[j];
            l.book_handle = b.book_handle;
            l.coefficient = {i64(coefficient.numerator()), i64(coefficient.denominator())};
            if (!l.coefficient.valid()) fail("native_coefficient");
            l.minimum_order_microunits = bounded(leg.at("minimum_order_microunits"), 1, INT64_MAX);
            const auto rate = bounded(leg.at("fee_rate_nanos"), 0, 1000000000);
            const auto exponent = bounded(leg.at("fee_exponent"), 0, 2);
            if (rate != b.fee_rate_nanos || exponent != b.fee_exponent
                || bounded(leg.at("tick_size_e4"), 1, 9999) != b.tick_size_e4
                || l.minimum_order_microunits < b.minimum_order_microunits) fail("native_venue_terms_mismatch");
            l.fee_rate = rate/1e9;
            l.fee_exponent = static_cast<double>(exponent);
            l.fee_verified = 1;
            const auto& payouts = leg.at("payout_vector").as_array();
            if (payouts.size() != states.size()) fail("native_proof_shape");
            for (std::size_t s = 0; s < states.size(); ++s) totals[s] += coefficient*rational(payouts[s]);
            expected_dependencies[n].push_back(h);
            relation_end = std::min(relation_end, b.end_wall_ms);
            relation_start = std::max(relation_start, b.start_wall_ms);
        }
        const Rational guarantee(Big(r.guaranteed_payout_microunits), Big(1000000));
        for (const auto& total : totals) if (total != guarantee) fail("native_payoff_proof_failed");
        if (relation_start > wall) fail("native_market_not_started");
        g.relation_deadlines_ns.push_back(deadline(relation_end, wall, mono));
        g.relations.push_back(r);
    }
    std::map<std::uint32_t, std::vector<std::uint32_t>> remapped;
    for (std::size_t i = 0; i < nodes.size(); ++i) {
        const auto& supplied = dependencies[i].as_array();
        if (supplied.size() != expected_dependencies[i].size() || supplied.size() > kMaxDependenciesPerToken)
            fail("native_dependencies");
        for (std::size_t j = 0; j < supplied.size(); ++j)
            if (integer(supplied[j]) != expected_dependencies[i][j]) fail("native_dependencies");
        remapped.emplace(mapped[i]->book_handle, expected_dependencies[i]);
    }
    for (const auto& [handle, edges] : remapped) {
        g.dependencies.push_back({handle, static_cast<std::uint32_t>(g.relation_handles.size()),
                                 static_cast<std::uint16_t>(edges.size())});
        g.relation_handles.insert(g.relation_handles.end(), edges.begin(), edges.end());
    }
    if (!g.structurally_valid()) fail("native_generation_structure");
    return out;
}
} // namespace pm::v7::exact_arb_graph
