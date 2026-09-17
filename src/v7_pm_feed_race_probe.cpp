#include "pm/fast_ws.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace json = boost::json;
using namespace std::chrono_literals;

namespace {

constexpr std::string_view kEndpoint =
    "wss://ws-subscriptions-clob.polymarket.com/ws/market";
constexpr std::array<const char*, 3> kNames{
    "PM_CONN_A", "PM_CONN_B", "PM_CONN_C"};

struct Record {
    std::string identity;
    std::string fingerprint;
    std::int64_t receive_ns = 0;
};

struct Group {
    std::array<const Record*, 3> record{};
    bool conflict = false;
};

struct Aggregate {
    std::uint64_t unique = 0;
    std::uint64_t matched = 0;
    std::uint64_t conflicting = 0;
    std::array<std::uint64_t, 3> present{};
    std::array<std::uint64_t, 3> duplicate{};
    std::array<std::uint64_t, 3> first{};
    std::array<std::vector<double>, 3> penalty_us;
    std::array<std::vector<double>, 3> saving_vs_primary_us;
    std::vector<double> virtual_first_saving_us;
    std::vector<double> race_ab_saving_vs_a_us;
    std::vector<double> race_ac_saving_vs_a_us;
};

[[nodiscard]] std::int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

[[nodiscard]] int bounded_int(std::string_view text, int lo, int hi) {
    int value = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size()
        || value < lo || value > hi) {
        throw std::invalid_argument("bounded integer required");
    }
    return value;
}

[[nodiscard]] std::string text_of(const json::value* value) {
    if (value == nullptr) return {};
    if (value->is_string()) return std::string(value->as_string());
    if (value->is_int64()) return std::to_string(value->as_int64());
    if (value->is_uint64()) return std::to_string(value->as_uint64());
    if (value->is_double()) return json::serialize(*value);
    return {};
}

[[nodiscard]] const json::value* field(
    const json::object& object, std::string_view key) noexcept {
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->value();
}

[[nodiscard]] json::object distribution(std::vector<double> values) {
    if (values.empty()) {
        return {{"count", 0}, {"p50", nullptr}, {"p95", nullptr},
                {"p99", nullptr}, {"p999", nullptr}, {"min", nullptr},
                {"max", nullptr}};
    }
    std::sort(values.begin(), values.end());
    const auto q = [&](double probability) {
        const auto index = std::min<std::size_t>(
            values.size() - 1,
            static_cast<std::size_t>(std::ceil(probability * values.size())) - 1);
        return values[index];
    };
    return {{"count", values.size()}, {"p50", q(.50)}, {"p95", q(.95)},
            {"p99", q(.99)}, {"p999", q(.999)},
            {"min", values.front()}, {"max", values.back()}};
}

class Recorder final {
public:
    Recorder(std::atomic<std::int64_t>& begin_ns,
             std::int64_t duration_ns,
             std::size_t limit)
        : begin_ns_(begin_ns), duration_ns_(duration_ns), limit_(limit) {
        records.reserve(limit_);
    }

    void on_message(std::string_view payload,
                    const pm::fast::FeedReceiveStamp& stamp) noexcept {
        try {
            if (payload.empty() || payload == "PONG") return;
            boost::system::error_code error;
            const auto root = json::parse(payload, error);
            if (error) {
                ++parse_failures;
                return;
            }
            if (root.is_array()) {
                for (const auto& value : root.as_array()) {
                    if (value.is_object()) inspect(value.as_object(), stamp.monotonic_ns);
                }
            } else if (root.is_object()) {
                inspect(root.as_object(), stamp.monotonic_ns);
            }
        } catch (...) {
            ++parse_failures;
        }
    }

