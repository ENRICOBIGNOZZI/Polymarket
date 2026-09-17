#include "pm/http.hpp"

#include <algorithm>
#include <chrono>
#include <cerrno>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {

struct Options {
    std::string endpoint = "https://clob.polymarket.com/time";
    std::string region;
    std::string exact_code_sha;
    std::string sample_audit_jsonl;
    std::size_t samples = 120;
    std::size_t warmup = 3;
    std::int64_t interval_ms = 500;
    bool validate_only = false;
};

[[nodiscard]] bool approved_endpoint(std::string_view url) noexcept {
    // A public clock GET is a connectivity probe, never an order-path probe.
    return url == "https://clob.polymarket.com/time";
}

[[nodiscard]] std::int64_t integer(const char* raw, const char* name) {
    if (raw == nullptr || *raw == '\0') throw std::runtime_error(std::string("missing ") + name);
    char* end = nullptr;
    errno = 0;
    const long long value = std::strtoll(raw, &end, 10);
    if (errno == ERANGE || end == raw || *end != '\0' || value < 0) {
        throw std::runtime_error(std::string("invalid ") + name);
    }
    return static_cast<std::int64_t>(value);
}

[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    return value.size() == 40 && std::all_of(value.begin(), value.end(), [](unsigned char character) {
        return std::isdigit(character) != 0 || (character >= 'a' && character <= 'f');
    });
}

[[nodiscard]] std::int64_t wall_millis() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

Options options(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string_view argument = argv[i];
        auto next = [&]() -> const char* {
            if (++i >= argc) throw std::runtime_error("missing option value");
            return argv[i];
        };
        if (argument == "--validate-only") out.validate_only = true;
        else if (argument == "--endpoint") out.endpoint = next();
        else if (argument == "--region") out.region = next();
        else if (argument == "--exact-code-sha") out.exact_code_sha = next();
        else if (argument == "--sample-audit-jsonl") out.sample_audit_jsonl = next();
        else if (argument == "--samples") out.samples = static_cast<std::size_t>(integer(next(), "samples"));
        else if (argument == "--warmup") out.warmup = static_cast<std::size_t>(integer(next(), "warmup"));
        else if (argument == "--interval-ms") out.interval_ms = integer(next(), "interval-ms");
        else throw std::runtime_error("unknown argument: " + std::string(argument));
    }
    if (!approved_endpoint(out.endpoint)) {
        throw std::runtime_error("latency probe permits only the public HTTPS /time endpoint");
    }
    if (out.region.empty() || out.region.find_first_of("\"\\\r\n") != std::string::npos) {
        throw std::runtime_error("region is required");
    }
    if (out.sample_audit_jsonl.find_first_of("\r\n") != std::string::npos) {
        throw std::runtime_error("sample-audit-jsonl contains a line break");
    }
    if (!exact_sha(out.exact_code_sha)) throw std::runtime_error("exact-code-sha must be lowercase SHA-1");
    if (out.samples == 0 || out.samples > 1'000'000) throw std::runtime_error("samples out of range");
    if (out.warmup > 10000 || out.interval_ms > 60000) throw std::runtime_error("probe load out of range");
    return out;
}

std::int64_t quantile(std::vector<std::int64_t> values, double probability) {
    if (values.empty()) return 0;
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(
        probability * static_cast<double>(values.size() - 1));
    return values[index];
}

void emit_distribution(const char* name, const std::vector<std::int64_t>& values) {
    std::cout << "\"" << name << "\":{";
    std::cout << "\"p50\":" << quantile(values, 0.50)
              << ",\"p90\":" << quantile(values, 0.90)
              << ",\"p95\":" << quantile(values, 0.95)
              << ",\"p99\":" << quantile(values, 0.99)
              << ",\"p99_9\":" << quantile(values, 0.999)
              << ",\"max\":" << *std::max_element(values.begin(), values.end()) << '}';
}

} // namespace

