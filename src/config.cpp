#include "pm/config.hpp"
#include "pm/v7_execution_mode.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace pm {
namespace json = boost::json;
namespace {

double jdouble(const json::object& o, const char* key, double d) {
    auto it = o.find(key);
    if (it == o.end()) return d;
    const auto& v = it->value();
    try {
        if (v.is_double()) return v.as_double();
        if (v.is_int64()) return static_cast<double>(v.as_int64());
        if (v.is_uint64()) return static_cast<double>(v.as_uint64());
        if (v.is_string()) return std::stod(std::string(v.as_string()));
    } catch (...) {}
    return d;
}

std::size_t jsize(const json::object& o, const char* key, std::size_t d) {
    return static_cast<std::size_t>(std::max(0.0, jdouble(o, key, static_cast<double>(d))));
}

bool jbool(const json::object& o, const char* key, bool d) {
    auto it = o.find(key);
    return it != o.end() && it->value().is_bool() ? it->value().as_bool() : d;
}

std::string jstring(const json::object& o, const char* key, const std::string& d) {
    auto it = o.find(key);
    return it != o.end() && it->value().is_string() ? std::string(it->value().as_string()) : d;
}

} // namespace

Config load_config(const std::string& path) {
    Config c;
    std::ifstream f(path);
    if (!f) throw std::runtime_error("Cannot open config: " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    auto root = json::parse(ss.str());
    if (!root.is_object()) throw std::runtime_error("Config must be JSON object");
    const auto& o = root.as_object();

    if (const auto mode_text = jstring(o, "execution_mode", ""); !mode_text.empty()) {
        const auto mode = v7::parse_execution_mode(mode_text);
        if (!mode) throw std::runtime_error("unknown execution_mode");
        const bool paper_only = jbool(o, "paper_only", false);
        const auto v7_it = o.find("v7");
        const json::object* v7_policy = v7_it != o.end() && v7_it->value().is_object()
            ? &v7_it->value().as_object() : nullptr;
        const bool authenticated = v7_policy != nullptr
            ? jbool(*v7_policy, "authenticated_execution", false) : false;
        const bool submission = v7_policy != nullptr
            ? jbool(*v7_policy, "real_order_submission", false) : false;
        if (!v7::legacy_execution_flags_match(*mode, paper_only, authenticated, submission)) {
            throw std::runtime_error("execution_mode conflicts with legacy authority flags");
        }
        if (v7_policy != nullptr && jstring(*v7_policy, "execution_mode", mode_text) != mode_text) {
            throw std::runtime_error("root and v7 execution_mode disagree");
        }
    }

    c.gamma_url = jstring(o, "gamma_url", c.gamma_url);
    c.clob_url = jstring(o, "clob_url", c.clob_url);
    c.run_dir = jstring(o, "run_dir", c.run_dir);
    c.external_signals_file = jstring(o, "external_signals_file", c.external_signals_file);
    c.market_limit = jsize(o, "market_limit", c.market_limit);
    c.gamma_page_size = jsize(o, "gamma_page_size", c.gamma_page_size);
    c.books_batch_size = jsize(o, "books_batch_size", c.books_batch_size);
    c.interval_seconds = static_cast<int>(jdouble(o, "interval_seconds", c.interval_seconds));
    c.starting_capital = jdouble(o, "starting_capital", c.starting_capital);
    c.min_liquidity = jdouble(o, "min_liquidity", c.min_liquidity);
    c.min_volume24h = jdouble(o, "min_volume24h", c.min_volume24h);
    c.min_mid = jdouble(o, "min_mid", c.min_mid);
    c.max_mid = jdouble(o, "max_mid", c.max_mid);
    c.max_spread = jdouble(o, "max_spread", c.max_spread);
    c.min_net_edge = jdouble(o, "min_net_edge", c.min_net_edge);
    c.uncertainty_penalty = jdouble(o, "uncertainty_penalty", c.uncertainty_penalty);
    c.slippage_bps = jdouble(o, "slippage_bps", c.slippage_bps);
    c.fractional_kelly = jdouble(o, "fractional_kelly", c.fractional_kelly);
    c.max_trade_usd = jdouble(o, "max_trade_usd", c.max_trade_usd);
    c.max_market_fraction = jdouble(o, "max_market_fraction", c.max_market_fraction);
    c.max_event_fraction = jdouble(o, "max_event_fraction", c.max_event_fraction);
    c.max_gross_fraction = jdouble(o, "max_gross_fraction", c.max_gross_fraction);
    c.max_drawdown = jdouble(o, "max_drawdown", c.max_drawdown);
    c.history_bootstrap = jbool(o, "history_bootstrap", c.history_bootstrap);
    c.bootstrap_market_limit = jsize(o, "bootstrap_market_limit", c.bootstrap_market_limit);
    c.history_lookback_hours = jsize(o, "history_lookback_hours", c.history_lookback_hours);
    c.history_fidelity_minutes = jsize(o, "history_fidelity_minutes", c.history_fidelity_minutes);
    c.pca_window = jsize(o, "pca_window", c.pca_window);
    c.pca_min_history = jsize(o, "pca_min_history", c.pca_min_history);
    c.pca_universe = jsize(o, "pca_universe", c.pca_universe);
    c.pca_mr_strength = jdouble(o, "pca_mr_strength", c.pca_mr_strength);
    c.pca_min_residual_z = jdouble(o, "pca_min_residual_z", c.pca_min_residual_z);
    c.graph_max_sum_error = jdouble(o, "graph_max_sum_error", c.graph_max_sum_error);
    c.semantic_min_similarity = jdouble(o, "semantic_min_similarity", c.semantic_min_similarity);
    c.semantic_shrink = jdouble(o, "semantic_shrink", c.semantic_shrink);
    c.scan_only = jbool(o, "scan_only", c.scan_only);

    if (auto it = o.find("expert_weights"); it != o.end() && it->value().is_object()) {
        for (const auto& kv : it->value().as_object()) {
            const std::string name(kv.key());
            c.expert_weights[name] = jdouble(it->value().as_object(), name.c_str(), c.expert_weights[name]);
        }
    }
    if (c.min_mid >= c.max_mid) throw std::runtime_error("min_mid must be below max_mid");
    return c;
}

} // namespace pm
