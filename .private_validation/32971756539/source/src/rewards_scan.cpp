#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/http.hpp"
#include "pm/rewards.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

std::string as_string(const json::value& v) {
    if (v.is_string()) return std::string(v.as_string());
    if (v.is_int64()) return std::to_string(v.as_int64());
    if (v.is_uint64()) return std::to_string(v.as_uint64());
    return {};
}

double as_double(const json::value& v, double fallback = 0.0) {
    try {
        if (v.is_double()) return v.as_double();
        if (v.is_int64()) return static_cast<double>(v.as_int64());
        if (v.is_uint64()) return static_cast<double>(v.as_uint64());
        if (v.is_string()) return std::stod(std::string(v.as_string()));
    } catch (...) {
    }
    return fallback;
}

std::string get_string(const json::object& o, const char* key) {
    const auto it = o.find(key);
    return it == o.end() ? std::string{} : as_string(it->value());
}

double get_double(const json::object& o, const char* key, double fallback = 0.0) {
    const auto it = o.find(key);
    return it == o.end() ? fallback : as_double(it->value(), fallback);
}

std::string lowercase(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return s;
}

std::string url_encode(const std::string& value) {
    std::ostringstream out;
    out << std::uppercase << std::hex;
    for (const unsigned char c : value) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') {
            out << static_cast<char>(c);
        } else {
            out << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(c);
        }
    }
    return out.str();
}

std::string csv_escape(const std::string& value) {
    if (value.find_first_of(",\"\n\r") == std::string::npos) return value;
    std::string out = "\"";
    for (const char c : value) out += c == '"' ? "\"\"" : std::string(1, c);
    out += '"';
    return out;
}

struct RewardPool {
    std::string condition_id;
    double rewards_max_spread_cents = 0.0;
    double rewards_min_size = 0.0;
    double native_daily_rate = 0.0;
    double sponsored_daily_rate = 0.0;
    double total_daily_rate = 0.0;
};

struct RewardMarket {
    std::string condition_id;
    std::string market_id;
    std::string event_id;
    std::string slug;
    std::string question;
    std::string yes_token;
    std::string no_token;
    double volume24h = 0.0;
    double market_competitiveness = 0.0;
};

struct Candidate {
    RewardMarket market;
    RewardPool pool;
    double yes_best_bid = 0.0;
    double yes_best_ask = 0.0;
    double no_best_bid = 0.0;
    double no_best_ask = 0.0;
    double adjusted_mid_yes = 0.0;
    double adjusted_mid_no = 0.0;
    double yes_quote = 0.0;
    double no_quote = 0.0;
    double quote_shares = 0.0;
    double quote_capital = 0.0;
    double one_sided_inventory = 0.0;
    double locked_complete_set_edge = 0.0;
    double book_q_one = 0.0;
    double book_q_two = 0.0;
    double book_qmin = 0.0;
    double our_q_one = 0.0;
    double our_q_two = 0.0;
    double our_qmin = 0.0;
    double reward_share_upper = 0.0;
    double reward_share_before_haircut = 0.0;
    double reward_share_haircut = 0.0;
    double estimated_native_daily_value = 0.0;
    double capital_charge_daily = 0.0;
    double adverse_budget_daily = 0.0;
    double conservative_daily_score = 0.0;
    double reward_to_one_sided_inventory = 0.0;
};

