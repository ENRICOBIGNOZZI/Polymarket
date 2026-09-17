#include "pm/http.hpp"
#include "pm/trade_identity.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <utility>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {
namespace fs = std::filesystem;
namespace json = boost::json;

std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}
std::int64_t now_s() { return now_ms() / 1000; }

std::string text(const json::value& value) {
    if (value.is_string()) return std::string(value.as_string());
    if (value.is_int64()) return std::to_string(value.as_int64());
    if (value.is_uint64()) return std::to_string(value.as_uint64());
    if (value.is_double()) {
        std::ostringstream out;
        out << std::setprecision(16) << value.as_double();
        return out.str();
    }
    return {};
}

double number(const json::value& value, double fallback = 0.0) {
    try {
        if (value.is_double()) return value.as_double();
        if (value.is_int64()) return static_cast<double>(value.as_int64());
        if (value.is_uint64()) return static_cast<double>(value.as_uint64());
        if (value.is_string()) return std::stod(std::string(value.as_string()));
    } catch (...) {}
    return fallback;
}

std::int64_t integer(const json::value& value, std::int64_t fallback = 0) {
    try {
        if (value.is_int64()) return value.as_int64();
        if (value.is_uint64()) return static_cast<std::int64_t>(value.as_uint64());
        if (value.is_double()) return static_cast<std::int64_t>(value.as_double());
        if (value.is_string()) return std::stoll(std::string(value.as_string()));
    } catch (...) {}
    return fallback;
}

std::string get_text(const json::object& object, const char* key) {
    const auto it = object.find(key);
    return it == object.end() ? std::string{} : text(it->value());
}
double get_number(const json::object& object, const char* key, double fallback = 0.0) {
    const auto it = object.find(key);
    return it == object.end() ? fallback : number(it->value(), fallback);
}
std::int64_t get_integer(const json::object& object, const char* key, std::int64_t fallback = 0) {
    const auto it = object.find(key);
    return it == object.end() ? fallback : integer(it->value(), fallback);
}
bool get_bool(const json::object& object, const char* key, bool fallback = false) {
    const auto it = object.find(key);
    return it != object.end() && it->value().is_bool() ? it->value().as_bool() : fallback;
}

std::string csv_escape(const std::string& value) {
    if (value.find_first_of(",\"\n\r") == std::string::npos) return value;
    std::string out = "\"";
    for (char c : value) out += c == '"' ? "\"\"" : std::string(1, c);
    return out + '"';
}

