    if (!output) throw std::runtime_error("cannot append paper bundle ledger");
    if (new_file) {
        output << "timestamp,opportunity_id,pair_id,relation,venue_a,market_a,outcome_a,venue_b,market_b,"
                  "outcome_b,shares,average_price_a,average_price_b,raw_cost,total_cost,payoff_floor,"
                  "expected_profit,net_edge_per_share,status\n";
    }
    output << opportunity.decision_ts_ms << ',' << csv_escape(opportunity.opportunity_id) << ','
           << csv_escape(opportunity.pair_id) << ',' << to_string(opportunity.relation) << ','
           << to_string(opportunity.leg_a.venue) << ',' << csv_escape(opportunity.leg_a.market_id) << ','
           << to_string(opportunity.leg_a.outcome) << ',' << to_string(opportunity.leg_b.venue) << ','
           << csv_escape(opportunity.leg_b.market_id) << ',' << to_string(opportunity.leg_b.outcome) << ','
           << std::setprecision(17) << opportunity.shares << ',' << opportunity.leg_a.average_price << ','
           << opportunity.leg_b.average_price << ',' << opportunity.raw_cost << ',' << opportunity.total_cost
           << ',' << opportunity.payoff_floor << ',' << opportunity.net_profit << ','
           << opportunity.net_edge_per_share << ",paper_locked_open\n";
}


void write_market_catalog(
    const std::filesystem::path& path,
    const std::map<Venue, std::vector<VenueMarket>>& markets) {
    json::array rows;
    for (const auto& [venue, venue_markets] : markets) {
        for (const auto& market : venue_markets) {
            json::object row;
            row["venue"] = to_string(venue);
            row["market_id"] = market.market_id;
            row["event_id"] = market.event_id;
            row["slug"] = market.slug;
            row["title"] = market.title;
            row["yes_token"] = market.yes_token;
            row["no_token"] = market.no_token;
            row["rules_primary"] = market.rules_primary;
            row["rules_secondary"] = market.rules_secondary;
            row["close_ts_ms"] = market.close_ts_ms;
            row["liquidity"] = market.liquidity;
            row["volume24h"] = market.volume24h;
            row["active"] = market.active;
            rows.emplace_back(std::move(row));
        }
    }
    json::object root;
    root["schema_version"] = 1;
    root["timestamp"] = now_seconds();
    root["markets"] = std::move(rows);
    atomic_write(path, json::serialize(root));
}

void write_candidates(const std::filesystem::path& path, const std::vector<CandidateMatch>& matches) {
    std::ostringstream output;
    output << "venue_a,market_a,venue_b,market_b,similarity,close_diff_seconds,numeric_tokens_match,"
              "eligible_for_review\n";
    for (const auto& match : matches) {
        output << to_string(match.venue_a) << ',' << csv_escape(match.market_a) << ','
               << to_string(match.venue_b) << ',' << csv_escape(match.market_b) << ','
               << std::setprecision(17) << match.similarity << ',' << match.close_diff_seconds << ','
               << (match.numeric_tokens_match ? 1 : 0) << ',' << (match.eligible_for_review ? 1 : 0) << '\n';
    }
    atomic_write(path, output.str());
}

void write_opportunities(const std::filesystem::path& path, const std::vector<Opportunity>& opportunities) {
    std::ostringstream output;
    output << "timestamp,opportunity_id,pair_id,relation,verified,executable,reject_reason,venue_a,market_a,"
              "outcome_a,venue_b,market_b,outcome_b,shares,raw_cost,total_cost,payoff_floor,net_profit,"
              "net_edge_per_share\n";
    for (const auto& opportunity : opportunities) {
        output << opportunity.decision_ts_ms << ',' << csv_escape(opportunity.opportunity_id) << ','
               << csv_escape(opportunity.pair_id) << ',' << to_string(opportunity.relation) << ','
               << (opportunity.settlement_verified ? 1 : 0) << ',' << (opportunity.executable ? 1 : 0) << ','
               << csv_escape(opportunity.reject_reason) << ',' << to_string(opportunity.leg_a.venue) << ','
               << csv_escape(opportunity.leg_a.market_id) << ',' << to_string(opportunity.leg_a.outcome) << ','
               << to_string(opportunity.leg_b.venue) << ',' << csv_escape(opportunity.leg_b.market_id) << ','
               << to_string(opportunity.leg_b.outcome) << ',' << std::setprecision(17) << opportunity.shares << ','
               << opportunity.raw_cost << ',' << opportunity.total_cost << ',' << opportunity.payoff_floor << ','
               << opportunity.net_profit << ',' << opportunity.net_edge_per_share << '\n';
    }
    atomic_write(path, output.str());
}

