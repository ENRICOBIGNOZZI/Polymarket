#include "pm/v7_crypto_book_tape.hpp"
#include "pm/v7_external_tape.hpp"
#include "pm/v7_market_ws.hpp"

#include <cassert>
#include <cstdint>

int main() {
    using namespace pm::v7;
    using namespace pm::v7::research;
    using namespace pm::v7::external_fair;

    CryptoBookTapePayload source;
    source.connection_epoch = 7;
    source.receive_wall_ms = 1'788'620'000'123;
    source.market_handle = 11;
    source.event_handle = 12;
    source.schema_version = kCryptoBookTapeSchemaVersion;
    source.event_kind = static_cast<std::uint8_t>(MarketWsEventKind::BookChanged);
    source.outcome = CryptoBookOutcome::Yes;
    source.book.state_version = 99;
    source.book.exchange_event_ns = 1'788'620'000'100'000'000LL;
    source.book.receive_monotonic_ns = 5'273'000'123'456'789LL;
    source.book.tick_size_e4 = 100;
    source.book.best_bid_e4 = 4'900;
    source.book.best_ask_e4 = 5'000;
    source.book.best_bid_microunits = 8'000'000;
    source.book.best_ask_microunits = 7'000'000;
    source.book.bid_level_count = 1;
    source.book.ask_level_count = 1;
    source.book.bid_levels[0] = {4'900, 8'000'000};
    source.book.ask_levels[0] = {5'000, 7'000'000};
    source.book.lineage_continuous = 1;
    source.book.valid = 1;

    const auto record = make_tape_record(
        TapeRecordKind::PmState, 13, source.book.receive_monotonic_ns, 1, source);
    assert(record.tape_sequence == 13);
    assert(record.kind == TapeRecordKind::PmState);
    assert(record.payload_size == sizeof(CryptoBookTapePayload));

    CryptoBookTapePayload decoded;
    assert(decode_tape_payload(record, decoded));
    assert(decoded.connection_epoch == source.connection_epoch);
    assert(decoded.receive_wall_ms == source.receive_wall_ms);
    assert(decoded.schema_version == kCryptoBookTapeSchemaVersion);
    assert(decoded.event_kind == source.event_kind);
    assert(decoded.outcome == CryptoBookOutcome::Yes);
    assert(decoded.book.state_version == source.book.state_version);
    assert(decoded.book.exchange_event_ns == source.book.exchange_event_ns);
    assert(decoded.book.receive_monotonic_ns == source.book.receive_monotonic_ns);
    assert(decoded.book.best_bid_e4 == 4'900);
    assert(decoded.book.best_ask_e4 == 5'000);
    assert(decoded.book.bid_levels[0].quantity_microunits == 8'000'000);
    assert(decoded.book.ask_levels[0].quantity_microunits == 7'000'000);
    assert(decoded.book.lineage_continuous == 1);
    assert(decoded.book.valid == 1);
    return 0;
}
