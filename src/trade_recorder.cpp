#include "pm/api.hpp"
#include "pm/engine.hpp"
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
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
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

std::string text(const json::value& v) {
    if (v.is_string()) return std::string(v.as_string());
    if (v.is_int64()) return std::to_string(v.as_int64());
    if (v.is_uint64()) return std::to_string(v.as_uint64());
    if (v.is_double()) {
        std::ostringstream os;
        os << std::setprecision(16) << v.as_double();
        return os.str();
    }
    return {};
}

double number(const json::value& v, double d = 0.0) {
    try {
        if (v.is_double()) return v.as_double();
        if (v.is_int64()) return static_cast<double>(v.as_int64());
        if (v.is_uint64()) return static_cast<double>(v.as_uint64());
        if (v.is_string()) return std::stod(std::string(v.as_string()));
    } catch (...) {
    }
    return d;
}

std::int64_t integer(const json::value& v, std::int64_t d = 0) {
    try {
        if (v.is_int64()) return v.as_int64();
        if (v.is_uint64()) return static_cast<std::int64_t>(v.as_uint64());
        if (v.is_double()) return static_cast<std::int64_t>(v.as_double());
        if (v.is_string()) return std::stoll(std::string(v.as_string()));
    } catch (...) {
    }
    return d;
}

std::string get_text(const json::object& o, const char* key) {
    auto it = o.find(key);
    return it == o.end() ? std::string{} : text(it->value());
}

double get_number(const json::object& o, const char* key, double d = 0.0) {
    auto it = o.find(key);
    return it == o.end() ? d : number(it->value(), d);
}

std::int64_t get_integer(const json::object& o, const char* key, std::int64_t d = 0) {
    auto it = o.find(key);
    return it == o.end() ? d : integer(it->value(), d);
}

std::string csv_escape(const std::string& s) {
    if (s.find_first_of(",\"\n\r") == std::string::npos) return s;
    std::string out = "\"";
    for (char c : s) out += c == '"' ? "\"\"" : std::string(1, c);
    out += '"';
    return out;
}

std::vector<std::string> split_csv(const std::string& line) {
    std::vector<std::string> out;
    std::string cur;
    bool quoted = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (c == '"') {
            if (quoted && i + 1 < line.size() && line[i + 1] == '"') {
                cur.push_back('"');
                ++i;
            } else {
                quoted = !quoted;
            }
        } else if (c == ',' && !quoted) {
            out.push_back(cur);
            cur.clear();
        } else {
            cur.push_back(c);
        }
    }
    out.push_back(cur);
    return out;
}

