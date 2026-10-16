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
