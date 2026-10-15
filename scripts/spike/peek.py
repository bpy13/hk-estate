"""Fetch one or more URLs politely and print a short summary of each.

Usage: python -I scripts/spike/peek.py SOURCE PAGE_TYPE URL [URL ...] [--links PATTERN] [--text N]
Prints status, title, size, visible-text excerpt, and (optionally) links matching a regex.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import fetch, fetcher, text_of, title  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("source")
p.add_argument("page_type")
p.add_argument("urls", nargs="+")
p.add_argument("--links", default=None, help="regex for hrefs to list")
p.add_argument("--grep", default=None, help="regex to search in raw HTML, prints matches")
p.add_argument("--text", type=int, default=500)
args = p.parse_args()

with fetcher() as f:
    for url in args.urls:
        html = fetch(f, args.source, url, args.page_type)
        print(f"===== {url}")
        if html is None:
            continue
        print(f"title: {title(html)!r}  size: {len(html)}")
        if args.text:
            print("text:", text_of(html, args.text))
        if args.links:
            hrefs = sorted(set(re.findall(r'href="([^"]+)"', html)))
            hits = [h for h in hrefs if re.search(args.links, h)]
            print(f"links matching {args.links!r}: {len(hits)}")
            for h in hits[:40]:
                print("  ", h)
        if args.grep:
            hits = re.findall(args.grep, html)
            print(f"grep {args.grep!r}: {len(hits)} hits")
            for h in hits[:20]:
                print("  ", str(h)[:200])
