#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
ESXi Storage Load Analyzer
==========================
Connects to a (standalone) ESXi host through the vSphere API and finds out WHO
is causing storage load and latency on every datastore - iSCSI LUNs (Synology),
local SATA SSD, local NVMe, NFS ...

Sections of the report
  * DATASTORES     IOPS, MB/s, read/write latency, how much of the IO is caused by VMs
  * DEVICES        per LUN / disk: DAVG / KAVG / QAVG / GAVG latency, paths, SCSI aborts
  * NETWORK        vmnic throughput / utilisation / drops (iSCSI network saturation)
  * TOP VMs        which VMs generate the most IO (all datastores)
  * PER DATASTORE  which VMs load each datastore and their share in %
  * VIRTUAL DISKS  the busiest virtual disks
  * CPU & MEMORY   host CPU/RAM, ballooning/compression/swap, TOP VMs by CPU (incl. CPU Ready
                   and Co-Stop) and by memory (consumed/active/balloon/swapped)
  * FINDINGS       automatic diagnosis with hints
  * GUEST INFO     (VMware Tools, read-only) OS, IP, filesystem fill level of every VM
  * IN-GUEST       (optional, SSH, read-only) for Linux VMs: which PROCESS inside the VM
                   reads/writes, per-disk await/util, iowait, PSI pressure, CPU breakdown,
                   RAM available, guest swapping, top processes by CPU and by RAM
  * DASHBOARD      at the end the whole report opens in a fullscreen pywebview window
                   (sortable / filterable tables, charts, live refresh). Helper files are
                   written to ".temp" in the CURRENT working directory and removed on exit.
                   History: the last hour is loaded from ESXi at start, every new 20 s sample
                   is appended (kept "history_hours" in memory) - charts, trends, range table.
                   Live mode can be switched on/off in the window. "Save report" writes all data
                   (current state + history + text report) as JSON for analysis, or as .html.
                   L = live on/off, F5 = refresh, Ctrl+S = save report, F11 = fullscreen, Ctrl+Q = quit.
  * MULTI-HOST     any number of ESXi hosts in one config, queried in parallel:
                   ALL HOSTS SUMMARY + CROSS-HOST section for datastores mounted on more
                   hosts (same Synology LUN) - total load and top VMs over ALL hosts

Guests whose login starts a special CLI/menu: the script first tries a normal
non-interactive command; if no shell answers it opens a PTY, sends the escape keys
(default Ctrl+C, q, Enter - "guest_escape_keys" in JSON) and then runs the read-only
measurement in the shell.

VMs whose SSH requires OTP / 2FA / keyboard-interactive or password login are
ignored (shown as skipped) - the script never prompts for or sends OTP codes.

In-guest analysis is strictly READ-ONLY: only `cat`, `grep`, `hostname`, `sleep`
on /proc and /sys are executed (optionally via `sudo -n` to see processes of all
users). Nothing is written or changed on the guests. Authentication uses an SSH
private key imported on first run (OpenSSH / PEM or PuTTY .ppk v2/v3) and stored
in the JSON (still protected by its passphrase, if it had one). The passphrase is
asked only when SSH is really needed. Guest SSH host keys are NOT checked and NOT
stored anywhere (no known_hosts) - nothing is ever written back into the JSON, so a
read-only configuration from CONFIG_URL works unchanged.

Password prompts show '*' and support paste via right mouse button, Ctrl+V and
Shift+Insert. Special characters via Alt codes (Alt + numpad) work on Windows;
on Linux use the terminal's Unicode input (e.g. Ctrl+Shift+U in GNOME).

Requirements
  pip install pyvmomi colorama
  pip install paramiko             (only for in-guest SSH analysis)
  pip install pywebview            (fullscreen dashboard; skipped with a warning if missing)
  optional: smbprotocol (UNC config on Linux/macOS), argon2-cffi (encrypted PPK v3),
            pyperclip (clipboard for Ctrl+V on Linux if xclip/xsel/wl-paste missing)

Configuration
  First run asks for parameters + credentials and stores them into a JSON file
  with the same name as the script (esxi_storage_analyzer.json) next to it.
  Next runs read the JSON and do not ask again. Use --reconfigure to ask again.

  If CONFIG_URL below is filled in, the configuration is loaded from there FIRST
  (read-only - never written back). If it cannot be loaded, the local JSON is used.
  Supported forms:
    https://server/path/cfg.json      (certificate is NOT verified)
    http://user:pass@server/cfg.json  (basic auth)
    \\server\share\path\cfg.json      (UNC)  or  smb://server/share/path/cfg.json
    file:///C:/cfg/cfg.json, C:\cfg\cfg.json, /etc/esxi/cfg.json  (local path)

Usage
  python esxi_storage_analyzer.py               # mode from config (snapshot / live)
  python esxi_storage_analyzer.py --live        # refresh every 20 s
  python esxi_storage_analyzer.py --snapshot --samples 45   # last 15 minutes
  python esxi_storage_analyzer.py --reconfigure  # all parameters, host menu: a / e N / d N
  python esxi_storage_analyzer.py --add-host     # add another ESXi host
  python esxi_storage_analyzer.py --list-hosts
  python esxi_storage_analyzer.py --hosts esx01,esx02   # only some hosts (name, address or number)
  python esxi_storage_analyzer.py --import-key C:\keys\id_ed25519.ppk
  python esxi_storage_analyzer.py --no-guest     # skip SSH into guests this run
  python esxi_storage_analyzer.py --no-gui       # console report only, no dashboard window

Extra JSON keys (optional, edit by hand):
  "guest_hosts":   {"vm-name": "10.0.0.15", "vm2": ["10.1.0.5", "192.168.5.5"]}
                                               # SSH IPv4 override per VM (one or more, tried in order)
  By default all IPv4 addresses of all NICs reported by VMware Tools are tested
  (in parallel, TCP on the SSH port) and the first reachable one is used.
  "guest_vm_list": ["web01", "db01"]           # used with "guest_scope": "list"
  "host_name":     "esx01.local"               # pick host when connected to vCenter
"""

# =============================================================================
#  CONFIG SOURCE - leave empty to use only the local <script_name>.json
# =============================================================================
CONFIG_URL = r"https://nas.falco81.net/esxi_storage_analyzer.json"
# Optional credentials for UNC / smb:// CONFIG_URL on Linux/macOS (Windows uses
# the logged-on user's credentials automatically)
CONFIG_SMB_USER = ""
CONFIG_SMB_PASSWORD = ""
# =============================================================================

import argparse
import atexit
import base64
import codecs
import getpass
import hashlib
import hmac
import io
import json
import math
import os
import pathlib
import re
import shutil
import ssl
import struct
import subprocess
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:
    print("Missing module 'colorama'. Install it with:  pip install colorama")
    sys.exit(1)

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim, vmodl
except ImportError:
    print("Missing module 'pyvmomi'. Install it with:  pip install pyvmomi")
    sys.exit(1)

colorama_init()

LAUNCH_DIR = os.getcwd()        # .temp is created in the directory the script was started from

REALTIME_INTERVAL = 20          # ESXi real-time statistics granularity (seconds)
KQ_WARN_MS = 2.0                # KAVG / QAVG warning threshold
KQ_CRIT_MS = 5.0                # KAVG / QAVG critical threshold

DEFAULTS = {
    "hosts": [],                 # [{"name", "host", "port", "user", "password_b64"[, "host_name"]}, ...]
    "mode": "snapshot",          # snapshot | live
    "samples": 15,               # snapshot window, 20 s samples (15 = 5 min, max 180 = 1 h)
    "live_samples": 1,           # samples averaged per refresh in live mode
    "top_n": 10,
    "sort_by": "iops",           # iops | throughput | latency
    "latency_warn_ms": 10.0,
    "latency_crit_ms": 30.0,
    "nic_util_warn_pct": 80.0,
    "show_vm_disks": True,
    # ---- in-guest analysis (Linux over SSH, read-only) ----
    "guest_ssh": False,
    "guest_user": "root",
    "guest_port": 22,
    "guest_sudo": False,         # use 'sudo -n' (non-interactive) to see IO of all processes
    "guest_sample_s": 5,         # measuring interval inside the guest
    "guest_scope": "top",        # top | all | list
    "guest_top_n": 5,
    "guest_vm_list": [],
    "guest_hosts": {},
    "guest_in_live": False,
    "guest_timeout_s": 10,
    "guest_probe_timeout_s": 2,  # TCP reachability test per IPv4 (all IPs tested in parallel)
    "guest_ssh_key": "",         # OpenSSH private key text (encrypted if it had a passphrase)
    "guest_ssh_key_info": "",
    "guest_escape_keys": ["^C", "q", "{ENTER}"],   # keys to leave a special login CLI ([] = off)
    "guest_escape_delay_s": 1.0,
    "gui": True,                 # open the fullscreen pywebview dashboard at the end
    "history_hours": 6,          # dashboard keeps per-sample history (20 s) for this many hours (in memory)
    "history_backfill_samples": 180,   # on start load the last N x 20 s from ESXi (180 = 1 h, ESXi keeps 1 h)
}

HOST_COUNTERS = [
    "cpu.usage.average",
    "datastore.numberReadAveraged.average", "datastore.numberWriteAveraged.average",
    "datastore.read.average", "datastore.write.average",
    "datastore.totalReadLatency.average", "datastore.totalWriteLatency.average",
    "disk.numberReadAveraged.average", "disk.numberWriteAveraged.average",
    "disk.read.average", "disk.write.average",
    "disk.deviceLatency.average", "disk.kernelLatency.average",
    "disk.queueLatency.average", "disk.totalLatency.average",
    "disk.commandsAborted.summation", "disk.busResets.summation",
    "net.received.average", "net.transmitted.average",
    "net.droppedRx.summation", "net.droppedTx.summation",
    "net.errorsRx.summation", "net.errorsTx.summation",
    "cpu.usagemhz.average",
    "mem.usage.average", "mem.consumed.average", "mem.active.average", "mem.vmmemctl.average",
    "mem.swapused.average", "mem.swapinRate.average", "mem.swapoutRate.average",
    "mem.compressed.average", "mem.state.latest",
]

VM_COUNTERS = [
    "virtualDisk.numberReadAveraged.average", "virtualDisk.numberWriteAveraged.average",
    "virtualDisk.read.average", "virtualDisk.write.average",
    "virtualDisk.totalReadLatency.average", "virtualDisk.totalWriteLatency.average",
    "datastore.numberReadAveraged.average", "datastore.numberWriteAveraged.average",
    "datastore.read.average", "datastore.write.average",
    "datastore.totalReadLatency.average", "datastore.totalWriteLatency.average",
    "cpu.usage.average", "cpu.usagemhz.average", "cpu.ready.summation", "cpu.costop.summation",
    "mem.active.average", "mem.consumed.average", "mem.vmmemctl.average", "mem.swapped.average",
    "mem.swapinRate.average", "mem.compressed.average",
]


# =============================================================================
#  Output helpers
# =============================================================================
def col(text, color=""):
    return f"{color}{text}{Style.RESET_ALL}" if color else str(text)


def p_info(msg):
    print(col("[i] ", Fore.CYAN) + msg)


def p_ok(msg):
    print(col("[+] ", Fore.GREEN) + msg)


def p_warn(msg):
    print(col("[!] ", Fore.YELLOW) + msg)


def p_err(msg):
    print(col("[x] ", Fore.RED + Style.BRIGHT) + msg)


def section(title):
    print()
    print(col(f"=== {title} " + "=" * max(3, 90 - len(title)), Style.BRIGHT + Fore.CYAN))


def trunc(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 2] + ".."


def f_num(v, d=1):
    return "-" if v is None else f"{v:,.{d}f}"


def f_int(v):
    return "-" if v is None else f"{v:,.0f}"


def f_pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


def print_table(headers, rows, align=None, indent="  "):
    """rows: list of lists; a cell is either text or (text, color)."""
    if not rows:
        print(indent + col("(no data)", Style.DIM))
        return
    norm = [[(str(c[0]), c[1]) if isinstance(c, tuple) else (str(c), "") for c in r] for r in rows]
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    widths = [len(h) for h in headers]
    for r in norm:
        for i, (t, _) in enumerate(r):
            widths[i] = max(widths[i], len(t))

    def pad(t, i):
        return t.ljust(widths[i]) if align[i] == "l" else t.rjust(widths[i])

    print(indent + "  ".join(col(pad(h, i), Style.BRIGHT) for i, h in enumerate(headers)))
    print(indent + "  ".join("-" * w for w in widths))
    for r in norm:
        print(indent + "  ".join(col(pad(t, i), c) for i, (t, c) in enumerate(r)))


def lat_color(v, cfg):
    if v is None:
        return Style.DIM
    if v >= cfg["latency_crit_ms"]:
        return Fore.RED + Style.BRIGHT
    if v >= cfg["latency_warn_ms"]:
        return Fore.YELLOW
    return Fore.GREEN


def lat_cell(v, cfg):
    return (f_num(v, 1), lat_color(v, cfg))


def kq_cell(v):
    if v is None:
        return ("-", Style.DIM)
    color = Fore.RED + Style.BRIGHT if v >= KQ_CRIT_MS else Fore.YELLOW if v >= KQ_WARN_MS else Fore.GREEN
    return (f_num(v, 1), color)


# =============================================================================
#  Masked password input  (* echo, paste: right click / Ctrl+V / Shift+Insert,
#  Alt codes on Windows, any Unicode character)
# =============================================================================
def _clipboard_text():
    try:
        import pyperclip
        t = pyperclip.paste()
        if t:
            return t
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            u32.GetClipboardData.restype = ctypes.c_void_p
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            if not u32.OpenClipboard(None):
                return ""
            try:
                h = u32.GetClipboardData(13)          # CF_UNICODETEXT
                if not h:
                    return ""
                ptr = k32.GlobalLock(h)
                try:
                    return ctypes.wstring_at(ptr) if ptr else ""
                finally:
                    k32.GlobalUnlock(h)
            finally:
                u32.CloseClipboard()
        except Exception:
            return ""
    for cmd in (["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"], ["pbpaste"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, capture_output=True, timeout=3).stdout.decode("utf-8", "replace")
            except Exception:
                pass
    return ""


def masked_input(prompt):
    """Password prompt that echoes '*'. Falls back to getpass when not on a TTY."""
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)
    out = sys.stdout
    out.write(prompt)
    out.flush()
    buf = []

    def add(text):
        for ch in text:
            if ch in "\r\n":
                break
            if ch.isprintable():
                buf.append(ch)
                out.write("*")
        out.flush()

    def back():
        if buf:
            buf.pop()
            out.write("\b \b")
            out.flush()

    def clear():
        while buf:
            back()

    def paste():
        t = _clipboard_text()
        add(t.splitlines()[0] if t else "")

    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()          # Alt+numpad codes arrive here already composed
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x08":
                back()
            elif ch == "\x16":            # Ctrl+V
                paste()
            elif ch == "\x15":            # Ctrl+U
                clear()
            elif ch in ("\x00", "\xe0"):  # special key, next char = scan code
                if msvcrt.getwch() == "R":    # Insert / Shift+Insert
                    paste()
            elif ch == "\x1b":
                continue
            else:
                add(ch)                   # right-click paste arrives as typed characters
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            tty.setraw(fd)
            while True:
                b = os.read(fd, 1)
                if not b:
                    break
                if b == b"\x1b":          # escape sequence
                    seq = b""
                    while select.select([fd], [], [], 0.05)[0] and len(seq) < 12:
                        c = os.read(fd, 1)
                        seq += c
                        if len(seq) > 1 and 0x40 <= c[0] <= 0x7E:
                            break
                    if seq in (b"[200~", b"[201~"):     # bracketed paste markers
                        continue
                    if seq.startswith(b"[2") and seq.endswith(b"~"):   # Insert / Shift+Insert
                        paste()
                    continue
                ch = dec.decode(b)
                if not ch:
                    continue
                if ch in ("\r", "\n"):
                    break
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch in ("\x7f", "\x08"):
                    back()
                elif ch == "\x16":
                    paste()
                elif ch == "\x15":
                    clear()
                else:
                    add(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    out.write("\n")
    out.flush()
    return "".join(buf)


# =============================================================================
#  Configuration
# =============================================================================
def script_base():
    if getattr(sys, "frozen", False):          # PyInstaller exe
        return os.path.splitext(os.path.abspath(sys.executable))[0]
    return os.path.splitext(os.path.abspath(__file__))[0]


def local_config_path():
    return script_base() + ".json"


def encode_pw(pw):
    return base64.b64encode(pw.encode("utf-8")).decode("ascii") if pw else ""


def get_password(cfg):
    if cfg.get("password"):                     # plain password allowed (e.g. remote JSON)
        return str(cfg["password"])
    if cfg.get("password_b64"):
        try:
            return base64.b64decode(cfg["password_b64"]).decode("utf-8")
        except Exception:
            return ""
    return ""


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def read_config_source(src):
    """Read JSON text from http(s) / UNC / smb:// / file:// / local path (read-only)."""
    s = src.strip().strip('"')
    low = s.lower()

    # --- HTTP / HTTPS (no certificate verification) ---------------------------
    if low.startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(s)
        headers = {"User-Agent": "esxi-storage-analyzer"}
        if parts.username:
            user = urllib.parse.unquote(parts.username)
            pwd = urllib.parse.unquote(parts.password or "")
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
            netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
            s = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(s, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return resp.read().decode("utf-8-sig")

    # --- file:// ----------------------------------------------------------------
    if low.startswith("file://"):
        parts = urllib.parse.urlsplit(s)
        if parts.netloc and parts.netloc.lower() != "localhost":
            s = "\\\\" + parts.netloc + urllib.parse.unquote(parts.path).replace("/", "\\")
        else:
            return _read_bytes(urllib.request.url2pathname(parts.path)).decode("utf-8-sig")

    # --- smb:// -> UNC -----------------------------------------------------------
    if s.lower().startswith("smb://"):
        parts = urllib.parse.urlsplit(s)
        s = "\\\\" + parts.hostname + urllib.parse.unquote(parts.path).replace("/", "\\")

    # --- UNC ----------------------------------------------------------------------
    if s.startswith("\\\\") or s.startswith("//"):
        unc = s.replace("/", "\\")
        if os.name == "nt":
            return _read_bytes(unc).decode("utf-8-sig")
        try:
            import smbclient  # from package smbprotocol
        except ImportError:
            raise RuntimeError("UNC path on Linux/macOS needs 'smbprotocol' (pip install smbprotocol)")
        server = unc.lstrip("\\").split("\\")[0]
        if CONFIG_SMB_USER:
            smbclient.register_session(server, username=CONFIG_SMB_USER, password=CONFIG_SMB_PASSWORD)
        with smbclient.open_file(unc, mode="rb") as fh:
            return fh.read().decode("utf-8-sig")

    # --- local path -------------------------------------------------------------
    return _read_bytes(os.path.expandvars(os.path.expanduser(s))).decode("utf-8-sig")


def to_bool(text):
    t = str(text).strip().lower()
    if t in ("y", "yes", "true", "1", "a", "ano"):
        return True
    if t in ("n", "no", "false", "0", "ne"):
        return False
    raise ValueError(text)


def ask(label, default=None, cast=str, required=False, choices=None):
    while True:
        shown = "" if default in (None, "") else f" [{default}]"
        raw = input(col(f"  {label}{shown}: ", Fore.CYAN)).strip()
        if not raw:
            if default not in (None, ""):
                raw = str(default)
            elif required:
                p_warn("Value is required.")
                continue
            else:
                return default
        try:
            val = cast(raw)
        except (ValueError, TypeError):
            p_warn("Invalid value, try again.")
            continue
        if choices and val not in choices:
            p_warn(f"Choose one of: {', '.join(choices)}")
            continue
        return val


HOST_KEYS = ("name", "host", "port", "user", "password", "password_b64", "host_name")


def normalize_hosts(cfg):
    """Migrate old single-host config (top-level host/user/password) to the 'hosts' list."""
    hosts = [dict(h) for h in (cfg.get("hosts") or []) if isinstance(h, dict) and h.get("host")]
    if not hosts and cfg.get("host"):
        hosts.append({k: cfg[k] for k in HOST_KEYS if k in cfg})
    for h in hosts:
        h.setdefault("port", 443)
        h.setdefault("user", "root")
        h.setdefault("name", h["host"])
    for k in HOST_KEYS:
        cfg.pop(k, None)
    cfg["hosts"] = hosts
    return cfg


def host_label(h):
    return h.get("name") or h.get("host", "?")


def prompt_host(h=None):
    h = dict(h or {})
    print(col("  -- ESXi host --", Style.BRIGHT))
    h["host"] = ask("ESXi host (IP or FQDN)", h.get("host"), str, required=True)
    h["name"] = ask("Display name", h.get("name") or h["host"], str)
    h["port"] = ask("HTTPS port", h.get("port", 443), int)
    h["user"] = ask("Username", h.get("user", "root"), str, required=True)
    old_pw = get_password(h)
    while True:
        pw = masked_input(col("  Password" + (" [Enter = keep saved]" if old_pw else "") + ": ", Fore.CYAN))
        if pw or old_pw:
            break
        p_warn("Password is required.")
    h["password_b64"] = encode_pw(pw or old_pw)
    h.pop("password", None)
    return h


def print_hosts(hosts):
    if not hosts:
        print(col("  (no ESXi host configured)", Style.DIM))
    for i, h in enumerate(hosts, 1):
        print(f"  {col(str(i) + '.', Style.BRIGHT)} {host_label(h)}  ({h['user']}@{h['host']}:{h.get('port', 443)})")


def add_hosts_loop(hosts):
    """Interactive first-run style: keep asking for more ESXi hosts."""
    hosts = [dict(h) for h in hosts or []]
    if not hosts:
        print(col("\n  ESXi host #1", Style.BRIGHT + Fore.CYAN))
        hosts.append(prompt_host())
    while True:
        print(col(f"\n  Configured ESXi hosts ({len(hosts)}):", Style.BRIGHT))
        print_hosts(hosts)
        if not ask("Add another ESXi host? (y/n)", "n", to_bool):
            return hosts
        print(col(f"\n  ESXi host #{len(hosts) + 1}", Style.BRIGHT + Fore.CYAN))
        hosts.append(prompt_host())


def manage_hosts(hosts):
    hosts = [dict(h) for h in hosts or []]
    if not hosts:
        return add_hosts_loop(hosts)
    while True:
        print(col("\n  Configured ESXi hosts:", Style.BRIGHT))
        print_hosts(hosts)
        a = (ask("[a]dd, [e]dit N, [d]elete N, Enter = done", "", str) or "").strip().lower()
        if not a:
            if hosts:
                return hosts
            p_warn("At least one ESXi host is required.")
            continue
        m = re.match(r"^([aed])\s*(\d*)$", a)
        if not m:
            p_warn("Unknown choice.")
            continue
        act, num = m.group(1), int(m.group(2)) if m.group(2) else None
        if act == "a":
            hosts.append(prompt_host())
        elif num is None or not 1 <= num <= len(hosts):
            p_warn(f"Give a host number 1-{len(hosts)}, e.g. 'e 2'.")
        elif act == "e":
            hosts[num - 1] = prompt_host(hosts[num - 1])
        else:
            p_info(f"Removed {host_label(hosts.pop(num - 1))}")


def prompt_config(d):
    print()
    print(col("Configuration - enter parameters (Enter = keep value in brackets)", Style.BRIGHT))
    cfg = dict(d)
    cfg["hosts"] = manage_hosts(d.get("hosts"))
    cfg["mode"] = ask("Default mode (snapshot / live)", d.get("mode", "snapshot"), lambda x: x.lower(),
                      choices=["snapshot", "live"])
    cfg["samples"] = max(1, min(180, ask("Snapshot window in 20 s samples (15 = 5 min, max 180)",
                                         d.get("samples", 15), int)))
    cfg["live_samples"] = max(1, min(15, ask("Samples averaged per live refresh (1 = 20 s)",
                                             d.get("live_samples", 1), int)))
    cfg["top_n"] = max(1, ask("Top N rows per table", d.get("top_n", 10), int))
    cfg["sort_by"] = ask("Sort consumers by (iops / throughput / latency)", d.get("sort_by", "iops"),
                         lambda x: x.lower(), choices=["iops", "throughput", "latency"])
    cfg["latency_warn_ms"] = ask("Latency WARNING threshold ms", d.get("latency_warn_ms", 10.0), float)
    cfg["latency_crit_ms"] = ask("Latency CRITICAL threshold ms", d.get("latency_crit_ms", 30.0), float)
    cfg["nic_util_warn_pct"] = ask("vmnic utilisation warning %", d.get("nic_util_warn_pct", 80.0), float)
    cfg["show_vm_disks"] = ask("Show busiest virtual disks table (y/n)",
                               "y" if d.get("show_vm_disks", True) else "n", to_bool)
    cfg["gui"] = ask("Open fullscreen dashboard (pywebview) at the end (y/n)",
                     "y" if d.get("gui", True) else "n", to_bool)

    print(col("\n  In-guest analysis of Linux VMs over SSH (read-only, nothing is changed on the VMs)",
              Style.BRIGHT))
    cfg["guest_ssh"] = ask("Enable in-guest SSH analysis (y/n)", "y" if d.get("guest_ssh") else "n", to_bool)
    if cfg["guest_ssh"]:
        cfg["guest_user"] = ask("SSH user on guests", d.get("guest_user", "root"), str, required=True)
        cfg["guest_port"] = ask("SSH port", d.get("guest_port", 22), int)
        if cfg["guest_user"] != "root":
            cfg["guest_sudo"] = ask("Use 'sudo -n' to see IO of all processes (y/n)",
                                    "y" if d.get("guest_sudo") else "n", to_bool)
        cfg["guest_sample_s"] = max(2, min(60, ask("Measuring interval inside guest (s)",
                                                   d.get("guest_sample_s", 5), int)))
        cfg["guest_scope"] = ask("Which VMs: top (busiest) / all (all Linux) / list", d.get("guest_scope", "top"),
                                 lambda x: x.lower(), choices=["top", "all", "list"])
        if cfg["guest_scope"] == "top":
            cfg["guest_top_n"] = max(1, ask("How many busiest Linux VMs", d.get("guest_top_n", 5), int))
        elif cfg["guest_scope"] == "list":
            cur = ", ".join(d.get("guest_vm_list") or [])
            cfg["guest_vm_list"] = [x.strip() for x in ask("VM names (comma separated)", cur, str,
                                                           required=True).split(",") if x.strip()]
        cur = ",".join(d.get("guest_escape_keys") or [])
        raw = ask("Keys to leave a special login CLI (comma separated, ^C=Ctrl+C, {ENTER}; '-' = off)",
                  cur or "-", str)
        cfg["guest_escape_keys"] = [] if raw.strip() in ("-", "") else [x.strip() for x in raw.split(",") if x.strip()]
        cfg["guest_in_live"] = ask("Run in-guest analysis also in live mode (y/n)",
                                   "y" if d.get("guest_in_live") else "n", to_bool)
        if d.get("guest_ssh_key"):
            p_info(f"Stored SSH key: {d.get('guest_ssh_key_info') or 'present'}")
            if ask("Import a different private key (y/n)", "n", to_bool):
                import_key_interactive(cfg)
        else:
            import_key_interactive(cfg)
        if not cfg.get("guest_ssh_key"):
            p_warn("No SSH key imported - in-guest analysis disabled.")
            cfg["guest_ssh"] = False
    return cfg


def save_config(cfg, path):
    data = {k: v for k, v in cfg.items()
            if k not in ("password", "guest_known_hosts") and not k.startswith("_")}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        if os.name != "nt":
            os.chmod(path, 0o600)
        p_ok(f"Configuration saved to {path}")
        p_warn("Password is stored base64-obfuscated, NOT encrypted - protect the file.")
    except OSError as e:
        p_err(f"Cannot save configuration to {path}: {e}")


def load_config(args):
    """Returns (cfg, readonly_source_flag)."""
    cfg, readonly = None, False
    local = local_config_path()

    if CONFIG_URL.strip():
        try:
            cfg = json.loads(read_config_source(CONFIG_URL))
            readonly = True
            p_ok(f"Configuration loaded (read-only) from {CONFIG_URL}")
            if args.reconfigure:
                p_warn("--reconfigure ignored: CONFIG_URL has priority and is read-only.")
        except Exception as e:
            p_warn(f"Cannot load CONFIG_URL ({e}) - falling back to local configuration.")
            cfg = None

    if cfg is None and not args.reconfigure and os.path.isfile(local):
        try:
            with open(local, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
            p_ok(f"Configuration loaded from {local}")
        except Exception as e:
            p_warn(f"Local configuration {local} is unreadable ({e}).")
            cfg = None

    if cfg is None:
        existing = {}
        if os.path.isfile(local):
            try:
                with open(local, encoding="utf-8-sig") as fh:
                    existing = json.load(fh)
            except Exception:
                existing = {}
        cfg = prompt_config(normalize_hosts({**DEFAULTS, **existing}))
        if args.import_key and import_key_from_path(cfg, args.import_key):
            cfg["guest_ssh"] = True
        save_config(cfg, local)
        cfg["_readonly"] = False
        return cfg, False

    had_old_format = "host" in cfg and not cfg.get("hosts")
    merged = normalize_hosts({**DEFAULTS, **cfg})
    changed = had_old_format
    if had_old_format:
        p_info("Old single-host configuration converted to the multi-host format.")
        if not readonly and not args.add_host:          # treat as first run of the multi-host version
            merged["hosts"] = add_hosts_loop(merged["hosts"])
    if args.add_host:
        if readonly:
            p_warn("--add-host ignored: configuration comes from read-only CONFIG_URL.")
        else:
            merged["hosts"] = manage_hosts(merged["hosts"] + [prompt_host()])
            changed = True
    if not merged["hosts"]:
        p_warn("No ESXi host configured.")
        merged["hosts"] = manage_hosts([])
        changed = True
    for h in merged["hosts"]:
        if not get_password(h):
            h["password_b64"] = encode_pw(masked_input(
                col(f"  Password for {h['user']}@{host_label(h)}: ", Fore.CYAN)))
            changed = True
    if args.import_key:
        if import_key_from_path(merged, args.import_key):
            merged["guest_ssh"] = True
            changed = True
    elif merged.get("guest_ssh") and not merged.get("guest_ssh_key") and not readonly:
        p_warn("In-guest SSH analysis is enabled but no private key is stored.")
        import_key_interactive(merged)
        changed = True
    if changed:
        if readonly:
            p_info("Missing values entered interactively - remote config is read-only, nothing saved.")
        else:
            save_config(merged, local)
    merged["_readonly"] = readonly
    return merged, readonly


# =============================================================================
#  vSphere helpers
# =============================================================================
def connect(cfg):
    host = cfg["host"].strip().replace("https://", "").replace("http://", "").strip("/")
    kwargs = dict(host=host, user=cfg["user"], pwd=get_password(cfg), port=int(cfg.get("port", 443)))
    try:
        si = SmartConnect(disableSslCertValidation=True, **kwargs)
    except TypeError:                          # older pyVmomi
        si = SmartConnect(sslContext=ssl._create_unverified_context(), **kwargs)
    atexit.register(Disconnect, si)
    return si


def get_props(si, vimtype, props):
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        tspec = vmodl.query.PropertyCollector.TraversalSpec(name="tv", path="view", skip=False,
                                                            type=vim.view.ContainerView)
        ospec = vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[tspec])
        pspec = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=props, all=False)
        fspec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[ospec], propSet=[pspec])
        result = content.propertyCollector.RetrieveContents([fspec])
    finally:
        view.Destroy()
    out = []
    for obj in result or []:
        d = {"obj": obj.obj}
        for p in obj.propSet or []:
            d[p.name] = p.val
        out.append(d)
    return out


def get_host(si, cfg):
    hosts = get_props(si, vim.HostSystem, ["name"])
    if not hosts:
        raise RuntimeError("No ESXi host found on this endpoint.")
    want = cfg.get("host_name")
    if want:
        for h in hosts:
            if h["name"] == want:
                return h["obj"]
    if len(hosts) > 1:
        p_warn(f"Endpoint manages {len(hosts)} hosts (vCenter?). Using '{hosts[0]['name']}'. "
               f"Add \"host_name\" to the JSON to pick another one.")
    return hosts[0]["obj"]


def classify_device(lun, hints):
    h = " ".join(x for x in hints if x).lower()
    cn = (lun.canonicalName or "").lower()
    vendor = (getattr(lun, "vendor", "") or "").strip().lower()
    model = (getattr(lun, "model", "") or "").strip().lower()
    disp = (lun.displayName or "").lower()
    if "internetscsi" in h or vendor == "synology":
        return "iSCSI"
    if "fibrechannel" in h:
        return "FC"
    if ("nvme" in h or "pcie" in h or cn.startswith("t10.nvme") or cn.startswith("eui.")
            or "nvme" in model or "nvme" in disp):
        return "NVMe"
    if cn.startswith("t10.ata") or vendor == "ata" or "sata" in disp:
        return "SATA"
    if "serialattached" in h or "sas" in disp:
        return "SAS"
    if "usb" in h or "usb" in disp:
        return "USB"
    if "block" in h or "parallel" in h:
        return "Local"
    return "Other"


def ctrl_prefix(ctrl):
    n = type(ctrl).__name__.lower()
    if "nvme" in n:
        return "nvme"
    if "ahci" in n or "sata" in n:
        return "sata"
    if "ide" in n:
        return "ide"
    return "scsi"


def count_snapshots(snapinfo):
    if not snapinfo or not getattr(snapinfo, "rootSnapshotList", None):
        return 0, None
    cnt, oldest, stack = 0, None, list(snapinfo.rootSnapshotList)
    while stack:
        s = stack.pop()
        cnt += 1
        if s.createTime and (oldest is None or s.createTime < oldest):
            oldest = s.createTime
        stack.extend(s.childSnapshotList or [])
    return cnt, oldest


