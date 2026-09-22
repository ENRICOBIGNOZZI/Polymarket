#include "pm/v7_clob_pair_transport.hpp"

#include <algorithm>
#include <chrono>
#include <thread>

namespace pm::v7::clob {
namespace {

[[nodiscard]] std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t skew(std::int64_t a, std::int64_t b) noexcept {
    if (a <= 0 || b <= 0) return 0;
    return a >= b ? a - b : b - a;
}

struct LaneJob {
    const char* data = nullptr;
    std::size_t size = 0;
};

class LaneWorker final {
public:
    LaneWorker(std::string_view host, std::uint16_t port, int timeout_ms,
               int socket_busy_poll_us) noexcept
        : session_(std::make_unique<PersistentTlsSession>(
              host, port, timeout_ms, socket_busy_poll_us)) {}

    ~LaneWorker() { stop(); }

    [[nodiscard]] TlsConnectResult connect(std::string_view ca_file) noexcept {
        return session_->connect(ca_file);
    }

    void start() {
        if (thread_.joinable()) return;
        stopping_.store(false, std::memory_order_release);
        thread_ = std::thread([this] { run(); });
    }

    void stop() noexcept {
        stopping_.store(true, std::memory_order_release);
        if (thread_.joinable()) thread_.join();
        session_->close();
    }

    [[nodiscard]] bool connected() const noexcept {
        return session_ && session_->connected();
    }

    void submit(std::uint64_t epoch, std::span<const char> frame) noexcept {
        job_.data = frame.data();
        job_.size = frame.size();
        epoch_.store(epoch, std::memory_order_release);
    }

    [[nodiscard]] bool wait(std::uint64_t epoch) noexcept {
        while (!stopping_.load(std::memory_order_acquire)) {
            if (done_epoch_.load(std::memory_order_acquire) == epoch) return true;
            std::this_thread::yield();
        }
        return false;
    }

    [[nodiscard]] PairTransportLegResult result() const noexcept {
        return result_;
    }

private:
    void run() noexcept {
        std::uint64_t seen = 0;
        while (!stopping_.load(std::memory_order_acquire)) {
            const auto epoch = epoch_.load(std::memory_order_acquire);
            if (epoch == 0 || epoch == seen) {
                std::this_thread::yield();
                continue;
            }
            seen = epoch;
            PairTransportLegResult out{};
            out.request_bytes = job_.size;
            if (job_.data == nullptr || job_.size == 0 || !session_->connected()) {
                out.transport_error = TlsTransportError::InvalidArgument;
                result_ = out;
                done_epoch_.store(epoch, std::memory_order_release);
                continue;
            }

            out.write_start_monotonic_ns = now_ns();
            const auto write = session_->write_all(
                std::span<const char>(job_.data, job_.size));
            out.transport_error = write.error;
            if (!write.ok) {
                result_ = out;
                done_epoch_.store(epoch, std::memory_order_release);
                continue;
            }
            out.wire_complete_monotonic_ns = write.completed_monotonic_ns;
            out.wire_ok = 1;

            parser_.reset();
            while (!parser_.complete()) {
                auto writable = parser_.writable();
                if (writable.empty()) {
                    out.transport_error = TlsTransportError::ReadFailed;
                    break;
                }
                const auto read = session_->read_some(writable);
                if (!read.ok || read.bytes == 0) {
                    out.transport_error = read.error;
                    break;
                }
                const auto state = parser_.commit(read.bytes);
                if (state == clob_transport::Http1ResponseState::Complete) {
                    out.ack_complete_monotonic_ns = read.completed_monotonic_ns;
                    out.http_status = parser_.status_code();
                    out.incoming_cpu = read.incoming_cpu;
                    out.incoming_napi_id = read.incoming_napi_id;
                    out.response_ok = 1;
                    break;
                }
                if (state != clob_transport::Http1ResponseState::Receiving) {
                    out.transport_error = TlsTransportError::ReadFailed;
                    break;
                }
            }
            result_ = out;
            done_epoch_.store(epoch, std::memory_order_release);
        }
    }

    std::unique_ptr<PersistentTlsSession> session_;
    clob_transport::FixedHttp1Response parser_{};
    LaneJob job_{};
    PairTransportLegResult result_{};
    std::atomic<std::uint64_t> epoch_{0};
    std::atomic<std::uint64_t> done_epoch_{0};
    std::atomic<bool> stopping_{false};
    std::thread thread_;
};

} // namespace

struct PairPersistentTlsTransport::Impl {
    LaneWorker yes;
    LaneWorker no;
    PersistentTlsSession batch;
    clob_transport::FixedHttp1Response batch_parser{};
    std::uint64_t next_epoch = 0;
    bool started = false;

    Impl(std::string_view host, std::uint16_t port, int timeout_ms,
         int socket_busy_poll_us) noexcept
        : yes(host, port, timeout_ms, socket_busy_poll_us),
          no(host, port, timeout_ms, socket_busy_poll_us),
          batch(host, port, timeout_ms, socket_busy_poll_us) {}

