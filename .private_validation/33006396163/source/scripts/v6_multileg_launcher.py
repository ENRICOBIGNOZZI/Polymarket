#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def _process_command(pid: int) -> str:
    result = subprocess.run(
        ["/bin/ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _process_ppid(pid: int) -> int | None:
    result = subprocess.run(
        ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return int(text) if text.isdigit() else None


def _process_cwd(pid: int) -> Path | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return Path(os.readlink(proc_cwd)).resolve()
        except OSError:
            return None

    lsof = shutil.which("lsof")
    if not lsof:
        return None
    result = subprocess.run(
        [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            try:
                return Path(line[1:]).resolve()
            except OSError:
                return None
    return None


def _is_descendant(pid: int, ancestor: int) -> bool:
    current = pid
    for _ in range(64):
        if current == ancestor:
            return True
        parent = _process_ppid(current)
        if parent is None or parent <= 0 or parent == current:
            return False
        current = parent
    return False


def _read_owner_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 64).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _safe_stale_owner(pid: int, runtime_parent_pid: int, repo_root: Path) -> bool:
    if _is_descendant(pid, runtime_parent_pid):
        return False
    command = _process_command(pid)
    if "polymarket_multileg_paper" not in command:
        return False
    cwd = _process_cwd(pid)
    return cwd == repo_root


def _recover_stale_owner(fd: int, lock: Path) -> bool:
    parent_text = os.environ.get("POLYMARKET_RUNTIME_PARENT_PID", "").strip()
    if not parent_text.isdigit() or int(parent_text) <= 0:
        return False
    runtime_parent_pid = int(parent_text)
    owner_pid = _read_owner_pid(fd)
    if owner_pid is None or not _pid_alive(owner_pid):
        return False

    repo_root = Path(__file__).resolve().parents[1]
    try:
        lock.resolve().relative_to(repo_root)
    except ValueError:
        return False
    if not _safe_stale_owner(owner_pid, runtime_parent_pid, repo_root):
        return False

    print(f"stale_v6_multileg_owner_reaped={owner_pid}", file=sys.stderr, flush=True)
    try:
        os.kill(owner_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(0.1)

    print(
        f"fatal: stale V6 multi-leg broker {owner_pid} did not release {lock}",
        file=sys.stderr,
        flush=True,
    )
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-writer launcher for V6 multi-leg paper broker")
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing broker command")
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not _recover_stale_owner(fd, args.lock):
            print(f"fatal: another V6 multi-leg broker already owns {args.lock}", file=sys.stderr, flush=True)
            os.close(fd)
            return 75
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.set_inheritable(fd, True)
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
