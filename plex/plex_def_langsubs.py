#!/usr/bin/env python3
"""
plex_def_langsubs.py  (Windows 10 CLI, colored UI via colorama)
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
existing one wins): path from --config / the PLEX_DEF_LANGSUBS_CONFIG
variable, then next to the script (plex_def_langsubs.config.json), then a
.config folder next to the script and in parent folders (works from a
network/samba drive too), finally ~/.config. A new config is created NEXT
TO THE SCRIPT (so it travels with it to another drive/OS). Show the
current path with --where-config, force a custom path with --config.
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
    python plex_def_langsubs.py                 # interactive wizard
    python plex_def_langsubs.py --show 32800 --audio kor --subs cze --yes
    python plex_def_langsubs.py --show "Recipe for Love" --subs off --dry-run
    python plex_def_langsubs.py --logout | --relogin | --switch-user
"""

import argparse
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
#   1) path from --config or the PLEX_DEF_LANGSUBS_CONFIG variable
#   2) next to the script:       <script_dir>/plex_def_langsubs.config.json
#   3) .config next to script:   <script_dir>/.config/plex_def_langsubs/config.json
#      and in PARENT folders     (works from a network/samba drive too - portable)
#   4) home ~/.config:           ~/.config/plex_def_langsubs/config.json
# A new config is created NEXT TO THE SCRIPT (portable - travels with it to
# another OS/drive); if a config already exists there, it keeps being used.
CONFIG_FILENAME = "plex_def_langsubs.config.json"
_CONFIG_OVERRIDE = None   # set by --config
CONFIG_PATH = None        # current path (resolved at run time)


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _script_config_path():
    return os.path.join(_script_dir(), CONFIG_FILENAME)


def _xdg_config_dir():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def _dotconfig_paths(base):
    """Candidates inside the .config folder at the given path."""
    return [os.path.join(base, ".config", "plex_def_langsubs", "config.json"),
            os.path.join(base, ".config", CONFIG_FILENAME)]


def _config_read_candidates():
    cands = []
    env = _CONFIG_OVERRIDE or os.environ.get("PLEX_DEF_LANGSUBS_CONFIG")
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
    cands.append(os.path.join(_xdg_config_dir(), "plex_def_langsubs", "config.json"))
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
    override = _CONFIG_OVERRIDE or os.environ.get("PLEX_DEF_LANGSUBS_CONFIG")
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
STATUS_AUTOHIDE = 4.0  # s: jak dlouho zůstane hláška (např. výsledek F5), než sama zmizí