def collect_inventory(si, host):
    inv = {"devices": {}, "datastores": {}, "ds_by_name": {}, "vms": {}, "nics": {}}
    hc = host.config
    inv["host_name"] = host.name
    inv["product"] = hc.product.fullName if hc and hc.product else "?"
    try:
        hw = host.hardware
        inv["hw"] = {"mem_bytes": hw.memorySize, "cores": hw.cpuInfo.numCpuCores,
                     "threads": hw.cpuInfo.numCpuThreads, "sockets": hw.cpuInfo.numCpuPackages,
                     "mhz": hw.cpuInfo.hz / 1e6, "cpu_model": (hw.cpuPkg[0].description if hw.cpuPkg else "")}
    except Exception:
        inv["hw"] = {}
    try:
        qs = host.summary.quickStats
        inv["quick"] = {"cpu_mhz": qs.overallCpuUsage, "mem_mb": qs.overallMemoryUsage}
    except Exception:
        inv["quick"] = {}
    sd = hc.storageDevice

    luns = {l.key: l for l in (sd.scsiLun or [])}
    hba_type = {h.key: type(h).__name__ for h in (sd.hostBusAdapter or [])}
    hints = defaultdict(set)
    paths = {}

    mp = getattr(sd, "multipathInfo", None)
    if mp:
        for ml in mp.lun or []:
            st = {"total": 0, "active": 0, "dead": 0}
            for p in ml.path or []:
                st["total"] += 1
                state = (getattr(p, "pathState", None) or getattr(p, "state", "") or "").lower()
                if state == "active":
                    st["active"] += 1
                elif state == "dead":
                    st["dead"] += 1
                if p.transport is not None:
                    hints[ml.lun].add(type(p.transport).__name__)
                if getattr(p, "adapter", None):
                    hints[ml.lun].add(hba_type.get(p.adapter, ""))
            paths[ml.lun] = st

    pst = getattr(sd, "plugStoreTopology", None)
    if pst:
        topo_ad = {a.key: a.adapter for a in (pst.adapter or [])}
        dev_lun = {d.key: d.lun for d in (pst.device or [])}
        pcount = defaultdict(int)
        for p in pst.path or []:
            lk = dev_lun.get(p.device)
            if lk:
                hints[lk].add(hba_type.get(topo_ad.get(p.adapter), ""))
                pcount[lk] += 1
        for lk, n in pcount.items():
            paths.setdefault(lk, {"total": n, "active": None, "dead": None})

    for key, l in luns.items():
        if getattr(l, "lunType", "") != "disk":
            continue
        cap = None
        if getattr(l, "capacity", None):
            cap = l.capacity.block * l.capacity.blockSize / 1024 ** 3
        inv["devices"][l.canonicalName] = {
            "name": l.canonicalName,
            "display": l.displayName or l.canonicalName,
            "vendor": (l.vendor or "").strip(),
            "model": (l.model or "").strip(),
            "type": classify_device(l, hints.get(key, set())),
            "paths": paths.get(key),
            "capacity_gb": cap,
            "datastores": [],
        }

    for ds in host.datastore or []:
        try:
            s, info = ds.summary, ds.info
        except Exception:
            continue
        url = s.url or ""
        uuid = url.rstrip("/").split("/")[-1] if url else ds._moId
        extents = []
        if isinstance(info, vim.host.VmfsDatastoreInfo) and info.vmfs:
            extents = [e.diskName for e in (info.vmfs.extent or [])]
        types = sorted({inv["devices"][e]["type"] for e in extents if e in inv["devices"]})
        dtype = "/".join(types) if types else (s.type or "?")
        if (s.type or "").upper().startswith("NFS"):
            dtype = "NFS"
        rec = {"name": s.name, "uuid": uuid, "fs": s.type, "type": dtype, "capacity": s.capacity or 0,
               "free": s.freeSpace or 0, "accessible": s.accessible, "extents": extents}
        inv["datastores"][uuid] = rec
        inv["ds_by_name"][s.name] = rec
        for e in extents:
            if e in inv["devices"]:
                inv["devices"][e]["datastores"].append(s.name)

    try:
        for pn in hc.network.pnic or []:
            inv["nics"][pn.device] = {"speed": pn.linkSpeed.speedMb if pn.linkSpeed else 0,
                                      "driver": pn.driver or ""}
    except Exception:
        pass

    props = ["name", "runtime.powerState", "runtime.host", "config.hardware.device", "config.template",
             "snapshot", "runtime.consolidationNeeded", "config.guestFullName",
             "guest.toolsRunningStatus", "guest.toolsVersionStatus2", "guest.guestFamily", "guest.guestFullName", "guest.hostName",
             "guest.ipAddress", "guest.net", "guest.disk", "config.hardware.numCPU", "config.hardware.memoryMB"]
    for r in get_props(si, vim.VirtualMachine, props):
        rh = r.get("runtime.host")
        if rh is not None and rh._moId != host._moId:
            continue
        devs = r.get("config.hardware.device") or []
        ctrls = {d.key: d for d in devs if isinstance(d, vim.vm.device.VirtualController)}
        disks = {}
        for d in devs:
            if not isinstance(d, vim.vm.device.VirtualDisk):
                continue
            c = ctrls.get(d.controllerKey)
            inst = f"{ctrl_prefix(c)}{c.busNumber}:{d.unitNumber}" if c is not None else str(d.key)
            fn = getattr(d.backing, "fileName", "") or ""
            dsn = fn[1:fn.index("]")] if fn.startswith("[") and "]" in fn else "?"
            disks[inst] = {"label": d.deviceInfo.label if d.deviceInfo else inst, "file": fn, "ds": dsn,
                           "size_gb": (d.capacityInKB or 0) / 1024 ** 2}
        snaps, oldest = count_snapshots(r.get("snapshot"))
        inv["vms"][r["obj"]._moId] = {
            "obj": r["obj"], "name": r.get("name", "?"), "power": str(r.get("runtime.powerState")),
            "template": bool(r.get("config.template")), "disks": disks, "snapshots": snaps,
            "vcpu": r.get("config.hardware.numCPU") or 0, "mem_mb": r.get("config.hardware.memoryMB") or 0,
            "oldest_snapshot": oldest, "consolidate": bool(r.get("runtime.consolidationNeeded")),
            "guest": {
                "tools": str(r.get("guest.toolsRunningStatus") or ""),
                "tools_version": str(r.get("guest.toolsVersionStatus2") or ""),
                "family": str(r.get("guest.guestFamily") or ""),
                "os": r.get("guest.guestFullName") or r.get("config.guestFullName") or "",
                "hostname": r.get("guest.hostName") or "",
                "ips": pick_ips(r.get("guest.ipAddress"), r.get("guest.net")),
                "fs": [(fs.diskPath, fs.capacity or 0, fs.freeSpace or 0) for fs in (r.get("guest.disk") or [])],
            },
        }
    return inv


IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _usable_ipv4(ip):
    m = IPV4_RE.match(ip or "")
    return bool(m) and all(int(x) <= 255 for x in m.groups()) \
        and not ip.startswith(("127.", "169.254.", "0.")) and ip != "255.255.255.255"


def pick_ips(primary, nets):
    """All usable IPv4 addresses reported by VMware Tools (primary first, all NICs, no duplicates)."""
    cands = [primary] if primary else []
    for n in nets or []:
        cands.extend(n.ipAddress or [])
        ipcfg = getattr(n, "ipConfig", None)
        for a in (getattr(ipcfg, "ipAddress", None) or []):
            cands.append(a.ipAddress)
    out = []
    for ip in cands:
        if _usable_ipv4(ip) and ip not in out:
            out.append(ip)
    return out


def is_linux_guest(v):
    g = v.get("guest", {})
    if "linux" in g.get("family", "").lower():
        return True
    osn = g.get("os", "").lower()
    return any(x in osn for x in ("linux", "ubuntu", "debian", "centos", "red hat", "rhel", "suse", "rocky",
                                  "alma", "photon", "fedora"))


# =============================================================================
#  Performance data
# =============================================================================
class Perf:
    def __init__(self, si):
        self.pm = si.RetrieveContent().perfManager
        self.by_name, self.by_id = {}, {}
        for c in self.pm.perfCounter:
            name = f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}"
            self.by_name[name] = c.key
            self.by_id[c.key] = name

    def _run(self, specs, out):
        for em in self.pm.QueryPerf(querySpec=specs) or []:
            d = out.setdefault(em.entity._moId, {})
            d["__ts__"] = [int(round(si.timestamp.timestamp() / REALTIME_INTERVAL)) * REALTIME_INTERVAL
                           for si in (em.sampleInfo or [])]
            for s in em.value or []:
                n = self.by_id.get(s.id.counterId)
                if n:
                    d.setdefault(n, {})[s.id.instance] = list(s.value)

    def query(self, entities, counters, samples):
        out = {}
        ids = [vim.PerformanceManager.MetricId(counterId=self.by_name[n], instance="*")
               for n in counters if n in self.by_name]
        if not ids or not entities:
            return out
        for i in range(0, len(entities), 40):
            chunk = entities[i:i + 40]
            specs = [vim.PerformanceManager.QuerySpec(entity=e, metricId=ids, intervalId=REALTIME_INTERVAL,
                                                      maxSample=samples) for e in chunk]
            try:
                self._run(specs, out)
            except Exception:
                for sp in specs:               # retry one by one, skip broken entities
                    try:
                        self._run([sp], out)
                    except Exception:
                        pass
        return out


def series(m, counter, inst):
    return (m.get(counter) or {}).get(inst) or []


def s_avg(vals):
    v = [x for x in vals if x is not None and x >= 0]
    return sum(v) / len(v) if v else None


def s_max(vals):
    v = [x for x in vals if x is not None and x >= 0]
    return max(v) if v else None


def s_sum(vals):
    v = [x for x in vals if x is not None and x >= 0]
    return sum(v) if v else None


def wlat(rl, wl, ri, wi):
    ri, wi = ri or 0, wi or 0
    if ri + wi > 0 and (rl is not None or wl is not None):
        return ((rl or 0) * ri + (wl or 0) * wi) / (ri + wi)
    vals = [x for x in (rl, wl) if x is not None]
    return max(vals) if vals else None


def finish_block(b):
    b["iops"] = (b["r_iops"] or 0) + (b["w_iops"] or 0)
    b["mbs"] = (b["r_mbs"] or 0) + (b["w_mbs"] or 0)
    b["lat"] = wlat(b["r_lat"], b["w_lat"], b["r_iops"], b["w_iops"])
    return b


def io_block(m, g, inst):
    rk = s_avg(series(m, f"{g}.read.average", inst))
    wk = s_avg(series(m, f"{g}.write.average", inst))
    maxes = [x for x in (s_max(series(m, f"{g}.totalReadLatency.average", inst)),
                         s_max(series(m, f"{g}.totalWriteLatency.average", inst))) if x is not None]
    return finish_block({
        "r_iops": s_avg(series(m, f"{g}.numberReadAveraged.average", inst)),
        "w_iops": s_avg(series(m, f"{g}.numberWriteAveraged.average", inst)),
        "r_mbs": rk / 1024 if rk is not None else None,
        "w_mbs": wk / 1024 if wk is not None else None,
        "r_lat": s_avg(series(m, f"{g}.totalReadLatency.average", inst)),
        "w_lat": s_avg(series(m, f"{g}.totalWriteLatency.average", inst)),
        "max_lat": max(maxes) if maxes else None,
    })


def combine(blocks):
    blocks = list(blocks)
    tot = {k: sum((b[k] or 0) for b in blocks) for k in ("r_iops", "w_iops", "r_mbs", "w_mbs")}
    for side in ("r", "w"):
        w = sum((b[f"{side}_iops"] or 0) for b in blocks)
        if w > 0:
            tot[f"{side}_lat"] = sum((b[f"{side}_lat"] or 0) * (b[f"{side}_iops"] or 0) for b in blocks) / w
        else:
            vals = [b[f"{side}_lat"] for b in blocks if b[f"{side}_lat"] is not None]
            tot[f"{side}_lat"] = max(vals) if vals else None
    maxes = [b["max_lat"] for b in blocks if b.get("max_lat") is not None]
    tot["max_lat"] = max(maxes) if maxes else None
    return finish_block(tot)


def analyze(inv, perf, host, samples):
    hm = perf.query([host], HOST_COUNTERS, samples).get(host._moId, {})
    running = [v for v in inv["vms"].values() if v["power"] == "poweredOn" and not v["template"]]
    vmm = perf.query([v["obj"] for v in running], VM_COUNTERS, samples)

    res = {"time": datetime.now(), "samples": samples, "ds": {}, "dev": {}, "nic": {}, "vm": {},
           "vdisks": [], "consumers": defaultdict(list)}
    cpu = s_avg(series(hm, "cpu.usage.average", ""))
    res["cpu"] = cpu / 100 if cpu is not None else None
    res["has_host_data"] = bool(hm)

    # ---- host CPU / memory ----
    def hv(c, inst=""):
        return s_avg(series(hm, c, inst))
    hw, qk = inv.get("hw", {}), inv.get("quick", {})
    tot_mhz = (hw.get("cores") or 0) * (hw.get("mhz") or 0)
    used_mhz = hv("cpu.usagemhz.average")
    if used_mhz is None:
        used_mhz = qk.get("cpu_mhz")
    mem_total_gb = (hw.get("mem_bytes") or 0) / 1024 ** 3
    consumed_kb = hv("mem.consumed.average")
    mem_used_gb = consumed_kb / 1024 ** 2 if consumed_kb is not None else \
        (qk.get("mem_mb") / 1024 if qk.get("mem_mb") is not None else None)
    state = s_max(series(hm, "mem.state.latest", ""))
    res["hostcpu"] = {"pct": res["cpu"] / 100 if res["cpu"] is not None else None,
                      "used_mhz": used_mhz, "total_mhz": tot_mhz,
                      "peak_pct": (s_max(series(hm, "cpu.usage.average", "")) or 0) / 10000 or None}   # 1/100 % -> fraction
    res["hostmem"] = {
        "total_gb": mem_total_gb, "used_gb": mem_used_gb,
        "pct": mem_used_gb / mem_total_gb if mem_used_gb is not None and mem_total_gb else None,
        "active_gb": (hv("mem.active.average") or 0) / 1024 ** 2,
        "balloon_gb": (hv("mem.vmmemctl.average") or 0) / 1024 ** 2,
        "swapused_gb": (hv("mem.swapused.average") or 0) / 1024 ** 2,
        "swapin_kbs": hv("mem.swapinRate.average") or 0, "swapout_kbs": hv("mem.swapoutRate.average") or 0,
        "compressed_gb": (hv("mem.compressed.average") or 0) / 1024 ** 2,
        "state": {0: "high", 1: "clear", 2: "soft", 3: "hard", 4: "low"}.get(int(state)) if state is not None else None,
    }

    for uuid in inv["datastores"]:
        res["ds"][uuid] = io_block(hm, "datastore", uuid)

    for cn in inv["devices"]:
        rk = s_avg(series(hm, "disk.read.average", cn))
        wk = s_avg(series(hm, "disk.write.average", cn))
        res["dev"][cn] = {
            "r_iops": s_avg(series(hm, "disk.numberReadAveraged.average", cn)),
            "w_iops": s_avg(series(hm, "disk.numberWriteAveraged.average", cn)),
            "mbs": ((rk or 0) + (wk or 0)) / 1024 if (rk is not None or wk is not None) else None,
            "davg": s_avg(series(hm, "disk.deviceLatency.average", cn)),
            "kavg": s_avg(series(hm, "disk.kernelLatency.average", cn)),
            "qavg": s_avg(series(hm, "disk.queueLatency.average", cn)),
            "gavg": s_avg(series(hm, "disk.totalLatency.average", cn)),
            "peak": s_max(series(hm, "disk.totalLatency.average", cn)),
            "aborts": s_sum(series(hm, "disk.commandsAborted.summation", cn)),
            "resets": s_sum(series(hm, "disk.busResets.summation", cn)),
        }

    for nic, info in inv["nics"].items():
        rx = s_avg(series(hm, "net.received.average", nic))
        tx = s_avg(series(hm, "net.transmitted.average", nic))
        util = None
        if info["speed"] and (rx is not None or tx is not None):
            util = max(rx or 0, tx or 0) * 8 * 1024 / 1e6 / info["speed"]
        drops = [s_sum(series(hm, f"net.dropped{d}.summation", nic)) for d in ("Rx", "Tx")]
        errs = [s_sum(series(hm, f"net.errors{d}.summation", nic)) for d in ("Rx", "Tx")]
        res["nic"][nic] = {"rx": rx / 1024 if rx is not None else None, "tx": tx / 1024 if tx is not None else None,
                           "util": util, "drops": sum(x or 0 for x in drops), "errors": sum(x or 0 for x in errs),
                           "speed": info["speed"]}

    res["vmres"] = {}
    for v in running:
        moid = v["obj"]._moId
        m = vmm.get(moid, {})
        ready = s_avg(series(m, "cpu.ready.summation", ""))
        costop = s_avg(series(m, "cpu.costop.summation", ""))
        vc = v.get("vcpu") or 1
        cpu_pct = s_avg(series(m, "cpu.usage.average", ""))

        def kb_gb(c):
            x = s_avg(series(m, c, ""))
            return x / 1024 ** 2 if x is not None else None
        res["vmres"][moid] = {
            "name": v["name"], "vcpu": v.get("vcpu"), "mem_gb": (v.get("mem_mb") or 0) / 1024,
            "cpu_pct": cpu_pct / 10000 if cpu_pct is not None else None,          # 1/100 % -> fraction
            "cpu_mhz": s_avg(series(m, "cpu.usagemhz.average", "")),
            "ready_pct": ready / (REALTIME_INTERVAL * 1000 * vc) if ready is not None else None,   # per vCPU
            "costop_pct": costop / (REALTIME_INTERVAL * 1000 * vc) if costop is not None else None,
            "consumed_gb": kb_gb("mem.consumed.average"), "active_gb": kb_gb("mem.active.average"),
            "balloon_gb": kb_gb("mem.vmmemctl.average"), "swapped_gb": kb_gb("mem.swapped.average"),
            "compressed_gb": kb_gb("mem.compressed.average"),
            "swapin_kbs": s_avg(series(m, "mem.swapinRate.average", "")),
        }
        per_ds = {}
        ds_insts = set((m.get("datastore.numberReadAveraged.average") or {}).keys()) | \
            set((m.get("datastore.read.average") or {}).keys())
        for inst in ds_insts:
            if inst:
                per_ds[inst] = io_block(m, "datastore", inst)
        disks = []
        vd_insts = set((m.get("virtualDisk.numberReadAveraged.average") or {}).keys()) | \
            set((m.get("virtualDisk.read.average") or {}).keys())
        for inst in vd_insts:
            if not inst:
                continue
            b = io_block(m, "virtualDisk", inst)
            meta = v["disks"].get(inst, {})
            b.update({"vm": v["name"], "inst": inst, "ds": meta.get("ds", "?"), "file": meta.get("file", "")})
            disks.append(b)
        if not per_ds and disks:              # fallback: group virtual disks by datastore name
            grouped = defaultdict(list)
            for d in disks:
                rec = inv["ds_by_name"].get(d["ds"])
                if rec:
                    grouped[rec["uuid"]].append(d)
            per_ds = {u: combine(bl) for u, bl in grouped.items()}
        total = combine(per_ds.values()) if per_ds else combine(disks) if disks else None
        if total is None:
            continue
        res["vm"][moid] = {"name": v["name"], "total": total, "per_ds": per_ds, "disks": disks,
                           "snapshots": v["snapshots"], "oldest_snapshot": v["oldest_snapshot"],
                           "consolidate": v["consolidate"]}
        res["vdisks"].extend(disks)
        for uuid, b in per_ds.items():
            res["consumers"][uuid].append((moid, b))
    res["series"] = build_series(inv, hm, vmm, running)
    return res


# =============================================================================
#  Per-sample history (20 s points) for the dashboard
# =============================================================================
def _pt(m, counter, inst, i):
    s = (m.get(counter) or {}).get(inst)
    if not s or i >= len(s):
        return None
    v = s[i]
    return v if v is not None and v >= 0 else None


def _r(v, d=2):
    return None if v is None else round(v, d)


def _io_pt(m, g, inst, i):
    """(iops, MB/s, read lat, write lat, r_iops, w_iops) of one sample or None."""
    ri, wi = _pt(m, f"{g}.numberReadAveraged.average", inst, i), _pt(m, f"{g}.numberWriteAveraged.average", inst, i)
    rk, wk = _pt(m, f"{g}.read.average", inst, i), _pt(m, f"{g}.write.average", inst, i)
    rl, wl = _pt(m, f"{g}.totalReadLatency.average", inst, i), _pt(m, f"{g}.totalWriteLatency.average", inst, i)
    if ri is None and wi is None and rk is None and wk is None:
        return None
    return ((ri or 0) + (wi or 0), ((rk or 0) + (wk or 0)) / 1024, rl, wl, ri or 0, wi or 0)


def build_series(inv, hm, vmm, running):
    """Every 20 s sample of the query -> {"pts": {epoch: point}, "names": {...}}."""
    pts = {}

    def P(ts):
        return pts.setdefault(ts, {"ds": {}, "vm": {}, "dev": {}, "nic": {}})
    mem_kb = (inv.get("hw", {}).get("mem_bytes") or 0) / 1024
    for i, ts in enumerate(hm.get("__ts__") or []):
        p = P(ts)
        c = _pt(hm, "cpu.usage.average", "", i)
        mc = _pt(hm, "mem.consumed.average", "", i)
        p["cpu"] = _r(c / 10000, 4) if c is not None else None
        p["mem"] = _r(mc / mem_kb, 4) if mc is not None and mem_kb else None
        for uuid in inv["datastores"]:
            x = _io_pt(hm, "datastore", uuid, i)
            if x:
                p["ds"][uuid] = [_r(x[0], 1), _r(x[1]), _r(x[2]), _r(x[3])]
        for cn in inv["devices"]:
            vals = [_pt(hm, f"disk.{k}.average", cn, i)
                    for k in ("totalLatency", "deviceLatency", "kernelLatency", "queueLatency")]
            ri = _pt(hm, "disk.numberReadAveraged.average", cn, i)
            wi = _pt(hm, "disk.numberWriteAveraged.average", cn, i)
            if any(v is not None for v in vals) or ri is not None:
                p["dev"][cn] = [_r(v) for v in vals] + [_r((ri or 0) + (wi or 0), 1)]
        for nic in inv["nics"]:
            rx, tx = _pt(hm, "net.received.average", nic, i), _pt(hm, "net.transmitted.average", nic, i)
            if rx is not None or tx is not None:
                p["nic"][nic] = [_r((rx or 0) / 1024), _r((tx or 0) / 1024)]
    names = {"vm": {}, "ds": {u: d["name"] for u, d in inv["datastores"].items()},
             "dev": {cn: d["display"] for cn, d in inv["devices"].items()}}
    for v in running:
        moid = v["obj"]._moId
        m = vmm.get(moid) or {}
        names["vm"][moid] = v["name"]
        grp = "datastore"
        insts = [k for k in (m.get("datastore.numberReadAveraged.average") or {}) if k]
        if not insts:
            grp, insts = "virtualDisk", [k for k in (m.get("virtualDisk.numberReadAveraged.average") or {}) if k]
        for i, ts in enumerate(m.get("__ts__") or []):
            iops = mbs = lw = 0.0
            lats, any_io = [], False
            for inst in insts:
                x = _io_pt(m, grp, inst, i)
                if not x:
                    continue
                any_io = True
                iops += x[0]
                mbs += x[1]
                lt = wlat(x[2], x[3], x[4], x[5])
                if lt is not None:
                    lats.append(lt)
                    lw += lt * x[0]
            lat = (lw / iops) if iops > 0 else (max(lats) if lats else None)
            cpu = _pt(m, "cpu.usagemhz.average", "", i)
            if any_io or cpu is not None:
                P(ts)["vm"][moid] = [_r(iops, 1) if any_io else None, _r(mbs) if any_io else None, _r(lat), _r(cpu, 0)]
    return {"pts": pts, "names": names}


class History:
    """In-memory per-host history of 20 s points (thread-safe), merged from every query.
    Point layout:  ds = [IOPS, MB/s, read ms, write ms]   vm = [IOPS, MB/s, latency ms, CPU MHz]
                   dev = [GAVG, DAVG, KAVG, QAVG, IOPS]    nic = [RX MB/s, TX MB/s]   cpu / mem = fraction"""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def merge(self, label, series, max_points):
        if not series:
            return
        with self.lock:
            h = self.data.setdefault(label, {"pts": {}, "names": {"vm": {}, "ds": {}, "dev": {}}})
            for ts, p in series["pts"].items():
                old = h["pts"].get(ts)
                if old is None:
                    h["pts"][ts] = p
                    continue
                for k, v in p.items():
                    if isinstance(v, dict):
                        old.setdefault(k, {}).update(v)
                    elif v is not None:
                        old[k] = v
            for k, d in series.get("names", {}).items():
                h["names"].setdefault(k, {}).update(d)
            if len(h["pts"]) > max_points:
                for ts in sorted(h["pts"])[:len(h["pts"]) - max_points]:
                    del h["pts"][ts]

    def payload(self, label):
        """Columnar form for the web UI / export: t[], cpu[], mem[], ds{id:{n,v[]}}, vm{..}, dev{..}, nic{..}."""
        with self.lock:
            h = self.data.get(label)
            if not h:
                return None
            ts = sorted(h["pts"])
            pts = [h["pts"][t] for t in ts]
            out = {"t": ts, "cpu": [p.get("cpu") for p in pts], "mem": [p.get("mem") for p in pts]}
            for grp in ("ds", "vm", "dev", "nic"):
                keys = set()
                for p in pts:
                    keys.update(p.get(grp, {}).keys())
                names = h["names"].get(grp, {})
                out[grp] = {k: {"n": names.get(k, k), "v": [p.get(grp, {}).get(k) for p in pts]}
                            for k in sorted(keys)}
            return out


HISTORY = History()


def history_max_points(cfg):
    try:
        return max(30, int(float(cfg.get("history_hours", 6)) * 3600 / REALTIME_INTERVAL))
    except (TypeError, ValueError):
        return 1080


def backfill_history(ctxs, cfg, samples):
    """Load the last `samples` x 20 s from ESXi into HISTORY (ESXi keeps real-time stats for 1 hour)."""
    ok = [c for c in ctxs if not c.get("error")]
    if not ok or int(samples) <= 1:
        return

    def one(c):
        try:
            r = analyze(c["inv"], c["perf"], c["host"], int(samples))
            HISTORY.merge(c["label"], r.get("series"), history_max_points(cfg))
        except Exception as e:
            if not SHUTDOWN.is_set():
                p_warn(f"{c['label']}: history backfill failed: {type(e).__name__}: {e}")
    with ThreadPoolExecutor(max_workers=min(8, len(ok))) as ex:
        list(ex.map(one, ok))


# =============================================================================
#  SSH private key import (OpenSSH / PEM / PuTTY PPK v2+v3)
# =============================================================================
def _ssh_str(buf, off):
    n = struct.unpack(">I", buf[off:off + 4])[0]
    return buf[off + 4:off + 4 + n], off + 4 + n


def _ssh_mpint(buf, off):
    b, off = _ssh_str(buf, off)
    return int.from_bytes(b, "big"), off


def _ssh_pack(b):
    return struct.pack(">I", len(b)) + b


def _argon2(kind, pw, salt, mem_kib, passes, lanes, length):
    try:
        from argon2.low_level import hash_secret_raw, Type
        t = {"argon2id": Type.ID, "argon2i": Type.I, "argon2d": Type.D}[kind.lower()]
        return hash_secret_raw(pw, salt, time_cost=passes, memory_cost=mem_kib, parallelism=lanes,
                               hash_len=length, type=t)
    except ImportError:
        pass
    if kind.lower() == "argon2id":
        try:
            from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
            return Argon2id(salt=salt, length=length, iterations=passes, lanes=lanes,
                            memory_cost=mem_kib).derive(pw)
        except ImportError:
            pass
    raise RuntimeError("Encrypted PPK v3 key needs Argon2:  pip install argon2-cffi")


