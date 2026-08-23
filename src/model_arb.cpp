#include "poly/model.hpp"
#include "poly/math.hpp"

#include <algorithm>
#include <cmath>
#include <unordered_map>

namespace poly {

std::vector<Signal> LogicalArbModel::generate(
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books) const {
    std::vector<Signal> out;

    // Binary complement arbitrage: buying YES + NO for less than $1 locks $1 at resolution.
    std::unordered_map<std::string, std::vector<std::string>> by_market;
    for (const auto& [token, m] : meta) by_market[m.market_id].push_back(token);

    for (const auto& [market_id, tokens] : by_market) {
        if (tokens.size() != 2) continue;
        const auto b0 = books.find(tokens[0]);
        const auto b1 = books.find(tokens[1]);
        if (b0 == books.end() || b1 == books.end()) continue;
        const double a0 = b0->second.best_ask();
        const double a1 = b1->second.best_ask();
        if (!std::isfinite(a0) || !std::isfinite(a1)) continue;
        const double locked = 1.0 - (a0 + a1);
        if (locked > 0.001) {
            for (int j = 0; j < 2; ++j) {
                const auto& token = tokens[static_cast<std::size_t>(j)];
                const auto& m = meta.at(token);
                const auto& b = books.at(token);
                Signal s;
                s.kind = SignalKind::ExactBasketArb;
                s.token_id = token;
                s.market_id = market_id;
                s.event_id = m.event_id;
                s.question = m.question + " [" + m.outcome + "]";
                s.side = Side::Buy;
                s.observed_price = b.best_ask();
                s.fair_price = s.observed_price + 0.5 * locked;
                s.gross_edge = 0.5 * locked;
                s.est_cost = taker_fee_usdc(1.0, s.observed_price, m.category);
                s.net_edge = s.gross_edge - s.est_cost;
                s.score = s.net_edge / std::max(1e-4, b.spread());
                s.confidence = 0.999;
                s.rationale = "binary complement basket asks sum below 1; execute legs atomically in live version";
                if (s.net_edge > 0.0) out.push_back(std::move(s));
            }
        }
    }

    // Negative-risk multi-market event consistency: YES asks across mutually exclusive outcomes.
    std::unordered_map<std::string, std::vector<std::string>> by_event_yes;
    for (const auto& [token, m] : meta) {
        if (!m.neg_risk || m.event_id.empty()) continue;
        std::string outcome = m.outcome;
        std::transform(outcome.begin(), outcome.end(), outcome.begin(), ::tolower);
        if (outcome == "yes") by_event_yes[m.event_id].push_back(token);
    }
    for (const auto& [event_id, tokens] : by_event_yes) {
        if (tokens.size() < 3) continue;
        double sum_asks = 0.0;
        bool ok = true;
        for (const auto& t : tokens) {
            const auto it = books.find(t);
            if (it == books.end() || !std::isfinite(it->second.best_ask())) { ok = false; break; }
            sum_asks += it->second.best_ask();
        }
        if (!ok || sum_asks >= 0.998) continue;
        const double basket_edge = 1.0 - sum_asks;
        for (const auto& t : tokens) {
            const auto& m = meta.at(t);
            const auto& b = books.at(t);
            Signal s;
            // Negative-risk pricing is shown as a logical consistency candidate only.
            // Until the event outcome set is explicitly verified as complete, treating the
            // discovered YES subset as a guaranteed $1 payout basket would create false arb P&L.
            s.kind = SignalKind::LogicalArb;
            s.token_id = t;
            s.market_id = m.market_id;
            s.event_id = event_id;
            s.question = m.event_title.empty() ? m.question : m.event_title;
            s.side = Side::Buy;
            s.observed_price = b.best_ask();
            s.fair_price = std::min(0.999, s.observed_price + basket_edge / static_cast<double>(tokens.size()));
            s.gross_edge = basket_edge / static_cast<double>(tokens.size());
            s.est_cost = taker_fee_usdc(1.0, s.observed_price, m.category);
            s.net_edge = s.gross_edge - s.est_cost;
            s.score = s.net_edge / std::max(1e-4, b.spread());
            s.confidence = 0.50;
            s.rationale = "negative-risk consistency candidate: discovered YES asks sum below 1; outcome-set completeness not yet verified";
            if (s.net_edge > 0.0) out.push_back(std::move(s));
        }
    }

    std::sort(out.begin(), out.end(), [](const Signal& a, const Signal& b){ return a.score > b.score; });
    return out;
}

} // namespace poly
