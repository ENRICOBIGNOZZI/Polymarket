#include "poly/model.hpp"
#include "poly/math.hpp"

#include <algorithm>
#include <cmath>
#include <map>
#include <numeric>
#include <sstream>

namespace poly {
namespace {
std::int64_t floor_bucket(std::int64_t ts, int seconds) {
    const auto s = std::max(1, seconds);
    return (ts / s) * s;
}
} // namespace

StatisticalModel::StatisticalModel(ModelConfig cfg) : cfg_(std::move(cfg)) {}

void StatisticalModel::seed_prices(const std::unordered_map<std::string, std::vector<HistoricalPoint>>& prices) {
    const std::size_t cap = std::max<std::size_t>(cfg_.lookback + 16, cfg_.min_history + 16);
    for (const auto& [token, raw] : prices) {
        std::vector<HistoricalPoint> px = raw;
        std::sort(px.begin(), px.end(), [](const auto& a, const auto& b){ return a.ts < b.ts; });
        auto& s = history_[token];
        s.obs.clear();
        for (const auto& point : px) {
            if (point.ts <= 0 || !std::isfinite(point.price) || point.price <= 0.0 || point.price >= 1.0) continue;
            Observation o{floor_bucket(point.ts, cfg_.bar_seconds), logit(point.price), point.price};
            if (!s.obs.empty() && s.obs.back().ts == o.ts) s.obs.back() = o;
            else s.obs.push_back(o);
        }
        while (s.obs.size() > cap) s.obs.pop_front();
    }
}

void StatisticalModel::observe(const std::unordered_map<std::string, TokenMeta>& meta,
                               const std::unordered_map<std::string, BookSnapshot>& books) {
    const auto now_sec = std::chrono::duration_cast<std::chrono::seconds>(Clock::now().time_since_epoch()).count();
    const auto bucket = floor_bucket(now_sec, cfg_.bar_seconds);
    const std::size_t cap = std::max<std::size_t>(cfg_.lookback + 16, cfg_.min_history + 16);
    for (const auto& [token, book] : books) {
        if (!meta.contains(token)) continue;
        const double p = book.mid();
        if (!std::isfinite(p) || p <= 0.0 || p >= 1.0) continue;
        auto& s = history_[token];
        Observation o{bucket, logit(p), p};
        if (!s.obs.empty() && s.obs.back().ts == bucket) s.obs.back() = o;
        else if (s.obs.empty() || s.obs.back().ts < bucket) s.obs.push_back(o);
        while (s.obs.size() > cap) s.obs.pop_front();
    }
}

std::size_t StatisticalModel::history_points(const std::string& token_id) const {
    const auto it = history_.find(token_id);
    return it == history_.end() ? 0 : it->second.obs.size();
}

std::vector<std::string> StatisticalModel::liquid_universe(const std::unordered_map<std::string, TokenMeta>& meta) const {
    std::vector<std::pair<double, std::string>> ranked;
    ranked.reserve(meta.size());
    for (const auto& [token, m] : meta) {
        const auto hit = history_.find(token);
        if (hit == history_.end() || hit->second.obs.size() < cfg_.min_history + 1) continue;
        if (!m.active || m.closed || !m.enable_order_book || m.liquidity < cfg_.min_liquidity) continue;
        ranked.emplace_back(m.liquidity + 0.01 * m.volume, token);
    }
    std::sort(ranked.begin(), ranked.end(), [](const auto& a, const auto& b){ return a.first > b.first; });
    if (ranked.size() > cfg_.max_factor_universe) ranked.resize(cfg_.max_factor_universe);
    std::vector<std::string> out; out.reserve(ranked.size());
    for (auto& x : ranked) out.push_back(std::move(x.second));
    return out;
}

StatisticalModel::FitResult StatisticalModel::fit_pca(const std::vector<std::string>& requested_tokens,
                                                       bool ew, bool robust, std::size_t factors) const {
    FitResult fit;
    if (requested_tokens.size() < 4) return fit;
    std::int64_t end = 0;
    for (const auto& token : requested_tokens) {
        const auto it = history_.find(token);
        if (it != history_.end() && !it->second.obs.empty()) end = std::max(end, it->second.obs.back().ts);
    }
    if (end <= 0) return fit;

    auto build_aligned = [&](std::size_t points, std::vector<std::string>& tokens,
                             std::vector<std::vector<double>>& paths) {
        tokens.clear(); paths.clear();
        const auto bar = static_cast<std::int64_t>(std::max(1, cfg_.bar_seconds));
        const auto max_stale = static_cast<std::int64_t>(std::max(0, cfg_.max_stale_bars)) * bar;
        const auto start = end - static_cast<std::int64_t>(points - 1) * bar;
        for (const auto& token : requested_tokens) {
            const auto hit = history_.find(token);
            if (hit == history_.end() || hit->second.obs.empty()) continue;
            const auto& obs = hit->second.obs;
            if (end - obs.back().ts > max_stale) continue;
            std::vector<double> path(points, 0.0);
            std::size_t idx = 0;
            while (idx + 1 < obs.size() && obs[idx + 1].ts <= start) ++idx;
            bool ok = true;
            for (std::size_t k = 0; k < points; ++k) {
                const auto t = start + static_cast<std::int64_t>(k) * bar;
                while (idx + 1 < obs.size() && obs[idx + 1].ts <= t) ++idx;
                if (obs[idx].ts > t || t - obs[idx].ts > max_stale) { ok = false; break; }
                path[k] = obs[idx].logit;
            }
            if (ok) { tokens.push_back(token); paths.push_back(std::move(path)); }
        }
    };

    std::size_t points = cfg_.lookback + 1;
    std::vector<std::string> tokens;
    std::vector<std::vector<double>> paths;
    build_aligned(points, tokens, paths);
    if (tokens.size() < 4 && cfg_.lookback > cfg_.min_history) {
        points = cfg_.min_history + 1;
        build_aligned(points, tokens, paths);
    }
    if (tokens.size() < 4 || points < cfg_.min_history + 1) return fit;

    const std::size_t rows = points - 1, cols = tokens.size();
    arma::mat r(rows, cols, arma::fill::zeros);
    for (std::size_t j = 0; j < cols; ++j)
        for (std::size_t t = 0; t < rows; ++t) r(t, j) = paths[j][t + 1] - paths[j][t];

    arma::vec w(rows, arma::fill::ones);
    if (ew) {
        const double lambda = std::exp(-std::log(2.0) / std::max(1.0, cfg_.ew_half_life));
        double x = 1.0;
        for (std::size_t rr = rows; rr-- > 0;) { w(rr) = x; x *= lambda; }
    }
    w /= arma::accu(w);
    fit.means = w.t() * r;
    fit.scales.set_size(cols);
    arma::mat z(rows, cols, arma::fill::zeros);
    for (std::size_t j = 0; j < cols; ++j) {
        arma::vec d = r.col(j) - fit.means(j);
        const double s = std::sqrt(std::max(1e-10, arma::dot(w, arma::square(d))));
        fit.scales(j) = s; z.col(j) = d / s;
    }

    const std::size_t requested_k = factors == 0 ? cfg_.global_factors : factors;
    const std::size_t k = std::min<std::size_t>(requested_k, std::min(rows, cols) - 1);
    if (k == 0) return fit;
    arma::mat sparse(rows, cols, arma::fill::zeros), residuals(rows, cols, arma::fill::zeros);
    for (int iter = 0; iter < (robust ? 5 : 1); ++iter) {
        arma::mat clean = z - sparse, weighted = clean;
        for (std::size_t i = 0; i < rows; ++i) weighted.row(i) *= std::sqrt(w(i) * static_cast<double>(rows));
        arma::mat U, V; arma::vec sv;
        if (!arma::svd_econ(U, sv, V, weighted, "both", "std")) return fit;
        fit.loadings = V.cols(0, k - 1);
        const arma::mat low_rank = clean * fit.loadings * fit.loadings.t();
        residuals = z - low_rank;
        if (robust) {
            sparse = residuals;
            const double tau = cfg_.robust_threshold;
            sparse.transform([&](double v){ const double a=std::abs(v)-tau; return a>0.0?std::copysign(a,v):0.0; });
        }
    }

    const arma::mat innovation_z = robust ? sparse : residuals;
    arma::mat innovation_logit = innovation_z;
    for (std::size_t j = 0; j < cols; ++j) innovation_logit.col(j) *= fit.scales(j);
    const arma::mat states = arma::cumsum(innovation_logit, 0);
    fit.forecast_delta_logit = arma::rowvec(cols, arma::fill::zeros);
    fit.forecast_sigma_logit = arma::rowvec(cols, arma::fill::zeros);
    fit.state_z = arma::rowvec(cols, arma::fill::zeros);
    fit.rho = arma::rowvec(cols, arma::fill::zeros);
    fit.half_life_min = arma::rowvec(cols, arma::fill::zeros);
    fit.unit_root_t = arma::rowvec(cols, arma::fill::zeros);
    fit.valid_ou = arma::urowvec(cols, arma::fill::zeros);
    const double horizon_steps = std::max(1e-6, cfg_.horizon_min * 60.0 / static_cast<double>(std::max(1, cfg_.bar_seconds)));

    for (std::size_t j = 0; j < cols; ++j) {
        const arma::vec state = states.col(j);
        if (state.n_elem < 12) continue;
        const arma::vec x = state.head(state.n_elem - 1), y = state.tail(state.n_elem - 1);
        const double xbar=arma::mean(x), ybar=arma::mean(y), sxx=arma::accu(arma::square(x-xbar));
        if (!(sxx > 1e-12)) continue;
        const double rho=arma::dot(x-xbar,y-ybar)/sxx, a=ybar-rho*xbar;
        const arma::vec err=y-(a+rho*x);
        const double dof=std::max(1.0,static_cast<double>(err.n_elem)-2.0);
        const double sigma2=std::max(1e-12,arma::dot(err,err)/dof);
        const double se_rho=std::sqrt(sigma2/sxx);
        const double unit_root_t=se_rho>0.0?(rho-1.0)/se_rho:0.0;
        fit.rho(j)=rho; fit.unit_root_t(j)=unit_root_t;
        if (!(rho > cfg_.min_ou_rho && rho < cfg_.max_ou_rho)) continue;
        const double half_steps=-std::log(2.0)/std::log(rho);
        const double half_min=half_steps*static_cast<double>(cfg_.bar_seconds)/60.0;
        fit.half_life_min(j)=half_min;
        if (!std::isfinite(half_min)||half_min>cfg_.max_ou_half_life_min||unit_root_t>cfg_.max_ou_unit_root_t) continue;
        const double mu=a/(1.0-rho);
        const double stationary_sd=std::sqrt(sigma2/std::max(1e-8,1.0-rho*rho));
        const double current=state(state.n_elem-1);
        const double state_z=(current-mu)/std::max(1e-8,stationary_sd);
        fit.state_z(j)=state_z;
        if (std::abs(state_z)<cfg_.min_state_z) continue;
        const double rho_h=std::pow(rho,horizon_steps);
        fit.forecast_delta_logit(j)=(mu-current)*(1.0-rho_h);
        const double innovation_var_h=sigma2*(1.0-std::pow(rho,2.0*horizon_steps))/std::max(1e-8,1.0-rho*rho);
        fit.forecast_sigma_logit(j)=std::sqrt(std::max(1e-12,innovation_var_h));
        fit.valid_ou(j)=1u;
    }
    fit.residual_scale=std::sqrt(std::max(1e-8,arma::accu(arma::square(innovation_z))/static_cast<double>(innovation_z.n_elem)));
    fit.standardized_returns=std::move(z); fit.tokens=std::move(tokens); fit.ok=true;
    return fit;
}

std::vector<Signal> StatisticalModel::signals_from_fit(const FitResult& fit, SignalKind kind,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books, double blend_weight) const {
    std::vector<Signal> out;
    if (!fit.ok) return out;
    for (std::size_t j=0;j<fit.tokens.size();++j) {
        if (fit.valid_ou.n_elem<=j||fit.valid_ou(j)==0u) continue;
        const auto& token=fit.tokens[j];
        const auto mit=meta.find(token), bit=books.find(token), hit=history_.find(token);
        if (mit==meta.end()||bit==books.end()||hit==history_.end()||hit->second.obs.empty()) continue;
        const double ask=bit->second.best_ask(), bid=bit->second.best_bid();
        if (!std::isfinite(ask)||!std::isfinite(bid)||ask<=0.0||ask>=1.0) continue;
        const double delta_logit=fit.forecast_delta_logit(j)*blend_weight;
        if (!(delta_logit>0.0)) continue;
        const double fair=logistic(hit->second.obs.back().logit+delta_logit), gross=fair-ask;
        if (gross<=0.0) continue;
        const double fee=taker_fee_usdc(1.0,ask,mit->second.category), micro=0.25*bit->second.spread();
        const double forecast_sigma_price=fit.forecast_sigma_logit(j)*fair*(1.0-fair);
        const double parameter_floor=fit.residual_scale*fit.scales(j)*fair*(1.0-fair)/
                                     std::sqrt(std::max(1.0,static_cast<double>(fit.standardized_returns.n_rows)));
        const double uncertainty=std::max(1e-5,forecast_sigma_price+parameter_floor);
        const double cost=fee+micro, net=gross-cost-cfg_.uncertainty_penalty*uncertainty;
        if (net<=0.0) continue;
        Signal s; s.kind=kind; s.token_id=token; s.market_id=mit->second.market_id; s.event_id=mit->second.event_id;
        s.question=mit->second.question+" ["+mit->second.outcome+"]"; s.side=Side::Buy; s.observed_price=ask;
        s.fair_price=fair; s.gross_edge=gross; s.est_cost=cost; s.uncertainty=uncertainty; s.net_edge=net;
        const double mr_strength=std::clamp(-fit.unit_root_t(j)/3.0,0.25,2.0);
        s.score=mr_strength*net/std::max(1e-4,uncertainty+bit->second.spread());
        s.expected_half_life_min=fit.half_life_min(j);
        s.confidence=std::clamp(1.0-std::exp(-std::max(0.0,s.score)),0.0,0.999);
        std::ostringstream why; why.setf(std::ios::fixed); why.precision(2);
        why<<to_string(kind)<<" OU residual: z="<<fit.state_z(j)<<", rho="<<fit.rho(j)
           <<", half-life="<<fit.half_life_min(j)<<"m, unit-root t="<<fit.unit_root_t(j);
        s.rationale=why.str(); out.push_back(std::move(s));
    }
    return out;
}

std::vector<Signal> StatisticalModel::hierarchical_signals(const std::vector<std::string>& universe,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books) const {
    std::vector<Signal> out;
    const auto global=fit_pca(universe,true,false);
    if (!global.ok) return out;
    std::unordered_map<std::string,std::size_t> global_index;
    for (std::size_t j=0;j<global.tokens.size();++j) global_index[global.tokens[j]]=j;
    std::map<std::string,std::vector<std::string>> groups;
    for (const auto& token:universe) { const auto it=meta.find(token); if(it!=meta.end()) groups[it->second.category].push_back(token); }
    for (auto& [_,tokens]:groups) {
        if(tokens.size()<std::max<std::size_t>(6,cfg_.local_factors+2)) continue;
        auto local=fit_pca(tokens,true,true,cfg_.local_factors);
        if(!local.ok) continue;
        for(std::size_t j=0;j<local.tokens.size();++j){
            const auto git=global_index.find(local.tokens[j]); if(git==global_index.end()) continue;
            const std::size_t g=git->second; const bool lv=local.valid_ou(j)!=0u, gv=global.valid_ou(g)!=0u;
            if(!lv&&!gv){local.valid_ou(j)=0u;continue;}
            if(lv&&gv){
                local.forecast_delta_logit(j)=0.5*local.forecast_delta_logit(j)+0.5*global.forecast_delta_logit(g);
                local.forecast_sigma_logit(j)=std::sqrt(0.25*std::pow(local.forecast_sigma_logit(j),2)+0.25*std::pow(global.forecast_sigma_logit(g),2));
                local.state_z(j)=0.5*local.state_z(j)+0.5*global.state_z(g);
                local.half_life_min(j)=0.5*local.half_life_min(j)+0.5*global.half_life_min(g);
                local.unit_root_t(j)=std::min(local.unit_root_t(j),global.unit_root_t(g));
                local.rho(j)=0.5*local.rho(j)+0.5*global.rho(g);
            } else if(gv){
                local.forecast_delta_logit(j)=global.forecast_delta_logit(g); local.forecast_sigma_logit(j)=global.forecast_sigma_logit(g);
                local.state_z(j)=global.state_z(g); local.half_life_min(j)=global.half_life_min(g);
                local.unit_root_t(j)=global.unit_root_t(g); local.rho(j)=global.rho(g); local.valid_ou(j)=1u;
            }
        }
        auto sig=signals_from_fit(local,SignalKind::HierarchicalPca,meta,books,1.0);
        out.insert(out.end(),sig.begin(),sig.end());
    }
    return out;
}

} // namespace poly
