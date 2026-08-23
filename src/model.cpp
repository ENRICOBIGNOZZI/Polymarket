#include "poly/engine.hpp"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace poly {
namespace {

double logit(double p) {
    p = std::clamp(p, 0.001, 0.999);
    return std::log(p / (1.0 - p));
}

double logistic(double z) {
    if (z >= 0.0) {
        const double e = std::exp(-z);
        return 1.0 / (1.0 + e);
    }
    const double e = std::exp(z);
    return e / (1.0 + e);
}

double weighted_mean(const std::vector<std::pair<double,double>>& x, double fallback) {
    double n = 0.0, d = 0.0;
    for (const auto& [a,w] : x) { n += a * w; d += w; }
    return d > 0.0 ? n / d : fallback;
}

std::chrono::system_clock::time_point parse_timestamp(const std::string& raw) {
    if (raw.empty()) return std::chrono::system_clock::now();
    try {
        std::size_t pos = 0;
        const auto sec = std::stoll(raw, &pos);
        if (pos == raw.size()) return std::chrono::system_clock::time_point{std::chrono::seconds(sec)};
    } catch (...) {}

    std::tm tm{};
    std::istringstream is(raw);
    is >> std::get_time(&tm, "%Y-%m-%dT%H:%M:%S");
    if (is.fail()) return std::chrono::system_clock::now();
#ifdef _WIN32
    const auto t = _mkgmtime(&tm);
#else
    const auto t = timegm(&tm);
#endif
    if (t < 0) return std::chrono::system_clock::now();
    return std::chrono::system_clock::from_time_t(t);
}

void atomic_write(const std::string& path, const std::string& content) {
    const std::string tmp = path + ".tmp";
    {
        std::ofstream out(tmp, std::ios::trunc);
        if (!out) throw std::runtime_error("cannot write state file: " + tmp);
        out << content;
        out.flush();
        if (!out) throw std::runtime_error("failed writing state file: " + tmp);
    }
    std::error_code ec;
    std::filesystem::rename(tmp, path, ec);
    if (ec) {
        std::filesystem::remove(path, ec);
        ec.clear();
        std::filesystem::rename(tmp, path, ec);
        if (ec) throw std::runtime_error("cannot atomically replace state file: " + path);
    }
}

} // namespace