std::string join_conditions(const std::vector<std::string>& ids, std::size_t lo, std::size_t hi) {
    std::ostringstream os;
    for (std::size_t i = lo; i < hi; ++i) {
        if (i != lo) os << ',';
        os << ids[i];
    }
    return os.str();
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

std::string trade_key(const Trade& t) {
    return pm::public_trade_key(
        t.tx_hash,
        t.condition_id,
        t.asset_id,
        t.ts,
        t.side,
        t.price,
        t.size);
}

class Recorder {
public:
    Recorder(pm::Config cfg, std::string run_dir, std::string data_url, std::size_t batch_size,
             std::int64_t lookback_s)
        : cfg_(std::move(cfg)), api_(cfg_), run_dir_(std::move(run_dir)), data_url_(std::move(data_url)),
          batch_size_(std::max<std::size_t>(1, batch_size)), lookback_s_(std::max<std::int64_t>(10, lookback_s)) {
        fs::create_directories(run_dir_);
        tape_path_ = fs::path(run_dir_) / "trade_tape.csv";
        state_path_ = fs::path(run_dir_) / "trade_recorder_state.csv";
        load_seen();
        if (!fs::exists(tape_path_) || fs::file_size(tape_path_) == 0) {
            std::ofstream f(tape_path_);
            f << "timestamp,received_ms,lag_ms,condition_id,asset_id,outcome,side,price,size,transaction_hash,slug,event_slug\n";
        }
    }

    void tick(std::size_t market_limit, double min_liquidity) {
        const auto started_ms = now_ms();
        const auto markets = api_.discover_markets(market_limit, min_liquidity);
        std::unordered_map<std::string,const pm::Market*> by_condition;
        std::vector<std::string> conditions;
        conditions.reserve(markets.size());
        for (const auto& m : markets) {
            if (m.condition_id.empty() || by_condition.count(m.condition_id)) continue;
            by_condition[m.condition_id] = &m;
            conditions.push_back(m.condition_id);
        }

        const std::int64_t end = now_s();
        // Always replay a fixed overlap window. A global last-trade watermark is
        // unsafe when markets have sparse/asynchronous public trade streams.
        const std::int64_t start = std::max<std::int64_t>(0, end - lookback_s_);
        std::size_t appended = 0;
        for (std::size_t lo = 0; lo < conditions.size(); lo += batch_size_) {
            const std::size_t hi = std::min(conditions.size(), lo + batch_size_);
            const auto query = data_url_ + "/trades?market=" + pm::url_encode(join_conditions(conditions, lo, hi))
                + "&after=" + std::to_string(start) + "&before=" + std::to_string(end);
            json::value response;
            try {
                response = pm::http_json_get(query, cfg_.http_timeout_ms);
            } catch (const std::exception& e) {
                std::cerr << "trade-recorder request failed: " << e.what() << '\n';
                continue;
            }
            const json::array* rows = nullptr;
            if (response.is_array()) rows = &response.as_array();
            else if (response.is_object()) {
                const auto& root = response.as_object();
                auto it = root.find("trades");
                if (it != root.end() && it->value().is_array()) rows = &it->value().as_array();
            }
            if (!rows) continue;
            for (const auto& value : *rows) {
                if (!value.is_object()) continue;
                const auto& row = value.as_object();
                Trade trade;
                trade.received_ms = now_ms();
                trade.ts = get_integer(row, "timestamp");
                trade.condition_id = get_text(row, "conditionId");
                if (trade.condition_id.empty()) trade.condition_id = get_text(row, "market");
                trade.asset_id = get_text(row, "asset");
                if (trade.asset_id.empty()) trade.asset_id = get_text(row, "asset_id");
                trade.outcome = get_text(row, "outcome");
                trade.side = get_text(row, "side");
                trade.price = get_number(row, "price");
                trade.size = get_number(row, "size");
                trade.tx_hash = get_text(row, "transactionHash");
                if (trade.tx_hash.empty()) trade.tx_hash = get_text(row, "transaction_hash");
                auto market_it = by_condition.find(trade.condition_id);
                if (market_it != by_condition.end()) {
                    trade.slug = market_it->second->slug;
                    trade.event_slug = market_it->second->event_slug;
                }
                if (trade.ts <= 0 || trade.asset_id.empty() || trade.side.empty()
                    || !(trade.price > 0.0 && trade.price < 1.0) || !(trade.size > 0.0)) continue;
                const auto key = trade_key(trade);
                if (!seen_.insert(key).second) continue;
                append(trade);
                last_trade_ts_ = std::max(last_trade_ts_, trade.ts);
                ++appended;
            }
        }
        trim_seen();
        persist_state();
        std::cout << "trade-recorder markets=" << markets.size()
                  << " conditions=" << conditions.size()
                  << " appended=" << appended
                  << " elapsed_ms=" << (now_ms() - started_ms)
                  << " last_trade_ts=" << last_trade_ts_ << '\n';
    }

private:
    pm::Config cfg_;
    pm::Api api_;
    std::string run_dir_;
    std::string data_url_;
    std::size_t batch_size_;
    std::int64_t lookback_s_;
    fs::path tape_path_;
    fs::path state_path_;
    std::unordered_set<std::string> seen_;
    std::int64_t last_trade_ts_ = 0;

    void append(const Trade& t) {
        std::ofstream f(tape_path_, std::ios::app);
        if (!f) throw std::runtime_error("Cannot append " + tape_path_.string());
        const auto lag_ms = t.received_ms - t.ts * 1000;
        f << t.ts << ',' << t.received_ms << ',' << lag_ms << ','
          << csv_escape(t.condition_id) << ',' << csv_escape(t.asset_id) << ','
          << csv_escape(t.outcome) << ',' << csv_escape(t.side) << ','
          << std::setprecision(16) << t.price << ',' << t.size << ','
          << csv_escape(t.tx_hash) << ',' << csv_escape(t.slug) << ','
          << csv_escape(t.event_slug) << '\n';
    }

    void load_seen() {
        if (!fs::exists(tape_path_)) return;
        std::ifstream f(tape_path_);
        std::string line;
        std::getline(f, line);
        const auto cutoff = now_s() - 2 * lookback_s_;
        while (std::getline(f, line)) {
            const auto fields = split_csv(line);
            if (fields.size() < 12) continue;
            Trade t;
            try {
                t.ts = std::stoll(fields[0]);
                if (t.ts < cutoff) continue;
                t.received_ms = std::stoll(fields[1]);
                t.condition_id = fields[3];
                t.asset_id = fields[4];
                t.outcome = fields[5];
                t.side = fields[6];
                t.price = std::stod(fields[7]);
                t.size = std::stod(fields[8]);
                t.tx_hash = fields[9];
                t.slug = fields[10];
                t.event_slug = fields[11];
                seen_.insert(trade_key(t));
                last_trade_ts_ = std::max(last_trade_ts_, t.ts);
            } catch (...) {
            }
        }
    }

    void trim_seen() {
        // Overlap replay only needs keys from the recent polling horizon. Rebuild
        // from disk once the in-memory set grows large; load_seen deliberately
        // retains only recent rows, so this actually bounds memory.
        if (seen_.size() < 250000) return;
        seen_.clear();
        load_seen();
    }

    void persist_state() const {
        std::ofstream f(state_path_);
        f << "last_trade_ts,seen_count\n" << last_trade_ts_ << ',' << seen_.size() << '\n';
    }
};

} // namespace

