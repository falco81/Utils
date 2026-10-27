#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
github_tools.py  (Windows 10 CLI, colored interactive UI via colorama)
=====================================================================

Upload / update a local folder to GitHub and download repositories - either
interactively (arrow-key menus, type-to-search) or fully via parameters.

  * UPLOAD / UPDATE: pushes the content of a folder (incl. subfolders) to a
    branch of one of your repositories as ONE commit. Only changed files are
    sent. Two modes: "update" (add + overwrite, keep other remote files) and
    "mirror" (the branch becomes an exact copy of the folder, removed files are
    deleted). Can create a new repository / a new branch, a tag or a release
    (with attached files). .gitignore files are honoured.
  * DOWNLOAD: the source of any branch / tag / release / commit (extracted
    into a folder, or kept as .zip), or the assets of a release.

The repositories, branches, tags and releases offered in the menus are read
live from your GitHub account.

Configuration (first match wins)
--------------------------------
  1) CONFIG_URL below (or --config-url / env GITHUB_TOOLS_CONFIG_URL): a local
     path, a UNC/samba path (\\\\nas\\share\\github_tools.config.json) or an
     http/https URL. It is ALWAYS READ-ONLY - the script never writes there.
     If the JSON exists, its keys are used with the highest priority.
  2) github_tools.config.json in the folder the script was started from
     (current directory), then next to the script.
  3) Nothing found -> the script asks interactively and CREATES the config in
     the folder it was started from.
  Values missing in the shared config (e.g. a token) are asked for and stored
  in the local config only.

Sign-in
-------
  * Personal access token (classic or fine-grained).
  * Username + password + 2FA/OTP: GitHub no longer accepts passwords in its
    API or git (since 2020/2021), so this goes through GitHub's official
    "device flow": you sign in on github.com (username, password, OTP code)
    and the script receives a token. It needs the Client ID of an OAuth App
    with "Device Flow" enabled (OAUTH_CLIENT_ID below, or it asks once).

Text input
----------
  Paste with right-click, Shift+Insert or Ctrl+V; passwords/tokens are shown as
  asterisks; Alt+numpad codes work (Alt+64 = @, Alt+0169 = (c), ...).

