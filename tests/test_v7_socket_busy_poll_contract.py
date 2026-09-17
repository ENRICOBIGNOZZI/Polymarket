from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_busy_poll_is_per_socket_and_verified() -> None:
    header = text("include/pm/socket_tuning.hpp")
    assert "SO_BUSY_POLL" in header
    assert "setsockopt" in header
    assert "getsockopt" in header
    assert "SO_INCOMING_CPU" in header
    assert "SO_INCOMING_NAPI_ID" in header
    assert "kMaxBusyPollUs = 2000" in header


def test_all_hft_network_surfaces_use_same_primitive() -> None:
    assert "apply_busy_poll" in text("src/http.cpp")
    assert "CURLOPT_SOCKOPTFUNCTION" in text("src/http.cpp")
    assert "apply_busy_poll" in text("src/fast_ws.cpp")
    assert "apply_busy_poll" in text("src/v7_external_ws.cpp")


def test_runtime_and_probe_expose_measured_busy_poll_value() -> None:
    runtime = text("src/v7_crypto_flash_shadow_runtime.cpp")
    probe = text("src/v7_latency_probe.cpp")
    assert '--socket-busy-poll-us' in runtime
    assert 'socket_busy_poll_us' in runtime
    assert '--socket-busy-poll-us' in probe
    assert 'socket_busy_poll_us' in probe
    assert 'incoming_cpu' in probe
    assert 'incoming_napi_id' in probe


def test_default_semantics_remain_off() -> None:
    assert "socket_busy_poll_us = 0" in text("include/pm/v7_external_ws.hpp")
    assert "socket_busy_poll_us = 0" in text("src/v7_crypto_flash_shadow_runtime.cpp")
    assert "socket_busy_poll_us = 0" in text("src/v7_latency_probe.cpp")


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for _, test in tests:
        test()
    print(f"{len(tests)} function tests passed")
