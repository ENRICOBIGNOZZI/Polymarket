        if (market.title.empty()) market.title = object_string(source, "subtitle");
        market.yes_token = market.market_id + ":YES";
        market.no_token = market.market_id + ":NO";
        market.rules_primary = object_string(source, "rules_primary");
        market.rules_secondary = object_string(source, "rules_secondary");
        market.close_ts_ms = parse_iso_time_ms(object_string(source, "close_time"));
        if (market.close_ts_ms == 0)
            market.close_ts_ms = parse_iso_time_ms(object_string(source, "expected_expiration_time"));
        market.liquidity = object_double(source, "liquidity_dollars");
        market.volume24h = object_double(source, "volume_24h_fp");
        market.active = lower_ascii(object_string(source, "status", "open")) == "open";
        if (!market.market_id.empty()) markets.push_back(std::move(market));
    }
    return markets;
}

OutcomeBooks parse_kalshi_orderbook_payload(const std::string& payload, std::string ticker) {
    const auto root = json::parse(payload);
    if (!root.is_object()) throw std::runtime_error("Kalshi orderbook payload is not an object");
    const auto* book_value = find_value(root.as_object(), "orderbook_fp");
    if (book_value == nullptr || !book_value->is_object())
        throw std::runtime_error("Kalshi orderbook payload has no orderbook_fp object");
    const auto& source = book_value->as_object();
    OutcomeBooks books;
    books.yes.token_id = ticker + ":YES";
    books.no.token_id = ticker + ":NO";
    books.yes.tick_size = books.no.tick_size = 0.01;
    books.yes.min_order_size = books.no.min_order_size = 1.0;
    auto parse_side = [](const json::value* value, std::vector<Level>& levels) {
        if (value == nullptr || !value->is_array()) return;
        for (const auto& row : value->as_array()) {
            if (!row.is_array() || row.as_array().size() < 2) continue;
            const double price = json_double(row.as_array()[0], -1.0);
            const double size = json_double(row.as_array()[1], 0.0);
            if (price > 0.0 && price < 1.0 && size > 0.0) levels.push_back({price, size});
        }
    };
    parse_side(find_value(source, "yes_dollars"), books.yes.bids);
    parse_side(find_value(source, "no_dollars"), books.no.bids);
    for (const auto& no_bid : books.no.bids) books.yes.asks.push_back({1.0 - no_bid.price, no_bid.size});
    for (const auto& yes_bid : books.yes.bids) books.no.asks.push_back({1.0 - yes_bid.price, yes_bid.size});
    books.received_ts_ms = now_ms();
    sort_books(books);
    return books;
}

LimitlessClient::LimitlessClient(Credentials credentials, std::string base_url)
    : credentials_(std::move(credentials)), base_url_(std::move(base_url)) {
    while (!base_url_.empty() && base_url_.back() == '/') base_url_.pop_back();
}

HttpResponse LimitlessClient::signed_get(std::string path_with_query) const {
    const auto signed_headers = sign_limitless_request(credentials_, "GET", path_with_query);
    return http_.get(base_url_ + path_with_query, signed_headers.headers);
}

std::vector<VenueMarket> LimitlessClient::discover_markets(std::size_t limit) const {
    if (limit == 0) return {};
    std::vector<VenueMarket> markets;
    std::unordered_set<std::string> seen;
    for (std::size_t page = 1; markets.size() < limit && page <= 200; ++page) {
        const std::size_t page_limit = std::min<std::size_t>(25, limit - markets.size());
        std::ostringstream path;
        path << "/markets/active?limit=" << page_limit << "&page=" << page << "&sortBy=liquidity";
        const auto response = signed_get(path.str());
        if (response.status < 200 || response.status >= 300) {
            throw std::runtime_error("Limitless active markets HTTP " + std::to_string(response.status) +
                                     ": " + response.body.substr(0, 300));
        }
        const auto batch = parse_limitless_active_markets_payload(response.body, page_limit);
        if (batch.empty()) break;
        for (auto market : batch) {
            const std::string key = market.market_id.empty() ? market.slug : market.market_id;
            if (seen.insert(key).second) markets.push_back(std::move(market));
            if (markets.size() >= limit) break;
        }
        if (batch.size() < page_limit) break;
    }
    return markets;
}

VenueMarket LimitlessClient::fetch_market(std::string_view slug) const {
    const std::string path = "/markets/" + url_encode(slug);
    const auto response = signed_get(path);
    if (response.status < 200 || response.status >= 300) {
        throw std::runtime_error("Limitless market HTTP " + std::to_string(response.status) +
                                 ": " + response.body.substr(0, 300));
    }
    return parse_limitless_market_payload(response.body);
}

OutcomeBooks LimitlessClient::fetch_books(const VenueMarket& market) const {
    const std::string path = "/markets/" + url_encode(market.slug) + "/orderbook";
    const auto response = signed_get(path);
    if (response.status < 200 || response.status >= 300) {
        throw std::runtime_error("Limitless orderbook HTTP " + std::to_string(response.status) +
                                 ": " + response.body.substr(0, 300));
    }
    return parse_limitless_orderbook_payload(response.body, market.yes_token, market.no_token);
}

