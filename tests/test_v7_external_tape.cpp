#include "pm/v7_external_fair.hpp"
#include "pm/v7_external_tape.hpp"

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <string>
#include <thread>

using namespace pm::v7::external_fair;

namespace {

std::filesystem::path segment_path(
    const std::filesystem::path& requested, std::uint64_t index) {
    std::ostringstream suffix;
    suffix << ".segment-" << std::setfill('0') << std::setw(6) << index;
    return requested.parent_path()
        / (requested.stem().string() + suffix.str() + requested.extension().string());
}

void remove_segments(const std::filesystem::path& requested) {
    std::filesystem::remove(requested);
    for (std::uint64_t index = 0; index < 8; ++index) {
        const auto segment = segment_path(requested, index);
        std::filesystem::remove(segment);
        std::filesystem::remove(segment.string() + ".open");
    }
}

void wait_until_written(
    const ExternalTapeRecorder& recorder, std::uint64_t expected) {
    for (int attempt = 0; attempt < 1000; ++attempt) {
        if (recorder.snapshot().written >= expected) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(false && "normalized tape writer did not drain");
}

void wait_until_written(
    const ExternalRawTapeRecorder& recorder, std::uint64_t expected) {
    for (int attempt = 0; attempt < 1000; ++attempt) {
        if (recorder.snapshot().written >= expected) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    assert(false && "raw tape writer did not drain");
}

} // namespace

int main() {
    const auto path =
        std::filesystem::temp_directory_path() / "pm_v7_external_tape_test.bin";
    remove_segments(path);
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

    const TapeSegmentationPolicy tiny_normalized_segment{
        sizeof(TapeSessionHeader) + sizeof(TapeRecord) + 1,
        60LL * 1'000'000'000LL,
    };
    {
        ExternalTapeRecorder recorder(
            path, sha, "run-1", "session-1", "external-fair", 1'000'000,
            tiny_normalized_segment);
        assert(std::filesystem::exists(
            segment_path(path, 0).string() + ".open"));
        assert(!std::filesystem::exists(segment_path(path, 0)));
        const auto first = make_tape_record(
            TapeRecordKind::OracleEvent, 1, 100, 1, oracle);
        const auto second = make_tape_record(
            TapeRecordKind::FairValueSnapshot, 2, 110, 2, fair);
        assert(recorder.try_record(first));
        assert(recorder.try_record(second));
        wait_until_written(recorder, 2);
        const auto snapshot = recorder.snapshot();
        assert(snapshot.accepted == 2);
        assert(snapshot.written == 2);
        assert(snapshot.dropped == 0);
        assert(snapshot.closed_segments == 1);
        assert(snapshot.active_segment_bytes >= sizeof(TapeSessionHeader));
        assert(snapshot.evidence_valid == 1);
    }

    const auto normalized_first = segment_path(path, 0);
    const auto normalized_second = segment_path(path, 1);
    assert(std::filesystem::exists(normalized_first));
    assert(std::filesystem::exists(normalized_second));
    assert(!std::filesystem::exists(normalized_first.string() + ".open"));
    assert(!std::filesystem::exists(normalized_second.string() + ".open"));
    assert(!std::filesystem::exists(path));

    bool overwrite_rejected = false;
    try {
        ExternalTapeRecorder duplicate(
            path, sha, "run-2", "session-2", "external-fair", 1'000'001);
    } catch (const std::invalid_argument&) {
        overwrite_rejected = true;
    }
    assert(overwrite_rejected);

    std::ifstream first_input(normalized_first, std::ios::binary);
    std::ifstream second_input(normalized_second, std::ios::binary);
    assert(first_input.good());
    assert(second_input.good());
    TapeSessionHeader first_header;
    TapeSessionHeader second_header;
    first_input.read(
        reinterpret_cast<char*>(&first_header), sizeof(first_header));
    second_input.read(
        reinterpret_cast<char*>(&second_header), sizeof(second_header));
    assert(first_input.good());
    assert(second_input.good());
    assert(std::string(first_header.magic.data(), first_header.magic.size())
           == "PMV7TAPE");
    assert(first_header.schema_version == kExternalTapeSchemaVersion);
    assert(first_header.record_bytes == sizeof(TapeRecord));
    assert(std::string(first_header.code_sha.data()) == sha);
    assert(std::string(first_header.run_id.data()) == "run-1");
    assert(second_header.creation_wall_ns >= first_header.creation_wall_ns);

    TapeRecord first;
    TapeRecord second;
    first_input.read(reinterpret_cast<char*>(&first), sizeof(first));
    second_input.read(reinterpret_cast<char*>(&second), sizeof(second));
    assert(first_input.good());
    assert(second_input.good());
    assert(first.tape_sequence == 1);
    assert(second.tape_sequence == 2);
    assert(first.receive_monotonic_ns < second.receive_monotonic_ns);

    OracleEvent oracle_roundtrip;
    FairValueSnapshot fair_roundtrip;
    assert(decode_tape_payload(first, oracle_roundtrip));
    assert(decode_tape_payload(second, fair_roundtrip));
    assert(oracle_roundtrip.value_numeric == oracle.value_numeric);
    assert(fair_roundtrip.fair_yes == fair.fair_yes);
    remove_segments(path);

    const auto raw_path = std::filesystem::temp_directory_path()
        / "pm_v7_external_raw_tape_test.bin";
    remove_segments(raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            raw_path, sha, "run-raw", "session-raw", "bybit-spot", 1'000'002);
        assert(std::filesystem::exists(
            segment_path(raw_path, 0).string() + ".open"));
        assert(raw_recorder.try_record_raw(
            VenueId::BybitSpot, 3, 120, 1'020,
            R"({"e":"depthUpdate"})"));
        assert(!raw_recorder.try_record_raw(
            VenueId::BybitSpot, 3, 121, 1'021,
            std::string(kExternalLargeRawTapePayloadBytes + 1, 'x')));
        wait_until_written(raw_recorder, 1);
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.written == 1);
        assert(raw_snapshot.dropped == 1);
        assert(raw_snapshot.dropped_payload_too_large == 1);
        assert(raw_snapshot.dropped_queue_full == 0);
        assert(raw_snapshot.evidence_valid == 0);
    }
    const auto raw_segment = segment_path(raw_path, 0);
    std::ifstream raw_input(raw_segment, std::ios::binary);
    TapeSessionHeader raw_header;
    RawTapeDiskRecordHeader raw_record;
    raw_input.read(reinterpret_cast<char*>(&raw_header), sizeof(raw_header));
    raw_input.read(reinterpret_cast<char*>(&raw_record), sizeof(raw_record));
    std::string raw_payload(raw_record.payload_size, '\0');
    raw_input.read(
        raw_payload.data(), static_cast<std::streamsize>(raw_payload.size()));
    assert(raw_input.good());
    assert(std::string(raw_header.magic.data(), raw_header.magic.size())
           == "PMV7RAW!");
    assert(raw_header.record_bytes == 0);
    assert(raw_record.venue == VenueId::BybitSpot);
    assert(raw_payload == R"({"e":"depthUpdate"})");
    remove_segments(raw_path);

