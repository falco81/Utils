#!/usr/bin/env python3
"""
video_tool.py  (Windows 10 CLI version, no alass)
=====================================================

Fixes shifted subtitle timing (e.g. Czech) using correctly timed subtitles
embedded in an MKV file (e.g. English) - even when they are in a different
language and split into lines differently (a professional translation).

Container support (MKV vs MP4)
------------------------------
- .mkv / .webm  -> subtitles are extracted via mkvextract (exact, lossless).
- .mp4 / .m4v / .mov / others -> mkvextract cannot touch these (Matroska only),
  so in that case subtitles are pulled by ffmpeg instead (mov_text -> srt).
  For MP4, ffmpeg is therefore ALWAYS required (even with --audio-mode off),
  whereas for MKV only if you enable --audio-mode replace/combine.
The audio track (VAD) is always extracted via ffmpeg, regardless of container.

Tools used
----------
- mkvtoolnix (mkvmerge + mkvextract) -> to EXTRACT the subtitle track from
                        MKV (text formats such as SRT/ASS are pulled 1:1 by
                        mkvextract, losslessly).
- ffmpeg (OPTIONAL, only for --audio-mode replace/combine) -> decoding the
                        audio track (AC-3/AAC/DTS/...) to raw PCM, since that
                        is not doable in pure Python without an external binary.
- numpy              -> pip package, for FFT correlation and VAD (speech detection).
- colorama (OPTIONAL) -> colored output in the Windows CLI. Without it the
                        script works the same, just without colors (no crash).
- EVERYTHING ELSE (SRT parsing, offset search, timing computation, speech
  detection) is pure Python written in this script - NO alass.

Installation on Windows 10
--------------------------
1) Python 3.9+  (https://www.python.org/downloads/)
2) pip install numpy colorama charset-normalizer deep-translator
   (the complete dependency list incl. optional ones and installation on
    Linux/AlmaLinux is in `python video_tool.py --help`)
3) MKVToolNix - you DON'T have to handle it manually. The script searches in
   this order (downloading is the LAST RESORT):
     1. PATH / --mkvmerge,--mkvextract
     2. typical install paths (C:\\Program Files\\MKVToolNix etc.)
     3. the video's directory, current directory, script directory, cache .mkvtoolnix
     4. only when none of the above is found: it downloads the current portable
        version (.7z) from mkvtoolnix.download and unpacks it into .mkvtoolnix
        next to the script.
   Unpacking the .7z additionally needs either the `py7zr` package (pip install
   py7zr, recommended - purely via pip), or an external 7z/7za in PATH. Without
   one of those two the downloaded archive won't unpack - in that case install
   MKVToolNix the classic way via its installer.
   Auto-download can be turned off with --no-mkvtoolnix-download, or provide
   your own path via --mkvmerge / --mkvextract.
4) ffmpeg (only for --audio-mode replace/combine) - you DON'T have to handle it
   manually: if the script doesn't find it in PATH nor in the cache folder
   ".ffmpeg" next to itself, it downloads it automatically and unpacks it into
   ".ffmpeg".
   Auto-download can be turned off with --no-ffmpeg-download, or provide your
   own path via --ffmpeg.

How the algorithm works
-----------------------
1. A reference timeline is obtained - depending on --audio-mode:
   - "off" (default): reference SRT extracted from the MKV (subtitle track).
   - "replace": the AUDIO track from MKV/MP4 - speech detection (VAD) finds
     segments where someone is speaking, and those are used as reference
     "anchors". Needs no reference subtitles at all.
   - "combine": both at once - subtitle anchors and speech segments are merged
     into a single reference timeline for maximum robustness and accuracy.
2. The reference timeline and the subtitles being fixed are converted into a
   binary "signal" over time (when "something happens" - subtitle/speech - and
   when not).
3. Using cross-correlation (FFT), the best overall time shift between the two
   signals is found - this handles even a large initial drift.
4. Around this rough shift, the nearest subtitles from the set being fixed are
   matched to individual anchors from the reference timeline, and from these
   pairs a precise linear transform is computed (shift + speed/FPS change),
   robustly - outlier/unmatched pairs are gradually discarded.
5. This transform (a*t + b) is applied to ALL times in the subtitles being
   fixed and the resulting .srt is saved. The subtitle text is not changed in
   any way, ONLY the timing is adjusted.

Speech detection (VAD) is a simple energy method (RMS loudness over 30ms
frames, adaptive thresholding via percentile) - it is not an ML model like
tools such as ffsubsync, but for rough/fine timing alignment by dialog it
usually works well. Quiet music/effects without dialog can occasionally
confuse the VAD - which is why a combined mode is also available.

Two timing-computation methods (--method switch)
------------------------------------------------
- "affine" = the procedure described above: ONE global relation
  new_time = a*time + b (shift + optional speed/FPS change). Language
  independent (timing only), works even across different languages and from
  audio alone (VAD). It cannot, however, fix PIECEWISE desync (when different
  parts of an episode need a different shift).
- "warp" = content-based synchronization BY SENTENCE. It matches specific
  Czech/foreign sentences between the subtitles being fixed and the reference
  (by text - character 3-grams + shared names/numbers), builds a PIECEWISE
  linear time map from confident pairs ("anchors") and moves each subtitle
  accordingly. This fixes even block/gradual desync and slight drift within
  individual scenes. It requires a TEXT reference track (not from audio alone).
- "auto" (default) = "combo" when a text reference track is available and
  enough reliable anchors are found; otherwise it safely falls back to "affine".
- "combo" = affine pre-alignment + warp fine-tuning by sentence. The most
  robust: the affine phase fixes global/speed (even with few text anchors),
  warp then fine-tunes piecewise where it has confident anchors. Recommended
  for accuracy.

Different languages (--translate)
---------------------------------
Different languages (--translate) + language detection FROM CONTENT
------------------------------------------------------------------
The script can ACTUALLY detect the language from the subtitle content (not by
extensions/tags; via 'langdetect' if installed, otherwise a built-in detector
for cs/sk/pl/en/de/fr/es/it/pt/nl/ru/uk/hu...). In the interactive --auto and
--auto-all modes it therefore recognizes on its own whether the subtitles being
fixed and the reference are in different languages, and only then offers
translation.

The "warp" method compares TEXT, so it works best when both subtitle sets are
in the same language (two translations of the same thing). When they are in
DIFFERENT languages, enable --translate google (online) or --translate argos
(offline): both sides are translated into a common language (--pivot-lang,
default 'en') FOR MATCHING ONLY, and only the translations are compared. The
output text is not translated. Translations are cached to disk. Without
--translate, different languages are safely computed via the affine method
(which is language independent, it just can't do piecewise desync).

Both methods change ONLY the timing, the subtitle text is never changed.

Usage
-----
    python video_tool.py --list-tracks video.mkv

    python video_tool.py video.mkv subtitles_cz.srt output_cz_synced.srt

    python video_tool.py video.mkv subtitles_cz.srt output.srt --ref-lang eng

    python video_tool.py video.mkv subtitles_cz.srt output.srt --track-id 2

    # sync by the audio track only (speech detection), without subtitle reference
    python video_tool.py video.mkv subtitles_cz.srt output.srt --audio-mode replace

    # combine subtitle reference + audio analysis for max accuracy
    python video_tool.py video.mkv subtitles_cz.srt output.srt --audio-mode combine --audio-lang cze
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import wave
from collections import Counter, defaultdict
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("[ERROR] Missing the numpy package. Install it: pip install numpy", file=sys.stderr)
    sys.exit(1)

try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
except ImportError:
    class _NoColor:
        def __getattr__(self, _name):
            return ""
    Fore = _NoColor()
    Style = _NoColor()


# ======================================================================
# USER-CONFIGURABLE SETTINGS
# ----------------------------------------------------------------------
# External-tool locations and download sources - edit these to point the
# script at your own install paths / mirrors. On Windows the tools are
# looked up in PATH first, then in the install folders below, and only as
# a last resort downloaded. All of these can also be overridden at runtime
# via --ffmpeg / --mkvmerge / --mkvextract / --ffmpeg-url.
# ======================================================================

# Binary names (or full paths). Bare names => looked up on PATH.
FFMPEG = "ffmpeg"
MKVMERGE = "mkvmerge"
MKVEXTRACT = "mkvextract"

# Typical Windows install folders searched BEFORE any download (a "bin"
# subfolder is searched too). Add your own paths here if needed.
FFMPEG_PROGRAM_FILES_DIRS = [
    r"C:\Program Files\ffmpeg",
    r"C:\Program Files (x86)\ffmpeg",
]
MKV_PROGRAM_FILES_DIRS = [
    r"C:\Program Files\MKVToolNix",
    r"C:\Program Files (x86)\MKVToolNix",
]

# Where to download the Windows ffmpeg build from (Windows only). Tried IN
# ORDER; the first working one is used. Feel free to edit/add more. Can be
# overridden at runtime via --ffmpeg-url (that one is then tried first).
FFMPEG_DOWNLOAD_URLS = [
    "http://nas.falco81.net/ffmpeg-release-essentials.zip",              # default (local NAS)
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",  # fallback
]
FFMPEG_DOWNLOAD_URL = FFMPEG_DOWNLOAD_URLS[0]   # backwards compatibility

# MKVToolNix: a primary DIRECT .7z download (tried first), with a fallback to the
# official download index below (which is parsed for the versioned .7z link).
MKVTOOLNIX_DOWNLOAD_URL = "https://nas.falco81.net/mkvtoolnix.7z"
MKVTOOLNIX_DOWNLOAD_PAGE = "https://mkvtoolnix.download/downloads.html"

# User-Agent used for all HTTP downloads.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Where to load/save the settings file (config + saved presets). EMPTY = next to
# the script (the current default behaviour). May be a FOLDER or a full .json path.
# Supported everywhere: '~' and environment variables (%APPDATA%, $HOME, ...).
# Windows: drive paths (Z:\Configs), UNC network paths (\\server\share\dir), and
# mapped network drives all work. Linux: normal paths incl. an SMB/CIFS mount
# (e.g. /mnt/nas/video_tool). Priority: --config-file (one-off) > this constant >
# the 'video_tool.configpath' pointer file (set via --config) > default.
CONFIG_STORE_PATH = r""


# ----------------------------------------------------------------------
# Helper functions / colored output (Windows CLI friendly via colorama)
# ----------------------------------------------------------------------

def log_info(msg: str):
    print(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")


def log_warn(msg: str):
    print(f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL} {msg}")


def log_done(msg: str):
    print(f"{Fore.GREEN}[DONE]{Style.RESET_ALL} {msg}")


def die(msg: str, code: int = 1):
    print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}", file=sys.stderr)
    sys.exit(code)


def _linux_install_hint(tool):
    """On Linux, advises how to install the tool via the package manager (we do
    not download Windows builds). Not called on Windows."""
    if tool == "ffmpeg":
        log_warn("ffmpeg not found in PATH. Install it via your package manager:")
        log_info("  Debian/Ubuntu:  sudo apt install ffmpeg")
        log_info("  Fedora:         sudo dnf install ffmpeg")
        log_info("  AlmaLinux/Rocky/RHEL (EPEL + CRB + RPM Fusion):")
        log_info("     sudo dnf install epel-release")
        log_info("     sudo dnf config-manager --set-enabled crb")
        log_info("     sudo dnf install --nogpgcheck https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm")
        log_info("     sudo dnf install ffmpeg   (or from EPEL: sudo dnf install ffmpeg-free)")
        log_info("  Arch:           sudo pacman -S ffmpeg")
    else:  # mkvtoolnix
        log_warn("mkvtoolnix (mkvmerge/mkvextract) not found in PATH. Install it via your package manager:")
        log_info("  Debian/Ubuntu:  sudo apt install mkvtoolnix")
        log_info("  Fedora:         sudo dnf install mkvtoolnix")
        log_info("  AlmaLinux/Rocky/RHEL - official bunkus.org repo (find version: rpm -E %rhel):")
        log_info("     EL8:     sudo rpm -Uhv https://mkvtoolnix.download/almalinux/bunkus-org-repo-2-4.noarch.rpm")
        log_info("     EL9/10:  sudo rpm -Uhv https://mkvtoolnix.download/centosstream/bunkus-org-repo-2-4.noarch.rpm")
        log_info("     sudo dnf install mkvtoolnix")
        log_info("  Arch:           sudo pacman -S mkvtoolnix-cli")


def find_tool(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# ----------------------------------------------------------------------
# Working with MKV via mkvmerge/mkvextract
# ----------------------------------------------------------------------

def mkvmerge_tracks(mkvmerge_bin: str, mkv_path: Path, track_type: str):
    """track_type: 'subtitles' or 'audio'."""
    cmd = [mkvmerge_bin, "-J", str(mkv_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    except subprocess.CalledProcessError as e:
        die(f"mkvmerge failed: {e.stderr}")
    data = json.loads(result.stdout)

    out = []
    for track in data.get("tracks", []):
        if track.get("type") == track_type:
            props = track.get("properties", {})
            out.append({
                "id": track["id"],                       # track ID for mkvextract
                "codec": track.get("codec", "?"),
                "lang": props.get("language", "und"),
                "title": props.get("track_name", ""),
            })
    return out


TEXT_CODEC_KEYWORDS = ("SUBRIP", "SRT", "ASS", "SUBSTATIONALPHA", "SSA", "WEBVTT", "USF", "TIMEDTEXT", "MOV_TEXT", "MOVTEXT")


def is_text_codec(codec: str) -> bool:
    c = codec.upper().replace(" ", "")
    return any(k in c for k in TEXT_CODEC_KEYWORDS)


def pick_reference_track(subs, ref_lang, track_id):
    if not subs:
        die("No subtitle track found in the MKV file.")

    if track_id is not None:
        for t in subs:
            if t["id"] == track_id:
                return t
        die(f"Track with ID {track_id} not found. Use --list-tracks.")

    candidates = subs
    if ref_lang:
        matches = [t for t in subs if t["lang"].lower().startswith(ref_lang.lower())]
        if matches:
            candidates = matches
        else:
            log_warn(f"Track with language '{ref_lang}' not found, trying automatic selection.")

    text_tracks = [t for t in candidates if is_text_codec(t["codec"])]
    if text_tracks:
        return text_tracks[0]

    any_text = [t for t in subs if is_text_codec(t["codec"])]
    if any_text:
        return any_text[0]

    die(
        "Only image-based subtitles found (e.g. PGS/VobSub) - those cannot be "
        "extracted as text. You need a text track (SRT/ASS) as reference."
    )


def pick_audio_track(audio_tracks, audio_lang, audio_track_id):
    if not audio_tracks:
        die("No audio track found in the MKV/MP4 file.")

    if audio_track_id is not None:
        for t in audio_tracks:
            if t["id"] == audio_track_id:
                return t
        die(f"Audio track with ID {audio_track_id} not found. Use --list-tracks.")

    if audio_lang:
        matches = [t for t in audio_tracks if t["lang"].lower().startswith(audio_lang.lower())]
        if matches:
            return matches[0]
        log_warn(f"Audio track with language '{audio_lang}' not found, using the first available.")

    return audio_tracks[0]


MKVEXTRACT_CONTAINER_EXTS = {".mkv", ".mka", ".webm"}


def extract_subtitle_to_srt(mkvextract_bin: str, mkv_path: Path, track_id: int, out_srt: Path):
    """For Matroska containers (.mkv/.webm) - mkvextract can only extract from those."""
    cmd = [mkvextract_bin, "tracks", str(mkv_path), f"{track_id}:{out_srt}"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not out_srt.exists() or out_srt.stat().st_size == 0:
        die(f"mkvextract could not extract subtitle track {track_id}:\n{result.stderr[-2000:]}")


def extract_subtitle_via_ffmpeg(ffmpeg_bin: str, video_path: Path, sub_position: int, out_srt: Path):
    """For MP4/MOV etc. - mkvextract does not touch those, so subtitles (typically
    mov_text) are extracted and converted to SRT by ffmpeg. sub_position = index
    among subtitle tracks (0 = first), matching the '0:s:N' specifier."""
    cmd = [ffmpeg_bin, "-y", "-i", str(video_path), "-map", f"0:s:{sub_position}", str(out_srt)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not out_srt.exists() or out_srt.stat().st_size == 0:
        die(f"ffmpeg could not extract the subtitle track:\n{result.stderr[-2000:]}")


# ----------------------------------------------------------------------
# ffmpeg toolkit - PATH / cache ".ffmpeg" next to the script / automatic download
# (the same proven mechanism as in the patreon downloader / mux_subs.py)
# ----------------------------------------------------------------------

# (FFMPEG / FFMPEG_DOWNLOAD_URLS / FFMPEG_DOWNLOAD_URL are defined in the
# USER-CONFIGURABLE SETTINGS section at the top of the script.)
_FFMPEG_URL_OVERRIDE = None


def _ffmpeg_urls():
    """List of URLs to try (an override from --ffmpeg-url goes first)."""
    urls = list(FFMPEG_DOWNLOAD_URLS)
    if _FFMPEG_URL_OVERRIDE:
        urls = [_FFMPEG_URL_OVERRIDE] + [u for u in urls if u != _FFMPEG_URL_OVERRIDE]
    return urls


def _exe(name):
    return name + ".exe" if os.name == "nt" else name


def _resolve_tool(value, name):
    """The value may be: a direct path to the exe, a FOLDER containing the exe
    (also in bin/), or a bare name looked up in PATH. Returns None if nothing
    is found."""
    if not value:
        return None
    exe = _exe(name)
    if os.path.isdir(value):
        for cand in (os.path.join(value, exe), os.path.join(value, "bin", exe)):
            if os.path.isfile(cand):
                return cand
        return None
    if os.path.isfile(value):
        return value
    return shutil.which(value) or shutil.which(exe)


def _try_ff(path):
    """ffmpeg wants '-version' (single dash), mkvmerge/mkvextract want
    '--version' (double dash, GNU style) - we try both variants."""
    if not path:
        return False
    for flag in ("--version", "-version"):
        try:
            subprocess.run([path, flag], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=True)
            return True
        except Exception:
            continue
    return False


def _cache_dir(name=".ffmpeg"):
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else None
    base = os.path.dirname(os.path.abspath(argv0)) if argv0 else os.getcwd()
    return os.path.join(base, name)


def _find_cached(name, search_dirs, max_depth=None):
    """max_depth limits how deep we go (because of searching wide directories
    like Program Files / the video directory - without a limit it could walk
    entire huge trees). None = no limit (cache folders)."""
    exe = _exe(name)
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        base_depth = os.path.abspath(d).rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(d):
            if max_depth is not None:
                depth = os.path.abspath(root).rstrip(os.sep).count(os.sep) - base_depth
                if depth >= max_depth:
                    dirs[:] = []
                    continue
            if exe in files:
                p = os.path.join(root, exe)
                if os.name != "nt":
                    try:
                        os.chmod(p, 0o755)
                    except OSError:
                        pass
                return p
    return None


def _extract_archive(path, dest, url):
    import zipfile
    import tarfile
    lower = (url or path).lower()
    if lower.endswith(".zip") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            t.extractall(dest)
    else:
        raise ValueError("Unknown archive format (expected .zip/.tar.*).")


def _extract_7z(path, dest):
    """Unpacks a .7z. MKVToolNix portable is distributed only as .7z, not .zip,
    so unlike the ffmpeg archive it additionally needs either the py7zr
    package or an external 7z/7za binary (if in PATH)."""
    try:
        import py7zr
        with py7zr.SevenZipFile(path, mode="r") as z:
            z.extractall(path=dest)
        return True
    except ImportError:
        pass
    except Exception as e:
        log_warn(f"Unpacking .7z via py7zr failed: {e}")

    seven_zip = find_tool(["7z", "7z.exe", "7za", "7za.exe"])
    if seven_zip:
        result = subprocess.run([seven_zip, "x", f"-o{dest}", "-y", path],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0:
            return True
        log_warn(f"Unpacking .7z via {seven_zip} failed: {result.stderr[-500:]}")
    return False


def _download_to_file(url, dest_path, label):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as r, open(dest_path, "wb") as f:
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {Fore.CYAN}{done * 100 // total:3d}%{Style.RESET_ALL}  "
                      f"{label}: {done // 1048576} / {total // 1048576} MB", end="")
        print()


def _download_ffmpeg(urls):
    """Tries to download+unpack ffmpeg from the URLs in order; the first success
    wins. Accepts a list or a single URL (backwards compatibility)."""
    if isinstance(urls, str):
        urls = [urls]
    cache = _cache_dir(".ffmpeg")
    os.makedirs(cache, exist_ok=True)
    tmp = os.path.join(cache, "ffmpeg_download.tmp")
    last_err = None
    for i, url in enumerate(urls, 1):
        try:
            log_info(f"ffmpeg not found; downloading from {url}" + (f" (source {i}/{len(urls)})" if len(urls) > 1 else ""))
            _download_to_file(url, tmp, "ffmpeg")
            log_info("Rozbaluji ffmpeg ...")
            _extract_archive(tmp, cache, url)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return True
        except Exception as e:
            last_err = e
            log_warn(f"Source failed ({url}): {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if i < len(urls):
                log_info("Trying next source...")
    if last_err:
        raise last_err
    return False


def ensure_ffmpeg(target_dir, allow_download):
    """Finds ffmpeg: PATH -> --ffmpeg/FFMPEG override -> cache .ffmpeg (no
    depth limit) -> the Windows install folders (FFMPEG_PROGRAM_FILES_DIRS) and
    the video directory/cwd (limited depth, so it doesn't walk entire huge trees)
    -> (only as a last resort, if allowed) downloads and unpacks from the
    configured URLs (FFMPEG_DOWNLOAD_URLS / --ffmpeg-url, in order with fallback).
    Windows only."""
    cache_dirs = [_cache_dir(".ffmpeg"), os.path.join(target_dir, ".ffmpeg"),
                  os.path.join(os.getcwd(), ".ffmpeg")]
    broad_dirs = FFMPEG_PROGRAM_FILES_DIRS + [target_dir, os.getcwd()]
    ff = _resolve_tool(FFMPEG, "ffmpeg")
    if not _try_ff(ff):
        ff = _find_cached("ffmpeg", cache_dirs)
        if not _try_ff(ff):
            ff = _find_cached("ffmpeg", broad_dirs, max_depth=3)
        if not _try_ff(ff):
            ff = None
    if ff is None and allow_download and _ffmpeg_urls() and os.name == "nt":
        try:
            _download_ffmpeg(_ffmpeg_urls())
        except Exception as e:
            log_warn(f"ffmpeg download failed (all sources): {e}")
        ff = _find_cached("ffmpeg", cache_dirs)
        if not _try_ff(ff):
            ff = None
    elif ff is None and os.name != "nt":
        _linux_install_hint("ffmpeg")
    return ff


# ----------------------------------------------------------------------
# mkvtoolnix toolkit - same principle as ffmpeg above, plus slightly more
# complex logic: the portable package is .7z (not .zip) and the download URL
# contains a version number, so it must first be read from the downloads page.
# ----------------------------------------------------------------------

# (MKVMERGE / MKVEXTRACT / MKVTOOLNIX_DOWNLOAD_PAGE are defined in the
# USER-CONFIGURABLE SETTINGS section at the top of the script.)


def _resolve_mkvtoolnix_url():
    """The downloads page contains versioned links (e.g. .../99.0/mkvtoolnix-
    -64-bit-99.0.7z) - there is no fixed 'latest' URL, so we must first read
    it out of the HTML."""
    import urllib.request
    req = urllib.request.Request(MKVTOOLNIX_DOWNLOAD_PAGE, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="ignore")

    m = re.search(r"https://mkvtoolnix\.download/windows/releases/[\d.]+/mkvtoolnix-64-bit-[\d.]+\.7z", html)
    if m:
        return m.group(0)

    m = re.search(r"windows/releases/([\d.]+)/mkvtoolnix-64-bit-([\d.]+)\.7z", html)
    if m:
        return f"https://mkvtoolnix.download/windows/releases/{m.group(1)}/mkvtoolnix-64-bit-{m.group(2)}.7z"

    return None


def _download_mkvtoolnix(url):
    cache = _cache_dir(".mkvtoolnix")
    os.makedirs(cache, exist_ok=True)
    log_info(f"mkvtoolnix not found; downloading the portable version from {url}")
    tmp = os.path.join(cache, "mkvtoolnix_download.tmp")
    _download_to_file(url, tmp, "mkvtoolnix")
    log_info("Rozbaluji mkvtoolnix (.7z) ...")
    ok = _extract_7z(tmp, cache)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if not ok:
        raise RuntimeError(
            "Could not unpack the .7z archive. Install the py7zr package "
            "(pip install py7zr) and try again, or install MKVToolNix "
            "download/install manually."
        )


# (MKV_PROGRAM_FILES_DIRS is defined in the USER-CONFIGURABLE SETTINGS
# section at the top of the script.)


def ensure_mkvtoolnix(target_dir, allow_download):
    """Finds mkvmerge+mkvextract, IN THIS ORDER (downloading is the LAST RESORT):
    1) PATH / --mkvmerge,--mkvextract override
    2) typical install paths (Program Files\\MKVToolNix)
    3) the video directory, current directory, script directory, cache .mkvtoolnix
       (in case it was left there from before)
    4) only when NONE of the above is found and it is allowed: downloads the
       portable .7z - first from the direct MKVTOOLNIX_DOWNLOAD_URL (e.g. the NAS),
       then, as a fallback, the versioned build parsed off mkvtoolnix.download -
       and unpacks it into .mkvtoolnix next to the script.
    Returns (mkvmerge, mkvextract); either item may be None."""
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else os.getcwd()
    cache = _cache_dir(".mkvtoolnix")

    def _try_path_override(value, name):
        p = _resolve_tool(value, name)
        return p if _try_ff(p) else None

    # 1) PATH / explicit override
    mm = _try_path_override(MKVMERGE, "mkvmerge")
    me = _try_path_override(MKVEXTRACT, "mkvextract")

    # 2) + 3) wider directory search - BEFORE any downloading
    if mm is None or me is None:
        broad_dirs = MKV_PROGRAM_FILES_DIRS + [target_dir, os.getcwd(), script_dir, cache]
        if mm is None:
            mm = _find_cached("mkvmerge", broad_dirs, max_depth=3)
            mm = mm if _try_ff(mm) else None
        if me is None:
            me = _find_cached("mkvextract", broad_dirs, max_depth=3)
            me = me if _try_ff(me) else None

    # 4) only now, as a last resort, downloading (Windows only - the portable .7z
    #    is a Windows build; on Linux we advise installing via the package manager)
    if (mm is None or me is None) and allow_download and os.name == "nt":
        ok = False
        # 1) primary DIRECT source(s) - e.g. the NAS .7z
        for u in ([MKVTOOLNIX_DOWNLOAD_URL] if MKVTOOLNIX_DOWNLOAD_URL else []):
            try:
                _download_mkvtoolnix(u)
                ok = True
                break
            except Exception as e:
                log_warn(f"mkvtoolnix source failed ({u}): {e}")
        # 2) fallback: parse the official download page for the versioned .7z
        if not ok:
            try:
                url = _resolve_mkvtoolnix_url()
                if not url:
                    raise RuntimeError("Could not find a link to the portable version on mkvtoolnix.download.")
                _download_mkvtoolnix(url)
            except Exception as e:
                log_warn(f"Downloading/unpacking mkvtoolnix failed: {e}")
        if mm is None:
            mm = _find_cached("mkvmerge", [cache])
            mm = mm if _try_ff(mm) else None
        if me is None:
            me = _find_cached("mkvextract", [cache])
            me = me if _try_ff(me) else None
    elif (mm is None or me is None) and os.name != "nt":
        _linux_install_hint("mkvtoolnix")

    return mm, me


def extract_audio_wav(ffmpeg_bin: str, mkv_path: Path, audio_position: int, out_wav: Path, sample_rate: int = 16000):
    """audio_position = index of the audio track among audio tracks (0 = first audio track in the file),
    matching the ffmpeg specifier '0:a:N'."""
    cmd = [
        ffmpeg_bin, "-y", "-i", str(mkv_path),
        "-map", f"0:a:{audio_position}",
        "-ac", "1", "-ar", str(sample_rate),
        "-f", "wav", str(out_wav),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size == 0:
        die(f"ffmpeg could not extract/decode the audio track:\n{result.stderr[-2000:]}")


def read_wav_mono(path: Path):
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
        sampwidth = wf.getsampwidth()
    if sampwidth != 2:
        die(f"Expected 16-bit WAV, found {sampwidth * 8}-bit (unexpected ffmpeg output).")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sr


def detect_speech_events(samples: "np.ndarray", sr: int, frame_ms: float = 30.0,
                          energy_percentile: float = 55.0, min_speech_ms: float = 200.0,
                          max_gap_ms: float = 300.0):
    """
    Simple energy-based VAD (speech detection):
    - splits the signal into frames of frame_ms,
    - computes the RMS loudness (in dB) of each frame,
    - everything above an adaptive threshold (loudness percentile of the whole
      track) = "speech",
    - short gaps between speech are merged, too short/random segments discarded.
    Returns events in the same format as subtitles: {"start", "end", "text": ""}.
    """
    frame_len = max(1, int(sr * frame_ms / 1000.0))
    n_frames = len(samples) // frame_len
    if n_frames < 2:
        die("The audio track is too short or empty for VAD analysis.")

    frames = samples[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-9)

    threshold = np.percentile(db, energy_percentile)
    active = db > threshold

    frame_dur = frame_len / sr
    raw_events = []
    i = 0
    while i < len(active):
        if active[i]:
            start = i
            while i < len(active) and active[i]:
                i += 1
            raw_events.append([start * frame_dur, i * frame_dur])
        else:
            i += 1

    max_gap = max_gap_ms / 1000.0
    merged = []
    for ev in raw_events:
        if merged and ev[0] - merged[-1][1] <= max_gap:
            merged[-1][1] = ev[1]
        else:
            merged.append(ev)

    min_dur = min_speech_ms / 1000.0
    merged = [m for m in merged if (m[1] - m[0]) >= min_dur]

    if len(merged) < 2:
        die(
            "VAD detected too few speech segments for reliable synchronization. "
            "Try a different --vad-percentile, another audio track, or use a subtitle reference."
        )

    return [{"start": s, "end": e, "text": ""} for s, e in merged]


# ----------------------------------------------------------------------
# Parsing / writing SRT (pure Python, no external library)
# ----------------------------------------------------------------------

TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})")
BLOCK_RE = re.compile(
    r"(\d+:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[.,]\d{3})"
)


def time_to_seconds(s: str) -> float:
    m = TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"Invalid time format: {s}")
    h, mi, sec, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + sec + ms / 1000.0


def seconds_to_srt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    s = int(t)
    ms = int(round((t - s) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _decode_subtitle_bytes(data):
    """Sensibly decodes subtitles of unknown encoding: BOM, plain UTF-8, UTF-16
    without BOM, and then by the pattern of high bytes distinguishes European
    (cp1250) vs Asian (Big5/GBK/EUC-KR) encodings - for Asian ones it uses the
    detector when available."""
    if not data:
        return ""
    ts = re.compile(r"\d\d:\d\d:\d\d[,.]\d{3}")
    # 1) BOM
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    if data[:2] == b"\xff\xfe" or data[:2] == b"\xfe\xff":
        try:
            return data.decode("utf-16", errors="replace")
        except Exception:
            pass
    # 2) plain UTF-8 with timestamps (the most common case)
    try:
        t = data.decode("utf-8")
        if ts.search(t):
            return t
    except Exception:
        pass
    # 3) UTF-16 without BOM (lots of null bytes)
    if data.count(b"\x00") > len(data) // 4:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                t = data.decode(enc)
                if ts.search(t):
                    return t
            except Exception:
                pass
    # 4) high bytes: in runs (CJK) vs isolated (European single-byte)
    hi = [i for i, b in enumerate(data) if b >= 0x80]
    cjk_like = False
    if hi:
        s = set(hi)
        adj = sum(1 for i in hi if (i - 1) in s or (i + 1) in s)
        cjk_like = (adj / len(hi)) > 0.6
    if cjk_like:
        for libname in ("charset_normalizer", "chardet"):
            try:
                lib = __import__(libname)
                if libname == "charset_normalizer":
                    b = lib.from_bytes(data).best()
                    if b is not None and ts.search(str(b)):
                        return str(b)
                else:
                    enc = (lib.detect(data) or {}).get("encoding")
                    if enc:
                        t = data.decode(enc, errors="replace")
                        if ts.search(t):
                            return t
            except Exception:
                pass
        order = ("gb18030", "big5", "euc-kr", "shift_jis", "cp1250", "cp1252", "latin-1")
    else:
        order = ("cp1250", "cp1252", "latin-1", "gb18030", "big5")
    best, best_score = None, -1
    for enc in order:
        try:
            txt = data.decode(enc)
        except Exception:
            continue
        score = len(ts.findall(txt))
        if score > best_score:
            best, best_score = txt, score
    if best is None or best_score <= 0:
        best = data.decode("utf-8", errors="replace")
    return best


def parse_srt(path: Path, strict=True):
    raw = _decode_subtitle_bytes(Path(path).read_bytes())
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    events = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        time_line_idx = None
        for i, line in enumerate(lines):
            if BLOCK_RE.search(line):
                time_line_idx = i
                break
        if time_line_idx is None:
            continue
        m = BLOCK_RE.search(lines[time_line_idx])
        start = time_to_seconds(m.group(1))
        end = time_to_seconds(m.group(2))
        text = "\n".join(lines[time_line_idx + 1:]).strip()
        events.append({"start": start, "end": end, "text": text})
    if not events:
        if strict:
            die(f"Could not load any subtitles from file {path} (bad format?).")
        return []
    return events


def write_srt(events, path: Path):
    with path.open("w", encoding="utf-8") as f:
        for i, ev in enumerate(events, start=1):
            f.write(f"{i}\n")
            f.write(f"{seconds_to_srt_time(ev['start'])} --> {seconds_to_srt_time(ev['end'])}\n")
            f.write(f"{ev['text']}\n\n")


# ----------------------------------------------------------------------
# Synchronization core - custom Python algorithm (no alass)
# ----------------------------------------------------------------------

def build_signal(events, resolution: float, duration: float):
    n = int(duration / resolution) + 1
    sig = np.zeros(n, dtype=np.float32)
    for ev in events:
        a = max(0, int(ev["start"] / resolution))
        b = min(n, int(ev["end"] / resolution) + 1)
        if b > a:
            sig[a:b] = 1.0
    return sig


def coarse_offset(ref_events, target_events, resolution=0.1, max_shift=120.0):
    """Finds the best overall time shift using FFT cross-correlation."""
    duration = max(
        max((e["end"] for e in ref_events), default=0.0),
        max((e["end"] for e in target_events), default=0.0),
    ) + max_shift + 1.0

    ref_sig = build_signal(ref_events, resolution, duration)
    tgt_sig = build_signal(target_events, resolution, duration)

    n = 1
    total_len = len(ref_sig) + len(tgt_sig)
    while n < total_len:
        n *= 2

    fft_ref = np.fft.rfft(ref_sig, n=n)
    fft_tgt = np.fft.rfft(tgt_sig, n=n)
    corr = np.fft.irfft(fft_ref * np.conj(fft_tgt), n=n)

    max_shift_samples = int(max_shift / resolution)
    shifts = np.concatenate([np.arange(0, max_shift_samples + 1),
                              np.arange(n - max_shift_samples, n)])
    shifts = shifts[shifts < n]
    best_k = shifts[np.argmax(corr[shifts])]
    if best_k > n // 2:
        best_k -= n
    return best_k * resolution  # positive = target is LATER than ref -> needs subtracting


def refine_affine(ref_events, target_events, coarse_shift, tolerance=1.5, iterations=5):
    """
    Finds the best linear transform target_time -> ref_time by
    target_corrected = scale * target_original + offset,
    matching the nearest subtitle start times and iteratively
    discarding outlier pairs (simple robust regression).
    """
    ref_starts = np.array([e["start"] for e in ref_events])
    tgt_starts = np.array([e["start"] for e in target_events])

    scale, offset = 1.0, coarse_shift
    pairs_ref, pairs_tgt = [], []

    for _ in range(iterations):
        corrected = scale * tgt_starts + offset
        pairs_ref, pairs_tgt = [], []
        idx_sorted = np.argsort(corrected)
        sorted_corrected = corrected[idx_sorted]
        for r in ref_starts:
            j = np.searchsorted(sorted_corrected, r)
            best_j, best_d = None, None
            for cand in (j - 1, j):
                if 0 <= cand < len(sorted_corrected):
                    d = abs(sorted_corrected[cand] - r)
                    if best_d is None or d < best_d:
                        best_d, best_j = d, cand
            if best_j is not None and best_d <= tolerance:
                orig_idx = idx_sorted[best_j]
                pairs_ref.append(r)
                pairs_tgt.append(tgt_starts[orig_idx])

        if len(pairs_ref) < 2:
            break

        x = np.array(pairs_tgt)
        y = np.array(pairs_ref)
        A = np.vstack([x, np.ones_like(x)]).T
        new_scale, new_offset = np.linalg.lstsq(A, y, rcond=None)[0]

        resid = np.abs((new_scale * x + new_offset) - y)
        med = np.median(resid) + 1e-6
        keep = resid <= max(tolerance, med * 4)
        if keep.sum() >= 2:
            x2, y2 = x[keep], y[keep]
            A2 = np.vstack([x2, np.ones_like(x2)]).T
            new_scale, new_offset = np.linalg.lstsq(A2, y2, rcond=None)[0]

        scale, offset = float(new_scale), float(new_offset)

    return scale, offset, len(pairs_ref)


def apply_transform(events, scale, offset):
    out = []
    for ev in events:
        out.append({
            "start": scale * ev["start"] + offset,
            "end": scale * ev["end"] + offset,
            "text": ev["text"],
        })
    return out


# ----------------------------------------------------------------------
# Content-based "warp" synchronization - by individual sentences (--method warp)
# ----------------------------------------------------------------------
#
# The affine method above can only do ONE global relation a*t+b for the whole
# episode. That fails when the desync is PIECEWISE (different parts need a
# different shift - typically when ad/block splitting differs, or when the
# original subtitles "drift" only in some scenes). This method instead:
#   1. Matches specific SENTENCES between the fixed and reference subtitles
#      (by text - character 3-grams + shared names/numbers, language robust).
#   2. Trusts ONLY confident, distinctive and unambiguous pairs ("anchors").
#      Short generic lines ("Yes.", "What?") are never matched by text -
#      they would match anywhere; they are interpolated between anchors.
#   3. From the anchors it builds a piecewise linear time map and moves ALL
#      subtitles by it; additionally it gently "snaps" lines to the matching
#      reference sentence, but only within a window of a few seconds (it cannot
#      teleport).
# The text is NEVER changed, only the times. It requires a TEXT reference
# (subtitle track) - with --audio-mode replace (VAD only) there is no text, so
# in "auto" the affine method is used automatically.

import bisect as _bisect
import unicodedata as _unicodedata
from collections import Counter as _Counter

CA_DEFAULTS = dict(
    coarse_len=20, coarse_sim=0.55, coarse_margin=0.15,   # coarse global phase
    band=45.0, min_len=12, min_sim=0.50, margin=0.10,     # fine phase (s)
    snap_win=3.0, snap_sim=0.30,                          # local snapping (s)
    min_dur=0.35,                                         # min subtitle duration (s)
)


def _ca_normalize(text):
    s = "".join(c for c in _unicodedata.normalize("NFKD", text)
                if not _unicodedata.combining(c)).lower()
    s = re.sub(r"[^0-9a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ca_grams(s, n=3):
    s = "\u2581" + s.replace(" ", "\u2581") + "\u2581"
    return _Counter(s[i:i + n] for i in range(len(s) - n + 1))


def _ca_distinctive(norm_text):
    return set(w for w in norm_text.split() if len(w) >= 4) | \
           set(re.findall(r"\d+", norm_text))


def _ca_prepare(events, sim_texts=None):
    """Returns copies of the events with precomputed fields for text comparison.
    sim_texts (optional) = a parallel list of texts used INSTEAD of ev['text']
    for the SIMILARITY COMPUTATION (e.g. machine translation into a common
    language). The output 'text' (and thus the saved subtitles) always stays
    the original."""
    out = []
    for idx, ev in enumerate(events):
        sim_src = sim_texts[idx] if sim_texts is not None else ev["text"]
        n = _ca_normalize(sim_src)
        g = _ca_grams(n)
        out.append({
            "start": ev["start"], "end": ev["end"], "text": ev["text"],
            "_n": n, "_g": g,
            "_gn": (sum(v * v for v in g.values()) ** 0.5) or 1.0,
            "_tk": _ca_distinctive(n),
            "_music": ("\u266a" in ev["text"] or "\u266b" in ev["text"] or "\u2669" in ev["text"]),
        })
    return out


def _ca_sim(a, b):
    g1, g2 = a["_g"], b["_g"]
    if len(g1) > len(g2):
        g1, g2 = g2, g1
    dot = sum(c * g2[k] for k, c in g1.items() if k in g2)
    cos = dot / (a["_gn"] * b["_gn"])
    return min(1.0, cos + min(0.25, 0.08 * len(a["_tk"] & b["_tk"])))


def _ca_combined(texts):
    n = " ".join(_ca_normalize(t) for t in texts)
    g = _ca_grams(n)
    return {"_n": n, "_g": g, "_gn": (sum(v * v for v in g.values()) ** 0.5) or 1.0,
            "_tk": _ca_distinctive(n)}


def _ca_lis(pairs):
    """Longest strictly increasing subsequence in j - enforces monotonic anchor order."""
    if not pairs:
        return []
    js = [p[1] for p in pairs]
    tails, tails_idx, parent = [], [], [-1] * len(pairs)
    for k, jv in enumerate(js):
        pos = _bisect.bisect_left(tails, jv)
        if pos == len(tails):
            tails.append(jv); tails_idx.append(k)
        else:
            tails[pos] = jv; tails_idx[pos] = k
        parent[k] = tails_idx[pos - 1] if pos > 0 else -1
    seq, k = [], (tails_idx[-1] if tails_idx else -1)
    while k != -1:
        seq.append(k); k = parent[k]
    seq.reverse()
    return [pairs[k] for k in seq]


def _ca_find_anchors(S, O, band, min_len, min_sim, margin, prior=None):
    """Confident pairs (target_i -> ref_j). prior=warp -> searches around the
    prediction, otherwise globally (coarse phase, handles desync of minutes)."""
    m = len(O)
    ostart = [o["start"] for o in O]
    raw = []
    for i, c in enumerate(S):
        if len(c["_n"]) < min_len:
            continue
        if prior is not None:
            centre = prior(c["start"])
            lo = _bisect.bisect_left(ostart, centre - band)
            hi = _bisect.bisect_right(ostart, centre + band)
            rng = range(lo, hi)
        else:
            rng = range(m)
        best, second = (-1.0, -1), -1.0
        for j in rng:
            sc = _ca_sim(c, O[j])
            if sc > best[0]:
                second = best[0]; best = (sc, j)
            elif sc > second:
                second = sc
        if best[1] >= 0 and best[0] >= min_sim and (best[0] - max(second, 0.0)) >= margin:
            raw.append((i, best[1], best[0]))
    return _ca_lis(raw)


def _ca_build_warp(S, O, anchors):
    pts = sorted({(S[i]["start"], O[j]["start"]) for i, j, _ in anchors})
    if not pts:
        return (lambda t: t)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]

    def warp(t):
        if t <= xs[0]:
            return t + (ys[0] - xs[0])
        if t >= xs[-1]:
            return t + (ys[-1] - xs[-1])
        k = _bisect.bisect_right(xs, t) - 1
        x0, x1, y0, y1 = xs[k], xs[k + 1], ys[k], ys[k + 1]
        return y0 if x1 == x0 else y0 + (t - x0) / (x1 - x0) * (y1 - y0)
    return warp


def _ca_iou(corrected, ref_events):
    """Intersection-over-union of two timelines (0..1, higher = better)."""
    def merge(iv):
        iv = sorted(iv); tot = 0.0; cs = ce = None
        for s, e in iv:
            if cs is None:
                cs, ce = s, e
            elif s <= ce:
                ce = max(ce, e)
            else:
                tot += ce - cs; cs, ce = s, e
        if cs is not None:
            tot += ce - cs
        return tot
    A = sorted((e["start"], e["end"]) for e in corrected)
    B = sorted((e["start"], e["end"]) for e in ref_events)
    inter = 0.0; j = 0
    for s, e in A:
        while j < len(B) and B[j][1] < s:
            j += 1
        k = j
        while k < len(B) and B[k][0] < e:
            inter += max(0.0, min(e, B[k][1]) - max(s, B[k][0])); k += 1
    uni = merge(A) + merge(B) - inter
    return inter / uni if uni else 0.0


def warp_align(ref_events, target_events, cfg=None, ref_sim_texts=None, target_sim_texts=None):
    """
    Content-based (by sentence) synchronization. ref_events must have TEXT.
    ref_sim_texts / target_sim_texts (optional) = texts for the SIMILARITY
    computation (typically machine translation of both sides into a common
    language), so it works even across DIFFERENT languages. The output subtitle
    text is never changed - only the timing.
    Returns (corrected_events, stats).
    """
    cfg = dict(CA_DEFAULTS, **(cfg or {}))
    S = _ca_prepare(target_events, sim_texts=target_sim_texts)
    O = _ca_prepare(ref_events, sim_texts=ref_sim_texts)
    n, m = len(S), len(O)
    ostart = [o["start"] for o in O]

    # 1) coarse global anchors -> coarse map
    coarse = _ca_find_anchors(S, O, band=10 ** 9,
                              min_len=cfg["coarse_len"], min_sim=cfg["coarse_sim"],
                              margin=cfg["coarse_margin"], prior=None)
    warp0 = _ca_build_warp(S, O, coarse)
    # 2) fine anchors around the coarse prediction
    anchors = _ca_find_anchors(S, O, band=cfg["band"],
                               min_len=cfg["min_len"], min_sim=cfg["min_sim"],
                               margin=cfg["margin"], prior=warp0)
    if len(anchors) < len(coarse):
        anchors = coarse
    warp = _ca_build_warp(S, O, anchors)
    amap = {i: j for i, j, _ in anchors}

    out = [None] * n; snapj = [None] * n; used_prev = -1
    for i in range(n):
        if i in amap:
            j = amap[i]; out[i] = [O[j]["start"], O[j]["end"]]; snapj[i] = j; used_prev = j; continue
        tw, twe = warp(S[i]["start"]), warp(S[i]["end"])
        if not S[i]["_music"]:
            lo = _bisect.bisect_left(ostart, tw - cfg["snap_win"])
            hi = _bisect.bisect_right(ostart, tw + cfg["snap_win"])
            best = (-1.0, -1)
            for j in range(lo, hi):
                sc = _ca_sim(S[i], O[j])
                if sc > best[0]:
                    best = (sc, j)
            if best[1] >= 0 and best[0] >= cfg["snap_sim"] and best[1] != used_prev:
                j = best[1]; out[i] = [O[j]["start"], O[j]["end"]]; snapj[i] = j; used_prev = j; continue
        out[i] = [tw, twe]; snapj[i] = None

    # 3) split runs of multiple target subtitles that fell into ONE ref sentence
    i = 0
    while i < n:
        j = snapj[i]
        if j is None:
            i += 1; continue
        k = i
        while k + 1 < n and snapj[k + 1] == j:
            k += 1
        if k > i:
            s, e = O[j]["start"], O[j]["end"]
            tot = sum(max(1, len(S[t]["text"])) for t in range(i, k + 1)); acc = 0
            for t in range(i, k + 1):
                w = max(1, len(S[t]["text"]))
                st = s + (e - s) * acc / tot; acc += w
                out[t] = [st, s + (e - s) * acc / tot]
        i = k + 1

    # 4) monotonic starts, min duration, no overlaps
    MIN = cfg["min_dur"]
    for i in range(n):
        if i > 0 and out[i][0] < out[i - 1][0]:
            out[i][0] = out[i - 1][0]
        if out[i][1] < out[i][0] + MIN:
            out[i][1] = out[i][0] + MIN
    for i in range(n - 1):
        if out[i][1] > out[i + 1][0]:
            if out[i + 1][0] > out[i][0] + MIN:
                out[i][1] = out[i + 1][0]
            else:
                mid = (out[i][0] + out[i + 1][1]) / 2.0
                out[i][1] = max(out[i][0] + MIN, min(out[i][1], mid))
                if out[i + 1][0] < out[i][1]:
                    out[i + 1][0] = out[i][1]
                if out[i + 1][1] < out[i + 1][0] + MIN:
                    out[i + 1][1] = out[i + 1][0] + MIN

    corrected = [{"start": out[i][0], "end": out[i][1], "text": target_events[i]["text"]}
                 for i in range(n)]
    # guarantee: text and order unchanged
    assert [e["text"] for e in corrected] == [e["text"] for e in target_events], \
        "warp_align: the text must not change!"
    stats = {"anchors": len(anchors), "iou": _ca_iou(corrected, ref_events)}
    return corrected, stats


# ----------------------------------------------------------------------
# Transplanting a PROFESSIONAL translation onto machine-timed subtitles
# (same show, same language, different translation + different episode/time
# splitting). Replaces the target subtitles' text with the most similar
# professional text, but KEEPS the target timing.
# ----------------------------------------------------------------------
def _best_in_range(s, O, lo, hi):
    """The most similar ref subtitle in the range [lo, hi] (linear scan)."""
    bj, bs = -1, 0.0
    for j in range(lo, hi + 1):
        sim = _ca_sim(s, O[j])
        if sim > bs:
            bs, bj = sim, j
    return bj, bs


def _transplant_accept(s, o, sim, min_sim):
    """Accept the replacement? Besides the threshold, it requires at least 1
    shared content word and, for short subtitles, higher confidence (prevents
    confusing it with another similar sentence)."""
    if sim < min_sim:
        return False
    shared = len(s["_tk"] & o["_tk"])
    ntk = len(s["_tk"])
    if shared < 1:
        return False
    if ntk <= 1:
        return sim >= min(0.92, min_sim + 0.28)
    if ntk <= 2:
        return sim >= min_sim + 0.12 and shared >= 1
    if ntk <= 3:
        return sim >= min_sim + 0.05
    return True


def _load_reference_pool(directory, recursive=False, continuous=False):
    """Loads and merges ALL .srt in the directory (sorted) into one event pool.
    continuous=True: shifts each file's times so they follow continuously one
    after another (needed for re-timing - time must be monotonic across the whole
    pool). Returns (pool, files)."""
    files = collect_srts(directory, recursive)
    pool = []
    offset = 0.0
    for f in files:
        try:
            evs = parse_srt(Path(f), strict=False)
        except Exception as e:
            log_warn(f"{os.path.basename(f)}: cannot load ({e}) - skipping.")
            continue
        if continuous and evs:
            base = offset - evs[0]["start"]
            for e in evs:
                pool.append({"start": e["start"] + base, "end": e["end"] + base, "text": e["text"]})
            offset = pool[-1]["end"] + 2.0
        else:
            pool.extend(evs)
    return pool, files


def retime_professional(ref_events, pool_events, min_sim=0.5, margin=25):
    """Re-times PROFESSIONAL subtitles (pool_events, the merged pro pool) to the
    timing of the reference subtitles (ref_events = your machine subtitles with
    good timing). Keeps 100% of the professional text, just gives it your timing.
    Returns (new_events, n_anchors) or (None, n_anchors) when anchors are few."""
    if not ref_events or not pool_events:
        return None, 0
    S = _ca_prepare(ref_events)
    O = _ca_prepare(pool_events)
    M = len(O)
    inv = {}
    for j, o in enumerate(O):
        for t in o["_tk"]:
            inv.setdefault(t, []).append(j)
    common_cap = max(30, int(0.01 * M) + 1)

    def best(i):
        s = S[i]
        if not s["_tk"]:
            return -1, 0.0
        cand = set()
        for t in s["_tk"]:
            post = inv.get(t)
            if post and len(post) <= common_cap:
                cand.update(post)
        bj, bs = -1, 0.0
        for j in cand:
            sim = _ca_sim(s, O[j])
            if sim > bs:
                bs, bj = sim, j
        return bj, bs

    N = len(S)
    raw = [best(i) for i in range(N)]
    conf = [(i, raw[i][0]) for i in range(N)
            if raw[i][0] >= 0 and raw[i][1] >= max(0.5, min_sim) and len(S[i]["_tk"]) >= 2]
    anchors = _ca_lis(conf)
    if len(anchors) < 3:
        return None, len(anchors)

    # the slice of the pool belonging to this episode (from first to last anchor + margin)
    j_lo = max(0, anchors[0][1] - margin)
    j_hi = min(M - 1, anchors[-1][1] + margin)

    # time warp: points (pro_time -> your_time) from the anchors
    pts = {}
    for (i, j) in anchors:
        pts[pool_events[j]["start"]] = ref_events[i]["start"]
    xs = sorted(pts)
    ys = [pts[x] for x in xs]

    def warp_time(t):
        if t <= xs[0]:
            slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if len(xs) >= 2 and xs[1] > xs[0] else 1.0
            return ys[0] + (t - xs[0]) * slope
        if t >= xs[-1]:
            slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) if len(xs) >= 2 and xs[-1] > xs[-2] else 1.0
            return ys[-1] + (t - xs[-1]) * slope
        k = _bisect.bisect_right(xs, t) - 1
        x0, x1, y0, y1 = xs[k], xs[k + 1], ys[k], ys[k + 1]
        return y0 if x1 <= x0 else y0 + (t - x0) * (y1 - y0) / (x1 - x0)

    out = []
    for j in range(j_lo, j_hi + 1):
        e = pool_events[j]
        ns = warp_time(e["start"])
        ne = warp_time(e["end"])
        if ne <= ns:
            ne = ns + max(0.3, e["end"] - e["start"])
        out.append({"start": ns, "end": ne, "text": e["text"]})

    out.sort(key=lambda x: x["start"])
    # light overlap prevention (push the end below the next start)
    for k in range(len(out) - 1):
        if out[k]["end"] > out[k + 1]["start"] - 0.02:
            out[k]["end"] = max(out[k]["start"] + 0.3, out[k + 1]["start"] - 0.05)
    return out, len(anchors)


def build_transplanter(ref_events):
    """Prepares the professional subtitle pool ONCE and returns a function that
    fits any target file onto it. Returns (transplant_fn, ref_cue_count)."""
    O = _ca_prepare(ref_events)
    M = len(O)
    inv = {}
    for j, o in enumerate(O):
        for t in o["_tk"]:
            inv.setdefault(t, []).append(j)
    common_cap = max(30, int(0.01 * M) + 1)

    def best_global(S, i):
        s = S[i]
        if not s["_tk"]:
            return -1, 0.0
        cand = set()
        for t in s["_tk"]:
            post = inv.get(t)
            if post and len(post) <= common_cap:
                cand.update(post)
        bj, bs = -1, 0.0
        for j in cand:
            sim = _ca_sim(s, O[j])
            if sim > bs:
                bs, bj = sim, j
        return bj, bs

    def transplant(target_events, min_sim=0.55, window=40):
        if not target_events or M == 0:
            return ([dict(e) for e in target_events], 0, len(target_events))
        S = _ca_prepare(target_events)
        N = len(S)
        raw = [best_global(S, i) for i in range(N)]
        conf = [(i, raw[i][0]) for i in range(N)
                if raw[i][0] >= 0 and raw[i][1] >= max(0.5, min_sim) and len(S[i]["_tk"]) >= 2]
        anchors = _ca_lis(conf)
        result = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in target_events]
        n_repl = 0
        if anchors:
            ai = [a[0] for a in anchors]
            aj = [a[1] for a in anchors]

            def predict(i):
                p = _bisect.bisect_left(ai, i)
                if p == 0:
                    return aj[0] + (i - ai[0])
                if p >= len(ai):
                    return aj[-1] + (i - ai[-1])
                if ai[p] == i:
                    return aj[p]
                i0, i1, j0, j1 = ai[p - 1], ai[p], aj[p - 1], aj[p]
                frac = (i - i0) / (i1 - i0) if i1 > i0 else 0.0
                return int(round(j0 + frac * (j1 - j0)))

            for i in range(N):
                r0 = max(0, min(M - 1, predict(i)))
                lo, hi = max(0, r0 - window), min(M - 1, r0 + window)
                bj, bs = _best_in_range(S[i], O, lo, hi)
                if bj >= 0 and _transplant_accept(S[i], O[bj], bs, min_sim):
                    result[i]["text"] = O[bj]["text"]
                    n_repl += 1
        else:
            for i in range(N):
                bj, bs = raw[i]
                if bj >= 0 and _transplant_accept(S[i], O[bj], bs, min_sim):
                    result[i]["text"] = O[bj]["text"]
                    n_repl += 1
        for i in range(1, N):
            a = result[i - 1]["text"].strip()
            b = result[i]["text"].strip()
            if a and a == b and b != target_events[i]["text"].strip():
                result[i]["text"] = target_events[i]["text"]
                n_repl -= 1
        return result, n_repl, N

    return transplant, M


def transplant_text(target_events, ref_events, min_sim=0.55, window=40):
    """Thin wrapper over build_transplanter (for one-off / testing)."""
    fn, _ = build_transplanter(ref_events)
    return fn(target_events, min_sim, window)





# ----------------------------------------------------------------------
# Machine translation for cross-language matching (for the SIMILARITY computation only)
# ----------------------------------------------------------------------
#
# When the fixed and reference subtitles are in DIFFERENT languages, text
# similarity (character 3-grams) alone is not enough. Solution: both sides are
# translated into a common language for MATCHING purposes (pivot, default
# English) and only the translated texts are compared. The output subtitles are
# NOT translated - only the timing changes, the original text stays. Translations
# are cached to disk (dedup + cache), so subsequent runs are fast and save
# service calls.

_TRANSLATE_CACHE = None
_TRANSLATE_CACHE_PATH = None


def _translate_cache_load(path):
    global _TRANSLATE_CACHE, _TRANSLATE_CACHE_PATH
    _TRANSLATE_CACHE_PATH = path
    if _TRANSLATE_CACHE is not None:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            _TRANSLATE_CACHE = json.load(f)
    except Exception:
        _TRANSLATE_CACHE = {}


def _translate_cache_save():
    if _TRANSLATE_CACHE_PATH is None or _TRANSLATE_CACHE is None:
        return
    try:
        with open(_TRANSLATE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_TRANSLATE_CACHE, f, ensure_ascii=False)
    except Exception as e:
        log_warn(f"Could not save the translation cache: {e}")


class _FatalAPIError(Exception):
    """Unrecoverable API error (e.g. 400/401/403/404) - no point retrying."""


def _http_error_detail(e):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        data = json.loads(body)
        msg = data.get("error", {})
        if isinstance(msg, dict):
            return (msg.get("message") or msg.get("type") or body)[:400]
        return (data.get("message") or body)[:400]
    except Exception:
        return body[:400]


def _call_with_timeout(fn, timeout):
    """Runs fn() with a time limit. Returns (result, error). On timeout it
    returns (None, TimeoutError) and lets the thread finish in the background
    (daemon), so a stuck network request doesn't block the whole run."""
    import threading
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, TimeoutError("timeout")
    return box.get("r"), box.get("e")


# Public key of the Google Translate widget (translatesubtitles.co uses the same).
# It is not a user account - it is "baked into" Google's web translator.
_GOOGLE_TRANSLATE_PA_KEY = "AIzaSyATBXajvzQLTDHEQbcpq0Ihe0vWDHmO520"
_GOOGLE_TRANSLATE_PA_URL = "https://translate-pa.googleapis.com/v1/translateHtml"


def google_translatehtml(texts, target_lang, source_lang="auto", timeout=30):
    """Translates a batch of texts via the modern Google Translate 'translateHtml'
    endpoint (free, no account; the same thing translatesubtitles.co and the
    Google Translate widget do). Returns a list of the same length (None on
    failure of an item), or None on failure of the whole call (so the caller can
    switch to a fallback)."""
    import urllib.request
    import urllib.error
    import html as _html
    escaped = [_html.escape(t if isinstance(t, str) else "", quote=True) for t in texts]
    payload = [[escaped, source_lang, target_lang], "te"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json+protobuf",
        "X-Goog-API-Key": _GOOGLE_TRANSLATE_PA_KEY,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://translatesubtitles.co/",
        "Origin": "https://translatesubtitles.co",
    }
    req = urllib.request.Request(_GOOGLE_TRANSLATE_PA_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    # response: [[<translations>], [<detected languages>]]
    try:
        translations = data[0]
    except (IndexError, KeyError, TypeError):
        return None
    if not isinstance(translations, list) or len(translations) != len(texts):
        return None
    return [_html.unescape(t) if isinstance(t, str) and t else None for t in translations]


def make_translator(engine, pivot_lang, cache_path=None, api_key=None, model=None):
    """Returns a function translate_list(list[str]) -> list[str] that translates
    texts into pivot_lang. Dedup + disk cache. Returns None if the chosen
    translator is not available. Supports 'google'/'argos' (free), 'deepl' (API
    key) and 'claude' (Anthropic API key)."""
    if engine in (None, "off"):
        return None
    if cache_path is None:
        cache_path = os.path.join(_cache_dir(".translate_cache"), f"{engine}_{pivot_lang}.json")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    _translate_cache_load(cache_path)

    backend = None
    if engine == "google":
        # Primarily the modern Google endpoint 'translateHtml' (the same one used
        # widget Google Translate i weby jako translatesubtitles.co): zdarma, bez
        # no account, the whole batch in one request, preserves HTML/entities.
        # If it fails, it falls back to deep-translator (if installed).
        dt = None
        try:
            from deep_translator import GoogleTranslator
            dt = GoogleTranslator(source="auto", target=pivot_lang)
        except Exception:
            dt = None
        _gw_state = {"ok": True}

        def backend(batch):
            if _gw_state["ok"]:
                res = google_translatehtml(batch, pivot_lang)
                if res is not None:
                    return res
                _gw_state["ok"] = False  # endpoint unavailable -> from now on fallback only
                if dt is not None:
                    log_warn("Google 'translateHtml' unavailable - switching to deep-translator.")
                else:
                    log_warn("Google 'translateHtml' unavailable and deep-translator is not "
                             "installed (pip install deep-translator).")
            if dt is None:
                return [None] * len(batch)
            try:
                r = dt.translate_batch(batch)
                if isinstance(r, list) and len(r) == len(batch):
                    return [x if isinstance(x, str) and x else None for x in r]
            except Exception:
                pass
            out = []
            for t in batch:
                try:
                    x = dt.translate(t)
                    out.append(x if isinstance(x, str) and x else None)
                except Exception:
                    out.append(None)
            return out

    elif engine == "deepl":
        if not api_key:
            log_warn("--translate deepl is missing an API key (--deepl-key or the "
                     "DEEPL_API_KEY variable). Continuing without DeepL.")
            return None
        try:
            from deep_translator import DeeplTranslator
        except ImportError:
            log_warn("DeepL is missing the 'deep-translator' package. "
                     "Install: pip install deep-translator. Continuing without DeepL.")
            return None
        try:
            dt = DeeplTranslator(api_key=api_key, source="auto", target=pivot_lang, use_free_api=True)
        except Exception as e:
            log_warn(f"DeepL could not be initialized: {e}")
            return None

        def backend(batch):
            res = []
            for t in batch:
                try:
                    r = dt.translate(t)
                    res.append(r if isinstance(r, str) and r else None)
                except Exception:
                    res.append(None)
            return res

    elif engine == "argos":
        try:
            import argostranslate.translate as _at  # noqa: F401
        except ImportError:
            log_warn("--translate argos is missing the 'argostranslate' package "
                     "(offline translation). Install: pip install argostranslate. "
                     "Continuing without translation.")
            return None
        try:
            import langdetect  # noqa: F401
            from langdetect import detect as _detect
        except ImportError:
            log_warn("Offline translation (argos) also needs 'langdetect' to detect "
                     "the source language: pip install langdetect. Continuing without translation.")
            return None

        def backend(batch):
            import argostranslate.translate as at
            res = []
            for t in batch:
                try:
                    src = _detect(t)
                    res.append(t if src == pivot_lang else at.translate(t, src, pivot_lang))
                except Exception:
                    res.append(None)
            return res

    elif engine == "claude":
        if not api_key:
            log_warn("Translation via Claude is missing an Anthropic API key "
                     "(--anthropic-key or ANTHROPIC_API_KEY). Continuing without Claude.")
            return None
        cl_model = model or "claude-sonnet-4-6"

        def backend(batch):
            res = anthropic_translate_batch(batch, pivot_lang, api_key, cl_model)
            if not isinstance(res, list) or len(res) != len(batch):
                return [None] * len(batch)
            return res

    elif engine == "gemini":
        if not api_key:
            log_warn("Translation via Gemini is missing a Google API key (--gemini-key or "
                     "GEMINI_API_KEY / GOOGLE_API_KEY). Zdarma na aistudio.google.com. "
                     "Continuing without Gemini.")
            return None
        gm_model = model or "gemini-2.5-flash"

        def backend(batch):
            res = gemini_translate_batch(batch, pivot_lang, api_key, gm_model)
            if not isinstance(res, list) or len(res) != len(batch):
                return [None] * len(batch)
            return res
    else:
        return None

    def translate_list(texts):
        key_prefix = f"{engine}|{pivot_lang}|"
        # what is missing in the cache (dedup by unique text)
        uniq = []
        seen = set()
        for t in texts:
            kt = t.strip()
            if kt and (key_prefix + kt) not in _TRANSLATE_CACHE and kt not in seen:
                seen.add(kt); uniq.append(kt)
        if uniq:
            total = len(uniq)
            CH = 25 if engine in ("google", "deepl", "claude", "gemini") else 50
            timeout = 90
            log_info(f"Translating {total} unique lines into '{pivot_lang}' ({engine})... "
                     f"(may take a while; progress below, results cached as they go)")
            done = 0
            failed = 0
            for i in range(0, total, CH):
                chunk = uniq[i:i + CH]
                outs, err = _call_with_timeout(lambda c=chunk: backend(c), timeout)
                if isinstance(err, _FatalAPIError):
                    print()
                    log_warn(f"Translation ({engine}) stopped: {err}")
                    log_warn("Check the API key and model name, or try another translator "
                             "(in the wizard the 'Machine translator' option, or the --translate switch). "
                             "Google is free without a key, Gemini free with a key. "
                             "The remaining lines stay in the original.")
                    failed += (total - done)
                    break
                if outs is None:
                    log_warn(f"\nThe translator did not respond within {timeout}s for block "
                             f"{i // CH + 1} - skipping (lines stay in the original).")
                    outs = [None] * len(chunk)
                if not isinstance(outs, list) or len(outs) != len(chunk):
                    outs = [None] * len(chunk)
                for src, dst in zip(chunk, outs):
                    if isinstance(dst, str) and dst:
                        _TRANSLATE_CACHE[key_prefix + src] = dst   # cache successes only
                    else:
                        failed += 1
                done += len(chunk)
                _translate_cache_save()                      # incrementally -> can interrupt and resume
                pct = int(done * 100 / total)
                print(f"\r  translation: {done}/{total} ({pct}%)" + (f", failed {failed}" if failed else ""),
                      end="", flush=True)
            print()  # newline after the progress
            if failed:
                log_warn(f"Translation failed for {failed}/{total} lines - "
                         "untranslated ones stay in the original. Check key/model/connection, "
                         "or try another translator (DeepL/Claude/Google/argos).")
            else:
                log_info(f"Translated and saved to cache ({done} lines).")
        # assemble the output (missing key = keep the original)
        return [_TRANSLATE_CACHE.get(key_prefix + t.strip(), t) for t in texts]

    return translate_list


def run_alignment(args, ref_events, ref_events_sub, target_events):
    """Selects and runs the synchronization method; returns fixed subtitles (events).
    method:
      'affine' - global a*t+b (language independent, also from audio),
      'warp'   - by sentence (text reference; also fixes piecewise desync),
      'combo'  - affine pre-align + warp fine-tune (most robust),
      'auto'   - combo when there is a text reference and enough anchors, else affine."""
    method = getattr(args, "method", "auto")
    have_text_ref = bool(ref_events_sub)

    def affine_core(targets):
        shift = coarse_offset(ref_events, targets, max_shift=args.max_shift)
        scale, offset, n_matched = refine_affine(ref_events, targets, shift, tolerance=args.tolerance)
        return apply_transform(targets, scale, offset), scale, offset, n_matched

    def do_affine():
        log_info("Method: affine (global shift + speed).")
        log_info("Searching for the coarse time shift (FFT cross-correlation)...")
        corrected, scale, offset, n_matched = affine_core(target_events)
        log_info(f"Resulting transform: new_time = {scale:.6f} * old_time + {offset:+.3f}")
        log_info(f"Matched {n_matched} of {len(ref_events)} reference anchors for refinement")
        if abs(scale - 1.0) > 0.05:
            log_warn("Large speed difference (>5%) - possibly different source framerate, check the result.")
        return corrected

    def _warp_cfg():
        cfg = {}
        if getattr(args, "ca_band", None) is not None:
            cfg["band"] = args.ca_band
        if getattr(args, "ca_snap_win", None) is not None:
            cfg["snap_win"] = args.ca_snap_win
        if getattr(args, "ca_min_sim", None) is not None:
            cfg["min_sim"] = args.ca_min_sim
        return cfg

    def _translations():
        engine = getattr(args, "translate", "off")
        if not engine or engine == "off":
            return None, None
        pivot = getattr(args, "pivot_lang", None) or "en"
        key = model = None
        if engine == "deepl":
            key = getattr(args, "deepl_key", None) or os.environ.get("DEEPL_API_KEY")
        elif engine == "claude":
            key = getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
            model = getattr(args, "anthropic_model", None)
        elif engine == "gemini":
            key = (getattr(args, "gemini_key", None) or os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("GOOGLE_API_KEY"))
            model = getattr(args, "gemini_model", None)
        translator = make_translator(engine, pivot, api_key=key, model=model)
        if translator is None:
            return None, None
        log_info(f"Cross-language mode: comparing via translation into '{pivot}' ({engine}).")
        return (translator([e["text"] for e in ref_events_sub]),
                translator([e["text"] for e in target_events]))

    def warp_on(targets, ref_sim, target_sim, label):
        log_info(label)
        corrected, st = warp_align(ref_events_sub, targets, _warp_cfg(),
                                   ref_sim_texts=ref_sim, target_sim_texts=target_sim)
        log_info(f"Used {st['anchors']} confident anchors out of {len(targets)} subtitles; "
                 f"match with reference (IoU): {st['iou']:.3f}")
        return corrected, st["anchors"]

    if method == "affine":
        return do_affine()

    if method in ("warp", "combo"):
        if not have_text_ref:
            die(f"--method {method} needs a TEXT reference subtitle track, but with "
                "--audio-mode replace there is none. Use --audio-mode off/combine "
                "or --method affine.")
        ref_sim, target_sim = _translations()
        if method == "combo":
            log_info("Method: combined (affine pre-align + warp fine-tune by sentence).")
            log_info("1/2 affine pre-align...")
            pre, scale, offset, _ = affine_core(target_events)
            corrected, n_anchors = warp_on(pre, ref_sim, target_sim, "2/2 warp fine-tune by sentence...")
            if n_anchors < 2:
                log_warn("Too few text anchors - keeping the result of the affine phase.")
                return pre
            return corrected
        corrected, n_anchors = warp_on(target_events, ref_sim, target_sim,
                                       "Method: content-based 'warp' (sentence matching + piecewise linear map).")
        if n_anchors < 2:
            log_warn("Too few text anchors for a reliable 'warp' map - "
                     "switching to the affine method.")
            return do_affine()
        return corrected

    # auto -> combo (affine pre-align + warp), with an affine fallback
    if have_text_ref:
        min_anchors = max(5, len(target_events) // 50)
        ref_sim, target_sim = _translations()
        log_info("Method: auto = combined (affine pre-align + warp).")
        log_info("1/2 affine pre-align...")
        pre, scale, offset, _ = affine_core(target_events)
        corrected, n_anchors = warp_on(pre, ref_sim, target_sim, "2/2 warp fine-tune by sentence...")
        if n_anchors >= min_anchors:
            return corrected
        log_warn(f"Few text anchors ({n_anchors} < {min_anchors}) - the reference translation "
                 "is probably in another language/very different; using the affine result "
                 "(consider --translate google).")
        return pre
    return do_affine()


# Default values for fix_short_durations - as constants so they can be
# referenced from multiple places (the CLI defaults are None now, so we can tell
# the user did NOT set them explicitly - important for --fix-readability below).
DEFAULT_MIN_CPS = 17.0
DEFAULT_MIN_DURATION_FLOOR = 1.0
DEFAULT_MIN_GAP = 0.084
DEFAULT_LINE_OVERHEAD = 0.2  # extra seconds for EACH additional line (the eyes must "jump" to the next line)

# Named reading-speed presets: (cps, floor). Used as a quick choice both for
# --reading-speed on the command line and for the interactive menu in
# --fix-readability - one source of truth for both.
READING_SPEED_PRESETS = {
    "normal":    (17.0, 1.0, "Normal speed"),
    "slow":      (12.0, 1.3, "Slow readers"),
    "very-slow": (9.0, 1.6, "Extremely slow / beginning readers"),
}


def resolve_speed_params(args):
    """Unified cps/floor/gap/line_overhead decision from args:
    --reading-speed provides the base, explicit --min-cps/--min-duration-floor/
    --min-gap/--line-overhead (if given) take precedence over the preset."""
    if args.reading_speed:
        cps, floor, _label = READING_SPEED_PRESETS[args.reading_speed]
    else:
        cps, floor = DEFAULT_MIN_CPS, DEFAULT_MIN_DURATION_FLOOR
    if args.min_cps is not None:
        cps = args.min_cps
    if args.min_duration_floor is not None:
        floor = args.min_duration_floor
    gap = args.min_gap if args.min_gap is not None else DEFAULT_MIN_GAP
    line_overhead = args.line_overhead if args.line_overhead is not None else DEFAULT_LINE_OVERHEAD
    return cps, floor, gap, line_overhead


def fix_short_durations(events, min_cps=DEFAULT_MIN_CPS, min_duration_floor=DEFAULT_MIN_DURATION_FLOOR,
                         min_gap=DEFAULT_MIN_GAP, line_overhead=DEFAULT_LINE_OVERHEAD):
    """
    Extends subtitles that disappear too quickly relative to the text length,
    but ONLY if there is free space for it (a gap to the next subtitle) - it
    never exceeds the gap (minus the safety min_gap before the next subtitle)
    and never extends more than the text actually "asks for". TIMING ALWAYS
    HAS PRIORITY: this function never changes the start of any subtitle and never
    reaches into the next one - that is an unbreakable boundary, regardless of
    which parameters you choose below.

    The target display duration is NOT just "character count / speed" - it also
    accounts for the number of lines (more lines = the eye must additionally jump
    to the next line, so short single-word subtitles never get the same duration
    as a multi-line sentence just because of a shared "floor").

    min_cps           - target reading speed in characters/s (default 17;
                         a smaller value = a longer ideal display)
    min_duration_floor - an absolute floor in seconds regardless of text
    min_gap           - a gap that must be preserved before the next subtitle
    line_overhead     - extra seconds for each line ABOVE the first (default 0.2);
                         a two-line subtitle thus gets +0.2s, a three-line +0.4s
                         on top of the pure character-based computation
    """
    out = [dict(ev) for ev in events]
    n = len(out)
    extended = 0
    for i in range(n):
        char_count = len(re.sub(r"\s+", "", out[i]["text"]))
        if char_count == 0:
            continue
        line_count = max(1, len([l for l in out[i]["text"].split("\n") if l.strip()]))
        ideal_duration = max(
            min_duration_floor,
            char_count / min_cps + (line_count - 1) * line_overhead,
        )
        duration = out[i]["end"] - out[i]["start"]
        if duration >= ideal_duration:
            continue

        gap_to_next = (out[i + 1]["start"] - out[i]["end"]) if i + 1 < n else float("inf")
        available = max(0.0, gap_to_next - min_gap)
        extend_by = min(available, ideal_duration - duration)
        if extend_by > 0.001:
            out[i]["end"] += extend_by
            extended += 1
    return out, extended


# ----------------------------------------------------------------------
# Batch processing (--all) - pairs video<->subtitles in the directory, verifies
# available tracks BEFORE processing, asks interactively about a problem, and
# then processes each pair as a subprocess of this script (no risk that a
# failure of one episode stops / damages the others).
# ----------------------------------------------------------------------

VIDEO_EXTS_BATCH = {".mkv", ".mp4", ".m4v", ".mov", ".webm"}


def collect_videos(directory, recursive):
    videos = []
    if recursive:
        for root, _d, files in os.walk(directory):
            for f in files:
                if Path(f).suffix.lower() in VIDEO_EXTS_BATCH:
                    videos.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            full = os.path.join(directory, f)
            if os.path.isfile(full) and Path(f).suffix.lower() in VIDEO_EXTS_BATCH:
                videos.append(full)
    return sorted(videos)


def collect_srts(directory, recursive):
    srts = []
    if recursive:
        for root, _d, files in os.walk(directory):
            for f in files:
                if f.lower().endswith(".srt"):
                    srts.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            full = os.path.join(directory, f)
            if os.path.isfile(full) and f.lower().endswith(".srt"):
                srts.append(full)
    return sorted(srts)


def _collect_videos_subs(directory, recursive, sub_exts=None):
    """Returns (videos, subtitle_files) in the directory. sub_exts limits which
    subtitle extensions count (default: common subtitle formats)."""
    exts = tuple(e.lower() for e in sub_exts) if sub_exts else (".srt", ".ass", ".ssa", ".vtt", ".sub")
    videos = collect_videos(directory, recursive)
    subs = []
    if recursive:
        for root, _d, files in os.walk(directory):
            for f in files:
                if f.lower().endswith(exts):
                    subs.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            full = os.path.join(directory, f)
            if os.path.isfile(full) and f.lower().endswith(exts):
                subs.append(full)
    return sorted(videos), sorted(subs)


def _srt_lang_tag(srt_path, vstem):
    """'X.S01E01.cs.srt' + vstem='X.S01E01' -> 'cs'. None if there is no tag."""
    sstem = Path(srt_path).stem
    if sstem == vstem:
        return None
    if sstem.startswith(vstem + "."):
        return sstem[len(vstem) + 1:].lower()
    return None


def match_srt_for_video(video_path, srt_candidates, target_lang):
    """Finds .srt files with the same base name as the video (+ an optional
    language/forced tag after the last dot), ONLY in the same directory as the
    video (so --recursive does not pair across different folders)."""
    vstem = Path(video_path).stem
    vdir = os.path.dirname(video_path) or "."
    matches = []
    for s in srt_candidates:
        if (os.path.dirname(s) or ".") != vdir:
            continue
        sstem = Path(s).stem
        if sstem == vstem or sstem.startswith(vstem + "."):
            matches.append(s)
    if target_lang:
        tagged = [s for s in matches if _srt_lang_tag(s, vstem) == target_lang.lower()]
        if tagged:
            matches = tagged
    return matches


# ----------------------------------------------------------------------
# Preset - saving/replaying the interactive wizard's answers (--save/--load)
# ----------------------------------------------------------------------
_PRESET_MODE = None        # None | "save" | "load"
_PRESET_DATA = []          # loaded answers (load)
_PRESET_IDX = 0
_PRESET_REC = []           # recorded answers (save)
_PRESET_CMD = None
_PRESET_KEY = None         # None = default preset (auto-start), otherwise the preset name
_PRESET_SAVED = False
_PRESET_MISS = object()
_PRESET_LABEL = None
_PRESET_DRYRUN = False
_SECRET_HINTS = ("key", "password", "token", "secret")

# ---- Unified store: video_tool.config.json (config + presets) -----------
_STORE_PATH = None
_STORE_FILENAME = "video_tool.config.json"
_STORE_POINTER_NAME = "video_tool.configpath"


def _script_dir():
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else None
    return os.path.dirname(os.path.abspath(argv0)) if argv0 else os.getcwd()


def _is_unc(p):
    return p.startswith("\\\\") or p.startswith("//")


def _normalize_store_path(raw):
    """Turns a user-supplied path into a full path to the settings .json.
    Accepts a folder OR a full .json file path; expands '~' and environment
    variables; keeps UNC (\\\\server\\share) and absolute paths; resolves a
    relative path against the script directory. Returns None for an empty value
    (meaning: use the default)."""
    if raw is None:
        return None
    p = str(raw).strip().strip('"').strip("'")
    if not p:
        return None
    p = os.path.expanduser(os.path.expandvars(p))
    # treat as a folder when it is an existing dir, ends with a separator, or has
    # no ".json" extension -> then append the standard filename
    if os.path.isdir(p) or p.endswith(("\\", "/")) or os.path.splitext(p)[1].lower() != ".json":
        p = os.path.join(p, _STORE_FILENAME)
    if not (os.path.isabs(p) or _is_unc(p)):
        p = os.path.join(_script_dir(), p)
    return p


def _read_store_pointer():
    try:
        with open(os.path.join(_script_dir(), _STORE_POINTER_NAME), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_store_pointer(path):
    """Saves (or clears when empty) the pointer file next to the script."""
    ptr = os.path.join(_script_dir(), _STORE_POINTER_NAME)
    try:
        if path and str(path).strip():
            with open(ptr, "w", encoding="utf-8") as f:
                f.write(str(path).strip())
        elif os.path.exists(ptr):
            os.remove(ptr)
        return True
    except Exception as e:
        log_warn(f"Could not update {_STORE_POINTER_NAME}: {e}")
        return False


def default_store_path():
    return os.path.join(_script_dir(), _STORE_FILENAME)


def current_store_path():
    # 1) explicit one-off override from --config-file (already normalized)
    if _STORE_PATH:
        return _STORE_PATH
    # 2) the CONFIG_STORE_PATH constant edited in the script
    n = _normalize_store_path(CONFIG_STORE_PATH)
    if n:
        return n
    # 3) the pointer file set interactively via --config
    n = _normalize_store_path(_read_store_pointer())
    if n:
        return n
    # 4) default: next to the script
    return default_store_path()


_STORE_WARNED = set()


def load_store():
    """Loads the unified file {config, presets, default_preset}."""
    p = current_store_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception as e:
        if p not in _STORE_WARNED:
            _STORE_WARNED.add(p)
            log_warn(f"Could not read the settings file: {p}")
            log_warn(f"  reason: {e}")
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("config", {})
    data.setdefault("presets", {})
    data.setdefault("default_preset", None)
    return data


def save_store(store):
    p = current_store_path()
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)   # create target folder (also for a new network dir)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(p, 0o600)   # contains API keys
        except OSError:
            pass
    except Exception as e:
        log_warn(f"Saving {os.path.basename(p)} failed: {e}")


def migrate_legacy_store():
    """One-time migration of old config.json / preset.json / presets/*.json into
    the new video_tool.config.json (when the new one doesn't exist yet)."""
    if os.path.exists(current_store_path()):
        return
    base = os.path.dirname(current_store_path())
    store = {"config": {}, "presets": {}, "default_preset": None}
    found = False
    old_cfg = os.path.join(base, "config.json")
    if os.path.isfile(old_cfg):
        try:
            with open(old_cfg, "r", encoding="utf-8") as f:
                store["config"] = json.load(f) or {}
            found = True
        except Exception:
            pass
    old_preset = os.path.join(base, "preset.json")
    if os.path.isfile(old_preset):
        try:
            with open(old_preset, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d and d.get("command"):
                store["default_preset"] = d
                found = True
        except Exception:
            pass
    old_dir = os.path.join(base, "presets")
    if os.path.isdir(old_dir):
        for fn in os.listdir(old_dir):
            if not fn.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(old_dir, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d and d.get("command"):
                    store["presets"][os.path.splitext(fn)[0]] = d
                    found = True
            except Exception:
                pass
    if found:
        save_store(store)
        log_info(f"Migrated old settings/presets into {os.path.basename(current_store_path())}.")


def preset_dryrun():
    """True when a preset is only being CREATED (answers are being collected)."""
    return _PRESET_DRYRUN


def _safe_preset_name(name):
    safe = re.sub(r"[^\w\-. ]+", "_", (name or "").strip()).strip().replace(" ", "_")
    return safe or "preset"


def list_named_presets():
    """Returns [(label, key, command)]; key is the preset name, or None for the
    default (runs at startup)."""
    store = load_store()
    out = []
    for name, p in sorted(store.get("presets", {}).items()):
        if p and p.get("command"):
            out.append((p.get("label") or name, name, p["command"]))
    dp = store.get("default_preset")
    if dp and dp.get("command"):
        out.append((f"(default - runs at startup) [{dp['command']}]", None, dp["command"]))
    return out


def get_preset(key):
    """key=None -> default preset, otherwise a named one."""
    store = load_store()
    if key is None:
        return store.get("default_preset")
    return store.get("presets", {}).get(key)


def delete_preset(key):
    store = load_store()
    if key is None:
        store["default_preset"] = None
    else:
        store.get("presets", {}).pop(key, None)
    save_store(store)


def preset_is_replaying():
    """True when a preset from --load is currently running (no interactive user)."""
    return _PRESET_MODE == "load"


def _is_secret_prompt(p):
    pl = str(p).lower()
    return any(h in pl for h in _SECRET_HINTS)


def _preset_replay():
    """Returns the next saved answer (load), or _PRESET_MISS when none/wrong mode."""
    global _PRESET_IDX
    if _PRESET_MODE != "load" or _PRESET_IDX >= len(_PRESET_DATA):
        return _PRESET_MISS
    item = _PRESET_DATA[_PRESET_IDX]
    _PRESET_IDX += 1
    if item.get("secret"):
        return _PRESET_MISS         # secrets are not stored -> ask / take from config
    return item.get("a")


def _preset_record(kind, prompt, value):
    if _PRESET_MODE in ("save", "offer"):
        if _is_secret_prompt(prompt):
            _PRESET_REC.append({"k": kind, "secret": True})
        else:
            _PRESET_REC.append({"k": kind, "q": str(prompt)[:60], "a": value})


def preset_begin_save(cmd, key=None, label=None, dryrun=False):
    global _PRESET_MODE, _PRESET_REC, _PRESET_CMD, _PRESET_KEY, _PRESET_SAVED, _PRESET_LABEL, _PRESET_DRYRUN
    _PRESET_MODE = "save"
    _PRESET_REC = []
    _PRESET_CMD = cmd
    _PRESET_KEY = key
    _PRESET_SAVED = False
    _PRESET_LABEL = label
    _PRESET_DRYRUN = dryrun


def preset_begin_offer(cmd):
    """Like save, but at the end it OFFERS to save (a question)."""
    global _PRESET_MODE, _PRESET_REC, _PRESET_CMD, _PRESET_KEY, _PRESET_SAVED, _PRESET_LABEL, _PRESET_DRYRUN
    _PRESET_MODE = "offer"
    _PRESET_REC = []
    _PRESET_CMD = cmd
    _PRESET_KEY = None
    _PRESET_SAVED = False
    _PRESET_LABEL = None
    _PRESET_DRYRUN = False


def preset_begin_load(answers):
    global _PRESET_MODE, _PRESET_DATA, _PRESET_IDX, _PRESET_DRYRUN
    _PRESET_MODE = "load"
    _PRESET_DATA = list(answers or [])
    _PRESET_IDX = 0
    _PRESET_DRYRUN = False


def _write_preset():
    store = load_store()
    payload = {"command": _PRESET_CMD, "answers": _PRESET_REC}
    if _PRESET_LABEL:
        payload["label"] = _PRESET_LABEL
    if _PRESET_KEY is None:
        store["default_preset"] = payload
    else:
        store.setdefault("presets", {})[_PRESET_KEY] = payload
    save_store(store)


def preset_flush_if_save():
    """Called right before running the operation. For 'save' it saves directly,
    for 'offer' it asks whether to save the choices as a preset (optionally named)."""
    global _PRESET_SAVED, _PRESET_KEY, _PRESET_LABEL
    if _PRESET_SAVED:
        return
    store_name = os.path.basename(current_store_path())
    if _PRESET_MODE == "save":
        try:
            _write_preset()
            _PRESET_SAVED = True
            log_done(f"Preset saved to {store_name}"
                     + (f" as '{_PRESET_KEY}'." if _PRESET_KEY else " (default, runs at startup)."))
        except Exception as e:
            log_warn(f"Saving the preset failed: {e}")
    elif _PRESET_MODE == "offer":
        _PRESET_SAVED = True   # so it doesn't ask twice
        raw = input("Save these choices as a preset? [y/N]: ").strip().lower()
        if raw not in ("a", "y", "ano", "yes", "ja"):
            return
        name = input("Preset name (Enter = default, runs by itself at startup): ").strip()
        if name:
            _PRESET_KEY = name
            _PRESET_LABEL = name
        try:
            _write_preset()
            if name:
                log_done(f"Preset '{name}' saved to {store_name}. You'll find it in the Presets menu.")
            else:
                log_done(f"Default preset saved to {store_name}. Next time it runs by itself.")
        except Exception as e:
            log_warn(f"Saving the preset failed: {e}")



def ask_choice(prompt, options, allow_skip=True, allow_abort=True, header=None):
    """CLI selection (arrow menu / numbered fallback). Returns an index (int),
    'skip', or 'abort'. Esc = back (WizardBack)."""
    r = _preset_replay()
    if r is not _PRESET_MISS:
        if isinstance(r, int) and 0 <= r < len(options):
            return r
        if r in ("skip", "abort"):
            return r
        return 0
    got, v = _back_get()
    if got:
        _preset_record("choice", prompt, v)
        return v
    n = len(options)
    labels = list(options)
    extra = []
    if allow_skip:
        labels.append(f"{Fore.MAGENTA}— skip this file —{Style.RESET_ALL}")
        extra.append("skip")
    if allow_abort:
        labels.append(f"{Fore.MAGENTA}— cancel the whole batch run —{Style.RESET_ALL}")
        extra.append("abort")
    _pend = _back_pending_take()
    _dflt = 0
    if _pend is not _BACK_NO_PENDING:
        if isinstance(_pend, int) and 0 <= _pend < n:
            _dflt = _pend
        elif _pend in extra:
            _dflt = n + extra.index(_pend)
    if _tui_supported():
        idx = interactive_menu(prompt, labels, default=_dflt, allow_cancel=True, header=header)
    else:
        for h in (header or []):
            if strip_ansi(h).strip():
                print(h)
        idx = _ask_pick_classic(prompt, labels, _dflt, allow_back=True)
    if idx is None:
        raise _StepBack()
    result = idx if idx < n else extra[idx - n]
    _back_put(result)
    _preset_record("choice", prompt, result)
    return result


def try_list_tracks(mkvmerge_bin, video_path):
    """Like mkvmerge_tracks(), but never dies (sys.exit) - for the batch
    pre-flight, where an error on one file should not stop the whole run."""
    try:
        result = subprocess.run([mkvmerge_bin, "-J", str(video_path)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        data = json.loads(result.stdout)
    except Exception as e:
        return None, None, str(e)
    subs, audio = [], []
    for track in data.get("tracks", []):
        props = track.get("properties", {})
        entry = {"id": track["id"], "codec": track.get("codec", "?"),
                  "lang": props.get("language", "und"), "title": props.get("track_name", "")}
        if track.get("type") == "subtitles":
            subs.append(entry)
        elif track.get("type") == "audio":
            audio.append(entry)
    return subs, audio, None


def preflight_check(video_path, mkvmerge_bin, args):
    """Verifies BEFORE processing that the video contains the tracks needed for the chosen
    --audio-mode (+ --ref-lang/--track-id, --audio-lang/--audio-track-id).
    Returns (ok: bool, problem: str, sub_tracks, audio_tracks)."""
    sub_tracks, audio_tracks, err = try_list_tracks(mkvmerge_bin, video_path)
    if err:
        return False, f"cannot read tracks ({err})", [], []

    need_sub = args.audio_mode in ("off", "combine")
    need_aud = args.audio_mode in ("replace", "combine")
    problems = []

    if need_sub:
        if args.track_id is not None:
            if not any(t["id"] == args.track_id for t in sub_tracks):
                problems.append(f"subtitle track with ID {args.track_id} does not exist")
        else:
            text_tracks = [t for t in sub_tracks if is_text_codec(t["codec"])]
            if not text_tracks:
                problems.append("no usable text subtitle track")
            elif args.ref_lang and not any(
                    t["lang"].lower().startswith(args.ref_lang.lower()) for t in text_tracks):
                avail = ", ".join(t["lang"] for t in text_tracks) or "none"
                problems.append(f"no subtitle track in language '{args.ref_lang}' (available: {avail})")

    if need_aud:
        if args.audio_track_id is not None:
            if not any(t["id"] == args.audio_track_id for t in audio_tracks):
                problems.append(f"audio track with ID {args.audio_track_id} does not exist")
        else:
            if not audio_tracks:
                problems.append("no audio track")
            elif args.audio_lang and not any(
                    t["lang"].lower().startswith(args.audio_lang.lower()) for t in audio_tracks):
                avail = ", ".join(t["lang"] for t in audio_tracks) or "none"
                problems.append(f"no audio track in language '{args.audio_lang}' (available: {avail})")

    return (len(problems) == 0), "; ".join(problems), sub_tracks, audio_tracks


def resolve_preflight_problem(video_path, problem, sub_tracks, audio_tracks):
    """Interactively asks what to do when a video lacks the expected tracks.
    Returns ('skip'|'abort'|'override', track_id_or_None, audio_track_id_or_None)."""
    log_warn(f"{os.path.basename(video_path)}: {problem}")
    hdr = [f"{Fore.YELLOW}{os.path.basename(video_path)}: {problem}{Style.RESET_ALL}"]
    if sub_tracks:
        hdr.append(f"{Fore.CYAN}Available subtitle tracks:{Style.RESET_ALL}")
        for t in sub_tracks:
            hdr.append(f"   ID={t['id']:>3}  {t['lang']:<5} {t['codec']}")
    if audio_tracks:
        hdr.append(f"{Fore.CYAN}Available audio tracks:{Style.RESET_ALL}")
        for t in audio_tracks:
            hdr.append(f"   ID={t['id']:>3}  {t['lang']:<5} {t['codec']}")
    hdr.append("")
    if sub_tracks:
        labels = [f"use subtitle track ID={t['id']} ({t['lang']}, {t['codec']})" for t in sub_tracks]
        labels.append("enter track IDs manually")
        choice = ask_choice("What to do?", labels, header=hdr)
        if choice in ("skip", "abort"):
            return choice, None, None
        if choice < len(sub_tracks):
            return "override", sub_tracks[choice]["id"], None
    else:
        choice = ask_choice("What to do?", ["enter track IDs manually"], header=hdr)
        if choice in ("skip", "abort"):
            return choice, None, None
    t_raw = ask_text("Subtitle track ID to use (Enter = none)", "").strip()
    a_raw = ask_text("Audio track ID to use (Enter = none)", "").strip()
    t_id = int(t_raw) if t_raw.isdigit() else None
    a_id = int(a_raw) if a_raw.isdigit() else None
    return "override", t_id, a_id


def build_passthrough_args(args):
    """Builds the per-file CLI arguments from the values in args (except
    batch-only and positional arguments and the track-id override, which are
    handled separately per file)."""
    out = ["--audio-mode", args.audio_mode]
    out += ["--method", args.method]
    if args.ca_band is not None:
        out += ["--ca-band", str(args.ca_band)]
    if args.ca_snap_win is not None:
        out += ["--ca-snap-win", str(args.ca_snap_win)]
    if args.ca_min_sim is not None:
        out += ["--ca-min-sim", str(args.ca_min_sim)]
    if getattr(args, "translate", "off") and args.translate != "off":
        out += ["--translate", args.translate, "--pivot-lang", args.pivot_lang]
    if args.ref_lang:
        out += ["--ref-lang", args.ref_lang]
    if args.audio_lang:
        out += ["--audio-lang", args.audio_lang]
    out += ["--vad-percentile", str(args.vad_percentile)]
    out += ["--max-shift", str(args.max_shift)]
    out += ["--tolerance", str(args.tolerance)]
    if args.fix_short_duration:
        out += ["--fix-short-duration"]
    if args.reading_speed:
        out += ["--reading-speed", args.reading_speed]
    if args.min_cps is not None:
        out += ["--min-cps", str(args.min_cps)]
    if args.min_duration_floor is not None:
        out += ["--min-duration-floor", str(args.min_duration_floor)]
    if args.min_gap is not None:
        out += ["--min-gap", str(args.min_gap)]
    if args.line_overhead is not None:
        out += ["--line-overhead", str(args.line_overhead)]
    if args.mkvmerge:
        out += ["--mkvmerge", args.mkvmerge]
    if args.mkvextract:
        out += ["--mkvextract", args.mkvextract]
    if args.no_mkvtoolnix_download:
        out += ["--no-mkvtoolnix-download"]
    if args.ffmpeg:
        out += ["--ffmpeg", args.ffmpeg]
    if args.no_ffmpeg_download:
        out += ["--no-ffmpeg-download"]
    return out


def run_batch(args):
    directory = str(args.mkv) if args.mkv else "."
    if not os.path.isdir(directory):
        die(f"Not a directory: {directory}")

    global MKVMERGE, MKVEXTRACT
    if args.mkvmerge:
        MKVMERGE = args.mkvmerge
    if args.mkvextract:
        MKVEXTRACT = args.mkvextract
    mkvmerge_bin = args.mkvmerge or find_tool(["mkvmerge", "mkvmerge.exe"])
    if not mkvmerge_bin:
        mkvmerge_bin, _ = ensure_mkvtoolnix(directory, allow_download=not args.no_mkvtoolnix_download)
    if not mkvmerge_bin:
        die("mkvmerge not found - required even just for previewing tracks in batch mode (--all).")

    videos = collect_videos(directory, args.recursive)
    if not videos:
        log_warn(f"No video files found in '{directory}' ({', '.join(sorted(VIDEO_EXTS_BATCH))}).")
        return
    srts = collect_srts(directory, args.recursive)
    log_info(f"Found {len(videos)} video files, {len(srts)} .srt files in '{directory}'.")

    plan = []
    skipped = []

    for v in videos:
        matches = match_srt_for_video(v, srts, args.target_lang)
        if not matches:
            log_warn(f"{os.path.basename(v)}: no matching .srt found - skipped")
            skipped.append(v)
            continue

        if len(matches) > 1:
            choice = ask_choice(
                f"{os.path.basename(v)}: found {len(matches)} matching .srt - which one belongs here?",
                [os.path.basename(m) for m in matches],
            )
            if choice == "skip":
                skipped.append(v)
                continue
            if choice == "abort":
                log_warn("Batch run cancelled by the user.")
                return
            srt = matches[choice]
        else:
            srt = matches[0]

        ok, problem, sub_tracks, audio_tracks = preflight_check(v, mkvmerge_bin, args)
        override_track, override_audio = args.track_id, args.audio_track_id
        if not ok:
            if args.yes:
                log_warn(f"{os.path.basename(v)}: {problem} - SKIPPED (--yes, no prompt).")
                skipped.append(v)
                continue
            action, t_id, a_id = resolve_preflight_problem(v, problem, sub_tracks, audio_tracks)
            if action == "skip":
                skipped.append(v)
                continue
            if action == "abort":
                log_warn("Batch run cancelled by the user.")
                return
            if t_id is not None:
                override_track = t_id
            if a_id is not None:
                override_audio = a_id

        plan.append((v, srt, override_track, override_audio))

    print()
    if skipped:
        log_warn(f"Skipped (missing subtitles/tracks): {len(skipped)}")
        for v in skipped:
            print(f"   - {os.path.basename(v)}")
    if not plan:
        log_warn("Nothing to process.")
        return

    log_info(f"To process: {len(plan)} files:")
    for v, s, *_ in plan:
        print(f"   {os.path.basename(v)}  <-  {os.path.basename(s)}")
    print()

    base_pass = build_passthrough_args(args)
    ok_count = 0
    fail_count = 0
    for v, s, t_id, a_id in plan:
        srt_path = Path(s)
        if args.overwrite:
            out_path = srt_path
            bak = srt_path.with_suffix(srt_path.suffix + ".bak")
            if not bak.exists():
                shutil.copy(srt_path, bak)
        else:
            out_path = srt_path.with_name(srt_path.stem + ".synced" + srt_path.suffix)

        extra = list(base_pass)
        if t_id is not None:
            extra += ["--track-id", str(t_id)]
        if a_id is not None:
            extra += ["--audio-track-id", str(a_id)]

        cmd = [sys.executable, os.path.abspath(__file__), str(v), str(srt_path), str(out_path)] + extra
        print(f"{Fore.CYAN}### {os.path.basename(v)}{Style.RESET_ALL}")
        result = subprocess.run(cmd)
        if result.returncode == 0:
            ok_count += 1
        else:
            fail_count += 1
            log_warn(f"Processing '{os.path.basename(v)}' failed (exit code {result.returncode}).")
        print()

    summary_color = Fore.GREEN if fail_count == 0 else Fore.YELLOW
    tail = f", {fail_count} failed" if fail_count else ""
    print(f"{summary_color}Done: {ok_count}/{len(plan)} succeeded{tail}.{Style.RESET_ALL}")


# ----------------------------------------------------------------------
# --fix-readability: a standalone batch mode that does NO synchronization at
# all - it only extends too-short display of subtitles (that already have
# CORRECT timing) for more comfortable reading. It needs neither video nor
# mkvtoolnix/ffmpeg - it works purely with .srt files.
#
# Timing safety: it uses the same fix_short_durations() as the single-file
# mode above - which NEVER moves a subtitle's start and never extends the end
# past the boundary (the gap to the next subtitle - a safety margin), so this
# operation cannot break existing timing nor cause overlaps between subtitles.
# ----------------------------------------------------------------------

def estimate_avg_cps(srt_files, sample_limit=5):
    """For the user's orientation during the interactive prompt: computes what
    reading speed (chars/s) the current subtitles typically HAVE (only for lines
    long enough to be meaningful - shorter than 0.3s are ignored, often overlaps/
    effects)."""
    speeds = []
    for path in srt_files[:sample_limit]:
        try:
            events = parse_srt(Path(path))
        except SystemExit:
            continue
        for ev in events:
            dur = ev["end"] - ev["start"]
            chars = len(re.sub(r"\s+", "", ev["text"]))
            if dur >= 0.3 and chars >= 3:
                speeds.append(chars / dur)
    if not speeds:
        return None
    speeds.sort()
    return speeds[len(speeds) // 2]  # median


def ask_readability_params(srt_files):
    """Interactively asks for the subtitle-extension parameters, with a clear
    explanation of what each is for, and with an approximate figure for the
    current subtitle speed (if it can be computed). Returns (cps, floor, gap, line_overhead)."""
    avg_cps = estimate_avg_cps(srt_files)
    preset_keys = list(READING_SPEED_PRESETS.keys())
    options = [f"{READING_SPEED_PRESETS[k][2]} ({READING_SPEED_PRESETS[k][0]:.0f} chars/s)" for k in preset_keys]
    options.append("Enter a custom reading speed (chars/s)")
    _rh = [f"{Fore.CYAN}Subtitle-extension settings for more comfortable reading{Style.RESET_ALL}",
           "Extends the display end only where there is free space (silence/gap); never changes",
           "a subtitle's start nor reaches into the next one. TIMING ALWAYS HAS PRIORITY."]
    if avg_cps:
        _rh.append(f"(For reference: your subtitles currently have a typical speed of ~{avg_cps:.1f} chars/s.)")
    _rh += ["", "Reading speed = how many characters the reader reads per 1 s. Lower number = longer on screen.", ""]
    choice = ask_choice("Choose the target reading speed:", options, allow_skip=False, allow_abort=True, header=_rh)
    if choice == "abort":
        return None

    if choice == len(preset_keys):
        raw = ask_text("Enter the reading speed in characters/s (e.g. 15)", "").strip()
        try:
            min_cps = float(raw)
        except ValueError:
            min_cps = DEFAULT_MIN_CPS
        default_floor = DEFAULT_MIN_DURATION_FLOOR
    else:
        min_cps, default_floor, _label = READING_SPEED_PRESETS[preset_keys[choice]]

    default_floor = 2.5   # default offered minimum display duration
    raw = ask_text(f"2) Minimum display duration (s) - even a short word is shown at least this "
                   f"long (regardless of the speed above). Seconds", str(default_floor)).strip()
    try:
        min_floor = float(raw) if raw else default_floor
    except ValueError:
        min_floor = default_floor

    raw = ask_text(f"3) Safety gap (s) before the next subtitle that the extension never "
                   f"exceeds (so they don't overlap). Seconds", str(DEFAULT_MIN_GAP)).strip()
    try:
        min_gap = float(raw) if raw else DEFAULT_MIN_GAP
    except ValueError:
        min_gap = DEFAULT_MIN_GAP

    raw = ask_text(f"4) Per-line bonus (s) extra for EACH line above the first (a two-line "
                   f"sentence gets more time than a one-line one). Seconds", str(DEFAULT_LINE_OVERHEAD)).strip()
    try:
        line_overhead = float(raw) if raw else DEFAULT_LINE_OVERHEAD
    except ValueError:
        line_overhead = DEFAULT_LINE_OVERHEAD

    return min_cps, min_floor, min_gap, line_overhead


# ----------------------------------------------------------------------
# Language detection FROM subtitle CONTENT (not from extensions/tags)
# ----------------------------------------------------------------------

_LANG_ALIASES = {
    "cze": "cs", "ces": "cs", "cz": "cs", "slk": "sk", "slo": "sk",
    "ger": "de", "deu": "de", "ger": "de", "eng": "en", "fre": "fr", "fra": "fr",
    "spa": "es", "ita": "it", "por": "pt", "dut": "nl", "nld": "nl",
    "rus": "ru", "ukr": "uk", "hun": "hu", "pol": "pl", "rum": "ro", "ron": "ro",
}


def norm_lang(code):
    if not code:
        return None
    c = str(code).strip().lower()
    if c in ("und", "unknown", ""):
        return None
    return _LANG_ALIASES.get(c, c[:2])


_LANG_STOPWORDS = {
    "cs": "a aby ale ani ano až bez bude budou by byl byla bylo být co či do i jak jako je jeho její jen ještě již k kde když ke která které kteří který má mě mi mít my na nad naše ne nebo není než nic o od on ona oni pak po pod podle pro proč protože při s se si tak také tam ten tedy to toho tom ty u už v vám váš ve však všechno vy z za ze že jsem jsi jsme jste jsou",
    "sk": "a aby ale ani áno až bez bude budú by bol bola bolo byť čo či do i ja ako je jeho jej len ešte už k kde keď ku ktorá ktoré ktorí ktorý má ma mi mať my na nad naša nie alebo nič o od on ona oni potom po pod podľa pre prečo pretože pri s sa si tak tiež tam ten teda to toho tom ty u už v vám váš vo viac však všetko vy z za zo že som sme ste sú",
    "en": "the a an and or but if then is are was were be been to of in on at for with from by as that this these those it he she they we you i me my your his her their our not no yes do does did have has had will would can could should about into over than when what which who",
    "de": "der die das und oder aber wenn dann ist sind war waren sein zu von in an auf für mit aus durch als dass dieser diese dieses es er sie wir ihr ich mich mein dein nicht kein ja doch noch schon auch nur wie wo wann warum weil über unter haben hat",
    "pl": "i a ale lub jeśli wtedy jest są był była było być do z w na o od po pod dla że ten ta to te oni my wy ja mnie mój nie tak czy już jeszcze jak gdzie kiedy dlaczego bo nad przez także również wszystko",
    "es": "el la los las un una unos unas y o pero si entonces es son era fue ser estar de en por para con sin que este esta esto estos ellos nosotros yo me mi tu su no sí ya como donde cuando porque más muy",
    "fr": "le la les un une des et ou mais si alors est sont était être de en pour avec sans que ce cette ces ils nous vous je me mon ton son ne pas plus très comme où quand parce qui",
    "it": "il lo la i gli le un uno una e o ma se allora è sono era essere di in per con senza che questo questa questi quelli noi voi io mi mio tuo suo non sì già come dove quando perché più molto",
    "pt": "o a os as um uma uns umas e ou mas se então é são era ser estar de em por para com sem que este esta isto estes eles nós eu me meu teu seu não sim já como onde quando porque mais muito",
    "nl": "de het een en of maar als dan is zijn was waren te van in op voor met uit door dat deze dit die zij wij jij ik mij mijn niet geen ja nog al ook hoe waar wanneer waarom omdat",
    "ru": "и а но или если то это есть был была было быть в на с по от до для что как эти они мы вы я мне мой не да уже еще где когда почему потому очень так",
    "uk": "і та але або якщо то це є був була було бути в на з по від до для що як ці вони ми ви я мені мій не так вже ще де коли чому тому дуже",
    "hu": "a az és vagy de ha akkor van vannak volt lenni hogy ez ezek ők mi ti én nekem nem igen már még hol mikor miért mert nem is",
}
_LANG_STOPWORDS = {k: set(v.split()) for k, v in _LANG_STOPWORDS.items()}


def _builtin_detect(text):
    low = text.lower()
    letters = [c for c in low if c.isalpha()]
    if not letters:
        return None
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    if cyr > 0.3 * len(letters):
        return "uk" if any(c in low for c in "іїєґ") else "ru"
    toks = re.findall(r"[^\W\d_]+", low, re.UNICODE)
    if not toks:
        return None
    scores = {lang: sum(1 for t in toks if t in sw) for lang, sw in _LANG_STOPWORDS.items()}
    # boosts based on characteristic characters
    if any(c in low for c in "řěů"):
        scores["cs"] += 6
    if any(c in low for c in "ľĺŕôä"):
        scores["sk"] += 5
    if any(c in low for c in "łżśźćń"):
        scores["pl"] += 6
    if "ß" in low:
        scores["de"] += 3
    if any(c in low for c in "ñ¿¡"):
        scores["es"] += 3
    if any(c in low for c in "çœ"):
        scores["fr"] += 2
    if any(c in low for c in "őű"):
        scores["hu"] += 5
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def detect_language(text, sample_chars=6000):
    """Detects the language FROM the text CONTENT. Prefers 'langdetect' (pip install
    langdetect) if installed; otherwise a built-in detector (cs/sk/pl/en/
    de/fr/es/it/pt/nl/ru/uk/hu...). Returns a 2-letter code or None."""
    sample = (text or "")[:sample_chars]
    if len(sample.strip()) < 10:
        return None
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return norm_lang(detect(sample))
    except Exception:
        pass
    return norm_lang(_builtin_detect(sample))


def detect_sub_language(events, max_lines=400):
    """Detects the language from subtitle events (a sample of the first max_lines lines)."""
    txt = " ".join(e["text"] for e in events[:max_lines] if e.get("text"))
    return detect_language(txt)


def detect_srt_file_language(path):
    try:
        return detect_sub_language(parse_srt(Path(path), strict=False))
    except Exception:
        return None


def detect_lang_tags(srt_files):
    """Guesses language/other tags from file names ('episode.cs.srt' -> 'cs').
    Only a hint - used for the interactive menu, not for hard filtering."""
    tags = set()
    for s in srt_files:
        stem = Path(s).stem
        parts = stem.rsplit(".", 1)
        if len(parts) == 2 and 1 <= len(parts[1]) <= 8 and parts[1].isalpha():
            tags.add(parts[1].lower())
    return sorted(tags)


def filter_by_tag(srt_files, tag):
    def has_tag(p):
        stem = Path(p).stem
        parts = stem.rsplit(".", 1)
        return len(parts) == 2 and parts[1].lower() == tag.lower()
    tagged = [s for s in srt_files if has_tag(s)]
    return tagged if tagged else srt_files


def ask_yes_no(prompt, default_no=True):
    r = _preset_replay()
    if r is not _PRESET_MISS:
        return bool(r)
    got, v = _back_get()
    if got:
        _preset_record("yesno", prompt, bool(v))
        return bool(v)
    suffix = "[y/N]" if default_no else "[Y/n]"
    _pend = _back_pending_take()
    _prefill = ""
    if _pend is not _BACK_NO_PENDING:
        _prefill = "y" if _pend else "n"
    if _tui_supported():
        raw = _read_line_tui(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} {suffix}: ", "", prefill=_prefill).strip().lower()
    else:
        raw = (input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} {suffix}: ").strip().lower() or _prefill)
    if not raw and _pend is not _BACK_NO_PENDING:
        val = bool(_pend)
    else:
        val = (not default_no) if not raw else (raw in ("a", "y", "ano", "yes", "ja"))
    _back_put(val)
    _preset_record("yesno", prompt, val)
    return val


def run_fix_readability(args):
    interactive = not args.yes
    print()
    if interactive and not args.mkv:
        print(f"{Fore.CYAN}--fix-readability: adjusting subtitle display duration (without affecting timing){Style.RESET_ALL}")
        print("I'll ask a few things step by step - anything can also be passed directly as a command "
              "argument so you don't have to fill it in again next time (see --help).")
        print()

    # 1) directory or a specific .srt file -----------------------------------
    if args.mkv:
        target = str(args.mkv)
    elif interactive:
        raw = ask_text("What to process - a directory to search, or a specific .srt file",
                       ".").strip()
        target = raw if raw else "."
    else:
        target = "."

    if os.path.isfile(target) and target.lower().endswith(".srt"):
        srt_files = [target]
        is_single_file = True
    elif os.path.isdir(target):
        is_single_file = False
        # 2) recursive search --------------------------------------------------
        recursive = args.recursive
        if interactive and not args.recursive:
            print("2) Should I search subdirectories too, or just this one directory?")
            recursive = ask_yes_no("   Search subdirectories too?", default_no=True)
            print()
        srt_files = collect_srts(target, recursive)
    else:
        die(f"Neither a .srt file nor a directory: {target}")

    if not srt_files:
        vids = [] if is_single_file else collect_videos(target, recursive)
        if vids and (args.yes or ask_yes_no(
                f"Found no .srt, but there are {len(vids)} videos. Extract subtitles from them?",
                default_no=False)):
            _saved = args.mkv
            args.mkv = Path(target)
            try:
                run_extract_subs(args, minimal=True)
            finally:
                args.mkv = _saved
            srt_files = collect_srts(target, recursive)
            if srt_files:
                log_info("Subtitles extracted - now applying readability to them.")
        if not srt_files:
            log_warn("No .srt files found to process.")
            return

    # 3) language/other filter - only when there are multiple variants and the user did not specify -----
    target_lang = args.target_lang
    if not is_single_file and target_lang is None and interactive and len(srt_files) > 1:
        tags = detect_lang_tags(srt_files)
        if len(tags) > 1:
            print(f"3) Found {len(srt_files)} .srt files with different language tags in the name ({', '.join(tags)}).")
            options = [f"only '{t}'" for t in tags] + ["all (do not filter)"]
            choice = ask_choice("   What to process?", options, allow_skip=False, allow_abort=True)
            if choice == "abort":
                log_warn("Cancelled by the user.")
                return
            if choice < len(tags):
                target_lang = tags[choice]
            print()
    if target_lang:
        srt_files = filter_by_tag(srt_files, target_lang)

    log_info(f"To process: {len(srt_files)} .srt files.")

    # 4) overwrite the original, or save as a new file -----------------------
    overwrite = args.overwrite
    make_bak = True
    if not args.overwrite and interactive:
        print()
        print("4) How to save the result?")
        choice = ask_choice(
            "   Choose the save mode:",
            ["New file '<name>.readability.srt' next to the original (recommended - nothing is overwritten)",
             "Overwrite the original directly (a '.bak' backup is created once)",
             "Overwrite the original WITHOUT a backup (no .bak is created)"],
            allow_skip=False, allow_abort=True,
        )
        if choice == "abort":
            log_warn("Cancelled by the user.")
            return
        overwrite = choice in (1, 2)
        make_bak = (choice == 1)
        print()

    # 5) reading-speed parameters ----------------------------------------------
    has_explicit_speed_choice = (
        args.reading_speed is not None or args.min_cps is not None or args.min_duration_floor is not None
    )
    if not has_explicit_speed_choice and interactive:
        result = ask_readability_params(srt_files)
        if result is None:
            log_warn("Cancelled by the user.")
            return
        min_cps, min_floor, min_gap, line_overhead = result
    else:
        min_cps, min_floor, min_gap, line_overhead = resolve_speed_params(args)

    print()
    log_info(
        f"Using: reading speed {min_cps:.1f} chars/s, min duration {min_floor:.2f}s, "
        f"gap {min_gap:.3f}s, per-line bonus {line_overhead:.2f}s, "
        f"output: {('overwrite original (+.bak)' if make_bak else 'overwrite original (NO .bak)') if overwrite else '*.readability.srt'}"
    )
    print()

    changed = 0
    unchanged = 0
    failed = 0

    for srt_file in srt_files:
        name = os.path.basename(srt_file)
        try:
            events = parse_srt(Path(srt_file))
        except SystemExit:
            log_warn(f"{name}: could not load (skipped)")
            failed += 1
            continue

        fixed, n_extended = fix_short_durations(
            events, min_cps=min_cps, min_duration_floor=min_floor, min_gap=min_gap, line_overhead=line_overhead
        )

        if n_extended == 0:
            print(f"  = {name} - unchanged")
            unchanged += 1
            continue

        srt_path = Path(srt_file)
        if overwrite:
            if make_bak:
                bak = srt_path.with_suffix(srt_path.suffix + ".bak")
                if not bak.exists():
                    shutil.copy(srt_path, bak)
            out_path = srt_path
        else:
            out_path = srt_path.with_name(srt_path.stem + ".readability" + srt_path.suffix)

        write_srt(fixed, out_path)
        print(f"  {Fore.GREEN}+{Style.RESET_ALL} {name} - extended {n_extended} subtitles -> {out_path.name}")
        changed += 1

    print()
    tail = f", {failed} failed" if failed else ""
    log_done(f"Done: {changed} modified, {unchanged} unchanged{tail} (of {len(srt_files)}).")


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Extract + translate subtitles into a new language and save .srt (--translate-subs)
# ----------------------------------------------------------------------
#
# For each video in the directory: extracts the chosen subtitle track, obtains
# subtitles in the TARGET language and saves them as <video>.<lang>.srt. Quality
# is handled in two
# cestami (lze kombinovat):
#   1) OpenSubtitles - downloads READY human subtitles in the target language
#      (best quality), optionally aligns them in time with the video using our
#      synchronization core (affine).
#   2) Machine translation of the extracted track (DeepL = best quality, Google,
#      or offline Argos) + proofreading (rule-based cleanup for free, optionally
#      AI proofreading via an OpenAI-compatible API).
# With machine translation the ORIGINAL TIMING is kept (only the text is
# translated), so the result fits the video without further synchronization.


def _resolve_tools_for_extract(args, video):
    is_mkv = video.suffix.lower() in MKVEXTRACT_CONTAINER_EXTS
    mkvmerge_bin = getattr(args, "mkvmerge", None) or find_tool(["mkvmerge", "mkvmerge.exe"])
    mkvextract_bin = getattr(args, "mkvextract", None) or find_tool(["mkvextract", "mkvextract.exe"])
    if not mkvmerge_bin or (is_mkv and not mkvextract_bin):
        try:
            mkvmerge_bin, mkvextract_bin = ensure_mkvtoolnix(
                str(video.parent), allow_download=not getattr(args, "no_mkvtoolnix_download", False))
        except Exception:
            pass
    ffmpeg_bin = None
    if not is_mkv:
        ffmpeg_bin = getattr(args, "ffmpeg", None) or find_tool(["ffmpeg", "ffmpeg.exe"])
        if not ffmpeg_bin:
            try:
                ffmpeg_bin = ensure_ffmpeg(str(video.parent), allow_download=not getattr(args, "no_ffmpeg_download", False))
            except Exception:
                pass
    return mkvmerge_bin, mkvextract_bin, ffmpeg_bin, is_mkv


def extract_subtitle_events(args, video, track_id=None, ref_lang=None):
    """Extracts the chosen subtitle track into events (track_id > ref_lang > first).
    Returns (events, chosen_track) or (None, None). Does not end the run on error."""
    video = Path(video)
    mkvmerge_bin, mkvextract_bin, ffmpeg_bin, is_mkv = _resolve_tools_for_extract(args, video)
    if not mkvmerge_bin:
        log_warn(f"{video.name}: mkvmerge not found - skipping.")
        return None, None
    try:
        sub_tracks = mkvmerge_tracks(mkvmerge_bin, video, "subtitles")
    except (Exception, SystemExit) as e:
        log_warn(f"{video.name}: cannot read tracks ({e}) - skipping.")
        return None, None
    if not sub_tracks:
        log_warn(f"{video.name}: no subtitle tracks.")
        return None, None
    try:
        chosen = pick_reference_track(sub_tracks, ref_lang, track_id)
    except SystemExit:
        chosen = sub_tracks[0]
    except Exception:
        chosen = sub_tracks[0]
    tmpd = tempfile.mkdtemp()
    try:
        outp = Path(tmpd) / "track.srt"
        if is_mkv:
            if not mkvextract_bin:
                log_warn(f"{video.name}: mkvextract not found.")
                return None, None
            extract_subtitle_to_srt(mkvextract_bin, video, chosen["id"], outp)
        else:
            if not ffmpeg_bin:
                log_warn(f"{video.name}: ffmpeg not found (extraction from MP4).")
                return None, None
            pos = [t["id"] for t in sub_tracks].index(chosen["id"])
            extract_subtitle_via_ffmpeg(ffmpeg_bin, video, pos, outp)
        ev = parse_srt(outp, strict=False)
        # Fallback: the track may not be SubRip (ASS/SSA/other encoding) - if the
        # result is empty and we have ffmpeg, let it convert to SRT.
        if not ev and ffmpeg_bin:
            try:
                pos = [t["id"] for t in sub_tracks].index(chosen["id"])
                outp2 = Path(tmpd) / "track_ff.srt"
                extract_subtitle_via_ffmpeg(ffmpeg_bin, video, pos, outp2)
                ev = parse_srt(outp2, strict=False)
            except Exception:
                pass
        if not ev:
            return None, chosen
        return ev, chosen
    except (Exception, SystemExit) as e:
        log_warn(f"{video.name}: track extraction failed: {e}")
        return None, None
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _track_tag(track, all_text_tracks):
    """Deterministic naming: the first track of a given language -> '<lang>',
    further ones of the same language -> '<lang>.2', '.3'... (independent of
    extraction order)."""
    lang = track["lang"] if track.get("lang") and track["lang"] != "und" else f"track{track['id']}"
    same = [t for t in all_text_tracks if (t.get("lang") or "") == (track.get("lang") or "")]
    if len(same) <= 1:
        return lang
    try:
        pos = same.index(track) + 1
    except ValueError:
        pos = 1
    return lang if pos == 1 else f"{lang}.{pos}"


def probe_text_subtitle_tracks(args, video):
    """Safely (never dies) returns the list of the video's TEXT subtitle tracks."""
    mm = getattr(args, "mkvmerge", None)
    if not mm:
        mm, me, ff, _ = _resolve_tools_for_extract(args, Path(video))
        if mm:
            args.mkvmerge = args.mkvmerge or mm
    if not mm:
        return []
    try:
        tracks = mkvmerge_tracks(mm, Path(video), "subtitles")
    except (Exception, SystemExit):
        return []
    return [t for t in tracks if is_text_codec(t["codec"])]


def extract_with_fallback(args, video, initial_track, text_tracks, done_ids=None,
                          interactive=True):
    """Extracts 'initial_track'; if the track fails (empty/unreadable/image-based)
    and we are interactive, SCANS the video and offers another track to choose
    (or to skip). Returns (events, chosen_track) or (None, None).
    Sets args._extract_skip_prompts when the user chooses 'don't ask again'."""
    done_ids = done_ids or set()
    tried = set()
    track = initial_track
    vname = Path(video).name
    while track is not None:
        tried.add(track["id"])
        events, chosen = extract_subtitle_events(args, video, track_id=track["id"])
        if events:
            return events, (chosen or track)

        # track failed
        if not interactive or getattr(args, "_extract_skip_prompts", False):
            log_warn(f"{vname}: track #{track['id']} ({track.get('lang', '?')}) "
                     "is empty/unreadable - skipping.")
            return None, None

        alts = [t for t in text_tracks if t["id"] not in tried and t["id"] not in done_ids]
        log_warn(f"{vname}: track #{track['id']} ({track.get('lang', '?')}) is empty or "
                 "unreadable (maybe image-based subtitles or corrupted).")
        labels = [f"#{t['id']}  {t['lang']:4} {t['codec']}  {t.get('title', '')}".rstrip() for t in alts]
        labels += ["skip this video", "don't ask for further videos (just skip)"]
        _fh = [f"{Fore.YELLOW}{vname}:{Style.RESET_ALL}",
               f"{Fore.YELLOW}Track #{track['id']} ({track.get('lang', '?')}) is empty/unreadable "
               f"(image-based subtitles or corrupted).{Style.RESET_ALL}", ""]
        i = ask_pick(f"{vname}: try another subtitle track?", labels,
                     default=0 if alts else len(alts), header=_fh, allow_back=False,
                     help=([f"Extracts track #{t['id']} ({t['lang']}, {t['codec']}) instead."
                            for t in alts]
                           + ["Skips this video (no .srt is saved from it).",
                              "For all further failed tracks it won't ask and will just skip them."]))
        if i < len(alts):
            track = alts[i]
        elif i == len(alts):
            return None, None
        else:
            args._extract_skip_prompts = True
            return None, None
    return None, None


def clean_subtitle_text(text, max_line=42):
    """Rule-based proofreading: normalizes spaces, fixes spaces before punctuation
    and excess blank lines, and breaks long single-line text into 2 lines."""
    t = text.replace("\r", "")
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    t = " ".join(lines)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.!?;:...])", r"\1", t)
    t = re.sub(r"([¿¡])\s+", r"\1", t)
    t = re.sub(r"\.{3,}", "...", t)
    if len(t) > max_line and " " in t:
        words = t.split(" ")
        target = len(t) / 2.0
        best_i, best_d, acc = None, None, 0
        for i, w in enumerate(words[:-1]):
            acc += len(w) + 1
            d = abs(acc - target)
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        l1 = " ".join(words[:best_i + 1])
        l2 = " ".join(words[best_i + 1:])
        if l1 and l2:
            t = l1 + "\n" + l2
    return t


_SENT_ENDERS = ".!?...:"


def _looks_continuation(cur, nxt, gap, max_gap=2.0):
    """Is 'nxt' a continuation of the sentence in 'cur'? (for joining fragments before translation)"""
    c = (cur or "").replace("\n", " ").strip()
    n = (nxt or "").replace("\n", " ").strip()
    if not c or not n or gap > max_gap:
        return False
    if n[:1] in "-–—":          # new speaker (dash) -> do not join
        return False
    last = c[-1]
    if last in _SENT_ENDERS:
        return False
    if last in "\"”»)]" and len(c) >= 2 and c[-2] in _SENT_ENDERS:
        return False
    return True


def _merge_sentence_groups(events, max_group=4, max_gap=2.0):
    """Groups consecutive subtitles that form one sentence."""
    groups = []
    i, n = 0, len(events)
    while i < n:
        grp = [i]
        while (len(grp) < max_group and i + 1 < n
               and _looks_continuation(events[i]["text"], events[i + 1]["text"],
                                       events[i + 1]["start"] - events[i]["end"], max_gap)):
            i += 1
            grp.append(i)
        groups.append(grp)
        i += 1
    return groups


def _split_translation(translated, parts):
    """Splits the translated sentence back into len(parts) pieces proportionally
    to the length of the originals, on word boundaries (so the timing stays but
    the text fits the original lines)."""
    translated = (translated or "").strip()
    if len(parts) == 1:
        return [translated]
    words = translated.split()
    if not words:
        return [translated] + [""] * (len(parts) - 1)
    total = sum(max(1, len(p.replace("\n", " ").strip())) for p in parts)
    out, wi, nw = [], 0, len(words)
    acc = 0.0
    for k, p in enumerate(parts):
        if k == len(parts) - 1:
            out.append(" ".join(words[wi:]))
            break
        acc += max(1, len(p.replace("\n", " ").strip())) / total
        tw = int(round(acc * nw))
        tw = max(wi + 1, min(tw, nw - (len(parts) - 1 - k)))
        out.append(" ".join(words[wi:tw]))
        wi = tw
    return out


def translate_events_to(events, engine, target_lang, api_key=None, model=None, sentence_aware=True):
    """Translates the event text into target_lang (timing unchanged). Returns new
    events, or None when a translator is not available.
    sentence_aware=True: joins sentence fragments split across multiple subtitles,
    translates them as a WHOLE sentence (better quality and with context) and splits back."""
    tr = make_translator(engine, target_lang, api_key=api_key, model=model)
    if tr is None:
        return None

    if not sentence_aware:
        out = tr([e["text"].replace("\n", " ") for e in events])
        return [{"start": e["start"], "end": e["end"], "text": (o or e["text"])}
                for e, o in zip(events, out)]

    groups = _merge_sentence_groups(events)
    merged = [" ".join(events[i]["text"].replace("\n", " ").strip() for i in g) for g in groups]
    translated = tr(merged)
    result = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]
    for g, tsent in zip(groups, translated):
        parts = [events[i]["text"] for i in g]
        if not tsent:
            continue  # translation failed -> keep the original
        pieces = _split_translation(tsent, parts)
        for idx, piece in zip(g, pieces):
            if piece.strip():
                result[idx]["text"] = piece.strip()
    return result


def anthropic_messages(prompt, api_key, model, max_tokens=4000, timeout=180):
    """A single Anthropic Messages API call. Returns the response text or None.
    On 4xx it raises _FatalAPIError with a specific message (no point retrying)."""
    import urllib.request
    import urllib.error
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e)
        msg = f"HTTP {e.code}: {detail or e.reason}"
        if e.code in (400, 401, 403, 404):
            raise _FatalAPIError(msg)
        raise RuntimeError(msg)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts) if parts else None


_ANTHROPIC_MODEL_HINTS = (
    ("opus", "most powerful (hardest tasks), most expensive per token"),
    ("fable", "top-tier model for very demanding tasks"),
    ("sonnet", "balanced quality/price/speed - recommended"),
    ("haiku", "fastest and cheapest (simple tasks)"),
)
_ANTHROPIC_STATIC_MODELS = [
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ("claude-opus-4-5-20251101", "Claude Opus 4.5 (pinned)"),
    ("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5 (pinned)"),
]


def _anthropic_model_hint(mid):
    m = (mid or "").lower()
    for k, v in _ANTHROPIC_MODEL_HINTS:
        if k in m:
            return v
    return ""


def anthropic_list_models(api_key, timeout=30):
    """Returns the list of models available for the given key (Anthropic /v1/models)."""
    import urllib.request
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                 headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("data", [])


def anthropic_model_info(api_key, model_id, timeout=15):
    """Model detail incl. token limits (max_input_tokens, max_tokens)."""
    import urllib.request
    from urllib.parse import quote
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    req = urllib.request.Request(f"https://api.anthropic.com/v1/models/{quote(model_id)}",
                                 headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _print_anthropic_models(args=None):
    key = (getattr(args, "anthropic_key", None) if args else None) or os.environ.get("ANTHROPIC_API_KEY")
    models = None
    if key:
        try:
            print(f"{Fore.CYAN}Loading the model list from the Anthropic API...{Style.RESET_ALL}")
            models = anthropic_list_models(key)
        except Exception as e:
            log_warn(f"Could not load the online list ({e}). Showing the built-in overview.")
    else:
        log_info("Anthropic key is not set - showing the built-in overview (the online list requires a key).")

    print(f"{Fore.MAGENTA}Available Claude models:{Style.RESET_ALL}")
    if models:
        for m in models:
            mid = m.get("id", "?")
            name = m.get("display_name", "")
            info = ""
            if key:
                try:
                    d = anthropic_model_info(key, mid)
                    ctx = d.get("max_input_tokens")
                    out = d.get("max_tokens")
                    if ctx or out:
                        info = f" | context {ctx}, max output {out} tok."
                except Exception:
                    pass
            hint = _anthropic_model_hint(mid)
            print(f"  {mid:<30}{name}{info}" + (f"  [{hint}]" if hint else ""))
    else:
        for mid, name in _ANTHROPIC_STATIC_MODELS:
            hint = _anthropic_model_hint(mid)
            print(f"  {mid:<30}{name}" + (f"  [{hint}]" if hint else ""))
    print(f"{Fore.CYAN}Note: Haiku = cheapest/fastest, Sonnet = balanced, Opus = "
          f"most expensive/most powerful. For exact token prices see anthropic.com/pricing.{Style.RESET_ALL}")


def _subtitle_translate_prompt(batch, target_lang):
    """A quality prompt for subtitle translation (tuned mainly for Czech)."""
    numbered = "\n".join(f"{k + 1}. {t.replace(chr(10), ' / ')}" for k, t in enumerate(batch))
    lang_note = ""
    if target_lang in ("cs", "sk"):
        lang_note = (" Use natural, colloquial " + ("Czech" if target_lang == "cs" else "Slovak")
                     + ", correct diacritics and punctuation. Keep a consistent form of address (informal/formal) "
                     "according to the scene context. Do NOT translate character names and titles.")
    return (f"You are an experienced translator of movie and TV subtitles. Translate the following "
            f"numbered lines into the language '{target_lang}'. Translate NATURALLY and IDIOMATICALLY "
            f"(not literally), preserve the meaning, tone, register and humor.{lang_note} "
            "The lines follow one another as continuous dialog - use the context, but keep the SAME "
            "NUMBER and ORDER of items. Separate a multi-line subtitle with ' / '. Return ONLY the numbered "
            "lines in the form 'number. translation', without quotes and without any comments.\n\n" + numbered)


def anthropic_translate_batch(batch, target_lang, api_key, model):
    """Translates a batch of subtitle lines into target_lang via Claude. Returns
    a list of the same length (None on failure). Propagates fatal errors (4xx)."""
    prompt = _subtitle_translate_prompt(batch, target_lang)
    try:
        content = anthropic_messages(prompt, api_key, model)
    except _FatalAPIError:
        raise
    except Exception:
        return [None] * len(batch)
    if not content:
        return [None] * len(batch)
    parsed = _parse_numbered(content, len(batch))
    return [(p.replace(" / ", "\n") if p else None) for p in parsed]


def gemini_generate(prompt, api_key, model="gemini-2.5-flash", timeout=120):
    """A single Google Gemini call (generativelanguage). Returns text or None.
    On 4xx it raises _FatalAPIError (do not retry)."""
    import urllib.request
    import urllib.error
    from urllib.parse import quote
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model)}"
           f":generateContent?key={quote(api_key)}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e)
        msg = f"HTTP {e.code}: {detail or e.reason}"
        if e.code in (400, 401, 403, 404):
            raise _FatalAPIError(msg)
        if e.code == 429 and ("limit: 0" in detail or "free_tier" in detail.lower() or "quota" in detail.lower()):
            raise _FatalAPIError(msg + "  → This model has no free quota for your account/region. "
                                 "Try another model ('?' at the model, or --gemini-model, e.g. "
                                 "gemini-1.5-flash), or use the 'google' engine (completely free without a key).")
        raise RuntimeError(msg)
    cands = data.get("candidates", [])
    if not cands:
        return None
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    return text or None


def gemini_translate_batch(batch, target_lang, api_key, model):
    """Translates a batch of subtitles into target_lang via Gemini (same prompt as Claude)."""
    prompt = _subtitle_translate_prompt(batch, target_lang)
    try:
        content = gemini_generate(prompt, api_key, model)
    except _FatalAPIError:
        raise
    except Exception:
        return [None] * len(batch)
    if not content:
        return [None] * len(batch)
    parsed = _parse_numbered(content, len(batch))
    return [(p.replace(" / ", "\n") if p else None) for p in parsed]


_GEMINI_STATIC_MODELS = [
    ("gemini-2.5-flash", "fast, good quality"),
    ("gemini-2.0-flash-lite", "cheapest/fastest"),
    ("gemini-1.5-flash", "older flash - often has a free quota"),
    ("gemini-1.5-flash-8b", "small, cheap"),
    ("gemini-2.5-flash", "newer flash"),
]


def gemini_list_models(api_key, timeout=30):
    import urllib.request
    from urllib.parse import quote
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={quote(api_key)}&pageSize=200"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("models", [])


def _print_gemini_models(args=None):
    key = ((getattr(args, "gemini_key", None) if args else None)
           or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    models = None
    if key:
        try:
            print(f"{Fore.CYAN}Loading the Gemini model list...{Style.RESET_ALL}")
            models = gemini_list_models(key)
        except Exception as e:
            log_warn(f"Could not load the online list ({e}). Showing the built-in overview.")
    else:
        log_info("Gemini key is not set - showing the built-in overview.")

    print(f"{Fore.MAGENTA}Gemini models for translation:{Style.RESET_ALL}")
    if models:
        for m in models:
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            mid = m.get("name", "").split("/")[-1]
            if "embedding" in mid or "aqa" in mid:
                continue
            disp = m.get("displayName", "")
            it = m.get("inputTokenLimit")
            ot = m.get("outputTokenLimit")
            lim = f" | input {it}, output {ot}" if it else ""
            print(f"  {mid:<28}{disp}{lim}")
    else:
        for mid, note in _GEMINI_STATIC_MODELS:
            print(f"  {mid:<28}{note}")
    print(f"{Fore.CYAN}Note: the 'flash' models are usually free. If one reports 'limit: 0' (no "
          f"free quota), try another flash, or use the 'google' engine (free without a key).{Style.RESET_ALL}")


def anthropic_proofread(events, target_lang, api_key, model, batch=40):
    """Proofreading of subtitles via Claude. Returns a list of texts of the same
    length. On a fatal error (4xx) it stops proofreading and leaves the rest unchanged."""
    out = []
    stop = None
    for i in range(0, len(events), batch):
        chunk = events[i:i + batch]
        if stop is None:
            numbered = "\n".join(f"{k + 1}. {c['text'].replace(chr(10), ' / ')}" for k, c in enumerate(chunk))
            prompt = (f"You are a professional proofreader of movie subtitles in the language '{target_lang}'. "
                      "Fix grammar, typos and unnatural phrasing of the machine translation, "
                      "preserve the MEANING, ORDER and NUMBER of items and do not translate into another language. "
                      "Separate a multi-line subtitle with ' / '. Return ONLY the numbered lines in the same "
                      "order and count, without comments.\n\n" + numbered)
            try:
                content = anthropic_messages(prompt, api_key, model)
                fixed = _parse_numbered(content or "", len(chunk))
            except _FatalAPIError as e:
                log_warn(f"Claude proofreading stopped: {e} (check --anthropic-model and the key).")
                stop = True
                fixed = [c["text"] for c in chunk]
            except Exception as e:
                log_warn(f"Claude proofreading failed at block {i // batch + 1}: {e}")
                fixed = [c["text"] for c in chunk]
        else:
            fixed = [c["text"] for c in chunk]
        for c, t in zip(chunk, fixed):
            out.append(t.replace(" / ", "\n") if t else c["text"])
    return out


def llm_proofread(events, target_lang, api_url, api_key, model, batch=40):
    """Proofreading via an OpenAI-compatible API (/chat/completions). Returns a
    list of texts of the same length; on a block error it keeps the original."""
    import urllib.request
    import urllib.error
    out = []
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    stop = None
    for i in range(0, len(events), batch):
        chunk = events[i:i + batch]
        if stop is not None:
            out.extend(c["text"] for c in chunk)
            continue
        numbered = "\n".join(f"{k + 1}. {c['text'].replace(chr(10), ' / ')}" for k, c in enumerate(chunk))
        prompt = (f"You are a professional proofreader of movie subtitles in the language '{target_lang}'. "
                  "Fix grammar, typos and unnatural phrasing of the machine translation, "
                  "preserve the MEANING, ORDER and NUMBER of items. Do not translate into another language. "
                  "Return ONLY the numbered lines in the same order and count (number. text), "
                  "separate a multi-line subtitle with ' / '. Without any comments.\n\n" + numbered)
        body = json.dumps({"model": model, "temperature": 0.2,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        try:
            req = urllib.request.Request(api_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            fixed = _parse_numbered(content, len(chunk))
        except urllib.error.HTTPError as e:
            detail = _http_error_detail(e)
            log_warn(f"LLM proofreading stopped: HTTP {e.code}: {detail or e.reason} "
                     "(check --llm-api, --llm-model and the key).")
            stop = True
            fixed = [c["text"] for c in chunk]
        except Exception as e:
            log_warn(f"AI proofreading failed at block {i // batch + 1}: {e}")
            fixed = [c["text"] for c in chunk]
        for c, t in zip(chunk, fixed):
            out.append(t.replace(" / ", "\n") if t else c["text"])
    return out


def apply_proofread(events, provider, target_lang, args):
    """Applies proofreading per the provider ('rules'/'llm'/'anthropic'/'off')."""
    if provider == "rules":
        for e in events:
            e["text"] = clean_subtitle_text(e["text"])
    elif provider == "llm":
        key = getattr(args, "llm_key", None) or os.environ.get("OPENAI_API_KEY")
        url = getattr(args, "llm_api", None) or "https://api.openai.com/v1/chat/completions"
        model = getattr(args, "llm_model", None) or "gpt-4o-mini"
        if not key:
            log_warn("LLM proofreading skipped - missing API key.")
            return events
        fixed = llm_proofread(events, target_lang, url, key, model)
        for e, t in zip(events, fixed):
            if t:
                e["text"] = t
    elif provider == "anthropic":
        key = getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
        model = getattr(args, "anthropic_model", None) or "claude-sonnet-4-6"
        if not key:
            log_warn("Proofreading via Claude skipped - missing Anthropic API key.")
            return events
        fixed = anthropic_proofread(events, target_lang, key, model)
        for e, t in zip(events, fixed):
            if t:
                e["text"] = t
    return events


def _parse_numbered(content, n):
    res = [None] * n
    for line in content.split("\n"):
        m = re.match(r"\s*(\d+)[.)]\s*(.*)", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n:
                res[idx] = m.group(2).strip()
    return res  # caller handles None? -> replace below


# --- OpenSubtitles -----------------------------------------------------

def opensubtitles_hash(path):
    """Official OpenSubtitles moviehash (size + 64 kB from the start and end)."""
    import struct
    fmt = "<q"
    size = struct.calcsize(fmt)
    filesize = os.path.getsize(path)
    if filesize < 65536 * 2:
        return None
    h = filesize
    with open(path, "rb") as f:
        for _ in range(65536 // size):
            (val,) = struct.unpack(fmt, f.read(size))
            h = (h + val) & 0xFFFFFFFFFFFFFFFF
        f.seek(filesize - 65536, 0)
        for _ in range(65536 // size):
            (val,) = struct.unpack(fmt, f.read(size))
            h = (h + val) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


class OpenSubtitles:
    BASE = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key, ua="video_tool v1.0", username=None, password=None):
        self.key = api_key
        self.ua = ua
        self.user = username
        self.password = password
        self.token = None

    def _req(self, method, path, body=None, auth=False):
        import urllib.request
        headers = {"Api-Key": self.key, "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": self.ua}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.BASE + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def login(self):
        if not (self.user and self.password):
            return False
        try:
            d = self._req("POST", "/login", {"username": self.user, "password": self.password})
            self.token = d.get("token")
            return bool(self.token)
        except Exception as e:
            log_warn(f"OpenSubtitles login failed: {e}")
            return False

    def search(self, languages, moviehash=None, query=None):
        from urllib.parse import urlencode
        params = {"languages": languages}
        if moviehash:
            params["moviehash"] = moviehash
        if query:
            params["query"] = query
        try:
            d = self._req("GET", "/subtitles?" + urlencode(params))
            return d.get("data", [])
        except Exception as e:
            log_warn(f"OpenSubtitles search failed: {e}")
            return []

    def download_srt(self, file_id):
        try:
            d = self._req("POST", "/download", {"file_id": file_id}, auth=True)
        except Exception as e:
            log_warn(f"OpenSubtitles /download failed (account/limit?): {e}")
            return None
        url = d.get("link")
        if not url:
            log_warn("OpenSubtitles: no download link returned (download limit reached, or login required).")
            return None
        try:
            import urllib.request
            # OpenSubtitles rejects requests without a proper User-Agent (HTTP 403),
            # so the link fetch must carry one too - not just the API calls.
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with urllib.request.urlopen(req, timeout=60) as r:
                return _decode_subtitle_bytes(r.read())
        except Exception as e:
            log_warn(f"OpenSubtitles file download failed: {e}")
            return None


def fetch_opensubtitles_events(video, lang, api_key, username=None, password=None, pick="downloads"):
    """Finds and downloads a human-subtitle match in language `lang`.
    pick: 'downloads' = most downloaded, 'rating' = best rated,
    'ask' = offer a selection from the list. Returns events or None.
    Downloading requires an account (username/password)."""
    client = OpenSubtitles(api_key, username=username, password=password)
    moviehash = None
    try:
        moviehash = opensubtitles_hash(str(video))
    except Exception:
        pass
    results = client.search(lang, moviehash=moviehash, query=None if moviehash else Path(video).stem)
    if not results and moviehash:
        results = client.search(lang, query=Path(video).stem)
    if not results:
        log_info(f"{Path(video).name}: OpenSubtitles - no match in '{lang}'.")
        return None

    def _attr(r, k, d=0):
        return r.get("attributes", {}).get(k, d) or d

    if pick == "ask" and len(results) > 1:
        ranked = sorted(results, key=lambda r: _attr(r, "download_count"), reverse=True)[:10]
        labels = []
        for r in ranked:
            a = r.get("attributes", {})
            rel = a.get("release", a.get("feature_details", {}).get("title", "?"))
            labels.append(f"{str(rel)[:50]}  | downloads {a.get('download_count', 0)} "
                          f"| rating {a.get('ratings', 0)} | hi={a.get('hearing_impaired', False)} "
                          f"| fps {a.get('fps', '?')}")
        idx = ask_pick(f"{Path(video).name}: choose a subtitle version:", labels, default=0)
        best = ranked[idx]
    elif pick == "rating":
        best = max(results, key=lambda r: _attr(r, "ratings"))
    else:
        best = max(results, key=lambda r: _attr(r, "download_count"))

    files = best.get("attributes", {}).get("files", [])
    if not files:
        return None
    file_id = files[0].get("file_id")
    client.login()  # download requires a token (account)
    srt_text = client.download_srt(file_id)
    if not srt_text:
        return None
    tmp = tempfile.mkdtemp()
    try:
        p = Path(tmp) / "dl.srt"
        p.write_text(srt_text, encoding="utf-8")
        return parse_srt(p, strict=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _affine_sync_args(base_args):
    import copy
    a = copy.copy(base_args)
    a.method = "affine"
    a.translate = "off"
    return a


# ----------------------------------------------------------------------
# Configuration (config.json) - API keys and default options
# ----------------------------------------------------------------------
#
# --config runs a wizard that asks only about what you want to enable
# (DeepL / OpenSubtitles / AI proofreading via Claude or OpenAI / default
# language) and saves it into config.json next to the script. The config is
# loaded automatically at startup. Value priority: command-line argument >
# environment variable > config.json > default. Everything is OPTIONAL - the
# script works entirely without a config and without online features.

# (config_key, args_attr, env_var, is_secret)
CONFIG_FIELDS = [
    ("deepl_key", "deepl_key", "DEEPL_API_KEY", True),
    ("opensubtitles_key", "opensubtitles_key", "OPENSUBTITLES_API_KEY", True),
    ("opensubtitles_user", "opensubtitles_user", "OPENSUBTITLES_USER", False),
    ("opensubtitles_password", "opensubtitles_password", "OPENSUBTITLES_PASSWORD", True),
    ("anthropic_key", "anthropic_key", "ANTHROPIC_API_KEY", True),
    ("anthropic_model", "anthropic_model", None, False),
    ("gemini_key", "gemini_key", "GEMINI_API_KEY", True),
    ("gemini_model", "gemini_model", None, False),
    ("llm_key", "llm_key", "OPENAI_API_KEY", True),
    ("llm_api", "llm_api", None, False),
    ("llm_model", "llm_model", None, False),
    ("out_lang", "out_lang", None, False),
    ("tmdb_key", "tmdb_key", "TMDB_API_KEY", True),
    ("tmdb_bearer", "tmdb_bearer", "TMDB_BEARER", True),
    ("omdb_key", "omdb_key", "OMDB_API_KEY", True),
    ("meta_lang", "meta_lang", None, False),
    ("subs_langs", "subs_langs", None, False),
]


def load_config():
    """Returns the config section from the unified store."""
    return load_store().get("config", {})


def save_config(cfg):
    """Saves the config section into the unified store (keeps presets)."""
    store = load_store()
    store["config"] = cfg
    save_store(store)


def apply_config_to_args(args, cfg, force=False):
    """Fills args with values that were not given on the command line.
    Priority: CLI (already set) > environment variable > saved config.
    force=True overrides args to match the saved config exactly - used right after
    the config wizard so that edits take effect in the SAME running session
    (otherwise a change would only apply after a restart)."""
    for ckey, attr, env, _secret in CONFIG_FIELDS:
        if not force and getattr(args, attr, None):
            continue  # given on the CLI
        val = (os.environ.get(env) if env else None) or cfg.get(ckey)
        if val:
            setattr(args, attr, val)
        elif force:
            setattr(args, attr, None)   # reflect deletions in-session
    # Gemini akceptuje i GOOGLE_API_KEY
    if not getattr(args, "gemini_key", None):
        g = os.environ.get("GOOGLE_API_KEY")
        if g:
            args.gemini_key = g


def _mask(s):
    if not s:
        return "(nenastaveno)"
    s = str(s)
    return (s[:4] + "..." + s[-2:]) if len(s) > 8 else "----"


def _config_location_wizard(args):
    """Lets the user choose WHERE the settings file is stored (folder or full .json
    path; UNC / mapped drive / ~ / env vars / Linux paths supported). Empty resets
    to the default (next to the script). Existing settings are copied to the new
    location. No effect when CONFIG_STORE_PATH is hard-coded in the script."""
    global _STORE_PATH
    if str(CONFIG_STORE_PATH).strip():
        log_info(f"Config location is fixed by CONFIG_STORE_PATH in the script: {current_store_path()}")
        return
    if _STORE_PATH:
        return   # a one-off --config-file was given; don't touch the pointer
    loc = current_store_path()
    if not ask_yes_no(f"Change WHERE settings are stored?  (now: {loc})", default_no=True):
        return
    log_info("Enter a FOLDER or a full .json path. Supported:")
    log_info("  Windows: Z:\\Configs, \\\\server\\share\\dir (UNC), mapped drives, %APPDATA%\\...")
    log_info("  Linux:   /mnt/nas/video_tool (e.g. an SMB/CIFS mount), ~/...")
    log_info("  EMPTY = reset to the default (next to the script).")
    newraw = ask_text("Config path", _read_store_pointer() or "")
    old_full = load_store()   # read from the current location before switching
    _write_store_pointer(newraw.strip() if newraw and newraw.strip() else "")
    dest = current_store_path()
    if os.path.abspath(dest) == os.path.abspath(loc):
        log_info(f"Config location unchanged: {dest}")
        return
    if os.path.exists(dest):
        log_info(f"Using existing settings already present at: {dest}")
    else:
        try:
            d = os.path.dirname(dest)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            save_store(old_full)   # writes to the NEW location (config + presets)
            log_done(f"Settings moved to: {dest}")
        except Exception as e:
            log_warn(f"Could not write to the new location ({e}). Reverting to the previous one.")
            _write_store_pointer("")


def run_config(args):
    """Interactive wizard for configuring config.json. It asks only about what
    you want to enable; leaves unfilled ones alone. Keys are stored in the file
    in readable form - keep it safe."""
    _config_location_wizard(args)
    store_name = os.path.basename(current_store_path())
    cfg = load_config()
    print(f"{Fore.MAGENTA}=== Settings ({store_name}) ==={Style.RESET_ALL}")
    log_info(f"File: {current_store_path()}")
    if cfg:
        log_info("Loaded existing settings - an empty answer keeps the current value.")
    log_warn(f"Note: keys are stored in readable form. Keep {store_name} safe.")

    def ask_secret(label, ckey):
        cur = cfg.get(ckey)
        prompt = f"{label}" + (f" (now {_mask(cur)}, Enter = keep)" if cur else " (Enter = skip)")
        val = ask_text(prompt, "")
        if val:
            cfg[ckey] = val

    def ask_plain(label, ckey, default=""):
        cur = cfg.get(ckey, default)
        val = ask_text(label, cur or "")
        if val:
            cfg[ckey] = val
        elif ckey in cfg and not val and not cur:
            pass

    # DeepL
    if ask_yes_no("Set up DeepL (high-quality machine translation)?", default_no=not cfg.get("deepl_key")):
        ask_secret("DeepL API key", "deepl_key")

    # OpenSubtitles
    if ask_yes_no("Set up OpenSubtitles (downloading ready-made human subtitles)?",
                  default_no=not cfg.get("opensubtitles_key")):
        ask_secret("OpenSubtitles API key", "opensubtitles_key")
        if ask_yes_no("Also add an account (username/password) for DOWNLOADING?",
                      default_no=not cfg.get("opensubtitles_user")):
            ask_plain("OpenSubtitles username", "opensubtitles_user")
            ask_secret("OpenSubtitles heslo", "opensubtitles_password")
        cur_langs = [x.strip().lower() for x in (cfg.get("subs_langs") or "cs").split(",") if x.strip()]
        if _tui_supported():
            log_info("Choose default subtitle languages for the downloader (Space = toggle, Enter = confirm).")
            picked = _pick_languages_multi(cur_langs)
            if picked:
                cfg["subs_langs"] = ",".join(picked)
        else:
            _sl = ask_text("Default subtitle languages (codes, comma-separated, e.g. cs,en)",
                           ",".join(cur_langs))
            if _sl:
                cfg["subs_langs"] = ",".join(x.strip().lower() for x in _sl.split(",") if x.strip())

    # AI proofreading / translation
    if ask_yes_no("Set up Google Gemini (FREE AI TRANSLATION, recommended for Czech)?",
                  default_no=not cfg.get("gemini_key")):
        ask_secret("Gemini API key (free at aistudio.google.com)", "gemini_key")
        _gm = ask_gemini_model("Model Gemini", cfg.get("gemini_model") or "gemini-2.5-flash",
                               type("A", (), {"gemini_key": cfg.get("gemini_key")
                                              or os.environ.get("GEMINI_API_KEY")
                                              or os.environ.get("GOOGLE_API_KEY")})())
        if _gm:
            cfg["gemini_model"] = _gm
    if ask_yes_no("Set up AI via Claude / an OpenAI-compatible API (translation and proofreading)?",
                  default_no=not (cfg.get("anthropic_key") or cfg.get("llm_key"))):
        which = ask_pick("Which AI provider to set up?",
                         ["Anthropic (Claude)", "OpenAI-compatible (OpenAI/local)", "both"], default=0)
        if which in (0, 2):
            ask_secret("Anthropic API key", "anthropic_key")
            _am = ask_anthropic_model("Claude model", cfg.get("anthropic_model") or "claude-sonnet-4-6",
                                      type("A", (), {"anthropic_key": cfg.get("anthropic_key")
                                                     or os.environ.get("ANTHROPIC_API_KEY")})())
            if _am:
                cfg["anthropic_model"] = _am
        if which in (1, 2):
            ask_secret("OpenAI API key", "llm_key")
            ask_plain("API URL (chat/completions)", "llm_api", "https://api.openai.com/v1/chat/completions")
            ask_plain("Model", "llm_model", "gpt-4o-mini")

    # default language
    if ask_yes_no("Set a default target language for --translate-subs?",
                  default_no=not cfg.get("out_lang")):
        _v = ask_language("Default target language (code, e.g. cs)", cfg.get("out_lang") or "cs")
        if _v:
            cfg["out_lang"] = _v

    # TMDB (online recognition + Plex naming for the renamer)
    if ask_yes_no("Set up TMDB (online show/movie recognition + Plex naming in the renamer)?",
                  default_no=not (cfg.get("tmdb_key") or cfg.get("tmdb_bearer"))):
        log_info("Free key at themoviedb.org -> Settings -> API (Developer plan). Either works:")
        log_info("  - v3 API key (a short string), or")
        log_info("  - v4 Read Access Token (a long token; paste it as the Bearer below).")
        ask_secret("TMDB API key (v3)", "tmdb_key")
        ask_secret("TMDB Read Access Token (v4, optional)", "tmdb_bearer")
        _ml = ask_language("Metadata language for the ONLINE RENAMER (code, e.g. cs, en)", cfg.get("meta_lang") or "cs")
        if _ml:
            cfg["meta_lang"] = _ml
        if ask_yes_no("Also add OMDb (IMDb IDs, optional)?", default_no=not cfg.get("omdb_key")):
            ask_secret("OMDb API key", "omdb_key")

    if ask_yes_no("Delete some saved value?", default_no=True):
        keys = list(cfg.keys())
        if keys:
            labels = [f"{k} = {_mask(cfg[k]) if 'key' in k or 'password' in k else cfg[k]}" for k in keys]
            labels.append("(nic nemazat)")
            idx = ask_pick("Co vymazat?", labels, default=len(labels) - 1)
            if idx < len(keys):
                cfg.pop(keys[idx], None)

    save_config(cfg)
    apply_config_to_args(args, cfg, force=True)   # apply the edits to THIS session right away
    print()
    log_done(f"Saved to {current_store_path()}")
    enabled = []
    if cfg.get("gemini_key"):
        enabled.append("Gemini")
    if cfg.get("deepl_key"):
        enabled.append("DeepL")
    if cfg.get("opensubtitles_key"):
        enabled.append("OpenSubtitles" + ("+account" if cfg.get("opensubtitles_user") else ""))
    if cfg.get("anthropic_key"):
        enabled.append("Claude")
    if cfg.get("llm_key"):
        enabled.append("OpenAI")
    if cfg.get("tmdb_key") or cfg.get("tmdb_bearer"):
        enabled.append("TMDB" + ("+OMDb" if cfg.get("omdb_key") else ""))
    log_info("Active: " + (", ".join(enabled) if enabled else "(no online features)"))


def run_test_api(args):
    """Quick API test: sends a trivial request and prints the EXACT response/error
    (including the server body). Helps reveal the cause of errors like HTTP 400."""
    import urllib.request
    import urllib.error
    # diagnostics: where are settings read from, and what got loaded?
    _p = current_store_path()
    print(f"{Fore.MAGENTA}=== API test ==={Style.RESET_ALL}")
    log_info(f"Settings file: {_p}")
    if os.path.exists(_p):
        _cfg = load_config()
        if _cfg:
            log_info(f"Loaded {len(_cfg)} setting(s): {', '.join(sorted(_cfg))}")
        else:
            log_warn("The settings file exists but contains no config values.")
    else:
        log_warn("The settings file was NOT found at this path (using defaults/none).")
        log_info("Tip: set the location via --config (Change WHERE settings are stored) "
                 "or the CONFIG_STORE_PATH constant.")
    tested = 0

    akey = getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
    amodel = getattr(args, "anthropic_model", None) or "claude-sonnet-4-6"
    if akey:
        tested += 1
        log_info(f"Test Anthropic (Claude) - model '{amodel}'...")
        try:
            txt = anthropic_messages("Reply with a single word: OK.", akey, amodel, max_tokens=16)
            log_done(f"Anthropic OK. Response: {txt!r}")
        except _FatalAPIError as e:
            log_warn(f"Anthropic FAILED: {e}")
            log_warn("If the message mentions 'model', fix --anthropic-model (--config). "
                     "Otherwise the problem is in this specific field/parameter.")
        except Exception as e:
            log_warn(f"Anthropic FAILED: {e}")
    else:
        log_info("Anthropic key not set - skipping.")

    gkey = (getattr(args, "gemini_key", None) or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY"))
    gmodel = getattr(args, "gemini_model", None) or "gemini-2.5-flash"
    if gkey:
        tested += 1
        log_info(f"Test Google Gemini - model '{gmodel}'...")
        try:
            txt = gemini_generate("Reply with a single word: OK.", gkey, gmodel)
            log_done(f"Gemini OK. Response: {txt!r}")
        except _FatalAPIError as e:
            log_warn(f"Gemini FAILED: {e}")
        except Exception as e:
            log_warn(f"Gemini FAILED: {e}")
    else:
        log_info("Gemini key not set - skipping.")

    okey = getattr(args, "llm_key", None) or os.environ.get("OPENAI_API_KEY")
    if okey:
        tested += 1
        url = getattr(args, "llm_api", None) or "https://api.openai.com/v1/chat/completions"
        omodel = getattr(args, "llm_model", None) or "gpt-4o-mini"
        log_info(f"Testing OpenAI-compatible API ({url}, model '{omodel}')...")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {okey}"}
        body = json.dumps({"model": omodel, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "Reply with: OK"}]}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            log_done(f"OpenAI OK. Response: {data['choices'][0]['message']['content']!r}")
        except urllib.error.HTTPError as e:
            log_warn(f"OpenAI FAILED: HTTP {e.code}: {_http_error_detail(e)}")
        except Exception as e:
            log_warn(f"OpenAI FAILED: {e}")

    if not tested:
        log_warn("No AI key is set. Set it via --config, --anthropic-key or --llm-key.")


def run_translate_subs(args):
    """Interactive wizard: the source is either a subtitle track from VIDEOS
    (extracted), or directly EXISTING subtitle files in the directory
    (one/several/all). It obtains the target language (OpenSubtitles and/or
    machine translation + proofreading), optionally fixes readability and saves .srt."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Translate subtitles and save .srt ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")

    recursive = ask_yes_no("Search subdirectories too?", default_no=True)

    src_type = ask_pick(
        "What do you want to translate?",
        ["Subtitle tracks from VIDEOS in the directory (extract from video)",
         "Existing SUBTITLE FILES in the directory (.srt)"],
        default=0,
        help=["From videos: for each video it extracts the chosen subtitle track and translates it "
              "(with 'auto' it may download ready ones from OpenSubtitles instead of translating).",
              "From subtitle files: translates existing .srt in the directory directly - you can pick "
              "one, several, or all. No video or tools needed."])

    jobs = []  # list of (path, kind) kind = "video" | "sub"
    args.track_id = None
    dry = preset_dryrun()
    if src_type == 0:
        videos = [] if dry else collect_videos(directory, recursive)
        if not videos and not dry:
            die("No videos in the directory. Choose 'subtitle files', or use --auto for synchronization.")
        _hdr = []
        if videos:
            log_info(f"Found {len(videos)} videos.")
            mkvmerge_bin, _, _, _ = _resolve_tools_for_extract(args, Path(videos[0]))
            try:
                st = mkvmerge_tracks(mkvmerge_bin, Path(videos[0]), "subtitles") if mkvmerge_bin else []
            except (Exception, SystemExit):
                st = []
            if st:
                _hdr.append(f"{Fore.CYAN}Subtitle tracks in the sample ({Path(videos[0]).name}):{Style.RESET_ALL}")
                for t in st:
                    _hdr.append(f"   #{t['id']}  {t['lang']}  {t['codec']}  {t.get('title', '')}")
                _hdr.append("")
        # track selection - FIXED choices (works even without video and is portable to a preset)
        ti = ask_pick("Which subtitle track to take from the video?",
                      ["by LANGUAGE (I'll enter a code - robust for the whole folder)",
                       "the first suitable subtitle track",
                       "a specific track ID (I'll enter a number)"], default=0, header=_hdr or None,
                      help=["Takes the track in the given language from each video (even if it has a different ID elsewhere).",
                            "Takes the first subtitle track of the video.",
                            "Extracts the track with a specific ID number (the same number for all videos)."])
        if ti == 0:
            args.ref_lang = norm_lang(ask_language("Source track language (eng/cze/...)", "eng") or None)
            args.track_id = None
        elif ti == 1:
            args.ref_lang = None
            args.track_id = None
        else:
            _tid = ask_text("Track ID (number)", "2")
            args.track_id = int(_tid) if str(_tid).strip().isdigit() else None
            args.ref_lang = None
        jobs = [(v, "video") for v in videos]
    else:
        subs = [] if dry else collect_srts(directory, recursive)
        if not subs and not dry:
            die("There are no .srt files in the directory.")
        sel = ask_pick(f"Which subtitle files to translate?{'' if dry else ' (found %d)' % len(subs)}",
                       ["all in the directory", "only some by name (I'll enter a filter)"], default=0,
                       help=["Translates all .srt in the directory.",
                             "Translates only files whose name contains the given text (e.g. 'S01')."])
        if sel == 0:
            chosen = subs
        else:
            pat = (ask_text("Part of the file name (filter; empty = all)", "") or "").lower()
            chosen = [s for s in subs if pat in os.path.basename(s).lower()] or subs
        jobs = [(s, "sub") for s in chosen]
        if not dry:
            log_info(f"Selected {len(jobs)} subtitle files to translate.")

    # target language
    out_lang = (ask_language("Into which language to translate (code, e.g. cs/en/de)",
                             getattr(args, "out_lang", None) or "cs") or "cs").lower()

    # source strategy (OpenSubtitles only makes sense for videos)
    if src_type == 0:
        si = ask_pick("Where to get the target subtitles from?",
                      ["auto - first try ready human ones (OpenSubtitles), otherwise machine translation",
                       "machine translation of the extracted track only",
                       "only download ready ones from OpenSubtitles"], default=0,
                      help=["auto: first tries to download ready human subtitles from OpenSubtitles "
                            "(best quality); if there are none/not possible, machine-translates the extracted track.",
                            "machine translation only: always translates the extracted subtitle track from the video "
                            "(keeps the original timing, matches the video).",
                            "OpenSubtitles only: uses only downloaded human subtitles; if there are none, "
                            "the video is skipped."])
        strategy = ["auto", "mt", "opensubtitles"][si]
    else:
        strategy = "mt"  # a subtitle file is simply translated

    engine = mt_key = mt_model = None
    if strategy in ("auto", "mt"):
        ei = ask_pick("Machine translator:",
                      ["gemini - FREE AI quality (Google AI Studio key) - recommended for Czech",
                       "deepl  - excellent quality (API key, free tier)",
                       "google - completely free without a key, good quality (modern Google endpoint)",
                       "claude - AI via the Anthropic API (paid)",
                       "argos  - offline, free (lower quality)"], default=0,
                      help=["gemini: AI translation from Google, FREE with an API key from aistudio.google.com "
                            "(generous free limit). Best quality/price for Czech.",
                            "deepl: top quality, requires a key (has a free tier ~500k chars/month).",
                            "google: completely FREE and without a key. Uses the same modern Google "
                            "Translate endpoint as the site translatesubtitles.co (batched, HTML-aware). With "
                            "sentence joining the result is decent.",
                            "claude: very high quality, paid (per token).",
                            "argos: fully offline, free, but noticeably lower quality."])
        engine = ["gemini", "deepl", "google", "claude", "argos"][ei]
        if engine == "gemini":
            mt_key = (getattr(args, "gemini_key", None) or os.environ.get("GEMINI_API_KEY")
                      or os.environ.get("GOOGLE_API_KEY")
                      or ask_text("Gemini API key (free at aistudio.google.com)", ""))
            args.gemini_key = args.gemini_key or mt_key
            mt_model = getattr(args, "gemini_model", None) or ask_gemini_model(
                "Model Gemini", "gemini-2.5-flash", args)
            args.gemini_model = args.gemini_model or mt_model
        elif engine == "deepl":
            mt_key = (getattr(args, "deepl_key", None) or os.environ.get("DEEPL_API_KEY")
                      or ask_text("DeepL API key", ""))
        elif engine == "claude":
            mt_key = (getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
                      or ask_text("Anthropic API key", ""))
            mt_model = getattr(args, "anthropic_model", None) or ask_anthropic_model("Claude model", "claude-sonnet-4-6", args)
            args.anthropic_key = args.anthropic_key or mt_key
            args.anthropic_model = args.anthropic_model or mt_model

    os_key = os_user = os_pw = None
    os_pick = "downloads"
    if src_type == 0 and strategy in ("auto", "opensubtitles"):
        os_key = (getattr(args, "opensubtitles_key", None) or os.environ.get("OPENSUBTITLES_API_KEY")
                  or ask_text("OpenSubtitles API key (empty = skip OpenSubtitles)", ""))
        os_user = getattr(args, "opensubtitles_user", None) or os.environ.get("OPENSUBTITLES_USER")
        os_pw = getattr(args, "opensubtitles_password", None) or os.environ.get("OPENSUBTITLES_PASSWORD")
        if os_key and not os_user and ask_yes_no("Do you have an OpenSubtitles account (needed for DOWNLOADING)?", default_no=True):
            os_user = ask_text("Username", "")
            os_pw = ask_text("Heslo", "")
        if os_key:
            pp = ask_pick("When multiple subtitle versions are found:",
                          ["most downloaded (automatically)",
                           "best rated (automatically)",
                           "choose manually for each video"], default=0)
            os_pick = ["downloads", "rating", "ask"][pp]
        if os_key and not os_user:
            log_warn("Without an OpenSubtitles account you can only search, not download - with 'auto' it "
                     "falls back to machine translation.")

    pi = ask_pick("Proofreading of the result:",
                  ["rules  - fast rule-based cleanup (free)",
                   "off    - none",
                   "claude - AI proofreading via the Anthropic API (key)",
                   "llm    - AI proofreading via an OpenAI-compatible API (key)"], default=0,
                  help=["rules: free, offline. Unifies spaces/punctuation, wraps long lines. "
                        "Does not change meaning.",
                        "off: leaves the translation as is.",
                        "claude: AI proofreading of grammar and naturalness via the Anthropic API "
                        "(paid per token).",
                        "llm: AI proofreading via an OpenAI-compatible API (OpenAI or a local server)."])
    proofread = ["rules", "off", "anthropic", "llm"][pi]
    if proofread == "llm":
        args.llm_api = (getattr(args, "llm_api", None)
                        or ask_text("API URL (chat/completions)", "https://api.openai.com/v1/chat/completions"))
        args.llm_key = getattr(args, "llm_key", None) or os.environ.get("OPENAI_API_KEY") or ask_text("API key", "")
        args.llm_model = getattr(args, "llm_model", None) or ask_text("Model", "gpt-4o-mini")
    elif proofread == "anthropic":
        args.anthropic_key = (getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
                              or ask_text("Anthropic API key", ""))
        args.anthropic_model = getattr(args, "anthropic_model", None) or ask_anthropic_model("Claude model", "claude-sonnet-4-6", args)

    sync_os = (src_type == 0 and strategy != "mt") and ask_yes_no(
        "Optionally time-align downloaded (human) subtitles to the video (affine)?", default_no=False)

    # readability fix (extending short subtitles) on the result
    args.fix_short_duration = False
    _ask_readability(args, [j[0] for j in jobs if j[1] == "sub"][:5])

    overwrite = ask_yes_no("Overwrite existing output .srt?", default_no=True)

    print()
    log_info(f"Target language: {out_lang} | source: {'videos' if src_type == 0 else 'subtitle files'}"
             + (f"/{strategy}" if src_type == 0 else "")
             + (f" | translator: {engine}" if engine else "")
             + f" | proofreading: {proofread}"
             + (" | readability: yes" if getattr(args, "fix_short_duration", False) else ""))
    _confirm = "Save this preset?" if dry else f"Run for {len(jobs)} items?"
    if not ask_yes_no(_confirm, default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()
    if dry:
        log_done("Preset created (nothing was executed). You'll find it in the Presets menu.")
        return

    done = skipped = 0
    for path, kind in jobs:
        p = Path(path)
        out_path = p.with_name(p.stem + f".{out_lang}.srt")
        if kind == "sub" and out_path.resolve() == p.resolve():
            out_path = p.with_name(p.stem + f".{out_lang}.tr.srt")
        if out_path.exists() and not overwrite:
            log_info(f"{p.name}: output already exists - skipping.")
            skipped += 1
            continue

        events = None
        source_used = None

        if kind == "video":
            if strategy in ("auto", "opensubtitles") and os_key:
                events = fetch_opensubtitles_events(p, out_lang, os_key, os_user, os_pw, pick=os_pick)
                if events:
                    source_used = "opensubtitles"
                    if sync_os:
                        ref_events, _ = extract_subtitle_events(args, p, args.track_id, args.ref_lang)
                        if ref_events:
                            log_info(f"{p.name}: aligning downloaded subtitles to the video (affine)...")
                            events = run_alignment(_affine_sync_args(args), ref_events, ref_events, events)
            if events is None and strategy in ("auto", "mt"):
                _vtext = probe_text_subtitle_tracks(args, p)
                _init = None
                if args.track_id is not None:
                    _init = next((t for t in _vtext if t["id"] == args.track_id), None)
                if _init is None and args.ref_lang:
                    _init = next((t for t in _vtext if t["lang"].lower().startswith(str(args.ref_lang).lower())), None)
                if _init is None and _vtext:
                    _init = _vtext[0]
                if _init is not None:
                    src_events, _chosen = extract_with_fallback(args, p, _init, _vtext,
                                                                interactive=not preset_is_replaying())
                else:
                    src_events = None
                if src_events:
                    log_info(f"{p.name}: translating {len(src_events)} subtitles into '{out_lang}' ({engine})...")
                    translated = translate_events_to(src_events, engine, out_lang, mt_key, mt_model)
                    if translated:
                        changed = sum(1 for a, b in zip(src_events, translated)
                                      if a["text"].replace("\n", " ").strip() != b["text"].replace("\n", " ").strip())
                        if changed < max(1, int(0.05 * len(translated))):
                            log_warn(f"{p.name}: translation failed (translated {changed}/{len(translated)}) - "
                                     f"NOT saving untranslated text as '{out_lang}'. Check key/model/engine.")
                            skipped += 1
                            continue
                        events = translated
                        source_used = f"mt:{engine}"
        else:  # kind == "sub"
            try:
                src_events = parse_srt(p, strict=False)
            except Exception as e:
                log_warn(f"{p.name}: cannot load ({e}) - skipping.")
                skipped += 1
                continue
            sl = detect_sub_language(src_events)
            if sl and sl == out_lang:
                log_info(f"{p.name}: source is already in language '{out_lang}' - skipping.")
                skipped += 1
                continue
            log_info(f"{p.name}: translating {len(src_events)} subtitles into '{out_lang}' ({engine})...")
            translated = translate_events_to(src_events, engine, out_lang, mt_key, mt_model)
            if translated:
                changed = sum(1 for a, b in zip(src_events, translated)
                              if a["text"].replace("\n", " ").strip() != b["text"].replace("\n", " ").strip())
                if changed < max(1, int(0.05 * len(translated))):
                    log_warn(f"{p.name}: translation failed (translated {changed}/{len(translated)}) - "
                             f"NOT saving untranslated text. Check key/model/engine.")
                    skipped += 1
                    continue
                events = translated
                source_used = f"mt:{engine}"

        if not events:
            log_warn(f"{p.name}: could not obtain target subtitles - skipping.")
            skipped += 1
            continue

        if proofread != "off":
            apply_proofread(events, proofread, out_lang, args)

        if getattr(args, "fix_short_duration", False):
            cps, floor, gap, overhead = resolve_speed_params(args)
            events, n_ext = fix_short_durations(
                events, min_cps=cps, min_duration_floor=floor, min_gap=gap, line_overhead=overhead)
            if n_ext:
                log_info(f"{p.name}: readability - extended {n_ext} short subtitles")

        try:
            write_srt(events, out_path)
            log_done(f"{p.name} -> {out_path.name}  ({source_used})")
            done += 1
        except Exception as e:
            log_warn(f"{p.name}: write failed: {e}")
            skipped += 1

    print()
    log_done(f"Done: {done} saved, {skipped} skipped (of {len(jobs)}).")


def _extract_fix_one_video(video, tracks, can_ask, label_fn, kind_word="subtitle"):
    """When a video's track layout doesn't match the batch selection (different
    IDs / track count), re-scan and let the user pick tracks for THIS video via a
    checklist. Returns a list of tracks to extract, or None to skip. Prompts only
    when interactive; the choice is NOT recorded into a preset (it is video-specific)."""
    if not can_ask or not tracks:
        return None
    try:
        if not sys.stdin.isatty():
            return None
    except Exception:
        return None
    hdr = [f"{Fore.YELLOW}{video.name}: different track layout - the batch selection "
           f"matched no track here.{Style.RESET_ALL}",
           f"{Fore.CYAN}This video's {kind_word} tracks:{Style.RESET_ALL}", ""]
    labels = [label_fn(t) for t in tracks]
    acts = [f"Extract CHECKED {kind_word} tracks for THIS video",
            f"ALL {kind_word} tracks in this video",
            "Skip this video"]
    global _PRESET_MODE
    _saved_mode = _PRESET_MODE
    _PRESET_MODE = None   # do not record/replay this per-video fix into a preset
    try:
        a, checked = ask_checklist("Fix the selection for this video", labels, acts, header=hdr)
    finally:
        _PRESET_MODE = _saved_mode
    if a == 2:
        return None
    if a == 1:
        return list(tracks)
    picked = [tracks[i] for i in checked if 0 <= i < len(tracks)]
    return picked or None


def _subtitle_track_label(t):
    """Label for a subtitle track from mkvmerge_tracks (id/lang/codec/title)."""
    return f"#{t['id']}  {t['lang']:4} {t['codec']:16} {t.get('title', '')}".rstrip()


def run_extract_subs(args, minimal=False):
    """Wizard: extracts subtitle tracks from videos (mkv/mp4/...) into .srt. Tracks
    are detected directly from the video; you interactively pick which (by language,
    specific tracks from the sample, or all text ones).
    minimal=True: clean extraction without prompts for cleanup/readability (used as
    an intermediate step for another mode that does the edits itself)."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Extract subtitles from videos (into .srt) ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")

    dry = preset_dryrun() and not minimal
    recursive = bool(getattr(args, "recursive", False)) if minimal else ask_yes_no(
        "Search subdirectories too?", default_no=True)
    videos = [] if dry else collect_videos(directory, recursive)
    if not videos and not dry:
        die("No videos (mkv/mp4/...) in the directory.")
    sample = None
    sub_tracks = []
    text_tracks = []
    if videos:
        log_info(f"Found {len(videos)} videos.")
        mkvmerge_bin, mkvextract_bin, ffmpeg_bin, _is_mkv = _resolve_tools_for_extract(args, Path(videos[0]))
        if not mkvmerge_bin:
            die("Could not find mkvmerge (mkvtoolnix) to read tracks. Install mkvtoolnix, "
                "or check the connection for automatic download.")
        args.mkvmerge = args.mkvmerge or mkvmerge_bin
        if mkvextract_bin:
            args.mkvextract = args.mkvextract or mkvextract_bin
        if ffmpeg_bin:
            args.ffmpeg = args.ffmpeg or ffmpeg_bin
        for cand in videos:
            try:
                st = mkvmerge_tracks(mkvmerge_bin, Path(cand), "subtitles")
            except (Exception, SystemExit):
                continue
            if st:
                sample = Path(cand)
                sub_tracks = st
                break
        if sample is None:
            die("Could not read subtitle tracks from any video (corrupted files, "
                "or the videos have no subtitles).")
        log_info(f"Found {len(sub_tracks)} tracks in the sample {sample.name}.")
        text_tracks = [t for t in sub_tracks if is_text_codec(t["codec"])]
        if not text_tracks:
            log_warn("The sample video has only image-based subtitles (PGS/VobSub). You can still try other "
                     "videos via selection by language - text tracks are extracted, image ones skipped.")

    _hdr = [f"{Fore.MAGENTA}=== Extract subtitles from videos ==={Style.RESET_ALL}"]
    if videos:
        _hdr.append(f"{Fore.CYAN}Found {len(videos)} videos. Sample: {sample.name}{Style.RESET_ALL}")
        _img = [t for t in sub_tracks if not is_text_codec(t["codec"])]
        if _img:
            _hdr.append(f"{Fore.YELLOW}(Image-based tracks can't go to .srt: "
                        + ", ".join(f"#{t['id']} {t['lang']}" for t in _img) + f"){Style.RESET_ALL}")
    _hdr.append("")

    item_labels = [f"#{t['id']}  {t['lang']:4} {t['codec']:16} {t.get('title', '')}".rstrip()
                   for t in text_tracks]
    actions = ["Extract CHECKED tracks (check them above with space)",
               "by LANGUAGE (I'll type codes - robust for the whole folder)",
               "ALL text subtitle tracks from each video"]
    act, checked = ask_checklist("Which subtitle tracks to extract?", item_labels, actions, header=_hdr)

    want_langs = None
    want_keys = None
    if act == 0:                       # extract the specific checked tracks
        if checked and text_tracks:
            sample_sel = _track_selectors(text_tracks)
            want_keys = [sample_sel[i][0] for i in checked if 0 <= i < len(sample_sel)]
        if not want_keys:
            log_info("Nothing checked - taking ALL text tracks.")
    elif act == 1:                     # by language
        raw = ask_text("Language codes separated by commas (e.g. eng,cze,ger; empty = all)", "")
        want_langs = [x.strip().lower() for x in raw.replace(" ", "").split(",") if x.strip()] or None
    # act == 2 -> all text ones (want_langs=None, want_keys=None)

    do_clean = False if minimal else ask_yes_no(
        "Rule-based text cleanup (spaces, punctuation, wrapping long lines)?", default_no=True)
    args.fix_short_duration = False
    if not minimal:
        _ask_readability(args, [])
    overwrite = True if minimal else ask_yes_no("Overwrite existing output .srt?", default_no=True)

    print()
    seltxt = ("languages: " + ",".join(want_langs)) if want_langs else \
             (f"{len(want_keys)} checked track(s)" if want_keys else "all text ones")
    log_info(f"Selection: {seltxt} | videos: {len(videos)}"
             + (" | cleanup" if do_clean else "")
             + (" | readability" if getattr(args, 'fix_short_duration', False) else ""))
    _cf = "Save this preset?" if dry else f"Run for {len(videos)} videos?"
    if not minimal and not ask_yes_no(_cf, default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()
    if dry:
        log_done("Preset created (nothing was executed). You'll find it in the Presets menu.")
        return

    done = skipped = wrote = 0
    for vid in videos:
        v = Path(vid)
        try:
            vtracks = mkvmerge_tracks(args.mkvmerge, v, "subtitles")
        except (Exception, SystemExit) as e:
            log_warn(f"{v.name}: cannot read tracks ({e}) - skipping.")
            skipped += 1
            continue
        vtext = [t for t in vtracks if is_text_codec(t["codec"])]
        if want_langs is not None:
            sel = [t for t in vtext if any(t["lang"].lower().startswith(l) for l in want_langs)]
        elif want_keys is not None:
            vkeymap = {k: t for k, _l, t in _track_selectors(vtext)}
            sel = [vkeymap[k] for k in want_keys if k in vkeymap]
        else:
            sel = vtext
        if not sel:
            # different track layout for this video -> re-scan and let the user fix it
            can_ask = not minimal and not preset_is_replaying() and not getattr(args, "yes", False)
            fixed = _extract_fix_one_video(v, vtext, can_ask, _subtitle_track_label, "subtitle")
            if not fixed:
                log_warn(f"{v.name}: no matching text track - skipping.")
                skipped += 1
                continue
            sel = fixed

        done_ids = set()
        any_written = False
        for t in sel:
            if t["id"] in done_ids:
                continue
            out_path = v.with_name(v.stem + f".{_track_tag(t, vtext)}.srt")
            if out_path.exists() and not overwrite:
                log_info(f"{out_path.name}: already exists - skipping.")
                done_ids.add(t["id"])
                continue
            events, chosen = extract_with_fallback(args, v, t, vtext, done_ids=done_ids,
                                                   interactive=not minimal and not preset_is_replaying())
            if not events:
                continue
            # if the fallback picked another track, name it by the actual track
            if chosen and chosen["id"] != t["id"]:
                out_path = v.with_name(v.stem + f".{_track_tag(chosen, vtext)}.srt")
                if out_path.exists() and not overwrite:
                    log_info(f"{out_path.name}: already exists - skipping.")
                    done_ids.add(chosen["id"])
                    continue
            done_ids.add(chosen["id"] if chosen else t["id"])
            if do_clean:
                for e in events:
                    e["text"] = clean_subtitle_text(e["text"])
            if getattr(args, "fix_short_duration", False):
                cps, floor, gap, overhead = resolve_speed_params(args)
                events, _n = fix_short_durations(events, min_cps=cps, min_duration_floor=floor,
                                                 min_gap=gap, line_overhead=overhead)
            ch = chosen or t
            try:
                write_srt(events, out_path)
                log_done(f"{v.name}: track #{ch['id']} ({ch['lang']}, {ch['codec']}) -> {out_path.name} "
                         f"({len(events)} subtitles)")
                wrote += 1
                any_written = True
            except Exception as e:
                log_warn(f"{v.name}: writing {out_path.name} failed: {e}")
        if any_written:
            done += 1
        else:
            skipped += 1

    print()
    log_done(f"Done: {wrote} .srt files from {done} videos ({skipped} videos skipped).")


# ======================================================================
# Working with tracks in containers (mkv/mp4) - port of standalone scripts:
# importing subtitles, removing tracks, default track, renaming subtitles.
# ======================================================================
_LANG3_FROM2 = {
    "en": "eng", "cs": "cze", "sk": "slo", "de": "ger", "fr": "fre", "es": "spa",
    "it": "ita", "pt": "por", "nl": "dut", "pl": "pol", "ru": "rus", "uk": "ukr",
    "ja": "jpn", "ko": "kor", "zh": "chi", "hu": "hun", "ro": "rum", "sv": "swe",
    "no": "nor", "da": "dan", "fi": "fin", "el": "gre", "tr": "tur", "ar": "ara",
    "he": "heb", "th": "tha", "vi": "vie", "id": "ind", "hi": "hin", "bg": "bul",
    "hr": "hrv", "sr": "srp", "sl": "slv", "et": "est", "lv": "lav", "lt": "lit",
    "fil": "fil", "ms": "msa",
}
_LANG3_ALIAS = {"ces": "cze", "deu": "ger", "fra": "fre", "nld": "dut", "ron": "rum",
                "slk": "slo", "zho": "chi", "ell": "gre", "may": "msa"}
_LANG3_NAME = {
    "eng": "English", "cze": "Czech", "slo": "Slovak", "ger": "German", "fre": "French",
    "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "dut": "Dutch", "pol": "Polish",
    "rus": "Russian", "ukr": "Ukrainian", "jpn": "Japanese", "kor": "Korean", "chi": "Chinese",
    "hun": "Hungarian", "rum": "Romanian", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "gre": "Greek", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew",
    "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian", "hin": "Hindi", "bul": "Bulgarian",
    "hrv": "Croatian", "srp": "Serbian", "slv": "Slovenian", "fil": "Filipino", "msa": "Malay",
    "und": "(unknown)",
}
_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
_LANG_TOKEN_RE = re.compile(r"^[A-Za-z]{2,3}$")
_FLAG_TOKENS = {"forced", "sdh", "cc", "hi", "foreign", "full", "default"}


def _canon3(lang):
    l = (lang or "").strip().lower()
    if not l or l == "und":
        return "und"
    if l in _LANG3_ALIAS:
        return _LANG3_ALIAS[l]
    if len(l) == 2:
        return _LANG3_FROM2.get(l, l)
    return l


def _lang3_name(code3):
    return _LANG3_NAME.get(code3, code3.upper())


def _episode_key(name):
    m = _EPISODE_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _fmt_ep(k):
    return f"S{k[0]:02d}E{k[1]:02d}"


def _find_mkvpropedit(mkvmerge_bin):
    if mkvmerge_bin:
        d = os.path.dirname(mkvmerge_bin)
        for n in ("mkvpropedit", "mkvpropedit.exe"):
            c = os.path.join(d, n)
            if os.path.isfile(c):
                return c
    return find_tool(["mkvpropedit", "mkvpropedit.exe"])


def _ensure_mkv_tools(args, target_dir, need_propedit=False):
    """Finds mkvmerge/mkvextract/mkvpropedit; if missing, downloads MKVToolNix into
    .mkvtoolnix (same as ffmpeg). Returns (mkvmerge, mkvextract, mkvpropedit)."""
    mm = getattr(args, "mkvmerge", None) or find_tool(["mkvmerge", "mkvmerge.exe"])
    me = getattr(args, "mkvextract", None) or find_tool(["mkvextract", "mkvextract.exe"])
    mp = _find_mkvpropedit(mm)
    if not mm or not me or (need_propedit and not mp):
        try:
            mm2, me2 = ensure_mkvtoolnix(target_dir, allow_download=not getattr(args, "no_mkvtoolnix_download", False))
            mm = mm or mm2
            me = me or me2
            mp = mp or _find_mkvpropedit(mm)
        except Exception as e:
            log_warn(f"Could not ensure MKVToolNix: {e}")
    if mm:
        args.mkvmerge = args.mkvmerge or mm
    if me:
        args.mkvextract = args.mkvextract or me
    return mm, me, mp


def _mkv_probe_full(mkvmerge_bin, video):
    """Returns {'audio':[...], 'subs':[...]}; each track has id, lang, name, codec,
    default, forced, a selektor sel (a1/s1... pro mkvpropedit)."""
    try:
        out = subprocess.run([mkvmerge_bin, "-J", str(video)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        data = json.loads(out.stdout or "{}")
    except Exception:
        return {"audio": [], "subs": []}
    audio, subs = [], []
    ai = si = 0
    for t in data.get("tracks", []):
        pr = t.get("properties", {}) or {}
        rec = {"id": t["id"], "lang": _canon3(pr.get("language")), "name": pr.get("track_name") or "",
               "codec": t.get("codec") or "", "default": bool(pr.get("default_track")),
               "forced": bool(pr.get("forced_track"))}
        if t.get("type") == "audio":
            ai += 1
            rec["sel"] = f"a{ai}"
            rec["channels"] = pr.get("audio_channels")
            audio.append(rec)
        elif t.get("type") == "subtitles":
            si += 1
            rec["sel"] = f"s{si}"
            subs.append(rec)
    return {"audio": audio, "subs": subs}


def _track_selectors(tracks):
    """For a list of tracks (of one kind) in ONE video, returns [(key, label, track)].
    Distinguishes even same languages - by track name, forced, and when nothing
    else works, by order. key = (lang, name_lower, forced, ordinal) is stable across episodes."""
    seen = {}
    prelim = []
    for t in tracks:
        lang = t.get("lang", "und") or "und"
        name = (t.get("name") or "").strip()
        forced = bool(t.get("forced"))
        base = (lang, name.lower(), forced)
        ordi = seen.get(base, 0)
        seen[base] = ordi + 1
        prelim.append((t, lang, name, forced, base, ordi))
    base_total = {}
    for _t, _l, _n, _f, base, _o in prelim:
        base_total[base] = base_total.get(base, 0) + 1
    out = []
    for t, lang, name, forced, base, ordi in prelim:
        key = (lang, name.lower(), forced, ordi)
        extra = []
        if name:
            extra.append(name)
        if forced:
            extra.append("forced")
        label = _lang3_name(lang) + (" - " + ", ".join(extra) if extra else "")
        if base_total[base] > 1:
            label += f"  #{ordi + 1}"
        cod = t.get("codec", "")
        if cod:
            label += f"  [{cod}]"
        out.append((key, label, t))
    return out


def _track_by_key(tracks, key):
    for k, _label, t in _track_selectors(tracks):
        if k == key:
            return t
    return None


def _aggregate_track_keys(infos, kind):
    """Across all videos, groups tracks by the distinguishing key (not just language).
    Returns a sorted list (key, label, count)."""
    order = []
    meta = {}
    for info in infos.values():
        for key, label, _t in _track_selectors(info.get(kind, [])):
            if key not in meta:
                meta[key] = [label, 0]
                order.append(key)
            meta[key][1] += 1
    return [(k, meta[k][0], meta[k][1]) for k in order]
    videos, subs = [], []
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            if ext in (".mkv", ".mp4"):
                videos.append(full)
            elif ext in sub_exts:
                subs.append(full)
    return sorted(videos), sorted(subs)


def _parse_sub_meta(sub_name, forced_lang=None):
    """From a subtitle file name, extracts (lang3, track_name, forced)."""
    stem = os.path.splitext(sub_name)[0]
    parts = stem.split(".")
    lang = forced_lang
    forced = sdh = False
    tail = []
    while parts and len(tail) < 2 and (parts[-1].lower() in _FLAG_TOKENS or _LANG_TOKEN_RE.match(parts[-1])):
        tail.insert(0, parts.pop())
    for tok in tail:
        low = tok.lower()
        if low == "forced":
            forced = True
        elif low in ("sdh", "cc", "hi"):
            sdh = True
        elif _LANG_TOKEN_RE.match(tok) and lang is None:
            lang = low
    lang3 = "und" if lang is None else _canon3(lang)
    name = _lang3_name(lang3)
    if forced:
        name += " (Forced)"
    elif sdh:
        name += " (SDH)"
    return lang3, name, forced


# ---------------------------------------------------------------- import
def run_import_subs(args):
    """Muxes subtitle files into videos (paired via SxxExx) using mkvmerge; sets
    language, track name, forced and optionally default. The output is always MKV."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Insert (mux) subtitles into videos ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)
    dry = preset_dryrun()
    if not dry:
        mm, me, mp = _ensure_mkv_tools(args, directory)
        if not mm:
            die("Could not find mkvmerge (MKVToolNix), nor download it.")
        videos, subs = _collect_videos_subs(directory, recursive)
        if not videos:
            die("No videos (mkv/mp4) in the directory.")
        if not subs:
            die("No subtitle files (.srt/.ass/...) in the directory.")
        vmap = {}
        for v in videos:
            k = _episode_key(os.path.basename(v))
            if k and k not in vmap:
                vmap[k] = v
        pairs = {}
        for s in subs:
            k = _episode_key(os.path.basename(s))
            if k and k in vmap:
                pairs.setdefault(vmap[k], []).append(s)
        if not pairs:
            die("Could not pair subtitles with videos by SxxExx.")
        log_info(f"Paired {sum(len(x) for x in pairs.values())} subtitles to {len(pairs)} videos.")
    else:
        pairs = {}

    set_default = ask_yes_no("Set one subtitle track as the DEFAULT?", default_no=True)
    default_lang = None
    if set_default:
        default_lang = ask_language("Language of the default subtitles (code, e.g. cs)", "cs") or None
    forced_lang = None
    if ask_yes_no("Don't the files have a language in their name? Set one language for all?", default_no=True):
        forced_lang = ask_language("Subtitle language (code)", "cs") or None
    replace = ask_yes_no("Overwrite the original video with the resulting MKV? (otherwise next to it as <name>.muxed.mkv)", default_no=True)

    print()
    _cf = "Save this preset?" if dry else f"Run mux for {len(pairs)} videos?"
    if not ask_yes_no(_cf, default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()
    if dry:
        log_done("Preset created (nothing was executed). You'll find it in the Presets menu.")
        return

    done = failed = 0
    for v in sorted(pairs):
        vp = Path(v)
        sub_list = pairs[v]
        metas = []
        for s in sub_list:
            lang3, name, forced = _parse_sub_meta(os.path.basename(s), forced_lang)
            metas.append((s, lang3, name, forced))
        chosen = None
        if set_default and metas:
            if default_lang:
                dl = _canon3(default_lang)
                for i, (_s, l3, _n, _f) in enumerate(metas):
                    if l3 == dl:
                        chosen = i
                        break
            if chosen is None:
                chosen = 0
        out_path = vp.with_suffix(".mkv") if replace else vp.with_name(vp.stem + ".muxed.mkv")
        tmp_out = str(vp.with_name(vp.stem + ".muxing.tmp.mkv"))
        cmd = [mm, "-o", tmp_out]
        if chosen is not None:
            for t in _mkv_probe_full(mm, v)["subs"]:
                cmd += ["--default-track", f"{t['id']}:no"]
        cmd += [str(v)]
        for i, (subfile, lang3, name, forced) in enumerate(metas):
            cmd += ["--language", f"0:{lang3}", "--track-name", f"0:{name}",
                    "--default-track", "0:yes" if (chosen is not None and i == chosen) else "0:no",
                    "--forced-track", "0:yes" if forced else "0:no", str(subfile)]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode >= 2 or not os.path.exists(tmp_out):
            tail = " | ".join([l for l in (res.stdout or "").splitlines() if l.strip()][-3:])
            log_warn(f"{vp.name}: mkvmerge failed: {tail}")
            failed += 1
            try:
                os.path.exists(tmp_out) and os.remove(tmp_out)
            except OSError:
                pass
            continue
        try:
            if replace:
                if vp.suffix.lower() != ".mkv":
                    os.remove(str(v))  # replacing the original mp4 with mkv
                os.replace(tmp_out, str(out_path))
            else:
                os.replace(tmp_out, str(out_path))
            log_done(f"{vp.name}: inserted {len(metas)} subtitles -> {out_path.name}")
            done += 1
        except OSError as e:
            log_warn(f"{vp.name}: replacement failed: {e}")
            failed += 1
    print()
    log_done(f"Done: {done} videos, {failed} errors.")


# --------------------------------------------------------- remove tracks
def run_remove_tracks(args):
    """Removes the chosen audio/subtitle tracks from MKV (mkvmerge -c copy)."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Remove audio/subtitle tracks from MKV ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)
    mm, me, mp = _ensure_mkv_tools(args, directory)
    if not mm:
        die("Could not find mkvmerge (MKVToolNix), nor download it.")

    mkvs = [v for v in collect_videos(directory, recursive) if Path(v).suffix.lower() == ".mkv"]
    if not mkvs:
        die("No .mkv files (this mode only supports Matroska).")
    log_info(f"Found {len(mkvs)} MKV. Reading tracks...")
    infos = {v: _mkv_probe_full(mm, v) for v in mkvs}

    def ask_remove_tracks(kind_label, entries):
        # entries: [(key, label, count)]
        if not entries:
            return set()
        items = [f"{label}  ({n}×)" for _k, label, n in entries]
        actions = [f"Remove checked {kind_label.lower()}", "remove nothing"]
        act, checked = ask_checklist(f"Which {kind_label.lower()} to REMOVE? (check with space)",
                                     items, actions,
                                     header=[f"{Fore.CYAN}{kind_label} (same languages distinguished too):{Style.RESET_ALL}"])
        if act != 0 or not checked:
            return set()
        return {entries[i][0] for i in checked if 0 <= i < len(entries)}

    a_entries = _aggregate_track_keys(infos, "audio")
    s_entries = _aggregate_track_keys(infos, "subs")
    rem_audio = ask_remove_tracks("Audio tracks", a_entries)
    rem_subs = ask_remove_tracks("Subtitle tracks", s_entries)
    if not rem_audio and not rem_subs:
        log_warn("Nothing to remove.")
        return
    replace = ask_yes_no("Overwrite the originals? (otherwise saved into the 'trimmed' subfolder)", default_no=True)

    print()
    _al = {lbl for k, lbl, _n in a_entries if k in rem_audio}
    _sl = {lbl for k, lbl, _n in s_entries if k in rem_subs}
    log_info("To remove -- audio: " + (", ".join(sorted(_al)) or "none")
             + " | subtitles: " + (", ".join(sorted(_sl)) or "none"))
    if not ask_yes_no(f"Run for {len(mkvs)} MKV?", default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()

    outdir = None
    if not replace:
        outdir = os.path.join(directory, "trimmed")
        os.makedirs(outdir, exist_ok=True)

    done = skipped = 0
    for v in mkvs:
        vp = Path(v)
        info = infos[v]
        a_sel = _track_selectors(info["audio"])
        s_sel = _track_selectors(info["subs"])
        a_all = [t["id"] for t in info["audio"]]
        s_all = [t["id"] for t in info["subs"]]
        a_keep = [t["id"] for k, _l, t in a_sel if k not in rem_audio]
        s_keep = [t["id"] for k, _l, t in s_sel if k not in rem_subs]
        if a_keep == a_all and s_keep == s_all:
            log_info(f"{vp.name}: nothing to remove - skipping.")
            skipped += 1
            continue
        if a_all and not a_keep:
            log_warn(f"{vp.name}: removal would delete ALL audio - skipping.")
            skipped += 1
            continue
        out_path = vp if replace else Path(outdir) / vp.name
        tmp_out = str(vp.with_name(vp.stem + ".trim.tmp.mkv"))
        cmd = [mm, "-o", tmp_out]
        if a_all and not a_keep:
            cmd += ["--no-audio"]
        elif a_keep and len(a_keep) < len(a_all):
            cmd += ["--audio-tracks", ",".join(str(i) for i in a_keep)]
        if s_all and not s_keep:
            cmd += ["--no-subtitles"]
        elif s_keep and len(s_keep) < len(s_all):
            cmd += ["--subtitle-tracks", ",".join(str(i) for i in s_keep)]
        cmd += [str(v)]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode >= 2 or not os.path.exists(tmp_out):
            tail = " | ".join([l for l in (res.stdout or "").splitlines() if l.strip()][-3:])
            log_warn(f"{vp.name}: mkvmerge failed: {tail}")
            skipped += 1
            try:
                os.path.exists(tmp_out) and os.remove(tmp_out)
            except OSError:
                pass
            continue
        try:
            os.replace(tmp_out, str(out_path))
            log_done(f"{vp.name}: audio {len(a_keep)}/{len(a_all)}, subtitles {len(s_keep)}/{len(s_all)} "
                     f"-> {out_path.name if replace else 'trimmed/' + out_path.name}")
            done += 1
        except OSError as e:
            log_warn(f"{vp.name}: replacement failed: {e}")
            skipped += 1
    print()
    log_done(f"Done: {done} MKV modified, {skipped} skipped.")


# ---------------------------------------------------- set default tracks
def run_set_default(args):
    """Sets the default audio/subtitle track by language. MKV via mkvpropedit
    (in place), MP4 via ffmpeg (remux)."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Set the default track by language ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)
    mm, me, mp = _ensure_mkv_tools(args, directory, need_propedit=True)
    if not mm:
        die("Could not find mkvmerge (MKVToolNix), nor download it.")

    videos = collect_videos(directory, recursive)
    if not videos:
        die("No videos (mkv/mp4) in the directory.")
    has_mp4 = any(Path(v).suffix.lower() == ".mp4" for v in videos)
    ffmpeg_bin = None
    if has_mp4:
        ffmpeg_bin = getattr(args, "ffmpeg", None) or ensure_ffmpeg(directory, allow_download=not getattr(args, "no_ffmpeg_download", False))
    log_info(f"Found {len(videos)} videos. Reading tracks...")
    infos = {v: _mkv_probe_full(mm, v) for v in videos}

    def ask_track(kind_label, entries, allow_none):
        # entries: [(key, label, count)]
        if not entries:
            return "keep"
        labels = [f"{label}  ({n}×)" for _k, label, n in entries]
        labels.append("do not change (leave as is)")
        if allow_none:
            labels.append("none (clear all default flags)")
        i = ask_pick(f"Default {kind_label.lower()} - pick a track", labels, default=len(entries),
                     header=[f"{Fore.CYAN}{kind_label} (same languages distinguished too):{Style.RESET_ALL}"])
        if i < len(entries):
            return entries[i][0]      # key
        if i == len(entries):
            return "keep"
        return "none"

    a_choice = ask_track("Audio track", _aggregate_track_keys(infos, "audio"), allow_none=False)
    s_choice = ask_track("Subtitle track", _aggregate_track_keys(infos, "subs"), allow_none=True)
    if a_choice == "keep" and s_choice == "keep":
        log_warn("Nothing to set.")
        return

    print()

    def _sum(c):
        return "unchanged" if c == "keep" else ("none (clear default)" if c == "none" else "selected track")
    log_info(f"Default audio: {_sum(a_choice)} | default subtitles: {_sum(s_choice)}")
    if not ask_yes_no(f"Run for {len(videos)} videos?", default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()

    def desired_default(tracks, choice):
        """Returns {track_id -> bool}; sets default exactly on the selected track
        (by the distinguishing key), turns the others off."""
        if choice == "keep":
            return None
        target = None if choice == "none" else _track_by_key(tracks, choice)
        return {tr["id"]: (target is not None and tr["id"] == target["id"]) for tr in tracks}

    done = failed = 0
    for v in videos:
        vp = Path(v)
        info = infos[v]
        a_want = desired_default(info["audio"], a_choice)
        s_want = desired_default(info["subs"], s_choice)
        if vp.suffix.lower() == ".mkv":
            edits = []
            for kind, want in (("audio", a_want), ("subs", s_want)):
                if want is None:
                    continue
                for tr in info[kind]:
                    d = want.get(tr["id"])
                    if d is not None and bool(d) != bool(tr["default"]):
                        edits += ["--edit", f"track:{tr['sel']}", "--set", f"flag-default={1 if d else 0}"]
            if not edits:
                log_info(f"{vp.name}: unchanged.")
                continue
            res = subprocess.run([mp, str(v)] + edits, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode >= 2:
                log_warn(f"{vp.name}: mkvpropedit error.")
                failed += 1
            else:
                log_done(f"{vp.name}: default tracks set.")
                done += 1
        else:
            if not ffmpeg_bin:
                log_warn(f"{vp.name}: MP4 requires ffmpeg - skipping.")
                failed += 1
                continue
            disp = []
            for kind, tkey, want in (("audio", "a", a_want), ("subs", "s", s_want)):
                if want is None:
                    continue
                for rel, tr in enumerate(info[kind]):
                    flags = []
                    if want.get(tr["id"]):
                        flags.append("default")
                    if tr["forced"]:
                        flags.append("forced")
                    disp += [f"-disposition:{tkey}:{rel}", "+".join(flags) if flags else "0"]
            tmp = str(v) + ".deftmp.mp4"
            cmd = [ffmpeg_bin, "-y", "-i", str(v), "-map", "0", "-c", "copy"] + disp + ["-default_mode", "passthrough", tmp]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode != 0 or not os.path.exists(tmp):
                log_warn(f"{vp.name}: ffmpeg error.")
                failed += 1
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                continue
            try:
                os.replace(tmp, str(v))
                log_done(f"{vp.name}: default tracks set (remux).")
                done += 1
            except OSError as e:
                log_warn(f"{vp.name}: replacement failed: {e}")
                failed += 1
    print()
    log_done(f"Done: {done} videos, {failed} errors/skipped.")


# -------------------------------------------------------- rename subs
def run_rename_subs(args):
    """Renames subtitles (.srt) by video names (paired via SxxExx), keeping the
    language/forced suffix."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Rename subtitles by video names ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)

    if preset_dryrun():
        if ask_yes_no("Save this preset? (renaming happens only when run in the folder)", default_no=False):
            preset_flush_if_save()
            log_done("Preset created (nothing was executed). You'll find it in the Presets menu.")
        return

    videos, subs = _collect_videos_subs(directory, recursive, sub_exts=(".srt",))
    if not subs:
        die("No .srt subtitles in the directory.")
    vmap = {}
    for v in sorted(videos):
        k = _episode_key(os.path.basename(v))
        if k and k not in vmap:
            vmap[k] = v

    planned = []
    used = {}
    for s in sorted(subs):
        sdir = os.path.dirname(s)
        sname = os.path.basename(s)
        k = _episode_key(sname)
        if not k or k not in vmap:
            continue
        stem = sname[:-4]
        parts = stem.split(".")
        suffix = []
        while parts and len(suffix) < 2 and (parts[-1].lower() in _FLAG_TOKENS or _LANG_TOKEN_RE.match(parts[-1])):
            suffix.insert(0, parts.pop())
        vstem = os.path.splitext(os.path.basename(vmap[k]))[0]
        new_name = vstem + ("." + ".".join(suffix) if suffix else "") + ".srt"
        dst = os.path.join(sdir, new_name)
        if os.path.abspath(dst) == os.path.abspath(s) or dst in used or os.path.exists(dst):
            continue
        used[dst] = s
        planned.append((s, dst))

    if not planned:
        log_warn("Nothing to rename (either SxxExx or videos are missing, or they already match).")
        return
    print(f"\nRename plan ({len(planned)}):")
    for s, d in planned[:30]:
        print(f"  {os.path.basename(s)}\n   -> {os.path.basename(d)}")
    if len(planned) > 30:
        print(f"  ... and {len(planned) - 30} more")
    print()
    if not ask_yes_no(f"Rename {len(planned)} subtitles?", default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()
    done = 0
    for s, d in planned:
        try:
            os.rename(s, d)
            done += 1
        except OSError as e:
            log_warn(f"{os.path.basename(s)}: {e}")
    log_done(f"Done, renamed {done} subtitles.")



def run_transplant(args):
    """Wizard: replaces the MACHINE translation (good timing) with PROFESSIONAL
    text from a separate directory (a different release of the show, different
    episode splitting), the timing stays. Matched by CONTENT, same language."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Replace machine translation with professional (by content) ==={Style.RESET_ALL}")
    log_info(f"Machine subtitles (good timing): {os.path.abspath(directory)}")
    log_info("I'll take your machine/AI subtitles and replace the text with a professional translation "
             "of the same show from another directory. The TIMING stays yours (matches your release).")

    recursive = ask_yes_no("Search machine subtitles in subdirectories too?", default_no=True)
    dry = preset_dryrun()
    targets = [] if dry else collect_srts(directory, recursive)
    if not targets and not dry:
        die("There are no .srt (machine subtitles) in the folder.")
    sel = ask_pick(f"Which machine subtitles to process?{'' if dry else ' (found %d)' % len(targets)}",
                   ["all in the directory", "only some by name (I'll enter a filter)"], default=0)
    if sel == 0:
        chosen = targets
    else:
        pat = (ask_text("Part of the name (filter; empty = all)", "") or "").lower()
        chosen = [s for s in targets if pat in os.path.basename(s).lower()] or targets

    # directory with professional ("viki") subtitles
    if dry:
        viki_dir = ask_text("Directory with PROFESSIONAL ('viki') subtitles (enter a path; verified on run)", "").strip().strip('"')
    else:
        while True:
            viki_dir = ask_text("Directory with PROFESSIONAL ('viki') subtitles", "").strip().strip('"')
            if not viki_dir:
                log_warn("This cannot work without a directory of professional subtitles. Cancelled.")
                return
            if os.path.isdir(viki_dir):
                break
            log_warn(f"Directory '{viki_dir}' does not exist - try again (or Enter = quit).")
    viki_rec = ask_yes_no("Search 'viki' subtitles in subdirectories too?", default_no=True)
    if not dry:
        pool, viki_files = _load_reference_pool(viki_dir, viki_rec)
        if not pool:
            die("There are no .srt in the professional subtitles directory.")
        log_info(f"Professional pool: {len(pool)} subtitles from {len(viki_files)} files "
                 f"(episode splitting doesn't matter - matched by content).")
        lp = detect_sub_language(pool)
        lt = detect_srt_file_language(chosen[0]) if chosen else None
        if lp and lt and lp != lt:
            log_warn(f"Note: the machine subtitles look like '{lt}', the professional ones like '{lp}'. "
                     "This function matches by text, so BOTH must be in the SAME language. "
                     "If they differ, the result won't make sense.")
            if not ask_yes_no("Continue anyway?", default_no=True):
                return
    else:
        pool = []

    ti = ask_pick("How strictly to match (quality vs. coverage trade-off)?",
                  ["conservative - only confident matches (fewest errors, fewer replacements)",
                   "balanced - a reasonable compromise (recommended)",
                   "aggressive - more replacements, but higher risk of errors"], default=1,
                  help=["conservative: replaces only where the match is very confident. Safest, "
                        "but covers fewer subtitles with professional text; the rest stays machine.",
                        "balanced: a reasonable balance between the number of replacements and reliability.",
                        "aggressive: replaces more subtitles, but the chance of a wrong match grows "
                        "(mainly with short/repeating lines)."])
    min_sim = [0.65, 0.55, 0.45][ti]

    args.fix_short_duration = False
    _ask_readability(args, chosen[:5])

    overwrite = ask_yes_no("Overwrite the original machine file? (otherwise saved next to it as <name>.pro.srt)",
                           default_no=True)

    print()
    log_info(f"Matching threshold: {min_sim:.2f} | files: {len(chosen)} | pro pool: {len(pool)} "
             + ("| readability: yes " if getattr(args, 'fix_short_duration', False) else "")
             + ("| overwrite original" if overwrite else "| saving as .pro.srt"))
    _cf = "Save this preset?" if dry else f"Run for {len(chosen)} files?"
    if not ask_yes_no(_cf, default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()
    if dry:
        log_done("Preset created (nothing was executed). You'll find it in the Presets menu.")
        return

    log_info("Preparing the professional pool (one-time)...")
    transplant, _M = build_transplanter(pool)

    done = skipped = 0
    tot_repl = tot_cues = 0
    for path in chosen:
        p = Path(path)
        try:
            ev = parse_srt(p, strict=False)
        except Exception as e:
            log_warn(f"{p.name}: cannot load ({e}) - skipping.")
            skipped += 1
            continue
        new_ev, n_repl, n_tot = transplant(ev, min_sim=min_sim)
        if getattr(args, "fix_short_duration", False):
            cps, floor, gap, overhead = resolve_speed_params(args)
            new_ev, _n = fix_short_durations(new_ev, min_cps=cps, min_duration_floor=floor,
                                             min_gap=gap, line_overhead=overhead)
        out_path = p if overwrite else p.with_name(p.stem + ".pro.srt")
        try:
            write_srt(new_ev, out_path)
            pct = (100 * n_repl / n_tot) if n_tot else 0
            log_done(f"{p.name} -> {out_path.name}: nahrazeno {n_repl}/{n_tot} ({pct:.0f}%) "
                     f"professional text, the rest left as machine.")
            done += 1
            tot_repl += n_repl
            tot_cues += n_tot
        except Exception as e:
            log_warn(f"{p.name}: write failed: {e}")
            skipped += 1

    print()
    pct = (100 * tot_repl / tot_cues) if tot_cues else 0
    log_done(f"Done: {done} files saved, {skipped} skipped. Total replaced "
             f"{tot_repl}/{tot_cues} subtitles ({pct:.0f}%).")
    if pct < 20:
        log_warn("Low coverage - either the translations are quite different, it's another release/show, "
                 "or try 'aggressive'. Also check that both are in the same language.")


def run_resync_pro(args):
    """Wizard: takes PROFESSIONAL subtitles from another directory (different
    release/episode splitting) and RE-TIMES them to your machine subtitles'
    timing. Result = 100% professional text with correct timing for your video."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Re-time professional subtitles to my timing (100% pro text) ==={Style.RESET_ALL}")
    log_info(f"Your subtitles with good timing: {os.path.abspath(directory)}")
    log_info("I'll take a professional translation of the same show from another directory and re-time it to your "
             "timing. The WHOLE professional text stays, it just gets your video's timing.")

    recursive = ask_yes_no("Search your subtitles in subdirectories too?", default_no=True)
    dry = preset_dryrun()
    targets = [] if dry else collect_srts(directory, recursive)
    if not targets and not dry:
        die("There are no .srt in the folder (your machine subtitles with good timing).")
    sel = ask_pick(f"Which of your subtitles (the timing template) to use?{'' if dry else ' (found %d)' % len(targets)}",
                   ["all in the directory", "only some by name (I'll enter a filter)"], default=0)
    if sel == 0:
        chosen = targets
    else:
        pat = (ask_text("Part of the name (filter; empty = all)", "") or "").lower()
        chosen = [s for s in targets if pat in os.path.basename(s).lower()] or targets

    if dry:
        viki_dir = ask_text("Directory with PROFESSIONAL ('viki') subtitles (enter a path; verified on run)", "").strip().strip('"')
    else:
        while True:
            viki_dir = ask_text("Directory with PROFESSIONAL ('viki') subtitles", "").strip().strip('"')
            if not viki_dir:
                log_warn("This cannot work without a directory of professional subtitles. Cancelled.")
                return
            if os.path.isdir(viki_dir):
                break
            log_warn(f"Directory '{viki_dir}' does not exist - try again (or Enter = quit).")
    viki_rec = ask_yes_no("Search 'viki' subtitles in subdirectories too?", default_no=True)
    if not dry:
        pool, viki_files = _load_reference_pool(viki_dir, viki_rec, continuous=True)
        if not pool:
            die("There are no .srt in the professional subtitles directory.")
        log_info(f"Professional pool: {len(pool)} subtitles from {len(viki_files)} files "
                 f"(different episode splitting is fine - the right slice is found by content).")
        lp = detect_sub_language(pool)
        lt = detect_srt_file_language(chosen[0]) if chosen else None
        if lp and lt and lp != lt:
            log_warn(f"Note: your subtitles look like '{lt}', the professional ones like '{lp}'. Re-timing "
                     "matches by text, so BOTH should be in the same language.")
            if not ask_yes_no("Continue anyway?", default_no=True):
                return
    else:
        pool = []

    args.fix_short_duration = False
    _ask_readability(args, chosen[:5])
    overwrite = ask_yes_no("Overwrite my original file with the professional one? (otherwise saved next to it as <name>.pro.srt)",
                           default_no=True)

    print()
    if not dry:
        log_info(f"Files: {len(chosen)} | pro pool: {len(pool)} "
                 + ("| readability: yes " if getattr(args, 'fix_short_duration', False) else "")
                 + ("| overwrite original" if overwrite else "| saving as .pro.srt"))
    _cf = "Save this preset?" if dry else f"Run for {len(chosen)} files?"
    if not ask_yes_no(_cf, default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()
    if dry:
        log_done("Preset created (nothing was executed). You'll find it in the Presets menu.")
        return

    done = skipped = 0
    for path in chosen:
        p = Path(path)
        try:
            ev = parse_srt(p, strict=False)
        except Exception as e:
            log_warn(f"{p.name}: cannot load ({e}) - skipping.")
            skipped += 1
            continue
        out, n_anchors = retime_professional(ev, pool, min_sim=0.5)
        if out is None:
            log_warn(f"{p.name}: few matches ({n_anchors} anchors) - cannot re-time reliably, "
                     "skipping. (Are these really the same episodes and the same language?)")
            skipped += 1
            continue
        if getattr(args, "fix_short_duration", False):
            cps, floor, gap, overhead = resolve_speed_params(args)
            out, _n = fix_short_durations(out, min_cps=cps, min_duration_floor=floor,
                                          min_gap=gap, line_overhead=overhead)
        out_path = p if overwrite else p.with_name(p.stem + ".pro.srt")
        try:
            write_srt(out, out_path)
            log_done(f"{p.name} -> {out_path.name}: {len(out)} professional subtitles "
                     f"re-timed to your timing ({n_anchors} anchors).")
            done += 1
        except Exception as e:
            log_warn(f"{p.name}: write failed: {e}")
            skipped += 1

    print()
    log_done(f"Done: {done} files saved, {skipped} skipped.")
    log_info("Tip: if the timing drifts somewhere, keep the pro pool as complete as possible (all episodes "
             "of the series) so there are enough anchors; re-timing is more accurate that way.")


def process_single(args):
    """Processes ONE video file: extracts the reference (subtitles/audio),
    computes the timing with the chosen method and saves the result."""
    is_mkv_container = args.mkv.suffix.lower() in MKVEXTRACT_CONTAINER_EXTS
    need_sub_extraction = args.audio_mode in ("off", "combine")
    need_audio = args.audio_mode in ("replace", "combine")
    need_ffmpeg = need_audio or (need_sub_extraction and not is_mkv_container)

    mkvmerge_bin = args.mkvmerge or find_tool(["mkvmerge", "mkvmerge.exe"])
    mkvextract_bin = args.mkvextract or find_tool(["mkvextract", "mkvextract.exe"])
    need_mkvextract = need_sub_extraction and is_mkv_container
    if not mkvmerge_bin or (need_mkvextract and not mkvextract_bin):
        global MKVMERGE, MKVEXTRACT
        if args.mkvmerge:
            MKVMERGE = args.mkvmerge
        if args.mkvextract:
            MKVEXTRACT = args.mkvextract
        mkvmerge_bin, mkvextract_bin = ensure_mkvtoolnix(
            str(args.mkv.parent), allow_download=not args.no_mkvtoolnix_download
        )
        if mkvmerge_bin:
            log_info(f"mkvmerge: {mkvmerge_bin}")
        if mkvextract_bin:
            log_info(f"mkvextract: {mkvextract_bin}")

    if not mkvmerge_bin:
        die(
            "mkvmerge not found and automatic download failed / is disabled. "
            "Download and install MKVToolNix from https://mkvtoolnix.download/downloads.html#windows "
            "(the installer offers adding to PATH), or use --mkvmerge with the full path "
            "to mkvmerge.exe (usually C:\\Program Files\\MKVToolNix\\mkvmerge.exe). It is also used "
            "for MP4 only to list/identify tracks; the actual extraction from MP4 is done by ffmpeg."
        )
    if need_mkvextract and not mkvextract_bin:
        die(
            "mkvextract not found and automatic download failed / is disabled "
            "(needed to extract subtitles from .mkv/.webm). Install MKVToolNix or "
            "use --mkvextract with the full path to the .exe."
        )

    ffmpeg_bin = None
    if need_ffmpeg:
        global FFMPEG
        if args.ffmpeg:
            FFMPEG = args.ffmpeg
        ffmpeg_bin = ensure_ffmpeg(str(args.mkv.parent), allow_download=not args.no_ffmpeg_download)
        if not ffmpeg_bin:
            reasons = []
            if need_audio:
                reasons.append("audio analysis (VAD)")
            if need_sub_extraction and not is_mkv_container:
                reasons.append("extracting subtitles from MP4 (mkvextract only supports .mkv/.webm)")
            die(
                f"I need ffmpeg ({' and '.join(reasons)}) and automatic download "
                "failed / is disabled. Download it manually from https://www.gyan.dev/ffmpeg/builds/, "
                "unpack it into '.ffmpeg' next to this script, or pass --ffmpeg with the full path "
                "k ffmpeg.exe."
            )
        log_info(f"ffmpeg: {ffmpeg_bin}")

    sub_tracks = mkvmerge_tracks(mkvmerge_bin, args.mkv, "subtitles")
    audio_tracks = mkvmerge_tracks(mkvmerge_bin, args.mkv, "audio")

    if args.list_tracks or not args.subtitle_to_fix or not args.output:
        if not sub_tracks:
            print("No subtitle tracks found.")
        else:
            print(f"{Fore.MAGENTA}Available subtitle tracks:{Style.RESET_ALL}")
            for t in sub_tracks:
                print(f"  ID={t['id']:>3}  lang={t['lang']:<5} codec={t['codec']:<20} title={t['title']}")
        if not audio_tracks:
            print("No audio tracks found.")
        else:
            print(f"{Fore.MAGENTA}Available audio tracks:{Style.RESET_ALL}")
            for t in audio_tracks:
                print(f"  ID={t['id']:>3}  lang={t['lang']:<5} codec={t['codec']:<20} title={t['title']}")
        if not args.list_tracks:
            print("\nUsage: python video_tool.py video.mkv subtitles.srt output.srt [--ref-lang eng] [--audio-mode combine]")
        return

    if not args.subtitle_to_fix.exists():
        die(f"The subtitle file to fix does not exist: {args.subtitle_to_fix}")

    with tempfile.TemporaryDirectory() as tmpdir:
        ref_events_sub = []
        ref_events_audio = []

        if args.audio_mode in ("off", "combine"):
            chosen_sub = pick_reference_track(sub_tracks, args.ref_lang, args.track_id)
            log_info(f"Reference subtitle track: ID={chosen_sub['id']} lang={chosen_sub['lang']} codec={chosen_sub['codec']}")
            ref_srt_path = Path(tmpdir) / "reference.srt"
            if is_mkv_container:
                extract_subtitle_to_srt(mkvextract_bin, args.mkv, chosen_sub["id"], ref_srt_path)
            else:
                sub_position = [t["id"] for t in sub_tracks].index(chosen_sub["id"])
                extract_subtitle_via_ffmpeg(ffmpeg_bin, args.mkv, sub_position, ref_srt_path)
            ref_events_sub = parse_srt(ref_srt_path)
            log_info(f"Reference subtitles: {len(ref_events_sub)}")

        if args.audio_mode in ("replace", "combine"):
            chosen_audio = pick_audio_track(audio_tracks, args.audio_lang, args.audio_track_id)
            audio_position = [t["id"] for t in audio_tracks].index(chosen_audio["id"])
            log_info(f"Reference audio track: ID={chosen_audio['id']} lang={chosen_audio['lang']} codec={chosen_audio['codec']}")

            wav_path = Path(tmpdir) / "reference_audio.wav"
            log_info("Extracting and decoding the audio track (ffmpeg)...")
            extract_audio_wav(ffmpeg_bin, args.mkv, audio_position, wav_path)

            samples, sr = read_wav_mono(wav_path)
            log_info(f"Audio track: {len(samples) / sr:.1f} s, {sr} Hz - searching for speech segments (VAD)...")
            ref_events_audio = detect_speech_events(samples, sr, energy_percentile=args.vad_percentile)
            log_info(f"Detected {len(ref_events_audio)} speech segments")

        if args.audio_mode == "off":
            ref_events = ref_events_sub
        elif args.audio_mode == "replace":
            ref_events = ref_events_audio
        else:  # combine
            ref_events = sorted(ref_events_sub + ref_events_audio, key=lambda e: e["start"])
            log_info(f"Combined reference timeline: {len(ref_events)} anchors (subtitles + speech)")

        target_events = parse_srt(args.subtitle_to_fix)
        log_info(f"Subtitles to fix: {len(target_events)}")

        # real language detection FROM CONTENT (after extracting the reference) and a
        # warning on mismatch when a content method runs without translation
        if getattr(args, "method", "auto") in ("auto", "warp", "combo") \
                and getattr(args, "translate", "off") == "off" and ref_events_sub:
            tl = detect_sub_language(target_events)
            rl = detect_sub_language(ref_events_sub)
            if tl and rl and tl != rl:
                log_warn(f"Detected languages differ (to-fix='{tl}', reference='{rl}'). "
                         "Content matching (warp) will be weak - consider --translate google "
                         "(or --method affine). Without translation the affine phase is used.")

        corrected = run_alignment(args, ref_events, ref_events_sub, target_events)

        if args.fix_short_duration:
            cps, floor, gap, overhead = resolve_speed_params(args)
            corrected, n_extended = fix_short_durations(
                corrected, min_cps=cps, min_duration_floor=floor, min_gap=gap, line_overhead=overhead,
            )
            log_info(f"Extended {n_extended} subtitles with shortened display (using free space)")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_srt(corrected, args.output)

    log_done(f"Synchronized subtitles saved to: {args.output}")


# ----------------------------------------------------------------------
# Interactive wizards (--auto for a single file, --auto-all for a batch)
# ----------------------------------------------------------------------

SUB_EXTS_AUTO = (".srt", ".ass", ".ssa", ".vtt")


def collect_subs(directory):
    """Finds subtitle files in the directory (.srt/.ass/.ssa/.vtt and .orig
    reference variants like 'name.srt.orig')."""
    out = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for f in names:
        full = os.path.join(directory, f)
        if not os.path.isfile(full):
            continue
        low = f.lower()
        if low.endswith(SUB_EXTS_AUTO) or low.endswith(".orig"):
            out.append(full)
    return out


class WizardBack(Exception):
    """Raised when the wizard should be exited to the main menu."""
    pass


class _StepBack(WizardBack):
    """Raised on Esc in a question - goes back by ONE question (handled by
    run_with_back). When there is nowhere to go, behaves like WizardBack (main menu)."""
    pass


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    return _ANSI_RE.sub("", s)


_TUI_WINDOWS = os.name == "nt"
try:
    if _TUI_WINDOWS:
        import msvcrt as _msvcrt
    else:
        import termios as _termios
        import tty as _tty
    _HAS_RAW = True
except Exception:
    _HAS_RAW = False


def _tui_supported():
    try:
        return _HAS_RAW and sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _flush_input():
    """Discards unread bytes in the input (leftover escape sequences from arrows,
    extra Enter, etc.) - otherwise the next prompt would read them as unintended input."""
    try:
        if _TUI_WINDOWS:
            while _msvcrt.kbhit():
                _msvcrt.getch()
        else:
            _termios.tcflush(sys.stdin.fileno(), _termios.TCIFLUSH)
    except Exception:
        pass


def clear_screen():
    """Clears the screen (application mode). Without a TTY it does nothing."""
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
    except Exception:
        pass


# --- step back in the wizard (Esc): remembers answers and replays to the previous --
_BACK_ACTIVE = False
_BACK_REPLAY = []   # answers to replay from last time (up to the "frontier")
_BACK_NEW = []      # new answers entered in this pass
_BACK_POS = 0
_BACK_NO_PENDING = object()
_BACK_PENDING = _BACK_NO_PENDING   # previous answer of the question we are returning to


def _back_pending_take():
    """The previous answer of the FIRST interactive question after replay (the one
    we returned to via Esc); otherwise _BACK_NO_PENDING. Consumed only once."""
    global _BACK_PENDING
    if _BACK_PENDING is _BACK_NO_PENDING:
        return _BACK_NO_PENDING
    v = _BACK_PENDING
    _BACK_PENDING = _BACK_NO_PENDING
    return v


def _back_get():
    """During replay (after Esc) returns (True, saved_answer), otherwise (False, None)."""
    global _BACK_POS
    if _BACK_ACTIVE and _BACK_POS < len(_BACK_REPLAY):
        v = _BACK_REPLAY[_BACK_POS]
        _BACK_POS += 1
        return True, v
    return False, None


def _back_put(v):
    if _BACK_ACTIVE:
        _BACK_NEW.append(v)


def run_with_back(fn, args):
    """Runs the interactive wizard fn(args) with 'step back' (Esc) support.
    Answers are recorded; Esc in a question raises _StepBack and the wizard is
    replayed up to the PREVIOUS question. Esc on the first question -> WizardBack
    (main menu). During preset replay (--load) it's just a pass-through, no changes."""
    global _BACK_ACTIVE, _BACK_POS, _BACK_REPLAY, _BACK_NEW, _BACK_PENDING
    if preset_is_replaying():
        return fn(args)
    saved = (_BACK_ACTIVE, _BACK_POS, _BACK_REPLAY, _BACK_NEW, _BACK_PENDING)
    tape = []
    pending = _BACK_NO_PENDING
    try:
        while True:
            _BACK_REPLAY = tape
            _BACK_NEW = []
            _BACK_POS = 0
            _BACK_ACTIVE = True
            _BACK_PENDING = pending
            if _PRESET_MODE in ("save", "offer"):
                _PRESET_REC.clear()   # clean preset recording for the (final) pass
            try:
                return fn(args)
            except _StepBack:
                full = list(_BACK_REPLAY[:_BACK_POS]) + _BACK_NEW
                if not full:
                    raise WizardBack()
                pending = full[-1]    # previous answer -> gets pre-filled
                tape = full[:-1]      # and we return to that question
    finally:
        _BACK_ACTIVE, _BACK_POS, _BACK_REPLAY, _BACK_NEW, _BACK_PENDING = saved


def _read_line_tui(prompt_str, default="", secret=False, prefill=""):
    """Line input in raw mode with Esc support (= step back). Returns text or
    default (Enter). Esc raises _StepBack. 'prefill' = pre-typed editable text
    (returning via Esc keeps the previous answer)."""
    sys.stdout.write(prompt_str)
    buf = list(prefill or "")
    if buf:
        sys.stdout.write(("*" * len(buf)) if secret else "".join(buf))
    sys.stdout.flush()
    with _RawMode():
        _flush_input()
        while True:
            key = _read_key()
            if key == "enter":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(buf) if buf else default
            if key == "esc":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                raise _StepBack()
            if key == "backspace":
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                buf.append(key[1])
                sys.stdout.write("*" if secret else key[1])
                sys.stdout.flush()


def _read_key():
    """Reads one key; returns an action ('up'/'down'/.../'enter'/'esc'/...) or
    ('char', znak)."""
    if _TUI_WINDOWS:
        ch = _msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            c2 = _msvcrt.getch()
            return {b"H": "up", b"P": "down", b"K": "left", b"M": "right",
                    b"G": "home", b"O": "end", b"I": "pgup", b"Q": "pgdn"}.get(c2, "other")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x08":
            return "backspace"
        if ch == b"\x1b":
            return "esc"
        if ch == b"\x03":
            raise KeyboardInterrupt
        for enc in ("utf-8", "cp1250", "latin-1"):
            try:
                return ("char", ch.decode(enc))
            except Exception:
                continue
        return "other"
    else:
        import select
        fd = sys.stdin.fileno()

        def _avail(timeout=0.12):
            try:
                r, _, _ = select.select([fd], [], [], timeout)
                return bool(r)
            except Exception:
                return False

        def _rd():
            try:
                return os.read(fd, 1)
            except Exception:
                return b""

        b = _rd()
        if not b:
            return "other"
        c0 = b[0]
        if c0 == 0x1b:                      # ESC or the start of an escape sequence
            if not _avail():                # nothing else arrived -> a lone Esc
                return "esc"
            b2 = _rd()
            if b2 not in (b"[", b"O"):
                return "esc"
            seq = b""
            while True:
                c = _rd()
                if not c:
                    break
                seq += c
                if c.isalpha() or c == b"~" or len(seq) > 6:
                    break
                if not _avail(0.02):
                    break
            s = seq.decode("ascii", "ignore")
            return {"A": "up", "B": "down", "C": "right", "D": "left",
                    "H": "home", "F": "end", "1~": "home", "4~": "end",
                    "5~": "pgup", "6~": "pgdn"}.get(s, "esc")
        if c0 in (0x0d, 0x0a):
            return "enter"
        if c0 in (0x7f, 0x08):
            return "backspace"
        if c0 == 0x03:
            raise KeyboardInterrupt
        # assemble a UTF-8 multi-byte character (e.g. accented characters when searching)
        n_more = 3 if c0 >= 0xF0 else 2 if c0 >= 0xE0 else 1 if c0 >= 0xC0 else 0
        data = b
        for _ in range(n_more):
            if not _avail(0.05):
                break
            data += _rd()
        for enc in ("utf-8", "cp1250", "latin-1"):
            try:
                return ("char", data.decode(enc))
            except Exception:
                continue
        return "other"


class _RawMode:
    """Context manager for character (cbreak) terminal mode (Unix only).
    Uses cbreak (not full raw) so output processing (ONLCR) stays - otherwise
    '\\n' would not return to the start of the line and the menu would fall apart."""
    def __enter__(self):
        if not _TUI_WINDOWS:
            self.fd = sys.stdin.fileno()
            self.old = _termios.tcgetattr(self.fd)
            try:
                _tty.setcbreak(self.fd)
            except Exception:
                _tty.setraw(self.fd)
        return self

    def __exit__(self, *a):
        if not _TUI_WINDOWS:
            _termios.tcsetattr(self.fd, _termios.TCSADRAIN, self.old)


def interactive_menu(prompt, labels, default=0, allow_cancel=False, help=None, header=None):
    """Menu controlled with the arrow keys ↑↓ (+ typing = search). Returns an
    index, or None (cancelled via Esc, only when allow_cancel). Drawn in place
    below the existing output. '?' shows help for the current item. Without a TTY
    returns None (the caller uses the numbered fallback)."""
    if not _tui_supported() or not labels:
        return None
    n = len(labels)
    plain = [strip_ansi(l) for l in labels]
    header = list(header or [])
    helplist = None
    if isinstance(help, (list, tuple)):
        helplist = list(help)
    elif isinstance(help, str):
        helplist = [help] * n
    filt = ""
    status = ""
    sel_pos = default if 0 <= default < n else 0
    prev_lines = 0
    first = True

    def visible_order():
        if not filt:
            return list(range(n))
        f = filt.lower()
        return [i for i in range(n) if f in plain[i].lower()]

    def term_size():
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def trunc(s, width):
        return s[:max(1, width - 1)] + "..." if len(s) > width else s

    def render(order, sel_pos):
        nonlocal prev_lines, first
        cols, rows_total = term_size()
        maxw = max(10, cols - 2)
        reserve = 6 + len(header)
        page_rows = max(3, rows_total - reserve)
        buf = []
        if first:
            buf.append("\x1b[2J\x1b[H")   # clear the screen + cursor home (application mode, like Plex)
            first = False
        elif prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")
        vis = []
        for h in header:
            sp = strip_ansi(h)
            vis.append(h if len(sp) <= maxw else trunc(sp, maxw))
        vis.append(f"{Fore.YELLOW}{trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        if filt:
            hint = "↑↓ move · Enter = select · Esc = clear search"
        elif allow_cancel:
            hint = "↑↓ move · type = search · Enter = select · Esc = back"
        else:
            hint = "↑↓ move · type = search · Enter = select"
        if helplist:
            hint += " · ? = help"
        vis.append(f"{Fore.CYAN}{trunc(hint, maxw)}{Style.RESET_ALL}")
        if not order:
            vis.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
            start = max(0, min(sel_pos - page_rows // 2, len(order) - page_rows))
            window = order[start:start + page_rows]
            if start > 0:
                vis.append(f"  {Fore.CYAN}^ ({start} above){Style.RESET_ALL}")
            for pos, i in enumerate(window, start):
                text = trunc(plain[i], maxw - 2)
                if pos == sel_pos:
                    vis.append(f"{Fore.GREEN}{Style.BRIGHT}›{Style.RESET_ALL} "
                               f"{Fore.GREEN}{Style.BRIGHT}{text}{Style.RESET_ALL}")
                else:
                    vis.append(f"  {text}")
            rest = len(order) - (start + len(window))
            if rest > 0:
                vis.append(f"  {Fore.CYAN}v ({rest} below){Style.RESET_ALL}")
        pos_info = f" [{sel_pos + 1}/{len(order)}]" if order else ""
        if filt:
            vis.append(f"{Fore.MAGENTA}{trunc('Search: ' + filt + pos_info, maxw)}{Style.RESET_ALL}")
        else:
            vis.append(f"{Fore.CYAN}{trunc('(start typing to search)' + pos_info, maxw)}{Style.RESET_ALL}")
        if status:
            for line in status.split("\n"):
                sp = strip_ansi(line)
                vis.append(line if len(sp) <= maxw else trunc(sp, maxw))
        buf.append("\n".join(vis))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)
        return page_rows

    with _RawMode():
        order = visible_order()
        if sel_pos >= len(order):
            sel_pos = max(0, len(order) - 1)
        page_rows = render(order, sel_pos)
        _flush_input()
        while True:
            key = _read_key()
            if key != "other" and status:
                status = ""
            if key == "up" and order:
                sel_pos = (sel_pos - 1) % len(order)
            elif key == "down" and order:
                sel_pos = (sel_pos + 1) % len(order)
            elif key == "pgup" and order:
                sel_pos = max(0, sel_pos - page_rows)
            elif key == "pgdn" and order:
                sel_pos = min(len(order) - 1, sel_pos + page_rows)
            elif key == "home":
                sel_pos = 0
            elif key == "end" and order:
                sel_pos = len(order) - 1
            elif isinstance(key, tuple) and key[0] == "char" and key[1] == "?" and helplist and order:
                status = f"{Fore.CYAN}i: {helplist[order[sel_pos]]}{Style.RESET_ALL}"
            elif key in ("enter", "right"):
                if order:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return order[sel_pos]
            elif key in ("esc", "left"):
                if filt:
                    filt = ""
                    order = visible_order()
                    sel_pos = 0
                elif allow_cancel:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
            elif key == "backspace" and filt:
                filt = filt[:-1]
                order = visible_order()
                sel_pos = 0
            elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                filt += key[1]
                order = visible_order()
                sel_pos = 0
            page_rows = render(order, sel_pos)


def interactive_checklist(prompt, item_labels, action_labels, header=None, checked=None):
    """Checklist menu (multi-select): items are toggled with space, actions are
    confirmed with Enter. Returns (action_index, [checked indices]) or None (Esc).
    ↑↓ move, space = check/uncheck, Enter = confirm action, Esc = back."""
    if not _tui_supported():
        return None
    nI = len(item_labels)
    nA = len(action_labels)
    rows = nI + nA
    if rows == 0:
        return None
    checkset = set(checked or [])
    sel = nI if nI < rows else 0   # start on the first action (typically "Done")
    header = list(header or [])
    prev_lines = 0
    first = True

    def render():
        nonlocal prev_lines, first
        try:
            cols = os.get_terminal_size().columns
        except Exception:
            cols = 80
        maxw = max(20, cols - 2)

        def trunc(s):
            return s if len(strip_ansi(s)) <= maxw else s[:maxw - 1] + "..."
        buf = []
        if first:
            buf.append("\x1b[2J\x1b[H")
            first = False
        elif prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")
        vis = []
        for h in header:
            vis.append(trunc(h))
        vis.append(f"{Fore.YELLOW}{trunc(strip_ansi(prompt))}{Style.RESET_ALL}")
        vis.append(f"{Fore.CYAN}↑↓ move · space = check · Enter = confirm · Esc = back{Style.RESET_ALL}")
        for i, lab in enumerate(item_labels):
            box = f"{Fore.GREEN}[x]{Style.RESET_ALL}" if i in checkset else "[ ]"
            line = f"{box} {lab}"
            if sel == i:
                vis.append(f"{Fore.GREEN}{Style.BRIGHT}›{Style.RESET_ALL} {box} "
                           f"{Fore.GREEN}{Style.BRIGHT}{trunc(lab)}{Style.RESET_ALL}")
            else:
                vis.append(f"  {trunc(line)}")
        if nI and nA:
            vis.append(f"  {Fore.CYAN}{'-' * min(20, maxw)}{Style.RESET_ALL}")
        for a, lab in enumerate(action_labels):
            if sel == nI + a:
                vis.append(f"{Fore.GREEN}{Style.BRIGHT}›{Style.RESET_ALL} "
                           f"{Fore.GREEN}{Style.BRIGHT}{trunc(lab)}{Style.RESET_ALL}")
            else:
                vis.append(f"  {trunc(lab)}")
        vis.append(f"{Fore.MAGENTA}Checked: {len(checkset)}{Style.RESET_ALL}")
        buf.append("\n".join(vis))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)

    with _RawMode():
        render()
        _flush_input()
        while True:
            key = _read_key()
            if key == "up":
                sel = (sel - 1) % rows
            elif key == "down":
                sel = (sel + 1) % rows
            elif key == "home":
                sel = 0
            elif key == "end":
                sel = rows - 1
            elif isinstance(key, tuple) and key[0] == "char" and key[1] == " ":
                if sel < nI:
                    checkset.symmetric_difference_update({sel})
            elif key == "enter":
                if sel >= nI:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return (sel - nI, sorted(checkset))
                else:
                    checkset.symmetric_difference_update({sel})   # Enter on an item = toggle
            elif key == "esc":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None
            render()


def _ask_checklist_classic(prompt, item_labels, action_labels, checked=None):
    checkset = set(checked or [])
    while True:
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}")
        for i, lab in enumerate(item_labels, 1):
            box = "[x]" if (i - 1) in checkset else "[ ]"
            print(f"  {i}) {box} {lab}")
        for a, lab in enumerate(action_labels):
            print(f"  {chr(ord('a') + a)}) {lab}")
        raw = input("Number = toggle check, letter = action, Enter = first action, z = back: ").strip().lower()
        if raw in ("z", "q"):
            return None
        if raw == "":
            return (0, sorted(checkset))
        if raw.isdigit() and 1 <= int(raw) <= len(item_labels):
            checkset.symmetric_difference_update({int(raw) - 1})
            continue
        if len(raw) == 1 and "a" <= raw < chr(ord("a") + len(action_labels)):
            return (ord(raw) - ord("a"), sorted(checkset))
        print(f"{Fore.RED}Invalid choice.{Style.RESET_ALL}")


def ask_checklist(prompt, item_labels, action_labels, header=None, checked=None):
    """Multi-select with checkboxes + action rows. Returns (action_index,
    [checked indices]). Esc = step back. Supports presets and going back."""
    r = _preset_replay()
    if r is not _PRESET_MISS:
        if isinstance(r, dict):
            return (int(r.get("a", 0)), list(r.get("c", [])))
        return (0, [])
    got, v = _back_get()
    if got and isinstance(v, dict):
        _preset_record("checklist", prompt, v)
        return (int(v.get("a", 0)), list(v.get("c", [])))
    _pend = _back_pending_take()
    init = checked
    if _pend is not _BACK_NO_PENDING and isinstance(_pend, dict):
        init = _pend.get("c", checked)
    if _tui_supported():
        res = interactive_checklist(prompt, item_labels, action_labels, header=header, checked=init)
    else:
        for h in (header or []):
            if strip_ansi(h).strip():
                print(h)
        res = _ask_checklist_classic(prompt, item_labels, action_labels, checked=init)
    if res is None:
        raise _StepBack()
    ai, checked_list = res
    rec = {"a": ai, "c": checked_list}
    _back_put(rec)
    _preset_record("checklist", prompt, rec)
    return (ai, checked_list)


def _ask_pick_classic(prompt, labels, default=0, help=None, allow_back=False):
    hint = f"{Fore.CYAN} (? = more info){Style.RESET_ALL}" if help else ""

    def _show():
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}{hint}")
        for i, l in enumerate(labels):
            mark = f" {Fore.CYAN}(default){Style.RESET_ALL}" if i == default else ""
            print(f"  {i + 1}) {l}{mark}")

    _show()
    back_hint = ", z = back" if allow_back else ""
    while True:
        raw = input(f"Choice [1-{len(labels)}, Enter = {default + 1}{back_hint}]: ").strip()
        if raw == "?" and help:
            if isinstance(help, (list, tuple)):
                for h in help:
                    print(f"  {Fore.CYAN}-{Style.RESET_ALL} {h}")
            else:
                print(f"  {help}")
            _show()
            continue
        if allow_back and raw.lower() in ("z", "q", "back"):
            return None
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw) - 1
        print(f"{Fore.RED}Invalid choice, try again.{Style.RESET_ALL}")


def ask_pick(prompt, labels, default=0, help=None, allow_back=None, header=None, cursor=None):
    """Selection from a menu. On a terminal an arrow menu (↑↓, search by typing,
    ? = help, Esc = back), otherwise a numbered fallback (z = back). Returns an
    index. 'default' = the item moved to the first place. 'cursor' = which item
    (original index) the highlight should start on (e.g. when returning to the
    menu). allow_back: None = Esc raises WizardBack; True = Esc returns None;
    False = Esc does nothing. Preserves preset saving/replay."""
    r = _preset_replay()
    if r is not _PRESET_MISS:
        try:
            r = int(r)
        except Exception:
            r = default
        if not (0 <= r < len(labels)):
            r = default if 0 <= default < len(labels) else 0
        return r
    got, v = _back_get()
    if got:
        try:
            v = int(v)
        except Exception:
            v = default
        if not (0 <= v < len(labels)):
            v = default if 0 <= default < len(labels) else 0
        _preset_record("pick", prompt, v)
        return v
    can_esc = allow_back is not False
    # returning via Esc in the wizard: pre-fill the earlier choice (cursor on it)
    _pend = _back_pending_take()
    eff_default = default
    highlight = cursor
    if _pend is not _BACK_NO_PENDING:
        try:
            _pi = int(_pend)
            if 0 <= _pi < len(labels):
                eff_default = _pi
                highlight = _pi
        except Exception:
            pass
    if highlight is None or not (0 <= highlight < len(labels)):
        highlight = eff_default
    # display: the DEFAULT item is always first, the others keep their order.
    # But the returned/saved index is the ORIGINAL one (so presets stay stable).
    if 0 <= eff_default < len(labels) and eff_default != 0:
        order = [eff_default] + [i for i in range(len(labels)) if i != eff_default]
    else:
        order = list(range(len(labels)))
    try:
        init_pos = order.index(highlight)
    except ValueError:
        init_pos = 0
    disp_labels = [labels[i] for i in order]
    if isinstance(help, (list, tuple)):
        disp_help = [help[i] for i in order]
    else:
        disp_help = help
    if _tui_supported():
        sel = interactive_menu(prompt, disp_labels, default=init_pos, help=disp_help,
                               allow_cancel=can_esc, header=header)
    else:
        for h in (header or []):
            if strip_ansi(h).strip():
                print(h)
        sel = _ask_pick_classic(prompt, disp_labels, init_pos, disp_help, allow_back=can_esc)
    if sel is None:
        if allow_back is True:
            return None
        if allow_back is False:
            idx = default if 0 <= default < len(labels) else 0
        else:
            raise _StepBack()
    else:
        idx = order[sel]
    _back_put(idx)
    _preset_record("pick", prompt, idx)
    return idx


def ask_text(prompt, default=""):
    r = _preset_replay()
    if r is not _PRESET_MISS:
        return r if r is not None else default
    got, v = _back_get()
    if got:
        _preset_record("text", prompt, v)
        return v
    suffix = f" [{default}]" if default else ""
    _pend = _back_pending_take()
    _prefill = _pend if (_pend is not _BACK_NO_PENDING and isinstance(_pend, str)) else ""
    if _tui_supported():
        val = _read_line_tui(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}{suffix}: ", default, prefill=_prefill)
    else:
        _dflt = _prefill or default
        _sfx = f" [{_dflt}]" if _dflt else ""
        val = input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}{_sfx}: ").strip() or _dflt
    _back_put(val)
    _preset_record("text", prompt, val)
    return val


LANGUAGE_NAMES = {
    "cs": "Czech", "sk": "Slovak", "en": "English", "de": "German",
    "pl": "Polish", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "ru": "Russian", "uk": "Ukrainian",
    "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "el": "Greek",
    "tr": "Turkish", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "is": "Icelandic", "hr": "Croatian", "sr": "Serbian",
    "sl": "Slovenian", "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian",
    "ga": "Irish", "mt": "Maltese", "ar": "Arabic", "he": "Hebrew",
    "fa": "Persian", "hi": "Hindi", "bn": "Bengali", "ur": "Urdu",
    "ta": "Tamil", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian",
    "ms": "Malay", "tl": "Tagalog", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "zh-cn": "Chinese (simpl.)", "zh-tw": "Chinese (trad.)",
    "ca": "Catalan", "eu": "Basque", "gl": "Galician",
    "af": "Afrikaans", "sw": "Swahili", "la": "Latin",
}


def _print_language_list():
    items = sorted(LANGUAGE_NAMES.items())
    cells = [f"{c:<6}{n}" for c, n in items]
    width = max(len(x) for x in cells) + 2
    cols = 3
    print(f"{Fore.MAGENTA}Common language codes:{Style.RESET_ALL}")
    for i in range(0, len(cells), cols):
        print("  " + "".join(cell.ljust(width) for cell in cells[i:i + cols]))
    try:
        from deep_translator import GoogleTranslator
        sup = GoogleTranslator().get_supported_languages(as_dict=True)  # name -> code
        extra = sorted(set(sup.values()) - set(LANGUAGE_NAMES.keys()))
        if extra:
            print(f"{Fore.CYAN}Other codes supported by Google:{Style.RESET_ALL}")
            for i in range(0, len(extra), 12):
                print("  " + " ".join(extra[i:i + 12]))
    except Exception:
        pass
    print(f"{Fore.CYAN}Note:{Style.RESET_ALL} exact support depends on the service "
          "(Google ~130, DeepL ~30, OpenSubtitles dle dostupnosti).")


def ask_language(prompt, default=""):
    """Like ask_text, but '?' prints the list of available language codes."""
    hint = f"{prompt} (? = language list)"
    while True:
        raw = ask_text(hint, default)
        if raw.strip() == "?":
            _print_language_list()
            continue
        return raw


def ask_anthropic_model(prompt, default="", args=None):
    """Like ask_text, but '?' prints the list of Claude models (online per the key,
    with token limits and a price/usage note; otherwise the built-in overview)."""
    hint = f"{prompt} (? = model list)"
    while True:
        raw = ask_text(hint, default)
        if raw.strip() == "?":
            _print_anthropic_models(args)
            continue
        return raw


def ask_gemini_model(prompt, default="", args=None):
    """Like ask_text, but '?' prints the list of Gemini models (online per the key)."""
    hint = f"{prompt} (? = model list)"
    while True:
        raw = ask_text(hint, default)
        if raw.strip() == "?":
            _print_gemini_models(args)
            continue
        return raw


def _pick_reading_speed():
    keys = list(READING_SPEED_PRESETS.keys())
    labels = [f"{k} - {READING_SPEED_PRESETS[k][2]} "
              f"({READING_SPEED_PRESETS[k][0]:.0f} zn/s, podlaha {READING_SPEED_PRESETS[k][1]:.1f}s)"
              for k in keys]
    return keys[ask_pick("Reading speed (target pace):", labels, default=0)]


def _ask_readability(into_args, srt_files):
    """Asks about extending short subtitles; optionally also the detailed
    parameters (min display time, gap, per-line bonus)."""
    if not ask_yes_no("Extend too-short subtitles for readability "
                      "(only into free space, never past an overlap)?", default_no=True):
        return
    into_args.fix_short_duration = True
    if ask_yes_no("Configure readability in DETAIL (reading speed, min display time, "
                  "safety gap, per-line bonus)?", default_no=True):
        res = ask_readability_params(srt_files or [])
        if res:
            cps, floor, gap, overhead = res
            into_args.min_cps = cps
            into_args.min_duration_floor = floor
            into_args.min_gap = gap
            into_args.line_overhead = overhead
            return
    into_args.reading_speed = _pick_reading_speed()


def _pick_method(into_args):
    mi = ask_pick(
        "Timing computation method:",
        ["auto  - recommended (combination: affine pre-align + warp; otherwise affine)",
         "combo - affine pre-align + warp fine-tune by sentence (most robust)",
         "warp  - by SENTENCE only (fast; needs a text reference)",
         "affine - global shift + speed only (language independent, also from audio)"],
        default=0,
        help=["auto: picks the best approach itself - if there is a text reference and enough "
              "anchors, it does combo; otherwise it falls back to affine.",
              "combo: first aligns global shift+speed (affine), then fine-tunes by sentence "
              "(warp). The most robust and accurate.",
              "warp: sentence matching + piecewise linear map only. Fixes piecewise desync, "
              "but needs a text reference.",
              "affine: only one global shift and speed (a*t+b). Works even from audio alone and "
              "across languages, but does not fix piecewise desync."])
    into_args.method = ["auto", "combo", "warp", "affine"][mi]


def _enable_translate_prompt(into_args):
    ei = ask_pick("Translator for cross-language matching:",
                  ["gemini - free AI (Google AI Studio key)",
                   "google - online, free without a key",
                   "deepl  - better quality (API key)",
                   "claude - via the Anthropic API (paid)",
                   "argos  - offline (pip install argostranslate langdetect)"], default=1)
    into_args.translate = ["gemini", "google", "deepl", "claude", "argos"][ei]
    into_args.pivot_lang = ask_text("Common language for matching (pivot)", "en") or "en"
    if into_args.translate == "gemini" and not getattr(into_args, "gemini_key", None):
        into_args.gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                                or ask_text("Gemini API key (free at aistudio.google.com)", "") or None)
    elif into_args.translate == "deepl" and not getattr(into_args, "deepl_key", None):
        into_args.deepl_key = os.environ.get("DEEPL_API_KEY") or ask_text("DeepL API key", "") or None
    elif into_args.translate == "claude" and not getattr(into_args, "anthropic_key", None):
        into_args.anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or ask_text("Anthropic API key", "") or None


def _setup_languages_translate(into_args, target_lang, ref_lang):
    """Based on the DETECTED languages (from content), decides about translation
    for matching. For 'affine', translation makes no sense."""
    if into_args.method == "affine":
        return
    log_info(f"Detected language - to-fix: {target_lang or '?'}, reference: {ref_lang or '?'}")
    if target_lang and ref_lang:
        if target_lang == ref_lang:
            log_info("Same language - translation for matching is not needed.")
            return
        log_warn(f"DIFFERENT languages ({target_lang} vs {ref_lang}). For accurate sentence "
                 "matching, translation helps FOR MATCHING ONLY (the text is not changed).")
        if ask_yes_no("Enable translation for cross-language matching?", default_no=False):
            _enable_translate_prompt(into_args)
        else:
            log_info("Without translation - different languages are computed by the affine phase.")
    else:
        if ask_yes_no("The language could not be determined unambiguously. Are the to-fix and "
                      "reference subtitles in DIFFERENT languages (enable translation for matching)?", default_no=True):
            _enable_translate_prompt(into_args)


def _resolve_mkvmerge(into_args):
    if getattr(into_args, "mkvmerge", None):
        return into_args.mkvmerge
    return find_tool(["mkvmerge", "mkvmerge.exe"])


def _is_text_sub(codec):
    c = (codec or "").lower()
    return any(x in c for x in ("subrip", "srt", "ass", "ssa", "substation", "vtt", "webvtt", "text"))


def _offer_video_reference(into_args, video_path):
    """Probes the real tracks in the video and offers them. Returns (audio_mode,
    ref_lang) and sets track_id / audio_track_id per the choice."""
    mkvmerge_bin = _resolve_mkvmerge(into_args)
    subs = audio = None
    if mkvmerge_bin:
        subs, audio, _err = try_list_tracks(mkvmerge_bin, video_path)
    if not subs and not audio:
        log_warn("Could not read tracks from the video (mkvmerge missing or an unreadable "
                 "container) - using automatic selection.")
        am = ask_pick("Reference from video - what to use?",
                      ["subtitle track (auto)", "audio - speech detection/VAD", "both"], default=0)
        mode = ["off", "replace", "combine"][am]
        rl = None
        if mode in ("off", "combine"):
            rl = norm_lang(ask_language("Reference track language (eng/cze; empty=auto)", "") or None)
        return mode, rl

    labels, opts = [], []
    for t in (subs or []):
        kind = "text" if _is_text_sub(t["codec"]) else "image-based (not usable as text)"
        title = f" '{t['title']}'" if t.get("title") else ""
        labels.append(f"subtitles #{t['id']}  {t['lang']}  {t['codec']}{title}  [{kind}]")
        opts.append(("off", t["id"], norm_lang(t["lang"])))
    for t in (audio or []):
        title = f" '{t['title']}'" if t.get("title") else ""
        labels.append(f"audio  #{t['id']}  {t['lang']}  {t['codec']}{title}  [speech detection/VAD]")
        opts.append(("replace", t["id"], None))
    labels.append("both: subtitle track + audio together (max robustness)")
    opts.append(("combine", None, None))

    idx = ask_pick("What to use as reference? (actual tracks found in the video)", labels, default=0)
    mode, track_id, lang = opts[idx]
    if mode == "off" and track_id is not None:
        into_args.track_id = track_id
    elif mode == "replace" and track_id is not None:
        into_args.audio_track_id = track_id
    return mode, lang


def sync_two_subs(target_path, ref_path, output, args):
    """Direct synchronization of two subtitle files (the reference is the second
    .srt), without video and without external tools. Text is unchanged, only timing."""
    target_events = parse_srt(Path(target_path))
    ref_events = parse_srt(Path(ref_path))
    log_info(f"Subtitles to fix: {len(target_events)}; reference: {len(ref_events)}")
    corrected = run_alignment(args, ref_events, ref_events, target_events)
    if getattr(args, "fix_short_duration", False):
        cps, floor, gap, overhead = resolve_speed_params(args)
        corrected, n_ext = fix_short_durations(
            corrected, min_cps=cps, min_duration_floor=floor, min_gap=gap, line_overhead=overhead)
        log_info(f"Extended {n_ext} subtitles with shortened display (using free space)")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    write_srt(corrected, Path(output))
    log_done(f"Synchronized subtitles saved to: {output}")


def run_auto_single(args):
    """Interactive wizard for ONE file: searches the directory, offers the
    subtitles to fix and a reference source (a video with real tracks, or a
    second subtitle file), actually detects languages from content and offers
    translation accordingly, asks about the method, output and readability."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Interactive synchronization (single file) ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")

    subs = collect_subs(directory)
    videos = collect_videos(directory, recursive=False)
    if not subs:
        die("There are no subtitle files (.srt/.ass/.vtt/.orig) in the directory. "
            "Run the script in a folder with subtitles or pass a path to it as the 1st argument.")

    ti = ask_pick("Which subtitle file should be FIXED (has bad timing)?",
                  [os.path.basename(s) for s in subs])
    target = subs[ti]
    target_lang = detect_srt_file_language(target)

    ref_opts, labels = [], []
    for v in videos:
        ref_opts.append(("video", v))
        labels.append(f"[video]   {os.path.basename(v)}  (extract a reference track from the video)")
    for s in subs:
        if s == target:
            continue
        ref_opts.append(("sub", s))
        labels.append(f"[subs]    {os.path.basename(s)}  (reference directly from this file)")
    if not ref_opts:
        die("Found no reference source (neither a video nor a second subtitle file). "
            "You need either a video with a correctly timed track, or a second .srt as reference.")

    ri = ask_pick("Where to take the CORRECT timing (reference) from?", labels, default=0)
    kind, refpath = ref_opts[ri]

    _pick_method(args)

    audio_mode, ref_lang = "off", None
    if kind == "video":
        audio_mode, ref_lang = _offer_video_reference(args, refpath)
    else:
        ref_lang = detect_srt_file_language(refpath)

    _setup_languages_translate(args, target_lang, ref_lang)

    tgt = Path(target)
    default_out = str(tgt.with_name(tgt.stem + ".synced" + tgt.suffix))
    if ask_yes_no(f"Overwrite the original file '{tgt.name}' (a .bak backup is created once)?", default_no=True):
        output = target
        bak = tgt.with_suffix(tgt.suffix + ".bak")
        if not bak.exists():
            shutil.copy(target, bak)
    else:
        output = ask_text("Output file", default_out)

    _ask_readability(args, [target])

    args.audio_mode = audio_mode
    args.ref_lang = ref_lang
    args.output = Path(output)

    print()
    log_info(f"Fix:       {os.path.basename(target)} (language: {target_lang or '?'})")
    log_info(f"Reference: {os.path.basename(refpath)} ({'video' if kind == 'video' else 'subtitles'}"
             + (f", language: {ref_lang}" if ref_lang else "") + ")")
    log_info(f"Method:    {args.method}"
             + (f" | audio-mode {audio_mode}" if kind == 'video' else "")
             + (f" | translation {args.translate}->{args.pivot_lang}" if getattr(args, 'translate', 'off') != 'off' else ""))
    log_info(f"Output:    {output}")
    if not ask_yes_no("Run synchronization?", default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()

    if kind == "sub":
        sync_two_subs(target, refpath, args.output, args)
    else:
        args.mkv = Path(refpath)
        args.subtitle_to_fix = Path(target)
        process_single(args)


def run_auto_all(args):
    """Interactive wizard for a BATCH (--all): offers the real tracks from a
    sample video, actually detects languages from subtitle content and offers
    translation accordingly; then runs the standard batch processing."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Interactive batch synchronization (--all) ==={Style.RESET_ALL}")
    log_info(f"Working directory: {os.path.abspath(directory)}")

    args.recursive = ask_yes_no("Search subdirectories too?", default_no=True)
    videos = collect_videos(directory, args.recursive)
    srts = collect_srts(directory, args.recursive)
    log_info(f"Found {len(videos)} videos and {len(srts)} .srt files.")
    if not videos:
        die("No videos for batch processing. To synchronize a pair of subtitle "
            "files (.srt + reference) use --auto.")

    _pick_method(args)

    # reference offer from the REAL tracks of the sample video
    sample_video = videos[0]
    log_info(f"Reading tracks from the sample video: {os.path.basename(sample_video)}")
    mkvmerge_bin = _resolve_mkvmerge(args)
    subs_tracks = None
    if mkvmerge_bin:
        subs_tracks, _audio, _err = try_list_tracks(mkvmerge_bin, sample_video)

    am = ask_pick("Reference source for the whole batch:",
                  ["subtitle track from the video (recommended)",
                   "audio - speech detection/VAD",
                   "both together (max robustness)"], default=0)
    args.audio_mode = ["off", "replace", "combine"][am]
    if args.audio_mode in ("off", "combine"):
        langs = sorted({norm_lang(t["lang"]) for t in (subs_tracks or []) if norm_lang(t["lang"])})
        if langs:
            labels = [f"{l}" for l in langs] + ["other (type manually)", "automatically (don't pick)"]
            i = ask_pick(f"Reference subtitle track language (found in the sample): ", labels, default=0)
            if i < len(langs):
                args.ref_lang = langs[i]
            elif i == len(langs):
                args.ref_lang = norm_lang(ask_language("Language (eng/cze/...)", "") or None)
            else:
                args.ref_lang = None
        else:
            args.ref_lang = norm_lang(ask_language(
                "Reference subtitle track language (eng/cze; empty=auto)", args.ref_lang or "") or None)

    args.target_lang = ask_text(
        "When multiple .srt match one video, the preferred language tag in the name (e.g. cs; empty = ask)",
        args.target_lang or "") or None

    # real language detection of the to-fix subtitles from the CONTENT of the sample .srt
    sample_srts = filter_by_tag(srts, args.target_lang) if (args.target_lang and srts) else srts
    target_lang = detect_srt_file_language(sample_srts[0]) if sample_srts else None
    _setup_languages_translate(args, target_lang, args.ref_lang)

    args.overwrite = ask_yes_no("Overwrite the original .srt directly (.bak backups are created)?", default_no=True)

    _ask_readability(args, (sample_srts or srts)[:5])

    args.yes = ask_yes_no("When a video lacks the needed tracks, automatically skip without asking?", default_no=True)

    print()
    log_info(f"Metoda: {args.method} | audio-mode: {args.audio_mode}"
             + (f" | translation {args.translate}->{args.pivot_lang}" if getattr(args, 'translate', 'off') != 'off' else "")
             + f" | overwrite: {'yes' if args.overwrite else 'no'} | recursive: {'yes' if args.recursive else 'no'}")
    if not ask_yes_no("Run batch processing?", default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()

    args.mkv = Path(directory)
    run_batch(args)


# ============================================================================
# Audio & file tools (extract / convert / mux audio, intelligent file renamer)
# ============================================================================

def _audio_ext_for(codec):
    """Maps an mkvmerge friendly audio codec name to a file extension."""
    c = (codec or "").lower()
    if "e-ac-3" in c or "eac3" in c or "e-ac3" in c:
        return ".eac3"
    if "ac-3" in c or "ac3" in c:
        return ".ac3"
    if "truehd" in c or "true hd" in c or "mlp" in c:
        return ".thd"
    if "dts" in c:
        return ".dts"
    if "aac" in c:
        return ".aac"
    if "flac" in c:
        return ".flac"
    if "mp3" in c or "mpeg audio" in c or "mp2" in c:
        return ".mp3"
    if "opus" in c:
        return ".opus"
    if "vorbis" in c:
        return ".ogg"
    if "pcm" in c:
        return ".wav"
    return ".mka"


def _ensure_ffmpeg_bin(args, directory):
    """Resolves ffmpeg (PATH / override / auto-download). Returns a path or None."""
    ff = getattr(args, "ffmpeg", None) or find_tool(["ffmpeg", "ffmpeg.exe"])
    if not ff:
        try:
            ff = ensure_ffmpeg(directory, allow_download=not getattr(args, "no_ffmpeg_download", False))
        except Exception:
            ff = None
    if ff:
        args.ffmpeg = getattr(args, "ffmpeg", None) or ff
    return ff


def _find_ffprobe(ffmpeg_bin):
    """Locates ffprobe next to ffmpeg, or in PATH."""
    if ffmpeg_bin:
        d = os.path.dirname(ffmpeg_bin)
        base = os.path.basename(ffmpeg_bin).lower()
        name = "ffprobe.exe" if base.endswith(".exe") else "ffprobe"
        c = os.path.join(d, name)
        if os.path.isfile(c):
            return c
    return find_tool(["ffprobe", "ffprobe.exe"])


def _ffprobe_streams(ffprobe_bin, path):
    """Returns the list of streams (ffprobe -show_streams) or []."""
    try:
        out = subprocess.run([ffprobe_bin, "-v", "error", "-print_format", "json",
                              "-show_streams", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return json.loads(out.stdout or "{}").get("streams", [])
    except Exception:
        return []


def _audio_track_label(t):
    """Rich one-line label for an audio track (to tell duplicates apart)."""
    flags = []
    if t.get("default"):
        flags.append("default")
    if t.get("forced"):
        flags.append("forced")
    fl = ("  [" + ", ".join(flags) + "]") if flags else ""
    nm = t.get("name") or "-"
    ch = t.get("channels")
    chs = f"  {ch}ch" if ch else ""
    return f"#{t['id']}  {_lang3_name(t.get('lang')):8} {t.get('codec', '?'):8}{chs}  name={nm}{fl}"


def run_extract_audio(args):
    """Wizard: extracts audio tracks from videos into standalone audio files via
    ffmpeg stream copy (no re-encoding). Tracks are detected directly from the video;
    you interactively pick which (checkbox multi-select from the sample, by language,
    or all) - just like subtitle extraction. Duplicate languages are told apart."""
    print(f"{Fore.MAGENTA}=== Extract audio tracks from videos ==={Style.RESET_ALL}")
    directory = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or "."
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)
    videos = collect_videos(directory, recursive)
    if not videos:
        die("No videos in the directory.")
    log_info(f"Found {len(videos)} videos.")
    mkvmerge_bin, _me, _ff, _ = _resolve_tools_for_extract(args, Path(videos[0]))
    if not mkvmerge_bin:
        die("mkvmerge not found (required to read tracks). Install MKVToolNix (see --help).")
    ffmpeg_bin = _ensure_ffmpeg_bin(args, directory)
    if not ffmpeg_bin:
        die("ffmpeg not found (required for audio extraction). Install it or allow auto-download.")
    args.mkvmerge = getattr(args, "mkvmerge", None) or mkvmerge_bin

    # sample = first video that has audio tracks
    sample = None
    sample_audio = []
    for cand in videos:
        full = _mkv_probe_full(mkvmerge_bin, Path(cand))
        if full.get("audio"):
            sample = Path(cand)
            sample_audio = full["audio"]
            break
    if sample is None:
        die("Could not read audio tracks from any video.")
    log_info(f"Found {len(sample_audio)} audio tracks in the sample {sample.name}.")

    hdr = [f"{Fore.MAGENTA}=== Extract audio tracks ==={Style.RESET_ALL}",
           f"{Fore.CYAN}Found {len(videos)} videos. Sample: {sample.name}{Style.RESET_ALL}", ""]
    item_labels = [_audio_track_label(t) for t in sample_audio]
    actions = ["Extract CHECKED tracks (check them above with space)",
               "by LANGUAGE (I'll type codes - robust for the whole folder)",
               "ALL audio tracks from each video"]
    act, checked = ask_checklist("Which audio tracks to extract?", item_labels, actions, header=hdr)

    sample_sel = _track_selectors(sample_audio)   # [(key, label, track)]
    want_keys = None
    want_langs = None
    if act == 0:
        if checked:
            want_keys = [sample_sel[i][0] for i in checked if 0 <= i < len(sample_sel)]
        if not want_keys:
            log_info("Nothing checked - taking ALL audio tracks.")
    elif act == 1:
        raw = ask_text("Language codes separated by commas (e.g. eng,jpn,ger; empty = all)", "")
        codes = [x.strip().lower() for x in raw.replace(" ", "").split(",") if x.strip()]
        want_langs = {_canon3(c) for c in codes} or None
    # act == 2 -> all

    overwrite = ask_yes_no("Overwrite existing output audio files?", default_no=True)
    seltxt = ("checked tracks" if want_keys else
              ("languages: " + ",".join(sorted(want_langs)) if want_langs else "all audio tracks"))
    log_info(f"Selection: {seltxt} | codec copy (no re-encode) | videos: {len(videos)}")
    if not ask_yes_no(f"Run for {len(videos)} videos?", default_no=False):
        log_warn("Cancelled by the user.")
        return
    preset_flush_if_save()

    done = skipped = wrote = 0
    for v in videos:
        vp = Path(v)
        full = _mkv_probe_full(mkvmerge_bin, vp)
        auds = full.get("audio", [])
        if not auds:
            log_warn(f"{vp.name}: no audio tracks - skipping.")
            skipped += 1
            continue
        if want_keys is not None:
            sels = _track_selectors(auds)
            keymap = {k: t for k, _l, t in sels}
            sel = [keymap[k] for k in want_keys if k in keymap]
        elif want_langs is not None:
            sel = [t for t in auds if _canon3(t.get("lang")) in want_langs]
        else:
            sel = list(auds)
        if not sel:
            can_ask = not preset_is_replaying() and not getattr(args, "yes", False)
            fixed = _extract_fix_one_video(vp, auds, can_ask, _audio_track_label, "audio")
            if not fixed:
                log_warn(f"{vp.name}: no matching audio track - skipping.")
                skipped += 1
                continue
            sel = fixed
        used = set()
        any_ok = False
        for t in sel:
            try:
                ordinal = auds.index(t)
            except ValueError:
                ordinal = 0
            ext = _audio_ext_for(t.get("codec"))
            tag = (t.get("lang") or "und").upper()
            out = vp.with_name(vp.stem + f"_{tag}{ext}")
            n = 2
            while str(out) in used:
                out = vp.with_name(vp.stem + f"_{tag}_{n}{ext}")
                n += 1
            used.add(str(out))
            if out.exists() and not overwrite:
                log_info(f"{out.name}: already exists - skipping.")
                continue
            cmd = [ffmpeg_bin, "-y", "-i", str(vp), "-map", f"0:a:{ordinal}", "-c:a", "copy", str(out)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0 or not out.exists():
                log_warn(f"{vp.name}: extracting audio #{t['id']} failed.")
                continue
            log_done(f"{vp.name}: audio #{t['id']} ({t.get('codec', '?')}) -> {out.name}")
            wrote += 1
            any_ok = True
        if any_ok:
            done += 1
        else:
            skipped += 1
    print()
    log_done(f"Done: {wrote} audio files from {done} videos ({skipped} skipped).")


def run_convert_audio(args):
    """Re-encodes all audio tracks in each MKV to a chosen codec (default AC-3),
    copying video and subtitles. Output: <base>_<codec>.mkv (or overwrite)."""
    print(f"{Fore.MAGENTA}=== Convert audio (re-encode, e.g. to AC-3) ==={Style.RESET_ALL}")
    directory = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or "."
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)
    all_videos = collect_videos(directory, recursive)
    videos = [v for v in all_videos if Path(v).suffix.lower() == ".mkv"]
    if not videos:
        die("No .mkv files in the directory (this mode outputs MKV and copies subtitles).")
    if len(videos) < len(all_videos):
        log_info(f"Note: {len(all_videos) - len(videos)} non-MKV files skipped (MKV only).")
    log_info(f"Found {len(videos)} MKV files.")
    ffmpeg_bin = _ensure_ffmpeg_bin(args, directory)
    if not ffmpeg_bin:
        die("ffmpeg not found (required for audio conversion). Install it or allow auto-download.")

    codecs = [("ac3", "AC-3 (Dolby Digital) - wide device compatibility"),
              ("eac3", "E-AC-3 (Dolby Digital Plus)"),
              ("aac", "AAC")]
    ci = ask_pick("Target audio codec:", [f"{c} - {d}" for c, d in codecs], default=0,
                  help=["AC-3: best compatibility with TVs/receivers (up to 640k).",
                        "E-AC-3: newer, more efficient than AC-3.",
                        "AAC: efficient, common for streaming/mobile."])
    codec = codecs[ci][0]
    bitrate = (ask_text("Audio bitrate (e.g. 640k, 448k, 256k)", "640k") or "640k").strip()
    overwrite = ask_yes_no("Overwrite the original MKV? (otherwise saved as <name>_<codec>.mkv)", default_no=True)
    log_info(f"Codec: {codec} @ {bitrate} | video + subtitles copied")
    if not ask_yes_no(f"Run for {len(videos)} MKV files?", default_no=False):
        log_warn("Cancelled.")
        return
    preset_flush_if_save()

    done = errors = 0
    for v in videos:
        vp = Path(v)
        out = vp if overwrite else vp.with_name(vp.stem + f"_{codec}.mkv")
        tmp = vp.with_name(vp.stem + f".__conv_{codec}__.mkv") if overwrite else out
        cmd = [ffmpeg_bin, "-y", "-i", str(vp), "-map", "0", "-c:v", "copy",
               "-c:a", codec, "-b:a", bitrate, "-c:s", "copy", str(tmp)]
        r = subprocess.run(cmd)
        if r.returncode != 0 or not os.path.exists(tmp):
            log_warn(f"{vp.name}: conversion failed.")
            errors += 1
            if overwrite and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            continue
        if overwrite:
            try:
                os.replace(str(tmp), str(vp))
            except OSError as e:
                log_warn(f"{vp.name}: could not replace the original ({e}).")
                errors += 1
                continue
        size_mb = os.path.getsize(out) / (1024 * 1024)
        log_done(f"{vp.name} -> {Path(out).name} ({codec} {bitrate}, {size_mb:.0f} MB)")
        done += 1
    print()
    log_done(f"Done: {done} converted, {errors} errors (of {len(videos)}).")


_IMPORT_AUDIO_EXTS = {".aac", ".ac3", ".eac3", ".dts", ".thd", ".mp3", ".flac",
                      ".mka", ".opus", ".ogg", ".wav"}
_MKV_SUB_COPY_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt",
                        "hdmv_pgs_subtitle", "dvd_subtitle"}
_MKV_SUB_CONVERT = {"mov_text", "tx3g"}


def _build_import_audio_cmd(ffmpeg_bin, video_path, audio_path, out_path, streams,
                            audio_lang3="eng", audio_title="English"):
    """Builds the ffmpeg command that muxes the external audio (as the first, default
    audio track) together with the original video/audio/subtitles into an MKV."""
    cmd = [ffmpeg_bin, "-y", "-i", str(video_path), "-i", str(audio_path)]
    map_args = ["-map", "1:a"]
    video_streams = [s for s in streams if s.get("codec_type") == "video"
                     and s.get("disposition", {}).get("attached_pic", 0) == 0]
    for s in video_streams:
        map_args += ["-map", f"0:{s['index']}"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    for s in audio_streams:
        map_args += ["-map", f"0:{s['index']}"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    sub_codec_args = []
    sub_index = 0
    skipped_subs = 0
    for s in subtitle_streams:
        codec = s.get("codec_name", "").lower()
        if codec in _MKV_SUB_COPY_CODECS:
            map_args += ["-map", f"0:{s['index']}"]
            sub_codec_args += [f"-c:s:{sub_index}", "copy"]
            sub_index += 1
        elif codec in _MKV_SUB_CONVERT:
            map_args += ["-map", f"0:{s['index']}"]
            sub_codec_args += [f"-c:s:{sub_index}", "srt"]
            sub_index += 1
        else:
            skipped_subs += 1
    if skipped_subs:
        log_info(f"  Skipped {skipped_subs} incompatible subtitle stream(s)")
    codec_args = ["-c:v", "copy", "-c:a", "copy"] + sub_codec_args
    disp_args = ["-disposition:a", "none", "-disposition:a:0", "default",
                 "-metadata:s:a:0", f"language={audio_lang3}",
                 "-metadata:s:a:0", f"title={audio_title}"]
    return cmd + map_args + codec_args + disp_args + [str(out_path)]


def _fix_merged_metadata(mkvmerge_bin, mkvpropedit_bin, mkv_path, default_audio_lang3, default_sub_lang3):
    """After muxing: fills missing track names by language and sets the chosen
    default audio + subtitle language as default (via mkvpropedit)."""
    if not mkvpropedit_bin:
        log_warn("  mkvpropedit not found - track names / default flags not set.")
        return
    full = _mkv_probe_full(mkvmerge_bin, mkv_path)
    edit_args = []
    # fill missing names
    for kind in ("audio", "subs"):
        for t in full.get(kind, []):
            if (t.get("name") or "").strip():
                continue
            name = _lang3_name(t.get("lang")) if t.get("lang") not in (None, "und") else None
            if name:
                edit_args += ["--edit", f"track:{t['sel']}", "--set", f"name={name}"]
    # default audio = chosen language (fallback: first audio)
    aud = full.get("audio", [])
    def_a = None
    if default_audio_lang3:
        def_a = next((t for t in aud if _canon3(t.get("lang")) == default_audio_lang3), None)
    if def_a is None:
        def_a = aud[0] if aud else None
    for t in aud:
        flag = "1" if def_a is not None and t["sel"] == def_a["sel"] else "0"
        edit_args += ["--edit", f"track:{t['sel']}", "--set", f"flag-default={flag}"]
    # default subtitle = chosen language (if present)
    subs = full.get("subs", [])
    def_s = next((t for t in subs if _canon3(t.get("lang")) == default_sub_lang3), None) if default_sub_lang3 else None
    for t in subs:
        flag = "1" if def_s is not None and t["sel"] == def_s["sel"] else "0"
        edit_args += ["--edit", f"track:{t['sel']}", "--set", f"flag-default={flag}"]
    if not edit_args:
        return
    r = subprocess.run([mkvpropedit_bin, str(mkv_path)] + edit_args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log_warn(f"  mkvpropedit: {r.stderr.strip()[:200]}")
    else:
        a_info = _lang3_name(def_a["lang"]) if def_a else "none"
        s_info = _lang3_name(def_s["lang"]) if def_s else "none"
        log_info(f"  Default audio: {a_info} | default subtitles: {s_info}")


def run_import_audio(args):
    """Muxes an external audio file (paired with each video by SxxExx) into the
    video as the default audio track, optionally sets a chosen subtitle language as
    default, and fills missing track names by language. Output: <base>_merged.mkv."""
    print(f"{Fore.MAGENTA}=== Insert (mux) external audio into videos ==={Style.RESET_ALL}")
    directory = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or "."
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)

    mkvmerge_bin, _me, mkvpropedit_bin = _ensure_mkv_tools(args, directory, need_propedit=True)
    ffmpeg_bin = _ensure_ffmpeg_bin(args, directory)
    if not ffmpeg_bin:
        die("ffmpeg not found (required for muxing). Install it or allow auto-download.")
    ffprobe_bin = _find_ffprobe(ffmpeg_bin)
    if not ffprobe_bin:
        die("ffprobe not found (usually next to ffmpeg). Install ffmpeg with ffprobe.")

    # build video + external-audio maps by SxxExx
    def _walk(d):
        if recursive:
            for root, _dirs, files in os.walk(d):
                for f in files:
                    yield os.path.join(root, f)
        else:
            for f in os.listdir(d):
                full = os.path.join(d, f)
                if os.path.isfile(full):
                    yield full

    video_map, audio_map = {}, {}
    for full in _walk(directory):
        ext = os.path.splitext(full)[1].lower()
        key = _episode_key(os.path.basename(full))
        if key is None:
            continue
        if ext in (".mkv", ".mp4", ".m4v", ".mov", ".webm") and "_merged" not in os.path.basename(full).lower():
            video_map.setdefault(key, full)
        elif ext in _IMPORT_AUDIO_EXTS:
            audio_map.setdefault(key, full)
    if not video_map:
        die("No video files with an SxxExx tag found.")
    log_info(f"Found {len(video_map)} videos and {len(audio_map)} external audio files (paired by SxxExx).")

    # show the audio tracks already present in a sample video (rich info)
    sample_v = video_map[sorted(video_map)[0]]
    sample_full = _mkv_probe_full(mkvmerge_bin, Path(sample_v))
    if sample_full.get("audio"):
        log_info(f"Existing audio tracks in the sample '{os.path.basename(sample_v)}':")
        for t in sample_full["audio"]:
            print("   " + _audio_track_label(t))

    audio_lang = (ask_language("Language of the external audio (code)", "eng") or "eng").strip()
    audio_lang3 = _canon3(audio_lang) or "eng"
    audio_title = _lang3_name(audio_lang3) or "English"
    default_audio = (ask_language("Which audio language to set as DEFAULT after muxing "
                                  "(code; empty = the imported one)", audio_lang) or "").strip()
    default_audio3 = _canon3(default_audio) if default_audio else audio_lang3
    default_sub = (ask_language("Subtitle language to set as DEFAULT (code; empty = none)", "cs") or "").strip()
    default_sub3 = _canon3(default_sub) if default_sub else None
    replace = ask_yes_no("Overwrite the original video with the result? (otherwise <name>_merged.mkv)", default_no=True)

    pairs = sorted(k for k in video_map if k in audio_map)
    missing = [k for k in video_map if k not in audio_map]
    if not pairs:
        die("Could not pair any external audio with a video by SxxExx.")
    log_info(f"Paired: {len(pairs)} | without audio: {len(missing)}")
    if not ask_yes_no(f"Run mux for {len(pairs)} videos?", default_no=False):
        log_warn("Cancelled.")
        return
    preset_flush_if_save()

    done = errors = 0
    for key in pairs:
        video_path = video_map[key]
        audio_path = audio_map[key]
        vp = Path(video_path)
        out_path = vp.with_name(vp.stem + "_merged.mkv")
        log_info(f"{_fmt_ep(key)}: {vp.name}  +  {os.path.basename(audio_path)}")
        streams = _ffprobe_streams(ffprobe_bin, video_path)
        cmd = _build_import_audio_cmd(ffmpeg_bin, video_path, audio_path, out_path, streams,
                                      audio_lang3, audio_title)
        r = subprocess.run(cmd)
        if r.returncode != 0 or not out_path.exists():
            log_warn(f"  {vp.name}: mux failed.")
            errors += 1
            continue
        _fix_merged_metadata(mkvmerge_bin, mkvpropedit_bin, out_path, default_audio3, default_sub3)
        if replace:
            try:
                os.remove(str(vp))
                os.replace(str(out_path), str(vp.with_suffix(".mkv")))
                out_path = vp.with_suffix(".mkv")
            except OSError as e:
                log_warn(f"  {vp.name}: could not replace original ({e}).")
        log_done(f"  -> {out_path.name}")
        done += 1
    print()
    log_done(f"Done: {done} muxed, {errors} errors, {len(missing)} without audio.")


# ---- Intelligent file renamer (ported) -------------------------------------
# Unifies file names in a folder: zero-pads numbers to a common width, fills in
# missing common words from the majority pattern, normalizes capitalization,
# strips emoji / Windows-illegal characters, and auto-detects multiple series.

_RN_NUM_RE = re.compile(r"\d+")
_RN_WORDCHARS = re.compile(r"[^\w]", re.UNICODE)
_RN_TOKEN_RE = re.compile(r"\d+|[^\W\d_]+(?:['\u2019][^\W\d_]+)*|[\W_]+", re.UNICODE)
_RN_ILLEGAL_WIN = set('<>:"/\\|?*')
_RN_RESERVED_WIN = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
_RN_EMOJI_RANGES = [(0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF),
                    (0x2B00, 0x2BFF), (0x1F1E6, 0x1F1FF), (0xFE00, 0xFE0F), (0x200D, 0x200D)]
_RN_SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "for", "of", "to",
                   "in", "on", "at", "by", "vs", "with", "as", "from", "into", "over", "per"}
_RN_VOWELS = set("AEIOU")
_RN_DEFAULT_STOP = {
    "episode", "episodes", "episod", "ep", "eps", "reaction", "reactions", "react",
    "reacts", "reacting", "uncut", "full", "part", "pt", "video", "official", "hd",
    "fhd", "uhd", "4k", "2k", "premiere", "finale", "final", "trailer", "teaser",
    "subbed", "sub", "dub", "raw", "movie", "series", "season", "watch", "watching",
    "review", "recap", "highlights", "clip", "clips", "cut", "edit", "compilation",
    "special", "bonus", "early", "access", "kdrama", "drama", "anime",
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "as", "by", "from", "into", "is", "are", "was", "were", "be", "this", "that",
    "these", "those", "here", "there", "now", "new", "my", "your", "our", "their",
    "his", "her", "its", "i", "im", "you", "we", "they", "he", "she", "it", "vs",
    "ft", "feat", "no", "yes", "so", "just",
}


def _rn_strip_pictographs(s):
    out = []
    for ch in s:
        cp = ord(ch)
        if any(a <= cp <= b for a, b in _RN_EMOJI_RANGES):
            continue
        if unicodedata.category(ch) in ("So", "Cf", "Cs", "Co"):
            continue
        out.append(ch)
    return "".join(out)


def _rn_strip_illegal(s):
    return "".join(ch for ch in s
                   if ch not in _RN_ILLEGAL_WIN and unicodedata.category(ch) != "Cc")


def _rn_clean(stem, removes):
    s = _rn_strip_pictographs(stem)
    s = _rn_strip_illegal(s)
    for rgx in removes:
        s = rgx.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def _rn_finalize(stem):
    stem = re.sub(r"[ \t]+", " ", stem).strip().strip(" .")
    if stem.upper() in _RN_RESERVED_WIN:
        stem += "_"
    return stem or "_"


def _rn_tokenize(s):
    toks = []
    for m in _RN_TOKEN_RE.finditer(s):
        t = m.group(0)
        if t.isdigit():
            toks.append(("num", t))
        elif t[0].isalpha() or t[0] in "'\u2019":
            toks.append(("word", t))
        else:
            toks.append(("sep", t))
    return toks


def _rn_is_acronym(tok):
    letters = [c for c in tok if c.isalpha()]
    return len(letters) >= 3 and tok == tok.upper() and not (set(tok.upper()) & _RN_VOWELS)


def _rn_cap_runs(tok):
    return re.sub(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*",
                  lambda m: m.group(0)[0].upper() + m.group(0)[1:],
                  tok.lower(), flags=re.UNICODE)


def _rn_case_word(tok, mode, first):
    if mode == "lower":
        return tok.lower()
    if mode == "upper":
        return tok.upper()
    if mode == "keep":
        return tok
    if _rn_is_acronym(tok):
        return tok
    core = _RN_WORDCHARS.sub("", tok).lower()
    if core in _RN_SMALL_WORDS and not first:
        return tok.lower()
    return _rn_cap_runs(tok)


def _rn_apply_case(tokens, mode):
    out, first = [], True
    for kind, text in tokens:
        if kind == "word":
            out.append((kind, _rn_case_word(text, mode, first)))
            first = False
        else:
            out.append((kind, text))
    return out


def _rn_keys_of(tokens):
    return [("#" if k == "num" else t.lower()) for k, t in tokens if k != "sep"]


def _rn_split_lead(tokens):
    res, lead = [], ""
    for kind, text in tokens:
        if kind == "sep":
            lead += text
        else:
            res.append((lead, kind, text))
            lead = ""
    return res, lead


def _rn_has_letters(key):
    return any(c.isalpha() for c in key)


def _rn_file_words(tokens, stop):
    return [t.lower() for k, t in tokens if k == "word" and t.lower() not in stop]


def _rn_longest_common_run(a, b):
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


def _rn_cluster_series(word_lists, min_run):
    n = len(word_lists)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _rn_longest_common_run(word_lists[i], word_lists[j]) >= min_run:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _rn_compute_widths(cleans, min_width):
    vals, rawlen = defaultdict(list), defaultdict(list)
    for s in cleans:
        for i, run in enumerate(_RN_NUM_RE.findall(s)):
            vals[i].append(int(run))
            rawlen[i].append(len(run))
    widths = {}
    for i in vals:
        w = max(len(str(max(vals[i]))), max(rawlen[i]))
        if min_width:
            w = max(w, min_width)
        widths[i] = w
    return widths


def _rn_lcs_align(ref, other):
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


def _rn_render(out_tokens, widths, trail=""):
    parts, counter = [], 0
    for lead, kind, text in out_tokens:
        parts.append(lead)
        if kind == "num":
            parts.append(text.zfill(widths.get(counter, len(text))))
            counter += 1
        else:
            parts.append(text)
    parts.append(trail)
    return _rn_finalize("".join(parts))


def _rn_consensus_name(item, ref_ns, ref_keys, widths, do_words):
    file_ns = item["ns"]
    file_keys = item["keys"]
    ops = _rn_lcs_align(ref_keys, file_keys)
    matched_ref = {op[1] for op in ops if op[0] == "match"}
    file_word_only = any(op[0] == "file" and file_ns[op[1]][1] == "word" for op in ops)
    out = []
    for op in ops:
        if op[0] == "match":
            out.append(file_ns[op[2]])
        elif op[0] == "file":
            out.append(file_ns[op[1]])
        else:
            r = op[1]
            lead, kind, text = ref_ns[r]
            if not do_words or kind != "word" or file_word_only:
                continue
            bound = ((r + 1 < len(ref_keys) and ref_keys[r + 1] == "#" and (r + 1) not in matched_ref)
                     or (r - 1 >= 0 and ref_keys[r - 1] == "#" and (r - 1) not in matched_ref))
            if bound:
                continue
            out.append((lead, kind, text))
    return _rn_render(out, widths, item["trail"])


def _rn_strict_name(item, ref_ns, ref_keys, ref_trail, widths, ref_num_slots, ref_word_keys):
    nums = [t for _, k, t in item["ns"] if k == "num"]
    file_word_keys = {k for k in item["keys"] if _rn_has_letters(k)}
    overlap = len(ref_word_keys & file_word_keys)
    fits = (len(nums) >= ref_num_slots
            and (not ref_word_keys or overlap >= (len(ref_word_keys) + 1) // 2))
    if not fits:
        return _rn_consensus_name(item, ref_ns, ref_keys, widths, do_words=True)
    out, ni = [], 0
    for lead, kind, text in ref_ns:
        if kind == "num":
            out.append((lead, "num", nums[ni])); ni += 1
        else:
            out.append((lead, kind, text))
    return _rn_render(out, widths, ref_trail)


def _rn_process_group(members, do_pad, do_words, min_width, strict):
    widths = _rn_compute_widths([m["clean"] for m in members], min_width) if do_pad else {}
    patterns = [tuple(m["keys"]) for m in members]
    counts = Counter(patterns)
    best = max(counts, key=lambda p: (counts[p], len(p)))
    ref = next(m for m, p in zip(members, patterns) if p == best)
    ref_ns, ref_trail = ref["ns"], ref["trail"]
    ref_keys = list(best)
    ref_num_slots = sum(1 for k in ref_keys if k == "#")
    ref_word_keys = {k for k in ref_keys if _rn_has_letters(k)}
    out = []
    for m in members:
        if strict:
            new = _rn_strict_name(m, ref_ns, ref_keys, ref_trail, widths, ref_num_slots, ref_word_keys)
        else:
            new = _rn_consensus_name(m, ref_ns, ref_keys, widths, do_words)
        out.append((m["name"], new + m["ext"]))
    return out


def _rn_build_plan(filenames, do_pad, do_words, min_width, removes=(), case_mode="title",
                   strict=False, group=True, group_min=1, stop=None):
    stop = _RN_DEFAULT_STOP if stop is None else (_RN_DEFAULT_STOP | set(stop))
    items = []
    for name in filenames:
        stem, ext = os.path.splitext(name)
        clean = _rn_clean(stem, removes)
        toks = _rn_apply_case(_rn_tokenize(clean), case_mode)
        ns, trail = _rn_split_lead(toks)
        items.append({"name": name, "ext": ext, "clean": clean, "ns": ns, "trail": trail,
                      "keys": _rn_keys_of(toks), "words": _rn_file_words(toks, stop)})
    if group:
        groups = _rn_cluster_series([it["words"] for it in items], group_min)
    else:
        groups = [list(range(len(items)))]
    plan = []
    for gi in groups:
        plan.extend(_rn_process_group([items[i] for i in gi], do_pad, do_words, min_width, strict))
    order = {it["name"]: k for k, it in enumerate(items)}
    plan.sort(key=lambda p: order[p[0]])
    return plan, len(groups)


def _rn_detect_collisions(plan, existing):
    changing = {o: n for o, n in plan if o != n}
    targets = Counter(changing.values())
    sources = set(changing)
    skip = set()
    for o, n in changing.items():
        if targets[n] > 1 or (n in existing and n not in sources):
            skip.add(o)
    return skip


def _rn_apply(changed, directory):
    tag = uuid.uuid4().hex[:8]
    temps = []
    try:
        for i, (o, _) in enumerate(changed):
            tmp = f".__rename_{tag}_{i}__"
            os.rename(os.path.join(directory, o), os.path.join(directory, tmp))
            temps.append(tmp)
        for (o, n), tmp in zip(changed, temps):
            os.rename(os.path.join(directory, tmp), os.path.join(directory, n))
        log_done(f"Renamed {len(changed)} files.")
        return True
    except OSError as e:
        log_warn(f"Rename error: {e}")
        return False


# ---- Total Commander-style interactive file browser (renamer front-end) ----

def _fb_drives():
    """Windows: existing drive roots (C:\\, D:\\ ...). Other OS: filesystem root."""
    if os.name == "nt":
        import string
        out = []
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                out.append(root)
        return out or ["C:\\"]
    return ["/"]


def _fb_list(path, filt):
    """Returns (dirs, files) for path. Dirs sorted; files sorted and filtered by
    filt (glob if it contains wildcards, otherwise a case-insensitive substring)."""
    try:
        entries = os.listdir(path)
    except OSError:
        return [], []
    dirs, files = [], []
    for e in entries:
        try:
            isdir = os.path.isdir(os.path.join(path, e))
        except OSError:
            isdir = False
        (dirs if isdir else files).append(e)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    if filt:
        low = filt.lower()
        if any(ch in filt for ch in "*?[]"):
            files = [x for x in files if fnmatch.fnmatch(x.lower(), low)]
        else:
            files = [x for x in files if low in x.lower()]
    return dirs, files


def _fb_termsize():
    try:
        sz = os.get_terminal_size()
        return max(40, sz.columns), max(10, sz.lines)
    except Exception:
        return 100, 30


def _fb_trunc(s, width):
    if len(s) <= width:
        return s
    return s[:max(1, width - 1)] + "..."


def _fb_write_frame(lines):
    """Draws a frame with minimal flicker (home, per-line clear, clear-to-end)."""
    out = "\x1b[H" + "\n".join(line + "\x1b[K" for line in lines) + "\x1b[J"
    sys.stdout.write(out)
    sys.stdout.flush()


def _fb_prompt_line(label, initial=""):
    """Modal single-line input in raw mode. Enter=confirm (returns text),
    Esc=cancel (returns None). The caller redraws its own frame afterwards."""
    buf = initial
    while True:
        _fb_write_frame([f"{Fore.CYAN}{label}{Style.RESET_ALL}",
                         f"{Fore.GREEN}> {buf}{Style.RESET_ALL}", "",
                         f"{Style.DIM}Enter = confirm | Esc = cancel{Style.RESET_ALL}"])
        k = _read_key()
        if k == "enter":
            return buf
        if k == "esc":
            return None
        if k == "backspace":
            buf = buf[:-1]
        elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
            buf += k[1]


def _fb_pick_location(cwd):
    """Small picker: drives (Windows) / quick locations (Linux). Returns a path
    or None. Navigated with up/down + Enter, Esc cancels."""
    opts = []
    for d in _fb_drives():
        opts.append((f"Drive {d}", d))
    home = os.path.expanduser("~")
    if os.path.isdir(home):
        opts.append((f"Home  ({home})", home))
    opts.append(("Type a path manually...", "__manual__"))
    pos = 0
    while True:
        lines = [f"{Fore.MAGENTA}=== Jump to ==={Style.RESET_ALL}", ""]
        for i, (lbl, _p) in enumerate(opts):
            if i == pos:
                lines.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {lbl}{Style.RESET_ALL}")
            else:
                lines.append(f"  {lbl}")
        lines += ["", f"{Style.DIM}\u2191\u2193 move | Enter = go | Esc = cancel{Style.RESET_ALL}"]
        _fb_write_frame(lines)
        k = _read_key()
        if k == "up":
            pos = (pos - 1) % len(opts)
        elif k == "down":
            pos = (pos + 1) % len(opts)
        elif k == "esc":
            return None
        elif k in ("enter", "right"):
            target = opts[pos][1]
            if target == "__manual__":
                p = _fb_prompt_line("Enter a directory path:", cwd)
                if p and os.path.isdir(p):
                    return os.path.abspath(p)
                return None
            if os.path.isdir(target):
                return os.path.abspath(target)
            return None


def _fb_rename_preview(cwd, files):
    """Interactive rename preview for the given files. Toggle options with keys and
    watch the before/after update live; apply with Enter/F2. Returns True if files
    were renamed, else False. Esc/q cancels back to the browser."""
    opts = {"pad": False, "case": "title", "words": False, "strict": False, "group": False}
    removes, removes_src = [], []
    top = 0
    while True:
        plan, n_groups = _rn_build_plan(
            files, opts["pad"], (True if opts["strict"] else opts["words"]), 0,
            removes=removes, case_mode=opts["case"], strict=opts["strict"],
            group=opts["group"], group_min=1)
        try:
            existing = set(os.listdir(cwd))
        except OSError:
            existing = set()
        skip = _rn_detect_collisions(plan, existing)
        changed = [(o, n) for o, n in plan if o != n and o not in skip]
        skipped = [(o, n) for o, n in plan if o in skip]
        unchanged = [(o, n) for o, n in plan if o == n]

        cols, rows = _fb_termsize()
        header = [
            f"{Fore.MAGENTA}=== Rename preview - {len(files)} file(s) ==={Style.RESET_ALL}",
            (f"{Fore.CYAN}[p]{Style.RESET_ALL} pad:{_fb_onoff(opts['pad'])}   "
             f"{Fore.CYAN}[c]{Style.RESET_ALL} case:{opts['case']:5}   "
             f"{Fore.CYAN}[w]{Style.RESET_ALL} fill-words:{_fb_onoff(opts['words'])}   "
             f"{Fore.CYAN}[s]{Style.RESET_ALL} strict:{_fb_onoff(opts['strict'])}   "
             f"{Fore.CYAN}[g]{Style.RESET_ALL} series:{_fb_onoff(opts['group'])}"),
            (f"{Fore.CYAN}[x]{Style.RESET_ALL} add strip-regex   "
             f"{Fore.CYAN}[X]{Style.RESET_ALL} clear   "
             + (f"strip: {', '.join(removes_src)}" if removes_src else "strip: (none)")),
            "",
        ]
        body = []
        rows_avail = max(6, rows - len(header) - 3)
        view = changed + skipped
        if top > max(0, len(view) - rows_avail):
            top = max(0, len(view) - rows_avail)
        # dynamic column: align arrows to the longest old name, no wider than needed
        max_old = max((len(o) for o, _n in view), default=10)
        oldw = min(max_old, max(10, cols - 6 - 24))
        destw = max(16, cols - 6 - oldw)
        for o, n in view[top:top + rows_avail]:
            conflict = o in skip
            arrow = f"{Fore.RED}->{Style.RESET_ALL}" if conflict else f"{Fore.CYAN}->{Style.RESET_ALL}"
            ncol = Fore.RED if conflict else Fore.GREEN
            tail = "  [CONFLICT]" if conflict else ""
            body.append(f"  {_fb_trunc(o, oldw):<{oldw}} {arrow} {ncol}{_fb_trunc(n, destw)}{tail}{Style.RESET_ALL}")
        if not view:
            body.append(f"  {Style.DIM}(nothing changes with the current options){Style.RESET_ALL}")

        summary = (f"{Fore.GREEN}Changed: {len(changed)}{Style.RESET_ALL}   "
                   f"{Fore.RED}Conflicts: {len(skipped)}{Style.RESET_ALL}   "
                   f"{Style.DIM}Unchanged: {len(unchanged)}{Style.RESET_ALL}")
        footer = (f"{Style.DIM}\u2191\u2193/PgUp/PgDn scroll | Enter = APPLY | Esc = cancel"
                  f"{Style.RESET_ALL}")
        _fb_write_frame(header + body + ["", summary, footer])

        k = _read_key()
        if k in ("esc",) or k == ("char", "q"):
            return False
        if k == "enter":
            if not changed:
                continue
            _fb_leave_screen()
            _rn_apply(changed, cwd)
            try:
                input(f"\n{Fore.CYAN}Renamed. Press Enter to continue...{Style.RESET_ALL}")
            except (EOFError, KeyboardInterrupt):
                pass
            return True
        if k == "down":
            top = min(max(0, len(view) - rows_avail), top + 1)
        elif k == "up":
            top = max(0, top - 1)
        elif k == "pgdn":
            top = min(max(0, len(view) - rows_avail), top + rows_avail)
        elif k == "pgup":
            top = max(0, top - rows_avail)
        elif isinstance(k, tuple) and k[0] == "char":
            ch = k[1]
            if ch == "p":
                opts["pad"] = not opts["pad"]
            elif ch == "w":
                opts["words"] = not opts["words"]
            elif ch == "s":
                opts["strict"] = not opts["strict"]
            elif ch == "g":
                opts["group"] = not opts["group"]
            elif ch == "c":
                order = ["title", "keep", "lower", "upper"]
                opts["case"] = order[(order.index(opts["case"]) + 1) % len(order)]
            elif ch == "x":
                rx = _fb_prompt_line("Regex to strip from names (e.g. \\(UNCUT\\) or \\[1080p\\]):", "")
                if rx:
                    try:
                        removes.append(re.compile(rx))
                        removes_src.append(rx)
                    except re.error:
                        pass
            elif ch == "X":
                removes, removes_src = [], []


def _fb_onoff(v):
    return f"{Fore.GREEN}ON {Style.RESET_ALL}" if v else f"{Style.DIM}off{Style.RESET_ALL}"


def _fb_enter_screen():
    sys.stdout.write("\x1b[?25l")   # hide cursor
    sys.stdout.flush()


def _fb_leave_screen():
    sys.stdout.write("\x1b[?25h\x1b[0m")   # show cursor, reset
    sys.stdout.flush()


def run_rename_files(args, mode="rename"):
    """Total Commander-style interactive file browser. Navigate folders / up / drives,
    tag files, filter live. mode='rename' offers local + online rename; mode='subs'
    downloads subtitles from OpenSubtitles for the tagged/shown videos."""
    is_subs = (mode == "subs")
    start = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(start):
        start = os.path.dirname(start) or "."
    cwd = os.path.abspath(start)

    if not _tui_supported():
        log_warn("The interactive file browser needs a real terminal (a TTY).")
        log_info("Run it directly on your machine's console, not through a pipe/redirect.")
        return

    tagged = set()
    filt = ""
    cursor = 0
    top = 0
    status = ""

    while True:   # outer loop: lets the online action leave raw mode and come back
        filter_editing = False
        online_files = None
        action = "quit"
        with _RawMode():
            _fb_enter_screen()
            try:
                while True:
                    dirs, files = _fb_list(cwd, filt)
                    rows_items = [("..", "up")] + [(d, "dir") for d in dirs] + [(f, "file") for f in files]
                    n = len(rows_items)
                    cursor = max(0, min(cursor, n - 1))

                    cols, rows = _fb_termsize()
                    head = [
                        f"{Fore.MAGENTA}{Style.BRIGHT}=== File browser - "
                        + ("download subtitles" if is_subs else "rename (local + online)")
                        + f" ==={Style.RESET_ALL}",
                        f"{Fore.CYAN}Path:{Style.RESET_ALL} {_fb_trunc(cwd, cols - 8)}",
                    ]
                    tagcount = len([f for f in files if f in tagged])
                    if filter_editing:
                        head.append(f"{Fore.YELLOW}Filter (typing):{Style.RESET_ALL} {filt}_   "
                                    f"{Style.DIM}Enter=keep Esc=clear{Style.RESET_ALL}")
                    else:
                        head.append(f"{Fore.CYAN}Filter:{Style.RESET_ALL} "
                                    + (filt if filt else f"{Style.DIM}(none){Style.RESET_ALL}")
                                    + f"    {Fore.CYAN}Tagged:{Style.RESET_ALL} {tagcount}/{len(files)}")
                    head.append("")

                    foot = [
                        "",
                        (f"{Style.DIM}\u2191\u2193 move | Enter/\u2192 open | \u2190/Bksp up | Space tag | "
                         f"* all | / filter | d drive | "
                         + ("s DOWNLOAD subs" if is_subs else "r RENAME | o ONLINE(TMDB)")
                         + f" | q quit{Style.RESET_ALL}"),
                    ]
                    if status:
                        foot.append(f"{Fore.YELLOW}{status}{Style.RESET_ALL}")

                    body_rows = max(4, rows - len(head) - len(foot))
                    if cursor < top:
                        top = cursor
                    elif cursor >= top + body_rows:
                        top = cursor - body_rows + 1
                    top = max(0, top)

                    body = []
                    view = rows_items[top:top + body_rows]
                    for idx, (name, kind) in enumerate(view, start=top):
                        cur = (idx == cursor)
                        tag = "*" if (kind == "file" and name in tagged) else " "
                        if kind == "up":
                            base, col = ".. (up one level)", Fore.CYAN
                        elif kind == "dir":
                            base, col = "[" + name + "]", Fore.CYAN + Style.BRIGHT
                        else:
                            base = name
                            col = Fore.YELLOW if name in tagged else Fore.WHITE
                        disp = _fb_trunc(base, cols - 6)
                        if cur:
                            body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a{tag} {disp}{Style.RESET_ALL}")
                        else:
                            body.append(f" {tag} {col}{disp}{Style.RESET_ALL}")
                    rest = n - (top + len(view))
                    if rest > 0:
                        body.append(f"  {Fore.CYAN}v ({rest} more){Style.RESET_ALL}")

                    _fb_write_frame(head + body + foot)
                    status = ""
                    k = _read_key()

                    if filter_editing:
                        if k == "enter":
                            filter_editing = False
                        elif k == "esc":
                            filter_editing = False
                            filt = ""
                            cursor = 0
                        elif k == "backspace":
                            filt = filt[:-1]
                            cursor = 0
                        elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
                            filt += k[1]
                            cursor = 0
                        continue

                    if k == "up":
                        cursor = (cursor - 1) % n
                    elif k == "down":
                        cursor = (cursor + 1) % n
                    elif k == "pgup":
                        cursor = max(0, cursor - body_rows)
                    elif k == "pgdn":
                        cursor = min(n - 1, cursor + body_rows)
                    elif k == "home":
                        cursor = 0
                    elif k == "end":
                        cursor = n - 1
                    elif k in ("enter", "right"):
                        name, kind = rows_items[cursor]
                        if kind == "up":
                            parent = os.path.dirname(cwd.rstrip(os.sep)) or cwd
                            if os.path.isdir(parent):
                                cwd, cursor, top, tagged, filt = os.path.abspath(parent), 0, 0, set(), ""
                        elif kind == "dir":
                            target = os.path.join(cwd, name)
                            if os.path.isdir(target):
                                cwd, cursor, top, tagged, filt = os.path.abspath(target), 0, 0, set(), ""
                        else:
                            tagged.symmetric_difference_update({name})
                    elif k in ("left", "backspace"):
                        parent = os.path.dirname(cwd.rstrip(os.sep)) or cwd
                        if os.path.isdir(parent):
                            cwd, cursor, top, tagged, filt = os.path.abspath(parent), 0, 0, set(), ""
                    elif isinstance(k, tuple) and k[0] == "char":
                        ch = k[1]
                        if ch == " ":
                            name, kind = rows_items[cursor]
                            if kind == "file":
                                tagged.symmetric_difference_update({name})
                                cursor = min(n - 1, cursor + 1)
                        elif ch == "*":
                            if all(f in tagged for f in files):
                                tagged -= set(files)
                            else:
                                tagged |= set(files)
                        elif ch == "/":
                            filter_editing = True
                        elif ch in ("d", "D"):
                            dest = _fb_pick_location(cwd)
                            if dest:
                                cwd, cursor, top, tagged, filt = dest, 0, 0, set(), ""
                        elif ch in ("r", "R", "F") and not is_subs:
                            sel = [f for f in files if f in tagged] or list(files)
                            if not sel:
                                status = "No files here to rename."
                            else:
                                _fb_rename_preview(cwd, sel)
                                tagged = set()
                        elif ch in ("o", "O") and not is_subs:
                            vids = [os.path.join(cwd, f) for f in (list(tagged) if tagged else files)
                                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS_BATCH]
                            if not vids:
                                status = "No videos here for online rename (tag some, or open a folder with videos)."
                            else:
                                online_files = vids
                                action = "online"
                                break
                        elif ch in ("s", "S") and is_subs:
                            vids = [os.path.join(cwd, f) for f in (list(tagged) if tagged else files)
                                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS_BATCH]
                            if not vids:
                                status = "No videos here for subtitle download (tag some, or open a folder with videos)."
                            else:
                                online_files = vids
                                action = "subs"
                                break
                        elif ch in ("q", "Q"):
                            action = "quit"
                            break
                        elif ch in ("?", "h"):
                            _fb_help_overlay(is_subs)
                    elif k == "esc":
                        if filt:
                            filt = ""
                            cursor = 0
                        else:
                            action = "quit"
                            break
            finally:
                _fb_leave_screen()
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.flush()

        if action == "online":
            # normal terminal mode here (raw mode released) -> the online preview runs
            _online_rename_core(args, online_files)
            tagged = set()
            continue
        if action == "subs":
            fails = _subs_download_core(args, online_files) or 0
            if fails:
                try:
                    input(f"\n{Fore.CYAN}Some downloads failed - press Enter to return...{Style.RESET_ALL}")
                except (EOFError, KeyboardInterrupt):
                    return
            tagged = set()
            continue
        return


def _fb_help_overlay(is_subs=False):
    lines = [
        f"{Fore.MAGENTA}{Style.BRIGHT}=== File browser - help ==={Style.RESET_ALL}", "",
        f"{Fore.CYAN}Navigation{Style.RESET_ALL}",
        "  Up/Down, PgUp/PgDn, Home/End   move the cursor",
        "  Enter / Right                  open a folder (or tag a file)",
        "  Left / Backspace               go up one level",
        "  d                              jump to a drive / home / typed path",
        "", f"{Fore.CYAN}Selecting{Style.RESET_ALL}",
        "  Space                          tag / untag the file under the cursor",
        "  *                              tag / untag ALL files shown",
        "  /                              live filter (type to narrow, Enter keeps, Esc clears)",
        "",
    ]
    if is_subs:
        lines += [
            f"{Fore.CYAN}Subtitles{Style.RESET_ALL}",
            "  s                              download SUBTITLES for the tagged files",
            "                                 (or all shown files if none are tagged)",
            "  In preview: arrows move, Enter = pick a version for this file,",
            "              Tab = info, a = download all (best), l languages (multi),",
            "              h hearing-impaired, o overwrite, m change match, Esc = cancel",
        ]
    else:
        lines += [
            f"{Fore.CYAN}Renaming{Style.RESET_ALL}",
            "  r                              open the rename preview for the tagged files",
            "                                 (or all shown files if none are tagged)",
            "  In preview: p/c/w/s/g toggle options, x/X strip-regex,",
            "              Enter = apply, Esc = cancel",
            "  o                              ONLINE rename via TMDB (recognizes the show/movie,",
            "                                 fetches episode titles, applies Plex naming)",
        ]
    lines += [
        "", f"{Fore.CYAN}Other{Style.RESET_ALL}",
        "  q / Esc                        quit back to the menu",
        "", f"{Style.DIM}Press any key to return...{Style.RESET_ALL}",
    ]
    _fb_write_frame(lines)
    _read_key()


# ============================================================================
# Video browser + inspector (Total Commander style) - full per-video toolbox
# ============================================================================

_AUDIO_FILE_EXTS = {".aac", ".ac3", ".eac3", ".dts", ".thd", ".mp3", ".flac",
                    ".mka", ".opus", ".ogg", ".wav", ".m4a"}
_SUB_FILE_EXTS = {".srt", ".ass", ".ssa", ".sup", ".vtt"}


def _vid_human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024


def _vid_human_dur(sec):
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "?"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _vid_fps(rate):
    try:
        a, b = rate.split("/")
        a, b = float(a), float(b)
        return (a / b) if b else 0.0
    except Exception:
        try:
            return float(rate)
        except Exception:
            return 0.0


def _vid_bitrate(bps):
    try:
        v = float(bps)
    except (TypeError, ValueError):
        return None
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} Mbps"
    if v >= 1000:
        return f"{v / 1000:.0f} kbps"
    return f"{v:.0f} bps"


def _vid_probe(args, path):
    """Rich probe of a single video: ffprobe for stream/format details, mkvmerge
    (for MKV) to obtain track IDs/selectors + attachments + chapters. Returns a
    unified info dict used both for the report and for the operations."""
    p = Path(path)
    ext = p.suffix.lower()
    is_mkv = ext in (".mkv", ".webm", ".mka")
    ffmpeg = _ensure_ffmpeg_bin(args, str(p.parent))
    ffprobe = _find_ffprobe(ffmpeg)
    mkvmerge = getattr(args, "mkvmerge", None) or find_tool(["mkvmerge", "mkvmerge.exe"])
    mkvextract = getattr(args, "mkvextract", None) or find_tool(["mkvextract", "mkvextract.exe"])
    mkvpropedit = _find_mkvpropedit(mkvmerge)

    streams = _ffprobe_streams(ffprobe, p) if ffprobe else []
    fmt = {}
    chapters = 0
    if ffprobe:
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_chapters",
                                  "-print_format", "json", str(p)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            data = json.loads(out.stdout or "{}")
            fmt = data.get("format", {})
            chapters = len(data.get("chapters", []))
        except Exception:
            pass

    mk = {"tracks": [], "attachments": []}
    if mkvmerge and is_mkv:
        try:
            mk = json.loads(subprocess.run([mkvmerge, "-J", str(p)], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout or "{}")
        except Exception:
            mk = {"tracks": [], "attachments": []}
    mk_audio = [t for t in mk.get("tracks", []) if t.get("type") == "audio"]
    mk_subs = [t for t in mk.get("tracks", []) if t.get("type") == "subtitles"]

    video, audio, subs = [], [], []
    ai = si = 0
    for s in streams:
        t = s.get("codec_type")
        disp = s.get("disposition", {}) or {}
        tags = s.get("tags", {}) or {}
        if t == "video" and not disp.get("attached_pic"):
            video.append({
                "codec": s.get("codec_name", "?"), "w": s.get("width"), "h": s.get("height"),
                "fps": _vid_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or "0"),
                "bitrate": _vid_bitrate(s.get("bit_rate") or fmt.get("bit_rate")),
                "pix": s.get("pix_fmt", ""),
            })
        elif t == "audio":
            rec = {
                "ord": ai, "index": s.get("index"), "codec": s.get("codec_name", "?"),
                "channels": s.get("channels"), "sr": s.get("sample_rate"),
                "bitrate": _vid_bitrate(s.get("bit_rate")),
                "lang": _canon3(tags.get("language")), "name": tags.get("title", ""),
                "default": bool(disp.get("default")), "forced": bool(disp.get("forced")),
            }
            if ai < len(mk_audio):
                rec["mkv_id"] = mk_audio[ai]["id"]
                rec["sel"] = f"a{ai + 1}"
                if not rec["name"]:
                    rec["name"] = (mk_audio[ai].get("properties", {}) or {}).get("track_name", "") or ""
            audio.append(rec)
            ai += 1
        elif t == "subtitle":
            codec = s.get("codec_name", "?")
            rec = {
                "ord": si, "index": s.get("index"), "codec": codec,
                "lang": _canon3(tags.get("language")), "name": tags.get("title", ""),
                "default": bool(disp.get("default")), "forced": bool(disp.get("forced")),
                "text": codec.lower() in ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"),
            }
            if si < len(mk_subs):
                rec["mkv_id"] = mk_subs[si]["id"]
                rec["sel"] = f"s{si + 1}"
                if not rec["name"]:
                    rec["name"] = (mk_subs[si].get("properties", {}) or {}).get("track_name", "") or ""
            subs.append(rec)
            si += 1

    atts = []
    for a in mk.get("attachments", []):
        atts.append({"id": a.get("id"), "name": a.get("file_name", "?"),
                     "mime": a.get("content_type", ""), "size": a.get("size")})

    return {
        "path": p, "ext": ext, "is_mkv": is_mkv,
        "container": ext.lstrip(".").upper(),
        "size": fmt.get("size"), "duration": fmt.get("duration"),
        "bitrate": _vid_bitrate(fmt.get("bit_rate")),
        "video": video, "audio": audio, "subs": subs,
        "chapters": chapters, "attachments": atts,
        "ffmpeg": ffmpeg, "ffprobe": ffprobe, "mkvmerge": mkvmerge,
        "mkvextract": mkvextract, "mkvpropedit": mkvpropedit,
    }


def _vid_report_lines(info):
    """Colorized, detailed breakdown of the video for the inspector header."""
    p = info["path"]
    L = []
    L.append(f"{Fore.MAGENTA}{Style.BRIGHT}=== {_fb_trunc(p.name, 70)} ==={Style.RESET_ALL}")
    meta = f"{info['container']}  |  {_vid_human_size(info['size'])}  |  {_vid_human_dur(info['duration'])}"
    if info["bitrate"]:
        meta += f"  |  {info['bitrate']}"
    L.append(f"{Fore.CYAN}{meta}{Style.RESET_ALL}")
    for v in info["video"]:
        res = f"{v['w']}x{v['h']}" if v.get("w") else "?"
        fps = f"{v['fps']:.3f}".rstrip("0").rstrip(".") + "fps" if v.get("fps") else ""
        extra = "  ".join(x for x in [v["codec"], res, fps, v.get("bitrate") or "", v.get("pix", "")] if x)
        L.append(f"  {Fore.GREEN}Video{Style.RESET_ALL}  {extra}")
    if info["audio"]:
        L.append(f"  {Fore.GREEN}Audio ({len(info['audio'])}){Style.RESET_ALL}")
        for a in info["audio"]:
            flags = []
            if a["default"]:
                flags.append("default")
            if a["forced"]:
                flags.append("forced")
            fl = f"  [{', '.join(flags)}]" if flags else ""
            ch = f"{a['channels']}ch" if a.get("channels") else ""
            nm = f'  "{a["name"]}"' if a.get("name") else ""
            idtag = f"#{a.get('mkv_id', a['ord'])}"
            L.append(f"    A{a['ord'] + 1} {idtag}  {_lang3_name(a['lang']):8} {a['codec']:6} {ch:4} "
                     f"{a.get('bitrate') or '':9}{fl}{nm}")
    if info["subs"]:
        L.append(f"  {Fore.GREEN}Subtitles ({len(info['subs'])}){Style.RESET_ALL}")
        for s in info["subs"]:
            flags = []
            if s["default"]:
                flags.append("default")
            if s["forced"]:
                flags.append("forced")
            fl = f"  [{', '.join(flags)}]" if flags else ""
            kind = "text" if s.get("text") else "image"
            nm = f'  "{s["name"]}"' if s.get("name") else ""
            idtag = f"#{s.get('mkv_id', s['ord'])}"
            L.append(f"    S{s['ord'] + 1} {idtag}  {_lang3_name(s['lang']):8} {s['codec']:8} ({kind}){fl}{nm}")
    tail = []
    if info["chapters"]:
        tail.append(f"Chapters: {info['chapters']}")
    if info["attachments"]:
        tail.append(f"Attachments: {len(info['attachments'])}")
    if tail:
        L.append(f"  {Fore.CYAN}{'  |  '.join(tail)}{Style.RESET_ALL}")
    L.append("")
    return L


# ---- helpers shared by the operations -------------------------------------

def _vid_ask_out_mode(default_new=True):
    """Overwrite in place, or write a new file next to it?"""
    return not ask_yes_no("Overwrite the original file in place? (otherwise a new file is written next to it)",
                          default_no=default_new)


def _vid_replace(tmp, target):
    try:
        os.replace(str(tmp), str(target))
        return True
    except OSError as e:
        log_warn(f"Could not replace the file ({e}).")
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        return False


def _vid_pick_tracks(info, kinds, prompt):
    """Checklist over audio+/subs tracks. kinds subset of {'audio','subs'}.
    Returns list of ('audio'|'subs', rec) or None if cancelled/none."""
    rows = []
    labels = []
    for k in kinds:
        for t in info[k]:
            rows.append((k, t))
            tag = "A" if k == "audio" else "S"
            flags = [x for x in ("default" if t["default"] else "", "forced" if t["forced"] else "") if x]
            fl = f"  [{', '.join(flags)}]" if flags else ""
            nm = f'  "{t["name"]}"' if t.get("name") else ""
            ch = f"  {t['channels']}ch" if t.get("channels") else ""
            labels.append(f"{tag}{t['ord'] + 1}  {_lang3_name(t['lang']):8} {t['codec']}{ch}{fl}{nm}")
    if not rows:
        log_warn("No matching tracks.")
        return None
    act, checked = ask_checklist(prompt, labels, ["Confirm CHECKED", "cancel"],
                                 header=[f"{Fore.CYAN}Check with space, confirm with Enter.{Style.RESET_ALL}"])
    if act != 0 or not checked:
        raise WizardBack()   # user cancelled -> straight back to the video's menu
    return [rows[i] for i in checked if 0 <= i < len(rows)]


def _vid_dir_files(info, exts):
    d = info["path"].parent
    out = []
    try:
        for f in sorted(os.listdir(d)):
            if Path(f).suffix.lower() in exts and (d / f).is_file():
                out.append(str(d / f))
    except OSError:
        pass
    return out


# ---- operations -----------------------------------------------------------

def _vid_extract_audio(args, info):
    if not info["audio"]:
        log_warn("This video has no audio tracks.")
        return None
    picks = _vid_pick_tracks(info, ["audio"], "Which audio tracks to EXTRACT?")
    if not picks:
        return None
    ff = info["ffmpeg"]
    p = info["path"]
    used = set()
    for _k, t in picks:
        ext = _audio_ext_for(t["codec"])
        tag = (t["lang"] or "und").upper()
        out = p.with_name(p.stem + f"_{tag}{ext}")
        n = 2
        while str(out) in used or out.exists():
            out = p.with_name(p.stem + f"_{tag}_{n}{ext}")
            n += 1
        used.add(str(out))
        cmd = [ff, "-y", "-i", str(p), "-map", f"0:a:{t['ord']}", "-c:a", "copy", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and out.exists():
            log_done(f"Audio A{t['ord'] + 1} ({t['codec']}) -> {out.name}")
        else:
            log_warn(f"A{t['ord'] + 1}: extraction failed.")
    return None


def _vid_extract_subs(args, info):
    if not info["subs"]:
        log_warn("This video has no subtitle tracks.")
        return None
    picks = _vid_pick_tracks(info, ["subs"], "Which subtitle tracks to EXTRACT (to .srt)?")
    if not picks:
        return None
    p = info["path"]
    for _k, t in picks:
        if not t.get("text"):
            log_warn(f"S{t['ord'] + 1} ({t['codec']}) is image-based (PGS/VobSub) - cannot go to .srt, skipping.")
            continue
        out = p.with_name(p.stem + f".{t['lang'] if t['lang'] != 'und' else 'sub' + str(t['ord'] + 1)}.srt")
        n = 2
        while out.exists():
            out = p.with_name(p.stem + f".{t['lang']}.{n}.srt")
            n += 1
        try:
            if info["is_mkv"] and info["mkvextract"] and t.get("mkv_id") is not None:
                extract_subtitle_to_srt(info["mkvextract"], p, t["mkv_id"], out)
            else:
                extract_subtitle_via_ffmpeg(info["ffmpeg"], p, t["ord"], out)
            log_done(f"Subtitles S{t['ord'] + 1} ({t['lang']}) -> {out.name}")
        except SystemExit:
            log_warn(f"S{t['ord'] + 1}: extraction failed.")
        except Exception as e:
            log_warn(f"S{t['ord'] + 1}: {e}")
    return None


def _vid_remove_tracks(args, info):
    picks = _vid_pick_tracks(info, ["audio", "subs"], "Which tracks to REMOVE (delete)?")
    if not picks:
        return None
    rem_a = {t["ord"] for k, t in picks if k == "audio"}
    rem_s = {t["ord"] for k, t in picks if k == "subs"}
    if len(rem_a) >= len(info["audio"]) and info["audio"]:
        log_warn("Refusing to remove ALL audio tracks.")
        return None
    p = info["path"]
    in_place = not _vid_ask_out_mode()
    if info["is_mkv"] and info["mkvmerge"]:
        keep_a = [t["mkv_id"] for t in info["audio"] if t["ord"] not in rem_a and t.get("mkv_id") is not None]
        keep_s = [t["mkv_id"] for t in info["subs"] if t["ord"] not in rem_s and t.get("mkv_id") is not None]
        tmp = p.with_name(p.stem + ".rmtmp.mkv")
        cmd = [info["mkvmerge"], "-o", str(tmp)]
        if info["audio"] and not keep_a:
            cmd += ["--no-audio"]
        elif keep_a and len(keep_a) < len(info["audio"]):
            cmd += ["--audio-tracks", ",".join(map(str, keep_a))]
        if info["subs"] and not keep_s:
            cmd += ["--no-subtitles"]
        elif keep_s and len(keep_s) < len(info["subs"]):
            cmd += ["--subtitle-tracks", ",".join(map(str, keep_s))]
        cmd += [str(p)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode >= 2 or not tmp.exists():
            log_warn("mkvmerge failed.")
            tmp.exists() and os.remove(str(tmp))
            return None
    else:
        ff = info["ffmpeg"]
        tmp = p.with_name(p.stem + ".rmtmp" + p.suffix)
        maps = ["-map", "0:v"]
        for t in info["audio"]:
            if t["ord"] not in rem_a:
                maps += ["-map", f"0:a:{t['ord']}"]
        for t in info["subs"]:
            if t["ord"] not in rem_s:
                maps += ["-map", f"0:s:{t['ord']}"]
        cmd = [ff, "-y", "-i", str(p)] + maps + ["-c", "copy", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not tmp.exists():
            log_warn("ffmpeg failed.")
            tmp.exists() and os.remove(str(tmp))
            return None
    target = p if in_place else p.with_name(p.stem + ".trimmed" + p.suffix)
    if _vid_replace(tmp, target):
        log_done(f"Removed {len(rem_a)} audio + {len(rem_s)} subtitle track(s) -> {target.name}")
        return target if in_place else info["path"]
    return None


def _vid_pick_language(current):
    """Searchable full-screen language picker (name + code). Type to filter, arrows
    to move, Enter selects (returns a 3-letter code), Esc cancels (returns None).
    A '(type a custom code)' entry lets you enter any code manually."""
    base = {}
    for c2, name in LANGUAGE_NAMES.items():
        base[_canon3(c2)] = name
    items = [("(undetermined)", "und")] + sorted(((n, c) for c, n in base.items()), key=lambda x: x[0])
    items.append(("(type a custom code...)", "__custom__"))
    filt = ""
    pos = 0
    top = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                f = filt.lower().strip()
                if f:
                    view = [(n, c) for n, c in items
                            if c not in ("__custom__",) and (f in n.lower() or f in c.lower())]
                    view.append(("(type a custom code...)", "__custom__"))
                else:
                    view = items
                if not view:
                    view = [("(no match - Esc to cancel)", "")]
                pos = max(0, min(pos, len(view) - 1))
                cols, rows = _fb_termsize()
                head = [f"{Fore.MAGENTA}{Style.BRIGHT}=== Select language ==={Style.RESET_ALL}",
                        f"{Fore.CYAN}Current:{Style.RESET_ALL} {current or 'und'}    "
                        f"{Fore.YELLOW}Search:{Style.RESET_ALL} {filt}_", ""]
                foot = ["", f"{Style.DIM}\u2191\u2193 move | type to search | Enter = select | Esc = cancel{Style.RESET_ALL}"]
                avail = max(3, rows - len(head) - len(foot))
                if pos < top:
                    top = pos
                elif pos >= top + avail:
                    top = pos - avail + 1
                top = max(0, top)
                body = []
                for i, (n, c) in enumerate(view[top:top + avail], start=top):
                    label = f"{n}  ({c})" if c and not c.startswith("__") else n
                    if i == pos:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {label}{Style.RESET_ALL}")
                    else:
                        body.append(f"  {Fore.CYAN}{label}{Style.RESET_ALL}")
                _fb_write_frame(head + body + foot)
                k = _read_key()
                if k == "esc":
                    return None
                elif k in ("enter", "right"):
                    n, c = view[pos]
                    if c == "__custom__":
                        code = _fb_prompt_line("Type a language code (e.g. eng, cze, jpn):", current or "")
                        return _canon3(code) if code else None
                    return c or None
                elif k == "up":
                    pos = (pos - 1) % len(view)
                elif k == "down":
                    pos = (pos + 1) % len(view)
                elif k == "pgup":
                    pos = max(0, pos - avail)
                elif k == "pgdn":
                    pos = min(len(view) - 1, pos + avail)
                elif k == "home":
                    pos = 0
                elif k == "end":
                    pos = len(view) - 1
                elif k == "backspace":
                    filt = filt[:-1]
                    pos = top = 0
                elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
                    filt += k[1]
                    pos = top = 0
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _vid_track_list_screen(info, tracks, start=0):
    """Arrow-key list of the video's audio+subtitle tracks. Enter on a track returns
    its index (to open the editor form); Esc/q returns None (done)."""
    pos = max(0, min(start, len(tracks) - 1))
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                lines = [f"{Fore.MAGENTA}{Style.BRIGHT}=== Edit tracks - {_fb_trunc(info['path'].name, 60)} ==={Style.RESET_ALL}",
                         f"{Fore.CYAN}Enter on a track to edit its name / language / flags.{Style.RESET_ALL}", ""]
                for i, (kind, t) in enumerate(tracks):
                    tag = ("A" if kind == "audio" else "S") + str(t["ord"] + 1)
                    flags = [x for x in ("default" if t["default"] else "", "forced" if t["forced"] else "") if x]
                    fl = f"  [{', '.join(flags)}]" if flags else ""
                    nm = f'  "{t["name"]}"' if t.get("name") else ""
                    ch = f"  {t['channels']}ch" if t.get("channels") else ""
                    row = f"{tag}  {_lang3_name(t['lang']):8} {t['codec']}{ch}{fl}{nm}"
                    if i == pos:
                        lines.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {row}{Style.RESET_ALL}")
                    else:
                        colr = Fore.YELLOW if kind == "audio" else Fore.CYAN
                        lines.append(f"  {colr}{row}{Style.RESET_ALL}")
                lines += ["", f"{Style.DIM}\u2191\u2193 move | Enter = edit this track | Esc = done{Style.RESET_ALL}"]
                _fb_write_frame(lines)
                k = _read_key()
                if k == "up":
                    pos = (pos - 1) % len(tracks)
                elif k == "down":
                    pos = (pos + 1) % len(tracks)
                elif k == "home":
                    pos = 0
                elif k == "end":
                    pos = len(tracks) - 1
                elif k in ("enter", "right"):
                    return pos
                elif k == "esc" or k == ("char", "q"):
                    return None
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _vid_track_form(kind, t):
    """Second screen: a field editor for ONE track. Navigate rows with Up/Down;
    type to edit text (Name / Language), Left/Right/Space toggles Default/Forced;
    Enter on [Confirm] applies, [Cancel]/Esc discards. Returns a changes dict
    (only fields that differ from the current value) or None on cancel."""
    tag = ("A" if kind == "audio" else "S") + str(t["ord"] + 1)
    fields = [
        {"key": "name", "label": "Name", "type": "text", "val": t.get("name") or ""},
        {"key": "lang", "label": "Language", "type": "text", "val": (t["lang"] if t["lang"] and t["lang"] != "und" else "")},
        {"key": "default", "label": "Default", "type": "bool", "val": bool(t["default"])},
        {"key": "forced", "label": "Forced", "type": "bool", "val": bool(t["forced"])},
    ]
    orig = {f["key"]: f["val"] for f in fields}
    rows = fields + [{"action": "confirm", "label": "[ Confirm changes ]"},
                     {"action": "cancel", "label": "[ Cancel ]"}]
    pos = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                lines = [f"{Fore.MAGENTA}{Style.BRIGHT}=== Edit track {tag}  ({_lang3_name(t['lang'])} {t['codec']}) ==={Style.RESET_ALL}", ""]
                for i, r in enumerate(rows):
                    cur = (i == pos)
                    mark = f"{Fore.GREEN}{Style.BRIGHT}\u203a{Style.RESET_ALL}" if cur else " "
                    if "action" in r:
                        col = Fore.GREEN if r["action"] == "confirm" else Fore.RED
                        style = Style.BRIGHT if cur else ""
                        lines.append(f"{mark} {col}{style}{r['label']}{Style.RESET_ALL}")
                    elif r["type"] == "text":
                        shown = r["val"] + ("_" if cur else "")
                        if not r["val"] and not cur:
                            shown = f"{Style.DIM}(empty){Style.RESET_ALL}"
                        hl = f"{Fore.YELLOW}" if cur else ""
                        hint = f"  {Style.DIM}(Enter = pick from list){Style.RESET_ALL}" if (cur and r["key"] == "lang") else ""
                        lines.append(f"{mark} {hl}{r['label']:9}:{Style.RESET_ALL} {shown}{hint}")
                    else:
                        val = f"{Fore.GREEN}yes{Style.RESET_ALL}" if r["val"] else f"{Style.DIM}no{Style.RESET_ALL}"
                        hl = f"{Fore.YELLOW}" if cur else ""
                        arrows = f"  {Style.DIM}<- ->{Style.RESET_ALL}" if cur else ""
                        lines.append(f"{mark} {hl}{r['label']:9}:{Style.RESET_ALL} {val}{arrows}")
                    if i == len(fields) - 1:
                        lines.append(f"  {Style.DIM}{'-' * 24}{Style.RESET_ALL}")
                lines += ["", f"{Style.DIM}\u2191\u2193 move | type to edit text | <-/->/Space toggle | "
                          f"Enter = confirm/apply | Esc = cancel{Style.RESET_ALL}"]
                _fb_write_frame(lines)
                k = _read_key()
                r = rows[pos]
                if k == "up":
                    pos = (pos - 1) % len(rows)
                elif k == "down":
                    pos = (pos + 1) % len(rows)
                elif k == "esc":
                    return None
                elif "action" in r:
                    if k in ("enter", "right"):
                        if r["action"] == "cancel":
                            return None
                        changes = {}
                        for f in fields:
                            if f["val"] != orig[f["key"]]:
                                changes[f["key"]] = f["val"]
                        return changes
                elif r["type"] == "bool":
                    if k in ("left", "right") or k == ("char", " "):
                        r["val"] = not r["val"]
                    elif k == "enter":
                        pos = (pos + 1) % len(rows)
                elif r["type"] == "text":
                    if k == "backspace":
                        r["val"] = r["val"][:-1]
                    elif k == "enter":
                        if r["key"] == "lang":
                            code = _vid_pick_language(r["val"])
                            if code is not None:
                                r["val"] = "" if code == "und" else code
                            _fb_enter_screen()   # re-hide cursor after the nested picker
                        else:
                            pos = (pos + 1) % len(rows)
                    elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
                        r["val"] += k[1]
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _vid_apply_track_changes(args, info, kind, t, changes):
    """Applies one track's changes (name/lang/default/forced). MKV -> mkvpropedit in
    place; MP4/other -> ffmpeg remux. Returns True on success."""
    if not changes:
        return False
    p = info["path"]
    if "lang" in changes:
        changes = dict(changes)
        changes["lang"] = _canon3(changes["lang"]) or "und"
    if info["is_mkv"] and info["mkvpropedit"] and t.get("sel"):
        edits = []
        if "name" in changes:
            edits += ["--edit", f"track:{t['sel']}", "--set", f"name={changes['name']}"] if changes["name"] \
                else ["--edit", f"track:{t['sel']}", "--delete", "name"]
        if "lang" in changes:
            edits += ["--edit", f"track:{t['sel']}", "--set", f"language={changes['lang']}"]
        if "default" in changes:
            edits += ["--edit", f"track:{t['sel']}", "--set", f"flag-default={1 if changes['default'] else 0}"]
            if changes["default"]:
                # exactly one default per type: clear default on the other tracks of this kind
                siblings = info["audio"] if kind == "audio" else info["subs"]
                for o in siblings:
                    if o.get("sel") and o["sel"] != t["sel"] and o.get("default"):
                        edits += ["--edit", f"track:{o['sel']}", "--set", "flag-default=0"]
        if "forced" in changes:
            edits += ["--edit", f"track:{t['sel']}", "--set", f"flag-forced={1 if changes['forced'] else 0}"]
        r = subprocess.run([info["mkvpropedit"], str(p)] + edits, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode >= 2:
            log_warn(f"mkvpropedit error: {r.stderr.strip()[:200]}")
            return False
        log_done(f"{('A' if kind == 'audio' else 'S')}{t['ord'] + 1}: updated {', '.join(changes)}.")
        return True
    # MP4 / other -> ffmpeg remux
    ff = info["ffmpeg"]
    tmp = p.with_name(p.stem + ".edittmp" + p.suffix)
    spec = f"a:{t['ord']}" if kind == "audio" else f"s:{t['ord']}"
    letter = "a" if kind == "audio" else "s"
    meta = []
    if "name" in changes:
        meta += [f"-metadata:s:{spec}", f"title={changes['name']}"]
    if "lang" in changes:
        meta += [f"-metadata:s:{spec}", f"language={changes['lang']}"]
    disp = []
    if changes.get("default") is True:
        # one default per type: set this track default, clear it on the others (keep forced)
        for o in (info["audio"] if kind == "audio" else info["subs"]):
            fl = []
            if o["ord"] == t["ord"]:
                fl.append("default")
                if changes.get("forced", o["forced"]):
                    fl.append("forced")
            elif o["forced"]:
                fl.append("forced")
            disp += [f"-disposition:{letter}:{o['ord']}", "+".join(fl) if fl else "0"]
    elif "default" in changes or "forced" in changes:
        fl = []
        if changes.get("default", t["default"]):
            fl.append("default")
        if changes.get("forced", t["forced"]):
            fl.append("forced")
        disp += [f"-disposition:{letter}:{t['ord']}", "+".join(fl) if fl else "0"]
    cmd = [ff, "-y", "-i", str(p), "-map", "0", "-c", "copy"] + meta + disp + [str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not tmp.exists():
        log_warn("ffmpeg remux failed.")
        tmp.exists() and os.remove(str(tmp))
        return False
    if _vid_replace(tmp, p):
        log_done(f"{('A' if kind == 'audio' else 'S')}{t['ord'] + 1}: updated {', '.join(changes)} (remux).")
        return True
    return False


def _vid_edit_track(args, info):
    """Track editor: a list of tracks; Enter opens a per-track form (Name / Language
    / Default / Forced) navigated with the arrows, with Confirm/Cancel. Changes are
    applied immediately per track; the list refreshes so you can edit several."""
    path = str(info["path"])
    if not (info["audio"] or info["subs"]):
        log_warn("This video has no audio/subtitle tracks to edit.")
        return None
    if not _tui_supported():
        log_warn("The track editor needs a real terminal.")
        return None
    changed_any = False
    sel = 0
    while True:
        info = _vid_probe(args, path)
        tracks = [("audio", t) for t in info["audio"]] + [("subs", t) for t in info["subs"]]
        if not tracks:
            break
        idx = _vid_track_list_screen(info, tracks, sel)
        if idx is None:
            break
        sel = idx
        kind, t = tracks[idx]
        changes = _vid_track_form(kind, t)
        if changes:
            if _vid_apply_track_changes(args, info, kind, t, changes):
                changed_any = True
        # loop back to the (refreshed) track list
    # leave immediately (the refreshed report will reflect any changes) - no extra pause
    raise WizardBack()


def _vid_set_default(args, info):
    """Quick set of the default audio + subtitle track by picking one of each."""
    def _pick(kind, label):
        tracks = info[kind]
        if not tracks:
            return "keep"
        labels = [f"{_lang3_name(t['lang'])} {t['codec']}" + (f'  "{t["name"]}"' if t.get("name") else "")
                  for t in tracks]
        labels += ["keep as is", "none (clear default)"]
        i = ask_pick(f"Default {label}", labels, default=len(tracks))
        if i < len(tracks):
            return tracks[i]["ord"]
        return "keep" if i == len(tracks) else "none"
    a = _pick("audio", "audio track")
    s = _pick("subs", "subtitle track")
    if a == "keep" and s == "keep":
        return None
    p = info["path"]
    if info["is_mkv"] and info["mkvpropedit"]:
        edits = []
        if a != "keep":
            for t in info["audio"]:
                val = 1 if (a != "none" and t["ord"] == a) else 0
                edits += ["--edit", f"track:{t['sel']}", "--set", f"flag-default={val}"]
        if s != "keep":
            for t in info["subs"]:
                val = 1 if (s != "none" and t["ord"] == s) else 0
                edits += ["--edit", f"track:{t['sel']}", "--set", f"flag-default={val}"]
        r = subprocess.run([info["mkvpropedit"], str(p)] + edits, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode >= 2:
            log_warn("mkvpropedit error.")
            return None
        log_done("Default track(s) set in place.")
        return info["path"]
    else:
        ff = info["ffmpeg"]
        tmp = p.with_name(p.stem + ".deftmp" + p.suffix)
        disp = []
        for choice, letter, tracks in ((a, "a", info["audio"]), (s, "s", info["subs"])):
            if choice == "keep":
                continue
            for t in tracks:
                fl = []
                if choice != "none" and t["ord"] == choice:
                    fl.append("default")
                if t["forced"]:
                    fl.append("forced")
                disp += [f"-disposition:{letter}:{t['ord']}", "+".join(fl) if fl else "0"]
        cmd = [ff, "-y", "-i", str(p), "-map", "0", "-c", "copy"] + disp + [str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not tmp.exists():
            log_warn("ffmpeg failed.")
            tmp.exists() and os.remove(str(tmp))
            return None
        if _vid_replace(tmp, p):
            log_done("Default track(s) set (remux).")
            return info["path"]
    return None


def _vid_add_audio(args, info):
    cands = _vid_dir_files(info, _AUDIO_FILE_EXTS)
    if not cands:
        log_warn("No external audio files (.ac3/.aac/.dts/...) in this folder.")
        return None
    i = ask_pick("Pick the external audio file to add:", [os.path.basename(c) for c in cands],
                 default=0, allow_back=True)
    if i is None:
        raise WizardBack()
    apath = cands[i]
    lang = _canon3(ask_language("Language of the added audio (code)", "eng") or "eng")
    make_default = ask_yes_no("Make the added audio the DEFAULT track?", default_no=False)
    p = info["path"]
    out = p.with_name(p.stem + ".mkv") if not info["is_mkv"] else p
    tmp = p.with_name(p.stem + ".addtmp.mkv")
    ff = info["ffmpeg"]
    cmd = [ff, "-y", "-i", str(p), "-i", str(apath), "-map", "0", "-map", "1:a",
           "-c", "copy", f"-metadata:s:a:{len(info['audio'])}", f"language={lang}"]
    if make_default:
        cmd += ["-disposition:a", "0", f"-disposition:a:{len(info['audio'])}", "default"]
    # convert text subs when muxing into mkv from mp4 to be safe
    cmd += ["-c:s", "copy" if info["is_mkv"] else "srt", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not tmp.exists():
        log_warn("ffmpeg mux failed.")
        tmp.exists() and os.remove(str(tmp))
        return None
    if not info["is_mkv"]:
        os.remove(str(p))
    if _vid_replace(tmp, out):
        log_done(f"Added audio '{os.path.basename(apath)}' -> {out.name}")
        return out
    return None


def _vid_add_subs(args, info):
    cands = _vid_dir_files(info, _SUB_FILE_EXTS)
    if not cands:
        log_warn("No subtitle files (.srt/.ass/...) in this folder.")
        return None
    i = ask_pick("Pick the subtitle file to add:", [os.path.basename(c) for c in cands],
                 default=0, allow_back=True)
    if i is None:
        raise WizardBack()
    spath = cands[i]
    lang = _canon3(ask_language("Subtitle language (code)", "eng") or "eng")
    name = ask_text("Track name (Enter = language name)", "") or _lang3_name(lang)
    forced = ask_yes_no("Mark as forced?", default_no=True)
    default = ask_yes_no("Mark as default subtitle?", default_no=True)
    p = info["path"]
    mm = info["mkvmerge"]
    if not mm:
        log_warn("mkvmerge (MKVToolNix) is required to mux subtitles.")
        return None
    out = p.with_name(p.stem + ".mkv")
    tmp = p.with_name(p.stem + ".subtmp.mkv")
    cmd = [mm, "-o", str(tmp), str(p),
           "--language", f"0:{lang}", "--track-name", f"0:{name}",
           "--forced-track", "0:yes" if forced else "0:no",
           "--default-track", "0:yes" if default else "0:no", str(spath)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode >= 2 or not tmp.exists():
        log_warn("mkvmerge mux failed.")
        tmp.exists() and os.remove(str(tmp))
        return None
    if info["ext"] != ".mkv":
        os.remove(str(p))
    if _vid_replace(tmp, out):
        log_done(f"Added subtitles '{os.path.basename(spath)}' -> {out.name}")
        return out
    return None


def _vid_convert_audio(args, info):
    if not info["audio"]:
        log_warn("No audio to convert.")
        return None
    codecs = [("ac3", "AC-3 (Dolby Digital) - wide compatibility"),
              ("eac3", "E-AC-3"), ("aac", "AAC")]
    ci = ask_pick("Convert ALL audio to which codec?", [f"{c} - {d}" for c, d in codecs], default=0)
    codec = codecs[ci][0]
    br = (ask_text("Bitrate", "640k") or "640k").strip()
    p = info["path"]
    in_place = not _vid_ask_out_mode()
    out = p if in_place else p.with_name(p.stem + f"_{codec}" + p.suffix)
    tmp = p.with_name(p.stem + f".convtmp{p.suffix}")
    ff = info["ffmpeg"]
    subflag = ["-c:s", "copy"] if info["is_mkv"] else ["-c:s", "mov_text"]
    cmd = [ff, "-y", "-i", str(p), "-map", "0", "-c:v", "copy",
           "-c:a", codec, "-b:a", br] + subflag + [str(tmp)]
    r = subprocess.run(cmd)
    if r.returncode != 0 or not tmp.exists():
        log_warn("Conversion failed.")
        tmp.exists() and os.remove(str(tmp))
        return None
    if _vid_replace(tmp, out):
        log_done(f"Audio converted to {codec} @ {br} -> {out.name}")
        return out if in_place else info["path"]
    return None


def _vid_convert_container(args, info):
    p = info["path"]
    ff = info["ffmpeg"]
    to_mkv = not info["is_mkv"]
    target_ext = ".mkv" if to_mkv else ".mp4"
    out = p.with_suffix(target_ext)
    if out.exists() and out != p:
        if not ask_yes_no(f"{out.name} already exists - overwrite?", default_no=True):
            return None
    tmp = p.with_name(p.stem + ".convtmp" + target_ext)
    if to_mkv:
        # everything copies cleanly into MKV
        cmd = [ff, "-y", "-i", str(p), "-map", "0", "-c", "copy", str(tmp)]
    else:
        # MKV -> MP4: text subs must become mov_text; image subs (PGS/VobSub) dropped
        has_img = any(not s.get("text") for s in info["subs"])
        cmd = [ff, "-y", "-i", str(p), "-map", "0:v", "-map", "0:a?"]
        if info["subs"]:
            if has_img:
                log_info("MP4 can't hold image subtitles (PGS/VobSub) - those will be dropped.")
                for s in info["subs"]:
                    if s.get("text"):
                        cmd += ["-map", f"0:s:{s['ord']}"]
            else:
                cmd += ["-map", "0:s?"]
        cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text", str(tmp)]
    log_info(f"Remuxing {info['container']} -> {target_ext.lstrip('.').upper()} (no re-encode)...")
    r = subprocess.run(cmd)
    if r.returncode != 0 or not tmp.exists():
        log_warn("Container conversion failed.")
        tmp.exists() and os.remove(str(tmp))
        return None
    keep_src = (out != p) and ask_yes_no(f"Keep the original {info['container']} file too?", default_no=True)
    if not _vid_replace(tmp, out):
        return None
    if p.exists() and p != out and not keep_src:
        try:
            os.remove(str(p))
        except OSError:
            pass
    log_done(f"Converted -> {out.name}")
    return out


def _vid_extract_chapters(args, info):
    if not info["is_mkv"] or not info["mkvextract"]:
        log_warn("Chapter extraction is available for MKV (mkvextract).")
        return None
    if not info["chapters"]:
        log_warn("This file has no chapters.")
        return None
    p = info["path"]
    out = p.with_name(p.stem + ".chapters.xml")
    r = subprocess.run([info["mkvextract"], str(p), "chapters", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode >= 2 or not out.exists():
        log_warn("Chapter extraction failed.")
        return None
    log_done(f"Chapters -> {out.name}")
    return None


def _vid_extract_attachments(args, info):
    if not info["is_mkv"] or not info["mkvextract"] or not info["attachments"]:
        log_warn("No attachments to extract (MKV only).")
        return None
    p = info["path"]
    outdir = p.with_name(p.stem + "_attachments")
    os.makedirs(outdir, exist_ok=True)
    spec = [f"{a['id']}:{outdir / a['name']}" for a in info["attachments"] if a.get("id") is not None]
    r = subprocess.run([info["mkvextract"], str(p), "attachments"] + spec, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode >= 2:
        log_warn("Attachment extraction failed.")
        return None
    log_done(f"Extracted {len(spec)} attachment(s) -> {outdir.name}/")
    return None


def _vid_thumbnail(args, info):
    p = info["path"]
    ts = ask_text("Timestamp for the thumbnail (HH:MM:SS or seconds)", "00:00:30").strip() or "00:00:30"
    out = p.with_name(p.stem + ".thumb.jpg")
    r = subprocess.run([info["ffmpeg"], "-y", "-ss", ts, "-i", str(p), "-frames:v", "1",
                        "-q:v", "2", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists():
        log_warn("Thumbnail extraction failed (timestamp beyond the end?).")
        return None
    log_done(f"Thumbnail at {ts} -> {out.name}")
    return None


def _vid_extract_stream_only(args, info):
    which = ask_pick("Extract a clean stream into its own file:",
                     ["video only (no audio/subs)", "all audio (video+audio, no subs)"], default=0)
    p = info["path"]
    ff = info["ffmpeg"]
    if which == 0:
        out = p.with_name(p.stem + ".video" + p.suffix)
        cmd = [ff, "-y", "-i", str(p), "-map", "0:v", "-c", "copy", "-an", "-sn", str(out)]
    else:
        out = p.with_name(p.stem + ".noSubs" + p.suffix)
        cmd = [ff, "-y", "-i", str(p), "-map", "0:v", "-map", "0:a?", "-c", "copy", "-sn", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists():
        log_warn("Extraction failed.")
        return None
    log_done(f"-> {out.name}")
    return None


def _vid_scroll_view(title, lines):
    """Full-screen scrollable text viewer (Up/Down/PgUp/PgDn/Home/End, Esc/q close).
    Falls back to a plain print when there is no TTY."""
    if not _tui_supported():
        for ln in lines:
            print(ln)
        return
    top = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                cols, rows = _fb_termsize()
                head = [f"{Fore.MAGENTA}{Style.BRIGHT}=== {_fb_trunc(title, cols - 8)} ==={Style.RESET_ALL}", ""]
                foot = ["", f"{Style.DIM}\u2191\u2193 / PgUp / PgDn / Home / End scroll | Esc = close{Style.RESET_ALL}"]
                avail = max(3, rows - len(head) - len(foot))
                top = max(0, min(top, max(0, len(lines) - avail)))
                body = []
                for ln in lines[top:top + avail]:
                    # truncate plain lines to width; leave short (colored header) lines as-is
                    body.append(ln if ("\x1b[" in ln or len(ln) <= cols) else _fb_trunc(ln, cols - 1))
                while len(body) < avail:
                    body.append("")
                pos = f"[{min(top + avail, len(lines))}/{len(lines)}]"
                head[0] = head[0] + f"  {Style.DIM}{pos}{Style.RESET_ALL}"
                _fb_write_frame(head + body + foot)
                k = _read_key()
                if k == "esc" or k == ("char", "q"):
                    return
                elif k == "down":
                    top += 1
                elif k == "up":
                    top = max(0, top - 1)
                elif k == "pgdn":
                    top += avail
                elif k == "pgup":
                    top = max(0, top - avail)
                elif k == "home":
                    top = 0
                elif k == "end":
                    top = len(lines)
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _vid_advanced_lines(info, data):
    """Builds a VLC-style, per-stream technical breakdown from full ffprobe output."""
    L = []
    fmt = data.get("format", {}) or {}
    L.append(f"{Fore.CYAN}File:{Style.RESET_ALL} {info['path'].name}")
    L.append(f"{Fore.CYAN}Container:{Style.RESET_ALL} {fmt.get('format_long_name') or info['container']}")
    L.append(f"Size: {_vid_human_size(fmt.get('size'))}   "
             f"Duration: {_vid_human_dur(fmt.get('duration'))}   "
             f"Overall bitrate: {_vid_bitrate(fmt.get('bit_rate')) or '?'}")
    if fmt.get("nb_streams"):
        L.append(f"Streams: {fmt.get('nb_streams')}   Chapters: {info.get('chapters', 0)}   "
                 f"Attachments: {len(info.get('attachments', []))}")
    L.append("")

    def add(label, val):
        if val not in (None, "", "N/A", "0/0"):
            L.append(f"    {label}: {val}")

    for s in data.get("streams", []):
        t = s.get("codec_type", "?")
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        typ = {"video": "Video", "audio": "Audio", "subtitle": "Subtitle",
               "data": "Data", "attachment": "Attachment"}.get(t, t.title())
        extra = []
        if tags.get("language"):
            extra.append(_lang3_name(_canon3(tags.get("language"))))
        if tags.get("title"):
            extra.append(f'"{tags["title"]}"')
        hdr = f"Stream {s.get('index')} - {typ}" + (("  (" + ", ".join(extra) + ")") if extra else "")
        L.append(f"{Fore.GREEN}{Style.BRIGHT}{hdr}{Style.RESET_ALL}")

        codec = s.get("codec_long_name") or s.get("codec_name", "?")
        if s.get("codec_name") and s.get("codec_name") not in codec.lower():
            codec += f" ({s['codec_name']})"
        tag = s.get("codec_tag_string")
        if tag and tag not in ("[0][0][0][0]", ""):
            codec += f"  [tag: {tag}]"
        add("Codec", codec)
        if s.get("profile"):
            add("Profile", str(s["profile"]) + (f" @ L{s['level']}" if s.get("level") not in (None, -99) else ""))
        if s.get("bit_rate"):
            add("Bitrate", f"{int(s['bit_rate']) // 1000} kb/s")

        if t == "video":
            add("Resolution", f"{s.get('width')}x{s.get('height')}")
            if s.get("coded_width") and (s.get("coded_width") != s.get("width")
                                         or s.get("coded_height") != s.get("height")):
                add("Coded size", f"{s.get('coded_width')}x{s.get('coded_height')}")
            add("Display aspect ratio", s.get("display_aspect_ratio"))
            add("Sample aspect ratio", s.get("sample_aspect_ratio"))
            fps = _vid_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or "0")
            if fps:
                add("Frame rate", f"{fps:.3f} fps")
            add("Pixel format", s.get("pix_fmt"))
            add("Bit depth", s.get("bits_per_raw_sample"))
            color = []
            for key, lab in (("color_primaries", "primaries"), ("color_transfer", "transfer"),
                             ("color_space", "space"), ("color_range", "range")):
                if s.get(key):
                    color.append(f"{lab} {s[key]}")
            if color:
                add("Color", " | ".join(color))
            add("Field order", s.get("field_order"))
            add("Frames", s.get("nb_frames"))
        elif t == "audio":
            if s.get("sample_rate"):
                add("Sample rate", f"{s['sample_rate']} Hz")
            if s.get("channels"):
                add("Channels", f"{s['channels']}" + (f" ({s['channel_layout']})" if s.get("channel_layout") else ""))
            add("Sample format", s.get("sample_fmt"))
            add("Bits per sample", s.get("bits_per_sample") or s.get("bits_per_raw_sample") or None)

        flags = [k for k in ("default", "forced", "hearing_impaired", "visual_impaired",
                             "comment", "attached_pic") if disp.get(k)]
        if flags:
            add("Flags", ", ".join(flags))
        if tags.get("handler_name"):
            add("Handler", tags.get("handler_name"))
        L.append("")
    return L


def _vid_advanced_info(args, info):
    """Shows a detailed, VLC-style technical breakdown of every stream (ffprobe)."""
    ffprobe = info.get("ffprobe") or _find_ffprobe(info.get("ffmpeg"))
    if not ffprobe:
        log_warn("ffprobe not found (needed for advanced info). Install ffmpeg (includes ffprobe).")
        return None
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                              "-print_format", "json", str(info["path"])], capture_output=True, text=True, encoding="utf-8", errors="replace")
        data = json.loads(out.stdout or "{}")
    except Exception as e:
        log_warn(f"ffprobe failed: {e}")
        return None
    _vid_scroll_view(f"Advanced info - {info['path'].name}", _vid_advanced_lines(info, data))
    raise WizardBack()   # pure viewer -> straight back to the menu, no extra pause


def _video_detail_menu(args, path):
    """Inspector for a single video: shows a full report and offers every operation.
    Loops until the user goes Back. Returns when done (path may have changed)."""
    path = str(path)
    sel = 0
    while True:
        try:
            info = _vid_probe(args, path)
        except Exception as e:
            log_warn(f"Could not read the video: {e}")
            try:
                input("Enter to go back...")
            except (EOFError, KeyboardInterrupt):
                pass
            return
        # build the action list (only what makes sense for this container)
        actions = []  # (label, help, fn)
        actions.append(("Advanced info (ffprobe / VLC-style)", "Full technical per-stream breakdown, like VLC's Codec tab (scrollable).", _vid_advanced_info))
        actions.append(("Extract audio track(s) -> file", "Stream-copy chosen audio tracks to standalone files.", _vid_extract_audio))
        actions.append(("Extract subtitle track(s) -> .srt", "Extract chosen text subtitle tracks to .srt.", _vid_extract_subs))
        actions.append(("Edit track metadata (name / language / default / forced)", "Rename/retag tracks and set flags.", _vid_edit_track))
        actions.append(("Set DEFAULT audio / subtitle track", "Pick which audio and subtitle become default.", _vid_set_default))
        actions.append(("Remove (delete) track(s)", "Drop chosen audio/subtitle tracks (fast remux).", _vid_remove_tracks))
        actions.append(("Add external AUDIO into the video", "Mux an audio file from this folder as a new track.", _vid_add_audio))
        actions.append(("Add external SUBTITLES into the video", "Mux a .srt/.ass from this folder (MKV output).", _vid_add_subs))
        actions.append(("Convert audio codec (AC-3 / E-AC-3 / AAC)", "Re-encode all audio, copy video+subs.", _vid_convert_audio))
        actions.append((f"Convert container -> {'MKV' if not info['is_mkv'] else 'MP4'}", "Remux to the other container (no re-encode).", _vid_convert_container))
        if info["is_mkv"] and info["chapters"]:
            actions.append(("Extract chapters -> .xml", "Save chapters as an XML file (mkvextract).", _vid_extract_chapters))
        if info["is_mkv"] and info["attachments"]:
            actions.append(("Extract attachments (fonts/images)", "Save attached files into a subfolder.", _vid_extract_attachments))
        actions.append(("Save a thumbnail (JPG)", "Grab a single frame at a chosen timestamp.", _vid_thumbnail))
        actions.append(("Extract clean stream (video-only / no-subs)", "Write a copy with only some stream kinds.", _vid_extract_stream_only))

        header = _vid_report_lines(info)
        header.append(f"{Fore.CYAN}Pick an operation (Esc = back to the browser):{Style.RESET_ALL}")
        idx = ask_pick("Action for this video:", [a[0] for a in actions],
                       default=0, allow_back=True, header=header, help=[a[1] for a in actions],
                       cursor=sel)
        if idx is None:
            return
        sel = idx    # remember where we were, so we come back to the same item
        fn = actions[idx][2]
        try:
            newp = fn(args, info)
        except (WizardBack, KeyboardInterrupt):
            continue    # cancelled (Esc / cancel choice) -> straight back, no warning/pause
        except SystemExit:
            newp = None    # die() inside the op: its message was already printed
        except Exception as e:
            log_warn(f"Operation failed: {e}")
            newp = None
        if newp:
            path = str(newp)
        try:
            input(f"\n{Fore.CYAN}Enter to return to the video's menu...{Style.RESET_ALL}")
        except (EOFError, KeyboardInterrupt):
            return


def _video_browser_loop(state, args):
    """Total Commander style browser limited to a view where Enter on a VIDEO opens
    the inspector. Navigates dirs/drives, live filter. Returns ('inspect', path),
    ('quit', None). Updates state (cwd/cursor/top/filt) in place."""
    filter_editing = False
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                dirs, files = _fb_list(state["cwd"], state["filt"])
                vids = [f for f in files if Path(f).suffix.lower() in VIDEO_EXTS_BATCH]
                others = [f for f in files if Path(f).suffix.lower() not in VIDEO_EXTS_BATCH]
                rows_items = [("..", "up")] + [(d, "dir") for d in dirs] \
                    + [(f, "video") for f in vids] + [(f, "file") for f in others]
                n = len(rows_items)
                state["cursor"] = max(0, min(state["cursor"], n - 1))
                cols, rows = _fb_termsize()
                head = [
                    f"{Fore.MAGENTA}{Style.BRIGHT}=== Video browser / inspector ==={Style.RESET_ALL}",
                    f"{Fore.CYAN}Path:{Style.RESET_ALL} {_fb_trunc(state['cwd'], cols - 8)}",
                ]
                if filter_editing:
                    head.append(f"{Fore.YELLOW}Filter (typing):{Style.RESET_ALL} {state['filt']}_   "
                                f"{Style.DIM}Enter=keep Esc=clear{Style.RESET_ALL}")
                else:
                    head.append(f"{Fore.CYAN}Filter:{Style.RESET_ALL} "
                                + (state["filt"] if state["filt"] else f"{Style.DIM}(none){Style.RESET_ALL}")
                                + f"    {Fore.CYAN}Videos here:{Style.RESET_ALL} {len(vids)}")
                head.append("")
                foot = ["",
                        (f"{Style.DIM}\u2191\u2193 move | Enter/\u2192 open (dir or INSPECT video) | \u2190/Bksp up | "
                         f"/ filter | d drive | q quit{Style.RESET_ALL}")]
                body_rows = max(4, rows - len(head) - len(foot))
                if state["cursor"] < state["top"]:
                    state["top"] = state["cursor"]
                elif state["cursor"] >= state["top"] + body_rows:
                    state["top"] = state["cursor"] - body_rows + 1
                state["top"] = max(0, state["top"])
                body = []
                view = rows_items[state["top"]:state["top"] + body_rows]
                for idx, (name, kind) in enumerate(view, start=state["top"]):
                    cur = (idx == state["cursor"])
                    if kind == "up":
                        disp, col = ".. (up one level)", Fore.CYAN
                    elif kind == "dir":
                        disp, col = "[" + name + "]", Fore.CYAN + Style.BRIGHT
                    elif kind == "video":
                        disp, col = name, Fore.WHITE + Style.BRIGHT
                    else:
                        disp, col = "  " + name, Style.DIM
                    disp = _fb_trunc(disp, cols - 4)
                    if cur:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {disp}{Style.RESET_ALL}")
                    else:
                        body.append(f"  {col}{disp}{Style.RESET_ALL}")
                rest = n - (state["top"] + len(view))
                if rest > 0:
                    body.append(f"  {Fore.CYAN}v ({rest} more){Style.RESET_ALL}")
                _fb_write_frame(head + body + foot)
                k = _read_key()

                if filter_editing:
                    if k == "enter":
                        filter_editing = False
                    elif k == "esc":
                        filter_editing = False
                        state["filt"] = ""
                        state["cursor"] = 0
                    elif k == "backspace":
                        state["filt"] = state["filt"][:-1]
                        state["cursor"] = 0
                    elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
                        state["filt"] += k[1]
                        state["cursor"] = 0
                    continue

                if k == "up":
                    state["cursor"] = (state["cursor"] - 1) % n
                elif k == "down":
                    state["cursor"] = (state["cursor"] + 1) % n
                elif k == "pgup":
                    state["cursor"] = max(0, state["cursor"] - body_rows)
                elif k == "pgdn":
                    state["cursor"] = min(n - 1, state["cursor"] + body_rows)
                elif k == "home":
                    state["cursor"] = 0
                elif k == "end":
                    state["cursor"] = n - 1
                elif k in ("enter", "right"):
                    name, kind = rows_items[state["cursor"]]
                    if kind == "up":
                        parent = os.path.dirname(state["cwd"].rstrip(os.sep)) or state["cwd"]
                        if os.path.isdir(parent):
                            state.update(cwd=os.path.abspath(parent), cursor=0, top=0, filt="")
                    elif kind == "dir":
                        target = os.path.join(state["cwd"], name)
                        if os.path.isdir(target):
                            state.update(cwd=os.path.abspath(target), cursor=0, top=0, filt="")
                    elif kind == "video":
                        return ("inspect", os.path.join(state["cwd"], name))
                    # plain files: ignore
                elif k in ("left", "backspace"):
                    parent = os.path.dirname(state["cwd"].rstrip(os.sep)) or state["cwd"]
                    if os.path.isdir(parent):
                        state.update(cwd=os.path.abspath(parent), cursor=0, top=0, filt="")
                elif isinstance(k, tuple) and k[0] == "char":
                    ch = k[1]
                    if ch == "/":
                        filter_editing = True
                    elif ch in ("d", "D"):
                        dest = _fb_pick_location(state["cwd"])
                        if dest:
                            state.update(cwd=dest, cursor=0, top=0, filt="")
                    elif ch in ("q", "Q"):
                        return ("quit", None)
                    elif ch in ("?", "h"):
                        _fb_help_overlay()
                elif k == "esc":
                    if state["filt"]:
                        state["filt"] = ""
                        state["cursor"] = 0
                    else:
                        return ("quit", None)
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def run_video_browser(args):
    """Total Commander style video browser: navigate folders/drives, and press Enter
    on a video to open a full inspector with every operation the tool can do on it
    (extract/import/rename/remove tracks, set defaults, convert audio, MP4<->MKV,
    chapters, attachments, thumbnails...). Cross-platform (Windows + Linux)."""
    start = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(start):
        start = os.path.dirname(start) or "."
    if not _tui_supported():
        log_warn("The interactive video browser needs a real terminal (a TTY).")
        log_info("Run it directly on your machine's console, not through a pipe/redirect.")
        return
    state = {"cwd": os.path.abspath(start), "cursor": 0, "top": 0, "filt": ""}
    while True:
        result, payload = _video_browser_loop(state, args)
        if result == "quit":
            return
        if result == "inspect":
            _video_detail_menu(args, payload)


# ============================================================================
# Online rename via TMDB (recognize shows/movies) + Plex-style naming
# ----------------------------------------------------------------------------
# Uses The Movie Database (TMDB). Data provided by TMDB - this product uses the
# TMDB API but is not endorsed or certified by TMDB. A free API key is needed
# (themoviedb.org -> Settings -> API); store it via --config. Works with either
# a v3 API key or a v4 Read Access Token (Bearer).
# ============================================================================

_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_CACHE = {}

_TMDB_LANG_MAP = {
    "cs": "cs-CZ", "sk": "sk-SK", "en": "en-US", "de": "de-DE", "pl": "pl-PL",
    "es": "es-ES", "fr": "fr-FR", "it": "it-IT", "ru": "ru-RU", "hu": "hu-HU",
    "pt": "pt-PT", "nl": "nl-NL", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN",
}

# SxxExx (+ optional -Exx range), and the 1x02 style
_MN_SE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?:[ ._-]?[Ee](\d{1,3}))?")
_MN_SE_X = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
_MN_YEAR = re.compile(r"[\(\.\[\s_](19\d{2}|20\d{2})[\)\.\]\s_]")
_MN_YEAR_END = re.compile(r"(19\d{2}|20\d{2})\s*$")


def _tmdb_auth(args):
    key = getattr(args, "tmdb_key", None) or os.environ.get("TMDB_API_KEY")
    bearer = getattr(args, "tmdb_bearer", None) or os.environ.get("TMDB_BEARER")
    return key, bearer


def _iso3_to_iso2(code3):
    code3 = (code3 or "").lower()
    for c2 in LANGUAGE_NAMES:
        if _canon3(c2) == code3:
            return c2
    return None


def _tmdb_lang(args):
    ml = (getattr(args, "meta_lang", None) or "cs").replace("_", "-").strip()
    if "-" in ml:
        return ml
    low = ml.lower()
    if low in _TMDB_LANG_MAP:
        return _TMDB_LANG_MAP[low]
    two = _iso3_to_iso2(low)          # accept 3-letter codes (e.g. from the language picker)
    if two:
        return _TMDB_LANG_MAP.get(two, two)
    return ml


def _tmdb_request(args, path, params=None):
    """Low-level TMDB GET. Raises on network/HTTP errors (caller handles)."""
    import urllib.request
    import urllib.parse
    key, bearer = _tmdb_auth(args)
    q = dict(params or {})
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif key:
        q["api_key"] = key
    else:
        raise RuntimeError("No TMDB key/token configured (run --config).")
    url = _TMDB_BASE + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _tmdb_cached(args, path, params=None):
    import urllib.parse
    keyid = path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))
    if keyid in _TMDB_CACHE:
        return _TMDB_CACHE[keyid]
    data = _tmdb_request(args, path, params)
    _TMDB_CACHE[keyid] = data
    return data


def _tmdb_error_hint(e):
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "TMDB rejected the key (HTTP 401) - check the API key/token in --config."
        if e.code == 429:
            return "TMDB rate limit (HTTP 429) - wait a moment and try again."
        return f"TMDB HTTP {e.code}."
    return f"TMDB network error: {e}"


def _tmdb_search_tv(args, query, year=None):
    p = {"query": query, "language": _tmdb_lang(args), "include_adult": "false"}
    if year:
        p["first_air_date_year"] = year
    data = _tmdb_cached(args, "/search/tv", p)
    out = []
    for r in data.get("results", [])[:12]:
        out.append({"id": r["id"], "kind": "tv",
                    "title": r.get("name") or r.get("original_name") or "",
                    "orig": r.get("original_name") or "",
                    "year": (r.get("first_air_date") or "")[:4],
                    "overview": r.get("overview") or ""})
    return out


def _tmdb_search_movie(args, query, year=None):
    p = {"query": query, "language": _tmdb_lang(args), "include_adult": "false"}
    if year:
        p["year"] = year
    data = _tmdb_cached(args, "/search/movie", p)
    out = []
    for r in data.get("results", [])[:12]:
        out.append({"id": r["id"], "kind": "movie",
                    "title": r.get("title") or r.get("original_title") or "",
                    "orig": r.get("original_title") or "",
                    "year": (r.get("release_date") or "")[:4],
                    "overview": r.get("overview") or ""})
    return out


def _tmdb_season_titles(args, tv_id, season):
    """Returns {episode_number: title}. Falls back to English titles when the
    localized ones are missing."""
    try:
        data = _tmdb_cached(args, f"/tv/{tv_id}/season/{season}", {"language": _tmdb_lang(args)})
    except Exception:
        return {}
    eps = {}
    missing = False
    for e in data.get("episodes", []):
        num = e.get("episode_number")
        name = (e.get("name") or "").strip()
        if num is not None:
            eps[int(num)] = name
            if not name:
                missing = True
    if missing and not _tmdb_lang(args).startswith("en"):
        try:
            en = _tmdb_cached(args, f"/tv/{tv_id}/season/{season}", {"language": "en-US"})
            for e in en.get("episodes", []):
                num = e.get("episode_number")
                if num is not None and not eps.get(int(num)):
                    eps[int(num)] = (e.get("name") or "").strip()
        except Exception:
            pass
    return eps


def _tmdb_external_ids(args, kind, tid):
    try:
        d = _tmdb_cached(args, f"/{kind}/{tid}/external_ids", {})
        return d.get("imdb_id") or None
    except Exception:
        return None


def _parse_media_name(name):
    """Guesses title / year / season / episode(s) from a file name. is_tv is True
    when an SxxExx / NxNN pattern is found."""
    stem = os.path.splitext(name)[0]
    s = e = e2 = None
    m = _MN_SE.search(stem)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        e2 = int(m.group(3)) if m.group(3) else None
        title_part = stem[:m.start()]
    else:
        m2 = _MN_SE_X.search(stem)
        if m2:
            s, e = int(m2.group(1)), int(m2.group(2))
            title_part = stem[:m2.start()]
        else:
            title_part = stem
    year = None
    ym = _MN_YEAR.search(title_part)
    if ym:
        year = ym.group(1)
        title_part = title_part[:ym.start()]
    else:
        ym2 = _MN_YEAR_END.search(title_part.strip())
        if ym2:
            year = ym2.group(1)
            title_part = title_part[:ym2.start()]
    title = re.sub(r"[._]+", " ", title_part)
    title = re.sub(r"\s+", " ", title).strip(" -_.[]()")
    return {"title": title, "year": year, "season": s, "episode": e,
            "episode_end": e2, "is_tv": s is not None, "raw": stem}


def _online_sanitize(t):
    t = _rn_strip_illegal(_rn_strip_pictographs(t or ""))
    t = re.sub(r"\s+", " ", t).strip().rstrip(". ")
    return t


def _online_ids_suffix(ids):
    if not ids:
        return ""
    parts = [f"{{{k}-{v}}}" for k, v in ids.items() if v]
    return (" " + " ".join(parts)) if parts else ""


def _plex_tv_name(show, year, s, e, e2, eptitle, ext, ids=None):
    base = _online_sanitize(show)
    if year:
        base += f" ({year})"
    epc = f"S{s:02d}E{e:02d}" + (f"-E{e2:02d}" if e2 else "")
    name = f"{base} - {epc}"
    et = _online_sanitize(eptitle)
    if et:
        name += f" - {et}"
    return name + _online_ids_suffix(ids) + ext


def _plex_movie_name(title, year, ext, ids=None):
    base = _online_sanitize(title)
    if year:
        base += f" ({year})"
    return base + _online_ids_suffix(ids) + ext


def _online_pick_show(args, candidates, guess_title, kind):
    """Returns ('ok', candidate) | ('research', None) | ('skip', None). Shows a
    Latin-friendly title (English fallback) so non-Latin names don't render as boxes."""
    labels = []
    for c in candidates:
        disp = _online_best_title(args, c)
        extra = f"   [{c['orig']}]" if (c.get('orig') and c['orig'] != disp and not _online_needs_latin(c['orig'])) else ""
        labels.append(f"{disp} ({c['year'] or '----'}){extra}")
    helps = [((c['overview'] or "(no overview)")[:400]) for c in candidates]
    labels.append("-> Type a different search query")
    labels.append("-> Skip this group")
    helps += ["Enter a different title to search TMDB again.", "Leave these files unchanged."]
    idx = ask_pick(f"Which {kind} is \"{guess_title}\"?", labels, default=0,
                   allow_back=True, help=helps)
    if idx is None or idx == len(candidates) + 1:
        return ("skip", None)
    if idx == len(candidates):
        return ("research", None)
    return ("ok", candidates[idx])


def _online_needs_latin(s):
    return any(ord(c) >= 0x370 for c in (s or ""))


def _online_best_title(args, cand):
    """Prefer a Latin-script title for file names (so names stay readable on Windows
    consoles and match Plex conventions). Uses the localized title when it is Latin,
    otherwise the English title, then the original."""
    t = (cand.get("title") or "").strip()
    if t and not _online_needs_latin(t):
        return t
    try:
        d = _tmdb_cached(args, f"/{cand['kind']}/{cand['id']}", {"language": "en-US"})
        en = (d.get("name") or d.get("title") or "").strip()
    except Exception:
        en = ""
    if en and not _online_needs_latin(en):
        return en
    orig = (cand.get("orig") or "").strip()
    if orig and not _online_needs_latin(orig):
        return orig
    return t or en or orig or "Unknown"


def _online_ensure_key(args):
    """Ensures a TMDB key/token is available; offers to enter and save one. Returns
    True when a key is available."""
    key, bearer = _tmdb_auth(args)
    if key or bearer:
        return True
    log_warn("No TMDB API key configured.")
    log_info("Get a FREE key at themoviedb.org -> Settings -> API (Developer plan).")
    if ask_yes_no("Enter a TMDB key/token now and save it?", default_no=False):
        k = (ask_text("TMDB v3 API key or v4 token", "") or "").strip()
        if k:
            cfg = load_config()
            if k.count(".") >= 2 and len(k) > 60:   # looks like a v4 JWT bearer
                cfg["tmdb_bearer"] = k
            else:
                cfg["tmdb_key"] = k
            save_config(cfg)
            apply_config_to_args(args, cfg, force=True)
            log_done("Saved.")
    key, bearer = _tmdb_auth(args)
    if not (key or bearer):
        log_warn("Cannot continue without a TMDB key.")
        return False
    return True


def _online_build_groups(videos):
    """Groups the videos by detected title (+ tv/movie; movies also by year)."""
    groups = {}
    for v in videos:
        info = _parse_media_name(Path(v).name)
        if not info["title"]:
            log_warn(f"{Path(v).name}: could not detect a title - skipping.")
            continue
        gkey = (info["title"].lower(), info["is_tv"], "" if info["is_tv"] else (info["year"] or ""))
        groups.setdefault(gkey, {"guess": info["title"], "year": info["year"], "is_tv": info["is_tv"],
                                 "query": info["title"], "files": [], "cands": [], "chosen": None, "caches": {}})
        groups[gkey]["files"].append((v, info))
    return list(groups.values())


def _online_search_group(args, g):
    try:
        g["cands"] = (_tmdb_search_tv(args, g["query"], g["year"]) if g["is_tv"]
                      else _tmdb_search_movie(args, g["query"], g["year"]))
        if not g["cands"] and g["year"]:
            g["cands"] = (_tmdb_search_tv(args, g["query"]) if g["is_tv"]
                          else _tmdb_search_movie(args, g["query"]))
    except Exception as e:
        log_warn("  " + _tmdb_error_hint(e))
        g["cands"] = []
    g["chosen"] = 0 if g["cands"] else None
    g["caches"] = {}


def _online_plan_entries(args, groups, embed_ids, folder_mode, use_eptitle=True):
    """Builds [dir, old, new, sub, changed, group] for every file, using cached
    per-group title / imdb / season data so toggles are instant. folder_mode is
    'off' | 'season' (Season NN) | 's' (SNN). use_eptitle appends the TMDB episode
    title (in the current language) when True."""
    entries = []
    for g in groups:
        chosen = g["cands"][g["chosen"]] if (g["cands"] and g["chosen"] is not None) else None
        if not chosen:
            for v, fi in g["files"]:
                p = Path(v)
                entries.append([str(p.parent), p.name, p.name, None, False, g])
            continue
        c = g["caches"]
        if "title" not in c:
            c["title"] = _online_best_title(args, chosen)
        title = c["title"]
        ids = None
        if embed_ids:
            if "imdb" not in c:
                c["imdb"] = _tmdb_external_ids(args, chosen["kind"], chosen["id"])
            ids = {"tmdb": chosen["id"]}
            if c["imdb"]:
                ids["imdb"] = c["imdb"]
        show_year = chosen["year"] or g["year"]
        show_folder = f"{_online_sanitize(title)}" + (f" ({show_year})" if show_year else "")
        seasons = c.setdefault("seasons", {})
        for v, fi in g["files"]:
            p = Path(v)
            if g["is_tv"]:
                s, e, e2 = fi["season"], fi["episode"], fi["episode_end"]
                if s not in seasons:
                    seasons[s] = _tmdb_season_titles(args, chosen["id"], s)
                ep = seasons[s].get(e, "") if use_eptitle else ""
                new = _plex_tv_name(title, show_year, s, e, e2, ep, p.suffix, ids)
                if folder_mode == "season":
                    sub = show_folder + os.sep + f"Season {s:02d}"
                elif folder_mode == "s":
                    sub = show_folder + os.sep + f"S{s:02d}"
                else:
                    sub = None
            else:
                new = _plex_movie_name(title, show_year, p.suffix, ids)
                sub = show_folder if folder_mode != "off" else None
            changed = (new != p.name) or bool(sub)
            entries.append([str(p.parent), p.name, new, sub, changed, g])
    return entries


def _online_disp_light(c):
    """Latin-friendly title for a candidate WITHOUT extra network calls."""
    t = (c.get("title") or "").strip()
    if t and not _online_needs_latin(t):
        return t
    o = (c.get("orig") or "").strip()
    if o and not _online_needs_latin(o):
        return o
    return t or o or "?"


def _online_live_match(args, g, info_fn=None):
    """Live, search-as-you-type match picker. The search box starts pre-filled with
    the detected title; editing it re-queries TMDB immediately and the result list
    updates. Enter selects the highlighted show; Tab shows detailed info; Esc keeps
    the current match. info_fn(candidate), if given, renders context-specific info
    (e.g. available online subtitles); otherwise TMDB details are shown."""
    kind = "series" if g["is_tv"] else "movie"

    def _search(q):
        orig = getattr(args, "meta_lang", None)
        args.meta_lang = "en"     # English titles -> readable (Latin) results
        try:
            return (_tmdb_search_tv(args, q) if g["is_tv"] else _tmdb_search_movie(args, q))
        except Exception:
            return []
        finally:
            args.meta_lang = orig

    def _show_info(c):
        (info_fn(c) if info_fn else _online_tmdb_info(args, c))

    query = (g.get("query") or g["guess"] or "").strip()
    results = []
    last = None
    sel = 0
    top = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                if query != last:
                    last = query
                    results = _search(query.strip()) if len(query.strip()) >= 2 else []
                    sel = 0
                    top = 0
                sel = max(0, min(sel, max(0, len(results) - 1)))
                cols, rows = _fb_termsize()
                head = [
                    f"{Fore.MAGENTA}{Style.BRIGHT}=== Change match ({kind}) ==={Style.RESET_ALL}",
                    f"{Fore.YELLOW}Search:{Style.RESET_ALL} {query}_",
                    f"{Style.DIM}{len(results)} result(s){Style.RESET_ALL}", "",
                ]
                foot = ["", f"{Style.DIM}\u2191\u2193 move | type / backspace to edit | "
                        f"Tab = more info | Enter = select | Esc = cancel{Style.RESET_ALL}"]
                avail = max(3, rows - len(head) - len(foot))
                if sel < top:
                    top = sel
                elif sel >= top + avail:
                    top = sel - avail + 1
                top = max(0, top)
                body = []
                for i, c in enumerate(results[top:top + avail], start=top):
                    label = f"{_online_disp_light(c)} ({c['year'] or '----'})   [tmdb-{c['id']}]"
                    if i == sel:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {_fb_trunc(label, cols - 4)}{Style.RESET_ALL}")
                    else:
                        body.append(f"  {Fore.CYAN}{_fb_trunc(label, cols - 4)}{Style.RESET_ALL}")
                if not results:
                    body.append(f"  {Style.DIM}" + ("(type at least 2 characters to search)"
                                if len(query.strip()) < 2 else "(no matches)") + f"{Style.RESET_ALL}")
                _fb_write_frame(head + body + foot)
                k = _read_key()
                if k == "esc":
                    return
                elif k in ("enter", "right"):
                    if results:
                        g["cands"] = results
                        g["chosen"] = sel
                        g["query"] = query
                        g["caches"] = {}
                    return
                elif k == ("char", "\t"):
                    if results:
                        _show_info(results[sel])
                        _fb_enter_screen()
                elif k == "up":
                    sel = (sel - 1) % len(results) if results else 0
                elif k == "down":
                    sel = (sel + 1) % len(results) if results else 0
                elif k == "pgup":
                    sel = max(0, sel - avail)
                elif k == "pgdn":
                    sel = min(len(results) - 1, sel + avail) if results else 0
                elif k == "backspace":
                    query = query[:-1]
                elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
                    query += k[1]
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _online_tmdb_info(args, cand):
    """Scrollable TMDB details for a candidate (overview, seasons/episodes, genres...)."""
    kind = cand.get("kind", "tv")
    try:
        d = _tmdb_cached(args, f"/{kind}/{cand['id']}", {"language": _tmdb_lang(args)})
    except Exception as e:
        log_warn("TMDB details: " + _tmdb_error_hint(e))
        return
    L = [f"{Fore.CYAN}Title:{Style.RESET_ALL} {cand.get('title')}",
         f"{Fore.CYAN}Original:{Style.RESET_ALL} {d.get('original_name') or d.get('original_title') or '-'}",
         f"{Fore.CYAN}Year:{Style.RESET_ALL} {cand.get('year') or '-'}    "
         f"{Fore.CYAN}Rating:{Style.RESET_ALL} {d.get('vote_average', '-')}/10 ({d.get('vote_count', 0)} votes)",
         f"{Fore.CYAN}Genres:{Style.RESET_ALL} {', '.join(g.get('name', '') for g in d.get('genres', [])) or '-'}",
         f"{Fore.CYAN}TMDB:{Style.RESET_ALL} tmdb-{cand['id']}", ""]
    if kind == "tv":
        L.append(f"{Fore.CYAN}Seasons:{Style.RESET_ALL} {d.get('number_of_seasons', '?')}   "
                 f"{Fore.CYAN}Episodes:{Style.RESET_ALL} {d.get('number_of_episodes', '?')}   "
                 f"{Fore.CYAN}Status:{Style.RESET_ALL} {d.get('status', '-')}")
        L.append(f"{Fore.CYAN}Networks:{Style.RESET_ALL} "
                 f"{', '.join(n.get('name', '') for n in d.get('networks', [])) or '-'}")
        for s in d.get("seasons", []):
            L.append(f"  Season {s.get('season_number')}: {s.get('episode_count', '?')} episodes"
                     + (f"  ({s.get('air_date')})" if s.get("air_date") else ""))
    else:
        L.append(f"{Fore.CYAN}Runtime:{Style.RESET_ALL} {d.get('runtime', '?')} min   "
                 f"{Fore.CYAN}Released:{Style.RESET_ALL} {d.get('release_date', '-')}")
    ov = (d.get("overview") or "").strip()
    if ov:
        L += ["", f"{Fore.CYAN}Overview:{Style.RESET_ALL}"]
        # wrap overview to ~90 chars
        import textwrap
        L += ["  " + ln for ln in textwrap.wrap(ov, 90)]
    _vid_scroll_view(f"TMDB info - {cand.get('title')}", L)


def _online_change_match(args, groups, info_fn=None):
    """Re-pick the TMDB match. With multiple detected titles, first choose which one,
    then use the live search picker. info_fn is forwarded to the picker for the
    Tab = more info panel."""
    if len(groups) == 1:
        _online_live_match(args, groups[0], info_fn=info_fn)
        return
    labels = []
    for g in groups:
        ch = g["cands"][g["chosen"]] if (g["cands"] and g["chosen"] is not None) else None
        labels.append(f"{g['guess']} -> " + (f"{_online_disp_light(ch)} ({ch['year'] or '----'})" if ch else "(no match)"))
    labels.append("cancel")
    gi = ask_pick("Change which match?", labels, default=0, allow_back=True)
    if gi is None or gi >= len(groups):
        return
    _online_live_match(args, groups[gi], info_fn=info_fn)


def _online_apply(changed):
    done = skipped = 0
    for d, old, new, sub, _ch, _g in changed:
        src = os.path.join(d, old)
        dstdir = os.path.join(d, sub) if sub else d
        dst = os.path.join(dstdir, new)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src):
            log_warn(f"{old}: target exists - skipping.")
            skipped += 1
            continue
        try:
            if sub:
                os.makedirs(dstdir, exist_ok=True)
            os.replace(src, dst)
            done += 1
        except OSError as ex:
            log_warn(f"{old}: {ex}")
            skipped += 1
    log_done(f"Done: {done} renamed/moved, {skipped} skipped.")


def _online_rename_preview(args, videos):
    """Interactive online-rename preview in the same style as the local renamer:
    toggle options with keys, re-pick the match, scroll the before/after list, and
    apply with Enter. Esc cancels."""
    groups = _online_build_groups(videos)
    if not groups:
        log_warn("Could not detect any titles from the file names.")
        return
    print(f"{Fore.CYAN}Searching TMDB...{Style.RESET_ALL}")
    orig_lang = getattr(args, "meta_lang", None)
    lang_code = orig_lang or "cs"
    args.meta_lang = lang_code       # session-only override; the config is NOT changed
    for g in groups:
        _online_search_group(args, g)

    embed_ids = False
    folder_mode = "off"   # off | season (Season NN) | s (SNN)
    use_eptitle = True
    top = 0
    to_apply = []
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                entries = _online_plan_entries(args, groups, embed_ids, folder_mode, use_eptitle)
                seen = {}
                conflicts = set()
                for d, old, new, sub, ch, g in entries:
                    if not ch:
                        continue
                    tgt = os.path.join(d, sub or "", new)
                    if tgt in seen or (os.path.exists(tgt) and os.path.basename(tgt) != old):
                        conflicts.add(tgt)
                    seen[tgt] = True

                def _is_conf(e):
                    return os.path.join(e[0], e[3] or "", e[2]) in conflicts
                changed = [e for e in entries if e[4] and not _is_conf(e)]
                conf = [e for e in entries if e[4] and _is_conf(e)]
                unchanged = [e for e in entries if not e[4]]

                cols, rows = _fb_termsize()
                header = [
                    f"{Fore.MAGENTA}=== Online rename preview - {len(videos)} file(s) ==={Style.RESET_ALL}",
                    (f"{Fore.CYAN}[i]{Style.RESET_ALL} embed-ids:{_fb_onoff(embed_ids)}   "
                     f"{Fore.CYAN}[f]{Style.RESET_ALL} folders:"
                     + ({"off": f"{Style.DIM}off{Style.RESET_ALL}",
                         "season": f"{Fore.GREEN}Season NN{Style.RESET_ALL}",
                         "s": f"{Fore.GREEN}SNN{Style.RESET_ALL}"}[folder_mode])
                     + f"   {Fore.CYAN}[t]{Style.RESET_ALL} ep-title:{_fb_onoff(use_eptitle)}   "
                     f"{Fore.CYAN}[l]{Style.RESET_ALL} lang:{lang_code}   "
                     f"{Fore.CYAN}[m]{Style.RESET_ALL} change match"),
                ]
                for g in groups:
                    ch = g["cands"][g["chosen"]] if (g["cands"] and g["chosen"] is not None) else None
                    if ch:
                        t = g["caches"].get("title") or ch["title"]
                        header.append(f"  {Fore.CYAN}Match:{Style.RESET_ALL} {_fb_trunc(t, 40)} "
                                      f"({ch['year'] or '----'})  {Style.DIM}[tmdb-{ch['id']}]{Style.RESET_ALL}")
                    else:
                        header.append(f"  {Fore.RED}Match: (none for \"{_fb_trunc(g['guess'], 30)}\") "
                                      f"- press m{Style.RESET_ALL}")
                header.append("")

                view = [(e[1], (e[3] + os.sep if e[3] else "") + e[2], _is_conf(e)) for e in entries if e[4]]
                rows_avail = max(6, rows - len(header) - 3)
                if top > max(0, len(view) - rows_avail):
                    top = max(0, len(view) - rows_avail)
                # dynamic column: align the arrows to the longest old name, no wider than needed
                max_old = max((len(o) for o, _d, _c in view), default=10)
                oldw = min(max_old, max(10, cols - 6 - 24))
                destw = max(16, cols - 6 - oldw)
                body = []
                for old, dest, cf in view[top:top + rows_avail]:
                    arrow = f"{Fore.RED}->{Style.RESET_ALL}" if cf else f"{Fore.CYAN}->{Style.RESET_ALL}"
                    ncol = Fore.RED if cf else Fore.GREEN
                    tail = "  [CONFLICT]" if cf else ""
                    body.append(f"  {_fb_trunc(old, oldw):<{oldw}} {arrow} {ncol}{_fb_trunc(dest, destw)}{tail}{Style.RESET_ALL}")
                if not view:
                    body.append(f"  {Style.DIM}(nothing to change - press m to pick a match){Style.RESET_ALL}")

                summary = (f"{Fore.GREEN}Changed: {len(changed)}{Style.RESET_ALL}   "
                           f"{Fore.RED}Conflicts: {len(conf)}{Style.RESET_ALL}   "
                           f"{Style.DIM}Unchanged: {len(unchanged)}{Style.RESET_ALL}")
                footer = (f"{Style.DIM}\u2191\u2193/PgUp/PgDn scroll | i/f/t toggle | l language | "
                          f"m change match | Enter = APPLY | Esc = cancel{Style.RESET_ALL}")
                _fb_write_frame(header + body + ["", summary, footer])

                k = _read_key()
                if k == "esc" or k == ("char", "q"):
                    return
                if k == "enter":
                    if not changed:
                        continue
                    to_apply = list(changed)
                    break
                if k == "down":
                    top = min(max(0, len(view) - rows_avail), top + 1)
                elif k == "up":
                    top = max(0, top - 1)
                elif k == "pgdn":
                    top = min(max(0, len(view) - rows_avail), top + rows_avail)
                elif k == "pgup":
                    top = max(0, top - rows_avail)
                elif isinstance(k, tuple) and k[0] == "char":
                    ch = k[1]
                    if ch in ("i", "I"):
                        embed_ids = not embed_ids
                    elif ch in ("f", "F"):
                        folder_mode = {"off": "season", "season": "s", "s": "off"}[folder_mode]
                    elif ch in ("t", "T"):
                        use_eptitle = not use_eptitle
                    elif ch in ("l", "L"):
                        code = _vid_pick_language(lang_code)
                        if code and code != "und":
                            lang_code = code
                            args.meta_lang = code           # session-only; config untouched
                            for g in groups:
                                g["caches"] = {}            # refetch titles/episodes in the new language
                        _fb_enter_screen()   # re-hide cursor after the nested picker
                    elif ch in ("m", "M"):
                        _online_change_match(args, groups)
                        _fb_enter_screen()   # re-hide cursor after the nested picker
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
            args.meta_lang = orig_lang   # restore the config default (config file was never touched)
    # normal terminal mode here -> apply is visible
    _online_apply(to_apply)
    try:
        input(f"\n{Fore.CYAN}Press Enter to continue...{Style.RESET_ALL}")
    except (EOFError, KeyboardInterrupt):
        pass


def _online_rename_core(args, videos):
    """Recognizes shows/movies via TMDB and renames the given video files to the
    Plex scheme. Uses the interactive preview on a real terminal; otherwise a
    simple prompt-based flow. Shared by the wizard and the file browser."""
    if not videos:
        log_warn("No videos to process.")
        return
    log_info("Data by TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB.")
    if not _online_ensure_key(args):
        return
    if _tui_supported():
        _online_rename_preview(args, videos)
    else:
        _online_rename_classic(args, videos)


def _online_rename_classic(args, videos):
    """Non-TTY fallback for the online rename (simple prompts + printed preview)."""
    if not videos:
        log_warn("No videos to process.")
        return
    embed_ids = ask_yes_no("Embed IDs for perfect Plex matching (e.g. {tmdb-123} {imdb-tt...})?", default_no=True)
    make_folders = ask_yes_no("Also move files into Plex folders (Show (Year)/Season NN/)?", default_no=True)

    # group by detected title (+ tv/movie); movies group by title+year
    groups = {}
    for v in videos:
        info = _parse_media_name(Path(v).name)
        if not info["title"]:
            log_warn(f"{Path(v).name}: could not detect a title - skipping.")
            continue
        gkey = (info["title"].lower(), info["is_tv"], "" if info["is_tv"] else (info["year"] or ""))
        groups.setdefault(gkey, {"info": info, "files": []})
        groups[gkey]["files"].append((v, info))
    if not groups:
        log_warn("Could not detect any titles from the file names.")
        return

    interactive = sys.stdin.isatty()
    plan = []   # (dir, old_name, new_name, subfolder_or_None)
    for gkey, g in sorted(groups.items()):
        info = g["info"]
        guess, year = info["title"], info["year"]
        is_tv = info["is_tv"]
        kind = "series" if is_tv else "movie"
        log_info(f"Detected {kind}: \"{guess}\"" + (f" ({year})" if year else "")
                 + f"  - {len(g['files'])} file(s)")
        query = guess
        chosen = None
        while True:
            try:
                cands = _tmdb_search_tv(args, query, year) if is_tv else _tmdb_search_movie(args, query, year)
                if not cands and year:
                    cands = _tmdb_search_tv(args, query) if is_tv else _tmdb_search_movie(args, query)
            except Exception as e:
                log_warn("  " + _tmdb_error_hint(e))
                cands = []
            if not cands:
                if interactive and ask_yes_no(f"  No match for \"{query}\". Type a different query?", default_no=False):
                    query = (ask_text("  Search query", guess) or guess).strip()
                    continue
                log_warn("  No match - skipping this group.")
                break
            if not interactive or len(cands) == 1:
                chosen = cands[0]
                if not interactive:
                    log_info(f"  Using: {chosen['title']} ({chosen['year'] or '----'})")
                break
            action, cand = _online_pick_show(args, cands, guess, kind)
            if action == "skip":
                break
            if action == "research":
                query = (ask_text("  New search query", query) or query).strip()
                continue
            chosen = cand
            break
        if not chosen:
            continue

        title = _online_best_title(args, chosen)   # Latin-friendly name for files/folders
        ids = None
        if embed_ids:
            ids = {"tmdb": chosen["id"]}
            imdb = _tmdb_external_ids(args, chosen["kind"], chosen["id"])
            if imdb:
                ids["imdb"] = imdb
        show_year = chosen["year"] or year
        season_cache = {}
        for v, finfo in g["files"]:
            p = Path(v)
            if is_tv:
                s, e, e2 = finfo["season"], finfo["episode"], finfo["episode_end"]
                if s not in season_cache:
                    season_cache[s] = _tmdb_season_titles(args, chosen["id"], s)
                eptitle = season_cache[s].get(e, "")
                newname = _plex_tv_name(title, show_year, s, e, e2, eptitle, p.suffix, ids)
                sub = (f"{_online_sanitize(title)}" + (f" ({show_year})" if show_year else "")
                       + os.sep + f"Season {s:02d}") if make_folders else None
            else:
                newname = _plex_movie_name(title, show_year, p.suffix, ids)
                sub = (f"{_online_sanitize(title)}" + (f" ({show_year})" if show_year else "")) if make_folders else None
            if newname != p.name or sub:
                plan.append((str(p.parent), p.name, newname, sub))

    if not plan:
        log_warn("Nothing to rename.")
        return

    print()
    log_info(f"Preview ({len(plan)} file(s)):")
    seen_targets = {}
    conflicts = set()
    for d, old, new, sub in plan:
        tgt = os.path.join(d, sub or "", new)
        if tgt in seen_targets or (os.path.exists(tgt) and os.path.basename(tgt) != old):
            conflicts.add(tgt)
        seen_targets[tgt] = True
    for d, old, new, sub in sorted(plan, key=lambda x: (x[3] or "", x[2])):
        tgt = os.path.join(d, sub or "", new)
        dest = (sub + os.sep if sub else "") + new
        color = Fore.RED if tgt in conflicts else Fore.GREEN
        flag = "   [CONFLICT]" if tgt in conflicts else ""
        print(f"  {old}\n    {color}-> {dest}{flag}{Style.RESET_ALL}")
    print()
    log_info(f"Ready: {len([1 for d, o, n, s in plan if os.path.join(d, s or '', n) not in conflicts])}   "
             f"Conflicts: {len(conflicts)}")

    if not ask_yes_no(f"Apply and rename/move {len(plan)} file(s)?", default_no=True):
        log_info("Preview only - nothing changed.")
        return

    done = skipped = 0
    for d, old, new, sub in plan:
        src = os.path.join(d, old)
        dstdir = os.path.join(d, sub) if sub else d
        dst = os.path.join(dstdir, new)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if os.path.join(d, sub or "", new) in conflicts or (os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src)):
            log_warn(f"{old}: target exists - skipping.")
            skipped += 1
            continue
        try:
            if sub:
                os.makedirs(dstdir, exist_ok=True)
            os.replace(src, dst)
            done += 1
        except OSError as ex:
            log_warn(f"{old}: {ex}")
            skipped += 1
    print()
    log_done(f"Done: {done} renamed/moved, {skipped} skipped.")


# ============================================================================
# Online subtitle download (OpenSubtitles) - same UI as the online renamer,
# with MULTI-language selection. Reachable from the file browser via 's'.
# ============================================================================

def _pick_languages_multi(selected):
    """Searchable multi-select language picker (name + 2-letter code). Space toggles,
    type to filter, Enter confirms (returns a list of codes), Esc cancels (None)."""
    items = sorted(((name, c2) for c2, name in LANGUAGE_NAMES.items()), key=lambda x: x[0])
    sel = set(c.lower() for c in (selected or []))
    filt = ""
    pos = 0
    top = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                f = filt.lower().strip()
                view = [(n, c) for n, c in items if (not f or f in n.lower() or f in c.lower())]
                if not view:
                    view = [("(no match)", "")]
                pos = max(0, min(pos, len(view) - 1))
                cols, rows = _fb_termsize()
                chosen = ", ".join(sorted(sel)) if sel else "(none)"
                head = [
                    f"{Fore.MAGENTA}{Style.BRIGHT}=== Select languages ==={Style.RESET_ALL}",
                    f"{Fore.CYAN}Selected:{Style.RESET_ALL} {_fb_trunc(chosen, cols - 12)}",
                    f"{Fore.YELLOW}Search:{Style.RESET_ALL} {filt}_", "",
                ]
                foot = ["", f"{Style.DIM}\u2191\u2193 move | Space = toggle | type = search | "
                        f"Enter = confirm | Esc = cancel{Style.RESET_ALL}"]
                avail = max(3, rows - len(head) - len(foot))
                if pos < top:
                    top = pos
                elif pos >= top + avail:
                    top = pos - avail + 1
                top = max(0, top)
                body = []
                for i, (n, c) in enumerate(view[top:top + avail], start=top):
                    box = "[x]" if c in sel else "[ ]"
                    label = f"{box} {n}  ({c})" if c else n
                    if i == pos:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {label}{Style.RESET_ALL}")
                    else:
                        col = Fore.YELLOW if c in sel else Fore.CYAN
                        body.append(f"  {col}{label}{Style.RESET_ALL}")
                _fb_write_frame(head + body + foot)
                k = _read_key()
                if k == "esc":
                    return None
                elif k == "enter":
                    return sorted(sel)
                elif k == ("char", " "):
                    c = view[pos][1]
                    if c:
                        sel.discard(c) if c in sel else sel.add(c)
                elif k == "up":
                    pos = (pos - 1) % len(view)
                elif k == "down":
                    pos = (pos + 1) % len(view)
                elif k == "pgup":
                    pos = max(0, pos - avail)
                elif k == "pgdn":
                    pos = min(len(view) - 1, pos + avail)
                elif k == "home":
                    pos = 0
                elif k == "end":
                    pos = len(view) - 1
                elif k == "backspace":
                    filt = filt[:-1]
                    pos = top = 0
                elif isinstance(k, tuple) and k[0] == "char" and k[1] >= " ":
                    filt += k[1]
                    pos = top = 0
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _subs_ensure_key(args):
    key = getattr(args, "opensubtitles_key", None) or os.environ.get("OPENSUBTITLES_API_KEY")
    if not key:
        log_warn("No OpenSubtitles API key configured.")
        log_info("Get a free API key at opensubtitles.com (consumer API). Downloading also needs an account.")
        if ask_yes_no("Enter an OpenSubtitles API key now and save it?", default_no=False):
            k = (ask_text("OpenSubtitles API key", "") or "").strip()
            if k:
                cfg = load_config()
                cfg["opensubtitles_key"] = k
                save_config(cfg)
                apply_config_to_args(args, cfg, force=True)
                log_done("Saved.")
        key = getattr(args, "opensubtitles_key", None)
    if not key:
        log_warn("Cannot continue without an OpenSubtitles API key.")
        return None
    return key


def _subs_client(args):
    key = getattr(args, "opensubtitles_key", None) or os.environ.get("OPENSUBTITLES_API_KEY")
    user = getattr(args, "opensubtitles_user", None) or os.environ.get("OPENSUBTITLES_USER")
    pw = getattr(args, "opensubtitles_password", None) or os.environ.get("OPENSUBTITLES_PASSWORD")
    return OpenSubtitles(key, username=user, password=pw)


def _subs_search(client, langs, query=None, season=None, episode=None, tmdb_id=None, hearing_impaired=None):
    from urllib.parse import urlencode
    params = {"languages": ",".join(langs)}
    if query:
        params["query"] = query
    if season is not None:
        params["season_number"] = season
    if episode is not None:
        params["episode_number"] = episode
    if tmdb_id:
        params["tmdb_id"] = tmdb_id
    if hearing_impaired is not None:
        params["hearing_impaired"] = "only" if hearing_impaired else "exclude"
    try:
        d = client._req("GET", "/subtitles?" + urlencode(params))
        return d.get("data", []) or []
    except Exception as e:
        log_warn("OpenSubtitles search: " + str(e))
        return []


def _subs_dl(r):
    a = r.get("attributes", {}) or {}
    return int(a.get("download_count", 0) or 0)


def _subs_best_by_lang(results, langs):
    """Returns {lang: best_result} choosing the most-downloaded per language."""
    best = {}
    for r in results:
        a = r.get("attributes", {}) or {}
        lg = (a.get("language") or "").lower()
        if lg not in langs:
            continue
        if lg not in best or _subs_dl(r) > _subs_dl(best[lg]):
            best[lg] = r
    return best


def _subs_file_id(r):
    files = (r.get("attributes", {}) or {}).get("files") or []
    return files[0].get("file_id") if files else None


def _subs_info_view(args, client, langs, cand):
    """Scrollable panel: which episodes/files have online subtitles for this show,
    with release, downloads, fps, hearing-impaired and uploader details."""
    tid = cand["id"]
    L = [f"{Fore.CYAN}Show:{Style.RESET_ALL} {_online_disp_light(cand)} ({cand.get('year') or '----'})  "
         f"{Style.DIM}[tmdb-{tid}]{Style.RESET_ALL}",
         f"{Fore.CYAN}Languages:{Style.RESET_ALL} {', '.join(langs) or '-'}", ""]
    # optional TMDB overview
    try:
        d = _tmdb_cached(args, f"/{cand.get('kind', 'tv')}/{tid}", {"language": _tmdb_lang(args)})
        ov = (d.get("overview") or "").strip()
        if ov:
            import textwrap
            L += [f"{Fore.CYAN}Overview:{Style.RESET_ALL}"] + ["  " + ln for ln in textwrap.wrap(ov, 90)] + [""]
    except Exception:
        pass
    L.append(f"{Fore.GREEN}Available online subtitles (OpenSubtitles):{Style.RESET_ALL}")
    try:
        res = _subs_search(client, langs, tmdb_id=tid)
    except Exception as e:
        res = []
        L.append(f"  {Style.DIM}(search failed: {e}){Style.RESET_ALL}")
    if not res:
        L.append(f"  {Style.DIM}(none found for these languages){Style.RESET_ALL}")
    else:
        by_ep = {}
        for r in res:
            a = r.get("attributes", {}) or {}
            fd = a.get("feature_details", {}) or {}
            by_ep.setdefault((fd.get("season_number"), fd.get("episode_number")), []).append(r)
        L.append(f"  {Style.DIM}{len(res)} subtitle file(s) across {len(by_ep)} item(s){Style.RESET_ALL}")
        for key in sorted(by_ep, key=lambda x: ((x[0] or 0), (x[1] or 0))):
            s, e = key
            fd0 = (by_ep[key][0].get("attributes", {}) or {}).get("feature_details", {}) or {}
            eptitle = fd0.get("title") or fd0.get("movie_name") or ""
            hdr = (f"S{s:02d}E{e:02d}" if (s and e) else (fd0.get("parent_title") or "item"))
            L.append("")
            L.append(f"{Fore.YELLOW}{hdr}{Style.RESET_ALL}" + (f"  {eptitle}" if eptitle else ""))
            for r in sorted(by_ep[key], key=_subs_dl, reverse=True)[:10]:
                a = r.get("attributes", {}) or {}
                L.append(f"   {(a.get('language') or '?'):5} dl {(_subs_dl(r)):>6}  "
                         f"fps {str(a.get('fps') or '?'):>5}  hi {'Y' if a.get('hearing_impaired') else 'N'}  "
                         f"{(a.get('release') or '')[:48]}")
    _vid_scroll_view(f"Online subtitles - {_online_disp_light(cand)}", L)


def _subs_result_line(r):
    a = r.get("attributes", {}) or {}
    fd = a.get("feature_details", {}) or {}
    ep = f"S{fd.get('season_number'):02d}E{fd.get('episode_number'):02d} " if (fd.get('season_number') and fd.get('episode_number')) else ""
    return (f"{(a.get('language') or '?'):5} dl {_subs_dl(r):>6}  fps {str(a.get('fps') or '?'):>5}  "
            f"hi {'Y' if a.get('hearing_impaired') else 'N'}  {ep}{(a.get('release') or a.get('files',[{}])[0].get('file_name','') or '')[:56]}")


def _subs_file_results(client, langs, video, tmdb_id):
    info = _parse_media_name(Path(video).name)
    try:
        return _subs_search(client, langs,
                            query=(None if tmdb_id else (info["title"] or Path(video).stem)),
                            season=info["season"], episode=info["episode"], tmdb_id=tmdb_id)
    except Exception:
        return []


def _subs_file_info(args, client, langs, video, tmdb_id):
    """Read-only scrollable list of the subtitles available online for one file."""
    res = _subs_file_results(client, langs, video, tmdb_id)
    L = [f"{Fore.CYAN}File:{Style.RESET_ALL} {Path(video).name}",
         f"{Fore.CYAN}Languages:{Style.RESET_ALL} {', '.join(langs) or '-'}",
         f"{Fore.CYAN}Available:{Style.RESET_ALL} {len(res)} subtitle file(s)", ""]

    def _lg(r):
        return (r.get("attributes", {}) or {}).get("language", "")
    if not res:
        L.append(f"  {Style.DIM}(none found for these languages){Style.RESET_ALL}")
    else:
        for r in sorted(res, key=lambda r: ((langs.index(_lg(r)) if _lg(r) in langs else 99), -_subs_dl(r))):
            L.append("  " + _subs_result_line(r))
    _vid_scroll_view(f"Subtitles - {Path(video).name}", L)


def _subs_pick_for_file(args, client, langs, video, tmdb_id, overwrite):
    """Interactive per-file picker: lists every available subtitle version for this
    episode and downloads the one you choose (Enter). Esc goes back."""
    res = _subs_file_results(client, langs, video, tmdb_id)

    def _lg(r):
        return (r.get("attributes", {}) or {}).get("language", "")
    if not res:
        _fb_write_frame([f"{Fore.MAGENTA}{Style.BRIGHT}=== {_fb_trunc(Path(video).name, 60)} ==={Style.RESET_ALL}",
                         "", f"  {Style.DIM}No subtitles found for the selected languages.{Style.RESET_ALL}",
                         "", f"  {Style.DIM}Tip: add languages with [l] in the preview.{Style.RESET_ALL}",
                         "", f"{Style.DIM}Press any key to go back...{Style.RESET_ALL}"])
        _read_key()
        return
    items = sorted(res, key=lambda r: ((langs.index(_lg(r)) if _lg(r) in langs else 99), -_subs_dl(r)))
    sel = 0
    top = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                cols, rows = _fb_termsize()
                head = [f"{Fore.MAGENTA}{Style.BRIGHT}=== Subtitles: {_fb_trunc(Path(video).name, 55)} ==={Style.RESET_ALL}",
                        f"{Style.DIM}{len(items)} version(s) - pick one to download{Style.RESET_ALL}", ""]
                foot = ["", f"{Style.DIM}\u2191\u2193 move | Enter = download this version | Esc = back{Style.RESET_ALL}"]
                avail = max(3, rows - len(head) - len(foot))
                sel = max(0, min(sel, len(items) - 1))
                if sel < top:
                    top = sel
                elif sel >= top + avail:
                    top = sel - avail + 1
                top = max(0, top)
                body = []
                for i, r in enumerate(items[top:top + avail], start=top):
                    line = _subs_result_line(r)
                    if i == sel:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {_fb_trunc(line, cols - 4)}{Style.RESET_ALL}")
                    else:
                        body.append(f"  {Fore.CYAN}{_fb_trunc(line, cols - 4)}{Style.RESET_ALL}")
                _fb_write_frame(head + body + foot)
                k = _read_key()
                if k == "esc" or k == ("char", "q"):
                    return
                elif k in ("enter", "right"):
                    r = items[sel]
                    lg = _lg(r) or "und"
                    fid = _subs_file_id(r)
                    out = f"{str(Path(video).with_suffix(''))}.{lg}.srt"
                    _fb_leave_screen()
                    if os.path.exists(out) and not overwrite:
                        log_warn(f"{Path(out).name}: already exists (enable [o] overwrite to replace).")
                    else:
                        client.login()
                        content = client.download_srt(fid) if fid else None
                        if content:
                            try:
                                with open(out, "w", encoding="utf-8") as f:
                                    f.write(content)
                                log_done(f"Saved {Path(out).name}")
                            except OSError as ex:
                                log_warn(f"{Path(out).name}: {ex}")
                        else:
                            log_warn(f"{Path(out).name}: download failed.")
                    try:
                        input(f"\n{Fore.CYAN}Press Enter to continue...{Style.RESET_ALL}")
                    except (EOFError, KeyboardInterrupt):
                        return
                    _fb_enter_screen()
                elif k == "up":
                    sel = (sel - 1) % len(items)
                elif k == "down":
                    sel = (sel + 1) % len(items)
                elif k == "pgup":
                    sel = max(0, sel - avail)
                elif k == "pgdn":
                    sel = min(len(items) - 1, sel + avail)
                elif k == "home":
                    sel = 0
                elif k == "end":
                    sel = len(items) - 1
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _subs_preview(args, videos):
    """Interactive OpenSubtitles download preview. Move the cursor with the arrows,
    press Enter on a file to pick a specific version, Tab to see all available
    subtitles for it, 'a' to batch-download the best per language. [l] languages
    (multi), [h] hearing-impaired, [o] overwrite, [m] refine the show match."""
    client = _subs_client(args)
    groups = _online_build_groups(videos)
    v2g = {}
    for g in groups:
        for v, _fi in g["files"]:
            v2g[v] = g
    _sl = getattr(args, "subs_langs", None) or getattr(args, "out_lang", None) or "cs"
    langs = [x.strip().lower() for x in str(_sl).split(",") if x.strip()] or ["cs"]
    hi = False
    overwrite = False
    cache = {}
    cursor = 0
    top = 0
    to_download = []

    def _tid(g):
        if g and g.get("cands") and g.get("chosen") is not None:
            return g["cands"][g["chosen"]]["id"]
        return None

    def plan_for(v):
        g = v2g.get(v)
        tid = _tid(g)
        key = (v, tuple(sorted(langs)), hi, tid)
        if key not in cache:
            info = _parse_media_name(Path(v).name)
            cache[key] = _subs_best_by_lang(_subs_search(
                client, langs,
                query=(None if tid else (info["title"] or Path(v).stem)),
                season=info["season"], episode=info["episode"],
                tmdb_id=tid, hearing_impaired=hi if hi else None), langs)
        return cache[key]

    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                entries = [(v, plan_for(v)) for v in videos]
                found_pairs = sum(len(b) for _v, b in entries)
                missing_pairs = len(videos) * len(langs) - found_pairs

                cols, rows = _fb_termsize()
                header = [
                    f"{Fore.MAGENTA}=== Subtitle download (OpenSubtitles) - {len(videos)} file(s) ==={Style.RESET_ALL}",
                    (f"{Fore.CYAN}[l]{Style.RESET_ALL} languages:{Fore.GREEN}{','.join(langs) or '-'}{Style.RESET_ALL}   "
                     f"{Fore.CYAN}[h]{Style.RESET_ALL} hearing-impaired:{_fb_onoff(hi)}   "
                     f"{Fore.CYAN}[o]{Style.RESET_ALL} overwrite:{_fb_onoff(overwrite)}   "
                     f"{Fore.CYAN}[m]{Style.RESET_ALL} change match"),
                ]
                for g in groups:
                    tid = _tid(g)
                    if tid:
                        ch = g["cands"][g["chosen"]]
                        header.append(f"  {Fore.CYAN}Match:{Style.RESET_ALL} {_fb_trunc(_online_disp_light(ch), 40)} "
                                      f"({ch['year'] or '----'})  {Style.DIM}[tmdb-{tid}]{Style.RESET_ALL}")
                header.append("")

                plain = []
                for v, best in entries:
                    cells = [(lg, (f"{lg}:OK[{_subs_dl(best[lg])}]" if best.get(lg) else f"{lg}:--"), bool(best.get(lg)))
                             for lg in langs]
                    plain.append((Path(v).name, cells))
                cellw = {lg: max((len(c[1]) for _n, cs in plain for c in cs if c[0] == lg), default=6) for lg in langs}
                namew = min(max((len(n) for n, _c in plain), default=10),
                            max(10, cols - sum(cellw.values()) - 2 * len(langs) - 8))

                rows_avail = max(4, rows - len(header) - 3)
                cursor = max(0, min(cursor, len(plain) - 1)) if plain else 0
                if cursor < top:
                    top = cursor
                elif cursor >= top + rows_avail:
                    top = cursor - rows_avail + 1
                top = max(0, top)
                body = []
                for idx, (name, cells) in enumerate(plain[top:top + rows_avail], start=top):
                    seg = "  ".join(
                        f"{(Fore.GREEN if found else Style.DIM)}{txt:<{cellw[lg]}}{Style.RESET_ALL}"
                        for lg, txt, found in cells)
                    if idx == cursor:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {_fb_trunc(name, namew):<{namew}}{Style.RESET_ALL}  {seg}")
                    else:
                        body.append(f"  {_fb_trunc(name, namew):<{namew}}  {seg}")
                if not plain:
                    body.append(f"  {Style.DIM}(no videos){Style.RESET_ALL}")

                summary = (f"{Fore.GREEN}Found: {found_pairs}{Style.RESET_ALL}   "
                           f"{Style.DIM}Missing: {missing_pairs}{Style.RESET_ALL}   "
                           f"(saved as name.<lang>.srt)")
                footer = (f"{Style.DIM}\u2191\u2193 move | Enter = versions/download | Tab = info | "
                          f"a = download all | l/h/o/m | Esc = cancel{Style.RESET_ALL}")
                _fb_write_frame(header + body + ["", summary, footer])

                k = _read_key()
                if k == "esc" or k == ("char", "q"):
                    return 0
                if k == "enter":
                    if plain:
                        v = videos[cursor]
                        _subs_pick_for_file(args, client, langs, v, _tid(v2g.get(v)), overwrite)
                        cache.clear()
                        _fb_enter_screen()
                    continue
                if k == ("char", "\t"):
                    if plain:
                        v = videos[cursor]
                        _subs_file_info(args, client, langs, v, _tid(v2g.get(v)))
                        _fb_enter_screen()
                    continue
                if k == "down":
                    cursor = (cursor + 1) % len(plain) if plain else 0
                elif k == "up":
                    cursor = (cursor - 1) % len(plain) if plain else 0
                elif k == "pgdn":
                    cursor = min(len(plain) - 1, cursor + rows_avail) if plain else 0
                elif k == "pgup":
                    cursor = max(0, cursor - rows_avail)
                elif k == "home":
                    cursor = 0
                elif k == "end":
                    cursor = len(plain) - 1 if plain else 0
                elif isinstance(k, tuple) and k[0] == "char":
                    ch = k[1]
                    if ch in ("a", "A"):
                        if found_pairs == 0:
                            continue
                        to_download = entries
                        break
                    elif ch in ("l", "L"):
                        newl = _pick_languages_multi(langs)
                        if newl is not None:
                            langs = newl or langs
                        _fb_enter_screen()
                    elif ch in ("h", "H"):
                        hi = not hi
                    elif ch in ("o", "O"):
                        overwrite = not overwrite
                    elif ch in ("m", "M"):
                        _online_change_match(args, groups,
                                             info_fn=lambda c: _subs_info_view(args, client, langs, c))
                        cache.clear()
                        _fb_enter_screen()
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()

    # batch download (from 'a') - visible in normal mode
    if not to_download:
        return 0
    logged = client.login()
    if not logged and not (client.user and client.password):
        log_warn("No OpenSubtitles account set - downloading requires a username/password (--config).")
    done = skipped = fail = 0
    for v, best in to_download:
        stem = str(Path(v).with_suffix(""))
        for lg, r in best.items():
            out = f"{stem}.{lg}.srt"
            if os.path.exists(out) and not overwrite:
                skipped += 1
                continue
            fid = _subs_file_id(r)
            content = client.download_srt(fid) if fid else None
            if content:
                try:
                    with open(out, "w", encoding="utf-8") as f:
                        f.write(content)
                    log_done(f"{Path(out).name}")
                    done += 1
                except OSError as ex:
                    log_warn(f"{Path(out).name}: {ex}")
                    fail += 1
            else:
                fail += 1
    print()
    log_done(f"Downloaded: {done}   skipped(existing): {skipped}   failed: {fail}")
    return fail


def _subs_classic(args, videos):
    """Non-TTY fallback: downloads the default-language subtitle for each video."""
    client = _subs_client(args)
    _sl = getattr(args, "subs_langs", None) or getattr(args, "out_lang", None) or "en"
    langs = [x.strip().lower() for x in str(_sl).split(",") if x.strip()] or ["en"]
    client.login()
    done = fail = 0
    for v in videos:
        info = _parse_media_name(Path(v).name)
        res = _subs_search(client, langs, query=info["title"] or Path(v).stem,
                           season=info["season"], episode=info["episode"])
        best = _subs_best_by_lang(res, langs)
        for lg, r in best.items():
            fid = _subs_file_id(r)
            content = client.download_srt(fid) if fid else None
            out = f"{str(Path(v).with_suffix(''))}.{lg}.srt"
            if content:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(content)
                log_done(Path(out).name)
                done += 1
            else:
                fail += 1
    log_done(f"Downloaded: {done}   failed: {fail}")
    return fail


def _subs_download_core(args, videos):
    """Downloads subtitles from OpenSubtitles for the given videos. Interactive
    preview on a real terminal; a simple flow otherwise. Returns the number of
    failed downloads (0 = all fine / nothing to do)."""
    if not videos:
        log_warn("No videos to process.")
        return 0
    log_info("Subtitles by OpenSubtitles.com.")
    if not _subs_ensure_key(args):
        return 0
    if _tui_supported():
        return _subs_preview(args, videos) or 0
    return _subs_classic(args, videos) or 0


def run_subs_download(args):
    """Standalone subtitle downloader: opens the same Total Commander-style browser
    as the renamer (navigate folders / drives / back, tag files), then downloads
    subtitles from OpenSubtitles (multi-language) via the 's' key."""
    run_rename_files(args, mode="subs")


# ============================================================================
# Re-time subtitles to a DIFFERENT video source (batch, auto-detect)
# ----------------------------------------------------------------------------
# In a folder there are pairs of the same episode from two sources: a SOURCE
# video that already has a correctly-timed .srt, and a TARGET video (different
# release, slightly different length) whose timing the subtitles don't match.
# This tool auto-detects which is which (by episode SxxExx + which video the srt
# belongs to), then re-times the srt to the target using the audio: it aligns
# the two videos' speech (VAD) and, as a cross-check, the subtitle timings vs the
# target speech - and applies the more reliable affine (offset + speed) transform.
# Output: <target-name>.<lang>.srt. At the end it reports what it made and by how
# much each was re-timed.
# ============================================================================


def _retime_is_lang(x):
    x = (x or "").lower()
    if len(x) not in (2, 3):
        return False
    if x in LANGUAGE_NAMES:
        return True
    c3 = _canon3(x)
    return bool(c3 and c3 != "und")


def _srt_lang_and_base(srt):
    """('cze', 'Show_S01E01_episode 1') from 'Show_S01E01_episode 1.cze.srt'."""
    stem = Path(srt).stem
    if "." in stem:
        base, last = stem.rsplit(".", 1)
        if _retime_is_lang(last):
            return last, base
    return None, stem


def _retime_scan(directory, recursive):
    """Auto-detects (source srt, its source video, target video, output) jobs."""
    videos = collect_videos(directory, recursive)
    srts = []
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, _dirs, files in walker:
        for f in files:
            if f.lower().endswith(".srt"):
                srts.append(os.path.join(root, f))
    ep_videos = {}
    stem_video = {}
    for v in videos:
        ep_videos.setdefault(_episode_key(Path(v).name), []).append(v)
        stem_video[Path(v).stem.lower()] = v
    source_stems = {_srt_lang_and_base(s)[1].lower() for s in srts}

    jobs = []
    seen_out = set()
    for srt in srts:
        lang, base = _srt_lang_and_base(srt)
        ep = _episode_key(Path(srt).name)
        if ep is None:
            continue
        src_video = stem_video.get(base.lower())
        for tv in ep_videos.get(ep, []):
            if Path(tv).stem.lower() in source_stems:
                continue   # this video is itself a source (has its own srt)
            out = str(Path(tv).with_name(Path(tv).stem + (f".{lang}" if lang else "") + ".srt"))
            if out in seen_out:
                continue
            seen_out.add(out)
            jobs.append(dict(source_srt=srt, source_video=src_video, target=tv,
                             out=out, lang=lang or "", ep=ep, exists=os.path.exists(out)))
    jobs.sort(key=lambda j: (j["ep"] or (0, 0), j["out"]))
    return jobs


def _video_audio_tracks(ffprobe_bin, video):
    """Lists audio tracks of a video as [{index(a:N), lang, codec, channels}]."""
    tracks = []
    try:
        for s in _ffprobe_streams(ffprobe_bin, video):
            if s.get("codec_type") == "audio":
                tags = s.get("tags", {}) or {}
                tracks.append(dict(index=len(tracks),
                                   lang=(_canon3(tags.get("language")) or "und"),
                                   codec=s.get("codec_name", "?"),
                                   channels=s.get("channels")))
    except Exception:
        pass
    return tracks


def _audio_desc(t):
    if not t:
        return "audio #0"
    ch = f" {t['channels']}ch" if t.get("channels") else ""
    return f"{_lang3_name(t['lang'])} {t['codec']}{ch} #{t['index'] + 1}"


def _pick_common_audio(src_tracks, tgt_tracks):
    """Chooses the SAME-language audio track in both videos (they contain the same
    audio, but the order can differ). Falls back to the first track of each."""
    src_by = {}
    for t in src_tracks:
        src_by.setdefault(t["lang"], t)
    tgt_by = {}
    for t in tgt_tracks:
        tgt_by.setdefault(t["lang"], t)
    common = [l for l in src_by if l != "und" and l in tgt_by]
    # prefer the language with the most channels (usually the main track)
    common.sort(key=lambda l: -(src_by[l].get("channels") or 0))
    if common:
        l = common[0]
        return src_by[l], tgt_by[l]
    return (src_tracks[0] if src_tracks else None), (tgt_tracks[0] if tgt_tracks else None)


def _video_vad(args, ffmpeg_bin, video, cache, audio_index=0):
    """Extracts the given audio track (a:N) and returns its speech (VAD) events.
    Cached per (video, audio_index); never raises (returns None on any problem)."""
    key = (video, audio_index)
    if key in cache:
        return cache[key]
    ev = None
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a.wav"
            extract_audio_wav(ffmpeg_bin, Path(video), audio_index, wav)
            samples, sr = read_wav_mono(wav)
        if samples is not None and len(samples):
            ev = detect_speech_events(samples, sr, energy_percentile=getattr(args, "vad_percentile", None) or 55.0)
    except SystemExit:
        ev = None
    except Exception:
        ev = None
    cache[key] = ev
    return ev


def _retime_local_offsets(src_vad, tgt_vad, win_s, step_s, max_shift):
    """Windowed local time offsets between the two speech (VAD) tracks: for each
    ~win_s window of source speech, find the shift that best matches the target
    speech nearby. Returns [(src_time, target_time, weight)] anchors across the
    whole episode - this is what reveals shifts in the MIDDLE / at several places."""
    if not src_vad or not tgt_vad:
        return []
    anchors = []
    tmax = src_vad[-1]["end"]
    c = win_s / 2.0
    while c < tmax:
        lo, hi = c - win_s / 2.0, c + win_s / 2.0
        sw = [e for e in src_vad if lo <= e["start"] < hi]
        if len(sw) >= 4:
            tw = [e for e in tgt_vad if (lo - max_shift) <= e["start"] <= (hi + max_shift)]
            if len(tw) >= 4:
                try:
                    shift = coarse_offset(tw, sw, max_shift=max_shift)
                    anchors.append((c, c + shift, len(sw)))
                except Exception:
                    pass
        c += step_s
    return anchors


def _retime_clean_and_breaks(anchors, jump_thresh=0.75, tol=1.0):
    """Rejects outlier anchors (robust local-median filter) and finds break points
    where the offset jumps by more than jump_thresh seconds (a mid-episode cut).
    Returns (clean_anchors, [(break_time, offset_delta), ...])."""
    import numpy as np
    if len(anchors) < 4:
        return anchors, []
    anchors = sorted(anchors, key=lambda a: a[0])
    offs = np.array([a[1] - a[0] for a in anchors])
    clean = []
    for i in range(len(anchors)):
        lo, hi = max(0, i - 4), min(len(anchors), i + 5)
        if abs(offs[i] - np.median(offs[lo:hi])) <= tol:
            clean.append(anchors[i])
    if len(clean) < 4:
        clean = anchors
    coffs = [c[1] - c[0] for c in clean]
    breaks = []
    for i in range(1, len(clean)):
        d = coffs[i] - coffs[i - 1]
        if abs(d) > jump_thresh:
            breaks.append(((clean[i - 1][0] + clean[i][0]) / 2.0, d))
    # merge breaks that are very close (same cut detected twice)
    merged = []
    for b in breaks:
        if merged and abs(b[0] - merged[-1][0]) < 8.0:
            merged[-1] = (merged[-1][0], merged[-1][1] + b[1])
        else:
            merged.append(list(b))
    return clean, [tuple(m) for m in merged]


def _retime_fit_line(anchors):
    import numpy as np
    if len(anchors) == 1:
        return 1.0, anchors[0][1] - anchors[0][0]
    xs = np.array([a[0] for a in anchors], dtype=float)
    ys = np.array([a[1] for a in anchors], dtype=float)
    if np.ptp(xs) < 1e-6:
        return 1.0, float(np.mean(ys - xs))
    sc, off = np.polyfit(xs, ys, 1)
    return float(sc), float(off)


def _retime_segments(clean, breaks):
    """Splits clean anchors at the break times and fits a line (scale+offset) per
    segment. Returns [(src_lo, src_hi, scale, offset)]."""
    bounds = sorted(b[0] for b in breaks)
    groups = [[] for _ in range(len(bounds) + 1)]
    for a in clean:
        gi = 0
        while gi < len(bounds) and a[0] >= bounds[gi]:
            gi += 1
        groups[gi].append(a)
    segs = []
    prev = -1e9
    for gi, g in enumerate(groups):
        hi = bounds[gi] if gi < len(bounds) else 1e9
        if g:
            sc, off = _retime_fit_line(g)
        elif segs:
            sc, off = segs[-1][2], segs[-1][3]
        else:
            sc, off = 1.0, 0.0
        segs.append((prev, hi, sc, off))
        prev = hi
    return segs


def _retime_apply_segments(events, segs):
    def mp(t):
        for lo, hi, sc, off in segs:
            if lo <= t < hi:
                return sc * t + off
        lo, hi, sc, off = segs[-1]
        return sc * t + off
    out = []
    last = -1e9
    for e in events:
        st = mp(e["start"])
        en = mp(e["end"])
        if st < last:
            st = last
        if en <= st:
            en = st + 0.5
        ne = dict(e)
        ne["start"], ne["end"] = st, en
        out.append(ne)
        last = st
    return out


def _print_progress(i, n, prefix=""):
    """One-line \\r progress bar (normal terminal mode)."""
    if n <= 0:
        return
    frac = min(1.0, i / n)
    bw = 28
    fill = int(bw * frac)
    sys.stdout.write(f"\r  {prefix}[{'#' * fill}{'-' * (bw - fill)}] {i}/{n} ({frac * 100:4.0f}%)")
    sys.stdout.flush()
    if i >= n:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _video_analysis(args, ffmpeg_bin, video, cache, audio_index=0):
    """Extracts one audio track ONCE and returns dict(vad, env, hz, dur): VAD speech
    events (coarse structure) AND a full-track RMS energy envelope at 50 Hz (for
    precise per-timestamp waveform correlation). Cached per (video, audio_index)."""
    import numpy as np
    key = (video, audio_index)
    if key in cache:
        return cache[key]
    res = None
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a.wav"
            extract_audio_wav(ffmpeg_bin, Path(video), audio_index, wav)
            samples, sr = read_wav_mono(wav)
        if samples is not None and len(samples):
            try:
                vad = detect_speech_events(samples, sr, energy_percentile=getattr(args, "vad_percentile", None) or 55.0)
            except (SystemExit, Exception):
                vad = []
            hz = 50
            fl = max(1, int(sr / hz))
            n = len(samples) // fl
            s = np.asarray(samples[:n * fl], dtype=np.float64).reshape(n, fl)
            env = np.sqrt(np.mean(s * s, axis=1))
            env = ((env - env.mean()) / (env.std() or 1.0)).astype(np.float32)
            res = dict(vad=vad, env=env, hz=float(hz), dur=len(samples) / sr)
    except (SystemExit, Exception):
        res = None
    cache[key] = res
    return res


def _ncc_locate(src_env, tgt_env, hz, t_src, t_guess, win_s=4.0, band_s=10.0):
    """Finds where the SOURCE audio window around t_src appears in the TARGET audio
    near t_guess, using normalized cross-correlation. Returns (t_target, confidence
    in -1..1) or None. This is the real per-timestamp waveform check."""
    import numpy as np
    w = int(win_s * hz)
    b = int(band_s * hz)
    si = int(t_src * hz)
    if w < 4 or si - w // 2 < 0 or si + w // 2 > len(src_env):
        return None
    seg = src_env[si - w // 2:si + w // 2].astype(np.float64)
    seg = seg - seg.mean()
    sn = float(np.linalg.norm(seg))
    if sn < 1e-6:
        return None
    ci = int(t_guess * hz)
    lo = max(0, ci - w // 2 - b)
    hi = min(len(tgt_env), ci + w // 2 + b)
    r = tgt_env[lo:hi].astype(np.float64)
    L = len(seg)
    if len(r) < L:
        return None
    c1 = np.concatenate([[0.0], np.cumsum(r)])
    c2 = np.concatenate([[0.0], np.cumsum(r * r)])
    wsum = c1[L:] - c1[:-L]
    wss = c2[L:] - c2[:-L]
    wnorm = np.sqrt(np.maximum(wss - (wsum * wsum) / L, 1e-9))
    num = np.correlate(r, seg, mode="valid")     # seg is zero-mean -> already the covariance
    ncc = num / (wnorm * sn)
    k = int(np.argmax(ncc))
    return (lo + k + w // 2) / hz, float(ncc[k])


def _segs_map(segs):
    def f(t):
        for lo, hi, sc, off in segs:
            if lo <= t < hi:
                return sc * t + off
        lo, hi, sc, off = segs[-1]
        return sc * t + off
    return f


def _retime_map_from_anchors(anchors, guess):
    """Monotonic map source_time -> target_time built from confident per-line anchors,
    with linear extrapolation at the ends."""
    import numpy as np
    anchors = sorted(anchors)
    xs = [anchors[0][0]]
    ys = [anchors[0][1]]
    for x, y in anchors[1:]:
        if x > xs[-1] + 1e-6:
            xs.append(x)
            ys.append(max(y, ys[-1]))
    xs = np.array(xs)
    ys = np.array(ys)

    def f(t):
        if t <= xs[0]:
            if len(xs) > 1:
                sl = (ys[1] - ys[0]) / (xs[1] - xs[0])
                return float(ys[0] + sl * (t - xs[0]))
            return float(guess(t))
        if t >= xs[-1]:
            if len(xs) > 1:
                sl = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
                return float(ys[-1] + sl * (t - xs[-1]))
            return float(guess(t))
        return float(np.interp(t, xs, ys))
    return f


def _retime_apply_mapfn(events, mapf):
    out = []
    last = -1e9
    for e in events:
        st = mapf(e["start"])
        en = mapf(e["end"])
        if st < last:
            st = last
        if en <= st:
            en = st + 0.4
        ne = dict(e)
        ne["start"], ne["end"] = st, en
        out.append(ne)
        last = st
    return out


def _retime_common_langs(ffprobe_bin, job):
    """{lang: (src_track, tgt_track)} for audio languages present in BOTH videos of
    the pair (the same content, so aligning like-for-like is reliable)."""
    tgt = _video_audio_tracks(ffprobe_bin, job["target"])
    src = _video_audio_tracks(ffprobe_bin, job["source_video"]) if job["source_video"] else []
    tgt_by, src_by = {}, {}
    for t in tgt:
        tgt_by.setdefault(t["lang"], t)
    for t in src:
        src_by.setdefault(t["lang"], t)
    return {lg: (src_by[lg], tgt_by[lg]) for lg in src_by if lg != "und" and lg in tgt_by}


def _retime_pick_audio_langs(lang_count):
    """Checkbox picker for the detected common audio languages (all pre-selected).
    Returns a list of language codes, or None on cancel."""
    items = sorted(lang_count.items(), key=lambda x: (-x[1], x[0]))
    sel = set(lg for lg, _c in items)
    pos = 0
    top = 0
    with _RawMode():
        _fb_enter_screen()
        try:
            while True:
                cols, rows = _fb_termsize()
                head = [
                    f"{Fore.MAGENTA}{Style.BRIGHT}=== Audio tracks for the analysis ==={Style.RESET_ALL}",
                    f"{Style.DIM}Pick which common-language tracks to analyse. More tracks = higher "
                    f"reliability (cross-checked), but slower.{Style.RESET_ALL}", "",
                ]
                foot = ["", f"{Style.DIM}\u2191\u2193 move | Space = toggle | Enter = confirm | Esc = cancel{Style.RESET_ALL}"]
                avail = max(3, rows - len(head) - len(foot))
                pos = max(0, min(pos, len(items) - 1))
                if pos < top:
                    top = pos
                elif pos >= top + avail:
                    top = pos - avail + 1
                top = max(0, top)
                body = []
                for i, (lg, cnt) in enumerate(items[top:top + avail], start=top):
                    box = "[x]" if lg in sel else "[ ]"
                    label = f"{box} {_lang3_name(lg)} ({lg})   {Style.DIM}- in {cnt} pair(s){Style.RESET_ALL}"
                    if i == pos:
                        body.append(f"{Fore.GREEN}{Style.BRIGHT}\u203a {label}{Style.RESET_ALL}")
                    else:
                        body.append(f"  {Fore.CYAN if lg in sel else ''}{label}{Style.RESET_ALL}")
                _fb_write_frame(head + body + foot)
                k = _read_key()
                if k == "esc":
                    return None
                elif k == "enter":
                    return sorted(sel)
                elif k == ("char", " "):
                    lg = items[pos][0]
                    sel.discard(lg) if lg in sel else sel.add(lg)
                elif k == "up":
                    pos = (pos - 1) % len(items)
                elif k == "down":
                    pos = (pos + 1) % len(items)
                elif k == "home":
                    pos = 0
                elif k == "end":
                    pos = len(items) - 1
        finally:
            _fb_leave_screen()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()


def _retime_perline(src_events, tracks, guess, ep_label):
    """Per-timestamp audio verification across one or MORE audio tracks. For every
    source subtitle time, each track votes with a normalized cross-correlation match;
    the highest-confidence hit is taken (best-of-N), which fills gaps where one track
    is silent. Returns (anchors[(src_t,tgt_t)], confs, per_lang_offsets)."""
    anchors, confs = [], []
    per_lang = {t[0]: [] for t in tracks}
    n = len(src_events)
    multi = len(tracks) > 1
    for i, e in enumerate(src_events):
        best = None
        for (lg, se, te, hz) in tracks:
            r = _ncc_locate(se, te, hz, e["start"], guess(e["start"]), win_s=4.0, band_s=10.0)
            if not r:
                continue
            if r[1] >= 0.30:
                per_lang[lg].append(r[0] - e["start"])
            if best is None or r[1] > best[0]:
                best = (r[1], r[0], lg)
        if best and best[0] >= 0.30:
            anchors.append((e["start"], best[1]))
            confs.append(best[0])
        if (i % 5 == 0) or (i + 1 == n):
            _print_progress(i + 1, n, prefix=f"{ep_label} checking timestamps "
                            f"({len(tracks)} track{'s' if multi else ''}) ")
    return anchors, confs, per_lang


def _retime_one(args, ffmpeg_bin, job, vad_cache):
    """Re-times the source srt to the target video by REAL per-timestamp audio
    verification across the selected common audio tracks (best-of-N per timestamp +
    cross-check between languages). A coarse VAD alignment aims the search. Falls back
    to a global affine when no usable audio is available."""
    import numpy as np
    src_events = parse_srt(Path(job["source_srt"]))
    if not src_events:
        return dict(ok=False, msg="empty or unreadable source .srt")

    ffprobe_bin = _find_ffprobe(ffmpeg_bin)
    langs = job.get("langs")
    if langs:
        pairs = [(lg, langs[lg][0], langs[lg][1]) for lg in sorted(langs)]
    else:
        tgt_tracks = _video_audio_tracks(ffprobe_bin, job["target"])
        src_tracks = _video_audio_tracks(ffprobe_bin, job["source_video"]) if job["source_video"] else []
        sa_t, ta_t = _pick_common_audio(src_tracks, tgt_tracks)
        pairs = [((ta_t["lang"] if ta_t else "und"), sa_t, ta_t)] if (sa_t or ta_t) else [("und", None, None)]

    # analyse each track pair once (cached)
    analyses = []   # (lang, src_an, tgt_an, src_track, tgt_track)
    for (lg, st, tt) in pairs:
        log_info(f"    analysing {_lang3_name(lg)} audio: target [{_audio_desc(tt)}]"
                 + (f" + source [{_audio_desc(st)}]" if st else "") + " ...")
        ta_an = _video_analysis(args, ffmpeg_bin, job["target"], vad_cache, tt["index"] if tt else 0)
        sa_an = (_video_analysis(args, ffmpeg_bin, job["source_video"], vad_cache, st["index"])
                 if (job["source_video"] and st) else None)
        analyses.append((lg, sa_an, ta_an, st, tt))

    tgt_an0 = next((a[2] for a in analyses if a[2] and a[2].get("vad")), None)
    if not tgt_an0:
        return dict(ok=False, msg="could not analyse the target audio")

    # coarse guess (aim only) from the first pair that has source speech
    guess, guess_desc = None, ""
    for (lg, sa_an, ta_an, st, tt) in analyses:
        if sa_an and sa_an.get("vad") and ta_an and ta_an.get("vad"):
            an = _retime_local_offsets(sa_an["vad"], ta_an["vad"], 60.0, 25.0, args.max_shift)
            clean, breaks = _retime_clean_and_breaks(an)
            if len(clean) >= 4 and breaks:
                guess = _segs_map(_retime_segments(clean, breaks))
                guess_desc = f"{len(breaks) + 1} segments"
            else:
                sh = coarse_offset(ta_an["vad"], sa_an["vad"], max_shift=args.max_shift)
                sc, off, _n = refine_affine(ta_an["vad"], sa_an["vad"], sh, tolerance=args.tolerance)
                guess = (lambda t, sc=sc, off=off: sc * t + off)
                guess_desc = "uniform"
            break
    if guess is None:
        sh = coarse_offset(tgt_an0["vad"], src_events, max_shift=args.max_shift)
        sc, off, _n = refine_affine(tgt_an0["vad"], src_events, sh, tolerance=args.tolerance)
        guess = (lambda t, sc=sc, off=off: sc * t + off)
        guess_desc = "uniform (subs)"

    # per-line multi-track verification
    tracks = [(lg, sa_an["env"], ta_an["env"], ta_an["hz"])
              for (lg, sa_an, ta_an, st, tt) in analyses
              if sa_an and ta_an and sa_an.get("env") is not None and ta_an.get("env") is not None
              and len(sa_an["env"]) and len(ta_an["env"])]
    if tracks:
        anchors, confs, per_lang = _retime_perline(src_events, tracks, guess, _fmt_ep(job["ep"]))
        if len(anchors) >= max(3, len(src_events) // 8):
            mapf = _retime_map_from_anchors(anchors, guess)
            corrected = _retime_apply_mapfn(src_events, mapf)
            Path(job["out"]).parent.mkdir(parents=True, exist_ok=True)
            write_srt(corrected, Path(job["out"]))
            offs = [b - a for a, b in anchors]
            meds = {lg: float(np.median(o)) for lg, o in per_lang.items() if len(o) >= 3}
            spread = (max(meds.values()) - min(meds.values())) if len(meds) > 1 else None
            used = []
            for (lg, sa_an, ta_an, st, tt) in analyses:
                if lg in {t[0] for t in tracks}:
                    used.append((lg, st, tt, len(per_lang.get(lg, []))))
            return dict(ok=True, method="audio per-line", perline=True,
                        verified=len(anchors), total=len(src_events), count=len(corrected),
                        conf=(float(np.median(confs)) if confs else 0.0),
                        off_lo=min(offs), off_hi=max(offs), guess=guess_desc,
                        used=used, spread=spread, span=(src_events[0]["start"], src_events[-1]["end"]))
        log_warn("    per-line audio match was weak - falling back to global alignment.")

    # fallback: VAD-based global affine (offset + speed)
    tgt_vad = tgt_an0["vad"]
    src_an0 = next((a[1] for a in analyses if a[1] and a[1].get("vad")), None)
    cands = []
    if src_an0 and src_an0.get("vad"):
        sv = src_an0["vad"]
        sh = coarse_offset(tgt_vad, sv, max_shift=args.max_shift)
        sc, off, nn = refine_affine(tgt_vad, sv, sh, tolerance=args.tolerance)
        cands.append(("audio<->audio", sc, off, nn, len(sv)))
    sh2 = coarse_offset(tgt_vad, src_events, max_shift=args.max_shift)
    sc2, off2, n2 = refine_affine(tgt_vad, src_events, sh2, tolerance=args.tolerance)
    cands.append(("subs<->audio", sc2, off2, n2, len(src_events)))

    def _sane(c):
        return c is not None and c[3] >= 6 and 0.5 < c[1] < 2.0
    aa = next((c for c in cands if c[0] == "audio<->audio"), None)
    sa = next((c for c in cands if c[0] == "subs<->audio"), None)
    best = aa if _sane(aa) else (sa if _sane(sa) else max(cands, key=lambda c: c[3]))
    name, scale, offset, nmatched, _tot = best
    corrected = apply_transform(src_events, scale, offset)
    Path(job["out"]).parent.mkdir(parents=True, exist_ok=True)
    write_srt(corrected, Path(job["out"]))
    return dict(ok=True, scale=scale, offset=offset, method=name, nmatched=nmatched,
                count=len(corrected), span=(src_events[0]["start"], src_events[-1]["end"]),
                used=[(pairs[0][0], pairs[0][1], pairs[0][2], nmatched)])


def run_retime_batch(args):
    """Scans a folder, auto-detects source(video+srt)/target(video) pairs of the
    same episode and re-times each subtitle file to its target video by analysing
    the audio of both. Writes <target>.<lang>.srt and reports what it changed."""
    print(f"{Fore.MAGENTA}=== Re-time subtitles to a different video source (batch) ==={Style.RESET_ALL}")
    directory = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or "."
    log_info(f"Working directory: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Search subdirectories too?", default_no=True)

    jobs = _retime_scan(directory, recursive)
    if not jobs:
        log_warn("No source(video+srt) / target(video) pairs detected in this folder.")
        log_info("Expected: one video with a matching .srt, plus another video of the SAME episode (SxxExx).")
        return

    log_info(f"Detected {len(jobs)} subtitle(s) to re-time:")
    for j in jobs:
        print(f"  {Fore.CYAN}{_fmt_ep(j['ep'])}{Style.RESET_ALL}  {Path(j['source_srt']).name}")
        print(f"      source video: "
              + (Path(j['source_video']).name if j['source_video'] else f"{Style.DIM}(none - will use the srt itself){Style.RESET_ALL}"))
        print(f"      -> target:    {Path(j['target']).name}")
        print(f"      => {Fore.GREEN}{Path(j['out']).name}{Style.RESET_ALL}"
              + (f"   {Fore.YELLOW}[exists]{Style.RESET_ALL}" if j['exists'] else ""))

    if any(j["exists"] for j in jobs):
        if not ask_yes_no("Some outputs already exist - overwrite them?", default_no=True):
            jobs = [j for j in jobs if not j["exists"]]
    if not jobs:
        log_info("Nothing to do.")
        return

    ffmpeg_bin = ensure_ffmpeg(directory, allow_download=not getattr(args, "no_ffmpeg_download", False))
    if not ffmpeg_bin:
        log_warn("ffmpeg is required (to read audio for the analysis) and was not found.")
        return

    # detect the common audio languages across all pairs, let the user choose which
    ffprobe_bin = _find_ffprobe(ffmpeg_bin)
    lang_count = {}
    job_common = {}
    for j in jobs:
        cl = _retime_common_langs(ffprobe_bin, j)
        job_common[id(j)] = cl
        for lg in cl:
            lang_count[lg] = lang_count.get(lg, 0) + 1
    if lang_count:
        log_info("Common audio languages found in source+target pairs: "
                 + ", ".join(f"{_lang3_name(lg)} ({lg}) x{c}" for lg, c in sorted(lang_count.items(), key=lambda x: -x[1])))
        if _tui_supported():
            picked = _retime_pick_audio_langs(lang_count)
            if picked is None:
                log_info("Cancelled.")
                return
            selected = set(picked) or set(lang_count)
        else:
            selected = set(lang_count)   # non-TTY: use all
        for j in jobs:
            j["langs"] = {lg: tp for lg, tp in job_common[id(j)].items() if lg in selected}
    else:
        log_warn("No common audio language detected between source and target - will use "
                 "the first audio track / subtitle timing as a fallback.")

    args.method = getattr(args, "method", None) or "auto"
    args.max_shift = getattr(args, "max_shift", None) or 240.0   # tolerate extra intro/recap at the start
    args.tolerance = getattr(args, "tolerance", None) or 1.5
    args.vad_percentile = getattr(args, "vad_percentile", None) or 55.0

    if not ask_yes_no(f"Re-time {len(jobs)} subtitle file(s) now? (reads audio from each video - can take a while)",
                      default_no=False):
        log_info("Cancelled.")
        return

    vad_cache = {}
    results = []
    for i, j in enumerate(jobs, 1):
        log_info(f"[{i}/{len(jobs)}] {_fmt_ep(j['ep'])}: {Path(j['source_srt']).name} -> {Path(j['target']).name}")
        try:
            r = _retime_one(args, ffmpeg_bin, j, vad_cache)
        except Exception as e:
            r = dict(ok=False, msg=str(e))
        results.append((j, r))
        if r.get("ok"):
            log_done(f"    saved {Path(j['out']).name}")
        else:
            log_warn(f"    failed: {r.get('msg')}")

    print()
    log_info("=== Re-time report ===")
    okc = 0
    for j, r in results:
        if r.get("ok"):
            okc += 1
            au = r.get("audio") or {}
            atxt = ""
            used = r.get("used")
            if used:
                parts = []
                for tup in used:
                    lg, st, tt = tup[0], tup[1], tup[2]
                    cnt = tup[3] if len(tup) > 3 else None
                    parts.append(f"{_lang3_name(lg)} [{_audio_desc(st)} <-> {_audio_desc(tt)}]"
                                 + (f" ({cnt} hits)" if cnt is not None else ""))
                atxt = "\n      audio tracks: " + "; ".join(parts)
            elif au.get("src") or au.get("tgt"):
                atxt = f"\n      audio: source [{_audio_desc(au.get('src'))}] <-> target [{_audio_desc(au.get('tgt'))}]"
            if r.get("perline"):
                rng = (f"{r['off_lo']:+.1f}s" if abs(r['off_hi'] - r['off_lo']) < 0.2
                       else f"{r['off_lo']:+.1f}..{r['off_hi']:+.1f}s")
                xcheck = ""
                if r.get("spread") is not None:
                    xcheck = (f" | {Fore.YELLOW}languages DISAGREE by {r['spread']:.2f}s{Style.RESET_ALL}"
                              if r["spread"] > 1.0 else f" | languages agree within {r['spread']:.2f}s")
                print(f"  {Fore.GREEN}{Path(j['out']).name}{Style.RESET_ALL}: {r['count']} lines | "
                      f"method {Fore.CYAN}audio per-line{Style.RESET_ALL} ({r['guess']} guess) | "
                      f"verified {r['verified']}/{r['total']} timestamps by audio "
                      f"(median match {r['conf']:.2f}) | offset {rng}{xcheck}{atxt}")
                continue
            if r.get("piecewise"):
                brk = ", ".join(f"{int(b[0]) // 60:02d}:{int(b[0]) % 60:02d} ({b[1]:+.1f}s)"
                                for b in r.get("breaks", []))
                print(f"  {Fore.GREEN}{Path(j['out']).name}{Style.RESET_ALL}: {r['count']} lines | "
                      f"method {Fore.YELLOW}PIECEWISE{Style.RESET_ALL} - {r['segments']} segments, "
                      f"shift changes at: {brk}{atxt}")
                continue
            span = max(0.001, r["span"][1] - r["span"][0])
            drift = (r["scale"] - 1.0) * span
            if r.get("agree") is not None:
                extra = (f" | {Fore.YELLOW}methods DISAGREE by {r['agree']:.2f}s - check this one{Style.RESET_ALL}"
                         if r.get("disagree") else f" | methods agree within {r['agree']:.2f}s")
            else:
                extra = f" | {Style.DIM}(no source video - subtitle match only){Style.RESET_ALL}"
            print(f"  {Fore.GREEN}{Path(j['out']).name}{Style.RESET_ALL}: {r['count']} lines | "
                  f"offset {r['offset']:+.2f}s | speed {r['scale']:.4f}x | "
                  f"drift {drift:+.1f}s over the episode | method {r['method']} ({r['nmatched']} anchors){extra}{atxt}")
        else:
            print(f"  {Fore.RED}FAILED{Style.RESET_ALL} {Path(j['target']).name}: {r.get('msg')}")
    print()
    log_done(f"Done: {okc}/{len(results)} subtitle file(s) re-timed to their target video.")


_INTERACTIVE_COMMANDS = {
    "auto": ("Synchronize one subtitle file", "run_auto_single"),
    "auto-all": ("Synchronize the whole folder", "run_auto_all"),
    "translate-subs": ("Translate subtitles", "run_translate_subs"),
    "extract-subs": ("Extract subtitles from videos", "run_extract_subs"),
    "merge-pro": ("Replace machine translation with pro text", "run_transplant"),
    "resync-pro": ("Re-time pro subtitles to my timing", "run_resync_pro"),
    "import-subs": ("Insert subtitles into videos", "run_import_subs"),
    "remove-tracks": ("Remove tracks from MKV", "run_remove_tracks"),
    "set-default": ("Set the default track", "run_set_default"),
    "rename-subs": ("Rename subtitles", "run_rename_subs"),
    "subs-download": ("Download subtitles (OpenSubtitles)", "run_subs_download"),
    "retime-subs": ("Re-time subtitles to a different video source", "run_retime_batch"),
    "extract-audio": ("Extract audio track from videos", "run_extract_audio"),
    "import-audio": ("Insert (mux) external audio into videos", "run_import_audio"),
    "convert-audio": ("Convert audio (e.g. to AC-3)", "run_convert_audio"),
    "rename-files": ("Intelligent file rename", "run_rename_files"),
    "video-browser": ("Video browser / inspector", "run_video_browser"),
}


def dispatch_interactive_command(cmd, args):
    """Runs an interactive mode by command name (shared for bare-run,
    --load i menu Presety)."""
    fn = {
        "auto": run_auto_single, "auto-all": run_auto_all,
        "translate-subs": run_translate_subs, "extract-subs": run_extract_subs,
        "merge-pro": run_transplant, "resync-pro": run_resync_pro,
        "import-subs": run_import_subs, "remove-tracks": run_remove_tracks,
        "set-default": run_set_default, "rename-subs": run_rename_subs, "subs-download": run_subs_download, "retime-subs": run_retime_batch,
        "extract-audio": run_extract_audio, "import-audio": run_import_audio,
        "convert-audio": run_convert_audio, "rename-files": run_rename_files,
        "video-browser": run_video_browser,
    }.get(cmd)
    if not fn:
        die(f"Unknown command in the preset: {cmd}")
    fn(args)


def _create_new_preset(args):
    """Lets you pick a mode, go through the wizard and save it as a named preset."""
    cmds = list(_INTERACTIVE_COMMANDS.items())
    labels = [f"{desc}" for _cmd, (desc, _fn) in cmds]
    i = ask_pick("Which mode to save as a preset?", labels, default=1, allow_back=True)
    if i is None:
        return
    cmd = cmds[i][0]
    dry_ok = {"translate-subs", "extract-subs", "rename-subs", "import-subs", "merge-pro", "resync-pro"}
    if cmd not in dry_ok:
        log_info("This mode needs to see real tracks/files (it selects from them), so it "
                 "can't be set up without them.")
        log_info("Create the preset like this: run this mode normally in the folder with videos/subtitles and at "
                 "the very end, at the save-preset question, enter a NAME - it is saved into the Presets library.")
        return
    name = ask_text("Preset name (how it will be called in the menu)", "") or cmds[i][1][0]
    if name in load_store().get("presets", {}) and not ask_yes_no(
            f"Preset '{name}' already exists - overwrite?", default_no=True):
        return
    log_info(f"Go through the wizard - the choices are just collected and saved as preset '{name}'. "
             "You don't need to be in the folder with videos/subtitles, nothing runs now.")
    preset_begin_save(cmd, key=name, label=name, dryrun=True)
    run_with_back(lambda a: dispatch_interactive_command(cmd, a), args)


def _delete_preset(presets):
    if not presets:
        log_warn("No presets to delete.")
        return
    labels = [f"{lbl}  ({cmd})" for lbl, _k, cmd in presets] + ["back"]
    i = ask_pick("Which preset to delete?", labels, default=len(presets), allow_back=True)
    if i is None or i >= len(presets):
        return
    lbl, key, _cmd = presets[i]
    if ask_yes_no(f"Really delete preset '{lbl}'?", default_no=True):
        delete_preset(key)
        log_done(f"Preset '{lbl}' deleted.")


def run_presets_menu(args):
    """Presets menu: run a saved configuration with a single choice, or create/
    delete a preset. Esc or 'Back' returns to the main menu."""
    while True:
        presets = list_named_presets()
        labels = [f"> {lbl}  ({cmd})" for lbl, _k, cmd in presets]
        labels += ["+ Create a new preset", "Delete a preset", "Back to the main menu"]
        header = [
            f"{Fore.MAGENTA}=== Presets (saved configurations) ==={Style.RESET_ALL}",
            (f"{Fore.CYAN}Pick a preset - it runs right away.{Style.RESET_ALL}" if presets
             else f"{Fore.CYAN}No presets yet - create your first.{Style.RESET_ALL}"),
            "",
        ]
        i = ask_pick("Presety:", labels, default=0 if presets else len(presets),
                     allow_back=True, header=header)
        if i is None:                     # Esc = back to the main menu
            return
        if i < len(presets):
            lbl, key, cmd = presets[i]
            data = get_preset(key)
            if not data:
                log_warn("Cannot load the preset (corrupted?).")
                continue
            log_info(f"Running preset '{lbl}' ({cmd})...")
            preset_begin_load(data.get("answers", []))
            dispatch_interactive_command(cmd, args)
            return
        elif i == len(presets):
            try:
                _create_new_preset(args)
            except WizardBack:
                pass
            continue                      # back to the presets menu
        elif i == len(presets) + 1:
            _delete_preset(presets)       # the loop continues (back to the presets menu)
        else:                             # "Back to the main menu"
            return


def _wizard_action_specs():
    """Returns the action registry: key -> dict(label, help, kind, run, flag, preset).
    kind: 'wizard' (flag+preset+back), 'readability', 'config', 'presets', 'direct'."""
    return {
        "sync-one": dict(
            label="Synchronize ONE subtitle file (to a video or other subtitles)",
            help="Pick a subtitle file with bad timing and a source of correct timing (a subtitle "
                 "track from a video, or a second .srt). Works between two .srt without a video too.",
            kind="wizard", run=run_auto_single, flag="auto", preset="auto"),
        "sync-folder": dict(
            label="Synchronize a WHOLE FOLDER (batch of videos + their subtitles)",
            help="For each video in the directory it computes the timing for its matching subtitles.",
            kind="wizard", run=run_auto_all, flag="auto_all", preset="auto-all"),
        "translate": dict(
            label="Translate subtitles into another language and save as .srt",
            help="Extracts a track from a video or takes an existing .srt, translates into the target "
                 "language (Gemini/Google/DeepL/Claude/Argos) + proofreading, saves <name>.<lang>.srt.",
            kind="wizard", run=run_translate_subs, flag="translate_subs", preset="translate-subs"),
        "extract-subs": dict(
            label="Extract subtitles from videos into .srt - pick which tracks",
            help="From each video it extracts subtitle tracks into .srt. You pick which (by language, "
                 "specific tracks, or all text ones). Image subtitles (PGS/VobSub) can't go to text.",
            kind="wizard", run=run_extract_subs, flag="extract_subs", preset="extract-subs"),
        "merge-pro": dict(
            label="Replace a MACHINE translation with a PROFESSIONAL one (by content, timing stays)",
            help="You have machine/AI subtitles with good timing and a professional translation of the "
                 "same show elsewhere. By content it inserts the pro text but keeps your timing.",
            kind="wizard", run=run_transplant, flag="merge_pro", preset="merge-pro"),
        "resync-pro": dict(
            label="Re-time PROFESSIONAL subtitles to MY timing (100% pro text)",
            help="Takes the whole professional translation from another directory and re-times it to "
                 "your timing. Result = 100% professional text with correct timing.",
            kind="wizard", run=run_resync_pro, flag="resync_pro", preset="resync-pro"),
        "import-subs": dict(
            label="Insert (mux) subtitles from a folder into videos (by SxxExx)",
            help="Muxes .srt/.ass from a folder into videos (paired via SxxExx) using MKVToolNix. Sets "
                 "language, track name, forced and optionally the default track. Output is always MKV.",
            kind="wizard", run=run_import_subs, flag="import_subs", preset="import-subs"),
        "rename-subs": dict(
            label="Rename subtitles by video names (SxxExx)",
            help="Renames .srt by the name of the matching video (paired via SxxExx), keeps the "
                 "language/forced suffix. Shows the plan first.",
            kind="wizard", run=run_rename_subs, flag="rename_subs", preset="rename-subs"),
        "retime-subs": dict(
            label="Re-time subtitles to a DIFFERENT video source (auto-detect, batch)",
            help="Same episode from two sources: one video has correct .srt, the other (different length) does not. "
                 "Auto-detects source/target by SxxExx, analyses BOTH videos' audio (speech VAD) plus the subtitle "
                 "timing, and writes <target>.<lang>.srt re-timed to the new video. Reports the offset/speed per file.",
            kind="wizard", run=run_retime_batch, flag="retime_subs", preset="retime-subs"),
        "subs-download": dict(
            label="Download subtitles from OpenSubtitles (multi-language)",
            help="Recognizes the show/episode from file names and downloads subtitles for one or MORE "
                 "languages from OpenSubtitles, saved next to each video as name.<lang>.srt. Needs a free "
                 "OpenSubtitles API key (and an account for downloading) - set via --config.",
            kind="browser", run=run_subs_download, flag="subs_download", preset="subs-download"),
        "fix-readability": dict(
            label="Just fix subtitle READABILITY (extend too-short ones)",
            help="Only extends too-briefly displayed subtitles into free space. When there are no .srt "
                 "in the folder, it offers extraction from videos.",
            kind="readability", run=run_fix_readability, flag="fix_readability", preset=None),
        "p1": dict(
            label="[preset] Extract CZECH subtitles + readability (--p1)",
            help="Fixed preset: extracts the Czech subtitle track from every video and applies the "
                 "readability fix (9 chars/s, min 2.5s). Asks only if several Czech tracks exist.",
            kind="direct", run=run_p1, flag=None, preset=None),
        "p2": dict(
            label="[preset] Translate ENGLISH -> CZECH + readability (--p2)",
            help="Fixed preset: extracts the English track, translates to Czech (google, free) + rules "
                 "proofreading + readability fix, saves <video>.cs.srt. Asks only if several EN tracks exist.",
            kind="direct", run=run_p2, flag=None, preset=None),
        "extract-audio": dict(
            label="Extract an audio track from videos (stream copy)",
            help="Extracts an audio track (by language) from each video into a standalone audio file "
                 "via ffmpeg stream copy (no re-encoding). Asks only if several matching tracks exist.",
            kind="wizard", run=run_extract_audio, flag="extract_audio", preset="extract-audio"),
        "import-audio": dict(
            label="Insert (mux) external audio into videos (by SxxExx)",
            help="Muxes an external audio file (paired by SxxExx) into each video as the default audio, "
                 "sets a chosen subtitle language as default and fills track names. Output <name>_merged.mkv.",
            kind="wizard", run=run_import_audio, flag="import_audio", preset="import-audio"),
        "convert-audio": dict(
            label="Convert audio (re-encode, e.g. to AC-3)",
            help="Re-encodes all audio in each MKV to a chosen codec (AC-3/E-AC-3/AAC) at a chosen "
                 "bitrate, copying video and subtitles. Output <name>_<codec>.mkv (or overwrite).",
            kind="wizard", run=run_convert_audio, flag="convert_audio", preset="convert-audio"),
        "remove-tracks": dict(
            label="Remove audio/subtitle tracks from MKV (by language)",
            help="Shows the languages of audio/subtitle tracks and you pick which to drop (fast remux). "
                 "Originals can be kept (the 'trimmed' subfolder).",
            kind="wizard", run=run_remove_tracks, flag="remove_tracks", preset="remove-tracks"),
        "set-default": dict(
            label="Set the DEFAULT track by language",
            help="Clears old default flags and sets the default audio/subtitles by language. MKV in "
                 "place (mkvpropedit), MP4 via remux (ffmpeg).",
            kind="wizard", run=run_set_default, flag="set_default", preset="set-default"),
        "rename-files": dict(
            label="Intelligent file rename (bulk, preview first)",
            help="Unifies file names by a glob pattern: zero-pads numbers, fills missing common words, "
                 "normalizes case, strips emoji/illegal characters, auto-detects series. Preview then apply.",
            kind="browser", run=run_rename_files, flag="rename_files", preset="rename-files"),
        "video-browser": dict(
            label="Video browser / inspector (Total Commander style)",
            help="Browse folders/drives; press Enter on a video for a full report and every operation: "
                 "extract/add/rename/remove tracks, set defaults, convert audio, MP4<->MKV, chapters, "
                 "attachments, thumbnails.",
            kind="browser", run=run_video_browser, flag=None, preset=None),
        "presets": dict(
            label="Presets - run a saved configuration with a single choice (or create one)",
            help="Saved wizard configurations named by you. Pick one and it runs with the saved choices. "
                 "You can create and delete presets here too.",
            kind="presets", run=None, flag=None, preset=None),
        "config": dict(
            label="Set API keys and default options (video_tool.config.json)",
            help="Saves API keys and default options into video_tool.config.json (loaded automatically).",
            kind="config", run=run_config, flag=None, preset=None),
        "test-api": dict(
            label="Test the AI API (Anthropic/OpenAI) - verify the key and model",
            help="Sends a trivial query and prints the exact response/error (debugging e.g. HTTP 400).",
            kind="direct", run=run_test_api, flag=None, preset=None),
    }


_WIZARD_CATEGORIES = [
    ("Subtitles", "Sync, translate, extract, transplant, rename, readability",
     ["sync-one", "sync-folder", "translate", "extract-subs", "merge-pro",
      "resync-pro", "import-subs", "rename-subs", "subs-download", "retime-subs", "fix-readability", "p1", "p2"]),
    ("Audio", "Extract, mux and convert audio tracks",
     ["extract-audio", "import-audio", "convert-audio"]),
    ("Video / tracks", "Remove tracks, set the default track",
     ["remove-tracks", "set-default", "video-browser"]),
    ("Files", "Intelligent bulk file renaming",
     ["rename-files"]),
    ("Presets & settings", "Saved presets, API keys/config, API test",
     ["presets", "config", "test-api"]),
]

_WIZARD_MODE_FLAGS = ("auto", "auto_all", "translate_subs", "merge_pro", "resync_pro",
                      "extract_subs", "import_subs", "remove_tracks", "set_default",
                      "rename_subs", "fix_readability", "extract_audio", "import_audio",
                      "convert_audio", "rename_files", "subs_download", "retime_subs")


def _run_wizard_action(key, args):
    """Runs a single action from the registry with the right preset/back behavior."""
    specs = _wizard_action_specs()
    spec = specs[key]
    kind = spec["kind"]
    if kind == "presets":
        try:
            run_presets_menu(args)
        except WizardBack:
            pass
        return False   # no post-run pause (presets menu handles its own flow)
    if kind == "browser":
        spec["run"](args)   # self-contained TUI; no preset, no post pause
        return False
    if spec["flag"]:
        setattr(args, spec["flag"], True)
    if kind == "wizard":
        preset_begin_offer(spec["preset"])
        run_with_back(spec["run"], args)
    elif kind == "readability":
        run_with_back(spec["run"], args)
    elif kind == "config":
        run_with_back(spec["run"], args)
    else:   # direct (p1/p2/test-api)
        spec["run"](args)
    return True


def run_master_wizard(args):
    """Main wizard when started WITHOUT arguments: a two-level menu (category ->
    action). Runs the chosen sub-wizard and, where applicable, offers to save the
    choices as a preset at the end."""
    _orig_mkv = getattr(args, "mkv", None)
    specs = _wizard_action_specs()
    cat_pos = 0
    while True:
        # clean state for each visit to the main menu
        args.mkv = _orig_mkv
        for _f in _WIZARD_MODE_FLAGS:
            if hasattr(args, _f):
                setattr(args, _f, False)
        if hasattr(args, "_extract_skip_prompts"):
            args._extract_skip_prompts = False
        global _PRESET_MODE, _PRESET_SAVED
        _PRESET_MODE = None
        _PRESET_SAVED = False

        cat_labels = [f"{name}   {Fore.CYAN}-  {desc}{Style.RESET_ALL}"
                      for name, desc, _keys in _WIZARD_CATEGORIES]
        ci = ask_pick(
            "What do you want to work with?",
            cat_labels, default=0, allow_back=True, cursor=cat_pos,
            header=[f"{Fore.MAGENTA}=== video_tool - interactive wizard ==={Style.RESET_ALL}",
                    f"{Fore.CYAN}Tip:{Style.RESET_ALL} pick a category, then an action (type = search, "
                    "? = help, Esc = back). At the end you can save the choices as a preset.",
                    ""],
            help=[desc for _n, desc, _k in _WIZARD_CATEGORIES])
        if ci is None:
            log_info("Quit.")
            return
        cat_pos = ci
        cat_name, _desc, keys = _WIZARD_CATEGORIES[ci]

        # submenu of this category
        act_pos = 0
        while True:
            labels = [specs[k]["label"] for k in keys]
            ai = ask_pick(
                f"{cat_name} - what to do?",
                labels, default=0, allow_back=True, cursor=act_pos,
                header=[f"{Fore.MAGENTA}=== {cat_name} ==={Style.RESET_ALL}",
                        f"{Fore.CYAN}Esc = back to categories.{Style.RESET_ALL}", ""],
                help=[specs[k]["help"] for k in keys])
            if ai is None:
                break   # back to categories
            act_pos = ai
            key = keys[ai]
            try:
                did_run = _run_wizard_action(key, args)
            except WizardBack:
                continue   # Esc on the first question -> back to this submenu
            except KeyboardInterrupt:
                print()
                return
            except SystemExit:
                # a die() inside a wizard must not kill the whole menu - the error
                # message was already printed; fall through and return to the menu
                did_run = True
            except Exception as e:
                log_warn(f"This action ended with an error: {e}")
                did_run = True
            if not did_run:
                continue
            try:
                input(f"\n{Fore.CYAN}Done - press Enter to return to the menu "
                      f"(Ctrl+C = quit)...{Style.RESET_ALL}")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            # reset flags before next action in the submenu
            for _f in _WIZARD_MODE_FLAGS:
                if hasattr(args, _f):
                    setattr(args, _f, False)
            _PRESET_MODE = None
            _PRESET_SAVED = False


def _pause_for_menu(seconds=3.0):
    """A short wait before running the default preset. Returns True when the user
    manages to press ENTER (wants the menu). In a non-interactive run (scheduled/
    pipe) it returns False immediately so automation doesn't wait."""
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            import time
            end = time.time() + seconds
            while time.time() < end:
                if msvcrt.kbhit():
                    msvcrt.getch()
                    return True
                time.sleep(0.05)
            return False
        else:
            import select
            r, _, _ = select.select([sys.stdin], [], [], seconds)
            if r:
                sys.stdin.readline()
                return True
            return False
    except Exception:
        return False


def _colorize_help_text(text):
    """Programmatically colors the WORKFLOW text for --help (headings, separators,
    commands). The text stays plain (easy maintenance), colors are added here.
    Without colorama, Fore/Style are empty, so plain text is returned."""
    Y, C, M, B, R = Fore.YELLOW, Fore.CYAN, Fore.MAGENTA, Style.BRIGHT, Style.RESET_ALL
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s and set(s) <= set("=-") and len(s) >= 3:          # ==== / ---- separator
            out.append(f"{M}{line}{R}")
        elif line and not line[0].isspace():                    # heading (not indented)
            out.append(f"{Y}{B}{line}{R}")
        elif re.match(r"^\s+python\b", line):                   # command
            out.append(f"{C}{line}{R}")
        else:
            out.append(line)
    return "\n".join(out)


def _probe_lang_tracks(mkvmerge_bin, videos, kind, lang3=None, text_only=False):
    """For each video, returns its tracks of the given kind ('subs'/'audio') whose
    canonical language equals lang3 (None = any). text_only filters to text subtitle
    codecs. Result: {video: {kind: [matching], '_all': [all tracks of kind]}} using
    the rich mkvmerge probe (id, lang, name, codec, default, forced)."""
    infos = {}
    for v in videos:
        try:
            full = _mkv_probe_full(mkvmerge_bin, Path(v))
        except Exception:
            full = {"audio": [], "subs": []}
        allk = full.get(kind, [])
        matching = list(allk)
        if text_only:
            matching = [t for t in matching if is_text_codec(t.get("codec", ""))]
        if lang3:
            matching = [t for t in matching if _canon3(t.get("lang")) == lang3]
        infos[str(v)] = {kind: matching, "_all": allk}
    return infos


def _probe_lang_subs(mkvmerge_bin, videos, lang3):
    """Backwards-compatible wrapper: text subtitle tracks in the given language."""
    return _probe_lang_tracks(mkvmerge_bin, videos, "subs", lang3, text_only=True)


def _select_lang_track_key(videos, infos, kind, lang_word, interactive):
    """Given per-video matching tracks, decide which track identity to use for the
    whole batch. With a single identity -> auto (no question). With several ->
    interactively let the user pick, showing a detailed listing to identify the
    track. Returns (key or None, aborted: bool). key=None means nothing matched.
    kind: 'subs' or 'audio'."""
    track_word = "subtitle" if kind == "subs" else "audio"
    keys = _aggregate_track_keys(infos, kind)   # [(key, label, count)]
    if not keys:
        return None, False
    if len(keys) == 1:
        return keys[0][0], False
    total = len(videos)
    sample = None
    for v in videos:
        if len(infos[str(v)][kind]) > 1:
            sample = v
            break
    if sample is None:
        for v in videos:
            if infos[str(v)][kind]:
                sample = v
                break
    header = [f"{Fore.YELLOW}Found several distinct {lang_word}{track_word} tracks across the videos.{Style.RESET_ALL}"]
    if sample is not None:
        header.append(f"{Fore.CYAN}Tracks in sample '{Path(sample).name}':{Style.RESET_ALL}")
        for t in infos[str(sample)][kind]:
            flags = []
            if t.get("forced"):
                flags.append("forced")
            if t.get("default"):
                flags.append("default")
            fl = ("  [" + ", ".join(flags) + "]") if flags else ""
            nm = t.get("name") or "-"
            header.append(f"   #{t['id']}  lang={_lang3_name(t.get('lang'))}  name={nm}  "
                          f"codec={t.get('codec', '?')}{fl}")
    labels = [f"{label}  —  in {count}/{total} videos" for _k, label, count in keys]
    if not interactive:
        log_warn(f"Multiple {lang_word}{track_word} tracks and no interactive terminal - using the first: {keys[0][1]}.")
        return keys[0][0], False
    idx = ask_pick(f"Which {lang_word}{track_word} track to use for all videos?",
                   labels, default=0, header=header, allow_back=True)
    if idx is None:
        log_warn("Cancelled by the user.")
        return None, True
    return keys[idx][0], False


def _pick_video_track(infos, video, chosen_key, kind="subs"):
    """Picks the track in this video matching chosen_key; falls back to the single
    matching track if the exact key isn't present. Returns a track dict or None."""
    matching = infos[str(video)][kind]
    if not matching:
        return None
    t = _track_by_key(matching, chosen_key)
    if t is not None:
        return t
    if len(matching) == 1:
        return matching[0]
    return None


def run_p1(args):
    """FIXED PRESET (--p1), built directly into the script (no preset file):
    from the videos in the directory it extracts CZECH subtitles (accepts the
    aliases cze/ces/cz/cs) and immediately applies a readability fix (9 chars/s,
    min 2.5s, gap 0.084s, bonus 0.20s). If several distinct Czech tracks are found
    it asks (once) which to use, with a detailed listing; otherwise it asks nothing.
    Overwrites <video>.cze.srt without a backup."""
    print(f"{Fore.MAGENTA}=== Preset --p1: extract CZECH subtitles + readability fix ==={Style.RESET_ALL}")
    directory = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or "."
    recursive = bool(getattr(args, "recursive", False))
    log_info(f"Working directory: {os.path.abspath(directory)}")
    videos = collect_videos(directory, recursive)
    if not videos:
        die("No videos in the directory.")
    log_info(f"Found {len(videos)} videos.")

    mkvmerge_bin, mkvextract_bin, ffmpeg_bin, _ = _resolve_tools_for_extract(args, Path(videos[0]))
    if not mkvmerge_bin:
        die("Could not find mkvmerge (MKVToolNix). Install MKVToolNix (see --help).")
    args.mkvmerge = getattr(args, "mkvmerge", None) or mkvmerge_bin
    if mkvextract_bin:
        args.mkvextract = getattr(args, "mkvextract", None) or mkvextract_bin
    if ffmpeg_bin:
        args.ffmpeg = getattr(args, "ffmpeg", None) or ffmpeg_bin

    interactive = sys.stdin.isatty()
    infos = _probe_lang_subs(mkvmerge_bin, videos, "cze")
    chosen_key, aborted = _select_lang_track_key(videos, infos, "subs", "Czech ", interactive)
    if aborted:
        return
    if chosen_key is None:
        die("No Czech text subtitle track found in any video.")

    # readability fix - exactly the values from the log
    RS = dict(min_cps=9.0, min_duration_floor=2.5, min_gap=0.084, line_overhead=0.2)

    done = skipped = 0
    for v in videos:
        vp = Path(v)
        chosen = _pick_video_track(infos, v, chosen_key, "subs")
        if chosen is None:
            log_warn(f"{vp.name}: no matching Czech track - skipping.")
            skipped += 1
            continue
        events, _ch = extract_subtitle_events(args, vp, track_id=chosen["id"])
        if not events:
            log_warn(f"{vp.name}: extracting track #{chosen['id']} failed - skipping.")
            skipped += 1
            continue
        fixed, n_ext = fix_short_durations(events, **RS)
        out = vp.with_name(vp.stem + ".cze.srt")
        try:
            write_srt(fixed, out)
        except Exception as e:
            log_warn(f"{vp.name}: write failed ({e}) - skipping.")
            skipped += 1
            continue
        log_done(f"{vp.name}: track #{chosen['id']} (cze, {chosen.get('codec', '')}) -> {out.name} "
                 f"({len(fixed)} subtitles, extended {n_ext})")
        done += 1

    print()
    log_done(f"Done: {done} .cze.srt from {len(videos)} videos ({skipped} skipped).")


def run_p2(args):
    """FIXED PRESET (--p2), built directly into the script (no preset file):
    from the videos in the directory it extracts an ENGLISH subtitle track and
    translates it into CZECH (engine 'google' - free, no key), with fast rule-based
    proofreading, then applies the same readability fix as --p1 (9 chars/s, min 2.5s,
    gap 0.084s, bonus 0.20s), saving <video>.cs.srt (original timing kept). If several
    distinct English tracks are found it asks (once) which to use, with a detailed
    listing; otherwise it asks nothing. Overwrites <video>.cs.srt."""
    print(f"{Fore.MAGENTA}=== Preset --p2: translate ENGLISH subtitles -> CZECH + readability (.cs.srt) ==={Style.RESET_ALL}")
    directory = str(args.mkv) if getattr(args, "mkv", None) else "."
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory) or "."
    recursive = bool(getattr(args, "recursive", False))
    log_info(f"Working directory: {os.path.abspath(directory)}")
    videos = collect_videos(directory, recursive)
    if not videos:
        die("No videos in the directory.")
    log_info(f"Found {len(videos)} videos.")

    mkvmerge_bin, mkvextract_bin, ffmpeg_bin, _ = _resolve_tools_for_extract(args, Path(videos[0]))
    if not mkvmerge_bin:
        die("Could not find mkvmerge (MKVToolNix). Install MKVToolNix (see --help).")
    args.mkvmerge = getattr(args, "mkvmerge", None) or mkvmerge_bin
    if mkvextract_bin:
        args.mkvextract = getattr(args, "mkvextract", None) or mkvextract_bin
    if ffmpeg_bin:
        args.ffmpeg = getattr(args, "ffmpeg", None) or ffmpeg_bin

    interactive = sys.stdin.isatty()
    infos = _probe_lang_subs(mkvmerge_bin, videos, "eng")
    chosen_key, aborted = _select_lang_track_key(videos, infos, "subs", "English ", interactive)
    if aborted:
        return
    if chosen_key is None:
        die("No English text subtitle track found in any video.")

    done = skipped = 0
    for v in videos:
        vp = Path(v)
        chosen = _pick_video_track(infos, v, chosen_key, "subs")
        if chosen is None:
            log_warn(f"{vp.name}: no matching English track - skipping.")
            skipped += 1
            continue
        events, _ch = extract_subtitle_events(args, vp, track_id=chosen["id"])
        if not events:
            log_warn(f"{vp.name}: extracting track #{chosen['id']} failed - skipping.")
            skipped += 1
            continue
        log_info(f"{vp.name}: translating {len(events)} subtitles into 'cs' (google)...")
        translated = translate_events_to(events, "google", "cs")
        if translated is None:
            die("Translator 'google' is not available (check your internet connection).")
        changed = sum(1 for a, b in zip(events, translated) if a["text"] != b["text"])
        if changed == 0:
            log_warn(f"{vp.name}: translation produced no changes - NOT saving as 'cs'. Skipping.")
            skipped += 1
            continue
        apply_proofread(translated, "rules", "cs", args)
        # readability fix - same values as --p1
        translated, n_ext = fix_short_durations(
            translated, min_cps=9.0, min_duration_floor=2.5, min_gap=0.084, line_overhead=0.2)
        out = vp.with_name(vp.stem + ".cs.srt")
        try:
            write_srt(translated, out)
        except Exception as e:
            log_warn(f"{vp.name}: write failed ({e}) - skipping.")
            skipped += 1
            continue
        log_done(f"{vp.name}: track #{chosen['id']} (eng) -> {out.name} "
                 f"({len(translated)} subtitles, extended {n_ext})")
        done += 1

    print()
    log_done(f"Done: {done} .cs.srt from {len(videos)} videos ({skipped} skipped).")


def _dependency_help():
    """Colored dependency section for --help. When something is added/removed,
    update it HERE (one source of truth for --help and the README)."""
    C, Y, G, M = Fore.CYAN, Fore.YELLOW, Fore.GREEN, Fore.MAGENTA
    B, R = Style.BRIGHT, Style.RESET_ALL
    return f"""
{M}{B}=== DEPENDENCIES / INSTALLATION ==={R}

{Y}Python packages (pip):{R}
  {G}Required:{R}     pip install numpy
  {G}Recommended:{R}  pip install numpy colorama charset-normalizer deep-translator
  {G}All/optional:{R} pip install numpy colorama charset-normalizer deep-translator argostranslate langdetect py7zr

    numpy               {C}REQUIRED{R} - the script won't start without it
    colorama            colored output (needed for colors on Windows; on Linux colors even without it)
    charset-normalizer  subtitle encoding detection (VIU/CJK/UTF-16); alternative: chardet
    deep-translator     translation via Google and DeepL
    argostranslate      offline translation (large - downloads language models)   [optional]
    langdetect          language detection (the script has its own fallback too) [optional]
    py7zr               Windows only: auto-download of MKVToolNix (.7z)          [optional]
  (AI translation Claude/Gemini/OpenAI and OpenSubtitles run over HTTP - no SDK needed.)

{Y}System tools (NOT via pip):{R}
  {G}MKVToolNix{R} (mkvmerge/mkvextract/mkvpropedit) - working with MKV tracks (extract, mux, default/remove)
  {G}ffmpeg{R} - only for AUDIO-based synchronization (VAD) and for MP4; not needed for normal subtitle work

  {C}Windows:{R}              the script downloads MKVToolNix and ffmpeg itself when needed
  {C}Debian/Ubuntu:{R}        sudo apt install mkvtoolnix ffmpeg
  {C}Fedora:{R}               sudo dnf install mkvtoolnix ffmpeg
  {C}Arch:{R}                 sudo pacman -S mkvtoolnix-cli ffmpeg
  {C}AlmaLinux/Rocky/RHEL:{R} (verze: rpm -E %rhel)
     MKVToolNix (official bunkus.org repo):
        EL8:     sudo rpm -Uhv https://mkvtoolnix.download/almalinux/bunkus-org-repo-2-4.noarch.rpm
        EL9/10:  sudo rpm -Uhv https://mkvtoolnix.download/centosstream/bunkus-org-repo-2-4.noarch.rpm
        sudo dnf install mkvtoolnix
     ffmpeg (EPEL + CRB + RPM Fusion):
        sudo dnf install epel-release
        sudo dnf config-manager --set-enabled crb
        sudo dnf install --nogpgcheck https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm
        sudo dnf install ffmpeg
"""


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{Fore.CYAN}{Style.BRIGHT}video_tool{Style.RESET_ALL} - synchronization, translation, extraction and maintenance of subtitles "
                    f"(MKVToolNix for tracks, ffmpeg optionally for audio/MP4).\n"
                    f"{Fore.YELLOW}Run without arguments for the interactive wizard with an arrow menu.{Style.RESET_ALL}",
        epilog=_dependency_help() + _colorize_help_text(r"""
WORKFLOW - how to actually work with the script
===============================================

SIMPLEST - the interactive wizard (the script asks about EVERYTHING step by step):
    python video_tool.py --auto                 # single file, searches this directory
    python video_tool.py --auto D:\folder       # ... in the given directory
    python video_tool.py --auto-all D:\series    # whole batch, but with questions up front
  --auto offers the subtitles to fix and a reference source; as a reference it can
  also take a SECOND SUBTITLE FILE (.srt/.orig) entirely WITHOUT a video - ideal when
  you have just two subtitle files (mistimed + correctly timed). For a video it offers
  the REAL tracks (mkvmerge), DETECTS languages from content itself, and for different
  languages offers translation just for matching. Default method = combo
  (affine pre-align + warp fine-tune).

TRANSLATE SUBTITLES INTO ANOTHER LANGUAGE (extract from a video and save .srt):
    python video_tool.py --translate-subs D:\series
  For all videos in the directory: extracts the chosen subtitle track and saves
  subtitles in the target language as <video>.<lang>.srt. It asks about everything.
  Quality is handled via: ready HUMAN subtitles from OpenSubtitles (best; requires an
  API key and an account for downloading), or machine translation (DeepL = best quality
  with an API key / Google free / Argos offline) + proofreading (free rule-based cleanup,
  optionally AI proofreading via an OpenAI-compatible API). With machine translation the
  original timing stays, so the result matches the video. Keys can also be provided via
  the DEEPL_API_KEY / OPENSUBTITLES_API_KEY / OPENAI_API_KEY variables.

Or manually, when you know exactly what you want:

STEP 1 - look at what is in the video:
    python video_tool.py --list-tracks video.mkv
  Lists both subtitle and audio tracks with their IDs and languages. From that you learn
  whether the video has a usable TEXT subtitle track (SRT/ASS) as a reference and what
  its language is.

STEP 2 - align a single file (the most common case):
    python video_tool.py video.mkv subtitles_cz.srt output_cz.srt
  By default (--method auto, --audio-mode off) it takes the reference subtitle track from
  the video and aligns your Czech .srt to it. When there is a text reference in the video,
  the new content-based method "warp" is used (by sentence - also fixes piecewise desync);
  otherwise it falls back to affine.

  - Specific/multilingual tracks:   --ref-lang eng    or    --track-id 3
  - No usable subtitle reference (only image-based PGS, or none)?
        python video_tool.py video.mkv t_cz.srt out.srt --audio-mode replace
    -> aligns by AUDIO (speech detection, VAD). Here it always runs "affine".
  - Maximum robustness (subtitles + audio together):  --audio-mode combine

STEP 3 - check the result in a player. When it almost fits, but some scenes "drift"
  differently from the rest = piecewise desync -> force the content-based method:
    python video_tool.py video.mkv t_cz.srt out.srt --method warp
  When on the contrary the reference translation is very different (few shared sentences),
  it is safer:  --method affine

STEP 4 (optional) - readability: extend subtitles that disappear too fast, but only into
  free space (never past an overlap):
    ... --fix-short-duration --reading-speed slow

BATCH (a whole series in one directory) - videos are paired with .srt by name:
    python video_tool.py D:\series --all --target-lang cs --overwrite
  (--overwrite overwrites the originals and makes a one-time .bak; without it you get
   '<name>.synced.srt'. --yes = don't ask and skip missing tracks.)

READABILITY ONLY, without synchronization (the subtitles already have correct timing):
    python video_tool.py D:\series --fix-readability

"warp" TUNING TIPS:
    --ca-band 60        wider anchor search (larger/block desync)
    --ca-snap-win 2     more cautious local fine-tuning
    --ca-min-sim 0.6    stricter anchors (more confident, but there will be fewer)

DIFFERENT LANGUAGES (target vs reference):
    ... --translate google                 # online, translates just for matching
    ... --translate argos --pivot-lang en   # offline (pip install argostranslate langdetect)
  Without --translate, different languages are handled by the affine method (timing). The
  subtitle text is never translated, only the times change. Translations are cached.
"""),
    )
    parser.add_argument("mkv", type=Path, nargs="?",
                         help="Input MKV/MP4 file (or with --all: a directory to search, default '.')")
    parser.add_argument("subtitle_to_fix", type=Path, nargs="?", help="The SRT with bad timing that we want to fix")
    parser.add_argument("output", type=Path, nargs="?", help="Path to the output fixed SRT")

    parser.add_argument("--all", action="store_true",
                         help="Batch mode: processes all videos in the directory (1st argument, default "
                              "the current directory). For each video the matching .srt is found by file "
                              "name, the available tracks are verified, and only then is it processed.")
    parser.add_argument("-r", "--recursive", action="store_true",
                         help="With --all: search subdirectories too.")
    parser.add_argument("--target-lang", help="With --all: if multiple .srt files match one video "
                                                "(different languages), use the one with this language tag in the name "
                                                "(e.g. 'cs' for 'episode.cs.srt').")
    parser.add_argument("--overwrite", action="store_true",
                         help="With --all: overwrite the original .srt directly (makes a one-time .bak backup). "
                              "Without this, the output is saved as '<name>.synced.srt' next to the original.")
    parser.add_argument("--yes", action="store_true",
                         help="With --all: when a video lacks the needed tracks, automatically skip "
                              "without an interactive prompt (for unattended/batch runs). "
                              "With --fix-readability: do not use default parameter values without asking.")

    parser.add_argument("--fix-readability", action="store_true",
                         help="Standalone mode (WITHOUT synchronization): finds in the directory (1st argument, "
                              "default the current directory; or a specific .srt directly) all subtitles that "
                              "already have CORRECT timing, and only extends those that disappear too "
                              "quickly for comfortable reading - exclusively into free space, never at the "
                              "cost of an overlap. Without --min-cps/--min-duration-floor/--min-gap it asks "
                              "for the values interactively (with an explanation and an approximate estimate of the current speed).")

    parser.add_argument("--ref-lang", help="Language of the reference SUBTITLE track in the MKV, e.g. eng, cze, ces")
    parser.add_argument("--track-id", type=int, help="Subtitle track ID in the MKV (see --list-tracks)")

    parser.add_argument(
        "--audio-mode", choices=["off", "replace", "combine"], default="off",
        help="off = subtitle reference only (default); replace = audio analysis only (VAD), "
             "the subtitle reference is not used; combine = subtitle reference + audio together "
             "for maximum accuracy.",
    )
    parser.add_argument("--audio-lang", help="Audio track language for VAD, e.g. eng, cze, ces")
    parser.add_argument("--audio-track-id", type=int, help="Audio track ID in the MKV (see --list-tracks)")
    parser.add_argument("--vad-percentile", type=float, default=55.0,
                         help="Loudness threshold for speech detection, percentile 0-100 (default 55; "
                              "raise for noisy background/music, lower for quieter dialog)")

    parser.add_argument(
        "--method", choices=["auto", "affine", "warp", "combo"], default="auto",
        help="How to compute the timing. 'affine' = global shift+speed (a*t+b, "
             "language independent, even from audio alone). 'warp' = content-based method by SENTENCE "
             "(also fixes piecewise desync; needs a text reference). 'combo' = "
             "affine pre-align + warp fine-tune (most robust). 'auto' (default) = "
             "combo when there is a text reference and enough anchors, otherwise affine.")
    parser.add_argument("--ca-band", type=float, default=None,
                         help="(--method warp/auto only) anchor search radius in seconds (default 45)")
    parser.add_argument("--ca-snap-win", type=float, default=None,
                         help="(--method warp/auto only) local snapping window in seconds "
                              "(default 3; smaller = more cautious)")
    parser.add_argument("--ca-min-sim", type=float, default=None,
                         help="(--method warp/auto only) min text similarity for an anchor 0-1 (default 0.50)")
    parser.add_argument("--translate", choices=["off", "google", "deepl", "argos", "claude", "gemini"], default="off",
                         help="Cross-language matching (for the warp/auto/combo method only): when the fixed and "
                              "reference subtitles are in DIFFERENT languages, it translates both sides into a common "
                              "language (--pivot-lang) FOR MATCHING purposes ONLY - the subtitle text is not changed. "
                              "'google' free, 'deepl'/'claude' better quality (API key), 'argos' offline. "
                              "Translations are cached. Without this, different languages are handled by the affine method.")
    parser.add_argument("--pivot-lang", default="en",
                         help="Common language for cross-language matching with --translate (default 'en').")

    parser.add_argument("--list-tracks", action="store_true", help="Just list the subtitle and audio tracks in the MKV and exit")
    parser.add_argument("--max-shift", type=float, default=120.0, help="Maximum expected shift in seconds (default 120)")
    parser.add_argument("--tolerance", type=float, default=1.5, help="Tolerance in seconds for matching during refinement (default 1.5)")

    parser.add_argument("--fix-short-duration", action="store_true",
                         help="After synchronization, extend subtitles that disappear too quickly relative "
                              "to the text length - but only if there is free space (a gap to the next "
                              "subtitle), never at the cost of an overlap with the next subtitle.")
    parser.add_argument("--reading-speed", choices=list(READING_SPEED_PRESETS.keys()),
                         help="Quick reading-speed preset choice instead of manual --min-cps/--min-duration-floor: "
                              + "; ".join(f"'{k}' = {v[2]} ({v[0]:.0f} chars/s, floor {v[1]:.1f}s)"
                                          for k, v in READING_SPEED_PRESETS.items())
                              + ". Explicitly given --min-cps/--min-duration-floor take precedence over the preset.")
    parser.add_argument("--min-cps", type=float, default=None,
                         help=f"Target reading speed in characters/s for computing the ideal min display "
                              f"duration (default {DEFAULT_MIN_CPS}; lower = longer display for the same text)")
    parser.add_argument("--min-duration-floor", type=float, default=None,
                         help=f"Absolute minimum subtitle display duration in seconds, regardless "
                              f"of text length (default {DEFAULT_MIN_DURATION_FLOOR})")
    parser.add_argument("--min-gap", type=float, default=None,
                         help=f"Gap in seconds that must be preserved before the next subtitle "
                              f"when extending (default {DEFAULT_MIN_GAP} - about 2 frames at 24fps)")
    parser.add_argument("--line-overhead", type=float, default=None,
                         help=f"Extra seconds for EACH subtitle line above the first - multiple lines "
                              f"need extra time for the eyes to jump (default {DEFAULT_LINE_OVERHEAD}; "
                              f"thanks to this a single-word subtitle never gets the same duration as "
                              f"a multi-line sentence just because of a shared floor)")
    parser.add_argument("--mkvmerge", help="Path to mkvmerge.exe or to a folder containing it, if not in PATH")
    parser.add_argument("--mkvextract", help="Path to mkvextract.exe or to a folder containing it, if not in PATH")
    parser.add_argument("--no-mkvtoolnix-download", action="store_true",
                         help="Do not try to automatically download MKVToolNix if it was not found anywhere")
    parser.add_argument("--ffmpeg", help="Path to ffmpeg.exe or to a folder containing it (only for --audio-mode replace/combine)")
    parser.add_argument("--ffmpeg-url",
                         help="URL to a .zip with a Windows ffmpeg build for auto-download (tried first, "
                              "then the built-in fallbacks). Default: see FFMPEG_DOWNLOAD_URLS in the script.")
    parser.add_argument("--no-ffmpeg-download", action="store_true",
                         help="Do not try to automatically download ffmpeg if it was not found anywhere")
    parser.add_argument("--auto", action="store_true",
                         help="Interactive wizard for ONE file: searches the directory, offers the subtitles to fix and a reference source (a video OR a second subtitle file) and asks about everything step by step.")
    parser.add_argument("--auto-all", action="store_true",
                         help="Interactive wizard for a BATCH (like --all, but asks about the settings first): method, reference source, language, overwrite, readability - then processes the whole directory.")
    parser.add_argument("--translate-subs", action="store_true",
                         help="Interactive mode: for all videos in the directory it extracts the chosen subtitle track, obtains subtitles in the target language (OpenSubtitles and/or machine translation + proofreading) and saves them as <video>.<lang>.srt.")
    parser.add_argument("--out-lang", default=None, help="(--translate-subs) target translation language, e.g. cs")
    parser.add_argument("--merge-pro", action="store_true",
                        help="Interactive mode: replaces the machine translation of your subtitles with a PROFESSIONAL "
                             "translation of the same show from another directory (matched by content), the timing "
                             "stays. It asks for the directory with the 'viki' subtitles.")
    parser.add_argument("--resync-pro", action="store_true",
                        help="Interactive mode: the OPPOSITE - takes professional subtitles from another directory "
                             "and re-times them to your timing (100%% pro text with correct timing).")
    parser.add_argument("--extract-subs", action="store_true",
                        help="Interactive mode: extracts subtitle tracks from videos (mkv/mp4/...) into .srt; "
                             "it detects the tracks from the video and you pick which (by language / specific / all).")
    parser.add_argument("--import-subs", action="store_true",
                        help="Interactive mode: inserts (mux) subtitles from a folder into videos by SxxExx (MKVToolNix).")
    parser.add_argument("--remove-tracks", action="store_true",
                        help="Interactive mode: removes audio/subtitle tracks from MKV by language.")
    parser.add_argument("--set-default", action="store_true",
                        help="Interactive mode: sets the default audio/subtitle track by language.")
    parser.add_argument("--rename-subs", action="store_true",
                        help="Interactive mode: renames .srt by video names (paired by SxxExx).")
    parser.add_argument("--extract-audio", action="store_true",
                        help="Interactive mode: extracts an audio track (by language) from each video into "
                             "a standalone audio file via stream copy.")
    parser.add_argument("--import-audio", action="store_true",
                        help="Interactive mode: muxes an external audio file (paired by SxxExx) into each "
                             "video as the default audio and sets default subtitles.")
    parser.add_argument("--convert-audio", action="store_true",
                        help="Interactive mode: re-encodes audio in each MKV to a chosen codec (e.g. AC-3), "
                             "copying video and subtitles.")
    parser.add_argument("--rename-files", action="store_true",
                        help="Interactive mode: intelligent file renamer (zero-pads numbers, fills common "
                             "words, normalizes case, strips emoji; preview then apply).")
    parser.add_argument("--video-browser", action="store_true",
                        help="Interactive mode: Total Commander style video browser; Enter on a video opens a "
                             "full inspector with every track/convert operation.")
    parser.add_argument("--subs-download", dest="subs_download", action="store_true",
                        help="Interactive mode: download subtitles from OpenSubtitles for a folder of videos "
                             "(multi-language). Needs a free OpenSubtitles key/account (--config).")
    parser.add_argument("--retime-subs", dest="retime_subs", action="store_true",
                        help="Interactive mode: re-time subtitles to a different video source (auto-detect "
                             "source/target by episode, analyse both videos' audio). Writes <target>.<lang>.srt.")
    parser.add_argument("--p1", action="store_true",
                        help="FIXED PRESET built into the script: from the videos in the directory it extracts CZECH subtitles "
                             "(aliases cze/ces/cz/cs) and immediately fixes readability (9 chars/s, min 2.5s). No prompts, "
                             "overwrites <video>.cze.srt. Optionally a path to the directory as a positional argument.")
    parser.add_argument("--p2", action="store_true",
                        help="FIXED PRESET built into the script: from the videos in the directory it extracts an "
                             "ENGLISH subtitle track, translates it into CZECH (engine 'google', free) with "
                             "rule-based proofreading and a readability fix (9 chars/s, min 2.5s), saving <video>.cs.srt. "
                             "If several English tracks exist it asks which to use. Optionally a path to the directory "
                             "as a positional argument.")
    parser.add_argument("--presets", action="store_true",
                        help="Opens the Presets menu (run/create/delete saved configurations) - works even when a default preset is set.")
    parser.add_argument("--no-preset", action="store_true",
                        help="Ignores the default preset and runs the wizard directly (even when a default preset is set).")
    parser.add_argument("--sub-source", choices=["auto", "mt", "opensubtitles"], default="auto",
                         help="(--translate-subs) where to get the target subtitles from (default auto).")
    parser.add_argument("--deepl-key", default=None, help="API key for DeepL (or the DEEPL_API_KEY variable).")
    parser.add_argument("--opensubtitles-key", default=None, help="API key for OpenSubtitles (or OPENSUBTITLES_API_KEY).")
    parser.add_argument("--llm-key", default=None, help="API key for AI proofreading (or OPENAI_API_KEY).")
    parser.add_argument("--config", action="store_true",
                         help="Interactive setup of API keys and default options into video_tool.config.json (asks only about what you want to enable). Loaded automatically at startup.")
    parser.add_argument("--config-file", default=None, help="One-off path to the settings file (folder or full .json; "
                        "supports UNC \\\\server\\share, mapped drives, ~ and env vars, Linux/SMB paths). "
                        "To set it permanently use --config or the CONFIG_STORE_PATH constant. Default: next to the script.")
    parser.add_argument("--no-config", action="store_true", help="Do not load the saved configuration (config) at startup.")
    parser.add_argument("--anthropic-key", default=None, help="Anthropic (Claude) API key (or ANTHROPIC_API_KEY).")
    parser.add_argument("--anthropic-model", default=None, help="Model Claude (default claude-sonnet-4-6).")
    parser.add_argument("--gemini-key", default=None, help="Google Gemini API key - FREE AI translation (or GEMINI_API_KEY/GOOGLE_API_KEY). Get one at aistudio.google.com.")
    parser.add_argument("--gemini-model", default=None, help="Model Gemini (default gemini-2.0-flash).")
    parser.add_argument("--llm-api", default=None, help="URL of an OpenAI-compatible API (/chat/completions).")
    parser.add_argument("--llm-model", default=None, help="Model for OpenAI-compatible proofreading.")
    parser.add_argument("--opensubtitles-user", default=None, help="OpenSubtitles username (for downloading).")
    parser.add_argument("--opensubtitles-password", default=None, help="OpenSubtitles password (for downloading).")
    parser.add_argument("--save", action="store_true",
                         help="Only with an interactive command (--auto/--auto-all/--translate-subs): "
                              "after filling it in, saves ALL choices into preset.json and runs the operation. "
                              "API keys are NOT saved into the preset (those belong in --config).")
    parser.add_argument("--load", action="store_true",
                         help="Loads preset.json and immediately runs the saved operation without prompts "
                              "(the command is taken from the preset, or specify it, e.g. --load --translate-subs).")
    parser.add_argument("--preset-file", default=None, help="(deprecated) Alias for --config-file; presets are now in video_tool.config.json.")
    parser.add_argument("--test-api", action="store_true",
                         help="Sends a trivial request to the configured AI API (Anthropic/OpenAI) and prints "
                              "the exact response or error (including the server body) - for debugging e.g. HTTP 400.")
    args = parser.parse_args()

    global _STORE_PATH, _FFMPEG_URL_OVERRIDE
    _STORE_PATH = _normalize_store_path(getattr(args, "config_file", None) or getattr(args, "preset_file", None))
    if getattr(args, "ffmpeg_url", None):
        _FFMPEG_URL_OVERRIDE = args.ffmpeg_url
    migrate_legacy_store()

    _cfg = {} if getattr(args, "no_config", False) else load_config()
    apply_config_to_args(args, _cfg)
    if args.config:
        run_config(args)
        return
    if args.test_api:
        run_test_api(args)
        return

    if getattr(args, "p1", False):
        run_p1(args)
        return

    if getattr(args, "p2", False):
        run_p2(args)
        return

    if getattr(args, "presets", False):
        run_presets_menu(args)
        return

    # Running WITHOUT any argument:
    #   - when a default preset exists -> runs the saved action directly (no prompts)
    #   - otherwise -> the main interactive wizard (offers to save a preset at the end)
    if len(sys.argv) <= 1 and not args.load and not args.save:
        _preset = get_preset(None)
        if _preset and _preset.get("command"):
            cmd = _preset["command"]
            store_name = os.path.basename(current_store_path())
            log_info(f"Found a DEFAULT preset ({cmd}) in {store_name}.")
            log_info("Press ENTER within 3 s for the Presets menu (run another / DELETE / wizard), "
                     "otherwise I'll run it right away...")
            if _pause_for_menu(3.0):
                run_presets_menu(args)
                return
            log_info(f"Running '{cmd}' without prompts. (Management: run with --presets, or --no-preset for the wizard.)")
            preset_begin_load(_preset.get("answers", []))
            dispatch_interactive_command(cmd, args)
            return
        run_master_wizard(args)
        return

    if getattr(args, "no_preset", False) and not any([
            args.auto, args.auto_all, args.translate_subs, args.merge_pro, args.resync_pro,
            args.extract_subs, args.import_subs, args.remove_tracks, args.set_default,
            args.rename_subs, args.fix_readability, args.extract_audio, args.import_audio,
            args.convert_audio, args.rename_files, args.video_browser, args.subs_download, args.retime_subs]):
        run_master_wizard(args)
        return

    # --- preset (--save / --load) for interactive commands ---------------
    interactive_cmd = ("auto-all" if args.auto_all else "auto" if args.auto
                       else "translate-subs" if args.translate_subs
                       else "merge-pro" if args.merge_pro
                       else "resync-pro" if args.resync_pro
                       else "extract-subs" if args.extract_subs
                       else "import-subs" if args.import_subs
                       else "remove-tracks" if args.remove_tracks
                       else "set-default" if args.set_default
                       else "rename-subs" if args.rename_subs
                       else "extract-audio" if args.extract_audio
                       else "import-audio" if args.import_audio
                       else "convert-audio" if args.convert_audio
                       else "rename-files" if args.rename_files
                       else "video-browser" if args.video_browser
                       else "subs-download" if args.subs_download
                       else "retime-subs" if args.retime_subs else None)
    if args.save and args.load:
        die("--save and --load cannot be combined.")
    if args.save and not interactive_cmd:
        die("--save only works with an interactive command (--auto / --auto-all / --translate-subs / --merge-pro / --resync-pro).")
    if args.load:
        preset = get_preset(None)
        if not preset:
            die(f"Default preset not found in {os.path.basename(current_store_path())} "
                "(first run the same command with --save, or create a preset in the Presets menu).")
        cmd = interactive_cmd or preset.get("command")
        if not cmd:
            die("The preset does not contain a saved command - run it e.g. as '--load --translate-subs'.")
        if interactive_cmd and preset.get("command") and interactive_cmd != preset.get("command"):
            log_warn(f"The preset is for '{preset.get('command')}', but you are running '{interactive_cmd}'.")
        preset_begin_load(preset.get("answers", []))
        log_info(f"Loading the preset ({len(preset.get('answers', []))} choices) and running '{cmd}' without prompts.")
        interactive_cmd = cmd
    if args.save and interactive_cmd:
        preset_begin_save(interactive_cmd)

    if interactive_cmd and (args.save or args.load):
        if interactive_cmd == "auto-all":
            run_auto_all(args)
        elif interactive_cmd == "auto":
            run_auto_single(args)
        elif interactive_cmd == "translate-subs":
            run_translate_subs(args)
        elif interactive_cmd == "merge-pro":
            run_transplant(args)
        elif interactive_cmd == "resync-pro":
            run_resync_pro(args)
        elif interactive_cmd == "extract-subs":
            run_extract_subs(args)
        elif interactive_cmd == "import-subs":
            run_import_subs(args)
        elif interactive_cmd == "remove-tracks":
            run_remove_tracks(args)
        elif interactive_cmd == "set-default":
            run_set_default(args)
        elif interactive_cmd == "rename-subs":
            run_rename_subs(args)
        elif interactive_cmd == "extract-audio":
            run_extract_audio(args)
        elif interactive_cmd == "import-audio":
            run_import_audio(args)
        elif interactive_cmd == "convert-audio":
            run_convert_audio(args)
        elif interactive_cmd == "rename-files":
            run_rename_files(args)
        elif interactive_cmd == "video-browser":
            run_video_browser(args)
        elif interactive_cmd == "subs-download":
            run_subs_download(args)
        elif interactive_cmd == "retime-subs":
            run_retime_batch(args)
        preset_flush_if_save()
        return

    if args.auto_all:
        run_auto_all(args)
        return
    if args.auto:
        run_auto_single(args)
        return
    if args.translate_subs:
        run_translate_subs(args)
        return
    if args.merge_pro:
        run_transplant(args)
        return
    if args.resync_pro:
        run_resync_pro(args)
        return
    if args.extract_subs:
        run_extract_subs(args)
        return
    if args.import_subs:
        run_import_subs(args)
        return
    if args.remove_tracks:
        run_remove_tracks(args)
        return
    if args.set_default:
        run_set_default(args)
        return
    if args.rename_subs:
        run_rename_subs(args)
        return
    if args.extract_audio:
        run_extract_audio(args)
        return
    if args.import_audio:
        run_import_audio(args)
        return
    if args.convert_audio:
        run_convert_audio(args)
        return
    if args.rename_files:
        run_rename_files(args)
        return
    if args.video_browser:
        run_video_browser(args)
        return
    if args.subs_download:
        run_subs_download(args)
        return
    if args.retime_subs:
        run_retime_batch(args)
        return

    if args.all and args.fix_readability:
        die("--all and --fix-readability cannot be used at the same time (they are two separate batch modes).")

    if args.fix_readability:
        run_fix_readability(args)
        return

    if args.all:
        run_batch(args)
        return

    if not args.mkv:
        parser.error("the following arguments are required: mkv")

    if not args.mkv.exists():
        die(f"Input file does not exist: {args.mkv}")

    process_single(args)


if __name__ == "__main__":
    try:
        main()
    except WizardBack:
        log_info("Back/quit.")
    except KeyboardInterrupt:
        print()
        log_warn("Interrupted by the user.")
        sys.exit(130)
