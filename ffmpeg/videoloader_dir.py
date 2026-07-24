from urllib.parse import (unquote, unquote_plus, urlencode, urljoin, urlparse,
                          parse_qsl, quote, urlunparse)
import requests
import argparse
import sys
from tqdm import tqdm
import os
import re
import fnmatch
import threading
import math
import shutil
import json
import base64
import subprocess
import time
import atexit
import random
from datetime import datetime
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
# Optional remote config: point this at a JSON file (e.g. on your NAS) and its keys OVERRIDE the
# defaults in this file — so you can change settings for many machines in one place. It may contain
# any subset of the configurable names (the USER CONFIG section below, the ADVANCED CONFIG block
# further down — timeouts, quality, ffmpeg/mp4decrypt paths+URLs — and the YouTube/Twitch API
# constants). Keys it omits keep their local value, and the command line still overrides everything.
# EMPTY = disabled (the script behaves exactly as if this didn't exist). A short timeout means a
# dead/unreachable URL never hangs startup. CONFIG_URL itself can NOT be set from the JSON (prevents
# a redirect/config loop).
# SECURITY: prefer https:// here. This JSON may override FFMPEG (a path that gets EXECUTED) and
# the tool download URLs, so anyone able to tamper with a plain-http response can make the script
# run code of their choosing. Over plain http the script therefore ignores the keys that lead to
# code execution (see _EXEC_SENSITIVE_CONFIG_KEYS below).
CONFIG_URL = "https://nas.falco81.net/videoloader_dir.config.json"   # e.g. "https://nas.example/videoloader_dir.config.json"
# --- Certificate checking, everywhere ------------------------------------------------------- #
# True = never verify a TLS certificate, for ANY connection the script makes (config, page scans,
# manifests, segments, every download). Broken, expired, self-signed, wrong hostname — all fine,
# nothing ever fails because of a certificate. A warning is printed once per run so it is never a
# silent state. Same idea as wget --no-check-certificate / curl -k; --insecure sets it for one run.
# ON by default: a NAS or a local server almost always has a self-signed certificate, and nothing
# should ever fail over that. Set both to False for strict checking.
INSECURE_TLS = True
# Allows a config fetched over an unverified connection to set the handful of settings that decide
# WHICH PROGRAM the script runs (FFMPEG, the tool download URLs). On by default so everything works
# out of the box; set to False if this machine is on a network you do not trust.
CONFIG_TRUST_UNVERIFIED = True
CONFIG_FETCH_TIMEOUT = 4          # seconds — keep short so an unreachable CONFIG_URL can't stall startup
# --- TLS for CONFIG_URL (a NAS usually has a self-signed certificate) ---------------------- #
# Pick ONE of these when https:// fails with an SSLError:
#   CONFIG_PIN_SHA256 = "AB:CD:.."  BEST. The certificate's own fingerprint — this AUTHENTICATES a
#                                   self-signed cert, so everything (including the settings that
#                                   choose which program to run) stays enabled. Run the script once
#                                   and it prints the fingerprint of whatever your NAS presented.
#   CONFIG_CA_BUNDLE = "nas.pem"    Equally good: the path to your NAS's certificate (or its CA).
#   CONFIG_VERIFY_TLS = False       Quickest, but the connection is then UNAUTHENTICATED — anyone on
#                                   the network can impersonate the NAS, so the script keeps ignoring
#                                   the handful of settings that could make it run someone else's
#                                   program (exactly as it does over plain http). Everything else
#                                   still applies.
# These three are deliberately NOT remotely overridable: a config that could switch off the checks
# guarding itself would be no protection at all.
CONFIG_VERIFY_TLS = True
CONFIG_CA_BUNDLE = ""
CONFIG_PIN_SHA256 = ""
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
AUTO_RETRIES = 3                  # how many times to automatically re-download files that failed
                                  # (resume) before giving up and recording them in resume.json.
                                  # Set to 0 to disable automatic retries.
BAR_NCOLS = 100                   # fixed progress-bar width so bars don't stretch across wide terminals
BAR_DESC_WIDTH = 26               # fixed filename column width so all bars line up

# ---- ntfy.sh push notifications (optional) -------------------------------- #
# Fill NTFY_TOPIC with your channel name to enable phone push notifications (install the
# "ntfy" app, subscribe to the same topic). Leave it EMPTY to disable notifications entirely.
NTFY_TOPIC = ""                   # your ntfy topic/channel  (EMPTY = notifications OFF)
NTFY_SERVER = "https://ntfy.sh"   # ntfy server hostname     (change for a self-hosted server)
NTFY_TOKEN = ""                   # optional access token for private / self-hosted topics

# ---- Auto-scan hosts (optional) ------------------------------------------- #
# If you launch the script with a URL whose host matches an entry below, the script behaves as if
# you had also typed --scan / --scan-browser — handy for sites you always want scanned. A host
# matches itself AND its subdomains (idnes.cz also matches www.idnes.cz, sport.idnes.cz).
# Each value can be:
#   None                      -> plain --scan
#   N (int)                   -> --scan N  (grab only the N-th found item)
#   {"scan": N, "m": M}       -> --scan N  AND  -m M  (cap connections at M for this URL; either
#                                key is optional, e.g. {"m": 8} = --scan + -m 8)
# An explicit --scan / --scan-browser on the command line disables the auto behaviour, and an
# explicit -m always wins over a host's "m". Empty = feature off. (Works from the JSON config too.)
SCAN_AUTO_HOSTS = {
    # "idnes.cz": None,             # -> runs as: <url> --scan
    # "nas.com": 3,                 # -> runs as: <url> --scan 3
    # "slow.example.com": {"m": 8}, # -> runs as: <url> --scan -m 8
    # "nas.lan.local": {"scan": 3, "m": 8},   # -> <url> --scan 3 -m 8
}
SCAN_BROWSER_AUTO_HOSTS = {
    # "example.com": None,          # -> runs as: <url> --scan-browser
    # "shop.example.org": 2,        # -> runs as: <url> --scan-browser 2
    # "portal.example.net": {"scan": 1, "m": 16},  # -> <url> --scan-browser 1 -m 16
}
SCRIPT_VERSION = "3.14.0"
SCAN_LINK_CAP = 300              # --follow-links: max same-site pages to visit from an index page
SCAN_BROWSER_WAIT = 8           # --scan-browser: seconds to let the page's player start and fetch
                                # its manifest (raise it on slow sites)
# --scan-browser behaviour. A hidden window that nobody ever clicks is exactly the case where a
# player never requests its manifest at all, so these default to a real, visible, interactive
# browser session.
SCAN_BROWSER_SHOW = True        # show the window (you can log in / press play in it yourself)
SCAN_BROWSER_CLICK = True       # auto-press play buttons and start muted <video> elements
SCAN_BROWSER_PROFILE = True     # keep cookies between runs, so a login survives
SCAN_BROWSER_PROBE_MAX = 25     # max observed requests confirmed as a manifest by their content
SCAN_PAGE_WORKERS = 4           # parallel page/subtitle requests when scanning (keep low for rate-limited servers)
# Native-HLS segment threads. Both accept a number, or 0 / null / "auto" / "max" meaning
# "no limit of my own":
#   HLS_SEGMENT_WORKERS = 12  -> exactly 12 threads (wins over everything below)
#   HLS_SEGMENT_WORKERS = 0   -> AUTO: use HLS_AUTO_WORKERS_CAP
#   HLS_AUTO_WORKERS_CAP = 24 -> AUTO uses at most 24 threads (CPU-friendly default)
#   HLS_AUTO_WORKERS_CAP = 0  -> AUTO uses the full -m connection budget (pre-3.7 behaviour;
#                                fastest on a throttled-per-connection CDN, but pins the CPU)
HLS_SEGMENT_WORKERS = 0
HLS_AUTO_WORKERS_CAP = 24
MAX_FILENAME_CHARS = 120        # cap for generated filenames (Windows paths break past ~260 chars)
PREVIEW_MAX_SECONDS = 120       # downloads shorter than this are flagged as probable previews
PREVIEW_MAX_MB = 60             # ...or smaller than this when the duration can't be read
# Paths that look like an episode/watch page — used to recognise an episode index and to visit
# those links first. Matches e.g. /episode/12, /watch/..., s01e05, /ep-3, /epizoda/2, /dil/4.
_EP_LINK_RE = re.compile(
    r'(?:episode|episod|epizod|/watch|/video/|/v/\d|season|s\d{1,2}e\d{1,2}'
    r'|[/_.\-]ep[/_.\-]?\d|[/_.\-]dil|[/_.\-]part\d|[/_.\-]e\d{1,3}(?:[/_.\-]|$))', re.I)
TEMP_SUBDIR = ".temp"             # all scratch files (.part/.lock/.parts/.merging/.video...) go here


def _auto_scan_opts(value):
    """Normalize a host's auto-config value into {'scan': idx|None, 'm': maxconn|None}. Accepted
    forms: None (plain --scan), an int N (--scan N), or a dict with any of {'scan': N, 'm': M} —
    where 'm' auto-applies -m M (max connections) to every download from that URL."""
    def _int(x):
        if isinstance(x, bool) or x is None:
            return None
        if isinstance(x, int):
            return x
        if isinstance(x, str) and x.strip().isdigit():
            return int(x)
        return None
    if isinstance(value, dict):
        return {'scan': _int(value.get('scan')), 'm': _int(value.get('m'))}
    return {'scan': _int(value), 'm': None}


def _auto_scan_match(host, hostmap):
    """Return (matched, opts) if `host` (or a parent domain of it) is listed in hostmap, where opts
    is {'scan': idx|None, 'm': maxconn|None}. An entry 'idnes.cz' matches idnes.cz and any subdomain
    (www.idnes.cz, sport.idnes.cz)."""
    if not host or not hostmap:
        return False, {'scan': None, 'm': None}
    for entry, value in hostmap.items():
        e = str(entry).lower().strip().lstrip('*').lstrip('.')
        if e and (host == e or host.endswith('.' + e)):
            return True, _auto_scan_opts(value)
    return False, {'scan': None, 'm': None}


# Keys a remote CONFIG_URL JSON may override — every genuine USER CONFIG / advanced-config knob in
# the file (they live in a few blocks: the USER CONFIG section, the network/quality/tools block
# lower down, and the YouTube/Twitch API constants). Deliberately EXCLUDED: CONFIG_URL /
# CONFIG_FETCH_TIMEOUT (no config loop); SCRIPT_VERSION / TEMP_SUBDIR (identity/internal); and all
# per-run/runtime state (AUDIO_SEL, SUB_SEL, CENC_KEYS, FORCE_CONTAINER, MP4DECRYPT, ASCII_BARS,
# BAR_FORMAT, CLR, regexes, and the derived *_HEADERS dicts — those follow USER_AGENT automatically).
_REMOTE_CONFIG_KEYS = {
    # -- USER CONFIG section --
    'DEFAULT_THREADS', 'DEFAULT_FOLDER_WORKERS', 'DEFAULT_MAX_CONNECTIONS', 'DEFAULT_CHUNK_SIZE',
    'DEFAULT_RECURSIVE', 'AUTO_COOKIES', 'USE_COLOR', 'FORCE_ASCII_BARS', 'PER_FILE_BAR_LIMIT',
    'SEGMENT_MIB', 'DROPBOX_DEFAULT_CONNECTIONS', 'AUTO_RETRIES', 'BAR_NCOLS', 'BAR_DESC_WIDTH',
    'NTFY_TOPIC', 'NTFY_SERVER', 'NTFY_TOKEN', 'SCAN_AUTO_HOSTS', 'SCAN_BROWSER_AUTO_HOSTS',
    # -- network / quality --
    'CONNECT_TIMEOUT', 'META_READ_TIMEOUT', 'DOWNLOAD_READ_TIMEOUT', 'DEFAULT_MAX_HEIGHT',
    'SCAN_LINK_CAP', 'SCAN_PAGE_WORKERS', 'SCAN_BROWSER_WAIT', 'PREVIEW_MAX_SECONDS',
    'PREVIEW_MAX_MB', 'SCAN_BROWSER_SHOW', 'SCAN_BROWSER_CLICK', 'SCAN_BROWSER_PROFILE',
    'SCAN_BROWSER_PROBE_MAX',
    'HLS_SEGMENT_WORKERS', 'HLS_AUTO_WORKERS_CAP', 'RESUME_FILE', 'MAX_FILENAME_CHARS',
    # -- external tools (paths, download URLs, install dirs) --
    'FFMPEG', 'FFMPEG_DOWNLOAD_URL', 'FFMPEG_PROGRAM_FILES_DIRS',
    'MP4DECRYPT_DOWNLOAD_URL', 'MP4DECRYPT_FALLBACK_URL', 'MP4DECRYPT_PROGRAM_FILES_DIRS',
    # -- filenames / identity --
    'ASCII_FILENAMES', 'USER_AGENT', 'TEMP_SUBDIR',
    # -- per-site request headers (handy to hot-fix when a site tightens hot-link checks) --
    'VIMEO_REFERER', 'MUX_HEADERS', 'PATREON_REFERER',
    # -- display --
    'BAR_FORMAT', 'ARROW',
    # -- filename sanitising and the rename heuristics (worth tuning per language) --
    'ILLEGAL_WIN', 'RESERVED_WIN', 'EMOJI_RANGES', 'SMALL_WORDS', 'VOWELS', 'DEFAULT_STOP',
    # -- YouTube / Twitch API constants (handy to hot-fix remotely when they rotate) --
    'YT_IOS_VERSION', 'YT_IOS_UA', 'YT_IOS_KEY', 'YT_WEB_VERSION', 'YT_WEB_KEY', 'TWITCH_CLIENT_ID',
}


# Display order for --dump-config (grouped like the config sections). Any key missing here is
# appended alphabetically, so a newly added setting can never fall out of the generated file.
_CONFIG_KEY_ORDER = (
    'DEFAULT_THREADS', 'DEFAULT_FOLDER_WORKERS', 'DEFAULT_MAX_CONNECTIONS', 'DEFAULT_CHUNK_SIZE',
    'DEFAULT_RECURSIVE', 'SEGMENT_MIB', 'DROPBOX_DEFAULT_CONNECTIONS', 'AUTO_RETRIES',
    'CONNECT_TIMEOUT', 'META_READ_TIMEOUT', 'DOWNLOAD_READ_TIMEOUT', 'DEFAULT_MAX_HEIGHT',
    'SCAN_LINK_CAP', 'SCAN_PAGE_WORKERS', 'SCAN_BROWSER_WAIT', 'SCAN_BROWSER_SHOW',
    'SCAN_BROWSER_CLICK', 'SCAN_BROWSER_PROFILE', 'SCAN_BROWSER_PROBE_MAX',
    'HLS_SEGMENT_WORKERS', 'HLS_AUTO_WORKERS_CAP',
    'PREVIEW_MAX_SECONDS', 'PREVIEW_MAX_MB', 'RESUME_FILE', 'MAX_FILENAME_CHARS',
    'FFMPEG', 'FFMPEG_DOWNLOAD_URL', 'FFMPEG_PROGRAM_FILES_DIRS',
    'MP4DECRYPT_DOWNLOAD_URL', 'MP4DECRYPT_FALLBACK_URL', 'MP4DECRYPT_PROGRAM_FILES_DIRS',
    'YT_IOS_VERSION', 'YT_IOS_UA', 'YT_IOS_KEY', 'YT_WEB_VERSION', 'YT_WEB_KEY', 'TWITCH_CLIENT_ID',
    'USE_COLOR', 'FORCE_ASCII_BARS', 'PER_FILE_BAR_LIMIT', 'BAR_NCOLS', 'BAR_DESC_WIDTH',
    'ASCII_FILENAMES', 'AUTO_COOKIES', 'NTFY_TOPIC', 'NTFY_SERVER', 'NTFY_TOKEN', 'USER_AGENT',
    'SCAN_AUTO_HOSTS', 'SCAN_BROWSER_AUTO_HOSTS',
    'TEMP_SUBDIR', 'BAR_FORMAT', 'ARROW',
    'VIMEO_REFERER', 'MUX_HEADERS', 'PATREON_REFERER',
    'ILLEGAL_WIN', 'RESERVED_WIN', 'EMOJI_RANGES', 'SMALL_WORDS', 'VOWELS', 'DEFAULT_STOP',
)


# Constants that must NEVER come from a remote config, with the reason. Everything else that
# looks like a setting has to be in _REMOTE_CONFIG_KEYS — _audit_config_keys() enforces exactly
# that, so a constant added later cannot silently fall out of the configurable set.
_NEVER_REMOTE_KEYS = {
    'CONFIG_URL': "would let a config redirect to another config (loop / hijack)",
    'CONFIG_FETCH_TIMEOUT': "already spent by the time the config arrives",
    'INSECURE_TLS': "a config must not be able to switch off the checks protecting itself",
    'CONFIG_TRUST_UNVERIFIED': "a config must not be able to grant itself more trust",
    'CONFIG_VERIFY_TLS': "guards the config fetch itself — a config must not relax its own checks",
    'CONFIG_CA_BUNDLE': "guards the config fetch itself",
    'CONFIG_PIN_SHA256': "guards the config fetch itself",
    'SCRIPT_VERSION': "identity, must not be spoofable",
    # Runtime state, filled in while running — not settings.
    'CLR': "runtime palette object built by setup_console()",
    'ASCII_BARS': "runtime, derived from FORCE_ASCII_BARS by setup_console()",
    'MP4DECRYPT': "runtime cache of the resolved executable path",
    # Per-run choices that come from the command line.
    'AUDIO_SEL': "per-run (--audio)", 'SUB_SEL': "per-run (--sub)",
    'SUBS_ONLY': "per-run (--subs-only)", 'MUSE_PASSWORD': "per-run (--password)",
    'CENC_KEYS': "per-run (--key/--keys)", 'FORCE_CONTAINER': "per-run (--container)",
    # Derived automatically from USER_AGENT / YT_IOS_UA / VIMEO_REFERER after a config load.
    'STREAMABLE_HEADERS': "derived from USER_AGENT", 'TWITCH_HEADERS': "derived from USER_AGENT",
    'YOUTUBE_HEADERS': "derived from YT_IOS_UA", 'VIMEO_HEADERS': "derived from VIMEO_REFERER",
    # Compiled regexes: a JSON string cannot become a compiled pattern safely.
    'NUM_RE': "compiled regex", 'WORDCHARS': "compiled regex", 'TOKEN_RE': "compiled regex",
}


def _module_constants():
    """Every public ALL_CAPS module-level name that holds a value (i.e. looks like a setting)."""
    import types
    out = {}
    for name, val in list(globals().items()):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', name):
            continue
        if isinstance(val, (types.FunctionType, types.ModuleType, type)):
            continue
        out[name] = val
    return out


def _audit_config_keys():
    """Return (unclassified, stale): constants that are neither overridable nor deliberately
    excluded, and allowlist entries that no longer exist."""
    consts = _module_constants()
    unclassified = sorted(n for n in consts
                          if n not in _REMOTE_CONFIG_KEYS and n not in _NEVER_REMOTE_KEYS)
    stale = sorted(k for k in _REMOTE_CONFIG_KEYS if k not in consts)
    return unclassified, stale


def dump_config_file(path):
    """Write a JSON file holding EVERY remotely-overridable setting with this script's current
    values — a ready-to-edit template for CONFIG_URL. Generated from the allowlist itself, so it
    can never drift out of sync with the code."""
    keys = [k for k in _CONFIG_KEY_ORDER if k in _REMOTE_CONFIG_KEYS]
    keys += sorted(k for k in _REMOTE_CONFIG_KEYS if k not in _CONFIG_KEY_ORDER)
    data = {k: _json_ready(globals().get(k)) for k in keys}
    unclassified, stale = _audit_config_keys()
    if unclassified:
        print(f"{CLR.YELLOW}[WARN]{CLR.RESET} These constants are neither remotely overridable "
              f"nor listed as deliberately excluded: {', '.join(unclassified)}")
    if stale:
        print(f"{CLR.YELLOW}[WARN]{CLR.RESET} These config keys no longer exist in the script: "
              f"{', '.join(stale)}")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[INFO] Wrote {len(data)} setting(s) to {path} (v{SCRIPT_VERSION}). "
              "Upload it to your CONFIG_URL and delete the keys you don't want to manage remotely.")
        return True
    except OSError as e:
        print(f"[ERROR] Could not write {path}: {e}")
        return False


def _as_worker_setting(value):
    """Normalise a worker knob: a positive number stays a number; 0 / null / "auto" / "max" /
    "unlimited" (any case) mean "no limit from me" and return None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ('', 'auto', 'max', 'none', 'off', 'unlimited', 'all'):
            return None
        try:
            value = int(v)
        except ValueError:
            return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _hls_worker_count(max_connections):
    """How many parallel segment threads native HLS should use (see the knobs at the top)."""
    forced = _as_worker_setting(HLS_SEGMENT_WORKERS)
    if forced:
        return max(1, min(forced, max_connections))
    cap = _as_worker_setting(HLS_AUTO_WORKERS_CAP)
    if cap is None:                       # 0 / "auto" -> use the whole -m budget
        return max(1, max_connections)
    return max(1, min(max_connections, max(4, cap)))


# Keys whose value may be a word ("auto") as well as a number, so they skip type coercion.
_FLEXIBLE_CONFIG_KEYS = {'HLS_SEGMENT_WORKERS', 'HLS_AUTO_WORKERS_CAP'}

# Keys that decide WHICH BINARY gets run or downloaded-and-run. Over an unauthenticated plain-http
# CONFIG_URL anyone on the network path can forge the response, so these are ignored there; every
# other setting is harmless enough to accept. Use https:// to manage them remotely.
_EXEC_SENSITIVE_CONFIG_KEYS = {
    'FFMPEG', 'FFMPEG_DOWNLOAD_URL', 'FFMPEG_PROGRAM_FILES_DIRS',
    'MP4DECRYPT_DOWNLOAD_URL', 'MP4DECRYPT_FALLBACK_URL', 'MP4DECRYPT_PROGRAM_FILES_DIRS',
}


def _coerce_config_value(current, value):
    """Best-effort coerce a JSON value to the type of the existing default (e.g. "5" -> 5).

    JSON has no set and no tuple, so a setting that is one locally arrives as a list and has to
    be converted back — otherwise e.g. ILLEGAL_WIN would silently become a list and every
    `ch in ILLEGAL_WIN` test would still work but `.add()` would not."""
    if isinstance(current, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(current, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, set):
        if isinstance(value, (list, tuple, set)):
            return set(value)
        raise TypeError("expected a list of values")
    if isinstance(current, tuple) and isinstance(value, list):
        return tuple(value)
    if isinstance(current, list) and isinstance(value, list):
        # Keep an inner tuple shape (EMOJI_RANGES is a list of (lo, hi) pairs).
        if current and isinstance(current[0], tuple):
            return [tuple(x) if isinstance(x, (list, tuple)) else x for x in value]
        return list(value)
    return value


def _json_ready(value):
    """Make a setting JSON-serialisable (sets have no JSON form; emit them sorted for a stable
    diff between generated config files)."""
    if isinstance(value, set):
        try:
            return sorted(value)
        except TypeError:
            return list(value)
    if isinstance(value, tuple):
        return [_json_ready(v) for v in value]
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    return value


_INSECURE_TLS_ACTIVE = False


def _apply_insecure_tls(announce=True):
    """Turn off certificate verification for EVERY connection this process makes.

    Patched in one place rather than at each call site, so nothing can be missed: requests (every
    Session, however it was built) and the standard library's default HTTPS context both stop
    verifying. Safe to call twice. Returns True if it is now active.

    Not covered, and not coverable from here: ffmpeg/ffprobe already accept any certificate by
    default (tls_verify is off), and the --scan-browser webview follows the system's own trust
    settings — it may still show a certificate warning you have to accept once."""
    global _INSECURE_TLS_ACTIVE
    if _INSECURE_TLS_ACTIVE:
        return True
    _INSECURE_TLS_ACTIVE = True
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    try:
        _orig_request = requests.Session.request

        def _request(self, method, url, **kw):
            # A string means the caller named a specific CA bundle to check against; honour that,
            # otherwise the connection would be reported as verified when it was not. Everything
            # else (unset, True, False) becomes "do not check".
            if not isinstance(kw.get('verify'), str):
                kw['verify'] = False
            return _orig_request(self, method, url, **kw)

        requests.Session.request = _request
    except Exception:
        pass
    try:
        import ssl as _ssl
        # Two different entry points: urllib.request uses the private one, while other libraries
        # call the public factory. Patch both, or "everywhere" would not be everywhere.
        _ssl._create_default_https_context = _ssl._create_unverified_context
        _orig_ctx = _ssl.create_default_context

        def _unverified_context(*a, **kw):
            c = _orig_ctx(*a, **kw)
            c.check_hostname = False        # must come first: CERT_NONE with hostname checks raises
            c.verify_mode = _ssl.CERT_NONE
            return c

        _ssl.create_default_context = _unverified_context
    except Exception:
        pass
    if announce:
        print(f"{CLR.DIM}[INFO] TLS certificate checking is off (INSECURE_TLS); bad or self-signed "
              f"certificates are accepted everywhere.{CLR.RESET}")
    return True


def _cert_fingerprint(url):
    """SHA-256 fingerprint of whatever certificate the host presents, or None.

    Used to TELL the user the value to pin after an SSLError — pinning authenticates a
    self-signed certificate properly, instead of turning verification off altogether."""
    import socket
    import ssl
    import hashlib
    pu = urlparse(url)
    host, port = pu.hostname, pu.port or 443
    if not host:
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=CONFIG_FETCH_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
        if not der:
            return None
        h = hashlib.sha256(der).hexdigest().upper()
        return ':'.join(h[i:i + 2] for i in range(0, len(h), 2))
    except (OSError, ValueError):
        return None


class _PinnedAdapter(HTTPAdapter):
    """HTTPS adapter that accepts exactly one certificate, identified by its SHA-256 fingerprint.

    This is real authentication for a self-signed certificate: the CA chain is irrelevant, but a
    different certificate (an impersonator) is rejected outright."""

    def __init__(self, fingerprint, **kw):
        self._fp = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        kw['assert_fingerprint'] = self._fp
        kw['cert_reqs'] = 'CERT_NONE'          # the fingerprint IS the check
        return super().init_poolmanager(connections, maxsize, block, **kw)


def _config_fetch(url):
    """Fetch the remote config honouring the TLS settings above.

    Returns (response, authenticated, note). `authenticated` says whether we actually know we
    talked to the right server — that is what decides if the program-selecting settings are
    allowed through."""
    is_https = url.lower().startswith('https://')
    pin = (CONFIG_PIN_SHA256 or '').replace(':', '').replace(' ', '').strip().lower()
    sess = requests.Session()
    verify = True
    note = ''
    if not is_https:
        return (sess.get(url, timeout=(CONFIG_FETCH_TIMEOUT, CONFIG_FETCH_TIMEOUT),
                         headers={'User-Agent': USER_AGENT}),
                False, 'plain http')
    if (_INSECURE_TLS_ACTIVE or INSECURE_TLS) and not pin and not CONFIG_CA_BUNDLE:
        # Global bypass is on: fetch it, no matter what the certificate looks like.
        return (sess.get(url, timeout=(CONFIG_FETCH_TIMEOUT, CONFIG_FETCH_TIMEOUT),
                         headers={'User-Agent': USER_AGENT}),
                bool(CONFIG_TRUST_UNVERIFIED), 'certificate NOT checked (INSECURE_TLS)')
    if pin:
        sess.mount('https://', _PinnedAdapter(pin))
        verify = False                          # the pin replaces CA verification
        note = 'pinned certificate'
        authenticated = True
    elif CONFIG_CA_BUNDLE:
        verify = CONFIG_CA_BUNDLE
        note = f'CA bundle {CONFIG_CA_BUNDLE}'
        authenticated = True
    elif not CONFIG_VERIFY_TLS:
        verify = False
        note = 'certificate NOT verified'
        authenticated = bool(CONFIG_TRUST_UNVERIFIED)
        try:                                    # our choice, so don't spam the warning
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    else:
        note = 'verified certificate'
        authenticated = True
    r = sess.get(url, timeout=(CONFIG_FETCH_TIMEOUT, CONFIG_FETCH_TIMEOUT),
                 headers={'User-Agent': USER_AGENT}, verify=verify)
    return r, authenticated, note


def _apply_remote_config():
    """If CONFIG_URL is set, fetch a JSON of overrides and apply them ON TOP of the USER CONFIG
    defaults — before argparse reads them, so the command line still wins. Keys the JSON omits keep
    their local value; CONFIG_URL can't be overridden (no loop). Any problem (empty URL, timeout,
    network error, non-JSON, wrong shape) falls back silently to local defaults; the short timeout
    guarantees an unreachable URL can't hang startup."""
    url = (CONFIG_URL or "").strip()
    if not url:
        return
    # Honour the setting even when the script was entered some other way than __main__ (imported,
    # or a future entry point) — otherwise the constant would say True while nothing acted on it.
    if INSECURE_TLS and not _INSECURE_TLS_ACTIVE:
        _apply_insecure_tls(announce=False)
    authenticated, tls_note = False, ''
    try:
        r, authenticated, tls_note = _config_fetch(url)
        if r.status_code != 200:
            print(f"[WARN] Config: {url} -> HTTP {r.status_code}; using local USER CONFIG only.")
            return
        data = r.json()
    except requests.exceptions.SSLError as e:
        # A NAS almost always has a self-signed certificate. Say exactly what to do, and hand over
        # the fingerprint so the good option is a copy-paste away.
        print(f"[WARN] Config: {url} — the certificate could not be verified ({type(e).__name__}).")
        fp = _cert_fingerprint(url)
        if fp:
            print("       The server presented a certificate with this SHA-256 fingerprint:")
            print(f"         {fp}")
            print("       If that is your NAS, put it near the top of the script as:")
            print(f'         CONFIG_PIN_SHA256 = "{fp}"')
            print("       That authenticates the self-signed certificate, so every setting stays "
                  "usable.")
        print("       Alternatives: CONFIG_CA_BUNDLE = \"path/to/nas.pem\", or switch checking "
              "off entirely with INSECURE_TLS = True (or --insecure).")
        print("[WARN] Config: using local USER CONFIG only.")
        return
    except requests.RequestException as e:
        print(f"[WARN] Config: {url} unreachable ({type(e).__name__}); using local USER CONFIG only.")
        return
    except ValueError:
        print(f"[WARN] Config: {url} is not valid JSON; using local USER CONFIG only.")
        return
    if not isinstance(data, dict):
        print(f"[WARN] Config: {url} must be a JSON object; using local USER CONFIG only.")
        return
    # Unauthenticated (plain http, or https with verification switched off) means anyone on the
    # network path could have written this response, so the settings that decide which program
    # gets executed stay off limits.
    insecure = not authenticated
    applied, skipped, blocked = [], [], []
    for key, value in data.items():
        if key in ('CONFIG_URL', 'CONFIG_FETCH_TIMEOUT') or key not in _REMOTE_CONFIG_KEYS:
            skipped.append(key)
            continue
        if insecure and key in _EXEC_SENSITIVE_CONFIG_KEYS:
            blocked.append(key)
            continue
        try:
            globals()[key] = (value if key in _FLEXIBLE_CONFIG_KEYS
                              else _coerce_config_value(globals().get(key), value))
            applied.append(key)
        except (ValueError, TypeError):
            print(f"[WARN] Config: bad value for {key}; keeping local default.")
    # Keep the derived header dicts in sync if USER_AGENT / YT_IOS_UA were overridden.
    for hname, uakey in (('STREAMABLE_HEADERS', 'USER_AGENT'), ('TWITCH_HEADERS', 'USER_AGENT'),
                         ('YOUTUBE_HEADERS', 'YT_IOS_UA')):
        h = globals().get(hname)
        if isinstance(h, dict) and 'User-Agent' in h:
            h['User-Agent'] = globals().get(uakey, h['User-Agent'])
    # VIMEO_HEADERS is built from VIMEO_REFERER, so rebuild it if that changed.
    if 'VIMEO_REFERER' in applied:
        ref = globals().get('VIMEO_REFERER') or ''
        globals()['VIMEO_HEADERS'] = {'Referer': ref,
                                      'Origin': ref.rstrip('/') or 'https://player.vimeo.com'}
    print(f"[INFO] Config: loaded from {url} ({tls_note}) — {len(applied)} override(s) applied "
          f"over local USER CONFIG"
          + (f", {len(skipped)} unknown key(s) ignored" if skipped else "") + ".")
    if blocked:
        why = ("is plain http" if url.lower().startswith('http://')
               else "was fetched without verifying the certificate")
        print(f"[WARN] Config: ignored {len(blocked)} key(s) that choose which program to run "
              f"({', '.join(sorted(blocked))}) because the connection {why} and could be forged.")
        print("       Set CONFIG_PIN_SHA256 (the script prints the fingerprint for you) or "
              "CONFIG_CA_BUNDLE to enable them properly,")
        print("       or CONFIG_TRUST_UNVERIFIED = True to allow them from an unverified "
              "connection anyway.")


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


def _detect_previews(files, verbose=False):
    """Spot downloads that are suspiciously small/short — typically Patreon PREVIEW clips (~30 s)
    served when the post is locked to a higher tier. Returns [(path, seconds_or_None, size)].
    A file is flagged when its duration is under PREVIEW_MAX_SECONDS, or (when the duration can't
    be read) when it is both under PREVIEW_MAX_MB and far smaller than the rest of the batch."""
    infos = []
    for f in files or ():
        try:
            if os.path.isfile(f):
                infos.append((f, os.path.getsize(f)))
        except OSError:
            pass
    if not infos:
        return []
    flagged = []
    sizes = sorted(s for _f, s in infos)
    median = sizes[len(sizes) // 2]
    for f, size in infos:
        secs = _ffprobe_duration(f, None, False)
        if secs and secs > 0:
            if secs <= PREVIEW_MAX_SECONDS:
                flagged.append((f, secs, size))
            continue
        # No readable duration: only a file that is BOTH small in absolute terms AND far
        # smaller than its siblings is suspicious. A lone small file is not evidence of
        # anything (plenty of legitimate videos are under PREVIEW_MAX_MB), so don't cry wolf.
        if len(infos) > 1 and size <= PREVIEW_MAX_MB * 1024 * 1024 and size * 5 < median:
            flagged.append((f, None, size))
    return flagged


def _report_previews(files, verbose=False):
    """Warn about likely preview downloads and return a short human-readable note (or '')."""
    flagged = _detect_previews(files, verbose)
    if not flagged:
        return ''
    print(f"{CLR.YELLOW}[WARN]{CLR.RESET} {len(flagged)} downloaded file(s) look like PREVIEWS, "
          f"not full videos (locked to a higher tier?):")
    for f, secs, size in flagged[:20]:
        length = f"{int(secs // 60)}:{int(secs % 60):02d}" if secs else "unknown length"
        print(f"        {os.path.basename(f)}  —  {length}, {size / 1024 / 1024:.1f} MiB")
    if len(flagged) > 20:
        print(f"        ... and {len(flagged) - 20} more")
    print("        Check your pledge tier for these posts, then delete the previews and re-run.")
    return (f"{len(flagged)} file(s) look like previews (~"
            f"{int(min((s for _f, s, _z in flagged if s), default=PREVIEW_MAX_SECONDS))}s) — "
            "probably locked to a higher tier")


def _cleanup_temp_dir(directory: str) -> None:
    """Remove the .temp folder if it is now empty (called after a run finishes). Leftover
    parts from an interrupted run keep it around so a re-run can resume."""
    tdir = os.path.join(directory or ".", TEMP_SUBDIR)
    try:
        if os.path.isdir(tdir) and not os.listdir(tdir):
            os.rmdir(tdir)
    except OSError:
        pass
# =============================================================================
#  ADVANCED CONFIG — network timeouts, quality default, and external-tool paths/URLs.
#  These are also overridable via CONFIG_URL (see the USER CONFIG section up top).
# =============================================================================
# Network timeouts (seconds). Without these a stalled connection (e.g. Google's videoplayback
# CDN ignoring a HEAD request) would hang the whole run forever.
CONNECT_TIMEOUT = 15              # max time to establish a TCP/TLS connection
META_READ_TIMEOUT = 30           # read timeout for small metadata/probe/listing requests
DOWNLOAD_READ_TIMEOUT = 120      # max gap between received chunks during an actual download
# --- Patreon-native (Vimeo/Mux HLS) settings ---
DEFAULT_MAX_HEIGHT = 0            # -q : cap HLS video height (e.g. 720). 0 = best available.
FFMPEG = "ffmpeg"                 # ffmpeg exe, a FOLDER containing it, or "ffmpeg" if on PATH
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
# Extra install locations searched (depth-limited) BEFORE any download. PATH is always tried
# first. On Linux/macOS the system dirs are already on PATH, so downloading is not attempted
# there (the URL above is a Windows build) — an install hint is shown instead.
FFMPEG_PROGRAM_FILES_DIRS = [
    r"C:\Program Files\ffmpeg",
    r"C:\Program Files (x86)\ffmpeg",
]
# mp4decrypt (Bento4) — for decrypting CENC/Widevine with a key you already hold. Same handling
# as ffmpeg: PATH -> cache -> install folders -> download (NAS first, then the internet).
# SECURITY: this archive is unpacked and EXECUTED, so it must not travel over plain http.
MP4DECRYPT_DOWNLOAD_URL = "https://nas.falco81.net/mp4decrypt.zip"
MP4DECRYPT_FALLBACK_URL = ("https://github.com/axiomatic-systems/Bento4/releases/download/"
                           "v1.6.0-641/Bento4-SDK-1-6-0-641.x86_64-microsoft-win32.zip")
MP4DECRYPT_PROGRAM_FILES_DIRS = [
    r"C:\Program Files\mp4decrypt",
    r"C:\Program Files (x86)\mp4decrypt",
]
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
            self.MAGENTA = '\033[35m'; self.BRIGHT = '\033[1m'
        else:
            self.RESET = self.RED = self.GREEN = self.YELLOW = self.CYAN = self.DIM = ''
            self.MAGENTA = self.BRIGHT = ''


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


def _log_source(module, detail=None):
    """Announce which downloader/source module is handling this download. Called on every download
    path so the log always shows the engine in use (e.g. 'YouTube — DASH via InnerTube')."""
    try:
        tag = f"{CLR.CYAN}{module}{CLR.RESET}"
    except Exception:
        tag = module
    print(f"[INFO] Downloader: {tag}" + (f" — {detail}" if detail else ""))


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
    # Remember the ABSOLUTE path: downloads run with the CWD switched into -d/--output-dir, but
    # the atexit release happens after it has been switched back, so a relative path would then
    # point at the wrong directory and leave the real lock behind.
    lock_path = os.path.abspath(lock_path)
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

            if owner == os.getpid():
                # Our own lock: two items in this run resolved to the same file name.
                print(f"[WARN] Two downloads in this run map to the same file name "
                      f"({os.path.basename(lock_path)[:-5]}); keeping only the first.")
                print("       Use -o to give one of them a different name.")
                return False

            if not _pid_alive(owner):
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
    lock_path = os.path.abspath(lock_path)   # must match how acquire_lock() stored it
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
            # Keep the flags the export carries: the browser needs them to store the cookie the
            # same way the original site set it (and HttpOnly decides whether it can be injected
            # from JavaScript at all).
            rest = {'HttpOnly': ''} if cookie.get("httpOnly") or cookie.get("httponly") else {}
            jar.set(name, value, domain=domain, path=path, expires=expires,
                    secure=bool(cookie.get("secure")), rest=rest)
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
        # Carry the flags across. The old code copied only name/value/domain/path, which quietly
        # turned every Secure cookie into a plain one, dropped every expiry, and lost the
        # HttpOnly marker that --scan-browser needs to know how a cookie can be injected.
        requests_jar.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain,
            path=cookie.path,
            secure=bool(cookie.secure),
            expires=cookie.expires,
            rest=dict(getattr(cookie, '_rest', None) or {}),
        )

    return requests_jar

class _ColorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """argparse help with colour, and with the description/epilog printed exactly as written.

    Colour comes from the same palette as the rest of the script, so --no-color and a redirected
    stdout switch it off here too (setup_console() decides, and it runs before --help is handled)."""

    def __init__(self, prog, **kw):
        kw.setdefault('max_help_position', 34)
        kw.setdefault('width', min(shutil.get_terminal_size((100, 24)).columns - 2, 108))
        super().__init__(prog, **kw)

    def start_section(self, heading):
        if heading:
            heading = f"{CLR.BRIGHT}{CLR.CYAN}{heading.upper()}{CLR.RESET}"
        return super().start_section(heading)

    def _format_action(self, action):
        # Colour is applied AFTER argparse has done its layout. Colouring inside
        # _format_action_invocation would make len() count the escape sequences as visible
        # characters, and every help column would line up wrongly.
        text = super()._format_action(action)
        if not CLR.RESET:
            return text
        inv = self._format_action_invocation(action)
        lines = text.split('\n')
        if inv and lines and inv in lines[0]:
            painted = re.sub(r'(--?[A-Za-z][\w-]*)', f'{CLR.GREEN}\\1{CLR.RESET}', inv)
            lines[0] = lines[0].replace(inv, painted, 1)
        return '\n'.join(lines)

    def _get_help_string(self, action):
        return action.help


def _help_text_blocks():
    """The prose parts of --help: what the tool does, how a session usually goes, and the exact
    layout of every file it reads. Returned as (description, epilog)."""
    B, R, C, D, Y = CLR.BRIGHT, CLR.RESET, CLR.CYAN, CLR.DIM, CLR.YELLOW

    def h(t):
        return f"{B}{C}{t}{R}"

    description = f"""
{B}videoloader_dir.py v{SCRIPT_VERSION}{R} — one downloader for Google Drive, Dropbox, Patreon,
YouTube, Twitch, Vimeo, Streamable, Vidyard, muse.ai, Viki and any page carrying an
HLS/DASH stream.

{h('HOW A SESSION USUALLY GOES')}
  {B}1.{R} {B}Point it at something.{R} A Drive folder, a Dropbox link, a Patreon post, a YouTube
     playlist — just paste the address. Nothing else is needed for the common cases:
       {D}videoloader_dir.py https://drive.google.com/drive/folders/XXXX{R}

  {B}2.{R} {B}If the page is an ordinary web page{R}, add --scan so it looks inside for video:
       {D}videoloader_dir.py --scan https://web.cz/serial/dil-1{R}
     Nothing found? The player probably builds the address in JavaScript — use --scan-browser,
     which opens a real browser, presses play and watches what the player fetches.

  {B}3.{R} {B}Locked behind a login?{R} Export your cookies and pass them with --cookies, or run
     --browser-login once and sign in by hand; the session is remembered afterwards.

  {B}4.{R} {B}Encrypted (DRM) stream?{R} Supply the key you already hold with --key KID:KEY.

  {B}5.{R} {B}Many videos at once?{R} Put them in a list and use --davka (each line group names the
     file, its stream and its key) or --url-list (one address per line). Both can be re-run
     safely: anything already downloaded is skipped, so an interrupted batch just continues.

  {B}6.{R} {B}When it finishes{R} it offers to tidy the episode names, writes a report next to the
     files, and can push a notification (--test-notify to check that side works).
"""

    epilog = f"""
{h('FILE FORMATS')}

{B}--davka FILE{R} / {B}--davka-browser FILE{R}
  Blocks of up to three lines per video. Blank lines and lines starting with # are ignored.
  Lines are recognised by their SHAPE, not their position, so a stray blank line cannot shift
  the file, and the key may be left out for an unencrypted stream.

    {D}# name the finished file should get{R}
    Serial S01E01
    {D}# the stream (--davka) or the PAGE holding it (--davka-browser){R}
    https://cdn.example/asset.ism/.mpd
    {D}# the key you already hold — optional, and several may follow one address{R}
    26dc8baa20b557a47c9f10b43f0a6fad:4ad13f26045246112815b49c6da3ed0f

    Serial S01E02
    https://cdn.example/asset2.ism/.mpd

  With --davka-browser the middle line is a normal page address: it is opened in a real
  browser, the stream its player fetches is discovered, and that is what gets downloaded.
  An address that already points at a .mpd/.m3u8/file is used as-is, so a list may mix both.

{B}--url-list FILE{R}
  One address per line; # comments and blank lines ignored. Finished entries are commented
  out in place as the run goes, so re-running continues where it stopped.
  A .json file is accepted too: a list of strings, or of objects with a "url" field.

{B}--keys FILE{R}
  One key per line, {D}KID:KEY{R} or a bare 32-character KEY. # comments allowed.

{B}--cookies FILE{R}
  Either a Netscape cookies.txt or a browser-extension JSON export. Both keep their Secure
  and HttpOnly flags. Without --cookies the script looks for a cookie file next to itself.

{B}CONFIG_URL{R} {D}(a JSON file served on your network){R}
  Central settings for every machine running this script. Generate the template with
  {D}--dump-config{R}, delete whatever you do not want to manage centrally, and serve the rest.
  Missing keys simply keep their local value.

{h('EXAMPLES')}
  {D}# a whole Drive folder into a chosen directory, 1080p at most{R}
  videoloader_dir.py https://drive.google.com/drive/folders/XXXX -d D:\\Video -q 1080

  {D}# find the video on an ordinary page, letting its JavaScript run{R}
  videoloader_dir.py --scan-browser https://web.cz/serial/dil-1

  {D}# see what is on the page without downloading anything{R}
  videoloader_dir.py --scan https://web.cz/serial/dil-1 --list

  {D}# a batch of episodes, each with its own decryption key{R}
  videoloader_dir.py --davka serial.txt -d D:\\Serialy

  {D}# sign in once; later --scan-browser runs on that site stay logged in{R}
  videoloader_dir.py --browser-login https://web.cz/

  {D}# retry whatever failed in the previous run{R}
  videoloader_dir.py --resume

{h('GOOD TO KNOW')}
  {Y}Interrupting is safe.{R} Work in progress lives in .temp/ and the final name only appears
  once a file is complete, so Ctrl+C never leaves a half file looking finished. Run the same
  command again and it picks up where it stopped.

  {Y}Speed{R} is governed by -m (total connections), not by -t or -w: all files share one pool
  and a freed connection moves straight to whatever is still downloading.

  {Y}ffmpeg{R} is fetched automatically if it is missing. DRM also needs mp4decrypt (Bento4).

  {Y}Certificates{R} are not verified by default (INSECURE_TLS), so a self-signed server on your
  own network just works. Set it to False at the top of the script for strict checking.
"""
    return description, epilog


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
    """Interactive multi-select of which cookie file(s) to load (arrow-key checklist with search +
    scrollbar; numbered fallback). Defaults to the newest. Returns a list of paths."""
    labels = [f"{os.path.basename(p)}   {CLR.DIM}({_format_mtime(p)}){CLR.RESET}"
              for p in candidates]
    idxs = tui_select_many(f"Select cookie file(s) to use — {len(candidates)} found",
                           labels, preselected=[0],
                           header=[f"{CLR.CYAN}JSON cookie files in this folder "
                                   f"(newest first){CLR.RESET}"])
    if not idxs:
        return [candidates[0]]
    return [candidates[i] for i in idxs]


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
_STREAMABLE_URL_RE = re.compile(r'https?://(?:www\.)?streamable\.com/[^\s"\'<>\\)]+', re.IGNORECASE)


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


def get_creator_name(campaign_id, session: requests.Session, verbose: bool = False):
    """Best-effort Patreon creator/campaign display name via the /api/campaigns/{id} endpoint.
    Isolated and defensive: any failure just returns None (never affects the download)."""
    if not campaign_id:
        return None
    url = (f"https://www.patreon.com/api/campaigns/{campaign_id}"
           f"?json-api-version=1.0&json-api-use-default-includes=false")
    try:
        r = session.get(url, headers={'Referer': PATREON_REFERER, 'Accept': 'application/json'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        d = r.json()
        return _creator_from_attrs((d.get('data') or {}).get('attributes') or {})
    except Exception:
        return None


def get_collection_info(collection_id: str, session: requests.Session, verbose: bool):
    """Best-effort (title, campaign_id) for a Patreon collection. Both may be None. Also sets
    the module-level creator name for display."""
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
    global _patreon_creator
    _patreon_creator = get_creator_name(campaign_id, session, verbose)
    if verbose:
        print(f"[INFO] Patreon collection '{title}', campaign {campaign_id}, "
              f"creator {_patreon_creator}")
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


def _post_plain_text(post: dict) -> str:
    """All human-readable text of a post (rich-text doc + teaser + html body), so things written in
    the post — like 'Password: FirstKiss?' — can be found."""
    a = post.get('attributes', {}) or {}
    out = []
    for key in ('content_json_string', 'teaser_text_json_string'):
        blob = a.get(key)
        if isinstance(blob, str) and blob:
            try:
                doc = json.loads(blob)
            except Exception:
                out.append(blob)
                continue

            def walk(node):
                if isinstance(node, dict):
                    if node.get('type') == 'text' and isinstance(node.get('text'), str):
                        out.append(node['text'])
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(doc)
    for key in ('content', 'content_teaser_text', 'cleaned_teaser_text', 'title'):
        v = a.get(key)
        if isinstance(v, str) and v:
            out.append(re.sub(r'<[^>]+>', ' ', v))
    return ' '.join(out)


def extract_vidyard_links_from_post(post: dict) -> list:
    """Vidyard video links anywhere in a post."""
    return _extract_links_from_post(post, ('vidyard.com',),
                                    (re.compile(r'https?://[^\s"\'<>\\)]+'),))


def extract_muse_links_from_post(post: dict) -> list:
    """muse.ai / skiv.com video links anywhere in a post."""
    return _extract_links_from_post(post, ('muse.ai', 'skiv.com'),
                                    (re.compile(r'https?://[^\s"\'<>\\)]+'),))


def extract_streamable_links_from_post(post: dict) -> list:
    """Return an ordered, de-duplicated list of Streamable URLs found anywhere in a post."""
    return _extract_links_from_post(post, ('streamable.com',), (_STREAMABLE_URL_RE,))


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


def _dropbox_raw_variant(url: str) -> str:
    """Alternative direct form: drop dl and use raw=1 (bypasses Dropbox's preview page for
    public links), keeping rlkey. Used as a fallback when dl=1 returns an HTML page."""
    from urllib.parse import parse_qsl, urlencode as _ue, urlunparse
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q.pop('dl', None)
    q['raw'] = '1'
    return urlunparse(parsed._replace(query=_ue(q)))


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


def _dropbox_probe(url: str, session: requests.Session, extra_headers: dict = None):
    """One ranged GET that returns (size_bytes, real_filename) for a direct URL: total size from
    Content-Range, real name from Content-Disposition (or the final URL). `extra_headers` carries a
    source's required headers (e.g. Vidyard's Referer) — without them a signed CDN can refuse the
    range request, and an unknown size forces a slow single-connection download."""
    size, name, saw_html = 0, '', False
    working = url
    base_h = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    base_h.update(extra_headers or {})
    for variant in (url, _dropbox_raw_variant(url)):
        try:
            with connection_slot():
                with session.get(variant, stream=True, allow_redirects=True,
                                 headers=dict(base_h, **{'Range': 'bytes=0-0'}),
                                 timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)) as r:
                    cr = r.headers.get('content-range', '')
                    if '/' in cr and cr.rsplit('/', 1)[-1].strip().isdigit():
                        size = int(cr.rsplit('/', 1)[-1].strip())
                    elif r.status_code == 200 and (r.headers.get('content-length', '') or '').isdigit():
                        size = int(r.headers['content-length'])
                    if not name:
                        name = _parse_content_disposition_filename(
                            r.headers.get('Content-Disposition', '')) or \
                            unquote(os.path.basename(urlparse(r.url).path))
                    if 'text/html' in (r.headers.get('Content-Type') or '').lower():
                        saw_html = True
                    else:
                        saw_html = False
                        working = variant          # this variant serves the file
                        if size:
                            return size, name, variant
        except requests.RequestException:
            pass
    if not size:                       # some CDNs ignore Range -> ask for the size directly
        try:
            with connection_slot():
                h = session.head(working, allow_redirects=True, headers=base_h,
                                 timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
            cl = (h.headers.get('content-length') or '').strip()
            if cl.isdigit() and int(cl) > 1:
                size = int(cl)
                name = name or _parse_content_disposition_filename(
                    h.headers.get('Content-Disposition', '')) or \
                    unquote(os.path.basename(urlparse(h.url).path))
        except requests.RequestException:
            pass
    if saw_html and not size:
        tqdm.write(f"[WARN] Dropbox returned an HTML page (not a file) for '{name or url}'. "
                   f"The link may point to a folder, be rate-limited, or be download-blocked; "
                   f"it will be reported as failed, not saved.")
    return size, name, working


def _download_from_posts(posts, session, chunk_size, num_threads, folder_workers, recursive,
                         verbose, select, out_dir, max_connections, max_height, list_only):
    """Given a list of Patreon post objects, gather every Drive/Dropbox link and native
    (Vimeo/Mux) stream, then download them with the shared pooled engines. Shared by the
    collection flow and the single-post flow."""
    drive_videos = []
    dropbox_items = []   # {'url','filename','title'}
    streamable_items = []  # {'shortcode','title'}
    muse_items = []        # {'svid','title','password'}
    seen_muse = set()
    vidyard_items = []     # {'uuid','title'}
    seen_vidyard = set()
    hls_videos = []      # stream descriptors
    seen_ids = set()
    seen_dropbox = set()
    seen_streamable = set()
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

        # --- Streamable links (progressive MP4, resolved via its API) ---
        for url in extract_streamable_links_from_post(post):
            sc = _streamable_id(url)
            if not sc or sc in seen_streamable:
                continue
            seen_streamable.add(sc)
            streamable_items.append({'shortcode': sc, 'title': post_title or sc})
            had_media = True

        # --- Vidyard links ---
        for url in extract_vidyard_links_from_post(post):
            vy = _vidyard_id(url)
            if not vy or vy in seen_vidyard:
                continue
            seen_vidyard.add(vy)
            vidyard_items.append({'uuid': vy, 'title': post_title or vy})
            had_media = True

        # --- muse.ai links (often password-protected; the password is written in the post) ---
        for url in extract_muse_links_from_post(post):
            svid = _muse_id(url)
            if not svid or svid in seen_muse:
                continue
            seen_muse.add(svid)
            muse_items.append({'svid': svid, 'title': post_title or svid,
                               'password': _muse_password_from_text(_post_plain_text(post))})
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

    total = (len(drive_videos) + len(dropbox_items) + len(streamable_items)
             + len(muse_items) + len(vidyard_items) + len(hls_videos))
    if total == 0:
        print("[ERROR] Found no downloadable videos (no Drive/Dropbox links or native videos).")
        return

    print(f"[INFO] Found {total} item(s) across {posts_with_media} post(s): "
          f"{len(drive_videos)} Drive, {len(dropbox_items)} Dropbox, "
          f"{len(streamable_items)} Streamable, {len(muse_items)} muse.ai, "
          f"{len(vidyard_items)} Vidyard, {len(hls_videos)} native.")

    if list_only:
        n = 0
        for v in drive_videos:
            n += 1
            print(f"   {n:>3}) [Drive]   {v['title']}")
            if verbose:
                print(f"        url: https://drive.google.com/file/d/{v.get('id')}/view")
        for d in dropbox_items:
            n += 1
            print(f"   {n:>3}) [Dropbox] {d['title']}")
            if verbose:
                print(f"        url: {d.get('url')}")
        for st in streamable_items:
            n += 1
            print(f"   {n:>3}) [Streamable] {st['title']}")
            if verbose:
                print(f"        url: https://streamable.com/{st['shortcode']}")
        for vy in vidyard_items:
            n += 1
            print(f"   {n:>3}) [Vidyard] {vy['title']}")
            if verbose:
                print(f"        url: https://share.vidyard.com/watch/{vy['uuid']}")
        for mu in muse_items:
            n += 1
            lock = ' (password from post)' if mu.get('password') else ''
            print(f"   {n:>3}) [muse.ai] {mu['title']}{lock}")
            if verbose:
                print(f"        url: https://muse.ai/v/{mu['svid']}")
        for h in hls_videos:
            tag = 'Mux' if h.get('source') == 'mux' else f"Vimeo {h.get('vimeo_id')}"
            n += 1
            print(f"   {n:>3}) [{tag}] {h['title']}")
            if verbose:
                src = h.get('master_url') or \
                    (f"vimeo {h.get('vimeo_id')}/{h.get('vimeo_hash')}" if h.get('source') == 'vimeo' else '')
                if src:
                    print(f"        src: {src}")
        return

    # Combined interactive selection across all three kinds.
    if select:
        interactive = bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
            bool(getattr(sys.stdout, 'isatty', lambda: False)())
        if interactive:
            combined = ([{'title': f"[Drive]   {v['title']}", '_k': 'drive', '_p': v} for v in drive_videos]
                        + [{'title': f"[Dropbox] {d['title']}", '_k': 'dbx', '_p': d} for d in dropbox_items]
                        + [{'title': f"[Streamable] {st['title']}", '_k': 'strm', '_p': st} for st in streamable_items]
                        + [{'title': f"[Vidyard] {vy['title']}", '_k': 'vy', '_p': vy} for vy in vidyard_items]
                        + [{'title': f"[muse.ai] {mu['title']}", '_k': 'muse', '_p': mu} for mu in muse_items]
                        + [{'title': f"[Native]  {h['title']}", '_k': 'hls', '_p': h} for h in hls_videos])
            chosen = _prompt_file_selection(combined)
            if not chosen:
                print("[INFO] Nothing selected; exiting.")
                return
            drive_videos = [c['_p'] for c in chosen if c['_k'] == 'drive']
            dropbox_items = [c['_p'] for c in chosen if c['_k'] == 'dbx']
            streamable_items = [c['_p'] for c in chosen if c['_k'] == 'strm']
            muse_items = [c['_p'] for c in chosen if c['_k'] == 'muse']
            vidyard_items = [c['_p'] for c in chosen if c['_k'] == 'vy']
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
        if SUBS_ONLY:
            _log_source("Subtitles-only (collection)", "native HLS text tracks -> .srt")
            # Subtitles-only: pull only the chosen-language subtitles from the native videos and
            # skip every actual video download (Drive/Dropbox/Streamable/HLS video+audio).
            _download_subs_only([{'kind': 'hls', 'stream': h, 'title': h.get('title')}
                                 for h in hls_videos], session, None, max_height, verbose)
            return
        if drive_videos:
            _log_source("Google Drive", f"direct download — {len(drive_videos)} file(s)")
            download_folder_pooled(drive_videos, session, chunk_size, verbose)
        if dropbox_items:
            dbx_videos = [{'id': normalize_dropbox_url(d['url']), 'title': d['filename'],
                           'name': d['filename'], 'direct_url': normalize_dropbox_url(d['url'])}
                          for d in dropbox_items]
            _log_source("Dropbox", f"direct download — {len(dbx_videos)} file(s)")
            download_folder_pooled(dbx_videos, session, chunk_size, verbose, label="Dropbox",
                                   conn_cap=DROPBOX_DEFAULT_CONNECTIONS)
        if streamable_items:
            strm_videos = []
            used_strm_names = set()
            for st in streamable_items:
                durl, title, _h = resolve_streamable(st['shortcode'], session, verbose)
                if not durl:
                    print(f"[WARN] Streamable {st['shortcode']}: could not resolve; skipping.")
                    continue
                fname = _unique_name(_streamable_fname(st['title'] or title, st['shortcode']),
                                     used_strm_names)
                strm_videos.append({'id': durl, 'title': fname, 'name': fname,
                                    'direct_url': durl, 'headers': STREAMABLE_HEADERS,
                                    'streamable_shortcode': st['shortcode']})
            if strm_videos:
                _log_source("Streamable", f"direct MP4 — {len(strm_videos)} file(s)")
                download_folder_pooled(strm_videos, session, chunk_size, verbose,
                                       label="Streamable", conn_cap=max_connections)
        if vidyard_items:
            download_vidyard_pooled(vidyard_items, session, chunk_size, max_connections,
                                    max_height, verbose)
        if muse_items:
            if not ensure_ffmpeg(verbose):
                print("[ERROR] muse.ai videos need ffmpeg to mux audio+video, and it was not "
                      "available. Skipping them.")
            else:
                download_muse_pooled(muse_items, session, chunk_size, max_connections,
                                     max_height, verbose)
        if hls_videos:
            if not ensure_ffmpeg(verbose):
                print("[ERROR] Native (Vimeo/Mux) videos need ffmpeg to mux audio+video, and it "
                      "was not available. Skipping the native videos.")
            else:
                _log_source("Native Vimeo/Mux", f"HLS + ffmpeg mux — {len(hls_videos)} file(s)")
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
    global _active_collection_title
    _active_collection_title = (title or '').strip() or None
    posts = list_collection_posts(collection_id, campaign_id, session, verbose)
    if not posts:
        print("[ERROR] No posts found in the collection.")
        print("        It may be private/paid (needs your Patreon cookies), empty, or Patreon")
        print("        changed its API. Provide cookies with --cookies or a *.json export nearby.")
        return

    if _patreon_creator:
        print(f"[INFO] Creator '{_patreon_creator}'")
    print(f"[INFO] Collection '{title or collection_id}': scanning {len(posts)} post(s) "
          f"for Drive / Dropbox links and native videos ...")

    _download_from_posts(posts, session, chunk_size, num_threads, folder_workers, recursive,
                         verbose, select, out_dir, max_connections, max_height, list_only)


def fetch_patreon_post(post_id: str, session: requests.Session, verbose: bool):
    """Fetch a single Patreon post object (with its linked 'included' resources merged in so
    link/stream extraction can see them). Returns the post dict or None."""
    params = {
        'include': 'collections,drop,primary_image,audio,video,embed',
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
    # Best-effort creator name from the post's campaign relationship (separate endpoint).
    global _patreon_creator
    camp = ((post.get('relationships') or {}).get('campaign') or {}).get('data') or {}
    _patreon_creator = get_creator_name(camp.get('id'), session, verbose)
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
    if _patreon_creator:
        print(f"[INFO] Creator '{_patreon_creator}'")
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
    headers = ({'Referer': 'https://drive.google.com/'}
               if 'google' in (url or '').lower()
               else {'User-Agent': USER_AGENT, 'Accept': '*/*'})
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

def merge_parts(part_files: list[str], output_filename: str, verbose: bool) -> bool:
    """Merge all part files into the final output file. Returns True on success.

    Callers MUST check the result: a failed merge means no output file exists, so treating it
    as a success would report a download that isn't there."""
    # Use tqdm.write for all messages: during pooled downloads there are many live progress
    # bars, and a plain print() would scribble over them and corrupt the display.
    if verbose:
        tqdm.write(f"[INFO] Merging {len(part_files)} parts into {os.path.basename(output_filename)}")

    if not part_files:
        tqdm.write(f"[ERROR] Nothing to merge for {os.path.basename(output_filename)}.")
        return False
    missing = [pf for pf in part_files if not os.path.exists(pf)]
    if missing:
        tqdm.write(f"[ERROR] Missing parts: {missing}")
        return False

    # Put the scratch .merging file next to the part files (they already live in CWD's .temp),
    # not next to the output — otherwise an output inside .temp would create a nested .temp/.temp.
    scratch_dir = os.path.dirname(part_files[0]) if part_files else _temp_dir_for(output_filename)
    tmp_output = os.path.join(scratch_dir, os.path.basename(output_filename) + ".merging")
    try:
        with open(tmp_output, 'wb') as outfile:
            for part_file in part_files:
                with open(part_file, 'rb') as pf:
                    shutil.copyfileobj(pf, outfile, length=8 * 1024 * 1024)
            outfile.flush()
            os.fsync(outfile.fileno())

        # Atomic swap: the final name only appears once the file is fully written.
        os.replace(tmp_output, output_filename)
    except OSError as e:
        # Out of disk space, permissions, a vanished part... keep the parts so a re-run resumes.
        tqdm.write(f"[ERROR] Merge failed for {os.path.basename(output_filename)}: {e}")
        try:
            os.remove(tmp_output)
        except OSError:
            pass
        return False

    for part_file in part_files: # Cleanup
        try:
            os.remove(part_file)
        except OSError:
            pass
    return True

def download_file(url: str, session: requests.Session, filename: str, chunk_size: int, num_threads: int, verbose: bool, position: int = 0, show_part_bars: bool = True) -> bool:
    """Downloads the file using multiple threads, each handling a byte-range segment.

    Returns True on success. When show_part_bars is False, only a single aggregate
    progress bar (at `position`) is shown — used for parallel folder downloads to keep
    the terminal readable.
    """

    errors = []
    num_threads = max(1, num_threads)

    # The final name only ever appears after a complete download (both paths below write into
    # .temp first and then move), so its presence means the file is done -> skip, like the
    # pooled folder engine does.
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        print(f"[INFO] Already have {os.path.basename(filename)}, skipping.")
        return True

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
        # The split size is part of the scratch name: parts left over from a run with a
        # DIFFERENT -t (or a different segment size) describe different byte ranges, and
        # silently appending to them would produce a corrupt file that still passes the
        # total-size check. Different split -> different name -> never reused by mistake.
        part_filename = _temp_artifact(filename, f".p{part_size}.part{i}")
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

    # Verify all parts downloaded correctly. Both directions matter: too FEW bytes means a
    # truncated download, too MANY means the parts don't line up with the expected split
    # (stale scratch files) and merging them would produce a corrupt file.
    downloaded_total = sum(os.path.getsize(pf) for pf in part_files if os.path.exists(pf))
    if downloaded_total < total_size:
        print(f"[ERROR] Download incomplete for {filename}: got {downloaded_total}/{total_size} bytes.")
        return False
    if downloaded_total > total_size:
        print(f"[ERROR] Inconsistent parts for {filename}: got {downloaded_total} bytes but the "
              f"file is {total_size}. Delete the .temp scratch files and re-run.")
        return False

    if not merge_parts(part_files, filename, verbose):
        return False
    filename = _apply_forced_container(filename, verbose)   # --container (no-op when auto)
    _record_download(filename)
    if show_part_bars:
        print(f"\n{filename} downloaded successfully.")
    return True

def download_single_threaded(url: str, session: requests.Session, filename: str, chunk_size: int, verbose: bool, position: int = 0) -> bool:
    """Fallback single-threaded download (used for -t 1 and when the size is unknown).

    Writes into a scratch .part file inside .temp and only moves it to its final name once the
    transfer completes, so an interrupted run can never leave a truncated file under the real
    name — and a re-run resumes from what is already there."""
    headers = {
        'Referer': 'https://drive.google.com/',
    }
    part_file = _temp_artifact(filename, ".part")
    file_mode = 'wb'
    downloaded_size = 0

    if os.path.exists(part_file):
        downloaded_size = os.path.getsize(part_file)
        if downloaded_size > 0:
            headers['Range'] = f"bytes={downloaded_size}-"
            file_mode = 'ab'

    if verbose:
        print(f"[INFO] Starting single-threaded download from {url}")

    with connection_slot():
        with session.get(url, stream=True, headers=headers,
                         timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as response:
            # 416 to a resume request means the server considers the range past the end, i.e.
            # what we already have IS the whole file.
            if response.status_code == 416 and downloaded_size > 0:
                if verbose:
                    print(f"[INFO] {filename} was already complete; finishing it.")
            elif response.status_code in (200, 206):  # 200 new/full, 206 partial
                # A 200 answer to a Range request means the server IGNORED the range and is
                # streaming from byte 0. Appending that would corrupt the file, so start over.
                if downloaded_size > 0 and response.status_code == 200:
                    if verbose:
                        print(f"[WARN] Server ignored Range for {filename}; restarting from scratch.")
                    downloaded_size = 0
                    file_mode = 'wb'
                total_size = int(response.headers.get('content-length', 0) or 0) + downloaded_size
                with open(part_file, file_mode) as file:
                    with make_bar(total=total_size, initial=downloaded_size, unit='B', unit_scale=True,
                                  desc=os.path.basename(filename),
                                  position=position if position is not None else 0,
                                  disable=(position is None), file=sys.stdout) as pbar:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if chunk:
                                file.write(chunk)
                                pbar.update(len(chunk))
            else:
                print(f"[ERROR] Error downloading {filename}, status code: {response.status_code}")
                return False

    if not os.path.exists(part_file) or os.path.getsize(part_file) == 0:
        print(f"[ERROR] Download produced no data for {filename}.")
        return False
    try:
        os.replace(part_file, filename)     # atomic: the final name means "finished"
    except OSError as e:
        print(f"[ERROR] Could not finalize {filename}: {e}")
        return False
    filename = _apply_forced_container(filename, verbose)   # --container (no-op when auto)
    _record_download(filename)              # so the rename offer / --url-list report see it
    print(f"\n{filename} downloaded successfully.")
    return True

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


# Emoji / pictographs / symbols that break console rendering (and clutter filenames).
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002190-\U000021FF]"
    "|[\u200d\u20e3\u2122\u2139\u3030\u303d\u3297\u3299]", flags=re.UNICODE)


ASCII_FILENAMES = False           # True = force plain-ASCII filenames (see also --ascii)


def _to_ascii(name: str) -> str:
    """Best-effort ASCII: strip accents from Latin (é->e), drop characters that have no ASCII
    form (CJK, etc.), then tidy the leftovers (empty brackets, doubled spaces)."""
    import unicodedata
    s = unicodedata.normalize('NFKD', name)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[\(\[\{]\s*[\)\]\}]', '', s)     # drop now-empty brackets
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' -_')


def safe_filename(name: str, fallback_id: str) -> str:
    """Sanitise a filename for Windows + Linux (also strips emoji, which cmd.exe renders as
    boxes and which clutter filenames). With ASCII_FILENAMES/--ascii, transliterates to ASCII.
    The result is also length-capped: creators sometimes put a whole paragraph in the post title,
    and Windows then fails (OSError 22 / 206) once the path with the .temp folder and the
    .lock/.part suffixes goes past its limit."""
    name = _EMOJI_RE.sub('', name)
    if ASCII_FILENAMES:
        name = _to_ascii(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', name)
    name = re.sub(r'\s{2,}', ' ', name)
    name = re.sub(r'[. ]+$', '', name).strip()
    # Windows refuses to create a file whose stem is a device name — and the extension does not
    # help, so "CON.mp4" fails just as hard as "CON". Only the rename step guarded against this,
    # which left the DOWNLOAD failing with a bare OSError on such a title.
    _stem, _ext = os.path.splitext(name)
    if _stem.upper() in RESERVED_WIN:
        name = f"_{_stem}{_ext}"
    name = _cap_filename(name)
    return name or f"{fallback_id}.mp4"


def _cap_filename(name: str) -> str:
    """Shorten an over-long filename, keeping a real video extension if it has one. Leaves room
    for the .temp folder and the .part/.lock/.v.mp4 suffixes the downloader appends."""
    limit = max(40, int(MAX_FILENAME_CHARS or 120))
    if len(name) <= limit:
        return name
    ext = os.path.splitext(name)[1]
    ext = ext if ext.lower() in _VIDEO_EXTS else ''
    stem = name[:len(name) - len(ext)] if ext else name
    stem = stem[:max(8, limit - len(ext))].rstrip(' .-_')
    return stem + ext


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


# =============================================================================
#  Interactive TUI — arrow-key menu + checklist with live search/filter and a
#  right-hand scrollbar. Falls back to a numbered prompt without a TTY.
# =============================================================================
_TUI_WIN = os.name == 'nt'
try:
    if _TUI_WIN:
        import msvcrt as _msvcrt
    else:
        import termios as _termios
        import tty as _tty
    _HAS_RAW = True
except Exception:
    _HAS_RAW = False

_TUI_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def _tui_strip(s):
    return _TUI_ANSI_RE.sub('', s)


def _tui_supported():
    try:
        return _HAS_RAW and sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _tui_flush_input():
    try:
        if _TUI_WIN:
            while _msvcrt.kbhit():
                _msvcrt.getch()
        else:
            _termios.tcflush(sys.stdin.fileno(), _termios.TCIFLUSH)
    except Exception:
        pass


def _tui_read_key():
    """Read one keypress -> 'up'/'down'/'pgup'/'pgdn'/'home'/'end'/'enter'/'esc'/
    'backspace'/'other', or ('char', <character>)."""
    if _TUI_WIN:
        ch = _msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            c2 = _msvcrt.getch()
            return {b'H': 'up', b'P': 'down', b'K': 'left', b'M': 'right', b'G': 'home',
                    b'O': 'end', b'I': 'pgup', b'Q': 'pgdn'}.get(c2, 'other')
        if ch in (b'\r', b'\n'):
            return 'enter'
        if ch == b'\x08':
            return 'backspace'
        if ch == b'\x1b':
            return 'esc'
        if ch == b'\x03':
            raise KeyboardInterrupt
        if ch in (b'\x01', b'\x04', b'\x12'):          # Ctrl+A / Ctrl+D / Ctrl+R
            return {b'\x01': 'check_all', b'\x04': 'uncheck_all', b'\x12': 'invert'}[ch]
        for enc in ('utf-8', 'cp1250', 'latin-1'):
            try:
                return ('char', ch.decode(enc))
            except Exception:
                continue
        return 'other'
    import select
    fd = sys.stdin.fileno()

    def _avail(t=0.12):
        try:
            return bool(select.select([fd], [], [], t)[0])
        except Exception:
            return False

    def _rd():
        try:
            return os.read(fd, 1)
        except Exception:
            return b''

    b = _rd()
    if not b:
        return 'other'
    c0 = b[0]
    if c0 == 0x1b:
        if not _avail():
            return 'esc'
        b2 = _rd()
        if b2 not in (b'[', b'O'):
            return 'esc'
        seq = b''
        while True:
            c = _rd()
            if not c:
                break
            seq += c
            if c.isalpha() or c == b'~' or len(seq) > 6:
                break
            if not _avail(0.02):
                break
        s = seq.decode('ascii', 'ignore')
        return {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left', 'H': 'home', 'F': 'end',
                '1~': 'home', '4~': 'end', '5~': 'pgup', '6~': 'pgdn'}.get(s, 'esc')
    if c0 in (0x0d, 0x0a):
        return 'enter'
    if c0 in (0x7f, 0x08):
        return 'backspace'
    if c0 == 0x03:
        raise KeyboardInterrupt
    if c0 in (0x01, 0x04, 0x12):                      # Ctrl+A / Ctrl+D / Ctrl+R
        return {0x01: 'check_all', 0x04: 'uncheck_all', 0x12: 'invert'}[c0]
    n_more = 3 if c0 >= 0xF0 else 2 if c0 >= 0xE0 else 1 if c0 >= 0xC0 else 0
    data = b
    for _ in range(n_more):
        if not _avail(0.05):
            break
        data += _rd()
    for enc in ('utf-8', 'cp1250', 'latin-1'):
        try:
            return ('char', data.decode(enc))
        except Exception:
            continue
    return 'other'


class _TuiRaw:
    def __enter__(self):
        if not _TUI_WIN:
            self.fd = sys.stdin.fileno()
            self.old = _termios.tcgetattr(self.fd)
            try:
                _tty.setcbreak(self.fd)
            except Exception:
                _tty.setraw(self.fd)
        return self

    def __exit__(self, *a):
        if not _TUI_WIN:
            _termios.tcsetattr(self.fd, _termios.TCSADRAIN, self.old)


def _tui_scrollbar(base_row, nrows, total, top, cols):
    """Draw a proportional vertical scrollbar in the last screen column next to the item area."""
    if total <= nrows or nrows <= 0 or cols <= 1:
        return
    thumb = max(1, min(nrows, int(round(nrows * nrows / float(total)))))
    span = nrows - thumb
    denom = max(1, total - nrows)
    tstart = max(0, min(span, int(round(span * (top / float(denom))))))
    parts = ['\x1b[?7l']
    for r in range(nrows):
        if tstart <= r < tstart + thumb:
            parts.append(f"\x1b[{base_row + r};{cols}H{CLR.CYAN}\u2588{CLR.RESET}")
        else:
            parts.append(f"\x1b[{base_row + r};{cols}H{CLR.DIM}\u2502{CLR.RESET}")
    parts.append('\x1b[?7h')
    sys.stdout.write(''.join(parts))
    sys.stdout.flush()


def _tui_list(prompt, labels, multi=False, preselected=None, header=None):
    """Arrow-key selector with live search and a scrollbar. multi=False -> returns an index (or
    None on Esc). multi=True -> returns a sorted list of indices (Enter confirms; Esc cancels).
    Returns None when a TTY/raw mode isn't available (caller uses the numbered fallback)."""
    if not _tui_supported() or not labels:
        return None
    n = len(labels)
    plain = [_tui_strip(x) for x in labels]
    header = list(header or [])
    checkset = set(preselected or []) if multi else set()
    filt = ''
    pending = None                  # ('+'|'-', typed pattern) while the filter prompt is open
    sel_pos = 0
    prev_lines = 0
    first = True

    def _match(pat):
        """Indices matching a user-typed pattern. '*'/'?' = wildcard (fnmatch, matched anywhere),
        otherwise a plain 'contains' match. Case-insensitive."""
        p = (pat or '').strip().lower()
        if not p:
            return set()
        if '*' in p or '?' in p:
            q = p if p.startswith('*') else '*' + p
            q = q if q.endswith('*') else q + '*'
            return {i for i in range(n) if fnmatch.fnmatch(plain[i].lower(), q)}
        return {i for i in range(n) if p in plain[i].lower()}

    def order():
        """Indices matching the current search. Plain text = substring match; a query containing
        '*' or '?' is treated as a wildcard pattern (fnmatch), e.g. 'ghostbusters*' or 'S0?E*'.
        An unanchored pattern is matched anywhere in the label."""
        if not filt:
            return list(range(n))
        f = filt.lower()
        if '*' in f or '?' in f:
            pat = f if f.startswith('*') else '*' + f
            pat = pat if pat.endswith('*') else pat + '*'
            return [i for i in range(n) if fnmatch.fnmatch(plain[i].lower(), pat)]
        return [i for i in range(n) if f in plain[i].lower()]

    def size():
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def trunc(s, w):
        return s[:max(1, w - 1)] + '…' if len(s) > w else s

    def render(od):
        nonlocal prev_lines, first
        cols, rows_total = size()
        maxw = max(10, cols - 3)                    # leave a column for the scrollbar
        page = max(3, rows_total - (6 + len(header)))
        vis = []
        buf = []
        if first:
            buf.append('\x1b[2J\x1b[H')
            first = False
        elif prev_lines > 0:
            buf.append((f"\x1b[{prev_lines - 1}F" if prev_lines > 1 else '\r') + '\x1b[J')
        for h in header:
            vis.append(trunc(h, maxw) if len(_tui_strip(h)) > maxw else h)
        vis.append(f"{CLR.YELLOW}{trunc(_tui_strip(prompt), maxw)}{CLR.RESET}")
        keys = ("space = toggle · * = all/none · +/- = check/uncheck by filter · Enter = confirm"
                if multi else "Enter = select")
        hint = (f"↑↓ move · type = search · {keys} · "
                + ("Esc = clear search" if filt else "Esc = cancel"))
        vis.append(f"{CLR.CYAN}{trunc(hint, maxw)}{CLR.RESET}")
        body_start = len(vis) + 1                    # 1-based screen row of the first item
        window = []
        if not od:
            vis.append(f"  {CLR.RED}(no match){CLR.RESET}")
        else:
            start = max(0, min(sel_pos - page // 2, len(od) - page))
            window = od[start:start + page]
            for pos, i in enumerate(window, start):
                box = (f"{CLR.GREEN}[x]{CLR.RESET} " if i in checkset else "[ ] ") if multi else ""
                txt = trunc(plain[i], maxw - (4 if multi else 2))
                if pos == sel_pos:
                    vis.append(f"{CLR.GREEN}{CLR.BRIGHT}›{CLR.RESET} {box}"
                               f"{CLR.GREEN}{CLR.BRIGHT}{txt}{CLR.RESET}")
                else:
                    vis.append(f"  {box}{txt}")
        posinfo = f" [{sel_pos + 1}/{len(od)}]" if od else ""
        if multi:
            posinfo += f"  ✓{len(checkset)}"
        if pending:
            act = 'CHECK' if pending[0] == '+' else 'UNCHECK'
            foot = (f"{act} by filter (e.g. *.mkv, *Viki*): {pending[1]}_"
                    f"   [{len(_match(pending[1]))} match]  Enter = apply · Esc = cancel")
            vis.append(f"{CLR.GREEN}{trunc(foot, maxw)}{CLR.RESET}")
        else:
            vis.append(f"{CLR.MAGENTA}{trunc(('Search: ' + filt if filt else '(type to search)') + posinfo, maxw)}{CLR.RESET}")
        buf.append('\n'.join(vis))
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)
        if window:
            _tui_scrollbar(body_start, len(window), len(od), od.index(window[0]), size()[0])
        return page

    with _TuiRaw():
        od = order()
        page = render(od)
        _tui_flush_input()
        while True:
            key = _tui_read_key()
            if pending is not None:
                # Filter prompt is open: type a pattern, Enter applies it, Esc cancels.
                act, buf_pat = pending
                if key == 'enter':
                    target = _match(buf_pat)
                    checkset = (checkset | target) if act == '+' else (checkset - target)
                    pending = None
                elif key == 'esc':
                    pending = None
                elif key == 'backspace':
                    pending = (act, buf_pat[:-1])
                elif isinstance(key, tuple) and key[0] == 'char' and key[1].isprintable():
                    pending = (act, buf_pat + key[1])
                page = render(od)
                continue
            if key == 'up' and od:
                sel_pos = (sel_pos - 1) % len(od)
            elif key == 'down' and od:
                sel_pos = (sel_pos + 1) % len(od)
            elif key == 'pgup' and od:
                sel_pos = max(0, sel_pos - page)
            elif key == 'pgdn' and od:
                sel_pos = min(len(od) - 1, sel_pos + page)
            elif key == 'home':
                sel_pos = 0
            elif key == 'end' and od:
                sel_pos = len(od) - 1
            elif multi and isinstance(key, tuple) and key[0] == 'char' and key[1] == ' ':
                if od:
                    checkset.symmetric_difference_update({od[sel_pos]})
            elif multi and isinstance(key, tuple) and key[0] == 'char' and key[1] == '*':
                checkset = set() if len(checkset) >= n else set(range(n))   # all <-> none
            elif multi and isinstance(key, tuple) and key[0] == 'char' and key[1] in '+-':
                if filt:                       # a search is active -> apply to what's shown
                    target = set(od)
                    checkset = (checkset | target) if key[1] == '+' else (checkset - target)
                else:                          # otherwise ask for a pattern (e.g. *.mkv, *Viki*)
                    pending = ('+' if key[1] == '+' else '-', '')
            elif multi and key in ('check_all', 'uncheck_all', 'invert'):
                # (also still available on Ctrl+A / Ctrl+D / Ctrl+R)
                target = set(od)
                if key == 'check_all':
                    checkset |= target
                elif key == 'uncheck_all':
                    checkset -= target
                else:
                    checkset ^= target
            elif key in ('enter', 'right'):
                if multi:
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return sorted(checkset)
                if od:
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return od[sel_pos]
            elif key in ('esc', 'left'):
                if filt:
                    filt = ''
                    od = order()
                    sel_pos = 0
                else:
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return None
            elif key == 'backspace' and filt:
                filt = filt[:-1]
                od = order()
                sel_pos = 0
            elif isinstance(key, tuple) and key[0] == 'char' and key[1].isprintable():
                filt += key[1]
                od = order()
                sel_pos = 0
            page = render(od)


def tui_select_one(prompt, labels, header=None):
    """Single-select. Returns the chosen index, or None if cancelled. TTY -> arrow menu; else a
    numbered prompt."""
    idx = _tui_list(prompt, labels, multi=False, header=header)
    if idx is not None or _tui_supported():
        return idx
    for h in (header or []):
        if _tui_strip(h).strip():
            print(h)
    print(f"{CLR.YELLOW}{prompt}{CLR.RESET}")
    for i, lab in enumerate(labels, 1):
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {_tui_strip(lab)}")
    for _ in range(3):
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw) - 1
        print(f"[WARN] Enter a number 1-{len(labels)}.")
    return None


def tui_select_many(prompt, labels, preselected=None, header=None):
    """Multi-select. Returns a list of chosen indices (possibly empty), or None if cancelled. TTY ->
    checklist with space to toggle; else a numbered 'a,c,1-3' prompt."""
    res = _tui_list(prompt, labels, multi=True, preselected=preselected, header=header)
    if res is not None or _tui_supported():
        return res
    for h in (header or []):
        if _tui_strip(h).strip():
            print(h)
    print(f"{CLR.YELLOW}{prompt}{CLR.RESET}")
    for i, lab in enumerate(labels, 1):
        print(f"   {CLR.CYAN}{i:>3}{CLR.RESET}) {_tui_strip(lab)}")
    print("Pick: e.g. '1,3,5-8', 'a' or Enter for ALL, 'q' to cancel.")
    for _ in range(3):
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None
        if raw == '' and preselected is not None:
            return list(preselected)           # Enter keeps the default (e.g. the newest cookie)
        sel = _parse_index_selection(raw, len(labels))
        if sel is None:
            print(f"[WARN] Invalid input. Use 1-{len(labels)}, 'a', or 'q'.")
            continue
        return [i - 1 for i in sel]
    return None


def _prompt_file_selection(videos: list) -> list:
    """Interactive multi-select of which videos to download (arrow-key checklist with search +
    scrollbar; numbered fallback without a TTY). Returns the chosen video dicts."""
    labels = [v['title'] for v in videos]
    idxs = tui_select_many(f"Select which of {len(videos)} file(s) to download",
                           labels, preselected=list(range(len(videos))),
                           header=[f"{CLR.CYAN}{len(videos)} file(s) found{CLR.RESET}"])
    if idxs is None:
        return []
    return [videos[i] for i in idxs]

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
        self.ua = entry.get('ua') if isinstance(entry, dict) else None   # override UA (googlevideo)
        self.dl_headers = entry.get('headers') if isinstance(entry, dict) else None
        self.url = None
        self.size = 0
        self.filename = None
        self.out_path = None        # final file location (defaults to filename in CWD)
        self.lock_path = None
        self.segments = []          # list of dicts: {start, end, path}
        self.remaining = 0
        self.failed = False
        self.fail_reason = None
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
    is_dropbox = bool(getattr(job, 'direct_url', '')) and 'dropbox' in (job.url or '').lower()
    for attempt in range(attempts):
        lo = start + downloaded
        if getattr(job, 'direct_url', ''):
            # Dropbox / other direct hosts: send a normal browser UA and NO Google referer,
            # otherwise Dropbox serves an HTML preview/interstitial instead of the file bytes.
            # For googlevideo, use the UA of the client that issued the URL (helps avoid extra
            # server-side throttling). --scan can supply a page-derived Referer/Origin.
            headers = dict(getattr(job, 'dl_headers', None)
                           or {'User-Agent': getattr(job, 'ua', None) or USER_AGENT, 'Accept': '*/*'})
        else:
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
                    if getattr(job, 'fail_reason', None) is None:
                        job.fail_reason = (
                            f"HTTP {r.status_code} (expired/invalid signed URL — re-run to refresh)"
                            if r.status_code in (403, 410)
                            else "rate-limited (HTTP 429)" if r.status_code == 429
                            else f"HTTP {r.status_code}")
                    if verbose:
                        tqdm.write(f"[DBG] download HTTP {r.status_code} for "
                                   f"{os.path.basename(seg['path'])}"
                                   + (" (googlevideo 403 usually means a PO-token is required "
                                      "for this client)" if r.status_code == 403
                                      and 'googlevideo' in (job.url or '') else ""))
                    return False
                # The body must be the file, not an HTML interstitial/error page (Dropbox and
                # Drive both do this when a link needs confirmation, is a folder, or is rate-
                # limited). Writing that as the video would leave a tiny broken "success", so
                # back off and retry, then fail honestly.
                ctype = (r.headers.get('Content-Type') or '').lower()
                if 'text/html' in ctype or 'application/json' in ctype:
                    # Dropbox served a preview/interstitial page, not the file. For Dropbox,
                    # try the raw=1 variant (bypasses the preview for public links); otherwise
                    # back off and retry, then fail honestly.
                    if is_dropbox and 'raw=1' not in (job.url or ''):
                        with job.url_lock:
                            job.url = _dropbox_raw_variant(job.url)
                        if verbose:
                            tqdm.write(f"[INFO] Dropbox returned HTML; retrying with raw=1: {job.title}")
                        continue
                    if attempt < attempts - 1:
                        time.sleep(min(10.0, 0.8 * (2 ** attempt)))
                        continue
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
                           label: str = "Folder", conn_cap: int = None, seg_mib: int = None,
                           dest_subdir: str = None, record: bool = True) -> None:
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
    seg_size = max(1, seg_mib or SEGMENT_MIB) * 1024 * 1024

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
                # Direct download (e.g. Dropbox): probe size, real filename, AND the URL
                # variant (dl=1 vs raw=1) that actually serves the file, in one go.
                size, real, working = _dropbox_probe(
                    job.direct_url, session, getattr(job, 'dl_headers', None))
                job.direct_url = working
                job.url = working
                job.size = size if size > 0 else get_file_size(working, session)
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
    if dest_subdir:
        try:
            os.makedirs(dest_subdir, exist_ok=True)
        except OSError:
            pass
    for job in jobs:
        if job.failed or not job.filename:
            tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Skipping (no playback URL): {job.title}")
            continue
        # Final file goes to dest_subdir (if any); scratch parts/lock stay in CWD's .temp so
        # they never nest (.temp/.temp) and get cleaned normally.
        job.out_path = os.path.join(dest_subdir, job.filename) if dest_subdir else job.filename
        # Already finished in a previous run? The final name only appears after a complete,
        # atomic merge, so its presence means the file is done -> skip (resume-friendly).
        if os.path.exists(job.out_path) and os.path.getsize(job.out_path) > 0:
            tqdm.write(f"[INFO] Already have {os.path.basename(job.out_path)}, skipping.")
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
                # See download_file(): the split size is baked into the scratch name so parts
                # written with a different SEGMENT_MIB / -t can never be resumed into the wrong
                # byte range.
                job.segments.append({'start': s, 'end': e,
                                     'path': _temp_artifact(job.filename, f".p{seg_size}.part{i}")})
        else:
            # Unknown size: one open-ended part. Distinct suffix so it can't be mistaken for a
            # sized segment 0.
            job.segments.append({'start': 0, 'end': None,
                                 'path': _temp_artifact(job.filename, ".whole.part0")})
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
          f"({seg_size // (1024 * 1024)} MiB segments). Freed connections flow to the "
          f"remaining files.\n")

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
        _final = job.out_path or job.filename
        if job.failed:
            reason = getattr(job, 'fail_reason', None) or "download failed"
        else:
            got = sum(os.path.getsize(s['path']) for s in job.segments if os.path.exists(s['path']))
            if job.size > 0:
                if got > job.size:
                    # Stale scratch files whose byte ranges don't match this split — merging
                    # them would silently produce a corrupt video.
                    reason = (f"inconsistent parts ({got} bytes for a {job.size}-byte file) — "
                              f"delete the .temp scratch files and re-run")
                elif got == job.size:
                    ok = merge_parts([s['path'] for s in job.segments], _final, verbose)
                    if not ok:
                        reason = "merge failed (see above); parts kept for resume"
                else:
                    reason = f"incomplete {got}/{job.size} bytes"
            else:
                # Size unknown (Google wouldn't report it): accept only if we actually got data.
                if got > 0:
                    ok = merge_parts([s['path'] for s in job.segments], _final, verbose)
                    if not ok:
                        reason = "merge failed (see above); parts kept for resume"
                else:
                    reason = "no data received (size unknown)"
        if job.locked:
            release_lock(job.lock_path)
        if ok:
            if record:
                _final = _apply_forced_container(_final, verbose)   # --container (no-op when auto)
                _record_download(_final)
        else:
            _vy = job.entry.get('vidyard_uuid') if isinstance(job.entry, dict) else None
            _sc = job.entry.get('streamable_shortcode') if isinstance(job.entry, dict) else None
            if _vy:
                # Vidyard CDN links are signed and expire, so resume must re-resolve them.
                _record_failed({'kind': 'vidyard', 'out_dir': os.getcwd(),
                                'filename': os.path.basename(job.filename or job.title or ''),
                                'reason': reason or 'download failed', 'uuid': _vy,
                                'title': job.title})
            elif _sc:
                # Streamable's CDN URL is a signed link that expires, so resume must re-resolve it
                # from the shortcode rather than replay the stale URL.
                _record_failed({'kind': 'streamable', 'out_dir': os.getcwd(),
                                'filename': os.path.basename(job.filename or job.title or ''),
                                'reason': reason or 'download failed', 'shortcode': _sc,
                                'title': job.title})
            else:
                _record_failed({'kind': 'dropbox' if job.direct_url else 'drive',
                                'out_dir': os.getcwd(),
                                'filename': os.path.basename(job.filename or job.title or ''),
                                'reason': reason or 'download failed',
                                'video': job.entry if isinstance(job.entry, dict) else None})
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
        try:
            if not job.failed:
                sess = _segment_session(session)
                if not _download_segment(job, seg, sess, chunk_size, on_bytes(job), verbose):
                    job.failed = True
        except Exception as exc:
            # An unexpected error must still count as "this segment is done", otherwise
            # `remaining` never reaches 0 and finalize() — which tallies the result and
            # releases the lock — would never run for this file.
            job.failed = True
            if job.fail_reason is None:
                job.fail_reason = f"{type(exc).__name__}: {exc}"
        finally:
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
        for fut in as_completed(futures):
            # Calling result() is what makes a worker crash visible: without it an exception
            # inside run_segment/finalize (full disk, permissions, ...) would vanish silently
            # and the file would be counted as neither succeeded nor failed.
            try:
                fut.result()
            except Exception as exc:
                tqdm.write(f"{CLR.RED}[ERROR]{CLR.RESET} Download worker crashed: "
                           f"{type(exc).__name__}: {exc}")

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
    # 2b) Vimeo without a privacy hash (public/unlisted): just the numeric id.
    mid = re.search(r'vimeo\.com/(\d+)(?:[/?#]|$)', url_field) or \
        re.search(r'player\.vimeo\.com/video/(\d+)', html)
    if mid:
        title = (emb.get('subject') or a.get('title') or mid.group(1)).strip()
        out.append({'source': 'vimeo', 'title': title,
                    'vimeo_id': mid.group(1), 'vimeo_hash': ''})
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


def _find_ffmpeg_in(dirs, max_depth=None):
    """Walk each directory (optionally depth-limited so we don't crawl huge trees like Program
    Files) looking for a working ffmpeg binary. Returns its path or None."""
    name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        base_depth = os.path.abspath(d).rstrip(os.sep).count(os.sep)
        for root, subdirs, files in os.walk(d):
            if max_depth is not None:
                depth = os.path.abspath(root).rstrip(os.sep).count(os.sep) - base_depth
                if depth >= max_depth:
                    subdirs[:] = []
                    continue
            if name in files:
                p = os.path.join(root, name)
                if os.name != 'nt':
                    try:
                        os.chmod(p, 0o755)
                    except OSError:
                        pass
                if _try_ffmpeg(p):
                    return p
    return None


def _find_cached_ffmpeg():
    return _find_ffmpeg_in([_ffmpeg_cache_dir()])


def _extract_archive(path, dest, url):
    """Extract a downloaded tool archive into `dest`, refusing any member that would escape it.

    A plain extractall() honours absolute paths and '..' inside the archive ("Zip Slip"), which
    would let a tampered download overwrite files anywhere the user can write. Every member is
    therefore checked against `dest` first, and symlinks/hardlinks/devices are skipped."""
    lower = (url or path).lower()
    import zipfile
    import tarfile

    root = os.path.realpath(dest)

    def _safe(name):
        target = os.path.realpath(os.path.join(root, name))
        return target == root or target.startswith(root + os.sep)

    if lower.endswith('.zip') or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            members = [n for n in z.namelist() if _safe(n)]
            if len(members) != len(z.namelist()):
                raise ValueError("Archive contains paths outside the target directory — refusing "
                                 "to extract (tampered or malicious download?)")
            z.extractall(dest, members=members)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if not (m.isfile() or m.isdir()):
                    raise ValueError(f"Archive contains a non-regular entry ({m.name}) — "
                                     "refusing to extract")
                if not _safe(m.name):
                    raise ValueError("Archive contains paths outside the target directory — "
                                     "refusing to extract (tampered or malicious download?)")
            try:
                t.extractall(dest, filter='data')   # Python 3.12+: strongest built-in filter
            except TypeError:
                t.extractall(dest)                  # older Python: our own checks above apply
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
    """Locate a working ffmpeg, in order: explicit --ffmpeg/FFMPEG + PATH -> cached .ffmpeg ->
    install folders (FFMPEG_PROGRAM_FILES_DIRS) + script dir + cwd (depth-limited) -> (Windows
    only, last resort) download & unpack. On Linux/macOS it uses PATH and, if missing, prints a
    package-manager hint instead of downloading a Windows build."""
    global FFMPEG
    resolved = _resolve_ffmpeg(FFMPEG)
    if resolved and _try_ffmpeg(resolved):        # explicit path/folder, or bare name on PATH
        FFMPEG = resolved
        return True
    cached = _find_cached_ffmpeg()
    if cached and _try_ffmpeg(cached):
        FFMPEG = cached
        if verbose:
            tqdm.write(f"[INFO] Using cached ffmpeg: {cached}")
        return True
    # Look in the configured install folders (+ script dir + cwd) before downloading.
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0] or '.')) or os.getcwd()
    if os.name == 'nt':
        broad = list(FFMPEG_PROGRAM_FILES_DIRS) + [script_dir, os.getcwd()]
    else:
        broad = [script_dir, os.getcwd()]          # system dirs are already covered by PATH
    found = _find_ffmpeg_in(broad, max_depth=4)
    if found:
        FFMPEG = found
        tqdm.write(f"[INFO] Found ffmpeg: {found}")
        return True
    if os.name == 'nt' and FFMPEG_DOWNLOAD_URL:
        try:
            path = _download_and_extract_ffmpeg(FFMPEG_DOWNLOAD_URL, verbose)
        except Exception as e:
            tqdm.write(f"[ERROR] ffmpeg download/extract failed: {e}")
            return False
        if path and _try_ffmpeg(path):
            FFMPEG = path
            tqdm.write(f"[INFO] Using downloaded ffmpeg: {path}")
            return True
        tqdm.write("[ERROR] Downloaded archive but could not find a working ffmpeg binary inside.")
        return False
    # Linux / macOS: don't fetch a Windows build — tell the user how to install it.
    tqdm.write("[ERROR] ffmpeg not found. Install it and/or put it on PATH:")
    tqdm.write("        Debian/Ubuntu:  sudo apt install ffmpeg")
    tqdm.write("        Fedora:         sudo dnf install ffmpeg")
    tqdm.write("        Arch:           sudo pacman -S ffmpeg")
    tqdm.write("        macOS (brew):   brew install ffmpeg")
    tqdm.write("        or pass --ffmpeg /path/to/ffmpeg (or a folder that contains it).")
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


# ---- Streamable (progressive MP4 via its public API) ---------------------- #
STREAMABLE_HEADERS = {'User-Agent': USER_AGENT, 'Referer': 'https://streamable.com/'}
# A path that looks like a media manifest/file (used to test relative sources found in a page).
_MEDIA_PATH_RE = re.compile(
    r'(?:\.(?:m3u8|mpd|mp4|webm|mov|m4v)(?:[?#/]|$)|\(format=(?:mpd|m3u8)[^)]*\))', re.I)
_STREAMABLE_RESERVED = {'player', 'videos', 'video', 'image', 'about', 'login', 'signup',
                        'terms', 'privacy', 'settings', 'help', 'e', 'o', 's'}
_STREAMABLE_RE = r'(?<![\w.])(?:www\.)?streamable\.com/(?:[eos]/)?([a-z0-9]{4,12})'


def _streamable_id(url):
    """Extract a Streamable shortcode from streamable.com/<code> (also /e/, /o/, /s/ forms)."""
    m = re.search(_STREAMABLE_RE, url or '', re.I)
    if m and m.group(1).lower() not in _STREAMABLE_RESERVED:
        return m.group(1)
    return None


def resolve_streamable(shortcode, session, verbose):
    """Return (direct_mp4_url, title, height) for a Streamable video, picking the highest-quality
    progressive MP4. Returns (None, None, 0) on failure."""
    api = f"https://api.streamable.com/videos/{shortcode}"
    try:
        r = session.get(api, headers=STREAMABLE_HEADERS,
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        if r.status_code != 200:
            if verbose:
                print(f"[WARN] Streamable API returned {r.status_code} for {shortcode}")
            return None, None, 0
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        if verbose:
            print(f"[WARN] Streamable fetch failed for {shortcode}: {e}")
        return None, None, 0
    files = data.get('files') or {}
    best = None
    for key, f in files.items():
        if 'mp4' not in key or not f.get('url'):
            continue
        if best is None or (f.get('height') or 0) > (best.get('height') or 0):
            best = f
    if not best:
        if verbose:
            print(f"[WARN] Streamable: no downloadable MP4 for {shortcode}")
        return None, None, 0
    url = best['url']
    if url.startswith('//'):
        url = 'https:' + url
    return url, data.get('title') or shortcode, best.get('height') or 0


_VIDEO_EXTS = ('.mp4', '.mkv', '.mov', '.m4v', '.webm', '.avi', '.ts', '.flv')


def _ensure_video_ext(name, default='.mp4'):
    """Append a video extension unless the name already ends in one. Checked against a real list
    because episode titles often end in something that only looks like an extension ('EP.1')."""
    return name if os.path.splitext(name)[1].lower() in _VIDEO_EXTS else name + default


def _unique_name(name, used):
    """Make `name` unique within `used` (a set that gets updated). Two posts in a collection often
    share a title, which would otherwise collide on one filename — and the second one would trip
    over the first one's lock."""
    stem, ext = os.path.splitext(name)
    cand, i = name, 2
    while cand.lower() in used:
        cand = f"{stem} ({i}){ext}"
        i += 1
    used.add(cand.lower())
    return cand


def _streamable_fname(base, shortcode):
    """Build a safe .mp4 filename for a Streamable video. The explicit extension matters: without
    it the direct-download engine treats the name as extension-less and replaces it with the CDN
    URL basename (the shortcode)."""
    return _ensure_video_ext(safe_filename(base or shortcode, shortcode))


def resolve_vimeo(vimeo_id, vimeo_hash, session, verbose):
    """Return (hls_master_url, title, duration_seconds) or (None, None, 0)."""
    embed = f"https://player.vimeo.com/video/{vimeo_id}"
    if vimeo_hash:
        embed += f"?h={vimeo_hash}"
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


# ---- YouTube + Twitch (pure requests; both resolve to a standard HLS master) ---------- #
# These API version/key constants occasionally rotate on YouTube's side; they're overridable via
# CONFIG_URL so you can hot-fix every machine at once without editing the script.
YT_IOS_VERSION = "19.45.4"
YT_IOS_UA = "com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"
YT_IOS_KEY = "AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc"
YT_WEB_VERSION = "2.20260114.08.00"
YT_WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
YOUTUBE_HEADERS = {'User-Agent': YT_IOS_UA, 'Origin': 'https://www.youtube.com'}
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_HEADERS = {'User-Agent': USER_AGENT, 'Referer': 'https://www.twitch.tv/'}


def _yt_video_id(url):
    """Extract a YouTube video id from any watch / youtu.be / shorts / embed URL (or a bare id)."""
    if re.fullmatch(r'[0-9A-Za-z_-]{11}', url or ''):
        return url
    for pat in (r'[?&]v=([0-9A-Za-z_-]{11})',
                r'youtu\.be/([0-9A-Za-z_-]{11})',
                r'/shorts/([0-9A-Za-z_-]{11})',
                r'/embed/([0-9A-Za-z_-]{11})',
                r'/live/([0-9A-Za-z_-]{11})'):
        m = re.search(pat, url or '')
        if m:
            return m.group(1)
    return None


def _yt_playlist_id(url):
    m = re.search(r'[?&]list=([0-9A-Za-z_-]+)', url or '')
    # Skip auto-generated non-playlists: RD/RDMM mixes, UL uploads, WL watch-later, LL liked,
    # LM liked-music. Real playlists start with PL/OL/FL etc.
    if m and not m.group(1).startswith(('RD', 'UL', 'WL', 'LL', 'LM')):
        return m.group(1)
    return None


def _innertube_player(video_id, session, verbose):
    """Call the InnerTube iOS 'player' endpoint (returns direct HLS manifest, no JS ciphers)."""
    body = {
        "videoId": video_id,
        "context": {"client": {
            "clientName": "IOS", "clientVersion": YT_IOS_VERSION,
            "deviceModel": "iPhone16,2", "hl": "en", "gl": "US", "userAgent": YT_IOS_UA,
        }},
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
        "contentCheckOk": True, "racyCheckOk": True,
    }
    url = f"https://www.youtube.com/youtubei/v1/player?key={YT_IOS_KEY}&prettyPrint=false"
    headers = {"User-Agent": YT_IOS_UA, "Content-Type": "application/json",
               "X-YouTube-Client-Name": "5", "X-YouTube-Client-Version": YT_IOS_VERSION,
               "Origin": "https://www.youtube.com"}
    try:
        r = session.post(url, json=body, headers=headers,
                         timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        return r.json()
    except (requests.RequestException, ValueError) as e:
        if verbose:
            tqdm.write(f"[WARN] YouTube player fetch failed for {video_id}: {e}")
        return None


# InnerTube clients that return direct format URLs (avoid the WEB SABR-only path).
# Fields: (name, client_context, X-YouTube-Client-Name number, needs_js_player).
# Values mirror yt-dlp's current INNERTUBE_CLIENTS. ANDROID_VR 1.65.10 is special: it returns
# direct googlevideo URLs with NO PO-token, NO SABR, and NO signature/n solving needed, so it
# works even without pywebview. Versions drift over time; update here if a client stops working.
_YT_CLIENTS = [
    ('android_vr', {'clientName': 'ANDROID_VR', 'clientVersion': '1.65.10',
                    'deviceMake': 'Oculus', 'deviceModel': 'Quest 3', 'androidSdkVersion': 32,
                    'osName': 'Android', 'osVersion': '12L',
                    'userAgent': 'com.google.android.apps.youtube.vr.oculus/1.65.10 '
                                 '(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip'},
     28, False),
    ('ios', {'clientName': 'IOS', 'clientVersion': '21.02.3', 'deviceMake': 'Apple',
             'deviceModel': 'iPhone16,2', 'osName': 'iPhone', 'osVersion': '18.3.2.22D82',
             'userAgent': 'com.google.ios.youtube/21.02.3 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)'},
     5, False),
    ('android', {'clientName': 'ANDROID', 'clientVersion': '21.02.35', 'androidSdkVersion': 30,
                 'osName': 'Android', 'osVersion': '11',
                 'userAgent': 'com.google.android.youtube/21.02.35 (Linux; U; Android 11) gzip'},
     3, False),
    ('web', {'clientName': 'WEB', 'clientVersion': '2.20250312.04.00',
             'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'},
     1, True),
    ('tv', {'clientName': 'TVHTML5', 'clientVersion': '7.20260114.12.00',
            'userAgent': 'Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.lts.30.1034943-gold '
                         '(unlike Gecko), Unknown_TV_Unknown_0/Unknown (Unknown, Unknown)'},
     7, True),
]


def _yt_cookie(session, *names):
    """First cookie value matching any of `names`, across any domain — avoids the
    CookieConflictError you'd get from session.cookies.get() when SAPISID exists on both
    .google.com and .youtube.com."""
    for c in session.cookies:
        if c.name in names:
            return c.value
    return None


_YT_AUTH_LOGGED = False


def _yt_auth_headers(session):
    """If the session carries logged-in YouTube cookies, return the SAPISIDHASH Authorization
    headers so InnerTube requests are treated as authenticated (this is what lets cookies bypass
    the 'confirm you're not a bot' wall). Returns {} when not logged in."""
    global _YT_AUTH_LOGGED
    # Prefer __Secure-3PAPISID: it's the value actually sent to youtube.com in a 3rd-party context.
    apisid = _yt_cookie(session, '__Secure-3PAPISID', 'SAPISID', '__Secure-1PAPISID')
    if not apisid:
        return {}
    import hashlib
    origin = 'https://www.youtube.com'
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {apisid} {origin}".encode()).hexdigest()
    # Browsers send all three variants; YouTube accepts whichever matches the cookies it received.
    auth = f"SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} SAPISID3PHASH {ts}_{h}"
    if not _YT_AUTH_LOGGED:
        _YT_AUTH_LOGGED = True
        tqdm.write("[INFO] Using your logged-in YouTube cookies (SAPISIDHASH auth) for requests.")
    return {'Authorization': auth, 'X-Origin': origin,
            'X-Goog-AuthUser': '0'}


_YT_POTOKEN_ACTIVE = None          # PO token (bound to visitorData) to append to stream URLs
_YT_POTOKEN_CACHE = {}             # visitorData -> token
_YT_POTOKEN_TRIED = False          # generate at most once per run

_YT_POTOKEN_SCRIPT = r'''// Generate a YouTube PO token bound to visitorData, via bgutils-js + jsdom.
const { BG, USER_AGENT } = require('bgutils-js');
const { JSDOM } = require('jsdom');
const visitorData = process.argv[2];
const requestKey = 'O43z0dpjhgX20SCx4KAo';
(async () => {
  if (!visitorData) throw new Error('missing visitorData');
  const dom = new JSDOM('<!DOCTYPE html><html><head></head><body></body></html>', {
    url: 'https://www.youtube.com/', referrer: 'https://www.youtube.com/', userAgent: USER_AGENT,
  });
  Object.assign(globalThis, {
    window: dom.window, document: dom.window.document,
    location: dom.window.location, origin: dom.window.origin,
  });
  if (!Object.getOwnPropertyDescriptor(globalThis, 'navigator')) {
    Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
  }
  const bgConfig = {
    fetch: (url, options) => fetch(url, options),
    globalObj: globalThis, identifier: visitorData, requestKey,
  };
  const challenge = await BG.Challenge.create(bgConfig);
  if (!challenge) throw new Error('could not create BotGuard challenge');
  const jsCode = challenge.interpreterJavascript &&
    challenge.interpreterJavascript.privateDoNotAccessOrElseSafeScriptWrappedValue;
  if (jsCode) { new Function(jsCode)(); } else { throw new Error('no interpreter js'); }
  const res = await BG.PoToken.generate({
    program: challenge.program, globalName: challenge.globalName, bgConfig,
  });
  process.stdout.write(JSON.stringify({ poToken: res.poToken, visitorData }));
})().catch((e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
'''


def _yt_potoken_dir():
    base = os.path.dirname(os.path.abspath(sys.argv[0] or '.')) or os.getcwd()
    return os.path.join(base, '.potoken')


def _yt_ensure_potoken_deps(verbose):
    """Set up a .potoken folder with bgutils-js + jsdom (one-time npm install). Returns
    (node_path, script_path) or (None, None) if node/npm are missing or the install failed."""
    import shutil
    node = shutil.which('node')
    if not node:
        if verbose:
            tqdm.write("[WARN] Node.js not found; cannot generate a PO token for high quality.")
        return None, None
    d = _yt_potoken_dir()
    try:
        os.makedirs(d, exist_ok=True)
        script = os.path.join(d, 'potoken.js')
        if not os.path.exists(script):
            with open(script, 'w', encoding='utf-8') as f:
                f.write(_YT_POTOKEN_SCRIPT)
        if not os.path.isdir(os.path.join(d, 'node_modules', 'bgutils-js')):
            npm = shutil.which('npm') or shutil.which('npm.cmd')
            if not npm:
                if verbose:
                    tqdm.write("[WARN] npm not found; cannot install PO-token deps (bgutils-js).")
                return None, None
            print("[INFO] Setting up PO-token support (npm install bgutils-js jsdom) — one-time, "
                  "needs internet...")
            subprocess.run([npm, 'install', 'bgutils-js', 'jsdom', '--no-fund', '--no-audit',
                            '--loglevel', 'error'], cwd=d, capture_output=True, text=True,
                           timeout=300)
        if not os.path.isdir(os.path.join(d, 'node_modules', 'bgutils-js')):
            return None, None
        return node, script
    except Exception as e:
        if verbose:
            tqdm.write(f"[WARN] PO-token setup failed: {e}")
        return None, None


def _yt_get_potoken(visitor_data, verbose):
    """Generate a YouTube PO token (BotGuard) bound to visitorData via Node + bgutils-js, cached
    per visitorData. This is what unlocks non-SABR (downloadable) high-quality formats from the
    WEB client. Returns the token string or None."""
    if not visitor_data:
        return None
    if visitor_data in _YT_POTOKEN_CACHE:
        return _YT_POTOKEN_CACHE[visitor_data]
    node, script = _yt_ensure_potoken_deps(verbose)
    if not node:
        return None
    try:
        if verbose:
            tqdm.write("[DBG] generating PO token via bgutils-js (BotGuard)...")
        proc = subprocess.run([node, script, visitor_data], cwd=_yt_potoken_dir(),
                              capture_output=True, text=True, timeout=90)
        if proc.returncode != 0 or not proc.stdout.strip():
            if verbose:
                tqdm.write(f"[WARN] PO token generation failed: {(proc.stderr or '').strip()[:200]}")
            return None
        tok = (json.loads(proc.stdout) or {}).get('poToken')
        _YT_POTOKEN_CACHE[visitor_data] = tok
        if verbose:
            tqdm.write(f"[DBG] PO token generated ({len(tok or '')} chars)")
        return tok
    except Exception as e:
        if verbose:
            tqdm.write(f"[WARN] PO token error: {e}")
        return None


def _innertube_call(video_id, session, name, client, cnum, verbose, visitor=None, po_token=None):
    ctx = {'clientName': client['clientName'], 'clientVersion': client['clientVersion'],
           'hl': 'en', 'gl': 'US'}
    ctx.update({k: v for k, v in client.items() if k not in ('clientName', 'clientVersion')})
    if visitor:
        ctx['visitorData'] = visitor
    body = {'videoId': video_id, 'context': {'client': ctx},
            'playbackContext': {'contentPlaybackContext': {'html5Preference': 'HTML5_PREF_WANTS'}},
            'contentCheckOk': True, 'racyCheckOk': True}
    if po_token:
        body['serviceIntegrityDimensions'] = {'poToken': po_token}
    url = 'https://www.youtube.com/youtubei/v1/player?prettyPrint=false'
    headers = {'Content-Type': 'application/json', 'User-Agent': client.get('userAgent', USER_AGENT),
               'X-YouTube-Client-Name': str(cnum), 'X-YouTube-Client-Version': client['clientVersion'],
               'Origin': 'https://www.youtube.com'}
    if visitor:
        headers['X-Goog-Visitor-Id'] = visitor
    # Cookie-based SAPISIDHASH auth only makes sense for web-style clients (TVHTML5/WEB/MWEB).
    # App clients (ANDROID*/IOS) authenticate differently and return an EMPTY response if we send
    # it — which is why android_vr/ios were coming back with 0 formats. They rely on visitorData
    # to get past the bot check instead.
    if not str(client.get('clientName', '')).startswith(('ANDROID', 'IOS')):
        headers.update(_yt_auth_headers(session))
    try:
        r = session.post(url, json=body, headers=headers,
                         timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        return r.json()
    except (requests.RequestException, ValueError) as e:
        if verbose:
            tqdm.write(f"[WARN] YouTube {name} player failed: {e}")
        return None


_YT_BOT_HINT_SHOWN = False


def _yt_bot_hint(has_login_cookies=False):
    """Explain YouTube's 'confirm you're not a bot' wall (shown once)."""
    global _YT_BOT_HINT_SHOWN
    if _YT_BOT_HINT_SHOWN:
        return
    _YT_BOT_HINT_SHOWN = True
    lines = ["[INFO] YouTube returned 'Sign in to confirm you're not a bot' for every client."]
    if has_login_cookies:
        lines += [
            "       You DID supply logged-in YouTube cookies, but YouTube still blocked the",
            "       requests. That usually means the cookies are stale/expired, are for a",
            "       different profile, or this account/IP is temporarily flagged. Try:",
            "        - re-export FRESH cookies from a browser tab open on youtube.com (logged",
            "          in) right before running, then pass --cookies <file>,",
            "        - or wait ~15-60 min / switch network / use a VPN."]
    else:
        lines += [
            "       No logged-in YouTube cookies were found in your session, so this is your IP",
            "       being rate-limited. The reliable fix:",
            "        - export YouTube cookies from a browser signed in to youtube.com (a",
            "          cookies.txt with SAPISID / __Secure-3PAPISID) and run  --cookies <file>",
            "          — the tool sends them authenticated (SAPISIDHASH) to bypass this wall.",
            "        - or wait ~15-60 min / switch network / use a VPN, and slow down."]
    tqdm.write("\n".join(lines))


def _yt_streaming_data(video_id, session, verbose, visitor=None):
    """Return (player_dict_with_usable_formats, client_name, needs_js_player) or (None, None, False)."""
    global _YT_POTOKEN_ACTIVE, _YT_POTOKEN_TRIED
    saw_bot = False
    for name, client, cnum, needs_js in _YT_CLIENTS:
        is_web = str(client.get('clientName', '')) in ('WEB', 'MWEB')
        po = None
        if is_web:
            # WEB returns downloadable (non-SABR) high-quality formats only with a valid PO token.
            # Generate it once, lazily (app clients are tried first and need no token when they work).
            if not _YT_POTOKEN_TRIED:
                _YT_POTOKEN_TRIED = True
                _YT_POTOKEN_ACTIVE = _yt_get_potoken(visitor, verbose)
            po = _YT_POTOKEN_ACTIVE
            if not po:
                continue                       # no token -> WEB would only be SABR; skip it
        d = _innertube_call(video_id, session, name, client, cnum, verbose, visitor, po)
        if not d:
            continue
        sd = d.get('streamingData') or {}
        fmts = (sd.get('adaptiveFormats') or []) + (sd.get('formats') or [])
        usable = any(f.get('url') or f.get('signatureCipher') for f in fmts)
        st_obj = d.get('playabilityStatus') or {}
        st = st_obj.get('status')
        reason = st_obj.get('reason') or ''
        if st == 'LOGIN_REQUIRED' and 'bot' in reason.lower():
            saw_bot = True
        if verbose:
            tqdm.write(f"[DBG] client {name}: status={st}{(' - ' + reason) if reason else ''}, "
                  f"formats={len(fmts)}, "
                  f"{'direct URLs' if usable else 'no url/cipher (SABR) - trying next'}")
        if usable:
            return d, name, needs_js
    if saw_bot:
        _yt_bot_hint(bool(_yt_cookie(session, 'SAPISID', '__Secure-3PAPISID', '__Secure-1PAPISID')))
    return None, None, False


def resolve_youtube(video_id, session, verbose):
    """Return (hls_master_url, title, duration_seconds) or (None, None, 0). Only used for the
    rare videos/live streams that expose an HLS manifest; regular VODs go via the DASH path."""
    d = _innertube_player(video_id, session, verbose)
    if not d:
        return None, None, 0
    status = (d.get('playabilityStatus') or {})
    if status.get('status') and status.get('status') != 'OK' and verbose:
        tqdm.write(f"[WARN] YouTube {video_id}: {status.get('status')} - {status.get('reason')}")
    sd = d.get('streamingData') or {}
    master = sd.get('hlsManifestUrl')
    vd = d.get('videoDetails') or {}
    title = vd.get('title')
    try:
        dur = int(vd.get('lengthSeconds') or 0)
    except (TypeError, ValueError):
        dur = 0
    return master, title, dur


# ---- YouTube DASH via node signature/n solver (yt_solver_bundle.js) --------------------- #

def _yt_pywebview_import_error():
    """Return None if pywebview imports, else a short error string explaining why."""
    try:
        import importlib
        importlib.import_module('webview')
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _yt_pywebview_available():
    return _yt_pywebview_import_error() is None


def _yt_can_solve():
    return _yt_pywebview_available()


# YouTube signature/n solver (yt-dlp public-domain AST solver + meriyah/astring parsers),
# gzip+base64-embedded so there is NO external .js file. Run inside a system webview
# (pywebview) as a real JavaScript engine.
_YT_BUNDLE_B64 = (
    "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"
)


def _yt_bundle_js():
    import gzip as _gz
    return _gz.decompress(base64.b64decode(_YT_BUNDLE_B64)).decode('utf-8')


def _yt_solver_html(data):
    """Self-contained HTML: embeds the challenge input + solver bundle, runs jsc() on load,
    stores the JSON result in window.__RESULT__."""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        "<script>window.__INPUT__=" + json.dumps(data) + ";</script>"
        "<script>" + _yt_bundle_js() + "</script>"
        "<script>try{window.__RESULT__=JSON.stringify(jsc(window.__INPUT__));}"
        "catch(e){window.__RESULT__=JSON.stringify({type:'error',error:String(e&&e.message||e)});}"
        "</script></body></html>"
    )


def _yt_webview_worker(html, conn):
    """Separate process: open a hidden webview, wait for window.__RESULT__, send it back.
    A separate process avoids pywebview single-start / main-thread limits."""
    try:
        import webview
    except Exception as e:
        try:
            conn.send({'error': 'pywebview not installed: {}'.format(e)})
            conn.close()
        except Exception:
            pass
        return
    holder = {}

    def _on_start(window):
        import time as _t
        for _ in range(1800):
            try:
                r = window.evaluate_js("window.__RESULT__ || null")
            except Exception:
                r = None
            if r:
                holder['r'] = r
                break
            _t.sleep(0.1)
        try:
            window.destroy()
        except Exception:
            pass

    # Serve the solver page over localhost HTTP. This avoids WebView2's NavigateToString ~2 MB
    # cap on html=... (the embedded base.js is ~2 MB) AND the script/security quirks of file://
    # URLs, which can silently fail to run the page's scripts.
    import http.server
    import threading
    html_bytes = html.encode('utf-8')

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html_bytes)))
            self.end_headers()
            try:
                self.wfile.write(html_bytes)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    try:
        srv = http.server.HTTPServer(('127.0.0.1', 0), _Handler)
    except Exception as e:
        try:
            conn.send({'error': f'local server failed: {e}'})
            conn.close()
        except Exception:
            pass
        return
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        w = webview.create_window('yt', url=f'http://127.0.0.1:{port}/', hidden=True)
        webview.start(_on_start, (w,))
    except Exception as e:
        try:
            conn.send({'error': str(e)})
            conn.close()
        except Exception:
            pass
        return
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
    try:
        conn.send({'result': holder.get('r')})
        conn.close()
    except Exception:
        pass


def _yt_solve_node(data, verbose):
    """Solve via Node.js if it's on PATH — deterministic and far more reliable than a headless
    webview. Returns the parsed solver output dict, or None if node is unavailable/failed."""
    import shutil
    node = shutil.which('node')
    if not node:
        return None
    import tempfile
    import subprocess
    d = tempfile.mkdtemp(prefix='ytsolve_')
    try:
        bundle_path = os.path.join(d, 'bundle.js')
        input_path = os.path.join(d, 'input.json')
        runner_path = os.path.join(d, 'run.js')
        with open(bundle_path, 'w', encoding='utf-8') as f:
            f.write(_yt_bundle_js())
        with open(input_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        runner = (
            "const fs=require('fs');"
            "const b=fs.readFileSync(" + json.dumps(bundle_path) + ",'utf8');"
            "eval('(function(module,exports,define){'+b+"
            "';globalThis.__jsc=jsc;})(undefined,undefined,undefined);');"
            "const input=JSON.parse(fs.readFileSync(" + json.dumps(input_path) + ",'utf8'));"
            "process.stdout.write(JSON.stringify(globalThis.__jsc(input)));"
        )
        with open(runner_path, 'w', encoding='utf-8') as f:
            f.write(runner)
        if verbose:
            tqdm.write("[DBG] solving via Node.js...")
        proc = subprocess.run([node, runner_path], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            if verbose:
                tqdm.write(f"[WARN] node solver failed: {(proc.stderr or '').strip()[:200]}")
            return None
        return json.loads(proc.stdout)
    except Exception as e:
        if verbose:
            tqdm.write(f"[WARN] node solver error: {e}")
        return None
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def _yt_solve_webview(data, verbose):
    """Solve via a hidden system webview (pywebview). Returns the parsed output dict or None."""
    if not _yt_pywebview_available():
        if verbose:
            print("[WARN] pywebview is not installed; cannot solve YouTube signatures.")
        return None
    html = _yt_solver_html(data)
    import multiprocessing as _mp
    got = None
    try:
        ctx = _mp.get_context('spawn')
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_yt_webview_worker, args=(html, child), daemon=True)
        if verbose:
            tqdm.write("[DBG] launching webview solver process...")
        proc.start()
        got = parent.recv() if parent.poll(90) else None
        proc.join(5)
        if proc.is_alive():
            proc.terminate()
    except Exception as e:
        if verbose:
            tqdm.write(f"[WARN] YouTube webview process failed: {e}")
        return None
    if verbose:
        if got is None:
            print("[WARN] webview solver: no response within 90s (WebView2 didn't return a "
                  "result — install Node.js for a reliable solver: https://nodejs.org).")
        elif got.get('error'):
            print(f"[WARN] webview solver error: {got['error']}")
        elif not got.get('result'):
            print("[WARN] webview solver: empty result (WebView2 backend issue).")
        else:
            tqdm.write(f"[DBG] webview solver returned {len(got['result'])} chars")
    if not got or got.get('error') or not got.get('result'):
        return None
    try:
        return json.loads(got['result'])
    except (ValueError, TypeError):
        return None


def _yt_solve(base_js, sig_list, n_list, verbose):
    """Solve signature + n challenges. Tries Node.js first (reliable), then a webview fallback.
    Returns (sig_map, n_map)."""
    reqs = []
    if sig_list:
        reqs.append({'type': 'sig', 'challenges': list(sig_list)})
    if n_list:
        reqs.append({'type': 'n', 'challenges': list(n_list)})
    if not reqs:
        return {}, {}
    data = {'type': 'player', 'player': base_js, 'output_preprocessed': False, 'requests': reqs}
    out = _yt_solve_node(data, verbose)
    if out is None:
        out = _yt_solve_webview(data, verbose)
    if not out:
        return {}, {}
    if out.get('type') != 'result':
        if verbose:
            tqdm.write(f"[WARN] YouTube solver: {out.get('error')}")
        return {}, {}
    sig_map, n_map = {}, {}
    responses = out.get('responses') or []
    if len(responses) < len(reqs) and verbose:
        tqdm.write(f"[WARN] YouTube solver returned {len(responses)} of {len(reqs)} response(s); "
                   f"the missing part stays unsolved (downloads may be throttled).")
    for req, resp in zip(reqs, responses):
        if resp.get('type') == 'result':
            (sig_map if req['type'] == 'sig' else n_map).update(resp.get('data') or {})
    return sig_map, n_map


def _yt_consent(session):
    """Set the SOCS consent cookie so YouTube doesn't serve the 'Before you continue' wall
    (which hides ytInitialData / the real title). Mirrors yt-dlp."""
    try:
        if not any(getattr(c, 'name', '') == 'SOCS' and 'youtube.com' in (getattr(c, 'domain', '') or '')
                   for c in session.cookies):
            session.cookies.set('SOCS', 'CAI', domain='.youtube.com')
    except Exception:
        pass


def _yt_watch_player(video_id, session, verbose):
    """Fetch the watch page and return (player_response_dict, base_js_text, visitor_data). The
    page's player response and its base.js always match, so signatures decode correctly."""
    _yt_consent(session)
    url = (f"https://www.youtube.com/watch?v={video_id}"
           "&bpctr=9999999999&has_verified=1&hl=en")
    headers = {'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'}
    try:
        r = session.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        html = r.text
    except requests.RequestException as e:
        if verbose:
            tqdm.write(f"[WARN] YouTube watch page fetch failed: {e}")
        return None, None, None
    if verbose:
        tqdm.write(f"[DBG] watch page: HTTP {r.status_code}, {len(html)} bytes, "
              f"final url {urlparse(r.url).netloc}{urlparse(r.url).path}")
    player = _extract_json_after(html, 'ytInitialPlayerResponse')
    if player is None:
        player = _extract_json_after(html, '"playerResponse":')
    vm = re.search(r'"visitorData":"([^"]+)"', html)
    visitor = vm.group(1).encode().decode('unicode_escape') if vm else None
    m = re.search(r'"jsUrl":"([^"]+base\.js)"', html) or re.search(r'(/s/player/[^"]+base\.js)', html)
    base_js = None
    if m:
        js_url = m.group(1)
        if js_url.startswith('/'):
            js_url = 'https://www.youtube.com' + js_url
        try:
            base_js = session.get(js_url, headers={'User-Agent': USER_AGENT},
                                  timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)).text
        except requests.RequestException as e:
            if verbose:
                tqdm.write(f"[WARN] base.js fetch failed: {e}")
    if verbose:
        sd = (player or {}).get('streamingData') or {}
        tqdm.write(f"[DBG] playerResponse: {'found' if player else 'MISSING'} | "
              f"base.js: {'found' if base_js else 'MISSING'} | "
              f"visitorData: {'found' if visitor else 'MISSING'} | "
              f"formats: {len(sd.get('formats') or [])} progressive, "
              f"{len(sd.get('adaptiveFormats') or [])} adaptive | "
              f"status: {(player or {}).get('playabilityStatus', {}).get('status')}")
    return player, base_js, visitor


def _extract_json_after(text, marker):
    """Extract the first balanced {...} object appearing after `marker` in text."""
    i = text.find(marker)
    if i < 0:
        return None
    i = text.find('{', i)
    if i < 0:
        return None
    depth, in_str, esc, start = 0, False, False, i
    for j in range(i, len(text)):
        c = text[j]
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
                        return json.loads(text[start:j + 1])
                    except ValueError:
                        return None
    return None


def _yt_usable_fmt(f):
    """A format we can actually build a URL for: it has a direct url or a (signature)cipher.
    Formats with neither are SABR-only (server-driven) and cannot be downloaded this way."""
    return bool(f.get('url') or f.get('signatureCipher') or f.get('cipher'))


def _yt_merge_by_itag(*groups):
    """Merge format lists, deduping by itag and preferring a usable (url/cipher) copy over a
    SABR-only one. Lets us combine the client's formats with the watch page's."""
    by = {}
    for group in groups:
        for f in group or []:
            it = f.get('itag')
            if it is None:
                continue
            if it not in by or (_yt_usable_fmt(f) and not _yt_usable_fmt(by[it])):
                by[it] = f
    return list(by.values())


def _yt_cipher_parts(fmt):
    """Return (needs_sig, s_value, sp_name, base_url) for a format's URL or signatureCipher."""
    if fmt.get('url'):
        return False, None, None, fmt['url']
    cipher = fmt.get('signatureCipher') or fmt.get('cipher') or ''
    q = dict(parse_qsl(cipher))
    return True, q.get('s'), (q.get('sp') or 'signature'), q.get('url')


def _yt_apply(base_url, sig_needed, s_val, sp_name, sig_map, n_map):
    """Build the final videoplayback URL: append the deciphered signature (if any) and swap the
    throttling 'n' parameter for its solved value."""
    if not base_url:
        return None
    url = base_url
    if sig_needed and s_val is not None:
        sig = sig_map.get(s_val)
        if not sig:
            return None
        url += ('&' if '?' in url else '?') + f"{sp_name}={quote(sig, safe='')}"
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if q.get('n') and n_map.get(q['n']):
        q['n'] = n_map[q['n']]
        url = urlunparse(parts._replace(query=urlencode(q)))
    # A PO token (BotGuard) must ride along on the stream URL for WEB-issued formats.
    if _YT_POTOKEN_ACTIVE and 'googlevideo' in url and 'pot=' not in url:
        url += ('&' if '?' in url else '?') + f"pot={quote(_YT_POTOKEN_ACTIVE, safe='')}"
    return url


_RES_LABELS = {4320: '8K', 2160: '4K', 1440: '2K', 1080: 'FHD', 720: 'HD', 480: 'SD'}


def _res_label(h):
    return _RES_LABELS.get(h, '')


def _parse_res(v):
    """Parse a --res value: None (not given), 'SCAN' (list qualities), or a target height int."""
    if v is None:
        return None
    v = str(v).strip().lower()
    if v in ('', 'scan', 'list', 'show'):
        return 'SCAN'
    aliases = {'8k': 4320, '4k': 2160, '2k': 1440, 'fhd': 1080, 'hd': 720, 'sd': 480}
    if v in aliases:
        return aliases[v]
    v2 = v.rstrip('p')
    return int(v2) if v2.isdigit() else None


def _parse_cenc_keys(key_args, keys_file):
    """Collect user-supplied decryption keys from --key / --keys, for content the user ALREADY
    holds keys for (their own protected storage). Accepts 'KID:KEY' (preferred) or a bare 32-hex
    'KEY'. Returns validated 'KID:KEY'/'KEY' strings for mp4decrypt. This is not key extraction
    or DRM circumvention — the user supplies their own keys."""
    raw = list(key_args or [])
    if keys_file:
        try:
            with open(keys_file, encoding='utf-8') as f:
                raw += [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith('#')]
        except OSError as e:
            print(f"[WARN] --keys: could not read {keys_file}: {e}")
    specs = []
    for item in raw:
        item = item.strip()
        if ':' in item:
            kid, key = item.split(':', 1)
            kid = kid.strip().lower().replace('0x', '')
            key = key.strip().lower().replace('0x', '')
            if re.fullmatch(r'[0-9a-f]{32}', key) and re.fullmatch(r'[0-9a-f]{1,32}', kid):
                specs.append(f"{kid}:{key}")
                continue
        else:
            key = item.lower().replace('0x', '')
            if re.fullmatch(r'[0-9a-f]{32}', key):
                specs.append(key)
                continue
        if item:
            print(f"[WARN] --key: ignoring '{item[:40]}' (need 32-hex KEY, optionally KID:KEY).")
    return specs


def _parse_track_sel(v):
    """Parse --audio/--sub: None (not given -> all), 'SCAN' (list), 'ALL', or [ints] (1-based)."""
    if v is None:
        return None
    v = str(v).strip().lower()
    if v in ('', 'scan', 'list', 'show'):
        return 'SCAN'
    if v in ('all', 'a'):
        return 'ALL'
    idxs = [int(p) for p in v.replace(' ', '').split(',') if p.isdigit()]
    return idxs or 'ALL'


def _yt_scan_formats(video_id, session, verbose):
    """Print the video qualities YouTube offers for this video, with codec/fps/size, so the
    user can re-run with --res <height>."""
    wp_player, _base_js, visitor = _yt_watch_player(video_id, session, verbose)
    player, _client, _needs_js = _yt_streaming_data(video_id, session, verbose, visitor)
    if player is None:
        player = wp_player
    if not player:
        print(f"[ERROR] Could not read formats for {video_id}.")
        return
    sd = player.get('streamingData') or {}
    wp_sd = (wp_player or {}).get('streamingData') or {}
    title = (player.get('videoDetails') or {}).get('title')
    if not title and wp_player:
        title = (wp_player.get('videoDetails') or {}).get('title')
    title = (title or video_id).strip()
    # Merge client + watch-page formats and only list ones we can actually download (usable),
    # so the scan matches what --res N will really fetch.
    adaptive = [f for f in _yt_merge_by_itag(sd.get('adaptiveFormats'), wp_sd.get('adaptiveFormats'))
                if _yt_usable_fmt(f)]
    vids = [f for f in adaptive if f.get('mimeType', '').startswith('video/mp4') and f.get('height')]
    if not vids:
        vids = [f for f in adaptive if f.get('mimeType', '').startswith('video/') and f.get('height')]
    if not vids:
        print(f"[WARN] No downloadable video formats found for: {title}")
        return
    print(f"\n[INFO] Available qualities for: {title}")
    seen = set()
    for f in sorted(vids, key=lambda f: (f.get('height', 0), f.get('fps', 0)), reverse=True):
        h = f.get('height')
        fps = f.get('fps') or 0
        if (h, fps) in seen:
            continue
        seen.add((h, fps))
        mt = f.get('mimeType', '')
        codec = ('AV1' if 'av01' in mt else 'VP9' if 'vp9' in mt
                 else 'H.264' if 'avc1' in mt else '?')
        cl = f.get('contentLength')
        size = f"~{int(cl) / 1e6:.0f} MB" if cl and str(cl).isdigit() else '?'
        tag = _res_label(h) + (' 60fps' if fps >= 50 else '')
        print(f"   --res {str(h):<5} {tag:<10} {codec:<6} {str(fps) + 'fps':<7} {size}")
    print("\nRe-run to download a quality, e.g.  --res 1080   or   --res 4k")
    print("(no --res = best; --res N = best video up to N tall, preferring 60fps.)\n")


def _yt_gather(video_id, session, max_height, verbose):
    """Resolve a video's formats and collect its sig/n challenges WITHOUT solving them (so a
    whole playlist can be solved in one webview session). Returns a plan dict, None, or
    {'__no_solver__': True, ...}."""
    wp_player, base_js, visitor = _yt_watch_player(video_id, session, verbose)
    player, client, needs_js = _yt_streaming_data(video_id, session, verbose, visitor)
    if player is None:
        player, needs_js = wp_player, True
    if not player:
        return None
    status = (player.get('playabilityStatus') or {})
    if status.get('status') and status.get('status') != 'OK':
        if verbose:
            tqdm.write(f"[WARN] YouTube {video_id}: {status.get('status')} - {status.get('reason')}")
        if status.get('status') in ('LOGIN_REQUIRED', 'UNPLAYABLE', 'ERROR'):
            return None
    sd = player.get('streamingData') or {}
    title = (player.get('videoDetails') or {}).get('title')
    if not title and wp_player:
        # The TV/embedded client player often omits videoDetails; the watch page always has it.
        title = (wp_player.get('videoDetails') or {}).get('title')
    title = (title or video_id).strip()

    # Merge the chosen client's formats with the watch-page (WEB) formats and dedupe by itag,
    # preferring a usable copy (direct url or signatureCipher) over a SABR-only one. The client
    # often returns high-res formats as SABR (no URL), while the watch page carries the same
    # itags with a signatureCipher we CAN solve — so this recovers the best downloadable quality.
    wp_sd = (wp_player or {}).get('streamingData') or {}
    adaptive = _yt_merge_by_itag(sd.get('adaptiveFormats'), wp_sd.get('adaptiveFormats'))
    progressive = _yt_merge_by_itag(sd.get('formats'), wp_sd.get('formats'))
    cap = max_height or 10000

    vids = [f for f in adaptive if (f.get('mimeType', '').startswith('video/mp4'))
            and f.get('height') and _yt_usable_fmt(f)]
    auds = [f for f in adaptive if f.get('mimeType', '').startswith('audio/mp4')
            and _yt_usable_fmt(f)]
    chosen, mode = [], None
    if vids and auds:
        # Best = highest resolution, then highest fps (so 1080p60 beats 1080p30), then bitrate.
        vids.sort(key=lambda f: (f.get('height', 0), f.get('fps', 0), f.get('bitrate', 0)))
        pick = [f for f in vids if f.get('height', 0) <= cap] or vids
        a = max(auds, key=lambda f: f.get('bitrate', 0))
        chosen, mode = [('video', pick[-1]), ('audio', a)], 'adaptive'
    elif progressive:
        pr = [f for f in progressive if f.get('height') and _yt_usable_fmt(f)]
        pr.sort(key=lambda f: f.get('height', 0))
        pick = [f for f in pr if f.get('height', 0) <= cap] or pr
        chosen, mode = [('muxed', pick[-1] if pick else progressive[-1])], 'progressive'
    else:
        if verbose:
            tqdm.write(f"[WARN] {video_id}: no usable mp4 formats "
                  f"(adaptive={len(adaptive)}, progressive={len(progressive)}). "
                  f"YouTube likely wants a PO-token / sign-in for this request.")
        return None

    sig_needed, n_needed, parsed = set(), set(), []
    for role, fmt in chosen:
        needs, s_val, sp, base_url = _yt_cipher_parts(fmt)
        if needs_js and needs and s_val:
            sig_needed.add(s_val)
        if base_url:
            nq = dict(parse_qsl(urlparse(base_url).query)).get('n')
            if nq:
                n_needed.add(nq)
        parsed.append((role, needs, s_val, sp, base_url))
    if needs_js and sig_needed and not (_yt_can_solve() and base_js is not None):
        return {'__no_solver__': True, 'title': title}
    if verbose:
        tqdm.write(f"[DBG] {video_id}: {mode} itag {chosen[0][1].get('itag')} | client '{client}' "
              f"needs_js={needs_js} | {len(sig_needed)} sig, {len(n_needed)} n")
    return {'title': title, 'mode': mode, 'parsed': parsed, 'needs_js': needs_js,
            'base_js': base_js, 'sig': sig_needed, 'n': n_needed}


def _yt_build(plan, sig_map, n_map, verbose):
    """Turn a gathered plan + solved maps into a download descriptor, or None."""
    if not plan or plan.get('__no_solver__'):
        return plan
    out = {'title': plan['title'], 'mode': plan['mode']}
    for role, needs, s_val, sp, base_url in plan['parsed']:
        final = _yt_apply(base_url, needs and plan['needs_js'], s_val, sp, sig_map, n_map)
        if not final:
            if verbose:
                tqdm.write(f"[WARN] {plan['title']}: could not build final URL for {role}.")
            return None
        out[role + '_url' if role != 'muxed' else 'url'] = final
    return out


def _yt_pick_streams(video_id, session, max_height, verbose):
    """Single-video resolve: gather + solve this one video + build. Returns a descriptor,
    None, or {'__no_solver__': True}."""
    plan = _yt_gather(video_id, session, max_height, verbose)
    if not plan or plan.get('__no_solver__'):
        return plan
    sig_map, n_map = {}, {}
    if (plan['sig'] and plan['needs_js']) or plan['n']:
        if _yt_can_solve() and plan['base_js'] is not None:
            sig_map, n_map = _yt_solve(plan['base_js'],
                                       plan['sig'] if plan['needs_js'] else set(),
                                       plan['n'], verbose)
        elif plan['n'] and verbose:
            tqdm.write(f"[WARN] {video_id}: no webview — 'n' left untransformed; download will be "
                  f"throttled. Install pywebview for full speed.")
    return _yt_build(plan, sig_map, n_map, verbose)


def download_youtube(items, session, chunk_size, max_connections, max_height, verbose):
    """Resolve each YouTube item (parallel) to DASH streams, download video+audio via the
    shared pool, and mux with ffmpeg — same workers/threads/connections/rename as everything
    else. `items` are descriptors with a 'youtube_id'."""
    from concurrent.futures import ThreadPoolExecutor

    print(f"[INFO] Resolving {len(items)} YouTube video(s)...")
    resolve_bar = make_bar(total=len(items), desc='Resolving', unit='video', leave=False)
    lock = threading.Lock()

    def _gather(it):
        try:
            return it, _yt_gather(it['youtube_id'], session, max_height, verbose)
        except Exception as exc:
            if verbose:
                tqdm.write(f"[WARN] Could not resolve {it.get('title')}: {exc}")
            return it, None
        finally:
            with lock:
                resolve_bar.update(1)

    plans = []
    with ThreadPoolExecutor(max_workers=min(max_connections, 8)) as ex:
        for it, plan in ex.map(_gather, items):
            plans.append((it, plan))
    resolve_bar.close()

    # Solve ALL sig/n challenges across the whole batch in a SINGLE webview session (instead of
    # one per video). base.js is the same for every video (same player).
    all_sig, all_n, base_js = set(), set(), None
    for _it, plan in plans:
        if plan and not plan.get('__no_solver__'):
            base_js = base_js or plan.get('base_js')
            if plan['needs_js']:
                all_sig |= plan['sig']
            all_n |= plan['n']
    sig_map, n_map = {}, {}
    if (all_sig or all_n) and base_js is not None and _yt_can_solve():
        print(f"[INFO] Solving {len(all_sig)} signature + {len(all_n)} throttle challenge(s) "
              f"in one webview pass...")
        sig_map, n_map = _yt_solve(base_js, all_sig, all_n, verbose)
        if verbose:
            tqdm.write(f"[DBG] batch solved {len(sig_map)}/{len(all_sig)} sig, {len(n_map)}/{len(all_n)} n")
    elif all_n and not _yt_can_solve():
        print("[WARN] pywebview not available — 'n' left untransformed; downloads will be slow.")

    resolved = [(it, _yt_build(plan, sig_map, n_map, verbose) if plan else None)
                for it, plan in plans]

    raw_jobs, mux_plan = [], []   # raw_jobs: direct-url entries; mux_plan: (final, video_tmp, audio_tmp|None)
    for it, desc in resolved:
        if not desc:
            print(f"[WARN] Skipped (no downloadable stream): {it.get('title')}")
            continue
        if desc.get('__no_solver__'):
            err = _yt_pywebview_import_error()
            print("[ERROR] This video's only streams need signature solving, but pywebview isn't")
            print("        usable in this Python. " + (f"({err}) " if err else ""))
            print(f'        Fix: "{sys.executable}" -m pip install pywebview')
            return
        base = safe_filename(prefer_mp4_ext(desc['title']), it['youtube_id'])
        stem = os.path.splitext(base)[0]
        # The RAW streams keep their native container (.mp4/.m4a) inside .temp; only the finished
        # file honours --container (ffmpeg writes it directly, so no extra remux is needed).
        final_ext = _container_ext(False)
        if desc['mode'] == 'progressive':
            raw_jobs.append({'direct_url': desc['url'], 'id': it['youtube_id'],
                             'title': stem, 'name': stem + '.mp4'})
            mux_plan.append((stem + final_ext, os.path.join(TEMP_SUBDIR, stem + '.mp4'), None))
        else:
            vname, aname = stem + '.ytv.mp4', stem + '.yta.m4a'
            raw_jobs.append({'direct_url': desc['video_url'], 'id': it['youtube_id'] + ':v',
                             'title': vname, 'name': vname})
            raw_jobs.append({'direct_url': desc['audio_url'], 'id': it['youtube_id'] + ':a',
                             'title': aname, 'name': aname})
            mux_plan.append((stem + final_ext, os.path.join(TEMP_SUBDIR, vname),
                             os.path.join(TEMP_SUBDIR, aname)))

    if not raw_jobs:
        print("[INFO] Nothing to download (YouTube).")
        return

    # Download the raw video/audio streams INTO .temp (so they never clutter the folder; only
    # the finished .mp4 lands next to the script). googlevideo gives each range request a fast
    # initial burst then throttles, so small segments + a modest connection count keep speed up.
    # record=False is essential: these are raw intermediates inside .temp. Recording them would
    # also run --container's remux on them (renaming .ytv.mp4 -> .ytv.mkv and deleting the
    # original), after which the mux step below would find nothing to mux.
    download_folder_pooled(raw_jobs, session, chunk_size, verbose, label="YouTube",
                           conn_cap=16, seg_mib=5, dest_subdir=TEMP_SUBDIR, record=False)

    if not ensure_ffmpeg(verbose):
        print("[ERROR] ffmpeg is required to finalize YouTube videos.")
        return
    for final, vfile, afile in mux_plan:
        vpath = vfile if os.path.isabs(vfile) else os.path.join(os.getcwd(), vfile)
        final_path = os.path.join(os.getcwd(), final)
        if afile is None:
            # Progressive: the downloaded file IS the result. Same container -> just move it out
            # of .temp; a forced different container (--container mkv) needs a real remux, since
            # renaming .mp4 to .mkv would only mislabel it.
            if not os.path.exists(vpath):
                print(f"[WARN] Missing stream: {final} (nothing downloaded)")
                continue
            if os.path.splitext(vpath)[1].lower() == os.path.splitext(final_path)[1].lower():
                try:
                    os.replace(vpath, final_path)
                except OSError as e:
                    print(f"[WARN] Could not finalize {final}: {e}")
                    continue
            else:
                ok, err = _ffmpeg_mux(vpath, None, final_path, verbose)
                if not ok:
                    print(f"[WARN] Could not convert {final} to {FORCE_CONTAINER}: {err}")
                    continue
                try:
                    os.remove(vpath)
                except OSError:
                    pass
            _record_download(os.path.abspath(final_path))
            tqdm.write(f"[OK] {final}")
            continue
        apath = afile if os.path.isabs(afile) else os.path.join(os.getcwd(), afile)
        if not (os.path.exists(vpath) and os.path.exists(apath)):
            print(f"[WARN] Missing stream for mux: {final} "
                  f"(video: {os.path.exists(vpath)}, audio: {os.path.exists(apath)})")
            continue
        ok, err = _ffmpeg_mux(vpath, apath, final_path, verbose)
        if ok:
            for t in (vpath, apath):
                try:
                    os.remove(t)
                except OSError:
                    pass
            _record_download(os.path.abspath(final_path))
            tqdm.write(f"[OK] {final}")
        else:
            print(f"[WARN] Mux failed for {final}: {err}")


def _yt_iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _yt_iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _yt_iter_dicts(v)


def _yt_browse(continuation_or_id, session, is_continuation, visitor=None):
    url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
    ctx = {"client": {"clientName": "WEB", "clientVersion": YT_WEB_VERSION, "hl": "en", "gl": "US"}}
    if visitor:
        ctx["client"]["visitorData"] = visitor
    body = {"context": ctx}
    if is_continuation:
        body["continuation"] = continuation_or_id
    else:
        body["browseId"] = "VL" + continuation_or_id
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               "X-YouTube-Client-Name": "1", "X-YouTube-Client-Version": YT_WEB_VERSION,
               "Origin": "https://www.youtube.com"}
    if visitor:
        headers["X-Goog-Visitor-Id"] = visitor
    r = session.post(url, json=body, headers=headers,
                     timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    return r.json()


def _yt_playlist_items_from(obj):
    """Walk a browse/ytInitialData object and yield (video_id, title); also return the next
    continuation token. Handles both the classic playlistVideoRenderer and the newer
    lockupViewModel layouts."""
    items, token = [], None
    for d in _yt_iter_dicts(obj):
        pvr = d.get('playlistVideoRenderer')
        if isinstance(pvr, dict) and pvr.get('videoId'):
            t = pvr.get('title') or {}
            title = (t['runs'][0].get('text', '') if isinstance(t.get('runs'), list) and t['runs']
                     else t.get('simpleText') or '')
            items.append((pvr['videoId'], title))
        lvm = d.get('lockupViewModel')
        if isinstance(lvm, dict) and lvm.get('contentId') and \
                (lvm.get('contentType') in (None, 'LOCKUP_CONTENT_TYPE_VIDEO')):
            title = ''
            meta = (((lvm.get('metadata') or {}).get('lockupMetadataViewModel') or {})
                    .get('title') or {})
            if isinstance(meta, dict):
                title = meta.get('content') or ''
            if len(lvm['contentId']) == 11:
                items.append((lvm['contentId'], title))
        cc = d.get('continuationCommand')
        if isinstance(cc, dict) and cc.get('token'):
            token = cc['token']
    return items, token


def _yt_playlist_title_from(obj):
    """Find a playlist's real title in ytInitialData / browse JSON, across old and new layouts."""
    for d in _yt_iter_dicts(obj):
        for key in ('playlistMetadataRenderer', 'microformatDataRenderer'):
            r = d.get(key)
            if isinstance(r, dict) and isinstance(r.get('title'), str) and r['title'].strip():
                return r['title'].strip()
        ph = d.get('playlistHeaderRenderer')
        if isinstance(ph, dict) and isinstance(ph.get('title'), dict):
            t = ph['title']
            if isinstance(t.get('runs'), list) and t['runs']:
                return (t['runs'][0].get('text') or '').strip()
            if t.get('simpleText'):
                return t['simpleText'].strip()
        phv = d.get('pageHeaderViewModel')
        if isinstance(phv, dict):
            tt = (((phv.get('title') or {}).get('dynamicTextViewModel') or {}).get('text') or {})
            if isinstance(tt, dict) and tt.get('content'):
                return tt['content'].strip()
    return None


def _yt_bad_title(t):
    return not t or 'before you continue' in t.lower()


def youtube_playlist_videos(playlist_id, session, verbose):
    """Enumerate a playlist's videos, following continuations. Uses the playlist page first
    (gives the first batch + visitorData), then InnerTube browse for the rest.
    Returns (videos, playlist_title|None)."""
    videos, seen = [], set()
    visitor, first_json, pl_title = None, None, None
    _yt_consent(session)
    # 1) Playlist page: robust first batch + visitorData.
    try:
        r = session.get(f"https://www.youtube.com/playlist?list={playlist_id}&hl=en&gl=US",
                        headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        html = r.text
        first_json = _extract_json_after(html, 'ytInitialData')
        vm = re.search(r'"visitorData":"([^"]+)"', html)
        if vm:
            visitor = vm.group(1).encode().decode('unicode_escape')
        if verbose:
            tqdm.write(f"[DBG] playlist page: HTTP {r.status_code}, ytInitialData "
                  f"{'found' if first_json else 'MISSING'}, visitorData "
                  f"{'found' if visitor else 'MISSING'}")
    except requests.RequestException as e:
        if verbose:
            print(f"[WARN] playlist page fetch failed: {e}")

    token = None
    if first_json is not None:
        pl_title = _yt_playlist_title_from(first_json)
        items, token = _yt_playlist_items_from(first_json)
        for vid, title in items:
            if vid not in seen:
                seen.add(vid)
                videos.append({'source': 'youtube', 'youtube_id': vid, 'title': title or vid})
        if verbose:
            tqdm.write(f"[DBG] playlist page: {len(items)} item(s), "
                  f"continuation {'yes' if token else 'no'}")
    if token is None and not videos:
        # Fall back to a direct InnerTube browse (VL<id>).
        try:
            j = _yt_browse(playlist_id, session, False, visitor)
            if _yt_bad_title(pl_title):
                pl_title = _yt_playlist_title_from(j)
            items, token = _yt_playlist_items_from(j)
            for vid, title in items:
                if vid not in seen:
                    seen.add(vid)
                    videos.append({'source': 'youtube', 'youtube_id': vid, 'title': title or vid})
        except (requests.RequestException, ValueError) as e:
            if verbose:
                print(f"[WARN] playlist browse failed: {e}")

    # 2) Follow continuations via InnerTube browse.
    for _page in range(300):
        if not token:
            break
        try:
            j = _yt_browse(token, session, True, visitor)
        except (requests.RequestException, ValueError) as e:
            if verbose:
                print(f"[WARN] playlist continuation failed: {e}")
            break
        items, token = _yt_playlist_items_from(j)
        added = 0
        for vid, title in items:
            if vid not in seen:
                seen.add(vid)
                videos.append({'source': 'youtube', 'youtube_id': vid, 'title': title or vid})
                added += 1
        if verbose:
            tqdm.write(f"[DBG] playlist continuation: +{added} (total {len(videos)})")
        if added == 0:
            break
    return videos, (None if _yt_bad_title(pl_title) else pl_title)


def youtube_playlist_title(playlist_id, session, verbose):
    _yt_consent(session)
    try:
        r = session.get(f"https://www.youtube.com/playlist?list={playlist_id}&hl=en",
                        headers={'User-Agent': USER_AGENT}, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        m = re.search(r'"title":"([^"]+)","description"', r.text) or \
            re.search(r'<title>([^<]+?)(?:\s*-\s*YouTube)?</title>', r.text)
        if m:
            return m.group(1).encode().decode('unicode_escape').strip()
    except requests.RequestException:
        pass
    try:
        j = _yt_browse(playlist_id, session, False)
    except (requests.RequestException, ValueError):
        return None
    for d in _yt_iter_dicts(j):
        md = d.get('playlistMetadataRenderer') or d.get('microformatDataRenderer')
        if isinstance(md, dict) and md.get('title'):
            return md['title']
    return None


def _twitch_vod_id(url):
    m = re.search(r'twitch\.tv/videos/(\d+)', url or '') or \
        re.search(r'twitch\.tv/\w+/v(?:ideo)?/(\d+)', url or '')
    return m.group(1) if m else None


def _twitch_gql(body, session):
    r = session.post("https://gql.twitch.tv/gql", json=body,
                     headers={"Client-ID": TWITCH_CLIENT_ID, "User-Agent": USER_AGENT},
                     timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    return r.json()


def _twitch_vod_title(vod_id, session):
    try:
        d = _twitch_gql({"query": "query{video(id:\"%s\"){title owner{displayName}}}" % vod_id},
                        session)
        v = (d.get('data') or {}).get('video') or {}
        owner = (v.get('owner') or {}).get('displayName')
        title = v.get('title')
        if title and owner:
            return f"{owner} - {title}"
        return title or None
    except (requests.RequestException, ValueError):
        return None


def resolve_twitch(vod_id, session, verbose):
    """Return (hls_master_url, title) for a Twitch VOD, or (None, None)."""
    query = ("query PlaybackAccessToken_Template($login: String!, $isLive: Boolean!, "
             "$vodID: ID!, $isVod: Boolean!, $playerType: String!, $platform: String!) { "
             "streamPlaybackAccessToken(channelName: $login, params: {platform: $platform, "
             "playerBackend: \"mediaplayer\", playerType: $playerType}) @include(if: $isLive) "
             "{ value signature authorization { isForbidden forbiddenReasonCode } __typename } "
             "videoPlaybackAccessToken(id: $vodID, params: {platform: $platform, "
             "playerBackend: \"mediaplayer\", playerType: $playerType}) @include(if: $isVod) "
             "{ value signature __typename } }")
    body = {"operationName": "PlaybackAccessToken_Template", "query": query,
            "variables": {"isLive": False, "login": "", "isVod": True,
                          "vodID": vod_id, "playerType": "site", "platform": "web"}}
    try:
        d = _twitch_gql(body, session)
        tok = (d.get('data') or {}).get('videoPlaybackAccessToken') or {}
        value, sig = tok.get('value'), tok.get('signature')
        if not (value and sig):
            if verbose:
                print(f"[WARN] Twitch: no access token for VOD {vod_id} (sub-only? needs cookies).")
            return None, None
    except (requests.RequestException, ValueError) as e:
        if verbose:
            print(f"[WARN] Twitch token fetch failed for {vod_id}: {e}")
        return None, None
    params = {'sig': sig, 'token': value, 'allow_source': 'true', 'allow_audio_only': 'true',
              'allow_spectre': 'false', 'p': str(random.randint(0, 9999999)),
              'player': 'twitchweb', 'platform': 'web', 'supported_codecs': 'av1,h265,h264',
              'playlist_include_framerate': 'true', 'reassignments_supported': 'true', 'type': 'any'}
    master = f"https://usher.ttvnw.net/vod/{vod_id}.m3u8?" + urlencode(params)
    return master, (_twitch_vod_title(vod_id, session) or f"twitch_{vod_id}")


def resolve_master(video, session, verbose):
    """Resolve a video descriptor to (hls_master_url, headers). Returns (None, {}) on failure."""
    src = video.get('source')
    if video.get('headers') and video.get('master_url'):
        return video['master_url'], video['headers']   # e.g. --scan HLS with page-derived headers
    if src == 'mux':
        return video.get('master_url'), MUX_HEADERS
    if src == 'twitch':
        if video.get('twitch_id'):
            master, _t = resolve_twitch(video['twitch_id'], session, verbose)
            return master, TWITCH_HEADERS
        return video.get('master_url'), TWITCH_HEADERS
    if src == 'youtube':
        if video.get('youtube_id'):
            master, _t, _d = resolve_youtube(video['youtube_id'], session, verbose)
            return master, YOUTUBE_HEADERS
        return video.get('master_url'), YOUTUBE_HEADERS
    master, _title, _dur = resolve_vimeo(video['vimeo_id'], video['vimeo_hash'], session, verbose)
    return master, VIMEO_HEADERS


AUDIO_SEL = None                  # None/'ALL' = all audio tracks; 'SCAN' = list; [ints] = selection
SUB_SEL = None                    # None/'ALL' = all subtitles; 'SCAN' = list; [ints] = selection
SUBS_ONLY = False                 # True (--subs-only): download ONLY chosen-language subtitles
MUSE_PASSWORD = None              # --password: for a single password-protected muse.ai video
CENC_KEYS = []                    # user-supplied CENC/Widevine content keys (hex), for content you
#                                   already hold keys for (own storage). NOT key extraction/DRM bypass.
FORCE_CONTAINER = None            # None = auto (.mkv when multi-track, else .mp4); 'mp4'/'mkv' force it


def _parse_media_attrs(line):
    """Parse KEY=VALUE attributes off an #EXT-X-MEDIA line (values may be quoted)."""
    attrs = {}
    for m in re.finditer(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line):
        attrs[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return attrs


def parse_master_tracks(master_url, session, max_height, headers, verbose):
    """Fetch the HLS master and return {'video':url, 'audios':[{uri,name,lang,default}],
    'subs':[...]} for the chosen video quality, or None."""
    try:
        r = session.get(master_url, headers=headers, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
    except requests.RequestException as e:
        if verbose:
            print(f"[WARN] HLS master fetch failed: {e}")
        return None
    if r.status_code != 200:
        if verbose:
            print(f"[WARN] HLS master returned {r.status_code}")
        return None
    lines = r.text.splitlines()
    audios, subs = [], []
    for line in lines:
        if line.startswith('#EXT-X-MEDIA:'):
            a = _parse_media_attrs(line)
            typ, uri = a.get('TYPE'), a.get('URI')
            if not uri:
                continue
            entry = {'uri': urljoin(master_url, uri),
                     'name': a.get('NAME') or a.get('LANGUAGE') or '',
                     'lang': a.get('LANGUAGE') or '', 'default': a.get('DEFAULT') == 'YES'}
            if typ == 'AUDIO':
                entry['name'] = entry['name'] or f"audio{len(audios) + 1}"
                audios.append(entry)
            elif typ == 'SUBTITLES':
                entry['name'] = entry['name'] or f"sub{len(subs) + 1}"
                subs.append(entry)
    variants = []
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
        return None
    variants.sort(key=lambda v: (v[0], v[1]))
    if max_height and max_height > 0:
        eligible = [v for v in variants if v[0] <= max_height]
        chosen = eligible[-1] if eligible else variants[0]
    else:
        chosen = variants[-1]
    if verbose:
        print(f"[INFO] Selected video {chosen[0]}p | {len(audios)} audio, {len(subs)} subtitle "
              f"track(s)")
    return {'video': chosen[2], 'audios': audios, 'subs': subs}


def _select_tracks(tracks, sel):
    """Filter a track list by a selection: None/'ALL' -> all, [ints] -> those 1-based indices."""
    if not tracks or sel in (None, 'ALL'):
        return list(tracks)
    if isinstance(sel, list):
        return [tracks[i - 1] for i in sel if 1 <= i <= len(tracks)]
    return list(tracks)


def _hls_list_tracks(title, audios, subs):
    """Print the audio/subtitle tracks a stream offers so the user can pick with --audio/--sub."""
    out = [f"\n[INFO] Tracks for: {title}"]
    if audios:
        out.append("  Audio (choose with --audio N[,N...] ; default = all):")
        for i, a in enumerate(audios, 1):
            out.append(f"     --audio {i:<2} {a.get('name') or '?'} [{a.get('lang') or '?'}]"
                       + ("  (default)" if a.get('default') else ""))
    else:
        out.append("  Audio: single/muxed track — nothing to choose (this stream has one audio).")
    if subs:
        out.append("  Subtitles (choose with --sub N[,N...] ; default = all):")
        for i, s in enumerate(subs, 1):
            out.append(f"     --sub {i:<2} {s.get('name') or '?'} [{s.get('lang') or '?'}]"
                       + ("  (default)" if s.get('default') else ""))
    else:
        out.append("  Subtitles: none found in this stream.")
    out.append("")
    tqdm.write("\n".join(out))


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
    """Download one HLS segment to `path` (atomic). Returns (ok, reason). Retries with
    exponential backoff on rate-limiting (429) and transient server/network errors, so busy
    CDNs (which throttle bursts of parallel requests) don't fail the whole video."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True, None
    attempts = 6
    last = None
    for attempt in range(attempts):
        try:
            with session.get(url, stream=True, headers=headers,
                             timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    last = ("rate-limited (HTTP 429)" if r.status_code == 429
                            else f"server error (HTTP {r.status_code})")
                    if attempt < attempts - 1:
                        ra = r.headers.get('Retry-After')
                        delay = (float(ra) if (ra and ra.isdigit()) else min(2 ** attempt, 20))
                        time.sleep(delay + random.uniform(0, 0.5))
                        continue
                    return False, last
                if r.status_code != 200:
                    if r.status_code in (403, 410):
                        last = (f"HTTP {r.status_code} (expired/invalid signed URL — re-run to "
                                f"refresh the link)")
                    else:
                        last = f"HTTP {r.status_code}"
                    if attempt < 2:
                        time.sleep(0.5)
                        continue
                    return False, last
                tmp = path + '.tmp'
                # copyfileobj runs the transfer loop in C: with a hundred-plus parallel segment
                # threads a Python-level chunk loop burns a lot of CPU for no extra throughput.
                with open(tmp, 'wb') as f:
                    r.raw.decode_content = True
                    shutil.copyfileobj(r.raw, f, 1024 * 1024)
                os.replace(tmp, path)
                return True, None
        except requests.RequestException as e:
            last = f"network error: {type(e).__name__}"
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 10) + random.uniform(0, 0.4))
                continue
            return False, last
    return False, last


def _concat_stream(parts, out_file):
    """Concatenate the init segment + media segments (in order) into one file."""
    with open(out_file, 'wb') as out:
        for p in parts:
            with open(p, 'rb') as f:
                shutil.copyfileobj(f, out, length=8 * 1024 * 1024)


def _ffprobe_duration(url, headers, verbose):
    """Best-effort total duration (seconds) via ffprobe, so the ffmpeg grab can show a % bar.
    Returns a float or None."""
    import shutil
    p = FFMPEG or 'ffmpeg'
    d, name = os.path.split(p)
    probe = os.path.join(d, name.lower().replace('ffmpeg', 'ffprobe')) if 'ffmpeg' in name.lower() \
        else 'ffprobe'
    if not os.path.isfile(probe):
        probe = shutil.which('ffprobe')
    if not probe:
        return None
    ua = (headers or {}).get('User-Agent', USER_AGENT)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if k.lower() != 'user-agent')
    cmd = [probe, '-v', 'quiet', '-user_agent', ua]
    if extra:
        cmd += ['-headers', extra]
    cmd += ['-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out.replace('.', '', 1).isdigit() else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _run_ffmpeg_progress(cmd, tmp, out_path, label, total_dur, verbose):
    """Run an ffmpeg command (which must end with the temp output path) while showing a live
    progress bar parsed from ffmpeg -progress. Returns (ok, error_text)."""
    full = cmd[:1] + ['-progress', 'pipe:1', '-nostats'] + cmd[1:]
    if total_dur and total_dur > 0:
        bar = make_bar(total=int(total_dur), desc=label, unit='s', leave=True,
                       bar_format='{desc} {percentage:3.0f}% |{bar}| {n_fmt}/{total_fmt}s')
    else:
        bar = make_bar(desc=label, unit='s', leave=True,
                       bar_format='{desc} {n_fmt}s processed  {elapsed}')
    last = 0
    try:
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except OSError as e:
        bar.close()
        return False, str(e)
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith(('out_time_us=', 'out_time_ms=')):
                try:
                    val = int(line.split('=', 1)[1])
                except ValueError:
                    continue
                sec = int(val / (1e6 if 'us=' in line else 1e3))
                if total_dur and total_dur > 0:
                    bar.update(max(0, min(int(total_dur), sec) - last))
                    last = sec
                else:
                    bar.n = sec
                    bar.refresh()
    finally:
        proc.wait()
        if total_dur and total_dur > 0 and bar.n < int(total_dur):
            bar.update(int(total_dur) - bar.n)
        bar.close()
    err = proc.stderr.read() if proc.stderr else ''
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        tail = " | ".join(l for l in (err or '').splitlines()[-3:] if l.strip())
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, tail or "ffmpeg failed"
    os.replace(tmp, out_path)
    return True, None


MP4DECRYPT = None                 # resolved path to Bento4 mp4decrypt, once found


def _mp4decrypt_cache_dir():
    base = os.path.dirname(os.path.abspath(sys.argv[0] or '.')) or os.getcwd()
    return os.path.join(base, '.mp4decrypt')


def _try_mp4decrypt(path):
    """True if `path` is a runnable mp4decrypt (it prints usage/version when run with no args)."""
    if not path or not os.path.isfile(path):
        return False
    try:
        r = subprocess.run([path], capture_output=True, text=True, timeout=15)
        blob = ((r.stdout or '') + (r.stderr or '')).lower()
        return 'mp4decrypt' in blob or 'usage' in blob or 'bento4' in blob
    except (OSError, subprocess.SubprocessError):
        return False


def _find_mp4decrypt_in(dirs, max_depth=None):
    """Walk each directory (optionally depth-limited) for a working mp4decrypt. Returns path/None."""
    name = 'mp4decrypt.exe' if os.name == 'nt' else 'mp4decrypt'
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        base_depth = os.path.abspath(d).rstrip(os.sep).count(os.sep)
        for root, subdirs, files in os.walk(d):
            if max_depth is not None:
                depth = os.path.abspath(root).rstrip(os.sep).count(os.sep) - base_depth
                if depth >= max_depth:
                    subdirs[:] = []
                    continue
            if name in files:
                p = os.path.join(root, name)
                if os.name != 'nt':
                    try:
                        os.chmod(p, 0o755)
                    except OSError:
                        pass
                if _try_mp4decrypt(p):
                    return p
    return None


def _find_cached_mp4decrypt():
    return _find_mp4decrypt_in([_mp4decrypt_cache_dir()])


def _download_and_extract_mp4decrypt(url, verbose):
    cache = _mp4decrypt_cache_dir()
    os.makedirs(cache, exist_ok=True)
    print(f"[INFO] mp4decrypt not found; downloading from {url}")
    tmp = os.path.join(cache, 'mp4decrypt_download.tmp')
    with requests.get(url, stream=True, headers={'User-Agent': USER_AGENT}, allow_redirects=True,
                      timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0) or 0)
        bar = make_bar(total=total or 1, unit='B', unit_scale=True, desc='mp4decrypt',
                       disable=(total == 0))
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(256 * 1024):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
        bar.close()
    print("[INFO] Extracting mp4decrypt ...")
    _extract_archive(tmp, cache, url)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return _find_cached_mp4decrypt()


def ensure_mp4decrypt(verbose):
    """Locate Bento4's mp4decrypt (to decrypt CENC/Widevine with a key you hold), in the same order
    as ffmpeg: explicit MP4DECRYPT + PATH -> cached .mp4decrypt -> install folders
    (MP4DECRYPT_PROGRAM_FILES_DIRS) + script dir + cwd -> (Windows only) download from the NAS URL,
    then the internet fallback. On Linux/macOS uses PATH and prints an install hint. Returns path
    or None."""
    global MP4DECRYPT
    if MP4DECRYPT and _try_mp4decrypt(MP4DECRYPT):
        return MP4DECRYPT
    import shutil
    onpath = shutil.which('mp4decrypt') or shutil.which('mp4decrypt.exe')
    if onpath and _try_mp4decrypt(onpath):
        MP4DECRYPT = onpath
        return onpath
    cached = _find_cached_mp4decrypt()
    if cached:
        MP4DECRYPT = cached
        if verbose:
            print(f"[INFO] Using cached mp4decrypt: {cached}")
        return cached
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0] or '.')) or os.getcwd()
    if os.name == 'nt':
        broad = list(MP4DECRYPT_PROGRAM_FILES_DIRS) + [script_dir, os.getcwd()]
    else:
        broad = [script_dir, os.getcwd()]
    found = _find_mp4decrypt_in(broad, max_depth=4)
    if found:
        MP4DECRYPT = found
        print(f"[INFO] Found mp4decrypt: {found}")
        return found
    if os.name == 'nt':
        for url in (MP4DECRYPT_DOWNLOAD_URL, MP4DECRYPT_FALLBACK_URL):
            if not url:
                continue
            try:
                path = _download_and_extract_mp4decrypt(url, verbose)
            except Exception as e:
                print(f"[WARN] mp4decrypt download from {url} failed: {e}")
                continue
            if path and _try_mp4decrypt(path):
                MP4DECRYPT = path
                print(f"[INFO] Using downloaded mp4decrypt: {path}")
                return path
        print("[ERROR] Could not obtain mp4decrypt from the NAS or the internet fallback.")
        return None
    print("[ERROR] mp4decrypt (Bento4) not found. Install it and/or put it on PATH:")
    print("        download Bento4 from https://www.bento4.com/downloads/ and add its bin/ to PATH,")
    print("        or drop mp4decrypt next to the script.")
    return None


def _download_hls_raw(media_url, headers, session, out_file, verbose, label):
    """Download an HLS media playlist's init + media segments RAW (no decryption) and concat to
    out_file, preserving CENC boxes so mp4decrypt can decrypt afterwards. Returns (ok, reason)."""
    init, segs = parse_media_playlist(media_url, session, headers)
    if not segs:
        return False, "no segments in playlist"
    tmpdir = out_file + ".segs"
    os.makedirs(tmpdir, exist_ok=True)
    items = ([('init', init)] if init else []) + [(f"{i:06d}", u) for i, u in enumerate(segs)]

    def _dl(item):
        name, u = item
        path = os.path.join(tmpdir, name)
        ok, reason = _download_hls_segment(u, path, _seg_session(session), headers)
        return name, path, ok, reason

    from concurrent.futures import ThreadPoolExecutor
    results, bad = {}, None
    bar = make_bar(total=len(items), desc=label, unit='seg', leave=True)
    with ThreadPoolExecutor(max_workers=16) as ex:
        for name, path, ok, reason in ex.map(_dl, items):
            results[name] = path
            if not ok:
                bad = reason or "segment download failed"
            bar.update(1)
    bar.close()
    if bad:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
        return False, bad
    ordered = ([results['init']] if init else []) + [results[f"{i:06d}"] for i in range(len(segs))]
    _concat_stream(ordered, out_file)
    import shutil as _sh
    _sh.rmtree(tmpdir, ignore_errors=True)
    return True, None


def _mpd_duration_seconds(text):
    """Parse an ISO-8601 duration (e.g. PT1H2M3.5S) to seconds."""
    m = re.match(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?', text or '')
    if not m:
        return 0.0
    d, h, mi, s = m.groups()
    return (int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0))


def _parse_mpd(mpd_url, session, headers, max_height, verbose):
    """Parse a DASH MPD into {'video': rep, 'audios': [rep...], 'subs': [rep...]} where each rep is
    {'file': url} (single-file SegmentBase) OR {'init': url, 'segments': [urls]} (SegmentTemplate),
    plus 'lang'/'name'/'kid'/'height'. Handles SegmentTimeline and $Number$. Returns None on
    failure. Covers the common VOD shapes, not every exotic MPD."""
    import xml.etree.ElementTree as ET
    try:
        r = session.get(mpd_url, headers=headers, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError):
        return None
    ns = root.tag[1:root.tag.index('}')] if root.tag.startswith('{') else ''

    def Q(t):
        return f'{{{ns}}}{t}' if ns else t

    def base_of(el, parent_base):
        b = el.find(Q('BaseURL'))
        return urljoin(parent_base, b.text.strip()) if (b is not None and b.text) else parent_base

    total = _mpd_duration_seconds(root.get('mediaPresentationDuration'))
    root_base = base_of(root, mpd_url)
    period = root.find(Q('Period'))
    if period is None:
        return None
    period_base = base_of(period, root_base)
    video, audios, subs = None, [], []

    def seglist(tmpl, rep, rep_base):
        # Single-file (SegmentBase, or no template at all): the whole BaseURL is the fragmented mp4.
        if tmpl is None or rep.find(Q('SegmentBase')) is not None:
            return {'file': rep_base}
        rid, bw = rep.get('id', ''), rep.get('bandwidth', '')

        def sub(t, number=None, time=None):
            t = t.replace('$RepresentationID$', rid).replace('$Bandwidth$', bw)
            if number is not None:
                t = re.sub(r'\$Number(%0\d+d)?\$',
                           lambda m: (m.group(1) % number) if m.group(1) else str(number), t)
            if time is not None:
                t = t.replace('$Time$', str(time))
            return urljoin(rep_base, t.replace('$$', '$'))

        init = tmpl.get('initialization')
        media = tmpl.get('media')
        start = int(tmpl.get('startNumber') or 1)
        init_url = sub(init) if init else None
        segs = []
        timeline = tmpl.find(Q('SegmentTimeline'))
        if timeline is not None:
            t, num = 0, start
            for s in timeline.findall(Q('S')):
                if s.get('t') is not None:
                    t = int(s.get('t'))
                d = int(s.get('d'))
                for _ in range(int(s.get('r') or 0) + 1):
                    segs.append(sub(media, number=num, time=t))
                    t += d
                    num += 1
        elif media:
            dur = int(tmpl.get('duration') or 0)
            ts = int(tmpl.get('timescale') or 1)
            if dur and total:
                count = int(total / (dur / ts)) + 1
                for i in range(count):
                    segs.append(sub(media, number=start + i, time=i * dur))
        return {'init': init_url, 'segments': segs}

    video_cands = []            # (is_mp4, height, info_dict)
    for aset in period.findall(Q('AdaptationSet')):
        mime = aset.get('mimeType', '')
        ctype = aset.get('contentType') or mime.split('/')[0]
        lang = aset.get('lang', '')
        aset_base = base_of(aset, period_base)
        aset_tmpl = aset.find(Q('SegmentTemplate'))
        kid = None
        for cp in aset.findall(Q('ContentProtection')):
            k = (cp.get('{urn:mpeg:cenc:2013}default_KID') or cp.get('default_KID')
                 or cp.get('cenc:default_KID'))
            if k:
                kid = k.replace('-', '')
        reps = aset.findall(Q('Representation'))
        if not reps:
            continue

        def info(rep, _mime=mime, _lang=lang, _base=aset_base, _tmpl=aset_tmpl, _kid=kid, _aset=aset):
            rep_base = base_of(rep, _base)
            tmpl = rep.find(Q('SegmentTemplate')) or _tmpl
            d = seglist(tmpl, rep, rep_base)
            d.update({'lang': _lang, 'name': _aset.get('label') or _lang or rep.get('id', ''),
                      'kid': _kid or rep.get('kid'), 'height': int(rep.get('height') or 0),
                      'mime': rep.get('mimeType') or _mime,
                      'default': any(rl.get('value') == 'main'
                                     for rl in _aset.findall(Q('Role')))})
            return d

        rep_mime = (reps[0].get('mimeType') or mime).lower()
        if ctype == 'video' or mime.startswith('video') or 'video' in rep_mime:
            for rp in reps:
                video_cands.append(('mp4' in ((rp.get('mimeType') or mime).lower()),
                                    int(rp.get('height') or 0), info(rp)))
        elif ctype == 'audio' or mime.startswith('audio') or 'audio' in rep_mime:
            audios.append(info(max(reps, key=lambda rp: int(rp.get('bandwidth') or 0))))
        elif ctype == 'text' or mime.startswith('text') or 'ttml' in rep_mime or 'vtt' in rep_mime:
            subs.append(info(reps[0]))

    if not video_cands:
        return None
    cap = max_height or 100000
    # Prefer mp4 (mp4decrypt/ffmpeg-friendly) over webm, then the highest resolution up to the cap.
    within = [c for c in video_cands if c[1] <= cap] or video_cands
    mp4s = [c for c in within if c[0]]
    pool = mp4s or within
    video = max(pool, key=lambda c: c[1])[2]
    return {'video': video, 'audios': audios, 'subs': subs}


def _download_file_progress(url, out_path, session, headers, label, verbose):
    """Stream a single URL to out_path with a byte progress bar. Returns (ok, reason)."""
    tmp = out_path + '.part'
    try:
        with session.get(url, stream=True, headers=headers,
                         timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            total = int(r.headers.get('Content-Length') or 0)
            if total:
                bar = make_bar(total=total, desc=label, unit='B', unit_scale=True, leave=True)
            else:
                # No Content-Length (common for subtitle files) — show bytes + speed, not a
                # misleading 0% bar that never fills.
                bar = make_bar(desc=label, unit='B', unit_scale=True, leave=True,
                               bar_format='{desc} {n_fmt}  {rate_fmt}  {elapsed}')
            with open(tmp, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))
            bar.close()
    except requests.RequestException as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, str(e)
    os.replace(tmp, out_path)
    return True, None


def _download_dash_raw(rep, headers, session, out_file, verbose, label):
    """Download a DASH representation RAW (single file, or init + segments concatenated) so
    mp4decrypt can decrypt it. Returns (ok, reason)."""
    if rep.get('file'):
        return _download_file_progress(rep['file'], out_file, session, headers, label, verbose)
    segs = rep.get('segments') or []
    if not segs:
        return False, "no segments"
    tmpdir = out_file + ".segs"
    os.makedirs(tmpdir, exist_ok=True)
    items = ([('init', rep['init'])] if rep.get('init') else []) + \
            [(f"{i:06d}", u) for i, u in enumerate(segs)]

    def _dl(item):
        name, u = item
        path = os.path.join(tmpdir, name)
        ok, reason = _download_hls_segment(u, path, _seg_session(session), headers)
        return name, path, ok, reason

    from concurrent.futures import ThreadPoolExecutor
    results, bad = {}, None
    bar = make_bar(total=len(items), desc=label, unit='seg', leave=True)
    with ThreadPoolExecutor(max_workers=16) as ex:
        for name, path, ok, reason in ex.map(_dl, items):
            results[name] = path
            if not ok:
                bad = reason or "segment failed"
            bar.update(1)
    bar.close()
    if bad:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
        return False, bad
    ordered = ([results['init']] if rep.get('init') else []) + \
              [results[f"{i:06d}"] for i in range(len(segs))]
    _concat_stream(ordered, out_file)
    import shutil as _sh
    _sh.rmtree(tmpdir, ignore_errors=True)
    return True, None


def _download_sub_quiet(s, headers, session, out_file):
    """Download one subtitle track (DASH single-file/segments, or HLS media playlist) to out_file
    with no progress bar of its own — used under a single consolidated 'subtitles' bar."""
    try:
        if s.get('file'):
            ok, _ = _download_hls_segment(s['file'], out_file, _seg_session(session), headers)
            return ok
        uri = s.get('uri')
        segs, init = (s.get('segments'), s.get('init'))
        if uri and not segs:
            init, segs = parse_media_playlist(uri, session, headers)
        if segs:
            parts = []
            for j, u in enumerate(([init] if init else []) + list(segs)):
                if not u:
                    continue
                p = out_file + f".{j:04d}"
                ok, _ = _download_hls_segment(u, p, _seg_session(session), headers)
                if not ok:
                    return False
                parts.append(p)
            _concat_stream(parts, out_file)
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            return True
    except (requests.RequestException, OSError):
        return False
    return False


def _sub_key(s):
    """A stable key for grouping/naming a subtitle track across videos (language, else name)."""
    return (s.get('lang') or s.get('name') or '?').strip() or '?'


def _prompt_language_selection(langs):
    """langs = [(key, display)]. Interactive multi-select of subtitle languages (arrow-key
    checklist with search + scrollbar; numbered fallback). Returns a set of chosen keys."""
    labels = [f"{disp} [{key}]" if disp and disp != key else f"[{key}]" for key, disp in langs]
    idxs = tui_select_many(f"Pick subtitle language(s) — {len(langs)} available", labels,
                           header=[f"{CLR.CYAN}Subtitle languages across the collection"
                                   f"{CLR.RESET}"])
    if not idxs:
        return set()
    return {langs[i][0] for i in idxs}


def _vtt_ts_to_srt(ts):
    """'00:01:02.345' or '01:02.345' -> '00:01:02,345' (SubRip). Returns None if unparseable."""
    m = re.match(r'\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*$', ts)
    if not m:
        return None
    h, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    ms = (m.group(4) + '000')[:3]
    return f"{h:02d}:{mm:02d}:{ss:02d},{ms}"


def _vtt_to_srt(vtt_text):
    """Convert WebVTT text to SubRip (.srt) text in pure Python — no ffmpeg. Drops the WEBVTT header,
    NOTE/STYLE/REGION blocks and cue-position settings, renumbers cues, and fixes the timestamp
    separator ('.'->','). Keeps <i>/<b> (SRT understands them); strips <c>/<v>/karaoke tags."""
    text = vtt_text.replace('\r\r\n', '\n').replace('\r\n', '\n').replace('\r', '\n')
    out, n = [], 0
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.split('\n')
        ti = next((k for k, ln in enumerate(lines) if '-->' in ln), None)
        if ti is None:
            continue                                   # header / NOTE / STYLE / REGION
        m = re.search(r'([\d:.,]+)\s*-->\s*([\d:.,]+)', lines[ti])
        if not m:
            continue
        start, end = _vtt_ts_to_srt(m.group(1)), _vtt_ts_to_srt(m.group(2))
        if not start or not end:
            continue
        payload = '\n'.join(lines[ti + 1:]).strip()
        payload = re.sub(r'<(\d{2}:)?\d{2}:\d{2}[.,]\d{3}>', '', payload)   # inline karaoke stamps
        payload = re.sub(r'</?c[^>]*>|</?v[^>]*>|</?ruby>|</?rt>|</?lang[^>]*>', '', payload)
        payload = payload.strip()
        if not payload:
            continue
        n += 1
        out.append(f"{n}\n{start} --> {end}\n{payload}\n")
    return '\n'.join(out)


def _to_srt(path, verbose):
    """Convert a downloaded .vtt / .ttml subtitle to .srt. WebVTT is converted in pure Python (no
    ffmpeg needed — most robust); anything else falls back to ffmpeg. Returns the .srt path, or the
    original path if conversion isn't possible."""
    if path.lower().endswith('.srt') or not os.path.exists(path):
        return path
    srt = os.path.splitext(path)[0] + '.srt'
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        content = ''
    # WebVTT (Viki/HLS/DASH text tracks) -> pure-Python conversion.
    if content and ('WEBVTT' in content[:80].upper() or '-->' in content):
        body = _vtt_to_srt(content)
        if body.strip():
            try:
                with open(srt, 'w', encoding='utf-8') as f:
                    f.write(body)
                if os.path.abspath(srt) != os.path.abspath(path):
                    os.remove(path)
                return srt
            except OSError:
                return path
    # Non-VTT (e.g. TTML): let ffmpeg try.
    if not ensure_ffmpeg(verbose):
        return path
    tmp = _temp_artifact(srt, ".conv.srt")
    proc = subprocess.run([FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', path,
                           '-y', tmp], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, srt)
        if os.path.abspath(srt) != os.path.abspath(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return srt
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def _subs_of_source(src, session, max_height, verbose):
    """Return (headers, [sub tracks]) for one subtitle source. Sources: {'kind':'hls','stream':..},
    {'kind':'hls_url','url':..,'headers':..}, or {'kind':'mpd','url':..,'headers':..}."""
    try:
        if src['kind'] == 'mpd':
            hdrs = src.get('headers') or {}
            mpd = _parse_mpd(src['url'], session, hdrs, max_height, verbose)
            return hdrs, (mpd or {}).get('subs') or []
        if src['kind'] == 'hls_url':
            hdrs = src.get('headers') or {}
            tracks = parse_master_tracks(src['url'], session, max_height, hdrs, verbose)
            return hdrs, (tracks or {}).get('subs') or []
        # 'hls' — a Patreon/Vimeo/Mux stream descriptor
        master, hdrs = resolve_master(src['stream'], session, verbose)
        if not master:
            return {}, []
        tracks = parse_master_tracks(master, session, max_height, hdrs, verbose)
        return hdrs, (tracks or {}).get('subs') or []
    except (requests.RequestException, OSError, ValueError):
        return {}, []


# --- Viki (rakuten viki) subtitles -----------------------------------------------------------
# A logged-in subscriber can pull every timed-text (.vtt) track directly from Viki's API — no video
# download. The web app embeds a session token in the page (__NEXT_DATA__); each video's per-play
# stream_id + language list come from the Next.js data endpoint; subtitles are then a single GET.
_VIKI_APP = "100000a"


def _viki_url_kind(url):
    """('container', id) for a viki.com/tv/<id>-… series, ('video', id) for a viki.com/videos/<id>-…
    page, else None."""
    m = re.search(r'viki\.com/tv/([0-9a-z]+)', url or '', re.I)
    if m:
        return ('container', m.group(1))
    m = re.search(r'viki\.com/videos/(\d+v)', url or '', re.I)
    if m:
        return ('video', m.group(1))
    return None


def _viki_page_ctx(url, session, verbose):
    """Fetch a Viki page and read its __NEXT_DATA__ -> (buildId, user token, container id, title)."""
    try:
        r = session.get(url, headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        data = json.loads(m.group(1)) if m else {}
    except (requests.RequestException, ValueError, AttributeError):
        return None, None, None, None
    build = data.get('buildId')
    pp = data.get('props', {}).get('pageProps', {})
    token = (pp.get('userInfo') or {}).get('token')
    cj = pp.get('containerJson') or (pp.get('videoMetadataJson') or {}).get('container') or {}
    cid = cj.get('id')
    titles = cj.get('titles') or {}
    title = titles.get('en') or (list(titles.values())[0] if titles else None) or cj.get('id')
    return build, token, cid, title


def _viki_episodes(cid, session, verbose):
    """Every episode of a container via the paginated episodes API (ascending by number)."""
    eps, page = [], 1
    while page <= 30:
        u = (f"https://api.viki.io/v4/containers/{cid}/episodes.json?app={_VIKI_APP}"
             f"&per_page=50&sort=number&direction=asc&with_upcoming=false&page={page}")
        try:
            d = session.get(u, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)).json()
        except (requests.RequestException, ValueError):
            break
        for ep in d.get('response', []):
            web = ((ep.get('url') or {}).get('web') or '')
            slug = web.split('/videos/')[-1] if '/videos/' in web else ep.get('id', '')
            eps.append({'id': ep.get('id'), 'number': ep.get('number'), 'slug': slug,
                        'langs': list((ep.get('subtitle_completions') or {}).keys())})
        if not d.get('more'):
            break
        page += 1
    return eps


def _viki_video_data(slug, build, session, verbose):
    """Next.js data for one video -> (video_id, stream_id, [subtitle langs], token)."""
    u = f"https://www.viki.com/_next/data/{build}/videos/{slug}.json?vid={slug}"
    try:
        pp = session.get(u, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)).json().get('pageProps', {})
    except (requests.RequestException, ValueError):
        return None, None, [], None
    stats = (pp.get('videoPlaybackStreamJson') or {}).get('stats') or []
    sid = stats[0].get('stream_id') if stats else None
    meta = pp.get('videoMetadataJson') or {}
    langs = list((meta.get('subtitle_completions') or {}).keys())
    return meta.get('id'), sid, langs, (pp.get('userInfo') or {}).get('token')


def _viki_sub_url(vid, lang, sid, token):
    u = (f"https://api.viki.io/v4/videos/{vid}/auth_subtitles/{lang}.vtt"
         f"?app={_VIKI_APP}&token={token}")
    return u + (f"&stream_id={sid}" if sid else "")


def _download_viki_subs(url, session, out_dir, verbose):
    """--subs-only for Viki: list the series' episodes, gather available subtitle languages, let the
    user pick, then download those .vtt tracks straight from Viki's API and convert to .srt. No video
    is downloaded. Needs your logged-in Viki session (export viki.com cookies into the folder).

    Kept deliberately light on the server: 1 page fetch + the episodes list + one probe, then only
    the subtitle files themselves. A per-video stream_id (1 extra request/episode) is fetched ONLY
    if Viki rejects the token-only request."""
    build, token, cid, title = _viki_page_ctx(url, session, verbose)
    kind = _viki_url_kind(url)
    if kind and kind[0] == 'container':
        cid = kind[1]
    if not build or not token:
        print(f"{CLR.YELLOW}[ERROR]{CLR.RESET} Viki: couldn't read your session from the page. "
              "Log in on viki.com and export your viki.com cookies (JSON) into this folder, so the "
              "script can use your subscription.")
        return
    if not cid:
        print(f"{CLR.YELLOW}[ERROR]{CLR.RESET} Viki: couldn't find the series id in that URL.")
        return
    episodes = _viki_episodes(cid, session, verbose)
    if not episodes:
        print("[INFO] Viki: no episodes found for this series.")
        return
    total = len(episodes)
    cov = {}
    for ep in episodes:
        for lang in ep['langs']:
            cov[lang] = cov.get(lang, 0) + 1
    if not cov:
        print("[INFO] Viki: no subtitle languages available.")
        return
    print(f"[INFO] Viki: '{title}' — {total} episode(s), {len(cov)} subtitle language(s) available.")
    # Show each language WITH its episode coverage, fullest first, so you can see at a glance which
    # languages cover the whole series and which are partial (e.g. specials only in English).
    def _disp(lang):
        return f"{_VIKI_LANG_NAMES.get(lang, lang)} · {cov.get(lang, 0)}/{total}"
    order = sorted(cov.keys(), key=lambda l: (-cov[l], _VIKI_LANG_NAMES.get(l, l).lower()))
    chosen = _prompt_language_selection([(l, _disp(l)) for l in order])
    if not chosen:
        print("[INFO] Viki: nothing selected.")
        return
    series = safe_filename(title or 'viki', 'viki')
    sid_cache = {}

    def _sid(ep):
        if ep['id'] not in sid_cache:
            _v, s, _l, _t = _viki_video_data(ep['slug'], build, session, verbose)
            sid_cache[ep['id']] = s
        return sid_cache[ep['id']]

    def _fetch(vid, lang, sid, out):
        try:
            r = session.get(_viki_sub_url(vid, lang, sid, token),
                            timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT))
            if r.status_code == 200 and r.text.lstrip().upper().startswith('WEBVTT'):
                body = _vtt_to_srt(r.text)      # convert in memory (no file round-trip)
                if body.strip():
                    with open(out, 'w', encoding='utf-8') as f:   # out is the .srt path
                        f.write(body)
                    return True
                vtt = out[:-4] + '.vtt'         # rare: couldn't parse -> keep .vtt, try ffmpeg
                with open(vtt, 'w', encoding='utf-8', newline='') as f:
                    f.write(r.text)
                _to_srt(vtt, verbose)
                return True
        except (requests.RequestException, OSError):
            pass
        return False

    # Per-language coverage for the languages you chose (so a lower count than the total is clearly
    # explained rather than a mystery).
    for lang in sorted(chosen, key=lambda l: (-cov.get(l, 0), l)):
        miss = [str(ep['number']) for ep in episodes if lang not in ep['langs']]
        note = f" — missing for E{', E'.join(miss)}" if miss else ""
        print(f"[INFO] Viki: {_VIKI_LANG_NAMES.get(lang, lang)} [{lang}] in "
              f"{total - len(miss)}/{total} episode(s){note}.")
    # Episodes that have NONE of your chosen languages — tell the user what they DO have.
    orphan = [ep for ep in episodes if not any(lang in ep['langs'] for lang in chosen)]
    if orphan:
        have = sorted({lang for ep in orphan for lang in ep['langs']})
        print(f"{CLR.YELLOW}[WARN]{CLR.RESET} Viki: {len(orphan)} episode(s) have none of your "
              f"chosen languages: E{', E'.join(str(ep['number']) for ep in orphan)}. "
              f"Those episodes only have: {', '.join(have) or '—'}. "
              "Re-run and add one of those to grab them too.")

    want = []
    converted = 0
    for ep in episodes:
        try:
            base = f"{series} - E{int(ep['number']):02d}"
        except (TypeError, ValueError):
            base = f"{series} - {ep['id']}"
        for lang in ep['langs']:
            if lang in chosen:
                srt = os.path.join(out_dir or '.', f"{base}.{lang}.srt")
                vtt = srt[:-4] + '.vtt'
                if os.path.exists(srt):
                    continue                          # already have the .srt
                if os.path.exists(vtt):               # leftover .vtt -> convert, no re-download
                    if _to_srt(vtt, verbose).lower().endswith('.srt'):
                        converted += 1
                    continue
                want.append((ep, lang, srt))
    if converted:
        print(f"[INFO] Viki: converted {converted} existing .vtt file(s) to .srt (no re-download).")
    if not want:
        print("[INFO] Viki: selected subtitles already present." if not converted
              else "[INFO] Viki: done — all selected subtitles are now .srt.")
        return

    # One bar per subtitle (per episode+language), sequentially — gentlest on a rate-limited server.
    need_sid, got = False, 0
    for idx, (ep, lang, out) in enumerate(want):
        bar = make_bar(total=1, desc=os.path.basename(out), unit='sub', leave=True)
        ok = _fetch(ep['id'], lang, _sid(ep) if need_sid else None, out)
        if not ok and idx == 0:                     # first failed token-only -> stream_id needed?
            ok = _fetch(ep['id'], lang, _sid(ep), out)
            if ok:
                need_sid = True
        bar.update(1)
        try:
            bar.colour = 'green' if ok else 'red'
        except Exception:
            pass
        bar.close()
        got += 1 if ok else 0
    extra = " (token-only)" if not need_sid else ""
    print(f"[INFO] Viki: downloaded {got}/{len(want)} subtitle file(s) in {len(chosen)} "
          f"language(s){extra}.")


_VIKI_LANG_NAMES = {
    'en': 'English', 'ko': 'Korean', 'ja': 'Japanese', 'zh': 'Chinese (Simplified)',
    'zt': 'Chinese (Traditional)', 'cs': 'Czech', 'sk': 'Slovak', 'de': 'German', 'fr': 'French',
    'es': 'Spanish', 'pt': 'Portuguese', 'it': 'Italian', 'ru': 'Russian', 'pl': 'Polish',
    'nl': 'Dutch', 'tr': 'Turkish', 'ar': 'Arabic', 'vi': 'Vietnamese', 'th': 'Thai', 'id': 'Indonesian',
    'ms': 'Malay', 'hi': 'Hindi', 'ro': 'Romanian', 'hu': 'Hungarian', 'el': 'Greek', 'sv': 'Swedish',
    'fi': 'Finnish', 'da': 'Danish', 'he': 'Hebrew', 'uk': 'Ukrainian', 'bg': 'Bulgarian',
    'hr': 'Croatian', 'sr': 'Serbian', 'tl': 'Tagalog',
}


def _download_subs_only(sources, session, out_dir, max_height, verbose):
    """--subs-only: for each source (native HLS stream, or a scanned HLS master / DASH .mpd),
    discover subtitle tracks WITHOUT downloading any video, let the user multi-select language(s),
    then download just those subtitles and convert them to .srt (WebVTT/TTML -> SubRip). Falls back
    to .vtt if ffmpeg isn't available. No-op (with a note) when nothing has subtitles."""
    if not sources:
        print("[INFO] --subs-only: no HLS/DASH videos here to pull subtitles from "
              "(Drive/Dropbox/Streamable files don't carry separate subtitle tracks).")
        return
    per_video, lang_map = [], {}
    bar = make_bar(total=len(sources), desc='scanning subs', unit='vid', leave=True)
    for src in sources:
        headers, subs = _subs_of_source(src, session, max_height, verbose)
        per_video.append((src.get('title') or 'video', headers, subs))
        for s in subs:
            k = _sub_key(s)
            lang_map.setdefault(k, s.get('name') or s.get('lang') or k)
        bar.update(1)
    bar.close()
    if not lang_map:
        print("[INFO] --subs-only: no subtitle tracks found in these videos.")
        return
    langs = sorted(lang_map.items(), key=lambda kv: kv[0].lower())
    chosen = _prompt_language_selection(langs)
    if not chosen:
        print("[INFO] --subs-only: nothing selected; no subtitles downloaded.")
        return
    tasks = []                          # (srt_target, sub, headers)
    converted = 0
    for title, headers, subs in per_video:
        base = re.sub(r'\.(mp4|mkv|m4v|webm)$', '', safe_filename(title, 'video'), flags=re.I)
        for s in subs:
            if _sub_key(s) in chosen:
                code = safe_filename(_sub_key(s), 'sub')
                srt = os.path.join(out_dir or '.', f"{base}.{code}.srt")
                vtt = srt[:-4] + '.vtt'
                if os.path.exists(srt):
                    continue
                if os.path.exists(vtt):             # leftover .vtt -> convert, no re-download
                    if _to_srt(vtt, verbose).lower().endswith('.srt'):
                        converted += 1
                    continue
                tasks.append((srt, s, headers))
    if converted:
        print(f"[INFO] --subs-only: converted {converted} existing .vtt file(s) to .srt.")
    if not tasks:
        print("[INFO] --subs-only: selected subtitles are already present.")
        return
    ensure_ffmpeg(verbose)              # resolve ffmpeg once, before the bar (clean output)
    got = [0]
    lock = threading.Lock()
    bar = make_bar(total=len(tasks), desc='subtitles', unit='sub', leave=True)

    def _one(t):
        srt, s, hdrs = t
        vtt = srt[:-4] + '.vtt'
        ok = _download_sub_quiet(s, hdrs, session, vtt)
        if ok:
            _to_srt(vtt, verbose)       # -> .srt (or keep .vtt if ffmpeg missing)
        with lock:
            if ok:
                got[0] += 1
            bar.update(1)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, SCAN_PAGE_WORKERS)) as ex:
        list(ex.map(_one, tasks))
    bar.close()
    print(f"[INFO] --subs-only: downloaded {got[0]}/{len(tasks)} subtitle file(s) "
          f"in {len(chosen)} language(s) across {len(per_video)} video(s).")


def _grab_subs(subs, out_path, headers, session, verbose, suffix='sub'):
    """Download ALL subtitle tracks in parallel under one bar. Returns [(file, lang, name)]."""
    if not subs:
        return []
    results = [None] * len(subs)
    bar = make_bar(total=len(subs), desc='subtitles', unit='sub', leave=True)
    lock = threading.Lock()

    def _one(idx_s):
        idx, s = idx_s
        sf = _cenc_tmp(out_path, f"s{idx}.{suffix}")
        ok = _download_sub_quiet(s, headers, session, sf)
        with lock:
            bar.update(1)
        if ok:
            results[idx] = (sf, s.get('lang'), s.get('name'))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_one, enumerate(subs)))
    bar.close()
    return [r for r in results if r]


def _cenc_tmp(out_path, name):
    """An ASCII-only temp path under .temp. mp4decrypt (Bento4) on Windows uses ANSI argv and
    mangles non-ASCII paths (é, Korean, …) to '?', so it can't open files named after a Unicode
    title. Intermediate enc/dec files therefore get a plain ASCII name; the final output keeps its
    real (Unicode) name because ffmpeg handles those fine."""
    import hashlib
    h = hashlib.md5(os.path.abspath(out_path).encode('utf-8', 'ignore')).hexdigest()[:12]
    tdir = os.path.join(os.path.dirname(os.path.abspath(out_path)), TEMP_SUBDIR)
    os.makedirs(tdir, exist_ok=True)
    return os.path.join(tdir, f"cenc_{h}_{name}")


def _grab_dash_plain(mpd_url, headers, session, max_height, out_path, verbose):
    """Download an UNENCRYPTED DASH stream natively: parse the MPD, pull the chosen video +
    audio representations (parallel, with progress bars), grab subtitles, then mux. Honours -q,
    --audio/--sub and --container. Returns (ok, reason)."""
    mpd = _parse_mpd(mpd_url, session, headers, max_height, verbose)
    if not mpd:
        return False, "could not parse the MPD (unsupported DASH layout — send me the .mpd)"
    if AUDIO_SEL == 'SCAN' or SUB_SEL == 'SCAN':
        _hls_list_tracks(os.path.splitext(os.path.basename(out_path))[0],
                         mpd['audios'], mpd['subs'])
        return True, None
    audios = _select_tracks(mpd['audios'], AUDIO_SEL)
    subs = _select_tracks(mpd['subs'], SUB_SEL)
    out_path = os.path.splitext(out_path)[0] + _container_ext(len(audios) > 1 or bool(subs))
    made = []
    vfile = _temp_artifact(out_path, ".v.mp4")
    ok, why = _download_dash_raw(mpd['video'], headers, session, vfile, verbose,
                                 f"video {mpd['video'].get('height') or '?'}p")
    if not ok:
        return False, f"video: {why}"
    made.append(vfile)
    afiles, ameta = [], []
    for i, a in enumerate(audios):
        af = _temp_artifact(out_path, f".a{i}.mp4")
        tag = a.get('lang') or a.get('name') or '?'
        ok, why = _download_dash_raw(a, headers, session, af, verbose, f"audio {i} [{tag}]")
        if not ok:
            for f in made:
                try:
                    os.remove(f)
                except OSError:
                    pass
            return False, f"audio {i}: {why}"
        afiles.append(af)
        ameta.append((a.get('lang'), a.get('name')))
        made.append(af)
    sfiles, smeta = [], []
    for sf, lang, name in _grab_subs(subs, out_path, headers, session, verbose, suffix='sub'):
        sfiles.append(sf)
        smeta.append((lang, name))
        made.append(sf)
    ok, reason = _ffmpeg_mux_multi(vfile, afiles, sfiles, ameta, smeta, out_path, verbose)
    for f in made:
        try:
            os.remove(f)
        except OSError:
            pass
    return ok, reason


# --- muse.ai / skiv.com --------------------------------------------------------------------- #
# Creators embed muse.ai links in Patreon posts, often password-protected with the password written
# in the post text. The page (after the password POST) carries a cacheVideos entry with the file id;
# the actual media is a plain unencrypted DASH manifest on the skiv CDN.
_MUSE_RE = re.compile(r'(?:muse\.ai|skiv\.com)/(?:v|embed)/([0-9A-Za-z]+)', re.I)
_MUSE_PW_RE = re.compile(
    r'(?:password|heslo|pass|pwd)\s*(?:is|:|=)\s*["\u201c]?([^\s"\u201d<\\]{2,40})', re.I)


def _muse_id(url):
    m = _MUSE_RE.search(url or '')
    return m.group(1) if m else None


def _muse_password_from_text(text):
    """Pull the video password out of a post's text ('Password: FirstKiss?'). Returns None if the
    post doesn't mention one (many muse.ai videos are not protected)."""
    m = _MUSE_PW_RE.search(text or '')
    if not m:
        return None
    return m.group(1).strip().rstrip('.,;')


def resolve_muse(svid, session, password=None, verbose=False):
    """Resolve a muse.ai video id to (dash_mpd_url, title, headers). Submits `password` when the
    video is protected. Returns (None, None, None) when it can't be unlocked."""
    page_url = f"https://muse.ai/v/{svid}"
    hdrs = {'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'}
    try:
        r = session.get(page_url, headers=hdrs, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        html = r.text or ''
        locked = r.status_code == 403 or 'name="password"' in html or 'password' in (r.reason or '').lower()
        if locked:
            if not password:
                tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} muse.ai {svid}: password protected and "
                           "no password found in the post text.")
                return None, None, None
            r = session.post(page_url, data={'password': password}, headers=hdrs,
                             timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT), allow_redirects=True)
            html = r.text or ''
            if r.status_code != 200 or f'cacheVideos.set("{svid}"' not in html:
                tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} muse.ai {svid}: the password from the "
                           f"post ('{password}') was not accepted.")
                return None, None, None
    except requests.RequestException as e:
        if verbose:
            tqdm.write(f"[WARN] muse.ai {svid}: {e}")
        return None, None, None
    m = re.search(r'cacheVideos\.set\(\s*"' + re.escape(svid) + r'"\s*,\s*(\{.*?\})\s*\)\s*;',
                  html, re.S)
    if not m:
        m = re.search(r'\{[^{}]*"svid":\s*"' + re.escape(svid) + r'"[^{}]*\}', html)
    if not m:
        if verbose:
            tqdm.write(f"[WARN] muse.ai {svid}: no video data on the page.")
        return None, None, None
    try:
        info = json.loads(m.group(1) if m.lastindex else m.group(0))
    except ValueError:
        return None, None, None
    base = (info.get('url') or '').split('?')[0].rstrip('/')
    if base.endswith('/data'):
        base = base[:-len('/data')]
    if not base:
        return None, None, None
    title = info.get('title') or info.get('filename') or svid
    return f"{base}/videos/dash.mpd", title, {'User-Agent': USER_AGENT, 'Referer': 'https://muse.ai/'}


def download_muse_video(svid, session, out_dir, max_height, verbose, password=None, title=None):
    """Download one muse.ai video (unencrypted DASH) into out_dir. Returns True on success."""
    mpd_url, real_title, hdrs = resolve_muse(svid, session, password, verbose)
    if not mpd_url:
        return False
    if password:
        tqdm.write(f"[INFO] muse.ai: {real_title or svid} {CLR.DIM}[{svid}]{CLR.RESET}  "
                   f"password: {CLR.GREEN}{password}{CLR.RESET}")
    name = safe_filename(title or real_title or svid, 'video')
    out = os.path.join(out_dir or '.', name + _container_ext(False))
    if os.path.exists(out):
        tqdm.write(f"[INFO] muse.ai: {os.path.basename(out)} already exists — skipping.")
        return True
    ok, why = _grab_dash_plain(mpd_url, hdrs, session, max_height, out, verbose)
    if ok:
        _record_download(out)
    else:
        tqdm.write(f"{CLR.RED}[FAIL]{CLR.RESET} muse.ai {svid}: {why}")
    return ok


# --- Vidyard --------------------------------------------------------------------------------- #
# Vidyard's player config exposes ready-made progressive MP4 renditions (1080p/720p/480p/360p), so
# these download through the same shared pool as Drive/Dropbox/Streamable — no ffmpeg mux needed.
_VIDYARD_RE = re.compile(
    r'vidyard\.com/(?:watch|share|embed)/([0-9A-Za-z_-]{8,})|play\.vidyard\.com/([0-9A-Za-z_-]{8,})',
    re.I)


def _vidyard_id(url):
    m = _VIDYARD_RE.search(url or '')
    if not m:
        return None
    vid = m.group(1) or m.group(2) or ''
    return re.sub(r'\.(html?|jpg|jpeg|png|json)$', '', vid, flags=re.I) or None


def _vidyard_fname(base, uuid):
    return _ensure_video_ext(safe_filename(base or uuid, uuid))


def _profile_height(p):
    m = re.search(r'(\d{3,4})', str(p or ''))
    return int(m.group(1)) if m else 0


def resolve_vidyard(uuid, session, max_height=0, verbose=False):
    """Resolve a Vidyard video to (direct_mp4_url, title, headers, height). Picks the best MP4 that
    fits -q (max_height); falls back to the HLS rendition if no MP4 is offered."""
    api = f"https://play.vidyard.com/player/{uuid}.json"
    hdrs = {'User-Agent': USER_AGENT, 'Referer': 'https://play.vidyard.com/',
            'Accept': 'application/json'}
    try:
        r = session.get(api, headers=hdrs, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        if r.status_code != 200:
            if verbose:
                tqdm.write(f"[WARN] Vidyard {uuid}: HTTP {r.status_code}")
            return None, None, None, 0
        payload = (r.json() or {}).get('payload') or {}
    except (requests.RequestException, ValueError) as e:
        if verbose:
            tqdm.write(f"[WARN] Vidyard {uuid}: {type(e).__name__}")
        return None, None, None, 0
    chapters = payload.get('chapters') or []
    if not chapters:
        return None, None, None, 0
    ch = chapters[0]
    title = ch.get('name') or payload.get('name') or uuid
    sources = ch.get('sources') or {}
    mp4s = [s for s in (sources.get('mp4') or []) if s.get('url')]
    if mp4s:
        ranked = sorted(mp4s, key=lambda s: _profile_height(s.get('profile')), reverse=True)
        pick = next((s for s in ranked
                     if not max_height or _profile_height(s.get('profile')) <= max_height), ranked[-1])
        return pick['url'], title, hdrs, _profile_height(pick.get('profile'))
    hls = [s for s in (sources.get('hls') or []) if s.get('url')]
    if hls:                                     # no progressive rendition -> hand back the master
        ranked = sorted(hls, key=lambda s: _profile_height(s.get('profile')), reverse=True)
        pick = next((s for s in ranked
                     if not max_height or _profile_height(s.get('profile')) <= max_height), ranked[-1])
        return pick['url'], title, hdrs, _profile_height(pick.get('profile'))
    return None, None, None, 0


def download_vidyard_pooled(items, session, chunk_size, max_connections, max_height, verbose):
    """Download Vidyard videos through the shared connection pool (same engine as Drive/Dropbox/
    Streamable: parallel files, segment work-stealing, resume, per-file bars). `items` are
    {'uuid', 'title'} dicts. HLS-only videos fall back to the native HLS path."""
    entries, hls_jobs = [], []
    used_names = set()
    bar = make_bar(total=len(items), desc='resolving vidyard', unit='vid', leave=True)
    lock = threading.Lock()

    def _prep(it):
        url, title, hdrs, height = resolve_vidyard(it['uuid'], session, max_height, verbose)
        with lock:
            if not url:
                tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Vidyard {it['uuid']}: could not resolve; "
                           "skipping.")
            elif '.m3u8' in url:
                hls_jobs.append({'title': it.get('title') or title, 'master_url': url,
                                 'source': 'vidyard', 'headers': hdrs})
            else:
                fname = _unique_name(_vidyard_fname(it.get('title') or title, it['uuid']),
                                     used_names)
                entries.append({'id': url, 'title': fname, 'name': fname, 'direct_url': url,
                                'headers': hdrs, 'vidyard_uuid': it['uuid'],
                                'vidyard_height': height})
            bar.update(1)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, SCAN_PAGE_WORKERS)) as ex:
        list(ex.map(_prep, items))
    bar.close()
    if entries:
        qual = sorted({e['vidyard_height'] for e in entries if e['vidyard_height']}, reverse=True)
        _log_source("Vidyard", f"direct MP4 via shared pool — {len(entries)} file(s)"
                               + (f", up to {qual[0]}p" if qual else ""))
        download_folder_pooled(entries, session, chunk_size, verbose, label="Vidyard",
                               conn_cap=max_connections)
    if hls_jobs:
        if not ensure_ffmpeg(verbose):
            print("[ERROR] Vidyard: these videos are HLS-only and need ffmpeg. Skipping.")
        else:
            _log_source("Vidyard", f"HLS + ffmpeg mux — {len(hls_jobs)} file(s)")
            download_hls_pooled(hls_jobs, session, None,
                                max_connections or DEFAULT_MAX_CONNECTIONS, max_height, verbose)


def download_muse_pooled(items, session, chunk_size, max_connections, max_height, verbose):
    """Download several muse.ai videos through the SAME shared connection pool the other direct
    sources use: every video's DASH renditions are plain MP4 files, so all of them (video AND audio
    of every episode) are pulled at once with segment work-stealing, then each pair is muxed.
    Falls back to the sequential path for any video whose DASH layout isn't plain files."""
    # Show which password came out of which post — plain text, so you can reuse it in a browser.
    with_pw = [mu for mu in items if mu.get('password')]
    if with_pw:
        w = min(60, max(len(str(mu.get('title') or mu['svid'])) for mu in with_pw))
        print("[INFO] muse.ai: password(s) found in the post text:")
        for mu in with_pw:
            title = str(mu.get('title') or mu['svid'])
            title = title if len(title) <= w else title[:w - 1] + '\u2026'
            print(f"        {title:<{w}}  {CLR.DIM}[{mu['svid']}]{CLR.RESET}  "
                  f"password: {CLR.GREEN}{mu['password']}{CLR.RESET}")
    if verbose:
        n_open = sum(1 for mu in items if not mu.get('password'))
        if n_open:
            print(f"[INFO] muse.ai: {n_open} video(s) had no password in the post "
                  "(fine if they aren't protected).")
    resolved, fallback = [], []
    bar = make_bar(total=len(items), desc='resolving muse.ai', unit='vid', leave=True)
    lock = threading.Lock()

    def _prep(mu):
        mpd_url, real_title, hdrs = resolve_muse(mu['svid'], session, mu.get('password'), verbose)
        out = None
        if mpd_url:
            tracks = _parse_mpd(mpd_url, session, hdrs, max_height, verbose)
            if tracks and tracks.get('video'):
                audios = _select_tracks(tracks['audios'], AUDIO_SEL)
                subs = _select_tracks(tracks['subs'], SUB_SEL)
                name = safe_filename(mu.get('title') or real_title or mu['svid'], 'video')
                out = {'svid': mu['svid'], 'title': name, 'headers': hdrs, 'subs': subs,
                       'video': tracks['video'], 'audios': audios, 'mpd': mpd_url,
                       'plain': bool(tracks['video'].get('file')
                                     and all(a.get('file') for a in audios))}
        with lock:
            (resolved if out and out['plain'] else
             fallback if out else resolved).append(out or {'svid': mu['svid'], 'failed': True,
                                                           'title': mu.get('title')})
            bar.update(1)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, SCAN_PAGE_WORKERS)) as ex:
        list(ex.map(_prep, items))
    bar.close()
    jobs = [j for j in resolved if j and not j.get('failed')]
    for j in [j for j in resolved if j and j.get('failed')]:
        tqdm.write(f"{CLR.RED}[FAIL]{CLR.RESET} muse.ai {j['svid']}: could not resolve "
                   f"({j.get('title') or ''}).")
    # Every rendition is one plain MP4 -> feed them all into the shared pool at once.
    entries, plan, used_finals = [], [], set()
    for j in jobs:
        ext = _container_ext(len(j['audios']) > 1 or bool(j['subs']))
        final = _unique_name(j['title'] + ext, used_finals)
        if os.path.exists(final):
            tqdm.write(f"[INFO] muse.ai: {final} already exists — skipping.")
            continue
        stem = safe_filename(j['svid'] + '_' + j['title'], j['svid'])[:60]
        vname = f".muse.{stem}.v.mp4"
        vtmp = os.path.join(TEMP_SUBDIR, vname)
        entries.append({'id': j['video']['file'], 'direct_url': j['video']['file'],
                        'title': vname, 'name': vname, 'headers': j['headers']})
        atmps = []
        for i, a in enumerate(j['audios']):
            aname = f".muse.{stem}.a{i}.mp4"
            atmps.append((os.path.join(TEMP_SUBDIR, aname), a))
            entries.append({'id': a['file'], 'direct_url': a['file'],
                            'title': aname, 'name': aname, 'headers': j['headers']})
        plan.append({'job': j, 'final': final, 'vtmp': vtmp, 'atmps': atmps})
    if entries:
        _log_source("muse.ai", f"DASH via shared pool — {len(plan)} video(s), "
                               f"{len(entries)} stream(s)")
        download_folder_pooled(entries, session, chunk_size, verbose, label="muse.ai",
                               conn_cap=max_connections, record=False, dest_subdir=TEMP_SUBDIR)
        for p in plan:                       # mux each finished pair
            j = p['job']
            vtmp = p['vtmp']
            if not os.path.exists(vtmp):
                tqdm.write(f"{CLR.RED}[FAIL]{CLR.RESET} muse.ai {j['title']}: video part missing.")
                continue
            afiles, ameta, made = [], [], [vtmp]
            for atmp, a in p['atmps']:
                if os.path.exists(atmp):
                    afiles.append(atmp)
                    ameta.append((a.get('lang'), a.get('name')))
                    made.append(atmp)
            sfiles, smeta = [], []
            for sf, lang, name in _grab_subs(j['subs'], p['final'], j['headers'], session,
                                             verbose, suffix='sub'):
                sfiles.append(sf)
                smeta.append((lang, name))
                made.append(sf)
            ok, why = _ffmpeg_mux_multi(vtmp, afiles, sfiles, ameta, smeta, p['final'], verbose)
            for f in made:
                try:
                    os.remove(f)
                except OSError:
                    pass
            if ok:
                _record_download(p['final'])
            else:
                tqdm.write(f"{CLR.RED}[FAIL]{CLR.RESET} muse.ai {j['title']}: mux failed ({why}).")
    for j in fallback:                       # segmented DASH (rare) -> one at a time
        if not j:
            continue
        tqdm.write(f"[INFO] muse.ai {j['title']}: segmented DASH — using the sequential path.")
        out = os.path.join('.', j['title'] + _container_ext(False))
        ok, why = _grab_dash_plain(j['mpd'], j['headers'], session, max_height, out, verbose)
        if ok:
            _record_download(out)
        else:
            tqdm.write(f"{CLR.RED}[FAIL]{CLR.RESET} muse.ai {j['title']}: {why}")


def _cenc_grab_dash(mpd_url, headers, session, max_height, out_path, key_specs, verbose):
    """Decrypt a DASH stream you hold keys for: parse the MPD, download each representation RAW,
    mp4decrypt with your key, then mux (languages + names). Returns (ok, reason)."""
    mp4d = ensure_mp4decrypt(verbose)
    if not mp4d:
        return False, "mp4decrypt (Bento4) not found — install it to decrypt with --key"
    mpd = _parse_mpd(mpd_url, session, headers, max_height, verbose)
    if not mpd:
        return False, "could not parse the MPD (unsupported DASH layout — send me the .mpd)"
    # Bare --audio / --sub: list what the MPD offers and stop (mirrors HLS behaviour).
    if AUDIO_SEL == 'SCAN' or SUB_SEL == 'SCAN':
        _hls_list_tracks(os.path.splitext(os.path.basename(out_path))[0],
                         mpd['audios'], mpd['subs'])
        return True, None
    if verbose:
        v = mpd['video']
        tqdm.write(f"[INFO] DASH: video {v.get('height') or '?'}p ({v.get('mime', '?')}), "
                   f"{len(mpd['audios'])} audio / {len(mpd['subs'])} subtitle track(s) available")
    audios = _select_tracks(mpd['audios'], AUDIO_SEL)
    subs = _select_tracks(mpd['subs'], SUB_SEL)
    out_path = os.path.splitext(out_path)[0] + _container_ext(len(audios) > 1 or bool(subs))
    made = []

    def _decrypt(rep, tag, label):
        enc = _cenc_tmp(out_path, f"{tag}.enc")
        dec = _cenc_tmp(out_path, f"{tag}.dec.mp4")
        ok, why = _download_dash_raw(rep, headers, session, enc, verbose, label)
        if not ok:
            return None, why
        dcmd = [mp4d]
        for spec in key_specs:
            dcmd += ['--key', spec if ':' in spec else f"1:{spec}"]
        dcmd += [enc, dec]
        if verbose:
            tqdm.write(f"[INFO] mp4decrypt: decrypting {tag} with your key(s)...")
        proc = subprocess.run(dcmd, capture_output=True, text=True)
        try:
            os.remove(enc)
        except OSError:
            pass
        if proc.returncode != 0 or not os.path.exists(dec) or os.path.getsize(dec) == 0:
            return None, (proc.stderr or '').strip()[:200] or "mp4decrypt failed"
        return dec, None

    vlabel = f"video {mpd['video'].get('height') or '?'}p (enc)"
    vfile, why = _decrypt(mpd['video'], 'v', vlabel)
    if not vfile:
        return False, f"video: {why}"
    made.append(vfile)
    afiles, ameta = [], []
    for i, a in enumerate(audios):
        tag = a.get('lang') or a.get('name') or '?'
        af, why = _decrypt(a, f"a{i}", f"audio {i} [{tag}] (enc)")
        if not af:
            for f in made:
                try:
                    os.remove(f)
                except OSError:
                    pass
            return False, f"audio {i}: {why}"
        afiles.append(af)
        ameta.append((a.get('lang'), a.get('name')))
        made.append(af)
    # Subtitles: DASH text tracks are normally unencrypted (WebVTT/TTML) — fetch all in parallel.
    sfiles, smeta = [], []
    for sf, lang, name in _grab_subs(subs, out_path, headers, session, verbose, suffix='sub'):
        sfiles.append(sf)
        smeta.append((lang, name))
        made.append(sf)
    ok, reason = _ffmpeg_mux_multi(vfile, afiles, sfiles, ameta, smeta, out_path, verbose)
    for f in made:
        try:
            os.remove(f)
        except OSError:
            pass
    return ok, reason


def _cenc_grab_hls(master_url, headers, session, max_height, out_path, key_specs, verbose):
    """Decrypt an HLS stream you hold keys for: download each track's segments RAW, decrypt each
    with mp4decrypt using your key, then mux (with languages + names). Returns (ok, reason)."""
    mp4d = ensure_mp4decrypt(verbose)
    if not mp4d:
        return False, "mp4decrypt (Bento4) not found — install it to decrypt with --key"
    tracks = parse_master_tracks(master_url, session, max_height, headers, verbose)
    if not tracks or not tracks.get('video'):
        return False, "could not resolve tracks"
    audios = _select_tracks(tracks['audios'], AUDIO_SEL if AUDIO_SEL != 'SCAN' else None)
    subs = _select_tracks(tracks['subs'], SUB_SEL if SUB_SEL != 'SCAN' else None)
    out_path = os.path.splitext(out_path)[0] + _container_ext(len(audios) > 1 or bool(subs))
    made = []

    def _decrypt(media_url, tag, label):
        enc = _cenc_tmp(out_path, f"{tag}.enc")
        dec = _cenc_tmp(out_path, f"{tag}.dec.mp4")
        ok, why = _download_hls_raw(media_url, headers, session, enc, verbose, label)
        if not ok:
            return None, why
        dcmd = [mp4d]
        for spec in key_specs:
            dcmd += ['--key', spec if ':' in spec else f"1:{spec}"]
        dcmd += [enc, dec]
        if verbose:
            tqdm.write(f"[INFO] mp4decrypt: decrypting {tag} with your key(s)...")
        proc = subprocess.run(dcmd, capture_output=True, text=True)
        try:
            os.remove(enc)
        except OSError:
            pass
        if proc.returncode != 0 or not os.path.exists(dec) or os.path.getsize(dec) == 0:
            return None, (proc.stderr or '').strip()[:200] or "mp4decrypt failed"
        return dec, None

    vfile, why = _decrypt(tracks['video'], 'v', "video (enc)")
    if not vfile:
        return False, f"video: {why}"
    made.append(vfile)
    afiles, ameta = [], []
    for i, a in enumerate(audios):
        tag = a.get('lang') or a.get('name') or '?'
        af, why = _decrypt(a['uri'], f"a{i}", f"audio {i} [{tag}] (enc)")
        if not af:
            for f in made:
                try:
                    os.remove(f)
                except OSError:
                    pass
            return False, f"audio {i}: {why}"
        afiles.append(af)
        ameta.append((a.get('lang'), a.get('name')))
        made.append(af)
    sfiles, smeta = [], []
    for sf, lang, name in _grab_subs(subs, out_path, headers, session, verbose, suffix='vtt'):
        sfiles.append(sf)
        smeta.append((lang, name))
        made.append(sf)
    ok, reason = _ffmpeg_mux_multi(vfile, afiles, sfiles, ameta, smeta, out_path, verbose)
    for f in made:
        try:
            os.remove(f)
        except OSError:
            pass
    return ok, reason


def _ffmpeg_grab_stream(url, headers, out_path, verbose):
    """Download a stream straight through ffmpeg — it fetches the manifest and keys and DECRYPTS
    AES-128/SAMPLE-AES HLS, and also handles DASH (.mpd). Maps ALL video/audio/subtitle streams
    the input exposes. Slower than the parallel path but works on protected streams.
    Returns (ok, error_text)."""
    if not ensure_ffmpeg(verbose):
        return False, "ffmpeg unavailable"
    if FORCE_CONTAINER in ('mp4', 'mkv'):
        out_path = os.path.splitext(out_path)[0] + '.' + FORCE_CONTAINER
    ext = os.path.splitext(out_path)[1].lower() or '.mp4'
    is_mkv = ext == '.mkv'
    tmp = _temp_artifact(out_path, ".part" + ext)
    ua = (headers or {}).get('User-Agent', USER_AGENT)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()
                    if k.lower() not in ('user-agent',))
    cmd = [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-user_agent', ua]
    if extra:
        cmd += ['-headers', extra]
    cmd += ['-i', url,
            '-map', '0:v?', '-map', '0:a?', '-map', '0:s?',   # every video/audio/subtitle stream
            '-c', 'copy', '-c:s', 'srt' if is_mkv else 'mov_text']
    if not is_mkv:
        cmd += ['-movflags', '+faststart']
    cmd += ['-y', tmp]
    if verbose:
        tqdm.write(f"[INFO] ffmpeg grab (decrypt/all tracks) {url[:80]}")
    dur = _ffprobe_duration(url, headers, verbose)
    return _run_ffmpeg_progress(cmd, tmp, out_path, os.path.basename(out_path)[:24], dur, verbose)


def _ffmpeg_grab_hls(master_url, headers, session, max_height, out_path, verbose):
    """ffmpeg-based HLS grab that pulls the chosen video variant PLUS every separate audio and
    subtitle rendition (EXT-X-MEDIA) as its own ffmpeg input, so ALL tracks end up in the file —
    and ffmpeg decrypts AES-128 on each. Falls back to a single-input grab if the master can't be
    parsed. Returns (ok, error_text)."""
    if not ensure_ffmpeg(verbose):
        return False, "ffmpeg unavailable"
    tracks = parse_master_tracks(master_url, session, max_height, headers, verbose)
    if not tracks or not tracks.get('video'):
        return _ffmpeg_grab_stream(master_url, headers, out_path, verbose)
    audios = _select_tracks(tracks['audios'], AUDIO_SEL if AUDIO_SEL != 'SCAN' else None)
    subs = _select_tracks(tracks['subs'], SUB_SEL if SUB_SEL != 'SCAN' else None)
    out_path = os.path.splitext(out_path)[0] + _container_ext(len(audios) > 1 or bool(subs))
    is_mkv = out_path.lower().endswith('.mkv')
    inputs = [tracks['video']] + [a['uri'] for a in audios] + [s['uri'] for s in subs]
    tmp = _temp_artifact(out_path, ".part" + (os.path.splitext(out_path)[1] or ".mp4"))
    ua = (headers or {}).get('User-Agent', USER_AGENT)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()
                    if k.lower() not in ('user-agent',))
    cmd = [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-user_agent', ua]
    for u in inputs:
        if extra:
            cmd += ['-headers', extra]
        cmd += ['-i', u]
    cmd += ['-map', '0:v:0']
    if audios:
        for i in range(len(audios)):
            cmd += ['-map', f'{1 + i}:a:0']
    else:
        cmd += ['-map', '0:a?']                       # audio muxed in the video variant
    sub_base = 1 + len(audios)
    for i in range(len(subs)):
        cmd += ['-map', f'{sub_base + i}:s:0']
    cmd += ['-c', 'copy']
    if subs:
        cmd += ['-c:s', 'srt' if is_mkv else 'mov_text']
    for i, a in enumerate(audios):
        if a.get('lang'):
            cmd += [f'-metadata:s:a:{i}', f"language={_iso639_2(a['lang'])}"]
        if a.get('name'):
            cmd += [f'-metadata:s:a:{i}', f"title={a['name']}"]
            if not is_mkv:
                cmd += [f'-metadata:s:a:{i}', f"handler_name={a['name']}"]
    for i, s in enumerate(subs):
        if s.get('lang'):
            cmd += [f'-metadata:s:s:{i}', f"language={_iso639_2(s['lang'])}"]
        if s.get('name'):
            cmd += [f'-metadata:s:s:{i}', f"title={s['name']}"]
            if not is_mkv:
                cmd += [f'-metadata:s:s:{i}', f"handler_name={s['name']}"]
    if not is_mkv:
        cmd += ['-movflags', '+faststart']
    cmd += ['-y', tmp]
    if verbose:
        tqdm.write(f"[INFO] ffmpeg grab: 1 video + {len(audios)} audio + {len(subs)} subtitle "
                   f"track(s), decrypting as needed")
    dur = _ffprobe_duration(tracks['video'], headers, verbose)
    ok, why = _run_ffmpeg_progress(cmd, tmp, out_path, os.path.basename(out_path)[:24], dur, verbose)
    if not ok and not os.path.exists(out_path):
        return _ffmpeg_grab_stream(master_url, headers, out_path, verbose)
    return ok, why


def _hls_stream_encrypted(master_url, session, headers):
    """Peek at the HLS master (and one variant) for AES-128/SAMPLE-AES encryption, which the
    parallel segment path can't decrypt — such streams must go through ffmpeg."""
    def _enc(text):
        return (('#EXT-X-KEY' in text or '#EXT-X-SESSION-KEY' in text) and 'METHOD=NONE' not in text
                and ('AES-128' in text or 'SAMPLE-AES' in text or 'cenc' in text.lower()))
    try:
        r = session.get(master_url, headers=headers, timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        if r.status_code != 200:
            return False
        if _enc(r.text):
            return True
        for line in r.text.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                rv = session.get(urljoin(master_url, line), headers=headers,
                                 timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
                return rv.status_code == 200 and _enc(rv.text)
    except requests.RequestException:
        pass
    return False


_ISO639 = {'en': 'eng', 'ko': 'kor', 'ja': 'jpn', 'zh': 'chi', 'cs': 'cze', 'sk': 'slo',
           'de': 'ger', 'fr': 'fre', 'es': 'spa', 'it': 'ita', 'pt': 'por', 'ru': 'rus',
           'pl': 'pol', 'nl': 'dut', 'sv': 'swe', 'no': 'nor', 'da': 'dan', 'fi': 'fin',
           'tr': 'tur', 'ar': 'ara', 'hi': 'hin', 'th': 'tha', 'vi': 'vie', 'id': 'ind',
           'uk': 'ukr', 'ro': 'rum', 'hu': 'hun', 'el': 'gre', 'he': 'heb', 'fa': 'per'}


def _container_ext(multi_track):
    """Chosen output extension. --container forces .mp4/.mkv; otherwise auto: .mkv when there are
    multiple audio tracks or any subtitles (names/languages show reliably there), else .mp4."""
    if FORCE_CONTAINER in ('mp4', 'mkv'):
        return '.' + FORCE_CONTAINER
    return '.mkv' if multi_track else '.mp4'


def _apply_forced_container(path, verbose):
    """When --container forces a specific container, remux a finished DIRECT download into it
    (stream copy, no re-encode) and return the new path. No-op when --container is auto, when the
    file already has the target extension, when ffmpeg is unavailable, or when the copy fails
    (incompatible codecs) — in those cases the original file is kept untouched."""
    if FORCE_CONTAINER not in ('mp4', 'mkv') or not path or not os.path.exists(path):
        return path
    target = '.' + FORCE_CONTAINER
    if os.path.splitext(path)[1].lower() == target:
        return path
    if not ensure_ffmpeg(verbose):
        return path
    is_mkv = target == '.mkv'
    new_path = os.path.splitext(path)[0] + target
    tmp = _temp_artifact(new_path, ".remux" + target)
    cmd = [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', path,
           '-map', '0', '-c', 'copy']
    cmd += (['-c:s', 'srt'] if is_mkv else ['-movflags', '+faststart'])
    cmd += ['-y', tmp]
    if verbose:
        tqdm.write(f"[INFO] --container: remuxing {os.path.basename(path)} -> {FORCE_CONTAINER}")
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        tail = " | ".join(l for l in (proc.stderr or '').splitlines()[-2:] if l.strip())
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} --container: couldn't remux "
                   f"{os.path.basename(path)} to {FORCE_CONTAINER} (kept original)."
                   + (f" {tail}" if verbose and tail else ""))
        return path
    os.replace(tmp, new_path)
    if os.path.abspath(new_path) != os.path.abspath(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return new_path


def _iso639_2(code):
    """Normalise a language tag (e.g. 'en', 'en-US', 'eng') to an ISO 639-2 3-letter code so
    players show the right language. Unknown codes are passed through as-is."""
    if not code:
        return ''
    c = code.strip().lower().replace('_', '-').split('-')[0]
    if len(c) == 3:
        return c
    return _ISO639.get(c, c)


def _ffmpeg_mux_multi(video_file, audio_files, sub_files, audio_meta, sub_meta, out_path, verbose):
    """Mux a video file + N audio files + M subtitle files into one container. `audio_meta`/
    `sub_meta` are lists of (language, name), applied as per-track language + title. The container
    follows out_path's extension: .mkv (recommended for multiple audio/subtitle tracks — names and
    languages show reliably) or .mp4. Returns (ok, error_text)."""
    ext = os.path.splitext(out_path)[1].lower() or '.mp4'
    is_mkv = ext == '.mkv'
    tmp = _temp_artifact(out_path, ".part" + ext)
    cmd = [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', video_file]
    for af in audio_files:
        cmd += ['-i', af]
    for sf in sub_files:
        cmd += ['-i', sf]
    cmd += ['-map', '0:v:0']
    if audio_files:
        for i in range(len(audio_files)):
            cmd += ['-map', f'{1 + i}:a:0']
    else:
        cmd += ['-map', '0:a?']                    # keep audio muxed into the video variant
    sub_base = 1 + len(audio_files)
    for i in range(len(sub_files)):
        cmd += ['-map', f'{sub_base + i}:0']
    cmd += ['-c:v', 'copy', '-c:a', 'copy']
    if sub_files:
        cmd += ['-c:s', 'srt' if is_mkv else 'mov_text']   # mkv: SubRip; mp4: mov_text
    for i, (lang, name) in enumerate(audio_meta):
        if lang:
            cmd += [f'-metadata:s:a:{i}', f'language={_iso639_2(lang)}']
        if name:
            cmd += [f'-metadata:s:a:{i}', f'title={name}']
            if not is_mkv:
                cmd += [f'-metadata:s:a:{i}', f'handler_name={name}']
    for i, (lang, name) in enumerate(sub_meta):
        if lang:
            cmd += [f'-metadata:s:s:{i}', f'language={_iso639_2(lang)}']
        if name:
            cmd += [f'-metadata:s:s:{i}', f'title={name}']
            if not is_mkv:
                cmd += [f'-metadata:s:s:{i}', f'handler_name={name}']
    if not is_mkv:
        cmd += ['-movflags', '+faststart']
    cmd += ['-y', tmp]
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


def _ffmpeg_mux(video_file, audio_file, out_path, verbose):
    """Mux already-downloaded local stream files into out_path. Returns (ok, error_text)."""
    ext = os.path.splitext(out_path)[1].lower() or '.mp4'
    tmp = _temp_artifact(out_path, ".part" + ext)
    cmd = [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', video_file]
    if audio_file:
        cmd += ['-i', audio_file, '-map', '0:v:0', '-map', '1:a:0']
    else:
        cmd += ['-map', '0']
    cmd += ['-c', 'copy']
    if ext != '.mkv':
        cmd += ['-movflags', '+faststart']
    cmd += ['-sn', '-y', tmp]
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
        self.streams = {}          # 'v'/'a0'/'a1'/'s0'... -> {'parts': [paths in order]}
        self.audio_meta = []       # [(stream_key, lang, name)] for separate audio tracks
        self.sub_meta = []         # [(stream_key, lang, name)] for subtitle tracks
        self.tasks = []            # (stream_key, idx, url, path)
        self.remaining = 0
        self.failed = False
        self.fail_reason = None
        self.bar = None


def _add_hls_stream(job, key, murl, session, headers):
    """Fetch a media playlist and register its (init + ordered segment) download tasks under key.
    Returns True if it has any segments."""
    init, segs = parse_media_playlist(murl, session, headers)
    parts = []
    if init:
        p = os.path.join(job.parts_dir, f"{key}_init")
        job.tasks.append((key, -1, init, p))
        parts.append(p)
    for i, su in enumerate(segs):
        p = os.path.join(job.parts_dir, f"{key}_{i:05d}")
        job.tasks.append((key, i, su, p))
        parts.append(p)
    if parts:
        job.streams[key] = {'parts': parts}
        return True
    return False


def _build_hls_job(video, session, out_dir, max_height, verbose):
    """Resolve a video (Vimeo/Mux/Twitch/scan) to its HLS segment lists, honouring --audio/--sub.
    Returns a ready _HlsJob, or None (also None when just listing tracks with a bare --audio/--sub)."""
    fallback = video.get('vimeo_id') or video.get('title') or 'video'
    stem = safe_filename(video['title'] or fallback, fallback)
    stem = re.sub(r'\.(mp4|mkv)$', '', stem, flags=re.I)

    master, headers = resolve_master(video, session, verbose)
    if not master:
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} No HLS for {video['title']} (private/unavailable).")
        return None
    tracks = parse_master_tracks(master, session, max_height, headers, verbose)
    if not tracks or not tracks.get('video'):
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} No video stream for {video['title']}.")
        return None
    audios, subs = tracks['audios'], tracks['subs']

    # Bare --audio / --sub: just list what's available and skip the download.
    if AUDIO_SEL == 'SCAN' or SUB_SEL == 'SCAN':
        _hls_list_tracks(video['title'], audios, subs)
        return None

    sel_audios = _select_tracks(audios, AUDIO_SEL)
    sel_subs = _select_tracks(subs, SUB_SEL)

    # Use MKV when there are several audio tracks or any subtitles — track NAMES and LANGUAGES
    # display reliably there (mp4 players often ignore them). A single audio track stays .mp4.
    ext = _container_ext(len(sel_audios) > 1 or bool(sel_subs))
    filename = stem + ext
    out_path = os.path.join(out_dir, filename) if out_dir else filename
    if os.path.exists(out_path) or os.path.exists(os.path.splitext(out_path)[0] + '.mp4'):
        tqdm.write(f"[INFO] Already have {filename}, skipping.")
        return None

    job = _HlsJob(video, out_path, headers)
    os.makedirs(job.parts_dir, exist_ok=True)
    _add_hls_stream(job, 'v', tracks['video'], session, headers)
    for idx, a in enumerate(sel_audios):
        key = f'a{idx}'
        if _add_hls_stream(job, key, a['uri'], session, headers):
            job.audio_meta.append((key, a['lang'], a['name']))
    for idx, s in enumerate(sel_subs):
        key = f's{idx}'
        if _add_hls_stream(job, key, s['uri'], session, headers):
            job.sub_meta.append((key, s['lang'], s['name']))

    if 'v' not in job.streams or not job.streams['v']['parts']:
        tqdm.write(f"{CLR.YELLOW}[WARN]{CLR.RESET} Empty playlist for {video['title']}.")
        return None
    job.remaining = len(job.tasks)
    return job


def download_hls_pooled(videos, session, out_dir, max_connections, max_height, verbose):
    """Download all native (Vimeo/Mux) videos as HLS via a shared pool of segment workers,
    then mux each with ffmpeg. All videos progress at once; the budget is shared."""
    from concurrent.futures import ThreadPoolExecutor

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
    _seg_workers = _hls_worker_count(max_connections)
    print(f"[INFO] Downloading {len(jobs)} native video(s) with {_seg_workers} parallel segment "
          f"thread(s) (of {max_connections} allowed connections), muxed with ffmpeg.\n")

    bar_lock = threading.Lock()
    overall = None
    if per_file_bars:
        for idx, job in enumerate(jobs):
            done = sum(1 for (_k, _i, _u, p) in job.tasks if os.path.exists(p) and os.path.getsize(p) > 0)
            job.bar = make_bar(total=max(len(job.tasks), 1), initial=done, unit='seg',
                               unit_scale=False, desc=os.path.basename(job.out_path),
                               position=idx, leave=True, mininterval=0.4)
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
            reason = getattr(job, 'fail_reason', None) or "segment download failed"
        else:
            try:
                vfile = _temp_artifact(job.out_path, ".video")
                _concat_stream(job.streams['v']['parts'], vfile)
                afiles, ameta = [], []
                for key, lang, name in job.audio_meta:
                    af = _temp_artifact(job.out_path, f".{key}")
                    _concat_stream(job.streams[key]['parts'], af)
                    afiles.append(af)
                    ameta.append((lang, name))
                sfiles, smeta = [], []
                for key, lang, name in job.sub_meta:
                    sf = _temp_artifact(job.out_path, f".{key}.vtt")
                    _concat_stream(job.streams[key]['parts'], sf)
                    sfiles.append(sf)
                    smeta.append((lang, name))
                ok, reason = _ffmpeg_mux_multi(vfile, afiles, sfiles, ameta, smeta,
                                               job.out_path, verbose)
            except Exception as exc:
                reason = str(exc)
            finally:
                cleanup = [_temp_artifact(job.out_path, ".video")]
                for key, _l, _n in job.audio_meta:
                    cleanup.append(_temp_artifact(job.out_path, f".{key}"))
                for key, _l, _n in job.sub_meta:
                    cleanup.append(_temp_artifact(job.out_path, f".{key}.vtt"))
                for extra in cleanup:
                    if os.path.exists(extra):
                        try:
                            os.remove(extra)
                        except OSError:
                            pass
        if ok:
            shutil.rmtree(job.parts_dir, ignore_errors=True)
            _record_download(job.out_path)
        else:
            _record_failed({'kind': 'native', 'out_dir': os.getcwd(),
                            'filename': os.path.basename(job.out_path or ''),
                            'reason': reason or 'download failed',
                            'stream': job.video if isinstance(job.video, dict) else None,
                            'max_height': max_height})
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
            ok_seg, why = _download_hls_segment(url, path, sess, job.headers)
            if not ok_seg:
                job.failed = True
                if getattr(job, 'fail_reason', None) is None:
                    job.fail_reason = why
                if verbose and why:
                    tqdm.write(f"[DBG] segment failed ({job.title}): {why}")
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

    workers = _hls_worker_count(max_connections)
    # Submitting every segment up front (tens of thousands of Future objects) plus an
    # as_completed() waiter over all of them costs real CPU; the pool drains on exit anyway.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t in order:
            ex.submit(run_task, *t)

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


_EP_MARKERS = {'ep', 'eps', 'episode', 'episodes', 'episod', 'e',
               'pt', 'part', 'chapter', 'ch'}
_MONTHS = {'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov',
           'dec', 'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september',
           'october', 'november', 'december'}
_ORDINAL_SUFFIX = {'th', 'st', 'nd', 'rd'}
_SERIES_NOISE = DEFAULT_STOP | _MONTHS | _ORDINAL_SUFFIX


def _ep_number(ns):
    """The episode number from a tokenized name: the number right after an Ep/Episode/E/Part/#
    marker (so a leading date like 'Feb 10th' is ignored). Returns int or None."""
    found, prev = [], None
    for lead, kind, text in ns:
        if kind == 'num':
            if (prev and prev.lower() in _EP_MARKERS) or '#' in lead:
                found.append(int(text))
            prev = None
        elif kind == 'word':
            prev = text
    return found[-1] if found else None


def _core_words(ns):
    """The show-title words in a name: everything before the episode marker/number, with leading
    date/noise words (months, 'th'/'st', 'uncut', articles) stripped."""
    words, prev = [], None
    for lead, kind, text in ns:
        if kind == 'num':
            if (prev and prev.lower() in _EP_MARKERS) or '#' in lead:
                break                                    # reached the episode number -> stop
            prev = None                                  # a date number: skip it, keep going
        elif kind == 'word':
            if text.lower() in _EP_MARKERS:
                break                                    # reached the 'Ep' marker -> stop
            words.append(text)
            prev = text
    while words and words[0].lower() in _SERIES_NOISE:
        words = words[1:]
    while words and words[-1].lower() in _SERIES_NOISE:
        words = words[:-1]
    return words


def _sublist(run, seq):
    n = len(run)
    return n > 0 and any(seq[i:i + n] == run for i in range(len(seq) - n + 1))


def _series_words(members):
    """The show title: the MOST COMMON core-word sequence across the files (majority vote), so
    typos ('Samdarl'), stray dates, and odd one-off files don't corrupt it. Original casing kept."""
    cores = [_core_words(m['ns']) for m in members]
    keyed = [(tuple(w.lower() for w in c), c) for c in cores if c]
    if not keyed:
        return []
    common = Counter(k for k, _ in keyed).most_common(1)[0][0]
    for k, c in keyed:
        if k == common:
            return c
    return list(common)


def _episode_series_plan(members, min_width):
    """If most files carry an explicit episode number (Ep/Episode/E/Part/#N), rename them to
    '<Series> <N>' — dropping date prefixes and parenthetical notes. Returns a plan or None."""
    eps = [_ep_number(m['ns']) for m in members]
    have = [e for e in eps if e is not None]
    if len(have) < max(2, (len(members) + 1) // 2):
        return None
    series = _series_words(members)
    if len(series) < 2:
        return None
    width = max(min_width or 0, max(len(str(e)) for e in have))
    name = " ".join(case_word(w, "title", i == 0) for i, w in enumerate(series))
    plan = []
    for m, ep in zip(members, eps):
        if ep is None:
            plan.append((m["name"], m["name"]))          # leave odd ones out untouched
        else:
            plan.append((m["name"], f"{name} {str(ep).zfill(width)}" + m["ext"]))
    return plan


def process_group(members, do_pad, do_words, min_width, strict):
    # Episode-series shortcut: when most files have an explicit Ep/Episode/#N, name them
    # "<Series> <N>" (ignoring date prefixes and notes) instead of aligning every number.
    if strict:
        ep_plan = _episode_series_plan(members, min_width)
        if ep_plan is not None:
            return ep_plan
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
# When a Patreon collection is being processed, its (clean) title is stored here so the
# post-download rename can name files "<Collection> <NN>" instead of the messy post titles.
_active_collection_title = None
# Patreon creator/campaign display name (e.g. "ES-DE Frontend (@es_de)") for the current source.
_patreon_creator = None


def _creator_from_attrs(a):
    name = (a.get('name') or '').strip()
    vanity = (a.get('vanity') or '').strip()
    if name:
        return f"{name} (@{vanity})" if vanity else name
    if vanity:
        return f"@{vanity}"
    return None

# Jobs that FAILED this run, captured with enough info to retry them later via resume.json.
RESUME_FILE = "resume.json"
_failed_jobs = []          # transient: failures of the current download/retry operation
_session_failed = []       # accumulates still-failing entries across the whole invocation
_failed_lock = threading.Lock()


def _record_failed(entry):
    """Remember a failed download (enough to retry it) for resume.json. Never raises."""
    try:
        with _failed_lock:
            _failed_jobs.append(entry)
    except Exception:
        pass


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


def _series_display_name(name):
    """Clean, Title-cased (acronym-aware) form of a collection name, e.g. 'My Demon'."""
    toks = apply_case(tokenize(clean_string(name or "", ())), 'title')
    return finalize_stem("".join(t for _k, t in toks)) or (name or "").strip() or "video"


def _ep_token_index(toks):
    """In a tokenize() list, the index+value of the EPISODE number: the num right after an
    Ep/Episode/E/Part marker (or after '#'), so a leading date ('March 11th') is ignored. Falls
    back to the first number. Returns (index, value_str) or (None, None)."""
    prev_word, prev_sep = None, ""
    for i, (k, t) in enumerate(toks):
        if k == 'num':
            if (prev_word and prev_word.lower() in _EP_MARKERS) or '#' in prev_sep:
                return i, t
        elif k == 'word':
            prev_word, prev_sep = t, ""
        elif k == 'sep':
            prev_sep = t
    for i, (k, t) in enumerate(toks):        # fallback: first number in the name
        if k == 'num':
            return i, t
    return None, None


def build_collection_plan(files, collection_name):
    """Name files as '<Collection> <episode-number>' (zero-padded), taking the episode number
    from each original filename. When several files map to the same name (a collision), append
    distinguishing text pulled from the original name (extra numbers/words), so nothing is lost.

    Returns a list of (old_name, new_name) pairs, or None if it can't do anything useful
    (e.g. no filename has a number to use as an episode id)."""
    series = _series_display_name(collection_name)
    series_words = {w.lower() for w in re.findall(r"[^\W\d_]+", collection_name or "", re.UNICODE)}

    items = []
    for name in files:
        stem, ext = os.path.splitext(name)
        toks = tokenize(clean_string(stem, ()))
        first_idx, first = _ep_token_index(toks)          # episode number, not a leading date
        after = toks[first_idx + 1:] if first_idx is not None else list(toks)
        suffix = [(k, t) for (k, t) in after
                  if not (k == 'word' and t.lower() in series_words)]
        items.append({'name': name, 'ext': ext, 'first': first, 'suffix': suffix})

    ep_nums = [it['first'] for it in items if it['first'] is not None]
    if not ep_nums:
        return None  # nothing numeric to key on -> let the generic renamer handle it
    ep_w = max([len(str(int(n))) for n in ep_nums] + [len(n) for n in ep_nums])

    for it in items:
        if it['first'] is not None:
            it['base'] = f"{series} {it['first'].zfill(ep_w)}"
        else:
            it['base'] = _series_display_name(os.path.splitext(it['name'])[0])

    def suffix_parts(it, keep_stop):
        out, first_word = [], True
        for k, t in it['suffix']:
            if k == 'num':
                out.append(('num', t))
            elif k == 'word':
                if (not keep_stop) and t.lower() in DEFAULT_STOP:
                    continue
                out.append(('word', case_word(t, 'title', first_word)))
                first_word = False
        return out

    by_base = defaultdict(list)
    for it in items:
        by_base[it['base']].append(it)

    used = set()
    for base, grp in by_base.items():
        if len(grp) == 1:
            used.add(finalize_stem(grp[0]['base']).lower())

    plan = []
    for base, grp in by_base.items():
        if len(grp) == 1:
            it = grp[0]
            plan.append((it['name'], finalize_stem(it['base']) + it['ext']))
            continue
        # Collision: build a distinguishing suffix. Try compact (drop filler words) first,
        # then the full text (keep them), then a numeric counter as a last resort.
        resolved = None
        for keep_stop in (False, True):
            swidth = {}
            for it in grp:
                si = 0
                for kind, val in suffix_parts(it, keep_stop):
                    if kind == 'num':
                        swidth[si] = max(swidth.get(si, 0), len(val), len(str(int(val))))
                        si += 1
            cand = []
            for it in grp:
                pieces, si = [], 0
                for kind, val in suffix_parts(it, keep_stop):
                    if kind == 'num':
                        pieces.append(val.zfill(swidth.get(si, len(val))))
                        si += 1
                    else:
                        pieces.append(val)
                s = " ".join(pieces)
                cand.append((it, f"{it['base']} {s}".strip() if s else it['base']))
            names = [finalize_stem(n).lower() for _it, n in cand]
            if len(set(names)) == len(names) and not (set(names) & used):
                resolved = cand
                break
        if resolved is None:
            resolved = [(it, f"{it['base']} ({i})") for i, it in enumerate(grp, 1)]
        for it, n in resolved:
            used.add(finalize_stem(n).lower())
            plan.append((it['name'], finalize_stem(n) + it['ext']))

    order = {name: i for i, name in enumerate(files)}
    plan.sort(key=lambda p: order.get(p[0], 0))
    return plan


def offer_strict_rename(directory, new_files, verbose, enabled=True, rename_mode='ask'):
    """Preview and (optionally) apply an intelligent --strict rename of freshly downloaded
    files. Returns a small result dict for logging.

    rename_mode='ask'  -> interactive prompt (default; needs a TTY).
    rename_mode='auto' -> apply automatically when there are NO naming conflicts; if any
                          conflict exists, skip the rename entirely (auto 'N'). Used by
                          --url-list so batches run unattended.
    """
    result = {'status': 'disabled', 'changed': 0, 'conflicts': 0, 'applied': 0,
              'renames': [], 'conflict_names': []}
    if not enabled or not new_files:
        return result
    interactive = bool(getattr(sys.stdin, 'isatty', lambda: False)()) and \
        bool(getattr(sys.stdout, 'isatty', lambda: False)())
    if rename_mode == 'ask' and not interactive:
        result['status'] = 'non-interactive'
        return result
    try:
        plan = None
        if _active_collection_title:
            plan = build_collection_plan(sorted(new_files), _active_collection_title)
        if plan is None:
            plan, _n = build_plan(sorted(new_files), do_pad=True, do_words=True,
                                  min_width=0, removes=(), case_mode="title", strict=True,
                                  group=True, group_min=1, stop=[])
    except Exception as exc:
        if verbose:
            print(f"[WARN] Renamer could not build a plan: {exc}")
        result['status'] = 'error'
        return result
    try:
        existing = set(os.listdir(directory))
    except OSError:
        existing = set()
    skip = detect_collisions(plan, existing)
    use_color = bool(CLR.RESET)
    changed = [(o, n) for o, n in plan if o != n and o not in skip]
    conflicts = [(o, n) for o, n in plan if o in skip]
    result['changed'] = len(changed)
    result['conflicts'] = len(conflicts)

    print(f"\n[INFO] {len(new_files)} file(s) downloaded. Intelligent --strict rename preview "
          f"(videoloader_dir v{SCRIPT_VERSION}):")
    print_preview(plan, skip, True, use_color)

    if not changed and not conflicts:
        print("[INFO] Filenames are already consistent; nothing to rename.")
        result['status'] = 'nothing'
        return result

    result['conflict_names'] = list(conflicts)

    if rename_mode == 'auto':
        if conflicts:
            print(f"[INFO] --url-list: {len(conflicts)} naming conflict(s) detected -> "
                  f"skipping rename automatically (auto N).")
            result['status'] = 'conflict-skipped'
            return result
        if not changed:
            result['status'] = 'nothing'
            return result
        print("[INFO] --url-list: no conflicts -> applying renames automatically (auto Y).")
        apply_renames(changed, directory, use_color)
        result['status'] = 'applied'
        result['applied'] = len(changed)
        result['renames'] = list(changed)
        return result

    # Interactive mode.
    if not changed:
        print("[INFO] Filenames are already consistent; nothing to rename.")
        result['status'] = 'nothing'
        return result
    try:
        ans = input("\nApply these renames? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        result['status'] = 'declined'
        return result
    if ans in ("n", "no", "ne"):
        print("[INFO] Left the filenames unchanged.")
        result['status'] = 'declined'
    else:
        apply_renames(changed, directory, use_color)
        result['status'] = 'applied'
        result['applied'] = len(changed)
        result['renames'] = list(changed)
    return result


def _has_ytdlp():
    """True if a yt-dlp executable is available on PATH."""
    import shutil as _sh
    return bool(_sh.which('yt-dlp') or _sh.which('yt-dlp.exe'))


def process_youtube(video_id, session, max_connections, max_height, verbose, list_only, out_dir,
                    output_file=None, res_scan=False):
    print(f"[INFO] Reading YouTube video {video_id} ...")
    if res_scan:
        _yt_scan_formats(video_id, session, verbose)
        return True
    if list_only:
        master, title, _dur = resolve_youtube(video_id, session, verbose)
        print(f"   1) [YouTube] {title or video_id}")
        if verbose:
            print(f"        id: {video_id} | solver: "
                  f"{'pywebview available' if _yt_can_solve() else 'pywebview MISSING'}")
        return True
    # Fast path: live streams / videos that expose an HLS manifest need no signature solving.
    master, title, _dur = resolve_youtube(video_id, session, verbose)
    if master:
        if not ensure_ffmpeg(verbose):
            print("[ERROR] ffmpeg is required to mux the stream.")
            return False
        if output_file:
            title = os.path.splitext(os.path.basename(output_file))[0]
        download_hls_pooled([{'source': 'youtube', 'youtube_id': video_id,
                              'master_url': master, 'title': title or video_id}],
                            session, None, max_connections, max_height, verbose)
        return True
    # Regular VOD: DASH formats decoded via the node signature/n solver, muxed with ffmpeg.
    _title = os.path.splitext(os.path.basename(output_file))[0] if output_file else ''
    download_youtube([{'youtube_id': video_id, 'title': _title or video_id}],
                     session, DEFAULT_CHUNK_SIZE, max_connections, max_height, verbose)
    return True


def process_youtube_playlist(playlist_id, session, max_connections, max_height, verbose,
                             list_only, out_dir, res_scan=False):
    print(f"[INFO] Reading YouTube playlist {playlist_id} ...")
    videos, title = youtube_playlist_videos(playlist_id, session, verbose)
    if not videos:
        print("[ERROR] No videos found in the playlist (private, empty, or removed).")
        return
    if res_scan:
        print("[INFO] Scanning qualities of the first video (playlists usually share the set).")
        _yt_scan_formats(videos[0]['youtube_id'], session, verbose)
        return
    if _yt_bad_title(title):
        title = youtube_playlist_title(playlist_id, session, verbose)
    if _yt_bad_title(title):
        title = None                       # never let the consent-wall title pollute renames
    global _active_collection_title
    _active_collection_title = (title or '').strip() or None
    print(f"[INFO] Playlist '{title or playlist_id}': {len(videos)} video(s).")
    if list_only:
        for i, v in enumerate(videos, 1):
            print(f"   {i:>3}) [YouTube] {v['title']}")
            if verbose:
                print(f"        id: {v['youtube_id']}")
        return
    download_youtube(videos, session, DEFAULT_CHUNK_SIZE, max_connections, max_height, verbose)


def process_twitch(vod_id, session, max_connections, max_height, verbose, list_only, out_dir,
                   output_file=None):
    if not ensure_ffmpeg(verbose):
        print("[ERROR] Twitch VODs are HLS and need ffmpeg to mux into MP4.")
        return False
    print(f"[INFO] Reading Twitch VOD {vod_id} ...")
    master, title = resolve_twitch(vod_id, session, verbose)
    if not master:
        print("[ERROR] Could not get the Twitch VOD stream (sub-only VODs need your Twitch cookies).")
        return False
    if output_file:
        title = os.path.splitext(os.path.basename(output_file))[0]
    desc = {'source': 'twitch', 'twitch_id': vod_id, 'title': title or f'twitch_{vod_id}'}
    if list_only:
        print(f"   1) [Twitch] {desc['title']}")
        if verbose:
            print(f"        src: {master}")
        return True
    download_hls_pooled([desc], session, None, max_connections, max_height, verbose)
    return True


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
    if 'twitch.tv' in id_or_url:
        vod = _twitch_vod_id(id_or_url)
        if vod:
            return 'twitch', vod
    if 'youtube.com' in id_or_url or 'youtu.be' in id_or_url:
        pl = _yt_playlist_id(id_or_url)
        vid = _yt_video_id(id_or_url)
        # A browser-copied watch URL often carries both ?v= and &list=. Prefer the whole
        # playlist when a real list= is present; fall back to the single video otherwise.
        if pl:
            return 'youtube_playlist', pl
        if vid:
            return 'youtube', vid
    if 'vimeo.com' in id_or_url:
        vid = re.search(r'vimeo\.com/(?:video/)?(\d+)', id_or_url)
        if vid:
            h = re.search(r'[/?&]h=([0-9a-zA-Z]+)', id_or_url) or \
                re.search(r'vimeo\.com/\d+/([0-9a-zA-Z]+)', id_or_url)
            return 'vimeo', {'source': 'vimeo', 'title': vid.group(1),
                             'vimeo_id': vid.group(1), 'vimeo_hash': h.group(1) if h else ''}
    if 'streamable.com' in id_or_url:
        sc = _streamable_id(id_or_url)
        if sc:
            return 'streamable', sc
    if 'vidyard.com' in id_or_url:
        vy = _vidyard_id(id_or_url)
        if vy:
            return 'vidyard', vy
    if 'muse.ai' in id_or_url or 'skiv.com' in id_or_url:
        sv = _muse_id(id_or_url)
        if sv:
            return 'muse', sv
    kind, tid = extract_drive_target(id_or_url)
    return kind, tid


def _url_resolution(url):
    """Height (+ optional bitrate) from a clear resolution marker in the URL, e.g. 720P_4000K,
    240p, 1080P. Returns (0, 0) when there is no such NNNp marker."""
    u = url.lower()
    m = re.search(r'(\d{3,4})p(?![0-9])', u)              # 240p, 720P, 1080P
    if not m:
        return 0, 0
    h = int(m.group(1))
    bm = re.search(r'(\d{3,5})\s*k(?:bps)?(?![0-9a-z])', u)   # 4000K (tie-breaker)
    return h, (int(bm.group(1)) if bm else 0)


def _res_signature(url):
    """A resolution-agnostic key so different renditions of the SAME stream collapse together,
    even when each rendition URL carries its own signed token. Uses host+path (drops the query),
    removes the resolution/bitrate token, and strips long random/signed segments (auth tokens),
    while keeping shorter stable ids (like a numeric video id) so different videos stay distinct."""
    p = urlparse(url)
    s = (p.netloc + p.path).lower()
    s = re.sub(r'\d{3,4}\s*p[_\-]?\d{0,5}k?', '', s)     # 720p / 720p_4000k
    s = re.sub(r'[a-z0-9=+_\-]{20,}', '', s)            # long signed/random segments
    return s


def _scan_resolution_winners(found):
    """Collapse same-stream renditions that encode a resolution in the URL down to the best one
    (highest height, then bitrate); keep everything else (other videos, known embeds, streams
    with no resolution hint). Preserves order."""
    best = {}   # signature -> (height, bitrate, id(item))
    for f in found:
        if f['kind'] not in ('hls', 'direct'):
            continue
        h, br = _url_resolution(f['url'])
        if h == 0:
            continue
        sig = _res_signature(f['url'])
        cur = best.get(sig)
        if cur is None or (h, br) > (cur[0], cur[1]):
            best[sig] = (h, br, id(f))
    winners = {v[2] for v in best.values()}
    keep = []
    for f in found:
        if f['kind'] in ('hls', 'direct') and _url_resolution(f['url'])[0] > 0:
            if id(f) in winners:
                keep.append(f)
        else:
            keep.append(f)
    return keep


def _page_title(html_text):
    """Best-effort human title of a page: og:title -> twitter:title -> <title> -> <h1>."""
    import html as _h
    for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
                r'<title[^>]*>([^<]+)</title>',
                r'<h1[^>]*>([^<]+)</h1>'):
        m = re.search(pat, html_text, re.I | re.S)
        if m:
            t = re.sub(r'\s+', ' ', _h.unescape(m.group(1))).strip()
            if t:
                return t
    return None


def _scan_unique_name(base, used):
    """Return a unique '<base>.mp4' filename within one --scan run (adds ' (2)', ' (3)' ...) so
    several title-named streams don't overwrite each other. Records the choice in `used`."""
    root = re.sub(r'\.(mp4|m4v|webm|mov|mkv)$', '', base, flags=re.I).strip() or 'video'
    name = root + '.mp4'
    i = 2
    while name.lower() in used:
        name = f"{root} ({i}).mp4"
        i += 1
    used.add(name.lower())
    return name


def _classify_media_url(u):
    """Turn a raw media URL into a (kind, canonical_url, label) tuple, or None if not media."""
    m = re.search(r'(?:youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|v/|shorts/|live/)'
                  r'|youtu\.be/)([0-9A-Za-z_-]{11})', u)
    if m:
        return 'url', f'https://www.youtube.com/watch?v={m.group(1)}', f'YouTube {m.group(1)}'
    m = re.search(r'(?:player\.)?vimeo\.com/(?:video/)?(\d{6,})', u)
    if m:
        return 'url', f'https://vimeo.com/{m.group(1)}', f'Vimeo {m.group(1)}'
    m = re.search(r'twitch\.tv/videos/(\d+)', u)
    if m:
        return 'url', f'https://www.twitch.tv/videos/{m.group(1)}', f'Twitch VOD {m.group(1)}'
    m = re.search(r'drive\.google\.com/file/d/([0-9A-Za-z_-]+)', u)
    if m:
        return 'url', f'https://drive.google.com/file/d/{m.group(1)}/view', f'Drive {m.group(1)}'
    m = re.search(_STREAMABLE_RE, u, re.I)
    if m and m.group(1).lower() not in _STREAMABLE_RESERVED:
        return 'url', f'https://streamable.com/{m.group(1)}', f'Streamable {m.group(1)}'
    if re.search(r'dropbox\.com/(?:s|scl/fi)/', u):
        return 'url', u, 'Dropbox file'
    if u.startswith('blob:') or u.startswith('data:'):
        return None                    # can't fetch these directly (MediaSource/inline)
    if re.search(r'\.m3u8(?:[?#/]|$)', u, re.I) or re.search(r'\(format=m3u8[^)]*\)', u, re.I):
        return 'hls', u, 'HLS stream (.m3u8)'
    # '.mpd' covers foo.mpd and the Unified-Origin/IIS shape '<asset>.ism/.mpd'; the second form
    # catches '<asset>.ism/manifest(format=mpd-time-csf)' used by the same origin servers.
    if re.search(r'\.mpd(?:[?#/]|$)', u, re.I) or re.search(r'\(format=mpd[^)]*\)', u, re.I):
        return 'mpd', u, 'DASH stream (.mpd)'
    if re.search(r'\.(mp4|webm|mov|m4v)(?:[?#]|$)', u, re.I):
        return 'direct', u, _direct_label(u)
    return None


_BROWSER_COLLECT_JS = r"""
(function(){
  var out = {urls: [], all: [], title: document.title || ''};
  var isMedia = function(n){
    return /\.(m3u8|mpd|mp4|webm|mov|m4v)(\?|#|\/|$)/i.test(n) ||
           /\(format=(mpd|m3u8)[^)]*\)/i.test(n) ||
           /\.isml?\/(?:\.?mpd|manifest|$)/i.test(n) ||
           /(youtube|youtu\.be|vimeo|twitch\.tv|drive\.google|dropbox)\.?/i.test(n);
  };
  // Segments, images and scripts are noise. Everything else the page fetched is reported
  // separately, because a manifest served from an address with no extension (for instance
  // /api/playback?type=dash) is indistinguishable from an ordinary request out here — Python
  // settles it by reading the first few KB of each candidate.
  var isAsset = function(n){
    return /\.(jpe?g|png|gif|webp|avif|svg|ico|css|js|mjs|woff2?|ttf|otf|map|m4s|ts|aac|vtt|json)(\?|#|$)/i.test(n) ||
           /^(data|blob|about|chrome|file):/i.test(n);
  };
  var push = function(n){
    if (!n) return;
    n = String(n);
    if (isMedia(n)) out.urls.push(n);
    else if (!isAsset(n) && /^https?:/i.test(n) && out.all.length < 600) out.all.push(n);
  };
  try {
    var og = document.querySelector('meta[property="og:title"]');
    if (og && og.content) out.title = og.content;
  } catch (e) {}
  var scrape = function(doc){
    try {
      Array.prototype.forEach.call(doc.querySelectorAll('video,source,audio'), function(e){
        if (e.src) out.urls.push(e.src);
        if (e.currentSrc) out.urls.push(e.currentSrc);
      });
      Array.prototype.forEach.call(doc.querySelectorAll('iframe'), function(e){
        if (e.src) out.urls.push(e.src);
      });
      // Player configuration frequently sits in data-* attributes rather than a real src.
      ['data-src','data-url','data-mpd','data-dash','data-hls','data-manifest','data-stream',
       'data-video','data-source','data-file','data-setup'].forEach(function(a){
        try {
          Array.prototype.forEach.call(doc.querySelectorAll('[' + a + ']'), function(e){
            var v = e.getAttribute(a); if (v) out.urls.push(v);
          });
        } catch (e) {}
      });
    } catch (e) {}
  };
  scrape(document);
  var perf = function(w){
    try {
      var res = (w.performance && w.performance.getEntriesByType) ?
                w.performance.getEntriesByType('resource') : [];
      for (var i = 0; i < res.length; i++) push(res[i].name || '');
    } catch (e) {}
  };
  perf(window);
  try {
    for (var i = 0; i < window.frames.length; i++) {
      try { scrape(window.frames[i].document); } catch (e) {}
      try { perf(window.frames[i]); } catch (e) {}
    }
  } catch (e) {}
  try { (window.__vlSeen || []).forEach(push); } catch (e) {}
  return JSON.stringify(out);
})()
"""


_BROWSER_HOOK_JS = r"""
(function(){
  // Installed as early as possible, and re-runnable: every call also covers frames that have
  // appeared since. Everything funnels into the TOP window's __vlSeen, so one collect gets all.
  if (!window.__vlSeen) window.__vlSeen = [];
  var note = function(u){ try { if (u) window.__vlSeen.push(String(u)); } catch (e) {} };
  var install = function(w){
    try {
      if (!w || w.__vlHooked) return;
      w.__vlHooked = true;
      // The default resource-timing buffer holds ~250 entries; on a heavy page the manifest
      // request is long evicted by the time we look. Enlarge it AND observe live.
      try { w.performance.setResourceTimingBufferSize(5000); } catch (e) {}
      try {
        var po = new w.PerformanceObserver(function(list){
          try { list.getEntries().forEach(function(en){ note(en.name); }); } catch (e) {}
        });
        po.observe({entryTypes: ['resource']});
      } catch (e) {}
      try {
        var ox = w.XMLHttpRequest.prototype.open;
        w.XMLHttpRequest.prototype.open = function(m, u){ note(u); return ox.apply(this, arguments); };
      } catch (e) {}
      try {
        var of = w.fetch;
        w.fetch = function(r){ note((r && r.url) ? r.url : r); return of.apply(this, arguments); };
      } catch (e) {}
      try {
        var ob = w.navigator.sendBeacon;
        if (ob) w.navigator.sendBeacon = function(u){ note(u); return ob.apply(this, arguments); };
      } catch (e) {}
    } catch (e) {}
  };
  install(window);
  try {
    for (var i = 0; i < window.frames.length; i++) {
      try { install(window.frames[i]); } catch (e) {}   // cross-origin frames throw: skipped
    }
  } catch (e) {}
  return true;
})()
"""


# Most players never request their manifest until something presses play, so a passive page load
# finds nothing at all. This mutes and starts every <video> and clicks the usual play buttons.
_BROWSER_PLAY_JS = r"""
(function(){
  var n = 0;
  var sels = ['button[aria-label*="play" i]', 'button[title*="play" i]', '[aria-label*="Přehrát" i]',
              '.vjs-big-play-button', '.jw-icon-playback', '.jw-display-icon-container',
              '.plyr__control--overlaid', '.ytp-large-play-button', '.shaka-play-button',
              '[class*="play-button"]', '[class*="playButton"]', '[class*="btn-play"]',
              '[id*="play-button"]', '[data-testid*="play"]', 'button.play', 'a.play'];
  var poke = function(doc){
    try {
      Array.prototype.forEach.call(doc.querySelectorAll('video'), function(v){
        try {
          v.muted = true; v.defaultMuted = true; v.autoplay = true;
          var p = v.play(); if (p && p.catch) p.catch(function(){});
          n++;
        } catch (e) {}
      });
      sels.forEach(function(s){
        try {
          Array.prototype.forEach.call(doc.querySelectorAll(s), function(el){
            try {
              if (el.__vlClicked) return;
              el.__vlClicked = 1;
              el.click();
              n++;
            } catch (e) {}
          });
        } catch (e) {}
      });
    } catch (e) {}
  };
  poke(document);
  try {
    for (var i = 0; i < window.frames.length; i++) {
      try { poke(window.frames[i].document); } catch (e) {}
    }
  } catch (e) {}
  return n;
})()
"""


def _browser_worker(url, wait_s, conn, opts=None):
    """Separate process: open the page in a REAL webview, let its scripts and player run, press
    play, and keep collecting media URLs the whole time.

    Polling repeatedly (rather than looking once at the end) matters: a player may fetch its
    manifest, hand the <video> element a blob: URL and move on, so the evidence is transient."""
    try:
        import webview
    except Exception as e:
        try:
            conn.send({'error': 'pywebview not installed: {}'.format(e)})
            conn.close()
        except Exception:
            pass
        return
    opts = opts or {}
    do_click = opts.get('click', True)
    show = opts.get('show', True)
    profile = opts.get('profile') or None
    cookies = opts.get('cookies') or []
    cookie_txt = opts.get('cookie_txt') or None
    login_only = opts.get('login', False)
    holder = {'urls': [], 'all': [], 'title': None, 'cookies': 'none'}

    def _merge(raw):
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        for key in ('urls', 'all'):
            for u in (data.get(key) or []):
                if u not in holder[key]:
                    holder[key].append(u)
        if data.get('title') and not holder['title']:
            holder['title'] = data['title']

    def _on_start(window):
        import time as _t
        # Cookies must exist BEFORE the real page loads, otherwise the site has already decided
        # you are a guest. So the window starts on the site's root, cookies go in, and only then
        # does it navigate to the page we actually care about.
        if cookies:
            _t.sleep(1.2)                    # let the placeholder page settle
            holder['cookies'] = _browser_apply_cookies(window, cookies, url, cookie_txt)
            try:
                window.load_url(url)
            except Exception:
                pass
            _t.sleep(1.5)

        if login_only:
            # Manual-login mode: leave the window open. webview.start() returns when the user
            # closes it, and the profile keeps whatever session was established.
            return

        deadline = _t.time() + max(4, wait_s) * 3
        played = 0
        while _t.time() < deadline:
            try:
                window.evaluate_js(_BROWSER_HOOK_JS)      # idempotent; also covers new frames
            except Exception:
                pass
            if do_click:
                try:
                    played += int(window.evaluate_js(_BROWSER_PLAY_JS) or 0)
                except Exception:
                    pass
            try:
                _merge(window.evaluate_js(_BROWSER_COLLECT_JS))
            except Exception:
                pass
            # Stop early once something conclusive turned up, but always give the page at least
            # the configured wait so a slow player isn't cut off.
            if holder['urls'] and _t.time() > deadline - max(4, wait_s) * 2:
                break
            _t.sleep(0.5)
        holder['played'] = played
        try:
            window.destroy()
        except Exception:
            pass

    try:
        pu = urlparse(url)
        start_url = f"{pu.scheme}://{pu.netloc}/" if cookies and pu.netloc else url
        w = webview.create_window('videoloader scan', url=start_url, hidden=not show,
                                  width=1280, height=800)
        kwargs = {}
        if profile:
            # A persistent profile keeps a login you performed in this window, so the next run
            # sees the real stream instead of a paywall.
            kwargs = {'private_mode': False, 'storage_path': profile}
        try:
            webview.start(_on_start, (w,), **kwargs)
        except TypeError:
            webview.start(_on_start, (w,))     # older pywebview without those options
    except Exception as e:
        try:
            conn.send({'error': str(e)})
            conn.close()
        except Exception:
            pass
        return
    try:
        conn.send({'result': json.dumps({'urls': holder['urls'], 'all': holder['all'],
                                         'title': holder['title'], 'cookies': holder['cookies'],
                                         'cookie_notes': holder.get('cookie_notes') or [],
                                         'played': holder.get('played', 0)})})
        conn.close()
    except Exception:
        pass


# Requests the player made that MIGHT be a manifest even though the address says nothing. These
# are settled by fetching a few KB and looking at what actually comes back, so an endpoint like
# /api/playback?type=dash is recognised while /api/user/profile is not.
_NON_MANIFEST_EXT_RE = re.compile(
    r'\.(?:jpe?g|png|gif|webp|avif|svg|ico|css|js|mjs|woff2?|ttf|otf|eot|map|wasm|m4s|ts|aac|'
    r'vtt|srt|html?|php)(?:[?#]|$)', re.I)
_MANIFEST_HINT_RE = re.compile(
    r'(?:/|[?&=])(?:manifest|master|playlist|index|stream|streams|playback|dash|hls|mpd|m3u8|'
    r'smil|media|source|video)(?:[/.?&=#]|$)|\.isml?/', re.I)


def _looks_probeable(u):
    """True if a URL is worth spending one small ranged GET on."""
    if not u or not u.lower().startswith(('http://', 'https://')):
        return False
    if _NON_MANIFEST_EXT_RE.search(u):
        return False
    if re.search(r'\.(?:mp4|webm|mov|m4v)(?:[?#]|$)', u, re.I):
        return False        # already handled as a direct file
    return bool(_MANIFEST_HINT_RE.search(u))


def _sniff_manifest(head, content_type=''):
    """Decide what a response actually is from its first bytes plus Content-Type."""
    ct = (content_type or '').lower()
    if 'dash+xml' in ct:
        return 'mpd'
    if 'mpegurl' in ct:
        return 'hls'
    txt = head.decode('utf-8', 'ignore') if isinstance(head, bytes) else (head or '')
    txt = txt[:2048].lstrip('\ufeff \t\r\n')
    if txt.startswith('#EXTM3U'):
        return 'hls'
    if re.match(r'<MPD\b', txt, re.I) or \
            (re.match(r'<\?xml', txt, re.I) and re.search(r'<MPD\b', txt[:1200], re.I)):
        return 'mpd'
    return None


def _confirm_manifest_urls(urls, session, headers, verbose, budget):
    """Fetch the first few KB of each plausible URL and return the confirmed [(kind, url)]."""
    cands = [u for u in dict.fromkeys(urls or []) if _looks_probeable(u)]
    if not cands or budget <= 0:
        return []

    def _rank(u):
        low = u.lower()
        return -sum(w for kw, w in (('manifest', 4), ('dash', 4), ('mpd', 4), ('.ism', 3),
                                    ('master', 3), ('playlist', 3), ('m3u8', 3), ('hls', 2),
                                    ('playback', 2), ('stream', 1)) if kw in low)

    cands = sorted(cands, key=_rank)
    total = len(cands)
    cands = cands[:budget]
    if verbose:
        if total > len(cands):
            tqdm.write(f"[DBG] --scan-browser: probing the {len(cands)} most likely of {total} "
                       f"candidate request(s) — raise SCAN_BROWSER_PROBE_MAX to try them all")
        else:
            tqdm.write(f"[DBG] --scan-browser: probing {len(cands)} request(s) for a manifest")
    found, lock = [], threading.Lock()

    def _one(u):
        try:
            h = dict(headers or {})
            h.setdefault('User-Agent', USER_AGENT)
            h['Range'] = 'bytes=0-8191'
            h['Accept'] = '*/*'
            with connection_slot():
                with session.get(u, headers=h, stream=True, allow_redirects=True,
                                 timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT)) as r:
                    if r.status_code not in (200, 206):
                        return
                    ct = r.headers.get('Content-Type', '')
                    if 'text/html' in ct.lower():
                        return
                    chunk = next(r.iter_content(chunk_size=8192), b'') or b''
            kind = _sniff_manifest(chunk, ct)
            if kind:
                if verbose:
                    tqdm.write(f"[DBG] --scan-browser: confirmed {kind.upper()} at {u}")
                with lock:
                    found.append((kind, u))
        except (requests.RequestException, StopIteration, ValueError, OSError):
            return

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(len(cands), SCAN_PAGE_WORKERS))) as ex:
        list(ex.map(_one, cands))
    return found


def _cookies_for_browser(session):
    """The session's cookies as plain dicts, so they can be handed to the webview process.

    The webview keeps its OWN cookie store, entirely separate from the requests session, so
    without this the browser opens logged out no matter what --cookies loaded."""
    out = []
    try:
        for c in session.cookies:
            if not getattr(c, 'name', None):
                continue
            rest = getattr(c, '_rest', None) or {}
            # requests puts a placeholder {'HttpOnly': None} on EVERY cookie it creates, so the
            # key merely existing proves nothing — only a non-None value marks a real HttpOnly.
            httponly = any(k.lower() == 'httponly' and rest[k] is not None for k in rest)
            out.append({
                'name': c.name,
                'value': c.value or '',
                'domain': (c.domain or '').lower(),
                'path': c.path or '/',
                'secure': bool(c.secure),
                'expires': int(c.expires) if getattr(c, 'expires', None) else 0,
                # An HttpOnly cookie can never be set from JavaScript, which is exactly why the
                # JS fallback alone cannot carry a login session.
                'httponly': httponly,
            })
    except Exception:
        return []
    return out


def _write_netscape_cookies(cookies, path):
    """Write cookies as a Netscape cookies.txt (the format WebKitGTK can load directly)."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# Generated by videoloader_dir.py for --scan-browser\n\n")
            for c in cookies:
                dom = c['domain'] or ''
                sub = 'TRUE' if dom.startswith('.') else 'FALSE'
                f.write("\t".join([dom, sub, c['path'] or '/',
                                   'TRUE' if c['secure'] else 'FALSE',
                                   str(c['expires'] or 0), c['name'], c['value']]) + "\n")
        return path
    except OSError:
        return None


# Setting cookies in the webview has no single portable API, so three routes are tried in order
# of how much they can do. Only the native ones can carry an HttpOnly session cookie — which is
# precisely the kind a login uses.
_BROWSER_SET_COOKIE_JS = r"""
(function(list){
  var n = 0;
  try {
    JSON.parse(list).forEach(function(c){
      try {
        var s = c.name + '=' + c.value + '; path=' + (c.path || '/');
        if (c.domain) s += '; domain=' + c.domain;
        if (c.expires) s += '; expires=' + new Date(c.expires * 1000).toUTCString();
        if (c.secure) s += '; secure';
        document.cookie = s;
        n++;
      } catch (e) {}
    });
  } catch (e) {}
  return n;
})(%s)
"""


def _cookie_applies(domain, host):
    """True if a cookie for `domain` would be sent to `host` (its own domain or a parent of it)."""
    d = (domain or '').lstrip('.').lower()
    h = (host or '').lower()
    return bool(d) and (h == d or h.endswith('.' + d))


def _browser_apply_cookies(window, cookies, page_url, cookie_txt, verbose_note=None):
    """Push the session's cookies into the webview.

    Returns {'how', 'complete', 'set', 'relevant', 'lost'} — `complete` is False only when a cookie
    THIS SITE needs could not be carried, which is the only case worth warning about."""
    if not cookies:
        return {'how': "none", 'complete': True, 'set': 0, 'relevant': 0, 'lost': 0}

    # 1) WebView2 (Windows) — its CookieManager accepts HttpOnly cookies properly.
    try:
        native = getattr(window, 'native', None)
        core = getattr(native, 'CoreWebView2', None) if native is not None else None
        cm = getattr(core, 'CookieManager', None) if core is not None else None
        if cm is not None:
            done = 0
            for c in cookies:
                try:
                    ck = cm.CreateCookie(c['name'], c['value'], c['domain'], c['path'] or '/')
                    ck.IsSecure = bool(c['secure'])
                    ck.IsHttpOnly = bool(c['httponly'])
                    if c['expires']:
                        ck.Expires = float(c['expires'])
                    cm.AddOrUpdateCookie(ck)
                    done += 1
                except Exception:
                    continue
            if done:
                return {'how': f"WebView2 cookie store ({done} cookie(s), HttpOnly included)",
                        'complete': True, 'set': done, 'relevant': len(cookies), 'lost': 0}
        elif verbose_note is not None:
            verbose_note.append("WebView2 CookieManager not reachable through this pywebview build "
                                "(window.native has no CoreWebView2) — falling back")
    except Exception as e:
        if verbose_note is not None:
            verbose_note.append(f"WebView2 cookie store unavailable ({type(e).__name__})")

    # 2) WebKitGTK (Linux) — its cookie manager reads a Netscape cookies.txt straight off disk.
    try:
        if cookie_txt and os.path.exists(cookie_txt):
            import gi
            gi.require_version('WebKit2', '4.0')
            from gi.repository import WebKit2
            ctx = WebKit2.WebContext.get_default()
            cm = ctx.get_cookie_manager()
            cm.set_persistent_storage(cookie_txt, WebKit2.CookiePersistentStorage.TEXT)
            return {'how': "WebKitGTK persistent cookie file (HttpOnly included)",
                    'complete': True, 'set': len(cookies), 'relevant': len(cookies), 'lost': 0}
    except Exception:
        pass

    # 3) Anything else: document.cookie. Only cookies belonging to THIS site can be set that way —
    #    the browser silently drops an attempt to set a cookie for someone else's domain, so pushing
    #    a whole 3000-entry export through would be wasted work reported as a meaningless count.
    try:
        host = (urlparse(page_url).hostname or '').lower()
        mine = [c for c in cookies if _cookie_applies(c['domain'], host)]
        usable = [c for c in mine if not c['httponly']]
        lost = [c for c in mine if c['httponly']]
        if not mine:
            return {'how': f"document.cookie — none of the {len(cookies)} cookie(s) belong to {host}",
                    'complete': False, 'set': 0, 'relevant': 0, 'lost': 0}
        n = 0
        if usable:
            n = window.evaluate_js(_BROWSER_SET_COOKIE_JS % json.dumps(json.dumps(usable))) or 0
        return {'how': f"document.cookie ({n} of {len(mine)} cookie(s) for {host})",
                'complete': not lost, 'set': int(n), 'relevant': len(mine), 'lost': len(lost)}
    except Exception as e:
        return {'how': f"failed ({type(e).__name__})", 'complete': False,
                'set': 0, 'relevant': 0, 'lost': 0}


def _browser_profile_dir():
    """Where the webview keeps its profile (cookies/logins) between runs."""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    d = os.path.join(base, '.videoloader_browser')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return d


def _browser_collect_media(page_url, verbose, wait_s=None, session=None, login_only=False):
    """Load a page in a real webview and return (media_urls, page_title, other_requests).

    `session` supplies the cookies loaded from --cookies / an auto-detected cookie file, so the
    browser is logged in the same way the rest of the script is. `other_requests` is everything
    else the page fetched that wasn't obviously an asset — a manifest served from an
    extension-less address hides in there, so the caller confirms those by reading their first
    bytes. Requires pywebview (same engine used for YouTube signatures)."""
    if wait_s is None:
        wait_s = max(1, int(SCAN_BROWSER_WAIT or 6))
    if not _yt_pywebview_available():
        print("[ERROR] --scan-browser needs pywebview. Install it with:  pip install pywebview")
        return [], None, []
    import multiprocessing as _mp
    profile = _browser_profile_dir() if SCAN_BROWSER_PROFILE else None
    cookies = _cookies_for_browser(session) if session is not None else []
    cookie_txt = None
    if cookies:
        cookie_txt = _write_netscape_cookies(
            cookies, os.path.join(profile or tempfile.gettempdir(), 'videoloader_cookies.txt'))
        _host = (urlparse(page_url).hostname or '').lower()
        _mine = [c for c in cookies if _cookie_applies(c['domain'], _host)]
        _http = sum(1 for c in _mine if c['httponly'])
        print(f"[INFO] --scan-browser: passing {len(cookies)} cookie(s) to the browser; "
              f"{len(_mine)} of them belong to {_host}"
              + (f" ({_http} HttpOnly)" if _http else "") + ".")
    elif session is not None:
        print("[INFO] --scan-browser: no cookies loaded — use --cookies FILE (or drop a cookie "
              ".json next to the script) if the page needs a login.")
    opts = {'click': bool(SCAN_BROWSER_CLICK), 'show': bool(SCAN_BROWSER_SHOW) or login_only,
            'profile': profile, 'cookies': cookies, 'cookie_txt': cookie_txt,
            'login': bool(login_only)}
    if opts['show'] and not login_only:
        print("[INFO] --scan-browser: a browser window will open — you can log in or press play "
              "in it if the page needs that; it closes on its own.")
    got = None
    try:
        ctx = _mp.get_context('spawn')
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_browser_worker, args=(page_url, wait_s, child, opts),
                           daemon=True)
        proc.start()
        # The worker keeps polling for up to 3x the wait, so allow for that plus startup.
        budget = 3600 if login_only else max(4, wait_s) * 3 + 60
        got = parent.recv() if parent.poll(budget) else None
        proc.join(5)
        if proc.is_alive():
            proc.terminate()
    except Exception as e:
        if verbose:
            print(f"[WARN] --scan-browser webview failed: {e}")
        return [], None, []
    if not got or got.get('error') or not got.get('result'):
        if got and got.get('error'):
            print(f"[WARN] --scan-browser: {got['error']}")
        return [], None, []
    try:
        data = json.loads(got['result'])
    except (ValueError, TypeError):
        return [], None, []
    ck = data.get('cookies')
    if cookies and isinstance(ck, dict):
        if verbose:
            for note in (data.get('cookie_notes') or []):
                tqdm.write(f"[DBG] --scan-browser: {note}")
        # Only a cookie THIS SITE needs going missing is worth a warning. Cookies for other
        # domains in the export are irrelevant here and were never going to be set anyway.
        if ck.get('complete'):
            print(f"[INFO] --scan-browser: cookies applied via {ck.get('how')}.")
        else:
            lost = ck.get('lost') or 0
            print(f"{CLR.YELLOW}[WARN]{CLR.RESET} --scan-browser: cookies applied via "
                  f"{ck.get('how')}"
                  + (f"; {lost} HttpOnly cookie(s) for this site could NOT be set" if lost else "")
                  + ".")
            print("       If the page turns out to be logged out, run --browser-login once: it "
                  "opens the site so you can sign in, and the session is remembered afterwards.")
    if verbose:
        tqdm.write(f"[DBG] --scan-browser: {data.get('played', 0)} play attempt(s), "
                   f"{len(data.get('urls') or [])} media hit(s), "
                   f"{len(data.get('all') or [])} other request(s) observed")
    return (list(dict.fromkeys(data.get('urls') or [])),
            (data.get('title') or None),
            list(dict.fromkeys(data.get('all') or [])))


def _browser_scan_items(page_url, session, verbose, wait_s=None):
    """One page's worth of --scan-browser discovery: open it in the real browser, take what the
    DOM and the player's own requests reveal, and confirm the ambiguous ones by their content.

    Returns discovery items shaped like scan_page_for_media()'s, so both callers (--scan-browser
    and --davka-browser) go through exactly the same logic."""
    items = []
    burls, btitle, bother = _browser_collect_media(page_url, verbose, wait_s=wait_s,
                                                   session=session)
    if verbose:
        tqdm.write(f"[DBG] browser collected {len(burls)} candidate URL(s):")
        for u in burls:
            print(f"      {u}")
        # The manifest may well be sitting in the "other" pile with nothing in its address to
        # give it away, so with -v show that pile in full too — it is the only way to tell
        # "the player never fetched it" from "we filtered it out".
        if bother:
            probeable = [u for u in bother if _looks_probeable(u)]
            tqdm.write(f"[DBG] browser also observed {len(bother)} other request(s); "
                       f"{len(probeable)} of them look worth probing:")
            for u in probeable:
                print(f"      ? {u}")
            for u in bother:
                if u not in probeable:
                    print(f"        {u}")

    def _add(kind, url, label):
        if not any(i['url'] == url for i in items):
            items.append({'kind': kind, 'url': url, 'label': label, 'name': btitle,
                          'page': page_url})

    for u in burls:
        cls = _classify_media_url(u)
        if cls:
            _add(cls[0], cls[1], cls[2] + ' (browser)')
    # The player may have fetched its manifest from an address containing no .mpd/.m3u8 at all.
    # Those look like any other request, so they are settled by their content.
    if bother and SCAN_BROWSER_PROBE_MAX:
        pg = urlparse(page_url)
        hdrs = {'User-Agent': USER_AGENT, 'Referer': page_url,
                'Origin': f"{pg.scheme}://{pg.netloc}"}
        known = {i['url'] for i in items}
        for k, cu in _confirm_manifest_urls([u for u in bother if u not in known],
                                            session, hdrs, verbose, SCAN_BROWSER_PROBE_MAX):
            _add(k, cu, ('DASH stream (.mpd)' if k == 'mpd' else 'HLS stream (.m3u8)')
                 + ' (browser request)')
    return items


def _direct_label(url):
    """Label for a plain video file, flagging the short teaser clips many sites embed next to the
    real (player-loaded) stream, so they aren't mistaken for the episode."""
    if re.search(r'(?:^|[/_-])(?:preview|trailer|teaser|sample)', urlparse(url).path, re.I):
        return 'Direct video file (preview clip?)'
    return 'Direct video file'


def _scan_diagnose(page_url, html, found=None):
    """Explain an empty --scan: show what was actually fetched and where (if anywhere) the source
    mentions a manifest, so a JavaScript-built player or a login wall is easy to tell apart."""
    found = found or []
    if found:
        print(f"[INFO] --scan: only {len(found)} non-stream file(s) found here (no .mpd/.m3u8) — "
              "a short preview clip usually means the real stream is loaded by the player.")
    print(f"[INFO] --scan: fetched {len(html)} character(s) from {page_url}")
    low = html.lower()
    if any(k in low for k in ('name="password"', "name='password'", 'type="password"',
                              'sign in', 'log in', 'prihlaseni', 'prihlasit')):
        print("[INFO] --scan: that looks like a login page — pass your cookies with --cookies "
              "(or drop a cookie .json next to the script) so the real page is fetched.")
    hits, seen_ctx = [], set()
    for m in re.finditer(r'(?i)(\.mpd|\.m3u8|\.ism|manifest|dash|playlist)', html):
        s = max(0, m.start() - 90)
        ctx = ' '.join(html[s:m.start() + 110].split())
        if ctx in seen_ctx:
            continue
        seen_ctx.add(ctx)
        hits.append(ctx)
        if len(hits) >= 6:
            break
    if hits:
        print("[INFO] --scan: the source mentions a stream here, but not as a URL the scanner "
              "could use:")
        for h in hits:
            print(f"        ...{h[:170]}...")
        print("[INFO] --scan: if the address is assembled in JavaScript, retry with --scan-browser.")
    else:
        print("[INFO] --scan: the source contains no stream reference at all, so the player builds "
              "it at runtime — retry the same URL with --scan-browser.")


def scan_page_for_media(page_url, session, verbose, _depth=0, _title=None, follow_links=False,
                        _seen=None, _is_sub=False):
    """Fetch any page and discover downloadable media on it, independent of the URL's shape:
    YouTube/Vimeo/Twitch/Drive/Dropbox embeds or links, plus direct .mp4/.m3u8/.webm/.mov URLs.
    Follows unknown player <iframe>s one level deep. With follow_links=True it also treats the page
    as an index: it visits every same-site <a href> link once (episode pages) and scans each too.
    Direct/HLS items carry a 'name' taken from the page title so the output isn't called
    'master.mp4'. Returns a de-duplicated list of {'kind','url','label','name'}."""
    if _seen is None:
        _seen = set()
    _seen.add(page_url.split('#')[0])
    try:
        r = session.get(page_url, headers={'User-Agent': USER_AGENT,
                                            'Accept-Language': 'en-US,en;q=0.9'},
                        timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        html = r.text
    except requests.RequestException as e:
        if _depth == 0 and not _is_sub:
            print(f"[ERROR] --scan: could not fetch the page: {e}")
        return []
    title = _title or _page_title(html)     # outer page's title wins (passed down into iframes)
    text = html.replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
    found, seen, all_urls = [], set(), set()

    def add(kind, url, label, name=None, page=None):
        if not url or (kind, url) in seen:
            return
        # A known-source URL (added earlier) that also ends in .mp4 shouldn't be re-added as a
        # raw direct/HLS download.
        if kind in ('direct', 'hls') and url in all_urls:
            return
        seen.add((kind, url))
        all_urls.add(url)
        found.append({'kind': kind, 'url': url, 'label': label, 'name': name,
                      'page': page or page_url})

    # The URL may BE the stream: pointing --scan straight at a .mpd/.m3u8 fetches the manifest
    # itself, so scanning it as a web page finds nothing. Detect that (by URL shape, or because the
    # body starts with an MPD/HLS manifest) and hand the manifest back as the one item.
    _self = _classify_media_url(page_url)
    if not _self:
        _head = html[:800].lstrip()
        if _head.startswith('#EXTM3U'):
            _self = ('hls', page_url, 'HLS stream (.m3u8)')
        elif '<MPD' in _head:
            _self = ('mpd', page_url, 'DASH stream (.mpd)')
    if _self and _self[0] in ('mpd', 'hls', 'direct'):
        _kind, _url, _label = _self
        _base = os.path.basename(urlparse(page_url).path.rstrip('/')) or ''
        if _base in ('', '.mpd', '.m3u8', 'manifest', 'Manifest') or _base.startswith('manifest('):
            # '<asset>.ism/.mpd' -> use the asset name, not the bare extension
            _parts = [p for p in urlparse(page_url).path.split('/') if p]
            _base = re.sub(r'\.isml?$', '', _parts[-2], flags=re.I) if len(_parts) > 1 else 'video'
        add(_kind, _url, _label, name=safe_filename(title or _base or 'video', 'video'))
        return found

    for m in re.finditer(r'(?:youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|v/|shorts/|live/)'
                         r'|youtu\.be/)([0-9A-Za-z_-]{11})', text):
        add('url', f'https://www.youtube.com/watch?v={m.group(1)}', f'YouTube {m.group(1)}')
    for m in re.finditer(r'(?:player\.)?vimeo\.com/(?:video/)?(\d{6,})', text):
        add('url', f'https://vimeo.com/{m.group(1)}', f'Vimeo {m.group(1)}')
    for m in re.finditer(r'twitch\.tv/videos/(\d+)', text):
        add('url', f'https://www.twitch.tv/videos/{m.group(1)}', f'Twitch VOD {m.group(1)}')
    for m in re.finditer(_STREAMABLE_RE, text, re.I):
        if m.group(1).lower() not in _STREAMABLE_RESERVED:
            add('url', f'https://streamable.com/{m.group(1)}', f'Streamable {m.group(1)}')
    for m in re.finditer(r'drive\.google\.com/file/d/([0-9A-Za-z_-]+)', text):
        add('url', f'https://drive.google.com/file/d/{m.group(1)}/view', f'Drive {m.group(1)}')
    for m in re.finditer(r'https?://(?:www\.)?dropbox\.com/(?:s|scl/fi)/[^\s"\'<>\\]+', text):
        add('url', m.group(0), 'Dropbox file')
    for m in re.finditer(r'https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', text):
        add('hls', m.group(0), 'HLS stream (.m3u8)', name=title)
    for m in re.finditer(r'https?://[^\s"\'<>\\]+?\.mpd[^\s"\'<>\\]*', text):
        add('mpd', m.group(0), 'DASH stream (.mpd)', name=title)
    for m in re.finditer(r'https?://[^\s"\'<>\\]+?\.(?:mp4|webm|mov|m4v)(?:\?[^\s"\'<>\\]*)?', text):
        add('direct', m.group(0), _direct_label(m.group(0)), name=title)
    # Relative and protocol-relative sources: pages served by a local/origin server usually embed
    # src="/0366/3671/movie.ism/.mpd" instead of a full URL, so resolve those against the page.
    for m in re.finditer(r'''["']\s*((?://|/|\.{1,2}/)[^\s"'<>\\]{3,}?)\s*["']''', text):
        cand = m.group(1).replace('&amp;', '&')
        if not _MEDIA_PATH_RE.search(cand):
            continue
        full = urljoin(page_url, cand)
        hit = _classify_media_url(full)
        if hit:
            kind, url_, label = hit
            add(kind, url_, label, name=title)

    # Follow unknown player iframes one level deep (known hosts are already captured above).
    if _depth < 1:
        known = ('youtube', 'youtu.be', 'vimeo', 'twitch.tv', 'drive.google', 'dropbox', 'streamable.com')
        iframes, done = [], 0
        for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html):
            src = m.group(1).replace('&amp;', '&')
            if src.startswith('//'):
                src = 'https:' + src
            if src.startswith('http') and not any(h in src.lower() for h in known):
                iframes.append(src)
        for src in iframes:
            if done >= 6:                       # safety cap on how many iframes we chase
                break
            done += 1
            if verbose:
                tqdm.write(f"[DBG] --scan: following iframe {src}")
            for s in scan_page_for_media(src, session, verbose, _depth + 1, _title=title):
                add(s['kind'], s['url'], s['label'] + ' (iframe)', name=s.get('name'),
                    page=s.get('page'))

    # Index/episode-list mode: visit same-site <a href> links and scan each page too, so a page
    # that only LINKS to episodes (rather than embedding the videos) is fully mined. follow_links=
    # True always follows; 'auto' follows when the page is an episode INDEX — it has no stream of
    # its own, OR it clearly lists episodes (>=2 links whose path looks like an episode/watch page,
    # e.g. containing "episode"). Episode-looking links are visited first (priority within the cap).
    if follow_links and _depth == 0:
        base_host = urlparse(page_url).netloc.lower()
        links, lseen = [], set()

        def _consider(href):
            href = (href or '').replace('&amp;', '&').strip()
            if not href or href.lower().startswith(('mailto:', 'tel:', 'javascript:', 'data:', '#')):
                return
            full = urljoin(page_url, href).split('#')[0]
            pu = urlparse(full)
            if pu.scheme not in ('http', 'https') or pu.netloc.lower() != base_host:
                return
            if full in _seen or full in lseen:
                return
            if re.search(r'\.(jpe?g|png|gif|webp|svg|css|js|ico|woff2?|ttf|zip|pdf|rss|xml)(\?|$)',
                         full, re.I):
                return
            lseen.add(full)
            links.append(full)

        for m in re.finditer(r'<a\b[^>]+href=["\']([^"\']+)["\']', html):
            _consider(m.group(1))
        # JS/lazy pages (links appear only on hover): the URL is almost always still in the source —
        # in a data-* attribute, JSON blob, onclick or <script> array, and may be relative. Harvest
        # any episode-looking token from anywhere in the raw HTML so those pages get scanned even
        # without a plain <a href>.
        for m in re.finditer(r'''["']([^"'\s<>]{3,})["']''', html):
            cand = m.group(1)
            if '/' in cand and _EP_LINK_RE.search(cand):
                _consider(cand)
        ep_links = [u for u in links if _EP_LINK_RE.search(urlparse(u).path)]
        if verbose:
            print(f"[INFO] --scan: {len(links)} same-site link(s), {len(ep_links)} look like "
                  f"episodes:")
            for u in (ep_links or links)[:40]:
                print(f"        {u}")
        _do_follow = (follow_links is True) or (follow_links == 'auto'
                                                and (not found or len(ep_links) >= 2))
        if _do_follow and links:
            others = [u for u in links if u not in set(ep_links)]
            links = (ep_links + others)[:SCAN_LINK_CAP]   # scan episode-looking links first
            _seen.update(links)
            note = f" ({len(ep_links)} look like episodes)" if ep_links else ""
            print(f"[INFO] --scan: index page — scanning {len(links)} linked page(s){note} "
                  f"for media ...")
            bar = make_bar(total=len(links), desc='scanning pages', unit='page', leave=True)
            lock = threading.Lock()

            def _scan_one(u):
                try:
                    return scan_page_for_media(u, session, verbose, _depth=0, _title=None,
                                               follow_links='auto', _seen=_seen, _is_sub=True)
                except Exception:
                    return []

            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max(1, SCAN_PAGE_WORKERS)) as ex:
                futs = [ex.submit(_scan_one, u) for u in links]
                for fut in as_completed(futs):
                    for s in fut.result():
                        add(s['kind'], s['url'], s['label'], name=s.get('name'), page=s.get('page'))
                    with lock:
                        bar.update(1)
            bar.close()
    if (_depth == 0 and not _is_sub
            and not any(f['kind'] in ('mpd', 'hls', 'url') for f in found)):
        _scan_diagnose(page_url, html, found)
    return found


def main(id_or_url: str, output_file: str = None, chunk_size: int = DEFAULT_CHUNK_SIZE,
         num_threads: int = None, verbose: bool = False, cookies_file: str = None,
         folder_workers: int = DEFAULT_FOLDER_WORKERS, recursive: bool = DEFAULT_RECURSIVE,
         max_connections: int = None, use_color: bool = USE_COLOR,
         auto_cookies: bool = AUTO_COOKIES, select: bool = False, auto: bool = False,
         out_dir: str = None, max_height: int = DEFAULT_MAX_HEIGHT, list_only: bool = False,
         ffmpeg_path: str = None, ffmpeg_url: str = None, do_rename: bool = True,
         rename_mode: str = 'ask', return_summary: bool = False, res_scan: bool = False,
         scan_mode: bool = False, browser_scan: bool = False, scan_pick: int = None,
         scan_links: bool = False):
    """Download from Google Drive (file/folder), Dropbox, Vimeo, a Patreon collection, or a
    single Patreon post (any of which may link to Drive/Dropbox and/or host native Vimeo/Mux)."""
    summary = {'ok': False, 'kind': None, 'downloaded': [], 'rename': None, 'error': None,
               'failed': [], 'title': None, 'preview_note': ''}
    global FFMPEG, FFMPEG_DOWNLOAD_URL
    if ffmpeg_path:
        FFMPEG = ffmpeg_path
    if ffmpeg_url is not None:
        FFMPEG_DOWNLOAD_URL = ffmpeg_url
    setup_console(use_color)

    kind, target_id = _classify_input(id_or_url)
    summary['kind'] = kind
    if not scan_mode and kind == 'youtube_playlist' and _yt_video_id(id_or_url):
        print("[INFO] This URL has a playlist (list=...) — using the WHOLE playlist.")
        print("       For just the single video, use the plain watch URL without &list=:")
        print(f'         videoloader_dir.py "https://www.youtube.com/watch?v={_yt_video_id(id_or_url)}"')
    if not scan_mode and res_scan and kind not in ('youtube', 'youtube_playlist'):
        print("[INFO] Listing qualities with a bare --res works only for YouTube right now.")
        print(f"       For {kind}, use --res <height> to cap quality, e.g. --res 720 or --res 1080.")
        summary['error'] = 'res-scan unsupported for this source'
        if return_summary:
            return summary
        return
    if not scan_mode and kind == 'patreon_bad':
        print("[ERROR] That looks like a Patreon URL but I couldn't find a collection or post id.")
        print("        Expected a collection like https://www.patreon.com/collection/122162")
        print("        or a post like https://www.patreon.com/<creator>/posts/<slug>-162557660")
        summary['error'] = "invalid Patreon URL (no collection/post id)"
        if return_summary:
            return summary
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
    with _failed_lock:
        _failed_jobs.clear()
    global _active_collection_title, _patreon_creator
    _active_collection_title = None
    _patreon_creator = None

    # Viki subtitles-only: pull every episode's .vtt straight from Viki's API (subscriber session).
    if SUBS_ONLY and _viki_url_kind(id_or_url):
        _log_source("Viki", "subtitle API (auth_subtitles) -> .srt")
        try:
            _with_out_dir(lambda: _download_viki_subs(id_or_url, session, None, verbose))
        finally:
            session.close()
        summary['ok'] = True
        if return_summary:
            return summary
        return None

    if not scan_mode and _classify_media_url(id_or_url) and \
            _classify_media_url(id_or_url)[0] in ('mpd', 'hls'):
        # A manifest URL passed on its own: treat it like --scan so it is downloaded as a stream
        # (otherwise it would be saved as the few-KB manifest file itself).
        scan_mode = True
        if verbose:
            print("[INFO] That URL is a stream manifest — handling it as --scan.")

    if scan_mode:
        # --follow-links forces link crawling; otherwise --subs-only auto-crawls when the given
        # page is an episode index (no stream of its own) rather than a direct episode page.
        _follow = True if scan_links else ('auto' if SUBS_ONLY else False)
        found = scan_page_for_media(id_or_url, session, verbose, follow_links=_follow)
        if browser_scan:
            print("[INFO] --scan-browser: loading the page in a browser (JS) to find media...")
            for _it in _browser_scan_items(id_or_url, session, verbose):
                if not any(f['url'] == _it['url'] for f in found):
                    found.append(_it)
        if not found:
            print("[INFO] --scan: found no downloadable media on that page.")
            session.close()
            if return_summary:
                return summary
            return None
        # --scan N: deterministically take just the N-th discovered item (1-indexed), in the
        # order they were found — e.g. --scan 2 on a page with several .mpd grabs the second.
        if scan_pick is not None:
            if 1 <= scan_pick <= len(found):
                chosen = found[scan_pick - 1]
                print(f"[INFO] --scan {scan_pick}: {len(found)} item(s) found; taking #{scan_pick} "
                      f"[{chosen['kind']}] {chosen['url']}")
                found = [chosen]
            else:
                print(f"[WARN] --scan {scan_pick}: only {len(found)} item(s) found — index out of "
                      f"range; downloading all instead.")
        # Default prioritisation: if an HLS (.m3u8) stream is present, prefer it (it is adaptive,
        # so it yields the best resolution) and drop the raw direct files, which are usually the
        # same video at a single fixed quality. Known-source embeds (YouTube/Vimeo/...) are kept.
        # With --select the user sees everything and decides, so we skip this there. A specific
        # --scan N pick also skips prioritisation (the user already chose exactly one).
        if not select and scan_pick is None:
            has_hls = any(f['kind'] in ('hls', 'mpd') for f in found)
            has_direct = any(f['kind'] == 'direct' for f in found)
            if has_hls and has_direct:
                dropped = sum(1 for f in found if f['kind'] == 'direct')
                found = [f for f in found if f['kind'] != 'direct']
                print(f"[INFO] --scan: HLS/DASH stream found; preferring it over {dropped} direct "
                      f"file(s) (use --select to choose manually).")
            # If the URLs encode a resolution (e.g. 720P_4000K, 480P_2000K), keep only the best
            # rendition of each stream.
            before = len(found)
            found = _scan_resolution_winners(found)
            if len(found) < before:
                print(f"[INFO] --scan: chose the best resolution from URL hints; dropped "
                      f"{before - len(found)} lower rendition(s) (use --select to choose manually).")
        if select and len(found) > 1:
            # Let the user pick which discovered items to act on (full URLs shown).
            picked = _prompt_file_selection(
                [dict(it, title=f"{it['label']} — {it['url']}") for it in found])
            if not picked:
                print("[INFO] --scan: nothing selected.")
                session.close()
                if return_summary:
                    return summary
                return None
            found = picked
        else:
            verb = "found (listing only)" if list_only else "found"
            print(f"[INFO] --scan {verb} {len(found)} item(s):")
            for it in found:
                print(f"   - {it['label']}: {it['url']}")
        _kw = dict(chunk_size=chunk_size, num_threads=num_threads, verbose=verbose,
                   folder_workers=folder_workers, recursive=recursive,
                   max_connections=max_connections, use_color=use_color, auto_cookies=auto_cookies,
                   select=False, auto=False, out_dir=out_dir, max_height=max_height,
                   list_only=list_only, ffmpeg_path=ffmpeg_path, ffmpeg_url=ffmpeg_url,
                   do_rename=do_rename, rename_mode=rename_mode, res_scan=res_scan, scan_mode=False)
        _scan_used = set()
        if SUBS_ONLY:
            _log_source("Subtitles-only (page scan)", "HLS/DASH text tracks -> .srt")
            # Subtitles-only over a scanned page: gather every HLS/DASH stream, pull subtitle tracks
            # directly (no video download), pick language(s), save as .srt.
            sub_sources = []
            for it in found:
                if it['kind'] not in ('hls', 'mpd'):
                    continue
                pg = urlparse(it.get('page') or id_or_url)
                origin = f"{pg.scheme}://{pg.netloc}" if pg.scheme and pg.netloc else None
                h = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
                if origin:
                    h['Referer'] = origin + '/'
                    h['Origin'] = origin
                ext = '.mpd' if it['kind'] == 'mpd' else '.m3u8'
                page_it = it.get('page') or ''
                slug = os.path.basename(urlparse(page_it).path.rstrip('/')) if page_it else ''
                base = slug or it.get('name') or \
                    os.path.basename(urlparse(it['url']).path).replace(ext, '') or 'video'
                sub_sources.append({'kind': 'mpd' if it['kind'] == 'mpd' else 'hls_url',
                                    'url': it['url'], 'headers': h,
                                    'title': safe_filename(base, 'video')})
            _with_out_dir(lambda: _download_subs_only(sub_sources, session, None, max_height,
                                                      verbose))
            session.close()
            summary['ok'] = True
            if return_summary:
                return summary
            return None
        for it in found:
            try:
                if it['kind'] == 'url':
                    # Known source: reuse the normal pipeline, so --list / --res behave as usual.
                    main(it['url'], output_file=None, cookies_file=cookies_file, **_kw)
                    continue
                # Direct file / HLS: --list or a bare --res just shows it (no variants to pick).
                if list_only or res_scan:
                    print(f"   [{it['kind']}] {it['url']}")
                    continue
                if it['kind'] == 'direct' and (AUDIO_SEL == 'SCAN' or SUB_SEL == 'SCAN'):
                    print(f"   [direct file] {it['url']}")
                    print("   --audio/--sub track selection applies to HLS streams; this is a "
                          "single file with no separate tracks. Re-run without --audio/--sub to "
                          "download it.")
                    continue
                # Headers the CDN expects: browser UA + Referer/Origin of the page the stream was
                # found on (many CDNs reject hot-linked HLS/masters with 403/412 otherwise).
                pg = urlparse(it.get('page') or id_or_url)
                origin = f"{pg.scheme}://{pg.netloc}" if pg.scheme and pg.netloc else None
                hdrs = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
                if origin:
                    hdrs['Referer'] = origin + '/'
                    hdrs['Origin'] = origin
                if it['kind'] == 'direct':
                    _log_source("Direct file (page scan)", "direct download")
                    base = it.get('name') or os.path.basename(urlparse(it['url']).path) or 'video'
                    final = _scan_unique_name(safe_filename(base, 'video'), _scan_used)
                    entry = {'id': it['url'], 'title': final, 'name': final,
                             'direct_url': it['url'], 'headers': hdrs}
                    _with_out_dir(lambda e=entry: download_folder_pooled(
                        [e], session, chunk_size, verbose, label="Direct", conn_cap=16))
                elif it['kind'] in ('hls', 'mpd'):
                    _log_source("DASH (.mpd)" if it['kind'] == 'mpd' else "HLS (.m3u8)",
                                "page scan" + (" + mp4decrypt" if CENC_KEYS else ""))
                    if not ensure_ffmpeg(verbose):
                        print("[WARN] --scan: skipping stream (ffmpeg needed).")
                        continue
                    ext = '.mpd' if it['kind'] == 'mpd' else '.m3u8'
                    base = it.get('name') or \
                        os.path.basename(urlparse(it['url']).path).replace(ext, '') or 'video'
                    final = _scan_unique_name(safe_filename(base, 'video'), _scan_used)

                    def _grab(it=it, final=final, hdrs=hdrs):
                        # DASH, or AES-128/SAMPLE-AES encrypted HLS -> ffmpeg (fetches keys +
                        # decrypts). Plain HLS -> fast parallel path, with an ffmpeg fallback if it
                        # produces nothing (e.g. a protection we didn't detect up front).
                        encrypted = it['kind'] != 'mpd' and _hls_stream_encrypted(it['url'],
                                                                                  session, hdrs)
                        if it['kind'] == 'mpd' or encrypted:
                            if CENC_KEYS and encrypted:
                                print(f"[INFO] --scan: encrypted HLS + {len(CENC_KEYS)} key(s) "
                                      f"provided — decrypting with mp4decrypt (all tracks).")
                                ok, why = _cenc_grab_hls(it['url'], hdrs, session, max_height,
                                                         final, CENC_KEYS, verbose)
                            elif it['kind'] == 'mpd':
                                if CENC_KEYS:
                                    print(f"[INFO] --scan: DASH (.mpd) + {len(CENC_KEYS)} key(s) "
                                          f"— decrypting with mp4decrypt (all tracks).")
                                    ok, why = _cenc_grab_dash(it['url'], hdrs, session, max_height,
                                                              final, CENC_KEYS, verbose)
                                else:
                                    ok, why = _ffmpeg_grab_stream(it['url'], hdrs, final, verbose)
                            else:
                                if CENC_KEYS:
                                    print("[INFO] --scan: you passed --key, but this HLS isn't "
                                          "detected as CENC/SAMPLE-AES encrypted; using ffmpeg "
                                          "(no decrypt). If it IS encrypted, send me the -v master "
                                          "playlist so I can fix detection.")
                                else:
                                    print("[INFO] --scan: encrypted HLS — ffmpeg (clear-key "
                                          "AES-128 only; pass --key KID:KEY for Widevine/CENC).")
                                ok, why = _ffmpeg_grab_hls(it['url'], hdrs, session, max_height,
                                                           final, verbose)
                            if not ok:
                                print(f"[WARN] --scan: could not grab the stream: {why}")
                            return
                        download_hls_pooled([{'source': 'mux', 'master_url': it['url'],
                                              'headers': hdrs, 'title': os.path.splitext(final)[0]}],
                                            session, None, min(max_connections, 16), max_height,
                                            verbose)
                        if not os.path.exists(final):
                            if verbose:
                                tqdm.write("[INFO] --scan: native HLS produced nothing; retrying "
                                           "via ffmpeg (handles encryption).")
                            _ok, _why = _ffmpeg_grab_stream(it['url'], hdrs, final, verbose)
                            if not _ok:
                                print(f"[WARN] --scan: could not grab the stream: {_why}")
                    _with_out_dir(_grab)
            except SystemExit:
                pass
            except Exception as e:
                print(f"[WARN] --scan: failed on {it['url']}: {e}")
        session.close()
        # The scan itself succeeded; per-item problems were reported above.
        summary['ok'] = True
        summary['downloaded'] = _session_downloads_in(rename_dir)
        if return_summary:
            return summary
        return None

    try:
        if kind == 'patreon':
            _log_source("Patreon collection", "Drive + Dropbox + Streamable + native Vimeo/Mux HLS")
            if output_file:
                print("[WARN] -o/--output is ignored for Patreon collections; names come from the posts.")
            process_patreon_collection(target_id, session, chunk_size, num_threads, folder_workers,
                                       recursive, verbose, select=select, out_dir=out_dir,
                                       max_connections=max_connections, max_height=max_height,
                                       list_only=list_only)
        elif kind == 'patreon_post':
            _log_source("Patreon post", "Drive + Dropbox + Streamable + native Vimeo/Mux HLS")
            if output_file:
                print("[WARN] -o/--output is ignored for Patreon posts; names come from the post.")
            process_patreon_post(target_id, session, chunk_size, num_threads, folder_workers,
                                 recursive, verbose, select=select, out_dir=out_dir,
                                 max_connections=max_connections, max_height=max_height,
                                 list_only=list_only)
        elif kind == 'dropbox':
            _log_source("Dropbox", "direct download")
            fname = safe_filename(output_file, 'video') if output_file else \
                dropbox_filename(target_id, 'video')
            durl = normalize_dropbox_url(target_id)
            entry = {'id': durl, 'title': fname, 'name': fname, 'direct_url': durl}
            _with_out_dir(lambda: download_folder_pooled([entry], session, chunk_size, verbose,
                                                         label="Dropbox", conn_cap=DROPBOX_DEFAULT_CONNECTIONS))
        elif kind == 'vidyard':
            title = os.path.splitext(output_file)[0] if output_file else None
            _with_out_dir(lambda: download_vidyard_pooled(
                [{'uuid': target_id, 'title': title}], session, chunk_size, max_connections,
                max_height, verbose))
        elif kind == 'muse':
            _log_source("muse.ai", "DASH + ffmpeg mux")
            if not ensure_ffmpeg(verbose):
                print("[ERROR] muse.ai videos need ffmpeg to mux audio+video.")
                summary['error'] = "ffmpeg unavailable"
                if return_summary:
                    return summary
                sys.exit(1)
            title = os.path.splitext(output_file)[0] if output_file else None
            ok = _with_out_dir(lambda: download_muse_video(target_id, session, None, max_height,
                                                           verbose, password=MUSE_PASSWORD,
                                                           title=title))
            if not ok:
                summary['error'] = "muse.ai download failed"
                if return_summary:
                    return summary
                sys.exit(1)
        elif kind == 'streamable':
            _log_source("Streamable", "direct MP4")
            durl, title, _h = resolve_streamable(target_id, session, verbose)
            if not durl:
                print(f"[ERROR] Could not resolve Streamable video {target_id}.")
                summary['error'] = "streamable resolve failed"
                if return_summary:
                    return summary
                sys.exit(1)
            fname = _streamable_fname(os.path.splitext(output_file)[0] if output_file
                                     else title, target_id)
            entry = {'id': durl, 'title': fname, 'name': fname, 'direct_url': durl,
                     'headers': STREAMABLE_HEADERS, 'streamable_shortcode': target_id}
            _with_out_dir(lambda: download_folder_pooled([entry], session, chunk_size, verbose,
                                                         label="Streamable",
                                                         conn_cap=max_connections))
        elif kind == 'vimeo':
            _log_source("Vimeo", "native HLS (ffmpeg mux)")
            if not ensure_ffmpeg(verbose):
                print("[ERROR] Vimeo videos are HLS and need ffmpeg to mux audio+video into MP4.")
                summary['error'] = "ffmpeg not available for Vimeo/HLS"
                if return_summary:
                    return summary
                sys.exit(1)
            if output_file:
                target_id['title'] = os.path.splitext(os.path.basename(output_file))[0]
            _with_out_dir(lambda: download_hls_pooled([target_id], session, None,
                                                      max_connections, max_height, verbose))
        elif kind == 'twitch':
            _log_source("Twitch", "native HLS")
            _with_out_dir(lambda: process_twitch(target_id, session, max_connections,
                                                 max_height, verbose, list_only, out_dir,
                                                 output_file=output_file))
        elif kind == 'youtube':
            _log_source("YouTube", "DASH via InnerTube")
            _with_out_dir(lambda: process_youtube(target_id, session, max_connections,
                                                  max_height, verbose, list_only, out_dir,
                                                  output_file=output_file, res_scan=res_scan))
        elif kind == 'youtube_playlist':
            _log_source("YouTube playlist", "DASH via InnerTube")
            if output_file:
                print("[WARN] -o/--output is ignored for playlists; names come from YouTube.")
            _with_out_dir(lambda: process_youtube_playlist(target_id, session, max_connections,
                                                           max_height, verbose, list_only, out_dir,
                                                           res_scan=res_scan))
        elif kind == 'folder':
            _log_source("Google Drive folder", "direct download")
            if output_file:
                print("[WARN] -o/--output is ignored for folders; names come from Drive.")
            _with_out_dir(lambda: process_folder(target_id, session, chunk_size, num_threads,
                                                 folder_workers, recursive, verbose, select=select))
        else:
            _log_source("Google Drive", "direct download")
            if select:
                print("[WARN] --select only applies to folders/collections; ignoring.")
            ok = _with_out_dir(lambda: process_single_video(target_id, session, output_file,
                                                            chunk_size, num_threads, verbose))
            if not ok:
                if not cookies_files:
                    print("Tip: For private files, use --cookies to provide a cookies.txt/JSON export.")
                summary['error'] = "download failed (file not accessible?)"
                if return_summary:
                    return summary
                session.close()
                sys.exit(1)

        # Automatically re-download anything that failed, up to AUTO_RETRIES times, before we
        # decide what (if anything) to record in resume.json.
        if not list_only:
            _still_failed = _retry_loop(session, verbose, chunk_size)
            summary['failed'] = [{'filename': e.get('filename'), 'reason': e.get('reason')}
                                 for e in _still_failed]
    finally:
        session.close()

    # After a successful download, offer to tidy up ONLY the files this run downloaded
    # (never unrelated files already present in the directory). Capture the list BEFORE the
    # rename, since renaming changes the names on disk.
    downloaded = _session_downloads_in(rename_dir)
    summary['downloaded'] = downloaded
    summary['preview_note'] = _report_previews(downloaded, verbose) if downloaded else ''
    if do_rename and not list_only:
        summary['rename'] = offer_strict_rename(rename_dir, downloaded, verbose,
                                                rename_mode=rename_mode)

    # Remove the .temp scratch folder if everything finished (leftover parts from an
    # interrupted run keep it so a re-run can resume).
    _cleanup_temp_dir(rename_dir)

    summary['ok'] = True
    summary['title'] = _active_collection_title or _patreon_creator or None
    if return_summary:
        return summary
    return None

def _ntfy_enabled():
    return bool((NTFY_TOPIC or "").strip())


def notify_ntfy(message, title=None, priority=None, tags=None):
    """Send a push notification via ntfy.sh. No-op when NTFY_TOPIC is empty. Never raises —
    a failed notification must never break a download."""
    if not _ntfy_enabled():
        return False
    url = f"{(NTFY_SERVER or 'https://ntfy.sh').rstrip('/')}/{NTFY_TOPIC.strip()}"
    headers = {}
    if title:
        # HTTP headers must be ASCII; unicode lives in the message body instead.
        headers['Title'] = str(title).encode('ascii', 'replace').decode('ascii')
    if priority:
        headers['Priority'] = str(priority)
    if tags:
        headers['Tags'] = tags
    if (NTFY_TOKEN or "").strip():
        headers['Authorization'] = f"Bearer {NTFY_TOKEN.strip()}"
    try:
        r = requests.post(url, data=(message or "").encode('utf-8'), headers=headers,
                          timeout=(CONNECT_TIMEOUT, META_READ_TIMEOUT))
        if r.status_code >= 400:
            print(f"[WARN] ntfy notification failed (HTTP {r.status_code}).")
            return False
        return True
    except requests.RequestException as e:
        print(f"[WARN] ntfy notification could not be sent: {e}")
        return False


def _notify_url_list_report(results, log_path):
    """Push a detailed ntfy summary of a --url-list run (no-op if ntfy is off)."""
    if not _ntfy_enabled():
        return
    total = len(results)
    ok = sum(1 for r in results if r['ok'] and not r['error'])
    failed_urls = [r for r in results if r['error'] or not r['ok']]
    files = sum(len(r.get('downloaded') or []) for r in results)
    renamed = sum(len((r.get('rename') or {}).get('renames') or []) for r in results)
    failed_files = sum(len(r.get('failed') or []) for r in results)
    problem = [r for r in results if r['error'] or not r['ok'] or r.get('failed')
               or not r.get('downloaded')]

    title = f"videoloader: {ok}/{total} URLs OK, {files} files"
    lines = [f"Finished {ok}/{total} URL(s) OK — {files} downloaded, {renamed} renamed"
             + (f", {failed_files} FAILED" if failed_files else "") + "."]

    for r in results:
        rn = r.get('rename') or {}
        fl = r.get('failed') or []
        if r['error'] or not r['ok']:
            st = "FAILED"
        elif fl:
            st = "PARTIAL"
        elif not r.get('downloaded'):
            st = "NOFILE"
        else:
            st = "OK"
        short = r['url'].split('patreon.com/')[-1].split('?')[0]
        lines.append("")
        lines.append(f"[{r['index']}] {st}  {short}")
        lines.append(f"    {len(r.get('downloaded') or [])} downloaded, "
                     f"{len(rn.get('renames') or [])} renamed")
        if r.get('error'):
            lines.append(f"    error: {r['error']}")
        for fe in fl[:8]:
            lines.append(f"    ✗ {fe.get('filename')}: {fe.get('reason')}")
        if len(fl) > 8:
            lines.append(f"    ...and {len(fl) - 8} more failed")

    lines.append("")
    lines.append(f"Report: {log_path}")

    notify_ntfy("\n".join(lines), title=title,
                priority=("high" if failed_urls or failed_files else "default"),
                tags=("warning" if problem else "white_check_mark"))


def _resume_path():
    return os.path.join(os.getcwd(), RESUME_FILE)


def _load_resume():
    try:
        with open(_resume_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_resume(entries):
    try:
        with open(_resume_path(), 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[WARN] Could not write {RESUME_FILE}: {e}")


def _delete_resume():
    try:
        os.remove(_resume_path())
    except OSError:
        pass


def _entry_resolved(e):
    """True if the failed entry's final file now exists (i.e. it downloaded correctly)."""
    fn = e.get('filename')
    if not fn:
        return False
    p = os.path.join(e.get('out_dir') or os.getcwd(), fn)
    try:
        return os.path.exists(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def _update_resume_after_run():
    """Merge this run's failures with any existing resume.json, drop the ones that are now
    downloaded, and write (or delete) the file. Called once at the end of every run."""
    existing = _load_resume()
    with _failed_lock:
        newly = list(_session_failed)
    kept, seen = [], set()
    for e in existing + newly:
        if not isinstance(e, dict):
            continue
        key = (e.get('out_dir'), e.get('filename'), e.get('kind'))
        if key in seen:
            continue
        seen.add(key)
        if _entry_resolved(e):
            continue                      # downloaded correctly -> remove from the list
        kept.append(e)
    if kept:
        _save_resume(kept)
        print(f"\n[INFO] {len(kept)} file(s) still failed — saved to {RESUME_FILE}.")
        print(f"[INFO] Re-run '{os.path.basename(sys.argv[0])}' with NO arguments to retry them.")
    elif existing or newly:
        _delete_resume()
        print(f"\n[INFO] Everything downloaded — removed {RESUME_FILE}.")


def _build_retry_session(base_kwargs):
    """Set the connection limit and open a cookie session the same way main() does."""
    verbose = base_kwargs.get('verbose', False)
    mc = base_kwargs.get('max_connections')
    global _max_conn_explicit
    _max_conn_explicit = mc is not None
    if mc is None:
        if base_kwargs.get('auto'):
            _t, mc, _l = _auto_settings()
        else:
            mc = DEFAULT_MAX_CONNECTIONS
    set_connection_limit(mc)
    if base_kwargs.get('cookies_file'):
        cookies_files = [base_kwargs['cookies_file']]
    elif base_kwargs.get('auto_cookies', True):
        cookies_files = auto_detect_cookies(verbose)
    else:
        cookies_files = []
    return get_cookies_session(cookies_files), mc


def _run_in_dir(directory, fn):
    """Run fn() with CWD switched to `directory` (created if needed)."""
    os.makedirs(directory or '.', exist_ok=True)
    old = os.getcwd()
    os.chdir(directory or '.')
    try:
        return fn()
    finally:
        os.chdir(old)


def _download_failed_entries(entries, session, verbose, chunk):
    """Re-download a list of failed-job entries once, grouped by kind/dir. Any that fail again
    are re-recorded into _failed_jobs by the engines' finalize()."""
    pooled = defaultdict(list)      # (out_dir, kind) -> [video dicts]
    native = defaultdict(list)      # (out_dir, max_height) -> [stream dicts]
    streamable = defaultdict(list)  # out_dir -> [failed streamable entries]
    vidyard = defaultdict(list)     # out_dir -> [failed vidyard entries]
    for e in entries:
        if not isinstance(e, dict):
            continue
        od = e.get('out_dir') or os.getcwd()
        if e.get('kind') == 'vidyard' and e.get('uuid'):
            vidyard[od].append(e)
        elif e.get('kind') == 'streamable' and e.get('shortcode'):
            streamable[od].append(e)
        elif e.get('kind') in ('drive', 'dropbox') and isinstance(e.get('video'), dict):
            pooled[(od, e['kind'])].append(e['video'])
        elif e.get('kind') == 'native' and isinstance(e.get('stream'), dict):
            native[(od, e.get('max_height', 0))].append(e['stream'])
    for (od, kind_p), videos in pooled.items():
        # A Dropbox retry must keep the same gentle connection cap as the original attempt,
        # otherwise it goes straight back to the full -m budget and gets rate-limited again.
        cap = DROPBOX_DEFAULT_CONNECTIONS if kind_p == 'dropbox' else None
        _run_in_dir(od, lambda videos=videos, cap=cap: download_folder_pooled(
            videos, session, chunk, verbose, label="Retry", conn_cap=cap))
    for od, entries_v in vidyard.items():
        def _retry_vidyard(entries_v=entries_v):
            vids = []
            for e in entries_v:
                url, title, hdrs, h = resolve_vidyard(e['uuid'], session, 0, verbose)
                if not url or '.m3u8' in url:
                    print(f"[WARN] Vidyard {e['uuid']}: could not re-resolve; skipping.")
                    continue
                fn = e.get('filename') or _vidyard_fname(title, e['uuid'])
                vids.append({'id': url, 'title': fn, 'name': fn, 'direct_url': url,
                             'headers': hdrs, 'vidyard_uuid': e['uuid'], 'vidyard_height': h})
            if vids:
                download_folder_pooled(vids, session, chunk, verbose, label="Retry",
                                       conn_cap=_max_connections)
        _run_in_dir(od, _retry_vidyard)
    for od, entries_s in streamable.items():
        def _retry_streamable(entries_s=entries_s):
            vids = []
            for e in entries_s:
                durl, title, _h = resolve_streamable(e['shortcode'], session, verbose)
                if not durl:
                    print(f"[WARN] Streamable {e['shortcode']}: could not re-resolve; skipping.")
                    continue
                fn = e.get('filename') or _streamable_fname(title, e['shortcode'])
                vids.append({'id': durl, 'title': fn, 'name': fn, 'direct_url': durl,
                             'headers': STREAMABLE_HEADERS, 'streamable_shortcode': e['shortcode']})
            if vids:
                download_folder_pooled(vids, session, chunk, verbose, label="Retry",
                                       conn_cap=_max_connections)
        _run_in_dir(od, _retry_streamable)
    for (od, mh), streams in native.items():
        if not ensure_ffmpeg(verbose):
            print("[ERROR] ffmpeg is required for native videos; skipping those on retry.")
            continue
        _run_in_dir(od, lambda streams=streams, mh=mh: download_hls_pooled(
            streams, session, None, _max_connections, mh, verbose))


def _retry_loop(session, verbose, chunk):
    """Automatically re-download whatever is in _failed_jobs, up to AUTO_RETRIES times. Files
    that succeed drop out; whatever still fails is moved into _session_failed (for resume.json)."""
    for attempt in range(1, max(0, AUTO_RETRIES) + 1):
        with _failed_lock:
            pending = list(_failed_jobs)
            _failed_jobs.clear()
        pending = [e for e in pending if isinstance(e, dict) and not _entry_resolved(e)]
        if not pending:
            return []
        print(f"\n[INFO] Auto-retry {attempt}/{AUTO_RETRIES}: re-downloading "
              f"{len(pending)} failed file(s)...")
        _download_failed_entries(pending, session, verbose, chunk)
    # Retries exhausted: whatever is still failing goes to the session accumulator.
    with _failed_lock:
        remaining = [e for e in _failed_jobs if isinstance(e, dict) and not _entry_resolved(e)]
        _failed_jobs.clear()
        _session_failed.extend(remaining)
    if remaining:
        print(f"[INFO] {len(remaining)} file(s) still failing after {AUTO_RETRIES} auto-retries.")
    return remaining


def run_resume(base_kwargs):
    """If a resume.json exists, re-download the failed files it lists (with auto-retries),
    then update/remove it. Returns True if a resume.json was found and processed."""
    entries = _load_resume()
    if not entries:
        return False

    print(f"[INFO] Found {RESUME_FILE} with {len(entries)} failed file(s) from a previous run. "
          f"Retrying...")
    setup_console(base_kwargs.get('use_color', True))
    try:
        session, _mc = _build_retry_session(base_kwargs)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] Failed to load cookies for retry: {e}")
        sys.exit(1)

    verbose = base_kwargs.get('verbose', False)
    chunk = base_kwargs.get('chunk_size', DEFAULT_CHUNK_SIZE)
    try:
        with _failed_lock:
            _failed_jobs.clear()
        _download_failed_entries(entries, session, verbose, chunk)
        _retry_loop(session, verbose, chunk)
    finally:
        session.close()

    _update_resume_after_run()
    return True


def _write_url_list_log(results, out_dir, started, label="--url-list"):
    """Write a plain-text report of a batch run and return its path."""
    directory = os.path.abspath(out_dir) if out_dir else os.getcwd()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        directory = os.getcwd()
    path = os.path.join(directory, f"videoloader_report_{started.strftime('%Y%m%d_%H%M%S')}.log")
    ok_n = sum(1 for r in results if r['ok'] and not r['error'])
    lines = [
        f"videoloader_dir.py  {label} report",
        f"Started : {started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Result  : {ok_n}/{len(results)} URL(s) succeeded",
        "=" * 70,
    ]
    for r in results:
        failed = r.get('failed') or []
        if r['error'] or not r['ok']:
            status = "FAILED"
        elif r.get('skipped'):
            status = "SKIP  "   # already on disk before this run — nothing was downloaded
        elif failed:
            status = "PARTIAL"
        elif not (r.get('downloaded')):
            status = "NOFILE"   # completed without error, but no new files (all failed/skipped)
        else:
            status = "OK    "
        lines.append("")
        # Identify the entry by its NAME when it has one (that is what the user wrote in the
        # list); the address goes on the line below so nothing is lost.
        name = (r.get('title') or '').strip()
        lines.append(f"[{r['index']}] {status}  {name or r['url']}")
        if name:
            lines.append(f"      url : {r['url']}")
        lines.append(f"      kind: {r.get('kind')}   time: {r.get('seconds', 0.0):.1f}s")

        dl = r.get('downloaded') or []
        lines.append(f"      downloaded: {len(dl)} file(s)")
        if r.get('preview_note'):
            lines.append(f"      WARNING: {r['preview_note']}")
        rn = r.get('rename') or {}
        renames = rn.get('renames') or []
        renamed_olds = {o for o, _n in renames}
        if renames:
            lines.append(f"      renamed: {len(renames)} file(s)  (original -> new)")
            width = min(60, max((len(o) for o, _n in renames), default=0))
            for o, n in renames:
                lines.append(f"          {o:<{width}}  ->  {n}")
        # List downloaded files that were NOT part of the rename (so nothing is hidden).
        rest = [name for name in dl if name not in renamed_olds]
        if renames:
            if rest:
                lines.append("      unchanged:")
                for name in rest:
                    lines.append(f"          - {name}")
        else:
            for name in dl:
                lines.append(f"          - {name}")

        if rn:
            lines.append(f"      rename: {rn.get('status')} "
                         f"(changed={rn.get('changed', 0)}, conflicts={rn.get('conflicts', 0)}, "
                         f"applied={rn.get('applied', 0)})")
            for o, n in (rn.get('conflict_names') or []):
                lines.append(f"          [conflict, skipped] {o}  ->  {n}")
        else:
            lines.append("      rename: (none)")

        if failed:
            lines.append(f"      failed: {len(failed)} file(s)")
            for fe in failed:
                fn = fe.get('filename') or '(unknown)'
                rs = fe.get('reason') or 'failed'
                lines.append(f"          - {fn}: {rs}")
        if r.get('error'):
            lines.append(f"      error: {r['error']}")
    text = "\n".join(lines) + "\n"
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    except OSError as e:
        print(f"[WARN] Could not write report log: {e}")
        return "(not written)"
    return path


def _url_list_is_json(path, text=None):
    """A list file counts as JSON when it ends in .json or its content starts with [ or {."""
    if path.lower().endswith('.json'):
        return True
    try:
        if text is None:
            with open(path, encoding='utf-8') as _f:
                text = _f.read(400)
        head = text.lstrip()
    except OSError:
        return False
    return head[:1] in ('[', '{')


def _json_list_entries(doc):
    """The array of entries inside a JSON list document, whatever shape it uses:
    ["url", ...] | [{"url": ...}, ...] | {"urls": [...]} | {"items": [...]}."""
    if isinstance(doc, dict):
        for key in ('urls', 'items', 'list'):
            if isinstance(doc.get(key), list):
                return doc[key]
        return []
    return doc if isinstance(doc, list) else []


def _read_url_list(path):
    """Read a --url-list file in EITHER format and return (pending_urls, fmt).
    JSON: entries with "done": true are skipped. TXT: '#' comments and blanks are skipped."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        print(f"[ERROR] Cannot read --url-list file '{path}': {e}")
        sys.exit(1)
    if _url_list_is_json(path, text):
        try:
            doc = json.loads(text)
        except ValueError as e:
            print(f"[ERROR] --url-list '{path}' is not valid JSON: {e}")
            sys.exit(1)
        pending, retries = [], 0
        for it in _json_list_entries(doc):
            if isinstance(it, str):
                if it.strip():
                    pending.append(it.strip())
            elif isinstance(it, dict) and it.get('url'):
                # Not done yet, OR done but flagged (no videos found / preview-only / failed) —
                # those are worth another try, e.g. after a tier upgrade.
                if not it.get('done'):
                    pending.append(str(it['url']).strip())
                elif it.get('warning'):
                    pending.append(str(it['url']).strip())
                    retries += 1
        if retries:
            print(f"[INFO] --url-list: retrying {retries} entry(ies) that finished with a warning "
                  "(no videos / previews only / failed).")
        return pending, 'json'
    return [s.strip() for s in text.splitlines()
            if s.strip() and not s.strip().startswith('#')], 'txt'


def _json_mark_done(path, url, label=None, count=None, warn=None):
    """Record the outcome of `url` in a JSON list file: done/title/files/warning/date. Plain-string
    entries are upgraded to objects so the metadata has somewhere to live."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    arr = _json_list_entries(doc)
    stamp = datetime.now().strftime('%Y-%m-%d')
    for i, it in enumerate(arr):
        cur = it if isinstance(it, str) else (it.get('url') if isinstance(it, dict) else None)
        if not cur or str(cur).strip() != url:
            continue
        if isinstance(it, dict) and it.get('done') and not it.get('warning'):
            continue                       # already finished cleanly — leave it alone
        rec = {'url': url} if isinstance(it, str) else dict(it)
        rec['attempts'] = int(rec.get('attempts') or 0) + 1
        rec['done'] = True
        rec['date'] = stamp
        if label:
            rec['title'] = label
        if count is not None:
            rec['files'] = count
        rec['warning'] = warn or None      # cleared when the retry finally succeeds
        arr[i] = rec
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            print(f"[WARN] Could not update {os.path.basename(path)}: {e}")
            return False
    return False


def _sidecar_path(txt_path):
    """The JSON twin of a TEXT list: downurl.txt -> downurl.json."""
    return os.path.splitext(txt_path)[0] + '.json'


def _sidecar_write(path, doc):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as e:
        print(f"[WARN] Could not update {os.path.basename(path)}: {e}")
        return False


def _sidecar_load(path, source_name):
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get('urls'), list):
            return doc
        if isinstance(doc, list):
            return {'source': source_name, 'urls': doc}
    except (OSError, ValueError):
        pass
    return {'source': source_name, 'updated': None, 'urls': []}


def _sidecar_init(txt_path, pending_urls):
    """Create/refresh the JSON twin of a TEXT list so it holds every URL of the batch, with the
    ones still to do marked done:false. Existing records (and any keys you added) are kept."""
    path = _sidecar_path(txt_path)
    doc = _sidecar_load(path, os.path.basename(txt_path))
    known = {str(e.get('url')): e for e in doc['urls'] if isinstance(e, dict) and e.get('url')}
    for u in pending_urls:
        if u in known:
            if not (known[u].get('done') and not known[u].get('warning')):
                known[u]['done'] = False
        else:
            rec = {'url': u, 'done': False}
            doc['urls'].append(rec)
            known[u] = rec
    doc['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _sidecar_write(path, doc)
    return path


def _sidecar_mark(txt_path, url, label=None, count=None, warn=None):
    """Mirror one finished URL into the JSON twin of a TEXT list."""
    path = _sidecar_path(txt_path)
    doc = _sidecar_load(path, os.path.basename(txt_path))
    rec = next((e for e in doc['urls']
                if isinstance(e, dict) and str(e.get('url')) == url), None)
    if rec is None:
        rec = {'url': url}
        doc['urls'].append(rec)
    rec['attempts'] = int(rec.get('attempts') or 0) + 1
    rec['done'] = True
    rec['date'] = datetime.now().strftime('%Y-%m-%d')
    if label:
        rec['title'] = label
    if count is not None:
        rec['files'] = count
    rec['warning'] = warn or None
    doc['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return _sidecar_write(path, doc)


def _mark_url_done(path, fmt, url, label=None, count=None, warn=None):
    """Mark a finished URL in whichever list format is in use. For a TEXT list the same data is
    ALSO mirrored into a structured JSON twin next to it (downurl.txt -> downurl.json)."""
    if fmt == 'json':
        return _json_mark_done(path, url, label=label, count=count, warn=warn)
    edited = _comment_url_in_list(path, url, label=label, count=count, warn=warn)
    _sidecar_mark(path, url, label=label, count=count, warn=warn)
    return edited


def _comment_url_in_list(list_path, url, label=None, count=None, warn=None):
    """Mark a finished URL in the list file: write a '# <collection/series name> — N file(s),
    <date>' note above it and prepend '# ' to the URL itself, then flush straight to disk (so
    progress survives an interruption). Returns True if it edited."""
    try:
        with open(list_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return False
    edited = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith('#') and s == url:
            indent = line[:len(line) - len(line.lstrip())]
            newline = '\n' if line.endswith('\n') else ''
            note = ''
            if label:
                bits = [str(label).strip()]
                if count:
                    bits.append(f"{count} file(s)")
                bits.append(datetime.now().strftime('%Y-%m-%d'))
                note = f"{indent}# --- {' — '.join(bits)}\n"
            if warn:
                note += f"{indent}# !!! WARNING: {warn}\n"
            lines[i] = f"{note}{indent}# {s}{newline}"
            edited = True
            break
    if not edited:
        return False
    try:
        with open(list_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as e:
        print(f"[WARN] Could not update {os.path.basename(list_path)}: {e}")
        return False


def run_url_list(list_path, base_kwargs, out_dir):
    """Download every URL in `list_path` in turn (auto-rename mode), then write a report log.
    The list may be a plain TEXT file (one URL per line; '#' comments and blanks ignored) or a
    JSON file (["url", ...] or [{"url": ..., "done": false}, ...] or {"urls": [...]}); finished
    URLs are commented out / marked "done": true, together with the collection name and any
    warning, so an interrupted run resumes where it stopped."""
    setup_console(base_kwargs.get('use_color', True))   # style the header lines too
    urls, list_fmt = _read_url_list(list_path)
    if not urls:
        print(f"[ERROR] --url-list file '{list_path}' contains no URLs left to do.")
        sys.exit(1)

    if list_fmt == 'txt':
        _sidecar_init(list_path, urls)
        print(f"[INFO] --url-list: also mirroring progress to "
              f"{os.path.basename(_sidecar_path(list_path))} (structured JSON).")
    print(f"[INFO] --url-list: {len(urls)} URL(s) from '{list_path}' ({list_fmt.upper()} format). "
          f"Rename runs automatically "
          f"(applies when there are no conflicts, otherwise it's skipped).")
    if _ntfy_enabled():
        print(f"[INFO] ntfy notifications ON -> {(NTFY_SERVER or 'https://ntfy.sh').rstrip('/')}/{NTFY_TOPIC}")
    results = []
    started = datetime.now()
    interrupted = False

    for i, url in enumerate(urls, 1):
        print("\n" + "=" * 70)
        print(f"[URL {i}/{len(urls)}] {url}")
        print("=" * 70)
        entry = {'index': i, 'url': url, 'ok': False, 'kind': None, 'title': None,
                 'preview_note': '',
                 'downloaded': [], 'rename': None, 'error': None, 'failed': [], 'seconds': 0.0}
        t0 = time.time()
        try:
            res = main(url, **base_kwargs, rename_mode='auto', return_summary=True)
            if isinstance(res, dict):
                for k in ('ok', 'kind', 'downloaded', 'rename', 'error', 'failed', 'title',
                          'preview_note'):
                    entry[k] = res.get(k, entry[k])
        except KeyboardInterrupt:
            entry['error'] = "interrupted"
            entry['seconds'] = time.time() - t0
            results.append(entry)
            print("\n[WARN] Interrupted during --url-list. Writing a partial report...")
            interrupted = True
            break
        except SystemExit as e:
            entry['error'] = entry['error'] or f"exited (code {getattr(e, 'code', None)})"
        except Exception as e:
            entry['error'] = f"{type(e).__name__}: {e}"
        entry['seconds'] = time.time() - t0
        results.append(entry)

        # If this URL downloaded correctly (no error, nothing still failing), mark it as done
        # in the list file RIGHT NOW by prepending '#', flushing straight to disk — so an
        # interrupted run resumes at the first not-yet-done URL.
        label = entry.get('title') or None
        n_files = len(entry.get('downloaded') or [])
        clean = entry['ok'] and not entry['error'] and not entry.get('failed')
        if clean and n_files:
            if _mark_url_done(list_path, list_fmt, url, label=label, count=n_files,
                              warn=entry.get('preview_note') or None):
                extra = f" — noted as '{label}'" if label else ""
                print(f"[INFO] Marked as done in {os.path.basename(list_path)}{extra}.")
        else:
            # Nothing came out of this URL (or it errored): still mark it done, but write the reason
            # above it so the list itself says what happened (uncomment it to try again later).
            if entry['error']:
                why = f"failed: {entry['error']}"
            elif entry.get('failed'):
                why = f"{len(entry['failed'])} file(s) failed to download"
            else:
                why = ("no downloadable videos found (locked to a higher tier, or an unsupported "
                       "video host)")
            if _mark_url_done(list_path, list_fmt, url, label=label, count=n_files, warn=why):
                print(f"{CLR.YELLOW}[WARN]{CLR.RESET} Marked as done in "
                      f"{os.path.basename(list_path)} with a warning: {why}")

    log_path = _write_url_list_log(results, out_dir, started)
    _notify_url_list_report(results, log_path)
    ok_n = sum(1 for r in results if r['ok'] and not r['error'])
    print("\n" + "=" * 70)
    print(f"[INFO] --url-list done: {ok_n}/{len(results)} URL(s) OK.")
    print(f"[INFO] Report saved to: {log_path}")
    print("=" * 70)
    if interrupted or ok_n != len(urls):
        sys.exit(1)


# =============================================================================
#  --davka  (batch list: name + stream URL + decryption key, per video)
# =============================================================================
# A plain text file describing one video per block:
#
#     Serial S01E01
#     https://host/path/asset.ism/.mpd
#     26dc8baa20b557a47c9f10b43f0a6fad:4ad13f26045246112815b49c6da3ed0f
#     Serial S01E02
#     https://host/path/asset2.ism/.mpd
#     11112222333344445555666677778888:9999aaaabbbbccccddddeeeeffff0000
#
# Line 1 = the name the finished file should get, line 2 = the .mpd/.m3u8 (or a direct file),
# line 3 = the key you already hold for it. Blank lines and '#' comments are ignored, the key is
# optional (unencrypted streams just leave it out) and several key lines may follow one URL when
# the tracks use different keys.

# A key-shaped line: 'KID:KEY' or a bare 32-hex KEY. Deliberately loose (any alphanumerics) so a
# typo'd key is recognised as an ATTEMPTED key and reported, instead of being silently mistaken
# for the next video's name.
_BATCH_KEY_SHAPE_RE = re.compile(
    r'^(?:0x)?[0-9A-Za-z]{8,64}\s*:\s*(?:0x)?[0-9A-Za-z]{8,64}$|^(?:0x)?[0-9A-Fa-f]{32}$')
_BATCH_VIDEO_EXTS = ('.mkv', '.mp4', '.m4v', '.webm', '.mov')


def _read_batch_file(path):
    """Parse a --davka file into [{name, url, keys, line}] plus a list of complaints.

    Lines are recognised by SHAPE, not by position, so an entry may legitimately have no key, or
    several, and a stray blank line can't shift the whole file by one."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            raw_lines = f.read().splitlines()
    except OSError as e:
        print(f"[ERROR] --davka: could not read '{path}': {e}")
        sys.exit(1)

    entries, problems = [], []
    cur = None
    for n, raw in enumerate(raw_lines, 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if re.match(r'^https?://', line, re.I):
            if cur is None or cur['url']:
                cur = {'name': None, 'url': None, 'keys': [], 'line': n}
                entries.append(cur)
            cur['url'] = line
        elif _BATCH_KEY_SHAPE_RE.match(line):
            if cur is None or not cur['url']:
                problems.append(f"line {n}: a key with no video URL above it — ignored")
                continue
            cur['keys'].append(line)
        else:
            cur = {'name': line, 'url': None, 'keys': [], 'line': n}
            entries.append(cur)

    good, used = [], set()
    for e in entries:
        if not e['url']:
            problems.append(f"line {e['line']}: '{(e['name'] or '')[:50]}' has no video URL "
                            f"below it — skipped")
            continue
        # Validate the keys now: downloading an encrypted stream with a broken key would produce
        # a file that plays as garbage, so it is far better to stop and say so.
        specs = _parse_cenc_keys(e['keys'], None) if e['keys'] else []
        if e['keys'] and not specs:
            problems.append(f"line {e['line']}: '{(e['name'] or '?')[:50]}' has no usable key "
                            f"(need 32-hex KEY or KID:KEY) — skipped")
            continue
        # Fall back to the URL's own name when the block had no title line.
        base = e['name']
        if not base:
            parts = [p for p in urlparse(e['url']).path.split('/') if p]
            base = re.sub(r'\.isml?$', '', parts[-2], flags=re.I) if len(parts) > 1 else 'video'
        # Strip only a REAL container extension. A blind splitext() would turn a perfectly
        # normal name like "Film 2.dil" into "Film 2", because ".dil" looks like an extension.
        stem = safe_filename(base, 'video')
        root, ext = os.path.splitext(stem)
        if ext.lower() in _BATCH_VIDEO_EXTS and root:
            stem = root
        stem = stem or 'video'
        if stem.lower() in used:                     # two blocks named the same
            i = 2
            while f"{stem} ({i})".lower() in used:
                i += 1
            problems.append(f"line {e['line']}: duplicate name '{stem}' — saving as '{stem} ({i})'")
            stem = f"{stem} ({i})"
        used.add(stem.lower())
        good.append({'name': stem, 'url': e['url'], 'keys': specs, 'line': e['line']})
    return good, problems


def _batch_existing_output(stem, directory=None):
    """Path of an already-finished file for this entry, or None. The container is chosen by the
    downloader (.mkv when multi-track, else .mp4), so every plausible extension is checked.
    A zero-byte leftover does not count as done."""
    for ext in _BATCH_VIDEO_EXTS:
        p = os.path.join(directory, stem + ext) if directory else stem + ext
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            pass
    return None


def _batch_resolve_page(page_url, session, verbose, use_browser):
    """Turn an ordinary web page into the one stream worth downloading.

    Used by --davka-browser, where the list holds page addresses instead of manifests. The
    picking order mirrors --scan: a real manifest beats a plain file (it is adaptive, so it
    carries the best quality), and same-stream renditions collapse to the best one."""
    found = scan_page_for_media(page_url, session, verbose)
    if use_browser:
        for it in _browser_scan_items(page_url, session, verbose):
            if not any(f['url'] == it['url'] for f in found):
                found.append(it)
    if not found:
        return None
    for kinds in (('mpd', 'hls'), ('direct',), ('url',)):
        subset = [f for f in found if f['kind'] in kinds]
        if not subset:
            continue
        if len(subset) > 1:
            subset = _scan_resolution_winners(subset)
        if verbose and len(found) > 1:
            tqdm.write(f"[DBG] --davka-browser: {len(found)} item(s) found, taking "
                       f"[{subset[0]['kind']}] {subset[0]['url']}")
        return subset[0]
    return None


def _download_batch_entry(entry, session, max_height, verbose, chunk_size=DEFAULT_CHUNK_SIZE,
                          use_browser=False, cookies_file=None):
    """Download one --davka entry into the current directory. Returns (ok, reason, path).

    With use_browser the entry's address is treated as a PAGE: it is opened in the real browser
    first and whatever stream that reveals is what gets downloaded, under the entry's name and
    with the entry's key."""
    url, stem, keys = entry['url'], entry['name'], entry['keys']
    page_url = None

    # A page address needs resolving; an address that already IS a manifest or a file does not,
    # so a list may freely mix the two.
    if use_browser and not _classify_media_url(url):
        print(f"[INFO] --davka-browser: opening {url}")
        hit = _batch_resolve_page(url, session, verbose, True)
        if not hit:
            return False, "no stream found on that page (try raising SCAN_BROWSER_WAIT)", None
        page_url, url = url, hit['url']
        print(f"[INFO] --davka-browser: found [{hit['kind']}] {url}")

    # CDNs commonly reject a manifest request that lacks the page's Referer/Origin, so those come
    # from the PAGE the stream was found on whenever we know it.
    pg = urlparse(page_url or url)
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    if pg.scheme and pg.netloc:
        headers['Referer'] = f"{pg.scheme}://{pg.netloc}/"
        headers['Origin'] = f"{pg.scheme}://{pg.netloc}"

    cls = _classify_media_url(url)
    kind = cls[0] if cls else None
    if kind == 'url':
        # A YouTube/Vimeo/Drive/... link: hand it to the normal pipeline under the given name.
        res = main(url, output_file=stem + '.mp4', verbose=verbose, max_height=max_height,
                   cookies_file=cookies_file, do_rename=False, return_summary=True, auto=True)
        okay = bool(res and res.get('ok') and res.get('downloaded'))
        # Report the actual file so the run's summary and the next run's skip-check see it.
        got = _batch_existing_output(stem)
        return okay, (None if okay else ((res or {}).get('error') or "no file downloaded")), got
    if kind == 'direct':
        fname = stem + (os.path.splitext(urlparse(url).path)[1] or '.mp4')
        download_folder_pooled([{'id': url, 'title': fname, 'name': fname, 'direct_url': url,
                                 'headers': headers}], session, chunk_size, verbose,
                               label="Davka", conn_cap=16)
        got = _batch_existing_output(stem) or (fname if os.path.exists(fname) else None)
        return bool(got), (None if got else "direct download failed"), got

    if kind is None:
        # Not obviously a manifest — let ffmpeg decide; it recognises far more than the URL does.
        kind = 'mpd' if '.mpd' in url.lower() else 'hls' if '.m3u8' in url.lower() else 'mpd'

    out_path = stem + '.mp4'      # the grabbers replace the extension per --container / tracks
    if keys:
        if kind == 'mpd':
            ok, why = _cenc_grab_dash(url, headers, session, max_height, out_path, keys, verbose)
        else:
            ok, why = _cenc_grab_hls(url, headers, session, max_height, out_path, keys, verbose)
    elif kind == 'mpd':
        ok, why = _grab_dash_plain(url, headers, session, max_height, out_path, verbose)
        if not ok:
            if verbose:
                tqdm.write(f"[INFO] --davka: native DASH failed ({why}); retrying through ffmpeg.")
            ok, why = _ffmpeg_grab_stream(url, headers, out_path, verbose)
    else:
        ok, why = _ffmpeg_grab_hls(url, headers, session, max_height, out_path, verbose)

    got = _batch_existing_output(stem)
    if ok and not got:
        return False, "the downloader reported success but produced no file", None
    if got:
        _record_download(os.path.abspath(got))
    return bool(ok and got), (None if ok else why), got


def run_batch(list_path, base_kwargs, out_dir):
    """--davka: download every video described in a name/URL/key text file.

    Re-running is safe and cheap: entries whose output file already exists are skipped, so an
    interrupted batch simply continues where it stopped."""
    # Colour, UTF-8 output and ASCII progress bars must be set up BEFORE the first message —
    # exactly like main() and run_resume() do. Doing it later (or not at all on the --list and
    # error-exit paths) is why the very first lines came out unstyled.
    setup_console(base_kwargs.get('use_color', True))

    entries, problems = _read_batch_file(list_path)
    use_browser = bool(base_kwargs.get('browser'))
    mode = '--davka-browser' if use_browser else '--davka'
    verbose = base_kwargs.get('verbose', False)
    max_height = base_kwargs.get('max_height', DEFAULT_MAX_HEIGHT)
    list_only = base_kwargs.get('list_only', False)
    for p in problems:
        print(f"{CLR.YELLOW}[WARN]{CLR.RESET} {mode}: {p}")
    if not entries:
        print(f"[ERROR] {mode}: '{list_path}' contains no usable video entries.")
        print("        Expected blocks of: name / URL / key (key optional), one per line.")
        sys.exit(1)

    enc_n = sum(1 for e in entries if e['keys'])
    print(f"[INFO] {mode}: {len(entries)} video(s) from '{list_path}' "
          f"({enc_n} with a decryption key)."
          + ("  Each address is opened in a real browser to find its stream."
             if use_browser else ""))
    if list_only:
        for i, e in enumerate(entries, 1):
            print(f"  {i:>3}. {e['name']}")
            print(f"       {e['url']}")
            print(f"       keys: {len(e['keys'])}" if e['keys'] else "       keys: none")
        return

    # Work out what is already downloaded FIRST. Nothing below should demand a tool for work
    # that no longer needs doing — re-running a finished batch must succeed even on a machine
    # without ffmpeg or mp4decrypt installed.
    base_dir = os.path.abspath(out_dir) if out_dir else os.getcwd()
    results, started, interrupted = [], datetime.now(), False
    todo = []
    for i, e in enumerate(entries, 1):
        have = _batch_existing_output(e['name'], base_dir)
        if have:
            results.append({'index': i, 'url': e['url'], 'ok': True, 'kind': 'davka',
                            'title': e['name'], 'preview_note': '',
                            'downloaded': [os.path.basename(have)], 'rename': None,
                            'error': None, 'failed': [], 'seconds': 0.0, 'skipped': True})
        else:
            todo.append((i, e))
    if len(todo) < len(entries):
        print(f"[INFO] {mode}: {len(entries) - len(todo)} video(s) already downloaded — "
              f"skipping those; {len(todo)} left to do.")
    if not todo:
        print(f"{CLR.GREEN}[INFO]{CLR.RESET} {mode}: everything in '{list_path}' is already "
              f"downloaded — nothing to do.")
        return

    if not ensure_ffmpeg(verbose):
        print(f"[ERROR] {mode}: ffmpeg is required to mux the downloaded streams.")
        sys.exit(1)
    if any(e['keys'] for _i, e in todo) and not ensure_mp4decrypt(verbose):
        print(f"[ERROR] {mode}: mp4decrypt (Bento4) is required to use the keys in this file.")
        sys.exit(1)
    if _ntfy_enabled():
        print(f"[INFO] ntfy notifications ON -> "
              f"{(NTFY_SERVER or 'https://ntfy.sh').rstrip('/')}/{NTFY_TOPIC}")

    try:
        session = get_cookies_session(
            [base_kwargs['cookies_file']] if base_kwargs.get('cookies_file')
            else (auto_detect_cookies(verbose) if base_kwargs.get('auto_cookies', True) else []))
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {mode}: failed to load cookies: {e}")
        sys.exit(1)

    global _max_conn_explicit
    mc = base_kwargs.get('max_connections')
    _max_conn_explicit = mc is not None
    if mc is None:
        mc = _auto_settings()[1] if base_kwargs.get('auto', True) else DEFAULT_MAX_CONNECTIONS
    set_connection_limit(mc)

    def _run_all():
        nonlocal interrupted
        for n, (i, e) in enumerate(todo, 1):
            print("\n" + "=" * 70)
            print(f"[{n}/{len(todo)}] {e['name']}")
            print("=" * 70)
            r = {'index': i, 'url': e['url'], 'ok': False, 'kind': 'davka', 'title': e['name'],
                 'preview_note': '', 'downloaded': [], 'rename': None, 'error': None,
                 'failed': [], 'seconds': 0.0}
            # Re-check here as well: a parallel run (or a very long batch) may have finished it
            # in the meantime.
            have = _batch_existing_output(e['name'])
            if have:
                print(f"[INFO] Already have {os.path.basename(have)}, skipping.")
                r.update(ok=True, downloaded=[os.path.basename(have)], skipped=True)
                results.append(r)
                continue
            t0 = time.time()
            try:
                ok, why, got = _download_batch_entry(e, session, max_height, verbose,
                                                     base_kwargs.get('chunk_size',
                                                                     DEFAULT_CHUNK_SIZE),
                                                     use_browser=use_browser,
                                                     cookies_file=base_kwargs.get('cookies_file'))
                r['ok'] = bool(ok)
                if not ok:
                    r['error'] = why or "download failed"
                elif got:
                    r['downloaded'] = [os.path.basename(got)]
            except KeyboardInterrupt:
                r['error'] = "interrupted"
                r['seconds'] = time.time() - t0
                results.append(r)
                print("\n[WARN] Interrupted. Finished files are kept — re-run the same command "
                      "to continue with the rest.")
                interrupted = True
                return
            except Exception as exc:
                r['error'] = f"{type(exc).__name__}: {exc}"
            r['seconds'] = time.time() - t0
            results.append(r)
            if r['ok']:
                print(f"{CLR.GREEN}[OK]{CLR.RESET} {r['downloaded'][0] if r['downloaded'] else e['name']}")
            else:
                print(f"{CLR.RED}[FAIL]{CLR.RESET} {e['name']}: {r['error']}")

    try:
        _run_in_dir(out_dir or '.', _run_all)
    finally:
        session.close()

    results.sort(key=lambda r: r['index'])
    log_path = _write_url_list_log(results, out_dir, started, label=mode)
    _notify_url_list_report(results, log_path)
    ok_n = sum(1 for r in results if r['ok'])
    skip_n = sum(1 for r in results if r.get('skipped'))
    print("\n" + "=" * 70)
    print(f"[INFO] {mode} done: {ok_n}/{len(entries)} video(s) OK"
          + (f" ({skip_n} were already downloaded)" if skip_n else "") + ".")
    print(f"[INFO] Report saved to: {log_path}")
    print("=" * 70)
    for r in results:
        if not r['ok']:
            print(f"[WARN] {r['title']}: {r['error']}")
    _cleanup_temp_dir(os.path.abspath(out_dir) if out_dir else os.getcwd())
    if interrupted or ok_n != len(entries):
        sys.exit(1)


if __name__ == "__main__":
    if sys.platform == "win32":
        # Put the console into UTF-8 so non-Latin titles (Korean, etc.) print correctly where the
        # font supports them (cmd.exe's default font still shows CJK as boxes — use Windows
        # Terminal or a CJK console font like "NSimSun" to see them). Filenames are unaffected;
        # they are always written correctly regardless of what the console can render.
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    # Optional remote config (CONFIG_URL) overrides USER CONFIG here, BEFORE argparse builds its
    # defaults from those globals — so the command line still wins over both. No-op when unset.
    # Many parallel download threads make CPython hand the GIL around very aggressively; a slightly
    # longer switch interval cuts that contention (pure CPU saving, no effect on throughput).
    try:
        sys.setswitchinterval(0.02)
    except (AttributeError, ValueError):
        pass

    # --insecure has to take effect BEFORE the config is fetched, so it is read straight from
    # argv rather than waiting for argparse.
    args_insecure = any(a in ('--insecure', '--no-check-certificate', '--no-check-certificates')
                        for a in sys.argv[1:])

    # --help is handled inside parse_args(), so the console (and therefore the palette) has to be
    # ready before the parser is even built.
    setup_console(USE_COLOR and '--no-color' not in sys.argv[1:])

    if args_insecure or INSECURE_TLS:
        _apply_insecure_tls(announce=not any(
            a in ('-h', '--help', '--version') for a in sys.argv[1:]))

    # Purely local commands don't need the remote config, and waiting CONFIG_FETCH_TIMEOUT
    # seconds (plus printing a warning) just to show --help or --version is pointless.
    if not any(a in ('-h', '--help', '--version', '--dump-config') for a in sys.argv[1:]):
        _apply_remote_config()

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

    _desc, _epilog = _help_text_blocks()
    parser = argparse.ArgumentParser(
        formatter_class=_ColorHelpFormatter, description=_desc, epilog=_epilog,
        add_help=False)
    parser.add_argument("-h", "--help", action="help",
                        help="Show this help — including the workflow, every option, and the exact "
                             "layout of the files this script reads — and exit.")
    parser.add_argument("video_id", type=str, nargs='?', default=None, help="A Drive file ID / file URL / FOLDER URL, a Dropbox share URL, a Vimeo URL, a YouTube video or playlist URL, a Twitch VOD URL, or a Patreon COLLECTION/POST URL. Folders, collections and playlists download every video found. Omit when using --url-list.")
    parser.add_argument("--url-list", type=str, default=None, help="Path to a TEXT file with ONE URL per line, or a JSON file ([\"url\", ...] / [{\"url\": ..., \"done\": false}] / {\"urls\": [...]}); finished entries are marked done with the collection name, file count and any warning (blank lines and '#' comments ignored). Downloads each in turn, auto-applies the rename when there are no conflicts (otherwise skips it), and writes a report .log at the end.")
    parser.add_argument("--davka", "--batch", dest="davka", type=str, default=None, metavar="FILE", help="Batch list: a TEXT file describing one video per block - NAME line, then the video URL (.mpd/.m3u8 or a direct file), then optionally the KID:KEY decryption key you already hold (several key lines are allowed, and unencrypted streams simply have none). Blank lines and '#' comments are ignored. Each video is saved under its NAME. Re-running skips videos whose file is already there, so an interrupted batch just continues. Add -l to only list what was parsed.")
    parser.add_argument("--davka-browser", "--batch-browser", dest="davka_browser", type=str, default=None, metavar="FILE", help="Same list format and behaviour as --davka, except the middle line is a normal PAGE address instead of a stream: each one is opened in a real browser (as --scan-browser does), the stream its player fetches is discovered, and THAT is downloaded under the block's name with the block's key. Addresses that already point straight at a .mpd/.m3u8/file are used as-is, so a list may mix both.")
    parser.add_argument("-o", "--output", type=str, help="Output file name (single file only; ignored for folders/collections).")
    parser.add_argument("-d", "--output-dir", type=str, default=None, help="Directory to save into (applies to any mode; created if missing). Default: current directory.")
    parser.add_argument("-c", "--chunk_size", type=positive_int, default=DEFAULT_CHUNK_SIZE, help=f"Streaming chunk size in bytes. Default {DEFAULT_CHUNK_SIZE} (edit DEFAULT_CHUNK_SIZE at the top of the script).")
    parser.add_argument("-t", "--threads", type=positive_int, default=None, help=f"Download threads per file (>=1) for a SINGLE Google Drive file. Default {DEFAULT_THREADS}. Folders, collections and every other source use the shared segment pool instead, where -m is what matters. Edit DEFAULT_THREADS at the top.")
    parser.add_argument("-w", "--folder-workers", type=nonneg_int, default=DEFAULT_FOLDER_WORKERS, help="DEPRECATED and ignored: the shared segment pool works on all files at once and is bounded by -m, so there is no per-file worker count any more. Accepted only so older commands keep working; use -m to control concurrency.")
    parser.add_argument("-m", "--max-connections", type=positive_int, default=None, help=f"Hard cap on simultaneous connections regardless of workers x threads. Default {DEFAULT_MAX_CONNECTIONS}. Lower (e.g. 16) if you hit 'insufficient resources'.")
    parser.add_argument("-q", "--max-height", type=nonneg_int, default=DEFAULT_MAX_HEIGHT, help="For native Patreon/Vimeo HLS videos: cap height (e.g. 720). 0 = best available (default).")
    parser.add_argument("--no-auto", action="store_true", help="Disable the default auto-tuning of threads/connections from the detected CPU (use the fixed defaults). Explicit -t/-m still win.")
    parser.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # now the default; kept so old commands don't break
    parser.add_argument("-s", "--select", action="store_true", help="For a folder or Patreon collection: list all items first and interactively choose which to download.")
    parser.add_argument("-l", "--list", action="store_true", help="Only list what was found (Patreon collection/post: Drive/Dropbox/native; folder: files; YouTube playlist/video and Twitch: titles) WITHOUT downloading. Add -v to also show source URLs/IDs.")
    parser.add_argument("--ascii", action="store_true", help="Force plain-ASCII filenames: strip accents (é->e) and drop non-Latin characters (Korean, etc.). Useful so Windows cmd shows names correctly and progress bars line up. Files keep their content; only names change.")
    parser.add_argument("--res", nargs='?', const='SCAN', default=None, metavar='HEIGHT', help="YouTube quality. Without a value (just --res) it SCANS and lists the available qualities and exits. With a value it downloads that quality: --res 1080, --res 720, --res 4k (=2160), --res 2k (=1440). No --res = best available (default).")
    parser.add_argument("--scan", nargs='?', const=True, default=None, metavar='N', help="Discovery mode: fetch ANY page URL, find every video it recognises (YouTube/Vimeo/Twitch/Drive/Dropbox embeds or links, plus direct .mp4/.m3u8/.mpd/.webm/.mov) and download them all. Give a number to grab only the N-th found item, e.g. --scan 2 (1-indexed, in discovery order).")
    parser.add_argument("--browser-login", type=str, default=None, metavar="URL", help="Open URL in the scan browser and leave the window open so you can sign in by hand, then close it. The session is kept in the browser profile, so later --scan-browser runs on that site are logged in. Use this when the site's login cookie is HttpOnly and cannot be injected from a cookie file.")
    parser.add_argument("--scan-browser", action="store_true", help="Like --scan but loads the page in a real browser (pywebview) and lets JavaScript run first, so it also finds media added dynamically by players/scripts. Implies --scan; works with --select/--list/--res/--audio/--sub.")
    parser.add_argument("--audio", nargs='?', const='SCAN', default=None, metavar='N', help="For streams with multiple audio tracks (HLS): without a value, lists the tracks and exits; with a value picks them, e.g. --audio 1,3 (or 'all'). Default (no --audio) includes ALL audio tracks.")
    parser.add_argument("--sub", nargs='?', const='SCAN', default=None, metavar='N', help="Subtitles (HLS): without a value, lists available subtitle tracks and exits; with a value picks them, e.g. --sub 1,2 (or 'all'). Default (no --sub) includes ALL subtitles found.")
    parser.add_argument("--key", action='append', default=None, metavar='KID:KEY', help="Content decryption key you ALREADY hold (for your own DRM-protected storage), as KID:KEY or a bare 32-hex KEY. Repeatable for multiple tracks. Used to decrypt CENC/Widevine HLS/DASH via ffmpeg. This is NOT key extraction or DRM-bypass; you must supply your own keys.")
    parser.add_argument("--keys", default=None, metavar='FILE', help="Read decryption keys (one KID:KEY per line) from a file, same purpose as --key.")
    parser.add_argument("--container", choices=['auto', 'mp4', 'mkv'], default='auto', help="Force the final container: 'mp4' or 'mkv'. Default 'auto' = .mkv when there are multiple audio tracks or subtitles (names/languages show reliably), otherwise .mp4.")
    parser.add_argument("--no-recursive", action="store_true", help="Do not descend into subfolders when given a folder.")
    parser.add_argument("--no-auto-cookies", action="store_true", help="Do not auto-use a *.json cookie file found next to the script / in the current directory.")
    parser.add_argument("--insecure", "--no-check-certificate", "--no-check-certificates", dest="insecure", action="store_true", help="Never verify TLS certificates, on any connection (config, page scans, manifests, every download). Expired, self-signed or wrong-hostname certificates all work. Prints a warning once. Set INSECURE_TLS = True at the top of the script to make it permanent.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookies", type=str, help="Path to a Netscape cookies.txt file or JSON cookie export (Google Drive / Patreon session).")
    parser.add_argument("--ffmpeg", type=str, default=None, help="Path to the ffmpeg executable or a folder containing it (for native HLS videos).")
    parser.add_argument("--ffmpeg-url", type=str, default=None, help="URL of an ffmpeg archive to auto-download if ffmpeg is missing. Empty string disables auto-download.")
    parser.add_argument("--resume", action="store_true", help="Retry only what failed in earlier runs (from resume.json) and stop. Running with no arguments at all does the same thing, then asks for a URL if there was nothing to retry; --resume never asks.")
    parser.add_argument("--no-rename", action="store_true", help="After downloading, do NOT offer the intelligent --strict rename of the new files.")
    parser.add_argument("--follow-links", action="store_true", help="With --scan: treat the given URL as an index/listing page — visit every same-site link (episode pages) and scan each for media too, instead of only the given page. Combines with --subs-only.")
    parser.add_argument("--password", type=str, default=None, help="Password for a single password-protected video (muse.ai). In Patreon collections the password is found automatically in the post text, so you rarely need this.")
    parser.add_argument("--subs-only", action="store_true", help="Subtitles-only mode: for a collection or any scanned page (HLS .m3u8 or DASH .mpd), find the videos, show which subtitle languages are available, let you multi-select which to grab, then download ONLY those subtitles directly (no video/audio) and save them as .srt. If the given URL is an episode index (no stream of its own), it automatically follows the same-site episode links and scans each. Other modes are unaffected.")
    parser.add_argument("--dump-config", nargs="?", const="videoloader_dir.config.json", metavar="FILE", help="Write a JSON file with every remotely-overridable setting and its current value (default name: videoloader_dir.config.json), then exit. Use it as the template for CONFIG_URL.")
    parser.add_argument("--test-notify", action="store_true", help="Send a test ntfy.sh push notification (uses NTFY_TOPIC/NTFY_SERVER set at the top of the script) and exit. Use this to verify your phone receives it.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")

    args = parser.parse_args()

    print(f"[INFO] videoloader_dir v{SCRIPT_VERSION}")

    # Already applied from raw argv before the config fetch; repeating it is a no-op and keeps the
    # parsed flag as the single source of truth for anything added later.
    if args.insecure:
        _apply_insecure_tls()

    if args.ascii:
        ASCII_FILENAMES = True
    if args.audio is not None:
        AUDIO_SEL = _parse_track_sel(args.audio)
    if args.sub is not None:
        SUB_SEL = _parse_track_sel(args.sub)
    CENC_KEYS = _parse_cenc_keys(args.key, args.keys)
    FORCE_CONTAINER = args.container if args.container in ('mp4', 'mkv') else None
    SUBS_ONLY = args.subs_only
    MUSE_PASSWORD = args.password

    _res = _parse_res(args.res)
    if isinstance(_res, int):
        args.max_height = _res              # --res 1080 -> cap height (best up to 1080)
    _res_scan = (_res == 'SCAN')

    if args.dump_config:
        sys.exit(0 if dump_config_file(args.dump_config) else 1)

    if args.test_notify:
        if not _ntfy_enabled():
            print("[ERROR] ntfy is not configured. Set NTFY_TOPIC (and NTFY_SERVER if self-hosting)")
            print("        near the top of the script, then run --test-notify again.")
            sys.exit(1)
        target = f"{(NTFY_SERVER or 'https://ntfy.sh').rstrip('/')}/{NTFY_TOPIC}"
        print(f"[INFO] Sending a test notification to {target} ...")
        ok = notify_ntfy(
            "Test notification from videoloader_dir.py. If you can read this on your phone, "
            "notifications work \u2705",
            title="videoloader test", priority="default", tags="tada")
        print("[INFO] Sent - check your phone." if ok else "[WARN] Could not send (see above).")
        sys.exit(0 if ok else 1)

    if args.browser_login:
        setup_console(USE_COLOR and not args.no_color)
        try:
            _sess = get_cookies_session(
                [args.cookies] if args.cookies
                else (auto_detect_cookies(args.verbose)
                      if (AUTO_COOKIES and not args.no_auto_cookies) else []))
        except (FileNotFoundError, ValueError) as e:
            print(f"[ERROR] Failed to load cookies: {e}")
            sys.exit(1)
        print("[INFO] --browser-login: sign in in the window, then close it. The session is kept "
              "for later --scan-browser runs.")
        try:
            _browser_collect_media(args.browser_login, args.verbose, session=_sess,
                                   login_only=True)
        finally:
            _sess.close()
        print("[INFO] --browser-login: done.")
        sys.exit(0)

    if sum(bool(x) for x in (args.video_id, args.url_list, args.davka,
                             args.davka_browser)) > 1:
        parser.error("give only ONE of: a single URL, --url-list, --davka, or --davka-browser.")

    # The batch modes run their own loop (name/URL/key blocks), so they short-circuit the rest.
    if args.davka or args.davka_browser:
        _list = args.davka or args.davka_browser
        _mode = "--davka-browser" if args.davka_browser else "--davka"
        if args.output:
            print(f"[WARN] -o/--output is ignored with {_mode}; names come from the file.")
        run_batch(_list, dict(
            verbose=args.verbose, cookies_file=args.cookies, max_height=args.max_height,
            max_connections=args.max_connections, use_color=(USE_COLOR and not args.no_color),
            auto_cookies=(AUTO_COOKIES and not args.no_auto_cookies), list_only=args.list,
            chunk_size=args.chunk_size, auto=(not args.no_auto),
            browser=bool(args.davka_browser),
        ), args.output_dir)
        sys.exit(0)

    # Auto-scan: a URL whose host matches SCAN_AUTO_HOSTS / SCAN_BROWSER_AUTO_HOSTS runs as if
    # --scan / --scan-browser was given. Only an explicit --scan / --scan-browser disables this
    # (your explicit scan choice wins); other flags like --res/--audio/--sub/--key/-v combine
    # with it normally.
    if args.video_id and args.scan is None and not args.scan_browser:
        _host = (urlparse(args.video_id).hostname or '').lower()

        def _apply_auto_m(opts):
            # Host-provided -m behaves exactly like a real -m (applies to every download from this
            # URL, overrides per-source caps), but an explicit command-line -m still wins.
            if opts['m'] is not None and args.max_connections is None:
                args.max_connections = opts['m']
                return f" -m {opts['m']}"
            return ""

        _m, _opts = _auto_scan_match(_host, SCAN_AUTO_HOSTS)
        if _m:
            args.scan = str(_opts['scan']) if _opts['scan'] is not None else True
            extra = _apply_auto_m(_opts)
            print(f"[INFO] Auto-scan: {_host} matches SCAN_AUTO_HOSTS -> running with "
                  f"--scan{(' ' + str(_opts['scan'])) if _opts['scan'] is not None else ''}{extra}")
        else:
            _m, _opts = _auto_scan_match(_host, SCAN_BROWSER_AUTO_HOSTS)
            if _m:
                args.scan_browser = True
                if _opts['scan'] is not None:
                    args.scan = str(_opts['scan'])
                extra = _apply_auto_m(_opts)
                print(f"[INFO] Auto-scan: {_host} matches SCAN_BROWSER_AUTO_HOSTS -> running with "
                      f"--scan-browser{(' ' + str(_opts['scan'])) if _opts['scan'] is not None else ''}{extra}")

    _scan_pick = int(args.scan) if isinstance(args.scan, str) and args.scan.isdigit() else None
    common = dict(
        chunk_size=args.chunk_size, num_threads=args.threads, verbose=args.verbose,
        cookies_file=args.cookies, folder_workers=args.folder_workers,
        recursive=(not args.no_recursive and DEFAULT_RECURSIVE),
        max_connections=args.max_connections, use_color=(USE_COLOR and not args.no_color),
        auto_cookies=(AUTO_COOKIES and not args.no_auto_cookies), select=args.select,
        auto=(not args.no_auto), out_dir=args.output_dir, max_height=args.max_height,
        list_only=args.list, ffmpeg_path=args.ffmpeg, ffmpeg_url=args.ffmpeg_url,
        do_rename=(not args.no_rename), res_scan=_res_scan,
        scan_mode=(args.scan is not None or args.scan_browser), browser_scan=args.scan_browser,
        scan_pick=_scan_pick, scan_links=args.follow_links,
    )

    # No URL and no --url-list: retry previously-failed files from resume.json, if present.
    if args.resume and (args.video_id or args.url_list or args.davka or args.davka_browser):
        parser.error("--resume retries the previous run, so it takes no URL or list.")

    if args.resume or not (args.url_list or args.video_id or args.davka or args.davka_browser):
        with _failed_lock:
            _failed_jobs.clear()
            _session_failed.clear()
        try:
            if run_resume(common):
                sys.exit(0)
        except KeyboardInterrupt:
            print("\n[WARN] Interrupted. resume.json kept — re-run with no arguments to continue.")
            sys.exit(130)
        if args.resume:
            print("[INFO] --resume: nothing left to retry.")
            sys.exit(0)
        # Nothing to resume and no URL on the command line. Ask for one interactively — pasting
        # here is immune to cmd.exe splitting a URL at '&' (e.g. watch?v=...&list=...).
        if getattr(sys.stdin, 'isatty', lambda: False)():
            print("Tip: paste the full URL here — '&' is safe in this prompt (unlike on the cmd line).")
            try:
                pasted = input("URL (YouTube / Patreon / Drive / Dropbox / Vimeo / Twitch): ").strip()
            except (EOFError, KeyboardInterrupt):
                pasted = ''
            pasted = pasted.strip('"').strip("'").strip()
            if pasted:
                args.video_id = pasted
            else:
                parser.error("provide a URL, or use --url-list FILE (or have a resume.json here to retry).")
        else:
            parser.error("provide a URL, or use --url-list FILE (or have a resume.json here to retry).")

    with _failed_lock:
        _failed_jobs.clear()
        _session_failed.clear()
    try:
        if args.url_list:
            if args.output:
                print("[WARN] -o/--output is ignored with --url-list; names come from each source.")
            run_url_list(args.url_list, dict(common, output_file=None), args.output_dir)
        else:
            main(args.video_id, args.output, **common)
        _update_resume_after_run()
    except KeyboardInterrupt:
        # Locks are released by the atexit handler; partial .part files are kept so a re-run
        # resumes where this left off.
        _update_resume_after_run()
        print("\n[WARN] Interrupted. Partial parts were kept — re-run the same command to resume.")
        sys.exit(130)
