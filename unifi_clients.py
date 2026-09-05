#!/usr/bin/env python3
"""
List clients connected to a UniFi OS Server (Network application).

On first run the script asks for connection details interactively and stores
them in unifi_clients.json next to the script (mode 0600). Every later run
reads that file and goes straight to work.

    python3 unifi_clients.py                # normal run
    python3 unifi_clients.py --reconfigure  # run the wizard again
    python3 unifi_clients.py --json         # raw JSON output
    python3 unifi_clients.py --wired        # include wired clients
    python3 unifi_clients.py --no-color     # plain output
    python3 unifi_clients.py --config /path/to/config.json

Interactive prompts support:
    * clipboard paste with Ctrl+V, or right-click / Ctrl+Shift+V in the
      terminal (characters are injected into stdin and read normally)
    * Alt+<numpad> and AltGr characters, accents and any other UTF-8 input
    * arrow keys, Home/End and history in plain prompts (via readline)
    * Ctrl+T in hidden prompts toggles masking, so you can verify a pasted key
"""

import os
import sys
import json
import stat
import codecs
import shutil
import argparse
import subprocess

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import requests
    import urllib3
except ImportError:
    sys.exit("Missing dependency. Install with:  pip install requests")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# colorama: cross-platform ANSI colors (and it fixes the legacy Windows console)
try:
    import colorama
    from colorama import Fore, Style

    try:
        colorama.just_fix_windows_console()      # colorama >= 0.4.6
    except AttributeError:
        colorama.init()                          # older releases
    HAVE_COLORAMA = True
except ImportError:
    HAVE_COLORAMA = False

    class _Dummy:
        def __getattr__(self, _):
            return ""

    Fore = Style = _Dummy()

# readline gives plain input() proper line editing, history and paste handling.
# On Windows this comes from pyreadline3; absence is not fatal.
try:
    import readline  # noqa: F401
except ImportError:
    try:
        import pyreadline3  # noqa: F401
    except ImportError:
        pass

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import msvcrt

# Make sure we can print and read non-ASCII on a legacy Windows console.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
class Palette:
    """Thin wrapper so every call site stays readable and color can be off."""

    def __init__(self, enabled=True):
        self.enabled = enabled and HAVE_COLORAMA

    def _wrap(self, text, code):
        return f"{code}{text}{Style.RESET_ALL}" if self.enabled else str(text)

    def head(self, t):
        return self._wrap(t, Style.BRIGHT + Fore.CYAN)

    def ok(self, t):
        return self._wrap(t, Fore.GREEN)

    def warn(self, t):
        return self._wrap(t, Fore.YELLOW)

    def err(self, t):
        return self._wrap(t, Style.BRIGHT + Fore.RED)

    def dim(self, t):
        return self._wrap(t, Style.DIM)

    def bold(self, t):
        return self._wrap(t, Style.BRIGHT)

    def key(self, t):
        return self._wrap(t, Fore.MAGENTA)


C = Palette()


def use_color(enabled):
    C.enabled = enabled and HAVE_COLORAMA and sys.stdout.isatty()


def pad(text, width):
    """Pad to width based on the visible string, then let color be applied."""
    text = str(text)
    return text[:width].ljust(width)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
