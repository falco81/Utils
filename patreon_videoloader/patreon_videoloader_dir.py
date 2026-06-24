"""Download all videos from a Patreon collection (Vimeo-hosted, HLS) into MP4 files.

Patreon collection URL  ->  /api/posts  ->  Vimeo embeds (id + privacy hash)
                        ->  Vimeo player config  ->  HLS master playlist
                        ->  best video + audio media playlists  ->  ffmpeg mux -> .mp4

These Patreon-hosted Vimeo videos are HLS-only with SEPARATE audio and video tracks,
so muxing requires ffmpeg (a pure-Python mux is not practical). Install ffmpeg and make
sure it is on PATH (or set FFMPEG below).

Shares the conveniences of the Drive downloader: auto/multi cookie files, interactive
--select, parallel downloads, per-file locks, atomic output, colored aligned progress.
"""

from urllib.parse import unquote, unquote_plus, urljoin, urlparse, parse_qs, urlencode
import requests
import argparse
import sys
import os
import re
import json
import math
import shutil
import threading
import subprocess
import tempfile
from http.cookiejar import MozillaCookieJar
from requests.cookies import RequestsCookieJar
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    print("This script needs tqdm:  pip install tqdm", file=sys.stderr)
    raise
try:
    import colorama
    _HAS_COLORAMA = True
except Exception:
    _HAS_COLORAMA = False

# =============================================================================
#  USER CONFIG — edit these defaults. The command line wins.
# =============================================================================
DEFAULT_VIDEO_WORKERS = 3         # -w : (legacy) kept for compatibility; segment pool uses -m
DEFAULT_MAX_CONNECTIONS = 64      # -m : shared pool of parallel segment connections
DEFAULT_MAX_HEIGHT = 0            # -q : 0 = best available; else cap height (e.g. 720)
FFMPEG = "ffmpeg"                 # ffmpeg executable, a FOLDER containing it, or "ffmpeg" if on PATH.
                                  # Windows: use forward slashes or a raw string to avoid backslash
                                  # escapes, e.g. "C:/Program Files/ffmpeg" or r"C:\Program Files\ffmpeg".
# If ffmpeg is not found, download it from this URL (zip or tar.*), extract next to the
# script into ./.ffmpeg/ and use it. Leave "" to disable auto-download.
# Windows build (default). For Linux use e.g.
#   https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
AUTO_COOKIES = True               # auto-use a *.json cookie file found nearby
USE_COLOR = True
FORCE_ASCII_BARS = None           # None = auto (ASCII on Windows); True/False to force
PER_FILE_BAR_LIMIT = 32
BAR_NCOLS = 100
BAR_DESC_WIDTH = 26
PATREON_REFERER = "https://www.patreon.com/"
VIMEO_REFERER = "https://player.vimeo.com/"
VIMEO_HEADERS = {'Referer': VIMEO_REFERER, 'Origin': 'https://player.vimeo.com'}
MUX_HEADERS = {'Referer': PATREON_REFERER}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# =============================================================================

ASCII_BARS = False
BAR_FORMAT = '{desc} {percentage:3.0f}% |{bar}| {n_fmt:>7}/{total_fmt:>7} {rate_fmt:>10}'


# --------------------------------------------------------------------------- #
#  Console / colour (shared with the Drive downloader)
# --------------------------------------------------------------------------- #
class _Palette:
    def __init__(self, enabled):
        if enabled:
            self.RESET = '\033[0m'; self.RED = '\033[31m'; self.GREEN = '\033[32m'
            self.YELLOW = '\033[33m'; self.CYAN = '\033[36m'; self.DIM = '\033[2m'
        else:
            self.RESET = self.RED = self.GREEN = self.YELLOW = self.CYAN = self.DIM = ''


CLR = _Palette(False)
_builtin_print = print


def _cprint(*args, **kwargs):
    if CLR.RESET and args and isinstance(args[0], str):
        s = args[0]; st = s.lstrip(); color = ''
        if st.startswith('[ERROR]'):
            color = CLR.RED
        elif st.startswith('[WARN]'):
            color = CLR.YELLOW
        elif st.startswith('[INFO]'):
            color = CLR.CYAN
        elif 'downloaded successfully' in s or 'done' in s.lower():
            color = CLR.GREEN
        if color:
            args = (color + s + CLR.RESET,) + args[1:]
    _builtin_print(*args, **kwargs)


print = _cprint


def _enable_windows_vt():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for h in (-11, -12):
            handle = k.GetStdHandle(h); mode = ctypes.c_uint32()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                k.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def setup_console(use_color):
    global CLR, ASCII_BARS
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    is_windows = os.name == 'nt'
    enabled = use_color and bool(getattr(sys.stdout, 'isatty', lambda: False)())
    if enabled and is_windows:
        if _HAS_COLORAMA:
            try:
                colorama.just_fix_windows_console()
            except Exception:
                try:
                    colorama.init()
                except Exception:
                    enabled = False
        elif not _enable_windows_vt():
            enabled = False
    use_ascii = is_windows if FORCE_ASCII_BARS is None else FORCE_ASCII_BARS
    ASCII_BARS = ' =' if use_ascii else False
    CLR = _Palette(enabled)


