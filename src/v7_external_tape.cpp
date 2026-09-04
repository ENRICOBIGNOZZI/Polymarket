#include "pm/v7_external_tape.hpp"

#include <algorithm>
#include <chrono>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <unistd.h>

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

[[nodiscard]] std::int64_t steady_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

[[nodiscard]] bool fsync_file(const std::filesystem::path& path) noexcept {
    const int descriptor = ::open(path.c_str(), O_RDWR);
    if (descriptor < 0) return false;
    const bool valid = ::fsync(descriptor) == 0;
    ::close(descriptor);
    return valid;
}

[[nodiscard]] bool fsync_directory(const std::filesystem::path& path) noexcept {
    int flags = O_RDONLY;
#ifdef O_DIRECTORY
    flags |= O_DIRECTORY;
#endif
    const int descriptor = ::open(path.c_str(), flags);
    if (descriptor < 0) return false;
    const bool valid = ::fsync(descriptor) == 0;
    ::close(descriptor);
    return valid;
}

[[nodiscard]] std::filesystem::path segment_path(
    const std::filesystem::path& requested, std::uint64_t index) {
    std::ostringstream suffix;
    suffix << ".segment-" << std::setfill('0') << std::setw(6) << index;
    return requested.parent_path()
        / (requested.stem().string() + suffix.str() + requested.extension().string());
}

[[nodiscard]] TapeSessionHeader make_header(
    bool raw,
    std::string_view code_sha,
    std::string_view run_id,
    std::string_view session_id,
    std::string_view source,
    std::int64_t creation_wall_ns) {
    if (!exact_sha(code_sha)) {
        throw std::invalid_argument("code_sha must be exact 40-char lowercase hex");
    }
    if (creation_wall_ns <= 0) {
        throw std::invalid_argument("creation_wall_ns must be positive");
    }
    TapeSessionHeader header;
    if (raw) header.magic = {'P','M','V','7','R','A','W','!'};
    header.record_bytes = raw ? 0U : static_cast<std::uint32_t>(sizeof(TapeRecord));
    header.creation_wall_ns = creation_wall_ns;
    copy_fixed(header.code_sha, code_sha, "code_sha");
    copy_fixed(header.run_id, run_id, "run_id");
    copy_fixed(header.session_id, session_id, "session_id");
    copy_fixed(header.source, source, "source");
    return header;
}

class SegmentedTapeFile final {
public:
    SegmentedTapeFile(std::filesystem::path requested_path,
                      TapeSessionHeader header,
                      TapeSegmentationPolicy policy)
        : requested_path_(std::move(requested_path)),
          header_(std::move(header)),
          policy_(policy) {
        if (requested_path_.empty() || requested_path_.extension() != ".bin") {
            throw std::invalid_argument("V7 tape path must end in .bin");
        }
        if (policy_.maximum_segment_bytes <= sizeof(TapeSessionHeader)
            || policy_.maximum_segment_age_ns <= 0) {
            throw std::invalid_argument("V7 tape segmentation policy is invalid");
        }
        if (!requested_path_.parent_path().empty()) {
            std::filesystem::create_directories(requested_path_.parent_path());
        }
        open_segment(header_.creation_wall_ns);
    }

    ~SegmentedTapeFile() { (void)finalize(); }

    SegmentedTapeFile(const SegmentedTapeFile&) = delete;
    SegmentedTapeFile& operator=(const SegmentedTapeFile&) = delete;

