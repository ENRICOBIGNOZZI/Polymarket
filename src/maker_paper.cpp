#include "pm/api.hpp"
#include "pm/engine.hpp"
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
#include <set>
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
    for (const auto& l : levels)
        if (std::abs(l.price - px) <= 1e-9) q += l.size;
    return q;
}

std::optional<std::pair<double,double>> sell_shares(const pm::Book& b, double requested_shares) {
    if (requested_shares <= 0.0) return std::nullopt;
    auto levels = b.bids;
    std::sort(levels.begin(), levels.end(), [](const auto& a, const auto& c) { return a.price > c.price; });
    double remaining = requested_shares, filled = 0.0, proceeds = 0.0;
    for (const auto& l : levels) {
        if (l.price <= 0.0 || l.price >= 1.0 || l.size <= 0.0) continue;
        const double q = std::min(remaining, l.size);
        filled += q;
        proceeds += q * l.price;
        remaining -= q;
        if (remaining <= 1e-9) break;
    }
    if (filled <= 0.0) return std::nullopt;
    return std::pair{filled, proceeds / filled};
}

struct Order {
    std::string market_id;
    std::string event_id;
    std::string condition_id;
    std::string slug;
    std::string side;
    std::string token_id;
    double limit_price = 0.0;
    double shares = 0.0; // remaining shares
    double queue_ahead = 0.0;
    std::int64_t created_ts = 0;
    bool sports_market = false;
    std::int64_t game_start_ts = 0;
    std::int64_t tape_ts = 0;
    std::string tape_key;
};

struct Position {
    std::string market_id;
    std::string event_id;
    std::string slug;
    std::string side;
    std::string token_id;
    double shares = 0.0;
    double entry_price = 0.0;
    std::int64_t entry_ts = 0;
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
    if (std::isfinite(y) && std::isfinite(n) && wy + wn > 1e-12)
        q = (wy * y + wn * (1.0 - n)) / (wy + wn);
    else if (std::isfinite(y))
        q = y;
    const double parity = std::isfinite(y) && std::isfinite(n) ? std::abs(y - (1.0 - n)) : 0.25;
    const double liq = (dy + dn) / (dy + dn + 200.0);
    const double spread_conf = std::exp(-5.0 * (yes.spread() + no.spread()));
    const double conf = std::clamp(liq * spread_conf * std::exp(-8.0 * parity), 0.02, 1.0);
    return {std::clamp(q, 0.001, 0.999), conf};
}

