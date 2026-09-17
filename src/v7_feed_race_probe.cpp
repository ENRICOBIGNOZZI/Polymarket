#include "pm/v7_external_ws.hpp"
#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <iostream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
#if !defined(__APPLE__)
#include <stop_token>
#endif

namespace json = boost::json;
using namespace pm::v7::external_fair;
using namespace std::chrono_literals;
namespace {
constexpr std::array<const char*, 3> kNames{"BINANCE_9443", "BINANCE_443", "BINANCE_VISION_443"};
constexpr std::array<const char*, 3> kSymbols{"BTCUSDT", "ETHUSDT", "SOLUSDT"};
using Key = std::tuple<int, int, std::uint64_t>;
struct Record {
    Key key;
    std::int64_t receive_ns = 0;
    std::string fingerprint;
};
std::int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
int positive(std::string_view value, int minimum, int maximum) {
    int out = 0;
    auto result = std::from_chars(value.data(), value.data() + value.size(), out);
    if (result.ec != std::errc{} || result.ptr != value.data() + value.size() || out < minimum || out > maximum)
        throw std::invalid_argument("argument outside bounded range");
    return out;
}
std::uint64_t sequence(const json::value& value) {
    if (value.is_uint64() && value.as_uint64() > 0) return value.as_uint64();
    if (value.is_int64() && value.as_int64() > 0) return static_cast<std::uint64_t>(value.as_int64());
    throw std::invalid_argument("positive integer sequence required");
}
json::object distribution(std::vector<double> values) {
    if (values.empty()) return {{"count", 0}, {"p50", nullptr}, {"p95", nullptr}, {"p99", nullptr}, {"min", nullptr}, {"max", nullptr}};
    std::sort(values.begin(), values.end());
    auto q = [&](double p) { return values[static_cast<std::size_t>(std::ceil(p * values.size())) - 1]; };
    return {{"count", values.size()}, {"p50", q(.50)}, {"p95", q(.95)}, {"p99", q(.99)}, {"min", values.front()}, {"max", values.back()}};
}
class Recorder final : public ExternalFrameObserver {
public:
    Recorder(std::atomic<std::int64_t>& begin, std::int64_t duration_ns, std::size_t limit)
        : begin_(begin), duration_ns_(duration_ns), limit_(limit) { records.reserve(limit); }
    std::vector<Record> records;
    std::atomic<bool> market_seen{false};
    std::uint64_t invalid = 0, overflow = 0;
    void on_frame(std::uint64_t, std::int64_t receive_ns, std::int64_t, std::string_view payload) noexcept override {
        try {
            if (payload.size() > 16384) { ++invalid; return; }
            boost::system::error_code error;
            auto value = json::parse(payload, error);
            if (error || !value.is_object()) { ++invalid; return; }
            const auto& root = value.as_object();
            const auto* symbol_value = root.if_contains("s");
            if (!symbol_value || !symbol_value->is_string()) return; // subscription/control frame
            int symbol = -1;
            for (int i = 0; i < 3; ++i) if (symbol_value->as_string() == kSymbols[i]) symbol = i;
            if (symbol < 0) return;
            int kind = -1;
            const auto* event = root.if_contains("e");
            if (event && event->is_string() && event->as_string() == "aggTrade") kind = 1;
            else if (root.if_contains("u") && root.if_contains("b") && root.if_contains("a")) kind = 0;
            if (kind < 0) return;
            const auto id = sequence(root.at(kind == 0 ? "u" : "a"));
            market_seen.store(true, std::memory_order_release);
            const auto begin = begin_.load(std::memory_order_acquire);
            if (begin == 0 || receive_ns < begin || receive_ns - begin >= duration_ns_) return;
            if (records.size() >= limit_) { ++overflow; return; }
            // Exact field representation, not a hash: conflicting copies of
            // the same exchange identity are excluded from timing comparisons.
            std::string fingerprint;
            if (kind == 0) {
                for (const auto* key : {"b", "B", "a", "A"}) fingerprint += json::serialize(root.at(key)) + "|";
            } else {
                for (const auto* key : {"p", "q", "f", "l", "m", "E", "T"}) fingerprint += json::serialize(root.at(key)) + "|";
            }
            if (fingerprint.size() > 1024) { ++invalid; return; }
            records.push_back({Key{symbol, kind, id}, receive_ns, std::move(fingerprint)});
        } catch (...) { ++invalid; }
    }
private:
    std::atomic<std::int64_t>& begin_;
    std::int64_t duration_ns_;
    std::size_t limit_;
};
struct Group {
    std::array<const Record*, 3> record{};
    bool conflict = false;
};
struct Aggregate {
    std::uint64_t unique = 0, matched = 0, conflicting = 0;
    std::array<std::uint64_t, 3> present{}, first{}, duplicate{};
    std::array<std::vector<double>, 3> penalty, saving;
    std::vector<double> first_of_three_saving_vs_primary;
    std::vector<double> quorum_two_of_three_saving_vs_primary;
    std::vector<double> first_to_quorum_delay;
    std::vector<double> first_to_last_spread;
};
json::array reduce(const std::array<const std::vector<Record>*, 3>& sources) {
    std::map<Key, Group> groups;
    std::array<Aggregate, 6> stats;
    for (std::size_t source = 0; source < sources.size(); ++source) {
        for (const auto& record : *sources[source]) {
            const auto symbol = std::get<0>(record.key), kind = std::get<1>(record.key);
            if (symbol < 0 || symbol >= 3 || kind < 0 || kind >= 2) throw std::invalid_argument("invalid reduction key");
            auto& group = groups[record.key];
            auto*& current = group.record[source];
            if (current) {
                ++stats[symbol * 2 + kind].duplicate[source];
                if (current->fingerprint != record.fingerprint) group.conflict = true;
            }
            if (!current || record.receive_ns < current->receive_ns) current = &record;
        }
    }
    for (const auto& [key, group] : groups) {
        auto& stat = stats[std::get<0>(key) * 2 + std::get<1>(key)];
        ++stat.unique;
        for (std::size_t i = 0; i < 3; ++i) if (group.record[i]) ++stat.present[i];
        if (!group.record[0] || !group.record[1] || !group.record[2]) continue;
        if (group.conflict || group.record[0]->fingerprint != group.record[1]->fingerprint
            || group.record[0]->fingerprint != group.record[2]->fingerprint) {
            ++stat.conflicting;
            continue;
        }
        ++stat.matched;
        std::array<std::int64_t, 3> arrivals{
            group.record[0]->receive_ns, group.record[1]->receive_ns, group.record[2]->receive_ns};
        auto ordered = arrivals;
        std::sort(ordered.begin(), ordered.end());
        const auto earliest = ordered[0];
        const auto quorum = ordered[1];
        const auto latest = ordered[2];
        stat.first_of_three_saving_vs_primary.push_back(
            static_cast<double>(arrivals[0] - earliest) / 1000.0);
        stat.quorum_two_of_three_saving_vs_primary.push_back(
            static_cast<double>(arrivals[0] - quorum) / 1000.0);
        stat.first_to_quorum_delay.push_back(
            static_cast<double>(quorum - earliest) / 1000.0);
        stat.first_to_last_spread.push_back(
            static_cast<double>(latest - earliest) / 1000.0);
        for (std::size_t i = 0; i < 3; ++i) {
            if (arrivals[i] == earliest) ++stat.first[i];
            stat.penalty[i].push_back(static_cast<double>(arrivals[i] - earliest) / 1000.0);
            stat.saving[i].push_back(static_cast<double>(arrivals[0] - arrivals[i]) / 1000.0);
        }
    }
    json::array output;
    for (std::size_t s = 0; s < 6; ++s) {
        auto& stat = stats[s];
        json::array endpoints;
        for (std::size_t i = 0; i < 3; ++i) {
            endpoints.emplace_back(json::object{
                {"endpoint", kNames[i]}, {"unique_observed", stat.present[i]},
                {"missing_from_union", stat.unique - stat.present[i]}, {"duplicate_copies", stat.duplicate[i]},
                {"first_arrivals_on_matched", stat.first[i]},
                {"arrival_penalty_to_first_us", distribution(std::move(stat.penalty[i]))},
                {"paired_saving_vs_9443_us", distribution(std::move(stat.saving[i]))}});
        }
        output.emplace_back(json::object{
            {"symbol", kSymbols[s / 2]}, {"stream", s % 2 == 0 ? "bookTicker" : "aggTrade"},
            {"unique_union", stat.unique}, {"identical_matched_all_endpoints", stat.matched},
            {"conflicting_matched_identities", stat.conflicting},
            {"matched_coverage", stat.unique ? json::value(static_cast<double>(stat.matched) / stat.unique) : json::value(nullptr)},
            {"virtual_first_of_three_saving_vs_9443_us", distribution(std::move(stat.first_of_three_saving_vs_primary))},
            {"virtual_quorum_two_of_three_saving_vs_9443_us", distribution(std::move(stat.quorum_two_of_three_saving_vs_primary))},
            {"first_to_quorum_delay_us", distribution(std::move(stat.first_to_quorum_delay))},
            {"first_to_last_spread_us", distribution(std::move(stat.first_to_last_spread))},
            {"endpoints", std::move(endpoints)}});
    }
    return output;
}
void self_test() {
    std::array<std::vector<Record>, 3> rows;
    rows[0] = {{{0, 0, 10}, 3000, "same"}, {{0, 0, 10}, 4000, "same"}, {{0, 0, 11}, 5000, "conflict-A"}, {{0, 0, 12}, 6000, "unmatched"}};
    rows[1] = {{{0, 0, 10}, 1000, "same"}, {{0, 0, 11}, 5000, "conflict-B"}};
    rows[2] = {{{0, 0, 10}, 2000, "same"}, {{0, 0, 11}, 5000, "conflict-A"}};
    auto result = reduce({&rows[0], &rows[1], &rows[2]});
    const auto& value = result[0].as_object();
    const auto& endpoints = value.at("endpoints").as_array();
    if (value.at("identical_matched_all_endpoints").as_uint64() != 1
        || value.at("conflicting_matched_identities").as_uint64() != 1
        || endpoints[0].as_object().at("duplicate_copies").as_uint64() != 1
        || endpoints[1].as_object().at("first_arrivals_on_matched").as_uint64() != 1
        || endpoints[1].as_object().at("paired_saving_vs_9443_us").as_object().at("p50").as_double() != 2.0
        || value.at("virtual_first_of_three_saving_vs_9443_us").as_object().at("p50").as_double() != 2.0
        || value.at("virtual_quorum_two_of_three_saving_vs_9443_us").as_object().at("p50").as_double() != 1.0
        || value.at("first_to_quorum_delay_us").as_object().at("p50").as_double() != 1.0
        || value.at("first_to_last_spread_us").as_object().at("p50").as_double() != 2.0
        || !result[1].as_object().at("matched_coverage").is_null())
        throw std::runtime_error("feed-race reduction self-test failed");
    std::cout << "feed race: exact matching, duplicate suppression, conflicts and missing data PASS\n";
}
}

