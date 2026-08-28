"""
src/utils/polite_client.py

A single shared HTTP client used by every stage of the pipeline
(discover_urls.py, download_html.py, extract_images.py). Centralizing this
in one place means the rate limit and robots.txt logic only has to be
right once, instead of every script reimplementing it slightly differently.

What it does:
  - Checks robots.txt for the target HOST before the first request to that
    host, and caches the result (one parsed ruleset per host).
  - Refuses to fetch a URL disallowed by robots.txt - raises, doesn't
    silently skip, so calling code has to consciously decide what to do.
  - Enforces a minimum delay between requests, PER HOST, using whichever is
    larger: our own MIN_DELAY_SECONDS, or the site's own Crawl-delay
    directive if it specifies one (robots.txt can ask for a longer delay
    than we'd otherwise use - we should honor that, not just our own
    default).
  - Retries transient failures (timeouts, 5xx, 429) with backoff, similar
    to what we ended up needing for the Wikipedia/WIT pipeline.

Uses `protego` (the robots.txt parser Scrapy uses) rather than the stdlib
`urllib.robotparser`. Found this the hard way while testing: stdlib
robotparser silently treats a URL as ALLOWED when a robots.txt has multiple
separate "User-agent: *" blocks instead of one block with multiple
directives - a real-world formatting variation, not just a malformed edge
case. protego handles this correctly. Given a wrong answer here means
either violating robots.txt or missing content, it's worth the extra
dependency.
"""

import sys
import time
from urllib.parse import urlparse

import requests
from protego import Protego

sys.path.insert(0, "config")
from sources import MIN_DELAY_SECONDS, USER_AGENT  # noqa: E402

MAX_RETRIES = 5


class RobotsDisallowed(Exception):
    pass


class PoliteClient:
    def __init__(self, min_delay=MIN_DELAY_SECONDS, user_agent=USER_AGENT):
        self.min_delay = min_delay
        self.user_agent = user_agent
        self._robots_cache = {}       # host -> Protego ruleset
        self._crawl_delay = {}        # host -> float seconds (from robots.txt, if any)
        self._last_request_at = {}    # host -> monotonic time of last request
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    def _host(self, url):
        return urlparse(url).netloc

    def _robots_for(self, url):
        host = self._host(url)
        if host not in self._robots_cache:
            parsed = urlparse(url)
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            try:
                resp = self.session.get(robots_url, timeout=15)
                rp = Protego.parse(resp.text if resp.ok else "")
                if not resp.ok:
                    print(f"NOTE: {robots_url} returned {resp.status_code} - "
                          f"treating as no restrictions.", file=sys.stderr)
            except Exception:
                # if robots.txt itself is unreachable, don't block on it -
                # but this means we fall back to "assume allowed", not
                # "assume disallowed", so log it clearly rather than hiding it
                print(f"WARNING: could not fetch/parse {robots_url} - "
                      f"proceeding as if crawling is allowed.", file=sys.stderr)
                rp = Protego.parse("")
            self._robots_cache[host] = rp

            delay = rp.crawl_delay(self.user_agent)
            self._crawl_delay[host] = float(delay) if delay else None
        return self._robots_cache[host]

    def _wait_if_needed(self, host):
        delay = max(self.min_delay, self._crawl_delay.get(host) or 0)
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            remaining = delay - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def get(self, url, **kwargs):
        rp = self._robots_for(url)
        if not rp.can_fetch(url, self.user_agent):
            raise RobotsDisallowed(f"robots.txt disallows fetching: {url}")

        host = self._host(url)
        delay_wait = 1.0
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_if_needed(host)
            self._last_request_at[host] = time.monotonic()
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else delay_wait
                    print(f"  {resp.status_code} from {url}, waiting {wait}s "
                          f"(attempt {attempt}/{MAX_RETRIES})...", file=sys.stderr)
                    time.sleep(wait)
                    delay_wait = min(delay_wait * 2, 60)
                    last_exc = requests.exceptions.HTTPError(f"{resp.status_code}")
                    continue
                resp.raise_for_status()
                # requests defaults response.encoding to ISO-8859-1 when the
                # server's Content-Type header omits an explicit charset
                # (old HTTP/1.1 convention), completely ignoring the page's
                # own <meta charset="utf-8"> declaration. Confirmed via
                # testing that this silently mojibake-corrupts Slovak
                # diacritics on any page whose HTTP headers don't spell out
                # charset=utf-8 even though the HTML itself is UTF-8 -
                # exactly the situation for Slovak-language content. Fix:
                # for text responses without an explicit charset, trust
                # content-sniffing (apparent_encoding) instead of the
                # RFC-default guess.
                content_type = resp.headers.get("Content-Type", "").lower()
                if content_type.startswith("text/") and "charset" not in content_type:
                    resp.encoding = resp.apparent_encoding
                return resp
            except requests.exceptions.RequestException as e:
                last_exc = e
                print(f"  request failed ({e.__class__.__name__}: {e}), "
                      f"retrying in {delay_wait}s (attempt {attempt}/{MAX_RETRIES})...",
                      file=sys.stderr)
                time.sleep(delay_wait)
                delay_wait = min(delay_wait * 2, 60)
        raise RuntimeError(f"Giving up on {url} after {MAX_RETRIES} attempts: {last_exc}")