std::unordered_map<std::string,RewardPool> fetch_reward_pools(
    const pm::HttpClient& http,
    const std::string& clob_url) {
    std::unordered_map<std::string,RewardPool> out;
    std::string cursor;
    std::unordered_set<std::string> seen_cursors;

    for (int page = 0; page < 100; ++page) {
        std::string url = clob_url + "/rewards/markets/current";
        if (!cursor.empty()) url += "?next_cursor=" + url_encode(cursor);
        const auto response = http.get(url);
        if (response.status < 200 || response.status >= 300) {
            throw std::runtime_error(
                "Rewards current HTTP " + std::to_string(response.status) + ": " + response.body.substr(0, 300));
        }
        const auto root = json::parse(response.body);
        if (!root.is_object()) throw std::runtime_error("Unexpected rewards current response");
        const auto& object = root.as_object();
        const auto data_it = object.find("data");
        if (data_it == object.end() || !data_it->value().is_array()) {
            throw std::runtime_error("Rewards current response has no data array");
        }

        for (const auto& value : data_it->value().as_array()) {
            if (!value.is_object()) continue;
            const auto& o = value.as_object();
            RewardPool pool;
            pool.condition_id = get_string(o, "condition_id");
            pool.rewards_max_spread_cents = get_double(o, "rewards_max_spread");
            pool.rewards_min_size = get_double(o, "rewards_min_size");
            pool.native_daily_rate = get_double(o, "native_daily_rate", -1.0);
            pool.sponsored_daily_rate = get_double(o, "sponsored_daily_rate", 0.0);
            pool.total_daily_rate = get_double(o, "total_daily_rate", -1.0);

            double config_sum = 0.0;
            const auto config_it = o.find("rewards_config");
            if (config_it != o.end() && config_it->value().is_array()) {
                for (const auto& item : config_it->value().as_array()) {
                    if (item.is_object()) config_sum += std::max(0.0, get_double(item.as_object(), "rate_per_day"));
                }
            }
            if (pool.native_daily_rate < 0.0) pool.native_daily_rate = config_sum;
            if (pool.total_daily_rate < 0.0) {
                pool.total_daily_rate = std::max(0.0, pool.native_daily_rate) +
                                        std::max(0.0, pool.sponsored_daily_rate);
            }
            if (!pool.condition_id.empty() && pool.rewards_max_spread_cents > 0.0 &&
                pool.rewards_min_size > 0.0 && pool.total_daily_rate > 0.0) {
                out[pool.condition_id] = std::move(pool);
            }
        }

        const auto cursor_it = object.find("next_cursor");
        const std::string next = cursor_it == object.end() ? std::string{} : as_string(cursor_it->value());
        if (next.empty() || next == "LTE=" || next == cursor || !seen_cursors.insert(next).second) break;
        cursor = next;
    }
    return out;
}

std::vector<RewardMarket> fetch_reward_markets(
    const pm::HttpClient& http,
    const std::string& clob_url,
    std::size_t market_limit) {
    std::vector<RewardMarket> out;
    out.reserve(market_limit);
    std::unordered_set<std::string> conditions;
    std::string cursor;
    std::unordered_set<std::string> seen_cursors;

    while (out.size() < market_limit) {
        std::ostringstream url;
        url << clob_url << "/rewards/markets/multi?limit=500";
        if (!cursor.empty()) url << "&next_cursor=" << url_encode(cursor);
        const auto response = http.get(url.str());
        if (response.status < 200 || response.status >= 300) {
            throw std::runtime_error(
                "Rewards markets HTTP " + std::to_string(response.status) + ": " + response.body.substr(0, 300));
        }
        const auto root = json::parse(response.body);
        if (!root.is_object()) throw std::runtime_error("Unexpected rewards markets response");
        const auto& object = root.as_object();
        const auto data_it = object.find("data");
        if (data_it == object.end() || !data_it->value().is_array()) {
            throw std::runtime_error("Rewards markets response has no data array");
        }

        for (const auto& value : data_it->value().as_array()) {
            if (!value.is_object()) continue;
            const auto& o = value.as_object();
            RewardMarket market;
            market.condition_id = get_string(o, "condition_id");
            market.market_id = get_string(o, "market_id");
            market.event_id = get_string(o, "event_id");
            market.slug = get_string(o, "market_slug");
            market.question = get_string(o, "question");
            market.volume24h = get_double(o, "volume_24hr");
            market.market_competitiveness = get_double(o, "market_competitiveness");

            const auto tokens_it = o.find("tokens");
            if (tokens_it != o.end() && tokens_it->value().is_array()) {
                for (const auto& token_value : tokens_it->value().as_array()) {
                    if (!token_value.is_object()) continue;
                    const auto& token = token_value.as_object();
                    const auto outcome = lowercase(get_string(token, "outcome"));
                    const auto token_id = get_string(token, "token_id");
                    if (outcome == "yes") market.yes_token = token_id;
                    else if (outcome == "no") market.no_token = token_id;
                }
            }
            if (!market.condition_id.empty() && !market.yes_token.empty() && !market.no_token.empty() &&
                conditions.insert(market.condition_id).second) {
                out.push_back(std::move(market));
                if (out.size() >= market_limit) break;
            }
        }

        const auto cursor_it = object.find("next_cursor");
        const std::string next = cursor_it == object.end() ? std::string{} : as_string(cursor_it->value());
        if (next.empty() || next == "LTE=" || next == cursor || !seen_cursors.insert(next).second) break;
        cursor = next;
    }
    return out;
}