class MakerPaper {
public:
    MakerPaper(pm::Config cfg, std::string run_dir, double min_edge, double max_order_usd,
               std::int64_t ttl, std::int64_t hold, double adverse_mult)
        : cfg_(std::move(cfg)), api_(cfg_), run_dir_(std::move(run_dir)), min_edge_(min_edge),
          max_order_usd_(max_order_usd), ttl_(ttl), hold_(hold), adverse_mult_(adverse_mult),
          cash_(cfg_.starting_capital), peak_equity_(cfg_.starting_capital) {
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
        for (const auto& [id,o] : orders_) {
            (void)id;
            if (std::find(tokens.begin(), tokens.end(), o.token_id) == tokens.end()) tokens.push_back(o.token_id);
        }
        for (const auto& [id,p] : positions_) {
            (void)id;
            if (std::find(tokens.begin(), tokens.end(), p.token_id) == tokens.end()) tokens.push_back(p.token_id);
        }
        auto books = tokens.empty() ? std::unordered_map<std::string,pm::Book>{} : api_.fetch_books(tokens);
        const auto now = now_s();

        std::set<std::string> condition_set;
        std::int64_t tape_after = now;
        for (const auto& [id,o] : orders_) {
            (void)id;
            if (!o.condition_id.empty()) condition_set.insert(o.condition_id);
            const auto cursor = o.tape_ts > 0 ? o.tape_ts : o.created_ts;
            if (cursor > 0) tape_after = std::min(tape_after, cursor);
        }
        std::vector<std::string> conditions(condition_set.begin(), condition_set.end());
        std::unordered_map<std::string,std::vector<pm::PublicTrade>> tape;
        if (!conditions.empty()) {
            // Re-read one second around the cursor; the per-order (timestamp,key) cursor prevents duplicates.
            tape = api_.fetch_public_trades(conditions, std::max<std::int64_t>(0, tape_after - 1), 10);
        }

        // Mark risk before processing fills/exits so a drawdown immediately freezes new maker risk.
        double eq = equity(books);
        peak_equity_ = std::max(peak_equity_, eq);
        if (peak_equity_ > 0.0 && 1.0 - eq / peak_equity_ >= cfg_.max_drawdown) killed_ = true;

        process_orders(books, tape, now);
        process_positions(books, market_by_id, now);
        eq = equity(books);
        peak_equity_ = std::max(peak_equity_, eq);
        if (peak_equity_ > 0.0 && 1.0 - eq / peak_equity_ >= cfg_.max_drawdown) killed_ = true;

        std::size_t signals = 0, posted = 0;
        if (!killed_) {
            for (const auto& m : markets) {
                if (!pm::eligible_before_game_start(m, now, 60)) continue;
                if (positions_.count(m.id) || orders_.count(m.id)) continue;
                auto yi = books.find(m.yes_token), ni = books.find(m.no_token);
                if (yi == books.end() || ni == books.end()) continue;
                const auto& yb = yi->second;
                const auto& nb = ni->second;
                const double mid = yb.midpoint();
                if (!std::isfinite(mid) || mid <= cfg_.min_mid || mid >= cfg_.max_mid) continue;
                if (yb.spread() > cfg_.max_spread || nb.spread() > cfg_.max_spread) continue;
                auto ms = micro_signal(yb, nb);
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
                auto consider = [&](const std::string& side, const pm::Book& b, const std::string& token, double fair) {
                    const double bid = b.best_bid(), ask = b.best_ask(), spread = b.spread();
                    if (!std::isfinite(bid) || !std::isfinite(ask) || !std::isfinite(spread) ||
                        bid <= 0.0 || ask <= bid) return;
                    const double adverse_buffer = adverse_mult_ * spread * (1.0 - ms.confidence);
                    // Entry is passive, but the expected exit is deliberately costed as taker.
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
                auto c = *std::max_element(choices.begin(), choices.end(), [](const auto& a, const auto& b) {
                    return a.net_expected_edge < b.net_expected_edge;
                });

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
                    max_order_usd_,
                    available_cash,
                    cfg_.max_market_fraction * eq,
                    std::max(0.0, event_room),
                    std::max(0.0, gross_room),
                    std::max(0.0, loss_room)
                });
                if (max_cash <= 0.0) continue;

                double shares = max_cash / limit;
                // We join behind the complete displayed best-bid queue and cap our own size.
                shares = std::min(shares, std::max(c.book->min_order_size, 0.25 * std::max(1.0, touch_size(*c.book, true))));
                if (shares < c.book->min_order_size || shares * limit > available_cash + 1e-9) continue;

                orders_[m.id] = Order{
                    m.id, m.event_id, m.condition_id, m.slug, c.side, c.token, limit, shares,
                    touch_size(*c.book, true), now, m.sports_market, m.game_start_ts, now, {}
                };
                append_order("POST", orders_.at(m.id), c.net_expected_edge, ms.confidence);
                ++posted;
            }
        }

