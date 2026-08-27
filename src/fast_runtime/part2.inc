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
        const auto observed_ms = now_ms();
        std::scoped_lock lock(mutex_);
        for (const auto& [token, snapshot] : fresh) {
            // A currently fresh full WS book has stronger gap-free lineage, so a REST
            // poll must not overwrite it. Otherwise /books is accepted only as a
            // timestamped point-in-time rebase; its timestamp/hash do not authorize
            // subsequent WS deltas without another full WS book event.
            const auto received = book_received_ts_ms_.find(token);
            const bool fresh_ws = ws_snapshot_ready_.count(token) &&
                                  received != book_received_ts_ms_.end() &&
                                  received->second > 0 && observed_ms >= received->second &&
                                  observed_ms - received->second <= kMaxBookAgeMs;
            if (fresh_ws) continue;
            books_[token] = snapshot.book;
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
            last_message = last_message_ms_;
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
            {"mode", "shadow"},
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