Requirements: Python 3.8+, optional `pip install colorama` (colors; runs
without it too). No git installation needed - everything uses the GitHub API.
Run with --help for all parameters and examples.
"""

# =============================================================================
#  USER SETTINGS
# =============================================================================
# Shared, READ-ONLY configuration source. Leave empty to disable. Examples:
#   CONFIG_URL = r"D:\config\github_tools.config.json"
#   CONFIG_URL = r"\\nas\share\config\github_tools.config.json"   (UNC / samba)
#   CONFIG_URL = r"\\nas\share\config"           (a folder: the file name is added)
#   CONFIG_URL = "https://nas.example.net/github_tools.config.json"
CONFIG_URL = "http://nas.falco81.net/github_tools.config.json"

# Client ID of YOUR GitHub OAuth App (Settings > Developer settings > OAuth Apps,
# tick "Enable Device Flow"). Needed only for "username + password + OTP" sign-in.
# Can also live in the config as "oauth_client_id".
OAUTH_CLIENT_ID = ""

DEFAULT_API_URL = "https://api.github.com"   # GitHub Enterprise: https://HOST/api/v3
CONFIG_FILENAME = "github_tools.config.json"
CONFIG_FETCH_TIMEOUT = 5          # s - an unreachable share/URL must not hang startup
UPLOAD_WORKERS = 3                # parallel blob uploads
MAX_FILE_SIZE = 100 * 1024 * 1024 # GitHub hard limit per file
INLINE_TEXT_LIMIT = 400 * 1024    # text files up to this size go inline (fewer requests)
# =============================================================================

import argparse
import base64
import concurrent.futures
import datetime
import fnmatch
import getpass
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile

VERSION = "1.0"
USER_AGENT = f"github_tools/{VERSION}"
_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Console setup: VT sequences + colours (colorama optional)
# ---------------------------------------------------------------------------
def _enable_vt():
    """Turn on ANSI/VT processing of the Windows 10 console (needed for the
    in-place redraw of menus). Returns True when ANSI sequences work."""
    if not _WINDOWS:
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


_VT = _enable_vt()

try:
    import colorama
    from colorama import Fore, Style
    if not _VT:
        colorama.init()      # old console: let colorama translate the colours
except ImportError:
    class _NoColor:
        def __getattr__(self, _name):
            return ""
    Fore = _NoColor()
    Style = _NoColor()

try:   # optional: verify HTTPS via the OS certificate store
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

try:   # make sure unicode output never crashes on an odd code page
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def log_info(msg):
    print(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")


def log_warn(msg):
    print(f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL} {msg}")


def log_done(msg):
    print(f"{Fore.GREEN}[DONE]{Style.RESET_ALL} {msg}")


def log_err(msg):
    print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}")


def die(msg, code=1):
    print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}", file=sys.stderr)
    sys.exit(code)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(s):
    return _ANSI_RE.sub("", str(s))


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def short_date(iso):
    return (iso or "")[:10]


def term_size():
    try:
        sz = os.get_terminal_size()
        cols, lines = sz.columns, sz.lines
    except Exception:
        cols, lines = 0, 0
    return (cols if cols >= 20 else 80), (lines if lines >= 8 else 24)


def clear_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def progress_bar(done, total, width=30):
    if total:
        pct = max(0, min(100, done * 100 // total))
        fill = round(width * pct / 100)
        return "[" + "#" * fill + "-" * (width - fill) + f"] {pct:3d}%"
    return f"[{'?' * 3}] {human_size(done)}"


def show_progress(done, total, label=""):
    cols, _ = term_size()
    line = f"  {progress_bar(done, total)}  {label}"
    sys.stdout.write("\r" + line[:max(10, cols - 2)] + "\x1b[K")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
if _WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32.GetStdHandle.restype = wintypes.HANDLE
    _k32.GetStdHandle.argtypes = [wintypes.DWORD]
    _k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GlobalLock.argtypes = [ctypes.c_void_p]
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _u32.OpenClipboard.argtypes = [wintypes.HWND]
    _u32.GetClipboardData.argtypes = [wintypes.UINT]
    _u32.GetClipboardData.restype = ctypes.c_void_p
    _u32.GetKeyState.argtypes = [ctypes.c_int]
    _u32.GetKeyState.restype = ctypes.c_short


def clipboard_text():
    """Current clipboard text ('' when empty / unavailable)."""
    try:
        if _WINDOWS:
            if not _u32.OpenClipboard(None):
                return ""
            try:
                h = _u32.GetClipboardData(13)          # CF_UNICODETEXT
                if not h:
                    return ""
                p = _k32.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)
                finally:
                    _k32.GlobalUnlock(h)
            finally:
                _u32.CloseClipboard()
        for cmd in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"],
                    ["xsel", "-ob"]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=2)
                if r.returncode == 0:
                    return r.stdout.decode("utf-8", "replace")
            except Exception:
                continue
    except Exception:
        pass
    return ""


def copy_to_clipboard(text):
    """Best effort - returns True when the text landed in the clipboard."""
    try:
        if _WINDOWS:
            r = subprocess.run(["clip"], input=text.encode("utf-16"), timeout=3)
            return r.returncode == 0
        for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["xsel", "-ib"]):
            try:
                if subprocess.run(cmd, input=text.encode(), timeout=2).returncode == 0:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Keyboard input
# ---------------------------------------------------------------------------
# Every reader returns an action string ('up', 'enter', 'esc', ...), a
# ('char', c) tuple, a ('paste', text) tuple or 'timeout'.
def alt_code_char(digits, oem_cp=437, ansi_cp=1252):
    """Character for an Alt+numpad code, the way Windows does it:
    Alt+NNN uses the OEM code page, Alt+0NNN the ANSI code page (values mod 256).
    Returns None for codes that give no printable character."""
    if not digits or not digits.isdigit():
        return None
    n = int(digits) % 256
    if n == 0:
        return None
    cp = ansi_cp if digits.startswith("0") else oem_cp
    try:
        c = bytes([n]).decode(f"cp{cp}")
    except Exception:
        c = bytes([n]).decode("latin-1")
    return c if c.isprintable() else None


if _WINDOWS:
    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                    ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

    class _EVENT_UNION(ctypes.Union):
        _fields_ = [("KeyEvent", _KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]

    class _INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT_UNION)]

    _k32.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_INPUT_RECORD),
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    _VK_NAV = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x24: "home",
               0x23: "end", 0x21: "pgup", 0x22: "pgdn", 0x2E: "delete", 0x2D: "insert",
               0x74: "f5", 0x78: "f9", 0x0D: "enter", 0x1B: "esc", 0x09: "tab",
               0x08: "backspace"}
    _VK_MODIFIERS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x5D, 0x90, 0x91,
                     0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
    _VK_NUMPAD = {0x60 + i: str(i) for i in range(10)}
    # numpad keys with NumLock off (non-"enhanced" = they came from the numpad)
    _VK_NUMPAD_NAV = {0x2D: "0", 0x23: "1", 0x28: "2", 0x22: "3", 0x25: "4",
                      0x0C: "5", 0x27: "6", 0x24: "7", 0x26: "8", 0x21: "9"}

    class _WinKeys:
        """Reads raw console key events (ReadConsoleInputW). This sees what the
        msvcrt readers miss: Alt+numpad codes (delivered on Alt release), the
        Shift state of Insert and Ctrl+V. Right-click paste arrives as normal
        typed characters."""

        def __init__(self):
            self.h = _k32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))
            m = wintypes.DWORD()
            self.ok = bool(self.h) and bool(_k32.GetConsoleMode(self.h, ctypes.byref(m)))
            self.alt_digits = ""
            self.pending = []
            try:
                self.oem_cp = _k32.GetOEMCP()
                self.ansi_cp = _k32.GetACP()
            except Exception:
                self.oem_cp, self.ansi_cp = 437, 1252

        def read(self, timeout=None):
            end = None if timeout is None else time.monotonic() + timeout
            while True:
                if self.pending:
                    return self.pending.pop(0)
                wait = 100     # short waits so Ctrl+C (KeyboardInterrupt) gets through
                if end is not None:
                    rem = end - time.monotonic()
                    if rem <= 0:
                        return "timeout"
                    wait = max(1, min(100, int(rem * 1000)))
                if _k32.WaitForSingleObject(self.h, wait) != 0:
                    continue
                rec = _INPUT_RECORD()
                n = wintypes.DWORD()
                if not _k32.ReadConsoleInputW(self.h, ctypes.byref(rec), 1, ctypes.byref(n)):
                    continue
                if n.value == 0 or rec.EventType != 1:      # KEY_EVENT only
                    continue
                res = self._translate(rec.Event.KeyEvent)
                if res is not None:
                    return res

        def _translate(self, ev):
            vk, st, ch = ev.wVirtualKeyCode, ev.dwControlKeyState, ev.uChar
            alt = bool(st & 0x0003)
            ctrl = bool(st & 0x000C)
            shift = bool(st & 0x0010)
            enhanced = bool(st & 0x0100)
            if not ev.bKeyDown:
                if vk == 0x12:                              # Alt released
                    digits, self.alt_digits = self.alt_digits, ""
                    if ch and ch != "\x00" and ch.isprintable():
                        return ("char", ch)                 # console composed it
                    if digits:
                        c = alt_code_char(digits, self.oem_cp, self.ansi_cp)
                        return ("char", c) if c else None
                return None
            if vk in _VK_MODIFIERS:
                return None
            if alt and not ctrl:                            # Alt+numpad digits
                d = _VK_NUMPAD.get(vk)
                if d is None and not enhanced:
                    d = _VK_NUMPAD_NAV.get(vk)
                if d is not None:
                    self.alt_digits += d
                    return None
                return None                                 # other Alt+key: ignore
            if ctrl and not alt and vk == 0x43:
                raise KeyboardInterrupt
            if (ctrl and not alt and vk == 0x56) or (vk == 0x2D and shift and not ctrl):
                return ("paste", clipboard_text())          # Ctrl+V / Shift+Insert
            if ch == "\x03":
                raise KeyboardInterrupt
            if vk in _VK_NAV and not (alt and ctrl and ch and ch.isprintable()):
                res = _VK_NAV[vk]
            elif ch and ch != "\x00" and ord(ch) >= 32:     # incl. AltGr characters
                res = ("char", ch)
            else:
                return None
            rep = int(ev.wRepeatCount or 1)
            if rep > 1:
                self.pending.extend([res] * (rep - 1))
            return res

    _WINKEYS = _WinKeys()
    _HAS_RAW = _WINKEYS.ok
else:
    _WINKEYS = None
    try:
        import termios
        import tty
        _HAS_RAW = True
    except Exception:
        _HAS_RAW = False


def _read_key_unix(timeout=None):
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
    if c == 0x1b:
        r, _, _ = select.select([fd], [], [], 0.05)
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
        if seq == "200~":                     # bracketed paste: read until 201~
            data = b""
            while not data.endswith(b"\x1b[201~"):
                nb = os.read(fd, 1)
                if not nb:
                    break
                data += nb
            return ("paste", data[:-6].decode("utf-8", "replace"))
        return {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
                "1~": "home", "4~": "end", "2~": "insert", "3~": "delete", "5~": "pgup",
                "6~": "pgdn", "15~": "f5", "20~": "f9", "Z": "tab"}.get(seq, "other")
    if c in (0x0d, 0x0a):
        return "enter"
    if c == 0x09:
        return "tab"
    if c in (0x7f, 0x08):
        return "backspace"
    if c == 0x03:
        raise KeyboardInterrupt
    if c == 0x16:
        return ("paste", clipboard_text())
    if c < 0x20:
        return "other"
    if c < 0x80:
        return ("char", chr(c))
    n = 3 if c >= 0xf0 else 2 if c >= 0xe0 else 1 if c >= 0xc0 else 0
    if not n:
        return "other"
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


def _read_key(timeout=None):
    if _WINDOWS:
        return _WINKEYS.read(timeout)
    return _read_key_unix(timeout)


class _RawMode:
    """Terminal raw mode (Unix only; Windows reads raw events anyway)."""
    def __enter__(self):
        if not _WINDOWS:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
            sys.stdout.write("\x1b[?2004h")    # bracketed paste on
            sys.stdout.flush()
        return self

    def __exit__(self, *a):
        if not _WINDOWS:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _tui_supported():
    return _HAS_RAW and _VT and sys.stdin.isatty() and sys.stdout.isatty()


def _one_line(text):
    """Pasted text for a one-line field: line breaks removed."""
    return "".join((text or "").replace("\r\n", "\n").splitlines())


# ---------------------------------------------------------------------------
# Interactive widgets
# ---------------------------------------------------------------------------
_YES = False          # --yes: never ask, use defaults


def can_ask():
    return (not _YES) and sys.stdin.isatty()


def _match_filter(text, filt):
    """Case-insensitive: plain text = substring, '*' / '?' = wildcards."""
    if not filt:
        return True
    t, f = text.lower(), filt.lower()
    if any(c in f for c in "*?["):
        return fnmatch.fnmatch(t, f) or fnmatch.fnmatch(t, f"*{f}*")
    return f in t


def _scrollbar_range(nrows, total, visible, top):
    if total <= visible or nrows <= 0:
        return None
    thumb = max(1, min(nrows, round(nrows * visible / total)))
    span = nrows - thumb
    denom = max(1, total - visible)
    return (max(0, min(span, round(span * (top / denom)))), thumb)


def _with_scrollbar(line, cols, r, sbr):
    if sbr is None:
        return line
    tstart, thumb = sbr
    cell = (f"{Fore.CYAN}{Style.BRIGHT}\u2588{Style.RESET_ALL}" if tstart <= r < tstart + thumb
            else f"{Style.DIM}\u2502{Style.RESET_ALL}")
    pad = max(1, (cols - 2) - len(strip_ansi(line)))
    return line + " " * pad + cell


def _trunc(s, w):
    return s[:max(1, w - 1)] + "…" if len(s) > w else s


def _browse_order(rows, plain, filt):
    """Visible rows for the search text; a header stays only if a child matches."""
    n = len(rows)
    if not filt:
        return list(range(n))
    mset = {i for i in range(n) if not rows[i].get("header") and _match_filter(plain[i], filt)}
    kids, cur = {}, None
    for i in range(n):
        if rows[i].get("header"):
            cur = i
            kids[cur] = []
        elif cur is not None:
            kids[cur].append(i)
    keep = {h for h, ks in kids.items() if any(k in mset for k in ks)}
    return [i for i in range(n) if i in mset or (rows[i].get("header") and i in keep)]


def _select_classic(prompt, rows, header, default, allow_back):
    for h in header or []:
        print(strip_ansi(h))
    sel = [i for i, r in enumerate(rows) if not r.get("header")]
    print(strip_ansi(prompt))
    for i, r in enumerate(rows):
        if r.get("header"):
            print(f"  {strip_ansi(r['label'])}")
            continue
        tag = f"   {strip_ansi(r['tag'])}" if r.get("tag") else ""
        print(f"  {sel.index(i) + 1:>3}) {strip_ansi(r['label'])}{tag}")
    dnum = sel.index(default) + 1 if default in sel else 1
    while True:
        try:
            raw = input(f"Choice [1-{len(sel)}, Enter = {dnum}"
                        + (", q = back" if allow_back else "") + "]: ").strip().lower()
        except EOFError:
            return None
        if raw == "":
            return sel[dnum - 1]
        if raw == "q" and allow_back:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(sel):
            return sel[int(raw) - 1]
        print("Invalid choice.")


def select_menu(prompt, rows, header=None, default=None, allow_back=True, cursor_out=None):
    """Arrow-key menu with type-to-search. rows: strings or dicts with 'label',
    optional 'tag' (aligned coloured status column) and 'header' (section title,
    not selectable). Returns the chosen row index, or None (Esc = back)."""
    rows = [r if isinstance(r, dict) else {"label": str(r)} for r in rows]
    n = len(rows)
    plain = [strip_ansi(r["label"]) for r in rows]
    selectable = [i for i in range(n) if not rows[i].get("header")]
    if not selectable:
        return None
    if default is None or default not in selectable:
        default = selectable[0]
    if not _tui_supported():
        return _select_classic(prompt, rows, header, default, allow_back)

    header = [h for h in (header or [])]
    filt = ""
    prev_lines = 0
    first = True

    def land_on(order, want):
        cand = [p for p, i in enumerate(order) if not rows[i].get("header")]
        if not cand:
            return 0
        for p in cand:
            if order[p] == want:
                return p
        return min(cand, key=lambda p: abs(order[p] - want))

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
        maxw = max(10, cols - 3)
        page_rows = max(3, rows_total - (6 + len(header)))
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
            vis.append(h if len(sp) <= maxw else _trunc(sp, maxw))
        vis.append(f"{Fore.YELLOW}{_trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        if filt:
            hint = "↑↓ move · Enter = select · Esc = clear search"
        else:
            hint = "↑↓ move · type = search · Enter = select" + (" · Esc = back" if allow_back else "")
        vis.append(f"{Fore.CYAN}{_trunc(hint, maxw)}{Style.RESET_ALL}")
        if not order:
            vis.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
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
                    line = f"  {Fore.CYAN}{Style.BRIGHT}{_trunc(plain[i], maxw - 4)}{Style.RESET_ALL}"
                    vis.append(_with_scrollbar(line, cols, pos - start, sbr))
                    continue
                tag = r.get("tag")
                if tag:
                    body, tail = fit(plain[i], textw), f"{' ' * gap}{tag}"
                else:
                    body, tail = _trunc(plain[i], maxw - 6), ""
                if pos == sel_pos:
                    line = f"{Fore.GREEN}{Style.BRIGHT}› {body}{strip_ansi(tail)}{Style.RESET_ALL}"
                else:
                    color = r.get("color", "")
                    line = f"  {color}{body}{Style.RESET_ALL if color else ''}{tail}"
                vis.append(_with_scrollbar(line, cols, pos - start, sbr))
            rest = len(order) - (start + len(window))
            if rest > 0:
                vis.append(f"  {Fore.CYAN}▼ ({rest} below){Style.RESET_ALL}")
        cnt = sum(1 for i in order if not rows[i].get("header"))
        cur = sum(1 for p, i in enumerate(order) if p <= sel_pos and not rows[i].get("header"))
        info = f" [{cur}/{cnt}]" if cnt else ""
        if filt:
            vis.append(f"{Fore.MAGENTA}{_trunc('Search: ' + filt + info, maxw)}{Style.RESET_ALL}")
        else:
            vis.append(f"{Fore.CYAN}{_trunc('(type to search)' + info, maxw)}{Style.RESET_ALL}")
        buf.append("\r\n".join(vis))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)
        return page_rows

    with _RawMode():
        order = _browse_order(rows, plain, filt)
        sel_pos = land_on(order, default)
        page_rows = render(order, sel_pos)
        while True:
            key = _read_key()
            if key == "up":
                sel_pos = step(order, sel_pos, -1)
            elif key == "down":
                sel_pos = step(order, sel_pos, 1)
            elif key == "pgup" and order:
                sel_pos = max(0, sel_pos - page_rows)
                if rows[order[sel_pos]].get("header"):
                    sel_pos = step(order, sel_pos, 1)
            elif key == "pgdn" and order:
                sel_pos = min(len(order) - 1, sel_pos + page_rows)
                if rows[order[sel_pos]].get("header"):
                    sel_pos = step(order, sel_pos, -1)
            elif key == "home" and order:
                sel_pos = step(order, -1, 1)
            elif key == "end" and order:
                sel_pos = step(order, 0, -1)
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
                    want = order[sel_pos] if order else 0
                    filt = ""
                    order = _browse_order(rows, plain, filt)
                    sel_pos = land_on(order, want)
                elif allow_back:
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
            elif isinstance(key, tuple) and key[0] in ("char", "paste"):
                txt = _one_line(key[1]) if key[0] == "paste" else key[1]
                if txt and txt.isprintable():
                    filt += txt
                    order = _browse_order(rows, plain, filt)
                    sel_pos = land_on(order, 0)
            page_rows = render(order, sel_pos)


def _checkbox_classic(prompt, rows, header=None):
    for h in header or []:
        print(strip_ansi(h))
    while True:
        print(strip_ansi(prompt))
        for i, r in enumerate(rows):
            box = "[x]" if r.get("selected") else "[ ]"
            tag = f"   {strip_ansi(r['tag'])}" if r.get("tag") else ""
            print(f"  {i + 1:>3}) {box} {strip_ansi(r['label'])}{tag}")
        try:
            raw = input("Number = toggle · a = all · n = none · Enter = confirm · q = back: ").strip().lower()
        except EOFError:
            return None
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
        elif raw.isdigit() and 1 <= int(raw) <= len(rows):
            rows[int(raw) - 1]["selected"] = not rows[int(raw) - 1].get("selected")


def checkbox_menu(prompt, rows, header=None):
    """Multi-select with [x]/[ ] boxes. rows: dicts 'label', 'selected', optional
    'tag'. Space toggle · a all · n none · i invert · / search · Enter confirm ·
    Esc back. Returns rows (with 'selected' set) or None."""
    if not _tui_supported():
        return _checkbox_classic(prompt, rows, header)
    plain = [strip_ansi(r["label"]) for r in rows]
    header = list(header or [])
    filt, search = "", False
    sel_pos, prev_lines, first = 0, 0, True

    def order():
        return [i for i in range(len(rows)) if not filt or _match_filter(plain[i], filt)]

    def render(o):
        nonlocal prev_lines, first
        cols, rows_total = term_size()
        maxw = max(10, cols - 3)
        page_rows = max(3, rows_total - (7 + len(header)))
        buf = []
        if first:
            buf.append("\x1b[2J\x1b[H")
            first = False
        elif prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")
        vis = [h if len(strip_ansi(h)) <= maxw else _trunc(strip_ansi(h), maxw) for h in header]
        vis.append(f"{Fore.YELLOW}{_trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        hint = ("type to search · Enter = done · Esc = clear" if search else
                "Space toggle · a all · n none · i invert · / search · Enter confirm · Esc back")
        vis.append(f"{Fore.CYAN}{_trunc(hint, maxw)}{Style.RESET_ALL}")
        if not o:
            vis.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
            statw = max((len(strip_ansi(rows[i].get("tag") or "")) for i in o), default=0)
            longest = max((len(plain[i]) for i in o), default=0)
            textw = max(6, min(maxw - 10 - (3 + statw), longest + 1)) if statw else maxw - 10
            start = max(0, min(sel_pos - page_rows // 2, len(o) - page_rows))
            window = o[start:start + page_rows]
            sbr = _scrollbar_range(len(window), len(o), len(window), start)
            if start > 0:
                vis.append(f"  {Fore.CYAN}▲ ({start} above){Style.RESET_ALL}")
            for pos, i in enumerate(window, start):
                r = rows[i]
                box = "[x]" if r.get("selected") else "[ ]"
                text = plain[i]
                text = (text[:textw - 1] + "…") if len(text) > textw else text + " " * (textw - len(text))
                tag = f"   {r['tag']}" if r.get("tag") else ""
                if pos == sel_pos:
                    line = f"{Fore.GREEN}{Style.BRIGHT}› {box} {text}{strip_ansi(tag)}{Style.RESET_ALL}"
                else:
                    b = f"{Fore.GREEN}{box}{Style.RESET_ALL}" if r.get("selected") else box
                    line = f"  {b} {text}{tag}"
                vis.append(_with_scrollbar(line, cols, pos - start, sbr))
            rest = len(o) - (start + len(window))
            if rest > 0:
                vis.append(f"  {Fore.CYAN}▼ ({rest} below){Style.RESET_ALL}")
        nsel = sum(1 for r in rows if r.get("selected"))
        foot = f"selected {nsel}/{len(rows)}" + (f" · search: {filt}" + ("_" if search else "")
                                                 if (filt or search) else "")
        vis.append(f"{Fore.MAGENTA}{_trunc(foot, maxw)}{Style.RESET_ALL}")
        buf.append("\r\n".join(vis))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis)

    with _RawMode():
        o = order()
        render(o)
        while True:
            key = _read_key()
            if search:
                if key in ("enter", "esc"):
                    if key == "esc":
                        filt = ""
                    search = False
                elif key == "backspace":
                    filt = filt[:-1]
                elif isinstance(key, tuple) and key[0] in ("char", "paste"):
                    filt += _one_line(key[1])
                o = order()
                sel_pos = 0 if key != "enter" else min(sel_pos, max(0, len(o) - 1))
                render(o)
                continue
            if key == "up" and o:
                sel_pos = (sel_pos - 1) % len(o)
            elif key == "down" and o:
                sel_pos = (sel_pos + 1) % len(o)
            elif key == "pgup" and o:
                sel_pos = max(0, sel_pos - 10)
            elif key == "pgdn" and o:
                sel_pos = min(len(o) - 1, sel_pos + 10)
            elif key == "home":
                sel_pos = 0
            elif key == "end" and o:
                sel_pos = len(o) - 1
            elif key == ("char", " ") and o:
                r = rows[o[sel_pos]]
                r["selected"] = not r.get("selected")
                sel_pos = min(sel_pos + 1, len(o) - 1)
            elif key == ("char", "a"):
                for i in o:
                    rows[i]["selected"] = True
            elif key == ("char", "n"):
                for i in o:
                    rows[i]["selected"] = False
            elif key == ("char", "i"):
                for i in o:
                    rows[i]["selected"] = not rows[i].get("selected")
            elif key == ("char", "/"):
                search, filt, sel_pos = True, "", 0
                o = order()
            elif key == "enter":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return rows
            elif key == "esc":
                if filt:
                    filt, sel_pos = "", 0
                    o = order()
                else:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return None
            render(o)


def ask_line(prompt, default="", secret=False, allow_empty=False):
    """One-line text input. Esc = back (returns None). Paste: right-click,
    Shift+Insert, Ctrl+V. Secret input is shown as asterisks. Alt+numpad codes
    (Alt+64 = @) are supported. Arrows/Home/End/Delete edit inside the text."""
    if not _tui_supported():
        if not sys.stdin.isatty() and not secret:
            pass
        try:
            if secret:
                raw = getpass.getpass(f"{strip_ansi(prompt)}: ")
            else:
                raw = input(f"{strip_ansi(prompt)}{f' [{default}]' if default else ''}: ")
        except EOFError:
            return None
        raw = raw.strip()
        if not raw:
            return default if default else ("" if allow_empty else None)
        return raw
    buf = list(default or "")
    pos = len(buf)
    view_start = 0
    tip = "Enter = OK · Esc = back · paste: right-click / Shift+Insert / Ctrl+V"
    sys.stdout.write(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}\r\n"
                     f"{Style.DIM}{tip}{Style.RESET_ALL}\r\n")
    sys.stdout.flush()

    def draw():
        nonlocal view_start
        cols, _ = term_size()
        w = max(10, cols - 5)
        disp = ("*" * len(buf)) if secret else "".join(buf)
        if pos < view_start:
            view_start = pos
        if pos >= view_start + w:
            view_start = pos - w + 1
        view_start = max(0, min(view_start, max(0, len(disp) - w + 1)))
        part = disp[view_start:view_start + w]
        back = len(part) - (pos - view_start)
        sys.stdout.write(f"\r\x1b[K{Fore.GREEN}> {Style.RESET_ALL}{part}"
                         + (f"\x1b[{back}D" if back > 0 else ""))
        sys.stdout.flush()

    with _RawMode():
        while True:
            draw()
            k = _read_key()
            if k == "esc":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return None
            if k == "enter":
                val = "".join(buf).strip()
                if val or allow_empty:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return val
                sys.stdout.write("\a")          # empty and required: stay
                continue
            if k == "backspace":
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif k == "delete":
                if pos < len(buf):
                    del buf[pos]
            elif k == "left":
                pos = max(0, pos - 1)
            elif k == "right":
                pos = min(len(buf), pos + 1)
            elif k == "home":
                pos = 0
            elif k == "end":
                pos = len(buf)
            elif isinstance(k, tuple) and k[0] in ("char", "paste"):
                txt = _one_line(k[1]) if k[0] == "paste" else k[1]
                txt = "".join(c for c in txt if c.isprintable())
                if txt:
                    buf[pos:pos] = list(txt)
                    pos += len(txt)


def ask_yes_back(prompt, default=True):
    """y / n / Enter = default; Esc = back (None)."""
    d = "Y/n" if default else "y/N"
    msg = f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} [{d}] {Style.DIM}(Esc = back){Style.RESET_ALL} "
    if not _tui_supported():
        try:
            raw = input(strip_ansi(msg)).strip().lower()
        except EOFError:
            return None
        if raw == "":
            return default
        if raw in ("q", "esc"):
            return None
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
            if isinstance(k, tuple) and k[0] == "char" and k[1].lower() in ("y", "n"):
                sys.stdout.write(k[1].lower() + "\r\n")
                sys.stdout.flush()
                return k[1].lower() == "y"


def pause(msg="Press Enter to continue…"):
    text = f"\n{Style.DIM}{msg}{Style.RESET_ALL}"
    if not sys.stdin.isatty() or _YES:
        return
    if not _tui_supported():
        try:
            input(strip_ansi(text))
        except EOFError:
            pass
        return
    print(text)
    with _RawMode():
        while _read_key() not in ("enter", "esc"):
            pass


def print_wrapped(text, indent="  ", color=""):
    import textwrap
    width = max(40, term_size()[0] - 2)
    for line in textwrap.wrap(text, width=width - len(indent)) or [""]:
        print(f"{color}{indent}{line}{Style.RESET_ALL}" if color else f"{indent}{line}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_CONFIG_OVERRIDE = None      # --config
_CONFIG_URL_OVERRIDE = None  # --config-url
LOCAL_CONFIG_PATH = None     # where the local config lives / will be created
LOCAL_CONFIG_EXISTS = False
REMOTE_CFG = {}              # keys that came from the shared read-only source
CONFIG_SOURCE = ""
_REMOTE_WARNED = set()


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def config_url():
    return (_CONFIG_URL_OVERRIDE or os.environ.get("GITHUB_TOOLS_CONFIG_URL")
            or CONFIG_URL or "").strip().strip('"')


def _local_candidates():
    if _CONFIG_OVERRIDE:
        return [os.path.abspath(os.path.expanduser(_CONFIG_OVERRIDE))]
    out = []
    for p in (os.path.join(os.getcwd(), CONFIG_FILENAME),
              os.path.join(_script_dir(), CONFIG_FILENAME)):
        if p not in out:
            out.append(p)
    return out


def _run_with_timeout(fn, timeout):
    """Run fn() in a thread; an unreachable UNC path / URL can't hang startup."""
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as ex:
            box["e"] = ex

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _file_url_to_path(s):
    if not s.lower().startswith("file:"):
        return s
    p = urllib.parse.urlparse(s)
    path = urllib.request.url2pathname(p.path)
    if p.netloc and p.netloc.lower() != "localhost":
        return "\\\\" + p.netloc + path.replace("/", "\\")   # file://nas/share/x -> \\nas\share\x
    return path


