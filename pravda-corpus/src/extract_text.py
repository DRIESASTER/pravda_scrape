"""
src/extract_text.py  (stage 4 of the pipeline)

Extracts clean article text from the raw HTML that download_html.py
(stage 2) saved. Pure local processing - no network requests - so it just
re-reads data/raw_html/<subdomain>/manifest.jsonl and regenerates its
output fresh each run.

Uses `trafilatura`, which is purpose-built for exactly this (pulling the
article body out of a news page while dropping nav, ads, comments,
related-article widgets, and boilerplate). Chosen over `docling` because
docling is aimed at PDFs/office documents rather than news HTML.

Trafilatura does its own boilerplate removal, independent of the
CONTENT_SELECTORS/NOISE_CLASS_SUBSTRINGS heuristics that stage 2 uses for
images. That's deliberate - they're solving different problems (stage 2
needs to know *which <img> tags* are content; this needs *the text*), and
trafilatura's model-based approach generalizes better across the 4
structurally different templates on this site than hand-written selectors
would. The `quality_flag` field below is how we catch cases where it
underperforms, rather than assuming it always works.

Output: one JSONL file per subdomain, data/text_corpus/<subdomain>.jsonl:
  {
    "subdomain": ...,
    "url": ...,
    "title": "<from data/urls/<subdomain>.jsonl>" | null,
    "published": "<from data/urls/<subdomain>.jsonl>" | null,
    "section_label": "<from data/urls/<subdomain>.jsonl>" | null,
    "description": "<article's meta description>" | null,
    "tags": ["...", ...],
    "is_sponsored": true | false,
    "text": "<extracted article body>" | null,
    "n_chars": <int>,
    "n_words": <int>,
    "quality_flag": null | "empty" | "very_short",
  }

Following the same flag-rather-than-drop approach used for the skwiki
dataset and stage 3's captions: articles where extraction produced
nothing or suspiciously little are KEPT with a quality_flag set, so a
downstream user can filter them out themselves rather than having that
decision silently baked in. "very_short" is a heuristic (< 200 chars) -
some legitimately short articles will be flagged; that's the intended
trade-off for not silently dropping data.

Usage:
  python src/extract_text.py                     # all subdomains
  python src/extract_text.py --subdomain spravy   # just one
"""

import argparse
import json
import os

import trafilatura

RAW_HTML_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw_html")
URLS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "urls")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "text_corpus")

VERY_SHORT_CHARS = 200


def load_url_metadata(subdomain):
    """url -> {"title", "published", "section_label"} from stage-1 output."""
    path = os.path.join(URLS_DIR, f"{subdomain}.jsonl")
    if not os.path.exists(path):
        print(f"[{subdomain}] NOTE: no {path} - title/published/section_label "
              f"will be null for all rows")
        return {}
    meta = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            meta[row["url"]] = {
                "title": row.get("title") or None,
                "published": row.get("published") or None,
                "section_label": row.get("section_label") or None,
            }
    return meta


def process_subdomain(subdomain):
    manifest_path = os.path.join(RAW_HTML_DIR, subdomain, "manifest.jsonl")
    if not os.path.exists(manifest_path):
        print(f"[{subdomain}] no {manifest_path} - run download_html.py first, skipping")
        return

    url_meta = load_url_metadata(subdomain)
    subdomain_root = os.path.join(RAW_HTML_DIR, subdomain)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{subdomain}.jsonl")

    n_total = 0
    n_ok = 0
    n_empty = 0
    n_short = 0
    total_words = 0

    with open(manifest_path, "r", encoding="utf-8") as f_in, \
            open(out_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            if not line.strip():
                continue
            article = json.loads(line)
            if article.get("status") != "ok" or not article.get("html_path"):
                continue

            html_file = os.path.join(subdomain_root, article["html_path"])
            if not os.path.exists(html_file):
                print(f"[{subdomain}] WARNING: missing {html_file} "
                      f"(manifest references it) - skipping")
                continue

            n_total += 1
            with open(html_file, "r", encoding="utf-8") as f:
                html = f.read()

            # no_fallback=False lets trafilatura fall back to its more
            # lenient extraction if the strict pass finds nothing - worth
            # it here since a few blog templates are unusual
            text = trafilatura.extract(
                html,
                include_comments=False,   # reader comments are not article text
                include_tables=True,      # varecha recipes use tables for ingredients
                no_fallback=False,
            )

            text = (text or "").strip() or None
            n_chars = len(text) if text else 0
            n_words = len(text.split()) if text else 0

            if not text:
                quality_flag = "empty"
                n_empty += 1
            elif n_chars < VERY_SHORT_CHARS:
                quality_flag = "very_short"
                n_short += 1
            else:
                quality_flag = None
                n_ok += 1
                total_words += n_words

            meta = url_meta.get(article["url"], {})
            row = {
                "subdomain": subdomain,
                "url": article["url"],
                "title": meta.get("title"),
                "published": meta.get("published"),
                "section_label": meta.get("section_label"),
                "description": article.get("description"),
                "tags": article.get("tags", []),
                "is_sponsored": article.get("is_sponsored", False),
                "text": text,
                "n_chars": n_chars,
                "n_words": n_words,
                "quality_flag": quality_flag,
            }
            f_out.write(json.dumps(row, ensure_ascii=False) + "\n")

    avg = (total_words / n_ok) if n_ok else 0
    print(f"[{subdomain}] {n_total} articles -> {n_ok} clean, "
          f"{n_short} very_short, {n_empty} empty "
          f"(avg {avg:.0f} words) -> {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subdomain", default=None,
                         help="Only process this subdomain (default: all with a manifest)")
    args = parser.parse_args()

    if args.subdomain:
        subdomains = [args.subdomain]
    else:
        subdomains = sorted(
            d for d in os.listdir(RAW_HTML_DIR)
            if os.path.isdir(os.path.join(RAW_HTML_DIR, d))
        ) if os.path.isdir(RAW_HTML_DIR) else []

    if not subdomains:
        print("No subdomains found under data/raw_html/ - run download_html.py first.")
        return

    for subdomain in subdomains:
        process_subdomain(subdomain)


if __name__ == "__main__":
    main()