    std::vector<Record> records;
    std::atomic<bool> market_seen{false};
    std::uint64_t parse_failures = 0;
    std::uint64_t overflow = 0;

private:
    void inspect(const json::object& event, std::int64_t receive_ns) {
        const json::object* payload = &event;
        if (const auto* wrapped = field(event, "payload");
            wrapped != nullptr && wrapped->is_object()) {
            payload = &wrapped->as_object();
        }
        auto type = text_of(field(event, "event_type"));
        if (type.empty()) type = text_of(field(event, "type"));
        if (type.empty()) return;
        market_seen.store(true, std::memory_order_release);
        if (type != "price_change") return;

        const auto timestamp = text_of(field(*payload, "timestamp"));
        const auto market = text_of(field(*payload, "market"));
        const auto* raw_changes = field(*payload, "price_changes");
        if (timestamp.empty() || raw_changes == nullptr || !raw_changes->is_array()) {
            ++parse_failures;
            return;
        }

        for (const auto& raw : raw_changes->as_array()) {
            if (!raw.is_object()) {
                ++parse_failures;
                continue;
            }
            const auto& change = raw.as_object();
            const auto asset = text_of(field(change, "asset_id"));
            const auto hash = text_of(field(change, "hash"));
            const auto price = text_of(field(change, "price"));
            const auto size = text_of(field(change, "size"));
            const auto side = text_of(field(change, "side"));
            const auto best_bid = text_of(field(change, "best_bid"));
            const auto best_ask = text_of(field(change, "best_ask"));
            if (asset.empty() || hash.empty() || price.empty() || size.empty()
                || side.empty()) {
                ++parse_failures;
                continue;
            }
            const auto begin = begin_ns_.load(std::memory_order_acquire);
            if (begin == 0 || receive_ns < begin
                || receive_ns - begin >= duration_ns_) {
                continue;
            }
            if (records.size() >= limit_) {
                ++overflow;
                continue;
            }
            Record record;
            record.fingerprint.reserve(
                market.size() + price.size() + size.size() + side.size()
                + best_bid.size() + best_ask.size() + 12);
            record.fingerprint = market + "|" + price + "|" + size + "|"
                + side + "|" + best_bid + "|" + best_ask;
            // The venue hash is useful but not assumed globally unique for one
            // scalar change. Match only byte-equivalent economic updates.
            record.identity.reserve(asset.size() + hash.size() + timestamp.size()
                                    + record.fingerprint.size() + 12);
            record.identity = "P|" + asset + "|" + hash + "|" + timestamp
                + "|" + record.fingerprint;
            record.receive_ns = receive_ns;
            records.push_back(std::move(record));
        }
    }

    std::atomic<std::int64_t>& begin_ns_;
    std::int64_t duration_ns_ = 0;
    std::size_t limit_ = 0;
};

[[nodiscard]] Aggregate reduce(
    const std::array<const std::vector<Record>*, 3>& sources) {
    std::map<std::string, Group> groups;
    Aggregate out;
    for (std::size_t source = 0; source < sources.size(); ++source) {
        for (const auto& record : *sources[source]) {
            auto& group = groups[record.identity];
            auto*& current = group.record[source];
            if (current != nullptr) {
                ++out.duplicate[source];
                if (current->fingerprint != record.fingerprint) group.conflict = true;
            }
            if (current == nullptr || record.receive_ns < current->receive_ns) {
                current = &record;
            }
        }
    }

    out.unique = groups.size();
    for (const auto& [identity, group] : groups) {
        (void)identity;
        for (std::size_t i = 0; i < group.record.size(); ++i) {
            if (group.record[i] != nullptr) ++out.present[i];
        }
        if (group.record[0] == nullptr || group.record[1] == nullptr
            || group.record[2] == nullptr) {
            continue;
        }
        if (group.conflict
            || group.record[0]->fingerprint != group.record[1]->fingerprint
            || group.record[0]->fingerprint != group.record[2]->fingerprint) {
            ++out.conflicting;
            continue;
        }
        ++out.matched;
        const auto earliest = std::min({group.record[0]->receive_ns,
                                        group.record[1]->receive_ns,
                                        group.record[2]->receive_ns});
        const double virtual_saving =
            static_cast<double>(group.record[0]->receive_ns - earliest) / 1000.0;
        out.virtual_first_saving_us.push_back(virtual_saving);
        out.race_ab_saving_vs_a_us.push_back(
            static_cast<double>(group.record[0]->receive_ns
                - std::min(group.record[0]->receive_ns, group.record[1]->receive_ns)) / 1000.0);
        out.race_ac_saving_vs_a_us.push_back(
            static_cast<double>(group.record[0]->receive_ns
                - std::min(group.record[0]->receive_ns, group.record[2]->receive_ns)) / 1000.0);
        for (std::size_t i = 0; i < 3; ++i) {
            if (group.record[i]->receive_ns == earliest) ++out.first[i];
            out.penalty_us[i].push_back(
                static_cast<double>(group.record[i]->receive_ns - earliest) / 1000.0);
            out.saving_vs_primary_us[i].push_back(
                static_cast<double>(group.record[0]->receive_ns
                                    - group.record[i]->receive_ns) / 1000.0);
        }
    }
    return out;
}

