#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NPS Log Analyzer
================

Parses Microsoft Network Policy Server (NPS / IAS) log files and opens an
interactive dashboard in an embedded browser window (pywebview).

Built for large log sets (millions of events):
  * parallel parsing on all CPU cores (files are split into chunks)
  * compact columnar storage (dictionary encoded, gzip compressed for the UI)
  * raw attributes are read from the log file on demand (click on an event)

Supported log formats (auto-detected):
  * DTS-compliant XML      <Event><Timestamp ...>...</Event>
  * Database-compatible    "NPS01","IAS",11/24/2026,13:08:00,1,"user",...
  * Legacy IAS format      10.0.0.1,user,11/24/2026,13:08:00,IAS,NPS01,4136,1,...

Working files are written to ".temp" in the current directory and removed on exit.

Usage examples:
  python nps_log_analyzer.py                      (all *.log in the current folder)
  python nps_log_analyzer.py C:\\Windows\\System32\\LogFiles
  python nps_log_analyzer.py IN2610.log IN2611.log
  python nps_log_analyzer.py D:\\nps-logs -r --pattern "IN*.log" --since 2026-11-01
  python nps_log_analyzer.py D:\\nps-logs --export report.html --no-gui

Requirements:
  pip install pywebview colorama
"""

from __future__ import annotations

import argparse
import atexit
import base64
import csv
import fnmatch
import gzip
import html
import json
import operator
import os
import re
import shutil
import sys
import time
import webbrowser
from array import array
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Optional dependencies                                                      #
# --------------------------------------------------------------------------- #
try:
    from colorama import Fore, Style, init as colorama_init

    colorama_init()
except ImportError:  # pragma: no cover - graceful fallback without colors
    class _NoColor:
        def __getattr__(self, _name):
            return ""

    Fore = Style = _NoColor()  # type: ignore

try:
    import webview  # pywebview

    HAS_WEBVIEW = True
except ImportError:
    webview = None
    HAS_WEBVIEW = False

APP_NAME = "NPS Log Analyzer"
APP_VERSION = "2.0.0"
EPOCH = datetime(1970, 1, 1)
CHUNK_BYTES = 48 * 1024 * 1024        # parallel work unit
RAW_EMBED_LIMIT = 20000               # embed raw attributes in exports up to this many events

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# --------------------------------------------------------------------------- #
#  Lookup tables                                                              #
# --------------------------------------------------------------------------- #
PACKET_TYPES = {
    1: "Access-Request",
    2: "Access-Accept",
    3: "Access-Reject",
    4: "Accounting-Request",
    5: "Accounting-Response",
    11: "Access-Challenge",
    40: "Disconnect-Request",
    41: "Disconnect-ACK",
    42: "Disconnect-NAK",
    43: "CoA-Request",
    44: "CoA-ACK",
    45: "CoA-NAK",
}

# Source: Microsoft "NPS Reason Codes" reference
REASON_CODES = {
    0: "Success - request authenticated and authorized",
    1: "Internal NPS server error",
    2: "Insufficient access rights to process the request",
    3: "Malformed RADIUS Access-Request message",
    4: "Global catalog (AD DS) unavailable",
    5: "Cannot connect to a domain controller",
    6: "NPS server unavailable (memory or database failure)",
    7: "Domain in User-Name does not exist",
    8: "User account does not exist",
    9: "Discarded by an IAS extension DLL",
    10: "IAS extension DLL failed",
    16: "Authentication failed - bad user name or password",
    17: "Password change failed",
    18: "Authentication method not supported by NPS",
    19: "CHAP used without reversible password encryption",
    20: "LAN Manager authentication is not supported",
    21: "Rejected by an IAS extension DLL",
    22: "Could not negotiate an EAP type",
    23: "EAP processing error (check EAP logs)",
    32: "Workgroup NPS cannot access domain accounts",
    33: "User must change password",
    34: "User account is disabled",
    35: "User account is expired",
    36: "User account is locked out",
    37: "Outside of permitted logon hours",
    38: "Account restriction",
    48: "No matching network policy",
    49: "No matching connection request policy",
    64: "Dial-in account locked out",
    65: "Dial-in Network Access Permission set to Deny",
    66: "Authentication method mismatch or guest access disabled",
    67: "Calling-Station-ID does not match Verify Caller ID",
    68: "Denied by day and time restrictions",
    69: "Called-Station-ID does not match policy constraint",
    70: "NAS-Port-Type does not match policy constraint",
    72: "Password expired, change not permitted",
    73: "Certificate lacks Client Authentication purpose (EKU)",
    80: "Failed to write accounting data",
    96: "EAP session timed out",
    97: "Inappropriate RADIUS message for secure authentication",
    112: "Remote RADIUS server rejected the request",
    113: "Remote RADIUS server group does not exist",
    115: "Proxy did not forward non-request message",
    116: "Proxy socket error while forwarding",
    117: "Remote RADIUS server did not respond",
    118: "Malformed message from remote RADIUS server",
    256: "Certificate has been revoked",
    257: "Cannot check certificate revocation (missing DLL)",
    258: "Certificate revocation list inaccessible",
    259: "CA unavailable for revocation check",
    260: "EAP or RADIUS message was altered",
    261: "Cannot contact AD DS or local account database",
    262: "Incomplete RADIUS message, signature not verified",
    263: "Incomplete user or computer credentials",
    264: "System clocks are not synchronized",
    265: "Certificate chains to an untrusted root CA",
    266: "Unexpected or malformed message",
    267: "Certificate lacks Client Authentication purpose",
    268: "Client certificate has expired",
    269: "No common algorithm between client and server",
    270: "Smart card logon required",
    271: "Server is shutting down or restarting",
    272: "Certificate maps to multiple accounts",
    273: "Trust provider not recognized",
    274: "Trust provider does not support the action",
    275: "Trust provider does not support the form",
    276: "EAP binary cannot be verified",
    277: "EAP binary is unsigned",
    278: "Certificate has expired",
    279: "Certificate chain validity periods do not match",
    280: "Certificate not issued by a valid CA",
    281: "Certificate chain path length exceeded",
    282: "Certificate has an unrecognized critical extension",
    283: "Certificate issuer does not match chain parent",
    285: "Certificate revocation list cannot be located",
    292: "User-Name does not match certificate CN",
    297: "Certificate does not have a valid name",
}

AUTH_TYPES = {
    1: "PAP",
    2: "CHAP",
    3: "MS-CHAP",
    4: "MS-CHAPv2",
    5: "EAP",
    6: "ARAP",
    7: "Unauthenticated",
    8: "Extension",
    9: "MS-CHAP CPW",
    10: "MS-CHAPv2 CPW",
    11: "PEAP",
}

ACCT_STATUS = {
    1: "Start",
    2: "Stop",
    3: "Interim-Update",
    7: "Accounting-On",
    8: "Accounting-Off",
}

NAS_PORT_TYPES = {
    0: "Async",
    1: "Sync",
    2: "ISDN Sync",
    5: "Virtual (VPN)",
    15: "Ethernet",
    19: "Wireless 802.11",
    20: "Wireless Other",
}

# Legacy IAS format: attribute ID -> canonical name
IAS_ATTRS = {
    1: "User-Name",
    4: "NAS-IP-Address",
    5: "NAS-Port",
    6: "Service-Type",
    7: "Framed-Protocol",
    8: "Framed-IP-Address",
    25: "Class",
    26: "Vendor-Specific",
    27: "Session-Timeout",
    28: "Idle-Timeout",
    30: "Called-Station-Id",
    31: "Calling-Station-Id",
    32: "NAS-Identifier",
    40: "Acct-Status-Type",
    41: "Acct-Delay-Time",
    42: "Acct-Input-Octets",
    43: "Acct-Output-Octets",
    44: "Acct-Session-Id",
    45: "Acct-Authentic",
    46: "Acct-Session-Time",
    47: "Acct-Input-Packets",
    48: "Acct-Output-Packets",
    49: "Acct-Terminate-Cause",
    55: "Event-Timestamp",
    61: "NAS-Port-Type",
    64: "Tunnel-Type",
    65: "Tunnel-Medium-Type",
    81: "Tunnel-Pvt-Group-ID",
    4108: "Client-IP-Address",
    4116: "Client-Vendor",
    4127: "Authentication-Type",
    4128: "Client-Friendly-Name",
    4129: "SAM-Account-Name",
    4130: "Fully-Qualified-User-Name",
    4132: "EAP-Friendly-Name",
    4136: "Packet-Type",
    4142: "Reason-Code",
    4149: "NP-Policy-Name",
    4154: "Proxy-Policy-Name",
    4155: "Provider-Type",
    4156: "Provider-Name",
    4157: "Remote-Server-Address",
}

# Column order of the "database-compatible" (ODBC) log format
DB_COLUMNS = [
    "Computer-Name", "Service-Name", "Record-Date", "Record-Time", "Packet-Type",
    "User-Name", "Fully-Qualified-Distinguished-Name", "Called-Station-Id",
    "Calling-Station-Id", "Callback-Number", "Framed-IP-Address", "NAS-Identifier",
    "NAS-IP-Address", "NAS-Port", "Client-Vendor", "Client-IP-Address",
    "Client-Friendly-Name", "Event-Timestamp", "Port-Limit", "NAS-Port-Type",
    "Connect-Info", "Framed-Protocol", "Service-Type", "Authentication-Type",
    "NP-Policy-Name", "Reason-Code", "Class", "Session-Timeout", "Idle-Timeout",
    "Termination-Action", "EAP-Friendly-Name", "Acct-Status-Type", "Acct-Delay-Time",
    "Acct-Input-Octets", "Acct-Output-Octets", "Acct-Session-Id", "Acct-Authentic",
    "Acct-Session-Time", "Acct-Input-Packets", "Acct-Output-Packets",
    "Acct-Terminate-Cause", "Acct-Multi-Ssn-ID", "Acct-Link-Count",
    "Acct-Interim-Interval", "Tunnel-Type", "Tunnel-Medium-Type", "Tunnel-Client-Endpt",
    "Tunnel-Server-Endpt", "Acct-Tunnel-Conn", "Tunnel-Pvt-Group-ID",
    "Tunnel-Assignment-ID", "Tunnel-Preference", "MS-Acct-Auth-Type", "MS-Acct-EAP-Type",
    "MS-RAS-Version", "MS-RAS-Vendor", "MS-CHAP-Error", "MS-CHAP-Domain",
    "MS-MPPE-Encryption-Types", "MS-MPPE-Encryption-Policy", "Proxy-Policy-Name",
    "Provider-Type", "Provider-Name", "Remote-Server-Address", "MS-RAS-Client-Name",
    "MS-RAS-Client-Version",
]

# Aliases (lower-case) -> canonical field used by the analyzer
FIELD_ALIASES = {
    "computer-name": "srv", "computername": "srv",
    "packet-type": "pt",
    "reason-code": "rc",
    "user-name": "user",
    "sam-account-name": "sam",
    "fully-qualifed-user-name": "fqun",  # sic - Microsoft typo in DTS format
    "fully-qualified-user-name": "fqun",
    "fully-qualified-distinguished-name": "fqun",
    "calling-station-id": "calling",
    "called-station-id": "called",
    "client-friendly-name": "client",
    "client-ip-address": "client_ip",
    "nas-ip-address": "nas_ip",
    "nas-identifier": "nas_id",
    "nas-port-type": "port_type",
    "np-policy-name": "policy", "policy-name": "policy",
    "proxy-policy-name": "crp",
    "authentication-type": "auth",
    "eap-friendly-name": "eap",
    "acct-status-type": "acct",
    "acct-session-id": "sid",
    "framed-ip-address": "framed",
    "timestamp": "ts",
}

# Columns shipped to the UI (columnar to keep the payload small)
COLUMNS = [
    "t", "out", "pt", "rc", "srv", "user", "calling", "called", "client",
    "client_ip", "nas_ip", "nas_id", "policy", "crp", "auth", "eap", "acct",
    "sid", "framed", "port_type", "file",
]


# Columns kept for every event (all dictionary encoded)
STR_COLS = ["out", "pt", "rc", "srv", "user", "calling", "called", "client", "client_ip",
            "nas_ip", "nas_id", "policy", "crp", "auth", "eap", "acct", "port_type", "file"]

# Indexes in the database-compatible format that we need on the fast path
_DB_NEED = ["Computer-Name", "Record-Date", "Record-Time", "Packet-Type", "User-Name",
            "Fully-Qualified-Distinguished-Name", "Called-Station-Id", "Calling-Station-Id",
            "NAS-Identifier", "NAS-IP-Address", "Client-IP-Address", "Client-Friendly-Name",
            "NAS-Port-Type", "Authentication-Type", "NP-Policy-Name", "Reason-Code",
            "EAP-Friendly-Name", "Acct-Status-Type", "Proxy-Policy-Name"]
DB_IDX = [DB_COLUMNS.index(n) for n in _DB_NEED]

OUTCOME_COLORS = {
    "accept": Fore.GREEN, "reject": Fore.RED, "discard": Fore.YELLOW, "challenge": Fore.CYAN,
    "accounting": Fore.MAGENTA, "request": Fore.WHITE, "other": Fore.WHITE,
}

# --------------------------------------------------------------------------- #
#  Console helpers                                                            #
# --------------------------------------------------------------------------- #
def c(text, color="", bright=False) -> str:
    return f"{Style.BRIGHT if bright else ''}{color}{text}{Style.RESET_ALL}"


def info(msg):
    print(c("  ›  ", Fore.CYAN, True) + msg)


def ok(msg):
    print(c("  ✔  ", Fore.GREEN, True) + msg)


def warn(msg):
    print(c("  !  ", Fore.YELLOW, True) + c(msg, Fore.YELLOW))


def err(msg):
    print(c("  ✖  ", Fore.RED, True) + c(msg, Fore.RED), file=sys.stderr)


def banner():
    line = "─" * 60
    print()
    print(c(f"  ╭{line}╮", Fore.BLUE, True))
    title = f"{APP_NAME}  v{APP_VERSION}"
    print(c("  │", Fore.BLUE, True) + c(f"{title:^60}", Fore.CYAN, True) + c("│", Fore.BLUE, True))
    sub = "Microsoft Network Policy Server log analysis"
    print(c("  │", Fore.BLUE, True) + c(f"{sub:^60}", Fore.WHITE) + c("│", Fore.BLUE, True))
    print(c(f"  ╰{line}╯", Fore.BLUE, True))
    print()


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def progress_bar(done, total, events, started, width=30):
    frac = done / total if total else 1
    filled = int(width * frac)
    bar = c("█" * filled, Fore.CYAN, True) + c("░" * (width - filled), Fore.BLUE)
    el = time.perf_counter() - started
    eta = (el / frac - el) if frac > 0.02 else 0
    sys.stdout.write(f"\r  Parsing {bar} {frac * 100:5.1f}%  {c(f'{events:,}', Fore.GREEN, True)} events  "
                     f"{c(f'{el:.1f}s', Fore.CYAN)}" + (f"  ETA {eta:.0f}s   " if eta else "          "))
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
#  Parsing helpers                                                            #
# --------------------------------------------------------------------------- #
EVENT_RE = re.compile(r"<Event>(.*?)</Event>", re.S)
TAG_RE = re.compile(r"<([A-Za-z0-9_\-]+)(?:\s[^>]*)?>(.*?)</\1>", re.S)


def to_int(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        return int(v, 0) if v.lower().startswith("0x") else int(float(v))
    except ValueError:
        return None


def parse_datetime(date_s: str, time_s: str = "") -> datetime | None:
    """Parse NPS date/time in the various locale-dependent flavours."""
    s = f"{date_s.strip()} {time_s.strip()}".strip()
    if not s:
        return None
    s = s.replace("T", " ")
    m = re.match(
        r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,6}))?)?", s)
    if not m:
        return None
    a, b, y, hh, mm, ss, frac = m.groups()
    a, b, y = int(a), int(b), int(y)
    if a > 31:  # ISO: YYYY-MM-DD
        year, month, day = a, b, y
    else:
        year = y if y > 99 else 2000 + y
        if "." in s.split(" ")[0] or a > 12:  # European DD.MM.YYYY
            day, month = a, b
        else:  # NPS default: MM/DD/YYYY
            month, day = a, b
    try:
        micro = int((frac or "0").ljust(6, "0")[:6])
        return datetime(year, month, day, int(hh or 0), int(mm or 0), int(ss or 0), micro)
    except ValueError:
        return None


def dt_to_ms(dt: datetime) -> int:
    """Naive (log-local) datetime -> milliseconds; the UI shows it unchanged (UTC math)."""
    d = dt - EPOCH
    return (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000


def classify(pt, rc) -> str:
    if pt == 2:
        return "accept"
    if pt == 3:
        return "reject"
    if pt == 11:
        return "challenge"
    if pt in (4, 5):
        return "accounting"
    if pt == 1:
        return "discard" if rc not in (None, 0) else "request"
    return "other"


def short_user(u: str) -> str:
    """'DOMAIN\\user' -> 'user' so request/response/accounting lines group together."""
    if "\\" in u:
        return u.rsplit("\\", 1)[1]
    return u


def split_csv(line: str):
    """Fast CSV split for NPS lines. Returns (fields, still_quoted)."""
    if '"' not in line:
        return line.split(","), False
    f = line.split(",")
    for x in f:
        if x[:1] == '"' and (len(x) == 1 or x[-1] != '"'):
            return next(csv.reader([line])), False  # comma inside quotes - slow path
    return f, True


def unq(x: str) -> str:
    if x[:1] == '"' and len(x) > 1:
        x = x[1:-1]
        return x.replace('""', '"') if '""' in x else x
    return x


def fields_from_line(line: str) -> tuple[str, dict]:
    """Full attribute dict of one log line (used for on-demand raw view)."""
    if "<Event>" in line:
        m = EVENT_RE.search(line)
        body = m.group(1) if m else line
        out = {}
        for tag, val in TAG_RE.findall(body):
            out.setdefault(tag, html.unescape(val))
        return "DTS XML", out
    f, quoted = split_csv(line)
    if quoted:
        f = [unq(x) for x in f]
    f = [x.strip() for x in f]
    if len(f) > 4 and f[4].isdigit():
        return "Database-compatible", {DB_COLUMNS[i]: v for i, v in enumerate(f) if i < len(DB_COLUMNS) and v}
    out = {"NAS-IP-Address": f[0], "User-Name": f[1], "Record-Date": f[2], "Record-Time": f[3],
           "Service-Name": f[4], "Computer-Name": f[5]} if len(f) >= 6 else {}
    rest = f[6:]
    for i in range(0, len(rest) - 1, 2):
        aid = to_int(rest[i])
        name = IAS_ATTRS.get(aid, f"Attr-{rest[i]}") if aid is not None else f"Attr-{rest[i]}"
        out[name] = f"{out[name]}; {rest[i + 1]}" if out.get(name) else rest[i + 1]
    return "IAS legacy", {k: v for k, v in out.items() if v}


def sniff_file(path: Path) -> tuple[str, str]:
    """-> (mode, fmt): mode 'bin' (utf-8/ansi, byte offsets) or 'text' (utf-16, line numbers)."""
    with open(path, "rb") as fh:
        head = fh.read(65536)
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff") or head[:200].count(b"\x00") > 40:
        enc = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-le"
        txt = head.decode(enc, errors="ignore")
        return "text:" + enc, "dts" if "<Event>" in txt else "csv"
    return "bin", "dts" if b"<Event>" in head else "csv"


# --------------------------------------------------------------------------- #
#  Chunk parser (runs in worker processes)                                    #
# --------------------------------------------------------------------------- #
class ChunkParser:
    def __init__(self, since, until, fname, exclude=()):
        n = len(STR_COLS)
        self.excl = re.compile("|".join(fnmatch.translate(x.lower()) for x in exclude)) if exclude else None
        self._excl_cache = {}
        self.excluded = 0
        self.maps = [{} for _ in range(n)]
        self.vals = [[] for _ in range(n)]
        self.codes = [array("I") for _ in range(n)]
        self.ts = array("q")
        self.loc = array("q")
        self.since, self.until, self.fname = since, until, fname
        self.skipped = self.filtered = 0
        self.fmts = set()
        self._dc, self._oc, self._lab = {}, {}, {}
        self._dts_buf, self._dts_loc = None, 0

    def ts_ms(self, d, t):
        base = self._dc.get(d)
        if base is None:
            dt = parse_datetime(d)
            base = False if dt is None else dt_to_ms(dt)
            self._dc[d] = base
        if base is False:
            return None
        try:
            p = t.split(":")
            if len(p) >= 3:
                s = p[2]
                ms = 0
                if "." in s or "," in s:
                    s, fr = re.split(r"[.,]", s, 1)
                    ms = int((fr + "00")[:3])
                return base + (int(p[0]) * 3600 + int(p[1]) * 60 + int(s)) * 1000 + ms
            if len(p) == 2:
                return base + (int(p[0]) * 3600 + int(p[1]) * 60) * 1000
            return base
        except ValueError:
            return None

    def label(self, table, raw):
        key = (id(table), raw)
        v = self._lab.get(key)
        if v is None:
            iv = to_int(raw)
            v = "" if iv is None else table.get(iv, str(iv))
            self._lab[key] = v
        return v

    def add(self, ts, loc, srv, pt, rc, user, fq, calling, called, client, client_ip, nas_ip,
            nas_id, policy, crp, auth, eap, acct, port):
        if ts is None:
            self.skipped += 1
            return
        if (self.since is not None and ts < self.since) or (self.until is not None and ts > self.until):
            self.filtered += 1
            return
        key = (pt, rc)
        out = self._oc.get(key)
        if out is None:
            out = self._oc[key] = classify(to_int(pt), to_int(rc))
        full = user or fq
        user = short_user(full)
        if self.excl is not None:
            hit = self._excl_cache.get(full)
            if hit is None:
                lf = full.lower()
                hit = self._excl_cache[full] = bool(self.excl.match(lf) or self.excl.match(user.lower())
                                                    or (fq and self.excl.match(fq.lower())))
            if hit:
                self.excluded += 1
                return
        row = (out, pt, rc, srv, user, calling, called, client, client_ip, nas_ip, nas_id, policy, crp,
               self.label(AUTH_TYPES, auth), eap, self.label(ACCT_STATUS, acct),
               self.label(NAS_PORT_TYPES, port), self.fname)
        for m, vals, codes, v in zip(self.maps, self.vals, self.codes, row):
            code = m.get(v)
            if code is None:
                code = m[v] = len(vals)
                vals.append(v)
            codes.append(code)
        self.ts.append(ts)
        self.loc.append(loc)

    def add_dict(self, f: dict, ts, loc):
        n = {}
        for k, v in f.items():
            key = FIELD_ALIASES.get(k.lower())
            if key and v and key not in n:
                n[key] = v.strip()
        g = n.get
        self.add(ts, loc, g("srv", ""), g("pt", ""), g("rc", ""), g("user", "") or g("sam", ""), g("fqun", ""),
                 g("calling", ""), g("called", ""), g("client", ""), g("client_ip", ""), g("nas_ip", ""),
                 g("nas_id", ""), g("policy", ""), g("crp", ""), g("auth", ""), g("eap", ""), g("acct", ""),
                 g("port_type", ""))

    # -- one log line --------------------------------------------------------
    def line(self, line: str, loc: int, fmt: str):
        if fmt == "dts":
            if self._dts_buf is not None:
                self._dts_buf += "\n" + line
                if "</Event>" not in line:
                    return
                line, loc, self._dts_buf = self._dts_buf, self._dts_loc, None
            elif "<Event>" in line and "</Event>" not in line:
                self._dts_buf, self._dts_loc = line, loc
                return
            for ev in EVENT_RE.finditer(line):
                fields = {}
                for tag, val in TAG_RE.findall(ev.group(1)):
                    if tag not in fields:
                        fields[tag] = html.unescape(val) if "&" in val else val
                ts_raw = fields.get("Timestamp", "").strip().split(" ", 1)
                ts = self.ts_ms(ts_raw[0], ts_raw[1] if len(ts_raw) > 1 else "")
                self.add_dict(fields, ts, loc)
                self.fmts.add("DTS XML")
            return

        f, quoted = split_csv(line)
        nf = len(f)
        if nf < 6:
            if line.strip():
                self.skipped += 1
            return
        f4 = unq(f[4]).strip() if quoted else f[4].strip()
        if f4.isdigit():  # database-compatible
            v = [f[i] if i < nf else "" for i in DB_IDX]
            if quoted:
                v = [unq(x) for x in v]
            (srv, d, t, pt, user, fq, called, calling, nas_id, nas_ip, client_ip, client, port, auth,
             policy, rc, eap, acct, crp) = v
            self.add(self.ts_ms(d.strip(), t.strip()), loc, srv, pt.strip(), rc.strip(), user, fq, calling,
                     called, client, client_ip, nas_ip, nas_id, policy, crp, auth, eap, acct, port)
            self.fmts.add("Database-compatible")
        else:  # legacy IAS
            if quoted:
                f = [unq(x) for x in f]
            f = [x.strip() for x in f]
            fields = {"NAS-IP-Address": f[0], "User-Name": f[1], "Computer-Name": f[5]}
            rest = f[6:]
            for i in range(0, len(rest) - 1, 2):
                aid = to_int(rest[i])
                name = IAS_ATTRS.get(aid) if aid is not None else None
                if name and not fields.get(name):
                    fields[name] = rest[i + 1]
            self.add_dict(fields, self.ts_ms(f[2], f[3]), loc)
            self.fmts.add("IAS legacy")

    def result(self, nbytes):
        return {"vals": self.vals, "codes": self.codes, "ts": self.ts, "loc": self.loc,
                "skipped": self.skipped, "filtered": self.filtered, "excluded": self.excluded, "fmts": sorted(self.fmts), "bytes": nbytes}


def parse_chunk(task):
    """Worker entry point (top level so it can be pickled on Windows)."""
    path, mode, fmt, start, end, fname, since, until, exclude = task
    p = ChunkParser(since, until, fname, exclude)
    if mode == "bin":
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start)
        off = start
        first = start == 0
        for b in data.split(b"\n"):
            loc = off
            off += len(b) + 1
            if not b or b == b"\r":
                continue
            try:
                s = b.decode("utf-8")
            except UnicodeDecodeError:
                s = b.decode("cp1252", errors="replace")
            if first:
                s = s.lstrip("\ufeff")
                first = False
            p.line(s.rstrip("\r"), loc, fmt)
    else:
        enc = mode.split(":", 1)[1]
        text = Path(path).read_bytes().decode(enc, errors="replace").lstrip("\ufeff")
        for i, s in enumerate(text.splitlines()):
            if s:
                p.line(s, i, fmt)
    return p.result(end - start)


def make_tasks(files, since, until, exclude=()):
    tasks = []
    for fi, f in enumerate(files):
        size = f["size"]
        if f["mode"] != "bin" or size <= CHUNK_BYTES:
            tasks.append((fi, (f["path"], f["mode"], f["fmt"], 0, size, f["display"], since, until, exclude)))
            continue
        bounds = [0]
        with open(f["path"], "rb") as fh:
            pos = CHUNK_BYTES
            while pos < size:
                fh.seek(pos)
                fh.readline()  # move to next line start
                nxt = fh.tell()
                if nxt >= size:
                    break
                bounds.append(nxt)
                pos = nxt + CHUNK_BYTES
        bounds.append(size)
        for a, b in zip(bounds, bounds[1:]):
            tasks.append((fi, (f["path"], f["mode"], f["fmt"], a, b, f["display"], since, until, exclude)))
    return tasks


# --------------------------------------------------------------------------- #
#  Merged store (main process)                                                #
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, files):
        n = len(STR_COLS)
        self.files = files
        self.maps = [{} for _ in range(n)]
        self.vals = [[] for _ in range(n)]
        self.codes = [array("H") for _ in range(n)]
        self.ts = array("q")
        self.loc = array("q")
        self.fidx = array("H")
        self.skipped = self.filtered = self.excluded = 0
        self.exclude_patterns = []
        self.formats = Counter()
        self._text_cache = (None, None)

    def __len__(self):
        return len(self.ts)

    def merge(self, res, fi):
        for j in range(len(STR_COLS)):
            m, vals = self.maps[j], self.vals[j]
            mapping = []
            for v in res["vals"][j]:
                code = m.get(v)
                if code is None:
                    code = m[v] = len(vals)
                    vals.append(v)
                mapping.append(code)
            if len(vals) > 65535 and self.codes[j].typecode == "H":
                self.codes[j] = array("I", self.codes[j])
            self.codes[j].extend(array(self.codes[j].typecode, map(mapping.__getitem__, res["codes"][j])))
        n = len(res["ts"])
        self.ts.extend(res["ts"])
        self.loc.extend(res["loc"])
        self.fidx.extend(array("H", [fi]) * n)
        self.files[fi]["events"] = self.files[fi].get("events", 0) + n
        self.skipped += res["skipped"]
        self.filtered += res["filtered"]
        self.excluded += res.get("excluded", 0)
        for f in res["fmts"]:
            self.formats[f] += 1
            self.files[fi]["format"] = f

    def sort_by_time(self):
        ts = self.ts
        if all(map(operator.le, ts, islice(ts, 1, None))):
            return False
        perm = sorted(range(len(ts)), key=ts.__getitem__)
        self.ts = array("q", map(ts.__getitem__, perm))
        self.loc = array("q", map(self.loc.__getitem__, perm))
        self.fidx = array("H", map(self.fidx.__getitem__, perm))
        self.codes = [array(a.typecode, map(a.__getitem__, perm)) for a in self.codes]
        return True

    def col(self, name):
        j = STR_COLS.index(name)
        return self.vals[j], self.codes[j]

    def raw_for(self, i: int) -> dict:
        f = self.files[self.fidx[i]]
        loc = self.loc[i]
        if f["mode"] == "bin":
            with open(f["path"], "rb") as fh:
                fh.seek(loc)
                b = fh.readline()
                if f["fmt"] == "dts" and b"</Event>" not in b:
                    for _ in range(200):
                        nxt = fh.readline()
                        if not nxt:
                            break
                        b += nxt
                        if b"</Event>" in nxt:
                            break
            try:
                line = b.decode("utf-8")
            except UnicodeDecodeError:
                line = b.decode("cp1252", errors="replace")
            where = f"byte offset {loc:,}"
        else:
            if self._text_cache[0] != f["path"]:
                enc = f["mode"].split(":", 1)[1]
                self._text_cache = (f["path"], Path(f["path"]).read_bytes().decode(enc, errors="replace")
                                    .lstrip("\ufeff").splitlines())
            line = self._text_cache[1][loc]
            where = f"line {loc + 1:,}"
        fmt, fields = fields_from_line(line.strip().lstrip("\ufeff"))
        return {"source": f"{f['path']} ({where})", "format": fmt, "fields": fields}


# --------------------------------------------------------------------------- #
#  File discovery                                                             #
# --------------------------------------------------------------------------- #
def discover(paths, pattern, recursive) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p).expanduser()
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            it = path.rglob("*") if recursive else path.glob("*")
            found.extend(f for f in it if f.is_file() and fnmatch.fnmatch(f.name.lower(), pattern.lower())
                         and ".temp" not in f.parts)
        elif any(ch in p for ch in "*?["):  # cmd.exe does not expand wildcards
            base = Path(p).parent if Path(p).parent != Path("") else Path(".")
            found.extend(m for m in sorted(base.glob(Path(p).name)) if m.is_file())
        else:
            warn(f"Path not found: {p}")
    seen, uniq = set(), []
    for f in found:
        key = str(f.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return sorted(uniq, key=lambda x: (str(x.parent).lower(), x.name.lower()))


# --------------------------------------------------------------------------- #
#  Summary                                                                    #
# --------------------------------------------------------------------------- #
def print_summary(st: Store):
    out_vals, out_codes = st.col("out")
    rc_vals, rc_codes = st.col("rc")
    user_vals, user_codes = st.col("user")
    n = len(st)
    outcomes = Counter()
    for code, cnt in Counter(out_codes).items():
        outcomes[out_vals[code]] += cnt
    print()
    print(c("  SUMMARY", Fore.CYAN, True))
    print(c("  " + "─" * 58, Fore.BLUE))
    def fmt_ts(ms):
        return (EPOCH + timedelta(milliseconds=ms)).strftime("%Y-%m-%d %H:%M:%S")
    info(f"Time range      {c(fmt_ts(st.ts[0]), Fore.WHITE, True)}  →  {c(fmt_ts(st.ts[-1]), Fore.WHITE, True)}")
    info(f"Total events    {c(f'{n:,}', Fore.WHITE, True)}")
    for name in ("accept", "reject", "discard", "challenge", "request", "accounting", "other"):
        if outcomes.get(name):
            bar = "█" * max(1, int(30 * outcomes[name] / max(1, n)))
            print(f"       {name.capitalize():<12}{c(f'{outcomes[name]:>11,}', OUTCOME_COLORS[name], True)}  "
                  f"{c(bar, OUTCOME_COLORS[name])}")
    auth_total = outcomes["accept"] + outcomes["reject"]
    if auth_total:
        rate = 100 * outcomes["accept"] / auth_total
        color = Fore.GREEN if rate >= 90 else Fore.YELLOW if rate >= 70 else Fore.RED
        info(f"Success rate    {c(f'{rate:.1f} %', color, True)}  (Accept / (Accept + Reject))")

    fail = {i for i, v in enumerate(out_vals) if v in ("reject", "discard")}
    if fail:
        rc_cnt, user_cnt = Counter(), Counter()
        for o, r, u in zip(out_codes, rc_codes, user_codes):
            if o in fail:
                rc_cnt[r] += 1
                user_cnt[u] += 1
        print()
        print(c("  TOP FAILURE REASONS", Fore.RED, True))
        for code, cnt in rc_cnt.most_common(5):
            rc = to_int(rc_vals[code])
            print(f"       {c(f'{cnt:>11,}', Fore.RED, True)}  {c(f'[{rc}]', Fore.YELLOW)} "
                  f"{REASON_CODES.get(rc, f'Reason code {rc}')}")
        print()
        print(c("  USERS WITH MOST FAILURES", Fore.RED, True))
        for code, cnt in user_cnt.most_common(5):
            print(f"       {c(f'{cnt:>11,}', Fore.RED, True)}  {user_vals[code] or '(empty)'}")
    print(c("  " + "─" * 58, Fore.BLUE))


# --------------------------------------------------------------------------- #
#  Dashboard data                                                             #
# --------------------------------------------------------------------------- #
def _b64gz(b: bytes) -> str:
    return base64.b64encode(gzip.compress(b, 5)).decode("ascii")


def _le_bytes(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array(a.typecode, a)
        a.byteswap()
    return a.tobytes()


def pack_codes(codes: array, nvals: int) -> dict:
    if nvals <= 256:
        return {"type": "u8", "data": _b64gz(array("B", codes).tobytes())}
    if nvals <= 65536:
        a = codes if codes.typecode == "H" else array("H", codes)
        return {"type": "u16", "data": _b64gz(_le_bytes(a))}
    tc = "I" if array("I").itemsize == 4 else "L"
    a = codes if codes.typecode == tc else array(tc, codes)
    return {"type": "u32", "data": _b64gz(_le_bytes(a))}


def build_data_js(st: Store, elapsed: float, embed_raw: bool) -> str:
    ts = st.ts
    n = len(ts)
    try:
        d = array("I", [0])
        d.extend(array("I", map(operator.sub, islice(ts, 1, None), ts)))
        ts_spec = {"type": "u32", "enc": "delta", "data": _b64gz(_le_bytes(d))}
    except OverflowError:
        ts_spec = {"type": "f64", "enc": "abs", "data": _b64gz(_le_bytes(array("d", ts)))}
    payload = {
        "meta": {
            "app": APP_NAME, "version": APP_VERSION,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": [{"name": f["display"], "path": str(f["path"]), "size": f["size"],
                       "events": f.get("events", 0), "format": f.get("format", "")} for f in st.files],
            "formats": dict(st.formats), "skipped": st.skipped, "filtered": st.filtered,
            "excluded": st.excluded, "exclude_patterns": st.exclude_patterns,
            "parse_seconds": round(elapsed, 2),
        },
        "n": n, "t0": ts[0] if n else 0, "ts": ts_spec,
        "cols": {name: {"dict": st.vals[j], **pack_codes(st.codes[j], len(st.vals[j]))}
                 for j, name in enumerate(STR_COLS)},
        "raw": [st.raw_for(i) for i in range(n)] if embed_raw else None,
        "reasons": {str(k): v for k, v in REASON_CODES.items()},
        "packets": {str(k): v for k, v in PACKET_TYPES.items()},
    }
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "window.NPS_DATA=" + js.replace("</", "<\\/").replace("<!--", "<\\!--") + ";"


def render_html(data_js: str | None, src: str | None = None) -> str:
    tag = f'<script src="{src}"></script>' if src else "<script>" + data_js + "</script>"
    return HTML_TEMPLATE.replace("<!--__DATA__-->", tag)


class Api:
    """Exposed to the dashboard as window.pywebview.api"""

    def __init__(self, store: Store):
        self._store = store

    def get_raw(self, i):
        try:
            return self._store.raw_for(int(i))
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}


# --------------------------------------------------------------------------- #
#  Main                                                                       #
# --------------------------------------------------------------------------- #
def parse_date_arg(s, end=False):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date: {s} (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    if end and len(s) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return dt_to_ms(dt)


def make_temp_dir() -> Path:
    root = Path.cwd() / ".temp"
    session = root / f"nps_{os.getpid()}_{int(time.time())}"
    session.mkdir(parents=True, exist_ok=True)

    def cleanup():
        shutil.rmtree(session, ignore_errors=True)
        try:
            if root.exists() and not any(root.iterdir()):
                root.rmdir()
        except OSError:
            pass

    atexit.register(cleanup)
    return session


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="nps_log_analyzer",
        description="Analyze Microsoft NPS (RADIUS) log files and show them in an interactive dashboard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s                       (all *.log files in the current folder)\n"
               "  %(prog)s C:\\Windows\\System32\\LogFiles\n"
               "  %(prog)s IN2610.log IN2611.log\n"
               "  %(prog)s D:\\logs -r --pattern \"IN*.log\" --since 2026-11-01\n"
               "  %(prog)s D:\\logs --export report.html --no-gui\n"
               "  %(prog)s -x testaccount -x \"svc_*\"",
    )
    ap.add_argument("paths", nargs="*", help="Log file(s), wildcards and/or directories (default: current folder)")
    ap.add_argument("-p", "--pattern", default="*.log", help="File name pattern inside directories (default: *.log)")
    ap.add_argument("-r", "--recursive", action="store_true", help="Search directories recursively")
    ap.add_argument("--since", help="Only events from this date/time (YYYY-MM-DD[ HH:MM])")
    ap.add_argument("--until", help="Only events up to this date/time (YYYY-MM-DD[ HH:MM])")
    ap.add_argument("-x", "--exclude-user", action="append", default=[], metavar="USER",
                    help="Skip events of this user (repeatable or comma separated, wildcards * ? allowed, "
                         "case-insensitive, matches 'user' and 'DOMAIN\\user'), e.g. -x testaccount -x \"svc_*\"")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help=f"Parallel worker processes (default: {os.cpu_count()})")
    ap.add_argument("-o", "--export", metavar="FILE.html", help="Save the dashboard as one standalone HTML file")
    ap.add_argument("--no-gui", action="store_true", help="Do not open the dashboard window")
    ap.add_argument("--browser", action="store_true", help="Use the default web browser instead of pywebview")
    ap.add_argument("--debug", action="store_true", help="Enable pywebview developer tools")
    args = ap.parse_args(argv)

    banner()

    paths = args.paths
    if not paths:
        cwd_logs = discover(["."], args.pattern, False)
        default = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "LogFiles"
        if cwd_logs:
            paths = ["."]
            info(f"No path given, using current folder {c(str(Path.cwd()), Fore.WHITE, True)}")
        elif os.name == "nt" and default.is_dir():
            paths = [str(default)]
            if args.pattern == "*.log":
                args.pattern = "IN*.log"
            info(f"No path given, using default NPS log folder {c(str(default), Fore.WHITE, True)}")
        else:
            ap.print_help()
            return 2

    tmp = None
    if not args.no_gui:
        tmp = make_temp_dir()
        info(f"Working folder  {c(str(tmp), Fore.WHITE, True)} (removed on exit)")

    try:
        since = parse_date_arg(args.since)
        until = parse_date_arg(args.until, end=True)
    except argparse.ArgumentTypeError as e:
        err(str(e))
        return 2

    exclude = tuple(x.strip() for v in args.exclude_user for x in v.split(",") if x.strip())
    if exclude:
        info(f"Excluding users {c(', '.join(exclude), Fore.YELLOW, True)}")

    paths_found = discover(paths, args.pattern, args.recursive)
    if not paths_found:
        err(f"No log files found (pattern: {args.pattern}).")
        return 1

    names = Counter(p.name.lower() for p in paths_found)
    files = []
    for p in paths_found:
        mode, fmt = sniff_file(p)
        files.append({"path": str(p.resolve()), "display": p.name if names[p.name.lower()] == 1
                      else f"{p.parent.name}/{p.name}", "size": p.stat().st_size, "mode": mode, "fmt": fmt})
    total_size = sum(f["size"] for f in files)
    info(f"Found {c(len(files), Fore.WHITE, True)} file(s), {c(human_size(total_size), Fore.WHITE, True)}")

    # ---- parse (parallel) ----------------------------------------------------
    tasks = make_tasks(files, since, until, exclude)
    jobs = max(1, min(args.jobs, len(tasks)))
    info(f"Parsing with {c(jobs, Fore.WHITE, True)} worker process(es), {len(tasks)} chunk(s)")
    print()
    store = Store(files)
    store.exclude_patterns = list(exclude)
    t0 = time.perf_counter()
    done_bytes, events = 0, 0
    pending, next_i = {}, 0

    def consume(i, res):
        nonlocal next_i
        pending[i] = res
        while next_i in pending:
            store.merge(pending.pop(next_i), tasks[next_i][0])
            next_i += 1

    progress_bar(0, total_size, 0, t0)
    if jobs == 1:
        for i, (_, t) in enumerate(tasks):
            res = parse_chunk(t)
            done_bytes += res["bytes"]
            events += len(res["ts"])
            consume(i, res)
            progress_bar(done_bytes, total_size, events, t0)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(parse_chunk, t): i for i, (_, t) in enumerate(tasks)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    print()
                    err(f"Failed to parse {tasks[i][1][0]}: {e}")
                    res = ChunkParser(since, until, "", exclude).result(tasks[i][1][4] - tasks[i][1][3])
                done_bytes += res["bytes"]
                events += len(res["ts"])
                consume(i, res)
                progress_bar(done_bytes, total_size, events, t0)
    print()
    if store.sort_by_time():
        info("Events re-ordered by time")
    elapsed = time.perf_counter() - t0
    print()

    width = max(len(f["display"]) for f in files)
    for i, f in enumerate(files, 1):
        ev = f"{f.get('events', 0):>11,}"
        print(f"  {c(f'[{i}/{len(files)}]', Fore.BLUE, True)} {f['display']:<{width}}  "
              f"{c(ev, Fore.GREEN, True)} events  "
              f"{c(human_size(f['size']), Fore.WHITE)}  {c(f.get('format', '-'), Fore.CYAN)}")
    print()
    ok(f"Parsed {c(f'{len(store):,}', Fore.WHITE, True)} events in {c(f'{elapsed:.1f}s', Fore.WHITE, True)} "
       f"({human_size(total_size / max(elapsed, 0.001))}/s)")
    if store.skipped:
        warn(f"Skipped {store.skipped:,} unrecognized line(s)")
    if store.filtered:
        info(f"Excluded {store.filtered:,} event(s) outside --since/--until range")
    if store.excluded:
        info(f"Excluded {c(f'{store.excluded:,}', Fore.YELLOW, True)} event(s) of excluded user(s)")
    elif exclude:
        warn("No events matched the --exclude-user pattern(s)")
    if not len(store):
        err("No events were parsed - is this really an NPS log?")
        return 1

    print_summary(store)

    # ---- build dashboard -----------------------------------------------------
    tb = time.perf_counter()
    if args.export:
        out = Path(args.export).expanduser().resolve()
        data_js = build_data_js(store, elapsed, embed_raw=len(store) <= RAW_EMBED_LIMIT)
        out.write_text(render_html(data_js), encoding="utf-8")
        ok(f"Dashboard saved to {c(str(out), Fore.WHITE, True)} ({human_size(out.stat().st_size)})")
        if args.no_gui:
            return 0
    if args.no_gui:
        return 0

    data_js = build_data_js(store, elapsed, embed_raw=False)
    (tmp / "data.js").write_text(data_js, encoding="utf-8")
    html_path = tmp / "dashboard.html"
    html_path.write_text(render_html(None, src="data.js"), encoding="utf-8")
    info(f"Dashboard data prepared in {time.perf_counter() - tb:.1f}s "
         f"({human_size((tmp / 'data.js').stat().st_size)}) → {c(str(tmp / 'dashboard.html'), Fore.WHITE)}")

    if args.browser or not HAS_WEBVIEW:
        if not HAS_WEBVIEW:
            warn("pywebview is not installed (pip install pywebview) - opening the default browser instead")
        webbrowser.open(html_path.as_uri())
        info("Press Enter to quit (the .temp folder will be cleaned up)...")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return 0

    info("Opening dashboard window...")
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True  # CSV export from the UI
    except Exception:
        pass
    webview.create_window(
        f"{APP_NAME} — {len(store):,} events",
        url=html_path.as_uri(),
        js_api=Api(store),
        width=1560, height=980, min_size=(1100, 700),
        maximized=True,
        background_color="#0b1020",
        text_select=True,
    )
    webview.start(debug=args.debug)
    ok("Window closed, cleaning up .temp - bye.")
    return 0


# --------------------------------------------------------------------------- #
#  Dashboard (HTML / CSS / JS, fully self-contained, no CDN)                   #
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NPS Log Analyzer</title>
<style>
:root{
  --bg:#0b1020; --bg2:#0f1630; --card:rgba(255,255,255,.035); --card-b:rgba(255,255,255,.08);
  --text:#e6e9f5; --muted:#8b93b3; --faint:#5b6488; --grid:rgba(255,255,255,.06);
  --accent:#7c8cff; --accent2:#4fd1c5;
  --accept:#34d399; --reject:#f87171; --discard:#fbbf24; --challenge:#60a5fa;
  --accounting:#a78bfa; --request:#64748b; --other:#475569;
  --shadow:0 10px 30px rgba(0,0,0,.35); --radius:16px;
  --font:"Segoe UI Variable Text","Segoe UI",Inter,system-ui,-apple-system,Roboto,sans-serif;
  --mono:"Cascadia Code","JetBrains Mono",Consolas,ui-monospace,monospace;
}
[data-theme=light]{
  --bg:#f4f6fb; --bg2:#ffffff; --card:#ffffff; --card-b:#e3e7f1;
  --text:#18203a; --muted:#5d6788; --faint:#9aa3c0; --grid:rgba(0,0,0,.06);
  --accent:#4f5bd5; --request:#94a3b8; --other:#cbd5e1; --shadow:0 6px 20px rgba(30,40,90,.08);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:var(--font);background:var(--bg);color:var(--text);font-size:13.5px;
  background-image:radial-gradient(1200px 600px at 10% -10%,rgba(124,140,255,.16),transparent 60%),
  radial-gradient(900px 500px at 110% 10%,rgba(79,209,197,.10),transparent 60%);background-attachment:fixed}
[data-theme=light] body{background-image:none}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--card-b);border-radius:10px}
button,input,select{font:inherit;color:inherit}
.app{max-width:1800px;margin:0 auto;padding:18px 22px 40px}

/* header */
header{display:flex;align-items:center;gap:16px;margin-bottom:16px}
.logo{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 6px 20px rgba(124,140,255,.35)}
.logo svg{width:24px;height:24px}
h1{font-size:20px;margin:0;font-weight:650;letter-spacing:.2px}
.sub{color:var(--muted);font-size:12.5px;margin-top:2px}
.spacer{flex:1}
.btn{background:var(--card);border:1px solid var(--card-b);padding:8px 13px;border-radius:10px;cursor:pointer;
  display:inline-flex;align-items:center;gap:7px;transition:.15s}
.btn:hover{border-color:var(--accent);transform:translateY(-1px)}
.btn svg{width:15px;height:15px}

/* filter bar */
.filters{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  padding:12px;margin:0 -4px 16px;border-radius:var(--radius);background:color-mix(in srgb,var(--bg2) 82%,transparent);
  backdrop-filter:blur(14px);border:1px solid var(--card-b);box-shadow:var(--shadow)}
.search{flex:1 1 280px;position:relative}
.search input{width:100%;padding:9px 12px 9px 34px;border-radius:10px;border:1px solid var(--card-b);background:var(--bg);outline:none}
.search input:focus,.field:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,140,255,.2)}
.search svg{position:absolute;left:11px;top:50%;transform:translateY(-50%);width:15px;height:15px;color:var(--muted)}
.search kbd{position:absolute;right:10px;top:50%;transform:translateY(-50%);font-size:11px;color:var(--faint);
  border:1px solid var(--card-b);border-radius:5px;padding:1px 6px}
.field{padding:8px 10px;border-radius:10px;border:1px solid var(--card-b);background:var(--bg);outline:none}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 11px;border-radius:999px;cursor:pointer;
  border:1px solid var(--card-b);background:var(--card);user-select:none;transition:.15s;font-size:12.5px}
.chip .dot{width:8px;height:8px;border-radius:50%}
.chip .n{color:var(--muted);font-variant-numeric:tabular-nums}
.chip.off{opacity:.38}
.chip:hover{border-color:var(--muted)}
.facets{display:flex;gap:6px;flex-wrap:wrap;width:100%}
.facets:empty{display:none}
.facet{background:rgba(124,140,255,.14);border:1px solid rgba(124,140,255,.4);color:var(--text);
  border-radius:999px;padding:4px 6px 4px 11px;font-size:12px;display:inline-flex;gap:6px;align-items:center}
.facet b{font-weight:600;color:var(--accent)}
.facet button{border:0;background:rgba(255,255,255,.1);border-radius:50%;width:18px;height:18px;cursor:pointer;line-height:1}

/* grid + cards */
.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--card-b);border-radius:var(--radius);padding:16px;box-shadow:var(--shadow);min-width:0}
.kpi .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.7px;display:flex;align-items:center;gap:7px}
.kpi .label i{width:8px;height:8px;border-radius:3px;display:inline-block}
.kpi .value{font-size:28px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.kpi .hint{color:var(--muted);font-size:12px;margin-top:2px}
.ring{height:4px;border-radius:4px;background:var(--grid);margin-top:10px;overflow:hidden}
.ring>div{height:100%;border-radius:4px}
.grid{display:grid;gap:14px;margin-bottom:14px}
.g-main{grid-template-columns:minmax(0,3fr) minmax(0,1fr)}
.g-3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g-2{grid-template-columns:minmax(0,1.3fr) minmax(0,1fr)}
.card h3{margin:0 0 12px;font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;
  display:flex;align-items:center;gap:8px}
.card h3 .tip{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400;color:var(--faint);font-size:11.5px}
@media (max-width:1300px){.kpis{grid-template-columns:repeat(3,1fr)}.g-3{grid-template-columns:1fr 1fr}}
@media (max-width:950px){.g-main,.g-2,.g-3{grid-template-columns:1fr}}

/* timeline */
#timeline{position:relative;height:240px;user-select:none}
#timeline svg{width:100%;height:100%;display:block}
.axis text{fill:var(--muted);font-size:10.5px}
.axis line{stroke:var(--grid)}
.bar-rect{transition:opacity .1s}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12px;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:10px;height:10px;border-radius:3px}
.tooltip{position:fixed;z-index:100;pointer-events:none;background:var(--bg2);border:1px solid var(--card-b);
  border-radius:10px;padding:9px 11px;font-size:12px;box-shadow:var(--shadow);min-width:150px;display:none}
.tooltip .tt-h{font-weight:600;margin-bottom:5px}
.tooltip .tt-r{display:flex;justify-content:space-between;gap:16px;font-variant-numeric:tabular-nums}
.tooltip .tt-r span:first-child{display:flex;align-items:center;gap:6px;color:var(--muted)}
.tooltip i{width:8px;height:8px;border-radius:2px;display:inline-block}

/* donut */
.donut-wrap{display:flex;flex-direction:column;align-items:center;gap:12px}
#donut svg{width:190px;height:190px}
.donut-legend{width:100%;display:grid;gap:6px}
.donut-legend div{display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;padding:3px 6px;border-radius:7px}
.donut-legend div:hover{background:var(--grid)}
.donut-legend i{width:10px;height:10px;border-radius:3px}
.donut-legend b{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
.donut-legend em{color:var(--muted);font-style:normal;width:48px;text-align:right;font-variant-numeric:tabular-nums}

/* bar lists */
.blist{display:flex;flex-direction:column;gap:4px;max-height:290px;overflow:auto;padding-right:4px}
.brow{position:relative;flex-shrink:0;display:flex;align-items:center;gap:10px;padding:6px 9px;border-radius:8px;cursor:pointer;overflow:hidden}
.brow:hover{background:var(--grid)}
.brow .fill{position:absolute;left:0;top:0;bottom:0;border-radius:8px;opacity:.18}
.brow .lbl{position:relative;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brow .lbl small{color:var(--muted);margin-left:6px}
.brow .cnt{position:relative;font-variant-numeric:tabular-nums;font-weight:600}
.empty{color:var(--faint);padding:20px 4px;text-align:center}
.code{font-family:var(--mono);font-size:11.5px;padding:1px 6px;border-radius:6px;background:var(--grid);color:var(--muted)}

/* heatmap */
#heatmap{display:grid;grid-template-columns:36px repeat(24,1fr);gap:3px;font-size:10.5px;color:var(--muted)}
#heatmap .hc{aspect-ratio:1.5/1;border-radius:4px;cursor:pointer;min-height:14px}
#heatmap .hc:hover{outline:2px solid var(--text)}
#heatmap .hl{display:flex;align-items:center}
#heatmap .hh{text-align:center}

/* table */
.tbl-wrap{overflow:auto;border-radius:12px;border:1px solid var(--card-b);max-height:620px}
table{width:100%;border-collapse:collapse;font-size:12.8px}
thead th{position:sticky;top:0;background:var(--bg2);text-align:left;font-weight:600;color:var(--muted);
  padding:10px 12px;border-bottom:1px solid var(--card-b);white-space:nowrap;cursor:pointer;user-select:none;z-index:1}
thead th:hover{color:var(--text)}
tbody td{padding:8px 12px;border-bottom:1px solid var(--grid);white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis}
tbody tr{cursor:pointer;transition:background .1s}
tbody tr:hover{background:rgba(124,140,255,.08)}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;font-size:11.5px;font-weight:600}
.mono{font-family:var(--mono);font-size:12px}
.muted{color:var(--muted)}
.pager{display:flex;align-items:center;gap:10px;margin-top:12px;color:var(--muted)}
.pager .btn{padding:6px 11px}

/* drawer */
.overlay{position:fixed;inset:0;background:rgba(5,8,20,.5);backdrop-filter:blur(3px);z-index:60;opacity:0;pointer-events:none;transition:.2s}
.overlay.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(560px,94vw);z-index:70;background:var(--bg2);border-left:1px solid var(--card-b);
  transform:translateX(102%);transition:transform .25s cubic-bezier(.2,.8,.2,1);display:flex;flex-direction:column;box-shadow:-20px 0 50px rgba(0,0,0,.35)}
.drawer.open{transform:none}
.drawer .dh{padding:20px 22px;border-bottom:1px solid var(--card-b);display:flex;gap:12px;align-items:flex-start}
.drawer .db{padding:18px 22px;overflow:auto;flex:1}
.drawer h2{margin:0 0 4px;font-size:17px;word-break:break-all}
.kv{display:grid;grid-template-columns:170px 1fr;gap:0;font-size:12.8px}
.kv div{padding:7px 0;border-bottom:1px solid var(--grid);word-break:break-all}
.kv div:nth-child(odd){color:var(--muted)}
.reason-box{border-radius:12px;padding:12px 14px;margin:4px 0 18px;border:1px solid}
.sec{font-size:11.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--faint);margin:18px 0 8px}
.linkish{color:var(--accent);cursor:pointer;text-decoration:none;border-bottom:1px dashed transparent}
.linkish:hover{border-bottom-color:var(--accent)}
.foot{color:var(--faint);font-size:11.5px;text-align:center;margin-top:24px}
</style>
<style>
.loader{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;gap:18px;align-items:center;justify-content:center;
  background:var(--bg);color:var(--muted);font-size:14px;transition:opacity .3s}
.loader.hide{opacity:0;pointer-events:none}
.spin{width:46px;height:46px;border-radius:50%;border:4px solid var(--grid);border-top-color:var(--accent);animation:sp 0.9s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(30px);opacity:0;z-index:150;
  background:var(--bg2);border:1px solid var(--card-b);box-shadow:var(--shadow);padding:11px 16px;border-radius:12px;transition:.25s;pointer-events:none}
.toast.show{opacity:1;transform:translateX(-50%)}
.app{max-width:none}
.tabs{display:flex;gap:4px;padding:4px;margin-left:28px;background:var(--card);border:1px solid var(--card-b);border-radius:13px}
.tab{border:0;background:transparent;padding:8px 18px;border-radius:10px;cursor:pointer;color:var(--muted);font-weight:600;
  display:inline-flex;gap:8px;align-items:center;transition:.15s}
.tab svg{width:16px;height:16px}
.tab:hover{color:var(--text);background:var(--grid)}
.tab.active{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 4px 14px rgba(124,140,255,.35)}
.tab .n{font-size:11.5px;font-weight:600;padding:1px 8px;border-radius:999px;background:rgba(255,255,255,.14);font-variant-numeric:tabular-nums}
.tab:not(.active) .n{background:var(--grid)}
.tabpane[hidden]{display:none}
.topbar{position:sticky;top:0;z-index:30;margin:-18px -22px 16px;padding:14px 22px 12px;background:var(--bg);
  border-bottom:1px solid transparent;transition:box-shadow .2s,border-color .2s}
.topbar.stuck{box-shadow:0 10px 30px rgba(0,0,0,.45);border-bottom-color:var(--card-b)}
.topbar header{margin-bottom:12px}
.topbar .filters{position:static;margin:0;box-shadow:none;backdrop-filter:none;background:var(--bg2)}
.ev-strip{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.ev-strip .chip{cursor:default}
#tab-events .card{padding-bottom:12px}
#tab-events .tbl-wrap{max-height:none}
:root{color-scheme:dark}[data-theme=light]{color-scheme:light}
.dp{position:relative}
.dp-btn{display:inline-flex;align-items:center;gap:9px;cursor:pointer;min-width:230px;transition:.15s}
.dp-btn svg{width:16px;height:16px;color:var(--muted);flex:none}
.dp-btn .chev{margin-left:auto;width:14px;height:14px;transition:transform .2s}
.dp-btn:hover{border-color:var(--accent)}
.dp-btn.active{border-color:var(--accent);background:rgba(124,140,255,.13)}
.dp-btn.active svg{color:var(--accent)}
.dp-btn.open .chev{transform:rotate(180deg)}
.dp-pop{position:absolute;right:0;top:calc(100% + 10px);z-index:90;display:flex;background:var(--bg2);border:1px solid var(--card-b);
  border-radius:18px;box-shadow:0 24px 70px rgba(0,0,0,.55);overflow:hidden;animation:dpIn .16s ease-out}
.dp-pop[hidden]{display:none}
@keyframes dpIn{from{opacity:0;transform:translateY(-6px) scale(.98)}to{opacity:1;transform:none}}
.dp-presets{display:flex;flex-direction:column;gap:2px;padding:14px 10px;border-right:1px solid var(--card-b);min-width:160px;background:var(--card)}
.dp-presets .ttl{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--faint);padding:2px 12px 8px}
.dp-presets button{text-align:left;border:0;background:transparent;padding:8px 12px;border-radius:9px;cursor:pointer;color:var(--muted);font-size:13px}
.dp-presets button:hover{background:var(--grid);color:var(--text)}
.dp-presets button.on{background:rgba(124,140,255,.16);color:var(--accent);font-weight:600}
.dp-main{padding:16px 20px 16px}
.dp-months{display:flex;gap:30px}
.dp-mh{display:grid;grid-template-columns:30px 1fr 30px;align-items:center;margin-bottom:10px}
.dp-mh h4{margin:0;text-align:center;font-size:14px;font-weight:600}
.dp-nav{width:30px;height:30px;border-radius:9px;border:1px solid var(--card-b);background:var(--card);cursor:pointer;font-size:16px;line-height:1}
.dp-nav:hover{border-color:var(--accent);color:var(--accent)}
.dp-grid{display:grid;grid-template-columns:repeat(7,38px);row-gap:3px}
.dp-wd{font-size:11px;color:var(--faint);text-align:center;padding-bottom:6px;font-weight:600}
.dp-day{position:relative;height:36px;border:0;background:transparent;cursor:pointer;font-size:12.8px;color:var(--text);border-radius:10px;font-variant-numeric:tabular-nums}
.dp-day:hover:not(:disabled){background:var(--grid)}
.dp-day:disabled{color:var(--faint);opacity:.4;cursor:default}
.dp-day .bar{position:absolute;left:11px;right:11px;bottom:4px;height:3px;border-radius:2px;background:var(--accent2)}
.dp-day.in{background:rgba(124,140,255,.16);border-radius:0}
.dp-day.s,.dp-day.e{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;font-weight:700}
.dp-day.s{border-radius:10px 0 0 10px}.dp-day.e{border-radius:0 10px 10px 0}.dp-day.s.e{border-radius:10px}
.dp-day.s .bar,.dp-day.e .bar{background:#fff}
.dp-times{display:flex;align-items:center;gap:12px;margin-top:14px;padding-top:14px;border-top:1px solid var(--card-b);font-size:12.5px;color:var(--muted)}
.dp-times label{display:flex;align-items:center;gap:8px}
.dp-times input{padding:6px 8px;width:auto}
.dp-hint{font-size:11.5px;color:var(--faint)}
.dp-foot{display:flex;align-items:center;gap:8px;margin-top:12px}
.dp-sum{flex:1;font-size:12.5px;color:var(--muted)}
.dp-sum b{color:var(--text);font-weight:600}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#fff;font-weight:600}
body.ev-mode{overflow:hidden}
body.ev-mode .foot{display:none}
body.ev-mode .app{padding-bottom:14px}
.drawer-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
</style>
</head>
<body>
<div class="loader" id="loader"><div class="spin"></div><div id="loaderMsg">Loading data…</div></div>
<div class="app">
  <div class="topbar" id="topbar">
  <header>
    <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg></div>
    <div>
      <h1>NPS Log Analyzer</h1>
      <div class="sub" id="subtitle"></div>
    </div>
    <nav class="tabs" id="tabs">
      <button class="tab active" data-tab="overview" title="Overview (key 1)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>Overview</button>
      <button class="tab" data-tab="events" title="Event log (key 2)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>Events <span class="n" id="tabCount"></span></button>
    </nav>
    <div class="spacer"></div>
    <button class="btn" id="btnFiles" title="Show source files"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>Files</button>
    <button class="btn" id="btnCsv" title="Export filtered events to CSV"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>Export CSV</button>
    <button class="btn" id="btnTheme" title="Toggle light / dark theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>Theme</button>
  </header>

  <div class="filters">
    <div class="search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      <input id="q" placeholder="Search user, MAC, client, IP, policy, reason…" autocomplete="off">
      <kbd>/</kbd>
    </div>
    <div class="chips" id="chips"></div>
    <select class="field" id="srv" title="NPS server"></select>
    <div class="dp" id="dp">
      <button class="field dp-btn" id="dpBtn" title="Select time range">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
        <span id="dpLabel">All time</span>
        <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>
      </button>
      <div class="dp-pop" id="dpPop" hidden>
        <div class="dp-presets" id="dpPresets"></div>
        <div class="dp-main">
          <div class="dp-months" id="dpMonths"></div>
          <div class="dp-times">
            <label>From <input class="field" type="time" id="dpFromT" value="00:00"></label>
            <span class="muted">→</span>
            <label>To <input class="field" type="time" id="dpToT" value="23:59"></label>
            <div class="spacer"></div>
            <span class="dp-hint">Click a start day, then an end day</span>
          </div>
          <div class="dp-foot">
            <div id="dpSum" class="dp-sum"></div>
            <button class="btn" id="dpClear">Clear</button>
            <button class="btn primary" id="dpApply">Apply</button>
          </div>
        </div>
      </div>
    </div>
    <button class="btn" id="btnReset"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>Reset</button>
    <div class="facets" id="facets"></div>
  </div>
  </div>

  <main class="tabpane" id="tab-overview">
  <section class="kpis" id="kpis"></section>

  <section class="grid g-main">
    <div class="card">
      <h3>Activity timeline <span class="tip">drag to zoom · double-click to reset · <span id="bucketInfo"></span></span></h3>
      <div id="timeline"></div>
      <div class="legend" id="tlLegend"></div>
    </div>
    <div class="card">
      <h3>Outcome distribution</h3>
      <div class="donut-wrap"><div id="donut"></div><div class="donut-legend" id="donutLegend"></div></div>
    </div>
  </section>

  <section class="grid g-2">
    <div class="card"><h3>Top failure reasons <span class="tip">click to filter</span></h3><div class="blist" id="lReasons"></div></div>
    <div class="card"><h3>Activity heatmap <span class="tip">weekday × hour · red = mostly failures</span></h3><div id="heatmap"></div></div>
  </section>

  <section class="grid g-3">
    <div class="card"><h3>Users with most failures</h3><div class="blist" id="lFailUsers"></div></div>
    <div class="card"><h3>Most active users</h3><div class="blist" id="lUsers"></div></div>
    <div class="card"><h3>RADIUS clients (NAS)</h3><div class="blist" id="lClients"></div></div>
    <div class="card"><h3>Network policies</h3><div class="blist" id="lPolicies"></div></div>
    <div class="card"><h3>Calling stations (MAC / IP)</h3><div class="blist" id="lCalling"></div></div>
    <div class="card"><h3>Authentication methods</h3><div class="blist" id="lAuth"></div></div>
  </section>

  </main>

  <main class="tabpane" id="tab-events" hidden>
  <section class="card">
    <h3>Event log <span class="tip" id="tblInfo"></span></h3>
    <div class="ev-strip" id="evStrip"></div>
    <div class="tbl-wrap"><table>
      <thead><tr id="thead"></tr></thead>
      <tbody id="tbody"></tbody>
    </table></div>
    <div class="pager">
      <button class="btn" id="pgFirst">«</button><button class="btn" id="pgPrev">‹ Prev</button>
      <span id="pgInfo"></span>
      <button class="btn" id="pgNext">Next ›</button><button class="btn" id="pgLast">»</button>
      <div class="spacer"></div>
      <select class="field" id="pgSize"><option>50</option><option selected>100</option><option>250</option><option>500</option></select>
    </div>
  </section>
  </main>
  <div class="foot" id="foot"></div>
</div>

<div class="tooltip" id="tt"></div>
<div class="toast" id="toast"></div>
<div class="overlay" id="overlay"></div>
<aside class="drawer" id="drawer"><div class="dh" id="dh"></div><div class="db" id="db"></div></aside>

<!--__DATA__-->
<script>
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => Number(n).toLocaleString('en-US');
const pct = (a, b) => b ? (100 * a / b) : 0;
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const tick = () => new Promise(r => setTimeout(r, 16));

const OUTCOMES = [
  {k:'accept', label:'Accepted'}, {k:'reject', label:'Rejected'}, {k:'discard', label:'Discarded'},
  {k:'challenge', label:'Challenge'}, {k:'request', label:'Request'}, {k:'accounting', label:'Accounting'},
  {k:'other', label:'Other'}];
const OUT_LABEL = Object.fromEntries(OUTCOMES.map(o => [o.k, o.label]));
const color = k => css('--' + k);
const DOWS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const FACET_LABEL = {user:'User', client:'Client', policy:'Policy', calling:'Calling station', rc:'Reason',
  auth:'Auth method', dow:'Weekday', hour:'Hour', srv:'Server', nas_ip:'NAS IP', client_ip:'Client IP', out:'Outcome'};
const SEARCH_COLS = ['user','calling','called','client','client_ip','nas_ip','nas_id','policy','crp','srv','eap',
                     'auth','rc','out','acct','port_type','file'];

let D, N = 0, TS, COL = {}, IDX = new Uint32Array(0), SORTED = null, LC = {}, RANKS = {};
let OUT_TOTAL = {}, FAILF, ACCTF, DECF, CLIENT_IP_OF, presentOutcomes = [];
const reasonText = rc => (rc === '' || rc == null) ? '' : (D.reasons[rc] || ('Reason code ' + rc));
const packetText = pt => (pt === '' || pt == null) ? '' : (D.packets[pt] || ('Packet type ' + pt));
const val = (c, i) => COL[c].dict[COL[c].codes[i]];

const S = {q:'', outs:new Set(), srv:-1, from:null, to:null, facets:{}, sort:{k:'ts', dir:-1}, page:0, size:100, tab:'overview'};
let LAST_ST = null;

// ---------- loading ----------
function setLoader(msg) { $('#loaderMsg').textContent = msg; }
function b64ToBytes(s) { const bin = atob(s), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; }
async function decode(spec) {
  const stream = new Blob([b64ToBytes(spec.data)]).stream().pipeThrough(new DecompressionStream('gzip'));
  const buf = await new Response(stream).arrayBuffer();
  return spec.type === 'u8' ? new Uint8Array(buf) : spec.type === 'u16' ? new Uint16Array(buf)
       : spec.type === 'u32' ? new Uint32Array(buf) : new Float64Array(buf);
}
async function boot() {
  try {
    D = window.NPS_DATA;
    if (!D) { setLoader('No data found.'); return; }
    N = D.n;
    setLoader(`Decoding ${fmt(N)} events…`); await tick();
    const t = await decode(D.ts);
    TS = new Float64Array(N);
    if (D.ts.enc === 'delta') { let acc = D.t0; for (let i = 0; i < N; i++) { acc += t[i]; TS[i] = acc; } } else TS.set(t);
    for (const [name, spec] of Object.entries(D.cols)) COL[name] = {dict: spec.dict, codes: await decode(spec)};
    D.ts.data = null; for (const k in D.cols) D.cols[k].data = null;
    setLoader('Building indexes…'); await tick();
    prepare(); initHeader(); wire();
    try { const t = localStorage.getItem('nps-tab'); if (t === 'events') setTab('events'); } catch (_) {}
    renderFacets(); applyFilters();
    $('#loader').classList.add('hide');
  } catch (e) {
    setLoader('Failed to load data: ' + e); console.error(e);
  }
}

function prepare() {
  const od = COL.out.dict, oc = COL.out.codes, cnt = new Uint32Array(od.length);
  for (let i = 0; i < N; i++) cnt[oc[i]]++;
  od.forEach((k, c) => OUT_TOTAL[k] = (OUT_TOTAL[k] || 0) + cnt[c]);
  presentOutcomes = OUTCOMES.filter(o => OUT_TOTAL[o.k]);
  S.outs = new Set(presentOutcomes.map(o => o.k));
  FAILF = Uint8Array.from(od, k => k === 'reject' || k === 'discard');
  ACCTF = Uint8Array.from(od, k => k === 'accounting');
  DECF = Uint8Array.from(od, k => k === 'accept' || k === 'reject');
  for (const c of SEARCH_COLS) LC[c] = COL[c].dict.map(v => {
    let s = String(v).toLowerCase();
    if (c === 'rc' && v !== '') s += ' ' + reasonText(v).toLowerCase();
    if (c === 'out') s += ' ' + (OUT_LABEL[v] || '').toLowerCase();
    return s;
  });
  CLIENT_IP_OF = new Int32Array(COL.client.dict.length).fill(-1);
  const cc = COL.client.codes, ci = COL.client_ip.codes;
  for (let i = 0; i < N; i++) if (CLIENT_IP_OF[cc[i]] < 0) CLIENT_IP_OF[cc[i]] = ci[i];
}

// ---------- time helpers (log-local time, shown as-is) ----------
const p2 = n => String(n).padStart(2, '0');
function fmtDate(ts, withMs) {
  const d = new Date(ts);
  let s = `${d.getUTCFullYear()}-${p2(d.getUTCMonth()+1)}-${p2(d.getUTCDate())} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
  if (withMs) s += '.' + String(d.getUTCMilliseconds()).padStart(3, '0');
  return s;
}
const toInput = ts => ts == null ? '' : fmtDate(ts).replace(' ', 'T');
const fromInput = v => v ? Date.parse(v + (v.length === 16 ? ':00' : '') + 'Z') : null;
const dowOf = t => (Math.floor(t / 86400000) + 3) % 7;
const hourOf = t => Math.floor(t / 3600000) % 24;
function lowerBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >>> 1; if (a[m] < x) lo = m + 1; else hi = m; } return lo; }
function upperBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >>> 1; if (a[m] <= x) lo = m + 1; else hi = m; } return lo; }

// ---------- filtering ----------
function termMasks() {
  const terms = S.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return terms.map(t => {
    const list = [];
    for (const c of SEARCH_COLS) {
      const lc = LC[c]; let m = null;
      for (let j = 0; j < lc.length; j++) if (lc[j].includes(t)) { if (!m) m = new Uint8Array(lc.length); m[j] = 1; }
      if (m) list.push([COL[c].codes, m]);
    }
    return list;
  });
}
function applyFilters() {
  const allow = Uint8Array.from(COL.out.dict, k => S.outs.has(k) ? 1 : 0);
  const oc = COL.out.codes, sc = COL.srv.codes, srv = S.srv;
  const lo = S.from != null ? lowerBound(TS, S.from) : 0;
  const hi = S.to != null ? upperBound(TS, S.to) : N;
  const fac = Object.entries(S.facets).filter(([k]) => k !== 'dow' && k !== 'hour').map(([k, v]) => [COL[k].codes, v]);
  const dowF = S.facets.dow, hourF = S.facets.hour, timeF = dowF != null || hourF != null;
  const tm = termMasks();
  if (tm.some(l => !l.length) || hi <= lo) { IDX = new Uint32Array(0); SORTED = null; S.page = 0; renderAll(); return; }
  const out = new Uint32Array(hi - lo); let n = 0;
  outer: for (let i = lo; i < hi; i++) {
    if (!allow[oc[i]]) continue;
    if (srv >= 0 && sc[i] !== srv) continue;
    for (let f = 0; f < fac.length; f++) if (fac[f][0][i] !== fac[f][1]) continue outer;
    if (timeF) { const t = TS[i]; if (dowF != null && dowOf(t) !== dowF) continue; if (hourF != null && hourOf(t) !== hourF) continue; }
    for (let a = 0; a < tm.length; a++) {
      const l = tm[a]; let m = false;
      for (let b = 0; b < l.length; b++) if (l[b][1][l[b][0][i]]) { m = true; break; }
      if (!m) continue outer;
    }
    out[n++] = i;
  }
  IDX = out.subarray(0, n); SORTED = null; S.page = 0;
  renderAll();
}

// ---------- header / filters ----------
function initHeader() {
  const m = D.meta;
  const range = N ? `${fmtDate(TS[0])} → ${fmtDate(TS[N - 1])}` : 'no data';
  $('#subtitle').textContent = `${fmt(N)} events · ${m.files.length} file(s) · ${range}` +
    (m.excluded ? ` · ${fmt(m.excluded)} excluded (${m.exclude_patterns.join(', ')})` : '');
  $('#foot').textContent = `${m.app} v${m.version} · generated ${m.generated} · parsed in ${m.parse_seconds}s` +
     (m.skipped ? ` · ${fmt(m.skipped)} unrecognized lines skipped` : '') + ` · formats: ${Object.keys(m.formats).join(', ') || 'n/a'}`;
  const servers = COL.srv.dict.map((s, c) => [s, c]).filter(([s]) => s).sort((a, b) => a[0].localeCompare(b[0]));
  $('#srv').innerHTML = `<option value="-1">All servers (${servers.length})</option>` + servers.map(([s, c]) => `<option value="${c}">${esc(s)}</option>`).join('');
  if (servers.length < 2) $('#srv').style.display = 'none';
}
function renderChips() {
  $('#chips').innerHTML = presentOutcomes.map(o =>
    `<span class="chip ${S.outs.has(o.k) ? '' : 'off'}" data-k="${o.k}" title="Click to toggle, double-click to show only this">
      <span class="dot" style="background:${color(o.k)}"></span>${o.label}<span class="n">${fmt(OUT_TOTAL[o.k] || 0)}</span></span>`).join('');
}
function facetText(k, v) {
  if (k === 'dow') return DOWS[v];
  if (k === 'hour') return `${v}:00`;
  const s = COL[k].dict[v];
  if (k === 'rc') return `${s} – ${reasonText(s)}`;
  if (k === 'out') return OUT_LABEL[s] || s;
  return s || '(empty)';
}
function renderFacets() {
  requestAnimationFrame(sizeTable);
  $('#facets').innerHTML = Object.entries(S.facets).map(([k, v]) =>
    `<span class="facet">${FACET_LABEL[k] || k}: <b>${esc(facetText(k, v))}</b><button data-k="${k}" title="Remove">×</button></span>`).join('');
  updateDpLabel();
}
function addFacet(k, v) { S.facets[k] = v; renderFacets(); applyFilters(); }

// ---------- one-pass statistics ----------
function computeStats() {
  const oc = COL.out.codes, uc = COL.user.codes, cc = COL.client.codes, ic = COL.client_ip.codes,
        pc = COL.policy.codes, kc = COL.calling.codes, ac = COL.auth.codes, rc = COL.rc.codes;
  const z = c => new Uint32Array(COL[c].dict.length);
  const st = {out:z('out'), rcF:z('rc'), userF:z('user'), user:z('user'), userAll:z('user'), client:z('client'),
              clientIp:z('client_ip'), policy:z('policy'), calling:z('calling'), auth:z('auth'),
              heat:new Uint32Array(168), heatF:new Uint32Array(168)};
  for (let k = 0; k < IDX.length; k++) {
    const i = IDX[k], o = oc[i], t = TS[i];
    const h = ((Math.floor(t / 86400000) + 3) % 7) * 24 + Math.floor(t / 3600000) % 24;
    st.out[o]++; st.heat[h]++; st.userAll[uc[i]]++;
    if (FAILF[o]) { st.rcF[rc[i]]++; st.userF[uc[i]]++; st.heatF[h]++; }
    if (!ACCTF[o]) st.user[uc[i]]++;
    if (DECF[o]) st.policy[pc[i]]++;
    st.client[cc[i]]++; st.clientIp[ic[i]]++; st.calling[kc[i]]++; st.auth[ac[i]]++;
  }
  st.byOut = {}; COL.out.dict.forEach((k, c) => st.byOut[k] = (st.byOut[k] || 0) + st.out[c]);
  return st;
}
const uniq = (counts, dict) => { let n = 0; for (let c = 0; c < counts.length; c++) if (counts[c] && dict[c] !== '') n++; return n; };

// ---------- KPIs ----------
function renderKpis(st) {
  const c = st.byOut, acc = c.accept || 0, rej = c.reject || 0, dis = c.discard || 0, att = acc + rej;
  const rate = pct(acc, att);
  const rateColor = rate >= 90 ? color('accept') : rate >= 70 ? color('discard') : color('reject');
  const clients = uniq(st.clientIp, COL.client_ip.dict) || uniq(st.client, COL.client.dict);
  const tiles = [
    {l:'Total events', v:fmt(IDX.length), h:`of ${fmt(N)} loaded`, c:'--accent'},
    {l:'Success rate', v:att ? rate.toFixed(1) + '%' : '—', h:`${fmt(att)} auth decisions`, bar:rate, bc:rateColor, c:'--accept'},
    {l:'Accepted', v:fmt(acc), h:`${pct(acc, IDX.length).toFixed(1)}% of events`, c:'--accept'},
    {l:'Rejected', v:fmt(rej), h:dis ? `+ ${fmt(dis)} discarded` : 'no discarded requests', c:'--reject'},
    {l:'Unique users', v:fmt(uniq(st.userAll, COL.user.dict)), h:`${fmt(uniq(st.calling, COL.calling.dict))} calling stations`, c:'--challenge'},
    {l:'RADIUS clients', v:fmt(clients), h:`${fmt(c.accounting || 0)} accounting records`, c:'--accounting'},
  ];
  $('#kpis').innerHTML = tiles.map(t => `<div class="card kpi">
    <div class="label"><i style="background:var(${t.c})"></i>${t.l}</div>
    <div class="value">${t.v}</div><div class="hint">${t.h}</div>
    ${t.bar != null ? `<div class="ring"><div style="width:${t.bar}%;background:${t.bc}"></div></div>` : ''}</div>`).join('');
}

// ---------- timeline ----------
const STEPS = [1e3, 5e3, 15e3, 30e3, 60e3, 300e3, 900e3, 1800e3, 3600e3, 10800e3, 21600e3, 43200e3, 86400e3, 604800e3, 2592000e3];
const STEP_LABEL = {1e3:'1 s',5e3:'5 s',15e3:'15 s',30e3:'30 s',60e3:'1 min',300e3:'5 min',900e3:'15 min',1800e3:'30 min',
  3600e3:'1 hour',10800e3:'3 hours',21600e3:'6 hours',43200e3:'12 hours',86400e3:'1 day',604800e3:'7 days',2592000e3:'30 days'};
let TL = null;
function renderTimeline() {
  const el = $('#timeline');
  const W = el.clientWidth || 800, H = el.clientHeight || 240, pad = {l:52, r:10, t:10, b:26};
  if (!IDX.length) { el.innerHTML = '<div class="empty">No events match the current filters</div>'; $('#tlLegend').innerHTML = ''; TL = null; return; }
  const t0 = S.from ?? TS[IDX[0]], t1 = S.to ?? TS[IDX[IDX.length - 1]];
  const span = Math.max(1000, t1 - t0);
  const maxBars = Math.max(20, Math.min(180, Math.floor((W - pad.l - pad.r) / 7)));
  const step = STEPS.find(s => span / s <= maxBars) || STEPS[STEPS.length - 1];
  const start = Math.floor(t0 / step) * step;
  const n = Math.max(1, Math.ceil((t1 - start + 1) / step));
  const od = COL.out.dict, nO = od.length, oc = COL.out.codes;
  const B = new Uint32Array(n * nO);
  for (let k = 0; k < IDX.length; k++) {
    const i = IDX[k]; let b = Math.floor((TS[i] - start) / step); if (b < 0) b = 0; else if (b >= n) b = n - 1;
    B[b * nO + oc[i]]++;
  }
  const keys = presentOutcomes.map(o => o.k).filter(k => S.outs.has(k));
  const kc = keys.map(k => od.indexOf(k));
  const get = (b, j) => B[b * nO + kc[j]];
  let maxV = 1;
  for (let b = 0; b < n; b++) { let s = 0; for (let j = 0; j < kc.length; j++) s += get(b, j); if (s > maxV) maxV = s; }
  const nice = niceMax(maxV), iw = W - pad.l - pad.r, ih = H - pad.t - pad.b, bw = iw / n;
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><g class="axis">`;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih - ih * i / 4;
    svg += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}"/><text x="${pad.l - 8}" y="${y + 3}" text-anchor="end">${shortNum(nice * i / 4)}</text>`;
  }
  const every = Math.max(1, Math.ceil(n / Math.floor(iw / 95)));
  for (let i = 0; i < n; i += every)
    svg += `<text x="${pad.l + i * bw + bw / 2}" y="${H - 8}" text-anchor="middle">${esc(tickLabel(start + i * step, step, span))}</text>`;
  svg += '</g><g>';
  const gap = bw > 6 ? Math.min(3, bw * .18) : 0.5;
  for (let b = 0; b < n; b++) {
    let y = pad.t + ih;
    for (let j = 0; j < keys.length; j++) {
      const v = get(b, j); if (!v) continue;
      const h = ih * v / nice; y -= h;
      svg += `<rect x="${pad.l + b * bw + gap / 2}" y="${y}" width="${Math.max(.5, bw - gap)}" height="${h}" rx="${bw > 8 ? 2 : 0}" fill="${color(keys[j])}"/>`;
    }
  }
  svg += `</g><rect id="tlHover" x="0" y="${pad.t}" width="${bw}" height="${ih}" fill="var(--text)" opacity="0"/>`;
  svg += `<rect id="tlBrush" x="0" y="${pad.t}" width="0" height="${ih}" fill="var(--accent)" opacity=".18" stroke="var(--accent)"/></svg>`;
  el.innerHTML = svg;
  $('#bucketInfo').textContent = 'bucket: ' + (STEP_LABEL[step] || step / 1000 + ' s');
  $('#tlLegend').innerHTML = keys.map(k => `<span><i style="background:${color(k)}"></i>${OUT_LABEL[k]}</span>`).join('');
  TL = {start, step, n, bw, pad, W, keys, get};
}
function niceMax(v) { if (v <= 20) return Math.max(4, Math.ceil(v / 4) * 4); const p = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p; return v; }
function shortNum(v) { return v >= 1e6 ? (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'k' : Math.round(v * 10) / 10; }
function tickLabel(ts, step, span) {
  const s = fmtDate(ts);
  if (step >= 86400e3) return s.slice(5, 10);
  if (span > 86400e3) return s.slice(5, 16);
  if (step < 60e3) return s.slice(11, 19);
  return s.slice(11, 16);
}
function tlIndex(ev) {
  const svg = $('#timeline svg'); if (!svg || !TL) return -1;
  const r = svg.getBoundingClientRect();
  return Math.floor(((ev.clientX - r.left) * TL.W / r.width - TL.pad.l) / TL.bw);
}
function initTimelineEvents() {
  const el = $('#timeline'), tt = $('#tt'); let dragFrom = null;
  el.addEventListener('mousemove', ev => {
    const i = tlIndex(ev), hov = $('#tlHover');
    if (!TL || i < 0 || i >= TL.n) { tt.style.display = 'none'; if (hov) hov.setAttribute('opacity', 0); return; }
    hov.setAttribute('x', TL.pad.l + i * TL.bw); hov.setAttribute('width', TL.bw); hov.setAttribute('opacity', .06);
    if (dragFrom != null) {
      const a = Math.min(dragFrom, i), b = Math.max(dragFrom, i), br = $('#tlBrush');
      br.setAttribute('x', TL.pad.l + a * TL.bw); br.setAttribute('width', (b - a + 1) * TL.bw);
    }
    let tot = 0, rows = '';
    TL.keys.forEach((k, j) => { const v = TL.get(i, j); tot += v; if (v) rows += `<div class="tt-r"><span><i style="background:${color(k)}"></i>${OUT_LABEL[k]}</span><b>${fmt(v)}</b></div>`; });
    tt.innerHTML = `<div class="tt-h">${fmtDate(TL.start + i * TL.step)}</div>${rows}
      <div class="tt-r" style="margin-top:4px;border-top:1px solid var(--card-b);padding-top:4px"><span>Total</span><b>${fmt(tot)}</b></div>`;
    showTip(ev);
  });
  el.addEventListener('mouseleave', () => { tt.style.display = 'none'; const h = $('#tlHover'); if (h) h.setAttribute('opacity', 0); });
  el.addEventListener('mousedown', ev => { const i = tlIndex(ev); if (TL && i >= 0 && i < TL.n) dragFrom = i; ev.preventDefault(); });
  window.addEventListener('mouseup', ev => {
    if (dragFrom == null || !TL) { dragFrom = null; return; }
    const i = Math.max(0, Math.min(TL.n - 1, tlIndex(ev)));
    const a = Math.min(dragFrom, i), b = Math.max(dragFrom, i); dragFrom = null;
    if (b > a) { S.from = TL.start + a * TL.step; S.to = TL.start + (b + 1) * TL.step - 1; renderFacets(); applyFilters(); }
    else { const br = $('#tlBrush'); if (br) br.setAttribute('width', 0); }
  });
  el.addEventListener('dblclick', () => { S.from = S.to = null; renderFacets(); applyFilters(); });
}
function showTip(ev) {
  const tt = $('#tt'); tt.style.display = 'block';
  const w = tt.offsetWidth, h = tt.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tt.style.left = x + 'px'; tt.style.top = y + 'px';
}

// ---------- donut ----------
function renderDonut(st) {
  const items = presentOutcomes.map(o => ({...o, v:st.byOut[o.k] || 0})).filter(o => o.v);
  const tot = IDX.length, R = 80, r0 = 56, cx = 95, cy = 95;
  let a = -Math.PI / 2, svg = `<svg viewBox="0 0 190 190">`;
  if (!tot) svg += `<circle cx="${cx}" cy="${cy}" r="${(R + r0) / 2}" fill="none" stroke="var(--grid)" stroke-width="${R - r0}"/>`;
  const p = (rad, ang) => `${cx + rad * Math.cos(ang)},${cy + rad * Math.sin(ang)}`;
  for (const it of items) {
    if (it.v === tot) { svg += `<circle cx="${cx}" cy="${cy}" r="${(R + r0) / 2}" fill="none" stroke="${color(it.k)}" stroke-width="${R - r0}"/>`; break; }
    const ang = 2 * Math.PI * it.v / tot, a2 = a + ang, large = ang > Math.PI ? 1 : 0;
    svg += `<path d="M${p(R, a)} A${R},${R} 0 ${large} 1 ${p(R, a2)} L${p(r0, a2)} A${r0},${r0} 0 ${large} 0 ${p(r0, a)}Z" fill="${color(it.k)}" stroke="var(--bg)" stroke-width="1.5"><title>${it.label}: ${fmt(it.v)}</title></path>`;
    a = a2;
  }
  svg += `<text x="${cx}" y="${cy - 2}" text-anchor="middle" font-size="22" font-weight="650" fill="var(--text)">${shortNum(tot)}</text>
          <text x="${cx}" y="${cy + 17}" text-anchor="middle" font-size="11" fill="var(--muted)">events</text></svg>`;
  $('#donut').innerHTML = svg;
  $('#donutLegend').innerHTML = items.map(it => `<div data-k="${it.k}" title="Show only ${it.label}"><i style="background:${color(it.k)}"></i>${it.label}
     <b>${fmt(it.v)}</b><em>${pct(it.v, tot).toFixed(1)}%</em></div>`).join('') || '<div class="empty">No data</div>';
}

// ---------- bar lists ----------
function topN(counts, dict, n = 25, keepEmpty = false) {
  const arr = [];
  for (let c = 0; c < counts.length; c++) if (counts[c] && (keepEmpty || dict[c] !== '')) arr.push([c, counts[c]]);
  arr.sort((a, b) => b[1] - a[1]);
  return arr.slice(0, n);
}
function barList(sel, col, counts, colr, labelFn, keepEmpty) {
  const el = $(sel), dict = COL[col].dict, entries = topN(counts, dict, 25, keepEmpty);
  if (!entries.length) { el.innerHTML = '<div class="empty">Nothing to show</div>'; return; }
  const max = entries[0][1];
  el.innerHTML = entries.map(([c, v]) => `<div class="brow" data-f="${col}" data-v="${c}" title="${esc(dict[c])} — click to filter">
     <div class="fill" style="width:${pct(v, max)}%;background:${colr}"></div>
     <div class="lbl">${labelFn ? labelFn(c, dict[c]) : esc(dict[c] || '(empty)')}</div><div class="cnt">${fmt(v)}</div></div>`).join('');
}
function renderLists(st) {
  barList('#lReasons', 'rc', st.rcF, color('reject'), (c, v) => `<span class="code">${esc(v)}</span> ${esc(reasonText(v))}`);
  barList('#lFailUsers', 'user', st.userF, color('reject'), null, true);
  barList('#lUsers', 'user', st.user, color('challenge'));
  barList('#lClients', 'client', st.client, color('accounting'), (c, v) => {
    const ip = CLIENT_IP_OF[c] >= 0 ? COL.client_ip.dict[CLIENT_IP_OF[c]] : ''; return esc(v) + (ip ? `<small>${esc(ip)}</small>` : ''); });
  barList('#lPolicies', 'policy', st.policy, color('accept'));
  barList('#lCalling', 'calling', st.calling, color('discard'), (c, v) => `<span class="mono">${esc(v)}</span>`);
  barList('#lAuth', 'auth', st.auth, css('--accent'));
}

// ---------- heatmap ----------
function renderHeatmap(st) {
  const max = Math.max(1, ...st.heat), acc = css('--accent');
  let h = '<div></div>' + Array.from({length:24}, (_, i) => `<div class="hh">${i % 3 === 0 ? i : ''}</div>`).join('');
  for (let d = 0; d < 7; d++) {
    h += `<div class="hl">${DOWS[d]}</div>`;
    for (let hr = 0; hr < 24; hr++) {
      const v = st.heat[d * 24 + hr], f = st.heatF[d * 24 + hr], a = v ? .12 + .88 * Math.sqrt(v / max) : 1;
      const bg = v ? (f / v > .5 ? color('reject') : acc) : 'var(--grid)';
      h += `<div class="hc" data-d="${d}" data-h="${hr}" style="background:${bg};opacity:${a}" title="${DOWS[d]} ${hr}:00–${hr}:59 · ${fmt(v)} events · ${fmt(f)} failures"></div>`;
    }
  }
  $('#heatmap').innerHTML = h;
}

// ---------- table ----------
const TCOLS = [
  {k:'ts', l:'Time', f:i => `<span class="mono">${fmtDate(TS[i])}</span>`},
  {k:'out', l:'Outcome', f:i => badge(val('out', i))},
  {k:'user', l:'User', f:i => esc(val('user', i)) || '<span class="muted">—</span>'},
  {k:'calling', l:'Calling station', f:i => `<span class="mono">${esc(val('calling', i))}</span>`},
  {k:'client', l:'RADIUS client', f:i => esc(val('client', i) || val('client_ip', i))},
  {k:'policy', l:'Network policy', f:i => esc(val('policy', i))},
  {k:'auth', l:'Auth', f:i => esc(val('auth', i))},
  {k:'rc', l:'Reason', f:i => { const r = val('rc', i); return r === '' ? '' : `<span class="code">${esc(r)}</span> <span class="${r !== '0' ? '' : 'muted'}">${esc(reasonText(r))}</span>`; }},
  {k:'srv', l:'Server', f:i => esc(val('srv', i))},
];
function badge(out) {
  const col = color(out);
  return `<span class="badge" style="background:color-mix(in srgb, ${col} 18%, transparent);color:${col}">
    <span style="width:6px;height:6px;border-radius:50%;background:${col}"></span>${OUT_LABEL[out] || out}</span>`;
}
function getRank(k) {
  if (RANKS[k]) return RANKS[k];
  const dict = COL[k].dict, numeric = k === 'rc' || k === 'pt';
  const order = dict.map((v, c) => c).sort((a, b) => {
    const x = dict[a], y = dict[b];
    if (k === 'out') return (OUT_LABEL[x] || x).localeCompare(OUT_LABEL[y] || y);
    if (numeric) return (x === '' ? 1e9 : +x) - (y === '' ? 1e9 : +y);
    if (x === '' || y === '') return x === '' ? 1 : -1;
    return x.localeCompare(y, undefined, {sensitivity:'base'});
  });
  const r = new Uint32Array(dict.length); order.forEach((c, i) => r[c] = i);
  return RANKS[k] = r;
}
function getSorted() {
  if (SORTED) return SORTED;
  const {k, dir} = S.sort, n = IDX.length;
  if (k === 'ts') {
    if (dir > 0) SORTED = IDX;
    else { const r = new Uint32Array(n); for (let a = 0, b = n - 1; b >= 0; a++, b--) r[a] = IDX[b]; SORTED = r; }
    return SORTED;
  }
  const rank = getRank(k), codes = COL[k].codes, R = rank.length, cnt = new Uint32Array(R + 1), out = new Uint32Array(n);
  for (let a = 0; a < n; a++) cnt[rank[codes[IDX[a]]] + 1]++;
  for (let r = 0; r < R; r++) cnt[r + 1] += cnt[r];
  for (let a = 0; a < n; a++) { const i = IDX[a]; out[cnt[rank[codes[i]]]++] = i; }
  if (dir < 0) out.reverse();
  return SORTED = out;
}
function renderTable() {
  const rows = getSorted(), pages = Math.max(1, Math.ceil(rows.length / S.size));
  S.page = Math.min(S.page, pages - 1);
  const slice = rows.subarray(S.page * S.size, (S.page + 1) * S.size);
  $('#thead').innerHTML = TCOLS.map(c => `<th data-k="${c.k}">${c.l}${S.sort.k === c.k ? (S.sort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('');
  let h = '';
  for (const i of slice) h += `<tr data-i="${i}">${TCOLS.map(c => `<td>${c.f(i)}</td>`).join('')}</tr>`;
  $('#tbody').innerHTML = h || `<tr><td colspan="${TCOLS.length}" class="empty">No events match the current filters</td></tr>`;
  $('#pgInfo').textContent = `Page ${fmt(S.page + 1)} / ${fmt(pages)}`;
  $('#tblInfo').textContent = `${fmt(rows.length)} matching · click a row for details`;
}

// ---------- drawer ----------
function openPanel(head, body) {
  $('#dh').innerHTML = head + '<button class="btn" id="dClose">✕</button>';
  $('#db').innerHTML = body;
  $('#drawer').classList.add('open'); $('#overlay').classList.add('open');
  $('#dClose').onclick = closeDrawer;
}
function closeDrawer() { $('#drawer').classList.remove('open'); $('#overlay').classList.remove('open'); }
function openDrawer(i) {
  const out = val('out', i), col = color(out), rc = val('rc', i), user = val('user', i);
  const link = k => { const v = val(k, i); return v ? `<a class="linkish" data-f="${k}" data-v="${COL[k].codes[i]}">${esc(v)}</a>` : ''; };
  const kv = [
    ['User name', link('user')], ['Calling station', link('calling')], ['Called station', esc(val('called', i))],
    ['RADIUS client', link('client')], ['Client IP', link('client_ip')],
    ['NAS IP / identifier', esc([val('nas_ip', i), val('nas_id', i)].filter(Boolean).join(' / '))],
    ['NAS port type', esc(val('port_type', i))], ['Network policy', link('policy')],
    ['Connection request policy', esc(val('crp', i))], ['Authentication', esc([val('auth', i), val('eap', i)].filter(Boolean).join(' · '))],
    ['Accounting status', esc(val('acct', i))], ['Source file', esc(val('file', i))],
  ].filter(([, v]) => v);
  let body = '';
  if (rc !== '') body += `<div class="reason-box" style="border-color:color-mix(in srgb, ${col} 45%, transparent);background:color-mix(in srgb, ${col} 10%, transparent)">
      <div style="font-weight:600">Reason code ${esc(rc)}</div><div style="margin-top:3px">${esc(reasonText(rc))}</div></div>`;
  body += `<div class="kv">${kv.map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join('')}</div>`;
  if (user) {
    const uc = COL.user.codes, me = uc[i], oc = COL.out.codes, cnt = new Uint32Array(COL.out.dict.length);
    for (let j = 0; j < N; j++) if (uc[j] === me) cnt[oc[j]]++;
    body += `<div class="sec">This user overall</div><div class="chips">${COL.out.dict.map((k, c) => cnt[c] ?
      `<span class="chip"><span class="dot" style="background:${color(k)}"></span>${OUT_LABEL[k]}<span class="n">${fmt(cnt[c])}</span></span>` : '').join('')}</div>`;
  }
  const acts = [['user', 'Show all events of this user'], ['client', 'Show all events of this client'], ['calling', 'Show all events of this station']]
    .filter(([k]) => val(k, i)).map(([k, l]) => `<button class="btn" data-goto="${k}" data-v="${COL[k].codes[i]}">${l} →</button>`).join('');
  if (acts) body += `<div class="drawer-actions">${acts}</div>`;
  body += `<div class="sec">All logged attributes</div><div id="rawBox" class="muted">Loading…</div>`;
  openPanel(`<div style="flex:1;min-width:0"><div style="margin-bottom:8px">${badge(out)} <span class="muted" style="margin-left:6px">${esc(packetText(val('pt', i)))}</span></div>
      <h2>${esc(user || '(no user name)')}</h2><div class="muted mono">${fmtDate(TS[i], true)} · ${esc(val('srv', i))}</div></div>`, body);
  loadRaw(i);
}
async function loadRaw(i) {
  const box = $('#rawBox');
  const show = r => {
    if (!r || r.error) { box.textContent = 'Could not read raw attributes' + (r && r.error ? ': ' + r.error : ''); return; }
    const f = r.fields || {};
    box.className = '';
    box.innerHTML = `<div class="muted" style="font-size:11.5px;margin-bottom:6px">${esc(r.format)} · ${esc(r.source)}</div><div class="kv">` +
      Object.keys(f).map(k => `<div>${esc(k)}</div><div class="mono">${esc(f[k])}</div>`).join('') + '</div>';
  };
  if (D.raw) return show(D.raw[i]);
  const api = window.pywebview && window.pywebview.api;
  if (api && api.get_raw) { try { show(await api.get_raw(i)); } catch (e) { show({error:String(e)}); } return; }
  box.textContent = 'Raw attributes are available in the application window (or in exports with up to 20,000 events).';
}

// ---------- CSV export ----------
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('show'), 3500); }
function exportCsv() {
  const LIMIT = 500000, rows = getSorted(), n = Math.min(rows.length, LIMIT);
  const cols = ['out','pt','rc','srv','user','calling','called','client','client_ip','nas_ip','nas_id','policy','crp','auth','eap','acct','port_type','file'];
  const q = v => { v = String(v ?? ''); return /[",\n;]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  const parts = ['\ufeff' + ['time'].concat(cols, ['reason']).join(',') + '\r\n'];
  let buf = [];
  for (let a = 0; a < n; a++) {
    const i = rows[a];
    buf.push([fmtDate(TS[i], true)].concat(cols.map(c => q(val(c, i))), [q(reasonText(val('rc', i)))]).join(','));
    if (buf.length === 20000) { parts.push(buf.join('\r\n') + '\r\n'); buf = []; }
  }
  if (buf.length) parts.push(buf.join('\r\n') + '\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(parts, {type:'text/csv;charset=utf-8'}));
  a.download = `nps_events_${fmtDate(Date.now()).replace(/[: ]/g, '-')}.csv`;
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
  toast(rows.length > LIMIT ? `Exported first ${fmt(LIMIT)} of ${fmt(rows.length)} events — narrow the filter for the rest` : `Exported ${fmt(n)} events`);
}

// ---------- date range picker ----------
const DAY = 864e5;
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const MON3 = MONTHS.map(m => m.slice(0, 3));
let DAYCNT = null, MIN_DAY = 0, MAX_DAY = 0, DAYMAX = 1;
const DP = {a:null, b:null, hover:null, vy:2026, vm:0};
const dayOf = ts => Math.floor(ts / DAY);
const ymOf = d => { const x = new Date(d * DAY); return [x.getUTCFullYear(), x.getUTCMonth()]; };
const nice = ts => { const x = new Date(ts); return `${MON3[x.getUTCMonth()]} ${x.getUTCDate()}, ${x.getUTCFullYear()} ${p2(x.getUTCHours())}:${p2(x.getUTCMinutes())}`; };
const lastTs = () => TS[N - 1];
const PRESETS = [
  ['Last hour',     () => [lastTs() - 3600e3, lastTs()]],
  ['Last 24 hours', () => [lastTs() - DAY, lastTs()]],
  ['Last 7 days',   () => [lastTs() - 7 * DAY, lastTs()]],
  ['Last 30 days',  () => [lastTs() - 30 * DAY, lastTs()]],
  ['Today',         () => [dayOf(lastTs()) * DAY, (dayOf(lastTs()) + 1) * DAY - 1]],
  ['Yesterday',     () => { const d = dayOf(lastTs()) - 1; return [d * DAY, (d + 1) * DAY - 1]; }],
  ['This week',     () => { const d = dayOf(lastTs()), mon = d - ((d + 3) % 7); return [mon * DAY, (mon + 7) * DAY - 1]; }],
  ['This month',    () => { const [y, m] = ymOf(dayOf(lastTs())); return [Date.UTC(y, m, 1), Date.UTC(y, m + 1, 1) - 1]; }],
  ['Last month',    () => { const [y, m] = ymOf(dayOf(lastTs())); return [Date.UTC(y, m - 1, 1), Date.UTC(y, m, 1) - 1]; }],
  ['All time',      () => [null, null]],
];
function initDayCounts() {
  if (!N) return;
  MIN_DAY = dayOf(TS[0]); MAX_DAY = dayOf(TS[N - 1]);
  DAYCNT = new Uint32Array(MAX_DAY - MIN_DAY + 1);
  for (let i = 0; i < N; i++) DAYCNT[dayOf(TS[i]) - MIN_DAY]++;
  DAYMAX = Math.max(1, ...DAYCNT);
}
function presetIndex() {
  for (let k = 0; k < PRESETS.length; k++) { const [f, t] = PRESETS[k][1](); if (f === S.from && t === S.to) return k; }
  return -1;
}
function updateDpLabel() {
  const btn = $('#dpBtn'); if (!btn) return;
  const pi = presetIndex();
  let txt = 'All time';
  if (S.from != null || S.to != null) txt = pi >= 0 ? PRESETS[pi][0] : `${S.from != null ? nice(S.from) : '…'}  →  ${S.to != null ? nice(S.to) : '…'}`;
  $('#dpLabel').textContent = txt;
  btn.classList.toggle('active', S.from != null || S.to != null);
}
function openDp() {
  if (!DAYCNT) initDayCounts();
  DP.a = S.from != null ? dayOf(S.from) : null;
  DP.b = S.to != null ? dayOf(S.to) : null;
  DP.hover = null;
  let [y, m] = ymOf(DP.b ?? DP.a ?? MAX_DAY);           // selected (or latest) month on the right
  m -= 1; if (m < 0) { m = 11; y--; }
  DP.vy = y; DP.vm = m;
  $('#dpFromT').value = S.from != null ? fmtDate(S.from).slice(11, 16) : '00:00';
  $('#dpToT').value = S.to != null ? fmtDate(S.to).slice(11, 16) : '23:59';
  const pi = presetIndex();
  $('#dpPresets').innerHTML = '<div class="ttl">Quick ranges</div>' +
    PRESETS.map(([l], k) => `<button data-preset="${k}" class="${k === pi || (pi < 0 && k === PRESETS.length - 1 && S.from == null && S.to == null) ? 'on' : ''}">${l}</button>`).join('') +
    '<div class="ttl" style="margin-top:10px">relative to last event</div>';
  renderDp();
  $('#dpPop').hidden = false; $('#dpBtn').classList.add('open');
}
function closeDp() { const p = $('#dpPop'); if (p && !p.hidden) { p.hidden = true; $('#dpBtn').classList.remove('open'); } }
function monthHtml(y, m, nav) {
  const first = Date.UTC(y, m, 1) / DAY, days = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  const lead = (new Date(Date.UTC(y, m, 1)).getUTCDay() + 6) % 7;
  const a = DP.a, b = DP.b ?? (a != null ? DP.hover : null);
  const lo = a == null ? null : (b == null ? a : Math.min(a, b)), hi = a == null ? null : (b == null ? a : Math.max(a, b));
  let h = `<div class="dp-month"><div class="dp-mh">${nav === 'l' ? '<button class="dp-nav" data-nav="-1" title="Previous month">‹</button>' : '<span></span>'}
    <h4>${MONTHS[m]} ${y}</h4>${nav === 'r' ? '<button class="dp-nav" data-nav="1" title="Next month">›</button>' : '<span></span>'}</div><div class="dp-grid">` +
    DOWS.map(d => `<div class="dp-wd">${d.slice(0, 2)}</div>`).join('') + '<div></div>'.repeat(lead);
  for (let i = 0; i < days; i++) {
    const d = first + i, off = d < MIN_DAY || d > MAX_DAY, cnt = off ? 0 : DAYCNT[d - MIN_DAY];
    let cls = 'dp-day';
    if (lo != null && d >= lo && d <= hi) cls += ' in';
    if (lo != null && d === lo) cls += ' s';
    if (hi != null && d === hi) cls += ' e';
    h += `<button class="${cls}" data-d="${d}" ${off ? 'disabled' : ''} title="${off ? 'No data' : fmt(cnt) + ' events'}">${i + 1}` +
         (cnt ? `<i class="bar" style="opacity:${(.25 + .75 * Math.sqrt(cnt / DAYMAX)).toFixed(2)}"></i>` : '') + '</button>';
  }
  return h + '</div></div>';
}
function renderDp() {
  let y2 = DP.vy, m2 = DP.vm + 1; if (m2 > 11) { m2 = 0; y2++; }
  $('#dpMonths').innerHTML = monthHtml(DP.vy, DP.vm, 'l') + monthHtml(y2, m2, 'r');
  dpSummary();
}
function dpRange() {
  if (DP.a == null) return [null, null];
  const b = DP.b ?? DP.a, lo = Math.min(DP.a, b), hi = Math.max(DP.a, b);
  const t = v => { const [h, mi] = String(v || '').split(':').map(Number); return ((h || 0) * 60 + (mi || 0)) * 60000; };
  return [lo * DAY + t($('#dpFromT').value), hi * DAY + t($('#dpToT').value || '23:59') + 59999];
}
function dpSummary() {
  const [f, t] = dpRange(), el = $('#dpSum');
  if (f == null) { el.textContent = `No range selected · data from ${nice(TS[0]).slice(0, 12)} to ${nice(TS[N - 1]).slice(0, 12)}`; return; }
  const n = Math.max(0, upperBound(TS, t) - lowerBound(TS, f));
  el.innerHTML = `<b>${nice(f)}</b> → <b>${nice(t)}</b> · ${fmt(n)} events`;
}
function applyRange(f, t) { S.from = f; S.to = t; closeDp(); renderFacets(); applyFilters(); }
function wireDatePicker() {
  $('#dpBtn').addEventListener('click', e => { e.stopPropagation(); $('#dpPop').hidden ? openDp() : closeDp(); });
  $('#dpPop').addEventListener('click', e => {
    e.stopPropagation();
    const nav = e.target.closest('[data-nav]');
    if (nav) { DP.vm += +nav.dataset.nav; if (DP.vm < 0) { DP.vm = 11; DP.vy--; } if (DP.vm > 11) { DP.vm = 0; DP.vy++; } renderDp(); return; }
    const day = e.target.closest('.dp-day');
    if (day && !day.disabled) {
      const d = +day.dataset.d;
      if (DP.a == null || DP.b != null) { DP.a = d; DP.b = null; } else DP.b = d;
      DP.hover = null; renderDp(); return;
    }
    const pr = e.target.closest('[data-preset]');
    if (pr) { const [f, t] = PRESETS[+pr.dataset.preset][1](); applyRange(f, t); }
  });
  $('#dpPop').addEventListener('dblclick', e => {
    const day = e.target.closest('.dp-day'); if (!day || day.disabled) return;
    DP.a = DP.b = +day.dataset.d; $('#dpFromT').value = '00:00'; $('#dpToT').value = '23:59';
    const [f, t] = dpRange(); applyRange(f, t);
  });
  $('#dpMonths').addEventListener('mouseover', e => {
    if (DP.a == null || DP.b != null) return;
    const day = e.target.closest('.dp-day'); const d = day && !day.disabled ? +day.dataset.d : null;
    if (d !== DP.hover) { DP.hover = d; renderDp(); }
  });
  $('#dpFromT').addEventListener('input', dpSummary);
  $('#dpToT').addEventListener('input', dpSummary);
  $('#dpApply').onclick = () => { const [f, t] = dpRange(); applyRange(f, t); };
  $('#dpClear').onclick = () => applyRange(null, null);
  document.addEventListener('click', e => { if (!e.target.closest('#dp')) closeDp(); });
}

// ---------- wiring ----------
function renderAll() {
  const st = LAST_ST = computeStats();
  renderChips();
  $('#tabCount').textContent = shortNum(IDX.length);
  if (S.tab === 'overview') renderOverview(st); else renderEvents(st);
}
function renderOverview(st) { renderKpis(st); renderTimeline(); renderDonut(st); renderLists(st); renderHeatmap(st); }
function renderEvents(st) {
  $('#evStrip').innerHTML = presentOutcomes.filter(o => st.byOut[o.k]).map(o =>
    `<span class="chip"><span class="dot" style="background:${color(o.k)}"></span>${o.label}<span class="n">${fmt(st.byOut[o.k])}</span></span>`).join('');
  renderTable(); sizeTable();
}
function sizeTable() {
  const w = $('#tab-events .tbl-wrap'); if (!w || S.tab !== 'events') return;
  const probe = 200;
  w.style.height = probe + 'px';
  const app = $('.app'), used = app.offsetTop + app.offsetHeight - probe;   // everything except the table body
  w.style.height = Math.max(160, innerHeight - used) + 'px';
}
function setTab(t) {
  S.tab = t;
  document.querySelectorAll('#tabs .tab').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  $('#tab-overview').hidden = t !== 'overview';
  $('#tab-events').hidden = t !== 'events';
  document.body.classList.toggle('ev-mode', t === 'events');
  try { localStorage.setItem('nps-tab', t); } catch (_) {}
  window.scrollTo(0, 0);
  if (LAST_ST) { if (t === 'overview') renderOverview(LAST_ST); else renderEvents(LAST_ST); }
}

function wire() {
  let deb;
  $('#q').addEventListener('input', e => { clearTimeout(deb); deb = setTimeout(() => { S.q = e.target.value; applyFilters(); }, 250); });
  let clickT;
  $('#chips').addEventListener('click', e => {
    const ch = e.target.closest('.chip'); if (!ch) return;
    clearTimeout(clickT);
    clickT = setTimeout(() => { const k = ch.dataset.k; S.outs.has(k) ? S.outs.delete(k) : S.outs.add(k); applyFilters(); }, 220);
  });
  $('#chips').addEventListener('dblclick', e => { const ch = e.target.closest('.chip'); if (!ch) return; clearTimeout(clickT); S.outs = new Set([ch.dataset.k]); applyFilters(); });
  $('#donutLegend').addEventListener('click', e => {
    const d = e.target.closest('[data-k]'); if (!d) return;
    S.outs = S.outs.size === 1 && S.outs.has(d.dataset.k) ? new Set(presentOutcomes.map(o => o.k)) : new Set([d.dataset.k]);
    applyFilters();
  });
  $('#srv').addEventListener('change', e => { S.srv = +e.target.value; applyFilters(); });
  wireDatePicker();
  $('#btnReset').addEventListener('click', () => {
    S.q = ''; $('#q').value = ''; S.outs = new Set(presentOutcomes.map(o => o.k)); S.srv = -1; $('#srv').value = '-1';
    S.from = S.to = null; S.facets = {}; renderFacets(); applyFilters();
  });
  $('#facets').addEventListener('click', e => { const b = e.target.closest('button[data-k]'); if (!b) return; delete S.facets[b.dataset.k]; renderFacets(); applyFilters(); });
  document.addEventListener('click', e => {
    const gt = e.target.closest('[data-goto]');
    if (gt) { closeDrawer(); S.facets[gt.dataset.goto] = +gt.dataset.v; setTab('events'); renderFacets(); applyFilters(); return; }
    const br = e.target.closest('.brow, .linkish');
    if (br && br.dataset.f) { closeDrawer(); addFacet(br.dataset.f, +br.dataset.v); return; }
    const hc = e.target.closest('.hc');
    if (hc) { S.facets.dow = +hc.dataset.d; S.facets.hour = +hc.dataset.h; renderFacets(); applyFilters(); }
  });
  $('#thead').addEventListener('click', e => {
    const th = e.target.closest('th'); if (!th) return;
    const k = th.dataset.k; S.sort = {k, dir:S.sort.k === k ? -S.sort.dir : (k === 'ts' ? -1 : 1)}; SORTED = null; S.page = 0; renderTable();
  });
  $('#tbody').addEventListener('click', e => { const tr = e.target.closest('tr[data-i]'); if (tr) openDrawer(+tr.dataset.i); });
  const pages = () => Math.max(1, Math.ceil(getSorted().length / S.size));
  const go = p => { S.page = Math.max(0, Math.min(pages() - 1, p)); renderTable(); $('#tab-events .tbl-wrap').scrollTop = 0; };
  $('#pgFirst').onclick = () => go(0);
  $('#pgPrev').onclick = () => go(S.page - 1);
  $('#pgNext').onclick = () => go(S.page + 1);
  $('#pgLast').onclick = () => go(pages() - 1);
  $('#pgSize').onchange = e => { S.size = +e.target.value; go(0); };
  $('#overlay').onclick = closeDrawer;
  $('#tabs').addEventListener('click', e => { const b = e.target.closest('.tab'); if (b) setTab(b.dataset.tab); });
  $('#btnCsv').onclick = exportCsv;
  $('#btnTheme').onclick = () => {
    const h = document.documentElement; h.dataset.theme = h.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('nps-theme', h.dataset.theme); } catch (_) {}
    renderAll();
  };
  $('#btnFiles').onclick = () => {
    const m = D.meta;
    openPanel(`<div style="flex:1"><h2>Source files</h2><div class="muted">${m.files.length} file(s) · formats: ${esc(Object.keys(m.formats).join(', '))}</div></div>`,
      `<div class="kv">${m.files.map(f => `<div class="mono">${esc(f.name)}<br><span class="muted" style="font-size:11px">${esc(f.path)}</span></div>
       <div>${fmt(f.events)} events · ${(f.size / 1048576).toFixed(1)} MB<br><span class="muted">${esc(f.format)}</span></div>`).join('')}</div>`);
  };
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDrawer(); closeDp(); }
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName);
    if (e.key === '/' && !typing) { e.preventDefault(); $('#q').focus(); }
    if (!typing && e.key === '1') setTab('overview');
    if (!typing && e.key === '2') setTab('events');
  });
  let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => S.tab === 'overview' ? renderTimeline() : sizeTable(), 120); });
  initTimelineEvents();
  window.addEventListener('scroll', () => $('#topbar').classList.toggle('stuck', scrollY > 4), {passive:true});
}

try { const t = localStorage.getItem('nps-theme'); if (t) document.documentElement.dataset.theme = t; } catch (_) {}
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        warn("Interrupted.")
        sys.exit(130)
