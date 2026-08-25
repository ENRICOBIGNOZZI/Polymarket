#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/market_data.hpp"
#include "pm/types.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace {
namespace fs = std::filesystem;

std::int64_t now_s() {
    return std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::vector<std::string> split(const std::string& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string x;
    while (std::getline(ss, x, ',')) out.push_back(x);
    return out;
}

double touch_size(const pm::Book& b, bool bid_side) {
    const double px = bid_side ? b.best_bid() : b.best_ask();
    if (!std::isfinite(px)) return 0.0;
    double q = 0.0;
    const auto& levels = bid_side ? b.bids : b.asks;
    for (const auto& l : levels) {
        if (std::abs(l.price - px) <= 1e-9) q += l.size;
    }
    return q;
}

struct Order {
    std::string market_id;
    std::string event_id;
    std::string condition_id;
    std::string slug;
    std::string side;
    std::string token_id;
    double limit_price = 0.0;
    double shares = 0.0; // Remaining shares only.
    double queue_ahead = 0.0;
    std::int64_t created_ts = 0;
    std::int64_t game_start_ts = 0;
    bool timed_sports = false;
    std::int64_t last_trade_ts = 0;
    std::string last_trade_keys = "|";
    double fee_rate = 0.07;
    double fee_exponent = 1.0;
    bool fee_taker_only = true;
};

struct Position {
    std::string market_id;
    std::string event_id;
    std::string condition_id;
    std::string slug;
    std::string side;
    std::string token_id;
    double shares = 0.0;
    double entry_price = 0.0;
    std::int64_t entry_ts = 0;
    std::int64_t game_start_ts = 0;
    bool timed_sports = false;
    double fee_rate = 0.07;
    double fee_exponent = 1.0;
    bool fee_taker_only = true;
};

struct MicroSignal {
    double q_yes = 0.5;
    double confidence = 0.0;
};

MicroSignal micro_signal(const pm::Book& yes, const pm::Book& no) {
    const double mid = yes.midpoint();
    if (!std::isfinite(mid)) return {};
    const double y = yes.microprice();
    const double n = no.microprice();
    const double dy = yes.weighted_depth(true) + yes.weighted_depth(false);
    const double dn = no.weighted_depth(true) + no.weighted_depth(false);
    const double wy = std::sqrt(std::max(0.0, dy)) / (1.0 + 20.0 * yes.spread());
    const double wn = std::sqrt(std::max(0.0, dn)) / (1.0 + 20.0 * no.spread());
    double q = mid;
    if (std::isfinite(y) && std::isfinite(n) && wy + wn > 1e-12) {
        q = (wy * y + wn * (1.0 - n)) / (wy + wn);
    } else if (std::isfinite(y)) {
        q = y;
    }
    const double parity = std::isfinite(y) && std::isfinite(n) ? std::abs(y - (1.0 - n)) : 0.25;
    const double liq = (dy + dn) / (dy + dn + 200.0);
    const double spread_conf = std::exp(-5.0 * (yes.spread() + no.spread()));
    const double conf = std::clamp(liq * spread_conf * std::exp(-8.0 * parity), 0.02, 1.0);
    return {std::clamp(q, 0.001, 0.999), conf};
}

bool cursor_contains(const Order& o, const std::string& trade_id) {
    return o.last_trade_keys.find('|' + trade_id + '|') != std::string::npos;
}

void cursor_remember(Order& o, const pm::RecentTrade& trade) {
    if (trade.ts > o.last_trade_ts) {
        o.last_trade_ts = trade.ts;
        o.last_trade_keys = "|";
    }
    if (trade.ts == o.last_trade_ts && !cursor_contains(o, trade.id)) {
        o.last_trade_keys += trade.id + '|';
    }
}

pm::MarketTiming order_timing(const Order& o) {
    return pm::MarketTiming{o.timed_sports, o.game_start_ts, 0};
}

pm::MarketTiming position_timing(const Position& p) {
    return pm::MarketTiming{p.timed_sports, p.game_start_ts, 0};
}

class MakerPaper {
public:
    MakerPaper(pm::Config cfg, std::string run_dir, double min_edge, double max_order_usd,
               std::int64_t ttl, std::int64_t hold, double adverse_mult, double max_queue_ratio)
        : cfg_(std::move(cfg)), api_(cfg_), run_dir_(std::move(run_dir)), min_edge_(min_edge),
          max_order_usd_(max_order_usd), ttl_(ttl), hold_(hold), adverse_mult_(adverse_mult),
          max_queue_ratio_(max_queue_ratio), cash_(cfg_.starting_capital), peak_equity_(cfg_.starting_capital) {
        fs::create_directories(run_dir_);
        load_state();
        ensure_files();
    }

    void tick(std::size_t market_limit, double min_liquidity) {
        auto markets = api_.discover_markets(market_limit, min_liquidity);
        std::unordered_map<std::string,const pm::Market*> market_by_id;
        std::vector<std::string> tokens;
        tokens.reserve(markets.size() * 2 + orders_.size() + positions_.size());
        for (const auto& m : markets) {
            market_by_id[m.id] = &m;
            tokens.push_back(m.yes_token);
            tokens.push_back(m.no_token);
        }
        for (const auto& [id, o] : orders_) {
            (void)id;
            if (std::find(tokens.begin(), tokens.end(), o.token_id) == tokens.end()) tokens.push_back(o.token_id);
        }
        for (const auto& [id, p] : positions_) {
            (void)id;
            if (std::find(tokens.begin(), tokens.end(), p.token_id) == tokens.end()) tokens.push_back(p.token_id);
        }
        auto books = tokens.empty() ? std::unordered_map<std::string,pm::Book>{} : api_.fetch_books(tokens);
        const auto now = now_s();

        double eq = equity(books);
        peak_equity_ = std::max(peak_equity_, eq);
        if (peak_equity_ > 0.0 && 1.0 - eq / peak_equity_ >= cfg_.max_drawdown) killed_ = true;

        process_orders(books, now);
        process_positions(books, market_by_id, now);
        eq = equity(books);
        peak_equity_ = std::max(peak_equity_, eq);
        if (peak_equity_ > 0.0 && 1.0 - eq / peak_equity_ >= cfg_.max_drawdown) killed_ = true;

        std::size_t signals = 0, posted = 0, queue_rejected = 0;
        if (!killed_) {
            for (const auto& m : markets) {
                if (positions_.count(m.id) || orders_.count(m.id)) continue;
                auto yi = books.find(m.yes_token), ni = books.find(m.no_token);
                if (yi == books.end() || ni == books.end()) continue;
                const auto& yb = yi->second;
                const auto& nb = ni->second;
                const double mid = yb.midpoint();
                if (!std::isfinite(mid) || mid <= cfg_.min_mid || mid >= cfg_.max_mid) continue;
                if (yb.spread() > cfg_.max_spread || nb.spread() > cfg_.max_spread) continue;
                const auto ms = micro_signal(yb, nb);
                if (ms.confidence < 0.10) continue;
                const auto fd = api_.fetch_fee_details(m);

                struct Choice {
                    std::string side;
                    const pm::Book* book = nullptr;
                    std::string token;
                    double fair = 0.5;
                    double net_expected_edge = -1.0;
                };
                std::vector<Choice> choices;
                auto consider = [&](const std::string& side, const pm::Book& b,
                                    const std::string& token, double fair) {
                    const double bid = b.best_bid(), ask = b.best_ask(), spread = b.spread();
                    if (!std::isfinite(bid) || !std::isfinite(ask) || !std::isfinite(spread) ||
                        bid <= 0.0 || ask <= bid) return;
                    const double adverse_buffer = adverse_mult_ * spread * (1.0 - ms.confidence);
                    const double future_bid = std::clamp(fair - 0.5 * spread, 0.001, 0.999) *
                                              (1.0 - cfg_.slippage_bps / 10000.0);
                    const double exit_fee_per_share = pm::Engine::protocol_fee(1.0, future_bid, fd);
                    const double edge = future_bid - exit_fee_per_share - bid - adverse_buffer;
                    if (edge > min_edge_) choices.push_back({side, &b, token, fair, edge});
                };
                consider("YES", yb, m.yes_token, ms.q_yes);
                consider("NO", nb, m.no_token, 1.0 - ms.q_yes);
                if (choices.empty()) continue;
                ++signals;
                const auto c = *std::max_element(choices.begin(), choices.end(), [](const auto& a, const auto& b) {
                    return a.net_expected_edge < b.net_expected_edge;
                });

                const pm::MarketTiming timing{m.timed_sports, m.game_start_ts, m.seconds_delay};
                if (!pm::pregame_market_eligible(timing, now)) continue;

                const double limit = c.book->best_bid();
                if (!std::isfinite(limit) || !std::isfinite(c.book->best_ask()) ||
                    limit >= c.book->best_ask() - 1e-12) continue;

                const double reserved = reserved_cash();
                const double pos_cost = position_cost();
                const double available_cash = std::max(0.0, cash_ - reserved);
                const double event_room = cfg_.max_event_fraction * eq - event_committed(m.event_id);
                const double gross_room = cfg_.max_gross_fraction * eq - pos_cost - reserved;
                const double current_drawdown_dollars = std::max(0.0, peak_equity_ - eq);
                const double loss_room = cfg_.max_drawdown * peak_equity_ - current_drawdown_dollars - pos_cost - reserved;
                const double max_cash = std::min({
                    max_order_usd_, available_cash, cfg_.max_market_fraction * eq,
                    std::max(0.0, event_room), std::max(0.0, gross_room), std::max(0.0, loss_room)
                });
                if (max_cash <= 0.0) continue;

                double shares = max_cash / limit;
                shares = std::min(shares,
                    std::max(c.book->min_order_size, 0.25 * std::max(1.0, touch_size(*c.book, true))));
                if (shares < c.book->min_order_size || shares * limit > available_cash + 1e-9) continue;

                const double queue_ahead = touch_size(*c.book, true);
                const double queue_ratio = queue_ahead / std::max({shares, c.book->min_order_size, 1e-9});
                if (max_queue_ratio_ > 0.0 && queue_ratio > max_queue_ratio_) {
                    ++queue_rejected;
                    continue;
                }

                Order o;
                o.market_id = m.id;
                o.event_id = m.event_id;
                o.condition_id = m.condition_id;
                o.slug = m.slug;
                o.side = c.side;
                o.token_id = c.token;
                o.limit_price = limit;
                o.shares = shares;
                o.queue_ahead = queue_ahead;
                o.created_ts = now;
                o.game_start_ts = timing.game_start_ts;
                o.timed_sports = timing.timed_sports;
                o.last_trade_ts = now;
                o.last_trade_keys = "|";
                o.fee_rate = fd.rate;
                o.fee_exponent = fd.exponent;
                o.fee_taker_only = fd.taker_only;
                orders_[m.id] = std::move(o);
                append_order("POST", orders_.at(m.id), c.net_expected_edge, ms.confidence);
                ++posted;
            }
        }

        eq = equity(books);
        peak_equity_ = std::max(peak_equity_, eq);
        persist();
        append_equity(now, eq);
        std::cout << "maker_tick markets=" << markets.size()
                  << " signals=" << signals
                  << " posted=" << posted
                  << " queue_rejected=" << queue_rejected
                  << " resting=" << orders_.size()
                  << " positions=" << positions_.size()
                  << " reserved=" << reserved_cash()
                  << " cash=" << cash_
                  << " equity=" << eq
                  << " drawdown=" << (peak_equity_ > 0.0 ? 1.0 - eq / peak_equity_ : 0.0)
                  << " killed=" << killed_ << '\n';
    }

private:
    pm::Config cfg_;
    pm::PolymarketApi api_;
    std::string run_dir_;
    double min_edge_ = 0.003;
    double max_order_usd_ = 75.0;
    std::int64_t ttl_ = 300;
    std::int64_t hold_ = 180;
    double adverse_mult_ = 0.50;
    double max_queue_ratio_ = 0.0;
    double cash_ = 0.0;
    double peak_equity_ = 0.0;
    bool killed_ = false;
    std::unordered_map<std::string,Order> orders_;
    std::unordered_map<std::string,Position> positions_;

    void ensure_files() {
        auto ensure = [&](const std::string& file, const std::string& header) {
            const auto p = fs::path(run_dir_) / file;
            if (!fs::exists(p) || fs::file_size(p) == 0) {
                std::ofstream f(p);
                f << header << '\n';
            }
        };
        ensure("maker_order_log.csv",
               "timestamp,action,market_id,slug,side,token_id,limit_price,remaining_shares,queue_ahead,signal_edge,confidence");
        ensure("maker_fills.csv", "timestamp,market_id,slug,action,side,shares,price,fee,reason");
        ensure("maker_equity.csv",
               "timestamp,cash,equity,reserved_cash,resting_orders,positions,peak_equity,drawdown,killed");
    }

    void load_state() {
        {
            std::ifstream f(fs::path(run_dir_) / "maker_risk.csv");
            std::string line;
            std::getline(f, line);
            if (std::getline(f, line)) {
                const auto x = split(line);
                try {
                    if (!x.empty()) cash_ = std::stod(x[0]);
                    if (x.size() >= 2) peak_equity_ = std::stod(x[1]);
                    if (x.size() >= 3) killed_ = std::stoi(x[2]) != 0;
                } catch (...) {}
            }
        }
        {
            std::ifstream f(fs::path(run_dir_) / "maker_orders.csv");
            std::string line;
            std::getline(f, line);
            while (std::getline(f, line)) {
                const auto x = split(line);
                // Old V3 orders are deliberately not restored: they lack a tape cursor and game clock.
                if (x.size() < 17) continue;
                try {
                    Order o;
                    o.market_id = x[0]; o.event_id = x[1]; o.condition_id = x[2]; o.slug = x[3];
                    o.side = x[4]; o.token_id = x[5]; o.limit_price = std::stod(x[6]);
                    o.shares = std::stod(x[7]); o.queue_ahead = std::stod(x[8]);
                    o.created_ts = std::stoll(x[9]); o.game_start_ts = std::stoll(x[10]);
                    o.timed_sports = std::stoi(x[11]) != 0; o.last_trade_ts = std::stoll(x[12]);
                    o.last_trade_keys = x[13].empty() ? "|" : x[13]; o.fee_rate = std::stod(x[14]);
                    o.fee_exponent = std::stod(x[15]); o.fee_taker_only = std::stoi(x[16]) != 0;
                    if (o.shares > 0.0 && !o.market_id.empty()) orders_[o.market_id] = std::move(o);
                } catch (...) {}
            }
        }
        {
            std::ifstream f(fs::path(run_dir_) / "maker_positions.csv");
            std::string line;
            std::getline(f, line);
            while (std::getline(f, line)) {
                const auto x = split(line);
                try {
                    Position p;
                    if (x.size() >= 14) {
                        p.market_id = x[0]; p.event_id = x[1]; p.condition_id = x[2]; p.slug = x[3];
                        p.side = x[4]; p.token_id = x[5]; p.shares = std::stod(x[6]);
                        p.entry_price = std::stod(x[7]); p.entry_ts = std::stoll(x[8]);
                        p.game_start_ts = std::stoll(x[9]); p.timed_sports = std::stoi(x[10]) != 0;
                        p.fee_rate = std::stod(x[11]); p.fee_exponent = std::stod(x[12]);
                        p.fee_taker_only = std::stoi(x[13]) != 0;
                    } else if (x.size() >= 8) {
                        p.market_id = x[0]; p.event_id = x[1]; p.slug = x[2]; p.side = x[3];
                        p.token_id = x[4]; p.shares = std::stod(x[5]);
                        p.entry_price = std::stod(x[6]); p.entry_ts = std::stoll(x[7]);
                    } else {
                        continue;
                    }
                    if (p.shares > 0.0 && !p.market_id.empty()) positions_[p.market_id] = std::move(p);
                } catch (...) {}
            }
        }
        if (peak_equity_ <= 0.0) peak_equity_ = std::max(cash_, cfg_.starting_capital);
    }

    void persist() const {
        {
            std::ofstream f(fs::path(run_dir_) / "maker_risk.csv");
            f << "cash,peak_equity,killed\n" << cash_ << ',' << peak_equity_ << ',' << (killed_ ? 1 : 0) << '\n';
        }
        {
            std::ofstream f(fs::path(run_dir_) / "maker_orders.csv");
            f << "market_id,event_id,condition_id,slug,side,token_id,limit_price,remaining_shares,queue_ahead,"
                 "created_ts,game_start_ts,timed_sports,last_trade_ts,last_trade_keys,fee_rate,fee_exponent,fee_taker_only\n";
            for (const auto& [id, o] : orders_) {
                (void)id;
                f << o.market_id << ',' << o.event_id << ',' << o.condition_id << ',' << o.slug << ',' << o.side << ','
                  << o.token_id << ',' << o.limit_price << ',' << o.shares << ',' << o.queue_ahead << ','
                  << o.created_ts << ',' << o.game_start_ts << ',' << (o.timed_sports ? 1 : 0) << ','
                  << o.last_trade_ts << ',' << o.last_trade_keys << ',' << o.fee_rate << ',' << o.fee_exponent << ','
                  << (o.fee_taker_only ? 1 : 0) << '\n';
            }
        }
        {
            std::ofstream f(fs::path(run_dir_) / "maker_positions.csv");
            f << "market_id,event_id,condition_id,slug,side,token_id,shares,entry_price,entry_ts,game_start_ts,"
                 "timed_sports,fee_rate,fee_exponent,fee_taker_only\n";
            for (const auto& [id, p] : positions_) {
                (void)id;
                f << p.market_id << ',' << p.event_id << ',' << p.condition_id << ',' << p.slug << ',' << p.side << ','
                  << p.token_id << ',' << p.shares << ',' << p.entry_price << ',' << p.entry_ts << ','
                  << p.game_start_ts << ',' << (p.timed_sports ? 1 : 0) << ',' << p.fee_rate << ','
                  << p.fee_exponent << ',' << (p.fee_taker_only ? 1 : 0) << '\n';
            }
        }
    }

    void append_order(const std::string& action, const Order& o, double edge, double confidence) const {
        std::ofstream f(fs::path(run_dir_) / "maker_order_log.csv", std::ios::app);
        f << now_s() << ',' << action << ',' << o.market_id << ',' << o.slug << ',' << o.side << ',' << o.token_id << ','
          << o.limit_price << ',' << o.shares << ',' << o.queue_ahead << ',' << edge << ',' << confidence << '\n';
    }

    void append_fill(const Position& p, const std::string& action, double shares,
                     double px, double fee, const std::string& reason) const {
        std::ofstream f(fs::path(run_dir_) / "maker_fills.csv", std::ios::app);
        f << now_s() << ',' << p.market_id << ',' << p.slug << ',' << action << ',' << p.side << ',' << shares << ','
          << px << ',' << fee << ',' << reason << '\n';
    }

    void append_equity(std::int64_t ts, double eq) const {
        std::ofstream f(fs::path(run_dir_) / "maker_equity.csv", std::ios::app);
        f << ts << ',' << cash_ << ',' << eq << ',' << reserved_cash() << ',' << orders_.size() << ','
          << positions_.size() << ',' << peak_equity_ << ','
          << (peak_equity_ > 0.0 ? 1.0 - eq / peak_equity_ : 0.0) << ',' << (killed_ ? 1 : 0) << '\n';
    }

    double reserved_cash() const {
        double x = 0.0;
        for (const auto& [id, o] : orders_) {
            (void)id;
            x += std::max(0.0, o.shares * o.limit_price);
        }
        return x;
    }

    double position_cost() const {
        double x = 0.0;
        for (const auto& [id, p] : positions_) {
            (void)id;
            x += std::max(0.0, p.shares * p.entry_price);
        }
        return x;
    }

    double event_committed(const std::string& event_id) const {
        double x = 0.0;
        for (const auto& [id, o] : orders_) {
            (void)id;
            if (o.event_id == event_id) x += o.shares * o.limit_price;
        }
        for (const auto& [id, p] : positions_) {
            (void)id;
            if (p.event_id == event_id) x += p.shares * p.entry_price;
        }
        return x;
    }

    void add_position_fill(const Order& o, double filled_shares, std::int64_t now) {
        auto it = positions_.find(o.market_id);
        if (it == positions_.end()) {
            Position p;
            p.market_id = o.market_id; p.event_id = o.event_id; p.condition_id = o.condition_id;
            p.slug = o.slug; p.side = o.side; p.token_id = o.token_id;
            p.shares = filled_shares; p.entry_price = o.limit_price; p.entry_ts = now;
            p.game_start_ts = o.game_start_ts; p.timed_sports = o.timed_sports;
            p.fee_rate = o.fee_rate; p.fee_exponent = o.fee_exponent; p.fee_taker_only = o.fee_taker_only;
            positions_[o.market_id] = std::move(p);
            return;
        }
        auto& p = it->second;
        const double new_shares = p.shares + filled_shares;
        if (new_shares <= 0.0) return;
        p.entry_price = (p.shares * p.entry_price + filled_shares * o.limit_price) / new_shares;
        p.shares = new_shares;
        p.entry_ts = std::min(p.entry_ts, now);
    }

    void process_orders(const std::unordered_map<std::string,pm::Book>& books, std::int64_t now) {
        std::vector<std::string> erase;
        for (auto& [id, o] : orders_) {
            if (killed_) {
                append_order("CANCEL_KILL", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            if (pm::timed_sports_started(order_timing(o), now)) {
                append_order("CANCEL_GAME_START", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            if (now - o.created_ts >= ttl_) {
                append_order("CANCEL_TTL", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            if (o.condition_id.empty()) {
                append_order("CANCEL_LEGACY_STATE", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }

            std::vector<pm::RecentTrade> trades;
            try {
                trades = api_.fetch_recent_trades(
                    {o.condition_id}, std::max(o.created_ts, o.last_trade_ts), now, 1000);
            } catch (const std::exception& e) {
                // Tape failure is fail-closed: the order remains resting but cannot fill.
                std::cerr << "maker_trade_tape_error market=" << o.market_id << " error=" << e.what() << '\n';
            }

            bool completed = false;
            for (const auto& trade : trades) {
                if (trade.ts < o.last_trade_ts) continue;
                if (trade.ts == o.last_trade_ts && cursor_contains(o, trade.id)) continue;
                cursor_remember(o, trade);
                if (trade.token_id != o.token_id) continue;

                auto consumed = pm::consume_passive_bid_trade(
                    o.queue_ahead, o.shares, o.limit_price,
                    books.count(o.token_id) ? books.at(o.token_id).tick_size : 0.01,
                    o.created_ts, trade);
                if (!consumed.eligible_trade) continue;
                const double old_queue = o.queue_ahead;
                o.queue_ahead = consumed.queue_ahead;

                if (consumed.filled_shares <= 0.0) {
                    if (o.queue_ahead + 1e-12 < old_queue) append_order("QUEUE_DEPLETION", o, 0.0, 0.0);
                    continue;
                }

                const double other_reserved = std::max(0.0, reserved_cash() - o.shares * o.limit_price);
                const double fill_cost = consumed.filled_shares * o.limit_price;
                if (fill_cost + other_reserved > cash_ + 1e-9) {
                    append_order("CANCEL_CAPITAL", o, 0.0, 0.0);
                    erase.push_back(id);
                    completed = true;
                    break;
                }

                cash_ -= fill_cost;
                o.shares = consumed.remaining_shares;
                add_position_fill(o, consumed.filled_shares, now);
                const auto& p = positions_.at(id);
                append_fill(p, o.shares <= 1e-12 ? "BUY_MAKER" : "BUY_MAKER_PARTIAL",
                            consumed.filled_shares, o.limit_price, 0.0, "taker_sell_consumed_queue");
                append_order(o.shares <= 1e-12 ? "FILL" : "PARTIAL_FILL", o, 0.0, 0.0);
                if (o.shares <= 1e-12) {
                    erase.push_back(id);
                    completed = true;
                    break;
                }
            }
            if (completed) continue;

            auto bit = books.find(o.token_id);
            if (bit == books.end()) continue;
            const double bb = bit->second.best_bid();
            if (std::isfinite(bb) && bb > o.limit_price + 0.5 * std::max(1e-6, bit->second.tick_size)) {
                append_order("CANCEL_STALE", o, 0.0, 0.0);
                erase.push_back(id);
            }
        }
        for (const auto& id : erase) orders_.erase(id);
    }

    void process_positions(const std::unordered_map<std::string,pm::Book>& books,
                           const std::unordered_map<std::string,const pm::Market*>& market_by_id,
                           std::int64_t now) {
        std::vector<std::string> erase;
        for (auto& [id, p] : positions_) {
            // Do not turn a partially filled entry into an unintended churn loop while the residual rests.
            if (orders_.count(id)) continue;
            auto bit = books.find(p.token_id);
            if (bit == books.end()) continue;

            const bool game_start = pm::timed_sports_started(position_timing(p), now);
            bool exit = killed_ || game_start || now - p.entry_ts >= hold_;
            auto mit = market_by_id.find(id);
            if (mit != market_by_id.end()) {
                const auto* m = mit->second;
                auto yi = books.find(m->yes_token), ni = books.find(m->no_token);
                if (yi != books.end() && ni != books.end()) {
                    const auto ms = micro_signal(yi->second, ni->second);
                    const double fair = p.side == "YES" ? ms.q_yes : 1.0 - ms.q_yes;
                    const double bid = bit->second.best_bid();
                    if (std::isfinite(bid) && fair <= bid + 0.25 * bit->second.spread()) exit = true;
                }
            }
            if (!exit) continue;

            const double best_bid = bit->second.best_bid();
            if (!std::isfinite(best_bid)) continue;
            const auto walked = pm::Engine::walk_book(bit->second, false, p.shares * std::max(1e-6, best_bid));
            if (!walked || walked->first <= 1e-12) continue;
            const double shares = std::min(p.shares, walked->first);
            const double px = walked->second * (1.0 - cfg_.slippage_bps / 10000.0);
            pm::FeeDetails fd{p.fee_rate, p.fee_exponent, p.fee_taker_only};
            if (mit != market_by_id.end()) fd = api_.fetch_fee_details(*mit->second);
            const double fee = pm::Engine::protocol_fee(shares, px, fd);
            cash_ += shares * px - fee;
            p.shares = std::max(0.0, p.shares - shares);
            const std::string reason = killed_ ? "drawdown_kill" :
                (game_start ? "game_start" : (now - p.entry_ts >= hold_ ? "max_hold" : "micro_reversal"));
            const bool fully_exited = p.shares <= 1e-9;
            append_fill(p, fully_exited ? "SELL_TAKER" : "SELL_TAKER_PARTIAL", shares, px, fee, reason);
            if (fully_exited) erase.push_back(id);
        }
        for (const auto& id : erase) positions_.erase(id);
    }

    double equity(const std::unordered_map<std::string,pm::Book>& books) const {
        double e = cash_;
        for (const auto& [id, p] : positions_) {
            (void)id;
            auto it = books.find(p.token_id);
            const double bid = it != books.end() ? it->second.best_bid() : p.entry_price;
            e += p.shares * (std::isfinite(bid) ? bid : p.entry_price);
        }
        return e;
    }
};

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config = "config/paper_v3.json";
        std::string run_dir = "runs/paper_v3_maker";
        std::size_t markets = 600;
        double min_liquidity = 100.0, min_edge = 0.003, max_order_usd = 75.0, adverse_mult = 0.50;
        double max_queue_ratio = 0.0;
        std::int64_t ttl = 300, hold = 180;
        int interval = 10;
        bool loop = false;
        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a);
                return std::string(argv[++i]);
            };
            if (a == "--config") config = next();
            else if (a == "--run-dir") run_dir = next();
            else if (a == "--markets") markets = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--min-liquidity") min_liquidity = std::stod(next());
            else if (a == "--min-edge") min_edge = std::stod(next());
            else if (a == "--max-order-usd") max_order_usd = std::stod(next());
            else if (a == "--ttl-seconds") ttl = std::stoll(next());
            else if (a == "--hold-seconds") hold = std::stoll(next());
            else if (a == "--adverse-selection-mult") adverse_mult = std::stod(next());
            else if (a == "--max-queue-ratio") max_queue_ratio = std::stod(next());
            else if (a == "--interval") interval = std::stoi(next());
            else if (a == "--loop") loop = true;
            else if (a == "--once") loop = false;
            else if (a == "--help" || a == "-h") {
                std::cout << "polymarket_maker_paper [--config FILE] [--run-dir DIR] [--markets N] [--min-edge X] "
                             "[--max-order-usd X] [--ttl-seconds N] [--hold-seconds N] "
                             "[--adverse-selection-mult X] [--max-queue-ratio X] [--interval N] [--once|--loop]\n";
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + a);
            }
        }
        auto cfg = pm::Engine::load_config(config);
        MakerPaper paper(cfg, run_dir, min_edge, max_order_usd, ttl, hold, adverse_mult, max_queue_ratio);
        do {
            paper.tick(markets, min_liquidity);
            if (loop) std::this_thread::sleep_for(std::chrono::seconds(std::max(1, interval)));
        } while (loop);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
