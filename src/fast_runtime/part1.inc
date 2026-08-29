#include "pm/api.hpp"
#include "pm/config.hpp"
#include "pm/fast_arb.hpp"
#include "pm/fast_ws.hpp"
#include "pm/http.hpp"
#include "pm/market_data.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;

std::atomic<bool> g_stop{false};

void handle_signal(int) { g_stop.store(true, std::memory_order_relaxed); }

std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::int64_t now_s() { return now_ms() / 1000; }

std::string text(const json::value& value) {
    if (value.is_string()) return std::string(value.as_string());
    if (value.is_int64()) return std::to_string(value.as_int64());
    if (value.is_uint64()) return std::to_string(value.as_uint64());
    if (value.is_double()) {
        std::ostringstream out;
        out << std::setprecision(17) << value.as_double();
        return out.str();
    }
    return {};
}

double number(const json::value& value, double fallback = 0.0) {
    try {
        if (value.is_double()) return value.as_double();
        if (value.is_int64()) return static_cast<double>(value.as_int64());
        if (value.is_uint64()) return static_cast<double>(value.as_uint64());
        if (value.is_string()) return std::stod(std::string(value.as_string()));
    } catch (...) {
    }
    return fallback;
}

std::int64_t integer(const json::value& value, std::int64_t fallback = 0) {
    try {
        if (value.is_int64()) return value.as_int64();
        if (value.is_uint64()) return static_cast<std::int64_t>(value.as_uint64());
        if (value.is_double()) return static_cast<std::int64_t>(value.as_double());
        if (value.is_string()) return std::stoll(std::string(value.as_string()));
    } catch (...) {
    }
    return fallback;
}

