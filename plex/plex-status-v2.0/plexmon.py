#!/usr/bin/env python3
"""
plexmon — Plex server monitoring daemon (single-file build).

A long-running daemon that collects SMART, temperatures, capacity/wear history,
now-playing, activity and performance from a Plex server, and exposes it on a
localhost HTTP API that index.php reads. It never wakes a sleeping disk on a
normal run: power state is inferred from kernel I/O counters, and smartctl is
called only for disks already awake, once a day, or on an explicit request.

This is the merged single-file version of the plexmon package; behaviour is
identical. Run it with:  python3 plexmon.py           (daemon)
                         python3 plexmon.py --check   (self-check)
                         python3 plexmon.py --diag    (API + disk state)
                         python3 plexmon.py --oneshot (one collection, for testing)
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time


# ==========================================================================
# config
# ==========================================================================

# ---- paths ---------------------------------------------------------------
STATE_DIR   = Path("/var/lib/plex-status")     # cache + history live here (root)
WEB_DIR     = Path("/var/www/html/smart")      # what the page reads
RUN_DIR     = Path("/run/plex-status")         # runtime: socket-less API token

DATA_FILE    = WEB_DIR / "data.json"           # the page's main snapshot
SESSIONS_WEB = WEB_DIR / "sessions.json"       # now-playing snapshot fallback
HISTORY_WEB  = WEB_DIR / "history-full.json"   # full-resolution chart data
CACHE_FILE   = STATE_DIR / "smart-cache.json"  # last good SMART per serial
HISTORY_FILE = STATE_DIR / "history.json"      # rolling samples

TOKEN_FILE  = RUN_DIR / "api-token"            # shared secret for the web page

PLEX_PREFS  = Path("/var/lib/plexmediaserver/Library/Application Support/"
                   "Plex Media Server/Preferences.xml")
PLEX_URL    = "http://127.0.0.1:32400"

SMARTCTL = "/usr/sbin/smartctl"
HDPARM   = "/usr/sbin/hdparm"

# ---- API -----------------------------------------------------------------
API_HOST = "127.0.0.1"
API_PORT = 9847               # localhost only; token guards privileged calls

# ---- timing (seconds) ----------------------------------------------------
# The daemon runs a fast loop that only ever touches memory (I/O counters,
# /proc, the Plex API) and a slow cadence for the heavier work. Neither wakes a
# disk: SMART is read only when a disk is demonstrably already awake, or once a
# day, or on an explicit request.
FAST_INTERVAL   = 10          # now-playing, activity, power state, temps-from-io
SLOW_INTERVAL   = 300         # perf probes, history sample, snapshot rebuild
SMART_DAILY_AT  = 4           # hour of day for the one guaranteed SMART sweep

# ---- thresholds ----------------------------------------------------------
TEMP_WARN = 60                # °C — the drives' own alarm point
TEMP_CRIT = 70                # °C — top of the rated operating range
# Flash runs hotter than platters and is rated for it: an NVMe at 60 °C is
# unremarkable, while a hard disk at 60 °C is not. These apply only to NVMe, and
# only when the drive doesn't publish limits of its own — most do, and those
# always win.
TEMP_WARN_NVME = 70
TEMP_CRIT_NVME = 80

# ---- disk behaviour ------------------------------------------------------
# hdparm -C is NOT used: the JMS567 USB bridge mistranslates CHECK POWER MODE
# and both lies about the state and wakes the disk. Power state is inferred from
# kernel I/O counters, which are read from memory.
USE_HDPARM        = False
WAKE_STANDBY      = False      # never read SMART from a sleeping disk on a normal run
SPINDOWN_AFTER_S  = 900        # treat a disk idle this long as spun down
# A disk that is genuinely busy stays "awake" for as long as it's in use, and
# reading SMART is cheap on an awake disk — but not free: every read spawns
# smartctl and puts SCSI traffic on a bus that may be streaming video. Routine
# reads are therefore throttled to this interval; forced passes (wake button,
# daily sweep) ignore it.
SMART_MIN_INTERVAL = 300

# Multi-bay USB-SATA bridges spin up every disk they carry when any one of them
# is touched — the bridge powers the whole group, not individual bays. With this
# on, disks sharing a bridge share a power state: if one has I/O, the others are
# reported as spinning too (because they are), and SMART may be read from them at
# no extra cost since they're already turning. Set false for bridges that really
# do power each bay independently.
BRIDGE_WAKES_SIBLINGS = True

# ---- history -------------------------------------------------------------
HISTORY_DAYS   = 7             # retention by age, not sample count
HISTORY_MAX    = 25000         # hard cap so the file can't grow without bound
HISTORY_POINTS = 140           # down-sampled points embedded in data.json

# ---- perf probe ----------------------------------------------------------
PERF_ENDPOINTS = ["/identity", "/library/sections", "/hubs"]
PERF_SAMPLES   = 5
# Same endpoints measured through the reverse proxy; the difference against the
# direct figures is nginx's per-request cost (the "Reverse proxy" card). Empty
# string disables the probe. Override via PLEXMON_PERF_PROXY_URL or config.json.
PERF_PROXY_URL = "https://plex.falco81.net"


_CONFIG_NOTES: list = []


def _parse_config(text: str):
    """
    Parse the config file. Accepts strict JSON, and recovers from the easy
    mistake of writing several objects one after another:

        {"spindown_after_s": 600}
        {"bridge_wakes_siblings": true}

    which is not valid JSON. Rather than silently ignoring the whole file — and
    leaving settings that look applied but aren't — the objects are merged and
    the problem is reported.
    """
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        _CONFIG_NOTES.append("config.json must contain an object; ignored")
        return {}
    except ValueError:
        pass
    merged, dec, idx, n = {}, json.JSONDecoder(), 0, 0
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = dec.raw_decode(text, idx)
        except ValueError:
            break
        if isinstance(obj, dict):
            merged.update(obj)
            n += 1
        idx = end
    if n > 1:
        _CONFIG_NOTES.append(
            f"config.json holds {n} separate JSON objects instead of one — "
            "the settings were merged, but please combine them into a single "
            "{...} block")
        return merged
    _CONFIG_NOTES.append("config.json is not valid JSON — DEFAULTS ARE IN USE")
    return {}


# Exactly what may be set from config.json / the environment. An allowlist, not
# "anything spelled in capitals": without it a key like "n" would silently
# overwrite an unrelated internal constant, and a typo would be applied instead
# of reported.
CONFIGURABLE = (
    "API_HOST", "API_PORT",
    "FAST_INTERVAL", "SLOW_INTERVAL", "SMART_DAILY_AT", "SMART_MIN_INTERVAL",
    "SPINDOWN_AFTER_S", "BRIDGE_WAKES_SIBLINGS", "USE_HDPARM", "WAKE_STANDBY",
    "TEMP_WARN", "TEMP_CRIT", "TEMP_WARN_NVME", "TEMP_CRIT_NVME",
    "HISTORY_DAYS", "HISTORY_MAX", "HISTORY_POINTS",
    "PERF_ENDPOINTS", "PERF_SAMPLES", "PERF_PROXY_URL",
    "PLEX_URL", "PLEX_PREFS", "SMARTCTL", "HDPARM",
    "STATE_DIR", "WEB_DIR", "RUN_DIR",
    "DATA_FILE", "SESSIONS_WEB", "HISTORY_WEB",
    "CACHE_FILE", "HISTORY_FILE", "TOKEN_FILE",
)


def _apply_overrides(ns: dict) -> None:
    """Layer env vars and the optional JSON config on top of the defaults."""
    cfg_path = Path(os.environ.get("PLEXMON_CONFIG", "/etc/plex-status/config.json"))
    if cfg_path.is_file():
        try:
            data = _parse_config(cfg_path.read_text())
            for k, v in data.items():
                key = k.upper()
                if key in CONFIGURABLE:
                    ns[key] = _coerce(ns[key], v)
                else:
                    _CONFIG_NOTES.append(f"config.json: unknown setting '{k}' ignored")
        except Exception as e:
            _CONFIG_NOTES.append(f"config.json could not be read: {e}")
    for k in CONFIGURABLE:
        env = os.environ.get("PLEXMON_" + k)
        if env is not None:
            ns[k] = _coerce(ns[k], env)

    # Derived paths follow their directory unless set explicitly, so moving
    # state_dir or web_dir actually moves the files instead of leaving them
    # pointing at the defaults.
    given = set()
    try:
        cfgp = Path(os.environ.get("PLEXMON_CONFIG", "/etc/plex-status/config.json"))
        if cfgp.is_file():
            given |= {k.upper() for k in _parse_config(cfgp.read_text())}
    except Exception:
        pass
    given |= {k for k in CONFIGURABLE if os.environ.get("PLEXMON_" + k) is not None}
    for name, base, leaf in (
            ("DATA_FILE", "WEB_DIR", "data.json"),
            ("SESSIONS_WEB", "WEB_DIR", "sessions.json"),
            ("HISTORY_WEB", "WEB_DIR", "history-full.json"),
            ("CACHE_FILE", "STATE_DIR", "smart-cache.json"),
            ("HISTORY_FILE", "STATE_DIR", "history.json"),
            ("TOKEN_FILE", "RUN_DIR", "api-token")):
        if base in given and name not in given:
            ns[name] = ns[base] / leaf


def _coerce(current, value):
    """Make an override match the type of the default it replaces."""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, Path):
        return Path(str(value))
    return value


_apply_overrides(globals())


# ==========================================================================
# util
# ==========================================================================

_DEBUG = False


def set_debug(on: bool) -> None:
    global _DEBUG
    _DEBUG = on


def log(msg: str, *, level: str = "info") -> None:
    """Write to stderr, which journald captures. Debug lines only when asked."""
    if level == "debug" and not _DEBUG:
        return
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], timeout: float = 10.0) -> str:
    """
    Run a command, return stdout, never raise, and never block past the timeout.

    subprocess.run(timeout=...) is not safe here. On timeout it kills the child
    and then *waits* for it — and a smartctl stuck on a wedged USB bridge sits in
    uninterruptible sleep, where SIGKILL does nothing and the wait never returns.
    That hung the collection thread indefinitely: it held the collection lock, so
    every later poll skipped, and the daemon quietly stopped recording history
    until someone restarted it. Here the child is killed and abandoned; a reaper
    thread collects it whenever the kernel finally lets go, so no zombie is left
    behind and the caller always gets its timeout honoured.
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except (FileNotFoundError, OSError) as e:
        log(f"run {cmd[0]} failed: {e}", level="debug")
        return ""
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out or ""
    except subprocess.TimeoutExpired:
        log(f"{cmd[0]} did not answer in {timeout}s — abandoning it "
            f"(device may be wedged)")
        try:
            proc.kill()
        except OSError:
            pass
        threading.Thread(target=_reap, args=(proc,), daemon=True).start()
        return ""


def _reap(proc) -> None:
    """Wait on an abandoned child so it doesn't linger as a zombie."""
    try:
        proc.wait()
    except Exception:
        pass


def write_bytes_atomic(path: Path, data: bytes, *, mode: int = 0o644) -> bool:
    """Atomic write for binary data (poster images) — text mode would corrupt it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log(f"write {path} failed: {e}", level="debug")
        return False


def write_atomic(path: Path, data: str, *, mode: int = 0o644,
                 owner: Optional[str] = None) -> bool:
    """
    Write a file so a reader never sees a half-written version: write to a temp
    file in the same directory, fix ownership/mode, then rename over the target.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.chmod(tmp, mode)
            if owner:
                uid = uid_of(owner)
                if uid is not None:
                    try:
                        os.chown(tmp, uid, -1)
                    except PermissionError:
                        pass
            os.replace(tmp, path)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log(f"write {path} failed: {e}", level="debug")
        return False


def write_json(path: Path, obj, **kw) -> bool:
    return write_atomic(path, json.dumps(obj, separators=(",", ":"),
                                         ensure_ascii=False), **kw)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def uid_of(name: str) -> Optional[int]:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        out = run(["id", "-u", name]).strip()
        return int(out) if out.isdigit() else None


def name_of_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def web_user() -> str:
    """
    Which user does php-fpm run as? Needed so the API token is written readable
    by the page and nobody else. On AlmaLinux php-fpm ships as 'apache' even
    when nginx serves the site, so ask the running process rather than guess.
    """
    for proc in ("php-fpm", "php-fpm8.3", "php-fpm8.2", "php-fpm8.1", "nginx", "httpd"):
        out = run(["ps", "-o", "user=", "-C", proc])
        users = {u.strip() for u in out.splitlines() if u.strip() and u.strip() != "root"}
        if users:
            return sorted(users)[0]
    return "nginx"


# ==========================================================================
# smart
# ==========================================================================

_BRIDGE_CACHE: dict = {}


