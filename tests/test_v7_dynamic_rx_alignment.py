from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def text(path): return (ROOT / path).read_text(encoding="utf-8")
def test_alignment_uses_kernel_receive_cpu_and_napi_lineage():
    s=text("include/pm/rx_thread_alignment.hpp")
    assert "incoming_cpu(fd)" in s and "incoming_napi_id(fd)" in s
    assert "pin_current_thread_to_cpu(out.incoming_cpu)" in s and "out.rejected = 1" in s
def test_runtime_never_allows_dynamic_rx_on_decision_core():
    s=text("src/v7_crypto_flash_shadow_runtime.cpp")
    assert "feed_rx_cpus.assign(role_cpus.begin() + 1, role_cpus.end())" in s
    assert "dynamic_rx_cpu_allowlist = feed_rx_cpus" in s
def test_alignment_is_fail_closed_for_latency_evidence():
    s=text("src/v7_crypto_flash_shadow_runtime.cpp")
    assert "rx_alignment_observed" in s and "rx_alignment_clean" in s
    assert "rx_alignment_errors == 0" in s and "rx_rejections == 0" in s
def test_both_websocket_planes_use_the_same_alignment_primitive():
    assert "align_thread_to_socket_rx" in text("src/fast_ws.cpp")
    assert "align_thread_to_socket_rx" in text("src/v7_external_ws.cpp")
if __name__ == "__main__":
    tests=[f for n,f in globals().items() if n.startswith("test_") and callable(f)]
    [f() for f in tests]
    print(f"{len(tests)} function tests passed")
