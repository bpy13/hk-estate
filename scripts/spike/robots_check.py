"""Save each host's robots.txt under data/spike/robots/ and test specific URLs against it.

Usage: python -I scripts/spike/robots_check.py URL [URL ...]
"""

import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
from common import fetcher  # noqa: E402

from hkestate.config import get_data_dir  # noqa: E402

out = get_data_dir() / "spike" / "robots"
out.mkdir(parents=True, exist_ok=True)

with fetcher() as f:
    for url in sys.argv[1:]:
        host = urlsplit(url).hostname
        (out / f"{host}.txt").write_text(f.robots_txt(url), encoding="utf-8")
        print(f"{'ALLOW' if f.allowed(url) else 'DENY '} {url}")
