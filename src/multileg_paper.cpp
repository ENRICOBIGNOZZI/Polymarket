#include "pm/api.hpp"
#include "pm/engine.hpp"
#include "pm/execution.hpp"
#include "pm/types.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {
namespace fs = std::filesystem;

std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}
std::int64_t now_s() { return now_ms() / 1000; }

std::vector<std::string> split_csv(const std::string& line) {
    std::vector<std::string> out;
    std::string cur;
    bool quoted = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (c == '"') {
            if (quoted && i + 1 < line.size() && line[i + 1] == '"') {
                cur.push_back('"');
                ++i;
            } else {
                quoted = !quoted;
            }
        } else if (c == ',' && !quoted) {
            out.push_back(cur);
            cur.clear();
        } else {
            cur.push_back(c);
        }
    }
    out.push_back(cur);
    return out;
}

std::string csv_escape(const std::string& s) {
    if (s.find_first_of(",\"\n\r") == std::string::npos) return s;
    std::string out = "\"";
    for (char c : s) out += c == '"' ? "\"\"" : std::string(1, c);
    out += '"';
    return out;
}

double touch_size_at(const pm::Book& b, double px, bool bid_side) {
    double q = 0.0;
    const auto& levels = bid_side ? b.bids : b.asks;
    for (const auto& l : levels) {
        if (std::abs(l.price - px) <= std::max(1e-9, 0.25 * b.tick_size)) q += std::max(0.0, l.size);
    }
    return q;
}

struct IntentLeg {
    std::string bundle_id;
    std::string strategy;
    std::string event_id;
    std::int64_t created_ts = 0;
    std::string mode;
    double expected_edge = 0.0;
    double max_notional = 0.0;
    std::string market_id;
    std::string side;
    double weight = 0.0;
    double limit_price = 0.0;
    std::int64_t execution_deadline_ts = 0;
    std::int64_t hold_deadline_ts = 0;
};

struct TapeTrade {
    std::int64_t ts = 0;
    std::string asset_id;
    std::string side;
    double price = 0.0;
    double size = 0.0;
};

struct LegState {
    std::string bundle_id;
    std::string market_id;
    std::string event_id;
    std::string side;
    std::string token_id;
    double weight = 0.0;
    double target_shares = 0.0;
    double filled_shares = 0.0;
    double limit_price = 0.0;
    double queue_ahead = 0.0;
    std::int64_t arrival_ms = 0;
    std::int64_t cancel_effective_ms = 0;
    int replace_count = 0;
    double entry_cash = 0.0;
    double entry_fee = 0.0;
    double exit_cash = 0.0;
    double exit_fee = 0.0;
    double slippage_cost = 0.0;
    std::int64_t first_fill_ts = 0;
    std::int64_t last_fill_ts = 0;
    double adverse_mark_pnl = 0.0;
    bool adverse_recorded = false;
    std::string order_state = "RESTING";

    double remaining() const { return std::max(0.0, target_shares - filled_shares); }
    double fill_fraction() const {
        return target_shares > 1e-12 ? std::clamp(filled_shares / target_shares, 0.0, 1.0) : 0.0;
    }
    double entry_avg() const { return filled_shares > 1e-12 ? entry_cash / filled_shares : 0.0; }
};

struct BundleState {
    std::string bundle_id;
    std::string strategy;
    std::string event_id;
    std::string status = "RESTING";
    std::int64_t created_ts = 0;
    double expected_edge = 0.0;
    double max_notional = 0.0;
    std::int64_t execution_deadline_ts = 0;
    std::int64_t hold_deadline_ts = 0;
    bool ledger_written = false;
    std::string abort_reason;
};

struct SellResult {
    double shares = 0.0;
    double raw_avg = 0.0;
    double slipped_avg = 0.0;
    double fee = 0.0;
};

std::optional<SellResult> sell_all(const pm::Book& book, double shares, double slippage_bps, const pm::FeeDetails& fd) {
    if (shares <= 0.0) return SellResult{};
    auto bids = book.bids;
    std::sort(bids.begin(), bids.end(), [](const auto& a, const auto& b) { return a.price > b.price; });
    double remaining = shares, raw_cash = 0.0, sold = 0.0;
    for (const auto& l : bids) {
        if (l.price <= 0.0 || l.size <= 0.0) continue;
        const double q = std::min(remaining, l.size);
        sold += q;
        raw_cash += q * l.price;
        remaining -= q;
        if (remaining <= 1e-9) break;
    }
    if (sold + 1e-9 < shares) return std::nullopt;
    const double raw_avg = raw_cash / sold;
    const double slipped = raw_avg * (1.0 - slippage_bps / 10000.0);
    const double fee = pm::Engine::protocol_fee(sold, slipped, fd);
    return SellResult{sold, raw_avg, slipped, fee};
}

