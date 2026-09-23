#include "pm/v7_pure_arb_multi_ledger.hpp"

#include <boost/json.hpp>

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <string>

using namespace pm::v7;
namespace fs = std::filesystem;
namespace json = boost::json;

int main() {
    const auto suffix = std::to_string(
        std::chrono::steady_clock::now().time_since_epoch().count());
    const auto root = fs::temp_directory_path() / ("pm-v7-multi-ledger-" + suffix);
    fs::create_directories(root);

    PureArbMultiLedgerConfig config{};
    config.run_root = root.string();
    config.model_sha = std::string(40, 'a');
    config.run_id = "run-test";
    config.server_id = "server-test";
    config.risk_policy_sha256 = std::string(64, 'b');
    config.markets.push_back(PureArbLedgerMarket{
        "market-1", "event-1", "BTC", "M5",
        "yes-token", "no-token", "GAMMA_FEES_DISABLED_EXPLICIT",
        11, 12, 0.0, 1.0});

    PureArbMultiLedgerWriter writer(std::move(config));
    NativePaperFillRecord fill{};
    fill.command.client_order_id = 101;
    fill.command.command_id = 202;
    fill.client_order_id = 101;
    fill.command_id = 202;
    fill.instrument_handle = 11;
    fill.side = Side::Buy;
    fill.price_tick = 40;
    fill.tick_size_e4 = 100;
    fill.fill_microunits = 5'000'000;
    fill.exchange_event_ns = 1'700'000'000'000'000'000LL;
    fill.receive_monotonic_ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    fill.order_state = OrderState::Filled;
    fill.taker = 1;
    fill.arrival_book_receive_ns = fill.receive_monotonic_ns - 1000;
    fill.arrival_book_version = 7;

    assert(writer.publish(0, fill));
    writer.stop();
    const auto snap = writer.snapshot();
    assert(snap.healthy == 1);
    assert(snap.published == 1);
    assert(snap.written == 1);
    assert(snap.dropped == 0);
    assert(snap.queued == 0);

    const auto spool = root / "ledger" / "spool";
    std::size_t files = 0;
    for (const auto& entry : fs::directory_iterator(spool)) {
        if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
        ++files;
        std::ifstream input(entry.path());
        std::string raw((std::istreambuf_iterator<char>(input)),
                        std::istreambuf_iterator<char>());
        boost::system::error_code ec;
        auto value = json::parse(raw, ec);
        assert(!ec && value.is_object());
        const auto& o = value.as_object();
        assert(o.at("schema_version").as_int64() == 1);
        assert(o.at("event_type").as_string() == "FILL");
        assert(o.at("strategy").as_string() == "CRYPTO_SETTLEMENT_ENGINE");
        assert(o.at("paper_only").as_bool());
        assert(!o.at("authenticated_execution").as_bool());
        assert(o.at("market_id").as_string() == "market-1");
        assert(o.at("event_id").as_string() == "event-1");
        assert(o.at("token_id").as_string() == "yes-token");
        assert(o.at("side").as_string() == "BUY");
        assert(o.at("filled_size").as_double() == 5.0);
        assert(o.at("fee").as_double() == 0.0);
        const auto& metadata = o.at("metadata").as_object();
        assert(metadata.at("paired_complete_set").as_bool());
        const auto& receipt = metadata.at("native_settlement_receipt").as_object();
        assert(receipt.at("owner").as_string() == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE");
        const auto& client_id = receipt.at("client_order_id");
        const auto client_id_value = client_id.is_uint64()
            ? client_id.as_uint64()
            : static_cast<std::uint64_t>(client_id.as_int64());
        assert(client_id_value == 101);
    }
    assert(files == 1);
    fs::remove_all(root);
    return 0;
}
