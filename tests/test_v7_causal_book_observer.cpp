// Exercise the production decoder, feature lane, queue and JSON producer.
#define main v7_fillability_observer_program_main
#include "../src/v7_maker_fillability_observer.cpp"
#undef main

#include <cassert>

int main() {
    const auto directory = fs::temp_directory_path() / ("v7-book-test-" + std::to_string(::getpid()));
    fs::create_directories(directory);
    {
        ExactWsObserver observer({SelectedToken{"market", "event", "yes", 1, 1, 1, 100}},
                                 "wss://unused.example", directory, std::string(40, 'a'));
        const auto snapshot = [](std::int64_t timestamp) {
            return "{\"event_type\":\"book\",\"asset_id\":\"yes\",\"timestamp\":" + std::to_string(timestamp)
                + ",\"bids\":[{\"price\":\"0.48\",\"size\":\"10\"}],"
                  "\"asks\":[{\"price\":\"0.52\",\"size\":\"20\"}]}";
        };
        auto send = [&](std::string payload, std::int64_t offset) {
            pm::fast::FeedReceiveStamp stamp;
            stamp.wall_ms = 1'700'000'000'000 + offset;
            stamp.monotonic_ns = 1'000'000'000 + offset * 1'000'000;
            observer.on_frame(payload, stamp);
            observer.drain();
        };
        send(snapshot(1'700'000'000'000), 0);
        send(snapshot(1'700'000'001'100), 1100);
        send(R"({"event_type":"last_trade_price","asset_id":"yes","timestamp":1700000001200,"side":"SELL","price":"0.48","size":"2"})", 1200);
        observer.write_status();
        auto read_last = [&](const fs::path& path) {
            std::ifstream stream(path); std::string line, last;
            while (std::getline(stream, line)) last = line;
            return json::parse(last).as_object();
        };
        const auto row = read_last(directory / "book_observations" / "current.jsonl");
        assert(row.at("features_valid").as_bool());
        const auto& print = row.at("public_trade").as_object();
        assert(print.at("aggressor_side").as_string() == "SELL");
        assert(print.at("size").as_double() == 2.0);
        assert(print.at("exchange_event_ns").as_int64() == 1'700'000'001'200'000'000LL);
        const auto& features = row.at("placement_features").as_object();
        assert(std::abs(features.at("spread_ticks").as_double() - 4.0) < 1e-12);
        assert(std::abs(features.at("imbalance").as_double() + 1.0/3.0) < 1e-12);
        assert(features.at("aggressive_sell_prints_per_second").as_double() > 0);
        assert(features.at("trade_intensity").as_double() > 0);
        assert(features.at("inventory_fraction").is_null());
        assert(row.at("execution_authority").as_string() == "ZERO_AUTHORITY_RESEARCH_ONLY");
        assert(read_last(directory / "fillability_ws.jsonl").at("size").as_double() == 2.0);
        observer.on_reconnect();
        send(snapshot(1'700'000'001'300), 1300);
        const auto reset = read_last(directory / "book_observations" / "current.jsonl");
        assert(!reset.at("features_valid").as_bool());
        assert(reset.at("public_trade").is_null());
        assert(reset.at("connection_epoch").as_int64() == 2);
        observer.stop();
    }
    const auto selection = directory / "selection.json";
    { std::ofstream out(selection); out << R"({"timestamp_ms":1,"markets":[{"market_id":"m","event_id":"e","yes_token":"y","no_token":"n","price":0.4}]})"; }
    const auto before = load_selected_pairs(selection);
    { std::ofstream out(selection); out << R"({"timestamp_ms":2,"markets":[{"market_id":"m","event_id":"e","yes_token":"y","no_token":"n","price":0.5}]})"; }
    assert(load_selected_pairs(selection) == before);
    { std::ofstream out(selection); out << R"({"timestamp_ms":3,"markets":[{"market_id":"m2","event_id":"e2","yes_token":"y2","no_token":"n2"}]})"; }
    assert(load_selected_pairs(selection) != before);
    fs::remove_all(directory);
}
