"""Print regex matches (with context) from a cached page, without fetching anything.

Usage: python -I -X utf8 scripts/spike/inspect_cache.py URL REGEX [CONTEXT] [MAX]
Reads the newest cached copy of URL from data/cache. Page text is treated as plain data.
"""

import gzip
import hashlib
import re
import sys
from urllib.parse import urlsplit

from hkestate.config import get_data_dir

url, pattern = sys.argv[1], sys.argv[2]
ctx = int(sys.argv[3]) if len(sys.argv) > 3 else 150
limit = int(sys.argv[4]) if len(sys.argv) > 4 else 5
sha = hashlib.sha1(url.encode()).hexdigest()
hits = sorted((get_data_dir() / "cache" / urlsplit(url).hostname).glob(f"*/{sha}.html.gz"))
if not hits:
    sys.exit("not cached")
html = gzip.decompress(hits[-1].read_bytes()).decode("utf-8", errors="replace")
ms = list(re.finditer(pattern, html))
print(f"{len(ms)} matches in {len(html)} chars")
for m in ms[:limit]:
    s = html[max(0, m.start() - ctx) : m.end() + ctx]
    print("----", m.start())
    print(s)