    [[nodiscard]] bool write_record(const void* first,
                                    std::size_t first_bytes,
                                    const void* second = nullptr,
                                    std::size_t second_bytes = 0) noexcept {
        if (failed_ || finalized_ || first == nullptr || first_bytes == 0
            || (second_bytes != 0 && second == nullptr)) {
            failed_ = true;
            return false;
        }
        if (first_bytes > static_cast<std::size_t>(-1) - second_bytes) {
            failed_ = true;
            return false;
        }
        const auto record_bytes = first_bytes + second_bytes;
        try {
            const auto age = steady_now_ns() - segment_opened_steady_ns_;
            if (records_in_segment_ != 0
                && (segment_bytes_ + record_bytes > policy_.maximum_segment_bytes
                    || age >= policy_.maximum_segment_age_ns)) {
                if (!close_segment()) return false;
                ++segment_index_;
                open_segment(wall_now_ns());
            }
            output_.write(static_cast<const char*>(first),
                          static_cast<std::streamsize>(first_bytes));
            if (second_bytes != 0) {
                output_.write(static_cast<const char*>(second),
                              static_cast<std::streamsize>(second_bytes));
            }
            if (!output_) {
                failed_ = true;
                return false;
            }
            segment_bytes_ += record_bytes;
            ++records_in_segment_;
            active_segment_bytes_.store(segment_bytes_, std::memory_order_release);
            return true;
        } catch (...) {
            failed_ = true;
            return false;
        }
    }

    [[nodiscard]] bool finalize() noexcept {
        if (finalized_) return !failed_;
        finalized_ = true;
        if (!output_.is_open()) return !failed_;
        if (failed_) {
            output_.close();
            return false;
        }
        if (records_in_segment_ == 0) {
            output_.close();
            std::error_code error;
            std::filesystem::remove(active_path_, error);
            active_segment_bytes_.store(0, std::memory_order_release);
            return !error;
        }
        return close_segment();
    }

    [[nodiscard]] std::uint64_t closed_segments() const noexcept {
        return closed_segments_.load(std::memory_order_acquire);
    }

    [[nodiscard]] std::uint64_t active_segment_bytes() const noexcept {
        return active_segment_bytes_.load(std::memory_order_acquire);
    }

private:
    void open_segment(std::int64_t creation_wall_ns) {
        published_path_ = segment_path(requested_path_, segment_index_);
        active_path_ = published_path_;
        active_path_ += ".open";
        if (std::filesystem::exists(published_path_)
            || std::filesystem::exists(active_path_)) {
            throw std::invalid_argument("V7 tape segment path already exists");
        }
        output_.clear();
        output_.open(active_path_, std::ios::binary | std::ios::trunc);
        if (!output_) throw std::runtime_error("unable to open V7 tape segment");
        header_.creation_wall_ns = creation_wall_ns;
        output_.write(reinterpret_cast<const char*>(&header_), sizeof(header_));
        output_.flush();
        if (!output_) {
            throw std::runtime_error("unable to write V7 tape segment header");
        }
        segment_bytes_ = sizeof(header_);
        records_in_segment_ = 0;
        segment_opened_steady_ns_ = steady_now_ns();
        active_segment_bytes_.store(segment_bytes_, std::memory_order_release);
    }

    [[nodiscard]] bool close_segment() noexcept {
        try {
            output_.flush();
            if (!output_) {
                failed_ = true;
                return false;
            }
            output_.close();
            if (!fsync_file(active_path_)) {
                failed_ = true;
                return false;
            }
            std::filesystem::rename(active_path_, published_path_);
            if (!published_path_.parent_path().empty()
                && !fsync_directory(published_path_.parent_path())) {
                failed_ = true;
                return false;
            }
            closed_segments_.fetch_add(1, std::memory_order_release);
            active_segment_bytes_.store(0, std::memory_order_release);
            segment_bytes_ = 0;
            records_in_segment_ = 0;
            return true;
        } catch (...) {
            failed_ = true;
            return false;
        }
    }

    std::filesystem::path requested_path_;
    TapeSessionHeader header_{};
    TapeSegmentationPolicy policy_{};
    std::ofstream output_;
    std::filesystem::path active_path_;
    std::filesystem::path published_path_;
    std::uint64_t segment_index_ = 0;
    std::uint64_t segment_bytes_ = 0;
    std::uint64_t records_in_segment_ = 0;
    std::int64_t segment_opened_steady_ns_ = 0;
    std::atomic<std::uint64_t> closed_segments_{0};
    std::atomic<std::uint64_t> active_segment_bytes_{0};
    bool failed_ = false;
    bool finalized_ = false;
};

} // namespace

