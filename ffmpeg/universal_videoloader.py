"""Universal intelligent video downloader.

Give it ANY video URL. The script:

  1. Detects the source via yt-dlp (1000+ sites: YouTube, Vimeo, Patreon, Drive,
     Twitch, Twitter/X, TikTok, generic HLS/DASH, ...).
  2. Probes everything that's available: every video quality, every audio track
     (multilingual dubs, descriptive audio, ...), every subtitle (manual +
     auto-generated), every thumbnail.
  3. INTERACTIVELY lets you pick what to keep — or with --auto grabs the best
     video + ALL audio tracks + ALL subtitles, no questions asked.
  4. Downloads with a CPU-tuned pool of parallel fragment connections (the
     equivalent of the connection-thread pool from the original Drive / Patreon
     downloaders), with resume on partial fragments.
  5. Muxes the lot into ONE .mkv file with every audio stream and every
     subtitle embedded as soft tracks. (MKV is used because, unlike MP4, it can
     hold an arbitrary number of audio + soft-subtitle tracks of any codec.)

Inherits the polish of the Drive / Patreon downloaders:
  * colorama-aware Windows-10 console (VT escape sequences),
  * aligned, ASCII-on-Windows tqdm progress bars,
  * JSON-cookie auto-detection (browser exports) with interactive picker when
    several are found next to the script,
  * ffmpeg auto-install if it isn't on PATH,
  * --auto sizes the connection budget from the detected CPU count.

Dependencies:
    pip install yt-dlp tqdm colorama requests

Examples:
    py universal_videoloader.py "https://www.youtube.com/watch?v=..."
    py universal_videoloader.py "https://vimeo.com/..." --auto
    py universal_videoloader.py URL -q 1080 --subs en,cs --no-auto-subs
    py universal_videoloader.py URL --list                  # only probe, don't download
    py universal_videoloader.py URL --cookies-from-browser firefox
"""

from __future__ import annotations

from urllib.parse import urlparse
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.cookiejar import MozillaCookieJar

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar

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
    import yt_dlp
except Exception:  # pragma: no cover
    print("This script needs yt-dlp:  pip install -U yt-dlp", file=sys.stderr)
    raise

try:
    import colorama
    _HAS_COLORAMA = True
except Exception:
    _HAS_COLORAMA = False

# ============================================================================
#  USER CONFIG — edit defaults; command-line flags always win.
# ============================================================================
DEFAULT_MAX_CONNECTIONS = 16      # -m : parallel fragment connections (yt-dlp's concurrent_fragment_downloads)
DEFAULT_MAX_HEIGHT = 0            # -q : 0 = best; else cap (e.g. 720, 1080)
DEFAULT_CONTAINER = "mkv"         # output container; "mkv" supports unlimited audio+subs
DEFAULT_INCLUDE_AUTO_SUBS = True  # also grab YouTube-style auto-generated captions
AUTO_COOKIES = True               # if --cookies omitted, auto-pick a JSON cookie export nearby
USE_COLOR = True                  # colored output (uses colorama on Windows when present)
FORCE_ASCII_BARS = None           # None = auto (ASCII on Windows); True/False to force
FFMPEG = "ffmpeg"                 # ffmpeg executable, a folder containing it, or a bare command on PATH.
                                  # Windows: forward slashes or a raw string, e.g. r"C:\ffmpeg\bin".
# Auto-install ffmpeg if missing. Empty string disables.
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
BAR_NCOLS = 100                   # progress-bar width cap
BAR_DESC_WIDTH = 26               # left label column (for alignment)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# ============================================================================

ASCII_BARS = False
BAR_FORMAT = '{desc} {percentage:3.0f}% |{bar}| {n_fmt:>9}/{total_fmt:>9} {rate_fmt:>10}'


# --------------------------------------------------------------------------- #
#  Console / colour (copied from the Drive + Patreon downloaders)
# --------------------------------------------------------------------------- #
class _Palette:
    def __init__(self, enabled: bool):
        if enabled:
            self.RESET = '\033[0m'; self.RED = '\033[31m'; self.GREEN = '\033[32m'
            self.YELLOW = '\033[33m'; self.CYAN = '\033[36m'; self.DIM = '\033[2m'
            self.BOLD = '\033[1m'; self.MAGENTA = '\033[35m'
        else:
            self.RESET = self.RED = self.GREEN = self.YELLOW = self.CYAN = ''
            self.DIM = self.BOLD = self.MAGENTA = ''


CLR = _Palette(False)
_builtin_print = print


def _cprint(*args, **kwargs):
    """Drop-in print that colorizes by [INFO]/[WARN]/[ERROR] prefix and success lines."""
    if CLR.RESET and args and isinstance(args[0], str):
        s = args[0]; st = s.lstrip(); color = ''
        if st.startswith('[ERROR]'):
            color = CLR.RED
        elif st.startswith('[WARN]'):
            color = CLR.YELLOW
        elif st.startswith('[INFO]'):
            color = CLR.CYAN
        elif st.startswith('[OK]') or 'downloaded successfully' in s or 'done' in s.lower():
            color = CLR.GREEN
        if color:
            args = (color + s + CLR.RESET,) + args[1:]
    _builtin_print(*args, **kwargs)


print = _cprint


def _enable_windows_vt() -> bool:
    """Enable ANSI escape processing on a Windows 10+ console without colorama."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for h in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = k.GetStdHandle(h); mode = ctypes.c_uint32()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                k.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def setup_console(use_color: bool) -> None:
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


def _fit_desc(name: str, width: int) -> str:
    """Pad/middle-truncate a label to a fixed width so progress bars line up."""
    name = str(name)
    if len(name) <= width:
        return name.ljust(width)
    keep = width - 2; head = (keep + 1) // 2; tail = keep - head
    return name[:head] + '..' + (name[-tail:] if tail > 0 else '')


def _bar_ncols() -> int:
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


def is_tty() -> bool:
    return bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
        bool(getattr(sys.stdout, 'isatty', lambda: False)())


# --------------------------------------------------------------------------- #
#  Cookies (copied from the Drive + Patreon downloaders, slightly refactored)
# --------------------------------------------------------------------------- #
def load_cookies_from_file(cookies_file: str) -> RequestsCookieJar:
    """Load cookies from a Netscape cookies.txt or browser JSON export."""
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
            data = data["cookies"]
        if not isinstance(data, list):
            raise ValueError(f"Unsupported JSON cookies format: {cookies_file}")
        jar = RequestsCookieJar()
        for c in data:
            if not isinstance(c, dict):
                continue
            name, value = c.get("name"), c.get("value")
            if not name or value is None:
                continue
            jar.set(name, value, domain=c.get("domain") or "", path=c.get("path") or "/",
                    expires=c.get("expirationDate") or c.get("expires"))
        return jar

    gen = None
    if not stripped.startswith('# Netscape HTTP Cookie File') and not stripped.startswith('# HTTP Cookie File'):
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
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


def _looks_like_cookies_json(path: str) -> bool:
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


def _format_mtime(path: str) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return '?'


def _prompt_cookie_choice(candidates: list) -> list:
    print(f"[INFO] Found {len(candidates)} JSON cookie files in the directory:")
    for i, p in enumerate(candidates, 1):
        print(f"   {CLR.CYAN}{i}{CLR.RESET}) {os.path.basename(p)}   "
              f"{CLR.DIM}({_format_mtime(p)}){CLR.RESET}")
    print("Select: number(s) like '1,3', 'a' for ALL, 'n' for NONE, or Enter for the newest (1).")
    for _ in range(3):
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(""); return [candidates[0]]
        if raw == '':
            return [candidates[0]]
        if raw in ('n', 'none', '0'):
            return []
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
        print(f"[WARN] Invalid choice. Enter number(s) 1-{len(candidates)}, 'a', 'n', or Enter.")
    return [candidates[0]]


def auto_detect_cookies(verbose: bool) -> list:
    """Find JSON cookie file(s) next to the script or in the working directory."""
    import glob

    def _norm(p):
        return os.path.normcase(os.path.realpath(p))

    dirs, seen_dirs = [], set()
    for d in (os.getcwd(), os.path.dirname(os.path.abspath(sys.argv[0] or '.'))):
        if not d:
            continue
        key = _norm(d)
        if key in seen_dirs:
            continue
        seen_dirs.add(key); dirs.append(d)

    candidates, seen = [], set()
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
    if not is_tty():
        print(f"[INFO] Found {len(candidates)} JSON cookie files; non-interactive, using newest: "
              f"{os.path.basename(candidates[0])}")
        return [candidates[0]]
    chosen = _prompt_cookie_choice(candidates)
    if chosen:
        print(f"[INFO] Using {len(chosen)} cookie file(s): "
              f"{', '.join(os.path.basename(p) for p in chosen)}")
    return chosen


def merge_cookies_to_netscape_file(cookies_files: list, dest_path: str) -> None:
    """Merge one-or-more cookie files (JSON or Netscape) into a Netscape-format file
    that we hand to yt-dlp. yt-dlp's --cookies wants the Netscape format only."""
    merged = RequestsCookieJar()
    for cf in cookies_files:
        for c in load_cookies_from_file(cf):
            merged.set_cookie(c)
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# Generated by universal_videoloader.py\n\n")
        for c in merged:
            # domain  include_subdomains  path  secure  expiry  name  value
            include_sub = "TRUE" if (c.domain or "").startswith(".") else "FALSE"
            secure = "TRUE" if c.secure else "FALSE"
            expiry = str(int(c.expires)) if c.expires else "0"
            f.write("\t".join([
                c.domain or "", include_sub, c.path or "/", secure, expiry,
                c.name or "", (c.value or "").replace("\n", " ")
            ]) + "\n")


