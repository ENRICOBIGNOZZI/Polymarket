#include "pm/engine.hpp"

#include <boost/json.hpp>
#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace pm {
namespace json = boost::json;
namespace fs = std::filesystem;

namespace {

std::int64_t now_s() {
    return std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

double jdouble(const json::object& o, const char* key, double d) {
    auto it = o.find(key);
    if (it == o.end()) return d;
    const auto& v = it->value();
    try {
        if (v.is_double()) return v.as_double();
        if (v.is_int64()) return static_cast<double>(v.as_int64());
        if (v.is_uint64()) return static_cast<double>(v.as_uint64());
        if (v.is_string()) return std::stod(std::string(v.as_string()));
    } catch (...) {}
    return d;
}

std::size_t jsize(const json::object& o, const char* key, std::size_t d) {
    return static_cast<std::size_t>(std::max(0.0, jdouble(o, key, static_cast<double>(d))));
}

bool jbool(const json::object& o, const char* key, bool d) {
    auto it = o.find(key);
    return it != o.end() && it->value().is_bool() ? it->value().as_bool() : d;
}

std::string jstring(const json::object& o, const char* key, const std::string& d) {
    auto it = o.find(key);
    return it != o.end() && it->value().is_string() ? std::string(it->value().as_string()) : d;
}

std::vector<std::string> split_csv_line(const std::string& line) {
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

std::vector<std::string> words(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (unsigned char c : s) {
        if (std::isalnum(c)) {
            cur.push_back(static_cast<char>(std::tolower(c)));
        } else {
            if (cur.size() >= 3) out.push_back(cur);
            cur.clear();
        }
    }
    if (cur.size() >= 3) out.push_back(cur);
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}

double jaccard(const std::string& a, const std::string& b) {
    const auto x = words(a);
    const auto y = words(b);
    std::size_t i = 0, j = 0, inter = 0, uni = 0;
    while (i < x.size() || j < y.size()) {
        if (i == x.size()) { ++uni; ++j; }
        else if (j == y.size()) { ++uni; ++i; }
        else if (x[i] == y[j]) { ++inter; ++uni; ++i; ++j; }
        else if (x[i] < y[j]) { ++uni; ++i; }
        else { ++uni; ++j; }
    }
    return uni ? static_cast<double>(inter) / static_cast<double>(uni) : 0.0;
}

double sample_sd(const std::vector<double>& x, double mean) {
    if (x.size() < 2) return 0.0;
    double s = 0.0;
    for (double v : x) {
        const double d = v - mean;
        s += d * d;
    }
    return std::sqrt(s / static_cast<double>(x.size() - 1));
}

double mean(const std::vector<double>& x) {
    return x.empty() ? 0.0 : std::accumulate(x.begin(), x.end(), 0.0) / static_cast<double>(x.size());
}

double dot(const std::vector<double>& a, const std::vector<double>& b) {
    return std::inner_product(a.begin(), a.end(), b.begin(), 0.0);
}

} // namespace

Engine::Engine(Config cfg)
    : cfg_(std::move(cfg)), api_(cfg_), cash_(cfg_.starting_capital), peak_equity_(cfg_.starting_capital) {
    ensure_runtime();
    load_state();
}

Config Engine::load_config(const std::string& path) {
    Config c;
    std::ifstream f(path);
    if (!f) throw std::runtime_error("Cannot open config: " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    auto root = json::parse(ss.str());
    if (!root.is_object()) throw std::runtime_error("Config must be JSON object");
    const auto& o = root.as_object();

    c.gamma_url = jstring(o, "gamma_url", c.gamma_url);
    c.clob_url = jstring(o, "clob_url", c.clob_url);
    c.run_dir = jstring(o, "run_dir", c.run_dir);
    c.external_signals_file = jstring(o, "external_signals_file", c.external_signals_file);
    c.market_limit = jsize(o, "market_limit", c.market_limit);
    c.gamma_page_size = jsize(o, "gamma_page_size", c.gamma_page_size);
    c.books_batch_size = jsize(o, "books_batch_size", c.books_batch_size);
    c.interval_seconds = static_cast<int>(jdouble(o, "interval_seconds", c.interval_seconds));
    c.starting_capital = jdouble(o, "starting_capital", c.starting_capital);
    c.min_liquidity = jdouble(o, "min_liquidity", c.min_liquidity);
    c.min_volume24h = jdouble(o, "min_volume24h", c.min_volume24h);
    c.min_mid = jdouble(o, "min_mid", c.min_mid);
    c.max_mid = jdouble(o, "max_mid", c.max_mid);
    c.max_spread = jdouble(o, "max_spread", c.max_spread);
    c.min_net_edge = jdouble(o, "min_net_edge", c.min_net_edge);
    c.uncertainty_penalty = jdouble(o, "uncertainty_penalty", c.uncertainty_penalty);
    c.slippage_bps = jdouble(o, "slippage_bps", c.slippage_bps);
    c.fractional_kelly = jdouble(o, "fractional_kelly", c.fractional_kelly);
    c.max_trade_usd = jdouble(o, "max_trade_usd", c.max_trade_usd);
    c.max_market_fraction = jdouble(o, "max_market_fraction", c.max_market_fraction);
    c.max_event_fraction = jdouble(o, "max_event_fraction", c.max_event_fraction);
    c.max_gross_fraction = jdouble(o, "max_gross_fraction", c.max_gross_fraction);
    c.max_drawdown = jdouble(o, "max_drawdown", c.max_drawdown);
    c.history_bootstrap = jbool(o, "history_bootstrap", c.history_bootstrap);
    c.bootstrap_market_limit = jsize(o, "bootstrap_market_limit", c.bootstrap_market_limit);
    c.history_lookback_hours = jsize(o, "history_lookback_hours", c.history_lookback_hours);
    c.history_fidelity_minutes = jsize(o, "history_fidelity_minutes", c.history_fidelity_minutes);
    c.pca_window = jsize(o, "pca_window", c.pca_window);
    c.pca_min_history = jsize(o, "pca_min_history", c.pca_min_history);
    c.pca_universe = jsize(o, "pca_universe", c.pca_universe);
    c.pca_mr_strength = jdouble(o, "pca_mr_strength", c.pca_mr_strength);
    c.pca_min_residual_z = jdouble(o, "pca_min_residual_z", c.pca_min_residual_z);
    c.graph_max_sum_error = jdouble(o, "graph_max_sum_error", c.graph_max_sum_error);
    c.semantic_min_similarity = jdouble(o, "semantic_min_similarity", c.semantic_min_similarity);
    c.semantic_shrink = jdouble(o, "semantic_shrink", c.semantic_shrink);
    c.scan_only = jbool(o, "scan_only", c.scan_only);

    if (auto it = o.find("expert_weights"); it != o.end() && it->value().is_object()) {
        for (const auto& kv : it->value().as_object()) {
            const std::string name(kv.key());
            c.expert_weights[name] = jdouble(it->value().as_object(), name.c_str(), c.expert_weights[name]);
        }
    }
    if (c.min_mid >= c.max_mid) throw std::runtime_error("min_mid must be below max_mid");
    return c;
}

void Engine::ensure_runtime() {
    fs::create_directories(cfg_.run_dir);
    auto ensure = [&](const std::string& name, const std::string& header) {
        const auto p = fs::path(cfg_.run_dir) / name;
        if (!fs::exists(p) || fs::file_size(p) == 0) {
            std::ofstream f(p);
            f << header << '\n';
        }
    };
    ensure("signals.csv", "timestamp,market_id,slug,side,mid,exec_price,fair_side,fair_yes,uncertainty,fee_per_share,slippage_per_share,gross_edge,cost_adjusted_edge,net_edge,score,desired_notional,experts");
    ensure("fills.csv", "timestamp,market_id,slug,action,side,shares,price,notional,fee");
    ensure("history.csv", "timestamp,market_id,mid");
    ensure("broker_state.csv", "market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid");
    ensure("risk_state.csv", "cash,peak_equity,killed");
    ensure("expert_scores.csv", "expert,brier,count");
    ensure("forecast_state.csv", "market_id,expert,q_yes");
    ensure("arbitrage.csv", "timestamp,type,market_or_event,raw_edge,net_edge,legs");
}

void Engine::load_state() {
    {
        std::ifstream f(fs::path(cfg_.run_dir) / "risk_state.csv");
        std::string line;
        std::getline(f, line);
        if (std::getline(f, line)) {
            auto x = split_csv_line(line);
            if (x.size() >= 3) {
                try { cash_ = std::stod(x[0]); peak_equity_ = std::stod(x[1]); killed_ = std::stoi(x[2]) != 0; } catch (...) {}
            }
        }
    }
    {
        std::ifstream f(fs::path(cfg_.run_dir) / "broker_state.csv");
        std::string line;
        std::getline(f, line);
        while (std::getline(f, line)) {
            auto x = split_csv_line(line);
            if (x.size() < 9) continue;
            try {
                Position p{x[0], x[1], x[2], x[3], x[4], std::stod(x[5]), std::stod(x[6]), std::stod(x[7]), std::stod(x[8])};
                positions_[p.market_id] = std::move(p);
            } catch (...) {}
        }
    }
    {
        std::ifstream f(fs::path(cfg_.run_dir) / "history.csv");
        std::string line;
        std::getline(f, line);
        while (std::getline(f, line)) {
            auto x = split_csv_line(line);
            if (x.size() < 3) continue;
            try {
                auto& d = history_[x[1]];
                d.push_back(std::stod(x[2]));
                while (d.size() > cfg_.pca_window + 2) d.pop_front();
            } catch (...) {}
        }
    }
    {
        std::ifstream f(fs::path(cfg_.run_dir) / "expert_scores.csv");
        std::string line;
        std::getline(f, line);
        while (std::getline(f, line)) {
            auto x = split_csv_line(line);
            if (x.size() < 3) continue;
            try { expert_brier_[x[0]] = std::stod(x[1]); expert_count_[x[0]] = std::stod(x[2]); } catch (...) {}
        }
    }
    {
        std::ifstream f(fs::path(cfg_.run_dir) / "forecast_state.csv");
        std::string line;
        std::getline(f, line);
        while (std::getline(f, line)) {
            auto x = split_csv_line(line);
            if (x.size() < 3) continue;
            try { last_forecasts_[x[0]][x[1]] = std::stod(x[2]); } catch (...) {}
        }
    }
}

std::string Engine::csv_escape(const std::string& s) {
    if (s.find_first_of(",\"\n") == std::string::npos) return s;
    std::string out = "\"";
    for (char c : s) out += c == '"' ? "\"\"" : std::string(1, c);
    return out + "\"";
}

void Engine::append_signal(const Signal& s) {
    std::ofstream f(fs::path(cfg_.run_dir) / "signals.csv", std::ios::app);
    std::ostringstream ex;
    for (std::size_t i = 0; i < s.experts.size(); ++i) {
        if (i) ex << '|';
        ex << s.experts[i].name << ':' << s.experts[i].q_yes << ':' << s.experts[i].confidence;
    }
    f << now_s() << ',' << csv_escape(s.market_id) << ',' << csv_escape(s.slug) << ',' << s.side << ','
      << s.market_mid << ',' << s.executable_price << ',' << s.fair_side << ',' << s.fair_yes << ','
      << s.uncertainty << ',' << s.fee_per_share << ',' << s.slippage_per_share << ',' << s.gross_edge << ','
      << s.cost_adjusted_edge << ',' << s.net_edge << ',' << s.score << ',' << s.desired_notional << ','
      << csv_escape(ex.str()) << '\n';
}

void Engine::append_fill(const Fill& x) {
    std::ofstream f(fs::path(cfg_.run_dir) / "fills.csv", std::ios::app);
    f << x.ts << ',' << csv_escape(x.market_id) << ',' << csv_escape(x.slug) << ',' << x.action << ',' << x.side << ','
      << x.shares << ',' << x.price << ',' << x.notional << ',' << x.fee << '\n';
}

void Engine::append_history(std::int64_t ts, const Market& m, double mid) {
    std::ofstream f(fs::path(cfg_.run_dir) / "history.csv", std::ios::app);
    f << ts << ',' << csv_escape(m.id) << ',' << mid << '\n';
    auto& d = history_[m.id];
    d.push_back(mid);
    while (d.size() > cfg_.pca_window + 2) d.pop_front();
}

void Engine::bootstrap_history(const std::vector<Market>& markets) {
    if (!cfg_.history_bootstrap || cfg_.bootstrap_market_limit == 0) return;
    std::vector<const Market*> need;
    for (const auto& m : markets) {
        auto it = history_.find(m.id);
        if (it == history_.end() || it->second.empty()) need.push_back(&m);
    }
    std::sort(need.begin(), need.end(), [](auto* a, auto* b){ return a->liquidity > b->liquidity; });
    if (need.size() > cfg_.bootstrap_market_limit) need.resize(cfg_.bootstrap_market_limit);
    if (need.empty()) return;

    std::vector<std::string> tokens;
    for (auto* m : need) tokens.push_back(m->yes_token);
    const auto end = now_s();
    const auto start = end - static_cast<std::int64_t>(cfg_.history_lookback_hours) * 3600;
    try {
        auto data = api_.fetch_price_history(tokens, start, end, cfg_.history_fidelity_minutes);
        std::ofstream hf(fs::path(cfg_.run_dir) / "history.csv", std::ios::app);
        std::size_t booted = 0, points = 0;
        for (auto* m : need) {
            auto it = data.find(m->yes_token);
            if (it == data.end() || it->second.size() < 2) continue;
            auto& d = history_[m->id];
            if (!d.empty()) continue;
            const auto& v = it->second;
            const std::size_t keep = std::min<std::size_t>(v.size(), cfg_.pca_window + 1);
            for (std::size_t i = v.size() - keep; i < v.size(); ++i) {
                if (v[i].price <= 0.0 || v[i].price >= 1.0) continue;
                d.push_back(v[i].price);
                hf << v[i].ts << ',' << csv_escape(m->id) << ',' << v[i].price << '\n';
                ++points;
            }
            while (d.size() > cfg_.pca_window + 2) d.pop_front();
            if (d.size() >= 2) ++booted;
        }
        if (booted) std::cout << "history_bootstrap markets=" << booted << " points=" << points << '\n';
    } catch (const std::exception& e) {
        std::cerr << "history bootstrap failed: " << e.what() << '\n';
    }
}

FeeDetails Engine::fee_for(const Market& m) {
    auto it = fee_cache_.find(m.condition_id);
    if (it != fee_cache_.end()) return it->second;
    auto fd = api_.fetch_fee_details(m);
    fee_cache_[m.condition_id] = fd;
    return fd;
}

void Engine::persist_state(double eq, double gross) {
    peak_equity_ = std::max(peak_equity_, eq);
    if (peak_equity_ > 0.0 && 1.0 - eq / peak_equity_ >= cfg_.max_drawdown) killed_ = true;
    {
        std::ofstream f(fs::path(cfg_.run_dir) / "broker_state.csv");
        f << "market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid\n";
        for (const auto& [id, p] : positions_) {
            (void)id;
            f << csv_escape(p.market_id) << ',' << csv_escape(p.event_id) << ',' << csv_escape(p.slug) << ',' << p.side << ','
              << p.token_id << ',' << p.shares << ',' << p.avg_price << ',' << p.cost_basis << ',' << p.fees_paid << '\n';
        }
    }
    {
        std::ofstream f(fs::path(cfg_.run_dir) / "risk_state.csv");
        f << "cash,peak_equity,killed\n" << cash_ << ',' << peak_equity_ << ',' << (killed_ ? 1 : 0) << '\n';
    }
    {
        std::ofstream f(fs::path(cfg_.run_dir) / "expert_scores.csv");
        f << "expert,brier,count\n";
        for (const auto& [name, weight] : cfg_.expert_weights) {
            (void)weight;
            const double b = expert_brier_.count(name) ? expert_brier_.at(name) : 0.25;
            const double n = expert_count_.count(name) ? expert_count_.at(name) : 0.0;
            f << name << ',' << b << ',' << n << '\n';
        }
    }
    {
        std::ofstream f(fs::path(cfg_.run_dir) / "forecast_state.csv");
        f << "market_id,expert,q_yes\n";
        for (const auto& [mid, mp] : last_forecasts_) {
            for (const auto& [name, q] : mp) f << csv_escape(mid) << ',' << csv_escape(name) << ',' << q << '\n';
        }
    }
    json::object st{{"timestamp", now_s()}, {"cash", cash_}, {"equity", eq}, {"peak_equity", peak_equity_},
                    {"drawdown", peak_equity_ > 0.0 ? 1.0 - eq / peak_equity_ : 0.0}, {"gross_exposure", gross},
                    {"open_positions", positions_.size()}, {"killed", killed_}, {"mode", "paper-v2"}};
    std::ofstream f(fs::path(cfg_.run_dir) / "status.json");
    f << json::serialize(st) << '\n';
}

std::unordered_map<std::string, ExternalSignal> Engine::load_external() const {
    std::unordered_map<std::string, ExternalSignal> out;
    std::ifstream f(cfg_.external_signals_file);
    if (!f) return out;
    std::string line;
    std::getline(f, line);
    const auto now = now_s();
    while (std::getline(f, line)) {
        auto x = split_csv_line(line);
        if (x.size() < 5) continue;
        try {
            ExternalSignal s{std::stod(x[1]), std::stod(x[2]), x[3], std::stoll(x[4])};
            const double age = static_cast<double>(std::max<std::int64_t>(0, now - s.timestamp));
            s.confidence *= std::exp(-std::log(2.0) * age / (6.0 * 3600.0));
            if (s.confidence > 1e-4) out[x[0]] = s;
        } catch (...) {}
    }
    return out;
}

std::optional<std::pair<double,double>> Engine::walk_book(const Book& book, bool buy, double requested_notional) {
    if (requested_notional <= 0.0) return std::nullopt;
    auto levels = buy ? book.asks : book.bids;
    if (buy) std::sort(levels.begin(), levels.end(), [](const auto& a, const auto& b){ return a.price < b.price; });
    else std::sort(levels.begin(), levels.end(), [](const auto& a, const auto& b){ return a.price > b.price; });
    double remaining = requested_notional, shares = 0.0, cash = 0.0;
    for (const auto& level : levels) {
        if (level.price <= 0.0 || level.size <= 0.0) continue;
        const double available = level.price * level.size;
        const double take = std::min(remaining, available);
        const double q = take / level.price;
        shares += q;
        cash += q * level.price;
        remaining -= take;
        if (remaining <= 1e-9) break;
    }
    if (shares <= 0.0 || remaining > std::max(0.01, requested_notional * 0.02)) return std::nullopt;
    return std::pair{shares, cash / shares};
}

double Engine::protocol_fee(double shares, double price, const FeeDetails& fd) {
    if (shares <= 0.0 || price <= 0.0 || price >= 1.0 || fd.rate <= 0.0) return 0.0;
    return shares * fd.rate * std::pow(price * (1.0 - price), std::max(0.0, fd.exponent));
}

double Engine::full_kelly(double q, double p) {
    return p > 0.0 && p < 1.0 ? std::max(0.0, (q - p) / (1.0 - p)) : 0.0;
}

std::unordered_map<std::string,Engine::Adjustment> Engine::pca_adjustments(
    const std::vector<Market>& markets,
    const std::unordered_map<std::string,Book>& yes_books) const {

    struct Series { const Market* m = nullptr; double mu = 0.0, sd = 0.0; std::vector<double> r; };
    std::vector<const Market*> base;
    for (const auto& m : markets) {
        auto hit = history_.find(m.id);
        auto bit = yes_books.find(m.yes_token);
        if (hit == history_.end() || bit == yes_books.end()) continue;
        const double cur = bit->second.midpoint();
        if (!std::isfinite(cur) || cur <= cfg_.min_mid || cur >= cfg_.max_mid || bit->second.spread() > cfg_.max_spread) continue;
        if (hit->second.size() < cfg_.pca_min_history) continue;
        base.push_back(&m);
    }
    std::sort(base.begin(), base.end(), [](auto* a, auto* b){ return a->liquidity > b->liquidity; });
    if (base.size() > cfg_.pca_universe) base.resize(cfg_.pca_universe);
    if (base.size() < 3) return {};

    std::size_t T = cfg_.pca_window;
    for (auto* m : base) T = std::min(T, history_.at(m->id).size() - 1);
    if (T + 1 < cfg_.pca_min_history || T < 12) return {};

    std::vector<Series> s;
    for (auto* m : base) {
        Series z;
        z.m = m;
        const auto& h = history_.at(m->id);
        const std::size_t start = h.size() - (T + 1);
        z.r.reserve(T);
        for (std::size_t t = 0; t < T; ++t) {
            z.r.push_back(std::clamp(logit(h[start + t + 1]) - logit(h[start + t]), -2.0, 2.0));
        }
        z.mu = mean(z.r);
        z.sd = sample_sd(z.r, z.mu);
        if (z.sd > 1e-4) s.push_back(std::move(z));
    }
    const std::size_t N = s.size();
    if (N < 3) return {};

    std::vector<std::vector<double>> Y(T, std::vector<double>(N));
    for (std::size_t j = 0; j < N; ++j) {
        for (std::size_t t = 0; t < T; ++t) Y[t][j] = std::clamp((s[j].r[t] - s[j].mu) / s[j].sd, -4.0, 4.0);
    }
    std::vector<std::vector<double>> C(N, std::vector<double>(N));
    for (std::size_t i = 0; i < N; ++i) {
        for (std::size_t j = i; j < N; ++j) {
            double q = 0.0;
            for (std::size_t t = 0; t < T; ++t) q += Y[t][i] * Y[t][j];
            q /= static_cast<double>(T);
            C[i][j] = C[j][i] = q;
        }
    }

    std::vector<double> v(N, 1.0 / std::sqrt(static_cast<double>(N)));
    for (int it = 0; it < 50; ++it) {
        std::vector<double> w(N);
        for (std::size_t i = 0; i < N; ++i) for (std::size_t j = 0; j < N; ++j) w[i] += C[i][j] * v[j];
        const double n = std::sqrt(dot(w, w));
        if (n < 1e-12) return {};
        for (std::size_t i = 0; i < N; ++i) v[i] = w[i] / n;
    }
    std::vector<double> Cv(N);
    for (std::size_t i = 0; i < N; ++i) for (std::size_t j = 0; j < N; ++j) Cv[i] += C[i][j] * v[j];
    const double lambda1 = dot(v, Cv);
    double trace = 0.0;
    for (std::size_t i = 0; i < N; ++i) trace += C[i][i];
    const double explained = trace > 1e-12 ? std::clamp(lambda1 / trace, 0.0, 1.0) : 0.0;

    std::vector<std::vector<double>> resid(T, std::vector<double>(N));
    for (std::size_t t = 0; t < T; ++t) {
        const double factor = dot(v, Y[t]);
        for (std::size_t j = 0; j < N; ++j) resid[t][j] = Y[t][j] - v[j] * factor;
    }

    std::vector<double> current_x(N);
    for (std::size_t j = 0; j < N; ++j) {
        const auto& h = history_.at(s[j].m->id);
        const double cur = yes_books.at(s[j].m->yes_token).midpoint();
        const double dr = logit(cur) - logit(h.back());
        current_x[j] = std::clamp((dr - s[j].mu) / s[j].sd, -6.0, 6.0);
    }
    const double current_factor = dot(v, current_x);

    const std::size_t H = std::min<std::size_t>(12, std::max<std::size_t>(4, T / 4));
    std::unordered_map<std::string,Adjustment> out;
    for (std::size_t j = 0; j < N; ++j) {
        std::vector<double> states;
        std::vector<double> next_resid;
        states.reserve(T - H);
        next_resid.reserve(T - H);
        for (std::size_t t = H - 1; t + 1 < T; ++t) {
            double state = 0.0;
            for (std::size_t k = t + 1 - H; k <= t; ++k) state += resid[k][j];
            states.push_back(state);
            next_resid.push_back(resid[t + 1][j]);
        }
        if (states.size() < 8) continue;

        const double state_mu = mean(states);
        const double state_sd = std::max(0.20, sample_sd(states, state_mu));
        double state_now = current_x[j] - v[j] * current_factor;
        for (std::size_t t = T - (H - 1); t < T; ++t) state_now += resid[t][j];
        const double x_now = state_now - state_mu;
        const double z = x_now / state_sd;
        if (std::abs(z) < cfg_.pca_min_residual_z) continue;

        // Estimate whether residual dislocations actually predict reversal instead of
        // assuming every large residual will mean-revert. The target is the next
        // standardized idiosyncratic return conditional on the rolling residual state.
        const double y_mu = mean(next_resid);
        double sxx = 0.0, sxy = 0.0;
        for (std::size_t i = 0; i < states.size(); ++i) {
            const double x = states[i] - state_mu;
            const double y = next_resid[i] - y_mu;
            sxx += x * x;
            sxy += x * y;
        }
        if (sxx <= 1e-8) continue;
        const double ridge = 0.05 * static_cast<double>(states.size()) * state_sd * state_sd;
        const double beta = sxy / (sxx + ridge);
        if (beta >= -1e-4) continue;

        double rss = 0.0;
        for (std::size_t i = 0; i < states.size(); ++i) {
            const double fitted = y_mu + beta * (states[i] - state_mu);
            const double e = next_resid[i] - fitted;
            rss += e * e;
        }
        const double dof = static_cast<double>(std::max<std::size_t>(1, states.size() - 2));
        const double sigma2 = rss / dof;
        const double se_beta = std::sqrt(std::max(0.0, sigma2) / std::max(1e-8, sxx + ridge));
        const double t_reversion = se_beta > 1e-8 ? -beta / se_beta : 0.0;
        if (t_reversion < 0.75) continue;

        const double forecast_std = y_mu + beta * x_now;
        if (forecast_std * x_now >= 0.0) continue;

        const double cur = yes_books.at(s[j].m->yes_token).midpoint();
        const double forecast_logit = std::clamp(cfg_.pca_mr_strength * forecast_std * s[j].sd, -0.20, 0.20);
        if (std::abs(forecast_logit) < 1e-5) continue;
        const double q = logistic(logit(cur) + forecast_logit);
        const double z_conf = std::clamp((std::abs(z) - cfg_.pca_min_residual_z) / 3.0 + 0.20, 0.10, 1.0);
        const double sample_conf = std::min(1.0, static_cast<double>(T) / 48.0);
        const double factor_conf = std::clamp(0.35 + 1.5 * explained, 0.35, 1.0);
        const double reversion_conf = std::clamp((t_reversion - 0.75) / 2.5, 0.05, 1.0);
        out[s[j].m->id] = {q, std::clamp(z_conf * sample_conf * factor_conf * reversion_conf, 0.02, 1.0)};
    }
    return out;
}

std::unordered_map<std::string,Engine::Adjustment> Engine::graph_adjustments(
    const std::vector<Market>& markets,
    const std::unordered_map<std::string,Book>& yes_books) const {
    (void)yes_books;
    std::unordered_map<std::string,std::size_t> represented;
    for (const auto& m : markets) if (m.neg_risk) ++represented[m.event_id];

    std::unordered_map<std::string,Adjustment> out;
    for (const auto& [event_id, count] : represented) {
        if (count < 2) continue;
        try {
            auto cached = event_markets_cache_.find(event_id);
            if (cached == event_markets_cache_.end()) {
                auto full = api_.fetch_event_markets(event_id);
                cached = event_markets_cache_.emplace(event_id, std::move(full)).first;
            }
            const auto& full = cached->second;
            if (full.size() < 2) continue;

            std::vector<std::string> tokens;
            tokens.reserve(full.size());
            for (const auto& m : full) tokens.push_back(m.yes_token);
            auto books = api_.fetch_books(tokens);

            struct G { const Market* m; double p; double var; };
            std::vector<G> g;
            g.reserve(full.size());
            double sum = 0.0, varsum = 0.0;
            bool complete = true;
            for (const auto& m : full) {
                auto it = books.find(m.yes_token);
                if (it == books.end()) { complete = false; break; }
                const double p = it->second.midpoint();
                const double spread = it->second.spread();
                const double depth = it->second.weighted_depth(true) + it->second.weighted_depth(false);
                if (!std::isfinite(p) || !std::isfinite(spread)) { complete = false; break; }
                const double var = std::max(1e-6, spread * spread + 1.0 / (depth + 1.0));
                g.push_back({&m, p, var});
                sum += p;
                varsum += var;
            }
            if (!complete || g.size() != full.size() || varsum <= 0.0) continue;
            if (std::abs(sum - 1.0) > cfg_.graph_max_sum_error) continue;

            const double conf = std::clamp(1.0 - std::abs(sum - 1.0) / std::max(1e-6, cfg_.graph_max_sum_error), 0.10, 1.0);
            std::vector<double> q(g.size());
            double qsum = 0.0;
            for (std::size_t i = 0; i < g.size(); ++i) {
                q[i] = std::clamp(g[i].p + (1.0 - sum) * g[i].var / varsum, 0.001, 0.999);
                qsum += q[i];
            }
            if (qsum <= 0.0) continue;
            for (std::size_t i = 0; i < g.size(); ++i) {
                out[g[i].m->id] = {std::clamp(q[i] / qsum, 0.001, 0.999), 0.85 * conf};
            }
        } catch (const std::exception& e) {
            std::cerr << "graph event lookup failed for " << event_id << ": " << e.what() << '\n';
        }
    }
    return out;
}

std::vector<ExpertPrediction> Engine::build_experts(
    const Market& m,
    const Book& yes,
    const Book& no,
    const std::vector<Market>& universe,
    const std::unordered_map<std::string,Book>& yes_books,
    const std::unordered_map<std::string,ExternalSignal>& external,
    const std::unordered_map<std::string,Adjustment>& pca,
    const std::unordered_map<std::string,Adjustment>& graph) const {

    std::vector<ExpertPrediction> out;
    const double mid = yes.midpoint();
    if (std::isfinite(mid)) {
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
        out.push_back({"micro", std::clamp(q, 0.001, 0.999), conf});
    }
    if (auto it = pca.find(m.id); it != pca.end()) out.push_back({"pca", it->second.first, it->second.second});
    if (auto it = graph.find(m.id); it != graph.end()) out.push_back({"graph", it->second.first, it->second.second});

    if (std::isfinite(mid)) {
        double sw = 0.0, spv = 0.0;
        for (const auto& peer : universe) {
            if (peer.id == m.id) continue;
            const double sim = jaccard(m.question, peer.question);
            if (sim < cfg_.semantic_min_similarity) continue;
            auto it = yes_books.find(peer.yes_token);
            if (it == yes_books.end()) continue;
            const double p = it->second.midpoint();
            if (!std::isfinite(p)) continue;
            const double w = sim * sim * std::sqrt(std::max(1.0, peer.liquidity));
            sw += w;
            spv += w * p;
        }
        if (sw > 0.0) {
            const double peer = spv / sw;
            const double q = (1.0 - cfg_.semantic_shrink) * mid + cfg_.semantic_shrink * peer;
            out.push_back({"semantic", std::clamp(q, 0.001, 0.999), std::min(0.25, 0.05 + cfg_.semantic_shrink)});
        }
    }

    auto add_external = [&](const std::string& key) {
        auto it = external.find(key);
        if (it == external.end()) return false;
        out.push_back({"external", std::clamp(it->second.q_yes, 0.001, 0.999), std::clamp(it->second.confidence, 0.0, 1.0)});
        return true;
    };
    if (!add_external(m.id) && !add_external(m.condition_id)) add_external(m.slug);
    return out;
}

std::pair<double,double> Engine::ensemble(const std::vector<ExpertPrediction>& preds, double spread) const {
    double sw = 0.0, q = 0.0;
    for (const auto& e : preds) {
        const double base = cfg_.expert_weights.count(e.name) ? cfg_.expert_weights.at(e.name) : 0.0;
        const double n = expert_count_.count(e.name) ? expert_count_.at(e.name) : 0.0;
        const double brier = n >= 5.0 && expert_brier_.count(e.name) ? expert_brier_.at(e.name) : 0.25;
        const double w = base * std::exp(-2.0 * brier) * e.confidence;
        sw += w;
        q += w * e.q_yes;
    }
    if (sw <= 1e-12) return {0.5, 1.0};
    q /= sw;
    double v = 0.0;
    for (const auto& e : preds) {
        const double base = cfg_.expert_weights.count(e.name) ? cfg_.expert_weights.at(e.name) : 0.0;
        const double n = expert_count_.count(e.name) ? expert_count_.at(e.name) : 0.0;
        const double brier = n >= 5.0 && expert_brier_.count(e.name) ? expert_brier_.at(e.name) : 0.25;
        const double w = base * std::exp(-2.0 * brier) * e.confidence;
        v += w * (e.q_yes - q) * (e.q_yes - q);
    }
    v /= sw;
    const double u = std::sqrt(std::max(0.0, v) + 0.25 * spread * spread);
    return {std::clamp(q, 0.001, 0.999), std::clamp(u, 1e-4, 1.0)};
}

double Engine::equity(const std::unordered_map<std::string,Book>& books) const {
    double e = cash_;
    for (const auto& [id, p] : positions_) {
        (void)id;
        auto it = books.find(p.token_id);
        const double px = it != books.end() && std::isfinite(it->second.best_bid()) ? it->second.best_bid() : p.avg_price;
        e += p.shares * std::max(0.0, px);
    }
    return e;
}

double Engine::gross_exposure(const std::unordered_map<std::string,Book>& books) const {
    double g = 0.0;
    for (const auto& [id, p] : positions_) {
        (void)id;
        auto it = books.find(p.token_id);
        const double px = it != books.end() && std::isfinite(it->second.midpoint()) ? it->second.midpoint() : p.avg_price;
        g += p.shares * std::max(0.0, px);
    }
    return g;
}

double Engine::open_loss_upper_bound() const {
    double x = 0.0;
    for (const auto& [id, p] : positions_) { (void)id; x += std::max(0.0, p.cost_basis); }
    return x;
}

double Engine::market_exposure(const std::string& id) const {
    auto it = positions_.find(id);
    return it == positions_.end() ? 0.0 : it->second.cost_basis;
}

double Engine::event_exposure(const std::string& event) const {
    double x = 0.0;
    for (const auto& [id, p] : positions_) { (void)id; if (p.event_id == event) x += p.cost_basis; }
    return x;
}

double Engine::size_trade(const Signal& s, const Market& m, double eq, double gross) const {
    if (killed_ || eq <= 0.0 || s.net_edge <= cfg_.min_net_edge) return 0.0;
    const double kelly = cfg_.fractional_kelly * full_kelly(s.fair_side, s.executable_price) * eq;
    double limit = std::min({kelly, cfg_.max_trade_usd,
                             cfg_.max_market_fraction * eq - market_exposure(m.id),
                             cfg_.max_event_fraction * eq - event_exposure(m.event_id),
                             cfg_.max_gross_fraction * eq - gross,
                             cash_});
    const double dd_room = cfg_.max_drawdown * peak_equity_ - (peak_equity_ - eq) - gross;
    limit = std::min(limit, std::max(0.0, dd_room));
    return std::max(0.0, limit);
}

void Engine::paper_trade(const Signal& s, const Market& m, const Book& book, const FeeDetails& fd, double notional) {
    if (positions_.count(m.id) || notional <= 0.0) return;
    auto walk = walk_book(book, true, notional);
    if (!walk) return;
    const double shares = walk->first;
    const double px = walk->second * (1.0 + cfg_.slippage_bps / 10000.0);
    const double fee = protocol_fee(shares, px, fd);
    const double cost = shares * px + fee;
    if (cost > cash_ || shares < book.min_order_size) return;

    // Re-run the complete admission test at the actual simulated VWAP. The ranking
    // stage sees the top ask; walking the book can consume enough edge that the
    // uncertainty-adjusted trade is no longer admissible.
    const double realized_cost_edge = s.fair_side - px - fee / shares;
    const double realized_net_edge = realized_cost_edge - cfg_.uncertainty_penalty * s.uncertainty;
    if (realized_net_edge <= cfg_.min_net_edge) return;

    positions_[m.id] = Position{m.id, m.event_id, m.slug, s.side, s.side == "YES" ? m.yes_token : m.no_token,
                                shares, px, cost, fee};
    cash_ -= cost;
    append_fill({now_s(), m.id, m.slug, "BUY", s.side, shares, px, shares * px, fee});
}

void Engine::maybe_exit(const Market& m, const Book& book, double fair_yes, const FeeDetails& fd) {
    auto it = positions_.find(m.id);
    if (it == positions_.end()) return;
    auto& p = it->second;
    const double fair = p.side == "YES" ? fair_yes : 1.0 - fair_yes;
    const double bid = book.best_bid();
    if (!std::isfinite(bid)) return;

    auto levels = book.bids;
    std::sort(levels.begin(), levels.end(), [](const auto& a, const auto& b){ return a.price > b.price; });
    double remaining = p.shares, proceeds = 0.0, sold = 0.0;
    for (const auto& level : levels) {
        const double q = std::min(remaining, level.size);
        sold += q;
        proceeds += q * level.price;
        remaining -= q;
        if (remaining <= 1e-9) break;
    }
    if (sold + 1e-9 < p.shares) return;
    const double px = (proceeds / sold) * (1.0 - cfg_.slippage_bps / 10000.0);
    const double fee = protocol_fee(sold, px, fd);
    const double net_px = px - fee / sold;
    if (!killed_ && fair >= net_px - cfg_.min_net_edge * 0.5) return;

    cash_ += sold * px - fee;
    append_fill({now_s(), m.id, m.slug, "SELL", p.side, sold, px, sold * px, fee});
    positions_.erase(it);
}

void Engine::score_resolved(const std::vector<Market>& markets) {
    for (const auto& m : markets) {
        if (!m.resolved_yes) continue;
        auto fit = last_forecasts_.find(m.id);
        if (fit != last_forecasts_.end()) {
            for (const auto& [name, q] : fit->second) {
                const double loss = (q - *m.resolved_yes) * (q - *m.resolved_yes);
                const double n = expert_count_[name];
                expert_brier_[name] = n <= 0.0 ? loss : 0.98 * expert_brier_[name] + 0.02 * loss;
                expert_count_[name] = n + 1.0;
            }
        }
        auto pit = positions_.find(m.id);
        if (pit != positions_.end()) {
            const double win = pit->second.side == "YES" ? *m.resolved_yes : 1 - *m.resolved_yes;
            cash_ += pit->second.shares * win;
            append_fill({now_s(), m.id, m.slug, "SETTLE", pit->second.side, pit->second.shares, win, pit->second.shares * win, 0.0});
            positions_.erase(pit);
        }
        last_forecasts_.erase(m.id);
    }
}

void Engine::run_once(bool paper, bool scan_only) {
    auto markets = api_.discover_markets(cfg_.market_limit, cfg_.min_liquidity);
    bootstrap_history(markets);

    std::vector<Market> resolution_markets = markets;
    std::unordered_set<std::string> active_ids;
    for (const auto& m : markets) active_ids.insert(m.id);
    for (const auto& [id, p] : positions_) {
        (void)p;
        if (active_ids.count(id)) continue;
        try { if (auto closed = api_.fetch_market_by_id(id)) resolution_markets.push_back(std::move(*closed)); }
        catch (const std::exception& e) { std::cerr << "resolution lookup failed for " << id << ": " << e.what() << '\n'; }
    }
    score_resolved(resolution_markets);

    std::vector<std::string> tokens;
    tokens.reserve(markets.size() * 2 + positions_.size());
    for (const auto& m : markets) { tokens.push_back(m.yes_token); tokens.push_back(m.no_token); }
    for (const auto& [id, p] : positions_) {
        (void)id;
        if (std::find(tokens.begin(), tokens.end(), p.token_id) == tokens.end()) tokens.push_back(p.token_id);
    }
    auto books = tokens.empty() ? std::unordered_map<std::string,Book>{} : api_.fetch_books(tokens);

    std::vector<Market> tradable;
    std::unordered_map<std::string,Book> yes_books;
    for (const auto& m : markets) {
        if (m.volume24h + 1e-12 < cfg_.min_volume24h) continue;
        auto yi = books.find(m.yes_token);
        auto ni = books.find(m.no_token);
        if (yi == books.end() || ni == books.end()) continue;
        const double mid = yi->second.midpoint();
        if (!std::isfinite(mid) || mid <= cfg_.min_mid || mid >= cfg_.max_mid) continue;
        if (yi->second.spread() > cfg_.max_spread || ni->second.spread() > cfg_.max_spread) continue;
        if (!std::isfinite(yi->second.best_ask()) || !std::isfinite(ni->second.best_ask())) continue;
        tradable.push_back(m);
        yes_books[m.yes_token] = yi->second;
    }

    double eq = equity(books), gross = gross_exposure(books);
    peak_equity_ = std::max(peak_equity_, eq);
    if (peak_equity_ > 0.0 && 1.0 - eq / peak_equity_ >= cfg_.max_drawdown) killed_ = true;

    auto external = load_external();
    auto pca = pca_adjustments(tradable, yes_books);
    auto graph = graph_adjustments(tradable, yes_books);

    struct Candidate { Signal s; const Market* m; const Book* b; FeeDetails fd; };
    std::vector<Candidate> candidates;
    const auto ts = now_s();
    std::size_t gross_positive = 0, cost_positive = 0, net_positive = 0;
    double best_net = -1e9;
    std::ofstream arb(fs::path(cfg_.run_dir) / "arbitrage.csv", std::ios::app);

    for (const auto& m : tradable) {
        auto yi = books.find(m.yes_token);
        auto ni = books.find(m.no_token);
        if (yi == books.end() || ni == books.end()) continue;
        const double mid = yi->second.midpoint();
        auto preds = build_experts(m, yi->second, ni->second, tradable, yes_books, external, pca, graph);
        auto [fair, uncertainty] = ensemble(preds, std::max(yi->second.spread(), ni->second.spread()));
        last_forecasts_[m.id].clear();
        for (const auto& e : preds) last_forecasts_[m.id][e.name] = e.q_yes;
        const FeeDetails fd = fee_for(m);

        const double ay = yi->second.best_ask(), an = ni->second.best_ask();
        if (std::isfinite(ay) && std::isfinite(an)) {
            const double fy = fd.rate * std::pow(ay * (1.0 - ay), std::max(0.0, fd.exponent));
            const double fn = fd.rate * std::pow(an * (1.0 - an), std::max(0.0, fd.exponent));
            const double raw = 1.0 - ay - an;
            const double net = raw - fy - fn - (ay + an) * cfg_.slippage_bps / 10000.0;
            if (net > -0.01) arb << ts << ",PAIR," << csv_escape(m.id) << ',' << raw << ',' << net << ','
                                 << csv_escape(m.yes_token + "|" + m.no_token) << '\n';
        }

        auto make = [&](const std::string& side, const Book& book, double qside) {
            const double ask = book.best_ask();
            if (!std::isfinite(ask)) return;
            Signal s;
            s.market_id = m.id;
            s.slug = m.slug;
            s.side = side;
            s.market_mid = mid;
            s.executable_price = ask;
            s.fair_side = qside;
            s.fair_yes = fair;
            s.uncertainty = uncertainty;
            s.fee_per_share = fd.rate * std::pow(ask * (1.0 - ask), std::max(0.0, fd.exponent));
            s.slippage_per_share = ask * cfg_.slippage_bps / 10000.0;
            s.gross_edge = qside - ask;
            s.cost_adjusted_edge = s.gross_edge - s.fee_per_share - s.slippage_per_share;
            s.net_edge = s.cost_adjusted_edge - cfg_.uncertainty_penalty * uncertainty;
            s.score = s.net_edge / std::max(1e-4, uncertainty);
            s.experts = preds;
            if (s.gross_edge > 0.0) ++gross_positive;
            if (s.cost_adjusted_edge > 0.0) ++cost_positive;
            if (s.net_edge > 0.0) ++net_positive;
            best_net = std::max(best_net, s.net_edge);
            candidates.push_back({std::move(s), &m, &book, fd});
        };
        make("YES", yi->second, fair);
        make("NO", ni->second, 1.0 - fair);

        auto pit = positions_.find(m.id);
        if (pit != positions_.end()) {
            auto bit = books.find(pit->second.token_id);
            if (bit != books.end()) maybe_exit(m, bit->second, fair, fd);
        }
        append_history(ts, m, mid);
    }

    std::sort(candidates.begin(), candidates.end(), [](const auto& a, const auto& b){ return a.s.score > b.s.score; });
    eq = equity(books);
    gross = gross_exposure(books);
    for (auto& c : candidates) {
        c.s.desired_notional = size_trade(c.s, *c.m, eq, gross);
        append_signal(c.s);
        if (paper && !scan_only && !cfg_.scan_only && c.s.net_edge > cfg_.min_net_edge && c.s.desired_notional > 0.0 && !positions_.count(c.m->id)) {
            paper_trade(c.s, *c.m, *c.b, c.fd, c.s.desired_notional);
            eq = equity(books);
            gross = gross_exposure(books);
        }
    }

    persist_state(equity(books), gross_exposure(books));
    std::cout << "discovered=" << markets.size() << " tradable=" << tradable.size() << " candidates=" << candidates.size()
              << " gross_pos=" << gross_positive << " cost_pos=" << cost_positive << " net_pos=" << net_positive
              << " best_net=" << (std::isfinite(best_net) ? best_net : 0.0) << " pca=" << pca.size() << " graph=" << graph.size()
              << " positions=" << positions_.size() << " cash=" << cash_ << " equity=" << equity(books) << " killed=" << killed_ << '\n';
}

void Engine::run_loop(bool paper) {
    for (;;) {
        try { run_once(paper, false); }
        catch (const std::exception& e) { std::cerr << "tick error: " << e.what() << '\n'; }
        std::this_thread::sleep_for(std::chrono::seconds(std::max(1, cfg_.interval_seconds)));
    }
}

} // namespace pm