void ExternalSignalStore::load_csv(const std::string& path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error("cannot open external signal CSV: " + path);
    std::string line;
    std::getline(in, line); // header
    while (std::getline(in, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::stringstream ss(line);
        std::string id, p, c, src, ts;
        std::getline(ss, id, ',');
        std::getline(ss, p, ',');
        std::getline(ss, c, ',');
        std::getline(ss, src, ',');
        std::getline(ss, ts);
        try {
            set(id, {clamp_probability(std::stod(p)), std::clamp(std::stod(c), 0.0, 1.0), src, parse_timestamp(ts)});
        } catch (...) {}
    }
}

void ExternalSignalStore::set(const std::string& id, ExternalSignal s) { signals_[id] = std::move(s); }
std::optional<ExternalSignal> ExternalSignalStore::get(const std::string& id) const {
    auto it = signals_.find(id);
    return it == signals_.end() ? std::nullopt : std::optional<ExternalSignal>(it->second);
}

std::vector<double> hash_text_embedding(const std::string& text, std::size_t dim) {
    std::vector<double> v(dim);
    std::string token;
    auto flush = [&] {
        if (token.size() < 2) { token.clear(); return; }
        std::uint64_t h = 1469598103934665603ULL;
        for (unsigned char c : token) { h ^= c; h *= 1099511628211ULL; }
        v[h % dim] += ((h >> 8) & 1U) ? 1.0 : -1.0;
        token.clear();
    };
    for (unsigned char c : text) {
        if (std::isalnum(c)) token.push_back(static_cast<char>(std::tolower(c)));
        else flush();
    }
    flush();
    const double n = std::sqrt(std::inner_product(v.begin(), v.end(), v.begin(), 0.0));
    if (n > 0.0) for (auto& x : v) x /= n;
    return v;
}

double cosine_similarity(const std::vector<double>& a, const std::vector<double>& b) {
    return a.size() == b.size() && !a.empty() ? std::inner_product(a.begin(), a.end(), b.begin(), 0.0) : 0.0;
}

double clamp_probability(double p) { return std::clamp(p, 0.001, 0.999); }

double platform_fee_per_share(double p, double r, double fee_exponent) {
    (void)fee_exponent; // retained for CLOB metadata compatibility; current public fee formula has exponent one.
    if (r <= 0.0) return 0.0;
    p = std::clamp(p, 0.001, 0.999);
    return r * p * (1.0 - p);
}

UniversalModel::UniversalModel(EngineConfig c) : cfg_(c) {}

void UniversalModel::load_history(const std::string& path) {
    history_path_ = path;
    std::ifstream in(path);
    std::string line;
    while (std::getline(in, line)) {
        std::stringstream ss(line);
        std::string ts, id, px;
        std::getline(ss, ts, ',');
        std::getline(ss, id, ',');
        std::getline(ss, px, ',');
        try {
            auto& h = price_history_[id];
            const auto t = static_cast<std::int64_t>(std::stoll(ts));
            const auto p = clamp_probability(std::stod(px));
            if (!h.empty() && h.back().timestamp == t) h.back().price = p;
            else h.push_back({t, p});
            while (h.size() > cfg_.factor_history + 2) h.pop_front();
        } catch (...) {}
    }
}

std::size_t UniversalModel::history_size(const std::string& id) const {
    auto it = price_history_.find(id);
    return it == price_history_.end() ? 0 : it->second.size();
}

void UniversalModel::update_history(const std::vector<LiveMarket>& u) {
    std::ofstream out;
    if (!history_path_.empty()) out.open(history_path_, std::ios::app);
    const auto ts = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    for (const auto& m : u) {
        auto& h = price_history_[m.market.id];
        const auto p = clamp_probability(m.yes_book.midpoint());
        if (!h.empty() && h.back().timestamp == ts) h.back().price = p;
        else h.push_back({ts, p});
        while (h.size() > cfg_.factor_history + 2) h.pop_front();
        if (out) out << ts << ',' << m.market.id << ',' << p << '\n';
    }
}

void UniversalModel::load_state(const std::string& path) {
    std::ifstream in(path);
    if (!in) return;
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        std::stringstream ss(line);
        std::vector<std::string> x;
        std::string v;
        while (std::getline(ss, v, ',')) x.push_back(v);
        try {
            if (x.size() >= 4 && x[0] == "expert") {
                expert_brier_sum_[x[1]] = std::stod(x[2]);
                expert_brier_n_[x[1]] = static_cast<std::size_t>(std::stoull(x[3]));
            } else if (x.size() >= 6 && x[0] == "prediction") {
                ExpertPrediction p{x[2], std::stod(x[3]), std::stod(x[4]), std::stoi(x[5]) != 0};
                last_prediction_[x[1]].components.push_back(std::move(p));
            }
        } catch (...) {}
    }
}

void UniversalModel::save_state(const std::string& path) const {
    std::ostringstream out;
    for (const auto& [expert, sum] : expert_brier_sum_) {
        auto n = expert_brier_n_.find(expert);
        out << "expert," << expert << ',' << sum << ',' << (n == expert_brier_n_.end() ? 0 : n->second) << '\n';
    }
    for (const auto& [market, fair] : last_prediction_) {
        for (const auto& p : fair.components) {
            out << "prediction," << market << ',' << p.expert << ',' << p.probability << ',' << p.confidence << ',' << (p.active ? 1 : 0) << '\n';
        }
    }
    atomic_write(path, out.str());
}

