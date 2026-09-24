#include "pm/v7_exact_arb_graph_shadow.hpp"
#include "pm/v7_exact_arb_ws_replay.hpp"
#include "pm/v7_exact_arb_evidence_json.hpp"
#include <cassert>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <thread>

thread_local std::size_t allocations = 0;
void* operator new(std::size_t size) { ++allocations; if (auto* p=std::malloc(size ? size : 1)) return p; throw std::bad_alloc(); }
void* operator new[](std::size_t size) { return ::operator new(size); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }

using namespace pm::v7;
using namespace pm::v7::exact_arb_graph;
namespace json=boost::json;
std::string read(const std::filesystem::path& path) { std::ifstream f(path); std::ostringstream s; s<<f.rdbuf(); return s.str(); }

int main() {
    std::string temporary=(std::filesystem::temp_directory_path()/"pm-ws-replay-test-XXXXXX").string();
    assert(mkdtemp(temporary.data()));
    const std::filesystem::path root(temporary);
    const std::string model(40,'a');
    const std::string snapshot=R"([{"event_type":"book","asset_id":"1001","timestamp":1000,"bids":[{"price":"0.39","size":"5"}],"asks":[{"price":"0.40","size":"5"},{"price":"0.41","size":"6"}]}])";
    std::vector<std::string> expected;
    {
        NativeGraphShadow shadow(root,model,"raw-session",{{"1001",1,100,1,10000,1000000,0,1}});
        MarketWsShard decoder({{"1001",1,1,1,100}});
        auto events=std::make_unique<std::array<MarketWsEvent,512>>();
        std::int64_t now=1000000000;
        std::uint64_t epoch=1;
        auto feed=[&](const std::string& payload) {
            pm::fast::FeedReceiveStamp clock; clock.wall_ms=1000; clock.monotonic_ns=++now;
            auto result=decoder.process_frame(payload,clock,*events);
            const auto before=allocations;
            shadow.on_frame(decoder,{events->data(),result.output_count},{1000,now,now+1,now},epoch,
                !result.invalid_frame && !result.output_overflow && !result.arena_exhausted,payload);
            assert(allocations==before); // graph inactive still captures ALL frames without allocating
            expected.push_back(payload);
        };
        feed(snapshot); shadow.drain(false);
        const auto first_version=decoder.deep_snapshot(1).state_version;
        feed(R"({"event_type":"price_change","timestamp":1001,"price_changes":[{"asset_id":"1001","side":"SELL","price":"0.40","size":"3"},{"asset_id":"1001","side":"SELL","price":"0.41","size":"7"}]})");
        shadow.drain(false);
        feed("{"); shadow.drain(false);
        decoder.invalidate_all_lineage(); ++epoch;
        feed(snapshot); shadow.drain(false);
        // Concurrent byte-ring wrap: fixed small descriptor queue, variable-size
        // bytes, writer and feed on separate threads. No source graph required.
        const auto large=snapshot+std::string(32768,' ');
        std::atomic<bool> done{false};
        std::thread producer([&] { for (int i=0;i<700;++i) feed(large); done.store(true,std::memory_order_release); });
        while (!done.load(std::memory_order_acquire)) { shadow.drain(false); std::this_thread::yield(); }
        producer.join();
        for (int i=0;i<5;++i) shadow.drain(false);
        shadow.write_status(1000);
        auto status=json::parse(read(root/"native_exact_arb_status.json")).as_object();
        const auto digest=std::string(status.at("session_manifest_sha256").as_string());
        const auto manifest=read(root/"native_sessions"/(digest+".json"));
        NativeWsReplay replay(manifest,model), again(manifest,model);
        std::ifstream tape(root/"native_exact_arb_ws_frames.jsonl");
        std::vector<std::string> rows;
        for(std::string line;std::getline(tape,line);) {
            const auto frame=json::parse(line).as_object();
            const auto sequence=json::value_to<std::uint64_t>(frame.at("feed_frame_sequence"));
            assert(std::string(frame.at("payload").as_string())==expected.at(sequence-1));
            const auto result=replay.ingest(line);
            assert(result==again.ingest(line));
            const auto output=json::parse(result).as_object();
            assert(output.at("receive_wall_ms")==frame.at("receive_wall_ms"));
            if (sequence==1) assert(json::value_to<std::uint64_t>(output.at("books").as_array()[0].as_object().at("state_version"))==first_version);
            if (sequence==2) {
                const auto& books=output.at("books").as_array(); assert(books.size()==1);
                const auto& asks=books[0].as_object().at("asks_e4_microshares").as_array();
                assert(json::value_to<std::int64_t>(asks[0].as_array()[1])==3000000);
                assert(json::value_to<std::int64_t>(asks[1].as_array()[1])==7000000);
            }
            if (sequence==3) assert(output.at("source_frame_valid").as_bool()==false && output.at("books").as_array().empty());
            rows.push_back(line);
        }
        assert(replay.receipt()==again.receipt());
        assert(json::value_to<std::uint64_t>(status.at("ws_frames_written"))==rows.size());
        assert(json::value_to<std::uint64_t>(status.at("ws_frames_written"))+json::value_to<std::uint64_t>(status.at("ws_frames_dropped"))==expected.size());
        assert(rows.size()>=4);
        // Mutation, duplicates and clocks fail the session, not just one row.
        auto reject=[&](json::object bad,const char* reason) {
            NativeWsReplay engine(manifest,model);
            (void)engine.ingest(rows[0]);
            bool failed=false;
            try { (void)engine.ingest(json::serialize(bad)); } catch(const std::exception& e) { failed=std::string(e.what()).find(reason)!=std::string::npos; }
            assert(failed);
            assert(json::parse(engine.receipt()).as_object().at("state").as_string()=="FAILED");
        };
        auto bad=json::parse(rows[1]).as_object(); bad["payload"]="[]"; reject(bad,"payload_digest");
        bad=json::parse(rows[1]).as_object(); bad["receive_monotonic_ns"]=1; reject(bad,"clock_inversion");
        bad=json::parse(rows[0]).as_object(); reject(bad,"sequence_reversal");
        bad=json::parse(rows[1]).as_object(); bad["decoded_events"]=511; reject(bad,"decoder_divergence");
        bad=json::parse(rows[1]).as_object(); bad["feed_frame_sequence"]=3;
        NativeWsReplay gap(manifest,model); (void)gap.ingest(rows[0]);
        const auto missing=json::parse(gap.ingest(json::serialize(bad))).as_object();
        assert(json::value_to<int>(missing.at("missing_frames_before"))==1);
        assert(!missing.at("source_state_versions_comparable").as_bool());
        assert(!missing.at("books").as_array()[0].as_object().at("lineage_continuous").as_bool());
        // Oversized frames and disk suppression remain distinct loss counters.
        const std::string oversized(1024*1024+1,' ');
        shadow.on_frame(decoder,{}, {1000,++now,now+1,now},epoch,false,oversized);
        feed(snapshot); shadow.drain(true); shadow.write_status(1000);
        status=json::parse(read(root/"native_exact_arb_status.json")).as_object();
        assert(json::value_to<int>(status.at("ws_frames_oversized"))==1);
        assert(json::value_to<int>(status.at("ws_frames_disk_suppressed"))==1);
    }
    std::filesystem::remove_all(root);
}
