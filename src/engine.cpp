#include "poly/engine.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cctype>
#include <curl/curl.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <utility>

#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>

namespace poly {
namespace {
using boost::property_tree::ptree;

size_t curl_write(void* data, size_t size, size_t nmemb, void* userdata) {
    const size_t bytes = size * nmemb;
    static_cast<std::string*>(userdata)->append(static_cast<char*>(data), bytes);
    return bytes;
}

double get_double(const ptree& p, const std::string& key, double fallback = 0.0) {
    if (auto v = p.get_optional<double>(key)) return *v;
    if (auto s = p.get_optional<std::string>(key)) {
        try { return std::stod(*s); } catch (...) {}
    }
    return fallback;
}

bool get_bool(const ptree& p, const std::string& key, bool fallback = false) {
    if (auto v = p.get_optional<bool>(key)) return *v;
    if (auto s = p.get_optional<std::string>(key)) {
        std::string x = *s;
        std::transform(x.begin(), x.end(), x.begin(), [](unsigned char c){ return std::tolower(c); });
        if (x == "true" || x == "1") return true;
        if (x == "false" || x == "0") return false;
    }
    return fallback;
}

std::vector<std::string> parse_json_string_array(const std::string& encoded) {
    std::vector<std::string> out;
    if (encoded.empty()) return out;
    std::istringstream in(encoded);
    ptree a;
    try {
        boost::property_tree::read_json(in, a);
        for (const auto& child : a) out.push_back(child.second.get_value<std::string>());
    } catch (...) {}
    return out;
}

std::vector<std::string> ptree_array_strings(const ptree& p, const std::string& key) {
    if (auto child = p.get_child_optional(key)) {
        std::vector<std::string> out;
        for (const auto& x : *child) out.push_back(x.second.get_value<std::string>());
        return out;
    }
    if (auto encoded = p.get_optional<std::string>(key)) return parse_json_string_array(*encoded);
    return {};
}

std::string first_event_id(const ptree& p) {
    if (auto events = p.get_child_optional("events")) {
        for (const auto& e : *events) {
            if (auto id = e.second.get_optional<std::string>("id")) return *id;
            if (auto idn = e.second.get_optional<long long>("id")) return std::to_string(*idn);
        }
    }
    if (auto id = p.get_optional<std::string>("eventId")) return *id;
    return {};
}

std::string now_iso() {
    const auto now = std::chrono::system_clock::now();
    const std::time_t t = std::chrono::system_clock::to_time_t(now);
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

double weighted_mean(const std::vector<std::pair<double,double>>& xs, double fallback) {
    double num = 0.0, den = 0.0;
    for (auto [x,w] : xs) { num += x*w; den += w; }
    return den > 0.0 ? num/den : fallback;
}

} // namespace

HttpClient::HttpClient() {
    static const int init = [](){ curl_global_init(CURL_GLOBAL_DEFAULT); return 1; }();
    (void)init;
}
HttpClient::~HttpClient() = default;

std::string HttpClient::get(const std::string& url) const {
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("curl_easy_init failed");
    std::string body;
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curl_write);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "PolymarketQuantEngine/0.2");
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 8L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    CURLcode rc = curl_easy_perform(curl);
    long status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_cleanup(curl);
    if (rc != CURLE_OK) throw std::runtime_error(std::string("HTTP failure: ") + curl_easy_strerror(rc));
    if (status < 200 || status >= 300) throw std::runtime_error("HTTP status " + std::to_string(status) + " for " + url);
    return body;
}

PolymarketClient::PolymarketClient(HttpClient http) : http_(std::move(http)) {}

