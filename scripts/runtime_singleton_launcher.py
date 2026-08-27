#!/usr/bin/env python3
"""Fail-closed single-owner supervisor for the PAPER runtime.

The launcher owns the canonical advisory lock and supervises one isolated
runtime process group.  A dedicated watchdog inherits the lock descriptor but
the runtime itself does not.  If the launcher is killed abruptly, the watchdog
keeps ownership fail-closed until the orphan runtime group has been drained.
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


def _write_owner(fd: int, pid: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, f"{pid}\n".encode("utf-8"))
    os.fsync(fd)


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _drain_process_group(pgid: int, grace_seconds: float = 5.0) -> None:
    """Terminate one owned group and do not return while it can still write."""

    if not _process_group_alive(pgid):
        return
    _signal_process_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while _process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_alive(pgid):
        _signal_process_group(pgid, signal.SIGKILL)
    # Fail closed.  If the kernel still reports the group alive, keep the
    # watchdog/launcher lock rather than admitting a second PAPER writer.
    while _process_group_alive(pgid):
        time.sleep(0.05)


def _watchdog(parent_pid: int, child_pgid: int, lock_fd: int) -> int:
    """Hold the singleton lock across abrupt launcher death and drain orphans."""

    os.fstat(lock_fd)
    while True:
        if not _process_group_alive(child_pgid):
            return 0
        # PPID changes immediately when the launcher exits, avoiding PID-reuse
        # ambiguity from a mere kill(parent_pid, 0) probe.
        if os.getppid() != parent_pid:
            _write_owner(lock_fd, os.getpid())
            print(
                f"singleton watchdog {os.getpid()} draining orphan runtime group {child_pgid} "
                f"after launcher {parent_pid} disappeared",
                file=sys.stderr,
                flush=True,
            )
            _drain_process_group(child_pgid)
            return 0
        time.sleep(0.05)


def _watchdog_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--child-pgid", type=int, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args(argv)
    return _watchdog(args.parent_pid, args.child_pgid, args.lock_fd)


def _forward_to_child_group(child: subprocess.Popen[object], signum: int) -> None:
    """Forward a service signal to the owned runtime group."""

    _signal_process_group(child.pid, signum)


def _spawn_watchdog(lock_fd: int, child_pgid: int) -> subprocess.Popen[object]:
    launcher = str(Path(__file__).resolve())
    return subprocess.Popen(
        [
            sys.executable or "python3",
            launcher,
            "--internal-watchdog",
            "--parent-pid",
            str(os.getpid()),
            "--child-pgid",
            str(child_pgid),
            "--lock-fd",
            str(lock_fd),
        ],
        close_fds=True,
        pass_fds=(lock_fd,),
        start_new_session=True,
    )


def _supervise(command: list[str], lock_fd: int) -> int:
    # The runtime has its own process group and never inherits the singleton fd.
    # Only the independent watchdog inherits that descriptor, so it can keep the
    # lock while draining a child group after an abrupt supervisor loss.
    child: subprocess.Popen[object] = subprocess.Popen(
        command,
        close_fds=True,
        start_new_session=True,
    )
    try:
        watchdog = _spawn_watchdog(lock_fd, child.pid)
    except BaseException:
        _drain_process_group(child.pid)
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise

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
        # A direct child can exit while descendants remain.  Drain the complete
        # owned group before this launcher can release its copy of the lock.
        _drain_process_group(child.pid)
        if child.poll() is None:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            watchdog.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Group drainage above is complete; a stuck watchdog is no longer
            # protecting live writers.  Terminate only that internal helper.
            watchdog.terminate()
            try:
                watchdog.wait(timeout=1)
            except subprocess.TimeoutExpired:
                watchdog.kill()
                watchdog.wait(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-owner launcher for the Polymarket PAPER runtime")
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

    _write_owner(fd, os.getpid())
    # The runtime command itself never gets this fd.  `_spawn_watchdog` passes a
    # duplicate only to the internal watchdog so ownership survives SIGKILL of
    # the launcher until its runtime process group is gone.
    os.set_inheritable(fd, False)
    try:
        return _supervise(command, fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-watchdog":
        raise SystemExit(_watchdog_main(sys.argv[2:]))
    raise SystemExit(main())
