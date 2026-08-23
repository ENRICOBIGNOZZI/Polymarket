#include "poly/polymarket_client.hpp"

#include <curl/curl.h>
#include <json-c/json.h>

#include <algorithm>
#include <cmath>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>

namespace poly {
namespace {

size_t write_cb(void* contents, size_t size, size_t nmemb, void* userp) {
    const size_t total = size * nmemb;
    static_cast<std::string*>(userp)->append(static_cast<char*>(contents), total);
    return total;
}

using JsonPtr = std::unique_ptr<json_object, decltype(&json_object_put)>;

std::string jstr(json_object* obj, const char* key, const std::string& fallback = {}) {
    json_object* v = nullptr;
    if (!obj || !json_object_object_get_ex(obj, key, &v) || !v || json_object_is_type(v, json_type_null)) return fallback;
    if (json_object_is_type(v, json_type_string)) return json_object_get_string(v);
    return json_object_to_json_string_ext(v, JSON_C_TO_STRING_PLAIN);
}

double jdouble(json_object* obj, const char* key, double fallback = 0.0) {
    json_object* v = nullptr;
    if (!obj || !json_object_object_get_ex(obj, key, &v) || !v || json_object_is_type(v, json_type_null)) return fallback;
    if (json_object_is_type(v, json_type_double) || json_object_is_type(v, json_type_int)) return json_object_get_double(v);
    if (json_object_is_type(v, json_type_string)) {
        try { return std::stod(json_object_get_string(v)); } catch (...) { return fallback; }
    }
    return fallback;
}

bool jbool(json_object* obj, const char* key, bool fallback = false) {
    json_object* v = nullptr;
    if (!obj || !json_object_object_get_ex(obj, key, &v) || !v || json_object_is_type(v, json_type_null)) return fallback;
    return json_object_get_boolean(v) != 0;
}

std::vector<std::string> parse_string_array_field(json_object* obj, const char* key) {
    json_object* v = nullptr;
    if (!obj || !json_object_object_get_ex(obj, key, &v) || !v) return {};
    JsonPtr parsed(nullptr, json_object_put);
    json_object* arr = v;
    if (json_object_is_type(v, json_type_string)) {
        parsed.reset(json_tokener_parse(json_object_get_string(v)));
        arr = parsed.get();
    }
    if (!arr || !json_object_is_type(arr, json_type_array)) return {};
    std::vector<std::string> out;
    const int n = json_object_array_length(arr);
    out.reserve(n);
    for (int i = 0; i < n; ++i) {
        json_object* x = json_object_array_get_idx(arr, i);
        if (x && json_object_is_type(x, json_type_string)) out.emplace_back(json_object_get_string(x));
        else if (x) out.emplace_back(json_object_to_json_string_ext(x, JSON_C_TO_STRING_PLAIN));
    }
    return out;
}

std::vector<double> parse_double_array_field(json_object* obj, const char* key) {
    json_object* v = nullptr;
    if (!obj || !json_object_object_get_ex(obj, key, &v) || !v) return {};
    JsonPtr parsed(nullptr, json_object_put);
    json_object* arr = v;
    if (json_object_is_type(v, json_type_string)) {
        parsed.reset(json_tokener_parse(json_object_get_string(v)));
        arr = parsed.get();
    }
    if (!arr || !json_object_is_type(arr, json_type_array)) return {};
    std::vector<double> out;
    const int n = json_object_array_length(arr);
    out.reserve(n);
    for (int i = 0; i < n; ++i) {
        json_object* x = json_object_array_get_idx(arr, i);
        if (!x) { out.push_back(std::numeric_limits<double>::quiet_NaN()); continue; }
        if (json_object_is_type(x, json_type_double) || json_object_is_type(x, json_type_int)) out.push_back(json_object_get_double(x));
        else {
            try { out.push_back(std::stod(json_object_get_string(x))); }
            catch (...) { out.push_back(std::numeric_limits<double>::quiet_NaN()); }
        }
    }
    return out;
}

std::vector<BookLevel> parse_levels(json_object* obj, const char* key) {
    json_object* arr = nullptr;
    if (!obj || !json_object_object_get_ex(obj, key, &arr) || !arr || !json_object_is_type(arr, json_type_array)) return {};
    std::vector<BookLevel> out;
    const int n = json_object_array_length(arr);
    out.reserve(n);
    for (int i = 0; i < n; ++i) {
        json_object* x = json_object_array_get_idx(arr, i);
        const double p = jdouble(x, "price", std::numeric_limits<double>::quiet_NaN());
        const double s = jdouble(x, "size", 0.0);
        if (std::isfinite(p) && s > 0.0) out.push_back({p, s});
    }
    return out;
}

} // namespace

PolymarketClient::PolymarketClient(ClientConfig cfg) : cfg_(std::move(cfg)) {
    curl_global_init(CURL_GLOBAL_DEFAULT);
}

std::string PolymarketClient::http_get(const std::string& url) {
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("curl_easy_init failed");
    std::string body;
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 12000L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "polymarket-quant-engine/0.1");
    const auto rc = curl_easy_perform(curl);
    long code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
    curl_easy_cleanup(curl);
    if (rc != CURLE_OK || code < 200 || code >= 300) {
        throw std::runtime_error("HTTP GET failed: " + url + " status=" + std::to_string(code));
    }
    return body;
}

