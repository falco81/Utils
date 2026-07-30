#!/usr/bin/env python3
"""
plex_tools.py  (Windows 10 CLI, colored UI via colorama)
===============================================================

Bulk-sets the DEFAULT audio track and subtitles on a Plex Media Server -
for every episode of a show, or for a movie. Signs in with your Plex
account and switches to a specific Plex Home user (with their PIN), then
lets you browse/search libraries (shows and movies), scans the available
tracks and offers a selection.

On first run it asks for:
  - the FQDN (address) of your Plex server, e.g. https://plex.falco81.net
  - Plex account sign-in (username+password, or a code on plex.tv/link)
  - which Plex Home user to use and its PIN (if it has one)
Everything is saved to the config (address + obtained tokens), so ON
SUBSEQUENT RUNS IT ASKS FOR NOTHING. The config is searched (first
existing one wins): path from --config / the PLEX_TOOLS_CONFIG
variable, then next to the script (plex_tools.config.json), then a
.config folder next to the script and in parent folders (works from a
network/samba drive too), finally ~/.config. A new config is created NEXT
TO THE SCRIPT (so it travels with it to another drive/OS). Show the
current path with --where-config, force a custom path with --config.
A remote config can be the PRIMARY source: set CONFIG_URL (or
PLEX_TOOLS_CONFIG_URL / --config-url) to a JSON file (e.g. on your NAS)
whose keys OVERRIDE the local config; if it's unreachable/invalid the
script falls back to the local file methods above (short timeout, no hang).
Logout / reset: --logout (delete tokens), --relogin (fresh sign-in),
--switch-user (pick the Home user again).

How it works (verified against the Plex API documentation)
----------------------------------------------------------
- Account sign-in:   POST https://plex.tv/api/v2/users/signin  (login/
                     password/verificationCode) OR a PIN on plex.tv/link.
- Home users:        GET  https://plex.tv/api/v2/home/users     (XML)
- Switch + PIN:      POST https://plex.tv/api/home/users/{id}/switch?pin=..
                     -> returns a user token (authenticationToken),
                        which is saved and reused next time.
- Setting a track:   PUT  {server}/library/parts/{partId}
                        ?audioStreamID=A&subtitleStreamID=T&allParts=1
  subtitleStreamID=0 = turn subtitles off. Only the default-track choice
  changes; files and subtitle text are untouched. Stream IDs are resolved
  at run time by track (Plex renumbers them after a metadata refresh).

Installation
------------
1) Python 3.8+
2) (optional) pip install colorama   -> colors on Windows CLI (the script
   runs fine without it). No other dependencies - all via the stdlib.

Usage
-----
    python plex_tools.py                 # interactive wizard
    python plex_tools.py --show 32800 --audio kor --subs cze --yes
    python plex_tools.py --show "Recipe for Love" --subs off --dry-run
    python plex_tools.py --logout | --relogin | --switch-user
"""

import argparse
import datetime
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Colored output (Windows-CLI friendly via colorama, no crash even without it)
# ---------------------------------------------------------------------------
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


# truststore (optional): verifies HTTPS via the OS certificate store
# (Windows can fetch a missing intermediate certificate). If absent,
# the script falls back automatically for your own server (see _connect).
try:
    import truststore
    truststore.inject_into_ssl()
    _HAS_TRUSTSTORE = True
except Exception:
    _HAS_TRUSTSTORE = False


def log_info(msg):
    print(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")


def log_warn(msg):
    print(f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL} {msg}")


def log_done(msg):
    print(f"{Fore.GREEN}[DONE]{Style.RESET_ALL} {msg}")


def die(msg, code=1):
    print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}", file=sys.stderr)
    sys.exit(code)


# --- config file location --------------------------------------------------
# The script searches for the config in several places (first existing one
# is used for reading):
#   1) path from --config or the PLEX_TOOLS_CONFIG variable
#   2) next to the script:       <script_dir>/plex_tools.config.json
#   3) .config next to script:   <script_dir>/.config/plex_tools/config.json
#      and in PARENT folders     (works from a network/samba drive too - portable)
#   4) home ~/.config:           ~/.config/plex_tools/config.json
# A new config is created NEXT TO THE SCRIPT (portable - travels with it to
# another OS/drive); if a config already exists there, it keeps being used.
CONFIG_FILENAME = "plex_tools.config.json"
_CONFIG_OVERRIDE = None   # set by --config
CONFIG_PATH = None        # current path (resolved at run time)

# Optional remote config: point this at a JSON file (e.g. on your NAS) whose keys
# OVERRIDE the local config (base_url, tokens, …). It's the PRIMARY source when set;
# if it's empty/unreachable/invalid the script falls back to the local file methods
# below. Set via the CONFIG_URL constant, the PLEX_TOOLS_CONFIG_URL env var, or
# --config-url. A short timeout keeps an unreachable URL from stalling startup, and
# the remote can't set the config URL itself (no redirect loop).
CONFIG_URL = "http://nas.falco81.net/plex_tools.config.json"            # e.g. "http://nas.falco81.net/plex_tools.config.json"
CONFIG_FETCH_TIMEOUT = 4   # seconds — keep short so an unreachable URL can't hang startup
_CONFIG_URL_OVERRIDE = None  # set by --config-url
CONFIG_SOURCE = ""         # human-readable description of where the config came from (set by load_config)
# When a remote CONFIG_URL is used, the effective config is also cached to a local
# file (resilience if the URL is unreachable later, and to keep a generated client_id
# stable). Set to False (or pass --no-local-config) for a pure-remote setup that never
# writes a local file — then put client_id (and any tokens) in the remote JSON.
WRITE_LOCAL_CONFIG = True
_NO_LOCAL_CONFIG = False   # set by --no-local-config


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _script_config_path():
    return os.path.join(_script_dir(), CONFIG_FILENAME)


def _xdg_config_dir():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def _dotconfig_paths(base):
    """Candidates inside the .config folder at the given path."""
    return [os.path.join(base, ".config", "plex_tools", "config.json"),
            os.path.join(base, ".config", CONFIG_FILENAME)]


def _config_read_candidates():
    cands = []
    env = _CONFIG_OVERRIDE or os.environ.get("PLEX_TOOLS_CONFIG")
    if env:
        cands.append(os.path.expanduser(env))
    cands.append(_script_config_path())
    # .config next to the script and in parent folders (portable, incl. samba drive)
    d = _script_dir()
    for _ in range(40):
        cands += _dotconfig_paths(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # home ~/.config
    cands += _dotconfig_paths(os.path.expanduser("~"))  # ~/.config/...
    cands.append(os.path.join(_xdg_config_dir(), "plex_tools", "config.json"))
    cands.append(os.path.join(_xdg_config_dir(), CONFIG_FILENAME))
    # dedup, keep order
    seen, out = set(), []
    for p in cands:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_config_path():
    """Resolve the config path (first existing candidate; otherwise the write default)."""
    global CONFIG_PATH
    override = _CONFIG_OVERRIDE or os.environ.get("PLEX_TOOLS_CONFIG")
    if override:
        CONFIG_PATH = os.path.expanduser(override)
        return CONFIG_PATH
    for p in _config_read_candidates():
        if os.path.isfile(p):
            CONFIG_PATH = p
            return p
    # nothing exists -> new one NEXT TO THE SCRIPT (portable; goes with it to a
    # network drive too). If writing next to the script fails, save_config tries ~/.config.
    CONFIG_PATH = _script_config_path()
    return CONFIG_PATH


# streamType in the Plex API
ST_VIDEO, ST_AUDIO, ST_SUBTITLE = 1, 2, 3
SECTION_TYPE_NUM = {"movie": 1, "show": 2}
STATUS_AUTOHIDE = 4.0  # s: how long a status message (e.g. the F5 result) stays before it auto-hides

PRODUCT = "plex_tools"
PLEX_TV = "https://plex.tv"
SIGNIN_URL = f"{PLEX_TV}/api/v2/users/signin"
PINS_URL = f"{PLEX_TV}/api/v2/pins"
HOMEUSERS_URL = f"{PLEX_TV}/api/v2/home/users"
SWITCH_URL = f"{PLEX_TV}/api/home/users/{{uid}}/switch"

# Plex Discover: the account-level watchlist lives here (NOT on the local server).
# Items are Plex online-metadata objects identified by a plex:// guid; the trailing
# id of that guid is the "ratingKey" the watchlist actions expect.
DISCOVER_URL = "https://discover.provider.plex.tv"


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------
# --- keyboard input detection (arrow keys) across platforms ----------------
_WINDOWS = os.name == "nt"
try:
    if _WINDOWS:
        import msvcrt
    else:
        import termios
        import tty
    _HAS_RAW = True
except Exception:
    _HAS_RAW = False

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    return _ANSI_RE.sub("", s)


def _scrollbar_range(nrows, total, visible, top):
    """Thumb (start_row, size) for a vertical scrollbar spanning nrows body rows,
    or None when everything fits (total <= visible). Thumb size ~ visible share,
    position ~ scroll offset."""
    if total <= visible or nrows <= 0:
        return None
    thumb = max(1, min(nrows, round(nrows * visible / total)))
    span = nrows - thumb                       # travel room for the thumb
    denom = max(1, total - visible)            # number of scroll positions
    tstart = max(0, min(span, round(span * (top / denom))))
    return (tstart, thumb)


def _with_scrollbar(line, cols, r, sbr):
    """Append a scrollbar cell to a body line at the last column (col `cols`).
    r = 0-based row within the visible body; sbr = (tstart, thumb) or None.
    The line is padded (spaces don't count color codes) so the cell lands on the
    right edge; the trailing CRLF after each body line prevents an autowrap."""
    if sbr is None:
        return line
    tstart, thumb = sbr
    if tstart <= r < tstart + thumb:
        cell = f"{Fore.CYAN}{Style.BRIGHT}\u2588{Style.RESET_ALL}"   # thumb (full block)
    else:
        cell = f"{Style.DIM}\u2502{Style.RESET_ALL}"                 # track (light vertical)
    pad = max(1, (cols - 1) - len(strip_ansi(line)))
    return line + (" " * pad) + cell


def _read_key(timeout=None):
    """Read a single key. Returns an action string or ('char', char) with a
    proper Unicode character (incl. Czech diacritics). With timeout (seconds),
    returns 'timeout' if nothing arrives in time. Works on Windows and Linux."""
    if _WINDOWS:
        if timeout is not None:
            end = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= end:
                    return "timeout"
                time.sleep(0.02)
        ch = msvcrt.getwch()  # wide char -> correct Unicode input (incl. diacritics)
        if ch in ("\x00", "\xe0"):
            c2 = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right",
                    "G": "home", "O": "end", "I": "pgup", "Q": "pgdn",
                    "?": "f5", "C": "f9", "S": "delete"}.get(c2, "other")  # F5 = 0x3F '?', F9 = 0x43 'C'

        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\t":
            return "tab"
        if ch == "\x08":
            return "backspace"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch and ord(ch) >= 32:
            return ("char", ch)
        return "other"
    else:
        import select
        fd = sys.stdin.fileno()
        if timeout is not None:
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return "timeout"
        b0 = os.read(fd, 1)
        if not b0:
            return "other"
        c = b0[0]
        if c == 0x1b:  # ESC alone, or start of an escape sequence (arrows, ...)
            r, _, _ = select.select([fd], [], [], 0.05)  # follow-up byte within 50 ms?
            if not r:
                return "esc"
            c1 = os.read(fd, 1)
            if c1 not in (b"[", b"O"):
                return "esc"
            seq = ""
            while True:
                nb = os.read(fd, 1)
                if not nb:
                    break
                seq += nb.decode("latin-1")
                if nb.isalpha() or nb == b"~" or len(seq) > 6:
                    break
            return {"A": "up", "B": "down", "C": "right", "D": "left",
                    "H": "home", "F": "end", "1~": "home", "4~": "end",
                    "3~": "delete", "5~": "pgup", "6~": "pgdn", "15~": "f5",
                    "20~": "f9"}.get(seq, "esc")
        if c in (0x0d, 0x0a):
            return "enter"
        if c == 0x09:
            return "tab"
        if c in (0x7f, 0x08):
            return "backspace"
        if c == 0x03:
            raise KeyboardInterrupt
        if c < 0x80:
            return ("char", chr(c))
        # UTF-8 multi-byte: read continuation bytes based on the lead byte
        if c >= 0xf0:
            n = 3
        elif c >= 0xe0:
            n = 2
        elif c >= 0xc0:
            n = 1
        else:
            return "other"  # stray continuation byte
        rest = b""
        for _ in range(n):
            rb = os.read(fd, 1)
            if not rb:
                break
            rest += rb
        try:
            return ("char", (b0 + rest).decode("utf-8"))
        except Exception:
            return "other"


class _RawMode:
    """Context manager for terminal raw mode (Unix only)."""
    def __enter__(self):
        if not _WINDOWS:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        return self

    def __exit__(self, *a):
        if not _WINDOWS:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _tui_supported():
    return _HAS_RAW and sys.stdin.isatty() and sys.stdout.isatty()


def clear_screen():
    """Clear the screen (app mode). Does nothing without a TTY."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def interactive_menu(prompt, labels, default=0, allow_cancel=False, page=None,
                     refresh_cb=None, header=None, refresh_all_cb=None, tags=None):
    """Arrow-key menu + type-to-search. Returns an index, or None (cancelled).
    Behaves like an app: clears the screen up front and redraws only the current
    view (no scrolling). header = context lines above the list.
    tags = optional list (same length as labels) of short status strings shown in
    an aligned column on the right (e.g. '[movie]'); the cursor highlight extends
    across them. Pre-colour a tag to tint the column; the selected row overrides it.
    refresh_cb(current_index) -> message (str): called on F5 (e.g. library scan)."""
    if not _tui_supported():
        for h in (header or []):
            print(h)
        return _ask_choice_classic(prompt, labels, default)

    n = len(labels)
    plain = [strip_ansi(l) for l in labels]
    tag_plain = [strip_ansi(t) if t else "" for t in (tags or [])]
    has_tags = any(tag_plain)
    header = list(header or [])
    filt = ""
    status = ""  # status line (e.g. F5 result) - shown INSIDE the window
    sel_pos = default if 0 <= default < n else 0
    prev_lines = 0  # number of lines in the previous frame
    first = True    # first render clears the screen

    def visible_order():
        if not filt:
            return list(range(n))
        return [i for i in range(n) if _match_filter(plain[i], filt)]

    def term_size():
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def trunc(s, width):
        if len(s) > width:
            return s[:max(1, width - 1)] + "…"
        return s

    def render(order, sel_pos):
        nonlocal prev_lines, first
        cols, rows_total = term_size()
        maxw = max(10, cols - 2)
        # how many rows fit: reserve prompt+hint+footer+status(+2 indicators)+header
        reserve = 6 + len(header)
        page_rows = max(3, rows_total - reserve)

        buf = []
        if first:
            buf.append("\x1b[2J\x1b[H")  # clear screen + cursor home (app mode)
            first = False
        elif prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")

        vis_lines = []  # individual frame lines (without \n)
        for h in header:
            sp = strip_ansi(h)
            vis_lines.append(h if len(sp) <= maxw else trunc(sp, maxw))
        vis_lines.append(f"{Fore.YELLOW}{trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        if filt:
            hint = "↑↓ move · Enter = select · Esc = clear search"
        elif allow_cancel:
            hint = "↑↓ move · type = search · Enter = select · Esc = back"
        else:
            hint = "↑↓ move · type = search · Enter = select"
        if refresh_cb is not None:
            hint += " · F5 = scan library"
        if refresh_all_cb is not None:
            hint += " · F9 = refresh all metadata"
        vis_lines.append(f"{Fore.CYAN}{trunc(hint, maxw)}{Style.RESET_ALL}")

        if not order:
            vis_lines.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
            start = max(0, min(sel_pos - page_rows // 2, len(order) - page_rows))
            window = order[start:start + page_rows]
            sbr = _scrollbar_range(len(window), len(order), len(window), start)
            if start > 0:
                vis_lines.append(f"  {Fore.CYAN}▲ ({start} above){Style.RESET_ALL}")
            # align a trailing tag column (e.g. [movie]/[show]) next to the longest
            # visible title that carries one, so the tags line up
            win_tags = has_tags and any(tag_plain[i] for i in window)
            if win_tags:
                statw = max((len(tag_plain[i]) for i in window if tag_plain[i]), default=0)
                longest = max((len(plain[i]) for i in window if tag_plain[i]), default=0)
                gap = 2
                textw = max(6, min(maxw - 4 - (gap + statw), longest + 1))

            def fit(s, w):
                return (s[:max(1, w - 1)] + "…") if len(s) > w else s + " " * (w - len(s))

            for pos, i in enumerate(window, start):
                if win_tags and tag_plain[i]:
                    body = fit(plain[i], textw)
                    tail = f"{' ' * gap}{tags[i]}"          # tag keeps its own colour
                    seltail = f"{' ' * gap}{tag_plain[i]}"  # …but turns green when selected
                else:
                    body = trunc(plain[i], maxw - 2)
                    tail = seltail = ""
                if pos == sel_pos:
                    line = f"{Fore.GREEN}{Style.BRIGHT}› {body}{seltail}{Style.RESET_ALL}"
                else:
                    line = f"  {body}{tail}"
                vis_lines.append(_with_scrollbar(line, cols, pos - start, sbr))
            rest = len(order) - (start + len(window))
            if rest > 0:
                vis_lines.append(f"  {Fore.CYAN}▼ ({rest} below){Style.RESET_ALL}")

        pos_info = f" [{sel_pos + 1}/{len(order)}]" if order else ""
        if filt:
            footer = f"{Fore.MAGENTA}{trunc('Search: ' + filt + pos_info, maxw)}{Style.RESET_ALL}"
        else:
            footer = f"{Fore.CYAN}{trunc('(type to search)' + pos_info, maxw)}{Style.RESET_ALL}"
        vis_lines.append(footer)
        # status line inside the window (redraws in place, no extra lines)
        if status:
            sp = strip_ansi(status)
            vis_lines.append(status if len(sp) <= maxw else trunc(sp, maxw))

        # \r\n (not just \n) for raw mode on Linux where \n does not return to column 0
        buf.append("\r\n".join(vis_lines))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis_lines)
        return page_rows

    with _RawMode():
        order = visible_order()
        if sel_pos >= len(order):
            sel_pos = max(0, len(order) - 1)
        page_rows = render(order, sel_pos)
        while True:
            # while a status message is showing (e.g. the F5 result), read with a
            # timeout so it disappears on its own even without a keypress
            key = _read_key(STATUS_AUTOHIDE if status else None)
            if key == "timeout":
                status = ""
                render(order, sel_pos)
                continue
            if key not in ("f5", "f9") and status:
                status = ""  # status message also disappears on any keypress
            if key == "f9" and refresh_all_cb is not None:
                idx = order[sel_pos] if order else None
                try:
                    status = refresh_all_cb(idx) or ""
                except Exception as ex:
                    status = f"{Fore.RED}Refresh All Metadata failed: {ex}{Style.RESET_ALL}"
                first = True          # the progress screen overwrote everything
                render(order, sel_pos)
                continue
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
            elif key == "f5" and refresh_cb is not None:
                cur = order[sel_pos] if order else None
                status = f"{Fore.CYAN}Running Scan Library Files…{Style.RESET_ALL}"
                render(order, sel_pos)  # immediate feedback (in place)
                try:
                    status = refresh_cb(cur) or ""
                except Exception as ex:
                    status = f"{Fore.RED}Scan Library Files failed: {ex}{Style.RESET_ALL}"
                # final render (at loop end) shows the result in the same window
            elif key in ("enter", "right"):
                if order:
                    result = order[sel_pos]
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return result
            elif key in ("esc", "left"):
                if filt:
                    filt = ""
                    order = visible_order()
                    sel_pos = 0
                elif allow_cancel:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return None
            elif key == "backspace":
                if filt:
                    filt = filt[:-1]
                    order = visible_order()
                    sel_pos = 0
            elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                filt += key[1]
                order = visible_order()
                sel_pos = 0
            page_rows = render(order, sel_pos)


def _match_filter(text, filt):
    """Case-insensitive match. Supports '*' and '?' wildcards; a plain string is a
    substring match (so 'roman' still works like before)."""
    if not filt:
        return True
    t, f = text.lower(), filt.lower()
    if any(c in f for c in "*?["):
        import fnmatch
        pat = f if f.startswith("*") or f.startswith("?") else f
        return fnmatch.fnmatch(t, pat) or fnmatch.fnmatch(t, f"*{pat}")
    return f in t


def ask_line(prompt, default="", allow_empty=False):
    """One-line text input with Esc = cancel (returns None). Handles Unicode."""
    if not _tui_supported():
        try:
            raw = input(f"{strip_ansi(prompt)}{f' [{default}]' if default else ''}: ").strip()
        except EOFError:
            return None
        return raw or (default if default else (None if not allow_empty else ""))
    buf = default
    sys.stdout.write(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} "
                     f"{Style.DIM}(Enter = ok · Esc = back){Style.RESET_ALL}\r\n")
    sys.stdout.flush()
    with _RawMode():
        while True:
            sys.stdout.write(f"\r\x1b[K> {buf}")
            sys.stdout.flush()
            k = _read_key()
            if k == "esc":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return None
            if k == "enter":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                if buf.strip() or allow_empty:
                    return buf.strip()
                return None
            if k == "backspace":
                buf = buf[:-1]
            elif isinstance(k, tuple) and k[0] == "char" and k[1].isprintable():
                buf += k[1]


def strip_ansi_len(s):
    return len(strip_ansi(s))


def _wrap_hint(text, width):
    """Word-wrap a coloured hint/header line to a visible width WITHOUT truncating.
    Breaks on ' · ' separators first, then on spaces, then hard-cuts a single over-long
    token. The line's leading colour is re-applied on each continuation line so the
    whole thing keeps its tint. Returns a list of lines, each with visible width
    <= `width` (so the menu's line-based redraw stays correct)."""
    width = max(8, width)
    if strip_ansi_len(text) <= width:
        return [text]
    m = re.match(r"^((?:\x1b\[[0-9;]*m)+)", text)
    lead = m.group(1) if m else ""
    reset = Style.RESET_ALL if lead else ""
    # tokens: ' · '-delimited pieces (separator kept), over-long ones split on spaces
    tokens = []
    parts = text.split(" · ")
    for i, p in enumerate(parts):
        tok = p + (" · " if i < len(parts) - 1 else "")
        if strip_ansi_len(tok) <= width:
            tokens.append(tok)
        else:
            tokens += [w for w in re.split(r"(\s+)", tok) if w]
    lines, cur = [], ""
    for tok in tokens:
        if cur and strip_ansi_len(cur) + strip_ansi_len(tok) > width:
            lines.append(cur.rstrip())
            cur = ""
        if not cur and strip_ansi_len(tok) > width:   # lone token still too long
            plain = strip_ansi(tok)
            for j in range(0, len(plain), width):
                lines.append(plain[j:j + width])
            continue
        cur += tok
    if cur.strip():
        lines.append(cur.rstrip())
    out = []
    for k, ln in enumerate(lines):
        out.append((lead if (lead and k > 0) else "") + ln + reset)
    return out or [text]


def _checkbox_classic(prompt, rows, header=None):
    """Fallback multi-select for non-TTY: number toggles, a/n/w/u group selects."""
    for h in (header or []):
        print(strip_ansi(h))
    while True:
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}")
        for i, r in enumerate(rows):
            box = "[x]" if r.get("selected") else "[ ]"
            tag = ""
            if "watched" in r:
                tag = "  (watched)" if r["watched"] else "  (unwatched)"
            print(f"  {i + 1:>3}) {box} {strip_ansi(r['label'])}{tag}")
        raw = input("Number=toggle  a=all  n=none  w=watched  u=unwatched  Enter=confirm  q=cancel: ").strip().lower()
        if raw == "q":
            return None
        if raw == "":
            return rows
        if raw == "a":
            for r in rows:
                r["selected"] = True
        elif raw == "n":
            for r in rows:
                r["selected"] = False
        elif raw == "w":
            for r in rows:
                if r.get("watched"):
                    r["selected"] = True
        elif raw == "u":
            for r in rows:
                if "watched" in r and not r["watched"]:
                    r["selected"] = True
        elif raw.isdigit() and 1 <= int(raw) <= len(rows):
            r = rows[int(raw) - 1]
            r["selected"] = not r.get("selected")


