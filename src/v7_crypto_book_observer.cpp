#include "pm/v7_crypto_book_observer.hpp"

#include "pm/api.hpp"
#include "pm/config.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_external_tape.hpp"
#include "pm/v7_market_ws.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>
#include <unistd.h>

namespace pm::v7::research {
namespace {
namespace fs = std::filesystem;
namespace json = boost::json;
using pm::v7::MarketWsEvent;
using pm::v7::MarketWsEventKind;
using pm::v7::external_fair::ExternalTapeRecorder;
using pm::v7::external_fair::TapeRecordKind;
using pm::v7::external_fair::TapeSegmentOptions;

constexpr std::size_t kOutputCapacity = 512;
constexpr std::uint64_t kSegmentBytes = 64ULL * 1024ULL * 1024ULL;
constexpr std::uint64_t kSegmentSeconds = 300;

[[nodiscard]] std::int64_t wall_ms() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
[[nodiscard]] std::int64_t wall_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
[[nodiscard]] std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    return std::all_of(value.begin(), value.end(), [](char ch) {
        return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
    });
}
[[nodiscard]] std::string read_file(const fs::path& path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error("cannot open " + path.string());
    std::ostringstream out; out << in.rdbuf(); return out.str();
}
[[nodiscard]] json::value read_json(const fs::path& path) {
    boost::system::error_code error;
    auto value = json::parse(read_file(path), error);
    if (error) throw std::runtime_error("invalid json " + path.string());
    return value;
}
[[nodiscard]] const json::value* find(const json::object& object, std::string_view key) noexcept {
    const auto it = object.find(key); return it == object.end() ? nullptr : &it->value();
}
[[nodiscard]] std::string text(const json::value* value) {
    if (value == nullptr) return {};
    if (value->is_string()) return std::string(value->as_string());
    if (value->is_int64()) return std::to_string(value->as_int64());
    if (value->is_uint64()) return std::to_string(value->as_uint64());
    return {};
}
[[nodiscard]] bool flag(const json::value* value, bool fallback) noexcept {
    return value != nullptr && value->is_bool() ? value->as_bool() : fallback;
}
void atomic_write(const fs::path& path, std::string_view content) {
    fs::create_directories(path.parent_path());
    const auto temporary = fs::path(path.string() + ".tmp." + std::to_string(::getpid()));
    { std::ofstream out(temporary, std::ios::trunc); if (!out) throw std::runtime_error("cannot write status");
      out.write(content.data(), static_cast<std::streamsize>(content.size())); out.flush();
      if (!out) throw std::runtime_error("cannot flush status"); }
    std::error_code error; fs::rename(temporary, path, error);
    if (error) { fs::remove(path, error); error.clear(); fs::rename(temporary, path, error); }
    if (error) throw std::runtime_error("cannot publish status");
}
[[nodiscard]] std::int32_t tick_e4(double tick) noexcept {
    if (!std::isfinite(tick) || tick <= 0.0 || tick >= 1.0) return 0;
    const auto value = static_cast<std::int32_t>(std::llround(tick * 10'000.0));
    return value > 0 && 10'000 % value == 0 ? value : 0;
}

struct BookTapePayload {
    std::uint64_t connection_epoch = 0;
    std::int64_t receive_wall_ms = 0;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint8_t event_kind = 0;
    std::uint8_t outcome = 0; // 1 YES, 2 NO
    std::array<std::uint8_t, 6> reserved{};
    BookHotSnapshot book{};
};
static_assert(std::is_trivially_copyable_v<BookTapePayload>);
static_assert(sizeof(BookTapePayload) <= pm::v7::external_fair::kExternalTapePayloadBytes);

struct ActiveMarket {
    std::string market_id, event_id, yes_token, no_token;
};

[[nodiscard]] ActiveMarket active_market(const fs::path& status_path, const std::string& sha) {
    const auto root = read_json(status_path);
    if (!root.is_object()) throw std::runtime_error("external fair status not object");
    const auto& object = root.as_object();
    if (text(find(object, "code_sha")) != sha || !flag(find(object, "paper_only"), false)
        || flag(find(object, "authenticated_execution"), true)
        || flag(find(object, "real_order_submission"), true)) {
        throw std::runtime_error("external fair status authority mismatch");
    }
    const auto* market_value = find(object, "market");
    if (market_value == nullptr || !market_value->is_object()) return {};
    const auto& market = market_value->as_object();
    ActiveMarket result{text(find(market, "market_id")), text(find(market, "event_id")),
                        text(find(market, "yes_token")), text(find(market, "no_token"))};
    if (result.market_id.empty() || result.yes_token.empty() || result.no_token.empty()
        || result.yes_token == result.no_token) return {};
    return result;
}
} // namespace

struct CryptoBookObserver::Impl {
    fs::path run_root, config_path, status_path, evidence_dir, observer_status;
    std::string model_sha, ws_url, market_id, event_id, yes_token, no_token, last_error;
    std::vector<std::string> ids;
    std::unique_ptr<pm::v7::MarketWsShard> decoder;
    std::unique_ptr<pm::fast::MarketWebSocketFeed> feed;
    std::unique_ptr<ExternalTapeRecorder> recorder;
    std::atomic<std::uint64_t> sequence{0}, connection_epoch{1}, dropped{0};
    std::atomic<std::uint64_t> decoder_failures{0}, reconnects{0};
    std::atomic<std::int64_t> last_exchange_ns{0}, last_receive_wall_ms{0};
    std::int64_t last_poll_ms = 0;
    std::string state = "waiting_for_market";

    Impl(fs::path root, fs::path config, std::string sha, std::string url)
        : run_root(std::move(root)), config_path(std::move(config)),
          status_path(run_root / "external_fair/status.json"),
          evidence_dir(run_root / "external_fair/clob_books"),
          observer_status(run_root / "external_fair/clob_book_tape_status.json"),
          model_sha(std::move(sha)), ws_url(std::move(url)) {
        if (!exact_sha(model_sha)) throw std::invalid_argument("crypto book observer exact SHA required");
        fs::create_directories(evidence_dir);
    }

    void record_gap() noexcept {
        if (!recorder) return;
        for (std::uint64_t handle = 1; handle <= 2; ++handle) {
            BookTapePayload payload; payload.connection_epoch = connection_epoch.load();
            payload.receive_wall_ms = wall_ms(); payload.market_handle = 1; payload.event_handle = 1;
            payload.event_kind = static_cast<std::uint8_t>(MarketWsEventKind::LineageInvalidated);
            payload.outcome = static_cast<std::uint8_t>(handle);
            const auto seq = sequence.fetch_add(1, std::memory_order_relaxed) + 1;
            auto record = pm::v7::external_fair::make_tape_record(
                TapeRecordKind::PmState, seq, monotonic_ns(), handle, payload);
            if (!recorder->try_record(record)) dropped.fetch_add(1, std::memory_order_relaxed);
        }
    }

    void stop_session() noexcept {
        if (feed) { feed->stop(); feed.reset(); }
        decoder.reset(); recorder.reset(); ids.clear();
        market_id.clear(); event_id.clear(); yes_token.clear(); no_token.clear();
    }

    void start_session(const ActiveMarket& market) {
        stop_session();
        const pm::Config config = pm::load_config(config_path.string());
        pm::PolymarketApi api(config);
        const std::vector<std::string> requested{market.yes_token, market.no_token};
        const auto books = api.fetch_books(requested);
        const auto yes = books.find(market.yes_token), no = books.find(market.no_token);
        if (yes == books.end() || no == books.end()) throw std::runtime_error("BTC M5 cold-start books unavailable");
        const auto yes_tick = tick_e4(yes->second.tick_size), no_tick = tick_e4(no->second.tick_size);
        if (yes_tick <= 0 || no_tick <= 0) throw std::runtime_error("BTC M5 tick size invalid");

        market_id = market.market_id; event_id = market.event_id;
        yes_token = market.yes_token; no_token = market.no_token; ids = requested;
        sequence.store(0); connection_epoch.store(1); dropped.store(0); decoder_failures.store(0);
        reconnects.store(0); last_exchange_ns.store(0); last_receive_wall_ms.store(0);
        std::vector<TokenBinding> bindings{{yes_token, 1, 1, 1, yes_tick}, {no_token, 1, 1, 2, no_tick}};
        decoder = std::make_unique<MarketWsShard>(std::move(bindings));

        const auto base = evidence_dir / ("btc-m5." + market_id + "." + std::to_string(::getpid()) + ".bin");
        const std::string run_id = model_sha.substr(0, 12) + "-" + std::to_string(::getpid());
        const std::string session_id = market_id + "-" + std::to_string(wall_ms());
        recorder = std::make_unique<ExternalTapeRecorder>(
            base, model_sha, run_id, session_id, "btc-m5-clob-book", wall_ns(),
            TapeSegmentOptions{kSegmentBytes, kSegmentSeconds});

        json::object manifest{{"schema", "polymarket_v7_btc_m5_clob_book_session_v1"},
            {"model_sha", model_sha}, {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false}, {"execution_authority", "RESEARCH_EVIDENCE_ONLY"},
            {"market_id", market_id}, {"event_id", event_id}, {"yes_token", yes_token}, {"no_token", no_token},
            {"yes_instrument_handle", 1}, {"no_instrument_handle", 2}, {"yes_tick_e4", yes_tick},
            {"no_tick_e4", no_tick}, {"tape_base", base.filename().string()}, {"started_ms", wall_ms()}};
        atomic_write(evidence_dir / ("btc-m5." + market_id + "." + std::to_string(::getpid()) + ".manifest.json"),
                     json::serialize(manifest) + "\n");

        feed = std::make_unique<pm::fast::MarketWebSocketFeed>(
            ws_url, ids, 1,
            [this](std::string_view frame, const pm::fast::FeedReceiveStamp& receive, std::size_t) {
                std::array<MarketWsEvent, kOutputCapacity> events{};
                const auto result = decoder->process_frame(frame, receive, events);
                if (result.output_overflow || result.arena_exhausted) decoder_failures.fetch_add(1);
                for (std::size_t i = 0; i < result.output_count; ++i) {
                    const auto& event = events[i];
                    if (event.instrument_handle < 1 || event.instrument_handle > 2) continue;
                    if (event.kind != MarketWsEventKind::BookChanged
                        && event.kind != MarketWsEventKind::TickSizeChanged
                        && event.kind != MarketWsEventKind::LineageInvalidated) continue;
                    BookTapePayload payload; payload.connection_epoch = connection_epoch.load();
                    payload.receive_wall_ms = receive.wall_ms; payload.market_handle = event.market_handle;
                    payload.event_handle = event.event_handle; payload.event_kind = static_cast<std::uint8_t>(event.kind);
                    payload.outcome = static_cast<std::uint8_t>(event.instrument_handle); payload.book = event.book;
                    const auto seq = sequence.fetch_add(1, std::memory_order_relaxed) + 1;
                    const auto record = pm::v7::external_fair::make_tape_record(
                        TapeRecordKind::PmState, seq, receive.monotonic_ns, event.instrument_handle, payload);
                    if (!recorder->try_record(record)) dropped.fetch_add(1, std::memory_order_relaxed);
                    last_exchange_ns.store(std::max(last_exchange_ns.load(), event.exchange_event_ns));
                    last_receive_wall_ms.store(std::max(last_receive_wall_ms.load(), receive.wall_ms));
                }
            },
            [this](std::size_t, std::string_view) {
                decoder->invalidate_all_lineage(); reconnects.fetch_add(1);
                connection_epoch.fetch_add(1); record_gap();
            });
        feed->start(); state = "running"; last_error.clear();
    }

    void poll() noexcept {
        const auto now = wall_ms(); if (now - last_poll_ms < 250) return; last_poll_ms = now;
        try {
            const auto market = active_market(status_path, model_sha);
            if (market.market_id.empty()) { state = "waiting_for_market"; return; }
            if (market.market_id != market_id || !feed || !recorder) start_session(market);
        } catch (const std::exception& error) {
            last_error = error.what(); state = "degraded_retrying";
        }
    }

    void write_status(bool stopped) noexcept {
        try {
            const auto tape = recorder ? recorder->snapshot() : pm::v7::external_fair::TapeRecorderSnapshot{};
            const auto network = feed ? feed->snapshot() : pm::fast::FeedSnapshot{};
            json::object value{{"schema", "polymarket_v7_btc_m5_clob_book_tape_status_v1"},
                {"timestamp_ms", wall_ms()}, {"model_sha", model_sha}, {"paper_only", true},
                {"authenticated_execution", false}, {"real_order_submission", false},
                {"execution_authority", "RESEARCH_EVIDENCE_ONLY"}, {"state", stopped ? "stopped" : state},
                {"market_id", market_id}, {"event_id", event_id}, {"yes_token", yes_token}, {"no_token", no_token},
                {"accepted", tape.accepted}, {"written", tape.written}, {"queued", tape.queued},
                {"dropped", tape.dropped + dropped.load()}, {"writer_healthy", tape.writer_healthy != 0},
                {"evidence_valid", tape.evidence_valid != 0 && decoder_failures.load() == 0 && dropped.load() == 0},
                {"decoder_failures", decoder_failures.load()}, {"reconnects", reconnects.load()},
                {"connection_epoch", connection_epoch.load()}, {"last_exchange_event_ns", last_exchange_ns.load()},
                {"last_receive_wall_ms", last_receive_wall_ms.load()}, {"feed_workers", network.workers},
                {"feed_connected_workers", network.connected_workers}, {"feed_messages", network.messages},
                {"feed_errors", network.errors}, {"last_error", last_error}};
            atomic_write(observer_status, json::serialize(value) + "\n");
        } catch (...) {}
    }

    void stop() noexcept { stop_session(); state = "stopped"; write_status(true); }
};

CryptoBookObserver::CryptoBookObserver(fs::path root, fs::path config, std::string sha, std::string url)
    : impl_(std::make_unique<Impl>(std::move(root), std::move(config), std::move(sha), std::move(url))) {}
CryptoBookObserver::~CryptoBookObserver() { if (impl_) impl_->stop(); }
void CryptoBookObserver::poll() noexcept { if (impl_) impl_->poll(); }
void CryptoBookObserver::write_status(bool stopped) noexcept { if (impl_) impl_->write_status(stopped); }
void CryptoBookObserver::stop() noexcept { if (impl_) impl_->stop(); }

} // namespace pm::v7::research
