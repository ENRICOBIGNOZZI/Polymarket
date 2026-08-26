#!/usr/bin/env python3
"""Fail-closed single-owner supervisor for the paper runtime.

The launcher retains the advisory lock itself and supervises one isolated child
process group. Letting the descriptor cross ``exec`` into every descendant made
a stopped wrapper capable of leaving an orphan child that still held the runtime
lock indefinitely. The supervisor also drains the complete owned process group
when the direct wrapper exits so recorder/proxy/broker descendants cannot survive
as stale writers or fixed-port listeners for the next validated runtime.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _current_owner(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 128).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _signal_child_group(group_id: int, signum: int) -> None:
    """Signal the isolated runtime group, including orphaned descendants."""

    try:
        os.killpg(group_id, signum)
    except ProcessLookupError:
        pass


def _child_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # An owned runtime group unexpectedly becoming unsignalable is not
        # evidence that it disappeared; keep cleanup fail-closed.
        return True
    return True


def _drain_child_group(group_id: int, timeout_seconds: float = 5.0) -> None:
    """Retire every process left in the runtime's private process group.

    ``Popen.wait()`` only reaps the direct wrapper. If that wrapper exits before
    one of its proxy/broker/recorder descendants, checking ``child.poll()`` and
    skipping cleanup leaves the descendant alive. That stale process can keep a
    fixed localhost port or state writer across a validated deploy handoff.
    """

    if not _child_group_exists(group_id):
        return
    _signal_child_group(group_id, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _child_group_exists(group_id):
            return
        time.sleep(0.05)

    _signal_child_group(group_id, signal.SIGKILL)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _child_group_exists(group_id):
            return
        time.sleep(0.05)
    raise RuntimeError(f"runtime child process group {group_id} did not terminate")


def _supervise(command: list[str]) -> int:
    # start_new_session makes the direct child the process-group/session leader.
    # The group id is stable for the lifetime of any descendants even when that
    # direct wrapper exits first, so cleanup can still retire the complete tree.
    child: subprocess.Popen[object] = subprocess.Popen(
        command,
        close_fds=True,
        start_new_session=True,
    )
    group_id = child.pid

    def forward(signum: int, _frame: object) -> None:
        _signal_child_group(group_id, signum)

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, forward)
    try:
        return child.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        _drain_child_group(group_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-owner launcher for the Polymarket paper runtime")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing runtime command")

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = _current_owner(fd)
        suffix = f" (owner pid {owner})" if owner else ""
        print(f"fatal: another paper runtime already owns {args.lock}{suffix}", file=sys.stderr, flush=True)
        os.close(fd)
        return 75

    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    os.fsync(fd)
    # The supervisor, not its descendants, is the sole lock holder. The marker
    # remains readable for a bounded, provenance-checked deploy handoff.
    os.set_inheritable(fd, False)
    try:
        return _supervise(command)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