void self_test() {
    std::array<std::vector<Record>, 3> rows;
    rows[0] = {{"a", "same", 3000}, {"a", "same", 4000},
               {"b", "bad-a", 5000}, {"c", "one", 6000}};
    rows[1] = {{"a", "same", 1000}, {"b", "bad-b", 5000}};
    rows[2] = {{"a", "same", 2000}, {"b", "bad-a", 5000}};
    const auto result = reduce({&rows[0], &rows[1], &rows[2]});
    if (result.unique != 3 || result.matched != 1 || result.conflicting != 1
        || result.duplicate[0] != 1 || result.first[1] != 1
        || result.virtual_first_saving_us.size() != 1
        || result.virtual_first_saving_us[0] != 2.0) {
        throw std::runtime_error("PM feed-race reducer self-test failed");
    }
    std::cout << "PM feed race reducer PASS\n";
}

} // namespace

int main(int argc, char** argv) {
    try {
        int duration_seconds = 60;
        int warmup_seconds = 3;
        int max_records = 100000;
        bool validate_only = false;
        bool run_self_test = false;
        std::vector<std::string> asset_ids;
        for (int i = 1; i < argc; ++i) {
            const std::string_view arg = argv[i];
            if (arg == "--asset-id" && i + 1 < argc) {
                asset_ids.emplace_back(argv[++i]);
            } else if (arg == "--duration-seconds" && i + 1 < argc) {
                duration_seconds = bounded_int(argv[++i], 1, 600);
            } else if (arg == "--warmup-seconds" && i + 1 < argc) {
                warmup_seconds = bounded_int(argv[++i], 1, 30);
            } else if (arg == "--max-records" && i + 1 < argc) {
                max_records = bounded_int(argv[++i], 100, 500000);
            } else if (arg == "--validate-only") {
                validate_only = true;
            } else if (arg == "--self-test") {
                run_self_test = true;
            } else {
                throw std::invalid_argument("unknown or incomplete argument");
            }
        }
        if (run_self_test) {
            self_test();
            return 0;
        }
        if (validate_only) {
            std::cout << "read-only Polymarket market WebSocket endpoint allowlist PASS\n";
            return 0;
        }
        if (asset_ids.empty() || asset_ids.size() > 64) {
            throw std::invalid_argument("provide 1..64 --asset-id values");
        }
        std::sort(asset_ids.begin(), asset_ids.end());
        asset_ids.erase(std::unique(asset_ids.begin(), asset_ids.end()), asset_ids.end());
        if (asset_ids.empty()) throw std::invalid_argument("asset ids required");

        std::atomic<std::int64_t> begin_ns{0};
        const auto duration_ns = static_cast<std::int64_t>(duration_seconds)
            * 1'000'000'000LL;
        std::array<std::unique_ptr<Recorder>, 3> recorders;
        std::array<std::unique_ptr<pm::fast::MarketWebSocketFeed>, 3> feeds;
        for (std::size_t i = 0; i < feeds.size(); ++i) {
            recorders[i] = std::make_unique<Recorder>(
                begin_ns, duration_ns, static_cast<std::size_t>(max_records));
            feeds[i] = std::make_unique<pm::fast::MarketWebSocketFeed>(
                std::string(kEndpoint), asset_ids, asset_ids.size(),
                [&, i](std::string_view payload,
                       const pm::fast::FeedReceiveStamp& stamp,
                       std::size_t) {
                    recorders[i]->on_message(payload, stamp);
                });
            feeds[i]->start();
        }

        const auto ready_deadline = now_ns() + 15'000'000'000LL;
        bool all_ready = false;
        do {
            all_ready = true;
            for (const auto& recorder : recorders) {
                all_ready = all_ready
                    && recorder->market_seen.load(std::memory_order_acquire);
            }
            if (!all_ready) std::this_thread::sleep_for(20ms);
        } while (!all_ready && now_ns() < ready_deadline);

        if (all_ready) std::this_thread::sleep_for(std::chrono::seconds(warmup_seconds));
        const auto start = now_ns();
        begin_ns.store(start, std::memory_order_release);
        const auto finish = start + duration_ns;
        while (now_ns() < finish) std::this_thread::sleep_for(20ms);
        for (auto& feed : feeds) feed->stop();

        std::array<const std::vector<Record>*, 3> sources{};
        json::array connections;
        bool clean = all_ready;
        for (std::size_t i = 0; i < feeds.size(); ++i) {
            sources[i] = &recorders[i]->records;
            const auto status = feeds[i]->snapshot();
                        connections.emplace_back(json::object{
                {"name", kNames[i]},
                {"records", recorders[i]->records.size()},
                {"parse_failures", recorders[i]->parse_failures},
                {"overflow", recorders[i]->overflow},
                {"messages", status.messages},
                {"reconnects", status.reconnects},
                {"transport_errors", status.errors},
            });
            clean = clean && recorders[i]->parse_failures == 0
                && recorders[i]->overflow == 0
                && status.reconnects == 0 && status.errors == 0;
        }

        const auto result = reduce(sources);
        json::array per_connection;
        for (std::size_t i = 0; i < 3; ++i) {
            per_connection.emplace_back(json::object{
                {"name", kNames[i]},
                {"present", result.present[i]},
                {"duplicate", result.duplicate[i]},
                {"first_arrivals", result.first[i]},
                {"arrival_penalty_to_first_us", distribution(result.penalty_us[i])},
                {"saving_vs_conn_a_us", distribution(result.saving_vs_primary_us[i])},
            });
        }
        const double coverage = result.unique > 0
            ? static_cast<double>(result.matched) / static_cast<double>(result.unique)
            : 0.0;
        std::cout << json::serialize(json::object{
            {"schema", "polymarket_v7_pm_redundant_feed_race_v1"},
            {"read_only_public_market_data", true},
            {"execution_authority", false},
            {"endpoint", kEndpoint},
            {"asset_count", asset_ids.size()},
            {"duration_seconds", duration_seconds},
            {"clean_capture", clean},
            {"all_connections_ready", all_ready},
            {"unique_union", result.unique},
            {"identical_matched_all_connections", result.matched},
            {"conflicting_matched_identities", result.conflicting},
            {"matched_coverage", coverage},
            {"virtual_first_saving_vs_conn_a_us",
             distribution(result.virtual_first_saving_us)},
            {"race_a_plus_b_saving_vs_a_us",
             distribution(result.race_ab_saving_vs_a_us)},
            {"race_a_plus_c_saving_vs_a_us",
             distribution(result.race_ac_saving_vs_a_us)},
            {"connections", std::move(connections)},
            {"matched_stats", std::move(per_connection)},
            {"note", "Price-change matching requires official asset_id + hash + timestamp plus identical economic fields; no trading or promotion is performed."},
        }) << '\n';
        return clean && result.matched > 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "pm_feed_race: " << error.what() << '\n';
        return 64;
    }
}
