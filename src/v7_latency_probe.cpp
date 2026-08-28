#include "pm/http.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {

struct Options {
    std::string endpoint = "https://clob.polymarket.com/time";
    std::size_t samples = 120;
    std::size_t warmup = 3;
    std::int64_t interval_ms = 500;
};

[[nodiscard]] bool approved_endpoint(std::string_view url) noexcept {
    if (url.find_first_of("\"\\\r\n") != std::string_view::npos) return false;
    constexpr std::string_view scheme = "https://";
    if (!url.starts_with(scheme)) return false;
    url.remove_prefix(scheme.size());
    const auto end = url.find_first_of("/:?#");
    const auto host = url.substr(0, end);
    constexpr std::string_view suffix = ".polymarket.com";
    return host == "polymarket.com"
        || (host.size() > suffix.size() && host.ends_with(suffix));
}

[[nodiscard]] std::int64_t integer(const char* raw, const char* name) {
    if (raw == nullptr || *raw == '\0') throw std::runtime_error(std::string("missing ") + name);
    char* end = nullptr;
    const long long value = std::strtoll(raw, &end, 10);
    if (end == raw || *end != '\0' || value < 0) {
        throw std::runtime_error(std::string("invalid ") + name);
    }
    return static_cast<std::int64_t>(value);
}

Options options(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string_view argument = argv[i];
        auto next = [&]() -> const char* {
            if (++i >= argc) throw std::runtime_error("missing option value");
            return argv[i];
        };
        if (argument == "--endpoint") out.endpoint = next();
        else if (argument == "--samples") out.samples = static_cast<std::size_t>(integer(next(), "samples"));
        else if (argument == "--warmup") out.warmup = static_cast<std::size_t>(integer(next(), "warmup"));
        else if (argument == "--interval-ms") out.interval_ms = integer(next(), "interval-ms");
        else throw std::runtime_error("unknown argument: " + std::string(argument));
    }
    if (!approved_endpoint(out.endpoint)) {
        throw std::runtime_error("latency probe endpoint must be HTTPS on polymarket.com");
    }
    if (out.samples == 0 || out.samples > 1'000'000) throw std::runtime_error("samples out of range");
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
        pm::HttpClient client;
        for (std::size_t i = 0; i < cfg.warmup; ++i) {
            const auto response = client.get(cfg.endpoint);
            if (response.status < 200 || response.status >= 400) {
                throw std::runtime_error("warmup returned HTTP " + std::to_string(response.status));
            }
        }

        std::vector<std::int64_t> dns;
        std::vector<std::int64_t> tcp;
        std::vector<std::int64_t> tls;
        std::vector<std::int64_t> first_byte;
        std::vector<std::int64_t> total;
        for (auto* series : {&dns, &tcp, &tls, &first_byte, &total}) series->reserve(cfg.samples);
        std::size_t reused = 0;
        std::size_t new_connections = 0;
        std::string primary_ip;
        for (std::size_t i = 0; i < cfg.samples; ++i) {
            const auto response = client.get(cfg.endpoint);
            if (response.status < 200 || response.status >= 400) {
                throw std::runtime_error("probe returned HTTP " + std::to_string(response.status));
            }
            dns.push_back(response.timings.dns_ns);
            tcp.push_back(response.timings.tcp_connect_ns);
            tls.push_back(response.timings.tls_connect_ns);
            first_byte.push_back(response.timings.first_byte_ns);
            total.push_back(response.timings.total_ns);
            reused += response.timings.connection_reused ? 1U : 0U;
            new_connections += static_cast<std::size_t>(
                std::max<long>(0, response.timings.new_connections));
            if (!response.timings.primary_ip.empty()) primary_ip = response.timings.primary_ip;
            if (cfg.interval_ms > 0 && i + 1 < cfg.samples) {
                std::this_thread::sleep_for(std::chrono::milliseconds(cfg.interval_ms));
            }
        }

        std::cout << "{\"schema\":\"polymarket_v7_regional_latency_probe_v1\""
                  << ",\"endpoint\":\"" << cfg.endpoint << "\""
                  << ",\"samples\":" << cfg.samples
                  << ",\"warmup\":" << cfg.warmup
                  << ",\"primary_ip\":\"" << primary_ip << "\""
                  << ",\"connection_reused_samples\":" << reused
                  << ",\"new_connections\":" << new_connections
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
