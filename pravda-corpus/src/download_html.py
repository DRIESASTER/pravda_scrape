"""
src/download_html.py  (stage 2 of the pipeline)

For each URL discovered by discover_urls.py (data/urls/<subdomain>.jsonl),
downloads the raw article HTML, plus every image referenced inside the
article's content area.

Output layout, per subdomain (under data/raw_html/<subdomain>/):
  manifest.jsonl          - one row per article (see below)
  html/<shard>/<hash>.html
  images/<shard>/<hash>_<n>.<ext>

<shard> is the first 2 hex chars of md5(url) - keeps any single directory
well under filesystem/git limits even at large scale (same fix used for
the fotkyzadarmo sharding).

Manifest row:
  {
    "url": ...,
    "subdomain": ...,
    "status": "ok" | "robots_disallowed" | "error",
    "html_path": "html/3f/3f9a1b2c....html"  (relative, or null),
    "description": "<article's og:description/description meta>" | null,
    "tags": ["<article:tag values>", ...],
    "is_sponsored": true | false,  # url path contains /inzercia/
    "images": [
      {"src": "<original image URL>", "local_path": "images/3f/..._0.jpg",
       "alt": "<img alt text>" | null,  # null for meta/og:image-sourced entries
       "source": "meta" | "inline"},  # meta = og:image/twitter:image, inline = <img> in body
      ...
    ],
    "fetched_at": "<ISO8601 UTC>",
    "error": "<message>" | null
  }

Resumable: any URL already present in the manifest (any status) is skipped
on rerun. To force a retry of failed articles, remove their lines from
manifest.jsonl (e.g. with jq) before rerunning - the manifest is a plain
JSONL file, so this is safe.

Usage:
  python src/download_html.py                     # all subdomains
  python src/download_html.py --subdomain spravy   # just one
  python src/download_html.py --limit 20           # cap per subdomain (testing)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config"))
from polite_client import PoliteClient, RobotsDisallowed  # noqa: E402
from sources import SOURCES  # noqa: E402

URLS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "urls")
OUT_ROOT = os.path.join(os.path.dirname(__file__), "..", "data", "raw_html")

# Try these containers in order; first one that matches anything wins.
# "div.boxSingle.post" is blog.pravda.sk's actual article container,
# confirmed from a real downloaded page. The rest are generic guesses for
# the other (non-blog) subdomains, since their markup wasn't verifiable
# from this sandbox - VERIFY against real downloaded HTML and tighten
# further once spravy/ekonomika/etc. pages have been pulled.
CONTENT_SELECTORS = [
    "div.boxSingle.post",
    "article",
    "div.article-content",
    "div.article__content",
    "div.article-body",
    "div#article-content",
    "main",
]

# Elements to strip out BEFORE looking for content images, regardless of
# which container above matched (or the whole-page fallback). Found by
# inspecting a real blog.pravda.sk page: the footer subscription widget
# ("Objednajte si predplatne") embeds a daily-newspaper-cover thumbnail
# from covers.digitania.eu that would otherwise get attached to every
# single article in the corpus as if it were real content - plus sidebar
# "about this blog" boxes, related-articles boxes, ad slots (adOcean), and
# comment threads, none of which are the article's own images.
NOISE_TAGS = {"footer", "header", "nav", "aside", "script", "style", "noscript"}
NOISE_CLASS_SUBSTRINGS = (
    "sidebar", "slidebar", "comment", "debata", "adocean", "advert",
    "dalsie_clanky", "related", "facebook-like", "fb-like", "app_links",
    "predplatne", "footer",
    # found by inspecting real spravy.pravda.sk articles: an inline "read
    # more" callout (a different, unrelated, sometimes years-old article)
    # embedded INSIDE the main content area rather than in a separate
    # sidebar - doesn't match "related" but is the same kind of noise -
    # and the byline author's headshot thumbnail, which is about the
    # journalist, not the story.
    "readmore", "author",
    # found on varecha.pravda.sk: a "you might also like" recipe-card
    # carousel wrapped in a div literally named "followingcontent" - its
    # cards use a lazy-load pattern where the real image sits in a
    # data-src on the PARENT <a>, not the <img> itself, so it wasn't
    # already caught by our data-src/data-original handling on <img>.
    "followingcontent",
    # found on sportweb.pravda.sk: an embedded "related photo gallery"
    # promo widget (class="detail_embed_gallery_...") that links out to a
    # DIFFERENT gallery article - confirmed the exact same image/gallery
    # embedded identically across 3 unrelated stories, same failure mode
    # as the "readmore" widget, different template.
    "embed_gallery",
)


def strip_noise(soup):
    for tag in soup.find_all(list(NOISE_TAGS)):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.parent is None:
            continue  # already detached by an earlier decompose() of an ancestor
        class_str = " ".join(tag.get("class") or []).lower()
        if any(sub in class_str for sub in NOISE_CLASS_SUBSTRINGS):
            tag.decompose()
    return soup

# Images smaller than this in EITHER declared HTML attribute (width/height,
# when present) are almost always icons/spacers, not content photos.
MIN_DECLARED_DIMENSION = 80

SKIP_SRC_PATTERNS = ("logo", "icon", "avatar", "sprite", "placeholder",
                      "digitania.eu",  # digitania.eu = the footer subscription-widget cover
                      "transparent-16x9")  # varecha.pravda.sk's lazy-load spacer/placeholder

MAX_IMAGE_BYTES = 25 * 1024 * 1024  # sanity cap; refuse to save absurd files

# prvd.sk's CDN serves the same underlying photo at multiple crops under a
# path segment like ".../<numeric id>/16x9-big/photo.jpg" - confirmed by
# comparing og:image vs. inline body <img> for several real articles: same
# numeric id, same filename, only this segment (and the cache-busting query
# string) differs. Deduping to one crop per photo avoids near-duplicate
# rows in the eventual caption dataset and saves a download per duplicate.
CROP_SEGMENT_RE = re.compile(r"/((?:\d+x\d+-(?:big|medium|small))|square-(?:mini|big|medium|small))/")
CROP_RANK = {"big": 3, "medium": 2, "small": 1, "mini": 1}


def canonical_photo_key(src):
    """URL with the crop-size path segment and query string stripped, so
    different crops of the same underlying photo collapse to one key."""
    path = urlparse(src).path
    return CROP_SEGMENT_RE.sub("/", path)


def crop_rank(src):
    """Higher = bigger/better crop, based on the CDN's own naming. Unknown
    or absent crop segment ranks lowest (0), so a normal (non-CDN, e.g.
    blog wp-content) image URL is never preferred away in favor of nothing."""
    path = urlparse(src).path
    m = CROP_SEGMENT_RE.search(path)
    if not m:
        return 0
    seg = m.group(1)
    for kw, rank in CROP_RANK.items():
        if kw in seg:
            return rank
    return 0


def dedupe_by_photo(candidates):
    """candidates: ordered list of {"src":..., "alt":..., "source":...} dicts.
    Keeps the best-crop candidate per underlying photo, preserving the
    original first-seen order."""
    best_for_key = {}     # canonical key -> (rank, candidate dict)
    first_seen_order = [] # canonical keys in first-seen order
    for cand in candidates:
        key = canonical_photo_key(cand["src"])
        rank = crop_rank(cand["src"])
        if key not in best_for_key:
            first_seen_order.append(key)
            best_for_key[key] = (rank, cand)
        elif rank > best_for_key[key][0]:
            best_for_key[key] = (rank, cand)
    return [best_for_key[key][1] for key in first_seen_order]


def url_hash(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def shard_paths(url, subdomain, kind):
    """kind: 'html' or 'images'. Returns (dir, stem) both relative to the
    subdomain's output root, and creates the directory."""
    h = url_hash(url)
    shard = h[:2]
    d = os.path.join(OUT_ROOT, subdomain, kind, shard)
    os.makedirs(d, exist_ok=True)
    return d, h


