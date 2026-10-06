#!/usr/bin/env python3
"""
nsx_dhcp_leases.py - list and optionally delete DHCP leases on a given NSX 4.x segment.

Policy API endpoints used:
  read:   GET  /policy/api/v1/infra/dhcp-server-configs/{config-id}/leases
               ?connectivity_path=<tier-1 or segment path>&segment_path=<segment path>
  delete: POST /policy/api/v1/infra/segments/{segment-id}?action=delete_dhcp_leases
          POST /policy/api/v1/infra/tier-1s/{t1-id}/segments/{segment-id}?action=delete_dhcp_leases
          body: {"leases": [{"ip": "...", "mac": "..."}]}

NSX requires an EXACT ip + mac pair when deleting. The script therefore always
fetches the current leases first and completes the pair for you - you only need
to supply an IP, a MAC, or both.

The segment can be given either as its ID or its display name.

Examples (Linux / macOS):
    export NSX_PASSWORD='secret'

    # list
    ./nsx_dhcp_leases.py -m nsx.example.com -u admin -s ls-prod-web --insecure
    ./nsx_dhcp_leases.py -m nsx.example.com -u admin -s ls-prod-web --json

    # delete specific leases (repeatable, IP and MAC may be mixed)
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --delete 10.20.30.105
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --delete 00:50:56:ae:6b:01
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web \
        --delete 10.20.30.105/00:50:56:ae:6b:01 --delete 10.20.30.9

    # delete every lease on the segment
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --delete-all --yes

    # delete every lease with a given lease time
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --lease-time 86400 --delete-all
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --lease-time '<600' --delete-all
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --lease-time 3600-7200 --delete-all

    # generic filter on any lease field
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --filter 'mac=00:50:56:*' --delete-all
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --filter 'ip=10.20.30.1*'

    # dry run, nothing is deleted
    ./nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --delete-all --dry-run

Examples (Windows 10 / 11, cmd.exe or PowerShell):
    cmd:        set NSX_PASSWORD=secret
    PowerShell: $env:NSX_PASSWORD = 'secret'

    py -3 nsx_dhcp_leases.py -m nsx.example.com -u admin -s ls-prod-web --insecure
    py -3 nsx_dhcp_leases.py -m nsx.example.com -s ls-prod-web --lease-time 86400 --delete-all

    Note for cmd.exe: quote values containing < or > with double quotes,
    e.g. --lease-time "<600", otherwise the shell treats them as redirection.

Exit codes:
    0  success
    1  nothing matched the given selection
    2  error (connection, authentication, bad arguments, API error)
    130 interrupted by the user

Requirements: requests          (pip install requests)
Optional:     colorama          (pip install colorama)  - colored output on Windows
"""

import argparse
import fnmatch
import getpass
import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("Missing module 'requests'. Install it with: pip install requests")


API = "/policy/api/v1"
DELETE_CHUNK = 100  # how many leases to send in a single POST request

EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130


# ---------------------------------------------------------------- console / color

def _enable_windows_vt_mode():
    """
    Turn on ANSI escape processing in the Windows 10+ console.

    Used as a fallback when colorama is not installed. Windows 10 build 1511
    and later supports VT sequences, but the flag is off by default for
    conhost (cmd.exe). Windows Terminal and PowerShell 7 already have it on.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ok = False
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = wintypes.DWORD()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            ):
                ok = True
        return ok
    except Exception:
        return False


def _fix_windows_encoding():
    """
    Make stdout/stderr UTF-8 safe on Windows.

    The legacy console code page (cp852, cp1250, ...) raises
    UnicodeEncodeError on characters coming back from the NSX API, for example
    in segment display names. Falling back to 'replace' is better than crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


class Palette:
    """ANSI colors with a no-op mode, so every call site stays identical."""

    _CODES = {
        "head": "\033[1;36m",   # bold cyan
        "ok": "\033[32m",       # green
        "warn": "\033[33m",     # yellow
        "err": "\033[1;31m",    # bold red
        "danger": "\033[31m",   # red
        "dim": "\033[90m",      # grey
        "bold": "\033[1m",
    }
    _RESET = "\033[0m"

    def __init__(self, enabled=True):
        self.enabled = enabled

    def __call__(self, style, text):
        if not self.enabled or style not in self._CODES:
            return str(text)
        return f"{self._CODES[style]}{text}{self._RESET}"


