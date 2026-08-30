#include "pm/api.hpp"
#include "pm/config.hpp"
#include "pm/fast_ws.hpp"
#include "pm/v7_maker_execution_policy.hpp"
#include "pm/v7_maker_lane.hpp"
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
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;
using pm::v7::BookHotSnapshot;
using pm::v7::CapitalLimits;
using pm::v7::ExecutionPlan;
using pm::v7::ExecutionPolicyId;
using pm::v7::IntentType;
using pm::v7::MarketWsEvent;
using pm::v7::MarketWsEventKind;
using pm::v7::Side;
using pm::v7::SleeveCapitalAccount;
using pm::v7::StrategyIntent;
using pm::v7::maker::Action;
using pm::v7::maker::InventorySnapshot;
using pm::v7::maker::MakerDecision;
using pm::v7::maker::MakerExecutionReason;
using pm::v7::maker::MakerInstrumentLane;
using pm::v7::maker::MakerLaneContext;
using pm::v7::maker::MakerModelSnapshot;
using pm::v7::maker::MakerModelStore;
using pm::v7::maker::MakerPaperExecutionPolicy;
using pm::v7::maker::PaperMakerEvent;
using pm::v7::maker::PaperMakerEventKind;
using pm::v7::maker::PaperMakerInventory;
using pm::v7::maker::PaperMakerPolicy;
using pm::v7::maker::PaperMakerResult;
using pm::v7::maker::QuoteSnapshot;

constexpr std::size_t kMarketsPerShard = 8;
constexpr std::size_t kWsOutputCapacity = 512;
constexpr std::size_t kTelemetryCapacity = 8192;
constexpr std::size_t kExecutionCriticalCapacity = 512;
constexpr std::size_t kExecutionNormalCapacity = 1024;
constexpr std::size_t kExecutionSnapshotCapacity = 1024;
constexpr std::size_t kExecutionTelemetryCapacity = 16384;
constexpr std::size_t kExecutionBacklogSoftLimit = 768;
constexpr std::size_t kDecisionReasonCount = 18;
constexpr double kMicrounitsPerShare = 1'000'000.0;
constexpr double kPriceScaleE4 = 10'000.0;
constexpr std::string_view kStrategy = "MICRO_MAKER_PRO";

std::atomic<bool> g_stop{false};

void signal_handler(int) noexcept { g_stop.store(true, std::memory_order_relaxed); }

[[nodiscard]] std::int64_t wall_ms() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

[[nodiscard]] std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

[[nodiscard]] pm::fast::FeedReceiveStamp receive_now() noexcept {
    pm::fast::FeedReceiveStamp out;
    out.wall_ms = wall_ms();
    out.monotonic_ns = monotonic_ns();
    return out;
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

[[nodiscard]] const json::object* child_object(const json::object& object, std::string_view key) noexcept {
    const auto* value = find_value(object, key);
    return value != nullptr && value->is_object() ? &value->as_object() : nullptr;
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
    if (value->is_string()) {
        char* end = nullptr;
        const std::string raw(value->as_string());
        const double parsed = std::strtod(raw.c_str(), &end);
        if (end != raw.c_str() && *end == '\0' && std::isfinite(parsed)) return parsed;
    }
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

[[nodiscard]] double object_number(const json::object* object, std::string_view key,
                                   double fallback) noexcept {
    return object == nullptr ? fallback : number(find_value(*object, key), fallback);
}

[[nodiscard]] std::int64_t object_integer(const json::object* object, std::string_view key,
                                          std::int64_t fallback) noexcept {
    return object == nullptr ? fallback : integer(find_value(*object, key), fallback);
}

[[nodiscard]] std::uint64_t fnv1a(std::string_view value) noexcept {
    std::uint64_t hash = 1469598103934665603ULL;
    for (const unsigned char ch : value) {
        hash ^= static_cast<std::uint64_t>(ch);
        hash *= 1099511628211ULL;
    }
    return hash == 0 ? 1 : hash;
}

[[nodiscard]] std::uint64_t mix64(std::uint64_t x) noexcept {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27U)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31U);
}

[[nodiscard]] std::string hex64(std::uint64_t value) {
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << value;
    return out.str();
}

[[nodiscard]] double e4_price(std::int32_t value) noexcept {
    return static_cast<double>(value) / kPriceScaleE4;
}

[[nodiscard]] double tick_price(std::int64_t tick, std::int32_t tick_e4) noexcept {
    return static_cast<double>(tick) * static_cast<double>(tick_e4) / kPriceScaleE4;
}

[[nodiscard]] double micro_shares(std::int64_t value) noexcept {
    return static_cast<double>(std::max<std::int64_t>(0, value)) / kMicrounitsPerShare;
}

[[nodiscard]] std::int64_t microdollars(double dollars) noexcept {
    if (!std::isfinite(dollars) || dollars <= 0.0) return 0;
    const long double raw = static_cast<long double>(dollars) * 1'000'000.0L;
    if (raw > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) return 0;
    return static_cast<std::int64_t>(std::floor(raw));
}

[[nodiscard]] const char* side_name(Side side) noexcept {
    return side == Side::Buy ? "BUY" : side == Side::Sell ? "SELL" : "";
}

[[nodiscard]] const char* action_name(Action action) noexcept {
    switch (action) {
        case Action::Join: return "JOIN";
        case Action::Improve1: return "IMPROVE1";
        case Action::Fade1: return "FADE1";
        case Action::Fade2: return "FADE2";
        case Action::OneSided: return "ONE_SIDED";
        case Action::Withdraw: return "WITHDRAW";
    }
    return "UNKNOWN";
}

[[nodiscard]] const char* decision_reason_name(
    pm::v7::maker::DecisionReason reason) noexcept {
    using Reason = pm::v7::maker::DecisionReason;
    switch (reason) {
        case Reason::Quote: return "QUOTE";
        case Reason::NoEconomicQuote: return "NO_ECONOMIC_QUOTE";
        case Reason::Toxic: return "TOXIC";
        case Reason::InventoryLimit: return "INVENTORY_LIMIT";
        case Reason::GlobalKill: return "GLOBAL_KILL";
        case Reason::StrategyKill: return "STRATEGY_KILL";
        case Reason::MarketKill: return "MARKET_KILL";
        case Reason::FeedUnhealthy: return "FEED_UNHEALTHY";
        case Reason::StaleState: return "STALE_STATE";
        case Reason::InvalidBook: return "INVALID_BOOK";
        case Reason::InvalidModel: return "INVALID_MODEL";
        case Reason::QuoteLifetimeHold: return "QUOTE_LIFETIME_HOLD";
        case Reason::NoChange: return "NO_CHANGE";
        case Reason::ExplorationQuote: return "EXPLORATION_QUOTE";
        case Reason::ExplorationHold: return "EXPLORATION_HOLD";
        case Reason::ExplorationExpired: return "EXPLORATION_EXPIRED";
        case Reason::NewRiskFrozen: return "NEW_RISK_FROZEN";
    }
    return "UNKNOWN";
}

[[nodiscard]] bool safety_preemption(
    pm::v7::maker::DecisionReason reason) noexcept {
    using Reason = pm::v7::maker::DecisionReason;
    return reason == Reason::Toxic || reason == Reason::InventoryLimit
        || reason == Reason::GlobalKill || reason == Reason::StrategyKill
        || reason == Reason::MarketKill || reason == Reason::FeedUnhealthy
        || reason == Reason::StaleState || reason == Reason::InvalidBook
        || reason == Reason::InvalidModel || reason == Reason::NewRiskFrozen;
}

[[nodiscard]] const char* order_state_name(pm::v7::OrderState state) noexcept {
    using pm::v7::OrderState;
    switch (state) {
        case OrderState::Intent: return "INTENT";
        case OrderState::SendPending: return "SEND_PENDING";
        case OrderState::AckPending: return "ACK_PENDING";
        case OrderState::Live: return "LIVE";
        case OrderState::Partial: return "PARTIAL";
        case OrderState::CancelRequested: return "CANCEL_REQUESTED";
        case OrderState::CancelPending: return "CANCEL_PENDING";
        case OrderState::Filled: return "FILLED";
        case OrderState::Cancelled: return "CANCELLED";
        case OrderState::Rejected: return "REJECTED";
        case OrderState::Expired: return "EXPIRED";
        case OrderState::Unknown: return "UNKNOWN";
        case OrderState::Reconciling: return "RECONCILING";
        case OrderState::Lost: return "LOST";
    }
    return "UNKNOWN";
}

struct Options {
    std::string config = "config/paper_v7.json";
    std::string maker_policy = "config/v7_professional_market_maker.json";
    std::string selection;
    std::string model;
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
        else if (arg == "--maker-policy") options.maker_policy = next();
        else if (arg == "--selection") options.selection = next();
        else if (arg == "--model") options.model = next();
        else if (arg == "--run-root") options.run_root = next();
        else if (arg == "--model-sha") options.model_sha = next();
        else if (arg == "--ws-url") options.ws_url = next();
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (options.selection.empty()) options.selection = options.run_root + "/micro_maker/reward_selection.json";
    if (options.model.empty()) options.model = options.run_root + "/micro_maker/execution_model.json";
    if (!exact_sha(options.model_sha)) throw std::runtime_error("--model-sha must be exact 40-hex SHA");
    return options;
}

struct SelectedMarket {
    std::string condition_id;
    std::string market_id;
    std::string event_id;
    std::string yes_token;
    std::string no_token;
    double reward_intensity = 0.0;
    double rewards_min_size = 0.0;
};

[[nodiscard]] std::vector<SelectedMarket> load_selection(const fs::path& path) {
    const auto root = read_json(path);
    if (!root.is_object()) throw std::runtime_error("maker selection must be object");
    const auto& object = root.as_object();
    if (const auto* safety = find_value(object, "paper_only"); safety != nullptr && !boolean(safety, false)) {
        throw std::runtime_error("maker selection is not PAPER-only");
    }
    if (const auto* auth = find_value(object, "authenticated_execution"); auth != nullptr && boolean(auth, true)) {
        throw std::runtime_error("maker selection enables authenticated execution");
    }
    const auto* capacity_value = find_value(object, "resource_capacity_markets");
    const auto max_markets = static_cast<std::size_t>(std::max<std::int64_t>(
        0, integer(capacity_value, 0)));
    if (max_markets == 0 || max_markets > pm::v7::maker::kMaxMakerExecutionMarkets) {
        throw std::runtime_error("maker selection resource capacity is invalid");
    }
    const auto* raw = find_value(object, "markets");
    if (raw == nullptr || !raw->is_array()) throw std::runtime_error("maker selection missing markets");
    std::vector<SelectedMarket> output;
    output.reserve(std::min<std::size_t>(max_markets, raw->as_array().size()));
    for (const auto& item : raw->as_array()) {
        if (!item.is_object() || output.size() >= max_markets) break;
        const auto& row = item.as_object();
        SelectedMarket market;
        market.condition_id = text(find_value(row, "condition_id"));
        market.market_id = text(find_value(row, "market_id"));
        market.event_id = text(find_value(row, "event_id"));
        market.yes_token = text(find_value(row, "yes_token"));
        market.no_token = text(find_value(row, "no_token"));
        market.reward_intensity = std::max(0.0, number(find_value(row, "reward_intensity"), 0.0));
        market.rewards_min_size = std::max(0.0, number(find_value(row, "rewards_min_size"), 0.0));
        if (market.market_id.empty() || market.condition_id.empty() || market.yes_token.empty()
            || market.no_token.empty() || market.yes_token == market.no_token) {
            continue;
        }
        output.push_back(std::move(market));
    }
    if (output.empty()) throw std::runtime_error("maker selection has no valid markets");
    return output;
}

