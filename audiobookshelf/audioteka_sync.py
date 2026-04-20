#!/usr/bin/env python3
"""
audioteka_sync.py
=================
Downloads your entire audioteka.com library into an Audiobookshelf-compatible
folder structure, with metadata (metadata.opf, cover.jpg, desc.txt, reader.txt).

How it works:
  1. Reads session cookies exported from your logged-in browser
     (the web login is protected by reCAPTCHA v3 so we skip it).
  2. For each book on your shelf:
       a) POST /v2/commands {"name":"RequestAudiobookDownload", ...}
          -> returns a "zip_file" URL for a signed ZIP containing MP3s.
       b) GET that URL -> ZIP with MP3 files + bookinfo.html + cover images.
       c) Extracts MP3 (keeping the original names, e.g. "01 Kapitola 1.mp3"),
          parses bookinfo.html for authoritative metadata, picks the largest
          cover image, and writes metadata.opf / desc.txt / reader.txt.

Requirements:
    pip install requests mutagen

Prepare cookies (one-time):
    1) Install the "Get cookies.txt LOCALLY" Chrome/Edge extension:
       https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
    2) Open https://audioteka.com/cz/ while logged in.
    3) Click the extension icon -> Export -> save as cookies.txt.

Usage:
    python audioteka_sync.py -c cookies.txt -o C:\\audiobooks --list
    python audioteka_sync.py -c cookies.txt -o C:\\audiobooks --skeleton
    python audioteka_sync.py -c cookies.txt -o C:\\audiobooks --limit 1 -v
    python audioteka_sync.py -c cookies.txt -o C:\\audiobooks
    python audioteka_sync.py -c cookies.txt -o C:\\audiobooks --keep-zips

Notes:
  * JWT token from cookies is valid for ~60 minutes. If it expires mid-run,
    re-export cookies.txt from the browser and rerun the script - it skips
    books that are already downloaded.
  * Book titles and author names may contain non-ASCII characters. The script
    reconfigures stdout to UTF-8 on Windows so this prints correctly in cmd.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from xml.sax.saxutils import escape


# ---------------------------------------------------------------------------
# Windows console UTF-8 setup (so non-ASCII book titles print correctly)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


try:
    import requests
except ImportError:
    print("Missing 'requests' library. Install with: pip install requests",
          file=sys.stderr)
    sys.exit(1)

try:
    from mutagen.id3 import ID3  # noqa: F401
    from mutagen.mp3 import MP3  # noqa: F401
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False


# ---------------------------------------------------------------------------
# Constants (reverse-engineered from audioteka.com web app)
# ---------------------------------------------------------------------------
BASE = "https://audioteka.com/cz/v2"
PAGE_BASE = "https://audioteka.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
X_DEVICE = f"AudiotekaWeb/1.131.1 Web/3.0 (Browser;{UA})"

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "x-audioteka-device": X_DEVICE,
    "Origin": "https://audioteka.com",
    "Referer": "https://audioteka.com/cz/policka/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("atk")

_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: Optional[str], default: str = "Unknown") -> str:
    """Return a filesystem-safe name; preserves diacritics."""
    if not name:
        return default
    name = _INVALID_FS.sub(" ", str(name))
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name or default


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class AudiotekaWeb:
    def __init__(self, cookies_path: Path):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(str(cookies_path),
                     ignore_discard=True, ignore_expires=True)
        except FileNotFoundError:
            raise SystemExit(f"Cookies file not found: {cookies_path}")
        except Exception as exc:
            raise SystemExit(
                f"Failed to load cookies: {exc}\n"
                "The file must be in Netscape (cookies.txt) format."
            )
        self.session.cookies = jar
        self._apply_jwt_from_cookies()

    def _apply_jwt_from_cookies(self) -> None:
        """Extract JWT token from cookies and attach it as Bearer header.
        Audioteka requires both the cookie AND the Authorization header."""
        candidates = ("api_token", "apiToken", "jwt_token", "jwtToken",
                      "access_token", "accessToken", "auth_token")
        jwt_val: Optional[str] = None
        for c in self.session.cookies:
            if c.name in candidates and c.value and "." in c.value:
                jwt_val = c.value
                break
        if not jwt_val:
            for c in self.session.cookies:
                if c.value and c.value.count(".") >= 2 and len(c.value) > 100:
                    jwt_val = c.value
                    log.debug("JWT-like candidate from cookie %s", c.name)
                    break

        if not jwt_val:
            log.debug("No JWT token found in cookies; requests will use "
                      "cookies only.")
            return

        self.session.headers["Authorization"] = f"Bearer {jwt_val}"

        # Decode the payload to show expiry (purely informational)
        try:
            parts = jwt_val.split(".")
            if len(parts) == 3:
                pad = "=" * (-len(parts[1]) % 4)
                payload = json.loads(
                    base64.urlsafe_b64decode(parts[1] + pad))
                exp = payload.get("exp")
                if exp:
                    rem_s = exp - int(time.time())
                    if rem_s > 0:
                        log.info("JWT token valid for another %d min",
                                 rem_s // 60)
                    else:
                        log.warning(
                            "JWT token already expired %d min ago - "
                            "re-export cookies.txt from your browser.",
                            -rem_s // 60)
        except Exception as exc:
            log.debug("JWT decode failed: %s", exc)

    def _abs(self, url_or_path: str) -> str:
        if url_or_path.startswith("http"):
            return url_or_path
        if url_or_path.startswith("/"):
            return PAGE_BASE + url_or_path
        return f"{BASE}/{url_or_path}"

    def get(self, url_or_path: str, **kw) -> requests.Response:
        return self.session.get(self._abs(url_or_path), timeout=60, **kw)

    def post_command(self, payload: dict) -> requests.Response:
        return self.session.post(
            f"{BASE}/commands",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )

    def verify(self) -> Dict[str, Any]:
        r = self.get(f"{BASE}/me")
        if r.status_code == 401:
            raise SystemExit(
                "Cookies are invalid or expired.\n"
                "  -> Log in to audioteka.com in your browser and re-export "
                "cookies.txt."
            )
        r.raise_for_status()
        return r.json()

    def iter_shelf(self) -> Iterator[Dict[str, Any]]:
        page = 1
        while True:
            r = self.get(f"{BASE}/me/shelf",
                         params={"page": page, "limit": 30,
                                 "sort": "added_at", "order": "desc"})
            r.raise_for_status()
            data = r.json()
            items = data.get("_embedded", {}).get("app:product", []) or []
            for it in items:
                yield it
            if page >= int(data.get("pages", 1)):
                return
            page += 1

    def audiobook_detail(self, link_href: str) -> Dict[str, Any]:
        r = self.get(link_href)
        r.raise_for_status()
        return r.json()

    def request_download(self, audiobook_id: str) -> Dict[str, Any]:
        """Returns {zip_file, cue, cover, ...}."""
        r = self.post_command({
            "name": "RequestAudiobookDownload",
            "audiobook_id": audiobook_id,
        })
        if r.status_code >= 400:
            raise RuntimeError(
                f"RequestAudiobookDownload -> {r.status_code}: "
                f"{r.text[:300]}"
            )
        return r.json()

    def download_binary(self, url: str, dest: Path,
                        label: str = "") -> bool:
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers: Dict[str, str] = {}
        resume_pos = 0
        if tmp.exists():
            resume_pos = tmp.stat().st_size
            headers["Range"] = f"bytes={resume_pos}-"
        try:
            with self.session.get(url, stream=True, timeout=300,
                                  headers=headers) as r:
                if r.status_code == 416:
                    tmp.rename(dest)
                    return True
                if r.status_code >= 400:
                    log.error("   HTTP %s on %s", r.status_code, url[:100])
                    return False
                total = resume_pos + int(
                    r.headers.get("Content-Length", 0) or 0)
                got = resume_pos
                last = time.time()
                mode = "ab" if resume_pos else "wb"
                with open(tmp, mode) as fh:
                    for chunk in r.iter_content(512 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last > 2.0 and total:
                            pct = got * 100 / total
                            log.info("      %s  %5.1f%%  %.1f/%.1f MB",
                                     label, pct, got / 1e6, total / 1e6)
                            last = now
            tmp.rename(dest)
            return True
        except Exception as exc:
            log.error("   Download error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Metadata model + OPF generator
# ---------------------------------------------------------------------------
@dataclass
class BookMeta:
    title: str = "Unknown Title"
    author: str = "Unknown Author"
    narrator: Optional[str] = None
    year: Optional[str] = None
    publisher: Optional[str] = None
    description: Optional[str] = None
    language: str = "ces"
    series: Optional[str] = None


def build_opf(m: BookMeta) -> str:
    parts: List[str] = []

    def add(tag: str, val: Optional[str], attrs: str = "") -> None:
        if not val:
            return
        prefix = f" {attrs}" if attrs else ""
        parts.append(f"    <{tag}{prefix}>{escape(str(val))}</{tag}>")

    add("dc:title", m.title)
    add("dc:creator", m.author,
        f'opf:role="aut" opf:file-as="{escape(m.author)}"')
    add("dc:publisher", m.publisher)
    add("dc:date", m.year)
    add("dc:language", m.language)
    add("dc:description", m.description)
    if m.narrator:
        parts.append(f'    <meta name="calibre:narrator" '
                     f'content="{escape(m.narrator)}"/>')
    if m.series:
        parts.append(f'    <meta name="calibre:series" '
                     f'content="{escape(m.series)}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'unique-identifier="BookId" version="2.0">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">\n'
        + "\n".join(parts) + "\n"
        "  </metadata>\n"
        "</package>\n"
    )


# ---------------------------------------------------------------------------
# bookinfo.html parser (authoritative metadata source from the ZIP)
# ---------------------------------------------------------------------------
_BOOKINFO_FIELDS: List[Tuple[str, str]] = [
    ("title",        r'<[^>]*id="Title"[^>]*>(.*?)</'),
    ("author",       r'<[^>]*id="Author"[^>]*>(.*?)</'),
    ("reader",       r'<[^>]*id="Reader"[^>]*>(.*?)</'),
    ("publisher",    r'<[^>]*id="Publisher"[^>]*>(.*?)</'),
    ("category",     r'<[^>]*id="CategoryName"[^>]*>(.*?)</'),
    ("description",  r'<p[^>]*id="GeneralDescription"[^>]*>(.*?)</p>'),
    ("created_date", r'<[^>]*id="CreatedDate"[^>]*>(.*?)</'),
]


def _unescape_html(s: str) -> str:
    return (s.replace("&amp;", "&")
             .replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"')
             .replace("&#160;", " ").replace("&nbsp;", " "))


def parse_bookinfo(html_text: str) -> Dict[str, Any]:
    """Extract metadata fields from bookinfo.html (shipped inside each ZIP).

    Returns a dict with string fields (title, author, reader, ...) plus
    an optional 'chapters' key – a list of dicts with keys
    'title', 'filename', 'length_ms'.
    """
    out: Dict[str, Any] = {}
    for field, pat in _BOOKINFO_FIELDS:
        m = re.search(pat, html_text, re.DOTALL)
        if m:
            val = _unescape_html(m.group(1).strip())
            if val:
                out[field] = val

    chapters: List[Dict[str, Any]] = []
    for m in re.finditer(
            r'<div[^>]+id="Chapter-\d+"[^>]*>(.*?)</div>',
            html_text, re.DOTALL):
        block = m.group(1)
        title_m = re.search(
            r'<[^>]*class="ChapterTitle"[^>]*>(.*?)</', block, re.DOTALL)
        length_m = re.search(
            r'<[^>]*class="Length"[^>]*>(.*?)</', block, re.DOTALL)
        link_m = re.search(
            r'<[^>]*class="Link"[^>]*>(.*?)</', block, re.DOTALL)
        if title_m and link_m:
            title = _unescape_html(title_m.group(1).strip())
            fname = _unescape_html(link_m.group(1).strip())
            length_ms = 0
            if length_m:
                try:
                    length_ms = int(length_m.group(1).strip())
                except ValueError:
                    pass
            chapters.append({
                "title": title,
                "filename": fname,
                "length_ms": length_ms,
            })
    if chapters:
        out["chapters"] = chapters
    return out


def extract_zip_to_target(zip_path: Path,
                          target: Path) -> Tuple[int, Dict[str, str]]:
    """Extract MP3s (keeping original names), pick the best cover, and
    parse bookinfo.html. Returns (mp3_count, bookinfo_dict)."""
    bookinfo: Dict[str, str] = {}
    n_mp3 = 0

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        log.error("   Corrupted ZIP file")
        return 0, {}

    with zf:
        members = [m for m in zf.infolist() if not m.is_dir()]

        # 1) bookinfo.html -> metadata
        for m in members:
            if Path(m.filename).name.lower() == "bookinfo.html":
                try:
                    with zf.open(m) as fh:
                        bookinfo = parse_bookinfo(
                            fh.read().decode("utf-8", errors="replace"))
                except Exception as exc:
                    log.debug("   bookinfo.html: %s", exc)
                break

        # 2) MP3 files - extract with original names (already well-numbered)
        target.mkdir(parents=True, exist_ok=True)
        for m in members:
            if not m.filename.lower().endswith(".mp3"):
                continue
            name = Path(m.filename).name
            dest = target / sanitize(name)
            if not dest.name.lower().endswith(".mp3"):
                dest = dest.with_suffix(".mp3")
            with zf.open(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            n_mp3 += 1

        # 3) Best available cover (priority: duze > srednie > male > any)
        cover_chosen = None
        for suffix in ("-duze.jpg", "-srednie.jpg", "-male.jpg", ".jpg"):
            for m in members:
                if m.filename.lower().endswith(suffix):
                    cover_chosen = m
                    break
            if cover_chosen:
                break
        if cover_chosen:
            try:
                with zf.open(cover_chosen) as src, \
                        open(target / "cover.jpg", "wb") as out:
                    shutil.copyfileobj(src, out)
            except Exception as exc:
                log.debug("   cover: %s", exc)

    return n_mp3, bookinfo


def apply_bookinfo_to_meta(meta: BookMeta, bi: Dict[str, Any]) -> None:
    """Overwrite meta fields using data from bookinfo.html (authoritative)."""
    if bi.get("title"):
        meta.title = bi["title"]
    if bi.get("author"):
        meta.author = bi["author"]
    if bi.get("reader"):
        # Audioteka separates multiple narrators with semicolons
        meta.narrator = bi["reader"].replace(";", ", ")
    if bi.get("publisher"):
        meta.publisher = bi["publisher"]
    if bi.get("description"):
        meta.description = bi["description"]
    if bi.get("created_date") and len(bi["created_date"]) >= 4:
        meta.year = bi["created_date"][:4]


def embed_id3_tags(target: Path, meta: BookMeta,
                   chapters: Optional[List[Dict[str, Any]]] = None,
                   embed_cover: bool = True) -> int:
    """Write ID3 tags (and optionally embed cover.jpg) into every MP3 in
    target. Returns the number of successfully tagged files."""
    if not HAVE_MUTAGEN:
        log.warning("   mutagen not installed, skipping ID3 embedding")
        return 0

    # Lazy import to keep the top of the file tidy
    from mutagen.id3 import (
        ID3, ID3NoHeaderError,
        TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCOM, TPUB, TCON, TLAN,
        COMM, APIC,
    )
    from mutagen.mp3 import MP3

    mp3_files = sorted(target.glob("*.mp3"))
    if not mp3_files:
        return 0
    total = len(mp3_files)

    # filename -> title map (from bookinfo.html chapters, if available)
    title_map: Dict[str, str] = {}
    if chapters:
        for ch in chapters:
            fn = ch.get("filename")
            tt = ch.get("title")
            if fn and tt:
                title_map[fn] = tt

    # Embedded cover (shared across all tracks)
    cover_data: Optional[bytes] = None
    cover_path = target / "cover.jpg"
    if embed_cover and cover_path.exists():
        try:
            cover_data = cover_path.read_bytes()
        except Exception:
            cover_data = None

    written = 0
    for idx, mp3_path in enumerate(mp3_files, start=1):
        try:
            try:
                audio = MP3(mp3_path, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(mp3_path)
                audio.add_tags()

            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags

            # Replace the frames we're about to write (avoid duplicates)
            for key in ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TDRC",
                        "TCOM", "TPUB", "TCON", "TLAN"):
                tags.delall(key)
            tags.delall("COMM")
            if embed_cover and cover_data:
                tags.delall("APIC")

            # Track title: prefer chapter title from bookinfo.html, else
            # fall back to the filename stem (e.g. "01 Kapitola 1")
            track_title = (title_map.get(mp3_path.name)
                           or mp3_path.stem)
            tags.add(TIT2(encoding=3, text=track_title))

            if meta.author:
                tags.add(TPE1(encoding=3, text=meta.author))
                tags.add(TPE2(encoding=3, text=meta.author))
            if meta.title:
                tags.add(TALB(encoding=3, text=meta.title))
            tags.add(TRCK(encoding=3, text=f"{idx}/{total}"))
            if meta.year:
                tags.add(TDRC(encoding=3, text=meta.year))
            if meta.narrator:
                tags.add(TCOM(encoding=3, text=meta.narrator))
            if meta.publisher:
                tags.add(TPUB(encoding=3, text=meta.publisher))
            if meta.language:
                tags.add(TLAN(encoding=3, text=meta.language))
            tags.add(TCON(encoding=3, text="Audiobook"))

            if meta.description:
                # Keep comment short – some players choke on very long COMM
                desc = meta.description[:500]
                tags.add(COMM(encoding=3, lang="ces", desc="", text=desc))

            if embed_cover and cover_data:
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                              desc="Cover", data=cover_data))

            audio.save()
            written += 1
        except Exception as exc:
            log.warning("   ID3 write failed for %s: %s",
                        mp3_path.name, exc)

    return written


def parse_opf_to_meta(opf_path: Path) -> Optional[BookMeta]:
    """Read metadata.opf and populate a BookMeta. Used by --retag mode
    when the ZIP was already deleted but metadata.opf is on disk."""
    try:
        text = opf_path.read_text(encoding="utf-8")
    except Exception:
        return None

    def get(tag: str) -> Optional[str]:
        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', text, re.DOTALL)
        if m:
            return _unescape_html(m.group(1).strip())
        return None

    def meta_attr(name: str) -> Optional[str]:
        m = re.search(rf'<meta[^>]+name="{name}"[^>]+content="([^"]+)"', text)
        return _unescape_html(m.group(1)) if m else None

    meta = BookMeta()
    t = get("dc:title");        meta.title = t or meta.title
    a = get("dc:creator");      meta.author = a or meta.author
    meta.publisher = get("dc:publisher")
    meta.year = get("dc:date")
    meta.language = get("dc:language") or "ces"
    meta.description = get("dc:description")
    meta.narrator = meta_attr("calibre:narrator")
    meta.series = meta_attr("calibre:series")
    return meta


# ---------------------------------------------------------------------------
# Per-book processing
# ---------------------------------------------------------------------------
def process_product(api: AudiotekaWeb, product: Dict[str, Any],
                    root: Path, *, skeleton_only: bool,
                    dry_run: bool, force: bool,
                    keep_zips_dir: Optional[Path],
                    no_embed: bool,
                    retag_mode: bool) -> bool:
    name = product.get("name") or "Unknown"
    # Shelf API returns the author's name in the "description" field
    author = product.get("description") or "Unknown Author"
    image_url = product.get("image_url")
    pid = product.get("id")

    links = product.get("_links", {})
    ab_link = (links.get("app:audiobook") or {}).get("href")

    log.info("[book] %s - %s", author, name)

    safe_author = sanitize(author)
    safe_title = sanitize(name)
    target = root / safe_author / safe_title

    if dry_run:
        log.info("   [dry-run] would be saved to %s", target)
        return True

    target.mkdir(parents=True, exist_ok=True)

    # ----- initial metadata (from shelf + detail endpoints) -----
    meta = BookMeta(title=name, author=author, language="ces")

    # In --retag mode, prefer metadata.opf that's already on disk – it was
    # generated from bookinfo.html which is the authoritative source.
    if retag_mode:
        existing_opf = target / "metadata.opf"
        if existing_opf.exists():
            opf_meta = parse_opf_to_meta(existing_opf)
            if opf_meta:
                meta = opf_meta
                log.debug("   loaded metadata from %s", existing_opf)

    if ab_link and not retag_mode:
        try:
            detail = api.audiobook_detail(ab_link)
            if detail.get("description"):
                meta.description = re.sub(r"<[^>]+>", "",
                                          str(detail["description"])).strip()
            for k in ("release_date", "publication_date", "published_at"):
                if detail.get(k):
                    meta.year = str(detail[k])[:4]
                    break
            lectors = (detail.get("lectors") or detail.get("narrators")
                       or detail.get("lector"))
            if isinstance(lectors, list) and lectors:
                first = lectors[0]
                meta.narrator = (first.get("name") if isinstance(first, dict)
                                 else str(first))
            meta.publisher = (detail.get("publisher_name")
                              or detail.get("publisher"))
            authors = (detail.get("authors")
                       or detail.get("_embedded", {}).get("app:author"))
            if isinstance(authors, list) and authors:
                a0 = authors[0]
                meta.author = (a0.get("name") if isinstance(a0, dict)
                               else str(a0))
        except Exception as exc:
            log.debug("   detail fetch failed: %s", exc)

    # ----- fallback cover from API (replaced later by the ZIP version) -----
    if image_url and not (target / "cover.jpg").exists():
        try:
            cover_url = re.sub(r"\?.*$", "", image_url) + "?w=600"
            r = api.session.get(cover_url, timeout=60)
            if r.status_code == 200 and r.content:
                (target / "cover.jpg").write_bytes(r.content)
        except Exception as exc:
            log.debug("   cover: %s", exc)

    (target / "metadata.opf").write_text(build_opf(meta), encoding="utf-8")
    if meta.description:
        (target / "desc.txt").write_text(meta.description, encoding="utf-8")
    if meta.narrator:
        (target / "reader.txt").write_text(meta.narrator, encoding="utf-8")

    if skeleton_only:
        log.info("   [ok] skeleton saved")
        return True

    # ----- already downloaded? -----
    existing_mp3 = list(target.glob("*.mp3"))

    if retag_mode:
        # Retag-only mode: no download, just write ID3 to existing MP3s.
        if not existing_mp3:
            log.info("   [skip] no MP3 files found, nothing to retag")
            return True
        # Pull latest meta from metadata.opf if we just regenerated it above;
        # it's already correct. Chapters are not available in retag mode
        # (bookinfo.html is inside the ZIP we no longer have) – fall back to
        # filename-based titles, which matches what audioteka uses anyway.
        log.info("   [retag] tagging %d MP3 files", len(existing_mp3))
        n = embed_id3_tags(target, meta, chapters=None,
                           embed_cover=not no_embed)
        log.info("   [done] %d files tagged", n)
        return True

    if existing_mp3 and not force:
        log.info("   [skip] %d MP3 files already present (use --force "
                 "to redownload, --retag to refresh ID3 tags only)",
                 len(existing_mp3))
        return True

    # ----- request ZIP download URL -----
    if not pid:
        log.warning("   no book id, skipping")
        return False

    try:
        download_info = api.request_download(pid)
    except Exception as exc:
        log.error("   RequestAudiobookDownload failed: %s", exc)
        return False

    zip_file_path = download_info.get("zip_file")
    if not zip_file_path:
        log.error("   response did not contain 'zip_file': %s",
                  json.dumps(download_info)[:300])
        return False

    zip_url = api._abs(zip_file_path)
    log.info("   [dl] %s", zip_file_path)

    # Where to store the ZIP
    if keep_zips_dir:
        keep_zips_dir.mkdir(parents=True, exist_ok=True)
        zip_path = keep_zips_dir / f"{safe_author} - {safe_title}.zip"
    else:
        zip_path = Path(tempfile.mkdtemp(prefix="atk-zip-")) / "book.zip"

    try:
        if not zip_path.exists() or force:
            if not api.download_binary(zip_url, zip_path,
                                       label=safe_title[:30]):
                return False
        else:
            log.info("   ZIP already exists (%.1f MB), reusing",
                     zip_path.stat().st_size / 1e6)

        log.info("   [extract]")
        n, bookinfo = extract_zip_to_target(zip_path, target)
        if n == 0:
            log.error("   extraction failed")
            return False

        # Overwrite metadata with bookinfo.html data (authoritative)
        chapters = bookinfo.get("chapters") if bookinfo else None
        if bookinfo:
            log.debug("   bookinfo.html fields: %s",
                      [k for k in bookinfo.keys() if k != "chapters"])
            apply_bookinfo_to_meta(meta, bookinfo)
            (target / "metadata.opf").write_text(build_opf(meta),
                                                 encoding="utf-8")
            if meta.description:
                (target / "desc.txt").write_text(meta.description,
                                                 encoding="utf-8")
            if meta.narrator:
                (target / "reader.txt").write_text(meta.narrator,
                                                   encoding="utf-8")

        # Embed ID3 tags (+ cover) into each MP3 – makes the files
        # self-describing when used outside Audiobookshelf.
        if not no_embed:
            log.info("   [tag] writing ID3 tags + embedded cover")
            tagged = embed_id3_tags(target, meta, chapters=chapters)
            log.info("   [done] %d tracks, %d tagged", n, tagged)
        else:
            log.info("   [done] %d tracks", n)
        return True
    finally:
        # Delete temp ZIP (unless the user asked to keep them)
        if not keep_zips_dir and zip_path.exists():
            try:
                zip_path.unlink()
                zip_path.parent.rmdir()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=("Download your audioteka.com library (CZ) into "
                     "Audiobookshelf structure. Uses browser cookies to "
                     "bypass reCAPTCHA login."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("-c", "--cookies", required=True, type=Path,
                    help="Netscape cookies.txt exported from your browser")
    ap.add_argument("-o", "--output", required=True, type=Path,
                    help="Root directory of the Audiobookshelf library")
    ap.add_argument("--list", action="store_true",
                    help="Only list books on the shelf, do nothing else")
    ap.add_argument("--skeleton", action="store_true",
                    help="Only create metadata.opf and cover.jpg; skip MP3")
    ap.add_argument("--keep-zips", metavar="DIR", type=Path, nargs="?",
                    const=Path("./audioteka_zips"), default=None,
                    help="Keep downloaded ZIPs (default dir: ./audioteka_zips)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing MP3 files / re-download ZIPs")
    ap.add_argument("--retag", action="store_true",
                    help="Do not download anything; only refresh ID3 tags "
                         "and embedded cover of already-downloaded books")
    ap.add_argument("--no-embed", action="store_true",
                    help="Do not write ID3 tags / embed cover into MP3 files")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max number of books to process (0 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write anything, only print what would happen")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Enable debug-level logging")
    args = ap.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    api = AudiotekaWeb(args.cookies)
    me = api.verify()
    uid = me.get("id") or me.get("user_id") or "?"
    log.info("Cookies OK (user id %s)", str(uid)[:12])

    args.output.mkdir(parents=True, exist_ok=True)

    shelf = list(api.iter_shelf())
    log.info("Shelf contains %d book(s)", len(shelf))

    if args.list:
        for i, p in enumerate(shelf, 1):
            print(f"{i:3d}. {p.get('description') or '?'} - {p.get('name')}")
        return 0

    ok = 0
    done = 0
    for p in shelf:
        if args.limit and done >= args.limit:
            break
        done += 1
        try:
            if process_product(api, p, args.output,
                               skeleton_only=args.skeleton,
                               dry_run=args.dry_run,
                               force=args.force,
                               keep_zips_dir=args.keep_zips,
                               no_embed=args.no_embed,
                               retag_mode=args.retag):
                ok += 1
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            break
        except Exception:
            log.exception("Error while processing %s", p.get("name"))

    log.info("--- Finished: %d / %d ---", ok, done)
    return 0 if ok == done else 2


if __name__ == "__main__":
    sys.exit(main())
