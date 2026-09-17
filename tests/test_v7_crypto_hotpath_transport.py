from __future__ import annotations

import concurrent.futures
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from v7_fast_forward_ipc import FastForwardIpcBridge, request
from v7_global_portfolio_coordinator import process_fast_forward_payload
from v7_lead_lag_taker_runtime import LeadLagRuntime
from test_v7_global_portfolio_coordinator import forward_envelope


def test_ipc_wakes_owner_and_authorizes_without_receipt_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        socket_path = root / "control" / "forward.sock"
        result: dict = {}
        with FastForwardIpcBridge(socket_path) as bridge:
            started = time.perf_counter_ns()
            client = threading.Thread(
                target=lambda: result.update(request(socket_path, forward_envelope(), timeout_seconds=1.0))
            )
            client.start()
            assert bridge.wait(0.5)
            assert bridge.drain(lambda raw: process_fast_forward_payload(root, raw, now_ns=150)) == 1
            client.join(1.0)
            elapsed_us = (time.perf_counter_ns() - started) / 1_000.0
        assert not client.is_alive()
        assert result["action"] == "TAKE"
        assert result["paper_forward_test_authorized"] is True
        assert result["new_risk_authorized"] is False
        assert result["receipt_file_written"] is False
        assert result["receipt_transport"] == "DIRECT_COORDINATOR_REPLY"
        assert result["ipc_queue_wait_ns"] >= 0
        assert not (root / "opportunities" / "receipts").exists()
        assert elapsed_us < 500_000


def test_ipc_wait_has_no_lost_wakeup() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "forward.sock"
        result: dict = {}
        with FastForwardIpcBridge(path) as bridge:
            client = threading.Thread(target=lambda: result.update(request(path, {"candidate": 1})))
            client.start()
            deadline = time.monotonic() + 1.0
            while bridge.snapshot()["queued"] == 0 and time.monotonic() < deadline:
                time.sleep(.001)
            assert bridge.wait(0.0)
            bridge.drain(lambda _: {"action": "NOTHING", "new_risk_authorized": False})
            client.join(1.0)
            assert bridge.snapshot()["queued"] == 0
            assert not bridge.wait(0.001)


def test_slow_settlement_io_never_blocks_hot_owner_thread() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        runtime = object.__new__(LeadLagRuntime)
        runtime.root = root
        runtime.gamma_url = "https://gamma.invalid"
        runtime.settlement_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        runtime.settlement_futures = {}
        runtime.event_driven_signal = True
        runtime.coordinator_ipc = None
        runtime.hot_book_cache = None
        runtime.state = {
            "positions": {
                "p1": {
                    "position_id": "p1", "market_id": "m1", "settled": False,
                    "resolution_due_ms": 1, "settlement_attempt_ms": 0,
                }
            }
        }
        runtime._settlement_request = lambda _: (time.sleep(.2), {"closed": False})[1]
        started = time.perf_counter()
        runtime.settle_positions()
        elapsed = time.perf_counter() - started
        try:
            assert elapsed < .05, elapsed
            assert "p1" in runtime.settlement_futures
        finally:
            runtime.settlement_pool.shutdown(wait=True, cancel_futures=True)


def test_launcher_keeps_hotpath_opt_in() -> None:
    source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    assert 'PM_V7_CRYPTO_HOTPATH_IPC:-0' in source
    assert '--fast-forward-ipc "$FAST_FORWARD_IPC_SOCKET"' in source
    assert '--coordinator-ipc "$FAST_FORWARD_IPC_SOCKET"' in source
    assert '[[ -S "$FAST_FORWARD_IPC_SOCKET" ]]' in source


def test_hot_book_path_never_calls_clob_rest_when_metadata_ready() -> None:
    from v7_hot_book_cache import HotBook
    class Cache:
        def read(self, token: str, **kwargs):
            return HotBook(token, "m1", 9, 1_789_650_000_000_000, 123, time.time_ns() // 1_000_000,
                           .001, ((.49, 10.0),), ((.51, 10.0),), f"hot:{token}:9")
    class NoRest:
        def request_books(self, _):
            raise AssertionError("REST /books entered hot path")
    runtime = object.__new__(LeadLagRuntime)
    runtime.hot_book_cache = Cache()
    runtime.book_metadata_key = ("yes", "no")
    runtime.book_metadata = {"yes": (.001, 1.0), "no": (.001, 1.0)}
    runtime.book_metadata_future = None
    runtime.book_metadata_pool = None
    runtime.clob = NoRest()
    status = {"market": {"market_id": "m1", "yes_token": "yes", "no_token": "no"}}
    books = runtime.books(status)
    assert set(books) == {"yes", "no"}
    assert books["yes"].asks[0] == (.51, 10.0)


