#pragma once

#include "pm/types.hpp"

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace pm::fast {

enum class OpportunityKind {
    BinaryCompleteSet,
    NegRiskCompleteSet,
    NegRiskConversion,
    LogicalImplication,
    LogicalMutualExclusion,
    LogicalExhaustivePair,
    ExternalLatency,
    MakerCompleteSet
};

struct Policy {
    double min_net_edge = 0.0005;
    double slippage_bps = 2.0;
    double latency_penalty_bps = 1.0;
    double max_notional_usd = 50.0;
    double min_executable_shares = 1.0;
    double external_uncertainty_penalty = 0.01;
    std::int64_t max_external_age_ms = 15000;
    double maker_one_sided_penalty_bps = 50.0;
    int maker_improve_ticks = 1;
    double conversion_fixed_cost_usd = 1.0;
};

struct BuyLeg {
    std::string market_id;
    std::string token_id;
    std::string outcome;
    const Book* book = nullptr;
    FeeDetails fee;
};

struct FilledLeg {
    std::string market_id;
    std::string token_id;
    std::string outcome;
    double shares = 0.0;
    double average_price = 0.0;
    double fee_per_share = 0.0;
    double all_in_cost = 0.0;
};

struct Opportunity {
    OpportunityKind kind = OpportunityKind::BinaryCompleteSet;
    std::string id;
    std::string event_id;
    bool hard_arbitrage = false;
    bool executable = false;
    std::string risk_class;
    std::string reject_reason;
    double payoff_floor = 0.0;
    double raw_edge_per_share = -1.0;
    double net_edge_per_share = -1.0;
    double executable_shares = 0.0;
    double capital_required = 0.0;
    double expected_profit = 0.0;
    std::int64_t exchange_ts_ms = 0;
    std::int64_t received_ts_ms = 0;
    std::int64_t decision_ts_ms = 0;
    std::vector<FilledLeg> legs;
};

struct ImplicationRelation {
    std::string id;
    std::string antecedent_market_id;
    std::string consequent_market_id;
};

struct BuyFill {
    bool complete = false;
    double shares = 0.0;
    double raw_cost = 0.0;
    double all_in_cost = 0.0;
    double average_price = 0.0;
    double average_fee = 0.0;
};

struct SellFill {
    bool complete = false;
    double shares = 0.0;
    double raw_proceeds = 0.0;
    double net_proceeds = 0.0;
    double average_price = 0.0;
    double average_fee = 0.0;
};

[[nodiscard]] const char* to_string(OpportunityKind kind);
[[nodiscard]] double fee_per_share(double price, const FeeDetails& fee);
[[nodiscard]] BuyFill walk_buy_shares(const Book& book, double shares, double slippage_bps,
                                      const FeeDetails& fee);
[[nodiscard]] SellFill walk_sell_shares(const Book& book, double shares, double slippage_bps,
                                        const FeeDetails& fee);
void apply_level(Book& book, bool bid_side, double price, double size);

[[nodiscard]] Opportunity evaluate_guaranteed_basket(
    OpportunityKind kind,
    std::string id,
    std::string event_id,
    const std::vector<BuyLeg>& legs,
    double payoff_floor,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_binary(
    const Market& market,
    const Book& yes,
    const Book& no,
    const FeeDetails& fee,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_negrisk_complete_set(
    const std::string& event_id,
    const std::vector<const Market*>& markets,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_implication(
    const ImplicationRelation& relation,
    const Market& antecedent,
    const Market& consequent,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_mutual_exclusion(
    const ImplicationRelation& relation,
    const Market& left,
    const Market& right,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_exhaustive_pair(
    const ImplicationRelation& relation,
    const Market& left,
    const Market& right,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_negrisk_conversion(
    const std::string& event_id,
    const Market& source,
    const std::vector<const Market*>& markets,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_external_latency(
    const Market& market,
    const Book& yes,
    const Book& no,
    const FeeDetails& fee,
    const ExternalSignal& signal,
    std::int64_t now_ms,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

[[nodiscard]] Opportunity evaluate_maker_complete_set(
    const Market& market,
    const Book& yes,
    const Book& no,
    const Policy& policy,
    std::int64_t exchange_ts_ms = 0,
    std::int64_t received_ts_ms = 0,
    std::int64_t decision_ts_ms = 0);

} // namespace pm::fast
