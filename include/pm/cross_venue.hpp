#pragma once

#include "pm/api.hpp"
#include "pm/http.hpp"
#include "pm/types.hpp"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace pm::cross {

enum class Venue { Polymarket, Limitless, Kalshi };
enum class Relation { SameEvent, ComplementEvent };
enum class Outcome { Yes, No };

[[nodiscard]] const char* to_string(Venue venue);
[[nodiscard]] const char* to_string(Relation relation);
[[nodiscard]] const char* to_string(Outcome outcome);
[[nodiscard]] std::optional<Venue> parse_venue(std::string_view value);
[[nodiscard]] std::optional<Relation> parse_relation(std::string_view value);

struct Credentials {
    std::string limitless_token_id;
    std::string limitless_token_secret;
    std::string limitless_account;
    bool limitless_execution_enabled = false;

    std::string kalshi_key_id;
    std::filesystem::path kalshi_private_key_path;
    std::string kalshi_environment = "production";
    bool kalshi_execution_enabled = false;

    [[nodiscard]] bool has_limitless_auth() const;
    [[nodiscard]] bool has_kalshi_auth() const;
    [[nodiscard]] bool any_execution_requested() const;
};

[[nodiscard]] Credentials load_credentials(
    const std::filesystem::path& env_file = "~/.config/polymarket/venues.env");
[[nodiscard]] std::filesystem::path expand_user_path(const std::filesystem::path& path);
[[nodiscard]] bool private_file_permissions_ok(const std::filesystem::path& path);

struct SignedHeaders {
    std::string timestamp;
    std::vector<std::pair<std::string, std::string>> headers;
};

[[nodiscard]] SignedHeaders sign_limitless_request(
    const Credentials& credentials,
    std::string_view method,
    std::string_view path_with_query,
    std::string_view body = {},
    std::optional<std::string> timestamp = std::nullopt);

[[nodiscard]] SignedHeaders sign_kalshi_request(
    const Credentials& credentials,
    std::string_view method,
    std::string_view path_with_optional_query,
    std::optional<std::int64_t> timestamp_ms = std::nullopt);

struct VenueMarket {
    Venue venue = Venue::Polymarket;
    std::string market_id;
    std::string event_id;
    std::string slug;
    std::string title;
    std::string yes_token;
    std::string no_token;
    std::string rules_primary;
    std::string rules_secondary;
    std::int64_t close_ts_ms = 0;
    double liquidity = 0.0;
    double volume24h = 0.0;
    bool active = true;
};

struct OutcomeBooks {
    Book yes;
    Book no;
    std::int64_t exchange_ts_ms = 0;
    std::int64_t received_ts_ms = 0;
};

struct VerifiedPair {
    std::string pair_id;
    Venue venue_a = Venue::Polymarket;
    std::string market_a;
    Venue venue_b = Venue::Limitless;
    std::string market_b;
    Relation relation = Relation::SameEvent;
    bool settlement_verified = false;
    std::int64_t max_close_diff_seconds = 3600;
    std::string notes;
};

struct CandidateMatch {
    Venue venue_a = Venue::Polymarket;
    std::string market_a;
    Venue venue_b = Venue::Limitless;
    std::string market_b;
    double similarity = 0.0;
    std::int64_t close_diff_seconds = 0;
    bool numeric_tokens_match = false;
    bool eligible_for_review = false;
};

struct Policy {
    std::size_t polymarket_market_limit = 1000;
    std::size_t limitless_market_limit = 250;
    std::size_t kalshi_market_limit = 1000;
    double polymarket_min_liquidity = 0.0;
    double min_net_edge_per_share = 0.0025;
    double slippage_bps = 3.0;
    double latency_reserve_bps = 5.0;
    double max_bundle_usd = 25.0;
    double max_open_capital_usd = 250.0;
    double min_shares = 1.0;
    double auto_match_min_similarity = 0.88;
    std::int64_t auto_match_max_close_diff_seconds = 3600;
    std::int64_t pair_cooldown_seconds = 60;
    bool paper_trade_enabled = true;
    bool require_supervisor_gate = true;
    bool require_verified_settlement = true;
    std::filesystem::path supervisor_limits = "runs/supervisor/capital_limits.json";
    std::map<Venue, double> fee_reserve_bps{
        {Venue::Polymarket, 10.0},
        {Venue::Limitless, 15.0},
        {Venue::Kalshi, 15.0},
    };
};

