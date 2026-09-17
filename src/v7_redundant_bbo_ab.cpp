#include "pm/v7_redundant_bbo_feed.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <map>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

using pm::v7::polymarket_bbo::Binding;
using pm::v7::redundant_bbo::Decision;
using pm::v7::redundant_bbo::Feed;
using pm::v7::redundant_bbo::Mode;

namespace {
struct Key {
    std::int64_t exchange_ns = 0;
    std::int32_t bid_e4 = 0;
    std::int32_t ask_e4 = 0;
    auto operator<=>(const Key&) const = default;
};

double quantile_us(std::vector<std::int64_t> values, double q) {
    if (values.empty()) return 0.0;
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(q * static_cast<double>(values.size() - 1));
    return static_cast<double>(values[index]) / 1000.0;
}

bool parse_int(std::string_view text, int& value) {
    try {
        std::size_t pos = 0;
        const int parsed = std::stoi(std::string(text), &pos);
        if (pos != text.size()) return false;
        value = parsed;
        return true;
    } catch (...) {
        return false;
    }
}

bool numeric_asset(std::string_view asset) {
    return !asset.empty() && asset.size() <= 96
        && std::all_of(asset.begin(), asset.end(), [](unsigned char c) { return c >= '0' && c <= '9'; });
}
}

