from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7PublicDnsRuntimeContractTests(unittest.TestCase):
    def test_public_https_proxy_is_loopback_tls_tunnel_only(self) -> None:
        source = (ROOT / "scripts" / "v7_public_https_proxy.py").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_DNS = ("1.1.1.1", "8.8.8.8")', source)
        self.assertIn('parts[0].upper() != "CONNECT"', source)
        self.assertIn('port != 443', source)
        self.assertIn('server.bind((host, port))', source)
        self.assertIn('{"127.0.0.1", "::1", "localhost"}', source)
        self.assertNotIn("ssl.wrap_socket", source)
        self.assertNotIn("Authorization", source)

    def test_paper_loop_routes_public_rest_and_resolves_ws_without_os_dns(self) -> None:
        source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("v7_public_https_proxy.py", source)
        self.assertIn('export PM_V7_HTTPS_PROXY="$PUBLIC_PROXY"', source)
        self.assertIn('export HTTPS_PROXY="$PUBLIC_PROXY"', source)
        self.assertIn('PM_V7_WS_RESOLVE_IPS="$(python3 scripts/v7_public_https_proxy.py --resolve', source)
        proxy_export = source.index('export HTTPS_PROXY="$PUBLIC_PROXY"')
        self.assertLess(proxy_export, source.index("v7_rtds_external_fair_monitor.py"))
        self.assertLess(proxy_export, source.index("v7_external_fair_paper_router.py"))
        self.assertIn("maker_selection_ready()", source)
        self.assertGreaterEqual(source.count('while [[ ! -e "$KILL" ]] && ! maker_selection_ready'), 2)
        self.assertIn("if ! maker_selection_ready; then", source)
        self.assertIn('authenticated_execution":false', source)
        self.assertIn('real_order_submission":false', source)
        self.assertIn("v7_rtds_external_fair_monitor.py", source)

    def test_macos_deploy_transfers_runtime_ownership_to_launchd(self) -> None:
        updater = (ROOT / "ops" / "update_server_v7.sh").read_text(encoding="utf-8")
        template = (ROOT / "ops" / "launchd" / "com.polymarket.v7.paper.plist.in").read_text(encoding="utf-8")
        self.assertIn('launchctl bootstrap "$domain" "$destination"', updater)
        self.assertIn('launchctl print "gui/$(id -u)/com.polymarket.v7.paper"', updater)
        self.assertIn("PM_V7_EXACT_SHA_CI_GREEN", template)

    def test_cpp_http_client_explicitly_uses_v7_proxy_only_for_polymarket_https(self) -> None:
        source = (ROOT / "src" / "http.cpp").read_text(encoding="utf-8")
        self.assertIn('std::getenv("PM_V7_HTTPS_PROXY")', source)
        self.assertIn('constexpr std::string_view scheme = "https://"', source)
        self.assertIn('host == "polymarket.com"', source)
        self.assertIn('host.ends_with(suffix)', source)
        self.assertIn("CURLOPT_PROXY", source)
        self.assertIn("CURLPROXY_HTTP", source)
        self.assertIn("CURLOPT_NOPROXY", source)
        self.assertNotIn("CURLOPT_SSL_VERIFYPEER", source)
        self.assertNotIn("CURLOPT_SSL_VERIFYHOST", source)

    def test_websocket_override_preserves_tls_hostname(self) -> None:
        source = (ROOT / "src" / "fast_ws.cpp").read_text(encoding="utf-8")
        self.assertIn('std::getenv("PM_V7_WS_RESOLVE_IPS")', source)
        self.assertIn('endpoint.host.ends_with(".polymarket.com")', source)
        self.assertIn("asio::ip::make_address", source)
        self.assertIn("public_dns_override(endpoint)", source)
        self.assertIn("SSL_set_tlsext_host_name", source)
        self.assertIn("ssl::host_name_verification(endpoint.host)", source)
        self.assertIn("ws.handshake(endpoint.host, endpoint.target)", source)


if __name__ == "__main__":
    unittest.main()
