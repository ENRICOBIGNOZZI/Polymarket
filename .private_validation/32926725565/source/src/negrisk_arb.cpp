#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/http.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {
namespace json = boost::json;

struct Opportunity {
    std::string type;
    std::string event_id;
    std::string anchor;
    std::size_t legs = 0;
    double raw_edge = -1.0;
    double net_edge_pre_gas = -1.0;
    double executable_shares = 0.0;
    double estimated_profit_pre_gas = 0.0;
};

bool json_bool(const json::value& v, bool fallback = false) {
    if (v.is_bool()) return v.as_bool();
    if (v.is_int64()) return v.as_int64() != 0;
    if (v.is_uint64()) return v.as_uint64() != 0;
    if (v.is_string()) {
        std::string s(v.as_string());
        std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        if (s == "true" || s == "1") return true;
        if (s == "false" || s == "0") return false;
    }
    return fallback;
}

std::optional<bool> is_augmented_event(const pm::Config& cfg, const pm::HttpClient& http,
                                       const std::string& event_id) {
    try {
        const auto r = http.get(cfg.gamma_url + "/events/" + event_id);
        if (r.status < 200 || r.status >= 300) return std::nullopt;
        const auto root = json::parse(r.body);
        if (!root.is_object()) return std::nullopt;
        const auto& o = root.as_object();
        if (auto it = o.find("negRiskAugmented"); it != o.end()) {
            return json_bool(it->value(), false);
        }
        if (auto it = o.find("neg_risk_augmented"); it != o.end()) {
            return json_bool(it->value(), false);
        }
        return false;
    } catch (...) {
        return std::nullopt;
    }
}

double touch_size(const pm::Book& book, bool buy) {
    const double px = buy ? book.best_ask() : book.best_bid();
    if (!std::isfinite(px)) return 0.0;
    const auto& levels = buy ? book.asks : book.bids;
    const double tol = std::max(1e-10, book.tick_size * 1e-6);
    double q = 0.0;
    for (const auto& level : levels) {
        if (std::abs(level.price - px) <= tol) q += std::max(0.0, level.size);
    }
    return q;
}

double fee_per_share(double price, const pm::FeeDetails& fd) {
    if (price <= 0.0 || price >= 1.0 || fd.rate <= 0.0) return 0.0;
    return fd.rate * std::pow(price * (1.0 - price), std::max(0.0, fd.exponent));
}

pm::FeeDetails fee_for(const pm::Market& m, const pm::PolymarketApi& api,
                       std::unordered_map<std::string, pm::FeeDetails>& cache) {
    auto it = cache.find(m.condition_id);
    if (it != cache.end()) return it->second;
    pm::FeeDetails fd;
    if (m.fee_rate >= 0.0) {
        fd = {m.fee_rate, m.fee_exponent, m.fee_taker_only};
    } else {
        fd = api.fetch_fee_details(m);
    }
    cache[m.condition_id] = fd;
    return fd;
}

std::string short_name(const pm::Market& m) {
    if (!m.slug.empty()) return m.slug;
    if (!m.question.empty()) return m.question;
    return m.id;
}

