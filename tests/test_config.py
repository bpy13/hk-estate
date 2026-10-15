from pathlib import Path

from hkestate import config


def test_data_dir_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("HKESTATE_DATA_DIR", raising=False)
    repo_root = Path(__file__).resolve().parents[1]
    assert config.get_data_dir() == repo_root / "data"


def test_data_dir_overridable_by_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    assert config.get_data_dir() == tmp_path


def test_module_level_data_dir_is_a_path():
    assert isinstance(config.DATA_DIR, Path)
