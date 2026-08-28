#include "pm/api.hpp"
#include "pm/config.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_jsonl_tail.hpp"
#include "pm/v7_markout.hpp"
#include "pm/v7_market_ws.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <limits>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
#include <unistd.h>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;
using pm::v7::BookHotSnapshot;
using pm::v7::MarketWsEvent;
using pm::v7::Side;

constexpr std::size_t kWsOutputCapacity = 512;
constexpr double kPriceScaleE4 = 10'000.0;
constexpr double kMicrounitsPerShare = 1'000'000.0;
constexpr std::string_view kStrategy = "MICRO_MAKER_PRO";
constexpr std::array<int, 5> kHorizonSeconds{{1, 10, 45, 60, 300}};
constexpr std::array<std::string_view, 5> kHorizonLabels{{"1s", "10s", "45s", "60s", "300s"}};

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

[[nodiscard]] double number(const json::value* value, double fallback) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_double()) return value->as_double();
    if (value->is_int64()) return static_cast<double>(value->as_int64());
    if (value->is_uint64()) return static_cast<double>(value->as_uint64());
    return fallback;
}

[[nodiscard]] std::int64_t integer(const json::value* value, std::int64_t fallback) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_int64()) return value->as_int64();
    if (value->is_uint64()) return static_cast<std::int64_t>(value->as_uint64());
    if (value->is_double() && std::isfinite(value->as_double())) {
        return static_cast<std::int64_t>(value->as_double());
    }
    return fallback;
}

[[nodiscard]] bool boolean(const json::value* value, bool fallback) noexcept {
    return value != nullptr && value->is_bool() ? value->as_bool() : fallback;
}

[[nodiscard]] std::uint64_t fnv1a(std::string_view value) noexcept {
    std::uint64_t hash = 1469598103934665603ULL;
    for (const unsigned char ch : value) {
        hash ^= static_cast<std::uint64_t>(ch);
        hash *= 1099511628211ULL;
    }
    return hash == 0 ? 1 : hash;
}