def load_manifest_done(path):
    if not os.path.exists(path):
        return set()
    done = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    done.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def append_manifest(path, row):
    # flush + fsync immediately after every single article - not batched -
    # so a hard crash/kill loses at most the one article in flight, never
    # more. This is the checkpointing lesson from the last project applied
    # from day one instead of bolted on after a crash.
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def find_meta_description(soup):
    """og:description, falling back to the plain description meta - used
    as the caption for a meta-sourced hero image, since og:image has no
    alt text of its own the way an inline <img> does."""
    tag = soup.find("meta", attrs={"property": "og:description"}) or \
        soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def find_article_tags(soup):
    """<meta property="article:tag" content="..."> - confirmed format from
    a real spravy.pravda.sk article: one meta tag per keyword, repeated."""
    return [t["content"].strip() for t in soup.find_all("meta", attrs={"property": "article:tag"})
            if t.get("content")]


def find_meta_image(soup, base_url):
    """og:image / twitter:image - the canonical 'this is the article's photo'
    signal most news CMSs populate for social-share cards. Needed because
    spravy.pravda.sk (and likely other non-blog subdomains) declare the
    article's hero photo ONLY here, not as an inline <img> in the body -
    confirmed by inspecting a real article where the only inline <img> for
    that same photo turned out to belong to an unrelated related-article
    teaser widget, not the article itself. Filtered through the same
    SKIP_SRC_PATTERNS as everything else, since on blog.pravda.sk this tag
    sometimes points at the post author's avatar rather than a real photo."""
    for prop in ("og:image", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            src = urljoin(base_url, tag["content"])
            if not any(p in src.lower() for p in SKIP_SRC_PATTERNS):
                return src
    return None


def find_content_images(soup, base_url):
    meta_image = find_meta_image(soup, base_url)

    strip_noise(soup)

    container = None
    for selector in CONTENT_SELECTORS:
        found = soup.select(selector)
        if found:
            container = found[0]
            break
    if container is None:
        container = soup  # fall back to (already noise-stripped) whole page

    candidates = []
    seen_src = set()

    if meta_image:
        seen_src.add(meta_image)
        candidates.append({"src": meta_image, "alt": None, "source": "meta"})

    for img in container.find_all("img"):
        # lazy-loaded images often carry the real URL in data-src/data-original
        # rather than src (which may hold a placeholder)
        src = img.get("data-src") or img.get("data-original") or img.get("src")
        if not src:
            continue
        src = urljoin(base_url, src)
        if src in seen_src:
            continue
        if any(p in src.lower() for p in SKIP_SRC_PATTERNS):
            continue

        w, h = img.get("width"), img.get("height")
        try:
            if w and int(float(w)) < MIN_DECLARED_DIMENSION:
                continue
            if h and int(float(h)) < MIN_DECLARED_DIMENSION:
                continue
        except ValueError:
            pass  # non-numeric width/height (e.g. "100%") - don't filter on it

        seen_src.add(src)
        alt = (img.get("alt") or "").strip() or None
        candidates.append({"src": src, "alt": alt, "source": "inline"})

    return dedupe_by_photo(candidates)


def ext_from_content_type(content_type, fallback_url):
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }
    if content_type:
        for k, v in mapping.items():
            if k in content_type:
                return v
    # fall back to whatever extension the URL itself has, if any
    path_ext = os.path.splitext(urlparse(fallback_url).path)[1]
    return path_ext if path_ext and len(path_ext) <= 5 else ".jpg"


