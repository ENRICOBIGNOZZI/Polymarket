    return signed_headers;
}

SignedHeaders sign_kalshi_request(
    const Credentials& credentials,
    std::string_view method,
    std::string_view path_with_optional_query,
    std::optional<std::int64_t> timestamp_ms) {
    if (!credentials.has_kalshi_auth()) throw std::runtime_error("Kalshi credentials are missing");
    SignedHeaders signed_headers;
    signed_headers.timestamp = std::to_string(timestamp_ms.value_or(now_ms()));
    std::string normalized_method(method);
    std::transform(normalized_method.begin(), normalized_method.end(), normalized_method.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    std::string path(path_with_optional_query);
    if (const auto query = path.find('?'); query != std::string::npos) path.resize(query);
    const std::string message = signed_headers.timestamp + normalized_method + path;
    const auto signature = rsa_pss_sha256_base64(credentials.kalshi_private_key_path, message);
    signed_headers.headers = {
        {"KALSHI-ACCESS-KEY", credentials.kalshi_key_id},
        {"KALSHI-ACCESS-SIGNATURE", signature},
        {"KALSHI-ACCESS-TIMESTAMP", signed_headers.timestamp},
    };
    return signed_headers;
}

EngineConfig load_engine_config(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open cross-venue config: " + path.string());
    std::ostringstream buffer;
    buffer << input.rdbuf();
    const auto root = json::parse(buffer.str());
    if (!root.is_object()) throw std::runtime_error("cross-venue config must be a JSON object");
    const auto& object = root.as_object();
    EngineConfig config;
    config.schema_version = static_cast<int>(object_int64(object, "schema_version", 1));
    if (config.schema_version != 1) throw std::runtime_error("unsupported cross-venue config schema");
    if (const auto value = object_string(object, "polymarket_config"); !value.empty())
        config.polymarket_config = value;
    if (const auto value = object_string(object, "pair_manifest"); !value.empty()) config.pair_manifest = value;
    if (const auto value = object_string(object, "credentials_file"); !value.empty())
        config.credentials_file = value;
    if (const auto value = object_string(object, "run_dir"); !value.empty()) config.run_dir = value;
    if (const auto value = object_string(object, "limitless_base_url"); !value.empty())
        config.limitless_base_url = value;
    if (const auto value = object_string(object, "kalshi_production_base_url"); !value.empty())
        config.kalshi_production_base_url = value;
    if (const auto value = object_string(object, "kalshi_demo_base_url"); !value.empty())
        config.kalshi_demo_base_url = value;
    config.interval_ms = object_int64(object, "interval_ms", config.interval_ms);
    config.starting_capital = object_double(object, "starting_capital", config.starting_capital);

    if (const auto* policy_value = find_value(object, "policy"); policy_value && policy_value->is_object()) {
        const auto& policy = policy_value->as_object();
        config.policy.polymarket_market_limit = static_cast<std::size_t>(
            object_int64(policy, "polymarket_market_limit", config.policy.polymarket_market_limit));
        config.policy.limitless_market_limit = static_cast<std::size_t>(
            object_int64(policy, "limitless_market_limit", config.policy.limitless_market_limit));
        config.policy.kalshi_market_limit = static_cast<std::size_t>(
            object_int64(policy, "kalshi_market_limit", config.policy.kalshi_market_limit));
        config.policy.polymarket_min_liquidity =
            object_double(policy, "polymarket_min_liquidity", config.policy.polymarket_min_liquidity);
        config.policy.min_net_edge_per_share =
            object_double(policy, "min_net_edge_per_share", config.policy.min_net_edge_per_share);
        config.policy.slippage_bps = object_double(policy, "slippage_bps", config.policy.slippage_bps);
        config.policy.latency_reserve_bps =
            object_double(policy, "latency_reserve_bps", config.policy.latency_reserve_bps);
        config.policy.max_bundle_usd = object_double(policy, "max_bundle_usd", config.policy.max_bundle_usd);
        config.policy.max_open_capital_usd =
            object_double(policy, "max_open_capital_usd", config.policy.max_open_capital_usd);
        config.policy.min_shares = object_double(policy, "min_shares", config.policy.min_shares);
        config.policy.auto_match_min_similarity =
            object_double(policy, "auto_match_min_similarity", config.policy.auto_match_min_similarity);
        config.policy.auto_match_max_close_diff_seconds = object_int64(
            policy, "auto_match_max_close_diff_seconds", config.policy.auto_match_max_close_diff_seconds);
        config.policy.pair_cooldown_seconds =
            object_int64(policy, "pair_cooldown_seconds", config.policy.pair_cooldown_seconds);
        config.policy.paper_trade_enabled =
            object_bool(policy, "paper_trade_enabled", config.policy.paper_trade_enabled);
        config.policy.require_supervisor_gate =
            object_bool(policy, "require_supervisor_gate", config.policy.require_supervisor_gate);
        config.policy.require_verified_settlement =
            object_bool(policy, "require_verified_settlement", config.policy.require_verified_settlement);
        if (const auto value = object_string(policy, "supervisor_limits"); !value.empty())
            config.policy.supervisor_limits = value;
        if (const auto* fees = find_value(policy, "fee_reserve_bps"); fees && fees->is_object()) {
            for (const auto& item : fees->as_object()) {
                const auto venue = parse_venue(item.key());
                if (venue) config.policy.fee_reserve_bps[*venue] = json_double(item.value(), 0.0);
            }
        }
    }
    if (config.interval_ms < 100) config.interval_ms = 100;
    if (config.policy.max_bundle_usd <= 0.0 || config.policy.max_open_capital_usd <= 0.0)
        throw std::runtime_error("cross-venue capital limits must be positive");
    return config;
}

std::vector<VerifiedPair> load_pair_manifest(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open cross-venue pair manifest: " + path.string());
    std::string header_line;
    if (!std::getline(input, header_line)) return {};
    const auto header = parse_csv_row(header_line);
    std::unordered_map<std::string, std::size_t> columns;
    for (std::size_t i = 0; i < header.size(); ++i) columns[lower_ascii(header[i])] = i;
    const std::array<const char*, 7> required{
        "pair_id", "venue_a", "market_a", "venue_b", "market_b", "relation", "settlement_verified"};
    for (const char* key : required) {
        if (!columns.contains(key)) throw std::runtime_error("pair manifest missing column: " + std::string(key));
    }
    auto field = [&](const std::vector<std::string>& row, std::string_view key) -> std::string {
        const auto it = columns.find(std::string(key));
        return it == columns.end() || it->second >= row.size() ? std::string{} : row[it->second];
    };

    std::vector<VerifiedPair> pairs;
    std::unordered_set<std::string> seen;
    std::string line;
    std::size_t line_number = 1;
    while (std::getline(input, line)) {
        ++line_number;
        if (trim(line).empty() || trim(line).front() == '#') continue;
        const auto row = parse_csv_row(line);
        VerifiedPair pair;
        pair.pair_id = field(row, "pair_id");
        const auto venue_a = parse_venue(field(row, "venue_a"));
        const auto venue_b = parse_venue(field(row, "venue_b"));
        const auto relation = parse_relation(field(row, "relation"));
        if (pair.pair_id.empty() || !venue_a || !venue_b || !relation)
            throw std::runtime_error("invalid pair manifest row " + std::to_string(line_number));
        if (*venue_a == *venue_b)
            throw std::runtime_error("cross-venue pair must use two distinct venues at row " +
                                     std::to_string(line_number));
        if (!seen.insert(pair.pair_id).second)
            throw std::runtime_error("duplicate pair_id: " + pair.pair_id);
        pair.venue_a = *venue_a;
        pair.venue_b = *venue_b;
        pair.market_a = field(row, "market_a");
        pair.market_b = field(row, "market_b");
        pair.relation = *relation;
        pair.settlement_verified = bool_value(field(row, "settlement_verified"), false);
        const auto close_diff = field(row, "max_close_diff_seconds");
        if (!close_diff.empty()) pair.max_close_diff_seconds = std::stoll(close_diff);
        pair.notes = field(row, "notes");
        if (pair.market_a.empty() || pair.market_b.empty())
            throw std::runtime_error("pair manifest market identifier is empty at row " +
                                     std::to_string(line_number));
        pairs.push_back(std::move(pair));
    }
    return pairs;
}

std::string canonical_event_text(std::string_view value) {
    std::string output;
    output.reserve(value.size());
    bool previous_space = true;
    for (const unsigned char c : value) {
        if (std::isalnum(c)) {
            output.push_back(static_cast<char>(std::tolower(c)));
            previous_space = false;
        } else if (!previous_space) {
            output.push_back(' ');
            previous_space = true;
        }
    }
    return trim(std::move(output));
}

double title_similarity(std::string_view left, std::string_view right) {
    const auto left_vector = text_tokens(left);
    const auto right_vector = text_tokens(right);
    if (left_vector.empty() || right_vector.empty()) return 0.0;
    const std::unordered_set<std::string> left_tokens(left_vector.begin(), left_vector.end());
    const std::unordered_set<std::string> right_tokens(right_vector.begin(), right_vector.end());
    std::size_t intersection = 0;
    for (const auto& token : left_tokens) {
        if (right_tokens.contains(token)) ++intersection;
    }
    const std::size_t union_size = left_tokens.size() + right_tokens.size() - intersection;
    if (union_size == 0) return 0.0;
    const double jaccard = static_cast<double>(intersection) / static_cast<double>(union_size);
    const std::string canonical_left = canonical_event_text(left);
    const std::string canonical_right = canonical_event_text(right);
    if (canonical_left == canonical_right) return 1.0;
    const double containment = static_cast<double>(intersection) /
                               static_cast<double>(std::min(left_tokens.size(), right_tokens.size()));
    return std::clamp(0.65 * jaccard + 0.35 * containment, 0.0, 1.0);
}

bool numeric_tokens_equal(std::string_view left, std::string_view right) {
    return numeric_tokens(left) == numeric_tokens(right);
}

std::vector<CandidateMatch> build_candidate_matches(
    const std::vector<VenueMarket>& left,
    const std::vector<VenueMarket>& right,
    const Policy& policy) {
    std::vector<CandidateMatch> matches;
    for (const auto& left_market : left) {
        if (!left_market.active) continue;
        for (const auto& right_market : right) {
            if (!right_market.active) continue;
            CandidateMatch match;
            match.venue_a = left_market.venue;
            match.market_a = left_market.market_id.empty() ? left_market.slug : left_market.market_id;
            match.venue_b = right_market.venue;
            match.market_b = right_market.market_id.empty() ? right_market.slug : right_market.market_id;
            match.similarity = title_similarity(left_market.title, right_market.title);
            match.numeric_tokens_match = numeric_tokens_equal(left_market.title, right_market.title);
            if (left_market.close_ts_ms > 0 && right_market.close_ts_ms > 0) {
                match.close_diff_seconds =
                    std::llabs(left_market.close_ts_ms - right_market.close_ts_ms) / 1000;
            }
            const bool close_ok = match.close_diff_seconds == 0 ||
                                  match.close_diff_seconds <= policy.auto_match_max_close_diff_seconds;
            match.eligible_for_review = match.similarity >= policy.auto_match_min_similarity &&
                                        match.numeric_tokens_match && close_ok;
            if (match.similarity >= std::max(0.50, policy.auto_match_min_similarity - 0.20))
                matches.push_back(std::move(match));
        }
    }
    std::sort(matches.begin(), matches.end(), [](const CandidateMatch& a, const CandidateMatch& b) {
        if (a.eligible_for_review != b.eligible_for_review) return a.eligible_for_review > b.eligible_for_review;
        return a.similarity > b.similarity;
    });
    return matches;
}

VenueMarket parse_limitless_market_payload(const std::string& payload) {
    const auto root = json::parse(payload);
    if (!root.is_object()) throw std::runtime_error("Limitless market payload is not an object");
    return parse_limitless_market_object(root.as_object());
}

std::vector<VenueMarket> parse_limitless_active_markets_payload(
    const std::string& payload,
    std::size_t limit) {
    const auto root = json::parse(payload);
    const json::array* data = nullptr;
    if (root.is_object()) {
        if (const auto* value = find_value(root.as_object(), "data"); value && value->is_array())
            data = &value->as_array();
    } else if (root.is_array()) {
        data = &root.as_array();
    }
    if (data == nullptr) throw std::runtime_error("Limitless active-markets payload has no data array");
    std::vector<VenueMarket> markets;
    for (const auto& value : *data) {
        if (!value.is_object()) continue;
        auto market = parse_limitless_market_object(value.as_object());
        const auto trade_type = lower_ascii(object_string(value.as_object(), "tradeType"));
        if (trade_type != "clob" || market.yes_token.empty() || market.no_token.empty()) continue;
        markets.push_back(std::move(market));
        if (limit > 0 && markets.size() >= limit) break;
    }
    return markets;
}

OutcomeBooks parse_limitless_orderbook_payload(
    const std::string& payload,
    std::string yes_token,
    std::string no_token) {
    const auto root = json::parse(payload);
    if (!root.is_object()) throw std::runtime_error("Limitless orderbook payload is not an object");
    const auto& object = root.as_object();
    Book yes;
    yes.token_id = yes_token.empty() ? object_string(object, "tokenId") : std::move(yes_token);
    yes.tick_size = 0.01;
    yes.min_order_size = object_double(object, "minSize", 1.0);
    yes.last_trade = object_double(object, "lastTradePrice", 0.0);
    parse_limitless_levels(find_value(object, "bids"), yes.bids);
    parse_limitless_levels(find_value(object, "asks"), yes.asks);
    auto books = derive_complement_books(std::move(yes), std::move(no_token));
    books.received_ts_ms = now_ms();
    sort_books(books);
    return books;
}

std::vector<VenueMarket> parse_kalshi_markets_payload(
    const std::string& payload,
    std::string* next_cursor) {
    const auto root = json::parse(payload);
    if (!root.is_object()) throw std::runtime_error("Kalshi markets payload is not an object");
    const auto& object = root.as_object();
    if (next_cursor != nullptr) *next_cursor = object_string(object, "cursor");
    const auto* markets_value = find_value(object, "markets");
    if (markets_value == nullptr || !markets_value->is_array())
        throw std::runtime_error("Kalshi markets payload has no markets array");
    std::vector<VenueMarket> markets;
    for (const auto& value : markets_value->as_array()) {
        if (!value.is_object()) continue;
        const auto& source = value.as_object();
        if (lower_ascii(object_string(source, "market_type", "binary")) != "binary") continue;
        VenueMarket market;
        market.venue = Venue::Kalshi;
        market.market_id = object_string(source, "ticker");
        market.event_id = object_string(source, "event_ticker");
        market.slug = market.market_id;
        market.title = object_string(source, "title");