def setup_console(color_mode):
    """
    Initialise console output and return a Palette.

    color_mode: 'auto' (color only on a real terminal), 'always', or 'never'.
    Honors the NO_COLOR convention (https://no-color.org/).
    """
    _fix_windows_encoding()

    if color_mode == "never" or os.environ.get("NO_COLOR"):
        return Palette(False)

    is_tty = sys.stdout.isatty()
    if color_mode == "auto" and not is_tty:
        return Palette(False)

    # colorama is the reliable path on Windows: it wraps the streams and
    # translates ANSI codes for consoles that cannot handle them natively.
    try:
        import colorama

        colorama.init(strip=False if color_mode == "always" else None,
                      convert=None, autoreset=False)
        return Palette(True)
    except ImportError:
        pass
    except Exception:
        pass

    if os.name == "nt" and not _enable_windows_vt_mode():
        # Old console without VT support and without colorama - plain text.
        return Palette(False)

    return Palette(True)


# ---------------------------------------------------------------- errors

class NsxError(Exception):
    pass


class NotFound(NsxError):
    pass


# ---------------------------------------------------------------- API client

class NsxClient:
    def __init__(self, manager, user, password, verify=True, timeout=60):
        self.base = f"https://{manager.rstrip('/')}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.verify = verify
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method, path, params=None, body=None):
        url = self.base + path
        try:
            r = self.s.request(method, url, params=params, json=body,
                               timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            raise NsxError(
                f"TLS error while connecting to {self.base}: {e}\n"
                "Use --ca-bundle with the CA certificate, or --insecure (lab only)."
            )
        except requests.exceptions.RequestException as e:
            raise NsxError(f"Request to {url} failed: {e}")

        if r.status_code == 404:
            raise NotFound(f"404 Not Found: {path}")
        if r.status_code in (401, 403):
            raise NsxError(
                f"HTTP {r.status_code} - authentication or authorization failed. "
                "Reading needs a read-only role; deleting needs write access."
            )
        if r.status_code >= 400:
            try:
                b = r.json()
                detail = b.get("error_message") or json.dumps(b)[:500]
            except ValueError:
                detail = r.text[:500]
            raise NsxError(f"HTTP {r.status_code} on {method} {path}: {detail}")

        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, params=None, body=None):
        return self._request("POST", path, params=params, body=body)


# ---------------------------------------------------------------- segment lookup

def resolve_segment(client, ident):
    """Find a segment by ID or display name. Returns the full segment object."""
    try:
        return client.get(f"{API}/infra/segments/{ident}")
    except NotFound:
        pass

    # The search API also finds fixed segments under /infra/tier-1s/<t1>/segments/<id>
    query = f'resource_type:Segment AND (display_name:"{ident}" OR id:"{ident}")'
    try:
        res = client.get(f"{API}/search/query", params={"query": query, "page_size": 50})
    except NsxError:
        res = {"results": []}

    results = res.get("results") or []
    if not results:
        raise NsxError(f"Segment '{ident}' not found (neither as an ID nor a display name).")
    if len(results) > 1:
        names = ", ".join(f"{x.get('display_name')} (id={x.get('id')})" for x in results[:10])
        raise NsxError(
            f"Multiple segments match '{ident}': {names}\n"
            "Pass the exact segment ID with -s."
        )

    hit = results[0]
    path = hit.get("path")
    return client.get(f"{API}{path}") if path else hit


def resolve_dhcp(client, segment, override_config=None, override_connectivity=None):
    """
    Work out which DHCP server config the segment uses and which connectivity_path
    the lease query needs.

    Per the NSX API:
      - DHCP server local to the segment  -> connectivity_path = segment path
      - DHCP server on a Tier-0/Tier-1    -> connectivity_path = gateway path
    """
    seg_path = segment.get("path") or f"/infra/segments/{segment.get('id')}"

    if override_config:
        return override_config, (override_connectivity or seg_path), "manually specified"

    dhcp_cfg = segment.get("dhcp_config_path")
    if dhcp_cfg:
        if "/dhcp-relay-configs/" in dhcp_cfg:
            raise NsxError(
                f"Segment '{segment.get('display_name')}' uses DHCP relay "
                f"({dhcp_cfg}), not a DHCP server. The leases live on the external "
                "DHCP server, so NSX can neither list nor delete them."
            )
        return dhcp_cfg, (override_connectivity or seg_path), "local DHCP server on the segment"

    conn_path = segment.get("connectivity_path")
    if not conn_path:
        raise NsxError(
            f"Segment '{segment.get('display_name')}' has neither dhcp_config_path "
            "nor connectivity_path - no DHCP server is configured for it."
        )

    try:
        gw = client.get(f"{API}{conn_path}")
    except NsxError as e:
        raise NsxError(f"Could not read gateway {conn_path}: {e}")

    cfg_paths = gw.get("dhcp_config_paths") or []
    if not cfg_paths:
        raise NsxError(
            f"Segment '{segment.get('display_name')}' has no DHCP config of its own, "
            f"and the connected gateway '{gw.get('display_name')}' ({conn_path}) has "
            "none either. There is most likely no NSX DHCP server on this segment."
        )

    cfg = cfg_paths[0]
    if "/dhcp-relay-configs/" in cfg:
        raise NsxError(
            f"Gateway '{gw.get('display_name')}' uses DHCP relay ({cfg}), "
            "not a DHCP server. NSX holds no leases."
        )
    return (cfg, (override_connectivity or conn_path),
            f"DHCP server on gateway '{gw.get('display_name')}'")