[[nodiscard]] std::int32_t tick_e4(double tick) noexcept {
    if (!std::isfinite(tick) || tick <= 0.0 || tick >= 1.0) return 0;
    const auto value = static_cast<std::int64_t>(std::llround(tick * kPriceScaleE4));
    if (value <= 0 || value > 10'000 || 10'000 % value != 0) return 0;
    return static_cast<std::int32_t>(value);
}

MakerModelSnapshot load_model_snapshot(const fs::path& policy_path,
                                       const fs::path& model_path,
                                       std::string_view expected_sha) {
    MakerModelSnapshot model;
    const auto policy_root = read_json(policy_path);
    if (!policy_root.is_object()) throw std::runtime_error("maker policy must be object");
    const auto& policy = policy_root.as_object();
    if (!boolean(find_value(policy, "paper_only"), false)
        || boolean(find_value(policy, "authenticated_execution"), true)
        || boolean(find_value(policy, "real_order_submission"), true)) {
        throw std::runtime_error("unsafe maker policy");
    }
    const auto* fair = child_object(policy, "fair_value");
    model.mid_weight = object_number(fair, "mid_weight", model.mid_weight);
    model.microprice_weight = object_number(fair, "microprice_weight", model.microprice_weight);
    model.flow_weight = object_number(fair, "flow_weight", model.flow_weight);
    model.related_weight = object_number(fair, "related_market_weight", model.related_weight);

    const auto* quoting = child_object(policy, "quoting");
    model.inventory_skew_ticks = object_number(quoting, "inventory_skew_strength", model.inventory_skew_ticks);
    model.toxicity_withdraw_threshold = object_number(
        quoting, "toxicity_withdraw_threshold", model.toxicity_withdraw_threshold);
    model.min_robust_ev_per_share = object_number(
        quoting, "minimum_exploit_ev_per_dollar", model.min_robust_ev_per_share);
    model.min_quote_lifetime_ns = object_integer(
        quoting, "min_quote_lifetime_ms", model.min_quote_lifetime_ns / 1'000'000LL) * 1'000'000LL;
    const auto* inventory = child_object(policy, "inventory");
    model.one_sided_inventory_fraction = std::max(0.0, object_number(
        inventory, "soft_directional_inventory_fraction", model.one_sided_inventory_fraction));

    const auto* market_selection = child_object(policy, "market_selection");
    const auto* exploration = child_object(policy, "exploration");
    const double configured_exploration_quote_fraction = std::clamp(object_number(
        exploration, "max_quote_notional_fraction", 0.0), 0.0, 0.01);
    const double exploration_market_fraction = std::clamp(object_number(
        exploration, "max_market_fraction", 0.0), 0.0, 1.0);
    const double exploration_capital_fraction = std::clamp(object_number(
        exploration, "max_capital_fraction", 0.0), 0.0, 1.0);
    // A binary market can expose two exploratory BUYs (YES and NO). Bound each
    // quote to half the per-market allowance, then bound the eligible market
    // handles so the worst-case aggregate remains inside the sleeve allowance.
    // This mirrors the constructor-side policy enrichment but uses the explicit
    // runtime policy path rather than depending on process environment.
    const double exploration_quote_fraction = std::min(
        configured_exploration_quote_fraction,
        exploration_market_fraction > 0.0 ? 0.5 * exploration_market_fraction : 0.0);
    const std::uint32_t configured_market_capacity = static_cast<std::uint32_t>(
        std::max<std::int64_t>(0, object_integer(market_selection, "max_active_markets", 0)));
    std::uint32_t exploration_market_cap = 0;
    if (exploration_quote_fraction > 0.0 && exploration_capital_fraction > 0.0) {
        exploration_market_cap = static_cast<std::uint32_t>(std::min<double>(
            configured_market_capacity,
            std::floor(exploration_capital_fraction
                       / (2.0 * exploration_quote_fraction) + 1e-12)));
    }
    model.exploration_epsilon = std::clamp(object_number(
        exploration, "epsilon", 0.0), 0.0, 1.0);
    const double configured_exploration_confidence_z = object_number(
        exploration, "confidence_z", -1.0);
    const bool valid_exploration_confidence = std::isfinite(configured_exploration_confidence_z)
        && configured_exploration_confidence_z >= 0.0
        && configured_exploration_confidence_z <= std::max(0.0, model.robust_ev_z);
    model.exploration_confidence_z = valid_exploration_confidence
        ? configured_exploration_confidence_z : 0.0;
    model.exploration_quote_notional_fraction = exploration_quote_fraction;
    model.exploration_max_active_markets = configured_market_capacity;
    model.exploration_concurrent_market_cap = exploration_market_cap;
    model.exploration_min_rest_ns = std::max<std::int64_t>(0, object_integer(
        exploration, "minimum_rest_ms", 0)) * 1'000'000LL;
    model.exploration_max_rest_ns = std::max(model.exploration_min_rest_ns,
        std::max<std::int64_t>(0, object_integer(exploration, "maximum_rest_ms", 0))
            * 1'000'000LL);
    model.exploration_enabled = static_cast<std::uint8_t>(exploration != nullptr
        && boolean(find_value(*exploration, "enabled"), false)
        && model.exploration_epsilon > 0.0
        && valid_exploration_confidence
        && exploration_quote_fraction > 0.0
        && exploration_market_cap > 0);

    const auto* latency = child_object(policy, "latency");
    model.max_related_snapshot_age_ns = object_integer(
        latency, "max_cross_book_skew_ms", model.max_related_snapshot_age_ns / 1'000'000LL) * 1'000'000LL;

    const auto* execution = child_object(policy, "execution_model");
    const double cold_fill = std::clamp(object_number(execution, "cold_start_fill_prior", 0.02),
                                        1e-6, 1.0 - 1e-6);
    const double cold_adverse = std::max(0.0, object_number(
        execution, "cold_start_adverse_markout_per_share", 0.002));
    model.fill_coefficients.fill(0.0);
    model.fill_coefficients[0] = std::log(cold_fill / (1.0 - cold_fill));
    model.markout_coefficients.fill(0.0);
    model.markout_coefficients[0] = cold_adverse;
    model.base_quote_shares = 5.0;
    model.policy_version = static_cast<std::uint64_t>(std::max<std::int64_t>(
        1, integer(find_value(policy, "schema_version"), 1)));

    if (fs::exists(model_path)) {
        try {
            const auto fitted_root = read_json(model_path);
            if (fitted_root.is_object()) {
                const auto& fitted = fitted_root.as_object();
                const std::string sha = text(find_value(fitted, "model_sha"));
                const bool safe = boolean(find_value(fitted, "paper_only"), false)
                    && !boolean(find_value(fitted, "authenticated_execution"), true);
                const auto* groups_value = find_value(fitted, "groups");
                if (safe && sha == expected_sha && groups_value != nullptr && groups_value->is_object()) {
                    const auto& groups = groups_value->as_object();
                    const auto it = groups.find("GLOBAL");
                    if (it != groups.end() && it->value().is_object()) {
                        const auto& global = it->value().as_object();
                        const double p = std::clamp(number(find_value(global, "fill_probability"), cold_fill),
                                                    1e-6, 1.0 - 1e-6);
                        model.fill_coefficients[0] = std::log(p / (1.0 - p));
                        model.markout_coefficients[0] = std::max(0.0, number(
                            find_value(global, "adverse_markout_per_share"), cold_adverse));
                        model.model_version = static_cast<std::uint64_t>(std::max<std::int64_t>(
                            1, integer(find_value(fitted, "generated_ts_ms"), 1)));
                    }
                }
            }
        } catch (...) {
            // Slow-plane model corruption never replaces the last valid snapshot.
        }
    }
    if (!model.valid()) throw std::runtime_error("invalid maker model snapshot");
    return model;
}

PaperMakerPolicy load_paper_policy(const fs::path& policy_path) {
    PaperMakerPolicy policy;
    const auto root = read_json(policy_path);
    if (!root.is_object()) return policy;
    const auto& object = root.as_object();
    const auto* quoting = child_object(object, "quoting");
    policy.cancel_latency_ns = object_integer(quoting, "cancel_latency_ms", 100) * 1'000'000LL;
    const auto* queue = child_object(object, "paper_queue");
    if (queue != nullptr) {
        const auto* multipliers_value = find_value(*queue, "queue_ahead_multipliers");
        if (multipliers_value != nullptr && multipliers_value->is_object()) {
            const auto& multipliers = multipliers_value->as_object();
            policy.queue_expected_multiplier = number(find_value(multipliers, "expected"), 1.25);
            policy.queue_upper_multiplier = number(find_value(multipliers, "upper"), 1.50);
        }
        policy.queue_confidence = object_number(queue, "cold_start_queue_confidence", 0.50);
        policy.assumed_submission_latency_ns = object_integer(
            queue, "assumed_submission_latency_ms", 1) * 1'000'000LL;
        const std::string operational = text(find_value(*queue, "operational_fill_scenario"));
        if (!operational.empty() && operational != "pessimistic") {
            throw std::runtime_error("PAPER maker operational fill must be pessimistic");
        }
    }
    if (!policy.valid()) throw std::runtime_error("invalid PAPER queue policy");
    return policy;
}

enum class TelemetryKind : std::uint8_t {
    Candidate = 1,
    PaperEvent = 2,
};

struct TelemetryRecord {
    TelemetryKind kind = TelemetryKind::Candidate;
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t state_version = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_wall_ms = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int32_t tick_size_e4 = 0;
    std::uint8_t outcome_yes = 0;
    std::uint8_t has_inventory = 0;
    std::uint16_t reserved = 0;
    Action action = Action::Withdraw;
    MakerDecision decision{};
    StrategyIntent intent{};
    PaperMakerEvent paper{};
    BookHotSnapshot book{};
    PaperMakerInventory inventory{};
};

struct ExecutionSnapshotUpdate {
    std::uint64_t market_handle = 0;
    std::uint64_t execution_version = 0;
    InventorySnapshot inventory{};
    QuoteSnapshot yes_quotes{};
    QuoteSnapshot no_quotes{};
    PaperMakerInventory paper_inventory{};
};

struct MarketContext {
    SelectedMarket cold;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    std::int32_t yes_tick_e4 = 0;
    std::int32_t no_tick_e4 = 0;
    double yes_min_order = 1.0;
    double no_min_order = 1.0;
    MakerInstrumentLane yes_lane{+1};
    MakerInstrumentLane no_lane{-1};

