#include "pm/api.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <cctype>
#include <ctime>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <unordered_set>

namespace pm {
namespace json = boost::json;
namespace {

double as_double(const json::value& v, double d = 0.0) {
    try {
        if (v.is_double()) return v.as_double();
        if (v.is_int64()) return static_cast<double>(v.as_int64());
        if (v.is_uint64()) return static_cast<double>(v.as_uint64());
        if (v.is_string()) return std::stod(std::string(v.as_string()));
    } catch (...) {}
    return d;
}

bool as_bool(const json::value& v, bool d = false) {
    if (v.is_bool()) return v.as_bool();
    if (v.is_int64()) return v.as_int64() != 0;
    if (v.is_string()) {
        auto s = std::string(v.as_string());
        std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        if (s == "true" || s == "1") return true;
        if (s == "false" || s == "0") return false;
    }
    return d;
}

std::string as_string(const json::value& v) {
    if (v.is_string()) return std::string(v.as_string());
    if (v.is_int64()) return std::to_string(v.as_int64());
    if (v.is_uint64()) return std::to_string(v.as_uint64());
    return {};
}

std::vector<std::string> string_array(const json::object& o, const char* key) {
    std::vector<std::string> out;
    auto it = o.find(key);
    if (it == o.end()) return out;
    json::value v = it->value();
    if (v.is_string()) {
        boost::system::error_code ec;
        auto parsed = json::parse(v.as_string(), ec);
        if (!ec) v = std::move(parsed);
        else {
            out.push_back(std::string(v.as_string()));
            return out;
        }
    }
    if (v.is_array()) {
        for (const auto& x : v.as_array()) out.push_back(as_string(x));
    }
    return out;
}

std::string url_encode(const std::string& raw) {
    std::ostringstream out;
    out << std::uppercase << std::hex;
    for (const unsigned char c : raw) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            out << static_cast<char>(c);
        } else {
            out << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(c);
        }
    }
    return out.str();
}

std::int64_t parse_time_value(const json::value& v) {
    if (v.is_int64()) return v.as_int64();
    if (v.is_uint64()) return static_cast<std::int64_t>(v.as_uint64());
    if (v.is_double()) return static_cast<std::int64_t>(v.as_double());
    if (!v.is_string()) return 0;

    const std::string raw(v.as_string());
    if (raw.empty()) return 0;
    try {
        std::size_t used = 0;
        const auto numeric = std::stoll(raw, &used);
        if (used == raw.size()) return numeric;
    } catch (...) {}

    std::tm tm{};
    std::istringstream iso(raw.substr(0, 19));
    iso >> std::get_time(&tm, "%Y-%m-%dT%H:%M:%S");
    if (iso.fail()) {
        tm = {};
        std::istringstream spaced(raw.substr(0, 19));
        spaced >> std::get_time(&tm, "%Y-%m-%d %H:%M:%S");
        if (spaced.fail()) return 0;
    }
#if defined(_WIN32)
    return static_cast<std::int64_t>(_mkgmtime(&tm));
#else
    return static_cast<std::int64_t>(timegm(&tm));
#endif
}

MarketTiming timing_from_object(const json::object& o) {
    MarketTiming timing;
    auto apply = [&](const json::object& x) {
        if (timing.game_start_ts <= 0) {
            if (auto it = x.find("gameStartTime"); it != x.end()) {
                timing.game_start_ts = parse_time_value(it->value());
                timing.timed_sports = timing.timed_sports || timing.game_start_ts > 0;
            }
        }
        if (!timing.timed_sports) {
            if (auto it = x.find("sportsMarketType"); it != x.end()) {
                timing.timed_sports = !as_string(it->value()).empty();
            }
        }
        if (timing.seconds_delay == 0) {
            if (auto it = x.find("secondsDelay"); it != x.end()) {
                timing.seconds_delay = static_cast<int>(as_double(it->value(), 0.0));
            }
        }
    };

    apply(o);
    if (auto it = o.find("events"); it != o.end() && it->value().is_array() &&
        !it->value().as_array().empty() && it->value().as_array().front().is_object()) {
        apply(it->value().as_array().front().as_object());
    }
    return timing;
}

