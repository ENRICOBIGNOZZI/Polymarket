#define main v7_authorized_executor_program_main
#include "../src/v7_authorized_maker_paper_executor.cpp"
#undef main
#include <cassert>

int main() {
    const auto directory = fs::temp_directory_path() / ("v7-feature-test-" + std::to_string(::getpid()));
    fs::create_directories(directory / "micro_maker" / "book_features");
    fs::create_directories(directory / "external_fair");
    const std::string sha(40, 'a');
    const auto now = wall_ms();
    json::object row{{"schema", "polymarket_v7_causal_book_observation_v1"},
        {"model_sha", sha}, {"market_id", "market"}, {"token_id", "token"},
        {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
        {"valid", true}, {"features_valid", true}, {"lineage_continuous", true},
        {"receive_wall_ms", now}, {"observer_session_id", "session"}, {"connection_epoch", 1},
        {"observer_sequence", 5}, {"tick_size", .01}, {"best_bid", .5}, {"best_ask", .52},
        {"bid_depth_l1", 12.0}, {"placement_features", json::object{{"ofi", .2}, {"inventory_fraction", .7}}}};
    json::object status{{"model_sha", sha}, {"state", "running"}, {"evidence_complete", true},
        {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
        {"timestamp_ms", now}, {"observer_session_id", "session"}, {"connection_epoch", 1}};
    auto write = [&](const fs::path& path, const json::object& object) {
        std::ofstream output(directory / path); output << json::serialize(object);
    };
    auto enrich = [&](bool flat = true) {
        write("micro_maker/book_features/token.json", row);
        write("micro_maker/fillability_ws_status.json", status);
        SelectionEvidence selection;
        selection.generated_at_ms = now - 60000;
        selection.tick_size_e4 = 100;
        selection.queue_ahead_shares = 5;
        enrich_observed_features(selection, directory, sha, "market", "token", .5, flat);
        assert(selection.generated_at_ms == now - 60000); // No stale-authority refresh.
        return selection;
    };
    auto selection = enrich();
    assert(selection.feature_snapshot_id == "session:5");
    assert(selection.queue_ahead_shares == 12);
    assert(selection.placement_features.at("inventory_fraction").is_null());
    json::object account{{"code_sha", sha}, {"timestamp", now / 1000.0},
        {"paper_exploration_account", json::object{{"model_sha", sha}, {"complete", true},
            {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
            {"open_positions", 0}, {"pending_maker_orders", 0}}}};
    write("external_fair/paper_router_status.json", account);
    auto global_flat = enrich();
    assert(global_flat.placement_features.at("inventory_fraction").as_double() == 0.0);
    assert(global_flat.inventory_fraction_source == "CANONICAL_ACCOUNT_GLOBAL_FLAT");
    account["paper_exploration_account"].as_object()["open_positions"] = 1;
    account["paper_exploration_account"].as_object()["positions"] = json::object{
        {"other-position", json::object{{"market_id", "other-market"}}}};
    write("external_fair/paper_router_status.json", account);
    auto target_flat = enrich();
    assert(target_flat.placement_features.at("inventory_fraction").as_double() == 0.0);
    assert(target_flat.inventory_fraction_source == "CANONICAL_ACCOUNT_TARGET_MARKET_FLAT");
    account["paper_exploration_account"].as_object()["positions"] = json::object{
        {"same-position", json::object{{"market_id", "market"}}}};
    write("external_fair/paper_router_status.json", account);
    assert(enrich().placement_features.at("inventory_fraction").is_null());
    assert(enrich(false).placement_features.at("inventory_fraction").is_null());
    row["receive_wall_ms"] = now + 10000;
    assert(enrich().feature_snapshot_id.empty());
    row["receive_wall_ms"] = now - 10000;
    assert(enrich().feature_snapshot_id.empty());
    row["receive_wall_ms"] = now;
    status["connection_epoch"] = 2;
    assert(enrich().feature_snapshot_id.empty());
    status["connection_epoch"] = 1;
    row["tick_size"] = .001;
    assert(enrich().feature_snapshot_id.empty());
    fs::remove_all(directory);
}