# --------------------------------------------------------------------------- #
#  ffmpeg locator + auto-installer  (copied from the Patreon downloader)
# --------------------------------------------------------------------------- #
def _resolve_ffmpeg(value: str) -> str | None:
    if not value:
        return None
    name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    if os.path.isdir(value):
        for cand in (os.path.join(value, name), os.path.join(value, 'bin', name)):
            if os.path.isfile(cand):
                return cand
        return None
    return value


def _try_ffmpeg(path: str) -> bool:
    if not path:
        return False
    try:
        subprocess.run([path, '-version'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def _ffmpeg_cache_dir() -> str:
    base = os.path.dirname(os.path.abspath(sys.argv[0] or '.')) or os.getcwd()
    return os.path.join(base, '.ffmpeg')


def _find_cached_ffmpeg() -> str | None:
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


def _extract_archive(path: str, dest: str, url: str) -> None:
    lower = (url or path).lower()
    import zipfile, tarfile
    if lower.endswith('.zip') or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            t.extractall(dest)
    else:
        raise ValueError("Unknown ffmpeg archive format (expected .zip or .tar.*)")


def _download_and_extract_ffmpeg(url: str, verbose: bool) -> str | None:
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


def ensure_ffmpeg(verbose: bool) -> str | None:
    """Return a working ffmpeg path, downloading it on demand. None if unavailable."""
    global FFMPEG
    resolved = _resolve_ffmpeg(FFMPEG)
    if resolved and _try_ffmpeg(resolved):
        FFMPEG = resolved
        return resolved
    cached = _find_cached_ffmpeg()
    if cached and _try_ffmpeg(cached):
        FFMPEG = cached
        if verbose:
            print(f"[INFO] Using cached ffmpeg: {cached}")
        return cached
    if FFMPEG_DOWNLOAD_URL:
        try:
            path = _download_and_extract_ffmpeg(FFMPEG_DOWNLOAD_URL, verbose)
        except Exception as e:
            print(f"[ERROR] ffmpeg download/extract failed: {e}")
            return None
        if path and _try_ffmpeg(path):
            FFMPEG = path
            print(f"[INFO] Using downloaded ffmpeg: {path}")
            return path
        print("[ERROR] Downloaded archive but could not find a working ffmpeg binary inside.")
    return None


# --------------------------------------------------------------------------- #
#  Filename safety
# --------------------------------------------------------------------------- #
def safe_filename(name: str, fallback: str = "video") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', str(name or ''))
    name = re.sub(r'[. ]+$', '', name)
    return name or str(fallback)


# --------------------------------------------------------------------------- #
#  --auto sizing  (matches the original scripts)
# --------------------------------------------------------------------------- #
def _auto_connections() -> tuple[int, int]:
    """Pick a connection budget from the CPU count. Downloads are I/O-bound, so we scale
    above the logical CPU count (more in-flight fragments than cores), capped to stay
    polite to whichever CDN the video lives on."""
    logical = os.cpu_count() or 4
    return max(8, min(64, logical * 4)), logical


# --------------------------------------------------------------------------- #
#  Selection parsing (shared with the Drive + Patreon downloaders)
# --------------------------------------------------------------------------- #
def _parse_index_selection(raw: str, n: int):
    """Parse a string like '1,3,5-8' into a list of 1-based indices.
    Returns None on invalid input; [] for cancel/none; full range for all/empty."""
    raw = raw.replace(' ', '').lower()
    if raw in ('a', 'all', ''):
        return list(range(1, n + 1))
    if raw in ('n', 'none', 'q', 'quit', '0'):
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


# --------------------------------------------------------------------------- #
#  Format classification
# --------------------------------------------------------------------------- #
def _format_size(n: int | None) -> str:
    if not n or n <= 0:
        return ''
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    x = float(n)
    for u in units:
        if x < 1024:
            return f"{x:.1f} {u}" if u != 'B' else f"{int(x)} {u}"
        x /= 1024
    return f"{x:.1f} PB"


def _format_rate(bps: float | None) -> str:
    if not bps or bps <= 0:
        return ''
    if bps >= 1000:
        return f"{bps/1000:.1f} Mbps"
    return f"{int(bps)} kbps"


def classify_formats(info: dict) -> tuple[list, list, list]:
    """Split yt-dlp's formats list into (video-only, audio-only, combined).
    Anything with both a v- and a-codec lands in 'combined' (often a progressive
    mp4 like YouTube's 18, or a single HLS variant that already has audio muxed)."""
    formats = info.get('formats') or []
    video, audio, combo = [], [], []
    for f in formats:
        vcodec = (f.get('vcodec') or 'none').lower()
        acodec = (f.get('acodec') or 'none').lower()
        has_v = vcodec != 'none' and vcodec != ''
        has_a = acodec != 'none' and acodec != ''
        # m3u8 master/manifest entries with no usable info: skip
        if not has_v and not has_a:
            continue
        # Storyboards / image streams: skip
        if (f.get('format_note') or '').lower() == 'storyboard':
            continue
        if has_v and has_a:
            combo.append(f)
        elif has_v:
            video.append(f)
        else:
            audio.append(f)

    def _vkey(f):
        return (
            f.get('height') or 0,
            f.get('width') or 0,
            f.get('fps') or 0,
            f.get('tbr') or f.get('vbr') or 0,
            -1 if (f.get('ext') == 'webm') else 0,  # tie-break: prefer mp4 over webm at equal quality
        )

    def _akey(f):
        return (
            f.get('abr') or f.get('tbr') or 0,
            f.get('asr') or 0,
            -1 if (f.get('ext') == 'webm') else 0,
        )

    video.sort(key=_vkey, reverse=True)
    audio.sort(key=_akey, reverse=True)
    combo.sort(key=_vkey, reverse=True)
    return video, audio, combo


def _describe_video(f: dict) -> str:
    h = f.get('height'); w = f.get('width'); fps = f.get('fps')
    note = f.get('format_note') or (f"{h}p" if h else '')
    res = f"{w}x{h}" if (w and h) else (f"{h}p" if h else '?')
    vcodec = (f.get('vcodec') or '').split('.')[0]
    ext = f.get('ext') or '?'
    bitrate = _format_rate(f.get('tbr') or f.get('vbr'))
    size = _format_size(f.get('filesize') or f.get('filesize_approx'))
    fps_s = f"{int(fps)}fps" if fps else ''
    hdr = ' HDR' if (f.get('dynamic_range') or '').upper() not in ('', 'SDR') else ''
    parts = [p for p in [res, fps_s, note, ext, vcodec, bitrate, size] if p]
    return f"{f.get('format_id'):>6}  " + "  ".join(parts) + hdr


def _describe_audio(f: dict) -> str:
    acodec = (f.get('acodec') or '').split('.')[0]
    ext = f.get('ext') or '?'
    abr = _format_rate(f.get('abr') or f.get('tbr'))
    asr = f.get('asr')
    asr_s = f"{int(asr/1000)}kHz" if asr else ''
    ch = f.get('audio_channels')
    ch_s = f"{ch}ch" if ch else ''
    lang = f.get('language') or ''
    lang_s = f"[{lang}]" if lang else ''
    note = f.get('format_note') or ''
    size = _format_size(f.get('filesize') or f.get('filesize_approx'))
    parts = [p for p in [lang_s, ext, acodec, abr, asr_s, ch_s, note, size] if p]
    return f"{f.get('format_id'):>6}  " + "  ".join(parts)


def _collect_subs(info: dict, include_auto: bool):
    """Return [(lang, name, kind)] sorted by lang. kind in ('manual','auto')."""
    out = []
    for lang, tracks in (info.get('subtitles') or {}).items():
        if lang.startswith('live_chat'):
            continue
        name = ''
        if tracks and isinstance(tracks, list):
            name = tracks[0].get('name', '') or ''
        out.append((lang, name, 'manual'))
    if include_auto:
        for lang, tracks in (info.get('automatic_captions') or {}).items():
            if lang.startswith('live_chat'):
                continue
            if any(x[0] == lang for x in out):
                continue  # don't list auto duplicates of manual subs
            name = ''
            if tracks and isinstance(tracks, list):
                name = tracks[0].get('name', '') or ''
            out.append((lang, name, 'auto'))
    out.sort(key=lambda x: (x[2] != 'manual', x[0]))
    return out


# --------------------------------------------------------------------------- #
#  Interactive selection prompts
# --------------------------------------------------------------------------- #
def prompt_video(video_formats: list, combo_formats: list, max_height: int) -> str:
    """Return a yt-dlp format selector for the video stream (just the video part).
    `max_height` only seeds the default suggestion; the user always sees the full list."""
    have_split = bool(video_formats)
    have_combo = bool(combo_formats)
    if not (have_split or have_combo):
        return 'best'  # let yt-dlp pick something

    items = []
    print(f"\n{CLR.BOLD}[?]{CLR.RESET} Video quality? ({len(video_formats)} video-only, "
          f"{len(combo_formats)} combined)")
    if have_split:
        print(f"  {CLR.DIM}--- video-only streams (will be merged with chosen audio) ---{CLR.RESET}")
        for f in video_formats:
            items.append(('split', f))
            i = len(items)
            print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {_describe_video(f)}")
    if have_combo:
        print(f"  {CLR.DIM}--- combined v+a streams (already include audio) ---{CLR.RESET}")
        for f in combo_formats:
            items.append(('combo', f))
            i = len(items)
            print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {_describe_video(f)} {CLR.DIM}+audio{CLR.RESET}")

    # default: best video-only if available (so we can layer in all audio tracks); else best combo
    default_idx = 1
    if max_height and max_height > 0:
        # try to pick the highest <= max_height in the first list
        for i, (kind, f) in enumerate(items, 1):
            if (f.get('height') or 0) <= max_height:
                default_idx = i; break
    print(f"Pick one (Enter = {default_idx} = {'best' if default_idx == 1 else f'≤{max_height}p'}, q to cancel): ", end='')
    for _ in range(3):
        try:
            raw = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(""); return 'CANCEL'
        if raw in ('q', 'quit'):
            return 'CANCEL'
        if raw == '':
            idx = default_idx
        else:
            try:
                idx = int(raw)
                if not (1 <= idx <= len(items)):
                    raise ValueError
            except ValueError:
                print(f"[WARN] Invalid choice. Enter 1-{len(items)}, Enter, or 'q'. ", end='')
                continue
        kind, f = items[idx - 1]
        return ('combo:' if kind == 'combo' else 'split:') + str(f.get('format_id'))
    return 'CANCEL'


def prompt_audio(audio_formats: list) -> list:
    """Return a list of format_ids the user wants. Empty list means 'no audio besides
    whatever's in the chosen video'."""
    if not audio_formats:
        return []
    print(f"\n{CLR.BOLD}[?]{CLR.RESET} Audio tracks? ({len(audio_formats)} available)")
    for i, f in enumerate(audio_formats, 1):
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {_describe_audio(f)}")
    print("Pick: numbers like '1,3', 'a' or Enter for ALL, 'n' for default best only.")
    for _ in range(3):
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(""); return [audio_formats[0].get('format_id')]
        if raw in ('n', 'none'):
            return [audio_formats[0].get('format_id')]
        sel = _parse_index_selection(raw, len(audio_formats))
        if sel is None:
            print(f"[WARN] Invalid input. Try '1,3', 'a', 'n', or Enter.")
            continue
        return [audio_formats[i - 1].get('format_id') for i in sel]
    return [audio_formats[0].get('format_id')]


def prompt_subs(subs: list, include_auto_default: bool) -> tuple[list, bool]:
    """Return (langs_to_keep, include_auto_subs).

    Empty langs list = no subs at all. ['all'] is a sentinel meaning "everything".
    """
    if not subs:
        return [], False

    manual = [s for s in subs if s[2] == 'manual']
    auto = [s for s in subs if s[2] == 'auto']

    print(f"\n{CLR.BOLD}[?]{CLR.RESET} Subtitles? ({len(manual)} manual, {len(auto)} auto-generated)")
    for i, (lang, name, kind) in enumerate(subs, 1):
        tag = f"{CLR.DIM}(auto-generated){CLR.RESET}" if kind == 'auto' else ''
        nm = f" - {name}" if name else ''
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {lang:<6}{nm}  {tag}")
    if auto:
        print("Pick: numbers like '1,2', 'a' for ALL, 'm' for ALL MANUAL only, 'n' for NONE, Enter = all manual.")
    else:
        print("Pick: numbers like '1,2', 'a' or Enter for ALL, 'n' for NONE.")

    for _ in range(3):
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(""); return [s[0] for s in manual], False
        if raw in ('n', 'none'):
            return [], False
        if raw == 'm':
            return [s[0] for s in manual], False
        if raw == '' and auto:
            return [s[0] for s in manual], False
        if raw in ('a', 'all', ''):
            return [s[0] for s in subs], bool(auto)
        sel = _parse_index_selection(raw, len(subs))
        if sel is None:
            print(f"[WARN] Invalid input.")
            continue
        chosen = [subs[i - 1] for i in sel]
        include_auto_flag = any(c[2] == 'auto' for c in chosen)
        return [c[0] for c in chosen], include_auto_flag
    return [s[0] for s in manual], False


def prompt_playlist_selection(entries: list) -> list:
    """For playlist URLs, ask which entries to download. Returns 1-based indices."""
    print(f"\n{CLR.BOLD}[?]{CLR.RESET} {len(entries)} video(s) in this playlist/collection:")
    for i, e in enumerate(entries, 1):
        title = (e.get('title') or e.get('id') or '?')[:80]
        dur = e.get('duration')
        dur_s = f" ({int(dur)//60}:{int(dur)%60:02d})" if dur else ''
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {title}{dur_s}")
    print("Pick: e.g. '1,3,5-8', 'a' or Enter for ALL, 'q' to cancel.")
    for _ in range(3):
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(""); return []
        sel = _parse_index_selection(raw, len(entries))
        if sel is None:
            print(f"[WARN] Invalid input. Try '1,3,5-8', 'a', 'n', or 'q'.")
            continue
        return sel
    return []


# --------------------------------------------------------------------------- #
#  yt-dlp progress hook -> tqdm bars (one per file being written)
# --------------------------------------------------------------------------- #
class ProgressBars:
    """Routes yt-dlp progress callbacks to per-file tqdm bars. Thread-safe; yt-dlp can
    fire fragment progress from many threads when concurrent_fragment_downloads > 1."""

    def __init__(self):
        self.bars: dict[str, tqdm] = {}
        self.last_status: dict[str, str] = {}
        self.lock = threading.Lock()
        self._pp_bar = None

    def progress_hook(self, d: dict):
        status = d.get('status')
        fn = d.get('filename') or d.get('info_dict', {}).get('_filename') or '?'
        with self.lock:
            if status == 'downloading':
                total = (d.get('total_bytes') or d.get('total_bytes_estimate') or 0)
                done = d.get('downloaded_bytes') or 0
                bar = self.bars.get(fn)
                if bar is None:
                    label = os.path.basename(fn)
                    bar = make_bar(total=max(total, 1), initial=0, desc=label,
                                   unit='B', unit_scale=True, leave=True,
                                   position=len(self.bars))
                    self.bars[fn] = bar
                if total and bar.total != total:
                    bar.total = total
                    bar.refresh()
                step = done - bar.n
                if step > 0:
                    bar.update(step)
                elif step < 0:
                    # Fragment got restarted from zero — pull the bar back so the
                    # math doesn't drift.
                    bar.n = max(0, done); bar.refresh()
            elif status == 'finished':
                bar = self.bars.pop(fn, None)
                if bar is not None:
                    bar.n = bar.total or bar.n
                    bar.refresh()
                    bar.close()
            elif status == 'error':
                bar = self.bars.pop(fn, None)
                if bar is not None:
                    bar.colour = 'red'
                    bar.set_postfix_str('FAILED')
                    bar.close()
                tqdm.write(f"{CLR.RED}[ERROR]{CLR.RESET} download error: {os.path.basename(fn)}")
            self.last_status[fn] = status

    def postprocessor_hook(self, d: dict):
        """yt-dlp's postprocessor steps (merge, embed subs, embed metadata).
        We just log them — they're fast enough to not need their own progress bar."""
        status = d.get('status')
        pp = d.get('postprocessor') or '?'
        if status == 'started':
            tqdm.write(f"{CLR.CYAN}[INFO]{CLR.RESET} {pp} ...")
        elif status == 'finished':
            tqdm.write(f"{CLR.GREEN}[OK]{CLR.RESET}   {pp} done")

    def close_all(self):
        with self.lock:
            for bar in self.bars.values():
                try:
                    bar.close()
                except Exception:
                    pass
            self.bars.clear()


# --------------------------------------------------------------------------- #
#  Building yt-dlp options from selections
# --------------------------------------------------------------------------- #
class _QuietLogger:
    """Silences yt-dlp's own stderr chatter so the tqdm bars stay clean.
    Real errors still go through the print path so they end up coloured."""
    def __init__(self, verbose: bool):
        self.verbose = verbose
    def debug(self, msg):
        if self.verbose and msg and not msg.startswith('[debug]'):
            tqdm.write(msg)
    def info(self, msg):
        if self.verbose:
            tqdm.write(msg)
    def warning(self, msg):
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} {msg}")
    def error(self, msg):
        tqdm.write(f"{CLR.RED}[ERROR]{CLR.RESET} {msg}")


def build_format_selector(video_choice: str, audio_ids: list, info: dict) -> tuple[str, bool]:
    """Return (format_string, allow_multiple_audio).

    video_choice is 'split:<id>' (download that video-only stream and add audios) or
    'combo:<id>' (a single combined stream — typically only one audio possible).
    audio_ids is the list of audio-only format ids the user wants. Empty -> use the
    extractor's best audio automatically. If only one audio id is given we still merge
    normally; with several we set allow_multiple_audio_streams so yt-dlp keeps them all."""
    kind, _, fid = video_choice.partition(':')
    if kind == 'combo':
        # Single self-contained stream. Adding audios on top is technically possible
        # via 'mergeall' but most users picking 'combo' just want that one file as-is.
        if not audio_ids:
            return fid, False
        # User picked combo + extra audio tracks: layer them in.
        fmt = fid + '+' + '+'.join(audio_ids)
        return fmt, len(audio_ids) >= 1
    # split mode
    if not audio_ids:
        return f"{fid}+bestaudio/{fid}+ba/{fid}/best", False
    fmt = fid + '+' + '+'.join(audio_ids)
    return fmt + f"/{fid}+bestaudio/best", len(audio_ids) > 1


def build_ydl_opts(
    *,
    out_template: str,
    container: str,
    format_string: str,
    allow_multi_audio: bool,
    sub_langs: list | None,         # None = none; ['all'] = all; or list of lang codes
    write_auto_subs: bool,
    embed_subs: bool,
    embed_thumbnail: bool,
    embed_metadata: bool,
    max_connections: int,
    cookies_netscape_path: str | None,
    cookies_from_browser: str | None,
    progress: ProgressBars,
    ffmpeg_location: str | None,
    verbose: bool,
    extra_headers: dict | None = None,
    allow_unplayable: bool = False,
    no_check_cert: bool = False,
    referer: str | None = None,
    excluded_extractors: list[str] | None = None,
) -> dict:
    opts: dict = {
        'outtmpl': out_template,
        'format': format_string,
        'merge_output_format': container,
        'concurrent_fragment_downloads': max_connections,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'retry_sleep_functions': {
            'http': lambda n: min(2 ** n, 30),
            'fragment': lambda n: min(2 ** n, 30),
        },
        'continuedl': True,                    # resume partial downloads
        'noprogress': True,                    # we drive our own bars
        'progress_hooks': [progress.progress_hook],
        'postprocessor_hooks': [progress.postprocessor_hook],
        'logger': _QuietLogger(verbose),
        'quiet': not verbose,
        'no_warnings': not verbose,
        'ignoreerrors': False,
        'overwrites': False,
        'http_headers': {'User-Agent': USER_AGENT, **(extra_headers or {})},
        'allow_multiple_audio_streams': allow_multi_audio,
        'allow_multiple_video_streams': False,
        # MKV-friendly: keep original codecs whenever possible (no re-encode in mux).
        'postprocessors': [],
    }
    # Bypass yt-dlp's preventive DRM heuristic: useful for self-hosted streamers
    # (Jellyfin/Plex/custom NAS players) that send AES-128 encrypted HLS with the
    # decryption key inline in the playlist — that's not legal DRM, ffmpeg handles it.
    if allow_unplayable:
        opts['allow_unplayable_formats'] = True
    if no_check_cert:
        # Self-signed certs on LAN NAS; yt-dlp would otherwise refuse.
        opts['nocheckcertificate'] = True
    if referer:
        opts['http_headers']['Referer'] = referer
    if excluded_extractors:
        # Disable specific extractors so GenericIE doesn't hand a recursively-found
        # embed URL off to e.g. KnownDRMIE (which always raises). Without that hop,
        # GenericIE keeps looking for the actual stream in the page HTML.
        opts['allowed_extractors'] = ['default'] + [f'-{n}' for n in excluded_extractors]
    if ffmpeg_location:
        opts['ffmpeg_location'] = ffmpeg_location
    if cookies_netscape_path:
        opts['cookiefile'] = cookies_netscape_path
    if cookies_from_browser:
        opts['cookiesfrombrowser'] = (cookies_from_browser,)

    # Subtitles
    if sub_langs:
        opts['writesubtitles'] = True
        if write_auto_subs:
            opts['writeautomaticsub'] = True
        opts['subtitleslangs'] = sub_langs
        # MKV stores SRT/ASS/VTT as soft tracks natively. The post-processor will run
        # ffmpeg to mux them into the container.
        opts['subtitlesformat'] = 'srt/vtt/best'
        if embed_subs:
            opts['postprocessors'].append({'key': 'FFmpegEmbedSubtitle',
                                           'already_have_subtitle': True})

    # Metadata + thumbnail (nice-to-haves for MKV)
    if embed_metadata:
        opts['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True,
                                       'add_chapters': True, 'add_infojson': False})
    if embed_thumbnail:
        opts['writethumbnail'] = True
        opts['postprocessors'].append({'key': 'EmbedThumbnail', 'already_have_thumbnail': False})

    return opts


