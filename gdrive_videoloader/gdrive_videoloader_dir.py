from urllib.parse import unquote, unquote_plus
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
PER_FILE_BAR_LIMIT = 16
SEGMENT_MIB = 32                  # folder mode: split each file into segments of this size (MiB)
                                  # so freed connections flow to remaining files (work-stealing).
BAR_NCOLS = 100                   # fixed progress-bar width so bars don't stretch across wide terminals
BAR_DESC_WIDTH = 26               # fixed filename column width so all bars line up
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # On some platforms (e.g. Windows) signal 0 is unsupported; assume alive to be safe.
        return True
    return True


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

            print(f"[ERROR] Another instance is already downloading this file.")
            print(f"        Lock held by PID {owner} ({lock_path}).")
            print(f"        Wait for it to finish, choose a different name with -o,")
            print(f"        or delete the lock file if you are sure no other run is active.")
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


def list_folder_entries(folder_id: str, session: requests.Session, verbose: bool) -> list[dict]:
    """List the direct children of a Drive folder via the embeddedfolderview endpoint.

    Returns a list of dicts: {'id', 'title', 'mime'}. 'mime' may be '' if it could not
    be detected. This is the one spot that depends on Google's HTML; if Google ever
    changes the markup, only this function needs updating.
    """
    url = f'https://drive.google.com/embeddedfolderview?id={folder_id}#list'
    resp = session.get(url, headers={'Referer': 'https://drive.google.com/'})
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
        print("[INFO] Parsing video playback URL and title.")
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
        print(f"[INFO] Video URL: {video}")
        print(f"[INFO] Video Title: {title}")

    return video, title

def get_file_size(url: str, session: requests.Session) -> int:
    """Get the total file size. Tries HEAD first, then a ranged GET (Google often
    ignores HEAD content-length, which previously forced a slow single-threaded fallback)."""
    headers = {'Referer': 'https://drive.google.com/'}

    with connection_slot():
        response = session.head(url, allow_redirects=True, headers=headers)
    size = int(response.headers.get('content-length', 0) or 0)
    if size > 0:
        return size

    # Fallback: ask for one byte and read the total from the Content-Range header.
    with connection_slot():
        with session.get(url, stream=True, allow_redirects=True,
                         headers={**headers, 'Range': 'bytes=0-0'}) as r:
            content_range = r.headers.get('content-range', '')
            if '/' in content_range:
                total = content_range.rsplit('/', 1)[-1].strip()
                if total.isdigit() and int(total) > 0:
                    return int(total)
            cl = r.headers.get('content-length', '')
            if r.status_code == 200 and cl.isdigit():
                return int(cl)
    return 0

def download_part(url: str, session: requests.Session, thread_lock, start: int, end: int, part_num: int, part_filename: str, chunk_size: int, pbar: tqdm, gpbar: tqdm, verbose: bool) -> None:
    """Downloads a specific byte range of the file and writes it to a part file."""
    headers = {
        'Range': f'bytes={start}-{end}',
        'Referer': 'https://drive.google.com/',
    }

    # Support resuming individual parts
    downloaded = 0
    if os.path.exists(part_filename):
        downloaded = os.path.getsize(part_filename)
        if downloaded > 0:
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
        with session.get(url, stream=True, headers=headers) as response:
            if response.status_code not in (200, 206):
                raise Exception(f"[ERROR] Failed to download part {part_filename}, status: {response.status_code}")

            file_mode = 'ab' if os.path.exists(part_filename) and os.path.getsize(part_filename) > 0 else 'wb'
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
    if verbose:
        print(f"[INFO] Merging {len(part_files)} parts into {output_filename}")

    missing = [pf for pf in part_files if not os.path.exists(pf)]
    if missing:
        print(f"[ERROR] Missing parts: {missing}")
        return

    tmp_output = output_filename + ".merging"
    with open(tmp_output, 'wb') as outfile:
        for part_file in part_files:
            if verbose:
                print("Merging " + part_file)
            with open(part_file, 'rb') as pf:
                shutil.copyfileobj(pf, outfile, length=8 * 1024 * 1024)
        outfile.flush()
        os.fsync(outfile.fileno())

    # Atomic swap: the final name only appears once the file is fully written.
    os.replace(tmp_output, output_filename)

    for part_file in part_files: # Cleanup
        os.remove(part_file)

    if verbose:
        print(f"[INFO] Merge complete. Cleaned up part files.")

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
        part_filename = f"{filename}.part{i}"
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
        with session.get(url, stream=True, headers=headers) as response:
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
        print(f"[INFO] Accessing {drive_url}")

    response = session.get(drive_url, allow_redirects=True)
    page_content = response.text

    if verbose:
        print(f"[INFO] get_video_info status: {response.status_code}")
        print(f"[INFO] response length: {len(page_content)} chars")

    return get_video_url(page_content, verbose)