std::string opportunity_id(
    const VerifiedPair& pair,
    Outcome left,
    Outcome right,
    std::int64_t timestamp) {
    std::ostringstream out;
    out << pair.pair_id << ':' << to_string(left) << ':' << to_string(right) << ':' << timestamp;
    return out.str();
}

void write_runtime_status(
    const EngineConfig& config,
    const SupervisorGate& gate,
    const std::map<Venue, std::vector<VenueMarket>>& markets,
    std::size_t verified_pairs,
    std::size_t candidate_count,
    const std::vector<Opportunity>& opportunities,
    std::size_t paper_entries,
    const PaperState& state,
    const Credentials& credentials,
    bool healthy,
    std::string error = {}) {
    json::object counts;
    for (const auto& [venue, venue_markets] : markets) counts[to_string(venue)] = venue_markets.size();
    std::size_t executable = 0;
    double best_edge = -1.0;
    for (const auto& opportunity : opportunities) {
        if (opportunity.executable) ++executable;
        best_edge = std::max(best_edge, opportunity.net_edge_per_share);
    }
    json::object auth;
    auth["limitless_configured"] = credentials.has_limitless_auth();
    auth["kalshi_configured"] = credentials.has_kalshi_auth();
    auth["limitless_execution_requested"] = credentials.limitless_execution_enabled;
    auth["kalshi_execution_requested"] = credentials.kalshi_execution_enabled;

    json::object supervisor;
    supervisor["available"] = gate.available;
    supervisor["global_kill"] = gate.global_kill;
    supervisor["new_exposure_allowed"] = gate.new_exposure_allowed;
    supervisor["capital_limit_usd"] = gate.capital_limit_usd;
    supervisor["max_bundle_usd"] = gate.max_bundle_usd;
    supervisor["reason"] = gate.reason;

    json::object object;
    const double equity = state.cash + state.reserved_capital;
    const double drawdown = state.peak_equity > 0.0 ? std::max(0.0, 1.0 - equity / state.peak_equity) : 0.0;
    object["schema_version"] = 1;
    object["schema"] = "polymarket_runtime_status_v1";
    object["engine"] = "cross_venue";
    object["timestamp"] = now_seconds();
    object["healthy"] = healthy;
    object["paper_only"] = true;
    object["authenticated_order_submission_compiled"] = false;
    object["error"] = std::move(error);
    object["market_counts"] = std::move(counts);
    object["verified_pairs"] = verified_pairs;
    object["candidate_matches"] = candidate_count;
    object["opportunities"] = opportunities.size();
    object["executable_opportunities"] = executable;
    object["paper_entries_this_cycle"] = paper_entries;
    object["best_net_edge_per_share"] = best_edge;
    object["cash"] = state.cash;
    object["reserved_capital"] = state.reserved_capital;
    object["reserved_cash"] = state.cash;
    object["gross_exposure"] = state.reserved_capital;
    object["expected_locked_profit"] = state.expected_locked_profit;
    object["equity"] = equity;
    object["pnl"] = 0.0;
    object["realized_pnl"] = 0.0;
    object["drawdown"] = drawdown;
    object["live_units"] = state.last_pair_trade.size();
    object["execution_imbalance"] = 0.0;
    object["execution_staleness"] = 0.0;
    object["killed"] = state.killed;
    object["auth"] = std::move(auth);
    object["supervisor"] = std::move(supervisor);
    atomic_write(config.run_dir / "runtime_status.json", json::serialize(object));
}

} // namespace

const char* to_string(Venue venue) {
    switch (venue) {
        case Venue::Polymarket: return "polymarket";
        case Venue::Limitless: return "limitless";
        case Venue::Kalshi: return "kalshi";
    }
    return "unknown";
}

const char* to_string(Relation relation) {
    switch (relation) {
        case Relation::SameEvent: return "same_event";
        case Relation::ComplementEvent: return "complement_event";
    }
    return "unknown";
}

const char* to_string(Outcome outcome) { return outcome == Outcome::Yes ? "YES" : "NO"; }

std::optional<Venue> parse_venue(std::string_view value) {
    const auto normalized = lower_ascii(trim(std::string(value)));
    if (normalized == "polymarket" || normalized == "poly") return Venue::Polymarket;
    if (normalized == "limitless" || normalized == "lmts") return Venue::Limitless;
    if (normalized == "kalshi") return Venue::Kalshi;
    return std::nullopt;
}

std::optional<Relation> parse_relation(std::string_view value) {
    const auto normalized = lower_ascii(trim(std::string(value)));
    if (normalized == "same_event" || normalized == "same" || normalized == "equivalent")
        return Relation::SameEvent;
    if (normalized == "complement_event" || normalized == "complement" || normalized == "opposite")
        return Relation::ComplementEvent;
    return std::nullopt;
}

bool Credentials::has_limitless_auth() const {
    return !limitless_token_id.empty() && !limitless_token_secret.empty();
}

