from urllib.parse import unquote
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

thread_errors = []


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

def get_cookies_session(cookies_file: str = None) -> requests.Session:
    """Create a requests session with optional cookies loaded from file."""
    session = requests.Session()

    if cookies_file:
        cookie_jar = load_cookies_from_file(cookies_file)
        session.cookies = cookie_jar

    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    })

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


def list_folder_videos(folder_id: str, session: requests.Session, recursive: bool, verbose: bool, _depth: int = 0) -> list[dict]:
    """Recursively collect candidate video files in a folder.

    A child is treated as a subfolder when its mime is the Drive folder type, as a video
    when its mime starts with 'video/', and otherwise when its mime is unknown (so it can
    still be probed later). Clearly non-video files (images, docs, audio) are skipped.
    """
    videos = []
    for e in list_folder_entries(folder_id, session, verbose):
        mime = e['mime']
        if mime == 'application/vnd.google-apps.folder':
            if recursive:
                if verbose:
                    print(f"[INFO] {'  ' * _depth}Entering subfolder: {e['title']}")
                videos.extend(list_folder_videos(e['id'], session, recursive, verbose, _depth + 1))
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
            title = unquote(content.split('=')[-1])
        elif "videoplayback" in content and not video:
            video = unquote(content).split("|")[-1]
        if video and title:
            break

    if verbose:
        print(f"[INFO] Video URL: {video}")
        print(f"[INFO] Video Title: {title}")

    return video, title

def get_file_size(url: str, session: requests.Session) -> int:
    """Gets the total file size via a HEAD request."""
    response = session.head(
        url,
        allow_redirects=True,
        headers={
            'Referer': 'https://drive.google.com/',
        }
    )
    return int(response.headers.get('content-length', 0))

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
        
    response = session.get(url, stream=True, headers=headers)
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