def _disk_bridge(name: str) -> Optional[str]:
    """
    The USB bridge a disk hangs off, e.g. '2-1', or None for non-USB devices.
    Taken from the sysfs device path, which contains the USB device between the
    controller and the SCSI host: .../usb2/2-1/2-1:1.0/host4/.../block/sda.
    Disks sharing this value sit in the same enclosure behind the same bridge.
    """
    if name in _BRIDGE_CACHE:
        return _BRIDGE_CACHE[name]
    br = None
    try:
        path = os.path.realpath(f"/sys/block/{name}")
        m = re.search(r"/usb\d+/(\d+-[\d.]+)/", path)
        br = m.group(1) if m else None
    except OSError:
        br = None
    _BRIDGE_CACHE[name] = br
    return br


def _disk_by_id(name: str) -> Optional[str]:
    """
    The /dev/disk/by-id name for a kernel device (e.g. 'sda'), preferring a
    wwn-* entry because it's tied to the drive itself rather than the USB bridge.
    This is the stable cache key: it survives reboots (sd? letters shuffle) and
    doesn't collapse together disks that share a bridge serial.
    """
    if not name:
        return None
    d = "/dev/disk/by-id"
    if not os.path.isdir(d):
        return None
    best = None
    try:
        for entry in os.listdir(d):
            try:
                target = os.path.basename(os.readlink(os.path.join(d, entry)))
            except OSError:
                continue
            if target != name:
                continue
            if entry.startswith("wwn-"):
                return entry
            if best is None:
                best = entry
    except OSError:
        return None
    return best


def _collect_mounts(node: dict, acc: list) -> None:
    """Gather (mountpoint, fssize, fsused) from a device and all its descendants."""
    mp = node.get("mountpoint")
    if mp:
        acc.append((mp, node.get("fssize"), node.get("fsused")))
    for c in node.get("children", []):
        _collect_mounts(c, acc)


def _pick_mount(node: dict) -> tuple:
    """
    Choose the mount that best represents a disk. The root filesystem wins (it's
    what matters on the system disk), otherwise the deepest real data path —
    never a tiny helper partition like /boot/efi or /boot when something more
    meaningful is mounted on the same disk. Recurses so LVM/crypt children count.
    """
    mounts = []
    _collect_mounts(node, mounts)
    if not mounts:
        return None, None, None
    real = [m for m in mounts if m[0] and m[0] not in ("[SWAP]",)]
    if not real:
        return None, None, None
    # root always wins
    for mp, sz, us in real:
        if mp == "/":
            return mp, sz, us
    # otherwise prefer a data mount over boot/efi helpers, then the longest path
    def score(m):
        mp = m[0]
        helper = mp.startswith("/boot") or mp == "/efi"
        return (0 if helper else 1, len(mp))
    real.sort(key=score, reverse=True)
    return real[0]


def list_block_devices() -> list[dict]:
    """Physical disks with their transport and mount, via lsblk (reads sysfs)."""
    out = run(["lsblk", "-bJ", "-o",
               "NAME,PATH,SIZE,MODEL,SERIAL,TRAN,TYPE,MOUNTPOINT,FSSIZE,FSUSED"])
    try:
        tree = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []
    devs = []
    for d in tree.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        mount, fssize, fsused = _pick_mount(d)
        d["_mount"] = mount
        d["fssize"] = fssize
        d["fsused"] = fsused
        devs.append(d)
    return devs


def diskstat(name: str) -> Optional[tuple[int, int]]:
    """(sectors read, sectors written) from /sys/block/<name>/stat — from memory."""
    try:
        parts = Path(f"/sys/block/{name}/stat").read_text().split()
        return int(parts[2]), int(parts[6])
    except (OSError, IndexError, ValueError):
        return None


def infer_power(name: str, tran: str, prev_total: Optional[int],
                idle_for: float, live_io: bool, recent_io: bool) -> tuple[str, str]:
    """
    Work out standby vs active without touching the disk. Ported from the PHP
    collector's decision so behaviour matches:

      * NVMe: always active.
      * Any I/O this poll or since the last one: active.
      * We have a baseline and it's been quiet less than the spindown timer:
        active (the disk hasn't had time to spin down yet).
      * Quiet at least the spindown timer: standby.
      * No baseline at all yet: unknown.

    The key point is the third rule: a disk read from in bursts (video playback)
    can go a poll or two without the counter moving, and it must NOT be called
    standby until it's genuinely been idle past the drive's own spindown timer.
    """
    if tran == "nvme":
        return "active", "nvme"
    if live_io or recent_io:
        return "active", "I/O since last poll"
    if prev_total is not None and idle_for < SPINDOWN_AFTER_S:
        return "active", f"idle only {int(idle_for)}s"
    if idle_for >= SPINDOWN_AFTER_S:
        return "standby", f"no I/O for {int(idle_for)}s"
    return "unknown", "no baseline yet"


# ---- SMART ---------------------------------------------------------------

# Per-device list of `smartctl -d` types known to return data, so repeat reads
# don't re-try the ones that never work on this bridge.
_SMART_TYPES_OK: dict = {}

_ATTR_RE = {
    "realloc":      "Reallocated_Sector_Ct",
    "pending":      "Current_Pending_Sector",
    "uncorrect":    "Offline_Uncorrectable",
    "reported_unc": "Reported_Uncorrect",
    "cmd_timeout":  "Command_Timeout",
    "crc_err":      "UDMA_CRC_Error_Count",
    "spin_retry":   "Spin_Retry_Count",
    "start_stop":   "Start_Stop_Count",
    "load_cycle":   "Load_Cycle_Count",
    "power_cycle":  "Power_Cycle_Count",
    "poh":          "Power_On_Hours",
    "offretract":   "Power-Off_Retract_Count",
    "lba_written":  "Total_LBAs_Written",
    "lba_read":     "Total_LBAs_Read",
}


def _attr(text: str, name: str) -> Optional[int]:
    # The daemon runs `smartctl -x`, whose attribute table is
    #   ID NAME FLAGS VALUE WORST THRESH FAIL RAW_VALUE
    # The RAW value is the last column. Grab the last number on the row, allowing
    # an optional bracket breakdown after it ("46115 (45 12 0)"). The previous
    # attempt assumed the wider `-a` layout and ended up capturing the normalized
    # VALUE column instead — which showed up as every attribute reading 100.
    m = re.search(
        r"^\s*\d+\s+" + re.escape(name) + r"\s+.+?\s+(\d+)(?:\s+\([\d\s]+\))?\s*$",
        text, re.MULTILINE)
    return int(m.group(1)) if m else None


def _attr_raw_row(text: str, name: str) -> Optional[dict]:
    m = re.search(r"^\s*(\d+)\s+" + re.escape(name) +
                  r"\s+\S+\s+(\d+)\s+(\d+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(.+?)\s*$",
                  text, re.MULTILINE)
    if not m:
        return None
    return {"id": int(m.group(1)), "value": int(m.group(2)),
            "worst": int(m.group(3)), "thresh": m.group(4),
            "raw": m.group(5).strip()}


