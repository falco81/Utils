from urllib.parse import unquote, unquote_plus, urlencode, urljoin, urlparse
import requests
import argparse
import sys
from tqdm import tqdm
import os
import re
import threading
import math
import shutil
import json
import subprocess
import time
import atexit
import unicodedata
import uuid
from collections import Counter, defaultdict
from http.cookiejar import MozillaCookieJar
from requests.cookies import RequestsCookieJar
import tempfile
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

try:
    import colorama
    _HAS_COLORAMA = True
except Exception:
    _HAS_COLORAMA = False

# =============================================================================
#  USER CONFIG — edit these defaults. The command line (-t, -w, -m, ...) wins.
# =============================================================================
DEFAULT_THREADS = 16              # -t : download threads per file
DEFAULT_FOLDER_WORKERS = 0        # -w : videos downloaded at once (0 = ALL at once)
DEFAULT_MAX_CONNECTIONS = 64      # -m : hard cap on simultaneous connections
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024   # -c : streaming chunk size in bytes (4 MiB)
DEFAULT_RECURSIVE = True          # descend into subfolders for folder URLs
AUTO_COOKIES = True               # if --cookies is omitted, auto-use a *.json cookie file found nearby
USE_COLOR = True                  # colored output (needs colorama on Windows)
FORCE_ASCII_BARS = None           # None = auto (ASCII on Windows); True/False to force
# Above this many concurrent files, show one overall bar instead of one bar per file.
PER_FILE_BAR_LIMIT = 32
SEGMENT_MIB = 32                  # folder mode: split each file into segments of this size (MiB)
                                  # so freed connections flow to remaining files (work-stealing).
DROPBOX_DEFAULT_CONNECTIONS = 8   # Dropbox rate-limits shared links hard, so unless the user
                                  # passes an explicit -m, Dropbox downloads default to this many
                                  # connections (a high auto value like 80 makes them 429/truncate).
BAR_NCOLS = 100                   # fixed progress-bar width so bars don't stretch across wide terminals
BAR_DESC_WIDTH = 26               # fixed filename column width so all bars line up
TEMP_SUBDIR = ".temp"             # all scratch files (.part/.lock/.parts/.merging/.video...) go here


def _temp_dir_for(final_path: str) -> str:
    """The sibling .temp directory next to a file's final location."""
    d = os.path.dirname(final_path) or "."
    return os.path.join(d, TEMP_SUBDIR)


