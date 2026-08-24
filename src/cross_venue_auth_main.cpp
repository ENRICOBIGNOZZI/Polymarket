#include "pm/cross_venue.hpp"

#include <boost/json.hpp>

#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc, char** argv) {
    try {
        std::filesystem::path config_path = "config/cross_venue.json";
        std::filesystem::path credentials_override;
        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            if (argument == "--config" && i + 1 < argc) config_path = argv[++i];
            else if (argument == "--credentials" && i + 1 < argc) credentials_override = argv[++i];
            else if (argument == "--help" || argument == "-h") {
                std::cout << "usage: prediction_cross_venue_auth_check [--config PATH] [--credentials PATH]\n";
                return 0;
            } else {
                throw std::runtime_error("unknown or incomplete argument: " + argument);
            }
        }
        auto config = pm::cross::load_engine_config(config_path);
        if (!credentials_override.empty()) config.credentials_file = credentials_override;
        const auto credentials = pm::cross::load_credentials(config.credentials_file);

        boost::json::object result;
        result["schema_version"] = 1;
        result["limitless_configured"] = credentials.has_limitless_auth();
        result["kalshi_configured"] = credentials.has_kalshi_auth();
        result["kalshi_environment"] = credentials.kalshi_environment;
        result["limitless_execution_enabled"] = credentials.limitless_execution_enabled;
        result["kalshi_execution_enabled"] = credentials.kalshi_execution_enabled;
        result["authenticated_order_submission_compiled"] = false;

        bool ok = true;
        if (credentials.has_limitless_auth()) {
            pm::cross::LimitlessClient client(credentials, config.limitless_base_url);
            const auto response = client.auth_check();
            result["limitless_http_status"] = response.status;
            result["limitless_auth_ok"] = response.status >= 200 && response.status < 300;
            ok = ok && response.status >= 200 && response.status < 300;
        } else {
            result["limitless_auth_ok"] = false;
            result["limitless_error"] = "credentials_missing";
            ok = false;
        }
        if (credentials.has_kalshi_auth()) {
            pm::cross::KalshiClient client(
                credentials, config.kalshi_production_base_url, config.kalshi_demo_base_url);
            const auto response = client.auth_check();
            result["kalshi_http_status"] = response.status;
            result["kalshi_auth_ok"] = response.status >= 200 && response.status < 300;
            ok = ok && response.status >= 200 && response.status < 300;
        } else {
            result["kalshi_auth_ok"] = false;
            result["kalshi_error"] = "credentials_or_key_file_missing";
            ok = false;
        }
        std::cout << boost::json::serialize(result) << '\n';
        return ok ? 0 : 1;
    } catch (const std::exception& error) {
        boost::json::object result;
        result["schema_version"] = 1;
        result["ok"] = false;
        result["error"] = error.what();
        std::cout << boost::json::serialize(result) << '\n';
        return 2;
    }
}