def read_clipboard():
    """Return clipboard text, or None if no working backend was found."""
    try:
        import pyperclip

        text = pyperclip.paste()
        if text:
            return text
    except Exception:
        pass

    if IS_WINDOWS:
        candidates = [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
    elif sys.platform == "darwin":
        candidates = [["pbpaste"]]
    else:
        candidates = [
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ]

    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(
                cmd, capture_output=True, timeout=5, check=True
            ).stdout.decode("utf-8", "replace")
            return out.rstrip("\r\n")
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Interactive input
# ---------------------------------------------------------------------------
PASTE_HINT = "Ctrl+V or right-click pastes"


def _insert_clipboard(buf, masked, mask_on):
    """Handle a Ctrl+V keystroke: append clipboard text to the buffer."""
    text = read_clipboard()
    if not text:
        sys.stdout.write("\a")  # nothing to paste
        sys.stdout.flush()
        return
    text = text.replace("\r", "").replace("\n", "").replace("\t", "")
    buf.extend(text)
    if masked and mask_on[0]:
        sys.stdout.write("*" * len(text))
    else:
        sys.stdout.write(text)
    sys.stdout.flush()


def _redraw(prompt, buf, masked, mask_on):
    """Repaint the current line, used after a masking toggle or backspace."""
    shown = "*" * len(buf) if (masked and mask_on[0]) else "".join(buf)
    sys.stdout.write("\r\x1b[2K" if C.enabled or True else "\r")
    sys.stdout.write(prompt + shown)
    sys.stdout.flush()


def _read_windows(prompt, masked):
    buf, mask_on = [], [masked]
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while True:
        ch = msvcrt.getwch()

        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            return "".join(buf)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x04" and not buf:
            raise EOFError
        if ch in ("\b", "\x7f"):
            if buf:
                buf.pop()
                _redraw(prompt, buf, masked, mask_on)
            continue
        if ch == "\x16":                       # Ctrl+V
            _insert_clipboard(buf, masked, mask_on)
            continue
        if ch == "\x14" and masked:            # Ctrl+T toggles masking
            mask_on[0] = not mask_on[0]
            _redraw(prompt, buf, masked, mask_on)
            continue
        if ch in ("\x00", "\xe0"):             # function / arrow key prefix
            msvcrt.getwch()                    # swallow the second byte
            continue
        if ch < " ":                           # any other control char
            continue

        # Regular character. getwch() is unicode-aware, so Alt+<numpad>
        # codes, AltGr characters and accents all land here intact.
        buf.append(ch)
        sys.stdout.write("*" if (masked and mask_on[0]) else ch)
        sys.stdout.flush()


def _read_posix(prompt, masked):
    import termios
    import select

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ECHO | termios.ICANON)   # lflag: no echo, char at a time
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0

    buf, mask_on = [], [masked]
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            raw = os.read(fd, 1)
            if not raw:
                raise EOFError
            ch = decoder.decode(raw)
            if not ch:
                continue                        # middle of a UTF-8 sequence

            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                return "".join(buf)
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x04" and not buf:
                raise EOFError
            if ch in ("\b", "\x7f"):
                if buf:
                    buf.pop()
                    _redraw(prompt, buf, masked, mask_on)
                continue
            if ch == "\x16":                    # Ctrl+V
                _insert_clipboard(buf, masked, mask_on)
                continue
            if ch == "\x14" and masked:         # Ctrl+T
                mask_on[0] = not mask_on[0]
                _redraw(prompt, buf, masked, mask_on)
                continue
            if ch == "\x15":                    # Ctrl+U clears the line
                buf.clear()
                _redraw(prompt, buf, masked, mask_on)
                continue
            if ch == "\x1b":
                # Escape sequence: arrow keys, or the bracketed-paste markers
                # \x1b[200~ / \x1b[201~. Drain and drop it; the pasted body
                # itself arrives as ordinary characters right after.
                while select.select([fd], [], [], 0.02)[0]:
                    nxt = os.read(fd, 1)
                    if not nxt or nxt.isalpha() or nxt == b"~":
                        break
                continue
            if ch < " ":
                continue

            # AltGr and Alt+<code> characters are delivered as normal UTF-8
            # here, so accented input works without any special casing.
            buf.append(ch)
            sys.stdout.write("*" if (masked and mask_on[0]) else ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_input(prompt, masked=False):
    """Read one line. Falls back to input() when stdin is not a terminal."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")

    if not masked:
        # Plain prompts go through input() so readline provides editing,
        # history and native terminal paste.
        return input(prompt)

    try:
        return _read_windows(prompt, True) if IS_WINDOWS else _read_posix(prompt, True)
    except (ImportError, OSError, termios_error()):
        import getpass

        return getpass.getpass(prompt)


def termios_error():
    """Return the termios error class, or a placeholder on Windows."""
    try:
        import termios

        return termios.error
    except ImportError:
        return OSError


def ask(prompt, default=None, secret=False, hint=None):
    """One question, with an optional default and hint line."""
    if hint:
        print(C.dim(f"  {hint}"))
    suffix = C.dim(f" [{default}]") if default is not None else ""
    label = f"{C.bold(prompt)}{suffix}: "
    while True:
        try:
            value = read_input(label, masked=secret).strip()
        except EOFError:
            raise KeyboardInterrupt
        if value:
            return value
        if default is not None:
            return default
        print(C.err("  This value is required."))


def ask_yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = read_input(f"{C.bold(prompt)}{C.dim(f' [{hint}]')}: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print(C.err("  Please answer y or n."))


def ask_choice(prompt, options, default):
    """options: list of (key, label, description)."""
    for key, label, desc in options:
        print(f"  {C.key(key)}) {C.bold(label)}")
        if desc:
            print(C.dim(f"     {desc}"))
    valid = {key for key, _, _ in options}
    while True:
        answer = ask(prompt, default)
        if answer in valid:
            return answer
        print(C.err(f"  Pick one of: {', '.join(sorted(valid))}"))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "unifi_clients.json"
)

# Some firmware revisions use the plural form, so both are tried.
INTEGRATION_PREFIXES = [
    "/proxy/network/integration/v1",
    "/proxy/network/integrations/v1",
]


def wizard(path):
    """Interactive setup, returns a ready config dict."""
    line = "=" * 66
    print(C.head(line))
    print(C.head(" UniFi OS Server - connection setup"))
    print(C.head(line))
    print("No configuration found, let's create one.")
    if not HAVE_COLORAMA:
        print(C.warn("Note: colorama is not installed, output will be plain."))
    print(C.dim(f"Prompts accept pasted text ({PASTE_HINT}) and accented characters.\n"))

    host = ask("Controller IP address or hostname", "192.168.1.1")

    print()
    print(C.bold("Network application port:"))
    print(f"  {C.key('443')}    UniFi OS console (UDM, UDR, Cloud Key, ...)")
    print(f"  {C.key('11443')}  UniFi OS Server (self-hosted on Linux/Windows/Docker)")
    while True:
        port = ask("Port", "11443")
        if port.isdigit() and 1 <= int(port) <= 65535:
            break
        print(C.err("  Port must be a number between 1 and 65535."))

    print()
    print(C.bold("Authentication method:"))
    choice = ask_choice(
        "Choice",
        [
            ("1", "API key - official Integration API (recommended)",
             "Network > Settings > Control Plane > Integrations"),
            ("2", "Username + password - legacy API",
             "Returns richer WiFi data: RSSI, signal, AP, channel"),
        ],
        default="1",
    )
    method = "official" if choice == "1" else "legacy"

    cfg = {
        "host": host,
        "port": int(port),
        "method": method,
        "verify_ssl": False,
        "site": "default",
    }

    print()
    if method == "official":
        cfg["api_key"] = ask(
            "API key",
            secret=True,
            hint=f"Hidden while typing. {PASTE_HINT}, Ctrl+T reveals it.",
        )
    else:
        print(C.warn("Tip: create a local read-only admin account without 2FA."))
        cfg["username"] = ask("Username")
        cfg["password"] = ask(
            "Password",
            secret=True,
            hint=f"Hidden while typing. {PASTE_HINT}, Ctrl+T reveals it.",
        )
        cfg["site"] = ask("Site name", "default")

    print()
    cfg["verify_ssl"] = ask_yes_no(
        "Verify the SSL certificate? (say no for a self-signed cert)", default=False
    )

    # Prove the settings work before writing them to disk.
    print(C.dim("\nTesting the connection..."))
    try:
        clients = fetch(cfg)
    except Exception as exc:
        print(C.err(f"  Failed: {exc}"))
        if not ask_yes_no("Save the configuration anyway?", default=False):
            sys.exit(C.err("Nothing was saved."))
    else:
        print(C.ok(f"  Connected. The controller returned {len(clients)} clients."))

    save_config(cfg, path)
    print(C.ok(f"\nSaved to {path} (mode 0600).\n"))
    return cfg


def save_config(cfg, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, ensure_ascii=False)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # holds a password or API key
    except OSError:
        pass


def load_config(path):
    try:
        with open(path, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except json.JSONDecodeError as exc:
        sys.exit(C.err(f"{path} is not valid JSON: {exc}"))

    if not IS_WINDOWS and stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        print(
            C.warn(f"Warning: {path} is readable by others. Run: chmod 600 {path}"),
            file=sys.stderr,
        )
    return cfg


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def base_url(cfg):
    return f"https://{cfg['host']}:{cfg['port']}"


def fetch(cfg):
    """Dispatch by method. Returns a list of normalised client dicts."""
    if cfg["method"] == "official":
        return fetch_official(cfg)
    return fetch_legacy(cfg)


def fetch_official(cfg):
    session = requests.Session()
    session.headers.update(
        {"X-API-KEY": cfg["api_key"], "Accept": "application/json"}
    )
    session.verify = cfg.get("verify_ssl", False)

    base = base_url(cfg)
    prefix = sites = None
    last_error = None

    # Probe for the working prefix and grab the site list in one go.
    for candidate in INTEGRATION_PREFIXES:
        try:
            resp = session.get(f"{base}{candidate}/sites", timeout=15)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if resp.status_code == 404:
            continue
        if resp.status_code in (401, 403):
            raise RuntimeError("Invalid API key, or the key lacks permissions.")
        resp.raise_for_status()
        prefix, sites = candidate, resp.json().get("data", [])
        break

    if prefix is None:
        raise RuntimeError(f"Integration API unreachable: {last_error or 'HTTP 404'}")
    if not sites:
        raise RuntimeError("The controller returned no sites.")

    site_id = sites[0]["id"]

    raw, offset = [], 0
    while True:
        resp = session.get(
            f"{base}{prefix}/sites/{site_id}/clients",
            params={"limit": 200, "offset": offset},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        page = body.get("data", [])
        raw.extend(page)
        offset += len(page)
        if not page or offset >= body.get("totalCount", offset):
            break

    result = []
    for item in raw:
        access = item.get("access")
        result.append(
            {
                "name": item.get("name") or item.get("hostname") or "?",
                "mac": item.get("macAddress", ""),
                "ip": item.get("ipAddress", ""),
                "wired": item.get("type", "").upper() == "WIRED",
                "ssid": access.get("ssid") if isinstance(access, dict) else None,
                "signal": None,
                "rssi": None,
                "channel": None,
                "ap": None,
                "_raw": item,
            }
        )
    return result


def fetch_legacy(cfg):
    session = requests.Session()
    session.verify = cfg.get("verify_ssl", False)
    base = base_url(cfg)

    resp = session.post(
        f"{base}/api/auth/login",
        json={"username": cfg["username"], "password": cfg["password"]},
        timeout=15,
    )
    if resp.status_code in (400, 401):
        raise RuntimeError("Login failed: wrong credentials, or the account uses 2FA.")
    resp.raise_for_status()

    token = resp.headers.get("x-csrf-token")
    if token:
        session.headers["X-CSRF-Token"] = token

    site = cfg.get("site", "default")
    try:
        resp = session.get(f"{base}/proxy/network/api/s/{site}/stat/sta", timeout=15)
        resp.raise_for_status()
        raw = resp.json()["data"]
    finally:
        try:
            session.post(f"{base}/api/auth/logout", timeout=10)
        except requests.RequestException:
            pass

    return [
        {
            "name": item.get("name") or item.get("hostname") or item.get("oui") or "?",
            "mac": item.get("mac", ""),
            "ip": item.get("ip", ""),
            "wired": bool(item.get("is_wired", False)),
            "ssid": item.get("essid"),
            "signal": item.get("signal"),
            "rssi": item.get("rssi"),
            "channel": item.get("channel"),
            "ap": item.get("ap_mac"),
            "_raw": item,
        }
        for item in raw
    ]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def colorize_signal(dbm):
    """Green for a healthy link, yellow for usable, red for poor."""
    if dbm is None:
        return C.dim(pad("-", 6))
    text = pad(dbm, 6)
    if dbm >= -60:
        return C.ok(text)
    if dbm >= -70:
        return C.warn(text)
    return C.err(text)


def print_table(clients, show_wired):
    rows = clients if show_wired else [c for c in clients if not c["wired"]]
    if not rows:
        print(C.warn("No clients found."))
        return

    detailed = any(row["signal"] is not None for row in rows)

    header = f"{pad('NAME', 26)} {pad('MAC', 19)} {pad('IP', 16)}"
    if detailed:
        header += f" {pad('SSID', 16)} {pad('SIGNAL', 6)} {pad('CH', 4)}  AP"
    elif show_wired:
        header += "  TYPE"
    print(C.head(header))
    print(C.dim("-" * len(header)))

    for row in sorted(rows, key=lambda r: (r["wired"], r["name"].lower())):
        line = (
            f"{C.bold(pad(row['name'], 26))} "
            f"{pad(row['mac'], 19)} "
            f"{pad(row['ip'], 16)}"
        )
        if detailed:
            line += (
                f" {pad(row['ssid'] or '-', 16)} "
                f"{colorize_signal(row['signal'])} "
                f"{pad(row['channel'] or '-', 4)}  "
                f"{C.dim(row['ap'] or '')}"
            )
        elif show_wired:
            line += "  " + (C.dim("wired") if row["wired"] else C.ok("wifi"))
        print(line)

    wifi_count = sum(1 for c in clients if not c["wired"])
    print()
    print(f"{C.ok('WiFi: ' + str(wifi_count))}   {C.dim('Total: ' + str(len(clients)))}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to the config file")
    parser.add_argument("--reconfigure", action="store_true", help="run the wizard again")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument("--wired", action="store_true", help="include wired clients")
    parser.add_argument("--no-color", action="store_true", help="disable colored output")
    args = parser.parse_args()

    # NO_COLOR is a de-facto standard, honour it alongside the flag.
    use_color(not args.no_color and not os.environ.get("NO_COLOR"))

    if args.reconfigure or not os.path.exists(args.config):
        if not sys.stdin.isatty():
            sys.exit(
                C.err(f"No config at {args.config} and no terminal to ask on. "
                      "Run the script interactively once, or pass --config.")
            )
        try:
            cfg = wizard(args.config)
        except (KeyboardInterrupt, EOFError):
            sys.exit(C.warn("\nAborted."))
    else:
        cfg = load_config(args.config)

    try:
        clients = fetch(cfg)
    except Exception as exc:
        sys.exit(
            C.err(f"Could not read from the controller: {exc}\n")
            + C.dim(f"Try: python3 {os.path.basename(__file__)} --reconfigure")
        )

    if args.json:
        selected = clients if args.wired else [c for c in clients if not c["wired"]]
        print(json.dumps([c["_raw"] for c in selected], indent=2, ensure_ascii=False))
    else:
        print_table(clients, args.wired)


if __name__ == "__main__":
    main()
