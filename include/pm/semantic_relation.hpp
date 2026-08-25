#pragma once

#include <algorithm>
#include <cctype>
#include <set>
#include <string>

namespace pm {

inline const std::set<std::string>& generic_semantic_relation_tokens() {
    static const std::set<std::string> tokens = {
        "after", "above", "april", "august", "before", "below", "between",
        "december", "dip", "drop", "election", "exact", "fall", "february",
        "happen", "hit", "january", "july", "june", "march", "market", "may",
        "more", "most", "next", "nomination", "november", "october", "over",
        "presidential", "price", "primary", "rate", "reach", "rise", "score",
        "september", "than", "under", "will", "win", "winner",
    };
    return tokens;
}

inline std::set<std::string> semantic_relation_anchors(const std::string& text) {
    std::set<std::string> out;
    std::string cur;
    auto flush = [&]() {
        if (cur.size() >= 3 && !generic_semantic_relation_tokens().contains(cur)) {
            const bool has_digit = std::any_of(cur.begin(), cur.end(), [](unsigned char c) {
                return std::isdigit(c) != 0;
            });
            if (!has_digit) out.insert(cur);
        }
        cur.clear();
    };
    for (unsigned char c : text) {
        if (std::isalnum(c)) cur.push_back(static_cast<char>(std::tolower(c)));
        else flush();
    }
    flush();
    return out;
}

inline bool has_shared_specific_semantic_anchor(const std::string& a, const std::string& b) {
    const auto x = semantic_relation_anchors(a);
    const auto y = semantic_relation_anchors(b);
    for (const auto& token : x) {
        if (y.contains(token)) return true;
    }
    return false;
}

} // namespace pm