def _fit_desc(name, width):
    name = str(name)
    if len(name) <= width:
        return name.ljust(width)
    keep = width - 2; head = (keep + 1) // 2; tail = keep - head
    return name[:head] + '..' + (name[-tail:] if tail > 0 else '')


def _bar_ncols():
    try:
        cols = shutil.get_terminal_size((BAR_NCOLS, 20)).columns
    except Exception:
        cols = BAR_NCOLS
    return max(40, min(BAR_NCOLS, cols - 1))


def make_bar(**kwargs):
    if kwargs.get('desc') is not None:
        kwargs['desc'] = _fit_desc(kwargs['desc'], BAR_DESC_WIDTH)
    kwargs.setdefault('ascii', ASCII_BARS)
    kwargs.setdefault('ncols', _bar_ncols())
    kwargs.setdefault('bar_format', BAR_FORMAT)
    return tqdm(**kwargs)


# --------------------------------------------------------------------------- #
#  Cookies / session (shared with the Drive downloader)
# --------------------------------------------------------------------------- #
def load_cookies_from_file(cookies_file):
    if not os.path.exists(cookies_file):
        raise FileNotFoundError(f"Cookies file not found: {cookies_file}")
    with open(cookies_file, 'r', encoding='utf-8') as f:
        content = f.read()
    stripped = content.lstrip()
    if stripped.startswith('[') or stripped.startswith('{'):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON cookies file: {cookies_file}") from exc
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            cookies_list = data["cookies"]
        elif isinstance(data, list):
            cookies_list = data
        else:
            raise ValueError(f"Unsupported JSON cookies format: {cookies_file}")
        jar = RequestsCookieJar()
        for c in cookies_list:
            if not isinstance(c, dict):
                continue
            name = c.get("name"); value = c.get("value")
            if not name or value is None:
                continue
            jar.set(name, value, domain=c.get("domain") or "", path=c.get("path") or "/",
                    expires=c.get("expirationDate") or c.get("expires"))
        return jar

    gen = None
    if not stripped.startswith('# Netscape HTTP Cookie File') and not stripped.startswith('# HTTP Cookie File'):
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write('# Netscape HTTP Cookie File\n\n'); tmp.write(content); tmp.close()
        gen = tmp.name; cookies_file = gen
    jar = MozillaCookieJar(cookies_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    finally:
        if gen and os.path.exists(gen):
            os.remove(gen)
    rj = RequestsCookieJar()
    for c in jar:
        rj.set(c.name, c.value, domain=c.domain, path=c.path)
    return rj


def _looks_like_cookies_json(path):
    try:
        if os.path.getsize(path) > 5 * 1024 * 1024:
            return False
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return False
    if isinstance(data, dict) and isinstance(data.get('cookies'), list):
        data = data['cookies']
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    return isinstance(first, dict) and 'name' in first and 'value' in first


def _format_mtime(path):
    import datetime
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return '?'


def _prompt_cookie_choice(candidates):
    print(f"[INFO] Found {len(candidates)} JSON cookie files in the directory:")
    for i, p in enumerate(candidates, 1):
        print(f"   {CLR.CYAN}{i}{CLR.RESET}) {os.path.basename(p)}   {CLR.DIM}({_format_mtime(p)}){CLR.RESET}")
    print("Select: number(s) like '1,3', 'a' for ALL, or Enter for the newest (1).")
    for _ in range(3):
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(""); return [candidates[0]]
        if raw == '':
            return [candidates[0]]
        if raw in ('a', 'all'):
            return list(candidates)
        try:
            picks = []
            for part in raw.replace(' ', '').split(','):
                if not part:
                    continue
                idx = int(part)
                if 1 <= idx <= len(candidates):
                    if candidates[idx - 1] not in picks:
                        picks.append(candidates[idx - 1])
                else:
                    raise ValueError
            if picks:
                return picks
        except ValueError:
            pass
        print(f"[WARN] Invalid choice. Enter number(s) 1-{len(candidates)}, 'a', or Enter.")
    return [candidates[0]]


def auto_detect_cookies(verbose):
    import glob

    def _norm(p):
        return os.path.normcase(os.path.realpath(p))

    dirs = []
    seen_dirs = set()
    for d in (os.getcwd(), os.path.dirname(os.path.abspath(sys.argv[0] or '.'))):
        if not d:
            continue
        key = _norm(d)
        if key in seen_dirs:
            continue
        seen_dirs.add(key); dirs.append(d)

    candidates = []
    seen = set()
    for d in dirs:
        for path in glob.glob(os.path.join(d, '*.json')):
            key = _norm(path)
            if key in seen:
                continue
            seen.add(key)
            real = os.path.abspath(path)
            if _looks_like_cookies_json(real):
                candidates.append(real)
    if not candidates:
        return []
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if len(candidates) == 1:
        print(f"[INFO] Auto-using cookies file: {os.path.basename(candidates[0])}")
        return [candidates[0]]
    interactive = bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
        bool(getattr(sys.stdout, 'isatty', lambda: False)())
    if not interactive:
        print(f"[INFO] Found {len(candidates)} JSON cookie files; non-interactive, using newest: "
              f"{os.path.basename(candidates[0])}")
        return [candidates[0]]
    chosen = _prompt_cookie_choice(candidates)
    print(f"[INFO] Using {len(chosen)} cookie file(s): {', '.join(os.path.basename(p) for p in chosen)}")
    return chosen


def _build_adapter():
    if Retry is not None:
        retry = Retry(total=4, connect=4, read=2, backoff_factor=0.6,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(('GET', 'HEAD')), raise_on_status=False)
    else:
        retry = 4
    return HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)


