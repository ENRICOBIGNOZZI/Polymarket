#include "poly/engine.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cctype>
#include <curl/curl.h>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <utility>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>

namespace poly {
namespace {
using boost::property_tree::ptree;

size_t curl_write(void* data, size_t size, size_t nmemb, void* user) {
    const auto n = size * nmemb;
    static_cast<std::string*>(user)->append(static_cast<char*>(data), n);
    return n;
}

double dval(const ptree& p, const std::string& k, double d = 0.0) {
    if (auto x = p.get_optional<double>(k)) return *x;
    if (auto x = p.get_optional<std::string>(k)) {
        try { return std::stod(*x); } catch (...) {}
    }
    return d;
}

bool bval(const ptree& p, const std::string& k, bool d = false) {
    if (auto x = p.get_optional<bool>(k)) return *x;
    if (auto x = p.get_optional<std::string>(k)) {
        auto s = *x;
        std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c){ return static_cast<char>(std::tolower(c)); });
        if (s == "true" || s == "1") return true;
        if (s == "false" || s == "0") return false;
    }
    return d;
}

std::vector<std::string> parse_arr(const std::string& raw) {
    std::vector<std::string> out;
    if (raw.empty()) return out;
    try {
        std::istringstream is(raw);
        ptree a;
        boost::property_tree::read_json(is, a);
        for (const auto& x : a) out.push_back(x.second.get_value<std::string>());
    } catch (...) {}
    return out;
}

std::vector<std::string> arr(const ptree& p, const std::string& k) {
    if (auto c = p.get_child_optional(k)) {
        if (!c->empty()) {
            std::vector<std::string> out;
            for (const auto& x : *c) out.push_back(x.second.get_value<std::string>());
            return out;
        }
        if (!c->data().empty()) return parse_arr(c->data());
    }
    if (auto s = p.get_optional<std::string>(k)) return parse_arr(*s);
    return {};
}

std::string event_id(const ptree& p) {
    if (auto es = p.get_child_optional("events")) {
        for (const auto& e : *es) {
            if (auto s = e.second.get_optional<std::string>("id")) return *s;
            if (auto n = e.second.get_optional<long long>("id")) return std::to_string(*n);
        }
    }
    return p.get<std::string>("eventId", "");
}

Market parse_market(const ptree& p) {
    Market m;
    m.id = p.get<std::string>("id", "");
    m.question = p.get<std::string>("question", "");
    m.slug = p.get<std::string>("slug", p.get<std::string>("market_slug", ""));
    m.condition_id = p.get<std::string>("conditionId", p.get<std::string>("condition_id", ""));
    m.event_id = event_id(p);
    m.end_date = p.get<std::string>("endDate", p.get<std::string>("end_date_iso", ""));
    m.active = bval(p, "active");
    m.closed = bval(p, "closed");
    m.accepting_orders = bval(p, "acceptingOrders", bval(p, "accepting_orders", true));
    m.neg_risk = bval(p, "negRisk", bval(p, "neg_risk", false));
    m.fees_enabled = bval(p, "feesEnabled", false);
    m.volume_24h = dval(p, "volume24hr", dval(p, "volume24hrClob"));
    m.liquidity = dval(p, "liquidityNum", dval(p, "liquidity"));

    auto tok = arr(p, "clobTokenIds");
    auto pr = arr(p, "outcomePrices");
    if (tok.size() >= 2) {
        m.yes_token = tok[0];
        m.no_token = tok[1];
    }
    if (!pr.empty()) {
        try { m.gamma_yes_price = std::stod(pr[0]); } catch (...) {}
    }

    if (auto fs = p.get_child_optional("feeSchedule")) {
        m.fees.rate = dval(*fs, "rate", 0.0);
        m.fees.exponent = dval(*fs, "exponent", 1.0);
        m.fees.taker_only = bval(*fs, "takerOnly", true);
        m.fees.live = true;
        m.fees_enabled = m.fees_enabled || m.fees.rate > 0.0;
    }
    return m;
}

BookSnapshot parse_book(const ptree& p) {
    BookSnapshot b;
    b.token_id = p.get<std::string>("asset_id", p.get<std::string>("token_id", ""));
    if (auto xs = p.get_child_optional("bids")) {
        for (const auto& x : *xs) {
            Level l{dval(x.second, "price"), dval(x.second, "size")};
            if (l.price > 0.0 && l.price < 1.0 && l.size > 0.0) b.bids.push_back(l);
        }
    }
    if (auto xs = p.get_child_optional("asks")) {
        for (const auto& x : *xs) {
            Level l{dval(x.second, "price"), dval(x.second, "size")};
            if (l.price > 0.0 && l.price < 1.0 && l.size > 0.0) b.asks.push_back(l);
        }
    }
    for (const auto& l : b.bids) b.best_bid = std::max(b.best_bid, l.price);
    for (const auto& l : b.asks) b.best_ask = std::min(b.best_ask, l.price);
    for (const auto& l : b.bids) if (b.best_bid - l.price <= 0.0300001) b.bid_depth += l.size;
    for (const auto& l : b.asks) if (l.price - b.best_ask <= 0.0300001) b.ask_depth += l.size;
    return b;
}

