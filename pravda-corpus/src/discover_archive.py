"""
src/discover_archive.py  (stage 1b - archive backfill)

Companion to discover_urls.py. Where that reads RSS feeds (which only
expose ~20-30 recent items), this walks the human-facing listing pages
via their ?strana=N pagination to reach the archive.

Writes to the SAME data/urls/<subdomain>.jsonl files in the SAME format,
deduping against whatever is already there - so download_html.py,
extract_images.py and extract_text.py need no changes at all; they just
see more URLs.

Verified against the live site before writing this:
  - listing pages 301-redirect, so redirects must be followed
  - ?strana=N is the real pagination parameter
  - pagination goes genuinely deep (page 1000 of spravy/domace reaches
    April 2022; link sets at pages 100/300/500/1000 all confirmed
    distinct, i.e. the site is not silently clamping out-of-range pages)
  - listing pages also carry a "latest news" sidebar that repeats the
    same recent articles on every page - so per-page link extraction is
    deliberately deduped globally, and an all-duplicates page is treated
    as a signal we've hit the end rather than as new content

Politeness: uses the same PoliteClient as every other stage (robots.txt
respected, >=3s between requests to the same host, single-threaded).
At 3s/request, ~300 pages x 12 sections is roughly 3 hours JUST for this
discovery step - hence --max-pages, and hence starting small.

Resumability: URLs already present in data/urls/<subdomain>.jsonl are
skipped, and each section's progress is checkpointed to
data/urls/.archive_progress.json, so an interrupted run resumes at the
page it left off rather than restarting from page 1.

Usage:
  # small test first - 3 pages per section
  python src/discover_archive.py --max-pages 3

  # one section only
  python src/discover_archive.py --subdomain spravy --max-pages 5

  # the big run (use tmux)
  python src/discover_archive.py --max-pages 1000
"""

import argparse
import json
import os
import re
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config"))
from polite_client import PoliteClient, RobotsDisallowed  # noqa: E402
from sources import ARCHIVE_SOURCES  # noqa: E402

URLS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "urls")
PROGRESS_PATH = os.path.join(URLS_DIR, ".archive_progress.json")

# article URLs look like .../clanok/2029597-some-slug
ARTICLE_HREF_RE = re.compile(r"/clanok/\d+")

# If a whole page yields no URLs we haven't already seen, that's very
# likely the end of the archive (or a repeat of sidebar-only content).
# Require a few consecutive such pages before stopping, so one odd page
# doesn't cut a section short.
EMPTY_PAGES_BEFORE_STOP = 3


def load_progress():
    if not os.path.exists(PROGRESS_PATH):
        return {}
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("NOTE: could not read archive progress file, starting fresh")
        return {}


def save_progress(progress):
    os.makedirs(URLS_DIR, exist_ok=True)
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PROGRESS_PATH)  # atomic, so an interrupt can't corrupt it


def load_known_urls(subdomain):
    path = os.path.join(URLS_DIR, f"{subdomain}.jsonl")
    known = set()
    if not os.path.exists(path):
        return known
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    known.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return known


