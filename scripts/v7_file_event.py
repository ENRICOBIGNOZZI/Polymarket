#!/usr/bin/env python3
"""Bounded file-replacement notifier; Linux inotify with portable timed fallback."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import select
import struct
import sys
import time

_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_MASK = _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE_SELF | _IN_MOVE_SELF
_EVENT = struct.Struct("iIII")


class AtomicReplaceWatcher:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = -1
        self.wd = -1
        self.mode = "TIMED_STAT_FALLBACK"
        self._identity = self._stat_identity()
        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1; init.argtypes = [ctypes.c_int]; init.restype = ctypes.c_int
            add = libc.inotify_add_watch; add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]; add.restype = ctypes.c_int
            fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd >= 0:
                wd = add(fd, os.fsencode(self.path.parent), _MASK)
                if wd >= 0:
                    self.fd, self.wd, self.mode = fd, wd, "LINUX_INOTIFY_ATOMIC_REPLACE"
                else:
                    os.close(fd)

    def _stat_identity(self) -> tuple[int, int, int, int] | None:
        try:
            value = self.path.stat()
            return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_size
        except OSError:
            return None

    def wait(self, timeout_seconds: float) -> bool:
        timeout = max(0.0, float(timeout_seconds))
        if self.fd < 0:
            if timeout:
                time.sleep(timeout)
            current = self._stat_identity()
            changed = current != self._identity
            self._identity = current
            return changed
        poller = select.poll(); poller.register(self.fd, select.POLLIN)
        ready = poller.poll(min(2_147_483_647, int(timeout * 1000 + .999)))
        if not ready:
            return False
        changed = False
        while True:
            try:
                payload = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            if not payload:
                break
            offset = 0
            while offset + _EVENT.size <= len(payload):
                wd, mask, _cookie, length = _EVENT.unpack_from(payload, offset)
                offset += _EVENT.size
                if offset + length > len(payload):
                    break
                raw = payload[offset:offset + length]; offset += length
                name = raw.split(b"\0", 1)[0]
                if wd == self.wd and name == os.fsencode(self.path.name) and mask & (_IN_MOVED_TO | _IN_CLOSE_WRITE | _IN_CREATE):
                    changed = True
        if changed:
            self._identity = self._stat_identity()
        return changed

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd); self.fd = -1

    def __enter__(self) -> "AtomicReplaceWatcher": return self
    def __exit__(self, exc_type, exc, tb) -> None: self.close()

class UnixDatagramJsonReceiver:
    """Local latest-signal receiver. The datagram carries data, not authority."""
    def __init__(self, path: Path, maximum_bytes: int = 64 * 1024):
        import socket
        self.socket_module = socket
        self.path = Path(path)
        self.maximum = int(maximum_bytes)
        if not 1024 <= self.maximum <= 1024 * 1024:
            raise ValueError("invalid datagram bound")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            mode = self.path.lstat().st_mode
            import stat
            if not stat.S_ISSOCK(mode):
                raise RuntimeError("signal socket path exists and is not a socket")
            self.path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        stat_value = self.path.stat()
        self._identity = stat_value.st_dev, stat_value.st_ino
        self.received = 0
        self.invalid = 0

    def wait_json(self, timeout_seconds: float):
        import json
        poller = select.poll(); poller.register(self.sock.fileno(), select.POLLIN)
        ready = poller.poll(min(2_147_483_647, int(max(0.0, timeout_seconds) * 1000 + .999)))
        if not ready:
            return None
        latest = None
        while True:
            try:
                payload = self.sock.recv(self.maximum + 1)
            except BlockingIOError:
                break
            if len(payload) > self.maximum:
                self.invalid += 1; continue
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.invalid += 1; continue
            if not isinstance(value, dict):
                self.invalid += 1; continue
            latest = value; self.received += 1
        return latest

    def close(self) -> None:
        if getattr(self, "sock", None) is not None:
            self.sock.close(); self.sock = None
        try:
            stat_value = self.path.stat()
            if (stat_value.st_dev, stat_value.st_ino) == self._identity:
                self.path.unlink()
        except OSError:
            pass
