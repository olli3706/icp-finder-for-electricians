"""
Warm the fetch cache from pages captured by a real browser.

Checkatrade's Cloudflare protection reliably serves a genuine browser but
challenges the automated one, especially on the /Search/ listing pages. When
the automated fetch is blocked, a person (or an assisting browser session)
can open the page in a normal browser and the captured markup is ingested
here, under the exact cache key the collectors use -- so a subsequent
cache-only run reproduces the CSVs with no network access at all.

Two inputs:
  listing  <anchor-town>  <file-with-one-slug-per-line>
  profile  <slug>         <file-with-page-html>

The stored form is byte-identical to what the collector would have cached,
so parse_profile and harvest_town read it transparently.
"""

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.cache import Cache
from src.collect_checkatrade import PROFILE_URL, SEARCH_URL


def ingest_profile(slug, html_path):
    html = Path(html_path).read_text(encoding="utf-8")
    Cache().put(PROFILE_URL.format(slug=slug), html)
    return len(html)


def ingest_listing(anchor, slugs_path):
    slugs = sorted({s.strip().lower() for s in
                    Path(slugs_path).read_text(encoding="utf-8").splitlines()
                    if s.strip() and not s.startswith("#")})
    key = SEARCH_URL.format(town=anchor) + "#harvest"
    Cache().put(key, json.dumps(
        {"anchor": anchor, "site_total": None, "slugs": slugs}), kind="listing")
    return len(slugs)


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: pane_ingest.py {listing|profile} <key> <file>")
    mode, key, path = sys.argv[1:4]
    if mode == "profile":
        n = ingest_profile(key, path)
        print(f"cached profile {key} ({n} bytes)")
    elif mode == "listing":
        n = ingest_listing(key, path)
        print(f"cached listing for {key} ({n} slugs)")
    else:
        sys.exit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
