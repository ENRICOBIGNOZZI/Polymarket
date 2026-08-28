#pragma once

#include "pm/v7_spsc.hpp"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <memory>
#include <string>
#include <type_traits>

namespace pm::v7::external_fair {

inline constexpr std::uint32_t kExternalTapeSchemaVersion = 1;
inline constexpr std::size_t kExternalTapePayloadBytes = 512;
inline constexpr std::size_t kExternalTapeQueueCapacity = 4096;

enum class TapeRecordKind : std::uint16_t {
    OracleEvent = 1,
    ExternalVenueEvent = 2,
    SettlementReference = 3,
    ExternalAssetSnapshot = 4,
    PmState = 5,
    PrivateState = 6,
    InventoryState = 7,
    RiskState = 8,
    FairValueSnapshot = 9,
    CausalCut = 10,
    CandidateAction = 11,
    ExecutionEvent = 12,
    TerminalSettlement = 13,
};

struct TapeSessionHeader {
    std::array<char, 8> magic{'P','M','V','7','T','A','P','E'};
    std::uint32_t schema_version = kExternalTapeSchemaVersion;
    std::uint32_t record_bytes = 0;
    std::int64_t creation_wall_ns = 0;
    std::array<char, 41> code_sha{};
    std::array<char, 65> run_id{};
    std::array<char, 65> session_id{};
    std::array<char, 33> source{};
};

struct TapeRecord {
    std::uint64_t tape_sequence = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::uint64_t source_handle = 0;
    TapeRecordKind kind = TapeRecordKind::ExternalVenueEvent;
    std::uint16_t reserved = 0;
    std::uint32_t payload_size = 0;
    std::array<std::byte, kExternalTapePayloadBytes> payload{};
};

struct TapeRecorderSnapshot {
    std::uint64_t accepted = 0;
    std::uint64_t written = 0;
    std::uint64_t dropped = 0;
    std::size_t queued = 0;
    std::uint8_t evidence_valid = 1;
    std::uint8_t writer_healthy = 1;
    std::array<std::uint8_t, 6> reserved{};
};

template <class T>
[[nodiscard]] TapeRecord make_tape_record(TapeRecordKind kind,
                                          std::uint64_t sequence,
                                          std::int64_t receive_monotonic_ns,
                                          std::uint64_t source_handle,
                                          const T& value) noexcept {
    static_assert(std::is_trivially_copyable_v<T>);
    static_assert(sizeof(T) <= kExternalTapePayloadBytes);
    TapeRecord record;
    record.tape_sequence = sequence;
    record.receive_monotonic_ns = receive_monotonic_ns;
    record.source_handle = source_handle;
    record.kind = kind;
    record.payload_size = static_cast<std::uint32_t>(sizeof(T));
    std::memcpy(record.payload.data(), &value, sizeof(T));
    return record;
}

template <class T>
[[nodiscard]] bool decode_tape_payload(const TapeRecord& record, T& value) noexcept {
    static_assert(std::is_trivially_copyable_v<T>);
    if (record.payload_size != sizeof(T) || sizeof(T) > record.payload.size()) return false;
    std::memcpy(&value, record.payload.data(), sizeof(T));
    return true;
}

class ExternalTapeRecorder final {
public:
    ExternalTapeRecorder(std::filesystem::path path,
                         std::string code_sha,
                         std::string run_id,
                         std::string session_id,
                         std::string source,
                         std::int64_t creation_wall_ns);
    ~ExternalTapeRecorder();

    ExternalTapeRecorder(const ExternalTapeRecorder&) = delete;
    ExternalTapeRecorder& operator=(const ExternalTapeRecorder&) = delete;

    // Hot-path API: bounded, lock-free SPSC enqueue only. Overflow marks the
    // evidence session invalid; it never performs synchronous persistence.
    [[nodiscard]] bool try_record(const TapeRecord& record) noexcept;
    [[nodiscard]] TapeRecorderSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::is_trivially_copyable_v<TapeSessionHeader>);
static_assert(std::is_trivially_copyable_v<TapeRecord>);
static_assert(std::is_trivially_copyable_v<TapeRecorderSnapshot>);

} // namespace pm::v7::external_fair
