#include "pm/api.hpp"
#include "pm/config.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_market_ws.hpp"
#include "pm/v7_spsc.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>
#include <unistd.h>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;
using pm::v7::MarketWsEvent;
using pm::v7::MarketWsEventKind;
using pm::v7::Side;

constexpr std::size_t kWsOutputCapacity = 512;
constexpr std::size_t kEvidenceCapacity = 16384;
constexpr double kPriceScaleE4 = 10'000.0;
constexpr double kMicrounitsPerShare = 1'000'000.0;

std::atomic<bool> g_stop{false};
void signal_handler(int) noexcept { g_stop.store(true, std::memory_order_relaxed); }

[[nodiscard]] std::int64_t wall_ms() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch()).count();
}

[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

[[nodiscard]] std::string read_file(const fs::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open " + path.string());
    std::ostringstream buffer;
    buffer << input.rdbuf();
    return buffer.str();
}

[[nodiscard]] json::value read_json(const fs::path& path) {
    boost::system::error_code error;
    auto value = json::parse(read_file(path), error);
    if (error) throw std::runtime_error("invalid json " + path.string() + ": " + error.message());
    return value;
}

void atomic_write(const fs::path& path, std::string_view content) {
    fs::create_directories(path.parent_path());
    const fs::path temporary = path.string() + ".tmp." + std::to_string(::getpid());
    {
        std::ofstream out(temporary, std::ios::trunc);
        if (!out) throw std::runtime_error("cannot write " + temporary.string());
        out.write(content.data(), static_cast<std::streamsize>(content.size()));
        out.flush();
        if (!out) throw std::runtime_error("failed writing " + temporary.string());
    }
    std::error_code error;
    fs::rename(temporary, path, error);
    if (error) {
        fs::remove(path, error);
        error.clear();
        fs::rename(temporary, path, error);
        if (error) throw std::runtime_error("cannot replace " + path.string());
    }
}

[[nodiscard]] const json::value* find_value(const json::object& object, std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] std::string text(const json::value* value) {
    if (value == nullptr) return {};
    if (value->is_string()) return std::string(value->as_string());
    if (value->is_int64()) return std::to_string(value->as_int64());
    if (value->is_uint64()) return std::to_string(value->as_uint64());
    return {};
}

[[nodiscard]] bool boolean(const json::value* value, bool fallback) noexcept {
    return value != nullptr && value->is_bool() ? value->as_bool() : fallback;
}

[[nodiscard]] std::int32_t price_e4(double price) noexcept {
    if (!std::isfinite(price) || price <= 0.0 || price >= 1.0) return 0;
    return static_cast<std::int32_t>(std::llround(price * kPriceScaleE4));
}

[[nodiscard]] std::int32_t tick_e4(double tick) noexcept {
    const auto value = price_e4(tick);
    if (value <= 0 || 10'000 % value != 0) return 0;
    return value;
}

[[nodiscard]] double e4_price(std::int32_t value) noexcept {
    return static_cast<double>(value) / kPriceScaleE4;
}

[[nodiscard]] double micro_shares(std::int64_t value) noexcept {
    return static_cast<double>(std::max<std::int64_t>(0, value)) / kMicrounitsPerShare;
}

[[nodiscard]] const char* side_name(Side side) noexcept {
    return side == Side::Buy ? "BUY" : side == Side::Sell ? "SELL" : "UNKNOWN";
}

struct Options {
    std::string config = "config/paper_v7.json";
    std::string selection;
    std::string run_root = "runs/paper_v7_live";
    std::string model_sha;
    std::string ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
};

Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (++i >= argc) throw std::runtime_error("missing value after " + arg);
            return argv[i];
        };
        if (arg == "--config") options.config = next();
        else if (arg == "--selection") options.selection = next();
        else if (arg == "--run-root") options.run_root = next();
        else if (arg == "--model-sha") options.model_sha = next();
        else if (arg == "--ws-url") options.ws_url = next();
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (options.selection.empty()) {
        options.selection = options.run_root + "/micro_maker/reward_selection.json";
    }
    if (!exact_sha(options.model_sha)) throw std::runtime_error("--model-sha must be exact 40-hex SHA");
    return options;
}

struct SelectedToken {
    std::string market_id;
    std::string event_id;
    std::string token_id;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::int32_t tick_size_e4 = 0;
};

