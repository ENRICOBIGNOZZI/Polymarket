// Zero-authority research reader for normalized external venue tapes.
#include "pm/v7_external_tape.hpp"
#include "pm/v7_external_fair.hpp"

#include <array>
#include <charconv>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

using namespace pm::v7::external_fair;

namespace {
bool parse_i64(const std::string& text, std::int64_t& out) {
    const auto r=std::from_chars(text.data(),text.data()+text.size(),out);
    return r.ec==std::errc{} && r.ptr==text.data()+text.size();
}
bool valid_header(const TapeSessionHeader& h) {
    const std::array<char,8> magic{'P','M','V','7','T','A','P','E'};
    return h.magic==magic && h.schema_version==kExternalTapeSchemaVersion
        && h.record_bytes==sizeof(TapeRecord);
}
}

int main(int argc,char** argv) {
    std::int64_t start=0,end=0;
    std::vector<std::filesystem::path> paths;
    for(int i=1;i<argc;++i) {
        const std::string arg=argv[i];
        if(arg=="--start-wall-ns" && i+1<argc) {
            if(!parse_i64(argv[++i],start)) return 2;
        } else if(arg=="--end-wall-ns" && i+1<argc) {
            if(!parse_i64(argv[++i],end)) return 2;
        } else if(arg=="--tape" && i+1<argc) {
            paths.emplace_back(argv[++i]);
        } else {
            std::cerr<<"usage: v7_external_event_export --start-wall-ns N --end-wall-ns N --tape PATH...\n";
            return 2;
        }
    }
    if(start<=0 || end<=start || paths.empty()) return 2;
    std::cout<<std::setprecision(17);
    for(const auto& path:paths) {
        std::ifstream in(path,std::ios::binary);
        if(!in) continue;
        TapeSessionHeader h{};
        in.read(reinterpret_cast<char*>(&h),sizeof(h));
        if(!in || !valid_header(h)) {
            std::cerr<<"invalid_header "<<path.string()<<"\n";
            return 3;
        }
        TapeRecord rec{};
        while(in.read(reinterpret_cast<char*>(&rec),sizeof(rec))) {
            if(rec.kind!=TapeRecordKind::ExternalVenueEvent) continue;
            ExternalVenueEvent e{};
            if(!decode_tape_payload(rec,e)) continue;
            if(e.local_receive_wall_ns<start || e.local_receive_wall_ns>end) continue;
            if(e.venue!=VenueId::BinanceSpot && e.venue!=VenueId::CoinbaseSpot
                    && e.venue!=VenueId::BybitSpot) continue;
            double price=0.0;
            if(e.event_type==ExternalEventType::Trade && e.trade_price>0.0) {
                price=e.trade_price;
            } else if(e.event_type==ExternalEventType::BookTop
                    && e.bid>0.0 && e.ask>e.bid) {
                price=0.5*(e.bid+e.ask);
            } else {
                continue;
            }
            std::cout<<e.local_receive_wall_ns<<','
                     <<static_cast<unsigned>(e.venue)<<','
                     <<static_cast<unsigned>(e.event_type)<<','
                     <<e.connection_epoch<<','
                     <<price<<'\n';
        }
        if(!in.eof()) {
            // Active .open segments may end with a partial record. The prefix
            // is still durable and causal; closed segments must be exact.
            const auto name=path.string();
            if(name.size()<5 || name.substr(name.size()-5)!=".open") {
                std::cerr<<"partial_closed_tape "<<name<<"\n";
                return 4;
            }
        }
    }
    return 0;
}
