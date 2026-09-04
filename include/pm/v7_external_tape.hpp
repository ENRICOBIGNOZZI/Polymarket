#pragma once

#include "pm/v7_spsc.hpp"
#include "pm/v7_external_fair.hpp"

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

// V2 extends ExternalVenueEvent with derivative-context fields; V3 extends
// ExternalAssetSnapshot with the Binance USD-M context slot. Replayers retain
// explicit V1/V2 compatibility; writers never reuse an old payload ABI.
inline constexpr std::uint32_t kExternalTapeSchemaVersion = 3;
inline constexpr std::uint32_t kExternalTapeOldestReplaySchemaVersion = 1;
inline constexpr std::size_t kExternalTapePayloadBytes = 512;
inline constexpr std::size_t kExternalTapeQueueCapacity = 4096;
inline constexpr std::size_t kExternalRawTapePayloadBytes = 32 * 1024;
inline constexpr std::size_t kExternalRawTapeQueueCapacity = 256;
// Deribit's catch-up batches can emit a transiently larger public frame. It
// uses this bounded source-specific FIFO so those frames are captured without
// making every venue pay the memory cost.
// Complete L2 recovery/public batches can exceed the ordinary source limit.
// Every live raw source gets this separate bounded queue, while ordinary
// frames retain the compact high-depth path above.
inline constexpr std::size_t kExternalLargeRawTapePayloadBytes = 2 * 1024 * 1024;
inline constexpr std::size_t kExternalLargeRawTapeQueueCapacity = 8;
inline constexpr std::uint64_t kExternalTapeDefaultSegmentMaxBytes =
    256ULL * 1024ULL * 1024ULL;
inline constexpr std::int64_t kExternalTapeDefaultSegmentMaxAgeNs =
    15LL * 60LL * 1'000'000'000LL;

struct TapeSegmentationPolicy {
    std::uint64_t maximum_segment_bytes = kExternalTapeDefaultSegmentMaxBytes;
    std::int64_t maximum_segment_age_ns = kExternalTapeDefaultSegmentMaxAgeNs;
};

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
    std::uint64_t dropped_payload_too_large = 0;
    std::uint64_t dropped_queue_full = 0;
    std::size_t queued = 0;
    std::uint64_t closed_segments = 0;
    std::uint64_t active_segment_bytes = 0;
    std::uint8_t evidence_valid = 1;
    std::uint8_t writer_healthy = 1;
    std::array<std::uint8_t, 6> reserved{};
};

// Raw frames are persisted per connection before parsing. The bounded maximum
// makes a payload that cannot be durably captured explicit evidence loss, not
// a silently truncated record. One recorder is owned by one WebSocket IO
// thread, preserving a lock-free SPSC handoff to its file writer.
struct RawTapeRecord {
    std::uint64_t tape_sequence = 0;
    std::uint64_t connection_epoch = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int64_t receive_wall_ns = 0;
    VenueId venue = VenueId::Unknown;
    std::uint8_t reserved[3]{};
    std::uint32_t payload_size = 0;
    std::array<std::byte, kExternalRawTapePayloadBytes> payload{};
};

struct LargeRawTapeRecord {
    std::uint64_t tape_sequence = 0;
    std::uint64_t connection_epoch = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int64_t receive_wall_ns = 0;
    VenueId venue = VenueId::Unknown;
    std::uint8_t reserved[3]{};
    std::uint32_t payload_size = 0;
    std::array<std::byte, kExternalLargeRawTapePayloadBytes> payload{};
};

// The in-memory queue uses RawTapeRecord's fixed capacity; disk records carry
// only their actual payload so ordinary market-data traffic does not expand to
// the queue's maximum size on durable storage.
struct RawTapeDiskRecordHeader {
    std::uint64_t tape_sequence = 0;
    std::uint64_t connection_epoch = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int64_t receive_wall_ns = 0;
    VenueId venue = VenueId::Unknown;
    std::uint8_t reserved[3]{};
    std::uint32_t payload_size = 0;
};

class ExternalRawFrameSink {
public:
    virtual ~ExternalRawFrameSink() = default;
    [[nodiscard]] virtual bool try_record_raw(VenueId venue,
                                               std::uint64_t connection_epoch,
                                               std::int64_t receive_monotonic_ns,
                                               std::int64_t receive_wall_ns,
                                               std::string_view payload) noexcept = 0;
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
                         std::int64_t creation_wall_ns,
                         TapeSegmentationPolicy segmentation = {});
    ~ExternalTapeRecorder();

    ExternalTapeRecorder(const ExternalTapeRecorder&) = delete;
    ExternalTapeRecorder& operator=(const ExternalTapeRecorder&) = delete;

    // Hot-path API: bounded, lock-free SPSC enqueue only. Overflow marks the
    // evidence session invalid; it never performs synchronous persistence.
    [[nodiscard]] bool try_record(const TapeRecord& record) noexcept;
    // One recorder is assigned to one ingress producer. This helper assigns a
    // local monotone tape sequence to each accepted normalized venue event.
    [[nodiscard]] bool try_record_external_venue_event(const ExternalVenueEvent& event) noexcept;
    [[nodiscard]] TapeRecorderSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

class ExternalRawTapeRecorder final : public ExternalRawFrameSink {
public:
    ExternalRawTapeRecorder(std::filesystem::path path,
                            std::string code_sha,
                            std::string run_id,
                            std::string session_id,
                            std::string source,
                            std::int64_t creation_wall_ns,
                            TapeSegmentationPolicy segmentation = {});
    ~ExternalRawTapeRecorder();

    ExternalRawTapeRecorder(const ExternalRawTapeRecorder&) = delete;
    ExternalRawTapeRecorder& operator=(const ExternalRawTapeRecorder&) = delete;

    [[nodiscard]] bool try_record_raw(VenueId venue,
                                       std::uint64_t connection_epoch,
                                       std::int64_t receive_monotonic_ns,
                                       std::int64_t receive_wall_ns,
                                       std::string_view payload) noexcept override;
    [[nodiscard]] TapeRecorderSnapshot snapshot() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

static_assert(std::is_trivially_copyable_v<TapeSegmentationPolicy>);
static_assert(std::is_trivially_copyable_v<TapeSessionHeader>);
static_assert(sizeof(TapeSessionHeader) == 232, "binary retention parses this ABI");
static_assert(std::is_trivially_copyable_v<TapeRecord>);
static_assert(sizeof(TapeRecord) == 544, "normalized tape record ABI drift");
static_assert(std::is_trivially_copyable_v<TapeRecorderSnapshot>);
static_assert(std::is_trivially_copyable_v<RawTapeRecord>);
static_assert(std::is_trivially_copyable_v<LargeRawTapeRecord>);
static_assert(std::is_trivially_copyable_v<RawTapeDiskRecordHeader>);
static_assert(sizeof(RawTapeDiskRecordHeader) == 40, "raw tape disk ABI drift");

} // namespace pm::v7::external_fair
