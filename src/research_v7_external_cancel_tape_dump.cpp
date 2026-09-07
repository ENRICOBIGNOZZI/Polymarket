#include "pm/v7_crypto_book_tape.hpp"
#include "pm/v7_external_fair.hpp"
#include "pm/v7_external_tape.hpp"

#include <array>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <string>

using namespace pm::v7;
using namespace pm::v7::research;
using namespace pm::v7::external_fair;

namespace {
bool valid_header(const TapeSessionHeader& h) {
    static constexpr std::array<char,8> magic{'P','M','V','7','T','A','P','E'};
    return h.magic == magic && h.record_bytes == sizeof(TapeRecord)
        && h.schema_version >= kExternalTapeOldestReplaySchemaVersion
        && h.schema_version <= kExternalTapeSchemaVersion;
}

template<class Fn>
bool read_tape(const std::string& path, Fn&& fn) {
    std::ifstream in(path, std::ios::binary);
    if (!in) { std::cerr << "open_failed:" << path << "\n"; return false; }
    TapeSessionHeader h{};
    if (!in.read(reinterpret_cast<char*>(&h), sizeof(h)) || !valid_header(h)) {
        std::cerr << "invalid_header:" << path << "\n"; return false;
    }
    TapeRecord r{};
    while (in.read(reinterpret_cast<char*>(&r), sizeof(r))) fn(r);
    if (!in.eof()) { std::cerr << "truncated_record:" << path << "\n"; return false; }
    return true;
}
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: polymarket_v7_research_external_cancel_tape_dump (--book|--external) PATH [PATH ...]\n";
        return 2;
    }
    const std::string mode = argv[1];
    std::cout << std::setprecision(17);
    if (mode == "--book") {
        std::cout << "seq,receive_ms,receive_mono_ns,outcome,kind,book_valid,lineage_continuous,bid,ask,bidq,askq,bid5,ask5,bid10,ask10,trade_price,trade_qty,trade_side\n";
        bool ok = true;
        for (int i=2;i<argc;++i) ok = read_tape(argv[i], [&](const TapeRecord& r) {
            if (r.kind != TapeRecordKind::PmState || r.payload_size != sizeof(CryptoBookTapePayload)) return;
            CryptoBookTapePayload p{}; std::memcpy(&p, r.payload.data(), sizeof(p));
            if (p.schema_version != kCryptoBookTapeSchemaVersion) return;
            auto q=[](std::int64_t x){ return static_cast<double>(x)/1e6; };
            std::cout << r.tape_sequence << ',' << p.receive_wall_ms << ',' << r.receive_monotonic_ns << ','
                << static_cast<int>(p.outcome) << ',' << static_cast<int>(p.event_kind) << ','
                << static_cast<int>(p.book.valid) << ',' << static_cast<int>(p.book.lineage_continuous) << ','
                << static_cast<double>(p.book.best_bid_e4)/1e4 << ',' << static_cast<double>(p.book.best_ask_e4)/1e4 << ','
                << q(p.book.best_bid_microunits) << ',' << q(p.book.best_ask_microunits) << ','
                << q(p.book.bid_depth.l5_microunits) << ',' << q(p.book.ask_depth.l5_microunits) << ','
                << q(p.book.bid_depth.l10_microunits) << ',' << q(p.book.ask_depth.l10_microunits) << ','
                << static_cast<double>(p.trade_price_e4)/1e4 << ',' << q(p.trade_quantity_microunits) << ','
                << static_cast<int>(p.trade_side) << '\n';
        }) && ok;
        return ok ? 0 : 1;
    }
    if (mode == "--external") {
        std::cout << "seq,record_receive_mono_ns,receive_wall_ns,venue,event_type,bid,ask,bid_size,ask_size,trade_price,trade_size,trade_side,healthy\n";
        bool ok = true;
        for (int i=2;i<argc;++i) ok = read_tape(argv[i], [&](const TapeRecord& r) {
            if (r.kind != TapeRecordKind::ExternalVenueEvent || r.payload_size != sizeof(ExternalVenueEvent)) return;
            ExternalVenueEvent e{}; std::memcpy(&e, r.payload.data(), sizeof(e));
            std::cout << r.tape_sequence << ',' << r.receive_monotonic_ns << ',' << e.local_receive_wall_ns << ','
                << static_cast<int>(e.venue) << ',' << static_cast<int>(e.event_type) << ','
                << e.bid << ',' << e.ask << ',' << e.bid_size << ',' << e.ask_size << ','
                << e.trade_price << ',' << e.trade_size << ',' << static_cast<int>(e.trade_side) << ','
                << static_cast<int>(e.healthy) << '\n';
        }) && ok;
        return ok ? 0 : 1;
    }
    std::cerr << "invalid_mode\n";
    return 2;
}