bool boolean(const json::value& value, bool fallback = false) {
    if (value.is_bool()) return value.as_bool();
    if (value.is_int64()) return value.as_int64() != 0;
    if (value.is_uint64()) return value.as_uint64() != 0;
    if (value.is_string()) {
        auto raw = std::string(value.as_string());
        std::transform(raw.begin(), raw.end(), raw.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        if (raw == "true" || raw == "1") return true;
        if (raw == "false" || raw == "0") return false;
    }
    return fallback;
}

std::string get_text(const json::object& object, const char* key) {
    const auto it = object.find(key);
    return it == object.end() ? std::string{} : text(it->value());
}

double get_number(const json::object& object, const char* key, double fallback = 0.0) {
    const auto it = object.find(key);
    return it == object.end() ? fallback : number(it->value(), fallback);
}

std::int64_t get_integer(const json::object& object, const char* key,
                         std::int64_t fallback = 0) {
    const auto it = object.find(key);
    return it == object.end() ? fallback : integer(it->value(), fallback);
}

std::string csv_escape(const std::string& value) {
    if (value.find_first_of(",\"\n\r") == std::string::npos) return value;
    std::string out = "\"";
    for (char c : value) out += c == '"' ? "\"\"" : std::string(1, c);
    return out + '"';
}

std::vector<std::string> split_csv(const std::string& line) {
    std::vector<std::string> fields;
    std::string current;
    bool quoted = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (c == '"') {
            if (quoted && i + 1 < line.size() && line[i + 1] == '"') {
                current.push_back('"');
                ++i;
            } else {
                quoted = !quoted;
            }
        } else if (c == ',' && !quoted) {
            fields.push_back(current);
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    fields.push_back(current);
    return fields;
}

std::int64_t normalize_timestamp_ms(std::int64_t timestamp) {
    if (timestamp <= 0) return 0;
    if (timestamp < 100000000000LL) return timestamp * 1000;
    return timestamp;
}

double quantile(std::deque<double> values, double probability) {
    if (values.empty()) return 0.0;
    std::sort(values.begin(), values.end());
    const double index = std::clamp(probability, 0.0, 1.0) *
                         static_cast<double>(values.size() - 1);
    const auto lo = static_cast<std::size_t>(std::floor(index));
    const auto hi = static_cast<std::size_t>(std::ceil(index));
    const double weight = index - static_cast<double>(lo);
    return values[lo] * (1.0 - weight) + values[hi] * weight;
}

void atomic_write(const fs::path& path, const std::string& content) {
    fs::create_directories(path.parent_path());
    const fs::path temporary = path.string() + ".tmp";
    {
        std::ofstream out(temporary, std::ios::trunc);
        if (!out) throw std::runtime_error("cannot write " + temporary.string());
        out << content;
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

struct Options {
    std::string config = "config/paper_v7.json";
    std::string policy = "config/v7_fast_structural.json";
    std::string relations = "config/v7_fast_structural_relations.csv";
    std::string external_signals;
    std::string run_dir = "runs/paper_v7_live/fast";
    std::string run_root = "runs/paper_v7_live";
    std::string model_sha;
    std::string ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
    std::size_t markets = 1000;
    std::size_t shard_size = 200;
    double min_liquidity = 2.0;
    int snapshot_refresh_seconds = 30;
    int status_interval_seconds = 1;
    int duration_seconds = 0;
    // The canonical service owns process supervision. A timed self-exit is
    // indistinguishable from a component crash to the single-writer runtime
    // and previously quarantined the whole PAPER stack every 15 minutes.
    int recycle_seconds = 0;
    std::size_t latency_sample_every = 20;
};

bool exact_sha(const std::string& value) {
    return value.size() == 40 && std::all_of(value.begin(), value.end(), [](unsigned char c) {
        return std::isdigit(c) || (c >= 'a' && c <= 'f');
    });
}

pm::fast::Policy load_policy(const std::string& path) {
    pm::fast::Policy policy;
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open fast-arb policy: " + path);
    std::stringstream buffer;
    buffer << input.rdbuf();
    const auto root = json::parse(buffer.str());
    if (!root.is_object()) throw std::runtime_error("fast-arb policy must be a JSON object");
    const auto& object = root.as_object();
    if (const auto it = object.find("real_order_submission");
        it != object.end() && boolean(it->value(), false)) {
        throw std::runtime_error("V7 Fast Structural PAPER refuses real_order_submission=true");
    }
    policy.min_net_edge = get_number(object, "min_net_edge", policy.min_net_edge);
    policy.slippage_bps = get_number(object, "slippage_bps", policy.slippage_bps);
    policy.latency_penalty_bps =
        get_number(object, "latency_penalty_bps", policy.latency_penalty_bps);
    policy.max_notional_usd =
        get_number(object, "max_notional_usd", policy.max_notional_usd);
    policy.min_executable_shares =
        get_number(object, "min_executable_shares", policy.min_executable_shares);
    policy.external_uncertainty_penalty = get_number(
        object, "external_uncertainty_penalty", policy.external_uncertainty_penalty);
    policy.max_external_age_ms = get_integer(
        object, "max_external_age_ms", policy.max_external_age_ms);
    policy.maker_one_sided_penalty_bps = get_number(
        object, "maker_one_sided_penalty_bps", policy.maker_one_sided_penalty_bps);
    policy.maker_improve_ticks = static_cast<int>(get_integer(
        object, "maker_improve_ticks", policy.maker_improve_ticks));
    policy.conversion_fixed_cost_usd = get_number(
        object, "conversion_fixed_cost_usd", policy.conversion_fixed_cost_usd);
    if (policy.min_net_edge < 0.0 || policy.max_notional_usd <= 0.0 ||
        policy.min_executable_shares <= 0.0 || policy.max_external_age_ms <= 0) {
        throw std::runtime_error("invalid fast-arb policy limits");
    }
    return policy;
}

enum class RelationType { Implication, MutualExclusion, ExhaustivePair };

struct RelationSpec {
    RelationType type = RelationType::Implication;
    pm::fast::ImplicationRelation relation;
};

std::vector<RelationSpec> load_relations(const std::string& path) {
    std::vector<RelationSpec> out;
    std::ifstream input(path);
    if (!input) return out;
    std::string line;
    std::getline(input, line);
    while (std::getline(input, line)) {
        const auto fields = split_csv(line);
        if (fields.size() < 5) continue;
        std::string type = fields[1];
        std::transform(type.begin(), type.end(), type.begin(), [](unsigned char c) {
            return static_cast<char>(std::toupper(c));
        });
        std::string enabled = fields[4];
        std::transform(enabled.begin(), enabled.end(), enabled.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        if (enabled != "1" && enabled != "true" && enabled != "yes") continue;
        RelationType relation_type;
        if (type == "IMPLICATION") relation_type = RelationType::Implication;
        else if (type == "MUTUAL_EXCLUSION") relation_type = RelationType::MutualExclusion;
        else if (type == "EXHAUSTIVE_PAIR") relation_type = RelationType::ExhaustivePair;
        else if (type == "EQUIVALENCE") {
            out.push_back(RelationSpec{
                RelationType::Implication,
                {fields[0] + ":forward", fields[2], fields[3]},
            });
            out.push_back(RelationSpec{
                RelationType::Implication,
                {fields[0] + ":reverse", fields[3], fields[2]},
            });
            continue;
        } else {
            continue;
        }
        out.push_back(RelationSpec{relation_type, {fields[0], fields[2], fields[3]}});
    }
    return out;
}

std::unordered_map<std::string, pm::ExternalSignal> load_external_signals(
    const std::string& path) {
    std::unordered_map<std::string, pm::ExternalSignal> out;
    std::ifstream input(path);
    if (!input) return out;
    std::string line;
    std::getline(input, line);
    while (std::getline(input, line)) {
        const auto fields = split_csv(line);
        if (fields.size() < 5) continue;
        try {
            pm::ExternalSignal signal{
                std::stod(fields[1]), std::stod(fields[2]), fields[3],
                std::stoll(fields[4]),
            };
            const auto it = out.find(fields[0]);
            if (it == out.end() || normalize_timestamp_ms(signal.timestamp) >
                                       normalize_timestamp_ms(it->second.timestamp)) {
                out[fields[0]] = std::move(signal);
            }
        } catch (...) {
        }
    }
    return out;
}