# --------------------------------------------------------------------------- #
#  Plan: probe + select + download
# --------------------------------------------------------------------------- #
def probe_info(url: str, cookies_netscape: str | None, cookies_from_browser: str | None,
               verbose: bool, *, allow_unplayable: bool = False, no_check_cert: bool = False,
               referer: str | None = None, extra_headers: dict | None = None,
               excluded_extractors: list[str] | None = None) -> dict | None:
    """Run yt-dlp's extractor against the URL WITHOUT downloading. Returns the info
    dict or None on failure."""
    headers = {'User-Agent': USER_AGENT, **(extra_headers or {})}
    if referer:
        headers['Referer'] = referer
    opts = {
        'quiet': not verbose, 'no_warnings': not verbose,
        'skip_download': True, 'extract_flat': False,
        'logger': _QuietLogger(verbose),
        'http_headers': headers,
    }
    if allow_unplayable:
        opts['allow_unplayable_formats'] = True
    if no_check_cert:
        opts['nocheckcertificate'] = True
    if excluded_extractors:
        opts['allowed_extractors'] = ['default'] + [f'-{n}' for n in excluded_extractors]
    if cookies_netscape:
        opts['cookiefile'] = cookies_netscape
    if cookies_from_browser:
        opts['cookiesfrombrowser'] = (cookies_from_browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False, process=True)
    except yt_dlp.utils.DownloadError as e:
        print(f"[ERROR] Could not extract video info: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Unexpected error during probe: {e}")
        return None


