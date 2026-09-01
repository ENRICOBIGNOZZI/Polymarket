#include "pm/v7_external_fair.hpp"
#include "pm/v7_external_tape.hpp"

#include <cassert>
#include <filesystem>
#include <fstream>
#include <string>

using namespace pm::v7::external_fair;

int main() {
    const auto path = std::filesystem::temp_directory_path() / "pm_v7_external_tape_test.bin";
    std::filesystem::remove(path);
    const std::string sha = "0123456789abcdef0123456789abcdef01234567";

    OracleEvent oracle;
    oracle.feed_handle = 1;
    oracle.source_sequence = 2;
    oracle.connection_epoch = 3;
    oracle.exact_decimal_handle = 4;
    oracle.value_numeric = 65000.125;
    oracle.oracle_observation_ns = 90;
    oracle.local_receive_monotonic_ns = 100;
    oracle.local_receive_wall_ns = 1000;
    oracle.window_seconds = 60;
    oracle.healthy = 1;

    FairValueSnapshot fair;
    fair.market_handle = 10;
    fair.model_version = 11;
    fair.fair_yes = 0.70;
    fair.fair_yes_lower = 0.65;
    fair.fair_yes_upper = 0.75;
    fair.calculated_monotonic_ns = 110;
    fair.valid_until_monotonic_ns = 200;
    fair.contract_verified = 1;
    fair.reference_verified = 1;
    fair.oracle_healthy = 1;
    fair.external_healthy = 1;
    fair.valid = 1;

    {
        ExternalTapeRecorder recorder(path, sha, "run-1", "session-1", "external-fair", 1'000'000);
        const auto first = make_tape_record(TapeRecordKind::OracleEvent, 1, 100, 1, oracle);
        const auto second = make_tape_record(TapeRecordKind::FairValueSnapshot, 2, 110, 2, fair);
        assert(recorder.try_record(first));
        assert(recorder.try_record(second));
        const auto snapshot = recorder.snapshot();
        assert(snapshot.accepted == 2);
        assert(snapshot.dropped == 0);
        assert(snapshot.evidence_valid == 1);
    }

    bool overwrite_rejected = false;
    try {
        ExternalTapeRecorder duplicate(path, sha, "run-2", "session-2", "external-fair", 1'000'001);
    } catch (const std::invalid_argument&) {
        overwrite_rejected = true;
    }
    assert(overwrite_rejected);

    std::ifstream input(path, std::ios::binary);
    assert(input.good());
    TapeSessionHeader header;
    input.read(reinterpret_cast<char*>(&header), sizeof(header));
    assert(input.good());
    assert(std::string(header.magic.data(), header.magic.size()) == "PMV7TAPE");
    assert(header.schema_version == kExternalTapeSchemaVersion);
    assert(header.record_bytes == sizeof(TapeRecord));
    assert(std::string(header.code_sha.data()) == sha);
    assert(std::string(header.run_id.data()) == "run-1");

    TapeRecord first;
    TapeRecord second;
    input.read(reinterpret_cast<char*>(&first), sizeof(first));
    input.read(reinterpret_cast<char*>(&second), sizeof(second));
    assert(input.good());
    assert(first.tape_sequence == 1);
    assert(second.tape_sequence == 2);
    assert(first.receive_monotonic_ns < second.receive_monotonic_ns);

    OracleEvent oracle_roundtrip;
    FairValueSnapshot fair_roundtrip;
    assert(decode_tape_payload(first, oracle_roundtrip));
    assert(decode_tape_payload(second, fair_roundtrip));
    assert(oracle_roundtrip.value_numeric == oracle.value_numeric);
    assert(fair_roundtrip.fair_yes == fair.fair_yes);

    std::filesystem::remove(path);
    const auto raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_raw_tape_test.bin";
    std::filesystem::remove(raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(raw_path, sha, "run-raw", "session-raw", "bybit-spot", 1'000'002);
        assert(raw_recorder.try_record_raw(VenueId::BybitSpot, 3, 120, 1'020, R"({"e":"depthUpdate"})"));
        assert(!raw_recorder.try_record_raw(VenueId::BybitSpot, 3, 121, 1'021,
                                             std::string(kExternalRawTapePayloadBytes + 1, 'x')));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 1);
        assert(raw_snapshot.dropped_payload_too_large == 1);
        assert(raw_snapshot.dropped_queue_full == 0);
        assert(raw_snapshot.evidence_valid == 0);
    }
    std::ifstream raw_input(raw_path, std::ios::binary);
    TapeSessionHeader raw_header;
    RawTapeDiskRecordHeader raw_record;
    raw_input.read(reinterpret_cast<char*>(&raw_header), sizeof(raw_header));
    raw_input.read(reinterpret_cast<char*>(&raw_record), sizeof(raw_record));
    std::string raw_payload(raw_record.payload_size, '\0');
    raw_input.read(raw_payload.data(), static_cast<std::streamsize>(raw_payload.size()));
    assert(raw_input.good());
    assert(std::string(raw_header.magic.data(), raw_header.magic.size()) == "PMV7RAW!");
    assert(raw_header.record_bytes == 0);
    assert(raw_record.venue == VenueId::BybitSpot);
    assert(raw_payload == R"({"e":"depthUpdate"})");
    std::filesystem::remove(raw_path);

    const auto large_raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_large_raw_tape_test.bin";
    std::filesystem::remove(large_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            large_raw_path, sha, "run-large-raw", "session-large-raw", "coinbase-spot", 1'000'003);
        const std::string full_coinbase_l2_snapshot((1U << 20U) + 64U, 'x');
        assert(raw_recorder.try_record_raw(VenueId::CoinbaseSpot, 4, 130, 1'030,
                                            full_coinbase_l2_snapshot));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.evidence_valid == 1);
    }
    std::filesystem::remove(large_raw_path);

    const auto binance_large_raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_binance_large_raw_tape_test.bin";
    std::filesystem::remove(binance_large_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            binance_large_raw_path, sha, "run-binance-large-raw", "session-binance-large-raw", "binance-spot", 1'000'004);
        assert(raw_recorder.try_record_raw(VenueId::BinanceSpot, 5, 139, 1'039,
                                            R"({"e":"depthUpdate"})"));
        const std::string full_binance_public_batch((kExternalRawTapePayloadBytes + 64U), 'x');
        assert(raw_recorder.try_record_raw(VenueId::BinanceSpot, 5, 140, 1'040,
                                            full_binance_public_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 2);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    std::filesystem::remove(binance_large_raw_path);

    const auto burst_raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_burst_raw_tape_test.bin";
    std::filesystem::remove(burst_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            burst_raw_path, sha, "run-burst-raw", "session-burst-raw", "bybit-linear", 1'000'005);
        const std::string volatile_bybit_batch((kExternalRawTapePayloadBytes + 64U), 'x');
        assert(raw_recorder.try_record_raw(VenueId::BybitLinear, 5, 140, 1'040,
                                            volatile_bybit_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    std::filesystem::remove(burst_raw_path);

    const auto deribit_burst_raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_deribit_burst_raw_tape_test.bin";
    std::filesystem::remove(deribit_burst_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            deribit_burst_raw_path, sha, "run-deribit-burst-raw", "session-deribit-burst-raw", "deribit", 1'000'006);
        const std::string deribit_catchup_batch((kExternalRawTapePayloadBytes + 64U), 'x');
        assert(raw_recorder.try_record_raw(VenueId::Deribit, 6, 150, 1'050,
                                            deribit_catchup_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    std::filesystem::remove(deribit_burst_raw_path);
    return 0;
}
