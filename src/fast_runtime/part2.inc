class FastRuntime {
public:
    FastRuntime(pm::Config config, pm::fast::Policy policy, Options options)
        : cfg_(std::move(config)), api_(cfg_), policy_(policy), options_(std::move(options)),
          run_dir_(options_.run_dir) {
        fs::create_directories(run_dir_);
        ensure_logs();
    }

    void initialize() {
        auto discovered = api_.discover_markets(options_.markets, options_.min_liquidity);
        const auto current = now_s();
        std::unordered_set<std::string> neg_events;
        for (auto& market : discovered) {
            if (!pm::pregame_market_eligible(
                    pm::MarketTiming{market.timed_sports, market.game_start_ts,
                                     market.seconds_delay},
                    current)) {
                continue;
            }
            if (market.neg_risk && !market.event_id.empty()) neg_events.insert(market.event_id);
            markets_[market.id] = std::move(market);
        }

        for (const auto& event_id : neg_events) {
            const auto expected_members = safe_event_market_count(event_id);
            if (!expected_members.has_value()) continue;
            try {
                auto members = api_.fetch_event_markets(event_id);
                bool complete = members.size() == *expected_members && members.size() >= 2;
                std::unordered_set<std::string> ids;
                for (auto& market : members) {
                    try {
                        if (const auto timing = api_.fetch_market_timing(market.id);
                            timing.has_value()) {
                            market.timed_sports = timing->timed_sports;
                            market.game_start_ts = timing->game_start_ts;
                            market.seconds_delay = timing->seconds_delay;
                        }
                    } catch (...) {
                    }
                    const pm::MarketTiming timing{market.timed_sports, market.game_start_ts,
                                                   market.seconds_delay};
                    complete = complete && market.active && !market.closed &&
                               market.enable_order_book && market.accepting_orders &&
                               pm::pregame_market_eligible(timing, current) &&
                               ids.insert(market.id).second;
                }
                if (!complete) continue;
                safe_negrisk_events_.insert(event_id);
                for (auto& market : members) markets_[market.id] = std::move(market);
            } catch (const std::exception& error) {
                append_error("negrisk_event", event_id + ": " + error.what());
            }
        }

        for (const auto& [id, market] : markets_) {
            (void)id;
            token_to_market_[market.yes_token] = market.id;
            token_to_market_[market.no_token] = market.id;
            event_members_[market.event_id].push_back(market.id);
            tokens_.push_back(market.yes_token);
            tokens_.push_back(market.no_token);
            pm::FeeDetails fee;
            bool fee_known = false;
            if (market.fee_rate >= 0.0) {
                fee = {market.fee_rate, market.fee_exponent, market.fee_taker_only};
                fee_known = true;
            } else {
                try {
                    fee = api_.fetch_fee_details(market);
                    fee_known = true;
                } catch (const std::exception& error) {
                    append_error("fee", market.id + ": " + error.what());
                }
            }
            if (fee_known) fees_[market.condition_id] = fee;
        }
        std::sort(tokens_.begin(), tokens_.end());
        tokens_.erase(std::unique(tokens_.begin(), tokens_.end()), tokens_.end());

        relations_ = load_relations(options_.relations);
        for (std::size_t i = 0; i < relations_.size(); ++i) {
            const auto& relation = relations_[i].relation;
            if (!markets_.count(relation.antecedent_market_id) ||
                !markets_.count(relation.consequent_market_id)) {
                append_error("relation", relation.id + ": unknown market id");
                continue;
            }
            active_relations_.push_back(relations_[i]);
            relations_by_market_[relation.antecedent_market_id].push_back(
                active_relations_.size() - 1);
            relations_by_market_[relation.consequent_market_id].push_back(
                active_relations_.size() - 1);
        }
        external_ = load_external_signals(options_.external_signals);
        resync_books();
        {
            std::scoped_lock lock(mutex_);
            initialized_ms_ = now_ms();
        }
    }

    [[nodiscard]] const std::vector<std::string>& tokens() const { return tokens_; }

    void on_payload(std::string_view payload, std::int64_t received_ms,
                    std::size_t shard) {
        const auto decision_start = std::chrono::steady_clock::now();
        boost::system::error_code error;
        const auto root = json::parse(payload, error);
        if (error) {
            append_error("websocket_json", error.message());
            return;
        }
        std::scoped_lock lock(mutex_);
        std::unordered_set<std::string> affected;
        std::int64_t exchange_ms = 0;
        if (root.is_array()) {
            for (const auto& value : root.as_array()) {
                if (value.is_object()) {
                    apply_event_locked(value.as_object(), affected, exchange_ms, received_ms);
                }
            }
        } else if (root.is_object()) {
            apply_event_locked(root.as_object(), affected, exchange_ms, received_ms);
        }
        if (affected.empty()) return;
        for (const auto& market_id : affected) {
            evaluate_market_locked(market_id);
        }
        const auto decision_end = std::chrono::steady_clock::now();
        const double decision_us = static_cast<double>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                decision_end - decision_start)
                .count());
        const double feed_ms = exchange_ms > 0 ?
                                   static_cast<double>(std::max<std::int64_t>(
                                       0, received_ms - exchange_ms)) :
                                   0.0;
        last_message_ms_ = received_ms;
        ++book_updates_;
        push_latency_locked(feed_ms, decision_us);
        if (options_.latency_sample_every > 0 &&
            book_updates_ % options_.latency_sample_every == 0) {
            latency_log_ << received_ms << ',' << shard << ',' << exchange_ms << ','
                         << feed_ms << ',' << decision_us << ',' << affected.size() << '\n';
        }
    }

    void resync_books() {
        const auto fresh = fetch_timestamped_rest_books();
        std::scoped_lock lock(mutex_);
        for (const auto& [token, snapshot] : fresh) {
            // A full WS snapshot followed by an uninterrupted delta stream is a
            // continuous state lineage. The age of the token's *last market update*
            // is not the age of that state: a quiet book can remain unchanged for
            // longer than the REST freshness window. Transport errors/reconnects
            // explicitly clear ws_snapshot_ready_, so REST must not downgrade a
            // still-continuous WS lineage merely because that token has been quiet.
            const bool fresh_ws = ws_snapshot_ready_.count(token) > 0;
            if (fresh_ws) continue;
            books_[token] = snapshot.book;
            ++token_state_versions_[token];
            ws_snapshot_ready_.erase(token);
            if (snapshot.exchange_ms > 0 && snapshot.received_ms > 0 &&
                !snapshot.hash.empty()) {
                rest_snapshot_ready_.insert(token);
                mark_l2_clock_locked(token, snapshot.exchange_ms, snapshot.received_ms);
            } else {
                rest_snapshot_ready_.erase(token);
                book_exchange_ts_ms_.erase(token);
                book_received_ts_ms_.erase(token);
            }
        }
        ++rest_resyncs_;
    }

    void reload_external() {
        const auto fresh = load_external_signals(options_.external_signals);
        std::scoped_lock lock(mutex_);
        external_ = fresh;
        for (const auto& [market_id, signal] : external_) {
            (void)signal;
            evaluate_market_locked(market_id);
        }
    }

    void reload_relations() {
        const auto fresh = load_relations(options_.relations);
        std::scoped_lock lock(mutex_);
        relations_ = fresh;
        active_relations_.clear();
        relations_by_market_.clear();
        for (const auto& spec : relations_) {
            const auto& relation = spec.relation;
            if (!markets_.count(relation.antecedent_market_id)
                || !markets_.count(relation.consequent_market_id)) {
                continue;
            }
            active_relations_.push_back(spec);
            const auto index = active_relations_.size() - 1;
            relations_by_market_[relation.antecedent_market_id].push_back(index);
            relations_by_market_[relation.consequent_market_id].push_back(index);
        }
    }

    void append_feed_error(std::size_t shard, std::string_view message) {
        // Any transport failure/reconnect breaks gap-free WS L2 lineage for the
        // affected subscription shard. An independent REST point-in-time snapshot
        // may remain usable until its own exchange-age bound expires.
        {
            std::scoped_lock lock(mutex_);
            const std::size_t begin = shard * options_.shard_size;
            const std::size_t end = std::min(tokens_.size(), begin + options_.shard_size);
            for (std::size_t i = begin; i < end; ++i) {
                const auto& token = tokens_[i];
                ws_snapshot_ready_.erase(token);
                ++lineage_epochs_[token];
                if (!rest_snapshot_ready_.count(token)) {
                    book_exchange_ts_ms_.erase(token);
                    book_received_ts_ms_.erase(token);
                }
            }
        }
        append_error("websocket_shard_" + std::to_string(shard), std::string(message));
    }

    void publish(const pm::fast::FeedSnapshot& feed, bool stopping = false) {
        std::vector<pm::fast::Opportunity> latest;
        std::deque<double> feed_latency;
        std::deque<double> decision_latency;
        std::uint64_t updates = 0;
        std::uint64_t evaluations = 0;
        std::uint64_t executable = 0;
        std::uint64_t hard_executable = 0;
        std::uint64_t rest_resyncs = 0;
        std::uint64_t freshness_missing = 0;
        std::uint64_t freshness_age = 0;
        std::uint64_t freshness_skew = 0;
        std::size_t external_count = 0;
        std::size_t ws_snapshot_tokens = 0;
        std::size_t rest_snapshot_tokens = 0;
        std::size_t freshness_ready_tokens = 0;
        std::vector<pm::fast::Opportunity> pending_candidates;
        json::array shared_books;
        json::array live_flow_rows;
        std::uint64_t raw_last_trade_events = 0;
        std::uint64_t valid_trade_prints = 0;
        std::uint64_t trade_unknown_asset = 0;
        std::uint64_t trade_missing_side = 0;
        std::uint64_t trade_missing_size = 0;
        std::uint64_t trade_invalid_quantity = 0;
        std::uint64_t trade_invalid_price = 0;
        std::uint64_t trade_invalid_timestamp = 0;
        std::uint64_t duplicate_trade_events = 0;
        std::int64_t last_message = 0;
        const auto snapshot_ms = now_ms();
        {
            std::scoped_lock lock(mutex_);
            latest.reserve(latest_.size());
            for (const auto& [id, opportunity] : latest_) {
                (void)id;
                auto current = opportunity;
                if (current.executable) {
                    const auto required = opportunity_required_tokens_locked(current);
                    if (!freshness_window_locked(required, snapshot_ms, false).has_value()) {
                        current.executable = false;
                        current.expected_profit = 0.0;
                        current.reject_reason = "stale_or_unsynchronized_leg_book";
                    }
                }
                latest.push_back(std::move(current));
            }
            feed_latency = feed_latency_ms_;
            decision_latency = decision_latency_us_;
            updates = book_updates_;
            evaluations = evaluations_;
            executable = executable_observations_;
            hard_executable = hard_executable_observations_;
            rest_resyncs = rest_resyncs_;
            freshness_missing = freshness_missing_rejections_;
            freshness_age = freshness_age_rejections_;
            freshness_skew = freshness_skew_rejections_;
            external_count = external_.size();
            ws_snapshot_tokens = ws_snapshot_ready_.size();
            rest_snapshot_tokens = rest_snapshot_ready_.size();
            freshness_ready_tokens = ws_snapshot_tokens + rest_snapshot_tokens;
            pending_candidates.swap(pending_candidates_);
            last_message = last_message_ms_;
            raw_last_trade_events = raw_last_trade_events_;
            valid_trade_prints = valid_trade_prints_;
            trade_unknown_asset = trade_unknown_asset_;
            trade_missing_side = trade_missing_side_;
            trade_missing_size = trade_missing_size_;
            trade_invalid_quantity = trade_invalid_quantity_;
            trade_invalid_price = trade_invalid_price_;
            trade_invalid_timestamp = trade_invalid_timestamp_;
            duplicate_trade_events = duplicate_trade_events_;
            shared_books.reserve(books_.size());
            for (const auto& [token, book] : books_) {
                const auto market_id = token_to_market_.find(token);
                if (market_id == token_to_market_.end()) continue;
                const auto market = markets_.find(market_id->second);
                const auto exchange = book_exchange_ts_ms_.find(token);
                const auto received = book_received_ts_ms_.find(token);
                if (market == markets_.end() || exchange == book_exchange_ts_ms_.end()
                    || received == book_received_ts_ms_.end() || book.bids.empty()
                    || book.asks.empty()) continue;
                auto levels = [](const std::vector<pm::Level>& raw, bool bids) {
                    auto sorted = raw;
                    std::sort(sorted.begin(), sorted.end(), [bids](const auto& a, const auto& b) {
                        return bids ? a.price > b.price : a.price < b.price;
                    });
                    json::array output;
                    output.reserve(sorted.size());
                    for (const auto& level : sorted) {
                        if (level.price > 0.0 && level.price < 1.0 && level.size > 0.0) {
                            output.emplace_back(json::object{
                                {"price", level.price}, {"size", level.size}});
                        }
                    }
                    return output;
                };
                const auto fee = fees_.find(market->second.condition_id);
                const auto economic_state = std::make_pair(
                    lineage_epochs_[token], token_state_versions_[token]);
                const bool economic_novelty =
                    last_published_economic_state_[token] != economic_state;
                last_published_economic_state_[token] = economic_state;
                const auto trades = live_trades_.find(token);
                json::object row{
                    {"token_id", token},
                    {"market_id", market->second.id},
                    {"condition_id", market->second.condition_id},
                    {"event_id", market->second.event_id},
                    {"outcome", token == market->second.yes_token ? "YES" : "NO"},
                    {"exchange_ts_ms", exchange->second},
                    {"receive_ts_ms", received->second},
                    {"source_receive_ts_ms", received->second},
                    {"snapshot_published_ms", snapshot_ms},
                    {"state_version", static_cast<std::int64_t>(token_state_versions_[token])},
                    {"lineage_epoch", static_cast<std::int64_t>(lineage_epochs_[token])},
                    {"lineage_continuous", ws_snapshot_ready_.count(token) > 0},
                    {"economic_novelty", economic_novelty},
                    {"last_book_change_receive_ms", last_book_change_receive_ms_[token]},
                    {"last_trade_receive_ms", trades != live_trades_.end() && !trades->second.empty()
                        ? trades->second.back().receive_ms : 0},
                    {"last_trade_exchange_ms", trades != live_trades_.end() && !trades->second.empty()
                        ? trades->second.back().exchange_ms : 0},
                    {"provenance", ws_snapshot_ready_.count(token) > 0 ? "WEBSOCKET" : "REST_BOOTSTRAP"},
                    {"tick_size", book.tick_size},
                    {"min_order_size", book.min_order_size},
                    {"bids", levels(book.bids, true)},
                    {"asks", levels(book.asks, false)},
                    {"fee_verified", fee != fees_.end()},
                    {"fee_rate", fee != fees_.end() ? fee->second.rate : 0.0},
                    {"fee_exponent", fee != fees_.end() ? fee->second.exponent : 1.0},
                    {"fee_taker_only", fee != fees_.end() ? fee->second.taker_only : true},
                };
                shared_books.emplace_back(std::move(row));
            }
            live_flow_rows.reserve(live_trades_.size());
            const auto cutoff = snapshot_ms - 600'000;
            for (auto& [token, prints] : live_trades_) {
                while (!prints.empty() && prints.front().receive_ms < cutoff) prints.pop_front();
                if (prints.empty()) continue;
                const auto market_id = token_to_market_.find(token);
                if (market_id == token_to_market_.end()) continue;
                const auto market = markets_.find(market_id->second);
                if (market == markets_.end()) continue;
                auto activity_it = book_activity_.find(token);
                if (activity_it != book_activity_.end()) {
                    while (!activity_it->second.empty()
                           && activity_it->second.front().receive_ms < cutoff) {
                        activity_it->second.pop_front();
                    }
                }
                const auto book_it = books_.find(token);
                const auto best_price = [](const std::vector<pm::Level>& levels, bool bid) {
                    double best = bid ? 0.0 : 1.0;
                    for (const auto& level : levels) {
                        if (level.size <= 0.0) continue;
                        best = bid ? std::max(best, level.price) : std::min(best, level.price);
                    }
                    return best;
                };
                const auto visible_depth = [](const std::vector<pm::Level>& levels) {
                    double depth = 0.0;
                    for (const auto& level : levels) depth += std::max(0.0, level.size);
                    return depth;
                };
                const auto best_level_depth = [](const std::vector<pm::Level>& levels,
                                                 double best) {
                    double depth = 0.0;
                    for (const auto& level : levels) {
                        if (level.size > 0.0 && std::abs(level.price - best) <= 1e-12) {
                            depth += level.size;
                        }
                    }
                    return depth;
                };
                const double best_bid = book_it != books_.end()
                    ? best_price(book_it->second.bids, true) : 0.0;
                const double best_ask = book_it != books_.end()
                    ? best_price(book_it->second.asks, false) : 1.0;
                json::object row{
                    {"token_id", token},
                    {"market_id", market->second.id},
                    {"condition_id", market->second.condition_id},
                    {"event_id", market->second.event_id},
                    {"outcome", token == market->second.yes_token ? "YES" : "NO"},
                    {"last_exchange_ts_ms", prints.back().exchange_ms},
                    {"last_receive_ts_ms", prints.back().receive_ms},
                    {"state_version", static_cast<std::int64_t>(token_state_versions_[token])},
                    {"lineage_epoch", static_cast<std::int64_t>(lineage_epochs_[token])},
                    {"tick_size", book_it != books_.end() ? book_it->second.tick_size : 0.0},
                    {"best_bid", best_bid},
                    {"best_ask", best_ask},
                    {"spread", best_ask > best_bid ? best_ask - best_bid : 0.0},
                    {"best_bid_depth", book_it != books_.end()
                        ? best_level_depth(book_it->second.bids, best_bid) : 0.0},
                    {"best_ask_depth", book_it != books_.end()
                        ? best_level_depth(book_it->second.asks, best_ask) : 0.0},
                    {"visible_bid_depth", book_it != books_.end()
                        ? visible_depth(book_it->second.bids) : 0.0},
                    {"visible_ask_depth", book_it != books_.end()
                        ? visible_depth(book_it->second.asks) : 0.0},
                };
                json::array trade_prints;
                trade_prints.reserve(prints.size());
                for (const auto& print : prints) {
                    trade_prints.emplace_back(json::object{
                        {"exchange_ts_ms", print.exchange_ms},
                        {"receive_ts_ms", print.receive_ms},
                        {"side", print.side},
                        {"price", print.price},
                        {"size", print.size},
                    });
                }
                row["trade_prints"] = std::move(trade_prints);
                for (const auto window_ms : {5'000LL, 15'000LL, 30'000LL,
                                             120'000LL, 600'000LL}) {
                    std::int64_t buy_prints = 0;
                    std::int64_t sell_prints = 0;
                    double buy_shares = 0.0;
                    double sell_shares = 0.0;
                    double buy_notional = 0.0;
                    double sell_notional = 0.0;
                    std::int64_t last_buy_receive = 0;
                    std::int64_t last_sell_receive = 0;
                    std::int64_t trade_through_count = 0;
                    std::vector<double> sizes;
                    for (const auto& print : prints) {
                        if (snapshot_ms - print.receive_ms > window_ms) continue;
                        sizes.push_back(print.size);
                        if (print.trade_through) ++trade_through_count;
                        if (print.side == "BUY") {
                            ++buy_prints;
                            buy_shares += print.size;
                            buy_notional += print.size * print.price;
                            last_buy_receive = std::max(last_buy_receive, print.receive_ms);
                        } else {
                            ++sell_prints;
                            sell_shares += print.size;
                            sell_notional += print.size * print.price;
                            last_sell_receive = std::max(last_sell_receive, print.receive_ms);
                        }
                    }
                    const auto suffix = std::to_string(window_ms / 1000) + "s";
                    row["buy_prints_" + suffix] = buy_prints;
                    row["sell_prints_" + suffix] = sell_prints;
                    row["buy_shares_" + suffix] = buy_shares;
                    row["sell_shares_" + suffix] = sell_shares;
                    row["buy_notional_" + suffix] = buy_notional;
                    row["sell_notional_" + suffix] = sell_notional;
                    row["last_buy_receive_ts_ms_" + suffix] = last_buy_receive;
                    row["last_sell_receive_ts_ms_" + suffix] = last_sell_receive;
                    std::sort(sizes.begin(), sizes.end());
                    const auto size_quantile = [&sizes](double q) {
                        if (sizes.empty()) return 0.0;
                        const auto index = static_cast<std::size_t>(std::floor(
                            q * static_cast<double>(sizes.size() - 1)));
                        return sizes[std::min(index, sizes.size() - 1)];
                    };
                    row["size_p50_" + suffix] = size_quantile(0.50);
                    row["size_p90_" + suffix] = size_quantile(0.90);
                    row["trade_through_count_" + suffix] = trade_through_count;
                    std::int64_t book_updates = 0;
                    std::int64_t l2_reductions = 0;
                    double l2_removed_size = 0.0;
                    if (activity_it != book_activity_.end()) {
                        for (const auto& activity : activity_it->second) {
                            if (snapshot_ms - activity.receive_ms > window_ms) continue;
                            ++book_updates;
                            if (activity.removed_size > 0.0) {
                                ++l2_reductions;
                                l2_removed_size += activity.removed_size;
                            }
                        }
                    }
                    row["book_updates_" + suffix] = book_updates;
                    row["l2_reductions_" + suffix] = l2_reductions;
                    row["l2_removed_size_" + suffix] = l2_removed_size;
                }
                live_flow_rows.emplace_back(std::move(row));
            }
            opportunity_log_.flush();
            latency_log_.flush();
            error_log_.flush();
        }
        std::sort(latest.begin(), latest.end(), [](const auto& left, const auto& right) {
            if (left.executable != right.executable) return left.executable > right.executable;
            if (left.net_edge_per_share != right.net_edge_per_share) {
                return left.net_edge_per_share > right.net_edge_per_share;
            }
            return left.id < right.id;
        });

        std::ostringstream csv;
        csv << opportunity_header() << '\n';
        for (const auto& opportunity : latest) write_opportunity(csv, snapshot_ms, opportunity);
        atomic_write(run_dir_ / "fast_arb_latest.csv", csv.str());
        for (const auto& opportunity : pending_candidates) spool_candidate(opportunity);

        const std::string shared_snapshot_id = options_.model_sha + ":" +
            std::to_string(updates) + ":" + std::to_string(snapshot_ms);
        json::object shared_state{
            {"schema", "polymarket_v7_shared_market_state_v1"},
            {"timestamp_ms", snapshot_ms},
            {"snapshot_id", shared_snapshot_id},
            {"generation", static_cast<std::int64_t>(updates)},
            {"producer", "FAST_STRUCTURAL_CPP_WEBSOCKET"},
            {"model_sha", options_.model_sha},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"single_source_of_truth_for_freshness", true},
            {"books", std::move(shared_books)},
        };
        atomic_write(fs::path(options_.run_root) / "market_data" / "shared_state.json",
                     json::serialize(shared_state) + "\n");
        json::object live_flow{
            {"schema", "polymarket_v7_live_trade_flow_v1"},
            {"timestamp_ms", snapshot_ms},
            {"producer", "FAST_STRUCTURAL_CPP_WEBSOCKET"},
            {"model_sha", options_.model_sha},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"window_clock", "local_receive_ts_ms"},
            {"raw_last_trade_events", static_cast<std::int64_t>(raw_last_trade_events)},
            {"valid_trade_prints", static_cast<std::int64_t>(valid_trade_prints)},
            {"unknown_asset", static_cast<std::int64_t>(trade_unknown_asset)},
            {"missing_side", static_cast<std::int64_t>(trade_missing_side)},
            {"missing_size", static_cast<std::int64_t>(trade_missing_size)},
            {"invalid_quantity", static_cast<std::int64_t>(trade_invalid_quantity)},
            {"invalid_price", static_cast<std::int64_t>(trade_invalid_price)},
            {"invalid_timestamp", static_cast<std::int64_t>(trade_invalid_timestamp)},
            {"duplicate_trade", static_cast<std::int64_t>(duplicate_trade_events)},
            {"rows", std::move(live_flow_rows)},
        };
        atomic_write(fs::path(options_.run_root) / "market_data" / "live_trade_flow.json",
                     json::serialize(live_flow) + "\n");

        std::size_t current_executable = 0;
        std::size_t current_hard = 0;
        std::size_t current_stale = 0;
        double best_edge = -1.0;
        double best_profit = 0.0;
        for (const auto& opportunity : latest) {
            if (!opportunity.executable) {
                if (opportunity.reject_reason == "stale_or_unsynchronized_leg_book") {
                    ++current_stale;
                }
                continue;
            }
            ++current_executable;
            if (opportunity.hard_arbitrage) ++current_hard;
            best_edge = std::max(best_edge, opportunity.net_edge_per_share);
            best_profit = std::max(best_profit, opportunity.expected_profit);
        }

        json::object status{
            {"schema_version", 1},
            {"timestamp_ms", snapshot_ms},
            {"mode", "paper"},
            {"paper_only", true},
            {"authenticated_execution", false},
            {"real_order_submission", false},
            {"stopping", stopping},
            {"markets", static_cast<std::int64_t>(markets_.size())},
            {"tokens", static_cast<std::int64_t>(tokens_.size())},
            {"safe_negrisk_events", static_cast<std::int64_t>(safe_negrisk_events_.size())},
            {"logical_relations", static_cast<std::int64_t>(active_relations_.size())},
            {"external_signals", static_cast<std::int64_t>(external_count)},
            {"ws_workers", static_cast<std::int64_t>(feed.workers)},
            {"ws_connected_workers", static_cast<std::int64_t>(feed.connected_workers)},
            {"ws_messages", static_cast<std::int64_t>(feed.messages)},
            {"ws_reconnects", static_cast<std::int64_t>(feed.reconnects)},
            {"ws_errors", static_cast<std::int64_t>(feed.errors)},
            {"book_updates", static_cast<std::int64_t>(updates)},
            {"raw_last_trade_events", static_cast<std::int64_t>(raw_last_trade_events)},
            {"valid_trade_prints", static_cast<std::int64_t>(valid_trade_prints)},
            {"trade_unknown_asset", static_cast<std::int64_t>(trade_unknown_asset)},
            {"trade_missing_side", static_cast<std::int64_t>(trade_missing_side)},
            {"trade_missing_size", static_cast<std::int64_t>(trade_missing_size)},
            {"trade_invalid_quantity", static_cast<std::int64_t>(trade_invalid_quantity)},
            {"trade_invalid_price", static_cast<std::int64_t>(trade_invalid_price)},
            {"trade_invalid_timestamp", static_cast<std::int64_t>(trade_invalid_timestamp)},
            {"duplicate_trade_events", static_cast<std::int64_t>(duplicate_trade_events)},
            {"rest_resyncs", static_cast<std::int64_t>(rest_resyncs)},
            {"ws_snapshot_ready_tokens", static_cast<std::int64_t>(ws_snapshot_tokens)},
            {"rest_point_in_time_ready_tokens", static_cast<std::int64_t>(rest_snapshot_tokens)},
            {"freshness_ready_tokens", static_cast<std::int64_t>(freshness_ready_tokens)},
            {"book_freshness_max_age_ms", kMaxBookAgeMs},
            {"book_freshness_max_skew_ms", kMaxBookSkewMs},
            {"freshness_missing_rejections", static_cast<std::int64_t>(freshness_missing)},
            {"freshness_age_rejections", static_cast<std::int64_t>(freshness_age)},
            {"freshness_skew_rejections", static_cast<std::int64_t>(freshness_skew)},
            {"evaluations", static_cast<std::int64_t>(evaluations)},
            {"executable_observations", static_cast<std::int64_t>(executable)},
            {"hard_executable_observations", static_cast<std::int64_t>(hard_executable)},
            {"canonical_candidates_spooled", static_cast<std::int64_t>(candidates_spooled_)},
            {"canonical_candidates_dropped", static_cast<std::int64_t>(candidates_dropped_)},
            {"execution_incompatible_observations", static_cast<std::int64_t>(execution_incompatible_observations_)},
            {"passive_maker_observations", static_cast<std::int64_t>(passive_maker_observations_)},
            {"current_opportunities", static_cast<std::int64_t>(latest.size())},
            {"current_executable", static_cast<std::int64_t>(current_executable)},
            {"current_hard_executable", static_cast<std::int64_t>(current_hard)},
            {"current_stale_opportunities", static_cast<std::int64_t>(current_stale)},
            {"best_net_edge_per_share", best_edge},
            {"best_expected_profit", best_profit},
            {"last_message_ms", last_message},
            {"feed_stale_ms", last_message > 0 ? snapshot_ms - last_message : -1},
            {"feed_latency_p50_ms", quantile(feed_latency, 0.50)},
            {"feed_latency_p95_ms", quantile(feed_latency, 0.95)},
            {"feed_latency_p99_ms", quantile(feed_latency, 0.99)},
            {"decision_latency_p50_us", quantile(decision_latency, 0.50)},
            {"decision_latency_p95_us", quantile(decision_latency, 0.95)},
            {"decision_latency_p99_us", quantile(decision_latency, 0.99)},
        };
        atomic_write(run_dir_ / "fast_arb_status.json", json::serialize(status) + "\n");
    }