struct ExternalTapeRecorder::Impl {
    SpscRing<TapeRecord, kExternalTapeQueueCapacity> queue{};
    SegmentedTapeFile tape;
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
         std::int64_t creation_wall_ns,
         TapeSegmentationPolicy segmentation)
        : tape(std::move(path),
               make_header(false, code_sha, run_id, session_id, source,
                           creation_wall_ns),
               segmentation) {
        worker = std::thread([this] { writer_loop(); });
    }

    ~Impl() {
        stop_requested.store(true, std::memory_order_release);
        if (worker.joinable()) worker.join();
    }

    void fail_writer() noexcept {
        writer_healthy.store(false, std::memory_order_release);
        evidence_valid.store(false, std::memory_order_release);
    }

    void writer_loop() noexcept {
        TapeRecord record;
        while (!stop_requested.load(std::memory_order_acquire)
               || queue.approximate_size() != 0) {
            bool progressed = false;
            while (queue.try_pop(record)) {
                progressed = true;
                if (!tape.write_record(&record, sizeof(record))) {
                    fail_writer();
                    return;
                }
                written.fetch_add(1, std::memory_order_relaxed);
            }
            if (!progressed) {
                std::this_thread::sleep_for(std::chrono::microseconds(100));
            }
        }
        if (!tape.finalize()) fail_writer();
    }
};

ExternalTapeRecorder::ExternalTapeRecorder(std::filesystem::path path,
                                           std::string code_sha,
                                           std::string run_id,
                                           std::string session_id,
                                           std::string source,
                                           std::int64_t creation_wall_ns,
                                           TapeSegmentationPolicy segmentation)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(code_sha),
                                   std::move(run_id), std::move(session_id),
                                   std::move(source), creation_wall_ns,
                                   segmentation)) {}

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

