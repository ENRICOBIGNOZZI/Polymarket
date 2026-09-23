#include "pm/fast_ws.hpp"
#include "pm/v7_clob_http1_response.hpp"
#include "pm/v7_clob_tls.hpp"
#include "pm/v7_market_ws.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
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

struct Options {
    std::string ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
    std::string clob_host = "clob.polymarket.com";
    std::string yes_token;
    std::string no_token;
    std::string ca_file;
    std::size_t samples = 250;
    int minimum_interval_ms = 100;
    int timeout_seconds = 180;
    int tls_timeout_ms = 2'000;
    std::int32_t tick_size_e4 = 100;
    bool validate_only = false;
};

[[nodiscard]] bool u64(std::string_view text, std::uint64_t& out) noexcept {
    out = 0;
    if (text.empty()) return false;
    const auto parsed = std::from_chars(
        text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{}
        && parsed.ptr == text.data() + text.size();
}

[[nodiscard]] Options parse(int argc, char** argv) {
    Options out{};
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg(argv[i]);
        const auto next = [&]() -> std::string_view {
            if (++i >= argc) throw std::invalid_argument("missing value");
            return argv[i];
        };
        if (arg == "--ws-url") out.ws_url = std::string(next());
        else if (arg == "--clob-host") out.clob_host = std::string(next());
        else if (arg == "--yes-token") out.yes_token = std::string(next());
        else if (arg == "--no-token") out.no_token = std::string(next());
        else if (arg == "--ca-file") out.ca_file = std::string(next());
        else if (arg == "--samples") {
            std::uint64_t value = 0;
            if (!u64(next(), value) || value < 20 || value > 5'000)
                throw std::invalid_argument("invalid samples");
            out.samples = static_cast<std::size_t>(value);
        } else if (arg == "--min-interval-ms") {
            std::uint64_t value = 0;
            if (!u64(next(), value) || value > 5'000)
                throw std::invalid_argument("invalid minimum interval");
            out.minimum_interval_ms = static_cast<int>(value);
        } else if (arg == "--timeout-seconds") {
            std::uint64_t value = 0;
            if (!u64(next(), value) || value < 5 || value > 900)
                throw std::invalid_argument("invalid timeout");
            out.timeout_seconds = static_cast<int>(value);
        } else if (arg == "--tls-timeout-ms") {
            std::uint64_t value = 0;
            if (!u64(next(), value) || value < 50 || value > 60'000)
                throw std::invalid_argument("invalid TLS timeout");
            out.tls_timeout_ms = static_cast<int>(value);
        } else if (arg == "--tick-size-e4") {
            std::uint64_t value = 0;
            if (!u64(next(), value) || value == 0 || value > 10'000)
                throw std::invalid_argument("invalid tick size");
            out.tick_size_e4 = static_cast<std::int32_t>(value);
        } else if (arg == "--validate-only") {
            out.validate_only = true;
        } else {
            throw std::invalid_argument("unknown argument");
        }
    }
    if (!out.validate_only
        && (out.yes_token.empty() || out.no_token.empty()
            || out.yes_token == out.no_token)) {
        throw std::invalid_argument("two distinct token ids required");
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

struct SampleStore {
    std::vector<std::int64_t> receive_to_decode;
    std::vector<std::int64_t> decode_to_write_start;
    std::vector<std::int64_t> receive_to_write_start;
    std::vector<std::int64_t> write_call;
    std::vector<std::int64_t> write_complete_to_ack;
    std::vector<std::int64_t> receive_to_ack;

    void reserve(std::size_t count) {
        for (auto* values : {
                &receive_to_decode, &decode_to_write_start,
                &receive_to_write_start, &write_call,
                &write_complete_to_ack, &receive_to_ack}) {
            values->reserve(count);
        }
    }
};

[[nodiscard]] std::string time_request(std::string_view host) {
    std::string request;
    request.reserve(256);
    request += "GET /time HTTP/1.1\r\nHost: ";
    request += host;
    request += "\r\nUser-Agent: polymarket-v7-event-wire-probe/1.0\r\n";
    request += "Accept: application/json\r\nConnection: keep-alive\r\n\r\n";
    return request;
}

} // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse(argc, argv);
        if (options.validate_only) {
            std::cout << "public event-to-wire probe configuration PASS\n";
            return 0;
        }

        constexpr std::uint64_t kMarket = 1;
        constexpr std::uint64_t kEvent = 1;
        constexpr std::uint64_t kYes = 1;
        constexpr std::uint64_t kNo = 2;
        std::vector<TokenBinding> bindings{
            {options.yes_token, kMarket, kEvent, kYes, options.tick_size_e4},
            {options.no_token, kMarket, kEvent, kNo, options.tick_size_e4},
        };
        MarketWsShard decoder(std::move(bindings));
        auto events = std::make_unique<std::array<MarketWsEvent, 1024>>();

        clob::PersistentTlsSession transport(
            options.clob_host, 443, options.tls_timeout_ms);
        const auto connected = transport.connect(options.ca_file);
        if (!connected.connected) {
            std::cerr << "public transport connect failed\n";
            return 2;
        }
        const auto request = time_request(options.clob_host);

        SampleStore samples;
        samples.reserve(options.samples);
        std::atomic<std::size_t> completed{0};
        std::atomic<bool> done{false};
        std::atomic<std::uint64_t> frames{0};
        std::atomic<std::uint64_t> decoded_events{0};
        std::atomic<std::uint64_t> lineage_faults{0};
        std::atomic<std::uint64_t> invalid_frames{0};
        std::atomic<std::uint64_t> transport_failures{0};
        std::atomic<std::uint64_t> http_failures{0};
        std::atomic<std::uint64_t> transport_reconnects{0};
        std::int64_t previous_probe_ns = 0;

        pm::fast::MarketWebSocketFeed feed(
            options.ws_url,
            {options.yes_token, options.no_token},
            2,
            [&](std::string_view payload,
                const pm::fast::FeedReceiveStamp& receive,
                std::size_t) {
                ++frames;
                auto& scratch = *events;
                const auto decoded =
                    decoder.process_frame(payload, receive, scratch);
                if (decoded.invalid_frame || decoded.output_overflow
                    || decoded.arena_exhausted) {
                    ++invalid_frames;
                    return;
                }
                if (decoded.lineage_invalidated) {
                    ++lineage_faults;
                    return;
                }
                for (std::size_t i = 0; i < decoded.output_count; ++i) {
                    const auto& event = scratch[i];
                    ++decoded_events;
                    if (event.kind != MarketWsEventKind::BookChanged
                        || event.book.valid == 0
                        || event.book.lineage_continuous == 0
                        || event.receive_monotonic_ns <= 0
                        || event.decode_complete_monotonic_ns
                           < event.receive_monotonic_ns) {
                        continue;
                    }
                    if (completed.load(std::memory_order_relaxed)
                        >= options.samples) {
                        done.store(true, std::memory_order_release);
                        return;
                    }
                    const auto minimum_interval_ns =
                        static_cast<std::int64_t>(options.minimum_interval_ms)
                        * 1'000'000LL;
                    if (previous_probe_ns > 0
                        && event.receive_monotonic_ns - previous_probe_ns
                           < minimum_interval_ns) {
                        continue;
                    }
                    previous_probe_ns = event.receive_monotonic_ns;

                    if (!transport.connected()) {
                        const auto reconnected =
                            transport.connect(options.ca_file);
                        ++transport_reconnects;
                        if (!reconnected.connected) {
                            ++transport_failures;
                            continue;
                        }
                    }

                    const auto write_start = now_ns();
                    const auto write = transport.write_all(
                        std::span<const char>(
                            request.data(), request.size()));
                    if (!write.ok) {
                        ++transport_failures;
                        continue;
                    }

                    clob_transport::FixedHttp1Response parser;
                    std::int64_t ack_ns = 0;
                    int status = 0;
                    while (!parser.complete()) {
                        auto writable = parser.writable();
                        if (writable.empty()) break;
                        const auto read = transport.read_some(writable);
                        if (!read.ok || read.bytes == 0) break;
                        const auto state = parser.commit(read.bytes);
                        if (state
                            == clob_transport::Http1ResponseState::Complete) {
                            ack_ns = read.completed_monotonic_ns;
                            status = parser.status_code();
                            break;
                        }
                        if (state
                            != clob_transport::Http1ResponseState::Receiving) {
                            break;
                        }
                    }
                    if (ack_ns <= 0 || status < 200 || status >= 400) {
                        ++http_failures;
                        continue;
                    }

                    samples.receive_to_decode.push_back(
                        event.decode_complete_monotonic_ns
                        - event.receive_monotonic_ns);
                    samples.decode_to_write_start.push_back(
                        write_start - event.decode_complete_monotonic_ns);
                    samples.receive_to_write_start.push_back(
                        write_start - event.receive_monotonic_ns);
                    samples.write_call.push_back(
                        write.completed_monotonic_ns - write_start);
                    samples.write_complete_to_ack.push_back(
                        ack_ns - write.completed_monotonic_ns);
                    samples.receive_to_ack.push_back(
                        ack_ns - event.receive_monotonic_ns);
                    const auto count =
                        completed.fetch_add(1, std::memory_order_release) + 1;
                    if (count >= options.samples) {
                        done.store(true, std::memory_order_release);
                        return;
                    }
                }
            },
            [&](std::size_t, std::string_view) {
                decoder.invalidate_all_lineage();
                ++lineage_faults;
            });

        feed.start();
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(options.timeout_seconds);
        while (!done.load(std::memory_order_acquire)
               && std::chrono::steady_clock::now() < deadline) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        feed.stop();
        transport.close();

        const auto feed_status = feed.snapshot();
        const auto valid = completed.load(std::memory_order_acquire);
        const bool enough = valid >= std::min<std::size_t>(
            options.samples, 20);
        json::object out{
            {"schema", "polymarket_v7_public_event_to_wire_probe_v1"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"authorizes_live_execution", false},
            {"scope",
             "PUBLIC_PM_BOOK_EVENT_TO_PUBLIC_GET_TIME_TRANSPORT_ONLY_NOT_ORDER_LATENCY"},
            {"wire_start_semantics",
             "APPLICATION_SSL_WRITE_CALL_START_NOT_KERNEL_OR_NIC_FIRST_BYTE"},
            {"requested_samples", options.samples},
            {"valid_samples", valid},
            {"frames", frames.load()},
            {"decoded_events", decoded_events.load()},
            {"lineage_faults", lineage_faults.load()},
            {"invalid_frames", invalid_frames.load()},
            {"transport_failures", transport_failures.load()},
            {"http_failures", http_failures.load()},
            {"transport_reconnects", transport_reconnects.load()},
            {"pm_feed_reconnects", feed_status.reconnects},
            {"pm_feed_errors", feed_status.errors},
            {"latency_ns", json::object{
                {"frame_receive_to_decode_done",
                 dist(samples.receive_to_decode)},
                {"decode_done_to_write_start",
                 dist(samples.decode_to_write_start)},
                {"frame_receive_to_write_start",
                 dist(samples.receive_to_write_start)},
                {"write_call",
                 dist(samples.write_call)},
                {"wire_complete_to_http_ack",
                 dist(samples.write_complete_to_ack)},
                {"frame_receive_to_http_ack",
                 dist(samples.receive_to_ack)},
            }},
        };
        std::cout << json::serialize(out) << '\n';
        return enough ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "public_event_to_wire_probe: "
                  << error.what() << '\n';
        return 64;
    }
}
