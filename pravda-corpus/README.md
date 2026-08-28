# pravda-corpus

Text and image corpus builder for pravda.sk (and its subdomains), for a
school NLP/multimodal-data project. Produces, per subdomain:

1. **Raw HTML** of articles, with their images
2. **Image corpus**: images paired with descriptions/captions/tags
3. **Text corpus**: clean extracted article text

## Ethics & rate limiting

This assignment explicitly requires that we not overload pravda.sk's
servers or risk an IP ban. Concretely, this pipeline:

- **Reads and respects `robots.txt`** at runtime for every host it talks to
  (via `urllib.robotparser`) - if a path is disallowed, the pipeline skips
  it rather than fetching anyway. See `src/utils/polite_client.py`.
- **Enforces a minimum delay between requests** to the same host (default
  3 seconds, configurable in `config/sources.py`), and honors the site's
  own `Crawl-delay` directive if it specifies something longer.
- **Uses official RSS feeds** for URL discovery rather than scraping
  listing pages - lighter weight, and exactly what RSS is designed for.
- **Identifies itself honestly** with a descriptive `User-Agent` (see
  `config/sources.py` - fill in real contact info before running this for
  real).
- **Runs single-threaded, sequentially** - no concurrent requests hammering
  the server from multiple directions at once.

If you need to scrape faster than this allows, that's a sign to ask
pravda.sk for permission or a data dump, not to loosen these settings.

## Pipeline stages

| Stage | Script | Input | Output |
|---|---|---|---|
| 1 | `src/discover_urls.py` | `config/sources.py` (RSS feed list) | `data/urls/<subdomain>.jsonl` |
| 2 | `src/download_html.py` | `data/urls/*.jsonl` | `data/raw_html/<subdomain>/{html,images}/` + `manifest.jsonl` |
| 3 | `src/extract_images.py` *(not yet built)* | `data/raw_html/` | `data/images/<subdomain>/` + metadata |
| 4 | `src/extract_text.py` *(not yet built)* | `data/raw_html/` | `data/text/<subdomain>/*.txt` + metadata |

### Stage 2 notes (`download_html.py`)

- For each discovered URL: saves the raw HTML, then downloads every image
  found inside the article's content area (not the whole page - logos, nav
  icons, and similar chrome are filtered out heuristically). See
  `CONTENT_SELECTORS` and the size/filename heuristics at the top of the
  script - these are a best-effort default since pravda.sk's real markup
  couldn't be inspected offline. **After the first real run, spot-check a
  few downloaded HTML files and their image sets by hand** and tighten the
  selectors/filters if they're pulling in the wrong things or missing real
  content photos.
- Output is sharded (`html/<2-hex-chars>/...`, `images/<2-hex-chars>/...`)
  to avoid the git/filesystem too-many-files-per-directory problem from the
  last project, from the start this time.
- Resumable via `manifest.jsonl`: any URL already in the manifest (success
  *or* failure) is skipped on rerun. To retry failed articles specifically,
  filter them out of the manifest first, e.g.:
  `jq 'select(.status=="ok")' manifest.jsonl > manifest.jsonl.tmp && mv manifest.jsonl.tmp manifest.jsonl`
- Uses `protego` (Scrapy's robots.txt library) instead of the stdlib
  `urllib.robotparser` - found while testing that stdlib silently allows a
  URL when a robots.txt has multiple separate `User-agent: *` blocks
  instead of one block with several directives. Real-world robots.txt files
  do this sometimes, so it wasn't worth the risk.
- Usage:
  ```bash
  python src/download_html.py                     # all subdomains
  python src/download_html.py --subdomain spravy   # just one
  python src/download_html.py --limit 20           # cap per subdomain, for testing
  ```

Each stage is resumable: rerunning a script picks up where it left off
rather than redoing completed work, and is safe to interrupt at any point.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python src/discover_urls.py
```

This is the current pilot scope: **recent articles only**, via RSS (which
naturally gives the most recent items per section - not a full historical
archive). Writes discovered article URLs to `data/urls/`.

Then, once you've confirmed `data/urls/` has real article URLs in it:

```bash
python src/download_html.py --subdomain spravy --limit 20   # pilot on one subdomain first
python src/download_html.py                                 # then everything
```

Stages 3-4 (image+caption corpus, extracted text corpus) aren't built yet.

## Data location

Actual scraped data (`data/`) is NOT committed to this repo (see
`.gitignore`) - it lives on the school server's filesystem where this is
run. Only source code and configuration are version-controlled.

## Subdomains covered

See `config/sources.py` for the current list and their RSS sources. Two
entries (`ekonomika`, `kultura`) use a guessed RSS URL pattern that wasn't
directly confirmed on pravda.sk's own RSS info page - `discover_urls.py`
logs a clear warning if a feed doesn't parse, so check the log on first run.
