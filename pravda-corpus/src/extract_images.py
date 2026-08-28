"""
src/extract_images.py  (stage 3 of the pipeline)

Assembles a clean, flat image+caption/tags corpus from what download_html.py
(stage 2) already collected. Pure local file processing - no network
requests, so no rate-limiting/resumability concerns; it just re-reads
data/raw_html/<subdomain>/manifest.jsonl each time and regenerates its
output fresh. Doesn't copy or move any image bytes - the corpus rows
reference the images already saved under data/raw_html/<subdomain>/images/,
to avoid duplicating storage. A later "build_hf_dataset.py" (not part of
this stage) would be the place to physically arrange images + metadata
into a HuggingFace-upload-ready layout, mirroring how the fotkyzadarmo
pipeline split extraction from HF packaging into separate scripts.

Caption logic, mirroring the skwiki dataset's "flag rather than drop"
approach (a quality_flag column let downstream users filter, instead of
silently dropping rows with iffy data):
  - image came from an inline <img> with real alt text -> caption = alt text,
    caption_source = "alt"
  - image came from og:image/twitter:image (no alt of its own) -> caption =
    the article's og:description/description meta, caption_source =
    "article_description"
  - neither available -> caption = null, caption_source = "none" (row is
    still included, not dropped - a downstream user may still want the
    image, or may want to filter these out themselves)

Output: one JSONL file per subdomain, data/image_corpus/<subdomain>.jsonl,
one row per image:
  {
    "subdomain": ...,
    "article_url": ...,
    "article_title": "<from data/urls/<subdomain>.jsonl>" | null,
    "published": "<from data/urls/<subdomain>.jsonl>" | null,
    "section_label": "<from data/urls/<subdomain>.jsonl>" | null,
    "tags": ["...", ...],
    "is_sponsored": true | false,
    "image_path": "raw_html/<subdomain>/images/<shard>/<hash>_n.ext",  (relative to data/)
    "image_source": "meta" | "inline",
    "caption": "..." | null,
    "caption_source": "alt" | "article_description" | "none",
  }

Usage:
  python src/extract_images.py                     # all subdomains
  python src/extract_images.py --subdomain spravy   # just one
"""

import argparse
import json
import os

RAW_HTML_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw_html")
URLS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "urls")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "image_corpus")


def load_url_metadata(subdomain):
    """url -> {"title":..., "published":..., "section_label":...}, from the
    stage-1 discovery output. Missing file / missing url -> caller gets
    None for each field rather than crashing - this metadata is a nice-to
    -have enrichment, not something stage 3 should hard-depend on."""
    path = os.path.join(URLS_DIR, f"{subdomain}.jsonl")
    if not os.path.exists(path):
        print(f"[{subdomain}] NOTE: no {path} - article_title/published/"
              f"section_label will be null for all rows")
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

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{subdomain}.jsonl")

    n_articles = 0
    n_images = 0
    n_captioned = 0

    with open(manifest_path, "r", encoding="utf-8") as f_in, \
            open(out_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            if not line.strip():
                continue
            article = json.loads(line)
            if article.get("status") != "ok" or not article.get("images"):
                continue
            n_articles += 1

            meta = url_meta.get(article["url"], {})
            description = article.get("description")

            for img in article["images"]:
                if img["source"] == "inline" and img.get("alt"):
                    caption, caption_source = img["alt"], "alt"
                elif img["source"] == "meta" and description:
                    caption, caption_source = description, "article_description"
                else:
                    caption, caption_source = None, "none"

                if caption_source != "none":
                    n_captioned += 1
                n_images += 1

                row = {
                    "subdomain": subdomain,
                    "article_url": article["url"],
                    "article_title": meta.get("title"),
                    "published": meta.get("published"),
                    "section_label": meta.get("section_label"),
                    "tags": article.get("tags", []),
                    "is_sponsored": article.get("is_sponsored", False),
                    "image_path": f"raw_html/{subdomain}/{img['local_path']}",
                    "image_source": img["source"],
                    "caption": caption,
                    "caption_source": caption_source,
                }
                f_out.write(json.dumps(row, ensure_ascii=False) + "\n")

    pct = (n_captioned / n_images * 100) if n_images else 0
    print(f"[{subdomain}] {n_articles} articles, {n_images} images "
          f"({n_captioned} captioned, {pct:.0f}%) -> {out_path}")


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
