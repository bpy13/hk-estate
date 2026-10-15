"""Summarise href patterns (digits replaced by N) in a cached page. No fetching.

Usage: python -I -X utf8 scripts/spike/link_patterns.py URL [PREFIX_REGEX] [TOP]
"""

import collections
import gzip
import hashlib
import re
import sys
from urllib.parse import urlsplit

from hkestate.config import get_data_dir

url = sys.argv[1]
prefix = sys.argv[2] if len(sys.argv) > 2 else "."
top = int(sys.argv[3]) if len(sys.argv) > 3 else 15
sha = hashlib.sha1(url.encode()).hexdigest()
path = sorted((get_data_dir() / "cache" / urlsplit(url).hostname).glob(f"*/{sha}.html.gz"))[-1]
html = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
hrefs = [h for h in re.findall(r'href="([^"]+)"', html) if re.search(prefix, h)]
counts = collections.Counter(re.sub(r"[0-9]+", "N", h.split("?")[0]) for h in hrefs)
for pattern, n in counts.most_common(top):
    print(n, pattern)
examples = [h for h in hrefs if re.search(r"[0-9]{5,}", h)][:3]
print("examples:", examples)