std::vector<Market> PolymarketClient::discover_markets(std::size_t limit) const {
    const std::string url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit=" + std::to_string(limit);
    std::istringstream in(http_.get(url));
    ptree root;
    boost::property_tree::read_json(in, root);
    std::vector<Market> out;
    for (const auto& item : root) {
        const auto& p = item.second;
        Market m;
        m.id = p.get<std::string>("id", "");
        m.question = p.get<std::string>("question", "");
        m.slug = p.get<std::string>("slug", "");
        m.condition_id = p.get<std::string>("conditionId", "");
        m.event_id = first_event_id(p);
        m.end_date = p.get<std::string>("endDate", "");
        m.active = get_bool(p, "active");
        m.closed = get_bool(p, "closed");
        m.accepting_orders = get_bool(p, "acceptingOrders", true);
        m.neg_risk = get_bool(p, "negRisk", false);
        m.volume_24h = get_double(p, "volume24hr", get_double(p, "volume24hrClob", 0.0));
        m.liquidity = get_double(p, "liquidityNum", get_double(p, "liquidity", 0.0));
        auto tokens = ptree_array_strings(p, "clobTokenIds");
        auto prices = ptree_array_strings(p, "outcomePrices");
        if (tokens.size() >= 2) { m.yes_token = tokens[0]; m.no_token = tokens[1]; }
        if (!prices.empty()) {
            try { m.gamma_yes_price = std::stod(prices[0]); } catch (...) {}
        }
        if (!m.id.empty() && !m.yes_token.empty() && !m.no_token.empty()) out.push_back(std::move(m));
    }
    return out;
}

BookSnapshot PolymarketClient::get_book(const std::string& token_id) const {
    const std::string url = "https://clob.polymarket.com/book?token_id=" + token_id;
    std::istringstream in(http_.get(url));
    ptree root;
    boost::property_tree::read_json(in, root);
    BookSnapshot b;
    b.token_id = token_id;
    if (auto xs = root.get_child_optional("bids")) {
        for (const auto& x : *xs) {
            Level l{get_double(x.second,"price"), get_double(x.second,"size")};
            if (l.price > 0.0 && l.size > 0.0) b.bids.push_back(l);
        }
    }
    if (auto xs = root.get_child_optional("asks")) {
        for (const auto& x : *xs) {
            Level l{get_double(x.second,"price"), get_double(x.second,"size")};
            if (l.price > 0.0 && l.size > 0.0) b.asks.push_back(l);
        }
    }
    b.best_bid = 0.0;
    b.best_ask = 1.0;
    for (const auto& l : b.bids) b.best_bid = std::max(b.best_bid, l.price);
    for (const auto& l : b.asks) b.best_ask = std::min(b.best_ask, l.price);
    const double band = 0.03;
    for (const auto& l : b.bids) if (b.best_bid - l.price <= band + 1e-12) b.bid_depth += l.size;
    for (const auto& l : b.asks) if (l.price - b.best_ask <= band + 1e-12) b.ask_depth += l.size;
    return b;
}

std::vector<LiveMarket> PolymarketClient::snapshot(std::size_t limit, double min_liquidity, double max_spread) const {
    std::vector<LiveMarket> out;
    for (auto& m : discover_markets(limit)) {
        if (!m.accepting_orders || m.liquidity < min_liquidity) continue;
        try {
            auto y = get_book(m.yes_token);
            auto n = get_book(m.no_token);
            if (y.bids.empty() || y.asks.empty() || n.bids.empty() || n.asks.empty()) continue;
            if (y.spread() <= 0.0 || y.spread() > max_spread || n.spread() <= 0.0 || n.spread() > max_spread) continue;
            LiveMarket lm{m, std::move(y), std::move(n), hash_text_embedding(m.question)};
            out.push_back(std::move(lm));
        } catch (const std::exception& e) {
            std::cerr << "[warn] skipping market " << m.id << ": " << e.what() << '\n';
        }
    }
    return out;
}

