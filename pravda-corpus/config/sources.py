"""
config/sources.py

Maps each subdomain we're targeting to its official RSS feed(s), discovered
from https://www.pravda.sk/info/rss-info - using first-party RSS instead of
scraping listing pages: it's lightweight (less load on their server, which
matters given we've been asked not to risk an IP ban), stable (won't break
if they redesign the site), and gives us exactly what we want for a "recent
articles" pilot without needing to guess pagination.

Each entry: subdomain name -> list of (section_label, feed_url) tuples.
`ekonomika` and `kultura` weren't listed on the RSS info page directly, but
follow the same URL pattern as every other section there, so they're
included as a guess - discover_urls.py logs clearly if a guessed feed 404s
rather than failing silently.
"""

SOURCES = {
    "spravy": [
        ("vsetky", "https://www.pravda.sk/spravy/rss/xml"),
        ("domace", "https://www.pravda.sk/spravy/domace/rss/xml"),
        ("svet", "https://www.pravda.sk/spravy/svet/rss/xml"),
        ("regiony", "https://www.pravda.sk/spravy/regiony/rss/xml"),
    ],
    "ekonomika": [
        ("vsetky", "https://www.pravda.sk/ekonomika/rss/xml"),  # guessed, verify
    ],
    "kultura": [
        ("vsetky", "https://www.pravda.sk/kultura/rss/xml"),  # guessed, verify
    ],
    "nazory": [
        ("vsetky", "https://www.pravda.sk/nazory/rss/xml"),
    ],
    "vat": [
        ("vsetky", "https://www.pravda.sk/vat/rss/xml"),
    ],
    "varecha": [
        ("recepty", "https://varecha.pravda.sk/rss/recepty.rss"),
        ("magazin", "https://varecha.pravda.sk/rss/magazin.rss"),
    ],
    "blog": [
        ("vsetky", "https://blog.pravda.sk/wp-content/recent-global-posts-feed.php"),
    ],
    # these are www.pravda.sk PATHS, not separate subdomains, but the task
    # asks for images+text corpus coverage broadly, so keeping them as
    # their own logical "sources" in the output even though the hostname
    # is shared
    "sportweb": [("vsetky", "https://www.pravda.sk/sportweb/rss/xml")],
    "koktail": [("vsetky", "https://www.pravda.sk/koktail/rss/xml")],
    "auto": [("vsetky", "https://www.pravda.sk/auto/rss/xml")],
    "uzitocna": [("vsetky", "https://www.pravda.sk/uzitocna/rss/xml")],
    "cestovanie": [("vsetky", "https://www.pravda.sk/cestovanie/rss/xml")],
    "zdravie": [("vsetky", "https://www.pravda.sk/zdravie/rss/xml")],
    "zena": [("vsetky", "https://www.pravda.sk/zena/rss/xml")],
}

# ---------------------------------------------------------------------------
# Archive (pagination) sources, used by discover_archive.py
# ---------------------------------------------------------------------------
# RSS only exposes a rolling window of ~20-30 recent items per feed, so it
# can't reach the archive. These are the human-facing listing pages, which
# DO paginate via ?strana=N. Verified against the live site: listing pages
# 301-redirect (so requests must follow redirects), return real article
# links, and paginate genuinely deep - page 1000 of spravy/domace reaches
# April 2022, with each page's link set confirmed distinct (checked by
# hashing the extracted link sets at pages 100/300/500/1000).
#
# Each entry: subdomain name -> list of (section_label, listing_url_base).
# discover_archive.py appends "?strana=N".
#
# NOT included: `blog` (blog.pravda.sk is a WordPress multi-site network
# with per-author subdomains, no single paginated index) and `varecha`
# (different platform/URL scheme). Both would need their own discovery
# logic; RSS still covers their recent content. Worth revisiting if the
# archive backfill needs to cover them too.
ARCHIVE_SOURCES = {
    "spravy": [
        ("domace", "https://www.pravda.sk/spravy/domace/"),
        ("svet", "https://www.pravda.sk/spravy/svet/"),
        ("regiony", "https://www.pravda.sk/spravy/regiony/"),
    ],
    # ekonomika and kultura redirect from /ekonomika/ and /kultura/ to
    # /spravy/... AND drop the query string in the process, so ?strana=N
    # was silently serving page 1 every time (kultura additionally 404s on
    # the pre-redirect form). Using the post-redirect URLs directly;
    # verified both paginate correctly (page 2 vs page 12 link sets differ).
    "ekonomika": [("vsetky", "https://www.pravda.sk/spravy/ekonomika")],
    "kultura": [("vsetky", "https://www.pravda.sk/spravy/kultura")],
    "nazory": [("vsetky", "https://www.pravda.sk/nazory/")],
    "vat": [("vsetky", "https://www.pravda.sk/vat/")],
    # /sportweb is a landing/aggregator page with no pagination of its own -
    # the real paginated listings are its sub-sections, which each follow the
    # normal ?strana=N pattern.
    "sportweb": [
        ("futbal", "https://www.pravda.sk/sportweb/futbal"),
        ("hokej", "https://www.pravda.sk/sportweb/hokej"),
        ("tenis", "https://www.pravda.sk/sportweb/tenis"),
        ("atletika", "https://www.pravda.sk/sportweb/atletika"),
        ("basketbal", "https://www.pravda.sk/sportweb/basketbal"),
        ("cyklistika", "https://www.pravda.sk/sportweb/cyklistika"),
        ("zimne-sporty", "https://www.pravda.sk/sportweb/zimne-sporty"),
        ("ostatne-sporty", "https://www.pravda.sk/sportweb/ostatne-sporty"),
    ],
    "koktail": [("vsetky", "https://www.pravda.sk/koktail/")],
    "auto": [("vsetky", "https://www.pravda.sk/auto/")],
    "uzitocna": [("vsetky", "https://www.pravda.sk/uzitocna/")],
    "cestovanie": [("vsetky", "https://www.pravda.sk/cestovanie/")],
    "zdravie": [("vsetky", "https://www.pravda.sk/zdravie/")],
    "zena": [("vsetky", "https://www.pravda.sk/zena/")],
}

# Be conservative: this is explicitly required by the assignment ("do not
# download too fast"). One request at a time, with a real pause between
# every single HTTP request made anywhere in the pipeline.
MIN_DELAY_SECONDS = 3.0
USER_AGENT = (
    "SkPravdaCorpusBot/1.0 (school research project; "
    "contact: <your email/course contact here>)"
)