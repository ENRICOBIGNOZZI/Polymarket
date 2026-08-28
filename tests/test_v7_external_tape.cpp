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
    return 0;
}