def get_cookies_session(cookies_files=None):
    session = requests.Session()
    if isinstance(cookies_files, str):
        cookies_files = [cookies_files]
    cookies_files = [f for f in (cookies_files or []) if f]
    if cookies_files:
        merged = RequestsCookieJar()
        for cf in cookies_files:
            for c in load_cookies_from_file(cf):
                merged.set_cookie(c)
        session.cookies = merged
    session.headers.update({'User-Agent': USER_AGENT,
                            'Accept': 'application/json, text/plain, */*',
                            'Accept-Language': 'en-US,en;q=0.9'})
    adapter = _build_adapter()
    session.mount('https://', adapter); session.mount('http://', adapter)
    return session


# --------------------------------------------------------------------------- #
#  Per-file lock + filename helpers (shared with the Drive downloader)
# --------------------------------------------------------------------------- #
def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def acquire_lock(lock_path):
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w') as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                with open(lock_path) as f:
                    owner = int((f.read().strip() or "-1"))
            except (ValueError, OSError):
                owner = -1
            if owner != os.getpid() and not _pid_alive(owner):
                try:
                    os.remove(lock_path); continue
                except OSError:
                    pass
            print(f"[WARN] Already downloading elsewhere (lock {os.path.basename(lock_path)}); skipping.")
            return False


def release_lock(lock_path):
    try:
        with open(lock_path) as f:
            owner = int((f.read().strip() or "-1"))
        if owner == os.getpid():
            os.remove(lock_path)
    except (ValueError, OSError):
        pass


def safe_filename(name, fallback):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', str(name))
    name = re.sub(r'[. ]+$', '', name)
    return name or str(fallback)


# --------------------------------------------------------------------------- #
#  Interactive --select (shared with the Drive downloader)
# --------------------------------------------------------------------------- #
def _parse_index_selection(raw, n):
    raw = raw.replace(' ', '').lower()
    if raw in ('a', 'all', ''):
        return list(range(1, n + 1))
    if raw in ('q', 'quit', 'none', '0'):
        return []
    picked = []
    for part in raw.split(','):
        if not part:
            continue
        if '-' in part:
            a, _, b = part.partition('-')
            if not (a.isdigit() and b.isdigit()):
                return None
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            if a < 1 or b > n:
                return None
            for i in range(a, b + 1):
                if i not in picked:
                    picked.append(i)
        else:
            if not part.isdigit():
                return None
            i = int(part)
            if not (1 <= i <= n):
                return None
            if i not in picked:
                picked.append(i)
    return picked


def _prompt_file_selection(videos):
    print(f"[INFO] {len(videos)} file(s) found:")
    for i, v in enumerate(videos, 1):
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {v['title']}")
    print("Select which to download: e.g. '1,3,5-8', 'a' or Enter for ALL, 'q' to cancel.")
    for _ in range(3):
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(""); return []
        sel = _parse_index_selection(raw, len(videos))
        if sel is None:
            print(f"[WARN] Invalid input. Use numbers/ranges 1-{len(videos)}, 'a', or 'q'.")
            continue
        return [videos[i - 1] for i in sel]
    return []


# --------------------------------------------------------------------------- #
#  Patreon collection enumeration
# --------------------------------------------------------------------------- #
def extract_collection_id(url_or_id):
    m = re.search(r'/collection/(\d+)', url_or_id)
    if m:
        return m.group(1)
    if url_or_id.isdigit():
        return url_or_id
    return None


def get_collection_info(collection_id, session, verbose):
    """Return (title, campaign_id) for a collection."""
    url = (f"https://www.patreon.com/api/collection/{collection_id}"
           f"?json-api-version=1.0&json-api-use-default-includes=false")
    r = session.get(url, headers={'Referer': PATREON_REFERER})
    title, campaign_id = None, None
    try:
        d = r.json()
        attrs = d.get('data', {}).get('attributes', {})
        title = attrs.get('title')
        # campaign id appears in cover-media URLs: .../p/campaign/<id>/...
        m = re.search(r'/campaign/(\d+)/', r.text)
        if m:
            campaign_id = m.group(1)
    except Exception:
        pass
    if verbose:
        print(f"[INFO] Collection '{title}', campaign {campaign_id}")
    return title, campaign_id