def checkbox_menu(prompt, rows, header=None, start_pos=0, pos_out=None, tristate=False,
                  ui_state=None, scope_picker=None, action_cb=None, f9_cb=None,
                  edit_cb=None, editable=None, on_edit=None, find_cb=None,
                  country_picker=None, export_cb=None):
    """Smart app-like multi-select with [x]/[ ] checkboxes.
    tristate=True: rows carry 'state' ("on"/"off"/"mixed") instead of 'selected';
    Space cycles on -> off -> (mixed, if that was the original state) and the box
    shows [x]/[ ]/[~]. Used for "which labels should these items have".
    rows: dicts with 'label', 'selected', optional 'watched' (bool status tag) and
    optional 'season' (int, enables season filtering). Group-select actions act on
    the CURRENTLY VISIBLE rows (respecting the active season/search filter):
      Space=toggle current, a=all, n=none, w=watched, u=unwatched, i=invert,
      [ / ]=previous/next season, /=search (type, Enter/Esc exit), Enter=confirm,
      Esc=clear filter or cancel. Returns rows (with 'selected' set) or None.
    start_pos restores the highlighted row; if pos_out is a list, the final
    highlighted row index is written to pos_out[0] (so a caller can come back to it).
    ui_state (dict): remembers the view across calls - active search filter, season
    scope and highlighted row - so stepping back into this menu looks exactly as it
    was left (not reset to defaults).
    scope_picker: callable() -> (name, set_of_row_keys) or None. Bound to 'l' and
    used to narrow the list to a group (e.g. every show carrying one label)."""
    if not _tui_supported():
        return _checkbox_classic(prompt, rows, header)

    plain = [strip_ansi(r["label"]) for r in rows]

    def _sync_plain():
        """Row labels can change while the menu is open (e.g. after re-reading the
        metadata language), so refresh the plain-text copy before using it."""
        if len(plain) != len(rows):
            plain[:] = [strip_ansi(r["label"]) for r in rows]
            return
        for i, r in enumerate(rows):
            plain[i] = strip_ansi(r["label"])

    header = list(header or [])
    seasons = sorted({r["season"] for r in rows if r.get("season") is not None})
    scopes = [None] + seasons            # None = all seasons
    scope_idx = 0
    filt = ""
    search_mode = False      # typing a view filter after '/'
    mark_mode = None         # "+" / "-": typing a pattern to check / uncheck
    mark_buf = ""            # the pattern being typed for +/-
    edit_i = None            # index into `editable` while editing a row in place
    edit_buf = ""            # the text being typed
    edit_pos = 0             # caret position inside edit_buf
    edit_row = None          # the row being edited
    edit_undo = None         # its values before the edit, for Esc
    ext_scope = None         # ("label X", {rk, ...}) from scope_picker, or None
    want_row = max(0, min(start_pos, len(rows) - 1)) if rows else 0
    if isinstance(ui_state, dict):     # restore the view exactly as it was left
        filt = ui_state.get("filt", "")
        if ui_state.get("scope", None) in scopes:
            scope_idx = scopes.index(ui_state.get("scope"))
        want_row = ui_state.get("row", want_row)
        ext_scope = ui_state.get("ext_scope")
    sel_pos = 0
    prev_lines = 0
    first = True

    def _remember():
        row = order[sel_pos] if order else 0
        if pos_out is not None:
            pos_out[:] = [row]  # remember the ROW index
        if isinstance(ui_state, dict):
            ui_state["filt"] = filt
            ui_state["scope"] = cur_season()
            ui_state["row"] = row
            ui_state["ext_scope"] = ext_scope


    def cur_season():
        return scopes[scope_idx] if scopes else None

    def is_header(r):
        return bool(r.get("header"))

    def group_eps(gid):
        return [i for i, r in enumerate(rows) if not is_header(r) and r.get("season") == gid]

    def header_eps(r):
        if r.get("master"):
            return [i for i, rr in enumerate(rows) if not is_header(rr)]
        return group_eps(r.get("season"))

    def visible_order():
        _sync_plain()
        s = cur_season()
        pat = filt
        out = []
        for i, r in enumerate(rows):
            if s is not None and r.get("season") != s:
                continue
            if ext_scope is not None and not is_header(r) and str(r.get("rk")) not in ext_scope[1]:
                continue
            if pat and not (is_header(r) or _match_filter(plain[i], pat)):
                continue
            out.append(i)
        return out

    def mark_matches(order):
        """Row indices (of the visible ones) matching the +/- pattern."""
        if not mark_buf:
            return []
        return [i for i in order
                if not is_header(rows[i]) and not rows[i].get("action")
                and _match_filter(plain[i], mark_buf)]

    def apply_mark(order):
        """Apply the +/- pattern once (on Enter), like the file picker does."""
        on = mark_mode == "+"
        hits = mark_matches(order)
        for i in hits:
            if tristate:
                rows[i]["state"] = "on" if on else "off"
            else:
                rows[i]["selected"] = on
        return len(hits)

    def term_size():
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def trunc(s, width):
        return s[:max(1, width - 1)] + "…" if len(s) > width else s

    def render(order, sel_pos):
        nonlocal prev_lines, first
        _sync_plain()
        cols, rows_total = term_size()
        maxw = max(10, cols - 2)

        # chrome = everything above the item list; header + hint lines are WRAPPED to
        # the window width (never truncated), and the page size is derived from the
        # chrome's real height so wrapped lines don't push items off-screen.
        chrome = []
        for h in header:
            chrome += _wrap_hint(h, maxw)
        n_eps = sum(1 for r in rows if not is_header(r))
        if tristate:
            n_sel = sum(1 for r in rows if not is_header(r) and r.get("state") == "on")
        else:
            n_sel = sum(1 for r in rows if not is_header(r) and r.get("selected"))
        chrome.append(f"{Fore.YELLOW}{trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        if tristate:
            hint_line = "Space cycle [x]/[ ]/[~] · a all on · n all off · r reset"
        else:
            hint_line = "Space toggle · a all · n none · w watched · u unwatched · i invert"
        for ln in _wrap_hint(hint_line, maxw):
            chrome.append(f"{Fore.CYAN}{ln}{Style.RESET_ALL}")
        s = cur_season()
        scope_txt = "all seasons" if s is None else f"S{int(s):02d}"
        has_seasons = len(scopes) > 1
        line2 = ((f"[ / ] change season ({scope_txt}) · " if has_seasons else "") +
                 "/ search · +/- check/uncheck by pattern"
                 + (" · l label" if scope_picker is not None else "")
                 + (" · c country" if country_picker is not None else "")
                 + (" · x export" if export_cb is not None else "")
                 + (" · F9 refresh checked" if f9_cb is not None else "")
                 + (" · e edit" if (edit_cb is not None or editable) else "")
                 + (" · f find match" if find_cb is not None else "")
                 + " · Enter apply · Esc "
                 + ("clear" if (filt or s is not None or ext_scope is not None) else "cancel"))
        for ln in _wrap_hint(line2, maxw):
            chrome.append(f"{Fore.CYAN}{ln}{Style.RESET_ALL}")
        if ext_scope is not None:
            for ln in _wrap_hint(ext_scope[0] + "   (Esc clears)", maxw):
                chrome.append(f"{Fore.GREEN}{ln}{Style.RESET_ALL}")
        if edit_i is not None and editable:
            fname = editable[edit_i][1]
            for ln in _wrap_hint(f"editing {fname} - type to change · ←/→ Home/End move · "
                                 f"Backspace/Del erase · Tab/↑↓ switch field · "
                                 f"Enter keep · Esc cancel", maxw):
                chrome.append(f"{Fore.MAGENTA}{ln}{Style.RESET_ALL}")
        if mark_mode:
            verb = "check" if mark_mode == "+" else "uncheck"
            nmatch = len(mark_matches(order))
            prompt_txt = (f"{verb} matching: {mark_buf}\u2588   ({nmatch} match)  "
                          f"Enter = apply · Esc = cancel")
            chrome.append(f"{Fore.MAGENTA}{trunc(prompt_txt, maxw)}{Style.RESET_ALL}")
        if search_mode or filt:
            chrome.append(f"{Fore.MAGENTA}"
                          f"{trunc('search: ' + filt + ('_' if search_mode else ''), maxw)}"
                          f"{Style.RESET_ALL}")

        # footer (1) + possible up/down arrow indicators (2) reserved below the list
        page_rows = max(3, rows_total - len(chrome) - 3)
        buf = []
        if first:
            buf.append("\x1b[2J\x1b[H")
            first = False
        elif prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")
        vis = list(chrome)
        if not order:
            vis.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
            # fixed status column so watched/unwatched and other tags line up
            has_tag = any(r.get("tag") for r in rows)
            has_watch = any("watched" in r for r in rows)
            # align to the longest title that actually carries a tag, so the column
            # stays next to the text instead of drifting to the right edge
            longest = max((len(plain[i]) for i in order
                           if not is_header(rows[i])
                           and (rows[i].get("tag") or "watched" in rows[i])),
                          default=0)
            statw = len("unwatched") if has_watch else 0
            if has_tag:
                statw = max(statw, max(len(strip_ansi(r.get("tag") or "")) for r in rows))
                statw = min(statw, max(10, maxw // 2))     # never eat the whole line
            statgap = 2
            # keep the tag column right after the longest title instead of pushing it
            # to the far right edge, so the eye doesn't have to travel
            textw = max(6, min(maxw - 8 - (statgap + statw), longest + 1))

            def fit(s, w):
                if len(s) > w:
                    return s[:max(1, w - 1)] + "…"
                return s + " " * (w - len(s))

            # rows may carry extra sub-lines (r["sub"]), so build the window by
            # accumulating real display heights instead of counting items
            heights = [1 + len(rows[i].get("sub") or []) for i in order]
            start = end = sel_pos
            total = heights[sel_pos] if order else 0
            half = max(1, page_rows // 2)
            # keep the cursor near the middle: fill upwards first, then downwards,
            # then use whatever space is left above
            while start > 0 and total + heights[start - 1] <= half:
                start -= 1
                total += heights[start]
            while end + 1 < len(order) and total + heights[end + 1] <= page_rows:
                end += 1
                total += heights[end]
            while start > 0 and total + heights[start - 1] <= page_rows:
                start -= 1
                total += heights[start]
            window = order[start:end + 1]
            sbr = _scrollbar_range(len(window), len(order), len(window), start)
            if start > 0:
                vis.append(f"  {Fore.CYAN}▲ ({start} above){Style.RESET_ALL}")
            for pos, i in enumerate(window, start):
                r = rows[i]
                cursor = pos == sel_pos
                if is_header(r):
                    eps = header_eps(r)
                    cnt = sum(1 for j in eps if rows[j].get("selected"))
                    box = "[x]" if (eps and cnt == len(eps)) else ("[~]" if cnt else "[ ]")
                    text = trunc(plain[i], max(1, maxw - 4 - (2 if sbr else 0)))
                    if cursor:
                        line = f"{Fore.GREEN}{Style.BRIGHT}› {box} {text}{Style.RESET_ALL}"
                    else:
                        line = f"  {Fore.CYAN}{Style.BRIGHT}{box} {text}{Style.RESET_ALL}"
                else:
                    if tristate:
                        st_ = r.get("state", "off")
                        if r.get("action"):      # action rows aren't states -> no checkbox
                            box = " » "
                        else:
                            box = {"on": "[x]", "off": "[ ]", "mixed": "[~]"}[st_]
                    else:
                        box = "[x]" if r.get("selected") else "[ ]"
                    if r.get("tag"):
                        tag = f"{' ' * statgap}{r['tag']}"
                    elif "watched" in r:
                        word = "watched" if r["watched"] else "unwatched"
                        color = Fore.GREEN if r["watched"] else Style.DIM
                        tag = f"{' ' * statgap}{color}{word}{Style.RESET_ALL}"
                    else:
                        tag = ""      # no tag -> no trailing padding
                    if r.get("tag") or "watched" in r:
                        text = fit(plain[i], textw)   # pad so the tag column lines up
                    else:
                        text = trunc(plain[i], max(6, maxw - 8))   # no tag -> full width
                    hit = bool(mark_mode and mark_buf and _match_filter(plain[i], mark_buf))
                    if hit:      # show what the +/- pattern will affect
                        line = f"    {Fore.MAGENTA}{box} {text}{Style.RESET_ALL}{tag}"
                    elif cursor:     # highlight the whole row, tag included
                        line = (f"{Fore.GREEN}{Style.BRIGHT}›   {box} {text}"
                                f"{strip_ansi(tag)}{Style.RESET_ALL}")
                    else:
                        on_ = (r.get("state") == "on") if tristate else r.get("selected")
                        boxc = f"{Fore.GREEN}{box}{Style.RESET_ALL}" if on_ else box
                        line = f"    {boxc} {text}{tag}"
                vis.append(_with_scrollbar(line, cols, pos - start, sbr))
                for sub in (r.get("sub") or []):
                    sp = strip_ansi(sub)
                    vis.append("        " + (sub if len(sp) <= maxw - 8
                                             else trunc(sp, maxw - 8)))
            rest = len(order) - (start + len(window))
            if rest > 0:
                vis.append(f"  {Fore.CYAN}▼ ({rest} below){Style.RESET_ALL}")
        info = f" [{sel_pos + 1}/{len(order)}]" if order else ""
        vis.append(f"{Fore.MAGENTA}{trunc(f'selected {n_sel}/{n_eps} (shown {len(order)})' + info, maxw)}{Style.RESET_ALL}")
        buf.append("\r\n".join(vis))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)

    with _RawMode():
        order = visible_order()
        sel_pos = order.index(want_row) if want_row in order else 0
        render(order, sel_pos)
        while True:
            key = _read_key()
            if edit_i is not None:             # editing a value right in the list
                fields = editable or []

                def _sync(done=False):
                    """Push the buffer (and caret) into the row so the caller can
                    draw the value with a cursor inside it."""
                    edit_row[fields[edit_i][0]] = edit_buf
                    if done:
                        edit_row.pop("_edit_field", None)
                        edit_row.pop("_edit_pos", None)
                    else:
                        edit_row["_edit_field"] = fields[edit_i][0]
                        edit_row["_edit_pos"] = edit_pos
                    if on_edit:
                        on_edit(edit_row)

                if key == "esc":
                    if edit_undo:
                        for k, v in edit_undo.items():
                            edit_row[k] = v
                    edit_row.pop("_edit_field", None)
                    edit_row.pop("_edit_pos", None)
                    if on_edit:
                        on_edit(edit_row)
                    edit_i, edit_buf, edit_row, edit_undo = None, "", None, None
                elif key == "enter":
                    _sync(done=True)
                    edit_i, edit_buf, edit_row, edit_undo = None, "", None, None
                elif key in ("tab", "down", "up") and len(fields) > 1:
                    _sync()                       # keep what was typed
                    step = -1 if key == "up" else 1
                    edit_i = (edit_i + step) % len(fields)     # cycle both ways
                    edit_buf = str(edit_row.get(fields[edit_i][0]) or "")
                    edit_pos = len(edit_buf)
                    _sync()
                elif key == "left":
                    edit_pos = max(0, edit_pos - 1); _sync()
                elif key == "right":
                    edit_pos = min(len(edit_buf), edit_pos + 1); _sync()
                elif key == "home":
                    edit_pos = 0; _sync()
                elif key == "end":
                    edit_pos = len(edit_buf); _sync()
                elif key == "backspace":
                    if edit_pos > 0:
                        edit_buf = edit_buf[:edit_pos - 1] + edit_buf[edit_pos:]
                        edit_pos -= 1
                    _sync()
                elif key == "delete":
                    edit_buf = edit_buf[:edit_pos] + edit_buf[edit_pos + 1:]
                    _sync()
                elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                    edit_buf = edit_buf[:edit_pos] + key[1] + edit_buf[edit_pos:]
                    edit_pos += 1
                    _sync()
                order = visible_order()
                render(order, sel_pos)
                continue
            if mark_mode:                      # typing a +/- pattern (list stays put)
                if key == "esc":
                    mark_mode, mark_buf = None, ""
                elif key == "enter":
                    if mark_buf.strip():
                        apply_mark(order)
                    mark_mode, mark_buf = None, ""
                elif key == "backspace":
                    mark_buf = mark_buf[:-1]
                elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                    mark_buf += key[1]
                render(order, sel_pos)
                continue
            if search_mode:
                if key in ("enter", "esc"):
                    if key == "esc":
                        filt = ""
                    search_mode = False
                    order = visible_order()
                    sel_pos = 0
                elif key == "backspace":
                    filt = filt[:-1]
                    order = visible_order()
                    sel_pos = 0
                elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                    filt += key[1]
                    order = visible_order()
                    sel_pos = 0
                render(order, sel_pos)
                continue
            # --- command mode ---
            if key == "up" and order:
                sel_pos = (sel_pos - 1) % len(order)
            elif key == "down" and order:
                sel_pos = (sel_pos + 1) % len(order)
            elif key == "pgup" and order:
                sel_pos = max(0, sel_pos - 10)
            elif key == "pgdn" and order:
                sel_pos = min(len(order) - 1, sel_pos + 10)
            elif key == "home":
                sel_pos = 0
            elif key == "end" and order:
                sel_pos = len(order) - 1
            elif key == ("char", " ") and order:
                r = rows[order[sel_pos]]
                if is_header(r):
                    eps = header_eps(r)
                    all_on = bool(eps) and all(rows[j].get("selected") for j in eps)
                    for j in eps:
                        rows[j]["selected"] = not all_on
                elif tristate:
                    cur = r.get("state", "off")
                    orig = r.get("orig", "off")
                    cycle = ["on", "off", "mixed"] if orig == "mixed" else ["on", "off"]
                    r["state"] = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
                else:
                    r["selected"] = not r.get("selected")
                if not tristate:  # advance for fast multi-select; tri-state cycles in place
                    sel_pos = min(sel_pos + 1, len(order) - 1)
            elif key == ("char", "a"):
                for i in order:
                    if not is_header(rows[i]):
                        if tristate:
                            rows[i]["state"] = "on"
                        else:
                            rows[i]["selected"] = True
            elif key == ("char", "n"):
                for i in order:
                    if not is_header(rows[i]):
                        if tristate:
                            rows[i]["state"] = "off"
                        else:
                            rows[i]["selected"] = False
            elif key == ("char", "r") and tristate:
                for i in order:  # reset to the original state
                    if not is_header(rows[i]):
                        rows[i]["state"] = rows[i].get("orig", "off")
            elif key == ("char", "w"):
                for i in order:
                    if rows[i].get("watched"):
                        rows[i]["selected"] = True
            elif key == ("char", "u"):
                for i in order:
                    if "watched" in rows[i] and not rows[i]["watched"]:
                        rows[i]["selected"] = True
            elif key == ("char", "i"):
                for i in order:
                    if not is_header(rows[i]):
                        rows[i]["selected"] = not rows[i].get("selected")
            elif key in (("char", "]"), ("char", "[")) and len(scopes) > 1:
                scope_idx = (scope_idx + (1 if key == ("char", "]") else -1)) % len(scopes)
                order = visible_order()
                sel_pos = 0
            elif key == ("char", "e") and editable and order:
                r = rows[order[sel_pos]]
                if not is_header(r) and not r.get("action"):
                    edit_row = r
                    edit_undo = {k: r.get(k) for k, _ in editable}
                    edit_i = 0
                    edit_buf = str(r.get(editable[0][0]) or "")
                    edit_pos = len(edit_buf)
                    r["_edit_field"] = editable[0][0]
                    r["_edit_pos"] = edit_pos
                    if on_edit:
                        on_edit(r)
            elif key == ("char", "f") and find_cb is not None and order:
                r = rows[order[sel_pos]]
                if not is_header(r) and not r.get("action"):
                    try:
                        find_cb(r)
                    except Exception as ex:
                        log_warn(f"Failed: {ex}")
                    order = visible_order()
                    sel_pos = min(sel_pos, max(0, len(order) - 1))
                    first = True
            elif key == ("char", "e") and edit_cb is not None:
                if order:
                    try:
                        edit_cb(rows[order[sel_pos]])
                    except Exception as ex:
                        log_warn(f"Failed: {ex}")
                    order = visible_order()
                    sel_pos = min(sel_pos, max(0, len(order) - 1))
                    first = True
            elif key == "f9" and f9_cb is not None:
                checked = [r for r in rows if (r.get("state") == "on") if tristate] \
                          if tristate else [r for r in rows if r.get("selected")]
                if order:      # plus the row under the cursor
                    cur_row = rows[order[sel_pos]]
                    if not is_header(cur_row) and cur_row not in checked:
                        checked = checked + [cur_row]
                try:
                    f9_cb(checked)
                except Exception as ex:
                    log_warn(f"Failed: {ex}")
                order = visible_order()
                sel_pos = min(sel_pos, max(0, len(order) - 1))
                first = True          # the progress screen overwrote everything
            elif key == ("char", "m") and action_cb is not None:
                # the checked rows plus the one under the cursor (deduped, in order)
                picked_now = [r for r in rows
                              if (r.get("state") == "on") if tristate] if tristate \
                             else [r for r in rows if r.get("selected")]
                if order:
                    cur_row = rows[order[sel_pos]]
                    if not is_header(cur_row) and cur_row not in picked_now:
                        picked_now = picked_now + [cur_row]
                try:
                    action_cb(picked_now)
                except Exception as ex:
                    log_warn(f"Failed: {ex}")
                order = visible_order()
                sel_pos = min(sel_pos, max(0, len(order) - 1))
                first = True          # the action drew over the screen
            elif key == ("char", "l") and scope_picker is not None:
                picked_scope = scope_picker()
                if picked_scope is not None:
                    ext_scope = picked_scope
                    order = visible_order()
                    sel_pos = 0
                first = True   # the picker drew over the screen -> full redraw
            elif key == ("char", "c") and country_picker is not None:
                picked_scope = country_picker()
                if picked_scope is not None:
                    ext_scope = picked_scope
                    order = visible_order()
                    sel_pos = 0
                first = True
            elif key == ("char", "x") and export_cb is not None:
                shown = [rows[i] for i in order
                         if not is_header(rows[i]) and not rows[i].get("action")]
                try:
                    export_cb(shown, ext_scope[0] if ext_scope else None)
                except Exception as ex:
                    log_warn(f"Export failed: {ex}")
                first = True
            elif key == ("char", "/"):
                search_mode = True
                filt = ""
                order = visible_order()
                sel_pos = 0
            elif key in (("char", "+"), ("char", "-")):
                mark_mode, mark_buf = key[1], ""
            elif key == "enter":
                if tristate and order and rows[order[sel_pos]].get("action"):
                    rows[order[sel_pos]]["state"] = "on"   # Enter on a » row runs it
                _remember()
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return rows
            elif key == "esc":
                if filt or cur_season() is not None or ext_scope is not None:
                    filt = ""
                    scope_idx = 0
                    ext_scope = None
                    order = visible_order()
                    sel_pos = 0
                else:
                    _remember()
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return None
            render(order, sel_pos)


def _ask_choice_classic(prompt, labels, default=0):
    """Numbered menu fallback for terminals without TTY/arrow-key support."""
    def _show():
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}")
        for i, l in enumerate(labels):
            mark = f" {Fore.CYAN}(default){Style.RESET_ALL}" if i == default else ""
            print(f"  {i + 1}) {l}{mark}")

    _show()
    while True:
        raw = input(f"Choice [1-{len(labels)}, Enter = {default + 1}]: ").strip()
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw) - 1
        print(f"{Fore.RED}Invalid choice, try again.{Style.RESET_ALL}")


def ask_choice(prompt, labels, default=0):
    idx = interactive_menu(prompt, labels, default=default, allow_cancel=False)
    return default if idx is None else idx


def ask_text(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    raw = input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}{suffix}: ").strip()
    return raw or default


def ask_secret(prompt):
    try:
        return getpass.getpass(f"{prompt}: ")
    except Exception:
        return input(f"{prompt}: ")


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    raw = input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} [{d}]: ").strip().lower()
    if raw == "":
        return default
    return raw in ("y", "yes")


def ask_yes_back(prompt, default=True):
    """Like ask_yes, but Esc goes back: returns True (yes) / False (no) / None (Esc)."""
    d = "Y/n" if default else "y/N"
    msg = (f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} [{d}] "
           f"{Style.DIM}(Esc = back){Style.RESET_ALL} ")
    if not _tui_supported():
        try:
            raw = input(strip_ansi(msg)).strip().lower()
        except EOFError:
            return None
        if raw == "":
            return default
        return raw in ("y", "yes")
    sys.stdout.write(msg)
    sys.stdout.flush()
    with _RawMode():
        while True:
            k = _read_key()
            if k == "esc":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return None
            if k == "enter":
                sys.stdout.write(("y" if default else "n") + "\r\n")
                sys.stdout.flush()
                return default
            if isinstance(k, tuple) and k[0] == "char":
                c = k[1].lower()
                if c == "y":
                    sys.stdout.write("y\r\n")
                    sys.stdout.flush()
                    return True
                if c == "n":
                    sys.stdout.write("n\r\n")
                    sys.stdout.flush()
                    return False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _config_url():
    return (_CONFIG_URL_OVERRIDE or os.environ.get("PLEX_TOOLS_CONFIG_URL") or CONFIG_URL or "").strip()


def _fetch_remote_config(url):
    """GET a JSON config from url (short timeout). Returns a dict or None.
    Never raises: any problem (timeout, HTTP error, bad JSON, cert) -> None so
    the caller falls back to the local config. Retries once insecurely on a
    certificate error (handy for a NAS with a self-signed cert)."""
    for verify in (True, False):
        try:
            ctx = _ssl_ctx(verify) if url.lower().startswith("https") else None
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=CONFIG_FETCH_TIMEOUT, context=ctx) as r:
                status, body = r.status, r.read().decode("utf-8", "replace")
            if status >= 400:
                log_warn(f"Remote config {url} -> HTTP {status}; using local config.")
                return None
            data = json.loads(body)
            if not isinstance(data, dict):
                log_warn(f"Remote config {url} must be a JSON object; using local config.")
                return None
            data.pop("config_url", None)  # never let the remote redirect the config source
            return data
        except Exception as ex:
            if verify and _is_cert_error(ex):
                log_warn("Remote config: certificate not trusted; retrying without verification.")
                continue  # retry once with verify=False
            log_warn(f"Remote config unreachable/invalid ({type(ex).__name__}); using local config.")
            return None
    return None


def load_config():
    global CONFIG_SOURCE
    if CONFIG_PATH is None:
        resolve_config_path()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    local_exists = bool(cfg)
    # remote config (if set) is primary: overlay its keys on top of the local one
    url = _config_url()
    if url:
        remote = _fetch_remote_config(url)
        if remote:
            cfg = {**cfg, **remote}
            base = f"overriding local {CONFIG_PATH}" if local_exists else f"local {CONFIG_PATH} (empty)"
            CONFIG_SOURCE = f"remote {url}"
            log_info(f"Config source: remote {url} ({len(remote)} key(s)), {base}.")
        else:
            CONFIG_SOURCE = f"local {CONFIG_PATH} (remote unavailable)"
            log_info(f"Config source: local {CONFIG_PATH} (remote {url} unavailable).")
    else:
        CONFIG_SOURCE = f"local {CONFIG_PATH}"
        log_info(f"Config source: local {CONFIG_PATH}.")
    return cfg


def save_config(cfg):
    global CONFIG_PATH
    if _NO_LOCAL_CONFIG or not WRITE_LOCAL_CONFIG:
        return  # pure-remote mode: never write a local config file
    if CONFIG_PATH is None:
        resolve_config_path()
    home_cfg = os.path.join(_xdg_config_dir(), "plex_tools", "config.json")
    for target in (CONFIG_PATH, _script_config_path(), home_cfg):
        try:
            d = os.path.dirname(target)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(target, 0o600)  # owner only (where supported)
            except Exception:
                pass
            CONFIG_PATH = target
            return
        except Exception:
            continue  # try the next location
    log_warn("Could not save config (neither next to the script nor to ~/.config).")


def get_client_id(cfg):
    cid = cfg.get("client_id")
    if not cid:
        cid = uuid.uuid4().hex
        cfg["client_id"] = cid
        if _NO_LOCAL_CONFIG or not WRITE_LOCAL_CONFIG:
            log_warn(f"No local config is written, so this client_id won't persist. "
                     f"Add \"client_id\": \"{cid}\" to your remote config to keep it stable.")
        else:
            save_config(cfg)
    return cid


# ---------------------------------------------------------------------------
# Low-level HTTP (stdlib)
# ---------------------------------------------------------------------------
def _ssl_ctx(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _is_cert_error(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper()


def http_raw(method, url, headers=None, data=None, verify=True, timeout=30,
             retries=2, backoff=0.8):
    """Returns (status, text). Does NOT raise on HTTP status errors (returns them).
    Retries transient network errors (timeout / reset); a certificate error is
    raised immediately (so the insecure-HTTPS fallback can kick in)."""
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    ctx = _ssl_ctx(verify) if url.lower().startswith("https") else None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            try:
                body = ex.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return ex.code, body
        except OSError as ex:
            # OSError covers URLError, TimeoutError (socket timeout), ConnectionError,
            # SSLError, ... A cert error is fatal (no retry) -> insecure fallback.
            if _is_cert_error(ex):
                raise RuntimeError(f"Connection failed: {getattr(ex, 'reason', ex)}  (try --insecure)")
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))  # transient -> back off and retry
                continue
            raise RuntimeError(
                f"Connection failed (after {retries + 1} attempts): {getattr(ex, 'reason', ex)}")


def http_json(method, url, headers=None, data=None, verify=True, timeout=30):
    st, text = http_raw(method, url, headers, data, verify, timeout)
    if st >= 400:
        raise RuntimeError(f"HTTP {st} at {method} {url.split('?')[0]} - {text[:200]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected response (not JSON) from {url.split('?')[0]}")


