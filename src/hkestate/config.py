"""Project settings.

The data folder (database, raw-page cache, reports) is the one path most likely to move, so it
is set in a single place: the HKESTATE_DATA_DIR environment variable, or `<repo>/data` by
default. Everything in it is git-ignored.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Identifies us to the sites we read, so they can see who is fetching and why.
USER_AGENT = "hk-estate-research/0.1 (personal, non-commercial)"


def get_data_dir() -> Path:
    """Return the data folder, honouring HKESTATE_DATA_DIR when set."""
    override = os.environ.get("HKESTATE_DATA_DIR")
    return Path(override) if override else REPO_ROOT / "data"


def get_cache_dir() -> Path:
    """The saved-page folder: HKESTATE_CACHE_DIR when set, else `<data dir>/cache`.

    A test rebuild can write its own database but read the live page cache."""
    override = os.environ.get("HKESTATE_CACHE_DIR")
    return Path(override) if override else get_data_dir() / "cache"


def get_lock_dir() -> Path:
    """The job-lock folder: HKESTATE_LOCK_DIR when set, else `<data dir>/locks`.

    A test rebuild with its own data folder can still see the live jobs' locks."""
    override = os.environ.get("HKESTATE_LOCK_DIR")
    return Path(override) if override else get_data_dir() / "locks"


DATA_DIR = get_data_dir()