def append_urls(subdomain, rows):
    os.makedirs(URLS_DIR, exist_ok=True)
    path = os.path.join(URLS_DIR, f"{subdomain}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def extract_article_urls(html, base_url):
    """All distinct /clanok/<id> links on a listing page, normalised.

    Strips query strings and fragments so the same article linked with
    different tracking params collapses to one URL - important because
    the RSS-sourced URLs carry ?utm_source=... and we don't want the same
    article stored twice under two spellings.
    """
    soup = BeautifulSoup(html, "lxml")
    found = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not ARTICLE_HREF_RE.search(href):
            continue
        if href.startswith("/"):
            href = "https://www.pravda.sk" + href
        href = href.split("?")[0].split("#")[0]
        if href in seen:
            continue
        seen.add(href)
        found.append(href)
    return found


def canonical(url):
    """Match the normalisation used above, so dedup against existing
    RSS-discovered URLs (which have ?utm_source=... suffixes) works."""
    return url.split("?")[0].split("#")[0]


def process_section(client, subdomain, section_label, base_url,
                     max_pages, start_page, known_canon, progress):
    new_total = 0
    consecutive_empty = 0
    identical_pages = 0
    prev_fingerprint = None
    page = start_page

    while page <= max_pages:
        sep = "&" if "?" in base_url else "?"
        page_url = f"{base_url}{sep}strana={page}"

        try:
            resp = client.get(page_url)
        except RobotsDisallowed:
            print(f"  [{subdomain}/{section_label}] page {page} disallowed by robots.txt, "
                  f"stopping this section")
            break
        except Exception as e:
            print(f"  [{subdomain}/{section_label}] page {page} FAILED ({e}), "
                  f"stopping this section")
            break

        urls = extract_article_urls(resp.text, page_url)
        fresh = [u for u in urls if canonical(u) not in known_canon]

        # Detect pagination that silently no-ops (some pravda.sk sections
        # redirect in a way that drops the ?strana=N query string, so every
        # page serves page 1). Without this check that looks identical to
        # "archive exhausted" and the section stops early with no warning -
        # which is exactly what happened for ekonomika/sportweb on the
        # first real run. Comparing consecutive pages' link sets makes the
        # difference visible.
        page_fingerprint = hash(tuple(sorted(urls)))
        if page > start_page and page_fingerprint == prev_fingerprint:
            identical_pages += 1
        else:
            identical_pages = 0
        prev_fingerprint = page_fingerprint

        if identical_pages >= 2:
            print(f"  [{subdomain}/{section_label}] WARNING: pages {page - 2}-{page} "
                  f"returned IDENTICAL link sets - pagination appears to be a no-op "
                  f"for this section (the ?strana=N parameter is probably being "
                  f"dropped by a redirect). Stopping; this section's base URL in "
                  f"ARCHIVE_SOURCES likely needs to be the post-redirect URL.")
            break

        if fresh:
            rows = [{
                "url": u,
                "title": None,        # listing pages give unreliable titles;
                "published": None,    # stage 2 reads both from the article itself
                "section_label": section_label,
                "subdomain": subdomain,
                "discovered_via": "archive",
            } for u in fresh]
            append_urls(subdomain, rows)
            for u in fresh:
                known_canon.add(canonical(u))
            new_total += len(fresh)
            consecutive_empty = 0
        else:
            consecutive_empty += 1

        # checkpoint after every page, so an interrupt resumes here
        progress[f"{subdomain}/{section_label}"] = page
        save_progress(progress)

        if page % 25 == 0 or fresh:
            print(f"  [{subdomain}/{section_label}] page {page}: "
                  f"{len(urls)} links, {len(fresh)} new (running total {new_total})")

        if consecutive_empty >= EMPTY_PAGES_BEFORE_STOP:
            print(f"  [{subdomain}/{section_label}] {EMPTY_PAGES_BEFORE_STOP} consecutive "
                  f"pages with nothing new - assuming end of archive, stopping")
            break

        page += 1

    return new_total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subdomain", default=None,
                         help="Only this subdomain (default: all in ARCHIVE_SOURCES)")
    parser.add_argument("--max-pages", type=int, default=3,
                         help="Max listing pages per section (default: 3, deliberately "
                              "small - raise it for the real run)")
    parser.add_argument("--restart", action="store_true",
                         help="Ignore saved progress and start each section at page 1")
    args = parser.parse_args()

    subdomains = [args.subdomain] if args.subdomain else list(ARCHIVE_SOURCES.keys())
    progress = {} if args.restart else load_progress()
    client = PoliteClient()

    grand_total = 0
    for subdomain in subdomains:
        sections = ARCHIVE_SOURCES.get(subdomain)
        if not sections:
            print(f"[{subdomain}] not in ARCHIVE_SOURCES (no paginated listing), skipping")
            continue

        known_canon = {canonical(u) for u in load_known_urls(subdomain)}
        print(f"[{subdomain}] {len(known_canon)} URLs already known")

        for section_label, base_url in sections:
            key = f"{subdomain}/{section_label}"
            start_page = progress.get(key, 0) + 1
            if start_page > args.max_pages:
                print(f"  [{key}] already past page {args.max_pages} "
                      f"(resume point {start_page}), skipping")
                continue
            if start_page > 1:
                print(f"  [{key}] resuming at page {start_page}")
            n = process_section(client, subdomain, section_label, base_url,
                                 args.max_pages, start_page, known_canon, progress)
            print(f"  [{key}] {n} new URLs")
            grand_total += n

    print(f"Done. {grand_total} new article URLs discovered from archive pagination.")


if __name__ == "__main__":
    main()