    [[nodiscard]] std::uint64_t epoch() noexcept {
        ++next_epoch;
        if (next_epoch == 0) ++next_epoch;
        return next_epoch;
    }

    void stop() noexcept {
        yes.stop();
        no.stop();
        batch.close();
        started = false;
    }
};

PairPersistentTlsTransport::PairPersistentTlsTransport(
    std::string_view host, std::uint16_t port, int timeout_ms,
    int socket_busy_poll_us) noexcept
    : impl_(std::make_unique<Impl>(
          host, port, timeout_ms, socket_busy_poll_us)) {}

PairPersistentTlsTransport::~PairPersistentTlsTransport() {
    close();
}

PairTransportConnectResult PairPersistentTlsTransport::connect(
    std::string_view ca_file) noexcept {
    PairTransportConnectResult out{};
    if (!impl_) return out;
    impl_->stop();
    out.yes = impl_->yes.connect(ca_file);
    if (!out.yes.connected) return out;
    out.no = impl_->no.connect(ca_file);
    if (!out.no.connected) { impl_->stop(); return out; }
    out.batch = impl_->batch.connect(ca_file);
    if (!out.batch.connected) { impl_->stop(); return out; }
    impl_->yes.start();
    impl_->no.start();
    impl_->started = true;
    out.ready = 1;
    return out;
}

void PairPersistentTlsTransport::close() noexcept {
    if (impl_) impl_->stop();
}

bool PairPersistentTlsTransport::ready() const noexcept {
    return impl_ && impl_->started
        && impl_->yes.connected() && impl_->no.connected()
        && impl_->batch.connected();
}

PairTransportResult PairPersistentTlsTransport::submit_parallel(
    std::span<const char> yes_frame,
    std::span<const char> no_frame) noexcept {
    PairTransportResult out{};
    if (!ready() || yes_frame.empty() || no_frame.empty()) return out;
    const auto epoch = impl_->epoch();
    impl_->yes.submit(epoch, yes_frame);
    impl_->no.submit(epoch, no_frame);
    if (!impl_->yes.wait(epoch) || !impl_->no.wait(epoch)) return out;
    out.yes = impl_->yes.result();
    out.no = impl_->no.result();
    out.both_wire_ok = static_cast<std::uint8_t>(
        out.yes.wire_ok != 0 && out.no.wire_ok != 0);
    out.both_response_ok = static_cast<std::uint8_t>(
        out.yes.response_ok != 0 && out.no.response_ok != 0);
    if (out.both_wire_ok) {
        out.first_leg_wire_monotonic_ns = std::min(
            out.yes.wire_complete_monotonic_ns,
            out.no.wire_complete_monotonic_ns);
        out.second_leg_wire_monotonic_ns = std::max(
            out.yes.wire_complete_monotonic_ns,
            out.no.wire_complete_monotonic_ns);
        out.wire_skew_ns = skew(
            out.yes.wire_complete_monotonic_ns,
            out.no.wire_complete_monotonic_ns);
    }
    if (out.both_response_ok) {
        out.first_ack_monotonic_ns = std::min(
            out.yes.ack_complete_monotonic_ns,
            out.no.ack_complete_monotonic_ns);
        out.second_ack_monotonic_ns = std::max(
            out.yes.ack_complete_monotonic_ns,
            out.no.ack_complete_monotonic_ns);
        out.ack_skew_ns = skew(
            out.yes.ack_complete_monotonic_ns,
            out.no.ack_complete_monotonic_ns);
    }
    return out;
}

BatchTransportResult PairPersistentTlsTransport::submit_batch(
    std::span<const char> batch_frame) noexcept {
    BatchTransportResult out{};
    if (!ready() || batch_frame.empty()) return out;
    out.request_bytes = batch_frame.size();
    out.write_start_monotonic_ns = now_ns();
    const auto write = impl_->batch.write_all(batch_frame);
    out.transport_error = write.error;
    if (!write.ok) return out;
    out.wire_complete_monotonic_ns = write.completed_monotonic_ns;
    out.wire_ok = 1;

    impl_->batch_parser.reset();
    while (!impl_->batch_parser.complete()) {
        auto writable = impl_->batch_parser.writable();
        if (writable.empty()) {
            out.transport_error = TlsTransportError::ReadFailed;
            return out;
        }
        const auto read = impl_->batch.read_some(writable);
        if (!read.ok || read.bytes == 0) {
            out.transport_error = read.error;
            return out;
        }
        const auto state = impl_->batch_parser.commit(read.bytes);
        if (state == clob_transport::Http1ResponseState::Complete) {
            out.ack_complete_monotonic_ns = read.completed_monotonic_ns;
            out.http_status = impl_->batch_parser.status_code();
            out.incoming_cpu = read.incoming_cpu;
            out.incoming_napi_id = read.incoming_napi_id;
            out.response_ok = 1;
            return out;
        }
        if (state != clob_transport::Http1ResponseState::Receiving) {
            out.transport_error = TlsTransportError::ReadFailed;
            return out;
        }
    }
    return out;
}

} // namespace pm::v7::clob