class MultiLegPaper {
public:
    MultiLegPaper(pm::Config cfg, std::string run_dir, std::string intents, std::string tape,
                  double min_edge, double completion_threshold, int submit_latency_ms,
                  int cancel_latency_ms, int max_replaces, double max_leg_risk_usd,
                  int adverse_horizon_s)
        : cfg_(std::move(cfg)), api_(cfg_), run_dir_(std::move(run_dir)), intents_path_(std::move(intents)),
          tape_path_(std::move(tape)), min_edge_(min_edge), completion_threshold_(completion_threshold),
          submit_latency_ms_(std::max(0, submit_latency_ms)), cancel_latency_ms_(std::max(0, cancel_latency_ms)),
          max_replaces_(std::max(0, max_replaces)), max_leg_risk_usd_(std::max(0.0, max_leg_risk_usd)),
          adverse_horizon_s_(std::max(1, adverse_horizon_s)), cash_(cfg_.starting_capital), peak_equity_(cfg_.starting_capital) {
        fs::create_directories(run_dir_);
        ensure_logs();
        load_state();
    }

    void tick() {
        const auto now = now_s();
        const auto nowm = now_ms();
        load_new_intents(now);

        std::unordered_map<std::string,pm::Market> markets;
        std::unordered_map<std::string,std::string> token_market;
        std::vector<std::string> tokens;
        resolve_live_markets(markets, token_market, tokens);
        auto books = tokens.empty() ? std::unordered_map<std::string,pm::Book>{} : api_.fetch_books(tokens);

        update_risk(books);
        const auto trades = read_new_tape();
        apply_trades(trades, books, markets, nowm);
        manage_orders(books, markets, nowm);
        manage_bundles(books, markets, now, nowm);
        measure_adverse_selection(books, now);
        update_risk(books);
        persist_state();
        append_equity(now, books);

        std::size_t resting = 0, complete = 0, aborting = 0, closed = 0, unwound = 0;
        for (const auto& [id,b] : bundles_) {
            (void)id;
            if (b.status == "RESTING") ++resting;
            else if (b.status == "COMPLETE") ++complete;
            else if (b.status == "ABORTING") ++aborting;
            else if (b.status == "CLOSED") ++closed;
            else if (b.status == "UNWOUND") ++unwound;
        }
        std::cout << "multileg_tick bundles=" << bundles_.size() << " resting=" << resting
                  << " complete=" << complete << " aborting=" << aborting
                  << " closed=" << closed << " unwound=" << unwound
                  << " trades_processed=" << trades.size() << " tape_cursor=" << tape_cursor_
                  << " reserved=" << reserved_cash() << " cash=" << cash_ << " equity=" << equity(books)
                  << " drawdown=" << drawdown(books) << " killed=" << killed_ << '\n';
    }

private:
    pm::Config cfg_;
    pm::PolymarketApi api_;
    std::string run_dir_;
    std::string intents_path_;
    std::string tape_path_;
    double min_edge_ = 0.001;
    double completion_threshold_ = 0.95;
    int submit_latency_ms_ = 250;
    int cancel_latency_ms_ = 250;
    int max_replaces_ = 3;
    double max_leg_risk_usd_ = 5.0;
    int adverse_horizon_s_ = 60;
    double cash_ = 0.0;
    double peak_equity_ = 0.0;
    bool killed_ = false;
    std::size_t tape_cursor_ = 0;
    std::unordered_map<std::string,BundleState> bundles_;
    std::vector<LegState> legs_;
    std::unordered_map<std::string,pm::Market> market_cache_;
    std::unordered_map<std::string,pm::FeeDetails> fee_cache_;

    fs::path p(const std::string& name) const { return fs::path(run_dir_) / name; }

    void ensure_logs() {
        auto ensure = [&](const std::string& name, const std::string& header) {
            const auto path = p(name);
            if (!fs::exists(path) || fs::file_size(path) == 0) {
                std::ofstream f(path);
                f << header << '\n';
            }
        };
        ensure("multileg_events.csv", "timestamp,event,bundle_id,market_id,side,shares,price,queue_ahead,detail");
        ensure("bundle_ledger.csv", "bundle_id,strategy,event_id,created_ts,closed_ts,status,expected_edge,max_notional,entry_cash,gross_pnl,fees,slippage,net_pnl,return_on_capital,fill_fraction,adverse_mark_pnl,abort_reason");
        ensure("multileg_equity.csv", "timestamp,cash,equity,reserved_cash,gross_entry_cash,peak_equity,drawdown,killed,live_bundles");
    }

    void append_event(const std::string& event, const std::string& bundle_id, const LegState* l,
                      double shares, double price, const std::string& detail) const {
        std::ofstream f(p("multileg_events.csv"), std::ios::app);
        f << now_s() << ',' << event << ',' << csv_escape(bundle_id) << ','
          << (l ? csv_escape(l->market_id) : "") << ',' << (l ? l->side : "") << ','
          << shares << ',' << price << ',' << (l ? l->queue_ahead : 0.0) << ',' << csv_escape(detail) << '\n';
    }