# ---------------------------------------------------------------- reading leases

def fetch_leases(client, config_path, connectivity_path, segment_path,
                 enforcement_point=None, max_pages=200):
    """Fetch all leases, following the cursor through every page."""
    config_id = config_path.rstrip("/").split("/")[-1]
    url = f"{API}/infra/dhcp-server-configs/{config_id}/leases"

    params = {"connectivity_path": connectivity_path, "segment_path": segment_path}
    if enforcement_point:
        params["enforcement_point_path"] = enforcement_point

    leases, meta = [], {}
    cursor, seen_cursors = None, set()

    for _ in range(max_pages):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = client.get(url, params=p)

        if not meta:
            meta = {
                "dhcp_server_id": data.get("dhcp_server_id"),
                "timestamp": data.get("timestamp"),
            }

        page = data.get("leases") or []
        leases.extend(page)

        cursor = data.get("cursor")
        if not cursor or not page or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    return leases, meta


# ---------------------------------------------------------------- filtering

# Aliases so you can write --filter ip=... instead of ip_address=...
FIELD_ALIASES = {
    "ip": "ip_address",
    "address": "ip_address",
    "mac": "mac_address",
    "lease-time": "lease_time",
    "leasetime": "lease_time",
    "expire": "expire_time",
    "start": "start_time",
    "host": "hostname",
}


