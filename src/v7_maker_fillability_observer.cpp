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
#include <deque>
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
// Bounded rolling window: diagnostics only; never grows the hot-path heap.
constexpr std::size_t kPureArbLatencySamples = 4096;
constexpr std::size_t kPureArbOutputCapacity = 4096;
constexpr std::size_t kPureArbDeepCapacity = 64;
constexpr std::array<double, 7> kPureArbReserveArms{
    0.0, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005
};

std::atomic<bool> g_stop{false};
void signal_handler(int) noexcept { g_stop.store(true, std::memory_order_relaxed); }

[[nodiscard]] std::int64_t wall_ms() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch()).count();
}

[[nodiscard]] std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
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

[[nodiscard]] std::int64_t integer64(const json::value* value, std::int64_t fallback = 0) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_int64()) return value->as_int64();
    if (value->is_uint64() && value->as_uint64() <= static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
        return static_cast<std::int64_t>(value->as_uint64());
    }
    return fallback;
}

[[nodiscard]] double number64(const json::value* value,
                              double fallback = std::numeric_limits<double>::quiet_NaN()) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_double()) return value->as_double();
    if (value->is_int64()) return static_cast<double>(value->as_int64());
    if (value->is_uint64()) return static_cast<double>(value->as_uint64());
    return fallback;
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
    bool fair_only = false;
    std::uintmax_t disk_pressure_min_free_bytes = 0;
    bool selection_only = false;
    bool state_only = false;
    std::int64_t state_publish_ms = 100;
    fs::path compact_label_tape_dir;
    bool selection_explicit = false;
    bool pure_arb_paper = false;
    double pure_arb_reserve_per_share = 0.0005;
    std::int64_t pure_arb_max_leg_skew_ms = 100;
    std::int64_t pure_arb_max_receive_to_decision_ns = 50'000'000LL;
    double pure_arb_prefunded_complete_set_shares = 1000.0;
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
        else if (arg == "--selection") { options.selection = next(); options.selection_explicit = true; }
        else if (arg == "--run-root") options.run_root = next();
        else if (arg == "--output-dir") options.output_dir = next();
        else if (arg == "--model-sha") options.model_sha = next();
        else if (arg == "--ws-url") options.ws_url = next();
        else if (arg == "--fair-only") options.fair_only = true;
        else if (arg == "--disk-pressure-min-free-bytes") options.disk_pressure_min_free_bytes = std::stoull(next());
        else if (arg == "--selection-only") options.selection_only = true;
        else if (arg == "--state-only") options.state_only = true;
        else if (arg == "--state-publish-ms") options.state_publish_ms = std::stoll(next());
        else if (arg == "--compact-label-tape-dir") options.compact_label_tape_dir = next();
        else if (arg == "--pure-arb-paper") options.pure_arb_paper = true;
        else if (arg == "--pure-arb-reserve-per-share") options.pure_arb_reserve_per_share = std::stod(next());
        else if (arg == "--pure-arb-max-leg-skew-ms") options.pure_arb_max_leg_skew_ms = std::stoll(next());
        else if (arg == "--pure-arb-max-receive-to-decision-ms") options.pure_arb_max_receive_to_decision_ns = std::stoll(next()) * 1'000'000LL;
        else if (arg == "--pure-arb-prefunded-complete-set-shares") options.pure_arb_prefunded_complete_set_shares = std::stod(next());
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (options.fair_only && options.selection_only) {
        throw std::runtime_error("--fair-only and --selection-only are mutually exclusive");
    }
    if (options.selection_only && !options.selection_explicit) {
        throw std::runtime_error("--selection-only requires explicit --selection");
    }
    if (options.state_only && !options.selection_only) {
        throw std::runtime_error("--state-only requires --selection-only");
    }
    if (options.state_publish_ms < 10 || options.state_publish_ms > 1000) {
        throw std::runtime_error("--state-publish-ms must be in [10,1000]");
    }
    if (!options.compact_label_tape_dir.empty() && (!options.state_only || !options.selection_only)) {
        throw std::runtime_error("--compact-label-tape-dir requires --selection-only --state-only");
    }
    if (options.pure_arb_paper && (!options.selection_only || options.state_only || options.fair_only)) {
        throw std::runtime_error("--pure-arb-paper requires the full selection-only causal book observer");
    }
    if (!std::isfinite(options.pure_arb_reserve_per_share)
        || options.pure_arb_reserve_per_share < 0.0 || options.pure_arb_reserve_per_share >= 1.0) {
        throw std::runtime_error("--pure-arb-reserve-per-share must be in [0,1)");
    }
    if (options.pure_arb_max_leg_skew_ms < 0 || options.pure_arb_max_leg_skew_ms > 5000) {
        throw std::runtime_error("--pure-arb-max-leg-skew-ms must be in [0,5000]");
    }
    if (options.pure_arb_max_receive_to_decision_ns < 1'000'000LL
        || options.pure_arb_max_receive_to_decision_ns > 5'000'000'000LL) {
        throw std::runtime_error("--pure-arb-max-receive-to-decision-ms must be in [1,5000]");
    }
    if (!std::isfinite(options.pure_arb_prefunded_complete_set_shares)
        || options.pure_arb_prefunded_complete_set_shares <= 0.0
        || options.pure_arb_prefunded_complete_set_shares > 1'000'000.0) {
        throw std::runtime_error("--pure-arb-prefunded-complete-set-shares must be in (0,1000000]");
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
    std::uint8_t is_yes = 0;
    std::int64_t start_wall_ms = 0;
    std::int64_t end_wall_ms = 0;
    std::string asset;
    std::string horizon;
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    std::uint8_t fee_verified = 0;
};