int main(int argc, char** argv) {
    try {
        std::string config = "config/v7_market_data_recorder.json";
        std::string run_dir = "runs/v7-paper";
        std::string data_url = "https://data-api.polymarket.com";
        std::size_t markets = 1000, batch = 100;
        double min_liquidity = 2.0;
        std::int64_t lookback_s = 300;
        int interval_s = 10;
        bool loop = false;

        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::runtime_error("Missing value after " + a);
                return std::string(argv[++i]);
            };
            if (a == "--config") config = next();
            else if (a == "--run-dir") run_dir = next();
            else if (a == "--data-url") data_url = next();
            else if (a == "--markets") markets = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--batch") batch = static_cast<std::size_t>(std::stoull(next()));
            else if (a == "--min-liquidity") min_liquidity = std::stod(next());
            else if (a == "--lookback-seconds") lookback_s = std::stoll(next());
            else if (a == "--interval") interval_s = std::stoi(next());
            else if (a == "--loop") loop = true;
            else if (a == "--once") loop = false;
            else if (a == "--help" || a == "-h") {
                std::cout << "polymarket_trade_recorder [--config FILE] [--run-dir DIR] [--data-url URL] "
                             "[--markets N] [--batch N] [--min-liquidity X] [--lookback-seconds N] "
                             "[--interval N] [--once|--loop]\n";
                return 0;
            } else {
                throw std::runtime_error("Unknown argument: " + a);
            }
        }

        auto cfg = pm::Engine::load_config(config);
        Recorder r(cfg, run_dir, data_url, batch, lookback_s);
        do {
            r.tick(markets, min_liquidity);
            if (loop) std::this_thread::sleep_for(std::chrono::seconds(std::max(1, interval_s)));
        } while (loop);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << '\n';
        return 1;
    }
}