std::string url_encode(const std::string& s) {
    static constexpr char hex[] = "0123456789ABCDEF";
    std::string out;
    out.reserve(s.size() * 3);
    for (unsigned char c : s) {
        const bool safe = std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~';
        if (safe) out.push_back(static_cast<char>(c));
        else {
            out.push_back('%');
            out.push_back(hex[(c >> 4) & 0x0F]);
            out.push_back(hex[c & 0x0F]);
        }
    }
    return out;
}

std::string curl_request(const std::string& url, const std::string* body) {
    std::string last_error;
    for (int attempt = 0; attempt < 3; ++attempt) {
        CURL* c = curl_easy_init();
        if (!c) throw std::runtime_error("curl init failed");
        std::string out;
        curl_easy_setopt(c, CURLOPT_URL, url.c_str());
        curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, curl_write);
        curl_easy_setopt(c, CURLOPT_WRITEDATA, &out);
        curl_easy_setopt(c, CURLOPT_USERAGENT, "PolymarketQuantEngine/0.5");
        curl_easy_setopt(c, CURLOPT_TIMEOUT, 20L);
        curl_easy_setopt(c, CURLOPT_CONNECTTIMEOUT, 8L);
        curl_easy_setopt(c, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(c, CURLOPT_ACCEPT_ENCODING, "");
        curl_easy_setopt(c, CURLOPT_TCP_KEEPALIVE, 1L);
        struct curl_slist* headers = nullptr;
        if (body) {
            headers = curl_slist_append(headers, "Content-Type: application/json");
            curl_easy_setopt(c, CURLOPT_HTTPHEADER, headers);
            curl_easy_setopt(c, CURLOPT_POST, 1L);
            curl_easy_setopt(c, CURLOPT_POSTFIELDS, body->c_str());
            curl_easy_setopt(c, CURLOPT_POSTFIELDSIZE, static_cast<long>(body->size()));
        }
        const auto rc = curl_easy_perform(c);
        long status = 0;
        curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, &status);
        if (headers) curl_slist_free_all(headers);
        curl_easy_cleanup(c);

        if (rc == CURLE_OK && status >= 200 && status < 300) return out;
        if (rc != CURLE_OK) last_error = std::string("HTTP failure: ") + curl_easy_strerror(rc);
        else last_error = "HTTP status " + std::to_string(status) + " for " + url;

        const bool transient = rc != CURLE_OK || status == 429 || status >= 500;
        if (!transient || attempt == 2) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(250 * (1 << attempt)));
    }
    throw std::runtime_error(last_error);
}

} // namespace

HttpClient::HttpClient() {
    static const int once = [](){ curl_global_init(CURL_GLOBAL_DEFAULT); return 1; }();
    (void)once;
}
HttpClient::~HttpClient() = default;
std::string HttpClient::get(const std::string& u) const { return curl_request(u, nullptr); }
std::string HttpClient::post_json(const std::string& u, const std::string& b) const { return curl_request(u, &b); }

std::vector<Market> parse_gamma_markets_json(const std::string& body) {
    std::istringstream is(body);
    ptree root;
    boost::property_tree::read_json(is, root);
    const ptree* list = &root;
    if (auto markets = root.get_child_optional("markets")) list = &*markets;

    std::vector<Market> out;
    for (const auto& it : *list) {
        const auto m = parse_market(it.second);
        if (!m.id.empty() && !m.yes_token.empty() && !m.no_token.empty()) out.push_back(m);
    }
    return out;
}

PolymarketClient::PolymarketClient(HttpClient h) : http_(std::move(h)) {}