def summarize_info(info: dict) -> None:
    title = info.get('title') or info.get('id') or '?'
    extractor = info.get('extractor_key') or info.get('extractor') or '?'
    dur = info.get('duration')
    dur_s = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else '?'
    uploader = info.get('uploader') or info.get('channel') or ''
    upl_s = f", by {uploader}" if uploader else ''
    print(f"[INFO] {CLR.BOLD}{title}{CLR.RESET}  ({extractor}, {dur_s}{upl_s})")


def render_listing(info: dict, include_auto_subs: bool) -> None:
    """Print everything the URL offers — for --list mode."""
    summarize_info(info)
    video, audio, combo = classify_formats(info)
    print(f"\n[INFO] {len(video)} video-only, {len(audio)} audio-only, {len(combo)} combined formats.")
    if video:
        print(f"\n  {CLR.BOLD}Video-only:{CLR.RESET}")
        for f in video:
            print(f"    {_describe_video(f)}")
    if audio:
        print(f"\n  {CLR.BOLD}Audio-only:{CLR.RESET}")
        for f in audio:
            print(f"    {_describe_audio(f)}")
    if combo:
        print(f"\n  {CLR.BOLD}Combined v+a:{CLR.RESET}")
        for f in combo:
            print(f"    {_describe_video(f)} +audio")
    subs = _collect_subs(info, include_auto_subs)
    if subs:
        manual = [s for s in subs if s[2] == 'manual']
        auto = [s for s in subs if s[2] == 'auto']
        print(f"\n  {CLR.BOLD}Subtitles:{CLR.RESET} {len(manual)} manual, {len(auto)} auto-generated")
        for lang, name, kind in subs:
            tag = "(auto)" if kind == 'auto' else ""
            nm = f" - {name}" if name else ''
            print(f"    {lang:<6}{nm}  {tag}")


