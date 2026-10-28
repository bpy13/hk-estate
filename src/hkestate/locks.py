"""Job locks: a file holding the process id of the job that owns it.

`hse28` is held by anything that reads 28Hse for a long time (backfill, weekly update), so
the daily listing job leaves 28Hse alone while one runs (28Hse rate-limits hard) instead of
doubling the request rate. `listings` stops two listing jobs overlapping. `centanet` is held
by the listing job only while it reads Centanet: a marker (other jobs on hk.centanet.com,
`cd-history` and the master pass, no longer pause for it since 2026-10-20; each keeps its
own slower pace). `cd-history` holds `cd-history` and `ricacorp` holds `ricacorp`, so
two copies cannot run (and the watchdog can tell they are alive). A lock whose process has
ended is stale and is taken over (and does not make anyone wait).
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from hkestate.config import get_lock_dir


class LockBusy(Exception):
    """Another live process holds the lock."""


def lock_path(name: str) -> Path:
    return get_lock_dir() / f"{name}.lock"


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


def wait_while_held(name: str, *, wait: Callable[[float], None] = time.sleep,
                    poll_s: float = 60.0, on_pause: Callable[[int], None] | None = None) -> bool:
    """Block while another live process holds the lock (a stale lock does not count).
    `on_pause(pid)` is called once when the wait starts. Returns True if it waited."""
    paused = False
    while (pid := held_by(name)) is not None:
        if not paused and on_pause is not None:
            on_pause(pid)
        paused = True
        wait(poll_s)
    return paused


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