bool Credentials::has_kalshi_auth() const {
    return !kalshi_key_id.empty() && !kalshi_private_key_path.empty() &&
           std::filesystem::exists(kalshi_private_key_path);
}

bool Credentials::any_execution_requested() const {
    return limitless_execution_enabled || kalshi_execution_enabled;
}

std::filesystem::path expand_user_path(const std::filesystem::path& path) {
    const std::string raw = path.string();
    if (raw == "~" || raw.rfind("~/", 0) == 0) {
        const char* home = std::getenv("HOME");
        if (home == nullptr || *home == '\0') return path;
        return raw == "~" ? std::filesystem::path(home)
                          : std::filesystem::path(home) / raw.substr(2);
    }
    return path;
}

bool private_file_permissions_ok(const std::filesystem::path& raw_path) {
    const auto path = expand_user_path(raw_path);
#if defined(_WIN32)
    return std::filesystem::exists(path);
#else
    struct stat info {};
    if (::stat(path.c_str(), &info) != 0) return false;
    return (info.st_mode & 0077) == 0;
#endif
}

Credentials load_credentials(const std::filesystem::path& env_file) {
    const auto expanded_env_file = expand_user_path(env_file);
    if (std::filesystem::exists(expanded_env_file) && !private_file_permissions_ok(expanded_env_file)) {
        throw std::runtime_error("credentials file permissions are not private (expected mode 600): " +
                                 expanded_env_file.string());
    }
    const auto values = load_env_map(expanded_env_file);
    Credentials credentials;
    credentials.limitless_token_id = first_value(values, {"LIMITLESS_TOKEN_ID", "LMTS_TOKEN_ID"});
    credentials.limitless_token_secret =
        first_value(values, {"LIMITLESS_TOKEN_SECRET", "LMTS_TOKEN_SECRET"});
    credentials.limitless_account = first_value(values, {"LIMITLESS_ACCOUNT", "LMTS_ACCOUNT"});
    credentials.limitless_execution_enabled = bool_value(
        first_value(values, {"LIMITLESS_EXECUTION_ENABLED"}), false);

    credentials.kalshi_key_id = first_value(values, {"KALSHI_API_KEY_ID", "KALSHI_KEY_ID"});
    const auto key_path = first_value(values, {"KALSHI_PRIVATE_KEY_PATH", "KALSHI_KEY_PATH"});
    if (!key_path.empty()) credentials.kalshi_private_key_path = expand_user_path(key_path);
    credentials.kalshi_environment =
        lower_ascii(first_value(values, {"KALSHI_ENV", "KALSHI_ENVIRONMENT"}));
    if (credentials.kalshi_environment.empty()) credentials.kalshi_environment = "production";
    credentials.kalshi_execution_enabled =
        bool_value(first_value(values, {"KALSHI_EXECUTION_ENABLED"}), false);

    if (!credentials.kalshi_private_key_path.empty() &&
        std::filesystem::exists(credentials.kalshi_private_key_path) &&
        !private_file_permissions_ok(credentials.kalshi_private_key_path)) {
        throw std::runtime_error("Kalshi private key permissions are not private (expected mode 600)");
    }
    if (credentials.any_execution_requested()) {
        if (credentials.limitless_execution_enabled && !credentials.has_limitless_auth())
            throw std::runtime_error("Limitless execution requested without complete credentials");
        if (credentials.kalshi_execution_enabled && !credentials.has_kalshi_auth())
            throw std::runtime_error("Kalshi execution requested without complete credentials");
        if (credentials.kalshi_execution_enabled &&
            !private_file_permissions_ok(credentials.kalshi_private_key_path)) {
            throw std::runtime_error("Kalshi private key permissions are not private (expected mode 600)");
        }
    }
    return credentials;
}

SignedHeaders sign_limitless_request(
    const Credentials& credentials,
    std::string_view method,
    std::string_view path_with_query,
    std::string_view body,
    std::optional<std::string> timestamp) {
    if (!credentials.has_limitless_auth()) throw std::runtime_error("Limitless credentials are missing");
    SignedHeaders signed_headers;
    signed_headers.timestamp = timestamp.value_or(iso8601_now());
    const std::string normalized_method = [&] {
        std::string value(method);
        std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
            return static_cast<char>(std::toupper(c));
        });
        return value;
    }();
    const std::string message = signed_headers.timestamp + "\n" + normalized_method + "\n" +
                                std::string(path_with_query) + "\n" + std::string(body);
    const auto secret = base64_decode(credentials.limitless_token_secret);
    const auto signature = hmac_sha256_base64(secret, message);
    signed_headers.headers = {
        {"lmts-api-key", credentials.limitless_token_id},
        {"lmts-timestamp", signed_headers.timestamp},
        {"lmts-signature", signature},
    };
