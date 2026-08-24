#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "include/pm/api.hpp",
    "namespace pm {\nclass PolymarketApi {",
    "namespace pm {\n\n"
    "std::string history_interval_for_range(std::int64_t start_ts, std::int64_t end_ts);\n\n"
    "class PolymarketApi {",
)

replace_once(
    "src/api.cpp",
    "} // namespace\n\nPolymarketApi::PolymarketApi(Config cfg) : cfg_(std::move(cfg)) {}",
    "} // namespace\n\n"
    "std::string history_interval_for_range(std::int64_t start_ts, std::int64_t end_ts) {\n"
    "    const std::int64_t seconds = std::max<std::int64_t>(0, end_ts - start_ts);\n"
    "    if (seconds <= 60 * 60) return \"1h\";\n"
    "    if (seconds <= 6 * 60 * 60) return \"6h\";\n"
    "    if (seconds <= 24 * 60 * 60) return \"1d\";\n"
    "    if (seconds <= 7 * 24 * 60 * 60) return \"1w\";\n"
    "    if (seconds <= 31LL * 24 * 60 * 60) return \"1m\";\n"
    "    return \"max\";\n"
    "}\n\n"
    "PolymarketApi::PolymarketApi(Config cfg) : cfg_(std::move(cfg)) {}",
)

old_function = '''std::unordered_map<std::string,std::vector<PricePoint>> PolymarketApi::fetch_price_history(
    const std::vector<std::string>& token_ids,
    std::int64_t start_ts,
    std::int64_t end_ts,
    std::size_t fidelity_minutes) const {
    std::unordered_map<std::string,std::vector<PricePoint>> out;
    constexpr std::size_t batch_size = 20;
    for (std::size_t pos = 0; pos < token_ids.size(); pos += batch_size) {
        const auto end = std::min(token_ids.size(), pos + batch_size);
        json::array ids;
        for (std::size_t i = pos; i < end; ++i) ids.emplace_back(token_ids[i]);
        json::object req{
            {"markets", std::move(ids)},
            {"start_ts", start_ts},
            {"end_ts", end_ts},
            {"fidelity", static_cast<std::int64_t>(std::max<std::size_t>(1, fidelity_minutes))}
        };
        const auto r = http_.post_json(cfg_.clob_url + "/batch-prices-history", json::serialize(req));
        if (r.status >= 200 && r.status < 300) {
            const auto root = json::parse(r.body);
            if (root.is_object()) {
                auto hi = root.as_object().find("history");
                if (hi != root.as_object().end() && hi->value().is_object()) {
                    for (const auto& kv : hi->value().as_object()) {
                        if (kv.value().is_array()) {
                            parse_history_array(kv.value().as_array(), out[std::string(kv.key())]);
                        }
                    }
                    continue;
                }
            }
        }
        for (std::size_t i = pos; i < end; ++i) {
            std::ostringstream u;
            u << cfg_.clob_url << "/prices-history?market=" << token_ids[i]
              << "&startTs=" << start_ts << "&endTs=" << end_ts
              << "&fidelity=" << std::max<std::size_t>(1, fidelity_minutes);
            const auto one = http_.get(u.str());
            if (one.status < 200 || one.status >= 300) continue;
            const auto root = json::parse(one.body);
            if (!root.is_object()) continue;
            auto hi = root.as_object().find("history");
            if (hi != root.as_object().end() && hi->value().is_array()) {
                parse_history_array(hi->value().as_array(), out[token_ids[i]]);
            }
        }
    }
    return out;
}
'''
new_function = '''std::unordered_map<std::string,std::vector<PricePoint>> PolymarketApi::fetch_price_history(
    const std::vector<std::string>& token_ids,
    std::int64_t start_ts,
    std::int64_t end_ts,
    std::size_t fidelity_minutes) const {
    std::unordered_map<std::string,std::vector<PricePoint>> out;
    if (token_ids.empty() || end_ts <= start_ts) return out;

    const std::string interval = history_interval_for_range(start_ts, end_ts);
    const auto fidelity = static_cast<std::int64_t>(std::max<std::size_t>(1, fidelity_minutes));
    auto trim = [&](std::vector<PricePoint>& points) {
        points.erase(std::remove_if(points.begin(), points.end(), [&](const PricePoint& point) {
            return point.ts < start_ts || point.ts > end_ts;
        }), points.end());
    };

    constexpr std::size_t batch_size = 20;
    for (std::size_t pos = 0; pos < token_ids.size(); pos += batch_size) {
        const auto end = std::min(token_ids.size(), pos + batch_size);
        json::array ids;
        for (std::size_t i = pos; i < end; ++i) ids.emplace_back(token_ids[i]);
        json::object req{
            {"markets", std::move(ids)},
            {"interval", interval},
            {"fidelity", fidelity}
        };

        std::set<std::string> received;
        try {
            const auto r = http_.post_json(cfg_.clob_url + "/batch-prices-history", json::serialize(req));
            if (r.status >= 200 && r.status < 300) {
                const auto root = json::parse(r.body);
                if (root.is_object()) {
                    auto hi = root.as_object().find("history");
                    if (hi != root.as_object().end() && hi->value().is_object()) {
                        for (const auto& kv : hi->value().as_object()) {
                            if (!kv.value().is_array()) continue;
                            const std::string token(kv.key());
                            auto& points = out[token];
                            parse_history_array(kv.value().as_array(), points);
                            trim(points);
                            if (!points.empty()) received.insert(token);
                        }
                    }
                }
            }
        } catch (...) {
            // Per-token fallback below preserves availability and diagnostics in
            // callers even when the batch endpoint changes or one batch fails.
        }

        for (std::size_t i = pos; i < end; ++i) {
            const auto& token = token_ids[i];
            if (received.count(token)) continue;
            std::ostringstream u;
            u << cfg_.clob_url << "/prices-history?market=" << token
              << "&interval=" << interval
              << "&fidelity=" << fidelity;
            try {
                const auto one = http_.get(u.str());
                if (one.status < 200 || one.status >= 300) continue;
                const auto root = json::parse(one.body);
                if (!root.is_object()) continue;
                auto hi = root.as_object().find("history");
                if (hi != root.as_object().end() && hi->value().is_array()) {
                    auto& points = out[token];
                    points.clear();
                    parse_history_array(hi->value().as_array(), points);
                    trim(points);
                }
            } catch (...) {}
        }
    }
    return out;
}
'''
replace_once("src/api.cpp", old_function, new_function)

replace_once(
    "tests/test_core.cpp",
    '#include "pm/engine.hpp"\n',
    '#include "pm/api.hpp"\n#include "pm/engine.hpp"\n',
)
replace_once(
    "tests/test_core.cpp",
    "int main(){\n",
    "int main(){\n"
    "    assert(pm::history_interval_for_range(0, 3'600) == \"1h\");\n"
    "    assert(pm::history_interval_for_range(0, 6 * 3'600) == \"6h\");\n"
    "    assert(pm::history_interval_for_range(0, 14 * 86'400) == \"1m\");\n"
    "    assert(pm::history_interval_for_range(0, 45 * 86'400) == \"max\");\n\n",
)

# Keep the source branch free of one-shot patching files after application.
Path(__file__).unlink()
print("fixed price-history interval selection and fallback")
