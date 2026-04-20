#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modrak_rss_gen.py
-----------------
Fetch the official Podbean feed and extend it with older episodes that
fall off the feed (Podbean only serves ~300 most recent episodes).
Writes the merged complete RSS feed into OUTPUT_DIR. Meant for cron.

Strategy: take the original XML from feed.podbean.com, keep it 1:1
(channel metadata, existing <item> elements), and insert additional
<item> elements for older scraped episodes before </channel>, using
an identical item format.

Usage (cron):
    /usr/bin/python3 /opt/modrak-rss/modrak_rss_gen.py

Configuration via env vars (or edit SHOWS below):
    OUTPUT_DIR   directory to write *.rss into (default /var/www/rss)
    SHOWS        JSON {"slug":"podbean-subdomain"}, else DEFAULT_SHOWS

Atomic write: writes via .tmp + os.replace, so nginx never serves a
partial/corrupted feed.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import tempfile
import traceback
from datetime import datetime
from email.utils import format_datetime
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit("Missing module 'requests'. Run: pip3 install requests")


# ----------- Configuration -----------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_ARCHIVE_PAGES = 80
REQUEST_DELAY = 0.5   # seconds between scraping requests

# Edit here or via ENV SHOWS.
# Key = output filename slug, value = podbean subdomain.
DEFAULT_SHOWS = {
    "modrak": "modrak",
    # Add more Podbean-hosted podcasts here, e.g.:
    # "otherpodcast": "otherpodcast",   # -> https://otherpodcast.podbean.com
}

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/var/www/rss")

try:
    SHOWS = json.loads(os.environ["SHOWS"]) if os.environ.get("SHOWS") else DEFAULT_SHOWS
except json.JSONDecodeError:
    print("ERROR: SHOWS is not valid JSON, using defaults.", file=sys.stderr)
    SHOWS = DEFAULT_SHOWS


# ----------- HTTP -----------

def http_get(url, timeout=30):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xml,application/rss+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en,cs;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


def resolve_mp3_via_share(share_url, timeout=20):
    """A share URL (podbean.com/media/share/...) redirects to the real
    mcdn.podbean.com MP3. Returns (final_url, length) or (None, 0)."""
    try:
        r = requests.head(share_url, allow_redirects=True, timeout=timeout,
                          headers={"User-Agent": USER_AGENT})
        return r.url, int(r.headers.get("Content-Length", 0) or 0)
    except Exception:
        return None, 0


# ----------- Archive scraping -----------

SHARE_RE = re.compile(
    r'https://www\.podbean\.com/media/share/pb-[a-z0-9-]+\?download=1', re.I
)
MCDN_RE = re.compile(r'https://mcdn\.podbean\.com/[^"\'<>\s]+?\.mp3', re.I)
EP_HREF_RE = re.compile(r'href="(/e/[^"]+?/)"')


def list_archive_urls(subdomain):
    """Walk /page/N/ on the podbean subdomain and return episode URLs."""
    base = f"https://{subdomain}.podbean.com"
    ordered = []
    seen = set()
    empty_pages = 0

    for page in range(1, MAX_ARCHIVE_PAGES + 1):
        url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
        try:
            t = http_get(url).text
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                break
            raise
        hits = EP_HREF_RE.findall(t)
        new = [urljoin(base, h) for h in hits if urljoin(base, h) not in seen]
        if not new:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
            for u in new:
                seen.add(u)
                ordered.append(u)
        time.sleep(REQUEST_DELAY)
    return ordered


def scrape_episode(url):
    """Fetch a single episode page and return a dict, or None on failure."""
    t = http_get(url).text

    def og(prop, default=""):
        m = re.search(rf'<meta\s+property="{re.escape(prop)}"\s+content="([^"]+)"', t)
        return html.unescape(m.group(1)) if m else default

    title = og("og:title") or "(untitled)"
    description = og("og:description")
    pub_iso = og("article:published_time")

    mp3_url, length = None, 0
    sm = SHARE_RE.search(t)
    if sm:
        mp3_url, length = resolve_mp3_via_share(sm.group(0))
    if not mp3_url:
        m = MCDN_RE.search(t)
        if m:
            mp3_url = m.group(0)
    if not mp3_url:
        return None

    ep_num_m = re.match(r"#?(\d+)[:\s]", title)
    ep_num = int(ep_num_m.group(1)) if ep_num_m else None

    # ISO -> RFC 822 (Podbean format: "Mon, 20 Apr 2026 09:28:00 +0200")
    pub_rfc = ""
    if pub_iso:
        try:
            dt = datetime.fromisoformat(pub_iso.replace("Z", "+00:00"))
            pub_rfc = format_datetime(dt)
        except Exception:
            pass

    return {
        "url": url,
        "title": title,
        "description": description,
        "pub_date": pub_rfc,
        "mp3_url": mp3_url,
        "length": length,
        "episode_num": ep_num,
    }


# ----------- Item template (identical format to Podbean) -----------

def escape_cdata(s):
    """CDATA must not contain ']]>', replace with ']]&gt;'."""
    s = "" if s is None else str(s)
    return s.replace("]]>", "]]&gt;")


def xml_attr(s):
    return html.escape("" if s is None else str(s), quote=True)


