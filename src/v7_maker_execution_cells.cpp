#include "pm/v7_maker_hft.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <string_view>

namespace pm::v7::maker {
namespace {
namespace fs = std::filesystem;
namespace json = boost::json;

constexpr double kFillOrderShrinkage = 40.0;
constexpr double kMarkoutShrinkage = 20.0;
constexpr double kClusterShrinkage = 5.0;

[[nodiscard]] bool exact_sha(std::string_view value) noexcept {
    if (value.size() != 40) return false;
    for (const char ch : value) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

[[nodiscard]] std::string trim(std::string value) {
    while (!value.empty() && (value.back() == '\n' || value.back() == '\r'
                              || value.back() == ' ' || value.back() == '\t')) {
        value.pop_back();
    }
    const auto first = value.find_first_not_of(" \t\r\n");
    return first == std::string::npos ? std::string{} : value.substr(first);
}

[[nodiscard]] std::string read_text(const fs::path& path) {
    std::ifstream input(path);
    if (!input) return {};
    std::ostringstream out;
    out << input.rdbuf();
    return out.str();
}

[[nodiscard]] std::string expected_sha() {
    if (const char* value = std::getenv("PM_V7_MODEL_SHA"); value != nullptr) {
        const std::string sha = trim(value);
        if (exact_sha(sha)) return sha;
    }

    fs::path git_dir = ".git";
    if (fs::is_regular_file(git_dir)) {
        const std::string link = trim(read_text(git_dir));
        constexpr std::string_view prefix = "gitdir:";
        if (link.rfind(prefix, 0) == 0) {
            git_dir = trim(link.substr(prefix.size()));
        }
    }
    const std::string head = trim(read_text(git_dir / "HEAD"));
    if (exact_sha(head)) return head;
    constexpr std::string_view ref_prefix = "ref:";
    if (head.rfind(ref_prefix, 0) != 0) return {};
    const std::string ref = trim(head.substr(ref_prefix.size()));
    const std::string loose = trim(read_text(git_dir / ref));
    if (exact_sha(loose)) return loose;

    const std::string packed = read_text(git_dir / "packed-refs");
    std::istringstream lines(packed);
    std::string line;
    while (std::getline(lines, line)) {
        line = trim(line);
        if (line.empty() || line.front() == '#' || line.front() == '^') continue;
        const auto space = line.find(' ');
        if (space == std::string::npos) continue;
        if (line.substr(space + 1) == ref && exact_sha(line.substr(0, space))) {
            return line.substr(0, space);
        }
    }
    return {};
}

[[nodiscard]] fs::path execution_model_path() {
    if (const char* value = std::getenv("PM_V7_MAKER_EXECUTION_MODEL"); value != nullptr && *value) {
        return value;
    }
    if (const char* root = std::getenv("PM_V7_RUN_ROOT"); root != nullptr && *root) {
        return fs::path(root) / "micro_maker" / "execution_model.json";
    }
    return fs::path("runs/paper_v7_live") / "micro_maker" / "execution_model.json";
}

[[nodiscard]] fs::path maker_policy_path() {
    if (const char* value = std::getenv("PM_V7_MAKER_POLICY"); value != nullptr && *value) {
        return value;
    }
    return fs::path("config/v7_professional_market_maker.json");
}

[[nodiscard]] const json::value* find_value(const json::object& object,
                                             std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] double number(const json::value* value, double fallback) noexcept {
    if (value == nullptr) return fallback;
    if (value->is_double()) return value->as_double();
    if (value->is_int64()) return static_cast<double>(value->as_int64());
    if (value->is_uint64()) return static_cast<double>(value->as_uint64());
    return fallback;
}

[[nodiscard]] std::uint32_t count(const json::value* value) noexcept {
    const double raw = std::max(0.0, number(value, 0.0));
    return static_cast<std::uint32_t>(std::min<double>(
        raw, static_cast<double>(std::numeric_limits<std::uint32_t>::max())));
}

[[nodiscard]] bool boolean(const json::value* value, bool fallback) noexcept {
    return value != nullptr && value->is_bool() ? value->as_bool() : fallback;
}

[[nodiscard]] std::string text(const json::value* value) {
    return value != nullptr && value->is_string() ? std::string(value->as_string()) : std::string{};
}

[[nodiscard]] const json::object* object_child(const json::object& object,
                                                std::string_view key) noexcept {
    const auto* value = find_value(object, key);
    return value != nullptr && value->is_object() ? &value->as_object() : nullptr;
}

[[nodiscard]] bool actions_include_join(const json::object& exploration) noexcept {
    const auto* actions = find_value(exploration, "actions");
    if (actions == nullptr || !actions->is_array()) return false;
    for (const auto& item : actions->as_array()) {
        if (item.is_string() && item.as_string() == "JOIN") return true;
    }
    return false;
}

void populate_exploration_policy(MakerModelSnapshot& model) noexcept {
    try {
        const fs::path path = maker_policy_path();
        if (path.empty() || !fs::exists(path) || !fs::is_regular_file(path)) return;
        const std::string payload = read_text(path);
        if (payload.empty()) return;

        boost::system::error_code error;
        const auto root = json::parse(payload, error);
        if (error || !root.is_object()) return;
        const auto& policy = root.as_object();
        if (!boolean(find_value(policy, "paper_only"), false)
            || boolean(find_value(policy, "authenticated_execution"), true)
            || boolean(find_value(policy, "real_order_submission"), true)
            || text(find_value(policy, "mode")) != "explore_then_exploit") {
            return;
        }
        const auto* exploration = object_child(policy, "exploration");
        if (exploration == nullptr || !boolean(find_value(*exploration, "enabled"), false)
            || !actions_include_join(*exploration)) {
            return;
        }

        const double epsilon = std::clamp(number(find_value(*exploration, "epsilon"), 0.0), 0.0, 1.0);
        const double confidence_z = number(find_value(*exploration, "confidence_z"), -1.0);
        const double configured_quote_fraction = std::clamp(
            number(find_value(*exploration, "max_quote_notional_fraction"), 0.0), 0.0, 1.0);
        const double market_fraction = std::clamp(
            number(find_value(*exploration, "max_market_fraction"), 0.0), 0.0, 1.0);
        const double capital_fraction = std::clamp(
            number(find_value(*exploration, "max_capital_fraction"), 0.0), 0.0, 1.0);
        const auto* selection = object_child(policy, "market_selection");
        const std::uint32_t selected_market_capacity = static_cast<std::uint32_t>(std::max(
            0.0, number(selection == nullptr ? nullptr
                : find_value(*selection, "max_active_markets"), 0.0)));
        // One eligible binary market can have at most two exploratory BUY quotes
        // (YES and NO). Shrink the per-quote cap so both together remain within
        // the configured per-market bound, then derive a hard market-count cap
        // so the worst case also remains within max_capital_fraction.
        const double quote_fraction = std::min(
            configured_quote_fraction, market_fraction > 0.0 ? 0.5 * market_fraction : 0.0);
        std::uint32_t max_markets = 0;
        if (quote_fraction > 0.0 && capital_fraction > 0.0) {
            max_markets = static_cast<std::uint32_t>(std::min<double>(
                selected_market_capacity,
                std::floor(capital_fraction / (2.0 * quote_fraction) + 1e-12)));
        }
        const double min_rest_ms = std::max(0.0, number(find_value(*exploration, "minimum_rest_ms"), 250.0));
        const double max_rest_ms = std::max(min_rest_ms, number(find_value(*exploration, "maximum_rest_ms"), 3000.0));
        const double persistent_fraction = std::clamp(number(
            find_value(*exploration, "persistent_quote_fraction"), 0.0), 0.0, 1.0);
        const double persistent_max_rest_ms = std::max(max_rest_ms, number(
            find_value(*exploration, "persistent_maximum_rest_ms"), max_rest_ms));

        if (epsilon <= 0.0 || !std::isfinite(confidence_z) || confidence_z < 0.0
            || confidence_z > std::max(0.0, model.robust_ev_z)
            || quote_fraction <= 0.0 || max_markets == 0
            || selected_market_capacity == 0) return;
        model.exploration_confidence_z = confidence_z;
        model.exploration_epsilon = epsilon;
        model.exploration_quote_notional_fraction = quote_fraction;
        model.exploration_max_active_markets = selected_market_capacity;
        model.exploration_concurrent_market_cap = max_markets;
        model.exploration_min_rest_ns = static_cast<std::int64_t>(std::llround(min_rest_ms * 1'000'000.0));
        model.exploration_max_rest_ns = static_cast<std::int64_t>(std::llround(max_rest_ms * 1'000'000.0));
        model.exploration_persistent_fraction = persistent_fraction;
        model.exploration_persistent_max_rest_ns = static_cast<std::int64_t>(
            std::llround(persistent_max_rest_ms * 1'000'000.0));
        model.exploration_enabled = 1;
    } catch (...) {
        // Exploration is opt-in and PAPER-only. Any malformed policy disables it
        // rather than changing normal exploit behavior.
        model.exploration_enabled = 0;
        model.exploration_confidence_z = 0.0;
        model.exploration_epsilon = 0.0;
        model.exploration_quote_notional_fraction = 0.0;
        model.exploration_persistent_fraction = 0.0;
        model.exploration_max_active_markets = 0;
        model.exploration_concurrent_market_cap = 0;
    }
}

[[nodiscard]] Action parse_action(std::string_view value) noexcept {
    if (value == "JOIN") return Action::Join;
    if (value == "IMPROVE1") return Action::Improve1;
    if (value == "FADE1") return Action::Fade1;
    if (value == "FADE2") return Action::Fade2;
    return Action::Withdraw;
}

[[nodiscard]] Side parse_side(std::string_view value) noexcept {
    if (value == "BUY") return Side::Buy;
    if (value == "SELL") return Side::Sell;
    return Side::None;
}

[[nodiscard]] double evidence_weight(std::uint32_t observations,
                                     double shrinkage) noexcept {
    const double n = static_cast<double>(observations);
    return n > 0.0 ? n / (n + shrinkage) : 0.0;
}

void populate_execution_cells(MakerModelSnapshot& model) noexcept {
    try {
        const std::string sha = expected_sha();
        if (!exact_sha(sha)) return;
        const fs::path path = execution_model_path();
        if (!fs::exists(path) || !fs::is_regular_file(path)) return;
        const std::string payload = read_text(path);
        if (payload.empty()) return;

        boost::system::error_code error;
        const auto root = json::parse(payload, error);
        if (error || !root.is_object()) return;
        const auto& object = root.as_object();
        if (text(find_value(object, "model_sha")) != sha
            || !boolean(find_value(object, "paper_only"), false)
            || boolean(find_value(object, "authenticated_execution"), true)) {
            return;
        }
        const auto* groups_value = find_value(object, "groups");
        if (groups_value == nullptr || !groups_value->is_object()) return;
        const auto& groups = groups_value->as_object();
        const auto global_it = groups.find("GLOBAL");
        if (global_it == groups.end() || !global_it->value().is_object()) return;
        const auto& global = global_it->value().as_object();
        const double global_fill = std::clamp(
            number(find_value(global, "fill_probability"), 0.02), 1e-6, 1.0 - 1e-6);
        const double global_adverse = std::max(
            0.0, number(find_value(global, "adverse_markout_per_share"), 0.002));

        for (const auto& item : groups) {
            const std::string key(item.key());
            if (key == "GLOBAL") continue;
            const auto first = key.find('|');
            const auto second = first == std::string::npos ? std::string::npos : key.find('|', first + 1);
            if (first == std::string::npos || second == std::string::npos) continue;
            const Action action = parse_action(std::string_view(key).substr(0, first));
            const std::string_view outcome = std::string_view(key).substr(first + 1, second - first - 1);
            const Side side = parse_side(std::string_view(key).substr(second + 1));
            const std::int8_t sign = outcome == "YES" ? 1 : outcome == "NO" ? -1 : 0;
            const std::size_t index = execution_cell_index(action, sign, side);
            if (index >= model.execution_cells.size() || !item.value().is_object()) continue;

            const auto& group = item.value().as_object();
            const std::uint32_t orders = count(find_value(group, "orders"));
            const std::uint32_t fills = count(find_value(group, "filled_orders"));
            const std::uint32_t order_clusters = count(find_value(group, "event_clusters"));
            const std::uint32_t markout_clusters = count(
                find_value(group, "adverse_markout_event_clusters"));
            const std::uint32_t clusters = std::max(order_clusters, markout_clusters);
            const auto* markout_observations = find_value(
                group, "adverse_markout_observations");
            // Read the canonical durable-learning field while retaining the
            // legacy name for old test/replay artifacts.
            const std::uint32_t markouts = count(markout_observations != nullptr
                ? markout_observations : find_value(group, "adverse_markout_n"));

            const double raw_fill = std::clamp(
                number(find_value(group, "fill_probability"), global_fill), 1e-6, 1.0 - 1e-6);
            const double raw_adverse = std::max(
                0.0, number(find_value(group, "adverse_markout_per_share"), global_adverse));
            const double cluster_weight = evidence_weight(clusters, kClusterShrinkage);
            const double fill_weight = std::min(
                evidence_weight(orders, kFillOrderShrinkage), cluster_weight);
            const double markout_weight = std::min(
                evidence_weight(markouts, kMarkoutShrinkage), cluster_weight);

            auto& cell = model.execution_cells[index];
            cell.fill_probability = global_fill + fill_weight * (raw_fill - global_fill);
            // The durable fitter already applies filled-share Bayesian
            // shrinkage to adverse markout. Applying another observation and
            // cluster shrinkage here erased the very risk signal the cell was
            // meant to carry. Keep markout_weight as an uncertainty diagnostic,
            // but consume the fitted posterior directly when it exists.
            cell.adverse_markout_per_share = markouts > 0 ? raw_adverse : global_adverse;
            cell.fill_weight = fill_weight;
            cell.markout_weight = markout_weight;
            cell.orders = orders;
            cell.filled_orders = fills;
            cell.adverse_markouts = markouts;
            cell.event_clusters = clusters;
            cell.valid = static_cast<std::uint8_t>(orders > 0 || markouts > 0);
        }
    } catch (...) {
        // Exact-SHA execution cells are optional evidence. A malformed, stale or
        // unavailable slow-plane file leaves every cell invalid so the kernel
        // falls back to the already validated GLOBAL snapshot.
    }
}

} // namespace

std::size_t execution_cell_index(Action action, std::int8_t instrument_inventory_sign,
                                 Side side) noexcept {
    std::size_t action_index = kExecutionActionCount;
    switch (action) {
        case Action::Join: action_index = 0; break;
        case Action::Improve1: action_index = 1; break;
        case Action::Fade1: action_index = 2; break;
        case Action::Fade2: action_index = 3; break;
        case Action::OneSided:
        case Action::Withdraw: return kExecutionCellCount;
    }
    const std::size_t outcome_index = instrument_inventory_sign > 0 ? 0
        : instrument_inventory_sign < 0 ? 1 : kExecutionOutcomeCount;
    const std::size_t side_index = side == Side::Buy ? 0
        : side == Side::Sell ? 1 : kExecutionSideCount;
    if (action_index >= kExecutionActionCount || outcome_index >= kExecutionOutcomeCount
        || side_index >= kExecutionSideCount) {
        return kExecutionCellCount;
    }
    return (action_index * kExecutionOutcomeCount + outcome_index) * kExecutionSideCount
        + side_index;
}

MakerModelSnapshot::MakerModelSnapshot() noexcept {
    populate_exploration_policy(*this);
    populate_execution_cells(*this);
}

} // namespace pm::v7::maker
