"""Job locks keep the daily job off 28Hse while a backfill runs."""

import os

import pytest

from hkestate import locks


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))


def test_free_lock_is_taken_and_released():
    assert locks.held_by("hse28") is None
    with locks.hold("hse28"):
        assert locks.lock_path("hse28").exists()
    assert not locks.lock_path("hse28").exists()


def test_lock_held_by_a_live_process_is_busy():
    path = locks.lock_path("hse28")
    path.parent.mkdir(parents=True)
    path.write_text(str(os.getppid()))  # the test runner's parent is alive
    assert locks.held_by("hse28") == os.getppid()
    with pytest.raises(locks.LockBusy), locks.hold("hse28"):
        pass


def test_stale_lock_is_taken_over():
    path = locks.lock_path("hse28")
    path.parent.mkdir(parents=True)
    path.write_text("999999999")  # no such process
    assert locks.held_by("hse28") is None
    with locks.hold("hse28"):
        assert path.read_text().strip() == str(os.getpid())


def test_wait_while_held_waits_until_free_and_skips_stale_locks(monkeypatch):
    path = locks.lock_path("centanet")
    path.parent.mkdir(parents=True)
    path.write_text("999999999")  # stale: no wait
    waits, pauses = [], []
    assert locks.wait_while_held("centanet", wait=waits.append, on_pause=pauses.append) is False
    assert waits == [] and pauses == []
    alive = iter([True, True, True])  # live for three checks, then the job ends
    monkeypatch.setattr(locks, "pid_alive", lambda pid: next(alive, False))
    assert locks.wait_while_held("centanet", wait=waits.append, poll_s=5,
                                 on_pause=pauses.append) is True
    assert waits == [5, 5, 5] and pauses == [999999999]