    MarketContext(SelectedMarket selected, std::uint64_t market,
                  std::uint64_t event, std::uint64_t yes_instrument,
                  std::uint64_t no_instrument, std::int32_t yes_tick,
                  std::int32_t no_tick, double yes_min, double no_min)
        : cold(std::move(selected)), market_handle(market), event_handle(event),
          yes_instrument_handle(yes_instrument), no_instrument_handle(no_instrument),
          yes_tick_e4(yes_tick), no_tick_e4(no_tick),
          yes_min_order(std::max(1e-6, yes_min)), no_min_order(std::max(1e-6, no_min)) {}
};

struct InstrumentRef {
    MarketContext* market = nullptr;
    std::uint8_t yes = 0;
    std::int64_t last_exchange_ns = 0;
};

[[nodiscard]] std::int64_t visible_queue_ahead(const StrategyIntent& intent,
                                                const BookHotSnapshot& book,
                                                std::int32_t tick_e4_value) noexcept {
    if (intent.type != IntentType::Quote || tick_e4_value <= 0 || intent.price_tick <= 0) return 0;
    const std::int64_t quote_e4 = intent.price_tick * static_cast<std::int64_t>(tick_e4_value);
    if (intent.side == Side::Buy) {
        if (quote_e4 > book.best_bid_e4) return 0;
        if (quote_e4 == book.best_bid_e4) return std::max<std::int64_t>(0, book.best_bid_microunits);
        return std::max<std::int64_t>(0, book.bid_depth.l10_microunits);
    }
    if (intent.side == Side::Sell) {
        if (quote_e4 < book.best_ask_e4) return 0;
        if (quote_e4 == book.best_ask_e4) return std::max<std::int64_t>(0, book.best_ask_microunits);
        return std::max<std::int64_t>(0, book.ask_depth.l10_microunits);
    }
    return 0;
}

enum class ExecutionCommandKind : std::uint8_t {
    Plan = 1,
    PublicTrade = 2,
    AdvanceTime = 3,
};

struct ExecutionContext {
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t instrument_handle = 0;
    std::uint64_t state_version = 0;
    std::int64_t exchange_event_ns = 0;
    std::int64_t receive_wall_ms = 0;
    std::int64_t receive_monotonic_ns = 0;
    std::int32_t tick_size_e4 = 0;
    std::uint8_t outcome_yes = 0;
    std::uint8_t reserved0 = 0;
    std::uint16_t reserved1 = 0;
    Action action = Action::Withdraw;
    MakerDecision decision{};
    StrategyIntent intent{};
    BookHotSnapshot book{};
};

struct ExecutionCommand {
    ExecutionCommandKind kind = ExecutionCommandKind::AdvanceTime;
    ExecutionContext context{};
    ExecutionPlan plan{};
    pm::v7::PublicTradePrint trade{};
    std::int64_t enqueue_monotonic_ns = 0;
    std::int64_t advance_monotonic_ns = 0;
};

struct ShardDiagnostics {
    pm::fast::FeedSnapshot feed{};
    std::uint64_t decisions = 0;
    std::uint64_t quote_intents = 0;
    std::uint64_t rejected_nonpositive_robust_ev = 0;
    std::uint64_t rejected_positive_point_ev = 0;
    std::int64_t best_rejected_robust_ev_nano = std::numeric_limits<std::int64_t>::min();
    std::int64_t best_rejected_point_ev_nano = std::numeric_limits<std::int64_t>::min();
    std::array<std::uint64_t, kDecisionReasonCount> reasons{};
};

static_assert(std::is_trivially_copyable_v<TelemetryRecord>);
static_assert(std::is_trivially_copyable_v<ExecutionSnapshotUpdate>);
static_assert(std::is_trivially_copyable_v<ExecutionCommand>);

class ShardRuntime final {
public:
    ShardRuntime(std::vector<MarketContext*> markets,
                 std::shared_ptr<MakerModelStore> models,
                 std::atomic<bool>* global_kill,
                 std::atomic<bool>* new_risk_frozen,
                 std::atomic<bool>* fatal,
                 std::string ws_url)
        : markets_(std::move(markets)), models_(std::move(models)),
          global_kill_(global_kill), new_risk_frozen_(new_risk_frozen),
          fatal_(fatal), ws_url_(std::move(ws_url)) {
        std::vector<pm::v7::TokenBinding> bindings;
        std::size_t max_instrument = 0;
        std::size_t max_market = 0;
        for (auto* market : markets_) {
            max_market = std::max<std::size_t>(max_market, market->market_handle);
            max_instrument = std::max<std::size_t>(max_instrument, market->no_instrument_handle);
            bindings.push_back({market->cold.yes_token, market->market_handle, market->event_handle,
                                market->yes_instrument_handle, market->yes_tick_e4});
            bindings.push_back({market->cold.no_token, market->market_handle, market->event_handle,
                                market->no_instrument_handle, market->no_tick_e4});
            tokens_.push_back(market->cold.yes_token);
            tokens_.push_back(market->cold.no_token);
        }
        refs_.resize(max_instrument + 1);
        execution_state_.resize(max_market + 1);
        for (auto* market : markets_) {
            refs_[market->yes_instrument_handle] = InstrumentRef{market, 1, 0};
            refs_[market->no_instrument_handle] = InstrumentRef{market, 0, 0};
            execution_state_[market->market_handle].market_handle = market->market_handle;
        }
        decoder_ = std::make_unique<pm::v7::MarketWsShard>(std::move(bindings));
    }

    void start() {
        feed_ = std::make_unique<pm::fast::MarketWebSocketFeed>(
            ws_url_, tokens_, std::max<std::size_t>(1, tokens_.size()),
            [this](std::string_view payload, const pm::fast::FeedReceiveStamp& receive,
                   std::size_t) { on_payload(payload, receive); },
            [this](std::size_t, std::string_view) { on_transport_error(); });
        feed_->start();
    }

    void stop() {
        if (feed_) feed_->stop();
    }

    [[nodiscard]] bool pop(TelemetryRecord& record) noexcept {
        return telemetry_.try_pop(record);
    }

    [[nodiscard]] bool pop_critical(ExecutionCommand& command) noexcept {
        return execution_critical_.try_pop(command);
    }

    [[nodiscard]] bool pop_normal(ExecutionCommand& command) noexcept {
        return execution_normal_.try_pop(command);
    }

    [[nodiscard]] std::size_t critical_backlog() const noexcept {
        return execution_critical_.approximate_size();
    }

    [[nodiscard]] std::size_t normal_backlog() const noexcept {
        return execution_normal_.approximate_size();
    }

    [[nodiscard]] ShardDiagnostics diagnostics() const noexcept {
        ShardDiagnostics out;
        if (feed_) out.feed = feed_->snapshot();
        out.decisions = decisions_.load(std::memory_order_relaxed);
        out.quote_intents = quote_intents_.load(std::memory_order_relaxed);
        out.rejected_nonpositive_robust_ev =
            rejected_nonpositive_robust_ev_.load(std::memory_order_relaxed);
        out.rejected_positive_point_ev = rejected_positive_point_ev_.load(std::memory_order_relaxed);
        out.best_rejected_robust_ev_nano = best_rejected_robust_ev_nano_.load(std::memory_order_relaxed);
        out.best_rejected_point_ev_nano = best_rejected_point_ev_nano_.load(std::memory_order_relaxed);
        for (std::size_t i = 0; i < out.reasons.size(); ++i) {
            out.reasons[i] = decision_reasons_[i].load(std::memory_order_relaxed);
        }
        return out;
    }

    void seed_execution_snapshot(const ExecutionSnapshotUpdate& update) noexcept {
        if (update.market_handle < execution_state_.size()) {
            execution_state_[static_cast<std::size_t>(update.market_handle)] = update;
        }
    }

    [[nodiscard]] bool push_execution_snapshot(const ExecutionSnapshotUpdate& update) noexcept {
        return execution_snapshots_.try_push(update);
    }

private:
    [[nodiscard]] InstrumentRef* ref(std::uint64_t instrument) noexcept {
        if (instrument >= refs_.size() || refs_[instrument].market == nullptr) return nullptr;
        return &refs_[instrument];
    }

    [[nodiscard]] const ExecutionSnapshotUpdate& execution_state(
        std::uint64_t market_handle) const noexcept {
        static const ExecutionSnapshotUpdate empty{};
        return market_handle < execution_state_.size()
            ? execution_state_[static_cast<std::size_t>(market_handle)] : empty;
    }

    void drain_execution_snapshots() noexcept {
        ExecutionSnapshotUpdate update;
        while (execution_snapshots_.try_pop(update)) {
            if (update.market_handle < execution_state_.size()) {
                execution_state_[static_cast<std::size_t>(update.market_handle)] = update;
            }
        }
    }

    void push(const TelemetryRecord& record) noexcept {
        if (!telemetry_.try_push(record)) fatal_->store(true, std::memory_order_release);
    }

    [[nodiscard]] ExecutionContext make_context(
        MarketContext& market,
        std::uint64_t instrument_handle,
        bool outcome_yes,
        Action action,
        const BookHotSnapshot& book,
        std::int64_t exchange_event_ns,
        const pm::fast::FeedReceiveStamp& receive) const noexcept {
        ExecutionContext context;
        context.market_handle = market.market_handle;
        context.event_handle = market.event_handle;
        context.instrument_handle = instrument_handle;
        context.state_version = book.state_version;
        context.exchange_event_ns = exchange_event_ns;
        context.receive_wall_ms = receive.wall_ms;
        context.receive_monotonic_ns = receive.monotonic_ns;
        context.tick_size_e4 = book.tick_size_e4 > 0
            ? book.tick_size_e4 : (outcome_yes ? market.yes_tick_e4 : market.no_tick_e4);
        context.outcome_yes = static_cast<std::uint8_t>(outcome_yes);
        context.action = action;
        context.book = book;
        return context;
    }

    [[nodiscard]] bool enqueue_execution(const ExecutionCommand& command) noexcept {
        ExecutionCommand stamped = command;
        stamped.enqueue_monotonic_ns = monotonic_ns();
        const bool critical = stamped.kind != ExecutionCommandKind::Plan
            || pm::v7::execution_plan_is_critical(stamped.plan);
        const bool accepted = critical
            ? execution_critical_.try_push(stamped)
            : execution_normal_.try_push(stamped);
        if (!accepted) fatal_->store(true, std::memory_order_release);
        return accepted;
    }

    void push_candidate(MarketContext& market, bool outcome_yes,
                        const MakerDecision& decision, const StrategyIntent& intent,
                        const BookHotSnapshot& book,
                        const pm::fast::FeedReceiveStamp& receive) noexcept {
        TelemetryRecord record;
        record.kind = TelemetryKind::Candidate;
        record.market_handle = market.market_handle;
        record.event_handle = market.event_handle;
        record.instrument_handle = intent.instrument_handle;
        record.state_version = book.state_version;
        record.exchange_event_ns = intent.exchange_event_ns;
        record.receive_wall_ms = receive.wall_ms;
        record.receive_monotonic_ns = receive.monotonic_ns;
        record.tick_size_e4 = book.tick_size_e4;
        record.outcome_yes = static_cast<std::uint8_t>(outcome_yes);
        record.action = decision.action;
        record.decision = decision;
        record.intent = intent;
        record.book = book;
        push(record);
    }

