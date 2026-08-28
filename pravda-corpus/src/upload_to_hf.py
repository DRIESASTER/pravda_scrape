"""
src/upload_to_hf.py  (packaging/publishing, after stages 3-4)

Uploads the two corpora to HuggingFace as two separate datasets:

  1. IMAGES: image+caption/tags pairs, with the actual image bytes
     embedded (self-contained - users don't have to re-fetch anything
     from pravda.sk).
  2. TEXT: extracted article text, one row per article.

These are kept as separate datasets rather than one dataset with two
configs because they have genuinely different shapes and audiences -
image+caption pairs for multimodal work vs. plain text for language
modelling - and most downstream users want one or the other.

Uses push_to_hub() (Parquet under the hood) rather than a git-based
upload, which is what worked for the skwiki dataset after hitting
git file-count limits and dataset-viewer problems with many small files.

Usage:
  # log in first (once):  huggingface-cli login
  python src/upload_to_hf.py --images-repo driesaster/pravda-sk-images \
                             --text-repo driesaster/pravda-sk-text

  # dry run - build the datasets locally, print stats, upload nothing:
  python src/upload_to_hf.py --dry-run

  # only one of the two:
  python src/upload_to_hf.py --images-repo driesaster/pravda-sk-images --skip-text
"""

import argparse
import json
import os

from datasets import Dataset, Features, Image, Sequence, Value

IMAGE_CORPUS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "image_corpus")
TEXT_CORPUS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "text_corpus")
RAW_HTML_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw_html")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def read_jsonl_dir(directory):
    """Read every *.jsonl in a directory into one list of dicts."""
    if not os.path.isdir(directory):
        print(f"WARNING: {directory} does not exist - nothing to read")
        return []
    rows = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def build_image_dataset():
    """One row per image, with the image file itself embedded.

    image_path in the corpus is relative to data/ (e.g.
    "raw_html/spravy/images/b0/....png"), so resolve against DATA_DIR.
    Rows whose image file is missing on disk are skipped with a warning
    rather than crashing the whole upload - but the count is reported so
    a large number of them is visible rather than silent.
    """
    rows = read_jsonl_dir(IMAGE_CORPUS_DIR)
    if not rows:
        return None

    kept, missing = [], 0
    for row in rows:
        abs_path = os.path.normpath(os.path.join(DATA_DIR, row["image_path"]))
        if not os.path.exists(abs_path):
            missing += 1
            continue
        kept.append({
            "image": abs_path,          # cast to Image() below -> bytes embedded
            "caption": row.get("caption"),
            "caption_source": row.get("caption_source"),
            "tags": row.get("tags") or [],
            "subdomain": row.get("subdomain"),
            "article_url": row.get("article_url"),
            "article_title": row.get("article_title"),
            "published": row.get("published"),
            "section_label": row.get("section_label"),
            "is_sponsored": bool(row.get("is_sponsored", False)),
            "image_source": row.get("image_source"),
        })

    if missing:
        print(f"NOTE: skipped {missing} image row(s) whose file was missing on disk")

    features = Features({
        "image": Image(),
        "caption": Value("string"),
        "caption_source": Value("string"),
        "tags": Sequence(Value("string")),
        "subdomain": Value("string"),
        "article_url": Value("string"),
        "article_title": Value("string"),
        "published": Value("string"),
        "section_label": Value("string"),
        "is_sponsored": Value("bool"),
        "image_source": Value("string"),
    })
    return Dataset.from_list(kept, features=features)


def build_text_dataset():
    """One row per article."""
    rows = read_jsonl_dir(TEXT_CORPUS_DIR)
    if not rows:
        return None

    cleaned = [{
        "text": r.get("text"),
        "title": r.get("title"),
        "description": r.get("description"),
        "tags": r.get("tags") or [],
        "subdomain": r.get("subdomain"),
        "url": r.get("url"),
        "published": r.get("published"),
        "section_label": r.get("section_label"),
        "is_sponsored": bool(r.get("is_sponsored", False)),
        "n_chars": int(r.get("n_chars") or 0),
        "n_words": int(r.get("n_words") or 0),
        "quality_flag": r.get("quality_flag"),
    } for r in rows]

    features = Features({
        "text": Value("string"),
        "title": Value("string"),
        "description": Value("string"),
        "tags": Sequence(Value("string")),
        "subdomain": Value("string"),
        "url": Value("string"),
        "published": Value("string"),
        "section_label": Value("string"),
        "is_sponsored": Value("bool"),
        "n_chars": Value("int64"),
        "n_words": Value("int64"),
        "quality_flag": Value("string"),
    })
    return Dataset.from_list(cleaned, features=features)


def describe(name, ds):
    if ds is None:
        print(f"{name}: nothing to upload (no rows found)")
        return
    print(f"{name}: {len(ds)} rows")
    subs = {}
    for s in ds["subdomain"]:
        subs[s] = subs.get(s, 0) + 1
    print("  per subdomain:", ", ".join(f"{k}={v}" for k, v in sorted(subs.items())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--images-repo", default=None,
                         help="e.g. driesaster/pravda-sk-images")
    parser.add_argument("--text-repo", default=None,
                         help="e.g. driesaster/pravda-sk-text")
    parser.add_argument("--private", action="store_true",
                         help="Create/push the repos as private")
    parser.add_argument("--dry-run", action="store_true",
                         help="Build and summarize locally, upload nothing")
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--skip-text", action="store_true")
    args = parser.parse_args()

    if not args.skip_images:
        print("Building image dataset (this loads image files - may take a minute)...")
        img_ds = build_image_dataset()
        describe("IMAGES", img_ds)
        if img_ds is not None and not args.dry_run:
            if not args.images_repo:
                print("  --images-repo not given, skipping upload")
            else:
                print(f"  pushing to {args.images_repo} ...")
                img_ds.push_to_hub(args.images_repo, private=args.private)
                print("  done")

    if not args.skip_text:
        print("Building text dataset...")
        txt_ds = build_text_dataset()
        describe("TEXT", txt_ds)
        if txt_ds is not None and not args.dry_run:
            if not args.text_repo:
                print("  --text-repo not given, skipping upload")
            else:
                print(f"  pushing to {args.text_repo} ...")
                txt_ds.push_to_hub(args.text_repo, private=args.private)
                print("  done")

    if args.dry_run:
        print("\n(dry run - nothing uploaded)")


if __name__ == "__main__":
    main()