void ExternalSignalStore::load_csv(const std::string& path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error("cannot open external signal CSV: " + path);
    std::string line;
    std::getline(in, line);
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        std::stringstream ss(line);
        std::string market, prob, conf, source;
        std::getline(ss, market, ','); std::getline(ss, prob, ','); std::getline(ss, conf, ','); std::getline(ss, source);
        try {
            set(market, ExternalSignal{clamp_probability(std::stod(prob)), std::clamp(std::stod(conf),0.0,1.0), source, std::chrono::system_clock::now()});
        } catch (...) {}
    }
}
void ExternalSignalStore::set(const std::string& market_id, ExternalSignal signal) { signals_[market_id] = std::move(signal); }
std::optional<ExternalSignal> ExternalSignalStore::get(const std::string& market_id) const {
    auto it = signals_.find(market_id);
    if (it == signals_.end()) return std::nullopt;
    return it->second;
}

std::vector<double> hash_text_embedding(const std::string& text, std::size_t dim) {
    std::vector<double> v(dim, 0.0);
    std::string token;
    auto flush = [&]() {
        if (token.size() < 2) { token.clear(); return; }
        uint64_t h = 1469598103934665603ULL;
        for (unsigned char c : token) { h ^= c; h *= 1099511628211ULL; }
        const std::size_t j = h % dim;
        const double s = ((h >> 8) & 1ULL) ? 1.0 : -1.0;
        v[j] += s;
        token.clear();
    };
    for (unsigned char c : text) {
        if (std::isalnum(c)) token.push_back(static_cast<char>(std::tolower(c)));
        else flush();
    }
    flush();
    const double norm = std::sqrt(std::inner_product(v.begin(),v.end(),v.begin(),0.0));
    if (norm > 0.0) for (double& x : v) x /= norm;
    return v;
}

double cosine_similarity(const std::vector<double>& a, const std::vector<double>& b) {
    if (a.size() != b.size() || a.empty()) return 0.0;
    return std::inner_product(a.begin(),a.end(),b.begin(),0.0);
}

double clamp_probability(double p) { return std::clamp(p, 0.001, 0.999); }

UniversalModel::UniversalModel(EngineConfig config) : cfg_(config) {}

ExpertPrediction UniversalModel::microstructure(const LiveMarket& m) const {
    const auto& b = m.yes_book;
    if (b.bids.empty() || b.asks.empty()) return {"micro",0.5,0.0,false};
    const double mid = b.midpoint();
    const double den = b.bid_depth + b.ask_depth;
    const double imbalance = den > 0.0 ? (b.bid_depth - b.ask_depth) / den : 0.0;
    const double fair = clamp_probability(mid + 0.35 * b.spread() * imbalance);
    const double spread_score = std::clamp(1.0 - b.spread()/cfg_.max_spread, 0.0, 1.0);
    const double depth_score = std::clamp(std::log1p(den)/std::log(10001.0), 0.0, 1.0);
    return {"micro", fair, 0.25 + 0.55*spread_score*depth_score, true};
}

