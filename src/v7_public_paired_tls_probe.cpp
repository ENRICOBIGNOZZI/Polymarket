#include "pm/v7_clob_pair_transport.hpp"
#include "pm/v7_clob_http1_response.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <limits>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace json = boost::json;
using namespace pm::v7;

namespace {

[[nodiscard]] std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t quantile(
    std::vector<std::int64_t> values, double p) {
    if (values.empty()) return 0;
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(
        std::llround(std::clamp(p, 0.0, 1.0)
                     * static_cast<double>(values.size() - 1)));
    return values[index];
}

[[nodiscard]] json::object dist(
    const std::vector<std::int64_t>& values) {
    return {
        {"count", values.size()},
        {"p50", quantile(values, .50)},
        {"p95", quantile(values, .95)},
        {"p99", quantile(values, .99)},
        {"p999", quantile(values, .999)},
        {"max", quantile(values, 1.0)},
    };
}

struct Options {
    std::string host = "clob.polymarket.com";
    std::string target = "/time";
    std::string ca_file;
    std::uint16_t port = 443;
    int timeout_ms = 2'000;
    std::size_t samples = 200;
    std::size_t warmup = 8;
    bool validate_only = false;
};

[[nodiscard]] bool parse_u64(
    std::string_view text, std::uint64_t& out) noexcept {
    out = 0;
    if (text.empty()) return false;
    const auto result =
        std::from_chars(text.data(), text.data() + text.size(), out);
    return result.ec == std::errc{}
        && result.ptr == text.data() + text.size();
}

[[nodiscard]] Options parse(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg(argv[i]);
        const auto next = [&]() -> std::string_view {
            if (i + 1 >= argc) throw std::invalid_argument("missing value");
            return argv[++i];
        };
        if (arg == "--host") out.host = std::string(next());
        else if (arg == "--target") out.target = std::string(next());
        else if (arg == "--ca-file") out.ca_file = std::string(next());
        else if (arg == "--port") {
            std::uint64_t value = 0;
            if (!parse_u64(next(), value) || value == 0 || value > 65535)
                throw std::invalid_argument("invalid port");
            out.port = static_cast<std::uint16_t>(value);
        } else if (arg == "--timeout-ms") {
            std::uint64_t value = 0;
            if (!parse_u64(next(), value) || value < 50 || value > 60'000)
                throw std::invalid_argument("invalid timeout");
            out.timeout_ms = static_cast<int>(value);
        } else if (arg == "--samples") {
            std::uint64_t value = 0;
            if (!parse_u64(next(), value) || value < 20 || value > 20'000)
                throw std::invalid_argument("invalid samples");
            out.samples = static_cast<std::size_t>(value);
        } else if (arg == "--warmup") {
            std::uint64_t value = 0;
            if (!parse_u64(next(), value) || value > 1'000)
                throw std::invalid_argument("invalid warmup");
            out.warmup = static_cast<std::size_t>(value);
        } else if (arg == "--validate-only") out.validate_only = true;
        else throw std::invalid_argument("unknown argument");
    }
    if (out.host.empty() || out.target.empty()
        || out.target.front() != '/') {
        throw std::invalid_argument("invalid endpoint");
    }
    return out;
}

struct LaneResult {
    std::int64_t wire_start_ns = 0;
    std::int64_t wire_complete_ns = 0;
    std::int64_t ack_ns = 0;
    int http_status = 0;
    std::uint8_t ok = 0;
};

struct LaneControl {
    std::atomic<std::uint64_t> epoch{0};
    std::atomic<std::uint64_t> done_epoch{0};
    LaneResult result{};
};

[[nodiscard]] std::string request_for(
    std::string_view host, std::string_view target) {
    std::string request;
    request.reserve(256);
    request += "GET ";
    request += target;
    request += " HTTP/1.1\r\nHost: ";
    request += host;
    request += "\r\nUser-Agent: polymarket-v7-latency-probe/1.0\r\n";
    request += "Accept: application/json\r\nConnection: keep-alive\r\n\r\n";
    return request;
}

[[nodiscard]] LaneResult one_request(
    clob::PersistentTlsSession& tls,
    std::string_view request) noexcept {
    LaneResult out{};
    clob_transport::FixedHttp1Response parser;
    out.wire_start_ns = now_ns();
    const auto write = tls.write_all(
        {request.data(), request.size()});
    if (!write.ok) return out;
    out.wire_complete_ns = write.completed_monotonic_ns;
    while (!parser.complete()) {
        const auto writable = parser.writable();
        if (writable.empty()) return out;
        const auto read = tls.read_some(writable);
        if (!read.ok || read.bytes == 0) return out;
        const auto state = parser.commit(read.bytes);
        if (state == clob_transport::Http1ResponseState::Complete) {
            out.ack_ns = read.completed_monotonic_ns;
            break;
        }
        if (state != clob_transport::Http1ResponseState::Receiving)
            return out;
    }
    out.http_status = parser.status_code();
    out.ok = static_cast<std::uint8_t>(
        out.ack_ns > 0 && out.http_status >= 200
        && out.http_status < 400);
    return out;
}

struct Samples {
    std::vector<std::int64_t> pair_completion_ns;
    std::vector<std::int64_t> wire_skew_ns;
    std::vector<std::int64_t> wire_complete_skew_ns;
    std::vector<std::int64_t> ack_skew_ns;
    std::uint64_t failures = 0;
};

void record(
    Samples& out, const LaneResult& a, const LaneResult& b) {
    if (!a.ok || !b.ok || a.wire_start_ns <= 0 || b.wire_start_ns <= 0
        || a.ack_ns <= 0 || b.ack_ns <= 0) {
        ++out.failures;
        return;
    }
    const auto begin = std::min(a.wire_start_ns, b.wire_start_ns);
    const auto end = std::max(a.ack_ns, b.ack_ns);
    out.pair_completion_ns.push_back(std::max<std::int64_t>(0, end - begin));
    out.wire_skew_ns.push_back(
        std::llabs(a.wire_start_ns - b.wire_start_ns));
    out.wire_complete_skew_ns.push_back(
        std::llabs(a.wire_complete_ns - b.wire_complete_ns));
    out.ack_skew_ns.push_back(std::llabs(a.ack_ns - b.ack_ns));
}

json::object samples_json(const Samples& s) {
    return {
        {"failures", s.failures},
        {"pair_completion_ns", dist(s.pair_completion_ns)},
        {"wire_start_skew_ns", dist(s.wire_skew_ns)},
        {"wire_complete_skew_ns", dist(s.wire_complete_skew_ns)},
        {"ack_skew_ns", dist(s.ack_skew_ns)},
    };
}

} // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse(argc, argv);
        if (options.validate_only) {
            std::cout
                << "public paired TLS latency probe configuration PASS\n";
            return 0;
        }

        clob::PairedPersistentTlsTransport transport(
            options.host, options.port, options.timeout_ms);
        const auto connected = transport.connect(options.ca_file);
        if (!connected.ready) {
            std::cerr << "paired TLS connect failed\n";
            return 2;
        }
        const auto request = request_for(options.host, options.target);

        LaneControl left, right;
        std::atomic<bool> stop{false};
        auto worker = [&](std::size_t lane_index, LaneControl& control) {
            std::uint64_t seen = 0;
            while (!stop.load(std::memory_order_acquire)) {
                const auto epoch =
                    control.epoch.load(std::memory_order_acquire);
                if (epoch == 0 || epoch == seen) {
                    std::this_thread::yield();
                    continue;
                }
                seen = epoch;
                control.result = one_request(
                    transport.leg(lane_index), request);
                control.done_epoch.store(epoch, std::memory_order_release);
            }
        };
        std::thread t0(worker, 0, std::ref(left));
        std::thread t1(worker, 1, std::ref(right));

        std::uint64_t epoch = 0;
        const auto run_one = [&](LaneControl& control) {
            ++epoch;
            if (epoch == 0) ++epoch;
            control.epoch.store(epoch, std::memory_order_release);
            while (control.done_epoch.load(std::memory_order_acquire)
                   != epoch) {
                std::this_thread::yield();
            }
            return control.result;
        };
        const auto run_parallel = [&]() {
            ++epoch;
            if (epoch == 0) ++epoch;
            const auto e = epoch;
            left.epoch.store(e, std::memory_order_release);
            right.epoch.store(e, std::memory_order_release);
            while (left.done_epoch.load(std::memory_order_acquire) != e
                   || right.done_epoch.load(std::memory_order_acquire) != e) {
                std::this_thread::yield();
            }
            return std::array<LaneResult, 2>{left.result, right.result};
        };

        for (std::size_t i = 0; i < options.warmup; ++i) {
            const auto a = run_one(left);
            const auto b = run_one(right);
            const auto p = run_parallel();
            if (!a.ok || !b.ok || !p[0].ok || !p[1].ok) {
                stop.store(true, std::memory_order_release);
                t0.join();
                t1.join();
                std::cerr << "warmup request failed\n";
                return 3;
            }
        }

        Samples serial, parallel;
        serial.pair_completion_ns.reserve(options.samples);
        serial.wire_skew_ns.reserve(options.samples);
        serial.wire_complete_skew_ns.reserve(options.samples);
        serial.ack_skew_ns.reserve(options.samples);
        parallel.pair_completion_ns.reserve(options.samples);
        parallel.wire_skew_ns.reserve(options.samples);
        parallel.wire_complete_skew_ns.reserve(options.samples);
        parallel.ack_skew_ns.reserve(options.samples);

        for (std::size_t i = 0; i < options.samples; ++i) {
            const auto first = run_one(left);
            const auto second = run_one(right);
            record(serial, first, second);
            const auto p = run_parallel();
            record(parallel, p[0], p[1]);
        }

        stop.store(true, std::memory_order_release);
        t0.join();
        t1.join();
        transport.close();

        const json::object output{
            {"schema", "polymarket_v7_public_paired_tls_probe_v1"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"endpoint", options.host + options.target},
            {"samples", options.samples},
            {"scope",
             "PUBLIC_GET_TIME_TRANSPORT_ONLY_NOT_MATCHING_ENGINE"},
            {"connect", json::object{
                {"leg0_dns_ns", connected.legs[0].dns_ns},
                {"leg0_tcp_ns", connected.legs[0].tcp_connect_ns},
                {"leg0_tls_ns", connected.legs[0].tls_handshake_ns},
                {"leg1_dns_ns", connected.legs[1].dns_ns},
                {"leg1_tcp_ns", connected.legs[1].tcp_connect_ns},
                {"leg1_tls_ns", connected.legs[1].tls_handshake_ns},
            }},
            {"serial_two_persistent_lanes", samples_json(serial)},
            {"parallel_two_persistent_lanes", samples_json(parallel)},
        };
        std::cout << json::serialize(output) << '\n';
        return parallel.pair_completion_ns.size() * 100 >= options.samples * 95 ? 0 : 4;
    } catch (const std::exception& error) {
        std::cerr << "paired_tls_probe: " << error.what() << '\n';
        return 64;
    }
}
