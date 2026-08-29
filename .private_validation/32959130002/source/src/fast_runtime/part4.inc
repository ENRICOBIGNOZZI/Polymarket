Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        auto next = [&]() {
            if (i + 1 >= argc) throw std::runtime_error("missing value after " + argument);
            return std::string(argv[++i]);
        };
        if (argument == "--config") options.config = next();
        else if (argument == "--policy") options.policy = next();
        else if (argument == "--relations") options.relations = next();
        else if (argument == "--external-signals") options.external_signals = next();
        else if (argument == "--run-dir") options.run_dir = next();
        else if (argument == "--ws-url") options.ws_url = next();
        else if (argument == "--markets") options.markets = std::stoull(next());
        else if (argument == "--shard-size") options.shard_size = std::stoull(next());
        else if (argument == "--min-liquidity") options.min_liquidity = std::stod(next());
        else if (argument == "--snapshot-refresh-seconds") {
            options.snapshot_refresh_seconds = std::stoi(next());
        } else if (argument == "--status-interval-seconds") {
            options.status_interval_seconds = std::stoi(next());
        } else if (argument == "--duration-seconds") {
            options.duration_seconds = std::stoi(next());
        } else if (argument == "--recycle-seconds") {
            options.recycle_seconds = std::stoi(next());
        } else if (argument == "--latency-sample-every") {
            options.latency_sample_every = std::stoull(next());
        } else if (argument == "--help" || argument == "-h") {
            std::cout
                << "polymarket_fast_arb_shadow [--config FILE] [--policy FILE] "
                   "[--relations CSV] [--external-signals CSV] [--run-dir DIR] "
                   "[--markets N] [--min-liquidity X] [--shard-size N] "
                   "[--snapshot-refresh-seconds N] [--status-interval-seconds N] "
                   "[--duration-seconds N] [--recycle-seconds N]\n";
            std::exit(0);
        } else {
            throw std::runtime_error("unknown argument: " + argument);
        }
    }
    options.shard_size = std::max<std::size_t>(1, options.shard_size);
    options.snapshot_refresh_seconds = std::max(5, options.snapshot_refresh_seconds);
    options.status_interval_seconds = std::max(1, options.status_interval_seconds);
    options.duration_seconds = std::max(0, options.duration_seconds);
    options.recycle_seconds = std::max(0, options.recycle_seconds);
    return options;
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::signal(SIGINT, handle_signal);
        std::signal(SIGTERM, handle_signal);
        auto options = parse_options(argc, argv);
        auto config = pm::Engine::load_config(options.config);
        if (options.external_signals.empty()) options.external_signals = config.external_signals_file;
        auto policy = load_policy(options.policy);
        FastRuntime runtime(std::move(config), policy, options);
        runtime.initialize();

        pm::fast::MarketWebSocketFeed feed(
            options.ws_url, runtime.tokens(), options.shard_size,
            [&](std::string_view message, std::int64_t received_ms, std::size_t shard) {
                runtime.on_payload(message, received_ms, shard);
            },
            [&](std::size_t shard, std::string_view message) {
                runtime.append_feed_error(shard, message);
            });
        feed.start();

        const auto started = std::chrono::steady_clock::now();
        auto last_resync = started;
        auto last_publish = started - std::chrono::seconds(options.status_interval_seconds);
        auto last_external = started;
        while (!g_stop.load(std::memory_order_relaxed)) {
            const auto current = std::chrono::steady_clock::now();
            const auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
                current - started).count();
            if (options.duration_seconds > 0 && elapsed >= options.duration_seconds) break;
            if (options.recycle_seconds > 0 && elapsed >= options.recycle_seconds) break;

            if (current - last_resync >=
                std::chrono::seconds(options.snapshot_refresh_seconds)) {
                try {
                    runtime.resync_books();
                } catch (const std::exception& error) {
                    runtime.append_feed_error(0, std::string("REST resync: ") + error.what());
                }
                last_resync = current;
            }
            if (current - last_external >= std::chrono::seconds(1)) {
                runtime.reload_external();
                last_external = current;
            }
            if (current - last_publish >=
                std::chrono::seconds(options.status_interval_seconds)) {
                runtime.publish(feed.snapshot());
                last_publish = current;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        feed.stop();
        runtime.publish(feed.snapshot(), true);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "fatal: " << error.what() << '\n';
        return 1;
    }
}