    void load_state() {
        {
            std::ifstream f(p("multileg_risk.csv"));
            std::string line;
            std::getline(f, line);
            if (std::getline(f, line)) {
                auto x = split_csv(line);
                try {
                    if (!x.empty()) cash_ = std::stod(x[0]);
                    if (x.size() > 1) peak_equity_ = std::stod(x[1]);
                    if (x.size() > 2) killed_ = std::stoi(x[2]) != 0;
                    if (x.size() > 3) tape_cursor_ = static_cast<std::size_t>(std::stoull(x[3]));
                } catch (...) {
                }
            }
        }
        {
            std::ifstream f(p("multileg_bundles.csv"));
            std::string line;
            std::getline(f, line);
            while (std::getline(f, line)) {
                auto x = split_csv(line);
                if (x.size() < 11) continue;
                try {
                    BundleState b;
                    b.bundle_id=x[0]; b.strategy=x[1]; b.event_id=x[2]; b.status=x[3];
                    b.created_ts=std::stoll(x[4]); b.expected_edge=std::stod(x[5]); b.max_notional=std::stod(x[6]);
                    b.execution_deadline_ts=std::stoll(x[7]); b.hold_deadline_ts=std::stoll(x[8]);
                    b.ledger_written=std::stoi(x[9])!=0; b.abort_reason=x[10];
                    bundles_[b.bundle_id]=std::move(b);
                } catch (...) {
                }
            }
        }
        {
            std::ifstream f(p("multileg_legs.csv"));
            std::string line;
            std::getline(f, line);
            while (std::getline(f, line)) {
                auto x = split_csv(line);
                if (x.size() < 23) continue;
                try {
                    LegState l;
                    std::size_t i=0;
                    l.bundle_id=x[i++]; l.market_id=x[i++]; l.event_id=x[i++]; l.side=x[i++]; l.token_id=x[i++];
                    l.weight=std::stod(x[i++]); l.target_shares=std::stod(x[i++]); l.filled_shares=std::stod(x[i++]);
                    l.limit_price=std::stod(x[i++]); l.queue_ahead=std::stod(x[i++]); l.arrival_ms=std::stoll(x[i++]);
                    l.cancel_effective_ms=std::stoll(x[i++]); l.replace_count=std::stoi(x[i++]);
                    l.entry_cash=std::stod(x[i++]); l.entry_fee=std::stod(x[i++]); l.exit_cash=std::stod(x[i++]);
                    l.exit_fee=std::stod(x[i++]); l.slippage_cost=std::stod(x[i++]); l.first_fill_ts=std::stoll(x[i++]);
                    l.last_fill_ts=std::stoll(x[i++]); l.adverse_mark_pnl=std::stod(x[i++]);
                    l.adverse_recorded=std::stoi(x[i++])!=0; l.order_state=x[i++];
                    legs_.push_back(std::move(l));
                } catch (...) {
                }
            }
        }
        if (cash_ <= 0.0 && legs_.empty()) cash_ = cfg_.starting_capital;
        if (peak_equity_ <= 0.0) peak_equity_ = std::max(cash_, cfg_.starting_capital);
    }

    void persist_state() const {
        {
            std::ofstream f(p("multileg_risk.csv"));
            f << "cash,peak_equity,killed,tape_cursor\n" << std::setprecision(15) << cash_ << ',' << peak_equity_ << ','
              << (killed_?1:0) << ',' << tape_cursor_ << '\n';
        }
        {
            std::ofstream f(p("multileg_bundles.csv"));
            f << "bundle_id,strategy,event_id,status,created_ts,expected_edge,max_notional,execution_deadline_ts,hold_deadline_ts,ledger_written,abort_reason\n";
            for (const auto& [id,b] : bundles_) {
                (void)id;
                f << csv_escape(b.bundle_id) << ',' << b.strategy << ',' << csv_escape(b.event_id) << ',' << b.status << ','
                  << b.created_ts << ',' << b.expected_edge << ',' << b.max_notional << ',' << b.execution_deadline_ts << ','
                  << b.hold_deadline_ts << ',' << (b.ledger_written?1:0) << ',' << csv_escape(b.abort_reason) << '\n';
            }
        }
        {
            std::ofstream f(p("multileg_legs.csv"));
            f << "bundle_id,market_id,event_id,side,token_id,weight,target_shares,filled_shares,limit_price,queue_ahead,arrival_ms,cancel_effective_ms,replace_count,entry_cash,entry_fee,exit_cash,exit_fee,slippage_cost,first_fill_ts,last_fill_ts,adverse_mark_pnl,adverse_recorded,order_state\n";
            for (const auto& l : legs_) {
                f << csv_escape(l.bundle_id) << ',' << l.market_id << ',' << csv_escape(l.event_id) << ',' << l.side << ',' << l.token_id << ','
                  << l.weight << ',' << l.target_shares << ',' << l.filled_shares << ',' << l.limit_price << ',' << l.queue_ahead << ','
                  << l.arrival_ms << ',' << l.cancel_effective_ms << ',' << l.replace_count << ',' << l.entry_cash << ',' << l.entry_fee << ','
                  << l.exit_cash << ',' << l.exit_fee << ',' << l.slippage_cost << ',' << l.first_fill_ts << ',' << l.last_fill_ts << ','
                  << l.adverse_mark_pnl << ',' << (l.adverse_recorded?1:0) << ',' << l.order_state << '\n';
            }
        }
    }

    std::vector<IntentLeg> read_intents() const {
        std::vector<IntentLeg> out;
        std::ifstream f(intents_path_);
        std::string line;
        std::getline(f, line);
        while (std::getline(f, line)) {
            auto x = split_csv(line);
            if (x.size() < 13) continue;
            try {
                IntentLeg q;
                q.bundle_id=x[0]; q.strategy=x[1]; q.event_id=x[2]; q.created_ts=std::stoll(x[3]); q.mode=x[4];
                q.expected_edge=std::stod(x[5]); q.max_notional=std::stod(x[6]); q.market_id=x[7]; q.side=x[8];
                q.weight=std::stod(x[9]); q.limit_price=std::stod(x[10]); q.execution_deadline_ts=std::stoll(x[11]);
                q.hold_deadline_ts=std::stoll(x[12]);
                if (!q.bundle_id.empty() && q.weight>0.0 && (q.side=="YES" || q.side=="NO")) out.push_back(std::move(q));
            } catch (...) {
            }
        }
        return out;
    }