[[nodiscard]] std::vector<std::pair<std::string, std::pair<std::string, std::string>>>
load_selected_pairs(const fs::path& path, bool require_selection_only = false,
                    std::string_view expected_model_sha = {}) {
    const auto root = read_json(path);
    if (!root.is_object()) throw std::runtime_error("maker selection must be object");
    const auto& object = root.as_object();
    if (const auto* value = find_value(object, "paper_only"); value != nullptr && !boolean(value, false)) {
        throw std::runtime_error("fillability observer selection is not PAPER-only");
    }
    if (const auto* value = find_value(object, "authenticated_execution"); value != nullptr && boolean(value, true)) {
        throw std::runtime_error("fillability observer selection enables authentication");
    }
    if (const auto* value = find_value(object, "real_order_submission"); value != nullptr && boolean(value, true)) {
        throw std::runtime_error("fillability observer selection enables real order submission");
    }
    if (require_selection_only) {
        if (text(find_value(object, "schema")) != "polymarket_v7_multi_crypto_book_selection_v1"
            || !boolean(find_value(object, "selection_only"), false)
            || boolean(find_value(object, "execution_authority"), true)
            || boolean(find_value(object, "real_capital_at_risk"), true)
            || expected_model_sha.empty()
            || text(find_value(object, "model_sha")) != expected_model_sha) {
            throw std::runtime_error("selection-only contract invalid");
        }
    }
    const auto* raw = find_value(object, "markets");
    if (raw == nullptr || !raw->is_array()) throw std::runtime_error("maker selection missing markets");
    std::vector<std::pair<std::string, std::pair<std::string, std::string>>> output;
    for (const auto& item : raw->as_array()) {
        if (!item.is_object() || output.size() >= 64) break;
        const auto& row = item.as_object();
        const std::string market = text(find_value(row, "market_id"));
        const std::string event = text(find_value(row, "event_id"));
        const std::string yes = text(find_value(row, "yes_token"));
        const std::string no = text(find_value(row, "no_token"));
        if (market.empty() || yes.empty() || no.empty() || yes == no) continue;
        output.push_back({market + "\n" + event, {yes, no}});
    }
    if (output.empty()) throw std::runtime_error("fillability observer has no selected markets");
    std::sort(output.begin(), output.end());
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
    if (options.fair_only) {
        while (!g_stop.load(std::memory_order_relaxed)) {
            pairs = fair_observation_pairs(options);
            if (!pairs.empty()) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        if (pairs.empty()) throw std::runtime_error("fair-only observation pair unavailable");
    } else {
        while (!g_stop.load(std::memory_order_relaxed)) {
            try {
                if (fs::exists(options.selection) && fs::file_size(options.selection) > 0) {
                    pairs = load_selected_pairs(options.selection, options.selection_only, options.model_sha);
                    break;
                }
            } catch (const std::exception& error) {
                std::cerr << "fillability selection not ready: " << error.what() << '\n';
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
        if (pairs.empty()) throw std::runtime_error("fillability selection unavailable");
    }

    if (!options.selection_only) {
        for (const auto& fair : fair_observation_pairs(options)) {
            const bool included = std::any_of(pairs.begin(), pairs.end(), [&](const auto& pair) {
                return pair.second == fair.second;
            });
            if (!included) pairs.push_back(fair);
        }
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
                // The fair-only observer is the causal repricing source. A
                // subscriber can join after a venue tick-size transition and
                // therefore miss the historical WS tick_size_change event.
                // Resolve the current token tick from the dedicated CLOB
                // endpoint at each cold start/recovery instead of trusting a
                // stale bootstrap regime. Any lookup failure retries closed.
                const double yes_tick_value = options.fair_only
                    ? api.fetch_tick_size(pair.second.first) : yes->second.tick_size;
                const double no_tick_value = options.fair_only
                    ? api.fetch_tick_size(pair.second.second) : no->second.tick_size;
                const std::int32_t yes_tick = tick_e4(yes_tick_value);
                const std::int32_t no_tick = tick_e4(no_tick_value);
                if (yes_tick <= 0 || no_tick <= 0) continue;
                const auto market = ++market_handle;
                output.push_back({market_id, event_id, pair.second.first, market, market,
                                  ++instrument_handle, yes_tick, 1, 0, 0});
                output.push_back({market_id, event_id, pair.second.second, market, market,
                                  ++instrument_handle, no_tick, 0, 0, 0});
            }
            if (!output.empty()) return output;
        } catch (const std::exception& error) {
            std::cerr << "fillability cold-start book fetch failed: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    throw std::runtime_error("fillability observer stopped before cold-start books became available");
}

void apply_selection_windows(std::vector<SelectedToken>& tokens, const fs::path& path,
                             bool require_complete = true) {
    const auto root = read_json(path);
    if (!root.is_object()) throw std::runtime_error("selection window source must be object");
    const auto* raw = find_value(root.as_object(), "markets");
    if (raw == nullptr || !raw->is_array()) throw std::runtime_error("selection windows missing markets");
    std::size_t assigned = 0;
    for (const auto& item : raw->as_array()) {
        if (!item.is_object()) continue;
        const auto& row = item.as_object();
        const auto market_id = text(find_value(row, "market_id"));
        const auto start_ms = integer64(find_value(row, "start_timestamp_ms"));
        const auto end_ms = integer64(find_value(row, "end_timestamp_ms"));
        const auto asset = text(find_value(row, "asset"));
        const auto horizon = text(find_value(row, "horizon"));
        if (market_id.empty() || start_ms <= 0 || end_ms <= start_ms
            || asset.empty() || horizon.empty()) {
            throw std::runtime_error("selection market window invalid");
        }
        double fee_rate = 0.0;
        double fee_exponent = 1.0;
        bool fee_verified = false;
        const auto* fee_value = find_value(row, "fee_schedule");
        if (fee_value != nullptr && fee_value->is_object()) {
            const auto& fee = fee_value->as_object();
            const auto rate = number64(find_value(fee, "rate"));
            const auto exponent = number64(find_value(fee, "exponent"), 1.0);
            if (std::isfinite(rate) && rate >= 0.0 && rate <= 1.0
                && std::isfinite(exponent) && exponent >= 0.0) {
                fee_rate = rate;
                fee_exponent = exponent;
                fee_verified = true;
            }
        }
        if (!fee_verified
            && boolean(find_value(row, "fees_enabled_explicit"), false)
            && !boolean(find_value(row, "fees_enabled"), true)) {
            fee_rate = 0.0;
            fee_exponent = 1.0;
            fee_verified = true;
        }
        for (auto& token : tokens) {
            if (token.market_id == market_id) {
                token.start_wall_ms = start_ms;
                token.end_wall_ms = end_ms;
                token.asset = asset;
                token.horizon = horizon;
                token.fee_rate = fee_rate;
                token.fee_exponent = fee_exponent;
                token.fee_verified = fee_verified ? 1 : 0;
                ++assigned;
            }
        }
    }
    if (require_complete && assigned != tokens.size()) {
        throw std::runtime_error("selection window coverage incomplete");
    }
}

[[nodiscard]] bool defer_pure_arb_membership_reload(
    const fs::path& selection,
    const std::vector<std::pair<std::string, std::pair<std::string, std::string>>>& subscribed,
    std::int64_t now_ms,
    std::int64_t rollover_grace_ms = 30'000) {
    try {
        const auto root = read_json(selection);
        if (!root.is_object()) return false;
        const auto* raw = find_value(root.as_object(), "markets");
        if (raw == nullptr || !raw->is_array()) return false;
        std::size_t active = 0;
        std::int64_t youngest_age_ms = std::numeric_limits<std::int64_t>::max();
        for (const auto& item : raw->as_array()) {
            if (!item.is_object()) continue;
            const auto& row = item.as_object();
            const auto start_ms = integer64(find_value(row, "start_timestamp_ms"));
            const auto end_ms = integer64(find_value(row, "end_timestamp_ms"));
            if (!(start_ms <= now_ms && now_ms < end_ms)) continue;
            const auto market_id = text(find_value(row, "market_id"));
            const auto event_id = text(find_value(row, "event_id"));
            const auto yes = text(find_value(row, "yes_token"));
            const auto no = text(find_value(row, "no_token"));
            if (market_id.empty() || yes.empty() || no.empty()) return false;
            const auto key = market_id + "\n" + event_id;
            const bool covered = std::any_of(subscribed.begin(), subscribed.end(),
                [&](const auto& pair) {
                    return pair.first == key && pair.second.first == yes && pair.second.second == no;
                });
            if (!covered) return false;
            ++active;
            youngest_age_ms = std::min(youngest_age_ms, now_ms - start_ms);
        }
        // The active execution/observation contract remains exactly 30 contexts.
        // If all newly-active pairs were already preloaded, keep the socket hot
        // through the boundary. Reload later, away from the boundary, to preload
        // the next generation.
        return active == 30 && youngest_age_ms >= 0 && youngest_age_ms < rollover_grace_ms;
    } catch (const std::exception&) {
        return false;
    }
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
    std::int64_t enqueue_monotonic_ns = 0;
    std::int32_t price_e4 = 0;
    std::int64_t quantity_microunits = 0;
    Side aggressor_side = Side::None;
    std::uint8_t lineage_continuous = 0;
    std::array<std::uint8_t, 6> reserved{};
};
static_assert(std::is_trivially_copyable_v<TradeEvidence>);

struct PureArbPairBinding {
    std::uint64_t market_handle = 0;
    std::uint64_t yes_handle = 0;
    std::uint64_t no_handle = 0;
};

struct PureArbDeepEvidence {
    std::uint64_t market_handle = 0;
    std::uint64_t connection_epoch = 0;
    std::int64_t receive_wall_ms = 0;
    std::int64_t trigger_receive_monotonic_ns = 0;
    pm::v7::BookDeepSnapshot yes{};
    pm::v7::BookDeepSnapshot no{};
};
static_assert(std::is_trivially_copyable_v<PureArbDeepEvidence>);

struct FlowSample {
    std::int64_t receive_wall_ms = 0;
    double shares = 0.0;
    Side side = Side::None;
    std::uint64_t connection_epoch = 0;
};

struct PureArbDirectionState {
    bool active = false;
    std::uint64_t cycles = 0;
    double paper_locked_pnl = 0.0;
    double conservative_locked_pnl = 0.0;
    double max_edge_per_share = 0.0;
    double last_edge_per_share = 0.0;
    double last_executable_shares = 0.0;
    double last_executable_shares_l10 = 0.0;
    double last_executable_shares_deep = 0.0;
    double last_locked_pnl = 0.0;
    double last_conservative_locked_pnl = 0.0;
    std::int64_t last_detect_wall_ms = 0;
};

struct PureArbMarketState {
    std::string market_id;
    std::string asset;
    std::string horizon;
    std::uint64_t yes_handle = 0;
    std::uint64_t no_handle = 0;
    double fee_rate = 0.0;
    double fee_exponent = 1.0;
    std::uint8_t fee_verified = 0;
    std::int64_t start_wall_ms = 0;
    std::int64_t end_wall_ms = 0;
    // PAPER inventory reservoir: consumed by SELL_COMPLETE_SET and replenished
    // only when this market handle rolls to a new contract. Never replenished
    // merely because the reservoir reaches zero inside the same contract.
    double prefunded_complete_set_shares_remaining = 0.0;
    PureArbDirectionState buy{};
    PureArbDirectionState sell{};
};

struct PureArbSweepResult {
    std::int64_t shares_microunits = 0;
    double gross_locked_pnl = 0.0;
    double conservative_locked_pnl = 0.0;
    double yes_notional = 0.0;
    double no_notional = 0.0;
    double marginal_edge_per_share = 0.0;
    std::uint16_t yes_levels_used = 0;
    std::uint16_t no_levels_used = 0;

    [[nodiscard]] double shares() const noexcept {
        return micro_shares(shares_microunits);
    }
    [[nodiscard]] double gross_edge_per_share() const noexcept {
        const double q = shares();
        return q > 0.0 ? gross_locked_pnl / q : 0.0;
    }
    [[nodiscard]] double conservative_edge_per_share() const noexcept {
        const double q = shares();
        return q > 0.0 ? conservative_locked_pnl / q : 0.0;
    }
    [[nodiscard]] double yes_vwap() const noexcept {
        const double q = shares();
        return q > 0.0 ? yes_notional / q : 0.0;
    }
    [[nodiscard]] double no_vwap() const noexcept {
        const double q = shares();
        return q > 0.0 ? no_notional / q : 0.0;
    }
};

struct PureArbQueuedEvent {
    std::uint64_t market_handle = 0;
    std::uint8_t kind = 0; // 1=BUY_COMPLETE_SET, 2=SELL_COMPLETE_SET
    std::uint8_t reserved0 = 0;
    std::uint16_t yes_levels_used = 0;
    std::uint16_t no_levels_used = 0;
    std::int64_t receive_wall_ms = 0;
    std::int64_t receive_to_decision_ns = 0;
    std::int64_t shares_microunits = 0;
    double gross_edge_per_share = 0.0;
    double conservative_edge_per_share = 0.0;
    double marginal_edge_per_share = 0.0;
    double executable_shares_l1 = 0.0;
    double executable_shares_l10 = 0.0;
    double gross_locked_pnl = 0.0;
    double conservative_locked_pnl = 0.0;
    double yes_vwap = 0.0;
    double no_vwap = 0.0;
};
static_assert(std::is_trivially_copyable_v<PureArbQueuedEvent>);

struct PureArbFunnelState {
    std::uint64_t book_updates = 0;
    std::uint64_t handles_ready = 0;
    std::uint64_t market_window = 0;
    std::uint64_t fee_ready = 0;
    std::uint64_t epoch_synced = 0;
    std::uint64_t leg_skew_ready = 0;
    std::uint64_t lineage_ready = 0;
    std::uint64_t book_valid = 0;
    std::uint64_t fee_finite = 0;
    std::uint64_t buy_raw_positive = 0;
    std::uint64_t buy_after_fee_positive = 0;
    std::uint64_t buy_after_reserve_positive = 0;
    std::uint64_t buy_l10_executable = 0;
    std::uint64_t buy_fresh_decision = 0;
    std::uint64_t sell_raw_positive = 0;
    std::uint64_t sell_after_fee_positive = 0;
    std::uint64_t sell_after_reserve_positive = 0;
    std::uint64_t sell_l10_executable = 0;
    std::uint64_t sell_fresh_decision = 0;
    std::uint64_t buy_cycles_recorded = 0;
    std::uint64_t sell_cycles_recorded = 0;
    std::uint64_t stale_decision_rejections = 0;
    std::array<std::uint64_t, kPureArbReserveArms.size()> buy_reserve_positive{};
    std::array<std::uint64_t, kPureArbReserveArms.size()> sell_reserve_positive{};
};

class PureArbLatencyWindow final {
public:
    void add(std::int64_t value) noexcept {
        value = std::max<std::int64_t>(0, value);
        values_[next_] = value;
        next_ = (next_ + 1) % values_.size();
        size_ = std::min<std::size_t>(size_ + 1, values_.size());
    }

    [[nodiscard]] std::int64_t quantile(double q) const {
        if (size_ == 0) return 0;
        std::vector<std::int64_t> copy;
        copy.reserve(size_);
        for (std::size_t i = 0; i < size_; ++i) copy.push_back(values_[i]);
        std::sort(copy.begin(), copy.end());
        const auto index = static_cast<std::size_t>(
            std::clamp(q, 0.0, 1.0) * static_cast<double>(copy.size() - 1));
        return copy[index];
    }

    [[nodiscard]] std::size_t size() const noexcept { return size_; }

private:
    std::array<std::int64_t, kPureArbLatencySamples> values_{};
    std::size_t next_ = 0;
    std::size_t size_ = 0;
};

class ExactWsObserver final {
public:
    ExactWsObserver(std::vector<SelectedToken> tokens, std::string ws_url,
                    fs::path output_dir, std::string model_sha, bool state_only = false,
                    std::int64_t state_publish_ms = 100, bool recover_missing_lineage = false,
                    fs::path compact_label_tape_dir = {}, bool pure_arb_paper = false,
                    double pure_arb_reserve_per_share = 0.0005,
                    std::int64_t pure_arb_max_leg_skew_ms = 100,
                    std::int64_t pure_arb_max_receive_to_decision_ns = 50'000'000LL,
                    double pure_arb_prefunded_complete_set_shares = 1000.0)
        : tokens_(std::move(tokens)), ws_url_(std::move(ws_url)),
          output_dir_(std::move(output_dir)), model_sha_(std::move(model_sha)),
          state_only_(state_only), state_publish_ms_(state_publish_ms),
          recover_missing_lineage_(recover_missing_lineage),
          compact_label_tape_dir_(std::move(compact_label_tape_dir)),
          pure_arb_paper_(pure_arb_paper),
          pure_arb_reserve_per_share_(pure_arb_reserve_per_share),
          pure_arb_max_leg_skew_ms_(pure_arb_max_leg_skew_ms),
          pure_arb_receive_to_decision_limit_ns_(pure_arb_max_receive_to_decision_ns),
          pure_arb_prefunded_complete_set_shares_(pure_arb_prefunded_complete_set_shares) {
        std::vector<pm::v7::TokenBinding> bindings;
        std::size_t max_handle = 0;
        std::size_t max_market_handle = 0;
        for (const auto& token : tokens_) {
            bindings.push_back({token.token_id, token.market_handle, token.event_handle,
                                token.instrument_handle, token.tick_size_e4});
            ids_.push_back(token.token_id);
            max_handle = std::max<std::size_t>(max_handle, token.instrument_handle);
            max_market_handle = std::max<std::size_t>(max_market_handle, token.market_handle);
        }
        by_handle_.resize(max_handle + 1, nullptr);
        lanes_.resize(max_handle + 1);
        feature_start_ns_.resize(max_handle + 1, 0);
        latest_books_.resize(max_handle + 1);
        flow_samples_.resize(max_handle + 1);
        book_token_seen_.resize(max_handle + 1, 0);
        book_market_seen_.resize(max_market_handle + 1, 0);
        pure_arb_latest_books_.resize(max_handle + 1);
        pure_arb_book_epochs_.resize(max_handle + 1, 0);
        pure_arb_book_receive_wall_ms_.resize(max_handle + 1, 0);
        pure_arb_markets_.resize(max_market_handle + 1);
        pure_arb_pair_by_handle_.resize(max_handle + 1);
        pure_arb_deep_trigger_active_.resize(max_market_handle + 1, 0);
        for (const auto& token : tokens_) by_handle_[token.instrument_handle] = &token;
        for (const auto& token : tokens_) {
            auto& market = pure_arb_markets_[token.market_handle];
            market.market_id = token.market_id;
            market.asset = token.asset;
            market.horizon = token.horizon;
            market.fee_rate = token.fee_rate;
            market.fee_exponent = token.fee_exponent;
            market.fee_verified = token.fee_verified;
            market.start_wall_ms = token.start_wall_ms;
            market.end_wall_ms = token.end_wall_ms;
            market.prefunded_complete_set_shares_remaining =
                pure_arb_prefunded_complete_set_shares_;
            if (token.is_yes != 0) market.yes_handle = token.instrument_handle;
            else market.no_handle = token.instrument_handle;
        }
        for (const auto& token : tokens_) {
            const auto& market = pure_arb_markets_[token.market_handle];
            pure_arb_pair_by_handle_[token.instrument_handle] = PureArbPairBinding{
                token.market_handle, market.yes_handle, market.no_handle};
        }
        for (const auto& token : tokens_) {
            lanes_[token.instrument_handle] = std::make_unique<pm::v7::maker::MakerInstrumentLane>(1);
        }
        decoder_ = std::make_unique<pm::v7::MarketWsShard>(std::move(bindings));
        fs::create_directories(output_dir_);
        evidence_path_ = output_dir_ / "fillability_ws.jsonl";
        status_path_ = output_dir_ / "fillability_ws_status.json";
        flow_path_ = output_dir_ / "fillability_flow_snapshot.json";
        pure_arb_status_path_ = output_dir_ / "pure_arb_status.json";
        pure_arb_trades_path_ = output_dir_ / "pure_arb_trades.jsonl";
        fs::create_directories(output_dir_ / "book_features");
        if (!state_only_) {
            output_.open(evidence_path_, std::ios::app);
            if (!output_) throw std::runtime_error("cannot open exact-WS fillability evidence file");
            fs::create_directories(output_dir_ / "book_observations");
            book_path_ = output_dir_ / "book_observations" / "current.jsonl";
            book_output_.open(book_path_, std::ios::app);
            if (!book_output_) throw std::runtime_error("cannot open canonical book evidence file");
        }
        session_id_ = std::to_string(wall_ms()) + "-" + std::to_string(::getpid());
        if (!compact_label_tape_dir_.empty()) initialize_compact_label_tape();
        if (pure_arb_paper_) {
            restore_pure_arb_status();
            pure_arb_output_.open(pure_arb_trades_path_, std::ios::app);
            if (!pure_arb_output_) throw std::runtime_error("cannot open pure arb PAPER evidence file");
        }
    }

    void maybe_queue_pure_arb_deep(
        const MarketWsEvent& event, const pm::fast::FeedReceiveStamp& receive) noexcept {
        if (!pure_arb_paper_ || event.instrument_handle == 0
            || event.instrument_handle >= pure_arb_pair_by_handle_.size()
            || event.book.valid == 0 || event.book.lineage_continuous == 0) {
            return;
        }
        const auto binding = pure_arb_pair_by_handle_[event.instrument_handle];
        if (binding.market_handle == 0 || binding.yes_handle == 0 || binding.no_handle == 0
            || binding.market_handle >= pure_arb_deep_trigger_active_.size()) {
            return;
        }
        const auto yes_hot = decoder_->snapshot(binding.yes_handle);
        const auto no_hot = decoder_->snapshot(binding.no_handle);
        if (yes_hot.valid == 0 || no_hot.valid == 0
            || yes_hot.lineage_continuous == 0 || no_hot.lineage_continuous == 0) {
            pure_arb_deep_trigger_active_[binding.market_handle] = 0;
            return;
        }
        const double raw_buy = 1.0 - e4_price(yes_hot.best_ask_e4) - e4_price(no_hot.best_ask_e4);
        const double raw_sell = e4_price(yes_hot.best_bid_e4) + e4_price(no_hot.best_bid_e4) - 1.0;
        const bool candidate = raw_buy > pure_arb_reserve_per_share_ + 1e-12
            || raw_sell > pure_arb_reserve_per_share_ + 1e-12;
        if (!candidate) {
            pure_arb_deep_trigger_active_[binding.market_handle] = 0;
            return;
        }
        if (pure_arb_deep_trigger_active_[binding.market_handle] != 0) return;

        PureArbDeepEvidence deep{};
        deep.market_handle = binding.market_handle;
        deep.connection_epoch = connection_epoch_.load(std::memory_order_relaxed);
        deep.receive_wall_ms = receive.wall_ms;
        deep.trigger_receive_monotonic_ns = receive.monotonic_ns;
        deep.yes = decoder_->deep_snapshot(binding.yes_handle);
        deep.no = decoder_->deep_snapshot(binding.no_handle);
        if (deep.yes.valid == 0 || deep.no.valid == 0
            || deep.yes.bid_truncated != 0 || deep.yes.ask_truncated != 0
            || deep.no.bid_truncated != 0 || deep.no.ask_truncated != 0) {
            ++pure_arb_deep_snapshot_rejections_;
            return;
        }
        if (!pure_arb_deep_queue_->try_push(deep)) {
            ++pure_arb_deep_queue_drops_;
            return;
        }
        pure_arb_deep_trigger_active_[binding.market_handle] = 1;
        ++pure_arb_deep_candidates_;
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
        lineage_invalid_book_snapshot_.fetch_add(
            result.lineage_invalid_book_snapshot, std::memory_order_relaxed);
        lineage_invalid_price_change_.fetch_add(
            result.lineage_invalid_price_change, std::memory_order_relaxed);
        lineage_invalid_tick_size_change_.fetch_add(
            result.lineage_invalid_tick_size_change, std::memory_order_relaxed);
        price_change_without_lineage_.fetch_add(
            result.price_change_without_lineage, std::memory_order_relaxed);
        // Frame/arena/output corruption is global. Token-local lineage failures
        // are handled below and trigger recovery only when that contract is active;
        // future preloaded contracts are allowed to remain FEEDS_WARMING.
        const bool root_lineage_failure =
            result.invalid_frame || result.output_overflow || result.arena_exhausted;
        if (root_lineage_failure) {
            root_lineage_recovery_requested_.store(true, std::memory_order_release);
            if (!lineage_recovery_requested_.exchange(true, std::memory_order_acq_rel)) {
                lineage_recovery_requests_.fetch_add(1, std::memory_order_relaxed);
            }
        }
        unknown_asset_.fetch_add(
            result.ignored_unknown_assets, std::memory_order_relaxed);
        if (result.invalid_frame || result.output_overflow || result.arena_exhausted) {
            decoder_failures_.fetch_add(1, std::memory_order_relaxed);
        }
        for (std::size_t i = 0; i < result.output_count; ++i) {
            const auto& event = events[i];
            if (event.instrument_handle == 0 || event.instrument_handle >= lanes_.size()) continue;
            if (recover_missing_lineage_ && event.kind == MarketWsEventKind::LineageInvalidated
                && token_active(event.instrument_handle, receive.wall_ms)) {
                if (!lineage_recovery_requested_.exchange(true, std::memory_order_acq_rel)) {
                    lineage_recovery_requests_.fetch_add(1, std::memory_order_relaxed);
                }
            }
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
            maybe_queue_pure_arb_deep(event, receive);
            row.enqueue_monotonic_ns = monotonic_ns();
            if (!queue_->try_push(row)) dropped_.fetch_add(1, std::memory_order_relaxed);
        }
        // A later full WS snapshot may already have healed the affected token.
        // Do not restart a recovered stream merely because a past root failure
        // latched the request. Missing/damaged evidence can never self-heal.
        if (lineage_recovery_requested_.load(std::memory_order_acquire)
            && decoder_failures_.load(std::memory_order_relaxed) == 0
            && dropped_.load(std::memory_order_relaxed) == 0) {
            const auto now_wall = receive.wall_ms; // causal frame receive time, not process wall clock
            const bool still_missing = std::any_of(tokens_.begin(), tokens_.end(),
                [&](const SelectedToken& token) {
                    return token.start_wall_ms <= now_wall && now_wall < token.end_wall_ms
                        && decoder_->snapshot(token.instrument_handle).lineage_continuous == 0;
                });
            if (!still_missing) {
                lineage_recovery_requested_.store(false, std::memory_order_release);
                lineage_recovered_without_restart_.fetch_add(1, std::memory_order_relaxed);
            }
        }
    }

    void set_disk_pressure(bool active) noexcept {
        disk_pressure_.store(active, std::memory_order_release);
    }

    [[nodiscard]] bool disk_pressure() const noexcept {
        return disk_pressure_.load(std::memory_order_acquire);
    }

    [[nodiscard]] bool lineage_recovery_requested() const noexcept {
        return lineage_recovery_requested_.load(std::memory_order_acquire);
    }

    [[nodiscard]] bool root_lineage_recovery_requested() const noexcept {
        return root_lineage_recovery_requested_.load(std::memory_order_acquire);
    }

    [[nodiscard]] std::uint64_t lineage_recovery_requests() const noexcept {
        return lineage_recovery_requests_.load(std::memory_order_relaxed);
    }

    [[nodiscard]] bool token_active(std::uint64_t instrument_handle, std::int64_t receive_wall_ms) const noexcept {
        if (instrument_handle == 0 || instrument_handle >= by_handle_.size()) return false;
        const auto* token = by_handle_[instrument_handle];
        return token != nullptr && token->start_wall_ms > 0 && token->end_wall_ms > token->start_wall_ms
            && token->start_wall_ms <= receive_wall_ms && receive_wall_ms < token->end_wall_ms;
    }

    void on_reconnect() {
        decoder_->invalidate_all_lineage();
        // A transport reconnect may cross a venue tick-size transition. The
        // in-memory bindings were created from the pre-reconnect cold-start
        // books, so accepting later snapshots against those stale ticks can
        // invalidate every update for the affected contract. Force the outer
        // loop to rebuild all bindings from authoritative current CLOB books.
        root_lineage_recovery_requested_.store(true, std::memory_order_release);
        if (!lineage_recovery_requested_.exchange(true, std::memory_order_acq_rel)) {
            lineage_recovery_requests_.fetch_add(1, std::memory_order_relaxed);
        }
        connection_epoch_.fetch_add(1, std::memory_order_relaxed);
        reconnects_.fetch_add(1, std::memory_order_relaxed);
        reset_pure_arb_state();
        std::fill(pure_arb_deep_trigger_active_.begin(), pure_arb_deep_trigger_active_.end(), 0);
        for (std::size_t i=1; i<lanes_.size(); ++i) {
            if (lanes_[i]) *lanes_[i] = pm::v7::maker::MakerInstrumentLane(1);
            feature_start_ns_[i] = 0;
            flow_samples_[i].clear();
        }
    }

    [[nodiscard]] static double pure_arb_fee_per_share(
        double price, double rate, double exponent) noexcept {
        if (!std::isfinite(price) || price <= 0.0 || price >= 1.0
            || !std::isfinite(rate) || rate < 0.0 || rate > 1.0
            || !std::isfinite(exponent) || exponent < 0.0) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        return rate == 0.0 ? 0.0 : rate * std::pow(price * (1.0 - price), exponent);
    }

    template <std::size_t N>
    [[nodiscard]] PureArbSweepResult sweep_pure_arb_levels(
        const std::array<pm::v7::PriceLevelE4, N>& yes_levels,
        std::size_t yes_count,
        const std::array<pm::v7::PriceLevelE4, N>& no_levels,
        std::size_t no_count,
        const PureArbMarketState& market,
        bool buy,
        std::int64_t maximum_shares_microunits) const noexcept {
        PureArbSweepResult result{};
        std::size_t yi = 0, ni = 0;
        std::int64_t yes_remaining = 0, no_remaining = 0;
        std::int64_t capacity_remaining = std::max<std::int64_t>(0, maximum_shares_microunits);

        while (yi < yes_count && ni < no_count && capacity_remaining > 0) {
            if (yes_remaining <= 0) yes_remaining = yes_levels[yi].quantity_microunits;
            if (no_remaining <= 0) no_remaining = no_levels[ni].quantity_microunits;
            if (yes_remaining <= 0) { ++yi; continue; }
            if (no_remaining <= 0) { ++ni; continue; }

            const double yes_price = e4_price(yes_levels[yi].price_e4);
            const double no_price = e4_price(no_levels[ni].price_e4);
            const double fee = pure_arb_fee_per_share(
                yes_price, market.fee_rate, market.fee_exponent)
                + pure_arb_fee_per_share(no_price, market.fee_rate, market.fee_exponent);
            if (!std::isfinite(fee)) break;

            const double gross_edge = buy
                ? 1.0 - yes_price - no_price - fee
                : yes_price + no_price - 1.0 - fee;
            if (!(gross_edge > pure_arb_reserve_per_share_ + 1e-12)) break;

            const auto quantity = std::min({yes_remaining, no_remaining, capacity_remaining});
            if (quantity <= 0) break;
            const double shares = micro_shares(quantity);
            result.shares_microunits += quantity;
            result.gross_locked_pnl += shares * gross_edge;
            result.conservative_locked_pnl += shares * (gross_edge - pure_arb_reserve_per_share_);
            result.yes_notional += shares * yes_price;
            result.no_notional += shares * no_price;
            result.marginal_edge_per_share = gross_edge;
            result.yes_levels_used = static_cast<std::uint16_t>(
                std::min<std::size_t>(std::numeric_limits<std::uint16_t>::max(),
                                      std::max<std::size_t>(result.yes_levels_used, yi + 1)));
            result.no_levels_used = static_cast<std::uint16_t>(
                std::min<std::size_t>(std::numeric_limits<std::uint16_t>::max(),
                                      std::max<std::size_t>(result.no_levels_used, ni + 1)));

            yes_remaining -= quantity;
            no_remaining -= quantity;
            capacity_remaining -= quantity;
            if (yes_remaining <= 0) ++yi;
            if (no_remaining <= 0) ++ni;
        }
        return result;
    }

    [[nodiscard]] PureArbSweepResult sweep_pure_arb(
        const pm::v7::BookHotSnapshot& yes,
        const pm::v7::BookHotSnapshot& no,
        const PureArbMarketState& market,
        bool buy,
        std::int64_t maximum_shares_microunits = std::numeric_limits<std::int64_t>::max()) const noexcept {
        return buy
            ? sweep_pure_arb_levels(
                yes.ask_levels, yes.ask_level_count,
                no.ask_levels, no.ask_level_count,
                market, true, maximum_shares_microunits)
            : sweep_pure_arb_levels(
                yes.bid_levels, yes.bid_level_count,
                no.bid_levels, no.bid_level_count,
                market, false, maximum_shares_microunits);
    }

    [[nodiscard]] PureArbSweepResult sweep_pure_arb(
        const pm::v7::BookDeepSnapshot& yes,
        const pm::v7::BookDeepSnapshot& no,
        const PureArbMarketState& market,
        bool buy,
        std::int64_t maximum_shares_microunits = std::numeric_limits<std::int64_t>::max()) const noexcept {
        return buy
            ? sweep_pure_arb_levels(
                yes.ask_levels, yes.ask_level_count,
                no.ask_levels, no.ask_level_count,
                market, true, maximum_shares_microunits)
            : sweep_pure_arb_levels(
                yes.bid_levels, yes.bid_level_count,
                no.bid_levels, no.bid_level_count,
                market, false, maximum_shares_microunits);
    }

    void restore_pure_arb_status() {
        if (!pure_arb_paper_ || !fs::exists(pure_arb_status_path_)) return;
        try {
            const auto value = read_json(pure_arb_status_path_);
            if (!value.is_object()) return;
            const auto& root = value.as_object();
            if (text(find_value(root, "schema")) != "polymarket_v7_pure_arb_paper_status_v1"
                || text(find_value(root, "model_sha")) != model_sha_
                || !boolean(find_value(root, "paper_only"), false)
                || boolean(find_value(root, "authenticated_execution"), true)
                || boolean(find_value(root, "real_order_submission"), true)) return;

            pure_arb_total_cycles_ = static_cast<std::uint64_t>(
                std::max<std::int64_t>(0, integer64(find_value(root, "cycles_total"))));
            pure_arb_total_pnl_ = number64(find_value(root, "paper_locked_pnl_pre_gas_total"), 0.0);
            pure_arb_conservative_total_pnl_ = number64(
                find_value(root, "conservative_locked_pnl_after_reserve_total"), 0.0);
            pure_arb_evaluations_ = static_cast<std::uint64_t>(
                std::max<std::int64_t>(0, integer64(find_value(root, "evaluations"))));
            pure_arb_fee_blocked_evaluations_ = static_cast<std::uint64_t>(
                std::max<std::int64_t>(0, integer64(find_value(root, "fee_blocked_evaluations"))));
            // Economic counters survive a restart; latency diagnostics do not.
            // Carrying a prior-generation stall into a fresh process makes the
            // current tail impossible to diagnose.

            const auto* contexts = find_value(root, "contexts");
            if (contexts == nullptr || !contexts->is_array()) return;
            for (const auto& item : contexts->as_array()) {
                if (!item.is_object()) continue;
                const auto& prior = item.as_object();
                const auto asset = text(find_value(prior, "asset"));
                const auto horizon = text(find_value(prior, "horizon"));
                const auto prior_market_id = text(find_value(prior, "market_id"));
                auto it = std::find_if(pure_arb_markets_.begin(), pure_arb_markets_.end(),
                    [&](const PureArbMarketState& market) {
                        return market.asset == asset && market.horizon == horizon;
                    });
                if (it == pure_arb_markets_.end()) continue;
                auto restore_direction = [&](std::string_view key,
                                             PureArbDirectionState& direction) {
                    const auto* raw = find_value(prior, key);
                    if (raw == nullptr || !raw->is_object()) return;
                    const auto& row = raw->as_object();
                    direction.cycles = static_cast<std::uint64_t>(
                        std::max<std::int64_t>(0, integer64(find_value(row, "cycles"))));
                    direction.paper_locked_pnl = number64(
                        find_value(row, "paper_locked_pnl_pre_gas"), 0.0);
                    direction.conservative_locked_pnl = number64(
                        find_value(row, "conservative_locked_pnl_after_reserve"), 0.0);
                    direction.max_edge_per_share = number64(
                        find_value(row, "max_edge_per_share"), 0.0);
                    direction.last_edge_per_share = number64(
                        find_value(row, "last_edge_per_share"), 0.0);
                    direction.last_executable_shares = number64(
                        find_value(row, "last_executable_shares_l1"), 0.0);
                    direction.last_executable_shares_l10 = number64(
                        find_value(row, "last_executable_shares_l10"), direction.last_executable_shares);
                    direction.last_executable_shares_deep = number64(
                        find_value(row, "last_executable_shares_deep"),
                        direction.last_executable_shares_l10);
                    direction.last_locked_pnl = number64(
                        find_value(row, "last_locked_pnl_pre_gas"), 0.0);
                    direction.last_conservative_locked_pnl = number64(
                        find_value(row, "last_conservative_locked_pnl_after_reserve"), 0.0);
                    direction.last_detect_wall_ms = integer64(
                        find_value(row, "last_detect_wall_ms"));
                    direction.active = prior_market_id == it->market_id
                        && boolean(find_value(row, "active"), false);
                };
                restore_direction("buy_complete_set", it->buy);
                restore_direction("sell_complete_set", it->sell);
                if (prior_market_id == it->market_id) {
                    const double prior_remaining = number64(
                        find_value(prior, "prefunded_complete_set_shares_remaining"),
                        it->prefunded_complete_set_shares_remaining);
                    if (std::isfinite(prior_remaining) && prior_remaining >= 0.0
                        && prior_remaining <= pure_arb_prefunded_complete_set_shares_) {
                        it->prefunded_complete_set_shares_remaining = prior_remaining;
                    }
                }
            }
        } catch (const std::exception&) {
            // Non-authoritative PAPER telemetry only. Corrupt/stale state
            // starts a fresh generation and never affects the market stream.
        }
    }

    void refresh_pure_arb_metadata(const fs::path& selection) {
        if (!pure_arb_paper_) return;
        apply_selection_windows(tokens_, selection, false);
        for (const auto& token : tokens_) {
            auto& market = pure_arb_markets_[token.market_handle];
            const bool new_market = !market.market_id.empty()
                && market.market_id != token.market_id;
            market.market_id = token.market_id;
            market.asset = token.asset;
            market.horizon = token.horizon;
            market.fee_rate = token.fee_rate;
            market.fee_exponent = token.fee_exponent;
            market.fee_verified = token.fee_verified;
            market.start_wall_ms = token.start_wall_ms;
            market.end_wall_ms = token.end_wall_ms;
            if (new_market) {
                market.prefunded_complete_set_shares_remaining =
                    pure_arb_prefunded_complete_set_shares_;
            }
        }
    }

    void reset_pure_arb_state() noexcept {
        if (!pure_arb_paper_) return;
        for (auto& book : pure_arb_latest_books_) book = pm::v7::BookHotSnapshot{};
        std::fill(pure_arb_book_epochs_.begin(), pure_arb_book_epochs_.end(), 0);
        std::fill(pure_arb_book_receive_wall_ms_.begin(), pure_arb_book_receive_wall_ms_.end(), 0);
        for (auto& market : pure_arb_markets_) {
            market.buy.active = false;
            market.sell.active = false;
        }
    }

    void record_pure_arb_cycle(std::uint64_t market_handle,
                               PureArbMarketState& market,
                               PureArbDirectionState& direction,
                               std::uint8_t kind,
                               const PureArbSweepResult& sweep,
                               double executable_shares_l1,
                               std::int64_t receive_wall_ms,
                               std::int64_t receive_monotonic_ns,
                               std::int64_t decision_ns) {
        const double shares = sweep.shares();
        const double edge_per_share = sweep.gross_edge_per_share();
        direction.active = true;
        ++direction.cycles;
        direction.paper_locked_pnl += sweep.gross_locked_pnl;
        direction.conservative_locked_pnl += sweep.conservative_locked_pnl;
        direction.max_edge_per_share = std::max(direction.max_edge_per_share, edge_per_share);
        direction.last_edge_per_share = edge_per_share;
        direction.last_executable_shares = executable_shares_l1;
        direction.last_executable_shares_deep = shares;
        direction.last_locked_pnl = sweep.gross_locked_pnl;
        direction.last_conservative_locked_pnl = sweep.conservative_locked_pnl;
        direction.last_detect_wall_ms = receive_wall_ms;
        pure_arb_total_pnl_ += sweep.gross_locked_pnl;
        pure_arb_conservative_total_pnl_ += sweep.conservative_locked_pnl;
        ++pure_arb_total_cycles_;
        if (kind == 1) ++pure_arb_funnel_.buy_cycles_recorded;
        else if (kind == 2) ++pure_arb_funnel_.sell_cycles_recorded;

        PureArbQueuedEvent event{};
        event.market_handle = market_handle;
        event.kind = kind;
        event.yes_levels_used = sweep.yes_levels_used;
        event.no_levels_used = sweep.no_levels_used;
        event.receive_wall_ms = receive_wall_ms;
        event.receive_to_decision_ns = std::max<std::int64_t>(
            0, decision_ns - receive_monotonic_ns);
        event.shares_microunits = sweep.shares_microunits;
        event.gross_edge_per_share = edge_per_share;
        event.conservative_edge_per_share = sweep.conservative_edge_per_share();
        event.marginal_edge_per_share = sweep.marginal_edge_per_share;
        event.executable_shares_l1 = executable_shares_l1;
        event.executable_shares_l10 = direction.last_executable_shares_l10;
        event.gross_locked_pnl = sweep.gross_locked_pnl;
        event.conservative_locked_pnl = sweep.conservative_locked_pnl;
        event.yes_vwap = sweep.yes_vwap();
        event.no_vwap = sweep.no_vwap();
        if (!pure_arb_event_queue_->try_push(event)) {
            ++pure_arb_event_queue_drops_;
        }
    }

    void flush_pure_arb_events() {
        if (!pure_arb_paper_ || !pure_arb_output_) return;
        PureArbQueuedEvent queued{};
        bool wrote = false;
        while (pure_arb_event_queue_->try_pop(queued)) {
            if (queued.market_handle == 0
                || queued.market_handle >= pure_arb_markets_.size()) continue;
            const auto& market = pure_arb_markets_[queued.market_handle];
            const char* kind = queued.kind == 1 ? "BUY_COMPLETE_SET" : "SELL_COMPLETE_SET";
            json::object event{
                {"schema", "polymarket_v7_pure_arb_paper_cycle_v3"},
                {"model_sha", model_sha_},
                {"paper_only", true},
                {"authenticated_execution", false},
                {"real_order_submission", false},
                {"real_capital_at_risk", false},
                {"execution_authority", "ZERO_AUTHORITY_PAPER_SIMULATION"},
                {"strategy", "PURE_COMPLETE_SET_ARB"},
                {"kind", kind},
                {"asset", market.asset},
                {"horizon", market.horizon},
                {"market_id", market.market_id},
                {"receive_wall_ms", queued.receive_wall_ms},
                {"receive_to_decision_ns", queued.receive_to_decision_ns},
                {"edge_per_share", queued.gross_edge_per_share},
                {"conservative_edge_per_share", queued.conservative_edge_per_share},
                {"marginal_edge_per_share", queued.marginal_edge_per_share},
                {"reserve_per_share", pure_arb_reserve_per_share_},
                {"executable_shares_l1", queued.executable_shares_l1},
                {"executable_shares_l10", queued.executable_shares_l10},
                {"executable_shares_local_deep", micro_shares(queued.shares_microunits)},
                {"paper_locked_pnl_pre_gas", queued.gross_locked_pnl},
                {"conservative_locked_pnl_after_reserve", queued.conservative_locked_pnl},
                {"yes_price", queued.yes_vwap},
                {"no_price", queued.no_vwap},
                {"yes_vwap", queued.yes_vwap},
                {"no_vwap", queued.no_vwap},
                {"yes_levels_used", queued.yes_levels_used},
                {"no_levels_used", queued.no_levels_used},
                {"sizing_depth", "LOCAL_DEEP_BOOK_POSITIVE_MARGINAL_EDGE"},
                {"fee_rate", market.fee_rate},
                {"fee_exponent", market.fee_exponent},
                {"artificial_delay_ms", 0},
                {"paired_fok_simulation", true},
                {"one_cycle_per_positive_episode", true},
                {"onchain_fixed_cost_modelled", false},
                {"prefunded_complete_set_for_sell", true},
                {"prefunded_complete_set_shares", pure_arb_prefunded_complete_set_shares_},
                {"hot_path_io", false},
            };
            pure_arb_output_ << json::serialize(event) << '\n';
            wrote = true;
        }
        if (wrote) pure_arb_output_.flush();
    }

    void evaluate_pure_arb(const TradeEvidence& row) {
        if (!pure_arb_paper_ || row.instrument_handle == 0
            || row.instrument_handle >= by_handle_.size()) return;
        ++pure_arb_funnel_.book_updates;
        const auto decision_started_ns = monotonic_ns();
        const auto* token = by_handle_[row.instrument_handle];
        if (token == nullptr || token->market_handle >= pure_arb_markets_.size()) return;
        pure_arb_latest_books_[row.instrument_handle] = row.book;
        pure_arb_book_epochs_[row.instrument_handle] = row.connection_epoch;
        pure_arb_book_receive_wall_ms_[row.instrument_handle] = row.receive_wall_ms;
        auto& market = pure_arb_markets_[token->market_handle];
        if (market.yes_handle == 0 || market.no_handle == 0
            || market.yes_handle >= pure_arb_latest_books_.size()
            || market.no_handle >= pure_arb_latest_books_.size()) return;
        ++pure_arb_funnel_.handles_ready;

        const auto& yes = pure_arb_latest_books_[market.yes_handle];
        const auto& no = pure_arb_latest_books_[market.no_handle];
        const auto yes_epoch = pure_arb_book_epochs_[market.yes_handle];
        const auto no_epoch = pure_arb_book_epochs_[market.no_handle];
        const auto yes_wall = pure_arb_book_receive_wall_ms_[market.yes_handle];
        const auto no_wall = pure_arb_book_receive_wall_ms_[market.no_handle];
        const auto now_wall = row.receive_wall_ms;

        if (!(token->start_wall_ms <= now_wall && now_wall < token->end_wall_ms)) {
            market.buy.active = false;
            market.sell.active = false;
            return;
        }
        ++pure_arb_funnel_.market_window;

        if (market.fee_verified == 0) {
            market.buy.active = false;
            market.sell.active = false;
            ++pure_arb_fee_blocked_evaluations_;
            return;
        }
        ++pure_arb_funnel_.fee_ready;

        if (yes_epoch == 0 || yes_epoch != no_epoch) {
            market.buy.active = false;
            market.sell.active = false;
            return;
        }
        ++pure_arb_funnel_.epoch_synced;

        if (yes_wall <= 0 || no_wall <= 0
            || (yes_wall >= no_wall ? yes_wall - no_wall : no_wall - yes_wall) > pure_arb_max_leg_skew_ms_) {
            market.buy.active = false;
            market.sell.active = false;
            return;
        }
        ++pure_arb_funnel_.leg_skew_ready;

        if (yes.valid == 0 || no.valid == 0
            || yes.lineage_continuous == 0 || no.lineage_continuous == 0) {
            market.buy.active = false;
            market.sell.active = false;
            return;
        }
        ++pure_arb_funnel_.lineage_ready;

        const bool book_valid =
            yes.best_bid_e4 > 0 && yes.best_ask_e4 > yes.best_bid_e4 && yes.best_ask_e4 < 10'000
            && no.best_bid_e4 > 0 && no.best_ask_e4 > no.best_bid_e4 && no.best_ask_e4 < 10'000
            && yes.best_bid_microunits > 0 && yes.best_ask_microunits > 0
            && no.best_bid_microunits > 0 && no.best_ask_microunits > 0
            && yes.bid_level_count > 0 && yes.ask_level_count > 0
            && no.bid_level_count > 0 && no.ask_level_count > 0;
        if (!book_valid) {
            market.buy.active = false;
            market.sell.active = false;
            return;
        }
        ++pure_arb_funnel_.book_valid;

        const double yes_ask = e4_price(yes.best_ask_e4);
        const double no_ask = e4_price(no.best_ask_e4);
        const double yes_bid = e4_price(yes.best_bid_e4);
        const double no_bid = e4_price(no.best_bid_e4);
        const double buy_fee = pure_arb_fee_per_share(
            yes_ask, market.fee_rate, market.fee_exponent)
            + pure_arb_fee_per_share(no_ask, market.fee_rate, market.fee_exponent);
        const double sell_fee = pure_arb_fee_per_share(
            yes_bid, market.fee_rate, market.fee_exponent)
            + pure_arb_fee_per_share(no_bid, market.fee_rate, market.fee_exponent);
        if (!std::isfinite(buy_fee) || !std::isfinite(sell_fee)) {
            market.buy.active = false;
            market.sell.active = false;
            return;
        }
        ++pure_arb_funnel_.fee_finite;

        const double buy_raw_edge = 1.0 - yes_ask - no_ask;
        const double sell_raw_edge = yes_bid + no_bid - 1.0;
        const double buy_edge = buy_raw_edge - buy_fee;
        const double sell_edge = sell_raw_edge - sell_fee;
        const auto buy_qty_l1 = std::min(yes.best_ask_microunits, no.best_ask_microunits);
        const auto sell_qty_l1 = std::min(yes.best_bid_microunits, no.best_bid_microunits);
        market.buy.last_edge_per_share = buy_edge;
        market.sell.last_edge_per_share = sell_edge;
        market.buy.last_executable_shares = micro_shares(buy_qty_l1);
        market.sell.last_executable_shares = micro_shares(sell_qty_l1);
        ++pure_arb_evaluations_;

        if (buy_raw_edge > 1e-12) ++pure_arb_funnel_.buy_raw_positive;
        if (sell_raw_edge > 1e-12) ++pure_arb_funnel_.sell_raw_positive;
        if (buy_edge > 1e-12) ++pure_arb_funnel_.buy_after_fee_positive;
        if (sell_edge > 1e-12) ++pure_arb_funnel_.sell_after_fee_positive;
        if (buy_edge > pure_arb_reserve_per_share_ + 1e-12) ++pure_arb_funnel_.buy_after_reserve_positive;
        if (sell_edge > pure_arb_reserve_per_share_ + 1e-12) ++pure_arb_funnel_.sell_after_reserve_positive;
        for (std::size_t i = 0; i < kPureArbReserveArms.size(); ++i) {
            if (buy_edge > kPureArbReserveArms[i] + 1e-12) {
                ++pure_arb_funnel_.buy_reserve_positive[i];
            }
            if (sell_edge > kPureArbReserveArms[i] + 1e-12) {
                ++pure_arb_funnel_.sell_reserve_positive[i];
            }
        }

        const auto buy_sweep = sweep_pure_arb(yes, no, market, true);
        const auto prefund_microunits = static_cast<std::int64_t>(std::llround(
            std::max(0.0, market.prefunded_complete_set_shares_remaining)
            * kMicrounitsPerShare));
        const auto sell_sweep = sweep_pure_arb(
            yes, no, market, false, prefund_microunits);
        market.buy.last_executable_shares_l10 = buy_sweep.shares();
        market.sell.last_executable_shares_l10 = sell_sweep.shares();
        if (buy_sweep.shares_microunits > 0) ++pure_arb_funnel_.buy_l10_executable;
        if (sell_sweep.shares_microunits > 0) ++pure_arb_funnel_.sell_l10_executable;

        const auto decision_ns = monotonic_ns();
        pure_arb_last_receive_to_enqueue_ns_ = std::max<std::int64_t>(
            0, row.enqueue_monotonic_ns - row.receive_monotonic_ns);
        pure_arb_last_queue_wait_ns_ = std::max<std::int64_t>(
            0, decision_started_ns - row.enqueue_monotonic_ns);
        pure_arb_last_decision_compute_ns_ = std::max<std::int64_t>(
            0, decision_ns - decision_started_ns);
        pure_arb_max_decision_compute_ns_ = std::max(
            pure_arb_max_decision_compute_ns_, pure_arb_last_decision_compute_ns_);
        pure_arb_last_receive_to_decision_ns_ = std::max<std::int64_t>(
            0, decision_ns - row.receive_monotonic_ns);
        pure_arb_max_receive_to_decision_ns_ = std::max(
            pure_arb_max_receive_to_decision_ns_, pure_arb_last_receive_to_decision_ns_);
        pure_arb_receive_to_enqueue_latency_.add(pure_arb_last_receive_to_enqueue_ns_);
        pure_arb_queue_wait_latency_.add(pure_arb_last_queue_wait_ns_);
        pure_arb_decision_latency_.add(pure_arb_last_decision_compute_ns_);
        pure_arb_receive_latency_.add(pure_arb_last_receive_to_decision_ns_);

        const bool fresh_decision =
            pure_arb_last_receive_to_decision_ns_ <= pure_arb_receive_to_decision_limit_ns_;
        if (!fresh_decision) {
            ++pure_arb_funnel_.stale_decision_rejections;
            market.buy.active = false;
            market.sell.active = false;
            return;
        }

        if (buy_sweep.shares_microunits > 0) {
            ++pure_arb_funnel_.buy_fresh_decision;
        } else {
            market.buy.active = false;
        }

        if (sell_sweep.shares_microunits > 0) {
            ++pure_arb_funnel_.sell_fresh_decision;
        } else {
            market.sell.active = false;
        }
    }

    void evaluate_pure_arb_deep(const PureArbDeepEvidence& row) {
        if (!pure_arb_paper_ || row.market_handle == 0
            || row.market_handle >= pure_arb_markets_.size()) return;
        if (row.connection_epoch != connection_epoch_.load(std::memory_order_relaxed)) {
            ++pure_arb_deep_snapshot_rejections_;
            return;
        }
        auto& market = pure_arb_markets_[row.market_handle];
        if (!(market.start_wall_ms <= row.receive_wall_ms
              && row.receive_wall_ms < market.end_wall_ms)
            || market.fee_verified == 0
            || row.yes.valid == 0 || row.no.valid == 0
            || row.yes.lineage_continuous == 0 || row.no.lineage_continuous == 0
            || row.yes.bid_truncated != 0 || row.yes.ask_truncated != 0
            || row.no.bid_truncated != 0 || row.no.ask_truncated != 0) {
            ++pure_arb_deep_snapshot_rejections_;
            return;
        }
        const auto skew_ns = row.yes.receive_monotonic_ns >= row.no.receive_monotonic_ns
            ? row.yes.receive_monotonic_ns - row.no.receive_monotonic_ns
            : row.no.receive_monotonic_ns - row.yes.receive_monotonic_ns;
        if (skew_ns > pure_arb_max_leg_skew_ms_ * 1'000'000LL) {
            ++pure_arb_deep_snapshot_rejections_;
            return;
        }

        const auto prefund_microunits = static_cast<std::int64_t>(std::llround(
            std::max(0.0, market.prefunded_complete_set_shares_remaining)
            * kMicrounitsPerShare));
        const auto buy_sweep = sweep_pure_arb(row.yes, row.no, market, true);
        const auto sell_sweep = sweep_pure_arb(
            row.yes, row.no, market, false, prefund_microunits);
        ++pure_arb_deep_evaluations_;

        const auto decision_ns = monotonic_ns();
        const auto receive_to_decision = std::max<std::int64_t>(
            0, decision_ns - row.trigger_receive_monotonic_ns);
        if (receive_to_decision > pure_arb_receive_to_decision_limit_ns_) {
            ++pure_arb_funnel_.stale_decision_rejections;
            market.buy.active = false;
            market.sell.active = false;
            return;
        }

        const double buy_l1 = row.yes.ask_level_count > 0 && row.no.ask_level_count > 0
            ? micro_shares(std::min(
                row.yes.ask_levels[0].quantity_microunits,
                row.no.ask_levels[0].quantity_microunits))
            : 0.0;
        const double sell_l1 = row.yes.bid_level_count > 0 && row.no.bid_level_count > 0
            ? micro_shares(std::min(
                row.yes.bid_levels[0].quantity_microunits,
                row.no.bid_levels[0].quantity_microunits))
            : 0.0;

        if (buy_sweep.shares_microunits > 0) {
            if (!market.buy.active) {
                record_pure_arb_cycle(
                    row.market_handle, market, market.buy, 1, buy_sweep,
                    buy_l1, row.receive_wall_ms,
                    row.trigger_receive_monotonic_ns, decision_ns);
            }
        } else {
            market.buy.active = false;
        }

        if (sell_sweep.shares_microunits > 0) {
            if (!market.sell.active) {
                record_pure_arb_cycle(
                    row.market_handle, market, market.sell, 2, sell_sweep,
                    sell_l1, row.receive_wall_ms,
                    row.trigger_receive_monotonic_ns, decision_ns);
                market.prefunded_complete_set_shares_remaining = std::max(
                    0.0,
                    market.prefunded_complete_set_shares_remaining - sell_sweep.shares());
            }
        } else {
            market.sell.active = false;
        }
    }

    void write_pure_arb_status(bool stopped) {
        if (!pure_arb_paper_) return;
        json::array contexts;
        const auto status_now_ms = wall_ms();
        std::uint64_t active_contexts = 0;
        std::uint64_t subscribed_contexts = 0;
        std::uint64_t preloaded_contexts = 0;
        std::uint64_t fee_ready = 0;
        std::uint64_t subscribed_fee_ready = 0;
        for (std::size_t i = 1; i < pure_arb_markets_.size(); ++i) {
            const auto& market = pure_arb_markets_[i];
            if (market.market_id.empty()) continue;
            ++subscribed_contexts;
            const bool active_window = market.start_wall_ms <= status_now_ms
                && status_now_ms < market.end_wall_ms;
            if (active_window) {
                ++active_contexts;
                fee_ready += market.fee_verified != 0;
            } else if (market.start_wall_ms > status_now_ms) {
                ++preloaded_contexts;
            }
            subscribed_fee_ready += market.fee_verified != 0;
            contexts.emplace_back(json::object{
                {"active_window", active_window},
                {"start_wall_ms", market.start_wall_ms},
                {"end_wall_ms", market.end_wall_ms},
                {"asset", market.asset},
                {"horizon", market.horizon},
                {"market_id", market.market_id},
                {"fee_verified", market.fee_verified != 0},
                {"prefunded_complete_set_shares_remaining",
                    market.prefunded_complete_set_shares_remaining},
                {"buy_complete_set", json::object{
                    {"active", market.buy.active},
                    {"cycles", market.buy.cycles},
                    {"paper_locked_pnl_pre_gas", market.buy.paper_locked_pnl},
                    {"conservative_locked_pnl_after_reserve", market.buy.conservative_locked_pnl},
                    {"last_edge_per_share", market.buy.last_edge_per_share},
                    {"max_edge_per_share", market.buy.max_edge_per_share},
                    {"last_executable_shares_l1", market.buy.last_executable_shares},
                    {"last_executable_shares_l10", market.buy.last_executable_shares_l10},
                    {"last_executable_shares_deep", market.buy.last_executable_shares_deep},
                    {"last_locked_pnl_pre_gas", market.buy.last_locked_pnl},
                    {"last_conservative_locked_pnl_after_reserve", market.buy.last_conservative_locked_pnl},
                    {"last_detect_wall_ms", market.buy.last_detect_wall_ms}}},
                {"sell_complete_set", json::object{
                    {"active", market.sell.active},
                    {"cycles", market.sell.cycles},
                    {"paper_locked_pnl_pre_gas", market.sell.paper_locked_pnl},
                    {"conservative_locked_pnl_after_reserve", market.sell.conservative_locked_pnl},
                    {"last_edge_per_share", market.sell.last_edge_per_share},
                    {"max_edge_per_share", market.sell.max_edge_per_share},
                    {"last_executable_shares_l1", market.sell.last_executable_shares},
                    {"last_executable_shares_l10", market.sell.last_executable_shares_l10},
                    {"last_executable_shares_deep", market.sell.last_executable_shares_deep},
                    {"last_locked_pnl_pre_gas", market.sell.last_locked_pnl},
                    {"last_conservative_locked_pnl_after_reserve", market.sell.last_conservative_locked_pnl},
                    {"last_detect_wall_ms", market.sell.last_detect_wall_ms}}},
            });
        }
        json::array reserve_arms;
        for (std::size_t i = 0; i < kPureArbReserveArms.size(); ++i) {
            reserve_arms.emplace_back(json::object{
                {"reserve_per_share", kPureArbReserveArms[i]},
                {"buy_positive_evaluations", pure_arb_funnel_.buy_reserve_positive[i]},
                {"sell_positive_evaluations", pure_arb_funnel_.sell_reserve_positive[i]}});
        }
        json::object status{
            {"schema", "polymarket_v7_pure_arb_paper_status_v1"},
            {"timestamp_ms", wall_ms()},
            {"model_sha", model_sha_},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"real_capital_at_risk", false},
            {"execution_authority", "ZERO_AUTHORITY_PAPER_SIMULATION"},
            {"state", stopped ? "stopped" : "running"},
            {"strategy", "PURE_COMPLETE_SET_ARB"},
            {"assets", json::array{"BTC","ETH","SOL","XRP","DOGE","BNB"}},
            {"horizons", json::array{"M5","M15","H1","H4","D1"}},
            {"expected_contexts", 30},
            {"active_contexts", active_contexts},
            {"subscribed_contexts", subscribed_contexts},
            {"preloaded_contexts", preloaded_contexts},
            {"fee_ready_contexts", fee_ready},
            {"subscribed_fee_ready_contexts", subscribed_fee_ready},
            {"artificial_delay_ms", 0},
            {"one_cycle_per_positive_episode", true},
            {"paired_fok_simulation", true},
            {"prefunded_complete_set_for_sell", true},
            {"prefunded_complete_set_shares", pure_arb_prefunded_complete_set_shares_},
            {"onchain_fixed_cost_modelled", false},
            {"hot_path_output_queue_drops", pure_arb_event_queue_drops_},
            {"reserve_arms", std::move(reserve_arms)},
            {"evaluations", pure_arb_evaluations_},
            {"fee_blocked_evaluations", pure_arb_fee_blocked_evaluations_},
            {"cycles_total", pure_arb_total_cycles_},
            {"paper_locked_pnl_pre_gas_total", pure_arb_total_pnl_},
            {"conservative_locked_pnl_after_reserve_total", pure_arb_conservative_total_pnl_},
            {"sizing_depth_authority", "LOCAL_DEEP_BOOK_POSITIVE_MARGINAL_EDGE"},
            {"deep_candidates_total", pure_arb_deep_candidates_},
            {"deep_evaluations_total", pure_arb_deep_evaluations_},
            {"deep_queue_drops_total", pure_arb_deep_queue_drops_},
            {"deep_snapshot_rejections_total", pure_arb_deep_snapshot_rejections_},
            {"reserve_per_share", pure_arb_reserve_per_share_},
            {"maximum_leg_skew_ms", pure_arb_max_leg_skew_ms_},
            {"maximum_receive_to_decision_ns", pure_arb_receive_to_decision_limit_ns_},
            {"funnel", json::object{
                {"book_updates", pure_arb_funnel_.book_updates},
                {"handles_ready", pure_arb_funnel_.handles_ready},
                {"market_window", pure_arb_funnel_.market_window},
                {"fee_ready", pure_arb_funnel_.fee_ready},
                {"epoch_synced", pure_arb_funnel_.epoch_synced},
                {"leg_skew_ready", pure_arb_funnel_.leg_skew_ready},
                {"lineage_ready", pure_arb_funnel_.lineage_ready},
                {"book_valid", pure_arb_funnel_.book_valid},
                {"fee_finite", pure_arb_funnel_.fee_finite},
                {"buy_raw_positive", pure_arb_funnel_.buy_raw_positive},
                {"buy_after_fee_positive", pure_arb_funnel_.buy_after_fee_positive},
                {"buy_after_reserve_positive", pure_arb_funnel_.buy_after_reserve_positive},
                {"buy_l10_executable", pure_arb_funnel_.buy_l10_executable},
                {"buy_fresh_decision", pure_arb_funnel_.buy_fresh_decision},
                {"sell_raw_positive", pure_arb_funnel_.sell_raw_positive},
                {"sell_after_fee_positive", pure_arb_funnel_.sell_after_fee_positive},
                {"sell_after_reserve_positive", pure_arb_funnel_.sell_after_reserve_positive},
                {"sell_l10_executable", pure_arb_funnel_.sell_l10_executable},
                {"sell_fresh_decision", pure_arb_funnel_.sell_fresh_decision},
                {"buy_cycles_recorded", pure_arb_funnel_.buy_cycles_recorded},
                {"sell_cycles_recorded", pure_arb_funnel_.sell_cycles_recorded},
                {"stale_decision_rejections", pure_arb_funnel_.stale_decision_rejections}}},
            {"latency_window_samples", static_cast<std::uint64_t>(pure_arb_receive_latency_.size())},
            {"receive_to_enqueue_ns", json::object{
                {"p50", pure_arb_receive_to_enqueue_latency_.quantile(0.50)},
                {"p90", pure_arb_receive_to_enqueue_latency_.quantile(0.90)},
                {"p99", pure_arb_receive_to_enqueue_latency_.quantile(0.99)},
                {"p99_9", pure_arb_receive_to_enqueue_latency_.quantile(0.999)},
                {"max", pure_arb_receive_to_enqueue_latency_.quantile(1.0)}}},
            {"queue_wait_ns", json::object{
                {"p50", pure_arb_queue_wait_latency_.quantile(0.50)},
                {"p90", pure_arb_queue_wait_latency_.quantile(0.90)},
                {"p99", pure_arb_queue_wait_latency_.quantile(0.99)},
                {"p99_9", pure_arb_queue_wait_latency_.quantile(0.999)},
                {"max", pure_arb_queue_wait_latency_.quantile(1.0)}}},
            {"receive_to_decision_ns", json::object{
                {"p50", pure_arb_receive_latency_.quantile(0.50)},
                {"p90", pure_arb_receive_latency_.quantile(0.90)},
                {"p99", pure_arb_receive_latency_.quantile(0.99)},
                {"p99_9", pure_arb_receive_latency_.quantile(0.999)},
                {"max", pure_arb_max_receive_to_decision_ns_}}},
            {"decision_compute_ns", json::object{
                {"p50", pure_arb_decision_latency_.quantile(0.50)},
                {"p90", pure_arb_decision_latency_.quantile(0.90)},
                {"p99", pure_arb_decision_latency_.quantile(0.99)},
                {"p99_9", pure_arb_decision_latency_.quantile(0.999)},
                {"max", pure_arb_max_decision_compute_ns_}}},
            {"last_receive_to_enqueue_ns", pure_arb_last_receive_to_enqueue_ns_},
            {"last_queue_wait_ns", pure_arb_last_queue_wait_ns_},
            {"last_decision_compute_ns", pure_arb_last_decision_compute_ns_},
            {"max_decision_compute_ns", pure_arb_max_decision_compute_ns_},
            {"last_receive_to_decision_ns", pure_arb_last_receive_to_decision_ns_},
            {"max_receive_to_decision_ns", pure_arb_max_receive_to_decision_ns_},
            {"contexts", std::move(contexts)},
        };
        atomic_write(pure_arb_status_path_, json::serialize(status) + "\n");
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
        if (!state_only_) {
            output_.flush();
            book_output_.flush();
            if (!output_ || !book_output_) {
                throw std::runtime_error("cannot flush canonical observer evidence");
            }
        }
        flush_pure_arb_events();
        if (pure_arb_output_.is_open()) pure_arb_output_.flush();
        if (compact_label_output_.is_open()) {
            compact_label_output_.flush();
            if (!compact_label_output_) throw std::runtime_error("cannot flush compact PM label tape");
        }
        write_status(true);
    }

    void drain() {
        TradeEvidence row;
        bool wrote = false;
        while (queue_->try_pop(row)) {
            if (!state_only_ && row.kind == MarketWsEventKind::Trade) {
                if (!disk_pressure()) write(row);
                else ++trade_events_suppressed_disk_pressure_;
            }
            evaluate_pure_arb(row);
            write_book(row);
            wrote = true;
        }
        PureArbDeepEvidence deep{};
        while (pure_arb_deep_queue_->try_pop(deep)) {
            evaluate_pure_arb_deep(deep);
        }
        const auto now_wall_ms = wall_ms();
        if (wrote && !state_only_ && now_wall_ms - last_evidence_flush_ms_ >= 25) {
            output_.flush();
            book_output_.flush();
            if (!output_ || !book_output_) {
                throw std::runtime_error("canonical observer evidence flush failed");
            }
            last_evidence_flush_ms_ = now_wall_ms;
        }
        if (!state_only_ && book_output_.tellp() >= 64 * 1024 * 1024) {
            book_output_.close();
            const auto sealed = book_path_.parent_path() / (session_id_ + ".segment-"
                + std::to_string(1'000'000 + book_segment_++) + ".jsonl");
            fs::rename(book_path_, sealed);
            book_output_.open(book_path_, std::ios::app);
            if (!book_output_) throw std::runtime_error("cannot rotate causal book evidence");
        }
        if (compact_label_output_.is_open() && now_wall_ms - last_compact_flush_ms_ >= 1000) {
            compact_label_output_.flush();
            if (!compact_label_output_) throw std::runtime_error("compact PM label tape flush failed");
            last_compact_flush_ms_ = now_wall_ms;
        }
        const std::int64_t publish_period_ms = state_only_ ? state_publish_ms_ : 50;
        if (now_wall_ms - last_book_publish_ms_ >= publish_period_ms) {
            for (std::size_t i=1; i<latest_books_.size(); ++i) {
                if (latest_books_[i].empty()) continue;
                atomic_write(output_dir_ / "book_features" / (by_handle_[i]->token_id + ".json"),
                             latest_books_[i]);
                latest_books_[i].clear();
            }
            last_book_publish_ms_ = wall_ms();
        }
    }

    void write_status(bool stopped = false, bool publish_flow = true) {
        flush_pure_arb_events();
        const auto feed = feed_ ? feed_->snapshot() : pm::fast::FeedSnapshot{};
        json::object root;
        root["schema"] = "polymarket_v7_maker_fillability_ws_status_v1";
        root["timestamp_ms"] = wall_ms();
        root["paper_only"] = true;
        root["authenticated_execution"] = false;
        root["real_order_submission"] = false;
        root["model_sha"] = model_sha_;
        root["observer_session_id"] = session_id_;
        root["book_events_observed"] = book_events_observed_;
        root["book_events_written"] = book_events_written_;
        root["subscribed_tokens"] = ids_.size();
        root["subscribed_markets"] = book_market_seen_.empty() ? 0 : book_market_seen_.size() - 1;
        const auto observed_tokens = static_cast<std::size_t>(
            std::count(book_token_seen_.begin(), book_token_seen_.end(), std::uint8_t{1}));
        const auto observed_markets = static_cast<std::size_t>(
            std::count(book_market_seen_.begin(), book_market_seen_.end(), std::uint8_t{1}));
        root["observed_tokens"] = observed_tokens;
        root["observed_markets"] = observed_markets;
        root["subscription_coverage_complete"] =
            observed_tokens == ids_.size()
            && observed_markets == (book_market_seen_.empty() ? 0 : book_market_seen_.size() - 1);
        root["disk_pressure"] = disk_pressure();
        root["book_event_tape_suppressed_by_disk_pressure"] = disk_pressure();
        root["book_events_suppressed_disk_pressure"] = book_events_suppressed_disk_pressure_;
        root["trade_events_suppressed_disk_pressure"] = trade_events_suppressed_disk_pressure_;
        root["state_only"] = state_only_;
        root["pure_arb_paper_enabled"] = pure_arb_paper_;
        root["state_publish_ms"] = state_publish_ms_;
        root["book_event_tape_enabled"] = !state_only_;
        root["compact_label_tape_enabled"] = compact_label_output_.is_open();
        root["compact_label_records"] = compact_label_records_;
        root["compact_label_records_suppressed_disk_pressure"] = compact_label_records_suppressed_disk_pressure_;
        root["compact_label_current_bytes"] = compact_label_current_bytes_;
        root["compact_label_segments"] = compact_label_segment_;
        root["compact_label_record_size"] = 80;
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
        root["lineage_invalid_book_snapshot"] = lineage_invalid_book_snapshot_.load(std::memory_order_relaxed);
        root["lineage_invalid_price_change"] = lineage_invalid_price_change_.load(std::memory_order_relaxed);
        root["lineage_invalid_tick_size_change"] = lineage_invalid_tick_size_change_.load(std::memory_order_relaxed);
        root["price_change_without_lineage"] = price_change_without_lineage_.load(std::memory_order_relaxed);
        root["lineage_recovery_requested"] = lineage_recovery_requested();
        root["root_lineage_recovery_requested"] = root_lineage_recovery_requested();
        root["lineage_recovery_requests"] = lineage_recovery_requests();
        root["lineage_recovered_without_restart"] = lineage_recovered_without_restart_.load(std::memory_order_relaxed);
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
            && decoder_failures_.load(std::memory_order_relaxed) == 0
            && !disk_pressure()
            && book_events_suppressed_disk_pressure_ == 0
            && trade_events_suppressed_disk_pressure_ == 0
            && compact_label_records_suppressed_disk_pressure_ == 0;
        const auto serialized_status = json::serialize(root) + "\n";
        atomic_write(status_path_, serialized_status);
        if (!compact_label_tape_dir_.empty()) {
            atomic_write(compact_label_tape_dir_ / (session_id_ + ".status.json"), serialized_status);
        }
        write_pure_arb_status(stopped);
        if (publish_flow) write_flow_snapshot(root["timestamp_ms"].as_int64());
    }

    void write_flow_snapshot(std::int64_t now_ms) {
        json::array rows;
        const auto epoch = connection_epoch_.load(std::memory_order_relaxed);
        for (std::size_t i = 1; i < flow_samples_.size(); ++i) {
            const auto* token = by_handle_[i];
            if (!token) continue;
            auto& samples = flow_samples_[i];
            while (!samples.empty() && (samples.front().connection_epoch != epoch
                    || samples.front().receive_wall_ms < now_ms - 600'000)) {
                samples.pop_front();
            }
            std::int64_t last_buy = 0, last_sell = 0, last_receive = 0;
            std::array<std::uint64_t, 4> buys{}, sells{};
            double buy_shares_120 = 0.0, buy_shares_600 = 0.0;
            double sell_shares_120 = 0.0, sell_shares_600 = 0.0;
            for (const auto& sample : samples) {
                if (sample.connection_epoch != epoch || sample.receive_wall_ms > now_ms + 5'000) continue;
                const auto age = now_ms - sample.receive_wall_ms;
                if (age < 0 || age > 600'000) continue;
                const std::array<std::int64_t, 4> windows{5'000, 30'000, 120'000, 600'000};
                auto& counts = sample.side == Side::Buy ? buys : sells;
                for (std::size_t w = 0; w < windows.size(); ++w) {
                    if (age <= windows[w]) ++counts[w];
                }
                if (sample.side == Side::Buy) {
                    last_buy = std::max(last_buy, sample.receive_wall_ms);
                    if (age <= 120'000) buy_shares_120 += sample.shares;
                    buy_shares_600 += sample.shares;
                } else if (sample.side == Side::Sell) {
                    last_sell = std::max(last_sell, sample.receive_wall_ms);
                    if (age <= 120'000) sell_shares_120 += sample.shares;
                    sell_shares_600 += sample.shares;
                }
                last_receive = std::max(last_receive, sample.receive_wall_ms);
            }
            rows.emplace_back(json::object{
                {"market_id", token->market_id}, {"event_id", token->event_id},
                {"token_id", token->token_id}, {"connection_epoch", epoch},
                {"buy_prints_5s", buys[0]}, {"buy_prints_30s", buys[1]},
                {"buy_prints_120s", buys[2]}, {"buy_prints_600s", buys[3]},
                {"sell_prints_5s", sells[0]}, {"sell_prints_30s", sells[1]},
                {"sell_prints_120s", sells[2]}, {"sell_prints_600s", sells[3]},
                {"buy_shares_120s", buy_shares_120}, {"buy_shares_600s", buy_shares_600},
                {"sell_shares_120s", sell_shares_120}, {"sell_shares_600s", sell_shares_600},
                {"last_buy_receive_ms", last_buy}, {"last_sell_receive_ms", last_sell},
                {"last_receive_ms", last_receive},
            });
        }
        json::object root{
            {"schema", "polymarket_v7_maker_fillability_flow_snapshot_v1"},
            {"timestamp_ms", now_ms}, {"model_sha", model_sha_},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false},
            {"execution_authority", "ZERO_AUTHORITY_RESEARCH_ONLY"},
            {"observer_session_id", session_id_}, {"connection_epoch", epoch},
            {"evidence_complete", dropped_.load(std::memory_order_relaxed) == 0
                && decoder_failures_.load(std::memory_order_relaxed) == 0
                && !disk_pressure()
                && book_events_suppressed_disk_pressure_ == 0
                && trade_events_suppressed_disk_pressure_ == 0
                && compact_label_records_suppressed_disk_pressure_ == 0},
            {"rows", std::move(rows)},
        };
        atomic_write(flow_path_, json::serialize(root) + "\n");
    }

private:
    static void put_u64(std::array<char, 80>& out, std::size_t offset, std::uint64_t value) noexcept {
        for (std::size_t i = 0; i < 8; ++i) out[offset + i] = static_cast<char>((value >> (8U * i)) & 0xffU);
    }
    static void put_i64(std::array<char, 80>& out, std::size_t offset, std::int64_t value) noexcept {
        put_u64(out, offset, static_cast<std::uint64_t>(value));
    }
    static void put_i32(std::array<char, 80>& out, std::size_t offset, std::int32_t value) noexcept {
        const auto raw = static_cast<std::uint32_t>(value);
        for (std::size_t i = 0; i < 4; ++i) out[offset + i] = static_cast<char>((raw >> (8U * i)) & 0xffU);
    }
    void initialize_compact_label_tape() {
        fs::create_directories(compact_label_tape_dir_);
        compact_label_path_ = compact_label_tape_dir_ / (session_id_ + ".current.bin");
        compact_label_output_.open(compact_label_path_, std::ios::binary | std::ios::trunc);
        if (!compact_label_output_) throw std::runtime_error("cannot open compact PM label tape");
        json::array token_rows;
        for (const auto& token : tokens_) {
            token_rows.emplace_back(json::object{
                {"instrument_handle", token.instrument_handle}, {"market_handle", token.market_handle},
                {"market_id", token.market_id}, {"event_id", token.event_id}, {"token_id", token.token_id},
                {"outcome", token.is_yes != 0 ? "YES" : "NO"}, {"initial_tick_e4", token.tick_size_e4}});
        }
        json::object manifest{
            {"schema", "polymarket_v7_compact_pm_label_tape_manifest_v2"}, {"version", 2},
            {"record_schema", "polymarket_v7_compact_pm_label_record_v2"}, {"record_size", 80},
            {"byte_order", "little_endian"}, {"model_sha", model_sha_}, {"observer_session_id", session_id_},
            {"paper_only", true}, {"authenticated_execution", false}, {"real_order_submission", false},
            {"execution_authority", "ZERO_AUTHORITY_RESEARCH_ONLY"}, {"selection_only", true},
            {"fields", json::array{"observer_sequence:u64", "instrument_handle:u64", "state_version:u64",
                "connection_epoch:u64", "receive_wall_ms:i64", "receive_monotonic_ns:i64",
                "best_bid_e4:i32", "best_ask_e4:i32", "tick_size_e4:i32",
                "bid_depth_l1_microunits:i64", "ask_depth_l1_microunits:i64", "valid:u8",
                "lineage_continuous:u8", "event_kind:u8", "reserved:u8"}},
            {"tokens", std::move(token_rows)}};
        atomic_write(compact_label_tape_dir_ / (session_id_ + ".manifest.json"), json::serialize(manifest) + "\n");
    }
    void rotate_compact_label_tape() {
        if (!compact_label_output_.is_open()) return;
        compact_label_output_.flush(); compact_label_output_.close();
        const auto sealed = compact_label_tape_dir_ / (session_id_ + ".segment-"
            + std::to_string(1'000'000 + compact_label_segment_) + ".bin");
        ++compact_label_segment_;
        fs::rename(compact_label_path_, sealed);
        compact_label_output_.open(compact_label_path_, std::ios::binary | std::ios::trunc);
        if (!compact_label_output_) throw std::runtime_error("cannot rotate compact PM label tape");
        compact_label_current_bytes_ = 0;
    }
    void append_compact_label(const TradeEvidence& row, std::uint64_t observer_sequence, bool valid) {
        if (!compact_label_output_.is_open()) return;
        if (disk_pressure()) {
            ++compact_label_records_suppressed_disk_pressure_;
            return;
        }
        if (compact_label_current_bytes_ + 80 > 256ULL * 1024ULL * 1024ULL) rotate_compact_label_tape();
        std::array<char, 80> payload{};
        put_u64(payload, 0, observer_sequence); put_u64(payload, 8, row.instrument_handle);
        put_u64(payload, 16, row.state_version); put_u64(payload, 24, row.connection_epoch);
        put_i64(payload, 32, row.receive_wall_ms); put_i64(payload, 40, row.receive_monotonic_ns);
        put_i32(payload, 48, row.book.best_bid_e4); put_i32(payload, 52, row.book.best_ask_e4);
        put_i32(payload, 56, row.book.tick_size_e4);
        put_i64(payload, 60, row.book.bid_depth.l1_microunits); put_i64(payload, 68, row.book.ask_depth.l1_microunits);
        payload[76] = valid ? 1 : 0; payload[77] = row.book.lineage_continuous != 0 ? 1 : 0;
        payload[78] = static_cast<char>(static_cast<std::uint8_t>(row.kind)); payload[79] = 0;
        compact_label_output_.write(payload.data(), static_cast<std::streamsize>(payload.size()));
        if (!compact_label_output_) throw std::runtime_error("cannot write compact PM label tape");
        ++compact_label_records_; compact_label_current_bytes_ += payload.size();
    }

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
            {"microstructure_shadow_delta_250ms",
             f.microstructure_shadow_valid ? json::value(f.microstructure_shadow_delta_250ms)
                                            : json::value(nullptr)},
            {"microstructure_shadow_valid", f.microstructure_shadow_valid != 0},
            {"microstructure_shadow_model_hash", pm::v7::maker::kMicrostructureShadowModelHash},
            {"microstructure_shadow_execution_authority", "ZERO_AUTHORITY_RESEARCH_ONLY"},
        };
        const bool valid = row.book.valid && row.book.lineage_continuous
            && dropped_.load(std::memory_order_relaxed) == 0
            && decoder_failures_.load(std::memory_order_relaxed) == 0;
        // Coverage means that the subscribed token delivered a causally
        // continuous book. A legitimate one-sided near-settlement book is
        // observed but not executable: row.valid remains false until both
        // sides are present and uncrossed.
        if (row.book.lineage_continuous) {
            if (row.instrument_handle < book_token_seen_.size()) {
                book_token_seen_[row.instrument_handle] = 1;
            }
            if (token->market_handle < book_market_seen_.size()) {
                book_market_seen_[token->market_handle] = 1;
            }
        }
        const auto observer_sequence = ++book_events_observed_;
        append_compact_label(row, observer_sequence, valid);
        // Persist the causal L10 ladders already present in BookHotSnapshot.
        // This lets the PAPER execution shadow revalidate a true multi-level
        // FOK without REST lookups or best-price/L1 approximations.
        json::array bid_levels_l10;
        json::array ask_levels_l10;
        bid_levels_l10.reserve(row.book.bid_level_count);
        ask_levels_l10.reserve(row.book.ask_level_count);
        for (std::size_t level = 0; level < row.book.bid_level_count; ++level) {
            const auto& item = row.book.bid_levels[level];
            if (item.price_e4 <= 0 || item.quantity_microunits <= 0) continue;
            bid_levels_l10.emplace_back(json::object{
                {"price", e4_price(item.price_e4)},
                {"size", micro_shares(item.quantity_microunits)},
            });
        }
        for (std::size_t level = 0; level < row.book.ask_level_count; ++level) {
            const auto& item = row.book.ask_levels[level];
            if (item.price_e4 <= 0 || item.quantity_microunits <= 0) continue;
            ask_levels_l10.emplace_back(json::object{
                {"price", e4_price(item.price_e4)},
                {"size", micro_shares(item.quantity_microunits)},
            });
        }
        json::object value{
            {"schema", "polymarket_v7_causal_book_observation_v1"},
            {"model_sha", model_sha_}, {"paper_only", true},
            {"authenticated_execution", false}, {"real_order_submission", false},
            {"execution_authority", "ZERO_AUTHORITY_RESEARCH_ONLY"},
            {"observer_session_id", session_id_}, {"connection_epoch", row.connection_epoch},
            {"observer_sequence", observer_sequence}, {"market_id", token->market_id},
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
            {"bid_levels_l10", std::move(bid_levels_l10)},
            {"ask_levels_l10", std::move(ask_levels_l10)},
            {"causal_depth_levels", static_cast<std::uint64_t>(pm::v7::kHotDepthLevels)},
            {"placement_features", std::move(features)},
            {"feature_semantics", "CANONICAL_MAKER_LANE_OBSERVED_FLOW_V1"},
            {"cancel_intensity_semantics", "L5_CONTRACTION_MINUS_OBSERVED_TRADES_NORMALIZED_EW_PROXY"},
        };
        value["public_trade"] = nullptr;
        if (row.kind == MarketWsEventKind::Trade) {
            value["public_trade"] = json::object{
                {"aggressor_side", side_name(row.aggressor_side)},
                {"price", e4_price(row.price_e4)},
                {"size", micro_shares(row.quantity_microunits)},
                {"exchange_event_ns", row.exchange_event_ns}};
        }
        const auto serialized = json::serialize(value) + "\n";
        if (!state_only_ && !disk_pressure()) {
            book_output_ << serialized;
            ++book_events_written_;
        } else if (!state_only_) {
            ++book_events_suppressed_disk_pressure_;
        }
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
        event["observer_session_id"] = session_id_;
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
        auto& flow = flow_samples_[row.instrument_handle];
        flow.push_back(FlowSample{row.receive_wall_ms, micro_shares(row.quantity_microunits),
                                  row.aggressor_side, row.connection_epoch});
        while (!flow.empty() && flow.front().receive_wall_ms < row.receive_wall_ms - 600'000) {
            flow.pop_front();
        }
        ++events_written_;
        last_exchange_ns_ = std::max(last_exchange_ns_, row.exchange_event_ns);
        last_receive_wall_ms_ = std::max(last_receive_wall_ms_, row.receive_wall_ms);
    }

    std::vector<SelectedToken> tokens_;
    std::string ws_url_;
    fs::path output_dir_;
    std::string model_sha_;
    bool state_only_ = false;
    std::int64_t state_publish_ms_ = 100;
    bool recover_missing_lineage_ = false;
    fs::path compact_label_tape_dir_;
    fs::path compact_label_path_;
    bool pure_arb_paper_ = false;
    double pure_arb_reserve_per_share_ = 0.0005;
    std::int64_t pure_arb_max_leg_skew_ms_ = 100;
    std::int64_t pure_arb_receive_to_decision_limit_ns_ = 50'000'000LL;
    double pure_arb_prefunded_complete_set_shares_ = 1000.0;
    fs::path pure_arb_status_path_;
    fs::path pure_arb_trades_path_;
    std::ofstream pure_arb_output_;
    std::unique_ptr<pm::v7::SpscRing<PureArbQueuedEvent, kPureArbOutputCapacity>>
        pure_arb_event_queue_ =
            std::make_unique<pm::v7::SpscRing<PureArbQueuedEvent, kPureArbOutputCapacity>>();
    std::uint64_t pure_arb_event_queue_drops_ = 0;
    std::unique_ptr<pm::v7::SpscRing<PureArbDeepEvidence, kPureArbDeepCapacity>>
        pure_arb_deep_queue_ =
            std::make_unique<pm::v7::SpscRing<PureArbDeepEvidence, kPureArbDeepCapacity>>();
    std::uint64_t pure_arb_deep_candidates_ = 0;
    std::uint64_t pure_arb_deep_queue_drops_ = 0;
    std::uint64_t pure_arb_deep_snapshot_rejections_ = 0;
    std::uint64_t pure_arb_deep_evaluations_ = 0;
    std::vector<PureArbPairBinding> pure_arb_pair_by_handle_;
    std::vector<std::uint8_t> pure_arb_deep_trigger_active_;
    std::vector<pm::v7::BookHotSnapshot> pure_arb_latest_books_;
    std::vector<std::uint64_t> pure_arb_book_epochs_;
    std::vector<std::int64_t> pure_arb_book_receive_wall_ms_;
    std::vector<PureArbMarketState> pure_arb_markets_;
    std::uint64_t pure_arb_evaluations_ = 0;
    std::uint64_t pure_arb_fee_blocked_evaluations_ = 0;
    std::uint64_t pure_arb_total_cycles_ = 0;
    double pure_arb_total_pnl_ = 0.0;
    double pure_arb_conservative_total_pnl_ = 0.0;
    PureArbFunnelState pure_arb_funnel_{};
    PureArbLatencyWindow pure_arb_receive_to_enqueue_latency_{};
    PureArbLatencyWindow pure_arb_queue_wait_latency_{};
    PureArbLatencyWindow pure_arb_receive_latency_{};
    PureArbLatencyWindow pure_arb_decision_latency_{};
    std::int64_t pure_arb_last_receive_to_enqueue_ns_ = 0;
    std::int64_t pure_arb_last_queue_wait_ns_ = 0;
    std::int64_t pure_arb_last_decision_compute_ns_ = 0;
    std::int64_t pure_arb_max_decision_compute_ns_ = 0;
    std::int64_t pure_arb_last_receive_to_decision_ns_ = 0;
    std::int64_t pure_arb_max_receive_to_decision_ns_ = 0;
    std::ofstream compact_label_output_;
    std::uint64_t compact_label_records_ = 0;
    std::uint64_t compact_label_records_suppressed_disk_pressure_ = 0;
    std::uint64_t compact_label_current_bytes_ = 0;
    std::uint64_t compact_label_segment_ = 0;
    std::int64_t last_compact_flush_ms_ = 0;
    std::int64_t last_evidence_flush_ms_ = 0;
    std::vector<std::string> ids_;
    std::vector<const SelectedToken*> by_handle_;
    std::vector<std::unique_ptr<pm::v7::maker::MakerInstrumentLane>> lanes_;
    std::vector<std::int64_t> feature_start_ns_;
    std::vector<std::string> latest_books_;
    std::vector<std::deque<FlowSample>> flow_samples_;
    std::vector<std::uint8_t> book_token_seen_;
    std::vector<std::uint8_t> book_market_seen_;
    pm::v7::maker::MakerModelSnapshot feature_model_;
    std::string session_id_;
    std::ofstream book_output_;
    fs::path book_path_;
    std::uint64_t book_segment_ = 0;
    std::uint64_t book_events_observed_ = 0;
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
    fs::path flow_path_;
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
    std::atomic<std::uint64_t> lineage_invalid_book_snapshot_{0};
    std::atomic<std::uint64_t> lineage_invalid_price_change_{0};
    std::atomic<std::uint64_t> lineage_invalid_tick_size_change_{0};
    std::atomic<std::uint64_t> price_change_without_lineage_{0};
    std::atomic<std::uint64_t> lineage_recovery_requests_{0};
    std::atomic<std::uint64_t> lineage_recovered_without_restart_{0};
    std::atomic<bool> lineage_recovery_requested_{false};
    std::atomic<bool> root_lineage_recovery_requested_{false};
    std::atomic<std::uint64_t> unknown_asset_{0};
    std::atomic<std::uint64_t> reconnects_{0};
    std::atomic<bool> disk_pressure_{false};
    std::uint64_t book_events_suppressed_disk_pressure_ = 0;
    std::uint64_t trade_events_suppressed_disk_pressure_ = 0;
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
            const auto fair_pairs = options.selection_only
                ? std::vector<std::pair<std::string, std::pair<std::string, std::string>>>{}
                : fair_observation_pairs(options);
            auto tokens = build_tokens(options, config);
            if (options.selection_only) apply_selection_windows(tokens, options.selection);
            const auto selected_pairs = options.fair_only
                ? std::vector<std::pair<std::string, std::pair<std::string, std::string>>>{}
                : load_selected_pairs(options.selection, options.selection_only, options.model_sha);
            ExactWsObserver observer(
                std::move(tokens), options.ws_url, options.output_dir, options.model_sha,
                options.state_only, options.state_publish_ms, options.selection_only,
                options.compact_label_tape_dir, options.pure_arb_paper,
                options.pure_arb_reserve_per_share,
                options.pure_arb_max_leg_skew_ms,
                options.pure_arb_max_receive_to_decision_ns,
                options.pure_arb_prefunded_complete_set_shares);
            const fs::path disk_pressure_marker = fs::path(options.run_root) / "control" / "DISK_PRESSURE";
            const auto local_disk_pressure = [&]() {
                if (fs::exists(disk_pressure_marker)) return true;
                if (options.disk_pressure_min_free_bytes == 0) return false;
                std::error_code error;
                const auto space = fs::space(fs::path(options.output_dir), error);
                return error || space.available <= options.disk_pressure_min_free_bytes;
            };
            observer.set_disk_pressure(local_disk_pressure());
            observer.start();
            std::int64_t last_status_ms = 0;
            std::int64_t last_membership_check_ms = 0;
            std::int64_t last_flow_status_ms = 0;
            // The causal labeler's grace is 75ms. A 1Hz watermark silently
            // censors valid +250ms labels even on an uninterrupted book stream.
            const std::int64_t status_period_ms = options.fair_only ? 25 : 1000;
            bool reload = false;
            while (!g_stop.load(std::memory_order_relaxed) && !reload) {
                observer.drain();
                const auto now = wall_ms();
                if (now - last_status_ms >= status_period_ms) {
                    const bool publish_flow = now - last_flow_status_ms >= 1000;
                    observer.write_status(false, publish_flow);
                    last_status_ms = now;
                    if (publish_flow) last_flow_status_ms = now;
                }
                if (now - last_membership_check_ms >= 1000) {
                    last_membership_check_ms = now;
                    observer.set_disk_pressure(local_disk_pressure());
                    if (options.pure_arb_paper) observer.refresh_pure_arb_metadata(options.selection);
                    // Price/feature refreshes do not change the subscription.
                    // Restarting on every mtime update erased queue evidence.
                    reload = !options.selection_only
                        && fair_observation_pairs(options) != fair_pairs;
                    // Token-local lineage gaps are isolated by the decoder and
                    // remain explicitly non-continuous until a later full snapshot
                    // heals that token. Restarting all 60 subscriptions for one
                    // expiring/gapped token creates a coverage death spiral.
                    // Only irrecoverable frame/arena/output corruption is global.
                    if ((options.fair_only || options.selection_only)
                            && observer.root_lineage_recovery_requested()) {
                        reload = true;
                    }
                    if (!options.fair_only) {
                        const auto latest_pairs = load_selected_pairs(
                            options.selection, options.selection_only, options.model_sha);
                        if (latest_pairs != selected_pairs) {
                            const bool defer_rollover = options.pure_arb_paper
                                && defer_pure_arb_membership_reload(
                                    options.selection, selected_pairs, now);
                            reload = reload || !defer_rollover;
                        }
                    }
                }
                if (options.pure_arb_paper) {
                    std::this_thread::sleep_for(std::chrono::microseconds(50));
                } else {
                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                }
            }
            observer.stop();
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_maker_fillability_observer: " << error.what() << '\n';
        return 2;
    }
}
