#pragma once

#include "pm/types.hpp"

#include <algorithm>
#include <cctype>
#include <set>
#include <string>
#include <string_view>

namespace pm {

enum class MarketRelation {
    none,
    same_event,
    semantic
};

inline std::set<std::string> relation_tokens(const Market& market) {
    static const std::set<std::string> stopwords{
        "a", "an", "and", "are", "at", "be", "before", "by", "for", "from",
        "happen", "in", "is", "it", "market", "no", "of", "on", "or", "the",
        "this", "to", "was", "were", "who", "will", "win", "yes"
    };
    const std::string text = market.slug + " " + market.question;
    std::set<std::string> out;
    std::string token;
    auto flush = [&]() {
        const bool numeric = !token.empty() && std::all_of(
            token.begin(), token.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
        if (token.size() >= 3 && !numeric && !stopwords.contains(token)) out.insert(token);
        token.clear();
    };
    for (unsigned char c : text) {
        if (std::isalnum(c)) token.push_back(static_cast<char>(std::tolower(c)));
        else flush();
    }
    flush();
    return out;
}

inline double market_text_jaccard(
    const Market& a,
    const Market& b,
    std::size_t* shared_out = nullptr) {
    const auto ta = relation_tokens(a);
    const auto tb = relation_tokens(b);
    std::size_t shared = 0;
    for (const auto& token : ta) if (tb.contains(token)) ++shared;
    if (shared_out) *shared_out = shared;
    const std::size_t union_size = ta.size() + tb.size() - shared;
    return union_size > 0
        ? static_cast<double>(shared) / static_cast<double>(union_size)
        : 0.0;
}

inline MarketRelation market_relation(
    const Market& a,
    const Market& b,
    double min_jaccard = 0.08,
    std::size_t min_shared_tokens = 1) {
    if (!a.event_id.empty() && a.event_id == b.event_id) return MarketRelation::same_event;
    std::size_t shared = 0;
    const double jaccard = market_text_jaccard(a, b, &shared);
    if (shared >= min_shared_tokens && jaccard >= min_jaccard) return MarketRelation::semantic;
    return MarketRelation::none;
}

inline std::string_view market_relation_name(MarketRelation relation) {
    switch (relation) {
        case MarketRelation::same_event: return "same_event";
        case MarketRelation::semantic: return "semantic";
        case MarketRelation::none: return "none";
    }
    return "none";
}

} // namespace pm