    [[nodiscard]] double min_order(const MarketContext& market, bool yes) const noexcept {
        return yes ? market.yes_min_order : market.no_min_order;
    }

    void apply_decision(MarketContext& market, InstrumentRef& instrument,
                        const MarketWsEvent& source_event,
                        const pm::fast::FeedReceiveStamp& receive) noexcept {
        if (normal_backlog() >= kExecutionBacklogSoftLimit
            || critical_backlog() >= kExecutionCriticalCapacity / 2) {
            // Execution ownership is authoritative. Never keep producing quote
            // traffic when the order-TX core is materially behind the feed.
            return;
        }

        const auto shared_model = models_->snapshot();
        if (!shared_model) {
            fatal_->store(true, std::memory_order_release);
            return;
        }
        MakerModelSnapshot model = *shared_model;
        model.base_quote_shares = std::max(model.base_quote_shares,
                                           min_order(market, instrument.yes != 0));
        // Reward qualification is not guaranteed P&L and is currently credited
        // as zero. Do not inflate trading inventory to the reward-program minimum;
        // size from the venue minimum and the common capital/risk envelope only.

        const auto& state = execution_state(market.market_handle);
        MakerLaneContext context;
        context.inventory = state.inventory;
        context.quotes = instrument.yes ? state.yes_quotes : state.no_quotes;
        // The runtime does not yet receive authoritative per-order rebate or
        // reward accruals. Keep both decision credits explicitly at zero until
        // that attribution exists; eligibility metadata alone is not economic
        // P&L and must never make an otherwise negative quote admissible.
        context.conservative_rebate_ev_per_share = 0.0;
        context.conservative_reward_ev_per_share = 0.0;
        const double mid = source_event.book.valid
            ? 0.5 * (e4_price(source_event.book.best_bid_e4) + e4_price(source_event.book.best_ask_e4))
            : 0.5;
        const double capital_bound = std::max(0.0, starting_capital_fraction_)
            / std::max(0.02, std::min(0.98, mid));
        context.risk.max_quote_shares = std::max(min_order(market, instrument.yes != 0),
                                                 std::min(100.0, capital_bound));
        context.risk.max_abs_residual_shares = std::max(
            2.0 * context.risk.max_quote_shares, 10.0 * min_order(market, instrument.yes != 0));
        const double exploration_cap = context.risk.max_quote_shares
            * model.exploration_quote_notional_fraction / 0.01;
        context.risk.exploration_max_quote_shares = exploration_cap + 1e-12
            >= min_order(market, instrument.yes != 0) ? exploration_cap : 0.0;
        context.risk.max_local_state_age_ns = 5'000'000'000LL;
        context.risk.global_kill = static_cast<std::uint8_t>(
            global_kill_->load(std::memory_order_acquire));
        context.risk.new_risk_frozen = static_cast<std::uint8_t>(
            new_risk_frozen_->load(std::memory_order_acquire));

        auto& lane = instrument.yes ? market.yes_lane : market.no_lane;
        MakerDecision decision = lane.on_market_event(source_event, context, model);
        decision.latency.parse_ns = source_event.frame_parse_ns;
        decision.latency.book_ns = source_event.book_apply_ns;
        decisions_.fetch_add(1, std::memory_order_relaxed);
        const auto reason_index = static_cast<std::size_t>(decision.reason);
        if (reason_index < decision_reasons_.size()) {
            decision_reasons_[reason_index].fetch_add(1, std::memory_order_relaxed);
        }
        if (decision.reason == pm::v7::maker::DecisionReason::NoEconomicQuote
            && decision.robust_ev <= model.min_robust_ev_per_share) {
            rejected_nonpositive_robust_ev_.fetch_add(1, std::memory_order_relaxed);
            const double point_ev = decision.robust_ev
                + std::max(0.0, model.robust_ev_z) * decision.ev_uncertainty;
            if (point_ev > model.min_robust_ev_per_share) {
                rejected_positive_point_ev_.fetch_add(1, std::memory_order_relaxed);
            }
            update_atomic_max(best_rejected_robust_ev_nano_, ev_nano(decision.robust_ev));
            update_atomic_max(best_rejected_point_ev_nano_, ev_nano(point_ev));
        }
        for (std::size_t i = 0; i < decision.intent_count; ++i) {
            StrategyIntent intent = decision.intents[i];
            // Safety controls must remain executable even after canonical lineage
            // invalidation zeroes the book clock. Preserve the most recent proven
            // exchange clock for the common PAPER OMS cancellation transition.
            if (intent.exchange_event_ns <= 0 && intent.type != IntentType::Quote) {
                intent.exchange_event_ns = std::max<std::int64_t>(1, instrument.last_exchange_ns);
            }
            if (intent.type == IntentType::Quote) {
                quote_intents_.fetch_add(1, std::memory_order_relaxed);
                const double shares_requested = micro_shares(intent.quantity_microunits);
                if (shares_requested + 1e-12 < min_order(market, instrument.yes != 0)) continue;
                push_candidate(market, instrument.yes != 0, decision, intent,
                               source_event.book, receive);
            }

            ExecutionCommand command;
            command.kind = ExecutionCommandKind::Plan;
            command.context = make_context(
                market, source_event.instrument_handle, instrument.yes != 0,
                decision.action, source_event.book, intent.exchange_event_ns, receive);
            command.context.intent = intent;
            command.context.decision = decision;
            command.plan.intent = intent;
            command.plan.public_queue_ahead_microunits = visible_queue_ahead(
                intent, source_event.book, command.context.tick_size_e4);
            command.plan.tick_size_e4 = command.context.tick_size_e4;
            command.plan.market_state_version = source_event.book.state_version;
            command.plan.policy = pm::v7::is_critical_intent(intent.type)
                ? ExecutionPolicyId::Emergency : ExecutionPolicyId::PassiveMaker;
            command.plan.public_queue_observable = static_cast<std::uint8_t>(
                intent.type != IntentType::Quote
                || (source_event.book.valid && source_event.book.tick_size_e4 > 0));
            if (!enqueue_execution(command)) return;
        }
    }

    void advance_market(MarketContext& market, const BookHotSnapshot& book,
                        std::uint64_t instrument, bool yes, Action action,
                        std::int64_t exchange_ns,
                        const pm::fast::FeedReceiveStamp& receive) noexcept {
        ExecutionCommand command;
        command.kind = ExecutionCommandKind::AdvanceTime;
        command.context = make_context(
            market, instrument, yes, action, book, exchange_ns, receive);
        command.advance_monotonic_ns = receive.monotonic_ns;
        (void)enqueue_execution(command);
    }

    void process_event(MarketWsEvent event, const pm::fast::FeedReceiveStamp& receive,
                       std::uint64_t trade_id) noexcept {
        InstrumentRef* instrument = ref(event.instrument_handle);
        if (instrument == nullptr) return;
        MarketContext& market = *instrument->market;
        if (event.exchange_event_ns > 0) instrument->last_exchange_ns = event.exchange_event_ns;
        if (event.receive_monotonic_ns <= 0) event.receive_monotonic_ns = receive.monotonic_ns;
        if (event.exchange_event_ns <= 0) event.exchange_event_ns = instrument->last_exchange_ns;
        if (event.kind == MarketWsEventKind::LineageInvalidated) {
            event.book = decoder_->snapshot(event.instrument_handle);
        }

        advance_market(market, event.book, event.instrument_handle, instrument->yes != 0,
                       Action::Withdraw, event.exchange_event_ns, receive);

        if (event.kind == MarketWsEventKind::Trade && event.book.valid
            && event.book.tick_size_e4 > 0 && event.price_e4 > 0
            && event.price_e4 % event.book.tick_size_e4 == 0) {
            ExecutionCommand command;
            command.kind = ExecutionCommandKind::PublicTrade;
            command.context = make_context(
                market, event.instrument_handle, instrument->yes != 0,
                Action::Join, event.book, event.exchange_event_ns, receive);
            command.trade.trade_id = trade_id == 0 ? 1 : trade_id;
            command.trade.instrument_handle = event.instrument_handle;
            command.trade.aggressor_side = event.side;
            command.trade.price_tick = event.price_e4 / event.book.tick_size_e4;
            command.trade.quantity_microunits = event.quantity_microunits;
            command.trade.exchange_event_ns = event.exchange_event_ns;
            command.trade.receive_monotonic_ns = receive.monotonic_ns;
            if (!enqueue_execution(command)) return;
        }

        apply_decision(market, *instrument, event, receive);
    }

    void invalidate_all_decisions(const pm::fast::FeedReceiveStamp& receive) noexcept {
        decoder_->invalidate_all_lineage();
        for (auto& instrument : refs_) {
            if (instrument.market == nullptr) continue;
            MarketWsEvent event;
            event.kind = MarketWsEventKind::LineageInvalidated;
            event.market_handle = instrument.market->market_handle;
            event.event_handle = instrument.market->event_handle;
            event.instrument_handle = instrument.yes
                ? instrument.market->yes_instrument_handle : instrument.market->no_instrument_handle;
            event.exchange_event_ns = instrument.last_exchange_ns;
            event.receive_monotonic_ns = receive.monotonic_ns;
            event.book = decoder_->snapshot(event.instrument_handle);
            process_event(event, receive, 0);
        }
    }

    void on_payload(std::string_view payload, const pm::fast::FeedReceiveStamp& receive) noexcept {
        drain_execution_snapshots();
        std::array<MarketWsEvent, kWsOutputCapacity> events{};
        const auto result = decoder_->process_frame(payload, receive, events);
        const std::uint64_t frame_hash = fnv1a(payload);
        for (std::size_t i = 0; i < result.output_count; ++i) {
            const std::uint64_t trade_id = mix64(frame_hash ^ (static_cast<std::uint64_t>(i + 1) << 32U)
                ^ events[i].instrument_handle ^ static_cast<std::uint64_t>(events[i].exchange_event_ns));
            process_event(events[i], receive, trade_id);
        }
        if (result.lineage_invalidated && result.output_count == 0) {
            invalidate_all_decisions(receive);
        }
        if (result.output_overflow || result.arena_exhausted) {
            fatal_->store(true, std::memory_order_release);
        }
    }

    void on_transport_error() noexcept {
        drain_execution_snapshots();
        invalidate_all_decisions(receive_now());
    }

public:
    void set_starting_capital_fraction(double dollars) noexcept {
        starting_capital_fraction_ = std::max(0.0, dollars);
    }

private:
    [[nodiscard]] static std::int64_t ev_nano(double value) noexcept {
        if (!std::isfinite(value)) return std::numeric_limits<std::int64_t>::min();
        constexpr double scale = 1'000'000'000.0;
        const double bounded = std::clamp(
            value, static_cast<double>(std::numeric_limits<std::int64_t>::min() + 1) / scale,
            static_cast<double>(std::numeric_limits<std::int64_t>::max()) / scale);
        return static_cast<std::int64_t>(std::llround(bounded * scale));
    }