std::optional<double> cutoff_price(
    const std::vector<pm::Level>& input,
    bool bids,
    double required_shares,
    double added_price = kNaN,
    double added_shares = 0.0) {
    std::vector<pm::Level> levels = input;
    if (std::isfinite(added_price) && added_price > 0.0 && added_price < 1.0 && added_shares > 0.0) {
        levels.push_back({added_price, added_shares});
    }
    levels.erase(std::remove_if(levels.begin(), levels.end(), [](const pm::Level& level) {
        return !std::isfinite(level.price) || level.price <= 0.0 || level.price >= 1.0 ||
               !std::isfinite(level.size) || level.size <= 0.0;
    }), levels.end());
    std::sort(levels.begin(), levels.end(), [bids](const pm::Level& a, const pm::Level& b) {
        return bids ? a.price > b.price : a.price < b.price;
    });
    double cumulative = 0.0;
    for (const auto& level : levels) {
        cumulative += level.size;
        if (cumulative + 1e-12 >= required_shares) return level.price;
    }
    return std::nullopt;
}

double score_levels(
    const std::vector<pm::Level>& levels,
    double adjusted_midpoint,
    double max_spread_price) {
    double score = 0.0;
    for (const auto& level : levels) {
        score += pm::reward_order_score(
            max_spread_price,
            std::abs(level.price - adjusted_midpoint),
            level.size);
    }
    return score;
}

std::optional<double> passive_bid(const pm::Book& book, int improve_ticks) {
    const double bid = book.best_bid();
    const double ask = book.best_ask();
    const double tick = std::max(1e-6, book.tick_size);
    if (!std::isfinite(bid) || !std::isfinite(ask) || bid <= 0.0 || ask <= bid + 0.5 * tick) return std::nullopt;
    const double desired = bid + std::max(0, improve_ticks) * tick;
    const double price = std::min(desired, ask - tick);
    if (!std::isfinite(price) || price <= 0.0 || price >= ask - 1e-12) return std::nullopt;
    return price;
}

