"""Find estate URLs by (Traditional Chinese) name in the cached Centanet estate sitemap.

Usage: python -I -X utf8 scripts/spike/find_estate.py NAME [NAME ...]
The zh_hk estate sitemap actually holds Simplified names; names are converted with OpenCC.
"""

import gzip
import hashlib
import re
import sys
from urllib.parse import quote

import opencc

from hkestate.config import get_data_dir

URL = "https://hk.centanet.com/sitemap-index/estate/sitemap_estate_zh_hk.xml"
sha = hashlib.sha1(URL.encode()).hexdigest()
path = sorted((get_data_dir() / "cache" / "hk.centanet.com").glob(f"*/{sha}.html.gz"))[-1]
locs = re.findall(r"<loc>([^<]+)</loc>", gzip.decompress(path.read_bytes()).decode("utf-8"))
t2s = opencc.OpenCC("t2s")
for name in sys.argv[1:]:
    simp = t2s.convert(name)
    hits = [u for u in locs if f"/estate/{simp}" in u or f"/estate/{name}" in u]
    for h in hits[:5]:
        code = h.rsplit("/", 1)[-1]
        print(name, code, f"https://hk.centanet.com/findproperty/centadata-details/{quote(name)}_{code}")
    if not hits:
        print(name, "-- not found")