def parse_ppk(text, passphrase):
    """Returns cryptography private key object. passphrase: str or None."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    hdr, pub, priv, i = {}, b"", b"", 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if k in ("Public-Lines", "Private-Lines"):
            n = int(v)
            data = base64.b64decode("".join(lines[i:i + n]))
            i += n
            if k == "Public-Lines":
                pub = data
            else:
                priv = data
        else:
            hdr[k] = v
    vkey = next((k for k in hdr if k.startswith("PuTTY-User-Key-File-")), None)
    if not vkey:
        raise ValueError("not a PuTTY key file")
    version = int(vkey.rsplit("-", 1)[1])
    if version not in (2, 3):
        raise ValueError(f"PPK version {version} is not supported (convert it with PuTTYgen)")
    alg, enc, comment = hdr[vkey], hdr.get("Encryption", "none"), hdr.get("Comment", "")
    pw = (passphrase or "").encode("utf-8")

    if enc == "aes256-cbc":
        if passphrase is None:
            raise TypeError("passphrase required")
        if version == 2:
            k = hashlib.sha1(b"\0\0\0\0" + pw).digest() + hashlib.sha1(b"\0\0\0\1" + pw).digest()
            ck, iv = k[:32], b"\0" * 16
            mac_key = hashlib.sha1(b"putty-private-key-file-mac-key" + pw).digest()
        else:
            out = _argon2(hdr.get("Key-Derivation", "Argon2id"), pw, bytes.fromhex(hdr["Argon2-Salt"]),
                          int(hdr["Argon2-Memory"]), int(hdr["Argon2-Passes"]), int(hdr["Argon2-Parallelism"]), 80)
            ck, iv, mac_key = out[:32], out[32:48], out[48:80]
        dec = Cipher(algorithms.AES(ck), modes.CBC(iv)).decryptor()
        priv = dec.update(priv) + dec.finalize()
    elif enc == "none":
        mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest() if version == 2 else b""
    else:
        raise ValueError(f"unsupported PPK encryption '{enc}'")

    mac_data = b"".join(_ssh_pack(x) for x in (alg.encode(), enc.encode(), comment.encode(), pub, priv))
    calc = hmac.new(mac_key, mac_data, hashlib.sha1 if version == 2 else hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, hdr.get("Private-MAC", "").lower()):
        raise ValueError("wrong passphrase or corrupted PPK file")

    _, off = _ssh_str(pub, 0)
    if alg == "ssh-rsa":
        e, off = _ssh_mpint(pub, off)
        n, off = _ssh_mpint(pub, off)
        d, o = _ssh_mpint(priv, 0)
        p, o = _ssh_mpint(priv, o)
        q, o = _ssh_mpint(priv, o)
        return rsa.RSAPrivateNumbers(p, q, d, rsa.rsa_crt_dmp1(d, p), rsa.rsa_crt_dmq1(d, q),
                                     rsa.rsa_crt_iqmp(p, q), rsa.RSAPublicNumbers(e, n)).private_key()
    if alg == "ssh-ed25519":
        sk, _ = _ssh_str(priv, 0)
        return ed25519.Ed25519PrivateKey.from_private_bytes(sk[:32])
    if alg.startswith("ecdsa-sha2-"):
        curve_name, off = _ssh_str(pub, off)
        curve = {b"nistp256": ec.SECP256R1(), b"nistp384": ec.SECP384R1(), b"nistp521": ec.SECP521R1()}[curve_name]
        d, _ = _ssh_mpint(priv, 0)
        return ec.derive_private_key(d, curve)
    raise ValueError(f"unsupported PPK key type '{alg}'")


def load_crypto_key(text, passphrase=None):
    """Load OpenSSH / PEM / PPK. Raises TypeError if passphrase is needed, ValueError if wrong."""
    from cryptography.hazmat.primitives import serialization
    if text.lstrip().startswith("PuTTY-User-Key-File-"):
        return parse_ppk(text, passphrase)
    data = text.encode("utf-8")
    pw = passphrase.encode("utf-8") if passphrase else None
    if b"BEGIN OPENSSH PRIVATE KEY" in data:
        return serialization.load_ssh_private_key(data, password=pw)
    return serialization.load_pem_private_key(data, password=pw)


def _load_with_prompt(text, what="private key"):
    """Returns (key, passphrase_or_None)."""
    try:
        return load_crypto_key(text), None
    except TypeError:
        pass
    for _ in range(3):
        pw = masked_input(col(f"  Passphrase for SSH {what}: ", Fore.CYAN))
        try:
            return load_crypto_key(text, pw), pw
        except (TypeError, ValueError):
            p_warn("Wrong passphrase.")
    raise RuntimeError(f"SSH {what} could not be decrypted")


def import_key_from_path(cfg, path):
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        p_err("Key import needs 'cryptography' (installed with: pip install paramiko)")
        return False
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    try:
        text = _read_bytes(path).decode("utf-8", "replace")
        is_ppk = text.lstrip().startswith("PuTTY-User-Key-File-")
        key, pw = _load_with_prompt(text)
        enc = serialization.BestAvailableEncryption(pw.encode("utf-8")) if pw else serialization.NoEncryption()
        cfg["guest_ssh_key"] = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                 enc).decode("ascii")
        kname = type(key).__name__.replace("_", "").replace("PrivateKey", "")
        cfg["guest_ssh_key_info"] = (f"{kname} from {'PuTTY PPK' if is_ppk else 'OpenSSH/PEM'} "
                                     f"'{os.path.basename(path)}', {'passphrase protected' if pw else 'no passphrase'}")
        p_ok(f"SSH key imported: {cfg['guest_ssh_key_info']}")
        return True
    except Exception as e:
        p_err(f"Cannot import key '{path}': {e}")
        return False


def import_key_interactive(cfg):
    while True:
        path = ask("Path to SSH private key (OpenSSH/PEM or PuTTY .ppk, Enter = skip)", "", str)
        if not path:
            return False
        if import_key_from_path(cfg, path):
            return True


_PKEY_CACHE = {}


def get_paramiko_key(cfg):
    if "key" in _PKEY_CACHE:
        return _PKEY_CACHE["key"]
    import paramiko
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    ck, _ = _load_with_prompt(cfg["guest_ssh_key"], "private key (guest login)")
    txt = ck.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                           serialization.NoEncryption()).decode("ascii")
    if isinstance(ck, ed25519.Ed25519PrivateKey):
        cls = paramiko.Ed25519Key
    elif isinstance(ck, ec.EllipticCurvePrivateKey):
        cls = paramiko.ECDSAKey
    elif isinstance(ck, rsa.RSAPrivateKey):
        cls = paramiko.RSAKey
    else:
        cls = getattr(paramiko, "DSSKey")
    _PKEY_CACHE["key"] = cls.from_private_key(io.StringIO(txt))
    return _PKEY_CACHE["key"]


# =============================================================================
#  In-guest analysis (Linux, SSH, READ-ONLY)
# =============================================================================
# Only reads /proc and /sys. Nothing is written on the guest.
GUEST_SCRIPT = r'''
snap() {
  echo "@@SNAP"
  cat /proc/uptime
  grep '^cpu ' /proc/stat
  echo "@@DISK"
  cat /proc/diskstats
  echo "@@PROC"
  grep -s -H -E '^(read_bytes|write_bytes|rchar|wchar):' /proc/[0-9]*/io
  echo "@@PSTAT"
  grep -s -H '' /proc/[0-9]*/stat
  echo "@@VMSTAT"
  grep -E '^(pswpin|pswpout|pgmajfault) ' /proc/vmstat
}
echo "@@TOOLS"
for t in cat grep hostname id getconf sleep base64 sudo; do command -v "$t" >/dev/null 2>&1 || echo "$t"; done
echo "@@OS"
grep -E '^(ID|ID_LIKE|PRETTY_NAME)=' /etc/os-release 2>/dev/null
echo "@@INFO"
hostname 2>/dev/null || cat /proc/sys/kernel/hostname
cat /proc/loadavg
grep -c '^processor' /proc/cpuinfo
id -un 2>/dev/null || echo "?"
getconf PAGESIZE 2>/dev/null || echo 4096
getconf CLK_TCK 2>/dev/null || echo 100
echo "@@DM"
for d in /sys/block/dm-*; do [ -r "$d/dm/name" ] && echo "${d##*/} $(cat "$d/dm/name")"; done
snap
sleep __SLEEP__
snap
echo "@@PSI"
cat /proc/pressure/io 2>/dev/null
echo "@@PSICPU"
cat /proc/pressure/cpu 2>/dev/null
echo "@@PSIMEM"
cat /proc/pressure/memory 2>/dev/null
echo "@@MEM"
cat /proc/meminfo
echo "@@COMM"
grep -s -H '' /proc/[0-9]*/comm
echo "@@DONE"
'''


def _is_whole_disk(n):
    if re.match(r"^(loop|ram|zram|sr|fd|nbd)\d*", n):
        return False
    if re.match(r"^(sd|vd|xvd|hd)[a-z]+\d+$", n) or re.match(r"^(nvme\d+n\d+p\d+|mmcblk\d+p\d+)$", n):
        return False
    return True


def parse_guest_output(out, sample_s):
    sections = []
    for line in out.splitlines():
        if line.startswith("@@"):
            sections.append([line[2:].strip(), []])
        elif sections:
            sections[-1][1].append(line)
    info, dm, psi, comm, snaps, meminfo, tools, osrel = [], {}, {}, {}, [], {}, [], {}
    psi_cpu, psi_mem = {}, {}
    for name, lines in sections:
        if name == "INFO":
            info = lines
        elif name == "TOOLS":
            tools = [l.strip() for l in lines if l.strip()]
        elif name == "OS":
            for l in lines:
                k, _, v = l.partition("=")
                osrel[k.strip()] = v.strip().strip('"')
        elif name == "DM":
            for l in lines:
                p = l.split(None, 1)
                if len(p) == 2:
                    dm[p[0]] = p[1]
        elif name == "SNAP":
            snaps.append({"uptime": float(lines[0].split()[0]), "cpu": [int(x) for x in lines[1].split()[1:]],
                          "disk": {}, "proc": defaultdict(dict), "pstat": {}, "vmstat": {}})
        elif name == "DISK" and snaps:
            for l in lines:
                p = l.split()
                if len(p) >= 14:
                    snaps[-1]["disk"][p[2]] = [int(x) for x in p[3:14]]
        elif name == "PROC" and snaps:
            for l in lines:
                m = re.match(r"/proc/(\d+)/io:(\w+):\s*(\d+)", l)
                if m:
                    snaps[-1]["proc"][m.group(1)][m.group(2)] = int(m.group(3))
        elif name == "PSTAT" and snaps:
            for l in lines:
                m = re.match(r"/proc/(\d+)/stat:\d+ \((.*)\) (.*)$", l)
                if m:
                    f = m.group(3).split()
                    if len(f) > 21:
                        snaps[-1]["pstat"][m.group(1)] = (int(f[11]) + int(f[12]), int(f[21]), m.group(2))
        elif name == "VMSTAT" and snaps:
            for l in lines:
                p = l.split()
                if len(p) == 2:
                    snaps[-1]["vmstat"][p[0]] = int(p[1])
        elif name == "MEM":
            for l in lines:
                m = re.match(r"(\w+):\s+(\d+)", l)
                if m:
                    meminfo[m.group(1)] = int(m.group(2))            # kB
        elif name in ("PSI", "PSICPU", "PSIMEM"):
            target = {"PSI": psi, "PSICPU": psi_cpu, "PSIMEM": psi_mem}[name]
            for l in lines:
                m = re.match(r"(some|full)\s+avg10=([\d.]+)", l)
                if m:
                    target[m.group(1)] = float(m.group(2))
        elif name == "COMM":
            for l in lines:
                m = re.match(r"/proc/(\d+)/comm:(.*)", l)
                if m:
                    comm[m.group(1)] = m.group(2).strip()
    if len(snaps) < 2:
        raise RuntimeError("incomplete output from guest")
    a, b = snaps[0], snaps[1]
    dt = (b["uptime"] - a["uptime"]) or float(sample_s)

    g = {"hostname": info[0] if info else "", "load": info[1].split()[:3] if len(info) > 1 else [],
         "cpus": info[2] if len(info) > 2 else "?", "remote_user": info[3] if len(info) > 3 else "",
         "psi": psi, "psi_cpu": psi_cpu, "psi_mem": psi_mem, "dt": dt, "disks": [], "procs": [],
         "missing_tools": tools, "os_id": f"{osrel.get('ID', '')} {osrel.get('ID_LIKE', '')}".strip(),
         "os_name": osrel.get("PRETTY_NAME", "")}
    dc = [y - x for x, y in zip(a["cpu"], b["cpu"])] + [0] * 8
    tot = sum(dc[:8]) or 1
    g["iowait"] = dc[4] / tot
    g["steal"] = dc[7] / tot
    g["cpu_user"] = (dc[0] + dc[1]) / tot
    g["cpu_sys"] = (dc[2] + dc[5] + dc[6]) / tot
    g["cpu_busy"] = 1 - (dc[3] + dc[4]) / tot

    # ---- memory ----
    mi = meminfo
    if mi.get("MemTotal"):
        avail = mi.get("MemAvailable", mi.get("MemFree", 0) + mi.get("Cached", 0))
        g["mem"] = {"total_gb": mi["MemTotal"] / 1024 ** 2, "avail_gb": avail / 1024 ** 2,
                    "avail_pct": avail / mi["MemTotal"],
                    "swap_total_gb": mi.get("SwapTotal", 0) / 1024 ** 2,
                    "swap_used_gb": (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) / 1024 ** 2,
                    "dirty_mb": mi.get("Dirty", 0) / 1024, "writeback_mb": mi.get("Writeback", 0) / 1024}
    else:
        g["mem"] = None
    vs1, vs2 = a["vmstat"], b["vmstat"]
    g["swapin_ps"] = (vs2.get("pswpin", 0) - vs1.get("pswpin", 0)) / dt
    g["swapout_ps"] = (vs2.get("pswpout", 0) - vs1.get("pswpout", 0)) / dt
    g["majflt_ps"] = (vs2.get("pgmajfault", 0) - vs1.get("pgmajfault", 0)) / dt

    # ---- per-process CPU / RSS ----
    try:
        page = int(info[4]) if len(info) > 4 else 4096
        hz = int(info[5]) if len(info) > 5 else 100
    except ValueError:
        page, hz = 4096, 100
    g["cpu_procs"] = []
    for pid, (t2, rss, cm) in b["pstat"].items():
        t1 = a["pstat"].get(pid, (t2, 0, ""))[0]
        g["cpu_procs"].append({"pid": pid, "comm": comm.get(pid, cm), "cpu": max(0, t2 - t1) / hz / dt,
                               "rss_mb": rss * page / 1024 ** 2})
    g["mem_procs"] = sorted(g["cpu_procs"], key=lambda p: p["rss_mb"], reverse=True)
    g["cpu_procs"].sort(key=lambda p: (p["cpu"], p["rss_mb"]), reverse=True)

    for name, v2 in b["disk"].items():
        v1 = a["disk"].get(name)
        if not v1 or not _is_whole_disk(name):
            continue
        d = [y - x for x, y in zip(v1, v2)]
        rd, wr = d[0], d[4]
        g["disks"].append({
            "name": f"{name} ({dm[name]})" if name in dm else name,
            "r_iops": rd / dt, "w_iops": wr / dt,
            "r_mbs": d[2] * 512 / dt / 1024 ** 2, "w_mbs": d[6] * 512 / dt / 1024 ** 2,
            "r_await": d[3] / rd if rd else None, "w_await": d[7] / wr if wr else None,
            "util": min(d[9] / (dt * 1000), 1.0), "inflight": v2[8],
        })
    for pid, v2 in b["proc"].items():
        v1 = a["proc"].get(pid)
        if not v1:
            continue
        rb = max(0, v2.get("read_bytes", 0) - v1.get("read_bytes", 0))
        wb = max(0, v2.get("write_bytes", 0) - v1.get("write_bytes", 0))
        rc = max(0, v2.get("rchar", 0) - v1.get("rchar", 0))
        wc = max(0, v2.get("wchar", 0) - v1.get("wchar", 0))
        if rb or wb or rc or wc:
            g["procs"].append({"pid": pid, "comm": comm.get(pid, "?"), "r_mbs": rb / dt / 1024 ** 2,
                               "w_mbs": wb / dt / 1024 ** 2, "rc_mbs": rc / dt / 1024 ** 2,
                               "wc_mbs": wc / dt / 1024 ** 2})
    g["procs"].sort(key=lambda p: (p["r_mbs"] + p["w_mbs"], p["rc_mbs"] + p["wc_mbs"]), reverse=True)
    g["disks"].sort(key=lambda d: d["util"], reverse=True)
    g["total_mbs"] = sum(p["r_mbs"] + p["w_mbs"] for p in g["procs"])
    return g


def probe_tcp(ips, port, timeout):
    """Test all IPs in parallel; returns reachable IPs in the original (preference) order."""
    import socket

    def ok(ip):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False
    if not ips:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(ips))) as ex:
        flags = list(ex.map(ok, ips))
    return [ip for ip, f in zip(ips, flags) if f]


class OtpRequired(Exception):
    """Server wants a second factor / interactive login (OTP, Duo, password after key ...)."""


INTERACTIVE_METHODS = {"keyboard-interactive", "password"}


def ssh_open_transport(ip, port, user, pkey, timeout):
    """Key-only SSH login without ANY interactive prompt. Host keys are not checked / stored.
    Raises OtpRequired when the server needs OTP / keyboard-interactive / password,
    AuthenticationException if the key is refused."""
    import socket
    import paramiko
    sock = socket.create_connection((ip, port), timeout=timeout)
    tr = paramiko.Transport(sock)
    try:
        tr.banner_timeout = timeout
        tr.auth_timeout = timeout
        tr.start_client(timeout=timeout)
        # --- public key only ---
        try:
            remaining = tr.auth_publickey(user, pkey)
        except paramiko.BadAuthenticationType as e:
            allowed = set(e.allowed_types or [])
            if "publickey" not in allowed and allowed & INTERACTIVE_METHODS:
                raise OtpRequired(f"server allows only {', '.join(sorted(allowed))} (OTP / password login)")
            raise
        if not tr.is_authenticated():
            # key accepted but server requests a further factor (partial success)
            raise OtpRequired("key accepted, but server requires a second factor "
                              f"({', '.join(remaining) or 'interactive'}) - OTP / 2FA")
        return tr
    except Exception:
        tr.close()
        raise


def ssh_connect_any(ips, pkey, cfg):
    """Try reachable IPv4 addresses one by one. Returns (transport, used_ip, info) or raises."""
    import logging
    import paramiko
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)   # errors are reported by us, no tracebacks
    port = int(cfg.get("guest_port", 22))
    t = int(cfg.get("guest_timeout_s", 10))
    reachable = probe_tcp(ips, port, float(cfg.get("guest_probe_timeout_s", 2)))
    unreachable = [ip for ip in ips if ip not in reachable]
    info = {"tried": list(ips), "reachable": reachable, "unreachable": unreachable}
    if not reachable:
        raise ConnectionError(f"SSH port {port} not reachable on any IPv4: {', '.join(ips)}")
    last = None
    for ip in reachable:
        try:
            return ssh_open_transport(ip, port, cfg.get("guest_user", "root"), pkey, t), ip, info
        except (OtpRequired, paramiko.AuthenticationException) as e:
            e.used_ip = ip
            raise                                       # same guest -> no point trying other IPs
        except Exception as e:                          # network-level problem -> next IP
            last = e
    raise ConnectionError(f"SSH failed on all reachable IPs ({', '.join(reachable)}): {last}")


def _payload_cmd(script, sudo):
    """Script travels inside the command line (base64) - nothing is fed to stdin, so a menu-type
    login shell never receives our text as keystrokes. Nothing is written to the guest's disk."""
    b = base64.b64encode(script.encode("utf-8")).decode("ascii")
    plain = f"echo {b} | base64 -d | sh"
    return f"echo {b} | base64 -d | sudo -n sh 2>/dev/null || {plain}" if sudo else plain


def _read_until(chan, done, first, first_timeout, total_timeout):
    """Read channel until marker 'done', EOF, or timeouts (no 'first' marker within first_timeout)."""
    import socket
    buf = bytearray()
    start = time.time()
    chan.settimeout(0.5)
    while True:
        try:
            data = chan.recv(65536)
            if not data:
                break
            buf.extend(data)
        except socket.timeout:
            pass
        if done in buf:
            break
        el = time.time() - start
        if (first not in buf and el > first_timeout) or el > total_timeout:
            break
    return buf.decode("utf-8", "replace")


def ssh_exec(tr, cmd, first_timeout, total_timeout, done=b"@@DONE", first=b"@@TOOLS", stdin_text=None):
    """Non-interactive exec, no PTY. stdin is closed immediately unless stdin_text is given
    (stdin is only used after a probe proved the login shell is a normal shell, not a menu)."""
    chan = tr.open_session(timeout=first_timeout)
    try:
        chan.exec_command(cmd)
        if stdin_text:
            chan.sendall(stdin_text.encode("utf-8"))
        chan.shutdown_write()
        return _read_until(chan, done, first, first_timeout, total_timeout)
    finally:
        chan.close()


# tools needed inside the guest; plain POSIX sh only (works without base64)
GUEST_TOOLS = ["sh", "cat", "grep", "hostname", "id", "getconf", "sleep", "base64", "sudo"]
GUEST_CORE_TOOLS = {"sh", "cat", "grep", "sleep"}          # without these no measurement is possible
PROBE_CMD = ('for t in ' + " ".join(GUEST_TOOLS) +
             '; do command -v "$t" >/dev/null 2>&1 || echo "MISSING:$t"; done; echo "@@PROBE""_END"')


def _key_bytes(k):
    k = str(k)
    named = {"{ENTER}": "\r", "{ESC}": "\x1b", "{TAB}": "\t", "{SPACE}": " ", "{BACKSPACE}": "\x7f"}
    if k.upper() in named:
        return named[k.upper()]
    if len(k) == 2 and k[0] == "^":                  # ^C, ^D, ^Z ...
        return chr(ord(k[1].upper()) & 0x1F)
    return k


def ssh_pty_escape(tr, cmd, keys, delay, first_timeout, total_timeout):
    """For guests whose login starts a special CLI/menu: open an interactive PTY, send the escape keys
    (default Ctrl+C, q, Enter) to reach the shell, then run the read-only payload."""
    chan = tr.open_session(timeout=first_timeout)
    try:
        chan.get_pty(term="vt100", width=1000, height=50)
        chan.invoke_shell()

        def drain(wait):
            end_t = time.time() + wait
            chan.settimeout(0.2)
            while time.time() < end_t:
                try:
                    if not chan.recv(65536):
                        return False
                except Exception:
                    pass
            return True
        drain(delay * 2)                               # let the CLI draw its banner / menu
        for k in keys:
            chan.send(_key_bytes(k))
            drain(delay)
        # @@SH""ELL_OK: the echoed command line does not contain the marker, only real shell output does
        chan.send('\recho @@SH""ELL_OK; ' + cmd + "\r")
        out = _read_until(chan, b"@@DONE", b"@@SHELL_OK", first_timeout + 5, total_timeout)
        i = out.find("@@SHELL_OK")
        return out[i:] if i >= 0 else ""
    finally:
        chan.close()


def ssh_collect(target, pkey, cfg, sample_s):
    import paramiko
    res = {"name": target["name"], "ip": ", ".join(target["ips"]), "moid": target["moid"], "error": None,
           "skipped": None, "sudo": False}
    tr = None
    try:
        tr, used_ip, info = ssh_connect_any(target["ips"], pkey, cfg)
        res["ip"] = used_ip
        res["ip_info"] = info
        script = GUEST_SCRIPT.replace("__SLEEP__", str(int(sample_s)))
        first_t = float(cfg.get("guest_timeout_s", 10))
        total_t = sample_s + 60
        want_sudo = bool(cfg.get("guest_sudo")) and cfg.get("guest_user", "root") != "root"
        out = ""
        if want_sudo:
            out = ssh_exec(tr, _payload_cmd(script, False).replace("| sh", "| sudo -n sh"), first_t, total_t)
            res["sudo"] = "@@DONE" in out
        if "@@DONE" not in out:
            out = ssh_exec(tr, _payload_cmd(script, False), first_t, total_t)
        if "@@DONE" not in out:
            # did a normal shell answer at all? (tool probe without base64)
            if not tr.is_active():
                tr.close()
                tr, used_ip, _ = ssh_connect_any([used_ip], pkey, cfg)
            probe = ssh_exec(tr, PROBE_CMD, first_t, first_t + 5, done=b"@@PROBE_END", first=b"@@PROBE_END")
            if "@@PROBE_END" in probe:
                missing = re.findall(r"MISSING:(\S+)", probe)
                res["missing_tools"] = missing
                core = GUEST_CORE_TOOLS & set(missing)
                if core:
                    raise RuntimeError(f"required tools missing in guest: {', '.join(sorted(core))}")
                if "base64" in missing:
                    # normal shell confirmed -> safe to feed the script via stdin
                    out = ssh_exec(tr, "sudo -n sh -s" if want_sudo else "sh -s", first_t, total_t,
                                   stdin_text=script)
                    if "@@DONE" not in out and want_sudo:
                        out = ssh_exec(tr, "sh -s", first_t, total_t, stdin_text=script)
        keys = cfg.get("guest_escape_keys") or []
        if "@@DONE" not in out and keys and "missing_tools" not in res:
            # no plain shell answered -> special login CLI? escape it in an interactive PTY
            if not tr.is_active():
                tr.close()
                tr, used_ip, _ = ssh_connect_any([used_ip], pkey, cfg)
            out = ssh_pty_escape(tr, _payload_cmd(script, want_sudo), keys,
                                 float(cfg.get("guest_escape_delay_s", 1.0)), first_t, total_t)
            if "@@DONE" in out:
                res["note"] = f"special login CLI escaped with keys {' '.join(map(str, keys))}"
        if "@@DONE" not in out:
            raise RuntimeError("no shell reached (special login CLI? adjust \"guest_escape_keys\" in JSON)")
        tm = re.search(r"@@TOOLS\s*\n(.*?)@@OS", out, re.S)
        if tm:
            res["missing_tools"] = [x.strip() for x in tm.group(1).splitlines() if x.strip()]
            core = GUEST_CORE_TOOLS & set(res["missing_tools"])
            if core:
                raise RuntimeError(f"required tools missing in guest: {', '.join(sorted(core))}")
        try:
            res.update(parse_guest_output(out, sample_s))
        except (IndexError, ValueError) as e:
            raise RuntimeError(f"unexpected output from guest ({type(e).__name__}) - missing tools or /proc "
                               f"restrictions?")
        if want_sudo:
            res["sudo"] = res.get("remote_user") == "root"
            if not res["sudo"]:
                res["note"] = (res.get("note", "") + "; " if res.get("note") else "") + \
                    "sudo -n not permitted - only own processes visible"
    except OtpRequired as e:
        res["ip"] = getattr(e, "used_ip", res["ip"])
        res["skipped"] = f"SSH requires OTP / interactive login - ignored ({e})"
    except paramiko.AuthenticationException:
        res["error"] = f"authentication failed for user '{cfg.get('guest_user')}' (key not authorized?)"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        if tr is not None:
            tr.close()
    return res


def guest_targets(inv, res, cfg):
    overrides = cfg.get("guest_hosts") or {}
    scope = cfg.get("guest_scope", "top")
    running = [v for v in inv["vms"].values() if v["power"] == "poweredOn" and not v["template"]]
    if scope == "list":
        names = set(cfg.get("guest_vm_list") or [])
        cand = [v for v in running if v["name"] in names]
    else:
        cand = [v for v in running if is_linux_guest(v) or v["name"] in overrides]
        if scope == "top":
            key = sort_key(cfg)
            cand = [v for v in cand if v["obj"]._moId in res["vm"]
                    and (res["vm"][v["obj"]._moId]["total"]["iops"] >= 1
                         or res["vm"][v["obj"]._moId]["total"]["mbs"] >= 0.1)]
            cand.sort(key=lambda v: key(res["vm"][v["obj"]._moId]["total"]), reverse=True)
            cand = cand[:int(cfg.get("guest_top_n", 5))]
    targets, skipped = [], []
    for v in cand:
        ov = overrides.get(v["name"])
        if ov:                                   # override: string, "ip1,ip2" or list
            ips = [x.strip() for x in (ov if isinstance(ov, list) else str(ov).split(",")) if x.strip()]
        else:
            ips = list(v["guest"].get("ips") or [])
        if ips:
            targets.append({"moid": v["obj"]._moId, "name": v["name"], "ips": ips})
        else:
            skipped.append((v["name"], "no IPv4 from VMware Tools (add it to \"guest_hosts\" in JSON)"))
    return targets, skipped


def run_guest_analysis(ctxs, cfg, sample_s):
    """In-guest analysis for all hosts at once. Stores res['guests'] / res['guest_skipped'] per context."""
    for c in ctxs:
        c["res"]["guests"], c["res"]["guest_skipped"] = {}, []
    try:
        import paramiko
    except ImportError:
        p_warn("In-guest analysis skipped: install paramiko  (pip install paramiko)")
        return
    if SHUTDOWN.is_set():
        return
    jobs = []
    for i, c in enumerate(ctxs):
        targets, skipped = guest_targets(c["inv"], c["res"], cfg)
        c["res"]["guest_skipped"] = skipped
        jobs.extend((i, t) for t in targets)
    if not jobs:
        return
    try:
        pkey = get_paramiko_key(cfg)
    except Exception as e:
        p_err(f"In-guest analysis skipped: {e}")
        return
    p_info(f"In-guest analysis (read-only SSH) of {len(jobs)} VM(s), sampling {sample_s} s ...")
    with ThreadPoolExecutor(max_workers=min(12, len(jobs))) as ex:
        results = list(ex.map(lambda j: ssh_collect(j[1], pkey, cfg, sample_s), jobs))
    for (i, _), r in zip(jobs, results):
        if r.get("skipped"):                     # OTP / interactive login -> ignored, not an error
            ctxs[i]["res"]["guest_skipped"].append((f"{r['name']} ({r['ip']})", r["skipped"]))
        else:
            ctxs[i]["res"]["guests"][r["moid"]] = r


def render_guest_info(inv, cfg):
    section("GUEST INFO (VMware Tools, read-only)")
    rows = []
    for v in sorted(inv["vms"].values(), key=lambda x: x["name"].lower()):
        if v["power"] != "poweredOn" or v["template"]:
            continue
        g = v["guest"]
        tools = g["tools"].replace("guestTools", "") or "-"
        tcol = Fore.GREEN if tools == "Running" else Fore.YELLOW
        fullest, ffree = "-", None
        for path, cap, free in g["fs"]:
            if cap and (ffree is None or free / cap < ffree):
                ffree, fullest = free / cap, path
        fcol = Fore.RED + Style.BRIGHT if ffree is not None and ffree < 0.10 else \
            Fore.YELLOW if ffree is not None and ffree < 0.20 else ""
        rows.append([trunc(v["name"], 28), (tools, tcol), trunc(g["os"] or "-", 32), trunc(g["hostname"] or "-", 24),
                     trunc(", ".join(g.get("ips") or []) or "-", 48), trunc(fullest, 20), (f_pct(ffree), fcol)])
    print_table(["VM", "Tools", "Guest OS", "Hostname", "IPv4 (all NICs)", "Fullest FS", "Free"], rows,
                ["l"] * 6 + ["r"])