struct EngineConfig {
    int schema_version = 1;
    std::filesystem::path polymarket_config = "config/paper_v4.json";
    std::filesystem::path pair_manifest = "config/cross_venue_pairs.csv";
    std::filesystem::path credentials_file = "~/.config/polymarket/venues.env";
    std::filesystem::path run_dir = "runs/cross_venue";
    std::string limitless_base_url = "https://api.limitless.exchange";
    std::string kalshi_production_base_url = "https://external-api.kalshi.com";
    std::string kalshi_demo_base_url = "https://external-api.demo.kalshi.co";
    std::int64_t interval_ms = 3000;
    double starting_capital = 10000.0;
    Policy policy;
};

[[nodiscard]] EngineConfig load_engine_config(const std::filesystem::path& path);
[[nodiscard]] std::vector<VerifiedPair> load_pair_manifest(const std::filesystem::path& path);
[[nodiscard]] std::string canonical_event_text(std::string_view value);
[[nodiscard]] double title_similarity(std::string_view left, std::string_view right);
[[nodiscard]] bool numeric_tokens_equal(std::string_view left, std::string_view right);
[[nodiscard]] std::vector<CandidateMatch> build_candidate_matches(
    const std::vector<VenueMarket>& left,
    const std::vector<VenueMarket>& right,
    const Policy& policy);

[[nodiscard]] VenueMarket parse_limitless_market_payload(const std::string& payload);
[[nodiscard]] std::vector<VenueMarket> parse_limitless_active_markets_payload(
    const std::string& payload,
    std::size_t limit = 0);
[[nodiscard]] OutcomeBooks parse_limitless_orderbook_payload(
    const std::string& payload,
    std::string yes_token = {},
    std::string no_token = {});

[[nodiscard]] std::vector<VenueMarket> parse_kalshi_markets_payload(
    const std::string& payload,
    std::string* next_cursor = nullptr);
[[nodiscard]] OutcomeBooks parse_kalshi_orderbook_payload(
    const std::string& payload,
    std::string ticker);

class LimitlessClient {
public:
    LimitlessClient(Credentials credentials, std::string base_url);
    [[nodiscard]] std::vector<VenueMarket> discover_markets(std::size_t limit) const;
    [[nodiscard]] VenueMarket fetch_market(std::string_view slug) const;
    [[nodiscard]] OutcomeBooks fetch_books(const VenueMarket& market) const;
    [[nodiscard]] HttpResponse auth_check() const;

private:
    [[nodiscard]] HttpResponse signed_get(std::string path_with_query) const;
    Credentials credentials_;
    std::string base_url_;
    HttpClient http_;
};

class KalshiClient {
public:
    KalshiClient(Credentials credentials, std::string production_base_url, std::string demo_base_url);
    [[nodiscard]] std::vector<VenueMarket> discover_markets(std::size_t limit) const;
    [[nodiscard]] OutcomeBooks fetch_books(const VenueMarket& market) const;
    [[nodiscard]] HttpResponse auth_check() const;
    [[nodiscard]] std::string base_url() const;

private:
    [[nodiscard]] HttpResponse signed_get(std::string path_with_query) const;
    Credentials credentials_;
    std::string production_base_url_;
    std::string demo_base_url_;
    HttpClient http_;
};

struct LegQuote {
    Venue venue = Venue::Polymarket;
    std::string market_id;
    Outcome outcome = Outcome::Yes;
    double shares = 0.0;
    double average_price = 0.0;
    double raw_cost = 0.0;
    double fee_reserve = 0.0;
};

struct Opportunity {
    std::string opportunity_id;
    std::string pair_id;
    Relation relation = Relation::SameEvent;
    bool settlement_verified = false;
    bool executable = false;
    std::string reject_reason;
    double shares = 0.0;
    double payoff_floor = 0.0;
    double raw_cost = 0.0;
    double total_cost = 0.0;
    double net_profit = 0.0;
    double net_edge_per_share = -1.0;
    std::int64_t decision_ts_ms = 0;
    LegQuote leg_a;
    LegQuote leg_b;
};

[[nodiscard]] std::vector<Opportunity> evaluate_pair(
    const VerifiedPair& pair,
    const VenueMarket& market_a,
    const OutcomeBooks& books_a,
    const VenueMarket& market_b,
    const OutcomeBooks& books_b,
    const Policy& policy,
    double capital_limit_usd);

struct SupervisorGate {
    bool available = false;
    bool global_kill = true;
    bool new_exposure_allowed = false;
    double capital_limit_usd = 0.0;
    double max_bundle_usd = 0.0;
    std::int64_t timestamp = 0;
    std::string reason;
};

[[nodiscard]] SupervisorGate load_supervisor_gate(const std::filesystem::path& path);

class CrossVenueEngine {
public:
    explicit CrossVenueEngine(EngineConfig config);
    int run_once();
    int run_loop();

private:
    EngineConfig config_;
    Credentials credentials_;
};

} // namespace pm::cross