std::optional<int> resolution_from_market(
    const json::object& o,
    const std::vector<std::string>& outcomes) {
    auto it = o.find("outcomePrices");
    if (it == o.end()) return std::nullopt;
    json::value v = it->value();
    if (v.is_string()) {
        boost::system::error_code ec;
        auto p = json::parse(v.as_string(), ec);
        if (!ec) v = std::move(p);
    }
    if (!v.is_array() || v.as_array().size() < 2 || outcomes.size() < 2) return std::nullopt;
    const double p0 = as_double(v.as_array()[0], -1.0);
    const double p1 = as_double(v.as_array()[1], -1.0);
    if (std::max(p0, p1) < 0.999) return std::nullopt;
    const int win = p0 > p1 ? 0 : 1;
    std::string label = outcomes[static_cast<std::size_t>(win)];
    std::transform(label.begin(), label.end(), label.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    if (label == "yes") return 1;
    if (label == "no") return 0;
    return std::nullopt;
}

std::optional<Market> parse_market_object(const json::object& o,
                                          double min_liquidity,
                                          bool require_tradable = true) {
    Market m;
    if (auto it = o.find("id"); it != o.end()) m.id = as_string(it->value());
    if (auto it = o.find("conditionId"); it != o.end()) m.condition_id = as_string(it->value());
    if (auto it = o.find("slug"); it != o.end()) m.slug = as_string(it->value());
    if (auto it = o.find("question"); it != o.end()) m.question = as_string(it->value());
    if (auto it = o.find("liquidityNum"); it != o.end()) m.liquidity = as_double(it->value());
    else if (auto it = o.find("liquidity"); it != o.end()) m.liquidity = as_double(it->value());
    if (auto it = o.find("volume24hr"); it != o.end()) m.volume24h = as_double(it->value());
    if (auto it = o.find("negRisk"); it != o.end()) m.neg_risk = as_bool(it->value());
    if (auto it = o.find("active"); it != o.end()) m.active = as_bool(it->value(), true);
    if (auto it = o.find("closed"); it != o.end()) m.closed = as_bool(it->value());
    if (auto it = o.find("enableOrderBook"); it != o.end()) m.enable_order_book = as_bool(it->value(), true);
    if (auto it = o.find("acceptingOrders"); it != o.end()) m.accepting_orders = as_bool(it->value(), true);
    const auto timing = timing_from_object(o);
    m.timed_sports = timing.timed_sports;
    m.game_start_ts = timing.game_start_ts;
    m.seconds_delay = timing.seconds_delay;
    if (auto it = o.find("eventId"); it != o.end()) m.event_id = as_string(it->value());
    if (m.event_id.empty()) {
        auto it = o.find("events");
        if (it != o.end() && it->value().is_array() && !it->value().as_array().empty() &&
            it->value().as_array().front().is_object()) {
            const auto& eo = it->value().as_array().front().as_object();
            if (auto ei = eo.find("id"); ei != eo.end()) m.event_id = as_string(ei->value());
            if (auto nr = eo.find("negRisk"); nr != eo.end()) m.neg_risk = m.neg_risk || as_bool(nr->value());
        }
    }
    if (m.event_id.empty()) m.event_id = m.condition_id.empty() ? m.id : m.condition_id;

    const auto tokens = string_array(o, "clobTokenIds");
    const auto outcomes = string_array(o, "outcomes");
    if (tokens.size() < 2) return std::nullopt;
    int yi = 0, ni = 1;
    for (std::size_t i = 0; i < outcomes.size() && i < tokens.size(); ++i) {
        auto s = outcomes[i];
        std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        if (s == "yes") yi = static_cast<int>(i);
        else if (s == "no") ni = static_cast<int>(i);
    }
    m.yes_token = tokens[static_cast<std::size_t>(yi)];
    m.no_token = tokens[static_cast<std::size_t>(ni)];
    m.resolved_yes = m.closed ? resolution_from_market(o, outcomes) : std::nullopt;

    if (auto it = o.find("feeSchedule"); it != o.end() && it->value().is_object()) {
        const auto& fs = it->value().as_object();
        if (auto x = fs.find("rate"); x != fs.end()) m.fee_rate = as_double(x->value(), -1.0);
        if (auto x = fs.find("exponent"); x != fs.end()) m.fee_exponent = as_double(x->value(), 1.0);
        if (auto x = fs.find("takerOnly"); x != fs.end()) m.fee_taker_only = as_bool(x->value(), true);
    } else if (auto it = o.find("feesEnabled"); it != o.end() && !as_bool(it->value(), false)) {
        m.fee_rate = 0.0;
    }

    if (m.id.empty() || m.condition_id.empty() || m.yes_token.empty() || m.no_token.empty()) return std::nullopt;
    if (require_tradable && (!m.active || m.closed || !m.enable_order_book || !m.accepting_orders)) return std::nullopt;
    if (m.liquidity + 1e-12 < min_liquidity) return std::nullopt;
    return m;
}

void parse_history_array(const json::array& arr, std::vector<PricePoint>& out) {
    for (const auto& v : arr) {
        if (!v.is_object()) continue;
        const auto& o = v.as_object();
        auto ti = o.find("t"), pi = o.find("p");
        if (ti == o.end() || pi == o.end()) continue;
        const auto ts = static_cast<std::int64_t>(as_double(ti->value(), 0.0));
        const double p = as_double(pi->value(), -1.0);
        if (ts > 0 && p > 0.0 && p < 1.0) out.push_back({ts, p});
    }
    std::sort(out.begin(), out.end(), [](const auto& a, const auto& b) { return a.ts < b.ts; });
    out.erase(std::unique(out.begin(), out.end(), [](const auto& a, const auto& b) {
        return a.ts == b.ts;
    }), out.end());
}

std::string trade_id(const RecentTrade& t) {
    std::ostringstream s;
    s << t.transaction_hash << ':' << t.token_id << ':' << t.ts << ':' << t.side << ':'
      << std::setprecision(17) << t.price << ':' << t.size;
    return s.str();
}

} // namespace