def download_part_wrapper(*args):
    try:
        download_part(*args)
    except Exception as e:
        print(e)
        thread_errors.append(e)

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

    thread_errors.clear()
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
    gpBar = tqdm(
        unit='B', unit_scale=True,
        desc=gp_desc,
        total=total_size,
        position=position,
        leave=show_part_bars,
    )

    if show_part_bars:
        pbars = [
            tqdm(
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

    for i in range(num_threads):
        start = i * part_size
        end = min(start + part_size - 1, total_size - 1)
        part_filename = f"{filename}.part{i}"
        part_files.append(part_filename)

        worker_session = requests.Session()
        worker_session.cookies.update(session.cookies)
        worker_session.headers.update(session.headers)

        t = threading.Thread(
            target=download_part_wrapper,
            args=(url, worker_session, thread_lock, start, end, i, part_filename, chunk_size, pbars[i], gpBar, verbose),
            daemon=True
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    gpBar.close()
    for pbar in pbars:
        if pbar is not None:
            pbar.close()

    if(len(thread_errors) > 0):
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

    response = session.get(url, stream=True, headers=headers)
    if response.status_code in (200, 206):  # 200 for new downloads, 206 for partial content
        total_size = int(response.headers.get('content-length', 0)) + downloaded_size
        with open(filename, file_mode) as file:
            with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=os.path.basename(filename), position=position, file=sys.stdout) as pbar:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        file.write(chunk)
                        pbar.update(len(chunk))
        print(f"\n{filename} downloaded successfully.")
        return True
    else:
        print(f"Error downloading {filename}, status code: {response.status_code}")
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
                         num_threads: int, verbose: bool, position: int = 0, show_part_bars: bool = True) -> bool:
    """Resolve, lock, and download a single video. Returns True on success."""
    video, title = fetch_video(video_id, session, verbose)
    if not video:
        print(f"[WARN] Could not get a playback URL for {video_id} (not a video, private, or unavailable). Skipping.")
        return False

    valid_filename = safe_filename(output_file or title or f"{video_id}.mp4", video_id)

    lock_path = valid_filename + ".lock"
    if not acquire_lock(lock_path):
        return False
    try:
        return download_file(video, session, valid_filename, chunk_size, num_threads, verbose,
                             position=position, show_part_bars=show_part_bars)
    finally:
        release_lock(lock_path)


def process_folder(folder_id: str, session: requests.Session, chunk_size: int, num_threads: int,
                   folder_workers: int, recursive: bool, verbose: bool) -> None:
    """List a folder and download all videos in it, up to `folder_workers` at a time."""
    print(f"[INFO] Listing folder {folder_id}" + (" (recursive)" if recursive else "") + " ...")
    videos = list_folder_videos(folder_id, session, recursive, verbose)
    if not videos:
        print("[ERROR] No videos found in the folder.")
        print("        The folder may be empty/private, may need a resource key, or Google may have")
        print("        changed the listing markup (see list_folder_entries() to adjust).")
        return

    print(f"[INFO] Found {len(videos)} candidate file(s). Downloading {folder_workers} at a time, "
          f"{num_threads} threads each.\n")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import queue

    # Pool of terminal line positions so concurrent progress bars don't overlap.
    positions = queue.Queue()
    for p in range(folder_workers):
        positions.put(p)

    done = {'ok': 0, 'fail': 0}
    done_lock = threading.Lock()

    def worker(entry):
        pos = positions.get()
        # Each concurrent file gets its own session to avoid sharing connection state.
        sess = requests.Session()
        sess.cookies.update(session.cookies)
        sess.headers.update(session.headers)
        try:
            ok = process_single_video(entry['id'], sess, None, chunk_size, num_threads, verbose,
                                      position=pos, show_part_bars=False)
        except Exception as exc:
            tqdm.write(f"[ERROR] {entry['title']}: {exc}")
            ok = False
        finally:
            positions.put(pos)
        with done_lock:
            done['ok' if ok else 'fail'] += 1
            n = done['ok'] + done['fail']
        tqdm.write(f"[{n}/{len(videos)}] {'OK ' if ok else 'FAIL'} {entry['title']}")
        return ok

    with ThreadPoolExecutor(max_workers=folder_workers) as ex:
        futures = [ex.submit(worker, v) for v in videos]
        for _ in as_completed(futures):
            pass

    print(f"\n[INFO] Folder done: {done['ok']} succeeded, {done['fail']} failed, out of {len(videos)}.")


def main(id_or_url: str, output_file: str = None, chunk_size: int = 4 * 1024 * 1024, num_threads: int = 4,
         verbose: bool = False, cookies_file: str = None, folder_workers: int = 3, recursive: bool = True) -> None:
    """Process a Drive file OR folder URL/ID and download the video(s)."""
    kind, target_id = extract_drive_target(id_or_url)

    if verbose:
        print(f"[INFO] Target kind: {kind}, id: {target_id}")
        if cookies_file:
            print(f"[INFO] Using cookies from: {cookies_file}")

    try:
        session = get_cookies_session(cookies_file)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] Failed to load cookies: {e}", file=sys.stderr)
        sys.exit(1)

    if kind == 'folder':
        if output_file:
            print("[WARN] -o/--output is ignored for folders; names come from Drive.")
        process_folder(target_id, session, chunk_size, num_threads, folder_workers, recursive, verbose)
    else:
        ok = process_single_video(target_id, session, output_file, chunk_size, num_threads, verbose)
        if not ok:
            if not cookies_file:
                print("Tip: For private files, use --cookies to provide a cookies.txt/JSON export.")
            sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download videos from Google Drive (single file or whole folder).")
    parser.add_argument("video_id", type=str, help="A Drive file ID, file URL (.../file/d/ID/view), or a FOLDER URL (.../drive/folders/ID). For a folder, all videos inside are downloaded.")
    parser.add_argument("-o", "--output", type=str, help="Output file name (single file only; ignored for folders).")
    parser.add_argument("-c", "--chunk_size", type=int, default=4 * 1024 * 1024, help="Chunk size (in bytes) for streaming the download. Default is 4194304 (4 MiB). Bigger = far less per-chunk overhead and faster downloads.")
    parser.add_argument("-t", "--threads", type=int, default=4, choices=range(1, 17), help="Parallel download threads per file (1-16). Default is 4.")
    parser.add_argument("-w", "--folder-workers", type=int, default=3, choices=range(1, 9), help="How many videos to download at the same time when given a folder (1-8). Default is 3.")
    parser.add_argument("--no-recursive", action="store_true", help="Do not descend into subfolders when given a folder.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookies", type=str, help="Path to a Netscape cookies.txt file or JSON cookie export for private Google Drive files/folders.")
    parser.add_argument("--version", action="version", version="%(prog)s 1.3.0")

    args = parser.parse_args()
    main(args.video_id, args.output, args.chunk_size, args.threads, args.verbose, args.cookies,
         folder_workers=args.folder_workers, recursive=not args.no_recursive)