# ---------------------------------------------------------------------------
# Plex account (plex.tv): sign-in + Home users + switch with PIN
# ---------------------------------------------------------------------------
class PlexAccount:
    def __init__(self, client_id, token=None):
        self.client_id = client_id
        self.token = token

    def _headers(self, accept="application/json", with_token=True):
        h = {
            "Accept": accept,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": "1.0",
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Device-Name": "plex_tools CLI",
        }
        if with_token and self.token:
            h["X-Plex-Token"] = self.token
        return h

    # --- sign in with username and password (+2FA) -------------------------
    def signin_password(self, login, password, code=None):
        body = {"login": login, "password": password, "rememberMe": "true"}
        if code:
            body["verificationCode"] = code
        data = urllib.parse.urlencode(body).encode()
        headers = {**self._headers(with_token=False),
                   "Content-Type": "application/x-www-form-urlencoded"}
        st, text = http_raw("POST", SIGNIN_URL, headers=headers, data=data)
        if st in (200, 201):
            j = json.loads(text)
            tok = j.get("authToken") or j.get("authenticationToken")
            if not tok:
                raise RuntimeError("Signed in, but the token is missing.")
            self.token = tok
            return tok
        if st == 401:
            # either a wrong password, or a missing 2FA code
            raise RuntimeError("UNAUTHORIZED")
        raise RuntimeError(f"Sign-in failed (HTTP {st}): {text[:200]}")

    # --- sign in with a pairing code on plex.tv/link -----------------------
    def pin_login(self, wait_timeout=300, poll=2):
        data = urllib.parse.urlencode({"strong": "false"}).encode()
        j = http_json("POST", PINS_URL,
                      headers={**self._headers(with_token=False),
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data=data)
        pin_id, code = j.get("id"), j.get("code")
        if not pin_id or not code:
            raise RuntimeError("Plex did not return a PIN.")
        print()
        print(f"{Fore.MAGENTA}==============================================={Style.RESET_ALL}")
        print(f"  1) Open:  {Fore.CYAN}https://plex.tv/link{Style.RESET_ALL}")
        print(f"  2) Enter code:  {Fore.GREEN}{Style.BRIGHT}{code}{Style.RESET_ALL}")
        print(f"{Fore.MAGENTA}==============================================={Style.RESET_ALL}")
        print("Waiting for confirmation", end="", flush=True)
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            time.sleep(poll)
            print(".", end="", flush=True)
            try:
                r = http_json("GET", f"{PINS_URL}/{pin_id}",
                              headers=self._headers(with_token=False))
            except Exception:
                continue
            if r.get("authToken"):
                print()
                self.token = r["authToken"]
                return self.token
        print()
        raise RuntimeError("Sign-in timed out (PIN was not confirmed).")

    # --- Home users (XML) -------------------------------------------------
    def home_users(self):
        st, text = http_raw("GET",
                            f"{HOMEUSERS_URL}?X-Plex-Client-Identifier={self.client_id}",
                            headers=self._headers(accept="application/xml"))
        if st == 401:
            raise RuntimeError("UNAUTHORIZED")
        if st >= 400:
            raise RuntimeError(f"HTTP {st} at home/users - {text[:200]}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise RuntimeError("Unexpected response at home/users (could not parse XML).")
        users = []
        for u in root.iter("user"):
            a = u.attrib
            users.append({
                "id": a.get("id"),
                "uuid": a.get("uuid"),
                "title": a.get("title") or a.get("username") or a.get("friendlyName") or "?",
                "admin": a.get("admin") == "1",
                "protected": a.get("protected") == "1",
                "restricted": a.get("restricted") == "1",
            })
        return users

    # --- switch to a user with a PIN -> user token -------------------------
    def switch_user(self, user_id, pin=None):
        params = {}
        if pin:
            params["pin"] = pin
        url = SWITCH_URL.format(uid=user_id)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        st, text = http_raw("POST", url, headers=self._headers(accept="application/xml"))
        if st in (401, 403):
            raise RuntimeError("PIN_INVALID")
        if st >= 400:
            raise RuntimeError(f"Switching user failed (HTTP {st}): {text[:200]}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise RuntimeError("Switch: unexpected response (XML).")
        tok = root.attrib.get("authenticationToken") or root.attrib.get("authToken")
        if not tok:
            el = root.find(".//user")
            if el is not None:
                tok = el.attrib.get("authenticationToken") or el.attrib.get("authToken")
        if not tok:
            raise RuntimeError("Switch: token missing in the response.")
        return tok


# ---------------------------------------------------------------------------
# Plex server client
# ---------------------------------------------------------------------------
class PlexClient:
    def __init__(self, base_url, token, client_id, verify=True, timeout=45):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client_id = client_id
        self.verify = verify
        self.timeout = timeout

    def _headers(self):
        return {
            "X-Plex-Token": self.token,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Client-Identifier": self.client_id,
            "Accept": "application/json",
        }

    def _url(self, path, params=None):
        q = urllib.parse.urlencode(params or {})
        return f"{self.base_url}{path}" + (f"?{q}" if q else "")

    def get_json(self, path, params=None):
        st, text = http_raw("GET", self._url(path, params), headers=self._headers(),
                            verify=self.verify, timeout=self.timeout)
        if st == 401:
            raise RuntimeError("UNAUTHORIZED")
        if st >= 400:
            raise RuntimeError(f"HTTP {st} at {path} - {text[:200]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Server did not return JSON at {path}.")

    def put(self, path, params=None):
        st, text = http_raw("PUT", self._url(path, params), headers=self._headers(),
                            verify=self.verify, timeout=self.timeout)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}: {text[:120]}")
        return st

    def check(self):
        j = self.get_json("/")
        mc = j.get("MediaContainer", {})
        return mc.get("friendlyName") or mc.get("machineIdentifier") or "Plex"

    def sections(self):
        j = self.get_json("/library/sections")
        return [{"key": d.get("key"), "title": d.get("title"), "type": d.get("type"),
                 "language": d.get("language"), "agent": d.get("agent")}
                for d in j.get("MediaContainer", {}).get("Directory", [])]

    def items_in_section(self, section_key, sec_type, query=None):
        params = {}
        num = SECTION_TYPE_NUM.get(sec_type)
        if num:
            params["type"] = num
        if query:
            params["title"] = query
        j = self.get_json(f"/library/sections/{section_key}/all", params)
        return [{"ratingKey": m.get("ratingKey"), "title": m.get("title"),
                 "year": m.get("year"), "type": m.get("type"), "guid": m.get("guid")}
                for m in j.get("MediaContainer", {}).get("Metadata", [])]

    def get_metadata(self, rating_key):
        j = self.get_json(f"/library/metadata/{rating_key}")
        md = j.get("MediaContainer", {}).get("Metadata", [])
        return md[0] if md else None

    def get_metadata_many(self, rating_keys, chunk=40):
        """Full metadata (incl. Media/Part/Stream) for many items in a FEW requests
        via /library/metadata/{k1,k2,...}. Returns {ratingKey: md}. On a batch
        failure it retries the items one by one and skips any that keep failing
        (logs a warning) instead of crashing the whole scan."""
        out = {}
        keys = [str(k) for k in rating_keys if k is not None]
        i = 0
        while i < len(keys):
            batch = keys[i:i + chunk]
            i += chunk
            try:
                j = self.get_json("/library/metadata/" + ",".join(batch))
                for md in j.get("MediaContainer", {}).get("Metadata", []):
                    out[str(md.get("ratingKey"))] = md
            except Exception:
                for k in batch:  # fall back per item so one slow item doesn't kill the batch
                    if k in out:
                        continue
                    try:
                        md = self.get_metadata(k)
                        if md:
                            out[k] = md
                    except Exception as ex:
                        log_warn(f"Could not load item {k} - skipping ({ex}).")
        return out

    def all_episodes(self, show_key):
        j = self.get_json(f"/library/metadata/{show_key}/allLeaves")
        return j.get("MediaContainer", {}).get("Metadata", [])

    def refresh_section(self, section_key, force=False):
        """'Scan Library Files' on the library (GET .../refresh). force=True is the
        heavy 'Refresh All Metadata' - it re-downloads metadata for every item."""
        params = {"force": 1} if force else None
        st, _ = http_raw("GET", self._url(f"/library/sections/{section_key}/refresh", params),
                         headers=self._headers(), verify=self.verify, timeout=self.timeout)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}")
        return st

    def scrobble(self, rating_key):
        """Mark item watched (GET /:/scrobble)."""
        return self._scrobble("/:/scrobble", rating_key)

    def unscrobble(self, rating_key):
        """Mark item unwatched (GET /:/unscrobble)."""
        return self._scrobble("/:/unscrobble", rating_key)

    def _scrobble(self, path, rating_key):
        params = {"identifier": "com.plexapp.plugins.library", "key": rating_key}
        st, _ = http_raw("GET", self._url(path, params), headers=self._headers(),
                         verify=self.verify, timeout=self.timeout)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}")
        return st

    def item_prefs(self, rating_key):
        """Per-item advanced settings (GET /library/metadata/{id}/prefs) as
        {id: value}. 'languageOverride' is the item's Metadata language; an empty
        value means 'Library default'."""
        try:
            j = self.get_json(f"/library/metadata/{rating_key}/prefs")
        except Exception:
            return {}
        out = {}
        for s in j.get("MediaContainer", {}).get("Setting", []) or []:
            if s.get("id") is not None:
                out[s["id"]] = s.get("value")
        return out

    def activities(self):
        """Background jobs the server is running (GET /activities) - used to show
        the progress of a library refresh."""
        try:
            j = self.get_json("/activities")
        except Exception:
            return []
        return j.get("MediaContainer", {}).get("Activity", []) or []

    def matches(self, rating_key, language=None, manual=1, title=None, year=None):
        """Agent search results for an item (GET /library/metadata/{id}/matches).
        Returns [{'name','guid','year','score'}]. language= picks the language of the
        names; title=/year= run a manual search instead of the automatic one."""
        params = {"manual": manual}
        if language:
            params["language"] = language
        if title:
            params["title"] = title
        if year:
            params["year"] = year
        st, text = http_raw("GET", self._url(f"/library/metadata/{rating_key}/matches", params),
                            headers={"X-Plex-Token": self.token,
                                     "X-Plex-Client-Identifier": self.client_id},
                            verify=self.verify, timeout=self.timeout)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}")
        try:
            root = ET.fromstring(text)
        except Exception:
            return []
        out = []
        for el in root.iter("SearchResult"):
            out.append({"name": el.get("name"), "guid": el.get("guid"),
                        "year": el.get("year"),
                        "score": int(el.get("score") or 0)})
        return out

    def fix_match(self, rating_key, guid, name):
        """Re-match an item to a specific entry (PUT /library/metadata/{id}/match).
        Passing the item's CURRENT guid keeps the same identity - it only makes the
        agent re-pull the metadata for that entry."""
        return self.put(f"/library/metadata/{rating_key}/match",
                        {"guid": guid, "name": name})

    def set_prefs(self, rating_key, **prefs):
        """Per-item advanced settings (PUT /library/metadata/{id}/prefs?key=value).
        languageOverride='en' makes the agent deliver English metadata for THIS item."""
        return self.put(f"/library/metadata/{rating_key}/prefs", prefs)

    def refresh_item(self, rating_key, force=False):
        """Re-pull metadata for one item (PUT /library/metadata/{id}/refresh).
        No re-match: the item keeps its guid, so watched state, resume positions and
        the media files (and their default track choices) are not affected."""
        params = {"force": 1} if force else {}
        return self.put(f"/library/metadata/{rating_key}/refresh", params)

    def unlock_field(self, section_key, sec_type, rating_key, field="title"):
        """Unlock a metadata field so a refresh may update it again."""
        return self.put(f"/library/sections/{section_key}/all",
                        {"type": SECTION_TYPE_NUM.get(sec_type, 2),
                         "id": str(rating_key), f"{field}.locked": 0})

    def set_title(self, section_key, sec_type, rating_key, title, sort_title=None,
                  lock=True):
        """Change the title (and optionally the sort title) of an item and lock the
        fields so an agent refresh won't overwrite them. Nothing else (streams,
        watched state, artwork) is touched."""
        params = {"type": SECTION_TYPE_NUM.get(sec_type, 2),
                  "id": str(rating_key),
                  "title.value": title,
                  "title.locked": 1 if lock else 0}
        if sort_title is not None:
            params["titleSort.value"] = sort_title
            params["titleSort.locked"] = 1 if lock else 0
        return self.put(f"/library/sections/{section_key}/all", params)

    def section_labels(self, section_key):
        """All label names that exist in a library."""
        return [e["title"] for e in self.section_label_entries(section_key)]

    def section_label_entries(self, section_key):
        """Labels of a library as [{'id': filterKey, 'title': name}, ...]
        (GET /library/sections/{key}/label). Labels are shared tag objects."""
        try:
            j = self.get_json(f"/library/sections/{section_key}/label")
        except Exception:
            return []
        out = []
        for d in j.get("MediaContainer", {}).get("Directory", []) or []:
            title = d.get("title")
            if title:
                out.append({"id": d.get("key"), "title": title})
        return out

    def items_with_label(self, section_key, sec_type, label_id, type_num=None):
        """ratingKeys of items carrying a label (server-side filter:
        /library/sections/{key}/all?type=N&label=<id>). type_num overrides the
        section type, so seasons (3), episodes (4) and collections (18) can be
        checked too - a label left on those keeps the tag alive in Plex."""
        params = {"label": label_id}
        num = type_num if type_num is not None else SECTION_TYPE_NUM.get(sec_type)
        if num:
            params["type"] = num
        j = self.get_json(f"/library/sections/{section_key}/all", params)
        return [str(m.get("ratingKey"))
                for m in j.get("MediaContainer", {}).get("Metadata", []) or []]

    def section_country_entries(self, section_key, sec_type=None):
        """Countries of origin present in a library as [{'id', 'title'}, ...]
        (GET /library/sections/{key}/country). Empty if the library/agent stores no
        country data or doesn't expose the filter - callers should fall back to
        reading each item's Country metadata."""
        params = {}
        num = SECTION_TYPE_NUM.get(sec_type) if sec_type else None
        if num:
            params["type"] = num
        try:
            j = self.get_json(f"/library/sections/{section_key}/country", params)
        except Exception:
            return []
        out = []
        for d in j.get("MediaContainer", {}).get("Directory", []) or []:
            if d.get("title") and d.get("key") is not None:
                out.append({"id": d.get("key"), "title": d.get("title")})
        return out

    def items_with_country(self, section_key, sec_type, country_key, type_num=None):
        """ratingKeys of items whose country of origin matches (server-side filter:
        /library/sections/{key}/all?country=<key>&type=N)."""
        params = {"country": country_key}
        num = type_num if type_num is not None else SECTION_TYPE_NUM.get(sec_type)
        if num:
            params["type"] = num
        j = self.get_json(f"/library/sections/{section_key}/all", params)
        return [str(m.get("ratingKey"))
                for m in j.get("MediaContainer", {}).get("Metadata", []) or []]

    def items_by_filter(self, section_key, sec_type, **filters):
        """Items matching a server-side filter (e.g. actor=<id>, director=<id>) as
        [{'rk','title','year','type'}]. /library/sections/{key}/all?<filter>&type=N."""
        params = dict(filters)
        num = SECTION_TYPE_NUM.get(sec_type)
        if num:
            params["type"] = num
        j = self.get_json(f"/library/sections/{section_key}/all", params)
        return [{"rk": str(m.get("ratingKey")), "title": m.get("title"),
                 "year": m.get("year"), "type": m.get("type")}
                for m in j.get("MediaContainer", {}).get("Metadata", []) or []]

    def edit_labels(self, section_key, sec_type, rating_keys, add=(), remove=(), type_num=None):
        """Add/remove labels on one or more items in a single request:
        PUT /library/sections/{key}/all?type=..&id=rk1,rk2&label[i].tag.tag=..
            &label[].tag.tag-=..&label.locked=1
        Labels are per-item tags; the same name is the same tag object server-side."""
        if not rating_keys or (not add and not remove):
            return 0
        params = {"type": type_num if type_num is not None else SECTION_TYPE_NUM.get(sec_type, 2),
                  "id": ",".join(str(k) for k in rating_keys),
                  "label.locked": 1}
        for i, tag in enumerate(add):
            params[f"label[{i}].tag.tag"] = tag
        if remove:
            # Plex takes a comma-separated removal list; send commas-containing
            # labels separately so they can't be split apart.
            simple = [t for t in remove if "," not in t]
            if simple:
                params["label[].tag.tag-"] = ",".join(simple)
            for tag in [t for t in remove if "," in t]:
                self.put(f"/library/sections/{section_key}/all",
                         {"type": params["type"], "id": params["id"],
                          "label.locked": 1, "label[].tag.tag-": tag})
        return self.put(f"/library/sections/{section_key}/all", params)

    # --- Watchlist (account level, via Plex Discover) ----------------------
    # The watchlist is NOT stored on the local server; it belongs to the signed-in
    # Plex account/user and lives on discover.provider.plex.tv. The same user token
    # that talks to the server authorises it. Items are online-metadata objects; the
    # trailing id of their plex:// guid is the "ratingKey" the actions below expect.
    def _discover_headers(self):
        return {
            "X-Plex-Token": self.token,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Client-Identifier": self.client_id,
            "Accept": "application/json",
        }

    def watchlist(self, libtype=None):
        """The signed-in user's watchlist from Plex Discover as
        [{'ratingKey','guid','title','year','type'}] (paged automatically).
        ratingKey is the Discover id; guid is 'plex://movie/<id>'. libtype
        ('movie'/'show') limits the kind returned."""
        base = {"includeCollections": 0, "includeExternalMedia": 0}
        if libtype:
            base["type"] = SECTION_TYPE_NUM.get(libtype, "")
        # Discover caps the container size (it rejects large values with HTTP 400) and,
        # like Plex Web, expects the paging as query params — not headers. 50 is the size
        # Plex Web itself uses.
        out, start, size = [], 0, 50
        while True:
            params = dict(base)
            params["X-Plex-Container-Start"] = start
            params["X-Plex-Container-Size"] = size
            url = f"{DISCOVER_URL}/library/sections/watchlist/all?" + urllib.parse.urlencode(params)
            st, text = http_raw("GET", url, headers=self._discover_headers(), verify=True,
                                timeout=self.timeout)
            if st == 401:
                raise RuntimeError("UNAUTHORIZED")
            if st >= 400:
                raise RuntimeError(f"HTTP {st} at watchlist - {text[:200]}")
            try:
                mc = json.loads(text).get("MediaContainer", {})
            except json.JSONDecodeError:
                raise RuntimeError("Watchlist: Plex Discover did not return JSON.")
            batch = mc.get("Metadata", []) or []
            for m in batch:
                out.append({"ratingKey": str(m.get("ratingKey")), "guid": m.get("guid"),
                            "title": m.get("title"), "year": m.get("year"),
                            "type": m.get("type")})
            total = mc.get("totalSize")
            got = start + len(batch)
            if not batch or len(batch) < size or (total is not None and got >= total):
                break
            start = got
        return out

    def watchlist_keys(self, libtype=None):
        """Set of Discover ratingKeys currently on the watchlist (fast membership test)."""
        return {w["ratingKey"] for w in self.watchlist(libtype) if w.get("ratingKey")}

    def _watchlist_action(self, action, discover_rating_key):
        url = (f"{DISCOVER_URL}/actions/{action}"
               f"?ratingKey={urllib.parse.quote(str(discover_rating_key))}")
        st, text = http_raw("PUT", url, headers=self._discover_headers(),
                            verify=True, timeout=self.timeout)
        if st == 401:
            raise RuntimeError("UNAUTHORIZED")
        if st >= 400:
            raise RuntimeError(f"HTTP {st}: {text[:120]}")
        return st

    def add_to_watchlist(self, discover_rating_key):
        """Put one online-metadata item on the watchlist (PUT addToWatchlist)."""
        return self._watchlist_action("addToWatchlist", discover_rating_key)

    def remove_from_watchlist(self, discover_rating_key):
        """Take one item off the watchlist (PUT removeFromWatchlist)."""
        return self._watchlist_action("removeFromWatchlist", discover_rating_key)

    def children(self, rating_key):
        """Direct children of a server item: the seasons of a show, or the episodes
        of a season (GET /library/metadata/{id}/children)."""
        j = self.get_json(f"/library/metadata/{rating_key}/children")
        return j.get("MediaContainer", {}).get("Metadata", []) or []

    def discover_metadata(self, discover_rating_key):
        """Online (Plex Discover) metadata for a watchlist item that isn't on this
        server. Returns a dict or None; never raises."""
        try:
            url = (f"{DISCOVER_URL}/library/metadata/"
                   f"{urllib.parse.quote(str(discover_rating_key))}?includeUserState=1")
            st, text = http_raw("GET", url, headers=self._discover_headers(), verify=True,
                                timeout=self.timeout)
            if st >= 400:
                return None
            md = json.loads(text).get("MediaContainer", {}).get("Metadata", [])
            return md[0] if md else None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Server-address helpers
# ---------------------------------------------------------------------------
def normalize_base_url(s):
    s = (s or "").strip().rstrip("/")
    if not s:
        return s
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    return s


def verify_for(base_url, verify_pref):
    # bare https to an IP has no valid certificate
    if verify_pref and base_url.lower().startswith("https") and \
            re.search(r"//\d+\.\d+\.\d+\.\d+(:\d+)?", base_url):
        return False
    return verify_pref


def parse_show_ref(ref):
    if ref is None:
        return None
    ref = ref.strip()
    if ref.isdigit():
        return ref
    m = re.search(r"/library/metadata/(\d+)", urllib.parse.unquote(ref))
    if m:
        return m.group(1)
    m = re.search(r"metadata%2F(\d+)", ref)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------
# Human-readable language names by ISO code (so foreign-script characters the
# console cannot render - e.g. Korean/Chinese as boxes - are avoided).
LANG_NAMES = {
    "eng": "English", "en": "English",
    "cze": "Czech", "ces": "Czech", "cs": "Czech",
    "slo": "Slovak", "slk": "Slovak", "sk": "Slovak",
    "ger": "German", "deu": "German", "de": "German",
    "fre": "French", "fra": "French", "fr": "French",
    "spa": "Spanish", "es": "Spanish",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian",
    "ukr": "Ukrainian", "uk": "Ukrainian",
    "pol": "Polish", "pl": "Polish",
    "kor": "Korean", "ko": "Korean",
    "jpn": "Japanese", "ja": "Japanese",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese",
    "dan": "Danish", "da": "Danish",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch",
    "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "no": "Norwegian",
    "fin": "Finnish", "fi": "Finnish",
    "hun": "Hungarian", "hu": "Hungarian",
    "gre": "Greek", "ell": "Greek", "el": "Greek",
    "tur": "Turkish", "tr": "Turkish",
    "ara": "Arabic", "ar": "Arabic",
    "heb": "Hebrew", "he": "Hebrew",
    "hin": "Hindi", "hi": "Hindi",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "ron": "Romanian", "rum": "Romanian", "ro": "Romanian",
    "bul": "Bulgarian", "bg": "Bulgarian",
    "hrv": "Croatian", "hr": "Croatian",
    "srp": "Serbian", "sr": "Serbian",
    "slv": "Slovenian", "sl": "Slovenian",
}


def _is_latin(s):
    """True if the text has no non-Latin letters (i.e. the console can render it)."""
    return all((ord(ch) < 0x250) or (not ch.isalpha()) for ch in s)


def lang_name(code, plex_name=None):
    """Human-readable language name: primarily by ISO code, otherwise Plex's name
    (only if Latin), otherwise the code itself."""
    c = (code or "").lower()
    if c in LANG_NAMES:
        return LANG_NAMES[c]
    if plex_name and _is_latin(plex_name):
        return plex_name
    return code or "?"


def stream_is_external(s):
    """Detects an external (sidecar) subtitle by the presence of 'key' or a flag."""
    return bool(s.get("key")) or str(s.get("external", "")).lower() in ("1", "true")


def stream_signature(s):
    """A stable track 'signature' across episodes (IDs differ per episode, this does not).
    Distinguishes language, codec, external/embedded, forced, SDH, region and title."""
    code = (s.get("languageCode") or s.get("languageTag") or "").lower()
    codec = (s.get("codec") or "").lower()
    ext = stream_is_external(s)
    forced = str(s.get("forced", "")).lower() in ("1", "true")
    sdh = str(s.get("hearingImpaired", "")).lower() in ("1", "true")
    name = (s.get("language") or "").strip().lower()
    title = (s.get("title") or "").strip().lower()
    return (code, codec, ext, forced, sdh, name, title)


def variant_label(s):
    """Human-readable track description. Prefers Plex's displayTitle (what you see
    in Plex) so multiple external subtitles can be told apart; otherwise builds the
    description from components. A foreign-script language is replaced by a Latin name."""
    code = (s.get("languageCode") or "").lower()
    # 1) Plex's own description (e.g. "Czech (SRT External)", "English (SRT, SDH)")
    disp = (s.get("extendedDisplayTitle") or s.get("displayTitle") or "").strip()
    if disp and _is_latin(disp):
        label = disp
        if code and f"[{code}]" not in label and f"({code})" not in label:
            label += f" [{code}]"
        return label
    # 2) fallback: build from components
    base = lang_name(code, s.get("language"))
    raw = (s.get("language") or "").strip()
    if raw and _is_latin(raw) and raw.lower() != base.lower() and ("(" in raw or len(raw.split()) > 1):
        base = raw
    quals = []
    codec = (s.get("codec") or "").upper()
    if codec:
        quals.append(codec)
    if stream_is_external(s):
        quals.append("external")
    if str(s.get("forced", "")).lower() in ("1", "true"):
        quals.append("forced")
    if str(s.get("hearingImpaired", "")).lower() in ("1", "true"):
        quals.append("SDH")
    title = (s.get("title") or "").strip()
    if title and _is_latin(title) and title.lower() not in base.lower():
        quals.append(title)
    label = base + (f" [{code}]" if code else "")
    if quals:
        label += f" ({', '.join(quals)})"
    return label


def variant_short(v):
    """Short identifier for the summary: 'ces', 'eng#2', 'ces·ext'."""
    parts = [v.get("code") or "?"]
    if v.get("ordinal", 0) > 0:
        parts[0] += f"#{v['ordinal'] + 1}"
    if v.get("external"):
        parts.append("ext")
    return "·".join(parts)


def iter_parts(md):
    for media in md.get("Media", []):
        for part in media.get("Part", []):
            yield part.get("id"), part.get("Stream", [])


def _new_variant(s, ordinal):
    return {"key": (stream_signature(s), ordinal), "sig": stream_signature(s),
            "ordinal": ordinal, "code": (s.get("languageCode") or "").lower(),
            "label": variant_label(s), "external": stream_is_external(s), "count": 1}


def _finalize_variants(reg):
    """Turn a {key: variant} registry into a sorted list and disambiguate duplicate labels."""
    from collections import Counter
    vs = list(reg.values())
    lab = Counter(v["label"] for v in vs)
    for v in vs:
        if lab[v["label"]] > 1:
            v["label"] = f"{v['label']} #{v['ordinal'] + 1}"
    vs.sort(key=lambda v: (v["label"].lower(), v["ordinal"]))
    return vs


def streams_variants(streams, stream_type):
    """Variants (concrete tracks) of the given type within one part, ordinal for duplicates."""
    reg = {}
    seen = {}
    for s in streams:
        if s.get("streamType") != stream_type:
            continue
        sig = stream_signature(s)
        ordinal = seen.get(sig, 0)
        seen[sig] = ordinal + 1
        key = (sig, ordinal)
        if key not in reg:
            reg[key] = _new_variant(s, ordinal)
    return reg


def collect_streams(client, items, fetch_full):
    """Returns (items_data, audio_variants, sub_variants), where variants are
    lists of concrete tracks (not just languages) merged across episodes by signature.
    Full metadata is fetched in a FEW batched requests (not one per episode), and
    episodes that can't be loaded are skipped instead of crashing the scan."""
    items_data = []
    audio_reg, sub_reg = {}, {}
    total = len(items)

    # prefetch full metadata (with streams) for items that don't have it yet,
    # in a few batched requests instead of one-per-episode (robust to a slow server)
    need = [str(it.get("ratingKey")) for it in items
            if not it.get("_md") and it.get("ratingKey") is not None]
    md_map = client.get_metadata_many(need) if need else {}

    skipped = 0
    for i, it in enumerate(items, 1):
        rk = str(it.get("ratingKey")) if it.get("ratingKey") is not None else None
        md = it.get("_md") or (md_map.get(rk) if rk else None)
        if md is not None:
            parts = list(iter_parts(md))
            for _pid, streams in parts:
                for stt, reg in ((ST_AUDIO, audio_reg), (ST_SUBTITLE, sub_reg)):
                    for key, v in streams_variants(streams, stt).items():
                        if key in reg:
                            reg[key]["count"] += 1
                        else:
                            reg[key] = v
            items_data.append({
                "ratingKey": it.get("ratingKey"),
                "title": it.get("title") or md.get("title") or f"#{rk}",
                "s": it.get("parentIndex"),
                "e": it.get("index"),
                "parts": parts,
            })
        else:
            skipped += 1  # couldn't load this episode -> skip, keep going
        if total > 1:
            pct = i * 100 // total
            print(f"\r  {Fore.CYAN}scanning streams: {pct:3d}%{Style.RESET_ALL} "
                  f"({i}/{total})   ", end="", flush=True)
    if total > 1:
        print()
    if skipped:
        log_warn(f"{skipped} of {total} items could not be loaded and were skipped.")
    if not items_data:
        die("Could not load any item metadata (the server did not respond).")
    return items_data, _finalize_variants(audio_reg), _finalize_variants(sub_reg)


def pick_variant_id(streams, stream_type, variant):
    """ID of a concrete track in this part by signature+ordinal (IDs differ per episode)."""
    sig, ordinal = variant["key"]
    cnt = 0
    for s in streams:
        if s.get("streamType") != stream_type:
            continue
        if stream_signature(s) == sig:
            if cnt == ordinal:
                return s.get("id")
            cnt += 1
    return None


def item_has_variant(item, stream_type, variant):
    for _pid, streams in item["parts"]:
        if pick_variant_id(streams, stream_type, variant) is not None:
            return True
    return False


def item_available_variants(item, stream_type):
    reg = {}
    for _pid, streams in item["parts"]:
        for key, v in streams_variants(streams, stream_type).items():
            reg.setdefault(key, v)
    return _finalize_variants(reg)


# ---------------------------------------------------------------------------
# Track selection
# ---------------------------------------------------------------------------
def choose_variant(kind, variants, allow_off=False, allow_keep=True, allow_back=False,
                   header=None, default=None):
    """Returns (action, idx). action is ('var',v)/('off',None)/('keep',None)/('back',None);
    idx is the highlighted menu index (to remember the position). `default` sets the
    initial highlighted row."""
    labels = [v["label"] for v in variants]
    off_idx = keep_idx = None
    all_labels = list(labels)
    if allow_off:
        off_idx = len(all_labels)
        all_labels.append(f"{Fore.MAGENTA}— turn OFF (no subtitles) —{Style.RESET_ALL}")
    if allow_keep:
        keep_idx = len(all_labels)
        all_labels.append(f"{Fore.MAGENTA}— leave unchanged —{Style.RESET_ALL}")
    if not all_labels:
        log_warn(f"No {kind} tracks found.")
        return ("keep", None), 0
    default_idx = default if default is not None else (keep_idx if keep_idx is not None else 0)
    default_idx = max(0, min(default_idx, len(all_labels) - 1))
    idx = interactive_menu(f"Select default {kind}:", all_labels,
                           default=default_idx, allow_cancel=allow_back, header=header)
    if idx is None:
        return ("back", None), default_idx
    if idx < len(variants):
        return ("var", variants[idx]), idx
    if off_idx is not None and idx == off_idx:
        return ("off", None), idx
    return ("keep", None), idx


def resolve_variant_arg(arg, variants, kind, allow_off):
    """CLI --audio/--subs: 'keep'/'off'/'0', a language code, or part of the label."""
    if arg is None:
        return None
    a = arg.strip().lower()
    if a in ("keep", "-"):
        return ("keep", None)
    if allow_off and a in ("off", "none", "0"):
        return ("off", None)
    cands = [v for v in variants if v["code"] == a]
    if not cands:
        cands = [v for v in variants if a in v["label"].lower()]
    if cands:
        cands.sort(key=lambda v: -v.get("count", 0))
        return ("var", cands[0])
    die(f"{kind}: '{arg}' not available. Codes: "
        + (", ".join(sorted({v['code'] for v in variants})) or "(none)"))


# ---------------------------------------------------------------------------
# Applying changes
# ---------------------------------------------------------------------------
def ep_label(it):
    if it["s"] is not None and it["e"] is not None:
        try:
            return f"S{int(it['s']):02d}E{int(it['e']):02d}"
        except Exception:
            pass
    return it["title"]


def _rescan_missing(client, section_key, items_data, missing, stream_type, variant,
                    wait_total=90, interval=3):
    """Trigger 'Scan Library Files' and wait until the missing items expose `variant`
    (re-fetching their metadata). Updates items_data[i]['parts'] in place. Returns the
    number of previously-missing items that now have the variant. Automates the manual
    'F5 refresh, wait, retry' dance for freshly added external subtitle files."""
    key = section_key
    if key is None and missing:  # --show path: derive the section from the item
        try:
            md = client.get_metadata(items_data[missing[0]]["ratingKey"])
            key = (md or {}).get("librarySectionID")
        except Exception:
            key = None
    try:
        if key is not None:
            client.refresh_section(key)
            log_info("Scan Library Files started; waiting for Plex to index the files…")
        else:
            for sec in client.sections():
                client.refresh_section(sec["key"])
            log_info("Scan Library Files started on all libraries; waiting…")
    except Exception as ex:
        log_warn(f"Could not start a scan: {ex}")

    deadline = time.monotonic() + wait_total
    while True:
        still = [i for i in missing if not item_has_variant(items_data[i], stream_type, variant)]
        if not still:
            break
        try:
            md_map = client.get_metadata_many([items_data[i]["ratingKey"] for i in still])
        except Exception:
            md_map = {}
        for i in still:
            md = md_map.get(str(items_data[i]["ratingKey"]))
            if md:
                items_data[i]["parts"] = list(iter_parts(md))  # refresh scanned streams
        have = sum(1 for i in missing if item_has_variant(items_data[i], stream_type, variant))
        remaining = len(missing) - have
        if remaining == 0 or time.monotonic() >= deadline:
            break
        print(f"\r  {Fore.CYAN}rescanning… {have}/{len(missing)} found, waiting for "
              f"{remaining} more…{Style.RESET_ALL}   ", end="", flush=True)
        time.sleep(interval)
    print()
    return sum(1 for i in missing if item_has_variant(items_data[i], stream_type, variant))


