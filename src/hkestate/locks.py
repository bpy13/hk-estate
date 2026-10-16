"""Job locks: a file holding the process id of the job that owns it.

`hse28` is held by anything that reads 28Hse for a long time (backfill, weekly update), so
the daily listing job leaves 28Hse alone while one runs (28Hse rate-limits hard) instead of
doubling the request rate. `daily` stops two daily jobs overlapping. A lock whose process has
ended is stale and is taken over.
"""

from __future__ import annotations

import ctypes
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from hkestate.config import get_data_dir


class LockBusy(Exception):
    """Another live process holds the lock."""


def lock_path(name: str) -> Path:
    return get_data_dir() / "locks" / f"{name}.lock"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows; ask for its exit code.
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def held_by(name: str) -> int | None:
    """Process id holding the lock, or None if free or stale."""
    path = lock_path(name)
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid != os.getpid() and pid_alive(pid) else None


@contextmanager
def hold(name: str):
    """Hold the lock for the duration of the block; LockBusy if a live process has it."""
    pid = held_by(name)
    if pid is not None:
        raise LockBusy(f"lock {name!r} is held by process {pid}")
    path = lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8").split()[0] == str(os.getpid()):
                path.unlink()
        except (OSError, IndexError):
            pass