    static void update_atomic_max(std::atomic<std::int64_t>& target,
                                  std::int64_t value) noexcept {
        auto current = target.load(std::memory_order_relaxed);
        while (value > current && !target.compare_exchange_weak(
                   current, value, std::memory_order_relaxed, std::memory_order_relaxed)) {}
    }

    std::vector<MarketContext*> markets_;
    std::shared_ptr<MakerModelStore> models_;
    std::atomic<bool>* global_kill_ = nullptr;
    std::atomic<bool>* new_risk_frozen_ = nullptr;
    std::atomic<bool>* fatal_ = nullptr;
    std::string ws_url_;
    std::vector<std::string> tokens_;
    std::vector<InstrumentRef> refs_;
    std::vector<ExecutionSnapshotUpdate> execution_state_;
    std::unique_ptr<pm::v7::MarketWsShard> decoder_;
    std::unique_ptr<pm::fast::MarketWebSocketFeed> feed_;
    pm::v7::SpscRing<TelemetryRecord, kTelemetryCapacity> telemetry_{};
    pm::v7::SpscRing<ExecutionCommand, kExecutionCriticalCapacity> execution_critical_{};
    pm::v7::SpscRing<ExecutionCommand, kExecutionNormalCapacity> execution_normal_{};
    pm::v7::SpscRing<ExecutionSnapshotUpdate, kExecutionSnapshotCapacity> execution_snapshots_{};
    std::array<std::atomic<std::uint64_t>, kDecisionReasonCount> decision_reasons_{};
    std::atomic<std::uint64_t> decisions_{0};
    std::atomic<std::uint64_t> quote_intents_{0};
    std::atomic<std::uint64_t> rejected_nonpositive_robust_ev_{0};
    std::atomic<std::uint64_t> rejected_positive_point_ev_{0};
    std::atomic<std::int64_t> best_rejected_robust_ev_nano_{
        std::numeric_limits<std::int64_t>::min()};
    std::atomic<std::int64_t> best_rejected_point_ev_nano_{
        std::numeric_limits<std::int64_t>::min()};
    double starting_capital_fraction_ = 5.0;
};

void write_runtime_diagnostics(
    const fs::path& run_root, std::string_view model_sha,
    const std::vector<std::unique_ptr<ShardRuntime>>& shards) {
    ShardDiagnostics total;
    for (const auto& shard : shards) {
        const auto value = shard->diagnostics();
        total.feed.workers += value.feed.workers;
        total.feed.connected_workers += value.feed.connected_workers;
        total.feed.messages += value.feed.messages;
        total.feed.reconnects += value.feed.reconnects;
        total.feed.errors += value.feed.errors;
        total.decisions += value.decisions;
        total.quote_intents += value.quote_intents;
        total.rejected_nonpositive_robust_ev += value.rejected_nonpositive_robust_ev;
        total.rejected_positive_point_ev += value.rejected_positive_point_ev;
        total.best_rejected_robust_ev_nano = std::max(
            total.best_rejected_robust_ev_nano, value.best_rejected_robust_ev_nano);
        total.best_rejected_point_ev_nano = std::max(
            total.best_rejected_point_ev_nano, value.best_rejected_point_ev_nano);
        for (std::size_t i = 0; i < total.reasons.size(); ++i) {
            total.reasons[i] += value.reasons[i];
        }
    }
    json::object reasons;
    for (std::size_t i = 1; i < total.reasons.size(); ++i) {
        reasons[decision_reason_name(static_cast<pm::v7::maker::DecisionReason>(i))] =
            total.reasons[i];
    }
    json::object root;
    root["schema"] = "polymarket_v7_maker_runtime_diagnostics_v1";
    root["timestamp_ms"] = wall_ms();
    root["paper_only"] = true;
    root["authenticated_execution"] = false;
    root["real_order_submission"] = false;
    root["model_sha"] = model_sha;
    root["feed_workers"] = total.feed.workers;
    root["feed_connected_workers"] = total.feed.connected_workers;
    root["feed_messages"] = total.feed.messages;
    root["feed_reconnects"] = total.feed.reconnects;
    root["feed_errors"] = total.feed.errors;
    root["decisions"] = total.decisions;
    root["quote_intents"] = total.quote_intents;
    root["rejected_nonpositive_robust_ev"] = total.rejected_nonpositive_robust_ev;
    root["rejected_positive_point_ev"] = total.rejected_positive_point_ev;
    if (total.best_rejected_robust_ev_nano != std::numeric_limits<std::int64_t>::min()) {
        root["best_rejected_robust_ev_per_share"] =
            static_cast<double>(total.best_rejected_robust_ev_nano) / 1'000'000'000.0;
    }
    if (total.best_rejected_point_ev_nano != std::numeric_limits<std::int64_t>::min()) {
        root["best_rejected_point_ev_per_share"] =
            static_cast<double>(total.best_rejected_point_ev_nano) / 1'000'000'000.0;
    }
    root["reason_counts"] = std::move(reasons);
    atomic_write(run_root / "micro_maker" / "runtime_diagnostics.json",
                 json::serialize(root) + "\n");
}

class ExecutionCore final {
public:
    [[nodiscard]] bool initialize(
        const std::vector<std::unique_ptr<MarketContext>>& markets,
        PaperMakerPolicy paper_policy,
        double starting_capital,
        std::int64_t minimum_quote_lifetime_ns,
        std::uint32_t exploration_concurrent_market_cap) {
        const std::int64_t budget = microdollars(starting_capital);
        if (budget < 100) return false;
        CapitalLimits limits;
        limits.sleeve_budget_microdollars = budget;
        limits.max_total_exposure_microdollars = budget;
        limits.max_market_exposure_microdollars = std::max<std::int64_t>(1, budget / 20);
        limits.max_single_order_microdollars = std::max<std::int64_t>(1, budget / 100);
        limits.max_single_order_microdollars = std::min(
            limits.max_single_order_microdollars, limits.max_market_exposure_microdollars);
        if (!capital_.configure(limits)) return false;

        std::size_t max_market = 0;
        std::size_t max_instrument = 0;
        for (const auto& market : markets) {
            max_market = std::max<std::size_t>(max_market, market->market_handle);
            max_instrument = std::max<std::size_t>(
                max_instrument, std::max(market->yes_instrument_handle,
                                         market->no_instrument_handle));
        }
        markets_.assign(max_market + 1, nullptr);
        control_watermarks_.assign(max_instrument + 1, {});
        exploration_market_active_.assign(max_market + 1, 0);
        minimum_quote_lifetime_ns_ = std::max<std::int64_t>(0, minimum_quote_lifetime_ns);
        exploration_concurrent_market_cap_ = exploration_concurrent_market_cap;
        for (const auto& market : markets) {
            if (market->yes_tick_e4 != market->no_tick_e4) return false;
            if (!policy_.register_market(
                    market->market_handle,
                    market->yes_instrument_handle,
                    market->no_instrument_handle,
                    market->yes_tick_e4,
                    paper_policy)) {
                return false;
            }
            markets_[market->market_handle] = market.get();
        }
        execution_version_ = 1;
        return true;
    }

    [[nodiscard]] bool process(const ExecutionCommand& command) noexcept {
        if (command.context.market_handle == 0
            || command.context.market_handle >= markets_.size()
            || markets_[command.context.market_handle] == nullptr) {
            return false;
        }

        const std::int64_t execution_start_ns = monotonic_ns();
        ExecutionContext measured_context = command.context;
        measured_context.decision.latency.tx_queue_ns = command.enqueue_monotonic_ns > 0
            ? std::max<std::int64_t>(0, execution_start_ns - command.enqueue_monotonic_ns) : 0;
        pm::v7::maker::MakerExecutionResult result;
        switch (command.kind) {
            case ExecutionCommandKind::Plan: {
                const auto& intent = command.plan.intent;
                // Cancels use a priority queue, so they may legitimately overtake
                // an older quote still waiting in the normal queue. Remember the
                // control boundary and suppress that stale quote when it arrives;
                // otherwise cancel-before-place would resurrect unwanted risk.
                if (intent.type == IntentType::Quote && quote_is_superseded(intent)) {
                    ++execution_version_;
                    return true;
                }
                if (economic_control_preempts_fresh_quote(command)) {
                    ++execution_version_;
                    return true;
                }
                const bool exploratory_placement = intent.type == IntentType::Quote
                    && command.context.decision.reason
                        == pm::v7::maker::DecisionReason::ExplorationQuote;
                if (exploratory_placement
                    && !exploration_market_active(command.context.market_handle)
                    && exploration_active_market_count_
                        >= exploration_concurrent_market_cap_) {
                    ++execution_version_;
                    return true;
                }
                record_control_watermark(intent, *markets_[command.context.market_handle]);
                result = policy_.process(command.plan, capital_);
                if (exploratory_placement && result.accepted) {
                    set_exploration_market_active(command.context.market_handle, true);
                }
                break;
            }
            case ExecutionCommandKind::PublicTrade:
                result = policy_.on_public_trade(
                    command.context.market_handle, command.trade, capital_);
                break;
            case ExecutionCommandKind::AdvanceTime:
                result = policy_.advance_time(
                    command.context.market_handle, command.advance_monotonic_ns, capital_);
                break;
        }

        refresh_exploration_market(command.context.market_handle);

        const std::int64_t execution_end_ns = monotonic_ns();
        measured_context.decision.latency.execution_ns = std::max<std::int64_t>(
            0, execution_end_ns - execution_start_ns);
        ++execution_version_;
        if (result.paper.event_count > 0) emit(measured_context, result.paper);
        if (result.capital_invariant_violation || result.paper.invariant_violation
            || result.reason == MakerExecutionReason::CapitalInvariant
            || result.reason == MakerExecutionReason::UnknownMarket) {
            return false;
        }
        return true;
    }

    [[nodiscard]] ExecutionSnapshotUpdate snapshot(std::uint64_t market_handle) const noexcept {
        ExecutionSnapshotUpdate update;
        if (market_handle == 0 || market_handle >= markets_.size()) return update;
        const auto* market = markets_[market_handle];
        if (market == nullptr) return update;
        update.market_handle = market_handle;
        update.execution_version = execution_version_;
        update.inventory = policy_.inventory_snapshot(market_handle);
        update.yes_quotes = policy_.quote_snapshot(market_handle, market->yes_instrument_handle);
        update.no_quotes = policy_.quote_snapshot(market_handle, market->no_instrument_handle);
        if (const auto* inventory = policy_.inventory(market_handle); inventory != nullptr) {
            update.paper_inventory = *inventory;
        }
        return update;
    }

