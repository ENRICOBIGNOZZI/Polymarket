        if (gate.global_kill) gate.new_exposure_allowed = false;
    } catch (const std::exception& error) {
        gate.available = false;
        gate.reason = std::string("supervisor_parse_error:") + error.what();
    }
    return gate;
}

CrossVenueEngine::CrossVenueEngine(EngineConfig config)
    : config_(std::move(config)), credentials_(load_credentials(config_.credentials_file)) {
    config_.credentials_file = expand_user_path(config_.credentials_file);
    std::filesystem::create_directories(config_.run_dir);
}

int CrossVenueEngine::run_once() {
    std::filesystem::create_directories(config_.run_dir);
    const auto pairs = load_pair_manifest(config_.pair_manifest);
    SupervisorGate gate;
    if (config_.policy.require_supervisor_gate) {
        gate = load_supervisor_gate(config_.policy.supervisor_limits);
    } else {
        gate.available = true;
        gate.global_kill = false;
        gate.new_exposure_allowed = true;
        gate.capital_limit_usd = config_.policy.max_open_capital_usd;
        gate.max_bundle_usd = config_.policy.max_bundle_usd;
        gate.timestamp = now_seconds();
        gate.reason = "supervisor_gate_not_required";
    }

    auto state = load_paper_state(config_.run_dir / "paper_state.json", config_.starting_capital);
    std::map<Venue, std::vector<VenueMarket>> markets;
    std::vector<std::string> errors;

    const Config polymarket_config = Engine::load_config(config_.polymarket_config.string());
    PolymarketApi polymarket_api(polymarket_config);
    try {
        const auto source_markets = polymarket_api.discover_markets(
            config_.policy.polymarket_market_limit, config_.policy.polymarket_min_liquidity);
        auto& target = markets[Venue::Polymarket];
        target.reserve(source_markets.size());
        for (const auto& market : source_markets) target.push_back(from_polymarket(market));
    } catch (const std::exception& error) {
        errors.push_back(std::string("polymarket_discovery:") + error.what());
    }

    std::optional<LimitlessClient> limitless_client;
    if (credentials_.has_limitless_auth()) {
        try {
            limitless_client.emplace(credentials_, config_.limitless_base_url);
            markets[Venue::Limitless] =
                limitless_client->discover_markets(config_.policy.limitless_market_limit);
        } catch (const std::exception& error) {
            errors.push_back(std::string("limitless_discovery:") + error.what());
        }
    } else {
        errors.push_back("limitless_credentials_missing");
    }

    std::optional<KalshiClient> kalshi_client;
    try {
        kalshi_client.emplace(credentials_, config_.kalshi_production_base_url, config_.kalshi_demo_base_url);
        markets[Venue::Kalshi] = kalshi_client->discover_markets(config_.policy.kalshi_market_limit);
        if (!credentials_.has_kalshi_auth()) errors.push_back("kalshi_credentials_missing_for_orderbooks");
    } catch (const std::exception& error) {
        errors.push_back(std::string("kalshi_discovery:") + error.what());
    }

    std::vector<CandidateMatch> candidates;
    const std::array<std::pair<Venue, Venue>, 3> venue_pairs{{
        {Venue::Polymarket, Venue::Limitless},
        {Venue::Polymarket, Venue::Kalshi},
        {Venue::Limitless, Venue::Kalshi},
    }};
    for (const auto& [left, right] : venue_pairs) {
        const auto left_it = markets.find(left);
        const auto right_it = markets.find(right);
        if (left_it == markets.end() || right_it == markets.end()) continue;
        auto batch = build_candidate_matches(left_it->second, right_it->second, config_.policy);
        candidates.insert(candidates.end(), std::make_move_iterator(batch.begin()),
                          std::make_move_iterator(batch.end()));
    }
    std::sort(candidates.begin(), candidates.end(), [](const CandidateMatch& left, const CandidateMatch& right) {
        if (left.eligible_for_review != right.eligible_for_review)
            return left.eligible_for_review > right.eligible_for_review;
        return left.similarity > right.similarity;
    });
    write_candidates(config_.run_dir / "candidate_matches.csv", candidates);
    write_market_catalog(config_.run_dir / "market_catalog.json", markets);

    std::unordered_map<std::string, OutcomeBooks> book_cache;
    std::vector<std::string> polymarket_tokens;
    for (const auto& pair : pairs) {
        for (const auto& [venue, identifier] :
             std::array<std::pair<Venue, std::string>, 2>{{{pair.venue_a, pair.market_a},
                                                           {pair.venue_b, pair.market_b}}}) {
            if (venue != Venue::Polymarket) continue;
            const auto* market = find_market(markets, venue, identifier);
            if (market == nullptr) continue;
            polymarket_tokens.push_back(market->yes_token);
            polymarket_tokens.push_back(market->no_token);
        }
    }
    std::sort(polymarket_tokens.begin(), polymarket_tokens.end());
    polymarket_tokens.erase(std::unique(polymarket_tokens.begin(), polymarket_tokens.end()),
                            polymarket_tokens.end());
    std::unordered_map<std::string, Book> polymarket_books;
    if (!polymarket_tokens.empty()) {
        try {
            polymarket_books = polymarket_api.fetch_books(polymarket_tokens);
        } catch (const std::exception& error) {
            errors.push_back(std::string("polymarket_books:") + error.what());
        }
    }

    auto get_books = [&](const VenueMarket& market) -> const OutcomeBooks* {
        const std::string key = venue_key(market.venue, market.market_id);
        if (const auto it = book_cache.find(key); it != book_cache.end()) return &it->second;
        try {
            OutcomeBooks books;
            if (market.venue == Venue::Polymarket) {
                const auto yes = polymarket_books.find(market.yes_token);
                const auto no = polymarket_books.find(market.no_token);
                if (yes == polymarket_books.end() || no == polymarket_books.end())
                    throw std::runtime_error("Polymarket book missing from batch response");
                books.yes = yes->second;
                books.no = no->second;
                books.received_ts_ms = now_ms();
            } else if (market.venue == Venue::Limitless) {
                if (!limitless_client) throw std::runtime_error("Limitless client unavailable");
                books = limitless_client->fetch_books(market);
            } else {
                if (!kalshi_client || !credentials_.has_kalshi_auth())
                    throw std::runtime_error("Kalshi authenticated client unavailable");
                books = kalshi_client->fetch_books(market);
            }
            auto [it, inserted] = book_cache.emplace(key, std::move(books));
            (void)inserted;
            return &it->second;
        } catch (const std::exception& error) {
            errors.push_back(key + ":" + error.what());
            return nullptr;
        }
    };

    Policy cycle_policy = config_.policy;
    if (gate.max_bundle_usd > 0.0)
        cycle_policy.max_bundle_usd = std::min(cycle_policy.max_bundle_usd, gate.max_bundle_usd);
    const double configured_capital =
        gate.capital_limit_usd > 0.0
            ? std::min(config_.policy.max_open_capital_usd, gate.capital_limit_usd)
            : config_.policy.max_open_capital_usd;
    const double remaining_capital = std::max(0.0, configured_capital - state.reserved_capital);
    const bool gate_open = gate.available && !gate.global_kill && gate.new_exposure_allowed &&
                           remaining_capital >= cycle_policy.min_shares * 0.01;

    std::vector<Opportunity> opportunities;
    for (const auto& pair : pairs) {
        const auto* market_a = find_market(markets, pair.venue_a, pair.market_a);
        const auto* market_b = find_market(markets, pair.venue_b, pair.market_b);
        if (market_a == nullptr || market_b == nullptr) {
            Opportunity rejected;
            rejected.pair_id = pair.pair_id;
            rejected.relation = pair.relation;
            rejected.settlement_verified = pair.settlement_verified;
            rejected.reject_reason = market_a == nullptr ? "market_a_not_discovered" : "market_b_not_discovered";
            rejected.decision_ts_ms = now_ms();
            rejected.opportunity_id = pair.pair_id + ":unavailable:" + std::to_string(rejected.decision_ts_ms);
            rejected.leg_a.venue = pair.venue_a;
            rejected.leg_a.market_id = pair.market_a;
            rejected.leg_b.venue = pair.venue_b;
            rejected.leg_b.market_id = pair.market_b;
            opportunities.push_back(std::move(rejected));
            continue;
        }
        const auto* books_a = get_books(*market_a);
        const auto* books_b = get_books(*market_b);
        if (books_a == nullptr || books_b == nullptr) {
            Opportunity rejected;
            rejected.pair_id = pair.pair_id;
            rejected.relation = pair.relation;
            rejected.settlement_verified = pair.settlement_verified;
            rejected.reject_reason = "book_unavailable";
            rejected.decision_ts_ms = now_ms();
            rejected.opportunity_id = pair.pair_id + ":book_unavailable:" +
                                      std::to_string(rejected.decision_ts_ms);
            rejected.leg_a.venue = pair.venue_a;
            rejected.leg_a.market_id = market_a->market_id;
            rejected.leg_b.venue = pair.venue_b;
            rejected.leg_b.market_id = market_b->market_id;
            opportunities.push_back(std::move(rejected));
            continue;
        }
        auto evaluated = evaluate_pair(pair, *market_a, *books_a, *market_b, *books_b,
                                       cycle_policy, std::max(remaining_capital, cycle_policy.max_bundle_usd));
        if (!gate_open) {
            for (auto& opportunity : evaluated) {
                if (opportunity.executable) {
                    opportunity.executable = false;
                    opportunity.reject_reason = gate.reason.empty() ? "supervisor_gate_closed" : gate.reason;
                }
            }
        }
        opportunities.insert(opportunities.end(), std::make_move_iterator(evaluated.begin()),
                             std::make_move_iterator(evaluated.end()));
    }

    std::sort(opportunities.begin(), opportunities.end(), [](const Opportunity& left, const Opportunity& right) {
        if (left.executable != right.executable) return left.executable > right.executable;
        return left.net_edge_per_share > right.net_edge_per_share;
    });
    write_opportunities(config_.run_dir / "opportunities.csv", opportunities);

    std::size_t paper_entries = 0;
    if (config_.policy.paper_trade_enabled && gate_open && !state.killed) {
        for (const auto& opportunity : opportunities) {
            if (!opportunity.executable) continue;
            const auto last = state.last_pair_trade.find(opportunity.pair_id);
            if (last != state.last_pair_trade.end() &&
                now_seconds() - last->second < config_.policy.pair_cooldown_seconds)
                continue;
            if (state.reserved_capital + opportunity.total_cost > configured_capital + kEpsilon) continue;
            if (state.cash + kEpsilon < opportunity.total_cost) continue;
            append_bundle(config_.run_dir / "paper_bundles.csv", opportunity);
            state.cash -= opportunity.total_cost;
            state.reserved_capital += opportunity.total_cost;
            state.expected_locked_profit += opportunity.net_profit;
            state.last_pair_trade[opportunity.pair_id] = now_seconds();
            ++paper_entries;
        }
    }
    state.peak_equity = std::max(state.peak_equity, state.cash + state.reserved_capital);
    if (gate.available && gate.global_kill) state.killed = true;
    persist_paper_state(config_.run_dir / "paper_state.json", state);

    std::ostringstream error_text;
    for (std::size_t i = 0; i < errors.size(); ++i) {
        if (i > 0) error_text << " | ";
        error_text << errors[i];
    }
    const bool healthy = errors.empty();
    write_runtime_status(config_, gate, markets, pairs.size(), candidates.size(), opportunities,
                         paper_entries, state, credentials_, healthy, error_text.str());
    return 0;
}

int CrossVenueEngine::run_loop() {
    while (true) {
        try {
            run_once();
        } catch (const std::exception& error) {
            std::cerr << "cross_venue_cycle_error=" << error.what() << '\n';
            std::map<Venue, std::vector<VenueMarket>> empty_markets;
            SupervisorGate gate;
            PaperState state = load_paper_state(config_.run_dir / "paper_state.json", config_.starting_capital);
            write_runtime_status(config_, gate, empty_markets, 0, 0, {}, 0, state, credentials_, false,
                                 error.what());
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(config_.interval_ms));
    }
}

} // namespace pm::cross
