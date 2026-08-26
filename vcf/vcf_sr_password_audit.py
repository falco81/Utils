#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcf_sr_password_audit.py
========================

Discovers the "VCF services runtime" nodes (VCF/VVF Operations -> Build ->
Lifecycle -> VCF Management) and audits password expiry settings on each node
over SSH using `chage -l <user>` (default: root, vmware-system-user).

Run with no arguments for a fully guided audit. The only thing you supply is
the Operations FQDN plus credentials - everything else is resolved from there:

    1. asks for the Operations FQDN, username and password
    2. GET  /casa/services                -> fleet lifecycle service key + FQDN
       POST /suite-api/api/auth/token/acquire   -> Operations token
       POST /suite-api/api/auth/token/exchange  -> fleet lifecycle bearer token
       GET  https://<fleet>/fleet-lcm/v1/components         -> VSP runtime
       GET  https://<fleet>/fleet-lcm/v1/components/<id>    -> node addresses
       (if no node list is published, the runtime IP pool from
        /components/<id>/config is expanded and probed on TCP/22)
    3. lists the discovered nodes
    4. asks for the SSH login and password used on the nodes
    5. runs `chage -l <that login>` and `chage -l root` on every node
    6. prints the summary table

Login flow on VCF appliance nodes:
    ssh vmware-system-user@<node>
    sudo -i                      <- prompts for the SAME user password
    chage -l root
    chage -l vmware-system-user

Privilege escalation (--escalate):
  auto    - try sudo -n, then sudo -S with password, then interactive shell (default)
  nopass  - sudo -n only (passwordless sudo configured)
  exec    - sudo -S -i with password fed over a PTY
  shell   - interactive shell: send `sudo -i`, answer the password prompt,
            then run the commands inside the root login shell
  none    - run chage directly, no escalation (only useful as root)

