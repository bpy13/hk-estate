"""Shared helpers for the Phase 0 source-spike probes (throwaway code).

Run from the repo root, e.g.:
    conda run -n hk-estate python -I scripts/spike/robots.py
Pages land in the git-ignored cache under data/. Nothing here executes page content.
"""

import re
import sys

from hkestate.fetch import Fetcher, FetchError, RobotsDisallowed, SourceBlocked

# Hard cap per source for the spike (decisions: at most ~50 pages per source).
PAGE_CAP = 50

_counts: dict[str, int] = {}


def fetcher() -> Fetcher:
    return Fetcher()


def fetch(f: Fetcher, source: str, url: str, page_type: str = "static") -> str | None:
    """Fetch one page for `source`, enforcing the spike cap. Returns text or None on failure."""
    _counts[source] = _counts.get(source, 0) + 1
    if _counts[source] > PAGE_CAP:
        sys.exit(f"{source}: page cap {PAGE_CAP} reached")
    try:
        return f.get(url, page_type)
    except RobotsDisallowed:
        print(f"  ROBOTS-DISALLOWED {url}")
    except SourceBlocked as e:
        print(f"  BLOCKED {url}: {e}")
    except FetchError as e:
        print(f"  ERROR {e.status} {url}")
    except Exception as e:  # network errors etc.
        print(f"  EXC {type(e).__name__} {url}: {e}")
    return None


def title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def text_of(html: str, limit: int = 600) -> str:
    """Visible text, squashed, for a quick look at what a page contains."""
    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser(html)
    for node in tree.css("script, style, noscript"):
        node.decompose()
    body = tree.body
    txt = body.text(separator=" ") if body else ""
    return re.sub(r"\s+", " ", txt).strip()[:limit]
