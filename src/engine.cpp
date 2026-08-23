#include "poly/engine.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <set>
#include <sstream>
#include <unordered_set>

namespace poly {
namespace {

std::string now_iso() {
    auto n = std::chrono::system_clock::now();
    auto t = std::chrono::system_clock::to_time_t(n);
    std::tm tm{};
#ifdef _WIN32
    gmtime_s(&tm, &t);
#else
    gmtime_r(&t, &tm);
#endif
    std::ostringstream os;
    os << std::put_time(&tm, "%Y-%m-%dT%H:%M:%SZ");
    return os.str();
}

FeeInfo effective_fee(const Market& m, const EngineConfig& cfg) {
    if (m.fees.live) return m.fees;
    return {cfg.fallback_taker_fee_rate, cfg.fallback_taker_fee_exponent, false, true};
}

bool final_resolution_visible(const Market& m) {
    if (m.resolution_status == "resolved" || m.resolution_status == "settled") return true;
    if (m.resolution_status == "requested" || m.resolution_status == "proposed" || m.resolution_status == "disputed") return false;
    // Some automatically-resolved markets do not expose UMA state.  In that
    // case require both closure and an effectively binary terminal price.
    return m.closed && (m.gamma_yes_price <= 0.001 || m.gamma_yes_price >= 0.999);
}

void apply_resolution_to_model(UniversalModel& model, const Market& m) {
    if (m.gamma_yes_price >= 0.999) model.observe_resolution(m.id, true);
    else if (m.gamma_yes_price <= 0.001) model.observe_resolution(m.id, false);
    else model.forget_prediction(m.id); // void/invalid/non-binary payout: no Brier target
}

} // namespace

QuantEngine::QuantEngine(EngineConfig c)
    : cfg_(c), client_(), model_(c), risk_(c), broker_(c.initial_capital), last_equity_(c.initial_capital) {
    std::filesystem::create_directories("runs");
    model_.load_history("runs/history.csv");
    model_.load_state("runs/model_state.csv");
    broker_.load_state("runs/broker_state.csv");
    risk_.load_state("runs/risk_state.csv");
    last_equity_ = broker_.cash();
}

void QuantEngine::set_external_csv(const std::string& p) { external_.load_csv(p); }

TradeIdea QuantEngine::make_idea(const LiveMarket& m, const FairValue& f) const {
    const double q_yes = f.probability, q_no = 1.0 - q_yes;
    const double yes_ask = m.yes_book.best_ask, no_ask = m.no_book.best_ask;
    TradeIdea i;
    i.market_id = m.market.id;
    i.question = m.market.question;
    i.uncertainty = f.uncertainty;
    if (q_yes - yes_ask >= q_no - no_ask) {
        i.token_id = m.market.yes_token;
        i.outcome = "YES";
        i.fair_probability = q_yes;
        i.entry_price = yes_ask;
        i.raw_edge = q_yes - yes_ask;
    } else {
        i.token_id = m.market.no_token;
        i.outcome = "NO";
        i.fair_probability = q_no;
        i.entry_price = no_ask;
        i.raw_edge = q_no - no_ask;
    }
    const auto fee_info = effective_fee(m.market, cfg_);
    const double fee = platform_fee_per_share(i.entry_price, fee_info.rate, fee_info.exponent);
    const double slip = i.entry_price * cfg_.slippage_bps / 10000.0;
    const double other = i.entry_price * cfg_.assumed_fee_bps / 10000.0;
    i.estimated_cost = fee + slip + other + cfg_.uncertainty_buffer * f.uncertainty;
    i.net_edge = i.raw_edge - i.estimated_cost;
    return i;
}

void QuantEngine::append_signal_log(const TradeIdea& i, const FairValue& f) const {
    (void)f;
    const bool fresh = !std::filesystem::exists("runs/signals.csv");
    std::ofstream o("runs/signals.csv", std::ios::app);
    if (fresh) o << "time,market_id,outcome,fair,entry,raw_edge,cost,net_edge,uncertainty,question\n";
    std::string q = i.question;
    std::replace(q.begin(), q.end(), ',', ';');
    o << now_iso() << ',' << i.market_id << ',' << i.outcome << ',' << i.fair_probability << ','
      << i.entry_price << ',' << i.raw_edge << ',' << i.estimated_cost << ',' << i.net_edge << ','
      << i.uncertainty << ',' << q << '\n';
}

void QuantEngine::append_fill_log(const PaperFill& f, const std::string& reason) const {
    const bool fresh = !std::filesystem::exists("runs/fills.csv");
    std::ofstream o("runs/fills.csv", std::ios::app);
    if (fresh) o << "time,action,market_id,token_id,outcome,shares,price,principal,fee,cash_after,reason\n";
    o << now_iso() << ',' << f.action << ',' << f.market_id << ',' << f.token_id << ',' << f.outcome << ','
      << f.shares << ',' << f.price << ',' << f.principal << ',' << f.fee << ',' << f.cash_after << ',' << reason << '\n';
}

void QuantEngine::write_status(const std::vector<LiveMarket>& u, double gross, double worst_case_loss) const {
    std::ofstream o("runs/status.json");
    o << "{\n"
      << "  \"time\": \"" << now_iso() << "\",\n"
      << "  \"paper\": true,\n"
      << "  \"markets\": " << u.size() << ",\n"
      << "  \"equity\": " << last_equity_ << ",\n"
      << "  \"peak_equity\": " << risk_.peak_equity() << ",\n"
      << "  \"drawdown\": " << risk_.drawdown(last_equity_) << ",\n"
      << "  \"killed\": " << (risk_.killed() ? "true" : "false") << ",\n"
      << "  \"cash\": " << broker_.cash() << ",\n"
      << "  \"gross_exposure\": " << gross << ",\n"
      << "  \"worst_case_open_loss\": " << worst_case_loss << ",\n"
      << "  \"positions\": " << broker_.positions().size() << ",\n"
      << "  \"ideas\": " << last_ideas_.size() << "\n"
      << "}\n";
}

void QuantEngine::persist_state() const {
    broker_.save_state("runs/broker_state.csv");
    risk_.save_state("runs/risk_state.csv");
    model_.save_state("runs/model_state.csv");
}

void QuantEngine::run_once() {
    auto u = client_.snapshot(cfg_.market_limit, cfg_.min_liquidity, cfg_.max_spread);
    if (u.empty() && broker_.positions().empty())
        throw std::runtime_error("no live tradable markets returned after filters");

    std::unordered_map<std::string,double> px;
    std::unordered_map<std::string,double> bid_by_token;
    std::unordered_map<std::string,std::string> m2e;
    std::unordered_map<std::string,FeeInfo> fee_by_market;
    std::unordered_map<std::string,const LiveMarket*> by_id;

    for (const auto& m : u) {
        px[m.market.yes_token] = m.yes_book.midpoint();
        px[m.market.no_token] = m.no_book.midpoint();
        bid_by_token[m.market.yes_token] = m.yes_book.best_bid;
        bid_by_token[m.market.no_token] = m.no_book.best_bid;
        m2e[m.market.id] = m.market.event_id;
        fee_by_market[m.market.id] = effective_fee(m.market, cfg_);
        by_id[m.market.id] = &m;
    }

    // Reconcile held positions even when their markets drop out of the tradable scan.
    std::set<std::string> held_market_ids;
    for (const auto& p : broker_.positions()) held_market_ids.insert(p.market_id);
    std::vector<Market> missing_active;
    std::vector<std::string> missing_tokens;
    for (const auto& mid : held_market_ids) {
        if (by_id.find(mid) != by_id.end()) continue;
        try {
            auto m = client_.get_market(mid);
            m2e[mid] = m.event_id;
            if (!m.fees.live) m.fees = client_.get_fee_info(m.condition_id);
            fee_by_market[mid] = effective_fee(m, cfg_);
            px[m.yes_token] = clamp_probability(m.gamma_yes_price);
            px[m.no_token] = clamp_probability(1.0 - m.gamma_yes_price);

            if (final_resolution_visible(m)) {
                apply_resolution_to_model(model_, m);
                for (const auto& fill : broker_.settle_market(mid, m.gamma_yes_price)) append_fill_log(fill, "resolution");
                next_resolution_check_.erase(mid);
                persist_state();
                continue;
            }
            if (!m.closed && !m.yes_token.empty() && !m.no_token.empty()) {
                missing_tokens.push_back(m.yes_token);
                missing_tokens.push_back(m.no_token);
                missing_active.push_back(std::move(m));
            }
        } catch (const std::exception& e) {
            std::cerr << "[warn] held-market reconciliation failed for " << mid << ": " << e.what() << '\n';
        }
    }

    if (!missing_tokens.empty()) {
        auto books = client_.get_books(missing_tokens);
        for (const auto& m : missing_active) {
            auto yi = books.find(m.yes_token), ni = books.find(m.no_token);
            if (yi != books.end()) {
                if (yi->second.two_sided()) px[m.yes_token] = yi->second.midpoint();
                if (!yi->second.bids.empty()) bid_by_token[m.yes_token] = yi->second.best_bid;
            }
            if (ni != books.end()) {
                if (ni->second.two_sided()) px[m.no_token] = ni->second.midpoint();
                if (!ni->second.bids.empty()) bid_by_token[m.no_token] = ni->second.best_bid;
            }
        }
    }

    model_.prepare_cycle(u);
    std::unordered_map<std::string,FairValue> fair;
    std::unordered_map<std::string,TradeIdea> idea;
    for (const auto& m : u) {
        auto f = model_.predict(m, u, external_);
        auto x = make_idea(m, f);
        fair[m.market.id] = f;
        idea[m.market.id] = x;
        append_signal_log(x, f);
    }

    // Calibrate experts on resolved markets even when we never traded them.
    // Only markets that have left the current active universe need a metadata
    // check, and those checks are rate-limited per cycle and per market.
    std::unordered_set<std::string> current_market_ids;
    for (const auto& m : u) current_market_ids.insert(m.market.id);
    const auto now_sec = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    std::size_t resolution_checks = 0;
    for (const auto& mid : model_.pending_market_ids()) {
        if (current_market_ids.count(mid) || held_market_ids.count(mid)) continue;
        auto next = next_resolution_check_.find(mid);
        if (next != next_resolution_check_.end() && next->second > now_sec) continue;
        if (resolution_checks >= cfg_.max_resolution_checks_per_cycle) break;
        ++resolution_checks;
        next_resolution_check_[mid] = now_sec + std::max<std::int64_t>(60, cfg_.resolution_check_interval_seconds);
        try {
            const auto m = client_.get_market(mid);
            if (final_resolution_visible(m)) {
                apply_resolution_to_model(model_, m);
                next_resolution_check_.erase(mid);
            }
        } catch (const std::exception& e) {
            std::cerr << "[warn] resolution calibration check failed for " << mid << ": " << e.what() << '\n';
        }
    }

    // Normal model-driven exits for positions whose market is still in the tradable universe.
    auto existing = broker_.positions();
    for (const auto& p : existing) {
        auto mi = by_id.find(p.market_id);
        if (mi == by_id.end()) continue;
        const auto& m = *mi->second;
        const auto& f = fair.at(p.market_id);
        const double side_fair = p.outcome == "YES" ? f.probability : 1.0 - f.probability;
        const double bid = p.outcome == "YES" ? m.yes_book.best_bid : m.no_book.best_bid;
        const bool opposite = idea.at(p.market_id).token_id != p.token_id && idea.at(p.market_id).net_edge >= cfg_.min_net_edge;
        const bool overvalued = side_fair + cfg_.exit_edge < bid;
        if (opposite || overvalued) {
            const auto fi = effective_fee(m.market, cfg_);
            if (auto fill = broker_.close_token(p.market_id, p.token_id, p.outcome, bid, fi.rate, fi.exponent, cfg_.slippage_bps)) {
                append_fill_log(*fill, opposite ? "opposite_signal" : "fair_below_bid");
                persist_state();
            }
        }
    }

    last_equity_ = broker_.marked_equity(px);
    risk_.update_equity(last_equity_);

    auto liquidate_if_killed = [&] {
        if (!risk_.killed()) return;
        auto open = broker_.positions();
        for (const auto& p : open) {
            auto bi = bid_by_token.find(p.token_id);
            if (bi == bid_by_token.end() || bi->second <= 0.0) continue;
            auto fi = fee_by_market.find(p.market_id);
            const FeeInfo fee = fi == fee_by_market.end()
                ? FeeInfo{cfg_.fallback_taker_fee_rate, cfg_.fallback_taker_fee_exponent, false, true}
                : fi->second;
            if (auto fill = broker_.close_token(p.market_id, p.token_id, p.outcome, bi->second, fee.rate, fee.exponent, cfg_.slippage_bps)) {
                append_fill_log(*fill, "drawdown_kill");
                persist_state();
            }
        }
        last_equity_ = broker_.marked_equity(px);
        risk_.update_equity(last_equity_);
    };
    liquidate_if_killed();

    last_ideas_.clear();
    for (const auto& m : u) {
        auto x = idea.at(m.market.id);
        if (x.net_edge >= cfg_.min_net_edge) last_ideas_.push_back(x);
    }
    std::sort(last_ideas_.begin(), last_ideas_.end(), [](const auto& a, const auto& b) {
        const double sa = a.net_edge / std::max(0.01, a.uncertainty);
        const double sb = b.net_edge / std::max(0.01, b.uncertainty);
        return sa > sb;
    });

    if (!risk_.killed()) {
        for (auto& x : last_ideas_) {
            const auto& m = *by_id.at(x.market_id);
            const double n = risk_.allowed_notional(
                x, last_equity_, broker_.gross_value(px), broker_.market_exposure(x.market_id, px),
                broker_.event_exposure(m.market.event_id, m2e, px), broker_.worst_case_loss(px));
            x.desired_notional = n;
            if (n <= 0.0) continue;
            const auto fi = effective_fee(m.market, cfg_);
            if (auto fill = broker_.buy(x, n, fi.rate, fi.exponent, cfg_.slippage_bps)) {
                append_fill_log(*fill, "entry");
                last_equity_ = broker_.marked_equity(px);
                risk_.update_equity(last_equity_);
                persist_state();
                if (risk_.killed()) break;
            }
        }
    }

    liquidate_if_killed();
    model_.update_history(u);
    last_equity_ = broker_.marked_equity(px);
    risk_.update_equity(last_equity_);
    persist_state();

    const double gross = broker_.gross_value(px);
    const double worst = broker_.worst_case_loss(px);
    write_status(u, gross, worst);

    std::cout << "\nPOLYMARKET UNIVERSAL ENGINE — PAPER ONLY\n"
              << "markets=" << u.size() << " equity=" << std::fixed << std::setprecision(2) << last_equity_
              << " cash=" << broker_.cash() << " positions=" << broker_.positions().size()
              << " gross=" << gross << " drawdown=" << 100.0 * risk_.drawdown(last_equity_)
              << "% killed=" << (risk_.killed() ? "YES" : "no") << "\n";
    std::cout << std::left << std::setw(9) << "market" << std::setw(5) << "side" << std::setw(9) << "entry"
              << std::setw(9) << "fair" << std::setw(10) << "netEdge" << std::setw(11) << "notional" << "question\n";
    for (std::size_t k = 0; k < std::min<std::size_t>(10, last_ideas_.size()); ++k) {
        const auto& i = last_ideas_[k];
        std::cout << std::setw(9) << i.market_id.substr(0,8) << std::setw(5) << i.outcome
                  << std::setw(9) << std::setprecision(4) << i.entry_price << std::setw(9) << i.fair_probability
                  << std::setw(10) << i.net_edge << std::setw(11) << std::setprecision(2) << i.desired_notional
                  << i.question.substr(0,75) << '\n';
    }
}

} // namespace poly