HttpResponse LimitlessClient::auth_check() const { return signed_get("/portfolio/positions"); }

KalshiClient::KalshiClient(
    Credentials credentials,
    std::string production_base_url,
    std::string demo_base_url)
    : credentials_(std::move(credentials)),
      production_base_url_(std::move(production_base_url)),
      demo_base_url_(std::move(demo_base_url)) {
    while (!production_base_url_.empty() && production_base_url_.back() == '/') production_base_url_.pop_back();
    while (!demo_base_url_.empty() && demo_base_url_.back() == '/') demo_base_url_.pop_back();
}

std::string KalshiClient::base_url() const {
    return credentials_.kalshi_environment == "demo" ? demo_base_url_ : production_base_url_;
}

HttpResponse KalshiClient::signed_get(std::string path_with_query) const {
    const auto signed_headers = sign_kalshi_request(credentials_, "GET", path_with_query);
    return http_.get(base_url() + path_with_query, signed_headers.headers);
}

std::vector<VenueMarket> KalshiClient::discover_markets(std::size_t limit) const {
    if (limit == 0) return {};
    std::vector<VenueMarket> markets;
    std::unordered_set<std::string> seen;
    std::string cursor;
    while (markets.size() < limit) {
        const std::size_t page_limit = std::min<std::size_t>(1000, limit - markets.size());
        std::ostringstream path;
        path << "/trade-api/v2/markets?status=open&limit=" << page_limit;
        if (!cursor.empty()) path << "&cursor=" << url_encode(cursor);
        const auto response = http_.get(base_url() + path.str());
        if (response.status < 200 || response.status >= 300) {
            throw std::runtime_error("Kalshi markets HTTP " + std::to_string(response.status) +
                                     ": " + response.body.substr(0, 300));
        }
        std::string next_cursor;
        auto batch = parse_kalshi_markets_payload(response.body, &next_cursor);
        for (auto market : batch) {
            if (seen.insert(market.market_id).second) markets.push_back(std::move(market));
            if (markets.size() >= limit) break;
        }
        if (next_cursor.empty() || next_cursor == cursor || batch.empty()) break;
        cursor = std::move(next_cursor);
    }
    return markets;
}

OutcomeBooks KalshiClient::fetch_books(const VenueMarket& market) const {
    const std::string path = "/trade-api/v2/markets/" + url_encode(market.market_id) + "/orderbook?depth=100";
    const auto response = signed_get(path);
    if (response.status < 200 || response.status >= 300) {
        throw std::runtime_error("Kalshi orderbook HTTP " + std::to_string(response.status) +
                                 ": " + response.body.substr(0, 300));
    }
    return parse_kalshi_orderbook_payload(response.body, market.market_id);
}

HttpResponse KalshiClient::auth_check() const { return signed_get("/trade-api/v2/portfolio/balance"); }