def _num(v):
    """Try to read the value as a number; return None if it is not numeric."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_match_spec(spec):
    """
    Turn a textual spec into a predicate over a field value.

    Supported forms:
        86400          exact match (numeric when both sides are numbers, else text)
        >3600 >=3600   numeric comparison (also <, <=, !=)
        3600-7200      numeric range, both bounds inclusive
        00:50:56:*     glob (wildcards * and ?)
    """
    s = str(spec).strip()
    if not s:
        raise NsxError("Empty filter specification.")

    for op in (">=", "<=", "!=", ">", "<"):
        if s.startswith(op):
            rhs = s[len(op):].strip()
            rnum = _num(rhs)
            if rnum is None:
                raise NsxError(f"Operator '{op}' needs a number, got '{rhs}'.")

            def pred(value, op=op, rnum=rnum):
                vnum = _num(value)
                if vnum is None:
                    return False
                return {
                    ">=": vnum >= rnum, "<=": vnum <= rnum,
                    ">": vnum > rnum, "<": vnum < rnum,
                    "!=": vnum != rnum,
                }[op]
            return pred

    # Range 3600-7200, only when both sides are numeric so MACs and dates survive.
    if "-" in s[1:]:
        lo_s, _, hi_s = s.partition("-")
        lo, hi = _num(lo_s), _num(hi_s)
        if lo is not None and hi is not None:
            def pred(value, lo=lo, hi=hi):
                vnum = _num(value)
                return vnum is not None and lo <= vnum <= hi
            return pred

    if any(ch in s for ch in "*?["):
        def pred(value, pat=s.lower()):
            return fnmatch.fnmatch(str(value or "").strip().lower(), pat)
        return pred

    snum = _num(s)

    def pred(value, s=s.lower(), snum=snum):
        if value is None:
            return False
        if snum is not None:
            vnum = _num(value)
            if vnum is not None:
                return vnum == snum
        return str(value).strip().lower() == s
    return pred


def build_filters(filter_args, lease_time_arg):
    """Build a list of (field, predicate, original_spec) from --filter / --lease-time."""
    filters = []

    if lease_time_arg:
        filters.append(("lease_time", parse_match_spec(lease_time_arg),
                        f"lease_time={lease_time_arg}"))

    for raw in (filter_args or []):
        if "=" not in raw:
            raise NsxError(
                f"Bad --filter format '{raw}'. Expected FIELD=VALUE, "
                "for example --filter lease_time=86400"
            )
        field, _, spec = raw.partition("=")
        field = field.strip().lower()
        field = FIELD_ALIASES.get(field, field)
        filters.append((field, parse_match_spec(spec), raw))

    return filters


def apply_filters(leases, filters):
    """Return only the leases matching ALL filters (AND)."""
    out = leases
    for field, pred, _raw in filters:
        out = [l for l in out if pred(l.get(field))]
    return out


# ---------------------------------------------------------------- deleting leases

def _norm_mac(v):
    if not v:
        return None
    return str(v).strip().lower().replace("-", ":")


def _norm_ip(v):
    if not v:
        return None
    return str(v).strip().split("/")[0]


def _looks_like_mac(v):
    return v.count(":") >= 2 or (v.count("-") >= 2 and "." not in v)


def parse_delete_spec(spec):
    """
    Parse one --delete argument into an (ip, mac) pair; a missing half is None.

    Accepts:  '10.20.30.5'
              '00:50:56:ae:6b:01'
              '10.20.30.5/00:50:56:ae:6b:01'   (',' and '|' work as separators too)
    """
    s = str(spec).strip()
    if not s:
        raise NsxError("Empty --delete argument.")

    for sep in ("/", ",", "|"):
        if sep in s:
            a, b = s.split(sep, 1)
            a, b = a.strip(), b.strip()
            if _looks_like_mac(a) and not _looks_like_mac(b):
                a, b = b, a  # the user wrote them the other way round
            return (_norm_ip(a) or None), (_norm_mac(b) or None)

    if _looks_like_mac(s):
        return None, _norm_mac(s)
    return _norm_ip(s), None


def select_leases_for_delete(leases, specs):
    """
    Pick the leases matching the given --delete specs.
    Returns (selected_leases, unmatched_specs).
    """
    chosen, seen, missing = [], set(), []

    for spec in specs:
        ip, mac = parse_delete_spec(spec)
        hits = [
            l for l in leases
            if (ip is None or _norm_ip(l.get("ip_address")) == ip)
            and (mac is None or _norm_mac(l.get("mac_address")) == mac)
        ]
        if not hits:
            missing.append(spec)
            continue
        for l in hits:
            key = (_norm_ip(l.get("ip_address")), _norm_mac(l.get("mac_address")))
            if key not in seen:
                seen.add(key)
                chosen.append(l)

    return chosen, missing


def delete_leases(client, segment_path, leases, enforcement_point=None):
    """
    Delete the given leases. NSX needs an exact ip + mac pair for each one.
    POSTing to the segment path covers both /infra/segments/<id> and
    /infra/tier-1s/<t1>/segments/<id>.
    Returns (number_sent, skipped_leases).
    """
    entries, skipped = [], []
    for l in leases:
        ip = _norm_ip(l.get("ip_address"))
        mac = _norm_mac(l.get("mac_address"))
        if not ip or not mac:
            skipped.append(l)
            continue
        entries.append({"ip": ip, "mac": mac})

    if not entries:
        return 0, skipped

    params = {"action": "delete_dhcp_leases"}
    if enforcement_point:
        params["enforcement_point_path"] = enforcement_point

    sent = 0
    for i in range(0, len(entries), DELETE_CHUNK):
        chunk = entries[i:i + DELETE_CHUNK]
        client.post(f"{API}{segment_path}", params=params, body={"leases": chunk})
        sent += len(chunk)

    return sent, skipped


# ---------------------------------------------------------------- output

def print_table(leases, c, highlight=False):
    """
    Print leases as an aligned ASCII table.

    Plain ASCII on purpose: box-drawing characters break on legacy Windows
    code pages. highlight=True colors the rows as deletion targets.
    """
    cols = [
        ("ip_address", "IP ADDRESS"),
        ("mac_address", "MAC"),
        ("hostname", "HOSTNAME"),
        ("subnet", "SUBNET"),
        ("lease_time", "LEASE(s)"),
        ("start_time", "START"),
        ("expire_time", "EXPIRE"),
    ]
    # Drop columns that are empty everywhere (NSX often omits hostname).
    cols = [c_ for c_ in cols if any(str(l.get(c_[0], "")).strip() for l in leases)]

    widths = []
    for key, head in cols:
        widths.append(max([len(head)] + [len(str(l.get(key, "") or "-")) for l in leases]))

    print(c("head", "  ".join(h.ljust(w) for (_, h), w in zip(cols, widths))))
    print(c("dim", "  ".join("-" * w for w in widths)))
    for l in sorted(leases, key=lambda x: _ip_key(x.get("ip_address", ""))):
        row = "  ".join(
            str(l.get(key, "") or "-").ljust(w) for (key, _), w in zip(cols, widths)
        ).rstrip()
        print(c("danger", row) if highlight else row)


def _ip_key(ip):
    try:
        return tuple(int(x) for x in str(ip).split("/")[0].split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)


def warn(c, msg):
    print(c("warn", f"WARNING: {msg}"), file=sys.stderr)


def confirm(c, prompt):
    if not sys.stdin.isatty():
        raise NsxError(
            "Cannot ask for confirmation (stdin is not a terminal). "
            "Add --yes for non-interactive runs."
        )
    try:
        answer = input(c("warn", f"{prompt} [yes/NO]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("yes", "y")


# ---------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(
        description="List and optionally delete DHCP leases on an NSX 4.x segment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="--delete is repeatable and accepts an IP, a MAC, or 'IP/MAC'.",
    )
    ap.add_argument("-m", "--manager", required=True,
                    help="NSX Manager FQDN or IP (or the cluster VIP)")
    ap.add_argument("-u", "--user", default=os.environ.get("NSX_USER", "admin"),
                    help="username (default: admin, or $NSX_USER)")
    ap.add_argument("-p", "--password", default=os.environ.get("NSX_PASSWORD"),
                    help="password (prefer $NSX_PASSWORD; prompted for otherwise)")
    ap.add_argument("-s", "--segment", required=True,
                    help="segment ID or display name")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--delete", action="append", metavar="IP|MAC|IP/MAC",
                   help="delete a specific lease; may be given several times")
    g.add_argument("--delete-all", action="store_true",
                   help="delete EVERY lease on the segment (narrowed by filters)")

    ap.add_argument("--lease-time", metavar="SPEC",
                    help="filter on lease time: '86400', '>3600', '<=600', '3600-7200'")
    ap.add_argument("--filter", action="append", metavar="FIELD=SPEC",
                    help="generic filter on a lease field (ip, mac, lease_time, subnet, "
                         "hostname, start_time, expire_time); supports >, <, >=, <=, !=, "
                         "ranges and * wildcards; repeatable (AND)")

    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the deletion prompt (for scripts and scheduled jobs)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be deleted and delete nothing")

    ap.add_argument("--dhcp-config-path",
                    help="explicit DHCP server config path "
                         "(/infra/dhcp-server-configs/xxx), bypasses autodetection")
    ap.add_argument("--connectivity-path",
                    help="explicit connectivity_path (tier-0/tier-1/segment)")
    ap.add_argument("--enforcement-point",
                    help="enforcement_point_path, e.g. "
                         "/infra/sites/default/enforcement-points/default")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of a table")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="colored output (default: auto - only on a terminal)")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate verification (lab use only)")
    ap.add_argument("--ca-bundle", help="path to the CA certificate used for TLS verification")
    ap.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    return ap


def run(args, c):
    deleting = bool(args.delete or args.delete_all)

    password = args.password or getpass.getpass(f"Password for {args.user}@{args.manager}: ")

    verify = args.ca_bundle if args.ca_bundle else True
    if args.insecure:
        verify = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except ImportError:
            pass

    client = NsxClient(args.manager, args.user, password,
                       verify=verify, timeout=args.timeout)

    segment = resolve_segment(client, args.segment)
    seg_path = segment.get("path") or f"/infra/segments/{segment.get('id')}"
    seg_name = segment.get("display_name")

    cfg_path, conn_path, kind = resolve_dhcp(
        client, segment,
        override_config=args.dhcp_config_path,
        override_connectivity=args.connectivity_path,
    )

    all_leases, meta = fetch_leases(
        client, cfg_path, conn_path, seg_path,
        enforcement_point=args.enforcement_point,
    )

    filters = build_filters(args.filter, args.lease_time)
    leases = apply_filters(all_leases, filters)
    filter_desc = ", ".join(raw for _f, _p, raw in filters)

    # ---------------------------------------------------------- list mode
    if not deleting:
        if args.json:
            print(json.dumps({
                "segment": {"id": segment.get("id"), "display_name": seg_name,
                            "path": seg_path},
                "dhcp_config_path": cfg_path,
                "connectivity_path": conn_path,
                "dhcp_server_id": meta.get("dhcp_server_id"),
                "timestamp": meta.get("timestamp"),
                "filters": filter_desc or None,
                "total_on_segment": len(all_leases),
                "lease_count": len(leases),
                "leases": leases,
            }, indent=2, ensure_ascii=False))
            return EXIT_OK

        print(f"{c('bold', 'Segment:')}     {seg_name} (id={segment.get('id')})")
        print(f"{c('bold', 'DHCP:')}        {kind}")
        print(c("dim", f"  config:    {cfg_path}"))
        print(c("dim", f"  conn path: {conn_path}"))
        if meta.get("dhcp_server_id"):
            print(c("dim", f"  server id: {meta['dhcp_server_id']}"))
        if filters:
            print(f"{c('bold', 'Filter:')}      {filter_desc}")
            print(f"{c('bold', 'Leases:')}      {len(leases)} "
                  f"(of {len(all_leases)} on the segment)\n")
        else:
            print(f"{c('bold', 'Leases:')}      {len(leases)}\n")

        if not leases:
            print(c("warn", "No leases match the filter." if filters
                            else "No active leases on this segment."))
            return EXIT_OK
        print_table(leases, c)
        return EXIT_OK

    # ---------------------------------------------------------- delete mode
    if not leases:
        msg = ("No leases match the filter, nothing to delete." if filters
               else "There are no leases on this segment, nothing to delete.")
        if args.json:
            print(json.dumps({"deleted": 0, "message": msg}, indent=2, ensure_ascii=False))
        else:
            print(c("warn", msg))
        return EXIT_OK

    if args.delete_all:
        targets, missing = list(leases), []
    else:
        targets, missing = select_leases_for_delete(leases, args.delete)

    for spec in missing:
        warn(c, f"'{spec}' does not match any active lease on this segment.")

    if not targets:
        print(c("err", "Nothing to delete - no selection matched an active lease."),
              file=sys.stderr)
        return EXIT_NO_MATCH

    if not args.json:
        print(f"{c('bold', 'Segment:')}   {seg_name} (id={segment.get('id')})")
        if filters:
            print(f"{c('bold', 'Filter:')}    {filter_desc}")
        print(f"{c('bold', 'To delete:')} "
              f"{c('danger', str(len(targets)))} of {len(all_leases)} leases "
              f"on the segment\n")
        print_table(targets, c, highlight=True)
        print()

    if args.dry_run:
        if args.json:
            print(json.dumps({"dry_run": True, "would_delete": len(targets),
                              "leases": targets}, indent=2, ensure_ascii=False))
        else:
            print(c("ok", "DRY RUN: nothing was deleted."))
        return EXIT_OK

    if not args.yes:
        what = (f"ALL {len(targets)} leases" if args.delete_all and not filters
                else f"{len(targets)} lease(s)")
        if not confirm(c, f"Really delete {what} on segment '{seg_name}'?"):
            print(c("ok", "Cancelled, nothing was deleted."))
            return EXIT_OK

    sent, skipped = delete_leases(client, seg_path, targets,
                                  enforcement_point=args.enforcement_point)

    for l in skipped:
        warn(c, f"skipped a lease with no IP or MAC: {l}")

    # Verify by re-reading the leases.
    remaining, _ = fetch_leases(client, cfg_path, conn_path, seg_path,
                                enforcement_point=args.enforcement_point)

    result = {
        "segment": seg_name,
        "filters": filter_desc or None,
        "requested": len(targets),
        "sent": sent,
        "skipped": len(skipped),
        "remaining_on_segment": len(remaining),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(c("ok", f"Deleted {sent} lease(s)."))
        print(f"Remaining on the segment: {len(remaining)}")
        print(c("dim", "\nNote: deleting a lease does not take the IP away from a "
                       "running client - it keeps using it until the next DHCP "
                       "renew or reboot."))
    return EXIT_OK


def main(argv=None):
    args = build_parser().parse_args(argv)
    c = setup_console(args.color)
    try:
        return run(args, c)
    except NsxError as e:
        print(c("err", f"ERROR: {e}"), file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(c("warn", "\nInterrupted."), file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