    std::string bundle_signature(const std::string& strategy, const std::vector<IntentLeg>& v) const {
        std::vector<std::string> ids;
        for (const auto& x : v) ids.push_back(x.market_id + ":" + x.side);
        std::sort(ids.begin(), ids.end());
        std::ostringstream os;
        os << strategy;
        for (const auto& x : ids) os << '|' << x;
        return os.str();
    }

    std::unordered_set<std::string> live_signatures() const {
        std::unordered_set<std::string> out;
        for (const auto& [id,b] : bundles_) {
            if (!pm::bundle_has_live_risk(b.status)) continue;
            std::vector<std::string> ids;
            for (const auto& l : legs_) if (l.bundle_id==id) ids.push_back(l.market_id+":"+l.side);
            std::sort(ids.begin(), ids.end());
            std::ostringstream os; os<<b.strategy; for (const auto& x:ids) os<<'|'<<x;
            out.insert(os.str());
        }
        return out;
    }

    pm::Market* market_for(const std::string& market_id) {
        auto it=market_cache_.find(market_id);
        if (it!=market_cache_.end()) return &it->second;
        try {
            auto m=api_.fetch_market_by_id(market_id);
            if (!m) return nullptr;
            market_cache_[market_id]=*m;
            return &market_cache_[market_id];
        } catch (...) { return nullptr; }
    }

    pm::FeeDetails fee_for(const pm::Market& m) {
        auto it=fee_cache_.find(m.condition_id);
        if (it!=fee_cache_.end()) return it->second;
        auto fd=api_.fetch_fee_details(m);
        fee_cache_[m.condition_id]=fd;
        return fd;
    }

    void load_new_intents(std::int64_t now) {
        if (killed_) return;
        const auto all=read_intents();
        std::map<std::string,std::vector<IntentLeg>> grouped;
        for (const auto& x:all) grouped[x.bundle_id].push_back(x);
        auto sigs=live_signatures();
        for (auto& [id,v]:grouped) {
            if (bundles_.count(id) || v.empty()) continue;
            const auto& h=v.front();
            if (h.mode!="MAKER" || h.expected_edge<=min_edge_ || h.max_notional<=0.0 || h.execution_deadline_ts<=now) continue;
            const auto sig=bundle_signature(h.strategy,v);
            if (sigs.count(sig)) continue;

            std::vector<pm::Market*> ms;
            std::vector<std::string> tokens;
            bool ok=true;
            for (const auto& q:v) {
                auto* m=market_for(q.market_id);
                if (!m || !m->active || m->closed || !m->enable_order_book || !m->accepting_orders) { ok=false; break; }
                ms.push_back(m);
                tokens.push_back(q.side=="YES"?m->yes_token:m->no_token);
            }
            if (!ok) continue;
            auto books=api_.fetch_books(tokens);
            double capital_per_unit=0.0;
            std::vector<double> limits;
            limits.reserve(v.size());
            for (std::size_t i=0;i<v.size();++i) {
                auto bi=books.find(tokens[i]);
                if (bi==books.end()) { ok=false; break; }
                double limit=v[i].limit_price>0.0?v[i].limit_price:bi->second.best_bid();
                const double ask=bi->second.best_ask();
                if (!std::isfinite(limit)||!std::isfinite(ask)||limit<=0.0||limit>=ask-1e-12) {ok=false;break;}
                limits.push_back(limit);
                capital_per_unit += v[i].weight*limit;
            }
            if (!ok||capital_per_unit<=1e-9) continue;
            const double eq=std::max(1.0,cash_+gross_entry_cash());
            const double current_dd=std::max(0.0,peak_equity_-eq);
            const double loss_room=cfg_.max_drawdown*std::max(peak_equity_,eq)-current_dd-gross_entry_cash()-reserved_cash();
            const double max_bundle=std::min({h.max_notional,cfg_.max_trade_usd,
                cfg_.max_gross_fraction*eq-gross_entry_cash()-reserved_cash(),cash_-reserved_cash(),std::max(0.0,loss_room)});
            if (max_bundle<=0.0) continue;
            double units=max_bundle/capital_per_unit;
            std::unordered_map<std::string,double> event_per_unit;
            for (std::size_t i=0;i<v.size();++i) {
                const auto& b=books.at(tokens[i]);
                const double leg_per_unit=v[i].weight*limits[i];
                const double market_room=cfg_.max_market_fraction*eq-market_committed(v[i].market_id);
                if(leg_per_unit>1e-12) units=std::min(units,std::max(0.0,market_room)/leg_per_unit);
                event_per_unit[ms[i]->event_id]+=leg_per_unit;
                units=std::min(units,std::max(0.0,0.25*std::max(1.0,touch_size_at(b,limits[i],true))/v[i].weight));
            }
            for(const auto&[event,per_unit]:event_per_unit){
                const double room=cfg_.max_event_fraction*eq-event_committed(event);
                if(per_unit>1e-12) units=std::min(units,std::max(0.0,room)/per_unit);
            }
            for (std::size_t i=0;i<v.size();++i) {
                const auto& b=books.at(tokens[i]);
                if (units*v[i].weight + 1e-9 < b.min_order_size) {ok=false;break;}
            }
            if (!ok||units<=0.0) continue;
            const double reserved=units*capital_per_unit;
            if (reserved<=0.0||reserved>cash_-reserved_cash()+1e-9) continue;

            BundleState b;
            b.bundle_id=id; b.strategy=h.strategy; b.event_id=h.event_id; b.status="RESTING"; b.created_ts=h.created_ts;
            b.expected_edge=h.expected_edge; b.max_notional=reserved; b.execution_deadline_ts=h.execution_deadline_ts;
            b.hold_deadline_ts=h.hold_deadline_ts;
            bundles_[id]=b;
            for (std::size_t i=0;i<v.size();++i) {
                LegState l;
                l.bundle_id=id; l.market_id=v[i].market_id; l.event_id=ms[i]->event_id; l.side=v[i].side; l.token_id=tokens[i];
                l.weight=v[i].weight; l.target_shares=units*v[i].weight; l.limit_price=limits[i];
                l.queue_ahead=touch_size_at(books.at(tokens[i]),l.limit_price,true);
                l.arrival_ms=now_ms()+submit_latency_ms_; l.order_state="RESTING";
                legs_.push_back(l);
                append_event("POST",id,&legs_.back(),0.0,l.limit_price,"queue_ahead="+std::to_string(l.queue_ahead));
            }
            sigs.insert(sig);
        }
    }

