#!/usr/bin/env python3
"""Render a relation-aware semantic V5 engine challenger from the incumbent source.

This is deliberately an explicit, exact-source research transform: it lets the
research branch exercise the challenger in the normal C++ engine without hiding
or mutating the incumbent source file. Integration should port the accepted
logic directly into src/engine.cpp after evidence review.
"""
from __future__ import annotations

import argparse
from pathlib import Path

OLD_WORDS = r'''std::vector<std::string> words(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (unsigned char c : s) {
        if (std::isalnum(c)) {
            cur.push_back(static_cast<char>(std::tolower(c)));
        } else {
            if (cur.size() >= 3) out.push_back(cur);
            cur.clear();
        }
    }
    if (cur.size() >= 3) out.push_back(cur);
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}
'''

NEW_WORDS = r'''std::vector<std::string> words(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    auto flush = [&]() {
        if (cur.empty()) return;
        const bool numeric = std::all_of(cur.begin(), cur.end(), [](unsigned char x) { return std::isdigit(x); });
        // Keep two-letter entities/negations (US, UK, no) and every numeric token.
        // The incumbent >=3 rule erased exactly the polarity/threshold information
        // needed to decide whether two propositions are probability peers.
        if (cur.size() >= 2 || numeric) out.push_back(cur);
        cur.clear();
    };
    for (unsigned char c : s) {
        if (std::isalnum(c)) cur.push_back(static_cast<char>(std::tolower(c)));
        else flush();
    }
    flush();
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}
'''

OLD_SEMANTIC = r'''    if (std::isfinite(mid)) {
        double sw = 0.0, spv = 0.0;
        for (const auto& peer : universe) {
            if (peer.id == m.id) continue;
            const double sim = jaccard(m.question, peer.question);
            if (sim < cfg_.semantic_min_similarity) continue;
            auto it = yes_books.find(peer.yes_token);
            if (it == yes_books.end()) continue;
            const double p = it->second.midpoint();
            if (!std::isfinite(p)) continue;
            const double w = sim * sim * std::sqrt(std::max(1.0, peer.liquidity));
            sw += w;
            spv += w * p;
        }
        if (sw > 0.0) {
            const double peer = spv / sw;
            const double q = (1.0 - cfg_.semantic_shrink) * mid + cfg_.semantic_shrink * peer;
            out.push_back({"semantic", std::clamp(q, 0.001, 0.999), std::min(0.25, 0.05 + cfg_.semantic_shrink)});
        }
    }
'''

NEW_SEMANTIC = r'''    if (std::isfinite(mid)) {
        // Probability pooling is allowed only for proposition-compatible peers.
        // Lexical overlap by itself is not enough: negation, numerical thresholds,
        // direction and event/entity content must agree before a peer can move fair.
        const auto token_set = [](const std::string& text) {
            const auto v = words(text);
            return std::set<std::string>(v.begin(), v.end());
        };
        const auto any_digit = [](const std::string& token) {
            return std::any_of(token.begin(), token.end(), [](unsigned char c) { return std::isdigit(c); });
        };
        const auto negative = [](const std::set<std::string>& x) {
            return x.count("no") || x.count("not") || x.count("never") || x.count("without");
        };
        const auto subset = [&](const std::set<std::string>& x, const std::set<std::string>& wanted, bool numeric_only) {
            std::set<std::string> out_tokens;
            for (const auto& token : x) {
                if ((numeric_only && any_digit(token)) || (!numeric_only && wanted.count(token))) out_tokens.insert(token);
            }
            return out_tokens;
        };
        const auto similarity = [](const std::set<std::string>& a, const std::set<std::string>& b) {
            if (a.empty() && b.empty()) return 1.0;
            std::size_t inter = 0;
            for (const auto& token : a) inter += b.count(token);
            const std::size_t uni = a.size() + b.size() - inter;
            return uni ? static_cast<double>(inter) / static_cast<double>(uni) : 0.0;
        };
        static const std::set<std::string> directions{
            "above", "below", "over", "under", "more", "less", "least", "most", "before", "after", "between", "by"
        };
        static const std::set<std::string> stopwords{
            "will", "would", "could", "should", "does", "did", "the", "this", "that", "what", "when", "which",
            "who", "whom", "whose", "have", "has", "had", "happen", "happens", "happened", "market", "markets",
            "price", "prices", "reach", "reaches", "reached", "become", "becomes", "became", "before", "after",
            "during", "between", "into", "from", "with", "without", "over", "under", "above", "below", "more",
            "less", "than", "then", "year", "years", "month", "months", "day", "days", "week", "weeks", "yes",
            "not", "never", "no", "and", "for", "are", "was", "were", "you", "your", "its", "our", "their"
        };
        const auto salient = [&](const std::set<std::string>& x) {
            std::set<std::string> out_tokens;
            for (const auto& token : x) {
                if (stopwords.count(token) || directions.count(token) || any_digit(token)) continue;
                out_tokens.insert(token);
            }
            return out_tokens;
        };

        const auto target_tokens = token_set(m.question);
        const auto target_numeric = subset(target_tokens, {}, true);
        const auto target_direction = subset(target_tokens, directions, false);
        const auto target_salient = salient(target_tokens);
        double sw = 0.0, spv = 0.0, best_relation = 0.0;
        for (const auto& peer : universe) {
            if (peer.id == m.id) continue;
            const double sim = jaccard(m.question, peer.question);
            if (sim < cfg_.semantic_min_similarity) continue;

            const auto peer_tokens = token_set(peer.question);
            if (negative(target_tokens) != negative(peer_tokens)) continue;
            if (target_numeric != subset(peer_tokens, {}, true)) continue;
            if (target_direction != subset(peer_tokens, directions, false)) continue;

            const auto peer_salient = salient(peer_tokens);
            std::size_t shared = 0;
            for (const auto& token : target_salient) shared += peer_salient.count(token);
            if (shared < 2) continue;
            const double entity_sim = similarity(target_salient, peer_salient);
            const bool same_event = !m.event_id.empty() && m.event_id == peer.event_id;
            const double required_entity_sim = same_event ? 0.65 : 0.80;
            if (entity_sim < required_entity_sim) continue;

            auto it = yes_books.find(peer.yes_token);
            if (it == yes_books.end()) continue;
            const double p = it->second.midpoint();
            if (!std::isfinite(p)) continue;
            const double relation = std::min(sim, entity_sim);
            const double w = relation * relation * std::sqrt(std::max(1.0, peer.liquidity));
            sw += w;
            spv += w * p;
            best_relation = std::max(best_relation, relation);
        }
        if (sw > 0.0) {
            const double peer = spv / sw;
            const double q = (1.0 - cfg_.semantic_shrink) * mid + cfg_.semantic_shrink * peer;
            const double conf = std::clamp(0.05 + 0.20 * best_relation, 0.05, 0.25);
            out.push_back({"semantic", std::clamp(q, 0.001, 0.999), conf});
        }
    }
'''


def render(source: str) -> str:
    if source.count(OLD_WORDS) != 1:
        raise RuntimeError("incumbent words() contract changed; refusing fuzzy semantic patch")
    if source.count(OLD_SEMANTIC) != 1:
        raise RuntimeError("incumbent semantic block changed; refusing fuzzy semantic patch")
    return source.replace(OLD_WORDS, NEW_WORDS).replace(OLD_SEMANTIC, NEW_SEMANTIC)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render(args.input.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
