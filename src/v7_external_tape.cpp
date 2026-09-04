#include "pm/v7_external_tape.hpp"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <stdexcept>
#include <string_view>
#include <thread>

namespace pm::v7::external_fair {
namespace {

template <std::size_t N>
void copy_fixed(std::array<char, N>& target,
                std::string_view value,
                std::string_view field) {
    if (value.empty() || value.size() >= N) {
        throw std::invalid_argument(std::string(field) + " invalid for V7 tape header");
    }
    std::copy(value.begin(), value.end(), target.begin());
    target[value.size()] = '\0';
}

[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        const bool digit = ch >= '0' && ch <= '9';
        const bool lower_hex = ch >= 'a' && ch <= 'f';
        if (!digit && !lower_hex) return false;
    }
    return true;
}

} // namespace

struct ExternalTapeRecorder::Impl {
    SpscRing<TapeRecord, kExternalTapeQueueCapacity> queue{};
    std::ofstream output;
    std::thread worker;
    std::atomic<bool> stop_requested{false};
    std::atomic<std::uint64_t> next_event_sequence{0};
    std::atomic<std::uint64_t> accepted{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> dropped{0};
    std::atomic<bool> evidence_valid{true};
    std::atomic<bool> writer_healthy{true};

    Impl(std::filesystem::path path,
         std::string code_sha,
         std::string run_id,
         std::string session_id,
         std::string source,
         std::int64_t creation_wall_ns) {
        if (!exact_sha(code_sha)) {
            throw std::invalid_argument("code_sha must be exact 40-char lowercase hex");
        }
        if (creation_wall_ns <= 0) {
            throw std::invalid_argument("creation_wall_ns must be positive");
        }
        if (std::filesystem::exists(path)) {
            throw std::invalid_argument("V7 external tape path already exists");
        }
        if (!path.parent_path().empty()) {
            std::filesystem::create_directories(path.parent_path());
        }
        output.open(path, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("unable to open V7 external tape");

        TapeSessionHeader header;
        header.record_bytes = static_cast<std::uint32_t>(sizeof(TapeRecord));
        header.creation_wall_ns = creation_wall_ns;
        copy_fixed(header.code_sha, code_sha, "code_sha");
        copy_fixed(header.run_id, run_id, "run_id");
        copy_fixed(header.session_id, session_id, "session_id");
        copy_fixed(header.source, source, "source");
        output.write(reinterpret_cast<const char*>(&header), sizeof(header));
        output.flush();
        if (!output) throw std::runtime_error("unable to write V7 tape header");

        worker = std::thread([this] { writer_loop(); });
    }

    ~Impl() {
        stop_requested.store(true, std::memory_order_release);
        if (worker.joinable()) worker.join();
        output.flush();
    }

    void writer_loop() noexcept {
        TapeRecord record;
        while (!stop_requested.load(std::memory_order_acquire)
               || queue.approximate_size() != 0) {
            bool progressed = false;
            while (queue.try_pop(record)) {
                progressed = true;
                output.write(reinterpret_cast<const char*>(&record), sizeof(record));
                if (!output) {
                    writer_healthy.store(false, std::memory_order_release);
                    evidence_valid.store(false, std::memory_order_release);
                    return;
                }
                written.fetch_add(1, std::memory_order_relaxed);
            }
            if (!progressed) std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
        output.flush();
        if (!output) {
            writer_healthy.store(false, std::memory_order_release);
            evidence_valid.store(false, std::memory_order_release);
        }
    }
};

ExternalTapeRecorder::ExternalTapeRecorder(std::filesystem::path path,
                                           std::string code_sha,
                                           std::string run_id,
                                           std::string session_id,
                                           std::string source,
                                           std::int64_t creation_wall_ns)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(code_sha),
                                   std::move(run_id), std::move(session_id),
                                   std::move(source), creation_wall_ns)) {}

ExternalTapeRecorder::~ExternalTapeRecorder() = default;

bool ExternalTapeRecorder::try_record(const TapeRecord& record) noexcept {
    if (!impl_ || record.tape_sequence == 0 || record.receive_monotonic_ns <= 0
        || record.payload_size == 0 || record.payload_size > record.payload.size()) {
        if (impl_) impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    if (!impl_->writer_healthy.load(std::memory_order_acquire)) {
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    if (!impl_->queue.try_push(record)) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    impl_->accepted.fetch_add(1, std::memory_order_relaxed);
    return true;
}

bool ExternalTapeRecorder::try_record_external_venue_event(const ExternalVenueEvent& event) noexcept {
    if (!impl_ || event.asset_handle == 0 || event.connection_epoch == 0
        || event.local_receive_monotonic_ns <= 0 || event.venue == VenueId::Unknown) {
        if (impl_) impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    const auto sequence = impl_->next_event_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
    return try_record(make_tape_record(TapeRecordKind::ExternalVenueEvent, sequence,
                                       event.local_receive_monotonic_ns,
                                       event.asset_handle, event));
}

TapeRecorderSnapshot ExternalTapeRecorder::snapshot() const noexcept {
    TapeRecorderSnapshot out;
    if (!impl_) {
        out.evidence_valid = 0;
        out.writer_healthy = 0;
        return out;
    }
    out.accepted = impl_->accepted.load(std::memory_order_acquire);
    out.written = impl_->written.load(std::memory_order_acquire);
    out.dropped = impl_->dropped.load(std::memory_order_acquire);
    out.queued = impl_->queue.approximate_size();
    out.evidence_valid = impl_->evidence_valid.load(std::memory_order_acquire) ? 1 : 0;
    out.writer_healthy = impl_->writer_healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

struct ExternalRawTapeRecorder::Impl {
    SpscRing<RawTapeRecord, kExternalRawTapeQueueCapacity> queue{};
    RawTapeRecord producer_record{};
    RawTapeRecord writer_record{};
    using LargeQueue = SpscRing<LargeRawTapeRecord, kExternalLargeRawTapeQueueCapacity>;
    std::unique_ptr<LargeQueue> large_queue{};
    std::unique_ptr<LargeRawTapeRecord> large_producer_record{};
    std::unique_ptr<LargeRawTapeRecord> large_writer_record{};
    // Payload storage remains split by size, but publication has one FIFO.
    // Draining either payload queue first can reorder frames (large #1,
    // small #2). Peeking both heads also races with concurrent publication.
    // A descriptor is published only AFTER its payload; its release/acquire
    // handoff makes the matching payload visible to the sole file writer.
    static constexpr std::size_t kPublicationCapacity =
        std::bit_ceil(kExternalRawTapeQueueCapacity + kExternalLargeRawTapeQueueCapacity);
    SpscRing<std::uint8_t, kPublicationCapacity> publication_queue{};
    std::ofstream output;
    std::thread worker;
    std::atomic<bool> stop_requested{false};
    std::atomic<std::uint64_t> next_sequence{0};
    std::atomic<std::uint64_t> accepted{0};
    std::atomic<std::uint64_t> written{0};
    std::atomic<std::uint64_t> dropped{0};
    std::atomic<std::uint64_t> dropped_payload_too_large{0};
    std::atomic<std::uint64_t> dropped_queue_full{0};
    std::atomic<bool> evidence_valid{true};
    std::atomic<bool> writer_healthy{true};

    Impl(std::filesystem::path path, std::string code_sha, std::string run_id,
         std::string session_id, std::string source, std::int64_t creation_wall_ns) {
        if (!exact_sha(code_sha)) throw std::invalid_argument("code_sha must be exact 40-char lowercase hex");
        if (creation_wall_ns <= 0) throw std::invalid_argument("creation_wall_ns must be positive");
        if (std::filesystem::exists(path)) throw std::invalid_argument("V7 raw tape path already exists");
        if (!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
        output.open(path, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("unable to open V7 raw tape");
        // Keep the high-rate 32 KiB recorder for every ordinary venue.
        // All live WebSocket clients admit recovery-sized frames. Keep their
        // ordinary traffic on the compact FIFO, and reserve this bounded
        // queue for the exceptional frame only.
        large_queue = std::make_unique<LargeQueue>();
        large_producer_record = std::make_unique<LargeRawTapeRecord>();
        large_writer_record = std::make_unique<LargeRawTapeRecord>();

        TapeSessionHeader header;
        header.magic = {'P','M','V','7','R','A','W','!'};
        // Variable-length records: header is followed by payload_size bytes.
        header.record_bytes = 0;
        header.creation_wall_ns = creation_wall_ns;
        copy_fixed(header.code_sha, code_sha, "code_sha");
        copy_fixed(header.run_id, run_id, "run_id");
        copy_fixed(header.session_id, session_id, "session_id");
        copy_fixed(header.source, source, "source");
        output.write(reinterpret_cast<const char*>(&header), sizeof(header));
        output.flush();
        if (!output) throw std::runtime_error("unable to write V7 raw tape header");
        worker = std::thread([this] { writer_loop(); });
    }

    ~Impl() {
        stop_requested.store(true, std::memory_order_release);
        if (worker.joinable()) worker.join();
        output.flush();
    }

    [[nodiscard]] std::size_t queued() const noexcept {
        std::size_t result = queue.approximate_size();
        result += large_queue->approximate_size();
        return result;
    }

    void fail_writer() noexcept {
        writer_healthy.store(false, std::memory_order_release);
        evidence_valid.store(false, std::memory_order_release);
    }

    template <class Record>
    [[nodiscard]] bool write_record(const Record& record) noexcept {
        RawTapeDiskRecordHeader disk;
        disk.tape_sequence = record.tape_sequence;
        disk.connection_epoch = record.connection_epoch;
        disk.receive_monotonic_ns = record.receive_monotonic_ns;
        disk.receive_wall_ns = record.receive_wall_ns;
        disk.venue = record.venue;
        disk.payload_size = record.payload_size;
        output.write(reinterpret_cast<const char*>(&disk), sizeof(disk));
        output.write(reinterpret_cast<const char*>(record.payload.data()),
                     static_cast<std::streamsize>(record.payload_size));
        if (!output) {
            fail_writer();
            return false;
        }
        written.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    void writer_loop() noexcept {
        while ((!stop_requested.load(std::memory_order_acquire) || queued() != 0)
               && writer_healthy.load(std::memory_order_acquire)) {
            bool progressed = false;
            std::uint8_t large = 0;
            while (publication_queue.try_pop(large)) {
                progressed = true;
                if (large != 0) {
                    if (!large_queue->try_pop(*large_writer_record)) {
                        fail_writer();
                        return;
                    }
                    if (!write_record(*large_writer_record)) return;
                } else {
                    if (!queue.try_pop(writer_record)) {
                        fail_writer();
                        return;
                    }
                    if (!write_record(writer_record)) return;
                }
            }
            if (!progressed) std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
        output.flush();
        if (!output) fail_writer();
    }
};

ExternalRawTapeRecorder::ExternalRawTapeRecorder(
    std::filesystem::path path, std::string code_sha, std::string run_id,
    std::string session_id, std::string source, std::int64_t creation_wall_ns)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(code_sha),
                                   std::move(run_id), std::move(session_id),
                                   std::move(source), creation_wall_ns)) {}

ExternalRawTapeRecorder::~ExternalRawTapeRecorder() = default;

bool ExternalRawTapeRecorder::try_record_raw(
    VenueId venue, std::uint64_t connection_epoch, std::int64_t receive_monotonic_ns,
    std::int64_t receive_wall_ns, std::string_view payload) noexcept {
    if (!impl_ || venue == VenueId::Unknown || connection_epoch == 0
        || receive_monotonic_ns <= 0 || receive_wall_ns <= 0 || payload.empty()
        || !impl_->writer_healthy.load(std::memory_order_acquire)) {
        if (impl_) impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    const auto sequence = impl_->next_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
    const auto enqueue = [&](auto& record, auto& queue) noexcept {
        record.tape_sequence = sequence;
        record.connection_epoch = connection_epoch;
        record.receive_monotonic_ns = receive_monotonic_ns;
        record.receive_wall_ns = receive_wall_ns;
        record.venue = venue;
        record.payload_size = static_cast<std::uint32_t>(payload.size());
        std::memcpy(record.payload.data(), payload.data(), payload.size());
        return queue.try_push(record);
    };
    const std::size_t maximum = kExternalLargeRawTapePayloadBytes;
    if (payload.size() > maximum) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->dropped_payload_too_large.fetch_add(1, std::memory_order_relaxed);
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    const bool large = payload.size() > kExternalRawTapePayloadBytes;
    const bool queued = large
        ? enqueue(*impl_->large_producer_record, *impl_->large_queue)
        : enqueue(impl_->producer_record, impl_->queue);
    if (!queued) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->dropped_queue_full.fetch_add(1, std::memory_order_relaxed);
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    // Capacity is at least the sum of the payload queues. Every outstanding
    // descriptor owns a payload slot, so a successful payload enqueue has a
    // descriptor slot. Treat a violated invariant as evidence loss, not as
    // permission to write an out-of-order frame or hang during shutdown.
    if (!impl_->publication_queue.try_push(large ? 1 : 0)) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->dropped_queue_full.fetch_add(1, std::memory_order_relaxed);
        impl_->fail_writer();
        return false;
    }
    impl_->accepted.fetch_add(1, std::memory_order_relaxed);
    return true;
}

TapeRecorderSnapshot ExternalRawTapeRecorder::snapshot() const noexcept {
    TapeRecorderSnapshot out;
    if (!impl_) { out.evidence_valid = 0; out.writer_healthy = 0; return out; }
    out.accepted = impl_->accepted.load(std::memory_order_acquire);
    out.written = impl_->written.load(std::memory_order_acquire);
    out.dropped = impl_->dropped.load(std::memory_order_acquire);
    out.dropped_payload_too_large = impl_->dropped_payload_too_large.load(std::memory_order_acquire);
    out.dropped_queue_full = impl_->dropped_queue_full.load(std::memory_order_acquire);
    out.queued = impl_->queued();
    out.evidence_valid = impl_->evidence_valid.load(std::memory_order_acquire) ? 1 : 0;
    out.writer_healthy = impl_->writer_healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