    [[nodiscard]] bool pop_telemetry(TelemetryRecord& record) noexcept {
        return telemetry_.try_pop(record);
    }

private:
    struct SideControlWatermark {
        std::int64_t bid_ns = 0;
        std::int64_t ask_ns = 0;
    };

    [[nodiscard]] bool exploration_market_active(std::uint64_t market_handle) const noexcept {
        return market_handle < exploration_market_active_.size()
            && exploration_market_active_[market_handle] != 0;
    }

    void set_exploration_market_active(std::uint64_t market_handle, bool active) noexcept {
        if (market_handle >= exploration_market_active_.size()) return;
        const bool current = exploration_market_active_[market_handle] != 0;
        if (current == active) return;
        exploration_market_active_[market_handle] = static_cast<std::uint8_t>(active);
        if (active) ++exploration_active_market_count_;
        else if (exploration_active_market_count_ > 0) --exploration_active_market_count_;
    }

    void refresh_exploration_market(std::uint64_t market_handle) noexcept {
        if (!exploration_market_active(market_handle)) return;
        const auto* market = market_handle < markets_.size() ? markets_[market_handle] : nullptr;
        if (market == nullptr) return;
        const auto yes = policy_.quote_snapshot(market_handle, market->yes_instrument_handle);
        const auto no = policy_.quote_snapshot(market_handle, market->no_instrument_handle);
        const bool order_or_cancel_pending = yes.bid_active != 0 || yes.ask_active != 0
            || yes.cancel_pending != 0 || no.bid_active != 0 || no.ask_active != 0
            || no.cancel_pending != 0;
        if (!order_or_cancel_pending) set_exploration_market_active(market_handle, false);
    }

    [[nodiscard]] bool economic_control_preempts_fresh_quote(
        const ExecutionCommand& command) const noexcept {
        const auto& intent = command.plan.intent;
        if (minimum_quote_lifetime_ns_ <= 0
            || (intent.type != IntentType::CancelQuote
                && intent.type != IntentType::Withdraw)) {
            return false;
        }
        using Reason = pm::v7::maker::DecisionReason;
        const auto reason = command.context.decision.reason;
        if (reason != Reason::Quote && reason != Reason::NoEconomicQuote
            && reason != Reason::QuoteLifetimeHold
            && reason != Reason::ExplorationHold
            && reason != Reason::ExplorationExpired) {
            return false;
        }
        const auto snapshot = policy_.quote_snapshot(
            intent.market_handle, intent.instrument_handle);
        const bool targeted_active = intent.type == IntentType::Withdraw
            ? snapshot.bid_active != 0 || snapshot.ask_active != 0
            : intent.side == Side::Buy ? snapshot.bid_active != 0
            : intent.side == Side::Sell ? snapshot.ask_active != 0
            : false;
        if (!targeted_active || snapshot.last_quote_monotonic_ns <= 0) return false;
        return intent.decision_monotonic_ns - snapshot.last_quote_monotonic_ns
            < minimum_quote_lifetime_ns_;
    }

    [[nodiscard]] bool quote_is_superseded(const StrategyIntent& intent) const noexcept {
        if (intent.instrument_handle >= control_watermarks_.size()) return true;
        const auto& watermark = control_watermarks_[intent.instrument_handle];
        const std::int64_t boundary = intent.side == Side::Buy ? watermark.bid_ns
            : intent.side == Side::Sell ? watermark.ask_ns
            : std::numeric_limits<std::int64_t>::max();
        return intent.decision_monotonic_ns <= boundary;
    }

    void watermark_instrument(std::uint64_t instrument_handle, Side side,
                              std::int64_t decision_ns) noexcept {
        if (instrument_handle >= control_watermarks_.size() || decision_ns <= 0) return;
        auto& watermark = control_watermarks_[instrument_handle];
        if (side == Side::Buy || side == Side::None) {
            watermark.bid_ns = std::max(watermark.bid_ns, decision_ns);
        }
        if (side == Side::Sell || side == Side::None) {
            watermark.ask_ns = std::max(watermark.ask_ns, decision_ns);
        }
    }

    void record_control_watermark(const StrategyIntent& intent,
                                  const MarketContext& market) noexcept {
        if (intent.type == IntentType::CancelQuote) {
            watermark_instrument(intent.instrument_handle, intent.side,
                                 intent.decision_monotonic_ns);
        } else if (intent.type == IntentType::Withdraw) {
            watermark_instrument(intent.instrument_handle, Side::None,
                                 intent.decision_monotonic_ns);
        } else if (intent.type == IntentType::Kill) {
            watermark_instrument(market.yes_instrument_handle, Side::None,
                                 intent.decision_monotonic_ns);
            watermark_instrument(market.no_instrument_handle, Side::None,
                                 intent.decision_monotonic_ns);
        }
    }

    void emit(const ExecutionContext& context, const PaperMakerResult& result) noexcept {
        const auto* market = markets_[context.market_handle];
        if (market == nullptr) return;
        const auto* inventory = policy_.inventory(context.market_handle);
        for (std::size_t i = 0; i < result.event_count; ++i) {
            TelemetryRecord record;
            record.kind = TelemetryKind::PaperEvent;
            record.market_handle = context.market_handle;
            record.event_handle = context.event_handle;
            record.instrument_handle = result.events[i].instrument_handle != 0
                ? result.events[i].instrument_handle : context.instrument_handle;
            record.state_version = context.state_version;
            record.exchange_event_ns = context.exchange_event_ns;
            record.receive_wall_ms = context.receive_wall_ms;
            record.receive_monotonic_ns = context.receive_monotonic_ns;
            record.tick_size_e4 = result.events[i].tick_size_e4 > 0
                ? result.events[i].tick_size_e4
                : (record.instrument_handle == market->yes_instrument_handle
                    ? market->yes_tick_e4 : market->no_tick_e4);
            record.outcome_yes = static_cast<std::uint8_t>(
                record.instrument_handle == market->yes_instrument_handle);
            record.has_inventory = 1;
            record.action = context.action;
            record.intent = context.intent;
            record.decision = context.decision;
            record.paper = result.events[i];
            record.book = context.book;
            if (inventory != nullptr) record.inventory = *inventory;
            if (!telemetry_.try_push(record)) {
                telemetry_overflow_.store(true, std::memory_order_release);
                return;
            }
        }
    }

public:
    [[nodiscard]] bool telemetry_overflow() const noexcept {
        return telemetry_overflow_.load(std::memory_order_acquire);
    }

private:
    MakerPaperExecutionPolicy policy_{};
    SleeveCapitalAccount capital_{};
    std::vector<MarketContext*> markets_;
    std::vector<SideControlWatermark> control_watermarks_;
    std::vector<std::uint8_t> exploration_market_active_;
    std::int64_t minimum_quote_lifetime_ns_ = 0;
    std::uint32_t exploration_concurrent_market_cap_ = 0;
    std::uint32_t exploration_active_market_count_ = 0;
    pm::v7::SpscRing<TelemetryRecord, kExecutionTelemetryCapacity> telemetry_{};
    std::atomic<bool> telemetry_overflow_{false};
    std::uint64_t execution_version_ = 1;
};

class TelemetryWriter final {
public:
    TelemetryWriter(fs::path run_root, std::string model_sha,
                    double starting_capital,
                    const std::vector<std::unique_ptr<MarketContext>>& markets)
        : run_root_(std::move(run_root)), model_sha_(std::move(model_sha)),
          starting_capital_(starting_capital),
          telemetry_epoch_(std::to_string(wall_ms()) + "-" +
                           hex64(static_cast<std::uint64_t>(monotonic_ns()))) {
        std::size_t max_market = 0;
        std::size_t max_instrument = 0;
        for (const auto& market : markets) {
            max_market = std::max<std::size_t>(max_market, market->market_handle);
            max_instrument = std::max<std::size_t>(max_instrument, market->no_instrument_handle);
        }
        markets_.resize(max_market + 1, nullptr);
        instruments_.resize(max_instrument + 1);
        inventory_.resize(max_market + 1);
        for (const auto& market : markets) {
            markets_[market->market_handle] = market.get();
            instruments_[market->yes_instrument_handle] = {market.get(), true};
            instruments_[market->no_instrument_handle] = {market.get(), false};
        }
        fs::create_directories(run_root_ / "ledger" / "spool");
        fs::create_directories(run_root_ / "micro_maker");
    }

    void consume(const TelemetryRecord& record) {
        if (record.market_handle >= markets_.size() || markets_[record.market_handle] == nullptr) return;
        if (record.has_inventory) inventory_[record.market_handle] = record.inventory;
        if (record.kind == TelemetryKind::Candidate) write_candidate(record);
        else write_paper(record);
    }

    void write_state() {
        json::object root;
        root["schema"] = "polymarket_v7_professional_maker_state_v2";
        root["timestamp_ms"] = wall_ms();
        root["paper_only"] = true;
        root["authenticated_execution"] = false;
        root["real_order_submission"] = false;
        root["model_sha"] = model_sha_;
        root["starting_capital"] = starting_capital_;

        double remaining_cost = 0.0;
        double realized = 0.0;
        json::object inventory;
        for (std::size_t handle = 1; handle < markets_.size(); ++handle) {
            const auto* market = markets_[handle];
            if (market == nullptr) continue;
            const auto& state = inventory_[handle];
            remaining_cost += state.yes_cost + state.no_cost;
            realized += state.realized_trading_pnl;
            json::object row;
            // Inventory identity must remain self-contained.  The live reward
            // selection rotates independently of held PAPER inventory, so a
            // risk mark cannot rely on the current selection still containing
            // the market that produced an earlier fill.
            row["condition_id"] = market->cold.condition_id;
            row["yes_token"] = market->cold.yes_token;
            row["no_token"] = market->cold.no_token;
            row["yes_shares"] = micro_shares(state.yes_microunits);
            row["no_shares"] = micro_shares(state.no_microunits);
            row["yes_cost"] = state.yes_cost;
            row["no_cost"] = state.no_cost;
            inventory[market->cold.market_id] = std::move(row);
        }
        root["cash"] = starting_capital_ - remaining_cost + realized;
        root["realized_trading_pnl"] = realized;
        root["estimated_maker_rebate_pnl"] = 0.0;
        root["estimated_liquidity_reward_pnl"] = 0.0;
        root["inventory"] = std::move(inventory);
        atomic_write(run_root_ / "micro_maker" / "state.json", json::serialize(root) + "\n");
    }

    void write_latency_header_if_needed() {
        const fs::path path = run_root_ / "micro_maker" / "latency.csv";
        if (!fs::exists(path)) {
            atomic_write(path, "recorded_ts_ms,record_kind,market_handle,instrument_handle,state_version,parse_ns,book_ns,feature_ns,decision_ns,risk_ns,tx_queue_ns,execution_ns,receive_to_intent_ns\n");
        }
    }

private:
    struct InstrumentCold { MarketContext* market = nullptr; bool yes = false; };

    [[nodiscard]] const InstrumentCold* instrument(std::uint64_t handle) const noexcept {
        return handle < instruments_.size() && instruments_[handle].market != nullptr
            ? &instruments_[handle] : nullptr;
    }

