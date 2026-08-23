#include "pm/engine.hpp"
#include "pm/maker.hpp"
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
void usage(){
    std::cout << "polymarket_engine --config FILE [--once|--loop] [--paper] [--scan-only] "
                 "[--markets N] [--min-liquidity X] [--run-dir DIR]\n";
}
}

int main(int argc,char**argv){
    try{
        std::string config="config/paper.example.json";
        bool once=true,loop=false,paper=false,scan=false;
        std::optional<std::size_t> markets;
        std::optional<double> min_liq;
        std::optional<std::string> run_dir;
        for(int i=1;i<argc;++i){
            std::string a=argv[i];
            auto next=[&](){if(i+1>=argc)throw std::runtime_error("Missing value after "+a);return std::string(argv[++i]);};
            if(a=="--config")config=next();
            else if(a=="--once"){once=true;loop=false;}
            else if(a=="--loop"){loop=true;once=false;}
            else if(a=="--paper")paper=true;
            else if(a=="--scan-only")scan=true;
            else if(a=="--markets")markets=static_cast<std::size_t>(std::stoull(next()));
            else if(a=="--min-liquidity")min_liq=std::stod(next());
            else if(a=="--run-dir")run_dir=next();
            else if(a=="--help"||a=="-h"){usage();return 0;}
            else throw std::runtime_error("Unknown argument: "+a);
        }

        auto cfg=pm::Engine::load_config(config);
        pm::MakerPaperController::apply_config(cfg,config);
        if(markets)cfg.market_limit=*markets;
        if(min_liq)cfg.min_liquidity=*min_liq;
        if(run_dir)cfg.run_dir=*run_dir;
        if(scan)cfg.scan_only=true;

        auto tick=[&](){
            {
                pm::Engine engine(cfg);
                engine.run_once(paper,scan);
            }
            {
                pm::MakerPaperController maker(cfg);
                maker.run_once(paper&&!scan&&!cfg.scan_only);
            }
        };

        if(loop){
            for(;;){
                try{tick();}
                catch(const std::exception&e){std::cerr<<"tick error: "<<e.what()<<"\n";}
                std::this_thread::sleep_for(std::chrono::seconds(std::max(1,cfg.interval_seconds)));
            }
        }else if(once){
            tick();
        }
        return 0;
    }catch(std::exception const&e){std::cerr<<"fatal: "<<e.what()<<"\n";return 1;}
}
