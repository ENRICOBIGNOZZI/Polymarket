#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path


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
        print(f"fatal: another V6 multi-leg broker already owns {args.lock}", file=sys.stderr, flush=True)
        os.close(fd)
        return 75
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.set_inheritable(fd, True)
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