ExpertPrediction UniversalModel::factor(const LiveMarket& m, const std::vector<LiveMarket>& universe) const {
    std::vector<const LiveMarket*> assets;
    std::size_t common = std::numeric_limits<std::size_t>::max();
    for (const auto& x : universe) {
        auto it = price_history_.find(x.market.id);
        if (it == price_history_.end() || it->second.size() < cfg_.min_factor_history) continue;
        assets.push_back(&x);
        common = std::min(common, it->second.size());
    }
    if (assets.size() < 3 || common < cfg_.min_factor_history) return {"pca_factor",0.5,0.0,false};
    common = std::min(common, cfg_.factor_history);
    const std::size_t n = assets.size();
    const std::size_t k = common - 1;
    std::size_t target = n;
    std::vector<std::vector<double>> r(n, std::vector<double>(k));
    std::vector<double> mean(n,0.0);
    auto logit=[](double p){ p=std::clamp(p,0.001,0.999); return std::log(p/(1.0-p)); };
    for (std::size_t i=0;i<n;++i) {
        if (assets[i]->market.id == m.market.id) target=i;
        const auto& h = price_history_.at(assets[i]->market.id);
        const std::size_t offset=h.size()-common;
        for (std::size_t t=0;t<k;++t) {
            r[i][t]=logit(h[offset+t+1])-logit(h[offset+t]);
            mean[i]+=r[i][t];
        }
        mean[i]/=static_cast<double>(k);
        for (double& x:r[i]) x-=mean[i];
    }
    if (target==n) return {"pca_factor",0.5,0.0,false};

    std::vector<std::vector<double>> cov(n,std::vector<double>(n,0.0));
    for(std::size_t i=0;i<n;++i) for(std::size_t j=i;j<n;++j) {
        double c=0.0;
        for(std::size_t t=0;t<k;++t)c+=r[i][t]*r[j][t];
        c/=static_cast<double>(std::max<std::size_t>(1,k-1));
        cov[i][j]=cov[j][i]=c;
    }
    std::vector<double> v(n,1.0/std::sqrt(static_cast<double>(n)));
    for(int iter=0;iter<30;++iter) {
        std::vector<double> z(n,0.0);
        for(std::size_t i=0;i<n;++i) for(std::size_t j=0;j<n;++j) z[i]+=cov[i][j]*v[j];
        double norm=std::sqrt(std::inner_product(z.begin(),z.end(),z.begin(),0.0));
        if(norm<1e-12) return {"pca_factor",0.5,0.0,false};
        for(std::size_t i=0;i<n;++i)v[i]=z[i]/norm;
    }

    std::vector<double> current_r(n,0.0);
    for(std::size_t i=0;i<n;++i) {
        const auto& h=price_history_.at(assets[i]->market.id);
        current_r[i]=logit(assets[i]->yes_book.midpoint())-logit(h.back())-mean[i];
    }
    const double f=std::inner_product(v.begin(),v.end(),current_r.begin(),0.0);
    const double residual=current_r[target]-v[target]*f;

    double rss=0.0;
    for(std::size_t t=0;t<k;++t) {
        double ft=0.0;
        for(std::size_t i=0;i<n;++i)ft+=v[i]*r[i][t];
        const double e=r[target][t]-v[target]*ft;
        rss+=e*e;
    }
    const double sd=std::sqrt(rss/static_cast<double>(std::max<std::size_t>(1,k-1))+1e-10);
    const double z=std::abs(residual)/sd;
    const double current_logit=logit(m.yes_book.midpoint());
    const double fair_logit=current_logit-0.35*residual;
    const double fair=1.0/(1.0+std::exp(-fair_logit));
    const double breadth=std::clamp(static_cast<double>(n)/20.0,0.0,1.0);
    const double conf=(0.12+0.38*std::clamp(z/3.0,0.0,1.0))*breadth;
    return {"pca_factor",clamp_probability(fair),conf,true};
}

ExpertPrediction UniversalModel::graph(const LiveMarket& m, const std::vector<LiveMarket>& universe) const {
    if (!m.market.neg_risk || m.market.event_id.empty()) return {"graph",0.5,0.0,false};
    std::vector<const LiveMarket*> group;
    for (const auto& x : universe) if (x.market.neg_risk && x.market.event_id == m.market.event_id) group.push_back(&x);
    if (group.size() < 2) return {"graph",0.5,0.0,false};
    double total = 0.0;
    for (const auto* x : group) total += x->yes_book.midpoint();
    if (total <= 0.0) return {"graph",0.5,0.0,false};
    const double normalized = m.yes_book.midpoint()/total;
    const double inconsistency = std::min(1.0, std::abs(total-1.0));
    return {"graph", clamp_probability(normalized), 0.30 + 0.50*inconsistency, true};
}

ExpertPrediction UniversalModel::semantic_relative_value(const LiveMarket& m, const std::vector<LiveMarket>& universe) const {
    std::vector<std::pair<double,double>> neighbors;
    for (const auto& x : universe) {
        if (x.market.id == m.market.id) continue;
        double sim = cosine_similarity(m.text_embedding, x.text_embedding);
        if (sim >= 0.72) neighbors.emplace_back(x.yes_book.midpoint(), sim*sim);
    }
    if (neighbors.size() < 2) return {"semantic_rv",0.5,0.0,false};
    const double peer = weighted_mean(neighbors, m.yes_book.midpoint());
    const double current = m.yes_book.midpoint();
    const double fair = 0.85*current + 0.15*peer;
    const double conf = std::min(0.30, 0.08 + 0.03*neighbors.size());
    return {"semantic_rv", clamp_probability(fair), conf, true};
}

