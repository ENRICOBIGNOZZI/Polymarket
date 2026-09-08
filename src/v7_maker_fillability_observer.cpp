#include "pm/api.hpp"
#include "pm/config.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_market_ws.hpp"
#include "pm/v7_maker_lane.hpp"
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
    std::string output_dir;
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
        else if (arg == "--output-dir") options.output_dir = next();
        else if (arg == "--model-sha") options.model_sha = next();
        else if (arg == "--ws-url") options.ws_url = next();
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (options.selection.empty()) {
        options.selection = options.run_root + "/micro_maker/reward_selection.json";
    }
    if (options.output_dir.empty()) {
        options.output_dir = options.run_root + "/micro_maker";
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

// The current fair-market pair is observed independently of maker eligibility.
// This adds public data coverage only; selection and authorization stay upstream.
[[nodiscard]] std::vector<std::pair<std::string, std::pair<std::string, std::string>>>
fair_observation_pairs(const Options& options) {
    try {
        const auto root = read_json(fs::path(options.run_root) / "external_fair" / "status.json");
        if (!root.is_object()) return {};
        const auto& object = root.as_object();
        if (text(find_value(object, "code_sha")) != options.model_sha
            || !boolean(find_value(object, "paper_only"), false)
            || boolean(find_value(object, "authenticated_execution"), true)
            || boolean(find_value(object, "real_order_submission"), true)) return {};
        const auto* raw = find_value(object, "market");
        if (!raw || !raw->is_object()) return {};
        const auto& market = raw->as_object();
        const auto id = text(find_value(market, "market_id"));
        const auto yes = text(find_value(market, "yes_token"));
        const auto no = text(find_value(market, "no_token"));
        if (id.empty() || yes.empty() || no.empty() || yes == no) return {};
        return {{id + "\n" + text(find_value(market, "event_id")), {yes, no}}};
    } catch (const std::exception&) { return {}; }
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

    for (const auto& fair : fair_observation_pairs(options)) {
        const bool included = std::any_of(pairs.begin(), pairs.end(), [&](const auto& pair) {
            return pair.second == fair.second;
        });
        if (!included) pairs.push_back(fair);
    }

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
    MarketWsEventKind kind = MarketWsEventKind::Trade;
    pm::v7::BookHotSnapshot book{};
    pm::v7::maker::Features features{};
    bool features_valid = false;
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
                    fs::path output_dir, std::string model_sha)
        : tokens_(std::move(tokens)), ws_url_(std::move(ws_url)),
          output_dir_(std::move(output_dir)), model_sha_(std::move(model_sha)) {
        std::vector<pm::v7::TokenBinding> bindings;
        std::size_t max_handle = 0;
        for (const auto& token : tokens_) {
            bindings.push_back({token.token_id, token.market_handle, token.event_handle,
                                token.instrument_handle, token.tick_size_e4});
            ids_.push_back(token.token_id);
            max_handle = std::max<std::size_t>(max_handle, token.instrument_handle);
        }
        by_handle_.resize(max_handle + 1, nullptr);
        lanes_.resize(max_handle + 1);
        feature_start_ns_.resize(max_handle + 1, 0);
        latest_books_.resize(max_handle + 1);
        for (const auto& token : tokens_) by_handle_[token.instrument_handle] = &token;
        for (const auto& token : tokens_) {
            lanes_[token.instrument_handle] = std::make_unique<pm::v7::maker::MakerInstrumentLane>(1);
        }
        decoder_ = std::make_unique<pm::v7::MarketWsShard>(std::move(bindings));
        fs::create_directories(output_dir_);
        evidence_path_ = output_dir_ / "fillability_ws.jsonl";
        status_path_ = output_dir_ / "fillability_ws_status.json";
        output_.open(evidence_path_, std::ios::app);
        if (!output_) throw std::runtime_error("cannot open exact-WS fillability evidence file");
        fs::create_directories(output_dir_ / "book_observations");
        book_path_ = output_dir_ / "book_observations" / "current.jsonl";
        book_output_.open(book_path_, std::ios::app);
        if (!book_output_) throw std::runtime_error("cannot open canonical book evidence file");
        session_id_ = std::to_string(wall_ms()) + "-" + std::to_string(::getpid());
    }

    void on_frame(std::string_view payload, const pm::fast::FeedReceiveStamp& receive) {
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
        if (result.invalid_frame || result.output_overflow || result.arena_exhausted) {
            decoder_failures_.fetch_add(1, std::memory_order_relaxed);
        }
        for (std::size_t i = 0; i < result.output_count; ++i) {
            const auto& event = events[i];
            if (event.instrument_handle == 0 || event.instrument_handle >= lanes_.size()) continue;
            if (event.kind == MarketWsEventKind::Trade && (
                event.price_e4 <= 0 || event.quantity_microunits <= 0
                || event.exchange_event_ns <= 0 || event.side == Side::None)) {
                continue;
            }
            TradeEvidence row;
            row.kind = event.kind;
            row.book = event.book;
            auto& lane = lanes_[event.instrument_handle];
            auto& started = feature_start_ns_[event.instrument_handle];
            if (!event.book.valid || !event.book.lineage_continuous
                || event.kind == MarketWsEventKind::LineageInvalidated
                || event.kind == MarketWsEventKind::TickSizeChanged) {
                // All path-dependent estimates restart across gaps and
                // tick regimes. The first full book starts a new cut.
                *lane = pm::v7::maker::MakerInstrumentLane(1);
                started = 0;
            } else {
                if (started == 0) started = receive.monotonic_ns;
                pm::v7::maker::MakerLaneContext context;
                context.risk.new_risk_frozen = 1;
                row.features = lane->on_market_event(event, context, feature_model_).features;
                row.features_valid = receive.monotonic_ns - started >= 1'000'000'000;
            }
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
            if (!queue_->try_push(row)) dropped_.fetch_add(1, std::memory_order_relaxed);
        }
    }

    void on_reconnect() {
        decoder_->invalidate_all_lineage();
        connection_epoch_.fetch_add(1, std::memory_order_relaxed);
        reconnects_.fetch_add(1, std::memory_order_relaxed);
        for (std::size_t i=1; i<lanes_.size(); ++i) {
            if (lanes_[i]) *lanes_[i] = pm::v7::maker::MakerInstrumentLane(1);
            feature_start_ns_[i] = 0;
        }
    }

    void start() {
        feed_ = std::make_unique<pm::fast::MarketWebSocketFeed>(
            ws_url_, ids_, std::max<std::size_t>(1, ids_.size()),
            [this](std::string_view payload, const pm::fast::FeedReceiveStamp& receive, std::size_t) {
                on_frame(payload, receive);
            },
            [this](std::size_t, std::string_view) {
                on_reconnect();
            });
        feed_->start();
    }

    void stop() {
        if (feed_) feed_->stop();
        drain();
        output_.flush();
        book_output_.flush();
        if (!output_ || !book_output_) throw std::runtime_error("cannot flush canonical observer evidence");
        write_status(true);
    }

    void drain() {
        TradeEvidence row;
        bool wrote = false;
        while (queue_->try_pop(row)) {
            if (row.kind == MarketWsEventKind::Trade) write(row);
            write_book(row);
            wrote = true;
        }
        if (wrote) { output_.flush(); book_output_.flush(); }
        if (book_output_.tellp() >= 64 * 1024 * 1024) {
            book_output_.close();
            const auto sealed = book_path_.parent_path() / (session_id_ + ".segment-"
                + std::to_string(1'000'000 + book_segment_++) + ".jsonl");
            fs::rename(book_path_, sealed);
            book_output_.open(book_path_, std::ios::app);
            if (!book_output_) throw std::runtime_error("cannot rotate causal book evidence");
        }
        if (wall_ms() - last_book_publish_ms_ >= 50) {
            for (std::size_t i=1; i<latest_books_.size(); ++i) {
                if (latest_books_[i].empty()) continue;
                atomic_write(output_dir_ / "book_features" / (by_handle_[i]->token_id + ".json"),
                             latest_books_[i]);
                latest_books_[i].clear();
            }
            last_book_publish_ms_ = wall_ms();
        }
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
        root["observer_session_id"] = session_id_;
        root["book_events_written"] = book_events_written_;
        root["book_watermark_receive_wall_ms"] = book_watermark_wall_ms_;
        root["book_watermark_receive_monotonic_ns"] = book_watermark_monotonic_ns_;
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
    void write_book(const TradeEvidence& row) {
        if (row.instrument_handle >= by_handle_.size()) return;
        const auto* token = by_handle_[row.instrument_handle];
        if (!token) return;
        const auto& f = row.features;
        json::object features{
            {"spread_ticks", f.spread_ticks}, {"imbalance", f.imbalance}, {"ofi", f.ofi},
            {"ew_vol_ticks", f.ew_vol_ticks}, {"short_return_ticks", f.short_return_ticks},
            {"trade_intensity", f.trade_intensity}, {"cancel_intensity", f.cancel_intensity},
            {"aggressive_buy_prints_per_second", f.aggressive_buy_prints_per_second},
            {"aggressive_sell_prints_per_second", f.aggressive_sell_prints_per_second},
            {"local_latency_ms", f.local_latency_ms}, {"inventory_fraction", nullptr},
        };
        const bool valid = row.book.valid && row.book.lineage_continuous
            && dropped_.load(std::memory_order_relaxed) == 0
            && decoder_failures_.load(std::memory_order_relaxed) == 0;
        json::object value{
            {"schema", "polymarket_v7_causal_book_observation_v1"},
            {"model_sha", model_sha_}, {"paper_only", true},
            {"authenticated_execution", false}, {"real_order_submission", false},
            {"execution_authority", "ZERO_AUTHORITY_RESEARCH_ONLY"},
            {"observer_session_id", session_id_}, {"connection_epoch", row.connection_epoch},
            {"observer_sequence", ++book_events_written_}, {"market_id", token->market_id},
            {"token_id", token->token_id}, {"state_version", row.state_version},
            {"receive_wall_ms", row.receive_wall_ms}, {"receive_monotonic_ns", row.receive_monotonic_ns},
            {"exchange_event_ns", row.book.exchange_event_ns},
            {"book_receive_monotonic_ns", row.book.receive_monotonic_ns},
            {"event_kind", static_cast<std::uint64_t>(row.kind)},
            {"valid", valid}, {"lineage_continuous", row.book.lineage_continuous != 0},
            {"features_valid", valid && row.features_valid},
            {"tick_size", e4_price(row.book.tick_size_e4)},
            {"best_bid", e4_price(row.book.best_bid_e4)}, {"best_ask", e4_price(row.book.best_ask_e4)},
            {"bid_depth_l1", micro_shares(row.book.bid_depth.l1_microunits)},
            {"ask_depth_l1", micro_shares(row.book.ask_depth.l1_microunits)},
            {"placement_features", std::move(features)},
            {"feature_semantics", "CANONICAL_MAKER_LANE_OBSERVED_FLOW_V1"},
            {"cancel_intensity_semantics", "L5_CONTRACTION_MINUS_OBSERVED_TRADES_NORMALIZED_EW_PROXY"},
        };
        const auto serialized = json::serialize(value) + "\n";
        book_output_ << serialized;
        latest_books_[row.instrument_handle] = serialized;
        book_watermark_wall_ms_ = std::max(book_watermark_wall_ms_, row.receive_wall_ms);
        book_watermark_monotonic_ns_ = std::max(book_watermark_monotonic_ns_, row.receive_monotonic_ns);
    }

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
    fs::path output_dir_;
    std::string model_sha_;
    std::vector<std::string> ids_;
    std::vector<const SelectedToken*> by_handle_;
    std::vector<std::unique_ptr<pm::v7::maker::MakerInstrumentLane>> lanes_;
    std::vector<std::int64_t> feature_start_ns_;
    std::vector<std::string> latest_books_;
    pm::v7::maker::MakerModelSnapshot feature_model_;
    std::string session_id_;
    std::ofstream book_output_;
    fs::path book_path_;
    std::uint64_t book_segment_ = 0;
    std::uint64_t book_events_written_ = 0;
    std::int64_t last_book_publish_ms_ = 0;
    std::int64_t book_watermark_wall_ms_ = 0;
    std::int64_t book_watermark_monotonic_ns_ = 0;
    std::unique_ptr<pm::v7::MarketWsShard> decoder_;
    std::unique_ptr<pm::fast::MarketWebSocketFeed> feed_;
    std::unique_ptr<pm::v7::SpscRing<TradeEvidence, kEvidenceCapacity>> queue_ =
        std::make_unique<pm::v7::SpscRing<TradeEvidence, kEvidenceCapacity>>();
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
        while (!g_stop.load(std::memory_order_relaxed)) {
            const auto fair_pairs = fair_observation_pairs(options);
            auto tokens = build_tokens(options, config);
            std::error_code stamp_error;
            const auto selection_stamp = fs::last_write_time(options.selection, stamp_error);
            ExactWsObserver observer(
                std::move(tokens), options.ws_url, options.output_dir, options.model_sha);
            observer.start();
            std::int64_t last_status_ms = 0;
            bool reload = false;
            while (!g_stop.load(std::memory_order_relaxed) && !reload) {
                observer.drain();
                const auto now = wall_ms();
                if (now - last_status_ms >= 1000) {
                    observer.write_status();
                    last_status_ms = now;
                    std::error_code current_error;
                    const auto current_stamp = fs::last_write_time(options.selection, current_error);
                    reload = (!stamp_error && !current_error && current_stamp != selection_stamp)
                        || fair_observation_pairs(options) != fair_pairs;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            observer.stop();
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_maker_fillability_observer: " << error.what() << '\n';
        return 2;
    }
}
