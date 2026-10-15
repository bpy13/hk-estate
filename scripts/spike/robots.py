"""Print robots.txt for every candidate source host (one request per host)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import fetcher  # noqa: E402

HOSTS = [
    "https://hk.centadata.com",
    "https://www.centadata.com",
    "https://hk.centanet.com",
    "https://www.28hse.com",
    "https://www.squarefoot.com.hk",
    "https://www.spacious.hk",
    "https://www.house730.com",
    "https://www.midland.com.hk",
    "https://www.srpe.gov.hk",
    "https://www.rvd.gov.hk",
    "https://data.gov.hk",
    "https://www.als.gov.hk",
    "https://portal.csdi.gov.hk",
]

with fetcher() as f:
    for host in HOSTS:
        print(f"===== {host}")
        try:
            text = f.robots_txt(host + "/")
            print(f"status {f.robots_status(host + '/')}; {len(text)} chars")
            print(text[-2500:] if text.lstrip().startswith(("User", "#", "Sitemap")) else "")
        except Exception as e:
            print(f"  EXC {type(e).__name__}: {e}")
