"""
src/discover_urls.py  (stage 1 of the pipeline)

For each subdomain in config/sources.py, fetches its RSS feed(s) and writes
out the list of article URLs found - this is the "recent articles" pilot
scope, since RSS feeds give the most recent N items per section by design.

Output: data/urls/<subdomain>.jsonl, one row per article:
  {"url": ..., "title": ..., "published": ..., "section_label": ..., "subdomain": ...}

Safe to rerun: dedupes against what's already in each output file, so
running this again later just adds newly-published articles rather than
duplicating everything.

Usage: python src/discover_urls.py
"""

import json
import os
import sys

import feedparser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config"))
from polite_client import PoliteClient, RobotsDisallowed  # noqa: E402
from sources import SOURCES  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "urls")


def load_existing_urls(path):
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {json.loads(line)["url"] for line in f if line.strip()}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    client = PoliteClient()

    total_new = 0
    for subdomain, feeds in SOURCES.items():
        out_path = os.path.join(OUT_DIR, f"{subdomain}.jsonl")
        existing = load_existing_urls(out_path)
        new_rows = []

        for section_label, feed_url in feeds:
            print(f"[{subdomain}/{section_label}] fetching {feed_url}")
            try:
                resp = client.get(feed_url)
            except RobotsDisallowed as e:
                print(f"  SKIPPED (robots.txt disallows this): {e}", file=sys.stderr)
                continue
            except Exception as e:
                print(f"  FAILED: {e}", file=sys.stderr)
                continue

            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                print(f"  WARNING: feed didn't parse cleanly and has no entries "
                      f"(possibly a wrong/guessed URL - worth checking by hand): "
                      f"{parsed.bozo_exception}", file=sys.stderr)
                continue

            found = 0
            for entry in parsed.entries:
                url = entry.get("link")
                if not url or url in existing:
                    continue
                new_rows.append({
                    "url": url,
                    "title": entry.get("title", ""),
                    "published": entry.get("published", ""),
                    "section_label": section_label,
                    "subdomain": subdomain,
                })
                existing.add(url)
                found += 1
            print(f"  {found} new article(s) found ({len(parsed.entries)} in feed total)")

        if new_rows:
            with open(out_path, "a", encoding="utf-8") as f:
                for row in new_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        total_new += len(new_rows)
        print(f"[{subdomain}] {len(new_rows)} new URLs written to {out_path}\n")

    print(f"Done. {total_new} new article URLs discovered across "
          f"{len(SOURCES)} subdomains.")


if __name__ == "__main__":
    main()