std::vector<Opportunity> evaluate_pair(
    const VerifiedPair& pair,
    const VenueMarket& market_a,
    const OutcomeBooks& books_a,
    const VenueMarket& market_b,
    const OutcomeBooks& books_b,
    const Policy& policy,
    double capital_limit_usd) {
    const std::array<std::pair<Outcome, Outcome>, 2> combinations =
        pair.relation == Relation::SameEvent
            ? std::array<std::pair<Outcome, Outcome>, 2>{{{Outcome::Yes, Outcome::No},
                                                          {Outcome::No, Outcome::Yes}}}
            : std::array<std::pair<Outcome, Outcome>, 2>{{{Outcome::Yes, Outcome::Yes},
                                                          {Outcome::No, Outcome::No}}};
    std::vector<Opportunity> opportunities;
    const double bundle_cap = std::max(0.0, std::min(policy.max_bundle_usd, capital_limit_usd));
    for (const auto& [outcome_a, outcome_b] : combinations) {
        Opportunity opportunity;
        opportunity.pair_id = pair.pair_id;
        opportunity.relation = pair.relation;
        opportunity.settlement_verified = pair.settlement_verified;
        opportunity.decision_ts_ms = now_ms();
        opportunity.opportunity_id = opportunity_id(pair, outcome_a, outcome_b, opportunity.decision_ts_ms);
        opportunity.leg_a.venue = market_a.venue;
        opportunity.leg_a.market_id = market_a.market_id;
        opportunity.leg_a.outcome = outcome_a;
        opportunity.leg_b.venue = market_b.venue;
        opportunity.leg_b.market_id = market_b.market_id;
        opportunity.leg_b.outcome = outcome_b;

        const Book& book_a = outcome_book(books_a, outcome_a);
        const Book& book_b = outcome_book(books_b, outcome_b);
        const double ask_a = book_a.best_ask();
        const double ask_b = book_b.best_ask();
        if (!std::isfinite(ask_a) || !std::isfinite(ask_b) || ask_a <= 0.0 || ask_b <= 0.0) {
            opportunity.reject_reason = "missing_executable_ask";
            opportunities.push_back(std::move(opportunity));
            continue;
        }
        if (bundle_cap <= 0.0) {
            opportunity.reject_reason = "capital_gate_closed";
            opportunities.push_back(std::move(opportunity));
            continue;
        }
        const double price_sum = ask_a + ask_b;
        double shares = std::min({available_ask_shares(book_a), available_ask_shares(book_b),
                                  bundle_cap / std::max(kEpsilon, price_sum)});
        if (shares < policy.min_shares) {
            opportunity.reject_reason = "insufficient_depth_or_capital";
            opportunities.push_back(std::move(opportunity));
            continue;
        }
        const auto walk_a = walk_buy(book_a, shares, policy.slippage_bps);
        const auto walk_b = walk_buy(book_b, shares, policy.slippage_bps);
        shares = std::min(walk_a.shares, walk_b.shares);
        if (!walk_a.complete || !walk_b.complete || shares < policy.min_shares) {
            opportunity.reject_reason = "partial_depth_walk";
            opportunities.push_back(std::move(opportunity));
            continue;
        }

        opportunity.shares = shares;
        opportunity.leg_a.shares = shares;
        opportunity.leg_b.shares = shares;
        opportunity.leg_a.average_price = walk_a.average_price;
        opportunity.leg_b.average_price = walk_b.average_price;
        opportunity.leg_a.raw_cost = walk_a.cost;
        opportunity.leg_b.raw_cost = walk_b.cost;
        const double fee_a_bps = policy.fee_reserve_bps.contains(market_a.venue)
                                     ? policy.fee_reserve_bps.at(market_a.venue)
                                     : 0.0;
        const double fee_b_bps = policy.fee_reserve_bps.contains(market_b.venue)
                                     ? policy.fee_reserve_bps.at(market_b.venue)
                                     : 0.0;
        opportunity.leg_a.fee_reserve = walk_a.cost * std::max(0.0, fee_a_bps) / 10000.0;
        opportunity.leg_b.fee_reserve = walk_b.cost * std::max(0.0, fee_b_bps) / 10000.0;
        opportunity.raw_cost = walk_a.cost + walk_b.cost;
        const double latency_reserve =
            opportunity.raw_cost * std::max(0.0, policy.latency_reserve_bps) / 10000.0;
        opportunity.total_cost = opportunity.raw_cost + opportunity.leg_a.fee_reserve +
                                 opportunity.leg_b.fee_reserve + latency_reserve;
        opportunity.payoff_floor = shares;
        opportunity.net_profit = opportunity.payoff_floor - opportunity.total_cost;
        opportunity.net_edge_per_share = opportunity.net_profit / shares;

        if (policy.require_verified_settlement && !pair.settlement_verified) {
            opportunity.reject_reason = "settlement_not_verified";
        } else if (market_a.close_ts_ms > 0 && market_b.close_ts_ms > 0 &&
                   std::llabs(market_a.close_ts_ms - market_b.close_ts_ms) / 1000 >
                       pair.max_close_diff_seconds) {
            opportunity.reject_reason = "close_time_mismatch";
        } else if (opportunity.net_edge_per_share + kEpsilon < policy.min_net_edge_per_share) {
            opportunity.reject_reason = "edge_below_threshold";
        } else {
            opportunity.executable = true;
        }
        opportunities.push_back(std::move(opportunity));
    }
    return opportunities;
}

SupervisorGate load_supervisor_gate(const std::filesystem::path& path) {
    SupervisorGate gate;
    std::ifstream input(path);
    if (!input) {
        gate.reason = "supervisor_limits_missing";
        return gate;
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    try {
        const auto root = json::parse(buffer.str());
        if (!root.is_object()) {
            gate.reason = "supervisor_limits_invalid";
            return gate;
        }
        const auto& object = root.as_object();
        gate.available = true;
        gate.timestamp = object_int64(object, "timestamp");
        gate.global_kill = object_bool(object, "global_kill", true);
        if (const auto* engines = find_value(object, "engines"); engines && engines->is_object()) {
            if (const auto* cross = find_value(engines->as_object(), "cross_venue");
                cross && cross->is_object()) {
                gate.new_exposure_allowed =
                    object_bool(cross->as_object(), "new_exposure_allowed", false);
                gate.capital_limit_usd = object_double(cross->as_object(), "capital_limit_usd", 0.0);
                gate.max_bundle_usd = object_double(cross->as_object(), "max_bundle_usd", 0.0);
                gate.reason = object_string(cross->as_object(), "reason");
            } else {
                gate.reason = "cross_venue_gate_missing";
            }
        } else {
            gate.reason = "engine_gates_missing";
        }
        if (gate.timestamp <= 0 || now_seconds() - gate.timestamp > 120) {
            gate.new_exposure_allowed = false;
            gate.reason = "supervisor_gate_stale";
        }
