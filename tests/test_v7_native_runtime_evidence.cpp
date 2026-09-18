#include "pm/v7_native_runtime_evidence.hpp"

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
    const auto root = fs::temp_directory_path() / "v7-native-evidence-test";
    fs::remove_all(root);

    NativeRuntimeEvidenceConfig config{};
    config.run_root = root.string();
    config.model_sha = std::string(40, 'a');
    config.run_id = "run-test";
    config.server_id = "server-test";
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
    assert(config.valid());

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
        writer.stop();
        assert(writer.healthy());
        assert(writer.published() == 2);
        assert(writer.written() == 2);
        assert(writer.dropped() == 0);
    }

    std::size_t count = 0;
    bool saw_order = false;
    bool saw_fill = false;
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
        const auto& receipt = metadata.at("native_settlement_receipt").as_object();
        assert(receipt.at("owner").as_string() == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE");
        assert(!receipt.at("real_order_submission").as_bool());
        if (kind == "ORDER_SUBMITTED") {
            saw_order = true;
            assert(value.at("intended_action").as_string() == "TAKE");
            assert(value.at("limit_price").as_double() == 0.41);
        } else if (kind == "FILL") {
            saw_fill = true;
            assert(value.at("fill_price").as_double() == 0.41);
            assert(value.at("filled_size").as_double() == 2.0);
            assert(value.at("fee").as_double() > 0.0);
            assert(value.at("fee_source").as_string() == "TEST_AUTHORITATIVE_FEE");
        }
        ++count;
    }
    assert(count == 2 && saw_order && saw_fill);
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
    assert(rollover_count == 3); // Local counter reuse must not overwrite records.
    assert(fs::is_regular_file(root / "control" / "native_evidence_status.json"));
    fs::remove_all(root);
    std::cout << "native runtime evidence PASS\n";
    return 0;
}