def safe_filename(name: str, fallback_id: str) -> str:
    """Sanitise a filename for Windows + Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', name)
    name = re.sub(r'[. ]+$', '', name)
    return name or f"{fallback_id}.mp4"


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

    valid_filename = safe_filename(output_file or title or f"{video_id}.mp4", video_id)

    lock_path = valid_filename + ".lock"
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

    for attempt in range(3):
        lo = start + downloaded
        headers = {'Referer': 'https://drive.google.com/'}
        if end is not None:
            headers['Range'] = f'bytes={lo}-{end}'
        elif downloaded:
            headers['Range'] = f'bytes={lo}-'

        try:
            with session.get(job.url, stream=True, headers=headers) as r:
                if r.status_code in (403, 410) and attempt < 2:
                    _refresh_job_url(job, session, verbose)
                    continue
                if r.status_code not in (200, 206):
                    return False
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
            return True
        except requests.RequestException:
            if attempt < 2:
                _refresh_job_url(job, session, verbose)
                continue
            return False
    return False


def download_folder_pooled(videos: list, session: requests.Session, chunk_size: int, verbose: bool) -> None:
    """Download all videos via a shared pool of `max_connections` workers pulling segments
    from any file. As files finish, workers automatically move to the remaining files, so
    the connection budget is always fully used and the last files speed up."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    budget = _max_connections
    seg_size = max(1, SEGMENT_MIB) * 1024 * 1024

    # 1) Resolve URL + size for every file, in parallel.
    print(f"[INFO] Resolving {len(videos)} file(s)...")
    jobs = [_FileJob(v) for v in videos]

    def resolve(job):
        url, title = fetch_video(job.id, session, verbose)
        if not url:
            job.failed = True
            return
        job.url = url
        job.size = get_file_size(url, session)
        job.filename = safe_filename(title or f"{job.id}.mp4", job.id)

    with ThreadPoolExecutor(max_workers=min(budget, 16)) as ex:
        list(ex.map(resolve, jobs))

    ready = []
    for job in jobs:
        if job.failed or not job.filename:
            tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Skipping (no playback URL): {job.title}")
            continue
        job.lock_path = job.filename + ".lock"
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
                job.segments.append({'start': s, 'end': e, 'path': f"{job.filename}.part{i}"})
        else:
            job.segments.append({'start': 0, 'end': None, 'path': f"{job.filename}.part0"})
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
            if job.size == 0 or got >= job.size:
                merge_parts([s['path'] for s in job.segments], job.filename, verbose)
                ok = True
            else:
                reason = f"incomplete {got}/{job.size} bytes"
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
            tqdm.write(f"[{n}/{len(ready)}] {tag} {job.title}")

    def run_segment(job, seg):
        if job.failed:
            return
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
    print(f"\n{color}[INFO] Folder done: {result['ok']} succeeded, {result['fail']} failed, "
          f"out of {len(ready)}.{CLR.RESET}")
    for title, reason in failures:
        print(f"[WARN] {title}: {reason or 'failed'} (parts kept for resume).")


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


