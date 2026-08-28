private:
    pm::Config cfg_;
    pm::PolymarketApi api_;
    pm::HttpClient http_;
    pm::fast::Policy policy_;
    Options options_;
    fs::path run_dir_;
    std::unordered_map<std::string, pm::Market> markets_;
    std::unordered_map<std::string, pm::Book> books_;
    std::unordered_map<std::string, pm::FeeDetails> fees_;
    std::unordered_map<std::string, std::string> token_to_market_;
    std::unordered_map<std::string, std::vector<std::string>> event_members_;
    std::unordered_set<std::string> safe_negrisk_events_;
    std::vector<std::string> tokens_;
    std::vector<RelationSpec> relations_;
    std::vector<RelationSpec> active_relations_;
    std::unordered_map<std::string, std::vector<std::size_t>> relations_by_market_;
    std::unordered_map<std::string, pm::ExternalSignal> external_;
    std::unordered_map<std::string, pm::fast::Opportunity> latest_;
    std::unordered_map<std::string, std::string> signatures_;
    std::unordered_map<std::string, std::int64_t> last_log_ms_;
    std::unordered_map<std::string, std::int64_t> book_exchange_ts_ms_;
    std::unordered_map<std::string, std::int64_t> book_received_ts_ms_;
    std::unordered_set<std::string> ws_snapshot_ready_;
    std::unordered_set<std::string> rest_snapshot_ready_;
    std::vector<pm::fast::Opportunity> pending_candidates_;
    std::deque<double> feed_latency_ms_;
    std::deque<double> decision_latency_us_;
    std::ofstream opportunity_log_;
    std::ofstream latency_log_;
    std::ofstream error_log_;
    mutable std::mutex mutex_;
    std::int64_t initialized_ms_ = 0;
    std::int64_t last_message_ms_ = 0;
    std::uint64_t book_updates_ = 0;
    std::uint64_t rest_resyncs_ = 0;
    std::uint64_t evaluations_ = 0;
    std::uint64_t executable_observations_ = 0;
    std::uint64_t hard_executable_observations_ = 0;
    std::uint64_t freshness_missing_rejections_ = 0;
    std::uint64_t freshness_age_rejections_ = 0;
    std::uint64_t freshness_skew_rejections_ = 0;
    std::uint64_t candidate_sequence_ = 0;
    std::uint64_t candidates_spooled_ = 0;
    std::uint64_t candidates_dropped_ = 0;

    // A REST /books image is only a point-in-time snapshot, so its exchange age and
    // cross-leg skew are bounded strictly. A full WS book followed by uninterrupted,
    // monotone price_change lineage is different: the state remains current when a
    // quiet token has no updates. Its last event timestamp is therefore an audit clock,
    // not the age of the current state. Reconnect/error explicitly invalidates that
    // continuity before any later delta can be trusted.
    static constexpr std::int64_t kMaxBookAgeMs = 5000;
    static constexpr std::int64_t kMaxBookSkewMs = 1500;

    struct FreshnessWindow {
        std::int64_t exchange_ms = 0;
        std::int64_t received_ms = 0;
    };

    struct RestSnapshot {
        pm::Book book;
        std::int64_t exchange_ms = 0;
        std::int64_t received_ms = 0;
        std::string hash;
    };

    static std::string opportunity_header() {
        return "observed_ts_ms,kind,id,event_id,hard_arbitrage,executable,risk_class,"
               "reject_reason,payoff_floor,raw_edge_per_share,net_edge_per_share,"
               "executable_shares,capital_required,expected_profit,exchange_ts_ms,"
               "received_ts_ms,decision_ts_ms,feed_latency_ms,legs";
    }

    static std::string leg_string(const pm::fast::Opportunity& opportunity) {
        std::ostringstream out;
        for (std::size_t i = 0; i < opportunity.legs.size(); ++i) {
            if (i) out << '|';
            const auto& leg = opportunity.legs[i];
            out << leg.market_id << ':' << leg.token_id << ':' << leg.outcome << ':'
                << leg.shares << ':' << leg.average_price << ':' << leg.fee_per_share << ':'
                << leg.all_in_cost;
        }
        return out.str();
    }

    static void write_opportunity(std::ostream& out, std::int64_t observed,
                                  const pm::fast::Opportunity& opportunity) {
        const auto feed_latency = opportunity.exchange_ts_ms > 0 &&
                                          opportunity.received_ts_ms > 0 ?
                                      std::max<std::int64_t>(
                                          0, opportunity.received_ts_ms -
                                                 opportunity.exchange_ts_ms) :
                                      0;
        out << observed << ',' << pm::fast::to_string(opportunity.kind) << ','
            << csv_escape(opportunity.id) << ',' << csv_escape(opportunity.event_id) << ','
            << (opportunity.hard_arbitrage ? 1 : 0) << ','
            << (opportunity.executable ? 1 : 0) << ','
            << csv_escape(opportunity.risk_class) << ','
            << csv_escape(opportunity.reject_reason) << ',' << opportunity.payoff_floor << ','
            << opportunity.raw_edge_per_share << ',' << opportunity.net_edge_per_share << ','
            << opportunity.executable_shares << ',' << opportunity.capital_required << ','
            << opportunity.expected_profit << ',' << opportunity.exchange_ts_ms << ','
            << opportunity.received_ts_ms << ',' << opportunity.decision_ts_ms << ','
            << feed_latency << ',' << csv_escape(leg_string(opportunity)) << '\n';
    }

    void spool_candidate(const pm::fast::Opportunity& opportunity) {
        if (!opportunity.executable || opportunity.id.empty()
            || opportunity.executable_shares <= 0.0 || !exact_sha(options_.model_sha)
            || opportunity.exchange_ts_ms <= 0 || opportunity.received_ts_ms <= 0
            || opportunity.decision_ts_ms < opportunity.received_ts_ms) return;
        const auto recorded = std::max(now_ms(), opportunity.decision_ts_ms);
        const auto sequence = ++candidate_sequence_;
        const auto identity = std::to_string(std::hash<std::string>{}(opportunity.id));
        const std::string record_id = "cpp-fast-" + std::to_string(recorded) + "-" +
                                      std::to_string(sequence);
        json::object metadata{
            {"opportunity_kind", pm::fast::to_string(opportunity.kind)},
            {"hard_arbitrage", opportunity.hard_arbitrage},
            {"risk_class", opportunity.risk_class},
            {"capital_required", opportunity.capital_required},
            {"payoff_floor", opportunity.payoff_floor},
            {"raw_edge_per_share", opportunity.raw_edge_per_share},
            {"legs", leg_string(opportunity)},
            {"paper_candidate_only", true},
        };
        json::object event{
            {"schema_version", 1},
            {"event_type", "CANDIDATE"},
            {"strategy", "FAST_STRUCTURAL"},
            {"model_sha", options_.model_sha},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"record_id", record_id},
            {"recorded_ts_ms", recorded},
            {"opportunity_id", opportunity.id},
            {"candidate_id", "fast-candidate-" + identity + "-" +
                             std::to_string(opportunity.decision_ts_ms)},
            {"bundle_id", "fast-bundle-" + identity},
            {"exchange_ts_ms", opportunity.exchange_ts_ms},
            {"receive_ts_ms", opportunity.received_ts_ms},
            {"decision_ts_ms", opportunity.decision_ts_ms},
            {"book_snapshot_id", "fast-book-" + identity + "-" +
                                 std::to_string(opportunity.exchange_ts_ms)},
            {"predicted_alpha", opportunity.net_edge_per_share},
            {"expected_ev", opportunity.expected_profit},
            {"intended_action", "ARB"},
            {"intended_size", opportunity.executable_shares},
            {"capital_cost", 0.0},
            {"latency_cost", 0.0},
            {"metadata", std::move(metadata)},
        };
        if (!opportunity.event_id.empty()) event["event_id"] = opportunity.event_id;
        const fs::path target = fs::path(options_.run_root) / "ledger" / "spool" /
            (std::to_string(recorded) + "." + record_id + ".json");
        atomic_write(target, json::serialize(event) + "\n");
        ++candidates_spooled_;
    }

    void ensure_logs() {
        const auto opportunity_path = run_dir_ / "fast_arb_opportunities.csv";
        if (!fs::exists(opportunity_path) || fs::file_size(opportunity_path) == 0) {
            std::ofstream out(opportunity_path);
            out << opportunity_header() << '\n';
        }
        const auto latency_path = run_dir_ / "fast_arb_latency.csv";
        if (!fs::exists(latency_path) || fs::file_size(latency_path) == 0) {
            std::ofstream out(latency_path);
            out << "received_ts_ms,shard,exchange_ts_ms,feed_latency_ms,decision_latency_us,"
                   "affected_markets\n";
        }
        const auto error_path = run_dir_ / "fast_arb_errors.csv";
        if (!fs::exists(error_path) || fs::file_size(error_path) == 0) {
            std::ofstream out(error_path);
            out << "timestamp_ms,component,message\n";
        }
        opportunity_log_.open(opportunity_path, std::ios::app);
        latency_log_.open(latency_path, std::ios::app);
        error_log_.open(error_path, std::ios::app);
        if (!opportunity_log_ || !latency_log_ || !error_log_) {
            throw std::runtime_error("cannot open fast-arb runtime logs");
        }
    }

    void append_error(const std::string& component, const std::string& message) {
        std::scoped_lock lock(mutex_);
        error_log_ << now_ms() << ',' << csv_escape(component) << ','
                   << csv_escape(message) << '\n';
        error_log_.flush();
    }

    std::optional<std::size_t> safe_event_market_count(const std::string& event_id) {
        try {
            const auto response = http_.get(cfg_.gamma_url + "/events/" + event_id);
            if (response.status < 200 || response.status >= 300) return std::nullopt;
            const auto root = json::parse(response.body);
            if (!root.is_object()) return std::nullopt;
            const auto& object = root.as_object();
            if (const auto it = object.find("negRisk");
                it != object.end() && !boolean(it->value(), false)) {
                return std::nullopt;
            }
            if (const auto it = object.find("negRiskAugmented");
                it != object.end() && boolean(it->value(), false)) {
                return std::nullopt;
            }
            if (const auto it = object.find("neg_risk_augmented");
                it != object.end() && boolean(it->value(), false)) {
                return std::nullopt;
            }
            const auto markets = object.find("markets");
            if (markets == object.end() || !markets->value().is_array() ||
                markets->value().as_array().size() < 2) {
                return std::nullopt;
            }
            for (const auto& value : markets->value().as_array()) {
                if (!value.is_object()) return std::nullopt;
                const auto& market = value.as_object();
                const auto active = market.find("active");
                const auto closed = market.find("closed");
                const auto book = market.find("enableOrderBook");
                const auto accepting = market.find("acceptingOrders");
                if ((active != market.end() && !boolean(active->value(), true)) ||
                    (closed != market.end() && boolean(closed->value(), false)) ||
                    (book != market.end() && !boolean(book->value(), true)) ||
                    (accepting != market.end() && !boolean(accepting->value(), true))) {
                    return std::nullopt;
                }
            }
            return markets->value().as_array().size();
        } catch (...) {
            return std::nullopt;
        }
    }

    static std::vector<pm::Level> parse_levels(const json::object& object,
                                                const char* key) {
        std::vector<pm::Level> levels;
        const auto it = object.find(key);
        if (it == object.end() || !it->value().is_array()) return levels;
        for (const auto& value : it->value().as_array()) {
            if (!value.is_object()) continue;
            const auto& level = value.as_object();
            const double price = get_number(level, "price", -1.0);
            const double size = get_number(level, "size", 0.0);
            if (price > 0.0 && price < 1.0 && size > 0.0) {
                levels.push_back({price, size});
            }
        }
        return levels;
    }

    std::unordered_map<std::string, RestSnapshot> fetch_timestamped_rest_books() {
        std::unordered_map<std::string, RestSnapshot> out;
        const std::size_t batch = std::max<std::size_t>(1, cfg_.books_batch_size);
        for (std::size_t pos = 0; pos < tokens_.size(); pos += batch) {
            json::array body;
            const auto end = std::min(tokens_.size(), pos + batch);
            for (std::size_t i = pos; i < end; ++i) {
                body.emplace_back(json::object{{"token_id", tokens_[i]}});
            }
            const auto response =
                http_.post_json(cfg_.clob_url + "/books", json::serialize(body));
            const auto received_ms = now_ms();
            if (response.status < 200 || response.status >= 300) {
                throw std::runtime_error("CLOB books HTTP " +
                                         std::to_string(response.status) + ": " +
                                         response.body.substr(0, 300));
            }
            const auto root = json::parse(response.body);
            if (!root.is_array()) {
                throw std::runtime_error("Unexpected CLOB books response");
            }
            for (const auto& value : root.as_array()) {
                if (!value.is_object()) continue;
                const auto& object = value.as_object();
                RestSnapshot snapshot;
                snapshot.book.token_id = get_text(object, "asset_id");
                snapshot.book.tick_size =
                    get_number(object, "tick_size", snapshot.book.tick_size);
                snapshot.book.min_order_size =
                    get_number(object, "min_order_size", snapshot.book.min_order_size);
                snapshot.book.neg_risk = boolean(
                    object.find("neg_risk") == object.end() ? json::value(false) :
                                                               object.at("neg_risk"),
                    false);
                snapshot.book.last_trade =
                    get_number(object, "last_trade_price", snapshot.book.last_trade);
                snapshot.book.bids = parse_levels(object, "bids");
                snapshot.book.asks = parse_levels(object, "asks");
                snapshot.exchange_ms = normalize_timestamp_ms(
                    get_integer(object, "timestamp", 0));
                snapshot.received_ms = received_ms;
                snapshot.hash = get_text(object, "hash");
                if (!snapshot.book.token_id.empty()) {
                    out[snapshot.book.token_id] = std::move(snapshot);
                }
            }
        }
        return out;
    }

    void mark_l2_clock_locked(const std::string& token, std::int64_t exchange_ms,
                              std::int64_t received_ms) {
        book_received_ts_ms_[token] = received_ms;
        if (exchange_ms > 0) {
            book_exchange_ts_ms_[token] = exchange_ms;
        } else {
            book_exchange_ts_ms_.erase(token);
        }
    }

    std::optional<FreshnessWindow> freshness_window_locked(
        const std::vector<std::string>& required_tokens, std::int64_t decision_ms,
        bool count_rejection = true) {
        std::unordered_set<std::string> unique;
        bool first = true;
        bool first_rest = true;
        std::int64_t min_exchange = 0;
        std::int64_t max_received = 0;
        std::int64_t min_rest_exchange = 0;
        std::int64_t max_rest_exchange = 0;
        std::int64_t min_rest_received = 0;
        std::int64_t max_rest_received = 0;

        auto reject_missing = [&]() -> std::optional<FreshnessWindow> {
            if (count_rejection) ++freshness_missing_rejections_;
            return std::nullopt;
        };
        auto reject_age = [&]() -> std::optional<FreshnessWindow> {
            if (count_rejection) ++freshness_age_rejections_;
            return std::nullopt;
        };
        auto reject_skew = [&]() -> std::optional<FreshnessWindow> {
            if (count_rejection) ++freshness_skew_rejections_;
            return std::nullopt;
        };

        for (const auto& token : required_tokens) {
            if (token.empty() || !unique.insert(token).second) continue;
            const bool continuous_ws = ws_snapshot_ready_.count(token) > 0;
            const bool point_in_time_rest = rest_snapshot_ready_.count(token) > 0;
            if (!continuous_ws && !point_in_time_rest) return reject_missing();

            const auto exchange = book_exchange_ts_ms_.find(token);
            const auto received = book_received_ts_ms_.find(token);
            if (exchange == book_exchange_ts_ms_.end() ||
                received == book_received_ts_ms_.end() || exchange->second <= 0 ||
                received->second <= 0) {
                return reject_missing();
            }

            // Both provenance modes must be causal: no leg may have been received
            // materially after the decision, nor may its exchange clock lead the
            // local receive clock beyond the configured clock-skew tolerance.
            if (decision_ms + kMaxBookSkewMs < received->second ||
                exchange->second > received->second + kMaxBookSkewMs) {
                return reject_skew();
            }

            if (!continuous_ws) {
                // REST is a point-in-time image. It must itself be recent and REST
                // legs in the same basket must describe nearly simultaneous images.
                const auto age = decision_ms - exchange->second;
                if (age > kMaxBookAgeMs || age < -kMaxBookSkewMs) return reject_age();
                if (first_rest) {
                    min_rest_exchange = max_rest_exchange = exchange->second;
                    min_rest_received = max_rest_received = received->second;
                    first_rest = false;
                } else {
                    min_rest_exchange = std::min(min_rest_exchange, exchange->second);
                    max_rest_exchange = std::max(max_rest_exchange, exchange->second);
                    min_rest_received = std::min(min_rest_received, received->second);
                    max_rest_received = std::max(max_rest_received, received->second);
                }
            }

            // For uninterrupted WS lineage, last-update age/skew are diagnostics,
            // not freshness gates. If no order-book event occurred, the current book
            // state is unchanged. A transport failure/reconnect clears this lineage.
            if (first) {
                min_exchange = exchange->second;
                max_received = received->second;
                first = false;
            } else {
                min_exchange = std::min(min_exchange, exchange->second);
                max_received = std::max(max_received, received->second);
            }
        }
        if (first) return reject_missing();
        if (!first_rest &&
            (max_rest_exchange - min_rest_exchange > kMaxBookSkewMs ||
             max_rest_received - min_rest_received > kMaxBookSkewMs)) {
            return reject_skew();
        }
        // Conservative audit clocks: oldest last exchange update and latest local
        // receipt. These clocks are not reinterpreted as WS state age downstream.
        return FreshnessWindow{min_exchange, max_received};
    }

    std::vector<std::string> opportunity_required_tokens_locked(
        const pm::fast::Opportunity& opportunity) const {
        std::vector<std::string> required;
        required.reserve(opportunity.legs.size());
        for (const auto& leg : opportunity.legs) {
            // Revalidation must match the books that were actually priced/executed.
            // Requiring the complementary outcome of every market can falsely reject
            // a valid YES-only NegRisk basket (or a two-leg logical relation) even
            // though that complementary token never enters the executable basket.
            if (!leg.token_id.empty()) required.push_back(leg.token_id);
        }
        return required;
    }

    void apply_event_locked(const json::object& event,
                            std::unordered_set<std::string>& affected,
                            std::int64_t& exchange_ms,
                            std::int64_t received_ms) {
        std::string type = get_text(event, "event_type");
        if (type.empty()) type = get_text(event, "type");
        const auto timestamp = normalize_timestamp_ms(get_integer(event, "timestamp", 0));
        exchange_ms = std::max(exchange_ms, timestamp);

        if (type == "book") {
            const auto token = get_text(event, "asset_id");
            if (token.empty() || !token_to_market_.count(token)) return;
            if (const auto previous = book_exchange_ts_ms_.find(token);
                timestamp > 0 && previous != book_exchange_ts_ms_.end() &&
                previous->second > timestamp) {
                return;
            }
            pm::Book book;
            if (const auto it = books_.find(token); it != books_.end()) book = it->second;
            book.token_id = token;
            book.bids = parse_levels(event, "bids");
            book.asks = parse_levels(event, "asks");
            book.tick_size = get_number(event, "tick_size", book.tick_size);
            book.min_order_size = get_number(event, "min_order_size", book.min_order_size);
            book.last_trade = get_number(event, "last_trade_price", book.last_trade);
            books_[token] = std::move(book);
            rest_snapshot_ready_.erase(token);
            ws_snapshot_ready_.insert(token);
            mark_l2_clock_locked(token, timestamp, received_ms);
            affected.insert(token_to_market_.at(token));
            return;
        }

        if (type == "price_change") {
            const auto it = event.find("price_changes");
            if (it == event.end() || !it->value().is_array()) return;
            for (const auto& value : it->value().as_array()) {
                if (!value.is_object()) continue;
                const auto& change = value.as_object();
                const auto token = get_text(change, "asset_id");
                if (token.empty() || !token_to_market_.count(token)) continue;
                // A delta is trustworthy only after this subscription has seen a
                // complete WS book. A fresh REST image is valid point-in-time
                // evidence, but it is never upgraded into gap-free WS lineage by a delta.
                if (!ws_snapshot_ready_.count(token) || timestamp <= 0) continue;
                if (const auto previous = book_exchange_ts_ms_.find(token);
                    previous != book_exchange_ts_ms_.end() &&
                    previous->second > timestamp) {
                    continue;
                }
                auto& book = books_[token];
                book.token_id = token;
                const auto side = get_text(change, "side");
                pm::fast::apply_level(book, side == "BUY", get_number(change, "price", -1.0),
                                      get_number(change, "size", 0.0));
                mark_l2_clock_locked(token, timestamp, received_ms);
                affected.insert(token_to_market_.at(token));
            }
            return;
        }

        if (type == "tick_size_change") {
            const auto token = get_text(event, "asset_id");
            if (token.empty() || !token_to_market_.count(token)) return;
            books_[token].tick_size = get_number(event, "new_tick_size",
                                                   books_[token].tick_size);
            affected.insert(token_to_market_.at(token));
            return;
        }

        if (type == "last_trade_price") {
            const auto token = get_text(event, "asset_id");
            if (token.empty() || !token_to_market_.count(token)) return;
            books_[token].last_trade = get_number(event, "price", books_[token].last_trade);
            affected.insert(token_to_market_.at(token));
        }
    }

    void evaluate_single_locked(const pm::Market& market) {
        const auto yes = books_.find(market.yes_token);
        const auto no = books_.find(market.no_token);
        if (yes == books_.end() || no == books_.end()) return;
        const auto fee_it = fees_.find(market.condition_id);
        const auto decision = now_ms();
        const auto clocks = freshness_window_locked(
            {market.yes_token, market.no_token}, decision);
        if (!clocks.has_value()) return;
        if (fee_it != fees_.end()) {
            record_locked(pm::fast::evaluate_binary(
                market, yes->second, no->second, fee_it->second, policy_, clocks->exchange_ms,
                clocks->received_ms, decision));
        }
        record_locked(pm::fast::evaluate_maker_complete_set(
            market, yes->second, no->second, policy_, clocks->exchange_ms,
            clocks->received_ms, decision));
        if (const auto external_it = external_.find(market.id);
            external_it != external_.end() && fee_it != fees_.end()) {
            record_locked(pm::fast::evaluate_external_latency(
                market, yes->second, no->second, fee_it->second, external_it->second, decision,
                policy_, clocks->exchange_ms, clocks->received_ms, decision));
        }
    }

    void evaluate_event_locked(const std::string& event_id) {
        if (!safe_negrisk_events_.count(event_id)) return;
        std::vector<const pm::Market*> members;
        const auto group = event_members_.find(event_id);
        if (group == event_members_.end()) return;
        for (const auto& id : group->second) {
            const auto member = markets_.find(id);
            if (member != markets_.end()) {
                if (!fees_.count(member->second.condition_id)) return;
                members.push_back(&member->second);
            }
        }
        if (members.size() < 2) return;

        // A NegRisk complete-set buys only YES tokens. Freshness is therefore
        // required on those exact executable legs, not on every complementary NO.
        std::vector<std::string> complete_set_tokens;
        complete_set_tokens.reserve(members.size());
        for (const auto* member : members) complete_set_tokens.push_back(member->yes_token);
        {
            const auto decision = now_ms();
            if (const auto clocks = freshness_window_locked(complete_set_tokens, decision);
                clocks.has_value()) {
                record_locked(pm::fast::evaluate_negrisk_complete_set(
                    event_id, members, books_, fees_, policy_, clocks->exchange_ms,
                    clocks->received_ms, decision));
            }
        }

        // Each conversion has a different executable basket: source NO plus the
        // YES token of every other event member. Gate each source independently.
        for (const auto* source : members) {
            std::vector<std::string> conversion_tokens;
            conversion_tokens.reserve(members.size());
            conversion_tokens.push_back(source->no_token);
            for (const auto* target : members) {
                if (target->id != source->id) conversion_tokens.push_back(target->yes_token);
            }
            const auto decision = now_ms();
            const auto clocks = freshness_window_locked(conversion_tokens, decision);
            if (!clocks.has_value()) continue;
            record_locked(pm::fast::evaluate_negrisk_conversion(
                event_id, *source, members, books_, fees_, policy_, clocks->exchange_ms,
                clocks->received_ms, decision));
        }
    }

    void evaluate_relation_locked(std::size_t index) {
        const auto& spec = active_relations_.at(index);
        const auto& relation = spec.relation;
        const auto left = markets_.find(relation.antecedent_market_id);
        const auto right = markets_.find(relation.consequent_market_id);
        if (left == markets_.end() || right == markets_.end()) return;
        if (!fees_.count(left->second.condition_id) || !fees_.count(right->second.condition_id)) return;
        const auto decision = now_ms();

        if (spec.type == RelationType::Implication) {
            const auto clocks = freshness_window_locked(
                {left->second.no_token, right->second.yes_token}, decision);
            if (!clocks.has_value()) return;
            record_locked(pm::fast::evaluate_implication(
                relation, left->second, right->second, books_, fees_, policy_,
                clocks->exchange_ms, clocks->received_ms, decision));
        } else if (spec.type == RelationType::MutualExclusion) {
            const auto clocks = freshness_window_locked(
                {left->second.no_token, right->second.no_token}, decision);
            if (!clocks.has_value()) return;
            record_locked(pm::fast::evaluate_mutual_exclusion(
                relation, left->second, right->second, books_, fees_, policy_,
                clocks->exchange_ms, clocks->received_ms, decision));
        } else {
            const auto clocks = freshness_window_locked(
                {left->second.yes_token, right->second.yes_token}, decision);
            if (!clocks.has_value()) return;
            record_locked(pm::fast::evaluate_exhaustive_pair(
                relation, left->second, right->second, books_, fees_, policy_,
                clocks->exchange_ms, clocks->received_ms, decision));
        }
    }

    void evaluate_all_locked() {
        for (const auto& [market_id, market] : markets_) {
            (void)market_id;
            evaluate_single_locked(market);
        }
        for (const auto& event_id : safe_negrisk_events_) {
            evaluate_event_locked(event_id);
        }
        for (std::size_t i = 0; i < active_relations_.size(); ++i) {
            evaluate_relation_locked(i);
        }
    }

    void evaluate_market_locked(const std::string& market_id) {
        const auto market_it = markets_.find(market_id);
        if (market_it == markets_.end()) return;
        evaluate_single_locked(market_it->second);
        evaluate_event_locked(market_it->second.event_id);

        std::unordered_set<std::size_t> relation_indices;
        if (const auto it = relations_by_market_.find(market_id);
            it != relations_by_market_.end()) {
            relation_indices.insert(it->second.begin(), it->second.end());
        }
        for (const auto index : relation_indices) {
            evaluate_relation_locked(index);
        }
    }

    void record_locked(pm::fast::Opportunity opportunity) {
        ++evaluations_;
        if (opportunity.executable) {
            ++executable_observations_;
            if (opportunity.hard_arbitrage) ++hard_executable_observations_;
        }
        const auto timestamp = now_ms();
        const auto edge_bucket = static_cast<long long>(
            std::floor(opportunity.net_edge_per_share * 100000.0));
        const auto shares_bucket = static_cast<long long>(
            std::floor(opportunity.executable_shares * 100.0));
        const std::string signature = std::to_string(opportunity.executable) + ':' +
                                      std::to_string(edge_bucket) + ':' +
                                      std::to_string(shares_bucket) + ':' +
                                      opportunity.reject_reason;
        const bool changed = signatures_[opportunity.id] != signature;
        const bool cooled = timestamp - last_log_ms_[opportunity.id] >= 100;
        latest_[opportunity.id] = opportunity;
        signatures_[opportunity.id] = signature;
        if (changed && cooled) {
            write_opportunity(opportunity_log_, timestamp, opportunity);
            last_log_ms_[opportunity.id] = timestamp;
            if (opportunity.executable) {
                constexpr std::size_t kMaxPendingCandidates = 4096;
                if (pending_candidates_.size() < kMaxPendingCandidates) {
                    pending_candidates_.push_back(opportunity);
                } else {
                    ++candidates_dropped_;
                }
            }
        }
    }

    void push_latency_locked(double feed_ms, double decision_us) {
        if (feed_ms >= 0.0) feed_latency_ms_.push_back(feed_ms);
        if (decision_us >= 0.0) decision_latency_us_.push_back(decision_us);
        constexpr std::size_t limit = 20000;
        while (feed_latency_ms_.size() > limit) feed_latency_ms_.pop_front();
        while (decision_latency_us_.size() > limit) decision_latency_us_.pop_front();
    }
};