void usage() {
    std::cout
        << "polymarket_negrisk_arb --config FILE [--markets N] [--min-liquidity X] "
           "[--top N] [--include-augmented]\n";
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config_path = "config/paper.example.json";
        std::optional<std::size_t> market_limit;
        std::optional<double> min_liquidity;
        std::size_t top_n = 30;
        bool include_augmented = false;

        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a);
                return std::string(argv[++i]);
            };
            if (a == "--config") config_path = next();
            else if (a == "--markets") market_limit = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--min-liquidity") min_liquidity = std::stod(next());
            else if (a == "--top") top_n = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--include-augmented") include_augmented = true;
            else if (a == "--help" || a == "-h") {
                usage();
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + a);
            }
        }

        auto cfg = pm::Engine::load_config(config_path);
        if (market_limit) cfg.market_limit = *market_limit;
        if (min_liquidity) cfg.min_liquidity = *min_liquidity;

        pm::PolymarketApi api(cfg);
        pm::HttpClient http;
        auto discovered = api.discover_markets(cfg.market_limit, cfg.min_liquidity);

        std::unordered_set<std::string> event_ids;
        for (const auto& m : discovered) {
            if (m.neg_risk && !m.event_id.empty()) event_ids.insert(m.event_id);
        }

        std::vector<Opportunity> opportunities;
        std::unordered_map<std::string, pm::FeeDetails> fee_cache;
        std::size_t scanned_events = 0;
        std::size_t skipped_augmented = 0;
        std::size_t skipped_metadata = 0;
        std::size_t skipped_incomplete = 0;
        std::size_t raw_positive = 0;
        std::size_t net_positive = 0;

        const double slip = std::max(0.0, cfg.slippage_bps) / 10000.0;

        for (const auto& event_id : event_ids) {
            const auto augmented = is_augmented_event(cfg, http, event_id);
            if (!augmented.has_value()) {
                ++skipped_metadata;
                continue;
            }
            if (*augmented && !include_augmented) {
                ++skipped_augmented;
                continue;
            }

            std::vector<pm::Market> markets;
            try {
                markets = api.fetch_event_markets(event_id);
            } catch (const std::exception&) {
                ++skipped_incomplete;
                continue;
            }
            if (markets.size() < 2) {
                ++skipped_incomplete;
                continue;
            }

            bool valid = true;
            std::vector<std::string> tokens;
            tokens.reserve(markets.size() * 2);
            for (const auto& m : markets) {
                if (!m.active || m.closed || !m.enable_order_book || !m.accepting_orders ||
                    m.yes_token.empty() || m.no_token.empty() || m.question.empty()) {
                    valid = false;
                    break;
                }
                tokens.push_back(m.yes_token);
                tokens.push_back(m.no_token);
            }
            if (!valid) {
                ++skipped_incomplete;
                continue;
            }

            std::unordered_map<std::string, pm::Book> books;
            try {
                books = api.fetch_books(tokens);
            } catch (const std::exception&) {
                ++skipped_incomplete;
                continue;
            }

            for (const auto& m : markets) {
                const auto yi = books.find(m.yes_token);
                const auto ni = books.find(m.no_token);
                if (yi == books.end() || ni == books.end() ||
                    !std::isfinite(yi->second.best_ask()) || !std::isfinite(yi->second.best_bid()) ||
                    !std::isfinite(ni->second.best_ask()) || !std::isfinite(ni->second.best_bid())) {
                    valid = false;
                    break;
                }
            }
            if (!valid) {
                ++skipped_incomplete;
                continue;
            }
            ++scanned_events;

            // Structure 1: buy one YES in every mutually-exclusive outcome. For a
            // non-augmented complete NegRisk event the basket pays $1 at resolution.
            {
                double raw_cost = 0.0;
                double net_cost = 0.0;
                double q_touch = std::numeric_limits<double>::infinity();
                double q_min = 0.0;
                bool basket_ok = true;
                for (const auto& m : markets) {
                    const auto& b = books.at(m.yes_token);
                    const double ask = b.best_ask();
                    if (!std::isfinite(ask)) {
                        basket_ok = false;
                        break;
                    }
                    const double px = std::min(0.999999, ask * (1.0 + slip));
                    const auto fd = fee_for(m, api, fee_cache);
                    raw_cost += ask;
                    net_cost += px + fee_per_share(px, fd);
                    q_touch = std::min(q_touch, touch_size(b, true));
                    q_min = std::max(q_min, b.min_order_size);
                }
                if (basket_ok && std::isfinite(q_touch) && q_touch > 0.0) {
                    const double raw_edge = 1.0 - raw_cost;
                    const double net_edge = 1.0 - net_cost;
                    const double capital_per_share = std::max(1e-9, net_cost);
                    const double q_cap = std::min(q_touch, cfg.max_trade_usd / capital_per_share);
                    const double executable = q_cap + 1e-12 >= q_min ? q_cap : 0.0;
                    Opportunity o{"BUY_ALL_YES", event_id, short_name(markets.front()), markets.size(),
                                  raw_edge, net_edge, executable, executable * net_edge};
                    opportunities.push_back(o);
                    if (raw_edge > 0.0) ++raw_positive;
                    if (net_edge > 0.0 && executable > 0.0) ++net_positive;
                }
            }

            // Structure 2: documented NegRisk conversion. Buy NO_i, convert it into
            // one YES share of every other outcome, then sell those YES shares.
            // Gas/conversion transaction cost is intentionally NOT guessed here;
            // net_edge_pre_gas contains only displayed-book taker fees and slippage.
            for (std::size_t i = 0; i < markets.size(); ++i) {
                const auto& source = markets[i];
                const auto& no_book = books.at(source.no_token);
                const double no_ask = no_book.best_ask();
                if (!std::isfinite(no_ask)) continue;

                double raw_receive = 0.0;
                double net_receive = 0.0;
                double q_touch = touch_size(no_book, true);
                double q_min = no_book.min_order_size;
                bool conversion_ok = q_touch > 0.0;

                const auto source_fd = fee_for(source, api, fee_cache);
                const double no_px = std::min(0.999999, no_ask * (1.0 + slip));
                const double net_pay = no_px + fee_per_share(no_px, source_fd);

                for (std::size_t j = 0; j < markets.size(); ++j) {
                    if (j == i) continue;
                    const auto& target = markets[j];
                    const auto& yes_book = books.at(target.yes_token);
                    const double bid = yes_book.best_bid();
                    const double depth = touch_size(yes_book, false);
                    if (!std::isfinite(bid) || depth <= 0.0) {
                        conversion_ok = false;
                        break;
                    }
                    const double px = std::max(1e-6, bid * (1.0 - slip));
                    const auto target_fd = fee_for(target, api, fee_cache);
                    raw_receive += bid;
                    net_receive += px - fee_per_share(px, target_fd);
                    q_touch = std::min(q_touch, depth);
                    q_min = std::max(q_min, yes_book.min_order_size);
                }
                if (!conversion_ok || !std::isfinite(q_touch) || q_touch <= 0.0) continue;

                const double raw_edge = raw_receive - no_ask;
                const double net_edge = net_receive - net_pay;
                const double capital_per_share = std::max(1e-9, net_pay);
                const double q_cap = std::min(q_touch, cfg.max_trade_usd / capital_per_share);
                const double executable = q_cap + 1e-12 >= q_min ? q_cap : 0.0;
                Opportunity o{"NO_TO_OTHER_YES", event_id, short_name(source), markets.size(),
                              raw_edge, net_edge, executable, executable * net_edge};
                opportunities.push_back(o);
                if (raw_edge > 0.0) ++raw_positive;
                if (net_edge > 0.0 && executable > 0.0) ++net_positive;
            }
        }

        std::sort(opportunities.begin(), opportunities.end(), [](const auto& a, const auto& b) {
            if (a.net_edge_pre_gas != b.net_edge_pre_gas) return a.net_edge_pre_gas > b.net_edge_pre_gas;
            return a.estimated_profit_pre_gas > b.estimated_profit_pre_gas;
        });

        std::cout << "discovered=" << discovered.size()
                  << " negRisk_event_ids=" << event_ids.size()
                  << " scanned_events=" << scanned_events
                  << " skipped_augmented=" << skipped_augmented
                  << " skipped_metadata=" << skipped_metadata
                  << " skipped_incomplete=" << skipped_incomplete
                  << " opportunities=" << opportunities.size()
                  << " raw_positive=" << raw_positive
                  << " net_positive_pre_gas=" << net_positive << '\n';

        std::cout << "type,event_id,anchor,legs,raw_edge,net_edge_pre_gas,executable_shares,estimated_profit_pre_gas\n";
        const std::size_t n = std::min(top_n, opportunities.size());
        for (std::size_t i = 0; i < n; ++i) {
            const auto& o = opportunities[i];
            std::cout << o.type << ',' << o.event_id << ',' << o.anchor << ',' << o.legs << ','
                      << std::setprecision(10) << o.raw_edge << ',' << o.net_edge_pre_gas << ','
                      << o.executable_shares << ',' << o.estimated_profit_pre_gas << '\n';
        }

        if (net_positive > 0) {
            std::cout << "NOTE: positive conversion edges are PRE-GAS and PRE-LATENCY diagnostics; "
                         "no order submission or on-chain conversion is performed.\n";
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
