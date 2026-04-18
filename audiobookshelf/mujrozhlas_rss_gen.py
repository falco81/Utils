#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mujrozhlas_rss_gen.py  (v4)
---------------------------
Generates an RSS feed 1:1 matching the structure of the official mujRozhlas
feed, but for ALL episodes (not just the last 50).

v4: rewritten to match the reference official feed - same namespaces,
    same image (resize variant), same promo text in description,
    same GUID (legacyId), same indentation.

Configuration via ENV:
    OUTPUT_DIR   directory for *.rss files (default /var/www/rss)
    SHOWS        JSON {"slug":"UUID"}, otherwise DEFAULT_SHOWS is used

Note: PROMO_SHOW text and "Copyright Český rozhlas" line are intentionally
kept in Czech because they are the exact strings used by the official
mujRozhlas feed (and the generated feed targets Czech listeners).
"""

import html
import json
import os
import re
import sys
import time
import tempfile
import traceback
from datetime import datetime, timezone
from email.utils import format_datetime

try:
    import requests
except ImportError:
    sys.exit("Missing 'requests' module. pip3 install requests")


API_BASE = "https://api.mujrozhlas.cz"
PAGE_SIZE = 100
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Promo text that Czech Radio (CRo) adds to every episode description.
# Contains {campaign} = episode UUID for UTM tracking.
# Kept in Czech - this is the literal string from the official feed and is
# consumed by Czech-speaking podcast listeners.
PROMO_SHOW = (
    '<br><br>Všechny díly podcastu {show_title} můžete pohodlně poslouchat '
    'v mobilní aplikaci mujRozhlas pro '
    '<a href="https://play.google.com/store/apps/details?id=cz.rozhlas.mujrozhlas">Android</a> '
    'a <a href="https://apps.apple.com/cz/app/id1455654616">iOS</a> '
    'nebo na webu <a href="https://www.mujrozhlas.cz/rapi/view/show/{show_uuid}'
    '?utm_source=rss&utm_medium=podcast&utm_campaign={campaign}">mujRozhlas.cz</a>.'
)

DEFAULT_SHOWS = {
    "quest": "9f19fbeb-a3d2-3cfb-b04e-3e0a253b639a",
}

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/var/www/rss")

try:
    SHOWS = json.loads(os.environ["SHOWS"]) if os.environ.get("SHOWS") else DEFAULT_SHOWS
except json.JSONDecodeError:
    print("ERROR: SHOWS is not valid JSON, using default.", file=sys.stderr)
    SHOWS = DEFAULT_SHOWS


def api_get(url, params=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.api+json, application/json",
        "Accept-Language": "cs,en;q=0.8",
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_show(uuid):
    return api_get(f"{API_BASE}/shows/{uuid}")


def fetch_all_episodes(uuid):
    episodes = []
    page = 1
    while True:
        params = {
            "filter[entity]": "episode",
            "filter[show]": uuid,
            "sort": "-since",
            "page[number]": page,
            "page[size]": PAGE_SIZE,
        }
        try:
            data = api_get(f"{API_BASE}/episodes", params=params)
        except requests.HTTPError as e:
            if page == 1 and e.response.status_code in (400, 404):
                return _fetch_alt(uuid)
            raise
        batch = data.get("data", [])
        if not batch:
            break
        episodes.extend(batch)
        if not data.get("links", {}).get("next") or len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.25)
    return episodes


def _fetch_alt(uuid):
    episodes = []
    next_url = f"{API_BASE}/shows/{uuid}/episodes?page[size]={PAGE_SIZE}&sort=-since"
    while next_url:
        data = api_get(next_url)
        episodes.extend(data.get("data", []))
        next_url = data.get("links", {}).get("next")
        time.sleep(0.25)
    return episodes


# --------- XML utilities ---------

def xml_escape(s):
    return html.escape("" if s is None else str(s), quote=True)


def cdata(s):
    s = "" if s is None else str(s)
    return "<![CDATA[" + s.replace("]]>", "]]&gt;") + "]]>"


def pub_date(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def duration_hms(seconds):
    if not seconds:
        return ""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --------- images ---------

def transform_image_to_resize(url):
    """
    Converts an original image URL to the resize variant used by the CRo feed:
      /sites/default/files/images/ABC.jpg
        -> /sites/default/files/styles/mr_square_large/public/images/ABC.jpg

    The ?itok and ?v query params are added by CRo for cache-busting, but the
    URL also works without them (the CDN just recomputes them).
    """
    if not url:
        return url
    # If already a resize variant, return unchanged
    if "/styles/" in url:
        return url
    # Insert the 'styles/mr_square_large/public/' segment before 'images/'
    new_url = re.sub(
        r'(/sites/default/files/)(images/)',
        r'\1styles/mr_square_large/public/\2',
        url,
    )
    return new_url


def get_image_url(attrs):
    """Find the image URL - the mujRozhlas API uses 'asset' (object)."""
    asset = attrs.get("asset")
    if isinstance(asset, dict):
        u = asset.get("url")
        if u:
            return transform_image_to_resize(u)
    # fallbacks for other shows
    for img in (attrs.get("assets") or []):
        if isinstance(img, dict):
            u = img.get("url")
            if u:
                return transform_image_to_resize(u)
    mi = attrs.get("mirroredImage")
    if isinstance(mi, str) and mi:
        return transform_image_to_resize(mi)
    return ""


# --------- audio ---------

def pick_audio(attrs):
    """(url, mime, length_bytes) - prefers podtrac."""
    links = attrs.get("audioLinks") or []
    # podtrac URL
    for src in links:
        url = src.get("url", "")
        if url and "podtrac.com" in url:
            length = int(src.get("duration", 0) or 0)
            # In the API, 'duration' is in seconds, not bytes - leave length at 0,
            # correct bytes will be resolved by --with-lengths mode or by the client.
            return url, src.get("mimeType", "audio/mpeg"), 0
    # mp3 variant
    for src in links:
        if src.get("variant") == "mp3" and src.get("url"):
            return src["url"], src.get("mimeType", "audio/mpeg"), 0
    # anything else
    for src in links:
        if src.get("url"):
            return src["url"], src.get("mimeType", "audio/mpeg"), 0
    return None, "audio/mpeg", 0


def head_content_length(url, timeout=10):
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers={"User-Agent": USER_AGENT})
        return int(r.headers.get("Content-Length", 0) or 0)
    except Exception:
        return 0


# --------- GUID ---------

def get_legacy_id(attrs):
    """Return legacyId (numeric) if present, otherwise None."""
    # Try various keys where the API might expose a numeric ID
    for key in ("legacyId", "contentId", "cid", "id"):
        v = attrs.get(key)
        if v and (isinstance(v, int) or (isinstance(v, str) and v.isdigit())):
            return str(v)
    return None


# --------- main build ---------

def build_rss(show_data, episodes, fetch_lengths=False):
    attrs = show_data["data"]["attributes"]
    show_uuid = show_data["data"]["id"]
    title = attrs.get("title", "Podcast")
    short_description = attrs.get("shortDescription") or ""
    base_description = attrs.get("description") or short_description

    # Promo text appended to description (same as in the official feed)
    channel_promo = PROMO_SHOW.format(
        show_title=title,
        show_uuid=show_uuid,
        campaign=f"{show_uuid}_description",
    )
    channel_description = base_description + channel_promo

    image_url = get_image_url(attrs)
    if image_url:
        print(f"  image: {image_url}", file=sys.stderr)
    else:
        print(f"  WARNING: image not found", file=sys.stderr)

    feed_link = f"https://www.mujrozhlas.cz/rapi/view/show/{show_uuid}"
    current_year = datetime.now().year

    # Header - matches the official feed exactly:
    # - no 'encoding' in the XML prolog (CRo doesn't include it either)
    # - ONLY itunes and content namespaces, no atom
    parts = [
        '<?xml version="1.0"?>',
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">',
        "  <channel>",
        f"    <title>{xml_escape(title)}</title>",
        "    <language>cs</language>",
        f"    <copyright>Český rozhlas 2000-{current_year}</copyright>",
        f"    <link>{xml_escape(feed_link)}</link>",
        "    <ttl>1440</ttl>",
        f"    <description>{cdata(channel_description)}</description>",
    ]

    if image_url:
        parts += [
            "    <image>",
            f"      <url>{xml_escape(image_url)}</url>",
            f"      <title>{xml_escape(title)}</title>",
            f"      <link>{xml_escape(feed_link)}</link>",
            "    </image>",
        ]

    # itunes:summary uses short_description if available (that's what CRo does)
    summary = short_description or base_description
    parts += [
        f"    <itunes:summary>{xml_escape(summary)}</itunes:summary>",
        '    <itunes:category text="Leisure">',
        '      <itunes:category text="Video Games"/>',
        "    </itunes:category>",
    ]

    if image_url:
        parts.append(f'    <itunes:image href="{xml_escape(image_url)}"/>')

    parts += [
        "    <itunes:author>Český rozhlas</itunes:author>",
        "    <itunes:explicit>No</itunes:explicit>",
        "    <itunes:owner>",
        "      <itunes:email>internet@rozhlas.cz</itunes:email>",
        "    </itunes:owner>",
    ]

    # --------- episodes ---------
    skipped = 0
    missing_legacy = 0
    for idx, ep in enumerate(episodes, 1):
        a = ep.get("attributes", {})
        ep_uuid = ep.get("id", "")
        ep_title = a.get("title", "") or "(no title)"
        base_desc = a.get("description") or a.get("shortDescription") or ""

        # Append promo block with unique utm_campaign = episode UUID
        ep_promo = PROMO_SHOW.format(
            show_title=title,
            show_uuid=show_uuid,
            campaign=ep_uuid,
        )
        ep_description = base_desc + ep_promo

        audio_url, mime, _ = pick_audio(a)
        if not audio_url:
            skipped += 1
            continue

        pub = pub_date(a.get("since") or a.get("published"))
        dur = duration_hms(a.get("duration"))
        link = f"https://www.mujrozhlas.cz/rapi/view/episode/{ep_uuid}"

        length = 0
        if fetch_lengths:
            length = head_content_length(audio_url)
            if idx % 10 == 0:
                print(f"    ... {idx}/{len(episodes)}", file=sys.stderr, flush=True)

        # GUID - officially a numeric legacyId, not a UUID.
        # If missing, fall back to the UUID.
        guid = get_legacy_id(a) or ep_uuid
        if not get_legacy_id(a):
            missing_legacy += 1

        # itunes:subtitle and itunes:summary use the short description (no promo)
        ep_short = a.get("description") or a.get("shortDescription") or ""

        parts += [
            "    <item>",
            f"      <title>{xml_escape(ep_title)}</title>",
            f"      <description>{cdata(ep_description)}</description>",
            f"      <itunes:subtitle>{xml_escape(ep_short)}</itunes:subtitle>",
            f"      <itunes:summary>{xml_escape(ep_short)}</itunes:summary>",
        ]
        if dur:
            parts.append(f"      <itunes:duration>{dur}</itunes:duration>")
        if pub:
            parts.append(f"      <pubDate>{pub}</pubDate>")
        parts += [
            f'      <enclosure url="{xml_escape(audio_url)}" type="{xml_escape(mime)}" length="{length}"/>',
            f'      <guid isPermaLink="false">{xml_escape(guid)}</guid>',
            f"      <link>{xml_escape(link)}</link>",
            "    </item>",
        ]

    parts += ["  </channel>", "</rss>"]

    if skipped:
        print(f"  WARNING: {skipped} episodes without audio URL (skipped)",
              file=sys.stderr)
    if missing_legacy:
        print(f"  INFO: {missing_legacy} episodes used UUID as guid "
              f"(legacyId missing in API)", file=sys.stderr)

    return "\n".join(parts).encode("utf-8")


def atomic_write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=d, prefix=".tmp_", suffix=".rss",
                                     delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, path)


def process_show(slug, uuid, fetch_lengths=False):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {slug} ({uuid})",
          file=sys.stderr, flush=True)
    show = fetch_show(uuid)
    episodes = fetch_all_episodes(uuid)
    print(f"  -> {len(episodes)} episodes", file=sys.stderr, flush=True)
    xml = build_rss(show, episodes, fetch_lengths=fetch_lengths)
    out_path = os.path.join(OUTPUT_DIR, f"{slug}.rss")
    atomic_write(out_path, xml)
    print(f"  -> wrote {out_path} ({len(xml)} B)",
          file=sys.stderr, flush=True)
    return len(episodes)


def main():
    fetch_lengths = "--with-lengths" in sys.argv

    print(f"OUTPUT_DIR = {OUTPUT_DIR}", file=sys.stderr)
    print(f"Shows = {list(SHOWS.keys())}", file=sys.stderr)
    if fetch_lengths:
        print("Mode: with Content-Length fetching (slow)", file=sys.stderr)

    failures = []
    for slug, uuid in SHOWS.items():
        try:
            process_show(slug, uuid, fetch_lengths=fetch_lengths)
        except Exception as e:
            print(f"  !! FAILED for {slug}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            failures.append(slug)

    if failures:
        print(f"FINISHED WITH ERRORS: {failures}", file=sys.stderr)
        sys.exit(1)
    print("FINISHED.", file=sys.stderr)


if __name__ == "__main__":
    main()