    void resolve_live_markets(std::unordered_map<std::string,pm::Market>& markets,
                              std::unordered_map<std::string,std::string>& token_market,
                              std::vector<std::string>& tokens) {
        std::unordered_set<std::string> uniq;
        for (auto& l:legs_) {
            auto bit=bundles_.find(l.bundle_id);
            if (bit==bundles_.end() || !pm::bundle_has_live_risk(bit->second.status)) continue;
            auto* m=market_for(l.market_id);
            if (!m) continue;
            markets[l.market_id]=*m;
            token_market[l.token_id]=l.market_id;
            if (uniq.insert(l.token_id).second) tokens.push_back(l.token_id);
        }
    }

    std::vector<TapeTrade> read_new_tape() {
        std::vector<TapeTrade> out;
        std::ifstream f(tape_path_);
        std::string line;
        if (!std::getline(f,line)) return out;
        std::size_t idx=0;
        while (std::getline(f,line)) {
            if (idx++ < tape_cursor_) continue;
            auto x=split_csv(line);
            if (x.size()<12) continue;
            try {
                TapeTrade t;
                t.ts=std::stoll(x[0]); t.asset_id=x[4]; t.side=x[6]; t.price=std::stod(x[7]); t.size=std::stod(x[8]);
                if (t.ts>0&&!t.asset_id.empty()&&t.size>0.0) out.push_back(t);
            } catch (...) {}
        }
        tape_cursor_=idx;
        std::sort(out.begin(),out.end(),[](const auto&a,const auto&b){return a.ts<b.ts;});
        return out;
    }

    void apply_trades(const std::vector<TapeTrade>& trades,
                      const std::unordered_map<std::string,pm::Book>& books,
                      const std::unordered_map<std::string,pm::Market>& markets,
                      std::int64_t nowm) {
        (void)books; (void)markets; (void)nowm;
        for (const auto& t:trades) {
            if (t.side!="SELL") continue; // taker sell can consume our resting bid
            const auto trade_ms=t.ts*1000;
            for (auto& l:legs_) {
                auto bit=bundles_.find(l.bundle_id);
                if (bit==bundles_.end() || !pm::bundle_has_live_risk(bit->second.status)) continue;
                if (l.token_id!=t.asset_id) continue;
                if (!pm::passive_buy_active_for_trade(l.order_state,trade_ms,l.arrival_ms,l.cancel_effective_ms)) continue;
                if (t.price>l.limit_price+1e-9) continue;
                const auto qfill=pm::consume_passive_buy(l.queue_ahead,l.remaining(),l.limit_price,t.price,t.size,true);
                l.queue_ahead=qfill.queue_ahead;
                if(qfill.fill_shares<=1e-12) continue;
                const double fill=qfill.fill_shares;
                const double cost=fill*l.limit_price;
                if (cost>cash_+1e-9) {
                    abort_bundle(l.bundle_id,"capital_during_fill");
                    continue;
                }
                cash_-=cost;
                l.filled_shares+=fill;
                l.entry_cash+=cost;
                if (l.first_fill_ts==0) l.first_fill_ts=t.ts;
                l.last_fill_ts=t.ts;
                append_event("PARTIAL_FILL",l.bundle_id,&l,fill,l.limit_price,"trade_price="+std::to_string(t.price));
                if (l.remaining()<=1e-9) l.order_state="FILLED";
            }
        }
    }

    void request_cancel(LegState& l, std::int64_t nowm, const std::string& reason) {
        if (l.order_state!="RESTING") return;
        l.order_state="CANCEL_PENDING";
        l.cancel_effective_ms=nowm+cancel_latency_ms_;
        append_event("CANCEL_REQUEST",l.bundle_id,&l,0.0,l.limit_price,reason);
    }

