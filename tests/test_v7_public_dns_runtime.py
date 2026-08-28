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
        self.assertIn("maker_selection_ready()", source)
        self.assertGreaterEqual(source.count('while [[ ! -e "$KILL" ]] && ! maker_selection_ready'), 2)
        self.assertIn("if ! maker_selection_ready; then", source)
        self.assertIn('authenticated_execution":false', source)
        self.assertIn('real_order_submission":false', source)

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
