#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modrak_archive.py
-----------------
Full offline archive of the paid Modrák Opinio podcast. Downloads every
MP3/M4A, cover image, and generates a local RSS feed in which all
<enclosure> URLs point to local files instead of Opinio's servers.

Why: when you stop paying Opinio, the hosted feed stops working. With
this archive you keep a fully functional local copy of everything that
was ever released while you were subscribed.

Idempotent: rerunning the script only downloads new/changed episodes,
everything already archived is skipped.

Archive layout:
  ARCHIVE_DIR/
  ├── media/
  │   ├── 417-vypraveni-pribehu.m4a
  │   ├── 416-arena-32-....m4a
  │   └── ...
  ├── cover.jpg              # channel image
  ├── feed-opinio.xml        # ORIGINAL Opinio feed (contains your token, chmod 600)
  └── modrak-archive.rss     # LOCAL feed with local URLs (safe to expose)

Usage (cron):
  /usr/bin/python3 /opt/modrak-archive/modrak_archive.py

Configuration via env vars or by editing the constants below:
  OPINIO_URL       paid Opinio RSS URL with your player_key
  ARCHIVE_DIR      where to store files (default /var/www/rss/modrak-archive)
  PUBLIC_BASE_URL  URL prefix used in the local RSS (default
                   http://localhost/modrak-archive/)

Security:
  feed-opinio.xml contains your player_key in every enclosure URL. It
  is written with mode 0600 so only the owning user can read it. Do NOT
  expose it via your web server. The generated modrak-archive.rss is
  clean of tokens and safe to serve.

NOTE on <enclosure length="...">:
  Opinio puts DURATION IN SECONDS into the RSS `length` attribute
  instead of the byte count required by RFC. We therefore ignore the
  feed's `length` and verify downloads against the HTTP Content-Length
  header from the CDN instead.
"""

import html
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("Missing module 'requests'. Run: pip3 install requests")


# ----- Configuration -----

OPINIO_URL = os.environ.get(
    "OPINIO_URL",
    "https://opinio.cz/rss/podcasts/modrak"
    "?player_key=",
)

ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/var/www/rss/modrak-archive")

# URL prefix where the ARCHIVE_DIR is served. MUST end with / or no /.
# Example: http://192.168.40.25:8088/modrak-archive
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    "http://localhost/modrak-archive",
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2.0
REQUEST_DELAY_SEC = 0.3       # pause between media downloads
CHUNK_SIZE = 65536            # 64 KB
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600            # 10 min per file
MIN_REASONABLE_SIZE = 100 * 1024  # 100 KB - anything smaller is suspicious


# ----- Regex -----

# Opinio enclosure URL: https://opinio.cz/api/v1/podcasts/SLUG.m4a?...
SLUG_RE = re.compile(
    r'https?://opinio\.cz/api/v1/podcasts/([^/"\'?]+)\.m4a', re.I
)

# Matches url="..." attribute within an <enclosure> to Opinio CDN
ENC_URL_RE = re.compile(
    r'url="(https?://opinio\.cz/api/v1/podcasts/[^"]+?\.m4a[^"]*)"'
)


# ----- Utilities -----

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def log_ts(msg):
    log(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")


def safe_slug(s):
    """Keep filename-safe characters only."""
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', s)[:200]


def mask_token(url):
    return re.sub(r'player_key=[^&]+', 'player_key=***', url)


def human_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ----- HTTP -----

def http_get(url, stream=False):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    r = requests.get(
        url, headers=headers, stream=stream,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    r.raise_for_status()
    return r


def http_head_length(url):
    """Return Content-Length of url via HEAD, or None if not available."""
    try:
        h = requests.head(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=(CONNECT_TIMEOUT, 30),
            allow_redirects=True,
        )
        h.raise_for_status()
        cl = h.headers.get('Content-Length')
        return int(cl) if cl else None
    except Exception:
        return None


# ----- File operations -----

def atomic_write_bytes(path, data, mode=0o644):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=d, prefix=".tmp_", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


def download_file(url, dest):
    """Download url to dest with retry.

    Idempotency: if dest exists, HEAD the URL and compare sizes. If the
    server advertises no Content-Length, we trust the existing file.

    Verification: after download, we compare the byte count we wrote
    against the Content-Length from the GET response (NOT the feed's
    length attribute - Opinio puts duration-seconds in there, which is
    useless as a byte count).

    Returns 'skip' | 'downloaded'.
    """

    if os.path.exists(dest):
        actual = os.path.getsize(dest)
        remote_len = http_head_length(url)
        if remote_len is None:
            # No way to verify - assume previously-downloaded file is fine
            if actual >= MIN_REASONABLE_SIZE:
                return 'skip'
            log(f"    existing file suspiciously small ({actual} B), re-downloading")
        elif actual == remote_len:
            return 'skip'
        else:
            log(f"    size mismatch ({actual} != {remote_len}), re-downloading")

    part = dest + '.part'
    os.makedirs(os.path.dirname(part), exist_ok=True)
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with http_get(url, stream=True) as r:
                # Grab Content-Length from the real GET response
                cl = r.headers.get('Content-Length')
                remote_len = int(cl) if cl else None

                total = 0
                with open(part, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)

            if remote_len is not None and total != remote_len:
                raise RuntimeError(
                    f"downloaded {total} B, server said {remote_len} B"
                )
            if total < MIN_REASONABLE_SIZE:
                raise RuntimeError(
                    f"suspiciously small download: {total} B "
                    f"(< {MIN_REASONABLE_SIZE} B threshold)"
                )

            os.chmod(part, 0o644)
            os.replace(part, dest)
            return 'downloaded'

        except Exception as e:
            last_err = e
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SEC ** attempt
                log(f"    attempt {attempt} failed: {e}; retry in {wait:.1f}s")
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"failed after {MAX_RETRIES} attempts: {last_err}"
                ) from last_err


def download_cover(url, dest):
    """Like download_file but without the audio-size sanity check - covers
    are typically 20-500 KB."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return 'skip'

    part = dest + '.part'
    os.makedirs(os.path.dirname(part), exist_ok=True)
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with http_get(url, stream=True) as r:
                cl = r.headers.get('Content-Length')
                remote_len = int(cl) if cl else None
                total = 0
                with open(part, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
            if remote_len is not None and total != remote_len:
                raise RuntimeError(
                    f"downloaded {total} B, server said {remote_len} B"
                )
            if total == 0:
                raise RuntimeError("empty response")
            os.chmod(part, 0o644)
            os.replace(part, dest)
            return 'downloaded'
        except Exception as e:
            last_err = e
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SEC ** attempt
                log(f"    attempt {attempt} failed: {e}; retry in {wait:.1f}s")
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"failed after {MAX_RETRIES} attempts: {last_err}"
                ) from last_err


# ----- Feed parsing -----

def parse_items(xml_text):
    """Extract list of {title, slug, enclosure_url} from <item>s.

    Note: we no longer extract `length` - it is duration-in-seconds in
    Opinio's feed, not bytes, so it's useless for verification."""
    items_raw = re.findall(r'<item>.*?</item>', xml_text, re.DOTALL)

    parsed = []
    for raw in items_raw:
        title_m = re.search(r'<title>([^<]*)</title>', raw)
        title = html.unescape(title_m.group(1)) if title_m else '(no title)'

        # Find the full <enclosure ...> opening tag, then parse attrs
        enc_tag = re.search(r'<enclosure\s+([^>]+?)\s*/?>', raw)
        if not enc_tag:
            continue
        attrs = enc_tag.group(1)
        url_m = re.search(r'\burl="([^"]+)"', attrs)
        if not url_m:
            continue
        url = html.unescape(url_m.group(1))

        slug_m = SLUG_RE.search(url)
        if not slug_m:
            continue

        parsed.append({
            'title': title,
            'slug': slug_m.group(1),
            'enclosure_url': url,
        })

    return parsed


def find_channel_image(xml_text):
    """Return URL of channel image (itunes:image or image/url), or None."""
    m = re.search(r'<itunes:image\s+href="([^"]+)"', xml_text)
    if m:
        return html.unescape(m.group(1))
    m = re.search(r'<image>\s*<url>([^<]+)</url>', xml_text)
    if m:
        return html.unescape(m.group(1))
    return None


# ----- Feed rewriting -----

def rewrite_feed_for_local(xml_text, downloaded_slugs, public_base,
                           cover_filename, local_feed_url):
    """Replace tokens and remote URLs with local equivalents for episodes
    we successfully downloaded. Episodes we could not download keep their
    original Opinio URL (they will break once subscription ends, but
    there is no alternative)."""

    base = public_base.rstrip('/')
    text = xml_text

    def replace_enc_url(m):
        url = m.group(1)
        sm = SLUG_RE.search(url)
        if sm and sm.group(1) in downloaded_slugs:
            slug = sm.group(1)
            local = f"{base}/media/{safe_slug(slug)}.m4a"
            return f'url="{html.escape(local, quote=True)}"'
        return m.group(0)

    text = ENC_URL_RE.sub(replace_enc_url, text)

    # Channel image -> local cover
    cover_url = f"{base}/{cover_filename}"
    cover_esc = html.escape(cover_url, quote=True)
    text = re.sub(
        r'<itunes:image\s+href="[^"]*"',
        f'<itunes:image href="{cover_esc}"',
        text,
    )
    text = re.sub(
        r'(<image>\s*<url>)[^<]*(</url>)',
        lambda m: m.group(1) + cover_esc + m.group(2),
        text,
        flags=re.DOTALL,
    )

    # atom:link rel="self" -> local feed
    local_feed_esc = html.escape(local_feed_url, quote=True)
    text = re.sub(
        r'<atom:link\s+href="[^"]*"\s+rel="self"[^>]*/?>',
        f'<atom:link href="{local_feed_esc}" rel="self" '
        f'type="application/rss+xml" />',
        text,
    )

    # itunes:new-feed-url -> local feed (or remove)
    text = re.sub(
        r'<itunes:new-feed-url>[^<]*</itunes:new-feed-url>',
        f'<itunes:new-feed-url>{local_feed_esc}</itunes:new-feed-url>',
        text,
    )

    return text


# ----- Orchestration -----

def main():
    log(f"ARCHIVE_DIR     = {ARCHIVE_DIR}")
    log(f"PUBLIC_BASE_URL = {PUBLIC_BASE_URL}")
    log(f"OPINIO_URL      = {mask_token(OPINIO_URL)}")
    log_ts("starting")

    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    # 1) Fetch Opinio feed
    log("fetching Opinio feed...")
    r = http_get(OPINIO_URL)
    opinio_bytes = r.content
    log(f"  -> {len(opinio_bytes)} B")

    head = opinio_bytes[:2000]
    if b'<rss' not in head or b'<channel>' not in head:
        raise RuntimeError("Response is not an RSS feed")

    # 2) Save original feed (with tokens) with chmod 0600
    feed_opinio_path = os.path.join(ARCHIVE_DIR, 'feed-opinio.xml')
    atomic_write_bytes(feed_opinio_path, opinio_bytes, mode=0o600)
    log(f"  saved {feed_opinio_path} (mode 0600, contains token!)")

    # 3) Parse items
    xml_text = opinio_bytes.decode('utf-8')
    items = parse_items(xml_text)
    log(f"  parsed {len(items)} episodes")
    if not items:
        raise RuntimeError("Zero items parsed - refusing to continue")

    # 4) Download cover image
    cover_url = find_channel_image(xml_text)
    cover_filename = 'cover.jpg'
    if cover_url:
        ext = os.path.splitext(urlparse(cover_url).path)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png', '.webp'):
            cover_filename = 'cover' + ext
        cover_path = os.path.join(ARCHIVE_DIR, cover_filename)
        log(f"fetching cover -> {cover_filename}")
        try:
            result = download_cover(cover_url, cover_path)
            log(f"  -> {result}")
        except Exception as e:
            log(f"  !! cover download failed: {e}")
    else:
        log("  no cover URL found in feed")

    # 5) Download all episodes
    media_dir = os.path.join(ARCHIVE_DIR, 'media')
    os.makedirs(media_dir, exist_ok=True)

    downloaded_slugs = set()
    stats = {'downloaded': 0, 'skip': 0, 'failed': 0}
    bytes_on_disk = 0

    for i, item in enumerate(items, 1):
        slug = item['slug']
        safe = safe_slug(slug)
        dest = os.path.join(media_dir, f'{safe}.m4a')

        try:
            result = download_file(item['enclosure_url'], dest)
            stats[result] += 1
            downloaded_slugs.add(safe)
            size = os.path.getsize(dest)
            bytes_on_disk += size

            if result == 'downloaded':
                log(f"  [{i}/{len(items)}] {slug}: downloaded "
                    f"({human_bytes(size)})")
            elif i % 50 == 0:
                log(f"  [{i}/{len(items)}] ok "
                    f"(downloaded={stats['downloaded']} "
                    f"skipped={stats['skip']})")

        except Exception as e:
            log(f"  !! [{i}/{len(items)}] {slug}: {e}")
            stats['failed'] += 1

        time.sleep(REQUEST_DELAY_SEC)

    log(f"media done: downloaded={stats['downloaded']} "
        f"skipped={stats['skip']} failed={stats['failed']} "
        f"total_on_disk={human_bytes(bytes_on_disk)}")

    # 6) Generate local RSS feed
    log("generating local RSS feed...")
    local_feed_url = f"{PUBLIC_BASE_URL.rstrip('/')}/modrak-archive.rss"
    local_xml = rewrite_feed_for_local(
        xml_text,
        downloaded_slugs,
        PUBLIC_BASE_URL,
        cover_filename,
        local_feed_url,
    )

    # Safety: if any episode was NOT downloaded, its Opinio URL stays -
    # including the token. Warn about that. But if everything downloaded,
    # the local feed must be token-free.
    if stats['failed'] == 0 and len(downloaded_slugs) == len(items):
        if 'player_key' in local_xml:
            raise RuntimeError(
                "BUG: token leak in local RSS despite full download - "
                "refusing to write"
            )
    else:
        if 'player_key' in local_xml:
            log("  NOTE: local RSS still contains tokens for "
                f"{stats['failed']} failed episodes - it is NOT safe to "
                "expose publicly yet. Rerun the script after fixing "
                "network issues.")

    local_path = os.path.join(ARCHIVE_DIR, 'modrak-archive.rss')
    atomic_write_bytes(local_path, local_xml.encode('utf-8'))
    log(f"  wrote {local_path} ({len(local_xml)} B)")

    log_ts("DONE")

    if stats['failed']:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