def auto_plan(info: dict, max_height: int, include_auto_subs: bool) -> tuple[str, bool, list, bool]:
    """Fully-automatic selection: best video + ALL audio + ALL subs.

    Returns (format_string, allow_multi_audio, sub_langs, write_auto_subs)."""
    video, audio, combo = classify_formats(info)

    # Pick the highest-quality eligible video stream.
    elig_video = video
    if max_height and max_height > 0:
        elig_video = [f for f in video if (f.get('height') or 0) <= max_height] or video
    elig_combo = combo
    if max_height and max_height > 0:
        elig_combo = [f for f in combo if (f.get('height') or 0) <= max_height] or combo

    if elig_video:
        vid_fid = elig_video[0].get('format_id')
        # Add every audio-only stream so all language tracks land in the MKV.
        audio_ids = [a.get('format_id') for a in audio if a.get('format_id')]
        if audio_ids:
            fmt = vid_fid + '+' + '+'.join(audio_ids) + f"/{vid_fid}+bestaudio/best"
            allow_multi = len(audio_ids) > 1
        else:
            fmt = f"{vid_fid}+bestaudio/{vid_fid}/best"
            allow_multi = False
    elif elig_combo:
        fmt = elig_combo[0].get('format_id') or 'best'
        allow_multi = False
    else:
        fmt = 'bv*+ba/best' if not (max_height and max_height > 0) else \
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best"
        allow_multi = False

    subs = _collect_subs(info, include_auto=include_auto_subs)
    if subs:
        sub_langs = ['all']
        write_auto = include_auto_subs and any(s[2] == 'auto' for s in subs)
    else:
        sub_langs = []
        write_auto = False
    return fmt, allow_multi, sub_langs, write_auto


def interactive_plan(info: dict, max_height: int, include_auto_subs_default: bool
                     ) -> tuple[str, bool, list, bool] | None:
    """Interactive selection. Returns the same tuple as auto_plan, or None on cancel."""
    video, audio, combo = classify_formats(info)
    subs = _collect_subs(info, include_auto=include_auto_subs_default)

    if not video and not combo:
        print("[WARN] No selectable video formats found; falling back to yt-dlp 'best'.")
        return 'best', False, ['all'] if subs else [], include_auto_subs_default

    video_choice = prompt_video(video, combo, max_height)
    if video_choice == 'CANCEL':
        return None

    audio_ids = []
    # When the user picks a combined stream we still let them add audio tracks if there
    # are extra audio-only streams (multilingual dubs); but the prompt becomes optional.
    if video_choice.startswith('split:'):
        audio_ids = prompt_audio(audio)
    elif audio:
        print(f"\n{CLR.DIM}[INFO] Combined stream chosen; you can still layer extra audio tracks if you want.{CLR.RESET}")
        audio_ids = prompt_audio(audio)

    if subs:
        sub_langs, write_auto = prompt_subs(subs, include_auto_subs_default)
    else:
        sub_langs, write_auto = [], False

    fmt, allow_multi = build_format_selector(video_choice, audio_ids, info)
    return fmt, allow_multi, sub_langs, write_auto


