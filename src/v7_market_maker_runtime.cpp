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
#include <unordered_set>
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

[[nodiscard]] const char* execution_outcome_name(
    pm::v7::maker::PaperExecutionOutcome outcome) noexcept {
    using Outcome = pm::v7::maker::PaperExecutionOutcome;
    switch (outcome) {
        case Outcome::Pending: return "PENDING";
        case Outcome::Filled: return "FILLED";
        case Outcome::PartialFill: return "PARTIAL_FILL";
        case Outcome::NoOppositeFlow: return "NO_OPPOSITE_FLOW";
        case Outcome::PriceNotReached: return "PRICE_NOT_REACHED";
        case Outcome::QueueNotDepleted: return "QUEUE_NOT_DEPLETED";
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
    double yes_aggressive_buy_shares_per_second = 0.0;
    double yes_aggressive_sell_shares_per_second = 0.0;
    double no_aggressive_buy_shares_per_second = 0.0;
    double no_aggressive_sell_shares_per_second = 0.0;
    double yes_aggressive_buy_prints_per_second = 0.0;
    double yes_aggressive_sell_prints_per_second = 0.0;
    double no_aggressive_buy_prints_per_second = 0.0;
    double no_aggressive_sell_prints_per_second = 0.0;
    std::uint8_t yes_execution_authority_mask = 0;
    std::uint8_t no_execution_authority_mask = 0;
    std::array<double, pm::v7::maker::kSelectorAuthorityCellCount>
        yes_projected_flow_reach_probability{};
    std::array<double, pm::v7::maker::kSelectorAuthorityCellCount>
        yes_projected_queue_depletion_probability{};
    std::array<double, pm::v7::maker::kSelectorAuthorityCellCount>
        yes_projected_fill_probability{};
    std::array<double, pm::v7::maker::kSelectorAuthorityCellCount>
        no_projected_flow_reach_probability{};
    std::array<double, pm::v7::maker::kSelectorAuthorityCellCount>
        no_projected_queue_depletion_probability{};
    std::array<double, pm::v7::maker::kSelectorAuthorityCellCount>
        no_projected_fill_probability{};
    bool flow_priors_present = false;
    bool selector_authority_required = false;
    bool inventory_seed_authorized = false;
    std::uint64_t selector_generation = 0;
};

struct InventoryFactoryPolicy {
    std::uint8_t enabled = 0;
    double bootstrap_complete_set_fraction = 0.10;
    double seed_min_quote_multiples = 2.0;
    // Complete-set collateral is capital, even though the pair is directionally
    // neutral. Dynamic SELL authority must not let a large venue minimum bypass
    // the evidence-collection budget for one market.
    double seed_max_market_fraction = 0.05;
    double seed_visible_depth_fraction = 0.10;
    double replenish_trigger_fraction = 0.50;
    double max_market_gross_fraction = 0.05;
    double max_maker_gross_fraction = 0.30;
    std::int64_t replenish_cooldown_ns = 60'000'000'000LL;

    [[nodiscard]] bool valid() const noexcept {
        return enabled <= 1
            && std::isfinite(bootstrap_complete_set_fraction)
            && bootstrap_complete_set_fraction > 0.0
            && bootstrap_complete_set_fraction <= max_maker_gross_fraction
            && std::isfinite(seed_min_quote_multiples)
            && seed_min_quote_multiples >= 1.0
            && std::isfinite(seed_max_market_fraction)
            && seed_max_market_fraction > 0.0
            && seed_max_market_fraction <= max_market_gross_fraction
            && std::isfinite(seed_visible_depth_fraction)
            && seed_visible_depth_fraction > 0.0
            && seed_visible_depth_fraction <= 1.0
            && std::isfinite(replenish_trigger_fraction)
            && replenish_trigger_fraction > 0.0
            && replenish_trigger_fraction < 1.0
            && std::isfinite(max_market_gross_fraction)
            && max_market_gross_fraction > 0.0
            && max_market_gross_fraction <= 1.0
            && std::isfinite(max_maker_gross_fraction)
            && max_maker_gross_fraction > 0.0
            && max_maker_gross_fraction <= 1.0
            && replenish_cooldown_ns >= 0;
    }
};

struct ExternalMakerFairPolicy {
    bool enabled = false;
    bool require_model_mature = true;
    bool require_verified_contract = true;
    bool require_positive_2x_cost_stress = true;
    std::string status_path = "runs/paper_v7_live/external_fair/status.json";
    std::int64_t maximum_snapshot_age_ns = 250'000'000LL;
    double maximum_interval_width = 0.20;
    double external_directional_weight = 1.0;
    double local_directional_weight = 0.0;

    [[nodiscard]] bool valid() const noexcept {
        return maximum_snapshot_age_ns > 0
            && std::isfinite(maximum_interval_width)
            && maximum_interval_width > 0.0 && maximum_interval_width < 1.0
            && std::isfinite(external_directional_weight)
            && external_directional_weight > 0.0
            && std::isfinite(local_directional_weight)
            && local_directional_weight >= 0.0;
    }
};

ExternalMakerFairPolicy load_external_maker_fair_policy(const fs::path& policy_path) {
    ExternalMakerFairPolicy out;
    const auto root = read_json(policy_path);
    if (!root.is_object()) throw std::runtime_error("maker policy must be object");
    const auto* external = child_object(root.as_object(), "settlement_aware_external_fair");
    if (external == nullptr) return out;
    out.enabled = boolean(find_value(*external, "enabled_for_paper_quotes"), false);
    out.require_model_mature = boolean(
        find_value(*external, "require_model_mature"), true);
    out.require_verified_contract = boolean(
        find_value(*external, "require_verified_contract_binding"), true);
    out.require_positive_2x_cost_stress = boolean(
        find_value(*external, "require_positive_2x_cost_stress"), true);
    const std::string configured_path = text(find_value(*external, "status_path"));
    if (!configured_path.empty()) out.status_path = configured_path;
    out.maximum_snapshot_age_ns = std::max<std::int64_t>(1, object_integer(
        external, "maximum_snapshot_age_ms", 250)) * 1'000'000LL;
    out.maximum_interval_width = object_number(
        external, "maximum_interval_width", out.maximum_interval_width);
    out.external_directional_weight = object_number(
        external, "external_directional_weight", out.external_directional_weight);
    out.local_directional_weight = object_number(
        external, "local_directional_weight", out.local_directional_weight);
    if (boolean(find_value(*external, "automatic_promotion"), true)
        || boolean(find_value(*external, "real_money_authority"), true)
        || !boolean(find_value(*external, "bid_uses_lower_bound"), false)
        || !boolean(find_value(*external, "ask_uses_upper_bound"), false)
        || !out.valid()) {
        throw std::runtime_error("unsafe settlement-aware external maker policy");
    }
    return out;
}

InventoryFactoryPolicy load_inventory_factory_policy(const fs::path& policy_path) {
    InventoryFactoryPolicy out;
    const auto root = read_json(policy_path);
    if (!root.is_object()) throw std::runtime_error("maker policy must be object");
    const auto* inventory = child_object(root.as_object(), "inventory");
    if (inventory == nullptr) throw std::runtime_error("maker inventory policy missing");
    out.enabled = static_cast<std::uint8_t>(
        boolean(find_value(*inventory, "auto_split_merge_accounting"), false)
        && boolean(find_value(*inventory, "inventory_factory_enabled"), false));
    out.bootstrap_complete_set_fraction = object_number(
        inventory, "bootstrap_complete_set_fraction", out.bootstrap_complete_set_fraction);
    out.seed_min_quote_multiples = object_number(
        inventory, "seed_min_quote_multiples", out.seed_min_quote_multiples);
    out.seed_max_market_fraction = object_number(
        inventory, "seed_max_market_fraction", out.seed_max_market_fraction);
    out.seed_visible_depth_fraction = object_number(
        inventory, "seed_visible_depth_fraction", out.seed_visible_depth_fraction);
    out.replenish_trigger_fraction = object_number(
        inventory, "replenish_trigger_fraction", out.replenish_trigger_fraction);
    out.max_market_gross_fraction = object_number(
        inventory, "max_market_gross_fraction", out.max_market_gross_fraction);
    out.max_maker_gross_fraction = object_number(
        inventory, "max_maker_gross_fraction", out.max_maker_gross_fraction);
    out.replenish_cooldown_ns = object_integer(
        inventory, "replenish_cooldown_seconds", 60) * 1'000'000'000LL;
    if (!out.valid()) throw std::runtime_error("invalid maker inventory factory policy");
    return out;
}

[[nodiscard]] std::vector<SelectedMarket> load_selection(
    const fs::path& path, std::string_view expected_model_sha) {
    const auto root = read_json(path);
    if (!root.is_object()) throw std::runtime_error("maker selection must be object");
    const auto& object = root.as_object();
    if (const auto* safety = find_value(object, "paper_only"); safety != nullptr && !boolean(safety, false)) {
        throw std::runtime_error("maker selection is not PAPER-only");
    }
    if (const auto* auth = find_value(object, "authenticated_execution"); auth != nullptr && boolean(auth, true)) {
        throw std::runtime_error("maker selection enables authenticated execution");
    }
    if (text(find_value(object, "model_sha")) != expected_model_sha) {
        throw std::runtime_error("maker selection model SHA mismatch");
    }
    if (!boolean(find_value(object, "execution_cell_authority_required"), false)
        || text(find_value(object, "execution_authority_semantics"))
            != "token_action_side_v2") {
        throw std::runtime_error("maker selection lacks exact execution-cell authority");
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
    const auto selector_generation = static_cast<std::uint64_t>(
        std::max<std::int64_t>(1, integer(find_value(object, "timestamp_ms"), 0)));
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
        market.selector_authority_required = true;
        market.inventory_seed_authorized = boolean(
            find_value(row, "inventory_seed_authorized"), false);
        market.selector_generation = selector_generation;
        const auto* authority_cells = find_value(row, "authorized_execution_cells");
        if (authority_cells == nullptr || !authority_cells->is_array()) {
            throw std::runtime_error("maker market lacks execution-cell authority array");
        }
        for (const auto& cell_value : authority_cells->as_array()) {
            if (!cell_value.is_object()) continue;
            const auto& cell = cell_value.as_object();
            const std::string token = text(find_value(cell, "token_id"));
            const std::string action_name_value = text(find_value(cell, "action"));
            const std::string quote_side = text(find_value(cell, "quote_side"));
            Action action = Action::Withdraw;
            if (action_name_value == "JOIN") action = Action::Join;
            else if (action_name_value == "IMPROVE1") action = Action::Improve1;
            else if (action_name_value == "FADE1") action = Action::Fade1;
            else if (action_name_value == "FADE2") action = Action::Fade2;
            const Side side = quote_side == "BUY" ? Side::Buy
                : quote_side == "SELL" ? Side::Sell : Side::None;
            const auto bit = pm::v7::maker::execution_authority_bit(action, side);
            if (bit == 0) continue;
            const auto index = pm::v7::maker::execution_authority_index(action, side);
            if (index >= pm::v7::maker::kSelectorAuthorityCellCount) continue;
            const double projected_reach = std::clamp(number(
                find_value(cell, "projected_flow_reach_probability"), 0.0), 0.0, 1.0);
            const double projected_queue = std::clamp(number(
                find_value(cell, "projected_queue_depletion_probability"), 0.0), 0.0, 1.0);
            const double projected_fill = std::clamp(number(
                find_value(cell, "projected_fill_probability"), 0.0), 0.0, 1.0);
            if (token == market.yes_token) {
                market.yes_execution_authority_mask |= bit;
                market.yes_projected_flow_reach_probability[index] = projected_reach;
                market.yes_projected_queue_depletion_probability[index] = projected_queue;
                market.yes_projected_fill_probability[index] = projected_fill;
            } else if (token == market.no_token) {
                market.no_execution_authority_mask |= bit;
                market.no_projected_flow_reach_probability[index] = projected_reach;
                market.no_projected_queue_depletion_probability[index] = projected_queue;
                market.no_projected_fill_probability[index] = projected_fill;
            }
        }
        bool sell_authorized = false;
        for (const auto action : {Action::Join, Action::Improve1,
                                  Action::Fade1, Action::Fade2}) {
            const auto bit = pm::v7::maker::execution_authority_bit(
                action, Side::Sell);
            sell_authorized = sell_authorized
                || (market.yes_execution_authority_mask & bit) != 0
                || (market.no_execution_authority_mask & bit) != 0;
        }
        if (market.inventory_seed_authorized != sell_authorized) {
            throw std::runtime_error(
                "maker inventory seed authority does not match SELL cells");
        }
        const auto* opportunities = find_value(row, "quote_opportunities");
        if (opportunities != nullptr && opportunities->is_array()) {
            for (const auto& opportunity_value : opportunities->as_array()) {
                if (!opportunity_value.is_object()) continue;
                const auto& opportunity = opportunity_value.as_object();
                const std::string token = text(find_value(opportunity, "token_id"));
                const std::string aggressor = text(find_value(
                    opportunity, "required_aggressor_side"));
                const double shares_10m = std::max(0.0, number(
                    find_value(opportunity, "opposite_shares_10m"), 0.0));
                const double prints_10m = std::max(0.0, number(
                    find_value(opportunity, "opposite_prints_10m"), 0.0));
                const double prints_30s = std::max(0.0, number(
                    find_value(opportunity, "opposite_prints_30s"), 0.0));
                const double age_ms = number(
                    find_value(opportunity, "last_opposite_flow_age_ms"), -1.0);
                double rate = 0.0;
                double print_rate = 0.0;
                if (age_ms >= 0.0 && shares_10m > 0.0 && prints_10m > 0.0) {
                    const double long_rate = shares_10m / 600.0;
                    const double recent_rate = (shares_10m / prints_10m)
                                             * prints_30s / 30.0;
                    // The short-window burst estimate earns selection, but an
                    // old last print must not retain quote authority forever.
                    const double freshness = std::exp(
                        -std::log(2.0) * age_ms / 30'000.0);
                    rate = std::max(long_rate, recent_rate) * freshness;
                    print_rate = std::max(
                        prints_10m / 600.0, prints_30s / 30.0) * freshness;
                }
                double* destination = nullptr;
                double* print_destination = nullptr;
                if (token == market.yes_token && aggressor == "BUY") {
                    destination = &market.yes_aggressive_buy_shares_per_second;
                    print_destination = &market.yes_aggressive_buy_prints_per_second;
                } else if (token == market.yes_token && aggressor == "SELL") {
                    destination = &market.yes_aggressive_sell_shares_per_second;
                    print_destination = &market.yes_aggressive_sell_prints_per_second;
                } else if (token == market.no_token && aggressor == "BUY") {
                    destination = &market.no_aggressive_buy_shares_per_second;
                    print_destination = &market.no_aggressive_buy_prints_per_second;
                } else if (token == market.no_token && aggressor == "SELL") {
                    destination = &market.no_aggressive_sell_shares_per_second;
                    print_destination = &market.no_aggressive_sell_prints_per_second;
                }
                if (destination != nullptr) *destination = std::max(*destination, rate);
                if (print_destination != nullptr) {
                    *print_destination = std::max(*print_destination, print_rate);
                }
            }
        }
        market.flow_priors_present =
            market.yes_aggressive_buy_shares_per_second > 0.0
            || market.yes_aggressive_sell_shares_per_second > 0.0
            || market.no_aggressive_buy_shares_per_second > 0.0
            || market.no_aggressive_sell_shares_per_second > 0.0;
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
                                       const fs::path& config_path,
                                       const fs::path& model_path,
                                       std::string_view expected_sha) {
    MakerModelSnapshot model;
    const std::string policy_content = read_file(policy_path);
    const std::string config_content = read_file(config_path);
    boost::system::error_code policy_error;
    const auto policy_root = json::parse(policy_content, policy_error);
    if (policy_error) throw std::runtime_error("invalid maker policy json");
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
    model.fill_prediction_horizon_seconds = static_cast<double>(std::max<std::int64_t>(
        1, object_integer(quoting, "max_quote_lifetime_ms", 5'000))) / 1'000.0;
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
    model.exploration_max_market_fraction = exploration_market_fraction;
    model.exploration_max_active_markets = configured_market_capacity;
    model.exploration_concurrent_market_cap = exploration_market_cap;
    model.exploration_min_rest_ns = std::max<std::int64_t>(0, object_integer(
        exploration, "minimum_rest_ms", 0)) * 1'000'000LL;
    model.exploration_max_rest_ns = std::max<std::int64_t>(model.exploration_min_rest_ns,
        std::max<std::int64_t>(0, object_integer(exploration, "maximum_rest_ms", 0))
            * std::int64_t{1'000'000});
    model.exploration_persistent_fraction = std::clamp(object_number(
        exploration, "persistent_quote_fraction", 0.0), 0.0, 1.0);
    model.exploration_persistent_max_rest_ns = std::max<std::int64_t>(
        model.exploration_max_rest_ns,
        std::max<std::int64_t>(0, object_integer(
            exploration, "persistent_maximum_rest_ms",
            model.exploration_max_rest_ns / 1'000'000LL)) * std::int64_t{1'000'000});
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
                const std::string code_sha = text(find_value(fitted, "code_sha"));
                const std::string policy_hash = text(find_value(fitted, "policy_hash"));
                const std::string config_hash = text(find_value(fitted, "config_hash"));
                const std::string role = text(find_value(fitted, "artifact_role"));
                const std::string state = text(find_value(fitted, "model_state"));
                const bool safe = boolean(find_value(fitted, "paper_only"), false)
                    && !boolean(find_value(fitted, "authenticated_execution"), true)
                    && !boolean(find_value(fitted, "real_order_submission"), true);
                const bool compatible = safe && sha == expected_sha && code_sha == expected_sha
                    && policy_hash == hex64(fnv1a(policy_content))
                    && config_hash == hex64(fnv1a(config_content))
                    && role == "champion"
                    && boolean(find_value(fitted, "eligible_for_live_reload"), false)
                    && (state == "COLD_START" || state == "EVIDENCE_ACCUMULATING"
                        || state == "MATURE")
                    && text(find_value(fitted, "execution_semantics_version"))
                        == "maker-paper-v7.2-bilateral-inventory";
                const auto* groups_value = find_value(fitted, "groups");
                if (compatible && groups_value != nullptr && groups_value->is_object()) {
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
                    const auto baseline_fill_coefficients = model.fill_coefficients;
                    const auto baseline_markout_coefficients = model.markout_coefficients;
                    const auto* placement = child_object(fitted, "learned_placement_policy");
                    const auto* fill_coefficients = placement == nullptr ? nullptr
                        : find_value(*placement, "fill_coefficients");
                    const auto* markout_coefficients = placement == nullptr ? nullptr
                        : find_value(*placement, "markout_coefficients");
                    if (placement != nullptr
                        && boolean(find_value(*placement, "valid"), false)
                        && text(find_value(*placement, "state")) == "OOS_VALID"
                        && fill_coefficients != nullptr && fill_coefficients->is_array()
                        && markout_coefficients != nullptr && markout_coefficients->is_array()
                        && fill_coefficients->as_array().size() == model.fill_coefficients.size()
                        && markout_coefficients->as_array().size() == model.markout_coefficients.size()) {
                        bool coefficients_valid = true;
                        for (std::size_t index = 0; index < model.fill_coefficients.size(); ++index) {
                            const double fill_value = number(
                                &fill_coefficients->as_array()[index], std::numeric_limits<double>::quiet_NaN());
                            const double markout_value = number(
                                &markout_coefficients->as_array()[index], std::numeric_limits<double>::quiet_NaN());
                            coefficients_valid = coefficients_valid
                                && std::isfinite(fill_value) && std::abs(fill_value) <= 20.0
                                && std::isfinite(markout_value) && std::abs(markout_value) <= 1.0;
                            model.fill_coefficients[index] = fill_value;
                            model.markout_coefficients[index] = markout_value;
                        }
                        if (!coefficients_valid) {
                            model.fill_coefficients = baseline_fill_coefficients;
                            model.markout_coefficients = baseline_markout_coefficients;
                        }
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

struct ExternalMakerSnapshot {
    double yes = 0.5;
    double lower = 0.0;
    double upper = 1.0;
    std::int64_t calculated_ns = 0;
    std::int64_t valid_until_ns = 0;
    bool valid = false;
};

struct MarketContext {
    SelectedMarket cold;
    // Membership and handles are immutable for a process generation, while
    // selector priors and token/action/side authority refresh atomically.
    std::shared_ptr<const SelectedMarket> selection_control;
    std::uint64_t market_handle = 0;
    std::uint64_t event_handle = 0;
    std::uint64_t yes_instrument_handle = 0;
    std::uint64_t no_instrument_handle = 0;
    std::int32_t yes_tick_e4 = 0;
    std::int32_t no_tick_e4 = 0;
    double yes_min_order = 1.0;
    double no_min_order = 1.0;
    double cold_visible_depth_shares = 0.0;
    double cold_spread = 1.0;
    double cold_volatility_proxy = 1.0;
    double cold_yes_reference_price = 0.5;
    // Written only by the cold control plane and atomically published as one
    // immutable snapshot. Filesystem/JSON work never enters the event callback.
    std::shared_ptr<const ExternalMakerSnapshot> external_fair =
        std::make_shared<const ExternalMakerSnapshot>();
    MakerInstrumentLane yes_lane{+1};
    MakerInstrumentLane no_lane{-1};

    MarketContext(SelectedMarket selected, std::uint64_t market,
                  std::uint64_t event, std::uint64_t yes_instrument,
                  std::uint64_t no_instrument, std::int32_t yes_tick,
                  std::int32_t no_tick, double yes_min, double no_min,
                  double visible_depth, double spread, double volatility_proxy,
                  double yes_reference_price)
        : cold(std::move(selected)), market_handle(market), event_handle(event),
          yes_instrument_handle(yes_instrument), no_instrument_handle(no_instrument),
          yes_tick_e4(yes_tick), no_tick_e4(no_tick),
          yes_min_order(std::max(1e-6, yes_min)), no_min_order(std::max(1e-6, no_min)),
          cold_visible_depth_shares(std::max(0.0, visible_depth)),
          cold_spread(std::clamp(spread, 0.0, 1.0)),
          cold_volatility_proxy(std::clamp(volatility_proxy, 0.0, 1.0)),
          cold_yes_reference_price(std::clamp(yes_reference_price, 1e-6, 1.0 - 1e-6)) {
        selection_control = std::make_shared<const SelectedMarket>(cold);
    }
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
    std::uint64_t rejected_below_minimum_order = 0;
    std::uint64_t rejected_nonpositive_robust_ev = 0;
    std::uint64_t rejected_positive_point_ev = 0;
    std::int64_t best_rejected_robust_ev_nano = std::numeric_limits<std::int64_t>::min();
    std::int64_t best_rejected_point_ev_nano = std::numeric_limits<std::int64_t>::min();
    std::array<std::uint64_t, kDecisionReasonCount> reasons{};
    std::uint64_t raw_last_trade_events = 0;
    std::uint64_t valid_trade_prints = 0;
    std::uint64_t trade_missing_side = 0;
    std::uint64_t trade_missing_size = 0;
    std::uint64_t trade_invalid_quantity = 0;
    std::uint64_t trade_invalid_price = 0;
    std::uint64_t trade_invalid_timestamp = 0;
    std::uint64_t unknown_asset = 0;
};

struct MakerFillFunnelCounters {
    std::atomic<std::uint64_t> public_trades{0};
    std::atomic<std::uint64_t> duplicate_trade{0};
    std::atomic<std::uint64_t> active_orders_seen{0};
    std::atomic<std::uint64_t> causally_pre_arrival{0};
    std::atomic<std::uint64_t> wrong_aggressor_side{0};
    std::atomic<std::uint64_t> price_not_crossing{0};
    std::atomic<std::uint64_t> cancel_effective_before_trade{0};
    std::atomic<std::uint64_t> eligible_orders{0};
    std::atomic<std::uint64_t> queue_not_depleted{0};
    std::atomic<std::uint64_t> eligible_fill_microunits{0};
    std::atomic<std::uint64_t> operational_fill_microunits{0};
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
        out.rejected_below_minimum_order =
            rejected_below_minimum_order_.load(std::memory_order_relaxed);
        out.rejected_nonpositive_robust_ev =
            rejected_nonpositive_robust_ev_.load(std::memory_order_relaxed);
        out.rejected_positive_point_ev = rejected_positive_point_ev_.load(std::memory_order_relaxed);
        out.best_rejected_robust_ev_nano = best_rejected_robust_ev_nano_.load(std::memory_order_relaxed);
        out.best_rejected_point_ev_nano = best_rejected_point_ev_nano_.load(std::memory_order_relaxed);
        for (std::size_t i = 0; i < out.reasons.size(); ++i) {
            out.reasons[i] = decision_reasons_[i].load(std::memory_order_relaxed);
        }
        out.raw_last_trade_events = raw_last_trade_events_.load(std::memory_order_relaxed);
        out.valid_trade_prints = valid_trade_prints_.load(std::memory_order_relaxed);
        out.trade_missing_side = trade_missing_side_.load(std::memory_order_relaxed);
        out.trade_missing_size = trade_missing_size_.load(std::memory_order_relaxed);
        out.trade_invalid_quantity = trade_invalid_quantity_.load(std::memory_order_relaxed);
        out.trade_invalid_price = trade_invalid_price_.load(std::memory_order_relaxed);
        out.trade_invalid_timestamp = trade_invalid_timestamp_.load(std::memory_order_relaxed);
        out.unknown_asset = unknown_asset_.load(std::memory_order_relaxed);
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
        const auto control = std::atomic_load_explicit(
            &market.selection_control, std::memory_order_acquire);
        if (!control) {
            fatal_->store(true, std::memory_order_release);
            return;
        }
        context.flow_prior_valid = static_cast<std::uint8_t>(
            control->flow_priors_present);
        context.selector_authority_required = static_cast<std::uint8_t>(
            control->selector_authority_required);
        context.selector_generation = control->selector_generation;
        context.selector_execution_authority_mask = instrument.yes
            ? control->yes_execution_authority_mask
            : control->no_execution_authority_mask;
        context.selector_projected_flow_reach_probability = instrument.yes
            ? control->yes_projected_flow_reach_probability
            : control->no_projected_flow_reach_probability;
        context.selector_projected_queue_depletion_probability = instrument.yes
            ? control->yes_projected_queue_depletion_probability
            : control->no_projected_queue_depletion_probability;
        context.selector_projected_fill_probability = instrument.yes
            ? control->yes_projected_fill_probability
            : control->no_projected_fill_probability;
        context.prior_aggressive_buy_shares_per_second = instrument.yes
            ? control->yes_aggressive_buy_shares_per_second
            : control->no_aggressive_buy_shares_per_second;
        context.prior_aggressive_sell_shares_per_second = instrument.yes
            ? control->yes_aggressive_sell_shares_per_second
            : control->no_aggressive_sell_shares_per_second;
        context.prior_aggressive_buy_prints_per_second = instrument.yes
            ? control->yes_aggressive_buy_prints_per_second
            : control->no_aggressive_buy_prints_per_second;
        context.prior_aggressive_sell_prints_per_second = instrument.yes
            ? control->yes_aggressive_sell_prints_per_second
            : control->no_aggressive_sell_prints_per_second;
        const auto decision_now_ns = monotonic_ns();
        const auto external = std::atomic_load_explicit(
            &market.external_fair, std::memory_order_acquire);
        const bool external_valid = external && external->valid
            && external->valid_until_ns >= decision_now_ns;
        if (external_valid) {
            context.related_fair_value = instrument.yes != 0
                ? external->yes : 1.0 - external->yes;
            context.related_fair_lower = instrument.yes != 0
                ? external->lower : 1.0 - external->upper;
            context.related_fair_upper = instrument.yes != 0
                ? external->upper : 1.0 - external->lower;
            context.related_snapshot_age_ns = std::max<std::int64_t>(
                0, decision_now_ns - external->calculated_ns);
            context.related_state_valid = 1;
            const double local_weight = std::max(0.0, market_external_local_weight_);
            model.mid_weight *= local_weight;
            model.microprice_weight *= local_weight;
            model.flow_weight *= local_weight;
            model.related_weight = market_external_fair_weight_;
        }
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
        const double venue_minimum_shares = min_order(market, instrument.yes != 0);
        // Use the ask as a conservative notional bound for either passive side:
        // JOIN BUY will be cheaper, while JOIN SELL cannot consume more funded
        // inventory than this price-valued bound. The per-quote fraction is a
        // target; the venue minimum may round it up only inside the independent
        // per-market hard cap. If even the venue minimum is unaffordable, zero
        // is an explicit denial consumed fail-closed by the hot path.
        const double quote_notional_price = source_event.book.valid
            ? std::clamp(e4_price(source_event.book.best_ask_e4), 0.01, 0.99)
            : 1.0;
        const double target_exploration_shares = std::min(
            context.risk.max_quote_shares,
            sleeve_starting_capital_dollars_
                * model.exploration_quote_notional_fraction / quote_notional_price);
        const double minimum_order_notional = venue_minimum_shares * quote_notional_price;
        const double market_notional_cap = sleeve_starting_capital_dollars_
            * model.exploration_max_market_fraction;
        if (target_exploration_shares + 1e-12 >= venue_minimum_shares) {
            context.risk.exploration_max_quote_shares = target_exploration_shares;
        } else if (minimum_order_notional <= market_notional_cap + 1e-12) {
            context.risk.exploration_max_quote_shares = venue_minimum_shares;
        } else {
            context.risk.exploration_max_quote_shares = 0.0;
        }
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
            if (point_ev > 0.0) {
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
                const double shares_requested = micro_shares(intent.quantity_microunits);
                if (shares_requested + 1e-12 < min_order(market, instrument.yes != 0)) {
                    rejected_below_minimum_order_.fetch_add(1, std::memory_order_relaxed);
                    continue;
                }
                quote_intents_.fetch_add(1, std::memory_order_relaxed);
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

public:
    void set_external_fair_weights(double external_weight, double local_weight) noexcept {
        market_external_fair_weight_ = std::max(0.0, external_weight);
        market_external_local_weight_ = std::max(0.0, local_weight);
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
        raw_last_trade_events_.fetch_add(result.raw_last_trade_events, std::memory_order_relaxed);
        valid_trade_prints_.fetch_add(result.trade_events, std::memory_order_relaxed);
        trade_missing_side_.fetch_add(result.trade_missing_side, std::memory_order_relaxed);
        trade_missing_size_.fetch_add(result.trade_missing_size, std::memory_order_relaxed);
        trade_invalid_quantity_.fetch_add(result.trade_invalid_quantity, std::memory_order_relaxed);
        trade_invalid_price_.fetch_add(result.trade_invalid_price, std::memory_order_relaxed);
        trade_invalid_timestamp_.fetch_add(result.trade_invalid_timestamp, std::memory_order_relaxed);
        unknown_asset_.fetch_add(result.ignored_unknown_assets, std::memory_order_relaxed);
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
    void set_sleeve_starting_capital(double dollars) noexcept {
        sleeve_starting_capital_dollars_ = std::max(0.0, dollars);
        starting_capital_fraction_ = 0.01 * sleeve_starting_capital_dollars_;
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
    double market_external_fair_weight_ = 1.0;
    double market_external_local_weight_ = 0.0;
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
    std::atomic<std::uint64_t> rejected_below_minimum_order_{0};
    std::atomic<std::uint64_t> rejected_nonpositive_robust_ev_{0};
    std::atomic<std::uint64_t> rejected_positive_point_ev_{0};
    std::atomic<std::uint64_t> raw_last_trade_events_{0};
    std::atomic<std::uint64_t> valid_trade_prints_{0};
    std::atomic<std::uint64_t> trade_missing_side_{0};
    std::atomic<std::uint64_t> trade_missing_size_{0};
    std::atomic<std::uint64_t> trade_invalid_quantity_{0};
    std::atomic<std::uint64_t> trade_invalid_price_{0};
    std::atomic<std::uint64_t> trade_invalid_timestamp_{0};
    std::atomic<std::uint64_t> unknown_asset_{0};
    std::atomic<std::int64_t> best_rejected_robust_ev_nano_{
        std::numeric_limits<std::int64_t>::min()};
    std::atomic<std::int64_t> best_rejected_point_ev_nano_{
        std::numeric_limits<std::int64_t>::min()};
    double starting_capital_fraction_ = 5.0;
    double sleeve_starting_capital_dollars_ = 0.0;
};

void write_runtime_diagnostics(
    const fs::path& run_root, std::string_view model_sha,
    const std::vector<std::unique_ptr<ShardRuntime>>& shards,
    const MakerFillFunnelCounters& fill_funnel,
    std::uint64_t inventory_seed_budget_rejections) {
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
        total.rejected_below_minimum_order += value.rejected_below_minimum_order;
        total.rejected_nonpositive_robust_ev += value.rejected_nonpositive_robust_ev;
        total.rejected_positive_point_ev += value.rejected_positive_point_ev;
        total.raw_last_trade_events += value.raw_last_trade_events;
        total.valid_trade_prints += value.valid_trade_prints;
        total.trade_missing_side += value.trade_missing_side;
        total.trade_missing_size += value.trade_missing_size;
        total.trade_invalid_quantity += value.trade_invalid_quantity;
        total.trade_invalid_price += value.trade_invalid_price;
        total.trade_invalid_timestamp += value.trade_invalid_timestamp;
        total.unknown_asset += value.unknown_asset;
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
    root["rejected_below_minimum_order"] = total.rejected_below_minimum_order;
    root["inventory_seed_budget_rejections"] = inventory_seed_budget_rejections;
    root["raw_last_trade_events"] = total.raw_last_trade_events;
    root["valid_trade_prints"] = total.valid_trade_prints;
    root["trade_missing_side"] = total.trade_missing_side;
    root["trade_missing_size"] = total.trade_missing_size;
    root["trade_invalid_quantity"] = total.trade_invalid_quantity;
    root["trade_invalid_price"] = total.trade_invalid_price;
    root["trade_invalid_timestamp"] = total.trade_invalid_timestamp;
    root["trade_unknown_asset"] = total.unknown_asset;
    json::object fill;
    fill["public_trades"] = fill_funnel.public_trades.load(std::memory_order_relaxed);
    fill["duplicate_trade"] = fill_funnel.duplicate_trade.load(std::memory_order_relaxed);
    fill["active_orders_seen"] = fill_funnel.active_orders_seen.load(std::memory_order_relaxed);
    fill["causally_pre_arrival"] = fill_funnel.causally_pre_arrival.load(std::memory_order_relaxed);
    fill["wrong_aggressor_side"] = fill_funnel.wrong_aggressor_side.load(std::memory_order_relaxed);
    fill["price_not_crossing"] = fill_funnel.price_not_crossing.load(std::memory_order_relaxed);
    fill["cancel_effective_before_trade"] = fill_funnel.cancel_effective_before_trade.load(std::memory_order_relaxed);
    fill["eligible_orders"] = fill_funnel.eligible_orders.load(std::memory_order_relaxed);
    fill["queue_not_depleted"] = fill_funnel.queue_not_depleted.load(std::memory_order_relaxed);
    fill["eligible_fill_volume"] = static_cast<double>(
        fill_funnel.eligible_fill_microunits.load(std::memory_order_relaxed)) / 1'000'000.0;
    fill["operational_fill_volume"] = static_cast<double>(
        fill_funnel.operational_fill_microunits.load(std::memory_order_relaxed)) / 1'000'000.0;
    root["fill_funnel"] = std::move(fill);
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
        InventoryFactoryPolicy inventory_factory,
        std::atomic<bool>* new_risk_frozen,
        MakerFillFunnelCounters* fill_funnel,
        double starting_capital,
        double base_quote_shares,
        std::int64_t minimum_quote_lifetime_ns,
        std::uint32_t exploration_concurrent_market_cap) {
        const std::int64_t budget = microdollars(starting_capital);
        if (budget < 100) return false;
        CapitalLimits limits;
        limits.sleeve_budget_microdollars = budget;
        limits.max_total_exposure_microdollars = budget;
        limits.max_market_exposure_microdollars = std::max<std::int64_t>(
            1, microdollars(starting_capital * inventory_factory.max_market_gross_fraction));
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
        inventory_targets_.assign(max_market + 1, 0);
        inventory_drain_active_by_market_.assign(max_market + 1, 0);
        inventory_seed_budget_rejected_by_market_.assign(max_market + 1, 0);
        inventory_last_replenish_ns_.assign(max_market + 1, 0);
        latest_yes_books_.assign(max_market + 1, {});
        latest_no_books_.assign(max_market + 1, {});
        inventory_factory_ = inventory_factory;
        inventory_seed_cap_microdollars_ = std::min(
            limits.max_market_exposure_microdollars,
            microdollars(starting_capital
                         * inventory_factory_.seed_max_market_fraction));
        if (inventory_factory_.enabled && inventory_seed_cap_microdollars_ <= 0) return false;
        new_risk_frozen_ = new_risk_frozen;
        fill_funnel_ = fill_funnel;
        if (new_risk_frozen_ == nullptr || fill_funnel_ == nullptr) return false;
        minimum_quote_lifetime_ns_ = std::max<std::int64_t>(0, minimum_quote_lifetime_ns);
        base_quote_shares_ = std::max(1e-6, base_quote_shares);
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

        if (inventory_factory_.enabled) {
            std::vector<std::int64_t> minimums(max_market + 1, 0);
            std::int64_t minimum_total = 0;
            std::size_t eligible = 0;
            for (const auto& market : markets) {
                if (!market->cold.inventory_seed_authorized) continue;
                const double minimum_shares = std::ceil(std::max({
                    base_quote_shares,
                    market->yes_min_order * inventory_factory_.seed_min_quote_multiples,
                    market->no_min_order * inventory_factory_.seed_min_quote_multiples}));
                const auto minimum = microdollars(minimum_shares);
                if (minimum <= 0 || minimum > inventory_seed_cap_microdollars_) {
                    ++inventory_seed_budget_rejections_;
                    continue;
                }
                minimums[market->market_handle] = minimum;
                if (minimum_total > std::numeric_limits<std::int64_t>::max() - minimum) return false;
                minimum_total += minimum;
                ++eligible;
            }
            const auto desired_pool = microdollars(
                starting_capital * inventory_factory_.bootstrap_complete_set_fraction);
            const auto hard_pool = microdollars(
                starting_capital * inventory_factory_.max_maker_gross_fraction);
            const std::int64_t pool = std::min(
                hard_pool, std::max(desired_pool, minimum_total));
            if (eligible > 0 && pool < minimum_total) return false;

            std::int64_t remaining = pool;
            std::int64_t remaining_minimum = minimum_total;
            std::size_t seeded = 0;
            for (const auto& market : markets) {
                if (!market->cold.inventory_seed_authorized) continue;
                const auto minimum = minimums[market->market_handle];
                if (minimum <= 0) continue;
                const auto markets_left = std::max<std::size_t>(1, eligible - seeded);
                const double base_dollars = static_cast<double>(remaining) /
                    1'000'000.0 / static_cast<double>(markets_left);
                const double tick = static_cast<double>(market->yes_tick_e4) / kPriceScaleE4;
                const double spread_factor = std::clamp(
                    market->cold_spread / std::max(2.0 * tick, 1e-6), 0.75, 1.25);
                const double minimum_shares = static_cast<double>(minimum) / 1'000'000.0;
                const double depth_factor = std::clamp(
                    market->cold_visible_depth_shares /
                        std::max(20.0 * minimum_shares, 1e-6),
                    0.50, 1.50);
                const double volatility_haircut = std::clamp(
                    1.0 / (1.0 + 5.0 * market->cold_volatility_proxy), 0.50, 1.0);
                const double depth_cap_shares = std::max(
                    minimum_shares,
                    market->cold_visible_depth_shares
                        * inventory_factory_.seed_visible_depth_fraction);
                const double risk_adjusted = base_dollars * spread_factor
                    * depth_factor * volatility_haircut;
                auto target = microdollars(std::max(
                    minimum_shares, std::min(risk_adjusted, depth_cap_shares)));
                target = std::clamp(target, minimum,
                    inventory_seed_cap_microdollars_);
                remaining_minimum -= minimum;
                target = std::min(target, remaining - remaining_minimum);
                if (target < minimum
                    || !policy_.set_complete_set_reserve_floor(
                        market->market_handle, target)) {
                    return false;
                }

                ExecutionContext context;
                context.market_handle = market->market_handle;
                context.event_handle = market->event_handle;
                context.instrument_handle = market->yes_instrument_handle;
                context.receive_wall_ms = wall_ms();
                context.receive_monotonic_ns = monotonic_ns();
                context.tick_size_e4 = market->yes_tick_e4;
                context.outcome_yes = 1;
                auto split = policy_.split_complete_sets(
                    market->market_handle, target,
                    context.receive_monotonic_ns,
                    market->cold_yes_reference_price, capital_);
                if (!split.accepted || split.capital_invariant_violation
                    || split.paper.invariant_violation) {
                    emit(context, split.paper);
                    return false;
                }
                inventory_targets_[market->market_handle] = target;
                inventory_last_replenish_ns_[market->market_handle] =
                    context.receive_monotonic_ns;
                emit(context, split.paper);
                remaining -= target;
                ++seeded;
            }
            if (eligible > 0 && seeded != eligible) return false;
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

        cache_latest_book(command.context);

        if (!sync_inventory_drain_mode()) return false;
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
                fill_funnel_->public_trades.fetch_add(1, std::memory_order_relaxed);
                fill_funnel_->duplicate_trade.fetch_add(
                    result.paper.duplicate_trade, std::memory_order_relaxed);
                fill_funnel_->active_orders_seen.fetch_add(
                    result.paper.active_orders_seen, std::memory_order_relaxed);
                fill_funnel_->causally_pre_arrival.fetch_add(
                    result.paper.causally_pre_arrival, std::memory_order_relaxed);
                fill_funnel_->wrong_aggressor_side.fetch_add(
                    result.paper.wrong_aggressor_side, std::memory_order_relaxed);
                fill_funnel_->price_not_crossing.fetch_add(
                    result.paper.price_not_crossing, std::memory_order_relaxed);
                fill_funnel_->cancel_effective_before_trade.fetch_add(
                    result.paper.cancel_effective_before_trade, std::memory_order_relaxed);
                fill_funnel_->eligible_orders.fetch_add(
                    result.paper.eligible_orders, std::memory_order_relaxed);
                fill_funnel_->queue_not_depleted.fetch_add(
                    result.paper.queue_not_depleted, std::memory_order_relaxed);
                fill_funnel_->eligible_fill_microunits.fetch_add(
                    std::max<std::int64_t>(0, result.paper.eligible_fill_microunits),
                    std::memory_order_relaxed);
                fill_funnel_->operational_fill_microunits.fetch_add(
                    std::max<std::int64_t>(0, result.paper.operational_fill_microunits),
                    std::memory_order_relaxed);
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
        if (!maybe_replenish(measured_context)) return false;
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

    [[nodiscard]] bool maintenance_tick(std::int64_t now_ns) noexcept {
        if (!sync_inventory_drain_mode()) return false;
        if (!inventory_drain_any_) return true;
        for (std::size_t handle = 1; handle < markets_.size(); ++handle) {
            const auto* market = markets_[handle];
            if (market == nullptr
                || handle >= inventory_drain_active_by_market_.size()
                || inventory_drain_active_by_market_[handle] == 0) continue;
            auto cancellation = policy_.cancel_all(handle, now_ns, capital_);
            ExecutionContext context;
            context.market_handle = handle;
            context.event_handle = market->event_handle;
            context.instrument_handle = market->yes_instrument_handle;
            context.receive_wall_ms = wall_ms();
            context.receive_monotonic_ns = now_ns;
            context.tick_size_e4 = market->yes_tick_e4;
            context.outcome_yes = 1;
            if (cancellation.paper.event_count > 0) emit(context, cancellation.paper);
            if (cancellation.capital_invariant_violation
                || cancellation.paper.invariant_violation) {
                return false;
            }
            auto result = policy_.advance_time(handle, now_ns, capital_);
            if (result.paper.event_count > 0) emit(context, result.paper);
            if (result.capital_invariant_violation || result.paper.invariant_violation) {
                return false;
            }
            const auto yes_quotes = policy_.quote_snapshot(
                handle, market->yes_instrument_handle);
            const auto no_quotes = policy_.quote_snapshot(
                handle, market->no_instrument_handle);
            const bool orders_terminal = yes_quotes.bid_active == 0
                && yes_quotes.ask_active == 0 && yes_quotes.cancel_pending == 0
                && no_quotes.bid_active == 0 && no_quotes.ask_active == 0
                && no_quotes.cancel_pending == 0;
            const auto* inventory = policy_.inventory(handle);
            if (orders_terminal && inventory != nullptr
                && inventory->directional_microunits != 0) {
                const auto& yes_book = latest_yes_books_[handle];
                const auto& no_book = latest_no_books_[handle];
                const bool liquidate_yes = inventory->directional_microunits > 0;
                const auto& relevant_book = liquidate_yes ? yes_book : no_book;
                constexpr std::int64_t kMaximumDrainBookAgeNs = 5'000'000'000LL;
                const auto relevant_receive_ns = relevant_book.receive_monotonic_ns;
                const bool books_executable = relevant_book.valid != 0
                    && relevant_book.lineage_continuous != 0
                    && relevant_book.tick_size_e4 == (liquidate_yes
                        ? market->yes_tick_e4 : market->no_tick_e4)
                    && relevant_receive_ns > 0 && now_ns >= relevant_receive_ns
                    && now_ns - relevant_receive_ns <= kMaximumDrainBookAgeNs;
                if (books_executable) {
                    auto liquidation = policy_.liquidate_directional_inventory(
                        handle,
                        yes_book.best_bid_e4 / market->yes_tick_e4,
                        yes_book.best_bid_microunits,
                        no_book.best_bid_e4 / market->no_tick_e4,
                        no_book.best_bid_microunits,
                        now_ns, capital_);
                    if (liquidation.accepted && liquidation.paper.event_count > 0) {
                        ExecutionContext liquidation_context = context;
                        liquidation_context.instrument_handle = liquidate_yes
                            ? market->yes_instrument_handle
                            : market->no_instrument_handle;
                        liquidation_context.outcome_yes = static_cast<std::uint8_t>(
                            liquidate_yes);
                        liquidation_context.book = relevant_book;
                        emit(liquidation_context, liquidation.paper);
                    }
                    if (liquidation.capital_invariant_violation
                        || liquidation.paper.invariant_violation) {
                        return false;
                    }
                }
            }
            const auto* final_inventory = policy_.inventory(handle);
            const bool globally_frozen = new_risk_frozen_ != nullptr
                && new_risk_frozen_->load(std::memory_order_acquire);
            if (!globally_frozen && final_inventory != nullptr
                && final_inventory->yes_microunits == 0
                && final_inventory->no_microunits == 0
                && final_inventory->pending_split_microunits == 0
                && final_inventory->pending_merge_microunits == 0) {
                inventory_targets_[handle] = 0;
                inventory_drain_active_by_market_[handle] = 0;
            }
        }
        inventory_drain_any_ = std::any_of(
            inventory_drain_active_by_market_.begin(),
            inventory_drain_active_by_market_.end(),
            [](std::uint8_t value) { return value != 0; });
        ++execution_version_;
        return true;
    }

private:
    struct SideControlWatermark {
        std::int64_t bid_ns = 0;
        std::int64_t ask_ns = 0;
    };

    void cache_latest_book(const ExecutionContext& context) noexcept {
        if (context.market_handle == 0
            || context.market_handle >= latest_yes_books_.size()
            || context.book.valid == 0 || context.book.lineage_continuous == 0) {
            return;
        }
        const auto* market = markets_[context.market_handle];
        if (market == nullptr) return;
        if (context.instrument_handle == market->yes_instrument_handle) {
            latest_yes_books_[context.market_handle] = context.book;
        } else if (context.instrument_handle == market->no_instrument_handle) {
            latest_no_books_[context.market_handle] = context.book;
        }
    }

    [[nodiscard]] bool sell_inventory_authorized(
        const MarketContext& market) const noexcept {
        const auto control = std::atomic_load_explicit(
            &market.selection_control, std::memory_order_acquire);
        if (!control) return false;
        for (const auto action : {Action::Join, Action::Improve1,
                                  Action::Fade1, Action::Fade2}) {
            const auto bit = pm::v7::maker::execution_authority_bit(
                action, Side::Sell);
            if ((control->yes_execution_authority_mask & bit) != 0
                || (control->no_execution_authority_mask & bit) != 0) {
                return true;
            }
        }
        return false;
    }

    [[nodiscard]] bool sync_inventory_drain_mode() noexcept {
        const bool globally_requested = new_risk_frozen_ != nullptr
            && new_risk_frozen_->load(std::memory_order_acquire);
        bool any = false;
        for (std::size_t handle = 1; handle < inventory_targets_.size(); ++handle) {
            const auto* market = markets_[handle];
            if (market == nullptr) continue;
            const bool authority_retired = inventory_targets_[handle] > 0
                && !sell_inventory_authorized(*market);
            const bool requested = globally_requested || authority_retired;
            const bool active = inventory_drain_active_by_market_[handle] != 0;
            if (requested != active) {
                if (!policy_.set_complete_set_reserve_floor(
                        handle, requested ? 0 : inventory_targets_[handle])) {
                    return false;
                }
                inventory_drain_active_by_market_[handle] =
                    static_cast<std::uint8_t>(requested);
            }
            any = any || requested;
        }
        inventory_drain_any_ = any;
        return true;
    }

    [[nodiscard]] bool maybe_replenish(const ExecutionContext& context) noexcept {
        if (!inventory_factory_.enabled
            || context.market_handle >= inventory_targets_.size()) return true;
        const auto handle = static_cast<std::size_t>(context.market_handle);
        if (inventory_drain_active_by_market_[handle] != 0) return true;
        auto target = inventory_targets_[handle];
        if (target <= 0) {
            const auto* market = markets_[handle];
            if (market == nullptr) return true;
            if (!sell_inventory_authorized(*market)) {
                inventory_seed_budget_rejected_by_market_[handle] = 0;
                return true;
            }
            const double minimum_shares = std::ceil(std::max({
                base_quote_shares_,
                market->yes_min_order * inventory_factory_.seed_min_quote_multiples,
                market->no_min_order * inventory_factory_.seed_min_quote_multiples}));
            target = microdollars(minimum_shares);
            if (target <= 0) return false;
            if (target > inventory_seed_cap_microdollars_) {
                if (inventory_seed_budget_rejected_by_market_[handle] == 0) {
                    inventory_seed_budget_rejected_by_market_[handle] = 1;
                    ++inventory_seed_budget_rejections_;
                }
                return true;
            }
            inventory_seed_budget_rejected_by_market_[handle] = 0;
            if (!policy_.set_complete_set_reserve_floor(handle, target)) {
                return false;
            }
            auto split = policy_.split_complete_sets(
                handle, target, std::max<std::int64_t>(1, monotonic_ns()),
                market->cold_yes_reference_price, capital_);
            emit(context, split.paper);
            if (split.capital_invariant_violation || split.paper.invariant_violation) {
                return false;
            }
            if (!split.accepted) {
                (void)policy_.set_complete_set_reserve_floor(handle, 0);
                return true;
            }
            inventory_targets_[handle] = target;
            inventory_last_replenish_ns_[handle] = monotonic_ns();
            ++execution_version_;
            return true;
        }
        const auto* inventory = policy_.inventory(context.market_handle);
        if (target <= 0 || inventory == nullptr) return true;
        const auto trigger = static_cast<std::int64_t>(std::floor(
            static_cast<long double>(target)
                * inventory_factory_.replenish_trigger_fraction));
        if (inventory->balanced_complete_set_microunits > trigger) return true;
        const auto now = std::max<std::int64_t>(1, monotonic_ns());
        const auto last = inventory_last_replenish_ns_[context.market_handle];
        if (last > 0 && now - last < inventory_factory_.replenish_cooldown_ns) return true;
        inventory_last_replenish_ns_[context.market_handle] = now;
        const auto delta = target - inventory->balanced_complete_set_microunits;
        if (delta <= 0) return true;
        const auto* market = markets_[context.market_handle];
        double yes_reference_price = market != nullptr
            ? market->cold_yes_reference_price : 0.5;
        if (context.book.valid && context.book.best_bid_e4 > 0
            && context.book.best_ask_e4 > context.book.best_bid_e4) {
            const double observed_mid = static_cast<double>(
                context.book.best_bid_e4 + context.book.best_ask_e4)
                / (2.0 * pm::v7::kCanonicalPriceScale);
            yes_reference_price = context.outcome_yes
                ? observed_mid : 1.0 - observed_mid;
        }
        yes_reference_price = std::clamp(yes_reference_price, 1e-6, 1.0 - 1e-6);
        auto split = policy_.split_complete_sets(
            context.market_handle, delta, now, yes_reference_price, capital_);
        ExecutionContext replenishment = context;
        replenishment.receive_wall_ms = wall_ms();
        replenishment.receive_monotonic_ns = now;
        emit(replenishment, split.paper);
        ++execution_version_;
        return !split.capital_invariant_violation && !split.paper.invariant_violation;
    }

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

    [[nodiscard]] std::uint64_t inventory_seed_budget_rejections() const noexcept {
        return inventory_seed_budget_rejections_;
    }

private:
    MakerPaperExecutionPolicy policy_{};
    SleeveCapitalAccount capital_{};
    std::vector<MarketContext*> markets_;
    std::vector<SideControlWatermark> control_watermarks_;
    std::vector<std::uint8_t> exploration_market_active_;
    std::vector<std::int64_t> inventory_targets_;
    std::vector<std::uint8_t> inventory_drain_active_by_market_;
    std::vector<std::uint8_t> inventory_seed_budget_rejected_by_market_;
    std::vector<std::int64_t> inventory_last_replenish_ns_;
    std::vector<BookHotSnapshot> latest_yes_books_;
    std::vector<BookHotSnapshot> latest_no_books_;
    InventoryFactoryPolicy inventory_factory_{};
    std::int64_t inventory_seed_cap_microdollars_ = 0;
    std::uint64_t inventory_seed_budget_rejections_ = 0;
    std::atomic<bool>* new_risk_frozen_ = nullptr;
    MakerFillFunnelCounters* fill_funnel_ = nullptr;
    bool inventory_drain_any_ = false;
    double base_quote_shares_ = 1.0;
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
                    std::string policy_hash, std::string config_hash,
                    double starting_capital,
                    const std::vector<std::unique_ptr<MarketContext>>& markets)
        : run_root_(std::move(run_root)), model_sha_(std::move(model_sha)),
          policy_hash_(std::move(policy_hash)), config_hash_(std::move(config_hash)),
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
        market_cycles_.resize(max_market + 1);
        for (const auto& market : markets) {
            markets_[market->market_handle] = market.get();
            instruments_[market->yes_instrument_handle] = {market.get(), true};
            instruments_[market->no_instrument_handle] = {market.get(), false};
        }
        fs::create_directories(run_root_ / "ledger" / "spool");
        fs::create_directories(run_root_ / "micro_maker");
        restore_flat_realized_pnl();
    }

    void consume(const TelemetryRecord& record) {
        if (record.market_handle >= markets_.size() || markets_[record.market_handle] == nullptr) return;
        if (record.has_inventory) inventory_[record.market_handle] = record.inventory;
        update_active_orders(record);
        if (record.kind == TelemetryKind::Candidate) write_candidate(record);
        else write_paper(record);
    }

    void write_model_reload(std::uint64_t old_version, std::uint64_t new_version,
                            std::string_view model_hash) {
        auto event = common("MODEL_RELOAD");
        event["model_version"] = std::to_string(new_version);
        event["intended_action"] = "ATOMIC_CHAMPION_RELOAD";
        json::object metadata;
        attach_economic_identity(metadata);
        metadata["old_model_version"] = old_version;
        metadata["new_model_version"] = new_version;
        metadata["champion_hash"] = model_hash;
        metadata["reload_semantics"] = "immutable_snapshot_atomic_publish";
        event["metadata"] = std::move(metadata);
        spool(std::move(event));
    }

    void write_state(bool new_risk_frozen = false) {
        json::object root;
        root["schema"] = "polymarket_v7_professional_maker_state_v2";
        root["timestamp_ms"] = wall_ms();
        root["paper_only"] = true;
        root["authenticated_execution"] = false;
        root["real_order_submission"] = false;
        root["model_sha"] = model_sha_;
        root["starting_capital"] = starting_capital_;

        double remaining_cost = 0.0;
        double realized = realized_pnl_before_boot_;
        bool inventory_flat = true;
        json::object inventory;
        for (std::size_t handle = 1; handle < markets_.size(); ++handle) {
            const auto* market = markets_[handle];
            if (market == nullptr) continue;
            const auto& state = inventory_[handle];
            remaining_cost += state.yes_cost + state.no_cost;
            realized += state.realized_trading_pnl;
            inventory_flat = inventory_flat
                && state.yes_microunits == 0 && state.no_microunits == 0
                && std::abs(state.yes_cost) <= 1e-12
                && std::abs(state.no_cost) <= 1e-12;
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
            row["yes_reserved_sell_shares"] = micro_shares(state.yes_reserved_sell_microunits);
            row["no_reserved_sell_shares"] = micro_shares(state.no_reserved_sell_microunits);
            row["yes_free_shares"] = micro_shares(state.yes_free_microunits);
            row["no_free_shares"] = micro_shares(state.no_free_microunits);
            row["balanced_complete_set_shares"] = micro_shares(
                state.balanced_complete_set_microunits);
            row["directional_shares"] = static_cast<double>(
                state.directional_microunits) / kMicrounitsPerShare;
            row["complete_set_reserve_floor_shares"] = micro_shares(
                state.complete_set_reserve_floor_microunits);
            row["collateral_committed"] = static_cast<double>(
                state.collateral_committed_microdollars) / 1'000'000.0;
            row["pending_split_shares"] = micro_shares(state.pending_split_microunits);
            row["pending_merge_shares"] = micro_shares(state.pending_merge_microunits);
            row["yes_cost"] = state.yes_cost;
            row["no_cost"] = state.no_cost;
            inventory[market->cold.market_id] = std::move(row);
        }
        root["cash"] = starting_capital_ - remaining_cost + realized;
        root["realized_trading_pnl"] = realized;
        root["estimated_maker_rebate_pnl"] = 0.0;
        root["estimated_liquidity_reward_pnl"] = 0.0;
        root["active_order_count"] = active_orders_.size();
        root["flat_restart_safe"] = active_orders_.empty() && inventory_flat;
        root["new_risk_frozen"] = new_risk_frozen;
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
    struct MarketCycleEconomics {
        double realized_pnl = 0.0;
        double executed_shares = 0.0;
        std::int64_t opened_ms = 0;
        std::uint64_t sequence = 0;
        bool active = false;
    };

    [[nodiscard]] std::string active_order_key(const TelemetryRecord& record) const {
        return std::to_string(record.market_handle) + ":" +
               std::to_string(record.paper.order_id);
    }

    void update_active_orders(const TelemetryRecord& record) {
        if (record.kind != TelemetryKind::PaperEvent || record.paper.order_id == 0) return;
        const auto key = active_order_key(record);
        if (record.paper.kind == PaperMakerEventKind::OrderLive) {
            active_orders_.insert(key);
            return;
        }
        if (record.paper.kind == PaperMakerEventKind::Cancelled
            || record.paper.kind == PaperMakerEventKind::Rejected
            || (record.paper.kind == PaperMakerEventKind::Fill
                && record.paper.order_state == pm::v7::OrderState::Filled)) {
            active_orders_.erase(key);
        }
    }

    void restore_flat_realized_pnl() {
        const fs::path path = run_root_ / "micro_maker" / "state.json";
        if (!fs::exists(path)) return;
        try {
            const auto prior = read_json(path);
            if (!prior.is_object()) return;
            const auto& root = prior.as_object();
            if (!boolean(find_value(root, "paper_only"), false)
                || boolean(find_value(root, "authenticated_execution"), true)
                || boolean(find_value(root, "real_order_submission"), true)
                || text(find_value(root, "model_sha")) != model_sha_
                || std::abs(number(find_value(root, "starting_capital"), -1.0)
                            - starting_capital_) > 1e-9
                || integer(find_value(root, "active_order_count"), 1) != 0) {
                return;
            }
            const auto* inventory = find_value(root, "inventory");
            if (inventory == nullptr || !inventory->is_object()) return;
            for (const auto& item : inventory->as_object()) {
                if (!item.value().is_object()) return;
                const auto& row = item.value().as_object();
                if (std::abs(number(find_value(row, "yes_shares"), 0.0)) > 1e-9
                    || std::abs(number(find_value(row, "no_shares"), 0.0)) > 1e-9
                    || std::abs(number(find_value(row, "yes_cost"), 0.0)) > 1e-9
                    || std::abs(number(find_value(row, "no_cost"), 0.0)) > 1e-9) {
                    return;
                }
            }
            const double realized = number(find_value(root, "realized_trading_pnl"), 0.0);
            if (std::isfinite(realized)) realized_pnl_before_boot_ = realized;
        } catch (...) {
            // A missing/corrupt/non-flat prior snapshot cannot seed a restart.
            // The cohort supervisor only rotates after independently proving
            // a fresh flat state, so fail closed by refusing the carry-forward.
        }
    }

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
        attach_economic_identity(metadata);
        metadata["outcome"] = record.outcome_yes ? "YES" : "NO";
        metadata["action"] = action_name(record.action);
        metadata["placement_action"] = action_name(
            record.decision.placement_action);
        metadata["execution_side"] = side_name(record.intent.side);
        metadata["maker_cpp_hot_path"] = true;
        metadata["queue_operational_scenario"] = "pessimistic";
        metadata["state_version"] = record.state_version;
        metadata["decision_reason"] = static_cast<std::uint64_t>(record.decision.reason);
        metadata["toxicity"] = record.decision.toxicity;
        metadata["robust_ev"] = record.decision.robust_ev;
        metadata["ev_uncertainty"] = record.decision.ev_uncertainty;
        metadata["fair_value"] = record.decision.fair_value;
        metadata["fair_lower"] = record.decision.fair_lower;
        metadata["fair_upper"] = record.decision.fair_upper;
        metadata["external_fair_authority"] =
            record.decision.external_fair_authority != 0;
        const bool bid_side = record.intent.side == Side::Buy;
        const auto selector_index = pm::v7::maker::execution_authority_index(
            record.decision.placement_action, record.intent.side);
        const bool selector_index_valid =
            selector_index < pm::v7::maker::kSelectorAuthorityCellCount;
        metadata["selector_execution_authority"] = json::object{
            {"required", record.decision.selector_authority_required != 0},
            {"selector_generation", record.decision.selector_generation},
            {"token_action_side_mask",
                static_cast<std::uint64_t>(
                    record.decision.selector_execution_authority_mask)},
            {"chosen_cell_authorized", bid_side
                ? record.decision.bid_selector_cell_authorized != 0
                : record.decision.ask_selector_cell_authorized != 0},
            {"inventory_reduction_bypass", bid_side
                ? record.decision.bid_inventory_reduction_bypass != 0
                : record.decision.ask_inventory_reduction_bypass != 0},
            {"selector_projected_flow_reach_probability", selector_index_valid
                ? record.decision.selector_projected_flow_reach_probability[selector_index]
                : 0.0},
            {"selector_projected_queue_depletion_probability", selector_index_valid
                ? record.decision.selector_projected_queue_depletion_probability[selector_index]
                : 0.0},
            {"selector_projected_fill_probability", selector_index_valid
                ? record.decision.selector_projected_fill_probability[selector_index]
                : 0.0},
        };
        metadata["execution_funnel_prediction"] = json::object{
            {"opposite_aggressive_flow_shares_per_second", bid_side
                ? record.decision.bid_opposite_flow_shares_per_second
                : record.decision.ask_opposite_flow_shares_per_second},
            {"opposite_aggressive_flow_prints_per_second", bid_side
                ? record.decision.bid_opposite_flow_prints_per_second
                : record.decision.ask_opposite_flow_prints_per_second},
            {"flow_reach_probability", bid_side
                ? record.decision.bid_flow_reach_probability
                : record.decision.ask_flow_reach_probability},
            {"queue_depletion_probability_given_reach", bid_side
                ? record.decision.bid_queue_depletion_probability
                : record.decision.ask_queue_depletion_probability},
            {"fill_probability", bid_side
                ? record.decision.bid_fill_probability
                : record.decision.ask_fill_probability},
        };
        metadata["placement_features"] = json::object{
            {"spread_ticks", record.decision.features.spread_ticks},
            {"imbalance", record.decision.features.imbalance},
            {"ofi", record.decision.features.ofi},
            {"ew_vol_ticks", record.decision.features.ew_vol_ticks},
            {"trade_intensity", record.decision.features.trade_intensity},
            {"aggressive_buy_shares_per_second",
                record.decision.features.aggressive_buy_shares_per_second},
            {"aggressive_sell_shares_per_second",
                record.decision.features.aggressive_sell_shares_per_second},
            {"aggressive_buy_prints_per_second",
                record.decision.features.aggressive_buy_prints_per_second},
            {"aggressive_sell_prints_per_second",
                record.decision.features.aggressive_sell_prints_per_second},
            {"cancel_intensity", record.decision.features.cancel_intensity},
            {"short_return_ticks", record.decision.features.short_return_ticks},
            {"inventory_fraction", record.decision.features.inventory_fraction},
            {"local_latency_ms", record.decision.features.local_latency_ms},
        };
        if (record.decision.exploration_max_rest_ns > 0) {
            metadata["exploration_lifetime_arm"] =
                record.decision.exploration_persistent != 0 ? "PERSISTENT" : "CONTROL";
            metadata["exploration_max_rest_ms"] =
                record.decision.exploration_max_rest_ns / 1'000'000LL;
        }
        event["metadata"] = std::move(metadata);
    }

    void attach_economic_identity(json::object& metadata) const {
        metadata["policy_hash"] = policy_hash_;
        metadata["config_hash"] = config_hash_;
        metadata["execution_semantics_version"] = "maker-paper-v7.2-bilateral-inventory";
        metadata["queue_model_version"] = "pessimistic-public-print-v1";
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
            track_market_cycle(record, paper.realized_pnl,
                               micro_shares(paper.operational_fill_microunits));
            if (paper.order_state == pm::v7::OrderState::Filled) {
                write_order_state(record, "FILLED");
            }
            else if (paper.order_state == pm::v7::OrderState::Partial) write_order_state(record, "PARTIAL");
            if (paper.realized_pnl != 0.0) {
                maybe_write_market_cycle_final(record, "ORDER_FILL_FLAT");
            }
            return;
        }
        if (paper.kind == PaperMakerEventKind::InventorySplitRequested) {
            write_inventory_event(record, "INVENTORY_SPLIT_REQUESTED");
            return;
        }
        if (paper.kind == PaperMakerEventKind::InventorySplitRejected) {
            write_inventory_event(record, "INVENTORY_SPLIT_REJECTED");
            return;
        }
        if (paper.kind == PaperMakerEventKind::InventorySplit) {
            track_market_cycle(record, 0.0, 0.0);
            write_inventory_event(record, "INVENTORY_SPLIT");
            return;
        }
        if (paper.kind == PaperMakerEventKind::InventoryMerge) {
            track_market_cycle(record, paper.realized_pnl, 0.0);
            write_inventory_event(record, "INVENTORY_MERGE");
            maybe_write_market_cycle_final(record, "COMPLETE_SET_MERGE_FLAT");
            return;
        }
        if (paper.kind == PaperMakerEventKind::InventoryLiquidation) {
            track_market_cycle(record, paper.realized_pnl,
                               micro_shares(paper.operational_fill_microunits));
            write_inventory_event(record, "INVENTORY_LIQUIDATION");
            maybe_write_market_cycle_final(record, "DRAIN_LIQUIDATION_FLAT");
            return;
        }
    }

    std::string write_inventory_event(const TelemetryRecord& record, const char* event_type) {
        const auto& paper = record.paper;
        auto event = common(event_type);
        attach_market(event, record);
        const std::string position_id = "mmi-" + std::to_string(record.market_handle) + "-" +
                                        telemetry_epoch_ + "-" +
                                        std::to_string(++inventory_event_sequence_);
        event["position_id"] = position_id;
        event["intended_action"] = event_type;
        event["intended_size"] = micro_shares(paper.original_microunits > 0
            ? paper.original_microunits : paper.operational_fill_microunits);
        event["realized_cashflow"] = paper.realized_pnl;
        if (paper.kind == PaperMakerEventKind::InventoryMerge) {
            event["final_pnl"] = paper.realized_pnl;
        }
        json::object metadata;
        attach_economic_identity(metadata);
        metadata["maker_cpp_hot_path"] = true;
        metadata["transformation"] = event_type;
        metadata["collateral_usd"] = static_cast<double>(
            paper.original_microunits > 0
                ? paper.original_microunits : paper.operational_fill_microunits)
            / kMicrounitsPerShare;
        metadata["collateral_before_usd"] = static_cast<double>(
            paper.collateral_before_microdollars) / 1'000'000.0;
        metadata["collateral_after_usd"] = static_cast<double>(
            paper.collateral_after_microdollars) / 1'000'000.0;
        metadata["yes_before_shares"] = micro_shares(paper.yes_before_microunits);
        metadata["no_before_shares"] = micro_shares(paper.no_before_microunits);
        metadata["yes_after_shares"] = micro_shares(paper.yes_after_microunits);
        metadata["no_after_shares"] = micro_shares(paper.no_after_microunits);
        metadata["yes_cost_before"] = paper.yes_cost_before;
        metadata["no_cost_before"] = paper.no_cost_before;
        metadata["yes_cost_after"] = paper.yes_cost_after;
        metadata["no_cost_after"] = paper.no_cost_after;
        metadata["split_yes_reference_price"] = paper.split_yes_reference_price;
        metadata["split_cost_allocation"] = "causal_yes_mid_and_complement";
        metadata["pnl_created_by_split"] = false;
        metadata["inventory_lineage"] = "market_local_average_cost_before_after";
        metadata["consumed_inventory_provenance_complete"] = true;
        event["metadata"] = std::move(metadata);
        spool(std::move(event));
        return position_id;
    }

    void track_market_cycle(const TelemetryRecord& record, double realized_pnl,
                            double executed_shares) {
        if (record.market_handle == 0 || record.market_handle >= market_cycles_.size()) return;
        auto& cycle = market_cycles_[record.market_handle];
        if (!cycle.active) {
            cycle = MarketCycleEconomics{};
            cycle.active = true;
            cycle.opened_ms = wall_ms();
            cycle.sequence = ++market_cycle_sequence_;
        }
        cycle.realized_pnl += realized_pnl;
        cycle.executed_shares += std::max(0.0, executed_shares);
    }

    [[nodiscard]] bool market_has_active_orders(std::uint64_t market_handle) const {
        const std::string prefix = std::to_string(market_handle) + ":";
        for (const auto& key : active_orders_) {
            if (key.rfind(prefix, 0) == 0) return true;
        }
        return false;
    }

    void maybe_write_market_cycle_final(
        const TelemetryRecord& record, const char* terminal_state) {
        if (record.market_handle == 0 || record.market_handle >= market_cycles_.size()
            || market_has_active_orders(record.market_handle)) return;
        auto& cycle = market_cycles_[record.market_handle];
        const auto& inventory = record.inventory;
        const bool economically_flat = inventory.yes_microunits == 0
            && inventory.no_microunits == 0
            && inventory.pending_split_microunits == 0
            && inventory.pending_merge_microunits == 0
            && std::abs(inventory.yes_cost) <= 1e-12
            && std::abs(inventory.no_cost) <= 1e-12;
        if (!cycle.active || !economically_flat) return;

        auto event = common("FINAL");
        attach_market(event, record);
        const std::string position_id = "mm-cycle-" +
            std::to_string(record.market_handle) + "-" + telemetry_epoch_ + "-" +
            std::to_string(cycle.sequence);
        event["position_id"] = position_id;
        event["final_pnl"] = cycle.realized_pnl;
        event["realized_cashflow"] = cycle.realized_pnl;
        event["fee"] = 0.0;
        event["slippage"] = 0.0;
        event["unwind_loss"] = 0.0;
        event["capital_cost"] = 0.0;
        event["latency_cost"] = 0.0;
        event["capital_duration_ms"] = std::max<std::int64_t>(
            0, wall_ms() - cycle.opened_ms);
        json::object metadata;
        attach_economic_identity(metadata);
        metadata["maker_cpp_hot_path"] = true;
        metadata["realized"] = true;
        metadata["unwind_accounted"] = true;
        metadata["economic_cycle_flat"] = true;
        metadata["cost_vector_complete"] = true;
        metadata["terminal_state"] = terminal_state;
        metadata["terminal_id"] = position_id + ":final";
        metadata["executed_shares"] = cycle.executed_shares;
        metadata["inventory_cost_basis_method"] =
            "market_local_weighted_average_cost_full_cycle";
        metadata["inventory_lineage_complete"] = true;
        metadata["pnl_decomposition"] = json::object{
            {"trading_pnl", cycle.realized_pnl},
            {"spread_capture", 0.0}, {"adverse_markout", 0.0},
            {"inventory_pnl", cycle.realized_pnl}, {"maker_rebates", 0.0},
            {"liquidity_rewards", 0.0}, {"own_reward_share_verified", false}};
        event["metadata"] = std::move(metadata);
        spool(std::move(event));
        cycle = MarketCycleEconomics{};
    }

    void write_order_state(const TelemetryRecord& record, const char* state) {
        if (record.paper.order_id == 0) return;
        auto event = common("ORDER_STATE");
        attach_market(event, record);
        event["order_id"] = order_id(record.market_handle, record.paper.order_id);
        event["order_state"] = state;
        event["side"] = side_name(record.paper.side);
        json::object metadata;
        attach_economic_identity(metadata);
        metadata["maker_cpp_hot_path"] = true;
        metadata["oms_state"] = order_state_name(record.paper.order_state);
        metadata["decision_reason"] = decision_reason_name(record.decision.reason);
        metadata["decision_reason_code"] = static_cast<std::uint64_t>(record.decision.reason);
        metadata["safety_preemption"] = safety_preemption(record.decision.reason);
        metadata["execution_outcome"] = execution_outcome_name(
            record.paper.execution_outcome);
        metadata["opposite_flow_prints_seen"] =
            record.paper.opposite_flow_prints_seen;
        metadata["price_reach_prints_seen"] =
            record.paper.price_reach_prints_seen;
        metadata["opposite_flow_shares_seen"] = micro_shares(
            record.paper.opposite_flow_microunits_seen);
        metadata["price_reach_shares_seen"] = micro_shares(
            record.paper.price_reach_microunits_seen);
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
        attach_economic_identity(metadata);
        metadata["maker_cpp_hot_path"] = true;
        metadata["public_trade_id"] = std::to_string(paper.trade_id);
        metadata["queue_ahead_lower"] = micro_shares(paper.queue.ahead_lower_microunits);
        metadata["queue_ahead_expected"] = micro_shares(paper.queue.ahead_expected_microunits);
        metadata["queue_ahead_upper"] = micro_shares(paper.queue.ahead_upper_microunits);
        metadata["queue_confidence"] = paper.queue.confidence;
        metadata["pessimistic_fill"] = micro_shares(paper.pessimistic_fill_microunits);
        metadata["expected_fill"] = micro_shares(paper.expected_fill_microunits);
        metadata["optimistic_fill"] = micro_shares(paper.optimistic_fill_microunits);
        metadata["execution_outcome"] = execution_outcome_name(
            paper.execution_outcome);
        metadata["opposite_flow_prints_seen"] = paper.opposite_flow_prints_seen;
        metadata["price_reach_prints_seen"] = paper.price_reach_prints_seen;
        metadata["opposite_flow_shares_seen"] = micro_shares(
            paper.opposite_flow_microunits_seen);
        metadata["price_reach_shares_seen"] = micro_shares(
            paper.price_reach_microunits_seen);
        event["metadata"] = std::move(metadata);
        spool(std::move(event));
    }

    fs::path run_root_;
    std::string model_sha_;
    std::string policy_hash_;
    std::string config_hash_;
    double starting_capital_ = 0.0;
    std::vector<MarketContext*> markets_;
    std::vector<InstrumentCold> instruments_;
    std::vector<PaperMakerInventory> inventory_;
    std::unordered_set<std::string> active_orders_;
    std::vector<MarketCycleEconomics> market_cycles_;
    double realized_pnl_before_boot_ = 0.0;
    // The supervisor can restart the C++ process without rotating the exact-SHA
    // ledger.  Sequence counters restart at one, so every process boot needs a
    // distinct causal namespace or the canonical spool correctly rejects all
    // subsequent telemetry as duplicate record/order IDs.
    std::string telemetry_epoch_;
    std::uint64_t record_sequence_ = 0;
    std::uint64_t inventory_event_sequence_ = 0;
    std::uint64_t market_cycle_sequence_ = 0;
};

std::vector<std::unique_ptr<MarketContext>> build_markets(
    const Options& options, const pm::Config& config) {
    std::vector<SelectedMarket> selected;
    while (!g_stop.load(std::memory_order_relaxed)) {
        try {
            if (fs::exists(options.selection) && fs::file_size(options.selection) > 0) {
                selected = load_selection(options.selection, options.model_sha);
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
        const double visible_depth = std::min({
            yes->second.top_depth(true, 5), yes->second.top_depth(false, 5),
            no->second.top_depth(true, 5), no->second.top_depth(false, 5)});
        const double spread = std::max(yes->second.spread(), no->second.spread());
        const double yes_mid = yes->second.midpoint();
        const double no_mid = no->second.midpoint();
        const double complement_gap = std::isfinite(yes_mid) && std::isfinite(no_mid)
            ? std::abs(yes_mid + no_mid - 1.0) : 1.0;
        // Cold start has no causal return history yet. Spread plus complement
        // dislocation is a conservative observable volatility/risk proxy; live
        // decisions replace it with the shared WebSocket state immediately.
        const double volatility_proxy = std::clamp(spread + complement_gap, 0.0, 1.0);
        output.push_back(std::make_unique<MarketContext>(
            std::move(selected_market), market, event, yes_instrument, no_instrument,
            yes_tick, no_tick, std::max(yes->second.min_order_size, 1e-6),
            std::max(no->second.min_order_size, 1e-6), visible_depth, spread,
            volatility_proxy,
            std::isfinite(yes_mid) && yes_mid > 0.0 && yes_mid < 1.0
                ? yes_mid : 0.5));
    }
    if (output.empty()) throw std::runtime_error("no selected maker market has valid cold-start books");
    return output;
}

[[nodiscard]] bool refresh_selection_control(
    const fs::path& path,
    std::string_view model_sha,
    const std::vector<std::unique_ptr<MarketContext>>& markets,
    std::uint64_t& current_generation) {
    auto selected = load_selection(path, model_sha);
    if (selected.size() != markets.size()) {
        throw std::runtime_error("maker in-place selection membership size changed");
    }
    std::unordered_map<std::string, MarketContext*> current;
    current.reserve(markets.size());
    for (const auto& market : markets) {
        current.emplace(market->cold.market_id, market.get());
    }
    std::vector<std::pair<MarketContext*, std::shared_ptr<const SelectedMarket>>> pending;
    pending.reserve(selected.size());
    std::uint64_t generation = 0;
    for (auto& next : selected) {
        const auto found = current.find(next.market_id);
        if (found == current.end()) {
            throw std::runtime_error("maker in-place selection introduced a new market");
        }
        const auto& cold = found->second->cold;
        if (next.condition_id != cold.condition_id
            || next.event_id != cold.event_id
            || next.yes_token != cold.yes_token
            || next.no_token != cold.no_token) {
            throw std::runtime_error("maker in-place selection changed immutable identity");
        }
        generation = std::max(generation, next.selector_generation);
        pending.emplace_back(
            found->second,
            std::make_shared<const SelectedMarket>(std::move(next)));
    }
    if (generation <= current_generation) return false;
    for (auto& [market, control] : pending) {
        std::atomic_store_explicit(
            &market->selection_control, std::move(control),
            std::memory_order_release);
    }
    current_generation = generation;
    return true;
}

[[nodiscard]] bool refresh_external_maker_fair(
    const ExternalMakerFairPolicy& policy,
    const std::vector<std::unique_ptr<MarketContext>>& markets,
    std::string_view model_sha,
    std::int64_t now_ns) {
    static const auto invalid = std::make_shared<const ExternalMakerSnapshot>();
    const auto invalidate_all = [&]() {
        for (const auto& market : markets) {
            std::atomic_store_explicit(
                &market->external_fair, invalid, std::memory_order_release);
        }
    };
    if (!policy.enabled) return false;

    const auto root_value = read_json(policy.status_path);
    if (!root_value.is_object()) {
        invalidate_all();
        return false;
    }
    const auto& root = root_value.as_object();
    if (text(find_value(root, "schema")) != "polymarket_v7_external_fair_status_v1"
        || text(find_value(root, "code_sha")) != model_sha
        || !boolean(find_value(root, "paper_only"), false)
        || boolean(find_value(root, "authenticated_execution"), true)
        || boolean(find_value(root, "real_order_submission"), true)) {
        invalidate_all();
        return false;
    }
    const auto* contract = child_object(root, "contract");
    const auto* model = child_object(root, "model");
    const auto* fair = child_object(root, "fair");
    const auto* market_status = child_object(root, "market");
    const auto* router = child_object(root, "paper_router");
    const auto* maturity = router == nullptr ? nullptr
        : child_object(*router, "maturity");
    if (contract == nullptr || model == nullptr || fair == nullptr || market_status == nullptr
        || (policy.require_verified_contract
            && !boolean(find_value(*contract, "verified"), false))
        || (policy.require_model_mature
            && !boolean(find_value(*model, "mature"), false))
        || (policy.require_positive_2x_cost_stress
            && (maturity == nullptr
                || !boolean(find_value(*maturity,
                    "eligible_for_manual_paper_promotion"), false)
                || number(find_value(*maturity,
                    "virtual_2x_cost_stress_pnl"), 0.0) <= 0.0))
        || !boolean(find_value(*fair, "valid"), false)) {
        invalidate_all();
        return false;
    }

    const std::string market_id = text(find_value(*market_status, "market_id"));
    const double yes = number(find_value(*fair, "yes"), -1.0);
    const double lower = number(find_value(*fair, "lower"), -1.0);
    const double upper = number(find_value(*fair, "upper"), -1.0);
    const auto calculated_ns = integer(find_value(*fair, "calculated_monotonic_ns"), 0);
    const auto valid_until_ns = integer(find_value(*fair, "valid_until_monotonic_ns"), 0);
    if (market_id.empty() || !std::isfinite(yes) || !std::isfinite(lower)
        || !std::isfinite(upper) || lower <= 0.0 || lower > yes || yes > upper
        || upper >= 1.0 || upper - lower > policy.maximum_interval_width
        || calculated_ns <= 0 || calculated_ns > now_ns || valid_until_ns < now_ns
        || now_ns - calculated_ns > policy.maximum_snapshot_age_ns) {
        invalidate_all();
        return false;
    }
    for (const auto& market : markets) {
        if (market->cold.market_id != market_id) continue;
        auto mutable_snapshot = std::make_shared<ExternalMakerSnapshot>();
        mutable_snapshot->yes = yes;
        mutable_snapshot->lower = lower;
        mutable_snapshot->upper = upper;
        mutable_snapshot->calculated_ns = calculated_ns;
        mutable_snapshot->valid_until_ns = valid_until_ns;
        mutable_snapshot->valid = true;
        std::shared_ptr<const ExternalMakerSnapshot> snapshot =
            std::move(mutable_snapshot);
        std::atomic_store_explicit(
            &market->external_fair, snapshot, std::memory_order_release);
        for (const auto& other : markets) {
            if (other.get() != market.get()) {
                std::atomic_store_explicit(
                    &other->external_fair, invalid, std::memory_order_release);
            }
        }
        return true;
    }
    invalidate_all();
    return false;
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
        const InventoryFactoryPolicy inventory_factory =
            load_inventory_factory_policy(options.maker_policy);
        ExternalMakerFairPolicy external_maker_fair =
            load_external_maker_fair_policy(options.maker_policy);
        if (fs::path(external_maker_fair.status_path).is_relative()) {
            external_maker_fair.status_path = (
                fs::path(options.run_root) / external_maker_fair.status_path).string();
        }
        auto initial = std::make_shared<MakerModelSnapshot>(
            load_model_snapshot(options.maker_policy, options.config,
                                options.model, options.model_sha));
        auto model_store = std::make_shared<MakerModelStore>(initial);
        auto markets = build_markets(options, config);

        std::atomic<bool> global_kill{false};
        std::atomic<bool> new_risk_frozen{false};
        std::atomic<bool> fatal{false};
        MakerFillFunnelCounters fill_funnel;
        auto execution = std::make_unique<ExecutionCore>();
        if (!execution->initialize(markets, paper_policy, inventory_factory,
                                   &new_risk_frozen, &fill_funnel,
                                   config.starting_capital, initial->base_quote_shares,
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
            shard->set_external_fair_weights(
                external_maker_fair.external_directional_weight,
                external_maker_fair.local_directional_weight);
            // Max quote dollars before per-event price conversion. The allocation
            // child config is already sleeve-bounded by v7_capital_allocator.py.
            shard->set_sleeve_starting_capital(config.starting_capital);
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
            std::int64_t last_maintenance_ns = monotonic_ns();
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
                    const auto now_ns = monotonic_ns();
                    if (now_ns - last_maintenance_ns >= 100'000'000LL) {
                        if (!execution->maintenance_tick(now_ns)) {
                            fatal.store(true, std::memory_order_release);
                        }
                        last_maintenance_ns = now_ns;
                    }
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

        TelemetryWriter writer(
            options.run_root, options.model_sha,
            hex64(fnv1a(read_file(options.maker_policy))),
            hex64(fnv1a(read_file(options.config))),
            config.starting_capital, markets);
        writer.write_state(false);
        for (auto& shard : shards) shard->start();

        std::int64_t last_state_ms = 0;
        std::int64_t last_model_check_ms = 0;
        std::int64_t last_selection_check_ms = 0;
        std::uint64_t current_model_version = initial->model_version;
        std::uint64_t current_selector_generation = 0;
        for (const auto& market : markets) {
            current_selector_generation = std::max(
                current_selector_generation, market->cold.selector_generation);
        }
        const fs::path kill_path = fs::path(options.run_root) / "control" / "KILL";
        const fs::path maker_freeze_path =
            fs::path(options.run_root) / "control" / "MAKER_FREEZE";
        const fs::path cutover_drain_path =
            fs::path(options.run_root) / "control" / "CUTOVER_DRAIN";
        const fs::path maker_rotation_drain_path =
            fs::path(options.run_root) / "control" / "MAKER_ROTATION_DRAIN";
        std::int64_t last_control_check_ms = 0;
        std::int64_t last_external_fair_check_ms = 0;
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
                        fs::exists(maker_freeze_path) || fs::exists(cutover_drain_path)
                            || fs::exists(maker_rotation_drain_path),
                        std::memory_order_release);
                    last_control_check_ms = now;
                }
                if (now - last_external_fair_check_ms >= 50) {
                    try {
                        (void)refresh_external_maker_fair(
                            external_maker_fair, markets, options.model_sha, monotonic_ns());
                    } catch (...) {
                        const auto invalid = std::make_shared<const ExternalMakerSnapshot>();
                        for (const auto& market : markets) {
                            std::atomic_store_explicit(
                                &market->external_fair, invalid, std::memory_order_release);
                        }
                    }
                    last_external_fair_check_ms = now;
                }
                if (now - last_state_ms >= 1000) {
                    writer.write_state(
                        new_risk_frozen.load(std::memory_order_acquire));
                    write_runtime_diagnostics(
                        options.run_root, options.model_sha, shards, fill_funnel,
                        execution->inventory_seed_budget_rejections());
                    last_state_ms = now;
                }
                if (now - last_model_check_ms >= 2000) {
                    try {
                        auto next = std::make_shared<MakerModelSnapshot>(
                            load_model_snapshot(options.maker_policy, options.config,
                                                options.model, options.model_sha));
                        if (next->model_version != current_model_version && model_store->publish(next)) {
                            writer.write_model_reload(
                                current_model_version, next->model_version,
                                hex64(fnv1a(read_file(options.model))));
                            current_model_version = next->model_version;
                        }
                    } catch (...) {
                        // Preserve the last valid immutable champion snapshot.
                    }
                    last_model_check_ms = now;
                }
                if (now - last_selection_check_ms >= 100) {
                    try {
                        (void)refresh_selection_control(
                            options.selection, options.model_sha, markets,
                            current_selector_generation);
                    } catch (...) {
                        // A membership change requires a lane handoff. Keep the
                        // last valid same-membership cell authority in place.
                    }
                    last_selection_check_ms = now;
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
        writer.write_state(true);
        return fatal.load(std::memory_order_relaxed) ? 2 : 0;
    } catch (const std::exception& error) {
        std::cerr << "polymarket_v7_market_maker_runtime: " << error.what() << '\n';
        return 2;
    }
}