[[nodiscard]] std::vector<std::pair<std::string, std::pair<std::string, std::string>>>
load_selected_pairs(const fs::path& path) {
    const auto root = read_json(path);
    if (!root.is_object()) throw std::runtime_error("maker selection must be object");
    const auto& object = root.as_object();
    if (const auto* value = find_value(object, "paper_only"); value != nullptr && !boolean(value, false)) {
        throw std::runtime_error("fillability observer selection is not PAPER-only");
    }
    if (const auto* value = find_value(object, "authenticated_execution"); value != nullptr && boolean(value, true)) {
        throw std::runtime_error("fillability observer selection enables authentication");
    }
    const auto* raw = find_value(object, "markets");
    if (raw == nullptr || !raw->is_array()) throw std::runtime_error("maker selection missing markets");
    std::vector<std::pair<std::string, std::pair<std::string, std::string>>> output;
    for (const auto& item : raw->as_array()) {
        if (!item.is_object() || output.size() >= 40) break;
        const auto& row = item.as_object();
        const std::string market = text(find_value(row, "market_id"));
        const std::string event = text(find_value(row, "event_id"));
        const std::string yes = text(find_value(row, "yes_token"));
        const std::string no = text(find_value(row, "no_token"));
        if (market.empty() || yes.empty() || no.empty() || yes == no) continue;
        output.push_back({market + "\n" + event, {yes, no}});
    }
    if (output.empty()) throw std::runtime_error("fillability observer has no selected markets");
    return output;
}