# --------------------------------------------------------------------------- #
#  Main download
# --------------------------------------------------------------------------- #
def download_one(
    url: str,
    info: dict,
    out_dir: str | None,
    out_name: str | None,
    plan: tuple[str, bool, list, bool],
    container: str,
    max_connections: int,
    cookies_netscape: str | None,
    cookies_from_browser: str | None,
    ffmpeg_location: str | None,
    embed_thumbnail: bool,
    embed_metadata: bool,
    verbose: bool,
    *,
    allow_unplayable: bool = False,
    no_check_cert: bool = False,
    referer: str | None = None,
    extra_headers: dict | None = None,
    excluded_extractors: list[str] | None = None,
) -> bool:
    fmt, allow_multi, sub_langs, write_auto = plan
    title = info.get('title') or info.get('id') or 'video'

    if out_name:
        base = safe_filename(out_name)
        # Strip any existing video extension; yt-dlp will append the right one and
        # merge_output_format takes care of the final container.
        stem = re.sub(r'\.(mkv|mp4|webm|m4a|mka)$', '', base, flags=re.IGNORECASE)
        out_template = os.path.join(out_dir or '.', f"{stem}.%(ext)s")
    else:
        # yt-dlp's own templating with our safe-character set
        safe_title = safe_filename(title, fallback=info.get('id') or 'video')
        out_template = os.path.join(out_dir or '.',
                                    f"{safe_title} [%(id)s].%(ext)s")

    progress = ProgressBars()
    opts = build_ydl_opts(
        out_template=out_template,
        container=container,
        format_string=fmt,
        allow_multi_audio=allow_multi,
        sub_langs=sub_langs or None,
        write_auto_subs=write_auto,
        embed_subs=bool(sub_langs),
        embed_thumbnail=embed_thumbnail,
        embed_metadata=embed_metadata,
        max_connections=max_connections,
        cookies_netscape_path=cookies_netscape,
        cookies_from_browser=cookies_from_browser,
        progress=progress,
        ffmpeg_location=ffmpeg_location,
        verbose=verbose,
        extra_headers=extra_headers,
        allow_unplayable=allow_unplayable,
        no_check_cert=no_check_cert,
        referer=referer,
        excluded_extractors=excluded_extractors,
    )

    if verbose:
        print(f"[INFO] Format selector: {fmt}")
        print(f"[INFO] Subtitle langs: {sub_langs or '(none)'} (auto-generated: {write_auto})")
        print(f"[INFO] Concurrent fragment connections: {max_connections}")
        print(f"[INFO] Output template: {out_template}")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ret = ydl.download([url])
        progress.close_all()
        if ret == 0:
            print(f"\n[OK] {title} downloaded successfully.")
            return True
        print(f"\n[ERROR] yt-dlp returned non-zero exit code {ret}.")
        return False
    except yt_dlp.utils.DownloadError as e:
        progress.close_all()
        print(f"\n[ERROR] Download failed: {e}")
        return False
    except KeyboardInterrupt:
        progress.close_all()
        print(f"\n[WARN] Interrupted by user. Partial files were kept; rerun to resume.")
        return False
    except Exception as e:
        progress.close_all()
        print(f"\n[ERROR] Unexpected error: {e}")
        return False


# --------------------------------------------------------------------------- #
#  Top-level orchestration
# --------------------------------------------------------------------------- #
def is_playlist_info(info: dict) -> bool:
    return info.get('_type') == 'playlist' or bool(info.get('entries'))