def resolve_coverage(kind, action, items_data, stream_type, allow_off, interactive,
                     client=None, section_key=None):
    """Returns a per-item plan (length == items_data), each element:
       ('var', variant) | ('off', None) | ('skip', None).
    If some episodes lack the track and we run interactively, ask for a replacement."""
    n = len(items_data)
    if action[0] == "keep":
        return [("skip", None)] * n
    if action[0] == "off":
        return [("off", None)] * n

    variant = action[1]
    plan = [None] * n
    missing = []
    for i, it in enumerate(items_data):
        if item_has_variant(it, stream_type, variant):
            plan[i] = ("var", variant)
        else:
            plan[i] = ("skip", None)
            missing.append(i)

    if not missing:
        return plan

    if not interactive:
        log_warn(f"{len(missing)} of {n} items lack {kind}: {variant['label']}")
        lbls = [ep_label(items_data[i]) for i in missing]
        print("   " + ", ".join(lbls[:30]) + (" …" if len(lbls) > 30 else ""))
        log_info(f"Non-interactive mode: leaving these {len(missing)} items unchanged.")
        return plan

    while missing:
        # available replacement variants across the missing ones (dedup by key)
        avail = {}
        cnt = {}
        for i in missing:
            for v in item_available_variants(items_data[i], stream_type):
                if v["key"] == variant["key"]:
                    continue
                avail.setdefault(v["key"], v)
                cnt[v["key"]] = cnt.get(v["key"], 0) + 1
        opts = sorted(avail.values(), key=lambda v: (-cnt[v["key"]], v["label"].lower()))
        labels = [f"{v['label']}  (has {cnt[v['key']]}/{len(missing)} missing)" for v in opts]

        extra, extra_kind = [], []
        if client is not None:
            extra.append(f"{Fore.CYAN}» Rescan library files & re-check "
                         f"(for freshly added files){Style.RESET_ALL}")
            extra_kind.append("rescan")
        if allow_off:
            extra.append(f"{Fore.MAGENTA}— turn subtitles OFF on missing —{Style.RESET_ALL}")
            extra_kind.append("off")
        extra.append(f"{Fore.MAGENTA}— leave missing unchanged (skip) —{Style.RESET_ALL}")
        extra_kind.append("skip")

        if not opts and "rescan" not in extra_kind:
            log_info("Missing items have no other track — leaving them unchanged.")
            break

        # default to Rescan when the wanted track is external (the usual cause), else skip
        if "rescan" in extra_kind and variant.get("external"):
            default_idx = len(labels) + extra_kind.index("rescan")
        else:
            default_idx = len(labels) + len(extra) - 1

        mlbls = [ep_label(items_data[i]) for i in missing]
        header = [
            f"{Fore.YELLOW}{len(missing)} of {n} items lack {kind}: {variant['label']}{Style.RESET_ALL}",
            "  " + ", ".join(mlbls[:40]) + (" …" if len(mlbls) > 40 else ""),
            "",
        ]
        idx = interactive_menu(f"Replace {kind} on missing episodes with?",
                               labels + extra, default=default_idx,
                               allow_cancel=True, header=header)
        if idx is None:
            break  # Esc = leave the missing items unchanged (they stay 'skip')
        if idx < len(opts):
            alt = opts[idx]
            still, applied = [], 0
            for i in missing:
                if item_has_variant(items_data[i], stream_type, alt):
                    plan[i] = ("var", alt)
                    applied += 1
                else:
                    still.append(i)
            log_done(f"Set '{alt['label']}' on {applied} episodes.")
            missing = still
            if missing:
                log_info(f"{len(missing)} episodes lack this replacement — pick another.")
        else:
            sel = extra_kind[idx - len(opts)]
            if sel == "rescan":
                _rescan_missing(client, section_key, items_data, missing,
                                stream_type, variant)
                still, applied = [], 0
                for i in missing:
                    if item_has_variant(items_data[i], stream_type, variant):
                        plan[i] = ("var", variant)
                        applied += 1
                    else:
                        still.append(i)
                if applied:
                    log_done(f"After rescan, {applied} episodes now have "
                             f"'{variant['label']}'.")
                else:
                    log_warn("Rescan finished but the files still aren't indexed yet "
                             "(try again, or check the sidecar filenames).")
                missing = still  # loop re-shows any that are still missing
            elif sel == "off":
                for i in missing:
                    plan[i] = ("off", None)
                log_done(f"Subtitles turned OFF on {len(missing)} missing episodes.")
                break
            else:  # skip
                log_info(f"{len(missing)} episodes left unchanged.")
                break
    return plan


def plan_summary(plan):
    from collections import Counter
    c = Counter()
    for p in plan:
        if p[0] == "var":
            c[variant_short(p[1])] += 1
        elif p[0] == "off":
            c["OFF"] += 1
        else:
            c["unchanged"] += 1
    return ", ".join(f"{k}: {v}×" for k, v in c.items())


def apply_changes(client, items_data, audio_plan, sub_plan, dry_run):
    ok = fail = changed = 0
    for i, it in enumerate(items_data):
        se = ep_label(it)
        se = (se + " ") if se else ""
        a = audio_plan[i]
        s = sub_plan[i]
        for part_id, streams in it["parts"]:
            params = {"allParts": 1}
            notes = []
            if a[0] == "var":
                aid = pick_variant_id(streams, ST_AUDIO, a[1])  # ID resolved PER EPISODE
                if aid is not None:
                    params["audioStreamID"] = aid
                    notes.append(f"audio->{variant_short(a[1])}")
                else:
                    notes.append(f"{Fore.YELLOW}audio '{variant_short(a[1])}' missing{Style.RESET_ALL}")
            if s[0] == "off":
                params["subtitleStreamID"] = 0
                notes.append("subs->OFF")
            elif s[0] == "var":
                tid = pick_variant_id(streams, ST_SUBTITLE, s[1])  # ID resolved PER EPISODE
                if tid is not None:
                    params["subtitleStreamID"] = tid
                    notes.append(f"subs->{variant_short(s[1])}")
                else:
                    notes.append(f"{Fore.YELLOW}subs '{variant_short(s[1])}' missing{Style.RESET_ALL}")
            if "audioStreamID" not in params and "subtitleStreamID" not in params:
                continue
            label = f"{se}{it['title']} (part {part_id}): " + ", ".join(notes)
            if dry_run:
                print(f"  {Fore.CYAN}[plan]{Style.RESET_ALL} {label}")
                changed += 1
                continue
            try:
                client.put(f"/library/parts/{part_id}", params)
                print(f"  {Fore.GREEN}+{Style.RESET_ALL} {label}")
                ok += 1
                changed += 1
            except Exception as ex:
                print(f"  {Fore.RED}x{Style.RESET_ALL} {label}  -> {ex}")
                fail += 1
    return ok, fail, changed


# ---------------------------------------------------------------------------
# Sign-in / obtaining a token (saved to the config)
# ---------------------------------------------------------------------------
def account_login(acct):
    """Interactively obtain an account token (password or plex.tv/link). Stored in acct.token."""
    idx = ask_choice("Sign in to Plex account:", [
        "Username/email + password",
        "Code on plex.tv/link (no password)",
    ])
    if idx == 0:
        login = ask_text("Plex username or email")
        pw = ask_secret("Password")
        try:
            acct.signin_password(login, pw)
        except RuntimeError as ex:
            if "UNAUTHORIZED" in str(ex):
                code = ask_text("Two-factor (2FA) code, if you have it enabled (otherwise Enter)")
                if code:
                    try:
                        acct.signin_password(login, pw, code=code)
                    except RuntimeError:
                        die("Sign-in failed (wrong username/password or 2FA code).")
                else:
                    die("Sign-in failed (wrong username/password, or missing 2FA code).")
            else:
                die(str(ex))
    else:
        acct.pin_login()
    log_done("Signed in to account.")
    return acct.token


def choose_home_user(acct, cfg):
    """Pick a Home user and obtain its token (via switch + PIN). Saved to cfg."""
    users = acct.home_users()
    if not users:
        die("The account has no Home users.")
    if len(users) == 1:
        chosen = users[0]
    else:
        labels = [u["title"] + (" (admin)" if u["admin"] else "")
                  + (" [PIN]" if u["protected"] else "") for u in users]
        chosen = users[ask_choice("Select a Plex Home user:", labels)]

    pin = None
    if chosen["protected"]:
        pin = ask_text(f"PIN for user '{chosen['title']}'")
    while True:
        try:
            user_token = acct.switch_user(chosen["id"], pin=pin)
            break
        except RuntimeError as ex:
            if "PIN_INVALID" in str(ex):
                log_warn("Invalid PIN.")
                pin = ask_text(f"PIN for user '{chosen['title']}' (again)")
                continue
            die(str(ex))
    cfg["home_user"] = {"id": chosen["id"], "uuid": chosen["uuid"],
                        "title": chosen["title"], "protected": chosen["protected"]}
    cfg["user_token"] = user_token
    save_config(cfg)
    log_done(f"Switched to user: {chosen['title']}")
    return user_token


def full_login(cfg, client_id):
    """Full sign-in: account -> Home user + PIN. Returns user_token."""
    acct = PlexAccount(client_id)
    account_login(acct)
    cfg["account_token"] = acct.token
    save_config(cfg)
    return choose_home_user(acct, cfg)


def reswitch(cfg, client_id):
    """When user_token expires: switch again (PIN) using the saved account_token."""
    acct = PlexAccount(client_id, cfg.get("account_token"))
    hu = cfg.get("home_user") or {}
    try:
        pin = ask_text(f"PIN for user '{hu.get('title','?')}'") if hu.get("protected") else None
        while True:
            try:
                tok = acct.switch_user(hu["id"], pin=pin)
                break
            except RuntimeError as ex:
                if "PIN_INVALID" in str(ex):
                    log_warn("Invalid PIN.")
                    pin = ask_text("PIN (again)")
                    continue
                raise
        cfg["user_token"] = tok
        save_config(cfg)
        return tok
    except RuntimeError:
        # the account token is probably invalid too -> start over
        log_warn("Saved sign-in expired, please sign in again.")
        return full_login(cfg, client_id)


def build_client(args, cfg):
    client_id = get_client_id(cfg)
    verify_pref = not args.insecure

    # server address: arg -> config -> ask (and save)
    base_url = normalize_base_url(args.base_url) if args.base_url else cfg.get("base_url")
    if not base_url:
        base_url = normalize_base_url(
            ask_text("Enter the FQDN / address of the Plex server (e.g. https://plex.falco81.net)"))
        if not base_url:
            die("Server address is required.")
        cfg["base_url"] = base_url
        save_config(cfg)
    elif args.base_url:
        cfg["base_url"] = base_url
        save_config(cfg)

    def _connect(token):
        """Connect to the server. On a certificate error, automatically switch to
        insecure HTTPS (your own server) and remember it. Returns (client, name)."""
        v = False if cfg.get("insecure_server") else verify_for(base_url, verify_pref)
        client = PlexClient(base_url, token, client_id, verify=v)
        try:
            return client, client.check()
        except RuntimeError as ex:
            msg = str(ex).upper()
            if v and ("CERTIFIC" in msg or "SSL" in msg):
                log_warn("Could not verify the server certificate (incomplete chain / "
                         "missing CA) — switching to insecure HTTPS for your server.")
                log_info("Tip: `pip install truststore` enables secure verification via "
                         "the Windows certificate store.")
                cfg["insecure_server"] = True
                save_config(cfg)
                client = PlexClient(base_url, token, client_id, verify=False)
                return client, client.check()
            raise

    # direct token (power-user override)
    if args.token:
        client, name = _connect(args.token)
        log_done(f"Connected to server: {name}")
        return client

    # already have a saved user token? -> try it directly
    user_token = cfg.get("user_token")
    if user_token:
        try:
            client, name = _connect(user_token)
            log_done(f"Connected as '{(cfg.get('home_user') or {}).get('title','?')}' to server: {name}")
            return client
        except RuntimeError as ex:
            if "UNAUTHORIZED" in str(ex):
                log_warn("Saved user token expired - refreshing.")
                user_token = reswitch(cfg, client_id)
                client, name = _connect(user_token)
                log_done(f"Connected to server: {name}")
                return client
            die(f"Server unreachable: {ex}")

    # first sign-in
    user_token = full_login(cfg, client_id)
    client, name = _connect(user_token)
    log_done(f"Connected to server: {name}")
    return client


# ---------------------------------------------------------------------------
# Target selection (show -> episodes, movie -> itself)
# ---------------------------------------------------------------------------
def _target_from_item(client, item):
    if item["type"] == "show":
        eps = client.all_episodes(item["ratingKey"])
        if not eps:
            die("The show has no episodes.")
        return eps, f"show '{item['title']}' ({len(eps)} episodes)", True
    md = client.get_metadata(item["ratingKey"])
    md["_md"] = md
    return [md], f"movie '{item['title']}'", False


def _target_from_md(client, md):
    if md.get("type") == "show":
        eps = client.all_episodes(md.get("ratingKey"))
        return eps, f"show '{md.get('title')}' ({len(eps)} episodes)", True
    md["_md"] = md
    return [md], f"'{md.get('title')}'", False


def scan_target(client, target):
    """Scan the target's streams. Returns (items_data, audio, subs, header_lines)."""
    its, label, ff = target
    clear_screen()
    log_info(f"Target: {Fore.CYAN}{label}{Style.RESET_ALL}")
    if ff:
        log_info("Scanning available tracks across episodes...")
    idata, audio, subs = collect_streams(client, its, ff)
    header = [
        f"{Fore.CYAN}Target:{Style.RESET_ALL} {label}",
        f"{Fore.CYAN}Audio:{Style.RESET_ALL}   " + (" | ".join(v["label"] for v in audio) or "(none)"),
        f"{Fore.CYAN}Subtitles:{Style.RESET_ALL} " + (" | ".join(v["label"] for v in subs) or "(none)"),
        "",
    ]
    return idata, audio, subs, header


def select_and_configure(client, args, resume=None):
    """Wizard: library -> item -> audio -> subtitles, with step-back (Esc).
    Returns (items_data, audio_action, sub_action, state) or None. `state` lets the
    caller resume at the subtitles step (e.g. after Esc on the final confirm)."""
    # --show: fixed item (skips the library/item steps)
    fixed = None
    if args.show:
        rk = parse_show_ref(args.show)
        if rk:
            md = client.get_metadata(rk)
            if not md:
                die(f"ratingKey {rk} not found.")
            fixed = _target_from_md(client, md)
        else:
            found = []
            for sec in client.sections():
                found += client.items_in_section(sec["key"], sec["type"], query=args.show)
            if not found:
                die(f"'{args.show}' not found.")
            if len(found) == 1:
                fixed = _target_from_item(client, found[0])
            else:
                labels = [f"{f['title']} ({f['year']})" if f.get("year") else f["title"] for f in found]
                ftags = [f"{Style.DIM}[{f['type']}]{Style.RESET_ALL}" for f in found]
                i = interactive_menu("Multiple found - select:", labels, allow_cancel=True, tags=ftags)
                if i is None:
                    return None
                fixed = _target_from_item(client, found[i])

    secs = client.sections()
    if not secs:
        die("The server has no libraries.")
    single_lib = len(secs) == 1
    have_item_step = fixed is None

    audio_interactive = args.audio is None
    subs_interactive = args.subs is None

    LIB, ITEM, AUDIO, SUBS = range(4)
    sec = secs[0] if single_lib else None
    scan_cache = {}
    idata = audio_vars = sub_vars = header = None
    audio_action = sub_action = None

    def scan_lib(section):
        try:
            client.refresh_section(section["key"])
            return (f"{Fore.GREEN}» Scan Library Files started on library "
                    f"'{section['title']}'.{Style.RESET_ALL}")
        except Exception as ex:
            return f"{Fore.RED}Scan Library Files failed: {ex}{Style.RESET_ALL}"

    if fixed:
        idata, audio_vars, sub_vars, header = scan_target(client, fixed)
        step = AUDIO
    else:
        step = ITEM if single_lib else LIB

    lib_idx = 0
    item_idx = {}  # section key -> last highlighted item index
    audio_idx = None
    sub_idx = None
    if resume is not None:
        idata = resume["idata"]
        audio_vars = resume["audio_vars"]
        sub_vars = resume["sub_vars"]
        header = resume["header"]
        audio_action = resume["audio_action"]
        audio_idx = resume.get("audio_idx")
        sub_idx = resume.get("sub_idx")
        # restore the picker context too, so stepping further back (SUBS -> AUDIO ->
        # ITEM) lands on the same library/item instead of an unset section
        sec = resume.get("sec") or sec
        lib_idx = resume.get("lib_idx", 0)
        item_idx = dict(resume.get("item_idx") or {})
        step = SUBS if subs_interactive else AUDIO
    while True:
        if step == LIB:
            labels = [s["title"] for s in secs]
            libtags = [f"{Style.DIM}[{s['type']}]{Style.RESET_ALL}" for s in secs]
            i = interactive_menu("Select a library:", labels, default=lib_idx, allow_cancel=True,
                                 tags=libtags,
                                 refresh_cb=lambda idx: scan_lib(secs[idx]) if idx is not None else None)
            if i is None:
                return None
            lib_idx = i
            sec = secs[i]
            step = ITEM

        elif step == ITEM:
            if sec is None:  # no library picked yet (e.g. resumed run) -> go pick one
                step = LIB
                continue
            items = client.items_in_section(sec["key"], sec["type"])
            if not items:
                log_warn("The library is empty.")
                if single_lib:
                    return None
                step = LIB
                continue
            items.sort(key=lambda x: (x["title"] or "").lower())
            labels = [f"{it['title']} ({it['year']})" if it["year"] else it["title"] for it in items]
            i = interactive_menu(f"Select a show/movie from '{sec['title']}':", labels,
                                 default=item_idx.get(sec["key"], 0), allow_cancel=True,
                                 refresh_cb=lambda idx: scan_lib(sec))
            if i is None:
                if single_lib:
                    return None
                step = LIB  # back to the library list, on the same library
                continue
            item_idx[sec["key"]] = i  # remember for when we come back (e.g. from AUDIO)
            chosen = items[i]
            rk = chosen["ratingKey"]
            if rk not in scan_cache:
                target = _target_from_item(client, chosen)
                scan_cache[rk] = (target, scan_target(client, target))
            _t, (idata, audio_vars, sub_vars, header) = scan_cache[rk]
            step = AUDIO

        elif step == AUDIO:
            if not audio_interactive:
                audio_action = resolve_variant_arg(args.audio, audio_vars, "audio", False)
                step = SUBS
                continue
            audio_action, audio_idx = choose_variant("audio", audio_vars, allow_off=False,
                                                     allow_back=True, header=header, default=audio_idx)
            if audio_action[0] == "back":
                if have_item_step:
                    step = ITEM
                    continue
                return None
            step = SUBS

        elif step == SUBS:
            if not subs_interactive:
                sub_action = resolve_variant_arg(args.subs, sub_vars, "subtitles", True)
                break
            sub_action, sub_idx = choose_variant("subtitles", sub_vars, allow_off=True,
                                                 allow_back=True, header=header, default=sub_idx)
            if sub_action[0] == "back":
                if audio_interactive:
                    step = AUDIO
                elif have_item_step:
                    step = ITEM
                else:
                    return None
                continue
            break

    if audio_action[0] == "keep" and sub_action[0] == "keep":
        log_warn("No change selected. Exiting.")
        return None
    state = {"idata": idata, "audio_vars": audio_vars, "sub_vars": sub_vars,
             "header": header, "audio_action": audio_action, "audio_idx": audio_idx,
             "sub_idx": sub_idx, "can_resume": subs_interactive or audio_interactive,
             "sec": sec, "lib_idx": lib_idx, "item_idx": item_idx}
    return idata, audio_action, sub_action, state


# ---------------------------------------------------------------------------
# Watched / unwatched flow
# ---------------------------------------------------------------------------
def pick_library_and_item(client):
    """Library -> show/movie picker (arrow keys, search, F5 scan, Esc back).
    Returns the chosen item dict (ratingKey/title/type) or None."""
    secs = client.sections()
    if not secs:
        die("The server has no libraries.")
    vid = [s for s in secs if s["type"] in ("show", "movie")] or secs
    single = len(vid) == 1

    def scan_lib(section):
        try:
            client.refresh_section(section["key"])
            return (f"{Fore.GREEN}» Scan Library Files started on library "
                    f"'{section['title']}'.{Style.RESET_ALL}")
        except Exception as ex:
            return f"{Fore.RED}Scan Library Files failed: {ex}{Style.RESET_ALL}"

    sec = vid[0] if single else None
    step = "item" if single else "lib"
    lib_idx = 0
    item_idx = {}  # section key -> last highlighted item index
    while True:
        if step == "lib":
            labels = [s["title"] for s in vid]
            libtags = [f"{Style.DIM}[{s['type']}]{Style.RESET_ALL}" for s in vid]
            i = interactive_menu("Select a library:", labels, default=lib_idx, allow_cancel=True,
                                 tags=libtags,
                                 refresh_cb=lambda idx: scan_lib(vid[idx]) if idx is not None else None)
            if i is None:
                return None
            lib_idx = i
            sec = vid[i]
            step = "item"
        else:
            items = client.items_in_section(sec["key"], sec["type"])
            if not items:
                log_warn("The library is empty.")
                if single:
                    return None
                step = "lib"
                continue
            items.sort(key=lambda x: (x["title"] or "").lower())
            labels = [f"{it['title']} ({it['year']})" if it["year"] else it["title"] for it in items]
            i = interactive_menu(f"Select a show/movie from '{sec['title']}':", labels,
                                 default=item_idx.get(sec["key"], 0), allow_cancel=True,
                                 refresh_cb=lambda idx: scan_lib(sec))
            if i is None:
                if single:
                    return None
                step = "lib"  # back to library list, on the same library
                continue
            item_idx[sec["key"]] = i  # remember, in case the caller comes back
            return items[i]


def _apply_watched(client, targets, action):
    """targets: list of (ratingKey, label, currently_watched). action:
    'watched' | 'unwatched' | 'toggle' (flip each based on its current state)."""
    ok = fail = 0
    total = len(targets)
    for i, (rk, label, cur) in enumerate(targets, 1):
        mark = True if action == "watched" else False if action == "unwatched" else (not cur)
        try:
            (client.scrobble if mark else client.unscrobble)(rk)
            ok += 1
        except Exception as ex:
            fail += 1
            print(f"\r  {Fore.RED}x{Style.RESET_ALL} {label}: {ex}")
        if total > 1:
            print(f"\r  {Fore.CYAN}applying: {i * 100 // total:3d}%{Style.RESET_ALL} "
                  f"({i}/{total})   ", end="", flush=True)
    if total > 1:
        print()
    color = Fore.GREEN if not fail else Fore.YELLOW
    verb = {"watched": "marked watched", "unwatched": "marked unwatched", "toggle": "toggled"}[action]
    print(f"{color}Done: {ok} {verb}" + (f", {fail} failed" if fail else "") + f".{Style.RESET_ALL}")
    log_info("The change shows in the client after a refresh.")


def _watched_action(preset, n, default=0, allow_toggle=True):
    """Return (action, index): action is 'watched'/'unwatched'/'toggle' (or the
    preset), or None on Esc. index is the chosen menu position (to remember it)."""
    if preset is not None:
        return preset, default
    opts = ["Mark as WATCHED", "Mark as UNWATCHED"]
    if allow_toggle:
        opts.append("Toggle each (flip watched state)")
    idx = interactive_menu(f"{n} selected — set status:", opts, default=default, allow_cancel=True)
    if idx is None:
        return None, default
    return ("watched", "unwatched", "toggle")[idx], idx


def _watched_for_item(client, args, item, preset):
    """Handle one movie/show. Returns True (applied), False (cancel to main),
    or 'back' (step back one level, e.g. to the item picker)."""
    # ----- movie: single item -----
    if item.get("type") == "movie":
        md = client.get_metadata(item["ratingKey"]) or {}
        watched = (md.get("viewCount") or 0) > 0
        clear_screen()
        status = (f"{Fore.GREEN}WATCHED{Style.RESET_ALL}" if watched
                  else f"{Style.DIM}UNWATCHED{Style.RESET_ALL}")
        log_info(f"Movie: {Fore.CYAN}{item['title']}{Style.RESET_ALL}  (currently {status})")
        action, _ = _watched_action(preset, 1)
        if action is None:
            return "back"  # Esc -> back to the item picker
        print()
        _apply_watched(client, [(item["ratingKey"], item["title"], watched)], action)
        return True

    # ----- show: episode checkbox list -----
    eps = client.all_episodes(item["ratingKey"])
    if not eps:
        log_warn("The show has no episodes.")
        return "back"
    ep_rows = []
    for e in eps:
        s, ep = e.get("parentIndex"), e.get("index")
        try:
            se = f"S{int(s):02d}E{int(ep):02d}"
        except Exception:
            se = ""
        title = e.get("title") or ""
        watched = (e.get("viewCount") or 0) > 0
        ep_rows.append({"rk": e.get("ratingKey"),
                        "label": f"{se}  {title}".strip(),
                        "watched": watched, "selected": False,
                        "season": s,
                        "sort": (s if s is not None else 0, ep if ep is not None else 0)})

    seen = sum(1 for r in ep_rows if r["watched"])

    # interleave a "Season N" header row before each season's episodes; checking
    # a header (Space) toggles the whole season. A master "All seasons" row on top
    # toggles the entire series.
    by_season = {}
    for r in ep_rows:
        by_season.setdefault(r["season"], []).append(r)
    rows = []
    if len([s for s in by_season if s is not None]) >= 1:
        rows.append({"header": True, "master": True, "season": None,
                     "label": f"All seasons   ({seen}/{len(ep_rows)} watched)",
                     "selected": False, "sort": (-1, -1)})
    for s in sorted(by_season, key=lambda x: (x is None, x)):
        eps_s = sorted(by_season[s], key=lambda r: r["sort"])
        if s is not None:
            w = sum(1 for r in eps_s if r["watched"])
            rows.append({"header": True, "season": s,
                         "label": f"Season {int(s)}   ({w}/{len(eps_s)} watched)",
                         "selected": False, "sort": (s, -1)})
        rows.extend(eps_s)

    header = [
        f"{Fore.CYAN}Show:{Style.RESET_ALL} {item['title']}   "
        f"({len(ep_rows)} episodes · {Fore.GREEN}{seen} watched{Style.RESET_ALL} · "
        f"{Style.DIM}{len(ep_rows) - seen} unwatched{Style.RESET_ALL})",
        f"{Style.DIM}a = whole series · w = only watched · u = only unwatched · "
        f"check the All seasons / Season row to (de)select that whole group.{Style.RESET_ALL}",
        "",
    ]
    # non-interactive whole-show mode: --show + --watched/--unwatched + --yes
    if args.show and preset is not None and args.yes:
        targets = [(r["rk"], r["label"], r["watched"]) for r in ep_rows]
        log_info(f"Marking all {len(targets)} episodes as {preset.upper()}.")
        _apply_watched(client, targets, preset)
        return True

    # step machine: selection -> action -> confirm. Esc steps back one level and
    # remembers the previous choice/answer/position (checkbox selection is kept in
    # `rows`, the action menu reopens on the last choice, confirm keeps its answer).
    action_idx = 0
    confirm_default = True
    sel_cursor = [0]
    sel_view = {}    # keeps the search filter / season / cursor across step-backs
    picked = []
    action = None
    stage = "select"
    while True:
        if stage == "select":
            res = checkbox_menu("Select episodes:", rows, header=header,
                                start_pos=sel_cursor[0], pos_out=sel_cursor,
                                ui_state=sel_view)
            if res is None:
                return "back"  # Esc on the selection -> back to the item picker
            picked = [r for r in rows if r.get("rk") and r["selected"]]
            if not picked:
                log_info("Nothing selected.")
                continue  # re-open the selection (Esc there steps back)
            stage = "action"

        elif stage == "action":
            action, action_idx = _watched_action(preset, len(picked), default=action_idx)
            if action is None:
                stage = "select"  # Esc on the status menu -> back to selection (keeps checkboxes)
                continue
            stage = "confirm"

        else:  # confirm
            prev = "select" if preset is not None else "action"
            clear_screen()
            verb = {"watched": "mark WATCHED", "unwatched": "mark UNWATCHED",
                    "toggle": "toggle watched state of"}[action]
            log_info(f"Will {verb} for {len(picked)} episodes:")
            for r in picked[:40]:
                print(f"    {r['label']}")
            if len(picked) > 40:
                print(f"    … (+{len(picked) - 40} more)")
            print()
            if args.yes:
                confirmed = True
            else:
                ans = ask_yes_back("Proceed?", default=confirm_default)
                if ans is None:
                    stage = prev  # Esc -> back one step (keeps answer/position)
                    continue
                confirm_default = ans
                confirmed = ans
            if not confirmed:
                stage = prev  # 'n' -> back one step
                continue
            print()
            _apply_watched(client, [(r["rk"], r["label"], r["watched"]) for r in picked], action)
            return True


