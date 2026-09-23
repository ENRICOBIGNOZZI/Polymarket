import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_clock_guard", ROOT / "scripts/v7_clock_guard.py")
clock = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(clock)


def test_server_time_uses_explicit_https_proxy_and_headers(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return b"1001.0"

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return Response()

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    times = iter([1000.0, 1000.2])
    monkeypatch.setattr(clock.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(clock.time, "time", lambda: next(times))

    offset, rtt = clock.server_time(
        "https://clob.polymarket.com", 2.0, "http://127.0.0.1:19109")

    assert captured["url"] == "https://clob.polymarket.com/time"
    assert captured["timeout"] == 2.0
    assert captured["handlers"]
    proxy_handler = captured["handlers"][0]
    assert proxy_handler.proxies["https"] == "http://127.0.0.1:19109"
    assert captured["headers"]["User-agent"] == "polymarket-v7-clock-guard/1"
    assert captured["headers"]["Accept"] == "application/json"
    assert abs(rtt - 0.2) < 1e-12
    assert abs(offset - 0.9) < 1e-12


def test_runtime_wires_clock_guard_to_public_proxy():
    launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
    assert 'scripts/v7_clock_guard.py' in launcher
    assert '--https-proxy "$PUBLIC_PROXY"' in launcher