    void manage_orders(const std::unordered_map<std::string,pm::Book>& books,
                       const std::unordered_map<std::string,pm::Market>& markets,
                       std::int64_t nowm) {
        (void)markets;
        const auto now=nowm/1000;
        for (auto& l:legs_) {
            auto bit=bundles_.find(l.bundle_id);
            if (bit==bundles_.end()) continue;
            auto& b=bit->second;

            if (b.status!="RESTING" && l.order_state=="RESTING") {
                request_cancel(l,nowm,"bundle_not_resting");
            }
            if ((killed_||now>=b.execution_deadline_ts) && l.order_state=="RESTING") {
                request_cancel(l,nowm,killed_?"kill":"execution_deadline");
            }

            const auto bk=books.find(l.token_id);
            if (l.order_state=="RESTING" && bk!=books.end()) {
                const double bb=bk->second.best_bid();
                const double tick=std::max(1e-6,bk->second.tick_size);
                if (std::isfinite(bb)&&std::abs(bb-l.limit_price)>=0.75*tick) request_cancel(l,nowm,"reprice");
            }

            if (l.order_state=="CANCEL_PENDING"&&nowm>=l.cancel_effective_ms) {
                append_event("CANCEL_EFFECTIVE",l.bundle_id,&l,0.0,l.limit_price,"remaining="+std::to_string(l.remaining()));
                const bool may_replace=b.status=="RESTING"&&!killed_&&now<b.execution_deadline_ts&&
                    l.remaining()>1e-9&&l.replace_count<max_replaces_;
                if (may_replace && bk!=books.end()) {
                    const double bb=bk->second.best_bid(), ask=bk->second.best_ask();
                    if (std::isfinite(bb)&&std::isfinite(ask)&&bb>0.0&&bb<ask-1e-12) {
                        l.limit_price=bb; l.queue_ahead=touch_size_at(bk->second,bb,true);
                        l.arrival_ms=nowm+submit_latency_ms_; l.cancel_effective_ms=0; ++l.replace_count; l.order_state="RESTING";
                        append_event("REPLACE",l.bundle_id,&l,0.0,l.limit_price,"priority_reset");
                    } else {
                        l.order_state="CANCELLED";
                    }
                } else {
                    l.order_state="CANCELLED";
                }
            }
        }
    }

    std::vector<LegState*> bundle_legs(const std::string& id) {
        std::vector<LegState*> out;
        for (auto& l:legs_) if (l.bundle_id==id) out.push_back(&l);
        return out;
    }

    double completion(const std::string& id) {
        auto ls=bundle_legs(id);
        std::vector<std::pair<double,double>> ft;
        ft.reserve(ls.size());
        for(auto*l:ls) ft.push_back({l->filled_shares,l->target_shares});
        return pm::minimum_completion(ft);
    }

    double bundle_leg_risk(const std::string& id, const std::unordered_map<std::string,pm::Book>& books,
                           const std::unordered_map<std::string,pm::Market>& markets) {
        double loss=0.0;
        for (auto* l:bundle_legs(id)) {
            if (l->filled_shares<=1e-12) continue;
            auto bk=books.find(l->token_id); auto mi=markets.find(l->market_id);
            if (bk==books.end()||mi==markets.end()) { loss+=l->entry_cash; continue; }
            const double bid=bk->second.best_bid();
            if (!std::isfinite(bid)) {loss+=l->entry_cash;continue;}
            auto fd=fee_for(mi->second);
            const double px=bid*(1.0-cfg_.slippage_bps/10000.0);
            const double proceeds=l->filled_shares*px-pm::Engine::protocol_fee(l->filled_shares,px,fd);
            loss+=std::max(0.0,l->entry_cash-proceeds);
        }
        return loss;
    }

    void abort_bundle(const std::string& id, const std::string& reason) {
        auto it=bundles_.find(id); if(it==bundles_.end()) return;
        if(it->second.status=="CLOSED"||it->second.status=="UNWOUND") return;
        it->second.status="ABORTING"; it->second.abort_reason=reason;
        append_event("ABORT",id,nullptr,0.0,0.0,reason);
    }

    bool all_orders_inactive(const std::string& id) {
        for (auto* l:bundle_legs(id)) if (l->order_state=="RESTING"||l->order_state=="CANCEL_PENDING") return false;
        return true;
    }

    bool exit_bundle(const std::string& id, const std::unordered_map<std::string,pm::Book>& books,
                     const std::unordered_map<std::string,pm::Market>& markets, const std::string& final_status) {
        auto ls=bundle_legs(id);
        struct R {LegState* l; SellResult r;};
        std::vector<R> sells;
        for (auto* l:ls) {
            const double open=std::max(0.0,l->filled_shares-(l->exit_cash>0.0?l->filled_shares:0.0));
            // Each leg exits exactly once. exit_cash > 0 is the durable marker.
            if (l->filled_shares<=1e-12||l->exit_cash>0.0) continue;
            auto bk=books.find(l->token_id); auto mi=markets.find(l->market_id);
            if (bk==books.end()||mi==markets.end()) return false;
            auto r=sell_all(bk->second,l->filled_shares,cfg_.slippage_bps,fee_for(mi->second));
            if (!r) return false;
            sells.push_back({l,*r});
            (void)open;
        }
        for (auto& s:sells) {
            const double raw_cash=s.r.shares*s.r.raw_avg;
            const double slipped_cash=s.r.shares*s.r.slipped_avg;
            s.l->exit_cash=slipped_cash-s.r.fee;
            s.l->exit_fee=s.r.fee;
            s.l->slippage_cost=raw_cash-slipped_cash;
            cash_+=s.l->exit_cash;
            s.l->order_state="DONE";
            append_event("EXIT_TAKER",id,s.l,s.r.shares,s.r.slipped_avg,final_status);
        }
        auto bit=bundles_.find(id);
        if(bit!=bundles_.end()) bit->second.status=final_status;
        write_ledger_if_final(id);
        return true;
    }

