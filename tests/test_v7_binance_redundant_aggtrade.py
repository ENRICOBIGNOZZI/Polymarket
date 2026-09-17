from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_binance_redundant_aggtrade.hpp"

using namespace pm::v7::external_fair;

ExternalVenueEvent trade(std::uint64_t asset, std::uint64_t sequence,
                         std::uint64_t epoch, std::int64_t receive_ns,
                         double price = 100.0) {
    ExternalVenueEvent event{};
    event.asset_handle = asset;
    event.source_sequence = sequence;
    event.connection_epoch = epoch;
    event.venue = VenueId::BinanceSpot;
    event.event_type = ExternalEventType::Trade;
    event.exchange_event_ns = static_cast<std::int64_t>(sequence * 1000);
    event.local_receive_monotonic_ns = receive_ns;
    event.local_receive_wall_ns = receive_ns;
    event.trade_price = price;
    event.trade_size = 1.25;
    event.trade_side = 1;
    event.healthy = 1;
    return event;
}

int main() {
    RedundantBinanceAggTradeShadowGate gate;
    if (!gate.register_asset(101) || !gate.register_asset(202)) return 1;

    auto a = gate.on_event(0, trade(101, 100, 1, 1000));
    if (!a.first_arrival_shadow || a.duplicate || a.quorum_confirmed) return 2;

    auto b = gate.on_event(1, trade(101, 100, 1, 1020));
    if (!b.duplicate || !b.quorum_confirmed || b.first_to_quorum_ns != 20) return 3;

    auto c = gate.on_event(2, trade(101, 100, 1, 1030));
    if (!c.duplicate || c.quorum_confirmed) return 4;

    auto d = gate.on_event(2, trade(101, 101, 1, 1040));
    if (!d.first_arrival_shadow) return 5;
    auto e = gate.on_event(0, trade(101, 101, 1, 1050));
    if (!e.quorum_confirmed || e.first_to_quorum_ns != 10) return 6;

    auto conflict = gate.on_event(1, trade(101, 101, 1, 1060, 101.0));
    if (!conflict.conflict || !conflict.asset_poisoned || !gate.asset_poisoned(101)) return 7;

    auto poisoned = gate.on_event(0, trade(101, 102, 1, 1070));
    if (poisoned.first_arrival_shadow || !poisoned.asset_poisoned) return 8;
    if (!gate.mark_asset_recovered(101, 102)) return 9;

    auto recovered = gate.on_event(0, trade(101, 103, 1, 1080));
    if (!recovered.first_arrival_shadow || recovered.asset_poisoned) return 10;

    auto gap = trade(101, 104, 1, 1090);
    gap.gap = 1;
    auto gap_result = gate.on_event(1, gap);
    if (!gap_result.lane_unhealthy) return 11;

    auto other_lane = gate.on_event(2, trade(101, 104, 1, 1100));
    if (!other_lane.first_arrival_shadow) return 12;

    auto reconnect = gate.on_event(1, trade(101, 105, 2, 1110));
    if (!reconnect.lane_unhealthy || reconnect.first_arrival_shadow) return 13;
    if (!gate.mark_lane_recovered(1, 2)) return 14;
    auto after_recovery = gate.on_event(1, trade(101, 105, 2, 1110));
    if (!after_recovery.first_arrival_shadow) return 15;

    auto first_106 = gate.on_event(0, trade(101, 106, 1, 1200));
    if (!first_106.first_arrival_shadow) return 16;
    auto causal_bad = gate.on_event(2, trade(101, 106, 1, 1190));
    if (!causal_bad.causal_order_violation || !causal_bad.asset_poisoned) return 17;

    if (!gate.mark_asset_recovered(101, 106)) return 18;
    auto independent = gate.on_event(2, trade(202, 1, 1, 1300, 200.0));
    if (!independent.first_arrival_shadow || gate.asset_poisoned(202)) return 19;

    auto invalid = trade(101, 107, 1, 1310);
    invalid.venue = VenueId::CoinbaseSpot;
    if (!gate.on_event(0, invalid).invalid) return 20;

    auto stale = gate.on_event(0, trade(101, 105, 1, 1320));
    if (!stale.stale || stale.first_arrival_shadow) return 21;
    return 0;
}
'''


class RedundantAggTradeGateTest(unittest.TestCase):
    def test_shadow_gate_invariants(self) -> None:
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "main.cpp"
            binary = Path(tmp) / "gate-test"
            source.write_text(PROGRAM)
            subprocess.run(
                [
                    compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                    f"-I{ROOT / 'include'}", str(source), "-o", str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