        eq = equity(books);
        peak_equity_ = std::max(peak_equity_, eq);
        persist();
        append_equity(now, eq);
        std::cout << "maker_tick markets=" << markets.size()
                  << " tape_markets=" << tape.size()
                  << " signals=" << signals
                  << " posted=" << posted
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
        ensure("maker_order_log.csv", "timestamp,action,market_id,slug,side,token_id,limit_price,remaining_shares,queue_ahead,signal_edge,confidence");
        ensure("maker_fills.csv", "timestamp,market_id,slug,action,side,shares,price,fee,reason");
        ensure("maker_equity.csv", "timestamp,cash,equity,reserved_cash,resting_orders,positions,peak_equity,drawdown,killed");
    }

    void load_state() {
        {
            std::ifstream f(fs::path(run_dir_) / "maker_risk.csv");
            std::string line;
            std::getline(f, line);
            if (std::getline(f, line)) {
                auto x = split(line);
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
                auto x = split(line);
                try {
                    if (x.size() >= 14) {
                        Order o{x[0],x[1],x[2],x[3],x[4],x[5],std::stod(x[6]),std::stod(x[7]),std::stod(x[8]),
                                std::stoll(x[9]),std::stoi(x[10])!=0,std::stoll(x[11]),std::stoll(x[12]),x[13]};
                        orders_[o.market_id] = std::move(o);
                    } else if (x.size() >= 10) {
                        // Legacy V3 orders lack condition/tape/game metadata. They are loaded only so that
                        // process_orders can cancel them explicitly; they can never be filled.
                        Order o;
                        o.market_id=x[0]; o.event_id=x[1]; o.slug=x[2]; o.side=x[3]; o.token_id=x[4];
                        o.limit_price=std::stod(x[5]); o.shares=std::stod(x[6]); o.queue_ahead=std::stod(x[7]);
                        o.created_ts=std::stoll(x[9]); o.tape_ts=o.created_ts;
                        orders_[o.market_id]=std::move(o);
                    }
                } catch (...) {}
            }
        }
        {
            std::ifstream f(fs::path(run_dir_) / "maker_positions.csv");
            std::string line;
            std::getline(f, line);
            while (std::getline(f, line)) {
                auto x = split(line);
                if (x.size() < 8) continue;
                try {
                    Position p{x[0],x[1],x[2],x[3],x[4],std::stod(x[5]),std::stod(x[6]),std::stoll(x[7])};
                    positions_[p.market_id] = std::move(p);
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
            f << "market_id,event_id,condition_id,slug,side,token_id,limit_price,remaining_shares,queue_ahead,created_ts,sports_market,game_start_ts,tape_ts,tape_key\n";
            for (const auto& [id,o] : orders_) {
                (void)id;
                f << o.market_id << ',' << o.event_id << ',' << o.condition_id << ',' << o.slug << ',' << o.side << ','
                  << o.token_id << ',' << o.limit_price << ',' << o.shares << ',' << o.queue_ahead << ',' << o.created_ts << ','
                  << (o.sports_market?1:0) << ',' << o.game_start_ts << ',' << o.tape_ts << ',' << o.tape_key << '\n';
            }
        }
        {
            std::ofstream f(fs::path(run_dir_) / "maker_positions.csv");
            f << "market_id,event_id,slug,side,token_id,shares,entry_price,entry_ts\n";
            for (const auto& [id,p] : positions_) {
                (void)id;
                f << p.market_id << ',' << p.event_id << ',' << p.slug << ',' << p.side << ',' << p.token_id << ','
                  << p.shares << ',' << p.entry_price << ',' << p.entry_ts << '\n';
            }
        }
    }

    void append_order(const std::string& action, const Order& o, double edge, double confidence) const {
        std::ofstream f(fs::path(run_dir_) / "maker_order_log.csv", std::ios::app);
        f << now_s() << ',' << action << ',' << o.market_id << ',' << o.slug << ',' << o.side << ',' << o.token_id << ','
          << o.limit_price << ',' << o.shares << ',' << o.queue_ahead << ',' << edge << ',' << confidence << '\n';
    }

    void append_fill(const Position& p, const std::string& action, double shares, double px, double fee, const std::string& reason) const {
        std::ofstream f(fs::path(run_dir_) / "maker_fills.csv", std::ios::app);
        f << now_s() << ',' << p.market_id << ',' << p.slug << ',' << action << ',' << p.side << ',' << shares << ','
          << px << ',' << fee << ',' << reason << '\n';
    }

    void append_equity(std::int64_t ts, double eq) const {
        std::ofstream f(fs::path(run_dir_) / "maker_equity.csv", std::ios::app);
        f << ts << ',' << cash_ << ',' << eq << ',' << reserved_cash() << ',' << orders_.size() << ',' << positions_.size() << ','
          << peak_equity_ << ',' << (peak_equity_ > 0.0 ? 1.0 - eq / peak_equity_ : 0.0) << ',' << (killed_ ? 1 : 0) << '\n';
    }

    double reserved_cash() const {
        double x = 0.0;
        for (const auto& [id,o] : orders_) {
            (void)id;
            x += std::max(0.0, o.shares * o.limit_price);
        }
        return x;
    }

    double position_cost() const {
        double x = 0.0;
        for (const auto& [id,p] : positions_) {
            (void)id;
            x += std::max(0.0, p.shares * p.entry_price);
        }
        return x;
    }

    double event_committed(const std::string& event_id) const {
        double x = 0.0;
        for (const auto& [id,o] : orders_) {
            (void)id;
            if (o.event_id == event_id) x += o.shares * o.limit_price;
        }
        for (const auto& [id,p] : positions_) {
            (void)id;
            if (p.event_id == event_id) x += p.shares * p.entry_price;
        }
        return x;
    }

    void add_maker_fill(const std::string& id, const Order& o, double filled, std::int64_t now) {
        if (filled <= 0.0) return;
        const double cost = filled * o.limit_price;
        if (cost > cash_ + 1e-9) return;
        cash_ -= cost;
        auto it = positions_.find(id);
        if (it == positions_.end()) {
            Position p{o.market_id,o.event_id,o.slug,o.side,o.token_id,filled,o.limit_price,now};
            positions_[id]=p;
            append_fill(positions_.at(id),"BUY_MAKER",filled,o.limit_price,0.0,"public_taker_sell_after_queue");
        } else {
            const double total=it->second.shares+filled;
            it->second.entry_price=(it->second.shares*it->second.entry_price+filled*o.limit_price)/std::max(1e-12,total);
            it->second.shares=total;
            append_fill(it->second,"BUY_MAKER",filled,o.limit_price,0.0,"public_taker_sell_after_queue");
        }
    }

    void process_orders(
        const std::unordered_map<std::string,pm::Book>& books,
        const std::unordered_map<std::string,std::vector<pm::PublicTrade>>& tape,
        std::int64_t now) {
        std::vector<std::string> erase;
        for (auto& [id,o] : orders_) {
            if (killed_) {
                append_order("CANCEL_KILL", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            if (o.condition_id.empty()) {
                append_order("CANCEL_UNVERIFIABLE_TAPE", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            if (o.sports_market && (o.game_start_ts <= 0 || now + 60 >= o.game_start_ts)) {
                append_order("CANCEL_GAME_START", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }

            auto bit = books.find(o.token_id);
            if (bit != books.end()) {
                auto tit=tape.find(o.condition_id);
                if (tit!=tape.end()) {
                    for(const auto& trade:tit->second) {
                        if(trade.ts<o.tape_ts||(trade.ts==o.tape_ts&&!o.tape_key.empty()&&trade.key<=o.tape_key)) continue;
                        o.tape_ts=trade.ts;
                        o.tape_key=trade.key;
                        if(!pm::is_aggressive_sell_for_bid(trade,o.token_id,o.limit_price,bit->second.tick_size,o.created_ts)) continue;

                        const double old_queue=o.queue_ahead;
                        const double old_remaining=o.shares;
                        const auto q=pm::consume_bid_queue(o.queue_ahead,o.shares,trade.size);
                        o.queue_ahead=q.queue_ahead;
                        o.shares=q.remaining_shares;
                        if(q.filled_shares>0.0) {
                            if(q.filled_shares*o.limit_price>cash_+1e-9) {
                                o.queue_ahead=old_queue;
                                o.shares=old_remaining;
                                append_order("CANCEL_CAPITAL",o,0.0,0.0);
                                erase.push_back(id);
                                break;
                            }
                            add_maker_fill(id,o,q.filled_shares,now);
                            append_order(o.shares<=1e-9?"FILL":"PARTIAL_FILL",o,0.0,0.0);
                        } else if(o.queue_ahead+1e-9<old_queue) {
                            append_order("QUEUE_DEPLETION",o,0.0,0.0);
                        }
                        if(o.shares<=1e-9) {
                            erase.push_back(id);
                            break;
                        }
                    }
                }
            }
            if (std::find(erase.begin(),erase.end(),id)!=erase.end()) continue;

            if (now - o.created_ts >= ttl_) {
                append_order("CANCEL_TTL", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            if (bit != books.end()) {
                const double bb = bit->second.best_bid();
                if (std::isfinite(bb) && bb > o.limit_price + 0.5 * std::max(1e-6, bit->second.tick_size)) {
                    append_order("CANCEL_STALE", o, 0.0, 0.0);
                    erase.push_back(id);
                }
            }
        }
        for (const auto& id : erase) orders_.erase(id);
    }

    void process_positions(const std::unordered_map<std::string,pm::Book>& books,
                           const std::unordered_map<std::string,const pm::Market*>& market_by_id,
                           std::int64_t now) {
        std::vector<std::string> erase;
        for (auto& [id,p] : positions_) {
            auto bit = books.find(p.token_id);
            if (bit == books.end()) continue;
            bool exit = killed_ || now - p.entry_ts >= hold_;

            const pm::Market* market=nullptr;
            std::optional<pm::Market> fetched;
            auto mit = market_by_id.find(id);
            if (mit != market_by_id.end()) market=mit->second;
            else {
                try { fetched=api_.fetch_market_by_id(id); } catch(...) {}
                if(fetched) market=&*fetched;
            }

            if (market) {
                auto yi = books.find(market->yes_token), ni = books.find(market->no_token);
                if (yi != books.end() && ni != books.end()) {
                    auto ms = micro_signal(yi->second, ni->second);
                    const double fair = p.side == "YES" ? ms.q_yes : 1.0 - ms.q_yes;
                    const double bid = bit->second.best_bid();
                    if (std::isfinite(bid) && fair <= bid + 0.25 * bit->second.spread()) exit = true;
                }
            }
            if (!exit) continue;

            auto oit=orders_.find(id);
            if(oit!=orders_.end()) {
                append_order("CANCEL_POSITION_EXIT",oit->second,0.0,0.0);
                orders_.erase(oit);
            }

            auto walked=sell_shares(bit->second,p.shares);
            if(!walked||walked->first+1e-9<p.shares*0.999) continue;
            const double shares=p.shares;
            const double px=walked->second*(1.0-cfg_.slippage_bps/10000.0);
            pm::FeeDetails fd{0.07,1.0,true};
            if(market) fd=api_.fetch_fee_details(*market);
            const double fee=pm::Engine::protocol_fee(shares,px,fd);
            cash_+=shares*px-fee;
            append_fill(p,"SELL_TAKER",shares,px,fee,
                        killed_?"drawdown_kill":(now-p.entry_ts>=hold_?"max_hold":"micro_reversal"));
            erase.push_back(id);
        }
        for (const auto& id : erase) positions_.erase(id);
    }

    double equity(const std::unordered_map<std::string,pm::Book>& books) const {
        double e = cash_;
        for (const auto& [id,p] : positions_) {
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
            else if (a == "--interval") interval = std::stoi(next());
            else if (a == "--loop") loop = true;
            else if (a == "--once") loop = false;
            else if (a == "--help" || a == "-h") {
                std::cout << "polymarket_maker_paper [--config FILE] [--run-dir DIR] [--markets N] [--min-edge X] "
                             "[--max-order-usd X] [--ttl-seconds N] [--hold-seconds N] [--adverse-selection-mult X] "
                             "[--interval N] [--once|--loop]\n";
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + a);
            }
        }
        auto cfg = pm::Engine::load_config(config);
        MakerPaper paper(cfg, run_dir, min_edge, max_order_usd, ttl, hold, adverse_mult);
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
