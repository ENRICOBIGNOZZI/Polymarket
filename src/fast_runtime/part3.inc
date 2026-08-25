private:
    pm::Config cfg_;
    pm::PolymarketApi api_;
    pm::HttpClient http_;
    pm::fast::Policy policy_;
    Options options_;
    fs::path run_dir_;
    std::unordered_map<std::string, pm::Market> markets_;
    std::unordered_map<std::string, pm::Book> books_;
    std::unordered_map<std::string, std::int64_t> book_received_ms_;
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

    static std::string opportunity_header() {
        return "observed_ts_ms,kind,id,event_id,hard_arbitrage,executable,risk_class,"
               "reject_reason,payoff_floor,raw_edge_per_share,net_edge_per_share,"
               "executable_shares,capital_required,expected_profit,exchange_ts_ms,"
               "received_ts_ms,decision_ts_ms,feed_latency_ms,max_leg_book_age_ms,"
               "leg_book_skew_ms,legs";
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
            << feed_latency << ',' << opportunity.max_leg_book_age_ms << ','
            << opportunity.leg_book_skew_ms << ',' << csv_escape(leg_string(opportunity)) << '\n';
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
            pm::Book book;
            if (const auto it = books_.find(token); it != books_.end()) book = it->second;
            book.token_id = token;
            book.bids = parse_levels(event, "bids");
            book.asks = parse_levels(event, "asks");
            book.tick_size = get_number(event, "tick_size", book.tick_size);
            book.min_order_size = get_number(event, "min_order_size", book.min_order_size);
            book.last_trade = get_number(event, "last_trade_price", book.last_trade);
            books_[token] = std::move(book);
            book_received_ms_[token] = received_ms;
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
                auto& book = books_[token];
                book.token_id = token;
                const auto side = get_text(change, "side");
                pm::fast::apply_level(book, side == "BUY", get_number(change, "price", -1.0),
                                      get_number(change, "size", 0.0));
                book_received_ms_[token] = received_ms;
                affected.insert(token_to_market_.at(token));
            }
            return;
        }

        if (type == "tick_size_change") {
            const auto token = get_text(event, "asset_id");
            if (token.empty() || !token_to_market_.count(token)) return;
            books_[token].tick_size = get_number(event, "new_tick_size",
                                                   books_[token].tick_size);
            book_received_ms_[token] = received_ms;
            affected.insert(token_to_market_.at(token));
            return;
        }

        if (type == "last_trade_price") {
            const auto token = get_text(event, "asset_id");
            if (token.empty() || !token_to_market_.count(token)) return;
            books_[token].last_trade = get_number(event, "price", books_[token].last_trade);
            book_received_ms_[token] = received_ms;
            affected.insert(token_to_market_.at(token));
        }
    }

    void evaluate_single_locked(const pm::Market& market, std::int64_t exchange_ms,
                                std::int64_t received_ms) {
        const auto yes = books_.find(market.yes_token);
        const auto no = books_.find(market.no_token);
        if (yes == books_.end() || no == books_.end()) return;
        const auto fee_it = fees_.find(market.condition_id);
        const pm::FeeDetails fee = fee_it == fees_.end() ? pm::FeeDetails{} : fee_it->second;
        const auto decision = now_ms();
        record_locked(pm::fast::evaluate_binary(
            market, yes->second, no->second, fee, policy_, exchange_ms, received_ms, decision));
        record_locked(pm::fast::evaluate_maker_complete_set(
            market, yes->second, no->second, policy_, exchange_ms, received_ms, decision));
        if (const auto external_it = external_.find(market.id); external_it != external_.end()) {
            record_locked(pm::fast::evaluate_external_latency(
                market, yes->second, no->second, fee, external_it->second, decision, policy_,
                exchange_ms, received_ms, decision));
        }
    }

    void evaluate_event_locked(const std::string& event_id, std::int64_t exchange_ms,
                               std::int64_t received_ms) {
        if (!safe_negrisk_events_.count(event_id)) return;
        std::vector<const pm::Market*> members;
        const auto group = event_members_.find(event_id);
        if (group == event_members_.end()) return;
        for (const auto& id : group->second) {
            const auto member = markets_.find(id);
            if (member != markets_.end()) members.push_back(&member->second);
        }
        const auto decision = now_ms();
        record_locked(pm::fast::evaluate_negrisk_complete_set(
            event_id, members, books_, fees_, policy_, exchange_ms, received_ms, decision));
        for (const auto* source : members) {
            record_locked(pm::fast::evaluate_negrisk_conversion(
                event_id, *source, members, books_, fees_, policy_, exchange_ms,
                received_ms, decision));
        }
    }

    void evaluate_relation_locked(std::size_t index, std::int64_t exchange_ms,
                                  std::int64_t received_ms) {
        const auto& spec = active_relations_.at(index);
        const auto& relation = spec.relation;
        const auto left = markets_.find(relation.antecedent_market_id);
        const auto right = markets_.find(relation.consequent_market_id);
        if (left == markets_.end() || right == markets_.end()) return;
        const auto decision = now_ms();
        if (spec.type == RelationType::Implication) {
            record_locked(pm::fast::evaluate_implication(
                relation, left->second, right->second, books_, fees_, policy_, exchange_ms,
                received_ms, decision));
        } else if (spec.type == RelationType::MutualExclusion) {
            record_locked(pm::fast::evaluate_mutual_exclusion(
                relation, left->second, right->second, books_, fees_, policy_, exchange_ms,
                received_ms, decision));
        } else {
            record_locked(pm::fast::evaluate_exhaustive_pair(
                relation, left->second, right->second, books_, fees_, policy_, exchange_ms,
                received_ms, decision));
        }
    }

    void evaluate_all_locked(std::int64_t exchange_ms, std::int64_t received_ms) {
        for (const auto& [market_id, market] : markets_) {
            (void)market_id;
            evaluate_single_locked(market, exchange_ms, received_ms);
        }
        for (const auto& event_id : safe_negrisk_events_) {
            evaluate_event_locked(event_id, exchange_ms, received_ms);
        }
        for (std::size_t i = 0; i < active_relations_.size(); ++i) {
            evaluate_relation_locked(i, exchange_ms, received_ms);
        }
    }

    void evaluate_market_locked(const std::string& market_id, std::int64_t exchange_ms,
                                std::int64_t received_ms) {
        const auto market_it = markets_.find(market_id);
        if (market_it == markets_.end()) return;
        evaluate_single_locked(market_it->second, exchange_ms, received_ms);
        evaluate_event_locked(market_it->second.event_id, exchange_ms, received_ms);

        std::unordered_set<std::size_t> relation_indices;
        if (const auto it = relations_by_market_.find(market_id);
            it != relations_by_market_.end()) {
            relation_indices.insert(it->second.begin(), it->second.end());
        }
        for (const auto index : relation_indices) {
            evaluate_relation_locked(index, exchange_ms, received_ms);
        }
    }

    void annotate_hard_freshness_locked(pm::fast::Opportunity& opportunity) const {
        if (!opportunity.hard_arbitrage || opportunity.legs.empty()) return;
        bool complete = true;
        bool first = true;
        std::int64_t oldest = 0;
        std::int64_t newest = 0;
        for (const auto& leg : opportunity.legs) {
            const auto it = book_received_ms_.find(leg.token_id);
            if (it == book_received_ms_.end() || it->second <= 0) {
                complete = false;
                break;
            }
            if (first) {
                oldest = newest = it->second;
                first = false;
            } else {
                oldest = std::min(oldest, it->second);
                newest = std::max(newest, it->second);
            }
        }
        if (!complete || first) return;
        const auto decision = opportunity.decision_ts_ms > 0 ? opportunity.decision_ts_ms : now_ms();
        opportunity.max_leg_book_age_ms = std::max<std::int64_t>(0, decision - oldest);
        opportunity.leg_book_skew_ms = std::max<std::int64_t>(0, newest - oldest);
    }

    void record_locked(pm::fast::Opportunity opportunity) {
        annotate_hard_freshness_locked(opportunity);
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
                                      std::to_string(opportunity.max_leg_book_age_ms) + ':' +
                                      std::to_string(opportunity.leg_book_skew_ms) + ':' +
                                      opportunity.reject_reason;
        const bool changed = signatures_[opportunity.id] != signature;
        const bool cooled = timestamp - last_log_ms_[opportunity.id] >= 100;
        latest_[opportunity.id] = opportunity;
        signatures_[opportunity.id] = signature;
        if (changed && cooled) {
            write_opportunity(opportunity_log_, timestamp, opportunity);
            last_log_ms_[opportunity.id] = timestamp;
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