ExpertPrediction UniversalModel::external_signal(const LiveMarket& m, const ExternalSignalStore& external) const {
    auto s = external.get(m.market.id);
    if (!s) return {"external",0.5,0.0,false};
    const auto age = std::chrono::duration_cast<std::chrono::hours>(std::chrono::system_clock::now()-s->timestamp).count();
    const double freshness = std::exp(-std::max<long long>(0, age)/48.0);
    return {"external", clamp_probability(s->probability), std::clamp(s->confidence*freshness,0.0,1.0), true};
}

double UniversalModel::expert_weight(const ExpertPrediction& p) const {
    auto ni = expert_brier_n_.find(p.expert);
    double brier = 0.25;
    if (ni != expert_brier_n_.end() && ni->second > 0) brier = expert_brier_sum_.at(p.expert)/ni->second;
    return p.confidence * std::exp(-cfg_.ensemble_eta*brier);
}

FairValue UniversalModel::predict(const LiveMarket& market,
                                  const std::vector<LiveMarket>& universe,
                                  const ExternalSignalStore& external) {
    FairValue fv;
    fv.components = {microstructure(market), factor(market,universe), graph(market,universe), semantic_relative_value(market,universe), external_signal(market,external)};
    double num = 0.0, den = 0.0;
    for (const auto& p : fv.components) if (p.active && p.confidence > 0.0) {
        const double w = expert_weight(p);
        num += w*p.probability;
        den += w;
    }
    const double fallback = market.yes_book.midpoint();
    fv.probability = clamp_probability(den > 0.0 ? num/den : fallback);
    double var = 0.0;
    if (den > 0.0) {
        for (const auto& p : fv.components) if (p.active && p.confidence > 0.0) {
            const double w = expert_weight(p);
            var += w*(p.probability-fv.probability)*(p.probability-fv.probability);
        }
        var /= den;
    }
    const double liquidity_uncertainty = std::clamp(market.yes_book.spread()/cfg_.max_spread,0.0,1.0);
    fv.uncertainty = std::clamp(std::sqrt(var) + 0.15*liquidity_uncertainty, 0.005, 0.50);
    last_prediction_[market.market.id] = fv;
    return fv;
}

void UniversalModel::update_history(const std::vector<LiveMarket>& universe) {
    for (const auto& m : universe) {
        auto& h = price_history_[m.market.id];
        h.push_back(m.yes_book.midpoint());
        while (h.size() > cfg_.factor_history) h.pop_front();
    }
}

void UniversalModel::observe_resolution(const std::string& market_id, bool yes_outcome) {
    auto it = last_prediction_.find(market_id);
    if (it == last_prediction_.end()) return;
    const double y = yes_outcome ? 1.0 : 0.0;
    for (const auto& p : it->second.components) if (p.active) {
        expert_brier_sum_[p.expert] += (p.probability-y)*(p.probability-y);
        expert_brier_n_[p.expert] += 1;
    }
}

RiskManager::RiskManager(EngineConfig config) : cfg_(config), peak_equity_(config.initial_capital) {}
double RiskManager::drawdown(double equity) const { return peak_equity_ > 0.0 ? std::max(0.0,1.0-equity/peak_equity_) : 0.0; }
void RiskManager::update_equity(double equity) {
    peak_equity_ = std::max(peak_equity_, equity);
    if (drawdown(equity) >= cfg_.max_drawdown) killed_ = true;
}