int main(int argc, char** argv) {
    try {
        const Options cfg = options(argc, argv);
        if (cfg.validate_only) {
            std::cout << "{\"validated\":true,\"network_calls\":0}\n";
            return 0;
        }
        std::ofstream sample_audit;
        if (!cfg.sample_audit_jsonl.empty()) {
            sample_audit.open(cfg.sample_audit_jsonl, std::ios::out | std::ios::trunc);
            if (!sample_audit) throw std::runtime_error("cannot open sample-audit-jsonl");
        }

        pm::HttpClient client;
        bool connection_seen = false;
        const auto warmup_started = std::chrono::steady_clock::now();
        std::size_t warmup_failed = 0;
        for (std::size_t i = 0; i < cfg.warmup; ++i) {
            try {
                const auto response = client.get(cfg.endpoint);
                connection_seen = connection_seen || response.timings.new_connections > 0;
                if (response.status < 200 || response.status >= 400) ++warmup_failed;
            } catch (const std::exception&) {
                ++warmup_failed;
            }
        }

        const auto started = std::chrono::system_clock::now();
        const auto measured_started = std::chrono::steady_clock::now();
        const auto warmup_elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            measured_started - warmup_started).count();
        std::vector<std::int64_t> dns;
        std::vector<std::int64_t> tcp;
        std::vector<std::int64_t> tls;
        std::vector<std::int64_t> first_byte;
        std::vector<std::int64_t> total;
        for (auto* series : {&dns, &tcp, &tls, &first_byte, &total}) series->reserve(cfg.samples);
        std::size_t reused = 0;
        std::size_t new_connections = 0;
        std::size_t failed = 0;
        std::size_t measured_reconnects = 0;
        std::size_t transport_exceptions = 0;
        std::string primary_ip;
        for (std::size_t i = 0; i < cfg.samples; ++i) {
            const auto sample_started_wall_ms = wall_millis();
            long sample_status = 0;
            std::int64_t sample_dns_ns = 0;
            std::int64_t sample_tcp_ns = 0;
            std::int64_t sample_tls_ns = 0;
            std::int64_t sample_first_byte_ns = 0;
            std::int64_t sample_total_ns = 0;
            long sample_new_connections = 0;
            bool sample_connection_reused = false;
            bool sample_success = false;
            bool sample_transport_exception = false;
            try {
                const auto response = client.get(cfg.endpoint);
                sample_status = response.status;
                sample_dns_ns = response.timings.dns_ns;
                sample_tcp_ns = response.timings.tcp_connect_ns;
                sample_tls_ns = response.timings.tls_connect_ns;
                sample_first_byte_ns = response.timings.first_byte_ns;
                sample_total_ns = response.timings.total_ns;
                sample_new_connections = std::max<long>(0, response.timings.new_connections);
                sample_connection_reused = response.timings.connection_reused;
                sample_success = response.status >= 200 && response.status < 400;
                const auto opened = static_cast<std::size_t>(sample_new_connections);
                if (opened > 0) {
                    measured_reconnects += opened - (connection_seen ? 0U : 1U);
                    connection_seen = true;
                }
                if (!sample_success) {
                    ++failed;
                } else {
                    dns.push_back(sample_dns_ns);
                    tcp.push_back(sample_tcp_ns);
                    tls.push_back(sample_tls_ns);
                    first_byte.push_back(sample_first_byte_ns);
                    total.push_back(sample_total_ns);
                    reused += sample_connection_reused ? 1U : 0U;
                    new_connections += opened;
                    if (!response.timings.primary_ip.empty()) primary_ip = response.timings.primary_ip;
                }
            } catch (const std::exception&) {
                ++failed;
                ++transport_exceptions;
                sample_transport_exception = true;
            }
            const auto sample_finished_wall_ms = wall_millis();
            if (sample_audit) {
                sample_audit << "{\"schema\":\"polymarket_v7_latency_sample_v1\""
                             << ",\"index\":" << i
                             << ",\"started_wall_ms\":" << sample_started_wall_ms
                             << ",\"finished_wall_ms\":" << sample_finished_wall_ms
                             << ",\"success\":" << (sample_success ? "true" : "false")
                             << ",\"http_status\":" << sample_status
                             << ",\"dns_ns\":" << sample_dns_ns
                             << ",\"tcp_connect_ns\":" << sample_tcp_ns
                             << ",\"tls_connect_ns\":" << sample_tls_ns
                             << ",\"first_byte_ns\":" << sample_first_byte_ns
                             << ",\"total_ns\":" << sample_total_ns
                             << ",\"connection_reused\":" << (sample_connection_reused ? "true" : "false")
                             << ",\"new_connections\":" << sample_new_connections
                             << ",\"transport_exception\":" << (sample_transport_exception ? "true" : "false")
                             << "}\n";
                sample_audit.flush();
                if (!sample_audit) throw std::runtime_error("cannot write sample-audit-jsonl");
            }
            if (cfg.interval_ms > 0 && i + 1 < cfg.samples) {
                std::this_thread::sleep_for(std::chrono::milliseconds(cfg.interval_ms));
            }
        }
        if (total.empty()) throw std::runtime_error("probe had no successful samples");
        const auto finished = std::chrono::system_clock::now();
        const auto measured_elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - measured_started).count();
        const auto started_ms = std::chrono::duration_cast<std::chrono::milliseconds>(started.time_since_epoch()).count();
        const auto finished_ms = std::chrono::duration_cast<std::chrono::milliseconds>(finished.time_since_epoch()).count();

        std::cout << "{\"schema\":\"polymarket_v7_regional_latency_probe_v1\""
                  << ",\"endpoint\":\"" << cfg.endpoint << "\""
                  << ",\"region\":\"" << cfg.region << "\""
                  << ",\"exact_code_sha\":\"" << cfg.exact_code_sha << "\""
                  << ",\"started_wall_ms\":" << started_ms
                  << ",\"finished_wall_ms\":" << finished_ms
                  << ",\"samples\":" << cfg.samples
                  << ",\"warmup\":" << cfg.warmup
                  << ",\"successful_samples\":" << total.size()
                  << ",\"failed_samples\":" << failed
                  << ",\"warmup_failed_samples\":" << warmup_failed
                  << ",\"primary_ip\":\"" << primary_ip << "\""
                  << ",\"connection_reused_samples\":" << reused
                  << ",\"new_connections\":" << new_connections
                  << ",\"reconnect_count\":" << measured_reconnects
                  << ",\"transport_exceptions\":" << transport_exceptions
                  << ",\"reconnect_observability\":\"RESPONSES_ONLY_EXCEPTIONS_UNOBSERVABLE\""
                  << ",\"measured_elapsed_monotonic_ns\":" << measured_elapsed_ns
                  << ",\"warmup_elapsed_monotonic_ns\":" << warmup_elapsed_ns
                  << ",\"sampling_mode\":\"CLOSED_LOOP\""
                  << ",\"coordinated_omission_corrected\":false"
                  << ",\"sample_audit_enabled\":" << (cfg.sample_audit_jsonl.empty() ? "false" : "true")
                  << ",\"sample_audit_rows\":" << (cfg.sample_audit_jsonl.empty() ? 0U : cfg.samples)
                  << ",\"timings_ns\":{";
        emit_distribution("dns", dns);
        std::cout << ',';
        emit_distribution("tcp_connect", tcp);
        std::cout << ',';
        emit_distribution("tls_connect", tls);
        std::cout << ',';
        emit_distribution("first_byte", first_byte);
        std::cout << ',';
        emit_distribution("total", total);
        std::cout << "},\"paper_only\":true"
                  << ",\"authenticated_execution\":false"
                  << ",\"real_order_submission\":false"
                  << ",\"measures_order_or_cancel_ack\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_latency_probe: " << error.what() << '\n';
        return 2;
    }
}