    void manage_bundles(const std::unordered_map<std::string,pm::Book>& books,
                        const std::unordered_map<std::string,pm::Market>& markets,
                        std::int64_t now, std::int64_t nowm) {
        std::vector<std::string> ids; ids.reserve(bundles_.size()); for(auto&[id,b]:bundles_){(void)b;ids.push_back(id);}
        for(const auto&id:ids){
            auto& b=bundles_.at(id);
            if(b.status=="CLOSED"||b.status=="UNWOUND"||b.status=="CANCELLED") continue;
            if(b.status=="RESTING"){
                const double c=completion(id);
                if(c>=completion_threshold_){
                    b.status="COMPLETE";
                    append_event("BUNDLE_COMPLETE",id,nullptr,0.0,0.0,"completion="+std::to_string(c));
                    for(auto*l:bundle_legs(id)) if(l->order_state=="RESTING") request_cancel(*l,nowm,"completion_residual");
                } else {
                    const double leg_risk=bundle_leg_risk(id,books,markets);
                    if((max_leg_risk_usd_>0.0&&leg_risk>max_leg_risk_usd_)||now>=b.execution_deadline_ts||killed_){
                        abort_bundle(id,killed_?"drawdown_kill":(now>=b.execution_deadline_ts?"execution_timeout":"leg_risk"));
                    }
                }
            }
            if(b.status=="ABORTING"){
                for(auto*l:bundle_legs(id)) if(l->order_state=="RESTING") request_cancel(*l,nowm,"abort");
                if(all_orders_inactive(id)) exit_bundle(id,books,markets,"UNWOUND");
            } else if(b.status=="COMPLETE"&&now>=b.hold_deadline_ts){
                for(auto*l:bundle_legs(id)) if(l->order_state=="RESTING") request_cancel(*l,nowm,"hold_exit");
                if(all_orders_inactive(id)) exit_bundle(id,books,markets,"CLOSED");
            }
        }
    }

    void measure_adverse_selection(const std::unordered_map<std::string,pm::Book>& books,std::int64_t now){
        for(auto&l:legs_){
            if(l.adverse_recorded||l.filled_shares<=1e-12||l.last_fill_ts<=0||now-l.last_fill_ts<adverse_horizon_s_) continue;
            auto bk=books.find(l.token_id); if(bk==books.end()) continue;
            const double bid=bk->second.best_bid(); if(!std::isfinite(bid)) continue;
            l.adverse_mark_pnl=l.filled_shares*(bid-l.entry_avg()); l.adverse_recorded=true;
            append_event("ADVERSE_MARK",l.bundle_id,&l,l.filled_shares,bid,"pnl="+std::to_string(l.adverse_mark_pnl));
        }
    }

    void write_ledger_if_final(const std::string&id){
        auto it=bundles_.find(id); if(it==bundles_.end()) return; auto&b=it->second;
        if(b.ledger_written||(b.status!="CLOSED"&&b.status!="UNWOUND"&&b.status!="CANCELLED")) return;
        double entry=0.0,exit=0.0,fees=0.0,slip=0.0,adverse=0.0; double fill=1.0; bool any=false;
        for(auto*l:bundle_legs(id)){
            entry+=l->entry_cash; exit+=l->exit_cash; fees+=l->entry_fee+l->exit_fee; slip+=l->slippage_cost; adverse+=l->adverse_mark_pnl;
            fill=std::min(fill,l->fill_fraction()); any=true;
        }
        if(!any) fill=0.0;
        const double gross=(exit+fees+slip)-entry;
        const double net=exit-entry;
        const double roc=entry>1e-12?net/entry:0.0;
        std::ofstream f(p("bundle_ledger.csv"),std::ios::app);
        f<<csv_escape(b.bundle_id)<<','<<b.strategy<<','<<csv_escape(b.event_id)<<','<<b.created_ts<<','<<now_s()<<','<<b.status<<','
         <<b.expected_edge<<','<<b.max_notional<<','<<entry<<','<<gross<<','<<fees<<','<<slip<<','<<net<<','<<roc<<','<<fill<<','<<adverse<<','<<csv_escape(b.abort_reason)<<'\n';
        b.ledger_written=true;
    }

