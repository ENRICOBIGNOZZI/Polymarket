#!/usr/bin/env python3
"""Fail-closed single-owner launcher for the paper runtime.

The lock file descriptor is deliberately inherited across exec, so ownership is
held for the full lifetime of the launched runtime rather than only while this
small launcher process exists.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path


def _current_owner(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 128).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


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
    os.set_inheritable(fd, True)
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
