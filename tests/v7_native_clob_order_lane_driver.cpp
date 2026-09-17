#include "pm/v7_native_clob_order_lane.hpp"
#include "pm/v7_execution_plan.hpp"

#include <array>
#include <cassert>
#include <cstdlib>
#include <iostream>

using namespace pm::v7;

ExecutionPlan make_plan() {
    ExecutionPlan plan{};
    plan.intent.intent_id = 1;
    plan.intent.market_handle = 7;
    plan.intent.event_handle = 8;
    plan.intent.instrument_handle = 11;
    plan.intent.state_version = 9;
    plan.intent.decision_monotonic_ns = 1'000;
    plan.intent.price_tick = 50;
    plan.intent.quantity_microunits = 5'000'000;
    plan.intent.strategy_id = StrategyId::CryptoInformedTaker;
    plan.intent.type = IntentType::TargetPosition;
    plan.intent.side = Side::Buy;
    plan.intent.urgency = Urgency::Aggressive;
    plan.intent.purpose = IntentPurpose::Alpha;
    plan.tick_size_e4 = 100;
    plan.market_state_version = 9;
    plan.policy = ExecutionPolicyId::AggressiveTaker;
    return plan;
}
int main(int argc, char** argv) {
    if (argc != 3) return 2;
    const auto port = static_cast<std::uint16_t>(std::strtoul(argv[1], nullptr, 10));
    std::array<std::uint8_t, 32> private_key{};
    private_key.back() = 1; // Public secp256k1 test scalar, never a production key.

    NativeClobLaneConfig config{};
    config.chain_id = 80002;
    config.exchange_contract = "0xE111180000d2663C0091e4f400237545B87B996B";
    config.deposit_wallet = "0x1111111111111111111111111111111111111111";
    config.signer_eoa_address = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf";
    config.token_id_decimal = "1234";
    config.metadata_hex = "0x0000000000000000000000000000000000000000000000000000000000000000";
    config.builder_hex = "0x0000000000000000000000000000000000000000000000000000000000000000";
    config.api_key = "00000000-0000-0000-0000-000000000000";
    config.passphrase = "pass-test";
    config.l2_secret_base64 = "YWJj";
    config.host = "localhost";
    config.port = port;
    config.timeout_ms = 2'000;

    auto mismatched = config;
    mismatched.signer_eoa_address = "0x0000000000000000000000000000000000000001";
    NativeClobOrderLane rejected_identity(mismatched, private_key);
    assert(!rejected_identity.valid());

    NativeClobOrderLane lane(config, private_key);
    assert(lane.valid());
    assert(lane.connect(argv[2]));
    assert(lane.connected());
    NativeOrderTxOwner oms;
    const auto prepared = oms.prepare_submit(make_plan(), 1'100);
    assert(prepared.accepted);
    assert(prepared.oms.state == OrderState::SendPending);

    UserOmsBridge bridge;
    std::array<RoutedOmsEvent, 8> routed{};
    const auto result = lane.submit(
        oms, bridge, prepared.command, 1'710'000'000'000ULL, routed);
    assert(result.accepted);
    assert(result.reason == NativeClobSubmitReason::Accepted);
    assert(result.http_status == 200);
    assert(result.identity_bound);
    assert(result.wire_monotonic_ns > 0);
    assert(result.response_complete_monotonic_ns >= result.wire_monotonic_ns);

    const auto* record = oms.find(prepared.command.client_order_id);
    assert(record != nullptr);
    assert(record->state == OrderState::Live);
    assert(record->wire_ns == result.wire_monotonic_ns);
    assert(record->ack_ns == result.response_complete_monotonic_ns);
    const auto exchange = bridge.lookup_exchange(prepared.command.client_order_id);
    assert(exchange.found && exchange.exchange_order_id == "ex-native-1");
    lane.close();
    std::cout << "NATIVE_CLOB_ORDER_LANE_PASS\n";
    return 0;
}