double RiskManager::allowed_notional(const TradeIdea& idea, double equity, double gross_exposure, double market_exposure, double event_exposure) const {
    if (killed_ || equity <= 0.0 || idea.net_edge <= 0.0) return 0.0;
    const double risk_budget = std::max(0.0, cfg_.max_gross_fraction*equity-gross_exposure);
    const double market_budget = std::max(0.0, cfg_.max_market_fraction*equity-market_exposure);
    const double event_budget = std::max(0.0, cfg_.max_event_fraction*equity-event_exposure);
    const double trade_cap = cfg_.max_trade_fraction*equity;
    const double p = std::clamp(idea.entry_price,0.001,0.999);
    const double q = std::clamp(idea.fair_probability,0.001,0.999);
    const double full_kelly = std::max(0.0,(q-p)/(1.0-p));
    const double kelly_cap = cfg_.kelly_fraction*full_kelly*equity;
    return std::max(0.0,std::min({risk_budget,market_budget,event_budget,trade_cap,kelly_cap}));
}

PaperBroker::PaperBroker(double cash) : cash_(cash) {}
bool PaperBroker::buy(const TradeIdea& idea, double notional) {
    if (notional <= 0.0 || notional > cash_ || idea.entry_price <= 0.0) return false;
    const double shares = notional/idea.entry_price;
    positions_.push_back(Position{idea.market_id,idea.token_id,idea.outcome,shares,notional});
    cash_ -= notional;
    return true;
}
double PaperBroker::gross_cost() const { double x=0.0; for (const auto& p:positions_) x+=p.cost; return x; }
double PaperBroker::marked_equity(const std::unordered_map<std::string,double>& token_mid) const {
    double e=cash_;
    for (const auto& p:positions_) {
        auto it=token_mid.find(p.token_id);
        e += p.shares*(it==token_mid.end()?p.cost/std::max(p.shares,1e-12):it->second);
    }
    return e;
}
double PaperBroker::market_exposure(const std::string& market_id) const { double x=0.0; for(const auto&p:positions_) if(p.market_id==market_id)x+=p.cost; return x; }
double PaperBroker::event_exposure(const std::string& event_id, const std::unordered_map<std::string,std::string>& market_to_event) const {
    if (event_id.empty()) return 0.0;
    double x=0.0;
    for(const auto&p:positions_) { auto it=market_to_event.find(p.market_id); if(it!=market_to_event.end()&&it->second==event_id)x+=p.cost; }
    return x;
}

QuantEngine::QuantEngine(EngineConfig config)
    : cfg_(config), client_(), model_(config), risk_(config), broker_(config.initial_capital), last_equity_(config.initial_capital) {
    std::filesystem::create_directories("runs");
}
void QuantEngine::set_external_csv(const std::string& path) { external_.load_csv(path); }

TradeIdea QuantEngine::make_idea(const LiveMarket& m, const FairValue& fair) const {
    const double qy = fair.probability;
    const double qn = 1.0-qy;
    const double y_entry = m.yes_book.best_ask;
    const double n_entry = m.no_book.best_ask;
    const double y_edge = qy-y_entry;
    const double n_edge = qn-n_entry;
    TradeIdea idea;
    idea.market_id=m.market.id; idea.question=m.market.question; idea.uncertainty=fair.uncertainty;
    if (y_edge >= n_edge) {
        idea.token_id=m.market.yes_token; idea.outcome="YES"; idea.fair_probability=qy; idea.entry_price=y_entry; idea.raw_edge=y_edge;
    } else {
        idea.token_id=m.market.no_token; idea.outcome="NO"; idea.fair_probability=qn; idea.entry_price=n_entry; idea.raw_edge=n_edge;
    }
    const double spread = idea.outcome=="YES" ? m.yes_book.spread() : m.no_book.spread();
    idea.estimated_cost = 0.5*spread + (cfg_.slippage_bps+cfg_.assumed_fee_bps)/10000.0 + cfg_.uncertainty_buffer*fair.uncertainty;
    idea.net_edge = idea.raw_edge-idea.estimated_cost;
    return idea;
}