std::optional<Candidate> evaluate_candidate(
    const RewardMarket& market,
    const RewardPool& pool,
    const pm::Book& yes,
    const pm::Book& no,
    double requested_shares,
    double max_notional,
    int improve_ticks,
    double competition_multiplier,
    double reward_haircut,
    double native_reward_unit_usd,
    double annual_capital_rate,
    double adverse_bps,
    double one_sided_fills_per_day) {
    const auto yes_quote_opt = passive_bid(yes, improve_ticks);
    const auto no_quote_opt = passive_bid(no, improve_ticks);
    if (!yes_quote_opt || !no_quote_opt) return std::nullopt;
    const double yes_quote = *yes_quote_opt;
    const double no_quote = *no_quote_opt;
    const double price_sum = yes_quote + no_quote;
    if (!std::isfinite(price_sum) || price_sum <= 0.0) return std::nullopt;

    const double minimum_shares = std::max({
        pool.rewards_min_size,
        yes.min_order_size,
        no.min_order_size
    });
    const double desired_shares = std::max(minimum_shares, requested_shares);
    const double capital_limited_shares = max_notional > 0.0 ? max_notional / price_sum : desired_shares;
    const double quote_shares = std::min(desired_shares, capital_limited_shares);
    if (!std::isfinite(quote_shares) || quote_shares + 1e-9 < minimum_shares) return std::nullopt;

    const auto yes_cut_bid = cutoff_price(yes.bids, true, pool.rewards_min_size, yes_quote, quote_shares);
    const auto yes_cut_ask = cutoff_price(yes.asks, false, pool.rewards_min_size);
    const auto no_cut_bid = cutoff_price(no.bids, true, pool.rewards_min_size, no_quote, quote_shares);
    const auto no_cut_ask = cutoff_price(no.asks, false, pool.rewards_min_size);
    if (!yes_cut_bid || !yes_cut_ask || !no_cut_bid || !no_cut_ask) return std::nullopt;

    const double midpoint_yes = 0.5 * (*yes_cut_bid + *yes_cut_ask);
    const double midpoint_no = 0.5 * (*no_cut_bid + *no_cut_ask);
    if (!std::isfinite(midpoint_yes) || !std::isfinite(midpoint_no) ||
        midpoint_yes <= 0.0 || midpoint_yes >= 1.0 || midpoint_no <= 0.0 || midpoint_no >= 1.0) {
        return std::nullopt;
    }

    const double max_spread_price = pm::reward_spread_price_from_cents(pool.rewards_max_spread_cents);
    if (max_spread_price <= 0.0) return std::nullopt;

    const double book_q_one = score_levels(yes.bids, midpoint_yes, max_spread_price) +
                              score_levels(no.asks, midpoint_no, max_spread_price);
    const double book_q_two = score_levels(yes.asks, midpoint_yes, max_spread_price) +
                              score_levels(no.bids, midpoint_no, max_spread_price);
    const double book_qmin = pm::reward_minimum_score(book_q_one, book_q_two, midpoint_yes);

    const double our_q_one = pm::reward_order_score(
        max_spread_price, std::abs(yes_quote - midpoint_yes), quote_shares);
    const double our_q_two = pm::reward_order_score(
        max_spread_price, std::abs(no_quote - midpoint_no), quote_shares);
    const double our_qmin = pm::reward_minimum_score(our_q_one, our_q_two, midpoint_yes);
    if (our_qmin <= 1e-12) return std::nullopt;

    const double share_upper = pm::estimated_reward_share(our_qmin, book_qmin, 1.0);
    const double share_before_haircut = pm::estimated_reward_share(
        our_qmin, book_qmin, competition_multiplier);
    const double conservative_share = std::clamp(share_before_haircut * reward_haircut, 0.0, 1.0);
    const double native_daily_value = std::max(0.0, pool.native_daily_rate) *
                                      std::max(0.0, native_reward_unit_usd) * conservative_share;
    const double quote_capital = quote_shares * price_sum;
    const double one_sided_inventory = quote_shares * std::max(yes_quote, no_quote);
    const double capital_charge = quote_capital * std::max(0.0, annual_capital_rate) / 365.0;
    const double adverse_budget = one_sided_inventory * std::max(0.0, adverse_bps) / 10000.0 *
                                  std::max(0.0, one_sided_fills_per_day);
    const double score = native_daily_value - capital_charge - adverse_budget;

    Candidate candidate;
    candidate.market = market;
    candidate.pool = pool;
    candidate.yes_best_bid = yes.best_bid();
    candidate.yes_best_ask = yes.best_ask();
    candidate.no_best_bid = no.best_bid();
    candidate.no_best_ask = no.best_ask();
    candidate.adjusted_mid_yes = midpoint_yes;
    candidate.adjusted_mid_no = midpoint_no;
    candidate.yes_quote = yes_quote;
    candidate.no_quote = no_quote;
    candidate.quote_shares = quote_shares;
    candidate.quote_capital = quote_capital;
    candidate.one_sided_inventory = one_sided_inventory;
    candidate.locked_complete_set_edge = 1.0 - price_sum;
    candidate.book_q_one = book_q_one;
    candidate.book_q_two = book_q_two;
    candidate.book_qmin = book_qmin;
    candidate.our_q_one = our_q_one;
    candidate.our_q_two = our_q_two;
    candidate.our_qmin = our_qmin;
    candidate.reward_share_upper = share_upper;
    candidate.reward_share_before_haircut = share_before_haircut;
    candidate.reward_share_haircut = conservative_share;
    candidate.estimated_native_daily_value = native_daily_value;
    candidate.capital_charge_daily = capital_charge;
    candidate.adverse_budget_daily = adverse_budget;
    candidate.conservative_daily_score = score;
    candidate.reward_to_one_sided_inventory = one_sided_inventory > 1e-12
        ? native_daily_value / one_sided_inventory
        : 0.0;
    return candidate;
}

const char* csv_header() {
    return "market_id,condition_id,event_id,slug,question,native_daily_rate,sponsored_daily_rate,total_daily_rate,"
           "rewards_min_size,rewards_max_spread_cents,volume24h,market_competitiveness,yes_best_bid,yes_best_ask,"
           "no_best_bid,no_best_ask,adjusted_mid_yes,adjusted_mid_no,yes_quote,no_quote,quote_shares,quote_capital,"
           "one_sided_inventory,locked_complete_set_edge,book_q_one,book_q_two,book_qmin,our_q_one,our_q_two,our_qmin,"
           "reward_share_upper,reward_share_before_haircut,reward_share_haircut,estimated_native_daily_value,"
           "capital_charge_daily,adverse_budget_daily,conservative_daily_score,reward_to_one_sided_inventory";
}