def render_guests(guests, skipped, cfg):
    section("IN-GUEST ANALYSIS (Linux over SSH, read-only)")
    if not guests and not skipped:
        print(col("  (no Linux VM selected - nothing to analyze)", Style.DIM))
    for name, why in skipped:
        print("  " + col(f"{name}: skipped - {why}", Fore.YELLOW))
    for g in sorted(guests.values(), key=lambda x: -(x.get("total_mbs") or 0)):
        print()
        if g["error"]:
            print("  " + col(f"{g['name']} ({g['ip']})", Style.BRIGHT) + "  " + col(g["error"], Fore.RED))
            continue
        iw = g.get("iowait")
        iwc = Fore.RED + Style.BRIGHT if iw is not None and iw >= 0.3 else Fore.YELLOW if iw is not None and iw >= 0.1 \
            else Fore.GREEN
        psi = g.get("psi") or {}
        psi_txt = f"  PSI io some {psi['some']:.1f}% full {psi.get('full', 0):.1f}%" if "some" in psi else ""
        print("  " + col(g["name"], Style.BRIGHT + Fore.WHITE) + f" ({g['hostname']}, {g['ip']})  load "
              f"{' '.join(g['load'])} / {g['cpus']} CPU  iowait " + col(f_pct(iw), iwc) +
              f"  steal {f_pct(g.get('steal'))}{psi_txt}")
        busy = g.get("cpu_busy")
        bcol = Fore.RED + Style.BRIGHT if (busy or 0) >= 0.9 else Fore.YELLOW if (busy or 0) >= 0.7 else Fore.GREEN
        stc = Fore.YELLOW if (g.get("steal") or 0) >= 0.05 else ""
        line = ("    CPU busy " + col(f_pct(busy), bcol) + f" (user {f_pct(g.get('cpu_user'))}, sys "
                f"{f_pct(g.get('cpu_sys'))}, steal " + col(f_pct(g.get("steal")), stc) + ")")
        mm = g.get("mem")
        if mm:
            acol = Fore.RED + Style.BRIGHT if mm["avail_pct"] < 0.05 else Fore.YELLOW if mm["avail_pct"] < 0.15 \
                else Fore.GREEN
            swc = Fore.RED + Style.BRIGHT if g["swapin_ps"] + g["swapout_ps"] >= 100 else \
                Fore.YELLOW if g["swapin_ps"] + g["swapout_ps"] > 0 else ""
            line += ("   RAM available " + col(f"{f_num(mm['avail_gb'])} of {f_num(mm['total_gb'])} GB "
                                             f"({f_pct(mm['avail_pct'])})", acol) +
                     f"   swap used {f_num(mm['swap_used_gb'], 2)} GB, " +
                     col(f"in/out {f_num(g['swapin_ps'], 0)}/{f_num(g['swapout_ps'], 0)} pages/s", swc))
        pc, pm = g.get("psi_cpu") or {}, g.get("psi_mem") or {}
        if pc or pm:
            line += col(f"   PSI cpu {pc.get('some', 0):.1f}% mem {pm.get('some', 0):.1f}%", Style.DIM)
        print(line)
        ipi = g.get("ip_info") or {}
        if len(ipi.get("tried", [])) > 1:
            print("    " + col(f"IPv4 tested: {', '.join(ipi['tried'])}  -> used {g['ip']}", Style.DIM)
                  + (col(f"  (unreachable: {', '.join(ipi['unreachable'])})", Style.DIM) if ipi.get("unreachable")
                     else ""))
        if g.get("note"):
            print("    " + col(g["note"], Fore.YELLOW))
        elif g.get("remote_user") not in ("root", "") and not g.get("sudo"):
            print("    " + col(f"logged in as '{g['remote_user']}' without sudo - only its own processes are visible",
                            Fore.YELLOW))
        rows = []
        for d in g["disks"][:8]:
            if d["r_iops"] + d["w_iops"] < 0.1 and d["util"] < 0.01:
                continue
            ucol = Fore.RED + Style.BRIGHT if d["util"] >= 0.9 else Fore.YELLOW if d["util"] >= 0.6 else Fore.GREEN
            rows.append([trunc(d["name"], 30), f_num(d["r_iops"]), f_num(d["w_iops"]), f_num(d["r_mbs"], 2),
                         f_num(d["w_mbs"], 2), lat_cell(d["r_await"], cfg), lat_cell(d["w_await"], cfg),
                         (f_pct(d["util"]), ucol), str(d["inflight"])])
        print_table(["Guest disk", "r/s", "w/s", "rMB/s", "wMB/s", "r_await", "w_await", "Util", "InFl"], rows,
                    indent="    ")
        rows = []
        for p in g["procs"][:int(cfg.get("top_n", 10))]:
            share = (p["r_mbs"] + p["w_mbs"]) / g["total_mbs"] if g["total_mbs"] > 0 else None
            hot = Fore.RED + Style.BRIGHT if (share or 0) >= 0.5 and g["total_mbs"] >= 1 else ""
            rows.append([p["pid"], (trunc(p["comm"], 24), hot), f_num(p["r_mbs"], 2), f_num(p["w_mbs"], 2),
                         (f_pct(share), hot), f_num(p["rc_mbs"], 2), f_num(p["wc_mbs"], 2)])
        print_table(["PID", "Process", "Disk R MB/s", "Disk W MB/s", "Share", "Logical R", "Logical W"], rows,
                    ["r", "l"] + ["r"] * 5, indent="    ")
        n = max(3, int(cfg.get("top_n", 10)) // 2)
        cpu_top = [p for p in g.get("cpu_procs", []) if p["cpu"] >= 0.005][:n]
        mem_top = g.get("mem_procs", [])[:n]
        rows = []
        for i in range(max(len(cpu_top), len(mem_top))):
            r = []
            if i < len(cpu_top):
                p = cpu_top[i]
                ccol = Fore.RED + Style.BRIGHT if p["cpu"] >= 0.9 else Fore.YELLOW if p["cpu"] >= 0.5 else ""
                r += [p["pid"], trunc(p["comm"], 20), (f"{p['cpu'] * 100:.0f}%", ccol)]
            else:
                r += ["", "", ""]
            r.append("|")
            if i < len(mem_top):
                p = mem_top[i]
                share = p["rss_mb"] / (mm["total_gb"] * 1024) if mm else None
                r += [p["pid"], trunc(p["comm"], 20), f_num(p["rss_mb"] / 1024, 2), f_pct(share)]
            else:
                r += ["", "", "", ""]
            rows.append(r)
        print_table(["PID", "Top CPU process", "CPU", "|", "PID", "Top RAM process", "RSS GB", "of RAM"], rows,
                    ["r", "l", "r", "l", "r", "l", "r", "r"], indent="    ")
    print(col("  Disk R/W = real block IO (read_bytes/write_bytes), Logical = incl. page cache (rchar/wchar); "
              "process CPU 100% = one full core", Style.DIM))


def guest_findings(guests, cfg):
    F = []
    for g in guests.values():
        if g["error"]:
            F.append(("info", f"SSH to VM '{g['name']}' ({g['ip']}) failed: {g['error']}"))
            continue
        iw = g.get("iowait") or 0
        if iw >= 0.2:
            F.append(("warn", f"VM '{g['name']}': {iw:.0%} CPU iowait inside the guest - applications wait for disk."))
        for d in g["disks"]:
            if d["util"] >= 0.9:
                F.append(("warn", f"VM '{g['name']}': guest disk {d['name']} {d['util']:.0%} busy "
                                  f"(await R {f_num(d['r_await'])} / W {f_num(d['w_await'])} ms)."))
        mm = g.get("mem")
        if mm and mm["avail_pct"] < 0.10:
            F.append(("warn", f"VM '{g['name']}': only {f_pct(mm['avail_pct'])} RAM available inside the guest "
                              f"({f_num(mm['avail_gb'])} of {f_num(mm['total_gb'])} GB)."))
        sw = g.get("swapin_ps", 0) + g.get("swapout_ps", 0)
        if sw > 0:
            F.append(("crit" if sw >= 100 else "warn",
                      f"VM '{g['name']}': guest is SWAPPING ({f_num(g['swapin_ps'], 0)} in / "
                      f"{f_num(g['swapout_ps'], 0)} out pages/s) - lack of RAM turns into disk IO."))
        if (g.get("cpu_busy") or 0) >= 0.9:
            top = g.get("cpu_procs") or []
            who = f" - top: {top[0]['comm']} (PID {top[0]['pid']}, {top[0]['cpu'] * 100:.0f}%)" if top else ""
            F.append(("warn", f"VM '{g['name']}': guest CPU {f_pct(g['cpu_busy'])} busy{who}."))
        if (g.get("steal") or 0) >= 0.05:
            F.append(("warn", f"VM '{g['name']}': CPU steal {f_pct(g['steal'])} - the VM waits for physical CPU "
                              f"(host overcommitted, see CPU Ready)."))
        if g["procs"] and g["total_mbs"] >= 1:
            p = g["procs"][0]
            share = (p["r_mbs"] + p["w_mbs"]) / g["total_mbs"]
            if share >= 0.4:
                F.append(("info", f"VM '{g['name']}': top IO process '{p['comm']}' (PID {p['pid']}) "
                                  f"R {f_num(p['r_mbs'], 2)} / W {f_num(p['w_mbs'], 2)} MB/s ({share:.0%} of guest IO)."))
    return F


# =============================================================================
#  Report
# =============================================================================
def sort_key(cfg):
    by = cfg.get("sort_by", "iops")
    if by == "throughput":
        return lambda b: b["mbs"] or 0
    if by == "latency":
        return lambda b: b["lat"] or 0
    return lambda b: b["iops"] or 0


def snap_cell(vmrec):
    if vmrec.get("consolidate"):
        return ("CONSOLIDATE", Fore.RED + Style.BRIGHT)
    n = vmrec.get("snapshots", 0)
    if not n:
        return ("-", Style.DIM)
    return (str(n), Fore.YELLOW)


def render(inv, res, cfg, live=False, findings=None, label=None):
    top_n = int(cfg.get("top_n", 10))
    key = sort_key(cfg)
    window = res["samples"] * REALTIME_INTERVAL

    print()
    print(col("#" * 96, Fore.MAGENTA))
    title = label if label and label != inv["host_name"] else inv["host_name"]
    print(col("ESXi STORAGE LOAD ANALYZER", Style.BRIGHT + Fore.MAGENTA) +
          f"   host {col(title, Style.BRIGHT)}" +
          (f" ({inv['host_name']})" if title != inv["host_name"] else "") + f"   {inv['product']}")
    cpu = res.get("cpu")
    cpu_txt = col(f"{cpu:.0f}%", Fore.RED if cpu >= 90 else Fore.YELLOW if cpu >= 75 else Fore.GREEN) \
        if cpu is not None else "-"
    print(f"Time {res['time']:%Y-%m-%d %H:%M:%S}   window: last {res['samples']} x 20 s = "
          f"{window // 60} min {window % 60} s   host CPU {cpu_txt}   sorted by {cfg.get('sort_by')}")
    if not res.get("has_host_data"):
        p_err("No performance data returned for the host (license / permissions / statistics problem).")

    # ---------------- datastores ----------------
    section("DATASTORES")
    rows = []
    ds_list = sorted(inv["datastores"].values(), key=lambda d: -(res["ds"][d["uuid"]]["iops"] or 0))
    for ds in ds_list:
        r = res["ds"][ds["uuid"]]
        free_pct = ds["free"] / ds["capacity"] if ds["capacity"] else None
        free_col = Fore.RED + Style.BRIGHT if free_pct is not None and free_pct < 0.10 else \
            Fore.YELLOW if free_pct is not None and free_pct < 0.20 else ""
        vm_iops = sum(b["iops"] for _, b in res["consumers"].get(ds["uuid"], []))
        share = vm_iops / r["iops"] if r["iops"] >= 1 else None
        share_col = Fore.YELLOW if share is not None and share < 0.5 else ""
        name = ds["name"] if ds["accessible"] else ds["name"] + " (INACCESSIBLE)"
        rows.append([(trunc(name, 30), Fore.RED if not ds["accessible"] else ""), ds["type"],
                     f_int(ds["capacity"] / 1024 ** 3), (f_pct(free_pct), free_col),
                     f_int(r["r_iops"]), f_int(r["w_iops"]), f_num(r["r_mbs"]), f_num(r["w_mbs"]),
                     lat_cell(r["r_lat"], cfg), lat_cell(r["w_lat"], cfg), lat_cell(r["max_lat"], cfg),
                     (f_pct(min(share, 1.0)) if share is not None else "-", share_col)])
    print_table(["Datastore", "Type", "Size GB", "Free", "R IOPS", "W IOPS", "R MB/s", "W MB/s",
                 "R ms", "W ms", "Peak ms", "By VMs"], rows,
                ["l", "l"] + ["r"] * 10)

    # ---------------- devices ----------------
    section("STORAGE DEVICES (LUNs / disks)")
    rows = []
    for cn, d in sorted(inv["devices"].items(), key=lambda kv: (kv[1]["type"], kv[0])):
        r = res["dev"][cn]
        p = d["paths"]
        if p is None:
            pc = ("-", Style.DIM)
        elif p.get("active") is None:
            pc = (f"?/{p['total']}", "")
        else:
            txt = f"{p['active']}/{p['total']}" + (f" {p['dead']}dead" if p["dead"] else "")
            pc = (txt, Fore.RED + Style.BRIGHT if p["dead"] else
                  Fore.YELLOW if d["type"] == "iSCSI" and p["total"] == 1 else Fore.GREEN)
        ab = (r["aborts"] or 0) + (r["resets"] or 0)
        rows.append([trunc(cn, 34), d["type"], trunc(f"{d['vendor']} {d['model']}".strip(), 22),
                     trunc(", ".join(d["datastores"]) or "-", 22), pc,
                     f_int(r["r_iops"]), f_int(r["w_iops"]), f_num(r["mbs"]),
                     lat_cell(r["davg"], cfg), kq_cell(r["kavg"]), kq_cell(r["qavg"]),
                     lat_cell(r["gavg"], cfg), lat_cell(r["peak"], cfg),
                     (f_int(ab), Fore.RED + Style.BRIGHT if ab else Style.DIM)])
    print_table(["Device", "Type", "Model", "Datastore", "Paths", "R IOPS", "W IOPS", "MB/s",
                 "DAVG", "KAVG", "QAVG", "GAVG", "Peak", "Abort"], rows,
                ["l", "l", "l", "l", "l"] + ["r"] * 9)
    print(col("  DAVG = device/array latency (Synology, SSD)  KAVG = ESXi kernel  "
              "QAVG = waiting in ESXi queue  GAVG = total latency seen by VMs (ms)", Style.DIM))

    # ---------------- NICs ----------------
    section("NETWORK (vmnics - iSCSI traffic path)")
    rows = []
    for nic, r in sorted(res["nic"].items()):
        if not r["speed"] and not r["rx"] and not r["tx"]:
            continue
        u = r["util"]
        ucol = Fore.RED + Style.BRIGHT if u is not None and u * 100 >= cfg["nic_util_warn_pct"] else \
            Fore.YELLOW if u is not None and u >= 0.6 else Fore.GREEN
        rows.append([nic, f"{r['speed']} Mb" if r["speed"] else ("down", Fore.RED),
                     f_num(r["rx"]), f_num(r["tx"]), (f_pct(u), ucol),
                     (f_int(r["drops"]), Fore.YELLOW if r["drops"] else Style.DIM),
                     (f_int(r["errors"]), Fore.RED if r["errors"] else Style.DIM)])
    print_table(["vmnic", "Link", "RX MB/s", "TX MB/s", "Util", "Drops", "Errors"], rows)

    render_cpu_mem(inv, res, cfg)

    # ---------------- top VMs ----------------
    section(f"TOP VMs - all datastores (top {top_n})")
    rows = []
    vms = sorted(res["vm"].values(), key=lambda v: key(v["total"]), reverse=True)[:top_n]
    for v in vms:
        t = v["total"]
        ds_names = [inv["datastores"][u]["name"] for u in v["per_ds"] if u in inv["datastores"]
                    and (v["per_ds"][u]["iops"] or v["per_ds"][u]["mbs"])]
        rows.append([trunc(v["name"], 30), f_int(t["r_iops"]), f_int(t["w_iops"]), f_num(t["r_mbs"]),
                     f_num(t["w_mbs"]), lat_cell(t["r_lat"], cfg), lat_cell(t["w_lat"], cfg),
                     lat_cell(t["max_lat"], cfg), snap_cell(v), trunc(", ".join(ds_names) or "-", 40)])
    print_table(["VM", "R IOPS", "W IOPS", "R MB/s", "W MB/s", "R ms", "W ms", "Peak ms", "Snaps",
                 "Datastores"], rows, ["l"] + ["r"] * 8 + ["l"])

    # ---------------- per datastore consumers ----------------
    section("WHO LOADS WHICH DATASTORE")
    any_ds = False
    for ds in ds_list:
        r = res["ds"][ds["uuid"]]
        cons = res["consumers"].get(ds["uuid"], [])
        if (r["iops"] or 0) < 1 and (r["mbs"] or 0) < 0.1 and not cons:
            continue
        any_ds = True
        print()
        print("  " + col(ds["name"], Style.BRIGHT + Fore.WHITE) + f" ({ds['type']})  total "
              f"{f_int(r['iops'])} IOPS, {f_num(r['mbs'])} MB/s, latency "
              + col(f"R {f_num(r['r_lat'])} / W {f_num(r['w_lat'])} ms", lat_color(r["lat"], cfg)))
        rows = []
        cons = sorted(cons, key=lambda x: key(x[1]), reverse=True)
        for moid, b in cons[:top_n]:
            vmrec = res["vm"][moid]
            sh_i = b["iops"] / r["iops"] if r["iops"] >= 1 else None
            sh_m = b["mbs"] / r["mbs"] if r["mbs"] >= 0.1 else None
            hot = Fore.RED + Style.BRIGHT if (sh_i or 0) >= 0.5 or (sh_m or 0) >= 0.5 else \
                Fore.YELLOW if (sh_i or 0) >= 0.25 or (sh_m or 0) >= 0.25 else ""
            rows.append([(trunc(vmrec["name"], 30), hot), f_int(b["r_iops"]), f_int(b["w_iops"]),
                         f_num(b["r_mbs"]), f_num(b["w_mbs"]), lat_cell(b["r_lat"], cfg), lat_cell(b["w_lat"], cfg),
                         (f_pct(min(sh_i, 1)) if sh_i is not None else "-", hot),
                         (f_pct(min(sh_m, 1)) if sh_m is not None else "-", hot), snap_cell(vmrec)])
        vm_i = sum(b["iops"] for _, b in cons)
        vm_m = sum(b["mbs"] for _, b in cons)
        rest_i, rest_m = (r["iops"] or 0) - vm_i, (r["mbs"] or 0) - vm_m
        if rest_i > max(5, 0.05 * (r["iops"] or 0)) or rest_m > max(1, 0.05 * (r["mbs"] or 0)):
            rows.append([("<not from VMs: host/backup/other>", Fore.MAGENTA), f_int(max(rest_i, 0)), "", f_num(max(rest_m, 0)),
                         "", "", "", (f_pct(rest_i / r["iops"]) if r["iops"] >= 1 else "-", Fore.MAGENTA),
                         (f_pct(rest_m / r["mbs"]) if r["mbs"] >= 0.1 else "-", Fore.MAGENTA), ""])
        print_table(["VM", "R IOPS", "W IOPS", "R MB/s", "W MB/s", "R ms", "W ms", "IOPS %", "MB/s %", "Snaps"],
                    rows, ["l"] + ["r"] * 9, indent="    ")
    if not any_ds:
        print(col("  (no measurable IO on any datastore)", Style.DIM))

    # ---------------- virtual disks ----------------
    if cfg.get("show_vm_disks", True):
        section(f"BUSIEST VIRTUAL DISKS (top {top_n})")
        rows = []
        for d in sorted(res["vdisks"], key=key, reverse=True)[:top_n]:
            fname = d["file"].split("]")[-1].strip() if d["file"] else ""
            rows.append([trunc(d["vm"], 26), d["inst"], trunc(d["ds"], 20), trunc(fname.split("/")[-1], 30),
                         f_int(d["r_iops"]), f_int(d["w_iops"]), f_num(d["r_mbs"]), f_num(d["w_mbs"]),
                         lat_cell(d["r_lat"], cfg), lat_cell(d["w_lat"], cfg), lat_cell(d["max_lat"], cfg)])
        print_table(["VM", "Disk", "Datastore", "VMDK", "R IOPS", "W IOPS", "R MB/s", "W MB/s", "R ms", "W ms",
                     "Peak"], rows, ["l", "l", "l", "l"] + ["r"] * 7)

    # ---------------- guests ----------------
    render_guest_info(inv, cfg)
    if res.get("guests") is not None:
        render_guests(res["guests"], res.get("guest_skipped", []), cfg)

    # ---------------- findings ----------------
    section("FINDINGS")
    if findings is None:
        findings = build_findings(inv, res, cfg)
    tags = {"crit": ("[CRIT]", Fore.RED + Style.BRIGHT), "warn": ("[WARN]", Fore.YELLOW),
            "info": ("[INFO]", Fore.CYAN), "ok": ("[ OK ]", Fore.GREEN)}
    for sev, msg in findings:
        tag, c = tags[sev]
        print("  " + col(tag, c) + " " + msg)
    print()


def pct_cell(v, warn, crit, digits=0):
    if v is None:
        return ("-", Style.DIM)
    c = Fore.RED + Style.BRIGHT if v >= crit else Fore.YELLOW if v >= warn else Fore.GREEN
    return (f"{v * 100:.{digits}f}%", c)


def gb_cell(v, warn_if_positive=False, digits=1):
    if v is None:
        return ("-", Style.DIM)
    if warn_if_positive and v >= 0.01:
        return (f_num(v, digits), Fore.RED + Style.BRIGHT)
    return (f_num(v, digits), "" if v >= 0.01 or not warn_if_positive else Style.DIM)


def render_cpu_mem(inv, res, cfg):
    hc, hm, hw = res.get("hostcpu") or {}, res.get("hostmem") or {}, inv.get("hw") or {}
    section("CPU & MEMORY (host)")
    cpu_txt = col(f_pct(hc.get("pct")), pct_cell(hc.get("pct"), 0.75, 0.90)[1])
    mhz = f"{f_int(hc.get('used_mhz'))} / {f_int(hc.get('total_mhz'))} MHz" if hc.get("total_mhz") else ""
    print(f"  CPU    {cpu_txt} used (peak {f_pct(hc.get('peak_pct'))})  {mhz}   "
          f"{hw.get('sockets', '?')} socket(s), {hw.get('cores', '?')} cores / {hw.get('threads', '?')} threads"
          + (f"  {trunc(hw['cpu_model'], 45)}" if hw.get("cpu_model") else ""))
    if hm:
        mem_col = pct_cell(hm.get("pct"), 0.85, 0.95)[1]
        st = hm.get("state")
        st_col = Fore.GREEN if st in ("high", "clear", None) else Fore.YELLOW if st == "soft" else Fore.RED + Style.BRIGHT
        print(f"  RAM    " + col(f"{f_num(hm.get('used_gb'))} / {f_num(hm.get('total_gb'))} GB "
                                 f"({f_pct(hm.get('pct'))})", mem_col) +
              f" consumed, active {f_num(hm.get('active_gb'))} GB   state " + col(st or "-", st_col))
        bad = lambda v: Fore.RED + Style.BRIGHT if v and v > 0.001 else Style.DIM
        print("  Reclaim  balloon " + col(f"{f_num(hm['balloon_gb'], 2)} GB", bad(hm["balloon_gb"])) +
              "  compressed " + col(f"{f_num(hm['compressed_gb'], 2)} GB", bad(hm["compressed_gb"])) +
              "  swapped " + col(f"{f_num(hm['swapused_gb'], 2)} GB", bad(hm["swapused_gb"])) +
              "  swap in/out " + col(f"{f_num(hm['swapin_kbs'], 0)}/{f_num(hm['swapout_kbs'], 0)} KB/s",
                                     bad(hm["swapin_kbs"] + hm["swapout_kbs"])))
    vms = list((res.get("vmres") or {}).values())
    top_n = int(cfg.get("top_n", 10))

    section(f"TOP VMs by CPU (top {top_n})")
    rows = []
    for v in sorted(vms, key=lambda x: (x["cpu_mhz"] or 0, x["cpu_pct"] or 0), reverse=True)[:top_n]:
        rows.append([trunc(v["name"], 30), str(v["vcpu"] or "-"), pct_cell(v["cpu_pct"], 0.75, 0.90),
                     f_int(v["cpu_mhz"]), pct_cell(v["ready_pct"], 0.05, 0.10, 1),
                     pct_cell(v["costop_pct"], 0.03, 0.10, 1)])
    print_table(["VM", "vCPU", "CPU", "MHz", "Ready/vCPU", "CoStop"], rows)
    print(col("  Ready = VM waited for a physical CPU (>5% contention, >10% serious); "
              "CoStop high = too many vCPUs for the VM", Style.DIM))

    section(f"TOP VMs by MEMORY (top {top_n})")
    rows = []
    for v in sorted(vms, key=lambda x: x["consumed_gb"] or 0, reverse=True)[:top_n]:
        act = v["active_gb"] / v["mem_gb"] if v["active_gb"] is not None and v["mem_gb"] else None
        rows.append([trunc(v["name"], 30), f_num(v["mem_gb"]), f_num(v["consumed_gb"]), f_num(v["active_gb"]),
                     pct_cell(act, 0.80, 0.95), gb_cell(v["balloon_gb"], True, 2),
                     gb_cell(v["compressed_gb"], True, 2), gb_cell(v["swapped_gb"], True, 2),
                     (f_num(v["swapin_kbs"], 0), Fore.RED + Style.BRIGHT if (v["swapin_kbs"] or 0) > 0 else Style.DIM)])
    print_table(["VM", "Config GB", "Consumed", "Active", "Active %", "Balloon", "Compr.", "Swapped",
                 "Swap-in KB/s"], rows)
    print(col("  Balloon / compressed / swapped > 0 = host is short of RAM and takes memory away from VMs "
              "(hypervisor swap also creates disk load!)", Style.DIM))


def cpu_mem_findings(inv, res, cfg):
    F = []
    hc, hm = res.get("hostcpu") or {}, res.get("hostmem") or {}
    if hc.get("pct") is not None and hc["pct"] >= 0.90:
        F.append(("crit" if hc["pct"] >= 0.97 else "warn", f"Host CPU {f_pct(hc['pct'])} used - CPU saturation "
                                                           f"(raises KAVG / storage latency and VM Ready time)."))
    if hm:
        if hm.get("pct") is not None and hm["pct"] >= 0.95:
            F.append(("warn", f"Host RAM {f_pct(hm['pct'])} consumed ({f_num(hm['used_gb'])} of "
                              f"{f_num(hm['total_gb'])} GB)."))
        if hm.get("state") in ("hard", "low"):
            F.append(("crit", f"Host memory state '{hm['state']}' - ESXi is actively reclaiming memory "
                              f"(compression / swapping)."))
        elif hm.get("state") == "soft":
            F.append(("warn", "Host memory state 'soft' - ballooning active."))
        if (hm.get("swapin_kbs") or 0) + (hm.get("swapout_kbs") or 0) > 0:
            F.append(("crit", f"Host is SWAPPING VM memory ({f_num(hm['swapin_kbs'], 0)} in / "
                              f"{f_num(hm['swapout_kbs'], 0)} out KB/s) - very slow VMs and extra datastore IO."))
        elif (hm.get("swapused_gb") or 0) > 0.01:
            F.append(("warn", f"{f_num(hm['swapused_gb'], 2)} GB of VM memory lies in host swap files "
                              f"(currently no swap activity)."))
    for v in (res.get("vmres") or {}).values():
        if (v["ready_pct"] or 0) >= 0.05:
            F.append(("crit" if v["ready_pct"] >= 0.10 else "warn",
                      f"VM '{v['name']}': CPU Ready {f_pct(v['ready_pct'])} per vCPU - waits for physical CPU "
                      f"(host CPU overcommitted or too many vCPUs)."))
        if (v["costop_pct"] or 0) >= 0.03:
            F.append(("warn", f"VM '{v['name']}': Co-Stop {f_pct(v['costop_pct'])} - consider fewer vCPUs "
                              f"({v['vcpu']} now)."))
        if (v["cpu_pct"] or 0) >= 0.90:
            F.append(("warn", f"VM '{v['name']}': CPU {f_pct(v['cpu_pct'])} of its {v['vcpu']} vCPU(s)."))
        rec = [(n, x) for n, x in (("balloon", v["balloon_gb"]), ("compressed", v["compressed_gb"]),
                                    ("swapped", v["swapped_gb"])) if x and x >= 0.01]
        if rec:
            F.append(("crit" if (v["swapin_kbs"] or 0) > 0 or any(n == "swapped" for n, _ in rec) else "warn",
                      f"VM '{v['name']}': host reclaims its memory - "
                      + ", ".join(f"{n} {f_num(x, 2)} GB" for n, x in rec) + "."))
    return F


def build_findings(inv, res, cfg):
    F = []
    warn, crit = cfg["latency_warn_ms"], cfg["latency_crit_ms"]
    now = datetime.now(timezone.utc)

    for uuid, ds in inv["datastores"].items():
        r = res["ds"][uuid]
        if not ds["accessible"]:
            F.append(("crit", f"Datastore '{ds['name']}' is NOT accessible (LUN / path / Synology problem)."))
        if ds["capacity"]:
            fp = ds["free"] / ds["capacity"]
            if fp < 0.10:
                F.append(("crit", f"Datastore '{ds['name']}' is almost full ({fp:.0%} free). "
                                  f"For iSCSI also check that the Synology volume behind a thin LUN is not full."))
            elif fp < 0.20:
                F.append(("warn", f"Datastore '{ds['name']}' has only {fp:.0%} free space."))
        lat = max([x for x in (r["r_lat"], r["w_lat"]) if x is not None], default=None)
        if lat is None or lat < warn:
            continue
        lvl = "crit" if lat >= crit else "warn"
        F.append((lvl, f"Datastore '{ds['name']}' ({ds['type']}): latency R {f_num(r['r_lat'])} / "
                       f"W {f_num(r['w_lat'])} ms (peak {f_num(r['max_lat'])}) at {f_int(r['iops'])} IOPS, "
                       f"{f_num(r['mbs'])} MB/s."))
        cons = sorted(res["consumers"].get(uuid, []), key=lambda x: x[1]["iops"] + x[1]["mbs"] * 10, reverse=True)
        tot_i, tot_m = r["iops"] or 0, r["mbs"] or 0
        if cons and (tot_i >= 1 or tot_m >= 0.1):
            moid, b = cons[0]
            si_ = b["iops"] / tot_i if tot_i >= 1 else 0
            sm_ = b["mbs"] / tot_m if tot_m >= 0.1 else 0
            if max(si_, sm_) >= 0.4:
                F.append((lvl, f"   -> main load generator: VM '{res['vm'][moid]['name']}' "
                               f"({min(si_, 1):.0%} of IOPS, {min(sm_, 1):.0%} of throughput)."))
                g = (res.get("guests") or {}).get(moid)
                if g and not g.get("error") and g.get("procs"):
                    top = ", ".join(f"{p['comm']}[{p['pid']}] R {f_num(p['r_mbs'], 1)}/W {f_num(p['w_mbs'], 1)} MB/s"
                                    for p in g["procs"][:3])
                    F.append((lvl, f"   -> inside that VM the IO comes from: {top}"))
            else:
                names = ", ".join(res["vm"][m]["name"] for m, _ in cons[:3])
                F.append(("info", f"   -> load is spread over several VMs; biggest: {names}."))
        vm_i = sum(b["iops"] for _, b in cons)
        if tot_i >= 20 and vm_i / tot_i < 0.5:
            F.append(("warn", f"   -> {1 - vm_i / tot_i:.0%} of IO on this datastore does NOT come from running VMs: "
                              f"backup via NBD/hot-add, Storage vMotion/clone, VMFS UNMAP, or another host using "
                              f"the same LUN."))
        if ds.get("shared_with"):
            F.append(("warn", f"   -> this datastore is also mounted on: {', '.join(ds['shared_with'])} - their load "
                              f"hits the same LUN, see CROSS-HOST section."))
        if tot_i < 150 and tot_m < 15:
            F.append(("warn", "   -> latency is high although the load is LOW -> the bottleneck is outside the VMs. "
                              "For Synology check: RAID rebuild / data scrubbing, failing disk (SMART), SSD cache, "
                              "snapshot replication, Hyper Backup / Active Backup, other iSCSI initiators, "
                              "NAS CPU/RAM (Resource Monitor)."))
        snapped = [res["vm"][m]["name"] for m, _ in cons if res["vm"][m]["snapshots"]]
        if snapped:
            F.append(("warn", f"   -> VMs on this datastore running on snapshots: {', '.join(snapped[:5])}."))

    for cn, d in inv["devices"].items():
        r = res["dev"][cn]
        name = f"{d['type']} device {trunc(cn, 40)} ({', '.join(d['datastores']) or 'no datastore'})"
        p = d["paths"]
        if p and p.get("dead"):
            F.append(("crit", f"{name}: {p['dead']} DEAD path(s) of {p['total']} - check iSCSI network / NAS."))
        elif p and d["type"] == "iSCSI" and p["total"] == 1:
            F.append(("info", f"{name}: single iSCSI path - no MPIO redundancy / load balancing."))
        if r["aborts"]:
            F.append(("crit", f"{name}: {f_int(r['aborts'])} SCSI command(s) ABORTED - storage did not answer in "
                              f"time (array overloaded or network problem)."))
        if r["resets"]:
            F.append(("crit", f"{name}: {f_int(r['resets'])} bus reset(s)."))
        if r["davg"] is not None and r["davg"] >= warn:
            lvl = "crit" if r["davg"] >= crit else "warn"
            where = "Synology / iSCSI network" if d["type"] == "iSCSI" else "the physical disk itself"
            F.append((lvl, f"{name}: DAVG {f_num(r['davg'])} ms -> slowness is on the device side ({where})."))
        if r["kavg"] is not None and r["kavg"] >= KQ_WARN_MS:
            F.append(("warn", f"{name}: KAVG {f_num(r['kavg'])} ms -> ESXi kernel delay "
                              f"(host CPU contention or queue depth limits)."))
        if r["qavg"] is not None and r["qavg"] >= KQ_WARN_MS:
            F.append(("warn", f"{name}: QAVG {f_num(r['qavg'])} ms -> IOs waiting in ESXi queue, device queue "
                              f"depth exhausted (one VM flooding the LUN or array too slow)."))

    for nic, r in res["nic"].items():
        if r["util"] is not None and r["util"] * 100 >= cfg["nic_util_warn_pct"]:
            F.append(("warn", f"{nic}: {r['util']:.0%} of {r['speed']} Mb/s link used (RX {f_num(r['rx'])} / "
                              f"TX {f_num(r['tx'])} MB/s) - if this carries iSCSI, the network is the limit."))
        if r["errors"]:
            F.append(("crit", f"{nic}: {f_int(r['errors'])} packet errors (cable / SFP / switch port)."))
        elif r["drops"]:
            F.append(("warn", f"{nic}: {f_int(r['drops'])} dropped packets."))

    for v in inv["vms"].values():
        if v["consolidate"]:
            F.append(("crit", f"VM '{v['name']}' needs snapshot CONSOLIDATION."))
        if v["snapshots"] and v["oldest_snapshot"]:
            age = (now - v["oldest_snapshot"]).days
            F.append(("warn" if age >= 3 else "info",
                      f"VM '{v['name']}' has {v['snapshots']} snapshot(s), oldest {age} day(s) - "
                      f"delta disks slow down IO (leftover from backup?)."))

    F.extend(cpu_mem_findings(inv, res, cfg))

    for v in inv["vms"].values():
        if v["power"] != "poweredOn":
            continue
        for path, cap, free in v["guest"]["fs"]:
            if cap and free / cap < 0.05:
                F.append(("warn", f"VM '{v['name']}': guest filesystem {path} is {1 - free / cap:.0%} full."))
    if res.get("guests"):
        F.extend(guest_findings(res["guests"], cfg))

    if not any(s in ("crit", "warn") for s, _ in F):
        F.append(("ok", "No storage problem detected in the sampled window."))
    order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
    # keep datastore sub-lines ("   ->") right below their parent line: stable sort by block
    blocks, cur = [], None
    for s, m in F:
        if m.startswith("   ->") and cur is not None:
            cur[1].append((s, m))
        else:
            cur = [order[s], [(s, m)]]
            blocks.append(cur)
    blocks.sort(key=lambda b: b[0])
    return [item for _, items in blocks for item in items]


# =============================================================================
#  Main
# =============================================================================
# =============================================================================
#  Temporary helper folder  (<launch dir>/.temp, removed on exit)
# =============================================================================
_TEMP = {"root": None, "session": None, "closed": False}
SHUTDOWN = threading.Event()        # set when the dashboard closes - background work stops printing / writing


def temp_session_dir():
    """<cwd at start>/.temp/esxi_analyzer_<pid>  - created on first use, removed by cleanup_temp()."""
    if _TEMP["session"] and os.path.isdir(_TEMP["session"]):
        return _TEMP["session"]
    if _TEMP["closed"]:
        raise OSError(".temp already cleaned up (shutting down)")
    root = os.path.join(LAUNCH_DIR, ".temp")
    created = not os.path.isdir(root)
    os.makedirs(root, exist_ok=True)
    if created and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(root, 0x02)      # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass
    sess = os.path.join(root, f"esxi_analyzer_{os.getpid()}")
    os.makedirs(sess, exist_ok=True)
    _TEMP.update(root=root, session=sess)
    atexit.register(cleanup_temp)
    return sess


def cleanup_temp():
    """Remove our session folder and .temp itself when nothing else is left in it.
    Retries for a while - the browser engine (WebView2) may still hold files for a moment."""
    sess, root = _TEMP.get("session"), _TEMP.get("root")
    if not sess:
        return
    _TEMP["session"] = None
    _TEMP["closed"] = True
    for _ in range(20):
        shutil.rmtree(sess, ignore_errors=True)
        if not os.path.exists(sess):
            break
        time.sleep(0.5)
    if os.path.exists(sess):
        p_warn(f"Could not fully remove {sess} (files still locked) - delete it manually.")
    try:
        if root and os.path.isdir(root) and not os.listdir(root):
            os.rmdir(root)
    except OSError:
        pass


def install_exit_handlers():
    """Make sure atexit (temp cleanup, vSphere logout) runs also on SIGTERM / console close."""
    def handler(signum, _frame):
        raise SystemExit(128 + int(signum))
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# =============================================================================
#  Dashboard data (JSON for the web UI)
# =============================================================================
_UI_SKIP = {"obj", "hostkey"}


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items() if k not in _UI_SKIP}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(x) for x in o]
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, int):
        return int(o)
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return str(o)


def _ui_guest(g):
    g = dict(g)
    for k in ("procs", "cpu_procs", "mem_procs"):
        if isinstance(g.get(k), list):
            g[k] = g[k][:40]
    return g


def build_ui_data(ctxs, good, cfg, mode):
    hosts = []
    good_ids = {id(c) for c in good}
    for c in ctxs:
        base = {"label": c["label"], "address": c["cfg"].get("host", ""), "user": c["cfg"].get("user", "")}
        if c.get("error") or id(c) not in good_ids or not c.get("res"):
            base["error"] = c.get("error") or c.get("res_error") or "no data"
            hosts.append(base)
            continue
        res = dict(c["res"])
        res.pop("series", None)
        if res.get("guests") is not None:
            res["guests"] = {m: _ui_guest(g) for m, g in res["guests"].items()}
        base.update(inv=_jsonable(c["inv"]), res=_jsonable(res),
                    findings=[[s, m] for s, m in c.get("findings") or []])
        hosts.append(base)
    cross = {"shared": [], "findings": []}
    if len(good) > 1:
        shared, cf = cross_host(good, cfg)
        for uuid, s in shared.items():
            cross["shared"].append({
                "uuid": uuid, "name": s["name"], "type": s["type"], "tot_i": s["tot_i"], "tot_m": s["tot_m"],
                "per_host": [dict(_jsonable(r), host=hc["label"]) for hc, r in s["per_host"]],
                "vms": [dict(_jsonable(b), vm=vn, host=hc["label"]) for hc, vn, b in s["vms"]],
            })
        cross["findings"] = [[s, m] for s, m in cf]
    missing = [{"host": h, "vm": v, "problem": w, "impact": i, "hint": t}
               for h, v, w, i, t in (collect_missing_tools(good, cfg) if good else [])]
    keys = ("latency_warn_ms", "latency_crit_ms", "nic_util_warn_pct", "top_n", "sort_by", "samples",
            "live_samples", "show_vm_disks", "guest_ssh", "history_hours")
    ui_cfg = {k: cfg.get(k) for k in keys}
    ui_cfg.update(kq_warn_ms=KQ_WARN_MS, kq_crit_ms=KQ_CRIT_MS)
    samples = int(cfg.get("live_samples" if mode == "live" else "samples", 1))
    history = {c["label"]: HISTORY.payload(c["label"]) for c in ctxs}
    return _jsonable({"generated": datetime.now(), "mode": mode, "interval": REALTIME_INTERVAL,
                      "window_s": samples * REALTIME_INTERVAL, "cfg": ui_cfg, "hosts": hosts,
                      "cross": cross, "missing": missing, "history": history})


# =============================================================================
#  Dashboard window (pywebview, fullscreen)
# =============================================================================
_GUI = {"version": 0, "data_json": "{}", "data": None, "busy": False, "msg": "", "window": None, "fs": True,
        "live": False, "closing": False, "lock": threading.Lock(), "wake": threading.Event(),
        "snap_req": threading.Event(), "ctxs": None, "good": None, "cfg": None, "mode": "snapshot"}

EXPORT_NOTES = {
    "purpose": "ESXi storage / CPU / memory analysis export - full current state + per-sample history.",
    "current_state": "hosts[].inv = inventory, hosts[].res = averages over window_s seconds, hosts[].findings = "
                     "automatic diagnosis, cross = datastores shared by more hosts, missing = tools missing in guests",
    "history": "history[host] is columnar, one entry per 20 s sample: t = unix epoch (UTC seconds); "
               "cpu / mem = host usage fraction 0..1; ds[uuid].v = [IOPS, MB/s, read ms, write ms]; "
               "vm[moid].v = [IOPS, MB/s, latency ms, CPU MHz]; dev[canonical].v = [GAVG, DAVG, KAVG, QAVG ms, IOPS]; "
               "nic[vmnic].v = [RX MB/s, TX MB/s]; null = no sample. .n = display name.",
    "latency": "DAVG = device/array, KAVG = ESXi kernel, QAVG = ESXi queue, GAVG = total seen by VMs (ms)",
    "text_report": "the same report as printed in the console, without colors",
}


def webview_available():
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        p_warn("Dashboard skipped: module 'pywebview' missing  (pip install pywebview)")
        return False


def publish_ui(ctxs, good, cfg, mode):
    if SHUTDOWN.is_set():
        return _GUI["data_json"]
    data = build_ui_data(ctxs, good, cfg, mode)
    _GUI["version"] += 1
    data["version"] = _GUI["version"]
    txt = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    _GUI.update(data_json=txt, data=data, ctxs=ctxs, good=good, cfg=cfg, mode=mode)
    try:
        path = os.path.join(temp_session_dir(), "data.json")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.replace(path + ".tmp", path)
    except OSError as e:
        p_warn(f"Cannot write dashboard data into .temp: {e}")
    return txt


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def text_report(ctxs, good, cfg):
    """Console report as plain text (no colors). Caller holds _GUI['lock'] so nothing else prints meanwhile."""
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            render_all(ctxs, good, cfg)
    except Exception as e:
        buf.write(f"\n[text report failed: {type(e).__name__}: {e}]\n")
    return _ANSI_RE.sub("", buf.getvalue())


def export_payload(as_html=False):
    with _GUI["lock"]:
        data = dict(_GUI["data"] or {})
        ctxs, good, cfg = _GUI["ctxs"], _GUI["good"], _GUI["cfg"] or {}
        txt = text_report(ctxs, good, cfg) if ctxs is not None else ""
    if as_html:
        data["exported_at"] = datetime.now().isoformat()
        j = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return DASHBOARD_HTML.replace("__DATA__", j.replace("</", "<\\/"))
    safe_cfg = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in
                ("hosts", "guest_ssh_key", "guest_known_hosts", "password", "password_b64")}
    safe_cfg["hosts"] = [{"name": host_label(h), "host": h.get("host"), "user": h.get("user")}
                         for h in cfg.get("hosts") or []]
    out = {"format": "esxi-storage-analyzer/export", "format_version": 1,
           "exported_at": datetime.now().astimezone().isoformat(), "notes": EXPORT_NOTES,
           "config": safe_cfg, "text_report": txt}
    out.update(data)
    return json.dumps(out, ensure_ascii=False, indent=1)


class DashboardApi:
    """Methods callable from JavaScript (window.pywebview.api.*). No public attributes on purpose."""

    def get_state(self):
        return {"v": _GUI["version"], "busy": _GUI["busy"], "msg": _GUI["msg"], "fs": _GUI["fs"],
                "live": _GUI["live"]}

    def get_data(self):
        return _GUI["data_json"]

    def refresh(self):
        if _GUI["live"] or _GUI["busy"]:
            return False
        _GUI["snap_req"].set()
        _GUI["wake"].set()
        return True

    def set_live(self, on):
        on = bool(on)
        if on == _GUI["live"]:
            return on
        _GUI["live"] = on
        if not on:                       # leaving live -> fresh snapshot over the full window
            _GUI["snap_req"].set()
        _GUI["wake"].set()
        return on

    def save_report(self):
        import webview
        w = _GUI["window"]
        if w is None:
            return {"ok": False, "error": "window not available"}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None)
        if kind is None:
            kind = getattr(webview, "SAVE_DIALOG", 30)
        try:
            path = w.create_file_dialog(kind, directory=LAUNCH_DIR, save_filename=f"esxi_report_{stamp}.json",
                                        file_types=("JSON data for analysis (*.json)", "HTML dashboard (*.html)"))
        except Exception as e:
            return {"ok": False, "error": f"save dialog failed: {e}"}
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        if not path:
            return {"ok": False, "cancelled": True}
        path = str(path)
        if not os.path.splitext(path)[1]:
            path += ".json"
        try:
            content = export_payload(as_html=path.lower().endswith((".html", ".htm")))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            p_ok(f"Report saved: {path}")
            return {"ok": True, "path": path, "size": len(content.encode("utf-8"))}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def toggle_fullscreen(self):
        w = _GUI["window"]
        if w is not None:
            w.toggle_fullscreen()
            _GUI["fs"] = not _GUI["fs"]
        return _GUI["fs"]

    def close(self):
        # destroy AFTER this call has returned to JS - otherwise pywebview tries to deliver the
        # return value into an already destroyed window (InvalidAsynchronousStateException)
        _GUI["closing"] = True
        SHUTDOWN.set()
        _quiet_webview_logger()
        w = _GUI["window"]
        if w is not None:
            threading.Timer(0.4, w.destroy).start()
        return True


def _quiet_webview_logger():
    """Shutdown races inside pywebview (late JS callbacks) are harmless - do not print their tracebacks."""
    import logging
    for name in ("pywebview", "webview"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 10)