void UniversalModel::prepare_cycle(const std::vector<LiveMarket>& universe) {
    factor_cache_.clear();
    if (cfg_.min_factor_history < 3) return;

    std::unordered_map<std::string, std::vector<const LiveMarket*>> groups;
    for (const auto& m : universe) {
        auto it = price_history_.find(m.market.id);
        if (it == price_history_.end() || it->second.size() < cfg_.min_factor_history) continue;
        const auto& h = it->second;
        std::ostringstream sig;
        const auto start = h.size() - cfg_.min_factor_history;
        for (std::size_t j = start; j < h.size(); ++j) sig << h[j].timestamp << ';';
        groups[sig.str()].push_back(&m);
    }

    for (const auto& [sig, g] : groups) {
        (void)sig;
        if (g.size() < 3) continue;
        std::size_t H = cfg_.factor_history;
        for (const auto* m : g) H = std::min(H, price_history_.at(m->market.id).size());
        H = std::max(H, cfg_.min_factor_history);

        // Extend the synchronized window backwards only while timestamps match exactly.
        while (H > cfg_.min_factor_history) {
            bool aligned = true;
            const auto& ref = price_history_.at(g.front()->market.id);
            const auto ref_start = ref.size() - H;
            for (std::size_t i = 1; i < g.size() && aligned; ++i) {
                const auto& h = price_history_.at(g[i]->market.id);
                const auto start = h.size() - H;
                for (std::size_t t = 0; t < H; ++t) {
                    if (h[start+t].timestamp != ref[ref_start+t].timestamp) { aligned = false; break; }
                }
            }
            if (aligned) break;
            --H;
        }
        if (H < cfg_.min_factor_history) continue;

        const std::size_t n = g.size(), k = H - 1;
        std::vector<std::vector<double>> r(n, std::vector<double>(k));
        std::vector<double> mu(n, 0.0);
        for (std::size_t i = 0; i < n; ++i) {
            const auto& h = price_history_.at(g[i]->market.id);
            const auto off = h.size() - H;
            for (std::size_t t = 0; t < k; ++t) {
                r[i][t] = logit(h[off+t+1].price) - logit(h[off+t].price);
                mu[i] += r[i][t];
            }
            mu[i] /= static_cast<double>(k);
            for (auto& x : r[i]) x -= mu[i];
        }

        std::vector<std::vector<double>> C(n, std::vector<double>(n, 0.0));
        for (std::size_t i = 0; i < n; ++i) {
            for (std::size_t j = i; j < n; ++j) {
                double s = 0.0;
                for (std::size_t t = 0; t < k; ++t) s += r[i][t] * r[j][t];
                s /= static_cast<double>(std::max<std::size_t>(1, k - 1));
                C[i][j] = C[j][i] = s;
            }
        }

        std::vector<double> v(n, 1.0 / std::sqrt(static_cast<double>(n)));
        bool stable = true;
        for (int iter = 0; iter < 40; ++iter) {
            std::vector<double> w(n, 0.0);
            for (std::size_t i = 0; i < n; ++i)
                for (std::size_t j = 0; j < n; ++j)
                    w[i] += C[i][j] * v[j];
            const double norm = std::sqrt(std::inner_product(w.begin(), w.end(), w.begin(), 0.0));
            if (norm < 1e-12 || !std::isfinite(norm)) { stable = false; break; }
            for (std::size_t i = 0; i < n; ++i) v[i] = w[i] / norm;
        }
        if (!stable) continue;

        double leading = 0.0, trace = 0.0;
        for (std::size_t i = 0; i < n; ++i) {
            trace += std::max(0.0, C[i][i]);
            for (std::size_t j = 0; j < n; ++j) leading += v[i] * C[i][j] * v[j];
        }
        const double explained = trace > 1e-12 ? std::clamp(leading / trace, 0.0, 1.0) : 0.0;

        std::vector<double> current(n, 0.0);
        for (std::size_t i = 0; i < n; ++i) {
            const auto& h = price_history_.at(g[i]->market.id);
            current[i] = logit(g[i]->yes_book.midpoint()) - logit(h.back().price) - mu[i];
        }
        const double common_shock = std::inner_product(v.begin(), v.end(), current.begin(), 0.0);

        std::vector<double> hist_factor(k, 0.0);
        for (std::size_t t = 0; t < k; ++t)
            for (std::size_t i = 0; i < n; ++i)
                hist_factor[t] += v[i] * r[i][t];

        for (std::size_t i = 0; i < n; ++i) {
            const double residual = current[i] - v[i] * common_shock;
            double rss = 0.0;
            for (std::size_t t = 0; t < k; ++t) {
                const double e = r[i][t] - v[i] * hist_factor[t];
                rss += e * e;
            }
            const double sd = std::sqrt(rss / static_cast<double>(std::max<std::size_t>(1, k - 1)) + 1e-10);
            const double z = std::abs(residual) / sd;
            const double fair_logit = logit(g[i]->yes_book.midpoint()) - cfg_.factor_mean_reversion * residual;
            const double group_strength = std::clamp(static_cast<double>(n) / 20.0, 0.15, 1.0);
            const double factor_strength = std::clamp(explained / 0.30, 0.20, 1.0);
            const double confidence = (0.10 + 0.45 * std::clamp(z / 3.0, 0.0, 1.0)) * group_strength * factor_strength;
            factor_cache_[g[i]->market.id] = {"pca_factor", clamp_probability(logistic(fair_logit)), std::clamp(confidence, 0.0, 0.75), true};
        }
    }
}