PRODUCT = "plex_def_langsubs"
PLEX_TV = "https://plex.tv"
SIGNIN_URL = f"{PLEX_TV}/api/v2/users/signin"
PINS_URL = f"{PLEX_TV}/api/v2/pins"
HOMEUSERS_URL = f"{PLEX_TV}/api/v2/home/users"
SWITCH_URL = f"{PLEX_TV}/api/home/users/{{uid}}/switch"


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
                    "?": "f5"}.get(c2, "other")  # F5 = scan code 0x3F ('?')
        if ch in ("\r", "\n"):
            return "enter"
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
                    "5~": "pgup", "6~": "pgdn", "15~": "f5"}.get(seq, "esc")
        if c in (0x0d, 0x0a):
            return "enter"
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
                     refresh_cb=None, header=None):
    """Arrow-key menu + type-to-search. Returns an index, or None (cancelled).
    Behaves like an app: clears the screen up front and redraws only the current
    view (no scrolling). header = context lines above the list.
    refresh_cb(current_index) -> message (str): called on F5 (e.g. library scan)."""
    if not _tui_supported():
        for h in (header or []):
            print(h)
        return _ask_choice_classic(prompt, labels, default)

    n = len(labels)
    plain = [strip_ansi(l) for l in labels]
    header = list(header or [])
    filt = ""
    status = ""  # status line (e.g. F5 result) - shown INSIDE the window
    sel_pos = default if 0 <= default < n else 0
    prev_lines = 0  # number of lines in the previous frame
    first = True    # first render clears the screen

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
        vis_lines.append(f"{Fore.CYAN}{trunc(hint, maxw)}{Style.RESET_ALL}")

        if not order:
            vis_lines.append(f"  {Fore.RED}(no match){Style.RESET_ALL}")
        else:
            start = max(0, min(sel_pos - page_rows // 2, len(order) - page_rows))
            window = order[start:start + page_rows]
            if start > 0:
                vis_lines.append(f"  {Fore.CYAN}▲ ({start} above){Style.RESET_ALL}")
            for pos, i in enumerate(window, start):
                text = trunc(plain[i], maxw - 2)
                if pos == sel_pos:
                    vis_lines.append(f"{Fore.GREEN}{Style.BRIGHT}›{Style.RESET_ALL} "
                                     f"{Fore.GREEN}{Style.BRIGHT}{text}{Style.RESET_ALL}")
                else:
                    vis_lines.append(f"  {text}")
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
            # když je zobrazená hláška (např. výsledek F5), čti s timeoutem,
            # ať sama zmizí i bez stisku klávesy
            key = _read_key(STATUS_AUTOHIDE if status else None)
            if key == "timeout":
                status = ""
                render(order, sel_pos)
                continue
            if key != "f5" and status:
                status = ""  # status message also disappears on any keypress
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


def _ask_choice_classic(prompt, labels, default=0):
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    if CONFIG_PATH is None:
        resolve_config_path()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    global CONFIG_PATH
    if CONFIG_PATH is None:
        resolve_config_path()
    home_cfg = os.path.join(_xdg_config_dir(), "plex_def_langsubs", "config.json")
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
            "X-Plex-Device-Name": "plex_def_langsubs CLI",
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
        return [{"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
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
                 "year": m.get("year"), "type": m.get("type")}
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

    def refresh_section(self, section_key):
        """Trigger 'Scan Library Files' on the library (GET .../refresh)."""
        st, _ = http_raw("GET", self._url(f"/library/sections/{section_key}/refresh"),
                         headers=self._headers(), verify=self.verify, timeout=self.timeout)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}")
        return st


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
def choose_variant(kind, variants, allow_off=False, allow_keep=True, allow_back=False, header=None):
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
        return ("keep", None)
    default_idx = keep_idx if keep_idx is not None else 0
    idx = interactive_menu(f"Select default {kind}:", all_labels,
                           default=default_idx, allow_cancel=allow_back, header=header)
    if idx is None:
        return ("back", None)
    if idx < len(variants):
        return ("var", variants[idx])
    if off_idx is not None and idx == off_idx:
        return ("off", None)
    return ("keep", None)


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


def resolve_coverage(kind, action, items_data, stream_type, allow_off, interactive):
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
        if not avail:
            log_info("Missing items have no other track — leaving them unchanged.")
            break
        opts = sorted(avail.values(), key=lambda v: (-cnt[v["key"]], v["label"].lower()))
        labels = [f"{v['label']}  (has {cnt[v['key']]}/{len(missing)} missing)" for v in opts]
        extra = []
        if allow_off:
            extra.append(f"{Fore.MAGENTA}— turn subtitles OFF on missing —{Style.RESET_ALL}")
        extra.append(f"{Fore.MAGENTA}— leave missing unchanged (skip) —{Style.RESET_ALL}")
        mlbls = [ep_label(items_data[i]) for i in missing]
        header = [
            f"{Fore.YELLOW}{len(missing)} of {n} items lack {kind}: {variant['label']}{Style.RESET_ALL}",
            "  " + ", ".join(mlbls[:40]) + (" …" if len(mlbls) > 40 else ""),
            "",
        ]
        idx = interactive_menu(f"Replace {kind} on missing episodes with?",
                               labels + extra, default=len(labels) + len(extra) - 1,
                               header=header)
        if idx is None:
            break
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
            sel = extra[idx - len(opts)]
            if allow_off and "OFF" in sel:
                for i in missing:
                    plan[i] = ("off", None)
                log_done(f"Subtitles turned OFF on {len(missing)} missing episodes.")
            else:
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


def select_and_configure(client, args):
    """Wizard: library -> item -> audio -> subtitles, with step-back (Esc).
    Returns (items_data, audio_action, sub_action) or None."""
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
                labels = [f"{f['title']} ({f['year']}) [{f['type']}]" for f in found]
                i = interactive_menu("Multiple found - select:", labels, allow_cancel=True)
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

    while True:
        if step == LIB:
            labels = [f"{s['title']}  [{s['type']}]" for s in secs]
            i = interactive_menu("Select a library:", labels, allow_cancel=True,
                                 refresh_cb=lambda idx: scan_lib(secs[idx]) if idx is not None else None)
            if i is None:
                return None
            sec = secs[i]
            step = ITEM

        elif step == ITEM:
            items = client.items_in_section(sec["key"], sec["type"])
            if not items:
                log_warn("The library is empty.")
                if single_lib:
                    return None
                step = LIB
                continue
            items.sort(key=lambda x: (x["title"] or "").lower())
            labels = [f"{it['title']} ({it['year']})" if it["year"] else it["title"] for it in items]
            i = interactive_menu(f"Select a show/movie from '{sec['title']}':", labels, allow_cancel=True,
                                 refresh_cb=lambda idx: scan_lib(sec))
            if i is None:
                if single_lib:
                    return None
                step = LIB
                continue
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
            audio_action = choose_variant("audio", audio_vars, allow_off=False,
                                          allow_back=True, header=header)
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
            sub_action = choose_variant("subtitles", sub_vars, allow_off=True,
                                        allow_back=True, header=header)
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
    return idata, audio_action, sub_action


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Bulk-set default audio/subtitles for a show or movie on Plex.")
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
    ap.add_argument("--config", help="Path to the config file (otherwise searched next to the script, in .config next to the script and parent folders, and in ~/.config)")
    ap.add_argument("--where-config", action="store_true",
                    help="Print the path of the config file in use and exit")
    args = ap.parse_args()

    global _CONFIG_OVERRIDE
    if args.config:
        _CONFIG_OVERRIDE = args.config
    resolve_config_path()
    if args.where_config:
        exists = "exists" if (CONFIG_PATH and os.path.isfile(CONFIG_PATH)) else "does not exist yet"
        print(f"Config: {CONFIG_PATH}  ({exists})")
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

    print(f"{Fore.MAGENTA}=== plex_def_langsubs - default audio/subtitles ==={Style.RESET_ALL}")

    client = build_client(args, cfg)

    result = select_and_configure(client, args)
    if result is None:
        log_info("Cancelled.")
        return
    items_data, audio_action, sub_action = result

    # build a per-episode plan; if some episodes lack the track, ask for a replacement
    interactive = not args.yes
    print()
    audio_plan = resolve_coverage("audio", audio_action, items_data, ST_AUDIO,
                                  allow_off=False, interactive=interactive)
    sub_plan = resolve_coverage("subtitles", sub_action, items_data, ST_SUBTITLE,
                                allow_off=True, interactive=interactive)

    # is there anything to do at all?
    if all(p[0] == "skip" for p in audio_plan) and all(p[0] == "skip" for p in sub_plan):
        log_warn("Nothing to change after all. Exiting.")
        return

    clear_screen()
    log_info(f"Will set on {len(items_data)} items:")
    print(f"    audio:   {plan_summary(audio_plan)}")
    print(f"    subtitles: {plan_summary(sub_plan)}")
    print()

    dry = args.dry_run
    if not dry and not args.yes:
        if not ask_yes("Apply the changes?", default=True):
            dry = ask_yes("At least show the plan (dry-run)?", default=True)
            if not dry:
                log_info("Cancelled.")
                return

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warn("Interrupted by user.")
        sys.exit(130)