def main(
    url: str,
    *,
    out_dir: str | None = None,
    out_name: str | None = None,
    container: str = DEFAULT_CONTAINER,
    max_height: int = DEFAULT_MAX_HEIGHT,
    max_connections: int | None = None,
    auto: bool = False,
    list_only: bool = False,
    select: bool = False,
    subs_arg: str | None = None,          # 'all' / 'none' / 'en,cs,...'
    include_auto_subs: bool = DEFAULT_INCLUDE_AUTO_SUBS,
    audios_arg: str | None = None,        # 'all' / 'none' / 'en,es,...' (filters by language)
    embed_thumbnail: bool = False,
    embed_metadata: bool = True,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    auto_cookies: bool = AUTO_COOKIES,
    use_color: bool = USE_COLOR,
    verbose: bool = False,
    # Network / source-tolerance knobs (useful for self-hosted NAS players)
    allow_unplayable: bool = False,
    no_check_cert: bool = False,
    referer: str | None = None,
    extra_headers: dict | None = None,
    excluded_extractors: list[str] | None = None,
) -> int:
    setup_console(use_color)

    # When the user opts in to allow_unplayable_formats they're explicitly saying
    # "I know this isn't real DRM, just fetch it" — so also skip yt-dlp's hardcoded
    # KnownDRMIE that ALWAYS raises (unaffected by allow_unplayable_formats). Some
    # NAS players' HTML happens to reference a KnownDRM-listed domain (footer link,
    # JW Player demo URL, etc.), which would otherwise sink the whole extraction.
    excluded_extractors = list(excluded_extractors or [])
    if allow_unplayable and 'KnownDRM' not in excluded_extractors:
        excluded_extractors.append('KnownDRM')

    # --- 1) Connection budget --------------------------------------------------
    if max_connections is None:
        if auto:
            max_connections, logical = _auto_connections()
            print(f"[INFO] --auto: {logical} logical CPU(s) detected, "
                  f"using {max_connections} parallel fragment connections.")
        else:
            max_connections = DEFAULT_MAX_CONNECTIONS

    # --- 2) ffmpeg -------------------------------------------------------------
    ffmpeg_path = ensure_ffmpeg(verbose) if not list_only else _resolve_ffmpeg(FFMPEG)
    if not list_only and not ffmpeg_path:
        print("[ERROR] ffmpeg not found and could not be auto-downloaded.")
        print("        yt-dlp needs ffmpeg to merge video+audio and embed subs into MKV.")
        print("        Install ffmpeg and put it on PATH, edit FFMPEG at the top of the script,")
        print("        or set FFMPEG_DOWNLOAD_URL (already preset for Windows).")
        return 2
    ffmpeg_location = os.path.dirname(ffmpeg_path) if ffmpeg_path else None

    # --- 3) Cookies ------------------------------------------------------------
    if cookies_from_browser and cookies_file:
        print("[WARN] Both --cookies and --cookies-from-browser given; --cookies wins.")
        cookies_from_browser = None

    netscape_cookies_path = None
    cleanup_cookies = None
    if cookies_file:
        cookies_files = [cookies_file]
    elif auto_cookies and not cookies_from_browser:
        cookies_files = auto_detect_cookies(verbose)
    else:
        cookies_files = []
    if cookies_files:
        # yt-dlp's --cookies wants Netscape format. Merge whatever the user has into
        # one temp file and feed that to yt-dlp.
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        tmp.close()
        try:
            merge_cookies_to_netscape_file(cookies_files, tmp.name)
            netscape_cookies_path = tmp.name
            cleanup_cookies = tmp.name
            if verbose:
                print(f"[INFO] Cookies merged for yt-dlp: {tmp.name}")
        except (FileNotFoundError, ValueError) as e:
            print(f"[ERROR] Failed to load cookies: {e}")
            try:
                os.remove(tmp.name)
            except OSError:
                pass
            return 1

    try:
        # Common kwargs that go to BOTH probe_info and download_one. Defined once so
        # we don't forget to update one call site when adding a new flag.
        _net_kwargs = {
            'allow_unplayable': allow_unplayable,
            'no_check_cert': no_check_cert,
            'referer': referer,
            'extra_headers': extra_headers or None,
            'excluded_extractors': excluded_extractors or None,
        }

        # --- 4) Probe ----------------------------------------------------------
        print(f"[INFO] Probing {url} ...")
        info = probe_info(url, netscape_cookies_path, cookies_from_browser, verbose, **_net_kwargs)
        if info is None:
            print("[ERROR] No video info available. Cookies may be missing/expired, "
                  "the URL may be private, the site uses real DRM, or it isn't supported.")
            if not allow_unplayable:
                print("        For self-hosted streamers (NAS / Jellyfin / Plex) that flagged DRM,")
                print("        try again with --allow-unplayable-formats (and --no-check-certificate")
                print("        if the server uses a self-signed cert).")
            else:
                print("        --allow-unplayable-formats is already on. If yt-dlp still says ")
                print("        'site is known to use DRM', the page HTML references one of yt-dlp's")
                print("        hardcoded DRM-only domains (KnownDRMIE) — that extractor is now")
                print("        auto-excluded but the page may also not expose a direct stream URL.")
                print("        Last-resort fix: in your browser open DevTools (F12) -> Network,")
                print("        filter by 'm3u8' or 'mpd', start playback, copy the request URL,")
                print("        and pass THAT URL to this script instead of the page URL.")
            return 1

        # --- 5) Playlist? selection --------------------------------------------
        if is_playlist_info(info):
            entries = [e for e in (info.get('entries') or []) if e]
            if not entries:
                print("[ERROR] Playlist contains no entries.")
                return 1
            pl_title = info.get('title') or info.get('id') or 'playlist'
            print(f"[INFO] Playlist '{pl_title}': {len(entries)} entries.")

            if list_only:
                for i, e in enumerate(entries, 1):
                    t = (e.get('title') or e.get('id') or '?')
                    print(f"   {i:>3}) {t}")
                return 0

            if select and is_tty() and not auto:
                idx = prompt_playlist_selection(entries)
                if not idx:
                    print("[INFO] Nothing selected; exiting.")
                    return 0
                entries = [entries[i - 1] for i in idx]
                print(f"[INFO] Selected {len(entries)} entry(ies).")

            # Apply one shared plan across the whole playlist for sanity.
            # We probe the first entry to decide. (yt-dlp playlist entries may need
            # a second extract pass to get formats.)
            print(f"[INFO] Resolving formats for entry 1/{len(entries)} to set the policy ...")
            first_url = entries[0].get('webpage_url') or entries[0].get('url') or entries[0].get('original_url')
            if not first_url:
                print("[ERROR] Could not get a URL for the first entry.")
                return 1
            first_info = probe_info(first_url, netscape_cookies_path, cookies_from_browser,
                                    verbose, **_net_kwargs)
            if first_info is None:
                return 1

            if auto or not is_tty():
                plan = _override_plan_from_args(
                    auto_plan(first_info, max_height, include_auto_subs),
                    first_info, subs_arg, include_auto_subs, audios_arg)
            else:
                summarize_info(first_info)
                plan = interactive_plan(first_info, max_height, include_auto_subs)
                if plan is None:
                    print("[INFO] Cancelled.")
                    return 0
                plan = _override_plan_from_args(plan, first_info, subs_arg, include_auto_subs, audios_arg)

            ok = 0; fail = 0
            for i, e in enumerate(entries, 1):
                eu = e.get('webpage_url') or e.get('url') or e.get('original_url')
                if not eu:
                    fail += 1; continue
                print(f"\n{CLR.BOLD}=== [{i}/{len(entries)}] {e.get('title') or eu}{CLR.RESET}")
                e_info = probe_info(eu, netscape_cookies_path, cookies_from_browser,
                                    verbose, **_net_kwargs)
                if e_info is None:
                    fail += 1; continue
                if download_one(eu, e_info, out_dir, None, plan, container, max_connections,
                                netscape_cookies_path, cookies_from_browser, ffmpeg_location,
                                embed_thumbnail, embed_metadata, verbose, **_net_kwargs):
                    ok += 1
                else:
                    fail += 1
            print(f"\n[INFO] Playlist done: {ok} succeeded, {fail} failed, out of {len(entries)}.")
            return 0 if fail == 0 else 1

        # --- 6) Single video ----------------------------------------------------
        summarize_info(info)

        if list_only:
            render_listing(info, include_auto_subs)
            return 0

        if auto or not is_tty():
            if not is_tty() and not auto:
                print("[INFO] No interactive terminal; running with --auto defaults.")
            plan = auto_plan(info, max_height, include_auto_subs)
            plan = _override_plan_from_args(plan, info, subs_arg, include_auto_subs, audios_arg)
            fmt, allow_multi, sub_langs, write_auto = plan
            # Brief, human-readable summary of the plan.
            vid_label = "best video" + (f" (\u2264{max_height}p)" if max_height else "")
            n_audio_in_fmt = max(0, fmt.split('/', 1)[0].count('+'))   # count '+' in the primary selector
            if n_audio_in_fmt == 0:
                audio_label = "auto audio"
            elif n_audio_in_fmt == 1:
                audio_label = "1 audio track"
            else:
                audio_label = f"{n_audio_in_fmt} audio tracks"
            if sub_langs == ['all']:
                subs_label = "all subs"
            elif sub_langs:
                subs_label = f"subs={','.join(sub_langs)}"
            else:
                subs_label = "no subs"
            print(f"[INFO] Plan: {vid_label} + {audio_label} + {subs_label}  ->  .{container}")
        else:
            plan = interactive_plan(info, max_height, include_auto_subs)
            if plan is None:
                print("[INFO] Cancelled.")
                return 0
            plan = _override_plan_from_args(plan, info, subs_arg, include_auto_subs, audios_arg)

        ok = download_one(url, info, out_dir, out_name, plan, container, max_connections,
                          netscape_cookies_path, cookies_from_browser, ffmpeg_location,
                          embed_thumbnail, embed_metadata, verbose, **_net_kwargs)
        return 0 if ok else 1

    finally:
        if cleanup_cookies:
            try:
                os.remove(cleanup_cookies)
            except OSError:
                pass


