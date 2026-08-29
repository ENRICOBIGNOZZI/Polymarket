#include "pm/fast_arb.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <utility>

namespace pm::fast {
namespace {

constexpr double kEps = 1e-12;

double effective_buy_price(double price, double slippage_bps) {
    return std::clamp(price * (1.0 + std::max(0.0, slippage_bps) / 10000.0), 1e-9, 1.0 - 1e-9);
}

std::vector<Level> valid_levels(std::vector<Level> levels, bool descending) {
    levels.erase(std::remove_if(levels.begin(), levels.end(), [](const Level& level) {
        return !std::isfinite(level.price) || !std::isfinite(level.size) ||
               level.price <= 0.0 || level.price >= 1.0 || level.size <= 0.0;
    }), levels.end());
    std::sort(levels.begin(), levels.end(), [descending](const Level& a, const Level& b) {
        return descending ? a.price > b.price : a.price < b.price;
    });
    return levels;
}

std::vector<Level> sorted_asks(const Book& book) { return valid_levels(book.asks, false); }
std::vector<Level> sorted_bids(const Book& book) { return valid_levels(book.bids, true); }

double total_depth(const std::vector<Level>& levels) {
    double total = 0.0;
    for (const auto& level : levels) {
        if (std::isfinite(level.price) && std::isfinite(level.size) &&
            level.price > 0.0 && level.price < 1.0 && level.size > 0.0) {
            total += level.size;
        }
    }
    return total;
}

double total_ask_depth(const Book& book) { return total_depth(book.asks); }
double total_bid_depth(const Book& book) { return total_depth(book.bids); }

double minimum_candidate_shares(const BuyLeg& leg) {
    if (leg.book == nullptr) return std::numeric_limits<double>::infinity();
    const double ask = leg.book->best_ask();
    if (!std::isfinite(ask) || ask <= 0.0) return std::numeric_limits<double>::infinity();
    // CLOB min_order_size is denominated in shares.
    return std::max(0.0, leg.book->min_order_size);
}

std::vector<double> candidate_sizes(const std::vector<BuyLeg>& legs, double max_shares) {
    std::set<double> candidates;
    candidates.insert(max_shares);
    for (const auto& leg : legs) {
        if (leg.book == nullptr) continue;
        double cumulative = 0.0;
        for (const auto& level : sorted_asks(*leg.book)) {
            cumulative += level.size;
            if (cumulative > kEps && cumulative <= max_shares + kEps) candidates.insert(cumulative);
            if (cumulative >= max_shares) break;
        }
    }
    return {candidates.begin(), candidates.end()};
}

FeeDetails fee_for_market(const std::unordered_map<std::string, FeeDetails>& fees,
                          const Market& market) {
    if (const auto it = fees.find(market.condition_id); it != fees.end()) return it->second;
    if (const auto it = fees.find(market.id); it != fees.end()) return it->second;
    if (market.fee_rate >= 0.0) {
        return FeeDetails{market.fee_rate, market.fee_exponent, market.fee_taker_only};
    }
    return FeeDetails{};
}

Opportunity rejected(OpportunityKind kind, std::string id, std::string event_id,
                     std::string reason, std::int64_t exchange_ts_ms,
                     std::int64_t received_ts_ms, std::int64_t decision_ts_ms) {
    Opportunity out;
    out.kind = kind;
    out.id = std::move(id);
    out.event_id = std::move(event_id);
    out.reject_reason = std::move(reason);
    out.exchange_ts_ms = exchange_ts_ms;
    out.received_ts_ms = received_ts_ms;
    out.decision_ts_ms = decision_ts_ms;
    return out;
}

} // namespace

const char* to_string(OpportunityKind kind) {
    switch (kind) {
        case OpportunityKind::BinaryCompleteSet: return "BINARY_COMPLETE_SET";
        case OpportunityKind::NegRiskCompleteSet: return "NEGRISK_COMPLETE_SET";
        case OpportunityKind::NegRiskConversion: return "NEGRISK_NO_CONVERSION";
        case OpportunityKind::LogicalImplication: return "LOGICAL_IMPLICATION";
        case OpportunityKind::LogicalMutualExclusion: return "LOGICAL_MUTUAL_EXCLUSION";
        case OpportunityKind::LogicalExhaustivePair: return "LOGICAL_EXHAUSTIVE_PAIR";
        case OpportunityKind::ExternalLatency: return "EXTERNAL_LATENCY_RV";
        case OpportunityKind::MakerCompleteSet: return "MAKER_COMPLETE_SET_SHADOW";
    }
    return "UNKNOWN";
}

double fee_per_share(double price, const FeeDetails& fee) {
    if (!std::isfinite(price) || price <= 0.0 || price >= 1.0 || fee.rate <= 0.0) return 0.0;
    return fee.rate * std::pow(price * (1.0 - price), std::max(0.0, fee.exponent));
}

BuyFill walk_buy_shares(const Book& book, double shares, double slippage_bps,
                        const FeeDetails& fee) {
    BuyFill out;
    if (!std::isfinite(shares) || shares <= 0.0) return out;
    double remaining = shares;
    for (const auto& level : sorted_asks(book)) {
        const double quantity = std::min(remaining, level.size);
        if (quantity <= 0.0) continue;
        const double execution_price = effective_buy_price(level.price, slippage_bps);
        const double per_share_fee = fee_per_share(execution_price, fee);
        out.raw_cost += quantity * level.price;
        out.all_in_cost += quantity * (execution_price + per_share_fee);
        out.shares += quantity;
        remaining -= quantity;
        if (remaining <= kEps) break;
    }
    out.complete = remaining <= kEps;
    if (out.shares > kEps) {
        const double slippage_cost = out.all_in_cost - out.raw_cost;
        out.average_fee = 0.0;
        // Preserve an all-in execution price net of fees for diagnostics.
        // The exact average fee is recovered below from the filled levels only
        // through all_in_cost - raw_cost; this includes explicit slippage.
        out.average_price = out.raw_cost / out.shares;
        if (slippage_cost > 0.0) out.average_fee = slippage_cost / out.shares;
    }
    return out;
}

SellFill walk_sell_shares(const Book& book, double shares, double slippage_bps,
                          const FeeDetails& fee) {
    SellFill out;
    if (!std::isfinite(shares) || shares <= 0.0) return out;
    double remaining = shares;
    for (const auto& level : sorted_bids(book)) {
        const double quantity = std::min(remaining, level.size);
        if (quantity <= 0.0) continue;
        const double execution_price = std::clamp(
            level.price * (1.0 - std::max(0.0, slippage_bps) / 10000.0),
            1e-9, 1.0 - 1e-9);
        const double per_share_fee = fee_per_share(execution_price, fee);
        out.raw_proceeds += quantity * level.price;
        out.net_proceeds += quantity * (execution_price - per_share_fee);
        out.shares += quantity;
        remaining -= quantity;
        if (remaining <= kEps) break;
    }
    out.complete = remaining <= kEps;
    if (out.shares > kEps) {
        out.average_price = out.raw_proceeds / out.shares;
        out.average_fee = (out.raw_proceeds - out.net_proceeds) / out.shares;
    }
    return out;
}

void apply_level(Book& book, bool bid_side, double price, double size) {
    if (!std::isfinite(price) || price <= 0.0 || price >= 1.0 || !std::isfinite(size)) return;
    auto& levels = bid_side ? book.bids : book.asks;
    const double tolerance = std::max(1e-10, std::max(1e-8, book.tick_size) * 1e-6);
    auto it = std::find_if(levels.begin(), levels.end(), [&](const Level& level) {
        return std::abs(level.price - price) <= tolerance;
    });
    if (size <= kEps) {
        if (it != levels.end()) levels.erase(it);
        return;
    }
    if (it == levels.end()) levels.push_back(Level{price, size});
    else it->size = size;
}

Opportunity evaluate_guaranteed_basket(
    OpportunityKind kind,
    std::string id,
    std::string event_id,
    const std::vector<BuyLeg>& legs,
    double payoff_floor,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    if (legs.empty() || payoff_floor <= 0.0) {
        return rejected(kind, std::move(id), std::move(event_id), "empty_or_invalid_basket",
                        exchange_ts_ms, received_ts_ms, decision_ts_ms);
    }

    double max_shares = std::numeric_limits<double>::infinity();
    double minimum_shares = std::max(0.0, policy.min_executable_shares);
    double best_touch_cost = 0.0;
    for (const auto& leg : legs) {
        if (leg.book == nullptr || !std::isfinite(leg.book->best_ask())) {
            return rejected(kind, std::move(id), std::move(event_id), "missing_ask",
                            exchange_ts_ms, received_ts_ms, decision_ts_ms);
        }
        max_shares = std::min(max_shares, total_ask_depth(*leg.book));
        minimum_shares = std::max(minimum_shares, minimum_candidate_shares(leg));
        const double ask = effective_buy_price(leg.book->best_ask(), policy.slippage_bps);
        best_touch_cost += ask + fee_per_share(ask, leg.fee);
    }
    if (!std::isfinite(max_shares) || max_shares <= kEps || best_touch_cost <= kEps) {
        return rejected(kind, std::move(id), std::move(event_id), "insufficient_depth",
                        exchange_ts_ms, received_ts_ms, decision_ts_ms);
    }
    max_shares = std::min(max_shares, std::max(0.0, policy.max_notional_usd) / best_touch_cost);
    if (max_shares + kEps < minimum_shares) {
        return rejected(kind, std::move(id), std::move(event_id), "below_minimum_order",
                        exchange_ts_ms, received_ts_ms, decision_ts_ms);
    }

    Opportunity best;
    best.kind = kind;
    best.id = std::move(id);
    best.event_id = std::move(event_id);
    best.hard_arbitrage = true;
    best.risk_class = "NON_ATOMIC_FOK_BASKET";
    best.payoff_floor = payoff_floor;
    best.exchange_ts_ms = exchange_ts_ms;
    best.received_ts_ms = received_ts_ms;
    best.decision_ts_ms = decision_ts_ms;

    const double latency_penalty = std::max(0.0, policy.latency_penalty_bps) / 10000.0;
    for (const double shares : candidate_sizes(legs, max_shares)) {
        if (shares + kEps < minimum_shares || shares <= kEps) continue;
        double raw_cost = 0.0;
        double all_in_cost = 0.0;
        std::vector<FilledLeg> filled;
        bool complete = true;
        for (const auto& leg : legs) {
            const auto fill = walk_buy_shares(*leg.book, shares, policy.slippage_bps, leg.fee);
            if (!fill.complete) {
                complete = false;
                break;
            }
            raw_cost += fill.raw_cost;
            all_in_cost += fill.all_in_cost;
            filled.push_back(FilledLeg{leg.market_id, leg.token_id, leg.outcome, shares,
                                       fill.average_price, fill.average_fee, fill.all_in_cost});
        }
        if (!complete) continue;
        if (all_in_cost > policy.max_notional_usd + kEps) continue;
        const double raw_edge = payoff_floor - raw_cost / shares;
        const double net_edge = payoff_floor - all_in_cost / shares - latency_penalty;
        const double profit = shares * net_edge;
        if (net_edge + kEps < policy.min_net_edge) continue;
        if (!best.executable || profit > best.expected_profit + kEps) {
            best.executable = true;
            best.raw_edge_per_share = raw_edge;
            best.net_edge_per_share = net_edge;
            best.executable_shares = shares;
            best.capital_required = all_in_cost;
            best.expected_profit = profit;
            best.legs = std::move(filled);
            best.reject_reason.clear();
        }
    }

    if (!best.executable) best.reject_reason = "edge_below_policy_after_costs";
    return best;
}

Opportunity evaluate_binary(const Market& market, const Book& yes, const Book& no,
                            const FeeDetails& fee, const Policy& policy,
                            std::int64_t exchange_ts_ms, std::int64_t received_ts_ms,
                            std::int64_t decision_ts_ms) {
    return evaluate_guaranteed_basket(
        OpportunityKind::BinaryCompleteSet,
        "binary:" + market.id,
        market.event_id,
        {{market.id, market.yes_token, "YES", &yes, fee},
         {market.id, market.no_token, "NO", &no, fee}},
        1.0, policy, exchange_ts_ms, received_ts_ms, decision_ts_ms);
}

Opportunity evaluate_negrisk_complete_set(
    const std::string& event_id,
    const std::vector<const Market*>& markets,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    std::vector<BuyLeg> legs;
    legs.reserve(markets.size());
    for (const auto* market : markets) {
        if (market == nullptr || !market->neg_risk) {
            return rejected(OpportunityKind::NegRiskCompleteSet, "negrisk:" + event_id,
                            event_id, "invalid_or_non_negrisk_member",
                            exchange_ts_ms, received_ts_ms, decision_ts_ms);
        }
        const auto it = books.find(market->yes_token);
        if (it == books.end()) {
            return rejected(OpportunityKind::NegRiskCompleteSet, "negrisk:" + event_id,
                            event_id, "missing_yes_book",
                            exchange_ts_ms, received_ts_ms, decision_ts_ms);
        }
        legs.push_back(BuyLeg{market->id, market->yes_token, "YES", &it->second,
                              fee_for_market(fees, *market)});
    }
    if (legs.size() < 2) {
        return rejected(OpportunityKind::NegRiskCompleteSet, "negrisk:" + event_id,
                        event_id, "incomplete_event_group",
                        exchange_ts_ms, received_ts_ms, decision_ts_ms);
    }
    return evaluate_guaranteed_basket(OpportunityKind::NegRiskCompleteSet,
                                      "negrisk:" + event_id, event_id, legs, 1.0, policy,
                                      exchange_ts_ms, received_ts_ms, decision_ts_ms);
}

Opportunity evaluate_implication(
    const ImplicationRelation& relation,
    const Market& antecedent,
    const Market& consequent,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    const auto no_a = books.find(antecedent.no_token);
    const auto yes_b = books.find(consequent.yes_token);
    if (no_a == books.end() || yes_b == books.end()) {
        return rejected(OpportunityKind::LogicalImplication, "implication:" + relation.id,
                        antecedent.event_id, "missing_relation_book",
                        exchange_ts_ms, received_ts_ms, decision_ts_ms);
    }
    return evaluate_guaranteed_basket(
        OpportunityKind::LogicalImplication,
        "implication:" + relation.id,
        antecedent.event_id,
        {{antecedent.id, antecedent.no_token, "NO_ANTECEDENT", &no_a->second,
          fee_for_market(fees, antecedent)},
         {consequent.id, consequent.yes_token, "YES_CONSEQUENT", &yes_b->second,
          fee_for_market(fees, consequent)}},
        1.0, policy, exchange_ts_ms, received_ts_ms, decision_ts_ms);
}

Opportunity evaluate_mutual_exclusion(
    const ImplicationRelation& relation,
    const Market& left,
    const Market& right,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    const auto left_no = books.find(left.no_token);
    const auto right_no = books.find(right.no_token);
    if (left_no == books.end() || right_no == books.end()) {
        return rejected(OpportunityKind::LogicalMutualExclusion,
                        "mutual-exclusion:" + relation.id, left.event_id,
                        "missing_relation_book", exchange_ts_ms,
                        received_ts_ms, decision_ts_ms);
    }
    return evaluate_guaranteed_basket(
        OpportunityKind::LogicalMutualExclusion,
        "mutual-exclusion:" + relation.id,
        left.event_id,
        {{left.id, left.no_token, "NO_LEFT", &left_no->second,
          fee_for_market(fees, left)},
         {right.id, right.no_token, "NO_RIGHT", &right_no->second,
          fee_for_market(fees, right)}},
        1.0, policy, exchange_ts_ms, received_ts_ms, decision_ts_ms);
}

Opportunity evaluate_exhaustive_pair(
    const ImplicationRelation& relation,
    const Market& left,
    const Market& right,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    const auto left_yes = books.find(left.yes_token);
    const auto right_yes = books.find(right.yes_token);
    if (left_yes == books.end() || right_yes == books.end()) {
        return rejected(OpportunityKind::LogicalExhaustivePair,
                        "exhaustive-pair:" + relation.id, left.event_id,
                        "missing_relation_book", exchange_ts_ms,
                        received_ts_ms, decision_ts_ms);
    }
    return evaluate_guaranteed_basket(
        OpportunityKind::LogicalExhaustivePair,
        "exhaustive-pair:" + relation.id,
        left.event_id,
        {{left.id, left.yes_token, "YES_LEFT", &left_yes->second,
          fee_for_market(fees, left)},
         {right.id, right.yes_token, "YES_RIGHT", &right_yes->second,
          fee_for_market(fees, right)}},
        1.0, policy, exchange_ts_ms, received_ts_ms, decision_ts_ms);
}

Opportunity evaluate_negrisk_conversion(
    const std::string& event_id,
    const Market& source,
    const std::vector<const Market*>& markets,
    const std::unordered_map<std::string, Book>& books,
    const std::unordered_map<std::string, FeeDetails>& fees,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    Opportunity out;
    out.kind = OpportunityKind::NegRiskConversion;
    out.id = "negrisk-conversion:" + event_id + ':' + source.id;
    out.event_id = event_id;
    out.hard_arbitrage = true;
    out.risk_class = "ONCHAIN_CONVERSION_NON_ATOMIC";
    out.exchange_ts_ms = exchange_ts_ms;
    out.received_ts_ms = received_ts_ms;
    out.decision_ts_ms = decision_ts_ms;

    const auto no_it = books.find(source.no_token);
    if (no_it == books.end() || markets.size() < 2) {
        out.reject_reason = "missing_source_no_or_incomplete_event";
        return out;
    }
    double max_shares = total_ask_depth(no_it->second);
    double min_shares = std::max(policy.min_executable_shares, no_it->second.min_order_size);
    std::vector<std::pair<const Market*, const Book*>> targets;
    for (const auto* market : markets) {
        if (market == nullptr || market->id == source.id) continue;
        const auto yes_it = books.find(market->yes_token);
        if (yes_it == books.end()) {
            out.reject_reason = "missing_target_yes_book";
            return out;
        }
        max_shares = std::min(max_shares, total_bid_depth(yes_it->second));
        min_shares = std::max(min_shares, yes_it->second.min_order_size);
        targets.emplace_back(market, &yes_it->second);
    }
    if (targets.empty() || !std::isfinite(max_shares) || max_shares + kEps < min_shares) {
        out.reject_reason = "insufficient_conversion_depth";
        return out;
    }

    const auto source_fee = fee_for_market(fees, source);
    const double top_no = effective_buy_price(no_it->second.best_ask(), policy.slippage_bps);
    const double source_top_cost = top_no + fee_per_share(top_no, source_fee);
    max_shares = std::min(max_shares,
                          policy.max_notional_usd / std::max(source_top_cost, kEps));
    if (max_shares + kEps < min_shares) {
        out.reject_reason = "below_minimum_order";
        return out;
    }

    std::set<double> sizes{max_shares};
    double cumulative = 0.0;
    for (const auto& level : sorted_asks(no_it->second)) {
        cumulative += level.size;
        if (cumulative >= min_shares && cumulative <= max_shares + kEps) sizes.insert(cumulative);
    }
    for (const auto& [market, book] : targets) {
        (void)market;
        cumulative = 0.0;
        for (const auto& level : sorted_bids(*book)) {
            cumulative += level.size;
            if (cumulative >= min_shares && cumulative <= max_shares + kEps) sizes.insert(cumulative);
        }
    }

    const double latency_penalty = std::max(0.0, policy.latency_penalty_bps) / 10000.0;
    for (const double shares : sizes) {
        if (shares + kEps < min_shares) continue;
        const auto buy = walk_buy_shares(no_it->second, shares, policy.slippage_bps,
                                         source_fee);
        if (!buy.complete || buy.all_in_cost > policy.max_notional_usd + kEps) continue;
        double raw_revenue = 0.0;
        double net_revenue = 0.0;
        std::vector<FilledLeg> legs;
        legs.push_back(FilledLeg{source.id, source.no_token, "BUY_NO_SOURCE", shares,
                                 buy.average_price, buy.average_fee, buy.all_in_cost});
        bool complete = true;
        for (const auto& [market, book] : targets) {
            const auto sell = walk_sell_shares(*book, shares, policy.slippage_bps,
                                               fee_for_market(fees, *market));
            if (!sell.complete) {
                complete = false;
                break;
            }
            raw_revenue += sell.raw_proceeds;
            net_revenue += sell.net_proceeds;
            legs.push_back(FilledLeg{market->id, market->yes_token,
                                     "SELL_YES_AFTER_CONVERSION", shares,
                                     sell.average_price, sell.average_fee,
                                     -sell.net_proceeds});
        }
        if (!complete) continue;
        const double fixed_cost = std::max(0.0, policy.conversion_fixed_cost_usd);
        if (buy.all_in_cost + fixed_cost > policy.max_notional_usd + kEps) continue;
        const double raw_edge = (raw_revenue - buy.raw_cost) / shares;
        const double net_profit = net_revenue - buy.all_in_cost - fixed_cost -
                                  latency_penalty * shares;
        const double net_edge = net_profit / shares;
        if (net_edge + kEps < policy.min_net_edge) continue;
        if (!out.executable || net_profit > out.expected_profit + kEps) {
            out.executable = true;
            out.payoff_floor = 0.0;
            out.raw_edge_per_share = raw_edge;
            out.net_edge_per_share = net_edge;
            out.executable_shares = shares;
            out.capital_required = buy.all_in_cost + fixed_cost;
            out.expected_profit = net_profit;
            out.legs = std::move(legs);
            out.reject_reason.clear();
        }
    }
    if (!out.executable) out.reject_reason = "edge_below_costed_conversion_policy";
    return out;
}

Opportunity evaluate_external_latency(
    const Market& market,
    const Book& yes,
    const Book& no,
    const FeeDetails& fee,
    const ExternalSignal& signal,
    std::int64_t now_ms,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    Opportunity out;
    out.kind = OpportunityKind::ExternalLatency;
    out.id = "external:" + market.id + ':' + signal.source;
    out.event_id = market.event_id;
    out.hard_arbitrage = false;
    out.risk_class = "MODEL_RISK_EXTERNAL_SIGNAL";
    out.exchange_ts_ms = exchange_ts_ms;
    out.received_ts_ms = received_ts_ms;
    out.decision_ts_ms = decision_ts_ms;

    const std::int64_t signal_ms = signal.timestamp > 100000000000LL
                                       ? signal.timestamp
                                       : signal.timestamp * 1000;
    if (signal_ms <= 0 || now_ms < signal_ms || now_ms - signal_ms > policy.max_external_age_ms) {
        out.reject_reason = "stale_external_signal";
        return out;
    }
    if (!std::isfinite(signal.q_yes) || signal.q_yes <= 0.0 || signal.q_yes >= 1.0 ||
        !std::isfinite(signal.confidence) || signal.confidence <= 0.0) {
        out.reject_reason = "invalid_external_signal";
        return out;
    }

    struct Candidate {
        bool yes_side = true;
        BuyFill fill;
        double fair = 0.0;
        double raw_edge = -1.0;
        double net_edge = -1.0;
    };

    const double uncertainty = std::max(0.0, policy.external_uncertainty_penalty) *
                               (1.0 - std::clamp(signal.confidence, 0.0, 1.0));
    const double latency = std::max(0.0, policy.latency_penalty_bps) / 10000.0;

    auto candidate = [&](bool yes_side, const Book& book, double fair) {
        Candidate c;
        c.yes_side = yes_side;
        c.fair = fair;
        const double ask = book.best_ask();
        if (!std::isfinite(ask)) return c;
        const double top_execution = effective_buy_price(ask, policy.slippage_bps);
        const double top_cost = top_execution + fee_per_share(top_execution, fee);
        const double shares = std::min(total_ask_depth(book),
                                       policy.max_notional_usd / std::max(top_cost, kEps));
        if (shares + kEps < std::max(policy.min_executable_shares, book.min_order_size)) return c;
        c.fill = walk_buy_shares(book, shares, policy.slippage_bps, fee);
        if (!c.fill.complete || c.fill.all_in_cost > policy.max_notional_usd + kEps) return c;
        c.raw_edge = fair - c.fill.raw_cost / c.fill.shares;
        c.net_edge = fair - c.fill.all_in_cost / c.fill.shares - uncertainty - latency;
        return c;
    };

    Candidate best = candidate(true, yes, signal.q_yes);
    Candidate no_candidate = candidate(false, no, 1.0 - signal.q_yes);
    if (no_candidate.net_edge > best.net_edge) best = std::move(no_candidate);
    if (!best.fill.complete) {
        out.reject_reason = "insufficient_depth_or_minimum_order";
        return out;
    }

    out.payoff_floor = 0.0;
    out.raw_edge_per_share = best.raw_edge;
    out.net_edge_per_share = best.net_edge;
    out.executable_shares = best.fill.shares;
    out.capital_required = best.fill.all_in_cost;
    out.expected_profit = best.fill.shares * best.net_edge;
    out.legs.push_back(FilledLeg{market.id, best.yes_side ? market.yes_token : market.no_token,
                                 best.yes_side ? "YES" : "NO", best.fill.shares,
                                 best.fill.average_price, best.fill.average_fee,
                                 best.fill.all_in_cost});
    out.executable = best.net_edge >= policy.min_net_edge;
    if (!out.executable) out.reject_reason = "edge_below_policy_after_costs";
    return out;
}

Opportunity evaluate_maker_complete_set(
    const Market& market,
    const Book& yes,
    const Book& no,
    const Policy& policy,
    std::int64_t exchange_ts_ms,
    std::int64_t received_ts_ms,
    std::int64_t decision_ts_ms) {
    Opportunity out;
    out.kind = OpportunityKind::MakerCompleteSet;
    out.id = "maker-binary:" + market.id;
    out.event_id = market.event_id;
    out.hard_arbitrage = false;
    out.risk_class = "NON_ATOMIC_PASSIVE_TWO_SIDED";
    out.payoff_floor = 1.0;
    out.exchange_ts_ms = exchange_ts_ms;
    out.received_ts_ms = received_ts_ms;
    out.decision_ts_ms = decision_ts_ms;

    const double yb = yes.best_bid(), ya = yes.best_ask();
    const double nb = no.best_bid(), na = no.best_ask();
    if (!std::isfinite(yb) || !std::isfinite(ya) || !std::isfinite(nb) || !std::isfinite(na)) {
        out.reject_reason = "missing_two_sided_book";
        return out;
    }
    const double ytick = std::max(1e-8, yes.tick_size);
    const double ntick = std::max(1e-8, no.tick_size);
    const double yquote = std::min(yb + policy.maker_improve_ticks * ytick, ya - ytick);
    const double nquote = std::min(nb + policy.maker_improve_ticks * ntick, na - ntick);
    if (yquote <= 0.0 || nquote <= 0.0 || yquote >= ya - kEps || nquote >= na - kEps) {
        out.reject_reason = "no_post_only_price";
        return out;
    }
    const double cost = yquote + nquote;
    const double raw_edge = 1.0 - cost;
    const double penalty = std::max(0.0, policy.maker_one_sided_penalty_bps) / 10000.0;
    const double net_edge = raw_edge - penalty;
    const double shares = policy.max_notional_usd / std::max(cost, kEps);
    out.raw_edge_per_share = raw_edge;
    out.net_edge_per_share = net_edge;
    out.executable_shares = shares;
    out.capital_required = shares * cost;
    out.expected_profit = shares * net_edge;
    out.legs = {
        FilledLeg{market.id, market.yes_token, "YES_POST_ONLY", shares, yquote, 0.0, shares * yquote},
        FilledLeg{market.id, market.no_token, "NO_POST_ONLY", shares, nquote, 0.0, shares * nquote}
    };
    out.executable = shares >= policy.min_executable_shares && net_edge >= policy.min_net_edge;
    if (!out.executable) out.reject_reason = "edge_below_one_sided_risk_penalty";
    return out;
}

} // namespace pm::fast