void write_candidate(std::ostream& out, const Candidate& c) {
    out << csv_escape(c.market.market_id) << ','
        << csv_escape(c.market.condition_id) << ','
        << csv_escape(c.market.event_id) << ','
        << csv_escape(c.market.slug) << ','
        << csv_escape(c.market.question) << ','
        << std::setprecision(12)
        << c.pool.native_daily_rate << ','
        << c.pool.sponsored_daily_rate << ','
        << c.pool.total_daily_rate << ','
        << c.pool.rewards_min_size << ','
        << c.pool.rewards_max_spread_cents << ','
        << c.market.volume24h << ','
        << c.market.market_competitiveness << ','
        << c.yes_best_bid << ',' << c.yes_best_ask << ','
        << c.no_best_bid << ',' << c.no_best_ask << ','
        << c.adjusted_mid_yes << ',' << c.adjusted_mid_no << ','
        << c.yes_quote << ',' << c.no_quote << ','
        << c.quote_shares << ',' << c.quote_capital << ','
        << c.one_sided_inventory << ',' << c.locked_complete_set_edge << ','
        << c.book_q_one << ',' << c.book_q_two << ',' << c.book_qmin << ','
        << c.our_q_one << ',' << c.our_q_two << ',' << c.our_qmin << ','
        << c.reward_share_upper << ',' << c.reward_share_before_haircut << ',' << c.reward_share_haircut << ','
        << c.estimated_native_daily_value << ',' << c.capital_charge_daily << ',' << c.adverse_budget_daily << ','
        << c.conservative_daily_score << ',' << c.reward_to_one_sided_inventory << '\n';
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config_path = "config/paper_v4.json";
        std::string csv_path;
        std::size_t market_limit = 2000;
        std::size_t top = 50;
        double quote_shares = 50.0;
        double max_notional = 100.0;
        double competition_multiplier = 2.0;
        double reward_haircut = 0.25;
        double native_reward_unit_usd = 1.0;
        double annual_capital_rate = 0.20;
        double adverse_bps = 50.0;
        double one_sided_fills_per_day = 1.0;
        int improve_ticks = 0;

        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + arg);
                return std::string(argv[++i]);
            };
            if (arg == "--config") config_path = next();
            else if (arg == "--csv") csv_path = next();
            else if (arg == "--markets") market_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (arg == "--top") top = static_cast<std::size_t>(std::stoull(next()));
            else if (arg == "--quote-shares") quote_shares = std::stod(next());
            else if (arg == "--max-notional") max_notional = std::stod(next());
            else if (arg == "--competition-multiplier") competition_multiplier = std::stod(next());
            else if (arg == "--reward-haircut") reward_haircut = std::stod(next());
            else if (arg == "--native-reward-unit-usd") native_reward_unit_usd = std::stod(next());
            else if (arg == "--annual-capital-rate") annual_capital_rate = std::stod(next());
            else if (arg == "--adverse-bps") adverse_bps = std::stod(next());
            else if (arg == "--one-sided-fills-per-day") one_sided_fills_per_day = std::stod(next());
            else if (arg == "--improve-ticks") improve_ticks = std::stoi(next());
            else if (arg == "--help" || arg == "-h") {
                std::cout
                    << "polymarket_rewards_scan [--config FILE] [--markets N] [--top N] [--csv FILE] "
                       "[--quote-shares X] [--max-notional X] [--competition-multiplier X] "
                       "[--reward-haircut X] [--native-reward-unit-usd X] [--annual-capital-rate X] "
                       "[--adverse-bps X] [--one-sided-fills-per-day X] [--improve-ticks N]\n";
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + arg);
            }
        }

        if (market_limit == 0 || top == 0 || quote_shares <= 0.0 || max_notional <= 0.0 ||
            competition_multiplier < 1.0 || reward_haircut < 0.0 || reward_haircut > 1.0 ||
            native_reward_unit_usd < 0.0 || annual_capital_rate < 0.0 || adverse_bps < 0.0 ||
            one_sided_fills_per_day < 0.0 || improve_ticks < 0) {
            throw std::runtime_error("Invalid rewards scanner arguments");
        }

        const auto config = pm::Engine::load_config(config_path);
        pm::HttpClient http;
        pm::PolymarketApi api(config);
        const auto pools = fetch_reward_pools(http, config.clob_url);
        const auto markets = fetch_reward_markets(http, config.clob_url, market_limit);

        std::vector<std::string> tokens;
        tokens.reserve(2 * markets.size());
        std::unordered_set<std::string> unique_tokens;
        std::size_t matched = 0;
        for (const auto& market : markets) {
            if (!pools.count(market.condition_id)) continue;
            ++matched;
            if (unique_tokens.insert(market.yes_token).second) tokens.push_back(market.yes_token);
            if (unique_tokens.insert(market.no_token).second) tokens.push_back(market.no_token);
        }
        const auto books = tokens.empty()
            ? std::unordered_map<std::string,pm::Book>{}
            : api.fetch_books(tokens);

        std::vector<Candidate> candidates;
        candidates.reserve(matched);
        for (const auto& market : markets) {
            const auto pool_it = pools.find(market.condition_id);
            const auto yes_it = books.find(market.yes_token);
            const auto no_it = books.find(market.no_token);
            if (pool_it == pools.end() || yes_it == books.end() || no_it == books.end()) continue;
            auto candidate = evaluate_candidate(
                market,
                pool_it->second,
                yes_it->second,
                no_it->second,
                quote_shares,
                max_notional,
                improve_ticks,
                competition_multiplier,
                reward_haircut,
                native_reward_unit_usd,
                annual_capital_rate,
                adverse_bps,
                one_sided_fills_per_day);
            if (candidate) candidates.push_back(std::move(*candidate));
        }

        std::sort(candidates.begin(), candidates.end(), [](const Candidate& a, const Candidate& b) {
            if (a.conservative_daily_score != b.conservative_daily_score) {
                return a.conservative_daily_score > b.conservative_daily_score;
            }
            if (a.estimated_native_daily_value != b.estimated_native_daily_value) {
                return a.estimated_native_daily_value > b.estimated_native_daily_value;
            }
            return a.market.condition_id < b.market.condition_id;
        });

        const auto positive = static_cast<std::size_t>(std::count_if(
            candidates.begin(), candidates.end(), [](const Candidate& candidate) {
                return candidate.conservative_daily_score > 0.0;
            }));
        double native_pool = 0.0;
        for (const auto& [condition, pool] : pools) {
            (void)condition;
            native_pool += std::max(0.0, pool.native_daily_rate) * native_reward_unit_usd;
        }
        const double best_score = candidates.empty() ? 0.0 : candidates.front().conservative_daily_score;
        std::cout << "rewards_scan pools=" << pools.size()
                  << " reward_markets=" << markets.size()
                  << " matched=" << matched
                  << " books=" << books.size()
                  << " candidates=" << candidates.size()
                  << " positive=" << positive
                  << " native_pool_value_assumption=" << native_pool
                  << " reward_haircut=" << reward_haircut
                  << " competition_multiplier=" << competition_multiplier
                  << " best_conservative_daily_score=" << best_score << '\n';
        std::cout << csv_header() << '\n';
        for (std::size_t i = 0; i < std::min(top, candidates.size()); ++i) write_candidate(std::cout, candidates[i]);

        if (!csv_path.empty()) {
            const fs::path path(csv_path);
            if (path.has_parent_path()) fs::create_directories(path.parent_path());
            const fs::path temporary = path.string() + ".tmp";
            {
                std::ofstream out(temporary, std::ios::trunc);
                if (!out) throw std::runtime_error("Cannot write rewards CSV: " + temporary.string());
                out << csv_header() << '\n';
                for (const auto& candidate : candidates) write_candidate(out, candidate);
                out.flush();
                if (!out) throw std::runtime_error("Failed writing rewards CSV: " + temporary.string());
            }
            std::error_code error;
            fs::rename(temporary, path, error);
            if (error) {
                std::error_code ignored;
                fs::remove(path, ignored);
                error.clear();
                fs::rename(temporary, path, error);
                if (error) throw std::runtime_error("Cannot replace rewards CSV: " + path.string());
            }
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "fatal: " << error.what() << '\n';
        return 1;
    }
}
