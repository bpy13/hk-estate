from pathlib import Path

from hkestate import config


def test_data_dir_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("HKESTATE_DATA_DIR", raising=False)
    repo_root = Path(__file__).resolve().parents[1]
    assert config.get_data_dir() == repo_root / "data"


def test_data_dir_overridable_by_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    assert config.get_data_dir() == tmp_path


def test_cache_and_lock_dirs_default_to_folders_in_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HKESTATE_CACHE_DIR", raising=False)
    monkeypatch.delenv("HKESTATE_LOCK_DIR", raising=False)
    assert config.get_cache_dir() == tmp_path / "cache"
    assert config.get_lock_dir() == tmp_path / "locks"


def test_cache_and_lock_dirs_overridable_by_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HKESTATE_CACHE_DIR", str(tmp_path / "live_cache"))
    monkeypatch.setenv("HKESTATE_LOCK_DIR", str(tmp_path / "live_locks"))
    assert config.get_cache_dir() == tmp_path / "live_cache"
    assert config.get_lock_dir() == tmp_path / "live_locks"
    assert config.get_data_dir() == tmp_path / "data"


def test_tests_never_point_at_the_real_data_folder():
    # conftest.py isolates every test; a log written by a test lands in a tmp folder
    assert config.get_data_dir() != config.REPO_ROOT / "data"


def test_module_level_data_dir_is_a_path():
    assert isinstance(config.DATA_DIR, Path)
