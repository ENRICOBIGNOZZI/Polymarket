#include "pm/v7_native_runtime_evidence.hpp"
#include "pm/v7_signal_funnel.hpp"

#include <boost/json.hpp>

#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

using namespace pm::v7;
namespace fs = std::filesystem;
namespace json = boost::json;

int main() {
    NativeSignalFunnel funnel;
    for (int i = 0; i < 10000; ++i)
        funnel.observe(1, 100, 200, 300, 110'000'000'200LL, 90,
                       true, true, false, false, false);
    assert(funnel.counts[0] == 1 && funnel.counts[1] == 1);
    assert(funnel.counts[3] == 1 && funnel.counts[4] == 1 && funnel.counts[5] == 0);
    funnel.observe(1, 100, 210, 300, 110'000'000'200LL, 90, true, true, true, true, true);
    assert(funnel.counts[5] == 1 && funnel.counts[6] == 1 && funnel.counts[7] == 1);
    funnel.observe(2, 400, 600, 500, 110'000'000'600LL, 390, false, true, false, false, false);
    assert(funnel.counts[0] == 2 && funnel.counts[2] == 1);
    funnel.observe(1, 100, 210, 300, 110'000'000'200LL, 90, true, true, true, true, true);
    assert(funnel.counts[0] == 2); // delayed old observation cannot recount
    const auto root = fs::temp_directory_path() / "v7-native-evidence-test";
    fs::remove_all(root);

    NativeRuntimeEvidenceConfig config{};
    config.run_root = root.string();
    config.model_sha = std::string(40, 'a');
    config.run_id = "run-test";
    config.server_id = "server-test";
    config.asset = "BTC";
    config.horizon = "M5";
    config.market_id = "market-test";
    config.event_id = "event-test";
    config.yes_token_id = "yes-token";
    config.no_token_id = "no-token";
    config.fee_source = "TEST_AUTHORITATIVE_FEE";
    config.yes_instrument_handle = 11;
    config.no_instrument_handle = 12;
    config.close_wall_ns = 2'000'000'000LL;
    config.taker_fee_rate = 0.02;
    config.taker_fee_exponent = 1.0;
    config.taker_maximum_entry_price_e4 = 7'500;
    config.maker_execution_policy_hash = std::string(16, 'b');
    config.maker_execution_config_hash = std::string(16, 'c');
    config.maker_execution_semantics = "maker-paper-v7.2-bilateral-inventory";
    config.observation_capture_mode = "DECISIONS";
    assert(config.valid());
    auto signal_config = config;
    signal_config.signal_policy_sha256 = std::string(64, 'd');
    assert(signal_config.valid());
    signal_config.signal_policy_sha256 = std::string(40, 'd');
    assert(!signal_config.valid());
    auto window_config = config;
    window_config.observation_capture_mode = "DECISION_WINDOWS";
    assert(window_config.valid());

    NativeOrderCommand command{};
    command.command_id = 7;
    command.client_order_id = 19;
    command.intent_id = 3;
    command.market_handle = 1;
    command.event_handle = 1;
    command.instrument_handle = 11;
    command.market_state_version = 9;
    command.price_tick = 41;
    command.quantity_microunits = 2'000'000;
    command.decision_monotonic_ns = 1'000'000'000LL;
    command.queue_monotonic_ns = 1'000'100'000LL;
    command.tick_size_e4 = 100;
    command.side = Side::Buy;
    command.time_in_force = AdapterTimeInForce::Fak;

    {
        NativeRuntimeEvidenceWriter writer(config);
        NativeEvidenceEvent order{};
        order.kind = NativeEvidenceKind::OrderSubmitted;
        order.command = command;
        order.strategy_id = StrategyId::CryptoInformedTaker;
        order.policy = ExecutionPolicyId::AggressiveTaker;
        order.causal_exchange_event_ns = 1'700'000'000'000'000'000LL;
        order.causal_receive_monotonic_ns = 999'900'000LL;
        order.recorded_monotonic_ns = command.queue_monotonic_ns;
        assert(writer.publish(order));

        NativeEvidenceEvent fill{};
        fill.kind = NativeEvidenceKind::Fill;
        fill.command = command;
        fill.strategy_id = StrategyId::CryptoInformedTaker;
        fill.policy = ExecutionPolicyId::AggressiveTaker;
        fill.order_state = OrderState::Filled;
        fill.fill.client_order_id = command.client_order_id;
        fill.fill.command_id = command.command_id;
        fill.fill.instrument_handle = command.instrument_handle;
        fill.fill.side = command.side;
        fill.fill.price_tick = command.price_tick;
        fill.fill.tick_size_e4 = command.tick_size_e4;
        fill.fill.fill_microunits = command.quantity_microunits;
        fill.fill.exchange_event_ns = 1'700'000'000'100'000'000LL;
        fill.fill.receive_monotonic_ns = 1'000'200'000LL;
        fill.fill.taker = 1;
        assert(writer.publish(fill));

        NativeEvidenceEvent maker_order{};
        maker_order.kind = NativeEvidenceKind::OrderSubmitted;
        maker_order.command = command;
        maker_order.command.client_order_id = 20;
        maker_order.strategy_id = StrategyId::ProfessionalMaker;
        maker_order.policy = ExecutionPolicyId::PassiveMaker;
        maker_order.causal_exchange_event_ns = 1'700'000'000'200'000'000LL;
        maker_order.causal_receive_monotonic_ns = 1'000'300'000LL;
        maker_order.recorded_monotonic_ns = 1'000'400'000LL;
        assert(writer.publish(maker_order));

        NativeObservation observation{};
        observation.kind = 2; observation.instrument_handle = 11;
        observation.book_version = 9; observation.signal_version = 42;
        observation.receive_ns = 999'900'000; observation.trigger_ns = 999'950'000;
        observation.evaluated_grid_ns = 1'000'000'000; observation.valid_until_ns = 1'100'000'000;
        observation.decision_ns = 1'000'010'000; observation.close_ns = 2'000'010'000;
        observation.binance_return_100ms_bp = 1.25; observation.coinbase_return_100ms_bp = 0.30;
        observation.signal_return_bp = 1.25; observation.confirmed_non_opposing = 1;
        observation.signal_valid = 1; observation.valid = 1; observation.accepted = 1;
        observation.bid_e4 = 4000; observation.ask_e4 = 4100;
        observation.bid_prices[0] = 4000; observation.bid_quantities[0] = 3'000'000;
        observation.reason = 3; observation.direction = 1;
        observation.repricing_pair_valid = 1;
        observation.yes_bid_e4 = 4000; observation.yes_ask_e4 = 4100;
        observation.no_bid_e4 = 5900; observation.no_ask_e4 = 6000;
        observation.yes_bid_quantity = 3'000'000;
        observation.yes_ask_quantity = 4'000'000;
        observation.no_bid_quantity = 5'000'000;
        observation.no_ask_quantity = 6'000'000;
        observation.repricing_origin_signal_version = 42;
        observation.repricing_horizon_ms = 500;
        observation.external_valid = 1;
        observation.external_composite_price = 100.;
        observation.external_return_250ms = .01;
        observation.external_return_250ms_valid = 1;
        // A taker fill estimate may be logged for research without claiming
        // that its settlement-edge score is already fill-conditioned.
        observation.expected_fill_probability = .35;
        observation.expected_fill_probability_valid = 1;
        observation.economic_score_fill_conditioned = 0;
        assert(writer.publish_observation(observation));

        NativeObservation pure_arb{};
        pure_arb.kind = 7;
        pure_arb.instrument_handle = 11;
        pure_arb.book_version = 10;
        pure_arb.connection_epoch = 3;
        pure_arb.receive_ns = 1'000'020'000;
        pure_arb.exchange_ns = 1'700'000'000'250'000'000LL;
        pure_arb.observed_ns = 1'000'040'000;
        pure_arb.decision_ns = 1'000'040'000;
        pure_arb.close_ns = 2'000'010'000;
        pure_arb.valid = 1;
        pure_arb.accepted = 1;
        pure_arb.repricing_pair_valid = 1;
        pure_arb.yes_bid_e4 = 4000; pure_arb.yes_ask_e4 = 4100;
        pure_arb.no_bid_e4 = 5800; pure_arb.no_ask_e4 = 5900;
        pure_arb.pure_arb_kind = 1;
        pure_arb.pure_arb_shares_microunits = 5'000'000;
        pure_arb.pure_arb_decision_compute_ns = 7'000;
        pure_arb.pure_arb_receive_to_decision_ns = 20'000;
        pure_arb.pure_arb_reserve_per_share = .0005;
        pure_arb.pure_arb_gross_edge_per_share = .01;
        pure_arb.pure_arb_conservative_edge_per_share = .0095;
        pure_arb.pure_arb_marginal_edge_per_share = .008;
        pure_arb.pure_arb_gross_locked_pnl = .05;
        pure_arb.pure_arb_conservative_locked_pnl = .0475;
        pure_arb.pure_arb_yes_vwap = .40;
        pure_arb.pure_arb_no_vwap = .59;
        pure_arb.pure_arb_yes_levels_used = 2;
        pure_arb.pure_arb_no_levels_used = 1;
        assert(writer.publish_optional_observation(pure_arb));

        NativeEvidenceEvent probability_order = order;
        probability_order.command.client_order_id = 21;
        probability_order.probability = {.up=.60, .lower=.52, .upper=.68,
            .asof_ns=1'000'000'000, .max_input_receive_ns=999'900'000,
            .valid_until_ns=1'100'000'000, .version=1, .valid=1};
        probability_order.economics.accepted=1;
        probability_order.economics.expected_net_edge=.18;
        probability_order.economics.conservative_net_edge=.10;
        probability_order.economics.probability=.60;
        probability_order.economics.probability_lower=.52;
        probability_order.economics.fee_per_share=.004838;
        probability_order.economics.cost_per_share=.42;
        probability_order.economics.cost_ceiling_microdollars=840010;
        probability_order.probability_input_instrument=11;
        assert(writer.publish(probability_order));
        writer.stop();
        assert(writer.healthy());
        assert(writer.published() == 4);
        assert(writer.written() == 4);
        assert(writer.dropped() == 0);
    }

    {
        std::ifstream status_stream(root / "control" / "native_evidence" / "market-test.json");
        std::string status_payload((std::istreambuf_iterator<char>(status_stream)), {});
        const auto status = json::parse(status_payload).as_object();
        assert(status.at("asset").as_string() == "BTC");
        assert(status.at("horizon").as_string() == "M5");
        assert(status.at("decision_observations").as_int64() == 1);
        assert(status.at("accepted_decision_observations").as_int64() == 1);
        assert(status.at("rejected_decision_observations").as_int64() == 0);
        assert(status.at("pure_arb_shadow_observations").as_int64() == 1);
        assert(status.at("pure_arb_shadow_buy_cycles").as_int64() == 1);
        assert(status.at("pure_arb_shadow_sell_cycles").as_int64() == 0);
        assert(status.at("decision_reason_counts").as_object().at("EXPIRED_SIGNAL").as_int64() == 1);
    }

    std::size_t count = 0;
    bool saw_order = false;
    bool saw_fill = false;
    bool saw_maker_order = false;
    for (const auto& entry : fs::directory_iterator(root / "ledger" / "spool")) {
        if (!entry.is_regular_file()) continue;
        std::ifstream in(entry.path());
        std::string payload((std::istreambuf_iterator<char>(in)), {});
        auto value = json::parse(payload).as_object();
        assert(value.at("paper_only").as_bool());
        assert(!value.at("authenticated_execution").as_bool());
        assert(value.at("model_sha").as_string() == std::string(40, 'a'));
        const auto kind = std::string(value.at("event_type").as_string());
        const auto& metadata = value.at("metadata").as_object();
        assert(metadata.at("taker_maximum_entry_price_e4").as_int64() == 7'500);
        const auto& receipt = metadata.at("native_settlement_receipt").as_object();
        assert(receipt.at("owner").as_string() == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE");
        assert(!receipt.at("real_order_submission").as_bool());
        if (kind == "ORDER_SUBMITTED") {
            assert(value.at("limit_price").as_double() == 0.41);
            const bool has_probability = metadata.if_contains("probability_up") != nullptr;
            if (auto q=metadata.if_contains("probability_up")) {
                assert(q->as_double()==.60);
                assert(metadata.at("selected_probability_lower").as_double()==.52);
                assert(metadata.at("probability_input_token_id").as_string()=="yes-token");
                assert(metadata.at("probability_input_features").as_array().size()==18);
                assert(value.at("expected_ev").as_double()==.36);
            }
            if (metadata.at("component").as_string() == "professional_maker") {
                saw_maker_order = true;
                assert(metadata.at("action_value_semantics").as_string()
                    == "MAKER_FILL_CONDITIONED_ROBUST_EV_PER_SHARE");
                assert(value.at("intended_action").as_string() == "MAKE");
                assert(metadata.at("policy_hash").as_string() == std::string(16, 'b'));
                assert(metadata.at("config_hash").as_string() == std::string(16, 'c'));
                assert(metadata.at("execution_semantics_version").as_string()
                    == "maker-paper-v7.2-bilateral-inventory");
                assert(metadata.at("identity_provenance").as_string()
                    == "EXACT_RUNTIME_ARTIFACT_V1");
            } else {
                saw_order = true;
                assert(value.at("intended_action").as_string() == "TAKE");
                assert(metadata.at("action_value_semantics").as_string()
                    == (has_probability
                        ? "TAKER_SETTLEMENT_EDGE_NOT_FILL_CONDITIONED"
                        : "TAKER_RULE_NO_COMPARABLE_ACTION_SCORE"));
                assert(metadata.at("policy_hash").is_null());
                assert(metadata.at("config_hash").is_null());
                assert(metadata.at("execution_semantics_version").is_null());
            }
        } else if (kind == "FILL") {
            saw_fill = true;
            assert(value.at("fill_price").as_double() == 0.41);
            assert(value.at("filled_size").as_double() == 2.0);
            assert(value.at("fee").as_double() > 0.0);
            assert(value.at("fee_source").as_string() == "TEST_AUTHORITATIVE_FEE");
        }
        ++count;
    }
    assert(count == 4 && saw_order && saw_fill && saw_maker_order);
    std::size_t observation_files = 0;
    for (const auto& entry : fs::directory_iterator(root / "research/native_observations/run-test")) {
        if (entry.path().extension() != ".jsonl") continue;
        assert(fs::is_regular_file(entry.path().string() + ".closed.json"));
        std::ifstream input(entry.path()); std::string line; std::getline(input, line);
        const auto observation = json::parse(line).as_object();
        assert(observation.at("kind").as_int64() == 2);
        assert(observation.at("signal_version").as_int64() == 42);
        assert(observation.at("expected_fill_probability").as_double() == .35);
        assert(!observation.at("economic_score_fill_conditioned").as_bool());
        assert(!observation.at("selector_score_comparable").as_bool());
        assert(observation.at("token_id").as_string() == "yes-token");
        assert(observation.at("yes_token_id").as_string() == "yes-token");
        assert(observation.at("no_token_id").as_string() == "no-token");
        assert(observation.at("capture_id").is_string());
        assert(observation.at("model_artifact_hash").is_null());
        assert(observation.at("policy_hash").is_null());
        assert(observation.at("config_hash").is_null());
        assert(observation.at("execution_semantics_version").is_null());
        assert(!observation.at("execution_authority").as_bool());
        assert(observation.at("probability_forecast").is_null());
        const auto& features=observation.at("external_features").as_object();
        assert(features.at("return_250ms").as_double()==.01);
        assert(features.at("return_1s").is_null() && features.at("return_5s").is_null());
        assert(features.at("oracle_basis").is_null());
        assert(observation.at("capture_mode").as_string() == "DECISIONS");
        assert(observation.at("taker_maximum_entry_price_e4").as_int64() == 7'500);
        assert(observation.at("binance_return_100ms_bp").as_double() == 1.25);
        assert(observation.at("coinbase_return_100ms_bp").as_double() == 0.30);
        assert(observation.at("confirmed_non_opposing").as_bool());
        assert(observation.at("signal_valid").as_bool());
        assert(observation.at("accepted").as_bool());
        assert(observation.at("repricing_pair_valid").as_bool());
        assert(observation.at("yes_bid_e4").as_int64() == 4000);
        assert(observation.at("no_ask_e4").as_int64() == 6000);
        assert(observation.at("yes_bid_quantity").as_int64() == 3'000'000);
        assert(observation.at("yes_ask_quantity").as_int64() == 4'000'000);
        assert(observation.at("no_bid_quantity").as_int64() == 5'000'000);
        assert(observation.at("no_ask_quantity").as_int64() == 6'000'000);
        assert(observation.at("repricing_origin_signal_version").as_int64() == 42);
        assert(observation.at("repricing_horizon_ms").as_int64() == 500);
        assert(observation.at("signal_age_ns").as_int64() == 60'000);
        assert(observation.at("tte_ns").as_int64() == 1'000'000'000);
        assert(observation.at("decision_wall_ns").as_int64() > 0);
        assert(observation.at("trigger_wall_ns").as_int64() > 0);
        assert(observation.at("close_wall_ns").as_int64() > observation.at("decision_wall_ns").as_int64());
        assert(observation.at("bids").as_array().size() == 1);
        std::string pure_arb_line;
        assert(std::getline(input, pure_arb_line));
        const auto pure_arb_observation = json::parse(pure_arb_line).as_object();
        assert(pure_arb_observation.at("kind").as_int64() == 7);
        assert(!pure_arb_observation.at("execution_authority").as_bool());
        const auto& pure = pure_arb_observation.at("pure_arb_shadow").as_object();
        assert(!pure.at("execution_authority").as_bool());
        assert(pure.at("kind").as_string() == "BUY_COMPLETE_SET");
        assert(pure.at("shares_microunits").as_int64() == 5'000'000);
        assert(pure.at("reserve_per_share").as_double() == .0005);
        assert(pure.at("decision_compute_ns").as_int64() == 7'000);
        assert(pure.at("receive_to_decision_ns").as_int64() == 20'000);
        assert(pure_arb_observation.at("expected_net_edge").is_null());
        assert(pure_arb_observation.at("conservative_net_edge").is_null());
        ++observation_files;
    }
    assert(observation_files == 1); // Observations never increase the ledger rows.
    config.market_id = "next-market";
    {
        NativeRuntimeEvidenceWriter writer(config);
        NativeEvidenceEvent event{};
        event.command = command;
        event.kind = NativeEvidenceKind::OrderSubmitted;
        event.causal_exchange_event_ns = 1'700'000'000'000'000'000LL;
        event.causal_receive_monotonic_ns = 999'900'000LL;
        assert(writer.publish(event));
        writer.stop();
        assert(writer.healthy() && writer.written() == 1);
    }
    std::size_t rollover_count = 0;
    for (const auto& entry : fs::directory_iterator(root / "ledger" / "spool")) {
        if (entry.is_regular_file()) ++rollover_count;
    }
    assert(rollover_count == 5); // Local counter reuse must not overwrite records.
    assert(fs::is_regular_file(root / "control" / "native_evidence" / "market-test.json"));
    assert(fs::is_regular_file(root / "control" / "native_evidence" / "next-market.json"));
    fs::remove_all(root);
    std::cout << "native runtime evidence PASS\n";
    return 0;
}