ExpertPrediction UniversalModel::microstructure(const LiveMarket& m) const {
    const auto& b = m.yes_book;
    if (!b.two_sided()) return {"micro", 0.5, 0.0, false};
    const double depth = b.bid_depth + b.ask_depth;
    const double imbalance = depth > 0.0 ? (b.bid_depth - b.ask_depth) / depth : 0.0;
    const double fair = clamp_probability(b.midpoint() + 0.35 * b.spread() * imbalance);
    const double spread_conf = std::clamp(1.0 - b.spread() / std::max(1e-6, cfg_.max_spread), 0.0, 1.0);
    const double depth_conf = std::clamp(std::log1p(depth) / std::log(10001.0), 0.0, 1.0);
    return {"micro", fair, 0.15 + 0.65 * spread_conf * depth_conf, true};
}

ExpertPrediction UniversalModel::factor(const LiveMarket& m) const {
    auto it = factor_cache_.find(m.market.id);
    return it == factor_cache_.end() ? ExpertPrediction{"pca_factor", 0.5, 0.0, false} : it->second;
}

ExpertPrediction UniversalModel::graph(const LiveMarket& m, const std::vector<LiveMarket>& u) const {
    if (!m.market.neg_risk || m.market.event_id.empty()) return {"graph", 0.5, 0.0, false};
    std::vector<const LiveMarket*> g;
    for (const auto& x : u) {
        if (x.market.neg_risk && x.market.event_id == m.market.event_id) g.push_back(&x);
    }
    if (g.size() < 2) return {"graph", 0.5, 0.0, false};
    double sum = 0.0;
    for (const auto* x : g) sum += x->yes_book.midpoint();
    const double dev = std::abs(sum - 1.0);
    if (sum <= 0.0 || dev > cfg_.graph_sum_tolerance) return {"graph", 0.5, 0.0, false};
    const double confidence = 0.20 + 0.50 * std::clamp(dev / std::max(1e-6, cfg_.graph_sum_tolerance), 0.0, 1.0);
    return {"graph", clamp_probability(m.yes_book.midpoint() / sum), confidence, true};
}

