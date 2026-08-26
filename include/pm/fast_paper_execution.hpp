#pragma once

#include "pm/fast_arb.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace pm::fast::paper {

constexpr double kEps = 1e-12;

struct LegState {
    std::string market_id;
    std::string token_id;
    std::string outcome;
    double target_shares = 0.0;
    double max_all_in_unit_cost = 0.0;
    std::int64_t attempted_ts_ms = 0;
    double filled_shares = 0.0;
    double raw_entry_cost = 0.0;
    double entry_all_in_cost = 0.0;
    bool entry_complete = false;
    double raw_exit_proceeds = 0.0;
    double exit_net_proceeds = 0.0;
    bool exit_complete = false;
};

struct Probe {
    std::string model_sha;
    std::string opportunity_id;
    std::string event_id;
    std::string kind;
    std::int64_t detected_ts_ms = 0;
    std::int64_t exchange_ts_ms = 0;
    std::int64_t received_ts_ms = 0;
    std::int64_t decision_ts_ms = 0;
    std::int64_t next_action_ts_ms = 0;
    double target_shares = 0.0;
    double payoff_floor = 0.0;
    std::size_t next_leg = 0;
    bool entry_failed = false;
    bool point_in_time = false;
    bool authoritative_fees = true;
    bool depth_executable = true;
    std::vector<LegState> legs;
};

struct Outcome {
    std::string model_sha;
    std::string opportunity_id;
    std::string event_id;
    std::string kind;
    std::string joint_state;
    std::int64_t decision_ts_ms = 0;
    std::int64_t closed_ts_ms = 0;
    std::size_t target_legs = 0;
    std::size_t filled_legs = 0;
    double target_shares = 0.0;
    double entry_cost = 0.0;
    double exit_proceeds = 0.0;
    std::optional<double> net_pnl;
    std::optional<double> locked_terminal_pnl;
    double explicit_cost = 0.0;
    double capital_seconds = 0.0;
    bool completed_basket = false;
    bool partial_unwind = false;
    bool unwind_accounted = false;
    bool point_in_time = false;
    bool authoritative_fees = false;
    bool depth_executable = false;
    std::vector<LegState> legs;
};

inline bool valid_model_sha(const std::string& value) {
    if (value.size() != 40) return false;
    return std::all_of(value.begin(), value.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    });
}

inline std::optional<Probe> start_probe(const Opportunity& opportunity,
                                        const std::string& model_sha,
                                        std::int64_t detected_ts_ms,
                                        std::int64_t leg_dispatch_delay_ms,
                                        std::int64_t max_feed_age_ms) {
    if (!opportunity.hard_arbitrage || !opportunity.executable || opportunity.legs.empty() ||
        opportunity.executable_shares <= kEps || !valid_model_sha(model_sha)) {
        return std::nullopt;
    }
    const bool causal = opportunity.exchange_ts_ms > 0 && opportunity.received_ts_ms > 0 &&
                        opportunity.received_ts_ms >= opportunity.exchange_ts_ms &&
                        opportunity.decision_ts_ms >= opportunity.received_ts_ms &&
                        detected_ts_ms >= opportunity.decision_ts_ms;
    const bool fresh = causal &&
                       opportunity.received_ts_ms - opportunity.exchange_ts_ms <=
                           std::max<std::int64_t>(0, max_feed_age_ms);
    if (!fresh) return std::nullopt;

    Probe probe;
    probe.model_sha = model_sha;
    probe.opportunity_id = opportunity.id;
    probe.event_id = opportunity.event_id;
    probe.kind = to_string(opportunity.kind);
    probe.detected_ts_ms = detected_ts_ms;
    probe.exchange_ts_ms = opportunity.exchange_ts_ms;
    probe.received_ts_ms = opportunity.received_ts_ms;
    probe.decision_ts_ms = opportunity.decision_ts_ms;
    probe.next_action_ts_ms = detected_ts_ms + std::max<std::int64_t>(1, leg_dispatch_delay_ms);
    probe.target_shares = opportunity.executable_shares;
    probe.payoff_floor = opportunity.payoff_floor;
    probe.point_in_time = true;
    probe.legs.reserve(opportunity.legs.size());
    for (const auto& leg : opportunity.legs) {
        const double cap = leg.all_in_cost / opportunity.executable_shares;
        if (!std::isfinite(cap) || cap <= 0.0 || cap >= 1.5) return std::nullopt;
        probe.legs.push_back(LegState{
            leg.market_id,
            leg.token_id,
            leg.outcome,
            opportunity.executable_shares,
            cap,
        });
    }
    return probe;
}

inline bool entry_due(const Probe& probe, std::int64_t now_ms) {
    return !probe.entry_failed && probe.next_leg < probe.legs.size() &&
           now_ms >= probe.next_action_ts_ms;
}