def mark_watched_flow(client, args):
    """Mark videos watched/unwatched with checkboxes (whole series with 'a' or the
    'All seasons' row, a season via its header row, only watched/unwatched with w/u,
    or individual episodes). Esc steps back one menu; from the item picker it exits.
    Returns True if a change was applied, False on cancel."""
    preset = "watched" if args.watched else ("unwatched" if args.unwatched else None)

    # resolve a fixed item from --show, or use the interactive picker (looped so
    # that Esc from a tool screen returns to the picker, not the main menu)
    fixed_item = None
    if args.show:
        rk = parse_show_ref(args.show)
        if rk:
            md = client.get_metadata(rk)
            if not md:
                die(f"ratingKey {rk} not found.")
            fixed_item = {"ratingKey": md.get("ratingKey"), "title": md.get("title"), "type": md.get("type")}
        else:
            found = []
            for sec in client.sections():
                found += client.items_in_section(sec["key"], sec["type"], query=args.show)
            if not found:
                die(f"'{args.show}' not found.")
            if len(found) == 1:
                fixed_item = found[0]
            else:
                labels = [f"{f['title']} ({f['year']})" if f.get("year") else f["title"] for f in found]
                ftags = [f"{Style.DIM}[{f['type']}]{Style.RESET_ALL}" for f in found]
                i = interactive_menu("Multiple found - select:", labels, allow_cancel=True, tags=ftags)
                if i is None:
                    return False
                fixed_item = found[i]

    while True:
        if fixed_item is not None:
            item = fixed_item
        else:
            item = pick_library_and_item(client)
            if not item:
                return False  # backed out of the picker -> main menu

        outcome = _watched_for_item(client, args, item, preset)
        if outcome == "back":
            if fixed_item is not None:
                return False  # --show: nothing to step back to -> main menu
            continue          # back to the library/item picker
        return outcome        # True (applied)


# ---------------------------------------------------------------------------
# Labels flow (custom labels on shows/movies)
# ---------------------------------------------------------------------------
def print_wrapped(text, indent="  ", color=""):
    """Print a paragraph wrapped to the terminal width instead of one long line."""
    import textwrap
    try:
        width = max(40, os.get_terminal_size().columns - 2)
    except Exception:
        width = 100
    for line in textwrap.wrap(text, width=width - len(indent)) or [""]:
        print(f"{color}{indent}{line}{Style.RESET_ALL}" if color else f"{indent}{line}")


def _progress_bar(pct, width=40):
    filled = max(0, min(width, round(width * pct / 100)))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:3d}%"


def refresh_all_metadata(client, section, wait=True):
    """Start 'Refresh All Metadata' on a library and follow it via /activities.
    Esc stops watching - the server keeps working. Returns a status line."""
    title = section.get("title", "?")
    try:
        client.refresh_section(section["key"], force=True)
    except Exception as ex:
        return f"{Fore.RED}Refresh All Metadata failed: {ex}{Style.RESET_ALL}"
    if not wait or not _tui_supported():
        return (f"{Fore.GREEN}» Refresh All Metadata started on '{title}'."
                f"{Style.RESET_ALL}")

    clear_screen()
    log_info(f"Refresh All Metadata running on '{Fore.CYAN}{title}{Style.RESET_ALL}'.")
    print(f"{Style.DIM}Plex re-downloads metadata for every item. Esc stops watching "
          f"(the server keeps going).{Style.RESET_ALL}")
    print()
    started = time.monotonic()
    seen = False
    aborted = False
    with _RawMode():
        while True:
            acts = [a for a in client.activities()
                    if "library" in str(a.get("type", "")).lower()
                    or "refresh" in str(a.get("type", "")).lower()]
            if acts:
                seen = True
                pct = max(int(a.get("progress") or 0) for a in acts)
                sub = (acts[0].get("subtitle") or acts[0].get("title") or "")[:60]
                sys.stdout.write(f"\r  {Fore.CYAN}{_progress_bar(pct)}{Style.RESET_ALL}  "
                                 f"{sub}\x1b[K")
                sys.stdout.flush()
            elif seen:
                break                       # activity finished
            elif time.monotonic() - started > 25:
                break                       # nothing ever showed up - server was quick
            else:
                sys.stdout.write(f"\r  {Fore.CYAN}{_progress_bar(0)}{Style.RESET_ALL}  "
                                 f"waiting for the server…\x1b[K")
                sys.stdout.flush()
            if time.monotonic() - started > 3600:
                break
            if _read_key(1.0) == "esc":     # poll once a second, Esc aborts watching
                aborted = True
                break
    print()
    if aborted:
        return (f"{Fore.YELLOW}» Still refreshing '{title}' in the background."
                f"{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}{_progress_bar(100)}{Style.RESET_ALL}")
    return f"{Fore.GREEN}» Refresh All Metadata finished on '{title}'.{Style.RESET_ALL}"


def lang_name_from_code(code):
    """'en' -> 'English'. Falls back to the raw code (also handles 'en-US')."""
    if not code:
        return None
    code = str(code)
    for c, name in PLEX_LANGS:
        if c.lower() == code.lower():
            return name
    base = code.split("-")[0].lower()
    for c, name in PLEX_LANGS:
        if c.lower() == base:
            return name
    return code


def library_language(client, section):
    """The library's own metadata language ('en'). Falls back to the section prefs
    when the section listing doesn't carry it."""
    code = section.get("language")
    if code:
        return code
    try:
        j = client.get_json(f"/library/sections/{section['key']}/prefs")
        for s in j.get("MediaContainer", {}).get("Setting", []) or []:
            if s.get("id") in ("languageOverride", "language"):
                if s.get("value"):
                    return s["value"]
    except Exception:
        pass
    return None


def read_metadata_languages(client, rows, section):
    """Ask the server for each item's real Metadata language (languageOverride).
    Empty means 'Library default', so the library's own language is used. Updates
    rows in place (lang / certain) and shows progress. Esc aborts."""
    lib_code = library_language(client, section)
    lib_name = lang_name_from_code(lib_code) or "library default"
    total = len(rows)
    clear_screen()
    log_info(f"Reading the real Metadata language of {total} item(s) from the server.")
    print(f"{Style.DIM}Library default for '{section.get('title','?')}' is "
          f"{lib_name}. Esc stops - already read items keep their value."
          f"{Style.RESET_ALL}")
    print()
    aborted = False
    with _RawMode():
        for i, r in enumerate(rows, 1):
            prefs = client.item_prefs(r["rk"])
            code = (prefs.get("languageOverride") or "").strip()
            if code:
                r["lang"] = lang_name_from_code(code)
                r["src"] = "item"
            else:
                r["lang"] = lib_name
                r["src"] = "library default"
            r["certain"] = True
            if i % 2 == 0 or i == total:
                pct = i * 100 // total
                sys.stdout.write(f"\r  {Fore.CYAN}{_progress_bar(pct)}{Style.RESET_ALL}  "
                                 f"({i}/{total})  {r['title'][:40]}\x1b[K")
                sys.stdout.flush()
            if _read_key(0) == "esc":
                aborted = True
                break
    print()
    if aborted:
        log_warn("Stopped - the rest keeps the guess from the title text.")
    return not aborted


def _pick_library(client, default=0):
    """Pick a video library. Returns (section, index, single) where `single` means
    there was nothing to choose from (so Esc one level up should leave the tool)."""
    secs = client.sections()
    vid = [s for s in secs if s["type"] in ("show", "movie")] or secs
    if not vid:
        die("The server has no libraries.")
    if len(vid) == 1:
        return vid[0], 0, True

    def scan_lib(i):
        try:
            client.refresh_section(vid[i]["key"])
            return f"{Fore.GREEN}» Scan Library Files started on '{vid[i]['title']}'.{Style.RESET_ALL}"
        except Exception as ex:
            return f"{Fore.RED}Scan Library Files failed: {ex}{Style.RESET_ALL}"

    labels = [s["title"] for s in vid]
    libtags = [f"{Style.DIM}[{s['type']}]{Style.RESET_ALL}" for s in vid]
    i = interactive_menu("Select a library:", labels, default=default,
                         allow_cancel=True, tags=libtags, refresh_cb=scan_lib,
                         refresh_all_cb=(lambda idx: refresh_all_metadata(client, vid[idx])
                                         if idx is not None else None))
    if i is None:
        return None, default, False
    return vid[i], i, False


def _labels_of(md):
    return [t.get("tag") for t in (md.get("Label") or []) if t.get("tag")]


def _dedup_titles(titles):
    """Collapse variants of the same show to a single entry, preferring the plain
    title. A variant is a title that ends in ' - <suffix>' (e.g. 'Something in the
    Rain - Viki'); such rows are grouped with the base title ('Something in the
    Rain') and only the base is kept. A title with no sibling variant is kept exactly
    as-is (so a lone 'Law & Order - SVU' is never shortened). First-appearance order
    is preserved. Returns (deduped_list, merged_count)."""
    def base(t):
        parts = t.rsplit(" - ", 1)
        return parts[0].strip() if len(parts) == 2 and parts[1].strip() else t.strip()

    groups, order = {}, []
    for t in titles:
        b = base(t)
        k = b.casefold()
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"base": b, "members": []}
            order.append(k)
        g["members"].append(t)
    out = []
    for k in order:
        g = groups[k]
        members = g["members"]
        plain = [m for m in members if m.strip().casefold() == g["base"].casefold()]
        out.append(plain[0] if plain else min(members, key=len))
    return out, len(titles) - len(out)