ExpertPrediction UniversalModel::semantic_relative_value(const LiveMarket& m, const std::vector<LiveMarket>& u) const {
    std::vector<std::pair<double,double>> peers;
    double sim_sum = 0.0;
    for (const auto& x : u) {
        if (x.market.id == m.market.id) continue;
        const double s = cosine_similarity(m.text_embedding, x.text_embedding);
        if (s >= cfg_.semantic_similarity_threshold) {
            peers.emplace_back(x.yes_book.midpoint(), s * s);
            sim_sum += s;
        }
    }
    if (peers.size() < 2) return {"semantic_rv", 0.5, 0.0, false};
    const double cur = m.yes_book.midpoint();
    const double peer = weighted_mean(peers, cur);
    const double rho = std::clamp(cfg_.semantic_shrinkage, 0.0, 0.5);
    const double avg_sim = sim_sum / static_cast<double>(peers.size());
    const double confidence = std::clamp(0.05 + 0.25 * (avg_sim - cfg_.semantic_similarity_threshold) /
        std::max(1e-6, 1.0 - cfg_.semantic_similarity_threshold) + 0.02 * std::min<std::size_t>(peers.size(), 10), 0.0, 0.45);
    return {"semantic_rv", clamp_probability((1.0 - rho) * cur + rho * peer), confidence, true};
}

ExpertPrediction UniversalModel::external_signal(const LiveMarket& m, const ExternalSignalStore& e) const {
    auto s = e.get(m.market.id);
    if (!s) return {"external", 0.5, 0.0, false};
    const double age = std::chrono::duration<double, std::ratio<3600>>(
        std::chrono::system_clock::now() - s->timestamp).count();
    const double tau = std::max(1e-6, cfg_.external_decay_hours);
    const double decayed = s->confidence * std::exp(-std::max(0.0, age) / tau);
    return {"external", clamp_probability(s->probability), std::clamp(decayed, 0.0, 1.0), true};
}

double UniversalModel::expert_weight(const ExpertPrediction& p) const {
    double brier = 0.25;
    auto n = expert_brier_n_.find(p.expert);
    if (n != expert_brier_n_.end() && n->second > 0) brier = expert_brier_sum_.at(p.expert) / static_cast<double>(n->second);
    return std::exp(-cfg_.ensemble_eta * brier);
}

FairValue UniversalModel::predict(const LiveMarket& m, const std::vector<LiveMarket>& u, const ExternalSignalStore& e) {
    FairValue f;
    f.components = {microstructure(m), factor(m), graph(m,u), semantic_relative_value(m,u), external_signal(m,e)};
    double num = 0.0, den = 0.0;
    for (const auto& p : f.components) {
        if (!p.active || p.confidence <= 0.0) continue;
        const double w = expert_weight(p) * p.confidence;
        num += w * p.probability;
        den += w;
    }
    f.probability = clamp_probability(den > 0.0 ? num / den : m.yes_book.midpoint());

    double variance = 0.0;
    if (den > 0.0) {
        for (const auto& p : f.components) {
            if (!p.active || p.confidence <= 0.0) continue;
            const double w = expert_weight(p) * p.confidence;
            const double x = p.probability - f.probability;
            variance += w * x * x;
        }
        variance /= den;
    }
    const double spread_ratio = std::clamp(m.yes_book.spread() / std::max(1e-6, cfg_.max_spread), 0.0, 1.0);
    f.uncertainty = std::clamp(std::sqrt(std::max(0.0, variance) + cfg_.uncertainty_spread_variance * spread_ratio), 0.005, 0.5);
    last_prediction_[m.market.id] = f;
    return f;
}

void UniversalModel::observe_resolution(const std::string& id, bool yes) {
    auto it = last_prediction_.find(id);
    if (it == last_prediction_.end()) return;
    const double y = yes ? 1.0 : 0.0;
    for (const auto& p : it->second.components) {
        if (!p.active) continue;
        expert_brier_sum_[p.expert] += (p.probability - y) * (p.probability - y);
        ++expert_brier_n_[p.expert];
    }
    last_prediction_.erase(it);
}

void UniversalModel::forget_prediction(const std::string& id) {
    last_prediction_.erase(id);
}

std::vector<std::string> UniversalModel::pending_market_ids() const {
    std::vector<std::string> ids;
    ids.reserve(last_prediction_.size());
    for (const auto& [id, fair] : last_prediction_) {
        (void)fair;
        ids.push_back(id);
    }
    return ids;
}

} // namespace poly