def list_collection_videos(collection_id, campaign_id, session, verbose):
    """Return a list of video descriptors for the collection. Each is either
    {'source':'vimeo', 'title', 'vimeo_id', 'vimeo_hash'} or
    {'source':'mux', 'title', 'master_url'} (Patreon-hosted / "embedded" videos)."""
    params = {
        'include': 'collections,drop,primary_image,audio,video,embed',
        'fields[primary-image]': 'is_fallback,image_small,image_medium,prefer_alternate_display',
        'sort': 'collection_order',
        'filter[collection_id]': collection_id,
        'filter[is_suspended]': 'false',
        'filter[include_drops]': 'true',
        'filter[is_published]': 'true',
        'page[size]': '999',
        'json-api-version': '1.0',
        'json-api-use-default-includes': 'false',
    }
    if campaign_id:
        params['filter[campaign_id]'] = campaign_id
    url = "https://www.patreon.com/api/posts?" + urlencode(params)
    r = session.get(url, headers={'Referer': PATREON_REFERER})
    try:
        d = r.json()
    except Exception:
        print(f"[ERROR] Patreon API did not return JSON (status {r.status_code}). "
              f"Cookies may be missing/expired.")
        return []
    posts = d.get('data', [])
    videos = []
    for p in posts if isinstance(posts, list) else []:
        a = p.get('attributes', {})
        post_type = a.get('post_type')

        # 1) Patreon-hosted ("embedded") video on Mux: the signed HLS master URL is ready to use.
        pf = a.get('post_file') or {}
        pf_url = pf.get('url', '') or ''
        playback = ((a.get('post_metadata') or {}).get('playback_data') or {})
        if 'stream.mux.com' in pf_url:
            mux_url = pf_url
        elif playback.get('playback_id') and playback.get('playback_token'):
            mux_url = (f"https://stream.mux.com/{playback['playback_id']}.m3u8"
                       f"?token={playback['playback_token']}")
        else:
            mux_url = None
        if mux_url and post_type in (None, 'video_external_file', 'video_file', 'video'):
            title = (a.get('title') or str(p.get('id'))).strip()
            videos.append({'source': 'mux', 'title': title, 'master_url': mux_url})
            continue

        # 2) Vimeo embed: extract id + privacy hash.
        emb = a.get('embed') or {}
        url_field = emb.get('url', '') or ''
        html = emb.get('html', '') or ''
        m = re.search(r'vimeo\.com/(\d+)/([0-9a-zA-Z]+)', url_field)
        if not m:
            m = re.search(r'player\.vimeo\.com/video/(\d+)\?h=([0-9a-zA-Z]+)', html)
        if m:
            title = (emb.get('subject') or a.get('title') or m.group(1)).strip()
            videos.append({'source': 'vimeo', 'title': title,
                           'vimeo_id': m.group(1), 'vimeo_hash': m.group(2)})
            continue

        if verbose and post_type and 'video' in str(post_type):
            print(f"[WARN] Unrecognized video post (type {post_type}): {a.get('title')}")
    return videos


def resolve_master(video, session, verbose):
    """Resolve a video descriptor to (hls_master_url, headers). Returns (None, {}) on failure."""
    if video.get('source') == 'mux':
        return video.get('master_url'), MUX_HEADERS
    # default: vimeo
    master, _title, _dur = resolve_vimeo(video['vimeo_id'], video['vimeo_hash'], session, verbose)
    return master, VIMEO_HEADERS


# --------------------------------------------------------------------------- #
#  Vimeo resolution: embed -> player config -> HLS master -> media playlists
# --------------------------------------------------------------------------- #
def _extract_player_config(html):
    """Extract the playerConfig JSON object from a Vimeo iframe page."""
    idx = html.find('playerConfig')
    if idx < 0:
        return None
    start = html.find('{', idx)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i + 1])
                    except Exception:
                        return None
    return None


def resolve_vimeo(vimeo_id, vimeo_hash, session, verbose):
    """Return (hls_master_url, title, duration_seconds) or (None, None, 0)."""
    embed = f"https://player.vimeo.com/video/{vimeo_id}?h={vimeo_hash}"
    r = session.get(embed, headers={'Referer': PATREON_REFERER, 'User-Agent': USER_AGENT})
    if r.status_code != 200:
        if verbose:
            print(f"[WARN] Vimeo player returned {r.status_code} for {vimeo_id}")
        return None, None, 0
    cfg = _extract_player_config(r.text)
    if not cfg:
        return None, None, 0
    req = cfg.get('request', {})
    files = req.get('files', {})
    hls = files.get('hls', {})
    cdns = hls.get('cdns', {})
    cdn = hls.get('default_cdn') or (next(iter(cdns)) if cdns else None)
    master = cdns.get(cdn, {}).get('url') if cdn else None
    vid = cfg.get('video', {})
    title = vid.get('title')
    duration = vid.get('duration') or 0
    return master, title, duration


def parse_master_playlist(master_url, session, max_height, headers, verbose):
    """Fetch the HLS master and return (video_url, audio_url_or_None) for the chosen quality."""
    r = session.get(master_url, headers=headers)
    if r.status_code != 200:
        if verbose:
            print(f"[WARN] HLS master returned {r.status_code}")
        return None, None
    text = r.text
    lines = text.splitlines()

    audio_url = None
    am = re.search(r'#EXT-X-MEDIA:TYPE=AUDIO[^\n]*URI="([^"]+)"', text)
    if am:
        audio_url = urljoin(master_url, am.group(1))

    variants = []  # (height, bandwidth, url)
    for i, line in enumerate(lines):
        if line.startswith('#EXT-X-STREAM-INF'):
            res = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            bw = re.search(r'BANDWIDTH=(\d+)', line)
            height = int(res.group(2)) if res else 0
            band = int(bw.group(1)) if bw else 0
            uri = ''
            for j in range(i + 1, len(lines)):
                if lines[j] and not lines[j].startswith('#'):
                    uri = lines[j].strip()
                    break
            if uri:
                variants.append((height, band, urljoin(master_url, uri)))
    if not variants:
        return None, None

    variants.sort(key=lambda v: (v[0], v[1]))
    if max_height and max_height > 0:
        eligible = [v for v in variants if v[0] <= max_height]
        chosen = eligible[-1] if eligible else variants[0]
    else:
        chosen = variants[-1]
    if verbose:
        print(f"[INFO] Selected video {chosen[0]}p, audio={'yes' if audio_url else 'muxed'}")
    return chosen[2], audio_url