def main(id_or_url: str, output_file: str = None, chunk_size: int = DEFAULT_CHUNK_SIZE,
         num_threads: int = None, verbose: bool = False, cookies_file: str = None,
         folder_workers: int = DEFAULT_FOLDER_WORKERS, recursive: bool = DEFAULT_RECURSIVE,
         max_connections: int = None, use_color: bool = USE_COLOR,
         auto_cookies: bool = AUTO_COOKIES, select: bool = False, auto: bool = False) -> None:
    """Process a Drive file OR folder URL/ID and download the video(s)."""
    setup_console(use_color)
    kind, target_id = extract_drive_target(id_or_url)

    # Resolve threads/connections: explicit -t/-m always win; otherwise --auto scales by CPU,
    # else the configured defaults.
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
        print(f"[INFO] Target kind: {kind}, id: {target_id}")
        print(f"[INFO] Max simultaneous connections: {max_connections}")
        if cookies_files:
            print(f"[INFO] Using cookies from: {', '.join(cookies_files)}")

    try:
        session = get_cookies_session(cookies_files)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] Failed to load cookies: {e}", file=sys.stderr)
        sys.exit(1)

    if kind == 'folder':
        if output_file:
            print("[WARN] -o/--output is ignored for folders; names come from Drive.")
        process_folder(target_id, session, chunk_size, num_threads, folder_workers, recursive, verbose,
                       select=select)
        session.close()
    else:
        if select:
            print("[WARN] --select only applies to folder URLs; ignoring.")
        ok = process_single_video(target_id, session, output_file, chunk_size, num_threads, verbose)
        session.close()
        if not ok:
            if not cookies_files:
                print("Tip: For private files, use --cookies to provide a cookies.txt/JSON export.")
            sys.exit(1)

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

    parser = argparse.ArgumentParser(description="Download videos from Google Drive (single file or whole folder).")
    parser.add_argument("video_id", type=str, help="A Drive file ID, file URL (.../file/d/ID/view), or a FOLDER URL (.../drive/folders/ID). For a folder, all videos inside are downloaded.")
    parser.add_argument("-o", "--output", type=str, help="Output file name (single file only; ignored for folders).")
    parser.add_argument("-c", "--chunk_size", type=positive_int, default=DEFAULT_CHUNK_SIZE, help=f"Streaming chunk size in bytes. Default {DEFAULT_CHUNK_SIZE} (edit DEFAULT_CHUNK_SIZE at the top of the script).")
    parser.add_argument("-t", "--threads", type=positive_int, default=None, help=f"Download threads per file (>=1). Default {DEFAULT_THREADS}. Edit DEFAULT_THREADS at the top.")
    parser.add_argument("-w", "--folder-workers", type=nonneg_int, default=DEFAULT_FOLDER_WORKERS, help=f"How many videos to download at once for a folder. 0 = ALL at once. Default {DEFAULT_FOLDER_WORKERS}. Edit DEFAULT_FOLDER_WORKERS at the top.")
    parser.add_argument("-m", "--max-connections", type=positive_int, default=None, help=f"Hard cap on simultaneous connections regardless of workers x threads. Default {DEFAULT_MAX_CONNECTIONS}. Lower (e.g. 16) if you hit 'insufficient resources'.")
    parser.add_argument("--auto", action="store_true", help="Auto-pick threads and connections from the detected CPU. Explicit -t/-m still win.")
    parser.add_argument("-s", "--select", action="store_true", help="For a folder: list all files first and interactively choose which to download.")
    parser.add_argument("--no-recursive", action="store_true", help="Do not descend into subfolders when given a folder.")
    parser.add_argument("--no-auto-cookies", action="store_true", help="Do not auto-use a *.json cookie file found next to the script / in the current directory.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookies", type=str, help="Path to a Netscape cookies.txt file or JSON cookie export for private Google Drive files/folders.")
    parser.add_argument("--version", action="version", version="%(prog)s 1.14.0")

    args = parser.parse_args()
    main(args.video_id, args.output, args.chunk_size, args.threads, args.verbose, args.cookies,
         folder_workers=args.folder_workers, recursive=(not args.no_recursive and DEFAULT_RECURSIVE),
         max_connections=args.max_connections, use_color=(USE_COLOR and not args.no_color),
         auto_cookies=(AUTO_COOKIES and not args.no_auto_cookies), select=args.select, auto=args.auto)