int main(int argc, char** argv) {
    try {
        int duration = 60, warmup = 3, max_records = 100000;
        bool validate_only = false, test = false;
        for (int i = 1; i < argc; ++i) {
            const std::string_view argument = argv[i];
            if (argument == "--duration-seconds" && i + 1 < argc) duration = positive(argv[++i], 1, 300);
            else if (argument == "--warmup-seconds" && i + 1 < argc) warmup = positive(argv[++i], 1, 30);
            else if (argument == "--max-records" && i + 1 < argc) max_records = positive(argv[++i], 100, 200000);
            else if (argument == "--validate-only") validate_only = true;
            else if (argument == "--self-test") test = true;
            else throw std::invalid_argument("unknown or incomplete argument; endpoints are not configurable");
        }
        if (test) { self_test(); return 0; }
        std::array<ExternalVenueConnectionSpec, 3> specs;
        for (std::size_t i = 0; i < 3; ++i) {
            specs[i] = btc_spot_connection_spec(VenueId::BinanceSpot, 500);
            specs[i].port = i == 0 ? "9443" : "443";
            if (i == 2) specs[i].host = "data-stream.binance.vision";
            specs[i].subscription_json = R"({"method":"SUBSCRIBE","params":["btcusdt@bookTicker","btcusdt@aggTrade","ethusdt@bookTicker","ethusdt@aggTrade","solusdt@bookTicker","solusdt@aggTrade"],"id":1})";
        }
        if (validate_only) {
            std::cout << "read-only Binance market-data endpoint allowlist PASS\n";
            return 0;
        }
        std::atomic<std::int64_t> begin{0};
        std::array<std::unique_ptr<Recorder>, 3> recorders;
        std::array<std::unique_ptr<ExternalVenueWsClient>, 3> clients;
        for (std::size_t i = 0; i < 3; ++i) {
            recorders[i] = std::make_unique<Recorder>(begin, static_cast<std::int64_t>(duration) * 1000000000LL, static_cast<std::size_t>(max_records));
            clients[i] = std::make_unique<ExternalVenueWsClient>(specs[i], nullptr, recorders[i].get());
        }
#if defined(__APPLE__)
        std::atomic<bool> stopping{false};
        auto token = ExternalStopToken(stopping);
#else
        std::stop_source stopping;
        auto token = stopping.get_token();
#endif
        std::vector<std::thread> workers;
        for (std::size_t i = 0; i < 3; ++i) workers.emplace_back([&, i] { clients[i]->run(token); });
        const auto ready_deadline = now_ns() + 15000000000LL;
        bool all_ready = false;
        do {
            all_ready = true;
            for (const auto& recorder : recorders) all_ready = all_ready && recorder->market_seen.load(std::memory_order_acquire);
            if (!all_ready) std::this_thread::sleep_for(20ms);
        } while (!all_ready && now_ns() < ready_deadline);
        const auto start = now_ns() + static_cast<std::int64_t>(warmup) * 1000000000LL;
        begin.store(start, std::memory_order_release);
        const auto finish = start + static_cast<std::int64_t>(duration) * 1000000000LL;
        while (now_ns() < finish) std::this_thread::sleep_for(20ms);
#if defined(__APPLE__)
        stopping.store(true, std::memory_order_release);
#else
        stopping.request_stop();
#endif
        for (auto& worker : workers) worker.join();
        json::array endpoints;
        bool clean_capture = all_ready;
        std::array<const std::vector<Record>*, 3> rows;
        for (std::size_t i = 0; i < 3; ++i) {
            rows[i] = &recorders[i]->records;
            auto state = clients[i]->snapshot();
            clean_capture = clean_capture && recorders[i]->overflow == 0 && recorders[i]->invalid == 0
                && state.successful_connections == 1 && state.transport_failures == 0;
            endpoints.emplace_back(json::object{
                {"name", kNames[i]}, {"host", specs[i].host}, {"port", specs[i].port},
                {"records", rows[i]->size()}, {"record_overflow", recorders[i]->overflow},
                {"invalid_frames", recorders[i]->invalid}, {"connection_attempts", state.connection_attempts},
                {"successful_connections", state.successful_connections}, {"transport_failures", state.transport_failures}});
        }
        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_feed_first_arrival_v1"}, {"paper_only", true},
            {"authenticated_execution", false}, {"real_order_submission", false},
            {"scope", "SAME_HOST_IDENTICAL_MESSAGE_ARRIVAL_NOT_EXCHANGE_ONE_WAY_LATENCY"},
            {"selection_or_execution_authority", false}, {"all_sources_ready_before_warmup", all_ready},
            {"clean_capture", clean_capture}, {"duration_seconds", duration}, {"warmup_seconds", warmup},
            {"matching_note", "Only identical exchange IDs and payload fields seen on all endpoints enter paired latency. Missing/conflicting events are counted separately. Do not compare monotonic clocks across hosts."},
            {"endpoints", std::move(endpoints)}, {"streams", reduce(rows)}}) << '\n';
        return clean_capture ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "feed_race_probe: " << error.what() << '\n';
        return 64;
    }
}