std::vector<std::string> split_csv(const std::string& line) {
    std::vector<std::string> fields;
    std::string current;
    bool quoted = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (c == '"') {
            if (quoted && i + 1 < line.size() && line[i + 1] == '"') {
                current.push_back('"');
                ++i;
            } else {
                quoted = !quoted;
            }
        } else if (c == ',' && !quoted) {
            fields.push_back(current);
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    fields.push_back(current);
    return fields;
}

std::string join_conditions(const std::vector<std::string>& ids, std::size_t lo, std::size_t hi) {
    std::ostringstream out;
    for (std::size_t i = lo; i < hi; ++i) {
        if (i != lo) out << ',';
        out << ids[i];
    }
    return out.str();
}

struct Trade {
    std::int64_t ts = 0;
    std::int64_t received_ms = 0;
    std::string condition_id;
    std::string asset_id;
    std::string outcome;
    std::string side;
    double price = 0.0;
    double size = 0.0;
    std::string tx_hash;
    std::string slug;
    std::string event_slug;
};

std::string trade_key(const Trade& trade) {
    return pm::public_trade_key(trade.tx_hash, trade.condition_id, trade.asset_id,
                                trade.ts, trade.side, trade.price, trade.size);
}

class V7TradeRecorder {
public:
    V7TradeRecorder(std::string run_dir, std::string data_url, std::string universe_path,
                    std::string model_sha, std::size_t batch_size,
                    std::int64_t lookback_seconds, std::int64_t universe_max_age_seconds)
        : run_dir_(std::move(run_dir)), data_url_(std::move(data_url)),
          universe_path_(std::move(universe_path)), model_sha_(std::move(model_sha)),
          batch_size_(std::max<std::size_t>(1, batch_size)),
          lookback_seconds_(std::max<std::int64_t>(10, lookback_seconds)),
          universe_max_age_ms_(std::max<std::int64_t>(1, universe_max_age_seconds) * 1000) {
        fs::create_directories(run_dir_);
        tape_path_ = fs::path(run_dir_) / "trade_tape.csv";
        state_path_ = fs::path(run_dir_) / "trade_recorder_state.csv";
        status_path_ = fs::path(run_dir_) / "trade_recorder_status.json";
        load_recent_seen();
        if (!fs::exists(tape_path_) || fs::file_size(tape_path_) == 0) {
            std::ofstream output(tape_path_);
            output << "timestamp,received_ms,lag_ms,condition_id,asset_id,outcome,side,price,size,transaction_hash,slug,event_slug\n";
        }
    }

    bool tick() {
        const auto started_ms = now_ms();
        std::vector<std::string> conditions;
        std::size_t market_count = 0;
        try {
            std::tie(market_count, conditions) = load_crypto_universe();
        } catch (const std::exception& error) {
            // A missing/stale crypto universe is incomplete evidence. Never
            // widen discovery to unrelated Polymarket markets and never reuse
            // stale membership; the next loop retries.
            persist_status(started_ms, 0, 0, 0, 0, 0, 1, 0);
            std::cerr << "trade_recorder crypto universe unavailable: " << error.what() << '\n';
            return false;
        }
        std::unordered_set<std::string> allowed_conditions(
            conditions.begin(), conditions.end());

        const auto end = now_s();
        const auto start = std::max<std::int64_t>(0, end - lookback_seconds_);
        std::vector<Trade> new_rows;
        std::size_t requests = 0;
        std::size_t errors = 0;
        std::size_t fetched = 0;
        std::size_t truncated_batches = 0;
        constexpr std::size_t page_limit = 10000;

        for (std::size_t lo = 0; lo < conditions.size(); lo += batch_size_) {
            const auto hi = std::min(conditions.size(), lo + batch_size_);
            const auto market_query = join_conditions(conditions, lo, hi);
            for (std::size_t offset : {std::size_t{0}, page_limit}) {
                std::ostringstream url;
                url << data_url_ << "/trades?limit=" << page_limit << "&offset=" << offset
                    << "&takerOnly=true&start=" << start << "&end=" << end
                    << "&market=" << market_query;
                ++requests;
                try {
                    const auto response = http_.get(url.str());
                    if (response.status < 200 || response.status >= 300) {
                        ++errors;
                        break;
                    }
                    const auto root = json::parse(response.body);
                    if (!root.is_array()) {
                        ++errors;
                        break;
                    }
                    const auto page_size = root.as_array().size();
                    fetched += page_size;
                    const auto received_ms = now_ms();
                    for (const auto& value : root.as_array()) {
                        if (!value.is_object()) continue;
                        const auto& object = value.as_object();
                        Trade trade;
                        trade.ts = get_integer(object, "timestamp");
                        trade.received_ms = received_ms;
                        trade.condition_id = get_text(object, "conditionId");
                        trade.asset_id = get_text(object, "asset");
                        trade.outcome = get_text(object, "outcome");
                        trade.side = get_text(object, "side");
                        trade.price = get_number(object, "price");
                        trade.size = get_number(object, "size");
                        trade.tx_hash = get_text(object, "transactionHash");
                        trade.slug = get_text(object, "slug");
                        trade.event_slug = get_text(object, "eventSlug");
                        if (trade.ts <= 0 || trade.asset_id.empty() || trade.price <= 0.0 ||
                            trade.price >= 1.0 || trade.size <= 0.0 ||
                            !allowed_conditions.count(trade.condition_id)) {
                            continue;
                        }
                        if (seen_.insert(trade_key(trade)).second) {
                            new_rows.push_back(std::move(trade));
                        }
                    }
                    if (page_size < page_limit) break;
                    if (offset == page_limit) ++truncated_batches;
                } catch (const std::exception& error) {
                    ++errors;
                    std::cerr << "v7 trade recorder request failed: " << error.what() << '\n';
                    break;
                }
            }
        }

        std::sort(new_rows.begin(), new_rows.end(), [](const Trade& left, const Trade& right) {
            if (left.ts != right.ts) return left.ts < right.ts;
            if (left.condition_id != right.condition_id) return left.condition_id < right.condition_id;
            if (left.asset_id != right.asset_id) return left.asset_id < right.asset_id;
            return trade_key(left) < trade_key(right);
        });

        std::ofstream output(tape_path_, std::ios::app);
        for (const auto& trade : new_rows) {
            const auto lag = trade.received_ms - trade.ts * 1000;
            output << trade.ts << ',' << trade.received_ms << ',' << lag << ','
                   << csv_escape(trade.condition_id) << ',' << csv_escape(trade.asset_id) << ','
                   << csv_escape(trade.outcome) << ',' << csv_escape(trade.side) << ','
                   << std::setprecision(12) << trade.price << ',' << trade.size << ','
                   << csv_escape(trade.tx_hash) << ',' << csv_escape(trade.slug) << ','
                   << csv_escape(trade.event_slug) << '\n';
            last_trade_ts_ = std::max(last_trade_ts_, trade.ts);
        }
        output.flush();
        persist_state();
        persist_status(started_ms, market_count, conditions.size(), requests, fetched,
                       new_rows.size(), errors, truncated_batches);
        trim_seen();

        std::cout << "trade_recorder crypto_markets=" << market_count
                  << " conditions=" << conditions.size()
                  << " requests=" << requests
                  << " fetched=" << fetched
                  << " new_trades=" << new_rows.size()
                  << " errors=" << errors
                  << " truncated_batches=" << truncated_batches
                  << " last_trade_ts=" << last_trade_ts_
                  << " seen=" << seen_.size()
                  << " elapsed_ms=" << (now_ms() - started_ms) << '\n';
        std::cout.flush();
        return errors == 0 && truncated_batches == 0;
    }

private:
    pm::HttpClient http_;
    std::string run_dir_;
    std::string data_url_;
    fs::path universe_path_;
    std::string model_sha_;
    std::size_t batch_size_;
    std::int64_t lookback_seconds_;
    std::int64_t universe_max_age_ms_;
    std::int64_t session_start_ms_ = now_ms();
    fs::path tape_path_;
    fs::path state_path_;
    fs::path status_path_;
    std::unordered_set<std::string> seen_;
    std::int64_t last_trade_ts_ = 0;


    std::pair<std::size_t, std::vector<std::string>> load_crypto_universe() const {
        std::ifstream input(universe_path_);
        if (!input) throw std::runtime_error("crypto_universe_missing");
        std::ostringstream buffer; buffer << input.rdbuf();
        const auto root = json::parse(buffer.str());
        if (!root.is_object()) throw std::runtime_error("crypto_universe_not_object");
        const auto& object = root.as_object();
        if (get_text(object, "schema") != "polymarket_v7_crypto_universe_snapshot_v1"
                || get_bool(object, "paper_only") != true
                || get_bool(object, "authenticated_execution", true) != false
                || get_bool(object, "real_order_submission", true) != false
                || get_bool(object, "execution_authority", true) != false
                || get_text(object, "model_sha") != model_sha_) {
            throw std::runtime_error("crypto_universe_contract_invalid");
        }
        const auto published_ms = get_integer(object, "timestamp_ms");
        const auto age_ms = now_ms() - published_ms;
        if (published_ms <= 0 || age_ms < -5000 || age_ms > universe_max_age_ms_) {
            throw std::runtime_error("crypto_universe_stale");
        }
        const auto it = object.find("markets");
        if (it == object.end() || !it->value().is_array()) {
            throw std::runtime_error("crypto_universe_markets_missing");
        }
        std::vector<std::string> conditions;
        std::unordered_set<std::string> seen;
        std::size_t market_count = 0;
        for (const auto& value : it->value().as_array()) {
            if (!value.is_object()) continue;
            ++market_count;
            const auto condition = get_text(value.as_object(), "condition_id");
            if (!condition.empty() && seen.insert(condition).second) conditions.push_back(condition);
        }
        if (market_count == 0 || conditions.empty()) {
            throw std::runtime_error("crypto_universe_empty");
        }
        return {market_count, conditions};
    }

    void load_recent_seen() {
        std::ifstream input(tape_path_);
        std::string line;
        std::getline(input, line);
        const auto cutoff = now_s() - 2 * lookback_seconds_;
        while (std::getline(input, line)) {
            const auto fields = split_csv(line);
            if (fields.size() < 12) continue;
            try {
                Trade trade;
                trade.ts = std::stoll(fields[0]);
                trade.condition_id = fields[3];
                trade.asset_id = fields[4];
                trade.outcome = fields[5];
                trade.side = fields[6];
                trade.price = std::stod(fields[7]);
                trade.size = std::stod(fields[8]);
                trade.tx_hash = fields[9];
                if (trade.ts >= cutoff) seen_.insert(trade_key(trade));
                last_trade_ts_ = std::max(last_trade_ts_, trade.ts);
            } catch (...) {}
        }
    }

    void trim_seen() {
        if (seen_.size() < 250000) return;
        seen_.clear();
        load_recent_seen();
    }

    void persist_state() const {
        const auto temporary = state_path_.string() + ".tmp";
        {
            std::ofstream output(temporary, std::ios::trunc);
            output << "last_trade_ts,seen_count\n" << last_trade_ts_ << ',' << seen_.size() << '\n';
        }
        std::error_code error;
        fs::rename(temporary, state_path_, error);
        if (error) {
            fs::remove(state_path_, error);
            error.clear();
            fs::rename(temporary, state_path_, error);
        }
        if (error) throw std::runtime_error("cannot persist V7 trade recorder state");
    }

    void persist_status(std::int64_t started_ms, std::size_t markets, std::size_t conditions,
                        std::size_t requests, std::size_t fetched, std::size_t new_trades,
                        std::size_t errors, std::size_t truncated_batches) const {
        // A header-only tape can be a complete, successful scan of the configured
        // crypto universe during a no-print regime. Monitoring must distinguish
        // that truthful state from a dead or partially failing recorder.
        const bool complete_scan = conditions > 0 && requests > 0 && errors == 0 &&
                                   truncated_batches == 0;
        const bool no_matching_standard_clob_flow = complete_scan && fetched == 0;
        json::object status{
            {"schema", "polymarket_v7_trade_recorder_status_v1"},
            {"timestamp_ms", now_ms()},
            {"started_ms", started_ms},
            {"data_plane_healthy", complete_scan},
            {"flow_regime", no_matching_standard_clob_flow
                ? "CRYPTO_CLOB_NO_MATCHING_TRADES"
                : (complete_scan ? "CRYPTO_CLOB_TRADES_OBSERVED" : "SCAN_INCOMPLETE_OR_FAILED")},
            {"markets", markets}, {"conditions", conditions}, {"requests", requests},
            {"fetched", fetched}, {"new_trades", new_trades}, {"errors", errors},
            {"truncated_batches", truncated_batches}, {"last_trade_ts", last_trade_ts_},
            {"model_sha", model_sha_}, {"session_start_ms", session_start_ms_},
            {"universe_path", universe_path_.string()},
            {"scope", "CONFIGURED_CRYPTO_CONTEXTS_ONLY"},
            {"paper_only", true}, {"authenticated_execution", false},
            {"real_order_submission", false},
        };
        const auto temporary = status_path_.string() + ".tmp";
        {
            std::ofstream output(temporary, std::ios::trunc);
            output << json::serialize(status) << '\n';
            output.flush();
            if (!output) throw std::runtime_error("cannot write V7 trade recorder status");
        }
        std::error_code error;
        fs::rename(temporary, status_path_, error);
        if (error) {
            fs::remove(status_path_, error);
            error.clear();
            fs::rename(temporary, status_path_, error);
        }
        if (error) throw std::runtime_error("cannot persist V7 trade recorder status");
    }
};

} // namespace