std::vector<Market> PolymarketClient::discover_markets(std::size_t n) const {
    if (n == 0) return {};
    std::vector<Market> out;
    std::string cursor;
    const auto page_cap = std::min<std::size_t>(100, n);
    try {
        while (out.size() < n) {
            const auto page_size = std::min(page_cap, n - out.size());
            std::string url = "https://gamma-api.polymarket.com/markets/keyset?active=true&closed=false&order=volume24hr&ascending=false&limit=" + std::to_string(page_size);
            if (!cursor.empty()) url += "&after_cursor=" + url_encode(cursor);
            const auto body = http_.get(url);
            auto page = parse_gamma_markets_json(body);
            out.insert(out.end(), page.begin(), page.end());

            std::istringstream is(body);
            ptree root;
            boost::property_tree::read_json(is, root);
            const auto next = root.get<std::string>("next_cursor", "");
            if (page.empty() || next.empty() || next == cursor) break;
            cursor = next;
        }
    } catch (const std::exception& e) {
        std::cerr << "[warn] keyset market discovery failed: " << e.what() << "; falling back to legacy listing\n";
        const auto limit = std::min<std::size_t>(n, 1000);
        out = parse_gamma_markets_json(http_.get(
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit=" + std::to_string(limit)));
    }
    if (out.size() > n) out.resize(n);
    return out;
}

Market PolymarketClient::get_market(const std::string& id) const {
    auto v = parse_gamma_markets_json("[" + http_.get("https://gamma-api.polymarket.com/markets/" + id) + "]");
    if (v.empty()) throw std::runtime_error("Gamma market parse failed for " + id);
    return v.front();
}

BookSnapshot PolymarketClient::get_book(const std::string& t) const {
    std::istringstream is(http_.get("https://clob.polymarket.com/book?token_id=" + t));
    ptree p;
    boost::property_tree::read_json(is, p);
    auto b = parse_book(p);
    if (b.token_id.empty()) b.token_id = t;
    return b;
}

std::unordered_map<std::string, BookSnapshot> PolymarketClient::get_books(const std::vector<std::string>& ids) const {
    std::unordered_map<std::string, BookSnapshot> out;
    constexpr std::size_t batch = 100;
    for (std::size_t i = 0; i < ids.size(); i += batch) {
        std::ostringstream body;
        body << '[';
        for (std::size_t j = i; j < std::min(ids.size(), i + batch); ++j) {
            if (j > i) body << ',';
            body << "{\"token_id\":\"" << ids[j] << "\"}";
        }
        body << ']';
        try {
            std::istringstream is(http_.post_json("https://clob.polymarket.com/books", body.str()));
            ptree root;
            boost::property_tree::read_json(is, root);
            for (const auto& x : root) {
                auto b = parse_book(x.second);
                if (!b.token_id.empty()) out[b.token_id] = std::move(b);
            }
        } catch (const std::exception& e) {
            std::cerr << "[warn] batch books: " << e.what() << "; falling back to singles\n";
            for (std::size_t j = i; j < std::min(ids.size(), i + batch); ++j) {
                try { out[ids[j]] = get_book(ids[j]); } catch (...) {}
            }
        }
    }
    return out;
}

FeeInfo PolymarketClient::get_fee_info(const std::string& condition) const {
    if (condition.empty()) return {};
    if (auto i = fee_cache_.find(condition); i != fee_cache_.end()) return i->second;
    FeeInfo f;
    try {
        std::istringstream is(http_.get("https://clob.polymarket.com/clob-markets/" + condition));
        ptree p;
        boost::property_tree::read_json(is, p);
        if (auto fd = p.get_child_optional("fd")) {
            f.rate = dval(*fd, "r", 0.0);
            f.exponent = dval(*fd, "e", 1.0);
            f.taker_only = bval(*fd, "to", true);
            f.live = true;
        } else {
            f = {0.0, 1.0, true, true};
        }
    } catch (...) {
        f.live = false;
    }
    fee_cache_[condition] = f;
    return f;
}

std::vector<LiveMarket> PolymarketClient::snapshot(std::size_t limit, double min_liq, double max_spread) const {
    auto markets = discover_markets(limit);
    std::vector<Market> eligible;
    std::vector<std::string> tokens;
    eligible.reserve(markets.size());
    tokens.reserve(markets.size() * 2);
    for (auto& m : markets) {
        if (m.accepting_orders && m.liquidity >= min_liq) {
            tokens.push_back(m.yes_token);
            tokens.push_back(m.no_token);
            eligible.push_back(std::move(m));
        }
    }

    auto books = get_books(tokens);
    std::vector<LiveMarket> out;
    out.reserve(eligible.size());
    for (auto& m : eligible) {
        auto yi = books.find(m.yes_token), ni = books.find(m.no_token);
        if (yi == books.end() || ni == books.end()) continue;
        auto y = yi->second, no = ni->second;
        if (!y.two_sided() || !no.two_sided()) continue;
        if (y.spread() > max_spread || no.spread() > max_spread) continue;
        if (!m.fees.live) m.fees = get_fee_info(m.condition_id);
        out.push_back({m, std::move(y), std::move(no), hash_text_embedding(m.question)});
    }
    return out;
}

} // namespace poly