def _temp_artifact(final_path: str, suffix: str) -> str:
    """Path (inside .temp) for a scratch artifact of `final_path`, e.g. suffix='.part0'.
    Keeps the working directory clean; the finished file still lands next to .temp."""
    tdir = _temp_dir_for(final_path)
    try:
        os.makedirs(tdir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(tdir, os.path.basename(final_path) + suffix)


def _cleanup_temp_dir(directory: str) -> None:
    """Remove the .temp folder if it is now empty (called after a run finishes). Leftover
    parts from an interrupted run keep it around so a re-run can resume."""
    tdir = os.path.join(directory or ".", TEMP_SUBDIR)
    try:
        if os.path.isdir(tdir) and not os.listdir(tdir):
            os.rmdir(tdir)
    except OSError:
        pass
# Network timeouts (seconds). Without these a stalled connection (e.g. Google's videoplayback
# CDN ignoring a HEAD request) would hang the whole run forever.
CONNECT_TIMEOUT = 15              # max time to establish a TCP/TLS connection
META_READ_TIMEOUT = 30           # read timeout for small metadata/probe/listing requests
DOWNLOAD_READ_TIMEOUT = 120      # max gap between received chunks during an actual download
# --- Patreon-native (Vimeo/Mux HLS) settings ---
DEFAULT_MAX_HEIGHT = 0            # -q : cap HLS video height (e.g. 720). 0 = best available.
FFMPEG = "ffmpeg"                 # ffmpeg exe, a FOLDER containing it, or "ffmpeg" if on PATH
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
VIMEO_REFERER = "https://player.vimeo.com/"
VIMEO_HEADERS = {'Referer': VIMEO_REFERER, 'Origin': 'https://player.vimeo.com'}
MUX_HEADERS = {'Referer': "https://www.patreon.com/"}
# =============================================================================

# Runtime console state (filled in by setup_console()).
ASCII_BARS = False
# Clean, aligned bar layout (no ragged columns, no odd characters).
BAR_FORMAT = '{desc} {percentage:3.0f}% |{bar}| {n_fmt:>7}/{total_fmt:>7} {rate_fmt:>10}'


class _Palette:
    def __init__(self, enabled: bool):
        if enabled:
            self.RESET = '\033[0m'; self.RED = '\033[31m'; self.GREEN = '\033[32m'
            self.YELLOW = '\033[33m'; self.CYAN = '\033[36m'; self.DIM = '\033[2m'
        else:
            self.RESET = self.RED = self.GREEN = self.YELLOW = self.CYAN = self.DIM = ''


CLR = _Palette(False)

_builtin_print = print


def _cprint(*args, **kwargs):
    """Drop-in print that colorizes by [INFO]/[WARN]/[ERROR] prefix and success lines."""
    if CLR.RESET and args and isinstance(args[0], str):
        s = args[0]
        stripped = s.lstrip()
        color = ''
        if stripped.startswith('[ERROR]'):
            color = CLR.RED
        elif stripped.startswith('[WARN]'):
            color = CLR.YELLOW
        elif stripped.startswith('[INFO]'):
            color = CLR.CYAN
        elif 'downloaded successfully' in s:
            color = CLR.GREEN
        if color:
            args = (color + s + CLR.RESET,) + args[1:]
    _builtin_print(*args, **kwargs)


print = _cprint  # shadow builtin within this module so all messages get colored


def _enable_windows_vt() -> bool:
    """Enable ANSI escape processing on a Windows 10+ console without colorama."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return True
    except Exception:
        return False


def setup_console(use_color: bool) -> None:
    """Make the console behave: UTF-8 output, ASCII progress bars on Windows, and color."""
    global CLR, ASCII_BARS

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')  # Python 3.7+: avoids garbled characters
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
    # A clean 2-level fill (space -> '=') instead of tqdm's default ' 123456789#',
    # which renders the digit characters on the bar tip that looked like "weird characters".
    ASCII_BARS = ' =' if use_ascii else False
    CLR = _Palette(enabled)


def _fit_desc(name: str, width: int) -> str:
    """Pad/middle-truncate a label to a fixed width so progress bars line up.
    Middle-truncation keeps both the start and the (distinguishing) end of filenames."""
    name = str(name)
    if len(name) <= width:
        return name.ljust(width)
    keep = width - 2
    head = (keep + 1) // 2
    tail = keep - head
    return name[:head] + '..' + (name[-tail:] if tail > 0 else '')


def _bar_ncols() -> int:
    """Bar width capped to BAR_NCOLS but never wider than the actual terminal."""
    try:
        cols = shutil.get_terminal_size((BAR_NCOLS, 20)).columns
    except Exception:
        cols = BAR_NCOLS
    return max(40, min(BAR_NCOLS, cols - 1))


def make_bar(**kwargs):
    """tqdm factory: applies the ASCII setting, fixed width, and aligned layout."""
    if kwargs.get('desc') is not None:
        kwargs['desc'] = _fit_desc(kwargs['desc'], BAR_DESC_WIDTH)
    kwargs.setdefault('ascii', ASCII_BARS)
    kwargs.setdefault('ncols', _bar_ncols())
    kwargs.setdefault('bar_format', BAR_FORMAT)
    return tqdm(**kwargs)


# Global cap on how many network connections may be open at the same time.
# This is the safety valve that prevents "insufficient resources" / WinError 10055
# when (folder_workers * threads) would otherwise open hundreds of sockets at once.
_conn_semaphore = None
_max_connections = 32
_max_conn_explicit = False   # True once the user passes an explicit -m (disables source caps)


def set_connection_limit(n: int) -> None:
    global _conn_semaphore, _max_connections
    _max_connections = max(1, n)
    _conn_semaphore = threading.BoundedSemaphore(_max_connections)


class _NullCtx:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


def connection_slot():
    """Context manager: acquire a global connection slot (or a no-op if unset)."""
    return _conn_semaphore if _conn_semaphore is not None else _NullCtx()


def _build_adapter() -> HTTPAdapter:
    if Retry is not None:
        retry = Retry(
            total=4, connect=4, read=2, backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(('GET', 'HEAD')),
            raise_on_status=False,
        )
    else:
        retry = 4
    # Small per-session pool: each session carries little traffic; the global
    # semaphore is what bounds total concurrency.
    return HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)


def new_session_from(base: requests.Session) -> requests.Session:
    """Create a fresh session that reuses another session's cookies/headers,
    with a retry-enabled adapter mounted. Caller is responsible for closing it."""
    s = requests.Session()
    s.cookies.update(base.cookies)
    s.headers.update(base.headers)
    adapter = _build_adapter()
    s.mount('https://', adapter)
    s.mount('http://', adapter)
    return s


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a process is still running (cross-platform safe)."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        # IMPORTANT: on Windows os.kill(pid, 0) calls TerminateProcess() and would KILL the
        # process, so we must query with OpenProcess instead.
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # No handle: access-denied means it's alive; anything else means it's gone.
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                code = wintypes.DWORD()
                if k.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k.CloseHandle(handle)
        except Exception:
            return True  # can't tell -> assume alive (don't reclaim someone else's lock)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# Locks this process is currently holding, released on exit even if interrupted (Ctrl+C).
_held_locks = set()
_held_locks_lock = threading.Lock()


def acquire_lock(lock_path: str) -> bool:
    """Atomically create a lock file for a download target.

    Returns True if the lock was acquired. If another live process already holds it,
    prints a message and returns False. Stale locks (owner no longer running) are reclaimed.
    """
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w') as f:
                f.write(str(os.getpid()))
            with _held_locks_lock:
                _held_locks.add(lock_path)
            return True
        except FileExistsError:
            try:
                with open(lock_path) as f:
                    owner = int((f.read().strip() or "-1"))
            except (ValueError, OSError):
                owner = -1

            if owner != os.getpid() and not _pid_alive(owner):
                # Stale lock from a crashed run -> reclaim it and retry.
                try:
                    os.remove(lock_path)
                    continue
                except OSError:
                    pass

            print("[ERROR] Another instance is already downloading this file.")
            print(f"        Lock held by PID {owner} ({lock_path}).")
            print("        Wait for it to finish, choose a different name with -o,")
            print("        or delete the lock file if you are sure no other run is active.")
            return False


def release_lock(lock_path: str) -> None:
    """Remove the lock file if it belongs to this process."""
    try:
        with open(lock_path) as f:
            owner = int((f.read().strip() or "-1"))
        if owner == os.getpid():
            os.remove(lock_path)
    except (ValueError, OSError):
        pass
    with _held_locks_lock:
        _held_locks.discard(lock_path)


def _release_all_locks():
    """Release every lock this process still holds (runs on normal exit and on Ctrl+C)."""
    with _held_locks_lock:
        paths = list(_held_locks)
    for p in paths:
        release_lock(p)


atexit.register(_release_all_locks)

def load_cookies_from_file(cookies_file: str) -> RequestsCookieJar:
    """Load cookies from a Netscape cookies.txt or JSON export file."""
    if not os.path.exists(cookies_file):
        raise FileNotFoundError(f"Cookies file not found: {cookies_file}")

    with open(cookies_file, 'r') as f:
        content = f.read()

    stripped = content.lstrip()
    if stripped.startswith('[') or stripped.startswith('{'):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON cookies file: {cookies_file}") from exc

        if isinstance(data, dict) and "cookies" in data and isinstance(data["cookies"], list):
            cookies_list = data["cookies"]
        elif isinstance(data, list):
            cookies_list = data
        else:
            raise ValueError(f"Unsupported JSON cookies format: {cookies_file}")

        jar = RequestsCookieJar()
        for cookie in cookies_list:
            if not isinstance(cookie, dict):
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain") or ""
            path = cookie.get("path") or "/"
            expires = cookie.get("expirationDate") or cookie.get("expires")
            if not name or value is None:
                continue
            jar.set(name, value, domain=domain, path=path, expires=expires)
        return jar

    generated_cookie_file = None
    if not stripped.startswith('# Netscape HTTP Cookie File') and not stripped.startswith('# HTTP Cookie File'):
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        temp_file.write('# Netscape HTTP Cookie File\n')
        temp_file.write('# https://curl.haxx.se/rfc/cookie_spec.html\n')
        temp_file.write('# This is a generated file! Do not edit.\n\n')
        temp_file.write(content)
        temp_file.close()
        generated_cookie_file = temp_file.name
        cookies_file = generated_cookie_file

    cookie_jar = MozillaCookieJar(cookies_file)
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    finally:
        if generated_cookie_file and os.path.exists(generated_cookie_file):
            os.remove(generated_cookie_file)

    requests_jar = RequestsCookieJar()
    for cookie in cookie_jar:
        requests_jar.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain,
            path=cookie.path
        )

    return requests_jar

def _looks_like_cookies_json(path: str) -> bool:
    """Cheaply check whether a .json file looks like a browser cookie export."""
    try:
        if os.path.getsize(path) > 5 * 1024 * 1024:  # cookie files are tiny; skip big JSON
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
    """Interactively ask which cookie file(s) to load. Returns a list of paths."""
    print(f"[INFO] Found {len(candidates)} JSON cookie files in the directory:")
    for i, p in enumerate(candidates, 1):
        print(f"   {CLR.CYAN}{i}{CLR.RESET}) {os.path.basename(p)}   "
              f"{CLR.DIM}({_format_mtime(p)}){CLR.RESET}")
    print("Select: number(s) like '1,3', 'a' for ALL, or Enter for the newest (1).")

    for _ in range(3):
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return [candidates[0]]

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
                    chosen = candidates[idx - 1]
                    if chosen not in picks:
                        picks.append(chosen)
                else:
                    raise ValueError
            if picks:
                return picks
        except ValueError:
            pass
        print(f"[WARN] Invalid choice. Enter number(s) 1-{len(candidates)}, 'a', or Enter.")

    print("[WARN] No valid choice; using the newest.")
    return [candidates[0]]


def auto_detect_cookies(verbose: bool) -> list:
    """Find JSON cookie file(s) next to the script or in the working directory.

    Returns a list of chosen paths (possibly empty). With one match it is used directly;
    with several, the user is asked interactively (or the newest is used when not on a TTY).
    """
    import glob

    def _norm(path):
        # normcase handles Windows case-insensitivity (c:\ vs C:\); realpath collapses
        # symlinks and ".." so the same file is never counted twice.
        return os.path.normcase(os.path.realpath(path))

    dirs = []
    seen_dirs = set()
    for d in (os.getcwd(), os.path.dirname(os.path.abspath(sys.argv[0] or '.'))):
        if not d:
            continue
        key = _norm(d)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        dirs.append(d)

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
    print(f"[INFO] Using {len(chosen)} cookie file(s): "
          f"{', '.join(os.path.basename(p) for p in chosen)}")
    return chosen


def get_cookies_session(cookies_files=None) -> requests.Session:
    """Create a requests session, merging cookies from one or more files.

    `cookies_files` may be a single path (str) or a list of paths. When several are
    given, their cookies are merged in order (later files win on name/domain/path clash).
    """
    session = requests.Session()

    if isinstance(cookies_files, str):
        cookies_files = [cookies_files]
    cookies_files = [f for f in (cookies_files or []) if f]

    if cookies_files:
        merged = RequestsCookieJar()
        for cf in cookies_files:
            sub_jar = load_cookies_from_file(cf)
            for cookie in sub_jar:
                merged.set_cookie(cookie)
        session.cookies = merged

    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    })

    adapter = _build_adapter()
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    return session

def extract_drive_id(input_str: str) -> str:
    """Extracts the Google Drive file ID from a URL or returns the input if it's already an ID."""
    pattern = r'/file/d/([a-zA-Z0-9_-]+)'
    match = re.search(pattern, input_str)
    if match:
        return match.group(1)
    return input_str

def extract_drive_target(input_str: str) -> tuple[str, str]:
    """Return (kind, id) where kind is 'folder' or 'file'.

    Recognises both /folders/<id> and /file/d/<id> URLs. A bare id is treated as a file.
    """
    folder_match = re.search(r'/folders/([a-zA-Z0-9_-]+)', input_str)
    if folder_match:
        return 'folder', folder_match.group(1)
    return 'file', extract_drive_id(input_str)


# =============================================================================
#  Patreon collection support
#  Given a Patreon collection or post URL, walk its posts, pull out the Google Drive
#  links (the "WATCH HERE" anchors, but any Drive link is picked up), and feed
#  them into the same folder download path (incl. --select).
# =============================================================================
PATREON_REFERER = "https://www.patreon.com/"
_DRIVE_URL_RE = re.compile(r'https?://drive\.google\.com/[^\s"\'<>\\)]+')
_DROPBOX_URL_RE = re.compile(r'https?://(?:www\.)?dropbox\.com/[^\s"\'<>\\)]+', re.IGNORECASE)


def extract_patreon_collection_id(input_str: str):
    """Return the collection id from a Patreon collection or post URL, or None if it's not one."""
    m = re.search(r'patreon\.com/collection/(\d+)', input_str)
    if m:
        return m.group(1)
    # Also accept a bare collection path, but only when it clearly is one (avoid colliding
    # with Drive URLs, which never contain '/collection/').
    m = re.search(r'/collection/(\d+)', input_str)
    if m:
        return m.group(1)
    return None


def extract_patreon_post_id(input_str: str):
    """Return the numeric post id from a Patreon single-post URL, or None.

    Handles: /posts/<id>, /posts/<slug>-<id>, /<creator>/posts/<slug>-<id>,
    /c/<creator>/posts/<slug>-<id> and query strings."""
    m = re.search(r'patreon\.com/(?:[^?#]*/)?posts/(?:[^/?#]*-)?(\d+)', input_str)
    if m:
        return m.group(1)
    return None


def get_collection_info(collection_id: str, session: requests.Session, verbose: bool):
    """Best-effort (title, campaign_id) for a Patreon collection. Both may be None."""
    url = (f"https://www.patreon.com/api/collection/{collection_id}"
           f"?json-api-version=1.0&json-api-use-default-includes=false")
    title, campaign_id = None, None
    try:
        r = session.get(url, headers={'Referer': PATREON_REFERER, 'Accept': 'application/json'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        d = r.json()
        attrs = d.get('data', {}).get('attributes', {})
        title = attrs.get('title')
        m = re.search(r'/campaign/(\d+)/', r.text)  # campaign id appears in cover-media URLs
        if m:
            campaign_id = m.group(1)
    except Exception:
        pass
    if verbose:
        print(f"[INFO] Patreon collection '{title}', campaign {campaign_id}")
    return title, campaign_id


def list_collection_posts(collection_id: str, campaign_id, session: requests.Session,
                          verbose: bool) -> list:
    """Return all post objects of a collection, following cursor pagination."""
    posts = []
    cursor = None
    page = 0
    while True:
        params = {
            'include': 'collections,drop,primary_image,audio,video,embed',
            'fields[post]': ('post_type,title,content,teaser_text,url,embed,post_file,'
                             'post_metadata,current_user_can_view,published_at'),
            'fields[primary-image]': 'is_fallback,image_small,image_medium,prefer_alternate_display',
            'sort': 'collection_order',
            'filter[collection_id]': collection_id,
            'filter[is_suspended]': 'false',
            'filter[include_drops]': 'true',
            'filter[is_published]': 'true',
            'page[size]': '100',
            'json-api-version': '1.0',
            'json-api-use-default-includes': 'false',
        }
        if campaign_id:
            params['filter[campaign_id]'] = campaign_id
        if cursor:
            params['page[cursor]'] = cursor
        url = "https://www.patreon.com/api/posts?" + urlencode(params)
        r = session.get(url, headers={'Referer': PATREON_REFERER, 'Accept': 'application/json'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        try:
            d = r.json()
        except Exception:
            print(f"[ERROR] Patreon API did not return JSON (status {r.status_code}). "
                  f"Your cookies may be missing or expired.")
            break
        batch = d.get('data', []) or []
        included = d.get('included', []) or []
        if included:
            inc_by_key = {(r.get('type'), r.get('id')): r
                          for r in included if isinstance(r, dict)}
            for p in batch:
                if not isinstance(p, dict):
                    continue
                rels = p.get('relationships') or {}
                mine = []
                for relname in ('video', 'embed', 'audio', 'post_file', 'images', 'attachments_media'):
                    rd = (rels.get(relname) or {}).get('data')
                    items = rd if isinstance(rd, list) else ([rd] if isinstance(rd, dict) else [])
                    for one in items:
                        r = inc_by_key.get((one.get('type'), one.get('id')))
                        if r:
                            mine.append(r)
                if mine:
                    p['_included'] = mine
        posts.extend(batch)
        page += 1
        cursor = (((d.get('meta') or {}).get('pagination') or {}).get('cursors') or {}).get('next')
        if verbose:
            print(f"[INFO] Patreon posts page {page}: +{len(batch)} (total {len(posts)})")
        if not cursor or not batch:
            break
        if page > 500:  # safety valve
            print("[WARN] Stopping Patreon pagination after 500 pages.")
            break
    return posts


def _walk_strings(obj):
    """Yield every string found anywhere inside a nested dict/list structure."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def extract_drive_links_from_post(post: dict) -> list:
    """Return an ordered, de-duplicated list of Google Drive URLs found anywhere in a post."""
    return _extract_links_from_post(post, ('drive.google.com',), (_DRIVE_URL_RE,))


def extract_dropbox_links_from_post(post: dict) -> list:
    """Return an ordered, de-duplicated list of Dropbox URLs found anywhere in a post."""
    return _extract_links_from_post(post, ('dropbox.com',), (_DROPBOX_URL_RE,))


def _extract_links_from_post(post: dict, host_needles, url_res) -> list:
    """Shared link scraper. Looks first at the rich-text body's explicit link anchors (the
    usual "WATCH HERE" proklik), then at any matching URL pasted as plain text, then at
    HTML/teaser fields, and finally scans the whole post object as a catch-all."""
    a = post.get('attributes', {}) or {}
    urls = []
    seen = set()

    def matches(u):
        return any(h in u for h in host_needles)

    def add(u):
        u = (u or '').strip().rstrip('.,);')  # trim trailing prose punctuation
        if u and matches(u) and u not in seen:
            seen.add(u)
            urls.append(u)

    def find_all(s):
        for rx in url_res:
            for m in rx.findall(s):
                add(m)

    # 1) Rich-text doc: explicit link marks first, then plain-text URLs inside it.
    cjs = a.get('content_json_string')
    if isinstance(cjs, str) and cjs:
        try:
            doc = json.loads(cjs)
        except Exception:
            doc = None
        if doc is not None:
            def walk_marks(node):
                if isinstance(node, dict):
                    for mk in (node.get('marks') or []):
                        if isinstance(mk, dict) and mk.get('type') == 'link':
                            href = (mk.get('attrs') or {}).get('href') or ''
                            if matches(href):
                                add(href)
                    for v in node.values():
                        walk_marks(v)
                elif isinstance(node, list):
                    for v in node:
                        walk_marks(v)
            walk_marks(doc)
            for s in _walk_strings(doc):
                find_all(s)

    # 2) HTML / teaser fallbacks.
    for fld in ('content', 'teaser_text'):
        val = a.get(fld)
        if isinstance(val, str):
            find_all(val)

    # 3) Catch-all: anything we missed (embeds, etc.).
    for s in _walk_strings(post):
        if matches(s):
            find_all(s)

    return urls


def normalize_dropbox_url(url: str) -> str:
    """Turn a Dropbox share link into a direct-download URL.

    Dropbox share links look like https://www.dropbox.com/s/<hash>/<name>?dl=0 (or the newer
    /scl/fi/<id>/<name>?rlkey=...&dl=0). Forcing dl=1 makes Dropbox serve the file bytes
    (it redirects to dl.dropboxusercontent.com), which is a plain progressive download."""
    parsed = urlparse(url)
    # Rebuild the query with dl=1.
    from urllib.parse import parse_qsl, urlencode as _ue, urlunparse
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q['dl'] = '1'
    new_query = _ue(q)
    return urlunparse(parsed._replace(query=new_query))


def dropbox_filename(url: str, fallback: str) -> str:
    """Derive a filename from a Dropbox URL's path (URL-decoded), else use the fallback."""
    path = urlparse(url).path
    base = unquote(os.path.basename(path))
    base = safe_filename(base, fallback)
    if not os.path.splitext(base)[1]:
        # No extension in the URL; keep the fallback's or default to .mp4.
        ext = os.path.splitext(fallback)[1] or '.mp4'
        base = base + ext
    return base


def _parse_content_disposition_filename(cd: str) -> str:
    """Extract the filename from a Content-Disposition header (handles filename*=UTF-8'')."""
    if not cd:
        return ''
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'filename\s*=\s*([^;]+)', cd, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip('"')
    return ''


def _looks_like_dropbox_id(name: str) -> bool:
    """True if `name` is just a bare Dropbox share id (e.g. '7fc9jqa2hb4lmjd.mp4') rather than
    a real, human filename."""
    stem = os.path.splitext(name)[0]
    return bool(re.fullmatch(r'[a-z0-9]{12,}', stem))


def _dropbox_probe(url: str, session: requests.Session):
    """One ranged GET that returns (size_bytes, real_filename) for a Dropbox direct URL:
    total size from Content-Range, real name from Content-Disposition (or the final URL)."""
    size, name = 0, ''
    try:
        with connection_slot():
            with session.get(url, stream=True, allow_redirects=True,
                             headers={'User-Agent': USER_AGENT, 'Range': 'bytes=0-0'},
                             timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)) as r:
                cr = r.headers.get('content-range', '')
                if '/' in cr and cr.rsplit('/', 1)[-1].strip().isdigit():
                    size = int(cr.rsplit('/', 1)[-1].strip())
                elif r.status_code == 200 and (r.headers.get('content-length', '') or '').isdigit():
                    size = int(r.headers['content-length'])
                name = _parse_content_disposition_filename(r.headers.get('Content-Disposition', ''))
                if not name:
                    name = unquote(os.path.basename(urlparse(r.url).path))
    except requests.RequestException:
        pass
    return size, name


def _download_from_posts(posts, session, chunk_size, num_threads, folder_workers, recursive,
                         verbose, select, out_dir, max_connections, max_height, list_only):
    """Given a list of Patreon post objects, gather every Drive/Dropbox link and native
    (Vimeo/Mux) stream, then download them with the shared pooled engines. Shared by the
    collection flow and the single-post flow."""
    drive_videos = []
    dropbox_items = []   # {'url','filename','title'}
    hls_videos = []      # stream descriptors
    seen_ids = set()
    seen_dropbox = set()
    folder_targets = []  # (folder_id, post_title)
    posts_with_media = 0

    for post in posts:
        a = post.get('attributes', {}) or {}
        post_title = (a.get('title') or str(post.get('id') or '')).strip()
        had_media = False

        # --- Google Drive links ---
        drive_links = extract_drive_links_from_post(post)
        file_ids = []
        for url in drive_links:
            kind, tid = extract_drive_target(url)
            if not tid:
                continue
            if kind == 'folder':
                folder_targets.append((tid, post_title))
            else:
                file_ids.append(tid)
        multi = len(file_ids) > 1
        for i, fid in enumerate(file_ids, 1):
            if fid in seen_ids:
                continue
            seen_ids.add(fid)
            name = f"{post_title} [{i}]" if multi else post_title
            drive_videos.append({'id': fid, 'title': name, 'name': name})
            had_media = True

        # --- Dropbox links ---
        dbx_links = extract_dropbox_links_from_post(post)
        for url in dbx_links:
            key = normalize_dropbox_url(url).split('?')[0]
            if key in seen_dropbox:
                continue
            seen_dropbox.add(key)
            fname = dropbox_filename(url, post_title or 'video')
            dropbox_items.append({'url': url, 'filename': fname, 'title': fname})
            had_media = True

        # --- Native Patreon (Vimeo/Mux) streams ---
        for stream in extract_streams_from_post(post, verbose):
            hls_videos.append(stream)
            had_media = True

        if had_media:
            posts_with_media += 1

    # Expand any Drive folders linked from posts (recursive, like folder mode).
    for folder_id, post_title in folder_targets:
        if verbose:
            print(f"[INFO] Expanding Drive folder linked from post '{post_title}' ...")
        for e in list_folder_videos(folder_id, session, recursive, verbose):
            if e['id'] in seen_ids:
                continue
            seen_ids.add(e['id'])
            drive_videos.append(e)

    total = len(drive_videos) + len(dropbox_items) + len(hls_videos)
    if total == 0:
        print("[ERROR] Found no downloadable videos (no Drive/Dropbox links or native videos).")
        return

    print(f"[INFO] Found {total} item(s) across {posts_with_media} post(s): "
          f"{len(drive_videos)} Drive, {len(dropbox_items)} Dropbox, {len(hls_videos)} native.")

    if list_only:
        n = 0
        for v in drive_videos:
            n += 1; print(f"   {n:>3}) [Drive]   {v['title']}")
        for d in dropbox_items:
            n += 1; print(f"   {n:>3}) [Dropbox] {d['title']}")
        for h in hls_videos:
            tag = 'Mux' if h.get('source') == 'mux' else f"Vimeo {h.get('vimeo_id')}"
            n += 1; print(f"   {n:>3}) [{tag}] {h['title']}")
        return

    # Combined interactive selection across all three kinds.
    if select:
        interactive = bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
            bool(getattr(sys.stdout, 'isatty', lambda: False)())
        if interactive:
            combined = ([{'title': f"[Drive]   {v['title']}", '_k': 'drive', '_p': v} for v in drive_videos]
                        + [{'title': f"[Dropbox] {d['title']}", '_k': 'dbx', '_p': d} for d in dropbox_items]
                        + [{'title': f"[Native]  {h['title']}", '_k': 'hls', '_p': h} for h in hls_videos])
            chosen = _prompt_file_selection(combined)
            if not chosen:
                print("[INFO] Nothing selected; exiting.")
                return
            drive_videos = [c['_p'] for c in chosen if c['_k'] == 'drive']
            dropbox_items = [c['_p'] for c in chosen if c['_k'] == 'dbx']
            hls_videos = [c['_p'] for c in chosen if c['_k'] == 'hls']
            print(f"[INFO] Selected {len(chosen)} item(s).")
        else:
            print("[WARN] --select needs an interactive terminal; downloading all.")

    # Everything is written into out_dir if given (chdir keeps every sub-downloader consistent).
    old_cwd = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        old_cwd = os.getcwd()
        os.chdir(out_dir)
    try:
        if drive_videos:
            print(f"[INFO] Downloading {len(drive_videos)} Google Drive video(s) ...")
            download_folder_pooled(drive_videos, session, chunk_size, verbose)
        if dropbox_items:
            dbx_videos = [{'id': normalize_dropbox_url(d['url']), 'title': d['filename'],
                           'name': d['filename'], 'direct_url': normalize_dropbox_url(d['url'])}
                          for d in dropbox_items]
            print(f"[INFO] Downloading {len(dbx_videos)} Dropbox file(s) ...")
            download_folder_pooled(dbx_videos, session, chunk_size, verbose, label="Dropbox",
                                   conn_cap=DROPBOX_DEFAULT_CONNECTIONS)
        if hls_videos:
            if not ensure_ffmpeg(verbose):
                print("[ERROR] Native (Vimeo/Mux) videos need ffmpeg to mux audio+video, and it "
                      "was not available. Skipping the native videos.")
            else:
                mc = max_connections or DEFAULT_MAX_CONNECTIONS
                download_hls_pooled(hls_videos, session, None, mc, max_height, verbose)
    finally:
        if old_cwd:
            os.chdir(old_cwd)


def process_patreon_collection(collection_id: str, session: requests.Session, chunk_size: int,
                               num_threads: int, folder_workers: int, recursive: bool,
                               verbose: bool, select: bool = False, out_dir: str = None,
                               max_connections: int = None, max_height: int = DEFAULT_MAX_HEIGHT,
                               list_only: bool = False) -> None:
    """Walk a Patreon collection and download everything it offers:
      * Google Drive links (files named after the post; linked Drive folders expanded),
      * Dropbox links (downloaded directly),
      * Patreon-native videos (Vimeo/Mux HLS, muxed to MP4 with ffmpeg).
    Honors --select (a single combined chooser) and --list."""
    print(f"[INFO] Reading Patreon collection {collection_id} ...")
    title, campaign_id = get_collection_info(collection_id, session, verbose)
    posts = list_collection_posts(collection_id, campaign_id, session, verbose)
    if not posts:
        print("[ERROR] No posts found in the collection.")
        print("        It may be private/paid (needs your Patreon cookies), empty, or Patreon")
        print("        changed its API. Provide cookies with --cookies or a *.json export nearby.")
        return

    print(f"[INFO] Collection '{title or collection_id}': scanning {len(posts)} post(s) "
          f"for Drive / Dropbox links and native videos ...")

    _download_from_posts(posts, session, chunk_size, num_threads, folder_workers, recursive,
                         verbose, select, out_dir, max_connections, max_height, list_only)


def fetch_patreon_post(post_id: str, session: requests.Session, verbose: bool):
    """Fetch a single Patreon post object (with its linked 'included' resources merged in so
    link/stream extraction can see them). Returns the post dict or None."""
    params = {
        'include': 'collections,drop,primary_image,audio,video,embed',
        'fields[post]': ('post_type,title,content,teaser_text,url,embed,post_file,'
                         'post_metadata,current_user_can_view,published_at'),
        'json-api-version': '1.0',
        'json-api-use-default-includes': 'false',
    }
    url = f"https://www.patreon.com/api/posts/{post_id}?" + urlencode(params)
    try:
        r = session.get(url, headers={'Referer': PATREON_REFERER, 'Accept': 'application/json'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        d = r.json()
    except Exception:
        print(f"[ERROR] Patreon API did not return JSON for post {post_id} "
              f"(status may be non-200; your cookies may be missing or expired).")
        return None
    post = d.get('data')
    if not isinstance(post, dict):
        return None
    inc = d.get('included') or []
    if inc:
        post = dict(post)
        post['_included'] = inc   # searched by _walk_strings for extra Drive/Dropbox links
    return post


def process_patreon_post(post_id: str, session: requests.Session, chunk_size: int,
                         num_threads: int, folder_workers: int, recursive: bool,
                         verbose: bool, select: bool = False, out_dir: str = None,
                         max_connections: int = None, max_height: int = DEFAULT_MAX_HEIGHT,
                         list_only: bool = False) -> None:
    """Download everything a SINGLE Patreon post offers (Drive/Dropbox links + native videos),
    using the same engine as the collection flow."""
    print(f"[INFO] Reading Patreon post {post_id} ...")
    post = fetch_patreon_post(post_id, session, verbose)
    if not post:
        print("[ERROR] Could not load that post. It may be private/paid (needs your Patreon")
        print("        cookies), deleted, or Patreon changed its API. Provide --cookies or a")
        print("        *.json cookie export nearby.")
        return
    a = post.get('attributes', {}) or {}
    title = (a.get('title') or post_id).strip()
    print(f"[INFO] Post '{title}': scanning for Drive / Dropbox links and native videos ...")
    _download_from_posts([post], session, chunk_size, num_threads, folder_workers, recursive,
                         verbose, select, out_dir, max_connections, max_height, list_only)


def list_folder_entries(folder_id: str, session: requests.Session, verbose: bool) -> list[dict]:
    """List the direct children of a Drive folder via the embeddedfolderview endpoint.

    Returns a list of dicts: {'id', 'title', 'mime'}. 'mime' may be '' if it could not
    be detected. This is the one spot that depends on Google's HTML; if Google ever
    changes the markup, only this function needs updating.
    """
    url = f'https://drive.google.com/embeddedfolderview?id={folder_id}#list'
    resp = session.get(url, headers={'Referer': 'https://drive.google.com/'},
                       timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    if resp.status_code != 200:
        print(f"[WARN] Folder listing returned status {resp.status_code} for {folder_id}")
        return []

    html = resp.text
    entries = []
    seen = set()
    # Each child is rendered as a div with id="entry-<FILE_ID>".
    for m in re.finditer(r'id="entry-([a-zA-Z0-9_-]+)"', html):
        fid = m.group(1)
        if fid in seen:
            continue
        seen.add(fid)
        # Look at the chunk of HTML following this entry for its title and icon mime.
        chunk = html[m.end():m.end() + 1500]
        title_m = re.search(r'flip-entry-title[^>]*>([^<]+)<', chunk)
        title = title_m.group(1).strip() if title_m else fid
        # drive-thirdparty icon URLs encode the mimeType, e.g. .../type/video/mp4
        mime_m = re.search(r'/type/([a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+)', chunk)
        mime = mime_m.group(1) if mime_m else ''
        entries.append({'id': fid, 'title': title, 'mime': mime})

    if verbose:
        print(f"[INFO] Folder {folder_id}: parsed {len(entries)} entries")
    return entries


def list_folder_videos(folder_id: str, session: requests.Session, recursive: bool, verbose: bool,
                       _depth: int = 0, _seen_ids: set = None) -> list[dict]:
    """Recursively collect candidate video files in a folder.

    A child is treated as a subfolder when its mime is the Drive folder type, as a video
    when its mime starts with 'video/', and otherwise when its mime is unknown (so it can
    still be probed later). Clearly non-video files (images, docs, audio) are skipped.
    The same file id is never returned twice, even if it appears in multiple subfolders."""
    if _seen_ids is None:
        _seen_ids = set()
    videos = []
    for e in list_folder_entries(folder_id, session, verbose):
        if e['id'] in _seen_ids:
            continue
        _seen_ids.add(e['id'])
        mime = e['mime']
        if mime == 'application/vnd.google-apps.folder':
            if recursive:
                if verbose:
                    print(f"[INFO] {'  ' * _depth}Entering subfolder: {e['title']}")
                videos.extend(list_folder_videos(e['id'], session, recursive, verbose, _depth + 1, _seen_ids))
            continue
        if mime.startswith('video/') or mime == '':
            videos.append(e)
        elif verbose:
            print(f"[INFO] Skipping non-video ({mime}): {e['title']}")
    return videos


def get_video_url(page_content: str, verbose: bool) -> tuple[str, str]:
    """Extracts the video playback URL and title from the page content."""
    if verbose:
        tqdm.write("[INFO] Parsing video playback URL and title.")
    contentList = page_content.split("&")
    video, title = None, None
    for content in contentList:
        if content.startswith('title=') and not title:
            # Drive returns the title form-encoded (spaces as '+'), so unquote_plus.
            title = unquote_plus(content.split('=')[-1])
        elif "videoplayback" in content and not video:
            video = unquote(content).split("|")[-1]
        if video and title:
            break

    if verbose:
        tqdm.write(f"[INFO] Video URL: {video}")
        tqdm.write(f"[INFO] Video Title: {title}")

    return video, title

def get_file_size(url: str, session: requests.Session, attempts: int = 3) -> int:
    """Get the total file size. Tries HEAD first, then a ranged GET (Google often
    ignores HEAD content-length). Retries a few times, because during a parallel resolve
    of many large files Google occasionally throttles a probe and returns no size — which
    previously left that file with an unknown size and a broken, single-connection download."""
    headers = {'Referer': 'https://drive.google.com/'}
    for attempt in range(max(1, attempts)):
        try:
            with connection_slot():
                response = session.head(url, allow_redirects=True, headers=headers,
                                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
            size = int(response.headers.get('content-length', 0) or 0)
            if size > 0:
                return size

            # Fallback: ask for one byte and read the total from the Content-Range header.
            with connection_slot():
                with session.get(url, stream=True, allow_redirects=True,
                                 headers={**headers, 'Range': 'bytes=0-0'},
                                 timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)) as r:
                    content_range = r.headers.get('content-range', '')
                    if '/' in content_range:
                        total = content_range.rsplit('/', 1)[-1].strip()
                        if total.isdigit() and int(total) > 0:
                            return int(total)
                    cl = r.headers.get('content-length', '')
                    if r.status_code == 200 and cl.isdigit() and int(cl) > 0:
                        return int(cl)
        except requests.RequestException:
            pass
        if attempt < attempts - 1:
            time.sleep(0.5 * (attempt + 1))
    return 0

def download_part(url: str, session: requests.Session, thread_lock, start: int, end: int, part_num: int, part_filename: str, chunk_size: int, pbar: tqdm, gpbar: tqdm, verbose: bool) -> None:
    """Downloads a specific byte range of the file and writes it to a part file."""
    headers = {
        'Range': f'bytes={start}-{end}',
        'Referer': 'https://drive.google.com/',
    }

    # Support resuming individual parts
    resuming = False
    downloaded = 0
    if os.path.exists(part_filename):
        downloaded = os.path.getsize(part_filename)
        if downloaded > 0:
            resuming = True
            headers['Range'] = f'bytes={start + downloaded}-{end}'

            # Update Progress
            with thread_lock:
                gpbar.update(downloaded)
                if pbar is not None:
                    pbar.update(downloaded)
            
            if verbose:
                print(f"[INFO] Resuming part {part_filename} from byte {start + downloaded}")

    # Check Part already fully downloaded
    if downloaded >= (end - start + 1):
        return

    # Hold a global connection slot for the whole transfer so we never open more
    # than --max-connections sockets at once, and use the response as a context
    # manager so the connection is released even when we break out early.
    with connection_slot():
        with session.get(url, stream=True, headers=headers,
                         timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as response:
            if response.status_code not in (200, 206):
                raise Exception(f"[ERROR] Failed to download part {part_filename}, status: {response.status_code}")

            # If we asked to resume (sent a Range) but the server replied 200, it ignored the
            # Range and is streaming the whole part from the start. Appending would corrupt
            # the file, so restart this part from scratch and roll the progress bars back.
            if resuming and response.status_code == 200:
                if verbose:
                    print(f"[WARN] Server ignored Range for {part_filename}; restarting part from scratch.")
                with thread_lock:
                    gpbar.update(-downloaded)
                    if pbar is not None:
                        pbar.update(-downloaded)
                downloaded = 0
                file_mode = 'wb'
            else:
                file_mode = 'ab' if downloaded > 0 else 'wb'

            with open(part_filename, file_mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    f.write(chunk)
                    with thread_lock:
                        gpbar.update(len(chunk))
                        if pbar is not None:
                            pbar.update(len(chunk))
                    downloaded += len(chunk)

                    # Check Part fully downloaded
                    if downloaded >= (end - start + 1):
                        break

def download_part_wrapper(errors, args):
    try:
        download_part(*args)
    except Exception as e:
        print(e)
        errors.append(e)

def merge_parts(part_files: list[str], output_filename: str, verbose: bool) -> None:
    """Merges all part files into the final output file."""
    # Use tqdm.write for all messages: during pooled downloads there are many live progress
    # bars, and a plain print() would scribble over them and corrupt the display.
    if verbose:
        tqdm.write(f"[INFO] Merging {len(part_files)} parts into {os.path.basename(output_filename)}")

    missing = [pf for pf in part_files if not os.path.exists(pf)]
    if missing:
        tqdm.write(f"[ERROR] Missing parts: {missing}")
        return

    tmp_output = _temp_artifact(output_filename, ".merging")
    with open(tmp_output, 'wb') as outfile:
        for part_file in part_files:
            with open(part_file, 'rb') as pf:
                shutil.copyfileobj(pf, outfile, length=8 * 1024 * 1024)
        outfile.flush()
        os.fsync(outfile.fileno())

    # Atomic swap: the final name only appears once the file is fully written.
    os.replace(tmp_output, output_filename)

    for part_file in part_files: # Cleanup
        os.remove(part_file)

def download_file(url: str, session: requests.Session, filename: str, chunk_size: int, num_threads: int, verbose: bool, position: int = 0, show_part_bars: bool = True) -> bool:
    """Downloads the file using multiple threads, each handling a byte-range segment.

    Returns True on success. When show_part_bars is False, only a single aggregate
    progress bar (at `position`) is shown — used for parallel folder downloads to keep
    the terminal readable.
    """

    errors = []
    num_threads = max(1, num_threads)

    total_size = get_file_size(url, session)
    if num_threads == 1:
        return download_single_threaded(url, session, filename, chunk_size, verbose, position)
    if total_size == 0:
        print("[WARN] Could not determine file size. Falling back to single-threaded download.")
        return download_single_threaded(url, session, filename, chunk_size, verbose, position)

    if verbose:
        print(f"[INFO] Total file size: {total_size} bytes")
        print(f"[INFO] Downloading with {num_threads} threads")

    part_size = math.ceil(total_size / num_threads)
    part_files = []
    threads = []

    gp_desc = os.path.basename(filename) if not show_part_bars else "Download Progress"
    silent = (position is None and not show_part_bars)
    gpBar = make_bar(
        unit='B', unit_scale=True,
        desc=gp_desc,
        total=total_size,
        position=position if position is not None else 0,
        leave=show_part_bars,
        disable=silent,
    )

    if show_part_bars:
        pbars = [
            make_bar(
                unit='B', unit_scale=True,
                desc="Downloading Part " + str(i+1),
                total=min((i * part_size) + part_size - 1, total_size - 1) - (i * part_size) + 1,
                position=i+1
            )
            for i in range(num_threads)
        ]
    else:
        pbars = [None] * num_threads

    thread_lock = threading.Lock()
    worker_sessions = []

    for i in range(num_threads):
        start = i * part_size
        end = min(start + part_size - 1, total_size - 1)
        part_filename = _temp_artifact(filename, f".part{i}")
        part_files.append(part_filename)

        worker_session = new_session_from(session)
        worker_sessions.append(worker_session)

        t = threading.Thread(
            target=download_part_wrapper,
            args=(errors, (url, worker_session, thread_lock, start, end, i, part_filename, chunk_size, pbars[i], gpBar, verbose)),
            daemon=True
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Close every worker session so its sockets are released immediately instead of
    # lingering until garbage collection (the cause of resource exhaustion on big folders).
    for ws in worker_sessions:
        ws.close()

    gpBar.close()
    for pbar in pbars:
        if pbar is not None:
            pbar.close()

    if len(errors) > 0:
        print(f"[ERROR] One of the parts failed for {filename}. Check the console for details.")
        return False

    # Verify all parts downloaded correctly
    downloaded_total = sum(os.path.getsize(pf) for pf in part_files if os.path.exists(pf))
    if downloaded_total < total_size:
        print(f"[ERROR] Download incomplete for {filename}: got {downloaded_total}/{total_size} bytes.")
        return False

    merge_parts(part_files, filename, verbose)
    _record_download(filename)
    if show_part_bars:
        print(f"\n{filename} downloaded successfully.")
    return True

def download_single_threaded(url: str, session: requests.Session, filename: str, chunk_size: int, verbose: bool, position: int = 0) -> bool:
    """Fallback single-threaded download (original behavior)."""
    headers = {
        'Referer': 'https://drive.google.com/',
    }
    file_mode = 'wb'
    downloaded_size = 0

    if os.path.exists(filename):
        downloaded_size = os.path.getsize(filename)
        headers['Range'] = f"bytes={downloaded_size}-"
        file_mode = 'ab'

    if verbose:
        print(f"[INFO] Starting single-threaded download from {url}")

    with connection_slot():
        with session.get(url, stream=True, headers=headers,
                         timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as response:
            if response.status_code in (200, 206):  # 200 new, 206 partial
                total_size = int(response.headers.get('content-length', 0)) + downloaded_size
                with open(filename, file_mode) as file:
                    with make_bar(total=total_size, initial=downloaded_size, unit='B', unit_scale=True,
                                  desc=os.path.basename(filename),
                                  position=position if position is not None else 0,
                                  disable=(position is None), file=sys.stdout) as pbar:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if chunk:
                                file.write(chunk)
                                pbar.update(len(chunk))
                print(f"\n{filename} downloaded successfully.")
                return True
            else:
                print(f"[ERROR] Error downloading {filename}, status code: {response.status_code}")
                return False

def fetch_video(video_id: str, session: requests.Session, verbose: bool) -> tuple[str, str]:
    """Resolve a Drive file id to its (playback_url, title). Returns (None, None) on failure."""
    drive_url = f'https://drive.google.com/u/0/get_video_info?docid={video_id}&drive_originator_app=303'
    if verbose:
        tqdm.write(f"[INFO] Accessing {drive_url}")

    response = session.get(drive_url, allow_redirects=True,
                           timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    page_content = response.text

    if verbose:
        tqdm.write(f"[INFO] get_video_info status: {response.status_code}")
        tqdm.write(f"[INFO] response length: {len(page_content)} chars")

    return get_video_url(page_content, verbose)


def safe_filename(name: str, fallback_id: str) -> str:
    """Sanitise a filename for Windows + Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', name)
    name = re.sub(r'[. ]+$', '', name)
    return name or f"{fallback_id}.mp4"


def prefer_mp4_ext(name: str) -> str:
    """Rewrite a trailing .m4v to .mp4. The two are the same MPEG-4 container (Drive often
    serves video files named .m4v), so this is a lossless rename, not a re-encode. Other
    real containers (.mkv, .webm, .mov, ...) are left untouched."""
    root, ext = os.path.splitext(name)
    if ext.lower() == '.m4v':
        return root + '.mp4'
    return name


def process_single_video(video_id: str, session: requests.Session, output_file: str, chunk_size: int,
                         num_threads: int, verbose: bool, position: int = 0, show_part_bars: bool = True,
                         attempts: int = 2) -> bool:
    """Resolve, lock, and download a single video. Returns True on success.

    On failure (e.g. a 403 from an expired playback link) it re-fetches a fresh link and
    retries up to `attempts` times. Partial parts are kept, so retries resume."""
    video, title = fetch_video(video_id, session, verbose)
    if not video:
        print(f"[WARN] Could not get a playback URL for {video_id} (not a video, private, or unavailable). Skipping.")
        return False

    # Respect an explicit -o name as-is; only normalize when the name comes from Drive.
    chosen_name = output_file or prefer_mp4_ext(title or f"{video_id}.mp4")
    valid_filename = safe_filename(chosen_name, video_id)

    lock_path = _temp_artifact(valid_filename, ".lock")
    if not acquire_lock(lock_path):
        return False
    try:
        for attempt in range(max(1, attempts)):
            if attempt > 0:
                fresh, _ = fetch_video(video_id, session, verbose)
                if fresh:
                    video = fresh
                tqdm.write(f"[WARN] Retry {attempt}/{attempts - 1} with a fresh link: {title or video_id}")
            if download_file(video, session, valid_filename, chunk_size, num_threads, verbose,
                             position=position, show_part_bars=show_part_bars):
                return True
        return False
    finally:
        release_lock(lock_path)


def _parse_index_selection(raw: str, n: int):
    """Parse a selection like '1,3,5-8' into a list of 1-based indices.
    Returns None on invalid input; [] for cancel; full range for all/empty."""
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


def _prompt_file_selection(videos: list) -> list:
    """Show a numbered list and let the user choose which videos to download."""
    print(f"[INFO] {len(videos)} file(s) found:")
    for i, v in enumerate(videos, 1):
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {v['title']}")
    print("Select which to download: e.g. '1,3,5-8', 'a' or Enter for ALL, 'q' to cancel.")

    for _ in range(3):
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return []
        sel = _parse_index_selection(raw, len(videos))
        if sel is None:
            print(f"[WARN] Invalid input. Use numbers/ranges 1-{len(videos)}, 'a', or 'q'.")
            continue
        return [videos[i - 1] for i in sel]

    print("[WARN] No valid selection; cancelling.")
    return []


class _FileJob:
    """One video being downloaded as a set of byte-range segments."""
    def __init__(self, entry):
        self.entry = entry
        self.id = entry['id']
        self.title = entry['title']
        # Direct-download jobs (e.g. Dropbox) carry a ready URL and skip Drive resolution.
        self.direct_url = entry.get('direct_url') if isinstance(entry, dict) else None
        self.url = None
        self.size = 0
        self.filename = None
        self.lock_path = None
        self.segments = []          # list of dicts: {start, end, path}
        self.remaining = 0
        self.failed = False
        self.locked = False
        self.url_lock = threading.Lock()
        self.bar = None


# Thread-local sessions so each pool worker reuses one connection set.
_seg_tls = threading.local()
_seg_sessions = []
_seg_sessions_lock = threading.Lock()


def _segment_session(base: requests.Session) -> requests.Session:
    s = getattr(_seg_tls, 'session', None)
    if s is None:
        s = new_session_from(base)
        _seg_tls.session = s
        with _seg_sessions_lock:
            _seg_sessions.append(s)
    return s


def _refresh_job_url(job: '_FileJob', session: requests.Session, verbose: bool) -> None:
    """Re-fetch a fresh playback URL for a job (used when a segment hits 403/expiry)."""
    with job.url_lock:
        if job.direct_url:
            job.url = job.direct_url  # direct (Dropbox) URLs don't expire; nothing to refresh
            return
        fresh, _ = fetch_video(job.id, session, verbose)
        if fresh:
            job.url = fresh


def _download_segment(job: '_FileJob', seg: dict, session: requests.Session, chunk_size: int,
                      on_bytes, verbose: bool) -> bool:
    """Download one segment to its part file. Resumes; refreshes URL on 403/expiry."""
    start, end, path = seg['start'], seg['end'], seg['path']
    seg_len = (end - start + 1) if end is not None else None

    downloaded = os.path.getsize(path) if os.path.exists(path) else 0
    if seg_len is not None and downloaded >= seg_len:
        return True

    attempts = 6
    retryable = (429, 500, 502, 503, 504)
    for attempt in range(attempts):
        lo = start + downloaded
        headers = {'Referer': 'https://drive.google.com/'}
        if end is not None:
            headers['Range'] = f'bytes={lo}-{end}'
        elif downloaded:
            headers['Range'] = f'bytes={lo}-'

        try:
            with session.get(job.url, stream=True, headers=headers,
                             timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
                if r.status_code in (403, 410) and attempt < attempts - 1:
                    _refresh_job_url(job, session, verbose)
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                if r.status_code in retryable and attempt < attempts - 1:
                    # Rate-limited or a transient server error. Dropbox returns HTTP 429 when
                    # its shared links get too much traffic / too many connections. Back off
                    # (honouring Retry-After) and try again.
                    ra = (r.headers.get('Retry-After') or '').strip()
                    delay = float(ra) if ra.isdigit() else min(15.0, 1.0 * (2 ** attempt))
                    time.sleep(delay)
                    continue
                # For a ranged (multi-segment) request the server MUST answer 206. A 200 means
                # it ignored our Range and is sending something else (often a rate-limit/error
                # page or the whole file from byte 0). Writing that into this part would corrupt
                # the output, so back off and retry instead of trusting it.
                if end is not None and r.status_code == 200:
                    if attempt < attempts - 1:
                        time.sleep(min(15.0, 1.0 * (2 ** attempt)))
                        continue
                    return False
                if r.status_code not in (200, 206):
                    return False
                # Whole-file part (unknown size) whose resume was ignored (200 to a Range): the
                # server restarted from 0, so discard what we had and rewrite from scratch.
                if end is None and downloaded > 0 and r.status_code == 200:
                    on_bytes(-downloaded)
                    downloaded = 0
                    mode = 'wb'
                else:
                    mode = 'ab' if downloaded > 0 else 'wb'
                with open(path, mode) as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        on_bytes(len(chunk))
                        if seg_len is not None and downloaded >= seg_len:
                            break
                # Short read: the server ended the body before the full segment arrived (a
                # truncated/limited response). Don't call this a success — back off and retry
                # (resuming from what we have); give up honestly if it keeps happening.
                if seg_len is not None and downloaded < seg_len:
                    if attempt < attempts - 1:
                        time.sleep(min(15.0, 1.0 * (2 ** attempt)))
                        continue
                    return False
            return True
        except requests.RequestException:
            if attempt < attempts - 1:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                _refresh_job_url(job, session, verbose)
                continue
            return False
    return False


def download_folder_pooled(videos: list, session: requests.Session, chunk_size: int, verbose: bool,
                           label: str = "Folder", conn_cap: int = None) -> None:
    """Download all videos via a shared pool of `max_connections` workers pulling segments
    from any file. As files finish, workers automatically move to the remaining files, so
    the connection budget is always fully used and the last files speed up.

    Handles both Drive entries (resolved via fetch_video) and direct-URL entries such as
    Dropbox (entry has 'direct_url'), so every source shares the exact same engine:
    Resolving bar, per-file bars, segment work-stealing, threads/workers/connections, resume.
    `conn_cap` optionally lowers the connection budget for gentler sources (e.g. Dropbox)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    budget = _max_connections
    # A source-specific default (e.g. Dropbox = 8) applies only when the user didn't pass an
    # explicit -m. An explicit -m always wins, up or down.
    if conn_cap and not _max_conn_explicit and budget > conn_cap:
        print(f"[INFO] {label}: using {conn_cap} connections by default (its links rate-limit "
              f"heavily; a higher value makes them fail). Override with -m if you want.")
        budget = conn_cap
    seg_size = max(1, SEGMENT_MIB) * 1024 * 1024

    # 1) Resolve URL + size for every file, in parallel.
    print(f"[INFO] Resolving {len(videos)} file(s)...")
    jobs = [_FileJob(v) for v in videos]

    resolve_bar = make_bar(total=len(jobs), desc='Resolving', unit='file',
                           unit_scale=False, leave=False)
    resolve_lock = threading.Lock()

    def resolve(job):
        # Each file is resolved independently; a network error or timeout on one must not
        # abort the whole batch, so failures are caught and the file is simply skipped.
        try:
            if job.direct_url:
                # Direct download (e.g. Dropbox): probe size AND the real filename in one go.
                job.url = job.direct_url
                size, real = _dropbox_probe(job.direct_url, session)
                job.size = size if size > 0 else get_file_size(job.direct_url, session)
                preferred = (job.entry.get('name') or job.entry.get('title') or job.id)
                # Old /s/<id> share links carry no filename in the URL, so the caller's name is
                # just the Dropbox id. Replace it with the real name from Content-Disposition.
                if real and (_looks_like_dropbox_id(preferred) or not os.path.splitext(preferred)[1]):
                    preferred = real
                if not os.path.splitext(preferred)[1]:
                    preferred += '.mp4'
                job.filename = safe_filename(prefer_mp4_ext(preferred), job.id)
            else:
                url, title = fetch_video(job.id, session, verbose)
                if not url:
                    job.failed = True
                else:
                    job.url = url
                    job.size = get_file_size(url, session)
                    if job.size == 0:
                        # A throttled/expired playback URL yields no size, which would force a
                        # slow single-connection whole-file download. Get a fresh URL and retry.
                        fresh, fresh_title = fetch_video(job.id, session, verbose)
                        if fresh:
                            job.url = fresh
                            job.size = get_file_size(fresh, session)
                            title = title or fresh_title
                    # A caller-supplied 'name' (e.g. a Patreon post title) wins over the often-
                    # generic Drive title. If it has no extension, borrow the Drive title's (or .mp4).
                    preferred = job.entry.get('name') if isinstance(job.entry, dict) else None
                    if preferred:
                        if not os.path.splitext(preferred)[1]:
                            preferred += (os.path.splitext(title or '')[1] or '.mp4')
                        job.filename = safe_filename(prefer_mp4_ext(preferred), job.id)
                    else:
                        job.filename = safe_filename(prefer_mp4_ext(title or f"{job.id}.mp4"), job.id)
        except Exception as exc:
            job.failed = True
            if verbose:
                tqdm.write(f"[WARN] Could not resolve {job.title}: {exc}")
        finally:
            with resolve_lock:
                resolve_bar.update(1)

    with ThreadPoolExecutor(max_workers=min(budget, 16)) as ex:
        list(ex.map(resolve, jobs))
    resolve_bar.close()

    ready = []
    for job in jobs:
        if job.failed or not job.filename:
            tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Skipping (no playback URL): {job.title}")
            continue
        # Already finished in a previous run? The final name only appears after a complete,
        # atomic merge, so its presence means the file is done -> skip (resume-friendly).
        if os.path.exists(job.filename) and os.path.getsize(job.filename) > 0:
            tqdm.write(f"[INFO] Already have {os.path.basename(job.filename)}, skipping.")
            continue
        job.lock_path = _temp_artifact(job.filename, ".lock")
        if not acquire_lock(job.lock_path):
            tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Already downloading elsewhere: {job.title}")
            continue
        job.locked = True
        # Build segments.
        if job.size > 0:
            n = max(1, math.ceil(job.size / seg_size))
            for i in range(n):
                s = i * seg_size
                e = min(s + seg_size - 1, job.size - 1)
                job.segments.append({'start': s, 'end': e,
                                     'path': _temp_artifact(job.filename, f".part{i}")})
        else:
            job.segments.append({'start': 0, 'end': None,
                                 'path': _temp_artifact(job.filename, ".part0")})
        job.remaining = len(job.segments)
        ready.append(job)

    if not ready:
        print("[ERROR] Nothing to download.")
        return

    total_bytes = sum(j.size for j in ready)
    # Per-file bars stay in place (leave=True) and show their own done/FAILED status — no
    # writing between live bars (that caused gaps/jumping on Windows). Above the limit, one
    # overall bar with OK/FAIL lines is used instead.
    per_file_bars = len(ready) <= PER_FILE_BAR_LIMIT

    print(f"[INFO] Downloading {len(ready)} file(s) with a shared pool of {budget} connections "
          f"({SEGMENT_MIB} MiB segments). Freed connections flow to the remaining files.\n")

    bar_lock = threading.Lock()
    overall = None
    if per_file_bars:
        for idx, job in enumerate(ready):
            initial = sum(os.path.getsize(s['path']) for s in job.segments if os.path.exists(s['path']))
            job.bar = make_bar(total=max(job.size, 1), initial=min(initial, job.size or initial),
                               unit='B', unit_scale=True, desc=os.path.basename(job.filename),
                               position=idx, leave=True)
    else:
        done_bytes = sum(os.path.getsize(s['path']) for j in ready for s in j.segments if os.path.exists(s['path']))
        overall = make_bar(total=max(total_bytes, 1), initial=min(done_bytes, total_bytes or done_bytes),
                           unit='B', unit_scale=True, desc=f'{len(ready)} files', position=0)

    result = {'ok': 0, 'fail': 0}
    result_lock = threading.Lock()
    failures = []  # (title, reason)

    def on_bytes(job):
        def _cb(n):
            with bar_lock:
                if job.bar is not None:
                    job.bar.update(n)
                if overall is not None:
                    overall.update(n)
        return _cb

    def finalize(job):
        ok, reason = False, None
        if job.failed:
            reason = "download failed"
        else:
            got = sum(os.path.getsize(s['path']) for s in job.segments if os.path.exists(s['path']))
            if job.size > 0:
                if got >= job.size:
                    merge_parts([s['path'] for s in job.segments], job.filename, verbose)
                    ok = True
                else:
                    reason = f"incomplete {got}/{job.size} bytes"
            else:
                # Size unknown (Google wouldn't report it): accept only if we actually got data.
                if got > 0:
                    merge_parts([s['path'] for s in job.segments], job.filename, verbose)
                    ok = True
                else:
                    reason = "no data received (size unknown)"
        if job.locked:
            release_lock(job.lock_path)
        if ok:
            _record_download(job.filename)
        with result_lock:
            result['ok' if ok else 'fail'] += 1
            n = result['ok'] + result['fail']
            if not ok:
                failures.append((job.title, reason))
        if per_file_bars and job.bar is not None:
            with bar_lock:
                job.bar.colour = 'green' if ok else 'red'
                job.bar.set_postfix_str('done' if ok else 'FAILED', refresh=False)
                job.bar.refresh()
        elif overall is not None:
            tag = f"{CLR.GREEN}OK  {CLR.RESET}" if ok else f"{CLR.RED}FAIL{CLR.RESET}"
            tqdm.write(f"[{n}/{len(ready)}] {tag} {job.title}")

    def run_segment(job, seg):
        # Even when the job has already failed we must still decrement `remaining` (just
        # skip the work), otherwise the counter never reaches 0 and finalize() — which
        # tallies the result and releases the lock — is never called for that file.
        if not job.failed:
            sess = _segment_session(session)
            if not _download_segment(job, seg, sess, chunk_size, on_bytes(job), verbose):
                job.failed = True
        with job.url_lock:
            job.remaining -= 1
            last = (job.remaining == 0)
        if last:
            finalize(job)

    # Interleave segments round-robin so every file starts progressing immediately.
    max_segs = max(len(j.segments) for j in ready)
    order = []
    for k in range(max_segs):
        for job in ready:
            if k < len(job.segments):
                order.append((job, job.segments[k]))

    with ThreadPoolExecutor(max_workers=budget) as ex:
        futures = [ex.submit(run_segment, job, seg) for job, seg in order]
        for _ in as_completed(futures):
            pass

    if per_file_bars:
        with bar_lock:
            for job in ready:
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
    print(f"\n{color}[INFO] {label} done: {result['ok']} succeeded, {result['fail']} failed, "
          f"out of {len(ready)}.{CLR.RESET}")
    for title, reason in failures:
        print(f"[WARN] {title}: {reason or 'failed'} (parts kept for resume).")
    if result['fail'] and label == "Dropbox":
        print(f"{CLR.YELLOW}[HINT]{CLR.RESET} Dropbox likely rate-limited its shared links "
              f"(too much traffic / too many connections). The partial parts were kept, so:")
        print("       - wait a while (Dropbox link limits reset over time) and re-run the SAME")
        print("         command — it resumes and skips finished files;")
        print("       - and/or use fewer connections, e.g. -m 6 or -m 4.")


def process_folder(folder_id: str, session: requests.Session, chunk_size: int, num_threads: int,
                   folder_workers: int, recursive: bool, verbose: bool, select: bool = False) -> None:
    """List a folder and download all videos in it.

    folder_workers == 0 means "all at once". Actual simultaneous network use is always
    bounded by the global connection limit (-m), so a huge folder_workers mainly costs
    threads, not sockets. When `select` is set, the user is asked which files to download.
    """
    print(f"[INFO] Listing folder {folder_id}" + (" (recursive)" if recursive else "") + " ...")
    videos = list_folder_videos(folder_id, session, recursive, verbose)
    if not videos:
        print("[ERROR] No videos found in the folder.")
        print("        The folder may be empty/private, may need a resource key, or Google may have")
        print("        changed the listing markup (see list_folder_entries() to adjust).")
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

    print(f"[INFO] Found {len(videos)} candidate file(s).")
    # folder_workers/num_threads are kept for compatibility but the shared segment pool
    # below uses the whole connection budget across all files at once.
    download_folder_pooled(videos, session, chunk_size, verbose)


def _auto_settings():
    """Pick threads-per-file and a connection budget from the CPU count. Downloads are
    I/O-bound, so connections scale above the logical CPU count; threads track it. Capped
    to stay reasonable on big machines."""
    logical = os.cpu_count() or 4
    threads = max(4, min(32, logical))
    conns = max(16, min(128, logical * 4))
    return threads, conns, logical


# =============================================================================
#  Patreon-native videos (Vimeo / Mux, HLS) — ported from the Patreon downloader.
#  These posts stream the video itself (separate audio+video HLS tracks) instead of
#  linking to Drive/Dropbox. We resolve them to an HLS master, download the segments
#  through a shared pool, and mux to MP4 with ffmpeg.
# =============================================================================
def extract_streams_from_post(post: dict, verbose: bool) -> list:
    """Return native video stream descriptors for ONE Patreon post. Each is either
    {'source':'vimeo','title','vimeo_id','vimeo_hash'} or {'source':'mux','title','master_url'}."""
    a = post.get('attributes', {}) or {}
    post_type = a.get('post_type')
    out = []

    # 1) Patreon-hosted ("embedded") video on Mux: a signed HLS master URL.
    pf = a.get('post_file') or {}
    pf_url = pf.get('url', '') or ''
    playback = ((a.get('post_metadata') or {}).get('playback_data') or {})
    mux_url = None
    if 'stream.mux.com' in pf_url:
        mux_url = pf_url
    elif playback.get('playback_id') and playback.get('playback_token'):
        mux_url = (f"https://stream.mux.com/{playback['playback_id']}.m3u8"
                   f"?token={playback['playback_token']}")
    if mux_url and post_type in (None, 'video_external_file', 'video_file', 'video'):
        title = (a.get('title') or str(post.get('id'))).strip()
        out.append({'source': 'mux', 'title': title, 'master_url': mux_url})
        return out

    # 2) Vimeo embed: extract id + privacy hash.
    emb = a.get('embed') or {}
    url_field = emb.get('url', '') or ''
    html = emb.get('html', '') or ''
    m = re.search(r'vimeo\.com/(\d+)/([0-9a-zA-Z]+)', url_field)
    if not m:
        m = re.search(r'player\.vimeo\.com/video/(\d+)\?h=([0-9a-zA-Z]+)', html)
    if m:
        title = (emb.get('subject') or a.get('title') or m.group(1)).strip()
        out.append({'source': 'vimeo', 'title': title,
                    'vimeo_id': m.group(1), 'vimeo_hash': m.group(2)})
        return out

    # 3) Fallback: some collections don't populate the structured embed/post_file fields, but
    #    the Mux master or Vimeo id+hash still appears somewhere in the post (embed html,
    #    included video/embed resources, content). Scan every string, unescaping slashes.
    title = (a.get('title') or str(post.get('id'))).strip()
    for s in _walk_strings(post):
        if not s or ('mux.com' not in s and 'vimeo.com' not in s):
            continue
        s2 = s.replace('\\/', '/')
        mm = re.search(r'https://stream\.mux\.com/[\w\-]+\.m3u8[^\s"\\<>]*', s2)
        if mm:
            out.append({'source': 'mux', 'title': title, 'master_url': mm.group(0)})
            return out
        vm = re.search(r'vimeo\.com/(?:video/)?(\d+)(?:/|\?h=)([0-9a-zA-Z]+)', s2)
        if vm:
            out.append({'source': 'vimeo', 'title': title,
                        'vimeo_id': vm.group(1), 'vimeo_hash': vm.group(2)})
            return out
    return out


# ---- ffmpeg: locate / download / verify ----------------------------------- #
def _resolve_ffmpeg(value):
    if not value:
        return None
    name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    if os.path.isdir(value):
        for cand in (os.path.join(value, name), os.path.join(value, 'bin', name)):
            if os.path.isfile(cand):
                return cand
        return None
    return value


def _try_ffmpeg(path):
    if not path:
        return False
    try:
        subprocess.run([path, '-version'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


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
    with requests.get(url, stream=True, headers={'User-Agent': USER_AGENT}, allow_redirects=True,
                      timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
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


# ---- Vimeo/Mux resolution + HLS playlist parsing -------------------------- #
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
    try:
        r = session.get(embed, headers={'Referer': PATREON_REFERER, 'User-Agent': USER_AGENT},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    except requests.RequestException as e:
        if verbose:
            print(f"[WARN] Vimeo player fetch failed for {vimeo_id}: {e}")
        return None, None, 0
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


def resolve_master(video, session, verbose):
    """Resolve a video descriptor to (hls_master_url, headers). Returns (None, {}) on failure."""
    if video.get('source') == 'mux':
        return video.get('master_url'), MUX_HEADERS
    master, _title, _dur = resolve_vimeo(video['vimeo_id'], video['vimeo_hash'], session, verbose)
    return master, VIMEO_HEADERS


def parse_master_playlist(master_url, session, max_height, headers, verbose):
    """Fetch the HLS master and return (video_url, audio_url_or_None) for the chosen quality."""
    try:
        r = session.get(master_url, headers=headers, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    except requests.RequestException as e:
        if verbose:
            print(f"[WARN] HLS master fetch failed: {e}")
        return None, None
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


def parse_media_playlist(url, session, headers):
    """Fetch an HLS media playlist and return (init_segment_url_or_None, [segment_urls])."""
    try:
        r = session.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    except requests.RequestException:
        return None, []
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


# ---- HLS segment pool + mux ------------------------------------------------ #
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
            with session.get(url, stream=True, headers=headers,
                             timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
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
    tmp = _temp_artifact(out_path, ".part.mp4")
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
        self.lock_path = _temp_artifact(out_path, ".lock")
        self.locked = False
        self.parts_dir = _temp_artifact(out_path, ".parts")
        self.streams = {}          # 'v'/'a' -> {'parts': [paths in order]}
        self.tasks = []            # (stream_key, idx, url, path)
        self.remaining = 0
        self.failed = False
        self.bar = None


def _build_hls_job(video, session, out_dir, max_height, verbose):
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


def download_hls_pooled(videos, session, out_dir, max_connections, max_height, verbose):
    """Download all native (Vimeo/Mux) videos as HLS via a shared pool of segment workers,
    then mux each with ffmpeg. All videos progress at once; the budget is shared."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    print(f"[INFO] Resolving {len(videos)} native video(s)...")
    # Each video is resolved independently with a visible progress bar; a network error or
    # timeout on one must not abort the whole batch (mirrors the Drive resolve phase).
    resolve_bar = make_bar(total=len(videos), desc='Resolving', unit='video',
                           unit_scale=False, leave=False)
    resolve_lock = threading.Lock()

    def _resolve(v):
        try:
            return _build_hls_job(v, session, out_dir, max_height, verbose)
        except Exception as exc:
            if verbose:
                tqdm.write(f"[WARN] Could not resolve {v.get('title')}: {exc}")
            return None
        finally:
            with resolve_lock:
                resolve_bar.update(1)

    jobs = []
    with ThreadPoolExecutor(max_workers=min(max_connections, 12)) as ex:
        for job in ex.map(_resolve, videos):
            if job is None:
                continue
            if not acquire_lock(job.lock_path):
                continue
            job.locked = True
            jobs.append(job)
    resolve_bar.close()

    if not jobs:
        print("[INFO] Nothing to download (native).")
        return

    per_file_bars = len(jobs) <= PER_FILE_BAR_LIMIT
    print(f"[INFO] Downloading {len(jobs)} native video(s) with a shared pool of {max_connections} "
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
                vfile = _temp_artifact(job.out_path, ".video")
                _concat_stream(job.streams['v']['parts'], vfile)
                afile = None
                if 'a' in job.streams:
                    afile = _temp_artifact(job.out_path, ".audio")
                    _concat_stream(job.streams['a']['parts'], afile)
                ok, reason = _ffmpeg_mux(vfile, afile, job.out_path, verbose)
            except Exception as exc:
                reason = str(exc)
            finally:
                for extra in (_temp_artifact(job.out_path, ".video"),
                              _temp_artifact(job.out_path, ".audio")):
                    if os.path.exists(extra):
                        try:
                            os.remove(extra)
                        except OSError:
                            pass
        if ok:
            shutil.rmtree(job.parts_dir, ignore_errors=True)
            _record_download(job.out_path)
        if job.locked:
            release_lock(job.lock_path)
        with result_lock:
            result['ok' if ok else 'fail'] += 1
            n = result['ok'] + result['fail']
            if not ok:
                failures.append((job.title, reason))
        if per_file_bars and job.bar is not None:
            with bar_lock:
                job.bar.colour = 'green' if ok else 'red'
                job.bar.set_postfix_str('done' if ok else 'FAILED', refresh=False)
                job.bar.refresh()
        elif overall is not None:
            tag = f"{CLR.GREEN}OK  {CLR.RESET}" if ok else f"{CLR.RED}FAIL{CLR.RESET}"
            tqdm.write(f"[{n}/{len(jobs)}] {tag} {job.title}")

    def run_task(job, key, idx, url, path):
        if not job.failed:
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
    print(f"\n{color}[INFO] Native videos done: {result['ok']} succeeded, {result['fail']} failed, "
          f"out of {len(jobs)}.{CLR.RESET}")
    for title, reason in failures:
        print(f"[WARN] {title}: {reason or 'failed'} (partial segments kept for resume).")


# =============================================================================
#  Intelligent file renamer (embedded from intelligent_file_renamer.py).
#  After a download finishes we offer to unify the freshly downloaded filenames
#  in the same way that tool does in its --strict mode (with a preview first).
# =============================================================================
NUM_RE = re.compile(r"\d+")
ARROW = "\u2192"  # →  (used by the preview below)
WORDCHARS = re.compile(r"[^\w]", re.UNICODE)
# tokenization: number | word (letters, with apostrophes) | separator/punctuation
TOKEN_RE = re.compile(r"\d+|[^\W\d_]+(?:['\u2019][^\W\d_]+)*|[\W_]+", re.UNICODE)


# --------------------------------------------------------------------------- #
#  Čištění názvů
# --------------------------------------------------------------------------- #
ILLEGAL_WIN = set('<>:"/\\|?*')
RESERVED_WIN = {"CON", "PRN", "AUX", "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
EMOJI_RANGES = [
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF),
    (0x2B00, 0x2BFF), (0x1F1E6, 0x1F1FF), (0xFE00, 0xFE0F), (0x200D, 0x200D),
]


def strip_pictographs(s):
    out = []
    for ch in s:
        cp = ord(ch)
        if any(a <= cp <= b for a, b in EMOJI_RANGES):
            continue
        if unicodedata.category(ch) in ("So", "Cf", "Cs", "Co"):
            continue
        out.append(ch)
    return "".join(out)


def strip_illegal(s):
    return "".join(ch for ch in s
                   if ch not in ILLEGAL_WIN and unicodedata.category(ch) != "Cc")


def clean_string(stem, removes):
    """Očistí název (emoji, zakázané znaky, uživatelské --remove, sjednocení mezer)."""
    s = strip_pictographs(stem)
    s = strip_illegal(s)
    for rgx in removes:
        s = rgx.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def finalize_stem(stem):
    stem = re.sub(r"[ \t]+", " ", stem).strip().strip(" .")
    if stem.upper() in RESERVED_WIN:
        stem += "_"
    return stem or "_"


# --------------------------------------------------------------------------- #
#  Tokenizace + velikost písmen
# --------------------------------------------------------------------------- #
SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "for", "of", "to",
               "in", "on", "at", "by", "vs", "with", "as", "from", "into",
               "over", "per"}
VOWELS = set("AEIOU")


def tokenize(s):
    """Vrátí list (kind, text): kind = 'num' | 'word' | 'sep'."""
    toks = []
    for m in TOKEN_RE.finditer(s):
        t = m.group(0)
        if t.isdigit():
            toks.append(("num", t))
        elif t[0].isalpha() or t[0] in "'\u2019":
            toks.append(("word", t))
        else:
            toks.append(("sep", t))
    return toks


def is_acronym(tok):
    letters = [c for c in tok if c.isalpha()]
    return len(letters) >= 3 and tok == tok.upper() and not (set(tok.upper()) & VOWELS)


def _cap_runs(tok):
    return re.sub(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*",
                  lambda m: m.group(0)[0].upper() + m.group(0)[1:],
                  tok.lower(), flags=re.UNICODE)


def case_word(tok, mode, first):
    if mode == "lower":
        return tok.lower()
    if mode == "upper":
        return tok.upper()
    if mode == "keep":
        return tok
    if is_acronym(tok):                      # CTLBT, LND, ...
        return tok
    core = WORDCHARS.sub("", tok).lower()
    if core in SMALL_WORDS and not first:    # a, of, the, over, ...
        return tok.lower()
    return _cap_runs(tok)


def apply_case(tokens, mode):
    out, first = [], True
    for kind, text in tokens:
        if kind == "word":
            out.append((kind, case_word(text, mode, first)))
            first = False
        else:
            out.append((kind, text))
    return out


def keys_of(tokens):
    """Klíče slov/čísel (bez oddělovačů): číslo -> '#', slovo -> malými."""
    return [("#" if k == "num" else t.lower()) for k, t in tokens if k != "sep"]


def split_lead(tokens):
    """Non-sep tokeny s předchozím oddělovačem + koncový oddělovač.
    Vrací (list (lead, kind, text), trailing_sep)."""
    res, lead = [], ""
    for kind, text in tokens:
        if kind == "sep":
            lead += text
        else:
            res.append((lead, kind, text))
            lead = ""
    return res, lead


def has_letters(key):
    return any(c.isalpha() for c in key)


# --------------------------------------------------------------------------- #
#  Detekce sérií
# --------------------------------------------------------------------------- #
DEFAULT_STOP = {
    "episode", "episodes", "episod", "ep", "eps", "reaction", "reactions",
    "react", "reacts", "reacting", "uncut", "full", "part", "pt", "video",
    "official", "hd", "fhd", "uhd", "4k", "2k", "premiere", "finale", "final",
    "trailer", "teaser", "subbed", "sub", "dub", "raw", "movie", "series",
    "season", "watch", "watching", "review", "recap", "highlights", "clip",
    "clips", "cut", "edit", "compilation", "special", "bonus", "early",
    "access", "kdrama", "drama", "anime",
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "as", "by", "from", "into", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "here", "there", "now", "new", "my",
    "your", "our", "their", "his", "her", "its", "i", "im", "you", "we",
    "they", "he", "she", "it", "vs", "ft", "feat", "no", "yes", "so", "just",
}


def file_words(tokens, stop):
    return [t.lower() for k, t in tokens
            if k == "word" and t.lower() not in stop]


def longest_common_run(a, b):
    if not a or not b:
        return 0
    dp = [0] * (len(b) + 1)
    best = 0
    for i in range(len(a)):
        ndp = [0] * (len(b) + 1)
        ai = a[i]
        for j in range(len(b)):
            if ai == b[j]:
                ndp[j + 1] = dp[j] + 1
                best = max(best, ndp[j + 1])
        dp = ndp
    return best


def cluster_series(word_lists, min_run):
    n = len(word_lists)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if longest_common_run(word_lists[i], word_lists[j]) >= min_run:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


# --------------------------------------------------------------------------- #
#  Zarovnání čísel + LCS
# --------------------------------------------------------------------------- #
def compute_widths(cleans, min_width):
    vals, rawlen = defaultdict(list), defaultdict(list)
    for s in cleans:
        for i, run in enumerate(NUM_RE.findall(s)):
            vals[i].append(int(run))
            rawlen[i].append(len(run))
    widths = {}
    for i in vals:
        w = max(len(str(max(vals[i]))), max(rawlen[i]))
        if min_width:
            w = max(w, min_width)
        widths[i] = w
    return widths


def lcs_align(ref, other):
    n, m = len(ref), len(other)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if ref[i] == other[j] \
                else max(dp[i + 1][j], dp[i][j + 1])
    ops, i, j = [], 0, 0
    while i < n and j < m:
        if ref[i] == other[j]:
            ops.append(("match", i, j)); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            ops.append(("ref", i)); i += 1
        else:
            ops.append(("file", j)); j += 1
    while i < n:
        ops.append(("ref", i)); i += 1
    while j < m:
        ops.append(("file", j)); j += 1
    return ops


def render(out_tokens, widths, trail=""):
    """out_tokens: list (lead, kind, text). Čísla zarovná podle widths."""
    parts, counter = [], 0
    for lead, kind, text in out_tokens:
        parts.append(lead)
        if kind == "num":
            parts.append(text.zfill(widths.get(counter, len(text))))
            counter += 1
        else:
            parts.append(text)
    parts.append(trail)
    return finalize_stem("".join(parts))


# --------------------------------------------------------------------------- #
#  Sjednocení jedné série
# --------------------------------------------------------------------------- #
def consensus_name(item, ref_ns, ref_keys, widths, do_words):
    """Bezpečný režim: zachová formát souboru, doplní chybějící společná slova."""
    file_ns = item["ns"]
    file_keys = item["keys"]
    ops = lcs_align(ref_keys, file_keys)
    matched_ref = {op[1] for op in ops if op[0] == "match"}
    file_word_only = any(op[0] == "file" and file_ns[op[1]][1] == "word" for op in ops)

    out = []
    for op in ops:
        if op[0] == "match":
            out.append(file_ns[op[2]])
        elif op[0] == "file":
            out.append(file_ns[op[1]])
        else:                                 # ref-only – kandidát na doplnění
            r = op[1]
            lead, kind, text = ref_ns[r]
            if not do_words or kind != "word" or file_word_only:
                continue
            # nevkládej slovo vázané na chybějící číslo (např. "Pt" bez čísla)
            bound = ((r + 1 < len(ref_keys) and ref_keys[r + 1] == "#"
                      and (r + 1) not in matched_ref)
                     or (r - 1 >= 0 and ref_keys[r - 1] == "#"
                         and (r - 1) not in matched_ref))
            if bound:
                continue
            out.append((lead, kind, text))
    return render(out, widths, item["trail"])


def strict_name(item, ref_ns, ref_keys, ref_trail, widths, ref_num_slots, ref_word_keys):
    """Striktní režim: přepíše soubor přesně podle vzoru série (mění se jen čísla)."""
    nums = [t for _, k, t in item["ns"] if k == "num"]
    file_word_keys = {k for k in item["keys"] if has_letters(k)}
    overlap = len(ref_word_keys & file_word_keys)
    fits = (len(nums) >= ref_num_slots
            and (not ref_word_keys or overlap >= (len(ref_word_keys) + 1) // 2))
    if not fits:
        return consensus_name(item, ref_ns, ref_keys, widths, do_words=True)

    out, ni = [], 0
    for lead, kind, text in ref_ns:
        if kind == "num":
            out.append((lead, "num", nums[ni])); ni += 1
        else:
            out.append((lead, kind, text))
    return render(out, widths, ref_trail)


def process_group(members, do_pad, do_words, min_width, strict):
    widths = compute_widths([m["clean"] for m in members], min_width) if do_pad else {}
    patterns = [tuple(m["keys"]) for m in members]
    counts = Counter(patterns)
    best = max(counts, key=lambda p: (counts[p], len(p)))
    ref = next(m for m, p in zip(members, patterns) if p == best)
    ref_ns, ref_trail = ref["ns"], ref["trail"]
    ref_keys = list(best)
    ref_num_slots = sum(1 for k in ref_keys if k == "#")
    ref_word_keys = {k for k in ref_keys if has_letters(k)}

    out = []
    for m in members:
        if strict:
            new = strict_name(m, ref_ns, ref_keys, ref_trail, widths,
                              ref_num_slots, ref_word_keys)
        else:
            new = consensus_name(m, ref_ns, ref_keys, widths, do_words)
        out.append((m["name"], new + m["ext"]))
    return out


def build_plan(filenames, do_pad, do_words, min_width, removes=(), case_mode="title",
               strict=False, group=True, group_min=1, stop=None):
    stop = DEFAULT_STOP if stop is None else (DEFAULT_STOP | set(stop))
    items = []
    for name in filenames:
        stem, ext = os.path.splitext(name)
        clean = clean_string(stem, removes)
        toks = apply_case(tokenize(clean), case_mode)
        ns, trail = split_lead(toks)
        items.append({"name": name, "ext": ext, "clean": clean,
                      "ns": ns, "trail": trail, "keys": keys_of(toks),
                      "words": file_words(toks, stop)})

    if group:
        groups = cluster_series([it["words"] for it in items], group_min)
    else:
        groups = [list(range(len(items)))]

    plan = []
    for gi in groups:
        plan.extend(process_group([items[i] for i in gi],
                                  do_pad, do_words, min_width, strict))
    order = {it["name"]: k for k, it in enumerate(items)}
    plan.sort(key=lambda p: order[p[0]])
    return plan, len(groups)
def detect_collisions(plan, existing):
    changing = {old: new for old, new in plan if old != new}
    targets = Counter(changing.values())
    sources = set(changing.keys())
    skip = set()
    for old, new in changing.items():
        if targets[new] > 1:
            skip.add(old)
        elif new in existing and new not in sources:
            skip.add(old)
    return skip


def _rn_color(s, c, enabled):
    codes = {"green": "32", "yellow": "33", "red": "31", "dim": "2", "cyan": "36"}
    return f"\033[{codes[c]}m{s}\033[0m" if enabled else s


def _rn_visible(s):
    return re.sub(r"\s+", " ", strip_pictographs(s)).strip()


def _rn_disp_width(s):
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _rn_pad_to(s, width):
    return s + " " * max(0, width - _rn_disp_width(s))


def print_preview(plan, skip, show_all, use_color):
    changed = [(o, n) for o, n in plan if o != n and o not in skip]
    skipped = [(o, n) for o, n in plan if o in skip]
    unchanged = [(o, n) for o, n in plan if o == n]
    width = max((_rn_disp_width(_rn_visible(o)) for o, _ in plan), default=0)

    for old, new in sorted(changed):
        print(f"  {_rn_pad_to(_rn_visible(old), width)}  {_rn_color(ARROW, 'cyan', use_color)}  "
              f"{_rn_color(new, 'green', use_color)}")
    for old, new in sorted(skipped):
        print(f"  {_rn_pad_to(_rn_visible(old), width)}  {_rn_color(ARROW, 'red', use_color)}  "
              f"{_rn_color(new + '   [CONFLICT - skipped]', 'red', use_color)}")
    if show_all:
        for old, _ in sorted(unchanged):
            print(_rn_color(f"  {_rn_pad_to(_rn_visible(old), width)}     (unchanged)", "dim", use_color))

    print()
    print(f"  Changed: {_rn_color(str(len(changed)), 'green', use_color)}   "
          f"Conflicts: {_rn_color(str(len(skipped)), 'red' if skipped else 'dim', use_color)}   "
          f"Unchanged: {_rn_color(str(len(unchanged)), 'dim', use_color)}")
    return changed


def apply_renames(changed, directory, use_color):
    """Safe rename via temporary names (handles A<->B swaps and cycles)."""
    tag = uuid.uuid4().hex[:8]
    temps = []
    try:
        for i, (old, _) in enumerate(changed):
            tmp = f".__rename_{tag}_{i}__"
            os.rename(os.path.join(directory, old), os.path.join(directory, tmp))
            temps.append(tmp)
        for (old, new), tmp in zip(changed, temps):
            os.rename(os.path.join(directory, tmp), os.path.join(directory, new))
        print(_rn_color(f"\n  Done - renamed {len(changed)} file(s).", "green", use_color))
    except OSError as e:
        print(_rn_color(f"\n  ERROR while renaming: {e}", "red", use_color))


_RENAME_VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.m4v', '.mov', '.avi', '.ts')

# Files successfully downloaded during THIS run (absolute paths). The post-download rename
# offer works only over these, never over unrelated files already sitting in the directory.
_session_downloads = []
_session_downloads_lock = threading.Lock()


def _record_download(path):
    """Register a file that finished downloading in this run (for the rename offer)."""
    try:
        ap = os.path.abspath(path)
    except Exception:
        return
    with _session_downloads_lock:
        _session_downloads.append(ap)


def _session_downloads_in(directory):
    """Basenames of this run's downloads that live directly in `directory` and still exist."""
    directory = os.path.abspath(directory)
    out = []
    seen = set()
    with _session_downloads_lock:
        paths = list(_session_downloads)
    for p in paths:
        if os.path.dirname(p) == directory and os.path.exists(p):
            b = os.path.basename(p)
            if b not in seen:
                seen.add(b)
                out.append(b)
    return out


def _list_video_files(directory):
    try:
        return {f for f in os.listdir(directory)
                if os.path.isfile(os.path.join(directory, f))
                and os.path.splitext(f)[1].lower() in _RENAME_VIDEO_EXTS}
    except OSError:
        return set()


def offer_strict_rename(directory, new_files, verbose, enabled=True):
    """Preview an intelligent --strict rename of the freshly downloaded files and, if the
    user confirms, apply it. Silent when non-interactive or nothing was downloaded."""
    if not enabled or not new_files:
        return
    interactive = bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
        bool(getattr(sys.stdout, 'isatty', lambda: False)())
    if not interactive:
        return
    try:
        plan, _n = build_plan(sorted(new_files), do_pad=True, do_words=True,
                              min_width=0, removes=(), case_mode="title", strict=True,
                              group=True, group_min=1, stop=[])
    except Exception as exc:
        if verbose:
            print(f"[WARN] Renamer could not build a plan: {exc}")
        return
    try:
        existing = set(os.listdir(directory))
    except OSError:
        existing = set()
    skip = detect_collisions(plan, existing)
    use_color = bool(CLR.RESET)
    print(f"\n[INFO] {len(new_files)} file(s) downloaded. Intelligent --strict rename preview:")
    changed = print_preview(plan, skip, True, use_color)
    if not changed:
        print("[INFO] Filenames are already consistent; nothing to rename.")
        return
    try:
        ans = input("\nApply these renames? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return
    if ans in ("n", "no", "ne"):
        print("[INFO] Left the filenames unchanged.")
    else:
        apply_renames(changed, directory, use_color)


def _classify_input(id_or_url: str):
    """Return (kind, target) for any supported input."""
    pid = extract_patreon_collection_id(id_or_url)
    if pid:
        return 'patreon', pid
    post_id = extract_patreon_post_id(id_or_url)
    if post_id:
        return 'patreon_post', post_id
    if 'patreon.com' in id_or_url:
        return 'patreon_bad', None
    if 'dropbox.com' in id_or_url:
        return 'dropbox', id_or_url
    if 'vimeo.com' in id_or_url:
        vid = re.search(r'vimeo\.com/(?:video/)?(\d+)', id_or_url)
        if vid:
            h = re.search(r'[/?&]h=([0-9a-zA-Z]+)', id_or_url) or \
                re.search(r'vimeo\.com/\d+/([0-9a-zA-Z]+)', id_or_url)
            return 'vimeo', {'source': 'vimeo', 'title': vid.group(1),
                             'vimeo_id': vid.group(1), 'vimeo_hash': h.group(1) if h else ''}
    kind, tid = extract_drive_target(id_or_url)
    return kind, tid


def main(id_or_url: str, output_file: str = None, chunk_size: int = DEFAULT_CHUNK_SIZE,
         num_threads: int = None, verbose: bool = False, cookies_file: str = None,
         folder_workers: int = DEFAULT_FOLDER_WORKERS, recursive: bool = DEFAULT_RECURSIVE,
         max_connections: int = None, use_color: bool = USE_COLOR,
         auto_cookies: bool = AUTO_COOKIES, select: bool = False, auto: bool = False,
         out_dir: str = None, max_height: int = DEFAULT_MAX_HEIGHT, list_only: bool = False,
         ffmpeg_path: str = None, ffmpeg_url: str = None, do_rename: bool = True) -> None:
    """Download from Google Drive (file/folder), Dropbox, Vimeo, a Patreon collection, or a
    single Patreon post (any of which may link to Drive/Dropbox and/or host native Vimeo/Mux)."""
    global FFMPEG, FFMPEG_DOWNLOAD_URL
    if ffmpeg_path:
        FFMPEG = ffmpeg_path
    if ffmpeg_url is not None:
        FFMPEG_DOWNLOAD_URL = ffmpeg_url
    setup_console(use_color)

    kind, target_id = _classify_input(id_or_url)
    if kind == 'patreon_bad':
        print("[ERROR] That looks like a Patreon URL but I couldn't find a collection or post id.")
        print("        Expected a collection like https://www.patreon.com/collection/122162")
        print("        or a post like https://www.patreon.com/<creator>/posts/<slug>-162557660")
        sys.exit(1)

    # Resolve threads/connections: explicit -t/-m always win; otherwise --auto scales by CPU,
    # else the configured defaults.
    global _max_conn_explicit
    _max_conn_explicit = max_connections is not None  # explicit -m disables per-source caps
    if auto:
        a_threads, a_conns, logical = _auto_settings()
        print(f"[INFO] --auto: {logical} logical CPU(s) detected -> {a_threads} threads/file, "
              f"{a_conns} connections.")
    if num_threads is None:
        num_threads = a_threads if auto else DEFAULT_THREADS
    if max_connections is None:
        max_connections = a_conns if auto else DEFAULT_MAX_CONNECTIONS

    # Cap total simultaneous connections regardless of folder_workers * threads.
    set_connection_limit(max_connections)

    # Build the list of cookie files: explicit --cookies wins; otherwise auto-detect.
    if cookies_file:
        cookies_files = [cookies_file]
    elif auto_cookies:
        cookies_files = auto_detect_cookies(verbose)
    else:
        cookies_files = []

    if verbose:
        print(f"[INFO] Target kind: {kind}")
        print(f"[INFO] Max simultaneous connections: {max_connections}")
        if cookies_files:
            print(f"[INFO] Using cookies from: {', '.join(cookies_files)}")

    try:
        session = get_cookies_session(cookies_files)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] Failed to load cookies: {e}", file=sys.stderr)
        sys.exit(1)

    def _with_out_dir(fn):
        """Run fn() with CWD switched to out_dir (so outputs land there)."""
        if not out_dir:
            return fn()
        os.makedirs(out_dir, exist_ok=True)
        old = os.getcwd()
        os.chdir(out_dir)
        try:
            return fn()
        finally:
            os.chdir(old)

    rename_dir = os.path.abspath(out_dir) if out_dir else os.getcwd()
    with _session_downloads_lock:
        _session_downloads.clear()

    try:
        if kind == 'patreon':
            if output_file:
                print("[WARN] -o/--output is ignored for Patreon collections; names come from the posts.")
            process_patreon_collection(target_id, session, chunk_size, num_threads, folder_workers,
                                       recursive, verbose, select=select, out_dir=out_dir,
                                       max_connections=max_connections, max_height=max_height,
                                       list_only=list_only)
        elif kind == 'patreon_post':
            if output_file:
                print("[WARN] -o/--output is ignored for Patreon posts; names come from the post.")
            process_patreon_post(target_id, session, chunk_size, num_threads, folder_workers,
                                 recursive, verbose, select=select, out_dir=out_dir,
                                 max_connections=max_connections, max_height=max_height,
                                 list_only=list_only)
        elif kind == 'dropbox':
            fname = safe_filename(output_file, 'video') if output_file else \
                dropbox_filename(target_id, 'video')
            durl = normalize_dropbox_url(target_id)
            entry = {'id': durl, 'title': fname, 'name': fname, 'direct_url': durl}
            _with_out_dir(lambda: download_folder_pooled([entry], session, chunk_size, verbose,
                                                         label="Dropbox", conn_cap=DROPBOX_DEFAULT_CONNECTIONS))
        elif kind == 'vimeo':
            if not ensure_ffmpeg(verbose):
                print("[ERROR] Vimeo videos are HLS and need ffmpeg to mux audio+video into MP4.")
                sys.exit(1)
            if output_file:
                target_id['title'] = os.path.splitext(os.path.basename(output_file))[0]
            _with_out_dir(lambda: download_hls_pooled([target_id], session, None,
                                                      max_connections, max_height, verbose))
        elif kind == 'folder':
            if output_file:
                print("[WARN] -o/--output is ignored for folders; names come from Drive.")
            _with_out_dir(lambda: process_folder(target_id, session, chunk_size, num_threads,
                                                 folder_workers, recursive, verbose, select=select))
        else:
            if select:
                print("[WARN] --select only applies to folders/collections; ignoring.")
            ok = _with_out_dir(lambda: process_single_video(target_id, session, output_file,
                                                            chunk_size, num_threads, verbose))
            if not ok:
                if not cookies_files:
                    print("Tip: For private files, use --cookies to provide a cookies.txt/JSON export.")
                session.close()
                sys.exit(1)
    finally:
        session.close()

    # After a successful download, offer to tidy up ONLY the files this run downloaded
    # (never unrelated files already present in the directory).
    if do_rename and not list_only:
        new_files = _session_downloads_in(rename_dir)
        offer_strict_rename(rename_dir, new_files, verbose)

    # Remove the .temp scratch folder if everything finished (leftover parts from an
    # interrupted run keep it so a re-run can resume).
    _cleanup_temp_dir(rename_dir)

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

    parser = argparse.ArgumentParser(description="Download videos from Google Drive (single file or whole folder), Dropbox, Vimeo, or a Patreon collection. A Patreon collection is fully mined: Google Drive links, Dropbox links, AND native Patreon/Vimeo/Mux videos are all downloaded.")
    parser.add_argument("video_id", type=str, help="A Drive file ID / file URL / FOLDER URL, a Dropbox share URL, a Vimeo URL, or a Patreon COLLECTION URL (.../collection/ID). Folders and collections download every video found.")
    parser.add_argument("-o", "--output", type=str, help="Output file name (single file only; ignored for folders/collections).")
    parser.add_argument("-d", "--output-dir", type=str, default=None, help="Directory to save into (applies to any mode; created if missing). Default: current directory.")
    parser.add_argument("-c", "--chunk_size", type=positive_int, default=DEFAULT_CHUNK_SIZE, help=f"Streaming chunk size in bytes. Default {DEFAULT_CHUNK_SIZE} (edit DEFAULT_CHUNK_SIZE at the top of the script).")
    parser.add_argument("-t", "--threads", type=positive_int, default=None, help=f"Download threads per file (>=1). Default {DEFAULT_THREADS}. Edit DEFAULT_THREADS at the top.")
    parser.add_argument("-w", "--folder-workers", type=nonneg_int, default=DEFAULT_FOLDER_WORKERS, help=f"How many videos to download at once for a folder. 0 = ALL at once. Default {DEFAULT_FOLDER_WORKERS}. Edit DEFAULT_FOLDER_WORKERS at the top.")
    parser.add_argument("-m", "--max-connections", type=positive_int, default=None, help=f"Hard cap on simultaneous connections regardless of workers x threads. Default {DEFAULT_MAX_CONNECTIONS}. Lower (e.g. 16) if you hit 'insufficient resources'.")
    parser.add_argument("-q", "--max-height", type=nonneg_int, default=DEFAULT_MAX_HEIGHT, help="For native Patreon/Vimeo HLS videos: cap height (e.g. 720). 0 = best available (default).")
    parser.add_argument("--no-auto", action="store_true", help="Disable the default auto-tuning of threads/connections from the detected CPU (use the fixed defaults). Explicit -t/-m still win.")
    parser.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # now the default; kept so old commands don't break
    parser.add_argument("-s", "--select", action="store_true", help="For a folder or Patreon collection: list all items first and interactively choose which to download.")
    parser.add_argument("-l", "--list", action="store_true", help="For a Patreon collection: only list what was found (Drive/Dropbox/native), do not download.")
    parser.add_argument("--no-recursive", action="store_true", help="Do not descend into subfolders when given a folder.")
    parser.add_argument("--no-auto-cookies", action="store_true", help="Do not auto-use a *.json cookie file found next to the script / in the current directory.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookies", type=str, help="Path to a Netscape cookies.txt file or JSON cookie export (Google Drive / Patreon session).")
    parser.add_argument("--ffmpeg", type=str, default=None, help="Path to the ffmpeg executable or a folder containing it (for native HLS videos).")
    parser.add_argument("--ffmpeg-url", type=str, default=None, help="URL of an ffmpeg archive to auto-download if ffmpeg is missing. Empty string disables auto-download.")
    parser.add_argument("--no-rename", action="store_true", help="After downloading, do NOT offer the intelligent --strict rename of the new files.")
    parser.add_argument("--version", action="version", version="%(prog)s 2.16.0")

    args = parser.parse_args()
    try:
        main(args.video_id, args.output, args.chunk_size, args.threads, args.verbose, args.cookies,
             folder_workers=args.folder_workers, recursive=(not args.no_recursive and DEFAULT_RECURSIVE),
             max_connections=args.max_connections, use_color=(USE_COLOR and not args.no_color),
             auto_cookies=(AUTO_COOKIES and not args.no_auto_cookies), select=args.select,
             auto=(not args.no_auto), out_dir=args.output_dir, max_height=args.max_height,
             list_only=args.list, ffmpeg_path=args.ffmpeg, ffmpeg_url=args.ffmpeg_url,
             do_rename=(not args.no_rename))
    except KeyboardInterrupt:
        # Locks are released by the atexit handler; partial .part files are kept so a re-run
        # resumes where this left off.
        print("\n[WARN] Interrupted. Partial parts were kept — re-run the same command to resume.")
        sys.exit(130)