inline void attempt_entry(Probe& probe, const Book* book, const FeeDetails* fee,
                          const Policy& policy, std::int64_t now_ms,
                          std::int64_t leg_dispatch_delay_ms) {
    if (!entry_due(probe, now_ms)) return;
    auto& leg = probe.legs[probe.next_leg];
    leg.attempted_ts_ms = now_ms;
    if (book == nullptr || fee == nullptr) {
        probe.authoritative_fees = probe.authoritative_fees && fee != nullptr;
        probe.depth_executable = probe.depth_executable && book != nullptr;
        probe.entry_failed = true;
        return;
    }

    auto asks = book->asks;
    asks.erase(std::remove_if(asks.begin(), asks.end(), [](const Level& level) {
        return !std::isfinite(level.price) || !std::isfinite(level.size) ||
               level.price <= 0.0 || level.price >= 1.0 || level.size <= 0.0;
    }), asks.end());
    std::sort(asks.begin(), asks.end(), [](const Level& left, const Level& right) {
        return left.price < right.price;
    });

    double remaining = leg.target_shares;
    for (const auto& level : asks) {
        const double execution_price = std::clamp(
            level.price * (1.0 + std::max(0.0, policy.slippage_bps) / 10000.0),
            1e-9, 1.0 - 1e-9);
        const double unit_cost = execution_price + fee_per_share(execution_price, *fee);
        if (unit_cost > leg.max_all_in_unit_cost + kEps) break;
        const double quantity = std::min(remaining, level.size);
        if (quantity <= 0.0) continue;
        leg.filled_shares += quantity;
        leg.raw_entry_cost += quantity * level.price;
        leg.entry_all_in_cost += quantity * unit_cost;
        remaining -= quantity;
        if (remaining <= kEps) break;
    }
    leg.entry_complete = remaining <= kEps;
    if (!leg.entry_complete) {
        probe.entry_failed = true;
        return;
    }
    ++probe.next_leg;
    if (probe.next_leg < probe.legs.size()) {
        probe.next_action_ts_ms = now_ms + std::max<std::int64_t>(1, leg_dispatch_delay_ms);
    }
}

inline void apply_unwind(LegState& leg, const Book* book, const FeeDetails* fee,
                         const Policy& policy) {
    if (leg.filled_shares <= kEps) {
        leg.exit_complete = true;
        return;
    }
    if (book == nullptr || fee == nullptr) {
        leg.raw_exit_proceeds = 0.0;
        leg.exit_net_proceeds = 0.0;
        leg.exit_complete = false;
        return;
    }
    const auto exit = walk_sell_shares(*book, leg.filled_shares, policy.slippage_bps, *fee);
    leg.raw_exit_proceeds = exit.raw_proceeds;
    leg.exit_net_proceeds = exit.net_proceeds;
    leg.exit_complete = exit.complete;
}

inline Outcome finalize_probe(const Probe& probe, std::int64_t closed_ts_ms) {
    Outcome out;
    out.model_sha = probe.model_sha;
    out.opportunity_id = probe.opportunity_id;
    out.event_id = probe.event_id;
    out.kind = probe.kind;
    out.decision_ts_ms = probe.decision_ts_ms;
    out.closed_ts_ms = closed_ts_ms;
    out.target_legs = probe.legs.size();
    out.target_shares = probe.target_shares;
    out.point_in_time = probe.point_in_time;
    out.authoritative_fees = probe.authoritative_fees;
    out.depth_executable = probe.depth_executable;
    out.legs = probe.legs;

    double raw_entry = 0.0;
    double all_in_entry = 0.0;
    double raw_exit = 0.0;
    double net_exit = 0.0;
    bool all_entries_complete = !probe.legs.empty();
    bool all_exits_complete = true;
    for (const auto& leg : probe.legs) {
        raw_entry += leg.raw_entry_cost;
        all_in_entry += leg.entry_all_in_cost;
        raw_exit += leg.raw_exit_proceeds;
        net_exit += leg.exit_net_proceeds;
        out.filled_legs += leg.filled_shares > kEps ? 1u : 0u;
        all_entries_complete = all_entries_complete && leg.entry_complete;
        if (leg.filled_shares > kEps) all_exits_complete = all_exits_complete && leg.exit_complete;
    }
    out.entry_cost = all_in_entry;
    out.exit_proceeds = net_exit;
    out.explicit_cost = std::max(0.0, all_in_entry - raw_entry) +
                        std::max(0.0, raw_exit - net_exit);
    out.capital_seconds = std::max<std::int64_t>(0, closed_ts_ms - probe.detected_ts_ms) / 1000.0;
    out.completed_basket = all_entries_complete;

    if (all_entries_complete) {
        out.joint_state = "ALL_LEGS_FILLED_OPEN";
        out.locked_terminal_pnl = probe.payoff_floor * probe.target_shares - all_in_entry;
        out.partial_unwind = false;
        out.unwind_accounted = true;
        // Do not call the locked terminal edge realized PnL. A later settlement/close
        // observation is required before this basket can enter the realized PnL gate.
        out.net_pnl = std::nullopt;
    } else if (out.filled_legs == 0) {
        out.joint_state = "NO_FILL";
        out.partial_unwind = false;
        out.unwind_accounted = true;
        out.net_pnl = std::nullopt;
    } else {
        out.joint_state = all_exits_complete ? "PARTIAL_UNWOUND"
                                             : "PARTIAL_UNWIND_DEPTH_SHORTFALL";
        out.partial_unwind = true;
        out.unwind_accounted = true;
        out.net_pnl = net_exit - all_in_entry;
    }
    return out;
}

} // namespace pm::fast::paper