std::string PolymarketClient::http_post_json(const std::string& url, const std::string& payload) {
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("curl_easy_init failed");
    std::string body;
    curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(payload.size()));
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "polymarket-quant-engine/0.1");
    const auto rc = curl_easy_perform(curl);
    long code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    if (rc != CURLE_OK || code < 200 || code >= 300) {
        throw std::runtime_error("HTTP POST failed: " + url + " status=" + std::to_string(code));
    }
    return body;
}

std::unordered_map<std::string, TokenMeta> PolymarketClient::discover_tokens() const {
    std::unordered_map<std::string, TokenMeta> out;
    const std::size_t page = std::max<std::size_t>(1, cfg_.gamma_page_size);

    for (std::size_t offset = 0; offset < cfg_.max_markets; offset += page) {
        std::ostringstream url;
        url << "https://gamma-api.polymarket.com/markets?closed=false&limit=" << page
            << "&offset=" << offset << "&order=liquidityNum&ascending=false";
        const std::string text = http_get(url.str());
        JsonPtr root(json_tokener_parse(text.c_str()), json_object_put);
        if (!root || !json_object_is_type(root.get(), json_type_array)) break;
        const int n = json_object_array_length(root.get());
        if (n == 0) break;

        for (int i = 0; i < n; ++i) {
            json_object* m = json_object_array_get_idx(root.get(), i);
            if (!m || !jbool(m, "active", true) || jbool(m, "closed", false) || !jbool(m, "enableOrderBook", false)) continue;

            auto token_ids = parse_string_array_field(m, "clobTokenIds");
            auto outcomes = parse_string_array_field(m, "outcomes");
            auto prices = parse_double_array_field(m, "outcomePrices");
            if (token_ids.empty()) continue;
            if (outcomes.size() < token_ids.size()) outcomes.resize(token_ids.size(), "Outcome");
            if (prices.size() < token_ids.size()) prices.resize(token_ids.size(), std::numeric_limits<double>::quiet_NaN());

            std::string event_id;
            std::string event_title;
            std::string event_category;
            json_object* events = nullptr;
            if (json_object_object_get_ex(m, "events", &events) && events && json_object_is_type(events, json_type_array) && json_object_array_length(events) > 0) {
                json_object* e0 = json_object_array_get_idx(events, 0);
                event_id = jstr(e0, "id");
                event_title = jstr(e0, "title");
                event_category = jstr(e0, "category");
            }

            const std::string market_id = jstr(m, "id");
            const std::string condition_id = jstr(m, "conditionId");
            const std::string question = jstr(m, "question");
            std::string category = jstr(m, "category", event_category);
            if (category.empty()) category = event_category.empty() ? "other" : event_category;
            const bool neg_risk = jbool(m, "negRisk", false);
            const double liquidity = jdouble(m, "liquidityNum", jdouble(m, "liquidity", 0.0));
            const double volume = jdouble(m, "volumeNum", jdouble(m, "volume", 0.0));

            for (std::size_t k = 0; k < token_ids.size(); ++k) {
                TokenMeta t;
                t.token_id = token_ids[k];
                t.market_id = market_id;
                t.condition_id = condition_id;
                t.event_id = event_id.empty() ? market_id : event_id;
                t.event_title = event_title;
                t.question = question;
                t.slug = jstr(m, "slug");
                t.category = category;
                t.outcome = outcomes[k];
                t.end_date = jstr(m, "endDate");
                t.active = true;
                t.closed = false;
                t.neg_risk = neg_risk;
                t.enable_order_book = true;
                t.liquidity = liquidity;
                t.volume = volume;
                t.reference_price = prices[k];
                out[t.token_id] = std::move(t);
            }
        }
        if (static_cast<std::size_t>(n) < page) break;
    }
    return out;
}

