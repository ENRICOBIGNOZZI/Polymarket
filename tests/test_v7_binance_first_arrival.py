from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_binance_first_arrival.hpp"
#include <cstdint>

using namespace pm::v7::external_fair;

ExternalVenueEvent trade(std::uint64_t seq, std::int64_t receive_ns,
                         double price = 100.0, std::uint64_t asset = 7) {
    ExternalVenueEvent e{};
    e.asset_handle = asset;
    e.source_sequence = seq;
    e.connection_epoch = 1;
    e.venue = VenueId::BinanceSpot;
    e.event_type = ExternalEventType::Trade;
    e.exchange_event_ns = 1'000'000 + static_cast<std::int64_t>(seq);
    e.local_receive_monotonic_ns = receive_ns;
    e.local_receive_wall_ns = receive_ns + 1'000;
    e.trade_price = price;
    e.trade_size = 0.25;
    e.trade_side = 1;
    e.healthy = 1;
    return e;
}

int main() {
    BinanceAggTradeFirstArrivalGate gate(7);

    auto a = gate.observe(0, trade(100, 1'000));
    if (a.disposition != BinanceFirstArrivalDisposition::First || !a.emit_first) return 1;
    if (a.lane_mask != 1 || a.source_sequence != 100) return 2;

    auto d = gate.observe(0, trade(100, 1'010));
    if (d.disposition != BinanceFirstArrivalDisposition::SameLaneDuplicate) return 3;

    auto c = gate.observe(1, trade(100, 1'020));
    if (c.disposition != BinanceFirstArrivalDisposition::IndependentConfirm) return 4;
    if (c.independent_confirmations != 1 || c.lane_mask != 3) return 5;

    auto bad = gate.observe(2, trade(100, 1'030, 101.0));
    if (bad.disposition != BinanceFirstArrivalDisposition::Conflict || !bad.conflict) return 6;
    BinanceAggTradeFirstArrivalGate delayed_confirm_gate(7);
    if (delayed_confirm_gate.observe(0, trade(200, 1'000)).disposition
        != BinanceFirstArrivalDisposition::First) return 14;
    if (delayed_confirm_gate.observe(0, trade(201, 1'010)).disposition
        != BinanceFirstArrivalDisposition::First) return 15;
    auto delayed_confirm = delayed_confirm_gate.observe(1, trade(200, 1'020));
    if (delayed_confirm.disposition != BinanceFirstArrivalDisposition::IndependentConfirm) return 16;

    auto newer = gate.observe(0, trade(100 + kBinanceFirstArrivalSlots, 2'000));
    if (newer.disposition != BinanceFirstArrivalDisposition::First || !newer.emit_first) return 7;

    auto stale_other = gate.observe(1, trade(99, 2'200));
    if (stale_other.disposition != BinanceFirstArrivalDisposition::StaleSequence) return 8;

    BinanceAggTradeFirstArrivalGate btc_gate(7);
    BinanceAggTradeFirstArrivalGate eth_gate(8);
    if (btc_gate.observe(0, trade(10'000, 3'000, 100.0, 7)).disposition
        != BinanceFirstArrivalDisposition::First) return 9;
    // Aggregate-trade IDs are symbol-local, so each asset owns its gate.
    if (eth_gate.observe(1, trade(1, 3'010, 200.0, 8)).disposition
        != BinanceFirstArrivalDisposition::First) return 17;
    if (eth_gate.observe(2, trade(10'000, 3'020, 200.0, 8)).disposition
        != BinanceFirstArrivalDisposition::First) return 18;
    // Cross-asset traffic fails closed instead of corrupting another asset's sequence state.
    if (btc_gate.observe(0, trade(10'001, 3'030, 200.0, 8)).disposition
        != BinanceFirstArrivalDisposition::Invalid) return 19;

    auto invalid = trade(5000, 4'000);
    invalid.venue = VenueId::CoinbaseSpot;
    if (gate.observe(0, invalid).disposition != BinanceFirstArrivalDisposition::Invalid) return 10;

    if (gate.first_arrivals() != 2 || gate.independent_confirms() != 1) return 11;
    if (gate.same_lane_duplicates() != 1 || gate.conflicts() != 1) return 12;
    if (gate.stale_sequences() != 1) return 13;
    return 0;
}
'''


def test_binance_first_arrival_shadow_gate() -> None:
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "binance-first-arrival-test"
        subprocess.run(
            [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
             f"-I{ROOT / 'include'}",
             str(ROOT / "src/v7_binance_first_arrival.cpp"),
             "-x", "c++", "-", "-o", str(binary)],
            input=PROGRAM,
            text=True,
            check=True,
            capture_output=True,
        )
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    test_binance_first_arrival_shadow_gate()