void QuantEngine::append_signal_log(const TradeIdea& idea, const FairValue& fair) const {
    const bool fresh = !std::filesystem::exists("runs/signals.csv");
    std::ofstream o("runs/signals.csv",std::ios::app);
    if (fresh) o << "time,market_id,outcome,fair,entry,raw_edge,cost,net_edge,uncertainty,question\n";
    std::string q=idea.question; std::replace(q.begin(),q.end(),',',';');
    o << now_iso()<<','<<idea.market_id<<','<<idea.outcome<<','<<fair.probability<<','<<idea.entry_price<<','<<idea.raw_edge<<','<<idea.estimated_cost<<','<<idea.net_edge<<','<<idea.uncertainty<<','<<q<<'\n';
}
void QuantEngine::append_trade_log(const TradeIdea& idea, double notional) const {
    const bool fresh = !std::filesystem::exists("runs/trades.csv");
    std::ofstream o("runs/trades.csv",std::ios::app);
    if (fresh) o << "time,market_id,token_id,outcome,entry,notional,net_edge\n";
    o << now_iso()<<','<<idea.market_id<<','<<idea.token_id<<','<<idea.outcome<<','<<idea.entry_price<<','<<notional<<','<<idea.net_edge<<'\n';
}

void QuantEngine::run_once() {
    auto universe = client_.snapshot(cfg_.market_limit,cfg_.min_liquidity,cfg_.max_spread);
    if (universe.empty()) throw std::runtime_error("no live tradable markets returned after filters");
    std::unordered_map<std::string,double> token_mid;
    std::unordered_map<std::string,std::string> market_to_event;
    for (const auto& m:universe) {
        token_mid[m.market.yes_token]=m.yes_book.midpoint();
        token_mid[m.market.no_token]=m.no_book.midpoint();
        market_to_event[m.market.id]=m.market.event_id;
    }
    last_equity_=broker_.marked_equity(token_mid);
    risk_.update_equity(last_equity_);
    last_ideas_.clear();
    for (const auto& m:universe) {
        auto fair=model_.predict(m,universe,external_);
        auto idea=make_idea(m,fair);
        append_signal_log(idea,fair);
        if (idea.net_edge < cfg_.min_net_edge || risk_.killed()) continue;
        const double market_exp=broker_.market_exposure(m.market.id);
        const double event_exp=broker_.event_exposure(m.market.event_id,market_to_event);
        const double allowed=risk_.allowed_notional(idea,last_equity_,broker_.gross_cost(),market_exp,event_exp);
        idea.desired_notional=allowed;
        last_ideas_.push_back(idea);
        if (allowed>0.0 && broker_.buy(idea,allowed)) append_trade_log(idea,allowed);
    }
    model_.update_history(universe);
    last_equity_=broker_.marked_equity(token_mid);
    risk_.update_equity(last_equity_);

    std::sort(last_ideas_.begin(),last_ideas_.end(),[](const auto&a,const auto&b){return a.net_edge>b.net_edge;});
    std::cout << "\nPOLYMARKET UNIVERSAL ENGINE — PAPER ONLY\n"
              << "markets="<<universe.size()<<" equity="<<std::fixed<<std::setprecision(2)<<last_equity_
              << " drawdown="<<100.0*risk_.drawdown(last_equity_)<<"% killed="<<(risk_.killed()?"YES":"no")<<"\n";
    std::cout << std::left << std::setw(9)<<"market"<<std::setw(5)<<"side"<<std::setw(9)<<"entry"<<std::setw(9)<<"fair"<<std::setw(10)<<"netEdge"<<std::setw(11)<<"notional"<<"question\n";
    for (std::size_t i=0;i<std::min<std::size_t>(10,last_ideas_.size());++i) {
        const auto& x=last_ideas_[i];
        std::cout<<std::setw(9)<<x.market_id.substr(0,8)<<std::setw(5)<<x.outcome<<std::setw(9)<<std::setprecision(4)<<x.entry_price<<std::setw(9)<<x.fair_probability<<std::setw(10)<<x.net_edge<<std::setw(11)<<std::setprecision(2)<<x.desired_notional<<x.question.substr(0,75)<<'\n';
    }
}

} // namespace poly
