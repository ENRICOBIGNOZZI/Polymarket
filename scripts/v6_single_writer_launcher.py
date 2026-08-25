#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path


def acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.set_inheritable(fd, True)
    return fd


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-writer launcher for stateful V6 paper workers")
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("--name", default="worker")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing worker command")

    fd = acquire_lock(args.lock)
    if fd is None:
        print(
            f"single_writer_skip name={args.name} lock={args.lock}",
            file=sys.stderr,
            flush=True,
        )
        return 0

    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