int main(int argc, char** argv) {
    std::string asset_id;
    int seconds = 45;
    int warmup_seconds = 5;
    int candidate_busy_poll_us = 50;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg(argv[i]);
        if (arg == "--asset-id" && i + 1 < argc) asset_id = argv[++i];
        else if (arg == "--seconds" && i + 1 < argc) {
            if (!parse_int(argv[++i], seconds)) return 64;
        } else if (arg == "--warmup-seconds" && i + 1 < argc) {
            if (!parse_int(argv[++i], warmup_seconds)) return 64;
        } else if (arg == "--candidate-busy-poll-us" && i + 1 < argc) {
            if (!parse_int(argv[++i], candidate_busy_poll_us)) return 64;
        } else {
            std::cerr << "invalid argument\n";
            return 64;
        }
    }
    if (!numeric_asset(asset_id) || seconds < 10 || seconds > 600
        || warmup_seconds < 0 || warmup_seconds > 60
        || candidate_busy_poll_us <= 0 || candidate_busy_poll_us > 2000) {
        std::cerr << "invalid benchmark parameters\n";
        return 64;
    }

    constexpr std::string_view endpoint = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
    const std::vector<Binding> bindings{{asset_id, 1, 1, 1}};
    Feed baseline(std::string(endpoint), bindings, Mode::Quorum2Of3, 0);
    Feed candidate(std::string(endpoint), bindings, Mode::Quorum2Of3, candidate_busy_poll_us);
    baseline.start();
    candidate.start();
    std::cerr << "V7_BBO_AB_PHASE=warmup_start\n";

    const auto drain_until = [&](std::chrono::steady_clock::time_point deadline) {
        Decision ignored{};
        while (std::chrono::steady_clock::now() < deadline) {
            bool worked = baseline.try_next_actionable(ignored);
            worked = candidate.try_next_actionable(ignored) || worked;
            if (!worked) std::this_thread::yield();
        }
    };
    drain_until(std::chrono::steady_clock::now() + std::chrono::seconds(warmup_seconds));
    std::cerr << "V7_BBO_AB_PHASE=capture_start\n";

    std::map<Key, std::int64_t> base_pending;
    std::map<Key, std::int64_t> candidate_pending;
    std::vector<std::int64_t> deltas_ns;
    std::vector<std::int64_t> base_quorum_wait_ns;
    std::vector<std::int64_t> candidate_quorum_wait_ns;
    std::uint64_t base_actions = 0;
    std::uint64_t candidate_actions = 0;
    std::uint64_t candidate_wins = 0;

    const auto capture = [&](bool is_candidate, const Decision& d) {
        const Key key{d.update.exchange_event_ns, d.update.best_bid_e4, d.update.best_ask_e4};
        auto& own = is_candidate ? candidate_pending : base_pending;
        auto& other = is_candidate ? base_pending : candidate_pending;
        if (is_candidate) {
            ++candidate_actions;
            candidate_quorum_wait_ns.push_back(d.ready_monotonic_ns - d.first_receive_monotonic_ns);
        } else {
            ++base_actions;
            base_quorum_wait_ns.push_back(d.ready_monotonic_ns - d.first_receive_monotonic_ns);
        }
        const auto it = other.find(key);
        if (it == other.end()) {
            own[key] = d.ready_monotonic_ns;
            return;
        }
        const std::int64_t delta = is_candidate
            ? d.ready_monotonic_ns - it->second
            : it->second - d.ready_monotonic_ns;
        deltas_ns.push_back(delta);
        if (delta < 0) ++candidate_wins;
        other.erase(it);
        if (own.size() > 4096) own.erase(own.begin());
    };

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
    while (std::chrono::steady_clock::now() < deadline) {
        bool worked = false;
        Decision d{};
        if (baseline.try_next_actionable(d)) { capture(false, d); worked = true; }
        if (candidate.try_next_actionable(d)) { capture(true, d); worked = true; }
        if (!worked) std::this_thread::yield();
    }
    std::cerr << "V7_BBO_AB_PHASE=capture_end\n";
    baseline.stop();
    candidate.stop();
    std::cerr << "V7_BBO_AB_PHASE=stopped\n";

    const auto b = baseline.snapshot();
    const auto c = candidate.snapshot();
    const bool transport_ok = b.disabled_mask == 0 && c.disabled_mask == 0;
    const bool semantic_ok = b.gate.conflicts == 0 && c.gate.conflicts == 0
        && b.gate.post_emit_conflicts == 0 && c.gate.post_emit_conflicts == 0;
    const double win_rate = deltas_ns.empty() ? 0.0
        : static_cast<double>(candidate_wins) / static_cast<double>(deltas_ns.size());

    std::cout << std::fixed << std::setprecision(3)
        << "V7_REDUNDANT_BBO_AB={"
        << "\"paper_only\":true,\"authenticated_execution\":false,\"real_order_submission\":false,"
        << "\"asset_id\":\"" << asset_id << "\","
        << "\"seconds\":" << seconds << ",\"warmup_seconds\":" << warmup_seconds << ','
        << "\"candidate_busy_poll_us\":" << candidate_busy_poll_us << ','
        << "\"matched\":" << deltas_ns.size() << ','
        << "\"baseline_actions\":" << base_actions << ','
        << "\"candidate_actions\":" << candidate_actions << ','
        << "\"candidate_win_rate\":" << win_rate << ','
        << "\"delta_us_p50\":" << quantile_us(deltas_ns, 0.50) << ','
        << "\"delta_us_p95\":" << quantile_us(deltas_ns, 0.95) << ','
        << "\"delta_us_p99\":" << quantile_us(deltas_ns, 0.99) << ','
        << "\"delta_us_p999\":" << quantile_us(deltas_ns, 0.999) << ','
        << "\"baseline_quorum_wait_us_p99\":" << quantile_us(base_quorum_wait_ns, 0.99) << ','
        << "\"candidate_quorum_wait_us_p99\":" << quantile_us(candidate_quorum_wait_ns, 0.99) << ','
        << "\"baseline_disabled_mask\":" << static_cast<unsigned>(b.disabled_mask) << ','
        << "\"candidate_disabled_mask\":" << static_cast<unsigned>(c.disabled_mask) << ','
        << "\"baseline_conflicts\":" << b.gate.conflicts << ','
        << "\"candidate_conflicts\":" << c.gate.conflicts << ','
        << "\"transport_ok\":" << (transport_ok ? "true" : "false") << ','
        << "\"semantic_ok\":" << (semantic_ok ? "true" : "false")
        << "}\n";

    if (!transport_ok || !semantic_ok) return 3;
    if (deltas_ns.size() < 10) return 2;
    return 0;
}