    [[nodiscard]] std::int64_t exchange_ms(const TelemetryRecord& record) const noexcept {
        return std::max<std::int64_t>(1, record.exchange_event_ns / 1'000'000LL);
    }

    [[nodiscard]] std::int64_t decision_wall_ms(const TelemetryRecord& record,
                                                const StrategyIntent& intent) const noexcept {
        if (record.receive_wall_ms <= 0 || record.receive_monotonic_ns <= 0
            || intent.decision_monotonic_ns <= 0) return std::max<std::int64_t>(1, record.receive_wall_ms);
        const auto delta = std::max<std::int64_t>(0, intent.decision_monotonic_ns - record.receive_monotonic_ns);
        return record.receive_wall_ms + delta / 1'000'000LL;
    }

    [[nodiscard]] std::string snapshot_id(const TelemetryRecord& record) const {
        return "mm-book-" + std::to_string(record.instrument_handle) + "-" +
               std::to_string(record.state_version);
    }

    [[nodiscard]] std::string candidate_id(std::uint64_t intent) const {
        return "mmc-" + telemetry_epoch_ + "-" + hex64(intent);
    }

    [[nodiscard]] std::string order_id(std::uint64_t market, std::uint64_t order) const {
        return "mmo-" + std::to_string(market) + "-" + telemetry_epoch_ + "-" +
               std::to_string(order);
    }

    [[nodiscard]] json::object common(const char* event_type) {
        json::object event;
        event["schema_version"] = 1;
        event["event_type"] = event_type;
        event["strategy"] = kStrategy;
        event["model_sha"] = model_sha_;
        event["paper_only"] = true;
        event["authenticated_execution"] = false;
        event["record_id"] = "cpp-mm-" + telemetry_epoch_ + "-" +
                             std::to_string(++record_sequence_);
        event["recorded_ts_ms"] = wall_ms();
        return event;
    }

    void attach_market(json::object& event, const TelemetryRecord& record) const {
        const auto* cold = instrument(record.instrument_handle);
        const auto* market = cold != nullptr ? cold->market : markets_[record.market_handle];
        if (market == nullptr) return;
        event["market_id"] = market->cold.market_id;
        if (!market->cold.event_id.empty()) event["event_id"] = market->cold.event_id;
        if (cold != nullptr) event["token_id"] = cold->yes ? market->cold.yes_token : market->cold.no_token;
    }

    void attach_book(json::object& event, const TelemetryRecord& record) const {
        event["bid"] = e4_price(record.book.best_bid_e4);
        event["ask"] = e4_price(record.book.best_ask_e4);
        event["bid_depth"] = micro_shares(record.book.bid_depth.l5_microunits);
        event["ask_depth"] = micro_shares(record.book.ask_depth.l5_microunits);
        event["book_snapshot_id"] = snapshot_id(record);
    }

    void attach_metadata(json::object& event, const TelemetryRecord& record) const {
        json::object metadata;
        metadata["outcome"] = record.outcome_yes ? "YES" : "NO";
        metadata["action"] = action_name(record.action);
        metadata["execution_side"] = side_name(record.intent.side);
        metadata["maker_cpp_hot_path"] = true;
        metadata["queue_operational_scenario"] = "pessimistic";
        metadata["state_version"] = record.state_version;
        metadata["decision_reason"] = static_cast<std::uint64_t>(record.decision.reason);
        metadata["toxicity"] = record.decision.toxicity;
        metadata["robust_ev"] = record.decision.robust_ev;
        metadata["ev_uncertainty"] = record.decision.ev_uncertainty;
        event["metadata"] = std::move(metadata);
    }

    void spool(json::object event) {
        const std::int64_t recorded = integer(find_value(event, "recorded_ts_ms"), wall_ms());
        const std::string record = text(find_value(event, "record_id"));
        const fs::path target = run_root_ / "ledger" / "spool" /
            (std::to_string(recorded) + "." + record + ".json");
        atomic_write(target, json::serialize(event) + "\n");
    }

    void write_candidate(const TelemetryRecord& record) {
        if (record.intent.type != IntentType::Quote || record.book.valid == 0) return;
        auto event = common("CANDIDATE");
        attach_market(event, record);
        attach_book(event, record);
        event["candidate_id"] = candidate_id(record.intent.intent_id);
        event["model_version"] = std::to_string(record.intent.model_version);
        event["exchange_ts_ms"] = exchange_ms(record);
        event["receive_ts_ms"] = record.receive_wall_ms;
        event["decision_ts_ms"] = decision_wall_ms(record, record.intent);
        event["side"] = side_name(record.intent.side);
        event["limit_price"] = tick_price(record.intent.price_tick, record.tick_size_e4);
        event["intended_action"] = action_name(record.action);
        event["intended_size"] = micro_shares(record.intent.quantity_microunits);
        event["predicted_alpha"] = record.intent.expected_edge;
        event["expected_ev"] = record.intent.expected_ev;
        event["capital_cost"] = 0.0;
        event["latency_cost"] = 0.0;
        attach_metadata(event, record);
        spool(std::move(event));
        append_latency(record);
    }

    void append_latency(const TelemetryRecord& record) {
        write_latency_header_if_needed();
        std::ofstream out(run_root_ / "micro_maker" / "latency.csv", std::ios::app);
        out << wall_ms() << ','
            << (record.kind == TelemetryKind::Candidate ? "candidate" : "paper_execution") << ','
            << record.market_handle << ',' << record.instrument_handle << ','
            << record.state_version << ',' << record.decision.latency.parse_ns << ','
            << record.decision.latency.book_ns << ',' << record.decision.latency.feature_ns << ','
            << record.decision.latency.decision_ns << ',' << record.decision.latency.risk_ns << ','
            << record.decision.latency.tx_queue_ns << ',' << record.decision.latency.execution_ns << ','
            << record.decision.latency.receive_to_intent_ns << '\n';
    }

    void write_paper(const TelemetryRecord& record) {
        const auto& paper = record.paper;
        if (paper.kind == PaperMakerEventKind::OrderLive) {
            append_latency(record);
            auto event = common("ORDER_SUBMITTED");
            attach_market(event, record);
            attach_book(event, record);
            event["candidate_id"] = candidate_id(paper.intent_id);
            event["order_id"] = order_id(record.market_handle, paper.order_id);
            event["model_version"] = std::to_string(record.intent.model_version);
            event["exchange_ts_ms"] = exchange_ms(record);
            event["receive_ts_ms"] = record.receive_wall_ms;
            event["decision_ts_ms"] = decision_wall_ms(record, record.intent);
            event["side"] = side_name(paper.side);
            event["limit_price"] = tick_price(paper.price_tick, paper.tick_size_e4);
            event["intended_action"] = action_name(record.action);
            event["intended_size"] = micro_shares(paper.original_microunits);
            event["queue_ahead"] = micro_shares(paper.queue.ahead_lower_microunits);
            event["expected_ev"] = record.intent.expected_ev;
            attach_metadata(event, record);
            spool(std::move(event));
            write_order_state(record, "LIVE");
            return;
        }
        if (paper.kind == PaperMakerEventKind::CancelRequested) {
            append_latency(record);
            write_order_state(record, "CANCEL_PENDING");
            return;
        }
        if (paper.kind == PaperMakerEventKind::Cancelled) {
            write_order_state(record, "CANCELLED");
            return;
        }
        if (paper.kind == PaperMakerEventKind::Fill && paper.operational_fill_microunits > 0) {
            write_fill(record);
            if (paper.order_state == pm::v7::OrderState::Filled) write_order_state(record, "FILLED");
            else if (paper.order_state == pm::v7::OrderState::Partial) write_order_state(record, "PARTIAL");
            return;
        }
        if (paper.kind == PaperMakerEventKind::FinalMerge) {
            auto event = common("FINAL");
            const auto* market = markets_[record.market_handle];
            if (market != nullptr) {
                event["market_id"] = market->cold.market_id;
                if (!market->cold.event_id.empty()) event["event_id"] = market->cold.event_id;
            }
            event["position_id"] = "mmm-" + std::to_string(record.market_handle) + "-" +
                                   std::to_string(++merge_sequence_);
            event["final_pnl"] = paper.realized_pnl;
            event["realized_cashflow"] = paper.realized_pnl;
            json::object metadata;
            metadata["complete_set_merge"] = true;
            metadata["merged_shares"] = micro_shares(paper.operational_fill_microunits);
            metadata["maker_cpp_hot_path"] = true;
            event["metadata"] = std::move(metadata);
            spool(std::move(event));
        }
    }

    void write_order_state(const TelemetryRecord& record, const char* state) {
        if (record.paper.order_id == 0) return;
        auto event = common("ORDER_STATE");
        attach_market(event, record);
        event["order_id"] = order_id(record.market_handle, record.paper.order_id);
        event["order_state"] = state;
        event["side"] = side_name(record.paper.side);
        json::object metadata;
        metadata["maker_cpp_hot_path"] = true;
        metadata["oms_state"] = order_state_name(record.paper.order_state);
        metadata["decision_reason"] = decision_reason_name(record.decision.reason);
        metadata["decision_reason_code"] = static_cast<std::uint64_t>(record.decision.reason);
        metadata["safety_preemption"] = safety_preemption(record.decision.reason);
        event["metadata"] = std::move(metadata);
        spool(std::move(event));
    }

    void write_fill(const TelemetryRecord& record) {
        const auto& paper = record.paper;
        auto event = common("FILL");
        attach_market(event, record);
        event["order_id"] = order_id(record.market_handle, paper.order_id);
        event["fill_id"] = "mmf-" + std::to_string(record.market_handle) + "-" +
                           telemetry_epoch_ + "-" + std::to_string(paper.order_id) + "-" +
                           hex64(paper.trade_id);
        event["exchange_ts_ms"] = exchange_ms(record);
        event["receive_ts_ms"] = record.receive_wall_ms;
        event["side"] = side_name(paper.side);
        event["fill_price"] = tick_price(paper.price_tick, paper.tick_size_e4);
        event["filled_size"] = micro_shares(paper.operational_fill_microunits);
        event["complete"] = paper.order_state == pm::v7::OrderState::Filled;
        event["fee"] = 0.0;
        event["fee_rate"] = 0.0;
        event["fee_source"] = "polymarket:maker_fee_zero";
        json::object metadata;
        metadata["maker_cpp_hot_path"] = true;
        metadata["public_trade_id"] = std::to_string(paper.trade_id);
        metadata["queue_ahead_lower"] = micro_shares(paper.queue.ahead_lower_microunits);
        metadata["queue_ahead_expected"] = micro_shares(paper.queue.ahead_expected_microunits);
        metadata["queue_ahead_upper"] = micro_shares(paper.queue.ahead_upper_microunits);
        metadata["queue_confidence"] = paper.queue.confidence;
        metadata["pessimistic_fill"] = micro_shares(paper.pessimistic_fill_microunits);
        metadata["expected_fill"] = micro_shares(paper.expected_fill_microunits);
        metadata["optimistic_fill"] = micro_shares(paper.optimistic_fill_microunits);
        event["metadata"] = std::move(metadata);
        spool(std::move(event));
    }