def run_gui(ctxs, good, cfg, mode):
    try:
        import webview
    except ImportError:
        p_warn("Dashboard skipped: install pywebview  (pip install pywebview)")
        return
    sess = temp_session_dir()
    _GUI["live"] = mode == "live"
    txt = publish_ui(ctxs, good, cfg, mode)
    html_path = os.path.join(sess, "dashboard.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(DASHBOARD_HTML.replace("__DATA__", txt.replace("</", "<\\/")))
    stop = threading.Event()

    def do_snapshot():
        _GUI["busy"], _GUI["msg"] = True, "Collecting performance data ..."
        try:
            with _GUI["lock"]:
                refresh_inventory(ctxs)
                g = analyze_all(ctxs, cfg, int(cfg["samples"]),
                                bool(cfg.get("guest_ssh") and cfg.get("guest_ssh_key")),
                                int(cfg.get("guest_sample_s", 5)))
                if not stop.is_set():
                    publish_ui(ctxs, g, cfg, "snapshot")
            _GUI["msg"] = ""
        except Exception as e:
            _GUI["msg"] = f"Refresh failed: {type(e).__name__}: {e}"
        finally:
            _GUI["busy"] = False

    def do_live(it):
        _GUI["busy"] = True
        try:
            with _GUI["lock"]:
                g = live_iteration(ctxs, cfg, it, gui=True)
                if not stop.is_set():
                    publish_ui(ctxs, g, cfg, "live")
            _GUI["msg"] = ""
        except Exception as e:
            _GUI["msg"] = f"Live update failed: {type(e).__name__}: {e}"
        finally:
            _GUI["busy"] = False

    def worker():
        # 1) history backfill: last hour from ESXi, so charts are not empty
        bf = int(cfg.get("history_backfill_samples", 180) or 0)
        if bf > 1:
            _GUI["busy"], _GUI["msg"] = True, "Loading history ..."
            try:
                with _GUI["lock"]:
                    backfill_history(ctxs, cfg, min(bf, 180))
                    if not stop.is_set():
                        publish_ui(ctxs, _GUI["good"] or good, cfg, _GUI["mode"])
            except Exception as e:
                p_warn(f"History backfill failed: {e}")
            finally:
                _GUI["busy"], _GUI["msg"] = False, ""
        # 2) live ticks / on-demand snapshots
        it, last = 1, time.time()
        while not stop.is_set():
            _GUI["wake"].wait(0.5)
            _GUI["wake"].clear()
            if stop.is_set():
                break
            if _GUI["snap_req"].is_set() and not _GUI["live"]:
                _GUI["snap_req"].clear()
                do_snapshot()
                last = time.time()
                continue
            if _GUI["live"] and time.time() - last >= REALTIME_INTERVAL:
                last = time.time()
                do_live(it)
                it += 1
            elif _GUI["live"] and _GUI["mode"] != "live":       # just switched on -> first tick now
                last = time.time()
                do_live(it)
                it += 1

    threading.Thread(target=worker, daemon=True).start()

    wkw = dict(title="ESXi Storage Analyzer", url=pathlib.Path(html_path).as_uri(), js_api=DashboardApi(),
               fullscreen=True, background_color="#090e1a", min_size=(1100, 700), text_select=True)
    try:
        window = webview.create_window(**wkw)
    except TypeError:                                   # older pywebview without text_select / min_size
        for k in ("text_select", "min_size"):
            wkw.pop(k, None)
        window = webview.create_window(**wkw)
    _GUI["window"] = window

    def on_closing(*_a):
        _GUI["closing"] = True
        SHUTDOWN.set()
        _quiet_webview_logger()
        stop.set()
        _GUI["wake"].set()
    try:
        window.events.closing += on_closing
    except Exception:
        pass
    try:
        window.events.closed += on_closing
    except Exception:
        pass
    p_info(f"Dashboard opened in fullscreen (helper files in {os.path.relpath(sess, LAUNCH_DIR)}). "
           f"F11 = window/fullscreen, L = live on/off, Ctrl+S = save report, Ctrl+Q = quit.")
    try:
        try:
            webview.start(private_mode=False, storage_path=os.path.join(sess, "webview"))
        except TypeError:                               # pywebview < 4
            webview.start()
    except Exception as e:
        p_err(f"Dashboard window could not be started: {type(e).__name__}: {e}")
    finally:
        SHUTDOWN.set()
        stop.set()
        _GUI["wake"].set()
        _GUI["window"] = None
        _quiet_webview_logger()
    p_info("Dashboard closed - cleaning up .temp ...")
    cleanup_temp()


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESXi Storage Analyzer</title>
<style>
:root{
  --bg:#090e1a;--bg2:#0c1322;--panel:#101929;--panel2:#152036;--line:#1d2942;--line2:#283754;
  --text:#e8edf7;--mute:#97a3bd;--dim:#5f6c8a;
  --acc:#7c8cff;--acc2:#2fd3c3;
  --ok:#34c88a;--warn:#f3b443;--crit:#ff5c70;--info:#4db8f2;--mag:#c87dff;
  --s1:#338ced;--s2:#e76624;--s3:#00a888;--s4:#d28e05;--s5:#e24d8b;--s6:#0aac23;--s7:#9887ef;--s8:#eb6263;
  --r:14px;--side:256px;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;color:var(--text);font:13px/1.45 "Segoe UI Variable Text","Segoe UI",Inter,"Helvetica Neue",Ubuntu,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
body{display:flex;overflow:hidden;background:
  radial-gradient(1100px 620px at 85% -15%,rgba(124,140,255,.13),transparent 60%),
  radial-gradient(900px 520px at -10% 115%,rgba(47,211,195,.08),transparent 60%),var(--bg)}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-thumb{background:#24314f;border-radius:8px;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:#31416a;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
.mono{font-family:"Cascadia Code","JetBrains Mono",Consolas,"DejaVu Sans Mono",monospace;font-size:11.5px}
.num{font-variant-numeric:tabular-nums}

/* ---------- sidebar ---------- */
aside{width:var(--side);flex:none;display:flex;flex-direction:column;border-right:1px solid var(--line);background:rgba(9,14,26,.78);backdrop-filter:blur(10px)}
.brand{padding:20px 18px 16px;display:flex;gap:12px;align-items:center}
.logo{width:38px;height:38px;border-radius:11px;flex:none;background:linear-gradient(135deg,var(--acc),var(--acc2));display:grid;place-items:center;box-shadow:0 8px 24px rgba(124,140,255,.35)}
.logo svg{width:21px;height:21px;stroke:#0b1020;fill:none;stroke-width:2.1;stroke-linecap:round;stroke-linejoin:round}
.brand h1{font-size:14.5px;margin:0;font-weight:650;letter-spacing:.2px}
.brand small{color:var(--mute);font-size:11px}
.sidescroll{flex:1;overflow:auto;padding-bottom:10px}
.side-sec{padding:14px 20px 6px;font-size:10.5px;letter-spacing:1.3px;text-transform:uppercase;color:var(--dim);font-weight:650}
.nav{padding:0 10px}
.ni{display:flex;align-items:center;gap:10px;padding:7px 10px;margin:1px 0;border-radius:9px;color:var(--mute);cursor:pointer;user-select:none;transition:background .15s,color .15s}
.ni:hover{background:var(--panel);color:var(--text)}
.ni.on{background:linear-gradient(90deg,rgba(124,140,255,.20),rgba(124,140,255,.03));color:var(--text);box-shadow:inset 3px 0 0 var(--acc)}
.ni svg{width:16px;height:16px;flex:none;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.ni .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnt{margin-left:auto;font-size:10.5px;font-weight:650;padding:1px 7px;border-radius:10px;background:var(--panel2);color:var(--mute)}
.cnt.crit{background:rgba(255,92,112,.17);color:var(--crit)}
.cnt.warn{background:rgba(243,180,67,.15);color:var(--warn)}
.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--dim)}
.dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(52,200,138,.15)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(243,180,67,.15)}
.dot.crit{background:var(--crit);box-shadow:0 0 0 3px rgba(255,92,112,.18)}
.dot.down{background:transparent;border:2px solid var(--crit);width:9px;height:9px}
.side-foot{padding:12px;border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 1fr;gap:8px}
.side-foot .meta{grid-column:1/-1;color:var(--dim);font-size:11px;line-height:1.5;padding:0 4px 4px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:8px 10px;border-radius:9px;border:1px solid var(--line2);background:var(--panel);color:var(--text);font:inherit;font-weight:550;cursor:pointer;transition:.15s}
.btn:hover{background:var(--panel2);border-color:#3a4c72}
.btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.btn.pri{background:linear-gradient(135deg,#6f80ff,#5b6cf0);border-color:transparent;color:#fff}
.btn.pri:hover{filter:brightness(1.08)}
.btn.danger:hover{background:rgba(255,92,112,.12);border-color:rgba(255,92,112,.5);color:#ffb3bd}
.btn[disabled]{opacity:.45;cursor:default;filter:none}
.btn.wide{grid-column:1/-1}
.spin{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* ---------- main ---------- */
main{flex:1;overflow:auto;position:relative}
.wrap{padding:24px 30px 60px;max-width:2000px}
.top{display:flex;align-items:flex-end;gap:18px;margin-bottom:20px;flex-wrap:wrap}
.top h2{margin:0;font-size:24px;font-weight:680;letter-spacing:-.2px}
.top .subt{color:var(--mute);margin-top:4px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.top .rt{margin-left:auto;display:flex;gap:10px;align-items:center;color:var(--mute);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;background:var(--panel2);border:1px solid var(--line2);font-size:11.5px;color:var(--mute)}
.pill.live{color:#ffd2d8;border-color:rgba(255,92,112,.45);background:rgba(255,92,112,.10)}
.pill.live i{width:7px;height:7px;border-radius:50%;background:var(--crit);animation:pulse 1.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,92,112,.7)}70%{box-shadow:0 0 0 8px rgba(255,92,112,0)}100%{box-shadow:0 0 0 0 rgba(255,92,112,0)}}
.sep{color:var(--dim)}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:14px}
.kpi{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;display:flex;align-items:center;gap:14px;min-height:96px;position:relative;overflow:hidden}
.kpi:after{content:"";position:absolute;inset:0 0 auto 0;height:2px;background:var(--kc,transparent);opacity:.9}
.kpi.ok{--kc:var(--ok)}.kpi.warn{--kc:var(--warn)}.kpi.crit{--kc:var(--crit)}
.kpi .lbl{color:var(--mute);font-size:10.5px;text-transform:uppercase;letter-spacing:.9px;font-weight:600}
.kpi .val{font-size:25px;font-weight:680;font-variant-numeric:tabular-nums;line-height:1.2;margin:2px 0}
.kpi .val small{font-size:13px;color:var(--mute);font-weight:500;margin-left:3px}
.kpi .sub{color:var(--dim);font-size:11.5px}
.ring{width:66px;height:66px;flex:none}
.ring .rbg{fill:none;stroke:var(--line2);stroke-width:7}
.ring .rfg{fill:none;stroke:currentColor;stroke-width:7;stroke-linecap:round;transition:stroke-dasharray .6s}
.ring text{fill:var(--text);font-size:15px;font-weight:680;text-anchor:middle;font-variant-numeric:tabular-nums}

.grid{display:grid;gap:14px;margin-bottom:14px}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
@media(max-width:1350px){.g2,.g3{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);margin-bottom:14px;overflow:hidden;box-shadow:0 10px 30px rgba(0,0,0,.18)}
.grid>.card{margin-bottom:0}
.ch{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);min-height:50px;flex-wrap:wrap}
.ch h3{margin:0;font-size:13.5px;font-weight:640}
.ch .s{color:var(--dim);font-size:12px}
.ch .rt{margin-left:auto;display:flex;gap:8px;align-items:center}
.cb{padding:14px 16px}
.cb.np{padding:0}
.note{color:var(--dim);font-size:11.5px;padding:10px 16px;border-top:1px solid var(--line)}
.empty{color:var(--dim);padding:22px 16px;text-align:center}
.search{background:var(--bg2) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%235f6c8a' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='M20 20l-4-4'/%3E%3C/svg%3E") no-repeat 9px center/14px;border:1px solid var(--line2);color:var(--text);border-radius:8px;padding:6px 10px 6px 30px;width:210px;font:inherit;outline:none;transition:.15s}
.search:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(124,140,255,.18)}

/* ---------- tables ---------- */
.tbl{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:10.5px;font-weight:650;color:var(--mute);text-transform:uppercase;letter-spacing:.55px;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none;background:rgba(21,32,54,.55)}
th:hover{color:var(--text)}
th.sorted{color:var(--acc)}
th .ar{font-size:9px;margin-left:4px}
td{padding:7px 12px;border-bottom:1px solid rgba(29,41,66,.65);white-space:nowrap;vertical-align:middle}
tbody tr{transition:background .1s}
tbody tr:hover td{background:rgba(124,140,255,.06)}
tbody tr:last-child td{border-bottom:none}
.l{text-align:left}.r{text-align:right}
td.wrapc{white-space:normal;min-width:260px}
.more{padding:9px;text-align:center;color:var(--acc);cursor:pointer;border-top:1px solid var(--line);font-weight:550}
.more:hover{background:rgba(124,140,255,.06)}
.c-ok{color:var(--ok)}.c-warn{color:var(--warn)}.c-crit{color:var(--crit);font-weight:650}.c-dim{color:var(--dim)}.c-info{color:var(--info)}.c-mag{color:var(--mag)}.c-mute{color:var(--mute)}
.badge{display:inline-block;padding:1px 8px;border-radius:6px;font-size:10.5px;font-weight:650;background:var(--panel2);color:var(--mute);border:1px solid var(--line2);vertical-align:1px;letter-spacing:.2px}
.badge.t-iSCSI{color:#a9b5ff;border-color:rgba(124,140,255,.4);background:rgba(124,140,255,.10)}
.badge.t-NVMe{color:#7ee8dc;border-color:rgba(47,211,195,.4);background:rgba(47,211,195,.09)}
.badge.t-SATA,.badge.t-SAS{color:#f7cf85;border-color:rgba(243,180,67,.35);background:rgba(243,180,67,.08)}
.badge.t-NFS{color:#ddb3ff;border-color:rgba(200,125,255,.4);background:rgba(200,125,255,.09)}
.badge.t-FC{color:#9fd8fa;border-color:rgba(77,184,242,.4);background:rgba(77,184,242,.09)}
.badge.b-crit{color:#ffb3bd;border-color:rgba(255,92,112,.5);background:rgba(255,92,112,.12)}
.badge.b-warn{color:#f7cf85;border-color:rgba(243,180,67,.4);background:rgba(243,180,67,.10)}
.badge.b-ok{color:#8fe3bd;border-color:rgba(52,200,138,.4);background:rgba(52,200,138,.09)}
.sub2{color:var(--dim);font-size:11px}
.bar{position:relative;min-width:120px;height:20px;border-radius:5px;background:var(--bg2);overflow:hidden;border:1px solid var(--line)}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--acc);opacity:.45;border-radius:4px;transition:width .5s}
.bar.ok i{background:var(--ok)}.bar.warn i{background:var(--warn);opacity:.55}.bar.crit i{background:var(--crit);opacity:.6}.bar.dim i{background:var(--dim)}
.bar b{position:relative;display:block;text-align:right;padding:0 7px;font-weight:600;font-size:11.5px;line-height:18px}

/* ---------- findings ---------- */
.fl{max-height:none}
.f{display:flex;gap:11px;padding:9px 16px;border-bottom:1px solid rgba(29,41,66,.6);align-items:flex-start}
.f .tag{font-size:10px;font-weight:750;padding:2px 0;width:44px;text-align:center;border-radius:5px;flex:none;letter-spacing:.6px;margin-top:1px}
.f .msg{flex:1;min-width:0;word-break:break-word}
.f-crit .tag{background:rgba(255,92,112,.16);color:var(--crit)}
.f-warn .tag{background:rgba(243,180,67,.14);color:var(--warn)}
.f-info .tag{background:rgba(77,184,242,.13);color:var(--info)}
.f-ok .tag{background:rgba(52,200,138,.14);color:var(--ok)}
.f.sub{padding:0 16px 7px 71px;color:var(--mute);border-bottom:none;margin-top:-3px}
.f.sub .tag{display:none}
.f.sub .msg:before{content:"↳ ";color:var(--dim)}
.fg{border-bottom:1px solid rgba(29,41,66,.6)}
.fg .f{border-bottom:none}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{padding:3px 10px;border-radius:20px;border:1px solid var(--line2);background:var(--bg2);color:var(--mute);font-size:11.5px;cursor:pointer;user-select:none;font-weight:600}
.chip.off{opacity:.4;text-decoration:line-through}
.chip.crit{color:var(--crit)}.chip.warn{color:var(--warn)}.chip.info{color:var(--info)}.chip.ok{color:var(--ok)}

/* ---------- charts ---------- */
.hb{display:grid;grid-template-columns:minmax(110px,210px) minmax(0,1fr) 112px;gap:12px;align-items:center;padding:5px 0}
.hb .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hb .tr{height:14px;border-radius:4px;background:var(--bg2);display:flex;gap:2px;overflow:hidden}
.hb .tr span{height:100%;border-radius:4px;min-width:0;transition:width .5s}
.hb .v{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.hb .v small{display:block;color:var(--dim);font-weight:500;font-size:11px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--mute);font-size:11.5px;align-items:center}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.lc{display:grid;grid-template-columns:minmax(120px,230px) minmax(0,1fr) 150px;gap:12px;align-items:center;padding:6px 0}
.lc .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lct{position:relative;height:22px;border-radius:5px;background:var(--bg2);display:flex;flex-direction:column;justify-content:center;gap:3px;padding:0}
.lct .lb{height:7px;border-radius:0 4px 4px 0;transition:width .5s}
.lb.ok{background:var(--ok)}.lb.warn{background:var(--warn)}.lb.crit{background:var(--crit)}.lb.dim{background:var(--dim)}
.lb.w{opacity:.6}
.mk{position:absolute;top:-3px;bottom:-3px;width:0;border-left:1px dashed}
.mk.w{border-color:rgba(243,180,67,.7)}.mk.c{border-color:rgba(255,92,112,.75)}
.lc .v{text-align:right;font-variant-numeric:tabular-nums;font-size:12px;color:var(--mute)}
.lc .v b{font-weight:650}
.stack{display:flex;height:26px;border-radius:6px;overflow:hidden;gap:2px;background:var(--bg2);margin:2px 0 10px}
.stack span{height:100%;min-width:3px;position:relative;cursor:default;transition:filter .15s}
.stack span:hover{filter:brightness(1.25)}
.hoverable{cursor:default}

.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;margin-bottom:14px}
.tile{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:var(--r);padding:16px;cursor:pointer;transition:.18s;position:relative;overflow:hidden}
.tile:hover{border-color:#3a4c72;transform:translateY(-2px);box-shadow:0 14px 30px rgba(0,0,0,.3)}
.tile h4{margin:0;font-size:15px;display:flex;align-items:center;gap:9px}
.tile .ad{color:var(--dim);font-size:11.5px;margin:2px 0 12px}
.tile .tm{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tile .tm div{background:var(--bg2);border:1px solid var(--line);border-radius:9px;padding:7px 9px}
.tile .tm small{display:block;color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.7px}
.tile .tm b{font-size:15px;font-variant-numeric:tabular-nums}
.tile .ws{margin-top:10px;color:var(--mute);font-size:12px}

.gstats{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line)}
.gs{background:var(--bg2);border:1px solid var(--line);border-radius:9px;padding:6px 11px;min-width:110px}
.gs small{display:block;color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.7px}
.gs b{font-variant-numeric:tabular-nums;font-weight:650}
.err{color:#ffb3bd;background:rgba(255,92,112,.08);border:1px solid rgba(255,92,112,.3);border-radius:10px;padding:12px 14px;margin:14px 16px}
.warnbox{color:#f7d79c;background:rgba(243,180,67,.07);border:1px solid rgba(243,180,67,.28);border-radius:10px;padding:10px 14px;margin:0 0 14px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px}
.kv dt{color:var(--dim)}.kv dd{margin:0;font-variant-numeric:tabular-nums}
.cols2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.cols2>div+div{border-left:1px solid var(--line)}
@media(max-width:1350px){.cols2{grid-template-columns:minmax(0,1fr)}.cols2>div+div{border-left:none;border-top:1px solid var(--line)}}
.subh{padding:10px 16px 4px;color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.8px;font-weight:650}

#busy{position:fixed;top:0;left:var(--side);right:0;height:3px;overflow:hidden;display:none;z-index:50}
#busy:before{content:"";position:absolute;top:0;bottom:0;width:35%;background:linear-gradient(90deg,transparent,var(--acc),var(--acc2),transparent);animation:slide 1.1s linear infinite}
@keyframes slide{from{left:-35%}to{left:100%}}
#tip{position:fixed;pointer-events:none;z-index:60;background:#1b2742;border:1px solid #33456c;border-radius:9px;padding:8px 11px;font-size:12px;box-shadow:0 10px 30px rgba(0,0,0,.45);display:none;max-width:380px}
#tip b{display:block;margin-bottom:2px}
#toast{position:fixed;bottom:20px;right:24px;z-index:70;display:flex;flex-direction:column;gap:8px}
.toast{background:#1b2742;border:1px solid #33456c;border-radius:10px;padding:10px 14px;box-shadow:0 12px 30px rgba(0,0,0,.4);animation:tin .25s ease-out}
.toast.err{border-color:rgba(255,92,112,.6);color:#ffc4cc}
@keyframes tin{from{transform:translateY(10px);opacity:0}}
.fade{animation:fade .3s ease-out}
@keyframes fade{from{opacity:.4}}
kbd{font:10.5px Consolas,monospace;padding:1px 5px;border-radius:4px;border:1px solid var(--line2);background:var(--bg2);color:var(--mute)}
.chart{position:relative}
.chart svg{display:block;overflow:visible}
.gl{stroke:rgba(40,55,84,.55);stroke-width:1}.gl.v{stroke:rgba(29,41,66,.45)}.gl.base{stroke:var(--line2)}
.ax{fill:var(--dim);font-size:10.5px;font-variant-numeric:tabular-nums}.ax.warn{fill:var(--warn)}.ax.crit{fill:var(--crit)}
.ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.ref{stroke-dasharray:4 4;stroke-width:1}.ref.warn{stroke:rgba(243,180,67,.75)}.ref.crit{stroke:rgba(255,92,112,.8)}
.xh{stroke:var(--mute);stroke-width:1;stroke-dasharray:3 3}
#tip .tr{display:flex;gap:8px;align-items:center;white-space:nowrap;line-height:1.6}
#tip .tr i{width:9px;height:9px;border-radius:3px;flex:none}
#tip .tr em{margin-left:auto;padding-left:16px;font-style:normal;font-weight:650;font-variant-numeric:tabular-nums}
.spark{width:110px;height:24px;display:block;margin-left:auto}
.spark path{fill:none;stroke:var(--s1);stroke-width:1.6}
.spark.warn path{stroke:var(--warn)}.spark.crit path{stroke:var(--crit)}.spark.ok path{stroke:var(--ok)}.spark.cpu path{stroke:var(--s3)}
.chip.on{color:var(--text);border-color:var(--acc);background:rgba(124,140,255,.16)}
.tog{grid-column:1/-1;display:flex;align-items:center;gap:11px;padding:9px 11px;border-radius:10px;border:1px solid var(--line2);background:var(--panel);cursor:pointer;user-select:none;transition:.15s}
.tog:hover{border-color:#3a4c72}
.tog b{display:block;font-size:12.5px;font-weight:600}.tog small{color:var(--dim);font-size:11px}
.tog.dis{opacity:.45;cursor:default}
.sw{width:38px;height:21px;border-radius:12px;background:var(--line2);position:relative;flex:none;transition:.2s}
.sw i{position:absolute;top:3px;left:3px;width:15px;height:15px;border-radius:50%;background:var(--mute);transition:.2s}
.sw.on{background:rgba(255,92,112,.32)}.sw.on i{left:20px;background:var(--crit);box-shadow:0 0 9px var(--crit)}
.pill[data-act]{cursor:pointer}.pill[data-act]:hover{border-color:#3a4c72}
.busymsg{color:var(--acc2)}
</style>
</head>
<body>
<aside>
  <div class="brand">
    <div class="logo"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/></svg></div>
    <div><h1>ESXi Storage Analyzer</h1><small id="brandsub">dashboard</small></div>
  </div>
  <div class="sidescroll" id="side"></div>
  <div class="side-foot" id="foot"></div>
</aside>
<main id="main"><div class="wrap" id="wrap"></div></main>
<div id="busy"></div><div id="tip"></div><div id="toast"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
let D = JSON.parse(document.getElementById('data').textContent);
const S = {host: null, view: null, sort: {}, filter: {}, expand: {}, sev: {crit: true, warn: true, info: true, ok: true}, busy: false, fs: true,
  range: 3600, live: D.mode === 'live', closing: false, msg: ''};
let POLL = 0;
const T = {};
let API = null;

/* ---------------- helpers ---------------- */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const isN = v => typeof v === 'number' && isFinite(v);
const fnum = (v, d = 1) => isN(v) ? v.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d}) : '–';
const fint = v => fnum(v, 0);
const fpct = (v, d = 0) => isN(v) ? (v * 100).toFixed(d) + '%' : '–';
const sum = (a, f) => a.reduce((s, x) => s + (f(x) || 0), 0);
const C = () => D.cfg || {};
const clamp = v => Math.max(0, Math.min(1, isN(v) ? v : 0));
const SER = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)', 'var(--s6)', 'var(--s7)', 'var(--s8)'];
function latC(v) { if (!isN(v)) return 'dim'; return v >= C().latency_crit_ms ? 'crit' : v >= C().latency_warn_ms ? 'warn' : 'ok'; }
function kqC(v) { if (!isN(v)) return 'dim'; return v >= C().kq_crit_ms ? 'crit' : v >= C().kq_warn_ms ? 'warn' : 'ok'; }
function pC(v, w, c) { if (!isN(v)) return 'dim'; return v >= c ? 'crit' : v >= w ? 'warn' : 'ok'; }
const span = (cls, txt) => `<span class="c-${cls}">${txt}</span>`;
const lat = v => span(latC(v), fnum(v));
const kq = v => span(kqC(v), fnum(v));
const badge = (t, cls) => `<span class="badge ${cls || ('t-' + esc(t).replace(/[^A-Za-z]/g, ''))}">${esc(t)}</span>`;
const bar = (frac, cls, text) => `<div class="bar ${cls || ''}"><i style="width:${(clamp(frac) * 100).toFixed(1)}%"></i><b>${text}</b></div>`;
const tm = iso => iso ? String(iso).replace('T', ' ').slice(0, 19) : '–';
const maxLat = b => { const v = [b.r_lat, b.w_lat].filter(isN); return v.length ? Math.max(...v) : null; };
const ICON = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  findings: '<path d="M5 21V4h11l-1.6 4L16 12H5"/>',
  datastores: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
  devices: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 14h.01M10 14h.01M14.5 10h3.5"/>',
  network: '<path d="M7 7h13l-4-4M17 17H4l4 4"/>',
  cpumem: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>',
  vms: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  consumers: '<path d="M21 12A9 9 0 1 1 12 3v9z"/><path d="M15 3.5A9 9 0 0 1 20.5 9H15z"/>',
  vdisks: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M12 3v3"/>',
  guests: '<path d="M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
  inguest: '<rect x="2.5" y="4" width="19" height="16" rx="2"/><path d="M6.5 9l3 3-3 3M12.5 15h5"/>',
  summary: '<rect x="3" y="3" width="18" height="7" rx="1.5"/><rect x="3" y="14" width="18" height="7" rx="1.5"/><path d="M7 6.5h.01M7 17.5h.01M12 10v4"/>',
  cross: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  missing: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L3 17.8V21h3.2l6.3-6.3a4 4 0 0 0 5.2-5.4l-2.5 2.5-2.6-.4-.4-2.6z"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.9-3M4 4v4h4M4 13a8 8 0 0 0 14.9 3M20 20v-4h-4"/>',
  fs: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  exit: '<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5M15 12H3"/>',
  history: '<path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5M12 7v5l3.5 2.5"/>',
  save: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>'
};
const ico = (n, cls) => `<svg viewBox="0 0 24 24" class="${cls || ''}">${ICON[n] || ''}</svg>`;

/* ---------------- tables ---------------- */
function table(id, cols, rows, o = {}) {
  T[id] = {cols, rows, o};
  return `<div class="tbl" id="t-${id}">${tableInner(id)}</div>`;
}
function cellVal(c, x) { return c.v ? c.v(x) : x[c.k]; }
function tableInner(id) {
  const {cols, rows, o} = T[id];
  let r = rows.slice();
  const q = (S.filter[id] || '').trim().toLowerCase();
  if (q) r = r.filter(x => cols.some(c => { const v = c.s ? c.s(x) : cellVal(c, x); return v != null && String(v).toLowerCase().includes(q); }));
  const st = S.sort[id] || o.sort;
  if (st) {
    const c = cols.find(c => c.k === st.k);
    if (c) r.sort((a, b) => {
      const x = cellVal(c, a), y = cellVal(c, b);
      const xn = x == null || x === '', yn = y == null || y === '';
      if (xn && yn) return 0; if (xn) return 1; if (yn) return -1;
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), undefined, {numeric: true})) * st.d;
    });
  }
  if (!r.length) return `<div class="empty">${q ? 'Nothing matches the filter' : (o.empty || 'No data')}</div>`;
  const total = r.length;
  const lim = o.limit && !q && !S.expand[id] ? o.limit : 0;
  if (lim) r = r.slice(0, lim);
  let h = '<table><thead><tr>';
  for (const c of cols) {
    const on = st && st.k === c.k;
    h += `<th class="${c.a === 'l' ? 'l' : 'r'}${on ? ' sorted' : ''}" data-t="${id}" data-k="${c.k}"${c.t ? ` title="${esc(c.t)}"` : ''}>${c.l}${on ? `<span class="ar">${st.d > 0 ? '▲' : '▼'}</span>` : ''}</th>`;
  }
  h += '</tr></thead><tbody>';
  for (const x of r) {
    h += '<tr>';
    for (const c of cols) h += `<td class="${c.a === 'l' ? 'l' : 'r'} ${c.cls ? c.cls(x) : ''}">${c.f ? c.f(x) : esc(x[c.k] ?? '–')}</td>`;
    h += '</tr>';
  }
  h += '</tbody></table>';
  if (lim && total > lim) h += `<div class="more" data-more="${id}">Show all ${total} rows</div>`;
  else if (o.limit && S.expand[id] && total > o.limit) h += `<div class="more" data-more="${id}">Show top ${o.limit} only</div>`;
  return h;
}
function rerenderTable(id) { const el = document.getElementById('t-' + id); if (el && T[id]) el.innerHTML = tableInner(id); }
const search = id => `<input class="search" data-filter="${id}" placeholder="Filter…" value="${esc(S.filter[id] || '')}">`;
function card(title, body, o = {}) {
  return `<div class="card ${o.cls || ''}"><div class="ch"><h3>${title}</h3>${o.sub ? `<span class="s">${o.sub}</span>` : ''}<div class="rt">${o.right || ''}${o.filter ? search(o.filter) : ''}</div></div>` +
    `<div class="cb ${o.np ? 'np' : ''}">${body}</div>${o.note ? `<div class="note">${o.note}</div>` : ''}</div>`;
}

/* ---------------- KPI / ring ---------------- */
function ring(frac, cls) {
  const r = 28, c = 2 * Math.PI * r, f = clamp(frac);
  return `<svg class="ring c-${cls}" viewBox="0 0 72 72"><circle cx="36" cy="36" r="${r}" class="rbg"/>` +
    `<circle cx="36" cy="36" r="${r}" class="rfg" stroke-dasharray="${(c * f).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 36 36)"/>` +
    `<text x="36" y="41">${isN(frac) ? Math.round(frac * 100) + '%' : '–'}</text></svg>`;
}
const kpiRing = (lbl, frac, cls, sub1, sub2) => `<div class="kpi ${cls}">${ring(frac, cls)}<div><div class="lbl">${lbl}</div><div class="sub" style="color:var(--mute);margin-top:4px">${sub1 || ''}</div><div class="sub">${sub2 || ''}</div></div></div>`;
const kpi = (lbl, val, sub, cls) => `<div class="kpi ${cls || ''}"><div><div class="lbl">${lbl}</div><div class="val">${val}</div><div class="sub">${sub || ''}</div></div></div>`;

/* ---------------- tooltip ---------------- */
document.addEventListener('mousemove', e => {
  if (e.target.closest && e.target.closest('.chart')) return;
  const t = e.target.closest('[data-tip]'), tip = $('#tip');
  if (!t) { tip.style.display = 'none'; return; }
  tip.innerHTML = t.dataset.tip; tip.style.display = 'block';
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + w > innerWidth - 8) x = e.clientX - w - 14;
  if (y + h > innerHeight - 8) y = e.clientY - h - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
const tipAttr = html => `data-tip="${esc(html)}"`;

/* ---------------- derived data ---------------- */
function hostStatus(h) {
  if (h.error) return 'down';
  const f = h.findings || [];
  return f.some(x => x[0] === 'crit') ? 'crit' : f.some(x => x[0] === 'warn') ? 'warn' : 'ok';
}
function fcount(F) { const c = {crit: 0, warn: 0, info: 0, ok: 0}; for (const [s] of F || []) c[s] = (c[s] || 0) + 1; return c; }
function dsRows(h) {
  const r = h.res;
  return Object.values(h.inv.datastores || {}).map(ds => {
    const p = r.ds[ds.uuid] || {};
    const cons = r.consumers[ds.uuid] || [];
    const vmI = sum(cons, x => x[1].iops);
    return Object.assign({}, p, ds, {freePct: ds.capacity ? ds.free / ds.capacity : null,
      vmShare: (p.iops || 0) >= 1 ? Math.min(vmI / p.iops, 1) : null, cons, lmax: maxLat(p)});
  });
}
function devRows(h) { return Object.values(h.inv.devices || {}).map(d => Object.assign({}, h.res.dev[d.name] || {}, d)); }
function vmRows(h) {
  const inv = h.inv;
  return Object.entries(h.res.vm || {}).map(([moid, v]) => Object.assign({}, v.total, {
    moid, name: v.name, snapshots: v.snapshots, consolidate: v.consolidate,
    dsNames: Object.keys(v.per_ds || {}).filter(u => inv.datastores[u] && (v.per_ds[u].iops || v.per_ds[u].mbs)).map(u => inv.datastores[u].name)
  }));
}
function sortMetric() { const by = C().sort_by; return by === 'throughput' ? 'mbs' : by === 'latency' ? 'lat' : 'iops'; }
const snapCell = v => v.consolidate ? '<span class="badge b-crit">CONSOLIDATE</span>' : v.snapshots ? `<span class="badge b-warn">${v.snapshots}</span>` : span('dim', '–');
const H = () => D.hosts[S.host];

/* ---------------- findings ---------------- */
function findingsHtml(F, limit) {
  const groups = [];
  for (const [s, m] of F || []) {
    if (m.startsWith('   ->') && groups.length) groups[groups.length - 1].subs.push([s, m.replace(/^\s*->\s*/, '')]);
    else groups.push({s, m, subs: []});
  }
  const vis = groups.filter(g => S.sev[g.s]);
  if (!vis.length) return '<div class="empty">No findings for the selected severities</div>';
  const shown = limit ? vis.slice(0, limit) : vis;
  const TAG = {crit: 'CRIT', warn: 'WARN', info: 'INFO', ok: 'OK'};
  let h = '<div class="fl">';
  for (const g of shown) {
    h += `<div class="fg"><div class="f f-${g.s}"><span class="tag">${TAG[g.s]}</span><div class="msg">${esc(g.m)}</div></div>`;
    for (const [s, m] of g.subs) h += `<div class="f sub f-${s}"><span class="tag"></span><div class="msg">${esc(m)}</div></div>`;
    h += '</div>';
  }
  if (limit && vis.length > limit) h += `<div class="more" data-goto="findings">${vis.length - limit} more findings →</div>`;
  return h + '</div>';
}
function sevChips(F) {
  const c = fcount(F);
  return '<div class="chips">' + ['crit', 'warn', 'info', 'ok'].filter(s => c[s]).map(s =>
    `<span class="chip ${s}${S.sev[s] ? '' : ' off'}" data-sev="${s}">${s.toUpperCase()} ${c[s]}</span>`).join('') + '</div>';
}

/* ---------------- charts ---------------- */
function latChart(ds) {
  const rows = ds.filter(d => isN(d.r_lat) || isN(d.w_lat)).sort((a, b) => (b.lmax || 0) - (a.lmax || 0));
  if (!rows.length) return '<div class="empty">No latency data</div>';
  const w = C().latency_warn_ms, c = C().latency_crit_ms;
  const mx = Math.max(c * 1.25, ...rows.map(d => d.lmax || 0)) * 1.05;
  let h = `<div class="legend" style="margin-bottom:8px"><span><i style="background:var(--ok)"></i>read (top)</span><span><i style="background:var(--ok);opacity:.6"></i>write (bottom)</span><span style="color:var(--warn)">┆ warn ${w} ms</span><span style="color:var(--crit)">┆ crit ${c} ms</span></div>`;
  for (const d of rows) {
    const tip = `<b>${esc(d.name)}</b>Read ${fnum(d.r_lat)} ms · Write ${fnum(d.w_lat)} ms<br>Peak ${fnum(d.max_lat)} ms · ${fint(d.iops)} IOPS · ${fnum(d.mbs)} MB/s`;
    h += `<div class="lc hoverable" ${tipAttr(tip)}><div class="n">${esc(d.name)} ${badge(d.type)}</div><div class="lct">` +
      `<i class="mk w" style="left:${(w / mx * 100).toFixed(1)}%"></i><i class="mk c" style="left:${(c / mx * 100).toFixed(1)}%"></i>` +
      `<span class="lb ${latC(d.r_lat)}" style="width:${((d.r_lat || 0) / mx * 100).toFixed(1)}%"></span>` +
      `<span class="lb w ${latC(d.w_lat)}" style="width:${((d.w_lat || 0) / mx * 100).toFixed(1)}%"></span></div>` +
      `<div class="v">R <b class="c-${latC(d.r_lat)}">${fnum(d.r_lat)}</b> · W <b class="c-${latC(d.w_lat)}">${fnum(d.w_lat)}</b> ms</div></div>`;
  }
  return h;
}
function hbars(items, legend) {
  if (!items.length) return '<div class="empty">No data</div>';
  const mx = Math.max(...items.map(i => sum(i.segs, s => s.v)), 1e-9);
  let h = legend ? `<div class="legend" style="margin-bottom:8px">${legend.map(([n, c]) => `<span><i style="background:${c}"></i>${n}</span>`).join('')}</div>` : '';
  for (const it of items) {
    h += `<div class="hb hoverable" ${tipAttr(it.tip || '')}><div class="n">${esc(it.label)}</div><div class="tr">` +
      it.segs.map(s => `<span style="width:${(s.v / mx * 100).toFixed(2)}%;background:${s.c}"></span>`).join('') +
      `</div><div class="v">${it.text}${it.sub ? `<small>${it.sub}</small>` : ''}</div></div>`;
  }
  return h;
}


/* ---------------- history: line charts ---------------- */
const CH = {};
const RANGES = [[900, '15 m'], [3600, '1 h'], [10800, '3 h'], [21600, '6 h'], [0, 'All']];
const rangeChips = () => '<div class="chips">' + RANGES.map(([v, l]) => `<span class="chip rng${S.range === v ? ' on' : ''}" data-range="${v}">${l}</span>`).join('') + '</div>';
const HIS = h => (D.history || {})[h.label];
function hctx(h) {
  const hi = HIS(h);
  if (!hi || !hi.t || !hi.t.length) return null;
  const n = hi.t.length, t1 = hi.t[n - 1], from = S.range ? t1 - S.range : hi.t[0];
  let i0 = 0; while (i0 < n && hi.t[i0] < from) i0++;
  return {hi, i0, t: hi.t.slice(i0), t0: Math.max(from, hi.t[0]), n: n - i0};
}
const colv = (hc, grp, k, f) => ((hc.hi[grp][k] || {}).v || []).slice(hc.i0).map(a => a ? f(a) : null);
const avgOf = a => { const v = a.filter(isN); return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null; };
const peakOf = a => { const v = a.filter(isN); return v.length ? Math.max(...v) : null; };
const wavg = (vals, w) => { let s = 0, ws = 0; vals.forEach((v, i) => { if (isN(v) && isN(w[i]) && w[i] > 0) { s += v * w[i]; ws += w[i]; } }); return ws > 0 ? s / ws : avgOf(vals); };
const latOf = a => (isN(a[2]) || isN(a[3])) ? Math.max(a[2] || 0, a[3] || 0) : null;
function dsColor(h) { const names = Object.values(h.inv.datastores || {}).map(d => d.name).sort(); return n => { const i = names.indexOf(n); return i >= 0 ? SER[i % 8] : '#8893b0'; }; }
function dsSeries(h, hc, f) {
  const col = dsColor(h);
  return Object.entries(hc.hi.ds).map(([u, o]) => ({n: o.n, c: col(o.n), v: colv(hc, 'ds', u, f)}))
    .filter(s => s.v.some(isN)).sort((a, b) => a.n.localeCompare(b.n)).slice(0, 8);
}
function topSeries(hc, grp, f, n = 8) {
  return Object.entries(hc.hi[grp]).map(([k, o]) => { const v = colv(hc, grp, k, f); return {n: o.n, v, a: avgOf(v) || 0}; })
    .filter(s => s.a > 0).sort((a, b) => b.a - a.a).slice(0, n).map((s, i) => Object.assign(s, {c: SER[i % 8]}));
}
function chart(id, spec) { CH[id] = spec; return `<div class="chart" id="c-${id}" style="height:${spec.h || 220}px"></div>`; }
const chartLegend = series => series.length ? `<div class="legend" style="margin-bottom:6px">${series.map(s => `<span><i style="background:${s.c}"></i>${esc(s.n)}</span>`).join('')}</div>` : '';
function chartCard(title, hc, series, spec, o = {}) {
  if (!hc) return card(title, '<div class="empty">No history yet – it grows with every 20 s sample</div>', {right: rangeChips()});
  const id = o.id || title.replace(/[^a-z0-9]/gi, '').toLowerCase();
  return card(title, chartLegend(series) + chart(id, Object.assign({t: hc.t, t0: hc.t0, series}, spec)), {right: rangeChips(), sub: o.sub});
}
function niceMax(v) { if (!(v > 0)) return 1; const p = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v - 1e-12) return m * p; return 10 * p; }
const hhmm = t => { const d = new Date(t * 1000); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
const hhmmss = t => hhmm(t) + ':' + String(new Date(t * 1000).getSeconds()).padStart(2, '0');
function drawChart(id) {
  const sp = CH[id], el = document.getElementById('c-' + id);
  if (!sp || !el) return;
  const t = sp.t, W = Math.max(240, el.clientWidth), Hh = sp.h || 220, m = {l: 52, r: 14, t: 10, b: 24};
  if (!t.length || !sp.series.length) { el.innerHTML = '<div class="empty">No data in the selected range</div>'; return; }
  const t1 = t[t.length - 1], t0 = Math.min(sp.t0 ?? t[0], t1 - 60), span = t1 - t0;
  let ymax = 0;
  for (const s of sp.series) for (const v of s.v) if (isN(v) && v > ymax) ymax = v;
  for (const r of sp.refs || []) if (ymax >= r.v * 0.5) ymax = Math.max(ymax, r.v);
  ymax = sp.max || niceMax(ymax * 1.06);
  const iw = W - m.l - m.r, ih = Hh - m.t - m.b;
  const X = tt => m.l + (tt - t0) / span * iw, Y = v => m.t + (1 - Math.min(v, ymax) / ymax) * ih;
  const yf = sp.yf || (v => v >= 1000 ? fnum(v / 1000, v >= 10000 ? 0 : 1) + 'k' : fnum(v, v < 10 && v % 1 ? 1 : 0));
  let g = '';
  for (let i = 0; i <= 4; i++) { const v = ymax * i / 4, y = Y(v).toFixed(1); g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" class="gl${i ? '' : ' base'}"/><text x="${m.l - 8}" y="${+y + 4}" class="ax" text-anchor="end">${yf(v)}</text>`; }
  const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200];
  const step = steps.find(s => span / s <= Math.max(3, Math.floor(iw / 110))) || 86400;
  for (let tt = Math.ceil(t0 / step) * step; tt <= t1; tt += step) { const x = X(tt).toFixed(1); g += `<line x1="${x}" x2="${x}" y1="${m.t}" y2="${m.t + ih}" class="gl v"/><text x="${x}" y="${Hh - 6}" class="ax" text-anchor="middle">${hhmm(tt)}</text>`; }
  for (const r of sp.refs || []) if (r.v <= ymax) { const y = Y(r.v).toFixed(1); g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" class="ref ${r.cls}"/><text x="${W - m.r - 4}" y="${+y - 4}" class="ax ${r.cls}" text-anchor="end">${esc(r.l)}</text>`; }
  const gap = 3 * (D.interval || 20);
  let paths = '';
  for (const s of sp.series) {
    let d = '', pen = false, prev = -1, dots = '', cnt = 0;
    for (let i = 0; i < t.length; i++) {
      const v = s.v[i];
      if (!isN(v)) { pen = false; continue; }
      if (pen && t[i] - t[prev] > gap) pen = false;
      const x = X(t[i]).toFixed(1), y = Y(v).toFixed(1);
      if (!pen && (i + 1 >= t.length || !isN(s.v[i + 1]) || t[i + 1] - t[i] > gap)) dots += `<circle cx="${x}" cy="${y}" r="2.5" fill="${s.c}"/>`;
      d += (pen ? 'L' : 'M') + x + ' ' + y; pen = true; prev = i; cnt++;
    }
    paths += `<path d="${d}" stroke="${s.c}" class="ln"/>${dots}`;
  }
  el.innerHTML = `<svg width="${W}" height="${Hh}">${g}${paths}<line class="xh" y1="${m.t}" y2="${m.t + ih}" x1="0" x2="0" visibility="hidden"/><g class="hd"></g></svg>`;
  const xh = el.querySelector('.xh'), hd = el.querySelector('.hd'), tip = $('#tip');
  const vf = sp.vf || (v => fnum(v, v < 10 ? 1 : 0));
  el.onmousemove = e => {
    const r = el.getBoundingClientRect(), px = e.clientX - r.left;
    if (px < m.l - 4 || px > W - m.r + 4) { el.onmouseleave(); return; }
    const tt = t0 + (px - m.l) / iw * span;
    let lo = 0, hi = t.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (t[mid] < tt) lo = mid; else hi = mid; }
    const i = Math.abs(t[lo] - tt) <= Math.abs(t[hi] - tt) ? lo : hi, x = X(t[i]).toFixed(1);
    xh.setAttribute('x1', x); xh.setAttribute('x2', x); xh.setAttribute('visibility', 'visible');
    const rows = sp.series.map(s => ({s, v: s.v[i]})).filter(o => isN(o.v)).sort((a, b) => b.v - a.v);
    hd.innerHTML = rows.map(o => `<circle cx="${x}" cy="${Y(o.v).toFixed(1)}" r="4" fill="${o.s.c}" stroke="#101929" stroke-width="2"/>`).join('');
    tip.innerHTML = `<b>${hhmmss(t[i])}</b>` + (rows.length ? rows.slice(0, 12).map(o => `<div class="tr"><i style="background:${o.s.c}"></i><span>${esc(o.s.n)}</span><em>${vf(o.v)}${sp.unit ? ' ' + sp.unit : ''}</em></div>`).join('') : '<div class="c-dim">no sample</div>');
    tip.style.display = 'block';
    const w = tip.offsetWidth, h2 = tip.offsetHeight;
    let tx = e.clientX + 16, ty = e.clientY + 12;
    if (tx + w > innerWidth - 8) tx = e.clientX - w - 16;
    if (ty + h2 > innerHeight - 8) ty = innerHeight - h2 - 8;
    tip.style.left = tx + 'px'; tip.style.top = ty + 'px';
  };
  el.onmouseleave = () => { xh.setAttribute('visibility', 'hidden'); hd.innerHTML = ''; tip.style.display = 'none'; };
}
function drawCharts() { for (const id in CH) drawChart(id); }
let _rz = 0;
window.addEventListener('resize', () => { clearTimeout(_rz); _rz = setTimeout(drawCharts, 120); });
function spark(vals, cls) {
  const v = vals.filter(isN);
  if (v.length < 2) return span('dim', '–');
  const mx = Math.max(...v) || 1, n = vals.length;
  let d = '', pen = false;
  vals.forEach((x, i) => { if (!isN(x)) { pen = false; return; } d += (pen ? 'L' : 'M') + (n > 1 ? i / (n - 1) * 100 : 0).toFixed(2) + ' ' + (24 - x / mx * 21).toFixed(2); pen = true; });
  return `<svg class="spark ${cls || ''}" viewBox="0 0 100 26" preserveAspectRatio="none"><path d="${d}" vector-effect="non-scaling-stroke"/></svg>`;
}
const pctAxis = v => Math.round(v * 100) + '%';
const latRefs = () => [{v: C().latency_warn_ms, cls: 'warn', l: `warn ${C().latency_warn_ms} ms`}, {v: C().latency_crit_ms, cls: 'crit', l: `crit ${C().latency_crit_ms} ms`}];
function cHostCpuMem(h, hc) {
  const s = hc ? [{n: 'Host CPU', c: 'var(--s1)', v: hc.hi.cpu.slice(hc.i0)}, {n: 'Host RAM consumed', c: 'var(--s2)', v: hc.hi.mem.slice(hc.i0)}] : [];
  return chartCard('Host CPU & RAM — history', hc, s, {max: 1, yf: pctAxis, vf: v => fpct(v, 1), refs: [{v: .9, cls: 'warn', l: '90 %'}]}, {id: 'hcm'});
}
const cDsIops = (h, hc) => chartCard('Datastore IOPS — history', hc, hc ? dsSeries(h, hc, a => a[0]) : [], {unit: 'IOPS'}, {id: 'dsiops'});
const cDsLat = (h, hc) => chartCard('Datastore latency — history', hc, hc ? dsSeries(h, hc, latOf) : [], {unit: 'ms', refs: latRefs(), vf: v => fnum(v, 1)}, {id: 'dslat', sub: 'max(read, write) per sample'});
const cDsMbs = (h, hc) => chartCard('Datastore throughput — history', hc, hc ? dsSeries(h, hc, a => a[1]) : [], {unit: 'MB/s', vf: v => fnum(v, 1)}, {id: 'dsmbs'});
const cVmIops = (h, hc) => chartCard('Top VMs by IOPS — history', hc, hc ? topSeries(hc, 'vm', a => a[0]) : [], {unit: 'IOPS'}, {id: 'vmiops', sub: 'top 8 by average in the range'});
const cVmLat = (h, hc) => chartCard('Top VMs latency — history', hc, hc ? (() => { const top = topSeries(hc, 'vm', a => a[0]).map(s => s.n); return Object.entries(hc.hi.vm).filter(([, o]) => top.includes(o.n)).map(([k, o]) => ({n: o.n, c: SER[top.indexOf(o.n) % 8], v: colv(hc, 'vm', k, a => a[2])})); })() : [], {unit: 'ms', refs: latRefs(), vf: v => fnum(v, 1)}, {id: 'vmlat', sub: 'same VMs as the IOPS chart'});
const cVmCpu = (h, hc) => chartCard('Top VMs by CPU — history', hc, hc ? topSeries(hc, 'vm', a => a[3]) : [], {unit: 'MHz', vf: v => fint(v)}, {id: 'vmcpu', sub: 'top 8 by average MHz'});
const cDevLat = (h, hc) => chartCard('Device GAVG — history', hc, hc ? topSeries(hc, 'dev', a => a[0]) : [], {unit: 'ms', refs: latRefs(), vf: v => fnum(v, 1)}, {id: 'devlat', sub: 'total latency seen by VMs'});
const cDevKq = (h, hc) => chartCard('Device KAVG + QAVG — history', hc, hc ? topSeries(hc, 'dev', a => (isN(a[2]) || isN(a[3])) ? (a[2] || 0) + (a[3] || 0) : null) : [], {unit: 'ms', refs: [{v: C().kq_warn_ms, cls: 'warn', l: `warn ${C().kq_warn_ms} ms`}, {v: C().kq_crit_ms, cls: 'crit', l: `crit ${C().kq_crit_ms} ms`}], vf: v => fnum(v, 1)}, {id: 'devkq', sub: 'ESXi kernel + queue (host side)'});
const cNic = (h, hc) => chartCard('vmnic throughput — history', hc, hc ? topSeries(hc, 'nic', a => (a[0] || 0) + (a[1] || 0)) : [], {unit: 'MB/s', vf: v => fnum(v, 1)}, {id: 'nic', sub: 'RX + TX'});

function vHistory(h) {
  const hc = hctx(h);
  if (!hc) return card('History', '<div class="empty">No history yet. The last hour is loaded from ESXi right after start, then every sample (20 s) is added.</div>');
  const hi = hc.hi, all = hi.t.length;
  let x = `<div class="warnbox" style="color:var(--mute);background:var(--panel);border-color:var(--line)">History: <b style="color:var(--text)">${all}</b> samples × ${D.interval} s · ` +
    `${tm(new Date(hi.t[0] * 1000).toISOString()).slice(5, 16).replace('-', '.')} → ${hhmm(hi.t[all - 1])} (${fnum((hi.t[all - 1] - hi.t[0]) / 3600, 1)} h) · ` +
    `kept in memory for ${C().history_hours || 6} h · showing <b style="color:var(--text)">${hc.n}</b> samples &nbsp; ${rangeChips()}</div>`;
  x += `<div class="grid g2">${cHostCpuMem(h, hc)}${cDsLat(h, hc)}</div>`;
  x += `<div class="grid g2">${cDsIops(h, hc)}${cDsMbs(h, hc)}</div>`;
  x += `<div class="grid g2">${cVmIops(h, hc)}${cVmLat(h, hc)}</div>`;
  x += `<div class="grid g2">${cVmCpu(h, hc)}${cNic(h, hc)}</div>`;
  x += `<div class="grid g2">${cDevLat(h, hc)}${cDevKq(h, hc)}</div>`;
  const dsr = Object.entries(hi.ds).map(([u, o]) => {
    const io = colv(hc, 'ds', u, a => a[0]), mb = colv(hc, 'ds', u, a => a[1]), lt = colv(hc, 'ds', u, latOf);
    return {name: o.n, ai: avgOf(io), pi: peakOf(io), am: avgOf(mb), pm: peakOf(mb), al: wavg(lt, io), pl: peakOf(lt), io, lt};
  });
  const dc = [
    {k: 'name', l: 'Datastore', a: 'l', f: d => `<b>${esc(d.name)}</b>`},
    {k: 'ai', l: 'Avg IOPS', f: d => fint(d.ai)}, {k: 'pi', l: 'Peak IOPS', f: d => fint(d.pi)},
    {k: 'am', l: 'Avg MB/s', f: d => fnum(d.am)}, {k: 'pm', l: 'Peak MB/s', f: d => fnum(d.pm)},
    {k: 'al', l: 'Avg lat ms', t: 'IOPS-weighted', f: d => lat(d.al)}, {k: 'pl', l: 'Peak lat ms', f: d => lat(d.pl)},
    {k: 'tio', l: 'IOPS trend', v: d => d.ai, f: d => spark(d.io)}, {k: 'tlt', l: 'Latency trend', v: d => d.pl, f: d => spark(d.lt, latC(d.pl))},
  ];
  x += card('Datastores over the selected range', table('hds', dc, dsr, {sort: {k: 'ai', d: -1}}), {np: true, filter: 'hds', right: rangeChips()});
  const vr = Object.entries(hi.vm).map(([k, o]) => {
    const io = colv(hc, 'vm', k, a => a[0]), mb = colv(hc, 'vm', k, a => a[1]), lt = colv(hc, 'vm', k, a => a[2]), cp = colv(hc, 'vm', k, a => a[3]);
    return {name: o.n, ai: avgOf(io), pi: peakOf(io), am: avgOf(mb), al: wavg(lt, io), pl: peakOf(lt), ac: avgOf(cp), pc: peakOf(cp), io, cp};
  }).filter(v => isN(v.ai) || isN(v.ac));
  const vc = [
    {k: 'name', l: 'VM', a: 'l', f: v => `<b>${esc(v.name)}</b>`},
    {k: 'ai', l: 'Avg IOPS', f: v => fint(v.ai)}, {k: 'pi', l: 'Peak IOPS', f: v => fint(v.pi)}, {k: 'am', l: 'Avg MB/s', f: v => fnum(v.am)},
    {k: 'al', l: 'Avg lat ms', f: v => lat(v.al)}, {k: 'pl', l: 'Peak lat ms', f: v => lat(v.pl)},
    {k: 'ac', l: 'Avg CPU MHz', f: v => fint(v.ac)}, {k: 'pc', l: 'Peak CPU MHz', f: v => fint(v.pc)},
    {k: 'tio', l: 'IOPS trend', v: v => v.ai, f: v => spark(v.io)}, {k: 'tcp', l: 'CPU trend', v: v => v.ac, f: v => spark(v.cp, 'cpu')},
  ];
  x += card('VMs over the selected range', table('hvm', vc, vr, {sort: {k: 'ai', d: -1}, limit: 25}), {np: true, filter: 'hvm', right: rangeChips()});
  return x;
}

/* ---------------- views: single host ---------------- */
function vOverview(h) {
  const r = h.res, hc = r.hostcpu || {}, hm = r.hostmem || {}, ds = dsRows(h);
  const totI = sum(ds, d => d.iops), totM = sum(ds, d => d.mbs), totR = sum(ds, d => d.r_iops), totW = sum(ds, d => d.w_iops);
  let worst = null; for (const d of ds) if (isN(d.lmax) && (!worst || d.lmax > worst.lmax)) worst = d;
  const all = Object.values(h.inv.vms || {}).filter(v => !v.template), on = all.filter(v => v.power === 'poweredOn').length;
  const fc = fcount(h.findings);
  const fcls = fc.crit ? 'crit' : fc.warn ? 'warn' : 'ok';
  let x = '<div class="kpis">';
  x += kpiRing('Host CPU', hc.pct, pC(hc.pct, .75, .9), `${fint(hc.used_mhz)} / ${fint(hc.total_mhz)} MHz`, `peak ${fpct(hc.peak_pct)}`);
  x += kpiRing('Host RAM', hm.pct, pC(hm.pct, .85, .95), `${fnum(hm.used_gb)} / ${fnum(hm.total_gb)} GB`, `state ${esc(hm.state || '–')}`);
  x += kpi('Datastore IOPS', fint(totI), `R ${fint(totR)} · W ${fint(totW)}`);
  x += kpi('Throughput', `${fnum(totM)}<small>MB/s</small>`, `${ds.length} datastores`);
  x += kpi('Worst latency', worst ? `${span(latC(worst.lmax), fnum(worst.lmax))}<small>ms</small>` : '–', worst ? esc(worst.name) : '', worst ? latC(worst.lmax) : '');
  x += kpi('VMs running', `${on}<small>/ ${all.length}</small>`, `${Object.keys(r.vm || {}).length} with storage IO`);
  x += kpi('Findings', `${span('crit', fc.crit)} <small>crit</small> ${span('warn', fc.warn)} <small>warn</small>`, `${fc.info} info`, fcls);
  x += '</div>';
  x += '<div class="grid g2">';
  x += card('Findings', findingsHtml(h.findings, 9), {np: true, right: sevChips(h.findings)});
  x += card('Datastore latency — current window', latChart(ds), {sub: `avg over ${Math.round(D.window_s / 60 * 10) / 10} min`});
  const hx = hctx(h);
  x += '</div><div class="grid g2">' + cDsIops(h, hx) + cDsLat(h, hx) + '</div><div class="grid g2">';
  const m = sortMetric() === 'mbs' ? 'mbs' : 'iops';
  const vr = vmRows(h).sort((a, b) => (b[sortMetric()] || 0) - (a[sortMetric()] || 0)).slice(0, 8);
  x += card('Top VMs — storage IO', hbars(vr.map(v => ({label: v.name,
    segs: m === 'mbs' ? [{v: v.r_mbs || 0, c: 'var(--s1)'}, {v: v.w_mbs || 0, c: 'var(--s2)'}] : [{v: v.r_iops || 0, c: 'var(--s1)'}, {v: v.w_iops || 0, c: 'var(--s2)'}],
    text: m === 'mbs' ? `${fnum(v.mbs)} MB/s` : `${fint(v.iops)} IOPS`, sub: m === 'mbs' ? `${fint(v.iops)} IOPS` : `${fnum(v.mbs)} MB/s`,
    tip: `<b>${esc(v.name)}</b>Read ${fint(v.r_iops)} IOPS / ${fnum(v.r_mbs)} MB/s<br>Write ${fint(v.w_iops)} IOPS / ${fnum(v.w_mbs)} MB/s<br>Latency R ${fnum(v.r_lat)} / W ${fnum(v.w_lat)} ms`
  })), [['read', 'var(--s1)'], ['write', 'var(--s2)']]), {sub: `sorted by ${esc(C().sort_by)}`, right: `<span class="chip" data-goto="vms">all VMs →</span>`});
  const cr = Object.values(r.vmres || {}).sort((a, b) => (b.cpu_mhz || 0) - (a.cpu_mhz || 0)).slice(0, 8);
  x += card('Top VMs — CPU', hbars(cr.map(v => ({label: v.name, segs: [{v: v.cpu_mhz || 0, c: 'var(--s3)'}],
    text: `${fint(v.cpu_mhz)} MHz`, sub: `${fpct(v.cpu_pct)} of ${v.vcpu} vCPU · ready ${fpct(v.ready_pct, 1)}`,
    tip: `<b>${esc(v.name)}</b>CPU ${fpct(v.cpu_pct)} of ${v.vcpu} vCPU<br>Ready ${fpct(v.ready_pct, 1)} / vCPU · Co-Stop ${fpct(v.costop_pct, 1)}`}))),
    {right: `<span class="chip" data-goto="cpumem">CPU & memory →</span>`});
  x += '</div><div class="grid g2">';
  const cap = ds.slice().sort((a, b) => (a.freePct ?? 1) - (b.freePct ?? 1));
  x += card('Datastore capacity', cap.map(d => {
    const used = d.freePct == null ? null : 1 - d.freePct, cls = d.freePct == null ? 'dim' : d.freePct < .1 ? 'crit' : d.freePct < .2 ? 'warn' : 'ok';
    return `<div class="hb hoverable" ${tipAttr(`<b>${esc(d.name)}</b>${fint(d.capacity / 1073741824)} GB total, ${fint(d.free / 1073741824)} GB free`)}><div class="n">${esc(d.name)} ${badge(d.type)}</div>` +
      `<div class="tr"><span style="width:${(clamp(used) * 100).toFixed(1)}%;background:var(--${cls === 'dim' ? 'dim' : cls})"></span></div>` +
      `<div class="v">${fpct(used)}<small>${fint(d.free / 1073741824)} GB free</small></div></div>`;
  }).join('') || '<div class="empty">No datastores</div>');
  const nics = Object.entries(r.nic || {}).filter(([, n]) => n.speed || n.rx || n.tx);
  x += card('Network (vmnics)', nics.map(([n, v]) => {
    const cls = pC(v.util, .6, C().nic_util_warn_pct / 100);
    return `<div class="hb hoverable" ${tipAttr(`<b>${esc(n)}</b>${v.speed ? v.speed + ' Mb/s' : 'link down'}<br>RX ${fnum(v.rx)} / TX ${fnum(v.tx)} MB/s<br>drops ${fint(v.drops)} · errors ${fint(v.errors)}`)}>` +
      `<div class="n">${esc(n)} <span class="sub2">${v.speed ? (v.speed >= 1000 ? v.speed / 1000 + ' Gb' : v.speed + ' Mb') : 'down'}</span></div>` +
      `<div class="tr"><span style="width:${(clamp(v.util) * 100).toFixed(1)}%;background:var(--${cls === 'dim' ? 'dim' : cls})"></span></div>` +
      `<div class="v">${fpct(v.util)}<small>↓${fnum(v.rx)} ↑${fnum(v.tx)} MB/s</small></div></div>`;
  }).join('') || '<div class="empty">No NIC data</div>');
  x += '</div>';
  return x;
}

function vDatastores(h) {
  const cols = [
    {k: 'name', l: 'Datastore', a: 'l', f: d => `<b>${esc(d.name)}</b>${!d.accessible ? ' ' + badge('INACCESSIBLE', 'b-crit') : ''}${(d.shared_with || []).length ? ` <span class="badge" title="also mounted on ${esc(d.shared_with.join(', '))}">shared ×${d.shared_with.length + 1}</span>` : ''}`},
    {k: 'type', l: 'Type', a: 'l', f: d => badge(d.type)},
    {k: 'capacity', l: 'Size GB', f: d => fint(d.capacity / 1073741824)},
    {k: 'freePct', l: 'Free', f: d => bar(d.freePct, d.freePct == null ? 'dim' : d.freePct < .1 ? 'crit' : d.freePct < .2 ? 'warn' : 'ok', `${fpct(d.freePct)} · ${fint(d.free / 1073741824)} GB`)},
    {k: 'r_iops', l: 'R IOPS', f: d => fint(d.r_iops)}, {k: 'w_iops', l: 'W IOPS', f: d => fint(d.w_iops)},
    {k: 'r_mbs', l: 'R MB/s', f: d => fnum(d.r_mbs)}, {k: 'w_mbs', l: 'W MB/s', f: d => fnum(d.w_mbs)},
    {k: 'r_lat', l: 'R ms', f: d => lat(d.r_lat)}, {k: 'w_lat', l: 'W ms', f: d => lat(d.w_lat)}, {k: 'max_lat', l: 'Peak ms', f: d => lat(d.max_lat)},
    {k: 'vmShare', l: 'By VMs', t: 'share of datastore IOPS generated by running VMs on this host', f: d => span(d.vmShare != null && d.vmShare < .5 ? 'warn' : 'mute', fpct(d.vmShare))},
    {k: 'trend', l: 'IOPS trend', t: 'history in the selected range', v: d => d.iops, f: d => hc ? spark(colv(hc, 'ds', d.uuid, a => a[0])) : span('dim', '–')},
  ];
  const hc = hctx(h);
  const ds = dsRows(h);
  return card('Datastores', table('ds', cols, ds, {sort: {k: 'iops', d: -1}}), {np: true, filter: 'ds', sub: `${ds.length} datastores`,
    note: '"By VMs" below 50 % = a big part of the IO comes from outside the running VMs (backup, Storage vMotion, UNMAP, another host on the same LUN).'}) +
    `<div class="grid g2">${cDsLat(h, hc)}${cDsIops(h, hc)}</div>` + `<div class="grid g2">${cDsMbs(h, hc)}${card('Latency vs. thresholds — current window', latChart(ds))}</div>`;
}

function vDevices(h) {
  const cols = [
    {k: 'display', l: 'Device', a: 'l', s: d => d.display + ' ' + d.name, f: d => `<div>${esc(d.display)}</div>${d.display !== d.name ? `<div class="mono c-dim">${esc(d.name)}</div>` : ''}`},
    {k: 'type', l: 'Type', a: 'l', f: d => badge(d.type)},
    {k: 'model', l: 'Model', a: 'l', v: d => `${d.vendor} ${d.model}`.trim(), f: d => esc(`${d.vendor} ${d.model}`.trim() || '–')},
    {k: 'dsn', l: 'Datastore', a: 'l', v: d => (d.datastores || []).join(', '), f: d => esc((d.datastores || []).join(', ') || '–')},
    {k: 'paths', l: 'Paths', a: 'l', v: d => d.paths ? d.paths.total : null, f: d => {
      const p = d.paths; if (!p) return span('dim', '–'); if (p.active == null) return `?/${p.total}`;
      const cls = p.dead ? 'crit' : (d.type === 'iSCSI' && p.total === 1) ? 'warn' : 'ok';
      return span(cls, `${p.active}/${p.total}${p.dead ? ` · ${p.dead} dead` : ''}`);
    }},
    {k: 'capacity_gb', l: 'Size GB', f: d => fint(d.capacity_gb)},
    {k: 'r_iops', l: 'R IOPS', f: d => fint(d.r_iops)}, {k: 'w_iops', l: 'W IOPS', f: d => fint(d.w_iops)}, {k: 'mbs', l: 'MB/s', f: d => fnum(d.mbs)},
    {k: 'davg', l: 'DAVG', t: 'device / array latency', f: d => lat(d.davg)}, {k: 'kavg', l: 'KAVG', t: 'ESXi kernel latency', f: d => kq(d.kavg)},
    {k: 'qavg', l: 'QAVG', t: 'time in ESXi queue', f: d => kq(d.qavg)}, {k: 'gavg', l: 'GAVG', t: 'total latency seen by the VMs', f: d => lat(d.gavg)},
    {k: 'peak', l: 'Peak', f: d => lat(d.peak)},
    {k: 'ab', l: 'Aborts', v: d => (d.aborts || 0) + (d.resets || 0), f: d => { const a = (d.aborts || 0) + (d.resets || 0); return a ? `<span class="badge b-crit">${fint(a)}</span>` : span('dim', '0'); }},
  ];
  return card('Storage devices (LUNs / disks)', table('dev', cols, devRows(h), {sort: {k: 'gavg', d: -1}}), {np: true, filter: 'dev',
    note: 'DAVG = device / array latency (Synology, SSD) · KAVG = ESXi kernel · QAVG = waiting in the ESXi queue · GAVG = total latency seen by VMs (ms). KAVG / QAVG warn ≥ ' + C().kq_warn_ms + ' ms, crit ≥ ' + C().kq_crit_ms + ' ms.'}) +
    `<div class="grid g2">${cDevLat(h, hctx(h))}${cDevKq(h, hctx(h))}</div>`;
}

function vNetwork(h) {
  const rows = Object.entries(h.res.nic || {}).filter(([, n]) => n.speed || n.rx || n.tx).map(([n, v]) => Object.assign({name: n, driver: (h.inv.nics[n] || {}).driver}, v));
  const cols = [
    {k: 'name', l: 'vmnic', a: 'l', f: n => `<b>${esc(n.name)}</b>`}, {k: 'driver', l: 'Driver', a: 'l', f: n => esc(n.driver || '–')},
    {k: 'speed', l: 'Link', f: n => n.speed ? `${n.speed} Mb/s` : span('crit', 'down')},
    {k: 'rx', l: 'RX MB/s', f: n => fnum(n.rx)}, {k: 'tx', l: 'TX MB/s', f: n => fnum(n.tx)},
    {k: 'util', l: 'Utilisation', f: n => bar(n.util, pC(n.util, .6, C().nic_util_warn_pct / 100), fpct(n.util))},
    {k: 'drops', l: 'Drops', f: n => n.drops ? span('warn', fint(n.drops)) : span('dim', '0')},
    {k: 'errors', l: 'Errors', f: n => n.errors ? span('crit', fint(n.errors)) : span('dim', '0')},
  ];
  return card('Network — vmnics (iSCSI / NFS traffic path)', table('nic', cols, rows, {sort: {k: 'util', d: -1}}), {np: true, filter: 'nic',
    note: `Utilisation = max(RX, TX) of the link speed; warning at ${C().nic_util_warn_pct} %.`}) + cNic(h, hctx(h));
}

function vCpuMem(h) {
  const r = h.res, hc = r.hostcpu || {}, hm = r.hostmem || {}, hw = h.inv.hw || {};
  const bad = v => (v || 0) > 0.001 ? 'crit' : 'dim';
  let x = '<div class="kpis">';
  x += kpiRing('Host CPU', hc.pct, pC(hc.pct, .75, .9), `${fint(hc.used_mhz)} / ${fint(hc.total_mhz)} MHz`, `peak ${fpct(hc.peak_pct)}`);
  x += kpiRing('Host RAM consumed', hm.pct, pC(hm.pct, .85, .95), `${fnum(hm.used_gb)} / ${fnum(hm.total_gb)} GB`, `active ${fnum(hm.active_gb)} GB`);
  const st = hm.state;
  x += kpi('Memory state', span(['high', 'clear', undefined, null].includes(st) ? 'ok' : st === 'soft' ? 'warn' : 'crit', esc(st || '–')), 'high / clear = no reclamation', ['high', 'clear', undefined, null].includes(st) ? 'ok' : st === 'soft' ? 'warn' : 'crit');
  x += kpi('Reclaim', `${span(bad(hm.balloon_gb), fnum(hm.balloon_gb, 2))}<small>GB balloon</small>`, `compressed ${fnum(hm.compressed_gb, 2)} GB · swapped ${fnum(hm.swapused_gb, 2)} GB`, (hm.balloon_gb || hm.compressed_gb || hm.swapused_gb) > 0.001 ? 'warn' : 'ok');
  x += kpi('Host swap in / out', `${span(bad((hm.swapin_kbs || 0) + (hm.swapout_kbs || 0)), fint(hm.swapin_kbs) + ' / ' + fint(hm.swapout_kbs))}<small>KB/s</small>`, 'any swapping = slow VMs + extra IO', (hm.swapin_kbs || hm.swapout_kbs) ? 'crit' : 'ok');
  x += '</div>';
  x += `<div class="grid g2">${cHostCpuMem(h, hctx(h))}${cVmCpu(h, hctx(h))}</div>`;
  x += card('Hardware', `<dl class="kv"><dt>CPU model</dt><dd>${esc(hw.cpu_model || '–')}</dd><dt>Sockets / cores / threads</dt><dd>${hw.sockets ?? '–'} / ${hw.cores ?? '–'} / ${hw.threads ?? '–'}</dd>` +
    `<dt>Clock</dt><dd>${fint(hw.mhz)} MHz per core · ${fint(hc.total_mhz)} MHz total</dd><dt>Memory</dt><dd>${fnum((hw.mem_bytes || 0) / 1073741824)} GB</dd><dt>Product</dt><dd>${esc(h.inv.product)}</dd></dl>`);
  const vms = Object.values(r.vmres || {});
  const cc = [
    {k: 'name', l: 'VM', a: 'l', f: v => `<b>${esc(v.name)}</b>`}, {k: 'vcpu', l: 'vCPU'},
    {k: 'cpu_pct', l: 'CPU', f: v => bar(v.cpu_pct, pC(v.cpu_pct, .75, .9), fpct(v.cpu_pct))},
    {k: 'cpu_mhz', l: 'MHz', f: v => fint(v.cpu_mhz)},
    {k: 'ready_pct', l: 'Ready / vCPU', t: '>5 % contention, >10 % serious', f: v => span(pC(v.ready_pct, .05, .1), fpct(v.ready_pct, 1))},
    {k: 'costop_pct', l: 'Co-Stop', t: 'high = too many vCPUs for the VM', f: v => span(pC(v.costop_pct, .03, .1), fpct(v.costop_pct, 1))},
  ];
  const gb = v => (v || 0) >= 0.01 ? span('crit', fnum(v, 2)) : span('dim', fnum(v, 2));
  const mc = [
    {k: 'name', l: 'VM', a: 'l', f: v => `<b>${esc(v.name)}</b>`}, {k: 'mem_gb', l: 'Config GB', f: v => fnum(v.mem_gb)},
    {k: 'consumed_gb', l: 'Consumed', f: v => fnum(v.consumed_gb)}, {k: 'active_gb', l: 'Active', f: v => fnum(v.active_gb)},
    {k: 'act', l: 'Active %', v: v => isN(v.active_gb) && v.mem_gb ? v.active_gb / v.mem_gb : null, f: v => { const a = isN(v.active_gb) && v.mem_gb ? v.active_gb / v.mem_gb : null; return bar(a, pC(a, .8, .95), fpct(a)); }},
    {k: 'balloon_gb', l: 'Balloon', f: v => gb(v.balloon_gb)}, {k: 'compressed_gb', l: 'Compr.', f: v => gb(v.compressed_gb)},
    {k: 'swapped_gb', l: 'Swapped', f: v => gb(v.swapped_gb)}, {k: 'swapin_kbs', l: 'Swap-in KB/s', f: v => (v.swapin_kbs || 0) > 0 ? span('crit', fint(v.swapin_kbs)) : span('dim', fint(v.swapin_kbs))},
  ];
  x += '<div class="grid g2">';
  x += card('VMs by CPU', table('vcpu', cc, vms, {sort: {k: 'cpu_mhz', d: -1}, limit: C().top_n * 2}), {np: true, filter: 'vcpu', note: 'Ready = VM waited for a physical CPU · Co-Stop = vCPUs held back to stay in sync.'});
  x += card('VMs by memory', table('vmem', mc, vms, {sort: {k: 'consumed_gb', d: -1}, limit: C().top_n * 2}), {np: true, filter: 'vmem', note: 'Balloon / compressed / swapped > 0 = host is short of RAM (hypervisor swap also creates disk load).'});
  return x + '</div>';
}

function vVms(h) {
  const hc = hctx(h);
  const rows = vmRows(h), mx = Math.max(...rows.map(v => v.iops || 0), 1);
  const cols = [
    {k: 'name', l: 'VM', a: 'l', f: v => `<b>${esc(v.name)}</b>`},
    {k: 'iops', l: 'IOPS', f: v => bar((v.iops || 0) / mx, '', fint(v.iops))},
    {k: 'r_iops', l: 'R IOPS', f: v => fint(v.r_iops)}, {k: 'w_iops', l: 'W IOPS', f: v => fint(v.w_iops)},
    {k: 'r_mbs', l: 'R MB/s', f: v => fnum(v.r_mbs)}, {k: 'w_mbs', l: 'W MB/s', f: v => fnum(v.w_mbs)},
    {k: 'r_lat', l: 'R ms', f: v => lat(v.r_lat)}, {k: 'w_lat', l: 'W ms', f: v => lat(v.w_lat)}, {k: 'max_lat', l: 'Peak ms', f: v => lat(v.max_lat)},
    {k: 'snapshots', l: 'Snaps', v: v => v.consolidate ? 999 : v.snapshots, f: snapCell},
    {k: 'trend', l: 'IOPS trend', t: 'history in the selected range', v: v => v.iops, f: v => hc ? spark(colv(hc, 'vm', v.moid, a => a[0])) : span('dim', '–')},
    {k: 'ds', l: 'Datastores', a: 'l', v: v => v.dsNames.join(', '), f: v => esc(v.dsNames.join(', ') || '–')},
  ];
  return card('VM storage IO — all datastores', table('vms', cols, rows, {sort: {k: sortMetric(), d: -1}}), {np: true, filter: 'vms', sub: `${rows.length} VMs with IO · current window`}) +
    `<div class="grid g2">${cVmIops(h, hc)}${cVmLat(h, hc)}</div>`;
}

function vConsumers(h) {
  const ds = dsRows(h).filter(d => (d.iops || 0) >= 1 || (d.mbs || 0) >= 0.1 || d.cons.length).sort((a, b) => (b.iops || 0) - (a.iops || 0));
  if (!ds.length) return card('Who loads which datastore', '<div class="empty">No measurable IO on any datastore</div>');
  const vmName = m => (h.res.vm[m] || {}).name || m;
  return ds.map((d, di) => {
    const cons = d.cons.slice().sort((a, b) => (b[1][sortMetric()] || 0) - (a[1][sortMetric()] || 0));
    const tot = d.iops || 0;
    const vmI = sum(cons, c => c[1].iops), rest = Math.max(tot - vmI, 0);
    const top = cons.slice(0, 7), other = cons.slice(7);
    const seg = [];
    if (tot >= 1) {
      top.forEach(([m, b], i) => seg.push({n: vmName(m), v: b.iops || 0, c: SER[i]}));
      if (other.length) seg.push({n: `${other.length} other VMs`, v: sum(other, c => c[1].iops), c: '#4a5878'});
      if (rest > Math.max(5, .05 * tot)) seg.push({n: 'not from VMs (host / backup / other host)', v: rest, c: 'repeating-linear-gradient(45deg,#c87dff 0 4px,#7d4fa8 4px 8px)'});
    }
    const stot = sum(seg, s => s.v) || 1;
    const stack = seg.length ? `<div class="stack">${seg.map(s => `<span style="width:${(s.v / stot * 100).toFixed(2)}%;background:${s.c}" ${tipAttr(`<b>${esc(s.n)}</b>${fint(s.v)} IOPS · ${fpct(s.v / stot)}`)}></span>`).join('')}</div>` +
      `<div class="legend" style="margin-bottom:12px">${seg.map(s => `<span><i style="background:${s.c}"></i>${esc(s.n)} ${fpct(s.v / stot)}</span>`).join('')}</div>` : '';
    const rows = cons.map(([m, b]) => Object.assign({}, b, {name: vmName(m), shI: tot >= 1 ? Math.min(b.iops / tot, 1) : null, shM: (d.mbs || 0) >= .1 ? Math.min(b.mbs / d.mbs, 1) : null,
      snapshots: (h.res.vm[m] || {}).snapshots, consolidate: (h.res.vm[m] || {}).consolidate}));
    if (rest > Math.max(5, .05 * tot)) rows.push({name: '‹not from VMs: host / backup / other›', iops: rest, r_iops: null, shI: tot ? rest / tot : null, special: true});
    const hot = x => Math.max(x.shI || 0, x.shM || 0);
    const cols = [
      {k: 'name', l: 'VM', a: 'l', f: x => x.special ? span('mag', esc(x.name)) : `<span class="c-${hot(x) >= .5 ? 'crit' : hot(x) >= .25 ? 'warn' : 'mute'}" style="color:${hot(x) < .25 ? 'var(--text)' : ''}">${esc(x.name)}</span>`},
      {k: 'r_iops', l: 'R IOPS', f: x => fint(x.r_iops)}, {k: 'w_iops', l: 'W IOPS', f: x => fint(x.w_iops)},
      {k: 'r_mbs', l: 'R MB/s', f: x => fnum(x.r_mbs)}, {k: 'w_mbs', l: 'W MB/s', f: x => fnum(x.w_mbs)},
      {k: 'r_lat', l: 'R ms', f: x => lat(x.r_lat)}, {k: 'w_lat', l: 'W ms', f: x => lat(x.w_lat)},
      {k: 'shI', l: 'IOPS share', f: x => bar(x.shI, x.special ? 'dim' : pC(x.shI, .25, .5), fpct(x.shI))},
      {k: 'shM', l: 'MB/s share', f: x => bar(x.shM, x.special ? 'dim' : pC(x.shM, .25, .5), fpct(x.shM))},
      {k: 'snapshots', l: 'Snaps', v: x => x.consolidate ? 999 : x.snapshots, f: x => x.special ? '' : snapCell(x)},
    ];
    const id = 'cons' + di;
    return card(`${esc(d.name)} ${badge(d.type)}`, stack + `<div style="margin:0 -16px -14px">${table(id, cols, rows, {sort: {k: 'shI', d: -1}, limit: C().top_n})}</div>`,
      {sub: `${fint(d.iops)} IOPS · ${fnum(d.mbs)} MB/s · latency ${span(latC(d.r_lat), 'R ' + fnum(d.r_lat))} / ${span(latC(d.w_lat), 'W ' + fnum(d.w_lat))} ms`, filter: id});
  }).join('');
}

function vVdisks(h) {
  const cols = [
    {k: 'vm', l: 'VM', a: 'l', f: d => `<b>${esc(d.vm)}</b>`}, {k: 'inst', l: 'Disk', a: 'l', f: d => `<span class="mono">${esc(d.inst)}</span>`},
    {k: 'ds', l: 'Datastore', a: 'l'},
    {k: 'vmdk', l: 'VMDK', a: 'l', v: d => (d.file || '').split(']').pop().trim().split('/').pop(), f: d => `<span class="mono c-mute" title="${esc(d.file)}">${esc((d.file || '').split(']').pop().trim().split('/').pop() || '–')}</span>`},
    {k: 'iops', l: 'IOPS', f: d => fint(d.iops)}, {k: 'r_iops', l: 'R IOPS', f: d => fint(d.r_iops)}, {k: 'w_iops', l: 'W IOPS', f: d => fint(d.w_iops)},
    {k: 'r_mbs', l: 'R MB/s', f: d => fnum(d.r_mbs)}, {k: 'w_mbs', l: 'W MB/s', f: d => fnum(d.w_mbs)},
    {k: 'r_lat', l: 'R ms', f: d => lat(d.r_lat)}, {k: 'w_lat', l: 'W ms', f: d => lat(d.w_lat)}, {k: 'max_lat', l: 'Peak', f: d => lat(d.max_lat)},
  ];
  return card('Virtual disks', table('vd', cols, h.res.vdisks || [], {sort: {k: sortMetric(), d: -1}, limit: 50}), {np: true, filter: 'vd', sub: `${(h.res.vdisks || []).length} disks`});
}

function vGuests(h) {
  const rows = Object.values(h.inv.vms || {}).filter(v => v.power === 'poweredOn' && !v.template).map(v => {
    const g = v.guest || {}; let ff = null, fp = '–';
    for (const [p, cap, free] of g.fs || []) if (cap && (ff == null || free / cap < ff)) { ff = free / cap; fp = p; }
    return {name: v.name, tools: (g.tools || '').replace('guestTools', '') || '–', ver: (g.tools_version || '').replace('guestTools', ''), os: g.os, hostname: g.hostname,
      ips: (g.ips || []).join(', '), fp, ff, fs: g.fs || [], vcpu: v.vcpu, mem: v.mem_mb / 1024};
  });
  const cols = [
    {k: 'name', l: 'VM', a: 'l', f: v => `<b>${esc(v.name)}</b>`},
    {k: 'tools', l: 'Tools', a: 'l', f: v => `${span(v.tools === 'Running' ? 'ok' : 'warn', esc(v.tools))}${v.ver && v.ver !== 'Current' ? ` <span class="sub2">${esc(v.ver)}</span>` : ''}`},
    {k: 'os', l: 'Guest OS', a: 'l', f: v => esc(v.os || '–')}, {k: 'hostname', l: 'Hostname', a: 'l', f: v => esc(v.hostname || '–')},
    {k: 'ips', l: 'IPv4 (all NICs)', a: 'l', f: v => `<span class="mono">${esc(v.ips || '–')}</span>`},
    {k: 'vcpu', l: 'vCPU'}, {k: 'mem', l: 'RAM GB', f: v => fnum(v.mem)},
    {k: 'fp', l: 'Fullest FS', a: 'l', f: v => `<span class="mono" ${tipAttr('<b>Filesystems</b>' + (v.fs.map(([p, c, f]) => `${esc(p)}: ${fpct(c ? f / c : null)} free of ${fnum(c / 1073741824)} GB`).join('<br>') || 'none reported'))}>${esc(v.fp)}</span>`},
    {k: 'ff', l: 'Free', f: v => bar(v.ff, v.ff == null ? 'dim' : v.ff < .1 ? 'crit' : v.ff < .2 ? 'warn' : 'ok', fpct(v.ff))},
  ];
  return card('Guest info (VMware Tools)', table('gi', cols, rows, {sort: {k: 'name', d: 1}}), {np: true, filter: 'gi', sub: `${rows.length} running VMs`});
}

function guestCard(g, idx) {
  if (g.error) return card(`${esc(g.name)} <span class="sub2 mono">${esc(g.ip)}</span>`, `<div class="err" style="margin:0">${esc(g.error)}</div>`);
  const mm = g.mem, swp = (g.swapin_ps || 0) + (g.swapout_ps || 0), psi = g.psi || {}, pc = g.psi_cpu || {}, pm = g.psi_mem || {};
  const st = (l, v, cls) => `<div class="gs"><small>${l}</small><b class="c-${cls || 'mute'}" style="${cls ? '' : 'color:var(--text)'}">${v}</b></div>`;
  let s = '<div class="gstats">';
  s += st('Load', `${esc((g.load || []).join(' '))} <span class="sub2">/ ${esc(g.cpus)} CPU</span>`);
  s += st('CPU busy', fpct(g.cpu_busy), pC(g.cpu_busy, .7, .9));
  s += st('user / sys', `${fpct(g.cpu_user)} / ${fpct(g.cpu_sys)}`);
  s += st('iowait', fpct(g.iowait), pC(g.iowait, .1, .3));
  s += st('steal', fpct(g.steal), (g.steal || 0) >= .05 ? 'warn' : 'ok');
  if (mm) {
    s += st('RAM available', `${fnum(mm.avail_gb)} / ${fnum(mm.total_gb)} GB · ${fpct(mm.avail_pct)}`, mm.avail_pct < .05 ? 'crit' : mm.avail_pct < .15 ? 'warn' : 'ok');
    s += st('Swap', `${fnum(mm.swap_used_gb, 2)} GB · ${fint(g.swapin_ps)}/${fint(g.swapout_ps)} pg/s`, swp >= 100 ? 'crit' : swp > 0 ? 'warn' : '');
    s += st('Dirty / writeback', `${fnum(mm.dirty_mb, 0)} / ${fnum(mm.writeback_mb, 0)} MB`);
  }
  if ('some' in psi) s += st('PSI io some / full', `${fnum(psi.some)} / ${fnum(psi.full || 0)} %`, pC((psi.some || 0) / 100, .1, .3));
  if ('some' in pc || 'some' in pm) s += st('PSI cpu / mem', `${fnum(pc.some || 0)} / ${fnum(pm.some || 0)} %`);
  s += st('Real disk IO', `${fnum(g.total_mbs, 2)} MB/s`);
  s += '</div>';
  const notes = [];
  if (g.note) notes.push(esc(g.note));
  else if (!['root', ''].includes(g.remote_user || '') && !g.sudo) notes.push(`logged in as '${esc(g.remote_user)}' without sudo — only its own processes are visible`);
  const ipi = g.ip_info || {};
  if ((ipi.tried || []).length > 1) notes.push(`IPv4 tested: ${esc(ipi.tried.join(', '))} → used ${esc(g.ip)}${(ipi.unreachable || []).length ? ` (unreachable: ${esc(ipi.unreachable.join(', '))})` : ''}`);
  if (notes.length) s += `<div class="warnbox" style="margin:12px 16px 0">${notes.join('<br>')}</div>`;
  const disks = (g.disks || []).filter(d => d.r_iops + d.w_iops >= 0.1 || d.util >= 0.01);
  const dc = [
    {k: 'name', l: 'Guest disk', a: 'l', f: d => `<span class="mono">${esc(d.name)}</span>`},
    {k: 'r_iops', l: 'r/s', f: d => fnum(d.r_iops)}, {k: 'w_iops', l: 'w/s', f: d => fnum(d.w_iops)},
    {k: 'r_mbs', l: 'rMB/s', f: d => fnum(d.r_mbs, 2)}, {k: 'w_mbs', l: 'wMB/s', f: d => fnum(d.w_mbs, 2)},
    {k: 'r_await', l: 'r_await', f: d => lat(d.r_await)}, {k: 'w_await', l: 'w_await', f: d => lat(d.w_await)},
    {k: 'util', l: 'Util', f: d => bar(d.util, pC(d.util, .6, .9), fpct(d.util))}, {k: 'inflight', l: 'InFl'},
  ];
  const tot = g.total_mbs || 0;
  const procs = (g.procs || []).map(p => Object.assign({share: tot > 0 ? (p.r_mbs + p.w_mbs) / tot : null}, p));
  const pcols = [
    {k: 'pid', l: 'PID', v: p => +p.pid}, {k: 'comm', l: 'Process', a: 'l', f: p => span((p.share || 0) >= .5 && tot >= 1 ? 'crit' : 'mute', `<span style="${(p.share || 0) >= .5 && tot >= 1 ? '' : 'color:var(--text)'}">${esc(p.comm)}</span>`)},
    {k: 'r_mbs', l: 'Disk R MB/s', f: p => fnum(p.r_mbs, 2)}, {k: 'w_mbs', l: 'Disk W MB/s', f: p => fnum(p.w_mbs, 2)},
    {k: 'share', l: 'Share', f: p => bar(p.share, pC(p.share, .25, .5), fpct(p.share))},
    {k: 'rc_mbs', l: 'Logical R', t: 'incl. page cache (rchar)', f: p => fnum(p.rc_mbs, 2)}, {k: 'wc_mbs', l: 'Logical W', t: 'incl. page cache (wchar)', f: p => fnum(p.wc_mbs, 2)},
  ];
  const cpu = (g.cpu_procs || []).filter(p => p.cpu >= .005);
  const ccols = [{k: 'pid', l: 'PID', v: p => +p.pid}, {k: 'comm', l: 'Process', a: 'l'}, {k: 'cpu', l: 'CPU', t: '100 % = one full core', f: p => span(pC(p.cpu, .5, .9), (p.cpu * 100).toFixed(0) + '%')}, {k: 'rss_mb', l: 'RSS GB', f: p => fnum(p.rss_mb / 1024, 2)}];
  const mcols = [{k: 'pid', l: 'PID', v: p => +p.pid}, {k: 'comm', l: 'Process', a: 'l'}, {k: 'rss_mb', l: 'RSS GB', f: p => fnum(p.rss_mb / 1024, 2)},
    {k: 'sh', l: 'of RAM', v: p => mm ? p.rss_mb / (mm.total_gb * 1024) : null, f: p => fpct(mm ? p.rss_mb / (mm.total_gb * 1024) : null)}];
  const id = 'g' + idx;
  let body = s;
  body += `<div class="subh">Block devices</div>` + table(id + 'd', dc, disks, {sort: {k: 'util', d: -1}, empty: 'no active disks'});
  body += `<div class="subh">Processes by disk IO</div>` + table(id + 'p', pcols, procs, {sort: {k: 'share', d: -1}, limit: C().top_n, empty: 'no process IO in the sample'});
  body += `<div class="cols2"><div><div class="subh">Top CPU</div>${table(id + 'c', ccols, cpu, {sort: {k: 'cpu', d: -1}, limit: 8, empty: 'idle'})}</div>` +
    `<div><div class="subh">Top RAM</div>${table(id + 'm', mcols, g.mem_procs || [], {sort: {k: 'rss_mb', d: -1}, limit: 8})}</div></div>`;
  const iwc = pC(g.iowait, .1, .3);
  return card(`${esc(g.name)} <span class="sub2">${esc(g.hostname || '')} · <span class="mono">${esc(g.ip)}</span>${g.os_name ? ' · ' + esc(g.os_name) : ''}</span>`, body,
    {np: true, right: `<span class="badge b-${iwc === 'dim' ? 'ok' : iwc}">iowait ${fpct(g.iowait)}</span>`});
}

function vInguest(h) {
  const r = h.res;
  if (r.guests == null) return card('In-guest analysis', `<div class="empty">In-guest SSH analysis did not run (disabled in config, <kbd>--no-guest</kbd>, or live mode without <i>guest_in_live</i>).</div>`);
  let x = '';
  const sk = r.guest_skipped || [];
  if (sk.length) x += card('Skipped VMs', sk.map(([n, w]) => `<div class="f f-warn"><span class="tag">SKIP</span><div class="msg"><b>${esc(n)}</b> — ${esc(w)}</div></div>`).join(''), {np: true});
  const gs = Object.values(r.guests || {}).sort((a, b) => (b.total_mbs || 0) - (a.total_mbs || 0));
  if (!gs.length && !sk.length) x += card('In-guest analysis', '<div class="empty">No Linux VM selected — nothing to analyze</div>');
  x += gs.map((g, i) => guestCard(g, i)).join('');
  return x + `<div class="sub2" style="padding:0 4px">Disk R/W = real block IO (read_bytes / write_bytes) · Logical = incl. page cache (rchar / wchar) · process CPU 100 % = one full core. Read-only: only /proc and /sys are read.</div>`;
}

function vMissing(label) {
  const rows = (D.missing || []).filter(m => !label || m.host === label);
  const cols = [
    ...(label || D.hosts.length < 2 ? [] : [{k: 'host', l: 'Host', a: 'l'}]),
    {k: 'vm', l: 'VM', a: 'l', f: m => `<b>${esc(m.vm)}</b>`},
    {k: 'problem', l: 'Problem', a: 'l', f: m => span(/NOT possible/.test(m.impact) || /NOT installed/.test(m.problem) ? 'crit' : 'warn', esc(m.problem))},
    {k: 'impact', l: 'Impact', a: 'l', cls: () => 'wrapc', f: m => esc(m.impact)},
    {k: 'hint', l: 'Install (run manually)', a: 'l', f: m => `<span class="mono c-info">${esc(m.hint)}</span>`},
  ];
  return card('Missing tools — install manually', table('miss' + (label ? 'h' : ''), cols, rows, {sort: {k: 'vm', d: 1}, empty: '✓ Nothing missing on the analyzed VMs'}), {np: true, filter: 'miss' + (label ? 'h' : ''),
    note: 'The script never installs anything — run the commands yourself (as root / with sudo).'});
}

function vError(h) {
  return `<div class="card"><div class="ch"><h3>${esc(h.label)}</h3><span class="s">${esc(h.user)}@${esc(h.address)}</span></div><div class="err">${esc(h.error)}</div>` +
    `<div class="note">Fix credentials with <kbd>--reconfigure</kbd> (menu: e N = edit host N). In live mode failed hosts are reconnected every ~5 minutes.</div></div>`;
}

/* ---------------- views: all hosts ---------------- */
function hostTotals(h) {
  const ds = dsRows(h); let worst = null;
  for (const d of ds) if (isN(d.lmax) && (!worst || d.lmax > worst.lmax)) worst = d;
  return {iops: sum(ds, d => d.iops), mbs: sum(ds, d => d.mbs), worst, fc: fcount(h.findings),
    on: Object.values(h.inv.vms || {}).filter(v => v.power === 'poweredOn').length, all: Object.keys(h.inv.vms || {}).length};
}
function vSummary() {
  let x = '<div class="tiles">';
  D.hosts.forEach((h, i) => {
    const st = hostStatus(h);
    if (h.error) { x += `<div class="tile" data-host="${i}"><h4><span class="dot down"></span>${esc(h.label)}</h4><div class="ad">${esc(h.address)}</div><div class="err" style="margin:0">${esc(h.error)}</div></div>`; return; }
    const t = hostTotals(h), hc = h.res.hostcpu || {}, hm = h.res.hostmem || {};
    x += `<div class="tile" data-host="${i}"><h4><span class="dot ${st}"></span>${esc(h.label)}<span style="margin-left:auto">${t.fc.crit ? badge(t.fc.crit + ' crit', 'b-crit') : ''} ${t.fc.warn ? badge(t.fc.warn + ' warn', 'b-warn') : ''} ${!t.fc.crit && !t.fc.warn ? badge('OK', 'b-ok') : ''}</span></h4>` +
      `<div class="ad">${esc(h.address)} · ${esc((h.inv.product || '').replace('VMware ', ''))}</div><div class="tm">` +
      `<div><small>CPU</small><b class="c-${pC(hc.pct, .75, .9)}">${fpct(hc.pct)}</b></div><div><small>RAM</small><b class="c-${pC(hm.pct, .85, .95)}">${fpct(hm.pct)}</b></div>` +
      `<div><small>IOPS</small><b>${fint(t.iops)}</b></div><div><small>MB/s</small><b>${fnum(t.mbs)}</b></div></div>` +
      `<div class="ws">VMs ${t.on}/${t.all} running · worst datastore ${t.worst ? `${esc(t.worst.name)} ${span(latC(t.worst.lmax), fnum(t.worst.lmax) + ' ms')}` : '–'}</div></div>`;
  });
  x += '</div>';
  const ok = D.hosts.filter(h => !h.error);
  const tI = sum(ok, h => hostTotals(h).iops), tM = sum(ok, h => hostTotals(h).mbs);
  const fAll = ok.flatMap(h => h.findings || []), fc = fcount(fAll);
  return `<div class="kpis">${kpi('Hosts', `${ok.length}<small>/ ${D.hosts.length} reachable</small>`, '', ok.length < D.hosts.length ? 'crit' : 'ok')}` +
    kpi('Total IOPS', fint(tI), 'all datastores, all hosts') + kpi('Total throughput', `${fnum(tM)}<small>MB/s</small>`) +
    kpi('Shared datastores', (D.cross.shared || []).length, 'mounted on more hosts') +
    kpi('Findings', `${span('crit', fc.crit)} <small>crit</small> ${span('warn', fc.warn)} <small>warn</small>`, '', fc.crit ? 'crit' : fc.warn ? 'warn' : 'ok') + '</div>' + x +
    ((D.cross.findings || []).length ? card('Cross-host findings', findingsHtml(D.cross.findings), {np: true}) : '');
}
function vCross() {
  const sh = (D.cross.shared || []).slice().sort((a, b) => b.tot_i - a.tot_i);
  let x = (D.cross.findings || []).length ? card('Cross-host findings', findingsHtml(D.cross.findings), {np: true}) : '';
  if (!sh.length) return x + card('Cross-host', '<div class="empty">No datastore is mounted on more than one configured host</div>');
  sh.forEach((s, si) => {
    const hidx = n => Math.max(0, D.hosts.findIndex(x => x.label === n));
    const seg = s.per_host.slice().sort((a, b) => b.iops - a.iops).map(r => ({n: r.host, v: r.iops || 0, c: SER[hidx(r.host) % 8]}));
    const tot = sum(seg, q => q.v) || 1;
    const stack = `<div class="stack">${seg.map(q => `<span style="width:${(q.v / tot * 100).toFixed(2)}%;background:${q.c}" ${tipAttr(`<b>${esc(q.n)}</b>${fint(q.v)} IOPS · ${fpct(q.v / tot)}`)}></span>`).join('')}</div>` +
      `<div class="legend" style="margin-bottom:12px">${seg.map(q => `<span><i style="background:${q.c}"></i>${esc(q.n)} ${fpct(q.v / tot)}</span>`).join('')}</div>`;
    const hc = [{k: 'host', l: 'Host', a: 'l', f: r => `<b>${esc(r.host)}</b>`}, {k: 'r_iops', l: 'R IOPS', f: r => fint(r.r_iops)}, {k: 'w_iops', l: 'W IOPS', f: r => fint(r.w_iops)},
      {k: 'r_mbs', l: 'R MB/s', f: r => fnum(r.r_mbs)}, {k: 'w_mbs', l: 'W MB/s', f: r => fnum(r.w_mbs)}, {k: 'r_lat', l: 'R ms', f: r => lat(r.r_lat)}, {k: 'w_lat', l: 'W ms', f: r => lat(r.w_lat)},
      {k: 'max_lat', l: 'Peak', f: r => lat(r.max_lat)}, {k: 'sh', l: 'IOPS share', v: r => s.tot_i >= 1 ? r.iops / s.tot_i : null, f: r => bar(s.tot_i >= 1 ? r.iops / s.tot_i : null, '', fpct(s.tot_i >= 1 ? r.iops / s.tot_i : null))}];
    const vc = [{k: 'vm', l: 'VM', a: 'l', f: v => `<b>${esc(v.vm)}</b>`}, {k: 'host', l: 'Host', a: 'l'}, {k: 'iops', l: 'IOPS', f: v => fint(v.iops)}, {k: 'mbs', l: 'MB/s', f: v => fnum(v.mbs)},
      {k: 'lat', l: 'Lat ms', f: v => lat(v.lat)},
      {k: 'shI', l: 'IOPS %', v: v => s.tot_i >= 1 ? v.iops / s.tot_i : null, f: v => { const q = s.tot_i >= 1 ? v.iops / s.tot_i : null; return bar(q, pC(q, .25, .5), fpct(q)); }},
      {k: 'shM', l: 'MB/s %', v: v => s.tot_m >= .1 ? v.mbs / s.tot_m : null, f: v => { const q = s.tot_m >= .1 ? v.mbs / s.tot_m : null; return bar(q, pC(q, .25, .5), fpct(q)); }}];
    x += card(`${esc(s.name)} ${badge(s.type)}`, stack + `<div style="margin:0 -16px">${table('xh' + si, hc, s.per_host, {sort: {k: 'iops', d: -1}})}</div>` +
      `<div class="subh" style="padding-left:0">Top VMs over all hosts</div><div style="margin:0 -16px -14px">${table('xv' + si, vc, s.vms, {sort: {k: 'shI', d: -1}, limit: C().top_n})}</div>`,
      {sub: `total all hosts ${fint(s.tot_i)} IOPS · ${fnum(s.tot_m)} MB/s`});
  });
  return x;
}
function vAllFindings() {
  const all = [];
  for (const h of D.hosts) for (const f of (h.error ? [['crit', `Host unreachable: ${h.error}`]] : h.findings || [])) all.push(f);
  let x = card('Filter', sevChips(all));
  D.hosts.forEach((h, i) => {
    const F = h.error ? [['crit', `Host unreachable: ${h.error}`]] : (h.findings || []);
    x += card(`<span class="dot ${hostStatus(h)}" style="display:inline-block;margin-right:8px"></span>${esc(h.label)}`, findingsHtml(F), {np: true, right: `<span class="chip" data-host="${i}">open host →</span>`});
  });
  if ((D.cross.findings || []).length) x += card('Cross-host', findingsHtml(D.cross.findings), {np: true});
  return x;
}

/* ---------------- navigation / layout ---------------- */
const HOST_VIEWS = [['overview', 'Overview'], ['history', 'History'], ['findings', 'Findings'], ['datastores', 'Datastores'], ['devices', 'Devices / LUNs'], ['network', 'Network'],
  ['cpumem', 'CPU & Memory'], ['vms', 'VM storage IO'], ['consumers', 'Who loads what'], ['vdisks', 'Virtual disks'], ['guests', 'Guest info'], ['inguest', 'In-guest (SSH)'], ['missing', 'Missing tools']];
const ALL_VIEWS = [['summary', 'All hosts'], ['cross', 'Cross-host'], ['allfindings', 'All findings'], ['missing', 'Missing tools']];
const HV = {overview: vOverview, datastores: vDatastores, devices: vDevices, network: vNetwork, cpumem: vCpuMem, vms: vVms, consumers: vConsumers, vdisks: vVdisks,
  guests: vGuests, inguest: vInguest, history: vHistory, missing: h => vMissing(D.hosts.length > 1 ? h.label : null),
  findings: h => card('Findings', findingsHtml(h.findings), {np: true, right: sevChips(h.findings)})};
const AV = {summary: vSummary, cross: vCross, allfindings: vAllFindings, missing: () => vMissing(null)};
const AV_ICON = {summary: 'summary', cross: 'cross', allfindings: 'findings', missing: 'missing'};

function fixState() {
  const multi = D.hosts.length > 1;
  if (S.host === null || (S.host !== 'all' && !D.hosts[S.host]) || (S.host === 'all' && !multi)) S.host = multi ? 'all' : 0;
  if (S.host === 'all') { if (!AV[S.view]) S.view = 'summary'; }
  else if (!HV[S.view]) S.view = 'overview';
}
function navItem(view, label, icon, host, extra) {
  const on = S.view === view && String(S.host) === String(host);
  return `<div class="ni${on ? ' on' : ''}" data-view="${view}" data-h="${host}">${ico(icon)}<span class="nm">${label}</span>${extra || ''}</div>`;
}
function renderSide() {
  const multi = D.hosts.length > 1;
  let x = '';
  if (multi) {
    x += '<div class="side-sec">Cluster</div><div class="nav">';
    const fc = fcount(D.hosts.flatMap(h => h.findings || []));
    for (const [v, l] of ALL_VIEWS) {
      let extra = '';
      if (v === 'allfindings' && (fc.crit || fc.warn)) extra = `<span class="cnt ${fc.crit ? 'crit' : 'warn'}">${fc.crit || fc.warn}</span>`;
      if (v === 'cross') extra = `<span class="cnt">${(D.cross.shared || []).length}</span>`;
      if (v === 'missing' && (D.missing || []).length) extra = `<span class="cnt warn">${D.missing.length}</span>`;
      x += navItem(v, l, AV_ICON[v], 'all', extra);
    }
    x += '</div>';
  }
  x += `<div class="side-sec">${multi ? 'Hosts' : 'Host'}</div><div class="nav">`;
  D.hosts.forEach((h, i) => {
    const st = hostStatus(h), on = S.host === i;
    x += `<div class="ni${on && multi ? ' on' : ''}" data-host="${i}" style="${on ? 'color:var(--text)' : ''}"><span class="dot ${st}"></span><span class="nm">${esc(h.label)}</span>` +
      (h.error ? '<span class="cnt crit">ERR</span>' : (() => { const c = fcount(h.findings); return c.crit ? `<span class="cnt crit">${c.crit}</span>` : c.warn ? `<span class="cnt warn">${c.warn}</span>` : ''; })()) + '</div>';
  });
  x += '</div>';
  if (S.host !== 'all') {
    const h = H();
    x += `<div class="side-sec">${esc(h.label)}</div><div class="nav">`;
    if (h.error) x += navItem('overview', 'Connection error', 'overview', S.host);
    else for (const [v, l] of HOST_VIEWS) {
      if (v === 'vdisks' && C().show_vm_disks === false) continue;
      let extra = '';
      if (v === 'findings') { const c = fcount(h.findings); if (c.crit || c.warn) extra = `<span class="cnt ${c.crit ? 'crit' : 'warn'}">${c.crit || c.warn}</span>`; }
      if (v === 'inguest' && h.res.guests) extra = `<span class="cnt">${Object.keys(h.res.guests).length}</span>`;
      if (v === 'missing') { const n = (D.missing || []).filter(m => m.host === h.label).length; if (n) extra = `<span class="cnt warn">${n}</span>`; }
      if (v === 'history') { const hi = HIS(h); if (hi && hi.t) extra = `<span class="cnt">${fnum((hi.t.length * D.interval) / 3600, 1)} h</span>`; }
      x += navItem(v, l, v, S.host, extra);
    }
    x += '</div>';
  }
  $('#side').innerHTML = x;
  $('#brandsub').textContent = `${D.hosts.length} host${D.hosts.length > 1 ? 's' : ''} · ${D.mode}`;
  const live = API ? S.live : D.mode === 'live';
  $('#foot').innerHTML = `<div class="meta">${S.busy && S.msg ? `<span class="busymsg">${esc(S.msg)}</span>` : `Updated <b style="color:var(--mute)">${tm(D.generated).slice(11)}</b> · ${D.mode} · window ${Math.round(D.window_s / 60 * 10) / 10} min`}<br>` +
    (API ? `<kbd>L</kbd> live · <kbd>F5</kbd> refresh · <kbd>Ctrl+S</kbd> save · <kbd>F11</kbd> · <kbd>Ctrl+Q</kbd>` : `static export from ${tm(D.exported_at || D.generated)}`) + `</div>` +
    `<div class="tog${API ? '' : ' dis'}" data-act="live" title="L"><span class="sw${live ? ' on' : ''}"><i></i></span><div><b>Live mode ${live ? 'ON' : 'OFF'}</b><small>${live ? `refresh every ${D.interval} s · click to stop` : 'click to start live refresh'}</small></div></div>` +
    `<button class="btn pri" data-act="refresh" ${(!API || live || S.busy) ? 'disabled' : ''} title="${live ? 'live mode refreshes automatically' : 'collect a new snapshot (F5)'}">${ico('refresh', S.busy ? 'spin' : '')}${S.busy ? 'Working…' : 'Refresh'}</button>` +
    `<button class="btn" data-act="save" ${API ? '' : 'disabled'} title="Save report – JSON (all data + history for analysis) or HTML (Ctrl+S)">${ico('save')}Save</button>` +
    `<button class="btn" data-act="fs" ${API ? '' : 'disabled'}>${ico('fs')}${S.fs ? 'Window' : 'Fullscreen'}</button>` +
    `<button class="btn danger" data-act="exit" ${API ? '' : 'disabled'}>${ico('exit')}Exit</button>`;
}
function header() {
  const isLive = API ? S.live : D.mode === 'live', da = API ? ' data-act="live" title="click = live on/off (L)"' : '';
  const live = isLive ? `<span class="pill live"${da}><i></i>LIVE</span>` : `<span class="pill"${da}>snapshot · live off</span>`;
  if (S.host === 'all') {
    const t = Object.fromEntries(ALL_VIEWS)[S.view];
    return `<div class="top"><div><h2>${t}</h2><div class="subt">${live}<span>${D.hosts.length} ESXi hosts</span><span class="sep">·</span><span>averages over the last ${Math.round(D.window_s / 60 * 10) / 10} min</span></div></div>` +
      `<div class="rt">generated ${tm(D.generated)}</div></div>`;
  }
  const h = H(), t = Object.fromEntries(HOST_VIEWS)[S.view];
  if (h.error) return `<div class="top"><div><h2>${esc(h.label)}</h2><div class="subt">${live}<span class="c-crit">not connected</span></div></div></div>`;
  return `<div class="top"><div><h2>${S.view === 'overview' ? esc(h.label) : t}</h2><div class="subt">${live}` +
    `${S.view !== 'overview' ? `<b>${esc(h.label)}</b><span class="sep">·</span>` : ''}<span>${esc(h.inv.host_name)}</span><span class="sep">·</span><span>${esc(h.inv.product)}</span>` +
    `<span class="sep">·</span><span>${h.res.samples} × ${D.interval} s samples</span></div></div><div class="rt">measured ${tm(h.res.time)}</div></div>`;
}
function render(keep) {
  fixState();
  for (const k in T) delete T[k];
  for (const k in CH) delete CH[k];
  renderSide();
  const m = $('#main'), st = m.scrollTop;
  let body;
  try {
    body = S.host === 'all' ? AV[S.view]() : (H().error ? vError(H()) : HV[S.view](H()));
  } catch (e) { body = `<div class="err">Render error: ${esc(e && e.stack || e)}</div>`; console.error(e); }
  $('#wrap').innerHTML = header() + body;
  drawCharts();
  if (keep) m.scrollTop = st; else m.scrollTop = 0;
}

/* ---------------- events ---------------- */
document.addEventListener('click', e => {
  const th = e.target.closest('th[data-k]');
  if (th) {
    const id = th.dataset.t, k = th.dataset.k, cur = S.sort[id] || T[id].o.sort;
    const col = T[id].cols.find(c => c.k === k);
    const sample = T[id].rows.map(r => cellVal(col, r)).find(v => v != null);
    S.sort[id] = cur && cur.k === k ? {k, d: -cur.d} : {k, d: typeof sample === 'number' ? -1 : 1};
    const el = document.getElementById('t-' + id); el.innerHTML = tableInner(id); return;
  }
  const more = e.target.closest('[data-more]');
  if (more) { const id = more.dataset.more; S.expand[id] = !S.expand[id]; rerenderTable(id); return; }
  const rg = e.target.closest('[data-range]');
  if (rg) { S.range = +rg.dataset.range; render(true); return; }
  const sev = e.target.closest('[data-sev]');
  if (sev) { S.sev[sev.dataset.sev] = !S.sev[sev.dataset.sev]; render(true); return; }
  const go = e.target.closest('[data-goto]');
  if (go) { S.view = go.dataset.goto; render(); return; }
  const nv = e.target.closest('[data-view]');
  if (nv) { S.view = nv.dataset.view; S.host = nv.dataset.h === 'all' ? 'all' : +nv.dataset.h; render(); return; }
  const hs = e.target.closest('[data-host]');
  if (hs) { const i = +hs.dataset.host; if (S.host !== i) { S.host = i; if (!HV[S.view]) S.view = 'overview'; } else S.view = 'overview'; render(); return; }
  const a = e.target.closest('[data-act]');
  if (a && !a.disabled && !a.classList.contains('dis')) act(a.dataset.act);
});
document.addEventListener('input', e => {
  const f = e.target.closest('[data-filter]');
  if (f) { S.filter[f.dataset.filter] = f.value; rerenderTable(f.dataset.filter); }
});
document.addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (e.key === 'F11') { e.preventDefault(); act('fs'); }
  else if (e.ctrlKey && k === 'q') { e.preventDefault(); act('exit'); }
  else if (e.key === 'F5' || (e.ctrlKey && k === 'r')) { e.preventDefault(); act('refresh'); }
  else if (e.key === 'Escape') { if (document.activeElement && document.activeElement.dataset && document.activeElement.dataset.filter) { document.activeElement.value = ''; S.filter[document.activeElement.dataset.filter] = ''; rerenderTable(document.activeElement.dataset.filter); } else if (S.fs) act('fs'); }
  else if (e.ctrlKey && k === 's') { e.preventDefault(); act('save'); }
  else if (k === 'l' && !e.ctrlKey && !e.altKey && !(document.activeElement && document.activeElement.tagName === 'INPUT')) { act('live'); }
  else if (e.ctrlKey && k === 'f') { const s = document.querySelector('.search'); if (s) { e.preventDefault(); s.focus(); } }
});
function toast(msg, err) {
  const t = document.createElement('div'); t.className = 'toast' + (err ? ' err' : ''); t.textContent = msg;
  $('#toast').appendChild(t); setTimeout(() => t.remove(), err ? 9000 : 3500);
}
async function act(a) {
  if (!API || S.closing) return;
  try {
    if (a === 'refresh') { if (S.live || S.busy) return; if (await API.refresh()) { S.busy = true; S.msg = 'Collecting performance data …'; setBusy(true); renderSide(); toast('Collecting a new snapshot…'); } }
    else if (a === 'live') {
      S.live = await API.set_live(!S.live);
      toast(S.live ? `Live mode ON – refresh every ${D.interval} s` : 'Live mode OFF – collecting a full-window snapshot…');
      render(true);
    }
    else if (a === 'save') {
      const r = await API.save_report();
      if (r && r.ok) toast(`Report saved: ${r.path} (${fnum(r.size / 1048576, 1)} MB)`);
      else if (r && !r.cancelled) toast('Save failed: ' + (r.error || '?'), true);
    }
    else if (a === 'fs') { S.fs = await API.toggle_fullscreen(); renderSide(); }
    else if (a === 'exit') {
      S.closing = true; clearInterval(POLL);
      document.body.style.opacity = '.6';
      API.close().catch(() => {});
    }
  } catch (e) { if (!S.closing) toast(String(e), true); }
}
let lastMsg = '';
function setBusy(b) { document.getElementById('busy').style.display = b ? 'block' : 'none'; }
async function poll() {
  if (S.closing) return;
  try {
    const s = await API.get_state();
    if (S.closing) return;
    if (s.live !== S.live) { S.live = s.live; render(true); }
    if (s.busy !== S.busy || (s.busy && s.msg !== S.msg)) { S.busy = s.busy; S.msg = s.msg; setBusy(s.busy); renderSide(); }
    if (s.msg && s.msg !== lastMsg && /failed/i.test(s.msg)) toast(s.msg, true);
    lastMsg = s.msg;
    if (s.v !== D.version) {
      D = JSON.parse(await API.get_data());
      const active = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.filter : null;
      render(true);
      if (active) { const el = document.querySelector(`[data-filter="${active}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
      $('#wrap').classList.remove('fade'); void $('#wrap').offsetWidth; $('#wrap').classList.add('fade');
    }
  } catch (e) { /* window closing */ }
}
function bindApi() {
  if (API || !window.pywebview || !window.pywebview.api || !window.pywebview.api.get_state) return;
  API = window.pywebview.api;
  API.get_state().then(s => { S.fs = s.fs; S.live = s.live; render(true); }).catch(() => {});
  POLL = setInterval(poll, 1500);
  renderSide();
}
window.addEventListener('pywebviewready', bindApi);
render();
setTimeout(bindApi, 300);
</script>
</body>
</html>
"""


def clear_screen():
    print("\033[2J\033[H", end="")


# ---------------------------------------------------------------- multi host
def connect_host(h):
    """Connect one ESXi host. Returns context dict (with 'error' on failure)."""
    ctx = {"cfg": h, "label": host_label(h), "error": None}
    try:
        si = connect(h)
        host = get_host(si, h)
        ctx.update(si=si, host=host, inv=collect_inventory(si, host), perf=Perf(si))
    except vim.fault.InvalidLogin:
        ctx["error"] = f"invalid username or password for {h['user']}@{h['host']}"
    except vim.fault.NoPermission as e:
        ctx["error"] = f"no permission: {e.msg}"
    except vmodl.MethodFault as e:
        ctx["error"] = f"vSphere API error: {e.msg}"
    except Exception as e:
        ctx["error"] = f"connection failed: {type(e).__name__}: {e}"
    return ctx


def connect_all(hosts):
    p_info(f"Connecting to {len(hosts)} ESXi host(s): {', '.join(host_label(h) for h in hosts)} ...")
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
        ctxs = list(ex.map(connect_host, hosts))
    for c in ctxs:
        if c["error"]:
            p_err(f"{c['label']}: {c['error']}")
        else:
            inv = c["inv"]
            p_ok(f"{c['label']}: {inv['product']} - {len(inv['datastores'])} datastores, "
                 f"{len(inv['devices'])} devices, {len(inv['vms'])} VMs")
    mark_shared(ctxs)
    return ctxs


def mark_shared(ctxs):
    """Mark datastores (same VMFS/NFS UUID) mounted on more than one configured host."""
    seen = defaultdict(list)
    for c in ctxs:
        if not c["error"]:
            for uuid in c["inv"]["datastores"]:
                seen[uuid].append(c["label"])
    for c in ctxs:
        if not c["error"]:
            for uuid, ds in c["inv"]["datastores"].items():
                ds["shared_with"] = [x for x in seen[uuid] if x != c["label"]]


def analyze_all(ctxs, cfg, samples, with_guests, guest_sample_s):
    ok = [c for c in ctxs if not c["error"]]

    def one(c):
        try:
            c["res"] = analyze(c["inv"], c["perf"], c["host"], samples)
            c["res_error"] = None
        except Exception as e:
            c["res"], c["res_error"] = None, f"{type(e).__name__}: {getattr(e, 'msg', None) or e}"
    if ok:
        with ThreadPoolExecutor(max_workers=min(8, len(ok))) as ex:
            list(ex.map(one, ok))
    good = [c for c in ok if c.get("res")]
    for c in good:
        HISTORY.merge(c["label"], c["res"].pop("series", None), history_max_points(cfg))
    if with_guests and good and not SHUTDOWN.is_set():
        run_guest_analysis(good, cfg, guest_sample_s)
    for c in good:
        c["findings"] = build_findings(c["inv"], c["res"], cfg)
    return good


def render_summary(ctxs, cfg):
    section("ALL HOSTS SUMMARY")
    rows = []
    for c in ctxs:
        if c.get("error") or not c.get("res"):
            rows.append([(c["label"], Fore.RED), ("ERROR", Fore.RED + Style.BRIGHT), "", "", "", "", "", "", "",
                         (trunc(c.get("error") or c.get("res_error") or "?", 70), Fore.RED)])
            continue
        inv, res = c["inv"], c["res"]
        worst_lat, worst_ds = None, "-"
        for uuid, r in res["ds"].items():
            lat = max([x for x in (r["r_lat"], r["w_lat"]) if x is not None], default=None)
            if lat is not None and (worst_lat is None or lat > worst_lat):
                worst_lat, worst_ds = lat, inv["datastores"][uuid]["name"]
        nc = sum(1 for s, _ in c["findings"] if s == "crit")
        nw = sum(1 for s, _ in c["findings"] if s == "warn")
        running = sum(1 for v in inv["vms"].values() if v["power"] == "poweredOn")
        tot_iops = sum(r["iops"] for r in res["ds"].values())
        tot_mbs = sum(r["mbs"] for r in res["ds"].values())
        status = ("CRIT", Fore.RED + Style.BRIGHT) if nc else ("WARN", Fore.YELLOW) if nw else ("OK", Fore.GREEN)
        cpu = res.get("cpu")
        hmem = res.get("hostmem") or {}
        rows.append([c["label"], status, trunc(inv["product"].replace("VMware ", ""), 30),
                     pct_cell(cpu / 100 if cpu is not None else None, 0.75, 0.90),
                     pct_cell(hmem.get("pct"), 0.85, 0.95), f"{running}/{len(inv['vms'])}",
                     f_int(tot_iops), f_num(tot_mbs),
                     (f"{trunc(worst_ds, 20)} {f_num(worst_lat)} ms", lat_color(worst_lat, cfg)),
                     (f"{nc} crit / {nw} warn", status[1])])
    hdr = ["Host", "Status", "Version", "CPU", "RAM", "VMs on", "IOPS", "MB/s", "Worst datastore", "Findings"]
    print_table(hdr, rows, ["l", "l", "l", "r", "r", "r", "r", "r", "l", "l"])


def cross_host(ctxs, cfg):
    """Shared datastores: combine load of all hosts. Returns (shared dict, findings)."""
    shared = defaultdict(list)
    for c in ctxs:
        for uuid, ds in c["inv"]["datastores"].items():
            shared[uuid].append(c)
    shared = {u: l for u, l in shared.items() if len(l) > 1}
    findings = []
    out = {}
    for uuid, cl in shared.items():
        per_host = [(c, c["res"]["ds"][uuid]) for c in cl]
        tot_i = sum(r["iops"] for _, r in per_host)
        tot_m = sum(r["mbs"] for _, r in per_host)
        vms = []
        for c in cl:
            for moid, b in c["res"]["consumers"].get(uuid, []):
                vms.append((c, c["res"]["vm"][moid]["name"], b))
        vms.sort(key=lambda x: x[2]["iops"] + x[2]["mbs"] * 10, reverse=True)
        out[uuid] = {"name": cl[0]["inv"]["datastores"][uuid]["name"], "type": cl[0]["inv"]["datastores"][uuid]["type"],
                     "per_host": per_host, "vms": vms, "tot_i": tot_i, "tot_m": tot_m}
        slow = [(c, r) for c, r in per_host
                if max([x for x in (r["r_lat"], r["w_lat"]) if x is not None], default=0) >= cfg["latency_warn_ms"]]
        if slow and (tot_i >= 1 or tot_m >= 0.1):
            lvl = "crit" if any(max(r["r_lat"] or 0, r["w_lat"] or 0) >= cfg["latency_crit_ms"] for _, r in slow) \
                else "warn"
            name = out[uuid]["name"]
            findings.append((lvl, f"Shared datastore '{name}': slow on {', '.join(c['label'] for c, _ in slow)}; "
                                  f"total load from all hosts {f_int(tot_i)} IOPS, {f_num(tot_m)} MB/s."))
            hs = ", ".join(f"{c['label']} {r['iops'] / tot_i:.0%}" for c, r in
                           sorted(per_host, key=lambda x: -x[1]["iops"])) if tot_i >= 1 else ""
            if hs:
                findings.append((lvl, f"   -> IOPS share per host: {hs}"))
            if vms:
                c, vname, b = vms[0]
                sh = max(b["iops"] / tot_i if tot_i >= 1 else 0, b["mbs"] / tot_m if tot_m >= 0.1 else 0)
                other = [s for s, _ in slow if s is not c]
                txt = f"   -> biggest consumer over ALL hosts: VM '{vname}' on {c['label']} ({min(sh, 1):.0%})"
                if other:
                    txt += f" - it slows down {', '.join(o['label'] for o in other)} too"
                findings.append((lvl, txt + "."))
    return out, findings


def render_cross_host(shared, findings, cfg):
    section("CROSS-HOST: DATASTORES SHARED BY MORE HOSTS (same LUN / NFS export)")
    if not shared:
        print(col("  (no datastore is mounted on more than one configured host)", Style.DIM))
        return
    top_n = int(cfg.get("top_n", 10))
    for uuid, s in sorted(shared.items(), key=lambda kv: -kv[1]["tot_i"]):
        print()
        print("  " + col(s["name"], Style.BRIGHT + Fore.WHITE) + f" ({s['type']})  total all hosts "
              f"{f_int(s['tot_i'])} IOPS, {f_num(s['tot_m'])} MB/s")
        rows = []
        for c, r in sorted(s["per_host"], key=lambda x: -x[1]["iops"]):
            sh = r["iops"] / s["tot_i"] if s["tot_i"] >= 1 else None
            rows.append([c["label"], f_int(r["r_iops"]), f_int(r["w_iops"]), f_num(r["r_mbs"]), f_num(r["w_mbs"]),
                         lat_cell(r["r_lat"], cfg), lat_cell(r["w_lat"], cfg), lat_cell(r["max_lat"], cfg), f_pct(sh)])
        print_table(["Host", "R IOPS", "W IOPS", "R MB/s", "W MB/s", "R ms", "W ms", "Peak ms", "IOPS %"], rows,
                    indent="    ")
        rows = []
        for c, vname, b in s["vms"][:top_n]:
            si_ = b["iops"] / s["tot_i"] if s["tot_i"] >= 1 else None
            sm_ = b["mbs"] / s["tot_m"] if s["tot_m"] >= 0.1 else None
            hot = Fore.RED + Style.BRIGHT if max(si_ or 0, sm_ or 0) >= 0.5 else \
                Fore.YELLOW if max(si_ or 0, sm_ or 0) >= 0.25 else ""
            rows.append([(trunc(vname, 30), hot), c["label"], f_int(b["iops"]), f_num(b["mbs"]),
                         lat_cell(b["lat"], cfg), (f_pct(si_), hot), (f_pct(sm_), hot)])
        print_table(["VM", "Host", "IOPS", "MB/s", "Lat ms", "IOPS %", "MB/s %"], rows,
                    ["l", "l"] + ["r"] * 5, indent="    ")
    if findings:
        print()
        tags = {"crit": ("[CRIT]", Fore.RED + Style.BRIGHT), "warn": ("[WARN]", Fore.YELLOW),
                "info": ("[INFO]", Fore.CYAN), "ok": ("[ OK ]", Fore.GREEN)}
        for sev, msg in findings:
            print("  " + col(tags[sev][0], tags[sev][1]) + " " + msg)


PKG_FAMILIES = {
    "debian": ("apt install", {"getconf": "libc-bin", "base64": "coreutils", "cat": "coreutils", "id": "coreutils",
                               "sleep": "coreutils", "sh": "dash"}),
    "rhel": ("dnf install", {"getconf": "glibc-common", "base64": "coreutils", "cat": "coreutils",
                             "id": "coreutils", "sleep": "coreutils", "sh": "bash"}),
    "suse": ("zypper install", {"getconf": "glibc", "base64": "coreutils", "cat": "coreutils", "id": "coreutils",
                                "sleep": "coreutils", "sh": "bash"}),
    "alpine": ("apk add", {"getconf": "musl-utils", "base64": "coreutils", "cat": "coreutils", "id": "coreutils",
                           "sleep": "coreutils", "sh": "busybox", "hostname": "busybox"}),
    "photon": ("tdnf install", {"getconf": "glibc", "base64": "coreutils", "cat": "coreutils", "id": "coreutils",
                                "sleep": "coreutils", "sh": "bash"}),
}
TOOL_IMPACT = {
    "base64": "script sent via stdin instead (only possible with a normal login shell)",
    "getconf": "page size / clock ticks assumed (4096 / 100) - process RAM/CPU may be inaccurate",
    "hostname": "hostname read from /proc instead",
    "id": "logged-in user unknown",
    "sudo": "only processes of the SSH user are visible",
    "cat": "in-guest analysis NOT possible", "grep": "in-guest analysis NOT possible",
    "sleep": "in-guest analysis NOT possible", "sh": "in-guest analysis NOT possible",
}


def pkg_family(*texts):
    t = " ".join(x for x in texts if x).lower()
    for fam, keys in (("debian", ("debian", "ubuntu", "mint")), ("suse", ("suse", "sles")),
                      ("alpine", ("alpine",)), ("photon", ("photon",)),
                      ("rhel", ("rhel", "centos", "rocky", "alma", "fedora", "red hat", "oracle", "amazon"))):
        if any(k in t for k in keys):
            return fam
    return None


def install_hint(tools, fam):
    if not fam:
        return "install package(s) providing: " + ", ".join(tools)
    cmd, mp = PKG_FAMILIES[fam]
    return f"{cmd} " + " ".join(sorted({mp.get(t, t) for t in tools}))


def collect_missing_tools(good, cfg):
    rows = []
    want_sudo = bool(cfg.get("guest_sudo")) and cfg.get("guest_user", "root") != "root"
    for c in good:
        inv, res = c["inv"], c["res"]
        guests = res.get("guests") or {}
        for moid, v in sorted(inv["vms"].items(), key=lambda kv: kv[1]["name"].lower()):
            if v["power"] != "poweredOn" or v["template"]:
                continue
            g = v["guest"]
            linux = is_linux_guest(v)
            ver, run = g.get("tools_version", ""), g.get("tools", "")
            problem = None
            if ver == "guestToolsNotInstalled":
                problem = "VMware Tools NOT installed"
            elif run and run != "guestToolsRunning":
                problem = "VMware Tools not running"
            elif ver in ("guestToolsNeedUpgrade", "guestToolsTooOld", "guestToolsBlacklisted",
                         "guestToolsSupportedOld"):
                problem = f"VMware Tools outdated ({ver.replace('guestTools', '')})"
            if problem:
                if linux:
                    fam = pkg_family(g.get("os"))
                    hint = (install_hint(["open-vm-tools"], fam) if fam else "install package open-vm-tools") + \
                        ("; systemctl enable --now vmtoolsd" if "running" in problem else "")
                else:
                    hint = "vSphere: VM > Guest OS > Install/Upgrade VMware Tools"
                rows.append((c["label"], v["name"], problem, "no IP / guest info - no in-guest SSH analysis", hint))
            gr = guests.get(moid)
            if gr:
                miss = [t for t in (gr.get("missing_tools") or []) if t != "sudo" or want_sudo]
                if miss:
                    fam = pkg_family(gr.get("os_id"), gr.get("os_name"), g.get("os"))
                    impact = "; ".join(sorted({TOOL_IMPACT.get(t, "") for t in miss} - {""}))
                    rows.append((c["label"], v["name"], "missing: " + ", ".join(miss), impact,
                                 install_hint(miss, fam)))
    return rows


def render_missing_tools(good, cfg):
    rows = collect_missing_tools(good, cfg)
    section("MISSING TOOLS - install manually")
    if not rows:
        print("  " + col("[ OK ]", Fore.GREEN) + " nothing missing on the analyzed VMs.")
        return
    multi = len(good) > 1
    tab = []
    for host, vm, what, impact, hint in rows:
        crit = "NOT possible" in impact or "NOT installed" in what
        tab.append(([host] if multi else []) + [trunc(vm, 28), (trunc(what, 40), Fore.RED if crit else Fore.YELLOW),
                                               trunc(impact, 90), (hint, Fore.CYAN)])
    print_table((["Host"] if multi else []) + ["VM", "Problem", "Impact", "Install (run manually)"], tab,
                ["l"] * (5 if multi else 4))
    print(col("  The script never installs anything - run the commands yourself (as root / with sudo).", Style.DIM))


def render_all(ctxs, good, cfg, live=False):
    for c in good:
        render(c["inv"], c["res"], cfg, live=live, findings=c["findings"], label=c["label"])
    for c in ctxs:
        if not c.get("error") and c.get("res_error"):
            p_err(f"{c['label']}: performance query failed: {c['res_error']}")
    if len(ctxs) > 1:
        print(col("#" * 96, Fore.MAGENTA))
        render_summary(ctxs, cfg)
        shared, cf = cross_host(good, cfg)
        render_cross_host(shared, cf, cfg)
        print()
    if good:
        render_missing_tools(good, cfg)
        print()


def refresh_inventory(ctxs):
    """Re-read inventory of connected hosts, reconnect failed / expired sessions."""
    for i, c in enumerate(ctxs):
        if c["error"]:
            ctxs[i] = connect_host(c["cfg"])
            continue
        try:
            c["inv"] = collect_inventory(c["si"], c["host"])
        except vim.fault.NotAuthenticated:
            ctxs[i] = connect_host(c["cfg"])
        except Exception as e:
            c["res_error"] = str(e)
    mark_shared(ctxs)


def live_iteration(ctxs, cfg, it, gui=False):
    if it and it % 15 == 0:                    # refresh inventory every ~5 min, reconnect failed hosts
        refresh_inventory(ctxs)
    good = analyze_all(ctxs, cfg, int(cfg.get("live_samples", 1)),
                       bool(cfg.get("guest_ssh") and cfg.get("guest_in_live") and cfg.get("guest_ssh_key")),
                       min(int(cfg.get("guest_sample_s", 5)), 15))
    if SHUTDOWN.is_set():                      # dashboard closed meanwhile - no more console output
        return good
    clear_screen()
    render_all(ctxs, good, cfg, live=True)
    print(col(f"Live mode - {len(good)}/{len(ctxs)} host(s), refresh every {REALTIME_INTERVAL} s, "
              + ("close the dashboard window (Ctrl+Q) to quit" if gui else "Ctrl+C to quit"), Style.DIM))
    return good


def run_live(ctxs, cfg):
    it = 0
    try:
        while True:
            t0 = time.time()
            live_iteration(ctxs, cfg, it)
            time.sleep(max(1.0, REALTIME_INTERVAL - (time.time() - t0)))
            it += 1
    except KeyboardInterrupt:
        print()
        p_info("Live mode stopped.")


def select_hosts(hosts, wanted):
    if not wanted:
        return hosts
    want = [w.strip().lower() for x in wanted for w in x.split(",") if w.strip()]
    sel = [h for i, h in enumerate(hosts, 1)
           if host_label(h).lower() in want or h["host"].lower() in want or str(i) in want]
    missing = [w for w in want if not any(w in (host_label(h).lower(), h["host"].lower(), str(i))
                                          for i, h in enumerate(hosts, 1))]
    if missing:
        p_warn(f"Unknown host(s) ignored: {', '.join(missing)}")
    return sel


def main():
    ap = argparse.ArgumentParser(description="ESXi storage load / latency analyzer (one or more ESXi hosts)")
    ap.add_argument("--reconfigure", action="store_true", help="ask for all parameters again (local JSON)")
    ap.add_argument("--add-host", action="store_true", help="add an ESXi host to the configuration")
    ap.add_argument("--list-hosts", action="store_true", help="list configured ESXi hosts and exit")
    ap.add_argument("--hosts", action="append", metavar="NAME",
                    help="analyze only these hosts (name, address or number; comma separated or repeated)")
    ap.add_argument("--live", action="store_true", help="live mode, refresh every 20 s")
    ap.add_argument("--snapshot", action="store_true", help="one-shot report")
    ap.add_argument("--samples", type=int, help="number of 20 s samples (1-180)")
    ap.add_argument("--top", type=int, help="top N rows")
    ap.add_argument("--sort", choices=["iops", "throughput", "latency"], help="sort consumers by")
    ap.add_argument("--import-key", metavar="PATH", help="import SSH private key (OpenSSH/PEM/PuTTY .ppk) for guests")
    ap.add_argument("--no-guest", action="store_true", help="skip in-guest SSH analysis this run")
    ap.add_argument("--no-gui", action="store_true", help="do not open the fullscreen dashboard window")
    args = ap.parse_args()

    print(col("ESXi Storage Load Analyzer", Style.BRIGHT + Fore.MAGENTA))
    cfg, readonly = load_config(args)
    if args.list_hosts:
        print_hosts(cfg["hosts"])
        return 0
    if args.samples:
        cfg["samples"] = cfg["live_samples"] = max(1, min(180, args.samples))
    if args.top:
        cfg["top_n"] = args.top
    if args.sort:
        cfg["sort_by"] = args.sort
    if args.no_guest:
        cfg["guest_ssh"] = False
    mode = "live" if args.live else "snapshot" if args.snapshot else cfg.get("mode", "snapshot")
    use_gui = bool(cfg.get("gui", True)) and not args.no_gui and webview_available()

    hosts = select_hosts(cfg["hosts"], args.hosts)
    if not hosts:
        p_err("No ESXi host selected.")
        return 2
    ctxs = connect_all(hosts)
    if all(c["error"] for c in ctxs):
        if not readonly:
            p_info("Fix credentials with --reconfigure (menu: e N = edit host N).")
        return 2

    if mode == "live":
        if use_gui:
            good = live_iteration(ctxs, cfg, 0, gui=True)
            run_gui(ctxs, good, cfg, "live")
        else:
            run_live(ctxs, cfg)
        return 0
    p_info(f"Querying performance data (last {cfg['samples']} x 20 s) ...")
    good = analyze_all(ctxs, cfg, int(cfg["samples"]),
                       bool(cfg.get("guest_ssh") and cfg.get("guest_ssh_key")), int(cfg.get("guest_sample_s", 5)))
    render_all(ctxs, good, cfg)
    rc = 1 if any(c["error"] for c in ctxs) else 0
    if use_gui:
        run_gui(ctxs, good, cfg, "snapshot")
    return rc


if __name__ == "__main__":
    install_exit_handlers()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        p_warn("Interrupted.")
        sys.exit(130)
    finally:
        cleanup_temp()