def build_item_xml(ep, subdomain, show_author):
    """Emit an <item> element matching Podbean's format."""
    title = ep["title"]
    link = ep["url"]
    comments = f"{link}#comments"
    pub = ep["pub_date"]

    # GUID in the same style as Podbean: "subdomain.podbean.com/<uuid>".
    # For scraped episodes we have no real UUID - derive a stable one
    # from the URL hash.
    ep_hash = hashlib.sha1(link.encode("utf-8")).hexdigest()
    guid_id = f"{ep_hash[0:8]}-{ep_hash[8:12]}-{ep_hash[12:16]}-{ep_hash[16:20]}-{ep_hash[20:32]}"
    guid = f"{subdomain}.podbean.com/{guid_id}"

    desc = ep.get("description", "")
    mp3 = ep["mp3_url"]
    length = ep.get("length", 0) or 0
    ep_num = ep.get("episode_num")

    lines = [
        "    <item>",
        f"        <title>{xml_attr(title)}</title>",
        f"        <itunes:title>{xml_attr(title)}</itunes:title>",
        f"        <link>{xml_attr(link)}</link>",
        f"        <comments>{xml_attr(comments)}</comments>",
        f"        <pubDate>{pub}</pubDate>",
        f'        <guid isPermaLink="false">{guid}</guid>',
        f"        <description><![CDATA[{escape_cdata(desc)}]]></description>",
        f'        <enclosure url="{xml_attr(mp3)}" length="{length}" type="audio/mpeg"/>',
        f"        <itunes:summary><![CDATA[{escape_cdata(desc)}]]></itunes:summary>",
        f"        <itunes:author>{xml_attr(show_author)}</itunes:author>",
        "        <itunes:explicit>false</itunes:explicit>",
        "        <itunes:block>No</itunes:block>",
    ]
    if ep_num is not None:
        lines.append(f"        <itunes:episode>{ep_num}</itunes:episode>")
    lines.append("        <itunes:episodeType>full</itunes:episodeType>")
    lines.append("    </item>")
    return "\n".join(lines)


# ----------- Merge: original feed + extra items -----------

def extract_existing_urls(feed_xml):
    """From the original XML, extract the set of episode URLs (from <link>
    elements inside <item>)."""
    items = re.findall(r"<item>.*?</item>", feed_xml, re.DOTALL)
    urls = set()
    for it in items:
        m = re.search(r"<link>([^<]+)</link>", it)
        if m:
            urls.add(m.group(1).strip())
    return urls, items


def extract_show_author(feed_xml):
    """Pull itunes:author from the channel."""
    # First match is inside <channel>, before any <item>.
    m = re.search(r"<itunes:author>([^<]+)</itunes:author>", feed_xml)
    return m.group(1).strip() if m else ""


def merge_feed(original_xml, extra_episodes, subdomain):
    """Insert extra <item> elements at the end of <channel>."""
    if not extra_episodes:
        return original_xml

    author = extract_show_author(original_xml)
    extra_items_xml = "\n".join(
        build_item_xml(ep, subdomain, author) for ep in extra_episodes
    )

    close_tag = "</channel>"
    idx = original_xml.rfind(close_tag)
    if idx < 0:
        raise ValueError("</channel> not found in original feed")

    before = original_xml[:idx].rstrip()
    after = original_xml[idx:]
    return before + "\n" + extra_items_xml + "\n\n" + after


# ----------- Atomic write -----------

def atomic_write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=d, prefix=".tmp_", suffix=".rss",
                                     delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, path)


# ----------- Orchestration -----------

def process_show(slug, subdomain):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {slug} ({subdomain})",
          file=sys.stderr, flush=True)

    # 1) Fetch the original feed (kept 1:1).
    feed_url = f"https://feed.podbean.com/{subdomain}/feed.xml"
    print(f"  feed: {feed_url}", file=sys.stderr, flush=True)
    r = http_get(feed_url)
    original_xml = r.content.decode("utf-8")

    existing_urls, existing_items = extract_existing_urls(original_xml)
    print(f"  -> {len(existing_items)} episodes in official feed",
          file=sys.stderr, flush=True)

    # 2) Walk the archive and find what is missing.
    archive_urls = list_archive_urls(subdomain)
    print(f"  -> {len(archive_urls)} episodes in archive",
          file=sys.stderr, flush=True)

    missing = [u for u in archive_urls if u not in existing_urls]
    print(f"  -> {len(missing)} old episodes to add",
          file=sys.stderr, flush=True)

    # 3) Scrape the missing episodes.
    extra_episodes = []
    if missing:
        for i, url in enumerate(missing, 1):
            try:
                ep = scrape_episode(url)
                if ep:
                    extra_episodes.append(ep)
            except Exception as e:
                print(f"  !! {url}: {e}", file=sys.stderr)
            if i % 20 == 0:
                print(f"    ... {i}/{len(missing)}",
                      file=sys.stderr, flush=True)
            time.sleep(REQUEST_DELAY)
        print(f"  -> added {len(extra_episodes)} episodes",
              file=sys.stderr, flush=True)

    # 4) Merge and write.
    merged = merge_feed(original_xml, extra_episodes, subdomain)
    out_path = os.path.join(OUTPUT_DIR, f"{slug}.rss")
    atomic_write(out_path, merged.encode("utf-8"))
    total = len(existing_items) + len(extra_episodes)
    print(f"  -> wrote {out_path} ({len(merged)} B, {total} episodes)",
          file=sys.stderr, flush=True)
    return total


def main():
    print(f"OUTPUT_DIR = {OUTPUT_DIR}", file=sys.stderr)
    print(f"Shows = {list(SHOWS.keys())}", file=sys.stderr)

    failures = []
    for slug, subdomain in SHOWS.items():
        try:
            process_show(slug, subdomain)
        except Exception as e:
            print(f"  !! FAILED for {slug}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            failures.append(slug)

    if failures:
        print(f"DONE WITH ERRORS: {failures}", file=sys.stderr)
        sys.exit(1)
    print("DONE.", file=sys.stderr)


if __name__ == "__main__":
    main()