# --------------------------------------------------------------------------- #
#  ffmpeg download + mux
# --------------------------------------------------------------------------- #
def _resolve_ffmpeg(value):
    """Turn a config value into a runnable ffmpeg path.

    Accepts an executable path, a FOLDER containing ffmpeg(.exe) (optionally in a bin/
    subfolder), or a bare command name resolved via PATH. Returns None if a given folder
    has no ffmpeg inside."""
    if not value:
        return None
    name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    if os.path.isdir(value):
        for cand in (os.path.join(value, name), os.path.join(value, 'bin', name)):
            if os.path.isfile(cand):
                return cand
        return None
    return value  # a file path, or a bare command name found on PATH


def _try_ffmpeg(path):
    if not path:
        return False
    try:
        subprocess.run([path, '-version'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def _auto_connections():
    """Pick a connection budget from the CPU count. Downloads are I/O-bound, so we scale
    above the logical CPU count (more in-flight segments than cores), capped to stay polite
    to Vimeo/Mux."""
    logical = os.cpu_count() or 4
    return max(16, min(128, logical * 4)), logical


def ffmpeg_available():
    return _try_ffmpeg(_resolve_ffmpeg(FFMPEG))


def _ffmpeg_cache_dir():
    base = os.path.dirname(os.path.abspath(sys.argv[0] or '.')) or os.getcwd()
    return os.path.join(base, '.ffmpeg')


def _find_cached_ffmpeg():
    name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    cache = _ffmpeg_cache_dir()
    if not os.path.isdir(cache):
        return None
    for root, _dirs, files in os.walk(cache):
        if name in files:
            p = os.path.join(root, name)
            if os.name != 'nt':
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
            return p
    return None


def _extract_archive(path, dest, url):
    lower = (url or path).lower()
    import zipfile
    import tarfile
    if lower.endswith('.zip') or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            t.extractall(dest)
    else:
        raise ValueError("Unknown ffmpeg archive format (expected .zip or .tar.*)")


def _download_and_extract_ffmpeg(url, verbose):
    cache = _ffmpeg_cache_dir()
    os.makedirs(cache, exist_ok=True)
    print(f"[INFO] ffmpeg not found; downloading from {url}")
    tmp = os.path.join(cache, 'ffmpeg_download.tmp')
    with requests.get(url, stream=True, headers={'User-Agent': USER_AGENT}, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0) or 0)
        bar = make_bar(total=total or 1, unit='B', unit_scale=True, desc='ffmpeg', disable=(total == 0))
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(256 * 1024):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
        bar.close()
    print("[INFO] Extracting ffmpeg ...")
    _extract_archive(tmp, cache, url)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return _find_cached_ffmpeg()


def ensure_ffmpeg(verbose):
    """Make sure FFMPEG points to a working ffmpeg, downloading it if needed."""
    global FFMPEG
    resolved = _resolve_ffmpeg(FFMPEG)
    if resolved and _try_ffmpeg(resolved):
        FFMPEG = resolved
        if verbose and resolved != FFMPEG:
            print(f"[INFO] Using ffmpeg: {resolved}")
        return True
    cached = _find_cached_ffmpeg()
    if cached and _try_ffmpeg(cached):
        FFMPEG = cached
        if verbose:
            print(f"[INFO] Using cached ffmpeg: {cached}")
        return True
    if FFMPEG_DOWNLOAD_URL:
        try:
            path = _download_and_extract_ffmpeg(FFMPEG_DOWNLOAD_URL, verbose)
        except Exception as e:
            print(f"[ERROR] ffmpeg download/extract failed: {e}")
            return False
        if path and _try_ffmpeg(path):
            FFMPEG = path
            print(f"[INFO] Using downloaded ffmpeg: {path}")
            return True
        print("[ERROR] Downloaded archive but could not find a working ffmpeg binary inside.")
    return False


def parse_media_playlist(url, session, headers):
    """Fetch an HLS media playlist and return (init_segment_url_or_None, [segment_urls])."""
    r = session.get(url, headers=headers)
    if r.status_code != 200:
        return None, []
    init = None
    segs = []
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith('#EXT-X-MAP:'):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init = urljoin(url, m.group(1))
        elif line and not line.startswith('#'):
            segs.append(urljoin(url, line))
    return init, segs


# Thread-local sessions for the segment pool (one connection set per worker).
_seg_tls = threading.local()
_seg_sessions = []
_seg_sessions_lock = threading.Lock()


def _seg_session(base):
    s = getattr(_seg_tls, 'session', None)
    if s is None:
        s = requests.Session()
        s.cookies.update(base.cookies)
        s.headers.update({'User-Agent': USER_AGENT})
        adapter = _build_adapter()
        s.mount('https://', adapter)
        s.mount('http://', adapter)
        _seg_tls.session = s
        with _seg_sessions_lock:
            _seg_sessions.append(s)
    return s


def _download_hls_segment(url, path, session, headers):
    """Download one HLS segment to `path` (atomic). Skips if already present."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
    for attempt in range(3):
        try:
            with session.get(url, stream=True, headers=headers) as r:
                if r.status_code != 200:
                    if attempt < 2:
                        continue
                    return False
                tmp = path + '.tmp'
                with open(tmp, 'wb') as f:
                    for chunk in r.iter_content(256 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, path)
                return True
        except requests.RequestException:
            if attempt < 2:
                continue
            return False
    return False


def _concat_stream(parts, out_file):
    """Concatenate the init segment + media segments (in order) into one file."""
    with open(out_file, 'wb') as out:
        for p in parts:
            with open(p, 'rb') as f:
                shutil.copyfileobj(f, out, length=8 * 1024 * 1024)


def _ffmpeg_mux(video_file, audio_file, out_path, verbose):
    """Mux already-downloaded local stream files into out_path. Returns (ok, error_text)."""
    tmp = out_path + ".part.mp4"
    cmd = [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', video_file]
    if audio_file:
        cmd += ['-i', audio_file, '-map', '0:v:0', '-map', '1:a:0']
    else:
        cmd += ['-map', '0']
    cmd += ['-c', 'copy', '-movflags', '+faststart', '-sn', '-y', tmp]
    if verbose:
        tqdm.write("[INFO] ffmpeg " + " ".join(cmd[1:]))
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        tail = " | ".join(l for l in (proc.stderr or '').splitlines()[-3:] if l.strip()) or "(no error text)"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, tail
    os.replace(tmp, out_path)
    return True, None


class _HlsJob:
    def __init__(self, video, out_path, headers):
        self.video = video
        self.title = video['title']
        self.out_path = out_path
        self.headers = headers
        self.lock_path = out_path + ".lock"
        self.locked = False
        self.parts_dir = out_path + ".parts"
        self.streams = {}          # 'v'/'a' -> {'parts': [paths in order]}
        self.tasks = []            # (stream_key, idx, url, path)
        self.remaining = 0
        self.failed = False
        self.bar = None


def _build_job(video, session, out_dir, max_height, verbose):
    """Resolve a video (Vimeo or Mux) to its HLS segment lists. Returns a ready _HlsJob or None."""
    fallback = video.get('vimeo_id') or video.get('title') or 'video'
    filename = safe_filename(video['title'] or fallback, fallback)
    if not filename.lower().endswith('.mp4'):
        filename += '.mp4'
    out_path = os.path.join(out_dir, filename) if out_dir else filename

    if os.path.exists(out_path):
        tqdm.write(f"[INFO] Already have {filename}, skipping.")
        return None

    master, headers = resolve_master(video, session, verbose)
    if not master:
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} No HLS for {video['title']} (private/unavailable).")
        return None
    video_url, audio_url = parse_master_playlist(master, session, max_height, headers, verbose)
    if not video_url:
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} No video stream for {video['title']}.")
        return None

    job = _HlsJob(video, out_path, headers)
    os.makedirs(job.parts_dir, exist_ok=True)
    for key, murl in (('v', video_url), ('a', audio_url)):
        if not murl:
            continue
        init, segs = parse_media_playlist(murl, session, headers)
        parts = []
        if init:
            p = os.path.join(job.parts_dir, f"{key}_init")
            job.tasks.append((key, -1, init, p)); parts.append(p)
        for i, su in enumerate(segs):
            p = os.path.join(job.parts_dir, f"{key}_{i:05d}")
            job.tasks.append((key, i, su, p)); parts.append(p)
        job.streams[key] = {'parts': parts}
    if 'v' not in job.streams or not job.streams['v']['parts']:
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Empty playlist for {video['title']}.")
        return None
    job.remaining = len(job.tasks)
    return job


def download_collection_pooled(videos, session, out_dir, max_connections, max_height, verbose):
    """Download all videos as HLS via a shared pool of `max_connections` segment workers,
    then mux each with ffmpeg. Mirrors the Drive downloader: all videos at once, the
    connection budget shared and rebalanced as videos finish."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    print(f"[INFO] Resolving {len(videos)} video(s)...")
    jobs = []
    with ThreadPoolExecutor(max_workers=min(max_connections, 12)) as ex:
        for job in ex.map(lambda v: _build_job(v, session, out_dir, max_height, verbose), videos):
            if job is None:
                continue
            if not acquire_lock(job.lock_path):
                continue
            job.locked = True
            jobs.append(job)

    if not jobs:
        print("[INFO] Nothing to download.")
        return

    # Per-file bars stay in place (leave=True) and show their own done/FAILED status — no
    # writing between live bars (that caused the gaps/jumping on Windows). Above the limit,
    # one overall bar with OK/FAIL lines is used instead.
    per_file_bars = len(jobs) <= PER_FILE_BAR_LIMIT
    print(f"[INFO] Downloading {len(jobs)} video(s) with a shared pool of {max_connections} "
          f"connections; segments fetched in parallel, muxed with ffmpeg.\n")

    bar_lock = threading.Lock()
    overall = None
    if per_file_bars:
        for idx, job in enumerate(jobs):
            done = sum(1 for (_k, _i, _u, p) in job.tasks if os.path.exists(p) and os.path.getsize(p) > 0)
            job.bar = make_bar(total=max(len(job.tasks), 1), initial=done, unit='seg', unit_scale=False,
                               desc=os.path.basename(job.out_path), position=idx, leave=True)
    else:
        total_tasks = sum(len(j.tasks) for j in jobs)
        done = sum(1 for j in jobs for (_k, _i, _u, p) in j.tasks
                   if os.path.exists(p) and os.path.getsize(p) > 0)
        overall = make_bar(total=max(total_tasks, 1), initial=done, unit='seg', unit_scale=False,
                           desc=f'{len(jobs)} videos', position=0)

    result = {'ok': 0, 'fail': 0}
    result_lock = threading.Lock()
    failures = []  # (title, reason)

    def finalize(job):
        ok, reason = False, None
        if job.failed:
            reason = "segment download failed"
        else:
            try:
                vfile = job.out_path + ".video"
                _concat_stream(job.streams['v']['parts'], vfile)
                afile = None
                if 'a' in job.streams:
                    afile = job.out_path + ".audio"
                    _concat_stream(job.streams['a']['parts'], afile)
                ok, reason = _ffmpeg_mux(vfile, afile, job.out_path, verbose)
            except Exception as exc:
                reason = str(exc)
            finally:
                for extra in (job.out_path + ".video", job.out_path + ".audio"):
                    if os.path.exists(extra):
                        try:
                            os.remove(extra)
                        except OSError:
                            pass
        if ok:
            shutil.rmtree(job.parts_dir, ignore_errors=True)
        if job.locked:
            release_lock(job.lock_path)
        with result_lock:
            result['ok' if ok else 'fail'] += 1
            n = result['ok'] + result['fail']
            if not ok:
                failures.append((job.title, reason))
        # Status feedback: in-place on the file's own bar (no inter-bar writing), or as a
        # log line when a single overall bar is used.
        if per_file_bars and job.bar is not None:
            with bar_lock:
                job.bar.colour = 'green' if ok else 'red'
                job.bar.set_postfix_str('done' if ok else 'FAILED', refresh=False)
                job.bar.refresh()
        elif overall is not None:
            tag = f"{CLR.GREEN}OK  {CLR.RESET}" if ok else f"{CLR.RED}FAIL{CLR.RESET}"
            tqdm.write(f"[{n}/{len(jobs)}] {tag} {job.title}")

    def run_task(job, key, idx, url, path):
        if job.failed:
            return
        sess = _seg_session(session)
        if not _download_hls_segment(url, path, sess, job.headers):
            job.failed = True
        with bar_lock:
            if job.bar is not None:
                job.bar.update(1)
            if overall is not None:
                overall.update(1)
        with result_lock:
            job.remaining -= 1
            last = (job.remaining == 0)
        if last:
            finalize(job)

    # Interleave tasks round-robin across jobs so every video progresses at once.
    max_t = max(len(j.tasks) for j in jobs)
    order = []
    for k in range(max_t):
        for job in jobs:
            if k < len(job.tasks):
                key, idx, url, path = job.tasks[k]
                order.append((job, key, idx, url, path))

    with ThreadPoolExecutor(max_workers=max_connections) as ex:
        futures = [ex.submit(run_task, *t) for t in order]
        for _ in as_completed(futures):
            pass

    if per_file_bars:
        with bar_lock:
            for job in jobs:
                if job.bar is not None:
                    job.bar.close()
    if overall is not None:
        overall.close()
    with _seg_sessions_lock:
        for s in _seg_sessions:
            s.close()
        _seg_sessions.clear()
    _seg_tls.__dict__.clear()

    color = CLR.GREEN if result['fail'] == 0 else CLR.YELLOW
    print(f"\n{color}[INFO] Collection done: {result['ok']} succeeded, {result['fail']} failed, "
          f"out of {len(jobs)}.{CLR.RESET}")
    for title, reason in failures:
        print(f"[WARN] {title}: {reason or 'failed'} (partial segments kept for resume).")


def process_collection(collection_id, session, out_dir, max_connections, max_height,
                       select, list_only, verbose):
    print(f"[INFO] Reading collection {collection_id} ...")
    title, campaign_id = get_collection_info(collection_id, session, verbose)
    videos = list_collection_videos(collection_id, campaign_id, session, verbose)
    if not videos:
        print("[ERROR] No videos found. The collection may be private, you may lack access, "
              "your cookies may be missing/expired, or Patreon changed its API.")
        return

    print(f"[INFO] Collection '{title or collection_id}': {len(videos)} video(s).")

    if list_only:
        for i, v in enumerate(videos, 1):
            if v.get('source') == 'mux':
                tag = 'embedded/Mux'
            else:
                tag = f"vimeo {v.get('vimeo_id')}"
            print(f"   {i:>3}) {v['title']}  ({tag})")
        return

    if select:
        interactive = bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
            bool(getattr(sys.stdout, 'isatty', lambda: False)())
        if interactive:
            videos = _prompt_file_selection(videos)
            if not videos:
                print("[INFO] Nothing selected; exiting.")
                return
            print(f"[INFO] Selected {len(videos)} file(s).")
        else:
            print("[WARN] --select needs an interactive terminal; downloading all.")

    download_collection_pooled(videos, session, out_dir, max_connections, max_height, verbose)


def main(url, out_dir=None, video_workers=DEFAULT_VIDEO_WORKERS, max_height=DEFAULT_MAX_HEIGHT,
         verbose=False, cookies_file=None, use_color=USE_COLOR, auto_cookies=AUTO_COOKIES,
         select=False, list_only=False, ffmpeg_path=None, ffmpeg_url=None,
         max_connections=None, auto=False):
    global FFMPEG, FFMPEG_DOWNLOAD_URL
    if ffmpeg_path:
        FFMPEG = ffmpeg_path
    if ffmpeg_url is not None:
        FFMPEG_DOWNLOAD_URL = ffmpeg_url
    setup_console(use_color)

    # Resolve the connection budget: explicit -m always wins; otherwise --auto scales by CPU,
    # else the configured default.
    if max_connections is None:
        if auto:
            max_connections, logical = _auto_connections()
            print(f"[INFO] --auto: {logical} logical CPU(s) detected, using {max_connections} "
                  f"parallel connections.")
        else:
            max_connections = DEFAULT_MAX_CONNECTIONS

    collection_id = extract_collection_id(url)
    if not collection_id:
        print("[ERROR] Could not find a collection id in the URL. Expected something like "
              "https://www.patreon.com/collection/512335")
        sys.exit(1)

    if not list_only and not ensure_ffmpeg(verbose):
        print(f"[ERROR] ffmpeg not found and could not be obtained. These videos are HLS and need "
              "ffmpeg to mux audio+video into MP4.")
        print("        Either install ffmpeg and put it on PATH, set FFMPEG at the top of the script,")
        print("        or set FFMPEG_DOWNLOAD_URL / pass --ffmpeg-url to auto-download it.")
        sys.exit(1)

    if cookies_file:
        cookies_files = [cookies_file]
    elif auto_cookies:
        cookies_files = auto_detect_cookies(verbose)
    else:
        cookies_files = []
    if not cookies_files:
        print("[WARN] No cookies provided. Patreon needs your logged-in session for paid posts; "
              "use --cookies or drop a JSON cookie export next to the script.")

    try:
        session = get_cookies_session(cookies_files)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] Failed to load cookies: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        process_collection(collection_id, session, out_dir, max_connections, max_height,
                           select, list_only, verbose)
    finally:
        session.close()