[[nodiscard]] std::vector<SelectedToken> build_tokens(const Options& options, const pm::Config& config) {
    std::vector<std::pair<std::string, std::pair<std::string, std::string>>> pairs;
    while (!g_stop.load(std::memory_order_relaxed)) {
        try {
            if (fs::exists(options.selection) && fs::file_size(options.selection) > 0) {
                pairs = load_selected_pairs(options.selection);
                break;
            }
        } catch (const std::exception& error) {
            std::cerr << "fillability selection not ready: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    if (pairs.empty()) throw std::runtime_error("fillability selection unavailable");

    std::vector<std::string> ids;
    ids.reserve(pairs.size() * 2);
    for (const auto& pair : pairs) {
        ids.push_back(pair.second.first);
        ids.push_back(pair.second.second);
    }

    pm::PolymarketApi api(config);
    while (!g_stop.load(std::memory_order_relaxed)) {
        try {
            const auto books = api.fetch_books(ids);
            std::vector<SelectedToken> output;
            std::uint64_t market_handle = 0;
            std::uint64_t instrument_handle = 0;
            for (const auto& pair : pairs) {
                const auto split = pair.first.find('\n');
                const std::string market_id = pair.first.substr(0, split);
                const std::string event_id = split == std::string::npos ? std::string{} : pair.first.substr(split + 1);
                const auto yes = books.find(pair.second.first);
                const auto no = books.find(pair.second.second);
                if (yes == books.end() || no == books.end()) continue;
                const std::int32_t yes_tick = tick_e4(yes->second.tick_size);
                const std::int32_t no_tick = tick_e4(no->second.tick_size);
                if (yes_tick <= 0 || no_tick <= 0) continue;
                const auto market = ++market_handle;
                output.push_back({market_id, event_id, pair.second.first, market, market,
                                  ++instrument_handle, yes_tick});
                output.push_back({market_id, event_id, pair.second.second, market, market,
                                  ++instrument_handle, no_tick});
            }
            if (!output.empty()) return output;
        } catch (const std::exception& error) {
            std::cerr << "fillability cold-start book fetch failed: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    throw std::runtime_error("fillability observer stopped before cold-start books became available");
}

struct TradeEvidence {
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::uint64_t connection_epoch = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_wall_ms = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int32_t price_e4 = 0;
    std::int64_t quantity_microunits = 0;
    Side aggressor_side = Side::None;
    std::uint8_t lineage_continuous = 0;
    std::array<std::uint8_t, 6> reserved{};
};
static_assert(std::is_trivially_copyable_v<TradeEvidence>);

class ExactWsObserver final {
public:
    ExactWsObserver(std::vector<SelectedToken> tokens, std::string ws_url,
                    fs::path run_root, std::string model_sha)
        : tokens_(std::move(tokens)), ws_url_(std::move(ws_url)),
          run_root_(std::move(run_root)), model_sha_(std::move(model_sha)) {
        std::vector<pm::v7::TokenBinding> bindings;
        std::size_t max_handle = 0;
        for (const auto& token : tokens_) {
            bindings.push_back({token.token_id, token.market_handle, token.event_handle,
                                token.instrument_handle, token.tick_size_e4});
            ids_.push_back(token.token_id);
            max_handle = std::max<std::size_t>(max_handle, token.instrument_handle);
        }
        by_handle_.resize(max_handle + 1, nullptr);
        for (const auto& token : tokens_) by_handle_[token.instrument_handle] = &token;
        decoder_ = std::make_unique<pm::v7::MarketWsShard>(std::move(bindings));
        fs::create_directories(run_root_ / "micro_maker");
        evidence_path_ = run_root_ / "micro_maker" / "fillability_ws.jsonl";
        status_path_ = run_root_ / "micro_maker" / "fillability_ws_status.json";
        output_.open(evidence_path_, std::ios::app);
        if (!output_) throw std::runtime_error("cannot open exact-WS fillability evidence file");
    }

    void start() {
        feed_ = std::make_unique<pm::fast::MarketWebSocketFeed>(
            ws_url_, ids_, std::max<std::size_t>(1, ids_.size()),
            [this](std::string_view payload, const pm::fast::FeedReceiveStamp& receive, std::size_t) {
                std::array<MarketWsEvent, kWsOutputCapacity> events{};
                const auto result = decoder_->process_frame(payload, receive, events);
                raw_last_trade_events_.fetch_add(
                    result.raw_last_trade_events, std::memory_order_relaxed);
                valid_trade_prints_.fetch_add(result.trade_events, std::memory_order_relaxed);
                missing_side_.fetch_add(result.trade_missing_side, std::memory_order_relaxed);
                missing_size_.fetch_add(result.trade_missing_size, std::memory_order_relaxed);
                invalid_quantity_.fetch_add(
                    result.trade_invalid_quantity, std::memory_order_relaxed);
                invalid_price_.fetch_add(result.trade_invalid_price, std::memory_order_relaxed);
                invalid_timestamp_.fetch_add(
                    result.trade_invalid_timestamp, std::memory_order_relaxed);
                unknown_asset_.fetch_add(
                    result.ignored_unknown_assets, std::memory_order_relaxed);
                if (result.output_overflow || result.arena_exhausted) {
                    decoder_failures_.fetch_add(1, std::memory_order_relaxed);
                }
                for (std::size_t i = 0; i < result.output_count; ++i) {
                    const auto& event = events[i];
                    if (event.kind != MarketWsEventKind::Trade || event.instrument_handle == 0
                        || event.price_e4 <= 0 || event.quantity_microunits <= 0
                        || event.exchange_event_ns <= 0 || event.side == Side::None) {
                        continue;
                    }
                    TradeEvidence row;
                    row.instrument_handle = event.instrument_handle;
                    row.state_version = event.state_version;
                    row.connection_epoch = connection_epoch_.load(std::memory_order_relaxed);
                    row.exchange_event_ns = event.exchange_event_ns;
                    row.receive_wall_ms = receive.wall_ms;
                    row.receive_monotonic_ns = receive.monotonic_ns;
                    row.price_e4 = event.price_e4;
                    row.quantity_microunits = event.quantity_microunits;
                    row.aggressor_side = event.side;
                    row.lineage_continuous = event.book.lineage_continuous;
                    if (!queue_.try_push(row)) dropped_.fetch_add(1, std::memory_order_relaxed);
                }
            },
            [this](std::size_t, std::string_view) {
                decoder_->invalidate_all_lineage();
                connection_epoch_.fetch_add(1, std::memory_order_relaxed);
                reconnects_.fetch_add(1, std::memory_order_relaxed);
            });
        feed_->start();
    }

    void stop() {
        if (feed_) feed_->stop();
        drain();
        output_.flush();
        write_status(true);
    }

    void drain() {
        TradeEvidence row;
        bool wrote = false;
        while (queue_.try_pop(row)) {
            write(row);
            wrote = true;
        }
        if (wrote) output_.flush();
    }

    void write_status(bool stopped = false) {
        const auto feed = feed_ ? feed_->snapshot() : pm::fast::FeedSnapshot{};
        json::object root;
        root["schema"] = "polymarket_v7_maker_fillability_ws_status_v1";
        root["timestamp_ms"] = wall_ms();
        root["paper_only"] = true;
        root["authenticated_execution"] = false;
        root["real_order_submission"] = false;
        root["model_sha"] = model_sha_;
        root["state"] = stopped ? "stopped" : "running";
        root["events_written"] = events_written_;
        root["dropped_events"] = dropped_.load(std::memory_order_relaxed);
        root["decoder_failures"] = decoder_failures_.load(std::memory_order_relaxed);
        root["raw_last_trade_events"] = raw_last_trade_events_.load(std::memory_order_relaxed);
        root["valid_trade_prints"] = valid_trade_prints_.load(std::memory_order_relaxed);
        root["missing_side"] = missing_side_.load(std::memory_order_relaxed);
        root["missing_size"] = missing_size_.load(std::memory_order_relaxed);
        root["invalid_quantity"] = invalid_quantity_.load(std::memory_order_relaxed);
        root["invalid_price"] = invalid_price_.load(std::memory_order_relaxed);
        root["invalid_timestamp"] = invalid_timestamp_.load(std::memory_order_relaxed);
        root["unknown_asset"] = unknown_asset_.load(std::memory_order_relaxed);
        root["reconnects"] = reconnects_.load(std::memory_order_relaxed);
        root["connection_epoch"] = connection_epoch_.load(std::memory_order_relaxed);
        root["feed_workers"] = feed.workers;
        root["feed_connected_workers"] = feed.connected_workers;
        root["feed_messages"] = feed.messages;
        root["feed_reconnects"] = feed.reconnects;
        root["feed_errors"] = feed.errors;
        root["last_exchange_event_ns"] = last_exchange_ns_;
        root["last_receive_wall_ms"] = last_receive_wall_ms_;
        root["evidence_complete"] = dropped_.load(std::memory_order_relaxed) == 0
            && decoder_failures_.load(std::memory_order_relaxed) == 0;
        atomic_write(status_path_, json::serialize(root) + "\n");
    }

private:
    void write(const TradeEvidence& row) {
        if (row.instrument_handle >= by_handle_.size()) return;
        const auto* token = by_handle_[row.instrument_handle];
        if (token == nullptr) return;
        json::object event;
        event["schema"] = "polymarket_v7_maker_fillability_ws_trade_v1";
        event["model_sha"] = model_sha_;
        event["paper_only"] = true;
        event["authenticated_execution"] = false;
        event["real_order_submission"] = false;
        event["observer_sequence"] = ++sequence_;
        event["market_id"] = token->market_id;
        if (!token->event_id.empty()) event["event_id"] = token->event_id;
        event["token_id"] = token->token_id;
        event["instrument_handle"] = row.instrument_handle;
        event["state_version"] = row.state_version;
        event["connection_epoch"] = row.connection_epoch;
        event["exchange_event_ns"] = row.exchange_event_ns;
        event["receive_wall_ms"] = row.receive_wall_ms;
        event["receive_monotonic_ns"] = row.receive_monotonic_ns;
        event["aggressor_side"] = side_name(row.aggressor_side);
        event["price"] = e4_price(row.price_e4);
        event["size"] = micro_shares(row.quantity_microunits);
        event["lineage_continuous"] = row.lineage_continuous != 0;
        output_ << json::serialize(event) << '\n';
        ++events_written_;
        last_exchange_ns_ = std::max(last_exchange_ns_, row.exchange_event_ns);
        last_receive_wall_ms_ = std::max(last_receive_wall_ms_, row.receive_wall_ms);
    }

    std::vector<SelectedToken> tokens_;
    std::string ws_url_;
    fs::path run_root_;
    std::string model_sha_;
    std::vector<std::string> ids_;
    std::vector<const SelectedToken*> by_handle_;
    std::unique_ptr<pm::v7::MarketWsShard> decoder_;
    std::unique_ptr<pm::fast::MarketWebSocketFeed> feed_;
    pm::v7::SpscRing<TradeEvidence, kEvidenceCapacity> queue_{};
    std::ofstream output_;
    fs::path evidence_path_;
    fs::path status_path_;
    std::atomic<std::uint64_t> connection_epoch_{1};
    std::atomic<std::uint64_t> dropped_{0};
    std::atomic<std::uint64_t> decoder_failures_{0};
    std::atomic<std::uint64_t> raw_last_trade_events_{0};
    std::atomic<std::uint64_t> valid_trade_prints_{0};
    std::atomic<std::uint64_t> missing_side_{0};
    std::atomic<std::uint64_t> missing_size_{0};
    std::atomic<std::uint64_t> invalid_quantity_{0};
    std::atomic<std::uint64_t> invalid_price_{0};
    std::atomic<std::uint64_t> invalid_timestamp_{0};
    std::atomic<std::uint64_t> unknown_asset_{0};
    std::atomic<std::uint64_t> reconnects_{0};
    std::uint64_t sequence_ = 0;
    std::uint64_t events_written_ = 0;
    std::int64_t last_exchange_ns_ = 0;
    std::int64_t last_receive_wall_ms_ = 0;
};

} // namespace

int main(int argc, char** argv) {
    try {
        std::signal(SIGINT, signal_handler);
        std::signal(SIGTERM, signal_handler);
        const Options options = parse_options(argc, argv);
        const pm::Config config = pm::load_config(options.config);
        auto tokens = build_tokens(options, config);
        ExactWsObserver observer(std::move(tokens), options.ws_url, options.run_root, options.model_sha);
        observer.start();
        std::int64_t last_status_ms = 0;
        while (!g_stop.load(std::memory_order_relaxed)) {
            observer.drain();
            const auto now = wall_ms();
            if (now - last_status_ms >= 1000) {
                observer.write_status();
                last_status_ms = now;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        observer.stop();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_maker_fillability_observer: " << error.what() << '\n';
        return 2;
    }
}
