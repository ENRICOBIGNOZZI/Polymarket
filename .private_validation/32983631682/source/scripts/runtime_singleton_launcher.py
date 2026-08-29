#!/usr/bin/env python3
"""Fail-closed single-owner supervisor for the paper runtime.

The launcher retains the advisory lock itself and supervises one isolated child
process group.  Letting the descriptor cross ``exec`` into every descendant
made a stopped wrapper capable of leaving an orphan child that still held the
runtime lock indefinitely.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path


def _current_owner(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 128).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _forward_to_child_group(child: subprocess.Popen[object], signum: int) -> None:
    """Forward a service signal to the owned runtime group, if it still exists."""

    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _supervise(command: list[str]) -> int:
    # start_new_session makes the direct child the process-group leader.  This
    # lets launchd stop the complete paper tree through this supervisor without
    # putting its singleton fd in child processes.
    child: subprocess.Popen[object] | None = subprocess.Popen(
        command,
        close_fds=True,
        start_new_session=True,
    )

    def forward(signum: int, _frame: object) -> None:
        _forward_to_child_group(child, signum)

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, forward)
    try:
        return child.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            _forward_to_child_group(child, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _forward_to_child_group(child, signal.SIGKILL)
                child.wait(timeout=5)


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
    # The supervisor, not its descendants, is the sole lock holder.  The
    # marker remains readable for a bounded, provenance-checked deploy handoff.
    os.set_inheritable(fd, False)
    try:
        return _supervise(command)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