def _fetch_http_json(url):
    for verify in (True, False):
        try:
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=CONFIG_FETCH_TIMEOUT,
                                        context=ctx if url.lower().startswith("https") else None) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            raise
        except Exception as ex:
            reason = getattr(ex, "reason", ex)
            if verify and (isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper()):
                continue          # self-signed NAS certificate: retry without verification
            raise
    return None


def read_shared_config(src):
    """Read the shared read-only config. Returns a dict, or None when missing /
    unreachable / invalid (never raises, never writes)."""
    s = src.strip().strip('"')
    try:
        if re.match(r"^https?://", s, re.I):
            data = _run_with_timeout(lambda: _fetch_http_json(s), CONFIG_FETCH_TIMEOUT + 2)
        else:
            path = os.path.expandvars(os.path.expanduser(_file_url_to_path(s)))

            def rd():
                p = path
                if os.path.isdir(p):
                    p = os.path.join(p, CONFIG_FILENAME)
                if not os.path.isfile(p):
                    return None
                with open(p, encoding="utf-8-sig") as f:
                    return json.load(f)

            data = _run_with_timeout(rd, CONFIG_FETCH_TIMEOUT)
        if data is None:
            log_info(f"Shared config {s} does not exist - using the local one.")
            return None
        if not isinstance(data, dict):
            log_warn(f"Shared config {s} is not a JSON object - ignored.")
            return None
        data.pop("config_url", None)          # can't redirect the source
        return data
    except Exception as ex:
        log_warn(f"Shared config {s} unreachable/invalid ({type(ex).__name__}: {ex}) "
                 f"- using the local one.")
        return None


def load_config():
    """Shared source (read-only, highest priority) over the local file."""
    global LOCAL_CONFIG_PATH, LOCAL_CONFIG_EXISTS, REMOTE_CFG, CONFIG_SOURCE
    local, LOCAL_CONFIG_PATH = {}, None
    for p in _local_candidates():
        if os.path.isfile(p):
            LOCAL_CONFIG_PATH = p
            try:
                with open(p, encoding="utf-8-sig") as f:
                    local = json.load(f)
                if not isinstance(local, dict):
                    local = {}
            except Exception as ex:
                log_warn(f"Local config {p} could not be read ({ex}) - ignored.")
                local = {}
            break
    LOCAL_CONFIG_EXISTS = LOCAL_CONFIG_PATH is not None
    if LOCAL_CONFIG_PATH is None:
        LOCAL_CONFIG_PATH = _local_candidates()[0]
    url = config_url()
    remote = read_shared_config(url) if url else None
    REMOTE_CFG = dict(remote or {})
    cfg = {**local, **REMOTE_CFG}
    parts = []
    if remote:
        parts.append(f"shared {url} (read-only)")
    if LOCAL_CONFIG_EXISTS:
        parts.append(f"local {LOCAL_CONFIG_PATH}")
    CONFIG_SOURCE = " + ".join(parts) or "none yet"
    log_info(f"Config: {CONFIG_SOURCE}.")
    return cfg


def save_config(cfg):
    """Write the local config (the shared source is never touched). Keys that
    come from the shared config are not duplicated locally."""
    global LOCAL_CONFIG_PATH, LOCAL_CONFIG_EXISTS, CONFIG_SOURCE
    for k, v in REMOTE_CFG.items():
        if cfg.get(k) != v and k not in _REMOTE_WARNED:
            _REMOTE_WARNED.add(k)
            log_warn(f"'{k}' comes from the read-only shared config and wins on the "
                     f"next start - change it there.")
    existing = {}
    if LOCAL_CONFIG_EXISTS:
        try:
            with open(LOCAL_CONFIG_PATH, encoding="utf-8-sig") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    data = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in REMOTE_CFG}
    for k in REMOTE_CFG:
        if k in existing:
            data[k] = existing[k]
    if not data and not LOCAL_CONFIG_EXISTS:
        return
    home_cfg = os.path.join(os.path.expanduser("~"), ".config", "github_tools", "config.json")
    targets = [LOCAL_CONFIG_PATH, os.path.join(_script_dir(), CONFIG_FILENAME), home_cfg]
    for target in targets:
        try:
            d = os.path.dirname(target)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(target, 0o600)
            except Exception:
                pass
            if not LOCAL_CONFIG_EXISTS or target != LOCAL_CONFIG_PATH:
                log_done(f"Config saved to {target}")
            LOCAL_CONFIG_PATH, LOCAL_CONFIG_EXISTS = target, True
            return
        except Exception:
            continue
    log_warn("Could not save the local config anywhere.")