bool ExternalTapeRecorder::try_record_external_venue_event(
    const ExternalVenueEvent& event) noexcept {
    if (!impl_ || event.asset_handle == 0 || event.connection_epoch == 0
        || event.local_receive_monotonic_ns <= 0
        || event.venue == VenueId::Unknown) {
        if (impl_) impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    const auto sequence =
        impl_->next_event_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
    return try_record(make_tape_record(TapeRecordKind::ExternalVenueEvent,
                                       sequence,
                                       event.local_receive_monotonic_ns,
                                       event.asset_handle,
                                       event));
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
    out.closed_segments = impl_->tape.closed_segments();
    out.active_segment_bytes = impl_->tape.active_segment_bytes();
    out.evidence_valid =
        impl_->evidence_valid.load(std::memory_order_acquire) ? 1 : 0;
    out.writer_healthy =
        impl_->writer_healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

struct ExternalRawTapeRecorder::Impl {
    SpscRing<RawTapeRecord, kExternalRawTapeQueueCapacity> queue{};
    RawTapeRecord producer_record{};
    RawTapeRecord writer_record{};
    using LargeQueue =
        SpscRing<LargeRawTapeRecord, kExternalLargeRawTapeQueueCapacity>;
    std::unique_ptr<LargeQueue> large_queue{};
    std::unique_ptr<LargeRawTapeRecord> large_producer_record{};
    std::unique_ptr<LargeRawTapeRecord> large_writer_record{};
    SegmentedTapeFile tape;
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

    Impl(std::filesystem::path path,
         std::string code_sha,
         std::string run_id,
         std::string session_id,
         std::string source,
         std::int64_t creation_wall_ns,
         TapeSegmentationPolicy segmentation)
        : large_queue(std::make_unique<LargeQueue>()),
          large_producer_record(std::make_unique<LargeRawTapeRecord>()),
          large_writer_record(std::make_unique<LargeRawTapeRecord>()),
          tape(std::move(path),
               make_header(true, code_sha, run_id, session_id, source,
                           creation_wall_ns),
               segmentation) {
        worker = std::thread([this] { writer_loop(); });
    }

    ~Impl() {
        stop_requested.store(true, std::memory_order_release);
        if (worker.joinable()) worker.join();
    }

    [[nodiscard]] std::size_t queued() const noexcept {
        return queue.approximate_size() + large_queue->approximate_size();
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
        if (!tape.write_record(&disk,
                               sizeof(disk),
                               record.payload.data(),
                               record.payload_size)) {
            fail_writer();
            return false;
        }
        written.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    void writer_loop() noexcept {
        while (!stop_requested.load(std::memory_order_acquire) || queued() != 0) {
            bool progressed = false;
            // The ordinary path must stay compact even for a source that
            // occasionally sends a recovery batch. Only the exceptional
            // frame goes through its wider FIFO.
            while (queue.try_pop(writer_record)) {
                progressed = true;
                if (!write_record(writer_record)) return;
            }
            while (large_queue->try_pop(*large_writer_record)) {
                progressed = true;
                if (!write_record(*large_writer_record)) return;
            }
            if (!progressed) {
                std::this_thread::sleep_for(std::chrono::microseconds(100));
            }
        }
        if (!tape.finalize()) fail_writer();
    }
};

ExternalRawTapeRecorder::ExternalRawTapeRecorder(
    std::filesystem::path path,
    std::string code_sha,
    std::string run_id,
    std::string session_id,
    std::string source,
    std::int64_t creation_wall_ns,
    TapeSegmentationPolicy segmentation)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(code_sha),
                                   std::move(run_id), std::move(session_id),
                                   std::move(source), creation_wall_ns,
                                   segmentation)) {}

ExternalRawTapeRecorder::~ExternalRawTapeRecorder() = default;

bool ExternalRawTapeRecorder::try_record_raw(
    VenueId venue,
    std::uint64_t connection_epoch,
    std::int64_t receive_monotonic_ns,
    std::int64_t receive_wall_ns,
    std::string_view payload) noexcept {
    if (!impl_ || venue == VenueId::Unknown || connection_epoch == 0
        || receive_monotonic_ns <= 0 || receive_wall_ns <= 0 || payload.empty()
        || !impl_->writer_healthy.load(std::memory_order_acquire)) {
        if (impl_) impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    const auto sequence =
        impl_->next_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
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
    if (payload.size() > kExternalLargeRawTapePayloadBytes) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->dropped_payload_too_large.fetch_add(
            1, std::memory_order_relaxed);
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    const bool queued = payload.size() <= kExternalRawTapePayloadBytes
        ? enqueue(impl_->producer_record, impl_->queue)
        : enqueue(*impl_->large_producer_record, *impl_->large_queue);
    if (!queued) {
        impl_->dropped.fetch_add(1, std::memory_order_relaxed);
        impl_->dropped_queue_full.fetch_add(1, std::memory_order_relaxed);
        impl_->evidence_valid.store(false, std::memory_order_release);
        return false;
    }
    impl_->accepted.fetch_add(1, std::memory_order_relaxed);
    return true;
}

TapeRecorderSnapshot ExternalRawTapeRecorder::snapshot() const noexcept {
    TapeRecorderSnapshot out;
    if (!impl_) {
        out.evidence_valid = 0;
        out.writer_healthy = 0;
        return out;
    }
    out.accepted = impl_->accepted.load(std::memory_order_acquire);
    out.written = impl_->written.load(std::memory_order_acquire);
    out.dropped = impl_->dropped.load(std::memory_order_acquire);
    out.dropped_payload_too_large =
        impl_->dropped_payload_too_large.load(std::memory_order_acquire);
    out.dropped_queue_full =
        impl_->dropped_queue_full.load(std::memory_order_acquire);
    out.queued = impl_->queued();
    out.closed_segments = impl_->tape.closed_segments();
    out.active_segment_bytes = impl_->tape.active_segment_bytes();
    out.evidence_valid =
        impl_->evidence_valid.load(std::memory_order_acquire) ? 1 : 0;
    out.writer_healthy =
        impl_->writer_healthy.load(std::memory_order_acquire) ? 1 : 0;
    return out;
}

} // namespace pm::v7::external_fair