[[nodiscard]] std::string hex64(std::uint64_t value) {
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << value;
    return out.str();
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

[[nodiscard]] std::int64_t quantity_microunits(double shares) noexcept {
    if (!std::isfinite(shares) || shares <= 0.0) return 0;
    const long double value = static_cast<long double>(shares) * kMicrounitsPerShare;
    if (value > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) return 0;
    return static_cast<std::int64_t>(std::llround(value));
}

[[nodiscard]] Side parse_side(std::string_view side) noexcept {
    if (side == "BUY") return Side::Buy;
    if (side == "SELL") return Side::Sell;
    return Side::None;
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
        throw std::runtime_error("markout observer selection is not PAPER-only");
    }
    if (const auto* value = find_value(object, "authenticated_execution"); value != nullptr && boolean(value, true)) {
        throw std::runtime_error("markout observer selection enables authentication");
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
    if (output.empty()) throw std::runtime_error("markout observer has no selected markets");
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
            std::cerr << "markout selection not ready: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    if (pairs.empty()) throw std::runtime_error("markout selection unavailable");

    std::vector<std::string> ids;
    ids.reserve(pairs.size() * 2);
    for (const auto& pair : pairs) {
        ids.push_back(pair.second.first);
        ids.push_back(pair.second.second);
    }

    // REST is used only once to discover authoritative venue tick sizes before
    // the public WS evidence observer starts. Transient HTTP/API failures must
    // not kill an otherwise healthy PAPER runtime, so this cold-start step is
    // retried; no REST call is made after the WS observer is live.
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
            std::cerr << "markout cold-start books incomplete; retrying\n";
        } catch (const std::exception& error) {
            std::cerr << "markout cold-start book fetch failed: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    throw std::runtime_error("markout observer stopped before cold-start books became available");
}

struct LatestBook {
    BookHotSnapshot book{};
    std::int64_t wall_receive_ms = 0;
};

class BookObserver final {
public:
    BookObserver(std::vector<SelectedToken> tokens, std::string ws_url)
        : tokens_(std::move(tokens)), ws_url_(std::move(ws_url)) {
        std::vector<pm::v7::TokenBinding> bindings;
        std::size_t max_handle = 0;
        for (const auto& token : tokens_) {
            bindings.push_back({token.token_id, token.market_handle, token.event_handle,
                                token.instrument_handle, token.tick_size_e4});
            ids_.push_back(token.token_id);
            max_handle = std::max<std::size_t>(max_handle, token.instrument_handle);
        }
        latest_.resize(max_handle + 1);
        decoder_ = std::make_unique<pm::v7::MarketWsShard>(std::move(bindings));
    }

    void start() {
        feed_ = std::make_unique<pm::fast::MarketWebSocketFeed>(
            ws_url_, ids_, std::max<std::size_t>(1, ids_.size()),
            [this](std::string_view payload, const pm::fast::FeedReceiveStamp& receive, std::size_t) {
                std::array<MarketWsEvent, kWsOutputCapacity> events{};
                const auto result = decoder_->process_frame(payload, receive, events);
                if (result.output_overflow || result.arena_exhausted) {
                    fatal_.store(true, std::memory_order_release);
                    return;
                }
                std::lock_guard<std::mutex> lock(mutex_);
                for (std::size_t i = 0; i < result.output_count; ++i) {
                    const auto& event = events[i];
                    if (event.instrument_handle >= latest_.size() || event.book.valid == 0) continue;
                    latest_[event.instrument_handle] = LatestBook{event.book, receive.wall_ms};
                }
            },
            [this](std::size_t, std::string_view) {
                decoder_->invalidate_all_lineage();
                std::lock_guard<std::mutex> lock(mutex_);
                for (auto& latest : latest_) latest = {};
            });
        feed_->start();
    }

    void stop() { if (feed_) feed_->stop(); }
    [[nodiscard]] bool fatal() const noexcept { return fatal_.load(std::memory_order_acquire); }

    [[nodiscard]] LatestBook snapshot(std::uint64_t instrument_handle) const {
        std::lock_guard<std::mutex> lock(mutex_);
        if (instrument_handle >= latest_.size()) return {};
        return latest_[instrument_handle];
    }

private:
    std::vector<SelectedToken> tokens_;
    std::string ws_url_;
    std::vector<std::string> ids_;
    mutable std::mutex mutex_;
    std::vector<LatestBook> latest_;
    std::unique_ptr<pm::v7::MarketWsShard> decoder_;
    std::unique_ptr<pm::fast::MarketWebSocketFeed> feed_;
    std::atomic<bool> fatal_{false};
};

struct PendingFill {
    std::string order_id;
    std::string fill_id;
    std::string market_id;
    std::string event_id;
    std::string token_id;
    std::uint64_t instrument_handle = 0;
    Side side = Side::None;
    std::int32_t fill_price_e4 = 0;
    std::int64_t quantity_microunits = 0;
    std::int64_t receive_ts_ms = 0;
    std::uint8_t completed_mask = 0;
};

class MarkoutEngine final {
public:
    MarkoutEngine(fs::path run_root, std::string model_sha, std::vector<SelectedToken> tokens)
        : run_root_(std::move(run_root)), model_sha_(std::move(model_sha)), tokens_(std::move(tokens)) {
        for (const auto& token : tokens_) token_to_handle_[token.token_id] = token.instrument_handle;
        fs::create_directories(run_root_ / "ledger" / "spool");
        bootstrap();
    }

    void poll_ledger() {
        const fs::path ledger = run_root_ / "ledger" / "execution.jsonl";
        if (!fs::exists(ledger)) return;
        const auto size = fs::file_size(ledger);
        if (size < offset_) {
            pending_.clear();
            bootstrap();
            return;
        }
        if (size == offset_) return;
        read_complete_tail(ledger, false);
    }

    void observe(const BookObserver& books) {
        const std::int64_t now = wall_ms();
        for (auto it = pending_.begin(); it != pending_.end();) {
            auto& fill = it->second;
            for (std::size_t h = 0; h < kHorizonSeconds.size(); ++h) {
                const std::uint8_t bit = static_cast<std::uint8_t>(1U << h);
                if ((fill.completed_mask & bit) != 0) continue;
                const std::int64_t due = fill.receive_ts_ms
                    + static_cast<std::int64_t>(kHorizonSeconds[h]) * 1000LL;
                if (now < due) continue;
                const LatestBook latest = books.snapshot(fill.instrument_handle);
                if (latest.book.valid == 0 || latest.wall_receive_ms < due) continue;
                // The first valid canonical snapshot observed at/after the horizon
                // closes that horizon. If L10 cannot liquidate the full fill, the
                // measurement is honestly missing rather than retried later.
                fill.completed_mask = static_cast<std::uint8_t>(fill.completed_mask | bit);
                const auto markout = pm::v7::executable_markout(
                    latest.book, fill.side, fill.quantity_microunits, fill.fill_price_e4);
                if (markout.observable) write_markout(fill, latest, h, markout);
            }
            if (fill.completed_mask == 0x1fU) it = pending_.erase(it);
            else ++it;
        }
    }

private:
    void read_complete_tail(const fs::path& ledger, bool bootstrap_mode) {
        std::ifstream input(ledger, std::ios::binary);
        if (!input) return;
        input.seekg(static_cast<std::streamoff>(offset_));
        if (!input) return;
        const std::string chunk{
            std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
        const std::size_t complete = pm::v7::complete_jsonl_prefix_size(chunk);
        if (complete == 0) return;

        const std::string_view view(chunk.data(), complete);
        std::size_t begin = 0;
        while (begin < view.size()) {
            const auto newline = view.find('\n', begin);
            if (newline == std::string_view::npos) break;
            process_line(view.substr(begin, newline - begin), bootstrap_mode);
            begin = newline + 1;
        }
        // Crucially, do not advance over an unterminated final JSON record.
        // The next poll starts at the same byte and sees it once the writer has
        // completed the newline-terminated canonical ledger append.
        offset_ += static_cast<std::uint64_t>(complete);
    }

    void bootstrap() {
        offset_ = 0;
        const fs::path ledger = run_root_ / "ledger" / "execution.jsonl";
        if (!fs::exists(ledger)) return;
        read_complete_tail(ledger, true);
        const std::int64_t now = wall_ms();
        for (auto& [_, fill] : pending_) {
            for (std::size_t h = 0; h < kHorizonSeconds.size(); ++h) {
                const std::int64_t due = fill.receive_ts_ms
                    + static_cast<std::int64_t>(kHorizonSeconds[h]) * 1000LL;
                if (due <= now) fill.completed_mask = static_cast<std::uint8_t>(fill.completed_mask | (1U << h));
            }
        }
        for (auto it = pending_.begin(); it != pending_.end();) {
            if (it->second.completed_mask == 0x1fU) it = pending_.erase(it);
            else ++it;
        }
    }

    void process_line(std::string_view line, bool bootstrap_mode) {
        if (line.empty()) return;
        boost::system::error_code error;
        const auto value = json::parse(line, error);
        if (error || !value.is_object()) return;
        const auto& event = value.as_object();
        if (text(find_value(event, "model_sha")) != model_sha_
            || text(find_value(event, "strategy")) != kStrategy) return;
        const std::string type = text(find_value(event, "event_type"));
        if (type == "FILL") {
            PendingFill fill;
            fill.order_id = text(find_value(event, "order_id"));
            fill.fill_id = text(find_value(event, "fill_id"));
            fill.market_id = text(find_value(event, "market_id"));
            fill.event_id = text(find_value(event, "event_id"));
            fill.token_id = text(find_value(event, "token_id"));
            fill.side = parse_side(text(find_value(event, "side")));
            fill.fill_price_e4 = price_e4(number(find_value(event, "fill_price"), 0.0));
            fill.quantity_microunits = quantity_microunits(number(find_value(event, "filled_size"), 0.0));
            fill.receive_ts_ms = integer(find_value(event, "receive_ts_ms"), 0);
            const auto handle = token_to_handle_.find(fill.token_id);
            fill.instrument_handle = handle == token_to_handle_.end() ? 0 : handle->second;
            if (!fill.fill_id.empty() && !fill.order_id.empty() && fill.instrument_handle > 0
                && fill.side != Side::None && fill.fill_price_e4 > 0
                && fill.quantity_microunits > 0 && fill.receive_ts_ms > 0) {
                pending_.try_emplace(fill.fill_id, std::move(fill));
            }
            return;
        }
        if (type == "MARKOUT") {
            const std::string fill_id = text(find_value(event, "fill_id"));
            auto it = pending_.find(fill_id);
            if (it == pending_.end()) return;
            const auto* markouts = find_value(event, "markouts");
            if (markouts == nullptr || !markouts->is_object()) return;
            for (std::size_t h = 0; h < kHorizonLabels.size(); ++h) {
                if (markouts->as_object().contains(kHorizonLabels[h])) {
                    it->second.completed_mask = static_cast<std::uint8_t>(it->second.completed_mask | (1U << h));
                }
            }
            if (!bootstrap_mode && it->second.completed_mask == 0x1fU) pending_.erase(it);
        }
    }

    void write_markout(const PendingFill& fill, const LatestBook& latest,
                       std::size_t horizon_index,
                       const pm::v7::ExecutableMarkout& markout) {
        const std::int64_t recorded = wall_ms();
        const std::uint64_t identity = fnv1a(fill.fill_id)
            ^ (static_cast<std::uint64_t>(kHorizonSeconds[horizon_index]) << 48U);
        const std::string record_id = "cpp-mm-markout-" + hex64(identity);
        json::object event;
        event["schema_version"] = 1;
        event["event_type"] = "MARKOUT";
        event["strategy"] = kStrategy;
        event["model_sha"] = model_sha_;
        event["paper_only"] = true;
        event["authenticated_execution"] = false;
        event["record_id"] = record_id;
        event["recorded_ts_ms"] = recorded;
        event["order_id"] = fill.order_id;
        event["fill_id"] = fill.fill_id;
        event["market_id"] = fill.market_id;
        if (!fill.event_id.empty()) event["event_id"] = fill.event_id;
        event["token_id"] = fill.token_id;
        event["side"] = fill.side == Side::Buy ? "BUY" : "SELL";
        event["exchange_ts_ms"] = std::max<std::int64_t>(1, latest.book.exchange_event_ns / 1'000'000LL);
        event["receive_ts_ms"] = latest.wall_receive_ms;
        event["book_snapshot_id"] = "mm-markout-book-" + std::to_string(fill.instrument_handle)
            + "-" + std::to_string(latest.book.state_version);
        event["executable_liquidation_value"] = markout.executable_liquidation_value;
        json::object values;
        values[kHorizonLabels[horizon_index]] = markout.pnl_per_share;
        event["markouts"] = std::move(values);
        json::object metadata;
        metadata["maker_markout_observer"] = true;
        metadata["fill_conditioned"] = true;
        metadata["full_l10_depth"] = true;
        metadata["future_vwap"] = markout.future_vwap;
        metadata["levels_used"] = static_cast<std::int64_t>(markout.levels_used);
        metadata["horizon_seconds"] = kHorizonSeconds[horizon_index];
        metadata["measurement_delay_ms"] = std::max<std::int64_t>(
            0, latest.wall_receive_ms - fill.receive_ts_ms
               - static_cast<std::int64_t>(kHorizonSeconds[horizon_index]) * 1000LL);
        event["metadata"] = std::move(metadata);

        const fs::path target = run_root_ / "ledger" / "spool" /
            (std::to_string(recorded) + "." + record_id + ".json");
        atomic_write(target, json::serialize(event) + "\n");
    }

    fs::path run_root_;
    std::string model_sha_;
    std::vector<SelectedToken> tokens_;
    std::unordered_map<std::string, std::uint64_t> token_to_handle_;
    std::unordered_map<std::string, PendingFill> pending_;
    std::uint64_t offset_ = 0;
};

} // namespace

int main(int argc, char** argv) {
    try {
        std::signal(SIGINT, signal_handler);
        std::signal(SIGTERM, signal_handler);
        const Options options = parse_options(argc, argv);
        const pm::Config config = pm::load_config(options.config);
        auto tokens = build_tokens(options, config);
        BookObserver books(tokens, options.ws_url);
        MarkoutEngine engine(options.run_root, options.model_sha, std::move(tokens));
        books.start();
        while (!g_stop.load(std::memory_order_relaxed)) {
            if (books.fatal()) throw std::runtime_error("markout WS bounded decoder overflow");
            engine.poll_ledger();
            engine.observe(books);
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        books.stop();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_maker_markout_observer: " << error.what() << '\n';
        return 2;
    }
}