    double market_committed(const std::string& market_id) const{
        double x=0.0;
        for(const auto&l:legs_){
            if(l.market_id!=market_id) continue;
            auto it=bundles_.find(l.bundle_id);
            if(it==bundles_.end()||it->second.status=="CLOSED"||it->second.status=="UNWOUND"||it->second.status=="CANCELLED") continue;
            x+=l.entry_cash;
            if(l.order_state=="RESTING"||l.order_state=="CANCEL_PENDING") x+=l.remaining()*l.limit_price;
        }
        return x;
    }
    double event_committed(const std::string& event_id) const{
        double x=0.0;
        for(const auto&l:legs_){
            if(l.event_id!=event_id) continue;
            auto it=bundles_.find(l.bundle_id);
            if(it==bundles_.end()||it->second.status=="CLOSED"||it->second.status=="UNWOUND"||it->second.status=="CANCELLED") continue;
            x+=l.entry_cash;
            if(l.order_state=="RESTING"||l.order_state=="CANCEL_PENDING") x+=l.remaining()*l.limit_price;
        }
        return x;
    }

    double reserved_cash() const{
        double x=0.0;
        for(const auto&l:legs_){
            auto it=bundles_.find(l.bundle_id);
            if(it==bundles_.end()||it->second.status=="CLOSED"||it->second.status=="UNWOUND"||it->second.status=="CANCELLED") continue;
            if(l.order_state=="RESTING"||l.order_state=="CANCEL_PENDING") x+=l.remaining()*l.limit_price;
        }
        return x;
    }
    double gross_entry_cash() const{double x=0.0;for(const auto&l:legs_) if(l.exit_cash<=0.0)x+=l.entry_cash;return x;}
    double equity(const std::unordered_map<std::string,pm::Book>& books) const{
        double e=cash_;
        for(const auto&l:legs_){
            if(l.filled_shares<=1e-12||l.exit_cash>0.0) continue;
            auto it=books.find(l.token_id); const double bid=it!=books.end()?it->second.best_bid():l.entry_avg();
            e+=l.filled_shares*(std::isfinite(bid)?bid:l.entry_avg());
        }
        return e;
    }
    double drawdown(const std::unordered_map<std::string,pm::Book>&books) const{return peak_equity_>0.0?std::max(0.0,1.0-equity(books)/peak_equity_):0.0;}
    void update_risk(const std::unordered_map<std::string,pm::Book>&books){
        const double eq=equity(books); peak_equity_=std::max(peak_equity_,eq); if(peak_equity_>0.0&&1.0-eq/peak_equity_>=cfg_.max_drawdown) killed_=true;
        if(killed_) for(auto&[id,b]:bundles_) if(b.status=="RESTING"||b.status=="COMPLETE") abort_bundle(id,"drawdown_kill");
    }
    void append_equity(std::int64_t ts,const std::unordered_map<std::string,pm::Book>&books) const{
        std::size_t live=0;for(const auto&[id,b]:bundles_){(void)id;if(pm::bundle_has_live_risk(b.status))++live;}
        std::ofstream f(p("multileg_equity.csv"),std::ios::app);
        f<<ts<<','<<cash_<<','<<equity(books)<<','<<reserved_cash()<<','<<gross_entry_cash()<<','<<peak_equity_<<','<<drawdown(books)<<','<<(killed_?1:0)<<','<<live<<'\n';
    }
};

} // namespace

int main(int argc,char**argv){
    try{
        std::string config="config/paper_v3.json",run_dir="runs/paper_v4",intents="runs/paper_v4/intents.csv",tape="runs/paper_v4/trade_tape.csv";
        double min_edge=0.001,completion=0.95,max_leg_risk=5.0; int submit_latency=250,cancel_latency=250,max_replaces=3,adverse_horizon=60,interval=10; bool loop=false;
        for(int i=1;i<argc;++i){const std::string a=argv[i];auto next=[&](){if(i+1>=argc)throw std::runtime_error("Missing value after "+a);return std::string(argv[++i]);};
            if(a=="--config")config=next();else if(a=="--run-dir")run_dir=next();else if(a=="--intents")intents=next();else if(a=="--trade-tape")tape=next();
            else if(a=="--min-edge")min_edge=std::stod(next());else if(a=="--completion-threshold")completion=std::stod(next());
            else if(a=="--submit-latency-ms")submit_latency=std::stoi(next());else if(a=="--cancel-latency-ms")cancel_latency=std::stoi(next());
            else if(a=="--max-replaces")max_replaces=std::stoi(next());else if(a=="--max-leg-risk-usd")max_leg_risk=std::stod(next());
            else if(a=="--adverse-horizon-seconds")adverse_horizon=std::stoi(next());else if(a=="--interval")interval=std::stoi(next());
            else if(a=="--loop")loop=true;else if(a=="--once")loop=false;else if(a=="--help"||a=="-h"){
                std::cout<<"polymarket_multileg_paper [--config FILE] [--run-dir DIR] [--intents FILE] [--trade-tape FILE] "
                            "[--min-edge X] [--completion-threshold X] [--submit-latency-ms N] [--cancel-latency-ms N] "
                            "[--max-replaces N] [--max-leg-risk-usd X] [--adverse-horizon-seconds N] [--interval N] [--once|--loop]\\n";return 0;
            }else throw std::runtime_error("Unknown argument: "+a);
        }
        auto cfg=pm::Engine::load_config(config); MultiLegPaper broker(cfg,run_dir,intents,tape,min_edge,completion,submit_latency,cancel_latency,max_replaces,max_leg_risk,adverse_horizon);
        do{broker.tick();if(loop)std::this_thread::sleep_for(std::chrono::seconds(std::max(1,interval)));}while(loop); return 0;
    }catch(const std::exception&e){std::cerr<<"fatal: "<<e.what()<<'\n';return 1;}
}