def _export_titles(section_title, shown_rows, scope_name=None):
    """Write the given rows' titles to a .txt in the current directory, one per line.
    Variants of the same show (e.g. 'X' and 'X - Viki') are merged to a single line
    using the main title. Filename: plex_<library>[_<filter>]_<timestamp>.txt.
    Returns (abspath, count, merged), or (None, 0, 0) if there was nothing to write."""
    titles = [(r.get("title") or strip_ansi(r.get("label") or "")).strip() for r in shown_rows]
    titles = [t for t in titles if t]
    if not titles:
        return (None, 0, 0)
    titles, merged = _dedup_titles(titles)

    def slug(s):
        s = re.sub(r"[^\w]+", "_", strip_ansi(s or ""), flags=re.UNICODE).strip("_")
        return s[:40] or "all"

    parts = ["plex", slug(section_title)]
    if scope_name:
        parts.append(slug(scope_name))
    parts.append(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    path = os.path.abspath("_".join(parts) + ".txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(titles) + "\n")
    return (path, len(titles), merged)


def _country_index_from_metadata(client, rows):
    """Fallback country index (name -> set of ratingKeys) built by reading each item's
    Country metadata - used when the server doesn't expose the country filter."""
    idx = {}
    md_map = client.get_metadata_many([r["rk"] for r in rows if r.get("rk")])
    for rk, md in md_map.items():
        for c in (md.get("Country") or []):
            if c.get("tag"):
                idx.setdefault(c["tag"], set()).add(str(rk))
    return idx


def labels_flow(client, args):
    """Manage custom labels: pick a library, check off several shows/movies
    (search supports * and ? wildcards), then set which labels they should have.
    Returns True if something was changed, False otherwise."""
    lib_idx = 0
    item_cursor = [0]
    item_view = {}   # keeps search filter + cursor when stepping back into the list
    while True:  # library level
        sec, lib_idx, single_lib = _pick_library(client, default=lib_idx)
        if sec is None:
            return False

        items = client.items_in_section(sec["key"], sec["type"])
        if not items:
            log_warn("The library is empty.")
            if single_lib:
                return False
            continue
        items.sort(key=lambda x: (x["title"] or "").lower())
        rows = [{"rk": it["ratingKey"],
                 "label": f"{it['title']} ({it['year']})" if it.get("year") else it["title"],
                 "selected": False, "title": it["title"]} for it in items]

        label_cache = {}
        country_cache = {}

        def pick_label_scope():
            """'l' - list every label in this library and narrow the view to the
            shows/movies carrying it (or to the ones with no label at all).
            Case variants of the same name are merged into one entry."""
            entries = client.section_label_entries(sec["key"])
            if not entries:
                log_warn("This library has no labels yet.")
                return None
            grouped = {}   # casefolded -> {"title": display, "ids": [...]}
            for e in entries:
                g = grouped.setdefault(e["title"].casefold(),
                                       {"title": e["title"], "ids": []})
                g["ids"].append(e["id"])
            groups = [grouped[k] for k in sorted(grouped, key=lambda c: grouped[c]["title"].lower())]
            names = [g["title"] + (f"   {Style.DIM}({len(g['ids'])} spellings){Style.RESET_ALL}"
                                   if len(g["ids"]) > 1 else "") for g in groups]
            names.append(f"{Style.DIM}(items with no label){Style.RESET_ALL}")
            i = interactive_menu("Show only items with label:", names, allow_cancel=True)
            if i is None:
                return None

            def members(g):
                out = set()
                for lid in g["ids"]:
                    if lid not in label_cache:
                        try:
                            label_cache[lid] = set(client.items_with_label(
                                sec["key"], sec["type"], lid))
                        except Exception as ex:
                            log_warn(f"Could not list items for '{g['title']}': {ex}")
                            label_cache[lid] = set()
                    out |= label_cache[lid]
                return out

            if i == len(groups):  # no label at all
                labelled = set()
                for g in groups:
                    labelled |= members(g)
                return ("label: (none)", {str(r["rk"]) for r in rows} - labelled)
            g = groups[i]
            rks = members(g)
            if not rks:
                log_warn(f"No items carry '{g['title']}'.")
                return None
            return (f"label: {g['title']}", rks)

        def pick_country_scope():
            """'c' - list the countries of origin in this library and narrow the view
            to the titles from the chosen one (searchable by typing). Uses Plex's fast
            server-side country filter when available, otherwise reads each item's
            Country metadata. Works in any library that stores a country of origin."""
            entries = country_cache.get("entries")
            if entries is None:
                entries = client.section_country_entries(sec["key"], sec["type"])
                country_cache["entries"] = entries
            if entries:   # fast path: server-side filter
                order_e = sorted(entries, key=lambda e: (e["title"] or "").lower())
                i = interactive_menu("Show only titles from country:",
                                     [e["title"] for e in order_e], allow_cancel=True)
                if i is None:
                    return None
                e = order_e[i]
                try:
                    rks = set(client.items_with_country(sec["key"], sec["type"], e["id"]))
                except Exception as ex:
                    log_warn(f"Could not filter by country: {ex}")
                    return None
                rks &= {str(r["rk"]) for r in rows}
                if not rks:
                    log_warn(f"No titles from {e['title']} in this library.")
                    return None
                return (f"country: {e['title']}", rks)
            # fallback: read Country from each item's metadata (cached)
            idx = country_cache.get("idx")
            if idx is None:
                clear_screen()
                log_info("Reading country of origin from metadata…")
                idx = _country_index_from_metadata(client, rows)
                country_cache["idx"] = idx
            if not idx:
                log_warn("No country-of-origin data found in this library.")
                _pause_to_menu()
                return None
            names = sorted(idx, key=lambda c: c.lower())
            labels = [f"{c}   {Style.DIM}({len(idx[c])}){Style.RESET_ALL}" for c in names]
            i = interactive_menu("Show only titles from country:", labels, allow_cancel=True)
            if i is None:
                return None
            return (f"country: {names[i]}", {str(x) for x in idx[names[i]]})

        def export_shown(shown_rows, scope_name=None):
            """'x' - write the titles currently shown (after any search / label /
            country filter) to a .txt file, one per line, for handing off elsewhere.
            Variants of the same show (e.g. 'X' and 'X - Viki') are merged to one line."""
            path, n, merged = _export_titles(sec["title"], shown_rows, scope_name)
            if not path:
                log_warn("Nothing to export in the current view.")
                _pause_to_menu()
                return
            clear_screen()
            log_done(f"Exported {n} title(s) (one per line) to:")
            print(f"    {Fore.CYAN}{path}{Style.RESET_ALL}")
            if merged:
                print(f"    {Style.DIM}{merged} duplicate variant(s) merged into their "
                      f"main title{Style.RESET_ALL}")
            if scope_name:
                print(f"    {Style.DIM}filter: {scope_name}{Style.RESET_ALL}")
            _pause_to_menu()

        while True:  # item selection level
            header = [
                f"{Fore.CYAN}Library:{Style.RESET_ALL} {sec['title']}   ({len(rows)} items)",
                f"{Style.DIM}/ = filter (e.g. *romance*) · + = check by pattern · "
                f"- = uncheck by pattern · l = only one label · c = only one country · "
                f"x = export shown to .txt · a = all shown · n = none · i = invert"
                f"{Style.RESET_ALL}",
                "",
            ]
            res = checkbox_menu("Select shows/movies to label:", rows, header=header,
                                start_pos=item_cursor[0], pos_out=item_cursor,
                                ui_state=item_view, scope_picker=pick_label_scope,
                                country_picker=pick_country_scope, export_cb=export_shown)
            if res is None:
                if single_lib:
                    return False  # nothing to step back to
                break  # back to the library picker
            picked = [r for r in rows if r["selected"]]
            if not picked:
                log_info("Nothing selected.")
                continue

            if _labels_editor(client, sec, picked, args):
                return True
            # editor cancelled -> back to the item selection (keeps checkboxes)


def _label_groups(client, section_key):
    """Labels of a library grouped case-insensitively:
    [{'title': display, 'ids': [filter ids]}, ...]"""
    grouped = {}
    for e in client.section_label_entries(section_key):
        g = grouped.setdefault(e["title"].casefold(), {"title": e["title"], "ids": []})
        g["ids"].append(e["id"])
        if e["title"] not in g.setdefault("names", [e["title"]]):
            g["names"].append(e["title"])
    for g in grouped.values():
        g.setdefault("names", [g["title"]])
    return [grouped[k] for k in sorted(grouped, key=lambda c: grouped[c]["title"].lower())]


# Plex metadata types a label can sit on. A label left on a season, an episode or
# a collection keeps the tag alive even after you cleared it from every show.
_LABEL_TYPES = [("item", None), ("season", 3), ("episode", 4), ("collection", 18)]


def _purge_label_everywhere(client, sec, group):
    """Strip a label (all case variants) from every object in the library: items,
    seasons, episodes and collections. Returns the number of objects touched."""
    touched = 0
    for kind, tnum in _LABEL_TYPES:
        type_num = SECTION_TYPE_NUM.get(sec["type"], 2) if tnum is None else tnum
        rks = set()
        for lid in group["ids"]:
            try:
                rks |= set(client.items_with_label(sec["key"], sec["type"], lid,
                                                   type_num=type_num))
            except Exception:
                pass          # some types don't exist in every library
        if not rks:
            continue
        try:
            client.edit_labels(sec["key"], sec["type"], sorted(rks),
                               remove=group["names"], type_num=type_num)
            touched += len(rks)
            log_done(f"Removed from {len(rks)} {kind}(s).")
        except Exception as ex:
            log_warn(f"Could not clear {kind}s: {ex}")
    if not touched:
        log_info("Nothing carries this label any more.")
    log_info("Plex has no API to delete a tag itself. If it still shows up: it may be "
             "used in a managed user's restrictions, and the empty tag disappears after "
             "Settings > Manage > Troubleshooting > Optimize database.")
    return touched


def _labels_editor(client, sec, picked, args):
    """Tri-state label editor for the picked items. Returns True if applied.
    Labels are matched case-insensitively: Plex can hold the same tag in several
    letter cases ('iPrima' / 'IPrima'), so they are shown as one row and removal
    sends every case variant that is actually present on the items."""
    while True:
        clear_screen()
        log_info(f"Loading labels for {len(picked)} item(s)…")
        md_map = client.get_metadata_many([r["rk"] for r in picked])
        raw_per_item = {}          # rk -> set of raw label strings on that item
        for r in picked:
            raw_per_item[r["rk"]] = set(_labels_of(md_map.get(str(r["rk"])) or {}))
        try:
            known = client.section_labels(sec["key"])
        except Exception:
            known = []

        # group by casefolded name. Plex capitalises the first letter when it returns
        # a label inside item metadata ('iPrima' -> 'IPrima'), so the library's own
        # label list is the canonical spelling and wins for display.
        variants = {}              # cf -> list of raw spellings seen on items
        for s in raw_per_item.values():
            for lab in s:
                variants.setdefault(lab.casefold(), []).append(lab)
        known_cf = {}
        for lab in known:
            known_cf.setdefault(lab.casefold(), lab)
            variants.setdefault(lab.casefold(), []).append(lab)
        display = {cf: known_cf.get(cf) or max(set(v), key=v.count)
                   for cf, v in variants.items()}
        cf_per_item = {rk: {l.casefold() for l in s} for rk, s in raw_per_item.items()}

        n = len(picked)
        rows = []
        for cf in sorted(display, key=lambda c: display[c].lower()):
            cnt = sum(1 for s in cf_per_item.values() if cf in s)
            state = "on" if cnt == n else ("off" if cnt == 0 else "mixed")
            suffix = "" if cnt in (0, n) else f"   {Style.DIM}({cnt}/{n}){Style.RESET_ALL}"
            rows.append({"cf": cf, "lab": display[cf], "label": display[cf] + suffix,
                         "state": state, "orig": state})

        def raw_variants(cf):
            """Every spelling of this label that is really present on the items."""
            out = []
            for s in raw_per_item.values():
                for lab in s:
                    if lab.casefold() == cf and lab not in out:
                        out.append(lab)
            return out or [display[cf]]

        header = [
            f"{Fore.CYAN}Items:{Style.RESET_ALL} " +
            ", ".join(r["title"] for r in picked[:6]) +
            (f" … (+{len(picked) - 6} more)" if len(picked) > 6 else ""),
            f"{Style.DIM}[x] = all items get it · [ ] = removed from all · "
            f"[~] = leave as it is · Enter on a » row runs it · Enter = apply{Style.RESET_ALL}",
            "",
        ]
        extra_rows = [
            {"action": "new", "label": f"{Fore.GREEN}+ New label…{Style.RESET_ALL}",
             "state": "mixed", "orig": "mixed"},
            {"action": "rename", "label": f"{Fore.CYAN}» Rename a label (on these items)…{Style.RESET_ALL}",
             "state": "mixed", "orig": "mixed"},
            {"action": "purge", "label": f"{Fore.YELLOW}» Clean a label out of the whole library "
                                         f"(items, seasons, episodes, collections)…{Style.RESET_ALL}",
             "state": "mixed", "orig": "mixed"},
            {"action": "raw", "label": f"{Style.DIM}» Show the exact label values the server "
                                       f"returns (diagnostics)…{Style.RESET_ALL}",
             "state": "mixed", "orig": "mixed"},
        ]
        menu_rows = rows + extra_rows
        if not rows:
            log_info("No labels exist yet — use '+ New label…' to create one.")

        res = checkbox_menu(f"Labels for {n} item(s):", menu_rows, header=header, tristate=True)
        if res is None:
            return False  # back to item selection

        # an action row left "on" means: run that action
        act = next((r for r in extra_rows if r.get("state") == "on"), None)
        if act:
            act["state"] = act["orig"]
            if act["action"] == "new":
                name = ask_line("New label name")
                if name:
                    client.edit_labels(sec["key"], sec["type"], [r["rk"] for r in picked],
                                       add=[name])
                    log_done(f"Label '{name}' added to {n} item(s).")
                    return True
                continue
            elif act["action"] == "raw":
                clear_screen()
                log_info("Exact strings returned by the Plex API (quoted, so letter case "
                         "is unambiguous). Plex Web may display them capitalised even when "
                         "they are stored differently.")
                print()
                print(f"  {Fore.CYAN}Library labels ({sec['title']}):{Style.RESET_ALL}")
                lib_entries = client.section_label_entries(sec["key"])
                if lib_entries:
                    for e in lib_entries:
                        print(f"      id={e['id']!s:<8} {e['title']!r}")
                else:
                    print("      (none)")
                print()
                print(f"  {Fore.CYAN}On the selected items:{Style.RESET_ALL}")
                for r in picked[:20]:
                    labs = sorted(raw_per_item[r["rk"]])
                    print(f"      {r['title'][:40]:<40} {[l for l in labs]!r}")
                if len(picked) > 20:
                    print(f"      … (+{len(picked) - 20} more)")
                print()
                _pause_to_menu()
                continue
            elif act["action"] == "purge":
                groups = _label_groups(client, sec["key"])
                if not groups:
                    log_warn("This library has no labels.")
                    continue
                names = [g["title"] + (f"   {Style.DIM}({len(g['names'])} spellings){Style.RESET_ALL}"
                                       if len(g["names"]) > 1 else "") for g in groups]
                i = interactive_menu("Clean which label out of the whole library?",
                                     names, allow_cancel=True)
                if i is None:
                    continue
                g = groups[i]
                clear_screen()
                log_info(f"This removes '{g['title']}' from every item, season, episode "
                         f"and collection in '{sec['title']}'.")
                print()
                if not args.yes:
                    a2 = ask_yes_back("Proceed?", default=False)
                    if not a2:
                        continue
                print()
                _purge_label_everywhere(client, sec, g)
                return True
            else:  # rename
                if not rows:
                    log_warn("There is no label to rename.")
                    continue
                i = interactive_menu("Rename which label?", [r["lab"] for r in rows],
                                     allow_cancel=True)
                if i is None:
                    continue
                cf = rows[i]["cf"]
                old = rows[i]["lab"]
                new = ask_line(f"Rename '{old}' to", default=old)
                if not new or new == old:
                    continue
                targets = [r["rk"] for r in picked if cf in cf_per_item[r["rk"]]]
                if not targets:
                    log_warn(f"None of the selected items has '{old}'.")
                    continue
                log_info("Plex has no rename API for tags, so this removes the old "
                         "label and adds the new one on the affected items.")
                if old.casefold() == new.casefold():
                    # case-only rename: do it in two steps, otherwise Plex can fold
                    # the add back onto the existing tag and nothing changes
                    client.edit_labels(sec["key"], sec["type"], targets,
                                       remove=raw_variants(cf))
                    client.edit_labels(sec["key"], sec["type"], targets, add=[new])
                    log_info("This only changed letter case; Plex may keep the old "
                             "spelling in filter lists until you run Optimize database.")
                else:
                    client.edit_labels(sec["key"], sec["type"], targets,
                                       add=[new], remove=raw_variants(cf))
                log_done(f"Renamed '{old}' -> '{new}' on {len(targets)} item(s).")
                return True

        # compute add/remove from the tri-state result
        to_add = [r["lab"] for r in rows if r["state"] == "on" and r["orig"] != "on"]
        to_remove = []
        for r in rows:
            if r["state"] == "off" and r["orig"] != "off":
                to_remove.extend(raw_variants(r["cf"]))   # drop every case variant
        if not to_add and not to_remove:
            log_info("Nothing to change.")
            continue

        clear_screen()
        log_info(f"On {n} item(s):")
        if to_add:
            print(f"    {Fore.GREEN}add:{Style.RESET_ALL}    " + ", ".join(to_add))
        if to_remove:
            print(f"    {Fore.RED}remove:{Style.RESET_ALL} " + ", ".join(to_remove))
        print()
        for r in picked[:20]:
            print(f"    {r['title']}")
        if len(picked) > 20:
            print(f"    … (+{len(picked) - 20} more)")
        print()
        if not args.yes:
            ans = ask_yes_back("Apply these label changes?", default=True)
            if ans is None:
                continue  # Esc -> back to the label editor (keeps the tri-states)
            if not ans:
                continue
        try:
            client.edit_labels(sec["key"], sec["type"], [r["rk"] for r in picked],
                               add=to_add, remove=to_remove)
        except Exception as ex:
            log_warn(f"Failed: {ex}")
            return False
        log_done(f"Labels updated on {n} item(s).")
        log_info("The change shows in the client after a refresh.")
        return True


# ---------------------------------------------------------------------------
# Titles flow (find non-English titles, switch them to the English one)
# ---------------------------------------------------------------------------
_SCRIPT_RANGES = [
    ("Hangul (Korean)", ((0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F),
                         (0xAC00, 0xD7AF))),
    ("Kana (Japanese)", ((0x3040, 0x30FF), (0x31F0, 0x31FF))),
    ("Han (Chinese/Japanese)", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    ("Cyrillic", ((0x0400, 0x04FF), (0x0500, 0x052F))),
    ("Greek", ((0x0370, 0x03FF),)),
    ("Arabic", ((0x0600, 0x06FF), (0x0750, 0x077F))),
    ("Hebrew", ((0x0590, 0x05FF),)),
    ("Thai", ((0x0E00, 0x0E7F),)),
    ("Devanagari", ((0x0900, 0x097F),)),
]


# Letters that give a Latin-script language away. Lowercase only and strictly
# non-ASCII - 'İ'.lower() contains a plain 'i', which would match almost anything.
_LANG_CHARS = [
    ("Czech", "ěřů"),
    ("Polish", "ąćęłńśźż"),
    ("Slovak", "ľĺŕô"),
    ("Hungarian", "őű"),
    ("Romanian", "ăîșț"),
    ("Turkish", "ğı"),
    ("Spanish", "ñ¿¡"),
    ("Portuguese", "ãõ"),
    ("German", "ß"),
    ("Nordic", "åæø"),
    ("French", "œ"),
]
# Weaker, shared signals - a family rather than one language.
_LANG_CHARS_SHARED = [
    ("Czech/Slovak", "žščďťň"),
    ("German", "äöü"),
    ("French/Romance", "çèêëàùû"),
]
# Function words that give a language away even without diacritics.
_LANG_WORDS = {
    "Czech": {"a", "o", "u", "v", "ve", "na", "do", "po", "za", "se", "si", "je",
              "jsou", "byl", "byla", "bylo", "byly", "neni", "nebylo", "to", "ta", "ten",
              "ty", "jak", "kdy", "kde", "ze", "ale", "nebo", "az", "ano", "ne",
              "muj", "moje", "nas", "nase", "jeho", "jeji", "jedna", "jedno", "jednou",
              "vsechno", "vsema", "mezi", "bez", "pro", "pres", "tak", "jen", "jeste",
              "uz", "kdyz", "aby", "laska", "lasky", "srdce", "zivot", "svatba", "sny",
              "svet", "svetlo", "pripad", "muz", "zena", "divka", "kluk", "skola",
              "mesto", "mestecko", "domu", "beru", "peklo", "polibek", "hmota", "duse"},
    "Slovak": {"sa", "ako", "ked", "aby", "alebo", "velmi", "zivot", "laska", "svet",
               "dievca"},
    "English": {"the", "an", "of", "and", "or", "in", "on", "at", "to", "for",
                "with", "my", "your", "his", "her", "our", "their", "is", "are", "was",
                "were", "be", "you", "we", "they", "it", "this", "that", "from", "by",
                "about", "love", "story", "man", "woman", "girl", "boy", "life",
                "world", "house", "night", "day", "who", "what", "when", "where",
                "why", "how", "up", "out", "down", "into", "over", "under", "again"},
    "German": {"der", "die", "das", "und", "ein", "eine", "ist", "mit", "von", "fur",
               "nicht", "auf", "aus", "dem", "den", "im", "zu"},
    "French": {"le", "les", "un", "une", "des", "et", "du", "au", "aux", "la", "de",
               "pour", "dans", "avec", "sur", "est", "qui", "que", "mon", "ma"},
    "Spanish": {"el", "los", "las", "una", "y", "del", "que", "en", "la", "de", "casa",
                "por", "para", "con", "es", "mi", "su", "amor", "vida"},
    "Italian": {"il", "lo", "gli", "una", "di", "del", "che", "per", "con",
                "non", "sono", "amore", "vita"},
}
_LANG_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _deaccent(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def title_language(text):
    """Best guess at the language of a title. Non-Latin scripts are decided by the
    writing system (reliable); Latin-script languages are guessed from distinctive
    letters and common function words, so treat them as a hint, not a verdict."""
    text = (text or "").strip()
    if not text:
        return "unknown"
    script = title_script(text)
    if script.startswith("Hangul"):
        return "Korean"
    if script.startswith("Kana"):
        return "Japanese"
    if script.startswith("Han"):
        return "Chinese/Japanese"
    if script in ("Cyrillic", "Greek", "Arabic", "Hebrew", "Thai", "Devanagari"):
        return script
    low = text.lower()
    for lang, chars in _LANG_CHARS:
        if any(c in low for c in chars):
            return lang
    for lang, chars in _LANG_CHARS_SHARED:
        if any(c in low for c in chars):
            return lang
    words = {w.lower() for w in _LANG_WORD_RE.findall(_deaccent(text))}
    if words:
        hits = {lang: len(words & ws) for lang, ws in _LANG_WORDS.items()}
        best = max(hits, key=lambda k: hits[k])
        # one shared little word ("del" in "Hotel Del Luna") proves nothing - ask for two
        if hits[best] >= 2:
            rivals = sorted(k for k, v in hits.items() if v == hits[best])
            if len(rivals) > 1 and "English" in rivals:
                rivals.remove("English")   # a tie with English is not English enough
            return rivals[0] + "?"         # word-based guess: flagged as uncertain
    return ("unknown (has accents)" if script == "Latin (accented)" else "unknown")


def title_script(text):
    """Which writing system a title uses: a script name, 'Latin (accented)' for
    things like Czech/French, or 'Latin' for plain ASCII."""
    text = text or ""
    for name, ranges in _SCRIPT_RANGES:
        for ch in text:
            o = ord(ch)
            if any(lo <= o <= hi for lo, hi in ranges):
                return name
    return "Latin" if all(ord(c) < 128 for c in text) else "Latin (accented)"


def _lang_candidate(client, rating_key, current, guid=None, lang="en"):
    """Name the Plex agent has for THIS item in `lang`. Prefers the candidate whose
    guid equals the item's current guid, i.e. the very same match - no re-matching
    happens, only the title text is read. Returns
    {'name','score','exact'} or None; exact=False means the guid did not line up,
    so the name may belong to a different entry and needs a human check."""
    try:
        cands = client.matches(rating_key, language=lang)
    except Exception:
        return None
    same, best = None, None
    for c in cands:
        name = (c.get("name") or "").strip()
        if not name or name == current:
            continue
        if guid and c.get("guid") == guid:
            same = {"name": name, "score": c["score"], "exact": True}
            break
        if best is None or c["score"] > best["score"]:
            best = {"name": name, "score": c["score"], "exact": False}
    return same or best


def titles_flow(client, args):
    """Find items whose title isn't in English and switch the title to the English
    one the Plex agent knows. Only the title field is written (and locked) - streams,
    watched state, subtitles and artwork are untouched. Returns True if changed."""
    tgt = pick_target_language()
    if tgt is None:
        return False
    lang_code, lang_name = tgt
    lib_idx = 0
    item_cursor = [0]
    item_view = {}
    lang_read_done = [False]
    while True:
        lang_read_done[0] = False
        sec, lib_idx, single_lib = _pick_library(client, default=lib_idx)
        if sec is None:
            return False
        items = client.items_in_section(sec["key"], sec["type"])
        if not items:
            log_warn("The library is empty.")
            if single_lib:
                return False
            continue
        items.sort(key=lambda x: (x["title"] or "").lower())
        rows = []
        for it in items:
            rows.append({"rk": it["ratingKey"], "title": it["title"],
                         "lang": title_language(it["title"]), "certain": False,
                         "src": "guessed from the title",
                         "guid": it.get("guid"), "selected": False, "label": ""})

        # the reliable source: each item's Metadata language (Library default -> the
        # library's own language). Costs one request per item, so it is offered, not forced.
        if not lang_read_done[0]:
            clear_screen()
            log_info(f"Library '{sec['title']}' has {len(rows)} item(s).")
            print()
            print_wrapped("The exact Metadata language comes from the server and needs "
                          "one request per item; the locked fields come along in the same "
                          "pass (batched, so they are cheap).", color=Style.DIM)
            print_wrapped("You can skip this and do it later with 'm' for just the items "
                          "you pick, or from the 'l' menu.", color=Style.DIM)
            print()
            ans = ask_yes_back("Read it for all items now?", default=False)
            if ans:
                read_metadata_languages(client, rows, sec)
                read_locked_fields(client, rows)
            lang_read_done[0] = True

        # manual-split detection: several library items sharing one match (same guid).
        # Marked with a [split] tag; computed before relabel so relabel can show it and
        # it survives a metadata re-read (which rebuilds the tags).
        guid_n = {}
        for r in rows:
            if r.get("guid"):
                guid_n[r["guid"]] = guid_n.get(r["guid"], 0) + 1
        for r in rows:
            r["dup"] = bool(r.get("guid") and guid_n.get(r["guid"], 0) > 1)

        def relabel():
            """Title stays in the label; the language, lock and split notes go to
            r['tag'], which the menu prints in its own aligned column."""
            lang_txt, lock_txt = {}, {}
            for r in rows:
                hit = _is_target_lang(r["lang"], lang_name)
                mark = "" if (r.get("certain") or str(r["lang"]).startswith("unknown")) else "?"
                lang_txt[id(r)] = "" if hit else f"[{r['lang']}{mark}]"
                if sort_title_differs(r["title"], r.get("sort")):
                    lock_txt[id(r)] = f"[sort: {r['sort']}]"
                if r.get("locked"):
                    order_pref = ["title", "titleSort", "originalTitle", "summary"]
                    names = ([f for f in order_pref if f in r["locked"]]
                             + sorted(f for f in r["locked"] if f not in order_pref))
                    shown = [lock_field_label(f) for f in names[:4]]
                    more = f" +{len(names) - 4}" if len(names) > 4 else ""
                    lock_txt[id(r)] = (lock_txt.get(id(r), "") + " " if lock_txt.get(id(r)) else "") \
                                      + f"[locked: {', '.join(shown)}{more}]"
                else:
                    lock_txt.setdefault(id(r), "")
            lw = max([len(v) for v in lang_txt.values()] or [0])
            for r in rows:
                lt, kt = lang_txt[id(r)], lock_txt[id(r)]
                pad = " " * (lw - len(lt) + (2 if lw else 0))
                tag = ""
                if lt:
                    tag += f"{Style.DIM}{lt}{Style.RESET_ALL}"
                if kt:
                    tag += f"{pad}{Fore.YELLOW}{kt}{Style.RESET_ALL}"
                elif lt:
                    tag = f"{Style.DIM}{lt}{Style.RESET_ALL}"
                if r.get("dup"):     # manual split -> keep it in the aligned tag column
                    tag += f"{'  ' if tag else ''}{Fore.YELLOW}[split]{Style.RESET_ALL}"
                r["label"] = r["title"]
                r["tag"] = tag
        relabel()

        def do_reread(subset=None):
            """Read the exact Metadata language - only for the rows handed over
            (checked ones plus the row under the cursor), not the whole library."""
            targets = [r for r in (subset or rows) if r.get("rk")]
            if not targets:
                log_warn("Nothing is checked - check items or move the cursor onto one.")
                _pause_to_menu()
                return
            read_metadata_languages(client, targets, sec)
            relabel()
        counts = {}
        for r in rows:
            counts[r["lang"]] = counts.get(r["lang"], 0) + 1

        def _with_exact_lang(tag, rks):
            """Whatever a filter picked, read its real Metadata language right away -
            these sets are small, so it costs a handful of requests."""
            subset = [r for r in rows if str(r["rk"]) in rks and not r.get("certain")]
            if subset:
                read_metadata_languages(client, subset, sec)
                relabel()
            return (tag, rks)

        def pick_lang_scope():
            order_names = sorted(counts, key=lambda s: (-counts[s], s))
            names = [f"{s}   ({counts[s]})" for s in order_names]
            names.append(f"{Style.DIM}(everything that isn't {lang_name}){Style.RESET_ALL}")
            names.append(f"{Fore.CYAN}» Read the Metadata language of ALL items (slow)"
                         f"{Style.RESET_ALL}")
            names.append(f"{Fore.YELLOW}» Items with a LOCKED title or sort title"
                         f"{Style.RESET_ALL}")
            names.append(f"{Fore.YELLOW}» Items with a LOCKED title{Style.RESET_ALL}")
            names.append(f"{Fore.YELLOW}» Items with a LOCKED sort title{Style.RESET_ALL}")
            names.append(f"{Fore.YELLOW}» Items with ANY locked field{Style.RESET_ALL}")
            names.append(f"{Fore.YELLOW}» Items where title and sort title differ"
                         f"{Style.RESET_ALL}")
            i = interactive_menu("Show only titles detected as:", names, allow_cancel=True)
            if i is None:
                return None
            if i >= len(order_names) + 2:
                if any(r.get("locked") is None for r in rows):
                    read_locked_fields(client, rows)
                    relabel()
                which = i - (len(order_names) + 2)   # 0 = title|sort, 1 = title, 2 = sort, 3 = any
                if which == 0:
                    want, tag = {"title", "titleSort"}, "locked title or sort title"
                elif which == 1:
                    want, tag = {"title"}, "locked title"
                elif which == 2:
                    want, tag = {"titleSort"}, "locked sort title"
                elif which == 3:
                    want, tag = None, "any locked field"
                else:
                    rks = {str(r["rk"]) for r in rows
                           if sort_title_differs(r["title"], r.get("sort"))}
                    if not rks:
                        log_warn("Every item's sort title matches its title.")
                        return None
                    return (_with_exact_lang("title != sort title", rks))
                rks = {str(r["rk"]) for r in rows
                       if (r.get("locked") or set()) and
                          (want is None or (r["locked"] & want))}
                if not rks:
                    log_warn(f"No item matches '{tag}'.")
                    return None
                return _with_exact_lang(tag, rks)
            if i == len(order_names) + 1:
                do_reread(rows)
                return ("re-read", {str(r["rk"]) for r in rows})   # keeps every row visible
            if i == len(order_names):
                return (f"not {lang_name}", {str(r["rk"]) for r in rows
                                             if not _is_target_lang(r["lang"], lang_name)})
            s = order_names[i]
            return (s, {str(r["rk"]) for r in rows if r["lang"] == s})

        foreign = sum(1 for r in rows if not _is_target_lang(r["lang"], lang_name))
        header = [
            f"{Fore.CYAN}Library:{Style.RESET_ALL} {sec['title']}   ({len(rows)} items, "
            f"{Fore.YELLOW}{foreign}{Style.RESET_ALL} don't look like {lang_name}) "
            f"  {Fore.GREEN}target: {lang_name} [{lang_code}]{Style.RESET_ALL}",
            f"{Style.DIM}l = filter by detected language · / = search · +/- = check/uncheck "
            f"by pattern · a = all shown{Style.RESET_ALL}",
            f"{Style.DIM}m = read the exact Metadata language of the checked items "
            f"(+ the one under the cursor) · F9 = refresh their metadata · a trailing "
            f"'?' means the language was only guessed.{Style.RESET_ALL}",
            "",
        ]
        while True:
            res = checkbox_menu(f"Select items whose title should become {lang_name}:", rows,
                                header=header, start_pos=item_cursor[0], pos_out=item_cursor,
                                ui_state=item_view, scope_picker=pick_lang_scope,
                                action_cb=do_reread,
                                f9_cb=lambda checked: refresh_items(client, checked))
            if res is None:
                if single_lib:
                    return False
                break
            picked = [r for r in rows if r["selected"]]
            if not picked:
                log_info("Nothing selected.")
                continue
            # read the exact Metadata language of exactly these items before going on,
            # so the next screens work with facts instead of a guess from the title
            unknown = [r for r in picked if not r.get("certain")]
            if unknown:
                read_metadata_languages(client, unknown, sec)
                relabel()
            clear_screen()
            log_info(f"{len(picked)} item(s) selected. How should the {lang_name} "
                     f"title be applied?")
            same = [r for r in picked if _is_target_lang(r["lang"], lang_name)]
            if same:
                log_info(f"{len(same)} of them already have {lang_name} metadata "
                         f"(only the text may be off).")
            print()
            print(f"  {Fore.CYAN}1){Style.RESET_ALL} Rewrite just the title text — the "
                  f"item keeps everything else exactly as it is.")
            print(f"  {Fore.CYAN}2){Style.RESET_ALL} Set the item's metadata language to "
                  f"{lang_name} and refresh — also fixes episode titles and summary.")
            print(f"  {Fore.CYAN}3){Style.RESET_ALL} Fix Match to the item's own entry "
                  f"(same guid) in {lang_name} — the heavy hammer.")
            print(f"     {Style.DIM}Items with a locked title or a manual split are "
                  f"skipped by options 2 and 3 unless you insist.{Style.RESET_ALL}")
            print(f"     {Style.DIM}No re-match happens either way: the guid stays, so "
                  f"watched state, resume positions and the default audio/subtitle "
                  f"tracks are not touched.{Style.RESET_ALL}")
            print(f"     {Style.DIM}Option 2 does re-download artwork and summary from "
                  f"the agent (in English).{Style.RESET_ALL}")
            print()
            how = interactive_menu("Method:", [
                "Rewrite the title only (surgical)",
                f"Set metadata language to {lang_name} + refresh (full localisation)",
                f"Fix Match to the same entry in {lang_name} (re-match)",
            ], allow_cancel=True)
            if how is None:
                continue
            done = (_titles_apply(client, sec, picked, args, lang_code, lang_name) if how == 0
                    else _titles_relanguage(client, sec, picked, args, lang_code, lang_name) if how == 1
                    else _titles_fixmatch(client, sec, picked, args, lang_code, lang_name))
            if done:
                return True


# Metadata languages Plex agents can deliver (ISO 639-1 codes).
PLEX_LANGS = [
    ("en", "English"), ("cs", "Czech"),          # kept first on purpose
    ("ar", "Arabic"), ("bg", "Bulgarian"), ("ca", "Catalan"), ("zh", "Chinese"),
    ("hr", "Croatian"), ("da", "Danish"), ("nl", "Dutch"), ("et", "Estonian"),
    ("fi", "Finnish"), ("fr", "French"), ("de", "German"), ("el", "Greek"),
    ("he", "Hebrew"), ("hi", "Hindi"), ("hu", "Hungarian"), ("id", "Indonesian"),
    ("it", "Italian"), ("ja", "Japanese"), ("ko", "Korean"), ("lv", "Latvian"),
    ("lt", "Lithuanian"), ("ms", "Malay"), ("nb", "Norwegian"), ("fa", "Persian"),
    ("pl", "Polish"), ("pt", "Portuguese"), ("pt-BR", "Portuguese (Brazil)"),
    ("ro", "Romanian"), ("ru", "Russian"), ("sr", "Serbian"), ("sk", "Slovak"),
    ("sl", "Slovenian"), ("es", "Spanish"), ("es-MX", "Spanish (Mexico)"),
    ("sv", "Swedish"), ("th", "Thai"), ("tr", "Turkish"), ("uk", "Ukrainian"),
    ("vi", "Vietnamese"),
]


def pick_target_language(default_code="en"):
    """Choose the language titles should end up in. English and Czech are on top,
    everything else alphabetical; type to search. Returns (code, name) or None."""
    head = [l for l in PLEX_LANGS[:2]]
    rest = sorted(PLEX_LANGS[2:], key=lambda l: l[1].lower())
    langs = head + rest
    labels = [f"{name}   {Style.DIM}[{code}]{Style.RESET_ALL}" for code, name in langs]
    default = next((i for i, (c, _) in enumerate(langs) if c == default_code), 0)
    i = interactive_menu("Target language for the titles:", labels,
                         default=default, allow_cancel=True)
    return None if i is None else langs[i]


def _is_target_lang(detected, target_name):
    """Does a detected language line up with the chosen target?"""
    d = (detected or "").rstrip("?")
    if d == "library default":
        return True          # the library decides and we could not read which language
    if target_name == "English":
        return d in ("English", "unknown")
    return d == target_name or d.startswith(target_name + "/") or d.endswith("/" + target_name)


def refresh_items(client, rows):
    """Refresh metadata of the given items (PUT /library/metadata/{id}/refresh).
    No re-match, no unlocking: the item keeps its guid, its locked fields and its
    watched state - Plex just re-pulls the agent data. Esc stops early."""
    if not rows:
        log_warn("Nothing is checked - check the items first.")
        _pause_to_menu()
        return
    total = len(rows)
    clear_screen()
    log_info(f"Refreshing metadata of {total} checked item(s).")
    print(f"{Style.DIM}Locked fields stay as they are; watched state and files are "
          f"untouched. Esc stops.{Style.RESET_ALL}")
    print()
    ok = fail = 0
    with _RawMode():
        for i, r in enumerate(rows, 1):
            try:
                client.refresh_item(r["rk"])
                ok += 1
            except Exception:
                fail += 1
            pct = i * 100 // total
            sys.stdout.write(f"\r  {Fore.CYAN}{_progress_bar(pct)}{Style.RESET_ALL}  "
                             f"({i}/{total})  {str(r.get('title', ''))[:40]}\x1b[K")
            sys.stdout.flush()
            if _read_key(0) == "esc":
                break
    print()
    color = Fore.GREEN if not fail else Fore.YELLOW
    print(f"{color}Done: {ok} item(s) queued for refresh"
          + (f", {fail} failed" if fail else "") + f".{Style.RESET_ALL}")
    log_info("Plex works through them in the background.")
    _pause_to_menu()


def read_locked_fields(client, rows):
    """Which metadata fields are locked on each item (batched, so it is cheap).
    Sets r['locked'] to a set like {'title', 'titleSort'}."""
    total = len(rows)
    clear_screen()
    log_info(f"Reading locked fields of {total} item(s)…")
    done = 0
    step = 40
    for i in range(0, total, step):
        chunk = rows[i:i + step]
        try:
            md_map = client.get_metadata_many([r["rk"] for r in chunk])
        except Exception:
            md_map = {}
        for r in chunk:
            md = md_map.get(str(r["rk"])) or {}
            r["locked"] = _locked_fields(md)
            r["sort"] = md.get("titleSort") or ""
        done += len(chunk)
        pct = done * 100 // total
        sys.stdout.write(f"\r  {Fore.CYAN}{_progress_bar(pct)}{Style.RESET_ALL} "
                         f"({done}/{total})\x1b[K")
        sys.stdout.flush()
    print()
    return True


# Human-readable names for the metadata fields Plex can lock.
LOCK_FIELD_NAMES = {
    "title": "title", "titleSort": "sort title", "originalTitle": "original title",
    "summary": "summary", "tagline": "tagline", "studio": "studio",
    "contentRating": "content rating", "originallyAvailableAt": "release date",
    "year": "year", "rating": "rating", "audienceRating": "audience rating",
    "thumb": "poster", "art": "background", "banner": "banner", "theme": "theme",
    "genre": "genres", "collection": "collections", "label": "labels",
    "director": "directors", "writer": "writers", "producer": "producers",
    "country": "countries", "similar": "similar", "editionTitle": "edition",
}


def lock_field_label(name):
    return LOCK_FIELD_NAMES.get(name, name)


_SUFFIX_RE = re.compile(r"\s+[-\u2013\u2014]\s+([^-\u2013\u2014]{1,24})$")


def title_suffix(title):
    """A manual marker at the end of a title, e.g. ' - Viki' in
    'Vlastní (ne)cestou - Viki'. Returns '' when there is none."""
    m = _SUFFIX_RE.search(title or "")
    return f" - {m.group(1).strip()}" if m else ""


def sort_title_differs(title, sort):
    """True when the sort title is really something else. Plex drops a leading
    article ('The Wire' -> 'Wire') on its own, which is normal and not a mismatch."""
    if not sort:
        return False
    t, s = (title or "").strip(), sort.strip()
    if t == s:
        return False
    low = t.lower()
    for art in ("the ", "a ", "an "):
        if low.startswith(art) and t[len(art):].strip() == s:
            return False
    return True


def _locked_fields(md):
    """Field names an item has locked (Plex returns <Field name=.. locked=1/>)."""
    out = set()
    for f in (md or {}).get("Field", []) or []:
        if f.get("locked") in (1, True, "1", "true") and f.get("name"):
            out.add(f["name"])
    return out


def _protected_items(client, picked):
    """Items that must not be touched blindly:
      - a locked title (somebody named it by hand, e.g. '… - Viki')
      - the guid is shared with another item in the library (a manual SPLIT, where
        each part points at a different folder/version of the same show)
    Returns {ratingKey: [reasons]}."""
    reasons = {}
    try:
        md_map = client.get_metadata_many([r["rk"] for r in picked])
    except Exception:
        md_map = {}
    for r in picked:
        why = []
        if "title" in _locked_fields(md_map.get(str(r["rk"]))):
            why.append("title is locked")
        if r.get("dup"):
            why.append("split/duplicate entry (shares its match with another item)")
        if why:
            reasons[str(r["rk"])] = why
    return reasons


def _drop_protected(client, picked, what, args):
    """Filter out protected items. Returns the safe list (possibly everything, if the
    user knowingly opts in)."""
    prot = _protected_items(client, picked)
    if not prot:
        return picked
    clear_screen()
    log_warn(f"{len(prot)} of the {len(picked)} selected item(s) look risky for {what}:")
    for r in picked:
        why = prot.get(str(r["rk"]))
        if why:
            print(f"    {r['title']}   {Style.DIM}- {'; '.join(why)}{Style.RESET_ALL}")
    print()
    log_info("A locked title is usually a name you gave the item on purpose, and a "
             "split entry is a second copy that must keep its own identity. Changing "
             "those can undo a manual split or wipe your custom name.")
    print()
    safe = [r for r in picked if str(r["rk"]) not in prot]
    if not args.yes:
        inc = ask_yes_back(f"Skip these {len(prot)} and continue with the other "
                           f"{len(safe)}?", default=True)
        if inc is None:
            return []
        if not inc:
            inc2 = ask_yes_back("Include the risky ones anyway (not recommended)?",
                                default=False)
            if inc2:
                return picked
            return []
    if not safe:
        log_info("Nothing safe is left to change.")
        _pause_to_menu()
    return safe


def _titles_fixmatch(client, sec, picked, args, lang="en", lang_name="English"):
    """Fix Match each item to ITS OWN entry (same guid) with metadata in `lang`.
    Skips locked titles and split duplicates unless the user insists."""
    picked = _drop_protected(client, picked, "a re-match", args)
    if not picked:
        return False
    clear_screen()
    log_info(f"Looking up the {lang_name} entry for {len(picked)} item(s)…")
    plan = []
    for i, r in enumerate(picked, 1):
        try:
            cands = client.matches(r["rk"], language=lang)
        except Exception:
            cands = []
        same = next((c for c in cands if r.get("guid") and c.get("guid") == r["guid"]), None)
        if same:
            plan.append({"rk": r["rk"], "guid": same["guid"], "name": same["name"],
                         "old": r["title"]})
        print(f"\r  {Fore.CYAN}looking up: {i * 100 // len(picked):3d}%{Style.RESET_ALL} "
              f"({i}/{len(picked)})   ", end="", flush=True)
    print()
    missing = len(picked) - len(plan)
    if missing:
        log_warn(f"{missing} item(s) had no candidate with their own guid and are "
                 f"skipped - re-matching them could point at a different show.")
    if not plan:
        _pause_to_menu()
        return False

    clear_screen()
    log_info(f"Will re-match {len(plan)} item(s) to the SAME entry, in {lang_name}:")
    shown = plan[:30]
    w = min(max([len(p["old"]) for p in shown] or [0]), 60)
    for p in shown:
        old = p["old"] if len(p["old"]) <= w else p["old"][:w - 1] + "…"
        print(f"    {old + ' ' * (w - len(old))}   {Fore.CYAN}->{Style.RESET_ALL}   "
              f"{Fore.GREEN}{p['name']}{Style.RESET_ALL}")
    if len(plan) > 30:
        print(f"    … (+{len(plan) - 30} more)")
    print()
    print(f"{Style.DIM}The guid does not change, so the item keeps its identity - watched "
          f"state, resume positions and the files (with their default tracks) stay. Plex "
          f"re-downloads titles, summary and artwork for that entry.{Style.RESET_ALL}")
    print()
    if not args.yes:
        ans = ask_yes_back("Proceed with the re-match?", default=False)
        if not ans:
            return False
    print()
    ok = fail = 0
    for i, p in enumerate(plan, 1):
        try:
            client.fix_match(p["rk"], p["guid"], p["name"])
            ok += 1
        except Exception as ex:
            fail += 1
            print(f"\r  {Fore.RED}x{Style.RESET_ALL} {p['old']}: {ex}")
        print(f"\r  {Fore.CYAN}applying: {i * 100 // len(plan):3d}%{Style.RESET_ALL} "
              f"({i}/{len(plan)})   ", end="", flush=True)
    print()
    color = Fore.GREEN if not fail else Fore.YELLOW
    print(f"{color}Done: {ok} item(s) re-matched" + (f", {fail} failed" if fail else "")
          + f".{Style.RESET_ALL}")
    log_info("Plex refreshes in the background - give it a moment, then reload the client.")
    return True


def _titles_relanguage(client, sec, picked, args, lang="en", lang_name="English"):
    """Set each item's metadata language to `lang` and refresh it. This is NOT a
    Fix Match - the item stays matched to the same guid, so watched state, resume
    positions and the media/track selections are untouched; only the agent's texts
    (title, episode titles, summary) and artwork are re-pulled in English."""
    picked = _drop_protected(client, picked, "a metadata refresh", args)
    if not picked:
        return False
    clear_screen()
    log_info(f"Will set the metadata language to {lang_name} and refresh {len(picked)} item(s):")
    for r in picked[:30]:
        print(f"    {r['title']}")
    if len(picked) > 30:
        print(f"    … (+{len(picked) - 30} more)")
    print()
    print(f"{Style.DIM}The item keeps its current match (same guid) - no re-identification. "
          f"Watched state, resume positions and default audio/subtitle tracks stay as they "
          f"are. Summary and artwork are re-downloaded in {lang_name}.{Style.RESET_ALL}")
    print()
    if not args.yes:
        ans = ask_yes_back("Proceed?", default=True)
        if not ans:
            return False
    print()
    ok = fail = 0
    for i, r in enumerate(picked, 1):
        try:
            # a locked title would survive the refresh unchanged -> unlock it first
            client.unlock_field(sec["key"], sec["type"], r["rk"], "title")
            client.set_prefs(r["rk"], languageOverride=lang)
            client.refresh_item(r["rk"])
            ok += 1
        except Exception as ex:
            fail += 1
            print(f"\r  {Fore.RED}x{Style.RESET_ALL} {r['title']}: {ex}")
        print(f"\r  {Fore.CYAN}applying: {i * 100 // len(picked):3d}%{Style.RESET_ALL} "
              f"({i}/{len(picked)})   ", end="", flush=True)
    print()
    color = Fore.GREEN if not fail else Fore.YELLOW
    print(f"{color}Done: {ok} item(s) switched to {lang_name} metadata"
          + (f", {fail} failed" if fail else "") + f".{Style.RESET_ALL}")
    log_info("Plex refreshes in the background - give it a moment, then reload the client.")
    return True


def _titles_apply(client, sec, picked, args, lang="en", lang_name="English"):
    """Look up English titles for the picked items, let the user confirm, then write
    only the title field. Returns True if anything was changed."""
    # locked titles are usually a name given by hand ("… - Viki"). Offer to rewrite
    # them anyway while keeping that manual suffix - that is the whole point here.
    # Locked titles are included straight away - their manual " - Viki" suffix is
    # carried over to the new name and the field stays locked. Only split/duplicate
    # entries are held back, because those must keep their own identity.
    prot = _protected_items(client, picked)
    keep_suffix = {str(r["rk"]): title_suffix(r["title"]) for r in picked
                   if "title is locked" in prot.get(str(r["rk"]), [])}
    if any(r.get("dup") for r in picked):
        picked = _drop_protected(client, picked, "a title rewrite", args)
    if not picked:
        return False
    clear_screen()
    log_info(f"Asking Plex for {lang_name} titles ({len(picked)} item(s))…")
    # current sort titles come along, so the preview can show both fields
    try:
        md_now = client.get_metadata_many([r["rk"] for r in picked])
    except Exception:
        md_now = {}
    props = []
    for i, r in enumerate(picked, 1):
        best = _lang_candidate(client, r["rk"], r["title"], guid=r.get("guid"), lang=lang)
        if best:
            sfx = keep_suffix.get(str(r["rk"]), "")
            if sfx and not best["name"].endswith(sfx):
                best["name"] = best["name"] + sfx      # keep the manual "- Viki" marker
            md = md_now.get(str(r["rk"])) or {}
            old_sort = md.get("titleSort") or ""
            # Plex only reports titleSort when it is set explicitly; otherwise it is
            # derived from the title and will follow the new title by itself
            derived = not old_sort
            props.append({"rk": r["rk"], "old": r["title"], "new": best["name"],
                          "old_sort": old_sort, "new_sort": best["name"],
                          "derived_sort": derived,
                          "do_sort": not derived,   # only touch an explicit sort title
                          "exact": best["exact"],
                          "selected": bool(best["exact"])})  # uncertain start unchecked
        print(f"\r  {Fore.CYAN}looking up: {i * 100 // len(picked):3d}%{Style.RESET_ALL} "
              f"({i}/{len(picked)})   ", end="", flush=True)
    print()
    if not props:
        log_warn(f"No {lang_name} title was found for the selected items "
                 f"(the agent may not offer one).")
        _pause_to_menu()
        return False
    skipped = len(picked) - len(props)
    if skipped:
        log_info(f"{skipped} item(s) had no {lang_name} candidate and are left alone.")

    def _col_width():
        """Common width of the 'current value' column, so the arrows line up."""
        vals = ([p["old"] for p in props]
                + [(p["old_sort"] or "(derived from the title)") for p in props])
        return min(max([len(v) for v in vals] or [0]), 60)

    def prop_label(p, w=None):
        """One checkbox per item, with the two fields listed underneath it."""
        w = _col_width() if w is None else w

        def col(s):
            s = s if len(s) <= w else s[:w - 1] + "…"
            return s + " " * (w - len(s))

        def val(field, text):
            """The new value; while this field is being edited the caret is drawn
            right inside the text (reverse video on the character under it)."""
            if p.get("_edit_field") != field:
                return f"{Fore.GREEN}{text}{Style.RESET_ALL}"
            i = max(0, min(int(p.get("_edit_pos") or 0), len(text)))
            here = text[i] if i < len(text) else " "
            return (f"{Fore.GREEN}{text[:i]}\x1b[7m{here}\x1b[27m{text[i + 1:]}"
                    f"{Style.RESET_ALL}")

        warn = "" if p["exact"] else f"   {Fore.YELLOW}[different match - check!]{Style.RESET_ALL}"
        p["label"] = f"{Style.BRIGHT}{p['old']}{Style.RESET_ALL}{warn}"
        arrow = f"{Fore.CYAN}->{Style.RESET_ALL}"
        sub = [f"{Style.DIM}title:{Style.RESET_ALL} {col(p['old'])}   {arrow}   "
               f"{val('new', p['new'])}"]
        if p["do_sort"]:
            sub.append(f"{Style.DIM}sort :{Style.RESET_ALL} "
                       f"{col(p['old_sort'] or '(none)')}   {arrow}   "
                       f"{val('new_sort', p['new_sort'])}")
        elif p.get("derived_sort"):
            sub.append(f"{Style.DIM}sort : {col('(derived from the title)')}   "
                       f"follows the new title by itself{Style.RESET_ALL}")
        else:
            sub.append(f"{Style.DIM}sort : {col(p['old_sort'] or '(none)')}   "
                       f"(kept unchanged){Style.RESET_ALL}")
        p["sub"] = sub

    def relabel_all():
        w = _col_width()
        for p in props:
            prop_label(p, w)

    relabel_all()

    n_exact = sum(1 for p in props if p["exact"])
    header = [
        f"{Style.DIM}Only title (and sort title, where shown) is written and locked. "
        f"No re-match - streams, subtitles, watched state and artwork stay untouched."
        f"{Style.RESET_ALL}",
        f"{Style.DIM}{n_exact}/{len(props)} come from the item's own match (same guid); "
        f"the rest are pre-unchecked. 'e' edits the row in place, 'f' searches for "
        f"the right entry online.{Style.RESET_ALL}",
        "",
    ]
    def find_match(p):
        """'f' - ask the agent again and pick the right entry by hand."""
        term = p["old"]
        while True:
            clear_screen()
            log_info(f"Searching {lang_name} matches for: {Fore.CYAN}{term}{Style.RESET_ALL}")
            try:
                cands = client.matches(p["rk"], language=lang,
                                       title=None if term == p["old"] else term)
            except Exception as ex:
                log_warn(f"Search failed: {ex}")
                cands = []
            labels = []
            for c in cands:
                yr = f" ({c['year']})" if c.get("year") else ""
                same = (f"  {Fore.GREEN}[this item's own match]{Style.RESET_ALL}"
                        if p.get("guid") and c.get("guid") == p.get("guid") else "")
                labels.append(f"{c['name']}{yr}   {Style.DIM}score {c['score']}"
                              f"{Style.RESET_ALL}{same}")
            labels.append(f"{Fore.CYAN}» Search for a different title…{Style.RESET_ALL}")
            if not cands:
                log_warn("Nothing found.")
            i = interactive_menu("Pick the right entry:", labels, allow_cancel=True,
                                 header=[f"{Style.DIM}Only the title text is taken from "
                                         f"the pick - no re-match happens."
                                         f"{Style.RESET_ALL}", ""])
            if i is None:
                return
            if i == len(cands):
                nt = ask_line("Search title", default=term)
                if not nt:
                    return
                term = nt
                continue
            c = cands[i]
            p["new"] = c["name"]
            if p["do_sort"]:
                p["new_sort"] = c["name"]
            p["exact"] = True          # picked by hand -> no warning any more
            p["selected"] = True
            relabel_all()
            return

    def after_edit(r):
        # touching the sort title by hand means the user wants it written
        if r.get("_edit_field") == "new_sort":
            r["do_sort"] = True
            r["derived_sort"] = False
        relabel_all()

    res = checkbox_menu("Confirm the new titles:", props, header=header,
                        editable=[("new", "title"), ("new_sort", "sort title")],
                        on_edit=after_edit, find_cb=find_match)
    if res is None:
        return False
    todo = [p for p in props if p["selected"]]
    if not todo:
        log_info("Nothing selected.")
        return False

    clear_screen()
    log_info(f"Will rename {len(todo)} title(s):")
    shown = todo[:30]
    olds = [p["old"] for p in shown] + [(p["old_sort"] or "(none)")
                                        for p in shown if p["do_sort"]]
    w = min(max([len(s) for s in olds] or [0]), 60)

    def col(s):
        s = s if len(s) <= w else s[:w - 1] + "…"
        return s + " " * (w - len(s))

    for p in shown:
        print(f"    title: {col(p['old'])}   {Fore.CYAN}->{Style.RESET_ALL}   "
              f"{Fore.GREEN}{p['new']}{Style.RESET_ALL}")
        if p["do_sort"]:
            print(f"    {Style.DIM}sort :{Style.RESET_ALL} {col(p['old_sort'] or '(none)')}"
                  f"   {Fore.CYAN}->{Style.RESET_ALL}   {Fore.GREEN}{p['new_sort']}"
                  f"{Style.RESET_ALL}")
        elif p.get("derived_sort"):
            print(f"    {Style.DIM}sort : {col('(derived from the title)')}   "
                  f"follows the new title by itself{Style.RESET_ALL}")
        else:
            print(f"    {Style.DIM}sort : {col(p['old_sort'] or '(none)')}   "
                  f"(kept unchanged){Style.RESET_ALL}")
    if len(todo) > 30:
        print(f"    … (+{len(todo) - 30} more)")
    print()
    if not args.yes:
        ans = ask_yes_back("Apply these titles?", default=True)
        if not ans:
            return False
    print()
    ok = fail = 0
    for i, p in enumerate(todo, 1):
        try:
            client.set_title(sec["key"], sec["type"], p["rk"], p["new"],
                             sort_title=p["new_sort"] if p["do_sort"] else None)
            ok += 1
        except Exception as ex:
            fail += 1
            print(f"\r  {Fore.RED}x{Style.RESET_ALL} {p['old']}: {ex}")
        print(f"\r  {Fore.CYAN}applying: {i * 100 // len(todo):3d}%{Style.RESET_ALL} "
              f"({i}/{len(todo)})   ", end="", flush=True)
    print()
    color = Fore.GREEN if not fail else Fore.YELLOW
    print(f"{color}Done: {ok} title(s) changed" + (f", {fail} failed" if fail else "")
          + f".{Style.RESET_ALL}")
    log_info("The change shows in the client after a refresh.")
    return True


# ---------------------------------------------------------------------------
# Top-level menu (hub) + flows
# ---------------------------------------------------------------------------
def langsubs_flow(client, args):
    """Set the default audio/subtitle tracks. Returns True if a change was made.
    Esc on the final confirm goes back to the subtitle step (keeping selections)."""
    resume = None
    confirm_default = True
    dry = args.dry_run
    while True:  # wizard loop (re-entered on Esc from the confirm)
        result = select_and_configure(client, args, resume=resume)
        if result is None:
            log_info("Cancelled.")
            return False
        items_data, audio_action, sub_action, state = result

        interactive = not args.yes
        sec = state.get("sec") if state else None
        section_key = sec["key"] if sec else None
        print()
        audio_plan = resolve_coverage("audio", audio_action, items_data, ST_AUDIO,
                                      allow_off=False, interactive=interactive,
                                      client=client, section_key=section_key)
        sub_plan = resolve_coverage("subtitles", sub_action, items_data, ST_SUBTITLE,
                                    allow_off=True, interactive=interactive,
                                    client=client, section_key=section_key)

        if all(p[0] == "skip" for p in audio_plan) and all(p[0] == "skip" for p in sub_plan):
            log_warn("Nothing to change after all.")
            return False

        clear_screen()
        log_info(f"Will set on {len(items_data)} items:")
        print(f"    audio:   {plan_summary(audio_plan)}")
        print(f"    subtitles: {plan_summary(sub_plan)}")
        print()

        if args.dry_run or args.yes:
            break  # no confirmation needed

        back_to_wizard = False
        while True:  # confirm loop (Esc steps back one question)
            ans = ask_yes_back("Apply the changes?", default=confirm_default)
            if ans is None:  # Esc -> back to the subtitle step
                back_to_wizard = True
                break
            confirm_default = ans
            if ans:
                dry = False
                break
            d2 = ask_yes_back("At least show the plan (dry-run)?", default=True)
            if d2 is None:  # Esc -> back to the "Apply?" question
                continue
            if not d2:
                log_info("Cancelled.")
                return False
            dry = True
            break

        if back_to_wizard:
            if state.get("can_resume"):
                resume = state
                continue  # re-enter the wizard at the subtitle step
            log_info("Cancelled.")
            return False
        break  # confirmed (real or dry-run)

    print()
    ok, fail, changed = apply_changes(client, items_data, audio_plan, sub_plan, dry)
    print()
    if dry:
        log_info(f"Dry-run: {changed} edits planned (nothing saved).")
    else:
        color = Fore.GREEN if fail == 0 else Fore.YELLOW
        print(f"{color}Done: {ok} succeeded"
              + (f", {fail} failed" if fail else "")
              + f" ({changed} parts affected).{Style.RESET_ALL}")
        log_info("The change shows in the client after a refresh / new playback.")
    return True


# ---------------------------------------------------------------------------
# Watchlist flow (manage the account/user watchlist, using this server's content)
# ---------------------------------------------------------------------------
# The watchlist is an account-level Plex Discover feature, not a per-server list.
# This tool deliberately works only with the movies and shows that live on THIS
# server: to add or edit you browse a library (movies / shows, same as the other
# tools), and each item is mapped to its Plex online entry via its plex:// guid.

# --- a single-select browser (arrow keys, type-to-search, Enter opens, Esc back) ---
def _browse_order(rows, plain, filt):
    """Visible row indices for a browse_menu given the search text. Section header
    rows (row['header']) are kept only when at least one of their children matches."""
    n = len(rows)
    if not filt:
        return list(range(n))
    mset = {i for i in range(n)
            if not rows[i].get("header") and _match_filter(plain[i], filt)}
    kids, cur = {}, None
    for i in range(n):
        if rows[i].get("header"):
            cur = i
            kids[cur] = []
        elif cur is not None:
            kids[cur].append(i)
    keep = {h for h, ks in kids.items() if any(k in mset for k in ks)}
    return [i for i in range(n) if (i in mset) or (rows[i].get("header") and i in keep)]


def browse_menu(prompt, rows, header=None, default=0, cursor_out=None):
    """Single-select browser used by the watchlist views. Arrow keys move, typing
    filters (search by just typing), Enter opens the highlighted row, Esc clears the
    search or steps back. Rows are dicts: 'label' (required); optional 'tag' (a coloured status
    shown in an aligned column) and 'header' (a non-selectable section title). Returns
    the chosen row index, or None on Esc. The final cursor row index is written to
    cursor_out[0] (if given) so the caller can restore it next time."""
    n = len(rows)
    plain = [strip_ansi(r["label"]) for r in rows]
    sel_rows = [i for i in range(n) if not rows[i].get("header")]

    if not _tui_supported():
        for i, r in enumerate(rows):
            if r.get("header"):
                print(strip_ansi(r["label"]))
            else:
                num = sel_rows.index(i) + 1
                tag = f"   {strip_ansi(r.get('tag') or '')}" if r.get("tag") else ""
                print(f"  {num:>3}) {strip_ansi(r['label'])}{tag}")
        while True:
            raw = input("Number = open · Enter/q = back: ").strip().lower()
            if raw in ("", "q"):
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(sel_rows):
                idx = sel_rows[int(raw) - 1]
                if cursor_out is not None:
                    cursor_out[:] = [idx]
                return idx

    header = list(header or [])
    filt = ""
    prev_lines = 0
    first = True

    def term_size():
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def trunc(s, w):
        return s[:max(1, w - 1)] + "…" if len(s) > w else s

    def land_on(order, want):
        """Nearest selectable position in `order` at/after the row index `want`."""
        if not order:
            return 0
        non_h = [p for p, i in enumerate(order) if not rows[i].get("header")]
        if not non_h:
            return 0
        for p, i in enumerate(order):
            if i == want and not rows[i].get("header"):
                return p
        return min(non_h, key=lambda p: abs(order[p] - want))

    def step(order, pos, d):
        if not order:
            return pos
        j = pos
        for _ in range(len(order)):
            j = (j + d) % len(order)
            if not rows[order[j]].get("header"):
                return j
        return pos

    def render(order, sel_pos):
        nonlocal prev_lines, first
        cols, rows_total = term_size()
        maxw = max(10, cols - 2)
        page_rows = max(3, rows_total - (5 + len(header)))
        buf = []
        if first:
            buf.append("\x1b[2J\x1b[H")
            first = False
        elif prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")
        vis = []
        for h in header:
            sp = strip_ansi(h)
            vis.append(h if len(sp) <= maxw else trunc(sp, maxw))
        vis.append(f"{Fore.YELLOW}{trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        hint = ("↑↓ move · Enter = open · Esc = clear search" if filt
                else "↑↓ move · type = search · Enter = open · Esc = back")
        vis.append(f"{Fore.CYAN}{trunc(hint, maxw)}{Style.RESET_ALL}")

        if not order:
            vis.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
            # align the tag column next to the longest visible title that has a tag
            statw = max((len(strip_ansi(rows[i].get("tag") or "")) for i in order
                         if not rows[i].get("header")), default=0)
            longest = max((len(plain[i]) for i in order
                           if not rows[i].get("header") and rows[i].get("tag")), default=0)
            gap = 3
            textw = max(6, min(maxw - 6 - (gap + statw), longest + 1)) if statw else maxw - 6

            def fit(s, w):
                return (s[:max(1, w - 1)] + "…") if len(s) > w else s + " " * (w - len(s))

            start = max(0, min(sel_pos - page_rows // 2, len(order) - page_rows))
            window = order[start:start + page_rows]
            sbr = _scrollbar_range(len(window), len(order), len(window), start)
            if start > 0:
                vis.append(f"  {Fore.CYAN}▲ ({start} above){Style.RESET_ALL}")
            for pos, i in enumerate(window, start):
                r = rows[i]
                if r.get("header"):
                    txt = trunc(plain[i], maxw - 2)
                    line = f"  {Fore.CYAN}{Style.BRIGHT}{txt}{Style.RESET_ALL}"
                    vis.append(_with_scrollbar(line, cols, pos - start, sbr))
                    continue
                tag = r.get("tag")
                if tag:
                    body = fit(plain[i], textw)
                    tail = f"{' ' * gap}{tag}"
                else:
                    body = trunc(plain[i], maxw - 4)
                    tail = ""
                if pos == sel_pos:
                    line = (f"{Fore.GREEN}{Style.BRIGHT}› {body}"
                            f"{strip_ansi(tail)}{Style.RESET_ALL}")
                else:
                    line = f"  {body}{tail}"
                vis.append(_with_scrollbar(line, cols, pos - start, sbr))
            rest = len(order) - (start + len(window))
            if rest > 0:
                vis.append(f"  {Fore.CYAN}▼ ({rest} below){Style.RESET_ALL}")

        sel_count = sum(1 for i in order if not rows[i].get("header"))
        cur_no = sum(1 for p, i in enumerate(order)
                     if p <= sel_pos and not rows[i].get("header"))
        info = f" [{cur_no}/{sel_count}]" if sel_count else ""
        if filt:
            vis.append(f"{Fore.MAGENTA}{trunc('Search: ' + filt + info, maxw)}{Style.RESET_ALL}")
        else:
            vis.append(f"{Fore.CYAN}{trunc('(type to search)' + info, maxw)}{Style.RESET_ALL}")
        buf.append("\r\n".join(vis))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)
        return page_rows

    with _RawMode():
        order = _browse_order(rows, plain, filt)
        sel_pos = land_on(order, default if 0 <= default < n else 0)
        page_rows = render(order, sel_pos)
        while True:
            key = _read_key()
            if key == "up":
                sel_pos = step(order, sel_pos, -1)
            elif key == "down":
                sel_pos = step(order, sel_pos, 1)
            elif key == "pgup":
                sel_pos = max(0, sel_pos - page_rows)
                if order and rows[order[sel_pos]].get("header"):
                    sel_pos = step(order, sel_pos, 1)
            elif key == "pgdn":
                sel_pos = min(len(order) - 1, sel_pos + page_rows) if order else 0
                if order and rows[order[sel_pos]].get("header"):
                    sel_pos = step(order, sel_pos, -1)
            elif key == "home":
                sel_pos = step(order, -1, 1) if order else 0
            elif key == "end":
                sel_pos = step(order, 0, -1) if order else 0
            elif key in ("enter", "right"):
                if order and not rows[order[sel_pos]].get("header"):
                    idx = order[sel_pos]
                    if cursor_out is not None:
                        cursor_out[:] = [idx]
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return idx
            elif key in ("esc", "left"):
                if filt:
                    filt = ""
                    order = _browse_order(rows, plain, filt)
                    sel_pos = land_on(order, order[sel_pos] if order else 0)
                else:
                    if cursor_out is not None and order:
                        cursor_out[:] = [order[sel_pos]]
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return None
            elif key == "backspace":
                if filt:
                    filt = filt[:-1]
                    order = _browse_order(rows, plain, filt)
                    sel_pos = land_on(order, 0)
            elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                filt += key[1]
                order = _browse_order(rows, plain, filt)
                sel_pos = land_on(order, 0)
            page_rows = render(order, sel_pos)


# --- detail cards ----------------------------------------------------------
def _fmt_duration(ms):
    try:
        secs = int(ms) // 1000
    except Exception:
        return None
    h, m = secs // 3600, (secs % 3600) // 60
    return (f"{h}h {m}m" if h else f"{m}m")


def _wrap_lines(text, indent="  "):
    import textwrap
    try:
        width = max(40, os.get_terminal_size().columns - 2)
    except Exception:
        width = 100
    out = []
    for para in str(text).split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue
        out += [f"{indent}{ln}" for ln in textwrap.wrap(para, width=width - len(indent))]
    return out


def _rating_bits(md):
    """Reviewer scores from a metadata dict as short strings, e.g.
    ['IMDb 7.7', 'RT 100%', 'RT Audience 94%', 'TMDB 82%'] - just like the Plex web
    detail page. Reads the Rating[] array when present, else the flat rating /
    audienceRating fields. IMDb/TMDB-style /10 values stay as x.x; Rotten Tomatoes and
    TMDB are shown as percentages (value*10)."""
    def source(image):
        im = (image or "").lower()
        if "imdb" in im:
            return ("IMDb", "ten")
        if "rottentomatoes" in im:
            return (("RT Audience" if ("upright" in im or "spilled" in im) else "RT"), "pct")
        if "themoviedb" in im or "tmdb" in im:
            return ("TMDB", "pct")
        return (None, "ten")

    def fmt(value, kind):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if kind == "pct":
            return f"{round(v if v > 10 else v * 10)}%"
        return f"{v:.1f}"

    pairs = []
    ratings = md.get("Rating")
    if isinstance(ratings, list) and ratings:
        for r in ratings:
            pairs.append((r.get("value"), r.get("image")))
    else:
        pairs.append((md.get("rating"), md.get("ratingImage")))
        pairs.append((md.get("audienceRating"), md.get("audienceRatingImage")))
    bits, seen = [], set()
    for value, image in pairs:
        if value is None:
            continue
        label, kind = source(image)
        val = fmt(value, kind)
        if val is None:
            continue
        entry = f"{label} {val}" if label else f"\u2605 {val}"
        if entry not in seen:
            seen.add(entry)
            bits.append(entry)
    return bits


def _ratings_line(md, indent="  "):
    bits = _rating_bits(md)
    return f"{indent}{Fore.YELLOW}" + "   ·   ".join(bits) + Style.RESET_ALL if bits else None


def _cast_names(md, n=6):
    """Comma-separated top cast names ('A, B, C +4 more'), or None."""
    names = [r.get("tag") for r in (md.get("Role") or []) if r.get("tag")]
    if not names:
        return None
    extra = len(names) - n
    return ", ".join(names[:n]) + (f"  +{extra} more" if extra > 0 else "")


def _episode_rating_tag(e):
    """Compact star rating for an episode row ('\u2b509.1'), or '' when unrated."""
    try:
        return f"\u2b50{float(e.get('rating')):.1f}"
    except (TypeError, ValueError):
        return ""


def _meta_line(md):
    bits = []
    if md.get("contentRating"):
        bits.append(str(md["contentRating"]))
    dur = _fmt_duration(md.get("duration"))
    if dur:
        bits.append(dur)
    genres = ", ".join(g.get("tag") for g in (md.get("Genre") or []) if g.get("tag"))
    if genres:
        bits.append(genres)
    return f"  {Style.DIM}" + "   ·   ".join(bits) + Style.RESET_ALL if bits else None


def _title_card_head(md, fallback_title, on_server):
    title = md.get("title") or fallback_title or "?"
    year = md.get("year")
    head = f"{Fore.MAGENTA}{Style.BRIGHT}{title}{Style.RESET_ALL}" + (f"  ({year})" if year else "")
    where = (f"{Fore.GREEN}on this server{Style.RESET_ALL}" if on_server
             else f"{Style.DIM}not on this server{Style.RESET_ALL}")
    return [head, f"  {where}", ""]


def _movie_card(md, fallback_title, on_server):
    lines = _title_card_head(md, fallback_title, on_server)
    rl = _ratings_line(md)
    if rl:
        lines += [rl]
    ml = _meta_line(md)
    if ml:
        lines += [ml]
    lines += [""]
    if md.get("tagline"):
        lines += [f"  {Style.DIM}{md['tagline']}{Style.RESET_ALL}", ""]
    if md.get("summary"):
        lines += _wrap_lines(md["summary"])
    else:
        lines += [f"  {Style.DIM}(no plot summary){Style.RESET_ALL}"]
    directors = ", ".join(d.get("tag") for d in (md.get("Director") or []) if d.get("tag"))
    if directors:
        lines += ["", f"  {Fore.CYAN}Director:{Style.RESET_ALL} {directors}"]
    cast = _cast_names(md)
    if cast:
        lines += [f"  {Fore.CYAN}Cast:{Style.RESET_ALL} {cast}"]
    return lines


def _show_card(md, fallback_title, on_server):
    lines = _title_card_head(md, fallback_title, on_server)
    rl = _ratings_line(md)
    if rl:
        lines += [rl]
    ml = _meta_line(md)
    if ml:
        lines += [ml]
    lines += [""]
    lc = md.get("leafCount") or md.get("childCount")
    if lc:
        kind = "episode" if md.get("leafCount") else "season"
        lines += [f"  {Fore.CYAN}{lc}{Style.RESET_ALL} {kind}(s)", ""]
    if md.get("summary"):
        lines += _wrap_lines(md["summary"])
    else:
        lines += [f"  {Style.DIM}(no plot summary){Style.RESET_ALL}"]
    cast = _cast_names(md)
    if cast:
        lines += ["", f"  {Fore.CYAN}Cast:{Style.RESET_ALL} {cast}"]
    return lines


def _episode_card(md):
    s, ep = md.get("parentIndex"), md.get("index")
    try:
        se = f"S{int(s):02d}E{int(ep):02d}"
    except Exception:
        se = ""
    title = md.get("title") or ""
    head = f"{Fore.MAGENTA}{Style.BRIGHT}{se}{Style.RESET_ALL}  {title}".strip()
    lines = [head]
    sub = []
    if md.get("grandparentTitle"):
        sub.append(md["grandparentTitle"])
    if md.get("originallyAvailableAt"):
        sub.append(str(md["originallyAvailableAt"]))
    dur = _fmt_duration(md.get("duration"))
    if dur:
        sub.append(dur)
    watched = (md.get("viewCount") or 0) > 0
    sub.append("watched" if watched else "unwatched")
    lines += [f"  {Style.DIM}" + "   ·   ".join(sub) + Style.RESET_ALL]
    rl = _ratings_line(md)
    if rl:
        lines += [rl]
    lines += [""]
    if md.get("summary"):
        lines += _wrap_lines(md["summary"])
    else:
        lines += [f"  {Style.DIM}(no plot summary){Style.RESET_ALL}"]
    credits = []
    directors = ", ".join(d.get("tag") for d in (md.get("Director") or []) if d.get("tag"))
    if directors:
        credits.append(f"  {Fore.CYAN}Director:{Style.RESET_ALL} {directors}")
    writers = ", ".join(w.get("tag") for w in (md.get("Writer") or []) if w.get("tag"))
    if writers:
        credits.append(f"  {Fore.CYAN}Writer:{Style.RESET_ALL} {writers}")
    cast = _cast_names(md)
    if cast:
        credits.append(f"  {Fore.CYAN}Cast:{Style.RESET_ALL} {cast}")
    if credits:
        lines += [""] + credits
    return lines


def _detail_wait():
    msg = f"\n{Style.DIM}Esc / Enter = back{Style.RESET_ALL}"
    if not _tui_supported():
        try:
            input(strip_ansi(msg))
        except EOFError:
            pass
        return
    print(msg)
    with _RawMode():
        while True:
            if _read_key() in ("esc", "enter", "left"):
                return


def _detail_screen(lines):
    clear_screen()
    for ln in lines:
        print(ln)
    _detail_wait()


# persistent cursors so re-opening a view lands where you left it, not on the default
_WL_STATE = {}


def discover_key_from_guid(guid):
    """'plex://movie/5d776…' -> '5d776…' (the Discover ratingKey the watchlist
    actions use). Returns None for old-agent guids (com.plexapp.agents.…), i.e.
    items that have no direct Plex online match to watchlist."""
    g = str(guid or "")
    if not g.startswith("plex://"):
        return None
    return g.rsplit("/", 1)[-1] or None


def _ensure_guids(client, rows):
    """Make sure every row has its 'guid'. The library listing normally carries it;
    any that are missing are back-filled with a single batched metadata read."""
    missing = [r for r in rows if not r.get("guid")]
    if not missing:
        return
    md_map = client.get_metadata_many([r["rk"] for r in missing])
    for r in missing:
        md = md_map.get(str(r["rk"])) or {}
        r["guid"] = md.get("guid")


def _server_discover_index(client):
    """Map Discover ratingKey -> {'title','type','section'} for every movie/show on
    this server that has a plex:// guid (one request per library). Lets the watchlist
    views show which titles actually live on the server."""
    idx = {}
    for s in client.sections():
        if s["type"] not in ("movie", "show"):
            continue
        try:
            items = client.items_in_section(s["key"], s["type"])
        except Exception:
            continue
        for it in items:
            dk = discover_key_from_guid(it.get("guid"))
            if dk:
                idx[dk] = {"rk": it["ratingKey"], "title": it["title"],
                           "year": it.get("year"), "type": it["type"],
                           "section": s["title"]}
    return idx


def _watchlist_error(ex):
    """Explain a failed watchlist call in the same tone as the rest of the tool."""
    if "UNAUTHORIZED" in str(ex).upper():
        log_warn("Plex would not authorise the watchlist for the current user.")
        print(f"    {Style.DIM}The watchlist belongs to the signed-in Plex account. A managed / "
              f"restricted Home user may not have one — try --switch-user to the main account."
              f"{Style.RESET_ALL}")
    else:
        log_warn(f"Could not reach the Plex watchlist: {ex}")
        print(f"    {Style.DIM}This uses Plex Discover (discover.provider.plex.tv) and needs "
              f"internet access to plex.tv.{Style.RESET_ALL}")


def _apply_watchlist(client, targets):
    """targets: list of (discover_rating_key, title, 'add'|'remove'). Applies each,
    shows progress like the other tools, and returns True if anything succeeded."""
    ok = fail = 0
    total = len(targets)
    for i, (dk, title, action) in enumerate(targets, 1):
        try:
            (client.add_to_watchlist if action == "add" else client.remove_from_watchlist)(dk)
            ok += 1
        except Exception as ex:
            fail += 1
            print(f"\r  {Fore.RED}x{Style.RESET_ALL} {title}: {ex}")
        if total > 1:
            print(f"\r  {Fore.CYAN}applying: {i * 100 // total:3d}%{Style.RESET_ALL} "
                  f"({i}/{total})   ", end="", flush=True)
    if total > 1:
        print()
    color = Fore.GREEN if not fail else Fore.YELLOW
    print(f"{color}Done: {ok} updated" + (f", {fail} failed" if fail else "") + f".{Style.RESET_ALL}")
    log_info("Watchlist changes may take a moment to appear in the Plex apps.")
    return ok > 0


def _watchlist_show(client, args):
    """Browse the current watchlist (Movies / Shows), aligned status column, type-to-
    search and '/'. Enter opens details: a movie/show card, and for shows the seasons →
    episodes → episode plot. Esc steps back, remembering the row you came from. Read-
    only, so it always returns False."""
    clear_screen()
    log_info("Loading your Plex watchlist…")
    try:
        wl = client.watchlist()
    except RuntimeError as ex:
        clear_screen()
        _watchlist_error(ex)
        _pause_to_menu()
        return False
    if not wl:
        clear_screen()
        log_info("Your watchlist is empty.")
        _pause_to_menu()
        return False
    try:
        srv = _server_discover_index(client)
    except Exception:
        srv = {}
    on_srv = sum(1 for w in wl if w["ratingKey"] in srv)
    movies = sorted((w for w in wl if w.get("type") == "movie"),
                    key=lambda w: (w.get("title") or "").lower())
    shows = sorted((w for w in wl if w.get("type") == "show"),
                   key=lambda w: (w.get("title") or "").lower())
    other = sorted((w for w in wl if w.get("type") not in ("movie", "show")),
                   key=lambda w: (w.get("title") or "").lower())

    rows = []

    def _add_group(title, group):
        if not group:
            return
        rows.append({"header": True, "label": f"{title}  ({len(group)})"})
        for w in group:
            hit = srv.get(w["ratingKey"])
            tag = (f"{Fore.GREEN}on server{Style.RESET_ALL}" if hit
                   else f"{Style.DIM}not on server{Style.RESET_ALL}")
            yr = f" ({w['year']})" if w.get("year") else ""
            rows.append({"label": f"{(w.get('title') or '?')}{yr}", "tag": tag,
                         "type": w.get("type"), "dk": w["ratingKey"],
                         "title": w.get("title"), "year": w.get("year"),
                         "srv_rk": (hit or {}).get("rk")})

    _add_group("Movies", movies)
    _add_group("Shows", shows)
    _add_group("Other", other)

    head = [
        f"{Fore.MAGENTA}{Style.BRIGHT}Your watchlist{Style.RESET_ALL}  "
        f"({len(wl)} title(s) · {Fore.GREEN}{on_srv} on this server{Style.RESET_ALL} · "
        f"{Style.DIM}{len(wl) - on_srv} elsewhere{Style.RESET_ALL})",
        f"{Style.DIM}type = search · Enter = details / episodes · Esc = back"
        f"{Style.RESET_ALL}",
        "",
    ]
    cur = _WL_STATE.setdefault("show_cursor", [0])
    while True:
        idx = browse_menu("Watchlist — open a title:", rows, header=head,
                          default=cur[0], cursor_out=cur)
        if idx is None:
            return False
        row = rows[idx]
        if row.get("type") == "show":
            _wl_open_show(client, row)
        else:
            _wl_open_movie(client, row)


def _wl_open_movie(client, row):
    """Open a movie: a little menu with its plot/details card and its cast."""
    on_server = bool(row.get("srv_rk"))
    md = {}
    if on_server:
        try:
            md = client.get_metadata(row["srv_rk"]) or {}
        except Exception:
            md = {}
    if not md:
        md = client.discover_metadata(row.get("dk")) or {}
    if not md:
        md = {"title": row.get("title"), "year": row.get("year")}
    _wl_title_menu(client, md, row.get("title"), on_server, is_show=False, show_rk=None)


def _wl_open_show(client, row):
    """Open a show: a menu with plot/details, cast, and the seasons (→ episodes →
    episode plot). Off-server shows use Plex Discover metadata (no season tree)."""
    rk = row.get("srv_rk")
    title = row.get("title") or "?"
    if not rk:
        md = client.discover_metadata(row.get("dk")) or {"title": title, "year": row.get("year")}
        _wl_title_menu(client, md, title, on_server=False, is_show=True, show_rk=None)
        return
    clear_screen()
    log_info(f"Loading '{title}'…")
    try:
        md = client.get_metadata(rk) or {}
    except Exception:
        md = {}
    if not md:
        md = {"title": title, "year": row.get("year"), "ratingKey": rk}
    _wl_title_menu(client, md, title, on_server=True, is_show=True, show_rk=rk)


def _wl_title_menu(client, md, fallback_title, on_server, is_show, show_rk):
    """Shared movie/show screen: ratings in the header, then rows for the plot/details
    card, the cast, and (for shows) each season. Enter opens the highlighted row."""
    title = md.get("title") or fallback_title or "?"
    rbits = _rating_bits(md)
    rows = [{"kind": "info", "label": "Plot & details"}]
    ncast = len([r for r in (md.get("Role") or []) if r.get("tag")])
    if ncast:
        rows.append({"kind": "cast", "label": f"Cast & crew ({ncast})"})
    if is_show and show_rk:
        try:
            seasons = [s for s in client.children(show_rk)
                       if s.get("type") == "season" or s.get("leafCount")]
        except Exception:
            seasons = []
        seasons.sort(key=lambda s: (s.get("index") if s.get("index") is not None else 9999))
        if seasons:
            rows.append({"header": True, "label": "Seasons"})
            for s in seasons:
                lc, vc = s.get("leafCount") or 0, s.get("viewedLeafCount") or 0
                tag = f"{Fore.GREEN}{vc}/{lc} watched{Style.RESET_ALL}" if lc else ""
                rows.append({"kind": "season", "tag": tag, "rk": s.get("ratingKey"),
                             "label": s.get("title") or f"Season {s.get('index')}",
                             "slabel": s.get("title") or f"Season {s.get('index')}"})
        else:   # a show that lists episodes directly, no season folders
            rows.append({"kind": "episodes", "label": "Episodes"})

    kind_word = "Show" if is_show else "Movie"
    cur = _WL_STATE.setdefault(f"title:{md.get('ratingKey') or title}", [0])
    while True:
        htitle = (f"{Fore.MAGENTA}{Style.BRIGHT}{title}{Style.RESET_ALL}"
                  + (f"  ({md['year']})" if md.get("year") else ""))
        head = [htitle]
        if rbits:
            head.append(f"  {Fore.YELLOW}" + "   ·   ".join(rbits) + Style.RESET_ALL)
        head += [f"{Style.DIM}type = search · Enter = open · Esc = back{Style.RESET_ALL}", ""]
        i = browse_menu(f"{kind_word}:", rows, header=head, default=cur[0], cursor_out=cur)
        if i is None:
            return
        r = rows[i]
        k = r.get("kind")
        if k == "info":
            _detail_screen(_show_card(md, title, on_server) if is_show
                           else _movie_card(md, title, on_server))
        elif k == "cast":
            _wl_cast_browser(client, md, title)
        elif k == "season":
            _wl_browse_episodes(client, r["rk"], title, r["slabel"])
        elif k == "episodes":
            _wl_browse_episodes(client, show_rk, title, None)


def _wl_cast_browser(client, md, title):
    """List the cast; Enter on a person finds the other titles they appear in on this
    server (which then open just like any other title)."""
    roles = [r for r in (md.get("Role") or []) if r.get("tag")]
    if not roles:
        _detail_screen([f"{Style.DIM}No cast information for this title.{Style.RESET_ALL}"])
        return
    rows = []
    for r in roles:
        char = r.get("role")
        rows.append({"label": r.get("tag"), "actor": r.get("tag"), "actor_id": r.get("id"),
                     "tag": (f"{Style.DIM}as {char}{Style.RESET_ALL}" if char else "")})
    cur = _WL_STATE.setdefault(f"cast:{md.get('ratingKey') or title}", [0])
    while True:
        head = [
            f"{Fore.CYAN}Cast & crew:{Style.RESET_ALL} {title}   ({len(rows)} people)",
            f"{Style.DIM}type = search · Enter = other titles with this person · Esc = back"
            f"{Style.RESET_ALL}",
            "",
        ]
        i = browse_menu("Cast & crew:", rows, header=head, default=cur[0], cursor_out=cur)
        if i is None:
            return
        _wl_actor_titles(client, rows[i]["actor"], rows[i]["actor_id"])


def _wl_actor_titles(client, actor_name, actor_id):
    """Every movie/show on this server featuring the given person; Enter opens a title."""
    clear_screen()
    log_info(f"Finding titles with {actor_name} on this server…")
    found, seen = [], set()
    if actor_id is not None:
        for s in client.sections():
            if s["type"] not in ("movie", "show"):
                continue
            try:
                items = client.items_by_filter(s["key"], s["type"], actor=actor_id)
            except Exception:
                items = []
            for it in items:
                if it["rk"] in seen:
                    continue
                seen.add(it["rk"])
                found.append(it)
    if not found:
        _detail_screen([f"{Fore.MAGENTA}{Style.BRIGHT}{actor_name}{Style.RESET_ALL}", "",
                        f"  {Style.DIM}No titles with this person found on this server."
                        f"{Style.RESET_ALL}"])
        return
    found.sort(key=lambda x: ({"movie": 0, "show": 1}.get(x.get("type"), 2),
                              (x.get("title") or "").lower()))
    rows = []
    for it in found:
        yr = f" ({it['year']})" if it.get("year") else ""
        rows.append({"label": f"{it['title']}{yr}",
                     "tag": f"{Fore.CYAN}{it.get('type', '')}{Style.RESET_ALL}",
                     "srv_rk": it["rk"], "type": it.get("type"), "title": it.get("title"),
                     "year": it.get("year"), "dk": None})
    cur = _WL_STATE.setdefault(f"actor:{actor_id or actor_name}", [0])
    while True:
        head = [
            f"{Fore.MAGENTA}{Style.BRIGHT}{actor_name}{Style.RESET_ALL}   "
            f"({len(rows)} title(s) on this server)",
            f"{Style.DIM}type = search · Enter = open title · Esc = back{Style.RESET_ALL}",
            "",
        ]
        i = browse_menu("Titles:", rows, header=head, default=cur[0], cursor_out=cur)
        if i is None:
            return
        r = rows[i]
        if r.get("type") == "show":
            _wl_open_show(client, r)
        else:
            _wl_open_movie(client, r)


def _wl_browse_episodes(client, season_rk, show_title, season_label):
    clear_screen()
    log_info("Loading episodes…")
    try:
        eps = client.children(season_rk)
    except Exception as ex:
        _detail_screen([f"{Fore.RED}Could not load episodes: {ex}{Style.RESET_ALL}"])
        return
    if not eps:
        _detail_screen([f"{Style.DIM}No episodes found.{Style.RESET_ALL}"])
        return
    eps.sort(key=lambda e: (e.get("parentIndex") if e.get("parentIndex") is not None else 0,
                            e.get("index") if e.get("index") is not None else 9999))
    erows = []
    for e in eps:
        s, ep = e.get("parentIndex"), e.get("index")
        try:
            se = f"S{int(s):02d}E{int(ep):02d}"
        except Exception:
            se = ""
        watched = (e.get("viewCount") or 0) > 0
        star = _episode_rating_tag(e)
        wtag = (f"{Fore.GREEN}watched{Style.RESET_ALL}" if watched
                else f"{Style.DIM}unwatched{Style.RESET_ALL}")
        tag = (f"{Fore.YELLOW}{star}{Style.RESET_ALL}  " if star else "") + wtag
        erows.append({"label": f"{se}  {e.get('title') or ''}".strip() or se or "?",
                      "tag": tag, "rk": e.get("ratingKey"), "md": e})
    ecur = _WL_STATE.setdefault(f"eps:{season_rk}", [0])
    while True:
        head = [
            f"{Fore.CYAN}{show_title}{Style.RESET_ALL}"
            + (f"  ·  {season_label}" if season_label else "")
            + f"   ({len(erows)} episode(s))",
            f"{Style.DIM}type = search · Enter = plot & ratings · Esc = back{Style.RESET_ALL}",
            "",
        ]
        i = browse_menu("Episodes:", erows, header=head, default=ecur[0], cursor_out=ecur)
        if i is None:
            return
        _wl_episode_detail(client, erows[i])


def _wl_episode_detail(client, row):
    # the season listing is lightweight (no ratings, cast or crew); pull the full
    # episode metadata so the card can show the plot, rating, director/writer and cast
    md = row.get("md") or {}
    try:
        full = client.get_metadata(row["rk"])
        if full:
            md = full
    except Exception:
        pass
    _detail_screen(_episode_card(md))


def _watchlist_add(client, args):
    """Pick a library (movies / shows), then check the titles that are NOT yet on the
    watchlist to add them. Returns True if anything was added."""
    lib_idx = 0
    item_cursor = [0]
    item_view = {}
    while True:
        sec, lib_idx, single_lib = _pick_library(client, default=lib_idx)
        if sec is None:
            return False
        clear_screen()
        log_info(f"Loading '{sec['title']}' and your watchlist…")
        items = client.items_in_section(sec["key"], sec["type"])
        if not items:
            log_warn("The library is empty.")
            if single_lib:
                return False
            continue
        try:
            on_wl = client.watchlist_keys()
        except RuntimeError as ex:
            clear_screen()
            _watchlist_error(ex)
            _pause_to_menu()
            return False
        items.sort(key=lambda x: (x["title"] or "").lower())
        rows = [{"rk": it["ratingKey"], "title": it["title"], "guid": it.get("guid"),
                 "year": it.get("year"), "selected": False,
                 "label": f"{it['title']} ({it['year']})" if it.get("year") else it["title"]}
                for it in items]
        _ensure_guids(client, rows)

        addable, already, unmatched = [], 0, 0
        for r in rows:
            dk = discover_key_from_guid(r.get("guid"))
            if not dk:
                unmatched += 1
                continue
            r["dk"] = dk
            if dk in on_wl:
                already += 1
                continue
            addable.append(r)

        if not addable:
            clear_screen()
            log_info(f"Nothing to add from '{sec['title']}'.")
            note = []
            if already:
                note.append(f"{already} already on the watchlist")
            if unmatched:
                note.append(f"{unmatched} without a Plex match")
            if note:
                print(f"    {Style.DIM}({', '.join(note)}){Style.RESET_ALL}")
            _pause_to_menu()
            if single_lib:
                return False
            continue

        header = [
            f"{Fore.CYAN}Library:{Style.RESET_ALL} {sec['title']}   "
            f"({len(addable)} not yet on the watchlist"
            + (f" · {already} already on it" if already else "")
            + (f" · {unmatched} without a Plex match" if unmatched else "") + ")",
            f"{Style.DIM}Check the titles to ADD · / = search · + / - = check/uncheck by "
            f"pattern · a = all · n = none · i = invert · Enter = add{Style.RESET_ALL}",
            "",
        ]
        res = checkbox_menu("Select titles to add to your watchlist:", addable, header=header,
                            start_pos=item_cursor[0], pos_out=item_cursor, ui_state=item_view)
        if res is None:
            if single_lib:
                return False
            continue
        picked = [r for r in addable if r["selected"]]
        if not picked:
            log_info("Nothing selected.")
            continue

        clear_screen()
        log_info(f"Will ADD {len(picked)} title(s) to your watchlist:")
        for r in picked[:20]:
            print(f"    {r['title']}")
        if len(picked) > 20:
            print(f"    … (+{len(picked) - 20} more)")
        print()
        if not args.yes:
            ans = ask_yes_back("Add these to the watchlist?", default=True)
            if ans is None:
                continue  # Esc -> back to the selection (keeps the checkboxes)
            if not ans:
                continue
        print()
        return _apply_watchlist(client, [(r["dk"], r["title"], "add") for r in picked])


def _watchlist_edit(client, args):
    """Pick a library and reconcile its whole watchlist state at once: every item that
    has a Plex match is shown pre-checked when it's already on the watchlist. Ticking
    adds, unticking removes. Returns True if anything changed."""
    lib_idx = 0
    item_cursor = [0]
    item_view = {}
    while True:
        sec, lib_idx, single_lib = _pick_library(client, default=lib_idx)
        if sec is None:
            return False
        clear_screen()
        log_info(f"Loading '{sec['title']}' and your watchlist…")
        items = client.items_in_section(sec["key"], sec["type"])
        if not items:
            log_warn("The library is empty.")
            if single_lib:
                return False
            continue
        try:
            on_wl = client.watchlist_keys()
        except RuntimeError as ex:
            clear_screen()
            _watchlist_error(ex)
            _pause_to_menu()
            return False
        items.sort(key=lambda x: (x["title"] or "").lower())
        rows = [{"rk": it["ratingKey"], "title": it["title"], "guid": it.get("guid"),
                 "year": it.get("year"),
                 "label": f"{it['title']} ({it['year']})" if it.get("year") else it["title"]}
                for it in items]
        _ensure_guids(client, rows)

        usable, unmatched = [], 0
        for r in rows:
            dk = discover_key_from_guid(r.get("guid"))
            if not dk:
                unmatched += 1
                continue
            r["dk"] = dk
            r["orig_on"] = dk in on_wl
            r["selected"] = r["orig_on"]      # checked = currently on the watchlist
            r["tag"] = (f"{Fore.GREEN}on watchlist{Style.RESET_ALL}" if r["orig_on"]
                        else f"{Style.DIM}not on watchlist{Style.RESET_ALL}")
            usable.append(r)

        if not usable:
            clear_screen()
            log_info(f"No items in '{sec['title']}' have a Plex online match to watchlist.")
            _pause_to_menu()
            if single_lib:
                return False
            continue

        while True:  # editor level (Esc keeps the checkboxes and steps back)
            n_on = sum(1 for r in usable if r["orig_on"])
            header = [
                f"{Fore.CYAN}Library:{Style.RESET_ALL} {sec['title']}   "
                f"({len(usable)} items · {Fore.GREEN}{n_on} on the watchlist{Style.RESET_ALL}"
                + (f" · {unmatched} without a Plex match" if unmatched else "") + ")",
                f"{Style.DIM}[x] = on your watchlist · [ ] = not on it · Space toggles · "
                f"/ = search · a = all · n = none · i = invert · Enter = apply{Style.RESET_ALL}",
                "",
            ]
            res = checkbox_menu("Watchlist membership — check the titles that should be on it:",
                                usable, header=header, start_pos=item_cursor[0],
                                pos_out=item_cursor, ui_state=item_view)
            if res is None:
                if single_lib:
                    return False
                break  # back to the library picker

            to_add = [r for r in usable if r["selected"] and not r["orig_on"]]
            to_remove = [r for r in usable if not r["selected"] and r["orig_on"]]
            if not to_add and not to_remove:
                log_info("Nothing to change.")
                continue

            clear_screen()
            log_info(f"On your watchlist ({sec['title']}):")
            if to_add:
                print(f"    {Fore.GREEN}add:{Style.RESET_ALL}    "
                      + ", ".join(r["title"] for r in to_add[:12])
                      + (f" … (+{len(to_add) - 12} more)" if len(to_add) > 12 else ""))
            if to_remove:
                print(f"    {Fore.RED}remove:{Style.RESET_ALL} "
                      + ", ".join(r["title"] for r in to_remove[:12])
                      + (f" … (+{len(to_remove) - 12} more)" if len(to_remove) > 12 else ""))
            print()
            if not args.yes:
                ans = ask_yes_back("Apply these watchlist changes?", default=True)
                if ans is None:
                    continue  # Esc -> back to the editor (keeps the checkboxes)
                if not ans:
                    continue
            print()
            targets = ([(r["dk"], r["title"], "add") for r in to_add]
                       + [(r["dk"], r["title"], "remove") for r in to_remove])
            changed = _apply_watchlist(client, targets)
            for r in to_add:      # reflect the new state in case the user stays
                r["orig_on"] = True
            for r in to_remove:
                r["orig_on"] = False
            return changed


def _watchlist_remove(client, args):
    """Work straight from the current watchlist: movies first, then shows, each tagged
    with whether it lives on this server. Check the titles to remove. Returns True if
    anything was removed."""
    clear_screen()
    log_info("Loading your Plex watchlist…")
    try:
        wl = client.watchlist()
    except RuntimeError as ex:
        clear_screen()
        _watchlist_error(ex)
        _pause_to_menu()
        return False
    if not wl:
        clear_screen()
        log_info("Your watchlist is empty — nothing to remove.")
        _pause_to_menu()
        return False
    try:
        srv = _server_discover_index(client)
    except Exception:
        srv = {}

    type_order = {"movie": 0, "show": 1}
    wl.sort(key=lambda w: (type_order.get(w.get("type"), 2), (w.get("title") or "").lower()))
    rows = []
    for w in wl:
        on = w["ratingKey"] in srv
        yr = f" ({w['year']})" if w.get("year") else ""
        typ = w.get("type") or "?"
        tag = (f"{Fore.CYAN}{typ}{Style.RESET_ALL}  "
               + (f"{Fore.GREEN}on server{Style.RESET_ALL}" if on
                  else f"{Style.DIM}not on server{Style.RESET_ALL}"))
        rows.append({"rk": w["ratingKey"], "dk": w["ratingKey"], "title": w.get("title"),
                     "label": f"{(w.get('title') or '?')}{yr}", "selected": False, "tag": tag})

    on_srv = sum(1 for w in wl if w["ratingKey"] in srv)
    header = [
        f"{Fore.CYAN}Watchlist:{Style.RESET_ALL} {len(rows)} title(s)   "
        f"({Fore.GREEN}{on_srv} on this server{Style.RESET_ALL})",
        f"{Style.DIM}Check the titles to REMOVE · movies are listed first, then shows · "
        f"/ = search · a = all · n = none · Enter = remove{Style.RESET_ALL}",
        "",
    ]
    res = checkbox_menu("Select titles to remove from your watchlist:", rows, header=header)
    if res is None:
        return False
    picked = [r for r in rows if r["selected"]]
    if not picked:
        log_info("Nothing selected.")
        return False

    clear_screen()
    log_info(f"Will REMOVE {len(picked)} title(s) from your watchlist:")
    for r in picked[:20]:
        print(f"    {r['title']}")
    if len(picked) > 20:
        print(f"    … (+{len(picked) - 20} more)")
    print()
    if not args.yes:
        ans = ask_yes_back("Remove these from the watchlist?", default=True)
        if ans is None or not ans:
            return False
    print()
    return _apply_watchlist(client, [(r["dk"], r["title"], "remove") for r in picked])


def watchlist_flow(client, args):
    """Little sub-menu for managing the signed-in user's Plex watchlist. Adding and
    editing browse this server's libraries (movies / shows first, like the other
    tools); viewing and removing work from the watchlist itself. It handles its own
    'press Enter' pauses, so it always reports False to the main menu."""
    actions = [
        ("Show the current watchlist", _watchlist_show),
        ("Add titles from a library  —  pick movies / shows to put on the watchlist", _watchlist_add),
        ("Edit a library's watchlist  —  tick / untick to add & remove in bulk", _watchlist_edit),
        ("Remove titles from the watchlist", _watchlist_remove),
    ]
    last = 0
    while True:
        header = [
            f"{Fore.MAGENTA}{Style.BRIGHT}Watchlist{Style.RESET_ALL}  ·  {client.base_url}",
            f"{Style.DIM}Adding & editing use the movies and shows on this server."
            f"{Style.RESET_ALL}",
            "",
        ]
        idx = interactive_menu("Watchlist — choose an action:",
                               [a[0] for a in actions] + ["Back"],
                               default=last, allow_cancel=True, header=header)
        if idx is None or idx == len(actions):
            return False
        last = idx
        try:
            changed = actions[idx][1](client, args)
        except RuntimeError as ex:
            clear_screen()
            _watchlist_error(ex)
            _pause_to_menu()
            changed = False
        if changed:
            _pause_to_menu()


def _pause_to_menu():
    msg = f"\n{Style.DIM}Press Enter to return to the main menu…{Style.RESET_ALL}"
    if not _tui_supported():
        try:
            input(msg)
        except EOFError:
            pass
        return
    print(msg)
    with _RawMode():
        while True:
            if _read_key() in ("enter", "esc"):
                return


def top_menu(client, args):
    """Top-level hub: pick a tool, run it, then return here. Esc/Quit exits.
    The 'press Enter' pause is shown only when a tool actually made a change."""
    tools = [
        ("Default audio & subtitles  —  set the default audio/subtitle track", langsubs_flow),
        ("Watched / unwatched  —  mark episodes or a whole show", mark_watched_flow),
        ("Labels  —  add / remove / rename custom labels on shows & movies", labels_flow),
        ("Titles  —  find non-English titles and switch them to English", titles_flow),
        ("Watchlist  —  view / add / edit / remove titles on your Plex watchlist", watchlist_flow),
    ]
    last = 0
    while True:
        header = [
            f"{Fore.MAGENTA}{Style.BRIGHT}plex_tools{Style.RESET_ALL}  ·  {client.base_url}",
            f"{Style.DIM}config: {CONFIG_SOURCE}{Style.RESET_ALL}" if CONFIG_SOURCE else "",
            "",
        ]
        idx = interactive_menu("Main menu — choose a tool:",
                               [t[0] for t in tools] + ["Quit"],
                               default=last, allow_cancel=True, header=header)
        if idx is None or idx == len(tools):
            return
        last = idx  # come back to the same tool next time
        changed = tools[idx][1](client, args)
        if changed:
            _pause_to_menu()  # only pause after an actual change; on cancel/back go straight back


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Plex tools: set the default audio/subtitle tracks for a show "
                    "or movie, mark videos watched/unwatched, manage labels and titles, "
                    "or manage your Plex watchlist.")
    ap.add_argument("--base-url", help="Server address/FQDN (otherwise the saved one is used / it asks)")
    ap.add_argument("--token", help="X-Plex-Token directly (skips sign-in)")
    ap.add_argument("--show", help="ratingKey, Plex URL, or show/movie title")
    ap.add_argument("--audio", help="Audio language code/name or 'keep'")
    ap.add_argument("--subs", help="Subtitle code/name, 'off' or 'keep'")
    ap.add_argument("--insecure", action="store_true", help="Do not validate the HTTPS certificate")
    ap.add_argument("--dry-run", action="store_true", help="Only show the plan")
    ap.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    ap.add_argument("--logout", action="store_true", help="Delete saved tokens and exit")
    ap.add_argument("--relogin", action="store_true", help="Force a fresh sign-in")
    ap.add_argument("--switch-user", action="store_true", help="Pick the Home user again")
    ap.add_argument("--watched", action="store_true",
                    help="Watched/unwatched mode: mark selection as WATCHED")
    ap.add_argument("--unwatched", action="store_true",
                    help="Watched/unwatched mode: mark selection as UNWATCHED")
    ap.add_argument("--config", help="Path to the config file (otherwise searched next to the script, in .config next to the script and parent folders, and in ~/.config)")
    ap.add_argument("--config-url", help="URL of a JSON config that OVERRIDES the local one (primary source; falls back to the local config if unreachable). Also settable via CONFIG_URL / PLEX_TOOLS_CONFIG_URL.")
    ap.add_argument("--no-local-config", action="store_true",
                    help="Pure-remote mode: never write a local config file (put client_id and tokens in the remote JSON).")
    ap.add_argument("--where-config", action="store_true",
                    help="Print the path of the config file in use and exit")
    args = ap.parse_args()

    global _CONFIG_OVERRIDE, _CONFIG_URL_OVERRIDE, _NO_LOCAL_CONFIG
    if args.config:
        _CONFIG_OVERRIDE = args.config
    if args.config_url:
        _CONFIG_URL_OVERRIDE = args.config_url
    if args.no_local_config:
        _NO_LOCAL_CONFIG = True
    resolve_config_path()
    if args.where_config:
        exists = "exists" if (CONFIG_PATH and os.path.isfile(CONFIG_PATH)) else "does not exist yet"
        print(f"Config: {CONFIG_PATH}  ({exists})")
        url = _config_url()
        if url:
            print(f"Remote config (primary): {url}")
        return

    cfg = load_config()
    client_id = get_client_id(cfg)

    if args.logout:
        for k in ("account_token", "user_token", "home_user"):
            cfg.pop(k, None)
        save_config(cfg)
        log_done("Logged out (tokens deleted). The server address is kept.")
        return

    if args.relogin:
        for k in ("account_token", "user_token", "home_user"):
            cfg.pop(k, None)
        save_config(cfg)

    if args.switch_user and cfg.get("account_token"):
        acct = PlexAccount(client_id, cfg["account_token"])
        try:
            choose_home_user(acct, cfg)
        except RuntimeError as ex:
            if "UNAUTHORIZED" in str(ex):
                cfg.pop("account_token", None)
                save_config(cfg)
            else:
                die(str(ex))

    print(f"{Fore.MAGENTA}=== plex_tools ==={Style.RESET_ALL}")

    client = build_client(args, cfg)

    # CLI shortcuts run one tool directly (automation); otherwise show the hub menu
    if args.watched or args.unwatched:
        mark_watched_flow(client, args)
        return
    if args.audio or args.subs:
        langsubs_flow(client, args)
        return

    top_menu(client, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warn("Interrupted by user.")
        sys.exit(130)