# ---------------------------------------------------------------------------
# GitHub API client (stdlib only)
# ---------------------------------------------------------------------------
class GitHubError(RuntimeError):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects but drop the Authorization header when the host changes
    (release assets redirect to a storage host that rejects foreign auth)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlparse(req.full_url).netloc.lower()
            if urllib.parse.urlparse(newurl).netloc.lower() != old_host:
                for store in (new.headers, new.unredirected_hdrs):
                    for k in list(store):
                        if k.lower() == "authorization":
                            del store[k]
        return new


def _next_link(link):
    for part in (link or "").split(","):
        m = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def _q(ref):
    return urllib.parse.quote(str(ref), safe="/")


class GitHub:
    def __init__(self, token=None, api_url=DEFAULT_API_URL, verify=True, timeout=60):
        self.token = token
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.timeout = timeout
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                  _StripAuthRedirect())
        self.last_headers = {}

    # --- transport ---------------------------------------------------------
    def _url(self, path, params=None):
        url = path if path.startswith("http") else self.api_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(self, method, url, data=None, headers=None, stream=False, timeout=None):
        """Returns (status, headers_lowercase, body_bytes | response when stream).
        Retries transient errors and waits out rate limits."""
        h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(headers or {})
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                resp = self.opener.open(req, timeout=timeout or self.timeout)
                hd = {k.lower(): v for k, v in resp.headers.items()}
                if stream:
                    return resp.status, hd, resp
                body = resp.read()
                resp.close()
                return resp.status, hd, body
            except urllib.error.HTTPError as ex:
                try:
                    body = ex.read()
                except Exception:
                    body = b""
                hd = {k.lower(): v for k, v in (ex.headers or {}).items()}
                if ex.code in (403, 429) and self._rate_wait(hd, body, attempt):
                    continue
                if ex.code in (500, 502, 503, 504) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return ex.code, hd, body
            except (urllib.error.URLError, OSError) as ex:
                reason = getattr(ex, "reason", ex)
                if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
                    raise GitHubError(0, f"TLS certificate problem: {reason} (try --insecure "
                                         f"for a self-hosted GitHub Enterprise)")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise GitHubError(0, f"Connection failed: {reason}")
        raise GitHubError(0, "Giving up after repeated rate-limit / server errors.")

    def _rate_wait(self, hd, body, attempt):
        text = body.decode("utf-8", "replace").lower() if body else ""
        wait = None
        if hd.get("retry-after", "").isdigit():
            wait = int(hd["retry-after"])
        elif hd.get("x-ratelimit-remaining") == "0" and hd.get("x-ratelimit-reset", "").isdigit():
            wait = int(hd["x-ratelimit-reset"]) - int(time.time()) + 2
            if wait > 15 * 60:
                return False        # primary limit far away - report it instead
        elif "secondary rate limit" in text or "abuse" in text:
            wait = 60 * (attempt + 1)
        if wait is None or attempt >= 5:
            return False
        wait = max(1, wait)
        for left in range(wait, 0, -1):
            sys.stdout.write(f"\r  {Fore.YELLOW}GitHub rate limit - waiting {left}s…"
                             f"{Style.RESET_ALL}\x1b[K")
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r\x1b[K")
        return True

    @staticmethod
    def _message(st, body):
        try:
            j = json.loads(body.decode("utf-8", "replace"))
            msg = j.get("message") or ""
            errs = j.get("errors") or []
            det = "; ".join((e.get("message") or e.get("code") or str(e)) if isinstance(e, dict)
                            else str(e) for e in errs)
            return f"{msg}" + (f" ({det})" if det else "")
        except Exception:
            return (body or b"").decode("utf-8", "replace")[:200] or f"HTTP {st}"

    def api(self, method, path, body=None, params=None, headers=None):
        url = self._url(path, params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = dict(headers or {})
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        st, hd, raw = self.request(method, url, data=data, headers=h)
        self.last_headers = hd
        if st >= 400:
            raise GitHubError(st, f"HTTP {st}: {self._message(st, raw)}", hd)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise GitHubError(st, f"Unexpected (non-JSON) answer from {url.split('?')[0]}")

    def paged(self, path, params=None, limit=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url, out = self._url(path, params), []
        while url:
            st, hd, raw = self.request("GET", url)
            if st >= 400:
                raise GitHubError(st, f"HTTP {st}: {self._message(st, raw)}", hd)
            items = json.loads(raw.decode("utf-8") or "[]")
            if isinstance(items, dict):        # e.g. {"total_count":..,"repositories":[..]}
                items = next((v for v in items.values() if isinstance(v, list)), [])
            out += items
            if limit and len(out) >= limit:
                return out[:limit]
            url = _next_link(hd.get("link"))
        return out

    # --- account / repositories -------------------------------------------
    def user(self):
        return self.api("GET", "/user")

    def orgs(self):
        try:
            return self.paged("/user/orgs")
        except GitHubError:
            return []

    def repos(self):
        return self.paged("/user/repos", {"affiliation": "owner,collaborator,organization_member",
                                          "sort": "full_name"})

    def repo(self, full):
        return self.api("GET", f"/repos/{full}")

    def create_repo(self, name, private=True, description="", org=None):
        body = {"name": name, "private": bool(private), "description": description or "",
                "auto_init": False}
        return self.api("POST", f"/orgs/{org}/repos" if org else "/user/repos", body)

    def branches(self, full):
        return self.paged(f"/repos/{full}/branches")

    def tags(self, full, limit=200):
        return self.paged(f"/repos/{full}/tags", limit=limit)

    def releases(self, full, limit=100):
        return self.paged(f"/repos/{full}/releases", limit=limit)

    def release_by_tag(self, full, tag):
        if tag == "latest":
            return self.api("GET", f"/repos/{full}/releases/latest")
        return self.api("GET", f"/repos/{full}/releases/tags/{_q(tag)}")

    # --- git data ----------------------------------------------------------
    def ref_sha(self, full, branch):
        """Commit SHA a branch points to; None if the branch (or any commit) is missing."""
        try:
            j = self.api("GET", f"/repos/{full}/git/ref/heads/{_q(branch)}")
        except GitHubError as ex:
            if ex.status in (404, 409):
                return None
            raise
        if isinstance(j, list):                 # older servers: prefix match
            j = next((r for r in j if r.get("ref") == f"refs/heads/{branch}"), None)
            if not j:
                return None
        return (j.get("object") or {}).get("sha")

    def commit_tree(self, full, commit_sha):
        return self.api("GET", f"/repos/{full}/git/commits/{commit_sha}")["tree"]["sha"]

    def tree_map(self, full, tree_sha):
        """{path: {'sha','mode','type'}} of all files (blobs, symlinks, submodules)."""
        out = {}
        j = self.api("GET", f"/repos/{full}/git/trees/{tree_sha}", params={"recursive": 1})
        if not j.get("truncated"):
            for e in j.get("tree", []):
                if e.get("type") != "tree":
                    out[e["path"]] = {"sha": e["sha"], "mode": e["mode"], "type": e["type"]}
            return out

        def walk(sha, prefix):                  # very large repo: walk level by level
            d = self.api("GET", f"/repos/{full}/git/trees/{sha}")
            for e in d.get("tree", []):
                p = prefix + e["path"]
                if e["type"] == "tree":
                    walk(e["sha"], p + "/")
                else:
                    out[p] = {"sha": e["sha"], "mode": e["mode"], "type": e["type"]}

        walk(tree_sha, "")
        return out

    def create_blob(self, full, data):
        return self.api("POST", f"/repos/{full}/git/blobs",
                        {"content": base64.b64encode(data).decode("ascii"),
                         "encoding": "base64"})["sha"]

    def create_tree(self, full, entries, base_tree=None):
        body = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        return self.api("POST", f"/repos/{full}/git/trees", body)["sha"]

    def create_commit(self, full, message, tree, parents):
        return self.api("POST", f"/repos/{full}/git/commits",
                        {"message": message, "tree": tree, "parents": parents})

    def update_branch(self, full, branch, sha, force=False):
        return self.api("PATCH", f"/repos/{full}/git/refs/heads/{_q(branch)}",
                        {"sha": sha, "force": bool(force)})

    def create_ref(self, full, ref, sha):
        return self.api("POST", f"/repos/{full}/git/refs", {"ref": ref, "sha": sha})

    def put_contents(self, full, path, data, message, branch=None):
        body = {"message": message, "content": base64.b64encode(data).decode("ascii")}
        if branch:
            body["branch"] = branch
        return self.api("PUT", f"/repos/{full}/contents/{_q(path)}", body)

    def create_release(self, full, tag, target, name=None, body="", draft=False, prerelease=False):
        return self.api("POST", f"/repos/{full}/releases",
                        {"tag_name": tag, "target_commitish": target, "name": name or tag,
                         "body": body or "", "draft": bool(draft), "prerelease": bool(prerelease)})

    def upload_asset(self, release, file_path):
        url = re.sub(r"\{.*\}$", "", release["upload_url"])
        name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            data = f.read()
        st, hd, raw = self.request("POST", url + "?" + urllib.parse.urlencode({"name": name}),
                                   data=data, headers={"Content-Type": "application/octet-stream"},
                                   timeout=max(120, len(data) // (256 * 1024)))
        if st >= 400:
            raise GitHubError(st, f"HTTP {st}: {self._message(st, raw)}", hd)
        return json.loads(raw.decode("utf-8"))

    # --- downloads ---------------------------------------------------------
    def download(self, url, dest_path, accept=None, label=""):
        """Stream a URL to a file with a progress bar. Returns the byte count."""
        st, hd, resp = self.request("GET", url, headers={"Accept": accept} if accept else None,
                                    stream=True, timeout=120)
        if st >= 400:
            raise GitHubError(st, f"HTTP {st}: {self._message(st, resp)}", hd)
        total = int(hd.get("content-length") or 0)
        done = 0
        tmp = dest_path + ".part"
        try:
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    show_progress(done, total, f"{label}  {human_size(done)}")
            resp.close()
            os.replace(tmp, dest_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        sys.stdout.write("\n")
        return done


def web_base(api_url):
    """https://api.github.com -> https://github.com; https://ghe/api/v3 -> https://ghe."""
    u = (api_url or DEFAULT_API_URL).rstrip("/")
    if u.lower() == "https://api.github.com":
        return "https://github.com"
    m = re.match(r"^(https?://[^/]+)/api/v3$", u, re.I)
    if m:
        return m.group(1)
    return re.sub(r"//api\.", "//", u)


def parse_repo_ref(s):
    """'owner/name', 'https://github.com/owner/name(.git)' or 'git@host:owner/name.git'."""
    s = (s or "").strip().strip('"')
    m = re.match(r"^(?:https?://[^/]+/|git@[^:]+:)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", s)
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
SCOPES = "repo read:org workflow"


def _token_login(api_url):
    web = web_base(api_url)
    url = (f"{web}/settings/tokens/new?scopes=repo,read:org,workflow"
           f"&description=github_tools")
    clear_screen()
    log_info("Sign in with a personal access token.")
    print()
    print_wrapped("Create one here (classic token, scopes repo + read:org + workflow; "
                  "a fine-grained token works too - give it Contents: read & write, "
                  "Metadata: read, and Workflows if you push .github/workflows):")
    print(f"    {Fore.CYAN}{url}{Style.RESET_ALL}")
    print()
    if can_ask():
        ans = ask_yes_back("Open that page in your browser now?", default=False)
        if ans is None:
            return None
        if ans:
            webbrowser.open(url)
    return ask_line("Token (shown as *)", secret=True)


def _device_login(cfg, api_url):
    """Username + password + OTP via GitHub's device flow (sign-in on github.com)."""
    web = web_base(api_url)
    clear_screen()
    log_info("Sign in with username + password (+ 2FA / OTP).")
    print()
    print_wrapped("GitHub no longer accepts passwords in its API or in git, so the "
                  "password is never typed here. Instead you sign in on github.com "
                  "itself (username, password, and the OTP code if 2FA is enabled) and "
                  "approve this tool - it then receives a token.", color=Style.DIM)
    print()
    client_id = cfg.get("oauth_client_id") or OAUTH_CLIENT_ID
    if not client_id:
        print_wrapped("This needs the Client ID of your own OAuth App (one-time setup, "
                      "free):")
        print(f"    1) {Fore.CYAN}{web}/settings/applications/new{Style.RESET_ALL}")
        print("    2) Name: github_tools · Homepage URL and Callback URL: any, e.g. "
              f"{web}")
        print("    3) Register application, tick 'Enable Device Flow', Update application")
        print("    4) Copy the 'Client ID' and paste it below")
        print()
        client_id = ask_line("OAuth App Client ID")
        if not client_id:
            return None
        cfg["oauth_client_id"] = client_id
    gh = GitHub(None, api_url)
    form = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    st, _, raw = gh.request("POST", f"{web}/login/device/code",
                            data=urllib.parse.urlencode({"client_id": client_id,
                                                         "scope": SCOPES}).encode(),
                            headers=form)
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:
        j = {}
    if st >= 400 or "device_code" not in j:
        err = j.get("error_description") or j.get("error") or f"HTTP {st}"
        log_err(f"GitHub refused the device sign-in: {err}")
        if "client" in str(err).lower() or st == 404:
            log_info("Check the Client ID (and that 'Enable Device Flow' is ticked).")
            cfg.pop("oauth_client_id", None)
        return None
    code, verify_url = j["user_code"], j.get("verification_uri") or f"{web}/login/device"
    interval = int(j.get("interval") or 5)
    deadline = time.time() + int(j.get("expires_in") or 900)
    copied = copy_to_clipboard(code)
    print(f"{Fore.MAGENTA}==============================================={Style.RESET_ALL}")
    print(f"  1) Open:        {Fore.CYAN}{verify_url}{Style.RESET_ALL}")
    print(f"  2) Enter code:  {Fore.GREEN}{Style.BRIGHT}{code}{Style.RESET_ALL}"
          + (f"   {Style.DIM}(copied to clipboard){Style.RESET_ALL}" if copied else ""))
    print("  3) Sign in (username, password, OTP) and click Authorize")
    print(f"{Fore.MAGENTA}==============================================={Style.RESET_ALL}")
    try:
        webbrowser.open(verify_url)
    except Exception:
        pass
    print(f"Waiting for confirmation {Style.DIM}(Esc = cancel){Style.RESET_ALL}", end="", flush=True)
    while time.time() < deadline:
        if _tui_supported():
            with _RawMode():
                end = time.monotonic() + interval
                while time.monotonic() < end:
                    if _read_key(max(0.05, end - time.monotonic())) == "esc":
                        print()
                        return None
        else:
            time.sleep(interval)
        print(".", end="", flush=True)
        st, _, raw = gh.request("POST", f"{web}/login/oauth/access_token",
                                data=urllib.parse.urlencode({
                                    "client_id": client_id, "device_code": j["device_code"],
                                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
                                }).encode(), headers=form)
        try:
            r = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if r.get("access_token"):
            print()
            return r["access_token"]
        err = r.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(r.get("interval") or interval + 5)
            continue
        print()
        msg = {"expired_token": "The code expired - try again.",
               "access_denied": "Access was denied on github.com.",
               "device_flow_disabled": "Device Flow is not enabled on the OAuth App "
                                       "(tick 'Enable Device Flow')."}.get(err, r.get("error_description") or err)
        log_err(msg)
        return None
    print()
    log_err("Sign-in timed out.")
    return None


def interactive_login(cfg, api_url):
    """Ask how to connect and obtain a verified token. Returns (token, user)."""
    while True:
        rows = [
            {"label": "Personal access token  (classic or fine-grained)", "m": "token"},
            {"label": "Username + password (+ 2FA / OTP)  —  sign in on github.com", "m": "device"},
        ]
        default = 1 if cfg.get("auth_method") == "device" else 0
        i = select_menu("How do you want to connect to GitHub?", rows, default=default,
                        header=[f"{Fore.MAGENTA}{Style.BRIGHT}github_tools{Style.RESET_ALL}  ·  "
                                f"sign-in to {web_base(api_url)}", ""])
        if i is None:
            return None, None
        method = rows[i]["m"]
        token = _token_login(api_url) if method == "token" else _device_login(cfg, api_url)
        if not token:
            continue
        try:
            gh = GitHub(token, api_url)
            me = gh.user()
        except GitHubError as ex:
            log_err(f"The token does not work: {ex}")
            pause()
            continue
        log_done(f"Signed in as {Fore.GREEN}{me.get('login')}{Style.RESET_ALL}"
                 + (f" ({me['name']})" if me.get("name") else ""))
        cfg["auth_method"] = method
        cfg["login"] = me.get("login")
        cfg["api_url"] = api_url
        store = ask_yes_back("Save the token in the config file (plain text) so you are "
                             "not asked next time?", default=True)
        if store:
            cfg["token"] = token
        else:
            cfg.pop("token", None)
        save_config(cfg)
        return token, me


def first_run_wizard(cfg):
    clear_screen()
    print(f"{Fore.MAGENTA}{Style.BRIGHT}github_tools - first start{Style.RESET_ALL}")
    print()
    print_wrapped(f"No configuration was found. A new one will be created here: "
                  f"{LOCAL_CONFIG_PATH}", color=Style.DIM)
    print()
    i = select_menu("Which GitHub do you use?",
                    ["github.com", "GitHub Enterprise Server (own host)"], allow_back=False)
    if i == 1:
        host = ask_line("Enterprise host or API URL (e.g. https://github.example.com)")
        if not host:
            die("No host given.")
        host = host.rstrip("/")
        if not re.match(r"^https?://", host, re.I):
            host = "https://" + host
        cfg["api_url"] = host if host.endswith("/api/v3") else host + "/api/v3"
    else:
        cfg["api_url"] = DEFAULT_API_URL


def connect(args, cfg):
    """Return (GitHub client, user dict). Token sources: --token, env
    GITHUB_TOKEN / GH_TOKEN, config; otherwise interactive sign-in."""
    nothing = not cfg
    if nothing and can_ask() and not args.token and not os.environ.get("GITHUB_TOKEN"):
        first_run_wizard(cfg)
    api_url = (args.api_url or cfg.get("api_url") or DEFAULT_API_URL).rstrip("/")
    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or cfg.get("token")
    if token:
        gh = GitHub(token, api_url, verify=not args.insecure)
        try:
            me = gh.user()
            scopes = gh.last_headers.get("x-oauth-scopes")
            if scopes is not None and scopes.strip() and not re.search(r"\b(public_)?repo\b", scopes):
                log_warn(f"The token has no 'repo' scope (scopes: {scopes}) - uploads may fail.")
            return gh, me
        except GitHubError as ex:
            if ex.status != 401:
                die(f"GitHub unreachable: {ex}")
            log_warn("The saved / given token is not valid any more.")
            if not can_ask():
                die("Sign-in failed (HTTP 401). Pass a valid --token.")
    elif not can_ask():
        die("No token. Use --token, the GITHUB_TOKEN variable or run interactively once.")
    token, me = interactive_login(cfg, api_url)
    if not token:
        die("Not signed in.")
    return GitHub(token, api_url, verify=not args.insecure), me


# ---------------------------------------------------------------------------
# Local folder scanning (.gitignore aware)
# ---------------------------------------------------------------------------
DEFAULT_EXCLUDES = [".git/", ".svn/", ".hg/", "__pycache__/", "*.pyc", "Thumbs.db",
                    "desktop.ini", ".DS_Store", "*.part", CONFIG_FILENAME]


class IgnoreRules:
    """Practical subset of .gitignore semantics: comments, '!' negation, trailing
    '/' (directories only), leading '/' or an inner '/' (anchored), '*', '?',
    '[..]' and '**'. Rules of nested .gitignore files apply to their subtree."""

    def __init__(self):
        self.rules = []

    def add(self, pattern, base=""):
        p = pattern.rstrip("\n").rstrip("\r")
        if not p.strip() or p.lstrip().startswith("#"):
            return
        p = p.strip()
        neg = p.startswith("!")
        if neg:
            p = p[1:]
        dir_only = p.endswith("/")
        p = p.rstrip("/")
        if not p:
            return
        anchored = "/" in p
        p = p.lstrip("/")
        if p.startswith("**/"):
            p, anchored = p[3:], "/" in p[3:]
        if _WINDOWS:
            p = p.lower()
        self.rules.append((base, p, neg, dir_only, anchored))

    def add_file(self, path, base=""):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    self.add(line, base)
        except Exception:
            pass

    @staticmethod
    def _m(s, pat):
        if fnmatch.fnmatchcase(s, pat):
            return True
        if "**/" in pat and fnmatch.fnmatchcase(s, pat.replace("**/", "")):
            return True
        if pat.endswith("/**") and (s == pat[:-3] or s.startswith(pat[:-3] + "/")):
            return True
        return False

    def ignored(self, rel, is_dir):
        r = rel.lower() if _WINDOWS else rel
        res = False
        for base, pat, neg, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            if base:
                if not r.startswith(base + "/"):
                    continue
                sub = r[len(base) + 1:]
            else:
                sub = r
            target = sub if anchored else sub.rsplit("/", 1)[-1]
            if self._m(target, pat):
                res = not neg
        return res


class LocalFile:
    __slots__ = ("rel", "abs", "size", "sha", "mode")

    def __init__(self, rel, abs_, size, sha, mode):
        self.rel, self.abs, self.size, self.sha, self.mode = rel, abs_, size, sha, mode


def git_blob_sha(path):
    """SHA-1 of the file as a git blob ('blob <size>\\0' + content)."""
    size = os.path.getsize(path)
    h = hashlib.sha1(f"blob {size}\0".encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_local(root, extra_excludes=(), use_gitignore=True, quiet=False):
    """Walk the folder. Returns (files, skipped_list)."""
    rules = IgnoreRules()
    for p in DEFAULT_EXCLUDES:
        rules.add(p)
    for p in extra_excludes or []:
        rules.add(p)
    cfg_abs = os.path.abspath(LOCAL_CONFIG_PATH) if LOCAL_CONFIG_PATH else None
    files, skipped = [], []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if use_gitignore and ".gitignore" in filenames:
            rules.add_file(os.path.join(dirpath, ".gitignore"),
                           rel_dir.lower() if _WINDOWS else rel_dir)
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if os.path.islink(os.path.join(dirpath, d)) or rules.ignored(rel, True):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            ab = os.path.join(dirpath, fn)
            if rules.ignored(rel, False) or (cfg_abs and os.path.abspath(ab) == cfg_abs):
                continue
            try:
                if not os.path.isfile(ab):
                    continue
                size = os.path.getsize(ab)
                if size > MAX_FILE_SIZE:
                    skipped.append((rel, f"{human_size(size)} - over GitHub's 100 MB limit"))
                    continue
                mode = "100755" if (not _WINDOWS and os.access(ab, os.X_OK)) else "100644"
                files.append(LocalFile(rel, ab, size, git_blob_sha(ab), mode))
            except OSError as ex:
                skipped.append((rel, str(ex)))
            count += 1
            if not quiet and count % 50 == 0:
                sys.stdout.write(f"\r  {Fore.CYAN}scanning… {count} files{Style.RESET_ALL}\x1b[K")
                sys.stdout.flush()
    if not quiet and count >= 50:
        sys.stdout.write("\r\x1b[K")
    return files, skipped


def build_plan(files, remote, mirror):
    """Compare the folder with the remote tree."""
    local_set = {f.rel for f in files}
    new, modified, unchanged = [], [], []
    for f in files:
        r = remote.get(f.rel)
        if r is None or r["type"] != "blob":
            new.append(f)
        elif r["sha"] != f.sha:
            modified.append(f)
        else:
            unchanged.append(f)
    deleted = sorted(p for p, r in remote.items()
                     if p not in local_set and r["type"] != "commit") if mirror else []
    return {"new": new, "modified": modified, "unchanged": unchanged, "deleted": deleted,
            "mirror": mirror}


def plan_counts(plan):
    return len(plan["new"]), len(plan["modified"]), len(plan["deleted"])


def _inline_text(path, size):
    if size > INLINE_TEXT_LIMIT:
        return None
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _file_mode(f, remote_entry):
    if remote_entry and remote_entry.get("mode") in ("100644", "100755") and _WINDOWS:
        return remote_entry["mode"]          # Windows has no exec bit - keep the remote one
    return f.mode


# ---------------------------------------------------------------------------
# Upload / update
# ---------------------------------------------------------------------------
_STATE = {}


def pick_repo(gh, me, prompt, allow_create=False, push_only=False):
    """Repository picker. Returns a repo dict, 'create', or None (back)."""
    clear_screen()
    log_info("Loading your repositories…")
    repos = gh.repos()
    if push_only:
        repos = [r for r in repos if (r.get("permissions") or {}).get("push")]
    repos.sort(key=lambda r: r["full_name"].lower())
    rows = []
    if allow_create:
        rows.append({"label": "» Create a new repository…", "act": "create", "color": Fore.GREEN})
    rows.append({"label": "» Enter owner/name or URL manually…", "act": "manual", "color": Fore.CYAN})
    rows.append({"header": True, "label": f"Repositories ({len(repos)})"})
    for r in repos:
        vis = (f"{Fore.YELLOW}private{Style.RESET_ALL}" if r.get("private")
               else f"{Fore.GREEN}public {Style.RESET_ALL}")
        extra = " · archived" if r.get("archived") else ""
        rows.append({"label": r["full_name"] + (f"  —  {r['description']}" if r.get("description") else ""),
                     "tag": f"{vis} {Style.DIM}{short_date(r.get('pushed_at'))}{extra}{Style.RESET_ALL}",
                     "repo": r})
    cur = _STATE.setdefault("repo_cursor:" + prompt, [len(rows) and (3 if allow_create else 2)])
    while True:
        i = select_menu(prompt, rows, default=cur[0], cursor_out=cur,
                        header=[f"{Fore.CYAN}Account:{Style.RESET_ALL} {me.get('login')}", ""])
        if i is None:
            return None
        act = rows[i].get("act")
        if act == "create":
            return "create"
        if act == "manual":
            s = ask_line("Repository (owner/name or URL)")
            if not s:
                continue
            full = parse_repo_ref(s)
            if not full:
                log_warn("That does not look like owner/name.")
                pause()
                continue
            try:
                return gh.repo(full)
            except GitHubError as ex:
                log_warn(f"{full}: {ex}")
                pause()
                continue
        return rows[i]["repo"]


def sanitize_repo_name(name):
    n = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.")
    return n or "new-repository"


def create_repo_interactive(gh, me, default_name):
    owners = [{"label": f"{me['login']}  (you)", "org": None}]
    for o in gh.orgs():
        owners.append({"label": o["login"], "org": o["login"]})
    org = None
    if len(owners) > 1:
        i = select_menu("Owner of the new repository:", owners)
        if i is None:
            return None
        org = owners[i]["org"]
    name = ask_line("Repository name", default=sanitize_repo_name(default_name))
    if not name:
        return None
    v = select_menu("Visibility:", ["Private", "Public"])
    if v is None:
        return None
    desc = ask_line("Description (optional)", allow_empty=True)
    if desc is None:
        return None
    full = f"{org or me['login']}/{name}"
    log_info(f"Creating {full}…")
    repo = gh.create_repo(name, private=(v == 0), description=desc, org=org)
    log_done(f"Repository {repo['full_name']} created ({repo['html_url']}).")
    return repo


def pick_upload_branch(gh, repo):
    """Returns (branch, is_new, base_branch_or_None) or None."""
    full = repo["full_name"]
    branches = gh.branches(full)
    default = repo.get("default_branch") or "main"
    if not branches:
        name = ask_line("The repository is empty. Branch to create", default=default)
        return (name, True, None) if name else None
    names = sorted((b["name"] for b in branches), key=lambda n: (n != default, n.lower()))
    rows = [{"label": "» New branch…", "act": "new", "color": Fore.GREEN},
            {"header": True, "label": f"Branches ({len(names)})"}]
    for n in names:
        rows.append({"label": n, "branch": n,
                     "tag": f"{Fore.CYAN}default{Style.RESET_ALL}" if n == default else ""})
    i = select_menu(f"Upload into which branch of {full}?", rows, default=2)
    if i is None:
        return None
    if rows[i].get("act") != "new":
        return rows[i]["branch"], False, None
    name = ask_line("New branch name")
    if not name:
        return None
    if name in names:
        log_info(f"Branch '{name}' already exists - using it.")
        return name, False, None
    brows = [{"label": f"from '{n}'", "base": n} for n in names]
    brows.append({"label": "empty branch (no history)", "base": None})
    j = select_menu(f"Start '{name}' from:", brows)
    if j is None:
        return None
    return name, True, brows[j]["base"]


def suggest_next_tag(tags):
    best = None
    for t in tags:
        m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)$", t.get("name", ""))
        if m:
            key = tuple(int(x) for x in m.groups()[1:])
            if best is None or key > best[1]:
                best = (m.group(1), key)
    if not best:
        return "v1.0.0"
    p, (a, b, c) = best
    return f"{p}{a}.{b}.{c + 1}"


def upload_flow(gh, me, args, cfg, path=None):
    """Upload / update a local folder. Returns True if a commit was made."""
    # 1) source folder
    root = path or args.path
    if not root:
        if not can_ask():
            die("Give the folder to upload.")
        root = ask_line("Folder to upload", default=os.getcwd())
        if not root:
            return False
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(root.strip('"'))))
    if not os.path.isdir(root):
        log_err(f"Not a folder: {root}")
        return False
    clear_screen()
    log_info(f"Scanning {Fore.CYAN}{root}{Style.RESET_ALL}…")
    excludes = list(cfg.get("excludes") or []) + list(args.exclude or [])
    files, skipped = scan_local(root, excludes, use_gitignore=not args.no_gitignore)
    total = sum(f.size for f in files)
    log_info(f"{len(files)} file(s), {human_size(total)}"
             + (f", {len(skipped)} skipped" if skipped else ""))
    for rel, why in skipped[:10]:
        log_warn(f"skipped {rel}: {why}")
    if not files:
        log_warn("Nothing to upload in that folder.")
        return False

    # 2) target repository
    repo = None
    if args.repo:
        full = parse_repo_ref(args.repo)
        if not full:
            die(f"Invalid --repo '{args.repo}' (use owner/name).")
        try:
            repo = gh.repo(full)
        except GitHubError as ex:
            if ex.status != 404:
                raise
            owner, name = full.split("/", 1)
            create = args.create or (can_ask() and ask_yes_back(
                f"{full} does not exist. Create it?", default=True))
            if not create:
                die(f"Repository {full} not found (add --create to create it).")
            org = None if owner.lower() == me["login"].lower() else owner
            repo = gh.create_repo(name, private=not args.public, description=args.description or "",
                                  org=org)
            log_done(f"Repository {repo['full_name']} created.")
    else:
        if not can_ask():
            die("Give the target repository with --repo owner/name.")
        while repo is None:
            r = pick_repo(gh, me, "Upload into which repository?", allow_create=True, push_only=True)
            if r is None:
                return False
            if r == "create":
                repo = create_repo_interactive(gh, me, os.path.basename(root))
                continue
            repo = r
    full = repo["full_name"]
    if not (repo.get("permissions") or {}).get("push", True):
        die(f"You have no push permission for {full}.")

    # 3) branch
    branch_names = [b["name"] for b in gh.branches(full)]
    empty_repo = not branch_names
    base = None
    if args.branch:
        branch = args.branch
        is_new = branch not in branch_names
        if is_new and not empty_repo:
            base = None if args.orphan else (args.base or repo.get("default_branch"))
    elif can_ask():
        res = pick_upload_branch(gh, repo)
        if res is None:
            return False
        branch, is_new, base = res
    else:
        branch = repo.get("default_branch") or "main"
        is_new = branch not in branch_names

    # 4) remote state
    clear_screen()
    log_info(f"Reading {full} @ {branch}…")
    parent = None
    if empty_repo:
        remote = {}
    else:
        src = branch if not is_new else base
        parent = gh.ref_sha(full, src) if src else None
        if src and not parent:
            die(f"Branch '{src}' not found in {full}.")
        remote = gh.tree_map(full, gh.commit_tree(full, parent)) if parent else {}
    base_tree = gh.commit_tree(full, parent) if parent else None

    plan_u = build_plan(files, remote, mirror=False)
    plan_m = build_plan(files, remote, mirror=True)

    # 5) mode
    if args.mode:
        mirror = args.mode == "mirror"
    elif not remote:
        mirror = False
    elif can_ask():
        nu, mu, _ = plan_counts(plan_u)
        nm, mm, dm = plan_counts(plan_m)
        rows = [
            {"label": "Update  —  add new + overwrite changed files, keep other remote files",
             "tag": f"{Fore.GREEN}+{nu}{Style.RESET_ALL} {Fore.YELLOW}~{mu}{Style.RESET_ALL}"},
            {"label": "Mirror  —  make the branch exactly match the folder (deletes extra files)",
             "tag": f"{Fore.GREEN}+{nm}{Style.RESET_ALL} {Fore.YELLOW}~{mm}{Style.RESET_ALL} "
                    f"{Fore.RED}-{dm}{Style.RESET_ALL}"},
        ]
        i = select_menu("Upload mode:", rows,
                        header=[f"{Fore.CYAN}Folder:{Style.RESET_ALL} {root}",
                                f"{Fore.CYAN}Target:{Style.RESET_ALL} {full} @ {branch}"
                                + (" (new branch)" if is_new else ""), ""])
        if i is None:
            return False
        mirror = i == 1
    else:
        mirror = False
    plan = plan_m if mirror else plan_u
    n_new, n_mod, n_del = plan_counts(plan)
    if not (n_new or n_mod or n_del) and not is_new:
        clear_screen()
        log_done(f"{full} @ {branch} already matches the folder - nothing to upload.")
        return False

    # 6) commit message
    default_msg = (f"Update from {os.path.basename(root) or root} "
                   f"({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})")
    msg = args.message
    if not msg:
        msg = ask_line("Commit message", default=default_msg) if can_ask() else default_msg
        if not msg:
            return False

    # 7) tag / release
    tag = args.tag
    release = bool(args.release)
    if args.release:
        tag = args.release
    rel_name, rel_notes = args.release_name, args.notes or ""
    assets = list(args.asset_file or [])
    for a in assets:
        if not os.path.isfile(a):
            die(f"--asset-file not found: {a}")
    if not tag and can_ask() and not args.message:
        i = select_menu("Mark this version?", ["No tag", "Create a tag", "Create a release (tag + release page)"])
        if i is None:
            return False
        if i:
            try:
                sug = suggest_next_tag(gh.tags(full, limit=100)) if not empty_repo else "v1.0.0"
            except GitHubError:
                sug = "v1.0.0"
            tag = ask_line("Tag name", default=sug)
            if not tag:
                return False
            release = i == 2
            if release:
                rel_name = ask_line("Release title", default=tag) or tag
                rel_notes = ask_line("Release notes (optional, one line)", allow_empty=True) or ""
                ap = ask_line("Files to attach (optional; separate with ';')", allow_empty=True) or ""
                for a in [x.strip().strip('"') for x in ap.split(";") if x.strip()]:
                    if os.path.isfile(a):
                        assets.append(a)
                    else:
                        log_warn(f"not a file, ignored: {a}")

    # 8) summary
    clear_screen()
    log_info(f"Upload {Fore.CYAN}{root}{Style.RESET_ALL}")
    print(f"    to:      {full} @ {branch}" + (" (new branch" + (f" from {base}" if base else
                                                                  (", empty" if not empty_repo else ""))
                                                + ")" if is_new else ""))
    print(f"    mode:    {'mirror' if mirror else 'update'}")
    print(f"    changes: {Fore.GREEN}+{n_new} new{Style.RESET_ALL}, "
          f"{Fore.YELLOW}~{n_mod} modified{Style.RESET_ALL}, "
          f"{Fore.RED}-{n_del} deleted{Style.RESET_ALL}, {len(plan['unchanged'])} unchanged")
    print(f"    message: {msg}")
    if tag:
        print(f"    {'release' if release else 'tag'}:  {tag}" + (f"  ({len(assets)} file(s) attached)" if assets else ""))
    print()
    lines = ([f"  {Fore.GREEN}+ {f.rel}{Style.RESET_ALL}" for f in plan["new"]]
             + [f"  {Fore.YELLOW}~ {f.rel}{Style.RESET_ALL}" for f in plan["modified"]]
             + [f"  {Fore.RED}- {p}{Style.RESET_ALL}" for p in plan["deleted"]])
    for ln in lines[:25]:
        print(ln)
    if len(lines) > 25:
        print(f"  … (+{len(lines) - 25} more)")
    print()
    if args.dry_run:
        log_info("Dry-run: nothing was uploaded.")
        return False
    if can_ask():
        ans = ask_yes_back("Upload now?", default=True)
        if not ans:
            log_info("Cancelled.")
            return False

    # 9) go
    force = args.force
    if empty_repo:
        log_info("Initializing the empty repository…")
        gh.put_contents(full, ".github_tools_init", b"init\n", "Initialize repository", branch=branch)
        parent, base_tree, force = None, None, True     # our commit replaces the bootstrap
        branch_names = [b["name"] for b in gh.branches(full)]
        is_new = branch not in branch_names

    changed = plan["new"] + plan["modified"]
    entries, to_blob = [], []
    log_info(f"Preparing {len(changed)} changed file(s)…")
    for f in changed:
        text = _inline_text(f.abs, f.size)
        entry = {"path": f.rel, "mode": _file_mode(f, remote.get(f.rel)), "type": "blob"}
        if text is not None:
            entry["content"] = text
        else:
            to_blob.append((f, entry))
        entries.append(entry)
    if to_blob:
        done, lock = [0], threading.Lock()

        def up(item):
            f, entry = item
            with open(f.abs, "rb") as fh:
                entry["sha"] = gh.create_blob(full, fh.read())
            with lock:
                done[0] += 1
                show_progress(done[0], len(to_blob), f"uploading {f.rel}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
            for fut in [ex.submit(up, it) for it in to_blob]:
                fut.result()
        sys.stdout.write("\n")
    if mirror or not base_tree:
        # full tree: unchanged files by their (already existing) blob SHA
        for f in plan["unchanged"]:
            entries.append({"path": f.rel, "mode": _file_mode(f, remote.get(f.rel)),
                            "type": "blob", "sha": f.sha})
        for p, r in remote.items():
            if r["type"] == "commit" and p not in {f.rel for f in files}:
                entries.append({"path": p, "mode": r["mode"], "type": "commit", "sha": r["sha"]})
        tree_base = None
    else:
        tree_base = base_tree
    # trees in chunks (keeps each request small), chained via base_tree
    tree = tree_base
    chunk, size, n_chunks = [], 0, 0
    total_chunks = max(1, (len(entries) + 499) // 500)
    for e in entries + [None]:
        if e is not None:
            chunk.append(e)
            size += len(e.get("content") or "") + 200
        if chunk and (e is None or len(chunk) >= 500 or size > 6 * 1024 * 1024):
            tree = gh.create_tree(full, chunk, base_tree=tree)
            n_chunks += 1
            show_progress(n_chunks, max(total_chunks, n_chunks), "building tree")
            chunk, size = [], 0
    sys.stdout.write("\n")
    if tree is None:
        die("Nothing to commit.")
    if base_tree and tree == base_tree and not is_new:
        log_done("The result is identical to the branch - no commit needed.")
        return False
    commit = gh.create_commit(full, msg, tree, [parent] if parent else [])
    csha = commit["sha"]
    try:
        if is_new:
            gh.create_ref(full, f"refs/heads/{branch}", csha)
        else:
            gh.update_branch(full, branch, csha, force=force)
    except GitHubError as ex:
        if ex.status == 422:
            die(f"The branch moved on GitHub meanwhile (or it is protected): {ex}. "
                f"Run again, or use --force.")
        raise
    web = web_base(gh.api_url)
    log_done(f"Commit {csha[:7]} pushed to {full} @ {branch}")
    print(f"    {Fore.CYAN}{web}/{full}/commit/{csha}{Style.RESET_ALL}")

    if tag:
        try:
            if release:
                rel = gh.create_release(full, tag, csha, name=rel_name or tag, body=rel_notes,
                                        draft=args.draft, prerelease=args.prerelease)
                log_done(f"Release {tag} created: {rel.get('html_url')}")
                for a in assets:
                    log_info(f"Attaching {os.path.basename(a)} ({human_size(os.path.getsize(a))})…")
                    gh.upload_asset(rel, a)
                    log_done(f"Attached {os.path.basename(a)}")
            else:
                gh.create_ref(full, f"refs/tags/{tag}", csha)
                log_done(f"Tag {tag} created.")
        except GitHubError as ex:
            log_err(f"Tag/release failed: {ex}")
    return True


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def pick_download_ref(gh, repo):
    """Branch / release / tag / commit picker. Returns a ref string or None."""
    full = repo["full_name"]
    clear_screen()
    log_info(f"Loading branches, releases and tags of {full}…")
    branches = gh.branches(full)
    if not branches:
        log_warn("The repository is empty.")
        pause()
        return None
    default = repo.get("default_branch")
    try:
        releases = gh.releases(full, limit=50)
    except GitHubError:
        releases = []
    try:
        tags = gh.tags(full, limit=100)
    except GitHubError:
        tags = []
    rows = [{"header": True, "label": f"Branches ({len(branches)})"}]
    for n in sorted((b["name"] for b in branches), key=lambda n: (n != default, n.lower())):
        rows.append({"label": n, "ref": n,
                     "tag": f"{Fore.CYAN}default{Style.RESET_ALL}" if n == default else ""})
    if releases:
        rows.append({"header": True, "label": f"Releases ({len(releases)})"})
        for r in releases:
            flag = " · pre-release" if r.get("prerelease") else (" · draft" if r.get("draft") else "")
            rows.append({"label": r["tag_name"] + (f"  —  {r['name']}" if r.get("name") and r["name"] != r["tag_name"] else ""),
                         "ref": r["tag_name"],
                         "tag": f"{Style.DIM}{short_date(r.get('published_at'))}{flag}{Style.RESET_ALL}"})
    if tags:
        rows.append({"header": True, "label": f"Tags ({len(tags)})"})
        for t in tags:
            rows.append({"label": t["name"], "ref": t["name"],
                         "tag": f"{Style.DIM}{t['commit']['sha'][:7]}{Style.RESET_ALL}"})
    rows.append({"header": True, "label": "Other"})
    rows.append({"label": "» Enter a commit SHA / ref manually…", "act": "manual", "color": Fore.CYAN})
    i = select_menu(f"Which version of {full}?", rows, default=1)
    if i is None:
        return None
    if rows[i].get("act") == "manual":
        return ask_line("Commit SHA, branch or tag")
    return rows[i]["ref"]


def _safe_name(s):
    return re.sub(r"[^\w.-]+", "_", s).strip("_") or "x"


def extract_zip_strip(zip_path, dest):
    """Extract a GitHub zipball into dest, dropping its 'owner-repo-sha/' top folder."""
    dest_abs = os.path.abspath(dest)
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        tops = {i.filename.split("/", 1)[0] for i in infos}
        prefix = (tops.pop() + "/") if len(tops) == 1 and all("/" in i.filename for i in infos) else ""
        total = len(infos)
        for n, info in enumerate(infos, 1):
            rel = info.filename[len(prefix):] if prefix else info.filename
            if not rel:
                continue
            target = os.path.abspath(os.path.join(dest_abs, rel))
            if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
                log_warn(f"skipped unsafe path in archive: {info.filename}")
                continue
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (info.external_attr >> 16) & 0o777
            if not _WINDOWS and mode & 0o111:
                try:
                    os.chmod(target, mode)
                except Exception:
                    pass
            if n % 20 == 0 or n == total:
                show_progress(n, total, "extracting")
        sys.stdout.write("\n")
    return total


def _dangerous_to_clear(path):
    p = os.path.abspath(path)
    return (p == os.path.abspath(os.sep) or os.path.dirname(p) == p
            or p == os.path.abspath(os.path.expanduser("~"))
            or os.path.abspath(os.getcwd()).startswith(p + os.sep) or p == os.path.abspath(os.getcwd()))


def _prepare_dest(dest, args):
    """Make sure dest is usable. Returns the folder or None (cancelled)."""
    while True:
        if not os.path.exists(dest) or (os.path.isdir(dest) and not os.listdir(dest)):
            os.makedirs(dest, exist_ok=True)
            return dest
        if not os.path.isdir(dest):
            log_err(f"{dest} exists and is a file.")
            return None
        if args.force:
            return dest
        if not can_ask():
            die(f"{dest} is not empty (use --force to overwrite files in it).")
        i = select_menu(f"{dest} is not empty:", [
            "Extract into it (overwrite files with the same name)",
            "Delete its content first, then extract",
            "Choose another folder",
        ])
        if i is None:
            return None
        if i == 0:
            return dest
        if i == 1:
            if _dangerous_to_clear(dest):
                log_err("Refusing to clear that folder (drive root, home or the current folder).")
                pause()
                continue
            if ask_yes_back(f"Really delete everything inside {dest}?", default=False):
                for name in os.listdir(dest):
                    p = os.path.join(dest, name)
                    shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
                return dest
            continue
        nd = ask_line("Destination folder", default=dest)
        if not nd:
            return None
        dest = os.path.abspath(os.path.expanduser(nd.strip('"')))


def download_source(gh, repo, ref, args, dest=None):
    full = repo["full_name"]
    name = repo["name"]
    as_zip = args.zip
    if not args.zip and can_ask() and not args.ref and not args.branch:
        i = select_menu("Save as:", ["Extract into a folder", "Keep the .zip archive"])
        if i is None:
            return False
        as_zip = i == 1
    suffix = "" if ref == repo.get("default_branch") else f"-{_safe_name(ref)}"
    if as_zip:
        default = os.path.join(os.getcwd(), f"{name}{suffix}.zip")
        if dest and os.path.isdir(dest):
            default = os.path.join(dest, f"{name}{suffix}.zip")
        elif dest:
            default = dest
        if not dest and can_ask():
            d = ask_line("Save the .zip as", default=default)
            if not d:
                return False
            default = d.strip('"')
        out = os.path.abspath(os.path.expanduser(default))
        if os.path.exists(out) and not args.force:
            if not can_ask() or not ask_yes_back(f"{out} exists. Overwrite?", default=False):
                log_info("Cancelled.")
                return False
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        log_info(f"Downloading {full} @ {ref}…")
        gh.download(gh._url(f"/repos/{full}/zipball/{_q(ref)}"), out, label=f"{name}@{ref}")
        log_done(f"Saved {out} ({human_size(os.path.getsize(out))})")
        return True
    target = dest or os.path.join(os.getcwd(), f"{name}{suffix}")
    if not dest and can_ask():
        d = ask_line("Extract into folder", default=target)
        if not d:
            return False
        target = d.strip('"')
    target = _prepare_dest(os.path.abspath(os.path.expanduser(target)), args)
    if not target:
        return False
    fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="github_tools_")
    os.close(fd)
    try:
        log_info(f"Downloading {full} @ {ref}…")
        gh.download(gh._url(f"/repos/{full}/zipball/{_q(ref)}"), tmp, label=f"{name}@{ref}")
        n = extract_zip_strip(tmp, target)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    log_done(f"{full} @ {ref} extracted into {target} ({n} entries).")
    return True


def download_release(gh, repo, args, dest=None):
    full = repo["full_name"]
    if args.release:
        try:
            rel = gh.release_by_tag(full, args.release)
        except GitHubError as ex:
            die(f"Release '{args.release}' not found in {full}: {ex}")
    else:
        clear_screen()
        log_info(f"Loading releases of {full}…")
        rels = gh.releases(full, limit=100)
        if not rels:
            log_warn(f"{full} has no releases.")
            pause()
            return False
        rows = []
        for r in rels:
            flag = " · pre-release" if r.get("prerelease") else (" · draft" if r.get("draft") else "")
            rows.append({"label": r["tag_name"] + (f"  —  {r['name']}" if r.get("name") and r["name"] != r["tag_name"] else ""),
                         "tag": f"{len(r.get('assets') or [])} file(s) {Style.DIM}"
                                f"{short_date(r.get('published_at'))}{flag}{Style.RESET_ALL}"})
        i = select_menu(f"Which release of {full}?", rows)
        if i is None:
            return False
        rel = rels[i]
    tag = rel["tag_name"]
    items = [{"label": a["name"], "tag": f"{human_size(a.get('size'))}", "asset": a,
              "selected": True} for a in rel.get("assets") or []]
    items.append({"label": f"Source code ({tag}.zip)", "tag": "", "source": True,
                  "selected": not rel.get("assets")})
    if args.asset:
        for it in items:
            it["selected"] = any(_match_filter(it["label"], p) for p in args.asset)
    elif can_ask():
        res = checkbox_menu(f"Files of release {tag}:", items,
                            header=[f"{Fore.CYAN}Release:{Style.RESET_ALL} {full} {tag}", ""])
        if res is None:
            return False
    picked = [it for it in items if it["selected"]]
    if not picked:
        log_info("Nothing selected.")
        return False
    target = dest or os.path.join(os.getcwd(), f"{repo['name']}-{_safe_name(tag)}")
    if not dest and can_ask():
        d = ask_line("Save into folder", default=target)
        if not d:
            return False
        target = d.strip('"')
    target = os.path.abspath(os.path.expanduser(target))
    os.makedirs(target, exist_ok=True)
    for it in picked:
        if it.get("source"):
            out = os.path.join(target, f"{repo['name']}-{_safe_name(tag)}.zip")
            gh.download(gh._url(f"/repos/{full}/zipball/{_q(tag)}"), out, label=os.path.basename(out))
        else:
            a = it["asset"]
            out = os.path.join(target, a["name"])
            gh.download(a["url"], out, accept="application/octet-stream", label=a["name"])
    log_done(f"{len(picked)} file(s) saved into {target}")
    return True


def download_flow(gh, me, args, cfg, dest=None):
    dest = dest or (args.path if args.download else None)
    if dest:
        dest = os.path.abspath(os.path.expanduser(dest.strip('"')))
    if args.repo:
        full = parse_repo_ref(args.repo)
        if not full:
            die(f"Invalid --repo '{args.repo}'.")
        repo = gh.repo(full)
    else:
        if not can_ask():
            die("Give the repository with --repo owner/name.")
        repo = pick_repo(gh, me, "Download from which repository?")
        if not repo or repo == "create":
            return False
    if args.release:
        return download_release(gh, repo, args, dest)
    ref = args.ref or args.branch
    if not ref:
        if can_ask():
            i = select_menu(f"Download from {repo['full_name']}:", [
                "Source code of a branch / release / tag / commit",
                "Files (assets) attached to a release",
            ])
            if i is None:
                return False
            if i == 1:
                return download_release(gh, repo, args, dest)
            ref = pick_download_ref(gh, repo)
            if not ref:
                return False
        else:
            ref = repo.get("default_branch")
    return download_source(gh, repo, ref, args, dest)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
def account_flow(gh, me, args, cfg):
    """Returns a new (gh, me) after a re-login, or None."""
    while True:
        scopes = gh.last_headers.get("x-oauth-scopes")
        header = [
            f"{Fore.MAGENTA}{Style.BRIGHT}Account{Style.RESET_ALL}",
            f"  {Fore.CYAN}login:{Style.RESET_ALL}   {me.get('login')}" + (f"  ({me['name']})" if me.get("name") else ""),
            f"  {Fore.CYAN}server:{Style.RESET_ALL}  {web_base(gh.api_url)}",
            f"  {Fore.CYAN}method:{Style.RESET_ALL}  {cfg.get('auth_method') or 'token'}"
            + (f" · scopes: {scopes}" if scopes else ""),
            f"  {Fore.CYAN}config:{Style.RESET_ALL}  {CONFIG_SOURCE}",
            f"  {Fore.CYAN}local:{Style.RESET_ALL}   {LOCAL_CONFIG_PATH}",
            "",
        ]
        i = select_menu("Account:", ["Sign in again / change the method or token",
                                     "Sign out (remove the saved token)", "Back"], header=header)
        if i is None or i == 2:
            return None
        if i == 0:
            token, me2 = interactive_login(cfg, gh.api_url)
            if token:
                ngh = GitHub(token, gh.api_url, verify=not args.insecure)
                ngh.user()
                return ngh, me2
        else:
            if "token" in REMOTE_CFG:
                log_warn("The token comes from the read-only shared config - remove it there.")
            cfg.pop("token", None)
            save_config(cfg)
            log_done("Saved token removed. You will be asked to sign in next time.")
            pause()
            sys.exit(0)


def top_menu(gh, me, args, cfg):
    tools = [
        ("Upload / update a folder  —  push local files to a repository branch", "up"),
        ("Download  —  a branch, tag, release, commit or release files", "down"),
        ("Account  —  sign in again, change token, sign out", "acct"),
    ]
    last = [0]
    while True:
        header = [
            f"{Fore.MAGENTA}{Style.BRIGHT}github_tools{Style.RESET_ALL}  ·  "
            f"{me.get('login')} @ {web_base(gh.api_url)}",
            f"{Style.DIM}config: {CONFIG_SOURCE}{Style.RESET_ALL}",
            "",
        ]
        i = select_menu("Main menu — choose a tool:", [t[0] for t in tools] + ["Quit"],
                        default=last[0], cursor_out=last, header=header)
        if i is None or i == len(tools):
            return
        what = tools[i][1]
        try:
            if what == "up":
                if upload_flow(gh, me, args, cfg):
                    pause("Press Enter to return to the main menu…")
            elif what == "down":
                if download_flow(gh, me, args, cfg):
                    pause("Press Enter to return to the main menu…")
            else:
                res = account_flow(gh, me, args, cfg)
                if res:
                    gh, me = res
        except GitHubError as ex:
            log_err(str(ex))
            pause("Press Enter to return to the main menu…")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
HELP_EPILOG = r"""
examples:
  interactive main menu:
      python github_tools.py

  upload a folder - asks for repository, branch, mode, message:
      python github_tools.py C:\Projects\mytool

  fully automatic upload (no questions):
      python github_tools.py C:\Projects\mytool --repo me/mytool -b main -m "Release 1.2" --yes
      python github_tools.py . --repo me/site --mirror --yes          (exact copy, deletes extras)
      python github_tools.py . --repo me/new-tool --create --public --yes
      python github_tools.py dist --repo me/tool --release v1.2.0 --asset-file tool.exe --yes
      python github_tools.py . --repo me/tool --dry-run               (show changes only)

  download:
      python github_tools.py --download                               (interactive)
      python github_tools.py D:\src\tool --download --repo me/tool --yes
      python github_tools.py --download --repo me/tool --ref v1.2.0 --zip --yes
      python github_tools.py --download --repo me/tool --release latest --asset "*.exe" --yes

  information:
      python github_tools.py --whoami | --list-repos | --list-refs me/tool | --where-config

upload modes:
  update  add new files and overwrite changed ones; files that exist only on GitHub stay
  mirror  the branch becomes an exact copy of the folder (files missing locally are deleted)
  Only changed files are sent; the result is one commit. .gitignore files are honoured,
  .git/, __pycache__/ and the config file itself are never uploaded.

configuration (first match wins; see CONFIG_URL at the top of the script):
  1) CONFIG_URL / --config-url / env GITHUB_TOOLS_CONFIG_URL - local, UNC (\\nas\share\x.json)
     or http(s) path, READ-ONLY, highest priority
  2) github_tools.config.json in the current folder, then next to the script
  3) otherwise it asks and creates github_tools.config.json in the current folder
  Keys: api_url, auth_method, token, login, oauth_client_id, excludes (list of patterns).

sign-in: personal access token, or username + password + 2FA/OTP via GitHub's device flow
  (sign-in happens on github.com; needs an OAuth App Client ID with Device Flow enabled).
  A token can also come from --token or the GITHUB_TOKEN / GH_TOKEN variables.

text input: paste with right-click, Shift+Insert or Ctrl+V; passwords are shown as *;
  Alt+numpad codes work (Alt+64 = @). Esc always steps one screen back.
"""


def build_parser():
    ap = argparse.ArgumentParser(
        prog="github_tools.py",
        description="Upload / update a local folder to GitHub and download repositories, "
                    "interactively or with parameters. Without arguments it opens the "
                    "interactive main menu; with a FOLDER it uploads that folder.",
        epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", metavar="FOLDER",
                    help="folder to upload (default action), or the destination with --download")
    g = ap.add_argument_group("what to do")
    g.add_argument("-d", "--download", action="store_true", help="download instead of upload")
    g.add_argument("-r", "--repo", metavar="OWNER/NAME", help="repository (owner/name or URL)")
    g.add_argument("-b", "--branch", help="upload: target branch (created if missing); "
                                          "download: branch to fetch")
    g.add_argument("-y", "--yes", action="store_true",
                   help="never ask: use parameters and defaults, no confirmations")
    g.add_argument("--dry-run", action="store_true", help="upload: only show what would change")
    up = ap.add_argument_group("upload options")
    up.add_argument("--mode", choices=("update", "mirror"), help="update (default) or mirror")
    up.add_argument("--mirror", dest="mode", action="store_const", const="mirror",
                    help="shortcut for --mode mirror")
    up.add_argument("-m", "--message", help="commit message")
    up.add_argument("--base", metavar="BRANCH", help="new branch starts from this branch "
                                                     "(default: the repository's default branch)")
    up.add_argument("--orphan", action="store_true", help="new branch starts empty (no history)")
    up.add_argument("--tag", help="create a tag at the new commit")
    up.add_argument("--release", metavar="TAG",
                    help="upload: create a release with this tag; download: release to fetch "
                         "files from ('latest' allowed)")
    up.add_argument("--release-name", help="release title (default: the tag)")
    up.add_argument("--notes", help="release notes")
    up.add_argument("--prerelease", action="store_true", help="mark the release as pre-release")
    up.add_argument("--draft", action="store_true", help="create the release as a draft")
    up.add_argument("--asset-file", action="append", metavar="FILE",
                    help="file to attach to the created release (repeatable)")
    up.add_argument("--create", action="store_true", help="create the repository if it is missing")
    up.add_argument("--public", action="store_true", help="a created repository is public "
                                                          "(default private)")
    up.add_argument("--description", help="description of a created repository")
    up.add_argument("--exclude", action="append", metavar="PATTERN",
                    help="gitignore-style pattern to skip (repeatable)")
    up.add_argument("--no-gitignore", action="store_true", help="ignore .gitignore files")
    up.add_argument("--force", action="store_true",
                    help="upload: force-update the branch; download: overwrite existing files")
    dn = ap.add_argument_group("download options")
    dn.add_argument("--ref", help="branch, tag or commit SHA to download")
    dn.add_argument("--zip", action="store_true", help="keep the .zip instead of extracting")
    dn.add_argument("--asset", action="append", metavar="PATTERN",
                    help="release files to download, wildcards allowed (repeatable)")
    cn = ap.add_argument_group("connection & config")
    cn.add_argument("--token", help="GitHub token (otherwise env GITHUB_TOKEN / config / sign-in)")
    cn.add_argument("--api-url", help=f"API URL (default {DEFAULT_API_URL}; "
                                      f"Enterprise: https://HOST/api/v3)")
    cn.add_argument("--insecure", action="store_true", help="do not verify HTTPS certificates")
    cn.add_argument("--config", metavar="FILE", help="use this local config file")
    cn.add_argument("--config-url", metavar="PATH_OR_URL",
                    help="shared read-only config (overrides CONFIG_URL)")
    cn.add_argument("--where-config", action="store_true", help="show the config locations and exit")
    cn.add_argument("--logout", action="store_true", help="remove the saved token and exit")
    cn.add_argument("--relogin", action="store_true", help="sign in again (choose method)")
    info = ap.add_argument_group("information")
    info.add_argument("--whoami", action="store_true", help="show the signed-in account")
    info.add_argument("--list-repos", action="store_true", help="list your repositories")
    info.add_argument("--list-refs", metavar="OWNER/NAME",
                      help="list branches, releases and tags of a repository")
    ap.add_argument("--version", action="version", version=f"github_tools {VERSION}")
    return ap


def main():
    global _YES, _CONFIG_OVERRIDE, _CONFIG_URL_OVERRIDE
    args = build_parser().parse_args()
    _YES = args.yes
    _CONFIG_OVERRIDE = args.config
    _CONFIG_URL_OVERRIDE = args.config_url

    if args.where_config:
        url = config_url()
        print(f"Shared (read-only): {url or '(not set)'}")
        for p in _local_candidates():
            print(f"Local:              {p}  ({'exists' if os.path.isfile(p) else 'missing'})")
        return

    cfg = load_config()
    if args.logout:
        cfg.pop("token", None)
        save_config(cfg)
        log_done("Saved token removed.")
        return
    if args.relogin:
        cfg.pop("token", None)

    gh, me = connect(args, cfg)
    log_done(f"Connected as {me.get('login')} to {web_base(gh.api_url)}")

    try:
        if args.whoami:
            print(f"{me.get('login')}  {me.get('name') or ''}  {me.get('html_url') or ''}")
            return
        if args.list_repos:
            for r in sorted(gh.repos(), key=lambda r: r["full_name"].lower()):
                print(f"{r['full_name']:<50} {'private' if r.get('private') else 'public ':<8} "
                      f"{short_date(r.get('pushed_at'))}")
            return
        if args.list_refs:
            full = parse_repo_ref(args.list_refs) or die("Invalid repository.")
            repo = gh.repo(full)
            print(f"{Fore.CYAN}Branches:{Style.RESET_ALL}")
            for b in gh.branches(full):
                print(f"  {b['name']}" + ("  (default)" if b["name"] == repo.get("default_branch") else ""))
            print(f"{Fore.CYAN}Releases:{Style.RESET_ALL}")
            for r in gh.releases(full, limit=50):
                print(f"  {r['tag_name']:<20} {short_date(r.get('published_at'))}  "
                      f"{len(r.get('assets') or [])} file(s)")
            print(f"{Fore.CYAN}Tags:{Style.RESET_ALL}")
            for t in gh.tags(full, limit=100):
                print(f"  {t['name']:<20} {t['commit']['sha'][:7]}")
            return
        if args.download:
            download_flow(gh, me, args, cfg)
            return
        if args.path:
            upload_flow(gh, me, args, cfg, path=args.path)
            return
        if not can_ask():
            die("Nothing to do: give a FOLDER to upload or --download (see --help).")
        top_menu(gh, me, args, cfg)
    except GitHubError as ex:
        die(str(ex))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warn("Interrupted by user.")
        sys.exit(130)