if __name__ == "__main__":
    def positive_int(v):
        iv = int(v)
        if iv < 1:
            raise argparse.ArgumentTypeError("must be >= 1")
        return iv

    def nonneg_int(v):
        iv = int(v)
        if iv < 0:
            raise argparse.ArgumentTypeError("must be >= 0")
        return iv

    parser = argparse.ArgumentParser(description="Download all videos from a Patreon collection (Vimeo/HLS) as MP4.")
    parser.add_argument("url", type=str, help="Patreon collection URL, e.g. https://www.patreon.com/collection/512335")
    parser.add_argument("-o", "--output-dir", type=str, default=None, help="Directory to save videos into (default: current directory).")
    parser.add_argument("-w", "--workers", type=positive_int, default=DEFAULT_VIDEO_WORKERS, help=argparse.SUPPRESS)
    parser.add_argument("-m", "--max-connections", type=positive_int, default=None, help=f"Shared pool of parallel segment connections across all videos. Default {DEFAULT_MAX_CONNECTIONS}. Lower if you hit rate limits.")
    parser.add_argument("--auto", action="store_true", help="Auto-pick the connection count from the detected CPU. Ignored if -m is given.")
    parser.add_argument("-q", "--max-height", type=nonneg_int, default=DEFAULT_MAX_HEIGHT, help="Cap video height (e.g. 720). 0 = best available (default).")
    parser.add_argument("-s", "--select", action="store_true", help="List all videos first and interactively choose which to download.")
    parser.add_argument("-l", "--list", action="store_true", help="Only list the videos in the collection; do not download.")
    parser.add_argument("--no-auto-cookies", action="store_true", help="Do not auto-use a *.json cookie file found nearby.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookies", type=str, help="Path to a Netscape cookies.txt or JSON cookie export (your Patreon session).")
    parser.add_argument("--ffmpeg", type=str, default=None, help="Path to the ffmpeg executable (overrides the FFMPEG setting).")
    parser.add_argument("--ffmpeg-url", type=str, default=None, help="URL of an ffmpeg archive (zip/tar.*) to auto-download if ffmpeg is missing. Empty string disables auto-download.")
    parser.add_argument("--version", action="version", version="%(prog)s 2.5.0")

    args = parser.parse_args()
    main(args.url, out_dir=args.output_dir, video_workers=args.workers, max_height=args.max_height,
         verbose=args.verbose, cookies_file=args.cookies,
         use_color=(USE_COLOR and not args.no_color),
         auto_cookies=(AUTO_COOKIES and not args.no_auto_cookies),
         select=args.select, list_only=args.list,
         ffmpeg_path=args.ffmpeg, ffmpeg_url=args.ffmpeg_url,
         max_connections=args.max_connections, auto=args.auto)