def _parse_temp(text: str) -> Optional[int]:
    for pat in (r"Temperature_Celsius\s+.*?(\d+)(?:\s|$)",
                r"Airflow_Temperature_Cel\s+.*?(\d+)(?:\s|$)",
                r"Current Drive Temperature:\s+(\d+)",
                r"Temperature:\s+(\d+)\s+Celsius"):
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def _parse_temp_minmax(text: str) -> tuple[Optional[int], Optional[int]]:
    m = re.search(r"Min/Max\s+Temperature:\s+(-?\d+)/(-?\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    lo = re.search(r"Temperature_Celsius\s+.*?\(Min/Max\s+(-?\d+)/(-?\d+)\)", text)
    if lo:
        return int(lo.group(1)), int(lo.group(2))
    return None, None


def _parse_health(text: str) -> Optional[str]:
    m = re.search(r"(?:SMART overall-health.*?:|SMART Health Status:)\s*(\w+)", text)
    return m.group(1) if m else None


def _parse_selftest(text: str) -> Optional[str]:
    m = re.search(r"# 1\s+(.*?)\s{2,}", text)
    return m.group(1).strip() if m else None


def _nvme(text: str, label: str) -> Optional[int]:
    m = re.search(re.escape(label) + r":\s+([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None


def run_smartctl(devpath: str, tran: str) -> dict:
    """
    Read SMART, trying several -d types, merging the best of each field.

    Some USB bridges answer one -d type with only a health status and another
    with only a temperature; taking the best of each gives the fullest picture.
    THIS SPINS THE DISK UP — callers gate it on the disk already being awake.
    """
    if tran == "nvme":
        types = ["nvme"]
    else:
        types = ["sat", "sat,12", "sat,16", "usbjmicron", "auto", "scsi"]

    # Trying every -d type on every read means six smartctl processes per disk
    # per pass, most of which are known to fail on this bridge. Once we've seen
    # which ones actually return data, use only those. If they all stop working
    # (bridge swapped, disk moved) we fall back to the full list below.
    known = _SMART_TYPES_OK.get(devpath)
    attempt = known if known else types
    # A known-good type gets the full timeout; a speculative probe does not,
    # because a bridge that doesn't understand it tends to stall until timeout.
    probe_timeout = 30 if known else 12

    merged: dict = {"smart_type": "n/a", "all_attrs": {}}
    last_out = ""
    worked: list = []
    for t in attempt:
        out = run([SMARTCTL, "-x", "-d", t, devpath], timeout=probe_timeout)
        if not out or "Unknown USB bridge" in out or "please specify device type" in out:
            continue
        last_out = out
        got_any = False

        temp = _parse_temp(out)
        if temp is not None and merged.get("temp") is None:
            merged["temp"] = temp; got_any = True

        health = _parse_health(out)
        if health and merged.get("health") is None:
            merged["health"] = health
            merged["smart_ok"] = (health.upper() in ("PASSED", "OK"))
            got_any = True

        for key, attr in _ATTR_RE.items():
            if merged.get(key) is None:
                v = _attr(out, attr)
                if v is not None:
                    merged[key] = v; got_any = True

        if tran == "nvme":
            for key, label in (("poh", "Power On Hours"),
                               ("power_cycle", "Power Cycles"),
                               ("nvme_written", "Data Units Written"),
                               ("nvme_read", "Data Units Read"),
                               ("unsafe_shutdown", "Unsafe Shutdowns"),
                               ("err_log", "Error Information Log Entries"),
                               ("nvme_media_err", "Media and Data Integrity Errors")):
                if merged.get(key) is None:
                    v = _nvme(out, label)
                    if v is not None:
                        merged[key] = v; got_any = True
            # These two carry a % sign, so the plain "label: number" parse above
            # misses them. Names must match what the page reads — the endurance
            # figure used to be stored as nvme_used_pct, which nothing looked at,
            # so the card showed a dash for a value we had all along.
            for key, label in (("nvme_used", "Percentage Used"),
                               ("nvme_spare", "Available Spare")):
                if merged.get(key) is None:
                    mm = re.search(re.escape(label) + r":\s+(\d+)\s*%", out)
                    if mm:
                        merged[key] = int(mm.group(1)); got_any = True
            # An NVMe has no spinning platters, so the ATA attribute table is
            # empty. Give the "All SMART values" expander something real to show.
            for label, key in (("Critical Warning", "nvme_crit_warn"),
                               ("Available Spare Threshold", "nvme_spare_thresh"),
                               ("Host Read Commands", "nvme_host_reads"),
                               ("Host Write Commands", "nvme_host_writes"),
                               ("Controller Busy Time", "nvme_busy_time"),
                               ("Warning  Comp. Temperature Time", "nvme_warn_temp_time"),
                               ("Critical Comp. Temperature Time", "nvme_crit_temp_time")):
                mm = re.search(re.escape(label) + r":\s+(\S+)", out)
                if mm and label not in merged["all_attrs"]:
                    merged["all_attrs"][label.strip()] = {
                        "id": 0, "value": 0, "worst": 0, "thresh": "—",
                        "raw": mm.group(1).rstrip("%")}
                    got_any = True

        # The drive's own temperature limits, when it publishes them. NVMe media
        # is rated far hotter than a hard disk — this one warns at 77 °C, while
        # our global default warns at 60 — so judging it by the spinning-disk
        # thresholds would raise an alarm over a perfectly healthy SSD.
        for key, pat in (("temp_warn_own", r"Warning\s+Comp\.\s+Temp\.\s+Threshold:\s+(\d+)"),
                         ("temp_crit_own", r"Critical\s+Comp\.\s+Temp\.\s+Threshold:\s+(\d+)")):
            if merged.get(key) is None:
                mm = re.search(pat, out)
                if mm:
                    merged[key] = int(mm.group(1))

        tmin, tmax = _parse_temp_minmax(out)
        if tmin is not None and merged.get("tmin") is None:
            merged["tmin"], merged["tmax"] = tmin, tmax; got_any = True

        st = _parse_selftest(out)
        if st and merged.get("selftest") is None:
            merged["selftest"] = st; got_any = True

        mm = re.search(r"Model Family:\s+(.+)", out) or re.search(r"Device Model:\s+(.+)", out) \
            or re.search(r"Model Number:\s+(.+)", out)
        if mm and merged.get("real_model") is None:
            merged["real_model"] = mm.group(1).strip()
        sn = re.search(r"Serial Number:\s+(.+)", out)
        if sn and merged.get("real_serial") is None:
            merged["real_serial"] = sn.group(1).strip()

        # full attribute table, for the "All SMART values" expander on the page
        for line in out.splitlines():
            # Handle both `-x` (ID NAME FLAGS VALUE WORST THRESH FAIL RAW) and
            # `-a` (…THRESH TYPE UPDATED WHEN_FAILED RAW): match up to THRESH, then
            # take everything after it and pull the RAW value (last number, before
            # an optional bracket breakdown) out of that tail.
            row = re.match(
                r"^\s*(\d+)\s+([\w-]+)\s+\S+\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$",
                line)
            if row and row.group(2) not in merged["all_attrs"]:
                tail = row.group(6)
                rm = re.search(r"(\d+)(?:\s+\([\d\s]+\))?\s*$", tail)
                raw = rm.group(1) if rm else tail.strip().split()[-1]
                merged["all_attrs"][row.group(2)] = {
                    "id": int(row.group(1)), "value": int(row.group(3)),
                    "worst": int(row.group(4)), "thresh": row.group(5),
                    "raw": raw}
                got_any = True

        if got_any:
            worked.append(t)
            if merged["smart_type"] == "n/a":
                merged["smart_type"] = t

        # Stop as soon as this type gave us the essentials. Merging across every
        # type exists for bridges that scatter the data (health from one, temp
        # from another); when one type answers fully there is nothing to gain
        # from five more invocations — and on a USB bridge the failing ones stall
        # for seconds each, which is what made the first wake after a restart
        # take longer than the page was willing to wait.
        if (merged.get("health") is not None and merged.get("temp") is not None
                and merged["all_attrs"]):
            break

    if worked:
        _SMART_TYPES_OK[devpath] = worked
    elif known:
        # the remembered types stopped working — forget them and retry in full
        _SMART_TYPES_OK.pop(devpath, None)
        return run_smartctl(devpath, tran)

    # Fallback: if a key numeric field didn't parse from the per-attribute pass
    # but the full attribute table has it, take the first integer of its raw
    # value. This is what rescues Power-on hours (and similar) on drives whose
    # raw column carries a suffix the direct parser tripped over.
    attr_map = {"poh": "Power_On_Hours", "start_stop": "Start_Stop_Count",
                "load_cycle": "Load_Cycle_Count", "power_cycle": "Power_Cycle_Count",
                "realloc": "Reallocated_Sector_Ct", "pending": "Current_Pending_Sector",
                "uncorrect": "Offline_Uncorrectable", "crc_err": "UDMA_CRC_Error_Count",
                "reported_unc": "Reported_Uncorrect"}
    for field, attr in attr_map.items():
        if merged.get(field) is None and attr in merged["all_attrs"]:
            raw = str(merged["all_attrs"][attr].get("raw", ""))
            mnum = re.match(r"\s*(\d+)", raw)
            if mnum:
                merged[field] = int(mnum.group(1))

    # SCSI/SAS drives report hours in prose rather than the attribute table —
    # the same fallbacks the PHP parse_poh() had.
    if merged.get("poh") is None:
        for pat in (r"Power On Hours:\s*([\d,]+)",
                    r"number of hours powered up\s*=\s*([\d.]+)"):
            mm = re.search(pat, last_out, re.I) if last_out else None
            if mm:
                merged["poh"] = int(float(mm.group(1).replace(",", "")))
                break

    # Worst pre-fail margin (VALUE − THRESH): the "Closest to threshold" row in
    # All SMART values. Pre-fail rows are flagged 'P…' in -x FLAGS or labelled
    # 'Pre-fail' in the -a TYPE column.
    worst = None
    worst_attr = None
    if last_out:
        for line in last_out.splitlines():
            mm = re.match(r"^\s*\d+\s+([\w-]+)\s+(\S+)\s+(\d+)\s+\d+\s+(\d+)", line)
            if not mm:
                continue
            flags = mm.group(2)
            prefail = flags.startswith("P") or "Pre-fail" in line
            if not prefail:
                continue
            margin = int(mm.group(3)) - int(mm.group(4))
            if worst is None or margin < worst:
                worst, worst_attr = margin, mm.group(1)
    if worst is not None:
        merged["margin"] = worst
        merged["margin_attr"] = worst_attr

    return merged


# ==========================================================================
# disks
# ==========================================================================

class DiskMonitor:
    def __init__(self) -> None:
        self.cache: dict = read_json(CACHE_FILE, {}) or {}
        # Restore which `smartctl -d` types work for each device. Without this the
        # memory is rebuilt from scratch after every restart, and the first wake
        # pays a full six-type probe per disk — slow enough that the page gave up
        # waiting and reported no SMART refresh until a second click.
        saved = (self.cache.get("__meta") or {}).get("smart_types")
        if isinstance(saved, dict):
            _SMART_TYPES_OK.update({k: v for k, v in saved.items()
                                    if isinstance(v, list) and v})
        # per-device rolling I/O state, keyed by kernel name
        self._io: dict[str, dict] = {}
        # when WE last spun a disk up by reading SMART — reporting only, never a
        # reason to read SMART again (see mark_awake)
        self._smart_woke: dict[str, float] = {}
        # when SMART was last actually read per device, for throttling
        self._last_smart: dict[str, float] = {}
        # did we report this disk as standby since its last SMART read? used to
        # sanity-check SPINDOWN_AFTER_S against the drive's real behaviour
        self._saw_standby: dict[str, bool] = {}
        self._last_serial: dict[str, str] = {}

    # -- cache keyed by serial, because sd? letters shuffle across reboots --
    def _key(self, dev: dict) -> str:
        """
        One stable key used for BOTH writing and reading the cache. Prefer the
        /dev/disk/by-id name (a wwn-* symlink is tied to the drive itself), then
        the real drive serial once we've read SMART, then the lsblk serial, then
        the mount. This must be computed the same way on every path — keying the
        write by one thing and the read by another is what made cached SMART
        vanish on the USB-bridged disks, whose lsblk 'serial' is the bridge's
        (shared by all of them), not the drive's.
        """
        byid = _disk_by_id(dev.get("name", ""))
        return (byid or dev.get("_real_serial") or dev.get("serial")
                or dev.get("_mount") or dev.get("name") or "?")

    def _cached(self, key: str) -> dict:
        entry = self.cache.get(key)
        return dict(entry) if isinstance(entry, dict) else {}

    def sample_io(self, devs: list[dict]) -> None:
        """Take an I/O snapshot for every disk. Pure memory read; wakes nothing."""
        now = time.time()
        for d in devs:
            name = d["name"]
            st = diskstat(name)
            if st is None:
                continue
            r, w = st
            total = r + w
            prev = self._io.get(name)
            if prev is None:
                # First time we've seen this disk. We have no idea how long it
                # has been idle, so seed changed_at far in the past — otherwise a
                # freshly-observed sleeping disk would look "recently active".
                self._io[name] = {"total": total, "last_total": total,
                                  "r": r, "w": w, "dr": 0, "dw": 0,
                                  "changed_at": now - SPINDOWN_AFTER_S - 1,
                                  "ever_changed": False, "changed_since_poll": False}
            else:
                # changed_since_poll: did the absolute counter move since the
                # PREVIOUS collect? This is the PHP recent_io test and has no time
                # window — a disk being read from is active even if the reads come
                # in bursts spaced further apart than a poll interval.
                prev["changed_since_poll"] = (total != prev["last_total"])
                prev["last_total"] = total
                # per-direction deltas drive the Reading/Writing badge
                prev["dr"] = max(0, r - prev.get("r", r))
                prev["dw"] = max(0, w - prev.get("w", w))
                prev["r"], prev["w"] = r, w
                if total != prev["total"]:
                    prev["total"] = total
                    prev["changed_at"] = now
                    prev["ever_changed"] = True

    def mark_awake(self, name: str) -> None:
        """
        Record that WE spun this disk up by reading SMART.

        Deliberately kept out of the I/O bookkeeping. Feeding this back into
        `changed_at` created a loop that never ended: the SMART read looked like
        activity, activity meant "disk is awake, safe to read SMART", and the
        next read renewed it — so the daemon polled SMART every 10 s forever and
        the drives could never spin down. This timestamp only informs what state
        we *report*; whether we may read SMART stays tied to real disk I/O.
        """
        self._smart_woke[name] = time.time()

    def _io_state(self, name: str) -> tuple[Optional[int], float, bool, bool]:
        """(current total, idle seconds, live this poll, changed since last poll)."""
        st = self._io.get(name)
        if st is None:
            return None, SPINDOWN_AFTER_S + 1, False, False
        idle = time.time() - st["changed_at"]
        live = st.get("ever_changed", False) and idle < FAST_INTERVAL * 1.5
        changed = st.get("changed_since_poll", False)
        return st["total"], idle, live, changed

    def collect(self, *, force: bool = False, read_smart: bool = True,
                align_smart: bool = False) -> list[dict]:
        """
        Build the per-disk records. `force` reads SMART even from a sleeping
        disk (used by the daily sweep and wake requests). With read_smart False
        the disks are reported purely from I/O state and cache — no smartctl at
        all — which is what the fast loop uses between SMART reads.
        """
        devs = list_block_devices()
        self.sample_io(devs)
        out = []

        # Per-disk I/O state, then — if the bridge powers a whole enclosure —
        # merged across each bridge group. A multi-bay bridge spins every bay up
        # together, so a disk whose siblings are busy is physically turning even
        # though nothing addressed it: its own counters stay flat while the
        # platters are moving. Sharing the group's freshest idle time makes the
        # reported state match the hardware, and lets SMART be read from a disk
        # that is already spinning at no extra cost.
        st_by_dev: dict[str, dict] = {}
        for d in devs:
            name = d["name"]
            total, idle, live, changed = self._io_state(name)
            recent = live or changed or (
                self._io.get(name, {}).get("ever_changed", False)
                and idle < FAST_INTERVAL * 3)
            st_by_dev[name] = {"total": total, "idle": idle, "live": live,
                               "recent": recent, "via_bridge": None}

        if BRIDGE_WAKES_SIBLINGS:
            groups: dict[str, list] = {}
            for d in devs:
                if (d.get("tran") or "").lower() != "usb":
                    continue
                br = _disk_bridge(d["name"])
                if br:
                    groups.setdefault(br, []).append(d["name"])
            for br, members in groups.items():
                if len(members) < 2:
                    continue
                busiest = min(members, key=lambda n: st_by_dev[n]["idle"])
                min_idle = st_by_dev[busiest]["idle"]
                any_live = any(st_by_dev[n]["live"] for n in members)
                any_recent = any(st_by_dev[n]["recent"] for n in members)
                for n in members:
                    s = st_by_dev[n]
                    if s["idle"] > min_idle:
                        s["idle"] = min_idle
                        s["via_bridge"] = (br, busiest)
                    if any_live and not s["live"]:
                        s["live"] = True
                        s["via_bridge"] = s["via_bridge"] or (br, busiest)
                    if any_recent and not s["recent"]:
                        s["recent"] = True
                        s["via_bridge"] = s["via_bridge"] or (br, busiest)

        for d in devs:
            name = d["name"]
            tran = (d.get("tran") or "").lower()
            key = self._key(d)
            st = st_by_dev.get(name, {})
            total, idle = st.get("total"), st.get("idle", SPINDOWN_AFTER_S + 1)
            live, recent = st.get("live", False), st.get("recent", False)
            have_baseline = total is not None

            power, psrc = infer_power(name, tran, total, idle, live, recent)
            # Say plainly when a disk is only spinning because a bridge sibling
            # is: it explains an "active" disk with no I/O of its own.
            if st.get("via_bridge") and power == "active":
                br, busiest = st["via_bridge"]
                psrc = f"spun up with {busiest} on bridge {br}"
            # A disk we spun up ourselves is genuinely turning, even though the
            # SMART read left no trace in the kernel I/O counters. Report that
            # for as long as the drive's own spindown timer would keep it up.
            # This is presentation only — `awake_certain` below still depends on
            # real I/O, so it can't turn into a reason to keep polling SMART.
            woke = self._smart_woke.get(name)
            if woke is not None and tran != "nvme":
                since = time.time() - woke
                if since < SPINDOWN_AFTER_S:
                    if power != "active":
                        power = "active"
                        psrc = f"spun up {int(since)}s ago"
                else:
                    self._smart_woke.pop(name, None)
            iost = self._io.get(name, {})
            io = "write" if iost.get("dw", 0) > 0 else (
                 "read" if iost.get("dr", 0) > 0 else "idle")

            # Record what we actually REPORT (after any override), not what the
            # raw inference said — this flag exists to be compared against the
            # drive's own spin-up counter, so it has to match what the user saw.
            if power == "standby":
                self._saw_standby[name] = True

            awake_certain = (tran == "nvme") or live or recent
            # Don't re-read SMART on every tick just because a disk is busy: the
            # values change on the order of minutes, not seconds, and each read
            # costs several smartctl processes. Forced passes bypass this.
            last_read = self._last_smart.get(name, 0.0)
            # Read on the pass that writes a history sample, so every sample
            # carries fresh counters. Without this the two cadences drift past
            # each other and most samples record nothing — which is why the
            # per-day charts came out as isolated dots instead of lines. Both
            # intervals default to 300 s, so this aligns the reads rather than
            # adding any.
            due = align_smart or (time.time() - last_read) >= SMART_MIN_INTERVAL
            may_read = read_smart and (force or (awake_certain and due))

            rec = {
                "dev": name,
                "path": d.get("path"),
                "mount": d.get("_mount"),
                "tran": tran,
                "serial": d.get("serial"),
                # A stable, UNIQUE key for history/charts. Never the lsblk serial:
                # USB-bridged disks all report the bridge's serial, which would
                # collapse every disk into one chart line (that's why Trends only
                # showed /data). by-id is per-drive; mount and dev are unique
                # fallbacks.
                "hist_key": (_disk_by_id(name) or d.get("_mount")
                             or d.get("path") or name),
                "model": d.get("model"),
                "size": self._human_size(d.get("size")),
                "size_b": d.get("size"),
                "fs_size_b": d.get("fssize"),
                "fs_used_b": d.get("fsused"),
                "power": power,
                # No hdparm on this bridge, so power is always inferred from I/O
                # counters. The 'inferred:' prefix and power_reliable=False are
                # what the page keys its "state is inferred" tooltip note on.
                "power_src": ("inferred: " + psrc) if tran != "nvme" else psrc,
                "power_reliable": tran == "nvme",
                "idle_for_s": int(idle),
                "io": io,
            }
            if d.get("fssize") and d.get("fsused"):
                try:
                    size_b = int(d["fssize"])
                    used_b = int(d["fsused"])
                    rec["fs_pct"] = round(used_b / size_b * 100)
                    rec["fs_size"] = self._human_size(size_b)
                    rec["fs_used"] = self._human_size(used_b)
                except (ValueError, ZeroDivisionError):
                    rec["fs_pct"] = None

            if may_read:
                sm = run_smartctl(d["path"], tran)
                if sm.get("smart_type", "n/a") != "n/a":
                    rec.update(sm)
                    rec["from_cache"] = False
                    rec["smart_stale"] = False
                    rec["cache_age"] = 0
                    rec["read_at"] = int(time.time())
                    self._last_smart[name] = time.time()
                    # Reading SMART spins the disk up, but smartctl talks to it
                    # over SG_IO, which does NOT increment /sys/block/*/stat. So
                    # mark the disk awake explicitly — otherwise a disk we just
                    # woke (via the wake button / daily sweep) would look idle and
                    # be reported as standby minutes later while it's still spinning.
                    self.mark_awake(name)
                    # Only claim the read is what woke it when nothing else did:
                    # for a disk already busy with real I/O, "I/O since last poll"
                    # is the truthful and more useful explanation.
                    if power != "active":
                        rec["power"] = "active"
                        rec["power_src"] = "just read SMART (disk spun up)"
                    # Key the write with the SAME function the read uses. Feed the
                    # real serial in first so _key can prefer by-id/real-serial
                    # consistently on both sides.
                    d["_real_serial"] = sm.get("real_serial")
                    ck = self._key(d)
                    # "SMART OK for X" — track when the error counters last moved.
                    # Compare against the previous cache entry BEFORE overwriting.
                    prev_entry = self.cache.get(ck) or {}
                    # Sanity-check SPINDOWN_AFTER_S against reality. Start_Stop_Count
                    # rises every time the drive spins up. If it rose since our last
                    # read while we never once reported this disk as standby, the
                    # drive parked and restarted behind our back — i.e. its real
                    # spindown timer is shorter than what we assume, and the state
                    # we show is wrong. Nothing else can catch this: we deliberately
                    # never ask the drive its power state.
                    prev_ss = prev_entry.get("start_stop")
                    new_ss = sm.get("start_stop")
                    if (prev_ss is not None and new_ss is not None
                            and new_ss > prev_ss and not self._saw_standby.get(name)):
                        rec["spindown_mismatch"] = True
                        log(f"{name}: spun up {new_ss - prev_ss}× since the last "
                            f"read although we never reported standby — the real "
                            f"spindown timer looks shorter than SPINDOWN_AFTER_S "
                            f"({SPINDOWN_AFTER_S}s); consider lowering it")
                    self._saw_standby[name] = False
                    # Which counters count as "errors" depends on the medium.
                    # An NVMe has none of the ATA sector attributes, so watching
                    # those left it with nothing to track and the card never got
                    # its "no error-counter change in ..." line. Flash reports
                    # its own equivalents.
                    if tran == "nvme":
                        counters = [sm.get("nvme_media_err"), sm.get("err_log")]
                    else:
                        counters = [sm.get("realloc"), sm.get("pending"),
                                    sm.get("uncorrect"), sm.get("reported_unc"),
                                    sm.get("crc_err")]
                    prev_counters = prev_entry.get("counters")
                    counters_ts = int(prev_entry.get("counters_ts") or time.time())
                    if prev_counters is not None and counters != prev_counters:
                        counters_ts = int(time.time())
                        log(f"{name}: SMART error counters changed", level="debug")
                    entry = {**sm, "cached_at": int(time.time()),
                             "dev": name, "mount": d.get("_mount")}
                    if None not in counters:
                        entry["counters"] = counters
                        entry["counters_ts"] = counters_ts
                    self.cache[ck] = entry
                    if _SMART_TYPES_OK:
                        self.cache.setdefault("__meta", {})["smart_types"] = \
                            dict(_SMART_TYPES_OK)
                    rec["stable_since"] = counters_ts
                else:
                    self._from_cache(rec, key)
            else:
                self._from_cache(rec, key)

            out.append(rec)

        # Prefer what the DRIVE reports over the USB bridge's generics: the
        # bridge advertises "USB3.0 DISKnn" and one shared serial for every bay,
        # while SMART gives the real model and serial. Keep the bridge name as
        # "enclosure" the way the PHP collector did.
        for rec in out:
            rec["enclosure"] = rec.get("model") or None
            if rec.get("real_model"):
                rec["model"] = rec["real_model"]
            if rec.get("real_serial"):
                rec["serial"] = rec["real_serial"]

        # The page reads a fixed set of SMART fields; make sure each disk record
        # has them all (null when a drive doesn't report that attribute) so the
        # template never trips over a missing key.
        optional = ("realloc", "pending", "uncorrect", "reported_unc", "crc_err",
                    "spin_retry", "cmd_timeout", "offretract", "spinup_ms",
                    "power_cycle", "start_stop", "load_cycle", "poh",
                    "lba_written", "lba_read", "margin", "margin_attr", "selftest",
                    "tmin", "tmax", "nvme_written", "nvme_read", "nvme_used",
                    "nvme_spare", "nvme_media_err", "unsafe_shutdown", "err_log",
                    "temp", "health", "smart_ok", "fs_pct", "fs_size", "fs_used",
                    "smart_stale", "temp_warn_own", "temp_crit_own",
                    "temp_warn_eff", "temp_crit_eff")
        for rec in out:
            for k in optional:
                rec.setdefault(k, None)
            # USB bridge that passes only a health verdict, nothing else — the
            # page shows "bridge exposes health only" for these.
            w, c = temp_limits(rec)
            rec["temp_warn_eff"], rec["temp_crit_eff"] = w, c
            rec["smart_limited"] = (
                rec.get("health") is not None and rec.get("temp") is None
                and rec.get("poh") is None and rec.get("realloc") is None
                and rec.get("pending") is None and rec.get("uncorrect") is None)
            rec.setdefault("stable_since", None)

        self._prune_cache(out)
        write_json(CACHE_FILE, self.cache, mode=0o600)
        return out

    @staticmethod
    def _human_size(b) -> str:
        try:
            b = int(b)
        except (TypeError, ValueError):
            return "?"
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if b < 1000:
                return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
            b /= 1000
        return f"{b:.1f} EB"

    def _from_cache(self, rec: dict, key: str) -> None:
        """Fill a record from the last good SMART read, flagged as cached."""
        # Use the key we were given — it's the same _key() used on write. Falling
        # back to the lsblk serial here was the bug: on USB-bridged disks that's
        # the bridge's serial, which never matches the by-id write key.
        entry = self.cache.get(key)
        if not entry:
            # tolerate older cache entries written under a bare serial
            entry = self._cached(rec.get("serial") or "")
        if entry:
            for k, v in entry.items():
                if k not in ("cached_at", "dev", "mount", "counters", "counters_ts"):
                    rec.setdefault(k, v)
            if entry.get("counters_ts"):
                rec.setdefault("stable_since", int(entry["counters_ts"]))
            rec["from_cache"] = True
            # Only report an age if we have a real timestamp. An entry inherited
            # from the old PHP cache has no cached_at, and time()-0 would render
            # as "20658d ago" — better to show nothing than a nonsense figure.
            cached_at = int(entry.get("cached_at", 0) or 0)
            rec["cache_age"] = (int(time.time()) - cached_at) if cached_at > 0 else None
            # Being served from cache is not the same as being out of date.
            # Routine reads are throttled to SMART_MIN_INTERVAL, so a disk read
            # moments ago is served from cache on the very next poll while still
            # being completely current. Only call it stale once it actually is —
            # otherwise a freshly woken disk looked out of date seconds later.
            age = rec.get("cache_age")
            rec["smart_stale"] = age is None or age > SMART_MIN_INTERVAL
        else:
            rec["from_cache"] = True
            rec["smart_stale"] = True
            rec["health"] = None
            rec["smart_ok"] = None
            rec["temp"] = None

    def _prune_cache(self, current: list[dict]) -> None:
        keep = {r.get("serial") for r in current if r.get("serial")}
        keep |= {r.get("real_serial") for r in current if r.get("real_serial")}
        # keep __meta and anything still present; drop vanished drives
        for k in list(self.cache):
            if k == "__meta":
                continue
            if k not in keep and not any(
                    (self.cache[k].get("mount") == r.get("mount")) for r in current):
                pass  # keep by default; disks that briefly disappear shouldn't lose history

    # -- wake probe files, created while a disk is up during a forced run --
    def create_probes(self, disks: list[dict]) -> None:
        for d in disks:
            mp = d.get("mount")
            if not mp or mp == "/" or d.get("tran") == "nvme":
                continue
            probe = Path(mp) / ".wake-probe"
            try:
                if probe.is_file() and probe.stat().st_size >= 65536:
                    continue
                probe.write_bytes(b"\0" * (1024 * 1024))
                probe.chmod(0o644)
                log(f"created wake probe {probe}", level="debug")
            except OSError as e:
                log(f"probe {probe}: {e}", level="debug")

    def mark_smart_read(self, disks: list[dict]) -> int:
        """Persist when SMART was genuinely read, surviving later cached runs."""
        n = sum(1 for d in disks
                if not d.get("from_cache") and d.get("tran") != "nvme"
                and d.get("smart_type", "n/a") != "n/a")
        if n:
            self.cache.setdefault("__meta", {})["last_smart_read"] = int(time.time())
            write_json(CACHE_FILE, self.cache, mode=0o600)
        return n

    @property
    def last_smart_read(self) -> int:
        return int(self.cache.get("__meta", {}).get("last_smart_read", 0))


# ==========================================================================
# system
# ==========================================================================

def read_system() -> dict:
    out = {"load": [0.0, 0.0, 0.0], "mem_total": 0, "mem_used": 0,
           "cpu_temp": None, "uptime_s": 0, "ncpu": os.cpu_count() or 1,
           "kernel": ""}
    try:
        out["load"] = list(os.getloadavg())
    except OSError:
        pass
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            mem[k] = int(v.strip().split()[0]) * 1024  # kB -> bytes
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        out["mem_total"] = total
        out["mem_used"] = total - avail
    except (OSError, ValueError):
        pass
    out["cpu_temp"] = _cpu_temp()
    try:
        out["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError):
        pass
    try:
        out["kernel"] = Path("/proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        pass
    return out


def _cpu_temp():
    """Highest thermal-zone / hwmon reading, in °C."""
    best = None
    for tz in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            v = int(tz.read_text().strip()) / 1000.0
            if best is None or v > best:
                best = v
        except (OSError, ValueError):
            pass
    if best is None:
        for h in Path("/sys/class/hwmon").glob("hwmon*/temp*_input"):
            try:
                v = int(h.read_text().strip()) / 1000.0
                if best is None or v > best:
                    best = v
            except (OSError, ValueError):
                pass
    return round(best) if best is not None else None


def temp_limits(d: dict) -> tuple[int, int]:
    """
    Effective (warn, crit) for one disk, in order of authority:

      1. what the drive itself publishes — it knows its own rating
      2. the NVMe defaults, for flash that publishes nothing
      3. the global defaults, meant for spinning disks

    Resolved once here so the daemon's verdict and the page's colouring can
    never disagree about where the line is.
    """
    own_w, own_c = d.get("temp_warn_own"), d.get("temp_crit_own")
    if own_w and own_c:
        return int(own_w), int(own_c)
    if (d.get("tran") or "").lower() == "nvme":
        return int(own_w or TEMP_WARN_NVME), int(own_c or TEMP_CRIT_NVME)
    return int(own_w or TEMP_WARN), int(own_c or TEMP_CRIT)


def assess(disks: list[dict], system: dict) -> tuple[str, list[dict]]:
    """
    Reduce everything to (overall, reasons). Ported verbatim from the PHP so the
    dashboard's colour and messages don't change. Severity order: crit > warn > ok.
    """
    reasons: list[dict] = []
    level = "ok"

    def bump(new):
        nonlocal level
        order = {"ok": 0, "warn": 1, "crit": 2}
        if order[new] > order[level]:
            level = new

    for d in disks:
        tag = d.get("mount") or d.get("dev")
        if d.get("smart_ok") is False:
            reasons.append({"level": "crit", "text": f"{tag}: SMART reports FAILED"})
            bump("crit")
        temp = d.get("temp")
        if temp is not None:
            # Prefer the limits the drive itself publishes. NVMe media is rated
            # much hotter than a hard disk, so the global spinning-disk defaults
            # would flag a perfectly healthy SSD as warm.
            t_warn, t_crit = temp_limits(d)
            if temp >= t_crit:
                reasons.append({"level": "crit", "text": f"{tag}: {temp} °C (critical)"})
                bump("crit")
            elif temp >= t_warn:
                reasons.append({"level": "warn", "text": f"{tag}: {temp} °C (warm)"})
                bump("warn")
        for attr, txt, sev in (
                ("realloc", "reallocated sectors", "warn"),
                ("pending", "pending sectors", "warn"),
                ("uncorrect", "uncorrectable sectors", "crit"),
                ("reported_unc", "reported uncorrectable errors", "warn"),
                ("spin_retry", "spin-retry events", "crit")):
            v = d.get(attr)
            if v:
                reasons.append({"level": sev, "text": f"{tag}: {v} {txt}"})
                bump(sev)
        if d.get("crc_err"):
            reasons.append({"level": "warn",
                            "text": f"{tag}: {d['crc_err']} UDMA CRC errors (cable/bridge)"})
            bump("warn")
        # filesystem fill only warns on the system volume; data disks are meant
        # to sit near-full on purpose
        if d.get("mount") == "/" and d.get("fs_pct") is not None and d["fs_pct"] >= 90:
            reasons.append({"level": "warn", "text": f"/: {d['fs_pct']} % full"})
            bump("warn")

    if system.get("mem_total"):
        used_pct = system["mem_used"] / system["mem_total"] * 100
        if used_pct >= 95:
            reasons.append({"level": "warn", "text": f"memory {round(used_pct)} % used"})
            bump("warn")
    if system.get("cpu_temp") and system["cpu_temp"] >= 85:
        reasons.append({"level": "warn", "text": f"CPU {system['cpu_temp']} °C"})
        bump("warn")

    return level, reasons


# ==========================================================================
# plex
# ==========================================================================

import urllib.request
import urllib.parse



def plex_token() -> Optional[str]:
    try:
        prefs = PLEX_PREFS.read_text()
    except OSError:
        return None
    m = re.search(r'PlexOnlineToken="([^"]+)"', prefs)
    return m.group(1) if m else None


def _api(path: str, token: str, timeout: float = 5.0, as_json: bool = True):
    sep = "&" if "?" in path else "?"
    url = f"{PLEX_URL}{path}{sep}X-Plex-Token={urllib.parse.quote(token)}"
    # Only ask for JSON when we actually want JSON. Sending this header on an
    # image request can make Plex return a JSON error instead of the poster.
    headers = {"Accept": "application/json"} if as_json else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except Exception as e:
        log(f"plex {path}: {e}", level="debug")
        return None
    if not as_json:
        return body
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def server_info(token: Optional[str]) -> dict:
    info = {"active": None, "version": None, "web_version": None}
    if not token:
        info["active"] = _plex_running()
        return info
    j = _api("/identity", token)
    if j:
        mc = j.get("MediaContainer", {})
        info["active"] = True
        info["version"] = mc.get("version")
    else:
        info["active"] = _plex_running()
    info["web_version"] = _web_version(token)
    return info


def _web_version(token: Optional[str]) -> Optional[str]:
    """
    Version of the served web client. Plex names its JS/CSS assets like
    '…-plex-4.160.0-<hash>.js', so the version is in the served HTML.
    """
    if not token:
        return None
    for path in ("/web/index.html", "/web/", "/web/index.htm"):
        body = _api(path, token, as_json=False)
        if not body:
            continue
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "ignore")
        m = re.search(r"-plex-(\d+\.\d+\.\d+)-[0-9a-f]+\.(?:js|css)", body, re.I)
        if m:
            return m.group(1)
        m = re.search(r'"?version"?\s*[:=]\s*["\']?(4\.\d+\.\d+)', body, re.I)
        if m:
            return m.group(1)
    return None


def _plex_running() -> bool:
    return bool(run(["pgrep", "-x", "Plex Media Server"]).strip())


def mount_list() -> list[str]:
    out = []
    try:
        for line in Path("/proc/self/mounts").read_text().splitlines():
            f = line.split()
            if len(f) >= 3 and f[2] in ("ext4", "xfs", "btrfs", "ext3", "zfs"):
                out.append(f[1].replace("\\040", " "))
    except OSError:
        pass
    out.sort(key=len, reverse=True)  # longest prefix wins
    return out


# ---- now playing ---------------------------------------------------------

def sessions(token: Optional[str], mounts: list[str]) -> list[dict]:
    if not token:
        return []
    j = _api("/status/sessions", token)
    items = (j or {}).get("MediaContainer", {}).get("Metadata", []) or []
    out = []
    for m in items:
        media = (m.get("Media") or [{}])[0]
        part = (media.get("Part") or [{}])[0]
        player = m.get("Player", {})
        sess = m.get("Session", {})
        user = m.get("User", {})

        # Plex only puts `decision` on streams during a transcode; on direct play
        # it is on the Part, and with neither it isn't converting anything.
        part_dec = part.get("decision") or media.get("decision")
        fallback = part_dec or (None if "TranscodeSession" in m else "directplay")

        streams = {"1": None, "2": None, "3": None}
        for s in part.get("Stream", []):
            t = str(s.get("streamType", ""))
            # membership test, not truthiness: the slots start out None
            if t not in streams or streams[t] is not None:
                continue
            if t != "1" and not s.get("selected"):
                continue
            streams[t] = {"title": s.get("displayTitle") or s.get("extendedDisplayTitle"),
                          "decision": s.get("decision") or fallback}

        file = part.get("file")
        vol = None
        if file:
            for mp in mounts:
                if mp != "/" and file.startswith(mp.rstrip("/") + "/"):
                    vol = mp
                    break
            if vol is None and file.startswith("/"):
                vol = "/"

        out.append({
            "type": m.get("type"), "title": m.get("title"),
            "show": m.get("grandparentTitle") or m.get("parentTitle"),
            "season": _int(m.get("parentIndex")), "episode": _int(m.get("index")),
            "year": _int(m.get("year")),
            "duration_ms": _int(m.get("duration")), "offset_ms": _int(m.get("viewOffset")) or 0,
            "state": player.get("state"), "user": user.get("title"),
            "product": player.get("product"), "player": player.get("title"),
            "address": player.get("address"), "local": bool(player.get("local")),
            "bandwidth": _int(sess.get("bandwidth")),
            "video": (streams["1"] or {}).get("title"),
            "video_dec": (streams["1"] or {}).get("decision"),
            "audio": (streams["2"] or {}).get("title"),
            "audio_dec": (streams["2"] or {}).get("decision"),
            "subs": (streams["3"] or {}).get("title"),
            "subs_dec": (streams["3"] or {}).get("decision"),
            "volume": vol,
            "art": m.get("grandparentThumb") or m.get("parentThumb") or m.get("thumb"),
        })
    return out


def cache_art(sessions_list: list[dict], token: Optional[str]) -> None:
    """Fetch each poster into the web dir so the token never reaches the browser."""
    keep = set()
    for s in sessions_list:
        key = s.get("art")
        if not key:
            continue
        name = "art-" + hashlib.md5(key.encode()).hexdigest()[:16] + ".jpg"
        path = WEB_DIR / name
        keep.add(name)
        if not path.is_file() and token:
            img = _api(key, token, as_json=False)
            if isinstance(img, (bytes, bytearray)) and len(img) > 200:
                write_bytes_atomic(path, bytes(img), mode=0o644)
        if path.is_file():
            s["thumb"] = name
    for old in WEB_DIR.glob("art-*.jpg"):
        if old.name not in keep:
            try:
                old.unlink()
            except OSError:
                pass


# ---- background activity -------------------------------------------------

def library_roots(token: Optional[str]) -> dict[str, str]:
    """Map a directory path to its library title, for labelling jobs."""
    roots = {}
    if not token:
        return roots
    j = _api("/library/sections", token)
    for d in (j or {}).get("MediaContainer", {}).get("Directory", []):
        title = d.get("title")
        for loc in d.get("Location", []):
            p = loc.get("path")
            if p and title:
                roots[p] = title
    return roots


def _classify(args: str, roots: dict[str, str]) -> Optional[dict]:
    low = args.lower()
    file = None
    m = re.search(r'-i\s+"?([^"]+?\.\w{2,4})"?(?:\s|$)', args)
    if m:
        file = m.group(1)
    library = None
    if file:
        for root, title in roots.items():
            if file.startswith(root):
                library = title
                break

    if "transcode/session/bif" in low or "indexes/bif" in low:
        kind, label, why = "bif", "Preview thumbnails", "generating scrub previews"
    elif "chapters" in low or "chapterimages" in low:
        kind, label, why = "chapter", "Chapter images", "extracting chapter thumbnails"
    elif "loudness" in low or "audio-analysis" in low:
        kind, label, why = "loudness", "Audio analysis", "measuring loudness"
    elif "transcode" in low:
        kind, label, why = "transcode", "Transcoding", "converting for a client"
    elif "eae" in low or "analysis" in low or "-analyze" in low:
        kind, label, why = "analysis", "Media analysis", "analysing a newly added file"
    elif "scanner" in low or "plex media scanner" in low:
        kind, label, why = "scan", "Library scan", "scanning for new media"
    else:
        return None

    return {"kind": kind, "label": label, "why": why,
            "file": Path(file).name if file else None,
            "dir": str(Path(file).parent) if file else None,
            "library": library}


def activity(roots: dict[str, str]) -> list[dict]:
    """What Plex is doing right now, from ps (reads /proc, no disk access)."""
    raw = run(["ps", "-eo", "etimes,pcpu,args", "--sort=-etimes"])
    jobs = []
    for line in raw.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        etimes, pcpu, args = parts
        if "Plex Transcoder" not in args and "Plex Media Scanner" not in args \
                and "EasyAudioEncoder" not in args:
            continue
        job = _classify(args, roots)
        if not job:
            continue
        try:
            job["runtime_s"] = int(etimes)
            job["cpu"] = float(pcpu)
        except ValueError:
            job["runtime_s"] = 0
            job["cpu"] = 0.0
        jobs.append(job)
    return jobs


# ---- performance ---------------------------------------------------------

def perf(token: Optional[str]) -> dict:
    """Latency of a few key endpoints, plus DB size. No disk access."""
    out = {"direct": {}, "proxy": None, "db": {}}
    if token:
        # Same probe as the proxy side (one reused connection), so the two sets
        # of medians are measured identically and their difference is meaningful.
        u = urllib.parse.urlparse(PLEX_URL)
        out["direct"] = _probe_endpoints(u.hostname or "127.0.0.1",
                                         u.port or 32400,
                                         (u.scheme == "https"), token)

    # Same endpoints through the reverse proxy — the delta against the direct
    # figures is nginx's per-request cost. Fills perf.proxy.overhead_ms (the
    # "Reverse proxy" card) and the srv.pxy history line, like the PHP version.
    if token and PERF_PROXY_URL and out["direct"]:
        via = _proxy_probe(PERF_PROXY_URL, token)
        if via:
            deltas = [v["median_ms"] - out["direct"][p]["median_ms"]
                      for p, v in via.items() if p in out["direct"]]
            out["proxy"] = {
                "url": PERF_PROXY_URL,
                "endpoints": via,
                "overhead_ms": round(sum(deltas) / len(deltas), 2) if deltas else None,
            }
        else:
            out["proxy"] = {"url": PERF_PROXY_URL, "endpoints": {},
                            "overhead_ms": None, "error": "unreachable"}
    db = PLEX_PREFS.parent / "Plug-in Support" / "Databases" / \
        "com.plexapp.plugins.library.db"
    try:
        out["db"]["bytes"] = db.stat().st_size
        wal = db.with_name(db.name + "-wal")
        out["db"]["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    except OSError:
        pass
    # Free-page ratio via Plex's own SQLite build (reads the DB on the SSD, never
    # a media disk). A high ratio means the DB wants a VACUUM.
    plexsql = "/usr/lib/plexmediaserver/Plex SQLite"
    if os.path.isfile(plexsql) and db.exists():
        try:
            free = run([plexsql, str(db), "PRAGMA freelist_count;"], timeout=8).strip()
            page = run([plexsql, str(db), "PRAGMA page_count;"], timeout=8).strip()
            if free.isdigit() and page.isdigit() and int(page) > 0:
                out["db"]["free_pct"] = round(int(free) / int(page) * 100, 1)
        except Exception:
            pass
    # stale database copies Plex leaves behind after its own maintenance
    try:
        import glob as _glob
        bk = _glob.glob(str(db.parent / "com.plexapp.plugins.library.db-20*"))
        if bk:
            out["db"]["backup_count"] = len(bk)
            out["db"]["backup_bytes"] = sum(os.path.getsize(b) for b in bk
                                            if os.path.isfile(b))
    except OSError:
        pass
    return out


def _probe_endpoints(host: str, port: int, tls: bool, token: str) -> dict:
    """
    Median latency per endpoint over ONE reused connection — the method the PHP
    collector used and the page describes ("median over one reused connection").

    Reuse matters: opening a fresh connection per sample charges every request a
    TCP (and over TLS, a handshake) that has nothing to do with the server's
    work. Measuring the proxy that way made nginx look ~5 ms slower than it is,
    because the direct probe went to localhost unencrypted while the proxy probe
    paid a full TLS handshake each time. With one connection held open, only the
    first sample carries setup cost and the median lands among the clean ones.
    """
    import http.client
    try:
        if tls:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(host, port, timeout=5, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=5)
    except Exception:
        return {}

    out: dict = {}
    try:
        for ep in PERF_ENDPOINTS:
            sep = "&" if "?" in ep else "?"
            path = f"{ep}{sep}X-Plex-Token={urllib.parse.quote(token)}"
            samples = []
            size = 0
            for _ in range(PERF_SAMPLES):
                t0 = time.time()
                try:
                    conn.request("GET", path, headers={
                        "Accept": "application/json",
                        "Connection": "keep-alive",
                        "User-Agent": "plexmon/2.0",
                    })
                    resp = conn.getresponse()
                    body = resp.read()          # must drain to reuse the socket
                except Exception:
                    break
                samples.append((time.time() - t0) * 1000)
                size = len(body)
            if samples:
                samples.sort()
                out[ep] = {"median_ms": round(samples[len(samples) // 2], 2),
                           "bytes": size}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _proxy_probe(base_url: str, token: str) -> dict:
    """Same endpoints through the reverse proxy, over one reused connection."""
    u = urllib.parse.urlparse(base_url)
    tls = (u.scheme or "https") == "https"
    host = u.hostname or ""
    port = u.port or (443 if tls else 80)
    if not host:
        return {}
    return _probe_endpoints(host, port, tls, token)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ==========================================================================
# history
# ==========================================================================

def load() -> list[dict]:
    # Be strict about the shape: an old history.json from the PHP collector, or
    # any malformed file, could be a dict — and .append() on that would crash the
    # daemon. Anything that isn't a list of samples starts fresh.
    data = read_json(HISTORY_FILE, [])
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict) and "t" in s]


def record(samples: list[dict], disks: list[dict], system: dict,
           perf: dict, plex: dict) -> list[dict]:
    """Append one sample and prune by age."""
    direct = perf.get("direct", {})
    sample = {
        "t": int(time.time()),
        "p": [
            direct.get("/identity", {}).get("median_ms"),
            direct.get("/library/sections", {}).get("median_ms"),
            direct.get("/hubs", {}).get("median_ms"),
        ],
        "s": [
            system.get("cpu_temp"),
            round(system.get("load", [0])[0], 2),
            (round(system["mem_used"] / system["mem_total"] * 100)
             if system.get("mem_total") else None),
        ],
        "x": [
            plex.get("sessions"),
            perf.get("db", {}).get("bytes"),
            perf.get("db", {}).get("wal_bytes"),
            perf.get("proxy", {}).get("overhead_ms") if perf.get("proxy") else None,
        ],
        "d": {},
    }
    for d in disks:
        key = d.get("hist_key") or d.get("serial") or d.get("dev")
        unit = 512000 if d.get("tran") == "nvme" else 512
        sample["d"][key] = [
            d.get("temp"),
            d.get("fs_pct"),
            d.get("fs_used_b"),
            # Only ever record counters we have just read from the drive. A
            # cached value is a real number but attached to the wrong moment: it
            # repeats unchanged while the disk sleeps, and then the next genuine
            # reading appears to jump by everything that accumulated meanwhile.
            # A rate computed across that pair blames hours of spin-ups on the
            # five minutes between two samples, which is where the absurd spikes
            # came from. Gaps while a disk sleeps are the honest answer: we did
            # not measure, so we do not claim.
            None if d.get("from_cache") else d.get("start_stop"),
            None if d.get("from_cache") else d.get("load_cycle"),
            None if d.get("from_cache") else (d.get("lba_written") or d.get("nvme_written")),
            None if d.get("from_cache") else (d.get("lba_read") or d.get("nvme_read")),
            None if d.get("from_cache") else d.get("crc_err"),
            None if d.get("from_cache") else (
                (d.get("realloc") or 0) + (d.get("pending") or 0)
                + (d.get("uncorrect") or 0) + (d.get("reported_unc") or 0)),
            unit,
        ]

    samples.append(sample)
    cutoff = time.time() - HISTORY_DAYS * 86400
    samples = [s for s in samples if s.get("t", 0) >= cutoff]
    if len(samples) > HISTORY_MAX:
        samples = samples[-HISTORY_MAX:]
    write_json(HISTORY_FILE, samples, mode=0o600)
    return samples


def series(samples: list[dict], disks: list[dict], points: int) -> dict:
    """Down-sample into the parallel arrays the page charts directly."""
    n = len(samples)
    if points and n > points:
        step = -(-n // points)  # ceil
        idx = list(range(0, n, step))
        if idx[-1] != n - 1:
            idx.append(n - 1)
        rows = [samples[i] for i in idx]
    else:
        rows = samples

    out = {
        "t": [s["t"] for s in rows],
        "perf": {"id": [], "ls": [], "hb": []},
        "sys": {"cpu": [], "load": [], "mem": []},
        "srv": {"sess": [], "db": [], "wal": [], "pxy": []},
        "disks": {},
    }
    for s in rows:
        out["perf"]["id"].append(s["p"][0])
        out["perf"]["ls"].append(s["p"][1])
        out["perf"]["hb"].append(s["p"][2])
        out["sys"]["cpu"].append(s["s"][0])
        out["sys"]["load"].append(s["s"][1])
        out["sys"]["mem"].append(s["s"][2])
        x = s.get("x", [None, None, None, None])
        out["srv"]["sess"].append(x[0])
        out["srv"]["db"].append(x[1])
        out["srv"]["wal"].append(x[2])
        out["srv"]["pxy"].append(x[3])

    labels = {}
    for d in disks:
        key = d.get("hist_key") or d.get("serial") or d.get("dev")
        labels[key] = {"label": d.get("mount") or d.get("dev"),
                       "dev": d.get("dev"), "tran": d.get("tran"),
                       "total_b": d.get("fs_size_b"),
                       "unit_b": 512000 if d.get("tran") == "nvme" else 512}
    for key, meta in labels.items():
        e = {**meta, "temp": [], "fs": [], "used": [], "ss": [], "lc": [],
             "lw": [], "lr": [], "crc": [], "errs": []}
        for s in rows:
            row = s.get("d", {}).get(key, [None] * 10)
            e["temp"].append(row[0]); e["fs"].append(row[1]); e["used"].append(row[2])
            e["ss"].append(row[3]); e["lc"].append(row[4]); e["lw"].append(row[5])
            e["lr"].append(row[6]); e["crc"].append(row[7]); e["errs"].append(row[8])
        out["disks"][key] = e
    return out


def spinups_window(samples: list[dict], key: str, seconds: int) -> Optional[int]:
    """
    Spin-ups within a trailing window, from the Start_Stop_Count history.

    Start_Stop_Count is only recorded on fresh SMART reads (a sleeping disk keeps
    its cached value out of the chart), so a window can easily contain no fresh
    reading at all. We therefore track the last known value at/before the window
    start and the last known value overall, and report their difference — which
    is 0 when the count hasn't moved, not "unknown". Only genuinely having no
    Start_Stop reading anywhere returns None.
    """
    cutoff = time.time() - seconds
    before = None          # last value at or before the window start
    win_first = None       # first value inside the window
    latest = None          # last value overall
    for s in samples:
        row = s.get("d", {}).get(key)
        if not row or len(row) < 4 or row[3] is None:
            continue
        v = int(row[3])
        latest = v
        if s.get("t", 0) <= cutoff:
            before = v
        elif win_first is None:
            win_first = v
    if latest is None:
        return None
    base = before if before is not None else win_first
    if base is None:
        base = latest
    d = latest - base
    return d if d >= 0 else None


# ==========================================================================
# diag
# ==========================================================================

import urllib.request
import urllib.error



G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; D = "\033[2m"; N = "\033[0m"


def _diag_api(path: str, timeout: float = 4.0):
    url = f"http://{API_HOST}:{API_PORT}{path}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
        return json.loads(body), (time.time() - t0) * 1000
    except Exception as e:
        return {"__error": str(e)}, (time.time() - t0) * 1000


def smart_age_note(d: dict) -> str:
    """
    How current a disk's SMART values are, for the CLI listings. Routine reads
    are throttled, so being served from cache is the normal state and says
    nothing about staleness — what matters is how old the numbers are.
    """
    age = d.get("cache_age")
    if d.get("smart_stale"):
        return f"  [SMART {age}s old]" if age else "  [SMART cached]"
    if d.get("from_cache") and age:
        return f"  [SMART {age}s ago]"
    return ""


def check() -> int:
    """Self-check: is every link in the chain working?"""
    ok = warn = bad = 0

    def p(status, name, detail=""):
        nonlocal ok, warn, bad
        col = {"ok": G, "warn": Y, "FAIL": R}[status]
        print(f"  {col}{status:5}{N} {name:32} {D}{detail}{N}")
        if status == "ok": ok += 1
        elif status == "warn": warn += 1
        else: bad += 1

    print(f"plexmon self-check   {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    print("Daemon")
    health, ms = _diag_api("/health")
    if "__error" in health:
        p("FAIL", "API reachable", health["__error"])
        print(f"\n  {R}the daemon isn't answering — systemctl status plex-status{N}")
        return 1
    p("ok", "API reachable", f"{ms:.0f} ms, up {health.get('uptime', 0)}s")

    print("\nData")
    data, _ = _diag_api("/data.json")
    if "__error" in data:
        p("FAIL", "data.json", data["__error"])
    else:
        age = int(time.time()) - data.get("generated", 0)
        (p("ok", "snapshot fresh", f"{age}s old, status={data.get('overall')}")
         if age < 900 else p("warn", "snapshot stale", f"{age}s old"))
        n_re = len(data.get("reasons", []))
        if n_re:
            p("warn", "status reasons", str(n_re))
            for r in data.get("reasons", []):
                print(f"        {D}- [{r['level']}] {r['text']}{N}")
        else:
            p("ok", "no warnings", "all checks passed")
        for note in _CONFIG_NOTES:
            p("warn", "config file", note[:60])

        bad = [d.get("dev") for d in data.get("disks", [])
               if d.get("spindown_mismatch")]
        if bad:
            p("warn", "spindown assumption",
              f"{', '.join(bad)} spun up unnoticed — lower spindown_after_s")
        else:
            p("ok", "spindown assumption", f"{SPINDOWN_AFTER_S}s, no mismatch seen")

        lsr = data.get("last_smart_read", 0)
        if lsr:
            p("ok", "SMART read on record", f"{int(time.time()) - lsr}s ago")
        else:
            p("warn", "no SMART read yet", "waking a disk or the daily sweep will populate it")

    print("\nLive panels")
    tok = TOKEN_FILE
    if tok.is_file():
        wu = web_user()
        readable = False
        try:
            st = tok.stat()
            # group-readable and owned by the web user's group, or world-readable
            readable = bool(st.st_mode & 0o040) or bool(st.st_mode & 0o004)
        except OSError:
            pass
        p("ok" if readable else "warn", f"API token present",
          f"the page reads it as {wu}" if readable else "check group/permissions")
    else:
        p("warn", "no API token yet", "created when the daemon starts")

    sess, _ = _diag_api("/sessions")
    if "__error" not in sess:
        p("ok", "sessions endpoint", f"{len(sess.get('sessions', []))} stream(s)")

    print("\nWake button")
    disks = data.get("disks", []) if "__error" not in data else []
    probes = 0
    missing = []
    for d in disks:
        mp = d.get("mount")
        if mp and mp != "/" and d.get("tran") != "nvme":
            if (Path(mp) / ".wake-probe").is_file():
                probes += 1
            else:
                missing.append(mp)
    if probes and not missing:
        p("ok", "probe files", f"{probes} volume(s)")
    elif probes:
        p("warn", "probe files", "missing on: " + " ".join(missing))
    else:
        p("warn", "no probe files", "a wake request or the daily sweep creates them")

    print("\nDisks")
    for d in disks:
        if d.get("tran") == "nvme":
            continue
        temp = f"{d['temp']} C" if d.get("temp") is not None else "  -"
        cached = smart_age_note(d)
        print(f"        {d.get('dev'):8} {str(d.get('mount')):10} {temp:>6}{cached}"
              f"   spin-ups: {d.get('spinups_1h', '-')} / h, {d.get('spinups_24h', '-')} / 24h")

    print()
    if bad:
        print(f"{R}{bad} problem(s){N}, {warn} warning(s), {ok} ok")
        return 1
    if warn:
        print(f"{Y}{warn} warning(s){N}, {ok} ok — nothing broken")
        return 0
    print(f"{G}all {ok} checks passed{N}")
    return 0


def diag() -> int:
    """Latency-focused probe of the API and the disks' reported state."""
    print(f"plexmon diagnostics   {time.strftime('%H:%M:%S')}\n")
    for ep in ("/health", "/data.json", "/sessions", "/history-full.json", "/wakecheck"):
        res, ms = _diag_api(ep, timeout=6)
        status = f"{R}error{N}" if "__error" in res else f"{G}ok{N}"
        extra = res.get("__error", "") if "__error" in res else ""
        print(f"  {ep:22} {ms:7.1f} ms  {status}  {D}{extra}{N}")

    print(f"\n  effective settings (what is ACTUALLY in force):")
    print(f"    spindown_after_s      {SPINDOWN_AFTER_S}")
    print(f"    smart_min_interval    {SMART_MIN_INTERVAL}")
    print(f"    bridge_wakes_siblings {BRIDGE_WAKES_SIBLINGS}")
    print(f"    fast/slow interval    {FAST_INTERVAL}s / {SLOW_INTERVAL}s")
    for note in _CONFIG_NOTES:
        print(f"    {R}config: {note}{N}")

    data, _ = _diag_api("/data.json")
    if "__error" not in data:
        print("\n  disk power state (inferred, no disk access):")
        for d in data.get("disks", []):
            print(f"    {d.get('dev'):8} {d.get('power', '?'):8} "
                  f"{D}{d.get('power_src', '')}{N}"
                  + smart_age_note(d))
    return 0


# ==========================================================================
# daemon
# ==========================================================================



class Daemon:
    def __init__(self) -> None:
        self.mon = DiskMonitor()
        self.lock = threading.Lock()          # guards only the published snapshot
        self.tick_lock = threading.Lock()     # serializes collections (fast loop vs wake)
        self.snapshot: dict = {}
        self.token = self._ensure_token()
        self._wake_deadline = 0.0             # forced-SMART requested until this time
        self._spinning_up = False             # a wake is currently spinning disks up
        self._wake_passes = 0                 # forced passes made in this wake window
        self._tick_started = 0.0              # when the running collection began
        self._stall_logged = 0.0              # last time a stall was reported
        self._last_slow = 0.0
        self._last_daily = 0.0
        self._plex_token = None
        self._roots: dict = {}
        self._roots_at = 0.0

    # -- API token: written where only the web user can read it -------------
    def _ensure_token(self) -> str:
        existing = None
        try:
            existing = TOKEN_FILE.read_text().strip()
        except OSError:
            pass
        tok = existing or secrets.token_urlsafe(32)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            TOKEN_FILE.write_text(tok)
            TOKEN_FILE.chmod(0o640)
            wu = web_user()
            uid = uid_of(wu)
            if uid is not None:
                try:
                    os.chown(TOKEN_FILE, 0, os.stat(TOKEN_FILE).st_gid)
                    # group-owned by the web user's group so 0640 lets it read
                    try:
                        gid = grp.getgrnam(wu).gr_gid
                        os.chown(TOKEN_FILE, 0, gid)
                    except KeyError:
                        os.chown(TOKEN_FILE, 0, uid)
                except PermissionError:
                    pass
            log(f"API token ready for the web page (readable by {wu})")
        except OSError as e:
            log(f"could not write token file: {e}")
        return tok

    # -- Plex token, refreshed occasionally --------------------------------
    def _get_plex_token(self) -> str | None:
        if self._plex_token is None:
            self._plex_token = plex_token()
        return self._plex_token

    def _get_roots(self, tok) -> dict:
        if time.time() - self._roots_at > 3600:
            self._roots = library_roots(tok)
            self._roots_at = time.time()
        return self._roots

    # ---- the loops -------------------------------------------------------
    def run(self) -> None:
        api = threading.Thread(target=self._serve_api, daemon=True)
        api.start()
        for note in _CONFIG_NOTES:
            log(f"config: {note}")
        log(f"plexmon started; API on http://{API_HOST}:{API_PORT}")
        # prime a snapshot immediately so the page isn't empty on first load
        self._tick(force_smart=False)
        while True:
            start = time.time()
            try:
                self._tick(force_smart=False)
            except Exception as e:
                log(f"tick error: {e}")
            elapsed = time.time() - start
            time.sleep(max(1.0, FAST_INTERVAL - elapsed))

    def _tick(self, *, force_smart: bool, wait: bool = False,
              refresh: bool = False) -> None:
        # Only one collection at a time. The fast loop and a wake-triggered tick
        # both call this; running two smartctl sweeps at once would race on the
        # cache. A routine tick skips if one is already running (the running one
        # will publish fresh data anyway); a forced tick, or one explicitly asked
        # to wait, blocks for its turn. `wait` exists for the wake's status
        # publish: it reads no SMART, so it's cheap, but it must not be dropped
        # or the dashboard keeps showing the pre-wake state.
        if force_smart or wait:
            self.tick_lock.acquire()
        else:
            if not self.tick_lock.acquire(blocking=False):
                # Normal for a moment. But if a collection drags on, nothing is
                # being recorded — say so, rather than going quiet the way an
                # eight-hour stall once did until someone looked at the charts.
                started = self._tick_started
                if started and time.time() - started > 300 \
                        and time.time() - self._stall_logged > 300:
                    self._stall_logged = time.time()
                    log(f"a collection has been running for "
                        f"{int(time.time() - started)}s — polls are being "
                        f"skipped and no history is being recorded")
                return
        self._tick_started = time.time()
        try:
            self._tick_locked(force_smart=force_smart, refresh=refresh)
        finally:
            took = time.time() - self._tick_started
            self._tick_started = 0.0
            self.tick_lock.release()
            if took > 60:
                log(f"collection took {int(took)}s")

    def _tick_locked(self, *, force_smart: bool, refresh: bool = False) -> None:
        now = time.time()
        tok = self._get_plex_token()

        # daily guaranteed SMART sweep
        do_daily = False
        lt = time.localtime(now)
        if lt.tm_hour == SMART_DAILY_AT and now - self._last_daily > 3600:
            do_daily = True
            self._last_daily = now

        force = force_smart or do_daily or now < self._wake_deadline

        # Everything below runs WITHOUT the lock. Collecting SMART from disks that
        # are spinning up can take tens of seconds, and holding the lock across it
        # would stall every API read (/data.json would time out — which is exactly
        # what happened, and the browser closing the connection then showed up as
        # BrokenPipe). We build a fresh snapshot here and only take the lock for
        # the instant it takes to publish it; a reference assignment is atomic, so
        # readers always see a complete snapshot, never a half-built one.
        # While a wake is spinning the disks up, don't read SMART at all. The
        # O_DIRECT probe moves the kernel I/O counters, so the disks look active
        # to the normal inference and a routine tick would happily fire smartctl
        # at a drive that is still coming up to speed — which fails, falls back
        # to cache, and holds the collection lock while it does. The wake worker
        # runs the real SMART pass the moment every drive is ready.
        read_smart = not (self._spinning_up and not force_smart)
        # Is this the pass that records a history sample? Decide before
        # collecting, so the SMART read can be aligned with it.
        do_slow = force or (now - self._last_slow >= SLOW_INTERVAL)
        # `refresh` bypasses the read throttle but not the sleep rule: disks that
        # are already turning get re-read now, sleeping ones are left alone.
        disks = self.mon.collect(force=force, read_smart=read_smart,
                                 align_smart=do_slow or refresh)

        # While a wake is spinning the platters up, say so. Without this the
        # disks keep reporting the standby they were in when the request came
        # in — for the 10–30 s a five-disk spin-up takes, the dashboard and
        # --diag look like nothing happened at all.
        if self._spinning_up:
            for d in disks:
                if d.get("tran") != "nvme" and d.get("mount") not in (None, "/"):
                    d["power"] = "spinning"
                    d["power_src"] = "wake in progress — spinning up"
        if force:
            self.mon.create_probes(disks)
            n = self.mon.mark_smart_read(disks)
            if n:
                log(f"SMART read from {n} disk(s)"
                    + (" (daily)" if do_daily else ""), level="debug")
                # The wake window keeps the fast loop retrying SMART. Clear it
                # when every spinning disk has answered — clearing on the first
                # partial success (e.g. the one disk already awake for playback)
                # used to leave the rest cached until a second click.
                usb = [d for d in disks if d.get("tran") != "nvme"]
                self._wake_passes += 1
                if usb and all(not d.get("from_cache") for d in usb):
                    self._wake_deadline = 0.0
                    self._wake_passes = 0
                elif self._wake_passes >= 3:
                    # A disk that still won't answer after three passes isn't
                    # going to: stop, rather than spinning every other disk up
                    # once per tick for the rest of the window.
                    missing = [d.get("dev") for d in usb if d.get("from_cache")]
                    log(f"giving up on SMART for {', '.join(missing)} after "
                        f"{self._wake_passes} passes")
                    self._wake_deadline = 0.0
                    self._wake_passes = 0

        system = read_system()
        np = sessions(tok, mount_list())
        cache_art(np, tok)
        acts = activity(self._get_roots(tok))
        srv = server_info(tok)
        srv["sessions"] = len(np)
        overall, reasons = assess(disks, system)

        # A forced pass is a full collector run: refresh perf/DB too and record a
        # history sample, exactly like the PHP collector did on its wake pass.
        # This also lands a fresh Start_Stop reading in the history right away,
        # which is what the per-hour/day spin-up figures are computed from.
        # (do_slow was decided before the collection, so the SMART read could
        # be aligned with the history sample)
        if do_slow:
            self._last_slow = now
            pf = perf(tok)
            self._samples = record(
                getattr(self, "_samples", load()),
                disks, system, pf, srv)
            full = series(self._samples, disks, 0)
            write_json(HISTORY_WEB, full, mode=0o644)
            self._perf = pf
            self._hist_disks = disks
        pf = getattr(self, "_perf", {"direct": {}, "proxy": None, "db": {}})
        samples = getattr(self, "_samples", load())

        hist = series(samples, disks, HISTORY_POINTS)
        new_snapshot = self._build_snapshot(
            disks, system, np, acts, srv, pf, overall, reasons, hist, samples)

        # publish atomically — the only thing the lock protects
        with self.lock:
            self.snapshot = new_snapshot
        write_json(DATA_FILE, new_snapshot, mode=0o644)
        write_json(SESSIONS_WEB,
                   {"generated": int(now), "sessions": np}, mode=0o644)

    def _build_snapshot(self, disks, system, np, acts, srv, pf,
                        overall, reasons, hist, samples) -> dict:
        for d in disks:
            key = d.get("hist_key") or d.get("serial") or d.get("dev")
            d["spinups_1h"] = spinups_window(samples, key, 3600)
            d["spinups_24h"] = spinups_window(samples, key, 86400)
        return {
            "generated": int(time.time()),
            "hostname": os.uname().nodename,
            "overall": overall,
            "reasons": reasons,
            "thresholds": {"temp_warn": TEMP_WARN, "temp_crit": TEMP_CRIT},
            "last_smart_read": self.mon.last_smart_read,
            "system": system,
            "plex": srv,
            "now_playing": np,
            "activity": acts,
            "perf": pf,
            "history": hist,
            "disks": disks,
        }

    # ---- wake handling ---------------------------------------------------
    def _poster_bytes(self, key: str) -> Optional[bytes]:
        """
        Poster image bytes for a Plex metadata key. Serves the cached file if the
        collector already fetched it, otherwise fetches live from Plex (and drops
        a cached copy for next time). Returns None if there's no image.
        """
        import hashlib as _hl
        name = "art-" + _hl.md5(key.encode()).hexdigest()[:16] + ".jpg"
        path = WEB_DIR / name
        try:
            if path.is_file() and path.stat().st_size > 200:
                return path.read_bytes()
        except OSError:
            pass
        tok = self._get_plex_token()
        if not tok:
            return None
        img = _api(key, tok, as_json=False)
        if isinstance(img, (bytes, bytearray)) and len(img) > 200:
            write_bytes_atomic(path, bytes(img), mode=0o644)
            return bytes(img)
        return None

    def request_wake(self) -> dict:
        """Read the probe files (spins the disks up) and force a SMART pass."""
        with self.lock:
            snap_disks = self.snapshot.get("disks", [])
        # is_file() on a data-disk mount can stall if the disk is spinning up, so
        # do it outside the lock — otherwise a wake request would hold the lock
        # exactly when the API needs it most.
        mounts = []
        for d in snap_disks:
            mp = d.get("mount")
            if mp and mp != "/" and d.get("tran") != "nvme":
                # No is_file() filter here: on a fresh install the probes don't
                # exist yet (a forced pass creates them), and filtering on them
                # meant the first wake had nothing to spin up. The worker creates
                # a missing probe itself — the write wakes the disk just as well.
                mounts.append((mp, d.get("dev"), d.get("path")))

        def worker():
            # Spin every disk up FIRST and wait for it: the O_DIRECT probe read
            # blocks until the platters turn, so by the time we run the SMART
            # pass the drives can actually answer. Firing smartctl in parallel
            # with the spin-up (the previous behaviour) meant the first click
            # only got SMART from disks that happened to be awake already — the
            # rest failed mid-spin-up, were served from cache, and looked like
            # they needed a second click.
            def spin(mp, dev_path):
                probe = Path(mp) / ".wake-probe"
                try:
                    if not probe.is_file():
                        probe.write_bytes(b"\0" * 65536)
                        probe.chmod(0o644)
                except OSError:
                    pass
                self._probe_read(probe, dev_path)

            self._spinning_up = True
            # Publish the spinning state right away — the fast loop would
            # otherwise leave the page showing the old standby for up to a full
            # interval, which reads as "the button did nothing".
            threading.Thread(target=lambda: self._tick(force_smart=False, wait=True),
                             daemon=True).start()
            try:
                threads = []
                for mp, _dev, dev_path in mounts:
                    t = threading.Thread(target=spin, args=(mp, dev_path), daemon=True)
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join(timeout=25)      # a 3.5" drive spins up in ~10 s
            finally:
                self._spinning_up = False
            # Only now open the forced-SMART window: opening it up front let the
            # fast loop fire smartctl at disks that were still spinning up, which
            # is exactly the failure this worker exists to avoid.
            self._wake_deadline = time.time() + 120
            self._wake_passes = 0
            self._tick(force_smart=True)

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "at": int(time.time()),
                "disks": [d for _, d, _p in mounts],
                # the daemon always re-reads SMART itself (it runs as root), so the
                # page can count on fresh values — no sudo rule involved anymore
                "refresh": True}

    @staticmethod
    def _probe_read(path: Path, dev_path: Optional[str] = None) -> None:
        """
        Force a physical read so a sleeping drive spins up, and block until it
        has. O_DIRECT is what makes this work: it bypasses the page cache, so the
        read cannot be answered from RAM and must reach the platters.

        The buffer alignment matters and is easy to get wrong. O_DIRECT requires
        a memory-aligned buffer; os.pread() allocates an ordinary bytes object,
        which the kernel rejects with EINVAL. That failure used to fall through
        to a buffered read — served straight from the page cache, waking nothing
        — so the probe silently did nothing at all and the disks were only ever
        spun up as a side effect of smartctl. mmap gives us a page-aligned
        buffer, and os.preadv() reads into it.

        Prefers the raw block device (a read there cannot be short-circuited by
        the filesystem) and falls back to the probe file on the mount.
        """
        import mmap

        def direct_read(target: str) -> bool:
            try:
                fd = os.open(target, os.O_RDONLY | os.O_DIRECT)
            except OSError:
                return False
            try:
                buf = mmap.mmap(-1, 4096)          # page-aligned by definition
                try:
                    os.preadv(fd, [buf], 0)
                    return True
                finally:
                    buf.close()
            except OSError:
                return False
            finally:
                os.close(fd)

        if dev_path and direct_read(dev_path):
            return
        if direct_read(str(path)):
            return
        # Last resort: a buffered read may be served from cache and wake nothing,
        # but it is better than giving up entirely.
        try:
            with open(path, "rb") as f:
                f.read(4096)
        except OSError:
            pass

    def wakecheck(self) -> dict:
        """
        Report which disks are spun up — from the I/O-counter state the daemon
        already samples, never by touching the disk. A probe read here would take
        seconds on a sleeping disk (holding the request open) and, worse, would
        spin it up: exactly what a *check* must not do. The fast loop refreshes
        this every FAST_INTERVAL, so it's already current.
        """
        with self.lock:
            disks = self.snapshot.get("disks", [])
        result = {}
        for d in disks:
            mp = d.get("mount")
            if not mp or mp == "/" or d.get("tran") == "nvme":
                continue
            # a disk we read SMART from this cycle, or that shows recent I/O, is up
            result[d.get("dev")] = (not d.get("from_cache", True)) or d.get("power") == "active"
        awake = sum(1 for v in result.values() if v)
        return {"ok": True, "disks": result, "awake": awake, "total": len(result)}

    # ---- HTTP API --------------------------------------------------------
    def _serve_api(self) -> None:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, ctype="application/json"):
                body = (json.dumps(obj).encode() if ctype == "application/json"
                        else obj)
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # the browser closed the connection before we finished (a poll
                    # that got superseded, a page reload) — harmless, not an error
                    pass

            def handle_one_request(self):
                # same swallow at the request level, in case the disconnect lands
                # between reading the request line and our handler
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def _authed(self) -> bool:
                sent = self.headers.get("X-Plexmon-Token", "")
                return secrets.compare_digest(sent, daemon.token)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/data.json":
                    # Grab the reference under the lock, then send OUTSIDE it. The
                    # snapshot is only ever replaced, never mutated in place, so
                    # the reference is a consistent view. Sending under the lock
                    # was the bug: a slow or half-closed client made wfile.write
                    # block while holding the lock, stalling every other reader
                    # (that's why /data.json and /wakecheck timed out during wake).
                    with daemon.lock:
                        snap = daemon.snapshot
                    self._send(200, snap)
                elif path == "/history-full.json":
                    self._send(200, read_json(HISTORY_WEB, {}))
                elif path == "/sessions":
                    # Query Plex live rather than returning the last tick's copy:
                    # a session poll is cheap (Plex API only, no disk access) and
                    # this is what keeps Now Playing responsive instead of lagging
                    # up to a full fast-loop interval behind.
                    tok = daemon._get_plex_token()
                    try:
                        np = sessions(tok, mount_list())
                        cache_art(np, tok)
                        acts = activity(daemon._get_roots(tok))
                        self._send(200, {"generated": int(time.time()),
                                         "sessions": np, "activity": acts})
                    except Exception:
                        # fall back to the snapshot if Plex is momentarily unreachable
                        with daemon.lock:
                            snap = daemon.snapshot
                        self._send(200, {"generated": snap.get("generated"),
                                         "sessions": snap.get("now_playing", []),
                                         "activity": snap.get("activity", [])})
                elif path == "/wakecheck":
                    self._send(200, daemon.wakecheck())
                elif path == "/art":
                    # Serve a poster directly: fetch from Plex if not cached yet.
                    # This makes Now Playing artwork appear immediately instead of
                    # waiting for the next collection to write the file.
                    from urllib.parse import parse_qs
                    q = parse_qs(urlparse(self.path).query)
                    key = (q.get("key") or [""])[0]
                    if not key.startswith("/library/"):
                        self._send(400, {"error": "bad key"})
                    else:
                        img = daemon._poster_bytes(key)
                        if img:
                            self._send(200, img, ctype="image/jpeg")
                        else:
                            self._send(404, {"error": "no poster"})
                elif path == "/health":
                    self._send(200, {"ok": True, "uptime": int(time.time() - START)})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                path = urlparse(self.path).path
                if not self._authed():
                    self._send(403, {"ok": False, "error": "bad or missing token"})
                    return
                if path == "/wake":
                    self._send(200, daemon.request_wake())
                elif path == "/smart":
                    # Refresh, not wake: re-read SMART from every disk that is
                    # already spinning, and leave the sleeping ones asleep. Use
                    # /wake when you want them spun up first.
                    threading.Thread(
                        target=lambda: daemon._tick(force_smart=False, refresh=True),
                        daemon=True).start()
                    with daemon.lock:
                        awake = [d.get("dev") for d in daemon.snapshot.get("disks", [])
                                 if d.get("power") == "active"]
                    self._send(200, {"ok": True, "refreshing": awake})
                else:
                    self._send(404, {"error": "not found"})

        global START
        START = time.time()
        ThreadingHTTPServer.allow_reuse_address = True
        try:
            srv = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
        except OSError as e:
            # No point running without the API — the whole daemon is only useful
            # if the page can reach it. Exit so systemd restarts us cleanly once
            # whatever holds the port is gone.
            log(f"cannot bind {API_HOST}:{API_PORT}: {e} — is another plexmon "
                f"already running? exiting")
            os._exit(1)
        srv.serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plex status monitoring daemon")
    ap.add_argument("-v", "--debug", action="store_true")
    ap.add_argument("--oneshot", action="store_true",
                    help="run a single collection and exit (no daemon, for testing)")
    ap.add_argument("--wake", action="store_true",
                    help="tell the running daemon to wake the disks and refresh SMART "
                         "(or, with --oneshot, force a SMART read in the one-off run)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-read SMART from the disks that are already spinning "
                         "(sleeping disks are left alone — use --wake for those)")
    ap.add_argument("--check", action="store_true",
                    help="self-check the whole chain against a running daemon")
    ap.add_argument("--diag", action="store_true",
                    help="probe the API and report latency + disk state")
    args = ap.parse_args(argv)
    set_debug(args.debug)

    if args.check:
        return check()
    if args.diag:
        return diag()

    if args.refresh:
        try:
            tok = TOKEN_FILE.read_text().strip()
        except OSError:
            print("no API token — is the daemon running?")
            return 1
        req = urllib.request.Request(
            f"http://{API_HOST}:{API_PORT}/smart", method="POST", data=b"")
        req.add_header("X-Plexmon-Token", tok)
        try:
            print(urllib.request.urlopen(req, timeout=8).read().decode())
            return 0
        except Exception as e:
            print(f"could not reach the daemon: {e}")
            return 1

    # Standalone --wake (without --oneshot): ask the running daemon to wake the
    # disks over its API, using the token, rather than starting a second daemon.
    if args.wake and not args.oneshot:
        try:
            tok = TOKEN_FILE.read_text().strip()
        except OSError:
            print("no API token — is the daemon running?")
            return 1
        req = urllib.request.Request(
            f"http://{API_HOST}:{API_PORT}/wake", method="POST", data=b"")
        req.add_header("X-Plexmon-Token", tok)
        try:
            body = urllib.request.urlopen(req, timeout=8).read()
            print(body.decode())
            return 0
        except Exception as e:
            print(f"could not reach the daemon: {e}")
            return 1

    d = Daemon()
    if args.oneshot:
        d._tick(force_smart=args.wake)
        with d.lock:
            print(json.dumps({"overall": d.snapshot.get("overall"),
                              "disks": len(d.snapshot.get("disks", [])),
                              "sessions": d.snapshot.get("plex", {}).get("sessions"),
                              "last_smart_read": d.snapshot.get("last_smart_read")}))
        return 0
    try:
        d.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