def test_hot_book_path_fails_closed_if_metadata_not_ready() -> None:
    class Cache:
        def read(self, *_args, **_kwargs):
            raise AssertionError("book must not be used before metadata is ready")
    class Pending:
        def done(self): return False
    runtime = object.__new__(LeadLagRuntime)
    runtime.hot_book_cache = Cache()
    runtime.book_metadata_key = ("yes", "no")
    runtime.book_metadata = {}
    runtime.book_metadata_future = Pending()
    status = {"market": {"market_id": "m1", "yes_token": "yes", "no_token": "no"}}
    assert runtime.books(status) == {}


def test_hot_candidate_uses_mmap_books_and_direct_receipt_without_rest_or_files() -> None:
    from unittest import mock
    from v7_hot_book_cache import HotBook
    from test_v7_lead_lag_taker_v1 import _runtime_case, _live_signal, _live_status, _receipt
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); runtime = _runtime_case(root)
        now_ns = time.time_ns(); sig = _live_signal(runtime.sha, now_ns); stat = _live_status(runtime.sha, now_ns)
        class Cache:
            def read(self, token, **_kwargs):
                ask = .40 if token == 'yes' else .61
                return HotBook(token, 'm-live', 11, now_ns, 123, time.time_ns()//1_000_000,
                               .01, ((ask-.01,100.0),), ((ask,100.0),), f'hot:{token}:11')
        runtime.event_driven_signal = True
        runtime.hot_book_cache = Cache()
        runtime.book_metadata_key = ('yes','no')
        runtime.book_metadata = {'yes':(.01,5.0),'no':(.01,5.0)}
        runtime.book_metadata_future = None
        runtime.cached_status = stat
        runtime.latest_signal = sig
        runtime.clob.request_books = mock.Mock(side_effect=AssertionError('REST /books entered hot path'))
        runtime.direct_receipt = mock.Mock(side_effect=lambda env: _receipt(env['deterministic_replay_key']))
        runtime.coordinator_ipc = Path('/unused-direct-ipc')
        runtime.candidate_step(sig)
        assert runtime.clob.request_books.call_count == 0
        assert runtime.direct_receipt.call_count == 1
        assert runtime.state['entries'] == 1
        assert not (root/'opportunities/fast_forward_inbox').exists()
        assert not (root/'opportunities/receipts').exists()


def test_hot_candidate_revalidates_newer_signal_while_waiting_for_coordinator() -> None:
    from unittest import mock
    from v7_hot_book_cache import HotBook
    from test_v7_lead_lag_taker_v1 import _runtime_case, _live_signal, _live_status, _receipt
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); runtime=_runtime_case(root)
        now_ns=time.time_ns(); sig=_live_signal(runtime.sha,now_ns); stat=_live_status(runtime.sha,now_ns)
        class Cache:
            def read(self, token, **_kwargs):
                ask=.40 if token=='yes' else .61
                return HotBook(token,'m-live',12,now_ns,124,time.time_ns()//1_000_000,.01,
                               ((ask-.01,100.0),),((ask,100.0),),f'hot:{token}:12')
        runtime.event_driven_signal=True; runtime.hot_book_cache=Cache()
        runtime.book_metadata_key=('yes','no'); runtime.book_metadata={'yes':(.01,5.0),'no':(.01,5.0)}
        runtime.book_metadata_future=None; runtime.cached_status=stat; runtime.latest_signal=sig
        runtime.clob.request_books=mock.Mock(side_effect=AssertionError('REST /books entered hot path'))
        runtime.coordinator_ipc=Path('/unused-direct-ipc')
        def authorize_then_invalidate(env):
            invalid=dict(sig); invalid['direction']='NONE'; invalid['confirmed_non_opposing']=False
            with runtime.signal_condition:
                runtime.latest_signal=invalid; runtime.signal_generation+=1
            return _receipt(env['deterministic_replay_key'])
        runtime.direct_receipt=mock.Mock(side_effect=authorize_then_invalidate)
        runtime.candidate_step(sig)
        assert runtime.state['entries']==0
        assert runtime.state['arrival_rejections']==1
        assert not (root/'ledger/spool').exists()
