from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_user_oms_bridge.hpp"
#include <array>
#include <cassert>
#include <cstdint>
using namespace pm::v7;
using namespace pm::v7::user_ws;

int main() {
    UserOmsBridge bridge;
    std::array<RoutedOmsEvent, 32> routed{};

    // Taker fill arrives before HTTP ACK/orderID mapping: retain, do not guess.
    auto early = decode(R"({"event_type":"trade","id":"trade-early","taker_order_id":"ex-1","market":"m","asset_id":"a","side":"BUY","size":"2.5","price":"0.50","status":"MATCHED","trader_side":"TAKER","maker_orders":[]})", 1'000);
    assert(early.recognized && !early.invalid);
    auto rr = bridge.on_user_event(early.event, routed);
    assert(rr.output_count == 0);
    assert(bridge.snapshot().pending_fills == 1);

    // Exact ACK creates identity and flushes the retained fill in causal order.
    rr = bridge.on_post_order_ack(
        11,
        R"({"success":true,"errorMsg":"","orderID":"ex-1","status":"live"})",
        1'100,
        routed);
    assert(!rr.invalid_ack && !rr.identity_conflict);
    assert(rr.output_count == 2);
    assert(routed[0].client_order_id == 11);
    assert(routed[0].event.type == OmsEventType::AckLive);
    assert(routed[0].event.timestamp_ns == 1'100);
    assert(routed[1].event.type == OmsEventType::FillDelta);
    assert(routed[1].event.timestamp_ns == 1'000);
    assert(routed[1].event.fill_delta_microunits == 2'500'000);
    assert(bridge.snapshot().pending_fills == 0);

    // Repeated MATCHED for the same exact trade/order pair never double-fills.
    rr = bridge.on_user_event(early.event, routed);
    assert(rr.output_count == 0 && rr.duplicate_fill);
    assert(bridge.snapshot().duplicate_fills == 1);

    // Maker correlation uses maker_orders[].order_id + matched_amount exactly.
    auto maker = decode(R"({"event_type":"trade","id":"trade-maker","taker_order_id":"foreign-taker","market":"m","asset_id":"a","side":"SELL","size":"5","price":"0.51","status":"MATCHED","trader_side":"MAKER","maker_orders":[{"order_id":"ex-maker","owner":"o","matched_amount":"1.25","price":"0.51","asset_id":"a","side":"BUY"},{"order_id":"foreign-maker","owner":"o","matched_amount":"3.75","price":"0.51","asset_id":"a","side":"BUY"}]})", 2'000);
    assert(maker.recognized && !maker.invalid);
    rr = bridge.on_user_event(maker.event, routed);
    assert(rr.output_count == 0);
    assert(bridge.snapshot().pending_fills == 3); // taker + two makers unresolved

    rr = bridge.on_post_order_ack(
        22,
        R"({"success":true,"orderID":"ex-maker","status":"matched"})",
        2'100,
        routed);
    assert(rr.output_count == 2);
    assert(routed[0].client_order_id == 22 && routed[0].event.type == OmsEventType::AckLive);
    assert(routed[1].client_order_id == 22 && routed[1].event.type == OmsEventType::FillDelta);
    assert(routed[1].event.fill_delta_microunits == 1'250'000);

    // Cancellation can also beat ACK and must replay after exact mapping.
    auto cancellation = decode(R"({"event_type":"order","id":"ex-cancel","market":"m","asset_id":"a","side":"SELL","price":"0.49","original_size":"3","size_matched":"0","type":"CANCELLATION"})", 3'000);
    assert(cancellation.recognized && !cancellation.invalid);
    rr = bridge.on_user_event(cancellation.event, routed);
    assert(rr.output_count == 0);
    assert(bridge.snapshot().pending_lifecycle == 1);
    rr = bridge.on_post_order_ack(
        33,
        R"({"success":true,"orderID":"ex-cancel","status":"live"})",
        3'100,
        routed);
    assert(rr.output_count == 2);
    assert(routed[0].event.type == OmsEventType::AckLive);
    assert(routed[1].event.type == OmsEventType::AckCancel);
    assert(routed[1].event.timestamp_ns == 3'000);

    // One exchange ID cannot be rebound to another client order.
    rr = bridge.on_post_order_ack(
        44,
        R"({"success":true,"orderID":"ex-1","status":"live"})",
        4'000,
        routed);
    assert(rr.identity_conflict && rr.output_count == 0);

    // Server-side rejection requires no exchange identity and routes Reject.
    rr = bridge.on_post_order_ack(
        55,
        R"({"success":false,"errorMsg":"insufficient balance"})",
        5'000,
        routed);
    assert(rr.output_count == 1);
    assert(routed[0].client_order_id == 55);
    assert(routed[0].event.type == OmsEventType::Reject);

    // Verify bridge events are compatible with canonical OMS transitions.
    StrategyIntent intent{};
    intent.intent_id = 7;
    intent.quantity_microunits = 10'000'000;
    intent.side = Side::Buy;
    OmsOrder order(intent, 77);
    OmsEvent queue{}; queue.event_id=1; queue.type=OmsEventType::QueueSend; queue.timestamp_ns=10;
    OmsEvent wire{}; wire.event_id=2; wire.type=OmsEventType::WireSend; wire.timestamp_ns=20;
    assert(order.apply(queue).applied);
    assert(order.apply(wire).applied);

    UserOmsBridge second;
    auto fill = decode(R"({"event_type":"trade","id":"trade-oms","taker_order_id":"ex-oms","market":"m","asset_id":"a","side":"BUY","size":"10","price":"0.50","status":"MATCHED","trader_side":"TAKER","maker_orders":[]})", 40);
    auto pre = second.on_user_event(fill.event, routed);
    assert(pre.output_count==0);
    auto post = second.on_post_order_ack(77, R"({"success":true,"orderID":"ex-oms","status":"matched"})", 30, routed);
    assert(post.output_count==2);
    assert(order.apply(routed[0].event).applied);
    assert(order.apply(routed[1].event).applied);
    assert(order.record().state == OrderState::Filled);
    assert(order.record().filled_microunits == 10'000'000);

    assert(second.release(77));
    assert(!second.lookup_exchange(77).found);
    return 0;
}
'''


def test_user_ws_to_oms_exact_identity_bridge() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        source = path / "main.cpp"
        binary = path / "user-oms-bridge-test"
        source.write_text(PROGRAM)
        subprocess.run(
            [
                cxx,
                "-std=c++20",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                f"-I{ROOT / 'include'}",
                "-I/opt/homebrew/include",
                str(ROOT / "src/boost_json.cpp"),
                str(ROOT / "src/v7_user_ws.cpp"),
                str(ROOT / "src/v7_clob_order_identity.cpp"),
                str(ROOT / "src/v7_oms.cpp"),
                str(ROOT / "src/v7_native_latency_tape.cpp"),
                str(ROOT / "src/v7_user_oms_bridge.cpp"),
                str(source),
                "-pthread",
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)
