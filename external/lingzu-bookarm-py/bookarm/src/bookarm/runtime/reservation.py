"""Exclusive leases for autonomous workflow and physical-hardware control."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import threading
import time
from typing import Optional


class RuntimeReservation:
    """Prevent two SDK workflows from commanding the same hardware together."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner_thread: Optional[int] = None

    def acquire(self, purpose: str) -> bool:
        purpose = None
        thread_id = threading.get_ident()
        with self._lock:
            if self._owner_thread not in (None, thread_id):
                return False
            self._owner_thread = thread_id
            return True

    def release(self) -> None:
        with self._lock:
            if self._owner_thread == threading.get_ident():
                self._owner_thread = None


class HardwareProcessLock:
    """Non-blocking OS lease preventing two BookArm daemons owning hardware.

    ``RuntimeReservation`` protects concurrent workflows inside one process.
    This lock is acquired before any CAN, serial or camera service starts, so
    a second daemon (or a stale launch script) fails clearly instead of racing
    on the same physical devices.  Kernel-managed ``flock`` releases the lease
    automatically when a crashed process exits.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file: Optional[object] = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            owner = self._read_owner(handle)
            handle.close()
            detail = ""
            if owner:
                detail = " (current owner: %s)" % owner
            raise RuntimeError(
                "bookarm hardware is already reserved by another process%s" % detail
            ) from error
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(
                {"pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            self._file = handle
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def _read_owner(handle: object) -> str:
        try:
            handle.seek(0)
            raw = handle.read()
            value = json.loads(raw) if raw else {}
            if isinstance(value, dict):
                return ", ".join(
                    "%s=%s" % (key, value[key])
                    for key in ("pid", "started_at")
                    if value.get(key) is not None
                )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return ""