int main(int argc, char** argv) {
    try {
        std::string run_dir = "runs/paper_v7_live";
        std::string data_url = "https://data-api.polymarket.com";
        std::string universe_path;
        std::string model_sha;
        std::size_t batch = 40;
        std::int64_t lookback_seconds = 180;
        std::int64_t universe_max_age_seconds = 180;
        int interval_seconds = 5;
        bool loop = false;

        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("missing value after " + argument);
                return std::string(argv[++i]);
            };
            if (argument == "--run-dir") run_dir = next();
            else if (argument == "--data-url") data_url = next();
            else if (argument == "--universe") universe_path = next();
            else if (argument == "--model-sha") model_sha = next();
            else if (argument == "--batch") batch = static_cast<std::size_t>(std::stoull(next()));
            else if (argument == "--lookback-seconds") lookback_seconds = std::stoll(next());
            else if (argument == "--universe-max-age-seconds") universe_max_age_seconds = std::stoll(next());
            else if (argument == "--interval") interval_seconds = std::stoi(next());
            else if (argument == "--loop") loop = true;
            else if (argument == "--once") loop = false;
            else if (argument == "--help" || argument == "-h") {
                std::cout << "polymarket_v7_trade_recorder [--run-dir DIR] [--universe FILE] "
                             "--model-sha SHA [--data-url URL] [--batch N] "
                             "[--lookback-seconds N] [--universe-max-age-seconds N] "
                             "[--interval N] [--once|--loop]\n";
                return 0;
            } else {
                throw std::runtime_error("unknown argument: " + argument);
            }
        }

        if (universe_path.empty()) universe_path = (fs::path(run_dir) / "universe/current.json").string();
        if (model_sha.size() != 40 || model_sha.find_first_not_of("0123456789abcdef") != std::string::npos) {
            throw std::runtime_error("--model-sha must be a 40-character lowercase hexadecimal SHA");
        }
        V7TradeRecorder recorder(run_dir, data_url, universe_path, model_sha, batch,
                                 lookback_seconds, universe_max_age_seconds);
        do {
            if (!recorder.tick() && !loop) return 1;
            if (loop) std::this_thread::sleep_for(std::chrono::seconds(std::max(1, interval_seconds)));
        } while (loop);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "fatal: " << error.what() << '\n';
        return 1;
    }
}