Discovery modes (--discover):
  fleetlcm  - fleet-lcm plugin REST API in VCF Operations (default)
  vcenter   - vCenter REST API, VMs matched by name prefix (most reliable)
  range     - IP range from the runtime IP pool + TCP/22 probe
  file      - plain text file, one IP/FQDN per line (# = comment)

TLS: certificate verification is DISABLED by default (VCF appliances ship
self-signed certificates). Use --verify-ssl to turn it back on.

Requirements:
    pip install requests paramiko colorama

Examples (Windows CMD / PowerShell):
    py vcf_sr_password_audit.py
        -> interactive wizard when started with no arguments

    py vcf_sr_password_audit.py --discover vcenter --vm-prefix vvfsr01- ^
        --vc-user "administrator@vsphere.local" ^
        --ssh-user vmware-system-user --csv audit.csv

    py vcf_sr_password_audit.py --discover range ^
        --ip-range 172.29.36.50-172.29.36.69 --escalate shell -vv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import ipaddress
import json
import os
import re
import shlex
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------
# Console setup (Windows 10 friendly)
# --------------------------------------------------------------------------

try:  # UTF-8 console output on Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
except Exception:
    pass

_COLOR = True
try:
    import colorama
    from colorama import Fore, Style

    if hasattr(colorama, "just_fix_windows_console"):
        colorama.just_fix_windows_console()
    else:  # pragma: no cover
        colorama.init()
except ImportError:  # graceful degradation - script still works without colors
    _COLOR = False

    class _Dummy:
        def __getattr__(self, _name: str) -> str:
            return ""

    Fore = Style = _Dummy()  # type: ignore[assignment]

VERBOSE = 0


def _c(text: str, color: str) -> str:
    return f"{color}{text}{Style.RESET_ALL}" if _COLOR else text


def disable_color() -> None:
    global _COLOR
    _COLOR = False


def info(msg: str) -> None:
    print(f"{_c('[*]', Fore.CYAN)} {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"{_c('[+]', Fore.GREEN)} {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"{_c('[!]', Fore.YELLOW)} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{_c('[x]', Fore.RED)} {msg}", file=sys.stderr)


def debug(msg: str, level: int = 1) -> None:
    if VERBOSE >= level:
        print(f"{_c('[d]', Fore.MAGENTA)} {msg}", file=sys.stderr)


def banner(msg: str) -> None:
    print(_c(f"\n=== {msg} ===", Fore.CYAN + Style.BRIGHT), file=sys.stderr)


# --------------------------------------------------------------------------
# Password entry: masked with '*', clipboard-aware, Alt-code friendly
# --------------------------------------------------------------------------

def read_clipboard() -> str:
    """Return the clipboard text, or '' when it cannot be read."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            CF_UNICODETEXT = 13
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            u32.OpenClipboard.argtypes = [wintypes.HWND]
            u32.OpenClipboard.restype = wintypes.BOOL
            u32.GetClipboardData.argtypes = [wintypes.UINT]
            u32.GetClipboardData.restype = wintypes.HANDLE
            k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            k32.GlobalLock.restype = wintypes.LPVOID
            k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

            if not u32.OpenClipboard(None):
                return ""
            try:
                handle = u32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = k32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.c_wchar_p(ptr).value or ""
                finally:
                    k32.GlobalUnlock(handle)
            finally:
                u32.CloseClipboard()
        except Exception:
            pass
    try:  # works on Linux/macOS, and as a Windows fallback
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        return ""


def _sanitize_pasted(text: str) -> str:
    """Keep a single line and drop control characters from pasted text."""
    for sep in ("\r\n", "\r", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return "".join(ch for ch in text if ch >= " " and ch != "\x7f")


def masked_input(prompt: str, mask: str = "*") -> str:
    """Read a secret, echoing '*' per character.

    Unlike getpass this gives visible feedback, and it accepts Ctrl+V. Typed
    input arrives as ordinary characters, so Windows Alt+numpad codes (Alt+64
    for '@', Alt+36 for '$', ...) and right-click paste work as usual - the
    console delivers the finished character and it is echoed like any other.
    """
    sys.stderr.write(_c(prompt, Fore.CYAN))
    sys.stderr.flush()

    if not sys.stdin.isatty():  # piped input - just read the line
        line = sys.stdin.readline()
        sys.stderr.write("\n")
        return line.rstrip("\r\n")

    buf: List[str] = []

    def echo(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    def backspace(n: int = 1) -> None:
        if n:
            echo("\b \b" * n)

    def insert(text: str) -> None:
        if text:
            buf.extend(text)
            echo(mask * len(text))

    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:  # pragma: no cover
            return getpass.getpass("")
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                echo("\n")
                return "".join(buf)
            if ch == "\x03":
                echo("\n")
                raise KeyboardInterrupt
            if ch in ("\x00", "\xe0"):   # function / arrow key: discard payload
                msvcrt.getwch()
                continue
            if ch in ("\x08", "\x7f"):   # backspace
                if buf:
                    buf.pop()
                    backspace()
                continue
            if ch == "\x16":             # Ctrl+V
                insert(_sanitize_pasted(read_clipboard()))
                continue
            if ch == "\x1b":             # Esc clears the line
                backspace(len(buf))
                buf.clear()
                continue
            if ch < " ":                 # any other control char
                continue
            insert(ch)

    # POSIX: turn off echo and canonical mode, read one character at a time
    try:
        import termios
    except ImportError:  # pragma: no cover
        return getpass.getpass("")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ECHO | termios.ICANON)  # lflags
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n", ""):
                echo("\n")
                return "".join(buf)
            if ch == "\x03":
                echo("\n")
                raise KeyboardInterrupt
            if ch in ("\x7f", "\x08"):
                if buf:
                    buf.pop()
                    backspace()
                continue
            if ch == "\x15":             # Ctrl+U clears the line
                backspace(len(buf))
                buf.clear()
                continue
            if ch == "\x16":             # Ctrl+V
                insert(_sanitize_pasted(read_clipboard()))
                continue
            if ch < " ":
                continue
            insert(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# --------------------------------------------------------------------------
# Dependency check
# --------------------------------------------------------------------------

try:
    import requests
    import urllib3
except ImportError:  # pragma: no cover
    error("Missing module 'requests' -> pip install requests")
    sys.exit(3)

try:
    import paramiko
except ImportError:  # pragma: no cover
    error("Missing module 'paramiko' -> pip install paramiko")
    sys.exit(3)


IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


# --------------------------------------------------------------------------
# 1) DISCOVERY - VCF Operations / fleet-lcm
# --------------------------------------------------------------------------

class VcfOpsClient:
    """Client for VCF/VVF Operations (suite-api token + plugin API)."""

    def __init__(self, host: str, verify: bool = False, cookie: Optional[str] = None):
        self.base = host if host.startswith("http") else f"https://{host}"
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers.update({"Accept": "application/json",
                               "Content-Type": "application/json"})
        if cookie:
            self.s.headers["Cookie"] = cookie
        self.token: Optional[str] = None

    def login(self, user: str, password: str, auth_source: str = "LOCAL") -> bool:
        url = f"{self.base}/suite-api/api/auth/token/acquire"
        payload: Dict[str, Any] = {"username": user, "password": password,
                                   "authSource": (auth_source or "LOCAL").upper()}
        try:
            r = self.s.post(url, json=payload, timeout=30)
        except requests.exceptions.SSLError as e:
            error(f"TLS error: {e}")
            warn("Self-signed certificate? Drop --verify-ssl or import the CA.")
            return False
        except requests.RequestException as e:
            error(f"Cannot reach {self.base}: {e}")
            return False
        if r.status_code != 200:
            error(f"Login failed: HTTP {r.status_code} {r.text[:200]}")
            return False
        self.token = r.json().get("token")
        self.s.headers["Authorization"] = f"vRealizeOpsToken {self.token}"
        ok("Authenticated to VCF Operations")
        return True

    # -- documented VCF 9.x fleet lifecycle flow ---------------------------
    def casa_services(self, user: str, password: str) -> Optional[Any]:
        """GET /casa/services with Basic auth - the service registry."""
        try:
            r = self.s.get(f"{self.base}/casa/services", timeout=30,
                           auth=(user, password),
                           headers={"Authorization": ""})
        except requests.RequestException as e:
            warn(f"/casa/services failed: {e}")
            return None
        if r.status_code != 200:
            warn(f"/casa/services -> HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def exchange_token(self, service_key: str) -> Optional[str]:
        """Exchange the Operations token for a fleet lifecycle bearer token."""
        if not self.token:
            return None
        try:
            r = self.s.post(
                f"{self.base}/suite-api/api/auth/token/exchange",
                json={"serviceKeys": [service_key]}, timeout=30,
                headers={"Authorization": f"OpsToken {self.token}",
                         "accept": "application/json"})
        except requests.RequestException as e:
            warn(f"Token exchange failed: {e}")
            return None
        if r.status_code != 200:
            warn(f"Token exchange -> HTTP {r.status_code} {r.text[:200]}")
            return None
        try:
            return r.json().get("jwtToken")
        except ValueError:
            return None

    def get_json(self, path: str, quiet: bool = True) -> Optional[Any]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        try:
            r = self.s.get(url, timeout=30)
        except requests.RequestException as e:
            if not quiet:
                warn(f"GET {path} failed: {e}")
            return None
        if r.status_code != 200:
            if not quiet:
                info(f"GET {path} -> HTTP {r.status_code}")
            return None
        if "json" not in r.headers.get("Content-Type", ""):
            return None
        try:
            return r.json()
        except ValueError:
            return None


FLEET_LCM_PATHS = [
    "/vcf-operations/plug/fleet-lcm/api/v1/components/{rid}",
    "/vcf-operations/plug/fleet-lcm/api/components/{rid}",
    "/vcf-operations/plug/fleet-lcm/api/v1/components/{rid}/nodes",
    "/vcf-operations/plug/fleet-lcm/api/v1/lifecycle/components/{rid}",
    "/vcf-operations/plug/fleet-lcm/api/v1/management-lifecycle",
    "/vcf-operations/plug/fleet-lcm/api/v1/components",
    "/vcf-operations/api/fleet-lcm/v1/components/{rid}",
    "/fleet-lcm/api/v1/components/{rid}",
]

NAME_KEYS = ("vmName", "vm_name", "name", "hostname", "fqdn", "nodeName", "displayName")
IP_KEYS = ("ipAddress", "ip_address", "ip", "primaryIp", "managementIp", "address")
TYPE_KEYS = ("nodeType", "node_type", "role", "type")

# Key names are matched case- and separator-insensitively, because VCF mixes
# vmName / hostName / node_name across releases and payload shapes.
NAME_KEY_PRIORITY = (
    "vmname", "vmdisplayname", "hostname", "fqdn", "nodename", "k8snodename",
    "kubernetesnodename", "displayname", "appliancename", "machinename",
    "vm", "host", "name", "id",
)
_TYPE_WORDS = {"worker", "workers", "controlplane", "control-plane", "control plane",
               "master", "primary", "secondary", "replica", "node", "vsp"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                     r"[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _plausible_name(value: Any) -> bool:
    """Reject IPs, UUIDs, role words and other non-name strings."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or len(v) > 253:
        return False
    if IP_RE.match(v) or UUID_RE.match(v):
        return False
    if v.lower().replace("_", "-") in _TYPE_WORDS:
        return False
    return True


def pick_name(node: Any, _depth: int = 0) -> str:
    """Find the most name-like string in a node object.

    Walks nested dictionaries and ranks candidates by how strongly the key
    suggests a host name, so an unexpected key spelling still resolves."""
    if not isinstance(node, dict) or _depth > 3:
        return ""

    best: Optional[Tuple[int, int, str]] = None   # (priority, depth, value)
    for key, value in node.items():
        nkey = _norm_key(key)
        if isinstance(value, str) and _plausible_name(value):
            for prio, hint in enumerate(NAME_KEY_PRIORITY):
                if nkey == hint or nkey.endswith(hint):
                    cand = (prio, _depth, value.strip())
                    if best is None or cand[:2] < best[:2]:
                        best = cand
                    break
        elif isinstance(value, dict):
            nested = pick_name(value, _depth + 1)
            if nested:
                cand = (len(NAME_KEY_PRIORITY), _depth + 1, nested)
                if best is None or cand[:2] < best[:2]:
                    best = cand
    return best[2] if best else ""


def walk_for_nodes(obj: Any, found: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    """Recursively scan any JSON payload for objects that look like a node."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        ip = None
        for k in IP_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and IP_RE.match(v.strip()):
                ip = v.strip()
                break
        if ip:
            name = pick_name(obj) or ip
            ntype = next((str(obj[k]) for k in TYPE_KEYS if isinstance(obj.get(k), str)), "")
            found.append({"name": name, "ip": ip, "type": ntype})
        for v in obj.values():
            walk_for_nodes(v, found)
    elif isinstance(obj, list):
        for v in obj:
            walk_for_nodes(v, found)
    return found


def dedup_nodes(nodes: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen, out = set(), []
    for n in nodes:
        if n["ip"] in seen:
            continue
        seen.add(n["ip"])
        out.append(n)
    try:
        return sorted(out, key=lambda x: ipaddress.ip_address(x["ip"]))
    except ValueError:
        return out


# --------------------------------------------------------------------------
# Documented VCF 9.x discovery:
#   GET  /casa/services              (Basic auth)  -> fleet lifecycle key + FQDN
#   POST /suite-api/api/auth/token/acquire         -> Operations token
#   POST /suite-api/api/auth/token/exchange        -> fleet lifecycle bearer JWT
#   GET  https://<fleet>/fleet-lcm/v1/components   -> components, VSP = runtime
#   GET  https://<fleet>/fleet-lcm/v1/components/<id>  -> node list
# --------------------------------------------------------------------------

FLEET_SERVICE_TYPE = "VCF_FLEET_LCM"


def find_service(obj: Any, wanted_type: str,
                 found: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Locate service-registry entries of a given type, with their key and FQDNs."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        if obj.get("type") == wanted_type and obj.get("key"):
            fqdns: List[str] = []
            for node in obj.get("nodes") or []:
                for addr in (node or {}).get("addresses") or []:
                    val = addr.get("value")
                    if val and addr.get("type", "").lower() in ("fqdn", "hostname"):
                        fqdns.append(val)
                    elif val and IP_RE.match(str(val)):
                        fqdns.append(val)
            found.append({"key": obj["key"], "name": obj.get("name", ""), "fqdns": fqdns})
        for v in obj.values():
            find_service(v, wanted_type, found)
    elif isinstance(obj, list):
        for v in obj:
            find_service(v, wanted_type, found)
    return found


def extract_fleet_nodes(component: Any) -> List[Dict[str, str]]:
    """Pull nodes out of a fleet-lcm component document.

    Handles both the deployed shape (nodes[].ipAddress/fqdn) and the spec shape
    (nodeSpecs[].deploymentSpec.ipv4Settings.address), falling back to a generic
    scan when neither matches."""
    out: List[Dict[str, str]] = []
    if not isinstance(component, dict):
        return out

    for key in ("nodes", "nodeSpecs", "vmNodes", "clusterNodes"):
        for node in component.get(key) or []:
            if not isinstance(node, dict):
                continue
            spec = node.get("deploymentSpec") or {}
            ipv4 = node.get("ipv4Settings") or spec.get("ipv4Settings") or {}
            ip = (node.get("ipAddress") or node.get("ip")
                  or ipv4.get("address") or spec.get("ipAddress"))
            name = pick_name(node)
            ntype = node.get("nodeType") or node.get("role") or node.get("type") or ""
            if ip and IP_RE.match(str(ip).strip()):
                if VERBOSE >= 2 and not name:
                    debug(f"node without a recognised name, keys: "
                          f"{sorted(node.keys())}", 2)
                entry = {"name": name or str(ip).strip(),
                         "ip": str(ip).strip(), "type": str(ntype)}
                # The UI shows the short VM name; keep the FQDN separately
                if name and "." in name and not IP_RE.match(name):
                    entry["fqdn"] = name
                    entry["name"] = name.split(".")[0]
                out.append(entry)
    return out or walk_for_nodes(component)


class FleetLcmClient:
    """Talks to the fleet lifecycle component with a bearer token."""

    def __init__(self, host: str, jwt: str, verify: bool = False):
        self.base = host if host.startswith("http") else f"https://{host}"
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers.update({"Authorization": f"Bearer {jwt}",
                               "Accept": "application/json"})

    def get(self, path: str) -> Optional[Any]:
        try:
            r = self.s.get(f"{self.base}{path}", timeout=30)
        except requests.RequestException as e:
            warn(f"GET {path} failed: {e}")
            return None
        if r.status_code != 200:
            warn(f"GET {path} -> HTTP {r.status_code} {r.text[:150]}")
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def components(self) -> List[Dict[str, Any]]:
        data = self.get("/fleet-lcm/v1/components")
        if isinstance(data, dict):
            return data.get("components") or []
        return data if isinstance(data, list) else []

    def component(self, comp_id: str) -> Optional[Any]:
        return self.get(f"/fleet-lcm/v1/components/{comp_id}")

    def component_config(self, comp_id: str) -> Optional[Any]:
        """Component configuration - carries the runtime IP pool."""
        return self.get(f"/fleet-lcm/v1/components/{comp_id}/config")

    def component_nodes(self, comp_id: str) -> Optional[Any]:
        return self.get(f"/fleet-lcm/v1/components/{comp_id}/nodes")


RANGE_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})")
START_KEYS = ("startIpAddress", "startIp", "start", "rangeStart", "beginIp", "from")
STOP_KEYS = ("endIpAddress", "endIp", "end", "rangeEnd", "stopIp", "to")


def extract_ip_pools(obj: Any, found: Optional[List[str]] = None) -> List[str]:
    """Find IP pool ranges anywhere in a component configuration document.

    Recognises both 'a.b.c.d-e.f.g.h' strings and start/end key pairs, which is
    how the 'VCF services runtime IP pool' is expressed."""
    if found is None:
        found = []
    if isinstance(obj, str):
        m = RANGE_RE.search(obj)
        if m:
            found.append(f"{m.group(1)}-{m.group(2)}")
    elif isinstance(obj, dict):
        start = next((str(obj[k]) for k in START_KEYS
                      if isinstance(obj.get(k), str) and IP_RE.match(obj[k])), None)
        stop = next((str(obj[k]) for k in STOP_KEYS
                     if isinstance(obj.get(k), str) and IP_RE.match(obj[k])), None)
        if start and stop:
            found.append(f"{start}-{stop}")
        for v in obj.values():
            extract_ip_pools(v, found)
    elif isinstance(obj, list):
        for v in obj:
            extract_ip_pools(v, found)
    # de-duplicate, keep order
    seen, out = set(), []
    for r in found:
        if r not in seen:
            seen.add(r)
            out.append(r)
    found[:] = out
    return out


def discover_fleet_api(a: argparse.Namespace, verify: bool) -> List[Dict[str, str]]:
    """Full documented discovery chain. Returns [] if any step fails."""
    client = VcfOpsClient(a.ops_host, verify=verify, cookie=a.cookie)
    if not client.login(a.ops_user, a.ops_pass, a.auth_source):
        return []

    # 1. service key + fleet FQDN (unless the operator supplied them)
    fleet_host, service_key = a.fleet_host, a.service_key
    if not service_key:
        services = client.casa_services(a.ops_user, a.ops_pass)
        if a.dump_api and services is not None:
            print(json.dumps(services, indent=2)[:20000])
        matches = find_service(services, FLEET_SERVICE_TYPE) if services else []
        if not matches:
            warn(f"No '{FLEET_SERVICE_TYPE}' entry found in /casa/services")
            return []
        svc = matches[0]
        service_key = svc["key"]
        if not fleet_host and svc["fqdns"]:
            fleet_host = svc["fqdns"][0]
        ok(f"Fleet lifecycle service found: {svc.get('name') or FLEET_SERVICE_TYPE}"
           + (f" @ {fleet_host}" if fleet_host else ""))

    if not fleet_host:
        warn("Fleet lifecycle FQDN unknown - pass it with --fleet-host "
             "(Components tab, 'Fleet lifecycle' row)")
        return []

    # 2. exchange the Operations token for a fleet lifecycle bearer token
    jwt = client.exchange_token(service_key)
    if not jwt:
        warn("Could not exchange the Operations token for a fleet lifecycle token")
        return []
    debug("Bearer token obtained", 1)

    # 3. list components and keep the VCF services runtime instances
    fleet = FleetLcmClient(fleet_host, jwt, verify=verify)
    comps = fleet.components()
    if not comps:
        warn(f"{fleet_host}/fleet-lcm/v1/components returned nothing")
        return []
    info(f"{len(comps)} component(s) reported by fleet lifecycle")

    wanted = [c for c in comps
              if a.all_components or str(c.get("componentType", "")).upper() == "VSP"]
    if not wanted:
        types = sorted({str(c.get("componentType", "?")) for c in comps})
        warn(f"No VSP component found. Types present: {', '.join(types)}")
        warn("Use --all-components to inspect every component instead.")
        return []

    nodes: List[Dict[str, str]] = []
    pools: List[str] = []
    for c in wanted:
        cid = c.get("id")
        label = c.get("name") or c.get("componentType") or cid
        detail = fleet.component(cid) if cid else None
        if a.dump_api and detail is not None:
            print(json.dumps(detail, indent=2)[:20000])
        found = extract_fleet_nodes(detail) if detail else []
        if not found:
            found = extract_fleet_nodes(c)
        if not found and cid:  # some builds expose a dedicated nodes sub-resource
            sub = fleet.component_nodes(cid)
            if sub:
                found = extract_fleet_nodes(sub) or walk_for_nodes(sub)
        debug(f"component {label}: {len(found)} node(s)", 1)
        nodes.extend(found)

        # Collect the runtime IP pool as an automatic fallback
        if cid:
            cfg = fleet.component_config(cid)
            if a.dump_api and cfg is not None:
                print(json.dumps(cfg, indent=2)[:20000])
            if cfg:
                pools.extend(extract_ip_pools(cfg))
                if not found:
                    nodes.extend(extract_fleet_nodes(cfg))

    nodes = dedup_nodes(nodes)
    if nodes:
        if all(n["name"] == n["ip"] for n in nodes):
            warn("The API returned addresses but no VM names; falling back to "
                 "reverse DNS and to asking each node for its hostname.")
            warn("Run with -vv to see the node keys, or --dump-api for the raw JSON.")
        return nodes

    # No explicit node list: derive the hosts from the runtime IP pool and probe
    for rng in pools:
        info(f"No node list in the API; probing the runtime IP pool {rng}")
        found = discover_range(rng, probe=True, port=a.ssh_port)
        if found:
            return found

    warn("Fleet lifecycle exposed neither a node list nor an IP pool")
    return []


def discover_fleetlcm(client: VcfOpsClient, resource_id: str,
                      dump: bool = False) -> List[Dict[str, str]]:
    for tmpl in FLEET_LCM_PATHS:
        path = tmpl.format(rid=resource_id)
        data = client.get_json(path, quiet=not dump)
        if data is None:
            continue
        if dump:
            print(json.dumps({"endpoint": path, "data": data}, indent=2)[:20000])
        nodes = dedup_nodes(walk_for_nodes(data))
        if nodes:
            ok(f"Nodes found via endpoint: {path}")
            return nodes
        info(f"Endpoint {path} responded but contained no node IPs")
    warn("No known fleet-lcm endpoint returned a node list.")
    warn("Tip: open F12 -> Network in the browser, reload the Lifecycle page, "
         "find the XHR returning the node JSON and pass its path via --api-path.")
    return []


# --------------------------------------------------------------------------
# 2) DISCOVERY - vCenter REST API
# --------------------------------------------------------------------------

def discover_vcenter(host: str, user: str, password: str, prefix: str,
                     verify: bool = False) -> List[Dict[str, str]]:
    base = f"https://{host}"
    s = requests.Session()
    s.verify = verify

    try:
        r = s.post(f"{base}/api/session", auth=(user, password), timeout=30)
        if r.status_code == 404:  # older vCenter
            r = s.post(f"{base}/rest/com/vmware/cis/session", auth=(user, password), timeout=30)
            r.raise_for_status()
            token = r.json()["value"]
        else:
            r.raise_for_status()
            body = r.json()
            token = body if isinstance(body, str) else body.get("value")
    except requests.RequestException as e:
        error(f"vCenter login failed: {e}")
        return []

    s.headers["vmware-api-session-id"] = token
    ok("Authenticated to vCenter")

    vms = s.get(f"{base}/api/vcenter/vm", timeout=60).json()
    targets = [v for v in vms if v.get("name", "").startswith(prefix)]
    info(f"Found {len(targets)} VM(s) matching prefix '{prefix}'")

    nodes: List[Dict[str, str]] = []
    for vm in targets:
        vm_id, name = vm["vm"], vm["name"]
        gi = s.get(f"{base}/api/vcenter/vm/{vm_id}/guest/identity", timeout=30)
        if gi.status_code != 200:
            warn(f"{name}: no IP reported (VMware Tools not running?)")
            continue
        ip = (gi.json() or {}).get("ip_address")
        if ip and IP_RE.match(ip):
            nodes.append({"name": name, "ip": ip, "type": ""})
        else:
            warn(f"{name}: no IPv4 address reported")
    return dedup_nodes(nodes)


# --------------------------------------------------------------------------
# 3) DISCOVERY - IP range / file
# --------------------------------------------------------------------------

def port_open(ip: str, port: int = 22, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def expand_range(ip_range: str) -> List[str]:
    if "-" in ip_range:
        start_s, end_s = [x.strip() for x in ip_range.split("-", 1)]
        if not IP_RE.match(end_s):  # short form: 172.29.36.50-69
            end_s = start_s.rsplit(".", 1)[0] + "." + end_s
        start, end = ipaddress.ip_address(start_s), ipaddress.ip_address(end_s)
        return [str(ipaddress.ip_address(i)) for i in range(int(start), int(end) + 1)]
    return [str(h) for h in ipaddress.ip_network(ip_range, strict=False).hosts()]


def discover_range(ip_range: str, probe: bool = True, port: int = 22) -> List[Dict[str, str]]:
    candidates = expand_range(ip_range)
    if not probe:
        return [{"name": c, "ip": c, "type": ""} for c in candidates]

    info(f"Probing TCP/{port} on {len(candidates)} address(es)...")
    alive: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = {ex.submit(port_open, c, port): c for c in candidates}
        for f in as_completed(futs):
            if f.result():
                ip = futs[f]
                alive.append({"name": ip, "ip": ip, "type": ""})
    return dedup_nodes(alive)


def resolve_node_names(nodes: List[Dict[str, str]], workers: int = 16) -> List[Dict[str, str]]:
    """Fill in names for nodes the API only gave us an IP for.

    The IP-pool probe and some API shapes yield bare addresses; a reverse DNS
    lookup usually recovers the appliance name (vvfsr01-bt588)."""
    todo = [n for n in nodes if not n.get("name") or n["name"] == n["ip"]]
    if not todo:
        return nodes

    def rdns(n: Dict[str, str]) -> None:
        try:
            host = socket.gethostbyaddr(n["ip"])[0]
        except (OSError, socket.herror, socket.gaierror):
            return
        if host and not IP_RE.match(host):
            n["fqdn"] = host
            n["name"] = host.split(".")[0]

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
        list(ex.map(rdns, todo))

    resolved = sum(1 for n in todo if n.get("fqdn"))
    if resolved:
        debug(f"reverse DNS resolved {resolved}/{len(todo)} name(s)", 1)
    return nodes


def discover_file(path: str) -> List[Dict[str, str]]:
    nodes = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                nodes.append({"name": line, "ip": line, "type": ""})
    return nodes


# --------------------------------------------------------------------------
# chage parsing
# --------------------------------------------------------------------------

CHAGE_MAP = {
    "Last password change": "last_change",
    "Password expires": "password_expires",
    "Password inactive": "password_inactive",
    "Account expires": "account_expires",
    "Minimum number of days between password change": "min_days",
    "Maximum number of days between password change": "max_days",
    "Number of days of warning before password expires": "warn_days",
}


def parse_chage(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = CHAGE_MAP.get(k.strip())
        if key:
            out[key] = v.strip()

    exp = out.get("password_expires", "")
    out["days_left"] = None
    low = exp.lower()
    if low.startswith("password must be changed"):
        out["days_left"] = -1
    elif exp and low != "never":
        for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                d = dt.datetime.strptime(exp, fmt).date()
                out["days_left"] = (d - dt.date.today()).days
                break
            except ValueError:
                continue
    return out


# --------------------------------------------------------------------------
# SSH privilege escalation
# --------------------------------------------------------------------------

SUDO_FAIL_PATTERNS = (
    "incorrect password", "sorry, try again", "authentication failure",
    "is not in the sudoers", "no tty present", "a terminal is required",
    "sudo: a password is required",
)

BEGIN = "___CHAGE_BEGIN___"
END = "___CHAGE_END___"
# Quoted forms used when *sending* the command. An interactive shell echoes the
# command line back, and the echo would otherwise contain the markers verbatim -
# the reader would then stop before the real output arrived. The shell strips
# the quotes, so only the genuine output contains BEGIN/END unquoted.
BEGIN_Q = '___CHAGE_"BEGIN"___'
END_Q = '___CHAGE_"END"___'
END_RE = re.compile(re.escape(END) + r"(\d+)")


def _looks_like_sudo_failure(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in SUDO_FAIL_PATTERNS)


def run_nopass(cli: "paramiko.SSHClient", cmd: str, timeout: int) -> Tuple[int, str]:
    """sudo -n: passwordless sudo, fails fast if a password would be needed."""
    full = f"sudo -n -- /bin/sh -c {shlex.quote(cmd)}"
    _, stdout, stderr = cli.exec_command(full, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out + err


def run_exec_sudo(cli: "paramiko.SSHClient", cmd: str, password: str,
                  timeout: int) -> Tuple[int, str]:
    """`sudo -S -i` over a PTY, password written to stdin.

    -i gives a root login shell just like the interactive `sudo -i`, so the
    environment matches what an operator would see."""
    full = f"sudo -S -p '' -i -- /bin/sh -c {shlex.quote(cmd)}"
    stdin, stdout, _ = cli.exec_command(full, timeout=timeout, get_pty=True)
    try:
        stdin.write(password + "\n")
        stdin.flush()
    except OSError:
        pass
    out = stdout.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out


class ShellSession:
    """Interactive shell that mirrors the manual workflow:
        ssh vmware-system-user@node -> sudo -i -> <password> -> chage -l ...

    Needed on appliances where sudo has `requiretty` or where a non-login
    escalation is refused. One `sudo -i` per node, then all commands reuse it.
    """

    def __init__(self, cli: "paramiko.SSHClient", timeout: int = 20):
        self.chan = cli.invoke_shell(width=250, height=200)
        self.chan.settimeout(0.5)
        self.timeout = timeout
        self.buf = ""
        self._drain(1.5)

    # -- low level ---------------------------------------------------------
    def _drain(self, seconds: float) -> str:
        end = time.time() + seconds
        chunk = ""
        while time.time() < end:
            try:
                if self.chan.recv_ready():
                    chunk += self.chan.recv(65536).decode(errors="replace")
                else:
                    time.sleep(0.05)
            except socket.timeout:
                break
        self.buf += chunk
        return chunk

    def _read_until(self, pred: Callable[[str], bool], timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if pred(self.buf):
                return True
            try:
                if self.chan.recv_ready():
                    self.buf += self.chan.recv(65536).decode(errors="replace")
                else:
                    time.sleep(0.05)
            except socket.timeout:
                continue
        return pred(self.buf)

    def _send(self, text: str) -> None:
        self.chan.send(text)

    # -- high level --------------------------------------------------------
    def escalate(self, password: str) -> Tuple[bool, str]:
        """Run `sudo -i` and answer the password prompt. Returns (ok, detail)."""
        self.buf = ""
        self._send("sudo -i\n")

        got_prompt = self._read_until(
            lambda b: "assword" in b or "Password" in b, 8)
        if got_prompt:
            debug("sudo password prompt received", 2)
            self._send(password + "\n")
            time.sleep(0.4)
        else:
            debug("no password prompt (passwordless sudo?)", 2)

        # Confirm we really are root now
        self.buf = ""
        self._send(f"echo {BEGIN_Q}$(id -u){END_Q}\n")
        self._read_until(
            lambda b: re.search(re.escape(BEGIN) + r"\d+" + re.escape(END), b) is not None, 10)
        m = re.search(re.escape(BEGIN) + r"(\d+)" + re.escape(END), self.buf)
        if m and m.group(1) == "0":
            # Silence command echo so later output stays clean
            self._send("stty -echo 2>/dev/null\n")
            self._drain(0.3)
            return True, "shell"
        if _looks_like_sudo_failure(self.buf):
            return False, "sudo rejected the password"
        if m:
            return False, f"escalation did not reach root (uid={m.group(1)})"
        return False, "no response after sudo -i"

    def run(self, cmd: str) -> Tuple[int, str]:
        self.buf = ""
        self._send(f"echo {BEGIN_Q}; {cmd} 2>&1; echo {END_Q}$?\n")
        # Wait for END followed by the exit code, not just the marker text
        self._read_until(lambda b: END_RE.search(b) is not None, self.timeout)
        m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END) + r"(\d+)",
                      self.buf, re.S)
        if not m:
            return 1, self.buf[-500:].strip()
        rc = int(m.group(2))
        lines = [ln.rstrip("\r") for ln in m.group(1).splitlines()
                 if BEGIN not in ln and END not in ln]
        return rc, "\n".join(lines)

    def close(self) -> None:
        try:
            self._send("exit\n")
            self.chan.close()
        except Exception:
            pass


def check_node(node: Dict[str, str], users: List[str], ssh_user: str,
               ssh_pass: Optional[str], key_file: Optional[str],
               sudo_pass: Optional[str], escalate: str,
               port: int, timeout: int,
               set_max_days: Optional[int] = None) -> List[Dict[str, Any]]:
    ip = node["ip"]
    name = node.get("name") or ip
    base = {"node": name, "ip": ip, "node_type": node.get("type", "")}

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(
            hostname=ip, port=port, username=ssh_user,
            password=ssh_pass, key_filename=key_file,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            look_for_keys=bool(key_file) or ssh_pass is None,
            allow_agent=ssh_pass is None,
        )
    except Exception as e:
        return [dict(base, user=u, method="", error=f"SSH: {e}") for u in users]

    debug(f"{ip}: connected as {ssh_user}", 1)

    # The API (or an IP-pool probe) may only have given us an address. Ask the
    # node itself - this needs no privileges and is the authoritative answer.
    if not name or name == ip:
        try:
            _, so, _ = cli.exec_command("hostname -f 2>/dev/null || hostname",
                                        timeout=timeout)
            reported = so.read().decode(errors="replace").strip().splitlines()
            hn = reported[0].strip() if reported else ""
            if hn and not IP_RE.match(hn):
                name = hn.split(".")[0]
                node["name"] = name
                base["node"] = name
                base["fqdn"] = hn
                debug(f"{ip}: hostname reported as {hn}", 1)
        except Exception:
            pass

    rows: List[Dict[str, Any]] = []
    shell: Optional[ShellSession] = None

    def cmd_for(u: str) -> str:
        return f"LC_ALL=C chage -l {shlex.quote(u)}"

    try:
        # ---- pick an escalation strategy once per node -------------------
        runner: Optional[Callable[[str], Tuple[int, str]]] = None
        method = ""
        esc_error = ""

        if escalate == "none":
            def runner(c: str) -> Tuple[int, str]:  # type: ignore[misc]
                _, so, se = cli.exec_command(c, timeout=timeout)
                o = so.read().decode(errors="replace") + se.read().decode(errors="replace")
                return so.channel.recv_exit_status(), o
            method = "direct"

        def usable(rc: int, out: str) -> bool:
            """The probe counts as success only if real chage output came back."""
            if rc != 0 or _looks_like_sudo_failure(out):
                return False
            parsed = parse_chage(out)
            return bool(parsed.get("last_change") or parsed.get("password_expires"))

        if runner is None and escalate in ("auto", "nopass"):
            rc, out = run_nopass(cli, cmd_for(users[0]), timeout)
            if usable(rc, out):
                debug(f"{ip}: passwordless sudo works", 1)
                runner = lambda c: run_nopass(cli, c, timeout)  # noqa: E731
                method = "sudo -n"
            elif escalate == "nopass":
                esc_error = f"sudo -n failed: {out.strip()[:120]}"

        if runner is None and escalate in ("auto", "exec") and sudo_pass:
            rc, out = run_exec_sudo(cli, cmd_for(users[0]), sudo_pass, timeout)
            if usable(rc, out):
                debug(f"{ip}: sudo -S -i works", 1)
                runner = lambda c: run_exec_sudo(cli, c, sudo_pass, timeout)  # noqa: E731
                method = "sudo -S -i"
            elif escalate == "exec":
                esc_error = f"sudo -S failed: {out.strip()[:120]}"

        if runner is None and escalate in ("auto", "shell") and sudo_pass:
            try:
                shell = ShellSession(cli, timeout=timeout)
                good, detail = shell.escalate(sudo_pass)
                if good:
                    debug(f"{ip}: interactive 'sudo -i' succeeded", 1)
                    runner = shell.run
                    method = "sudo -i (shell)"
                else:
                    esc_error = esc_error or detail
                    shell.close()
                    shell = None
            except Exception as e:
                esc_error = esc_error or f"shell escalation: {e}"

        if runner is None:
            msg = esc_error or "privilege escalation failed (no password supplied?)"
            return [dict(base, user=u, method="", error=msg) for u in users]

        # ---- run chage for every account ---------------------------------
        for u in users:
            row: Dict[str, Any] = dict(base, user=u, method=method, error="")
            try:
                rc, out = runner(cmd_for(u))
                parsed = parse_chage(out)
                if parsed.get("last_change") or parsed.get("password_expires"):
                    row.update(parsed)
                    row["raw"] = out.strip()
                else:
                    row["error"] = (out.strip().replace("\n", " ")[:160]
                                    or f"exit code {rc}")
            except Exception as e:
                row["error"] = str(e)

            # ---- optionally disable expiry, then re-read the settings ----
            if set_max_days is not None and not row["error"]:
                row["apply_error"] = ""
                try:
                    rc2, out2 = runner(f"chage -M {int(set_max_days)} {shlex.quote(u)}")
                    if rc2 != 0:
                        row["apply_error"] = (out2.strip().replace("\n", " ")[:160]
                                              or f"chage -M exit code {rc2}")
                    else:
                        rc3, out3 = runner(cmd_for(u))
                        after = parse_chage(out3)
                        if after.get("last_change") or after.get("password_expires"):
                            for k, v in after.items():
                                row[f"new_{k}"] = v
                            row["changed"] = (after.get("max_days") != row.get("max_days")
                                              or after.get("password_expires")
                                              != row.get("password_expires"))
                        else:
                            row["apply_error"] = "could not re-read chage after the change"
                except Exception as e:
                    row["apply_error"] = str(e)
            rows.append(row)
    finally:
        if shell:
            shell.close()
        cli.close()
    return rows


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def days_color(days: Optional[int]) -> str:
    if days is None:
        return Fore.WHITE
    if days < 0:
        return Fore.RED + Style.BRIGHT
    if days <= 14:
        return Fore.RED
    if days <= 30:
        return Fore.YELLOW
    return Fore.GREEN


def print_table(rows: List[Dict[str, Any]]) -> None:
    hdr = ["NODE", "IP", "USER", "LAST CHANGE", "EXPIRES", "DAYS",
           "MIN", "MAX", "WARN", "NOTE"]
    plain: List[List[str]] = []
    for r in rows:
        d = r.get("days_left")
        plain.append([
            r.get("node", ""), r.get("ip", ""), r.get("user", ""),
            r.get("last_change", "-"), r.get("password_expires", "-"),
            "-" if d is None else str(d),
            r.get("min_days", "-"), r.get("max_days", "-"), r.get("warn_days", "-"),
            (r.get("error", "") or "")[:60],
        ])

    widths = [len(h) for h in hdr]
    for row in plain:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(hdr))
    print(_c(header_line, Fore.CYAN + Style.BRIGHT))
    print(_c("-" * len(header_line), Fore.CYAN))

    for r, row in zip(rows, plain):
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row)]
        if r.get("error"):
            print(_c("  ".join(cells), Fore.RED))
            continue
        cells[5] = _c(cells[5], days_color(r.get("days_left")))
        print("  ".join(cells))


def print_change_table(rows: List[Dict[str, Any]]) -> None:
    """Before/after view used when expiry was disabled."""
    hdr = ["NODE", "IP", "USER", "MAX BEFORE", "EXPIRES BEFORE",
           "MAX AFTER", "EXPIRES AFTER", "RESULT"]
    plain: List[List[str]] = []
    for r in rows:
        if r.get("error"):
            result = "failed"
        elif r.get("apply_error"):
            result = "not applied"
        elif r.get("changed"):
            result = "changed"
        else:
            result = "already set"
        plain.append([
            r.get("node", ""), r.get("ip", ""), r.get("user", ""),
            r.get("max_days", "-"), r.get("password_expires", "-"),
            r.get("new_max_days", "-"), r.get("new_password_expires", "-"),
            result,
        ])

    widths = [len(h) for h in hdr]
    for row in plain:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(hdr))
    print(_c(header_line, Fore.CYAN + Style.BRIGHT))
    print(_c("-" * len(header_line), Fore.CYAN))

    for r, row in zip(rows, plain):
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row)]
        verdict = row[7]
        if verdict in ("failed", "not applied"):
            print(_c("  ".join(cells), Fore.RED))
            continue
        cells[7] = _c(cells[7], Fore.GREEN if verdict == "changed" else Fore.YELLOW)
        print("  ".join(cells))


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    cols = ["node", "ip", "node_type", "user", "last_change", "password_expires",
            "days_left", "password_inactive", "account_expires", "min_days",
            "max_days", "warn_days", "new_last_change", "new_password_expires",
            "new_days_left", "new_min_days", "new_max_days", "new_warn_days",
            "changed", "method", "error", "apply_error"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------
# Interactive wizard
# --------------------------------------------------------------------------

def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(_c(f"  {label}{suffix}: ", Fore.CYAN)).strip() or default


def prompt_operations(a: argparse.Namespace) -> argparse.Namespace:
    """The only thing the operator has to supply: where Operations lives.

    Everything else - the fleet lifecycle FQDN, its service key, the component
    IDs and the node addresses - is resolved from there automatically."""
    banner("VCF Operations - connection")
    while not a.ops_host:
        a.ops_host = ask("Operations FQDN")
        if not a.ops_host:
            warn("  An FQDN or IP address is required.")
    a.ops_user = ask("Username", a.ops_user)
    a.ops_pass = masked_input(f"  Password for {a.ops_user}: ")
    a.discover = "fleetlcm"
    return a


def prompt_node_credentials(a: argparse.Namespace) -> List[str]:
    """Step 3: credentials used on every discovered node."""
    banner("Node credentials")
    a.ssh_user = ask("SSH login for the nodes", a.ssh_user)
    a.ssh_pass = masked_input(f"  Password for {a.ssh_user}: ")
    a.sudo_pass = a.ssh_pass  # `sudo -i` reuses the same password

    # Audit the login the operator just gave us, plus root
    users = [a.ssh_user] + ([] if a.ssh_user == "root" else ["root"])
    a.users = ",".join(users)
    return users


def prompt_ip_range(a: argparse.Namespace) -> List[Dict[str, str]]:
    """Last resort only: the API gave us nothing at all to work with."""
    warn("Operations did not expose the node list nor the runtime IP pool.")
    print("  Enter the 'VCF services runtime IP pool' from Build > Lifecycle,")
    print("  or press Enter to give up.")
    rng = ask("IP range", "")
    if not rng:
        return []
    a.ip_range = rng
    return discover_range(rng, probe=True, port=a.ssh_port)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Audit password expiry settings on VCF services runtime nodes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    p.add_argument("--discover", choices=["fleetlcm", "vcenter", "range", "file"],
                   default="fleetlcm", help="node discovery source")
    p.add_argument("--no-fallback", action="store_true",
                   help="do not fall back to the IP range probe when discovery finds nothing")

    g = p.add_argument_group("VCF Operations")
    g.add_argument("--ops-host", help="VCF Operations FQDN or IP (no default)")
    g.add_argument("--ops-user", default="admin")
    g.add_argument("--ops-pass", default=os.environ.get("OPS_PASS"),
                   help="or set the OPS_PASS environment variable")
    g.add_argument("--auth-source", default="LOCAL")
    g.add_argument("--fleet-host",
                   help="Fleet lifecycle FQDN (Components tab). Autodetected via "
                        "/casa/services when omitted, e.g. vvfleet.kr-olomoucky.int")
    g.add_argument("--service-key",
                   help="VCF_FLEET_LCM service key; autodetected via /casa/services")
    g.add_argument("--all-components", action="store_true",
                   help="scan every fleet component, not only the VSP runtime")
    g.add_argument("--legacy-api", action="store_true",
                   help="also try the older guessed plugin endpoints")
    g.add_argument("--resource-id", default="",
                   help="component ID from the fleet-lcm UI URL (legacy API only)")
    g.add_argument("--api-path", help="explicit API path ({rid} = resource id)")
    g.add_argument("--cookie", help="Cookie header copied from the browser")
    g.add_argument("--dump-api", action="store_true", help="print raw API JSON responses")

    g = p.add_argument_group("vCenter")
    g.add_argument("--vc-host", help="vCenter FQDN or IP (no default)")
    g.add_argument("--vc-user", default="administrator@vsphere.local")
    g.add_argument("--vc-pass", default=os.environ.get("VC_PASS"),
                   help="or set the VC_PASS environment variable")
    g.add_argument("--vm-prefix", default="vvfsr01-",
                   help="VM name prefix used by --discover vcenter")

    g = p.add_argument_group("IP range / file")
    g.add_argument("--ip-range", help="e.g. 172.29.36.50-172.29.36.69 or a CIDR")
    g.add_argument("--no-probe", action="store_true", help="skip the TCP/22 probe")
    g.add_argument("--nodes-file", help="text file with IPs/FQDNs, one per line")

    g = p.add_argument_group("SSH / privilege escalation")
    g.add_argument("--ssh-user", default="vmware-system-user",
                   help="SSH account used to log in to the nodes")
    g.add_argument("--ssh-pass", default=os.environ.get("SSH_PASS"),
                   help="or set the SSH_PASS environment variable")
    g.add_argument("--ssh-key", help="path to a private key file")
    g.add_argument("--ssh-port", type=int, default=22)
    g.add_argument("--escalate", choices=["auto", "nopass", "exec", "shell", "none"],
                   default="auto", help="how to become root before running chage")
    g.add_argument("--sudo-pass", default=os.environ.get("SUDO_PASS"),
                   help="password for 'sudo -i'; defaults to the SSH password")
    g.add_argument("--users", default="root,vmware-system-user",
                   help="comma separated accounts to audit")
    g.add_argument("--workers", type=int, default=8)
    g.add_argument("--timeout", type=int, default=20)

    g = p.add_argument_group("Changing the settings")
    g.add_argument("--no-expire", action="store_true",
                   help="after reading the current values, run 'chage -M 99999' for "
                        "every audited account on every node, then show before/after")
    g.add_argument("--max-days", type=int, default=99999,
                   help="value used by --no-expire")
    g.add_argument("--yes", "-y", action="store_true",
                   help="skip the confirmation prompt for --no-expire")

    g = p.add_argument_group("Output / TLS")
    g.add_argument("--verify-ssl", action="store_true",
                   help="enable TLS certificate verification (off by default)")
    g.add_argument("--json", dest="json_out", help="write results to a JSON file")
    g.add_argument("--csv", dest="csv_out", help="write results to a CSV file")
    g.add_argument("--no-color", action="store_true", help="disable colored output")
    g.add_argument("--list-only", action="store_true", help="list nodes and exit")
    g.add_argument("--wizard", action="store_true",
                   help="force the guided prompts even when other options are given")
    g.add_argument("--no-wizard", action="store_true",
                   help="never prompt for connection details (unattended runs)")
    g.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v for progress detail, -vv for escalation tracing")
    return p


# Arguments that mean "I know what I want, do not ask me"
CONFIG_FLAGS = (
    "--discover", "--ops-host", "--ops-user", "--ops-pass", "--auth-source",
    "--resource-id", "--api-path", "--cookie", "--vc-host", "--vc-user",
    "--vc-pass", "--vm-prefix", "--ip-range", "--nodes-file", "--ssh-user",
    "--ssh-pass", "--ssh-key", "--sudo-pass", "--users", "--list-only",
    "--no-probe", "--dump-api", "--fleet-host", "--service-key",
    "--all-components", "--legacy-api",
)


def decide_interactive(a: argparse.Namespace) -> bool:
    if a.no_wizard:
        return False
    if a.wizard:
        return True
    if not sys.stdin.isatty():
        return False
    for arg in sys.argv[1:]:
        head = arg.split("=", 1)[0]
        if head in CONFIG_FLAGS:
            return False
    # Credentials injected through the environment also imply unattended use
    if os.environ.get("OPS_PASS") or os.environ.get("SSH_PASS"):
        return False
    return True


def main() -> int:
    global VERBOSE
    p = build_parser()
    a = p.parse_args()

    VERBOSE = a.verbose
    if a.no_color:
        disable_color()

    # Guided run: ask for Operations, discover the nodes, then ask for the node
    # credentials. Triggered unless the caller supplied connection arguments, so
    # cosmetic flags (--csv, --no-color, -v) do not silently disable the wizard.
    interactive = decide_interactive(a)
    if interactive:
        a = prompt_operations(a)

    verify = a.verify_ssl
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        warn("TLS certificate verification is DISABLED (use --verify-ssl to enable)")

    # --- discovery --------------------------------------------------------
    banner("Node discovery")
    nodes: List[Dict[str, str]] = []

    if a.discover == "fleetlcm":
        if not a.ops_host:
            error("Operations FQDN is required: pass --ops-host, or run without "
                  "arguments for the guided prompts.")
            return 2
        if not a.ops_pass and not a.cookie:
            a.ops_pass = masked_input(f"  Password for {a.ops_user}@{a.ops_host}: ")

        # Primary: the documented VCF 9.x chain (casa/services -> token
        # exchange -> fleet-lcm/v1/components on the Fleet lifecycle host)
        if a.ops_pass:
            nodes = discover_fleet_api(a, verify)

        # Secondary: an explicit path, or the older guessed plugin endpoints
        if not nodes and (a.api_path or a.legacy_api):
            client = VcfOpsClient(a.ops_host, verify=verify, cookie=a.cookie)
            authed = client.login(a.ops_user, a.ops_pass, a.auth_source) if a.ops_pass else False
            if authed or a.cookie:
                if a.api_path:
                    data = client.get_json(a.api_path.format(rid=a.resource_id), quiet=False)
                    if a.dump_api and data is not None:
                        print(json.dumps(data, indent=2)[:20000])
                    nodes = dedup_nodes(walk_for_nodes(data)) if data else []
                else:
                    nodes = discover_fleetlcm(client, a.resource_id, dump=a.dump_api)

        if not nodes and not a.no_fallback:
            if interactive:
                nodes = prompt_ip_range(a)
            elif any(arg.split("=", 1)[0] == "--ip-range" for arg in sys.argv[1:]):
                warn(f"Falling back to the supplied IP range ({a.ip_range})")
                nodes = discover_range(a.ip_range, probe=True, port=a.ssh_port)

    elif a.discover == "vcenter":
        if not a.vc_host:
            error("--discover vcenter requires --vc-host")
            return 2
        if not a.vc_pass:
            a.vc_pass = masked_input(f"  Password for {a.vc_user}: ")
        nodes = discover_vcenter(a.vc_host, a.vc_user, a.vc_pass, a.vm_prefix, verify)
        if not nodes and not a.no_fallback and a.ip_range:
            warn(f"Falling back to IP range probe ({a.ip_range})")
            nodes = discover_range(a.ip_range, probe=True, port=a.ssh_port)

    elif a.discover == "range":
        if not a.ip_range:
            error("--discover range requires --ip-range")
            return 2
        nodes = discover_range(a.ip_range, probe=not a.no_probe, port=a.ssh_port)

    else:
        if not a.nodes_file:
            p.error("--discover file requires --nodes-file")
        nodes = discover_file(a.nodes_file)

    if not nodes:
        error("No nodes discovered. Try a different --discover mode.")
        return 2

    # --- node list --------------------------------------------------------
    resolve_node_names(nodes)
    banner(f"Discovered nodes ({len(nodes)})")
    w_ip = max([len(n["ip"]) for n in nodes] + [10])
    w_name = max([len(n.get("name", "")) for n in nodes] + [7])
    print(_c(f"  {'IP'.ljust(w_ip)}  {'VM NAME'.ljust(w_name)}  NODE TYPE",
             Fore.CYAN + Style.BRIGHT), file=sys.stderr)
    for n in nodes:
        print(f"  {n['ip'].ljust(w_ip)}  {n.get('name', '').ljust(w_name)}  "
              f"{_c(n.get('type', ''), Fore.MAGENTA)}", file=sys.stderr)
    if a.list_only:
        return 0

    # --- SSH audit --------------------------------------------------------
    if interactive:
        users = prompt_node_credentials(a)
    else:
        users = [u.strip() for u in a.users.split(",") if u.strip()]
        if not a.ssh_pass and not a.ssh_key:
            a.ssh_pass = masked_input(f"  SSH password for {a.ssh_user}: ") or None
    if not a.sudo_pass and a.escalate != "none":
        # Same account, same password - that is the documented VCF workflow
        a.sudo_pass = a.ssh_pass
        if a.ssh_key and not a.sudo_pass:
            a.sudo_pass = masked_input(f"  sudo password for {a.ssh_user}: ") or None

    banner("Password expiry audit")
    info(f"Accounts checked on each node: {', '.join(users)}")
    info(f"Escalation mode: {a.escalate}")

    # ---- confirm the write, it touches every node at once ----------------
    set_max_days: Optional[int] = None
    if a.no_expire:
        target = f"chage -M {a.max_days}"
        warn(f"--no-expire will run '{target} <user>' for {', '.join(users)} "
             f"on all {len(nodes)} node(s)")
        if a.yes:
            set_max_days = a.max_days
        elif sys.stdin.isatty():
            reply = input(_c("  Type 'yes' to proceed: ", Fore.YELLOW)).strip().lower()
            if reply == "yes":
                set_max_days = a.max_days
            else:
                info("Change skipped - reporting the current settings only")
        else:
            error("Refusing to modify anything without confirmation. Add --yes.")
            return 2

    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(check_node, n, users, a.ssh_user, a.ssh_pass, a.ssh_key,
                          a.sudo_pass, a.escalate, a.ssh_port, a.timeout,
                          set_max_days)
                for n in nodes]
        for f in as_completed(futs):
            rows.extend(f.result())

    rows.sort(key=lambda r: (r.get("ip", ""), r.get("user", "")))
    print()
    if set_max_days is not None:
        print(_c("Before / after", Fore.CYAN + Style.BRIGHT))
        print_change_table(rows)
    else:
        print_table(rows)

    # --- summary ----------------------------------------------------------
    problems = [r for r in rows if r.get("error")]
    methods = sorted({r.get("method", "") for r in rows if r.get("method")})
    print()
    if methods:
        print(f"Escalation used: {', '.join(methods)}")

    if set_max_days is not None:
        changed = [r for r in rows if r.get("changed")]
        apply_failed = [r for r in rows if r.get("apply_error")]
        still_expiring = [r for r in rows if isinstance(r.get("new_days_left"), int)
                          and r["new_days_left"] <= 30]
        print(f"Records: {len(rows)}   "
              f"{_c(f'changed: {len(changed)}', Fore.GREEN if changed else Fore.YELLOW)}   "
              f"{_c(f'unreachable: {len(problems)}', Fore.RED if problems else Fore.GREEN)}   "
              f"{_c(f'not applied: {len(apply_failed)}', Fore.RED if apply_failed else Fore.GREEN)}")
        for r in apply_failed:
            print(_c(f"  x {r['ip']} / {r['user']}: {r['apply_error']}", Fore.RED))
        for r in still_expiring:
            print(_c(f"  ! {r['ip']} / {r['user']}: still expires in "
                     f"{r['new_days_left']} day(s)", Fore.YELLOW))
    else:
        expiring = [r for r in rows
                    if isinstance(r.get("days_left"), int) and r["days_left"] <= 30]
        print(f"Records: {len(rows)}   "
              f"{_c(f'errors: {len(problems)}', Fore.RED if problems else Fore.GREEN)}   "
              f"{_c(f'expiring within 30 days: {len(expiring)}', Fore.YELLOW if expiring else Fore.GREEN)}")
        for r in sorted(expiring, key=lambda x: x["days_left"]):
            d = r["days_left"]
            label = "ALREADY EXPIRED" if d < 0 else f"expires in {d} day(s)"
            print(_c(f"  ! {r['ip']} / {r['user']}: {label} ({r.get('password_expires')})",
                     days_color(d)))

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        ok(f"JSON written to {a.json_out}")
    if a.csv_out:
        write_csv(rows, a.csv_out)
        ok(f"CSV written to {a.csv_out}")

    failed_writes = [r for r in rows if r.get("apply_error")]
    return 1 if (problems or failed_writes) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        error("Interrupted by user")
        sys.exit(130)