def _override_plan_from_args(
    plan: tuple[str, bool, list, bool],
    info: dict,
    subs_arg: str | None,
    include_auto_subs: bool,
    audios_arg: str | None,
) -> tuple[str, bool, list, bool]:
    """Apply CLI overrides on top of the chosen plan.

    --subs all      -> grab every subtitle language
    --subs none     -> no subtitles at all
    --subs en,cs    -> just those languages
    --audios all/none/en,cs : currently affects only auto-pick (filters audio formats by language).
    The audio override only kicks in when the plan was made by auto_plan (we re-derive)."""
    fmt, allow_multi, sub_langs, write_auto = plan

    if subs_arg is not None:
        s = subs_arg.strip().lower()
        if s in ('none', 'no', ''):
            sub_langs, write_auto = [], False
        elif s == 'all':
            subs = _collect_subs(info, include_auto=include_auto_subs)
            if subs:
                sub_langs = ['all']
                write_auto = include_auto_subs and any(x[2] == 'auto' for x in subs)
            else:
                sub_langs, write_auto = [], False
        else:
            wanted = [x.strip() for x in subs_arg.split(',') if x.strip()]
            available = _collect_subs(info, include_auto=include_auto_subs)
            sub_langs = [lang for (lang, _name, _kind) in available if lang in wanted]
            write_auto = include_auto_subs and any(
                (lang in wanted and kind == 'auto') for (lang, _name, kind) in available)
            if not sub_langs:
                print(f"[WARN] None of the requested subtitle languages ({subs_arg}) are available.")

    if audios_arg is not None:
        s = audios_arg.strip().lower()
        _video, audio, _combo = classify_formats(info)
        if not audio:
            pass  # nothing to filter
        else:
            video_part = fmt.split('+', 1)[0]
            if s in ('none', 'no'):
                fmt = video_part + "+bestaudio/best"
                allow_multi = False
            elif s == 'all':
                ids = [a.get('format_id') for a in audio if a.get('format_id')]
                fmt = video_part + '+' + '+'.join(ids) + f"/{video_part}+bestaudio/best"
                allow_multi = len(ids) > 1
            else:
                wanted = {x.strip().lower() for x in audios_arg.split(',') if x.strip()}
                ids = [a.get('format_id') for a in audio
                       if a.get('format_id') and (a.get('language') or '').lower() in wanted]
                if ids:
                    fmt = video_part + '+' + '+'.join(ids) + f"/{video_part}+bestaudio/best"
                    allow_multi = len(ids) > 1
                else:
                    print(f"[WARN] No audio streams matched languages ({audios_arg}); using best.")
                    fmt = video_part + "+bestaudio/best"
                    allow_multi = False

    return fmt, allow_multi, sub_langs, write_auto


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _cli_parser() -> argparse.ArgumentParser:
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

    p = argparse.ArgumentParser(
        description="Universal intelligent video downloader (yt-dlp under the hood). "
                    "Downloads the best video + every audio track + every subtitle as ONE .mkv.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  py %(prog)s "https://www.youtube.com/watch?v=..."         # interactive
  py %(prog)s URL --auto                                    # grab everything, no prompts
  py %(prog)s URL -q 1080 --subs en,cs --no-auto-subs       # cap 1080p, only these subs
  py %(prog)s URL --list                                    # just show what's available
  py %(prog)s PLAYLIST_URL --auto --select                  # pick which entries to grab
  py %(prog)s URL --cookies-from-browser firefox            # use your browser's cookies
""")
    p.add_argument("url", type=str, help="Video or playlist URL (any site supported by yt-dlp).")

    p.add_argument("-o", "--output", type=str, default=None,
                   help="Output filename (single video only). Extension is forced to match --container.")
    p.add_argument("-d", "--out-dir", type=str, default=None,
                   help="Directory to save into (default: current directory).")
    p.add_argument("--container", type=str, default=DEFAULT_CONTAINER, choices=['mkv', 'mp4', 'webm'],
                   help=f"Output container. mkv is recommended (multi-audio + soft subs). Default: {DEFAULT_CONTAINER}.")

    p.add_argument("-q", "--max-height", type=nonneg_int, default=DEFAULT_MAX_HEIGHT,
                   help="Cap video height (e.g. 720, 1080). 0 = best available. Default: best.")
    p.add_argument("-m", "--max-connections", type=positive_int, default=None,
                   help=f"Parallel fragment connections. Default {DEFAULT_MAX_CONNECTIONS}; --auto picks from CPU.")
    p.add_argument("--auto", action="store_true",
                   help="No prompts: best video + ALL audio tracks + ALL subs, CPU-tuned connections.")

    p.add_argument("-l", "--list", action="store_true",
                   help="Probe only; list every video/audio/subtitle format. Do not download.")
    p.add_argument("-s", "--select", action="store_true",
                   help="For playlists: interactively pick which entries to download.")

    p.add_argument("--subs", type=str, default=None,
                   help="Override subtitle selection: 'all', 'none', or comma list like 'en,cs,de'.")
    p.add_argument("--no-auto-subs", action="store_true",
                   help="Do not include auto-generated (YouTube-style) captions.")
    p.add_argument("--audios", type=str, default=None,
                   help="Override audio selection: 'all', 'none', or comma list of language codes "
                        "like 'en,ja' (matches audio stream language tags).")

    p.add_argument("--embed-thumbnail", action="store_true",
                   help="Embed the cover thumbnail into the MKV.")
    p.add_argument("--no-metadata", action="store_true",
                   help="Do not write title/chapter/uploader metadata into the file.")

    p.add_argument("--cookies", type=str, default=None,
                   help="Path to a Netscape cookies.txt or JSON cookie export.")
    p.add_argument("--cookies-from-browser", type=str, default=None,
                   metavar="BROWSER",
                   help="Use the given browser's cookies directly: firefox, chrome, edge, brave, etc.")
    p.add_argument("--no-auto-cookies", action="store_true",
                   help="Do not auto-use a *.json cookie file found next to the script.")

    p.add_argument("--no-color", action="store_true", help="Disable colored output.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging from yt-dlp + this script.")

    # --- Source tolerance: for self-hosted streamers (NAS / Jellyfin / Plex / generic HLS) ---
    g = p.add_argument_group("source tolerance (self-hosted NAS / generic HLS)")
    g.add_argument("--allow-unplayable-formats", action="store_true",
                   help="Skip yt-dlp's preventive DRM heuristic. Self-hosted streamers often "
                        "send AES-128 encrypted HLS with the key in the playlist; that's not DRM "
                        "and ffmpeg can decrypt it transparently. Do NOT use for actual paid "
                        "streaming services — the resulting file will not be playable anyway.")
    g.add_argument("--no-check-certificate", action="store_true",
                   help="Skip TLS certificate validation. Use for LAN NAS servers with self-signed certs.")
    g.add_argument("--referer", type=str, default=None,
                   help="Set a Referer header. Some custom players require it to authorize streaming.")
    g.add_argument("--header", action="append", default=[], metavar="KEY:VALUE",
                   help="Add an arbitrary HTTP header (repeatable). Example: --header 'X-Token: abc123'.")
    g.add_argument("--exclude-extractor", action="append", default=[], metavar="NAME",
                   help="Disable a specific yt-dlp extractor (repeatable). 'KnownDRM' is auto-excluded "
                        "when --allow-unplayable-formats is set, so the generic extractor doesn't "
                        "bail on a stray KnownDRM-listed domain found in your page's HTML.")

    p.add_argument("--version", action="version", version="%(prog)s 1.2.0")
    return p


if __name__ == "__main__":
    args = _cli_parser().parse_args()

    # Parse --header KEY:VALUE pairs into a dict (last one wins for duplicate keys).
    extra_headers: dict = {}
    for raw in args.header or []:
        if ':' not in raw:
            print(f"[WARN] Ignoring malformed --header (no ':'): {raw}")
            continue
        k, _, v = raw.partition(':')
        k = k.strip(); v = v.strip()
        if k:
            extra_headers[k] = v

    sys.exit(main(
        args.url,
        out_dir=args.out_dir,
        out_name=args.output,
        container=args.container,
        max_height=args.max_height,
        max_connections=args.max_connections,
        auto=args.auto,
        list_only=args.list,
        select=args.select,
        subs_arg=args.subs,
        include_auto_subs=(DEFAULT_INCLUDE_AUTO_SUBS and not args.no_auto_subs),
        audios_arg=args.audios,
        embed_thumbnail=args.embed_thumbnail,
        embed_metadata=(not args.no_metadata),
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        auto_cookies=(AUTO_COOKIES and not args.no_auto_cookies),
        use_color=(USE_COLOR and not args.no_color),
        verbose=args.verbose,
        allow_unplayable=args.allow_unplayable_formats,
        no_check_cert=args.no_check_certificate,
        referer=args.referer,
        extra_headers=extra_headers or None,
        excluded_extractors=args.exclude_extractor or None,
    ))
