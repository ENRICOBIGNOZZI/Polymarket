#include "pm/v7_clob_pair_transport.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

namespace json = boost::json;
using namespace pm::v7;

namespace {

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

struct Samples {
    std::vector<std::int64_t> pair_completion_ns;
    std::vector<std::int64_t> wire_skew_ns;
    std::vector<std::int64_t> ack_skew_ns;
    std::uint64_t failures = 0;
};

void reserve(Samples& s, std::size_t n) {
    s.pair_completion_ns.reserve(n);
    s.wire_skew_ns.reserve(n);
    s.ack_skew_ns.reserve(n);
}

void record_parallel(
    Samples& out, const clob::PairTransportResult& pair) {
    if (!pair.both_wire_ok || !pair.both_response_ok
        || pair.yes.http_status < 200 || pair.yes.http_status >= 400
        || pair.no.http_status < 200 || pair.no.http_status >= 400) {
        ++out.failures;
        return;
    }
    const auto start = std::min(
        pair.yes.write_start_monotonic_ns,
        pair.no.write_start_monotonic_ns);
    const auto ack = std::max(
        pair.yes.ack_complete_monotonic_ns,
        pair.no.ack_complete_monotonic_ns);
    if (start <= 0 || ack < start) {
        ++out.failures;
        return;
    }
    out.pair_completion_ns.push_back(ack - start);
    out.wire_skew_ns.push_back(pair.wire_skew_ns);
    out.ack_skew_ns.push_back(pair.ack_skew_ns);
}

void record_serial(
    Samples& out,
    const clob::BatchTransportResult& first,
    const clob::BatchTransportResult& second) {
    if (!first.wire_ok || !first.response_ok
        || !second.wire_ok || !second.response_ok
        || first.http_status < 200 || first.http_status >= 400
        || second.http_status < 200 || second.http_status >= 400
        || first.write_start_monotonic_ns <= 0
        || second.ack_complete_monotonic_ns
           < first.write_start_monotonic_ns) {
        ++out.failures;
        return;
    }
    out.pair_completion_ns.push_back(
        second.ack_complete_monotonic_ns
        - first.write_start_monotonic_ns);
    out.wire_skew_ns.push_back(std::llabs(
        second.wire_complete_monotonic_ns
        - first.wire_complete_monotonic_ns));
    out.ack_skew_ns.push_back(std::llabs(
        second.ack_complete_monotonic_ns
        - first.ack_complete_monotonic_ns));
}

json::object samples_json(const Samples& s) {
    return {
        {"failures", s.failures},
        {"pair_completion_ns", dist(s.pair_completion_ns)},
        {"wire_skew_ns", dist(s.wire_skew_ns)},
        {"ack_skew_ns", dist(s.ack_skew_ns)},
    };
}

} // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse(argc, argv);
        if (options.validate_only) {
            std::cout << "public paired CLOB transport probe configuration PASS\n";
            return 0;
        }

        clob::PairPersistentTlsTransport transport(
            options.host, options.port, options.timeout_ms);
        const auto connected = transport.connect(options.ca_file);
        if (!connected.ready || !transport.ready()) {
            std::cerr << "paired transport connect failed\n";
            return 2;
        }

        const auto request = request_for(options.host, options.target);
        const std::span<const char> frame(request.data(), request.size());

        for (std::size_t i = 0; i < options.warmup; ++i) {
            const auto pair = transport.submit_parallel(frame, frame);
            const auto one = transport.submit_batch(frame);
            const auto two = transport.submit_batch(frame);
            if (!pair.both_response_ok || !one.response_ok || !two.response_ok) {
                std::cerr << "warmup request failed\n";
                return 3;
            }
        }

        Samples serial, parallel;
        reserve(serial, options.samples);
        reserve(parallel, options.samples);
        for (std::size_t i = 0; i < options.samples; ++i) {
            const auto first = transport.submit_batch(frame);
            const auto second = transport.submit_batch(frame);
            record_serial(serial, first, second);
            const auto pair = transport.submit_parallel(frame, frame);
            record_parallel(parallel, pair);
        }
        transport.close();

        const json::object output{
            {"schema", "polymarket_v7_public_paired_clob_transport_probe_v1"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"endpoint", options.host + options.target},
            {"samples", options.samples},
            {"scope", "PUBLIC_GET_TIME_TRANSPORT_ONLY_NOT_MATCHING_ENGINE"},
            {"connect", json::object{
                {"yes_dns_ns", connected.yes.dns_ns},
                {"yes_tcp_ns", connected.yes.tcp_connect_ns},
                {"yes_tls_ns", connected.yes.tls_handshake_ns},
                {"no_dns_ns", connected.no.dns_ns},
                {"no_tcp_ns", connected.no.tcp_connect_ns},
                {"no_tls_ns", connected.no.tls_handshake_ns},
                {"serial_lane_dns_ns", connected.batch.dns_ns},
                {"serial_lane_tcp_ns", connected.batch.tcp_connect_ns},
                {"serial_lane_tls_ns", connected.batch.tls_handshake_ns},
            }},
            {"serial_persistent_lane", samples_json(serial)},
            {"parallel_persistent_legs", samples_json(parallel)},
        };
        std::cout << json::serialize(output) << '\n';
        return parallel.pair_completion_ns.size() * 100
                >= options.samples * 95
            ? 0 : 4;
    } catch (const std::exception& error) {
        std::cerr << "public_paired_clob_transport_probe: "
                  << error.what() << '\n';
        return 64;
    }
}
