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
#include <iomanip>
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
    for (const auto& l : levels) if (std::abs(l.price - px) <= 1e-9) q += l.size;
    return q;
}

struct Order {
    std::string market_id;
    std::string event_id;
    std::string slug;
    std::string side;
    std::string token_id;
    double limit_price = 0.0;
    double shares = 0.0;
    double queue_ahead = 0.0;
    double created_last_trade = 0.0;
    std::int64_t created_ts = 0;
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
    if (std::isfinite(y) && std::isfinite(n) && wy + wn > 1e-12) q = (wy * y + wn * (1.0 - n)) / (wy + wn);
    else if (std::isfinite(y)) q = y;
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
          cash_(cfg_.starting_capital) {
        fs::create_directories(run_dir_);
        load_state();
        ensure_files();
    }

    void tick(std::size_t market_limit, double min_liquidity) {
        auto markets = api_.discover_markets(market_limit, min_liquidity);
        std::unordered_map<std::string,const pm::Market*> market_by_id;
        std::vector<std::string> tokens;
        for (const auto& m : markets) {
            market_by_id[m.id] = &m;
            tokens.push_back(m.yes_token); tokens.push_back(m.no_token);
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

        process_orders(books, market_by_id, now);
        process_positions(books, market_by_id, now);

        std::size_t signals = 0, posted = 0;
        for (const auto& m : markets) {
            if (positions_.count(m.id) || orders_.count(m.id)) continue;
            auto yi = books.find(m.yes_token), ni = books.find(m.no_token);
            if (yi == books.end() || ni == books.end()) continue;
            const auto& yb = yi->second; const auto& nb = ni->second;
            const double mid = yb.midpoint();
            if (!std::isfinite(mid) || mid <= cfg_.min_mid || mid >= cfg_.max_mid) continue;
            if (yb.spread() > cfg_.max_spread || nb.spread() > cfg_.max_spread) continue;
            auto ms = micro_signal(yb, nb);
            if (ms.confidence < 0.10) continue;

            struct Choice { std::string side; const pm::Book* b; std::string token; double fair; double edge; };
            std::vector<Choice> choices;
            auto consider = [&](const std::string& side, const pm::Book& b, const std::string& token, double fair) {
                const double bid = b.best_bid(), ask = b.best_ask();
                if (!std::isfinite(bid) || !std::isfinite(ask) || bid <= 0.0 || ask <= bid) return;
                const double adverse_buffer = adverse_mult_ * b.spread() * (1.0 - ms.confidence);
                const double edge = fair - bid - adverse_buffer;
                if (edge > min_edge_) choices.push_back({side, &b, token, fair, edge});
            };
            consider("YES", yb, m.yes_token, ms.q_yes);
            consider("NO", nb, m.no_token, 1.0 - ms.q_yes);
            if (choices.empty()) continue;
            ++signals;
            auto c = *std::max_element(choices.begin(), choices.end(), [](const auto& a, const auto& b){ return a.edge < b.edge; });

            const double limit = c.b->best_bid();
            if (!std::isfinite(limit) || !std::isfinite(c.b->best_ask()) || limit >= c.b->best_ask() - 1e-12) continue;
            const double max_cash = std::min({max_order_usd_, cash_, cfg_.max_market_fraction * std::max(1.0, equity(books))});
            double shares = max_cash / limit;
            shares = std::min(shares, std::max(c.b->min_order_size, 0.25 * std::max(1.0, touch_size(*c.b, true))));
            if (shares < c.b->min_order_size || shares * limit > cash_ + 1e-9) continue;
            orders_[m.id] = Order{m.id, m.event_id, m.slug, c.side, c.token, limit, shares,
                                  touch_size(*c.b, true), c.b->last_trade, now};
            append_order("POST", orders_.at(m.id), c.edge, ms.confidence);
            ++posted;
        }

        persist();
        const double eq = equity(books);
        append_equity(now, eq);
        std::cout << "maker_tick markets=" << markets.size() << " signals=" << signals << " posted=" << posted
                  << " resting=" << orders_.size() << " positions=" << positions_.size()
                  << " cash=" << cash_ << " equity=" << eq << '\n';
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
    std::unordered_map<std::string,Order> orders_;
    std::unordered_map<std::string,Position> positions_;

    void ensure_files() {
        auto ensure = [&](const std::string& file, const std::string& header) {
            const auto p = fs::path(run_dir_) / file;
            if (!fs::exists(p) || fs::file_size(p) == 0) { std::ofstream f(p); f << header << '\n'; }
        };
        ensure("maker_order_log.csv", "timestamp,action,market_id,slug,side,token_id,limit_price,shares,queue_ahead,signal_edge,confidence");
        ensure("maker_fills.csv", "timestamp,market_id,slug,action,side,shares,price,fee,reason");
        ensure("maker_equity.csv", "timestamp,cash,equity,resting_orders,positions");
    }

    void load_state() {
        {
            std::ifstream f(fs::path(run_dir_) / "maker_risk.csv");
            std::string line; std::getline(f, line);
            if (std::getline(f, line)) { try { cash_ = std::stod(line); } catch (...) {} }
        }
        {
            std::ifstream f(fs::path(run_dir_) / "maker_orders.csv");
            std::string line; std::getline(f, line);
            while (std::getline(f, line)) {
                auto x = split(line); if (x.size() < 10) continue;
                try {
                    Order o{x[0],x[1],x[2],x[3],x[4],std::stod(x[5]),std::stod(x[6]),std::stod(x[7]),std::stod(x[8]),std::stoll(x[9])};
                    orders_[o.market_id] = std::move(o);
                } catch (...) {}
            }
        }
        {
            std::ifstream f(fs::path(run_dir_) / "maker_positions.csv");
            std::string line; std::getline(f, line);
            while (std::getline(f, line)) {
                auto x = split(line); if (x.size() < 8) continue;
                try {
                    Position p{x[0],x[1],x[2],x[3],x[4],std::stod(x[5]),std::stod(x[6]),std::stoll(x[7])};
                    positions_[p.market_id] = std::move(p);
                } catch (...) {}
            }
        }
    }

    void persist() const {
        {
            std::ofstream f(fs::path(run_dir_) / "maker_risk.csv");
            f << "cash\n" << cash_ << '\n';
        }
        {
            std::ofstream f(fs::path(run_dir_) / "maker_orders.csv");
            f << "market_id,event_id,slug,side,token_id,limit_price,shares,queue_ahead,created_last_trade,created_ts\n";
            for (const auto& [id,o] : orders_) {
                (void)id;
                f << o.market_id << ',' << o.event_id << ',' << o.slug << ',' << o.side << ',' << o.token_id << ','
                  << o.limit_price << ',' << o.shares << ',' << o.queue_ahead << ',' << o.created_last_trade << ',' << o.created_ts << '\n';
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

    void append_fill(const Position& p, const std::string& action, double px, double fee, const std::string& reason) const {
        std::ofstream f(fs::path(run_dir_) / "maker_fills.csv", std::ios::app);
        f << now_s() << ',' << p.market_id << ',' << p.slug << ',' << action << ',' << p.side << ',' << p.shares << ','
          << px << ',' << fee << ',' << reason << '\n';
    }

    void append_equity(std::int64_t ts, double eq) const {
        std::ofstream f(fs::path(run_dir_) / "maker_equity.csv", std::ios::app);
        f << ts << ',' << cash_ << ',' << eq << ',' << orders_.size() << ',' << positions_.size() << '\n';
    }

    bool strict_trade_through(const Order& o, const pm::Book& b) const {
        const double tick = std::max(1e-6, b.tick_size);
        const double ask = b.best_ask();
        if (std::isfinite(ask) && ask < o.limit_price - 0.25 * tick) return true;
        if (b.last_trade > 0.0 && std::abs(b.last_trade - o.created_last_trade) > 1e-12 &&
            b.last_trade < o.limit_price - 0.25 * tick) return true;
        return false;
    }

    void process_orders(const std::unordered_map<std::string,pm::Book>& books,
                        const std::unordered_map<std::string,const pm::Market*>& market_by_id,
                        std::int64_t now) {
        std::vector<std::string> erase;
        for (auto& [id,o] : orders_) {
            auto bit = books.find(o.token_id);
            if (bit == books.end()) continue;
            if (strict_trade_through(o, bit->second)) {
                const double cost = o.shares * o.limit_price; // maker entry: no fee
                if (cost <= cash_ + 1e-9) {
                    cash_ -= cost;
                    Position p{o.market_id,o.event_id,o.slug,o.side,o.token_id,o.shares,o.limit_price,now};
                    positions_[id] = p;
                    append_fill(p, "BUY_MAKER", o.limit_price, 0.0, "strict_trade_through");
                    append_order("FILL", o, 0.0, 0.0);
                }
                erase.push_back(id);
                continue;
            }
            if (now - o.created_ts >= ttl_) {
                append_order("CANCEL_TTL", o, 0.0, 0.0);
                erase.push_back(id);
                continue;
            }
            const double bb = bit->second.best_bid();
            if (std::isfinite(bb) && bb > o.limit_price + 0.5 * std::max(1e-6, bit->second.tick_size)) {
                // Our order has fallen behind the live best bid. Cancel instead of pretending queue priority.
                append_order("CANCEL_STALE", o, 0.0, 0.0);
                erase.push_back(id);
            }
        }
        for (const auto& id : erase) orders_.erase(id);
        (void)market_by_id;
    }

    void process_positions(const std::unordered_map<std::string,pm::Book>& books,
                           const std::unordered_map<std::string,const pm::Market*>& market_by_id,
                           std::int64_t now) {
        std::vector<std::string> erase;
        for (auto& [id,p] : positions_) {
            auto bit = books.find(p.token_id);
            if (bit == books.end()) continue;
            bool exit = now - p.entry_ts >= hold_;
            auto mit = market_by_id.find(id);
            if (mit != market_by_id.end()) {
                const auto* m = mit->second;
                auto yi = books.find(m->yes_token), ni = books.find(m->no_token);
                if (yi != books.end() && ni != books.end()) {
                    auto ms = micro_signal(yi->second, ni->second);
                    const double fair = p.side == "YES" ? ms.q_yes : 1.0 - ms.q_yes;
                    const double bid = bit->second.best_bid();
                    if (std::isfinite(bid) && fair <= bid + 0.25 * bit->second.spread()) exit = true;
                }
            }
            if (!exit) continue;

            auto walked = pm::Engine::walk_book(bit->second, false, p.shares * std::max(1e-6, bit->second.best_bid()));
            if (!walked || walked->first + 1e-9 < p.shares * 0.98) continue;
            const double shares = std::min(p.shares, walked->first);
            const double px = walked->second * (1.0 - cfg_.slippage_bps / 10000.0);
            pm::FeeDetails fd;
            if (mit != market_by_id.end()) fd = api_.fetch_fee_details(*mit->second);
            const double fee = pm::Engine::protocol_fee(shares, px, fd);
            cash_ += shares * px - fee;
            append_fill(p, "SELL_TAKER", px, fee, now - p.entry_ts >= hold_ ? "max_hold" : "micro_reversal");
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
            auto next = [&]() { if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a); return std::string(argv[++i]); };
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
            } else throw std::runtime_error("Unknown argument: " + a);
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