    fs::path run_root_;
    std::string model_sha_;
    double starting_capital_ = 0.0;
    std::vector<MarketContext*> markets_;
    std::vector<InstrumentCold> instruments_;
    std::vector<PaperMakerInventory> inventory_;
    // The supervisor can restart the C++ process without rotating the exact-SHA
    // ledger.  Sequence counters restart at one, so every process boot needs a
    // distinct causal namespace or the canonical spool correctly rejects all
    // subsequent telemetry as duplicate record/order IDs.
    std::string telemetry_epoch_;
    std::uint64_t record_sequence_ = 0;
    std::uint64_t merge_sequence_ = 0;
};

std::vector<std::unique_ptr<MarketContext>> build_markets(
    const Options& options, const pm::Config& config) {
    std::vector<SelectedMarket> selected;
    while (!g_stop.load(std::memory_order_relaxed)) {
        try {
            if (fs::exists(options.selection) && fs::file_size(options.selection) > 0) {
                selected = load_selection(options.selection);
                break;
            }
        } catch (const std::exception& error) {
            std::cerr << "maker selection not ready: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    if (selected.empty()) throw std::runtime_error("maker selection unavailable");

    std::vector<std::string> tokens;
    for (const auto& market : selected) {
        tokens.push_back(market.yes_token);
        tokens.push_back(market.no_token);
    }
    pm::PolymarketApi api(config);
    std::unordered_map<std::string, pm::Book> books;
    while (!g_stop.load(std::memory_order_relaxed)) {
        try {
            books = api.fetch_books(tokens);
            if (books.size() >= tokens.size()) break;
        } catch (const std::exception& error) {
            std::cerr << "maker cold-start books not ready: " << error.what() << '\n';
        }
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    if (books.empty()) throw std::runtime_error("maker cold-start books unavailable");

    std::vector<std::unique_ptr<MarketContext>> output;
    std::uint64_t market_handle = 0;
    std::uint64_t instrument_handle = 0;
    for (auto& selected_market : selected) {
        const auto yes = books.find(selected_market.yes_token);
        const auto no = books.find(selected_market.no_token);
        if (yes == books.end() || no == books.end()) continue;
        const std::int32_t yes_tick = tick_e4(yes->second.tick_size);
        const std::int32_t no_tick = tick_e4(no->second.tick_size);
        // One binary market must use one execution tick contract in the common
        // policy. Mixed token ticks fail closed instead of being silently coerced.
        if (yes_tick <= 0 || no_tick <= 0 || yes_tick != no_tick) continue;
        const auto market = ++market_handle;
        const auto event = market;
        const auto yes_instrument = ++instrument_handle;
        const auto no_instrument = ++instrument_handle;
        output.push_back(std::make_unique<MarketContext>(
            std::move(selected_market), market, event, yes_instrument, no_instrument,
            yes_tick, no_tick, std::max(yes->second.min_order_size, 1e-6),
            std::max(no->second.min_order_size, 1e-6)));
    }
    if (output.empty()) throw std::runtime_error("no selected maker market has valid cold-start books");
    return output;
}

void write_kill(const fs::path& run_root, std::string_view model_sha,
                std::string_view reason) {
    json::object event;
    event["schema"] = "polymarket_v7_maker_cpp_failure_v1";
    event["timestamp_ms"] = wall_ms();
    event["paper_only"] = true;
    event["authenticated_execution"] = false;
    event["model_sha"] = model_sha;
    event["reason"] = reason;
    atomic_write(run_root / "control" / "KILL", json::serialize(event) + "\n");
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::signal(SIGINT, signal_handler);
        std::signal(SIGTERM, signal_handler);
        const Options options = parse_options(argc, argv);
        const pm::Config config = pm::load_config(options.config);
        if (config.starting_capital <= 0.0) throw std::runtime_error("maker sleeve starting capital must be positive");
        const PaperMakerPolicy paper_policy = load_paper_policy(options.maker_policy);
        auto initial = std::make_shared<MakerModelSnapshot>(
            load_model_snapshot(options.maker_policy, options.model, options.model_sha));
        auto model_store = std::make_shared<MakerModelStore>(initial);
        auto markets = build_markets(options, config);

        std::atomic<bool> global_kill{false};
        std::atomic<bool> new_risk_frozen{false};
        std::atomic<bool> fatal{false};
        auto execution = std::make_unique<ExecutionCore>();
        if (!execution->initialize(markets, paper_policy, config.starting_capital,
                                   initial->min_quote_lifetime_ns,
                                   initial->exploration_concurrent_market_cap)) {
            throw std::runtime_error("cannot initialize single V7 Maker execution owner");
        }

        std::vector<std::unique_ptr<ShardRuntime>> shards;
        for (std::size_t start = 0; start < markets.size(); start += kMarketsPerShard) {
            std::vector<MarketContext*> group;
            for (std::size_t i = start; i < std::min(markets.size(), start + kMarketsPerShard); ++i) {
                group.push_back(markets[i].get());
            }
            auto shard = std::make_unique<ShardRuntime>(
                std::move(group), model_store, &global_kill, &new_risk_frozen,
                &fatal, options.ws_url);
            // Max quote dollars before per-event price conversion. The allocation
            // child config is already sleeve-bounded by v7_capital_allocator.py.
            shard->set_starting_capital_fraction(config.starting_capital * 0.01);
            for (std::size_t i = start; i < std::min(markets.size(), start + kMarketsPerShard); ++i) {
                shard->seed_execution_snapshot(execution->snapshot(markets[i]->market_handle));
            }
            shards.push_back(std::move(shard));
        }

        std::atomic<bool> execution_stop{false};
        std::thread execution_thread([&]() {
            std::size_t critical_cursor = 0;
            std::size_t normal_cursor = 0;
            auto pending = [&]() noexcept {
                for (const auto& shard : shards) {
                    if (shard->critical_backlog() != 0 || shard->normal_backlog() != 0) return true;
                }
                return false;
            };
            std::uint32_t idle_iterations = 0;
            while (!execution_stop.load(std::memory_order_acquire) || pending()) {
                ExecutionCommand command;
                std::size_t source_shard = 0;
                bool have = false;

                // Risk-off/market-state commands are globally prioritized over
                // new quote placements while preserving round-robin shard fairness.
                for (std::size_t step = 0; step < shards.size(); ++step) {
                    const std::size_t index = (critical_cursor + step) % shards.size();
                    if (shards[index]->pop_critical(command)) {
                        source_shard = index;
                        critical_cursor = (index + 1) % shards.size();
                        have = true;
                        break;
                    }
                }
                if (!have) {
                    for (std::size_t step = 0; step < shards.size(); ++step) {
                        const std::size_t index = (normal_cursor + step) % shards.size();
                        if (shards[index]->pop_normal(command)) {
                            source_shard = index;
                            normal_cursor = (index + 1) % shards.size();
                            have = true;
                            break;
                        }
                    }
                }
                if (!have) {
                    // Avoid a fixed 50 us tax on a newly enqueued cancel/order.
                    // The isolated order-TX owner spins briefly, then yields,
                    // and only sleeps after a prolonged idle period.
                    ++idle_iterations;
                    if (idle_iterations < 256) {
                        std::atomic_signal_fence(std::memory_order_seq_cst);
                    } else if (idle_iterations < 1024) {
                        std::this_thread::yield();
                    } else {
                        std::this_thread::sleep_for(std::chrono::microseconds(5));
                        idle_iterations = 0;
                    }
                    continue;
                }
                idle_iterations = 0;

                if (!execution->process(command)) {
                    fatal.store(true, std::memory_order_release);
                    continue;
                }
                const auto update = execution->snapshot(command.context.market_handle);
                if (update.market_handle != 0
                    && !shards[source_shard]->push_execution_snapshot(update)) {
                    fatal.store(true, std::memory_order_release);
                }
                if (execution->telemetry_overflow()) {
                    fatal.store(true, std::memory_order_release);
                }
            }
        });

        TelemetryWriter writer(options.run_root, options.model_sha,
                               config.starting_capital, markets);
        writer.write_state();
        for (auto& shard : shards) shard->start();

        std::int64_t last_state_ms = 0;
        std::int64_t last_model_check_ms = 0;
        std::uint64_t current_model_version = initial->model_version;
        const fs::path kill_path = fs::path(options.run_root) / "control" / "KILL";
        const fs::path maker_freeze_path =
            fs::path(options.run_root) / "control" / "MAKER_FREEZE";
        const fs::path cutover_drain_path =
            fs::path(options.run_root) / "control" / "CUTOVER_DRAIN";
        std::int64_t last_control_check_ms = 0;
        while (!g_stop.load(std::memory_order_relaxed)) {
            if (fs::exists(kill_path)) {
                global_kill.store(true, std::memory_order_release);
                break;
            }
            if (fatal.load(std::memory_order_acquire)) {
                global_kill.store(true, std::memory_order_release);
                write_kill(options.run_root, options.model_sha, "maker_cpp_order_tx_or_telemetry_invariant_failure");
                break;
            }

            try {
                TelemetryRecord record;
                for (auto& shard : shards) {
                    while (shard->pop(record)) writer.consume(record);
                }
                while (execution->pop_telemetry(record)) writer.consume(record);

                const auto now = wall_ms();
                if (now - last_control_check_ms >= 100) {
                    // Filesystem access stays on this cold control thread. The
                    // hot shards only read the resulting atomic flag.
                    new_risk_frozen.store(
                        fs::exists(maker_freeze_path) || fs::exists(cutover_drain_path),
                        std::memory_order_release);
                    last_control_check_ms = now;
                }
                if (now - last_state_ms >= 1000) {
                    writer.write_state();
                    write_runtime_diagnostics(options.run_root, options.model_sha, shards);
                    last_state_ms = now;
                }
                if (now - last_model_check_ms >= 2000) {
                    try {
                        auto next = std::make_shared<MakerModelSnapshot>(
                            load_model_snapshot(options.maker_policy, options.model, options.model_sha));
                        if (next->model_version != current_model_version && model_store->publish(next)) {
                            current_model_version = next->model_version;
                        }
                    } catch (...) {
                        // Preserve the last valid immutable champion snapshot.
                    }
                    last_model_check_ms = now;
                }
            } catch (...) {
                fatal.store(true, std::memory_order_release);
                continue;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }

        global_kill.store(true, std::memory_order_release);
        for (auto& shard : shards) shard->stop();
        execution_stop.store(true, std::memory_order_release);
        if (execution_thread.joinable()) execution_thread.join();

        TelemetryRecord record;
        for (auto& shard : shards) {
            while (shard->pop(record)) writer.consume(record);
        }
        while (execution->pop_telemetry(record)) writer.consume(record);
        writer.write_state();
        return fatal.load(std::memory_order_relaxed) ? 2 : 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_market_maker_runtime: " << error.what() << '\n';
        return 2;
    }
}