PolymarketApi::PolymarketApi(Config cfg) : cfg_(std::move(cfg)) {}

std::vector<Market> PolymarketApi::discover_markets(std::size_t limit, double min_liquidity) const {
    const bool unbounded = limit == 0;
    const std::size_t requested = unbounded ? std::numeric_limits<std::size_t>::max() : limit;
    const std::size_t page_size = std::clamp<std::size_t>(cfg_.gamma_page_size, 1, 100);
    std::vector<Market> out;
    out.reserve(unbounded ? 4096 : requested);
    std::unordered_set<std::string> seen;
    std::unordered_set<std::string> seen_cursors;
    std::string cursor;

    while (out.size() < requested) {
        std::ostringstream u;
        u << cfg_.gamma_url << "/markets/keyset?closed=false&limit=" << page_size
          << "&order=liquidity_num&ascending=false"
          << "&liquidity_num_min=" << std::setprecision(12) << min_liquidity;
        if (!cursor.empty()) u << "&after_cursor=" << url_encode(cursor);
        const auto r = http_.get(u.str());
        if (r.status < 200 || r.status >= 300) {
            throw std::runtime_error("Gamma markets keyset HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 300));
        }
        const auto root = json::parse(r.body);
        if (!root.is_object()) throw std::runtime_error("Unexpected Gamma markets keyset response");
        const auto& object = root.as_object();
        const auto markets_it = object.find("markets");
        if (markets_it == object.end() || !markets_it->value().is_array()) {
            throw std::runtime_error("Gamma markets keyset response has no markets array");
        }
        const auto& arr = markets_it->value().as_array();
        for (const auto& v : arr) {
            if (!v.is_object()) continue;
            if (auto m = parse_market_object(v.as_object(), min_liquidity)) {
                if (seen.insert(m->id).second) out.push_back(std::move(*m));
                if (out.size() >= requested) break;
            }
        }
        if (out.size() >= requested) break;
        std::string next_cursor;
        if (const auto cursor_it = object.find("next_cursor"); cursor_it != object.end()) {
            next_cursor = as_string(cursor_it->value());
        }
        if (next_cursor.empty() || next_cursor == cursor || !seen_cursors.insert(next_cursor).second) break;
        cursor = std::move(next_cursor);
        if (arr.empty()) break;
    }
    return out;
}

std::optional<Market> PolymarketApi::fetch_market_by_id(const std::string& id) const {
    const auto r = http_.get(cfg_.gamma_url + "/markets/" + id);
    if (r.status == 404) return std::nullopt;
    if (r.status < 200 || r.status >= 300) {
        throw std::runtime_error("Gamma market HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 300));
    }
    const auto root = json::parse(r.body);
    if (!root.is_object()) return std::nullopt;
    return parse_market_object(root.as_object(), 0.0, false);
}

std::optional<MarketTiming> PolymarketApi::fetch_market_timing(const std::string& id) const {
    const auto r = http_.get(cfg_.gamma_url + "/markets/" + id);
    if (r.status == 404) return std::nullopt;
    if (r.status < 200 || r.status >= 300) {
        throw std::runtime_error("Gamma timing HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 300));
    }
    const auto root = json::parse(r.body);
    if (!root.is_object()) return std::nullopt;
    return timing_from_object(root.as_object());
}

std::unordered_map<std::string,Book> PolymarketApi::fetch_books(
    const std::vector<std::string>& token_ids) const {
    std::unordered_map<std::string,Book> out;
    const std::size_t batch = std::max<std::size_t>(1, cfg_.books_batch_size);
    for (std::size_t pos = 0; pos < token_ids.size(); pos += batch) {
        json::array body;
        const auto end = std::min(token_ids.size(), pos + batch);
        for (std::size_t i = pos; i < end; ++i) body.emplace_back(json::object{{"token_id", token_ids[i]}});
        const auto r = http_.post_json(cfg_.clob_url + "/books", json::serialize(body));
        if (r.status < 200 || r.status >= 300) {
            throw std::runtime_error("CLOB books HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 300));
        }
        const auto root = json::parse(r.body);
        if (!root.is_array()) throw std::runtime_error("Unexpected CLOB books response");
        for (const auto& v : root.as_array()) {
            if (!v.is_object()) continue;
            const auto& o = v.as_object();
            Book b;
            if (auto it = o.find("asset_id"); it != o.end()) b.token_id = as_string(it->value());
            if (auto it = o.find("tick_size"); it != o.end()) b.tick_size = as_double(it->value(), 0.01);
            if (auto it = o.find("min_order_size"); it != o.end()) b.min_order_size = as_double(it->value(), 1.0);
            if (auto it = o.find("neg_risk"); it != o.end()) b.neg_risk = as_bool(it->value());
            if (auto it = o.find("last_trade_price"); it != o.end()) b.last_trade = as_double(it->value());
            auto parse_levels = [&](const char* key, std::vector<Level>& dst) {
                auto it = o.find(key);
                if (it == o.end() || !it->value().is_array()) return;
                for (const auto& lv : it->value().as_array()) {
                    if (!lv.is_object()) continue;
                    const auto& lo = lv.as_object();
                    auto p = lo.find("price"), s = lo.find("size");
                    if (p == lo.end() || s == lo.end()) continue;
                    const double px = as_double(p->value(), -1.0);
                    const double sz = as_double(s->value(), 0.0);
                    if (px > 0.0 && px < 1.0 && sz > 0.0) dst.push_back({px, sz});
                }
            };
            parse_levels("bids", b.bids);
            parse_levels("asks", b.asks);
            if (!b.token_id.empty()) out[b.token_id] = std::move(b);
        }
    }
    return out;
}

std::unordered_map<std::string,std::vector<PricePoint>> PolymarketApi::fetch_price_history(
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

std::vector<RecentTrade> PolymarketApi::fetch_recent_trades(
    const std::vector<std::string>& condition_ids,
    std::int64_t start_ts,
    std::int64_t end_ts,
    std::size_t limit_per_market) const {
    std::vector<RecentTrade> out;
    std::set<std::string> unique;
    const auto limit = std::clamp<std::size_t>(limit_per_market, 1, 10000);

    for (const auto& condition_id : condition_ids) {
        if (condition_id.empty()) continue;
        std::ostringstream u;
        u << "https://data-api.polymarket.com/trades?market=" << condition_id
          << "&limit=" << limit << "&takerOnly=true";
        if (start_ts > 0) u << "&start=" << start_ts;
        if (end_ts > 0) u << "&end=" << end_ts;
        const auto r = http_.get(u.str());
        if (r.status < 200 || r.status >= 300) {
            throw std::runtime_error("Data API trades HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 300));
        }
        const auto root = json::parse(r.body);
        const json::array* arr = nullptr;
        if (root.is_array()) arr = &root.as_array();
        else if (root.is_object()) {
            auto it = root.as_object().find("data");
            if (it != root.as_object().end() && it->value().is_array()) arr = &it->value().as_array();
        }
        if (!arr) throw std::runtime_error("Unexpected Data API trades response");

        for (const auto& value : *arr) {
            if (!value.is_object()) continue;
            const auto& o = value.as_object();
            RecentTrade t;
            if (auto it = o.find("conditionId"); it != o.end()) t.condition_id = as_string(it->value());
            if (t.condition_id.empty()) t.condition_id = condition_id;
            if (auto it = o.find("asset"); it != o.end()) t.token_id = as_string(it->value());
            if (auto it = o.find("side"); it != o.end()) t.side = uppercase_ascii(as_string(it->value()));
            if (auto it = o.find("transactionHash"); it != o.end()) t.transaction_hash = as_string(it->value());
            if (auto it = o.find("price"); it != o.end()) t.price = as_double(it->value(), -1.0);
            if (auto it = o.find("size"); it != o.end()) t.size = as_double(it->value(), 0.0);
            if (auto it = o.find("timestamp"); it != o.end()) t.ts = parse_time_value(it->value());
            if (t.token_id.empty() || t.ts <= 0 || t.price <= 0.0 || t.price >= 1.0 || t.size <= 0.0) continue;
            t.id = trade_id(t);
            if (unique.insert(t.id).second) out.push_back(std::move(t));
        }
    }
    std::sort(out.begin(), out.end(), [](const RecentTrade& a, const RecentTrade& b) {
        if (a.ts != b.ts) return a.ts < b.ts;
        return a.id < b.id;
    });
    return out;
}

FeeDetails PolymarketApi::fetch_fee_details(const Market& market) const {
    if (market.fee_rate >= 0.0) {
        return FeeDetails{market.fee_rate, market.fee_exponent, market.fee_taker_only};
    }
    FeeDetails fd{0.07, 1.0, true};
    try {
        const auto r = http_.get(cfg_.clob_url + "/clob-markets/" + market.condition_id);
        if (r.status >= 200 && r.status < 300) {
            const auto root = json::parse(r.body);
            if (root.is_object()) {
                auto it = root.as_object().find("fd");
                if (it != root.as_object().end() && it->value().is_object()) {
                    const auto& f = it->value().as_object();
                    if (auto x = f.find("r"); x != f.end()) fd.rate = as_double(x->value(), 0.0);
                    if (auto x = f.find("e"); x != f.end()) fd.exponent = as_double(x->value(), 1.0);
                    if (auto x = f.find("to"); x != f.end()) fd.taker_only = as_bool(x->value(), true);
                    return fd;
                }
            }
        }
    } catch (...) {}
    try {
        const auto r = http_.get(cfg_.clob_url + "/fee-rate?token_id=" + market.yes_token);
        if (r.status >= 200 && r.status < 300) {
            const auto root = json::parse(r.body);
            if (root.is_object()) {
                auto it = root.as_object().find("base_fee");
                if (it != root.as_object().end()) fd.rate = as_double(it->value()) / 10000.0;
            }
        }
    } catch (...) {}
    return fd;
}

} // namespace pm
