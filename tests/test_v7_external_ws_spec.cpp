#include "pm/v7_external_ws.hpp"

#include <cassert>
#include <stdexcept>
#include <string>

using namespace pm::v7::external_fair;

int main() {
    const auto eth_binance =
        crypto_connection_spec(VenueId::BinanceSpot, 0x455448ULL, "ETHUSDT");
    assert(eth_binance.symbol == "ETHUSDT");
    assert(eth_binance.subscription_json.find("ethusdt@depth@100ms") != std::string::npos);
    assert(eth_binance.subscription_json.find("ethusdt@bookTicker") != std::string::npos);
    assert(eth_binance.subscription_json.find("btcusdt") == std::string::npos);

    const auto sol_coinbase =
        crypto_connection_spec(VenueId::CoinbaseSpot, 0x534F4CULL, "SOL-USD");
    assert(sol_coinbase.symbol == "SOL-USD");
    assert(sol_coinbase.subscription_json.find("\"SOL-USD\"") != std::string::npos);
    assert(sol_coinbase.subscription_json.find("BTC-USD") == std::string::npos);

    const auto doge_bybit =
        crypto_connection_spec(VenueId::BybitSpot, 0x444F4745ULL, "DOGEUSDT");
    assert(doge_bybit.subscription_json.find("orderbook.50.DOGEUSDT") != std::string::npos);
    assert(doge_bybit.subscription_json.find("publicTrade.DOGEUSDT") != std::string::npos);

    const auto bnb_linear =
        crypto_connection_spec(VenueId::BybitLinear, 0x424E42ULL, "BNBUSDT");
    assert(bnb_linear.subscription_json.find("tickers.BNBUSDT") != std::string::npos);
    assert(bnb_linear.subscription_json.find("allLiquidation.BNBUSDT") != std::string::npos);

    const auto eth_deribit =
        crypto_connection_spec(VenueId::Deribit, 0x455448ULL, "ETH-PERPETUAL");
    assert(eth_deribit.subscription_json.find("ticker.ETH-PERPETUAL.100ms") != std::string::npos);
    assert(eth_deribit.subscription_json.find("trades.ETH-PERPETUAL.100ms") != std::string::npos);

    const auto btc_compat = btc_spot_connection_spec(VenueId::CoinbaseSpot, 0x425443ULL);
    assert(btc_compat.symbol == "BTC-USD");
    assert(btc_compat.subscription_json.find("\"BTC-USD\"") != std::string::npos);

    bool rejected = false;
    try {
        (void)crypto_connection_spec(VenueId::BinanceSpot, 1, "");
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    assert(rejected);

    rejected = false;
    try {
        (void)crypto_connection_spec(VenueId::Unknown, 1, "BTCUSDT");
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    assert(rejected);
    return 0;
}
