"""Shared test setup.

Every test starts with the data, cache and lock folders pointed at a throw-away folder, so no
test can write into the real `data/` folder (logs, locks, pages) or see the live jobs' locks.
A test that needs its own folder sets the variables again with `monkeypatch`.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_folders(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("isolated_data")
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(root))
    monkeypatch.delenv("HKESTATE_CACHE_DIR", raising=False)
    monkeypatch.delenv("HKESTATE_LOCK_DIR", raising=False)
    # the request cap is process-wide; no test may leave one behind
    from hkestate import fetch

    fetch.set_request_cap(None)
    yield
    fetch.set_request_cap(None)
