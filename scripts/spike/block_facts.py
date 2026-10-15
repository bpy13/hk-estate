"""Summarise Centadata block pages: blocks, completion, units, floors, 3-year deals, deal years.

Usage: python -I -X utf8 scripts/spike/block_facts.py URL [URL ...]
Fetches politely (counts toward the spike cap) and parses the visible text only.
"""

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import fetch, fetcher, text_of, title  # noqa: E402

with fetcher() as f:
    for url in sys.argv[1:]:
        html = fetch(f, "centadata", url, "transaction")
        if html is None:
            continue
        text = text_of(html, 10**7)
        blocks = re.findall(r'data-value="1-[A-Z]+"', html)
        facts = re.search(r"(\d{4}年\d{2}月) 入伙日期 ([\d,]+) 單位數目 (\d+) 層數", text)
        deals3 = re.search(r"過去3年共有(\d+)宗成交", text)
        years = collections.Counter(re.findall(r"\$[\d.,]+萬 (\d{4})", text))
        print(f"== {title(html)}")
        print(f"   blocks: {len(set(blocks))}; facts: {facts.groups() if facts else None}")
        print(f"   3-yr deals: {deals3.group(1) if deals3 else None}")
        print(f"   grid deals (each shown twice on the page): {sum(years.values())}")
        print(f"   grid deal years: {sorted(years.items())}")
