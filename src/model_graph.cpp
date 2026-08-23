#include "poly/model.hpp"
#include "poly/math.hpp"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <tuple>
#include <unordered_set>

namespace poly {

std::vector<Signal> StatisticalModel::graph_signals(
        const std::vector<std::string>& universe,
        const std::unordered_map<std::string, TokenMeta>& meta,
        const std::unordered_map<std::string, BookSnapshot>& books) const {
    if (universe.size() < 6) return {};
    static const std::unordered_set<std::string> stop={"will","what","when","where","which","with","from","this","that","have","before","after","above","below","more","less","than","over","under","between","market","polymarket","yes","outcome","does","into","during","there","their","they","them","2025","2026","2027"};
    auto tokenize=[&](const std::string& text){
        std::unordered_set<std::string> out; std::string w;
        auto flush=[&]{if(w.size()>=4&&!stop.contains(w))out.insert(w);w.clear();};
        for(unsigned char ch:text){if(std::isalnum(ch))w.push_back(static_cast<char>(std::tolower(ch)));else flush();} flush(); return out;
    };
    const std::size_t n=universe.size();
    std::vector<std::unordered_set<std::string>> words(n); std::unordered_map<std::string,std::vector<std::size_t>> inverted;
    std::vector<double> latest_ret(n,0.0); std::vector<bool> valid(n,false);
    for(std::size_t i=0;i<n;++i){
        const auto mit=meta.find(universe[i]),hit=history_.find(universe[i]);
        if(mit==meta.end()||hit==history_.end()||hit->second.obs.size()<2)continue;
        words[i]=tokenize(mit->second.question+" "+mit->second.event_title+" "+mit->second.category);
        for(const auto&w:words[i])inverted[w].push_back(i);
        latest_ret[i]=hit->second.obs.back().logit-hit->second.obs[hit->second.obs.size()-2].logit; valid[i]=true;
    }
    struct GRes{bool ok{false};double residual{0.0};std::size_t neighbors{0};};
    std::vector<GRes> gr(n); std::vector<double> pool; pool.reserve(n);
    for(std::size_t i=0;i<n;++i){
        if(!valid[i]||words[i].empty())continue; std::unordered_map<std::size_t,int> common;
        for(const auto&w:words[i]){const auto it=inverted.find(w);if(it==inverted.end())continue;for(auto j:it->second)if(j!=i)++common[j];}
        std::vector<std::pair<double,std::size_t>> neighbors;
        for(const auto&[j,c]:common){
            if(!valid[j]||meta.at(universe[j]).market_id==meta.at(universe[i]).market_id)continue;
            const double denom=static_cast<double>(words[i].size()+words[j].size()-static_cast<std::size_t>(c)); if(denom<=0.0)continue;
            double sim=static_cast<double>(c)/denom;
            if(!meta.at(universe[i]).event_id.empty()&&meta.at(universe[i]).event_id==meta.at(universe[j]).event_id)sim+=0.75;
            if(meta.at(universe[i]).category==meta.at(universe[j]).category)sim+=0.08;
            if(sim>=cfg_.graph_min_similarity)neighbors.emplace_back(sim,j);
        }
        std::sort(neighbors.begin(),neighbors.end(),[](const auto&a,const auto&b){return a.first>b.first;});
        if(neighbors.size()>cfg_.graph_neighbors)neighbors.resize(cfg_.graph_neighbors); if(neighbors.size()<2)continue;
        double ws=0.0,mv=0.0;for(const auto&[ww,j]:neighbors){ws+=ww;mv+=ww*latest_ret[j];} if(ws<=0.0)continue;
        const double residual=latest_ret[i]-mv/ws;gr[i]={true,residual,neighbors.size()};pool.push_back(residual);
    }
    double scale=mad(pool);if(!(scale>1e-8)){double ss=0.0;for(double x:pool)ss+=x*x;scale=pool.empty()?1.0:std::sqrt(ss/static_cast<double>(pool.size()));}scale=std::max(scale,1e-6);
    const double rev=1.0-std::exp(-std::log(2.0)*cfg_.horizon_min/std::max(1.0,cfg_.graph_reversion_half_life_min));
    std::vector<Signal> out;
    for(std::size_t i=0;i<n;++i){
        if(!gr[i].ok||gr[i].residual>=0.0)continue;const auto&token=universe[i];const auto bit=books.find(token),hit=history_.find(token);
        if(bit==books.end()||hit==history_.end())continue;const double ask=bit->second.best_ask(),bid=bit->second.best_bid();
        if(!std::isfinite(ask)||!std::isfinite(bid)||ask<=0.0||ask>=1.0)continue;
        const double fair=logistic(hit->second.obs.back().logit-rev*gr[i].residual),gross=fair-ask;if(gross<=0.0)continue;
        const auto&m=meta.at(token);const double cost=taker_fee_usdc(1.0,ask,m.category)+0.25*bit->second.spread();
        const double uncertainty=scale*ask*(1.0-ask)/std::sqrt(static_cast<double>(gr[i].neighbors));
        const double net=gross-cost-cfg_.uncertainty_penalty*uncertainty;if(net<=0.0)continue;
        Signal s;s.kind=SignalKind::GraphResidual;s.token_id=token;s.market_id=m.market_id;s.event_id=m.event_id;s.question=m.question+" ["+m.outcome+"]";s.side=Side::Buy;
        s.observed_price=ask;s.fair_price=fair;s.gross_edge=gross;s.est_cost=cost;s.uncertainty=uncertainty;s.net_edge=net;
        s.score=net/std::max(1e-4,uncertainty+bit->second.spread());s.expected_half_life_min=cfg_.graph_reversion_half_life_min;
        s.confidence=std::clamp(1.0-std::exp(-std::max(0.0,s.score)),0.0,0.999);s.rationale="semantic/event graph relative move vs "+std::to_string(gr[i].neighbors)+" neighbors";out.push_back(std::move(s));
    }
    return out;
}

std::vector<Signal> StatisticalModel::generate(const std::unordered_map<std::string,TokenMeta>&meta,
                                                const std::unordered_map<std::string,BookSnapshot>&books){
    std::vector<Signal>out;const auto universe=liquid_universe(meta);if(universe.size()<4)return out;
    for(const auto&[kind,ew,robust]:std::vector<std::tuple<SignalKind,bool,bool>>{{SignalKind::Pca,false,false},{SignalKind::EwPca,true,false},{SignalKind::RobustPca,true,true}}){
        auto fit=fit_pca(universe,ew,robust);auto sig=signals_from_fit(fit,kind,meta,books);out.insert(out.end(),sig.begin(),sig.end());
    }
    auto h=hierarchical_signals(universe,meta,books);out.insert(out.end(),h.begin(),h.end());
    auto g=graph_signals(universe,meta,books);out.insert(out.end(),g.begin(),g.end());
    std::sort(out.begin(),out.end(),[](const Signal&a,const Signal&b){return a.score>b.score;});return out;
}

} // namespace poly