def download_images(client, image_candidates, subdomain, article_url):
    results = []
    for idx, cand in enumerate(image_candidates):
        src = cand["src"]
        try:
            resp = client.get(src, stream=True)
        except RobotsDisallowed:
            print(f"    image SKIPPED (robots.txt): {src}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"    image FAILED ({e.__class__.__name__}): {src}", file=sys.stderr)
            continue

        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type and not os.path.splitext(urlparse(src).path)[1]:
            print(f"    skipping non-image response for {src}", file=sys.stderr)
            continue

        content = resp.content
        if len(content) > MAX_IMAGE_BYTES:
            print(f"    skipping oversized image ({len(content)} bytes): {src}",
                  file=sys.stderr)
            continue

        img_dir, stem = shard_paths(article_url, subdomain, "images")
        ext = ext_from_content_type(content_type, src)
        local_name = f"{stem}_{idx}{ext}"
        local_path = os.path.join(img_dir, local_name)
        with open(local_path, "wb") as f:
            f.write(content)

        rel_path = os.path.relpath(local_path, os.path.join(OUT_ROOT, subdomain))
        results.append({
            "src": src, "local_path": rel_path,
            "alt": cand["alt"], "source": cand["source"],
        })
    return results


def process_article(client, row, subdomain, manifest_path):
    url = row["url"]
    now = datetime.now(timezone.utc).isoformat()

    try:
        resp = client.get(url)
    except RobotsDisallowed as e:
        append_manifest(manifest_path, {
            "url": url, "subdomain": subdomain, "status": "robots_disallowed",
            "html_path": None, "description": None, "tags": [], "is_sponsored": False,
            "images": [], "fetched_at": now, "error": str(e),
        })
        print(f"  SKIPPED (robots.txt): {url}", file=sys.stderr)
        return
    except Exception as e:
        append_manifest(manifest_path, {
            "url": url, "subdomain": subdomain, "status": "error",
            "html_path": None, "description": None, "tags": [], "is_sponsored": False,
            "images": [], "fetched_at": now, "error": str(e),
        })
        print(f"  FAILED: {url} ({e})", file=sys.stderr)
        return

    html_dir, stem = shard_paths(url, subdomain, "html")
    html_path = os.path.join(html_dir, f"{stem}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(resp.text)
    rel_html_path = os.path.relpath(html_path, os.path.join(OUT_ROOT, subdomain))

    soup = BeautifulSoup(resp.text, "lxml")
    description = find_meta_description(soup)
    tags = find_article_tags(soup)
    image_candidates = find_content_images(soup, url)
    images = download_images(client, image_candidates, subdomain, url)

    is_sponsored = "/inzercia/" in url  # pravda.sk's advertorial/sponsored-content path

    append_manifest(manifest_path, {
        "url": url, "subdomain": subdomain, "status": "ok",
        "html_path": rel_html_path, "description": description, "tags": tags,
        "is_sponsored": is_sponsored, "images": images,
        "fetched_at": now, "error": None,
    })
    print(f"  OK: {url} ({len(images)} image(s))")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subdomain", default=None,
                         help="Only process this subdomain (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Max NEW articles to process per subdomain (for testing)")
    args = parser.parse_args()

    subdomains = [args.subdomain] if args.subdomain else list(SOURCES.keys())
    client = PoliteClient()

    for subdomain in subdomains:
        urls_path = os.path.join(URLS_DIR, f"{subdomain}.jsonl")
        if not os.path.exists(urls_path):
            print(f"[{subdomain}] no {urls_path} - run discover_urls.py first, skipping")
            continue

        with open(urls_path, "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]

        os.makedirs(os.path.join(OUT_ROOT, subdomain), exist_ok=True)
        manifest_path = os.path.join(OUT_ROOT, subdomain, "manifest.jsonl")
        done = load_manifest_done(manifest_path)

        todo = [r for r in rows if r["url"] not in done]
        if args.limit:
            todo = todo[:args.limit]

        print(f"[{subdomain}] {len(rows)} discovered, {len(done)} already done, "
              f"{len(todo)} to process this run")

        for i, row in enumerate(todo, 1):
            print(f"[{subdomain}] ({i}/{len(todo)}) {row['url']}")
            process_article(client, row, subdomain, manifest_path)

    print("Done.")


if __name__ == "__main__":
    main()