std::unordered_map<std::string, BookSnapshot> PolymarketClient::fetch_books(
        const std::unordered_map<std::string, TokenMeta>& tokens) const {
    std::vector<std::string> ids;
    ids.reserve(tokens.size());
    for (const auto& [id, m] : tokens) {
        if (m.active && !m.closed && m.enable_order_book && m.liquidity >= cfg_.min_liquidity_for_book) ids.push_back(id);
    }
    std::unordered_map<std::string, BookSnapshot> out;

    for (std::size_t start = 0; start < ids.size(); start += cfg_.book_batch_size) {
        const std::size_t stop = std::min(ids.size(), start + cfg_.book_batch_size);
        json_object* arr = json_object_new_array();
        for (std::size_t i = start; i < stop; ++i) {
            json_object* x = json_object_new_object();
            json_object_object_add(x, "token_id", json_object_new_string(ids[i].c_str()));
            json_object_array_add(arr, x);
        }
        const std::string payload = json_object_to_json_string_ext(arr, JSON_C_TO_STRING_PLAIN);
        json_object_put(arr);

        try {
            const std::string text = http_post_json("https://clob.polymarket.com/books", payload);
            JsonPtr root(json_tokener_parse(text.c_str()), json_object_put);
            if (!root || !json_object_is_type(root.get(), json_type_array)) continue;
            const int n = json_object_array_length(root.get());
            for (int i = 0; i < n; ++i) {
                json_object* b = json_object_array_get_idx(root.get(), i);
                const std::string token = jstr(b, "asset_id");
                if (token.empty()) continue;
                BookSnapshot snap;
                snap.token_id = token;
                snap.market_id = jstr(b, "market");
                snap.bids = parse_levels(b, "bids");
                snap.asks = parse_levels(b, "asks");
                snap.last_trade = jdouble(b, "last_trade_price", std::numeric_limits<double>::quiet_NaN());
                snap.tick_size = jdouble(b, "tick_size", 0.01);
                snap.min_order_size = jdouble(b, "min_order_size", 1.0);
                snap.neg_risk = jbool(b, "neg_risk", false);
                out[token] = std::move(snap);
            }
        } catch (const std::exception& e) {
            std::cerr << "book batch failed: " << e.what() << '\n';
        }
    }
    return out;
}

std::unordered_map<std::string, std::vector<double>> PolymarketClient::fetch_warm_history(
        const std::unordered_map<std::string, TokenMeta>& tokens) const {
    std::vector<std::pair<double, std::string>> ranked;
    ranked.reserve(tokens.size());
    for (const auto& [id, m] : tokens) {
        if (!m.active || m.closed || !m.enable_order_book || m.liquidity < cfg_.min_liquidity_for_book) continue;
        ranked.emplace_back(m.liquidity + 0.01 * m.volume, id);
    }
    std::sort(ranked.begin(), ranked.end(), [](const auto& a, const auto& b){ return a.first > b.first; });
    if (ranked.size() > cfg_.warm_start_tokens) ranked.resize(cfg_.warm_start_tokens);

    std::unordered_map<std::string, std::vector<double>> out;
    const std::size_t batch = std::clamp<std::size_t>(cfg_.history_batch_size, 1, 20);
    for (std::size_t start = 0; start < ranked.size(); start += batch) {
        const std::size_t stop = std::min(ranked.size(), start + batch);
        json_object* root_req = json_object_new_object();
        json_object* arr = json_object_new_array();
        for (std::size_t i = start; i < stop; ++i) {
            json_object_array_add(arr, json_object_new_string(ranked[i].second.c_str()));
        }
        json_object_object_add(root_req, "markets", arr);
        json_object_object_add(root_req, "interval", json_object_new_string("6h"));
        json_object_object_add(root_req, "fidelity", json_object_new_int(cfg_.history_fidelity_min));
        const std::string payload = json_object_to_json_string_ext(root_req, JSON_C_TO_STRING_PLAIN);
        json_object_put(root_req);

        try {
            const std::string text = http_post_json("https://clob.polymarket.com/batch-prices-history", payload);
            JsonPtr root(json_tokener_parse(text.c_str()), json_object_put);
            json_object* history = nullptr;
            if (!root || !json_object_object_get_ex(root.get(), "history", &history) ||
                !history || !json_object_is_type(history, json_type_object)) continue;
            json_object_object_foreach(history, key, arr_hist) {
                if (!arr_hist || !json_object_is_type(arr_hist, json_type_array)) continue;
                std::vector<double> px;
                const int n = json_object_array_length(arr_hist);
                px.reserve(n);
                for (int i = 0; i < n; ++i) {
                    json_object* point = json_object_array_get_idx(arr_hist, i);
                    const double p = jdouble(point, "p", std::numeric_limits<double>::quiet_NaN());
                    if (std::isfinite(p) && p > 0.0 && p < 1.0) px.push_back(p);
                }
                if (!px.empty()) out[key] = std::move(px);
            }
        } catch (const std::exception& e) {
            std::cerr << "history batch failed: " << e.what() << '\n';
        }
    }
    return out;
}

} // namespace poly