    const auto raw_rotation_path = std::filesystem::temp_directory_path()
        / "pm_v7_external_raw_rotation_test.bin";
    remove_segments(raw_rotation_path);
    const TapeSegmentationPolicy tiny_raw_segment{
        sizeof(TapeSessionHeader) + sizeof(RawTapeDiskRecordHeader) + 4,
        60LL * 1'000'000'000LL,
    };
    {
        ExternalRawTapeRecorder raw_recorder(
            raw_rotation_path, sha, "run-raw-rotation", "session-raw-rotation",
            "binance-spot", 1'000'002, tiny_raw_segment);
        assert(raw_recorder.try_record_raw(
            VenueId::BinanceSpot, 3, 120, 1'020, "abc"));
        assert(raw_recorder.try_record_raw(
            VenueId::BinanceSpot, 3, 121, 1'021, "def"));
        wait_until_written(raw_recorder, 2);
        assert(raw_recorder.snapshot().closed_segments == 1);
    }
    assert(std::filesystem::exists(segment_path(raw_rotation_path, 0)));
    assert(std::filesystem::exists(segment_path(raw_rotation_path, 1)));
    remove_segments(raw_rotation_path);

    const auto large_raw_path = std::filesystem::temp_directory_path()
        / "pm_v7_external_large_raw_tape_test.bin";
    remove_segments(large_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            large_raw_path, sha, "run-large-raw", "session-large-raw",
            "coinbase-spot", 1'000'003);
        const std::string full_coinbase_l2_snapshot((1U << 20U) + 64U, 'x');
        assert(raw_recorder.try_record_raw(
            VenueId::CoinbaseSpot, 4, 130, 1'030,
            full_coinbase_l2_snapshot));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.evidence_valid == 1);
    }
    remove_segments(large_raw_path);

    const auto binance_large_raw_path = std::filesystem::temp_directory_path()
        / "pm_v7_external_binance_large_raw_tape_test.bin";
    remove_segments(binance_large_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            binance_large_raw_path, sha, "run-binance-large-raw",
            "session-binance-large-raw", "binance-spot", 1'000'004);
        assert(raw_recorder.try_record_raw(
            VenueId::BinanceSpot, 5, 139, 1'039,
            R"({"e":"depthUpdate"})"));
        const std::string full_binance_public_batch(
            kExternalRawTapePayloadBytes + 64U, 'x');
        assert(raw_recorder.try_record_raw(
            VenueId::BinanceSpot, 5, 140, 1'040,
            full_binance_public_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 2);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    remove_segments(binance_large_raw_path);

    const auto bybit_large_raw_path = std::filesystem::temp_directory_path()
        / "pm_v7_external_bybit_large_raw_tape_test.bin";
    remove_segments(bybit_large_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            bybit_large_raw_path, sha, "run-bybit-large-raw",
            "session-bybit-large-raw", "bybit-linear", 1'000'005);
        const std::string volatile_bybit_batch(
            2U * kExternalRawTapePayloadBytes + 64U, 'x');
        assert(raw_recorder.try_record_raw(
            VenueId::BybitLinear, 5, 140, 1'040,
            volatile_bybit_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    remove_segments(bybit_large_raw_path);

    const auto deribit_burst_raw_path = std::filesystem::temp_directory_path()
        / "pm_v7_external_deribit_burst_raw_tape_test.bin";
    remove_segments(deribit_burst_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            deribit_burst_raw_path, sha, "run-deribit-burst-raw",
            "session-deribit-burst-raw", "deribit", 1'000'006);
        const std::string deribit_catchup_batch(
            2U * kExternalRawTapePayloadBytes + 64U, 'x');
        assert(raw_recorder.try_record_raw(
            VenueId::Deribit, 6, 150, 1'050,
            deribit_catchup_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    remove_segments(deribit_burst_raw_path);
    return 0;
}
