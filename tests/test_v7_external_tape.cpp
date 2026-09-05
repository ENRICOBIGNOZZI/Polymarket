// Assertions in this test invoke the enqueue API; they must run in Release too.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "pm/v7_external_fair.hpp"
#include "pm/v7_external_tape.hpp"

#include <cassert>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <algorithm>
#include <thread>
#include <chrono>
#include <unistd.h>
#include <sys/wait.h>

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
                                             std::string(kExternalLargeRawTapePayloadBytes + 1, 'x')));
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

    const auto bybit_large_raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_bybit_large_raw_tape_test.bin";
    std::filesystem::remove(bybit_large_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            bybit_large_raw_path, sha, "run-bybit-large-raw", "session-bybit-large-raw", "bybit-linear", 1'000'005);
        const std::string volatile_bybit_batch((2U * kExternalRawTapePayloadBytes + 64U), 'x');
        assert(raw_recorder.try_record_raw(VenueId::BybitLinear, 5, 140, 1'040,
                                            volatile_bybit_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    std::filesystem::remove(bybit_large_raw_path);

    const auto deribit_burst_raw_path = std::filesystem::temp_directory_path() / "pm_v7_external_deribit_burst_raw_tape_test.bin";
    std::filesystem::remove(deribit_burst_raw_path);
    {
        ExternalRawTapeRecorder raw_recorder(
            deribit_burst_raw_path, sha, "run-deribit-burst-raw", "session-deribit-burst-raw", "deribit", 1'000'006);
        const std::string deribit_catchup_batch((2U * kExternalRawTapePayloadBytes + 64U), 'x');
        assert(raw_recorder.try_record_raw(VenueId::Deribit, 6, 150, 1'050,
                                            deribit_catchup_batch));
        const auto raw_snapshot = raw_recorder.snapshot();
        assert(raw_snapshot.accepted == 1);
        assert(raw_snapshot.dropped == 0);
        assert(raw_snapshot.evidence_valid == 1);
    }
    std::filesystem::remove(deribit_burst_raw_path);

    // Bounded segments have independent headers, global sequence continuity,
    // and no .bin publication until the writer has closed the whole segment.
    const auto segmented = std::filesystem::temp_directory_path() /
        ("pm_v7_segments_" + std::to_string(::getpid()));
    std::filesystem::remove_all(segmented);
    std::filesystem::create_directories(segmented);
    {
        ExternalTapeRecorder recorder(segmented/"normalized.bin",sha,"run-seg","session-seg","binance",1'000'010,
            TapeSegmentOptions{sizeof(TapeSessionHeader)+2*sizeof(TapeRecord),0});
        for(std::uint64_t sequence=1;sequence<=7;++sequence)
            assert(recorder.try_record(make_tape_record(TapeRecordKind::OracleEvent,sequence,100+sequence,1,oracle)));
    }
    std::vector<std::filesystem::path> parts;
    for(const auto& entry:std::filesystem::directory_iterator(segmented)) {
        assert(entry.path().extension()!=".open");
        if(entry.path().extension()==".bin")parts.push_back(entry.path());
    }
    std::sort(parts.begin(),parts.end());assert(parts.size()==4);
    std::uint64_t sequence=0;
    for(const auto& part:parts) {
        assert(std::filesystem::file_size(part)<=sizeof(TapeSessionHeader)+2*sizeof(TapeRecord));
        std::ifstream stream(part,std::ios::binary);TapeSessionHeader h{};
        stream.read(reinterpret_cast<char*>(&h),sizeof(h));assert(stream.good());
        assert(std::string(h.code_sha.data())==sha);assert(h.record_bytes==sizeof(TapeRecord));
        TapeRecord r{};
        while(stream.read(reinterpret_cast<char*>(&r),sizeof(r)))assert(r.tape_sequence==++sequence);
        assert(stream.eof() && stream.gcount()==0);
    }
    assert(sequence==7);
    const auto raw_segments=segmented/"raw";std::filesystem::create_directory(raw_segments);
    {
        ExternalRawTapeRecorder recorder(raw_segments/"mixed.bin",sha,"run-seg","session-raw-seg","binance",1'000'011,
            TapeSegmentOptions{1024,0});
        for(int i=0;i<6;++i) {
            const std::string payload(i%2==0?20:kExternalRawTapePayloadBytes+10,char('a'+i));
            assert(recorder.try_record_raw(VenueId::BinanceSpot,1,100+i,1000+i,payload));
        }
    }
    parts.clear();for(const auto& entry:std::filesystem::directory_iterator(raw_segments)) {
        assert(entry.path().extension()==".bin");parts.push_back(entry.path());
    }
    std::sort(parts.begin(),parts.end());sequence=0;
    for(const auto& part:parts) {
        std::ifstream stream(part,std::ios::binary);TapeSessionHeader h{};
        stream.read(reinterpret_cast<char*>(&h),sizeof(h));assert(stream.good());
        assert(std::string(h.magic.data(),8)=="PMV7RAW!");
        RawTapeDiskRecordHeader r{};
        while(stream.read(reinterpret_cast<char*>(&r),sizeof(r))) {
            assert(r.tape_sequence==++sequence);
            std::string payload(r.payload_size,'\0');stream.read(payload.data(),payload.size());assert(stream.good());
            assert(payload==std::string(sequence%2==1?20:kExternalRawTapePayloadBytes+10,char('a'+sequence-1)));
        }
        assert(stream.eof() && stream.gcount()==0);
    }
    assert(sequence==6);
    const auto timed=segmented/"timed";std::filesystem::create_directory(timed);
    {
        ExternalTapeRecorder recorder(timed/"clock.bin",sha,"run-age","session-age","binance",1'000'012,
            TapeSegmentOptions{0,1});
        assert(recorder.try_record(make_tape_record(TapeRecordKind::OracleEvent,1,100,1,oracle)));
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
        // Closed publication precedes directory synchronization and opening
        // the next segment. Wait for both observable states, not their atomicity.
        while((!std::filesystem::exists(timed/"clock.segment-000000.bin")
               || !std::filesystem::exists(timed/"clock.segment-000001.bin.open"))
              && std::chrono::steady_clock::now()<deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        assert(std::filesystem::exists(timed/"clock.segment-000000.bin"));
        assert(std::filesystem::exists(timed/"clock.segment-000001.bin.open"));
        assert(recorder.snapshot().writer_healthy==1);
    }
    const auto crashed=segmented/"crashed";std::filesystem::create_directory(crashed);
    const auto child=::fork();assert(child>=0);
    if(child==0) {
        ExternalTapeRecorder recorder(crashed/"crash.bin",sha,"run-crash","session-crash","binance",1'000'013,
            TapeSegmentOptions{4096,300});
        assert(recorder.try_record(make_tape_record(TapeRecordKind::OracleEvent,1,100,1,oracle)));
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        ::_exit(0); // Deliberately no C++ destructors: must not publish .bin.
    }
    int child_status=0;const auto waited=::waitpid(child,&child_status,0);
    assert(waited==child);assert(WIFEXITED(child_status));
    assert(std::filesystem::exists(crashed/"crash.segment-000000.bin.open"));
    assert(!std::filesystem::exists(crashed/"crash.segment-000000.bin"));
    std::filesystem::remove_all(segmented);

    return 0;
}
